"""Resumable, release-bound ingestion of a verified RAG bundle."""

from __future__ import annotations

import json
import math
import uuid
from collections.abc import Iterable, Iterator, Mapping, Sequence
from contextlib import contextmanager
from dataclasses import dataclass
from pathlib import Path, PurePosixPath
from tempfile import TemporaryDirectory
from typing import Protocol
from urllib.parse import urlparse

from google.cloud import storage

from .rag_bundle import RagReleaseContract, VerifiedRagBundle, verify_rag_bundle
from .rag_db import (
    EmbeddingReuseResult,
    EmbeddingReuseStats,
    RagDatabaseError,
    ReleaseState,
    ReleaseStats,
)
from .vertex_clients import EmbeddingClient, VertexEmbeddingError


class IngestionError(RuntimeError):
    """Raised when a release cannot be safely completed or resumed."""


class IngestionBundle(Protocol):
    @property
    def contract(self) -> RagReleaseContract: ...

    def iter_records(self) -> Iterable[dict[str, object]]: ...


class IngestionRepository(Protocol):
    def begin_release(self, contract: RagReleaseContract) -> str: ...

    def release_state(self, release_id: str) -> ReleaseState: ...

    def assert_active_bundle_matches(
        self,
        release_id: str,
        records: Iterable[Mapping[str, object]],
        *,
        contract: RagReleaseContract | None = None,
    ) -> None: ...

    def upsert_bundle_records(
        self, release_id: str, records: Iterable[Mapping[str, object]]
    ) -> None: ...

    def reconcile_facets(
        self, release_id: str, records: Iterable[Mapping[str, object]]
    ) -> None: ...

    def start_ingestion_run(self, release_id: str, run_id: str) -> None: ...

    def pending_passages(self, release_id: str, limit: int = 100) -> list[dict[str, object]]: ...

    def reuse_embeddings(
        self, target_release_id: str, source_release_id: str
    ) -> EmbeddingReuseResult: ...

    def embedding_reuse_stats(
        self, target_release_id: str, source_release_id: str
    ) -> EmbeddingReuseStats: ...

    def store_embedding(
        self,
        release_id: str,
        passage_id: str,
        content_sha256: str,
        embedding: Sequence[float],
        *,
        embedding_model: str,
        embedding_dimension: int,
    ) -> None: ...

    def activate_release(
        self,
        release_id: str,
        *,
        embedding_model: str,
        embedding_dimension: int,
    ) -> None: ...

    def finish_ingestion_run(
        self,
        release_id: str,
        run_id: str,
        *,
        status: str,
        embedded_count: int,
        skipped_count: int,
        error_summary: str | None,
    ) -> None: ...

    def release_stats(self, release_id: str) -> ReleaseStats: ...


@dataclass(frozen=True)
class IngestionSummary:
    release_id: str
    run_id: str
    preexisting: int
    copied_this_run: int
    embedded_this_run: int
    skipped_this_run: int
    source_eligible_total: int
    non_reusable_embedded_total: int
    active: bool
    error_summary: str | None = None

    @property
    def embedded(self) -> int:
        return self.embedded_this_run

    @property
    def skipped(self) -> int:
        return self.skipped_this_run


@contextmanager
def verified_bundle_source(
    source: str | Path, *, project_id: str
) -> Iterator[VerifiedRagBundle]:
    """Materialize only a declared local/GCS manifest and bundle, then verify both."""
    source_text = str(source)
    if not source_text.startswith("gs://"):
        yield verify_rag_bundle(Path(source_text))
        return
    parsed = urlparse(source_text)
    object_name = parsed.path.lstrip("/")
    if (
        parsed.scheme != "gs"
        or not parsed.netloc
        or not object_name
        or parsed.query
        or parsed.fragment
    ):
        raise IngestionError("invalid GCS RAG manifest URI")
    with TemporaryDirectory(prefix="hk-movie-rag-bundle-") as temporary:
        root = Path(temporary)
        manifest_path = root / "rag_release_manifest.json"
        bucket = storage.Client(project=project_id).bucket(parsed.netloc)
        try:
            bucket.blob(object_name).download_to_filename(str(manifest_path))
            manifest = json.loads(manifest_path.read_text(encoding="utf-8"))
            if not isinstance(manifest, dict):
                raise TypeError("manifest must be an object")
            bundle = manifest.get("bundle")
            if not isinstance(bundle, dict):
                raise TypeError("manifest bundle is missing")
            filename = bundle.get("filename")
            if (
                not isinstance(filename, str)
                or not filename
                or PurePosixPath(filename).name != filename
            ):
                raise ValueError("manifest bundle filename is unsafe")
            bundle_object = (PurePosixPath(object_name).parent / filename).as_posix()
            bucket.blob(bundle_object).download_to_filename(str(root / filename))
        except IngestionError:
            raise
        except Exception as exc:
            raise IngestionError("RAG bundle download failed") from exc
        yield verify_rag_bundle(manifest_path)


def ingest_bundle(
    bundle: IngestionBundle,
    repository: IngestionRepository,
    embedding_client: EmbeddingClient,
    *,
    require_existing_active: bool = False,
    reuse_from_release_id: str | None = None,
    require_source_eligible_total: int | None = None,
    require_non_reusable_embedded_total: int | None = None,
) -> IngestionSummary:
    """Load bundle records and immediately persist only missing passage embeddings."""
    contract = bundle.contract
    _assert_client_matches_contract(contract, embedding_client)
    _validate_reuse_acceptance(
        contract.rag_release_id,
        reuse_from_release_id,
        require_source_eligible_total,
        require_non_reusable_embedded_total,
    )
    expected_passages = contract.expected_embeddings
    active_bundle_checked = False
    try:
        existing_state = repository.release_state(contract.rag_release_id)
    except RagDatabaseError:
        existing_state = None
    if require_existing_active and (
        existing_state is None
        or existing_state.status != "active"
        or existing_state.embeddings != expected_passages
    ):
        raise IngestionError("existing active release precondition failed")
    if existing_state is not None and existing_state.status == "active":
        try:
            _assert_release_matches_contract(
                contract,
                existing_state,
                allow_legacy_identity=True,
            )
            repository.assert_active_bundle_matches(
                contract.rag_release_id,
                bundle.iter_records(),
                contract=contract,
            )
        except (RagDatabaseError, ValueError) as exc:
            message = (
                "existing active release precondition failed"
                if require_existing_active
                else str(exc)
            )
            raise IngestionError(message) from exc
        active_bundle_checked = True
    try:
        release_id = contract.rag_release_id
        if active_bundle_checked:
            state = repository.release_state(release_id)
        else:
            release_id = repository.begin_release(contract)
            state = repository.release_state(release_id)
        _assert_release_matches_contract(contract, state)
    except (RagDatabaseError, ValueError) as exc:
        raise IngestionError(str(exc)) from exc
    if state.embeddings > expected_passages:
        raise IngestionError("release contains more embeddings than declared passages")
    if state.status == "active" and not active_bundle_checked:
        try:
            repository.assert_active_bundle_matches(
                release_id,
                bundle.iter_records(),
                contract=contract,
            )
        except RagDatabaseError as exc:
            raise IngestionError(str(exc)) from exc
        active_bundle_checked = True

    if state.status == "active":
        try:
            _assert_active_stats(bundle, repository.release_stats(release_id))
            source_eligible_total = 0
            non_reusable_embedded_total = expected_passages
            if reuse_from_release_id is not None:
                reuse_stats = repository.embedding_reuse_stats(
                    release_id,
                    reuse_from_release_id,
                )
                source_eligible_total = reuse_stats.source_eligible_total
                non_reusable_embedded_total = reuse_stats.non_reusable_embedded_total
                _assert_reuse_stats(
                    reuse_stats,
                    require_source_eligible_total,
                    require_non_reusable_embedded_total,
                )
        except (RagDatabaseError, ValueError) as exc:
            raise IngestionError(str(exc)) from exc
        return IngestionSummary(
            release_id=release_id,
            run_id="",
            preexisting=state.embeddings,
            copied_this_run=0,
            embedded_this_run=0,
            skipped_this_run=state.embeddings,
            source_eligible_total=source_eligible_total,
            non_reusable_embedded_total=non_reusable_embedded_total,
            active=True,
        )

    run_id = uuid.uuid4().hex
    try:
        repository.start_ingestion_run(release_id, run_id)
    except RagDatabaseError as exc:
        raise IngestionError(str(exc)) from exc

    preexisting = state.embeddings
    copied_this_run = 0
    embedded_this_run = 0
    source_eligible_total = 0
    non_reusable_embedded_total = 0
    try:
        if state.status == "loading":
            repository.upsert_bundle_records(
                release_id,
                (record for record in bundle.iter_records() if record.get("record_kind") != "facet"),
            )
            repository.reconcile_facets(
                release_id,
                (
                    record
                    for record in bundle.iter_records()
                    if record.get("record_kind") == "facet"
                ),
            )
        if state.status == "loading":
            if reuse_from_release_id is not None:
                reuse_result = repository.reuse_embeddings(
                    release_id,
                    reuse_from_release_id,
                )
                copied_this_run = reuse_result.copied_this_run
                source_eligible_total = reuse_result.source_eligible_total
                if source_eligible_total != require_source_eligible_total:
                    raise IngestionError(
                        "source-eligible embedding count does not match acceptance"
                    )
            seen_pending: set[str] = set()
            while pending := repository.pending_passages(release_id, limit=100):
                for passage in pending:
                    passage_id = _required_text(passage, "passage_id")
                    if passage_id in seen_pending:
                        raise IngestionError("pending passage did not persist")
                    seen_pending.add(passage_id)
                    vector = embedding_client.embed_document(
                        _required_text(passage, "body"),
                        title=_required_text(passage, "title"),
                    )
                    repository.store_embedding(
                        release_id,
                        passage_id,
                        _required_text(passage, "content_sha256"),
                        _validated_embedding(vector, contract.embedding_dimension),
                        embedding_model=contract.embedding_model,
                        embedding_dimension=contract.embedding_dimension,
                    )
                    embedded_this_run += 1
            if preexisting + copied_this_run + embedded_this_run != expected_passages:
                raise IngestionError(
                    "embedded passage count does not match the verified bundle"
                )
            if reuse_from_release_id is not None:
                reuse_stats = repository.embedding_reuse_stats(
                    release_id,
                    reuse_from_release_id,
                )
                source_eligible_total = reuse_stats.source_eligible_total
                non_reusable_embedded_total = reuse_stats.non_reusable_embedded_total
                _assert_reuse_stats(
                    reuse_stats,
                    require_source_eligible_total,
                    require_non_reusable_embedded_total,
                )
            repository.activate_release(
                release_id,
                embedding_model=contract.embedding_model,
                embedding_dimension=contract.embedding_dimension,
            )
        else:
            raise IngestionError("failed release cannot be ingested")
        if reuse_from_release_id is None:
            non_reusable_embedded_total = expected_passages
        skipped_this_run = preexisting + copied_this_run
        repository.finish_ingestion_run(
            release_id,
            run_id,
            status="complete",
            embedded_count=embedded_this_run,
            skipped_count=skipped_this_run,
            error_summary=None,
        )
    except Exception as exc:
        summary = _safe_error_summary(exc)
        try:
            repository.finish_ingestion_run(
                release_id,
                run_id,
                status="failed",
                embedded_count=embedded_this_run,
                skipped_count=preexisting + copied_this_run,
                error_summary=summary,
            )
        except RagDatabaseError as recording_error:
            exc.add_note(
                "ingestion run failure could not be recorded: "
                f"{type(recording_error).__name__}"
            )
        raise IngestionError(summary) from exc
    return IngestionSummary(
        release_id=release_id,
        run_id=run_id,
        preexisting=preexisting,
        copied_this_run=copied_this_run,
        embedded_this_run=embedded_this_run,
        skipped_this_run=skipped_this_run,
        source_eligible_total=source_eligible_total,
        non_reusable_embedded_total=non_reusable_embedded_total,
        active=True,
    )


def _assert_client_matches_contract(
    contract: RagReleaseContract, embedding_client: EmbeddingClient
) -> None:
    if embedding_client.model != contract.embedding_model:
        raise IngestionError("embedding client model does not match bundle")
    if embedding_client.dimension != contract.embedding_dimension:
        raise IngestionError("embedding client dimension does not match bundle")
    if contract.embedding_dimension != 768:
        raise IngestionError("bundle embedding dimension must be 768")


def _assert_release_matches_contract(
    contract: RagReleaseContract,
    state: ReleaseState,
    *,
    allow_legacy_identity: bool = False,
) -> None:
    if state.embedding_model != contract.embedding_model:
        if state.embeddings:
            raise IngestionError("embedding model is immutable after first stored embedding")
        raise IngestionError("release embedding model does not match bundle")
    if state.embedding_dimension != contract.embedding_dimension:
        raise IngestionError("release embedding dimension does not match bundle")
    identity = (state.manifest_sha256, state.document_embedding_profile)
    if allow_legacy_identity and identity == (None, None):
        return
    if identity != (contract.manifest_sha256, contract.document_embedding_profile):
        raise IngestionError("release manifest identity does not match bundle")


def _assert_active_stats(bundle: IngestionBundle, stats: ReleaseStats) -> None:
    counts = bundle.contract.counts
    observed = (
        stats.movies,
        stats.assets,
        stats.metadata_passages,
        stats.documents,
        stats.pdf_passages,
        stats.embeddings,
    )
    expected = (
        counts.movies,
        counts.poster_rows,
        counts.metadata_passages,
        counts.documents,
        counts.pdf_passages,
        bundle.contract.expected_embeddings,
    )
    if observed != expected:
        raise IngestionError("active release statistics do not match the verified bundle")


def _validate_reuse_acceptance(
    target_release_id: str,
    reuse_from_release_id: str | None,
    required_source_eligible_total: int | None,
    required_non_reusable_embedded_total: int | None,
) -> None:
    values = (required_source_eligible_total, required_non_reusable_embedded_total)
    if reuse_from_release_id is None:
        if values != (None, None):
            raise IngestionError("reuse acceptance counts require a source release")
        return
    if not reuse_from_release_id or reuse_from_release_id == target_release_id:
        raise IngestionError("reuse source and target releases must be distinct")
    if any(value is None for value in values):
        raise IngestionError("reuse acceptance counts must be provided together")
    if any(
        not isinstance(value, int) or isinstance(value, bool) or value < 0
        for value in values
    ):
        raise IngestionError("reuse acceptance counts must be non-negative integers")


def _assert_reuse_stats(
    stats: EmbeddingReuseStats,
    required_source_eligible_total: int | None,
    required_non_reusable_embedded_total: int | None,
) -> None:
    if stats.source_eligible_total != required_source_eligible_total:
        raise IngestionError("source-eligible embedding count does not match acceptance")
    if stats.non_reusable_embedded_total != required_non_reusable_embedded_total:
        raise IngestionError("non-reusable embedding count does not match acceptance")


def _required_text(value: Mapping[str, object], key: str) -> str:
    observed = value.get(key)
    if not isinstance(observed, str) or not observed:
        raise IngestionError(f"pending passage field must be non-empty text: {key}")
    return observed


def _validated_embedding(values: Sequence[float], dimension: int) -> list[float]:
    try:
        vector = [float(value) for value in values if not isinstance(value, bool)]
    except (TypeError, ValueError, OverflowError) as exc:
        raise IngestionError("embedding must contain exactly 768 finite floats") from exc
    if len(values) != dimension or len(vector) != dimension or not all(
        math.isfinite(value) for value in vector
    ):
        raise IngestionError("embedding must contain exactly 768 finite floats")
    return vector


def _safe_error_summary(exc: Exception) -> str:
    if isinstance(exc, (IngestionError, RagDatabaseError, VertexEmbeddingError)):
        summary = str(exc)
    else:
        summary = f"ingestion failed ({type(exc).__name__})"
    return summary[:512] or "ingestion failed"
