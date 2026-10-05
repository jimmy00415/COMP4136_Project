"""CLI boundary tests for reusable RAG bundle document roots."""

from __future__ import annotations

import hashlib
import json
import sys
from contextlib import contextmanager
from pathlib import Path
from types import SimpleNamespace

import pytest

from hk_movie_rag import cli


def test_verify_rag_bundle_cli_emits_the_complete_deployment_authority(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    capsys: pytest.CaptureFixture[str],
) -> None:
    """Breaks if deployment must reconstruct any R2 identity outside verification."""
    manifest_path = tmp_path / "rag_release_manifest.json"
    documents = [
        {
            "movie_id": "1987_FGBR_001",
            "document_id": "its-a-mad-mad-mad-world-deep-analysis-v1",
            "source_filename": "S級港片-富貴逼人.pdf",
            "source_sha256": "1" * 64,
            "rights_status": "restricted",
            "quality_status": "manual_approved",
            "page_count": 6,
        }
    ]
    manifest_bytes = json.dumps({"documents": documents}, ensure_ascii=False).encode()
    manifest_path.write_bytes(manifest_bytes)
    manifest_sha256 = hashlib.sha256(manifest_bytes).hexdigest()
    counts = SimpleNamespace(
        movies=4_658,
        facet_count=4_658,
        tier_s_count=50,
        tier_a_count=313,
        tier_b_count=4_295,
        pilot_count=24,
        poster_rows=4_658,
        primary_poster_rows=4_658,
        approved_poster_objects=4_545,
        unavailable_poster_rows=113,
        derived_poster_bytes=123_456,
        metadata_passages=4_658,
        documents=1,
        pdf_passages=6,
    )
    contract = SimpleNamespace(
        schema_version="1.2",
        rag_release_id="v1.2-demo-r2",
        parent_release_manifest_sha256="2" * 64,
        manifest_sha256=manifest_sha256,
        bundle_sha256="4" * 64,
        derived_inventory_sha256="5" * 64,
        counts=counts,
        expected_embeddings=4_664,
        embedding_model="gemini-embedding-2",
        embedding_dimension=768,
        generation_model="gemini-3.5-flash-lite",
        text_extraction_profile="cjk-layout-v1",
        document_embedding_profile="vertex-title-text-v1",
        relevance_policy_sha256="6" * 64,
        poster_authority_sha256="7" * 64,
        access_mode="restricted_demo",
    )
    verified = SimpleNamespace(
        document_count=1,
        embedding_dimension=768,
        embedding_model="gemini-embedding-2",
        facet_count=4_658,
        movie_count=4_658,
        pilot_count=24,
        pdf_passage_count=6,
        poster_row_count=4_658,
        tier_a_count=313,
        tier_b_count=4_295,
        tier_s_count=50,
        primary_poster_row_count=4_658,
        approved_poster_object_count=4_545,
        unavailable_poster_row_count=113,
        derived_poster_bytes=123_456,
        derived_inventory_sha256="5" * 64,
        contract=contract,
    )
    monkeypatch.setattr(cli, "verify_rag_bundle", lambda _: verified)
    monkeypatch.setattr(
        sys, "argv", ["hk-movie-rag", "verify-rag-bundle", str(manifest_path)]
    )

    cli.main()

    assert json.loads(capsys.readouterr().out) == {
        "access_mode": "restricted_demo",
        "approved_poster_object_count": 4_545,
        "bundle_sha256": "4" * 64,
        "derived_inventory_sha256": "5" * 64,
        "derived_poster_bytes": 123_456,
        "document_count": 1,
        "document_embedding_profile": "vertex-title-text-v1",
        "documents": documents,
        "embedding_dimension": 768,
        "embedding_model": "gemini-embedding-2",
        "expected_embedding_count": 4_664,
        "facet_count": 4_658,
        "gcs_prefix": "rag/v1.2-demo-r2/",
        "generation_model": "gemini-3.5-flash-lite",
        "manifest_sha256": manifest_sha256,
        "metadata_passage_count": 4_658,
        "movie_count": 4_658,
        "parent_release_manifest_sha256": "2" * 64,
        "pdf_passage_count": 6,
        "pilot_count": 24,
        "poster_authority_sha256": "7" * 64,
        "poster_row_count": 4_658,
        "primary_poster_row_count": 4_658,
        "rag_schema_version": "1.2",
        "rag_release_id": "v1.2-demo-r2",
        "relevance_policy_sha256": "6" * 64,
        "schema_version": "rag-bundle-verification/v2",
        "text_extraction_profile": "cjk-layout-v1",
        "tier_a_count": 313,
        "tier_b_count": 4_295,
        "tier_s_count": 50,
        "unavailable_poster_row_count": 113,
        "valid": True,
    }


def test_verify_poster_fleet_cli_resolves_the_same_remote_verified_manifest(
    monkeypatch: pytest.MonkeyPatch,
    capsys: pytest.CaptureFixture[str],
) -> None:
    """Breaks if the poster job reconstructs a release ID instead of verifying the bundle."""
    manifest_uri = (
        "gs://motionexpaiweb-880586285913-hk-movie-rag-release/"
        "rag/v1.2-demo-r2/rag_release_manifest.json"
    )
    captured: list[tuple[str, str]] = []
    contract = SimpleNamespace(poster_authority_sha256="a" * 64)
    bundle = SimpleNamespace(rag_release_id="v1.2-demo-r2", contract=contract)

    @contextmanager
    def fake_source(uri: str, *, project_id: str):
        captured.append((uri, project_id))
        yield bundle

    @contextmanager
    def fake_repository():
        yield object()

    report = SimpleNamespace(passed=True, to_dict=lambda: {"passed": True})
    monkeypatch.setattr(cli, "verified_bundle_source", fake_source)
    monkeypatch.setattr(cli, "_repository_from_env", fake_repository)
    monkeypatch.setattr(cli, "GcsPosterFleetStorage", object)
    monkeypatch.setattr(cli, "load_poster_serving_authority", lambda *_args, **_kwargs: object())
    monkeypatch.setattr(cli, "verify_poster_fleet", lambda *_args, **_kwargs: report)
    monkeypatch.setattr(
        sys,
        "argv",
        [
            "hk-movie-rag",
            "verify-poster-fleet",
            "--manifest",
            manifest_uri,
            "--bucket",
            "motionexpaiweb-880586285913-hk-movie-rag-release",
        ],
    )

    cli.main()

    assert captured == [(manifest_uri, "motionexpaiweb")]
    assert json.loads(capsys.readouterr().out) == {"passed": True}


def _bundle_result(output: Path) -> SimpleNamespace:
    return SimpleNamespace(
        bundle_sha256="a" * 64,
        document_count=1,
        facet_count=1,
        manifest_path=output / "rag_release_manifest.json",
        movie_count=1,
        pilot_count=0,
        pdf_passage_count=1,
        poster_row_count=1,
        tier_a_count=0,
        tier_b_count=1,
        tier_s_count=0,
        primary_poster_row_count=1,
        approved_poster_object_count=0,
        unavailable_poster_row_count=1,
        derived_poster_bytes=0,
        derived_inventory_sha256="b" * 64,
    )


@pytest.mark.parametrize("explicit_document_root", [False, True])
def test_build_rag_bundle_cli_resolves_optional_document_source_root(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    explicit_document_root: bool,
) -> None:
    """Breaks if the CLI omits, mis-defaults, or passes an unresolved document root."""
    source_root = tmp_path / "source"
    document_root = tmp_path / "documents"
    source_root.mkdir()
    document_root.mkdir()
    captured: list[Path] = []

    def fake_build(
        source: Path,
        config: Path,
        output: Path,
        *,
        document_source_root: Path,
    ) -> SimpleNamespace:
        assert source == source_root
        captured.append(document_source_root)
        return _bundle_result(output)

    monkeypatch.setattr(cli, "build_rag_bundle", fake_build)
    arguments = [
        "hk-movie-rag",
        "build-rag-bundle",
        "--source-root",
        str(source_root),
        "--config",
        str(tmp_path / "config.yaml"),
        "--output",
        str(tmp_path / "out"),
    ]
    if explicit_document_root:
        arguments.extend(["--document-source-root", str(document_root / ".." / "documents")])
    monkeypatch.setattr(sys, "argv", arguments)

    cli.main()

    assert captured == [(document_root if explicit_document_root else source_root).resolve()]


def test_build_rag_bundle_cli_rejects_non_directory_document_root(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """Breaks if raw-document input can be bound to a missing or non-directory path."""
    source_root = tmp_path / "source"
    source_root.mkdir()
    not_a_directory = tmp_path / "document.pdf"
    not_a_directory.write_bytes(b"pdf")
    monkeypatch.setattr(
        sys,
        "argv",
        [
            "hk-movie-rag",
            "build-rag-bundle",
            "--source-root",
            str(source_root),
            "--document-source-root",
            str(not_a_directory),
            "--config",
            str(tmp_path / "config.yaml"),
            "--output",
            str(tmp_path / "out"),
        ],
    )

    with pytest.raises(SystemExit) as exc_info:
        cli.main()

    assert exc_info.value.code == 2


def test_preflight_pdf_binding_cli_emits_canonical_unicode_json(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    capsys: pytest.CaptureFixture[str],
) -> None:
    source_root = tmp_path / "source"
    document_root = tmp_path / "documents"
    source_root.mkdir()
    document_root.mkdir()
    config_path = tmp_path / "rag.yaml"
    expected = {
        "binding": {
            "movie_id": "1987_FGBR_001",
            "source_filename": "富貴逼人.pdf",
        },
        "schema_version": "rag-pdf-binding-preflight/v1",
        "valid": True,
    }
    captured: list[object] = []

    def fake_prepare(*args: object, **kwargs: object) -> SimpleNamespace:
        captured.extend((args, kwargs))
        return SimpleNamespace(to_dict=lambda: expected)

    monkeypatch.setattr(cli, "prepare_pdf_binding", fake_prepare)
    monkeypatch.setattr(
        sys,
        "argv",
        [
            "hk-movie-rag",
            "preflight-pdf-binding",
            "--source-root",
            str(source_root),
            "--document-source-root",
            str(document_root / ".." / "documents"),
            "--config",
            str(config_path),
            "--movie-id",
            "1987_FGBR_001",
            "--document-id",
            "its-a-mad-mad-mad-world-deep-analysis-v1",
            "--source-filename",
            "富貴逼人.pdf",
            "--rights-status",
            "restricted",
            "--quality-status",
            "manual_approved",
        ],
    )

    cli.main()

    assert captured == [
        (source_root, config_path, document_root.resolve()),
        {
            "movie_id": "1987_FGBR_001",
            "document_id": "its-a-mad-mad-mad-world-deep-analysis-v1",
            "source_filename": "富貴逼人.pdf",
            "rights_status": "restricted",
            "quality_status": "manual_approved",
        },
    ]
    assert capsys.readouterr().out == (
        json.dumps(expected, ensure_ascii=False, sort_keys=True, separators=(",", ":"))
        + "\n"
    )


def test_preflight_pdf_binding_cli_requires_explicit_governance_decisions(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    capsys: pytest.CaptureFixture[str],
) -> None:
    source_root = tmp_path / "source"
    document_root = tmp_path / "documents"
    source_root.mkdir()
    document_root.mkdir()
    monkeypatch.setattr(
        sys,
        "argv",
        [
            "hk-movie-rag",
            "preflight-pdf-binding",
            "--source-root",
            str(source_root),
            "--document-source-root",
            str(document_root),
            "--config",
            str(tmp_path / "rag.yaml"),
            "--movie-id",
            "1987_FGBR_001",
            "--document-id",
            "its-a-mad-mad-mad-world-deep-analysis-v1",
            "--source-filename",
            "富貴逼人.pdf",
        ],
    )

    with pytest.raises(SystemExit) as exc_info:
        cli.main()

    assert exc_info.value.code == 2
    stderr = capsys.readouterr().err
    assert "--rights-status" in stderr
    assert "--quality-status" in stderr


def test_ingest_cli_requires_final_state_acceptance_with_reuse_before_bundle_access(
    monkeypatch: pytest.MonkeyPatch,
    capsys: pytest.CaptureFixture[str],
) -> None:
    """Breaks if reuse can begin without explicit final-state gates or touch Vertex first."""
    source_called = False

    def forbidden_source(*_args: object, **_kwargs: object) -> object:
        nonlocal source_called
        source_called = True
        raise AssertionError("bundle source must not be opened")

    monkeypatch.setattr(cli, "verified_bundle_source", forbidden_source)
    monkeypatch.setattr(
        sys,
        "argv",
        [
            "hk-movie-rag",
            "ingest-rag-bundle",
            "unused-manifest.json",
            "--reuse-from-release-id",
            "v1.2-demo",
        ],
    )

    with pytest.raises(SystemExit) as exc_info:
        cli.main()

    assert exc_info.value.code == 2
    assert "reuse acceptance counts must be provided together" in capsys.readouterr().err
    assert source_called is False


@pytest.mark.parametrize(
    ("arguments", "message"),
    [
        (["--require-embedded", "1"], "ingestion acceptance counts must be provided together"),
        (["--require-skipped", "1"], "ingestion acceptance counts must be provided together"),
        (
            ["--require-embedded", "-1", "--require-skipped", "0"],
            "ingestion acceptance counts must be non-negative",
        ),
        (
            ["--require-source-eligible-total", "1", "--require-non-reusable-embedded-total", "0"],
            "reuse acceptance counts require a source release",
        ),
        (
            [
                "--reuse-from-release-id",
                "v1.2-demo",
                "--require-source-eligible-total",
                "-1",
                "--require-non-reusable-embedded-total",
                "0",
            ],
            "reuse acceptance counts must be non-negative",
        ),
        (
            [
                "--reuse-from-release-id",
                "   ",
                "--require-source-eligible-total",
                "0",
                "--require-non-reusable-embedded-total",
                "0",
            ],
            "reuse source release ID must be non-empty",
        ),
    ],
)
def test_ingest_cli_rejects_every_invalid_gate_shape_before_side_effects(
    monkeypatch: pytest.MonkeyPatch,
    capsys: pytest.CaptureFixture[str],
    arguments: list[str],
    message: str,
) -> None:
    """Breaks if malformed old/new acceptance gates open bundle, DB, or Vertex first."""
    source_called = False

    def forbidden_source(*_args: object, **_kwargs: object) -> object:
        nonlocal source_called
        source_called = True
        raise AssertionError("bundle source must not be opened")

    monkeypatch.setattr(cli, "verified_bundle_source", forbidden_source)
    monkeypatch.setattr(
        sys,
        "argv",
        [
            "hk-movie-rag",
            "ingest-rag-bundle",
            "unused-manifest.json",
            *arguments,
        ],
    )

    with pytest.raises(SystemExit) as exc_info:
        cli.main()

    assert exc_info.value.code == 2
    assert message in capsys.readouterr().err
    assert source_called is False
