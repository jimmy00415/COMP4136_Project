from __future__ import annotations

import hashlib
import io
import json
import sys
from collections.abc import Iterator
from contextlib import contextmanager
from dataclasses import dataclass, field, replace

import pytest

from hk_movie_rag import poster_fleet
from hk_movie_rag.poster_authority import (
    POSTER_SERVING_AUTHORITY_V2_SHA256,
    POSTER_SERVING_AUTHORITY_V3_SHA256,
    load_poster_serving_authority,
)
from hk_movie_rag.poster_fleet import PosterObject, verify_poster_fleet
from hk_movie_rag.rag_bundle import RagReleaseContract, RagReleaseCounts
from hk_movie_rag.rag_db import RagRepository

_RELEASE = "v1.2-demo"
_BUCKET = "private-bucket"
_R2_RELEASE = "v1.2-demo-r2"
_R3_RELEASE = "v1.2-demo-r3"


def _r2_contract() -> RagReleaseContract:
    return RagReleaseContract(
        schema_version="1.2",
        rag_release_id=_R2_RELEASE,
        parent_release_manifest_sha256=(
            "e9842db33208e7146d78d86689b13f81fffcf41288d79d84e599b93050445e2c"
        ),
        manifest_sha256="a" * 64,
        bundle_sha256="b" * 64,
        derived_inventory_sha256=(
            "b754727129bc044b6c9da1e6db22067c8279c5e7f8dd41d0350e365376538154"
        ),
        counts=RagReleaseCounts(
            movies=4658,
            facet_count=4658,
            tier_s_count=50,
            tier_a_count=313,
            tier_b_count=4295,
            pilot_count=24,
            poster_rows=4658,
            primary_poster_rows=4658,
            approved_poster_objects=4545,
            unavailable_poster_rows=113,
            derived_poster_bytes=1,
            metadata_passages=4658,
            documents=3,
            pdf_passages=12,
        ),
        embedding_model="gemini-embedding-2",
        embedding_dimension=768,
        generation_model="gemini-3.5-flash-lite",
        text_extraction_profile="cjk-layout-v1",
        document_embedding_profile="vertex-title-text-v1",
        relevance_policy_sha256="c" * 64,
        poster_authority_sha256=POSTER_SERVING_AUTHORITY_V2_SHA256,
        access_mode="restricted_demo",
    )


def _r3_contract() -> RagReleaseContract:
    parent = _r2_contract()
    return replace(
        parent,
        schema_version="1.3",
        rag_release_id=_R3_RELEASE,
        derived_inventory_sha256=(
            "3e3cedee507c3017821de1081bc70bbb01d0c73f8683ab484a464d1fc1f7c1db"
        ),
        counts=replace(
            parent.counts,
            movies=4659,
            facet_count=4659,
            tier_s_count=51,
            poster_rows=4659,
            primary_poster_rows=4659,
            approved_poster_objects=4546,
            metadata_passages=4659,
            documents=5,
            pdf_passages=21,
        ),
        poster_authority_sha256=POSTER_SERVING_AUTHORITY_V3_SHA256,
    )


@dataclass(frozen=True)
class _VerifiedBundle:
    contract: RagReleaseContract

    @property
    def rag_release_id(self) -> str:
        return self.contract.rag_release_id


def _approved_row(index: int) -> tuple[dict[str, object], bytes]:
    movie_id = f"A{index:05d}"
    body = b"RIFF" + index.to_bytes(4, "little") + b"WEBPposter"
    return (
        {
            "movie_id": movie_id,
            "derived_object_uri": f"assets/posters/derived/{movie_id}.webp",
            "derived_content_sha256": hashlib.sha256(body).hexdigest(),
            "derived_byte_length": len(body),
            "derived_mime_type": "image/webp",
            "quality_status": "machine_passed",
        },
        body,
    )


def _unavailable_row(index: int) -> dict[str, object]:
    return {
        "movie_id": f"U{index:05d}",
        "derived_object_uri": "",
        "derived_content_sha256": "",
        "derived_byte_length": None,
        "derived_mime_type": "",
        "quality_status": "missing",
    }


@dataclass
class _Repository:
    rows: list[dict[str, object]]
    reads: list[str] = field(default_factory=list)

    def poster_fleet_rows(self, release_id: str) -> list[dict[str, object]]:
        self.reads.append(release_id)
        return self.rows


class _TrackedStream(io.BytesIO):
    def __init__(self, body: bytes, closed_names: list[str], name: str) -> None:
        super().__init__(body)
        self._closed_names = closed_names
        self._name = name

    def close(self) -> None:
        if not self.closed:
            self._closed_names.append(self._name)
        super().close()


class _ReadTrackedStream(_TrackedStream):
    def __init__(self, body: bytes, closed_names: list[str], name: str) -> None:
        super().__init__(body, closed_names, name)
        self.bytes_returned = 0
        self.read_calls = 0

    def read(self, size: int = -1) -> bytes:
        self.read_calls += 1
        chunk = super().read(size)
        self.bytes_returned += len(chunk)
        return chunk


@dataclass
class _Storage:
    bodies: dict[str, bytes]
    content_types: dict[str, str] = field(default_factory=dict)
    declared_sizes: dict[str, int] = field(default_factory=dict)
    requested: list[tuple[str, str]] = field(default_factory=list)
    closed: list[str] = field(default_factory=list)

    def open_object(self, bucket: str, object_name: str) -> PosterObject | None:
        self.requested.append((bucket, object_name))
        body = self.bodies.get(object_name)
        if body is None:
            return None
        return PosterObject(
            stream=_TrackedStream(body, self.closed, object_name),
            size=self.declared_sizes.get(object_name, len(body)),
            mime_type=self.content_types.get(object_name, "image/webp"),
        )


@dataclass
class _SingleObjectStorage:
    value: PosterObject

    def open_object(self, bucket: str, object_name: str) -> PosterObject:
        del bucket, object_name
        return self.value


class _Context:
    def __init__(self, value: object) -> None:
        self.value = value

    def __enter__(self) -> object:
        return self.value

    def __exit__(self, *_args: object) -> None:
        return None


@dataclass
class _Cursor:
    rows: list[tuple[object, ...]]
    calls: list[tuple[str, tuple[object, ...]]] = field(default_factory=list)

    def execute(self, sql: str, params: tuple[object, ...]) -> None:
        self.calls.append((sql, params))

    def fetchall(self) -> list[tuple[object, ...]]:
        return self.rows


@dataclass
class _Connection:
    cursor_value: _Cursor

    def transaction(self) -> _Context:
        return _Context(None)

    def cursor(self) -> _Context:
        return _Context(self.cursor_value)


@pytest.fixture(scope="module")
def fleet_fixture() -> tuple[list[dict[str, object]], dict[str, bytes]]:
    rows: list[dict[str, object]] = []
    bodies: dict[str, bytes] = {}
    for index in range(4_545):
        row, body = _approved_row(index)
        rows.append(row)
        bodies[str(row["derived_object_uri"])] = body
    rows.extend(_unavailable_row(index) for index in range(113))
    return rows, bodies


def test_fleet_verifies_all_approved_bytes_and_unavailable_states(
    fleet_fixture: tuple[list[dict[str, object]], dict[str, bytes]],
) -> None:
    """Breaks if the verifier samples rows/bytes or skips unavailable canonical keys."""
    rows, bodies = fleet_fixture
    repository = _Repository(rows)
    storage = _Storage(bodies)

    report = verify_poster_fleet(repository, storage, _RELEASE, _BUCKET, workers=4)

    assert (report.primary_rows, report.approved, report.unavailable) == (4658, 4545, 113)
    assert report.verified == 4545
    assert report.failures == ()
    assert report.passed is True
    assert repository.reads == [_RELEASE]
    assert len(storage.requested) == 4658
    assert all(bucket == _BUCKET for bucket, _key in storage.requested)
    assert sorted(storage.closed) == sorted(bodies)


def test_r2_fleet_consumes_verified_bundle_and_dataset_authority_without_child_literals(
    fleet_fixture: tuple[list[dict[str, object]], dict[str, bytes]],
) -> None:
    """Breaks if R2 fleet selection trusts a release string or old module counts."""
    rows, bodies = fleet_fixture
    contract = _r2_contract()
    bundle = _VerifiedBundle(contract)
    authority = load_poster_serving_authority(
        _R2_RELEASE,
        POSTER_SERVING_AUTHORITY_V2_SHA256,
        contract=contract,
    )
    repository = _Repository(rows)

    report = verify_poster_fleet(
        repository,
        _Storage(bodies),
        bundle,
        _BUCKET,
        workers=4,
        authority=authority,
    )

    assert report.passed is True
    assert report.release_id == _R2_RELEASE
    assert repository.reads == [_R2_RELEASE]


def test_r3_fleet_consumes_release_scoped_authority_and_contract_counts(
    fleet_fixture: tuple[list[dict[str, object]], dict[str, bytes]],
) -> None:
    """Breaks if fleet verification admits only the R2 authority shape."""
    parent_rows, parent_bodies = fleet_fixture
    supplemental_row, supplemental_body = _approved_row(4545)
    rows = [*parent_rows, supplemental_row]
    bodies = {
        **parent_bodies,
        str(supplemental_row["derived_object_uri"]): supplemental_body,
    }
    contract = _r3_contract()
    authority = load_poster_serving_authority(
        _R3_RELEASE,
        POSTER_SERVING_AUTHORITY_V3_SHA256,
        contract=contract,
    )
    repository = _Repository(rows)

    report = verify_poster_fleet(
        repository,
        _Storage(bodies),
        _VerifiedBundle(contract),
        _BUCKET,
        workers=4,
        authority=authority,
    )

    assert report.passed is True
    assert (report.primary_rows, report.approved, report.unavailable) == (
        4659,
        4546,
        113,
    )
    assert report.verified == 4546
    assert repository.reads == [_R3_RELEASE]


def test_r2_fleet_rejects_missing_authority_before_database_or_storage_reads() -> None:
    """Breaks if a child manifest can inherit poster serving permission by fallback."""
    repository = _Repository([])
    storage = _Storage({})

    report = verify_poster_fleet(
        repository,
        storage,
        _VerifiedBundle(_r2_contract()),
        _BUCKET,
    )

    assert report.release_id == _R2_RELEASE
    assert [(failure.movie_id, failure.reason) for failure in report.failures] == [
        ("[fleet]", "authority_invalid")
    ]
    assert repository.reads == []
    assert storage.requested == []


def test_r2_fleet_rejects_wrapper_release_that_differs_from_verified_contract() -> None:
    """Breaks if one child can query another release under an unrelated contract."""
    contract = _r2_contract()
    authority = load_poster_serving_authority(
        _R2_RELEASE,
        POSTER_SERVING_AUTHORITY_V2_SHA256,
        contract=contract,
    )

    @dataclass(frozen=True)
    class MismatchedBundle:
        rag_release_id: str
        contract: RagReleaseContract

    repository = _Repository([])
    storage = _Storage({})

    report = verify_poster_fleet(
        repository,
        storage,
        MismatchedBundle("v1.2-demo-other", contract),
        _BUCKET,
        authority=authority,
    )

    assert report.release_id == "v1.2-demo-other"
    assert [(failure.movie_id, failure.reason) for failure in report.failures] == [
        ("[fleet]", "authority_invalid")
    ]
    assert repository.reads == []
    assert storage.requested == []


def test_repository_reads_release_primary_poster_rows_in_one_scoped_query() -> None:
    """Breaks if the fleet DB snapshot is sampled, cross-release, or split across queries."""
    cursor = _Cursor(
        [
            (
                "1978_ZQ_001",
                "assets/posters/derived/1978_ZQ_001.webp",
                "a" * 64,
                100,
                "image/webp",
                "machine_passed",
            ),
            ("missing_movie", "", "", None, "", "missing"),
        ]
    )

    rows = RagRepository(_Connection(cursor)).poster_fleet_rows(_RELEASE)

    assert rows == [
        {
            "movie_id": "1978_ZQ_001",
            "derived_object_uri": "assets/posters/derived/1978_ZQ_001.webp",
            "derived_content_sha256": "a" * 64,
            "derived_byte_length": 100,
            "derived_mime_type": "image/webp",
            "quality_status": "machine_passed",
        },
        {
            "movie_id": "missing_movie",
            "derived_object_uri": "",
            "derived_content_sha256": "",
            "derived_byte_length": None,
            "derived_mime_type": "",
            "quality_status": "missing",
        },
    ]
    assert len(cursor.calls) == 1
    sql, params = cursor.calls[0]
    assert params == (_RELEASE,)
    assert "WHERE release_id = %s AND asset_type = 'poster' AND is_primary" in sql
    assert "ORDER BY movie_id" in sql


@pytest.mark.parametrize("mismatch", ["hash", "size", "mime"])
def test_fleet_fails_on_one_hash_size_or_mime_mismatch(
    fleet_fixture: tuple[list[dict[str, object]], dict[str, bytes]], mismatch: str
) -> None:
    """Breaks if one corrupt object's streamed identity can pass the fleet gate."""
    source_rows, source_bodies = fleet_fixture
    rows = [dict(row) for row in source_rows]
    bodies = dict(source_bodies)
    key = str(rows[0]["derived_object_uri"])
    storage = _Storage(bodies)
    if mismatch == "hash":
        bodies[key] = bodies[key][:-1] + bytes([bodies[key][-1] ^ 1])
    elif mismatch == "size":
        storage.declared_sizes[key] = len(bodies[key]) + 1
    else:
        storage.content_types[key] = "image/jpeg"

    report = verify_poster_fleet(_Repository(rows), storage, _RELEASE, _BUCKET, workers=2)

    assert report.passed is False
    assert report.verified == 4544
    assert len(report.failures) == 1
    assert report.failures[0].movie_id == "A00000"
    assert report.failures[0].reason in {
        "content_sha256_mismatch",
        "object_size_mismatch",
        "object_mime_mismatch",
    }
    assert key in storage.closed


@pytest.mark.parametrize(
    ("remote_size_delta", "remote_mime", "expected_reason"),
    [
        (1, "image/webp", "object_size_mismatch"),
        (0, "image/jpeg", "object_mime_mismatch"),
    ],
)
def test_fleet_rejects_remote_metadata_before_reading_object_bytes(
    remote_size_delta: int,
    remote_mime: str,
    expected_reason: str,
) -> None:
    """Breaks if known-bad provider metadata still permits an object-body read."""
    row, body = _approved_row(0)
    closed: list[str] = []
    stream = _ReadTrackedStream(body, closed, "poster")
    storage = _SingleObjectStorage(
        PosterObject(stream, len(body) + remote_size_delta, remote_mime)
    )

    verified, failure = poster_fleet._verify_row(storage, _BUCKET, row)

    assert verified is False
    assert failure is not None and failure.reason == expected_reason
    assert stream.read_calls == 0
    assert stream.bytes_returned == 0
    assert stream.closed is True


def test_fleet_aborts_stream_at_first_byte_beyond_declared_remote_size() -> None:
    """Breaks if a corrupt oversized object is consumed after its size violation is known."""
    row, expected_body = _approved_row(0)
    corrupt_body = expected_body + (b"x" * (2 * 64 * 1024))
    closed: list[str] = []
    stream = _ReadTrackedStream(corrupt_body, closed, "poster")
    storage = _SingleObjectStorage(
        PosterObject(stream, len(expected_body), "image/webp")
    )

    verified, failure = poster_fleet._verify_row(storage, _BUCKET, row)

    assert verified is False
    assert failure is not None and failure.reason == "object_size_mismatch"
    assert stream.bytes_returned == len(expected_body) + 1
    assert stream.bytes_returned < len(corrupt_body)
    assert stream.closed is True


def test_fleet_requires_literal_population_before_any_storage_read() -> None:
    """Breaks if partial database populations are mistaken for a verified fleet."""
    row, body = _approved_row(0)
    storage = _Storage({str(row["derived_object_uri"]): body})

    report = verify_poster_fleet(_Repository([row]), storage, _RELEASE, _BUCKET)

    assert report.passed is False
    assert report.primary_rows == 1
    assert report.verified == 0
    assert [(item.movie_id, item.reason) for item in report.failures] == [
        ("[fleet]", "primary_row_count_mismatch")
    ]
    assert storage.requested == []


def test_fleet_unavailable_requires_no_canonical_object_and_sorts_redacted_failures(
    fleet_fixture: tuple[list[dict[str, object]], dict[str, bytes]],
) -> None:
    """Breaks if unavailable objects exist or concurrent failures leak provider details/order."""
    source_rows, source_bodies = fleet_fixture
    rows = [dict(row) for row in source_rows]
    bodies = dict(source_bodies)
    unavailable_key = "assets/posters/derived/U00001.webp"
    bodies[unavailable_key] = b"must-not-exist"
    bad_key = str(rows[1]["derived_object_uri"])
    bodies[bad_key] = bodies[bad_key][:-1] + bytes([bodies[bad_key][-1] ^ 1])

    report = verify_poster_fleet(_Repository(rows), _Storage(bodies), _RELEASE, _BUCKET, workers=4)

    assert [(item.movie_id, item.reason) for item in report.failures] == [
        ("A00001", "content_sha256_mismatch"),
        ("U00001", "unavailable_object_exists"),
    ]
    encoded = json.dumps(report.to_dict(), sort_keys=True)
    assert _BUCKET not in encoded
    assert "gs://" not in encoded
    assert "must-not-exist" not in encoded


def test_fleet_rejects_worker_counts_outside_one_to_sixteen() -> None:
    """Breaks if caller input can exceed the bounded fleet concurrency."""
    for workers in (0, 17, True):
        with pytest.raises(ValueError, match="workers must be between 1 and 16"):
            verify_poster_fleet(_Repository([]), _Storage({}), _RELEASE, _BUCKET, workers=workers)


@pytest.mark.parametrize(
    "unsafe_release_id",
    [
        "https://user:secret@example.invalid/release",
        "postgresql://db_user:db_password@db.internal/database",
        "projects/demo/secrets/demo-key/versions/latest",
    ],
)
def test_fleet_never_reads_or_emits_an_untrusted_release_id(unsafe_release_id: str) -> None:
    """Breaks if caller-controlled URL, DB, or secret text reaches a fleet report."""
    repository = _Repository([])

    report = verify_poster_fleet(repository, _Storage({}), unsafe_release_id, _BUCKET)

    encoded = json.dumps(report.to_dict(), sort_keys=True)
    assert report.release_id == _RELEASE
    assert [(item.movie_id, item.reason) for item in report.failures] == [
        ("[fleet]", "release_id_invalid")
    ]
    assert unsafe_release_id not in encoded
    assert "secret" not in encoded.lower()
    assert "password" not in encoded.lower()
    assert repository.reads == []


def test_verify_poster_fleet_cli_emits_one_redacted_report_and_fails_nonzero(
    monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]
) -> None:
    """Breaks if the job CLI prints secrets/details or returns success for a failed report."""
    from hk_movie_rag import cli

    row, body = _approved_row(0)

    @contextmanager
    def fake_repository() -> Iterator[_Repository]:
        yield _Repository([row])

    @contextmanager
    def fake_verified_source(_manifest: str, *, project_id: str) -> Iterator[_VerifiedBundle]:
        assert project_id == "motionexpaiweb"
        yield _VerifiedBundle(_r2_contract())

    monkeypatch.setattr(cli, "_repository_from_env", fake_repository)
    monkeypatch.setattr(cli, "GcsPosterFleetStorage", lambda: _Storage({str(row["derived_object_uri"]): body}))
    monkeypatch.setattr(cli, "verified_bundle_source", fake_verified_source)
    monkeypatch.setattr(
        sys,
        "argv",
        [
            "hk-movie-rag",
            "verify-poster-fleet",
            "--manifest",
            "verified-r2-manifest.json",
            "--bucket",
            _BUCKET,
            "--workers",
            "4",
        ],
    )

    with pytest.raises(SystemExit) as raised:
        cli.main()

    captured = capsys.readouterr()
    assert raised.value.code == 1
    assert captured.err == ""
    assert len(captured.out.splitlines()) == 1
    report = json.loads(captured.out)
    assert report["release_id"] == _R2_RELEASE
    assert report["passed"] is False
    assert report["failures"] == [
        {"movie_id": "[fleet]", "reason": "primary_row_count_mismatch"}
    ]
    assert _BUCKET not in captured.out


@pytest.mark.parametrize(
    "arguments",
    [
        ["verify-poster-fleet"],
        [
            "verify-poster-fleet",
            "--manifest",
            "manifest.json",
            "--bucket",
            _BUCKET,
            "--workers",
            "not-an-int",
        ],
        [
            "verify-poster-fleet",
            "--manifest",
            "manifest.json",
            "--bucket",
            _BUCKET,
            "--unknown",
            "token-value",
        ],
        [
            "verify-poster-fleet",
            "--release-id",
            "postgresql://user:db-password@db.internal/database",
            "--bucket",
            _BUCKET,
        ],
    ],
)
def test_verify_poster_fleet_cli_parse_failures_are_one_redacted_json_report(
    arguments: list[str],
    monkeypatch: pytest.MonkeyPatch,
    capsys: pytest.CaptureFixture[str],
) -> None:
    """Breaks if argparse usage or caller-controlled values leak from the Job CLI."""
    from hk_movie_rag import cli

    monkeypatch.setattr(sys, "argv", ["hk-movie-rag", *arguments])

    with pytest.raises(SystemExit) as raised:
        cli.main()

    captured = capsys.readouterr()
    assert raised.value.code != 0
    assert captured.err == ""
    assert len(captured.out.splitlines()) == 1
    report = json.loads(captured.out)
    assert report == {
        "approved": 0,
        "failures": [{"movie_id": "[fleet]", "reason": "cli_arguments_invalid"}],
        "passed": False,
        "primary_rows": 0,
        "release_id": _RELEASE,
        "unavailable": 0,
        "verified": 0,
    }
    assert "password" not in captured.out.lower()
    assert "token-value" not in captured.out
