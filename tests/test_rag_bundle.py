"""Behavior tests for the deterministic, parent-bound RAG release bundle."""

from __future__ import annotations

import hashlib
import json
from dataclasses import dataclass
from importlib.resources import files
from pathlib import Path

import pyarrow as pa
import pyarrow.parquet as pq
import pytest
import yaml
from PIL import Image
from pypdf import PdfWriter
from pypdf.generic import DecodedStreamObject, DictionaryObject, NameObject

from hk_movie_rag import rag_bundle
from hk_movie_rag.poster_policy import MAX_POSTER_BYTES
from hk_movie_rag.rag_bundle import (
    RagBundleError,
    build_rag_bundle,
    prepare_pdf_binding,
    verify_rag_bundle,
)


def test_checked_in_r2_config_binds_three_governed_documents_without_rewriting_v1() -> None:
    """Breaks if the operator config loses its immutable parent or raw-document bindings."""
    root = Path(__file__).resolve().parents[1]
    historical = root / "config" / "rag_demo.yaml"
    config = yaml.safe_load((root / "config" / "rag_demo_r2.yaml").read_text(encoding="utf-8"))

    assert _sha256(historical) == "81a3794b29a2e02048a7e2b4bd7b3c1e38c82491b60132c0a04c75c4ba1ce0d0"
    assert config == {
        "schema_version": "1.2",
        "rag_release_id": "v1.2-demo-r2",
        "parent_release_manifest_sha256": (
            "e9842db33208e7146d78d86689b13f81fffcf41288d79d84e599b93050445e2c"
        ),
        "expected_movies": 4_658,
        "expected_poster_rows": 4_658,
        "expected_approved_poster_objects": 4_545,
        "expected_unavailable_poster_rows": 113,
        "expected_derived_inventory_sha256": (
            "b754727129bc044b6c9da1e6db22067c8279c5e7f8dd41d0350e365376538154"
        ),
        "expected_facet_count": 4_658,
        "expected_tier_s_count": 50,
        "expected_tier_a_count": 313,
        "expected_tier_b_count": 4_295,
        "expected_pilot_count": 24,
        "embedding_model": "gemini-embedding-2",
        "embedding_dimension": 768,
        "generation_model": "gemini-3.5-flash-lite",
        "text_extraction_profile": "cjk-layout-v1",
        "document_embedding_profile": "vertex-title-text-v1",
        "relevance_policy_sha256": (
            "a4ecce727a469689a2a08c6d42e0d7d1eb975761173e3b6e4d1eaf34db7b2aba"
        ),
        "poster_authority_sha256": (
            "9e56adda2a1fdfa648869c53d7e60a085db5d1c6d11db90dbec13bfe12f0eeed"
        ),
        "access_mode": "restricted_demo",
        "deep_documents": [
            {
                "movie_id": "1978_ZQ_001",
                "document_id": "drunken-master-deep-analysis-v1",
                "source_filename": "S 級港⽚-醉拳.pdf",
                "source_sha256": (
                    "ab27baf8e22ceb63a476b65dbfb02951a63a9e9bf39d96a47cd8e3f9fd314a75"
                ),
                "rights_status": "restricted",
                "quality_status": "manual_approved",
                "page_count": 3,
            },
            {
                "movie_id": "1982_ZJPD_001",
                "document_id": "aces-go-places-deep-analysis-v1",
                "source_filename": "S 級港⽚-最佳拍檔.pdf",
                "source_sha256": (
                    "69fa1ad28916c043689ef4fe826cde816c05a3b254e5a0372d1a57ca2ce63bce"
                ),
                "rights_status": "restricted",
                "quality_status": "manual_approved",
                "page_count": 3,
            },
            {
                "movie_id": "1987_FGBR_001",
                "document_id": "its-a-mad-mad-mad-world-deep-analysis-v1",
                "source_filename": "S級港片-富貴逼人.pdf",
                "source_sha256": (
                    "5aef7f8684bc0d91d77ffb6d7f89cb323358e2804ef8eaacbc0a02cfeb3d3df4"
                ),
                "rights_status": "restricted",
                "quality_status": "manual_approved",
                "page_count": 6,
            },
        ],
    }


@dataclass(frozen=True)
class RagSource:
    root: Path
    config: Path
    pdf_paths: tuple[Path, ...]


@pytest.fixture
def rag_source(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> RagSource:
    """A small manifest-shaped source; the real Release verifier is integration-tested separately."""
    root = tmp_path / "source"
    monkeypatch.setattr("hk_movie_rag.rag_bundle.verify_release_manifest", lambda _: None)
    release = root / "data" / "release" / "vtest"
    release.mkdir(parents=True)
    movies = [
        {
            "movie_id": "1978_ZQ_001",
            "chinese_title": "醉拳",
            "english_title": "Drunken Master",
            "release_date": "1978-10-05",
            "production_region": "Hong Kong",
            "director": "袁和平",
            "screenwriter": "吳思遠",
            "cast": "成龍",
            "genre": "動作",
            "runtime_minutes": "111",
            "production_company": "思遠影業",
            "data_source": "fixture",
        },
        {
            "movie_id": "1982_ZJPD_001",
            "chinese_title": "最佳拍檔",
            "english_title": "Aces Go Places",
            "release_date": "1982-01-16",
            "production_region": "Hong Kong",
            "director": "曾志偉",
            "screenwriter": "黃百鳴",
            "cast": "許冠傑",
            "genre": "喜劇",
            "runtime_minutes": "93",
            "production_company": "新藝城",
            "data_source": "fixture",
        },
        {
            "movie_id": "1983_TS_001",
            "chinese_title": "測試片",
            "english_title": "Test Film",
            "release_date": "1983-01-01",
            "production_region": "Hong Kong",
            "director": "測試導演",
            "screenwriter": "測試編劇",
            "cast": "測試主演",
            "genre": "劇情",
            "runtime_minutes": "90",
            "production_company": "測試公司",
            "data_source": "fixture",
        },
    ]
    _write_csv(release / "movies.csv", movies)
    pq.write_table(pa.Table.from_pylist(movies), release / "movies.parquet")
    pq.write_table(pa.Table.from_pylist([]), release / "enrichment_provenance.parquet")
    tiers = [
        {
            "movie_id": "1978_ZQ_001",
            "tier": "S",
            "tier_reason": "fixture S-tier evidence",
            "human_review": "required",
            "pilot_movie": False,
        },
        {
            "movie_id": "1982_ZJPD_001",
            "tier": "A",
            "tier_reason": "fixture A-tier evidence",
            "human_review": "not_required",
            "pilot_movie": True,
        },
        {
            "movie_id": "1983_TS_001",
            "tier": "B",
            "tier_reason": "fixture B-tier evidence",
            "human_review": "not_required",
            "pilot_movie": False,
        },
    ]
    pilots = [
        {
            "movie_id": "1982_ZJPD_001",
            "handbook_genre": "喜劇",
            "evidence_note": "fixture pilot evidence",
            "evidence_url": "https://example.test/pilot/1982_ZJPD_001",
            "source_record_id": "fixture-pilot-1",
        }
    ]
    pq.write_table(pa.Table.from_pylist(tiers), release / "movie_tiers.parquet")
    pq.write_table(pa.Table.from_pylist(pilots), release / "pilot_movies.parquet")
    derived_dir = root / "assets" / "posters" / "derived"
    derived_dir.mkdir(parents=True)
    approved_ids = {"1978_ZQ_001", "1982_ZJPD_001"}
    for index, movie_id in enumerate(sorted(approved_ids), start=1):
        Image.new("RGB", (2, 2), (index * 30, index * 40, index * 50)).save(
            derived_dir / f"{movie_id}.webp", format="WEBP", lossless=True
        )
    posters = [
        _poster(movie["movie_id"], approved=movie["movie_id"] in approved_ids)
        for movie in movies
    ]
    pq.write_table(pa.Table.from_pylist(posters), release / "poster_manifest.parquet")
    manifest_path = release / "release_manifest.json"
    artifacts = sorted(path for path in release.glob("*") if path.name != manifest_path.name)
    manifest_path.write_text(
        json.dumps(
            {
                "release_version": "vtest",
                "artifacts": [
                    {
                        "relative_path": path.relative_to(root).as_posix(),
                        "row_count": {
                            "movies.csv": 3,
                            "movies.parquet": 3,
                            "movie_tiers.parquet": 3,
                            "pilot_movies.parquet": 1,
                            "poster_manifest.parquet": 3,
                        }.get(path.name, 0),
                        "schema_path": "schemas/movie.schema.json",
                        "size_bytes": path.stat().st_size,
                        "sha256": _sha256(path),
                    }
                    for path in artifacts
                ],
            },
            ensure_ascii=False,
            sort_keys=True,
            separators=(",", ":"),
        )
        + "\n",
        encoding="utf-8",
    )
    pdf_dir = root / "deep"
    pdf_dir.mkdir()
    pdf_paths = (pdf_dir / "drunken.pdf", pdf_dir / "aces.pdf")
    for path, text in zip(pdf_paths, ("醉拳 分析", "最佳拍檔 分析"), strict=True):
        _write_one_page_pdf(path, text)
    config = root / "rag_demo.yaml"
    config.write_text(
        yaml.safe_dump(
            {
                "schema_version": "1.1",
                "rag_release_id": "vtest-demo",
                "parent_release_manifest_path": "data/release/vtest/release_manifest.json",
                "parent_release_manifest_sha256": _sha256(manifest_path),
                "expected_movies": 3,
                "expected_poster_rows": 3,
                "expected_approved_poster_objects": 2,
                "expected_unavailable_poster_rows": 1,
                "expected_derived_inventory_sha256": _fixture_inventory_sha256(
                    root, approved_ids
                ),
                "expected_facet_count": 3,
                "expected_tier_s_count": 1,
                "expected_tier_a_count": 1,
                "expected_tier_b_count": 1,
                "expected_pilot_count": 1,
                "embedding_model": "gemini-embedding-2",
                "embedding_dimension": 768,
                "generation_model": "gemini-3.5-flash-lite",
                "access_mode": "restricted_demo",
                "deep_documents": [
                    _document("1978_ZQ_001", "drunken-master", pdf_paths[0]),
                    _document("1982_ZJPD_001", "aces-go-places", pdf_paths[1]),
                ],
            },
            allow_unicode=True,
            sort_keys=False,
        ),
        encoding="utf-8",
    )
    return RagSource(root=root, config=config, pdf_paths=pdf_paths)


def test_bundle_is_parent_bound_and_byte_deterministic(
    rag_source: RagSource, tmp_path: Path
) -> None:
    first = build_rag_bundle(rag_source.root, rag_source.config, tmp_path / "one")
    second = build_rag_bundle(rag_source.root, rag_source.config, tmp_path / "two")
    assert first.movie_count == 3
    assert first.metadata_passage_count == 3
    assert first.document_count == 2
    assert first.pdf_passage_count == 2
    assert first.bundle_sha256 == second.bundle_sha256
    assert first.manifest_bytes == second.manifest_bytes


def test_v13_bundle_merges_one_auditable_supplemental_movie_into_normal_records(
    rag_source: RagSource, tmp_path: Path
) -> None:
    """Breaks if an approved overlay is omitted or routed through a runtime-only side path."""
    _configure_v13_with_supplemental_movie(rag_source)

    result = build_rag_bundle(
        rag_source.root,
        rag_source.config,
        tmp_path / "r3",
        document_source_root=rag_source.root / "deep",
    )
    manifest = json.loads(result.manifest_path.read_text(encoding="utf-8"))
    records = [
        json.loads(line)
        for line in result.bundle_path.read_text(encoding="utf-8").splitlines()
    ]

    assert result.movie_count == 4
    assert result.metadata_passage_count == 4
    assert result.poster_row_count == 4
    assert result.approved_poster_object_count == 3
    assert result.facet_count == 4
    assert result.tier_s_count == 2
    assert manifest["parent_rag_release_id"] == "vtest-demo-r2"
    assert manifest["supplemental_movies"] == [_supplemental_movie()]
    assert manifest["supplemental_facets"] == [_supplemental_facet()]
    assert manifest["supplemental_posters"] == [_supplemental_poster()]
    assert any(
        record.get("record_kind") == "passage"
        and record.get("passage_id") == "metadata:2001_SLZQ_001"
        and "chinese_title: 少林足球" in str(record.get("body"))
        for record in records
    )


def test_v13_bundle_rejects_supplemental_movie_id_collision(
    rag_source: RagSource, tmp_path: Path
) -> None:
    """Breaks if an overlay can silently replace a governed parent movie."""
    _configure_v13_with_supplemental_movie(rag_source)
    config = yaml.safe_load(rag_source.config.read_text(encoding="utf-8"))
    config["supplemental_movies"][0]["movie_id"] = "1978_ZQ_001"
    config["supplemental_facets"][0]["movie_id"] = "1978_ZQ_001"
    config["supplemental_posters"][0]["movie_id"] = "1978_ZQ_001"
    rag_source.config.write_text(
        yaml.safe_dump(config, allow_unicode=True, sort_keys=False), encoding="utf-8"
    )

    with pytest.raises(RagBundleError, match="supplemental movie ID collides"):
        build_rag_bundle(
            rag_source.root,
            rag_source.config,
            tmp_path / "out",
            document_source_root=rag_source.root / "deep",
        )


def test_prepare_pdf_binding_emits_config_ready_identity_without_mutation(
    rag_source: RagSource,
) -> None:
    """Breaks if reusable intake mutates inputs or guesses a governed identity."""
    _make_reusable_config(rag_source)
    path = rag_source.root / "deep" / "new-analysis.pdf"
    _write_pdf_pages(path, ("page one", "page two", None))
    observed_before = {
        candidate: candidate.read_bytes()
        for candidate in (
            rag_source.config,
            path,
            rag_source.root / "data/release/vtest/release_manifest.json",
            rag_source.root / "data/release/vtest/movies.csv",
        )
    }

    prepared = prepare_pdf_binding(
        rag_source.root,
        rag_source.config,
        rag_source.root / "deep",
        movie_id="1983_TS_001",
        document_id="test-film-deep-analysis-v1",
        source_filename=path.name,
        rights_status="restricted",
        quality_status="manual_approved",
    )

    assert prepared.to_dict() == {
        "binding": {
            "document_id": "test-film-deep-analysis-v1",
            "movie_id": "1983_TS_001",
            "page_count": 2,
            "quality_status": "manual_approved",
            "rights_status": "restricted",
            "source_filename": "new-analysis.pdf",
            "source_sha256": _sha256(path),
        },
        "next_counts": {
            "documents": 3,
            "expected_embeddings": 7,
            "pdf_passages": 4,
        },
        "parent_release_manifest_sha256": _sha256(
            rag_source.root / "data/release/vtest/release_manifest.json"
        ),
        "schema_version": "rag-pdf-binding-preflight/v1",
        "text_extraction_profile": "cjk-layout-v1",
        "valid": True,
    }
    assert {
        candidate: candidate.read_bytes() for candidate in observed_before
    } == observed_before


@pytest.mark.parametrize(
    ("movie_id", "document_id", "source_filename", "error"),
    (
        ("UNKNOWN", "new-analysis", "new.pdf", "movie is not in the Release"),
        (
            "1983_TS_001",
            "drunken-master",
            "new.pdf",
            "duplicate document_id",
        ),
        (
            "1983_TS_001",
            "new-analysis",
            "drunken.pdf",
            "duplicate source binding",
        ),
        (
            "1983_TS_001",
            "new-analysis",
            "not-a-pdf.txt",
            "direct filename",
        ),
    ),
)
def test_prepare_pdf_binding_rejects_invalid_or_duplicate_identity(
    rag_source: RagSource,
    movie_id: str,
    document_id: str,
    source_filename: str,
    error: str,
) -> None:
    _make_reusable_config(rag_source)
    path = rag_source.root / "deep" / source_filename
    if not path.exists():
        _write_one_page_pdf(path, "new page")

    with pytest.raises(RagBundleError, match=error):
        prepare_pdf_binding(
            rag_source.root,
            rag_source.config,
            rag_source.root / "deep",
            movie_id=movie_id,
            document_id=document_id,
            source_filename=source_filename,
            rights_status="restricted",
            quality_status="manual_approved",
        )


@pytest.mark.parametrize(
    "page_texts",
    ((None,), (None, "page two"), ("page one", None, "page three")),
)
def test_prepare_pdf_binding_rejects_non_contiguous_extracted_pages(
    rag_source: RagSource,
    page_texts: tuple[str | None, ...],
) -> None:
    _make_reusable_config(rag_source)
    path = rag_source.root / "deep" / "non-contiguous.pdf"
    _write_pdf_pages(path, page_texts)

    with pytest.raises(RagBundleError, match="contiguous non-empty pages"):
        prepare_pdf_binding(
            rag_source.root,
            rag_source.config,
            rag_source.root / "deep",
            movie_id="1983_TS_001",
            document_id="test-film-deep-analysis-v1",
            source_filename=path.name,
            rights_status="restricted",
            quality_status="manual_approved",
        )


def test_prepare_pdf_binding_rejects_duplicate_pdf_content(
    rag_source: RagSource,
) -> None:
    _make_reusable_config(rag_source)
    path = rag_source.root / "deep" / "renamed-copy.pdf"
    path.write_bytes(rag_source.pdf_paths[0].read_bytes())

    with pytest.raises(RagBundleError, match="duplicate source binding"):
        prepare_pdf_binding(
            rag_source.root,
            rag_source.config,
            rag_source.root / "deep",
            movie_id="1983_TS_001",
            document_id="test-film-deep-analysis-v1",
            source_filename=path.name,
            rights_status="restricted",
            quality_status="manual_approved",
        )


def test_prepare_pdf_binding_rejects_pdf_changed_during_extraction(
    rag_source: RagSource,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    _make_reusable_config(rag_source)
    path = rag_source.root / "deep" / "changing.pdf"
    _write_one_page_pdf(path, "stable before extraction")
    extract = rag_bundle.extract_pdf_pages

    def changing_extract(candidate: Path, *, profile: str):
        pages = extract(candidate, profile=profile)
        candidate.write_bytes(candidate.read_bytes() + b"\n")
        return pages

    monkeypatch.setattr(rag_bundle, "extract_pdf_pages", changing_extract)

    with pytest.raises(RagBundleError, match="changed during extraction"):
        prepare_pdf_binding(
            rag_source.root,
            rag_source.config,
            rag_source.root / "deep",
            movie_id="1983_TS_001",
            document_id="test-film-deep-analysis-v1",
            source_filename=path.name,
            rights_status="restricted",
            quality_status="manual_approved",
        )


def test_prepare_pdf_binding_rejects_schema_1_2_extraction_profile_drift(
    rag_source: RagSource,
) -> None:
    _make_reusable_config(rag_source)
    config = yaml.safe_load(rag_source.config.read_text(encoding="utf-8"))
    config["text_extraction_profile"] = "historical-v1"
    rag_source.config.write_text(
        yaml.safe_dump(config, allow_unicode=True, sort_keys=False),
        encoding="utf-8",
    )
    path = rag_source.root / "deep" / "wrong-profile.pdf"
    _write_one_page_pdf(path, "wrong profile")

    with pytest.raises(RagBundleError, match="schema 1.2 extraction profile"):
        prepare_pdf_binding(
            rag_source.root,
            rag_source.config,
            rag_source.root / "deep",
            movie_id="1983_TS_001",
            document_id="test-film-deep-analysis-v1",
            source_filename=path.name,
            rights_status="restricted",
            quality_status="manual_approved",
        )


def test_bundle_includes_one_authoritative_facet_per_movie(
    rag_source: RagSource, tmp_path: Path
) -> None:
    """Breaks if declared tier/pilot evidence is omitted or projected to the wrong movie."""
    result = build_rag_bundle(rag_source.root, rag_source.config, tmp_path / "out")
    facets = [
        record
        for record in verify_rag_bundle(result.manifest_path).iter_records()
        if record["record_kind"] == "facet"
    ]

    assert result.facet_count == 3
    assert (result.tier_s_count, result.tier_a_count, result.tier_b_count) == (1, 1, 1)
    assert result.pilot_count == 1
    assert [record["movie_id"] for record in facets] == [
        "1978_ZQ_001",
        "1982_ZJPD_001",
        "1983_TS_001",
    ]
    assert facets[1]["tier"] == "A"
    assert facets[1]["pilot_movie"] is True
    assert facets[1]["pilot_evidence"] == {
        "evidence_note": "fixture pilot evidence",
        "evidence_url": "https://example.test/pilot/1982_ZJPD_001",
        "handbook_genre": "喜劇",
        "source_record_id": "fixture-pilot-1",
    }
    facet_body = {
        key: value
        for key, value in facets[1].items()
        if key not in {"record_kind", "content_sha256"}
    }
    assert facets[1]["content_sha256"] == hashlib.sha256(
        json.dumps(
            facet_body, ensure_ascii=False, sort_keys=True, separators=(",", ":")
        ).encode("utf-8")
    ).hexdigest()
    manifest = json.loads(result.manifest_path.read_text(encoding="utf-8"))
    assert manifest["facet_artifacts"] == {
        "movie_tiers": {
            "relative_path": "data/release/vtest/movie_tiers.parquet",
            "sha256": _sha256(
                rag_source.root / "data/release/vtest/movie_tiers.parquet"
            ),
            "row_count": 3,
        },
        "pilot_movies": {
            "relative_path": "data/release/vtest/pilot_movies.parquet",
            "sha256": _sha256(
                rag_source.root / "data/release/vtest/pilot_movies.parquet"
            ),
            "row_count": 1,
        },
    }


def test_bundle_rejects_tier_or_pilot_membership_drift(
    rag_source: RagSource, tmp_path: Path
) -> None:
    """Breaks if either authoritative child stops covering the exact movie population."""
    _rewrite_tier_fixture_without_last_movie(rag_source)

    with pytest.raises(RagBundleError, match="facet movie membership"):
        build_rag_bundle(rag_source.root, rag_source.config, tmp_path / "out")


def test_bundle_rejects_pilot_agreement_drift(
    rag_source: RagSource, tmp_path: Path
) -> None:
    """Breaks if the tier boolean and pilot child artifact disagree about pilot membership."""
    _rewrite_tier_fixture_with_unflagged_pilot(rag_source)

    with pytest.raises(RagBundleError, match="facet pilot membership"):
        build_rag_bundle(rag_source.root, rag_source.config, tmp_path / "out")


def test_verifier_rejects_facet_artifact_count_drift(
    rag_source: RagSource, tmp_path: Path
) -> None:
    """Breaks if a manifest can claim facet evidence from a differently sized child artifact."""
    result = build_rag_bundle(rag_source.root, rag_source.config, tmp_path / "out")
    manifest = json.loads(result.manifest_path.read_text(encoding="utf-8"))
    manifest["facet_artifacts"]["movie_tiers"]["row_count"] = 2
    result.manifest_path.write_text(
        json.dumps(manifest, ensure_ascii=False, sort_keys=True, separators=(",", ":")) + "\n",
        encoding="utf-8",
    )

    with pytest.raises(RagBundleError, match="facet artifact counts"):
        verify_rag_bundle(result.manifest_path)


def test_bundle_derives_one_primary_rag_poster_per_movie_and_preserves_source_state(
    rag_source: RagSource, tmp_path: Path
) -> None:
    """Breaks if Phase 1's non-primary state is copied into the one-row-per-movie RAG join."""
    result = build_rag_bundle(rag_source.root, rag_source.config, tmp_path / "out")
    records = [json.loads(line) for line in result.bundle_path.read_text(encoding="utf-8").splitlines()]
    posters = [record for record in records if record["record_kind"] == "poster"]
    manifest = json.loads(result.manifest_path.read_text(encoding="utf-8"))

    assert len(posters) == 3
    assert all(record["is_primary"] is True for record in posters)
    assert all(record["source_is_primary"] is False for record in posters)
    assert all(record["rag_primary_policy"] == "one_state_row_per_movie" for record in posters)
    approved = [record for record in posters if record["quality_status"] == "machine_passed"]
    unavailable = [record for record in posters if record["quality_status"] == "missing"]
    assert len(approved) == 2
    assert len(unavailable) == 1
    for record in approved:
        path = rag_source.root / str(record["derived_object_uri"])
        assert record["content_sha256"] == "1" * 64
        assert record["derived_content_sha256"] == _sha256(path)
        assert record["derived_content_sha256"] != record["content_sha256"]
        assert record["derived_byte_length"] == path.stat().st_size
        assert record["derived_mime_type"] == "image/webp"
    assert all("derived_content_sha256" not in record for record in unavailable)
    assert all("derived_byte_length" not in record for record in unavailable)
    assert all("derived_mime_type" not in record for record in unavailable)
    assert manifest["counts"]["primary_poster_rows"] == 3
    assert manifest["counts"]["approved_poster_objects"] == 2
    assert manifest["counts"]["unavailable_poster_rows"] == 1
    assert manifest["counts"]["derived_poster_bytes"] == sum(
        int(record["derived_byte_length"]) for record in approved
    )
    assert manifest["poster_derivation"]["derived_is_primary"] is True
    assert manifest["poster_derivation"]["mode"] == "one_state_row_per_movie"
    assert manifest["poster_derivation"]["source_is_primary_field"] == "source_is_primary"
    assert manifest["poster_derivation"]["derived_inventory_sha256"] == _inventory_sha256(
        approved
    )

    verified = verify_rag_bundle(result.manifest_path)
    assert verified.poster_row_count == 3
    assert verified.primary_poster_row_count == 3


def test_bundle_rejects_missing_or_non_webp_approved_derivative(
    rag_source: RagSource, tmp_path: Path
) -> None:
    derived = rag_source.root / "assets/posters/derived/1978_ZQ_001.webp"
    derived.write_bytes(b"not-a-webp")

    with pytest.raises(RagBundleError, match="derived poster is not WebP"):
        build_rag_bundle(rag_source.root, rag_source.config, tmp_path / "out")


def test_bundle_rejects_missing_approved_derivative(
    rag_source: RagSource, tmp_path: Path
) -> None:
    (rag_source.root / "assets/posters/derived/1978_ZQ_001.webp").unlink()

    with pytest.raises(RagBundleError, match="approved derived poster is missing"):
        build_rag_bundle(rag_source.root, rag_source.config, tmp_path / "out")


def test_bundle_poster_authority_rejects_declared_size_above_serving_ceiling() -> None:
    record: dict[str, object] = {
        "record_kind": "poster",
        "movie_id": "1978_ZQ_001",
        "is_primary": True,
        "source_is_primary": False,
        "rag_primary_policy": "one_state_row_per_movie",
        "quality_status": "machine_passed",
        "content_sha256": "a" * 64,
        "derived_object_uri": "assets/posters/derived/1978_ZQ_001.webp",
        "derived_content_sha256": "b" * 64,
        "derived_byte_length": MAX_POSTER_BYTES + 1,
        "derived_mime_type": "image/webp",
    }

    with pytest.raises(RagBundleError, match="derived identity"):
        rag_bundle._require_poster_derivation((record,))


def test_bundle_builder_rejects_oversized_poster_before_unbounded_hashing(
    tmp_path: Path,
) -> None:
    root = tmp_path / "source"
    derived = root / "assets/posters/derived/1978_ZQ_001.webp"
    derived.parent.mkdir(parents=True)
    size = MAX_POSTER_BYTES + 1
    with derived.open("wb") as stream:
        stream.write(b"RIFF" + (size - 8).to_bytes(4, "little") + b"WEBP")
        stream.truncate(size)
    poster = {
        "quality_status": "machine_passed",
        "derived_object_uri": "assets/posters/derived/1978_ZQ_001.webp",
        "content_sha256": "a" * 64,
    }

    with pytest.raises(RagBundleError, match="byte length"):
        rag_bundle._derived_poster_identity(root, "1978_ZQ_001", poster)


def test_verifier_rejects_self_consistent_false_source_primary_provenance(
    rag_source: RagSource, tmp_path: Path
) -> None:
    result = build_rag_bundle(rag_source.root, rag_source.config, tmp_path / "out")
    records = [json.loads(line) for line in result.bundle_path.read_text(encoding="utf-8").splitlines()]
    poster = next(record for record in records if record["record_kind"] == "poster")
    poster["source_is_primary"] = True
    _rewrite_bundle_and_hash(result.manifest_path, result.bundle_path, records)

    with pytest.raises(RagBundleError, match="poster primary derivation is invalid"):
        verify_rag_bundle(result.manifest_path)


def test_verifier_rejects_self_consistent_noncanonical_approved_identity(
    rag_source: RagSource, tmp_path: Path
) -> None:
    result = build_rag_bundle(rag_source.root, rag_source.config, tmp_path / "out")
    records = [json.loads(line) for line in result.bundle_path.read_text(encoding="utf-8").splitlines()]
    approved = next(
        record
        for record in records
        if record.get("record_kind") == "poster"
        and record.get("quality_status") == "machine_passed"
    )
    approved["derived_object_uri"] = "assets/posters/derived/other.webp"
    _rewrite_bundle_and_hash(result.manifest_path, result.bundle_path, records)

    with pytest.raises(RagBundleError, match="approved poster derived identity is invalid"):
        verify_rag_bundle(result.manifest_path)


def test_verifier_rejects_self_consistent_unavailable_derived_identity(
    rag_source: RagSource, tmp_path: Path
) -> None:
    result = build_rag_bundle(rag_source.root, rag_source.config, tmp_path / "out")
    records = [json.loads(line) for line in result.bundle_path.read_text(encoding="utf-8").splitlines()]
    unavailable = next(
        record
        for record in records
        if record.get("record_kind") == "poster" and record.get("quality_status") == "missing"
    )
    unavailable["derived_content_sha256"] = "f" * 64
    _rewrite_bundle_and_hash(result.manifest_path, result.bundle_path, records)

    with pytest.raises(RagBundleError, match="unavailable poster has derived identity"):
        verify_rag_bundle(result.manifest_path)


def test_verifier_enforces_literal_v12_poster_semantics_even_if_manifest_is_self_consistent(
    rag_source: RagSource, tmp_path: Path
) -> None:
    result = build_rag_bundle(rag_source.root, rag_source.config, tmp_path / "out")
    manifest = json.loads(result.manifest_path.read_text(encoding="utf-8"))
    manifest["parent_release_manifest_sha256"] = (
        "e9842db33208e7146d78d86689b13f81fffcf41288d79d84e599b93050445e2c"
    )
    manifest["rag_release_id"] = "v1.2-demo"
    result.manifest_path.write_text(
        json.dumps(manifest, ensure_ascii=False, sort_keys=True, separators=(",", ":")) + "\n",
        encoding="utf-8",
    )

    with pytest.raises(RagBundleError, match="v1.2 poster semantics mismatch"):
        verify_rag_bundle(result.manifest_path)


def test_verifier_rejects_v12_release_id_with_a_self_consistent_different_parent(
    rag_source: RagSource, tmp_path: Path
) -> None:
    result = build_rag_bundle(rag_source.root, rag_source.config, tmp_path / "out")
    manifest = json.loads(result.manifest_path.read_text(encoding="utf-8"))
    manifest["rag_release_id"] = "v1.2-demo"
    result.manifest_path.write_text(
        json.dumps(manifest, ensure_ascii=False, sort_keys=True, separators=(",", ":")) + "\n",
        encoding="utf-8",
    )

    with pytest.raises(RagBundleError, match="v1.2 RAG release parent binding mismatch"):
        verify_rag_bundle(result.manifest_path)


def test_schema_v12_child_release_can_bind_the_governed_v12_parent(
    rag_source: RagSource, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    document_root = _configure_v12(rag_source, document_count=2)
    config = yaml.safe_load(rag_source.config.read_text(encoding="utf-8"))
    parent_sha256 = config["parent_release_manifest_sha256"]
    monkeypatch.setattr(rag_bundle, "_V12_PARENT_MANIFEST_SHA256", parent_sha256)
    config["rag_release_id"] = "v1.2-demo-r2"
    rag_source.config.write_text(
        yaml.safe_dump(config, allow_unicode=True, sort_keys=False), encoding="utf-8"
    )

    result = build_rag_bundle(
        rag_source.root,
        rag_source.config,
        tmp_path / "out",
        document_source_root=document_root,
    )
    verified = verify_rag_bundle(result.manifest_path)

    assert verified.contract.rag_release_id == "v1.2-demo-r2"
    assert verified.contract.parent_release_manifest_sha256 == parent_sha256


def test_builder_rejects_configured_poster_semantic_split_drift(
    rag_source: RagSource, tmp_path: Path
) -> None:
    config = yaml.safe_load(rag_source.config.read_text(encoding="utf-8"))
    config["expected_approved_poster_objects"] = 1
    config["expected_unavailable_poster_rows"] = 2
    rag_source.config.write_text(
        yaml.safe_dump(config, allow_unicode=True, sort_keys=False), encoding="utf-8"
    )

    with pytest.raises(RagBundleError, match="poster semantic counts do not match configuration"):
        build_rag_bundle(rag_source.root, rag_source.config, tmp_path / "out")


def test_builder_rejects_configured_derived_inventory_drift(
    rag_source: RagSource, tmp_path: Path
) -> None:
    config = yaml.safe_load(rag_source.config.read_text(encoding="utf-8"))
    config["expected_derived_inventory_sha256"] = "0" * 64
    rag_source.config.write_text(
        yaml.safe_dump(config, allow_unicode=True, sort_keys=False), encoding="utf-8"
    )

    with pytest.raises(RagBundleError, match="derived poster inventory does not match configuration"):
        build_rag_bundle(rag_source.root, rag_source.config, tmp_path / "out")


def test_packaged_rag_manifest_schema_matches_repository_schema() -> None:
    """Breaks if an installed wheel cannot execute bundle build/verify validation."""
    packaged = files("hk_movie_rag").joinpath(
        "schemas", "rag_release_manifest.schema.json"
    ).read_text(encoding="utf-8")
    repository = (
        Path(__file__).resolve().parents[1] / "schemas/rag_release_manifest.schema.json"
    ).read_text(encoding="utf-8")
    assert packaged == repository


def test_packaged_rag_manifest_v1_2_schema_matches_repository_schema() -> None:
    """Breaks if an installed wheel cannot verify the reusable manifest contract."""
    packaged = files("hk_movie_rag").joinpath(
        "schemas", "rag_release_manifest_v1_2.schema.json"
    ).read_text(encoding="utf-8")
    repository = (
        Path(__file__).resolve().parents[1]
        / "schemas/rag_release_manifest_v1_2.schema.json"
    ).read_text(encoding="utf-8")
    assert packaged == repository


def test_bundle_rejects_changed_pdf_before_extraction(
    rag_source: RagSource, tmp_path: Path
) -> None:
    rag_source.pdf_paths[0].write_bytes(b"changed")
    with pytest.raises(RagBundleError, match="PDF SHA-256 mismatch"):
        build_rag_bundle(rag_source.root, rag_source.config, tmp_path / "out")


def test_bundle_never_discovers_a_third_pdf(rag_source: RagSource, tmp_path: Path) -> None:
    (rag_source.root / "undeclared.pdf").write_bytes(rag_source.pdf_paths[0].read_bytes())
    result = build_rag_bundle(rag_source.root, rag_source.config, tmp_path / "out")
    assert result.document_count == 2


def test_schema_1_2_builds_positive_dynamic_document_contract(
    rag_source: RagSource, tmp_path: Path
) -> None:
    """Breaks if schema 1.2 remains hard-coded to two documents or omits release identity."""
    document_root = _configure_v12(rag_source, document_count=1)

    result = build_rag_bundle(
        rag_source.root,
        rag_source.config,
        tmp_path / "out",
        document_source_root=document_root,
    )
    verified = verify_rag_bundle(result.manifest_path)

    assert result.document_count == 1
    assert result.pdf_passage_count == 1
    assert verified.contract.counts.documents == 1
    assert verified.contract.counts.pdf_passages == 1
    assert verified.contract.expected_embeddings == 4
    assert verified.contract.manifest_sha256 == _sha256(result.manifest_path)
    assert verified.contract.bundle_sha256 == result.bundle_sha256
    assert verified.contract.derived_inventory_sha256 == result.derived_inventory_sha256
    assert verified.contract.text_extraction_profile == "cjk-layout-v1"
    assert verified.contract.document_embedding_profile == "vertex-title-text-v1"
    assert verified.contract.relevance_policy_sha256 == "a" * 64
    assert verified.contract.poster_authority_sha256 == "b" * 64


def test_schema_1_2_accepts_blank_physical_pages_only_after_declared_range(
    rag_source: RagSource, tmp_path: Path
) -> None:
    """Breaks if a trailing blank sheet invalidates a contiguous logical document."""
    document_root = _configure_v12(rag_source, document_count=1)
    _write_pdf_pages(rag_source.pdf_paths[0], ("page one", "page two", None))
    _rebind_document(rag_source, page_count=2)

    result = build_rag_bundle(
        rag_source.root,
        rag_source.config,
        tmp_path / "out",
        document_source_root=document_root,
    )
    records = [
        json.loads(line)
        for line in result.bundle_path.read_text(encoding="utf-8").splitlines()
    ]

    assert [
        record["page_number"]
        for record in records
        if record.get("passage_kind") == "pdf"
    ] == [1, 2]


@pytest.mark.parametrize(
    ("page_texts", "declared_page_count"),
    [
        (("page one", None, "page three"), 2),
        (("page one", None), 2),
        (("page one", "page two", "page three"), 2),
    ],
    ids=("internal-blank", "missing-declared-page", "later-nonempty-page"),
)
def test_schema_1_2_rejects_physical_pages_outside_exact_declared_range(
    rag_source: RagSource,
    tmp_path: Path,
    page_texts: tuple[str | None, ...],
    declared_page_count: int,
) -> None:
    """Breaks if count-only validation accepts gaps or undeclared later content."""
    document_root = _configure_v12(rag_source, document_count=1)
    _write_pdf_pages(rag_source.pdf_paths[0], page_texts)
    _rebind_document(rag_source, page_count=declared_page_count)

    with pytest.raises(RagBundleError, match="exact declared range"):
        build_rag_bundle(
            rag_source.root,
            rag_source.config,
            tmp_path / "out",
            document_source_root=document_root,
        )


@pytest.mark.parametrize(
    ("mutation", "message"),
    [
        (lambda docs: docs.__setitem__(1, {**docs[1], "document_id": docs[0]["document_id"]}), "duplicate document_id"),
        (lambda docs: docs.__setitem__(1, {**docs[1], "source_filename": docs[0]["source_filename"]}), "duplicate source binding"),
        (
            lambda docs: docs.__setitem__(0, {**docs[0], "page_count": 2}),
            "exact declared range",
        ),
    ],
)
def test_schema_1_2_rejects_invalid_document_declarations(
    rag_source: RagSource,
    tmp_path: Path,
    mutation: object,
    message: str,
) -> None:
    """Breaks if declarations can alias identities/sources or drift from extracted pages."""
    document_root = _configure_v12(rag_source, document_count=2)
    config = yaml.safe_load(rag_source.config.read_text(encoding="utf-8"))
    mutation(config["deep_documents"])
    rag_source.config.write_text(
        yaml.safe_dump(config, allow_unicode=True, sort_keys=False), encoding="utf-8"
    )

    with pytest.raises(RagBundleError, match=message):
        build_rag_bundle(
            rag_source.root,
            rag_source.config,
            tmp_path / "out",
            document_source_root=document_root,
        )


@pytest.mark.parametrize("unsafe_name", ["deep/drunken.pdf", "../drunken.pdf"])
def test_schema_1_2_document_sources_must_be_direct_children(
    rag_source: RagSource, tmp_path: Path, unsafe_name: str
) -> None:
    """Breaks if a declaration can traverse or introduce a nested discovery surface."""
    document_root = _configure_v12(rag_source, document_count=1)
    config = yaml.safe_load(rag_source.config.read_text(encoding="utf-8"))
    config["deep_documents"][0]["source_filename"] = unsafe_name
    rag_source.config.write_text(
        yaml.safe_dump(config, allow_unicode=True, sort_keys=False), encoding="utf-8"
    )

    with pytest.raises(RagBundleError, match="direct filename"):
        build_rag_bundle(
            rag_source.root,
            rag_source.config,
            tmp_path / "out",
            document_source_root=document_root,
        )


def test_schema_1_2_document_source_rejects_symlink_escape(
    rag_source: RagSource, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """Breaks if a direct-looking filename can escape its governed root through a symlink."""
    document_root = _configure_v12(rag_source, document_count=1)
    declared_path = rag_source.pdf_paths[0]
    original_is_symlink = Path.is_symlink
    monkeypatch.setattr(
        Path,
        "is_symlink",
        lambda path: path == declared_path or original_is_symlink(path),
    )

    with pytest.raises(RagBundleError, match="not a regular file"):
        build_rag_bundle(
            rag_source.root,
            rag_source.config,
            tmp_path / "out",
            document_source_root=document_root,
        )


@pytest.mark.parametrize(
    ("mutate_records", "message"),
    [
        (
            lambda records: next(
                record for record in records if record.get("passage_kind") == "pdf"
            ).update({"passage_id": "metadata:1978_ZQ_001"}),
            "duplicate passage_id",
        ),
        (
            lambda records: next(
                record for record in records if record.get("passage_kind") == "pdf"
            ).update({"page_number": 0}),
            "contiguous positive pages",
        ),
        (
            lambda records: next(
                record for record in records if record.get("passage_kind") == "pdf"
            ).update({"page_number": 2, "passage_id": "pdf:drunken-master:p2"}),
            "contiguous positive pages",
        ),
        (
            lambda records: next(
                record for record in records if record.get("passage_kind") == "pdf"
            ).update({"body": "", "content_sha256": hashlib.sha256(b"").hexdigest()}),
            "empty PDF page",
        ),
    ],
)
def test_schema_1_2_verifier_rejects_invalid_page_contract(
    rag_source: RagSource,
    tmp_path: Path,
    mutate_records: object,
    message: str,
) -> None:
    """Breaks if verified page identity, numbering, or content can drift from the manifest."""
    document_root = _configure_v12(rag_source, document_count=1)
    result = build_rag_bundle(
        rag_source.root,
        rag_source.config,
        tmp_path / "out",
        document_source_root=document_root,
    )
    records = list(verify_rag_bundle(result.manifest_path).iter_records())
    mutate_records(records)
    _rewrite_bundle_identity(result.manifest_path, result.bundle_path, records)

    with pytest.raises(RagBundleError, match=message):
        verify_rag_bundle(result.manifest_path)


@pytest.mark.parametrize(
    ("field", "replacement"),
    [
        ("movie_id", "1982_ZJPD_001"),
        ("source_filename", "other.pdf"),
        ("source_sha256", "9" * 64),
        ("rights_status", "cleared"),
        ("quality_status", "machine_passed"),
        ("page_count", 2),
    ],
)
def test_schema_1_2_verifier_binds_document_records_to_manifest(
    rag_source: RagSource,
    tmp_path: Path,
    field: str,
    replacement: object,
) -> None:
    """Breaks if a bundle document record can drift from its verified declaration."""
    document_root = _configure_v12(rag_source, document_count=1)
    result = build_rag_bundle(
        rag_source.root,
        rag_source.config,
        tmp_path / "out",
        document_source_root=document_root,
    )
    records = list(verify_rag_bundle(result.manifest_path).iter_records())
    document = next(record for record in records if record["record_kind"] == "document")
    document[field] = replacement
    _rewrite_bundle_identity(result.manifest_path, result.bundle_path, records)

    with pytest.raises(
        RagBundleError, match="bundle document bindings do not match manifest"
    ):
        verify_rag_bundle(result.manifest_path)


def test_schema_1_2_verifier_rejects_boolean_document_page_count(
    rag_source: RagSource, tmp_path: Path
) -> None:
    """Breaks if JSON true can impersonate integer page_count=1 during binding checks."""
    document_root = _configure_v12(rag_source, document_count=1)
    result = build_rag_bundle(
        rag_source.root,
        rag_source.config,
        tmp_path / "out",
        document_source_root=document_root,
    )
    records = list(verify_rag_bundle(result.manifest_path).iter_records())
    document = next(record for record in records if record["record_kind"] == "document")
    document["page_count"] = True
    _rewrite_bundle_identity(result.manifest_path, result.bundle_path, records)

    with pytest.raises(
        RagBundleError, match="bundle document bindings do not match manifest"
    ):
        verify_rag_bundle(result.manifest_path)


def test_verifier_hashes_the_exact_manifest_bytes_it_parses(
    rag_source: RagSource,
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Breaks if replacement after parsing can change the returned manifest identity."""
    document_root = _configure_v12(rag_source, document_count=1)
    result = build_rag_bundle(
        rag_source.root,
        rag_source.config,
        tmp_path / "out",
        document_source_root=document_root,
    )
    original_manifest = result.manifest_path.read_bytes()
    original_load_json = rag_bundle._load_json
    replaced = False

    def replace_after_parse(text: str, label: str) -> dict[str, object]:
        nonlocal replaced
        value = original_load_json(text, label)
        if label == "RAG release manifest" and not replaced:
            replaced = True
            result.manifest_path.write_bytes(b'{"replacement":true}\n')
        return value

    monkeypatch.setattr(rag_bundle, "_load_json", replace_after_parse)

    verified = verify_rag_bundle(result.manifest_path)

    assert replaced is True
    assert verified.contract.manifest_sha256 == hashlib.sha256(original_manifest).hexdigest()


@pytest.mark.parametrize(
    ("section", "field", "value", "message"),
    [
        ("counts", "documents", 2, "manifest document count mismatch"),
        ("counts", "pdf_passages", 2, "PDF passage count"),
        ("documents", "page_count", 2, "PDF passage count"),
    ],
)
def test_schema_1_2_verifier_rejects_manifest_count_drift(
    rag_source: RagSource,
    tmp_path: Path,
    section: str,
    field: str,
    value: int,
    message: str,
) -> None:
    """Breaks if manifest counts can disagree with declarations or expected embeddings."""
    document_root = _configure_v12(rag_source, document_count=1)
    result = build_rag_bundle(
        rag_source.root,
        rag_source.config,
        tmp_path / "out",
        document_source_root=document_root,
    )
    manifest = json.loads(result.manifest_path.read_text(encoding="utf-8"))
    if section == "documents":
        manifest["documents"][0][field] = value
    else:
        manifest[section][field] = value
    result.manifest_path.write_text(
        json.dumps(manifest, ensure_ascii=False, sort_keys=True, separators=(",", ":")) + "\n",
        encoding="utf-8",
    )

    with pytest.raises(RagBundleError, match=message):
        verify_rag_bundle(result.manifest_path)


def _configure_v12(rag_source: RagSource, *, document_count: int) -> Path:
    document_root = rag_source.root / "deep"
    config = yaml.safe_load(rag_source.config.read_text(encoding="utf-8"))
    config.update(
        {
            "schema_version": "1.2",
            "text_extraction_profile": "cjk-layout-v1",
            "document_embedding_profile": "vertex-title-text-v1",
            "relevance_policy_sha256": "a" * 64,
            "poster_authority_sha256": "b" * 64,
        }
    )
    documents = config["deep_documents"][:document_count]
    for document in documents:
        document["source_filename"] = Path(document["source_filename"]).name
        document["page_count"] = 1
    config["deep_documents"] = documents
    rag_source.config.write_text(
        yaml.safe_dump(config, allow_unicode=True, sort_keys=False), encoding="utf-8"
    )
    return document_root


def _rebind_document(rag_source: RagSource, *, page_count: int) -> None:
    config = yaml.safe_load(rag_source.config.read_text(encoding="utf-8"))
    config["deep_documents"][0]["source_sha256"] = _sha256(rag_source.pdf_paths[0])
    config["deep_documents"][0]["page_count"] = page_count
    rag_source.config.write_text(
        yaml.safe_dump(config, allow_unicode=True, sort_keys=False), encoding="utf-8"
    )


def _rewrite_bundle_identity(
    manifest_path: Path, bundle_path: Path, records: list[dict[str, object]]
) -> None:
    bundle_bytes = b"".join(
        json.dumps(record, ensure_ascii=False, sort_keys=True, separators=(",", ":")).encode(
            "utf-8"
        )
        + b"\n"
        for record in records
    )
    bundle_path.write_bytes(bundle_bytes)
    manifest = json.loads(manifest_path.read_text(encoding="utf-8"))
    manifest["bundle"]["sha256"] = hashlib.sha256(bundle_bytes).hexdigest()
    manifest["bundle"]["size_bytes"] = len(bundle_bytes)
    manifest_path.write_text(
        json.dumps(manifest, ensure_ascii=False, sort_keys=True, separators=(",", ":")) + "\n",
        encoding="utf-8",
    )


def _document(movie_id: str, document_id: str, path: Path) -> dict[str, str]:
    return {
        "movie_id": movie_id,
        "document_id": document_id,
        "source_filename": path.relative_to(path.parents[1]).as_posix(),
        "source_sha256": _sha256(path),
        "rights_status": "restricted",
        "quality_status": "manual_approved",
    }


def _poster(movie_id: str, *, approved: bool = False) -> dict[str, object]:
    return {
        "asset_id": f"poster:{movie_id}:v1",
        "movie_id": movie_id,
        "asset_type": "poster",
        "source_object_uri": "",
        "original_object_uri": f"assets/posters/originals/{movie_id}.jpg" if approved else "",
        "derived_object_uri": f"assets/posters/derived/{movie_id}.webp" if approved else "",
        "quarantine_object_uri": "",
        "content_sha256": "1" * 64 if approved else "",
        "detected_format": "JPEG" if approved else "",
        "detected_mime_type": "image/jpeg" if approved else "",
        "detected_extension": ".jpg" if approved else "",
        "width": 2 if approved else None,
        "height": 2 if approved else None,
        "color_mode": "RGB" if approved else "",
        "byte_length": 123 if approved else None,
        "quality_status": "machine_passed" if approved else "missing",
        "identity_confidence": None,
        "source_url": None,
        "rights_status": "unknown",
        "version": 1,
        "is_primary": False,
        "publishable": False,
        "created_at": None,
        "reviewed_at": None,
    }


def _write_csv(path: Path, rows: list[dict[str, str]]) -> None:
    import csv

    with path.open("w", encoding="utf-8", newline="") as stream:
        writer = csv.DictWriter(stream, fieldnames=list(rows[0]))
        writer.writeheader()
        writer.writerows(rows)


def _write_one_page_pdf(path: Path, text: str) -> None:
    _write_pdf_pages(path, (text,))


def _write_pdf_pages(path: Path, page_texts: tuple[str | None, ...]) -> None:
    writer = PdfWriter()
    font = DictionaryObject(
        {
            NameObject("/Type"): NameObject("/Font"),
            NameObject("/Subtype"): NameObject("/Type1"),
            NameObject("/BaseFont"): NameObject("/Helvetica"),
        }
    )
    font_reference = writer._add_object(font)
    for text in page_texts:
        page = writer.add_blank_page(width=100, height=100)
        if text is None:
            continue
        page[NameObject("/Resources")] = DictionaryObject(
            {NameObject("/Font"): DictionaryObject({NameObject("/F1"): font_reference})}
        )
        contents = DecodedStreamObject()
        contents.set_data(f"BT /F1 12 Tf 10 50 Td ({text}) Tj ET".encode())
        page[NameObject("/Contents")] = writer._add_object(contents)
    with path.open("wb") as stream:
        writer.write(stream)


def _make_reusable_config(source: RagSource) -> None:
    config = yaml.safe_load(source.config.read_text(encoding="utf-8"))
    config["schema_version"] = "1.2"
    config["text_extraction_profile"] = "cjk-layout-v1"
    config["document_embedding_profile"] = "vertex-title-text-v1"
    config["relevance_policy_sha256"] = "a" * 64
    config["poster_authority_sha256"] = "b" * 64
    for document in config["deep_documents"]:
        document["page_count"] = 1
    source.config.write_text(
        yaml.safe_dump(config, allow_unicode=True, sort_keys=False),
        encoding="utf-8",
    )


def _configure_v13_with_supplemental_movie(source: RagSource) -> None:
    _make_reusable_config(source)
    supplemental_id = "2001_SLZQ_001"
    Image.new("RGB", (2, 2), (120, 80, 40)).save(
        source.root / "assets" / "posters" / "derived" / f"{supplemental_id}.webp",
        format="WEBP",
        lossless=True,
    )
    config = yaml.safe_load(source.config.read_text(encoding="utf-8"))
    for document in config["deep_documents"]:
        document["source_filename"] = Path(document["source_filename"]).name
    config.update(
        {
            "schema_version": "1.3",
            "rag_release_id": "vtest-demo-r3",
            "parent_rag_release_id": "vtest-demo-r2",
            "expected_movies": 4,
            "expected_poster_rows": 4,
            "expected_approved_poster_objects": 3,
            "expected_unavailable_poster_rows": 1,
            "expected_facet_count": 4,
            "expected_tier_s_count": 2,
            "supplemental_movies": [_supplemental_movie()],
            "supplemental_facets": [_supplemental_facet()],
            "supplemental_posters": [_supplemental_poster()],
        }
    )
    config["expected_derived_inventory_sha256"] = _fixture_inventory_sha256(
        source.root,
        {"1978_ZQ_001", "1982_ZJPD_001", supplemental_id},
    )
    source.config.write_text(
        yaml.safe_dump(config, allow_unicode=True, sort_keys=False), encoding="utf-8"
    )


def _supplemental_movie() -> dict[str, str]:
    return {
        "movie_id": "2001_SLZQ_001",
        "chinese_title": "少林足球",
        "english_title": "Shaolin Soccer",
        "release_date": "2001-07-12",
        "production_region": "香港",
        "director": "周星馳",
        "screenwriter": "周星馳、曾謹昌、馮勉恆、馮志強",
        "cast": "周星馳、趙薇、吳孟達、黃一飛",
        "genre": "動作、喜劇、運動",
        "runtime_minutes": "112",
        "production_company": "星輝海外有限公司、寰宇娛樂有限公司",
        "data_source": "Wikidata + 香港電影資料館 + R3 operator approval",
    }


def _supplemental_facet() -> dict[str, object]:
    return {
        "movie_id": "2001_SLZQ_001",
        "tier": "S",
        "tier_reason": "R3 operator-approved supplemental S-tier deep document",
        "human_review": "manual_approved",
        "pilot_movie": False,
        "pilot_evidence": None,
    }


def _supplemental_poster() -> dict[str, object]:
    return _poster("2001_SLZQ_001", approved=True)


def _rewrite_tier_fixture_without_last_movie(source: RagSource) -> None:
    path = source.root / "data/release/vtest/movie_tiers.parquet"
    table = pq.read_table(path)
    pq.write_table(pa.Table.from_pylist(table.to_pylist()[:-1], schema=table.schema), path)
    _rebind_parent_release_artifact(source, path.name)


def _rewrite_tier_fixture_with_unflagged_pilot(source: RagSource) -> None:
    path = source.root / "data/release/vtest/movie_tiers.parquet"
    table = pq.read_table(path)
    rows = table.to_pylist()
    for row in rows:
        if row["movie_id"] == "1982_ZJPD_001":
            row["pilot_movie"] = False
    pq.write_table(pa.Table.from_pylist(rows, schema=table.schema), path)
    _rebind_parent_release_artifact(source, path.name)


def _rebind_parent_release_artifact(source: RagSource, filename: str) -> None:
    manifest_path = source.root / "data/release/vtest/release_manifest.json"
    manifest = json.loads(manifest_path.read_text(encoding="utf-8"))
    artifact = next(
        item
        for item in manifest["artifacts"]
        if Path(str(item["relative_path"])).name == filename
    )
    artifact_path = source.root / str(artifact["relative_path"])
    artifact["row_count"] = pq.read_metadata(artifact_path).num_rows
    artifact["size_bytes"] = artifact_path.stat().st_size
    artifact["sha256"] = _sha256(artifact_path)
    manifest_path.write_text(
        json.dumps(manifest, ensure_ascii=False, sort_keys=True, separators=(",", ":")) + "\n",
        encoding="utf-8",
    )
    config = yaml.safe_load(source.config.read_text(encoding="utf-8"))
    config["parent_release_manifest_sha256"] = _sha256(manifest_path)
    source.config.write_text(
        yaml.safe_dump(config, allow_unicode=True, sort_keys=False), encoding="utf-8"
    )


def _sha256(path: Path) -> str:
    return hashlib.sha256(path.read_bytes()).hexdigest()


def _inventory_sha256(records: list[dict[str, object]]) -> str:
    inventory = [
        {
            key: record[key]
            for key in (
                "movie_id",
                "derived_object_uri",
                "derived_content_sha256",
                "derived_byte_length",
                "derived_mime_type",
            )
        }
        for record in sorted(records, key=lambda item: str(item["movie_id"]))
    ]
    payload = json.dumps(
        inventory, ensure_ascii=False, sort_keys=True, separators=(",", ":")
    ).encode("utf-8")
    return hashlib.sha256(payload).hexdigest()


def _fixture_inventory_sha256(root: Path, movie_ids: set[str]) -> str:
    records = []
    for movie_id in sorted(movie_ids):
        path = root / f"assets/posters/derived/{movie_id}.webp"
        records.append(
            {
                "movie_id": movie_id,
                "derived_object_uri": f"assets/posters/derived/{movie_id}.webp",
                "derived_content_sha256": _sha256(path),
                "derived_byte_length": path.stat().st_size,
                "derived_mime_type": "image/webp",
            }
        )
    payload = json.dumps(
        records, ensure_ascii=False, sort_keys=True, separators=(",", ":")
    ).encode("utf-8")
    return hashlib.sha256(payload).hexdigest()


def _rewrite_bundle_and_hash(
    manifest_path: Path, bundle_path: Path, records: list[dict[str, object]]
) -> None:
    bundle_bytes = b"".join(
        json.dumps(record, ensure_ascii=False, sort_keys=True, separators=(",", ":")).encode(
            "utf-8"
        )
        + b"\n"
        for record in records
    )
    bundle_path.write_bytes(bundle_bytes)
    manifest = json.loads(manifest_path.read_text(encoding="utf-8"))
    manifest["bundle"]["sha256"] = hashlib.sha256(bundle_bytes).hexdigest()
    manifest["bundle"]["size_bytes"] = len(bundle_bytes)
    posters = [record for record in records if record.get("record_kind") == "poster"]
    approved = [
        record
        for record in posters
        if record.get("quality_status") in {"machine_passed", "manual_approved"}
    ]
    unavailable = [
        record
        for record in posters
        if record.get("quality_status") in {"content_conflict", "missing", "placeholder"}
    ]
    manifest["counts"]["approved_poster_objects"] = len(approved)
    manifest["counts"]["unavailable_poster_rows"] = len(unavailable)
    manifest["counts"]["derived_poster_bytes"] = sum(
        int(record["derived_byte_length"]) for record in approved
    )
    manifest["poster_derivation"]["expected_approved_poster_objects"] = len(approved)
    manifest["poster_derivation"]["expected_unavailable_poster_rows"] = len(unavailable)
    manifest["poster_derivation"]["derived_inventory_sha256"] = _inventory_sha256(approved)
    manifest_path.write_text(
        json.dumps(manifest, ensure_ascii=False, sort_keys=True, separators=(",", ":")) + "\n",
        encoding="utf-8",
    )
