"""Parent-bound deterministic JSONL bundle for the minimum RAG demo."""

from __future__ import annotations

import csv
import hashlib
import json
import re
import stat
from collections.abc import Iterator, Mapping, Sequence
from dataclasses import dataclass
from datetime import date
from importlib.resources import files
from pathlib import Path, PurePosixPath

import pyarrow.parquet as pq
import yaml
from jsonschema import Draft202012Validator

from .hashing import sha256_file
from .pdf_extract import PdfExtractError, PdfPage, extract_pdf_pages
from .poster_policy import MAX_POSTER_BYTES
from .release_manifest import ManifestError, verify_release_manifest

_MOVIE_FIELDS = (
    "movie_id",
    "chinese_title",
    "english_title",
    "release_date",
    "production_region",
    "director",
    "screenwriter",
    "cast",
    "genre",
    "runtime_minutes",
    "production_company",
    "data_source",
)
_KIND_ORDER = {"movie": 0, "poster": 1, "facet": 2, "document": 3, "passage": 4}
_RAG_PRIMARY_POLICY = "one_state_row_per_movie"
_TIERS = frozenset({"S", "A", "B"})
_APPROVED_POSTER_STATES = frozenset({"machine_passed", "manual_approved"})
_UNAVAILABLE_POSTER_STATES = frozenset({"content_conflict", "missing", "placeholder"})
_DERIVED_MIME_TYPE = "image/webp"
_DERIVED_IDENTITY_FIELDS = (
    "derived_content_sha256",
    "derived_byte_length",
    "derived_mime_type",
)
_DOCUMENT_BINDING_FIELDS = (
    "movie_id",
    "document_id",
    "source_filename",
    "source_sha256",
    "rights_status",
    "quality_status",
)
_SHA256 = re.compile(r"[0-9a-f]{64}")
_DIRECT_FILENAME = re.compile(r"(?!\.{1,2}$)[^/\\:*?\"<>|]+$")
_HISTORICAL_SCHEMA_VERSION = "1.1"
_REUSABLE_SCHEMA_VERSION = "1.2"
_SUPPLEMENTAL_SCHEMA_VERSION = "1.3"
_HISTORICAL_TEXT_EXTRACTION_PROFILE = "historical-v1"
_REUSABLE_TEXT_EXTRACTION_PROFILE = "cjk-layout-v1"
_REUSABLE_DOCUMENT_EMBEDDING_PROFILE = "vertex-title-text-v1"
_MANIFEST_SCHEMAS = {
    _HISTORICAL_SCHEMA_VERSION: "rag_release_manifest.schema.json",
    _REUSABLE_SCHEMA_VERSION: "rag_release_manifest_v1_2.schema.json",
    _SUPPLEMENTAL_SCHEMA_VERSION: "rag_release_manifest_v1_3.schema.json",
}
_V12_PARENT_MANIFEST_SHA256 = (
    "e9842db33208e7146d78d86689b13f81fffcf41288d79d84e599b93050445e2c"
)
_V12_RAG_RELEASE_ID = "v1.2-demo"
_V12_DERIVED_INVENTORY_SHA256 = (
    "b754727129bc044b6c9da1e6db22067c8279c5e7f8dd41d0350e365376538154"
)
_V12_POSTER_SEMANTICS = (4658, 4545, 113)
_V12_FACET_SEMANTICS = (4658, 50, 313, 4295, 24)
_V12_FACET_ARTIFACTS = {
    "movie_tiers": {
        "relative_path": "data/release/v1.2/movie_tiers.parquet",
        "sha256": "d9bb0ddd8ab7269bb9b3948be7e34f4a032dc2fe7d59a134df02b5b2ff3c4256",
        "row_count": 4658,
    },
    "pilot_movies": {
        "relative_path": "data/release/v1.2/pilot_movies.parquet",
        "sha256": "ad2e8cf500a70f2f2d8e063aacb6ea26643d75268046de8ce2076e54fdd07565",
        "row_count": 24,
    },
}
_FACET_ARTIFACT_FILENAMES = {
    "movie_tiers": "movie_tiers.parquet",
    "pilot_movies": "pilot_movies.parquet",
}
_POSTER_DERIVATION_POLICY = {
    "derived_is_primary": True,
    "mode": _RAG_PRIMARY_POLICY,
    "source_is_primary_field": "source_is_primary",
    "derived_object_key_template": "assets/posters/derived/{movie_id}.webp",
    "derived_mime_type": _DERIVED_MIME_TYPE,
    "unavailable_identity": "absent",
}


class RagBundleError(ValueError):
    """Raised when the RAG input or published bundle is not authoritative."""


@dataclass(frozen=True)
class RagBundleResult:
    manifest_path: Path
    bundle_path: Path
    movie_count: int
    facet_count: int
    tier_s_count: int
    tier_a_count: int
    tier_b_count: int
    pilot_count: int
    poster_row_count: int
    primary_poster_row_count: int
    approved_poster_object_count: int
    unavailable_poster_row_count: int
    derived_poster_bytes: int
    derived_inventory_sha256: str
    metadata_passage_count: int
    document_count: int
    pdf_passage_count: int
    bundle_sha256: str
    manifest_bytes: bytes


@dataclass(frozen=True)
class RagReleaseCounts:
    movies: int
    facet_count: int
    tier_s_count: int
    tier_a_count: int
    tier_b_count: int
    pilot_count: int
    poster_rows: int
    primary_poster_rows: int
    approved_poster_objects: int
    unavailable_poster_rows: int
    derived_poster_bytes: int
    metadata_passages: int
    documents: int
    pdf_passages: int


@dataclass(frozen=True)
class RagReleaseContract:
    schema_version: str
    rag_release_id: str
    parent_release_manifest_sha256: str
    manifest_sha256: str
    bundle_sha256: str
    derived_inventory_sha256: str
    counts: RagReleaseCounts
    embedding_model: str
    embedding_dimension: int
    generation_model: str
    text_extraction_profile: str
    document_embedding_profile: str
    relevance_policy_sha256: str | None
    poster_authority_sha256: str | None
    access_mode: str

    @property
    def expected_embeddings(self) -> int:
        return self.counts.metadata_passages + self.counts.pdf_passages


@dataclass(frozen=True)
class VerifiedRagBundle:
    manifest_path: Path
    bundle_path: Path
    rag_release_id: str
    movie_count: int
    facet_count: int
    tier_s_count: int
    tier_a_count: int
    tier_b_count: int
    pilot_count: int
    poster_row_count: int
    primary_poster_row_count: int
    approved_poster_object_count: int
    unavailable_poster_row_count: int
    derived_poster_bytes: int
    derived_inventory_sha256: str
    metadata_passage_count: int
    document_count: int
    pdf_passage_count: int
    embedding_model: str
    embedding_dimension: int
    generation_model: str
    access_mode: str
    contract: RagReleaseContract

    def iter_records(self) -> Iterator[dict[str, object]]:
        with self.bundle_path.open("r", encoding="utf-8", newline="") as stream:
            for line in stream:
                yield _load_json(line, "bundle record")


@dataclass(frozen=True)
class _Document:
    binding: dict[str, object]
    pages: tuple[PdfPage, ...]


@dataclass(frozen=True)
class PreparedPdfBinding:
    """Read-only, config-ready identity for one governed PDF candidate."""

    movie_id: str
    document_id: str
    source_filename: str
    source_sha256: str
    rights_status: str
    quality_status: str
    page_count: int
    parent_release_manifest_sha256: str
    text_extraction_profile: str
    next_document_count: int
    next_pdf_passage_count: int
    next_expected_embedding_count: int

    def to_dict(self) -> dict[str, object]:
        return {
            "binding": {
                "document_id": self.document_id,
                "movie_id": self.movie_id,
                "page_count": self.page_count,
                "quality_status": self.quality_status,
                "rights_status": self.rights_status,
                "source_filename": self.source_filename,
                "source_sha256": self.source_sha256,
            },
            "next_counts": {
                "documents": self.next_document_count,
                "expected_embeddings": self.next_expected_embedding_count,
                "pdf_passages": self.next_pdf_passage_count,
            },
            "parent_release_manifest_sha256": self.parent_release_manifest_sha256,
            "schema_version": "rag-pdf-binding-preflight/v1",
            "text_extraction_profile": self.text_extraction_profile,
            "valid": True,
        }


def prepare_pdf_binding(
    source_root: Path,
    config_path: Path,
    document_source_root: Path,
    *,
    movie_id: str,
    document_id: str,
    source_filename: str,
    rights_status: str,
    quality_status: str,
) -> PreparedPdfBinding:
    """Validate one new PDF and return its exact binding without mutating governed inputs."""
    root = source_root.resolve()
    if not root.is_dir():
        raise RagBundleError("source root must resolve to a directory")
    document_root = _resolve_document_root(root, document_source_root)
    config = _load_config(config_path)
    if _required_text(config, "schema_version") != _REUSABLE_SCHEMA_VERSION:
        raise RagBundleError("PDF binding preflight requires schema 1.2")
    if not isinstance(movie_id, str) or not movie_id:
        raise RagBundleError("deep document movie_id must be non-empty text")
    if not isinstance(document_id, str) or not document_id:
        raise RagBundleError("deep document document_id must be non-empty text")
    if not isinstance(source_filename, str) or not source_filename:
        raise RagBundleError("deep document source_filename must be non-empty text")
    if rights_status != "restricted" or quality_status != "manual_approved":
        raise RagBundleError("deep document governance decisions are invalid")

    parent_manifest_sha256 = _required_text(
        config, "parent_release_manifest_sha256"
    )
    manifest_path = root / str(
        config.get(
            "parent_release_manifest_path",
            "data/release/v1.2/release_manifest.json",
        )
    )
    _require_under(root, manifest_path, "parent release manifest")
    if not manifest_path.is_file() or sha256_file(manifest_path) != parent_manifest_sha256:
        raise RagBundleError("parent release manifest SHA-256 mismatch")
    try:
        verify_release_manifest(manifest_path)
    except ManifestError as exc:
        raise RagBundleError(f"parent release manifest is invalid: {exc}") from exc
    parent = _load_json(
        manifest_path.read_text(encoding="utf-8"), "parent release manifest"
    )
    movies = _read_movies(root, manifest_path.parent, parent)
    if movie_id not in movies:
        raise RagBundleError("deep document movie is not in the Release")
    expected_movies = _required_int(config, "expected_movies")
    if len(movies) != expected_movies:
        raise RagBundleError(
            f"movie count mismatch: expected {expected_movies}, got {len(movies)}"
        )

    raw_documents = config.get("deep_documents")
    if not isinstance(raw_documents, list) or not raw_documents:
        raise RagBundleError("one or more deep documents are required")
    existing_document_ids: set[str] = set()
    existing_source_filenames: set[str] = set()
    existing_source_hashes: set[str] = set()
    existing_pdf_passages = 0
    for raw in raw_documents:
        if not isinstance(raw, Mapping):
            raise RagBundleError("deep document must be an object")
        existing_document_ids.add(_required_text(raw, "document_id"))
        existing_source_filenames.add(_required_text(raw, "source_filename"))
        existing_source_hashes.add(_required_text(raw, "source_sha256"))
        existing_pdf_passages += _required_positive_int(raw, "page_count")
    if len(existing_document_ids) != len(raw_documents):
        raise RagBundleError("existing deep document IDs are not unique")
    if (
        len(existing_source_filenames) != len(raw_documents)
        or len(existing_source_hashes) != len(raw_documents)
    ):
        raise RagBundleError("existing deep document source bindings are not unique")
    if document_id in existing_document_ids:
        raise RagBundleError(f"duplicate document_id: {document_id}")
    if source_filename in existing_source_filenames:
        raise RagBundleError(f"duplicate source binding: {source_filename}")

    profile = _required_text(config, "text_extraction_profile")
    if profile != _REUSABLE_TEXT_EXTRACTION_PROFILE:
        raise RagBundleError("schema 1.2 extraction profile is invalid")
    path = _resolve_regular_pdf_source(document_root, source_filename)
    source_sha256, pages = _extract_stable_pdf_pages(path, profile=profile)
    if source_sha256 in existing_source_hashes:
        raise RagBundleError(f"duplicate source binding: {source_filename}")
    next_pdf_passages = existing_pdf_passages + len(pages)
    return PreparedPdfBinding(
        movie_id=movie_id,
        document_id=document_id,
        source_filename=source_filename,
        source_sha256=source_sha256,
        rights_status=rights_status,
        quality_status=quality_status,
        page_count=len(pages),
        parent_release_manifest_sha256=parent_manifest_sha256,
        text_extraction_profile=profile,
        next_document_count=len(raw_documents) + 1,
        next_pdf_passage_count=next_pdf_passages,
        next_expected_embedding_count=expected_movies + next_pdf_passages,
    )


def build_rag_bundle(
    source_root: Path,
    config_path: Path,
    output_dir: Path,
    *,
    document_source_root: Path | None = None,
) -> RagBundleResult:
    """Build a byte-deterministic JSONL bundle from the declared Release and PDFs only."""
    root = source_root.resolve()
    config = _load_config(config_path)
    schema_version = _required_text(config, "schema_version")
    document_root = _resolve_document_root(root, document_source_root)
    _require_v12_binding(
        _required_text(config, "rag_release_id"),
        _required_text(config, "parent_release_manifest_sha256"),
    )
    expected_derived_inventory_sha256 = _required_text(
        config, "expected_derived_inventory_sha256"
    )
    if not _valid_sha256(expected_derived_inventory_sha256):
        raise RagBundleError("configured derived poster inventory SHA-256 is invalid")
    manifest_path = root / str(
        config.get("parent_release_manifest_path", "data/release/v1.2/release_manifest.json")
    )
    _require_under(root, manifest_path, "parent release manifest")
    if not manifest_path.is_file():
        raise RagBundleError("parent release manifest is missing")
    if sha256_file(manifest_path) != _required_text(config, "parent_release_manifest_sha256"):
        raise RagBundleError("parent release manifest SHA-256 mismatch")
    try:
        verify_release_manifest(manifest_path)
    except ManifestError as exc:
        raise RagBundleError(f"parent release manifest is invalid: {exc}") from exc
    parent = _load_json(manifest_path.read_text(encoding="utf-8"), "parent release manifest")
    release_dir = manifest_path.parent
    movies = _read_movies(root, release_dir, parent)
    posters = _read_posters(root, release_dir, parent)
    facets, facet_artifacts = _read_facets(root, release_dir, parent, movies)
    supplemental_movies, supplemental_posters, supplemental_facets = (
        _supplemental_overlay(config, schema_version, movies)
    )
    movies.update(supplemental_movies)
    posters.update(supplemental_posters)
    facets.update(supplemental_facets)
    movies = dict(sorted(movies.items()))
    posters = dict(sorted(posters.items()))
    facets = dict(sorted(facets.items()))
    expected_movies = _required_int(config, "expected_movies")
    expected_posters = _required_int(config, "expected_poster_rows")
    expected_approved = _required_int(config, "expected_approved_poster_objects")
    expected_unavailable = _required_int(config, "expected_unavailable_poster_rows")
    expected_facet_count = _required_int(config, "expected_facet_count")
    expected_tier_s_count = _required_int(config, "expected_tier_s_count")
    expected_tier_a_count = _required_int(config, "expected_tier_a_count")
    expected_tier_b_count = _required_int(config, "expected_tier_b_count")
    expected_pilot_count = _required_int(config, "expected_pilot_count")
    if len(movies) != expected_movies:
        raise RagBundleError(f"movie count mismatch: expected {expected_movies}, got {len(movies)}")
    if len(posters) != expected_posters:
        raise RagBundleError(
            f"poster row count mismatch: expected {expected_posters}, got {len(posters)}"
        )
    if set(movies) != set(posters):
        raise RagBundleError("poster movie membership does not match movies")
    if expected_approved + expected_unavailable != expected_posters:
        raise RagBundleError("configured poster semantic counts do not match poster rows")
    documents = _declared_documents(document_root, config, movies, schema_version)
    records = _records(root, movies, posters, facets, documents)
    _require_poster_derivation(records)
    _require_facet_derivation(records)
    output_dir.mkdir(parents=True, exist_ok=True)
    bundle_path = output_dir / "rag_bundle.jsonl"
    bundle_bytes = b"".join(_canonical_json(record) + b"\n" for record in records)
    bundle_path.write_bytes(bundle_bytes)
    counts = _record_counts(records)
    if (
        counts["approved_poster_objects"] != expected_approved
        or counts["unavailable_poster_rows"] != expected_unavailable
    ):
        raise RagBundleError("poster semantic counts do not match configuration")
    if (
        counts["facet_count"],
        counts["tier_s_count"],
        counts["tier_a_count"],
        counts["tier_b_count"],
        counts["pilot_count"],
    ) != (
        expected_facet_count,
        expected_tier_s_count,
        expected_tier_a_count,
        expected_tier_b_count,
        expected_pilot_count,
    ):
        raise RagBundleError("facet semantic counts do not match configuration")
    _require_v12_facet_semantics(_required_text(config, "rag_release_id"), counts)
    _require_facet_artifacts(
        facet_artifacts,
        counts,
        _required_text(config, "rag_release_id"),
        schema_version=schema_version,
        supplemental_facet_count=len(supplemental_facets),
        supplemental_pilot_count=sum(
            facet["pilot_movie"] is True for facet in supplemental_facets.values()
        ),
    )
    derived_inventory_sha256 = _derived_inventory_sha256(records)
    if derived_inventory_sha256 != expected_derived_inventory_sha256:
        raise RagBundleError("derived poster inventory does not match configuration")
    _require_v12_inventory(_required_text(config, "rag_release_id"), derived_inventory_sha256)
    bundle_sha256 = hashlib.sha256(bundle_bytes).hexdigest()
    manifest: dict[str, object] = {
        "schema_version": schema_version,
        "rag_release_id": _required_text(config, "rag_release_id"),
        "parent_release_manifest_sha256": _required_text(config, "parent_release_manifest_sha256"),
        "bundle": {
            "filename": bundle_path.name,
            "sha256": bundle_sha256,
            "size_bytes": len(bundle_bytes),
        },
        "counts": counts,
        "facet_artifacts": facet_artifacts,
        "poster_derivation": {
            **_POSTER_DERIVATION_POLICY,
            "expected_approved_poster_objects": expected_approved,
            "expected_unavailable_poster_rows": expected_unavailable,
            "derived_inventory_sha256": derived_inventory_sha256,
        },
        "models": {
            "embedding_model": _required_text(config, "embedding_model"),
            "embedding_dimension": _required_int(config, "embedding_dimension"),
            "generation_model": _required_text(config, "generation_model"),
        },
        "access_mode": _required_text(config, "access_mode"),
        "documents": [document.binding for document in documents],
    }
    if _is_reusable_schema(schema_version):
        manifest.update(
            {
                "text_extraction_profile": _required_text(
                    config, "text_extraction_profile"
                ),
                "document_embedding_profile": _required_text(
                    config, "document_embedding_profile"
                ),
                "relevance_policy_sha256": _required_text(
                    config, "relevance_policy_sha256"
                ),
                "poster_authority_sha256": _required_text(
                    config, "poster_authority_sha256"
                ),
            }
        )
    if schema_version == _SUPPLEMENTAL_SCHEMA_VERSION:
        manifest.update(
            {
                "parent_rag_release_id": _required_text(
                    config, "parent_rag_release_id"
                ),
                "supplemental_movies": list(supplemental_movies.values()),
                "supplemental_facets": list(supplemental_facets.values()),
                "supplemental_posters": list(supplemental_posters.values()),
            }
        )
    _validate_manifest(manifest)
    manifest_bytes = _canonical_json(manifest) + b"\n"
    result_path = output_dir / "rag_release_manifest.json"
    result_path.write_bytes(manifest_bytes)
    return RagBundleResult(
        manifest_path=result_path,
        bundle_path=bundle_path,
        movie_count=counts["movies"],
        facet_count=counts["facet_count"],
        tier_s_count=counts["tier_s_count"],
        tier_a_count=counts["tier_a_count"],
        tier_b_count=counts["tier_b_count"],
        pilot_count=counts["pilot_count"],
        poster_row_count=counts["poster_rows"],
        primary_poster_row_count=counts["primary_poster_rows"],
        approved_poster_object_count=counts["approved_poster_objects"],
        unavailable_poster_row_count=counts["unavailable_poster_rows"],
        derived_poster_bytes=counts["derived_poster_bytes"],
        derived_inventory_sha256=derived_inventory_sha256,
        metadata_passage_count=counts["metadata_passages"],
        document_count=counts["documents"],
        pdf_passage_count=counts["pdf_passages"],
        bundle_sha256=bundle_sha256,
        manifest_bytes=manifest_bytes,
    )


def verify_rag_bundle(manifest_path: Path) -> VerifiedRagBundle:
    """Verify a standalone RAG manifest and return its ordered JSONL record stream."""
    manifest_path = manifest_path.resolve()
    manifest_bytes = manifest_path.read_bytes()
    manifest_sha256 = hashlib.sha256(manifest_bytes).hexdigest()
    try:
        manifest_text = manifest_bytes.decode("utf-8")
    except UnicodeDecodeError as exc:
        raise RagBundleError("RAG release manifest is not UTF-8") from exc
    manifest = _load_json(manifest_text, "RAG release manifest")
    _validate_manifest(manifest)
    bundle = _mapping(manifest, "bundle")
    bundle_path = manifest_path.parent / _required_text(bundle, "filename")
    if not bundle_path.is_file() or bundle_path.stat().st_size != _required_int(
        bundle, "size_bytes"
    ):
        raise RagBundleError("bundle size mismatch")
    if sha256_file(bundle_path) != _required_text(bundle, "sha256"):
        raise RagBundleError("bundle SHA-256 mismatch")
    records = list(_iter_records(bundle_path))
    _require_order(records)
    _require_document_bindings(manifest, records)
    _require_document_pages(manifest, records)
    _require_supplemental_records(manifest, records)
    _require_poster_derivation(records)
    _require_facet_derivation(records)
    observed = _record_counts(records)
    counts = _mapping(manifest, "counts")
    if observed != {key: _required_int(counts, key) for key in observed}:
        raise RagBundleError("bundle record counts mismatch")
    poster_derivation = _mapping(manifest, "poster_derivation")
    if (
        observed["approved_poster_objects"]
        != _required_int(poster_derivation, "expected_approved_poster_objects")
        or observed["unavailable_poster_rows"]
        != _required_int(poster_derivation, "expected_unavailable_poster_rows")
    ):
        raise RagBundleError("bundle poster semantic counts mismatch")
    derived_inventory_sha256 = _derived_inventory_sha256(records)
    if derived_inventory_sha256 != _required_text(
        poster_derivation, "derived_inventory_sha256"
    ):
        raise RagBundleError("bundle derived poster inventory mismatch")
    parent_manifest_sha256 = _required_text(manifest, "parent_release_manifest_sha256")
    rag_release_id = _required_text(manifest, "rag_release_id")
    _require_v12_binding(rag_release_id, parent_manifest_sha256)
    if rag_release_id == _V12_RAG_RELEASE_ID and (
        observed["poster_rows"],
        observed["approved_poster_objects"],
        observed["unavailable_poster_rows"],
    ) != _V12_POSTER_SEMANTICS:
        raise RagBundleError("v1.2 poster semantics mismatch")
    _require_v12_inventory(rag_release_id, derived_inventory_sha256)
    _require_v12_facet_semantics(rag_release_id, observed)
    supplemental_facets = manifest.get("supplemental_facets", [])
    if not isinstance(supplemental_facets, list):
        raise RagBundleError("supplemental facets must be an array")
    _require_facet_artifacts(
        _mapping(manifest, "facet_artifacts"),
        observed,
        rag_release_id,
        schema_version=_required_text(manifest, "schema_version"),
        supplemental_facet_count=len(supplemental_facets),
        supplemental_pilot_count=sum(
            isinstance(facet, dict) and facet.get("pilot_movie") is True
            for facet in supplemental_facets
        ),
    )
    models = _mapping(manifest, "models")
    contract = _release_contract(manifest_sha256, manifest)
    return VerifiedRagBundle(
        manifest_path=manifest_path,
        bundle_path=bundle_path,
        rag_release_id=rag_release_id,
        movie_count=observed["movies"],
        facet_count=observed["facet_count"],
        tier_s_count=observed["tier_s_count"],
        tier_a_count=observed["tier_a_count"],
        tier_b_count=observed["tier_b_count"],
        pilot_count=observed["pilot_count"],
        poster_row_count=observed["poster_rows"],
        primary_poster_row_count=observed["primary_poster_rows"],
        approved_poster_object_count=observed["approved_poster_objects"],
        unavailable_poster_row_count=observed["unavailable_poster_rows"],
        derived_poster_bytes=observed["derived_poster_bytes"],
        derived_inventory_sha256=derived_inventory_sha256,
        metadata_passage_count=observed["metadata_passages"],
        document_count=observed["documents"],
        pdf_passage_count=observed["pdf_passages"],
        embedding_model=_required_text(models, "embedding_model"),
        embedding_dimension=_required_int(models, "embedding_dimension"),
        generation_model=_required_text(models, "generation_model"),
        access_mode=_required_text(manifest, "access_mode"),
        contract=contract,
    )


def _read_movies(
    root: Path, release_dir: Path, parent: Mapping[str, object]
) -> dict[str, dict[str, str]]:
    path = _declared_path(root, release_dir, parent, "movies.csv")
    with path.open("r", encoding="utf-8", newline="") as stream:
        rows = list(csv.DictReader(stream))
    movies = {
        str(row.get("movie_id", "")): {field: str(row.get(field, "")) for field in _MOVIE_FIELDS}
        for row in rows
    }
    if len(movies) != len(rows) or not movies or any(not movie_id for movie_id in movies):
        raise RagBundleError("movies artifact has invalid movie IDs")
    return dict(sorted(movies.items()))


def _read_posters(
    root: Path, release_dir: Path, parent: Mapping[str, object]
) -> dict[str, dict[str, object]]:
    path = _declared_path(root, release_dir, parent, "poster_manifest.parquet")
    rows = pq.read_table(path).to_pylist()
    posters = {str(row.get("movie_id", "")): dict(row) for row in rows}
    if len(posters) != len(rows) or any(not movie_id for movie_id in posters):
        raise RagBundleError("poster artifact has invalid movie IDs")
    return dict(sorted(posters.items()))


def _read_facets(
    root: Path,
    release_dir: Path,
    parent: Mapping[str, object],
    movies: Mapping[str, object],
) -> tuple[dict[str, dict[str, object]], dict[str, dict[str, object]]]:
    tier_path, tier_artifact = _declared_artifact_binding(
        root, release_dir, parent, "movie_tiers.parquet"
    )
    pilot_path, pilot_artifact = _declared_artifact_binding(
        root, release_dir, parent, "pilot_movies.parquet"
    )
    tier_rows = pq.read_table(tier_path).to_pylist()
    pilot_rows = pq.read_table(pilot_path).to_pylist()
    tiers: dict[str, dict[str, object]] = {}
    for row in tier_rows:
        movie_id = _required_text(row, "movie_id")
        if movie_id in tiers:
            raise RagBundleError("facet movie membership is not unique")
        tier = _required_text(row, "tier")
        if tier not in _TIERS:
            raise RagBundleError("facet tier vocabulary is invalid")
        pilot_movie = row.get("pilot_movie")
        if not isinstance(pilot_movie, bool):
            raise RagBundleError("facet pilot flag must be boolean")
        tiers[movie_id] = {
            "movie_id": movie_id,
            "tier": tier,
            "tier_reason": _required_text(row, "tier_reason"),
            "human_review": _required_text(row, "human_review"),
            "pilot_movie": pilot_movie,
        }
    pilots: dict[str, dict[str, str]] = {}
    for row in pilot_rows:
        movie_id = _required_text(row, "movie_id")
        if movie_id in pilots:
            raise RagBundleError("facet pilot membership is not unique")
        pilots[movie_id] = {
            key: _required_text(row, key)
            for key in ("handbook_genre", "evidence_note", "evidence_url", "source_record_id")
        }
    if set(tiers) != set(movies) or not tiers:
        raise RagBundleError("facet movie membership does not match movies")
    pilot_ids = set(pilots)
    if not pilot_ids.issubset(movies) or pilot_ids != {
        movie_id for movie_id, row in tiers.items() if row["pilot_movie"] is True
    }:
        raise RagBundleError("facet pilot membership does not match tier flags")
    return (
        {
            movie_id: {**row, "pilot_evidence": pilots.get(movie_id)}
            for movie_id, row in sorted(tiers.items())
        },
        {"movie_tiers": tier_artifact, "pilot_movies": pilot_artifact},
    )


def _supplemental_overlay(
    config: Mapping[str, object],
    schema_version: str,
    parent_movies: Mapping[str, object],
) -> tuple[
    dict[str, dict[str, str]],
    dict[str, dict[str, object]],
    dict[str, dict[str, object]],
]:
    if schema_version != _SUPPLEMENTAL_SCHEMA_VERSION:
        return {}, {}, {}
    raw_movies = config.get("supplemental_movies")
    raw_posters = config.get("supplemental_posters")
    raw_facets = config.get("supplemental_facets")
    if not isinstance(raw_movies, list) or not raw_movies:
        raise RagBundleError("one or more supplemental movies are required")
    if not isinstance(raw_posters, list) or not isinstance(raw_facets, list):
        raise RagBundleError("supplemental poster and facet declarations are required")

    movies: dict[str, dict[str, str]] = {}
    for raw in raw_movies:
        if not isinstance(raw, Mapping):
            raise RagBundleError("supplemental movie must be an object")
        movie = {field: _required_text(raw, field) for field in _MOVIE_FIELDS}
        movie_id = movie["movie_id"]
        if movie_id in parent_movies:
            raise RagBundleError(f"supplemental movie ID collides with parent: {movie_id}")
        if movie_id in movies:
            raise RagBundleError(f"duplicate supplemental movie ID: {movie_id}")
        try:
            date.fromisoformat(movie["release_date"])
            runtime = int(movie["runtime_minutes"])
        except ValueError as exc:
            raise RagBundleError("supplemental movie date or runtime is invalid") from exc
        if runtime <= 0:
            raise RagBundleError("supplemental movie date or runtime is invalid")
        movies[movie_id] = movie

    posters: dict[str, dict[str, object]] = {}
    for raw in raw_posters:
        if not isinstance(raw, Mapping):
            raise RagBundleError("supplemental poster must be an object")
        poster = dict(raw)
        movie_id = _required_text(poster, "movie_id")
        if movie_id in posters:
            raise RagBundleError(f"duplicate supplemental poster ID: {movie_id}")
        posters[movie_id] = poster

    facets: dict[str, dict[str, object]] = {}
    for raw in raw_facets:
        if not isinstance(raw, Mapping):
            raise RagBundleError("supplemental facet must be an object")
        movie_id = _required_text(raw, "movie_id")
        tier = _required_text(raw, "tier")
        if tier not in _TIERS:
            raise RagBundleError("supplemental facet tier vocabulary is invalid")
        pilot_movie = raw.get("pilot_movie")
        if not isinstance(pilot_movie, bool):
            raise RagBundleError("supplemental facet pilot flag must be boolean")
        if movie_id in facets:
            raise RagBundleError(f"duplicate supplemental facet ID: {movie_id}")
        facets[movie_id] = {
            "movie_id": movie_id,
            "tier": tier,
            "tier_reason": _required_text(raw, "tier_reason"),
            "human_review": _required_text(raw, "human_review"),
            "pilot_movie": pilot_movie,
            "pilot_evidence": raw.get("pilot_evidence"),
        }

    supplemental_ids = set(movies)
    if set(posters) != supplemental_ids or set(facets) != supplemental_ids:
        raise RagBundleError(
            "supplemental movie, poster and facet memberships must match"
        )
    return (
        dict(sorted(movies.items())),
        dict(sorted(posters.items())),
        dict(sorted(facets.items())),
    )


def _declared_documents(
    document_root: Path,
    config: Mapping[str, object],
    movies: Mapping[str, object],
    schema_version: str,
) -> list[_Document]:
    raw_documents = config.get("deep_documents")
    if schema_version == _HISTORICAL_SCHEMA_VERSION and (
        not isinstance(raw_documents, list) or len(raw_documents) != 2
    ):
        raise RagBundleError("exactly two deep documents are required")
    if _is_reusable_schema(schema_version) and (
        not isinstance(raw_documents, list) or not raw_documents
    ):
        raise RagBundleError("one or more deep documents are required")
    if schema_version not in _MANIFEST_SCHEMAS:
        raise RagBundleError(f"unsupported RAG manifest schema version: {schema_version}")
    assert isinstance(raw_documents, list)
    documents: list[_Document] = []
    document_ids: set[str] = set()
    source_filenames: set[str] = set()
    source_hashes: set[str] = set()
    for raw in raw_documents:
        if not isinstance(raw, dict):
            raise RagBundleError("deep document must be an object")
        binding: dict[str, object] = {
            key: _required_text(raw, key)
            for key in (
                "movie_id",
                "document_id",
                "source_filename",
                "source_sha256",
                "rights_status",
                "quality_status",
            )
        }
        if binding["movie_id"] not in movies:
            raise RagBundleError("deep document movie is not in the Release")
        document_id = str(binding["document_id"])
        if document_id in document_ids:
            raise RagBundleError(f"duplicate document_id: {document_id}")
        document_ids.add(document_id)
        source_filename = str(binding["source_filename"])
        source_hash = str(binding["source_sha256"])
        if source_filename in source_filenames or source_hash in source_hashes:
            raise RagBundleError(f"duplicate source binding: {binding['source_filename']}")
        source_filenames.add(source_filename)
        source_hashes.add(source_hash)
        profile = (
            _required_text(config, "text_extraction_profile")
            if _is_reusable_schema(schema_version)
            else _HISTORICAL_TEXT_EXTRACTION_PROFILE
        )
        if _is_reusable_schema(schema_version):
            path = _resolve_regular_pdf_source(document_root, source_filename)
            observed_sha256, pages = _extract_stable_pdf_pages(path, profile=profile)
            if observed_sha256 != binding["source_sha256"]:
                raise RagBundleError(f"PDF SHA-256 mismatch: {binding['source_filename']}")
        else:
            path = document_root / source_filename
            _require_under(document_root, path, "PDF source")
            if path.is_symlink() or not path.is_file():
                raise RagBundleError(
                    f"PDF source is not a regular file: {binding['source_filename']}"
                )
            if sha256_file(path) != binding["source_sha256"]:
                raise RagBundleError(f"PDF SHA-256 mismatch: {binding['source_filename']}")
            try:
                pages = extract_pdf_pages(path, profile=profile)
            except PdfExtractError as exc:
                raise RagBundleError(str(exc)) from exc
        if _is_reusable_schema(schema_version):
            page_count = _required_positive_int(raw, "page_count")
            expected_page_numbers = list(range(1, page_count + 1))
            observed_page_numbers = [page.page_number for page in pages]
            if observed_page_numbers != expected_page_numbers:
                raise RagBundleError(
                    f"PDF pages for {binding['document_id']} do not match the exact "
                    f"declared range 1..{page_count}"
                )
            binding["page_count"] = page_count
        documents.append(_Document(binding=binding, pages=pages))
    return sorted(
        documents,
        key=lambda item: (item.binding["movie_id"], item.binding["document_id"]),
    )


def _resolve_regular_pdf_source(document_root: Path, source_filename: str) -> Path:
    if (
        _DIRECT_FILENAME.fullmatch(source_filename) is None
        or Path(source_filename).suffix.casefold() != ".pdf"
    ):
        raise RagBundleError(
            "schema 1.2 PDF source must be a direct filename ending in .pdf"
        )
    path = document_root / source_filename
    _require_under(document_root, path, "PDF source")
    try:
        path_stat = path.lstat()
    except OSError as exc:
        raise RagBundleError(f"PDF source is not a regular file: {source_filename}") from exc
    reparse_flag = getattr(stat, "FILE_ATTRIBUTE_REPARSE_POINT", 0x400)
    file_attributes = getattr(path_stat, "st_file_attributes", 0)
    if (
        path.is_symlink()
        or not stat.S_ISREG(path_stat.st_mode)
        or bool(file_attributes & reparse_flag)
    ):
        raise RagBundleError(f"PDF source is not a regular file: {source_filename}")
    return path


def _extract_stable_pdf_pages(
    path: Path, *, profile: str
) -> tuple[str, tuple[PdfPage, ...]]:
    before_sha256 = sha256_file(path)
    try:
        pages = extract_pdf_pages(path, profile=profile)
    except PdfExtractError as exc:
        raise RagBundleError(str(exc)) from exc
    after_sha256 = sha256_file(path)
    if before_sha256 != after_sha256:
        raise RagBundleError(f"PDF changed during extraction: {path.name}")
    page_numbers = [page.page_number for page in pages]
    if not pages or page_numbers != list(range(1, len(pages) + 1)):
        raise RagBundleError(
            "PDF pages do not match the exact declared range of contiguous "
            f"non-empty pages from 1: {path.name}"
        )
    return before_sha256, pages


def _records(
    root: Path,
    movies: Mapping[str, Mapping[str, str]],
    posters: Mapping[str, Mapping[str, object]],
    facets: Mapping[str, Mapping[str, object]],
    documents: Sequence[_Document],
) -> list[dict[str, object]]:
    records: list[dict[str, object]] = []
    for movie_id, movie in movies.items():
        records.append({"record_kind": "movie", **movie})
        source_poster = dict(posters[movie_id])
        for field in _DERIVED_IDENTITY_FIELDS:
            source_poster.pop(field, None)
        source_is_primary = source_poster.get("is_primary")
        if not isinstance(source_is_primary, bool):
            raise RagBundleError("parent poster is_primary must be boolean")
        poster_record = {
            "record_kind": "poster",
            **source_poster,
            "source_is_primary": source_is_primary,
            "is_primary": True,
            "rag_primary_policy": _RAG_PRIMARY_POLICY,
        }
        poster_record.update(_derived_poster_identity(root, movie_id, source_poster))
        records.append(poster_record)
        facet_body = dict(facets[movie_id])
        records.append(
            {
                "record_kind": "facet",
                **facet_body,
                "content_sha256": hashlib.sha256(_canonical_json(facet_body)).hexdigest(),
            }
        )
        body = "\n".join(f"{field}: {movie[field]}" for field in _MOVIE_FIELDS)
        records.append(
            {
                "record_kind": "passage",
                "passage_kind": "metadata",
                "passage_id": f"metadata:{movie_id}",
                "movie_id": movie_id,
                "document_id": "",
                "page_number": 0,
                "body": body,
                "content_sha256": hashlib.sha256(body.encode("utf-8")).hexdigest(),
            }
        )
    for document in documents:
        binding = document.binding
        records.append({"record_kind": "document", **binding})
        for page in document.pages:
            records.append(
                {
                    "record_kind": "passage",
                    "passage_kind": "pdf",
                    "passage_id": f"pdf:{binding['document_id']}:p{page.page_number}",
                    "movie_id": binding["movie_id"],
                    "document_id": binding["document_id"],
                    "page_number": page.page_number,
                    "body": page.body,
                    "content_sha256": page.body_sha256,
                }
            )
    return sorted(records, key=_record_key)


def _record_key(record: Mapping[str, object]) -> tuple[object, ...]:
    page_number = record.get("page_number", 0)
    if not isinstance(page_number, int) or isinstance(page_number, bool):
        raise RagBundleError("bundle record has invalid page number")
    return (
        _KIND_ORDER[str(record["record_kind"])],
        str(record.get("movie_id", "")),
        str(record.get("document_id", "")),
        page_number,
    )


def _record_counts(records: Sequence[Mapping[str, object]]) -> dict[str, int]:
    return {
        "movies": sum(record["record_kind"] == "movie" for record in records),
        "facet_count": sum(record["record_kind"] == "facet" for record in records),
        "tier_s_count": sum(
            record["record_kind"] == "facet" and record.get("tier") == "S"
            for record in records
        ),
        "tier_a_count": sum(
            record["record_kind"] == "facet" and record.get("tier") == "A"
            for record in records
        ),
        "tier_b_count": sum(
            record["record_kind"] == "facet" and record.get("tier") == "B"
            for record in records
        ),
        "pilot_count": sum(
            record["record_kind"] == "facet" and record.get("pilot_movie") is True
            for record in records
        ),
        "poster_rows": sum(record["record_kind"] == "poster" for record in records),
        "primary_poster_rows": sum(
            record["record_kind"] == "poster" and record.get("is_primary") is True
            for record in records
        ),
        "approved_poster_objects": sum(
            record["record_kind"] == "poster"
            and record.get("quality_status") in _APPROVED_POSTER_STATES
            for record in records
        ),
        "unavailable_poster_rows": sum(
            record["record_kind"] == "poster"
            and record.get("quality_status") in _UNAVAILABLE_POSTER_STATES
            for record in records
        ),
        "derived_poster_bytes": sum(
            _positive_integer(record.get("derived_byte_length"))
            for record in records
            if record["record_kind"] == "poster"
            and record.get("quality_status") in _APPROVED_POSTER_STATES
        ),
        "metadata_passages": sum(record.get("passage_kind") == "metadata" for record in records),
        "documents": sum(record["record_kind"] == "document" for record in records),
        "pdf_passages": sum(record.get("passage_kind") == "pdf" for record in records),
    }


def _require_poster_derivation(records: Sequence[Mapping[str, object]]) -> None:
    posters = [record for record in records if record.get("record_kind") == "poster"]
    if not posters or any(
        record.get("is_primary") is not True
        or record.get("source_is_primary") is not False
        or record.get("rag_primary_policy") != _RAG_PRIMARY_POLICY
        for record in posters
    ):
        raise RagBundleError("bundle poster primary derivation is invalid")
    for record in posters:
        movie_id = record.get("movie_id")
        quality_status = record.get("quality_status")
        if not isinstance(movie_id, str) or not movie_id:
            raise RagBundleError("bundle poster movie identity is invalid")
        if quality_status in _APPROVED_POSTER_STATES:
            if (
                record.get("derived_object_uri")
                != f"assets/posters/derived/{movie_id}.webp"
                or not _valid_sha256(record.get("content_sha256"))
                or not _valid_sha256(record.get("derived_content_sha256"))
                or not _is_valid_poster_byte_length(
                    record.get("derived_byte_length")
                )
                or record.get("derived_mime_type") != _DERIVED_MIME_TYPE
            ):
                raise RagBundleError("bundle approved poster derived identity is invalid")
        elif quality_status in _UNAVAILABLE_POSTER_STATES:
            if record.get("derived_object_uri") != "" or any(
                field in record for field in _DERIVED_IDENTITY_FIELDS
            ):
                raise RagBundleError("bundle unavailable poster has derived identity")
        else:
            raise RagBundleError("bundle poster quality state is invalid")


def _require_facet_derivation(records: Sequence[Mapping[str, object]]) -> None:
    movie_ids = {
        record.get("movie_id") for record in records if record.get("record_kind") == "movie"
    }
    facets = [record for record in records if record.get("record_kind") == "facet"]
    facet_ids = [record.get("movie_id") for record in facets]
    if not facets or set(facet_ids) != movie_ids or len(facet_ids) != len(set(facet_ids)):
        raise RagBundleError("bundle facet movie membership is invalid")
    for record in facets:
        if (
            not isinstance(record.get("movie_id"), str)
            or record.get("tier") not in _TIERS
            or not isinstance(record.get("tier_reason"), str)
            or not record["tier_reason"]
            or not isinstance(record.get("human_review"), str)
            or not record["human_review"]
            or not isinstance(record.get("pilot_movie"), bool)
        ):
            raise RagBundleError("bundle facet evidence is invalid")
        pilot_evidence = record.get("pilot_evidence")
        if record["pilot_movie"]:
            if not isinstance(pilot_evidence, dict) or set(pilot_evidence) != {
                "handbook_genre",
                "evidence_note",
                "evidence_url",
                "source_record_id",
            } or any(not isinstance(value, str) or not value for value in pilot_evidence.values()):
                raise RagBundleError("bundle facet pilot evidence is invalid")
        elif pilot_evidence is not None:
            raise RagBundleError("bundle facet has unexpected pilot evidence")
        body = {
            key: value
            for key, value in record.items()
            if key not in {"record_kind", "content_sha256"}
        }
        if record.get("content_sha256") != hashlib.sha256(_canonical_json(body)).hexdigest():
            raise RagBundleError("bundle facet content hash is invalid")


def _require_facet_artifacts(
    artifacts: Mapping[str, object],
    counts: Mapping[str, int],
    rag_release_id: str,
    *,
    schema_version: str = _REUSABLE_SCHEMA_VERSION,
    supplemental_facet_count: int = 0,
    supplemental_pilot_count: int = 0,
) -> None:
    if set(artifacts) != set(_FACET_ARTIFACT_FILENAMES):
        raise RagBundleError("facet artifact identities are invalid")
    normalized: dict[str, dict[str, object]] = {}
    for name, filename in _FACET_ARTIFACT_FILENAMES.items():
        artifact = _mapping(artifacts, name)
        relative_path = _required_text(artifact, "relative_path")
        sha256 = _required_text(artifact, "sha256")
        if Path(relative_path).name != filename or not _valid_sha256(sha256):
            raise RagBundleError("facet artifact identities are invalid")
        normalized[name] = {
            "relative_path": relative_path,
            "sha256": sha256,
            "row_count": _required_int(artifact, "row_count"),
        }
    expected_facet_artifact_rows = counts["facet_count"]
    expected_pilot_artifact_rows = counts["pilot_count"]
    if schema_version == _SUPPLEMENTAL_SCHEMA_VERSION:
        expected_facet_artifact_rows -= supplemental_facet_count
        expected_pilot_artifact_rows -= supplemental_pilot_count
    if (
        normalized["movie_tiers"]["row_count"] != expected_facet_artifact_rows
        or normalized["pilot_movies"]["row_count"] != expected_pilot_artifact_rows
    ):
        raise RagBundleError("facet artifact counts do not match bundle facets")
    if rag_release_id == _V12_RAG_RELEASE_ID and normalized != _V12_FACET_ARTIFACTS:
        raise RagBundleError("v1.2 facet artifact provenance mismatch")


def _derived_poster_identity(
    root: Path, movie_id: str, poster: Mapping[str, object]
) -> dict[str, object]:
    quality_status = poster.get("quality_status")
    object_uri = poster.get("derived_object_uri")
    if quality_status in _UNAVAILABLE_POSTER_STATES:
        if object_uri != "":
            raise RagBundleError("unavailable poster must not declare a derivative")
        return {}
    if quality_status not in _APPROVED_POSTER_STATES:
        raise RagBundleError("unsupported poster quality state")
    canonical_uri = f"assets/posters/derived/{movie_id}.webp"
    if object_uri != canonical_uri:
        raise RagBundleError("approved poster derivative key is not canonical")
    if not _valid_sha256(poster.get("content_sha256")):
        raise RagBundleError("approved poster original hash is invalid")
    relative = PurePosixPath(canonical_uri)
    candidate = root.joinpath(*relative.parts)
    derived_root = root / "assets" / "posters" / "derived"
    try:
        resolved_root = derived_root.resolve(strict=True)
        resolved = candidate.resolve(strict=True)
    except OSError as exc:
        raise RagBundleError("approved derived poster is missing") from exc
    if (
        not resolved_root.is_relative_to(root)
        or not resolved.is_relative_to(root)
        or candidate.is_symlink()
        or resolved.parent != resolved_root
        or not resolved.is_file()
    ):
        raise RagBundleError("approved derived poster is outside its canonical directory")
    try:
        file_size = resolved.stat().st_size
    except OSError as exc:
        raise RagBundleError("approved derived poster is unreadable") from exc
    if not _is_valid_poster_byte_length(file_size):
        raise RagBundleError("approved derived poster byte length is invalid")
    digest = hashlib.sha256()
    size = 0
    try:
        with resolved.open("rb") as stream:
            header = stream.read(12)
            digest.update(header)
            size += len(header)
            for block in iter(lambda: stream.read(1024 * 1024), b""):
                digest.update(block)
                size += len(block)
                if size > MAX_POSTER_BYTES:
                    raise RagBundleError(
                        "approved derived poster byte length is invalid"
                    )
    except OSError as exc:
        raise RagBundleError("approved derived poster is unreadable") from exc
    if (
        len(header) != 12
        or header[:4] != b"RIFF"
        or header[8:12] != b"WEBP"
        or int.from_bytes(header[4:8], "little") + 8 != size
    ):
        raise RagBundleError("derived poster is not WebP")
    return {
        "derived_content_sha256": digest.hexdigest(),
        "derived_byte_length": size,
        "derived_mime_type": _DERIVED_MIME_TYPE,
    }


def _derived_inventory_sha256(records: Sequence[Mapping[str, object]]) -> str:
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
        for record in records
        if record.get("record_kind") == "poster"
        and record.get("quality_status") in _APPROVED_POSTER_STATES
    ]
    return hashlib.sha256(_canonical_json_list(inventory)).hexdigest()


def _canonical_json_list(value: Sequence[Mapping[str, object]]) -> bytes:
    return json.dumps(value, ensure_ascii=False, sort_keys=True, separators=(",", ":")).encode(
        "utf-8"
    )


def _valid_sha256(value: object) -> bool:
    return isinstance(value, str) and _SHA256.fullmatch(value) is not None


def _require_v12_binding(rag_release_id: str, parent_manifest_sha256: str) -> None:
    if (
        rag_release_id == _V12_RAG_RELEASE_ID
        and parent_manifest_sha256 != _V12_PARENT_MANIFEST_SHA256
    ):
        raise RagBundleError("v1.2 RAG release parent binding mismatch")


def _require_v12_inventory(rag_release_id: str, derived_inventory_sha256: str) -> None:
    if (
        rag_release_id == _V12_RAG_RELEASE_ID
        and derived_inventory_sha256 != _V12_DERIVED_INVENTORY_SHA256
    ):
        raise RagBundleError("v1.2 derived poster inventory mismatch")


def _require_v12_facet_semantics(
    rag_release_id: str, counts: Mapping[str, int]
) -> None:
    if rag_release_id == _V12_RAG_RELEASE_ID and tuple(
        counts[key]
        for key in (
            "facet_count",
            "tier_s_count",
            "tier_a_count",
            "tier_b_count",
            "pilot_count",
        )
    ) != _V12_FACET_SEMANTICS:
        raise RagBundleError("v1.2 facet semantics mismatch")


def _is_valid_poster_byte_length(value: object) -> bool:
    return (
        isinstance(value, int)
        and not isinstance(value, bool)
        and 1 <= value <= MAX_POSTER_BYTES
    )


def _positive_integer(value: object) -> int:
    if not _is_valid_poster_byte_length(value):
        raise RagBundleError("bundle derived poster byte length is invalid")
    assert isinstance(value, int)
    return value


def _declared_artifact_binding(
    root: Path, release_dir: Path, parent: Mapping[str, object], filename: str
) -> tuple[Path, dict[str, object]]:
    path = _declared_path(root, release_dir, parent, filename)
    entry = _declared_artifact_entry(parent, filename)
    return path, {
        "relative_path": _required_text(entry, "relative_path"),
        "sha256": _required_text(entry, "sha256"),
        "row_count": _required_int(entry, "row_count"),
    }


def _declared_path(
    root: Path, release_dir: Path, parent: Mapping[str, object], filename: str
) -> Path:
    entry = _declared_artifact_entry(parent, filename)
    path = root / _required_text(entry, "relative_path")
    _require_under(root, path, filename)
    if path.parent != release_dir or sha256_file(path) != _required_text(entry, "sha256"):
        raise RagBundleError(f"parent artifact mismatch: {filename}")
    return path


def _declared_artifact_entry(parent: Mapping[str, object], filename: str) -> Mapping[str, object]:
    artifacts = parent.get("artifacts")
    if not isinstance(artifacts, list):
        raise RagBundleError("parent release manifest has no artifacts")
    entry = next(
        (
            item
            for item in artifacts
            if isinstance(item, dict) and Path(str(item.get("relative_path", ""))).name == filename
        ),
        None,
    )
    if entry is None:
        raise RagBundleError(f"parent release manifest does not declare {filename}")
    return entry


def _load_config(path: Path) -> Mapping[str, object]:
    try:
        value = yaml.safe_load(path.read_text(encoding="utf-8"))
    except (OSError, yaml.YAMLError) as exc:
        raise RagBundleError("RAG config is unreadable") from exc
    if not isinstance(value, dict):
        raise RagBundleError("RAG config must be an object")
    return value


def _validate_manifest(manifest: Mapping[str, object]) -> None:
    schema_version = manifest.get("schema_version")
    if not isinstance(schema_version, str) or schema_version not in _MANIFEST_SCHEMAS:
        raise RagBundleError(f"unsupported RAG manifest schema version: {schema_version}")
    try:
        schema = json.loads(
            files("hk_movie_rag.schemas")
            .joinpath(_MANIFEST_SCHEMAS[schema_version])
            .read_text(encoding="utf-8")
        )
        Draft202012Validator(schema).validate(manifest)
    except Exception as exc:
        raise RagBundleError(f"RAG release manifest is invalid: {exc}") from exc
    _require_manifest_cross_fields(manifest)


def _is_reusable_schema(schema_version: str) -> bool:
    return schema_version in {
        _REUSABLE_SCHEMA_VERSION,
        _SUPPLEMENTAL_SCHEMA_VERSION,
    }


def _require_manifest_cross_fields(manifest: Mapping[str, object]) -> None:
    documents = manifest.get("documents")
    if not isinstance(documents, list):
        raise RagBundleError("RAG release documents must be an array")
    counts = _mapping(manifest, "counts")
    if _required_int(counts, "documents") != len(documents):
        raise RagBundleError("manifest document count mismatch")
    if not _is_reusable_schema(_required_text(manifest, "schema_version")):
        return
    document_ids: set[str] = set()
    source_filenames: set[str] = set()
    source_hashes: set[str] = set()
    declared_pages = 0
    for document in documents:
        if not isinstance(document, dict):
            raise RagBundleError("manifest document must be an object")
        document_id = _required_text(document, "document_id")
        source_filename = _required_text(document, "source_filename")
        source_hash = _required_text(document, "source_sha256")
        if document_id in document_ids:
            raise RagBundleError(f"duplicate document_id: {document_id}")
        if source_filename in source_filenames or source_hash in source_hashes:
            raise RagBundleError(f"duplicate source binding: {source_filename}")
        document_ids.add(document_id)
        source_filenames.add(source_filename)
        source_hashes.add(source_hash)
        declared_pages += _required_positive_int(document, "page_count")
    if _required_int(counts, "pdf_passages") != declared_pages:
        raise RagBundleError("manifest PDF passage count does not match document page counts")


def _require_document_pages(
    manifest: Mapping[str, object], records: Sequence[Mapping[str, object]]
) -> None:
    schema_version = manifest.get("schema_version")
    if not isinstance(schema_version, str) or not _is_reusable_schema(schema_version):
        return
    documents = manifest.get("documents")
    assert isinstance(documents, list)
    declared = {
        _required_text(document, "document_id"): _required_positive_int(
            document, "page_count"
        )
        for document in documents
        if isinstance(document, dict)
    }
    passage_ids: set[str] = set()
    pages: dict[str, list[int]] = {document_id: [] for document_id in declared}
    for record in records:
        if record.get("record_kind") != "passage":
            continue
        passage_id = _required_text(record, "passage_id")
        if passage_id in passage_ids:
            raise RagBundleError(f"duplicate passage_id: {passage_id}")
        passage_ids.add(passage_id)
        if record.get("passage_kind") != "pdf":
            continue
        document_id = _required_text(record, "document_id")
        if document_id not in declared:
            raise RagBundleError(f"PDF passage has undeclared document_id: {document_id}")
        raw_page_number = record.get("page_number")
        if (
            not isinstance(raw_page_number, int)
            or isinstance(raw_page_number, bool)
            or raw_page_number <= 0
        ):
            raise RagBundleError(
                f"document must contain contiguous positive pages: {document_id}"
            )
        page_number = raw_page_number
        body = record.get("body")
        if not isinstance(body, str) or not body.strip():
            raise RagBundleError(f"empty PDF page: {document_id} page {page_number}")
        expected_passage_id = f"pdf:{document_id}:p{page_number}"
        if passage_id != expected_passage_id:
            raise RagBundleError(f"invalid PDF passage_id: {passage_id}")
        if _required_text(record, "content_sha256") != hashlib.sha256(
            body.encode("utf-8")
        ).hexdigest():
            raise RagBundleError(f"PDF page content SHA-256 mismatch: {passage_id}")
        pages[document_id].append(page_number)
    for document_id, page_count in declared.items():
        if pages[document_id] != list(range(1, page_count + 1)):
            raise RagBundleError(
                f"document must contain contiguous positive pages: {document_id}"
            )


def _require_document_bindings(
    manifest: Mapping[str, object], records: Sequence[Mapping[str, object]]
) -> None:
    schema_version = _required_text(manifest, "schema_version")
    fields = (
        (*_DOCUMENT_BINDING_FIELDS, "page_count")
        if _is_reusable_schema(schema_version)
        else _DOCUMENT_BINDING_FIELDS
    )
    documents = manifest.get("documents")
    assert isinstance(documents, list)
    declared = [
        {field: document[field] for field in fields}
        for document in documents
        if isinstance(document, dict)
    ]
    observed: list[dict[str, object]] = []
    expected_record_fields = {"record_kind", *fields}
    for record in records:
        if record.get("record_kind") != "document":
            continue
        if set(record) != expected_record_fields:
            raise RagBundleError("bundle document bindings do not match manifest")
        binding = {field: record[field] for field in fields}
        if any(
            not isinstance(binding[field], str) or not binding[field]
            for field in _DOCUMENT_BINDING_FIELDS
        ):
            raise RagBundleError("bundle document bindings do not match manifest")
        if _is_reusable_schema(schema_version):
            page_count = binding["page_count"]
            if (
                not isinstance(page_count, int)
                or isinstance(page_count, bool)
                or page_count <= 0
            ):
                raise RagBundleError("bundle document bindings do not match manifest")
        observed.append(binding)
    key = lambda item: (str(item["movie_id"]), str(item["document_id"]))
    if sorted(observed, key=key) != sorted(declared, key=key):
        raise RagBundleError("bundle document bindings do not match manifest")


def _require_supplemental_records(
    manifest: Mapping[str, object], records: Sequence[Mapping[str, object]]
) -> None:
    if manifest.get("schema_version") != _SUPPLEMENTAL_SCHEMA_VERSION:
        return
    raw_movies = manifest.get("supplemental_movies")
    raw_facets = manifest.get("supplemental_facets")
    raw_posters = manifest.get("supplemental_posters")
    if not all(isinstance(value, list) for value in (raw_movies, raw_facets, raw_posters)):
        raise RagBundleError("supplemental manifest declarations must be arrays")
    assert isinstance(raw_movies, list)
    assert isinstance(raw_facets, list)
    assert isinstance(raw_posters, list)
    movies = {
        _required_text(movie, "movie_id"): movie
        for movie in raw_movies
        if isinstance(movie, dict)
    }
    facets = {
        _required_text(facet, "movie_id"): facet
        for facet in raw_facets
        if isinstance(facet, dict)
    }
    posters = {
        _required_text(poster, "movie_id"): poster
        for poster in raw_posters
        if isinstance(poster, dict)
    }
    if not movies or set(movies) != set(facets) or set(movies) != set(posters):
        raise RagBundleError("supplemental manifest memberships do not match")
    by_kind_and_movie = {
        (str(record.get("record_kind")), str(record.get("movie_id"))): record
        for record in records
        if record.get("movie_id") in movies
    }
    for movie_id, movie in movies.items():
        if by_kind_and_movie.get(("movie", movie_id)) != {
            "record_kind": "movie",
            **movie,
        }:
            raise RagBundleError("supplemental movie record does not match manifest")
        facet = facets[movie_id]
        expected_facet = {"record_kind": "facet", **facet}
        expected_facet["content_sha256"] = hashlib.sha256(
            _canonical_json(facet)
        ).hexdigest()
        if by_kind_and_movie.get(("facet", movie_id)) != expected_facet:
            raise RagBundleError("supplemental facet record does not match manifest")
        poster = posters[movie_id]
        observed_poster = by_kind_and_movie.get(("poster", movie_id))
        if observed_poster is None:
            raise RagBundleError("supplemental poster record does not match manifest")
        for key, value in poster.items():
            if key == "is_primary":
                continue
            if observed_poster.get(key) != value:
                raise RagBundleError("supplemental poster record does not match manifest")
        if (
            observed_poster.get("source_is_primary") != poster.get("is_primary")
            or observed_poster.get("is_primary") is not True
        ):
            raise RagBundleError("supplemental poster record does not match manifest")


def _release_contract(
    manifest_sha256: str, manifest: Mapping[str, object]
) -> RagReleaseContract:
    counts = _mapping(manifest, "counts")
    models = _mapping(manifest, "models")
    bundle = _mapping(manifest, "bundle")
    poster_derivation = _mapping(manifest, "poster_derivation")
    schema_version = _required_text(manifest, "schema_version")
    release_counts = RagReleaseCounts(
        movies=_required_int(counts, "movies"),
        facet_count=_required_int(counts, "facet_count"),
        tier_s_count=_required_int(counts, "tier_s_count"),
        tier_a_count=_required_int(counts, "tier_a_count"),
        tier_b_count=_required_int(counts, "tier_b_count"),
        pilot_count=_required_int(counts, "pilot_count"),
        poster_rows=_required_int(counts, "poster_rows"),
        primary_poster_rows=_required_int(counts, "primary_poster_rows"),
        approved_poster_objects=_required_int(counts, "approved_poster_objects"),
        unavailable_poster_rows=_required_int(counts, "unavailable_poster_rows"),
        derived_poster_bytes=_required_int(counts, "derived_poster_bytes"),
        metadata_passages=_required_int(counts, "metadata_passages"),
        documents=_required_int(counts, "documents"),
        pdf_passages=_required_int(counts, "pdf_passages"),
    )
    reusable = _is_reusable_schema(schema_version)
    contract = RagReleaseContract(
        schema_version=schema_version,
        rag_release_id=_required_text(manifest, "rag_release_id"),
        parent_release_manifest_sha256=_required_text(
            manifest, "parent_release_manifest_sha256"
        ),
        manifest_sha256=manifest_sha256,
        bundle_sha256=_required_text(bundle, "sha256"),
        derived_inventory_sha256=_required_text(
            poster_derivation, "derived_inventory_sha256"
        ),
        counts=release_counts,
        embedding_model=_required_text(models, "embedding_model"),
        embedding_dimension=_required_int(models, "embedding_dimension"),
        generation_model=_required_text(models, "generation_model"),
        text_extraction_profile=(
            _required_text(manifest, "text_extraction_profile")
            if reusable
            else _HISTORICAL_TEXT_EXTRACTION_PROFILE
        ),
        document_embedding_profile=(
            _required_text(manifest, "document_embedding_profile")
            if reusable
            else _REUSABLE_DOCUMENT_EMBEDDING_PROFILE
        ),
        relevance_policy_sha256=(
            _required_text(manifest, "relevance_policy_sha256") if reusable else None
        ),
        poster_authority_sha256=(
            _required_text(manifest, "poster_authority_sha256") if reusable else None
        ),
        access_mode=_required_text(manifest, "access_mode"),
    )
    return contract


def _iter_records(path: Path) -> Iterator[dict[str, object]]:
    with path.open("r", encoding="utf-8", newline="") as stream:
        for line in stream:
            yield _load_json(line, "bundle record")


def _require_order(records: Sequence[Mapping[str, object]]) -> None:
    if records != sorted(records, key=_record_key):
        raise RagBundleError("bundle records are not ordered")


def _canonical_json(value: Mapping[str, object]) -> bytes:
    return json.dumps(value, ensure_ascii=False, sort_keys=True, separators=(",", ":")).encode(
        "utf-8"
    )


def _load_json(text: str, label: str) -> dict[str, object]:
    try:
        value = json.loads(text)
    except json.JSONDecodeError as exc:
        raise RagBundleError(f"{label} is invalid JSON") from exc
    if not isinstance(value, dict):
        raise RagBundleError(f"{label} must be an object")
    return value


def _mapping(value: Mapping[str, object], key: str) -> Mapping[str, object]:
    item = value.get(key)
    if not isinstance(item, dict):
        raise RagBundleError(f"field must be an object: {key}")
    return item


def _required_text(value: Mapping[str, object], key: str) -> str:
    item = value.get(key)
    if not isinstance(item, str) or not item:
        raise RagBundleError(f"field must be non-empty text: {key}")
    return item


def _required_int(value: Mapping[str, object], key: str) -> int:
    item = value.get(key)
    if not isinstance(item, int) or isinstance(item, bool) or item < 0:
        raise RagBundleError(f"field must be a non-negative integer: {key}")
    return item


def _required_positive_int(value: Mapping[str, object], key: str) -> int:
    item = _required_int(value, key)
    if item == 0:
        raise RagBundleError(f"field must be a positive integer: {key}")
    return item


def _resolve_document_root(source_root: Path, document_source_root: Path | None) -> Path:
    candidate = source_root if document_source_root is None else document_source_root
    resolved = candidate.resolve()
    if not resolved.is_dir():
        raise RagBundleError("document source root must resolve to a directory")
    return resolved


def _require_under(root: Path, path: Path, label: str) -> None:
    if not path.resolve().is_relative_to(root):
        raise RagBundleError(f"{label} is outside source workspace")
