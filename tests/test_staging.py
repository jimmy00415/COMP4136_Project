from __future__ import annotations

import hashlib
import json
import subprocess
import sys
from pathlib import Path
from types import SimpleNamespace

import pyarrow as pa
import pyarrow.parquet as pq
import pytest

from hk_movie_rag import cli, staging
from hk_movie_rag.cli import main
from hk_movie_rag.staging import (
    StagingError,
    document_identity,
    validate_analysis_submission,
    validate_poster_submission,
)

RELEASE_ID = "1970_GSQ_001"
POSTER_BYTES = (Path(__file__).parent / "fixtures" / "images" / "valid.jpg").read_bytes()


def test_analysis_rejects_unknown_movie_id(tmp_path: Path) -> None:
    """Breaks if a non-Release key can enter the document staging contract."""
    path = _write_analysis(tmp_path, movie_id="2099_UNKNOWN_001")

    with pytest.raises(StagingError, match="not in formal Release"):
        validate_analysis_submission(path, {RELEASE_ID})


def test_poster_requires_source_rights_and_expected_hash(tmp_path: Path) -> None:
    """Breaks if a sidecar can omit source, rights, or byte-integrity evidence."""
    path = _write_poster_submission(tmp_path, metadata={"movie_id": RELEASE_ID})

    with pytest.raises(
        StagingError,
        match="source_url.*rights_status.*expected_sha256",
    ):
        validate_poster_submission(path, {RELEASE_ID})


def test_unchanged_analysis_hash_is_idempotent() -> None:
    """Breaks if retrying identical content creates a different document identity."""
    first = document_identity(RELEASE_ID, "analysis-1", b"same")
    second = document_identity(RELEASE_ID, "analysis-1", b"same")

    assert first == second


def test_changed_analysis_content_produces_a_new_identity() -> None:
    """Breaks if changed document bytes reuse the identity of an earlier revision."""
    assert document_identity(RELEASE_ID, "analysis-1", b"first") != document_identity(
        RELEASE_ID, "analysis-1", b"second"
    )


def test_analysis_collection_deduplicates_an_unchanged_revision(tmp_path: Path) -> None:
    """Breaks if an unchanged retry creates a second promotion candidate."""
    first = _write_analysis(tmp_path / "first")
    second = _write_analysis(tmp_path / "second")

    result = staging.validate_analysis_collection([second, first], {RELEASE_ID})

    assert len(result) == 1
    assert result[0].content_version == "1"


def test_analysis_collection_rejects_changed_bytes_for_one_revision(
    tmp_path: Path,
) -> None:
    """Breaks if one movie/document/version key can identify different bytes."""
    first = _write_analysis(tmp_path / "first", body=b"first revision bytes\n")
    changed = _write_analysis(tmp_path / "changed", body=b"changed revision bytes\n")

    with pytest.raises(StagingError, match="revision conflict"):
        staging.validate_analysis_collection([first, changed], {RELEASE_ID})


@pytest.mark.parametrize(
    ("metadata_overrides", "path_component"),
    [
        ({"movie_id": "1970_OTHER_001"}, "movie_id"),
        ({"submission_id": "poster-2"}, "submission_id"),
    ],
)
def test_poster_path_must_match_sidecar_before_payload_decode(
    tmp_path: Path,
    metadata_overrides: dict[str, object],
    path_component: str,
) -> None:
    """Breaks if sidecar identity can disagree with its staging directory."""
    payload = b"not an image"
    metadata = _poster_metadata(
        expected_sha256=hashlib.sha256(payload).hexdigest(),
        **metadata_overrides,
    )
    path = _write_poster_submission(tmp_path, metadata=metadata)
    (path / "poster.jpg").write_bytes(payload)

    with pytest.raises(StagingError, match=rf"poster path {path_component} mismatch"):
        validate_poster_submission(path, {RELEASE_ID, "1970_OTHER_001"})


@pytest.mark.parametrize(
    ("metadata_overrides", "path_component"),
    [
        ({"movie_id": "1970_OTHER_001"}, "movie_id"),
        ({"document_id": "analysis-2"}, "document_id"),
    ],
)
def test_analysis_path_must_match_front_matter_before_body_hash(
    tmp_path: Path,
    metadata_overrides: dict[str, object],
    path_component: str,
) -> None:
    """Breaks if front-matter identity can disagree with its staging path."""
    path = _write_analysis(
        tmp_path,
        content_sha256="0" * 64,
        path_movie_id=RELEASE_ID,
        **metadata_overrides,
    )

    with pytest.raises(StagingError, match=rf"analysis path {path_component} mismatch"):
        validate_analysis_submission(path, {RELEASE_ID, "1970_OTHER_001"})


def test_analysis_recomputes_body_hash_and_rejects_a_false_declaration(
    tmp_path: Path,
) -> None:
    """Breaks if the declared analysis digest is trusted instead of recomputed."""
    path = _write_analysis(tmp_path, content_sha256="0" * 64)

    with pytest.raises(StagingError, match="content SHA-256 mismatch"):
        validate_analysis_submission(path, {RELEASE_ID})


def test_poster_recomputes_file_hash_and_rejects_a_false_declaration(
    tmp_path: Path,
) -> None:
    """Breaks if the declared poster digest is trusted instead of image bytes."""
    path = _write_poster_submission(
        tmp_path,
        metadata=_poster_metadata(expected_sha256="0" * 64),
    )

    with pytest.raises(StagingError, match="poster SHA-256 mismatch"):
        validate_poster_submission(path, {RELEASE_ID})


def test_source_urls_reject_embedded_credentials(tmp_path: Path) -> None:
    """Breaks if staging accepts credential-bearing provenance URLs."""
    path = _write_poster_submission(
        tmp_path,
        metadata=_poster_metadata(source_url="https://:secret@example.com/poster.jpg"),
    )

    with pytest.raises(StagingError, match="without credentials"):
        validate_poster_submission(path, {RELEASE_ID})


@pytest.mark.parametrize(
    "source_url",
    [
        "https://example.com:99999/poster.jpg",
        "https://[::1/poster.jpg",
        "https://:/poster.jpg",
    ],
    ids=["out-of-range-port", "malformed-ipv6", "missing-host"],
)
def test_source_urls_reject_malformed_authorities(
    tmp_path: Path, source_url: str
) -> None:
    """Breaks if malformed URL authorities can enter provenance metadata."""
    path = _write_poster_submission(
        tmp_path,
        metadata=_poster_metadata(source_url=source_url),
    )

    with pytest.raises(StagingError, match=r"safe HTTP\(S\) URL"):
        validate_poster_submission(path, {RELEASE_ID})


def test_poster_and_analysis_reject_additional_properties(tmp_path: Path) -> None:
    """Breaks if undeclared metadata can silently alter a staged submission."""
    poster_metadata = _poster_metadata()
    poster_metadata["unreviewed_override"] = True
    poster = _write_poster_submission(tmp_path / "poster", metadata=poster_metadata)
    analysis = _write_analysis(tmp_path / "analysis", unreviewed_override=True)

    with pytest.raises(StagingError, match="additional properties"):
        validate_poster_submission(poster, {RELEASE_ID})
    with pytest.raises(StagingError, match="additional properties"):
        validate_analysis_submission(analysis, {RELEASE_ID})


def test_duplicate_metadata_keys_fail_closed(tmp_path: Path) -> None:
    """Breaks if ambiguous JSON or YAML keys are resolved by last-value-wins."""
    poster = _write_poster_submission(tmp_path / "poster", metadata=_poster_metadata())
    metadata_path = poster / "metadata.json"
    metadata_path.write_text(
        metadata_path.read_text(encoding="utf-8").replace(
            '"movie_id": "1970_GSQ_001"',
            '"movie_id": "1970_GSQ_001", "movie_id": "1970_GSQ_001"',
        ),
        encoding="utf-8",
    )
    analysis = tmp_path / "analysis" / "duplicate.md"
    analysis.parent.mkdir(parents=True)
    body = b"duplicate keys\n"
    digest = hashlib.sha256(body).hexdigest()
    analysis.write_bytes(
        (
            "---\n"
            "schema_version: '1.0'\n"
            f"movie_id: {RELEASE_ID}\n"
            f"movie_id: {RELEASE_ID}\n"
            "document_id: duplicate\n"
            "document_type: analysis\n"
            "language: en\n"
            "title: Duplicate\n"
            "source_urls: [https://example.com/source]\n"
            "rights_status: cleared\n"
            "authorship_method: human\n"
            "content_version: '1'\n"
            f"content_sha256: {digest}\n"
            "---\n"
        ).encode()
        + body
    )

    with pytest.raises(StagingError, match="duplicate metadata key"):
        validate_poster_submission(poster, {RELEASE_ID})
    with pytest.raises(StagingError, match="duplicate front-matter key"):
        validate_analysis_submission(analysis, {RELEASE_ID})


def test_publishability_requires_both_quality_and_cleared_rights(tmp_path: Path) -> None:
    """Breaks if quality alone or rights alone can authorize publication."""
    cleared = validate_poster_submission(
        _write_poster_submission(tmp_path / "cleared", metadata=_poster_metadata()),
        {RELEASE_ID},
    )
    unknown = validate_poster_submission(
        _write_poster_submission(
            tmp_path / "unknown",
            metadata=_poster_metadata(rights_status="unknown"),
        ),
        {RELEASE_ID},
    )
    rejected = validate_analysis_submission(
        _write_analysis(
            tmp_path / "rejected",
            quality_status="rejected",
            rights_status="cleared",
        ),
        {RELEASE_ID},
    )

    assert cleared.publishable is True
    assert unknown.publishable is False
    assert rejected.publishable is False


def test_poster_rejects_noncanonical_or_linked_submission_entries(
    tmp_path: Path,
) -> None:
    """Breaks if staging follows a linked sidecar or accepts an ambiguous payload."""
    submission = _write_poster_submission(tmp_path, metadata=_poster_metadata())
    outside = tmp_path / "outside.json"
    outside.write_text("{}", encoding="utf-8")
    metadata = submission / "metadata.json"
    metadata.unlink()
    try:
        metadata.symlink_to(outside)
    except OSError:
        pytest.skip("symbolic links are unavailable")

    with pytest.raises(StagingError, match="direct regular files"):
        validate_poster_submission(submission, {RELEASE_ID})


def test_valid_analysis_fixture_has_deterministic_cli_output(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    capsys: pytest.CaptureFixture[str],
) -> None:
    """Breaks if CLI membership depends on ignored repository-local Release data."""
    fixture = _write_analysis(
        tmp_path / "data" / "staging" / "analyses",
        body=b"# Analysis\n\nA source-backed analysis for contract validation.\n",
    )
    _write_release_ids(tmp_path, [RELEASE_ID])
    _mock_cli_settings(monkeypatch, tmp_path)
    monkeypatch.chdir(tmp_path)
    monkeypatch.setattr(
        sys,
        "argv",
        [
            "hk-movie-rag",
            "validate-staging",
            "--kind",
            "analysis",
            fixture.relative_to(tmp_path).as_posix(),
        ],
    )

    main()

    assert json.loads(capsys.readouterr().out) == {
        "content_sha256": "df407e1c2fc2ef1f3dc7bc3ef5b73e14a2a88642be96081823839637053ef816",
        "document_id": "analysis-1",
        "identity": "510ac06d0dc63e9fd576eda88ec4e6f1e25dc0e493a1cdb7367f2a7238aea612",
        "kind": "analysis",
        "movie_id": RELEASE_ID,
        "publishable": True,
    }


@pytest.mark.parametrize("release_bytes", [None, b"not parquet"], ids=["missing", "corrupt"])
def test_cli_reports_unreadable_canonical_release_as_a_governed_error(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    capsys: pytest.CaptureFixture[str],
    release_bytes: bytes | None,
) -> None:
    """Breaks if missing/corrupt canonical Parquet escapes as a traceback."""
    fixture = _write_analysis(tmp_path / "data" / "staging" / "analyses")
    release_path = tmp_path / "data" / "release" / "v1.2" / "movies.parquet"
    release_path.parent.mkdir(parents=True)
    if release_bytes is not None:
        release_path.write_bytes(release_bytes)
    _mock_cli_settings(monkeypatch, tmp_path)
    monkeypatch.chdir(tmp_path)
    monkeypatch.setattr(
        sys,
        "argv",
        [
            "hk-movie-rag",
            "validate-staging",
            "--kind",
            "analysis",
            fixture.relative_to(tmp_path).as_posix(),
        ],
    )

    with pytest.raises(SystemExit) as exc_info:
        main()

    assert exc_info.value.code == 2
    stderr = capsys.readouterr().err
    assert "Release movie IDs are unreadable" in stderr
    assert "Traceback" not in stderr


def test_cli_containment_preserves_the_unresolved_input_path(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """Breaks if containment resolution erases a link before no-follow validation."""
    repo = tmp_path / "repo"
    repo.mkdir()
    submitted = repo / "data" / "staging" / "linked.md"
    submitted.parent.mkdir(parents=True)
    submitted.write_bytes(b"submitted")
    physical = repo / "physical.md"
    physical.write_bytes(b"physical")
    real_resolve = Path.resolve

    def resolve_with_link(path: Path, strict: bool = False) -> Path:
        if path == submitted:
            return physical
        return real_resolve(path, strict=strict)

    monkeypatch.setattr(Path, "resolve", resolve_with_link)

    result = cli._resolve_staging_path(repo, Path("data/staging/linked.md"), ())

    assert result == submitted


def test_byte_hashed_analysis_fixture_is_reproducibly_lf_pinned(repo_root: Path) -> None:
    """Breaks if checkout line-ending conversion changes the declared body digest."""
    fixture = repo_root / "tests" / "fixtures" / "analysis" / RELEASE_ID / "analysis-1.md"
    payload = fixture.read_bytes()
    body = payload.split(b"---\n", maxsplit=2)[2]
    attributes = subprocess.run(
        ["git", "check-attr", "eol", "--", fixture.relative_to(repo_root).as_posix()],
        cwd=repo_root,
        check=True,
        capture_output=True,
        text=True,
    ).stdout

    assert b"\r\n" not in payload
    assert hashlib.sha256(body).hexdigest() == (
        "df407e1c2fc2ef1f3dc7bc3ef5b73e14a2a88642be96081823839637053ef816"
    )
    assert attributes.rstrip().endswith("eol: lf")


def _write_poster_submission(
    root: Path,
    *,
    metadata: dict[str, object],
) -> Path:
    submission = root / RELEASE_ID / "poster-1"
    submission.mkdir(parents=True)
    (submission / "poster.jpg").write_bytes(POSTER_BYTES)
    (submission / "metadata.json").write_text(
        json.dumps(metadata, ensure_ascii=False),
        encoding="utf-8",
    )
    return submission


def _poster_metadata(**overrides: object) -> dict[str, object]:
    metadata: dict[str, object] = {
        "schema_version": "1.0",
        "movie_id": RELEASE_ID,
        "submission_id": "poster-1",
        "source_url": "https://example.com/poster.jpg",
        "retrieved_at": "2026-07-27T10:00:00Z",
        "rights_status": "cleared",
        "expected_sha256": hashlib.sha256(POSTER_BYTES).hexdigest(),
    }
    metadata.update(overrides)
    return metadata


def _write_analysis(
    root: Path,
    *,
    movie_id: str = RELEASE_ID,
    content_sha256: str | None = None,
    body: bytes = b"Analysis body.\n",
    path_movie_id: str | None = None,
    path_document_id: str = "analysis-1",
    **overrides: object,
) -> Path:
    metadata: dict[str, object] = {
        "schema_version": "1.0",
        "movie_id": movie_id,
        "document_id": "analysis-1",
        "document_type": "analysis",
        "language": "en",
        "title": "A test analysis",
        "source_urls": ["https://example.com/source"],
        "rights_status": "cleared",
        "authorship_method": "human",
        "content_version": "1",
        "content_sha256": content_sha256 or hashlib.sha256(body).hexdigest(),
    }
    metadata.update(overrides)
    path = root / (path_movie_id or movie_id) / f"{path_document_id}.md"
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_bytes(
        b"---\n" + json.dumps(metadata, ensure_ascii=False).encode("utf-8") + b"\n---\n" + body
    )
    return path


def _write_release_ids(repo_root: Path, movie_ids: list[str]) -> Path:
    path = repo_root / "data" / "release" / "v1.2" / "movies.parquet"
    path.parent.mkdir(parents=True, exist_ok=True)
    pq.write_table(pa.table({"movie_id": movie_ids}), path)
    return path


def _mock_cli_settings(monkeypatch: pytest.MonkeyPatch, repo_root: Path) -> None:
    protected = repo_root / "protected-source"
    protected.mkdir()
    settings = SimpleNamespace(
        repo_root=repo_root.resolve(strict=True),
        release_version="v1.2",
        inventory_roots=(protected.resolve(strict=True),),
    )
    monkeypatch.setattr(cli, "Settings", SimpleNamespace(load=lambda _: settings))
