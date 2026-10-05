from __future__ import annotations

import json
from copy import deepcopy
from pathlib import Path

import pytest
from jsonschema import Draft202012Validator, FormatChecker, ValidationError

from hk_movie_rag.staging import _SAFE_HTTP_URL, _require_http_url

SCHEMA_ROOT = Path(__file__).resolve().parents[1] / "schemas"


def test_published_schemas_are_valid_draft_2020_12() -> None:
    """Breaks if any published contract is not a valid executable JSON Schema."""
    for path in sorted(SCHEMA_ROOT.glob("*.schema.json")):
        Draft202012Validator.check_schema(_load_schema(path.name))


def test_rag_manifest_schema_1_1_keeps_exact_two_document_contract() -> None:
    """Breaks if publishing 1.2 silently weakens historical 1.1 validation."""
    schema = _load_schema("rag_release_manifest.schema.json")
    assert schema["properties"]["schema_version"] == {"const": "1.1"}
    assert schema["properties"]["documents"]["minItems"] == 2
    assert schema["properties"]["documents"]["maxItems"] == 2


def test_rag_manifest_schema_1_2_accepts_any_positive_document_count() -> None:
    """Breaks if reusable releases remain frozen to the legacy two-document shape."""
    validator = _validator(_load_schema("rag_release_manifest_v1_2.schema.json"))
    for count in (1, 3):
        manifest = _valid_rag_manifest_v1_2()
        manifest["documents"] = [
            {**manifest["documents"][0], "document_id": f"doc-{index}", "source_filename": f"doc-{index}.pdf", "source_sha256": f"{index + 1:064x}"}
            for index in range(count)
        ]
        manifest["counts"]["documents"] = count
        manifest["counts"]["pdf_passages"] = count
        validator.validate(manifest)


@pytest.mark.parametrize(
    ("mutation", "message"),
    [
        (lambda value: value["documents"][0].update({"rights_status": "public"}), "rights"),
        (lambda value: value["documents"][0].update({"quality_status": "draft"}), "quality"),
        (lambda value: value.pop("relevance_policy_sha256"), "relevance hash"),
        (lambda value: value.pop("poster_authority_sha256"), "poster hash"),
        (lambda value: value.update({"text_extraction_profile": "unknown"}), "extraction profile"),
        (lambda value: value.update({"document_embedding_profile": "unknown"}), "embedding profile"),
    ],
)
def test_rag_manifest_schema_1_2_rejects_untrusted_contract_values(
    mutation: object, message: str
) -> None:
    """Breaks if ungoverned policy, profile, rights, or quality values validate."""
    manifest = _valid_rag_manifest_v1_2()
    mutation(manifest)

    with pytest.raises(ValidationError):
        _validator(_load_schema("rag_release_manifest_v1_2.schema.json")).validate(manifest)


@pytest.mark.parametrize(
    "required_field",
    ["identity_confidence", "source_url", "created_at", "reviewed_at"],
)
def test_media_asset_contract_requires_governing_record_fields(
    required_field: str,
) -> None:
    """Breaks if a governed media record can omit identity/source/audit evidence."""
    record = _valid_media_asset()
    _media_validator().validate(record)
    record.pop(required_field)

    with pytest.raises(ValidationError):
        _media_validator().validate(record)


def test_media_asset_contract_rejects_additional_properties() -> None:
    """Breaks if ungoverned fields can enter the canonical media record."""
    record = _valid_media_asset()
    record["override"] = True

    with pytest.raises(ValidationError):
        _media_validator().validate(record)


def test_url_contracts_publish_the_runtime_safe_subset() -> None:
    """Breaks if either published URL grammar drifts from runtime validation."""
    media_pattern = _load_schema("media_asset.schema.json")["$defs"]["httpUrl"][
        "pattern"
    ]
    document_pattern = _load_schema("movie_document.schema.json")["properties"][
        "source_urls"
    ]["items"]["pattern"]

    assert media_pattern == document_pattern == _SAFE_HTTP_URL.pattern


@pytest.mark.parametrize(
    "source_url",
    [
        "https://example.com",
        "https://sub-domain.example.com/source?id=1#section",
        "http://example.com/archive/a:b",
        "https://127.0.0.1/source",
        "https://example.com/a-z_A~9/!$&'()*+,;=:@/%20?q=one/two?three&x=%2F#frag/path?x=%7E",
    ],
)
def test_url_schema_and_runtime_accept_the_same_safe_subset(source_url: str) -> None:
    """Breaks if a legitimate contract URL is rejected by schema or runtime."""
    _require_http_url(source_url)
    sidecar = _valid_poster_sidecar()
    sidecar["source_url"] = source_url
    document = _valid_movie_document()
    document["source_urls"] = [source_url]

    _poster_submission_validator().validate(sidecar)
    _movie_document_validator().validate(document)


@pytest.mark.parametrize(
    "source_url",
    [
        "https://user@example.com/source",
        "https://:secret@example.com/source",
        "https://example.com:443/source",
        "https://example.com:99999/x",
        "https://[::1]/x",
        "https://[::1/x",
        "https://:/x",
        "HTTPS://example.com/source",
        "https://example.com\\source",
        "https://example.com/a b",
        "https://example.com\n",
        "https://example.com/path\n",
        "https://example.com\r",
        "https://example.com/path\r",
        "https://example.com\r\n",
        "https://example.com/path\r\n",
        "https://example.com\x00",
        "https://example.com/path\x00",
        "https://example.com/\x00",
        "https://example.com/\x1f",
        "https://example.com/\x7f",
        "https://example.com/café",
        'https://example.com/"',
        "https://example.com/<",
        "https://example.com/>",
        "https://example.com/{",
        "https://example.com/}",
        "https://example.com/|",
        "https://example.com/^",
        "https://example.com/`",
        "https://example.com/%",
        "https://example.com/%z",
        "https://example.com/%zz",
        "https://example.com/?q=\x00",
        "https://example.com/?q=café",
        "https://example.com/?q=%",
        "https://example.com/#frag|bad",
        "https://example.com/#café",
        "https://example.com/#%zz",
    ],
)
def test_url_schema_and_runtime_reject_the_same_unsafe_subset(source_url: str) -> None:
    """Breaks if published schemas and runtime disagree on unsafe URLs."""
    sidecar = _valid_poster_sidecar()
    sidecar["source_url"] = source_url
    document = _valid_movie_document()
    document["source_urls"] = [source_url]

    with pytest.raises(ValueError):
        _require_http_url(source_url)
    with pytest.raises(ValidationError):
        _poster_submission_validator().validate(sidecar)
    with pytest.raises(ValidationError):
        _movie_document_validator().validate(document)


@pytest.mark.parametrize(
    "relative_path",
    [
        "/absolute/path",
        "C:/drive/path",
        "C:\\drive\\path",
        "\\\\server\\share\\path",
        "data\\release\\movies.parquet",
        ".",
        "./movies.parquet",
        "data/./movies.parquet",
        "..",
        "../movies.parquet",
        "data/../movies.parquet",
        "data//movies.parquet",
        "data/release/",
        "data/movie:stream.parquet",
        "data/<movie>.parquet",
        "data/\"movie\".parquet",
        "data/movie|copy.parquet",
        "data/movie?.parquet",
        "data/movie*.parquet",
        "CON",
        "con.txt",
        "data/AuX.json",
        "data/NUL",
        "data/com1.bin",
        "data/LpT9.parquet",
        "data/name.",
        "data/name ",
        "data./name",
        "data /name",
    ],
)
@pytest.mark.parametrize("artifact_field", ["relative_path", "schema_path"])
def test_release_manifest_schema_rejects_noncanonical_relative_paths(
    relative_path: str, artifact_field: str
) -> None:
    """Breaks if manifest paths can use traversal or platform-specific aliases."""
    manifest = _valid_release_manifest()
    manifest["artifacts"][0][artifact_field] = relative_path

    with pytest.raises(ValidationError):
        _manifest_validator().validate(manifest)


@pytest.mark.parametrize(
    "relative_path",
    [
        "data/release/v1.2/movies.parquet",
        "schemas/media_asset.schema.json",
        "artifacts_1/a-b_c.123",
        "README.md",
    ],
)
def test_release_manifest_schema_accepts_forward_slash_relative_paths(
    relative_path: str,
) -> None:
    """Breaks if a canonical repository-relative artifact path is rejected."""
    manifest = _valid_release_manifest()
    manifest["artifacts"][0]["relative_path"] = relative_path
    _manifest_validator().validate(manifest)


def _load_schema(filename: str) -> dict[str, object]:
    return json.loads((SCHEMA_ROOT / filename).read_text(encoding="utf-8"))


def _validator(schema: dict[str, object]) -> Draft202012Validator:
    Draft202012Validator.check_schema(schema)
    return Draft202012Validator(schema, format_checker=FormatChecker())


def _media_validator() -> Draft202012Validator:
    return _validator(_load_schema("media_asset.schema.json"))


def _poster_submission_validator() -> Draft202012Validator:
    media = _load_schema("media_asset.schema.json")
    schema = {
        "$schema": "https://json-schema.org/draft/2020-12/schema",
        "$ref": "#/$defs/posterSubmission",
        "$defs": deepcopy(media["$defs"]),
    }
    return _validator(schema)


def _movie_document_validator() -> Draft202012Validator:
    return _validator(_load_schema("movie_document.schema.json"))


def _manifest_validator() -> Draft202012Validator:
    return _validator(_load_schema("release_manifest.schema.json"))


def _valid_poster_sidecar() -> dict[str, object]:
    return {
        "schema_version": "1.0",
        "movie_id": "1970_GSQ_001",
        "submission_id": "poster-1",
        "source_url": "https://example.com/poster.jpg",
        "retrieved_at": "2026-07-27T10:00:00Z",
        "rights_status": "cleared",
        "expected_sha256": "a" * 64,
    }


def _valid_movie_document() -> dict[str, object]:
    return {
        "schema_version": "1.0",
        "movie_id": "1970_GSQ_001",
        "document_id": "analysis-1",
        "document_type": "analysis",
        "language": "en",
        "title": "Analysis",
        "source_urls": ["https://example.com/source"],
        "rights_status": "cleared",
        "authorship_method": "human",
        "content_version": "1",
        "content_sha256": "b" * 64,
    }


def _valid_media_asset() -> dict[str, object]:
    return {
        "asset_id": "poster:1970_GSQ_001:v1",
        "movie_id": "1970_GSQ_001",
        "asset_type": "poster",
        "source_object_uri": "source/poster.jpg",
        "original_object_uri": "assets/posters/originals/poster.jpg",
        "derived_object_uri": "assets/posters/derived/poster.webp",
        "quarantine_object_uri": "",
        "content_sha256": "c" * 64,
        "detected_format": "JPEG",
        "detected_mime_type": "image/jpeg",
        "detected_extension": ".jpg",
        "width": 600,
        "height": 900,
        "color_mode": "RGB",
        "byte_length": 12345,
        "quality_status": "machine_passed",
        "identity_confidence": None,
        "source_url": None,
        "rights_status": "cleared",
        "version": 1,
        "is_primary": True,
        "publishable": True,
        "created_at": None,
        "reviewed_at": None,
    }


def _valid_release_manifest() -> dict[str, object]:
    return {
        "release_version": "v1.2",
        "source_manifest_sha256": "d" * 64,
        "source_generated_at": "2026-07-17T02:10:25Z",
        "expected_counts": {
            "release_movies": 4658,
            "quarantine_movies": 6,
            "tier_counts": {"A": 122},
            "pilot_movies": 23,
            "audit_rows": 805,
        },
        "poster_state_counts": {
            "machine_passed": 4545,
            "content_conflict": 73,
            "placeholder": 9,
            "missing": 31,
        },
        "artifacts": [
            {
                "relative_path": "data/release/v1.2/movies.parquet",
                "schema_path": "schemas/movie.schema.json",
                "size_bytes": 1,
                "sha256": "e" * 64,
                "row_count": 4658,
            }
        ],
    }


def _valid_rag_manifest_v1_2() -> dict[str, object]:
    return {
        "schema_version": "1.2",
        "rag_release_id": "vtest-demo-r2",
        "parent_release_manifest_sha256": "a" * 64,
        "bundle": {"filename": "rag_bundle.jsonl", "sha256": "b" * 64, "size_bytes": 1},
        "counts": {
            "movies": 1,
            "facet_count": 1,
            "tier_s_count": 0,
            "tier_a_count": 0,
            "tier_b_count": 1,
            "pilot_count": 0,
            "poster_rows": 1,
            "primary_poster_rows": 1,
            "approved_poster_objects": 0,
            "unavailable_poster_rows": 1,
            "derived_poster_bytes": 0,
            "metadata_passages": 1,
            "documents": 1,
            "pdf_passages": 1,
        },
        "facet_artifacts": {
            "movie_tiers": {"relative_path": "movie_tiers.parquet", "sha256": "c" * 64, "row_count": 1},
            "pilot_movies": {"relative_path": "pilot_movies.parquet", "sha256": "d" * 64, "row_count": 0},
        },
        "poster_derivation": {
            "derived_is_primary": True,
            "mode": "one_state_row_per_movie",
            "source_is_primary_field": "source_is_primary",
            "derived_object_key_template": "assets/posters/derived/{movie_id}.webp",
            "derived_mime_type": "image/webp",
            "unavailable_identity": "absent",
            "expected_approved_poster_objects": 0,
            "expected_unavailable_poster_rows": 1,
            "derived_inventory_sha256": "e" * 64,
        },
        "models": {
            "embedding_model": "gemini-embedding-2",
            "embedding_dimension": 768,
            "generation_model": "gemini-3.5-flash-lite",
        },
        "text_extraction_profile": "cjk-layout-v1",
        "document_embedding_profile": "vertex-title-text-v1",
        "relevance_policy_sha256": "f" * 64,
        "poster_authority_sha256": "1" * 64,
        "access_mode": "restricted_demo",
        "documents": [
            {
                "movie_id": "movie-1",
                "document_id": "doc-1",
                "source_filename": "doc-1.pdf",
                "source_sha256": "2" * 64,
                "rights_status": "restricted",
                "quality_status": "manual_approved",
                "page_count": 1,
            }
        ],
    }
