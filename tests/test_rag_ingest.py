"""Resumable Vertex embedding ingestion contracts for the minimum RAG demo."""

from __future__ import annotations

import hashlib
import json
import sys
from collections.abc import Iterator, Mapping
from contextlib import contextmanager
from dataclasses import dataclass, field, replace
from pathlib import Path
from types import SimpleNamespace

import pytest

from hk_movie_rag.rag_bundle import RagReleaseContract, RagReleaseCounts
from hk_movie_rag.rag_db import RagDatabaseError, ReleaseState, ReleaseStats
from hk_movie_rag.rag_ingest import (
    IngestionError,
    IngestionSummary,
    ingest_bundle,
    verified_bundle_source,
)
from hk_movie_rag.vertex_clients import VertexEmbeddingClient, VertexEmbeddingError

EXPECTED_PASSAGES = 4664


@dataclass(frozen=True)
class FakeBundle:
    rag_release_id: str = "v1.2-demo"
    movie_count: int = 4658
    poster_row_count: int = 4658
    primary_poster_row_count: int = 4658
    approved_poster_object_count: int = 4545
    unavailable_poster_row_count: int = 113
    metadata_passage_count: int = 4658
    document_count: int = 2
    pdf_passage_count: int = 6
    embedding_model: str = "gemini-embedding-2"
    embedding_dimension: int = 768
    generation_model: str = "gemini-3.5-flash-lite"
    access_mode: str = "restricted_demo"
    manifest_sha256: str = "a" * 64
    document_embedding_profile: str = "vertex-title-text-v1"
    records: tuple[Mapping[str, object], ...] = ()

    @property
    def contract(self) -> RagReleaseContract:
        return RagReleaseContract(
            schema_version="1.2",
            rag_release_id=self.rag_release_id,
            parent_release_manifest_sha256="b" * 64,
            manifest_sha256=self.manifest_sha256,
            bundle_sha256="c" * 64,
            derived_inventory_sha256="d" * 64,
            counts=RagReleaseCounts(
                movies=self.movie_count,
                facet_count=4658,
                tier_s_count=50,
                tier_a_count=313,
                tier_b_count=4295,
                pilot_count=24,
                poster_rows=self.poster_row_count,
                primary_poster_rows=self.primary_poster_row_count,
                approved_poster_objects=self.approved_poster_object_count,
                unavailable_poster_rows=self.unavailable_poster_row_count,
                derived_poster_bytes=1,
                metadata_passages=self.metadata_passage_count,
                documents=self.document_count,
                pdf_passages=self.pdf_passage_count,
            ),
            embedding_model=self.embedding_model,
            embedding_dimension=self.embedding_dimension,
            generation_model=self.generation_model,
            text_extraction_profile="cjk-layout-v1",
            document_embedding_profile=self.document_embedding_profile,
            relevance_policy_sha256="d" * 64,
            poster_authority_sha256="e" * 64,
            access_mode=self.access_mode,
        )

    def iter_records(self) -> Iterator[dict[str, object]]:
        for record in self.records:
            yield dict(record)


@dataclass
class FakeEmbeddingClient:
    model: str = "gemini-embedding-2"
    dimension: int = 768
    fail_on_call: int | None = None
    document_calls: list[tuple[str, str]] = field(default_factory=list)

    def embed_document(self, text: str, *, title: str) -> list[float]:
        self.document_calls.append((text, title))
        if self.fail_on_call == len(self.document_calls):
            raise VertexEmbeddingError("embedding request failed (status 400)")
        return [0.25] * self.dimension

    def embed_query(self, text: str) -> list[float]:
        return [0.5] * self.dimension


@dataclass
class FakeRepository:
    existing_embeddings: int = 0
    status: str = "loading"
    model: str = "gemini-embedding-2"
    dimension: int = 768
    pending: list[dict[str, object]] = field(default_factory=list)
    upserted: bool = False
    upserted_records: list[dict[str, object]] = field(default_factory=list)
    stored: list[str] = field(default_factory=list)
    reconciled_facets: list[dict[str, object]] = field(default_factory=list)
    reconcile_calls: int = 0
    started_runs: list[str] = field(default_factory=list)
    finished_runs: list[dict[str, object]] = field(default_factory=list)
    begin_calls: int = 0
    persisted_non_facet_records: tuple[Mapping[str, object], ...] | None = None
    active_bundle_checks: list[list[dict[str, object]]] = field(default_factory=list)
    manifest_sha256: str = "a" * 64
    embedding_profile: str = "vertex-title-text-v1"
    copied_on_reuse: int = 0
    source_eligible_total: int = 0
    non_reusable_embedded_total: int = 0
    reuse_calls: list[tuple[str, str]] = field(default_factory=list)
    current_contract: RagReleaseContract | None = None

    def begin_release(self, contract: RagReleaseContract) -> str:
        self.begin_calls += 1
        if self.model != contract.embedding_model:
            raise RagDatabaseError("embedding model is immutable")
        if self.dimension != contract.embedding_dimension:
            raise RagDatabaseError("embedding dimension is immutable")
        if self.manifest_sha256 != contract.manifest_sha256:
            raise RagDatabaseError("release contract is immutable")
        if self.embedding_profile != contract.document_embedding_profile:
            raise RagDatabaseError("release contract is immutable")
        self.current_contract = contract
        return contract.rag_release_id

    def release_state(self, release_id: str) -> ReleaseState:
        return ReleaseState(
            self.status,
            self.model,
            self.dimension,
            self.existing_embeddings,
            self.manifest_sha256,
            self.embedding_profile,
        )

    def assert_active_bundle_matches(
        self,
        release_id: str,
        records: Iterator[dict[str, object]],
        *,
        contract: RagReleaseContract | None = None,
    ) -> None:
        assert contract is not None
        assert release_id == contract.rag_release_id
        self.current_contract = contract
        observed = [dict(record) for record in records if record.get("record_kind") != "facet"]
        self.active_bundle_checks.append(observed)
        if self.persisted_non_facet_records is not None and observed != [
            dict(record) for record in self.persisted_non_facet_records
        ]:
            raise RagDatabaseError("active bundle content conflict")

    def upsert_bundle_records(
        self, release_id: str, records: Iterator[dict[str, object]]
    ) -> None:
        assert self.current_contract is None or release_id == self.current_contract.rag_release_id
        self.upserted_records = list(records)
        self.upserted = True

    def reconcile_facets(
        self, release_id: str, records: Iterator[dict[str, object]]
    ) -> None:
        self.reconcile_calls += 1
        assert self.current_contract is None or release_id == self.current_contract.rag_release_id
        self.reconciled_facets = list(records)
        if self.status == "loading" and self.reconciled_facets and not any(
            record.get("record_kind") == "movie" for record in self.upserted_records
        ):
            raise RagDatabaseError("facet movie foreign key is unavailable")

    def start_ingestion_run(self, release_id: str, run_id: str) -> None:
        assert self.current_contract is None or release_id == self.current_contract.rag_release_id
        self.started_runs.append(run_id)

    def pending_passages(self, release_id: str, limit: int = 100) -> list[dict[str, object]]:
        assert self.current_contract is None or release_id == self.current_contract.rag_release_id
        return self.pending[:limit]

    def reuse_embeddings(
        self, target_release_id: str, source_release_id: str
    ) -> SimpleNamespace:
        self.reuse_calls.append((target_release_id, source_release_id))
        self.existing_embeddings += self.copied_on_reuse
        return SimpleNamespace(
            source_eligible_total=self.source_eligible_total,
            copied_this_run=self.copied_on_reuse,
        )

    def embedding_reuse_stats(
        self, target_release_id: str, source_release_id: str
    ) -> SimpleNamespace:
        return SimpleNamespace(
            source_eligible_total=self.source_eligible_total,
            non_reusable_embedded_total=self.non_reusable_embedded_total,
        )

    def store_embedding(
        self,
        release_id: str,
        passage_id: str,
        content_sha256: str,
        embedding: list[float],
        *,
        embedding_model: str,
        embedding_dimension: int,
    ) -> None:
        assert self.current_contract is None or release_id == self.current_contract.rag_release_id
        assert embedding_model == self.model
        assert embedding_dimension == self.dimension
        passage = next(item for item in self.pending if item["passage_id"] == passage_id)
        assert passage["content_sha256"] == content_sha256
        assert len(embedding) == 768
        self.pending.remove(passage)
        self.stored.append(passage_id)
        self.existing_embeddings += 1
        self.non_reusable_embedded_total += 1

    def activate_release(
        self,
        release_id: str,
        *,
        embedding_model: str,
        embedding_dimension: int,
    ) -> None:
        assert self.current_contract is None or release_id == self.current_contract.rag_release_id
        assert embedding_model == self.model
        assert embedding_dimension == self.dimension
        contract = self.current_contract or FakeBundle().contract
        assert self.existing_embeddings == contract.expected_embeddings
        self.status = "active"

    def finish_ingestion_run(
        self,
        release_id: str,
        run_id: str,
        *,
        status: str,
        embedded_count: int,
        skipped_count: int,
        error_summary: str | None,
    ) -> None:
        assert self.current_contract is None or release_id == self.current_contract.rag_release_id
        self.finished_runs.append(
            {
                "run_id": run_id,
                "status": status,
                "embedded_count": embedded_count,
                "skipped_count": skipped_count,
                "error_summary": error_summary,
            }
        )

    def release_stats(self, release_id: str) -> ReleaseStats:
        contract = self.current_contract or FakeBundle().contract
        counts = contract.counts
        return ReleaseStats(
            counts.movies,
            counts.poster_rows,
            counts.metadata_passages,
            counts.documents,
            counts.pdf_passages,
            self.existing_embeddings,
        )


def _passages(count: int) -> list[dict[str, object]]:
    return [
        {
            "passage_id": f"metadata:movie-{index:04d}",
            "movie_id": f"movie-{index:04d}",
            "passage_kind": "metadata",
            "title": f"電影 {index}",
            "body": f"電影資料 {index}",
            "content_sha256": f"{index:064x}",
        }
        for index in range(count)
    ]


def test_ingestion_resumes_only_missing_embeddings_and_persists_each_one() -> None:
    repository = FakeRepository(existing_embeddings=4650, pending=_passages(14))
    embedding_client = FakeEmbeddingClient()

    result = ingest_bundle(FakeBundle(), repository, embedding_client)

    assert result.embedded == 14
    assert result.skipped == 4650
    assert result.active is True
    assert len(embedding_client.document_calls) == 14
    assert repository.stored == [f"metadata:movie-{index:04d}" for index in range(14)]
    assert repository.status == "active"
    assert repository.finished_runs == [
        {
            "run_id": result.run_id,
            "status": "complete",
            "embedded_count": 14,
            "skipped_count": 4650,
            "error_summary": None,
        }
    ]


def test_ingestion_passes_semantic_title_and_unmodified_body_to_client() -> None:
    repository = FakeRepository(existing_embeddings=4663, pending=_passages(1))
    embedding_client = FakeEmbeddingClient()

    ingest_bundle(FakeBundle(), repository, embedding_client)

    assert embedding_client.document_calls == [("電影資料 0", "電影 0")]


def test_ingestion_never_switches_model_after_first_embedding() -> None:
    repository = FakeRepository(existing_embeddings=1, model="gemini-embedding-2")
    fallback_bundle = replace(FakeBundle(), embedding_model="gemini-embedding-001")
    fallback_client = FakeEmbeddingClient(model="gemini-embedding-001")

    with pytest.raises(IngestionError, match="embedding model is immutable"):
        ingest_bundle(fallback_bundle, repository, fallback_client)

    assert repository.started_runs == []
    assert fallback_client.document_calls == []


@dataclass
class ModelSwitchingRepository(FakeRepository):
    """Simulates a competing fallback worker after initial model validation."""

    switched: bool = False

    def pending_passages(self, release_id: str, limit: int = 100) -> list[dict[str, object]]:
        if not self.switched:
            self.model = "gemini-embedding-001"
            self.switched = True
        return super().pending_passages(release_id, limit)

    def store_embedding(
        self,
        release_id: str,
        passage_id: str,
        content_sha256: str,
        embedding: list[float],
        *,
        embedding_model: str,
        embedding_dimension: int,
    ) -> None:
        if self.model != embedding_model or self.dimension != embedding_dimension:
            raise RagDatabaseError("release embedding contract changed before persistence")
        super().store_embedding(
            release_id,
            passage_id,
            content_sha256,
            embedding,
            embedding_model=embedding_model,
            embedding_dimension=embedding_dimension,
        )


def test_ingestion_rechecks_model_inside_every_embedding_write_transaction() -> None:
    repository = ModelSwitchingRepository(
        existing_embeddings=4663,
        pending=_passages(1),
    )
    embedding_client = FakeEmbeddingClient()

    with pytest.raises(IngestionError, match="embedding contract changed"):
        ingest_bundle(FakeBundle(), repository, embedding_client)

    assert repository.model == "gemini-embedding-001"
    assert repository.status == "loading"
    assert repository.stored == []
    assert repository.finished_runs[0]["status"] == "failed"
    assert repository.finished_runs[0]["error_summary"] == (
        "release embedding contract changed before persistence"
    )


def test_ingestion_rejects_client_model_or_dimension_drift_before_writes() -> None:
    repository = FakeRepository()

    with pytest.raises(IngestionError, match="client model does not match bundle"):
        ingest_bundle(
            FakeBundle(), repository, FakeEmbeddingClient(model="gemini-embedding-001")
        )
    with pytest.raises(IngestionError, match="client dimension does not match bundle"):
        ingest_bundle(FakeBundle(), repository, FakeEmbeddingClient(dimension=1536))

    assert repository.started_runs == []
    assert repository.upserted is False


def test_active_release_rerun_is_idempotent_and_does_not_upsert_or_embed() -> None:
    repository = FakeRepository(existing_embeddings=EXPECTED_PASSAGES, status="active")
    embedding_client = FakeEmbeddingClient()

    result = ingest_bundle(FakeBundle(), repository, embedding_client)

    assert result.embedded == 0
    assert result.skipped == EXPECTED_PASSAGES
    assert result.active is True
    assert repository.upserted is False
    assert repository.reconcile_calls == 0
    assert repository.begin_calls == 0
    assert repository.started_runs == []
    assert repository.finished_runs == []
    assert result.run_id == ""
    assert embedding_client.document_calls == []


def _r2_bundle() -> FakeBundle:
    return replace(
        FakeBundle(),
        rag_release_id="v1.2-demo-r2",
        document_count=3,
        pdf_passage_count=12,
        manifest_sha256="f" * 64,
    )


def _r2_repository(**changes: object) -> FakeRepository:
    values: dict[str, object] = {
        "manifest_sha256": "f" * 64,
        "source_eligible_total": 4658,
        **changes,
    }
    return FakeRepository(**values)  # type: ignore[arg-type]


def test_reuse_clean_run_copies_metadata_and_embeds_only_non_reusable_pages() -> None:
    """Breaks if a clean R2 run calls Vertex for reusable metadata or reports deltas as state."""
    repository = _r2_repository(copied_on_reuse=4658, pending=_passages(12))
    client = FakeEmbeddingClient()

    result = ingest_bundle(
        _r2_bundle(),
        repository,
        client,
        reuse_from_release_id="v1.2-demo",
        require_source_eligible_total=4658,
        require_non_reusable_embedded_total=12,
    )

    assert result.preexisting == 0
    assert result.copied_this_run == 4658
    assert result.embedded_this_run == 12
    assert result.skipped_this_run == 4658
    assert result.source_eligible_total == 4658
    assert result.non_reusable_embedded_total == 12
    assert repository.reuse_calls == [("v1.2-demo-r2", "v1.2-demo")]
    assert len(client.document_calls) == 12


def test_reuse_resume_acceptance_uses_final_state_not_clean_run_copy_delta() -> None:
    """Breaks if resume incorrectly requires all 4,658 vectors to be copied again."""
    repository = _r2_repository(
        existing_embeddings=4665,
        copied_on_reuse=0,
        pending=_passages(5),
        non_reusable_embedded_total=7,
    )
    client = FakeEmbeddingClient()

    result = ingest_bundle(
        _r2_bundle(),
        repository,
        client,
        reuse_from_release_id="v1.2-demo",
        require_source_eligible_total=4658,
        require_non_reusable_embedded_total=12,
    )

    assert result.preexisting == 4665
    assert result.copied_this_run == 0
    assert result.embedded_this_run == 5
    assert result.skipped_this_run == 4665
    assert result.source_eligible_total == 4658
    assert result.non_reusable_embedded_total == 12
    assert len(client.document_calls) == 5


def test_reuse_fails_final_source_eligibility_before_first_vertex_call() -> None:
    """Breaks if an unsafe zero-copy result silently falls back to full re-embedding."""
    repository = _r2_repository(
        source_eligible_total=0,
        copied_on_reuse=0,
        pending=_passages(4670),
    )
    client = FakeEmbeddingClient()

    with pytest.raises(IngestionError, match="source-eligible embedding count"):
        ingest_bundle(
            _r2_bundle(),
            repository,
            client,
            reuse_from_release_id="v1.2-demo",
            require_source_eligible_total=4658,
            require_non_reusable_embedded_total=12,
        )

    assert client.document_calls == []
    assert repository.stored == []


def test_reuse_final_state_failure_cannot_activate_target_release() -> None:
    """Breaks if final reuse acceptance runs only after the loading release activates."""
    repository = _r2_repository(
        copied_on_reuse=4658,
        pending=_passages(12),
        non_reusable_embedded_total=-1,
    )

    with pytest.raises(IngestionError, match="non-reusable embedding count"):
        ingest_bundle(
            _r2_bundle(),
            repository,
            FakeEmbeddingClient(),
            reuse_from_release_id="v1.2-demo",
            require_source_eligible_total=4658,
            require_non_reusable_embedded_total=12,
        )

    assert repository.status == "loading"


def test_active_r2_rerun_reports_all_passages_skipped_without_reuse_or_vertex() -> None:
    repository = _r2_repository(
        existing_embeddings=4670,
        status="active",
        source_eligible_total=4658,
        non_reusable_embedded_total=12,
    )
    client = FakeEmbeddingClient()

    result = ingest_bundle(
        _r2_bundle(),
        repository,
        client,
        reuse_from_release_id="v1.2-demo",
        require_source_eligible_total=4658,
        require_non_reusable_embedded_total=12,
    )

    assert result.copied_this_run == 0
    assert result.embedded_this_run == 0
    assert result.skipped_this_run == 4670
    assert repository.reuse_calls == []
    assert client.document_calls == []


@pytest.mark.parametrize(
    ("status", "embeddings"),
    [("loading", 0), ("loading", EXPECTED_PASSAGES), ("active", EXPECTED_PASSAGES - 1)],
)
def test_required_existing_active_release_fails_before_any_ingestion_mutation(
    status: str, embeddings: int
) -> None:
    """Breaks if an MVP upgrade can create/embed/activate before its precondition fails."""
    repository = FakeRepository(existing_embeddings=embeddings, status=status)
    client = FakeEmbeddingClient()

    with pytest.raises(IngestionError, match="existing active release"):
        ingest_bundle(
            FakeBundle(),
            repository,
            client,
            require_existing_active=True,
        )

    assert repository.begin_calls == 0
    assert repository.started_runs == []
    assert repository.upserted is False
    assert repository.reconciled_facets == []
    assert repository.stored == []
    assert repository.finished_runs == []
    assert repository.status == status
    assert client.document_calls == []


def test_required_active_upgrade_rejects_changed_content_before_facet_reconcile() -> None:
    """Breaks if matching counts/model/dimension can conceal one changed active record."""
    persisted = (
        {"record_kind": "movie", "movie_id": "movie-1", "chinese_title": "原片名"},
        {
            "record_kind": "passage",
            "passage_id": "metadata:movie-1",
            "movie_id": "movie-1",
            "passage_kind": "metadata",
            "page_number": 0,
            "body": "original body",
            "content_sha256": "a" * 64,
        },
    )
    changed = (
        persisted[0],
        {**persisted[1], "body": "changed body"},
        {"record_kind": "facet", "movie_id": "movie-1"},
    )
    repository = FakeRepository(
        existing_embeddings=EXPECTED_PASSAGES,
        status="active",
        persisted_non_facet_records=persisted,
    )
    client = FakeEmbeddingClient()

    with pytest.raises(IngestionError, match="existing active release"):
        ingest_bundle(
            FakeBundle(records=changed),
            repository,
            client,
            require_existing_active=True,
        )

    assert repository.active_bundle_checks == [[dict(changed[0]), dict(changed[1])]]
    assert repository.begin_calls == 0
    assert repository.started_runs == []
    assert repository.reconciled_facets == []
    assert repository.stored == []
    assert client.document_calls == []


def test_required_active_upgrade_accepts_exact_bundle_without_rewriting_or_embedding() -> None:
    """Breaks if a byte-equivalent active bundle performs any insertion reconciliation."""
    persisted = (
        {"record_kind": "movie", "movie_id": "movie-1", "chinese_title": "原片名"},
        {
            "record_kind": "passage",
            "passage_id": "metadata:movie-1",
            "movie_id": "movie-1",
            "passage_kind": "metadata",
            "page_number": 0,
            "body": "original body",
            "content_sha256": "a" * 64,
        },
    )
    facet = {"record_kind": "facet", "movie_id": "movie-1"}
    repository = FakeRepository(
        existing_embeddings=EXPECTED_PASSAGES,
        status="active",
        persisted_non_facet_records=persisted,
    )
    client = FakeEmbeddingClient()

    result = ingest_bundle(
        FakeBundle(records=(*persisted, facet)),
        repository,
        client,
        require_existing_active=True,
    )

    assert result.active is True
    assert repository.active_bundle_checks == [[dict(record) for record in persisted]]
    assert repository.reconciled_facets == []
    assert repository.reconcile_calls == 0
    assert repository.begin_calls == 0
    assert repository.upserted is False
    assert repository.stored == []
    assert client.document_calls == []


def test_active_release_rerun_with_facets_is_read_only_without_reembedding() -> None:
    """Breaks if an active facet-bearing rerun enters the insertion reconciliation path."""
    facets = tuple(
        {
            "record_kind": "facet",
            "movie_id": f"movie-{index:04d}",
            "tier": "B",
            "content_sha256": f"{index + 1:064x}",
        }
        for index in range(4658)
    )
    repository = FakeRepository(existing_embeddings=EXPECTED_PASSAGES, status="active")

    result = ingest_bundle(FakeBundle(records=facets), repository, FakeEmbeddingClient())

    assert result.active is True
    assert repository.reconciled_facets == []
    assert repository.reconcile_calls == 0
    assert repository.upserted is False


def test_loading_release_persists_movies_before_reconciling_facets() -> None:
    """Breaks if a fresh release attempts the facet composite foreign key before its movies."""
    records = (
        {"record_kind": "movie", "movie_id": "movie-1"},
        {
            "record_kind": "facet",
            "movie_id": "movie-1",
            "tier": "B",
            "content_sha256": "a" * 64,
        },
    )
    repository = FakeRepository(existing_embeddings=EXPECTED_PASSAGES)

    result = ingest_bundle(FakeBundle(records=records), repository, FakeEmbeddingClient())

    assert result.active is True
    assert repository.upserted_records == [{"record_kind": "movie", "movie_id": "movie-1"}]
    assert repository.reconciled_facets == [records[1]]


def test_permanent_error_keeps_release_loading_and_records_safe_error_summary() -> None:
    repository = FakeRepository(existing_embeddings=4662, pending=_passages(2))
    embedding_client = FakeEmbeddingClient(fail_on_call=2)

    with pytest.raises(IngestionError, match="embedding request failed"):
        ingest_bundle(FakeBundle(), repository, embedding_client)

    assert repository.status == "loading"
    assert repository.stored == ["metadata:movie-0000"]
    assert repository.finished_runs == [
        {
            "run_id": repository.started_runs[0],
            "status": "failed",
            "embedded_count": 1,
            "skipped_count": 4662,
            "error_summary": "embedding request failed (status 400)",
        }
    ]


class StatusError(RuntimeError):
    def __init__(self, status_code: int) -> None:
        super().__init__(f"remote detail that must not be exposed: {status_code}")
        self.status_code = status_code


class FakeModels:
    def __init__(self, outcomes: list[object]) -> None:
        self.outcomes = outcomes
        self.calls: list[dict[str, object]] = []

    def embed_content(self, **kwargs: object) -> object:
        self.calls.append(kwargs)
        outcome = self.outcomes.pop(0)
        if isinstance(outcome, Exception):
            raise outcome
        return outcome


def _embedding_response(values: list[float] | None = None) -> object:
    return SimpleNamespace(
        embeddings=[SimpleNamespace(values=[0.125] * 768 if values is None else values)]
    )


def _vertex_client(monkeypatch: pytest.MonkeyPatch, models: FakeModels) -> VertexEmbeddingClient:
    fake_genai_client = SimpleNamespace(models=models)
    monkeypatch.setattr(
        "hk_movie_rag.vertex_clients.genai.Client", lambda **kwargs: fake_genai_client
    )
    return VertexEmbeddingClient("motionexpaiweb", "global", "gemini-embedding-2", 768)


def test_vertex_document_format_and_exact_dimension(monkeypatch: pytest.MonkeyPatch) -> None:
    models = FakeModels([_embedding_response()])
    client = _vertex_client(monkeypatch, models)

    vector = client.embed_document("body", title="醉拳")

    assert vector == [0.125] * 768
    assert models.calls[0]["model"] == "gemini-embedding-2"
    assert models.calls[0]["contents"] == "title: 醉拳 | text: body"
    assert models.calls[0]["config"].output_dimensionality == 768


def test_vertex_can_use_explicit_short_lived_credentials(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    observed: dict[str, object] = {}
    sentinel_credentials = object()

    def fake_client(**kwargs: object) -> object:
        observed.update(kwargs)
        return SimpleNamespace(models=FakeModels([_embedding_response()]))

    monkeypatch.setattr("hk_movie_rag.vertex_clients.genai.Client", fake_client)

    VertexEmbeddingClient(
        "motionexpaiweb",
        "global",
        "gemini-embedding-2",
        768,
        credentials=sentinel_credentials,
    )

    assert observed["credentials"] is sentinel_credentials
    assert observed["http_options"].retry_options.attempts == 1


def test_vertex_query_passes_task_formatted_text_without_rewriting(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    models = FakeModels([_embedding_response()])
    client = _vertex_client(monkeypatch, models)

    client.embed_query("task: search result | query: 醉拳是誰主演？")

    assert models.calls[0]["contents"] == "task: search result | query: 醉拳是誰主演？"


def test_vertex_retries_only_transient_statuses_with_at_most_five_attempts(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    sleeps: list[float] = []
    models = FakeModels([StatusError(429), StatusError(503), _embedding_response()])
    client = _vertex_client(monkeypatch, models)
    monkeypatch.setattr("hk_movie_rag.vertex_clients.time.sleep", sleeps.append)
    monkeypatch.setattr("hk_movie_rag.vertex_clients.random.uniform", lambda _a, _b: 0.125)

    assert client.embed_document("body", title="title") == [0.125] * 768
    assert len(models.calls) == 3
    assert sleeps == [1.125, 2.125]

    exhausted = FakeModels([StatusError(503) for _ in range(5)])
    exhausted_client = _vertex_client(monkeypatch, exhausted)
    with pytest.raises(VertexEmbeddingError, match="status 503 after 5 attempts") as raised:
        exhausted_client.embed_query("query")
    assert "remote detail" not in str(raised.value)
    assert len(exhausted.calls) == 5


def test_vertex_does_not_retry_permanent_status_or_invalid_vectors(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    sleeps: list[float] = []
    permanent = FakeModels([StatusError(400)])
    client = _vertex_client(monkeypatch, permanent)
    monkeypatch.setattr("hk_movie_rag.vertex_clients.time.sleep", sleeps.append)

    with pytest.raises(VertexEmbeddingError, match="status 400 after 1 attempt"):
        client.embed_document("body", title="title")
    assert len(permanent.calls) == 1
    assert sleeps == []

    wrong_dimension = _vertex_client(monkeypatch, FakeModels([_embedding_response([0.1] * 767)]))
    with pytest.raises(VertexEmbeddingError, match="exactly 768 finite floats"):
        wrong_dimension.embed_query("query")

    non_finite = [0.1] * 768
    non_finite[-1] = float("nan")
    nan_client = _vertex_client(monkeypatch, FakeModels([_embedding_response(non_finite)]))
    with pytest.raises(VertexEmbeddingError, match="exactly 768 finite floats"):
        nan_client.embed_query("query")


def test_ingest_cli_uses_verified_gcs_bundle_and_declared_model(
    monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]
) -> None:
    from hk_movie_rag import cli

    bundle = _r2_bundle()
    repository = object()
    observed: dict[str, object] = {}

    @contextmanager
    def fake_source(source: str, *, project_id: str) -> Iterator[FakeBundle]:
        observed.update(source=source, source_project=project_id)
        yield bundle

    @contextmanager
    def fake_repository() -> Iterator[object]:
        yield repository

    class FakeVertex:
        def __init__(self, project_id: str, location: str, model: str, dimension: int) -> None:
            observed.update(
                project_id=project_id,
                location=location,
                model=model,
                dimension=dimension,
            )

    def fake_ingest(
        bundle_arg: object,
        repository_arg: object,
        client_arg: object,
        *,
        require_existing_active: bool = False,
        reuse_from_release_id: str | None = None,
        require_source_eligible_total: int | None = None,
        require_non_reusable_embedded_total: int | None = None,
    ) -> object:
        observed.update(
            bundle=bundle_arg,
            repository=repository_arg,
            client=client_arg,
            require_existing_active=require_existing_active,
            reuse_from_release_id=reuse_from_release_id,
            require_source_eligible_total=require_source_eligible_total,
            require_non_reusable_embedded_total=require_non_reusable_embedded_total,
        )
        return IngestionSummary(
            release_id="v1.2-demo-r2",
            run_id="run-1",
            preexisting=0,
            copied_this_run=4658,
            embedded_this_run=12,
            skipped_this_run=4658,
            source_eligible_total=4658,
            non_reusable_embedded_total=12,
            active=True,
        )

    monkeypatch.setattr(cli, "verified_bundle_source", fake_source, raising=False)
    monkeypatch.setattr(cli, "_repository_from_env", fake_repository, raising=False)
    monkeypatch.setattr(cli, "VertexEmbeddingClient", FakeVertex, raising=False)
    monkeypatch.setattr(cli, "ingest_bundle", fake_ingest, raising=False)
    monkeypatch.setattr(
        sys,
        "argv",
        [
            "hk-movie-rag",
            "ingest-rag-bundle",
            "gs://demo/rag/v1.2-demo/rag_release_manifest.json",
            "--reuse-from-release-id",
            "v1.2-demo",
            "--require-source-eligible-total",
            "4658",
            "--require-non-reusable-embedded-total",
            "12",
        ],
    )

    cli.main()

    assert observed == {
        "source": "gs://demo/rag/v1.2-demo/rag_release_manifest.json",
        "source_project": "motionexpaiweb",
        "project_id": "motionexpaiweb",
        "location": "global",
        "model": "gemini-embedding-2",
        "dimension": 768,
        "bundle": bundle,
        "repository": repository,
        "client": observed["client"],
        "require_existing_active": False,
        "reuse_from_release_id": "v1.2-demo",
        "require_source_eligible_total": 4658,
        "require_non_reusable_embedded_total": 12,
    }
    assert json.loads(capsys.readouterr().out) == {
        "active": True,
        "copied_this_run": 4658,
        "embedded_this_run": 12,
        "non_reusable_embedded_total": 12,
        "preexisting": 0,
        "release_id": "v1.2-demo-r2",
        "run_id": "run-1",
        "skipped_this_run": 4658,
        "source_eligible_total": 4658,
    }


def test_rag_db_stats_cli_emits_counts_without_secrets(
    monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]
) -> None:
    from hk_movie_rag import cli

    class StatsRepository:
        def release_stats(self, release_id: str) -> ReleaseStats:
            assert release_id == "v1.2-demo"
            return ReleaseStats(4658, 4658, 4658, 2, 6, 4664)

        def release_state(self, release_id: str) -> ReleaseState:
            assert release_id == "v1.2-demo"
            return ReleaseState("active", "gemini-embedding-2", 768, 4664)

    @contextmanager
    def fake_repository() -> Iterator[StatsRepository]:
        yield StatsRepository()

    monkeypatch.setattr(cli, "_repository_from_env", fake_repository, raising=False)
    monkeypatch.setattr(
        sys, "argv", ["hk-movie-rag", "rag-db-stats", "--release-id", "v1.2-demo"]
    )

    cli.main()

    assert json.loads(capsys.readouterr().out) == {
        "assets": 4658,
        "documents": 2,
        "embedding_dimension": 768,
        "embedding_model": "gemini-embedding-2",
        "embeddings": 4664,
        "metadata_passages": 4658,
        "movies": 4658,
        "pdf_passages": 6,
        "release_id": "v1.2-demo",
        "status": "active",
    }


def test_verified_gcs_source_downloads_only_manifest_declared_bundle(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    facet_body = {
        "movie_id": "movie-1",
        "tier": "B",
        "tier_reason": "fixture tier",
        "human_review": "fixture review",
        "pilot_movie": False,
        "pilot_evidence": None,
    }
    records = [
        {"record_kind": "movie", "movie_id": "movie-1"},
        {
            "record_kind": "poster",
            "movie_id": "movie-1",
            "is_primary": True,
            "source_is_primary": False,
            "rag_primary_policy": "one_state_row_per_movie",
            "quality_status": "missing",
            "content_sha256": "",
            "derived_object_uri": "",
        },
        {
            "record_kind": "facet",
            **facet_body,
            "content_sha256": hashlib.sha256(
                json.dumps(facet_body, ensure_ascii=False, sort_keys=True, separators=(",", ":")).encode()
            ).hexdigest(),
        },
        {
            "record_kind": "document",
            "movie_id": "movie-1",
            "document_id": "doc-1",
            "source_filename": "one.pdf",
            "source_sha256": "b" * 64,
            "rights_status": "restricted",
            "quality_status": "manual_approved",
        },
        {
            "record_kind": "document",
            "movie_id": "movie-1",
            "document_id": "doc-2",
            "source_filename": "two.pdf",
            "source_sha256": "c" * 64,
            "rights_status": "restricted",
            "quality_status": "manual_approved",
        },
        {
            "record_kind": "passage",
            "passage_kind": "metadata",
            "movie_id": "movie-1",
            "page_number": 0,
        },
        {
            "record_kind": "passage",
            "passage_kind": "pdf",
            "movie_id": "movie-1",
            "document_id": "doc-1",
            "page_number": 1,
        },
        {
            "record_kind": "passage",
            "passage_kind": "pdf",
            "movie_id": "movie-1",
            "document_id": "doc-2",
            "page_number": 1,
        },
    ]
    bundle_bytes = b"".join(
        json.dumps(record, sort_keys=True, separators=(",", ":")).encode() + b"\n"
        for record in records
    )
    manifest = {
        "schema_version": "1.1",
        "rag_release_id": "vtest-demo",
        "parent_release_manifest_sha256": "a" * 64,
        "bundle": {
            "filename": "rag_bundle.jsonl",
            "sha256": hashlib.sha256(bundle_bytes).hexdigest(),
            "size_bytes": len(bundle_bytes),
        },
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
            "documents": 2,
            "pdf_passages": 2,
        },
        "facet_artifacts": {
            "movie_tiers": {
                "relative_path": "fixtures/movie_tiers.parquet",
                "sha256": "d" * 64,
                "row_count": 1,
            },
            "pilot_movies": {
                "relative_path": "fixtures/pilot_movies.parquet",
                "sha256": "e" * 64,
                "row_count": 0,
            },
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
            "derived_inventory_sha256": hashlib.sha256(b"[]").hexdigest(),
        },
        "models": {
            "embedding_model": "gemini-embedding-2",
            "embedding_dimension": 768,
            "generation_model": "gemini-3.5-flash-lite",
        },
        "access_mode": "restricted_demo",
        "documents": [
            {
                "movie_id": "movie-1",
                "document_id": "doc-1",
                "source_filename": "one.pdf",
                "source_sha256": "b" * 64,
                "rights_status": "restricted",
                "quality_status": "manual_approved",
            },
            {
                "movie_id": "movie-1",
                "document_id": "doc-2",
                "source_filename": "two.pdf",
                "source_sha256": "c" * 64,
                "rights_status": "restricted",
                "quality_status": "manual_approved",
            },
        ],
    }
    objects = {
        "rag/vtest/rag_release_manifest.json": json.dumps(
            manifest, sort_keys=True, separators=(",", ":")
        ).encode()
        + b"\n",
        "rag/vtest/rag_bundle.jsonl": bundle_bytes,
        "rag/vtest/not-declared.txt": b"must not be downloaded",
    }
    downloads: list[str] = []

    class FakeBlob:
        def __init__(self, name: str) -> None:
            self.name = name

        def download_to_filename(self, filename: str) -> None:
            downloads.append(self.name)
            Path(filename).write_bytes(objects[self.name])

    class FakeBucket:
        def blob(self, name: str) -> FakeBlob:
            return FakeBlob(name)

    class FakeStorageClient:
        def __init__(self, *, project: str) -> None:
            assert project == "motionexpaiweb"

        def bucket(self, name: str) -> FakeBucket:
            assert name == "demo-bucket"
            return FakeBucket()

    monkeypatch.setattr("hk_movie_rag.rag_ingest.storage.Client", FakeStorageClient)

    with verified_bundle_source(
        "gs://demo-bucket/rag/vtest/rag_release_manifest.json",
        project_id="motionexpaiweb",
    ) as bundle:
        assert bundle.rag_release_id == "vtest-demo"
        assert bundle.bundle_path.is_file()
    assert downloads == [
        "rag/vtest/rag_release_manifest.json",
        "rag/vtest/rag_bundle.jsonl",
    ]
