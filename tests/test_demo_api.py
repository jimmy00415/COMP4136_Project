"""HTTP and browser-facing contracts for the restricted RAG demo."""

from __future__ import annotations

import hashlib
import logging
import traceback
from dataclasses import dataclass, field, replace
from io import BytesIO

import pytest
from fastapi.testclient import TestClient

from hk_movie_rag import demo_api, rag_db
from hk_movie_rag.demo_api import (
    DemoConfigurationError,
    DemoSettings,
    GcsPosterStorage,
    PosterContent,
    PosterNotFoundError,
    create_app,
    create_offline_app,
)
from hk_movie_rag.demo_auth import SessionAuth
from hk_movie_rag.poster_authority import (
    POSTER_SERVING_AUTHORITY_V2_SHA256,
    POSTER_SERVING_AUTHORITY_V3_SHA256,
    PosterAuthorityError,
)
from hk_movie_rag.rag_bundle import RagReleaseContract, RagReleaseCounts
from hk_movie_rag.rag_db import FacetStats, RagDatabaseError, ReleaseState, ReleaseStats
from hk_movie_rag.rag_query import ChatAnswer, Citation, GroundingError, MovieCard
from hk_movie_rag.relevance import load_general_relevance_policy
from hk_movie_rag.retrieval import ConversationExchange

_ACCESS_CODE = "correct-demo-code"
_POSTER_BYTES = b"RIFF\x0c\x00\x00\x00WEBPVP8 "
_POSTER_SHA256 = hashlib.sha256(_POSTER_BYTES).hexdigest()
_ORIGINAL_POSTER_SHA256 = hashlib.sha256(b"different original image bytes").hexdigest()
_POSTER_AUTHORITY_SHA256 = "ca99a5c04d8e61029f58e90cb3017730dc2b3511361834d7a0cf4860b54005e4"
_RELEASE_MANIFEST_SHA256 = "ab" * 32
_R2_RELEASE_ID = "v1.2-demo-r2"
_R2_RELEVANCE_POLICY_SHA256 = (
    "a4ecce727a469689a2a08c6d42e0d7d1eb975761173e3b6e4d1eaf34db7b2aba"
)
_R3_RELEASE_ID = "v1.2-demo-r3"
_R3_RELEASE_MANIFEST_SHA256 = (
    "97a8ca8f431f4904e967f0646fc6bb99387c4944dddea2570d816874a4f0478a"
)
_R3_RELEVANCE_POLICY_SHA256 = (
    "d6db5e118aeea5dc0484f3c591baac666952f78f4d41bfdcdbbb58693f4f6bba"
)


def _r2_release_contract(**overrides: object) -> RagReleaseContract:
    values: dict[str, object] = {
        "schema_version": "1.2",
        "rag_release_id": _R2_RELEASE_ID,
        "parent_release_manifest_sha256": (
            "e9842db33208e7146d78d86689b13f81fffcf41288d79d84e599b93050445e2c"
        ),
        "manifest_sha256": _RELEASE_MANIFEST_SHA256,
        "bundle_sha256": "bc" * 32,
        "derived_inventory_sha256": (
            "b754727129bc044b6c9da1e6db22067c8279c5e7f8dd41d0350e365376538154"
        ),
        "counts": RagReleaseCounts(
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
            documents=3,
            pdf_passages=12,
        ),
        "embedding_model": "gemini-embedding-2",
        "embedding_dimension": 768,
        "generation_model": "gemini-3.5-flash-lite",
        "text_extraction_profile": "cjk-layout-v1",
        "document_embedding_profile": "vertex-title-text-v1",
        "relevance_policy_sha256": _R2_RELEVANCE_POLICY_SHA256,
        "poster_authority_sha256": POSTER_SERVING_AUTHORITY_V2_SHA256,
        "access_mode": "restricted_demo",
    }
    values.update(overrides)
    return RagReleaseContract(**values)  # type: ignore[arg-type]


def _r3_release_contract() -> RagReleaseContract:
    parent = _r2_release_contract()
    return replace(
        parent,
        schema_version="1.3",
        rag_release_id=_R3_RELEASE_ID,
        manifest_sha256=_R3_RELEASE_MANIFEST_SHA256,
        bundle_sha256=(
            "43659833b915167857cfde1b603cf8e73ac3fd21f43cce1a35713a8e38061aef"
        ),
        derived_inventory_sha256=(
            "3e3cedee507c3017821de1081bc70bbb01d0c73f8683ab484a464d1fc1f7c1db"
        ),
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
        relevance_policy_sha256=_R3_RELEVANCE_POLICY_SHA256,
        poster_authority_sha256=POSTER_SERVING_AUTHORITY_V3_SHA256,
    )


@dataclass(frozen=True)
class QueryCall:
    question: str
    history: tuple[ConversationExchange, ...]


def _poster_asset(
    movie_id: str,
    *,
    quality_status: str = "machine_passed",
    derived_object_uri: str | None = None,
    content_sha256: str = _ORIGINAL_POSTER_SHA256,
    derived_content_sha256: str = _POSTER_SHA256,
    derived_byte_length: int = len(_POSTER_BYTES),
    derived_mime_type: str = "image/webp",
    rights_status: str | None = "unknown",
) -> dict[str, object]:
    asset: dict[str, object] = {
        "asset_id": f"poster:{movie_id}:v1",
        "movie_id": movie_id,
        "derived_object_uri": (
            f"assets/posters/derived/{movie_id}.webp"
            if derived_object_uri is None
            else derived_object_uri
        ),
        "content_sha256": content_sha256,
        "derived_content_sha256": derived_content_sha256,
        "derived_byte_length": derived_byte_length,
        "derived_mime_type": derived_mime_type,
        "quality_status": quality_status,
    }
    if rights_status is not None:
        asset["rights_status"] = rights_status
    return asset


@dataclass
class FakeQueryService:
    """External query work is replaced; route validation and serialization stay real."""

    failure: Exception | None = None
    questions: list[str] = field(default_factory=list)
    calls: list[QueryCall] = field(default_factory=list)

    def answer(
        self, question: str, history: tuple[ConversationExchange, ...] = ()
    ) -> ChatAnswer:
        if self.failure is not None:
            raise self.failure
        self.questions.append(question)
        self.calls.append(QueryCall(question, history))
        return ChatAnswer(
            answer_markdown="《醉拳》的動作設計把喜劇節奏融入打鬥。[pdf:drunken:p2]",
            citations=(
                Citation(
                    citation_id="pdf:drunken:p2",
                    movie_id="1978_ZQ_001",
                    movie_title="醉拳",
                    source_kind="pdf_page",
                    page_number=2,
                    source_filename="S 級港⽚-醉拳.pdf",
                    excerpt="第二頁的受控分析摘錄。",
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


@dataclass
class FakeRepository:
    assets: dict[str, dict[str, object] | None] = field(default_factory=dict)
    readiness_failure: Exception | None = None
    readiness_calls: list[str] = field(default_factory=list)
    operation_order: list[str] = field(default_factory=list)

    def assert_query_ready(self, release_id: str) -> None:
        self.operation_order.append("assert_query_ready")
        self.readiness_calls.append(release_id)
        if self.readiness_failure is not None:
            raise self.readiness_failure

    def query_ready_contract(self, release_id: str) -> RagReleaseContract:
        self.operation_order.append("query_ready_contract")
        self.readiness_calls.append(release_id)
        if self.readiness_failure is not None:
            raise self.readiness_failure
        return _r2_release_contract(rag_release_id=release_id)

    def release_state(self, release_id: str) -> ReleaseState:
        self.operation_order.append("release_state")
        assert release_id == "v1.2-demo"
        return ReleaseState(
            "active", "gemini-embedding-2", 768, 4670, _RELEASE_MANIFEST_SHA256
        )

    def release_stats(self, release_id: str) -> ReleaseStats:
        self.operation_order.append("release_stats")
        assert release_id == "v1.2-demo"
        return ReleaseStats(4658, 4658, 4658, 3, 12, 4670)

    def facet_stats(self, release_id: str) -> FacetStats:
        self.operation_order.append("facet_stats")
        assert release_id == "v1.2-demo"
        return FacetStats(4658, 50, 313, 4295, 24)

    def get_poster_asset(self, release_id: str, movie_id: str) -> dict[str, object] | None:
        self.operation_order.append("get_poster_asset")
        assert release_id == "v1.2-demo"
        return self.assets.get(movie_id)


@dataclass
class FakePosterStorage:
    calls: list[tuple[str, str, str, int, str, int]] = field(default_factory=list)

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
        self.calls.append(
            (
                bucket_name,
                object_name,
                expected_sha256,
                expected_size,
                expected_mime_type,
                max_bytes,
            )
        )
        return PosterContent(
            stream=BytesIO(_POSTER_BYTES),
            size=len(_POSTER_BYTES),
            content_type="image/webp",
            etag='"poster-etag"',
            content_sha256=_POSTER_SHA256,
        )


@pytest.fixture
def settings() -> DemoSettings:
    policy_sha256 = load_general_relevance_policy().artifact_sha256
    return DemoSettings(
        project_id="motionexpaiweb",
        vertex_location="global",
        release_id="v1.2-demo",
        release_manifest_sha256=_RELEASE_MANIFEST_SHA256,
        embedding_model="gemini-embedding-2",
        embedding_dimension=768,
        generation_model="gemini-3.5-flash-lite",
        poster_bucket="private-demo-bucket",
        relevance_policy_sha256=policy_sha256,
        poster_authority_sha256=_POSTER_AUTHORITY_SHA256,
        serving_revision="hk-movie-rag-demo-00010-abc",
        image_digest=f"sha256:{'1' * 64}",
    )


@pytest.fixture
def repository() -> FakeRepository:
    return FakeRepository(
        assets={
            "1978_ZQ_001": _poster_asset("1978_ZQ_001"),
            "1982_ZJPD_001": _poster_asset("1982_ZJPD_001"),
            "manual_movie": _poster_asset(
                "manual_movie",
                quality_status="manual_approved",
            ),
            "rights_restricted": _poster_asset(
                "rights_restricted", rights_status="restricted"
            ),
            "rights_cleared": _poster_asset("rights_cleared", rights_status="cleared"),
            "rights_forbidden": _poster_asset(
                "rights_forbidden", rights_status="forbidden"
            ),
            "rights_empty": _poster_asset("rights_empty", rights_status=""),
            "rights_missing": _poster_asset("rights_missing", rights_status=None),
            "conflicted_movie": _poster_asset(
                "conflicted_movie", quality_status="content_conflict"
            ),
            "wrong_hash": _poster_asset("wrong_hash", derived_content_sha256="0" * 64),
            "wrong_length": _poster_asset("wrong_length", derived_byte_length=999),
            "wrong_mime": _poster_asset("wrong_mime", derived_mime_type="image/jpeg"),
            "absolute_gs": _poster_asset(
                "absolute_gs",
                derived_object_uri=(
                    "gs://private-demo-bucket/assets/posters/derived/absolute_gs.webp"
                ),
            ),
            "absolute_path": _poster_asset(
                "absolute_path",
                derived_object_uri="/assets/posters/derived/absolute_path.webp",
            ),
            "parent_path": _poster_asset(
                "parent_path",
                derived_object_uri="assets/posters/derived/../parent_path.webp",
            ),
            "alternate_prefix": _poster_asset(
                "alternate_prefix",
                derived_object_uri="posters/alternate_prefix.webp",
            ),
            "wrong_movie": _poster_asset(
                "wrong_movie",
                derived_object_uri="assets/posters/derived/1978_ZQ_001.webp",
            ),
            "wrong_extension": _poster_asset(
                "wrong_extension",
                derived_object_uri="assets/posters/derived/wrong_extension.png",
            ),
            "query_key": _poster_asset(
                "query_key",
                derived_object_uri="assets/posters/derived/query_key.webp?download=1",
            ),
            "fragment_key": _poster_asset(
                "fragment_key",
                derived_object_uri="assets/posters/derived/fragment_key.webp#other",
            ),
            "redirect_uri": _poster_asset(
                "redirect_uri",
                derived_object_uri="https://example.invalid/redirect_uri.webp",
            ),
        }
    )


@pytest.fixture
def query_service() -> FakeQueryService:
    return FakeQueryService()


@pytest.fixture
def storage() -> FakePosterStorage:
    return FakePosterStorage()


@pytest.fixture
def client(
    settings: DemoSettings,
    query_service: FakeQueryService,
    repository: FakeRepository,
    storage: FakePosterStorage,
) -> TestClient:
    application = create_app(
        settings=settings,
        query_service=query_service,
        repository=repository,
        poster_storage=storage,
    )
    with TestClient(application, base_url="https://testserver") as test_client:
        yield test_client


def test_public_health_and_ui_do_not_expose_runtime_configuration(client: TestClient) -> None:
    """Breaks if liveness or static UI leaks project, bucket, model, or demo secrets."""
    health = client.get("/health")
    reserved_legacy_health = client.get("/healthz")
    page = client.get("/")

    assert health.status_code == 200
    assert health.json() == {"status": "ok"}
    assert reserved_legacy_health.status_code == 404
    assert page.status_code == 200
    assert "MotionExpert beta" in page.text
    assert "可追溯引用" in page.text
    assert 'for="access-code"' not in page.text
    combined = health.text + page.text
    assert _ACCESS_CODE not in combined
    assert "private-demo-bucket" not in combined
    assert "motionexpaiweb" not in combined


def test_ui_uses_the_motionexpert_light_palette_without_changing_static_routes(
    client: TestClient,
) -> None:
    page = client.get("/")
    stylesheet = client.get("/static/styles.css")

    assert page.status_code == 200
    assert stylesheet.status_code == 200
    assert '<meta name="color-scheme" content="light">' in page.text
    assert "--primary-red: #e63946;" in stylesheet.text
    assert "--primary-red-dark: #c42834;" in stylesheet.text
    assert "--main-bg: #f5f5f5;" in stylesheet.text
    assert "--card-bg: #fff;" in stylesheet.text
    assert "color-scheme: light;" in stylesheet.text
    assert "#0d1111" not in stylesheet.text
    brand_content = stylesheet.text.split(".brand-lockup > div {", 1)
    assert len(brand_content) == 2
    assert "min-width: 0;" in brand_content[1].split("}", 1)[0]


def test_public_demo_exposes_grounded_routes_without_a_session_and_serves_approved_copy(
    client: TestClient,
) -> None:
    """Breaks if the public demo regains a code gate or publishes stale access copy."""
    page = client.get("/")
    config = client.get("/api/config")
    chat = client.post("/api/chat", json={"question": "醉拳是哪一年上映？"})
    poster = client.get("/api/posters/1978_ZQ_001")
    legacy_session = client.post("/api/session", json={"access_code": _ACCESS_CODE})

    assert page.status_code == 200
    assert "MotionExpert · Hong Kong Movie RAG" in page.text
    assert "1970‑2026 香港電影元數據｜垂類 AI 基座" in page.text
    assert "MotionExpert beta" in page.text
    assert "可追溯引用" in page.text
    assert "一起來研究香港電影吧，請隨意提問" in page.text
    assert "答案會在這裡顯示，並附上資料庫引用。" in page.text
    assert "先證明垂直閉環" not in page.text
    assert "依托第一階段元數據基座；遇到缺少參考資料的議題，系統不會生成沒有依據的答案。" in page.text
    assert "輸入演示碼" not in page.text
    assert 'id="logout-button"' not in page.text
    assert config.status_code == 200
    assert config.json()["access_mode"] == "public_tech_demo"
    assert chat.status_code == 200
    assert poster.status_code == 200
    assert legacy_session.status_code == 404


def test_ui_scope_is_a_placeholder_until_authenticated_config_populates_counts(
    client: TestClient,
) -> None:
    """Breaks if adding a document requires editing release-specific UI literals."""
    page = client.get("/")
    script = client.get("/static/app.js")

    assert page.status_code == 200
    assert script.status_code == 200
    assert '<dd id="scope-movies">—</dd>' in page.text
    assert '<dd id="scope-documents">—</dd>' in page.text
    assert '<dd id="scope-pages">—</dd>' in page.text
    assert "兩份深度分析" not in page.text
    assert "scopeMovies.textContent = counts.movies.toLocaleString()" in script.text
    assert "scopeDocuments.textContent = counts.movie_documents.toLocaleString()" in script.text
    assert "scopePages.textContent = counts.pdf_passages.toLocaleString()" in script.text


def test_quick_prompts_cover_general_rag_canonical_deep_and_comparison_paths(
    client: TestClient,
) -> None:
    page = client.get("/")

    assert 'data-question="有沒有好的香港喜劇推薦？">喜劇推薦<' in page.text
    assert 'data-question="請查詢《少林足球》的影片資料。">影片資料查詢<' in page.text
    assert 'data-question="《醉拳》的視覺美學有甚麼特色？">視覺美學分析<' in page.text
    assert 'data-question="比較《醉拳》和《少林足球》的視覺與喜劇風格。">跨作品風格對比<' in page.text


def test_legacy_session_routes_are_absent_and_never_set_a_cookie(
    client: TestClient,
) -> None:
    """Breaks if the removed code-login surface is accidentally restored."""
    post = client.post("/api/session", json={"access_code": _ACCESS_CODE})
    delete = client.delete("/api/session")

    assert post.status_code == 404
    assert delete.status_code == 404
    assert "set-cookie" not in post.headers
    assert "set-cookie" not in delete.headers


def test_expired_and_tampered_session_tokens_are_rejected() -> None:
    """Breaks if a copied or edited access session survives expiry/signature validation."""
    clock = [1_000]
    auth = SessionAuth(_ACCESS_CODE, ttl_seconds=10, now=lambda: clock[0])
    token = auth.issue_token()

    assert auth.is_valid_token(token)
    changed = token[:-1] + ("A" if token[-1] != "A" else "B")
    assert not auth.is_valid_token(changed)
    clock[0] = 1_011
    assert not auth.is_valid_token(token)
    assert _ACCESS_CODE not in token
    assert _ACCESS_CODE not in repr(auth)


def test_malformed_unicode_session_token_is_rejected_without_an_exception() -> None:
    """Breaks if attacker-controlled cookie text can escape the auth boundary as a 500."""
    auth = SessionAuth(_ACCESS_CODE)
    signature = auth.issue_token().split(".", 1)[1]

    assert not auth.is_valid_token(f"☃.{signature}")


def test_chat_config_and_approved_poster_are_public_without_skipping_authority_checks(
    client: TestClient, repository: FakeRepository
) -> None:
    """Breaks if public access bypasses release readiness or governed poster identity."""
    assert client.get("/api/config").status_code == 200
    assert client.post("/api/chat", json={"question": "醉拳導演是誰？"}).status_code == 200
    assert client.get("/api/posters/1978_ZQ_001").status_code == 200
    assert repository.readiness_calls != []
    assert "get_poster_asset" in repository.operation_order


def test_public_config_returns_active_counts_without_infrastructure_or_secrets(
    client: TestClient, repository: FakeRepository
) -> None:
    """Breaks if config omits the active release proof or serializes deployment secrets."""
    response = client.get("/api/config")

    assert response.status_code == 200
    assert response.json() == {
        "access_mode": "public_tech_demo",
        "rag_release_id": "v1.2-demo",
        "manifest_sha256": _RELEASE_MANIFEST_SHA256,
        "status": "active",
        "embedding_model": "gemini-embedding-2",
        "embedding_dimension": 768,
        "generation_model": "gemini-3.5-flash-lite",
        "relevance_policy_sha256": load_general_relevance_policy().artifact_sha256,
        "poster_authority_sha256": _POSTER_AUTHORITY_SHA256,
        "serving_revision": "hk-movie-rag-demo-00010-abc",
        "image_digest": f"sha256:{'1' * 64}",
        "facets": {
            "total": 4658,
            "tier_s": 50,
            "tier_a": 313,
            "tier_b": 4295,
            "pilot_movies": 24,
        },
        "counts": {
            "movies": 4658,
            "media_assets": 4658,
            "metadata_passages": 4658,
            "movie_documents": 3,
            "pdf_passages": 12,
            "embeddings": 4670,
        },
    }
    assert _ACCESS_CODE not in response.text
    assert "private-demo-bucket" not in response.text
    assert "motionexpaiweb" not in response.text
    assert repository.readiness_calls == ["v1.2-demo"]
    assert repository.operation_order == [
        "query_ready_contract",
        "release_state",
        "release_stats",
        "facet_stats",
    ]


def test_config_shared_readiness_failure_is_controlled_and_redacted(
    client: TestClient,
    repository: FakeRepository,
    caplog: pytest.LogCaptureFixture,
) -> None:
    """Breaks if config skips the query gate or exposes repository failure details."""
    secret = "postgresql://rag_user:secret@private-host/rag"
    repository.readiness_failure = RagDatabaseError(secret)
    with caplog.at_level(logging.WARNING):
        response = client.get("/api/config")

    assert response.status_code == 503
    assert response.json() == {"detail": "configuration temporarily unavailable"}
    assert repository.readiness_calls == ["v1.2-demo"]
    assert secret not in response.text
    assert secret not in caplog.text


def test_config_refuses_an_inactive_or_model_mismatched_release(
    settings: DemoSettings,
    query_service: FakeQueryService,
    repository: FakeRepository,
    storage: FakePosterStorage,
) -> None:
    """Breaks if the UI advertises a loading release or a different vector contract as usable."""

    class InactiveRepository(FakeRepository):
        def release_state(self, release_id: str) -> ReleaseState:
            del release_id
            return ReleaseState("loading", "gemini-embedding-001", 768, 3)

    application = create_app(
        settings=settings,
        query_service=query_service,
        repository=InactiveRepository(assets=repository.assets),
        poster_storage=storage,
    )
    with TestClient(application, base_url="https://testserver") as test_client:
        response = test_client.get("/api/config")

    assert response.status_code == 503
    assert response.json() == {"detail": "configuration temporarily unavailable"}


def test_config_accepts_dynamic_complete_release_counts_from_database_contract(
    settings: DemoSettings,
    query_service: FakeQueryService,
    repository: FakeRepository,
    storage: FakePosterStorage,
) -> None:
    """Breaks if runtime literals reject a complete immutable selected release."""

    class IncompleteRepository(FakeRepository):
        def release_state(self, release_id: str) -> ReleaseState:
            del release_id
            return ReleaseState(
                "active", "gemini-embedding-2", 768, 4670, _RELEASE_MANIFEST_SHA256
            )

        def release_stats(self, release_id: str) -> ReleaseStats:
            del release_id
            return ReleaseStats(4658, 4658, 4658, 3, 12, 4670)

    application = create_app(
        settings=settings,
        query_service=query_service,
        repository=IncompleteRepository(assets=repository.assets),
        poster_storage=storage,
    )
    with TestClient(application, base_url="https://testserver") as test_client:
        response = test_client.get("/api/config")

    assert response.status_code == 200
    assert response.json()["counts"] == {
        "movies": 4658,
        "media_assets": 4658,
        "metadata_passages": 4658,
        "movie_documents": 3,
        "pdf_passages": 12,
        "embeddings": 4670,
    }


def test_public_chat_returns_citations_and_same_origin_poster(client: TestClient) -> None:
    """Breaks if grounded service output loses citations or lets the model choose a poster URL."""
    response = client.post("/api/chat", json={"question": "醉拳的視覺美學？"})

    assert response.status_code == 200
    payload = response.json()
    assert payload["answer_markdown"].endswith("[pdf:drunken:p2]")
    assert payload["citations"] == [
        {
            "citation_id": "pdf:drunken:p2",
            "movie_id": "1978_ZQ_001",
            "movie_title": "醉拳",
            "source_kind": "pdf_page",
            "page_number": 2,
            "source_filename": "S 級港⽚-醉拳.pdf",
            "excerpt": "第二頁的受控分析摘錄。",
        }
    ]
    assert payload["movies"][0]["poster_url"] == "/api/posters/1978_ZQ_001"


def test_chat_passes_bounded_history_and_returns_facet_cards(
    client: TestClient, query_service: FakeQueryService
) -> None:
    """Breaks if the chat route drops typed history or hides governed card facets."""
    response = client.post(
        "/api/chat",
        json={
            "question": "還有別的嗎？",
            "history": [
                {"question": "推薦喜劇", "answer": "三部。", "movie_ids": ["a", "b", "c"]}
            ],
        },
    )

    assert response.status_code == 200
    assert query_service.calls == [
        QueryCall("還有別的嗎？", (ConversationExchange("推薦喜劇", "三部。", ("a", "b", "c")),))
    ]
    assert response.json()["movies"][0]["tier"] == "S"
    assert response.json()["movies"][0]["pilot_movie"] is True


def test_chat_rejects_fifth_history_exchange(client: TestClient) -> None:
    """Breaks if the API silently truncates a client history beyond the four-turn contract."""
    exchanges = [
        {"question": f"問題 {index}", "answer": "答案", "movie_ids": [f"movie_{index}"]}
        for index in range(5)
    ]

    response = client.post("/api/chat", json={"question": "下一部", "history": exchanges})

    assert response.status_code == 422
    assert response.json() == {"detail": "invalid request"}


@pytest.mark.parametrize(
    "history",
    [
        [{"question": "問題", "answer": "答案", "movie_ids": ["valid"], "extra": "nope"}],
        [{"question": "問題", "answer": "答案"}],
        [{"question": "問題", "answer": "答案", "movie_ids": [f"movie_{index}" for index in range(9)]}],
        [{"question": "問題", "answer": "答" * 12_001, "movie_ids": []}],
    ],
)
def test_chat_rejects_history_outside_the_strict_public_contract(
    client: TestClient, history: list[dict[str, object]]
) -> None:
    """Breaks if oversized or unrecognized anonymous history reaches the query service."""
    response = client.post("/api/chat", json={"question": "下一部", "history": history})

    assert response.status_code == 422
    assert response.json() == {"detail": "invalid request"}


def test_config_refuses_incomplete_facet_health(
    settings: DemoSettings,
    query_service: FakeQueryService,
    repository: FakeRepository,
    storage: FakePosterStorage,
) -> None:
    """Breaks if a release with a missing tier/pilot facet can be advertised as healthy."""

    class IncompleteFacetRepository(FakeRepository):
        def facet_stats(self, release_id: str) -> FacetStats:
            del release_id
            return FacetStats(4658, 50, 313, 4294, 24)

    application = create_app(
        settings=settings,
        query_service=query_service,
        repository=IncompleteFacetRepository(assets=repository.assets),
        poster_storage=storage,
    )
    with TestClient(application, base_url="https://testserver") as test_client:
        response = test_client.get("/api/config")

    assert response.status_code == 503
    assert response.json() == {"detail": "configuration temporarily unavailable"}


def _runtime_environ() -> dict[str, str]:
    return {
        "GOOGLE_CLOUD_PROJECT": "motionexpaiweb",
        "VERTEX_LOCATION": "global",
        "RAG_RELEASE_ID": _R2_RELEASE_ID,
        "RAG_RELEASE_MANIFEST_SHA256": _RELEASE_MANIFEST_SHA256,
        "RAG_EMBEDDING_MODEL": "gemini-embedding-2",
        "RAG_EMBEDDING_DIMENSION": "768",
        "RAG_GENERATION_MODEL": "gemini-3.5-flash-lite",
        "RAG_RELEASE_BUCKET": "private-demo-bucket",
        "RAG_RELEVANCE_POLICY_SHA256": _R2_RELEVANCE_POLICY_SHA256,
        "RAG_POSTER_AUTHORITY_SHA256": POSTER_SERVING_AUTHORITY_V2_SHA256,
        "K_REVISION": "hk-movie-rag-demo-00010-abc",
        "RAG_IMAGE_DIGEST": f"sha256:{'1' * 64}",
        "DB_NAME": "rag",
        "DB_USER": "rag_user",
        "DB_PASSWORD": "runtime-db-secret",
        "INSTANCE_CONNECTION_NAME": "project:region:instance",
    }


@pytest.mark.parametrize(
    "configured", [None, "0" * 63, "A" * 64, "sha256:" + "0" * 64]
)
def test_demo_settings_require_lowercase_relevance_policy_digest_syntax(
    configured: str | None,
) -> None:
    """Semantic authority comes from DB; settings still reject malformed identity."""
    environ = _runtime_environ()
    if configured is None:
        environ.pop("RAG_RELEVANCE_POLICY_SHA256")
    else:
        environ["RAG_RELEVANCE_POLICY_SHA256"] = configured

    with pytest.raises(DemoConfigurationError, match="relevance policy"):
        DemoSettings.from_env(environ)


@pytest.mark.parametrize(
    "configured",
    [None, "0" * 63, "A" * 64, "sha256:" + "0" * 64],
)
def test_demo_settings_require_exact_lowercase_release_manifest_digest(
    configured: str | None,
) -> None:
    """Breaks if startup can select a release without an exact manifest identity."""
    environ = _runtime_environ()
    if configured is None:
        environ.pop("RAG_RELEASE_MANIFEST_SHA256")
    else:
        environ["RAG_RELEASE_MANIFEST_SHA256"] = configured

    with pytest.raises(DemoConfigurationError, match="release manifest"):
        DemoSettings.from_env(environ)


@pytest.mark.parametrize(
    "configured",
    [None, "0" * 63, POSTER_SERVING_AUTHORITY_V2_SHA256.upper(), "sha256:" + "0" * 64],
)
def test_demo_settings_require_exact_lowercase_poster_authority_digest(
    configured: str | None,
) -> None:
    """Breaks if a revision can start without the exact reviewed poster authority."""
    environ = _runtime_environ()
    if configured is None:
        environ.pop("RAG_POSTER_AUTHORITY_SHA256")
    else:
        environ["RAG_POSTER_AUTHORITY_SHA256"] = configured

    with pytest.raises(DemoConfigurationError, match="poster authority"):
        DemoSettings.from_env(environ)


@pytest.mark.parametrize(
    ("field_name", "configured"),
    (
        ("K_REVISION", None),
        ("K_REVISION", "latest"),
        ("RAG_IMAGE_DIGEST", None),
        ("RAG_IMAGE_DIGEST", "sha256:short"),
        ("RAG_IMAGE_DIGEST", f"sha256:{'A' * 64}"),
    ),
)
def test_demo_settings_require_exact_serving_identity(
    field_name: str, configured: str | None
) -> None:
    """Breaks if a revision can conceal which image actually served a smoke request."""
    environ = _runtime_environ()
    if configured is None:
        environ.pop(field_name)
    else:
        environ[field_name] = configured

    with pytest.raises(DemoConfigurationError, match="serving identity"):
        DemoSettings.from_env(environ)


def test_demo_settings_accept_unique_explicit_r2_revision_suffix() -> None:
    environ = _runtime_environ()
    environ["K_REVISION"] = (
        "hk-movie-rag-demo-00000-r2-abcdef123456-0123456789abcdef"
    )

    settings = DemoSettings.from_env(environ)

    assert settings.serving_revision == environ["K_REVISION"]


@pytest.mark.parametrize(
    "revision",
    [
        "other-service-00000-r2-abcdef123456-0123456789abcdef",
        "hk-movie-rag-demo-00000-r2-abcdef123456-0123456789abcdef-",
        "hk-movie-rag-demo-00000-r2-ABCDEF123456-0123456789abcdef",
        "hk-movie-rag-demo-" + "a" * 46,
    ],
)
def test_demo_settings_reject_non_scoped_or_unbounded_revision(revision: str) -> None:
    environ = _runtime_environ()
    environ["K_REVISION"] = revision

    with pytest.raises(DemoConfigurationError, match="serving identity"):
        DemoSettings.from_env(environ)


@pytest.mark.parametrize(
    "configured",
    [None, "0" * 63, POSTER_SERVING_AUTHORITY_V2_SHA256.upper()],
)
def test_invalid_poster_authority_refuses_lifespan_before_external_factories(
    monkeypatch: pytest.MonkeyPatch,
    configured: str | None,
) -> None:
    """Breaks if invalid authority opens DB, GCS, or Vertex resources before refusal."""
    environ = _runtime_environ()
    if configured is None:
        environ.pop("RAG_POSTER_AUTHORITY_SHA256")
    else:
        environ["RAG_POSTER_AUTHORITY_SHA256"] = configured
    calls: list[str] = []

    class Pool:
        def close(self) -> None:
            calls.append("pool_close")

    monkeypatch.setattr(
        demo_api,
        "create_db_pool",
        lambda *_args: calls.append("db_pool") or Pool(),
    )
    monkeypatch.setattr(
        demo_api.storage,
        "Client",
        lambda **_kwargs: calls.append("gcs") or object(),
    )
    monkeypatch.setattr(
        demo_api,
        "VertexEmbeddingClient",
        lambda *_args: calls.append("embedding") or object(),
    )
    monkeypatch.setattr(
        demo_api,
        "VertexGenerationClient",
        lambda *_args: calls.append("generation") or object(),
    )
    monkeypatch.setattr(demo_api, "QueryService", lambda *_args, **_kwargs: object())

    application = create_app(environ=environ)
    with (
        pytest.raises(DemoConfigurationError, match="poster authority"),
        TestClient(application, base_url="https://testserver"),
    ):
        pass

    assert calls == []


def test_tampered_packaged_authority_closes_bound_pool_before_remote_clients(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Packaged bytes are checked only after the persisted release selects them."""
    calls: list[str] = []

    class Pool:
        def close(self) -> None:
            calls.append("pool_close")

    class ContractRepository(FakeRepository):
        def query_ready_contract(self, release_id: str) -> RagReleaseContract:
            calls.append("query_ready_contract")
            assert release_id == _R2_RELEASE_ID
            return _r2_release_contract()

    monkeypatch.setattr(
        demo_api,
        "load_poster_serving_authority",
        lambda *_args, **_kwargs: (_ for _ in ()).throw(
            PosterAuthorityError("tampered-secret-detail")
        ),
    )
    monkeypatch.setattr(
        demo_api,
        "create_db_pool",
        lambda *_args: calls.append("db_pool") or Pool(),
    )
    monkeypatch.setattr(
        demo_api.RagRepository,
        "from_pool",
        classmethod(lambda _cls, _pool: ContractRepository()),
    )
    monkeypatch.setattr(
        demo_api,
        "VertexEmbeddingClient",
        lambda *_args: calls.append("embedding") or object(),
    )
    monkeypatch.setattr(
        demo_api.storage,
        "Client",
        lambda **_kwargs: calls.append("gcs") or object(),
    )

    with pytest.raises(RagDatabaseError, match="runtime service startup failed") as raised:
        demo_api._runtime_services(_runtime_environ())

    assert calls == ["db_pool", "query_ready_contract", "pool_close"]
    assert "tampered-secret-detail" not in str(raised.value)


def test_r2_runtime_binds_persisted_contract_before_authorities_or_remote_clients(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Breaks if R2 startup trusts env-only policy/poster identities."""
    calls: list[str] = []
    contract = _r2_release_contract()

    class Pool:
        def close(self) -> None:
            calls.append("pool_close")

    class ContractRepository(FakeRepository):
        def query_ready_contract(self, release_id: str) -> RagReleaseContract:
            calls.append("query_ready_contract")
            assert release_id == _R2_RELEASE_ID
            return contract

    real_policy_loader = load_general_relevance_policy
    real_poster_loader = demo_api.load_poster_serving_authority

    def policy_loader(release_id: str, digest: str) -> object:
        calls.append("policy")
        assert (release_id, digest) == (
            _R2_RELEASE_ID,
            _R2_RELEVANCE_POLICY_SHA256,
        )
        return real_policy_loader(release_id, digest)

    def poster_loader(
        release_id: str,
        digest: str,
        *,
        contract: RagReleaseContract,
    ) -> object:
        calls.append("poster_authority")
        assert contract == _r2_release_contract()
        return real_poster_loader(release_id, digest, contract=contract)

    query_arguments: dict[str, object] = {}

    def query_service(*args: object, **kwargs: object) -> FakeQueryService:
        calls.append("query_service")
        query_arguments["args"] = args
        query_arguments["kwargs"] = kwargs
        return FakeQueryService()

    pool = Pool()
    monkeypatch.setattr(demo_api, "create_db_pool", lambda _settings: pool)
    monkeypatch.setattr(
        demo_api.RagRepository,
        "from_pool",
        classmethod(lambda _cls, _pool: ContractRepository()),
    )
    monkeypatch.setattr(
        demo_api, "load_general_relevance_policy", policy_loader, raising=False
    )
    monkeypatch.setattr(demo_api, "load_poster_serving_authority", poster_loader)
    monkeypatch.setattr(
        demo_api,
        "VertexEmbeddingClient",
        lambda *_args: calls.append("embedding") or object(),
    )
    monkeypatch.setattr(
        demo_api,
        "VertexGenerationClient",
        lambda *_args: calls.append("generation") or object(),
    )
    monkeypatch.setattr(demo_api, "QueryService", query_service)
    monkeypatch.setattr(
        demo_api.storage,
        "Client",
        lambda **_kwargs: calls.append("gcs") or object(),
    )

    services = demo_api._runtime_services(_runtime_environ())

    assert calls == [
        "query_ready_contract",
        "policy",
        "poster_authority",
        "embedding",
        "generation",
        "query_service",
        "gcs",
    ]
    assert query_arguments["kwargs"] == {
        "expected_relevance_policy_sha256": _R2_RELEVANCE_POLICY_SHA256
    }
    services.closeable.close()
    assert calls[-1] == "pool_close"


def test_runtime_contract_accepts_the_exact_r3_schema_and_bound_authorities(
    settings: DemoSettings,
) -> None:
    """Breaks if an ingested R3 release still cannot start the Cloud Run service."""
    manifest_sha256 = "3" * 64
    relevance_sha256 = "4" * 64
    poster_sha256 = "5" * 64
    r3_settings = replace(
        settings,
        release_id="v1.2-demo-r3",
        release_manifest_sha256=manifest_sha256,
        relevance_policy_sha256=relevance_sha256,
        poster_authority_sha256=poster_sha256,
    )
    r3_contract = replace(
        _r2_release_contract(),
        schema_version="1.3",
        rag_release_id="v1.2-demo-r3",
        manifest_sha256=manifest_sha256,
        relevance_policy_sha256=relevance_sha256,
        poster_authority_sha256=poster_sha256,
    )

    demo_api._assert_runtime_contract(r3_settings, r3_contract)


def test_r3_runtime_accepts_release_scoped_restricted_poster_authority(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Breaks if startup re-imposes the R2-only source-rights shape."""
    calls: list[str] = []
    contract = _r3_release_contract()
    environ = _runtime_environ()
    environ.update(
        {
            "RAG_RELEASE_ID": _R3_RELEASE_ID,
            "RAG_RELEASE_MANIFEST_SHA256": _R3_RELEASE_MANIFEST_SHA256,
            "RAG_RELEVANCE_POLICY_SHA256": _R3_RELEVANCE_POLICY_SHA256,
            "RAG_POSTER_AUTHORITY_SHA256": POSTER_SERVING_AUTHORITY_V3_SHA256,
        }
    )

    class Pool:
        def close(self) -> None:
            calls.append("pool_close")

    class ContractRepository(FakeRepository):
        def query_ready_contract(self, release_id: str) -> RagReleaseContract:
            calls.append("query_ready_contract")
            assert release_id == _R3_RELEASE_ID
            return contract

    pool = Pool()
    monkeypatch.setattr(demo_api, "create_db_pool", lambda _settings: pool)
    monkeypatch.setattr(
        demo_api.RagRepository,
        "from_pool",
        classmethod(lambda _cls, _pool: ContractRepository()),
    )
    monkeypatch.setattr(
        demo_api,
        "VertexEmbeddingClient",
        lambda *_args: calls.append("embedding") or object(),
    )
    monkeypatch.setattr(
        demo_api,
        "VertexGenerationClient",
        lambda *_args: calls.append("generation") or object(),
    )
    monkeypatch.setattr(
        demo_api,
        "QueryService",
        lambda *_args, **_kwargs: calls.append("query_service") or FakeQueryService(),
    )
    monkeypatch.setattr(
        demo_api.storage,
        "Client",
        lambda **_kwargs: calls.append("gcs") or object(),
    )

    services = demo_api._runtime_services(environ)

    assert calls == [
        "query_ready_contract",
        "embedding",
        "generation",
        "query_service",
        "gcs",
    ]
    services.closeable.close()
    assert calls[-1] == "pool_close"


@pytest.mark.parametrize(
    "field_name",
    ("RAG_RELEVANCE_POLICY_SHA256", "RAG_POSTER_AUTHORITY_SHA256"),
)
def test_well_formed_env_authority_drift_fails_after_db_binding_before_remotes(
    monkeypatch: pytest.MonkeyPatch,
    field_name: str,
) -> None:
    calls: list[str] = []

    class Pool:
        def close(self) -> None:
            calls.append("pool_close")

    pool = Pool()
    monkeypatch.setattr(demo_api, "create_db_pool", lambda _settings: pool)
    monkeypatch.setattr(
        demo_api.RagRepository,
        "from_pool",
        classmethod(lambda _cls, _pool: FakeRepository()),
    )
    monkeypatch.setattr(
        demo_api,
        "VertexEmbeddingClient",
        lambda *_args: calls.append("embedding") or object(),
    )
    monkeypatch.setattr(
        demo_api.storage,
        "Client",
        lambda **_kwargs: calls.append("gcs") or object(),
    )
    environ = _runtime_environ()
    environ[field_name] = "0" * 64

    with pytest.raises(RagDatabaseError, match="runtime service startup failed"):
        demo_api._runtime_services(environ)

    assert calls == ["pool_close"]


def test_runtime_lifespan_uses_pooled_repository_and_closes_pool_exactly_once(
    monkeypatch: pytest.MonkeyPatch,
    repository: FakeRepository,
    query_service: FakeQueryService,
) -> None:
    """Breaks if runtime retains a shared connection or lifespan leaks its opened pool."""

    class Pool:
        def __init__(self) -> None:
            self.close_calls = 0

        def close(self) -> None:
            self.close_calls += 1

    pool = Pool()
    observed: dict[str, object] = {}

    def from_pool(cls: type[object], received_pool: object) -> FakeRepository:
        observed["repository_class"] = cls
        observed["pool"] = received_pool
        return repository

    monkeypatch.setattr(demo_api, "create_db_pool", lambda _settings: pool, raising=False)
    monkeypatch.setattr(
        demo_api.RagRepository, "from_pool", classmethod(from_pool), raising=False
    )
    monkeypatch.setattr(demo_api, "VertexEmbeddingClient", lambda *_args: object())
    monkeypatch.setattr(demo_api, "VertexGenerationClient", lambda *_args: object())
    monkeypatch.setattr(
        demo_api, "QueryService", lambda *_args, **_kwargs: query_service
    )
    monkeypatch.setattr(demo_api.storage, "Client", lambda **_kwargs: object())

    application = create_app(environ=_runtime_environ())
    with TestClient(application, base_url="https://testserver") as test_client:
        assert test_client.get("/health").json() == {"status": "ok"}
        services = application.state.demo_services
        assert services.repository is repository
        assert services.query_service is query_service
        assert services.closeable is pool
        assert pool.close_calls == 0

    assert observed == {"repository_class": demo_api.RagRepository, "pool": pool}
    assert pool.close_calls == 1


def test_runtime_verifies_selected_release_before_vertex_or_gcs_construction(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Breaks if remote clients exist before the immutable DB release is authenticated."""
    calls: list[str] = []

    class Pool:
        def __init__(self) -> None:
            self.close_calls = 0

        def close(self) -> None:
            self.close_calls += 1
            calls.append("pool_close")

    class OrderedRepository(FakeRepository):
        def query_ready_contract(self, release_id: str) -> RagReleaseContract:
            calls.append("query_ready_contract")
            return super().query_ready_contract(release_id)

    pool = Pool()
    repository = OrderedRepository()
    monkeypatch.setattr(demo_api, "create_db_pool", lambda _settings: pool, raising=False)
    monkeypatch.setattr(
        demo_api.RagRepository,
        "from_pool",
        classmethod(lambda _cls, _pool: repository),
        raising=False,
    )
    monkeypatch.setattr(
        demo_api,
        "VertexEmbeddingClient",
        lambda *_args: calls.append("embedding") or object(),
    )
    monkeypatch.setattr(
        demo_api,
        "VertexGenerationClient",
        lambda *_args: calls.append("generation") or object(),
    )
    monkeypatch.setattr(
        demo_api,
        "QueryService",
        lambda *_args, **_kwargs: calls.append("query_service") or FakeQueryService(),
    )
    monkeypatch.setattr(
        demo_api.storage,
        "Client",
        lambda **_kwargs: calls.append("gcs") or object(),
    )

    services = demo_api._runtime_services(_runtime_environ())

    assert calls == [
        "query_ready_contract",
        "embedding",
        "generation",
        "query_service",
        "gcs",
    ]
    assert pool.close_calls == 0
    services.closeable.close()
    assert pool.close_calls == 1


@pytest.mark.parametrize(
    ("field_name", "value"),
    (
        ("rag_release_id", "v1.2-other"),
        ("manifest_sha256", "cd" * 32),
        ("embedding_model", "other-model"),
        ("embedding_dimension", 1024),
        ("generation_model", "other-generator"),
        ("access_mode", "public"),
        ("relevance_policy_sha256", "0" * 64),
        ("poster_authority_sha256", "1" * 64),
    ),
)
def test_runtime_release_identity_mismatch_closes_pool_once_before_remote_clients(
    monkeypatch: pytest.MonkeyPatch,
    field_name: str,
    value: object,
) -> None:
    calls: list[str] = []

    class Pool:
        def __init__(self) -> None:
            self.close_calls = 0

        def close(self) -> None:
            self.close_calls += 1

    class MismatchedRepository(FakeRepository):
        def query_ready_contract(self, release_id: str) -> RagReleaseContract:
            del release_id
            return _r2_release_contract(**{field_name: value})

    pool = Pool()
    monkeypatch.setattr(demo_api, "create_db_pool", lambda _settings: pool, raising=False)
    monkeypatch.setattr(
        demo_api.RagRepository,
        "from_pool",
        classmethod(lambda _cls, _pool: MismatchedRepository()),
        raising=False,
    )
    monkeypatch.setattr(
        demo_api,
        "VertexEmbeddingClient",
        lambda *_args: calls.append("embedding") or object(),
    )
    monkeypatch.setattr(
        demo_api.storage,
        "Client",
        lambda **_kwargs: calls.append("gcs") or object(),
    )

    with pytest.raises(RagDatabaseError, match="runtime service startup failed"):
        demo_api._runtime_services(_runtime_environ())

    assert calls == []
    assert pool.close_calls == 1


def test_runtime_service_construction_failure_closes_pool_and_redacts_error(
    monkeypatch: pytest.MonkeyPatch,
    repository: FakeRepository,
) -> None:
    """Breaks if startup after pool readiness leaks the pool or dependency details."""

    class Pool:
        def __init__(self) -> None:
            self.close_calls = 0

        def close(self) -> None:
            self.close_calls += 1

    pool = Pool()
    monkeypatch.setattr(demo_api, "create_db_pool", lambda _settings: pool, raising=False)
    monkeypatch.setattr(
        demo_api.RagRepository,
        "from_pool",
        classmethod(lambda _cls, _pool: repository),
        raising=False,
    )

    def fail_embedding(*_args: object) -> object:
        raise RuntimeError("postgresql://rag_user:runtime-db-secret@cloudsql/rag")

    monkeypatch.setattr(demo_api, "VertexEmbeddingClient", fail_embedding)

    with pytest.raises(RagDatabaseError, match="runtime service startup failed") as raised:
        demo_api._runtime_services(_runtime_environ())

    formatted = "".join(traceback.format_exception(raised.value))
    assert pool.close_calls == 1
    assert "runtime-db-secret" not in str(raised.value)
    assert "postgresql" not in str(raised.value)
    assert "runtime-db-secret" not in formatted
    assert "postgresql" not in formatted


def _assert_redacted_stage_log(
    caplog: pytest.LogCaptureFixture, *, stage: str, exception_type: str, secret: str
) -> None:
    messages = [
        record.getMessage()
        for record in caplog.records
        if record.name == "hk_movie_rag.demo_api"
    ]
    assert messages == [f"stage={stage} exception_type={exception_type}"]
    assert secret not in caplog.text
    assert "postgresql" not in caplog.text


def test_pool_checkout_timeout_is_controlled_503_with_redacted_config_db_log(
    settings: DemoSettings,
    query_service: FakeQueryService,
    storage: FakePosterStorage,
    caplog: pytest.LogCaptureFixture,
) -> None:
    """Breaks if pool pressure becomes a 500 or exposes checkout/connection details."""
    secret = "postgresql://rag_user:checkout-secret@cloudsql/rag"

    class TimeoutPool:
        def connection(self, *, timeout: float) -> object:
            assert 0 < timeout <= 30
            raise TimeoutError(secret)

    repository = rag_db.RagRepository.from_pool(TimeoutPool())
    application = create_app(
        settings=settings,
        query_service=query_service,
        repository=repository,
        poster_storage=storage,
    )
    caplog.set_level(logging.WARNING, logger="hk_movie_rag.demo_api")
    with TestClient(application, base_url="https://testserver") as test_client:
        response = test_client.get("/api/config")

    assert response.status_code == 503
    assert response.json() == {"detail": "configuration temporarily unavailable"}
    assert "checkout-secret" not in response.text
    _assert_redacted_stage_log(
        caplog, stage="config_db", exception_type="RagDatabaseError", secret="checkout-secret"
    )


def test_chat_backend_failure_logs_only_stage_and_exception_type(
    settings: DemoSettings,
    repository: FakeRepository,
    storage: FakePosterStorage,
    caplog: pytest.LogCaptureFixture,
) -> None:
    secret = "postgresql://rag_user:chat-secret@cloudsql/rag"
    application = create_app(
        settings=settings,
        query_service=FakeQueryService(failure=GroundingError(secret)),
        repository=repository,
        poster_storage=storage,
    )
    caplog.set_level(logging.WARNING, logger="hk_movie_rag.demo_api")
    with TestClient(application, base_url="https://testserver") as test_client:
        response = test_client.post("/api/chat", json={"question": "醉拳的美學？"})

    assert response.status_code == 503
    _assert_redacted_stage_log(
        caplog, stage="chat_backend", exception_type="GroundingError", secret="chat-secret"
    )


def test_poster_database_failure_logs_only_stage_and_exception_type(
    settings: DemoSettings,
    query_service: FakeQueryService,
    storage: FakePosterStorage,
    caplog: pytest.LogCaptureFixture,
) -> None:
    secret = "postgresql://rag_user:poster-db-secret@cloudsql/rag"

    class FailingRepository(FakeRepository):
        def get_poster_asset(
            self, release_id: str, movie_id: str
        ) -> dict[str, object] | None:
            del release_id, movie_id
            raise RuntimeError(secret)

    application = create_app(
        settings=settings,
        query_service=query_service,
        repository=FailingRepository(),
        poster_storage=storage,
    )
    caplog.set_level(logging.WARNING, logger="hk_movie_rag.demo_api")
    with TestClient(application, base_url="https://testserver") as test_client:
        response = test_client.get("/api/posters/1978_ZQ_001")

    assert response.status_code == 503
    _assert_redacted_stage_log(
        caplog, stage="poster_db", exception_type="RuntimeError", secret="poster-db-secret"
    )


def test_poster_storage_failure_logs_only_stage_and_exception_type(
    settings: DemoSettings,
    query_service: FakeQueryService,
    repository: FakeRepository,
    caplog: pytest.LogCaptureFixture,
) -> None:
    secret = "gs://private-demo-bucket/poster-storage-secret.webp"

    class FailingStorage:
        def open_object(self, *_args: object, **_kwargs: object) -> PosterContent:
            raise RuntimeError(secret)

    application = create_app(
        settings=settings,
        query_service=query_service,
        repository=repository,
        poster_storage=FailingStorage(),
    )
    caplog.set_level(logging.WARNING, logger="hk_movie_rag.demo_api")
    with TestClient(application, base_url="https://testserver") as test_client:
        response = test_client.get("/api/posters/1978_ZQ_001")

    assert response.status_code == 503
    _assert_redacted_stage_log(
        caplog,
        stage="poster_storage",
        exception_type="RuntimeError",
        secret="poster-storage-secret",
    )


@pytest.mark.parametrize(
    "body",
    [
        {},
        {"question": "   "},
        {"question": "問題", "unexpected": "field"},
        {"question": "問" * 1001},
    ],
)
def test_chat_rejects_malformed_or_out_of_bounds_input_with_controlled_error(
    client: TestClient, body: dict[str, object]
) -> None:
    """Breaks if malformed JSON reaches Vertex or framework validation details escape."""
    response = client.post("/api/chat", json=body)

    assert response.status_code == 422
    assert response.json() == {"detail": "invalid request"}


def test_chat_dependency_failure_does_not_leak_stack_or_dependency_message(
    settings: DemoSettings,
    repository: FakeRepository,
    storage: FakePosterStorage,
) -> None:
    """Breaks if a DB/Vertex exception body reaches the browser."""
    query = FakeQueryService(failure=GroundingError("postgresql://user:secret@host/db"))
    application = create_app(
        settings=settings,
        query_service=query,
        repository=repository,
        poster_storage=storage,
    )
    with TestClient(application, base_url="https://testserver") as test_client:
        response = test_client.post("/api/chat", json={"question": "醉拳的美學？"})

    assert response.status_code == 503
    assert response.json() == {"detail": "chat temporarily unavailable"}
    assert "secret" not in response.text
    assert "postgresql" not in response.text


def test_chat_rejects_non_same_origin_movie_media_even_if_a_dependency_regresses(
    settings: DemoSettings,
    repository: FakeRepository,
    storage: FakePosterStorage,
) -> None:
    """Breaks if an upstream regression can serialize a model-controlled external poster URL."""

    class UnsafeQueryService:
        def answer(
            self, question: str, history: tuple[ConversationExchange, ...] = ()
        ) -> ChatAnswer:
            del question, history
            return ChatAnswer(
                answer_markdown="受控答案。[metadata:movie]",
                citations=(
                    Citation(
                        citation_id="metadata:movie",
                        movie_id="movie",
                        movie_title="電影",
                        source_kind="movie_metadata",
                        page_number=None,
                        source_filename=None,
                        excerpt="資料",
                    ),
                ),
                movies=(
                    MovieCard(
                        movie_id="movie",
                        chinese_title="電影",
                        english_title="Movie",
                        release_date="2000",
                        director="導演",
                        cast="演員",
                        genre="類型",
                        tier="S",
                        pilot_movie=False,
                        poster_url="https://untrusted.invalid/poster.jpg",
                    ),
                ),
            )

    application = create_app(
        settings=settings,
        query_service=UnsafeQueryService(),
        repository=repository,
        poster_storage=storage,
    )
    with TestClient(application, base_url="https://testserver") as test_client:
        response = test_client.post("/api/chat", json={"question": "電影資料？"})

    assert response.status_code == 503
    assert response.json() == {"detail": "chat temporarily unavailable"}
    assert "untrusted.invalid" not in response.text


@pytest.mark.parametrize("movie_id", ["1978_ZQ_001", "1982_ZJPD_001", "manual_movie"])
def test_poster_proxy_streams_only_approved_exact_db_object_with_private_headers(
    client: TestClient,
    storage: FakePosterStorage,
    repository: FakeRepository,
    movie_id: str,
) -> None:
    """Breaks if approved private poster bytes, MIME, ETag, rights, or exact key drift."""
    response = client.get(f"/api/posters/{movie_id}")

    assert response.status_code == 200
    assert response.content == _POSTER_BYTES
    assert response.headers["content-type"] == "image/webp"
    assert response.headers["etag"] == '"poster-etag"'
    assert response.headers["cache-control"] == "private, no-store"
    assert response.headers["x-rag-rights-status"] == "unknown"
    expected_name = f"assets/posters/derived/{movie_id}.webp"
    assert storage.calls[-1] == (
        "private-demo-bucket",
        expected_name,
        _POSTER_SHA256,
        len(_POSTER_BYTES),
        "image/webp",
        16 * 1024 * 1024,
    )
    asset = repository.assets[movie_id]
    assert asset is not None
    assert asset["content_sha256"] == _ORIGINAL_POSTER_SHA256
    assert asset["derived_content_sha256"] == _POSTER_SHA256
    assert "gs://" not in str(response.headers)


@pytest.mark.parametrize(
    "rights_status", ["missing", "empty", "cleared", "forbidden"]
)
def test_poster_proxy_rejects_every_non_unknown_rights_status_before_storage(
    client: TestClient,
    storage: FakePosterStorage,
    rights_status: str,
) -> None:
    """Breaks if a header-safe but unauthorized source-rights state can serve bytes."""
    response = client.get(f"/api/posters/rights_{rights_status}")

    assert response.status_code == 404
    assert response.json() == {"detail": "poster not found"}
    assert storage.calls == []


def test_public_poster_proxy_serves_operator_approved_restricted_rights_through_hash_gate(
    client: TestClient,
) -> None:
    """Breaks if public exposure bypasses or rejects the governed R3 poster identity."""
    response = client.get("/api/posters/rights_restricted")

    assert response.status_code == 200
    assert response.content == _POSTER_BYTES
    assert response.headers["cache-control"] == "private, no-store"
    assert response.headers["x-rag-rights-status"] == "restricted"


@pytest.mark.parametrize(
    "path",
    [
        "/api/posters/conflicted_movie",
        "/api/posters/missing_movie",
        "/api/posters/wrong_hash",
        "/api/posters/wrong_length",
        "/api/posters/wrong_mime",
        "/api/posters/absolute_gs",
        "/api/posters/absolute_path",
        "/api/posters/parent_path",
        "/api/posters/alternate_prefix",
        "/api/posters/wrong_movie",
        "/api/posters/wrong_extension",
        "/api/posters/query_key",
        "/api/posters/fragment_key",
        "/api/posters/redirect_uri",
        "/api/posters/%2E%2E%2Fsecret",
        "/api/posters/bad%00id",
    ],
)
def test_poster_proxy_rejects_conflict_missing_tampering_and_never_accepts_object_path(
    client: TestClient, path: str
) -> None:
    """Breaks if a caller can select an object path or bypass governed poster state."""
    response = client.get(path)

    assert response.status_code in {404, 422}
    assert "gs://" not in response.text


def test_all_113_nonapproved_release_rows_are_unavailable_without_storage_reads(
    settings: DemoSettings,
    query_service: FakeQueryService,
    storage: FakePosterStorage,
) -> None:
    """Breaks if missing/conflict/placeholder rows enter the 4,545-object allowlist."""
    statuses = ("content_conflict", "missing", "placeholder")
    assets = {
        f"unavailable_{index:03d}": _poster_asset(
            f"unavailable_{index:03d}", quality_status=statuses[index % len(statuses)]
        )
        for index in range(113)
    }
    application = create_app(
        settings=settings,
        query_service=query_service,
        repository=FakeRepository(assets=assets),
        poster_storage=storage,
    )
    with TestClient(application, base_url="https://testserver") as test_client:
        statuses_seen = [
            test_client.get(f"/api/posters/{movie_id}").status_code for movie_id in assets
        ]

    assert statuses_seen == [404] * 113
    assert storage.calls == []


@pytest.mark.parametrize(
    "missing_field",
    ["derived_content_sha256", "derived_byte_length", "derived_mime_type"],
)
def test_incomplete_derived_identity_is_rejected_before_storage(
    settings: DemoSettings,
    query_service: FakeQueryService,
    missing_field: str,
) -> None:
    """Breaks if the proxy falls back to the original hash or guesses WebP metadata."""
    asset = _poster_asset("incomplete_movie")
    del asset[missing_field]
    storage = FakePosterStorage()
    application = create_app(
        settings=settings,
        query_service=query_service,
        repository=FakeRepository(assets={"incomplete_movie": asset}),
        poster_storage=storage,
    )
    with TestClient(application, base_url="https://testserver") as test_client:
        response = test_client.get("/api/posters/incomplete_movie")

    assert response.status_code == 404
    assert storage.calls == []


def test_invalid_storage_adapter_result_returns_controlled_poster_error(
    settings: DemoSettings,
    query_service: FakeQueryService,
    repository: FakeRepository,
) -> None:
    """Breaks if a regressed storage adapter turns poster validation into an uncontrolled 500."""

    class InvalidPosterStorage:
        def open_object(self, *_args: object, **_kwargs: object) -> object:
            return object()

    application = create_app(
        settings=settings,
        query_service=query_service,
        repository=repository,
        poster_storage=InvalidPosterStorage(),  # type: ignore[arg-type]
    )
    with TestClient(
        application, base_url="https://testserver", raise_server_exceptions=False
    ) as test_client:
        response = test_client.get("/api/posters/1978_ZQ_001")

    assert response.status_code == 404
    assert response.json() == {"detail": "poster not found"}


def test_removed_logout_route_cannot_relock_public_data_routes(client: TestClient) -> None:
    """Breaks if a stale logout surface can change the public serving contract."""
    assert client.get("/api/config").status_code == 200

    response = client.delete("/api/session")

    assert response.status_code == 404
    assert "set-cookie" not in response.headers
    assert client.get("/api/config").status_code == 200


def test_explicit_offline_factory_runs_same_public_chat_and_poster_contract_without_cloud() -> None:
    """Breaks if local browser smoke needs DB, GCS, or Vertex credentials."""
    application = create_offline_app(environ={"RAG_DEMO_OFFLINE": "1"})
    with TestClient(application, base_url="https://testserver") as test_client:
        answer = test_client.post("/api/chat", json={"question": "醉拳的視覺美學？"})
        config = test_client.get("/api/config")
        drunken_poster = test_client.get("/api/posters/1978_ZQ_001")
        aces_poster = test_client.get("/api/posters/1982_ZJPD_001")
        unavailable = test_client.get("/api/posters/unavailable_movie")

    assert answer.status_code == 200
    assert config.status_code == 200
    assert config.json()["poster_authority_sha256"] == _POSTER_AUTHORITY_SHA256
    assert answer.json()["citations"][0]["page_number"] == 2
    assert drunken_poster.status_code == 200
    assert aces_poster.status_code == 200
    assert drunken_poster.headers["content-type"].startswith("image/")
    assert aces_poster.headers["content-type"].startswith("image/")
    assert unavailable.status_code == 404


def test_offline_factory_refuses_cloud_run_environment() -> None:
    """Breaks if a deployment can accidentally start fake/offline data on Cloud Run."""
    with pytest.raises(DemoConfigurationError, match="offline demo is local-only"):
        create_offline_app(
            environ={
                "RAG_DEMO_OFFLINE": "1",
                "K_SERVICE": "hk-movie-rag-demo",
            }
        )


@dataclass
class _StorageBlob:
    body: bytes
    content_type: str = "image/webp"
    etag: str = '"gcs-etag"'
    declared_size: int | None = None

    @property
    def size(self) -> int:
        return len(self.body) if self.declared_size is None else self.declared_size

    def open(self, mode: str) -> BytesIO:
        assert mode == "rb"
        return BytesIO(self.body)


@dataclass
class _StorageBucket:
    blob: _StorageBlob | None
    object_names: list[str] = field(default_factory=list)

    def get_blob(self, object_name: str) -> _StorageBlob | None:
        self.object_names.append(object_name)
        return self.blob


@dataclass
class _StorageClient:
    fake_bucket: _StorageBucket
    bucket_names: list[str] = field(default_factory=list)

    def bucket(self, bucket_name: str) -> _StorageBucket:
        self.bucket_names.append(bucket_name)
        return self.fake_bucket


def test_gcs_adapter_hashes_and_bounds_the_exact_private_object_before_streaming() -> None:
    """Breaks if live GCS bytes are returned before exact key, size, and SHA verification."""
    bucket = _StorageBucket(_StorageBlob(_POSTER_BYTES))
    client = _StorageClient(bucket)
    adapter = GcsPosterStorage(client)  # type: ignore[arg-type]

    content = adapter.open_object(
        "private-demo-bucket",
        "posters/1978_ZQ_001.webp",
        expected_sha256=_POSTER_SHA256,
        expected_size=len(_POSTER_BYTES),
        expected_mime_type="image/webp",
        max_bytes=1024,
    )

    try:
        assert content.stream.read() == _POSTER_BYTES
        assert content.size == len(_POSTER_BYTES)
        assert content.content_type == "image/webp"
        assert content.etag == '"gcs-etag"'
        assert content.content_sha256 == _POSTER_SHA256
        assert client.bucket_names == ["private-demo-bucket"]
        assert bucket.object_names == ["posters/1978_ZQ_001.webp"]
    finally:
        content.stream.close()


@pytest.mark.parametrize(
    "blob, expected_sha256, expected_size, expected_mime_type, max_bytes",
    [
        (_StorageBlob(_POSTER_BYTES), "0" * 64, len(_POSTER_BYTES), "image/webp", 1024),
        (
            _StorageBlob(_POSTER_BYTES, declared_size=len(_POSTER_BYTES) - 1),
            _POSTER_SHA256,
            len(_POSTER_BYTES),
            "image/webp",
            1024,
        ),
        (
            _StorageBlob(_POSTER_BYTES),
            _POSTER_SHA256,
            len(_POSTER_BYTES) - 1,
            "image/webp",
            1024,
        ),
        (
            _StorageBlob(_POSTER_BYTES, content_type="image/jpeg"),
            _POSTER_SHA256,
            len(_POSTER_BYTES),
            "image/webp",
            1024,
        ),
        (
            _StorageBlob(_POSTER_BYTES),
            _POSTER_SHA256,
            len(_POSTER_BYTES),
            "image/webp",
            len(_POSTER_BYTES) - 1,
        ),
    ],
)
def test_gcs_adapter_fails_closed_on_hash_size_or_bound_mismatch(
    blob: _StorageBlob,
    expected_sha256: str,
    expected_size: int,
    expected_mime_type: str,
    max_bytes: int,
) -> None:
    """Breaks if truncated, replaced, or oversized GCS content can reach the proxy."""
    adapter = GcsPosterStorage(_StorageClient(_StorageBucket(blob)))  # type: ignore[arg-type]

    with pytest.raises(PosterNotFoundError, match="poster is unavailable"):
        adapter.open_object(
            "private-demo-bucket",
            "posters/1978_ZQ_001.webp",
            expected_sha256=expected_sha256,
            expected_size=expected_size,
            expected_mime_type=expected_mime_type,
            max_bytes=max_bytes,
        )
