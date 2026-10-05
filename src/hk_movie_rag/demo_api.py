"""Restricted same-origin FastAPI demo for the minimum movie RAG slice."""

from __future__ import annotations

import hashlib
import logging
import os
import re
import tempfile
from collections.abc import AsyncIterator, Iterator, Mapping
from contextlib import asynccontextmanager, suppress
from dataclasses import asdict, dataclass
from importlib.resources import files
from io import BytesIO
from typing import IO, Literal, Protocol, cast

from fastapi import FastAPI, HTTPException, Path, Request, Response
from fastapi.exceptions import RequestValidationError
from fastapi.responses import JSONResponse, StreamingResponse
from google.cloud import storage
from pydantic import BaseModel, ConfigDict, Field, field_validator, model_validator

from .cloud_run_identity import is_cloud_run_revision_name
from .poster_authority import (
    POSTER_SERVING_AUTHORITY_SHA256,
    load_poster_serving_authority,
)
from .poster_policy import MAX_POSTER_BYTES as _MAX_POSTER_BYTES
from .rag_bundle import RagReleaseContract, RagReleaseCounts
from .rag_db import (
    DatabaseSettings,
    FacetStats,
    RagDatabaseError,
    RagRepository,
    ReleaseState,
    ReleaseStats,
    create_db_pool,
)
from .rag_query import (
    ChatAnswer,
    Citation,
    MovieCard,
    QueryRepository,
    QueryService,
    QueryValidationError,
)
from .relevance import GENERAL_RELEVANCE_POLICY_SHA256, load_general_relevance_policy
from .retrieval import ConversationExchange
from .vertex_clients import VertexEmbeddingClient, VertexGenerationClient

_POSTER_CHUNK_BYTES = 64 * 1024
_APPROVED_POSTER_STATES = frozenset({"machine_passed", "manual_approved"})
_MOVIE_ID = re.compile(r"^[A-Za-z0-9][A-Za-z0-9_-]{0,127}$")
_SHA256 = re.compile(r"^[0-9a-f]{64}$")
_REQUIRED_FACET_COUNTS = FacetStats(4658, 50, 313, 4295, 24)
_LOGGER = logging.getLogger(__name__)
_FailureStage = Literal["config_db", "chat_backend", "poster_db", "poster_storage"]


class DemoConfigurationError(RuntimeError):
    """Raised when the demo cannot start with a safe complete configuration."""


class PosterNotFoundError(RuntimeError):
    """Raised when a governed poster object is absent or fails its identity gate."""


class PosterStorageError(RuntimeError):
    """Raised when private object storage is temporarily unavailable."""


def _log_stage_failure(stage: _FailureStage, exc: Exception) -> None:
    """Emit only the fixed stage and exception class, never dependency text."""
    _LOGGER.warning("stage=%s exception_type=%s", stage, type(exc).__name__)


@dataclass(frozen=True)
class DemoSettings:
    """Non-secret public-demo settings bound to one immutable RAG release."""

    project_id: str
    vertex_location: str
    release_id: str
    release_manifest_sha256: str
    embedding_model: str
    embedding_dimension: int
    generation_model: str
    poster_bucket: str
    relevance_policy_sha256: str
    poster_authority_sha256: str
    serving_revision: str
    image_digest: str

    def __post_init__(self) -> None:
        text_values = {
            "GOOGLE_CLOUD_PROJECT": self.project_id,
            "VERTEX_LOCATION": self.vertex_location,
            "RAG_RELEASE_ID": self.release_id,
            "RAG_RELEASE_MANIFEST_SHA256": self.release_manifest_sha256,
            "RAG_EMBEDDING_MODEL": self.embedding_model,
            "RAG_GENERATION_MODEL": self.generation_model,
            "RAG_RELEASE_BUCKET": self.poster_bucket,
            "RAG_RELEVANCE_POLICY_SHA256": self.relevance_policy_sha256,
            "RAG_POSTER_AUTHORITY_SHA256": self.poster_authority_sha256,
            "K_REVISION": self.serving_revision,
            "RAG_IMAGE_DIGEST": self.image_digest,
        }
        missing = [name for name, value in text_values.items() if not isinstance(value, str) or not value]
        if missing:
            raise DemoConfigurationError(f"missing required demo settings: {', '.join(missing)}")
        if self.vertex_location != "global":
            raise DemoConfigurationError("Vertex location must be global")
        if _SHA256.fullmatch(self.release_manifest_sha256) is None:
            raise DemoConfigurationError("release manifest digest is invalid")
        if self.embedding_dimension != 768:
            raise DemoConfigurationError("embedding dimension must be 768")
        if self.generation_model != "gemini-3.5-flash-lite":
            raise DemoConfigurationError("generation model is not approved for this demo")
        if not is_cloud_run_revision_name(self.serving_revision) or (
            re.fullmatch(r"sha256:[0-9a-f]{64}", self.image_digest) is None
        ):
            raise DemoConfigurationError("serving identity is invalid")
        if _SHA256.fullmatch(self.relevance_policy_sha256) is None:
            raise DemoConfigurationError("relevance policy digest is invalid")
        if _SHA256.fullmatch(self.poster_authority_sha256) is None:
            raise DemoConfigurationError("poster authority digest is invalid")
        if not re.fullmatch(r"[a-z0-9][a-z0-9._-]{1,221}[a-z0-9]", self.poster_bucket):
            raise DemoConfigurationError("release bucket name is invalid")

    @classmethod
    def from_env(cls, environ: Mapping[str, str] | None = None) -> DemoSettings:
        values = os.environ if environ is None else environ
        required = (
            "GOOGLE_CLOUD_PROJECT",
            "VERTEX_LOCATION",
            "RAG_RELEASE_ID",
            "RAG_RELEASE_MANIFEST_SHA256",
            "RAG_EMBEDDING_MODEL",
            "RAG_EMBEDDING_DIMENSION",
            "RAG_GENERATION_MODEL",
            "RAG_RELEASE_BUCKET",
            "RAG_RELEVANCE_POLICY_SHA256",
            "RAG_POSTER_AUTHORITY_SHA256",
            "K_REVISION",
            "RAG_IMAGE_DIGEST",
        )
        missing = [name for name in required if not values.get(name)]
        if missing:
            if "RAG_RELEASE_MANIFEST_SHA256" in missing:
                raise DemoConfigurationError("release manifest digest is invalid")
            if "RAG_RELEVANCE_POLICY_SHA256" in missing:
                raise DemoConfigurationError("relevance policy digest is invalid")
            if "RAG_POSTER_AUTHORITY_SHA256" in missing:
                raise DemoConfigurationError("poster authority digest is invalid")
            if "K_REVISION" in missing or "RAG_IMAGE_DIGEST" in missing:
                raise DemoConfigurationError("serving identity is invalid")
            raise DemoConfigurationError(f"missing required demo settings: {', '.join(missing)}")
        try:
            dimension = int(values["RAG_EMBEDDING_DIMENSION"])
        except ValueError as exc:
            raise DemoConfigurationError("numeric demo setting is invalid") from exc
        return cls(
            project_id=values["GOOGLE_CLOUD_PROJECT"],
            vertex_location=values["VERTEX_LOCATION"],
            release_id=values["RAG_RELEASE_ID"],
            release_manifest_sha256=values["RAG_RELEASE_MANIFEST_SHA256"],
            embedding_model=values["RAG_EMBEDDING_MODEL"],
            embedding_dimension=dimension,
            generation_model=values["RAG_GENERATION_MODEL"],
            poster_bucket=values["RAG_RELEASE_BUCKET"],
            relevance_policy_sha256=values["RAG_RELEVANCE_POLICY_SHA256"],
            poster_authority_sha256=values["RAG_POSTER_AUTHORITY_SHA256"],
            serving_revision=values["K_REVISION"],
            image_digest=values["RAG_IMAGE_DIGEST"],
        )


@dataclass(frozen=True)
class PosterContent:
    """A bounded, hash-verified poster stream and its GCS response metadata."""

    stream: IO[bytes]
    size: int
    content_type: str
    etag: str
    content_sha256: str


class DemoQueryService(Protocol):
    def answer(
        self, question: str, history: tuple[ConversationExchange, ...] = ()
    ) -> ChatAnswer: ...


class DemoRepository(Protocol):
    def query_ready_contract(self, release_id: str) -> RagReleaseContract: ...

    def assert_query_ready(self, release_id: str) -> None: ...

    def release_state(self, release_id: str) -> ReleaseState: ...

    def release_stats(self, release_id: str) -> ReleaseStats: ...

    def facet_stats(self, release_id: str) -> FacetStats: ...

    def get_poster_asset(self, release_id: str, movie_id: str) -> dict[str, object] | None: ...


class PosterStorage(Protocol):
    def open_object(
        self,
        bucket_name: str,
        object_name: str,
        *,
        expected_sha256: str,
        expected_size: int,
        expected_mime_type: str,
        max_bytes: int,
    ) -> PosterContent: ...


class GcsPosterStorage:
    """Open an exact private GCS object into a bounded spool before HTTP streaming."""

    def __init__(self, client: storage.Client) -> None:
        self._client = client

    def open_object(
        self,
        bucket_name: str,
        object_name: str,
        *,
        expected_sha256: str,
        expected_size: int,
        expected_mime_type: str,
        max_bytes: int,
    ) -> PosterContent:
        spool: IO[bytes] | None = None
        try:
            blob = self._client.bucket(bucket_name).get_blob(object_name)
            if blob is None:
                raise PosterNotFoundError("poster is unavailable")
            if (
                not isinstance(blob.size, int)
                or isinstance(blob.size, bool)
                or blob.size != expected_size
                or expected_size < 1
                or blob.size > max_bytes
                or expected_mime_type != "image/webp"
                or blob.content_type != expected_mime_type
                or not isinstance(blob.etag, str)
                or not blob.etag
            ):
                raise PosterNotFoundError("poster is unavailable")
            spool = _new_poster_spool()
            digest = hashlib.sha256()
            observed = 0
            with blob.open("rb") as source:
                while True:
                    chunk = source.read(_POSTER_CHUNK_BYTES)
                    if not chunk:
                        break
                    observed += len(chunk)
                    if observed > max_bytes:
                        raise PosterNotFoundError("poster is unavailable")
                    digest.update(chunk)
                    spool.write(chunk)
            if (
                observed != expected_size
                or observed != blob.size
                or digest.hexdigest() != expected_sha256
            ):
                raise PosterNotFoundError("poster is unavailable")
            spool.seek(0)
            return PosterContent(
                stream=spool,
                size=observed,
                content_type=blob.content_type,
                etag=blob.etag,
                content_sha256=digest.hexdigest(),
            )
        except PosterNotFoundError:
            if spool is not None:
                spool.close()
            raise
        except Exception as exc:
            if spool is not None:
                spool.close()
            raise PosterStorageError("poster storage is unavailable") from exc


@dataclass
class _DemoServices:
    settings: DemoSettings
    query_service: DemoQueryService
    repository: DemoRepository
    poster_storage: PosterStorage
    closeable: object | None = None

    def close(self) -> None:
        close = getattr(self.closeable, "close", None)
        if callable(close):
            close()


class _HistoryExchangeRequest(BaseModel):
    """Strict, client-owned history item accepted only for the current request."""

    model_config = ConfigDict(extra="forbid", strict=True)

    question: str = Field(min_length=1, max_length=1000)
    answer: str = Field(min_length=1, max_length=10000)
    movie_ids: list[str] = Field(max_length=8)

    @field_validator("question", "answer")
    @classmethod
    def text_must_have_non_whitespace_content(cls, value: str) -> str:
        normalized = value.strip()
        if not normalized:
            raise ValueError("history text is empty")
        return normalized

    @field_validator("movie_ids")
    @classmethod
    def movie_ids_must_be_unique_bounded_identifiers(cls, value: list[str]) -> list[str]:
        if len(set(value)) != len(value) or any(_MOVIE_ID.fullmatch(movie_id) is None for movie_id in value):
            raise ValueError("history movie ids are invalid")
        return value

    def as_conversation_exchange(self) -> ConversationExchange:
        return ConversationExchange(self.question, self.answer, tuple(self.movie_ids))


class _ChatRequest(BaseModel):
    model_config = ConfigDict(extra="forbid", strict=True)

    question: str = Field(min_length=1, max_length=1000)
    history: list[_HistoryExchangeRequest] = Field(default_factory=list, max_length=4)

    @field_validator("question")
    @classmethod
    def question_must_have_non_whitespace_text(cls, value: str) -> str:
        normalized = value.strip()
        if not normalized:
            raise ValueError("question is empty")
        return normalized

    @model_validator(mode="after")
    def history_must_fit_total_character_budget(self) -> _ChatRequest:
        characters = sum(
            len(exchange.question)
            + len(exchange.answer)
            + sum(len(movie_id) for movie_id in exchange.movie_ids)
            for exchange in self.history
        )
        if characters > 12_000:
            raise ValueError("history exceeds character budget")
        return self

    def as_history(self) -> tuple[ConversationExchange, ...]:
        return tuple(exchange.as_conversation_exchange() for exchange in self.history)


def create_app(
    *,
    settings: DemoSettings | None = None,
    query_service: DemoQueryService | None = None,
    repository: DemoRepository | None = None,
    poster_storage: PosterStorage | None = None,
    environ: Mapping[str, str] | None = None,
) -> FastAPI:
    """Build the public ASGI app while preserving release and poster authority gates."""
    injected = (settings, query_service, repository, poster_storage)
    if any(item is not None for item in injected) and not all(item is not None for item in injected):
        raise DemoConfigurationError("all injected demo dependencies are required")

    @asynccontextmanager
    async def lifespan(application: FastAPI) -> AsyncIterator[None]:
        if all(item is not None for item in injected):
            assert settings is not None
            assert query_service is not None
            assert repository is not None
            assert poster_storage is not None
            services = _DemoServices(
                settings=settings,
                query_service=query_service,
                repository=repository,
                poster_storage=poster_storage,
            )
        else:
            try:
                services = _runtime_services(environ)
            except Exception as exc:
                _log_stage_failure("config_db", exc)
                raise
        application.state.demo_services = services
        try:
            yield
        finally:
            services.close()

    application = FastAPI(
        title="HK Movie RAG public technical demo",
        docs_url=None,
        redoc_url=None,
        openapi_url=None,
        lifespan=lifespan,
    )

    @application.exception_handler(RequestValidationError)
    async def controlled_validation_error(
        _request: Request, _exc: RequestValidationError
    ) -> JSONResponse:
        return JSONResponse(status_code=422, content={"detail": "invalid request"})

    @application.get("/", include_in_schema=False)
    def index() -> Response:
        return _static_response("index.html", "text/html; charset=utf-8")

    @application.get("/static/app.js", include_in_schema=False)
    def javascript() -> Response:
        return _static_response("app.js", "text/javascript; charset=utf-8")

    @application.get("/static/styles.css", include_in_schema=False)
    def stylesheet() -> Response:
        return _static_response("styles.css", "text/css; charset=utf-8")

    @application.get("/health")
    def health() -> dict[str, str]:
        return {"status": "ok"}

    movie_id_path = Path(min_length=1, max_length=128, pattern=_MOVIE_ID.pattern)

    @application.get("/api/config")
    def config(request: Request) -> dict[str, object]:
        services = _services(request)
        try:
            contract = services.repository.query_ready_contract(services.settings.release_id)
            release = services.repository.release_state(services.settings.release_id)
            counts = services.repository.release_stats(services.settings.release_id)
            facets = services.repository.facet_stats(services.settings.release_id)
            expected_facets = FacetStats(
                contract.counts.facet_count,
                contract.counts.tier_s_count,
                contract.counts.tier_a_count,
                contract.counts.tier_b_count,
                contract.counts.pilot_count,
            )
            if (
                release.status != "active"
                or release.embedding_model != services.settings.embedding_model
                or release.embedding_dimension != services.settings.embedding_dimension
                or release.manifest_sha256
                != services.settings.release_manifest_sha256
                or release.embeddings != counts.embeddings
                or not isinstance(facets, FacetStats)
                or facets != expected_facets
            ):
                raise DemoConfigurationError("active release contract is unavailable")
        except Exception as exc:
            _log_stage_failure("config_db", exc)
            raise HTTPException(status_code=503, detail="configuration temporarily unavailable") from exc
        return {
            "access_mode": "public_tech_demo",
            "rag_release_id": services.settings.release_id,
            "manifest_sha256": release.manifest_sha256,
            "status": release.status,
            "embedding_model": release.embedding_model,
            "embedding_dimension": release.embedding_dimension,
            "generation_model": services.settings.generation_model,
            "relevance_policy_sha256": services.settings.relevance_policy_sha256,
            "poster_authority_sha256": services.settings.poster_authority_sha256,
            "serving_revision": services.settings.serving_revision,
            "image_digest": services.settings.image_digest,
            "facets": {
                "total": facets.total,
                "tier_s": facets.tier_s,
                "tier_a": facets.tier_a,
                "tier_b": facets.tier_b,
                "pilot_movies": facets.pilots,
            },
            "counts": {
                "movies": counts.movies,
                "media_assets": counts.assets,
                "metadata_passages": counts.metadata_passages,
                "movie_documents": counts.documents,
                "pdf_passages": counts.pdf_passages,
                "embeddings": counts.embeddings,
            },
        }

    @application.post("/api/chat")
    def chat(
        payload: _ChatRequest,
        request: Request,
    ) -> dict[str, object]:
        services = _services(request)
        try:
            answer = services.query_service.answer(payload.question, payload.as_history())
        except QueryValidationError as exc:
            raise HTTPException(status_code=422, detail="invalid request") from exc
        except Exception as exc:
            _log_stage_failure("chat_backend", exc)
            raise HTTPException(status_code=503, detail="chat temporarily unavailable") from exc
        return _chat_payload(answer)

    @application.get("/api/posters/{movie_id}")
    def poster(
        request: Request,
        movie_id: str = movie_id_path,
    ) -> StreamingResponse:
        services = _services(request)
        try:
            asset = services.repository.get_poster_asset(services.settings.release_id, movie_id)
        except Exception as exc:
            _log_stage_failure("poster_db", exc)
            raise HTTPException(status_code=503, detail="poster temporarily unavailable") from exc
        identity = _approved_poster_identity(asset, movie_id)
        if identity is None:
            raise HTTPException(status_code=404, detail="poster not found")
        object_name, expected_sha256, expected_size, expected_mime_type, rights_status = identity
        try:
            content = services.poster_storage.open_object(
                services.settings.poster_bucket,
                object_name,
                expected_sha256=expected_sha256,
                expected_size=expected_size,
                expected_mime_type=expected_mime_type,
                max_bytes=_MAX_POSTER_BYTES,
            )
        except PosterNotFoundError as exc:
            raise HTTPException(status_code=404, detail="poster not found") from exc
        except Exception as exc:
            _log_stage_failure("poster_storage", exc)
            raise HTTPException(status_code=503, detail="poster temporarily unavailable") from exc
        if not _valid_poster_content(
            content, expected_sha256, expected_size, expected_mime_type
        ):
            close = getattr(getattr(content, "stream", None), "close", None)
            if callable(close):
                with suppress(Exception):
                    close()
            raise HTTPException(status_code=404, detail="poster not found")
        return StreamingResponse(
            _poster_chunks(content),
            media_type=content.content_type,
            headers={
                "Cache-Control": "private, no-store",
                "Content-Length": str(content.size),
                "ETag": content.etag,
                "X-RAG-Rights-Status": rights_status,
                "X-Content-Type-Options": "nosniff",
            },
        )

    return application


def _assert_runtime_contract(
    settings: DemoSettings, contract: RagReleaseContract
) -> None:
    if (
        contract.schema_version not in {"1.2", "1.3"}
        or contract.rag_release_id != settings.release_id
        or contract.manifest_sha256 != settings.release_manifest_sha256
        or contract.embedding_model != settings.embedding_model
        or contract.embedding_dimension != settings.embedding_dimension
        or contract.generation_model != settings.generation_model
        or contract.access_mode != "restricted_demo"
        or contract.relevance_policy_sha256 != settings.relevance_policy_sha256
        or contract.poster_authority_sha256 != settings.poster_authority_sha256
    ):
        raise DemoConfigurationError("selected release authority is unavailable")


def _runtime_services(environ: Mapping[str, str] | None) -> _DemoServices:
    values = os.environ if environ is None else environ
    settings = DemoSettings.from_env(values)
    pool = create_db_pool(DatabaseSettings.from_env(values))
    try:
        repository = RagRepository.from_pool(pool)
        contract = repository.query_ready_contract(settings.release_id)
        _assert_runtime_contract(settings, contract)
        policy_sha256 = contract.relevance_policy_sha256
        poster_authority_sha256 = contract.poster_authority_sha256
        if not isinstance(policy_sha256, str) or not isinstance(
            poster_authority_sha256, str
        ):
            raise DemoConfigurationError("selected release authority is unavailable")
        policy = load_general_relevance_policy(
            contract.rag_release_id,
            policy_sha256,
        )
        authority = load_poster_serving_authority(
            contract.rag_release_id,
            poster_authority_sha256,
            contract=contract,
        )
        if (
            policy.artifact_sha256 != policy_sha256
            or policy.release_id != contract.rag_release_id
            or policy.embedding_model != contract.embedding_model
            or policy.embedding_dimension != contract.embedding_dimension
            or authority.artifact_sha256 != poster_authority_sha256
            or authority.access_mode != contract.access_mode
            or authority.delivery_scope
            != "authenticated_same_origin_private_proxy"
            or authority.primary_poster_rows != contract.counts.primary_poster_rows
            or authority.approved_poster_objects
            != contract.counts.approved_poster_objects
            or authority.unavailable_poster_rows
            != contract.counts.unavailable_poster_rows
            or authority.operator_permission_confirmed is not True
            or authority.source_rights_mutation != "forbidden"
        ):
            raise DemoConfigurationError("selected release authority is unavailable")
        query_service = QueryService(
            settings.release_id,
            settings.release_manifest_sha256,
            cast(QueryRepository, repository),
            VertexEmbeddingClient(
                settings.project_id,
                settings.vertex_location,
                settings.embedding_model,
                settings.embedding_dimension,
            ),
            VertexGenerationClient(
                settings.project_id,
                settings.vertex_location,
                settings.generation_model,
            ),
            expected_relevance_policy_sha256=policy_sha256,
        )
        return _DemoServices(
            settings=settings,
            query_service=query_service,
            repository=repository,
            poster_storage=GcsPosterStorage(storage.Client(project=settings.project_id)),
            closeable=pool,
        )
    except Exception:  # noqa: BLE001 - initialized pool must close on any constructor failure
        with suppress(Exception):
            pool.close()
        raise RagDatabaseError("runtime service startup failed") from None


def _new_poster_spool() -> IO[bytes]:
    """Create a spool whose ownership transfers to the returned PosterContent."""
    return tempfile.SpooledTemporaryFile(max_size=1024 * 1024, mode="w+b")


def _services(request: Request) -> _DemoServices:
    services = getattr(request.app.state, "demo_services", None)
    if not isinstance(services, _DemoServices):
        raise HTTPException(status_code=503, detail="demo is unavailable")
    return services


def _chat_payload(answer: ChatAnswer) -> dict[str, object]:
    if (
        not isinstance(answer, ChatAnswer)
        or not isinstance(answer.answer_markdown, str)
        or not answer.answer_markdown.strip()
        or not isinstance(answer.citations, tuple)
        or not isinstance(answer.movies, tuple)
        or not all(_valid_citation(citation) for citation in answer.citations)
        or not all(_valid_movie_card(movie) for movie in answer.movies)
    ):
        raise HTTPException(status_code=503, detail="chat temporarily unavailable")
    return {
        "answer_markdown": answer.answer_markdown,
        "citations": [asdict(citation) for citation in answer.citations],
        "movies": [asdict(movie) for movie in answer.movies],
    }


def _valid_citation(citation: object) -> bool:
    if not isinstance(citation, Citation):
        return False
    return (
        all(
            isinstance(value, str) and value
            for value in (
                citation.citation_id,
                citation.movie_id,
                citation.movie_title,
                citation.source_kind,
                citation.excerpt,
            )
        )
        and citation.source_kind in {"movie_metadata", "pdf_page"}
        and (
            citation.page_number is None
            or (
                isinstance(citation.page_number, int)
                and not isinstance(citation.page_number, bool)
                and citation.page_number > 0
            )
        )
        and (citation.source_filename is None or isinstance(citation.source_filename, str))
    )


def _valid_movie_card(movie: object) -> bool:
    if not isinstance(movie, MovieCard) or _MOVIE_ID.fullmatch(movie.movie_id) is None:
        return False
    if not all(
        isinstance(value, str)
        for value in (
            movie.chinese_title,
            movie.english_title,
            movie.release_date,
            movie.director,
            movie.cast,
            movie.genre,
        )
    ):
        return False
    return (
        movie.tier in {"S", "A", "B"}
        and isinstance(movie.pilot_movie, bool)
        and (movie.poster_url is None or movie.poster_url == f"/api/posters/{movie.movie_id}")
    )


def _approved_poster_identity(
    asset: Mapping[str, object] | None,
    requested_movie_id: str,
) -> tuple[str, str, int, str, str] | None:
    if not isinstance(asset, Mapping):
        return None
    quality_status = asset.get("quality_status")
    rights_status = asset.get("rights_status")
    derived_content_sha256 = asset.get("derived_content_sha256")
    derived_byte_length = asset.get("derived_byte_length")
    derived_mime_type = asset.get("derived_mime_type")
    object_uri = asset.get("derived_object_uri")
    asset_movie_id = asset.get("movie_id")
    canonical_object_name = f"assets/posters/derived/{requested_movie_id}.webp"
    if (
        asset_movie_id != requested_movie_id
        or quality_status not in _APPROVED_POSTER_STATES
        or rights_status not in {"unknown", "restricted"}
        or not isinstance(derived_content_sha256, str)
        or _SHA256.fullmatch(derived_content_sha256) is None
        or not isinstance(derived_byte_length, int)
        or isinstance(derived_byte_length, bool)
        or not 1 <= derived_byte_length <= _MAX_POSTER_BYTES
        or derived_mime_type != "image/webp"
        or not isinstance(object_uri, str)
        or object_uri != canonical_object_name
    ):
        return None
    return (
        canonical_object_name,
        derived_content_sha256,
        derived_byte_length,
        derived_mime_type,
        rights_status,
    )


def _valid_poster_content(
    content: object, expected_sha256: str, expected_size: int, expected_mime_type: str
) -> bool:
    return (
        isinstance(content, PosterContent)
        and isinstance(content.size, int)
        and not isinstance(content.size, bool)
        and content.size == expected_size
        and 1 <= content.size <= _MAX_POSTER_BYTES
        and content.content_type == expected_mime_type == "image/webp"
        and content.content_sha256 == expected_sha256
        and isinstance(content.etag, str)
        and 0 < len(content.etag) <= 256
        and "\r" not in content.etag
        and "\n" not in content.etag
        and callable(getattr(content.stream, "read", None))
    )


def _poster_chunks(content: PosterContent) -> Iterator[bytes]:
    remaining = content.size
    try:
        while remaining:
            chunk = content.stream.read(min(_POSTER_CHUNK_BYTES, remaining))
            if not chunk:
                raise PosterStorageError("poster stream ended early")
            remaining -= len(chunk)
            yield chunk
        if content.stream.read(1):
            raise PosterStorageError("poster stream exceeded declared size")
    finally:
        content.stream.close()


def _static_response(filename: str, media_type: str) -> Response:
    resource = files("hk_movie_rag").joinpath("static", filename)
    try:
        body = resource.read_bytes()
    except (FileNotFoundError, OSError) as exc:
        raise HTTPException(status_code=503, detail="demo UI is unavailable") from exc
    headers = {
        "Cache-Control": "public, max-age=300",
        "Content-Security-Policy": (
            "default-src 'self'; img-src 'self'; script-src 'self'; style-src 'self'; "
            "base-uri 'none'; frame-ancestors 'none'; form-action 'self'"
        ),
        "Referrer-Policy": "no-referrer",
        "X-Content-Type-Options": "nosniff",
    }
    return Response(content=body, media_type=media_type, headers=headers)


_OFFLINE_POSTER = (
    b"RIFF\x1e\x00\x00\x00WEBPVP8L\x11\x00\x00\x00/\x00\x00\x00\x00\x07P\x98b"
    b"\x94\xa4\xff\x81\x88\xe8\x7f\x00\x00"
)


class _OfflineQueryService:
    def answer(self, question: str, history: tuple[ConversationExchange, ...] = ()) -> ChatAnswer:
        del question, history
        return ChatAnswer(
            answer_markdown="《醉拳》的動作設計結合了喜劇節奏。[pdf:drunken:p2]",
            citations=(
                Citation(
                    citation_id="pdf:drunken:p2",
                    movie_id="1978_ZQ_001",
                    movie_title="醉拳",
                    source_kind="pdf_page",
                    page_number=2,
                    source_filename="S 級港⽚-醉拳.pdf",
                    excerpt="本地 smoke 使用的受控示例摘錄。",
                ),
            ),
            movies=(
                MovieCard(
                    movie_id="1978_ZQ_001",
                    chinese_title="醉拳",
                    english_title="Drunken Master",
                    release_date="1978-10-05",
                    director="袁和平",
                    cast="成龍、袁小田",
                    genre="動作、喜劇",
                    tier="S",
                    pilot_movie=True,
                    poster_url="/api/posters/1978_ZQ_001",
                ),
            ),
        )


class _OfflineRepository:
    def query_ready_contract(self, release_id: str) -> RagReleaseContract:
        if release_id != "v1.2-demo":
            raise RagDatabaseError("active release contract is unavailable")
        return RagReleaseContract(
            schema_version="1.2",
            rag_release_id=release_id,
            parent_release_manifest_sha256="0" * 64,
            manifest_sha256="0" * 64,
            bundle_sha256="0" * 64,
            derived_inventory_sha256="0" * 64,
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
                documents=2,
                pdf_passages=6,
            ),
            embedding_model="gemini-embedding-2",
            embedding_dimension=768,
            generation_model="gemini-3.5-flash-lite",
            text_extraction_profile="cjk-layout-v1",
            document_embedding_profile="vertex-title-text-v1",
            relevance_policy_sha256=GENERAL_RELEVANCE_POLICY_SHA256,
            poster_authority_sha256=POSTER_SERVING_AUTHORITY_SHA256,
            access_mode="restricted_demo",
        )

    def assert_query_ready(self, release_id: str) -> None:
        if release_id != "v1.2-demo":
            raise RagDatabaseError("active release contract is unavailable")

    def release_state(self, release_id: str) -> ReleaseState:
        del release_id
        return ReleaseState("active", "gemini-embedding-2", 768, 4664, "0" * 64)

    def release_stats(self, release_id: str) -> ReleaseStats:
        del release_id
        return ReleaseStats(4658, 4658, 4658, 2, 6, 4664)

    def facet_stats(self, release_id: str) -> FacetStats:
        del release_id
        return _REQUIRED_FACET_COUNTS

    def get_poster_asset(self, release_id: str, movie_id: str) -> dict[str, object] | None:
        del release_id
        if movie_id not in {"1978_ZQ_001", "1982_ZJPD_001"}:
            return None
        return {
            "asset_id": f"poster:{movie_id}:offline",
            "movie_id": movie_id,
            "derived_object_uri": f"assets/posters/derived/{movie_id}.webp",
            "content_sha256": hashlib.sha256(b"offline original poster").hexdigest(),
            "derived_content_sha256": hashlib.sha256(_OFFLINE_POSTER).hexdigest(),
            "derived_byte_length": len(_OFFLINE_POSTER),
            "derived_mime_type": "image/webp",
            "quality_status": "machine_passed",
            "rights_status": "unknown",
        }


class _OfflinePosterStorage:
    def open_object(
        self,
        bucket_name: str,
        object_name: str,
        *,
        expected_sha256: str,
        expected_size: int,
        expected_mime_type: str,
        max_bytes: int,
    ) -> PosterContent:
        if (
            bucket_name != "offline-private-bucket"
            or object_name
            not in {
                "assets/posters/derived/1978_ZQ_001.webp",
                "assets/posters/derived/1982_ZJPD_001.webp",
            }
            or expected_sha256 != hashlib.sha256(_OFFLINE_POSTER).hexdigest()
            or expected_size != len(_OFFLINE_POSTER)
            or expected_mime_type != "image/webp"
            or len(_OFFLINE_POSTER) > max_bytes
        ):
            raise PosterNotFoundError("poster is unavailable")
        return PosterContent(
            stream=BytesIO(_OFFLINE_POSTER),
            size=len(_OFFLINE_POSTER),
            content_type="image/webp",
            etag='"offline-poster"',
            content_sha256=expected_sha256,
        )


def create_offline_app(environ: Mapping[str, str] | None = None) -> FastAPI:
    """Create an explicit local-only fake-bound app for browser smoke testing."""
    values = os.environ if environ is None else environ
    if values.get("RAG_DEMO_OFFLINE") != "1" or values.get("K_SERVICE"):
        raise DemoConfigurationError("offline demo is local-only")
    return create_app(
        settings=DemoSettings(
            project_id="offline-local",
            vertex_location="global",
            release_id="v1.2-demo",
            release_manifest_sha256="0" * 64,
            embedding_model="gemini-embedding-2",
            embedding_dimension=768,
            generation_model="gemini-3.5-flash-lite",
            poster_bucket="offline-private-bucket",
            relevance_policy_sha256=GENERAL_RELEVANCE_POLICY_SHA256,
            poster_authority_sha256=POSTER_SERVING_AUTHORITY_SHA256,
            serving_revision="hk-movie-rag-demo-00000-offline",
            image_digest=f"sha256:{hashlib.sha256(b'offline-image').hexdigest()}",
        ),
        query_service=_OfflineQueryService(),
        repository=_OfflineRepository(),
        poster_storage=_OfflinePosterStorage(),
    )


app = create_app()
