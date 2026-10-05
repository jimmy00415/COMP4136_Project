"""Repository-boundary tests for the minimum PostgreSQL/pgvector RAG demo."""

from __future__ import annotations

import hashlib
import json
import queue
import re
import threading
import time
import traceback
from collections.abc import Iterable, Iterator, Mapping
from concurrent.futures import ThreadPoolExecutor
from contextlib import contextmanager
from dataclasses import asdict, dataclass, field, replace
from importlib.resources import files
from pathlib import Path

import pytest

from hk_movie_rag import rag_db
from hk_movie_rag.rag_bundle import RagReleaseContract, RagReleaseCounts
from hk_movie_rag.rag_db import (
    DatabaseSettings,
    FacetStats,
    RagDatabaseError,
    RagRepository,
    connect_db,
)
from hk_movie_rag.retrieval import (
    AmbiguousPersonResolutionError,
    RecommendationPlan,
    parse_person_query_shape,
)

EXPECTED_RELEASE = "v1.2-demo"
MIGRATION_PATH = Path(__file__).resolve().parents[1] / "migrations" / "0001_rag_demo.sql"
FACET_MIGRATION_PATH = (
    Path(__file__).resolve().parents[1] / "src" / "hk_movie_rag" / "migrations" / "0002_movie_facets.sql"
)
POSTER_CEILING_MIGRATION_PATH = (
    Path(__file__).resolve().parents[1]
    / "src"
    / "hk_movie_rag"
    / "migrations"
    / "0003_poster_byte_ceiling.sql"
)
RELEASE_IDENTITY_MIGRATION_PATH = (
    Path(__file__).resolve().parents[1]
    / "src"
    / "hk_movie_rag"
    / "migrations"
    / "0004_release_manifest_identity.sql"
)
CONTRACT_DRIVEN_IDENTITY_MIGRATION_PATH = (
    Path(__file__).resolve().parents[1]
    / "src"
    / "hk_movie_rag"
    / "migrations"
    / "0005_contract_driven_release_identity.sql"
)


def _normalized_migration_function_source(migration: str, function_name: str) -> str:
    match = re.search(
        rf"CREATE OR REPLACE FUNCTION public\.{function_name}\(.*?\)"
        r".*?AS \$function\$(.*?)\$function\$;",
        migration,
        re.DOTALL,
    )
    assert match is not None
    return " ".join(match.group(1).split())


_LEGACY_RELEASE_IDENTITY_MIGRATION = RELEASE_IDENTITY_MIGRATION_PATH.read_text(
    encoding="utf-8"
)
_RELEASE_IDENTITY_MIGRATION = CONTRACT_DRIVEN_IDENTITY_MIGRATION_PATH.read_text(
    encoding="utf-8"
)
EXPECTED_IDENTITY_CHECK_DEFINITION = (
    "CHECK (rag_release_manifest_identity_v4_is_valid(release_id, expected_movies, "
    "expected_assets, expected_metadata_passages, expected_documents, "
    "expected_pdf_passages, embedding_model, embedding_dimension, generation_model, "
    "access_mode, manifest_sha256, document_embedding_profile, contract_json) IS TRUE)"
)
EXPECTED_IDENTITY_VALIDATOR_SOURCE = _normalized_migration_function_source(
    _RELEASE_IDENTITY_MIGRATION, "rag_release_manifest_identity_v4_is_valid"
)
EXPECTED_IDENTITY_TRIGGER_SOURCE = _normalized_migration_function_source(
    _RELEASE_IDENTITY_MIGRATION, "enforce_release_manifest_identity"
)


def _facet_records() -> tuple[dict[str, object], ...]:
    records: list[dict[str, object]] = []
    for index in range(4658):
        tier = "S" if index < 50 else "A" if index < 363 else "B"
        body = {
            "movie_id": f"movie-{index:04d}",
            "tier": tier,
            "tier_reason": f"reason {index}",
            "human_review": "reviewed",
            "pilot_movie": index < 24,
            "pilot_evidence": (
                {
                    "handbook_genre": "喜劇",
                    "evidence_note": "pilot evidence",
                    "evidence_url": "https://example.test/evidence",
                    "source_record_id": f"source-{index}",
                }
                if index < 24
                else None
            ),
        }
        records.append(
            {
                "record_kind": "facet",
                **body,
                "content_sha256": hashlib.sha256(
                    json.dumps(body, ensure_ascii=False, sort_keys=True, separators=(",", ":")).encode()
                ).hexdigest(),
            }
        )
    return tuple(records)


def _stored_facet_rows(
    records: Iterable[Mapping[str, object]],
) -> list[tuple[object, ...]]:
    return [
        (
            record["movie_id"],
            record["content_sha256"],
            record["tier"],
            record["tier_reason"],
            record["human_review"],
            record["pilot_movie"],
            record["pilot_evidence"],
        )
        for record in records
    ]


@pytest.fixture
def migration_statements() -> list[str]:
    """The executable DDL statements, normalized only for whitespace-insensitive inspection."""
    return [
        re.sub(r"\s+", " ", statement).strip()
        for statement in MIGRATION_PATH.read_text(encoding="utf-8").split(";")
        if statement.strip()
    ]


@pytest.fixture
def connection() -> RecordingConnection:
    return RecordingConnection()


@pytest.fixture
def repository(connection: RecordingConnection) -> RagRepository:
    return RagRepository(connection)


def _configure_recommendation_ready(connection: RecordingConnection) -> None:
    """Expose the fixture release through the same active-contract gate as production."""
    connection.release_status = "active"


def test_migration_creates_vector_768_hnsw_and_all_six_tables(
    migration_statements: list[str],
) -> None:
    """Breaks if the database cannot store the declared 768-D release graph efficiently."""
    tables = {
        match.group(1)
        for statement in migration_statements
        if (match := re.match(r"CREATE TABLE (?:IF NOT EXISTS )?(\w+)", statement, re.IGNORECASE))
    }
    migration = "\n".join(migration_statements)

    assert tables == {
        "rag_releases",
        "ingestion_runs",
        "movies",
        "media_assets",
        "movie_documents",
        "document_chunks",
    }
    assert "CREATE EXTENSION IF NOT EXISTS vector" in migration
    assert "embedding vector(768)" in migration
    assert "USING hnsw" in migration
    assert "error_summary text" in migration
    assert "embedded_count integer" in migration
    assert "skipped_count integer" in migration


def test_migration_enforces_release_bound_content_and_one_primary_poster(
    migration_statements: list[str],
) -> None:
    """Breaks if an ingestion can cross releases, mutate content, or select two posters."""
    migration = "\n".join(migration_statements)

    assert "FOREIGN KEY (release_id, movie_id)" in migration
    assert "content_sha256" in migration
    assert "derived_content_sha256" in migration
    assert "derived_byte_length" in migration
    assert "derived_mime_type" in migration
    assert "derived_mime_type = 'image/webp'" in migration
    assert "derived_byte_length IS NOT NULL" in migration
    assert "derived_byte_length <= 16777216" in migration
    assert "derived_content_sha256 = ''" in migration
    assert "derived_byte_length IS NULL" in migration
    poster_contract = next(
        statement
        for statement in migration_statements
        if "CONSTRAINT media_assets_poster_derivative_contract" in statement
    )
    assert poster_contract.count("OR ( is_primary AND") == 2
    assert "WHERE is_primary AND asset_type = 'poster'" in migration


def test_migration_binds_pdf_chunks_to_their_document_movie_and_immutable_payloads(
    migration_statements: list[str],
) -> None:
    """Breaks if a PDF chunk can borrow another movie's document or mutate persisted content."""
    migration = "\n".join(migration_statements)

    assert "UNIQUE (release_id, document_id, movie_id)" in migration
    assert "FOREIGN KEY (release_id, document_id, movie_id)" in migration
    assert "BEFORE UPDATE ON movies" in migration
    assert "NEW.body IS DISTINCT FROM OLD.body" in migration


def test_migration_rejects_direct_asset_and_document_hash_mutation(
    migration_statements: list[str],
) -> None:
    """Breaks if a direct SQL update can alter an asset or document hash without payload drift."""
    migration = "\n".join(migration_statements)
    payload_guard = re.search(
        r"CREATE FUNCTION reject_payload_change\(\) RETURNS trigger LANGUAGE plpgsql AS \$\$(.*?)\$\$",
        migration,
        re.DOTALL,
    )

    assert payload_guard is not None
    assert "NEW.content_sha256 <> OLD.content_sha256" in payload_guard.group(1)
    asset_guard = re.search(
        r"CREATE FUNCTION reject_media_asset_change\(\) RETURNS trigger LANGUAGE plpgsql AS \$\$(.*?)\$\$",
        migration,
        re.DOTALL,
    )
    assert asset_guard is not None
    assert "NEW IS DISTINCT FROM OLD" in asset_guard.group(1)
    assert "BEFORE UPDATE ON media_assets" in migration
    assert "EXECUTE FUNCTION reject_media_asset_change()" in migration
    assert "BEFORE UPDATE ON movie_documents" in migration


def test_packaged_migration_matches_the_repository_migration() -> None:
    """Breaks if an installed wheel loses or drifts from the SQL migration it executes."""
    packaged = files("hk_movie_rag.migrations").joinpath("0001_rag_demo.sql").read_text(
        encoding="utf-8"
    )
    assert packaged == MIGRATION_PATH.read_text(encoding="utf-8")


def test_facet_migration_has_a_release_movie_key_and_immutable_content() -> None:
    """Breaks if independently persisted facets can drift from a governed release movie."""
    migration = FACET_MIGRATION_PATH.read_text(encoding="utf-8")

    assert "CREATE TABLE movie_facets" in migration
    assert "PRIMARY KEY (release_id, movie_id)" in migration
    assert "FOREIGN KEY (release_id, movie_id) REFERENCES movies" in migration
    assert "tier IN ('S', 'A', 'B')" in migration
    assert "CREATE TRIGGER movie_facets_immutable" in migration
    assert "BEFORE UPDATE OR DELETE ON movie_facets" in migration


def test_forward_migration_enforces_the_shared_poster_byte_ceiling() -> None:
    """Breaks if an existing Cloud SQL schema can retain the old positive-only check."""
    migration = POSTER_CEILING_MIGRATION_PATH.read_text(encoding="utf-8")
    packaged = files("hk_movie_rag.migrations").joinpath(
        "0003_poster_byte_ceiling.sql"
    ).read_text(encoding="utf-8")

    assert migration == packaged
    assert "media_assets_poster_byte_ceiling" in migration
    assert "derived_byte_length <= 16777216" in migration


def test_release_identity_migration_allows_only_one_locked_legacy_claim() -> None:
    """Breaks if new/null identities or a second manifest/profile claim can reach storage."""
    migration = RELEASE_IDENTITY_MIGRATION_PATH.read_text(encoding="utf-8")
    packaged = files("hk_movie_rag.migrations").joinpath(
        "0004_release_manifest_identity.sql"
    ).read_text(encoding="utf-8")

    assert migration == packaged
    assert "manifest_sha256 text" in migration
    assert "document_embedding_profile text" in migration
    assert "contract_json jsonb" in migration
    assert "manifest_sha256 IS NOT NULL" in migration
    assert "(manifest_sha256_value ~ '^[0-9a-f]{64}$') IS TRUE" in migration
    assert "document_embedding_profile_value IS NOT NULL" in migration
    assert "btrim(document_embedding_profile_value) <> ''" in migration
    assert "contract_json IS NOT NULL" in migration
    assert "TG_OP = 'INSERT'" in migration
    assert "OLD.manifest_sha256 IS NULL" in migration
    assert "OLD.document_embedding_profile IS NULL" in migration
    assert "OLD.contract_json IS NULL" in migration
    assert "NEW.expected_documents IS DISTINCT FROM OLD.expected_documents" in migration
    assert "NEW.embedding_model IS DISTINCT FROM OLD.embedding_model" in migration
    assert "NEW.manifest_sha256 IS DISTINCT FROM OLD.manifest_sha256" in migration
    assert (
        "NEW.document_embedding_profile IS DISTINCT FROM OLD.document_embedding_profile"
        in migration
    )
    assert "NEW.contract_json IS DISTINCT FROM OLD.contract_json" in migration
    assert "BEFORE INSERT OR UPDATE ON rag_releases" in migration


def test_release_identity_migration_models_the_edc6ae6_transitional_upgrade() -> None:
    """Breaks if valid two-field identities cannot survive adding contract_json."""
    migration = RELEASE_IDENTITY_MIGRATION_PATH.read_text(encoding="utf-8")

    assert (
        _normalized_migration_function_source(
            migration, "rag_release_manifest_identity_v3_is_valid"
        )
        == _normalized_migration_function_source(
            _LEGACY_RELEASE_IDENTITY_MIGRATION,
            "rag_release_manifest_identity_v3_is_valid",
        )
    )
    assert (
        "CHECK ( public.rag_release_manifest_identity_v3_is_valid( release_id, "
        "expected_movies, expected_assets, expected_metadata_passages, "
        "expected_documents, expected_pdf_passages, embedding_model, "
        "embedding_dimension, generation_model, access_mode, manifest_sha256, "
        "document_embedding_profile, contract_json ) IS TRUE )"
        in " ".join(migration.split())
    )
    assert (
        _normalized_migration_function_source(
            migration, "enforce_release_manifest_identity"
        )
        == _normalized_migration_function_source(
            _LEGACY_RELEASE_IDENTITY_MIGRATION,
            "enforce_release_manifest_identity",
        )
    )


def test_release_identity_migration_rejects_empty_or_row_divergent_contracts() -> None:
    """Breaks if ``{}`` or a structurally valid but differently bound contract can insert."""
    migration = RELEASE_IDENTITY_MIGRATION_PATH.read_text(encoding="utf-8")
    normalized = " ".join(migration.split())

    assert "contract_json_value ?& ARRAY[" in migration
    assert "FROM jsonb_object_keys(contract_json_value)" in migration
    assert "FROM jsonb_object_keys(contract_json_value -> 'counts')" in migration
    assert "OR CASE" in migration
    assert "WHEN jsonb_typeof(contract_json_value) IS DISTINCT FROM 'object'" in normalized
    assert "END ) ) ) IS TRUE;" in normalized
    assert (
        "contract_json_value ->> 'manifest_sha256' = manifest_sha256_value"
        in normalized
    )
    assert (
        "contract_json_value ->> 'document_embedding_profile' = "
        "document_embedding_profile_value"
    ) in normalized
    for binding in (
        "contract_json_value ->> 'rag_release_id' = release_id_value",
        "contract_json_value ->> 'embedding_model' = embedding_model_value",
        "contract_json_value ->> 'generation_model' = generation_model_value",
        "contract_json_value ->> 'access_mode' = access_mode_value",
    ):
        assert binding in normalized
    for row_binding in (
        "NEW.release_id",
        "NEW.expected_movies",
        "NEW.expected_assets",
        "NEW.expected_metadata_passages",
        "NEW.expected_documents",
        "NEW.expected_pdf_passages",
        "NEW.embedding_model",
        "NEW.embedding_dimension",
        "NEW.generation_model",
        "NEW.access_mode",
    ):
        assert row_binding in normalized
    assert ") IS NOT TRUE OR NEW.contract_json IS NULL THEN" in normalized
    for required_key in (
        "schema_version",
        "rag_release_id",
        "parent_release_manifest_sha256",
        "manifest_sha256",
        "bundle_sha256",
        "derived_inventory_sha256",
        "counts",
        "embedding_model",
        "embedding_dimension",
        "generation_model",
        "text_extraction_profile",
        "document_embedding_profile",
        "relevance_policy_sha256",
        "poster_authority_sha256",
        "access_mode",
    ):
        assert f"'{required_key}'" in migration


def test_active_release_reconciles_facets_idempotently_and_rejects_conflict(
    repository: RagRepository, connection: RecordingConnection
) -> None:
    """Breaks if a resumed active release can skip, duplicate, or mutate a facet."""
    records = _facet_records()
    connection.facet_rows = [
        (
            record["movie_id"],
            record["content_sha256"],
            record["tier"],
            record["tier_reason"],
            record["human_review"],
            record["pilot_movie"],
            record["pilot_evidence"],
        )
        for record in records
    ]
    connection.facet_stats_row = (4658, 50, 313, 4295, 24)

    repository.reconcile_facets(EXPECTED_RELEASE, records)
    repository.reconcile_facets(EXPECTED_RELEASE, records)

    assert repository.facet_stats(EXPECTED_RELEASE) == FacetStats(4658, 50, 313, 4295, 24)
    conflicting = list(records)
    changed_body = {
        **{key: value for key, value in conflicting[0].items() if key not in {"record_kind", "content_sha256"}},
        "tier_reason": "valid but conflicting replacement",
    }
    conflicting[0] = {
        "record_kind": "facet",
        **changed_body,
        "content_sha256": hashlib.sha256(
            json.dumps(changed_body, ensure_ascii=False, sort_keys=True, separators=(",", ":")).encode()
        ).hexdigest(),
    }
    with pytest.raises(RagDatabaseError, match="facet content conflict"):
        repository.reconcile_facets(EXPECTED_RELEASE, conflicting)


def test_active_release_facet_reconciliation_rejects_missing_row_without_insert(
    repository: RagRepository,
    connection: RecordingConnection,
) -> None:
    """Breaks if a direct active reconciliation can insert a missing immutable facet."""
    records = _facet_records()
    connection.release_status = "active"
    connection.facet_rows = _stored_facet_rows(records[:-1])

    with pytest.raises(RagDatabaseError, match="facet coverage conflict"):
        repository.reconcile_facets(EXPECTED_RELEASE, records)

    assert not any("INSERT INTO movie_facets" in call.sql for call in connection.calls)


def test_loading_facet_reconciliation_uses_selected_contract_tier_expectations(
    repository: RagRepository,
    connection: RecordingConnection,
) -> None:
    """Breaks if loading reconciliation substitutes module tier constants."""
    records = list(_facet_records())
    changed = dict(records[49])
    changed["tier"] = "A"
    body = {
        key: changed[key]
        for key in (
            "movie_id",
            "tier",
            "tier_reason",
            "human_review",
            "pilot_movie",
            "pilot_evidence",
        )
    }
    changed["content_sha256"] = hashlib.sha256(
        json.dumps(body, ensure_ascii=False, sort_keys=True, separators=(",", ":")).encode()
    ).hexdigest()
    records[49] = changed
    selected = replace(
        _bundle().contract,
        counts=replace(
            _bundle().contract.counts,
            tier_s_count=49,
            tier_a_count=314,
        ),
    )
    connection.release_contract_json = asdict(selected)
    connection.facet_stats_row = (4658, 49, 314, 4295, 24)

    repository.reconcile_facets(EXPECTED_RELEASE, records)

    assert any("INSERT INTO movie_facets" in call.sql for call in connection.calls)


def test_reconcile_rejects_changed_facet_body_even_when_its_old_hash_is_reused(
    repository: RagRepository, connection: RecordingConnection
) -> None:
    """Breaks if caller-provided hashes can conceal a changed immutable facet field."""
    records = _facet_records()
    changed = list(records)
    changed[0] = {**changed[0], "tier_reason": "tampered while retaining old hash"}
    connection.facet_rows = []

    with pytest.raises(RagDatabaseError, match="facet content hash is invalid"):
        repository.reconcile_facets(EXPECTED_RELEASE, changed)


def test_reconcile_binds_sql_null_for_non_pilot_and_canonical_json_for_pilot(
    repository: RagRepository, connection: RecordingConnection
) -> None:
    """Breaks if Python None becomes JSONB null or pilot evidence loses canonical encoding."""
    records = _facet_records()
    missing_ids = {records[0]["movie_id"], records[-1]["movie_id"]}
    connection.facet_rows = [
        (
            record["movie_id"],
            record["content_sha256"],
            record["tier"],
            record["tier_reason"],
            record["human_review"],
            record["pilot_movie"],
            record["pilot_evidence"],
        )
        for record in records
        if record["movie_id"] not in missing_ids
    ]

    repository.reconcile_facets(EXPECTED_RELEASE, records)

    inserts = [call for call in connection.calls if "INSERT INTO movie_facets" in call.sql]
    assert len(inserts) == 2
    by_movie_id = {call.params[1]: call for call in inserts}
    pilot_evidence = records[0]["pilot_evidence"]
    assert by_movie_id[records[0]["movie_id"]].params[-1] == json.dumps(
        pilot_evidence,
        ensure_ascii=False,
        sort_keys=True,
        separators=(",", ":"),
    )
    assert by_movie_id[records[-1]["movie_id"]].params[-1] is None


def test_person_resolution_normalizes_only_the_query_and_selects_one_exact_credit_token(
    repository: RagRepository, connection: RecordingConnection
) -> None:
    """Breaks if simplified input cannot resolve one release-scoped canonical actor token."""
    connection.person_rows = [("周星馳", "actor")]

    resolved = repository.resolve_recommendation_person(
        EXPECTED_RELEASE, "推荐演员周星驰的电影"
    )

    assert resolved is not None
    assert (resolved.name, resolved.role) == ("周星馳", "actor")
    call = connection.calls[-1]
    sql = " ".join(call.sql.split())
    assert call.params == (EXPECTED_RELEASE, EXPECTED_RELEASE)
    assert "release.status = 'active'" in sql
    assert "movie.release_id = %s" in sql
    assert "regexp_split_to_array( COALESCE(movie.payload ->> 'director', '')" in sql
    assert "regexp_split_to_array( COALESCE(movie.payload ->> 'cast', '')" in sql
    assert "char_length(name) BETWEEN 2 AND 64" in sql
    assert "SELECT DISTINCT name, role" in sql
    assert "role = %s" not in sql


@pytest.mark.parametrize(
    ("question", "canonical_name", "credit_role", "expected_role"),
    (
        ("推荐钟楚红的好电影", "鍾楚紅", "actor", None),
        ("推荐杜琪峰导演的电影", "杜琪峯", "director", "director"),
        ("推荐王家卫导演的电影", "王家衞", "director", "director"),
        ("推荐尔冬升导演的电影", "爾冬陞", "director", "director"),
        ("推荐廖启智的好电影", "廖啟智", "actor", None),
        ("推荐黄沾的好电影", "黃霑", "actor", None),
    ),
)
def test_person_resolution_matches_simplified_alias_to_release_canonical_credit(
    repository: RagRepository,
    connection: RecordingConnection,
    question: str,
    canonical_name: str,
    credit_role: str,
    expected_role: str | None,
) -> None:
    """Breaks if OpenCC output is mistaken for the governed canonical credit spelling."""
    connection.person_rows = [
        ("不相關人物", credit_role),
        (canonical_name, credit_role),
        ("另一人物", credit_role),
    ]

    resolved = repository.resolve_recommendation_person(EXPECTED_RELEASE, question)

    assert resolved is not None
    assert (resolved.name, resolved.role) == (canonical_name, expected_role)
    call = connection.calls[-1]
    assert call.params.count(EXPECTED_RELEASE) == 2
    assert canonical_name not in call.params


def test_person_resolution_returns_every_token_in_one_hk2t_equivalence_class(
    repository: RagRepository, connection: RecordingConnection
) -> None:
    """Breaks if one simplified alias silently drops an equivalent governed spelling."""
    connection.person_rows = [("廖啓智", "actor"), ("廖啟智", "actor")]

    resolved = repository.resolve_recommendation_person(
        EXPECTED_RELEASE, "推荐廖启智的好电影"
    )

    assert resolved is not None
    assert (resolved.name, resolved.role) == ("廖啓智", None)
    assert resolved.exact_names == ("廖啓智", "廖啟智")


def test_person_resolution_matches_identity_before_role_filtering(
    repository: RagRepository, connection: RecordingConnection
) -> None:
    """Breaks if requested role filters the lexicon before known identity resolution."""
    connection.person_rows = [("周星馳", "actor")]

    resolved = repository.resolve_recommendation_person(
        EXPECTED_RELEASE, "周星馳導演的作品"
    )

    assert resolved is not None
    assert (resolved.name, resolved.role) == ("周星馳", "director")
    assert resolved.exact_names == ("周星馳",)


def test_person_resolution_matches_longer_exact_release_credit(
    repository: RagRepository, connection: RecordingConnection
) -> None:
    """Breaks if bounded longer compiler candidates cannot reach exact Release identity."""
    connection.person_rows = [("大島由加里", "actor"), ("大島由加利", "actor")]

    resolved = repository.resolve_recommendation_person(
        EXPECTED_RELEASE, "大島由加里的電影"
    )

    assert resolved is not None
    assert (resolved.name, resolved.role) == ("大島由加里", None)
    assert resolved.exact_names == ("大島由加里",)


def test_unified_identity_evidence_keeps_only_maximal_exact_spans(
    repository: RagRepository, connection: RecordingConnection
) -> None:
    """Breaks if a contained shorter Release credit makes one exact name ambiguous."""
    connection.person_rows = [("周星馳", "actor"), ("星馳", "actor")]

    resolved = repository.resolve_recommendation_person(
        EXPECTED_RELEASE, "周星馳的電影"
    )

    assert resolved is not None
    assert (resolved.name, resolved.role) == ("周星馳", None)
    assert resolved.exact_names == ("周星馳",)


@pytest.mark.parametrize(
    "question",
    (
        "想看周星驰和成龍電影，推薦幾部",
        "想看成龙和周星馳電影，推薦幾部",
    ),
)
def test_unified_identity_evidence_rejects_mixed_exact_and_alias_people(
    repository: RagRepository,
    connection: RecordingConnection,
    question: str,
) -> None:
    """Breaks if an exact match hides a distinct alias identity at another span."""
    connection.person_rows = [("周星馳", "actor"), ("成龍", "actor")]

    with pytest.raises(AmbiguousPersonResolutionError):
        repository.resolve_recommendation_person(EXPECTED_RELEASE, question)


@pytest.mark.parametrize(
    "question",
    (
        "請你幫我比較周星驰跟成龍",
        "請你幫我比較成龙跟周星馳",
        "推薦電影，周星馳跟成龍都可以",
        "想看點電影，周星馳或成龍都行",
        "請比較周星馳跟成龍，推薦電影",
    ),
)
def test_unified_identity_evidence_defends_outside_the_person_parser_grammar(
    repository: RagRepository,
    connection: RecordingConnection,
    question: str,
) -> None:
    """Breaks if the repository trusts a direct match before disjoint alias evidence."""
    connection.person_rows = [("周星馳", "actor"), ("成龍", "actor")]
    assert parse_person_query_shape(question) is None

    with pytest.raises(AmbiguousPersonResolutionError):
        repository.resolve_recommendation_person(EXPECTED_RELEASE, question)


@pytest.mark.parametrize(
    ("question", "expected_name"),
    (
        ("推荐周星驰的好电影", "周星馳"),
        ("成龍的電影", "成龍"),
    ),
)
def test_unified_identity_evidence_preserves_single_person_controls(
    repository: RagRepository,
    connection: RecordingConnection,
    question: str,
    expected_name: str,
) -> None:
    """Breaks if unrelated Release credits make one named identity ambiguous."""
    connection.person_rows = [("周星馳", "actor"), ("成龍", "actor")]

    resolved = repository.resolve_recommendation_person(EXPECTED_RELEASE, question)

    assert resolved is not None
    assert (resolved.name, resolved.role) == (expected_name, None)
    assert resolved.exact_names == (expected_name,)


@pytest.mark.parametrize(
    "question",
    (
        "周星馳和成龍的電影",
        "周星馳的電影和成龍的作品",
        "推薦周星馳的電影以及成龍的作品",
        "比較周星馳跟成龍",
    ),
)
def test_person_resolution_rejects_multiple_distinct_release_identities(
    repository: RagRepository,
    connection: RecordingConnection,
    question: str,
) -> None:
    """Breaks if repository resolution silently selects the earliest Release credit."""
    connection.person_rows = [("周星馳", "actor"), ("成龍", "actor")]

    with pytest.raises(AmbiguousPersonResolutionError):
        repository.resolve_recommendation_person(EXPECTED_RELEASE, question)


def test_person_resolution_reports_compound_release_names_as_ambiguous() -> None:
    """Breaks if an internal coordination glyph hides a second governed identity."""
    connection = RecordingConnection()
    connection.person_rows = [("袁和平", "director"), ("成龍", "actor")]
    repository = RagRepository(connection)

    with pytest.raises(AmbiguousPersonResolutionError):
        repository.resolve_recommendation_person(
            EXPECTED_RELEASE, "推薦袁和平和成龍的電影"
        )


@pytest.mark.parametrize(
    ("question", "person_rows"),
    (
        ("推荐张冲的好电影", [("張沖", "actor"), ("張衝", "actor")]),
        ("推荐陈冲的好电影", [("陳沖", "actor"), ("陳衝", "actor")]),
    ),
)
def test_simplified_homograph_with_multiple_hk2t_classes_fails_closed(
    repository: RagRepository,
    connection: RecordingConnection,
    question: str,
    person_rows: list[tuple[object, ...]],
) -> None:
    """Breaks if a simplified homograph guesses one unrelated traditional person."""
    connection.person_rows = person_rows

    with pytest.raises(AmbiguousPersonResolutionError):
        repository.resolve_recommendation_person(EXPECTED_RELEASE, question)


@pytest.mark.parametrize(
    ("exact_name", "other_name"),
    (
        ("張沖", "張衝"),
        ("張衝", "張沖"),
        ("陳沖", "陳衝"),
        ("陳衝", "陳沖"),
    ),
)
def test_exact_traditional_homograph_selects_only_its_hk2t_class(
    repository: RagRepository,
    connection: RecordingConnection,
    exact_name: str,
    other_name: str,
) -> None:
    """Breaks if an exact governed spelling is overwritten by its simplified homograph."""
    connection.person_rows = [(exact_name, "actor"), (other_name, "actor")]

    resolved = repository.resolve_recommendation_person(
        EXPECTED_RELEASE, f"推薦{exact_name}的好電影"
    )

    assert resolved is not None
    assert (resolved.name, resolved.role) == (exact_name, None)
    assert resolved.exact_names == (exact_name,)


def test_person_resolution_reuses_one_complete_immutable_release_credit_lexicon(
    repository: RagRepository, connection: RecordingConnection
) -> None:
    """Breaks if every recommendation retransfers the active release credit lexicon."""
    connection.person_rows = [("周星馳", "actor"), ("杜琪峯", "director")]

    actor = repository.resolve_recommendation_person(
        EXPECTED_RELEASE, "推薦演員周星馳的電影"
    )
    director = repository.resolve_recommendation_person(
        EXPECTED_RELEASE, "推薦杜琪峰導演的電影"
    )

    assert actor is not None and (actor.name, actor.role) == ("周星馳", "actor")
    assert director is not None and (director.name, director.role) == (
        "杜琪峯",
        "director",
    )
    credit_calls = [call for call in connection.calls if "credit_tokens AS" in call.sql]
    assert len(credit_calls) == 1


def test_person_resolution_initializes_the_release_credit_cache_once_concurrently(
    repository: RagRepository, connection: RecordingConnection
) -> None:
    """Breaks if concurrent first requests race and repeat the full credit query."""
    connection.person_rows = [("成龍", "actor"), ("周星馳", "actor")]

    with ThreadPoolExecutor(max_workers=8) as executor:
        results = tuple(
            executor.map(
                lambda _: repository.resolve_recommendation_person(
                    EXPECTED_RELEASE, "推薦成龍主演的電影"
                ),
                range(8),
            )
        )

    assert all(
        result is not None and (result.name, result.role) == ("成龍", "actor")
        for result in results
    )
    credit_calls = [call for call in connection.calls if "credit_tokens AS" in call.sql]
    assert len(credit_calls) == 1


def test_person_resolution_precomputes_each_credit_alias_once_per_release(
    repository: RagRepository,
    connection: RecordingConnection,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Breaks if each query reruns OpenCC once per cached credit token."""
    connection.person_rows = [("成龍", "actor"), ("周星馳", "actor")]
    converted: list[str] = []

    def recording_alias(text: str) -> str:
        converted.append(text)
        return text

    monkeypatch.setattr("hk_movie_rag.rag_db.credit_query_alias", recording_alias)

    repository.resolve_recommendation_person(
        EXPECTED_RELEASE, "推荐成龙主演的电影"
    )
    repository.resolve_recommendation_person(
        EXPECTED_RELEASE, "想看成龙主演的电影"
    )

    assert converted.count("成龍") == 1
    assert converted.count("周星馳") == 1
    assert converted.count("推薦成龍主演的電影") == 1
    assert converted.count("想看成龍主演的電影") == 1


@pytest.mark.parametrize(
    ("method_name", "passage_kind"),
    (
        ("search_recommendations", "metadata"),
        ("search_deep_recommendations", "pdf"),
    ),
)
def test_person_recommendation_sql_uses_parameterized_exact_split_token_predicates(
    repository: RagRepository,
    connection: RecordingConnection,
    method_name: str,
    passage_kind: str,
) -> None:
    """Breaks if either recommendation path uses substring matching or omits the person AND."""
    _configure_recommendation_ready(connection)
    exact_names = ("廖啓智", "廖啟智")
    plan = replace(
        RecommendationPlan(1, ("喜劇",), None, None, None, False, False, (), "廖啟智喜劇"),
        person_name="廖啓智",
        person_role="actor",
        person_exact_names=exact_names,
    )

    search = getattr(repository, method_name)
    search(EXPECTED_RELEASE, [0.1] * 768, plan)

    call = connection.calls[-1]
    sql = " ".join(call.sql.split())
    assert f"chunk.passage_kind = '{passage_kind}'" in sql
    assert "regexp_split_to_array( COALESCE(movie.payload ->> 'director', '')" in sql
    assert "regexp_split_to_array( COALESCE(movie.payload ->> 'cast', '')" in sql
    assert "btrim(credit.name) = ANY(%s::text[])" in sql
    assert "(%s::text IS NULL OR credit.role = %s::text)" in sql
    assert "LIKE" not in sql.upper()
    assert call.params.count(list(exact_names)) == 2
    assert call.params.count("actor") == 2


@pytest.mark.parametrize(
    "method_name", ("search_recommendations", "search_deep_recommendations")
)
def test_recommendation_sql_preserves_regex_quantifiers_inside_f_string(
    repository: RagRepository,
    connection: RecordingConnection,
    method_name: str,
) -> None:
    """Breaks if Python consumes SQL regex quantifiers while formatting fragments."""
    _configure_recommendation_ready(connection)
    plan = RecommendationPlan(
        1, ("喜劇",), None, None, None, False, False, (), "推薦喜劇"
    )

    getattr(repository, method_name)(EXPECTED_RELEASE, [0.1] * 768, plan)

    call = connection.calls[-1]
    assert "derived_content_sha256 ~ '^[0-9a-f]{64}$'" in call.sql
    assert "derived_content_sha256 ~ '^[0-9a-f]64$'" not in call.sql
    assert "asset.derived_byte_length <= 16777216" in call.sql
    assert "movie.payload ->> 'release_date' ~ '^\\d{4}-\\d{2}-\\d{2}$'" in call.sql
    assert "movie.payload ->> 'release_date' ~ '^\\d4-\\d2-\\d2$'" not in call.sql


@pytest.mark.parametrize(
    "method_name", ("search_recommendations", "search_deep_recommendations")
)
def test_typed_genre_and_title_proxy_sql_uses_parameterized_all_any_and_strpos(
    repository: RagRepository,
    connection: RecordingConnection,
    method_name: str,
) -> None:
    """Breaks if typed semantics collapse to genre OR or interpolate query prose."""
    _configure_recommendation_ready(connection)
    raw_genre_question = "哪些電影適合喜歡武打喜劇的觀眾？"
    genre_plan = replace(
        RecommendationPlan(
            2, (), None, None, None, False, False, (), raw_genre_question
        ),
        genres_all=("喜劇",),
        genres_any=("動作", "功夫", "武俠"),
    )

    search = getattr(repository, method_name)
    search(EXPECTED_RELEASE, [0.1] * 768, genre_plan)

    genre_call = connection.calls[-1]
    sql = " ".join(genre_call.sql.split())
    assert "cardinality(%s::text[]) = 0 OR NOT EXISTS" in sql
    assert "WHERE NOT EXISTS" in sql
    assert "cardinality(%s::text[]) = 0 OR EXISTS" in sql
    assert genre_call.params.count(["喜劇"]) == 2
    assert genre_call.params.count(["動作", "功夫", "武俠"]) == 2
    assert raw_genre_question not in genre_call.params

    raw_title_question = "有什麼關於兄弟情的香港電影？"
    title_plan = replace(
        RecommendationPlan(
            2, (), None, None, None, False, False, (), raw_title_question
        ),
        genres_any=("劇情", "動作", "犯罪"),
        title_terms_any=("兄弟", "手足"),
        scope_label="title_keyword_proxy",
    )
    search(EXPECTED_RELEASE, [0.1] * 768, title_plan)

    title_call = connection.calls[-1]
    sql = " ".join(title_call.sql.split())
    assert re.search(r"strpos\s*\(\s*lower\s*\(\s*COALESCE\s*\([^)]*chinese_title", sql)
    assert re.search(r"strpos\s*\(\s*lower\s*\(\s*COALESCE\s*\([^)]*english_title", sql)
    assert title_call.params.count(["劇情", "動作", "犯罪"]) == 2
    assert title_call.params.count(["兄弟", "手足"]) == 2
    assert raw_title_question not in title_call.params

    mixed_plan = replace(title_plan, genres=("喜劇",))
    search(EXPECTED_RELEASE, [0.1] * 768, mixed_plan)

    mixed_call = connection.calls[-1]
    assert mixed_call.params.count(["喜劇"]) == 2
    assert mixed_call.params.count(["劇情", "動作", "犯罪"]) == 2
    assert mixed_call.params.count(["兄弟", "手足"]) == 2


@pytest.mark.parametrize(
    "method_name", ("search_recommendations", "search_deep_recommendations")
)
def test_controlled_credit_group_sql_is_separate_from_specific_person(
    repository: RagRepository,
    connection: RecordingConnection,
    method_name: str,
) -> None:
    """Breaks if a demographic phrase is treated as one fuzzy person name."""
    _configure_recommendation_ready(connection)
    exact_names = (
        "許鞍華",
        "張婉婷",
        "张婉婷",
        "羅卓瑤",
        "麥曦茵",
        "麦曦茵",
        "黃真真",
        "岸西",
    )
    plan = replace(
        RecommendationPlan(2, (), None, None, None, False, False, (), "女性導演"),
        credit_group_id="female_directors_v1",
        credit_role="director",
        credit_exact_names=exact_names,
        scope_label="controlled_credit_group",
    )

    search = getattr(repository, method_name)
    search(EXPECTED_RELEASE, [0.1] * 768, plan)

    call = connection.calls[-1]
    assert call.params.count(list(exact_names)) == 2
    assert call.params.count("director") == 2
    assert "女性導演" not in call.params
    assert plan.person_name is None
    assert plan.person_exact_names == ()


def test_recommendation_sql_rejects_uncontrolled_title_or_credit_constraints(
    repository: RagRepository,
) -> None:
    """Breaks if arbitrary user-derived strings can become SQL constraints."""
    uncontrolled_title = replace(
        RecommendationPlan(1, (), None, None, None, False, False, (), "raw query"),
        title_terms_any=("raw query",),
        scope_label="title_keyword_proxy",
    )
    uncontrolled_group = replace(
        RecommendationPlan(1, (), None, None, None, False, False, (), "raw query"),
        credit_group_id="raw query",
        credit_role="director",
        credit_exact_names=("raw query",),
        scope_label="controlled_credit_group",
    )
    mixed_controlled_proxies = replace(
        RecommendationPlan(1, (), None, None, None, False, False, (), "raw query"),
        title_terms_any=("兄弟", "賭"),
        scope_label="title_keyword_proxy",
    )
    forged_title_proxy_semantics = replace(
        RecommendationPlan(1, (), None, None, None, False, False, (), "raw query"),
        genres_any=("恐怖",),
        title_terms_any=("兄弟", "手足"),
        scope_label="title_keyword_proxy",
    )
    forged_family_scope = replace(
        RecommendationPlan(1, (), None, None, None, False, False, (), "raw query"),
        genres_all=("家庭",),
        genres_any=("動作",),
        scope_label="family_genre_proxy",
    )

    with pytest.raises(RagDatabaseError, match="title terms"):
        repository.search_recommendations(
            EXPECTED_RELEASE, [0.1] * 768, uncontrolled_title
        )
    with pytest.raises(RagDatabaseError, match="credit group"):
        repository.search_recommendations(
            EXPECTED_RELEASE, [0.1] * 768, uncontrolled_group
        )
    with pytest.raises(RagDatabaseError, match="title terms"):
        repository.search_recommendations(
            EXPECTED_RELEASE, [0.1] * 768, mixed_controlled_proxies
        )
    with pytest.raises(RagDatabaseError, match="title proxy"):
        repository.search_recommendations(
            EXPECTED_RELEASE, [0.1] * 768, forged_title_proxy_semantics
        )
    with pytest.raises(RagDatabaseError, match="family genre"):
        repository.search_recommendations(
            EXPECTED_RELEASE, [0.1] * 768, forged_family_scope
        )


def test_recommendation_sql_filters_tier_genre_year_and_exclusions(
    repository: RagRepository, connection: RecordingConnection
) -> None:
    """Breaks if structured recommendations infer no matches from a short top-K result."""
    _configure_recommendation_ready(connection)
    connection.recommendation_rows = [
        (
            "metadata:1984_XJ_001",
            "1984_XJ_001",
            "metadata",
            "movie_id: 1984_XJ_001\ngenre: 喜劇",
            0,
            None,
            None,
            {"movie_id": "1984_XJ_001", "genre": "喜劇", "release_date": "1984-01-01"},
            "S",
            "high cultural value",
            "reviewed",
            False,
            None,
            "a" * 64,
            True,
            0.12,
            18,
        )
    ]
    plan = RecommendationPlan(
        3, ("喜劇",), "S", 1980, 1989, False, True, ("1982_ZJPD_001",), "喜劇 S級 1980年代"
    )

    result = repository.search_recommendations(EXPECTED_RELEASE, [0.1] * 768, plan)

    assert result.total_matches == 18
    assert result.records[0]["body"] == "movie_id: 1984_XJ_001\ngenre: 喜劇"
    assert result.records[0]["movie"] == {
        "movie_id": "1984_XJ_001",
        "genre": "喜劇",
        "release_date": "1984-01-01",
        "tier": "S",
        "tier_reason": "high cultural value",
        "human_review": "reviewed",
        "pilot_movie": False,
        "pilot_evidence": None,
        "facet_content_sha256": "a" * 64,
    }
    call = connection.calls[-1]
    assert "movie_facets" in call.sql
    assert "COUNT(*) OVER()" in call.sql
    assert "chunk.passage_kind = 'metadata'" in call.sql
    assert "CROSS JOIN LATERAL" in call.sql
    assert "release_date.release_year" in call.sql
    assert call.params[-1] == 3
    assert call.params[-2] == ["1982_ZJPD_001"]


def test_recommendation_search_counts_unique_movies_and_normalizes_genre_whitespace(
    repository: RagRepository, connection: RecordingConnection
) -> None:
    """Breaks if two metadata chunks inflate the movie total or whitespace blocks a genre match."""
    _configure_recommendation_ready(connection)
    first = (
        "metadata:1984_XJ_001:a", "1984_XJ_001", "metadata", "first body", 0, None, None,
        {"movie_id": "1984_XJ_001", "genre": "喜劇, 愛情", "release_date": "1984-01-01"},
        "S", "reason", "reviewed", False, None, "a" * 64, True, 0.12, 99,
    )
    duplicate = (
        "metadata:1984_XJ_001:b", "1984_XJ_001", "metadata", "duplicate body", 0, None, None,
        {"movie_id": "1984_XJ_001", "genre": "喜劇, 愛情", "release_date": "1984-01-01"},
        "S", "reason", "reviewed", False, None, "a" * 64, True, 0.20, 99,
    )
    connection.recommendation_rows = [first, duplicate]
    connection.recommendation_rows_are_candidates = True
    plan = RecommendationPlan(3, (" 愛情 ",), "S", 1980, 1989, False, False, (), "愛情")

    result = repository.search_recommendations(EXPECTED_RELEASE, [0.1] * 768, plan)

    assert result.total_matches == 1
    assert [record["passage_id"] for record in result.records] == ["metadata:1984_XJ_001:a"]
    call = connection.calls[-1]
    assert "ROW_NUMBER() OVER" in call.sql
    assert "PARTITION BY movie_id" in call.sql
    assert "btrim(requested.genre)" in call.sql
    assert "btrim(stored.genre)" in call.sql
    assert call.params[4] == ["愛情"]


def test_deep_recommendation_sql_filters_pdf_before_limit_and_preserves_plan(
    repository: RagRepository, connection: RecordingConnection
) -> None:
    """Breaks if generic top-K crowding or lost constraints can select deep evidence."""
    _configure_recommendation_ready(connection)
    connection.recommendation_rows = [
        (
            "pdf:1984_XJ_001:p1",
            "1984_XJ_001",
            "pdf",
            "verified deep-analysis body",
            1,
            "deep-doc",
            "deep.pdf",
            {"movie_id": "1984_XJ_001", "genre": "喜劇", "release_date": "1984-01-01"},
            "S",
            "high cultural value",
            "reviewed",
            True,
            {"source_record_id": "pilot-1"},
            "a" * 64,
            True,
            0.12,
            2,
        )
    ]
    plan = RecommendationPlan(
        2,
        ("喜劇",),
        "S",
        1980,
        1989,
        True,
        True,
        ("1978_ZQ_001",),
        "1980年代S級深度喜劇",
    )

    result = repository.search_deep_recommendations(
        EXPECTED_RELEASE, [0.1] * 768, plan
    )

    assert result.total_matches == 2
    assert result.records[0]["passage_kind"] == "pdf"
    call = connection.calls[-1]
    assert "chunk.passage_kind = 'pdf'" in call.sql
    assert call.sql.index("chunk.passage_kind = 'pdf'") < call.sql.index("LIMIT %s")
    assert "PARTITION BY movie_id" in call.sql
    assert "COUNT(*) OVER() AS total_matches" in call.sql
    assert "facet.tier = %s::text" in call.sql
    assert "btrim(requested.genre)" in call.sql
    assert "release_date.release_year >= %s::integer" in call.sql
    assert "release_date.release_year <= %s::integer" in call.sql
    assert call.params[2:4] == ("S", "S")
    assert call.params[4:6] == (["喜劇"], ["喜劇"])
    assert call.params[6:8] == ([], [])
    assert call.params[8:12] == (1980, 1980, 1989, 1989)
    assert call.params[-2] == ["1978_ZQ_001"]
    assert call.params[-1] == 8


def test_deep_recommendation_search_returns_one_pdf_per_movie_and_unique_total(
    repository: RagRepository, connection: RecordingConnection
) -> None:
    """Breaks if multiple PDF pages inflate either candidates or availability."""
    _configure_recommendation_ready(connection)
    first = (
        "pdf:movie-1:p1", "movie-1", "pdf", "first page", 1, "doc-1", "one.pdf",
        {"movie_id": "movie-1", "genre": "喜劇", "release_date": "1981-01-01"},
        "S", "reason", "reviewed", True, None, "a" * 64, True, 0.10, 99,
    )
    duplicate = (
        "pdf:movie-1:p2", "movie-1", "pdf", "second page", 2, "doc-1", "one.pdf",
        {"movie_id": "movie-1", "genre": "喜劇", "release_date": "1981-01-01"},
        "S", "reason", "reviewed", True, None, "a" * 64, True, 0.20, 99,
    )
    second = (
        "pdf:movie-2:p1", "movie-2", "pdf", "other movie", 1, "doc-2", "two.pdf",
        {"movie_id": "movie-2", "genre": "喜劇", "release_date": "1991-01-01"},
        "A", "reason", "reviewed", True, None, "b" * 64, False, 0.30, 99,
    )
    connection.recommendation_rows = [first, duplicate, second]
    connection.recommendation_rows_are_candidates = True
    plan = RecommendationPlan(3, ("喜劇",), None, None, None, False, False, (), "深度喜劇")

    result = repository.search_deep_recommendations(
        EXPECTED_RELEASE, [0.1] * 768, plan
    )

    assert result.total_matches == 2
    assert [record["passage_id"] for record in result.records] == [
        "pdf:movie-1:p1",
        "pdf:movie-2:p1",
    ]


def test_ensure_schema_verifies_facet_columns_constraints_and_indexes(
    repository: RagRepository, connection: RecordingConnection
) -> None:
    """Breaks if a table/trigger lookalike can satisfy the 0002 readiness gate."""
    repository.ensure_schema()

    readiness_query = connection.calls[-1].sql
    assert "table_name = 'movie_facets'" in readiness_query
    assert "pg_constraint" in readiness_query
    assert "movie_facets_release_tier" in readiness_query
    assert "movie_facets_release_pilot" in readiness_query
    assert "database_trigger.tgtype" in readiness_query
    assert "database_trigger.tgenabled = 'O'" in readiness_query


def test_ensure_schema_upgrades_a_pre_0002_database_without_missing_regclass_cast(
    repository: RagRepository, connection: RecordingConnection
) -> None:
    """Breaks if catalog readiness casts an absent facet table before running 0002."""
    connection.facets_exists = False
    connection.facets_ready = False
    connection.reject_missing_facet_regclass_cast = True

    repository.ensure_schema()

    assert connection.facets_exists is True
    assert connection.facets_ready is True
    assert len([call for call in connection.calls if "to_regclass" in call.sql]) == 2
    assert any("CREATE TABLE movie_facets" in call.sql for call in connection.calls)
    assert all("'public.movie_facets'::regclass" not in call.sql for call in connection.calls)


def test_ensure_schema_applies_missing_poster_ceiling_forward_migration(
    repository: RagRepository, connection: RecordingConnection
) -> None:
    """Breaks if an already-provisioned database never receives the serving ceiling."""
    connection.poster_ceiling_ready = False

    repository.ensure_schema()

    assert connection.poster_ceiling_ready is True
    assert any(
        "media_assets_poster_byte_ceiling" in call.sql for call in connection.calls
    )
    connection.calls.clear()

    repository.ensure_schema()

    assert len(connection.calls) == 1
    assert "to_regclass" in connection.calls[0].sql


def test_active_bundle_comparison_rejects_one_changed_non_facet_record(
    repository: RagRepository, connection: RecordingConnection
) -> None:
    """Breaks if an active upgrade accepts matching counts with changed persisted content."""
    movie = {"record_kind": "movie", "movie_id": "movie-1", "chinese_title": "原片名"}
    passage = {
        "record_kind": "passage",
        "passage_id": "metadata:movie-1",
        "movie_id": "movie-1",
        "passage_kind": "metadata",
        "page_number": 0,
        "body": "original body",
        "content_sha256": "b" * 64,
    }
    movie_payload = json.dumps(movie, ensure_ascii=False, sort_keys=True, separators=(",", ":"))
    connection.release_status = "active"
    connection.active_bundle_rows = [
        (
            "movie",
            "movie-1",
            {
                "movie_id": "movie-1",
                "payload_sha256": hashlib.sha256(movie_payload.encode()).hexdigest(),
                "payload": movie,
            },
        ),
        (
            "passage",
            "metadata:movie-1",
            {
                "passage_id": "metadata:movie-1",
                "movie_id": "movie-1",
                "document_id": None,
                "passage_kind": "metadata",
                "page_number": 0,
                "body": "tampered body",
                "content_sha256": "b" * 64,
            },
        ),
    ]

    with pytest.raises(RagDatabaseError, match="active bundle content conflict"):
        repository.assert_active_bundle_matches(EXPECTED_RELEASE, (movie, passage))

    assert any("UNION ALL" in call.sql for call in connection.calls)


def test_active_bundle_comparison_accepts_exact_movie_document_chunk_and_poster(
    repository: RagRepository, connection: RecordingConnection
) -> None:
    """Breaks if the exact verified active bundle cannot pass the pre-facet upgrade gate."""
    movie = {"record_kind": "movie", "movie_id": "movie-1", "chinese_title": "原片名"}
    poster = {
        "record_kind": "poster",
        "asset_id": "poster:movie-1:v1",
        "movie_id": "movie-1",
        "asset_type": "poster",
        "is_primary": True,
        "quality_status": "missing",
        "content_sha256": "",
        "original_object_uri": "",
        "derived_object_uri": "",
    }
    document = {
        "record_kind": "document",
        "document_id": "doc-1",
        "movie_id": "movie-1",
        "source_sha256": "c" * 64,
        "source_filename": "source.pdf",
        "rights_status": "restricted",
        "quality_status": "verified",
    }
    passage = {
        "record_kind": "passage",
        "passage_id": "pdf:doc-1:p1",
        "movie_id": "movie-1",
        "document_id": "doc-1",
        "passage_kind": "pdf",
        "page_number": 1,
        "body": "verified body",
        "content_sha256": "d" * 64,
    }
    movie_payload = json.dumps(movie, ensure_ascii=False, sort_keys=True, separators=(",", ":"))
    connection.release_status = "active"
    connection.active_bundle_rows = [
        (
            "document",
            "doc-1",
            {
                "document_id": "doc-1",
                "movie_id": "movie-1",
                "content_sha256": "c" * 64,
                "source_filename": "source.pdf",
                "rights_status": "restricted",
                "quality_status": "verified",
                "payload": document,
            },
        ),
        (
            "movie",
            "movie-1",
            {
                "movie_id": "movie-1",
                "payload_sha256": hashlib.sha256(movie_payload.encode()).hexdigest(),
                "payload": movie,
            },
        ),
        (
            "passage",
            "pdf:doc-1:p1",
            {
                "passage_id": "pdf:doc-1:p1",
                "movie_id": "movie-1",
                "document_id": "doc-1",
                "passage_kind": "pdf",
                "page_number": 1,
                "body": "verified body",
                "content_sha256": "d" * 64,
            },
        ),
        (
            "poster",
            "poster:movie-1:v1",
            {
                "asset_id": "poster:movie-1:v1",
                "movie_id": "movie-1",
                "asset_type": "poster",
                "content_sha256": "",
                "original_object_uri": "",
                "derived_object_uri": "",
                "derived_content_sha256": "",
                "derived_byte_length": None,
                "derived_mime_type": "",
                "is_primary": True,
                "payload": poster,
            },
        ),
    ]

    repository.assert_active_bundle_matches(
        EXPECTED_RELEASE, (movie, poster, document, passage)
    )


def test_recommendation_search_fails_before_join_when_facet_health_is_not_exact(
    repository: RagRepository, connection: RecordingConnection
) -> None:
    """Breaks if a facet join silently narrows recommendations after one row disappears."""
    _configure_recommendation_ready(connection)
    connection.facet_stats_row = (4657, 50, 313, 4294, 24)
    plan = RecommendationPlan(3, ("喜劇",), None, None, None, False, False, (), "喜劇")

    with pytest.raises(RagDatabaseError, match="facets: expected 4658, got 4657"):
        repository.search_recommendations(EXPECTED_RELEASE, [0.1] * 768, plan)

    assert not any("WITH candidates AS" in call.sql for call in connection.calls)


def test_settings_use_cloud_run_socket_and_do_not_echo_secrets(monkeypatch: pytest.MonkeyPatch) -> None:
    """Breaks if Cloud Run loses its Unix-socket route or configuration leaks a password."""
    monkeypatch.setenv("DB_NAME", "rag")
    monkeypatch.setenv("DB_USER", "rag_user")
    monkeypatch.setenv("DB_PASSWORD", "correct-horse-battery-staple")
    monkeypatch.setenv("INSTANCE_CONNECTION_NAME", "project:region:instance")
    monkeypatch.setenv("DB_HOST", "must-not-be-used")

    settings = DatabaseSettings.from_env()

    assert settings.connection_kwargs() == {
        "dbname": "rag",
        "user": "rag_user",
        "password": "correct-horse-battery-staple",
        "host": "/cloudsql/project:region:instance",
    }

    monkeypatch.delenv("DB_NAME")
    with pytest.raises(RagDatabaseError) as raised:
        DatabaseSettings.from_env()
    assert "correct-horse-battery-staple" not in str(raised.value)


def test_connect_uses_explicit_local_host_and_port_only_without_instance(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Breaks if the local-test connection omits the explicitly configured endpoint."""
    received: dict[str, object] = {}
    sentinel = object()

    def record_connect(**kwargs: object) -> object:
        received.update(kwargs)
        return sentinel

    monkeypatch.setattr("hk_movie_rag.rag_db.psycopg.connect", record_connect)
    settings = DatabaseSettings(
        dbname="rag", user="rag_user", password="secret", host="127.0.0.1", port=5433
    )

    assert connect_db(settings) is sentinel
    assert received == {
        "dbname": "rag",
        "user": "rag_user",
        "password": "secret",
        "host": "127.0.0.1",
        "port": 5433,
    }


def test_create_db_pool_opens_with_bounded_five_connection_contract(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Breaks if runtime startup returns before pool readiness or can exceed five DB sessions."""
    received: dict[str, object] = {}
    events: list[tuple[object, ...]] = []

    class FakePool:
        check_connection = staticmethod(lambda _connection: None)

        def __init__(self, **kwargs: object) -> None:
            received.update(kwargs)

        def open(self, *, wait: bool, timeout: float) -> None:
            events.append(("open", wait, timeout))

        def close(self) -> None:
            events.append(("close",))

    monkeypatch.setattr(rag_db, "ConnectionPool", FakePool, raising=False)
    settings = DatabaseSettings(
        dbname="rag", user="rag_user", password="pool-secret", host="127.0.0.1", port=5433
    )

    pool = rag_db.create_db_pool(settings)

    assert isinstance(pool, FakePool)
    assert "pool-secret" not in repr(settings)
    assert received == {
        "kwargs": settings.connection_kwargs(),
        "min_size": 1,
        "max_size": 5,
        "open": False,
        "timeout": rag_db.DB_POOL_CHECKOUT_TIMEOUT_SECONDS,
        "max_waiting": 20,
        "check": FakePool.check_connection,
    }
    assert events == [("open", True, rag_db.DB_POOL_STARTUP_TIMEOUT_SECONDS)]
    assert 0 < rag_db.DB_POOL_CHECKOUT_TIMEOUT_SECONDS <= 30
    assert 0 < rag_db.DB_POOL_STARTUP_TIMEOUT_SECONDS <= 30


def test_create_db_pool_closes_and_redacts_a_failed_readiness_wait(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Breaks if failed pool startup leaks connections or exception/connection details."""
    events: list[tuple[object, ...]] = []

    class FailingPool:
        check_connection = staticmethod(lambda _connection: None)

        def __init__(self, **_kwargs: object) -> None:
            pass

        def open(self, *, wait: bool, timeout: float) -> None:
            events.append(("open", wait, timeout))
            raise RuntimeError("postgresql://rag_user:pool-secret@127.0.0.1/rag")

        def close(self) -> None:
            events.append(("close",))

    monkeypatch.setattr(rag_db, "ConnectionPool", FailingPool, raising=False)
    settings = DatabaseSettings(
        dbname="rag", user="rag_user", password="pool-secret", host="127.0.0.1", port=5433
    )

    with pytest.raises(RagDatabaseError, match="database pool startup failed") as raised:
        rag_db.create_db_pool(settings)

    assert events == [
        ("open", True, rag_db.DB_POOL_STARTUP_TIMEOUT_SECONDS),
        ("close",),
    ]
    formatted = "".join(traceback.format_exception(raised.value))
    assert "pool-secret" not in str(raised.value)
    assert "postgresql" not in str(raised.value)
    assert "pool-secret" not in formatted
    assert "postgresql" not in formatted


def test_pooled_repository_checks_out_once_per_operation_without_connection_overlap() -> None:
    """Breaks if concurrent requests reuse one connection or fail to return pool checkouts."""
    shared_connection = ExclusiveConnection()
    direct_repository = RagRepository(shared_connection)
    start = threading.Barrier(20)

    def direct_read(_index: int) -> object:
        start.wait(timeout=5)
        return direct_repository.release_state(EXPECTED_RELEASE)

    with ThreadPoolExecutor(max_workers=20) as executor:
        direct_futures = [executor.submit(direct_read, index) for index in range(20)]
    direct_failures = [future.exception() for future in direct_futures]
    assert any(isinstance(failure, RagDatabaseError) for failure in direct_failures)
    assert shared_connection.overlap_rejections > 0

    pool = ExclusivePool(size=5)
    pooled_repository = rag_db.RagRepository.from_pool(pool)
    pooled_start = threading.Barrier(20)

    def pooled_read(_index: int) -> object:
        pooled_start.wait(timeout=5)
        return pooled_repository.release_state(EXPECTED_RELEASE)

    with ThreadPoolExecutor(max_workers=20) as executor:
        pooled_results = list(executor.map(pooled_read, range(20)))

    assert len(pooled_results) == 20
    assert pool.checkout_count == 20
    assert 2 <= pool.max_active <= 5
    assert pool.active == 0
    assert pool.available.qsize() == 5
    assert all(connection.overlap_rejections == 0 for connection in pool.connections)


def test_migrate_submits_the_full_ddl_as_one_transactional_operation(
    repository: RagRepository, connection: RecordingConnection
) -> None:
    """Breaks if deploying the repository skips the versioned schema migration."""
    repository.migrate()

    assert len(connection.calls) == 1
    assert "CREATE TABLE document_chunks" in connection.calls[0].sql


def test_ensure_schema_migrates_only_a_fresh_database(
    repository: RagRepository, connection: RecordingConnection
) -> None:
    """Breaks if the ingestion CLI cannot bootstrap once or reruns destructive DDL."""
    connection.schema_exists = False
    connection.schema_ready = False
    repository.ensure_schema()
    assert any("to_regclass" in call.sql for call in connection.calls)
    assert any("CREATE TABLE document_chunks" in call.sql for call in connection.calls)
    assert any("ADD COLUMN IF NOT EXISTS manifest_sha256" in call.sql for call in connection.calls)

    connection.calls.clear()
    connection.schema_exists = True
    connection.schema_ready = True
    repository.ensure_schema()
    assert len(connection.calls) == 1
    assert "to_regclass" in connection.calls[0].sql
    assert "derived_content_sha256" in connection.calls[0].sql
    assert "reject_media_asset_change" in connection.calls[0].sql

    connection.calls.clear()
    connection.schema_exists = True
    connection.schema_ready = False
    with pytest.raises(RagDatabaseError, match="database schema contract is outdated"):
        repository.ensure_schema()
    assert len(connection.calls) == 1


def test_ensure_schema_applies_missing_release_identity_forward_migration(
    repository: RagRepository,
    connection: RecordingConnection,
) -> None:
    """Breaks if an existing pre-0004 database never receives manifest identity gates."""
    connection.release_identity_exists = False
    connection.release_identity_ready = False

    repository.ensure_schema()

    assert any("ADD COLUMN IF NOT EXISTS manifest_sha256" in call.sql for call in connection.calls)
    assert connection.release_identity_exists is True
    assert connection.release_identity_ready is True


def test_ensure_schema_upgrades_the_existing_v3_identity_gate_to_v4(
    repository: RagRepository,
    connection: RecordingConnection,
) -> None:
    """Breaks if the deployed R2 database cannot accept a contract-driven R3 child."""
    connection.release_identity_ready = False

    repository.ensure_schema()

    migration_calls = [
        call.sql
        for call in connection.calls
        if "rag_release_manifest_identity_v4_is_valid" in call.sql
        and "CREATE OR REPLACE FUNCTION" in call.sql
    ]
    assert len(migration_calls) == 1
    assert connection.release_identity_ready is True


def test_release_identity_schema_readiness_verifies_catalog_definitions_and_trigger_shape(
    repository: RagRepository,
    connection: RecordingConnection,
) -> None:
    """Breaks if a same-named but weakened constraint or trigger is accepted as ready."""
    repository.ensure_schema()

    status_sql = connection.calls[0].sql
    normalized_status_sql = " ".join(status_sql.split())
    assert "pg_get_constraintdef" in status_sql
    assert "pg_get_constraintdef(identity_constraint.oid, true) = %s" in status_sql
    assert "FROM pg_depend AS validator_dependency" in status_sql
    assert (
        "validator_dependency.objid = identity_constraint.oid"
        in normalized_status_sql
    )
    assert (
        "validator_dependency.refclassid = 'pg_proc'::regclass"
        in normalized_status_sql
    )
    assert "validator_dependency.refobjid = to_regprocedure(" in status_sql
    assert "identity_validator.prosrc" in status_sql
    assert "trigger_function.prosrc" in status_sql
    assert "database_trigger.tgtype = 23" in status_sql
    assert "database_trigger.tgenabled = 'O'" in status_sql
    assert "database_trigger.tgqual IS NULL" in status_sql
    assert "database_trigger.tgnargs = 0" in status_sql
    assert "database_trigger.tgattr = ''::int2vector" in status_sql
    assert "database_trigger.tgparentid = 0" in status_sql
    assert "database_trigger.tgconstraint = 0" in status_sql
    assert "database_trigger.tgoldtable IS NULL" in status_sql
    assert "database_trigger.tgnewtable IS NULL" in status_sql
    assert "identity_validator.proconfig =" in status_sql
    assert "trigger_function.proconfig =" in status_sql
    assert status_sql.count("ARRAY['search_path=pg_catalog']::text[]") == 2
    assert "'rag_release_manifest_identity_v4_is_valid'" in status_sql
    assert "identity_validator.pronargs = 13" in status_sql
    assert "identity_validator.pronargdefaults = 0" in status_sql
    assert "identity_validator.prosupport = 0" in status_sql
    assert "trigger_function.prosupport = 0" in status_sql
    assert "trigger_function.pronamespace = to_regnamespace('public')" in status_sql


def test_release_identity_schema_readiness_uses_exact_catalog_definitions(
    repository: RagRepository,
    connection: RecordingConnection,
) -> None:
    """Breaks if catalog readiness can be satisfied by scattered source snippets."""
    status = repository._schema_status()

    assert status[-1] is True
    call = connection.calls[-1]
    assert "pg_get_constraintdef(identity_constraint.oid, true) = %s" in call.sql
    assert "btrim(regexp_replace(identity_validator.prosrc" in call.sql
    assert "btrim(regexp_replace(trigger_function.prosrc" in call.sql
    assert "regexp_replace(btrim(identity_validator.prosrc)" not in call.sql
    assert "regexp_replace(btrim(trigger_function.prosrc)" not in call.sql
    assert "identity_validator.prorettype = 'boolean'::regtype" in call.sql
    assert "identity_validator.provolatile = 'i'" in call.sql
    assert "trigger_function.prorettype = 'trigger'::regtype" in call.sql
    assert "trigger_function.provolatile = 'v'" in call.sql
    assert call.params == (
        EXPECTED_IDENTITY_CHECK_DEFINITION,
        EXPECTED_IDENTITY_VALIDATOR_SOURCE,
        EXPECTED_IDENTITY_TRIGGER_SOURCE,
    )


def test_schema_status_escapes_literal_percent_when_binding_catalog_definitions(
    repository: RagRepository,
    connection: RecordingConnection,
) -> None:
    """Breaks if psycopg parses the poster LIKE pattern as a malformed placeholder."""
    repository._schema_status()

    assert "LIKE '%%derived_byte_length <= 16777216%%'" in " ".join(
        connection.calls[-1].sql.split()
    )


@pytest.mark.parametrize(
    ("catalog_field", "altered_definition"),
    [
        (
            "release_identity_constraint_definition",
            EXPECTED_IDENTITY_CHECK_DEFINITION[:-1] + " OR TRUE)",
        ),
        (
            "release_identity_validator_source",
            "SELECT TRUE OR ("
            + EXPECTED_IDENTITY_VALIDATOR_SOURCE.removeprefix("SELECT ").removesuffix(";")
            + ");",
        ),
        (
            "release_identity_trigger_source",
            EXPECTED_IDENTITY_TRIGGER_SOURCE.replace(
                "BEGIN IF TG_OP",
                "BEGIN IF FALSE THEN RAISE NOTICE 'unreachable expected snippets'; "
                "END IF; IF TG_OP",
                1,
            ),
        ),
    ],
)
def test_release_identity_schema_readiness_rejects_altered_exact_definitions(
    repository: RagRepository,
    connection: RecordingConnection,
    catalog_field: str,
    altered_definition: str,
) -> None:
    """Breaks if OR TRUE or an altered function body is accepted as equivalent."""
    setattr(connection, catalog_field, altered_definition)

    assert repository._schema_status()[-1] is False


def test_release_identity_schema_readiness_rejects_trigger_when_bypass(
    repository: RagRepository,
    connection: RecordingConnection,
) -> None:
    """Breaks if a same-named ``WHEN (false)`` trigger is accepted as ready."""
    connection.release_identity_trigger_has_predicate = True

    assert repository._schema_status()[-1] is False


@pytest.mark.parametrize(
    "catalog_field",
    (
        "release_identity_validator_safe_config",
        "release_identity_trigger_safe_config",
    ),
)
def test_release_identity_schema_readiness_rejects_function_config_drift(
    repository: RagRepository,
    connection: RecordingConnection,
    catalog_field: str,
) -> None:
    """Breaks if an altered function search path can retain ready status."""
    setattr(connection, catalog_field, False)

    assert repository._schema_status()[-1] is False


def test_release_identity_schema_readiness_rejects_shadow_validator_constraint(
    repository: RagRepository,
    connection: RecordingConnection,
) -> None:
    """Breaks if the CHECK can call a shadow-schema function with matching deparse."""
    connection.release_identity_constraint_uses_public_validator = False

    assert repository._schema_status()[-1] is False


def test_begin_release_rejects_contract_outside_the_parent_dataset_invariants(
    repository: RagRepository, connection: RecordingConnection
) -> None:
    """Breaks if a caller can create an empty release by supplying zero expected counts."""
    empty = replace(
        _bundle(),
        movie_count=0,
        poster_row_count=0,
        metadata_passage_count=0,
        document_count=0,
        pdf_passage_count=0,
    )

    with pytest.raises(RagDatabaseError, match="verified release counts"):
        repository.begin_release(empty.contract)
    assert connection.calls == []


def test_release_contract_accepts_a_verified_supplemental_overlay_census() -> None:
    """Breaks if runtime persistence stays pinned to the 4,658-row R2 parent."""
    base = _bundle().contract
    r3 = replace(
        base,
        schema_version="1.3",
        rag_release_id="v1.2-demo-r3",
        counts=replace(
            base.counts,
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
    )

    RagRepository._validate_release_contract(r3)


def test_contract_driven_identity_migration_has_no_parent_census_literal() -> None:
    """Breaks if PostgreSQL accepts R2 but rejects a manifest-bound child overlay."""
    migration = CONTRACT_DRIVEN_IDENTITY_MIGRATION_PATH.read_text(encoding="utf-8")

    assert "rag_release_manifest_identity_v4_is_valid" in migration
    assert "= 4658" not in migration
    assert "'counts' ->> 'movies'" in migration
    assert "'counts' ->> 'poster_rows'" in migration
    assert "'counts' ->> 'metadata_passages'" in migration


def test_begin_release_rejects_internally_inconsistent_poster_semantics(
    repository: RagRepository, connection: RecordingConnection
) -> None:
    """Breaks if approved and unavailable rows can exceed the selected primary census."""
    wrong_split = replace(
        _bundle(),
        approved_poster_object_count=4658,
        unavailable_poster_row_count=113,
    )

    with pytest.raises(RagDatabaseError, match="count relationships"):
        repository.begin_release(wrong_split.contract)
    assert connection.calls == []


def test_begin_release_rejects_blank_release_id_before_database_access(
    repository: RagRepository, connection: RecordingConnection
) -> None:
    """Breaks if a verified contract can create an unaddressable release row."""
    contract = replace(_bundle(), rag_release_id="").contract

    with pytest.raises(RagDatabaseError, match="release ID"):
        repository.begin_release(contract)
    assert connection.calls == []


def test_begin_release_and_upsert_records_use_bound_parameterized_rows(
    repository: RagRepository, connection: RecordingConnection
) -> None:
    """Breaks if bundle records are interpolated, skipped, or written outside their release."""
    repository.begin_release(_bundle().contract)
    repository.upsert_bundle_records(
        EXPECTED_RELEASE,
        [
            {"record_kind": "movie", "movie_id": "1978_ZQ_001", "chinese_title": "醉拳"},
            {
                "record_kind": "poster",
                "asset_id": "poster:1978_ZQ_001:v1",
                "movie_id": "1978_ZQ_001",
                "content_sha256": "a" * 64,
                "original_object_uri": "gs://demo/poster.jpg",
                "derived_object_uri": "assets/posters/derived/1978_ZQ_001.webp",
                "derived_content_sha256": "d" * 64,
                "derived_byte_length": 38140,
                "derived_mime_type": "image/webp",
                "quality_status": "machine_passed",
                "is_primary": True,
            },
            {
                "record_kind": "document",
                "document_id": "drunken-master",
                "movie_id": "1978_ZQ_001",
                "source_sha256": "b" * 64,
                "source_filename": "drunken.pdf",
                "rights_status": "restricted",
                "quality_status": "manual_approved",
            },
            {
                "record_kind": "passage",
                "passage_id": "metadata:1978_ZQ_001",
                "passage_kind": "metadata",
                "movie_id": "1978_ZQ_001",
                "document_id": "",
                "page_number": 0,
                "body": "chinese_title: 醉拳",
                "content_sha256": "c" * 64,
            },
        ],
    )

    writes = [call for call in connection.calls if "INSERT INTO" in call.sql]
    assert len(writes) == 5
    assert all("醉拳" not in call.sql for call in writes)
    assert {call.params[0] for call in writes} == {EXPECTED_RELEASE}
    assert any("INSERT INTO document_chunks" in call.sql for call in writes)
    asset_write = next(call for call in writes if "INSERT INTO media_assets" in call.sql)
    assert "derived_content_sha256" in asset_write.sql
    assert "derived_byte_length" in asset_write.sql
    assert "derived_mime_type" in asset_write.sql
    assert "a" * 64 in asset_write.params
    assert "d" * 64 in asset_write.params
    assert 38140 in asset_write.params
    assert "image/webp" in asset_write.params


def test_upsert_accepts_missing_poster_without_original_or_derived_identity(
    repository: RagRepository,
) -> None:
    """Breaks if one of the 31 governed missing rows makes the 4,658-row load impossible."""
    repository.upsert_bundle_records(
        EXPECTED_RELEASE,
        [
            {"record_kind": "movie", "movie_id": "missing_movie"},
            {
                "record_kind": "poster",
                "asset_id": "poster:missing_movie:v1",
                "movie_id": "missing_movie",
                "content_sha256": "",
                "original_object_uri": "",
                "derived_object_uri": "",
                "quality_status": "missing",
                "is_primary": True,
            },
        ],
    )

    asset_write = next(
        call for call in repository.connection.calls if "INSERT INTO media_assets" in call.sql
    )
    assert "" in asset_write.params
    assert None in asset_write.params


@pytest.mark.parametrize(
    "field,value",
    [
        ("derived_content_sha256", None),
        ("derived_byte_length", 0),
        ("derived_byte_length", 16 * 1024 * 1024 + 1),
        ("derived_mime_type", "image/jpeg"),
        ("derived_object_uri", "assets/posters/derived/other.webp"),
    ],
)
def test_upsert_rejects_partial_or_noncanonical_approved_derivative(
    repository: RagRepository, field: str, value: object
) -> None:
    """Breaks if a caller can persist an approved poster outside the verified bundle contract."""
    poster: dict[str, object] = {
        "record_kind": "poster",
        "asset_id": "poster:1978_ZQ_001:v1",
        "movie_id": "1978_ZQ_001",
        "asset_type": "poster",
        "content_sha256": "a" * 64,
        "original_object_uri": "assets/posters/originals/1978_ZQ_001.jpg",
        "derived_object_uri": "assets/posters/derived/1978_ZQ_001.webp",
        "derived_content_sha256": "d" * 64,
        "derived_byte_length": 38140,
        "derived_mime_type": "image/webp",
        "quality_status": "machine_passed",
        "is_primary": True,
    }
    if value is None:
        del poster[field]
    else:
        poster[field] = value

    with pytest.raises(RagDatabaseError, match="poster derivative identity is invalid"):
        repository.upsert_bundle_records(EXPECTED_RELEASE, [poster])


def test_begin_release_inserts_dynamic_document_counts_and_verified_identity(
    repository: RagRepository, connection: RecordingConnection
) -> None:
    """Breaks if the DB substitutes legacy 2/6 counts or omits verified identity."""
    target = replace(_bundle(), rag_release_id="v1.2-demo-r2", document_count=3, pdf_passage_count=12)
    connection.expected_counts = (4658, 4658, 4658, 3, 12)
    connection.release_id = target.rag_release_id
    connection.release_manifest_sha256 = target.manifest_sha256
    connection.release_embedding_profile = target.document_embedding_profile
    connection.release_contract_json = asdict(target.contract)

    assert repository.begin_release(target.contract) == target.rag_release_id

    insert = next(call for call in connection.calls if "INSERT INTO rag_releases" in call.sql)
    assert insert.params[:-1] == (
        target.rag_release_id,
        4658,
        4658,
        4658,
        3,
        12,
        target.embedding_model,
        768,
        target.generation_model,
        target.access_mode,
        target.manifest_sha256,
        target.document_embedding_profile,
    )
    persisted_contract = json.loads(str(insert.params[-1]))
    assert persisted_contract == asdict(target.contract)
    assert persisted_contract["derived_inventory_sha256"] == "d" * 64


def test_begin_release_allows_identical_interrupted_loading_contract(
    repository: RagRepository, connection: RecordingConnection
) -> None:
    """Breaks if a retry cannot resume the exact locked loading release."""
    connection.expected_counts = (4658, 4658, 4658, 3, 12)
    contract = replace(_bundle(), document_count=3, pdf_passage_count=12).contract
    connection.release_contract_json = asdict(contract)

    assert repository.begin_release(contract) == EXPECTED_RELEASE
    assert not any("UPDATE rag_releases" in call.sql for call in connection.calls)


def test_begin_release_completes_an_exact_edc6ae6_loading_contract_under_lock(
    repository: RagRepository,
    connection: RecordingConnection,
) -> None:
    """Breaks if a valid two-field loading release cannot receive contract_json once."""
    contract = _bundle().contract
    connection.release_status = "loading"
    connection.release_manifest_sha256 = contract.manifest_sha256
    connection.release_embedding_profile = contract.document_embedding_profile
    connection.release_contract_json = None

    assert repository.begin_release(contract) == EXPECTED_RELEASE

    transaction = connection.transactions[-1]
    lock_index = next(
        index
        for index, call in enumerate(transaction)
        if "FROM rag_releases AS release" in call.sql and "FOR UPDATE" in call.sql
    )
    claim_index, claim = next(
        (index, call)
        for index, call in enumerate(transaction)
        if "SET contract_json = %s::jsonb" in call.sql
    )
    assert claim_index > lock_index
    assert "SET manifest_sha256" not in claim.sql
    assert "status = 'loading'" in claim.sql
    assert "manifest_sha256 = %s" in claim.sql
    assert "document_embedding_profile = %s" in claim.sql
    assert json.loads(str(claim.params[0])) == asdict(contract)
    assert claim.params[1:] == (
        EXPECTED_RELEASE,
        contract.manifest_sha256,
        contract.document_embedding_profile,
    )


def test_begin_release_completes_a_pre_0004_loading_identity_under_lock(
    repository: RagRepository,
    connection: RecordingConnection,
) -> None:
    """Breaks if a pre-identity loading release cannot resume before record writes."""
    contract = _bundle().contract
    connection.release_status = "loading"
    connection.release_manifest_sha256 = None
    connection.release_embedding_profile = None
    connection.release_contract_json = None

    assert repository.begin_release(contract) == EXPECTED_RELEASE

    transaction = connection.transactions[-1]
    lock_index = next(
        index
        for index, call in enumerate(transaction)
        if "FROM rag_releases AS release" in call.sql and "FOR UPDATE" in call.sql
    )
    claim_index, claim = next(
        (index, call)
        for index, call in enumerate(transaction)
        if "SET manifest_sha256 = %s" in call.sql
    )
    assert claim_index > lock_index
    assert "status = 'loading'" in claim.sql
    assert "manifest_sha256 IS NULL" in claim.sql
    assert "document_embedding_profile IS NULL" in claim.sql
    assert "contract_json IS NULL" in claim.sql
    assert claim.params[:2] == (
        contract.manifest_sha256,
        contract.document_embedding_profile,
    )
    assert json.loads(str(claim.params[2])) == asdict(contract)
    assert claim.params[3] == EXPECTED_RELEASE


@pytest.mark.parametrize(
    ("manifest_sha256", "profile", "contract_payload"),
    [
        (None, "vertex-title-text-v1", None),
        ("a" * 64, None, None),
        ("not-a-sha", "vertex-title-text-v1", None),
        ("a" * 64, "", None),
        (None, None, {"schema_version": "1.2"}),
    ],
)
def test_begin_release_rejects_partial_invalid_stored_identity_states(
    repository: RagRepository,
    connection: RecordingConnection,
    manifest_sha256: str | None,
    profile: str | None,
    contract_payload: Mapping[str, object] | None,
) -> None:
    """Breaks if any state outside legacy, transitional, or complete can resume."""
    connection.release_manifest_sha256 = manifest_sha256
    connection.release_embedding_profile = profile
    connection.release_contract_json = contract_payload

    with pytest.raises(RagDatabaseError, match="stored release contract is invalid"):
        repository.begin_release(_bundle().contract)

    assert not any("SET contract_json = %s::jsonb" in call.sql for call in connection.calls)


@pytest.mark.parametrize(
    ("field", "value"),
    [
        ("manifest_sha256", "f" * 64),
        ("document_count", 3),
        ("embedding_model", "gemini-embedding-001"),
        ("document_embedding_profile", "other-profile"),
    ],
)
def test_begin_release_rejects_release_id_contract_drift(
    repository: RagRepository,
    field: str,
    value: object,
) -> None:
    """Breaks if one release ID can silently acquire a different immutable contract."""
    changed = replace(_bundle(), **{field: value}).contract
    with pytest.raises(RagDatabaseError, match="release contract is immutable"):
        repository.begin_release(changed)


@pytest.mark.parametrize(
    ("field_name", "replacement"),
    [
        ("schema_version", "1.3"),
        ("parent_release_manifest_sha256", "f" * 64),
        ("bundle_sha256", "f" * 64),
        ("derived_inventory_sha256", "f" * 64),
        ("generation_model", "other-generation-model"),
        ("text_extraction_profile", "other-extraction-profile"),
        ("relevance_policy_sha256", "f" * 64),
        ("poster_authority_sha256", "f" * 64),
        ("access_mode", "other-access-mode"),
    ],
)
def test_begin_release_rejects_drift_in_every_complete_contract_identity_family(
    repository: RagRepository,
    field_name: str,
    replacement: object,
) -> None:
    """Breaks if contract_json stores fields that resume comparison silently ignores."""
    changed = replace(_bundle().contract, **{field_name: replacement})

    with pytest.raises(RagDatabaseError, match="release contract is immutable"):
        repository.begin_release(changed)


def test_active_bundle_check_claims_both_null_legacy_identities_once_under_lock(
    repository: RagRepository, connection: RecordingConnection
) -> None:
    """Breaks if an exact verified legacy row cannot be claimed atomically once."""
    connection.release_manifest_sha256 = None
    connection.release_embedding_profile = None
    connection.release_contract_json = None
    connection.release_status = "active"
    connection.expected_counts = (4658, 4658, 4658, 2, 6)
    record = {"record_kind": "movie", "movie_id": "1978_ZQ_001", "chinese_title": "醉拳"}
    payload_json = json.dumps(
        record, ensure_ascii=False, sort_keys=True, separators=(",", ":")
    )
    connection.active_bundle_rows = [
        (
            "movie",
            "1978_ZQ_001",
            {
                "movie_id": "1978_ZQ_001",
                "payload_sha256": hashlib.sha256(payload_json.encode("utf-8")).hexdigest(),
                "payload": record,
            },
        )
    ]
    facets = _facet_records()
    connection.facet_rows = _stored_facet_rows(facets)

    repository.assert_active_bundle_matches(
        EXPECTED_RELEASE,
        (record, *facets),
        contract=_bundle().contract,
    )

    update = next(call for call in connection.calls if "SET manifest_sha256" in call.sql)
    assert update.params[:2] == ("a" * 64, "vertex-title-text-v1")
    assert json.loads(str(update.params[2])) == asdict(_bundle().contract)
    assert update.params[3] == EXPECTED_RELEASE


def test_active_bundle_check_completes_transitional_contract_only_after_exact_comparison(
    repository: RagRepository,
    connection: RecordingConnection,
) -> None:
    """Breaks if an edc6ae6 active row can claim contract_json before content proof."""
    contract = _bundle().contract
    connection.release_status = "active"
    connection.release_manifest_sha256 = contract.manifest_sha256
    connection.release_embedding_profile = contract.document_embedding_profile
    connection.release_contract_json = None
    movie = {
        "record_kind": "movie",
        "movie_id": "movie-0000",
        "chinese_title": "原片名",
    }
    payload_json = json.dumps(
        movie, ensure_ascii=False, sort_keys=True, separators=(",", ":")
    )
    facets = _facet_records()
    connection.active_bundle_rows = [
        (
            "movie",
            "movie-0000",
            {
                "movie_id": "movie-0000",
                "payload_sha256": hashlib.sha256(payload_json.encode("utf-8")).hexdigest(),
                "payload": movie,
            },
        )
    ]
    connection.facet_rows = _stored_facet_rows(facets)

    repository.assert_active_bundle_matches(
        EXPECTED_RELEASE,
        (movie, *facets),
        contract=contract,
    )

    transaction = connection.transactions[-1]
    claim_index, claim = next(
        (index, call)
        for index, call in enumerate(transaction)
        if "SET contract_json = %s::jsonb" in call.sql
    )
    facet_index = next(
        index for index, call in enumerate(transaction) if "FROM movie_facets" in call.sql
    )
    assert claim_index > facet_index
    assert "SET manifest_sha256" not in claim.sql
    assert "status = 'active'" in claim.sql
    assert claim.params[1:] == (
        EXPECTED_RELEASE,
        contract.manifest_sha256,
        contract.document_embedding_profile,
    )


@pytest.mark.parametrize("tamper", ["missing", "mismatch"])
def test_legacy_active_claim_requires_the_exact_complete_facet_set_without_mutation(
    repository: RagRepository,
    connection: RecordingConnection,
    tamper: str,
) -> None:
    """Breaks if a missing or changed active facet can be hidden by the identity claim."""
    movie = {
        "record_kind": "movie",
        "movie_id": "movie-0000",
        "chinese_title": "原片名",
    }
    payload_json = json.dumps(
        movie, ensure_ascii=False, sort_keys=True, separators=(",", ":")
    )
    facets = _facet_records()
    stored_facets = _stored_facet_rows(facets)
    if tamper == "missing":
        stored_facets.pop()
    else:
        changed = list(stored_facets[0])
        changed[2] = "B"
        stored_facets[0] = tuple(changed)
    connection.release_status = "active"
    connection.release_manifest_sha256 = None
    connection.release_embedding_profile = None
    connection.release_contract_json = None
    connection.active_bundle_rows = [
        (
            "movie",
            "movie-0000",
            {
                "movie_id": "movie-0000",
                "payload_sha256": hashlib.sha256(payload_json.encode("utf-8")).hexdigest(),
                "payload": movie,
            },
        )
    ]
    connection.facet_rows = stored_facets

    with pytest.raises(RagDatabaseError, match="facet (coverage|content) conflict"):
        repository.assert_active_bundle_matches(
            EXPECTED_RELEASE,
            (movie, *facets),
            contract=_bundle().contract,
        )

    assert not any("SET manifest_sha256" in call.sql for call in connection.calls)


def test_legacy_active_claim_persists_the_complete_contract_in_the_same_transaction(
    repository: RagRepository,
    connection: RecordingConnection,
) -> None:
    """Breaks if a claimed manifest can later be paired with a different bundle contract."""
    movie = {
        "record_kind": "movie",
        "movie_id": "movie-0000",
        "chinese_title": "原片名",
    }
    payload_json = json.dumps(
        movie, ensure_ascii=False, sort_keys=True, separators=(",", ":")
    )
    facets = _facet_records()
    connection.release_status = "active"
    connection.release_manifest_sha256 = None
    connection.release_embedding_profile = None
    connection.release_contract_json = None
    connection.active_bundle_rows = [
        (
            "movie",
            "movie-0000",
            {
                "movie_id": "movie-0000",
                "payload_sha256": hashlib.sha256(payload_json.encode("utf-8")).hexdigest(),
                "payload": movie,
            },
        )
    ]
    connection.facet_rows = _stored_facet_rows(facets)

    repository.assert_active_bundle_matches(
        EXPECTED_RELEASE,
        (movie, *facets),
        contract=_bundle().contract,
    )

    transaction = connection.transactions[-1]
    claim = next(call for call in transaction if "SET manifest_sha256" in call.sql)
    assert "contract_json" in claim.sql
    assert "schema_version" in json.dumps(claim.params[2], sort_keys=True)
    assert transaction.index(claim) > next(
        index for index, call in enumerate(transaction) if "FROM movie_facets" in call.sql
    )


def test_reuse_embeddings_copies_only_exact_null_target_vectors_inside_postgres(
    repository: RagRepository, connection: RecordingConnection
) -> None:
    """Breaks if vectors leave PostgreSQL or any embedding-input identity can differ."""
    result = repository.reuse_embeddings("v1.2-demo-r2", EXPECTED_RELEASE)

    assert result.source_eligible_total == 4658
    assert result.copied_this_run == 4658

    update = next(
        call for call in connection.calls if "UPDATE document_chunks AS target" in call.sql
    )
    assert update.params == ("v1.2-demo-r2", EXPECTED_RELEASE)
    assert "SET embedding = eligible.source_embedding" in update.sql
    assert "target.embedding IS NULL" in update.sql
    assert "source.embedding IS NOT NULL" in update.sql
    for equality in (
        "source.passage_id = target.passage_id",
        "source.movie_id = target.movie_id",
        "source.passage_kind = target.passage_kind",
        "source.document_id IS NOT DISTINCT FROM target.document_id",
        "source.page_number = target.page_number",
        "source.body = target.body",
        "source.content_sha256 = target.content_sha256",
        "source_title.embedding_title = target_title.embedding_title",
    ):
        assert equality in update.sql


@pytest.mark.parametrize(
    ("target", "source_status", "target_status", "source_profile", "target_profile"),
    [
        (EXPECTED_RELEASE, "active", "loading", "vertex-title-text-v1", "vertex-title-text-v1"),
        ("v1.2-demo-r2", "loading", "loading", "vertex-title-text-v1", "vertex-title-text-v1"),
        ("v1.2-demo-r2", "active", "active", "vertex-title-text-v1", "vertex-title-text-v1"),
        ("v1.2-demo-r2", "active", "loading", "historical-title", "vertex-title-text-v1"),
    ],
)
def test_reuse_embeddings_rejects_unsafe_release_contract_before_copy(
    repository: RagRepository,
    connection: RecordingConnection,
    target: str,
    source_status: str,
    target_status: str,
    source_profile: str,
    target_profile: str,
) -> None:
    """Breaks if source/target state, identity, or profile validation can fail open."""
    connection.reuse_contract_row = (
        source_status,
        target_status,
        "gemini-embedding-2",
        "gemini-embedding-2",
        768,
        768,
        source_profile,
        target_profile,
    )

    with pytest.raises(RagDatabaseError, match="embedding reuse contract"):
        repository.reuse_embeddings(target, EXPECTED_RELEASE)
    assert not any("UPDATE document_chunks AS target" in call.sql for call in connection.calls)


def test_embedding_reuse_stats_revalidates_source_and_target_release_states(
    repository: RagRepository,
    connection: RecordingConnection,
) -> None:
    """Breaks if final-state acceptance can trust an inactive reuse source."""
    connection.reuse_contract_row = (
        "loading",
        "active",
        "gemini-embedding-2",
        "gemini-embedding-2",
        768,
        768,
        "vertex-title-text-v1",
        "vertex-title-text-v1",
    )

    with pytest.raises(RagDatabaseError, match="embedding reuse contract"):
        repository.embedding_reuse_stats("v1.2-demo-r2", EXPECTED_RELEASE)

    assert not any("source_eligible_total" in call.sql for call in connection.calls)


def test_activate_release_requires_exact_counts(repository: RagRepository) -> None:
    """Breaks if a partial metadata ingest can become an active searchable release."""
    repository.connection.expected_counts = (4658, 4658, 4658, 2, 6)
    repository.connection.actual_counts = (4658, 4658, 4657, 2, 6, 4663)

    with pytest.raises(RagDatabaseError, match="metadata passages: expected 4658, got 4657"):
        repository.activate_release(
            EXPECTED_RELEASE,
            embedding_model="gemini-embedding-2",
            embedding_dimension=768,
        )


def test_activate_release_requires_exact_persisted_poster_semantics(
    repository: RagRepository,
) -> None:
    """Breaks if 4,658 assets with a falsified approved/unavailable split can activate."""
    repository.connection.actual_counts = (4658, 4658, 4658, 2, 6, 4664)
    repository.connection.poster_semantic_counts = (4658, 4658, 0)

    with pytest.raises(
        RagDatabaseError, match="approved poster objects: expected 4545, got 4658"
    ):
        repository.activate_release(
            EXPECTED_RELEASE,
            embedding_model="gemini-embedding-2",
            embedding_dimension=768,
        )


def test_activate_release_does_not_substitute_total_assets_for_primary_posters(
    repository: RagRepository,
) -> None:
    """Breaks if one non-primary asset can conceal a missing primary poster row."""
    repository.connection.actual_counts = (4658, 4658, 4658, 2, 6, 4664)
    repository.connection.poster_semantic_counts = (4657, 4545, 113)

    with pytest.raises(RagDatabaseError, match="primary poster rows: expected 4658, got 4657"):
        repository.activate_release(
            EXPECTED_RELEASE,
            embedding_model="gemini-embedding-2",
            embedding_dimension=768,
        )


def _configure_query_ready(connection: RecordingConnection) -> None:
    connection.release_status = "active"
    connection.expected_counts = (4658, 4658, 4658, 2, 6)
    connection.actual_counts = (4658, 4658, 4658, 2, 6, 4664)
    connection.facet_stats_row = (4658, 50, 313, 4295, 24)
    connection.poster_semantic_counts = (4658, 4545, 113)
    connection.release_embedding_model = "gemini-embedding-2"
    connection.release_embedding_dimension = 768
    connection.release_generation_model = "gemini-3.5-flash-lite"
    connection.release_access_mode = "restricted_demo"


@pytest.mark.parametrize(
    ("index", "observed", "message"),
    [
        (0, 4657, "movies: expected 4658, got 4657"),
        (1, 4659, "assets: expected 4658, got 4659"),
        (2, 4657, "metadata passages: expected 4658, got 4657"),
        (3, 1, "documents: expected 2, got 1"),
        (4, 7, "pdf passages: expected 6, got 7"),
        (5, 4663, "embeddings: expected 4664, got 4663"),
    ],
)
def test_assert_query_ready_rejects_each_release_count_tamper(
    repository: RagRepository,
    connection: RecordingConnection,
    index: int,
    observed: int,
    message: str,
) -> None:
    """Breaks if one partial or surplus release count can remain queryable."""
    _configure_query_ready(connection)
    counts = list(connection.actual_counts)
    counts[index] = observed
    connection.actual_counts = tuple(counts)  # type: ignore[assignment]

    with pytest.raises(RagDatabaseError, match=re.escape(message)):
        repository.assert_query_ready(EXPECTED_RELEASE)


@pytest.mark.parametrize(
    ("index", "observed", "message"),
    [
        (0, 4657, "facets: expected 4658, got 4657"),
        (1, 49, "tier S: expected 50, got 49"),
        (2, 314, "tier A: expected 313, got 314"),
        (3, 4294, "tier B: expected 4295, got 4294"),
        (4, 23, "pilot facets: expected 24, got 23"),
    ],
)
def test_assert_query_ready_rejects_each_facet_count_tamper(
    repository: RagRepository,
    connection: RecordingConnection,
    index: int,
    observed: int,
    message: str,
) -> None:
    """Breaks if one facet census drift can remain queryable."""
    _configure_query_ready(connection)
    counts = list(connection.facet_stats_row)
    counts[index] = observed
    connection.facet_stats_row = tuple(counts)  # type: ignore[assignment]

    with pytest.raises(RagDatabaseError, match=re.escape(message)):
        repository.assert_query_ready(EXPECTED_RELEASE)


@pytest.mark.parametrize(
    ("index", "observed", "message"),
    [
        (0, 4657, "primary poster rows: expected 4658, got 4657"),
        (1, 4544, "approved poster objects: expected 4545, got 4544"),
        (2, 114, "unavailable poster rows: expected 113, got 114"),
    ],
)
def test_assert_query_ready_rejects_each_poster_semantic_tamper(
    repository: RagRepository,
    connection: RecordingConnection,
    index: int,
    observed: int,
    message: str,
) -> None:
    """Breaks if primary, approved, or unavailable poster semantics drift."""
    _configure_query_ready(connection)
    counts = list(connection.poster_semantic_counts)
    counts[index] = observed
    connection.poster_semantic_counts = tuple(counts)  # type: ignore[assignment]

    with pytest.raises(RagDatabaseError, match=re.escape(message)):
        repository.assert_query_ready(EXPECTED_RELEASE)


@pytest.mark.parametrize(
    ("field_name", "value"),
    [
        ("release_embedding_dimension", 1536),
        ("release_manifest_sha256", None),
        ("release_embedding_profile", None),
    ],
)
def test_assert_query_ready_rejects_invalid_selected_release_identity(
    repository: RagRepository,
    connection: RecordingConnection,
    field_name: str,
    value: object,
) -> None:
    """Breaks if an unbound manifest/profile or wrong physical dimension is queryable."""
    _configure_query_ready(connection)
    setattr(connection, field_name, value)

    with pytest.raises(RagDatabaseError, match="active release contract is unavailable"):
        repository.assert_query_ready(EXPECTED_RELEASE)


def test_assert_query_ready_rejects_selected_release_parent_count_contract_drift(
    repository: RagRepository,
    connection: RecordingConnection,
) -> None:
    """Breaks if a selected release row can redefine the fixed parent census."""
    _configure_query_ready(connection)
    connection.expected_counts = (4657, 4658, 4658, 2, 6)
    connection.actual_counts = (4657, 4658, 4658, 2, 6, 4664)

    with pytest.raises(RagDatabaseError, match="release contract is immutable"):
        repository.assert_query_ready(EXPECTED_RELEASE)


def test_assert_query_ready_uses_selected_contract_facet_and_poster_expectations(
    repository: RagRepository,
    connection: RecordingConnection,
) -> None:
    """Breaks if readiness substitutes module facet/poster constants for the selected row."""
    _configure_query_ready(connection)
    selected = replace(
        _bundle().contract,
        counts=replace(
            _bundle().contract.counts,
            tier_s_count=51,
            tier_a_count=312,
            approved_poster_objects=4544,
            unavailable_poster_rows=114,
        ),
    )
    connection.release_contract_json = asdict(selected)
    connection.facet_stats_row = (4658, 51, 312, 4295, 24)
    connection.poster_semantic_counts = (4658, 4544, 114)

    repository.assert_query_ready(EXPECTED_RELEASE)


def test_query_ready_contract_returns_the_exact_persisted_active_authority(
    repository: RagRepository,
    connection: RecordingConnection,
) -> None:
    """Breaks if runtime must reconstruct release authority from environment values."""
    _configure_query_ready(connection)
    expected = _bundle().contract

    assert repository.query_ready_contract(EXPECTED_RELEASE) == expected


def test_question_search_uses_persisted_contract_facet_expectations(
    repository: RagRepository,
    connection: RecordingConnection,
) -> None:
    """Breaks if runtime search silently reverts to the R2 facet census."""
    parent = _bundle().contract
    selected = replace(
        parent,
        schema_version="1.3",
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
    )
    connection.release_status = "active"
    connection.expected_counts = (4659, 4659, 4659, 5, 21)
    connection.release_contract_json = asdict(selected)
    connection.release_embeddings = 4680
    connection.facet_stats_row = (4659, 51, 313, 4295, 24)

    results = repository.search(
        EXPECTED_RELEASE,
        [0.1] * 768,
        question="香港動作喜劇有什麼代表特色？",
        embedding_model="gemini-embedding-2",
        embedding_dimension=768,
        target_movie_ids=("1985_JSXS_001",),
    )

    assert results == []
    assert any("FOR SHARE" in call.sql for call in connection.calls)
    assert any("FROM movie_facets" in call.sql for call in connection.calls)


@pytest.mark.parametrize(
    "method_name", ("search_recommendations", "search_deep_recommendations")
)
def test_recommendation_search_uses_persisted_contract_facet_expectations(
    repository: RagRepository,
    connection: RecordingConnection,
    method_name: str,
) -> None:
    """Breaks if either recommendation path silently reverts to the R2 facet census."""
    parent = _bundle().contract
    selected = replace(
        parent,
        schema_version="1.3",
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
    )
    connection.release_status = "active"
    connection.expected_counts = (4659, 4659, 4659, 5, 21)
    connection.release_contract_json = asdict(selected)
    connection.release_embeddings = 4680
    connection.facet_stats_row = (4659, 51, 313, 4295, 24)
    plan = RecommendationPlan(
        3, ("喜劇",), None, None, None, False, False, (), "推薦香港喜劇"
    )

    search = getattr(repository, method_name)
    result = search(EXPECTED_RELEASE, [0.1] * 768, plan)

    assert result.records == ()
    assert result.total_matches == 0
    assert any("FOR SHARE" in call.sql for call in connection.calls)
    assert any("FROM movie_facets" in call.sql for call in connection.calls)


def test_assert_query_ready_locks_before_one_aggregate_snapshot_in_one_transaction(
    repository: RagRepository,
    connection: RecordingConnection,
) -> None:
    """Breaks if READ COMMITTED can mix release, facet, and poster count snapshots."""
    _configure_query_ready(connection)

    repository.assert_query_ready(EXPECTED_RELEASE)

    assert len(connection.transactions) == 1
    statements = connection.transactions[0]
    assert len(statements) == 2
    assert "FOR SHARE" in statements[0].sql
    assert "readiness_movies" in statements[1].sql
    assert "readiness_facets" in statements[1].sql
    assert "readiness_primary_posters" in statements[1].sql
    assert "derived_byte_length <= 16777216" in statements[1].sql
    assert "payload ->> 'rights_status' = 'unknown'" in statements[1].sql


def test_assert_query_ready_checks_out_one_pool_connection_on_success_and_failure() -> None:
    """Breaks if one readiness call can consume multiple pool slots or leak a failed checkout."""
    pool = ExclusivePool(size=1)
    connection = pool.connections[0]
    _configure_query_ready(connection)
    repository = RagRepository.from_pool(pool)

    repository.assert_query_ready(EXPECTED_RELEASE)
    assert pool.checkout_count == 1
    assert pool.active == 0

    connection.actual_counts = (4658, 4658, 4658, 2, 6, 4663)
    with pytest.raises(RagDatabaseError, match="embeddings"):
        repository.assert_query_ready(EXPECTED_RELEASE)
    assert pool.checkout_count == 2
    assert pool.active == 0


def test_assert_query_ready_stops_after_inactive_lock_before_count_snapshot(
    repository: RagRepository,
    connection: RecordingConnection,
) -> None:
    """Breaks if an inactive release performs or trusts later count work."""
    _configure_query_ready(connection)
    connection.release_status = "loading"

    with pytest.raises(RagDatabaseError, match="active release is unavailable"):
        repository.assert_query_ready(EXPECTED_RELEASE)

    assert len(connection.transactions) == 1
    assert len(connection.transactions[0]) == 1
    assert "FOR SHARE" in connection.transactions[0][0].sql


def test_activate_release_uses_one_release_lock_transaction_for_counts_and_transition(
    repository: RagRepository,
) -> None:
    """Breaks if concurrent ingestion can race the checked counts before activation commits."""
    repository.connection.actual_counts = (4658, 4658, 4658, 2, 6, 4664)

    repository.activate_release(
        EXPECTED_RELEASE,
        embedding_model="gemini-embedding-2",
        embedding_dimension=768,
    )

    assert len(repository.connection.transactions) == 1
    statements = repository.connection.transactions[0]
    assert any("FROM rag_releases" in call.sql and "FOR UPDATE" in call.sql for call in statements)
    assert any("approved_poster_objects" in call.sql for call in statements)
    assert any("derived_byte_length <= 16777216" in call.sql for call in statements)
    assert any("UPDATE rag_releases" in call.sql for call in statements)


def test_activation_uses_selected_release_dynamic_document_counts(
    repository: RagRepository, connection: RecordingConnection
) -> None:
    """Breaks if activation substitutes the historical 2/6/4,664 document contract."""
    connection.expected_counts = (4658, 4658, 4658, 3, 12)
    connection.actual_counts = (4658, 4658, 4658, 3, 12, 4670)
    connection.release_contract_json = asdict(
        replace(
            _bundle().contract,
            counts=replace(_bundle().contract.counts, documents=3, pdf_passages=12),
        )
    )

    repository.activate_release(
        EXPECTED_RELEASE,
        embedding_model="gemini-embedding-2",
        embedding_dimension=768,
    )

    assert any("SET status = 'active'" in call.sql for call in connection.calls)


def test_activation_uses_selected_contract_facet_and_poster_expectations(
    repository: RagRepository,
    connection: RecordingConnection,
) -> None:
    """Breaks if activation accepts only module facet/poster constants."""
    selected = replace(
        _bundle().contract,
        counts=replace(
            _bundle().contract.counts,
            tier_s_count=51,
            tier_a_count=312,
            approved_poster_objects=4544,
            unavailable_poster_rows=114,
        ),
    )
    connection.release_contract_json = asdict(selected)
    connection.actual_counts = (4658, 4658, 4658, 2, 6, 4664)
    connection.facet_stats_row = (4658, 51, 312, 4295, 24)
    connection.poster_semantic_counts = (4658, 4544, 114)

    repository.activate_release(
        EXPECTED_RELEASE,
        embedding_model="gemini-embedding-2",
        embedding_dimension=768,
    )


def test_activate_release_requires_one_embedding_per_passage(repository: RagRepository) -> None:
    """Breaks if a release becomes active while any bundle passage lacks an embedding."""
    repository.connection.actual_counts = (4658, 4658, 4658, 2, 6, 4663)

    with pytest.raises(RagDatabaseError, match="embeddings: expected 4664, got 4663"):
        repository.activate_release(
            EXPECTED_RELEASE,
            embedding_model="gemini-embedding-2",
            embedding_dimension=768,
        )


def test_sql_failure_rolls_back_and_never_exposes_connection_details(
    repository: RagRepository,
) -> None:
    """Breaks if a database exception can leave a partial write or leak its connection string."""
    repository.connection.fail_next_execute = True

    with pytest.raises(RagDatabaseError) as raised:
        repository.begin_release(_bundle().contract)

    assert repository.connection.rolled_back
    assert "postgresql://rag_user:secret@localhost/rag" not in str(raised.value)


def test_ingestion_and_embedding_writes_reject_an_active_release(
    repository: RagRepository, connection: RecordingConnection
) -> None:
    """Breaks if an activated release can be mutated by a late bundle or embedding worker."""
    connection.release_status = "active"

    with pytest.raises(RagDatabaseError, match="release is not loading"):
        repository.upsert_bundle_records(
            EXPECTED_RELEASE,
            [{"record_kind": "movie", "movie_id": "1978_ZQ_001", "chinese_title": "醉拳"}],
        )
    with pytest.raises(RagDatabaseError, match="release is not loading"):
        repository.store_embedding(
            EXPECTED_RELEASE,
            "metadata:1978_ZQ_001",
            "d" * 64,
            [0.1] * 768,
            embedding_model="gemini-embedding-2",
            embedding_dimension=768,
        )

    assert not any("INSERT INTO movies" in call.sql or "UPDATE document_chunks" in call.sql for call in connection.calls)


def test_embedding_write_and_activation_lock_the_expected_model_contract(
    repository: RagRepository, connection: RecordingConnection
) -> None:
    """Breaks if a concurrent pre-first-vector fallback can relabel stale embeddings."""
    connection.release_embedding_model = "gemini-embedding-001"

    with pytest.raises(RagDatabaseError, match="embedding contract changed"):
        repository.store_embedding(
            EXPECTED_RELEASE,
            "metadata:1978_ZQ_001",
            "d" * 64,
            [0.1] * 768,
            embedding_model="gemini-embedding-2",
            embedding_dimension=768,
        )
    with pytest.raises(RagDatabaseError, match="embedding contract changed"):
        repository.activate_release(
            EXPECTED_RELEASE,
            embedding_model="gemini-embedding-2",
            embedding_dimension=768,
        )

    assert not any(
        "UPDATE document_chunks" in call.sql or "SET status = 'active'" in call.sql
        for call in connection.calls
    )
    contract_locks = [
        call
        for call in connection.calls
        if "FOR UPDATE" in call.sql and "embedding_model = %s" in call.sql
    ]
    assert len(contract_locks) == 2
    assert all(
        call.params == (EXPECTED_RELEASE, "gemini-embedding-2", 768)
        for call in contract_locks
    )


def test_passages_embeddings_search_and_poster_reads_share_release_boundary(
    repository: RagRepository,
) -> None:
    """Breaks if reads or embedding writes can escape the requested release."""
    repository.connection.pending_rows = [
        ("metadata:1978_ZQ_001", "1978_ZQ_001", "metadata", "醉拳", "text", "d" * 64)
    ]
    repository.connection.search_rows = [
        ("metadata:1978_ZQ_001", "1978_ZQ_001", "metadata", "text", 0.25)
    ]
    repository.connection.poster_row = (
        "poster:1978_ZQ_001:v1",
        "1978_ZQ_001",
        "assets/posters/derived/1978_ZQ_001.webp",
        "a" * 64,
        "d" * 64,
        38140,
        "image/webp",
        "machine_passed",
        "unknown",
    )

    assert repository.pending_passages(EXPECTED_RELEASE) == [
        {
            "passage_id": "metadata:1978_ZQ_001",
            "movie_id": "1978_ZQ_001",
            "passage_kind": "metadata",
            "title": "醉拳",
            "body": "text",
            "content_sha256": "d" * 64,
        }
    ]
    repository.store_embedding(
        EXPECTED_RELEASE,
        "metadata:1978_ZQ_001",
        "d" * 64,
        [0.1] * 768,
        embedding_model="gemini-embedding-2",
        embedding_dimension=768,
    )
    assert repository.search(EXPECTED_RELEASE, [0.1] * 768, limit=1) == [
        {
            "passage_id": "metadata:1978_ZQ_001",
            "movie_id": "1978_ZQ_001",
            "passage_kind": "metadata",
            "body": "text",
            "distance": 0.25,
        }
    ]
    assert repository.get_poster_asset(EXPECTED_RELEASE, "1978_ZQ_001") == {
        "asset_id": "poster:1978_ZQ_001:v1",
        "movie_id": "1978_ZQ_001",
        "derived_object_uri": "assets/posters/derived/1978_ZQ_001.webp",
        "content_sha256": "a" * 64,
        "derived_content_sha256": "d" * 64,
        "derived_byte_length": 38140,
        "derived_mime_type": "image/webp",
        "quality_status": "machine_passed",
        "rights_status": "unknown",
    }

    scoped = [call.params for call in repository.connection.calls if call.params]
    assert scoped and all(EXPECTED_RELEASE in params for params in scoped)
    embedding_write = next(
        call for call in repository.connection.calls if "UPDATE document_chunks" in call.sql
    )
    assert "embedding IS NULL" in embedding_write.sql


def test_release_state_and_ingestion_run_lifecycle_are_release_scoped(
    repository: RagRepository, connection: RecordingConnection
) -> None:
    """Breaks if resume/model state or a safe failure summary is not persisted."""
    connection.release_status = "loading"
    connection.release_embedding_model = "gemini-embedding-2"
    connection.release_embedding_dimension = 768
    connection.release_embeddings = 4650

    state = repository.release_state(EXPECTED_RELEASE)
    repository.start_ingestion_run(EXPECTED_RELEASE, "run-1")
    repository.finish_ingestion_run(
        EXPECTED_RELEASE,
        "run-1",
        status="failed",
        embedded_count=1,
        skipped_count=4650,
        error_summary="embedding request failed (status 400)",
    )

    assert state.status == "loading"
    assert state.embedding_model == "gemini-embedding-2"
    assert state.embedding_dimension == 768
    assert state.embeddings == 4650
    lifecycle = [call for call in connection.calls if "ingestion_runs" in call.sql]
    assert len(lifecycle) == 2
    assert all(EXPECTED_RELEASE in call.params for call in lifecycle)
    assert "embedding request failed (status 400)" in lifecycle[-1].params


def test_poster_lookup_returns_only_governed_approved_identity_fields(
    repository: RagRepository, connection: RecordingConnection
) -> None:
    """Breaks if the proxy cannot verify exact object hash, quality, and rights from the DB."""
    connection.poster_row = (
        "poster:1978_ZQ_001:v1",
        "1978_ZQ_001",
        "assets/posters/derived/1978_ZQ_001.webp",
        "a" * 64,
        "d" * 64,
        38140,
        "image/webp",
        "machine_passed",
        "unknown",
    )

    poster = repository.get_poster_asset(EXPECTED_RELEASE, "1978_ZQ_001")

    assert poster == {
        "asset_id": "poster:1978_ZQ_001:v1",
        "movie_id": "1978_ZQ_001",
        "derived_object_uri": "assets/posters/derived/1978_ZQ_001.webp",
        "content_sha256": "a" * 64,
        "derived_content_sha256": "d" * 64,
        "derived_byte_length": 38140,
        "derived_mime_type": "image/webp",
        "quality_status": "machine_passed",
        "rights_status": "unknown",
    }
    call = connection.calls[-1]
    assert call.params == (
        EXPECTED_RELEASE,
        ["1978_ZQ_001"],
        ["1978_ZQ_001"],
    )
    assert "quality_status" in call.sql
    assert "machine_passed" in call.sql
    assert "manual_approved" in call.sql
    assert "derived_object_uri =" in call.sql
    assert "derived_content_sha256 ~ '^[0-9a-f]{64}$'" in call.sql
    assert "derived_byte_length > 0" in call.sql
    assert "derived_mime_type = 'image/webp'" in call.sql
    assert "payload ->> 'rights_status' AS rights_status" in call.sql
    assert "rights_status" in call.sql and "('unknown', 'restricted')" in call.sql
    assert "COALESCE" not in call.sql


def test_approved_poster_batch_is_ordered_bounded_and_endpoint_shared(
    repository: RagRepository, connection: RecordingConnection
) -> None:
    first = (
        "poster:1978_ZQ_001:v1",
        "1978_ZQ_001",
        "assets/posters/derived/1978_ZQ_001.webp",
        "a" * 64,
        "d" * 64,
        38140,
        "image/webp",
        "machine_passed",
        "unknown",
    )
    second = (
        "poster:1982_ZJPD_001:v1",
        "1982_ZJPD_001",
        "assets/posters/derived/1982_ZJPD_001.webp",
        "b" * 64,
        "e" * 64,
        40000,
        "image/webp",
        "manual_approved",
        "unknown",
    )
    connection.poster_rows = [first, second]

    assets = repository.get_approved_poster_assets(
        EXPECTED_RELEASE, ("1978_ZQ_001", "1982_ZJPD_001")
    )

    assert tuple(assets) == ("1978_ZQ_001", "1982_ZJPD_001")
    assert assets["1978_ZQ_001"]["derived_content_sha256"] == "d" * 64
    call = connection.calls[-1]
    assert call.params == (
        EXPECTED_RELEASE,
        ["1978_ZQ_001", "1982_ZJPD_001"],
        ["1978_ZQ_001", "1982_ZJPD_001"],
    )
    assert "JOIN rag_releases" in call.sql
    assert "release.status = 'active'" in call.sql
    assert "derived_content_sha256 ~ '^[0-9a-f]{64}$'" in call.sql
    assert "asset.derived_byte_length <= 16777216" in call.sql
    assert "rights_status" in call.sql and "('unknown', 'restricted')" in call.sql

    connection.poster_rows = [first]
    assert repository.get_poster_asset(EXPECTED_RELEASE, "1978_ZQ_001") == assets[
        "1978_ZQ_001"
    ]


@pytest.mark.parametrize(
    "movie_ids",
    [
        (),
        ("1978_ZQ_001", "1978_ZQ_001"),
        ("../secret",),
        tuple(f"2000_MOVIE_{index:03d}" for index in range(9)),
    ],
)
def test_approved_poster_batch_rejects_invalid_or_unbounded_ids(
    repository: RagRepository, movie_ids: tuple[str, ...]
) -> None:
    with pytest.raises(RagDatabaseError, match="poster lookup"):
        repository.get_approved_poster_assets(EXPECTED_RELEASE, movie_ids)


@dataclass(frozen=True)
class _Bundle:
    rag_release_id: str = EXPECTED_RELEASE
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
    bundle_sha256: str = "b" * 64
    document_embedding_profile: str = "vertex-title-text-v1"

    @property
    def contract(self) -> RagReleaseContract:
        return RagReleaseContract(
            schema_version="1.2",
            rag_release_id=self.rag_release_id,
            parent_release_manifest_sha256="c" * 64,
            manifest_sha256=self.manifest_sha256,
            bundle_sha256=self.bundle_sha256,
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


def _bundle() -> _Bundle:
    return _Bundle()


@dataclass(frozen=True)
class SqlCall:
    sql: str
    params: tuple[object, ...]


@dataclass
class RecordingConnection:
    """Small psycopg-shaped boundary double; each query receives real SQL and parameters."""

    calls: list[SqlCall] = field(default_factory=list)
    expected_counts: tuple[int, int, int, int, int] = (4658, 4658, 4658, 2, 6)
    actual_counts: tuple[int, int, int, int, int, int] = (0, 0, 0, 0, 0, 0)
    poster_semantic_counts: tuple[int, int, int] = (4658, 4545, 113)
    pending_rows: list[tuple[object, ...]] = field(default_factory=list)
    search_rows: list[tuple[object, ...]] = field(default_factory=list)
    recommendation_rows: list[tuple[object, ...]] = field(default_factory=list)
    recommendation_rows_are_candidates: bool = False
    person_rows: list[tuple[object, ...]] = field(default_factory=list)
    facet_rows: list[tuple[object, ...]] = field(default_factory=list)
    facet_stats_row: tuple[int, int, int, int, int] = (4658, 50, 313, 4295, 24)
    poster_row: tuple[object, ...] | None = None
    poster_rows: list[tuple[object, ...]] = field(default_factory=list)
    rolled_back: bool = False
    fail_next_execute: bool = False
    release_status: str = "loading"
    release_id: str = EXPECTED_RELEASE
    release_embedding_model: str = "gemini-embedding-2"
    release_embedding_dimension: int = 768
    release_generation_model: str = "gemini-3.5-flash-lite"
    release_access_mode: str = "restricted_demo"
    release_manifest_sha256: str | None = "a" * 64
    release_embedding_profile: str | None = "vertex-title-text-v1"
    release_contract_json: Mapping[str, object] | None = field(
        default_factory=lambda: asdict(_bundle().contract)
    )
    release_embeddings: int = 0
    schema_ready: bool = True
    schema_exists: bool = True
    facets_exists: bool = True
    facets_ready: bool = True
    poster_ceiling_ready: bool = True
    release_identity_exists: bool = True
    release_identity_ready: bool = True
    release_identity_constraint_definition: str = EXPECTED_IDENTITY_CHECK_DEFINITION
    release_identity_validator_source: str = EXPECTED_IDENTITY_VALIDATOR_SOURCE
    release_identity_trigger_source: str = EXPECTED_IDENTITY_TRIGGER_SOURCE
    release_identity_trigger_has_predicate: bool = False
    release_identity_validator_safe_config: bool = True
    release_identity_trigger_safe_config: bool = True
    release_identity_constraint_uses_public_validator: bool = True
    reject_missing_facet_regclass_cast: bool = False
    active_bundle_rows: list[tuple[object, ...]] = field(default_factory=list)
    reuse_contract_row: tuple[object, ...] = (
        "active",
        "loading",
        "gemini-embedding-2",
        "gemini-embedding-2",
        768,
        768,
        "vertex-title-text-v1",
        "vertex-title-text-v1",
    )
    reuse_result_row: tuple[int, int, int] = (4658, 4658, 0)
    reuse_stats_row: tuple[int, int] = (0, 0)
    transactions: list[list[SqlCall]] = field(default_factory=list)
    _active_transaction: list[SqlCall] | None = None

    @contextmanager
    def transaction(self) -> Iterator[RecordingConnection]:
        transaction: list[SqlCall] = []
        self.transactions.append(transaction)
        self._active_transaction = transaction
        try:
            yield self
        except Exception:
            self.rollback()
            raise
        finally:
            self._active_transaction = None

    @contextmanager
    def cursor(self) -> Iterator[RecordingCursor]:
        yield RecordingCursor(self)

    def rollback(self) -> None:
        self.rolled_back = True


class RecordingCursor:
    def __init__(self, connection: RecordingConnection) -> None:
        self.connection = connection
        self.sql = ""
        self.params: tuple[object, ...] = ()

    def execute(self, sql: str, params: tuple[object, ...] | None = None) -> None:
        if self.connection.fail_next_execute:
            self.connection.fail_next_execute = False
            raise RuntimeError("postgresql://rag_user:secret@localhost/rag")
        self.sql = sql
        self.params = params or ()
        if (
            self.connection.reject_missing_facet_regclass_cast
            and not self.connection.facets_exists
            and "'public.movie_facets'::regclass" in sql
        ):
            raise RuntimeError("relation public.movie_facets does not exist")
        call = SqlCall(sql, params or ())
        self.connection.calls.append(call)
        if self.connection._active_transaction is not None:
            self.connection._active_transaction.append(call)
        if "CREATE TABLE movie_facets" in sql:
            self.connection.facets_exists = True
            self.connection.facets_ready = True
        if "CREATE TABLE document_chunks" in sql:
            self.connection.schema_exists = True
            self.connection.schema_ready = True
        if "ADD CONSTRAINT media_assets_poster_byte_ceiling" in sql:
            self.connection.poster_ceiling_ready = True
        if "ADD COLUMN IF NOT EXISTS manifest_sha256" in sql:
            self.connection.release_identity_exists = True
            self.connection.release_identity_ready = True
        if "SET manifest_sha256" in sql or "SET contract_json = %s::jsonb" in sql:
            self.rowcount = 1

    def fetchone(self) -> tuple[object, ...] | None:
        if "to_regclass" in self.sql:
            exact_identity_catalog = (
                "pg_get_constraintdef(identity_constraint.oid, true) = %s" in self.sql
                and "btrim(regexp_replace(identity_validator.prosrc" in self.sql
                and "btrim(regexp_replace(trigger_function.prosrc" in self.sql
                and "database_trigger.tgqual IS NULL" in self.sql
                and "database_trigger.tgnargs = 0" in self.sql
                and "database_trigger.tgattr = ''::int2vector" in self.sql
                and not self.connection.release_identity_trigger_has_predicate
                and "identity_validator.proconfig =" in self.sql
                and "trigger_function.proconfig =" in self.sql
                and self.connection.release_identity_validator_safe_config
                and self.connection.release_identity_trigger_safe_config
                and "FROM pg_depend AS validator_dependency" in self.sql
                and self.connection.release_identity_constraint_uses_public_validator
                and self.params
                == (
                    self.connection.release_identity_constraint_definition,
                    self.connection.release_identity_validator_source,
                    self.connection.release_identity_trigger_source,
                )
            )
            return (
                self.connection.schema_exists,
                self.connection.schema_ready,
                self.connection.facets_exists,
                self.connection.facets_ready,
                self.connection.poster_ceiling_ready,
                self.connection.release_identity_exists,
                self.connection.release_identity_ready and exact_identity_catalog,
            )
        if "source_release.status AS source_status" in self.sql:
            return self.connection.reuse_contract_row
        if "source_eligible_total" in self.sql:
            if "copied_this_run" in self.sql:
                return self.connection.reuse_result_row
            return self.connection.reuse_stats_row
        if "readiness_movies" in self.sql:
            return (
                *self.connection.actual_counts,
                *self.connection.facet_stats_row,
                *self.connection.poster_semantic_counts,
            )
        if "FROM movie_facets" in self.sql and "COUNT(*) AS total" in self.sql:
            return self.connection.facet_stats_row
        if "status = 'active'" in self.sql and "FOR SHARE" in self.sql:
            return (
                (
                    self.connection.release_status,
                    *self.connection.expected_counts,
                    self.connection.release_embedding_model,
                    self.connection.release_embedding_dimension,
                    self.connection.release_generation_model,
                    self.connection.release_access_mode,
                    self.connection.release_manifest_sha256,
                    self.connection.release_embedding_profile,
                    self.connection.release_contract_json,
                    self.connection.release_embeddings,
                )
                if self.connection.release_status == "active"
                else None
            )
        if "embedding_model" in self.sql and "COUNT(*)" in self.sql:
            if "expected_movies" in self.sql:
                if (
                    "release.status = 'loading'" in self.sql
                    and self.connection.release_status != "loading"
                ):
                    return None
                if len(self.params) >= 3 and (
                    self.connection.release_embedding_model != self.params[1]
                    or self.connection.release_embedding_dimension != self.params[2]
                ):
                    return None
                return (
                    self.connection.release_status,
                    *self.connection.expected_counts,
                    self.connection.release_embedding_model,
                    self.connection.release_embedding_dimension,
                    self.connection.release_generation_model,
                    self.connection.release_access_mode,
                    self.connection.release_manifest_sha256,
                    self.connection.release_embedding_profile,
                    self.connection.release_contract_json,
                    self.connection.release_embeddings,
                )
            return (
                self.connection.release_status,
                self.connection.release_embedding_model,
                self.connection.release_embedding_dimension,
                self.connection.release_manifest_sha256,
                self.connection.release_embedding_profile,
                self.connection.release_embeddings,
            )
        if "FROM rag_releases" in self.sql and "FOR UPDATE" in self.sql:
            if "status IN ('loading', 'active')" in self.sql:
                return (
                    (
                        self.connection.release_status,
                        self.connection.release_contract_json,
                    )
                    if self.connection.release_status in {"loading", "active"}
                    else None
                )
            if "embedding_model = %s" in self.sql:
                expected_model = self.params[1]
                expected_dimension = self.params[2]
                if (
                    self.connection.release_status != "loading"
                    or self.connection.release_embedding_model != expected_model
                    or self.connection.release_embedding_dimension != expected_dimension
                ):
                    return None
            return ("release",) if self.connection.release_status == "loading" else None
        if "expected_movies" in self.sql:
            return self.connection.expected_counts
        if "metadata_passages" in self.sql:
            return self.connection.actual_counts
        if "approved_poster_objects" in self.sql:
            return self.connection.poster_semantic_counts
        return self.connection.poster_row

    def fetchall(self) -> list[tuple[object, ...]]:
        if "ORDER BY release_id" in self.sql and "document_embedding_profile" in self.sql:
            (
                source_status,
                target_status,
                source_model,
                target_model,
                source_dimension,
                target_dimension,
                source_profile,
                target_profile,
            ) = self.connection.reuse_contract_row
            target_release_id, source_release_id = self.params
            rows = [
                (
                    source_release_id,
                    source_status,
                    source_model,
                    source_dimension,
                    source_profile,
                ),
                (
                    target_release_id,
                    target_status,
                    target_model,
                    target_dimension,
                    target_profile,
                ),
            ]
            return sorted(rows, key=lambda row: str(row[0]))
        if "ORDER BY array_position" in self.sql and "FROM media_assets AS asset" in self.sql:
            if self.connection.poster_rows:
                return self.connection.poster_rows
            return [self.connection.poster_row] if self.connection.poster_row is not None else []
        if "credit_tokens AS" in self.sql:
            return self.connection.person_rows
        if "WITH persisted_records AS" in self.sql:
            return self.connection.active_bundle_rows
        if "FROM movie_facets" in self.sql and "content_sha256" in self.sql:
            return self.connection.facet_rows
        if "FROM ranked" in self.sql:
            if self.connection.recommendation_rows_are_candidates and "movie_position = 1" in self.sql:
                unique: dict[object, tuple[object, ...]] = {}
                for row in sorted(
                    self.connection.recommendation_rows, key=lambda item: (item[15], item[0])
                ):
                    unique.setdefault(row[1], row)
                return [row[:-1] + (len(unique),) for row in unique.values()]
            return self.connection.recommendation_rows
        if "embedding IS NULL" in self.sql:
            return self.connection.pending_rows
        if "ORDER BY chunk.embedding" in self.sql:
            return self.connection.search_rows
        return []


class ExclusiveConnection(RecordingConnection):
    """Connection double that exposes unsafe concurrent cursor ownership."""

    def __init__(self) -> None:
        super().__init__(release_status="active", release_embeddings=4664)
        self._cursor_lock = threading.Lock()
        self.overlap_rejections = 0

    @contextmanager
    def cursor(self) -> Iterator[RecordingCursor]:
        if not self._cursor_lock.acquire(blocking=False):
            self.overlap_rejections += 1
            raise RuntimeError("overlapping connection work")
        try:
            time.sleep(0.01)
            yield RecordingCursor(self)
        finally:
            self._cursor_lock.release()


class ExclusivePool:
    """Bounded pool double whose context owns checkout and return."""

    def __init__(self, *, size: int) -> None:
        self.connections = [ExclusiveConnection() for _ in range(size)]
        self.available: queue.Queue[ExclusiveConnection] = queue.Queue()
        for connection in self.connections:
            self.available.put(connection)
        self.checkout_count = 0
        self.active = 0
        self.max_active = 0
        self._state_lock = threading.Lock()

    @contextmanager
    def connection(self, *, timeout: float) -> Iterator[ExclusiveConnection]:
        connection = self.available.get(timeout=timeout)
        with self._state_lock:
            self.checkout_count += 1
            self.active += 1
            self.max_active = max(self.max_active, self.active)
        try:
            yield connection
        finally:
            with self._state_lock:
                self.active -= 1
            self.available.put(connection)
