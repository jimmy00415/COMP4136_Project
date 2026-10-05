"""PostgreSQL/pgvector boundary for the minimum verifiable RAG release."""

from __future__ import annotations

import hashlib
import json
import math
import os
import re
import threading
from collections.abc import Iterable, Iterator, Mapping, Sequence
from contextlib import contextmanager, suppress
from dataclasses import asdict, dataclass, field, fields
from importlib.resources import files
from typing import Any, cast

import psycopg
from pgvector import Vector
from psycopg_pool import ConnectionPool

from .poster_policy import MAX_POSTER_BYTES as _MAX_POSTER_BYTES
from .rag_bundle import RagReleaseContract, RagReleaseCounts
from .retrieval import (
    AmbiguousPersonResolutionError,
    ExplicitMovieResolution,
    PersonRole,
    RecommendationPlan,
    RecommendationSearch,
    ResolvedPerson,
    controlled_credit_group,
    controlled_title_policy,
    credit_equivalence_key,
    credit_query_alias,
    infer_person_role,
    normalize_query_text,
    parse_person_query_shape,
    resolve_explicit_movie_identity_context,
    sql_title_normalization,
)

_COUNT_LABELS = ("movies", "assets", "metadata passages", "documents", "pdf passages")
_REQUIRED_POSTER_SEMANTICS = (4658, 4545, 113)
_POSTER_SEMANTIC_LABELS = (
    "primary poster rows",
    "approved poster objects",
    "unavailable poster rows",
)
_SHA256 = re.compile(r"[0-9a-f]{64}")
_MOVIE_ID = re.compile(r"[0-9]{4}_[A-Z0-9]+_[0-9]{3}")
_MAX_TARGET_MOVIE_IDS = 8
_MAX_EXPLICIT_TITLE_CANDIDATES = 64
DB_POOL_CHECKOUT_TIMEOUT_SECONDS = 10.0
DB_POOL_STARTUP_TIMEOUT_SECONDS = 10.0
_APPROVED_POSTER_STATES = frozenset({"machine_passed", "manual_approved"})
_UNAVAILABLE_POSTER_STATES = frozenset({"content_conflict", "missing", "placeholder"})
_REQUIRED_FACET_COUNTS = (4658, 50, 313, 4295, 24)
_FACET_COUNT_LABELS = ("facets", "tier S", "tier A", "tier B", "pilot facets")
_RELEASE_IDENTITY_CHECK_DEFINITION = (
    "CHECK (rag_release_manifest_identity_v4_is_valid(release_id, expected_movies, "
    "expected_assets, expected_metadata_passages, expected_documents, "
    "expected_pdf_passages, embedding_model, embedding_dimension, generation_model, "
    "access_mode, manifest_sha256, document_embedding_profile, contract_json) IS TRUE)"
)


def _packaged_release_identity_function_source(
    function_name: str,
    migration_name: str = "0005_contract_driven_release_identity.sql",
) -> str:
    """Return the exact normalized body shipped in the immutable migration artifact."""
    migration = files("hk_movie_rag.migrations").joinpath(migration_name).read_text(
        encoding="utf-8"
    )
    signature = f"CREATE OR REPLACE FUNCTION public.{function_name}("
    function_start = migration.find(signature)
    if function_start < 0:
        raise RuntimeError("release identity migration function is unavailable")
    body_marker = "AS $function$"
    body_start = migration.find(body_marker, function_start)
    if body_start < 0:
        raise RuntimeError("release identity migration function body is unavailable")
    body_start += len(body_marker)
    body_end = migration.find("$function$;", body_start)
    if body_end < 0:
        raise RuntimeError("release identity migration function terminator is unavailable")
    return re.sub(r"\s+", " ", migration[body_start:body_end]).strip()


_RELEASE_IDENTITY_VALIDATOR_SOURCE = _packaged_release_identity_function_source(
    "rag_release_manifest_identity_v4_is_valid"
)
_RELEASE_IDENTITY_TRIGGER_SOURCE = _packaged_release_identity_function_source(
    "enforce_release_manifest_identity"
)
_CREDIT_TOKEN_PREDICATE = """
                      AND (
                          cardinality(%s::text[]) = 0 OR EXISTS (
                              SELECT 1
                              FROM (
                                  SELECT 'director'::text AS role, credit.name
                                  FROM unnest(regexp_split_to_array(
                                      COALESCE(movie.payload ->> 'director', ''),
                                      '[、,，/;；]'
                                  )) AS credit(name)
                                  UNION ALL
                                  SELECT 'actor'::text AS role, credit.name
                                  FROM unnest(regexp_split_to_array(
                                      COALESCE(movie.payload ->> 'cast', ''),
                                      '[、,，/;；]'
                                  )) AS credit(name)
                              ) AS credit
                              WHERE btrim(credit.name) = ANY(%s::text[])
                                AND (%s::text IS NULL OR credit.role = %s::text)
                          )
                      )
"""
_GENRE_ANY_PREDICATE = """
                      AND (
                          cardinality(%s::text[]) = 0 OR EXISTS (
                              SELECT 1
                              FROM unnest(%s::text[]) AS requested(genre)
                              WHERE EXISTS (
                                  SELECT 1
                                  FROM unnest(regexp_split_to_array(
                                      COALESCE(movie.payload ->> 'genre', ''),
                                      '[、,，/;；]'
                                  )) AS stored(genre)
                                  WHERE lower(btrim(stored.genre)) =
                                        lower(btrim(requested.genre))
                              )
                          )
                      )
"""
_GENRE_ALL_PREDICATE = """
                      AND (
                          cardinality(%s::text[]) = 0 OR NOT EXISTS (
                              SELECT 1
                              FROM unnest(%s::text[]) AS requested(genre)
                              WHERE NOT EXISTS (
                                  SELECT 1
                                  FROM unnest(regexp_split_to_array(
                                      COALESCE(movie.payload ->> 'genre', ''),
                                      '[、,，/;；]'
                                  )) AS stored(genre)
                                  WHERE lower(btrim(stored.genre)) =
                                        lower(btrim(requested.genre))
                              )
                          )
                      )
"""
_TITLE_TERMS_ANY_PREDICATE = """
                      AND (
                          cardinality(%s::text[]) = 0 OR EXISTS (
                              SELECT 1
                              FROM unnest(%s::text[]) AS requested(term)
                              WHERE strpos(lower(COALESCE(
                                  movie.payload ->> 'chinese_title', '')),
                                  lower(btrim(requested.term))
                              ) > 0
                                 OR strpos(lower(COALESCE(
                                  movie.payload ->> 'english_title', '')),
                                  lower(btrim(requested.term))
                              ) > 0
                          )
                      )
"""
_APPROVED_POSTER_ASSET_PREDICATE = """
                                 AND asset.asset_type = 'poster'
                                 AND asset.is_primary
                                 AND asset.derived_object_uri =
                                     'assets/posters/derived/' || asset.movie_id || '.webp'
                                 AND asset.derived_content_sha256 ~ '^[0-9a-f]{64}$'
                                 AND asset.derived_byte_length > 0
                                 AND asset.derived_byte_length <= __MAX_POSTER_BYTES__
                                 AND asset.derived_mime_type = 'image/webp'
                                 AND asset.payload ->> 'quality_status'
                                     IN ('machine_passed', 'manual_approved')
                                 AND asset.payload ->> 'rights_status'
                                     IN ('unknown', 'restricted')
""".replace("__MAX_POSTER_BYTES__", str(_MAX_POSTER_BYTES))
_POSTER_AVAILABLE_PREDICATE = f"""
                           EXISTS (
                               SELECT 1 FROM media_assets AS asset
                               WHERE asset.release_id = chunk.release_id
                                 AND asset.movie_id = chunk.movie_id
                                 {_APPROVED_POSTER_ASSET_PREDICATE}
                           )
"""
_RELEASE_YEAR_LATERAL_SQL = r"""
                    CROSS JOIN LATERAL (
                        SELECT CASE
                            WHEN movie.payload ->> 'release_date' ~ '^\d{4}-\d{2}-\d{2}$'
                            THEN left(movie.payload ->> 'release_date', 4)::integer
                        END AS release_year
                    ) AS release_date
"""


def _broad_title_candidate_lateral_sql() -> tuple[str, tuple[object, ...]]:
    """Build a bounded broad-candidate expression; Python owns title intent."""
    normalized_title_sql, normalized_title_params = sql_title_normalization(
        "COALESCE(movie.payload ->> 'chinese_title', '')"
    )
    return (
        f"""
        CROSS JOIN LATERAL (
            SELECT {normalized_title_sql} AS chinese_title
        ) AS normalized_title
        CROSS JOIN LATERAL (
            SELECT MIN(term.position) AS broad_position
            FROM (VALUES
                (NULLIF(strpos(lower(query_input.question),
                               lower(movie.movie_id)), 0)),
                (NULLIF(strpos(
                    query_input.normalized_question,
                    NULLIF(normalized_title.chinese_title, '')
                ), 0)),
                (NULLIF(strpos(
                    lower(query_input.question),
                    NULLIF(lower(movie.payload ->> 'english_title'), '')
                ), 0))
            ) AS term(position)
        ) AS broad_match
        """,
        normalized_title_params,
    )


class RagDatabaseError(RuntimeError):
    """Raised when the repository cannot safely complete a database operation."""


@dataclass(frozen=True)
class DatabaseSettings:
    """Connection parameters kept separate from printable DSNs and logs."""

    dbname: str
    user: str
    password: str = field(repr=False)
    host: str | None = None
    port: int | None = None
    instance_connection_name: str | None = None

    @classmethod
    def from_env(cls, environ: Mapping[str, str] | None = None) -> DatabaseSettings:
        values = os.environ if environ is None else environ
        missing = [key for key in ("DB_NAME", "DB_USER", "DB_PASSWORD") if not values.get(key)]
        if missing:
            raise RagDatabaseError(f"missing required database settings: {', '.join(missing)}")
        instance = values.get("INSTANCE_CONNECTION_NAME")
        if instance:
            return cls(
                dbname=values["DB_NAME"],
                user=values["DB_USER"],
                password=values["DB_PASSWORD"],
                instance_connection_name=instance,
            )
        host = values.get("DB_HOST")
        raw_port = values.get("DB_PORT")
        if not host or not raw_port:
            raise RagDatabaseError("local database requires explicit DB_HOST and DB_PORT")
        try:
            port = int(raw_port)
        except ValueError as exc:
            raise RagDatabaseError("DB_PORT must be an integer") from exc
        if not 1 <= port <= 65535:
            raise RagDatabaseError("DB_PORT must be between 1 and 65535")
        return cls(values["DB_NAME"], values["DB_USER"], values["DB_PASSWORD"], host, port)

    def connection_kwargs(self) -> dict[str, Any]:
        kwargs: dict[str, Any] = {
            "dbname": self.dbname,
            "user": self.user,
            "password": self.password,
        }
        if self.instance_connection_name:
            kwargs["host"] = f"/cloudsql/{self.instance_connection_name}"
        elif self.host is not None and self.port is not None:
            kwargs.update(host=self.host, port=self.port)
        else:
            raise RagDatabaseError("database endpoint is incomplete")
        return kwargs


def connect_db(settings: DatabaseSettings) -> psycopg.Connection[Any]:
    """Connect without serialising connection details into an error message."""
    try:
        return psycopg.connect(**settings.connection_kwargs())
    except psycopg.Error as exc:
        raise RagDatabaseError("database connection failed") from exc


def create_db_pool(settings: DatabaseSettings) -> ConnectionPool[Any]:
    """Open the bounded runtime pool without exposing connection details."""
    pool: ConnectionPool[Any] | None = None
    try:
        pool = ConnectionPool(
            kwargs=settings.connection_kwargs(),
            min_size=1,
            max_size=5,
            open=False,
            timeout=DB_POOL_CHECKOUT_TIMEOUT_SECONDS,
            max_waiting=20,
            check=ConnectionPool.check_connection,
        )
        pool.open(wait=True, timeout=DB_POOL_STARTUP_TIMEOUT_SECONDS)
        return pool
    except Exception:  # noqa: BLE001 - startup redaction boundary must close on any failure
        if pool is not None:
            with suppress(Exception):
                pool.close()
        raise RagDatabaseError("database pool startup failed") from None


@dataclass(frozen=True)
class ReleaseStats:
    movies: int
    assets: int
    metadata_passages: int
    documents: int
    pdf_passages: int
    embeddings: int


@dataclass(frozen=True)
class ReleaseState:
    status: str
    embedding_model: str
    embedding_dimension: int
    embeddings: int
    manifest_sha256: str | None = None
    document_embedding_profile: str | None = None


@dataclass(frozen=True)
class EmbeddingReuseResult:
    source_eligible_total: int
    copied_this_run: int


@dataclass(frozen=True)
class EmbeddingReuseStats:
    source_eligible_total: int
    non_reusable_embedded_total: int


@dataclass(frozen=True)
class _StoredReleaseContract:
    release_id: str
    status: str
    expected_counts: tuple[int, int, int, int, int]
    embedding_model: str
    embedding_dimension: int
    generation_model: str
    access_mode: str
    manifest_sha256: str | None
    document_embedding_profile: str | None
    persisted_contract: RagReleaseContract | None
    embeddings: int = 0

    @property
    def expected_embeddings(self) -> int:
        return self.expected_counts[2] + self.expected_counts[4]


@dataclass(frozen=True)
class FacetStats:
    total: int
    tier_s: int
    tier_a: int
    tier_b: int
    pilots: int


@dataclass(frozen=True)
class _PersonIdentityMatch:
    start: int
    end: int
    canonical_name: str
    simplified_alias: str
    equivalence_key: str
    exact: bool


class RagRepository:
    """Release-scoped SQL boundary; callers cannot read or write across releases."""

    def __init__(self, connection: Any, *, _pool: Any | None = None) -> None:
        if connection is None and _pool is None:
            raise TypeError("a direct connection or pool is required")
        self.connection = connection
        self._pool = _pool
        self._person_lexicons: dict[
            str, tuple[tuple[str, PersonRole, str, str], ...]
        ] = {}
        self._person_lexicons_lock = threading.Lock()

    @classmethod
    def from_pool(cls, pool: Any) -> RagRepository:
        """Build the request-safe repository path backed by per-operation checkout."""
        return cls(None, _pool=pool)

    def migrate(self) -> None:
        migration = files("hk_movie_rag.migrations").joinpath("0001_rag_demo.sql")
        with self._cursor() as cursor:
            cursor.execute(migration.read_text(encoding="utf-8"))

    def _migrate_facets(self) -> None:
        migration = files("hk_movie_rag.migrations").joinpath("0002_movie_facets.sql")
        with self._cursor() as cursor:
            cursor.execute(migration.read_text(encoding="utf-8"))

    def _migrate_poster_ceiling(self) -> None:
        migration = files("hk_movie_rag.migrations").joinpath(
            "0003_poster_byte_ceiling.sql"
        )
        with self._cursor() as cursor:
            cursor.execute(migration.read_text(encoding="utf-8"))

    def _migrate_release_identity(self) -> None:
        for migration_name in (
            "0004_release_manifest_identity.sql",
            "0005_contract_driven_release_identity.sql",
        ):
            migration = files("hk_movie_rag.migrations").joinpath(migration_name)
            with self._cursor() as cursor:
                cursor.execute(migration.read_text(encoding="utf-8"))

    def _migrate_contract_driven_release_identity(self) -> None:
        migration = files("hk_movie_rag.migrations").joinpath(
            "0005_contract_driven_release_identity.sql"
        )
        with self._cursor() as cursor:
            cursor.execute(migration.read_text(encoding="utf-8"))

    def _schema_status(self) -> tuple[bool, bool, bool, bool, bool, bool, bool]:
        with self._cursor() as cursor:
            cursor.execute(
                """
                SELECT
                    to_regclass('public.rag_releases') IS NOT NULL AS schema_exists,
                    to_regclass('public.rag_releases') IS NOT NULL
                    AND (
                        SELECT COUNT(*) = 3
                        FROM information_schema.columns
                        WHERE table_schema = 'public' AND table_name = 'media_assets'
                          AND column_name IN (
                              'derived_content_sha256',
                              'derived_byte_length',
                              'derived_mime_type'
                          )
                    )
                    AND EXISTS (
                        SELECT 1
                        FROM pg_trigger AS database_trigger
                        JOIN pg_proc AS trigger_function
                          ON trigger_function.oid = database_trigger.tgfoid
                        WHERE database_trigger.tgname = 'media_assets_immutable'
                          AND NOT database_trigger.tgisinternal
                          AND trigger_function.proname = 'reject_media_asset_change'
                    ) AS schema_ready,
                    to_regclass('public.movie_facets') IS NOT NULL AS facets_exists,
                    to_regclass('public.movie_facets') IS NOT NULL
                    AND (
                        SELECT COUNT(*) = 8
                        FROM information_schema.columns
                        WHERE table_schema = 'public' AND table_name = 'movie_facets'
                          AND column_name IN (
                              'release_id', 'movie_id', 'content_sha256', 'tier',
                              'tier_reason', 'human_review', 'pilot_movie', 'pilot_evidence'
                          )
                    )
                    AND (
                        SELECT COUNT(*) = 1
                        FROM pg_constraint
                        WHERE conrelid = to_regclass('public.movie_facets') AND contype = 'p'
                    )
                    AND (
                        SELECT COUNT(*) = 1
                        FROM pg_constraint
                        WHERE conrelid = to_regclass('public.movie_facets') AND contype = 'f'
                    )
                    AND (
                        SELECT COUNT(*) = 5
                        FROM pg_constraint
                        WHERE conrelid = to_regclass('public.movie_facets') AND contype = 'c'
                    )
                    AND (
                        SELECT COUNT(*) = 2
                        FROM pg_indexes
                        WHERE schemaname = 'public' AND tablename = 'movie_facets'
                          AND indexname IN (
                              'movie_facets_release_tier', 'movie_facets_release_pilot'
                          )
                    )
                    AND EXISTS (
                        SELECT 1
                        FROM pg_trigger AS database_trigger
                        JOIN pg_proc AS trigger_function
                          ON trigger_function.oid = database_trigger.tgfoid
                        WHERE database_trigger.tgname = 'movie_facets_immutable'
                          AND NOT database_trigger.tgisinternal
                          AND database_trigger.tgrelid = to_regclass('public.movie_facets')
                          AND (database_trigger.tgtype & 1) = 1
                          AND (database_trigger.tgtype & 2) = 2
                          AND (database_trigger.tgtype & 8) = 8
                          AND (database_trigger.tgtype & 16) = 16
                          AND database_trigger.tgenabled = 'O'
                          AND trigger_function.proname = 'reject_movie_facet_change'
                    ) AS facets_ready,
                    EXISTS (
                        SELECT 1
                        FROM pg_constraint AS poster_constraint
                        WHERE poster_constraint.conrelid =
                              to_regclass('public.media_assets')
                          AND poster_constraint.conname =
                              'media_assets_poster_byte_ceiling'
                          AND poster_constraint.contype = 'c'
                          AND poster_constraint.convalidated
                          AND pg_get_constraintdef(poster_constraint.oid) LIKE
                              '%%derived_byte_length <= 16777216%%'
                    ) AS poster_ceiling_ready,
                    (
                        SELECT COUNT(*) = 3
                               AND bool_and(
                                   (column_name IN (
                                       'manifest_sha256',
                                       'document_embedding_profile'
                                   ) AND data_type = 'text')
                                   OR (column_name = 'contract_json' AND data_type = 'jsonb')
                               )
                        FROM information_schema.columns
                        WHERE table_schema = 'public' AND table_name = 'rag_releases'
                          AND column_name IN (
                              'manifest_sha256', 'document_embedding_profile',
                              'contract_json'
                          )
                    ) AS release_identity_exists,
                    (
                        SELECT COUNT(*) = 3
                               AND bool_and(
                                   (column_name IN (
                                       'manifest_sha256',
                                       'document_embedding_profile'
                                   ) AND data_type = 'text')
                                   OR (column_name = 'contract_json' AND data_type = 'jsonb')
                               )
                        FROM information_schema.columns
                        WHERE table_schema = 'public' AND table_name = 'rag_releases'
                          AND column_name IN (
                              'manifest_sha256', 'document_embedding_profile',
                              'contract_json'
                          )
                    )
                    AND EXISTS (
                        SELECT 1
                        FROM pg_constraint AS identity_constraint
                        WHERE identity_constraint.conrelid =
                              to_regclass('public.rag_releases')
                          AND identity_constraint.conname =
                              'rag_releases_manifest_identity_format'
                          AND identity_constraint.contype = 'c'
                          AND identity_constraint.convalidated
                          AND pg_get_constraintdef(identity_constraint.oid, true) = %s
                          AND EXISTS (
                              SELECT 1
                              FROM pg_depend AS validator_dependency
                              WHERE validator_dependency.classid =
                                    'pg_constraint'::regclass
                                AND validator_dependency.objid =
                                    identity_constraint.oid
                                AND validator_dependency.refclassid =
                                    'pg_proc'::regclass
                                AND validator_dependency.refobjid = to_regprocedure(
                                    'public.rag_release_manifest_identity_v4_is_valid(text,integer,integer,integer,integer,integer,text,integer,text,text,text,text,jsonb)'
                                )
                                AND validator_dependency.deptype = 'n'
                          )
                    )
                    AND EXISTS (
                        SELECT 1
                        FROM pg_proc AS identity_validator
                        JOIN pg_language AS validator_language
                          ON validator_language.oid = identity_validator.prolang
                        WHERE identity_validator.proname =
                              'rag_release_manifest_identity_v4_is_valid'
                          AND identity_validator.pronamespace =
                              to_regnamespace('public')
                          AND identity_validator.pronargs = 13
                          AND identity_validator.pronargdefaults = 0
                          AND oidvectortypes(identity_validator.proargtypes) =
                              'text, integer, integer, integer, integer, integer, '
                              'text, integer, text, text, text, text, jsonb'
                          AND identity_validator.prorettype = 'boolean'::regtype
                          AND NOT identity_validator.proretset
                          AND NOT identity_validator.proisstrict
                          AND identity_validator.provolatile = 'i'
                          AND identity_validator.prokind = 'f'
                          AND NOT identity_validator.prosecdef
                          AND NOT identity_validator.proleakproof
                          AND identity_validator.proparallel = 'u'
                          AND identity_validator.prosupport = 0
                          AND identity_validator.proconfig =
                              ARRAY['search_path=pg_catalog']::text[]
                          AND validator_language.lanname = 'sql'
                          AND btrim(regexp_replace(identity_validator.prosrc,
                              '[[:space:]]+', ' ', 'g'
                          )) = %s
                    )
                    AND EXISTS (
                        SELECT 1
                        FROM pg_trigger AS database_trigger
                        JOIN pg_proc AS trigger_function
                          ON trigger_function.oid = database_trigger.tgfoid
                        JOIN pg_language AS trigger_language
                          ON trigger_language.oid = trigger_function.prolang
                        WHERE database_trigger.tgname =
                              'rag_releases_manifest_identity_immutable'
                          AND NOT database_trigger.tgisinternal
                          AND database_trigger.tgrelid = to_regclass('public.rag_releases')
                          AND database_trigger.tgtype = 23
                          AND database_trigger.tgenabled = 'O'
                          AND database_trigger.tgqual IS NULL
                          AND database_trigger.tgnargs = 0
                          AND database_trigger.tgattr = ''::int2vector
                          AND database_trigger.tgparentid = 0
                          AND database_trigger.tgconstraint = 0
                          AND database_trigger.tgconstrrelid = 0
                          AND database_trigger.tgconstrindid = 0
                          AND NOT database_trigger.tgdeferrable
                          AND NOT database_trigger.tginitdeferred
                          AND database_trigger.tgoldtable IS NULL
                          AND database_trigger.tgnewtable IS NULL
                          AND trigger_function.proname =
                              'enforce_release_manifest_identity'
                          AND trigger_function.pronamespace = to_regnamespace('public')
                          AND trigger_function.pronargs = 0
                          AND trigger_function.pronargdefaults = 0
                          AND trigger_function.prorettype = 'trigger'::regtype
                          AND NOT trigger_function.proretset
                          AND trigger_function.provolatile = 'v'
                          AND trigger_function.prokind = 'f'
                          AND NOT trigger_function.prosecdef
                          AND NOT trigger_function.proleakproof
                          AND trigger_function.proparallel = 'u'
                          AND trigger_function.prosupport = 0
                          AND trigger_function.proconfig =
                              ARRAY['search_path=pg_catalog']::text[]
                          AND trigger_language.lanname = 'plpgsql'
                          AND btrim(regexp_replace(trigger_function.prosrc,
                              '[[:space:]]+', ' ', 'g'
                          )) = %s
                    ) AS release_identity_ready
                """,
                (
                    _RELEASE_IDENTITY_CHECK_DEFINITION,
                    _RELEASE_IDENTITY_VALIDATOR_SOURCE,
                    _RELEASE_IDENTITY_TRIGGER_SOURCE,
                ),
            )
            row = cursor.fetchone()
        if row is None or len(row) != 7:
            raise RagDatabaseError("database schema status is unavailable")
        return cast(
            tuple[bool, bool, bool, bool, bool, bool, bool],
            tuple(bool(value) for value in row),
        )

    def ensure_schema(self) -> None:
        """Apply forward migrations and fail closed on an obsolete partial contract."""
        (
            schema_exists,
            schema_ready,
            facets_exist,
            facets_ready,
            poster_ceiling_ready,
            release_identity_exists,
            release_identity_ready,
        ) = self._schema_status()
        migrated = False
        if not schema_exists:
            self.migrate()
            self._migrate_facets()
            self._migrate_poster_ceiling()
            self._migrate_release_identity()
            migrated = True
        else:
            if not schema_ready:
                raise RagDatabaseError("database schema contract is outdated")
            if not facets_exist:
                self._migrate_facets()
                migrated = True
            elif not facets_ready:
                raise RagDatabaseError("movie facet schema contract is outdated")
            if not poster_ceiling_ready:
                self._migrate_poster_ceiling()
                migrated = True
            if not release_identity_exists:
                self._migrate_release_identity()
                migrated = True
            elif not release_identity_ready:
                self._migrate_contract_driven_release_identity()
                migrated = True
        if migrated:
            (
                schema_exists,
                schema_ready,
                facets_exist,
                facets_ready,
                poster_ceiling_ready,
                release_identity_exists,
                release_identity_ready,
            ) = self._schema_status()
            if not schema_exists or not schema_ready:
                raise RagDatabaseError("database schema contract is outdated")
            if not facets_exist or not facets_ready:
                raise RagDatabaseError("movie facet schema contract is outdated")
            if not poster_ceiling_ready:
                raise RagDatabaseError("poster byte ceiling schema contract is outdated")
            if not release_identity_exists or not release_identity_ready:
                raise RagDatabaseError("release identity schema contract is outdated")

    def begin_release(self, contract: RagReleaseContract) -> str:
        """Insert or resume only the exact verified immutable release contract."""
        expected_counts = self._expected_counts(contract)
        self._validate_release_contract(contract)
        with self._cursor() as cursor:
            cursor.execute(
                """
                INSERT INTO rag_releases (
                    release_id, status, expected_movies, expected_assets,
                    expected_metadata_passages, expected_documents, expected_pdf_passages,
                    embedding_model, embedding_dimension, generation_model, access_mode,
                    manifest_sha256, document_embedding_profile, contract_json
                ) VALUES (
                    %s, 'loading', %s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s,
                    %s::jsonb
                )
                ON CONFLICT (release_id) DO NOTHING
                """,
                (
                    contract.rag_release_id,
                    *expected_counts,
                    contract.embedding_model,
                    contract.embedding_dimension,
                    contract.generation_model,
                    contract.access_mode,
                    contract.manifest_sha256,
                    contract.document_embedding_profile,
                    self._contract_json(contract),
                ),
            )
            cursor.execute(
                """
                SELECT release.status,
                       release.expected_movies, release.expected_assets,
                       release.expected_metadata_passages, release.expected_documents,
                       release.expected_pdf_passages,
                       release.embedding_model, release.embedding_dimension,
                       release.generation_model, release.access_mode,
                       release.manifest_sha256, release.document_embedding_profile,
                       release.contract_json,
                       (SELECT COUNT(*) FROM document_chunks AS chunk
                        WHERE chunk.release_id = release.release_id
                          AND chunk.embedding IS NOT NULL) AS embeddings
                FROM rag_releases AS release
                WHERE release.release_id = %s
                FOR UPDATE
                """,
                (contract.rag_release_id,),
            )
            row = cursor.fetchone()
            if row is None:
                raise RagDatabaseError("release state is unavailable")
            stored = self._stored_contract_from_row(contract.rag_release_id, row)
            self._assert_stored_contract_matches(
                contract,
                stored,
                allow_legacy_identity=stored.status == "loading",
                allow_transitional_contract=stored.status == "loading",
            )
            if stored.status not in {"loading", "active"}:
                raise RagDatabaseError("failed release cannot be resumed")
            if (
                stored.status == "loading"
                and stored.manifest_sha256 is None
                and stored.document_embedding_profile is None
                and stored.persisted_contract is None
            ):
                cursor.execute(
                    """
                    UPDATE rag_releases
                    SET manifest_sha256 = %s,
                        document_embedding_profile = %s,
                        contract_json = %s::jsonb
                    WHERE release_id = %s
                      AND status = 'loading'
                      AND manifest_sha256 IS NULL
                      AND document_embedding_profile IS NULL
                      AND contract_json IS NULL
                    """,
                    (
                        contract.manifest_sha256,
                        contract.document_embedding_profile,
                        self._contract_json(contract),
                        contract.rag_release_id,
                    ),
                )
                if hasattr(cursor, "rowcount") and cursor.rowcount != 1:
                    raise RagDatabaseError("legacy release identity claim failed")
            elif (
                stored.status == "loading"
                and stored.manifest_sha256 is not None
                and stored.document_embedding_profile is not None
                and stored.persisted_contract is None
            ):
                cursor.execute(
                    """
                    UPDATE rag_releases
                    SET contract_json = %s::jsonb
                    WHERE release_id = %s
                      AND status = 'loading'
                      AND manifest_sha256 = %s
                      AND document_embedding_profile = %s
                      AND contract_json IS NULL
                    """,
                    (
                        self._contract_json(contract),
                        contract.rag_release_id,
                        contract.manifest_sha256,
                        contract.document_embedding_profile,
                    ),
                )
                if hasattr(cursor, "rowcount") and cursor.rowcount != 1:
                    raise RagDatabaseError("transitional release contract claim failed")
        return contract.rag_release_id

    def release_state(self, release_id: str) -> ReleaseState:
        with self._cursor() as cursor:
            cursor.execute(
                """
                SELECT release.status, release.embedding_model, release.embedding_dimension,
                       release.manifest_sha256, release.document_embedding_profile,
                       (SELECT COUNT(*) FROM document_chunks AS chunk
                        WHERE chunk.release_id = release.release_id
                          AND chunk.embedding IS NOT NULL) AS embeddings
                FROM rag_releases AS release
                WHERE release.release_id = %s
                """,
                (release_id,),
            )
            row = cursor.fetchone()
        if row is None:
            raise RagDatabaseError("release state is unavailable")
        return self._state_from_row(row)

    def assert_active_bundle_matches(
        self,
        release_id: str,
        records: Iterable[Mapping[str, object]],
        *,
        contract: RagReleaseContract | None = None,
    ) -> None:
        """Lock and compare an active bundle, atomically claiming a legacy identity."""
        record_rows = tuple(records)
        expected = self._active_bundle_records(record_rows)
        expected_facets: dict[str, dict[str, object]] | None = None
        if contract is not None:
            self._validate_release_contract(contract)
            expected_facets = self._facet_records(record_rows)
            self._assert_facet_counts(
                self._facet_stats_from_records(expected_facets.values()),
                self._facet_expectations(contract),
            )
        with self._cursor() as cursor:
            cursor.execute(
                """
                SELECT release.status,
                       release.expected_movies, release.expected_assets,
                       release.expected_metadata_passages, release.expected_documents,
                       release.expected_pdf_passages,
                       release.embedding_model, release.embedding_dimension,
                       release.generation_model, release.access_mode,
                       release.manifest_sha256, release.document_embedding_profile,
                       release.contract_json,
                       (SELECT COUNT(*) FROM document_chunks AS chunk
                        WHERE chunk.release_id = release.release_id
                          AND chunk.embedding IS NOT NULL) AS embeddings
                FROM rag_releases AS release
                WHERE release.release_id = %s AND release.status = 'active'
                FOR UPDATE
                """,
                (release_id,),
            )
            release_row = cursor.fetchone()
            if release_row is None:
                raise RagDatabaseError("active release is unavailable")
            stored_contract = self._stored_contract_from_row(release_id, release_row)
            if contract is not None:
                self._assert_stored_contract_matches(
                    contract,
                    stored_contract,
                    allow_legacy_identity=True,
                    allow_transitional_contract=True,
                )
            cursor.execute(
                """
                WITH persisted_records AS (
                    SELECT 'movie'::text AS record_kind,
                           movie_id AS record_id,
                           jsonb_build_object(
                               'movie_id', movie_id,
                               'payload_sha256', payload_sha256,
                               'payload', payload
                           ) AS record_body
                    FROM movies
                    WHERE release_id = %s
                    UNION ALL
                    SELECT 'poster'::text,
                           asset_id,
                           jsonb_build_object(
                               'asset_id', asset_id,
                               'movie_id', movie_id,
                               'asset_type', asset_type,
                               'content_sha256', content_sha256,
                               'original_object_uri', original_object_uri,
                               'derived_object_uri', derived_object_uri,
                               'derived_content_sha256', derived_content_sha256,
                               'derived_byte_length', derived_byte_length,
                               'derived_mime_type', derived_mime_type,
                               'is_primary', is_primary,
                               'payload', payload
                           )
                    FROM media_assets
                    WHERE release_id = %s
                    UNION ALL
                    SELECT 'document'::text,
                           document_id,
                           jsonb_build_object(
                               'document_id', document_id,
                               'movie_id', movie_id,
                               'content_sha256', content_sha256,
                               'source_filename', source_filename,
                               'rights_status', rights_status,
                               'quality_status', quality_status,
                               'payload', payload
                           )
                    FROM movie_documents
                    WHERE release_id = %s
                    UNION ALL
                    SELECT 'passage'::text,
                           passage_id,
                           jsonb_build_object(
                               'passage_id', passage_id,
                               'movie_id', movie_id,
                               'document_id', document_id,
                               'passage_kind', passage_kind,
                               'page_number', page_number,
                               'body', body,
                               'content_sha256', content_sha256
                           )
                    FROM document_chunks
                    WHERE release_id = %s
                )
                SELECT record_kind, record_id, record_body
                FROM persisted_records
                ORDER BY record_kind, record_id
                """,
                (release_id, release_id, release_id, release_id),
            )
            stored: dict[tuple[str, str], str] = {}
            for row in cursor.fetchall():
                if (
                    len(row) != 3
                    or not isinstance(row[0], str)
                    or not isinstance(row[1], str)
                    or not isinstance(row[2], Mapping)
                ):
                    raise RagDatabaseError("active bundle record is invalid")
                key = (row[0], row[1])
                if key in stored:
                    raise RagDatabaseError("active bundle content conflict")
                try:
                    stored[key] = self._canonical_json(row[2])
                except (TypeError, ValueError):
                    raise RagDatabaseError("active bundle record is invalid") from None
            if stored != expected:
                raise RagDatabaseError("active bundle content conflict")
            if expected_facets is not None:
                self._assert_active_facets_match(cursor, release_id, expected_facets)
            if (
                contract is not None
                and stored_contract.manifest_sha256 is None
                and stored_contract.document_embedding_profile is None
                and stored_contract.persisted_contract is None
            ):
                cursor.execute(
                    """
                    UPDATE rag_releases
                    SET manifest_sha256 = %s,
                        document_embedding_profile = %s,
                        contract_json = %s::jsonb
                    WHERE release_id = %s
                      AND manifest_sha256 IS NULL
                      AND document_embedding_profile IS NULL
                      AND contract_json IS NULL
                    """,
                    (
                        contract.manifest_sha256,
                        contract.document_embedding_profile,
                        self._contract_json(contract),
                        release_id,
                    ),
                )
                if hasattr(cursor, "rowcount") and cursor.rowcount != 1:
                    raise RagDatabaseError("legacy release identity claim failed")
            elif (
                contract is not None
                and stored_contract.manifest_sha256 is not None
                and stored_contract.document_embedding_profile is not None
                and stored_contract.persisted_contract is None
            ):
                cursor.execute(
                    """
                    UPDATE rag_releases
                    SET contract_json = %s::jsonb
                    WHERE release_id = %s
                      AND status = 'active'
                      AND manifest_sha256 = %s
                      AND document_embedding_profile = %s
                      AND contract_json IS NULL
                    """,
                    (
                        self._contract_json(contract),
                        release_id,
                        contract.manifest_sha256,
                        contract.document_embedding_profile,
                    ),
                )
                if hasattr(cursor, "rowcount") and cursor.rowcount != 1:
                    raise RagDatabaseError("transitional release contract claim failed")

    def query_ready_contract(self, release_id: str) -> RagReleaseContract:
        """Return the exact persisted authority for one fully query-ready active release."""
        with self._cursor() as cursor:
            contract = self._lock_active_release(cursor, release_id)
            release, facets, posters = self._query_readiness_stats(cursor, release_id)
            self._assert_counts(
                contract.expected_counts,
                (
                    release.movies,
                    release.assets,
                    release.metadata_passages,
                    release.documents,
                    release.pdf_passages,
                )
            )
            if release.embeddings != contract.expected_embeddings:
                raise RagDatabaseError(
                    f"embeddings: expected {contract.expected_embeddings}, "
                    f"got {release.embeddings}"
                )
            selected = contract.persisted_contract
            if selected is None:
                raise RagDatabaseError("active release contract is unavailable")
            self._assert_facet_counts(facets, self._facet_expectations(selected))
            self._assert_bundle_poster_semantics(
                posters,
                self._poster_expectations(selected),
            )
        return selected

    def assert_query_ready(self, release_id: str) -> None:
        """Fail closed unless one active-release snapshot matches the complete query contract."""
        self.query_ready_contract(release_id)

    def start_ingestion_run(self, release_id: str, run_id: str) -> None:
        if not run_id:
            raise RagDatabaseError("ingestion run ID must be non-empty")
        with self._cursor() as cursor:
            cursor.execute(
                """
                INSERT INTO ingestion_runs (release_id, run_id, status)
                VALUES (%s, %s, 'running')
                """,
                (release_id, run_id),
            )

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
        if status not in {"complete", "failed"}:
            raise RagDatabaseError("ingestion run status must be complete or failed")
        if embedded_count < 0 or skipped_count < 0:
            raise RagDatabaseError("ingestion run counts must be non-negative")
        if status == "complete" and error_summary is not None:
            raise RagDatabaseError("complete ingestion run cannot contain an error summary")
        if status == "failed" and not error_summary:
            raise RagDatabaseError("failed ingestion run requires an error summary")
        if error_summary is not None and len(error_summary) > 512:
            raise RagDatabaseError("ingestion error summary is too long")
        with self._cursor() as cursor:
            cursor.execute(
                """
                UPDATE ingestion_runs
                SET status = %s, embedded_count = %s, skipped_count = %s,
                    error_summary = %s, completed_at = now()
                WHERE release_id = %s AND run_id = %s AND status = 'running'
                """,
                (
                    status,
                    embedded_count,
                    skipped_count,
                    error_summary,
                    release_id,
                    run_id,
                ),
            )
            if hasattr(cursor, "rowcount") and cursor.rowcount != 1:
                raise RagDatabaseError("ingestion run is not running")

    def upsert_bundle_records(
        self, release_id: str, records: Iterable[Mapping[str, object]]
    ) -> None:
        with self._cursor() as cursor:
            self._lock_loading_release(cursor, release_id)
            for record in records:
                self._upsert_record(cursor, release_id, record)

    def pending_passages(self, release_id: str, limit: int = 100) -> list[dict[str, object]]:
        if limit < 1:
            raise RagDatabaseError("limit must be positive")
        with self._cursor() as cursor:
            cursor.execute(
                """
                SELECT chunk.passage_id, chunk.movie_id, chunk.passage_kind,
                       COALESCE(NULLIF(movie.payload ->> 'chinese_title', ''),
                                NULLIF(movie.payload ->> 'english_title', ''),
                                chunk.movie_id) AS title,
                       chunk.body, chunk.content_sha256
                FROM document_chunks AS chunk
                JOIN movies AS movie
                  ON movie.release_id = chunk.release_id AND movie.movie_id = chunk.movie_id
                WHERE chunk.release_id = %s AND chunk.embedding IS NULL
                ORDER BY chunk.passage_id
                LIMIT %s
                """,
                (release_id, limit),
            )
            rows = cursor.fetchall()
        return [
            {
                "passage_id": row[0],
                "movie_id": row[1],
                "passage_kind": row[2],
                "title": row[3],
                "body": row[4],
                "content_sha256": row[5],
            }
            for row in rows
        ]

    def reuse_embeddings(
        self, target_release_id: str, source_release_id: str
    ) -> EmbeddingReuseResult:
        """Copy only exact embedding inputs inside one locked PostgreSQL transaction."""
        if target_release_id == source_release_id:
            raise RagDatabaseError("embedding reuse contract is invalid")
        with self._cursor() as cursor:
            self._lock_reuse_contracts(cursor, target_release_id, source_release_id)
            cursor.execute(
                """
                WITH eligible AS MATERIALIZED (
                    SELECT target.ctid AS target_ctid,
                           target.embedding AS target_embedding,
                           source.embedding AS source_embedding
                    FROM document_chunks AS target
                    JOIN document_chunks AS source
                      ON source.passage_id = target.passage_id
                    JOIN movies AS target_movie
                      ON target_movie.release_id = target.release_id
                     AND target_movie.movie_id = target.movie_id
                    JOIN movies AS source_movie
                      ON source_movie.release_id = source.release_id
                     AND source_movie.movie_id = source.movie_id
                    CROSS JOIN LATERAL (
                        SELECT COALESCE(
                            NULLIF(target_movie.payload ->> 'chinese_title', ''),
                            NULLIF(target_movie.payload ->> 'english_title', ''),
                            target.movie_id
                        ) AS embedding_title
                    ) AS target_title
                    CROSS JOIN LATERAL (
                        SELECT COALESCE(
                            NULLIF(source_movie.payload ->> 'chinese_title', ''),
                            NULLIF(source_movie.payload ->> 'english_title', ''),
                            source.movie_id
                        ) AS embedding_title
                    ) AS source_title
                    WHERE target.release_id = %s
                      AND source.release_id = %s
                      AND source.embedding IS NOT NULL
                      AND source.passage_id = target.passage_id
                      AND source.movie_id = target.movie_id
                      AND source.passage_kind = target.passage_kind
                      AND source.document_id IS NOT DISTINCT FROM target.document_id
                      AND source.page_number = target.page_number
                      AND source.body = target.body
                      AND source.content_sha256 = target.content_sha256
                      AND source_title.embedding_title = target_title.embedding_title
                ),
                copied AS (
                    UPDATE document_chunks AS target
                    SET embedding = eligible.source_embedding
                    FROM eligible
                    WHERE target.ctid = eligible.target_ctid
                      AND target.embedding IS NULL
                    RETURNING 1
                )
                SELECT COUNT(*) AS source_eligible_total,
                       (SELECT COUNT(*) FROM copied) AS copied_this_run,
                       COUNT(*) FILTER (
                           WHERE eligible.target_embedding IS NOT NULL
                             AND eligible.target_embedding IS DISTINCT FROM
                                 eligible.source_embedding
                       ) AS mismatched_preexisting
                FROM eligible
                """,
                (target_release_id, source_release_id),
            )
            row = cursor.fetchone()
            if row is None or len(row) != 3:
                raise RagDatabaseError("embedding reuse result is unavailable")
            eligible_total, copied_this_run, mismatched_preexisting = (
                self._non_negative_count(value, "embedding reuse result") for value in row
            )
            if mismatched_preexisting:
                raise RagDatabaseError("preexisting reusable embedding differs from source")
            return EmbeddingReuseResult(eligible_total, copied_this_run)

    def embedding_reuse_stats(
        self, target_release_id: str, source_release_id: str
    ) -> EmbeddingReuseStats:
        """Measure final strict source eligibility and populated non-reusable vectors."""
        if target_release_id == source_release_id:
            raise RagDatabaseError("embedding reuse contract is invalid")
        with self._cursor() as cursor:
            self._lock_reuse_contracts(
                cursor,
                target_release_id,
                source_release_id,
                allow_active_target=True,
            )
            cursor.execute(
                """
                WITH reusable AS MATERIALIZED (
                    SELECT target.passage_id,
                           target.embedding AS target_embedding,
                           source.embedding AS source_embedding
                    FROM document_chunks AS target
                    JOIN document_chunks AS source
                      ON source.passage_id = target.passage_id
                    JOIN movies AS target_movie
                      ON target_movie.release_id = target.release_id
                     AND target_movie.movie_id = target.movie_id
                    JOIN movies AS source_movie
                      ON source_movie.release_id = source.release_id
                     AND source_movie.movie_id = source.movie_id
                    CROSS JOIN LATERAL (
                        SELECT COALESCE(
                            NULLIF(target_movie.payload ->> 'chinese_title', ''),
                            NULLIF(target_movie.payload ->> 'english_title', ''),
                            target.movie_id
                        ) AS embedding_title
                    ) AS target_title
                    CROSS JOIN LATERAL (
                        SELECT COALESCE(
                            NULLIF(source_movie.payload ->> 'chinese_title', ''),
                            NULLIF(source_movie.payload ->> 'english_title', ''),
                            source.movie_id
                        ) AS embedding_title
                    ) AS source_title
                    WHERE target.release_id = %s
                      AND source.release_id = %s
                      AND source.embedding IS NOT NULL
                      AND source.passage_id = target.passage_id
                      AND source.movie_id = target.movie_id
                      AND source.passage_kind = target.passage_kind
                      AND source.document_id IS NOT DISTINCT FROM target.document_id
                      AND source.page_number = target.page_number
                      AND source.body = target.body
                      AND source.content_sha256 = target.content_sha256
                      AND source_title.embedding_title = target_title.embedding_title
                )
                SELECT COUNT(*) FILTER (
                           WHERE reusable.target_embedding IS NOT NULL
                             AND reusable.target_embedding IS NOT DISTINCT FROM
                                 reusable.source_embedding
                       ) AS source_eligible_total,
                       (
                           SELECT COUNT(*)
                           FROM document_chunks AS populated
                           WHERE populated.release_id = %s
                             AND populated.embedding IS NOT NULL
                             AND NOT EXISTS (
                                 SELECT 1 FROM reusable
                                 WHERE reusable.passage_id = populated.passage_id
                             )
                       ) AS non_reusable_embedded_total
                FROM reusable
                """,
                (target_release_id, source_release_id, target_release_id),
            )
            row = cursor.fetchone()
        if row is None or len(row) != 2:
            raise RagDatabaseError("embedding reuse statistics are unavailable")
        return EmbeddingReuseStats(
            self._non_negative_count(row[0], "source-eligible embedding count"),
            self._non_negative_count(row[1], "non-reusable embedding count"),
        )

    def store_embedding(
        self,
        release_id: str,
        passage_id: str,
        content_sha256: str,
        embedding: Sequence[float],
        *,
        embedding_model: str,
        embedding_dimension: int,
    ) -> None:
        if len(embedding) != 768:
            raise RagDatabaseError("embedding must contain 768 values")
        with self._cursor() as cursor:
            self._lock_loading_embedding_contract(
                cursor,
                release_id,
                embedding_model,
                embedding_dimension,
            )
            cursor.execute(
                """
                UPDATE document_chunks
                SET embedding = %s::vector
                WHERE release_id = %s AND passage_id = %s AND content_sha256 = %s
                      AND embedding IS NULL
                """,
                (self._vector(embedding), release_id, passage_id, content_sha256),
            )
            if hasattr(cursor, "rowcount") and cursor.rowcount != 1:
                raise RagDatabaseError("passage content changed before embedding persistence")

    def activate_release(
        self,
        release_id: str,
        *,
        embedding_model: str,
        embedding_dimension: int,
    ) -> None:
        with self._cursor() as cursor:
            contract = self._lock_loading_embedding_contract(
                cursor,
                release_id,
                embedding_model,
                embedding_dimension,
            )
            actual = self._release_stats(cursor, release_id)
            self._assert_counts(
                contract.expected_counts,
                (
                    actual.movies,
                    actual.assets,
                    actual.metadata_passages,
                    actual.documents,
                    actual.pdf_passages,
                )
            )
            poster_semantics = self._poster_semantic_counts(cursor, release_id)
            selected = contract.persisted_contract
            if selected is None:
                raise RagDatabaseError(
                    "release is not loading or embedding contract changed"
                )
            self._assert_facet_counts(
                self._facet_stats(cursor, release_id),
                self._facet_expectations(selected),
            )
            self._assert_bundle_poster_semantics(
                poster_semantics,
                self._poster_expectations(selected),
            )
            if actual.embeddings != contract.expected_embeddings:
                raise RagDatabaseError(
                    f"embeddings: expected {contract.expected_embeddings}, "
                    f"got {actual.embeddings}"
                )
            cursor.execute(
                """
                UPDATE rag_releases
                SET status = 'active', activated_at = now()
                WHERE release_id = %s AND status = 'loading'
                """,
                (release_id,),
            )

    def reconcile_facets(
        self, release_id: str, records: Iterable[Mapping[str, object]]
    ) -> None:
        """Persist the complete immutable facet set once, then verify it on every resume."""
        facets = self._facet_records(records)
        with self._cursor() as cursor:
            cursor.execute(
                """
                SELECT status, contract_json
                FROM rag_releases
                WHERE release_id = %s AND status IN ('loading', 'active')
                FOR UPDATE
                """,
                (release_id,),
            )
            release_row = cursor.fetchone()
            if release_row is None or len(release_row) != 2:
                raise RagDatabaseError("release is not loading or active")
            status, contract_payload = release_row
            selected = self._contract_from_payload(contract_payload)
            if selected.rag_release_id != release_id:
                raise RagDatabaseError("release contract is immutable")
            expected_facets = self._facet_expectations(selected)
            self._assert_facet_counts(
                self._facet_stats_from_records(facets.values()),
                expected_facets,
            )
            cursor.execute(
                """
                SELECT movie_id, content_sha256, tier, tier_reason, human_review,
                       pilot_movie, pilot_evidence
                FROM movie_facets
                WHERE release_id = %s
                FOR UPDATE
                """,
                (release_id,),
            )
            stored: dict[str, tuple[str, dict[str, object]]] = {}
            for row in cursor.fetchall():
                if len(row) != 7 or not isinstance(row[0], str) or not isinstance(row[1], str):
                    raise RagDatabaseError("stored facet record is invalid")
                stored[row[0]] = (
                    row[1],
                    {
                        "movie_id": row[0],
                        "tier": row[2],
                        "tier_reason": row[3],
                        "human_review": row[4],
                        "pilot_movie": row[5],
                        "pilot_evidence": row[6],
                    },
                )
            unexpected = sorted(set(stored) - set(facets))
            if unexpected:
                raise RagDatabaseError("facet coverage conflict")
            for movie_id, (content_sha256, stored_body) in stored.items():
                facet_body = self._facet_body(facets[movie_id])
                try:
                    stored_body_hash = self._facet_content_sha256(stored_body)
                except (TypeError, ValueError):
                    raise RagDatabaseError("facet content conflict") from None
                if (
                    facets[movie_id]["content_sha256"] != content_sha256
                    or stored_body_hash != content_sha256
                    or stored_body != facet_body
                ):
                    raise RagDatabaseError("facet content conflict")
            missing = set(facets) - set(stored)
            if status == "active" and missing:
                raise RagDatabaseError("facet coverage conflict")
            for movie_id in sorted(missing):
                facet = facets[movie_id]
                pilot_evidence = facet["pilot_evidence"]
                cursor.execute(
                    """
                    INSERT INTO movie_facets (
                        release_id, movie_id, content_sha256, tier, tier_reason,
                        human_review, pilot_movie, pilot_evidence
                    ) VALUES (%s, %s, %s, %s, %s, %s, %s, %s::jsonb)
                    """,
                    (
                        release_id,
                        movie_id,
                        facet["content_sha256"],
                        facet["tier"],
                        facet["tier_reason"],
                        facet["human_review"],
                        facet["pilot_movie"],
                        (
                            None
                            if pilot_evidence is None
                            else self._canonical_json(pilot_evidence)
                        ),
                    ),
                )
            self._assert_facet_counts(
                self._facet_stats(cursor, release_id),
                expected_facets,
            )

    def facet_stats(self, release_id: str) -> FacetStats:
        with self._cursor() as cursor:
            return self._facet_stats(cursor, release_id)

    def resolve_recommendation_person(
        self, release_id: str, question: str
    ) -> ResolvedPerson | None:
        """Resolve one canonical release credit named by normalized query text."""
        if not isinstance(release_id, str) or not release_id.strip():
            raise RagDatabaseError("person resolution release ID must be non-empty")
        if not isinstance(question, str) or not question.strip():
            raise RagDatabaseError("person resolution question must be non-empty")
        raw_question = question.strip()
        normalized_question = normalize_query_text(raw_question)
        person_shape = parse_person_query_shape(normalized_question)
        if person_shape is not None and len(person_shape.candidate_names) != 1:
            raise AmbiguousPersonResolutionError(
                "multiple Release person identities"
            )
        if person_shape is not None and (
            person_shape.exclusionary or person_shape.role_conflict
        ):
            return None
        compiler_candidate = (
            person_shape.candidate_names[0] if person_shape is not None else None
        )
        requested_role = (
            person_shape.role
            if person_shape is not None
            else infer_person_role(normalized_question)
        )
        lexicon = self._release_credit_lexicon(release_id)
        if not lexicon:
            return None
        simplified_question = credit_query_alias(normalized_question)

        exact_candidates: set[_PersonIdentityMatch] = set()
        alias_candidates: set[_PersonIdentityMatch] = set()
        for canonical_name, _, simplified_alias, equivalence_key in lexicon:
            position = raw_question.find(canonical_name)
            while position >= 0:
                exact_candidates.add(
                    _PersonIdentityMatch(
                        position,
                        position + len(canonical_name),
                        canonical_name,
                        simplified_alias,
                        equivalence_key,
                        True,
                    )
                )
                position = raw_question.find(canonical_name, position + 1)

            position = simplified_question.find(simplified_alias)
            while position >= 0:
                alias_candidates.add(
                    _PersonIdentityMatch(
                        position,
                        position + len(simplified_alias),
                        canonical_name,
                        simplified_alias,
                        equivalence_key,
                        False,
                    )
                )
                position = simplified_question.find(simplified_alias, position + 1)

        maximal_exact_matches = tuple(
            match
            for match in exact_candidates
            if not any(
                other.start <= match.start
                and other.end >= match.end
                and (other.start < match.start or other.end > match.end)
                for other in exact_candidates
            )
        )
        nonoverlapping_aliases = tuple(
            match
            for match in alias_candidates
            if not any(
                match.start < exact.end and exact.start < match.end
                for exact in maximal_exact_matches
            )
        )
        maximal_alias_matches = tuple(
            match
            for match in nonoverlapping_aliases
            if not any(
                other.start <= match.start
                and other.end >= match.end
                and (other.start < match.start or other.end > match.end)
                for other in nonoverlapping_aliases
            )
        )
        identity_matches = (*maximal_exact_matches, *maximal_alias_matches)

        if compiler_candidate is not None:
            candidate_position = normalized_question.find(compiler_candidate)
            candidate_end = candidate_position + len(compiler_candidate)
            if candidate_position < 0 or not any(
                match.start == candidate_position and match.end == candidate_end
                for match in identity_matches
            ):
                return None

        identity_classes = {
            (match.simplified_alias, match.equivalence_key)
            for match in identity_matches
        }
        if len(identity_classes) > 1:
            raise AmbiguousPersonResolutionError(
                "multiple Release person identities"
            )
        if not identity_classes:
            return None
        simplified_alias, equivalence_key = next(iter(identity_classes))
        exact_class_matches = tuple(
            match
            for match in maximal_exact_matches
            if (match.simplified_alias, match.equivalence_key)
            == (simplified_alias, equivalence_key)
        )
        canonical_name = (
            min(
                exact_class_matches,
                key=lambda match: (
                    match.start,
                    -(match.end - match.start),
                    match.canonical_name,
                ),
            ).canonical_name
            if exact_class_matches
            else min(
                name
                for name, _, entry_alias, entry_equivalence_key in lexicon
                if entry_alias == simplified_alias
                and entry_equivalence_key == equivalence_key
            )
        )
        exact_names = tuple(
            sorted(
                {
                    name
                    for name, _, entry_alias, entry_equivalence_key in lexicon
                    if entry_alias == simplified_alias
                    and entry_equivalence_key == equivalence_key
                }
            )
        )
        return ResolvedPerson(
            canonical_name,
            cast(PersonRole | None, requested_role),
            exact_names,
        )

    def _release_credit_lexicon(
        self, release_id: str
    ) -> tuple[tuple[str, PersonRole, str, str], ...]:
        with self._person_lexicons_lock:
            cached = self._person_lexicons.get(release_id)
            if cached is not None:
                return cached
            with self._cursor() as cursor:
                cursor.execute(
                    """
                    WITH credit_tokens AS (
                        SELECT 'director'::text AS role, btrim(credit.name) AS name
                        FROM movies AS movie
                        JOIN rag_releases AS release
                          ON release.release_id = movie.release_id
                         AND release.status = 'active'
                        CROSS JOIN LATERAL unnest(regexp_split_to_array(
                            COALESCE(movie.payload ->> 'director', ''), '[、,，/;；]'
                        )) AS credit(name)
                        WHERE movie.release_id = %s
                        UNION ALL
                        SELECT 'actor'::text AS role, btrim(credit.name) AS name
                        FROM movies AS movie
                        JOIN rag_releases AS release
                          ON release.release_id = movie.release_id
                         AND release.status = 'active'
                        CROSS JOIN LATERAL unnest(regexp_split_to_array(
                            COALESCE(movie.payload ->> 'cast', ''), '[、,，/;；]'
                        )) AS credit(name)
                        WHERE movie.release_id = %s
                    )
                    SELECT DISTINCT name, role
                    FROM credit_tokens
                    WHERE name <> ''
                      AND char_length(name) BETWEEN 2 AND 64
                    ORDER BY name, role
                    """,
                    (release_id, release_id),
                )
                rows = cursor.fetchall()
            lexicon: list[tuple[str, PersonRole, str, str]] = []
            for row in rows:
                if len(row) != 2:
                    raise RagDatabaseError("person resolution result is invalid")
                name, role = row
                if (
                    not isinstance(name, str)
                    or not name.strip()
                    or role not in {"actor", "director"}
                ):
                    raise RagDatabaseError("person resolution result is invalid")
                canonical_name = name.strip()
                lexicon.append(
                    (
                        canonical_name,
                        cast(PersonRole, role),
                        credit_query_alias(canonical_name),
                        credit_equivalence_key(canonical_name),
                    )
                )
            immutable_lexicon = tuple(lexicon)
            self._person_lexicons[release_id] = immutable_lexicon
            return immutable_lexicon

    def search_deep_recommendations(
        self,
        release_id: str,
        embedding: Sequence[float],
        plan: RecommendationPlan,
    ) -> RecommendationSearch:
        """Select one PDF passage per eligible movie before applying the result limit."""
        if len(embedding) != 768:
            raise RagDatabaseError("embedding must contain 768 values")
        self._validate_recommendation_plan(plan)
        explicit_genres, genres_all, genres_any = self._plan_genres(plan)
        title_terms_any = list(self._normalized_values(plan.title_terms_any))
        credit_exact_names, credit_role = self._plan_credit_constraint(plan)
        candidate_limit = 8 if plan.diversify_decades else plan.requested_count
        with self._cursor() as cursor:
            self._lock_active_release_with_facets(cursor, release_id)
            cursor.execute(
                f"""
                WITH candidates AS (
                    SELECT chunk.passage_id, chunk.movie_id, chunk.passage_kind, chunk.body,
                           chunk.page_number, chunk.document_id, document.source_filename,
                           movie.payload, facet.tier, facet.tier_reason, facet.human_review,
                           facet.pilot_movie, facet.pilot_evidence, facet.content_sha256,
                           {_POSTER_AVAILABLE_PREDICATE} AS poster_available,
                           chunk.embedding <=> %s::vector AS distance,
                           release_date.release_year
                    FROM document_chunks AS chunk
                    JOIN rag_releases AS release
                      ON release.release_id = chunk.release_id AND release.status = 'active'
                    JOIN movies AS movie
                      ON movie.release_id = chunk.release_id AND movie.movie_id = chunk.movie_id
                    JOIN movie_facets AS facet
                      ON facet.release_id = movie.release_id AND facet.movie_id = movie.movie_id
                    JOIN movie_documents AS document
                      ON document.release_id = chunk.release_id
                     AND document.document_id = chunk.document_id
                     AND document.movie_id = chunk.movie_id
                    {_RELEASE_YEAR_LATERAL_SQL}
                    WHERE chunk.release_id = %s AND chunk.passage_kind = 'pdf'
                      AND chunk.embedding IS NOT NULL
                      AND (%s::text IS NULL OR facet.tier = %s::text)
                      {_GENRE_ANY_PREDICATE}
                      {_GENRE_ANY_PREDICATE}
                      AND (%s::integer IS NULL OR release_date.release_year >= %s::integer)
                      AND (%s::integer IS NULL OR release_date.release_year <= %s::integer)
                      {_GENRE_ALL_PREDICATE}
                      {_TITLE_TERMS_ANY_PREDICATE}
                      {_CREDIT_TOKEN_PREDICATE}
                      AND NOT (chunk.movie_id = ANY(%s::text[]))
                ), deduplicated AS (
                    SELECT *, ROW_NUMBER() OVER (
                        PARTITION BY movie_id ORDER BY distance, passage_id
                    ) AS movie_position
                    FROM candidates
                ), ranked AS (
                    SELECT *, COUNT(*) OVER() AS total_matches
                    FROM deduplicated
                    WHERE movie_position = 1
                )
                SELECT passage_id, movie_id, passage_kind, body, page_number, document_id,
                       source_filename, payload, tier, tier_reason, human_review, pilot_movie,
                       pilot_evidence, content_sha256, poster_available, distance, total_matches
                FROM ranked
                ORDER BY CASE tier WHEN 'S' THEN 0 WHEN 'A' THEN 1 ELSE 2 END,
                         CASE WHEN pilot_movie THEN 0 ELSE 1 END,
                         distance, release_year NULLS LAST, movie_id
                LIMIT %s
                """,
                (
                    self._vector(embedding),
                    release_id,
                    plan.tier,
                    plan.tier,
                    list(explicit_genres),
                    list(explicit_genres),
                    list(genres_any),
                    list(genres_any),
                    plan.year_from,
                    plan.year_from,
                    plan.year_to,
                    plan.year_to,
                    list(genres_all),
                    list(genres_all),
                    title_terms_any,
                    title_terms_any,
                    list(credit_exact_names),
                    list(credit_exact_names),
                    credit_role,
                    credit_role,
                    list(plan.excluded_movie_ids),
                    candidate_limit,
                ),
            )
            rows = cursor.fetchall()
        return self._recommendation_search(rows, expected_passage_kind="pdf")

    def search_recommendations(
        self,
        release_id: str,
        embedding: Sequence[float],
        plan: RecommendationPlan,
    ) -> RecommendationSearch:
        if len(embedding) != 768:
            raise RagDatabaseError("embedding must contain 768 values")
        self._validate_recommendation_plan(plan)
        explicit_genres, genres_all, genres_any = self._plan_genres(plan)
        title_terms_any = list(self._normalized_values(plan.title_terms_any))
        credit_exact_names, credit_role = self._plan_credit_constraint(plan)
        with self._cursor() as cursor:
            self._lock_active_release_with_facets(cursor, release_id)
            cursor.execute(
                f"""
                WITH candidates AS (
                    SELECT chunk.passage_id, chunk.movie_id, chunk.passage_kind, chunk.body,
                           chunk.page_number, chunk.document_id, document.source_filename,
                           movie.payload, facet.tier, facet.tier_reason, facet.human_review,
                           facet.pilot_movie, facet.pilot_evidence, facet.content_sha256,
                           {_POSTER_AVAILABLE_PREDICATE} AS poster_available,
                           chunk.embedding <=> %s::vector AS distance,
                           release_date.release_year
                    FROM document_chunks AS chunk
                    JOIN rag_releases AS release
                      ON release.release_id = chunk.release_id AND release.status = 'active'
                    JOIN movies AS movie
                      ON movie.release_id = chunk.release_id AND movie.movie_id = chunk.movie_id
                    JOIN movie_facets AS facet
                      ON facet.release_id = movie.release_id AND facet.movie_id = movie.movie_id
                    LEFT JOIN movie_documents AS document
                      ON document.release_id = chunk.release_id
                     AND document.document_id = chunk.document_id
                    {_RELEASE_YEAR_LATERAL_SQL}
                    WHERE chunk.release_id = %s AND chunk.passage_kind = 'metadata'
                      AND chunk.embedding IS NOT NULL
                      AND (%s::text IS NULL OR facet.tier = %s::text)
                      {_GENRE_ANY_PREDICATE}
                      {_GENRE_ANY_PREDICATE}
                      AND (%s::integer IS NULL OR release_date.release_year >= %s::integer)
                      AND (%s::integer IS NULL OR release_date.release_year <= %s::integer)
                      {_GENRE_ALL_PREDICATE}
                      {_TITLE_TERMS_ANY_PREDICATE}
                      {_CREDIT_TOKEN_PREDICATE}
                      AND NOT (chunk.movie_id = ANY(%s::text[]))
                ), deduplicated AS (
                    SELECT *, ROW_NUMBER() OVER (
                        PARTITION BY movie_id ORDER BY distance, passage_id
                    ) AS movie_position
                    FROM candidates
                ), ranked AS (
                    SELECT *, COUNT(*) OVER() AS total_matches
                    FROM deduplicated
                    WHERE movie_position = 1
                )
                SELECT passage_id, movie_id, passage_kind, body, page_number, document_id,
                       source_filename, payload, tier, tier_reason, human_review, pilot_movie,
                       pilot_evidence, content_sha256, poster_available, distance, total_matches
                FROM ranked
                ORDER BY CASE tier WHEN 'S' THEN 0 WHEN 'A' THEN 1 ELSE 2 END,
                         CASE WHEN pilot_movie THEN 0 ELSE 1 END,
                         distance, release_year NULLS LAST, movie_id
                LIMIT %s
                """,
                (
                    self._vector(embedding),
                    release_id,
                    plan.tier,
                    plan.tier,
                    list(explicit_genres),
                    list(explicit_genres),
                    list(genres_any),
                    list(genres_any),
                    plan.year_from,
                    plan.year_from,
                    plan.year_to,
                    plan.year_to,
                    list(genres_all),
                    list(genres_all),
                    title_terms_any,
                    title_terms_any,
                    list(credit_exact_names),
                    list(credit_exact_names),
                    credit_role,
                    credit_role,
                    list(plan.excluded_movie_ids),
                    plan.requested_count,
                ),
            )
            rows = cursor.fetchall()
        return self._recommendation_search(rows, expected_passage_kind="metadata")

    def resolve_explicit_movie_context(
        self, release_id: str, question: str
    ) -> ExplicitMovieResolution:
        """Resolve current IDs and residual text before history can bind."""
        if not isinstance(question, str) or not question.strip():
            raise RagDatabaseError("explicit target question must be non-empty")
        normalized_question_sql, normalized_question_params = sql_title_normalization(
            "%s::text"
        )
        broad_match_sql, broad_match_params = _broad_title_candidate_lateral_sql()
        with self._cursor() as cursor:
            cursor.execute(
                f"""
                WITH query_input AS (
                    SELECT %s::text AS question,
                           {normalized_question_sql} AS normalized_question
                ), candidates AS (
                    SELECT movie.movie_id,
                           COALESCE(movie.payload ->> 'chinese_title', '') AS chinese_title,
                           COALESCE(movie.payload ->> 'english_title', '') AS english_title,
                           broad_match.broad_position
                    FROM movies AS movie
                    JOIN rag_releases AS release
                      ON release.release_id = movie.release_id
                     AND release.status = 'active'
                    CROSS JOIN query_input
                    {broad_match_sql}
                    WHERE movie.release_id = %s
                      AND broad_match.broad_position IS NOT NULL
                )
                SELECT movie_id, chinese_title, english_title, broad_position
                FROM candidates
                ORDER BY GREATEST(char_length(chinese_title), char_length(english_title)) DESC,
                         broad_position,
                         movie_id
                LIMIT %s
                """,
                (
                    question,
                    normalize_query_text(question),
                    *normalized_question_params,
                    *broad_match_params,
                    release_id,
                    _MAX_EXPLICIT_TITLE_CANDIDATES,
                ),
            )
            rows = cursor.fetchall()
        candidates: list[tuple[str, str, str]] = []
        for row in rows:
            if (
                len(row) != 4
                or not isinstance(row[0], str)
                or _MOVIE_ID.fullmatch(row[0]) is None
                or not isinstance(row[1], str)
                or not isinstance(row[2], str)
                or not isinstance(row[3], int)
                or isinstance(row[3], bool)
                or row[3] < 1
            ):
                raise RagDatabaseError("explicit target result is invalid")
            candidates.append((row[0], row[1], row[2]))
        return resolve_explicit_movie_identity_context(
            question, candidates, max_results=_MAX_TARGET_MOVIE_IDS
        )

    def resolve_explicit_movie_ids(
        self, release_id: str, question: str
    ) -> tuple[str, ...]:
        """Resolve current-turn exact movie IDs before a pronoun binds to history."""
        return self.resolve_explicit_movie_context(release_id, question).movie_ids

    def search(
        self,
        release_id: str,
        embedding: Sequence[float],
        limit: int = 5,
        *,
        question: str | None = None,
        embedding_model: str | None = None,
        embedding_dimension: int | None = None,
        target_movie_ids: tuple[str, ...] = (),
    ) -> list[dict[str, object]]:
        if (
            not isinstance(target_movie_ids, tuple)
            or len(target_movie_ids) > _MAX_TARGET_MOVIE_IDS
            or len(set(target_movie_ids)) != len(target_movie_ids)
            or any(
                not isinstance(movie_id, str)
                or _MOVIE_ID.fullmatch(movie_id) is None
                for movie_id in target_movie_ids
            )
        ):
            raise RagDatabaseError("target movie IDs are invalid")
        if target_movie_ids and question is None:
            raise RagDatabaseError("target movie IDs require a search question")
        if len(embedding) != 768:
            raise RagDatabaseError("embedding must contain 768 values")
        if limit < 1:
            raise RagDatabaseError("limit must be positive")
        if question is not None:
            if not question.strip():
                raise RagDatabaseError("search question must be non-empty")
            if (
                not isinstance(embedding_model, str)
                or not embedding_model
                or embedding_dimension != 768
            ):
                raise RagDatabaseError("query embedding contract is invalid")
            return self._search_for_question(
                release_id,
                embedding,
                question,
                limit,
                embedding_model,
                embedding_dimension,
                target_movie_ids,
            )
        with self._cursor() as cursor:
            cursor.execute(
                """
                SELECT chunk.passage_id, chunk.movie_id, chunk.passage_kind, chunk.body,
                       chunk.embedding <=> %s::vector AS distance
                FROM document_chunks AS chunk
                JOIN rag_releases AS release ON release.release_id = chunk.release_id
                WHERE chunk.release_id = %s AND release.status = 'active'
                      AND chunk.embedding IS NOT NULL
                ORDER BY chunk.embedding <=> %s::vector, chunk.passage_id
                LIMIT %s
                """,
                (self._vector(embedding), release_id, self._vector(embedding), limit),
            )
            rows = cursor.fetchall()
        return [
            {
                "passage_id": row[0],
                "movie_id": row[1],
                "passage_kind": row[2],
                "body": row[3],
                "distance": row[4],
            }
            for row in rows
        ]

    def _search_for_question(
        self,
        release_id: str,
        embedding: Sequence[float],
        question: str,
        limit: int,
        embedding_model: str,
        embedding_dimension: int,
        target_movie_ids: tuple[str, ...],
    ) -> list[dict[str, object]]:
        """Rank exact ID/title movies before vector distance and return grounding records."""
        with self._cursor() as cursor:
            try:
                stored = self._lock_active_release(cursor, release_id)
            except RagDatabaseError:
                raise RagDatabaseError(
                    "active release query embedding contract changed"
                ) from None
            if (
                stored.embedding_model != embedding_model
                or stored.embedding_dimension != embedding_dimension
            ):
                raise RagDatabaseError("active release query embedding contract changed")
            persisted_contract = stored.persisted_contract
            if persisted_contract is None:  # Defensive; the release lock rejects this state.
                raise RagDatabaseError("active release query embedding contract changed")
            self._assert_facet_counts(
                self._facet_stats(cursor, release_id),
                self._facet_expectations(persisted_contract),
            )
            cursor.execute(
                f"""
                WITH query_input AS (
                    SELECT %s::vector AS query_embedding,
                           %s::text[] AS target_movie_ids
                ), scored AS (
                    SELECT chunk.passage_id, chunk.movie_id, chunk.passage_kind, chunk.body,
                           chunk.page_number, chunk.document_id, document.source_filename,
                           movie.payload || jsonb_build_object(
                               'tier', facet.tier,
                               'tier_reason', facet.tier_reason,
                               'pilot_movie', facet.pilot_movie,
                               'pilot_evidence', facet.pilot_evidence
                           ) AS payload,
                           {_POSTER_AVAILABLE_PREDICATE} AS poster_available,
                           CASE
                               WHEN chunk.movie_id = ANY(query_input.target_movie_ids)
                               THEN array_position(query_input.target_movie_ids, chunk.movie_id)
                           END AS exact_position,
                           chunk.embedding <=> query_input.query_embedding AS distance
                    FROM document_chunks AS chunk
                    JOIN rag_releases AS release
                      ON release.release_id = chunk.release_id AND release.status = 'active'
                     AND release.embedding_model = %s AND release.embedding_dimension = %s
                    JOIN movies AS movie
                      ON movie.release_id = chunk.release_id AND movie.movie_id = chunk.movie_id
                    JOIN movie_facets AS facet
                      ON facet.release_id = movie.release_id AND facet.movie_id = movie.movie_id
                    LEFT JOIN movie_documents AS document
                      ON document.release_id = chunk.release_id
                     AND document.document_id = chunk.document_id
                    CROSS JOIN query_input
                    WHERE chunk.release_id = %s AND chunk.embedding IS NOT NULL
                      AND (
                          cardinality(query_input.target_movie_ids) = 0
                          OR chunk.movie_id = ANY(query_input.target_movie_ids)
                      )
                )
                SELECT passage_id, movie_id, passage_kind, body, page_number, document_id,
                       source_filename, payload, poster_available, distance
                FROM scored
                ORDER BY CASE WHEN exact_position IS NULL THEN 1 ELSE 0 END,
                         exact_position NULLS LAST,
                         CASE WHEN exact_position IS NOT NULL AND passage_kind = 'metadata'
                              THEN 0 ELSE 1 END,
                         distance,
                         passage_id
                LIMIT %s
                """,
                (
                    self._vector(embedding),
                    list(target_movie_ids),
                    embedding_model,
                    embedding_dimension,
                    release_id,
                    limit,
                ),
            )
            rows = cursor.fetchall()
        results: list[dict[str, object]] = []
        for row in rows:
            if len(row) != 10 or not isinstance(row[7], Mapping):
                raise RagDatabaseError("search result is invalid")
            results.append(
                {
                    "passage_id": row[0],
                    "movie_id": row[1],
                    "passage_kind": row[2],
                    "body": row[3],
                    "page_number": row[4],
                    "document_id": row[5],
                    "source_filename": row[6],
                    "movie": dict(row[7]),
                    "poster_available": row[8],
                    "distance": row[9],
                }
            )
        return results

    def get_approved_poster_assets(
        self, release_id: str, movie_ids: Sequence[str]
    ) -> dict[str, dict[str, object]]:
        requested = tuple(movie_ids)
        if (
            not requested
            or len(requested) > _MAX_TARGET_MOVIE_IDS
            or len(set(requested)) != len(requested)
            or any(
                not isinstance(movie_id, str)
                or _MOVIE_ID.fullmatch(movie_id) is None
                for movie_id in requested
            )
        ):
            raise RagDatabaseError("poster lookup movie IDs are invalid")
        with self._cursor() as cursor:
            cursor.execute(
                f"""
                SELECT asset.asset_id, asset.movie_id, asset.derived_object_uri,
                       asset.content_sha256, asset.derived_content_sha256,
                       asset.derived_byte_length, asset.derived_mime_type,
                       asset.payload ->> 'quality_status' AS quality_status,
                       asset.payload ->> 'rights_status' AS rights_status
                FROM media_assets AS asset
                JOIN rag_releases AS release
                  ON release.release_id = asset.release_id AND release.status = 'active'
                WHERE asset.release_id = %s
                      AND asset.movie_id = ANY(%s::text[])
                      {_APPROVED_POSTER_ASSET_PREDICATE}
                ORDER BY array_position(%s::text[], asset.movie_id)
                """,
                (release_id, list(requested), list(requested)),
            )
            rows = cursor.fetchall()
        assets: dict[str, dict[str, object]] = {}
        for row in rows:
            if (
                len(row) != 9
                or not isinstance(row[1], str)
                or row[1] not in requested
                or row[1] in assets
            ):
                raise RagDatabaseError("poster asset record is invalid")
            assets[row[1]] = {
                "asset_id": row[0],
                "movie_id": row[1],
                "derived_object_uri": row[2],
                "content_sha256": row[3],
                "derived_content_sha256": row[4],
                "derived_byte_length": row[5],
                "derived_mime_type": row[6],
                "quality_status": row[7],
                "rights_status": row[8],
            }
        return assets

    def get_poster_asset(self, release_id: str, movie_id: str) -> dict[str, object] | None:
        return self.get_approved_poster_assets(release_id, (movie_id,)).get(movie_id)

    def poster_fleet_rows(self, release_id: str) -> list[dict[str, object]]:
        """Read every release-scoped primary poster state in one SQL query."""
        with self._cursor() as cursor:
            cursor.execute(
                """
                SELECT movie_id, derived_object_uri, derived_content_sha256,
                       derived_byte_length, derived_mime_type,
                       payload ->> 'quality_status' AS quality_status
                FROM media_assets
                WHERE release_id = %s AND asset_type = 'poster' AND is_primary
                ORDER BY movie_id
                """,
                (release_id,),
            )
            rows = cursor.fetchall()
        result: list[dict[str, object]] = []
        for row in rows:
            if len(row) != 6:
                raise RagDatabaseError("poster fleet record is invalid")
            result.append(
                {
                    "movie_id": row[0],
                    "derived_object_uri": row[1],
                    "derived_content_sha256": row[2],
                    "derived_byte_length": row[3],
                    "derived_mime_type": row[4],
                    "quality_status": row[5],
                }
            )
        return result

    def release_stats(self, release_id: str) -> ReleaseStats:
        with self._cursor() as cursor:
            return self._release_stats(cursor, release_id)

    def _release_stats(self, cursor: Any, release_id: str) -> ReleaseStats:
        cursor.execute(
            """
            SELECT
                (SELECT COUNT(*) FROM movies WHERE release_id = %s) AS movies,
                (SELECT COUNT(*) FROM media_assets WHERE release_id = %s) AS assets,
                (SELECT COUNT(*) FROM document_chunks WHERE release_id = %s
                    AND passage_kind = 'metadata') AS metadata_passages,
                (SELECT COUNT(*) FROM movie_documents WHERE release_id = %s) AS documents,
                (SELECT COUNT(*) FROM document_chunks WHERE release_id = %s
                    AND passage_kind = 'pdf') AS pdf_passages,
                (SELECT COUNT(*) FROM document_chunks WHERE release_id = %s
                    AND embedding IS NOT NULL) AS embeddings
            """,
            (release_id, release_id, release_id, release_id, release_id, release_id),
        )
        row = cursor.fetchone()
        if row is None:
            raise RagDatabaseError("release statistics are unavailable")
        return ReleaseStats(*(int(value) for value in row))

    @staticmethod
    def _poster_semantic_counts(cursor: Any, release_id: str) -> tuple[int, int, int]:
        cursor.execute(
            """
            SELECT
                COUNT(*) FILTER (
                    WHERE asset_type = 'poster' AND is_primary
                ) AS primary_poster_rows,
                COUNT(*) FILTER (
                    WHERE asset_type = 'poster' AND is_primary
                      AND payload ->> 'quality_status'
                          IN ('machine_passed', 'manual_approved')
                      AND payload ->> 'rights_status' IN ('unknown', 'restricted')
                      AND payload ->> 'source_is_primary' = 'false'
                      AND payload ->> 'rag_primary_policy' = 'one_state_row_per_movie'
                      AND content_sha256 ~ '^[0-9a-f]{64}$'
                      AND derived_object_uri =
                          'assets/posters/derived/' || movie_id || '.webp'
                      AND derived_content_sha256 ~ '^[0-9a-f]{64}$'
                      AND derived_byte_length > 0
                      AND derived_byte_length <= __MAX_POSTER_BYTES__
                      AND derived_mime_type = 'image/webp'
                ) AS approved_poster_objects,
                COUNT(*) FILTER (
                    WHERE asset_type = 'poster' AND is_primary
                      AND payload ->> 'quality_status'
                          IN ('content_conflict', 'missing', 'placeholder')
                      AND payload ->> 'rights_status' = 'unknown'
                      AND payload ->> 'source_is_primary' = 'false'
                      AND payload ->> 'rag_primary_policy' = 'one_state_row_per_movie'
                      AND derived_object_uri = ''
                      AND derived_content_sha256 = ''
                      AND derived_byte_length IS NULL
                      AND derived_mime_type = ''
                ) AS unavailable_poster_rows
            FROM media_assets
            WHERE release_id = %s
            """.replace("__MAX_POSTER_BYTES__", str(_MAX_POSTER_BYTES)),
            (release_id,),
        )
        row = cursor.fetchone()
        if row is None or len(row) != 3:
            raise RagDatabaseError("poster semantic counts are unavailable")
        return int(row[0]), int(row[1]), int(row[2])

    @staticmethod
    def _query_readiness_stats(
        cursor: Any, release_id: str
    ) -> tuple[ReleaseStats, FacetStats, tuple[int, int, int]]:
        cursor.execute(
            """
            SELECT
                (SELECT COUNT(*) FROM movies
                 WHERE release_id = target.release_id) AS readiness_movies,
                (SELECT COUNT(*) FROM media_assets
                 WHERE release_id = target.release_id) AS readiness_assets,
                (SELECT COUNT(*) FROM document_chunks
                 WHERE release_id = target.release_id
                   AND passage_kind = 'metadata') AS readiness_metadata_passages,
                (SELECT COUNT(*) FROM movie_documents
                 WHERE release_id = target.release_id) AS readiness_documents,
                (SELECT COUNT(*) FROM document_chunks
                 WHERE release_id = target.release_id
                   AND passage_kind = 'pdf') AS readiness_pdf_passages,
                (SELECT COUNT(*) FROM document_chunks
                 WHERE release_id = target.release_id
                   AND embedding IS NOT NULL) AS readiness_embeddings,
                (SELECT COUNT(*) FROM movie_facets
                 WHERE release_id = target.release_id) AS readiness_facets,
                (SELECT COUNT(*) FROM movie_facets
                 WHERE release_id = target.release_id AND tier = 'S') AS readiness_tier_s,
                (SELECT COUNT(*) FROM movie_facets
                 WHERE release_id = target.release_id AND tier = 'A') AS readiness_tier_a,
                (SELECT COUNT(*) FROM movie_facets
                 WHERE release_id = target.release_id AND tier = 'B') AS readiness_tier_b,
                (SELECT COUNT(*) FROM movie_facets
                 WHERE release_id = target.release_id AND pilot_movie) AS readiness_pilots,
                (SELECT COUNT(*) FROM media_assets
                 WHERE release_id = target.release_id
                   AND asset_type = 'poster' AND is_primary) AS readiness_primary_posters,
                (SELECT COUNT(*) FROM media_assets
                 WHERE release_id = target.release_id
                   AND asset_type = 'poster' AND is_primary
                   AND payload ->> 'quality_status' IN ('machine_passed', 'manual_approved')
                   AND payload ->> 'rights_status' IN ('unknown', 'restricted')
                   AND payload ->> 'source_is_primary' = 'false'
                   AND payload ->> 'rag_primary_policy' = 'one_state_row_per_movie'
                   AND content_sha256 ~ '^[0-9a-f]{64}$'
                   AND derived_object_uri = 'assets/posters/derived/' || movie_id || '.webp'
                   AND derived_content_sha256 ~ '^[0-9a-f]{64}$'
                   AND derived_byte_length > 0
                   AND derived_byte_length <= __MAX_POSTER_BYTES__
                   AND derived_mime_type = 'image/webp') AS readiness_approved_posters,
                (SELECT COUNT(*) FROM media_assets
                 WHERE release_id = target.release_id
                   AND asset_type = 'poster' AND is_primary
                   AND payload ->> 'quality_status'
                       IN ('content_conflict', 'missing', 'placeholder')
                   AND payload ->> 'rights_status' = 'unknown'
                   AND payload ->> 'source_is_primary' = 'false'
                   AND payload ->> 'rag_primary_policy' = 'one_state_row_per_movie'
                   AND derived_object_uri = ''
                   AND derived_content_sha256 = ''
                   AND derived_byte_length IS NULL
                   AND derived_mime_type = '') AS readiness_unavailable_posters
            FROM (VALUES (%s::text)) AS target(release_id)
            """.replace("__MAX_POSTER_BYTES__", str(_MAX_POSTER_BYTES)),
            (release_id,),
        )
        row = cursor.fetchone()
        if row is None or len(row) != 14:
            raise RagDatabaseError("query readiness statistics are unavailable")
        values = tuple(int(value) for value in row)
        return (
            ReleaseStats(*values[:6]),
            FacetStats(*values[6:11]),
            cast(tuple[int, int, int], values[11:14]),
        )

    @staticmethod
    def _assert_counts(
        expected_counts: tuple[int, int, int, int, int],
        observed_counts: tuple[int, int, int, int, int],
    ) -> None:
        for label, expected, observed in zip(
            _COUNT_LABELS, expected_counts, observed_counts, strict=True
        ):
            if observed != expected:
                raise RagDatabaseError(f"{label}: expected {expected}, got {observed}")

    @classmethod
    def _validate_release_contract(cls, contract: RagReleaseContract) -> None:
        if not isinstance(contract.rag_release_id, str) or not contract.rag_release_id.strip():
            raise RagDatabaseError("release ID must be non-empty")
        if contract.embedding_dimension != 768:
            raise RagDatabaseError("embedding dimension must be 768")
        expected_counts = cls._expected_counts(contract)
        if any(value < 1 for value in expected_counts[:3]):
            raise RagDatabaseError("verified release counts are invalid")
        count_values = tuple(
            getattr(contract.counts, field.name) for field in fields(RagReleaseCounts)
        )
        if any(
            not isinstance(value, int) or isinstance(value, bool) or value < 0
            for value in count_values
        ):
            raise RagDatabaseError("verified release counts are invalid")
        if (
            contract.counts.facet_count != contract.counts.movies
            or contract.counts.tier_s_count
            + contract.counts.tier_a_count
            + contract.counts.tier_b_count
            != contract.counts.facet_count
            or contract.counts.pilot_count > contract.counts.facet_count
            or contract.counts.primary_poster_rows != contract.counts.poster_rows
            or contract.counts.approved_poster_objects
            + contract.counts.unavailable_poster_rows
            != contract.counts.primary_poster_rows
        ):
            raise RagDatabaseError("verified release count relationships are invalid")
        if (
            not contract.schema_version
            or _SHA256.fullmatch(contract.parent_release_manifest_sha256) is None
            or _SHA256.fullmatch(contract.manifest_sha256) is None
            or _SHA256.fullmatch(contract.bundle_sha256) is None
            or _SHA256.fullmatch(contract.derived_inventory_sha256) is None
            or not contract.document_embedding_profile
            or not contract.embedding_model
            or not contract.generation_model
            or not contract.text_extraction_profile
            or not contract.access_mode
            or (
                contract.relevance_policy_sha256 is not None
                and _SHA256.fullmatch(contract.relevance_policy_sha256) is None
            )
            or (
                contract.poster_authority_sha256 is not None
                and _SHA256.fullmatch(contract.poster_authority_sha256) is None
            )
        ):
            raise RagDatabaseError("verified release contract is invalid")

    @classmethod
    def _contract_from_payload(cls, value: object) -> RagReleaseContract:
        if not isinstance(value, Mapping):
            raise RagDatabaseError("stored release contract is invalid")
        payload = dict(value)
        expected_fields = {field.name for field in fields(RagReleaseContract)}
        if set(payload) != expected_fields:
            raise RagDatabaseError("stored release contract is invalid")
        counts_value = payload.get("counts")
        if not isinstance(counts_value, Mapping):
            raise RagDatabaseError("stored release contract is invalid")
        counts_payload = dict(counts_value)
        count_fields = {field.name for field in fields(RagReleaseCounts)}
        if set(counts_payload) != count_fields:
            raise RagDatabaseError("stored release contract is invalid")

        def count(name: str) -> int:
            observed = counts_payload[name]
            if (
                not isinstance(observed, int)
                or isinstance(observed, bool)
                or observed < 0
            ):
                raise RagDatabaseError("stored release contract is invalid")
            return observed

        def text(name: str) -> str:
            observed = payload[name]
            if not isinstance(observed, str) or not observed:
                raise RagDatabaseError("stored release contract is invalid")
            return observed

        def optional_sha(name: str) -> str | None:
            observed = payload[name]
            if observed is None:
                return None
            if not isinstance(observed, str) or _SHA256.fullmatch(observed) is None:
                raise RagDatabaseError("stored release contract is invalid")
            return observed

        counts = RagReleaseCounts(
            movies=count("movies"),
            facet_count=count("facet_count"),
            tier_s_count=count("tier_s_count"),
            tier_a_count=count("tier_a_count"),
            tier_b_count=count("tier_b_count"),
            pilot_count=count("pilot_count"),
            poster_rows=count("poster_rows"),
            primary_poster_rows=count("primary_poster_rows"),
            approved_poster_objects=count("approved_poster_objects"),
            unavailable_poster_rows=count("unavailable_poster_rows"),
            derived_poster_bytes=count("derived_poster_bytes"),
            metadata_passages=count("metadata_passages"),
            documents=count("documents"),
            pdf_passages=count("pdf_passages"),
        )
        embedding_dimension = payload["embedding_dimension"]
        if not isinstance(embedding_dimension, int) or isinstance(
            embedding_dimension, bool
        ):
            raise RagDatabaseError("stored release contract is invalid")
        contract = RagReleaseContract(
            schema_version=text("schema_version"),
            rag_release_id=text("rag_release_id"),
            parent_release_manifest_sha256=text("parent_release_manifest_sha256"),
            manifest_sha256=text("manifest_sha256"),
            bundle_sha256=text("bundle_sha256"),
            derived_inventory_sha256=text("derived_inventory_sha256"),
            counts=counts,
            embedding_model=text("embedding_model"),
            embedding_dimension=embedding_dimension,
            generation_model=text("generation_model"),
            text_extraction_profile=text("text_extraction_profile"),
            document_embedding_profile=text("document_embedding_profile"),
            relevance_policy_sha256=optional_sha("relevance_policy_sha256"),
            poster_authority_sha256=optional_sha("poster_authority_sha256"),
            access_mode=text("access_mode"),
        )
        cls._validate_release_contract(contract)
        return contract

    @classmethod
    def _contract_json(cls, contract: RagReleaseContract) -> str:
        return cls._canonical_json(asdict(contract))

    @staticmethod
    def _expected_counts(
        contract: RagReleaseContract,
    ) -> tuple[int, int, int, int, int]:
        counts = contract.counts
        values = (
            counts.movies,
            counts.poster_rows,
            counts.metadata_passages,
            counts.documents,
            counts.pdf_passages,
        )
        if any(
            not isinstance(value, int) or isinstance(value, bool) or value < 0
            for value in values
        ):
            raise RagDatabaseError("verified release counts are invalid")
        return values

    @classmethod
    def _stored_contract_from_row(
        cls, release_id: str, row: Sequence[object]
    ) -> _StoredReleaseContract:
        if len(row) != 14:
            raise RagDatabaseError("stored release contract is invalid")
        status = row[0]
        expected_counts = tuple(row[1:6])
        model, dimension, generation_model, access_mode = row[6:10]
        manifest_sha256, profile, contract_payload, embeddings = row[10:14]
        if status not in {"loading", "active", "failed"}:
            raise RagDatabaseError("stored release contract is invalid")
        if any(
            not isinstance(value, int) or isinstance(value, bool) or value < 0
            for value in (*expected_counts, embeddings)
        ):
            raise RagDatabaseError("stored release contract is invalid")
        if (
            not isinstance(model, str)
            or not model
            or not isinstance(dimension, int)
            or isinstance(dimension, bool)
            or not isinstance(generation_model, str)
            or not generation_model
            or not isinstance(access_mode, str)
            or not access_mode
            or (manifest_sha256 is not None and not isinstance(manifest_sha256, str))
            or (profile is not None and not isinstance(profile, str))
        ):
            raise RagDatabaseError("stored release contract is invalid")
        identities = (manifest_sha256, profile, contract_payload)
        if identities == (None, None, None):
            persisted_contract = None
        else:
            if (
                not isinstance(manifest_sha256, str)
                or _SHA256.fullmatch(manifest_sha256) is None
                or not isinstance(profile, str)
                or not profile
            ):
                raise RagDatabaseError("stored release contract is invalid")
            if contract_payload is None:
                persisted_contract = None
            else:
                persisted_contract = cls._contract_from_payload(contract_payload)
        return _StoredReleaseContract(
            release_id=release_id,
            status=cast(str, status),
            expected_counts=cast(tuple[int, int, int, int, int], expected_counts),
            embedding_model=model,
            embedding_dimension=dimension,
            generation_model=generation_model,
            access_mode=access_mode,
            manifest_sha256=manifest_sha256,
            document_embedding_profile=profile,
            persisted_contract=persisted_contract,
            embeddings=cast(int, embeddings),
        )

    @classmethod
    def _assert_stored_contract_matches(
        cls,
        contract: RagReleaseContract,
        stored: _StoredReleaseContract,
        *,
        allow_legacy_identity: bool = False,
        allow_transitional_contract: bool = False,
    ) -> None:
        expected = (
            contract.rag_release_id,
            cls._expected_counts(contract),
            contract.embedding_model,
            contract.embedding_dimension,
            contract.generation_model,
            contract.access_mode,
        )
        observed = (
            stored.release_id,
            stored.expected_counts,
            stored.embedding_model,
            stored.embedding_dimension,
            stored.generation_model,
            stored.access_mode,
        )
        if observed != expected:
            raise RagDatabaseError("release contract is immutable")
        identities = (
            stored.manifest_sha256,
            stored.document_embedding_profile,
            stored.persisted_contract,
        )
        if identities == (None, None, None):
            if allow_legacy_identity:
                return
            raise RagDatabaseError("legacy release requires verified bundle claim")
        if stored.persisted_contract is None:
            if (
                stored.manifest_sha256 != contract.manifest_sha256
                or stored.document_embedding_profile
                != contract.document_embedding_profile
            ):
                raise RagDatabaseError("release contract is immutable")
            if allow_transitional_contract:
                return
            raise RagDatabaseError("transitional release requires complete contract claim")
        if (
            stored.manifest_sha256 != contract.manifest_sha256
            or stored.document_embedding_profile != contract.document_embedding_profile
            or stored.persisted_contract != contract
        ):
            raise RagDatabaseError("release contract is immutable")

    @staticmethod
    def _non_negative_count(value: object, label: str) -> int:
        if not isinstance(value, int) or isinstance(value, bool) or value < 0:
            raise RagDatabaseError(f"{label} is invalid")
        return value

    @classmethod
    def _active_bundle_records(
        cls, records: Iterable[Mapping[str, object]]
    ) -> dict[tuple[str, str], str]:
        expected: dict[tuple[str, str], str] = {}
        for record in records:
            kind = record.get("record_kind")
            if kind == "facet":
                continue
            payload_json = cls._canonical_json(record)
            payload = json.loads(payload_json)
            if kind == "movie":
                record_id = cls._text(record, "movie_id")
                body = {
                    "movie_id": record_id,
                    "payload_sha256": hashlib.sha256(payload_json.encode("utf-8")).hexdigest(),
                    "payload": payload,
                }
            elif kind == "poster":
                record_id = cls._text(record, "asset_id")
                (
                    movie_id,
                    asset_type,
                    content_sha256,
                    original_object_uri,
                    derived_object_uri,
                    derived_content_sha256,
                    derived_byte_length,
                    derived_mime_type,
                    is_primary,
                ) = cls._poster_values(record)
                body = {
                    "asset_id": record_id,
                    "movie_id": movie_id,
                    "asset_type": asset_type,
                    "content_sha256": content_sha256,
                    "original_object_uri": original_object_uri,
                    "derived_object_uri": derived_object_uri,
                    "derived_content_sha256": derived_content_sha256,
                    "derived_byte_length": derived_byte_length,
                    "derived_mime_type": derived_mime_type,
                    "is_primary": is_primary,
                    "payload": payload,
                }
            elif kind == "document":
                record_id = cls._text(record, "document_id")
                body = {
                    "document_id": record_id,
                    "movie_id": cls._text(record, "movie_id"),
                    "content_sha256": cls._text(record, "source_sha256"),
                    "source_filename": cls._text(record, "source_filename"),
                    "rights_status": cls._text(record, "rights_status"),
                    "quality_status": cls._text(record, "quality_status"),
                    "payload": payload,
                }
            elif kind == "passage":
                record_id = cls._text(record, "passage_id")
                body = {
                    "passage_id": record_id,
                    "movie_id": cls._text(record, "movie_id"),
                    "document_id": str(record.get("document_id", "")) or None,
                    "passage_kind": cls._text(record, "passage_kind"),
                    "page_number": cls._integer(record, "page_number"),
                    "body": cls._text(record, "body"),
                    "content_sha256": cls._text(record, "content_sha256"),
                }
            else:
                raise RagDatabaseError("unsupported bundle record kind")
            key = (kind, record_id)
            if key in expected:
                raise RagDatabaseError("active bundle record identity is not unique")
            expected[key] = cls._canonical_json(body)
        if not expected:
            raise RagDatabaseError("active bundle records are missing")
        return expected

    @classmethod
    def _assert_active_facets_match(
        cls,
        cursor: Any,
        release_id: str,
        expected: Mapping[str, Mapping[str, object]],
    ) -> None:
        cursor.execute(
            """
            SELECT movie_id, content_sha256, tier, tier_reason, human_review,
                   pilot_movie, pilot_evidence
            FROM movie_facets
            WHERE release_id = %s
            ORDER BY movie_id
            FOR SHARE
            """,
            (release_id,),
        )
        stored: dict[str, tuple[str, dict[str, object]]] = {}
        for row in cursor.fetchall():
            if (
                len(row) != 7
                or not isinstance(row[0], str)
                or not isinstance(row[1], str)
                or row[0] in stored
            ):
                raise RagDatabaseError("stored facet record is invalid")
            stored[row[0]] = (
                row[1],
                {
                    "movie_id": row[0],
                    "tier": row[2],
                    "tier_reason": row[3],
                    "human_review": row[4],
                    "pilot_movie": row[5],
                    "pilot_evidence": row[6],
                },
            )
        if set(stored) != set(expected):
            raise RagDatabaseError("facet coverage conflict")
        for movie_id, (content_sha256, stored_body) in stored.items():
            expected_facet = expected[movie_id]
            expected_body = cls._facet_body(expected_facet)
            try:
                stored_hash = cls._facet_content_sha256(stored_body)
            except (TypeError, ValueError):
                raise RagDatabaseError("facet content conflict") from None
            if (
                expected_facet["content_sha256"] != content_sha256
                or stored_hash != content_sha256
                or stored_body != expected_body
            ):
                raise RagDatabaseError("facet content conflict")

    @staticmethod
    def _canonical_json(value: object) -> str:
        return json.dumps(
            value,
            ensure_ascii=False,
            sort_keys=True,
            separators=(",", ":"),
            allow_nan=False,
        )

    @staticmethod
    def _poster_expectations(contract: RagReleaseContract) -> tuple[int, int, int]:
        return (
            contract.counts.primary_poster_rows,
            contract.counts.approved_poster_objects,
            contract.counts.unavailable_poster_rows,
        )

    @staticmethod
    def _assert_bundle_poster_semantics(
        counts: tuple[int, int, int],
        expected_counts: tuple[int, int, int] = _REQUIRED_POSTER_SEMANTICS,
    ) -> None:
        for label, expected, observed in zip(
            _POSTER_SEMANTIC_LABELS, expected_counts, counts, strict=True
        ):
            if observed != expected:
                raise RagDatabaseError(f"{label}: expected {expected}, got {observed}")

    @staticmethod
    def _facet_records(
        records: Iterable[Mapping[str, object]],
    ) -> dict[str, dict[str, object]]:
        facets: dict[str, dict[str, object]] = {}
        for record in records:
            if record.get("record_kind") != "facet":
                continue
            movie_id = RagRepository._text(record, "movie_id")
            if movie_id in facets:
                raise RagDatabaseError("facet movie membership is not unique")
            tier = RagRepository._text(record, "tier")
            if tier not in {"S", "A", "B"}:
                raise RagDatabaseError("facet tier is invalid")
            pilot_movie = record.get("pilot_movie")
            if not isinstance(pilot_movie, bool):
                raise RagDatabaseError("facet pilot flag is invalid")
            content_sha256 = RagRepository._text(record, "content_sha256")
            if not _SHA256.fullmatch(content_sha256):
                raise RagDatabaseError("facet content hash is invalid")
            pilot_evidence = record.get("pilot_evidence")
            if pilot_movie:
                if not isinstance(pilot_evidence, Mapping):
                    raise RagDatabaseError("facet pilot evidence is invalid")
                normalized_pilot_evidence: dict[str, object] | None = dict(pilot_evidence)
            else:
                if pilot_evidence is not None:
                    raise RagDatabaseError("facet has unexpected pilot evidence")
                normalized_pilot_evidence = None
            facet = {
                "movie_id": movie_id,
                "tier": tier,
                "tier_reason": RagRepository._text(record, "tier_reason"),
                "human_review": RagRepository._text(record, "human_review"),
                "pilot_movie": pilot_movie,
                "pilot_evidence": normalized_pilot_evidence,
            }
            if content_sha256 != RagRepository._facet_content_sha256(facet):
                raise RagDatabaseError("facet content hash is invalid")
            facets[movie_id] = {"content_sha256": content_sha256, **facet}
        if not facets:
            raise RagDatabaseError("facet records are missing")
        return facets

    @staticmethod
    def _facet_body(facet: Mapping[str, object]) -> dict[str, object]:
        return {
            key: facet[key]
            for key in (
                "movie_id",
                "tier",
                "tier_reason",
                "human_review",
                "pilot_movie",
                "pilot_evidence",
            )
        }

    @staticmethod
    def _facet_content_sha256(facet: Mapping[str, object]) -> str:
        body = RagRepository._facet_body(facet)
        canonical = json.dumps(
            body, ensure_ascii=False, sort_keys=True, separators=(",", ":")
        ).encode("utf-8")
        return hashlib.sha256(canonical).hexdigest()

    @staticmethod
    def _facet_stats_from_records(records: Iterable[Mapping[str, object]]) -> FacetStats:
        rows = list(records)
        return FacetStats(
            len(rows),
            sum(row["tier"] == "S" for row in rows),
            sum(row["tier"] == "A" for row in rows),
            sum(row["tier"] == "B" for row in rows),
            sum(row["pilot_movie"] is True for row in rows),
        )

    @staticmethod
    def _facet_expectations(contract: RagReleaseContract) -> FacetStats:
        return FacetStats(
            contract.counts.facet_count,
            contract.counts.tier_s_count,
            contract.counts.tier_a_count,
            contract.counts.tier_b_count,
            contract.counts.pilot_count,
        )

    def _lock_active_release_with_facets(
        self, cursor: Any, release_id: str
    ) -> _StoredReleaseContract:
        """Bind every serving query to one active release and its persisted facet census."""
        stored = self._lock_active_release(cursor, release_id)
        persisted_contract = stored.persisted_contract
        if persisted_contract is None:  # Defensive; the release lock already rejects this state.
            raise RagDatabaseError("active release contract is unavailable")
        self._assert_facet_counts(
            self._facet_stats(cursor, release_id),
            self._facet_expectations(persisted_contract),
        )
        return stored

    @staticmethod
    def _assert_facet_counts(
        stats: FacetStats,
        expected_stats: FacetStats | None = None,
    ) -> None:
        if expected_stats is None:
            expected_stats = FacetStats(*_REQUIRED_FACET_COUNTS)
        for label, expected, observed in zip(
            _FACET_COUNT_LABELS,
            expected_stats.__dict__.values(),
            stats.__dict__.values(),
            strict=True,
        ):
            if observed != expected:
                raise RagDatabaseError(f"{label}: expected {expected}, got {observed}")

    @staticmethod
    def _facet_stats(cursor: Any, release_id: str) -> FacetStats:
        cursor.execute(
            """
            SELECT COUNT(*) AS total,
                   COUNT(*) FILTER (WHERE tier = 'S') AS tier_s,
                   COUNT(*) FILTER (WHERE tier = 'A') AS tier_a,
                   COUNT(*) FILTER (WHERE tier = 'B') AS tier_b,
                   COUNT(*) FILTER (WHERE pilot_movie) AS pilots
            FROM movie_facets
            WHERE release_id = %s
            """,
            (release_id,),
        )
        row = cursor.fetchone()
        if row is None or len(row) != 5:
            raise RagDatabaseError("facet statistics are unavailable")
        return FacetStats(*(int(value) for value in row))

    @staticmethod
    def _validate_recommendation_plan(plan: RecommendationPlan) -> None:
        if not isinstance(plan.requested_count, int) or isinstance(plan.requested_count, bool):
            raise RagDatabaseError("recommendation count must be an integer")
        if not 1 <= plan.requested_count <= 50:
            raise RagDatabaseError("recommendation count must be between 1 and 50")
        if plan.tier is not None and plan.tier not in {"S", "A", "B"}:
            raise RagDatabaseError("recommendation tier is invalid")
        for genres in (plan.genres, plan.genres_all, plan.genres_any):
            if not isinstance(genres, tuple) or any(
                not isinstance(genre, str) or not genre.strip() for genre in genres
            ):
                raise RagDatabaseError("recommendation genres are invalid")
            if len(set(genres)) != len(genres):
                raise RagDatabaseError("recommendation genres are invalid")
        if not isinstance(plan.title_terms_any, tuple) or any(
            not isinstance(term, str)
            or not term.strip()
            for term in plan.title_terms_any
        ):
            raise RagDatabaseError("recommendation title terms are invalid")
        title_policy = (
            controlled_title_policy(plan.title_terms_any)
            if plan.title_terms_any
            else None
        )
        if plan.title_terms_any and title_policy is None:
            raise RagDatabaseError("recommendation title terms are invalid")
        if title_policy is not None and (
            plan.genres_all,
            plan.genres_any,
        ) != title_policy:
            raise RagDatabaseError("recommendation title proxy constraints are invalid")
        if len(set(plan.title_terms_any)) != len(plan.title_terms_any):
            raise RagDatabaseError("recommendation title terms are invalid")
        if bool(plan.title_terms_any) != (plan.scope_label == "title_keyword_proxy"):
            raise RagDatabaseError("recommendation title terms require title proxy scope")
        if any(not isinstance(movie_id, str) or not movie_id for movie_id in plan.excluded_movie_ids):
            raise RagDatabaseError("recommendation exclusions are invalid")
        if plan.person_name is None:
            if plan.person_role is not None or plan.person_exact_names:
                raise RagDatabaseError("recommendation person role requires a person")
        elif not isinstance(plan.person_name, str) or not plan.person_name.strip():
            raise RagDatabaseError("recommendation person is invalid")
        if not isinstance(plan.person_exact_names, tuple) or any(
            not isinstance(name, str) or not name.strip()
            for name in plan.person_exact_names
        ):
            raise RagDatabaseError("recommendation person exact names are invalid")
        if len(set(plan.person_exact_names)) != len(plan.person_exact_names):
            raise RagDatabaseError("recommendation person exact names are invalid")
        if (
            plan.person_exact_names
            and plan.person_name not in plan.person_exact_names
        ):
            raise RagDatabaseError("recommendation person exact names are invalid")
        if plan.person_role not in {None, "actor", "director"}:
            raise RagDatabaseError("recommendation person role is invalid")
        if plan.credit_group_id is None:
            if plan.credit_role is not None or plan.credit_exact_names:
                raise RagDatabaseError("recommendation credit group is invalid")
        elif not isinstance(plan.credit_group_id, str) or not plan.credit_group_id.strip():
            raise RagDatabaseError("recommendation credit group is invalid")
        if plan.credit_group_id is not None:
            controlled = controlled_credit_group(plan.credit_group_id)
            if (
                controlled is None
                or (plan.credit_role, plan.credit_exact_names) != controlled
                or plan.person_name is not None
            ):
                raise RagDatabaseError("recommendation credit group is invalid")
        if plan.scope_label not in {
            "metadata",
            "title_keyword_proxy",
            "controlled_credit_group",
            "family_genre_proxy",
        }:
            raise RagDatabaseError("recommendation scope is invalid")
        if (plan.credit_group_id is not None) != (
            plan.scope_label == "controlled_credit_group"
        ):
            raise RagDatabaseError("recommendation credit group scope is invalid")
        if plan.scope_label == "family_genre_proxy" and (
            plan.genres_all != ("家庭",) or plan.genres_any
        ):
            raise RagDatabaseError("recommendation family genre scope is invalid")
        for year in (plan.year_from, plan.year_to):
            if year is not None and (
                not isinstance(year, int) or isinstance(year, bool) or not 1888 <= year <= 2100
            ):
                raise RagDatabaseError("recommendation year is invalid")
        if plan.year_from is not None and plan.year_to is not None and plan.year_from > plan.year_to:
            raise RagDatabaseError("recommendation year range is invalid")

    @staticmethod
    def _plan_person_exact_names(plan: RecommendationPlan) -> tuple[str, ...]:
        if plan.person_name is None:
            return ()
        return plan.person_exact_names or (plan.person_name,)

    @classmethod
    def _plan_credit_constraint(
        cls, plan: RecommendationPlan
    ) -> tuple[tuple[str, ...], PersonRole | None]:
        if plan.credit_group_id is not None:
            return plan.credit_exact_names, plan.credit_role
        return cls._plan_person_exact_names(plan), plan.person_role

    @classmethod
    def _plan_genres(
        cls, plan: RecommendationPlan
    ) -> tuple[tuple[str, ...], tuple[str, ...], tuple[str, ...]]:
        explicit_genres = cls._normalized_values(plan.genres)
        genres_all = cls._normalized_values(plan.genres_all)
        genres_any = cls._normalized_values(plan.genres_any)
        return explicit_genres, genres_all, genres_any

    @staticmethod
    def _normalized_values(values: Sequence[str]) -> tuple[str, ...]:
        return tuple(dict.fromkeys(value.strip() for value in values))

    @classmethod
    def _recommendation_search(
        cls,
        rows: Sequence[Sequence[object]],
        *,
        expected_passage_kind: str,
    ) -> RecommendationSearch:
        if not rows:
            return RecommendationSearch((), 0)
        total_matches = cls._recommendation_total(rows)
        records: list[dict[str, object]] = []
        seen_movies: set[object] = set()
        for row in rows:
            if (
                len(row) != 17
                or not isinstance(row[7], Mapping)
                or row[2] != expected_passage_kind
                or row[1] in seen_movies
            ):
                raise RagDatabaseError("recommendation search result is invalid")
            seen_movies.add(row[1])
            movie = dict(row[7])
            movie.update(
                {
                    "tier": row[8],
                    "tier_reason": row[9],
                    "human_review": row[10],
                    "pilot_movie": row[11],
                    "pilot_evidence": row[12],
                    "facet_content_sha256": row[13],
                }
            )
            records.append(
                {
                    "passage_id": row[0],
                    "movie_id": row[1],
                    "passage_kind": row[2],
                    "body": row[3],
                    "page_number": row[4],
                    "document_id": row[5],
                    "source_filename": row[6],
                    "movie": movie,
                    "poster_available": row[14],
                    "distance": row[15],
                }
            )
        return RecommendationSearch(tuple(records), total_matches)

    @staticmethod
    def _recommendation_total(rows: Sequence[Sequence[object]]) -> int:
        totals = {row[16] for row in rows if len(row) == 17}
        if len(totals) != 1:
            raise RagDatabaseError("recommendation total match count is invalid")
        total = next(iter(totals))
        if not isinstance(total, int) or isinstance(total, bool) or total < len(rows):
            raise RagDatabaseError("recommendation total match count is invalid")
        return total

    @staticmethod
    def _lock_loading_release(cursor: Any, release_id: str) -> None:
        cursor.execute(
            """
            SELECT release_id
            FROM rag_releases
            WHERE release_id = %s AND status = 'loading'
            FOR UPDATE
            """,
            (release_id,),
        )
        row = cursor.fetchone()
        if row is None:
            raise RagDatabaseError("release is not loading")

    @classmethod
    def _lock_active_release(
        cls, cursor: Any, release_id: str
    ) -> _StoredReleaseContract:
        cursor.execute(
            """
            SELECT release.status,
                   release.expected_movies, release.expected_assets,
                   release.expected_metadata_passages,
                   release.expected_documents, release.expected_pdf_passages,
                   release.embedding_model, release.embedding_dimension,
                   release.generation_model, release.access_mode,
                   release.manifest_sha256, release.document_embedding_profile,
                   release.contract_json,
                   (SELECT COUNT(*) FROM document_chunks AS chunk
                    WHERE chunk.release_id = release.release_id
                      AND chunk.embedding IS NOT NULL) AS embeddings
            FROM rag_releases AS release
            WHERE release.release_id = %s AND release.status = 'active'
            FOR SHARE
            """,
            (release_id,),
        )
        row = cursor.fetchone()
        if row is None:
            raise RagDatabaseError("active release is unavailable")
        try:
            contract = cls._stored_contract_from_row(release_id, row)
        except RagDatabaseError:
            raise RagDatabaseError("active release contract is unavailable") from None
        if (
            contract.status != "active"
            or contract.embedding_dimension != 768
            or contract.persisted_contract is None
        ):
            raise RagDatabaseError("active release contract is unavailable")
        cls._assert_stored_contract_matches(contract.persisted_contract, contract)
        return contract

    @classmethod
    def _lock_loading_embedding_contract(
        cls,
        cursor: Any,
        release_id: str,
        embedding_model: str,
        embedding_dimension: int,
    ) -> _StoredReleaseContract:
        if not embedding_model or embedding_dimension != 768:
            raise RagDatabaseError("release embedding contract is invalid")
        cursor.execute(
            """
            SELECT release.status,
                   release.expected_movies, release.expected_assets,
                   release.expected_metadata_passages,
                   release.expected_documents, release.expected_pdf_passages,
                   release.embedding_model, release.embedding_dimension,
                   release.generation_model, release.access_mode,
                   release.manifest_sha256, release.document_embedding_profile,
                   release.contract_json,
                   (SELECT COUNT(*) FROM document_chunks AS chunk
                    WHERE chunk.release_id = release.release_id
                      AND chunk.embedding IS NOT NULL) AS embeddings
            FROM rag_releases AS release
            WHERE release.release_id = %s AND release.embedding_model = %s
                  AND release.embedding_dimension = %s AND release.status = 'loading'
            FOR UPDATE
            """,
            (release_id, embedding_model, embedding_dimension),
        )
        row = cursor.fetchone()
        if row is None:
            raise RagDatabaseError("release is not loading or embedding contract changed")
        try:
            contract = cls._stored_contract_from_row(release_id, row)
        except RagDatabaseError:
            raise RagDatabaseError(
                "release is not loading or embedding contract changed"
            ) from None
        if contract.persisted_contract is None:
            raise RagDatabaseError("release is not loading or embedding contract changed")
        cls._assert_stored_contract_matches(contract.persisted_contract, contract)
        return contract

    @classmethod
    def _lock_reuse_contracts(
        cls,
        cursor: Any,
        target_release_id: str,
        source_release_id: str,
        *,
        allow_active_target: bool = False,
    ) -> None:
        cursor.execute(
            """
            SELECT release_id, status, embedding_model, embedding_dimension,
                   document_embedding_profile
            FROM rag_releases
            WHERE release_id IN (%s, %s)
            ORDER BY release_id
            FOR UPDATE
            """,
            (target_release_id, source_release_id),
        )
        rows = cursor.fetchall()
        if len(rows) != 2:
            raise RagDatabaseError("embedding reuse contract is invalid")
        contracts: dict[str, tuple[object, ...]] = {}
        for row in rows:
            if len(row) != 5 or not isinstance(row[0], str) or row[0] in contracts:
                raise RagDatabaseError("embedding reuse contract is invalid")
            contracts[row[0]] = tuple(row[1:])
        source = contracts.get(source_release_id)
        target = contracts.get(target_release_id)
        if source is None or target is None:
            raise RagDatabaseError("embedding reuse contract is invalid")
        source_status, source_model, source_dimension, source_profile = source
        target_status, target_model, target_dimension, target_profile = target
        allowed_target_statuses = {"loading", "active"} if allow_active_target else {"loading"}
        if (
            source_status != "active"
            or target_status not in allowed_target_statuses
            or not isinstance(source_model, str)
            or not source_model
            or source_model != target_model
            or source_dimension != 768
            or target_dimension != 768
            or not isinstance(source_profile, str)
            or not source_profile
            or source_profile != target_profile
        ):
            raise RagDatabaseError("embedding reuse contract is invalid")

    def _upsert_record(self, cursor: Any, release_id: str, record: Mapping[str, object]) -> None:
        kind = record.get("record_kind")
        payload = json.dumps(record, ensure_ascii=False, sort_keys=True, separators=(",", ":"))
        if kind == "movie":
            payload_sha256 = hashlib.sha256(payload.encode("utf-8")).hexdigest()
            cursor.execute(
                """
                INSERT INTO movies (release_id, movie_id, payload_sha256, payload)
                VALUES (%s, %s, %s, %s::jsonb)
                ON CONFLICT (release_id, movie_id) DO UPDATE SET
                    payload_sha256 = EXCLUDED.payload_sha256,
                    payload = EXCLUDED.payload
                """,
                (release_id, self._text(record, "movie_id"), payload_sha256, payload),
            )
        elif kind == "poster":
            poster = self._poster_values(record)
            cursor.execute(
                """
                INSERT INTO media_assets (
                    release_id, asset_id, movie_id, asset_type, content_sha256,
                    original_object_uri, derived_object_uri, derived_content_sha256,
                    derived_byte_length, derived_mime_type, is_primary, payload
                ) VALUES (%s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s::jsonb)
                ON CONFLICT (release_id, asset_id) DO UPDATE SET
                    original_object_uri = EXCLUDED.original_object_uri,
                    derived_object_uri = EXCLUDED.derived_object_uri,
                    derived_content_sha256 = EXCLUDED.derived_content_sha256,
                    derived_byte_length = EXCLUDED.derived_byte_length,
                    derived_mime_type = EXCLUDED.derived_mime_type,
                    is_primary = EXCLUDED.is_primary,
                    payload = EXCLUDED.payload,
                    content_sha256 = EXCLUDED.content_sha256
                """,
                (
                    release_id,
                    self._text(record, "asset_id"),
                    *poster,
                    payload,
                ),
            )
        elif kind == "document":
            cursor.execute(
                """
                INSERT INTO movie_documents (
                    release_id, document_id, movie_id, content_sha256, source_filename,
                    rights_status, quality_status, payload
                ) VALUES (%s, %s, %s, %s, %s, %s, %s, %s::jsonb)
                ON CONFLICT (release_id, document_id) DO UPDATE SET
                    source_filename = EXCLUDED.source_filename,
                    rights_status = EXCLUDED.rights_status,
                    quality_status = EXCLUDED.quality_status,
                    payload = EXCLUDED.payload,
                    content_sha256 = EXCLUDED.content_sha256
                """,
                (
                    release_id,
                    self._text(record, "document_id"),
                    self._text(record, "movie_id"),
                    self._text(record, "source_sha256"),
                    self._text(record, "source_filename"),
                    self._text(record, "rights_status"),
                    self._text(record, "quality_status"),
                    payload,
                ),
            )
        elif kind == "passage":
            document_id = str(record.get("document_id", "")) or None
            cursor.execute(
                """
                INSERT INTO document_chunks (
                    release_id, passage_id, movie_id, document_id, passage_kind,
                    page_number, body, content_sha256
                ) VALUES (%s, %s, %s, %s, %s, %s, %s, %s)
                ON CONFLICT (release_id, passage_id) DO UPDATE SET
                    body = EXCLUDED.body,
                    content_sha256 = EXCLUDED.content_sha256
                """,
                (
                    release_id,
                    self._text(record, "passage_id"),
                    self._text(record, "movie_id"),
                    document_id,
                    self._text(record, "passage_kind"),
                    self._integer(record, "page_number"),
                    self._text(record, "body"),
                    self._text(record, "content_sha256"),
                ),
            )
        else:
            raise RagDatabaseError("unsupported bundle record kind")

    @contextmanager
    def _cursor(self) -> Iterator[Any]:
        try:
            if self._pool is not None:
                with self._pool.connection(
                    timeout=DB_POOL_CHECKOUT_TIMEOUT_SECONDS
                ) as connection, connection.cursor() as cursor:
                    yield cursor
                return
            with self.connection.transaction(), self.connection.cursor() as cursor:
                yield cursor
        except RagDatabaseError:
            raise
        except Exception as exc:
            raise RagDatabaseError("database operation failed") from exc

    @staticmethod
    def _text(record: Mapping[str, object], key: str) -> str:
        value = record.get(key)
        if not isinstance(value, str) or not value:
            raise RagDatabaseError(f"bundle record field must be non-empty text: {key}")
        return value

    @staticmethod
    def _integer(record: Mapping[str, object], key: str) -> int:
        value = record.get(key)
        if not isinstance(value, int) or isinstance(value, bool) or value < 0:
            raise RagDatabaseError(f"bundle record field must be a non-negative integer: {key}")
        return value

    @classmethod
    def _poster_values(cls, record: Mapping[str, object]) -> tuple[object, ...]:
        movie_id = cls._text(record, "movie_id")
        if record.get("asset_type", "poster") != "poster":
            raise RagDatabaseError("bundle poster asset type is invalid")
        if record.get("is_primary") is not True:
            raise RagDatabaseError("bundle poster primary state is invalid")
        quality_status = cls._text(record, "quality_status")
        content_sha256 = record.get("content_sha256")
        original_object_uri = record.get("original_object_uri", "")
        derived_object_uri = record.get("derived_object_uri", "")
        if not isinstance(content_sha256, str) or (
            content_sha256 == "" and quality_status != "missing"
        ):
            raise RagDatabaseError("bundle poster original hash is invalid")
        if content_sha256 and _SHA256.fullmatch(content_sha256) is None:
            raise RagDatabaseError("bundle poster original hash is invalid")
        if not isinstance(original_object_uri, str) or not isinstance(derived_object_uri, str):
            raise RagDatabaseError("bundle poster object identity is invalid")
        if quality_status in _APPROVED_POSTER_STATES:
            derived_content_sha256 = record.get("derived_content_sha256")
            derived_byte_length = record.get("derived_byte_length")
            derived_mime_type = record.get("derived_mime_type")
            if (
                derived_object_uri != f"assets/posters/derived/{movie_id}.webp"
                or not isinstance(derived_content_sha256, str)
                or _SHA256.fullmatch(derived_content_sha256) is None
                or not isinstance(derived_byte_length, int)
                or isinstance(derived_byte_length, bool)
                or derived_byte_length < 1
                or derived_byte_length > _MAX_POSTER_BYTES
                or derived_mime_type != "image/webp"
            ):
                raise RagDatabaseError("bundle poster derivative identity is invalid")
        elif quality_status in _UNAVAILABLE_POSTER_STATES:
            if derived_object_uri != "" or any(
                field in record
                for field in (
                    "derived_content_sha256",
                    "derived_byte_length",
                    "derived_mime_type",
                )
            ):
                raise RagDatabaseError("unavailable poster has derived identity")
            derived_content_sha256 = ""
            derived_byte_length = None
            derived_mime_type = ""
        else:
            raise RagDatabaseError("bundle poster quality state is invalid")
        return (
            movie_id,
            "poster",
            content_sha256,
            original_object_uri,
            derived_object_uri,
            derived_content_sha256,
            derived_byte_length,
            derived_mime_type,
            True,
        )

    @staticmethod
    def _vector(embedding: Sequence[float]) -> str:
        try:
            values = [float(value) for value in embedding if not isinstance(value, bool)]
        except (TypeError, ValueError) as exc:
            raise RagDatabaseError("embedding values must be numeric") from exc
        if len(values) != len(embedding) or not all(math.isfinite(value) for value in values):
            raise RagDatabaseError("embedding values must be finite numbers")
        return Vector(values).to_text()

    @staticmethod
    def _state_from_row(row: Sequence[object]) -> ReleaseState:
        if len(row) != 6:
            raise RagDatabaseError("release state is invalid")
        status, model, dimension, manifest_sha256, profile, embeddings = row
        if status not in {"loading", "active", "failed"}:
            raise RagDatabaseError("release status is invalid")
        if not isinstance(model, str) or not model:
            raise RagDatabaseError("release embedding model is invalid")
        if (
            not isinstance(dimension, int)
            or isinstance(dimension, bool)
            or not isinstance(embeddings, int)
            or isinstance(embeddings, bool)
            or embeddings < 0
            or (manifest_sha256 is not None and not isinstance(manifest_sha256, str))
            or (profile is not None and not isinstance(profile, str))
        ):
            raise RagDatabaseError("release embedding state is invalid")
        return ReleaseState(
            status,
            model,
            dimension,
            embeddings,
            manifest_sha256,
            profile,
        )
