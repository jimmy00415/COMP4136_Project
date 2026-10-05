from __future__ import annotations

import hashlib
import io
import json
import os
import sys
from collections.abc import Callable
from dataclasses import dataclass, field
from pathlib import Path
from types import SimpleNamespace

import pytest

from hk_movie_rag import cli, gcs_archive
from hk_movie_rag.gcs_archive import (
    ArchiveAuthority,
    ArchiveError,
    ArchiveObject,
    ArchiveVerificationReport,
    assert_archive_authority_current,
    assert_authority_state_current,
    establish_archive_authority,
    upload_archive,
    upload_one,
    verify_archive,
    verify_one,
    write_verification_report,
)
from hk_movie_rag.inventory import InventoryEntry


@dataclass
class FakeBlob:
    name: str
    content: bytes | None = None
    metadata: dict[str, str] | None = None
    generation: int | None = None
    upload_preconditions: list[int] = field(default_factory=list)
    before_upload_read: Callable[[], None] | None = None
    reject_buffered_download: bool = False
    raise_precondition_once: bool = False
    before_precondition_failure: Callable[[], None] | None = None

    @property
    def size(self) -> int | None:
        return None if self.content is None else len(self.content)

    def upload_from_file(self, stream: io.BufferedReader, *, if_generation_match: int) -> None:
        self.upload_preconditions.append(if_generation_match)
        if self.raise_precondition_once:
            self.raise_precondition_once = False
            if self.before_precondition_failure is not None:
                self.before_precondition_failure()
            from google.api_core.exceptions import PreconditionFailed

            raise PreconditionFailed("racing creator")
        if self.content is not None and if_generation_match == 0:
            raise RuntimeError("precondition failed")
        if self.before_upload_read is not None:
            self.before_upload_read()
        self.content = stream.read()
        self.generation = 1 if self.generation is None else self.generation + 1

    def download_to_file(self, stream: io.BufferedWriter, *, if_generation_match: int) -> None:
        if self.content is None:
            raise RuntimeError("missing object")
        if if_generation_match != self.generation:
            raise RuntimeError("generation changed")
        if self.reject_buffered_download and hasattr(stream, "getvalue"):
            raise RuntimeError("download destination must not buffer full object bytes")
        stream.write(self.content)


class FakeBucket:
    def __init__(self, name: str = "archive-bucket") -> None:
        self.name = name
        self.project_number = 123
        self.location = "US-CENTRAL1"
        self.iam_configuration = SimpleNamespace(
            uniform_bucket_level_access_enabled=True, public_access_prevention="enforced"
        )
        self.versioning_enabled = True
        self.soft_delete_policy = SimpleNamespace(retention_duration_seconds=604800)
        self.lifecycle_rules: list[object] = []
        self.metageneration = 1
        writer = "serviceAccount:hk-rag-archive-writer@project-a.iam.gserviceaccount.com"
        self.iam_bindings: list[dict[str, object]] = [
            {"role": "roles/storage.objectCreator", "members": [writer]},
            {"role": "roles/storage.objectViewer", "members": [writer]},
        ]
        self._blobs: dict[str, FakeBlob] = {}
        self.list_call_count = 0
        self.before_second_list: Callable[[], None] | None = None

    def blob(self, name: str) -> FakeBlob:
        return self._blobs.setdefault(name, FakeBlob(name=name))

    def get_blob(
        self, name: str, *, generation: int | None = None
    ) -> FakeBlob | None:
        blob = self._blobs.get(name)
        if blob is None or blob.content is None:
            return None
        if generation is not None and generation != blob.generation:
            return None
        return blob

    def list_blobs(self, *, prefix: str, versions: bool = False) -> list[FakeBlob]:
        assert versions is False
        self.list_call_count += 1
        if self.list_call_count == 2 and self.before_second_list is not None:
            self.before_second_list()
        return [
            blob
            for name, blob in sorted(self._blobs.items())
            if name.startswith(prefix) and blob.content is not None
        ]

    def add(self, name: str, content: bytes, *, metadata: dict[str, str]) -> FakeBlob:
        blob = self.blob(name)
        blob.content = content
        blob.metadata = metadata
        blob.generation = 7
        return blob

    def reload(self) -> None:
        return None

    def get_iam_policy(self, *, requested_policy_version: int) -> object:
        assert requested_policy_version == 3
        return SimpleNamespace(
            version=3,
            etag="policy-etag",
            bindings=self.iam_bindings,
        )

    def test_iam_permissions(self, permissions: list[str]) -> list[str]:
        return permissions


class FakeStorageClient:
    def __init__(self) -> None:
        self.project = "project-a"
        self.buckets: dict[str, FakeBucket] = {}

    def bucket(self, name: str) -> FakeBucket:
        return self.buckets.setdefault(name, FakeBucket(name))


class RaisingGetBucket(FakeBucket):
    def get_blob(self, name: str, *, generation: int | None = None) -> FakeBlob | None:
        raise RuntimeError("credential secret must not escape")


class RaisingListBucket(FakeBucket):
    def list_blobs(self, *, prefix: str, versions: bool = False) -> list[FakeBlob]:
        def fail() -> object:
            yield from ()
            raise RuntimeError("credential secret must not escape")

        return fail()  # type: ignore[return-value]


@pytest.fixture
def fake_bucket() -> FakeBucket:
    return FakeBucket()


def inventory_entry(relative_path: str, content: bytes) -> ArchiveObject:
    return ArchiveObject(
        inventory=InventoryEntry(
            relative_path=relative_path,
            size_bytes=len(content),
            sha256=hashlib.sha256(content).hexdigest(),
            classification="formal_release_source",
        ),
        local_path=Path("unused"),
        object_name=f"source/v1.2/files/{relative_path}",
    )


def test_existing_object_with_different_hash_aborts(fake_bucket: FakeBucket) -> None:
    entry = inventory_entry("a", b"expected")
    fake_bucket.add(entry.object_name, b"different", metadata={"source_sha256": "bad"})

    with pytest.raises(ArchiveError, match="existing object mismatch"):
        upload_one(fake_bucket, entry, release_version="v1.2", inventory_digest="digest")


def test_verify_streams_remote_bytes_not_only_metadata(fake_bucket: FakeBucket) -> None:
    entry = inventory_entry("a", b"expected")
    fake_bucket.add(
        entry.object_name, b"tampered", metadata={"source_sha256": entry.inventory.sha256}
    )

    with pytest.raises(ArchiveError, match="remote SHA-256 mismatch"):
        verify_one(fake_bucket, entry)


def test_upload_recomputes_local_bytes_and_uses_create_only_precondition(
    tmp_path: Path, fake_bucket: FakeBucket
) -> None:
    local_path = tmp_path / "source.txt"
    local_path.write_bytes(b"expected")
    entry = ArchiveObject(
        inventory=InventoryEntry(
            relative_path="source.txt",
            size_bytes=len(b"expected"),
            sha256=hashlib.sha256(b"expected").hexdigest(),
            classification="formal_release_source",
        ),
        local_path=local_path,
        object_name="source/v1.2/files/source.txt",
    )

    result = upload_one(fake_bucket, entry, release_version="v1.2", inventory_digest="digest")

    blob = fake_bucket.get_blob(entry.object_name)
    assert result.action == "uploaded"
    assert blob is not None
    assert blob.upload_preconditions == [0]
    assert blob.metadata == {
        "source_sha256": entry.inventory.sha256,
        "release_version": "v1.2",
        "inventory_digest": "digest",
    }


def test_upload_rejects_local_bytes_that_drifted_from_inventory(
    tmp_path: Path, fake_bucket: FakeBucket
) -> None:
    local_path = tmp_path / "source.txt"
    local_path.write_bytes(b"changed")
    entry = ArchiveObject(
        inventory=InventoryEntry(
            relative_path="source.txt",
            size_bytes=len(b"expected"),
            sha256=hashlib.sha256(b"expected").hexdigest(),
            classification="formal_release_source",
        ),
        local_path=local_path,
        object_name="source/v1.2/files/source.txt",
    )

    with pytest.raises(ArchiveError, match="local source mismatch"):
        upload_one(fake_bucket, entry, release_version="v1.2", inventory_digest="digest")


def test_upload_sends_the_verified_staged_bytes_when_source_changes_during_upload(
    tmp_path: Path, fake_bucket: FakeBucket
) -> None:
    content = b"expected"
    local_path = tmp_path / "source.txt"
    local_path.write_bytes(content)
    entry = ArchiveObject(
        inventory=InventoryEntry(
            relative_path="source.txt",
            size_bytes=len(content),
            sha256=hashlib.sha256(content).hexdigest(),
            classification="formal_release_source",
        ),
        local_path=local_path,
        object_name="source/v1.2/files/source.txt",
    )
    fake_bucket.blob(entry.object_name).before_upload_read = lambda: local_path.write_bytes(
        b"changed!"
    )

    upload_one(fake_bucket, entry, release_version="v1.2", inventory_digest="digest")

    remote = fake_bucket.get_blob(entry.object_name)
    assert remote is not None
    assert remote.content == content


def test_create_race_reconciles_only_an_identical_winning_source(
    tmp_path: Path, fake_bucket: FakeBucket
) -> None:
    content = b"expected"
    local_path = tmp_path / "source.txt"
    local_path.write_bytes(content)
    entry = ArchiveObject(
        inventory=InventoryEntry(
            relative_path="source.txt",
            size_bytes=8,
            sha256=hashlib.sha256(content).hexdigest(),
            classification="formal_release_source",
        ),
        local_path=local_path,
        object_name="source/v1.2/files/source.txt",
    )
    winner = fake_bucket.blob(entry.object_name)
    winner.raise_precondition_once = True
    winner.before_precondition_failure = lambda: _set_winner(
        winner,
        content,
        {
            "source_sha256": entry.inventory.sha256,
            "release_version": "v1.2",
            "inventory_digest": "digest",
        },
        9,
    )

    result = upload_one(fake_bucket, entry, release_version="v1.2", inventory_digest="digest")

    assert result.action == "skipped_existing"
    assert result.generation == 9


def _set_winner(
    blob: FakeBlob, content: bytes, metadata: dict[str, str], generation: int
) -> None:
    blob.content = content
    blob.metadata = metadata
    blob.generation = generation


def test_upload_and_verify_reject_extra_objects_and_create_immutable_reports(
    tmp_path: Path, fake_bucket: FakeBucket
) -> None:
    source_root = tmp_path / "sources"
    source_root.mkdir()
    content = b"expected"
    (source_root / "a").write_bytes(content)
    inventory_path = tmp_path / "source_inventory.jsonl"
    inventory_path.write_text(
        json.dumps(
            {
                "classification": "formal_release_source",
                "relative_path": "sources/a",
                "sha256": hashlib.sha256(content).hexdigest(),
                "size_bytes": len(content),
            },
            sort_keys=True,
            separators=(",", ":"),
        )
        + "\n",
        encoding="utf-8",
    )

    upload_report = upload_archive(
        fake_bucket,
        repo_root=tmp_path,
        inventory_path=inventory_path,
        release_version="v1.2",
        prefix="source/v1.2",
    )
    verification_report = verify_archive(
        fake_bucket,
        repo_root=tmp_path,
        inventory_path=inventory_path,
        release_version="v1.2",
        prefix="source/v1.2",
    )

    assert upload_report.object_count == 1
    assert verification_report.verified is True
    assert verification_report.object_count == 1
    assert fake_bucket.get_blob("source/v1.2/manifests/source_inventory.jsonl") is not None
    report_blobs = [
        blob
        for blob in fake_bucket.list_blobs(prefix="source/v1.2/manifests/")
        if blob.name.startswith("source/v1.2/manifests/verification-")
    ]
    assert len(report_blobs) == 1
    assert report_blobs[0].upload_preconditions == [0]
    assert report_blobs[0].name.endswith("Z.json")

    fake_bucket.add("source/v1.2/files/unexpected", b"extra", metadata={})
    with pytest.raises(ArchiveError, match="unexpected remote objects"):
        verify_archive(
            fake_bucket,
            repo_root=tmp_path,
            inventory_path=inventory_path,
            release_version="v1.2",
            prefix="source/v1.2",
        )


def test_verify_archive_uses_inventory_after_local_sources_are_detached(
    tmp_path: Path, fake_bucket: FakeBucket
) -> None:
    """Breaks if post-cleanup remote verification reopens detached source files."""
    source_root = tmp_path / "sources"
    source_root.mkdir()
    source = source_root / "a"
    content = b"expected"
    source.write_bytes(content)
    inventory_path = tmp_path / "source_inventory.jsonl"
    inventory_path.write_text(
        json.dumps(
            {
                "classification": "formal_release_source",
                "relative_path": "sources/a",
                "sha256": hashlib.sha256(content).hexdigest(),
                "size_bytes": len(content),
            },
            sort_keys=True,
            separators=(",", ":"),
        )
        + "\n",
        encoding="utf-8",
    )
    upload_archive(
        fake_bucket,
        repo_root=tmp_path,
        inventory_path=inventory_path,
        release_version="v1.2",
        prefix="source/v1.2",
        source_roots=(source_root,),
    )
    source.unlink()
    source_root.rmdir()

    report = verify_archive(
        fake_bucket,
        repo_root=tmp_path,
        inventory_path=inventory_path,
        release_version="v1.2",
        prefix="source/v1.2",
        source_roots=(source_root,),
    )

    assert report.verified is True
    assert report.object_count == 1


def test_detached_archive_verification_still_enforces_configured_source_roots(
    tmp_path: Path, fake_bucket: FakeBucket
) -> None:
    """Breaks if detached verification trusts inventory paths outside its root allowlist."""
    source_root = tmp_path / "other"
    source_root.mkdir()
    source = source_root / "a"
    content = b"expected"
    source.write_bytes(content)
    inventory_path = tmp_path / "source_inventory.jsonl"
    inventory_path.write_text(
        json.dumps(
            {
                "classification": "formal_release_source",
                "relative_path": "other/a",
                "sha256": hashlib.sha256(content).hexdigest(),
                "size_bytes": len(content),
            }
        )
        + "\n",
        encoding="utf-8",
    )
    upload_archive(
        fake_bucket,
        repo_root=tmp_path,
        inventory_path=inventory_path,
        release_version="v1.2",
        prefix="source/v1.2",
    )
    source.unlink()
    source_root.rmdir()

    with pytest.raises(ArchiveError, match="outside configured source roots"):
        verify_archive(
            fake_bucket,
            repo_root=tmp_path,
            inventory_path=inventory_path,
            release_version="v1.2",
            prefix="source/v1.2",
            source_roots=(tmp_path / "approved-detached-root",),
        )


def test_verify_rejects_an_archive_generation_change_before_report(
    tmp_path: Path, fake_bucket: FakeBucket
) -> None:
    source_root = tmp_path / "sources"
    source_root.mkdir()
    content = b"expected"
    (source_root / "a").write_bytes(content)
    inventory_path = tmp_path / "source_inventory.jsonl"
    inventory_path.write_text(
        json.dumps(
            {
                "classification": "formal_release_source",
                "relative_path": "sources/a",
                "sha256": hashlib.sha256(content).hexdigest(),
                "size_bytes": len(content),
            }
        )
        + "\n",
        encoding="utf-8",
    )
    upload_archive(
        fake_bucket,
        repo_root=tmp_path,
        inventory_path=inventory_path,
        release_version="v1.2",
        prefix="source/v1.2",
    )
    source_blob = fake_bucket.get_blob("source/v1.2/files/sources/a")
    assert source_blob is not None
    fake_bucket.before_second_list = lambda: setattr(source_blob, "generation", 8)

    with pytest.raises(ArchiveError, match="remote archive changed"):
        verify_archive(
            fake_bucket,
            repo_root=tmp_path,
            inventory_path=inventory_path,
            release_version="v1.2",
            prefix="source/v1.2",
        )


def test_verify_rejects_a_remote_object_without_a_generation(fake_bucket: FakeBucket) -> None:
    entry = inventory_entry("a", b"expected")
    blob = fake_bucket.add(
        entry.object_name, b"expected", metadata={"source_sha256": entry.inventory.sha256}
    )
    blob.generation = None

    with pytest.raises(ArchiveError, match="no generation"):
        verify_one(fake_bucket, entry)


def test_repeated_upload_skips_an_identical_immutable_inventory(
    tmp_path: Path, fake_bucket: FakeBucket
) -> None:
    source_root = tmp_path / "sources"
    source_root.mkdir()
    content = b"expected"
    (source_root / "a").write_bytes(content)
    inventory_path = tmp_path / "source_inventory.jsonl"
    inventory_path.write_text(
        json.dumps(
            {
                "classification": "formal_release_source",
                "relative_path": "sources/a",
                "sha256": hashlib.sha256(content).hexdigest(),
                "size_bytes": len(content),
            },
            sort_keys=True,
            separators=(",", ":"),
        )
        + "\n",
        encoding="utf-8",
    )

    first = upload_archive(
        fake_bucket,
        repo_root=tmp_path,
        inventory_path=inventory_path,
        release_version="v1.2",
        prefix="source/v1.2",
    )
    second = upload_archive(
        fake_bucket,
        repo_root=tmp_path,
        inventory_path=inventory_path,
        release_version="v1.2",
        prefix="source/v1.2",
    )

    assert first.objects[0].action == "uploaded"
    assert second.objects[0].action == "skipped_existing"


def test_existing_remote_object_does_not_hide_local_inventory_drift(
    tmp_path: Path, fake_bucket: FakeBucket
) -> None:
    source_root = tmp_path / "sources"
    source_root.mkdir()
    content = b"expected"
    source = source_root / "a"
    source.write_bytes(content)
    inventory_path = tmp_path / "source_inventory.jsonl"
    inventory_path.write_text(
        json.dumps(
            {
                "classification": "formal_release_source",
                "relative_path": "sources/a",
                "sha256": hashlib.sha256(content).hexdigest(),
                "size_bytes": len(content),
            },
            sort_keys=True,
            separators=(",", ":"),
        )
        + "\n",
        encoding="utf-8",
    )
    upload_archive(
        fake_bucket,
        repo_root=tmp_path,
        inventory_path=inventory_path,
        release_version="v1.2",
        prefix="source/v1.2",
    )
    source.write_bytes(b"changed")

    with pytest.raises(ArchiveError, match="local source mismatch"):
        upload_archive(
            fake_bucket,
            repo_root=tmp_path,
            inventory_path=inventory_path,
            release_version="v1.2",
            prefix="source/v1.2",
        )


def test_real_storage_sdk_uses_an_explicit_https_endpoint(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    from importlib.metadata import version

    from google.cloud import storage

    for name in (
        "STORAGE_EMULATOR_HOST",
        "API_ENDPOINT_OVERRIDE",
        "GOOGLE_CLOUD_PROJECT",
        "GCLOUD_PROJECT",
        "CLOUDSDK_CORE_PROJECT",
        "GOOGLE_APPLICATION_CREDENTIALS",
        "GOOGLE_OAUTH_ACCESS_TOKEN",
        "CLOUDSDK_API_ENDPOINT_OVERRIDES_STORAGE",
        "GOOGLE_CLOUD_STORAGE_ENDPOINT",
    ):
        monkeypatch.delenv(name, raising=False)
    client = gcs_archive.create_storage_client("exact-account-token", "project-a")
    request_url = client._connection.build_api_url(path="/b", query_params={})  # type: ignore[attr-defined]

    assert isinstance(client, storage.Client)
    assert version("google-cloud-storage") == "3.13.0"
    assert client.project == "project-a"
    assert request_url == "https://storage.googleapis.com/storage/v1/b?prettyPrint=false"


def test_storage_client_rejects_ambient_endpoint_or_identity_overrides(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setenv("STORAGE_EMULATOR_HOST", "http://localhost:4443")

    with pytest.raises(ArchiveError, match="storage client is unavailable"):
        gcs_archive.create_storage_client("exact-account-token")


def test_exact_account_credentials_refresh_only_through_the_bound_refresher() -> None:
    calls = 0

    def refresher() -> str:
        nonlocal calls
        calls += 1
        return "renewed-exact-account-token"

    credentials = gcs_archive._ExactAccountCredentials("initial-token", refresher)
    credentials.refresh(object())

    assert calls == 1
    assert credentials.token == "renewed-exact-account-token"
    assert credentials.valid is True


def test_verify_uses_a_non_buffering_hash_sink(fake_bucket: FakeBucket) -> None:
    entry = inventory_entry("a", b"expected")
    fake_bucket.add(
        entry.object_name, b"expected", metadata={"source_sha256": entry.inventory.sha256}
    )
    fake_bucket.get_blob(entry.object_name).reject_buffered_download = True  # type: ignore[union-attr]

    result = verify_one(fake_bucket, entry)

    assert result.hash_verified is True


def test_local_verification_report_rejects_hard_link_destination(tmp_path: Path) -> None:
    original = tmp_path / "original.json"
    original.write_text("do not replace", encoding="utf-8")
    output = tmp_path / "verification.json"
    os.link(original, output)
    report = ArchiveVerificationReport(
        bucket="archive-bucket",
        prefix="source/v1.2",
        object_count=1,
        total_bytes=8,
        inventory_digest="digest",
        objects=(),
        verification_timestamp="2026-07-28T00:00:00+00:00",
        verified=True,
    )

    with pytest.raises(ArchiveError, match="verification report"):
        write_verification_report(
            report,
            Path("verification.json"),
            repo_root=tmp_path,
            protected_roots=(),
        )

    assert original.read_text(encoding="utf-8") == "do not replace"


@pytest.mark.parametrize(
    "relative_path", ["a\\b", "C:/a", "a/CON", "a/file. ", "a/../b", "a/LONGFI~1.XLS"]
)
def test_inventory_rejects_windows_alias_paths(
    tmp_path: Path, fake_bucket: FakeBucket, relative_path: str
) -> None:
    inventory = tmp_path / "inventory.jsonl"
    inventory.write_text(
        json.dumps(
            {
                "relative_path": relative_path,
                "size_bytes": 1,
                "sha256": "0" * 64,
                "classification": "formal_release_source",
            }
        )
        + "\n",
        encoding="utf-8",
    )
    with pytest.raises(ArchiveError, match="invalid inventory relative path"):
        upload_archive(
            fake_bucket,
            repo_root=tmp_path,
            inventory_path=inventory,
            release_version="v1.2",
            prefix="source/v1.2",
        )


def test_archive_cli_outputs_machine_readable_failure(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]
) -> None:
    settings = SimpleNamespace(
        repo_root=tmp_path,
        inventory_roots=(),
        gcp=SimpleNamespace(project_id="project-a", archive_prefix="source/v1.2"),
        release_version="v1.2",
    )
    (tmp_path / "inventory.jsonl").write_text("", encoding="utf-8")
    monkeypatch.setattr(cli.Settings, "load", lambda _: settings)
    storage_client_calls: list[str] = []
    monkeypatch.setattr(
        cli,
        "establish_archive_authority",
        lambda *_args, **_kwargs: (_ for _ in ()).throw(ArchiveError("authority test")),
    )
    monkeypatch.setattr(
        cli, "create_storage_client", lambda _token: storage_client_calls.append("called")
    )
    monkeypatch.setattr(
        sys,
        "argv",
        [
            "hk-movie-rag",
            "archive-upload",
            "--inventory",
            "inventory.jsonl",
            "--bucket",
            "archive-bucket",
        ],
    )
    monkeypatch.chdir(tmp_path)

    with pytest.raises(SystemExit) as exit_info:
        cli.main()

    assert exit_info.value.code == 1
    assert json.loads(capsys.readouterr().out) == {
        "error": "archive_operation_failed",
        "ok": False,
        "reason": "authority test",
    }
    assert storage_client_calls == []


@pytest.mark.parametrize(
    "relative_path",
    [
        "a/CONIN$",
        "a/CONOUT$",
        "a/COM¹.txt",
        "a/LPT³",
        "a/NUL:stream",
        "a/file.txt:stream",
    ],
)
def test_inventory_rejects_all_windows_reserved_or_ads_names(
    tmp_path: Path, fake_bucket: FakeBucket, relative_path: str
) -> None:
    inventory = tmp_path / "inventory.jsonl"
    inventory.write_text(
        json.dumps(
            {
                "relative_path": relative_path,
                "size_bytes": 1,
                "sha256": "0" * 64,
                "classification": "formal_release_source",
            }
        )
        + "\n",
        encoding="utf-8",
    )

    with pytest.raises(ArchiveError, match="invalid inventory relative path"):
        upload_archive(
            fake_bucket,
            repo_root=tmp_path,
            inventory_path=inventory,
            release_version="v1.2",
            prefix="source/v1.2",
        )


def test_upload_wraps_get_failure_without_credential_text(tmp_path: Path) -> None:
    local = tmp_path / "source.txt"
    local.write_bytes(b"expected")
    entry = ArchiveObject(
        inventory=InventoryEntry(
            relative_path="source.txt",
            size_bytes=8,
            sha256=hashlib.sha256(b"expected").hexdigest(),
            classification="formal_release_source",
        ),
        local_path=local,
        object_name="source/v1.2/files/source.txt",
    )

    with pytest.raises(ArchiveError, match="cannot fetch remote object") as raised:
        upload_one(RaisingGetBucket(), entry, release_version="v1.2", inventory_digest="digest")

    assert "secret" not in str(raised.value)


def test_verify_wraps_list_iterator_failure_without_credential_text(
    tmp_path: Path,
) -> None:
    source_root = tmp_path / "sources"
    source_root.mkdir()
    content = b"expected"
    (source_root / "a").write_bytes(content)
    inventory = tmp_path / "inventory.jsonl"
    inventory.write_text(
        json.dumps(
            {
                "relative_path": "sources/a",
                "size_bytes": len(content),
                "sha256": hashlib.sha256(content).hexdigest(),
                "classification": "formal_release_source",
            }
        )
        + "\n",
        encoding="utf-8",
    )

    with pytest.raises(ArchiveError, match="cannot list remote archive objects") as raised:
        verify_archive(
            RaisingListBucket(),
            repo_root=tmp_path,
            inventory_path=inventory,
            release_version="v1.2",
            prefix="source/v1.2",
        )

    assert "secret" not in str(raised.value)


def test_archive_authority_requires_current_preflight_and_terraform_output() -> None:
    settings = SimpleNamespace(
        repo_root=Path("workspace"),
        gcp=SimpleNamespace(
            project_id="project-a",
            project_number=None,
            required_account="admin@example.com",
            archive_bucket_template="{project_id}-{project_number}-archive",
        ),
    )
    preflight = SimpleNamespace(
        ready_for_archive=True,
        active_account="admin@example.com",
        active_project="project-a",
        project_number="123",
        project_lifecycle="ACTIVE",
        billing_enabled=True,
        caller_iam_visible=True,
        required_services={
            "storage.googleapis.com": "ENABLED",
            "serviceusage.googleapis.com": "ENABLED",
            "cloudresourcemanager.googleapis.com": "ENABLED",
        },
        blockers=[],
    )

    client = FakeStorageClient()
    client.bucket("motionexpaiweb-880586285913-hk-movie-rag-tfstate").add(
        "hk-movie-rag/archive/default.tfstate",
        _terraform_state("project-a-123-archive", "project-a"),
        metadata={},
    )

    class TokenRunner:
        def access_token(self, account: str) -> str:
            assert account == "admin@example.com"
            return "short-lived-exact-token"

    authority = establish_archive_authority(
        settings,
        "project-a-123-archive",
        preflight_runner=object(),
        token_runner=TokenRunner(),
        client_factory=lambda token: _assert_exact_token_and_return_client(token, client),
        preflight_reader=lambda _settings, _runner: preflight,
    )

    assert isinstance(authority, ArchiveAuthority)
    assert authority.bucket is client.bucket("project-a-123-archive")


@pytest.mark.parametrize(
    ("blocker", "actual_project"),
    [
        ("services_availability_lookup_timeout", "project-a"),
        ("active_project_mismatch", "untrusted-project-secret"),
    ],
)
def test_archive_authority_reports_only_normalized_preflight_blockers(
    blocker: str, actual_project: str,
) -> None:
    settings, preflight, _client = _authority_fixture()
    preflight.ready_for_archive = False
    preflight.blockers = [blocker, blocker, "raw_token_secret", "admin_motionexp_com"]
    preflight.active_project = actual_project

    with pytest.raises(ArchiveError) as raised:
        establish_archive_authority(
            settings,
            "project-a-123-archive",
            preflight_runner=object(),
            token_runner=SimpleNamespace(access_token=lambda _account: pytest.fail("must not mint token")),
            client_factory=lambda _token: pytest.fail("must not create Storage client"),
            preflight_reader=lambda _settings, _runner: preflight,
        )

    assert blocker in str(raised.value)
    assert "raw_token_secret" not in str(raised.value)
    assert "admin_motionexp_com" not in str(raised.value)
    assert "untrusted-project-secret" not in str(raised.value)


@pytest.mark.parametrize(
    "required_services",
    [
        {
            "storage.googleapis.com": "ELIGIBLE",
            "serviceusage.googleapis.com": "ENABLED",
            "cloudresourcemanager.googleapis.com": "ENABLED",
        },
        {
            "storage.googleapis.com": "ENABLED",
            "serviceusage.googleapis.com": "UNKNOWN",
            "cloudresourcemanager.googleapis.com": "ENABLED",
        },
        {
            "storage.googleapis.com": "ENABLED",
            "serviceusage.googleapis.com": "UNAVAILABLE",
            "cloudresourcemanager.googleapis.com": "ENABLED",
        },
        {"serviceusage.googleapis.com": "ENABLED"},
    ],
)
def test_archive_authority_rejects_noncanonical_required_service_states(
    required_services: dict[str, str],
) -> None:
    settings, preflight, _client = _authority_fixture()
    preflight.required_services = required_services

    with pytest.raises(
        ArchiveError, match=r"^archive preflight is not ready: preflight_contract_mismatch$"
    ):
        establish_archive_authority(
            settings,
            "project-a-123-archive",
            preflight_runner=object(),
            token_runner=SimpleNamespace(access_token=lambda _account: pytest.fail("must not mint token")),
            client_factory=lambda _token: pytest.fail("must not create Storage client"),
            preflight_reader=lambda _settings, _runner: preflight,
        )


def test_archive_authority_accepts_eligible_nonstorage_service() -> None:
    settings, preflight, client = _authority_fixture()
    preflight.required_services["serviceusage.googleapis.com"] = "ELIGIBLE"
    preflight.required_services["cloudresourcemanager.googleapis.com"] = "ENABLED"
    client.bucket("motionexpaiweb-880586285913-hk-movie-rag-tfstate").add(
        "hk-movie-rag/archive/default.tfstate",
        _terraform_state("project-a-123-archive", "project-a"),
        metadata={},
    )

    authority = establish_archive_authority(
        settings,
        "project-a-123-archive",
        preflight_runner=object(),
        token_runner=SimpleNamespace(access_token=lambda _account: "exact-token"),
        client_factory=lambda _token: client,
        preflight_reader=lambda _settings, _runner: preflight,
    )

    assert authority.bucket is client.bucket("project-a-123-archive")


def test_archive_authority_rejects_empty_preflight_blockers_as_contract_mismatch() -> None:
    settings, preflight, _client = _authority_fixture()
    preflight.ready_for_archive = False

    with pytest.raises(
        ArchiveError, match=r"^archive preflight is not ready: preflight_contract_mismatch$"
    ):
        establish_archive_authority(
            settings,
            "project-a-123-archive",
            preflight_runner=object(),
            token_runner=SimpleNamespace(access_token=lambda _account: pytest.fail("must not mint token")),
            client_factory=lambda _token: pytest.fail("must not create Storage client"),
            preflight_reader=lambda _settings, _runner: preflight,
        )


def _assert_exact_token_and_return_client(token: str, client: FakeStorageClient) -> FakeStorageClient:
    assert token == "short-lived-exact-token"
    return client


def _terraform_state(bucket: str, project: str) -> bytes:
    return json.dumps(
        {
            "version": 4,
            "serial": 1,
            "lineage": "governed-archive-state",
            "outputs": {
                "archive_bucket_name": {"sensitive": False, "type": "string", "value": bucket},
                "archive_writer_service_account": {
                    "sensitive": False,
                    "type": "string",
                    "value": f"hk-rag-archive-writer@{project}.iam.gserviceaccount.com",
                },
            },
            "resources": [
                {
                    "mode": "managed",
                    "type": "google_storage_bucket",
                    "name": "source_archive",
                    "instances": [
                        {
                            "attributes": {
                                "name": bucket,
                                "project": project,
                                "location": "US-CENTRAL1",
                                "uniform_bucket_level_access": True,
                                "public_access_prevention": "enforced",
                                "force_destroy": False,
                                "versioning": [{"enabled": True}],
                                "soft_delete_policy": [
                                    {
                                        "effective_time": "2026-07-28T10:37:14.679Z",
                                        "retention_duration_seconds": 604800,
                                    }
                                ],
                                "lifecycle_rule": [],
                            }
                        }
                    ],
                },
                {
                    "mode": "managed",
                    "type": "google_service_account",
                    "name": "archive_writer",
                    "instances": [
                        {
                            "attributes": {
                                "account_id": "hk-rag-archive-writer",
                                "email": f"hk-rag-archive-writer@{project}.iam.gserviceaccount.com",
                                "project": project,
                            }
                        }
                    ],
                },
                {
                    "mode": "managed",
                    "type": "google_storage_bucket_iam_policy",
                    "name": "archive_writer",
                    "instances": [
                        {
                            "attributes": {
                                "bucket": f"b/{bucket}",
                                "policy_data": json.dumps(
                                    {
                                        "bindings": [
                                            {
                                                "role": "roles/storage.objectCreator",
                                                "members": [
                                                    f"serviceAccount:hk-rag-archive-writer@{project}.iam.gserviceaccount.com"
                                                ],
                                            },
                                            {
                                                "role": "roles/storage.objectViewer",
                                                "members": [
                                                    f"serviceAccount:hk-rag-archive-writer@{project}.iam.gserviceaccount.com"
                                                ],
                                            },
                                        ]
                                    }
                                ),
                            }
                        }
                    ],
                },
            ],
        },
        sort_keys=True,
    ).encode("utf-8")


@pytest.mark.parametrize("include_sensitive", [False, True])
def test_terraform_state_accepts_canonical_non_sensitive_output_variants(
    include_sensitive: bool,
) -> None:
    """Fails if canonical Terraform output without `sensitive` is not unwrapped."""
    bucket = "project-a-123-archive"
    project = "project-a"
    state = json.loads(_terraform_state(bucket, project))
    if not include_sensitive:
        for output in state["outputs"].values():
            output.pop("sensitive")

    gcs_archive._validate_terraform_state(state, bucket, project)


@pytest.mark.parametrize(
    "bucket_output",
    [
        {"sensitive": True, "type": "string", "value": "project-a-123-archive"},
        {"type": "list", "value": "project-a-123-archive"},
        {"type": "string", "value": "project-a-123-archive", "unexpected": False},
    ],
)
def test_terraform_state_rejects_noncanonical_bucket_output(
    bucket_output: dict[str, object],
) -> None:
    """Fails if sensitive, malformed, or extended Terraform output wrappers are trusted."""
    bucket = "project-a-123-archive"
    state = json.loads(_terraform_state(bucket, "project-a"))
    state["outputs"]["archive_bucket_name"] = bucket_output

    with pytest.raises(ArchiveError, match="does not authorize this bucket"):
        gcs_archive._validate_terraform_state(state, bucket, "project-a")


@pytest.mark.parametrize(
    ("output_name", "raw_output", "error"),
    [
        ("archive_bucket_name", "project-a-123-archive", "does not authorize this bucket"),
        (
            "archive_writer_service_account",
            "hk-rag-archive-writer@project-a.iam.gserviceaccount.com",
            "writer output is invalid",
        ),
    ],
)
def test_terraform_state_rejects_raw_scalar_outputs(
    output_name: str, raw_output: str, error: str
) -> None:
    """Fails if an unwrapped scalar can satisfy a protected Terraform output check."""
    bucket = "project-a-123-archive"
    state = json.loads(_terraform_state(bucket, "project-a"))
    state["outputs"][output_name] = raw_output
    state["resources"][0]["instances"][0]["attributes"]["soft_delete_policy"] = [
        {"retention_duration_seconds": 604800}
    ]

    with pytest.raises(ArchiveError, match=error):
        gcs_archive._validate_terraform_state(state, bucket, "project-a")


def test_terraform_state_accepts_canonical_provider_soft_delete_policy() -> None:
    bucket = "project-a-123-archive"
    state = json.loads(_terraform_state(bucket, "project-a"))

    gcs_archive._validate_terraform_state(state, bucket, "project-a")


@pytest.mark.parametrize(
    "policy",
    [
        [{"retention_duration_seconds": 604800}],
        [{"effective_time": "", "retention_duration_seconds": 604800}],
        [{"effective_time": "2026-07-28 10:37:14.679Z", "retention_duration_seconds": 604800}],
        [{"effective_time": "2026-07-28T10:37:14.679+00:00", "retention_duration_seconds": 604800}],
        [
            {
                "effective_time": "2026-07-28T10:37:14.679Z",
                "retention_duration_seconds": 604800,
                "unexpected": False,
            }
        ],
        [{"effective_time": "2026-07-28T10:37:14.679Z", "retention_duration_seconds": True}],
        {"effective_time": "2026-07-28T10:37:14.679Z", "retention_duration_seconds": 604800},
    ],
)
def test_terraform_state_rejects_noncanonical_provider_soft_delete_policy(
    policy: object,
) -> None:
    """Fails if a missing, non-UTC, malformed, or extended provider policy is trusted."""
    bucket = "project-a-123-archive"
    state = json.loads(_terraform_state(bucket, "project-a"))
    state["resources"][0]["instances"][0]["attributes"]["soft_delete_policy"] = policy

    with pytest.raises(ArchiveError, match="archive bucket is invalid"):
        gcs_archive._validate_terraform_state(state, bucket, "project-a")


def test_terraform_state_accepts_locked_provider_canonical_iam_bucket_reference() -> None:
    bucket = "project-a-123-archive"
    state = json.loads(_terraform_state(bucket, "project-a"))

    gcs_archive._validate_terraform_state(state, bucket, "project-a")


@pytest.mark.parametrize(
    "iam_bucket",
    [
        "project-a-123-archive",
        "b/project-a-123-archive/extra",
        "b/other-archive",
        "x/project-a-123-archive",
        "b/",
    ],
)
def test_terraform_state_rejects_noncanonical_iam_bucket_reference(iam_bucket: str) -> None:
    """Fails if raw or extended IAM bucket references can authorize the archive policy."""
    bucket = "project-a-123-archive"
    state = json.loads(_terraform_state(bucket, "project-a"))
    state["resources"][2]["instances"][0]["attributes"]["bucket"] = iam_bucket

    with pytest.raises(ArchiveError, match="IAM resource is invalid"):
        gcs_archive._validate_terraform_state(state, bucket, "project-a")


def _authority_fixture() -> tuple[SimpleNamespace, SimpleNamespace, FakeStorageClient]:
    settings = SimpleNamespace(
        repo_root=Path("workspace"),
        gcp=SimpleNamespace(
            project_id="project-a",
            required_account="admin@example.com",
            archive_bucket_template="{project_id}-{project_number}-archive",
            required_services=(
                "storage.googleapis.com",
                "serviceusage.googleapis.com",
                "cloudresourcemanager.googleapis.com",
            ),
        ),
    )
    preflight = SimpleNamespace(
        ready_for_archive=True,
        active_account="admin@example.com",
        active_project="project-a",
        project_number="123",
        project_lifecycle="ACTIVE",
        billing_enabled=True,
        caller_iam_visible=True,
        required_services={
            "storage.googleapis.com": "ENABLED",
            "serviceusage.googleapis.com": "ENABLED",
            "cloudresourcemanager.googleapis.com": "ENABLED",
        },
        blockers=[],
    )
    client = FakeStorageClient()
    return settings, preflight, client


def test_archive_authority_rejects_remote_state_generation_drift() -> None:
    settings, preflight, client = _authority_fixture()
    state = client.bucket("motionexpaiweb-880586285913-hk-movie-rag-tfstate").add(
        "hk-movie-rag/archive/default.tfstate",
        _terraform_state("project-a-123-archive", "project-a"),
        metadata={},
    )

    authority = establish_archive_authority(
        settings,
        "project-a-123-archive",
        preflight_runner=object(),
        token_runner=SimpleNamespace(access_token=lambda _account: "exact-token"),
        client_factory=lambda _token: client,
        preflight_reader=lambda _settings, _runner: preflight,
    )
    state.generation = 8

    with pytest.raises(ArchiveError, match="authority state changed"):
        assert_authority_state_current(authority)


def test_archive_authority_boundary_rejects_live_bucket_drift() -> None:
    settings, preflight, client = _authority_fixture()
    client.bucket("motionexpaiweb-880586285913-hk-movie-rag-tfstate").add(
        "hk-movie-rag/archive/default.tfstate",
        _terraform_state("project-a-123-archive", "project-a"),
        metadata={},
    )
    authority = establish_archive_authority(
        settings,
        "project-a-123-archive",
        preflight_runner=object(),
        token_runner=SimpleNamespace(access_token=lambda _account: "exact-token"),
        client_factory=lambda _token: client,
        preflight_reader=lambda _settings, _runner: preflight,
    )
    authority.bucket.metageneration = 2

    with pytest.raises(ArchiveError, match="bucket authority changed"):
        assert_archive_authority_current(authority)


def test_archive_authority_rejects_client_project_or_bucket_project_number_mismatch() -> None:
    settings, preflight, client = _authority_fixture()
    client.bucket("motionexpaiweb-880586285913-hk-movie-rag-tfstate").add(
        "hk-movie-rag/archive/default.tfstate",
        _terraform_state("project-a-123-archive", "project-a"),
        metadata={},
    )
    client.project = "other-project"

    with pytest.raises(ArchiveError, match="client project is invalid"):
        establish_archive_authority(
            settings,
            "project-a-123-archive",
            preflight_runner=object(),
            token_runner=SimpleNamespace(access_token=lambda _account: "exact-token"),
            client_factory=lambda _token: client,
            preflight_reader=lambda _settings, _runner: preflight,
        )

    client.project = "project-a"
    client.bucket("project-a-123-archive").project_number = 999
    with pytest.raises(ArchiveError, match="bucket authority is invalid"):
        establish_archive_authority(
            settings,
            "project-a-123-archive",
            preflight_runner=object(),
            token_runner=SimpleNamespace(access_token=lambda _account: "exact-token"),
            client_factory=lambda _token: client,
            preflight_reader=lambda _settings, _runner: preflight,
        )


def test_policy_binding_list_is_normalized_and_mapping_is_rejected() -> None:
    assert gcs_archive._normalize_policy_bindings(
        [{"role": "roles/storage.objectViewer", "members": {"serviceAccount:writer@example.com"}}]
    ) == [{"role": "roles/storage.objectViewer", "members": ["serviceAccount:writer@example.com"]}]
    assert gcs_archive._normalize_policy_bindings(
        {"roles/storage.objectViewer": {"serviceAccount:writer@example.com"}}
    ) is None


@pytest.mark.parametrize(
    "unexpected_binding",
    [
        {"role": "roles/storage.legacyBucketReader", "members": ["projectViewer:project-a"]},
        {
            "role": "roles/storage.objectCreator",
            "members": [
                "serviceAccount:hk-rag-archive-writer@project-a.iam.gserviceaccount.com",
                "user:unexpected@example.com",
            ],
        },
        {
            "role": "roles/storage.objectViewer",
            "members": ["serviceAccount:hk-rag-archive-writer@project-a.iam.gserviceaccount.com"],
        },
        {
            "role": "roles/storage.objectViewer",
            "members": ["serviceAccount:hk-rag-archive-writer@project-a.iam.gserviceaccount.com"],
            "condition": {"title": "unexpected", "expression": "true"},
        },
    ],
)
def test_archive_authority_rejects_any_extra_live_iam_binding(
    unexpected_binding: dict[str, object],
) -> None:
    settings, preflight, client = _authority_fixture()
    client.bucket("motionexpaiweb-880586285913-hk-movie-rag-tfstate").add(
        "hk-movie-rag/archive/default.tfstate",
        _terraform_state("project-a-123-archive", "project-a"),
        metadata={},
    )
    archive_bucket = client.bucket("project-a-123-archive")
    archive_bucket.iam_bindings.append(unexpected_binding)

    with pytest.raises(ArchiveError, match="IAM authority is invalid"):
        establish_archive_authority(
            settings,
            "project-a-123-archive",
            preflight_runner=object(),
            token_runner=SimpleNamespace(access_token=lambda _account: "exact-token"),
            client_factory=lambda _token: client,
            preflight_reader=lambda _settings, _runner: preflight,
        )


def test_archive_authority_rejects_wrong_managed_resource_schema() -> None:
    settings, preflight, client = _authority_fixture()
    state = json.loads(_terraform_state("project-a-123-archive", "project-a"))
    state["resources"][0]["instances"][0]["attributes"]["force_destroy"] = True
    client.bucket("motionexpaiweb-880586285913-hk-movie-rag-tfstate").add(
        "hk-movie-rag/archive/default.tfstate",
        json.dumps(state).encode("utf-8"),
        metadata={},
    )

    with pytest.raises(ArchiveError, match="archive bucket is invalid"):
        establish_archive_authority(
            settings,
            "project-a-123-archive",
            preflight_runner=object(),
            token_runner=SimpleNamespace(access_token=lambda _account: "exact-token"),
            client_factory=lambda _token: client,
            preflight_reader=lambda _settings, _runner: preflight,
        )


def test_archive_authority_hides_exact_token_failure() -> None:
    settings, preflight, _client = _authority_fixture()

    with pytest.raises(ArchiveError, match="account token is unavailable") as raised:
        establish_archive_authority(
            settings,
            "project-a-123-archive",
            preflight_runner=object(),
            token_runner=SimpleNamespace(
                access_token=lambda _account: (_ for _ in ()).throw(RuntimeError("token-secret"))
            ),
            client_factory=lambda _token: pytest.fail("client must not be created"),
            preflight_reader=lambda _settings, _runner: preflight,
        )

    assert "token-secret" not in str(raised.value)


def test_archive_cli_reports_config_failure_as_json(
    monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]
) -> None:
    from hk_movie_rag.config import ConfigError

    monkeypatch.setattr(cli.Settings, "load", lambda _root: (_ for _ in ()).throw(ConfigError("x")))
    monkeypatch.setattr(
        sys,
        "argv",
        ["hk-movie-rag", "archive-upload", "--inventory", "inventory.jsonl", "--bucket", "x"],
    )

    with pytest.raises(SystemExit) as raised:
        cli.main()

    assert raised.value.code == 1
    assert json.loads(capsys.readouterr().out) == {
        "error": "archive_operation_failed",
        "ok": False,
        "reason": "archive configuration is invalid",
    }


def test_archive_verify_cli_runs_after_an_inventory_root_is_detached(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    capsys: pytest.CaptureFixture[str],
) -> None:
    """Breaks if the post-cleanup CLI selects strict source-building settings."""
    config_dir = tmp_path / "config"
    inputs = tmp_path / "inputs"
    poster_root = inputs / "posters"
    inventory_dir = tmp_path / "artifacts" / "inventory" / "v1.2"
    config_dir.mkdir()
    poster_root.mkdir(parents=True)
    inventory_dir.mkdir(parents=True)
    for name in (
        "manifest.json",
        "validation.json",
        "movies.csv",
        "movies.xlsx",
        "tiers.xlsx",
        "issues.xlsx",
    ):
        (inputs / name).touch()
    inventory_path = inventory_dir / "source_inventory.jsonl"
    inventory_path.write_text("{}\n", encoding="utf-8")
    (config_dir / "local.yaml").write_text(
        """release_version: v1.2
source_manifest: inputs/manifest.json
source_validation: inputs/validation.json
movies_csv: inputs/movies.csv
movies_xlsx: inputs/movies.xlsx
tiers_xlsx: inputs/tiers.xlsx
issue_ledger_xlsx: inputs/issues.xlsx
inventory_roots: [detached-posters]
canonical_poster_roots:
  - path: inputs/posters
    recursive: false
expected:
  release_movies: 1
  quarantine_movies: 0
  tier_counts: {S: 1}
  pilot_movies: 1
  audit_rows: 1
  poster_states: {machine_passed: 1}
""",
        encoding="utf-8",
    )
    (config_dir / "gcp.yaml").write_text(
        """project_id: test-project
region: us-central1
required_account: test@example.com
archive_bucket_template: test-{project_id}
archive_prefix: source/v1.2
required_services: [storage.googleapis.com]
""",
        encoding="utf-8",
    )
    report = ArchiveVerificationReport(
        bucket="archive-bucket",
        prefix="source/v1.2",
        object_count=1,
        total_bytes=8,
        inventory_digest="inventory-digest",
        objects=(),
        verification_timestamp="2026-07-30T00:00:00+00:00",
        verified=True,
    )
    authority = SimpleNamespace(bucket=FakeBucket("archive-bucket"))
    monkeypatch.setattr(cli, "establish_archive_authority", lambda *_args, **_kwargs: authority)
    monkeypatch.setattr(cli, "assert_archive_authority_current", lambda _authority: None)
    monkeypatch.setattr(cli, "assert_authority_state_current", lambda _authority: None)
    monkeypatch.setattr(cli, "verify_archive", lambda *_args, **_kwargs: report)
    monkeypatch.chdir(tmp_path)
    monkeypatch.setattr(
        sys,
        "argv",
        [
            "hk-movie-rag",
            "archive-verify",
            "--inventory",
            "artifacts/inventory/v1.2/source_inventory.jsonl",
            "--bucket",
            "archive-bucket",
            "--output",
            "artifacts/archive/v1.2/post-cleanup-verification.json",
        ],
    )

    cli.main()

    assert json.loads(capsys.readouterr().out)["verified"] is True
    assert (
        tmp_path
        / "artifacts"
        / "archive"
        / "v1.2"
        / "post-cleanup-verification.json"
    ).exists()


def test_upload_rejects_inventory_parent_identity_swap(
    tmp_path: Path, fake_bucket: FakeBucket, monkeypatch: pytest.MonkeyPatch
) -> None:
    root = tmp_path / "root"
    parent = root / "input"
    parent.mkdir(parents=True)
    source = root / "source.txt"
    source.write_bytes(b"expected")
    inventory = parent / "inventory.jsonl"
    inventory.write_text(
        json.dumps(
            {
                "relative_path": "source.txt",
                "size_bytes": 8,
                "sha256": hashlib.sha256(b"expected").hexdigest(),
                "classification": "formal_release_source",
            }
        )
        + "\n",
        encoding="utf-8",
    )
    inventory_content = inventory.read_bytes()
    real_open = gcs_archive._open_protected_archive_descriptor

    def swap_inventory_parent(path: Path) -> int:
        if path == inventory:
            parent.rename(root / "input-original")
            parent.mkdir()
            (parent / "inventory.jsonl").write_bytes(inventory_content)
        return real_open(path)

    monkeypatch.setattr(gcs_archive, "_open_protected_archive_descriptor", swap_inventory_parent)

    with pytest.raises(ArchiveError, match="inventory changed during protected open"):
        upload_archive(
            fake_bucket,
            repo_root=root,
            inventory_path=Path("input/inventory.jsonl"),
            release_version="v1.2",
            prefix="source/v1.2",
        )


def test_upload_rejects_source_parent_identity_swap(
    tmp_path: Path, fake_bucket: FakeBucket, monkeypatch: pytest.MonkeyPatch
) -> None:
    root = tmp_path / "root"
    sources = root / "sources"
    sources.mkdir(parents=True)
    source = sources / "a"
    source.write_bytes(b"expected")
    inventory = root / "inventory.jsonl"
    inventory.write_text(
        json.dumps(
            {
                "relative_path": "sources/a",
                "size_bytes": 8,
                "sha256": hashlib.sha256(b"expected").hexdigest(),
                "classification": "formal_release_source",
            }
        )
        + "\n",
        encoding="utf-8",
    )
    real_open = gcs_archive._open_protected_archive_descriptor

    def swap_source_parent(path: Path) -> int:
        if path == source:
            sources.rename(root / "sources-original")
            sources.mkdir()
            (sources / "a").write_bytes(b"expected")
        return real_open(path)

    monkeypatch.setattr(gcs_archive, "_open_protected_archive_descriptor", swap_source_parent)

    with pytest.raises(ArchiveError, match="source sources/a changed during protected open"):
        upload_archive(
            fake_bucket,
            repo_root=root,
            inventory_path=Path("inventory.jsonl"),
            release_version="v1.2",
            prefix="source/v1.2",
        )


def test_authority_guard_stops_later_creates_after_state_drift(
    tmp_path: Path, fake_bucket: FakeBucket
) -> None:
    first = b"first"
    second = b"second"
    (tmp_path / "first.txt").write_bytes(first)
    (tmp_path / "second.txt").write_bytes(second)
    inventory = tmp_path / "inventory.jsonl"
    inventory.write_text(
        "\n".join(
            json.dumps(
                {
                    "relative_path": name,
                    "size_bytes": len(content),
                    "sha256": hashlib.sha256(content).hexdigest(),
                    "classification": "formal_release_source",
                }
            )
            for name, content in (("first.txt", first), ("second.txt", second))
        )
        + "\n",
        encoding="utf-8",
    )
    drifted = False

    def guard() -> None:
        if drifted:
            raise ArchiveError("Terraform authority state changed")

    first_blob = fake_bucket.blob("source/v1.2/files/first.txt")

    def drift_after_first_create() -> None:
        nonlocal drifted
        drifted = True

    first_blob.before_upload_read = drift_after_first_create

    with pytest.raises(ArchiveError, match="authority state changed"):
        upload_archive(
            fake_bucket,
            repo_root=tmp_path,
            inventory_path=inventory,
            release_version="v1.2",
            prefix="source/v1.2",
            authority_guard=guard,
        )

    assert fake_bucket.get_blob("source/v1.2/files/first.txt") is not None
    assert fake_bucket.get_blob("source/v1.2/files/second.txt") is None
    assert fake_bucket.get_blob("source/v1.2/manifests/source_inventory.jsonl") is None


def test_create_bucket_hides_explicit_client_failure(monkeypatch: pytest.MonkeyPatch) -> None:
    from google.cloud import storage

    monkeypatch.setattr(
        storage,
        "Client",
        lambda *, credentials: (_ for _ in ()).throw(RuntimeError("credential secret")),
    )

    with pytest.raises(ArchiveError, match="storage client is unavailable") as raised:
        gcs_archive.create_bucket("archive-bucket", "exact-account-token")

    assert "secret" not in str(raised.value)
