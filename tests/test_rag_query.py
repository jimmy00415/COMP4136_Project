"""Grounding contracts for retrieval and Vertex-backed movie answers."""

from __future__ import annotations

import json
import re
from collections.abc import Iterator
from contextlib import contextmanager
from dataclasses import asdict, dataclass, field, replace
from types import SimpleNamespace

import pytest

from hk_movie_rag import retrieval
from hk_movie_rag.rag_bundle import RagReleaseContract, RagReleaseCounts
from hk_movie_rag.rag_db import RagDatabaseError, RagRepository, ReleaseState
from hk_movie_rag.rag_query import (
    ChatAnswer,
    EvidencePassage,
    GeneratedAnswer,
    GroundingError,
    QueryService,
    QueryValidationError,
    _bounded_analysis_markdown,
    _deterministic_qualitative_citation_ids,
    _deterministic_scoped_recommendation,
    _evidence_from_record,
    _exact_target_movie_ids,
    _extractive_qualitative_answer,
    _relevant_excerpt,
    _selected_deep_recommendation_evidence,
    _selected_qualitative_citation_ids,
    _selected_recommendation_evidence,
    _validated_bounded_analysis_bindings,
    _validated_selected_pdf_citation_ids,
)
from hk_movie_rag.relevance import has_strong_out_of_domain_signal, load_general_relevance_policy
from hk_movie_rag.retrieval import ConversationExchange, RecommendationPlan, RecommendationSearch
from hk_movie_rag.vertex_clients import VertexGenerationClient, VertexGenerationError

RELEASE_ID = "v1.2-demo"
RELEASE_MANIFEST_SHA256 = "ab" * 32
RELEVANCE_POLICY_SHA256 = load_general_relevance_policy().artifact_sha256


def _passage(
    passage_id: str,
    movie_id: str,
    *,
    chinese_title: str,
    english_title: str,
    passage_kind: str = "metadata",
    page_number: int = 0,
    source_filename: str | None = None,
    poster_available: bool = False,
    distance: float = 0.2,
    release_date: str = "1978-10-05",
    genre: str = "動作",
    tier: str = "B",
    tier_reason: str = "fixture tier",
    pilot_movie: bool = False,
    director: str = "測試導演",
    cast: str = "測試演員",
) -> dict[str, object]:
    body = f"{chinese_title} 的受控證據：{passage_id}"
    if passage_kind == "metadata":
        body = "\n".join(
            (
                f"movie_id: {movie_id}",
                f"chinese_title: {chinese_title}",
                f"english_title: {english_title}",
                f"release_date: {release_date}",
                "production_region: Hong Kong",
                f"director: {director}",
                "screenwriter: 測試編劇",
                f"cast: {cast}",
                f"genre: {genre}",
                "runtime_minutes: 100",
                "production_company: 測試公司",
                "data_source: fixture",
            )
        )
    return {
        "passage_id": passage_id,
        "movie_id": movie_id,
        "passage_kind": passage_kind,
        "body": body,
        "page_number": page_number,
        "document_id": "deep-doc" if passage_kind == "pdf" else None,
        "source_filename": source_filename,
        "movie": {
            "movie_id": movie_id,
            "chinese_title": chinese_title,
            "english_title": english_title,
            "release_date": release_date,
            "director": director,
            "cast": cast,
            "genre": genre,
            "tier": tier,
            "tier_reason": tier_reason,
            "pilot_movie": pilot_movie,
        },
        "poster_available": poster_available,
        "distance": distance,
    }


@dataclass
class FakeRepository:
    passages: list[dict[str, object]]
    status: str = "active"
    embedding_model: str = "gemini-embedding-2"
    embedding_dimension: int = 768
    calls: list[dict[str, object]] = field(default_factory=list)
    recommendation_calls: list[RecommendationPlan] = field(default_factory=list)
    deep_recommendation_calls: list[RecommendationPlan] = field(default_factory=list)
    recommendation_total_matches: int | None = None
    state_calls: list[str] = field(default_factory=list)
    query_ready: bool = True
    readiness_calls: list[str] = field(default_factory=list)
    explicit_target_calls: list[tuple[str, str]] = field(default_factory=list)
    person_resolutions: dict[str, tuple[object, ...]] = field(default_factory=dict)
    ambiguous_person_questions: set[str] = field(default_factory=set)
    person_resolution_calls: list[tuple[str, str]] = field(default_factory=list)
    approved_poster_ids: frozenset[str] | None = None
    poster_batch_calls: list[tuple[str, tuple[str, ...]]] = field(default_factory=list)

    def release_state(self, release_id: str) -> ReleaseState:
        self.state_calls.append(release_id)
        return ReleaseState(
            self.status,
            self.embedding_model,
            self.embedding_dimension,
            len(self.passages),
            RELEASE_MANIFEST_SHA256,
        )

    def assert_query_ready(self, release_id: str) -> None:
        self.readiness_calls.append(release_id)
        if not self.query_ready:
            raise RagDatabaseError("facets: expected 4658, got 4657")

    def resolve_explicit_movie_ids(
        self, release_id: str, question: str
    ) -> tuple[str, ...]:
        return self.resolve_explicit_movie_context(release_id, question).movie_ids

    def resolve_explicit_movie_context(
        self, release_id: str, question: str
    ) -> retrieval.ExplicitMovieResolution:
        self.explicit_target_calls.append((release_id, question))
        evidence = tuple(_evidence_from_record(passage) for passage in self.passages)
        candidates = tuple(
            (
                passage.movie_id,
                str(passage.movie.get("chinese_title", "")),
                str(passage.movie.get("english_title", "")),
            )
            for passage in evidence
        )
        return retrieval.resolve_explicit_movie_identity_context(question, candidates)

    def resolve_recommendation_person(
        self, release_id: str, question: str
    ) -> object | None:
        self.person_resolution_calls.append((release_id, question))
        if question in self.ambiguous_person_questions:
            error_type = getattr(
                retrieval, "AmbiguousPersonResolutionError", RuntimeError
            )
            raise error_type("multiple Release person identities")
        resolved = self.person_resolutions.get(question)
        return retrieval.ResolvedPerson(*resolved) if resolved is not None else None

    def search(
        self,
        release_id: str,
        embedding: list[float],
        limit: int = 8,
        *,
        question: str | None = None,
        embedding_model: str | None = None,
        embedding_dimension: int | None = None,
        target_movie_ids: tuple[str, ...] = (),
    ) -> list[dict[str, object]]:
        self.calls.append(
            {
                "release_id": release_id,
                "embedding": embedding,
                "limit": limit,
                "question": question,
                "embedding_model": embedding_model,
                "embedding_dimension": embedding_dimension,
                "target_movie_ids": target_movie_ids,
            }
        )
        if target_movie_ids:
            return [
                passage
                for passage in self.passages
                if passage["movie_id"] in target_movie_ids
            ]
        return self.passages

    def search_recommendations(
        self,
        release_id: str,
        embedding: list[float],
        plan: RecommendationPlan,
    ) -> RecommendationSearch:
        self.recommendation_calls.append(plan)
        total_matches = (
            len(self.passages)
            if self.recommendation_total_matches is None
            else self.recommendation_total_matches
        )
        return RecommendationSearch(tuple(self.passages), total_matches)

    def search_deep_recommendations(
        self,
        release_id: str,
        embedding: list[float],
        plan: RecommendationPlan,
    ) -> RecommendationSearch:
        self.deep_recommendation_calls.append(plan)
        matching: list[dict[str, object]] = []
        seen_movies: set[str] = set()
        requested_genres = {genre.strip() for genre in plan.genres}
        for passage in self.passages:
            if passage.get("passage_kind") != "pdf":
                continue
            movie = passage.get("movie")
            if not isinstance(movie, dict):
                continue
            movie_id = passage.get("movie_id")
            if not isinstance(movie_id, str) or movie_id in seen_movies:
                continue
            if movie_id in plan.excluded_movie_ids:
                continue
            if plan.tier is not None and movie.get("tier") != plan.tier:
                continue
            stored_genres = {
                token.strip()
                for token in re.split(r"[、,，/;；]", str(movie.get("genre", "")))
                if token.strip()
            }
            if requested_genres and requested_genres.isdisjoint(stored_genres):
                continue
            release_date = str(movie.get("release_date", ""))
            release_year = (
                int(release_date[:4])
                if re.fullmatch(r"\d{4}-\d{2}-\d{2}", release_date)
                else None
            )
            if plan.year_from is not None and (
                release_year is None or release_year < plan.year_from
            ):
                continue
            if plan.year_to is not None and (
                release_year is None or release_year > plan.year_to
            ):
                continue
            seen_movies.add(movie_id)
            matching.append(passage)
        candidate_limit = 8 if plan.diversify_decades else plan.requested_count
        return RecommendationSearch(tuple(matching[:candidate_limit]), len(matching))

    def get_approved_poster_assets(
        self, release_id: str, movie_ids: tuple[str, ...]
    ) -> dict[str, dict[str, object]]:
        self.poster_batch_calls.append((release_id, movie_ids))
        approved = self.approved_poster_ids
        if approved is None:
            approved = frozenset(
                str(passage["movie_id"])
                for passage in self.passages
                if passage.get("poster_available") is True
            )
        return {
            movie_id: {"movie_id": movie_id}
            for movie_id in movie_ids
            if movie_id in approved
        }


@dataclass
class FakeEmbeddingClient:
    model: str = "gemini-embedding-2"
    dimension: int = 768
    calls: list[str] = field(default_factory=list)

    def embed_query(self, text: str) -> list[float]:
        self.calls.append(text)
        return [0.125] * 768


@dataclass
class FakeGenerationClient:
    answer: GeneratedAnswer
    calls: list[
        tuple[
            str,
            tuple[EvidencePassage, ...],
            str,
            tuple[str, ...],
            tuple[str, ...],
        ]
    ] = field(default_factory=list)

    def generate_grounded(
        self,
        question: str,
        passages: tuple[EvidencePassage, ...],
        *,
        mode: str,
        conversation_context: tuple[str, ...],
        required_citation_ids: tuple[str, ...],
    ) -> GeneratedAnswer:
        self.calls.append(
            (question, passages, mode, conversation_context, required_citation_ids)
        )
        return self.answer


def _service(
    passages: list[dict[str, object]],
    answer_markdown: str,
    citation_ids: tuple[str, ...],
) -> tuple[QueryService, FakeRepository, FakeEmbeddingClient, FakeGenerationClient]:
    repository = FakeRepository(passages)
    embedder = FakeEmbeddingClient()
    generator = FakeGenerationClient(GeneratedAnswer(answer_markdown, citation_ids))
    service = QueryService(
        RELEASE_ID,
        RELEASE_MANIFEST_SHA256,
        repository,
        embedder,
        generator,
        expected_relevance_policy_sha256=RELEVANCE_POLICY_SHA256,
    )
    return service, repository, embedder, generator


def _two_citation_passages() -> list[dict[str, object]]:
    return [
        _passage(
            "metadata:first",
            "first",
            chinese_title="第一部",
            english_title="First",
        ),
        _passage(
            "metadata:second",
            "second",
            chinese_title="第二部",
            english_title="Second",
        ),
    ]


def test_query_service_validates_relevance_policy_identity_at_construction() -> None:
    """Breaks if a mismatched embedding model survives until the first user request."""
    repository = FakeRepository([])
    embedder = FakeEmbeddingClient(model="other-embedding-model")
    generator = FakeGenerationClient(GeneratedAnswer("unused", ()))

    with pytest.raises(GroundingError, match="relevance policy"):
        QueryService(
            RELEASE_ID,
            RELEASE_MANIFEST_SHA256,
            repository,
            embedder,
            generator,
            expected_relevance_policy_sha256=RELEVANCE_POLICY_SHA256,
        )


def test_query_service_rejects_an_unselected_relevance_policy_digest() -> None:
    repository = FakeRepository([])
    embedder = FakeEmbeddingClient()
    generator = FakeGenerationClient(GeneratedAnswer("unused", ()))

    with pytest.raises(GroundingError, match="relevance policy"):
        QueryService(
            RELEASE_ID,
            RELEASE_MANIFEST_SHA256,
            repository,
            embedder,
            generator,
            expected_relevance_policy_sha256="0" * 64,
        )


def test_query_service_rechecks_manifest_before_embedding_or_query() -> None:
    passage = _passage(
        "metadata:target",
        "target",
        chinese_title="目標片",
        english_title="Target Film",
    )
    service, repository, embedder, generator = _service(
        [passage], "must not be used", ()
    )

    def mismatched_state(_release_id: str) -> ReleaseState:
        return ReleaseState(
            "active", "gemini-embedding-2", 768, 1, "cd" * 32
        )

    repository.release_state = mismatched_state  # type: ignore[method-assign]

    with pytest.raises(GroundingError, match="release identity"):
        service.answer("目標片的導演是誰？")

    assert embedder.calls == []
    assert repository.calls == []
    assert generator.calls == []


@pytest.mark.parametrize("question", ["", "  \n\t  ", "問" * 1001])
def test_answer_rejects_empty_or_overlong_questions_before_remote_calls(question: str) -> None:
    service, repository, embedder, generator = _service([], "", ())

    with pytest.raises(QueryValidationError, match="question"):
        service.answer(question)

    assert repository.calls == []
    assert embedder.calls == []
    assert generator.calls == []


@pytest.mark.parametrize(
    "question",
    (
        "九十年代香港喜劇電影有什麼特點？",
        "香港犯罪電影中常見哪些角色？",
        "香港電影如何表現身份認同？",
        "香港恐怖電影有哪些類型？",
    ),
)
def test_broad_unobservable_analysis_returns_stable_capability_boundary(
    question: str,
) -> None:
    service, repository, embedder, generator = _service([], "unused", ())

    result = service.answer(question)

    assert result == ChatAnswer(
        answer_markdown=(
            "這個問題需要結構化欄位以外的分析證據；目前 Release "
            "不足以可靠判斷這項條件。請改問具體片名，或使用年代、類型、"
            "導演、演員等已治理欄位。"
        ),
        citations=(),
        movies=(),
    )
    assert repository.readiness_calls == []
    assert repository.state_calls == []
    assert repository.calls == []
    assert repository.recommendation_calls == []
    assert repository.deep_recommendation_calls == []
    assert embedder.calls == []
    assert generator.calls == []


@pytest.mark.parametrize(
    ("question", "expected_movie_ids", "expected_passage_ids", "summary_terms"),
    (
        (
            "香港動作喜劇有什麼代表特色？",
            ("1978_ZQ_001", "1982_ZJPD_001"),
            (
                "pdf:drunken-master-deep-analysis-v1:p1",
                "pdf:aces-go-places-deep-analysis-v1:p1",
            ),
            ("動作設計", "長鏡頭", "都市空間", "明快剪輯"),
        ),
        (
            "七十年代功夫片常見什麼元素？",
            ("1978_ZQ_001",),
            ("pdf:drunken-master-deep-analysis-v1:p1",),
            ("長鏡頭", "功夫武打", "動作笑點"),
        ),
        (
            "香港電影裡的師徒關係如何表現？",
            ("1978_ZQ_001",),
            ("pdf:drunken-master-deep-analysis-v1:p2",),
            ("受辱", "訓練", "成長", "師徒關係"),
        ),
        (
            "港產片如何呈現都市空間？",
            ("1982_ZJPD_001",),
            ("pdf:aces-go-places-deep-analysis-v1:p1",),
            ("中環商場", "海底隧道", "貨櫃場", "超現實"),
        ),
        (
            "想看節奏明快的香港動作片",
            ("1982_ZJPD_001",),
            ("pdf:aces-go-places-deep-analysis-v1:p1",),
            ("剪輯節奏明快", "每四至五分鐘", "肢體衝突", "交通工具特技"),
        ),
        (
            "根據提供的深度分析，這些電影如何運用空間、動作與聲音？",
            ("1978_ZQ_001", "1982_ZJPD_001", "1987_FGBR_001"),
            (
                "pdf:drunken-master-deep-analysis-v1:p1",
                "pdf:aces-go-places-deep-analysis-v1:p1",
                "pdf:its-a-mad-mad-mad-world-deep-analysis-v1:p1",
            ),
            ("有限", "長鏡頭", "都市空間", "公屋", "聲音"),
        ),
    ),
)
def test_bounded_analysis_uses_exact_pdf_passages_without_vertex_generalization(
    question: str,
    expected_movie_ids: tuple[str, ...],
    expected_passage_ids: tuple[str, ...],
    summary_terms: tuple[str, ...],
) -> None:
    passages = [
        _passage(
            "pdf:drunken-master-deep-analysis-v1:p1",
            "1978_ZQ_001",
            chinese_title="醉拳",
            english_title="Drunken Master",
            passage_kind="pdf",
            page_number=1,
            source_filename="S 級港⽚-醉拳.pdf",
            poster_available=True,
            genre="功夫 / 動作 / 喜劇",
        ),
        _passage(
            "pdf:drunken-master-deep-analysis-v1:p2",
            "1978_ZQ_001",
            chinese_title="醉拳",
            english_title="Drunken Master",
            passage_kind="pdf",
            page_number=2,
            source_filename="S 級港⽚-醉拳.pdf",
            poster_available=True,
            genre="功夫 / 動作 / 喜劇",
        ),
        _passage(
            "pdf:aces-go-places-deep-analysis-v1:p1",
            "1982_ZJPD_001",
            chinese_title="最佳拍檔",
            english_title="Aces Go Places",
            passage_kind="pdf",
            page_number=1,
            source_filename="S 級港⽚-最佳拍檔.pdf",
            poster_available=True,
            genre="動作 / 喜劇",
        ),
        _passage(
            "pdf:its-a-mad-mad-mad-world-deep-analysis-v1:p1",
            "1987_FGBR_001",
            chinese_title="富貴逼人",
            english_title="It's a Mad, Mad, Mad World",
            passage_kind="pdf",
            page_number=1,
            source_filename="S級港片-富貴逼人.pdf",
            poster_available=True,
            genre="喜劇 / 家庭 / 奇幻",
        ),
    ]
    passages[0]["body"] = "醉拳以功夫武打、長鏡頭與動作笑點形成諧趣。"
    passages[1]["body"] = (
        "視覺置景描述。" * 100
        + "黃飛鴻受辱後拜蘇乞兒為師，訓\n練使師\n徒\n關\n係產生成長與轉變。"
    )
    passages[2]["body"] = (
        "最佳拍檔把中環商場、海底隧道與貨櫃場組織為超現實的遊樂場式鬥智舞台；"
        "影片剪輯節奏明快，幾乎每四至五分鐘安排一次肢體衝突或交通工具特技。"
    )
    passages[3]["body"] = (
        "富貴逼人以固定機位呈現公屋單位的狹窄格局，家庭碰撞依賴空間的擠迫感。"
    )
    service, repository, embedder, generator = _service(passages, "unused", ())

    result = service.answer(question)

    assert tuple(citation.movie_id for citation in result.citations) == expected_movie_ids
    assert tuple(citation.citation_id for citation in result.citations) == expected_passage_ids
    assert tuple(movie.movie_id for movie in result.movies) == expected_movie_ids
    assert all(citation.source_kind == "pdf_page" for citation in result.citations)
    assert all(f"[{citation_id}]" in result.answer_markdown for citation_id in expected_passage_ids)
    assert all(term in result.answer_markdown for term in summary_terms)
    summary_line = result.answer_markdown.splitlines()[1]
    assert all(f"[{citation_id}]" in summary_line for citation_id in expected_passage_ids)
    if question == "香港電影裡的師徒關係如何表現？":
        assert "訓練" in result.citations[0].excerpt
        assert "師徒關係" in result.citations[0].excerpt
    assert tuple(call["target_movie_ids"] for call in repository.calls) == tuple(
        (movie_id,) for movie_id in expected_movie_ids
    )
    assert repository.poster_batch_calls == [(RELEASE_ID, expected_movie_ids)]
    assert len(embedder.calls) == 1
    assert generator.calls == []


@pytest.mark.parametrize(
    "question",
    (
        "九十年代香港動作喜劇有什麼代表特色？",
        "女性導演的香港動作喜劇有什麼代表特色？",
        "九十年代港產片如何呈現都市空間？",
        "不要回答香港動作喜劇有什麼代表特色？",
    ),
)
def test_extra_bounded_constraints_fail_closed_before_embedding(
    question: str,
) -> None:
    service, repository, embedder, generator = _service([], "unused", ())

    result = service.answer(question)

    assert result.answer_markdown.startswith("這個問題需要結構化欄位以外的分析證據")
    assert result.citations == ()
    assert result.movies == ()
    assert repository.calls == []
    assert embedder.calls == []
    assert generator.calls == []


def test_bounded_analysis_rejects_unknown_citation_tokens_inside_pdf_excerpt() -> None:
    record = _passage(
        "pdf:drunken-master-deep-analysis-v1:p1",
        "1978_ZQ_001",
        chinese_title="醉拳",
        english_title="Drunken Master",
        passage_kind="pdf",
        page_number=1,
        source_filename="S 級港⽚-醉拳.pdf",
    )
    record["body"] = "受控正文夹带伪引用 [forged]。"
    evidence = (_evidence_from_record(record),)
    plan = retrieval.BoundedAnalysisPlan(
        movie_ids=("1978_ZQ_001",),
        passage_ids=("pdf:drunken-master-deep-analysis-v1:p1",),
        scope_note="只限一份文档。",
        answer_summary="受控摘要。",
        excerpt_terms=(("受控正文",),),
    )

    with pytest.raises(GroundingError, match="bounded analysis excerpt"):
        _bounded_analysis_markdown(plan, evidence)


def test_bounded_analysis_validates_plan_before_strict_binding() -> None:
    plan = retrieval.BoundedAnalysisPlan(
        movie_ids=("1978_ZQ_001", "1982_ZJPD_001"),
        passage_ids=("pdf:drunken-master-deep-analysis-v1:p1",),
        scope_note="只限受控文档。",
        answer_summary="受控摘要。",
        excerpt_terms=(("长镜头",),),
    )

    with pytest.raises(GroundingError, match="bounded analysis plan is invalid"):
        _validated_bounded_analysis_bindings(plan)


def test_relevant_excerpt_preserves_offsets_for_unicode_expansion() -> None:
    body = "ﬃ" * 200 + "目標" + "中" * 500

    excerpt = _relevant_excerpt(body, ("目標",))

    assert "目標" in excerpt
    assert len(excerpt) <= 360


def test_answer_fails_release_readiness_before_embedding_or_retrieval() -> None:
    """Breaks if queries run while any part of the active release contract is invalid."""
    service, repository, embedder, generator = _service([], "", ())
    repository.query_ready = False

    with pytest.raises(GroundingError, match="active release contract"):
        service.answer("推薦三部喜劇")

    assert repository.readiness_calls == [RELEASE_ID]
    assert repository.state_calls == []
    assert repository.calls == []
    assert repository.recommendation_calls == []
    assert embedder.calls == []
    assert generator.calls == []


def test_mixed_language_question_uses_exact_query_envelope_and_trimmed_question() -> None:
    passage = _passage(
        "metadata:1978_ZQ_001",
        "1978_ZQ_001",
        chinese_title="醉拳",
        english_title="Drunken Master",
    )
    service, repository, embedder, generator = _service(
        [passage],
        "《醉拳》是 Drunken Master。[metadata:1978_ZQ_001]",
        ("metadata:1978_ZQ_001",),
    )

    answer = service.answer("  醉拳 / Drunken Master 是誰主演？  ")

    assert answer.citations[0].citation_id == "metadata:1978_ZQ_001"
    assert embedder.calls == [
        "task: search result | query: 醉拳 / Drunken Master 是誰主演？"
    ]
    assert repository.calls[0]["question"] == "醉拳 / Drunken Master 是誰主演？"
    assert repository.calls[0]["limit"] == 8
    assert repository.calls[0]["embedding_model"] == "gemini-embedding-2"
    assert repository.calls[0]["embedding_dimension"] == 768
    assert repository.state_calls == [RELEASE_ID]
    assert generator.calls == []


def test_query_rejects_active_fallback_release_with_primary_embedding_client() -> None:
    passage = _passage(
        "metadata:1978_ZQ_001",
        "1978_ZQ_001",
        chinese_title="醉拳",
        english_title="Drunken Master",
    )
    service, repository, embedder, generator = _service(
        [passage],
        "不得使用。[metadata:1978_ZQ_001]",
        ("metadata:1978_ZQ_001",),
    )
    repository.embedding_model = "gemini-embedding-001"

    with pytest.raises(GroundingError, match="query embedding contract"):
        service.answer("醉拳是誰主演？")

    assert repository.state_calls == [RELEASE_ID]
    assert repository.calls == []
    assert embedder.calls == []
    assert generator.calls == []


@pytest.mark.parametrize(
    ("status", "model", "dimension"),
    [
        ("loading", "gemini-embedding-2", 768),
        ("active", "gemini-embedding-2", 1536),
    ],
)
def test_query_requires_active_release_with_exact_client_dimension(
    status: str, model: str, dimension: int
) -> None:
    service, repository, embedder, generator = _service([], "", ())
    repository.status = status
    repository.embedding_model = model
    repository.embedding_dimension = dimension

    with pytest.raises(GroundingError, match="query embedding contract"):
        service.answer("香港電影問題")

    assert repository.calls == []
    assert embedder.calls == []
    assert generator.calls == []


def test_exact_title_precedes_a_closer_vector_result() -> None:
    vector_first = _passage(
        "metadata:other",
        "other",
        chinese_title="其他電影",
        english_title="Other Film",
        distance=0.01,
    )
    exact_later = _passage(
        "metadata:1982_ZJPD_001",
        "1982_ZJPD_001",
        chinese_title="最佳拍檔",
        english_title="Aces Go Places",
        distance=0.9,
    )
    service, _, _, generator = _service(
        [vector_first, exact_later],
        "《最佳拍檔》有結構化資料。[metadata:1982_ZJPD_001]",
        ("metadata:1982_ZJPD_001",),
    )

    result = service.answer("Aces Go Places / 最佳拍檔的資料")

    assert [item.citation_id for item in result.citations] == [
        "metadata:1982_ZJPD_001"
    ]
    assert generator.calls == []


def test_retrieval_deduplicates_passages_and_never_supplies_more_than_eight() -> None:
    passages = [
        _passage(
            f"metadata:movie-{index}",
            f"movie-{index}",
            chinese_title=f"電影{index}",
            english_title=f"Film {index}",
        )
        for index in range(10)
    ]
    passages.insert(1, dict(passages[0]))
    service, _, _, generator = _service(
        passages,
        "第一部電影有資料。[metadata:movie-0]",
        ("metadata:movie-0",),
    )

    service.answer("請解釋這批香港電影資料彼此的關係")

    evidence = generator.calls[0][1]
    assert len(evidence) == 8
    assert len({item.passage_id for item in evidence}) == 8


def test_generic_answer_returns_cards_and_posters_only_for_cited_movies() -> None:
    """Breaks if uncited vector neighbours leak into UI cards or chat history."""
    passages = [
        _passage(
            "metadata:1987_FGBR_001",
            "1987_FGBR_001",
            chinese_title="富貴逼人",
            english_title="It's a Mad, Mad, Mad World",
            poster_available=True,
        ),
        _passage(
            "metadata:1988_FGZBR_001",
            "1988_FGZBR_001",
            chinese_title="富貴再逼人",
            english_title="It's a Mad, Mad, Mad World II",
            poster_available=True,
        ),
        _passage(
            "metadata:1989_FGZSBR_001",
            "1989_FGZSBR_001",
            chinese_title="富貴再三逼人",
            english_title="It's a Mad, Mad, Mad World III",
            poster_available=True,
        ),
    ]
    service, repository, _, _ = _service(
        passages,
        "《富貴逼人》有受控資料。[metadata:1987_FGBR_001]",
        ("metadata:1987_FGBR_001",),
    )

    result = service.answer("請解釋這批香港電影資料彼此的關係")

    assert [citation.movie_id for citation in result.citations] == [
        "1987_FGBR_001"
    ]
    assert [movie.movie_id for movie in result.movies] == ["1987_FGBR_001"]
    assert repository.poster_batch_calls == [
        (RELEASE_ID, ("1987_FGBR_001",))
    ]


def test_generic_cited_movie_is_the_only_followup_history_anchor() -> None:
    passages = [
        _passage(
            "metadata:1987_FGBR_001",
            "1987_FGBR_001",
            chinese_title="富貴逼人",
            english_title="It's a Mad, Mad, Mad World",
            director="高志森",
        ),
        _passage(
            "metadata:1988_FGZBR_001",
            "1988_FGZBR_001",
            chinese_title="富貴再逼人",
            english_title="It's a Mad, Mad, Mad World II",
        ),
        _passage(
            "metadata:1989_FGZSBR_001",
            "1989_FGZSBR_001",
            chinese_title="富貴再三逼人",
            english_title="It's a Mad, Mad, Mad World III",
        ),
    ]
    first_question = "請解釋這批香港電影資料彼此的關係"
    service, repository, _, _ = _service(
        passages,
        "《富貴逼人》有受控資料。[metadata:1987_FGBR_001]",
        ("metadata:1987_FGBR_001",),
    )
    first = service.answer(first_question)
    history = (
        ConversationExchange(
            first_question,
            first.answer_markdown,
            tuple(movie.movie_id for movie in first.movies),
        ),
    )

    followup = service.answer("它的導演是誰？", history)

    assert repository.calls[-1]["target_movie_ids"] == ("1987_FGBR_001",)
    assert "高志森" in followup.answer_markdown
    assert [movie.movie_id for movie in followup.movies] == ["1987_FGBR_001"]


def test_answer_rejects_model_citation_not_in_retrieval() -> None:
    passage = _passage(
        "metadata:1978_ZQ_001",
        "1978_ZQ_001",
        chinese_title="醉拳",
        english_title="Drunken Master",
    )
    service, *_ = _service([passage], "unsupported [P:missing]", ("P:missing",))

    with pytest.raises(GroundingError, match="unknown citation"):
        service.answer("香港電影問題")


def test_answer_rejects_duplicate_citation_ids() -> None:
    passage = _passage(
        "metadata:1978_ZQ_001",
        "1978_ZQ_001",
        chinese_title="醉拳",
        english_title="Drunken Master",
    )
    service, *_ = _service(
        [passage],
        "有資料。[metadata:1978_ZQ_001]",
        ("metadata:1978_ZQ_001", "metadata:1978_ZQ_001"),
    )

    with pytest.raises(GroundingError, match="duplicate citation"):
        service.answer("香港電影問題")


def test_answer_rejects_uncited_generated_claim_line() -> None:
    passage = _passage(
        "metadata:1978_ZQ_001",
        "1978_ZQ_001",
        chinese_title="醉拳",
        english_title="Drunken Master",
    )
    service, *_ = _service(
        [passage],
        "《醉拳》有結構化資料。[metadata:1978_ZQ_001]\n但這一行沒有引用。",
        ("metadata:1978_ZQ_001",),
    )

    with pytest.raises(GroundingError, match="uncited generated claim"):
        service.answer("香港電影問題")


@pytest.mark.parametrize(
    "answer_markdown",
    [
        "更多資料見 https://example.com。[metadata:1978_ZQ_001]",
        "[更多資料](https://example.com)。[metadata:1978_ZQ_001]",
        "圖片位於 gs://private-bucket/poster.webp。[metadata:1978_ZQ_001]",
        "更多資料見 example.com/path。[metadata:1978_ZQ_001]",
        "圖片路徑 /api/posters/movie。[metadata:1978_ZQ_001]",
        "路徑:/api/poster。[metadata:1978_ZQ_001]",
        "路徑:../api/poster。[metadata:1978_ZQ_001]",
        "路徑:./api/poster。[metadata:1978_ZQ_001]",
        "路徑:(/api/poster)。[metadata:1978_ZQ_001]",
        "路徑:/海報/醉拳。[metadata:1978_ZQ_001]",
        "data:text/html,x。[metadata:1978_ZQ_001]",
        '<a href="/api/poster">資料</a>。[metadata:1978_ZQ_001]',
    ],
)
def test_answer_rejects_urls_and_markdown_links(answer_markdown: str) -> None:
    passage = _passage(
        "metadata:1978_ZQ_001",
        "1978_ZQ_001",
        chinese_title="醉拳",
        english_title="Drunken Master",
    )
    service, *_ = _service(
        [passage], answer_markdown, ("metadata:1978_ZQ_001",)
    )

    with pytest.raises(GroundingError, match="URL or Markdown link"):
        service.answer("香港電影問題")


@pytest.mark.parametrize(
    "answer_markdown",
    [
        "這是有效的普通句子，沒有外部連結。[metadata:1978_ZQ_001]",
        "《醉拳》的類型是動作/喜劇。[metadata:1978_ZQ_001]",
        "年份是 1978；片長是 100 分鐘。[metadata:1978_ZQ_001]",
    ],
)
def test_answer_allows_plain_prose_and_punctuation(answer_markdown: str) -> None:
    passage = _passage(
        "metadata:1978_ZQ_001",
        "1978_ZQ_001",
        chinese_title="醉拳",
        english_title="Drunken Master",
    )
    service, *_ = _service(
        [passage], answer_markdown, ("metadata:1978_ZQ_001",)
    )

    assert service.answer("香港電影資料").answer_markdown == answer_markdown


def test_answer_rejects_citation_markers_without_substantive_text() -> None:
    passage = _passage(
        "metadata:1978_ZQ_001",
        "1978_ZQ_001",
        chinese_title="醉拳",
        english_title="Drunken Master",
    )
    service, *_ = _service(
        [passage], "。[metadata:1978_ZQ_001]", ("metadata:1978_ZQ_001",)
    )

    with pytest.raises(GroundingError, match="substantive"):
        service.answer("香港電影問題")


@pytest.mark.parametrize("separator", [",", ", ", "，", "， "])
def test_grouped_known_inline_citations_are_normalized_to_separate_markers(
    separator: str,
) -> None:
    service, *_ = _service(
        _two_citation_passages(),
        f"兩部都有資料。[metadata:first{separator}metadata:second]",
        ("metadata:first", "metadata:second"),
    )

    result = service.answer("比較兩部電影")

    assert result.answer_markdown == "兩部都有資料。[metadata:first][metadata:second]"
    assert [citation.citation_id for citation in result.citations] == [
        "metadata:first",
        "metadata:second",
    ]


@pytest.mark.parametrize(
    ("marker", "citation_ids"),
    [
        ("[metadata:first, metadata:missing]", ("metadata:first",)),
        ("[metadata:first,]", ("metadata:first",)),
        ("[,metadata:second]", ("metadata:second",)),
        (
            "[metadata:first,,metadata:second]",
            ("metadata:first", "metadata:second"),
        ),
        (
            "[metadata:first；metadata:second]",
            ("metadata:first", "metadata:second"),
        ),
    ],
)
def test_grouped_inline_citations_reject_unknown_empty_or_other_delimiters(
    marker: str, citation_ids: tuple[str, ...]
) -> None:
    service, *_ = _service(
        _two_citation_passages(),
        f"兩部都有資料。{marker}",
        citation_ids,
    )

    with pytest.raises(GroundingError, match="unknown citation"):
        service.answer("比較兩部電影")


def test_exact_known_citation_id_containing_a_comma_is_not_split() -> None:
    composite_id = "metadata:first,metadata:second"
    composite = _passage(
        composite_id,
        "composite",
        chinese_title="複合識別碼",
        english_title="Composite ID",
    )
    service, *_ = _service(
        [composite, *_two_citation_passages()],
        f"這是單一證據。[{composite_id}]",
        (composite_id,),
    )

    result = service.answer("比較兩部電影")

    assert result.answer_markdown == f"這是單一證據。[{composite_id}]"
    assert [citation.citation_id for citation in result.citations] == [composite_id]


def test_grouped_inline_citations_do_not_expand_the_declared_citation_set() -> None:
    service, *_ = _service(
        _two_citation_passages(),
        "兩部都有資料。[metadata:first, metadata:second]",
        ("metadata:first",),
    )

    with pytest.raises(GroundingError, match="inline citations"):
        service.answer("比較兩部電影")


def test_grouped_inline_citations_do_not_relax_per_line_grounding() -> None:
    service, *_ = _service(
        _two_citation_passages(),
        "第一行有資料。[metadata:first, metadata:second]\n第二行沒有引用。",
        ("metadata:first", "metadata:second"),
    )

    with pytest.raises(GroundingError, match="uncited generated claim"):
        service.answer("比較兩部電影")


@pytest.mark.parametrize(
    "answer_markdown",
    [
        "資料。[metadata:first, metadata:second](relative-target)",
        "路徑:/api/poster。[metadata:first, metadata:second]",
    ],
)
def test_grouped_inline_citations_do_not_relax_link_or_path_rejection(
    answer_markdown: str,
) -> None:
    service, *_ = _service(
        _two_citation_passages(),
        answer_markdown,
        ("metadata:first", "metadata:second"),
    )

    with pytest.raises(GroundingError, match="URL or Markdown link"):
        service.answer("比較兩部電影")


def test_inline_citations_must_exactly_match_declared_citation_ids() -> None:
    first = _passage(
        "metadata:first",
        "first",
        chinese_title="第一部",
        english_title="First",
    )
    second = _passage(
        "metadata:second",
        "second",
        chinese_title="第二部",
        english_title="Second",
    )
    service, *_ = _service(
        [first, second],
        "兩部都有資料。[metadata:first][metadata:second]",
        ("metadata:first",),
    )

    with pytest.raises(GroundingError, match="inline citations"):
        service.answer("比較兩部電影")


def test_pdf_page_citation_is_preserved_from_repository_record() -> None:
    metadata = _passage(
        "metadata:1978_ZQ_001",
        "1978_ZQ_001",
        chinese_title="醉拳",
        english_title="Drunken Master",
    )
    page = _passage(
        "pdf:drunken-master:p2",
        "1978_ZQ_001",
        chinese_title="醉拳",
        english_title="Drunken Master",
        passage_kind="pdf",
        page_number=2,
        source_filename="S 級港⽚-醉拳.pdf",
        poster_available=True,
    )
    page["body"] = "第二頁以動作節奏分析電影美學。"
    service, *_ = _service(
        [metadata, page],
        "分析見第二頁。[pdf:drunken-master:p2]",
        ("pdf:drunken-master:p2",),
    )

    result = service.answer("醉拳的美學分析")

    assert result.citations[0].source_kind == "pdf_page"
    assert result.citations[0].page_number == 2
    assert result.citations[0].source_filename == "S 級港⽚-醉拳.pdf"
    assert result.movies[0].poster_url == "/api/posters/1978_ZQ_001"


def test_multi_title_qualitative_comparison_uses_general_generation() -> None:
    drunken_metadata = _passage(
        "metadata:1978_ZQ_001",
        "1978_ZQ_001",
        chinese_title="醉拳",
        english_title="Drunken Master",
        poster_available=True,
    )
    drunken_page = _passage(
        "pdf:drunken:p1",
        "1978_ZQ_001",
        chinese_title="醉拳",
        english_title="Drunken Master",
        passage_kind="pdf",
        page_number=1,
        source_filename="drunken.pdf",
        poster_available=True,
    )
    aces_page = _passage(
        "pdf:aces:p1",
        "1982_ZJPD_001",
        chinese_title="最佳拍檔",
        english_title="Aces Go Places",
        passage_kind="pdf",
        page_number=1,
        source_filename="aces.pdf",
        poster_available=False,
    )
    service, repository, _, generator = _service(
        [drunken_metadata, drunken_page, aces_page],
        "兩片的分析可比較。[pdf:drunken:p1][pdf:aces:p1]",
        ("pdf:drunken:p1", "pdf:aces:p1"),
    )

    result = service.answer("比較醉拳和最佳拍檔")

    assert result.answer_markdown == "兩片的分析可比較。[pdf:drunken:p1][pdf:aces:p1]"
    assert [item.citation_id for item in result.citations] == [
        "pdf:drunken:p1",
        "pdf:aces:p1",
    ]
    assert [movie.movie_id for movie in result.movies] == [
        "1978_ZQ_001",
        "1982_ZJPD_001",
    ]
    assert generator.calls[0][0] == "比較醉拳和最佳拍檔"
    assert repository.poster_batch_calls == [
        (RELEASE_ID, ("1978_ZQ_001", "1982_ZJPD_001"))
    ]


@pytest.mark.parametrize(
    "question",
    (
        "醉拳和最佳拍檔的導演？",
        "醉拳和最佳拍檔的主演？",
    ),
)
def test_multi_title_canonical_query_uses_metadata_only(question: str) -> None:
    drunken = _passage(
        "metadata:drunken",
        "1978_ZQ_001",
        chinese_title="醉拳",
        english_title="Drunken Master",
        director="袁和平",
        cast="成龍、袁小田",
    )
    aces = _passage(
        "metadata:aces",
        "1982_ZJPD_001",
        chinese_title="最佳拍檔",
        english_title="Aces Go Places",
        director="曾志偉",
        cast="許冠傑、麥嘉",
    )
    service, _, _, generator = _service(
        [drunken, aces],
        "模型不應回答多片規範欄位。[metadata:drunken][metadata:aces]",
        ("metadata:drunken", "metadata:aces"),
    )

    result = service.answer(question)

    assert [item.citation_id for item in result.citations] == [
        "metadata:drunken",
        "metadata:aces",
    ]
    assert all(item.source_kind == "movie_metadata" for item in result.citations)
    assert generator.calls == []


def test_multi_title_mixed_query_fails_controlled_before_generation() -> None:
    drunken = _passage(
        "metadata:drunken",
        "1978_ZQ_001",
        chinese_title="醉拳",
        english_title="Drunken Master",
    )
    aces = _passage(
        "metadata:aces",
        "1982_ZJPD_001",
        chinese_title="最佳拍檔",
        english_title="Aces Go Places",
    )
    drunken_pdf = _passage(
        "pdf:drunken:p1",
        "1978_ZQ_001",
        chinese_title="醉拳",
        english_title="Drunken Master",
        passage_kind="pdf",
        page_number=1,
        source_filename="drunken.pdf",
    )
    aces_pdf = _passage(
        "pdf:aces:p1",
        "1982_ZJPD_001",
        chinese_title="最佳拍檔",
        english_title="Aces Go Places",
        passage_kind="pdf",
        page_number=1,
        source_filename="aces.pdf",
    )
    service, _, _, generator = _service(
        [drunken, drunken_pdf, aces, aces_pdf],
        "模型不應混寫導演與視覺分析。[pdf:drunken:p1][pdf:aces:p1]",
        ("pdf:drunken:p1", "pdf:aces:p1"),
    )

    result = service.answer("醉拳和最佳拍檔的導演和視覺風格？")

    assert "目前只有結構化電影資料" in result.answer_markdown
    assert [item.citation_id for item in result.citations] == [
        "metadata:drunken",
        "metadata:aces",
    ]
    assert generator.calls == []


def _cross_movie_pdf_evidence() -> tuple[EvidencePassage, ...]:
    first = _passage(
        "pdf:first:p1",
        "first",
        chinese_title="第一部",
        english_title="First Film",
        passage_kind="pdf",
        page_number=1,
        source_filename="first.pdf",
    )
    second = _passage(
        "pdf:second:p1",
        "second",
        chinese_title="第二部",
        english_title="Second Film",
        passage_kind="pdf",
        page_number=1,
        source_filename="second.pdf",
    )
    first["body"] = "第一部以構圖形成張力。"
    second["body"] = "第二部以聲音形成節奏。"
    return tuple(_evidence_from_record(record) for record in (first, second))


def test_exact_target_pdf_selector_rejects_cross_movie_eligible_evidence() -> None:
    evidence = _cross_movie_pdf_evidence()
    generated = GeneratedAnswer(
        "模型只選第一部。[pdf:first:p1]",
        ("pdf:first:p1",),
    )

    with pytest.raises(GroundingError, match="one exact movie"):
        _validated_selected_pdf_citation_ids(generated, evidence)


def test_exact_target_extractor_rejects_cross_movie_selected_evidence() -> None:
    evidence = _cross_movie_pdf_evidence()

    with pytest.raises(GroundingError, match="one exact movie"):
        _extractive_qualitative_answer(
            "比較第一部和第二部的視覺風格",
            ("pdf:first:p1", "pdf:second:p1"),
            evidence,
        )


@pytest.mark.parametrize(
    ("retrieval_flag", "approved_ids", "expected_url"),
    [
        (False, frozenset({"poster-movie"}), "/api/posters/poster-movie"),
        (True, frozenset(), None),
    ],
    ids=("batch-authority-adds-url", "batch-authority-removes-url"),
)
def test_movie_cards_use_one_batch_poster_authority_not_search_boolean(
    retrieval_flag: bool,
    approved_ids: frozenset[str],
    expected_url: str | None,
) -> None:
    passage = _passage(
        "metadata:poster-movie",
        "poster-movie",
        chinese_title="海報測試片",
        english_title="Poster Test Film",
        poster_available=retrieval_flag,
    )
    service, repository, _, _ = _service(
        [passage],
        "《海報測試片》在受控資料中。[metadata:poster-movie]",
        ("metadata:poster-movie",),
    )
    repository.approved_poster_ids = approved_ids

    result = service.answer("海報測試片是哪一年上映？")

    assert result.movies[0].poster_url == expected_url
    assert repository.poster_batch_calls == [
        (RELEASE_ID, ("poster-movie",))
    ]


def test_movie_card_generation_fails_closed_when_poster_authority_is_unavailable(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    passage = _passage(
        "metadata:poster-movie",
        "poster-movie",
        chinese_title="海報測試片",
        english_title="Poster Test Film",
    )
    service, repository, _, _ = _service(
        [passage],
        "《海報測試片》在受控資料中。[metadata:poster-movie]",
        ("metadata:poster-movie",),
    )

    def unavailable(_release_id: str, _movie_ids: tuple[str, ...]) -> object:
        raise RagDatabaseError("database unavailable")

    monkeypatch.setattr(repository, "get_approved_poster_assets", unavailable)

    with pytest.raises(GroundingError, match="poster authority is unavailable"):
        service.answer("海報測試片是哪一年上映？")


@pytest.mark.parametrize(
    "malformed",
    (
        [],
        {"unexpected": {"movie_id": "unexpected"}},
        {"poster-movie": {"movie_id": "wrong"}},
    ),
)
def test_movie_card_generation_rejects_malformed_poster_authority_mapping(
    monkeypatch: pytest.MonkeyPatch, malformed: object
) -> None:
    passage = _passage(
        "metadata:poster-movie",
        "poster-movie",
        chinese_title="海報測試片",
        english_title="Poster Test Film",
    )
    service, repository, _, _ = _service(
        [passage],
        "《海報測試片》在受控資料中。[metadata:poster-movie]",
        ("metadata:poster-movie",),
    )
    monkeypatch.setattr(
        repository,
        "get_approved_poster_assets",
        lambda _release_id, _movie_ids: malformed,
    )

    with pytest.raises(GroundingError, match="poster authority response is invalid"):
        service.answer("海報測試片是哪一年上映？")


def test_metadata_only_movie_does_not_gain_deep_analysis() -> None:
    passage = _passage(
        "metadata:no-deep",
        "no-deep",
        chinese_title="沒有深度文檔",
        english_title="Metadata Only",
    )
    service, *_ = _service(
        [passage],
        "目前只有結構化電影資料，不能提供深度美學分析。[metadata:no-deep]",
        ("metadata:no-deep",),
    )

    result = service.answer("分析一部沒有深度文檔的電影美學")

    assert "目前只有結構化電影資料" in result.answer_markdown
    assert all(citation.source_kind == "movie_metadata" for citation in result.citations)


def _flying_mr_boo_evidence() -> list[dict[str, object]]:
    metadata = _passage(
        "metadata:1987_FGBR_001",
        "1987_FGBR_001",
        chinese_title="富貴逼人",
        english_title="It's a Mad, Mad, Mad World",
        director="高志森",
        cast="董驃、沈殿霞、曾志偉",
        genre="喜劇、家庭、奇幻",
    )
    pdf = _passage(
        "pdf:fgbr:p3",
        "1987_FGBR_001",
        chinese_title="富貴逼人",
        english_title="It's a Mad, Mad, Mad World",
        passage_kind="pdf",
        page_number=3,
        source_filename="富貴逼人分析.pdf",
    )
    pdf["body"] = (
        "畫面以擠迫構圖和喧鬧聲場形成喜劇張力；"
        "影片以1984與1997的時代對照強化家庭轉折；"
        "另稱主演還有虛構演員，片長99分鐘。"
    )
    return [metadata, pdf]


def test_exact_target_canonical_facts_are_deterministic_metadata_only() -> None:
    service, _, _, generator = _service(
        _flying_mr_boo_evidence(),
        "PDF 錯誤聲稱片長99分鐘。[pdf:fgbr:p3]",
        ("pdf:fgbr:p3",),
    )

    result = service.answer("富貴逼人的主演和片長是甚麼？")

    assert "董驃、沈殿霞、曾志偉" in result.answer_markdown
    assert "100" in result.answer_markdown
    assert "99" not in result.answer_markdown
    assert [item.citation_id for item in result.citations] == [
        "metadata:1987_FGBR_001"
    ]
    assert generator.calls == []


@pytest.mark.parametrize(
    "model_answer",
    ("", "虛構者在1988年執導全片。[pdf:fgbr:p3]"),
)
def test_exact_target_qualitative_question_sends_only_that_movies_pdf(
    model_answer: str,
) -> None:
    service, _, _, generator = _service(
        _flying_mr_boo_evidence(),
        model_answer,
        ("pdf:fgbr:p3",),
    )

    result = service.answer("富貴逼人的視覺或聲音如何營造喜劇感？")

    assert [item.source_kind for item in generator.calls[0][1]] == ["pdf_page"]
    assert [item.movie_id for item in generator.calls[0][1]] == ["1987_FGBR_001"]
    assert "喜劇張力" in result.answer_markdown
    assert "虛構者" not in result.answer_markdown
    assert "1988" not in result.answer_markdown
    assert "虛構演員" not in result.answer_markdown
    assert "99" not in result.answer_markdown
    assert result.answer_markdown.endswith("[pdf:fgbr:p3]")
    assert [item.citation_id for item in result.citations] == ["pdf:fgbr:p3"]


@pytest.mark.parametrize(
    "question",
    (
        "讲一下富贵逼人的喜剧创作",
        "講下富貴逼人的喜劇創作",
        "聊聊富貴逼人的家庭喜劇",
        "我想了解富貴逼人的創作特色",
        "请分析一下富贵逼人的笑点设计",
        "富貴逼人的喜劇創作有何特色？",
        "讲一下富贵逼人这部电影的喜剧创作",
        "说说富贵逼人的喜剧创作",
        "请帮我讲一下富贵逼人的喜剧创作",
        "请你讲一下富贵逼人的喜剧创作",
        "讲一讲富贵逼人的喜剧创作",
        "请，讲一下富贵逼人的喜剧创作",
        "麻烦你讲一下富贵逼人的喜剧创作",
        "讲一下，富贵逼人的喜剧创作",
        "讲一下富贵逼人的喜剧书写",
    ),
)
def test_conversational_release_title_reaches_targeted_grounded_rag(
    question: str,
) -> None:
    """Breaks if title scope still depends on a whitelist of analysis topics."""
    evidence = _flying_mr_boo_evidence()
    for passage in evidence:
        passage["poster_available"] = True
    service, repository, embedder, generator = _service(
        evidence,
        "",
        ("pdf:fgbr:p3",),
    )

    result = service.answer(question)

    assert repository.calls[0]["target_movie_ids"] == ("1987_FGBR_001",)
    assert len(embedder.calls) == 1
    assert [item.source_kind for item in generator.calls[0][1]] == ["pdf_page"]
    assert "喜劇張力" in result.answer_markdown
    assert [item.citation_id for item in result.citations] == ["pdf:fgbr:p3"]
    assert [movie.movie_id for movie in result.movies] == ["1987_FGBR_001"]
    assert result.movies[0].poster_url == "/api/posters/1987_FGBR_001"


def test_exact_target_qualitative_allows_verbatim_analytical_years() -> None:
    service, _, _, _ = _service(
        _flying_mr_boo_evidence(),
        "模型年份不應控制輸出。[pdf:fgbr:p3]",
        ("pdf:fgbr:p3",),
    )

    result = service.answer("富貴逼人的視覺如何呈現1984與1997的時代對照？")

    assert "1984與1997" in result.answer_markdown
    assert "模型年份" not in result.answer_markdown
    assert "喜劇張力" not in result.answer_markdown
    assert [item.citation_id for item in result.citations] == ["pdf:fgbr:p3"]


@pytest.mark.parametrize(
    "unsafe_body",
    (
        "主演：虛構演員；全片共99分鐘。",
        "更多分析見https://example.com/report。",
        "構圖詳見[forged]。",
    ),
)
def test_exact_target_qualitative_fails_closed_when_no_safe_span_exists(
    unsafe_body: str,
) -> None:
    evidence = _flying_mr_boo_evidence()
    evidence[1]["body"] = unsafe_body
    service, _, _, _ = _service(
        evidence,
        "模型自行補寫構圖分析。[pdf:fgbr:p3]",
        ("pdf:fgbr:p3",),
    )

    result = service.answer("富貴逼人的視覺風格如何？")

    assert result.answer_markdown == "目前沒有可安全抽取的定性分析證據。"
    assert result.citations == ()
    assert result.movies == ()


def test_exact_target_qualitative_ranks_one_span_per_passage_and_two_lines() -> None:
    metadata = _passage(
        "metadata:extractive",
        "extractive",
        chinese_title="摘錄電影",
        english_title="Extractive Film",
    )
    pages: list[dict[str, object]] = []
    expected_lines: list[str] = []
    for page_number, detail in enumerate(("低頻", "鼓點", "靜默"), start=1):
        passage_id = f"pdf:extractive:p{page_number}"
        page = _passage(
            passage_id,
            "extractive",
            chinese_title="摘錄電影",
            english_title="Extractive Film",
            passage_kind="pdf",
            page_number=page_number,
            source_filename="extractive.pdf",
        )
        selected = f"聲音設計以{detail}形成節奏。"
        page["body"] = f"無關的畫面描述。{selected}"
        pages.append(page)
        expected_lines.append(f"{selected}[{passage_id}]")
    expected_lines[0] = f"《摘錄電影》提供的分析指出：{expected_lines[0]}"
    service, _, _, _ = _service(
        [metadata, *pages],
        "",
        tuple(f"pdf:extractive:p{page_number}" for page_number in range(1, 4)),
    )

    result = service.answer("摘錄電影的聲音風格如何？")

    assert result.answer_markdown.splitlines() == expected_lines[:2]
    assert [item.citation_id for item in result.citations] == [
        "pdf:extractive:p1",
        "pdf:extractive:p2",
    ]


def test_exact_target_qualitative_handles_real_pdf_line_breaks_and_rhythm() -> None:
    movie = {
        "movie_id": "1978_ZQ_001",
        "chinese_title": "醉拳",
        "english_title": "Drunken Master",
    }
    page_one = EvidencePassage(
        passage_id="pdf:drunken-master-deep-analysis-v1:p1",
        movie_id="1978_ZQ_001",
        source_kind="pdf_page",
        body=(
            "【美學特徵分析】\n"
            "視覺美學：鏡頭體系以完整呈現武打動作為最高原則，偏好中全景"
            "機位或緩慢橫移，單個打鬥段落常以一分鐘以上的長鏡頭展示招式"
            "連貫性，且頻繁使用 zoom\n\nin/ 或\n\nzoom\n\nout\n"
            "的鏡頭運動呈現喜劇及風格化的畫面節奏，不僅經常作為轉換場景"
            "時的開場畫面，在角色的打鬥段落中更尤其突出，全片幾乎在所有"
            "涉及開始動作戲的鏡頭設計上皆使用 zoom out，並以兩段式運動從"
            "近鏡轉到中景及遠景，展露更多人物間打鬥時與空間的互動。\n\n"
            "武術設計是這部電影的核心，使每一次出招都包含一個錯誤動作"
            "引發的笑點，並以雜耍般的小動作營造詼諧輕盈感。"
        ),
        page_number=1,
        source_filename="S 級港片-醉拳.pdf",
        movie=movie,
        poster_available=True,
    )
    page_two = EvidencePassage(
        passage_id="pdf:drunken-master-deep-analysis-v1:p2",
        movie_id="1978_ZQ_001",
        source_kind="pdf_page",
        body=(
            "喜劇機制以表情動作笑點為絕對主導，依靠精準的動作編排和"
            "鏡頭運動而非對白或剪輯。\n\n"
            "聲畫關係上，喜劇動作的節拍均與鑼鼓點精準對齊，將戲曲的"
            "邏輯移植於電影。"
        ),
        page_number=2,
        source_filename="S 級港片-醉拳.pdf",
        movie=movie,
        poster_available=True,
    )

    answer, citation_ids = _extractive_qualitative_answer(
        "《醉拳》的動作美學如何結合喜劇節奏？",
        (page_one.passage_id, page_two.passage_id),
        (page_one, page_two),
    )

    lines = answer.splitlines()
    assert lines[0].startswith("《醉拳》提供的分析指出：")
    assert "zoom。in" not in answer
    assert "out。的鏡頭" not in answer
    assert any(term in answer for term in ("武術", "功夫", "武打", "鏡頭"))
    assert any(term in answer for term in ("笑點", "詼諧", "諧趣"))
    assert "鑼鼓" in answer
    assert len(lines) == 2
    assert citation_ids == (page_two.passage_id,)

    page_three = replace(
        page_two,
        passage_id="pdf:drunken-master-deep-analysis-v1:p3",
        body="本片奠定動作喜劇化設計的招牌。",
        page_number=3,
    )
    assert _deterministic_qualitative_citation_ids(
        "《醉拳》的動作美學如何結合喜劇節奏？",
        (page_one, page_two, page_three),
    ) == (page_one.passage_id, page_two.passage_id)


def test_exact_target_qualitative_ranks_symbolism_synonyms_and_discloses_analysis() -> None:
    passage = EvidencePassage(
        passage_id="pdf:its-a-mad-mad-mad-world-deep-analysis-v1:p2",
        movie_id="1987_FGBR_001",
        source_kind="pdf_page",
        body=(
            "敘事美學採用三段式結構，先建立家庭的日常窘境。"
            "片中編劇以二姐送梳給招弟，二姐卻想拿回來重用，來隱喻當年"
            "香港和中國的政治狀況。"
        ),
        page_number=2,
        source_filename="S級港片-富貴逼人.pdf",
        movie={
            "movie_id": "1987_FGBR_001",
            "chinese_title": "富貴逼人",
            "english_title": "It's a Mad, Mad, Mad World",
        },
        poster_available=True,
    )

    answer, citation_ids = _extractive_qualitative_answer(
        "《富貴逼人》中梳子的象徵意義是什麼？",
        (passage.passage_id,),
        (passage,),
    )

    assert answer.startswith("《富貴逼人》提供的分析指出：")
    assert "送梳" in answer
    assert "隱喻" in answer
    assert citation_ids == (passage.passage_id,)


def test_exact_target_qualitative_empty_model_selection_uses_grounded_page_fallback() -> None:
    metadata = _passage(
        "metadata:sound-fallback",
        "sound-fallback",
        chinese_title="聲音電影",
        english_title="Sound Film",
    )
    visual = _passage(
        "pdf:sound-fallback:p1",
        "sound-fallback",
        chinese_title="聲音電影",
        english_title="Sound Film",
        passage_kind="pdf",
        page_number=1,
        source_filename="sound-film.pdf",
    )
    visual["body"] = "視覺設計以固定鏡位呈現狹窄空間。"
    sound = _passage(
        "pdf:sound-fallback:p5",
        "sound-fallback",
        chinese_title="聲音電影",
        english_title="Sound Film",
        passage_kind="pdf",
        page_number=5,
        source_filename="sound-film.pdf",
    )
    sound["body"] = "聲音美學以輕快電子琴和環境聲建立喜劇節奏。"
    service, _, _, generator = _service(
        [metadata, visual, sound],
        "模型沒有選擇引用。",
        (),
    )

    result = service.answer("《聲音電影》的聲音設計如何？")

    assert [citation.citation_id for citation in result.citations] == [
        "pdf:sound-fallback:p5"
    ]
    assert "聲音美學" in result.answer_markdown
    assert [passage.source_kind for passage in generator.calls[0][1]] == [
        "pdf_page",
        "pdf_page",
    ]


def test_exact_target_qualitative_prunes_a_valid_but_weaker_extra_page() -> None:
    """Breaks if Vertex can over-cite a generic adjacent page beyond lexical page authority."""
    movie = {
        "movie_id": "1985_JSXS_001",
        "chinese_title": "殭屍先生",
        "english_title": "Mr. Vampire",
    }
    sound = EvidencePassage(
        passage_id="pdf:mr-vampire-deep-analysis-v1:p3",
        movie_id="1985_JSXS_001",
        source_kind="pdf_page",
        body=(
            "聲音美學以電子合成器低頻長音為恐怖基底，殭屍出場時疊加木魚與銅鈴，"
            "喜劇段落則切入輕快揚琴，音效在恐怖與喜劇之間果決轉換。"
        ),
        page_number=3,
        source_filename="S 級港片-殭屍先生.pdf",
        movie=movie,
        poster_available=True,
    )
    adjacent = replace(
        sound,
        passage_id="pdf:mr-vampire-deep-analysis-v1:p4",
        body="本片將殭屍從純粹恐怖象徵改造為可被喜劇消解的類型元素。",
        page_number=4,
    )
    generated = GeneratedAnswer(
        "模型同時選了兩頁。",
        (sound.passage_id, adjacent.passage_id),
    )

    assert _selected_qualitative_citation_ids(
        generated,
        "《殭屍先生》如何用聲音在恐怖與喜劇之間轉換？",
        (sound, adjacent),
    ) == (sound.passage_id,)


def test_exact_target_qualitative_scores_analysis_fields_outside_metadata_envelope() -> None:
    """Breaks if PDF field layout hides a reviewed analysis line behind metadata/URL."""
    movie = {
        "movie_id": "2001_SLZQ_001",
        "chinese_title": "少林足球",
        "english_title": "Shaolin Soccer",
    }
    page_one = EvidencePassage(
        passage_id="pdf:shaolin-soccer-deep-analysis-v1:p1",
        movie_id="2001_SLZQ_001",
        source_kind="pdf_page",
        body=(
            "【影片基本資訊】\n"
            "片名：少林足球\n"
            "年份：2001\n"
            "導演：周星馳\n"
            "類型標籤：功夫喜劇、體育\n"
            "開創性定位：首部將傳統武術、CG 特效與體育競技類型系統性融合的功夫喜劇\n"
            "影片來源：https://example.invalid/source\n\n"
            "【美學特徵分析】\n"
            "視覺美學以非寫實漫畫化鏡頭統攝。"
        ),
        page_number=1,
        source_filename="S 級港片-少林足球.pdf",
        movie=movie,
        poster_available=True,
    )
    page_two = replace(
        page_one,
        passage_id="pdf:shaolin-soccer-deep-analysis-v1:p2",
        body="開場以阿星示範功夫結合足球建立核心命題。",
        page_number=2,
    )
    page_three = replace(
        page_one,
        passage_id="pdf:shaolin-soccer-deep-analysis-v1:p3",
        body="喜劇機制以動作笑點與情境笑點並重。",
        page_number=3,
    )
    generated = GeneratedAnswer(
        "模型同時選了首頁與相鄰頁。",
        (page_one.passage_id, page_three.passage_id),
    )

    assert _selected_qualitative_citation_ids(
        generated,
        "《少林足球》如何把足球、功夫和喜劇結合？",
        (page_one, page_two, page_three),
    ) == (page_one.passage_id,)


def test_exact_target_qualitative_adds_server_page_for_uncovered_intent_group() -> None:
    """Breaks if one model-selected page can omit a requested qualitative facet."""
    movie = {
        "movie_id": "1978_ZQ_001",
        "chinese_title": "醉拳",
        "english_title": "Drunken Master",
    }
    action = EvidencePassage(
        passage_id="pdf:drunken-master-deep-analysis-v1:p1",
        movie_id="1978_ZQ_001",
        source_kind="pdf_page",
        body="武術設計以廣角鏡頭和雜耍動作營造喜劇笑點。",
        page_number=1,
        source_filename="S 級港片-醉拳.pdf",
        movie=movie,
        poster_available=True,
    )
    rhythm = replace(
        action,
        passage_id="pdf:drunken-master-deep-analysis-v1:p2",
        body="聲畫關係以鑼鼓節拍配合喜劇動作與鏡頭運動。",
        page_number=2,
    )
    generated = GeneratedAnswer("模型只選了動作頁。", (action.passage_id,))

    assert _selected_qualitative_citation_ids(
        generated,
        "《醉拳》的動作美學如何結合喜劇節奏？",
        (action, rhythm),
    ) == (action.passage_id, rhythm.passage_id)


def test_exact_target_qualitative_uses_two_segments_from_one_complete_page() -> None:
    """Breaks if sentence coverage is incorrectly expanded into extra page citations."""
    movie = {
        "movie_id": "1978_ZQ_001",
        "chinese_title": "醉拳",
        "english_title": "Drunken Master",
    }
    action_comedy = EvidencePassage(
        passage_id="pdf:drunken-master-deep-analysis-v1:p1",
        movie_id="1978_ZQ_001",
        source_kind="pdf_page",
        body="武術設計讓錯誤動作引發笑點，並以雜耍小動作營造詼諧感。",
        page_number=1,
        source_filename="S 級港片-醉拳.pdf",
        movie=movie,
        poster_available=True,
    )
    rhythm = replace(
        action_comedy,
        passage_id="pdf:drunken-master-deep-analysis-v1:p2",
        body=(
            "喜劇機制以表情動作笑點為主，依靠精準的動作編排。"
            "聲畫關係上，喜劇動作的節拍與鑼鼓點精準對齊，將戲曲邏輯移植於電影。"
        ),
        page_number=2,
    )
    generated = GeneratedAnswer("模型只選了節奏頁。", (rhythm.passage_id,))

    selected = _selected_qualitative_citation_ids(
        generated,
        "《醉拳》的動作美學如何結合喜劇節奏？",
        (action_comedy, rhythm),
    )
    answer, citation_ids = _extractive_qualitative_answer(
        "《醉拳》的動作美學如何結合喜劇節奏？",
        selected,
        (action_comedy, rhythm),
    )

    assert selected == (rhythm.passage_id,)
    assert "笑點" in answer
    assert "鑼鼓" in answer
    assert citation_ids == (rhythm.passage_id,)


def test_exact_target_qualitative_keeps_sound_transition_on_its_one_source_page() -> None:
    """Breaks if two complementary sentences on one page pull in an adjacent page."""
    movie = {
        "movie_id": "1985_JSXS_001",
        "chinese_title": "殭屍先生",
        "english_title": "Mr. Vampire",
    }
    adjacent = EvidencePassage(
        passage_id="pdf:mr-vampire-deep-analysis-v1:p1",
        movie_id="1985_JSXS_001",
        source_kind="pdf_page",
        body="九叔的嚴肅科普與文才的笨拙失誤建立喜劇基調。",
        page_number=1,
        source_filename="S 級港片-殭屍先生.pdf",
        movie=movie,
        poster_available=True,
    )
    sound = replace(
        adjacent,
        passage_id="pdf:mr-vampire-deep-analysis-v1:p3",
        body=(
            "聲音美學以電子合成器低頻長音為恐怖基底，配樂疊加木魚與銅鈴。"
            "喜劇段落則切入輕快揚琴與笛子，切換果決。"
        ),
        page_number=3,
    )
    generated = GeneratedAnswer("模型只選了聲音頁。", (sound.passage_id,))

    selected = _selected_qualitative_citation_ids(
        generated,
        "《殭屍先生》如何用聲音在恐怖與喜劇之間轉換？",
        (adjacent, sound),
    )
    answer, citation_ids = _extractive_qualitative_answer(
        "《殭屍先生》如何用聲音在恐怖與喜劇之間轉換？",
        selected,
        (adjacent, sound),
    )

    assert selected == (sound.passage_id,)
    assert "木魚" in answer
    assert "揚琴" in answer
    assert citation_ids == (sound.passage_id,)


def test_exact_target_qualitative_prefers_one_fusion_page_over_two_partial_pages() -> None:
    """Breaks if exact phrase overlap defeats the smallest complete citation set."""
    movie = {
        "movie_id": "2001_SLZQ_001",
        "chinese_title": "少林足球",
        "english_title": "Shaolin Soccer",
    }
    complete = EvidencePassage(
        passage_id="pdf:shaolin-soccer-deep-analysis-v1:p1",
        movie_id="2001_SLZQ_001",
        source_kind="pdf_page",
        body="首部將傳統武術、CG 特效與體育競技系統性融合的功夫喜劇。",
        page_number=1,
        source_filename="S 級港片-少林足球.pdf",
        movie=movie,
        poster_available=True,
    )
    action = replace(
        complete,
        passage_id="pdf:shaolin-soccer-deep-analysis-v1:p2",
        body="阿星示範功夫結合足球，建立核心命題。",
        page_number=2,
    )
    comedy = replace(
        complete,
        passage_id="pdf:shaolin-soccer-deep-analysis-v1:p3",
        body="喜劇機制以動作笑點與情境笑點並重。",
        page_number=3,
    )
    generated = GeneratedAnswer(
        "模型同時選了兩頁。", (action.passage_id, complete.passage_id)
    )

    assert _selected_qualitative_citation_ids(
        generated,
        "《少林足球》如何把足球、功夫和喜劇結合？",
        (complete, action, comedy),
    ) == (complete.passage_id,)


def test_exact_target_qualitative_disagreement_starts_from_strongest_server_page() -> None:
    """Breaks if model/server disagreement expands to every ranked page."""
    movie = {
        "movie_id": "2001_SLZQ_001",
        "chinese_title": "少林足球",
        "english_title": "Shaolin Soccer",
    }
    primary = EvidencePassage(
        passage_id="pdf:shaolin-soccer-deep-analysis-v1:p1",
        movie_id="2001_SLZQ_001",
        source_kind="pdf_page",
        body="傳統武術與體育競技融合成漫畫化的功夫喜劇。",
        page_number=1,
        source_filename="S 級港片-少林足球.pdf",
        movie=movie,
        poster_available=True,
    )
    adjacent = replace(
        primary,
        passage_id="pdf:shaolin-soccer-deep-analysis-v1:p2",
        body="阿星示範功夫結合足球，建立核心命題。",
        page_number=2,
    )
    model_only = replace(
        primary,
        passage_id="pdf:shaolin-soccer-deep-analysis-v1:p3",
        body="喜劇機制以動作笑點與情境笑點並重。",
        page_number=3,
    )
    generated = GeneratedAnswer("模型只選了非權威頁。", (model_only.passage_id,))

    assert _selected_qualitative_citation_ids(
        generated,
        "《少林足球》如何把足球、功夫和喜劇結合？",
        (primary, adjacent, model_only),
    ) == (primary.passage_id,)


@pytest.mark.parametrize(
    ("citation_ids", "error"),
    (
        (("pdf:missing:p1",), "unknown citation"),
        (("metadata:1987_FGBR_001",), "unknown citation"),
    ),
)
def test_exact_target_qualitative_rejects_invalid_selected_pdf_ids(
    citation_ids: tuple[str, ...], error: str
) -> None:
    service, _, _, _ = _service(
        _flying_mr_boo_evidence(),
        "模型文字不受信任。",
        citation_ids,
    )

    with pytest.raises(GroundingError, match=error):
        service.answer("富貴逼人的視覺風格如何？")


def test_exact_target_qualitative_deduplicates_repeated_valid_vertex_page() -> None:
    service, _, _, _ = _service(
        _flying_mr_boo_evidence(),
        "擠迫構圖形成喜劇張力。[pdf:fgbr:p3]",
        ("pdf:fgbr:p3", "pdf:fgbr:p3"),
    )

    result = service.answer("富貴逼人的視覺風格如何？")

    assert [item.citation_id for item in result.citations] == ["pdf:fgbr:p3"]
    assert result.answer_markdown.count("[pdf:fgbr:p3]") == 1


def test_mixed_exact_target_composes_metadata_facts_with_pdf_only_analysis() -> None:
    service, _, _, generator = _service(
        _flying_mr_boo_evidence(),
        "虛構者在1988年執導全片。[pdf:fgbr:p3]",
        ("pdf:fgbr:p3",),
    )

    result = service.answer(
        "富貴逼人的主演和片長是甚麼，並分析它的視覺或聲音？"
    )

    generated_question, generated_evidence, *_ = generator.calls[0]
    assert "主演" not in generated_question
    assert "片長" not in generated_question
    assert [item.source_kind for item in generated_evidence] == ["pdf_page"]
    assert "董驃、沈殿霞、曾志偉" in result.answer_markdown
    assert "100" in result.answer_markdown
    assert "喜劇張力" in result.answer_markdown
    assert "虛構者" not in result.answer_markdown
    assert "1988" not in result.answer_markdown
    assert "99" not in result.answer_markdown
    assert [item.citation_id for item in result.citations] == [
        "metadata:1987_FGBR_001",
        "pdf:fgbr:p3",
    ]


def test_natural_conjunction_partitions_runtime_from_visual_analysis() -> None:
    service, _, _, generator = _service(
        _flying_mr_boo_evidence(),
        "擠迫構圖形成喜劇張力。[pdf:fgbr:p3]",
        ("pdf:fgbr:p3",),
    )

    result = service.answer("富貴逼人的片長和視覺風格？")

    assert generator.calls[0][0] == "視覺風格？"
    assert [item.source_kind for item in generator.calls[0][1]] == ["pdf_page"]
    assert "100" in result.answer_markdown
    assert "99" not in result.answer_markdown
    assert [item.citation_id for item in result.citations] == [
        "metadata:1987_FGBR_001",
        "pdf:fgbr:p3",
    ]


def test_canonical_conjunction_keeps_cast_and_runtime_in_one_metadata_branch() -> None:
    service, _, _, generator = _service(
        _flying_mr_boo_evidence(), "must not be used", ()
    )

    result = service.answer("富貴逼人的主演和片長？")

    assert "董驃、沈殿霞、曾志偉" in result.answer_markdown
    assert "100" in result.answer_markdown
    assert generator.calls == []


def test_mixed_exact_target_keeps_metadata_when_pdf_has_no_safe_span() -> None:
    evidence = _flying_mr_boo_evidence()
    evidence[1]["body"] = "主演：虛構演員；全片共99分鐘。"
    service, _, _, _ = _service(
        evidence,
        "模型自行補寫視覺分析。[pdf:fgbr:p3]",
        ("pdf:fgbr:p3",),
    )

    result = service.answer("富貴逼人的片長和視覺風格？")

    assert "100" in result.answer_markdown
    assert "目前沒有可安全抽取的定性分析證據" in result.answer_markdown
    assert "99" not in result.answer_markdown
    assert [item.citation_id for item in result.citations] == [
        "metadata:1987_FGBR_001"
    ]


@pytest.mark.parametrize("connector", ("以及", "跟"))
def test_natural_mixed_connectors_preserve_clean_qualitative_prompt(
    connector: str,
) -> None:
    service, _, _, generator = _service(
        _flying_mr_boo_evidence(),
        "擠迫構圖形成喜劇張力。[pdf:fgbr:p3]",
        ("pdf:fgbr:p3",),
    )

    result = service.answer(f"富貴逼人的片長{connector}視覺風格？")

    assert generator.calls[0][0] == "視覺風格？"
    assert "100" in result.answer_markdown
    assert [item.citation_id for item in result.citations] == [
        "metadata:1987_FGBR_001",
        "pdf:fgbr:p3",
    ]


@pytest.mark.parametrize("title", ("第一類型危險", "臨時演員"))
def test_exact_title_canonical_words_do_not_create_false_metadata_intent(
    title: str,
) -> None:
    metadata = _passage(
        "metadata:title-words",
        "title-words",
        chinese_title=title,
        english_title="Canonical Word Title",
    )
    pdf = _passage(
        "pdf:title-words:p1",
        "title-words",
        chinese_title=title,
        english_title="Canonical Word Title",
        passage_kind="pdf",
        page_number=1,
        source_filename="title-words.pdf",
    )
    pdf["body"] = "構圖形成不安感。"
    service, _, _, generator = _service(
        [metadata, pdf],
        "構圖形成不安感。[pdf:title-words:p1]",
        ("pdf:title-words:p1",),
    )

    result = service.answer(f"{title}的視覺風格如何？")

    assert [item.passage_id for item in generator.calls[0][1]] == [
        "pdf:title-words:p1"
    ]
    assert [item.citation_id for item in result.citations] == [
        "pdf:title-words:p1"
    ]


def test_simplified_query_masks_traditional_exact_title_before_intent_scan() -> None:
    metadata = _passage(
        "metadata:temporary-actor",
        "temporary-actor",
        chinese_title="臨時演員",
        english_title="Temporary Actor",
    )
    pdf = _passage(
        "pdf:temporary-actor:p1",
        "temporary-actor",
        chinese_title="臨時演員",
        english_title="Temporary Actor",
        passage_kind="pdf",
        page_number=1,
        source_filename="temporary-actor.pdf",
    )
    pdf["body"] = "構圖形成不安感。"
    service, _, _, generator = _service(
        [metadata, pdf],
        "構圖形成不安感。[pdf:temporary-actor:p1]",
        ("pdf:temporary-actor:p1",),
    )

    result = service.answer("临时演员的视觉风格如何？")

    assert [item.passage_id for item in generator.calls[0][1]] == [
        "pdf:temporary-actor:p1"
    ]
    assert [item.citation_id for item in result.citations] == [
        "pdf:temporary-actor:p1"
    ]


@pytest.mark.parametrize(
    ("question", "expected"),
    (
        ("临时演员的上映日期？", "1978-10-05"),
        ("临时演员的主演？", "測試演員"),
    ),
)
def test_simplified_exact_title_canonical_intents_use_metadata_only(
    question: str, expected: str
) -> None:
    metadata = _passage(
        "metadata:temporary-actor",
        "temporary-actor",
        chinese_title="臨時演員",
        english_title="Temporary Actor",
    )
    pdf = _passage(
        "pdf:temporary-actor:p1",
        "temporary-actor",
        chinese_title="臨時演員",
        english_title="Temporary Actor",
        passage_kind="pdf",
        page_number=1,
        source_filename="temporary-actor.pdf",
    )
    service, _, _, generator = _service(
        [metadata, pdf],
        "模型不應被調用。[pdf:temporary-actor:p1]",
        ("pdf:temporary-actor:p1",),
    )

    result = service.answer(question)

    assert expected in result.answer_markdown
    assert [item.citation_id for item in result.citations] == [
        "metadata:temporary-actor"
    ]
    assert generator.calls == []


@pytest.mark.parametrize(
    "deep_wording",
    [
        "燈光手法",
        "灯光手法",
        "角色塑造",
        "如何呈現",
        "如何呈现",
        "摄影风格",
    ],
)
def test_exact_metadata_only_target_is_not_upgraded_by_unrelated_pdf(
    deep_wording: str,
) -> None:
    target_metadata = _passage(
        "metadata:no-deep",
        "no-deep",
        chinese_title="沒有深度文檔",
        english_title="Metadata Only",
    )
    unrelated_pdf = _passage(
        "pdf:unrelated:p1",
        "unrelated",
        chinese_title="其他深度電影",
        english_title="Unrelated Deep Film",
        passage_kind="pdf",
        page_number=1,
        source_filename="unrelated.pdf",
        distance=0.001,
    )
    service, _, _, generator = _service(
        [unrelated_pdf, target_metadata],
        "錯誤地借用了別片分析。[pdf:unrelated:p1]",
        ("pdf:unrelated:p1",),
    )

    result = service.answer(f"沒有深度文檔的{deep_wording}是怎樣的？")

    assert "目前只有結構化電影資料" in result.answer_markdown
    assert [citation.citation_id for citation in result.citations] == ["metadata:no-deep"]
    assert [movie.movie_id for movie in result.movies] == ["no-deep"]
    assert generator.calls == []


@pytest.mark.parametrize(
    "question",
    [
        "無間道透過色彩傳達了甚麼？",
        "無間道的畫面如何營造張力？",
        "無間道導演用了甚麼手法？",
        "請問一下無間道導演用了甚麼手法？",
        "我想問一下無間道導演用了甚麼手法？",
        "How does Infernal Affairs use colour to convey emotion?",
    ],
)
def test_metadata_only_exact_target_refuses_every_non_whitelisted_intent(
    question: str,
) -> None:
    target_metadata = _passage(
        "metadata:ia",
        "2002_IA_001",
        chinese_title="無間道",
        english_title="Infernal Affairs",
    )
    unrelated_pdf = _passage(
        "pdf:unrelated:p1",
        "unrelated",
        chinese_title="其他深度電影",
        english_title="Unrelated Deep Film",
        passage_kind="pdf",
        page_number=1,
        source_filename="unrelated.pdf",
        distance=0.001,
    )
    service, _, _, generator = _service(
        [unrelated_pdf, target_metadata],
        "不應借用其他電影的內容。[pdf:unrelated:p1]",
        ("pdf:unrelated:p1",),
    )

    result = service.answer(question)

    assert "目前只有結構化電影資料" in result.answer_markdown
    assert [citation.citation_id for citation in result.citations] == ["metadata:ia"]
    assert generator.calls == []


@pytest.mark.parametrize(
    "question",
    [
        "無間道是哪一年上映？",
        "無間道的導演是誰？",
        "請問一下無間道的導演是誰？",
        "请问一下无间道的导演是谁？",
        "我想問一下無間道的導演是誰？",
        "我想问一下无间道的导演是谁？",
        "無間道的主演有哪些？",
        "What is the runtime of Infernal Affairs?",
        "Who stars in Infernal Affairs?",
        "Tell me the basic information about Infernal Affairs.",
    ],
)
def test_metadata_only_exact_target_allows_present_canonical_metadata_intents(
    question: str,
) -> None:
    target_metadata = _passage(
        "metadata:ia",
        "2002_IA_001",
        chinese_title="無間道",
        english_title="Infernal Affairs",
    )
    unrelated_pdf = _passage(
        "pdf:unrelated:p1",
        "unrelated",
        chinese_title="其他深度電影",
        english_title="Unrelated Deep Film",
        passage_kind="pdf",
        page_number=1,
        source_filename="unrelated.pdf",
        distance=0.001,
    )
    service, _, _, generator = _service(
        [unrelated_pdf, target_metadata],
        "結構化資料可以回答。[metadata:ia]",
        ("metadata:ia",),
    )

    result = service.answer(question)

    assert "目前只有結構化電影資料" not in result.answer_markdown
    assert result.answer_markdown.endswith("[metadata:ia]")
    assert [item.citation_id for item in result.citations] == ["metadata:ia"]
    assert generator.calls == []


@pytest.mark.parametrize("question", ["無間道使用甚麼語言？", "What is its rating, 無間道?"])
def test_metadata_whitelist_still_refuses_fields_absent_from_the_passage(question: str) -> None:
    target_metadata = _passage(
        "metadata:ia",
        "2002_IA_001",
        chinese_title="無間道",
        english_title="Infernal Affairs",
    )
    service, _, _, generator = _service(
        [target_metadata],
        "模型不應補完缺失欄位。[metadata:ia]",
        ("metadata:ia",),
    )

    result = service.answer(question)

    assert "目前只有結構化電影資料" in result.answer_markdown
    assert generator.calls == []


def test_no_retrieved_evidence_returns_deterministic_insufficient_answer() -> None:
    service, _, _, generator = _service([], "must not be used", ())

    result = service.answer("香港電影資料庫完全沒有命中的問題")

    assert result.answer_markdown == "目前沒有足夠的檢索證據回答這個問題。"
    assert result.citations == ()
    assert result.movies == ()
    assert generator.calls == []


@pytest.mark.parametrize(
    "question",
    [
        "推荐一本好书。",
        "推薦一部好看的小說。",
        "recommend a good novel",
        "推薦一首好歌。",
        "推荐一首流行歌曲。",
        "recommend a good song",
        "推薦一款好玩的電子遊戲。",
        "推荐一个好玩的游戏。",
        "recommend a fun video game",
        "推薦一家北京餐廳。",
        "推荐一家好吃的餐厅。",
        "recommend a restaurant",
        "今天天氣怎麼樣？",
        "今天天气怎么样？",
        "what is the weather today?",
        "最近哪隻股票值得買？",
        "比特币今天多少钱？",
        "which crypto should I buy?",
        "幫我寫一封請假郵件。",
        "把這段中文改寫成英文。",
        "rewrite this email in English",
        "幫我總結這份合同。",
        "帮我总结这份合同。",
        "summarize this contract",
        "頭痛應該吃什麼藥？",
        "头痛应该吃什么药？",
        "what medicine should I take for a headache?",
        "幫我制定健身計劃。",
        "帮我制定健身计划。",
        "make me a fitness plan",
        "這週 NBA 比賽結果。",
        "这周足球比赛结果。",
        "what was the sports result?",
        "番茄炒蛋怎麼做？",
        "番茄炒蛋怎么做？",
        "give me a recipe",
        "如何學好粵語？",
        "如何学好粤语？",
        "how can I learn Cantonese?",
        "1995年香港發生了什麼大事？",
        "家庭理財有哪些方法？",
        "如何分析一部小說的劇情？",
        "香港家庭理財有哪些方法？",
        "誰是這本小說的導演？",
        "這部遊戲的劇情怎麼樣？",
        "香港犯罪率是多少？",
        "香港戰爭歷史",
    ],
)
def test_non_movie_domains_are_rejected_before_embedding_generation_or_search(
    question: str,
) -> None:
    """Breaks if recommendation wording or nearest neighbours can bypass the movie gate."""
    service, repository, embedder, generator = _service([], "must not be used", ())

    result = service.answer(question)

    assert result == ChatAnswer(
        answer_markdown="我目前只能回答这个 Release 内的香港电影问题。",
        citations=(),
        movies=(),
    )
    assert embedder.calls == []
    assert generator.calls == []
    assert repository.calls == []
    assert repository.recommendation_calls == []
    assert repository.deep_recommendation_calls == []


def test_exact_release_title_cannot_override_an_explicit_non_movie_object() -> None:
    """Breaks if a known movie title turns a book request into movie retrieval."""
    passage = _passage(
        "metadata:1978_ZQ_001",
        "1978_ZQ_001",
        chinese_title="醉拳",
        english_title="Drunken Master",
    )
    service, repository, embedder, generator = _service(
        [passage], "must not be used", ()
    )

    result = service.answer("推薦一本關於《醉拳》的好書")

    assert result == ChatAnswer(
        answer_markdown="我目前只能回答这个 Release 内的香港电影问题。",
        citations=(),
        movies=(),
    )
    assert repository.calls == []
    assert repository.recommendation_calls == []
    assert repository.deep_recommendation_calls == []
    assert embedder.calls == []
    assert generator.calls == []


@pytest.mark.parametrize(
    ("title", "movie_id"),
    [
        ("合約情人", "2007_CONTRACT_LOVER_001"),
        ("生化藥屍", "2017_BIO_RAIDERS_001"),
    ],
)
def test_exact_title_with_credit_question_overrides_title_token_ood_collision(
    title: str, movie_id: str
) -> None:
    """Breaks if an OOD word inside a release title blocks its movie metadata."""
    citation_id = f"metadata:{movie_id}"
    passage = _passage(
        citation_id,
        movie_id,
        chinese_title=title,
        english_title="Collision Title",
    )
    service, repository, embedder, generator = _service(
        [passage],
        f"《{title}》的導演有受控資料。[{citation_id}]",
        (citation_id,),
    )

    result = service.answer(f"{title}的導演是誰？")

    assert result.citations[0].citation_id == citation_id
    assert repository.calls[0]["target_movie_ids"] == (movie_id,)
    assert len(embedder.calls) == 1
    assert generator.calls == []


def test_analyze_exact_contract_title_is_not_misclassified_as_legal_ood() -> None:
    """Breaks if an analysis verb plus a movie title is treated as a legal task."""
    passage = _passage(
        "metadata:2007_CONTRACT_LOVER_001",
        "2007_CONTRACT_LOVER_001",
        chinese_title="合約情人",
        english_title="Contract Lover",
    )
    service, repository, embedder, generator = _service(
        [passage], "must not be used", ()
    )

    result = service.answer("分析合約情人的導演風格")

    assert result.answer_markdown != "我目前只能回答这个 Release 内的香港电影问题。"
    assert result.citations[0].citation_id == "metadata:2007_CONTRACT_LOVER_001"
    assert repository.calls[0]["target_movie_ids"] == ("2007_CONTRACT_LOVER_001",)
    assert len(embedder.calls) == 1
    assert generator.calls == []


@pytest.mark.parametrize(
    ("english_title", "movie_id"),
    [
        ("How is the weather to-day?", "1974_WEATHER_001"),
        ("Just like weather", "1986_WEATHER_001"),
        ("Fitness Tour", "1997_FITNESS_001"),
        ("Love recipe", "1994_RECIPE_001"),
    ],
)
@pytest.mark.parametrize(
    "question_template",
    [
        "Who directed {title}",
        "{title} 的導演是誰？",
    ],
)
def test_exact_english_title_with_director_question_overrides_ood_word_collision(
    english_title: str, movie_id: str, question_template: str
) -> None:
    """Breaks if a release title's English words are treated as a non-movie task."""
    citation_id = f"metadata:{movie_id}"
    passage = _passage(
        citation_id,
        movie_id,
        chinese_title=f"受控片{movie_id[-3:]}",
        english_title=english_title,
    )
    service, repository, embedder, generator = _service(
        [passage],
        f"The director is governed.[{citation_id}]",
        (citation_id,),
    )

    result = service.answer(question_template.format(title=english_title))

    assert result.citations[0].citation_id == citation_id
    assert repository.calls[0]["target_movie_ids"] == (movie_id,)
    assert len(embedder.calls) == 1
    assert generator.calls == []


@pytest.mark.parametrize(
    "question",
    [
        "合約情人今天天氣？",
        "合約情人的導演是誰？另外今天天氣？",
    ],
)
def test_exact_title_cannot_override_explicit_weather_ood(question: str) -> None:
    """Breaks if exact-title presence alone bypasses an unrelated weather request."""
    passage = _passage(
        "metadata:2007_CONTRACT_LOVER_001",
        "2007_CONTRACT_LOVER_001",
        chinese_title="合約情人",
        english_title="Contract Lover",
    )
    service, repository, embedder, generator = _service(
        [passage], "must not be used", ()
    )

    result = service.answer(question)

    assert result.answer_markdown == "我目前只能回答这个 Release 内的香港电影问题。"
    assert repository.calls == []
    assert embedder.calls == []
    assert generator.calls == []


def test_person_biography_does_not_fall_back_to_generic_movie_vectors() -> None:
    """Breaks if the existence of a release credit turns biography into movie evidence."""
    service, repository, embedder, generator = _service([], "must not be used", ())
    repository.person_resolutions["周星馳收入多少？"] = ("周星馳", None)

    result = service.answer("周星馳收入多少？")

    assert result.answer_markdown == "我目前只能回答这个 Release 内的香港电影问题。"
    assert result.citations == ()
    assert result.movies == ()
    assert embedder.calls == []
    assert generator.calls == []
    assert repository.calls == []
    assert repository.recommendation_calls == []


def test_fresh_exact_title_resolution_precedes_movie_domain_gate_and_cutoff() -> None:
    """Breaks if an exact release title without a generic movie word is rejected or tail-filtered."""
    passage = _passage(
        "metadata:1978_ZQ_001",
        "1978_ZQ_001",
        chinese_title="醉拳",
        english_title="Drunken Master",
        distance=0.91,
    )
    service, repository, _, generator = _service(
        [passage],
        "《醉拳》於 1978 年上映。[metadata:1978_ZQ_001]",
        ("metadata:1978_ZQ_001",),
    )

    result = service.answer("醉拳是哪年")

    assert result.movies[0].movie_id == "1978_ZQ_001"
    assert repository.explicit_target_calls == [(RELEASE_ID, "醉拳是哪年")]
    assert repository.calls[0]["target_movie_ids"] == ("1978_ZQ_001",)
    assert [item.citation_id for item in result.citations] == [
        "metadata:1978_ZQ_001"
    ]
    assert generator.calls == []


@pytest.mark.parametrize(
    "question",
    [
        "醉拳怎麼樣？",
        "醉拳好不好看？",
        "醉拳值得看嗎？",
        "醉拳講什麼？",
        "醉拳誰演的？",
        "電影醉拳的導演是誰？",
        "影片醉拳的導演是誰？",
    ],
)
def test_common_title_first_movie_questions_resolve_the_exact_release_title(
    question: str,
) -> None:
    evidence = (
        _evidence_from_record(
            _passage(
                "metadata:1978_ZQ_001",
                "1978_ZQ_001",
                chinese_title="醉拳",
                english_title="Drunken Master",
            )
        ),
    )

    assert _exact_target_movie_ids(question, evidence) == ("1978_ZQ_001",)


def test_exact_title_cannot_authorize_a_title_owned_non_movie_object() -> None:
    passage = _passage(
        "metadata:1980_YX_001",
        "1980_YX_001",
        chinese_title="英雄",
        english_title="Hero",
    )
    service, repository, embedder, generator = _service(
        [passage], "must not be used", ()
    )

    result = service.answer("英雄的小說劇情是什麼？")

    assert result.answer_markdown == "我目前只能回答这个 Release 内的香港电影问题。"
    assert repository.calls == []
    assert repository.recommendation_calls == []
    assert repository.deep_recommendation_calls == []
    assert embedder.calls == []
    assert generator.calls == []


@pytest.mark.parametrize(
    "question",
    [
        "《英雄》小說講什麼？",
        "《英雄》這本小說講什麼？",
        "《英雄》這本書講什麼？",
        "《英雄》書講什麼？",
        "《英雄》书讲什么？",
        "《英雄》書的內容？",
        "電影英雄書講什麼？",
        "香港電影英雄書的內容？",
        "電影富貴逼人書講什麼？",
        "《英雄》書值得看嗎？",
        "《英雄》書的作者是誰？",
        "《英雄》書出版於哪年？",
        "《英雄》書評怎麼樣？",
        "《英雄》書讀起來如何？",
        "《英雄》書和電影比較",
        "《英雄》改編成的小說講什麼？",
        "《英雄》相關的遊戲怎麼樣？",
        "《英雄》遊戲怎麼樣？",
        "1980_YX_001小說講什麼？",
        "1980_YX_001改編的小說講什麼？",
    ],
)
def test_delimited_title_or_movie_id_cannot_authorize_a_following_nonmovie_object(
    question: str,
) -> None:
    passage = _passage(
        "metadata:1980_YX_001",
        "1980_YX_001",
        chinese_title="英雄",
        english_title="Hero",
    )
    evidence = (_evidence_from_record(passage),)
    assert _exact_target_movie_ids(question, evidence) == ()

    service, repository, embedder, generator = _service(
        [passage], "must not be used", ()
    )
    result = service.answer(question)

    assert result == ChatAnswer(
        answer_markdown="我目前只能回答这个 Release 内的香港电影问题。",
        citations=(),
        movies=(),
    )
    assert repository.calls == []
    assert repository.recommendation_calls == []
    assert repository.deep_recommendation_calls == []
    assert embedder.calls == []
    assert generator.calls == []


@pytest.mark.parametrize(
    "question",
    [
        "《死亡遊戲》好看嗎？",
        "死亡遊戲怎麼樣？",
        "推薦電影《死亡遊戲》",
        "推薦《死亡遊戲》",
    ],
)
def test_full_release_title_containing_nonmovie_word_remains_an_exact_target(
    question: str,
) -> None:
    passage = _passage(
        "metadata:1978_SWYX_001",
        "1978_SWYX_001",
        chinese_title="死亡遊戲",
        english_title="Game of Death",
    )
    evidence = (_evidence_from_record(passage),)

    assert _exact_target_movie_ids(question, evidence) == ("1978_SWYX_001",)
    service, repository, _, _ = _service([passage], "must not be used", ())

    result = service.answer(question)

    assert result.answer_markdown != "我目前只能回答这个 Release 内的香港电影问题。"
    assert repository.calls[0]["target_movie_ids"] == ("1978_SWYX_001",)
    assert result.movies[0].movie_id == "1978_SWYX_001"


def test_collective_identity_residual_masks_every_movie_in_comparison() -> None:
    passages = [
        _passage(
            "metadata:1978_SWYX_001",
            "1978_SWYX_001",
            chinese_title="死亡遊戲",
            english_title="Game of Death",
        ),
        _passage(
            "metadata:1978_ZQ_001",
            "1978_ZQ_001",
            chinese_title="醉拳",
            english_title="Drunken Master",
        ),
    ]
    evidence = tuple(_evidence_from_record(passage) for passage in passages)

    assert _exact_target_movie_ids("比較《死亡遊戲》和《醉拳》", evidence) == (
        "1978_SWYX_001",
        "1978_ZQ_001",
    )


@pytest.mark.parametrize(
    ("question", "movie_id", "chinese_title", "english_title"),
    [
        (
            "recommend Lung Fung Restaurant",
            "1990_LFR_001",
            "龍鳳茶樓",
            "Lung Fung Restaurant",
        ),
        (
            "recommend Love recipe",
            "1994_LR_001",
            "愛情食譜",
            "Love recipe",
        ),
        ("推薦《音樂殭屍》", "1992_YYJS_001", "音樂殭屍", "The Musical Vampire"),
    ],
)
def test_recommendation_object_words_inside_full_release_titles_are_masked(
    question: str,
    movie_id: str,
    chinese_title: str,
    english_title: str,
) -> None:
    passage = _passage(
        f"metadata:{movie_id}",
        movie_id,
        chinese_title=chinese_title,
        english_title=english_title,
    )
    evidence = (_evidence_from_record(passage),)

    assert _exact_target_movie_ids(question, evidence) == (movie_id,)
    service, repository, _, _ = _service([passage], "must not be used", ())

    result = service.answer(question)

    assert result.answer_markdown != "我目前只能回答这个 Release 内的香港电影问题。"
    assert repository.calls[0]["target_movie_ids"] == (movie_id,)
    assert result.movies[0].movie_id == movie_id


@pytest.mark.parametrize(
    ("question", "movie_id", "chinese_title", "english_title"),
    [
        (
            "The cannonball run",
            "1981_PDFC_001",
            "炮彈飛車",
            "The cannonball run",
        ),
        (
            "What about The cannonball run?",
            "1981_PDFC_001",
            "炮彈飛車",
            "The cannonball run",
        ),
        (
            "recommend The cannonball run",
            "1981_PDFC_001",
            "炮彈飛車",
            "The cannonball run",
        ),
        (
            "How is the weather to-day?",
            "1974_HNSBB_001",
            "何日卿再來",
            "How is the weather to-day?",
        ),
        ("Hello", "2017_HYBB_001", "藍天白雲", "Hello"),
    ],
)
def test_strong_ood_words_inside_exact_release_title_use_identity_residual(
    question: str,
    movie_id: str,
    chinese_title: str,
    english_title: str,
) -> None:
    assert has_strong_out_of_domain_signal(question) is True
    passage = _passage(
        f"metadata:{movie_id}",
        movie_id,
        chinese_title=chinese_title,
        english_title=english_title,
    )
    citation_id = f"metadata:{movie_id}"
    service, repository, embedder, _ = _service(
        [passage], f"受控回答。[{citation_id}]", (citation_id,)
    )

    result = service.answer(question)

    assert result.answer_markdown != "我目前只能回答这个 Release 内的香港电影问题。"
    assert repository.calls[0]["target_movie_ids"] == (movie_id,)
    assert result.movies[0].movie_id == movie_id
    assert len(embedder.calls) == 1


def test_long_release_title_span_suppresses_competing_short_title_movie() -> None:
    passages = [
        _passage(
            "metadata:1994_AQSXW_001",
            "1994_AQSXW_001",
            chinese_title="愛情食譜",
            english_title="Love recipe",
        ),
        _passage(
            "metadata:1997_CJWDZNZ_001",
            "1997_CJWDZNZ_001",
            chinese_title="初戀無限Touch",
            english_title="Love",
        ),
    ]
    evidence = tuple(_evidence_from_record(passage) for passage in passages)

    assert _exact_target_movie_ids("recommend Love recipe", evidence) == (
        "1994_AQSXW_001",
    )
    service, repository, _, _ = _service(passages, "must not be used", ())

    result = service.answer("recommend Love recipe")

    assert repository.calls[0]["target_movie_ids"] == ("1994_AQSXW_001",)
    assert [movie.movie_id for movie in result.movies] == ["1994_AQSXW_001"]


def test_nonoverlapping_short_and_long_titles_remain_a_real_comparison() -> None:
    evidence = (
        _evidence_from_record(
            _passage(
                "metadata:1997_CJWDZNZ_001",
                "1997_CJWDZNZ_001",
                chinese_title="初戀無限Touch",
                english_title="Love",
            )
        ),
        _evidence_from_record(
            _passage(
                "metadata:1994_AQSXW_001",
                "1994_AQSXW_001",
                chinese_title="愛情食譜",
                english_title="Love recipe",
            )
        ),
    )

    assert _exact_target_movie_ids("compare Love and Love recipe", evidence) == (
        "1997_CJWDZNZ_001",
        "1994_AQSXW_001",
    )


def test_strong_ood_text_outside_exact_title_remains_rejected() -> None:
    passage = _passage(
        "metadata:1980_YX_001",
        "1980_YX_001",
        chinese_title="英雄",
        english_title="Hero",
    )
    service, repository, embedder, generator = _service(
        [passage], "must not be used", ()
    )

    result = service.answer("《英雄》今天天氣怎麼樣？")

    assert result.answer_markdown == "我目前只能回答这个 Release 内的香港电影问题。"
    assert repository.calls == []
    assert embedder.calls == []
    assert generator.calls == []


def test_exact_release_movie_analysis_can_use_an_ood_subject_as_film_content() -> None:
    """Breaks if a film about sport is rejected merely because its subject is football."""
    passage = _passage(
        "pdf:shaolin-soccer-deep-analysis-v1:p1",
        "2001_SLZQ_001",
        chinese_title="少林足球",
        english_title="Shaolin Soccer",
        passage_kind="pdf",
        page_number=1,
        source_filename="S 級港片-少林足球.pdf",
        genre="喜劇、動作",
    )
    passage["body"] = "廣角鏡頭與漫畫式 CG 把足球、功夫和喜劇動作結合。"
    citation_id = "pdf:shaolin-soccer-deep-analysis-v1:p1"
    question = "《少林足球》如何把足球、功夫和喜劇結合？"
    assert has_strong_out_of_domain_signal(question) is True
    service, repository, embedder, _ = _service(
        [passage], f"受控回答。[{citation_id}]", (citation_id,)
    )

    result = service.answer(question)

    assert result.answer_markdown != "我目前只能回答这个 Release 内的香港电影问题。"
    assert repository.calls[0]["target_movie_ids"] == ("2001_SLZQ_001",)
    assert [citation.citation_id for citation in result.citations] == [citation_id]
    assert len(embedder.calls) == 1


@pytest.mark.parametrize(
    "question",
    [
        "recommend a restaurant",
        "recommend a song",
        "recommend a recipe",
    ],
)
def test_recommendation_nonmovie_objects_without_title_targets_fail_closed(
    question: str,
) -> None:
    service, repository, embedder, generator = _service([], "must not be used", ())

    result = service.answer(question)

    assert result.answer_markdown == "我目前只能回答这个 Release 内的香港电影问题。"
    assert repository.calls == []
    assert repository.recommendation_calls == []
    assert repository.deep_recommendation_calls == []
    assert embedder.calls == []
    assert generator.calls == []


def test_music_movie_recommendation_remains_in_domain() -> None:
    service, repository, embedder, _ = _service([], "must not be used", ())

    result = service.answer("推薦音樂電影")

    assert result.answer_markdown != "我目前只能回答这个 Release 内的香港电影问题。"
    assert len(repository.recommendation_calls) == 1
    assert len(embedder.calls) == 1


def test_person_semantics_cannot_authorize_a_nonmovie_game_request() -> None:
    question = "推薦周星馳的電影遊戲"
    service, repository, embedder, generator = _service([], "must not be used", ())
    repository.person_resolutions[question] = ("周星馳", None)

    result = service.answer(question)

    assert result == ChatAnswer(
        answer_markdown="我目前只能回答这个 Release 内的香港电影问题。",
        citations=(),
        movies=(),
    )
    assert repository.calls == []
    assert repository.recommendation_calls == []
    assert repository.deep_recommendation_calls == []
    assert repository.person_resolution_calls == []
    assert embedder.calls == []
    assert generator.calls == []


@pytest.mark.parametrize("question", ["英雄联盟怎麼玩？", "英雄聯盟怎麼玩？"])
def test_short_release_title_inside_non_movie_phrase_is_not_an_explicit_target(
    question: str,
) -> None:
    """Breaks if a short release title can authorize an unrelated product phrase."""
    passage = _passage(
        "metadata:1980_YX_001",
        "1980_YX_001",
        chinese_title="英雄",
        english_title="Hero",
    )
    service, repository, embedder, generator = _service(
        [passage], "must not be used", ()
    )

    result = service.answer(question)

    assert result == ChatAnswer(
        answer_markdown="我目前只能回答这个 Release 内的香港电影问题。",
        citations=(),
        movies=(),
    )
    assert repository.calls == []
    assert repository.recommendation_calls == []
    assert repository.deep_recommendation_calls == []
    assert embedder.calls == []
    assert generator.calls == []


def test_polite_analysis_lead_in_keeps_short_title_target_scope() -> None:
    """A short title plus movie-specific suffix must not fall back corpus-wide."""
    passage = _passage(
        "metadata:1978_ZQ_001",
        "1978_ZQ_001",
        chinese_title="醉拳",
        english_title="Drunken Master",
    )
    service, repository, _, generator = _service(
        [passage],
        "不應使用。[metadata:1978_ZQ_001]",
        ("metadata:1978_ZQ_001",),
    )

    result = service.answer("请分析一下醉拳的视觉风格")

    assert repository.calls[0]["target_movie_ids"] == ("1978_ZQ_001",)
    assert [citation.citation_id for citation in result.citations] == [
        "metadata:1978_ZQ_001"
    ]
    assert generator.calls == []


@pytest.mark.parametrize(
    "question",
    [
        "我想问一下醉拳的导演是谁？",
        "请问一下醉拳的导演是谁？",
        "我想問一下醉拳的導演是誰？",
        "請問一下醉拳的導演是誰？",
    ],
)
def test_polite_exact_title_director_question_uses_canonical_metadata(
    question: str,
) -> None:
    passage = _passage(
        "metadata:1978_ZQ_001",
        "1978_ZQ_001",
        chinese_title="醉拳",
        english_title="Drunken Master",
        director="袁和平",
    )
    service, repository, _, generator = _service([passage], "must not be used", ())

    result = service.answer(question)

    assert result.answer_markdown == "《醉拳》導演：袁和平。[metadata:1978_ZQ_001]"
    assert [item.citation_id for item in result.citations] == [
        "metadata:1978_ZQ_001"
    ]
    assert repository.calls[0]["target_movie_ids"] == ("1978_ZQ_001",)
    assert generator.calls == []


@pytest.mark.parametrize(
    "question",
    (
        "我想问一下醉拳的导演是谁？",
        "请问一下醉拳的导演是谁？",
        "关于醉拳的视觉风格",
        "想知道醉拳的剧情",
        "能介绍一下醉拳的动作设计吗？",
        "可不可以聊聊醉拳的喜剧创作？",
    ),
)
def test_bounded_request_grammar_keeps_short_title_target_scope(
    question: str,
) -> None:
    """A bounded request plus movie-specific suffix may identify a short title."""
    resolution = retrieval.resolve_explicit_movie_identity_context(
        question,
        (("1978_ZQ_001", "醉拳", "Drunken Master"),),
    )

    assert resolution.movie_ids == ("1978_ZQ_001",)
    assert resolution.ambiguous is False


@pytest.mark.parametrize(
    "question",
    (
        "《英雄》的影像書寫有什麼特色？",
        "《英雄》中書信如何推動劇情？",
        "《英雄》裡的書架有什麼作用？",
        "讲一下富贵逼人的喜剧书写",
    ),
)
def test_movie_analysis_book_compounds_remain_in_movie_scope(question: str) -> None:
    movie_id = "1987_FGBR_001" if "富" in question else "1980_YX_001"
    title = "富貴逼人" if movie_id == "1987_FGBR_001" else "英雄"
    passage = _passage(
        f"metadata:{movie_id}",
        movie_id,
        chinese_title=title,
        english_title="Fixture",
    )
    service, repository, _, generator = _service(
        [passage],
        f"不應使用。[metadata:{movie_id}]",
        (f"metadata:{movie_id}",),
    )

    result = service.answer(question)

    assert repository.calls[0]["target_movie_ids"] == (movie_id,)
    assert [citation.citation_id for citation in result.citations] == [
        f"metadata:{movie_id}"
    ]
    assert generator.calls == []


@pytest.mark.parametrize(
    ("question", "governed_title"),
    (
        ("講一下她的想法", "她"),
        ("屋的面積怎麼算？", "屋"),
        ("聊聊朋友之間的信任", "朋友"),
        ("談談天堂是否存在", "天堂"),
        ("請分析海燕的遷徙習性", "海燕"),
        ("講一下英雄的成長", "英雄"),
    ),
)
def test_common_short_release_titles_do_not_authorize_ordinary_language(
    question: str, governed_title: str
) -> None:
    """Breaks if a topic-independent grammar treats common nouns as movie scope."""
    passage = _passage(
        "metadata:common-title",
        "common-title",
        chinese_title=governed_title,
        english_title="Common Title",
    )
    service, repository, embedder, generator = _service(
        [passage],
        "不應使用。[metadata:common-title]",
        ("metadata:common-title",),
    )

    result = service.answer(question)

    assert result == ChatAnswer(
        answer_markdown="我目前只能回答这个 Release 内的香港电影问题。",
        citations=(),
        movies=(),
    )
    assert repository.calls == []
    assert embedder.calls == []
    assert generator.calls == []


def test_generic_search_filters_only_records_beyond_the_policy_cutoff() -> None:
    """Breaks if generic nearest-neighbour tails can be supplied as grounded evidence."""
    kept = _passage(
        "metadata:kept",
        "kept",
        chinese_title="保留電影",
        english_title="Kept Film",
        distance=0.36,
    )
    dropped = _passage(
        "metadata:dropped",
        "dropped",
        chinese_title="尾部電影",
        english_title="Tail Film",
        distance=0.360001,
    )
    service, _, _, generator = _service(
        [kept, dropped],
        "香港電影有受控資料。[metadata:kept]",
        ("metadata:kept",),
    )

    result = service.answer("香港電影類型通常是什麼？")

    assert result.citations[0].citation_id == "metadata:kept"
    assert [passage.passage_id for passage in generator.calls[0][1]] == [
        "metadata:kept"
    ]
    assert generator.calls[0][1][0].distance == 0.36


@pytest.mark.parametrize(
    ("distance", "remove_distance"),
    [
        (True, False),
        (float("nan"), False),
        (float("inf"), False),
        (-0.000001, False),
        (2.000001, False),
        (0.2, True),
    ],
)
def test_generic_search_rejects_missing_or_invalid_cosine_distance(
    distance: object, remove_distance: bool
) -> None:
    """Breaks if malformed repository distance metadata is ignored or coerced."""
    passage = _passage(
        "metadata:movie",
        "movie",
        chinese_title="電影",
        english_title="Film",
    )
    passage["distance"] = distance
    if remove_distance:
        passage.pop("distance")
    service, _, _, generator = _service(
        [passage],
        "不應生成。[metadata:movie]",
        ("metadata:movie",),
    )

    with pytest.raises(GroundingError, match="distance"):
        service.answer("香港電影資料")

    assert generator.calls == []


@pytest.mark.parametrize(
    ("question", "passage_kind", "source_filename"),
    [
        ("推薦1部喜劇電影", "metadata", None),
        ("推薦1部攝影出色的喜劇電影", "pdf", "deep.pdf"),
    ],
    ids=["structured-recommendation", "deep-recommendation"],
)
def test_structured_searches_reject_malformed_distance_without_applying_cutoff(
    question: str,
    passage_kind: str,
    source_filename: str | None,
) -> None:
    """Breaks if predicate-constrained retrieval bypasses distance validation too."""
    passage = _passage(
        "metadata:movie" if passage_kind == "metadata" else "pdf:movie:p1",
        "movie",
        chinese_title="受控喜劇",
        english_title="Governed Comedy",
        genre="喜劇",
        passage_kind=passage_kind,
        page_number=1 if passage_kind == "pdf" else 0,
        source_filename=source_filename,
    )
    passage["distance"] = float("nan")
    citation_id = str(passage["passage_id"])
    service, _, _, generator = _service(
        [passage],
        f"《受控喜劇》：符合受控條件。[{citation_id}]",
        (citation_id,),
    )

    with pytest.raises(GroundingError, match="distance"):
        service.answer(question)

    assert generator.calls == []


def test_planner_normalizes_genre_tier_decade_and_clamps_requested_count() -> None:
    """Breaks if simplified recommendation constraints are sent to semantic search only."""
    plan = retrieval.plan_recommendation("请推荐99部1980年代S级喜剧电影", ())

    assert plan == RecommendationPlan(
        requested_count=8,
        genres=("喜劇",),
        tier="S",
        year_from=1980,
        year_to=1989,
        diversify_decades=False,
        continuation=False,
        excluded_movie_ids=(),
        context_text="请推荐99部1980年代S级喜剧电影",
    )
    english_decade = retrieval.plan_recommendation(
        "recommend comedy movies from the 1980s", ()
    )
    assert english_decade is not None
    assert (english_decade.year_from, english_decade.year_to) == (1980, 1989)
    assert english_decade.genres == ("喜劇",)


def test_follow_up_year_override_inherits_genre_count_and_excludes_prior_movies() -> None:
    """Breaks if a continuation loses filters or intersects an explicit override with old years."""
    history = (
        ConversationExchange(
            "推薦3部1980年代香港喜劇",
            "上一輪回答不可以成為證據。",
            ("old-one", "old-two", "old-three"),
        ),
    )

    plan = retrieval.plan_recommendation("换成2010年以后", history)

    assert plan is not None
    assert plan.requested_count == 3
    assert plan.genres == ("喜劇",)
    assert (plan.year_from, plan.year_to) == (2010, None)
    assert plan.continuation is True
    assert plan.excluded_movie_ids == ("old-one", "old-two", "old-three")
    assert "上一輪回答" not in plan.context_text


def test_new_recommendation_does_not_inherit_unstated_old_filters() -> None:
    """Breaks if inheritance leaks beyond continuation or replacement turns."""
    history = (
        ConversationExchange(
            "推薦3部1980年代S級喜劇",
            "上一輪。",
            ("old-one", "old-two", "old-three"),
        ),
    )

    plan = retrieval.plan_recommendation("推薦2部動作片", history)

    assert plan is not None
    assert plan.requested_count == 2
    assert plan.genres == ("動作",)
    assert plan.tier is None
    assert (plan.year_from, plan.year_to) == (None, None)
    assert plan.excluded_movie_ids == ()


@pytest.mark.parametrize(
    "question",
    [
        "喜劇電影有哪些常見主題？",
        "列出動作電影的視覺美學風格",
        "犯罪片有哪些典型敘事手法？",
    ],
)
def test_genre_qualified_deep_analysis_stays_on_generic_fail_closed_path(
    question: str,
) -> None:
    """Breaks if list words route unsupported thematic claims as recommendations."""
    assert retrieval.plan_recommendation(question, ()) is None
    passage = _passage(
        "metadata:genre-result",
        "genre-result",
        chinese_title="類型片",
        english_title="Genre Film",
        genre="喜劇",
    )
    service, repository, _, generator = _service(
        [passage],
        "目前只有結構化電影資料，不能提供沒有深度文檔支持的分析。"
        "[metadata:genre-result]",
        ("metadata:genre-result",),
    )

    result = service.answer(question)

    assert "不能提供沒有深度文檔支持的分析" in result.answer_markdown
    assert len(repository.calls) == 1
    assert repository.recommendation_calls == []
    assert generator.calls[0][2] == "general"


@pytest.mark.parametrize(
    "question",
    [
        "推薦3部攝影出色的喜劇電影",
        "推荐3部视觉风格鲜明的喜剧电影",
        "推薦3部動作設計出色的功夫電影",
    ],
)
def test_explicit_recommendation_wording_cannot_override_deep_analysis(
    question: str,
) -> None:
    """Breaks if metadata recommendation evidence can answer a requested deep criterion."""
    assert retrieval.plan_recommendation(question, ()) is None


@pytest.mark.parametrize(
    "question",
    [
        "list the themes in comedy movies",
        "list common narratives in crime movies",
        "list the aesthetics and visual style of action films",
        "list the cinematography and lighting in comedy films",
        "list symbolism in crime movies",
    ],
)
def test_english_deep_analysis_aliases_suppress_list_recommendation_routing(
    question: str,
) -> None:
    """Breaks if English analytical nouns are treated as structured movie filters."""
    assert retrieval.plan_recommendation(question, ()) is None


def test_english_deep_aliases_are_boundary_aware_and_explicit_recommendation_still_refuses() -> None:
    substring_control = retrieval.plan_recommendation(
        "list comedy movies tagged themepark", ()
    )
    explicit_recommendation = retrieval.plan_recommendation(
        "recommend comedy movies with a distinctive visual style", ()
    )

    assert substring_control is not None
    assert substring_control.genres == ("喜劇",)
    assert explicit_recommendation is None


def test_continuation_chain_reconstructs_filters_and_accumulates_exclusions() -> None:
    """Breaks if the newest continuation erases constraints from the original request."""
    history = (
        ConversationExchange(
            "推薦3部1980年代S級喜劇", "第一輪。", ("a", "b", "c")
        ),
        ConversationExchange(
            "還有別的喜劇嗎？", "第二輪。", ("d", "e", "f")
        ),
    )

    plan = retrieval.plan_recommendation("再來", history)

    assert plan is not None
    assert plan.requested_count == 3
    assert plan.genres == ("喜劇",)
    assert plan.tier == "S"
    assert (plan.year_from, plan.year_to) == (1980, 1989)
    assert plan.excluded_movie_ids == ("a", "b", "c", "d", "e", "f")


def test_title_s_storm_is_not_misread_as_an_s_tier_recommendation() -> None:
    """Breaks if any title beginning with S is interpreted as the literal S tier."""
    assert retrieval.plan_recommendation("S风暴是什么电影？", ()) is None
    evidence = (
        EvidencePassage(
            passage_id="metadata:s-storm",
            movie_id="2016_SFB_001",
            source_kind="movie_metadata",
            body="movie_id: 2016_SFB_001\nchinese_title: S風暴",
            page_number=None,
            source_filename=None,
            movie={"movie_id": "2016_SFB_001", "chinese_title": "S風暴"},
            poster_available=False,
        ),
    )
    assert _exact_target_movie_ids("S风暴是什么电影？", evidence) == (
        "2016_SFB_001",
    )


@pytest.mark.parametrize(
    "question",
    (
        "推荐周星驰的好电影",
        "推薦周星馳的好電影",
    ),
)
def test_simplified_and_traditional_person_requests_resolve_one_canonical_name(
    question: str,
) -> None:
    """Breaks if query normalization cannot bind both scripts to the governed credit token."""
    passage = _passage(
        "metadata:kung-fu",
        "movie-kung-fu",
        chinese_title="功夫",
        english_title="Kung Fu Hustle",
        cast="周星馳、黃聖依",
    )
    service, repository, _, generator = _service([passage], "must not be used", ())
    repository.person_resolutions[question] = ("周星馳", None)

    result = service.answer(question)

    assert repository.recommendation_calls[0].person_name == "周星馳"
    assert repository.recommendation_calls[0].person_role is None
    assert repository.calls == []
    assert generator.calls == []
    assert result.movies == ()


@pytest.mark.parametrize(
    ("question", "role", "director", "cast"),
    (
        ("推薦1部演員周星馳的電影", "actor", "測試導演", "周星馳、黃聖依"),
        ("推薦1部周星馳導演的電影", "director", "周星馳", "測試演員"),
    ),
)
def test_actor_only_and_director_only_requests_preserve_the_resolved_role(
    question: str,
    role: str,
    director: str,
    cast: str,
) -> None:
    """Breaks if an explicit role qualifier is widened to the other credit column."""
    passage = _passage(
        "metadata:role-match",
        "movie-role-match",
        chinese_title="角色命中",
        english_title="Role Match",
        director=director,
        cast=cast,
    )
    service, repository, _, generator = _service(
        [passage],
        "《角色命中》：符合指定人物條件。[metadata:role-match]",
        ("metadata:role-match",),
    )
    repository.person_resolutions[question] = ("周星馳", role)

    result = service.answer(question)

    assert repository.recommendation_calls[0].person_name == "周星馳"
    assert repository.recommendation_calls[0].person_role == role
    assert [movie.movie_id for movie in result.movies] == ["movie-role-match"]
    assert [passage.movie_id for passage in generator.calls[0][1]] == [
        "movie-role-match"
    ]


def test_equivalent_credit_token_passes_service_validation_and_reaches_generation() -> None:
    """Breaks if service validation accepts only the display spelling of one person class."""
    question = "推薦1部廖启智主演的電影"
    passage = _passage(
        "metadata:equivalent-credit",
        "movie-equivalent-credit",
        chinese_title="等價字形電影",
        english_title="Equivalent Credit Film",
        cast="廖啟智、其他演員",
    )
    service, repository, _, generator = _service(
        [passage],
        "《等價字形電影》：符合指定人物條件。[metadata:equivalent-credit]",
        ("metadata:equivalent-credit",),
    )
    repository.person_resolutions[question] = (
        "廖啓智",
        "actor",
        ("廖啓智", "廖啟智"),
    )

    result = service.answer(question)

    plan = repository.recommendation_calls[0]
    assert plan.person_name == "廖啓智"
    assert plan.person_exact_names == ("廖啓智", "廖啟智")
    assert [movie.movie_id for movie in result.movies] == ["movie-equivalent-credit"]
    assert len(generator.calls) == 1


def test_person_genre_year_filter_rejects_an_unrelated_repository_candidate() -> None:
    """Breaks if a vector-near non-credit movie can reach generation or movie cards."""
    unrelated = _passage(
        "metadata:unrelated",
        "movie-unrelated",
        chinese_title="無關喜劇",
        english_title="Unrelated Comedy",
        release_date="1992-01-01",
        genre="喜劇",
        cast="其他演員",
        distance=0.01,
    )
    matching = _passage(
        "metadata:matching",
        "movie-matching",
        chinese_title="人物喜劇",
        english_title="Person Comedy",
        release_date="1993-01-01",
        genre="喜劇",
        cast="周星馳、吳孟達",
        distance=0.20,
    )
    question = "推薦1部1990年代周星馳主演的喜劇電影"
    service, repository, _, generator = _service(
        [unrelated, matching],
        "《人物喜劇》符合全部條件。[metadata:matching]",
        ("metadata:matching",),
    )
    repository.person_resolutions[question] = ("周星馳", "actor")

    with pytest.raises(GroundingError, match="person-qualified search"):
        service.answer(question)

    plan = repository.recommendation_calls[0]
    assert (plan.person_name, plan.person_role) == ("周星馳", "actor")
    assert plan.genres == ("喜劇",)
    assert (plan.year_from, plan.year_to) == (1990, 1999)
    assert generator.calls == []


@pytest.mark.parametrize("question", ("周星驰的动作喜剧", "周星馳的動作喜劇"))
def test_bare_person_genre_query_uses_exact_structured_retrieval(
    question: str,
) -> None:
    """Breaks if a complete person/genre request falls into generic vector search."""
    passages = [
        _passage(
            f"metadata:chow-{index}",
            f"chow-{index}",
            chinese_title=f"周星馳動作喜劇{index}",
            english_title=f"Chow Action Comedy {index}",
            genre="喜劇、動作",
            cast="周星馳、吳孟達",
        )
        for index in range(5)
    ]
    citation_ids = tuple(str(item["passage_id"]) for item in passages)
    answer = "\n".join(
        f"《周星馳動作喜劇{index}》：符合人物及雙類型條件。[metadata:chow-{index}]"
        for index in range(5)
    )
    service, repository, _, _ = _service(passages, answer, citation_ids)
    repository.person_resolutions[question] = ("周星馳", None)

    result = service.answer(question)

    assert len(result.movies) == 5
    assert repository.calls == []
    assert repository.person_resolution_calls == [(RELEASE_ID, question)]
    plan = repository.recommendation_calls[0]
    assert (plan.person_name, plan.person_role) == ("周星馳", None)
    assert plan.genres_all == ("喜劇", "動作")
    assert plan.excluded_movie_ids == ()


def test_fresh_bare_person_query_resets_prior_person_context() -> None:
    """Breaks if a complete current person request consults or inherits prior identity."""
    current = "周星馳的動作喜劇"
    previous = "王家衛的電影"
    passages = [
        _passage(
            f"metadata:fresh-chow-{index}",
            f"fresh-chow-{index}",
            chinese_title=f"新周星馳電影{index}",
            english_title=f"Fresh Chow Film {index}",
            genre="喜劇、動作",
            cast="周星馳、吳孟達",
        )
        for index in range(5)
    ]
    citation_ids = tuple(str(item["passage_id"]) for item in passages)
    answer = "\n".join(
        f"《新周星馳電影{index}》：符合新人物條件。[metadata:fresh-chow-{index}]"
        for index in range(5)
    )
    service, repository, _, _ = _service(passages, answer, citation_ids)
    repository.person_resolutions[current] = ("周星馳", None)
    repository.person_resolutions[previous] = ("王家衛", "director")
    history = (ConversationExchange(previous, "上一輪。", ("old-wong",)),)

    result = service.answer(current, history)

    assert len(result.movies) == 5
    assert repository.calls == []
    assert repository.person_resolution_calls == [(RELEASE_ID, current)]
    assert len(repository.recommendation_calls) == 1
    plan = repository.recommendation_calls[0]
    assert (plan.person_name, plan.person_role) == ("周星馳", None)
    assert plan.genres_all == ("喜劇", "動作")
    assert plan.excluded_movie_ids == ()


def test_bare_person_continuation_inherits_person_genres_and_exclusions() -> None:
    """Breaks if a true continuation loses a bare-person plan or its prior exclusions."""
    current = "還有別的嗎？"
    previous = "周星馳的動作喜劇"
    passages = [
        _passage(
            f"metadata:more-chow-{index}",
            f"more-chow-{index}",
            chinese_title=f"另一部周星馳電影{index}",
            english_title=f"Another Chow Film {index}",
            genre="喜劇、動作",
            cast="周星馳、吳孟達",
        )
        for index in range(5)
    ]
    citation_ids = tuple(str(item["passage_id"]) for item in passages)
    answer = "\n".join(
        f"《另一部周星馳電影{index}》：符合延續條件。[metadata:more-chow-{index}]"
        for index in range(5)
    )
    service, repository, _, _ = _service(passages, answer, citation_ids)
    repository.person_resolutions[previous] = ("周星馳", None)
    history = (
        ConversationExchange(previous, "上一輪。", ("old-chow-a", "old-chow-b")),
    )

    result = service.answer(current, history)

    assert len(result.movies) == 5
    assert repository.calls == []
    assert repository.person_resolution_calls == [
        (RELEASE_ID, current),
        (RELEASE_ID, previous),
    ]
    assert len(repository.recommendation_calls) == 1
    plan = repository.recommendation_calls[0]
    assert (plan.person_name, plan.person_role) == ("周星馳", None)
    assert plan.genres_all == ("喜劇", "動作")
    assert plan.excluded_movie_ids == ("old-chow-a", "old-chow-b")


@pytest.mark.parametrize(
    ("question", "resolved_role", "release_date", "genre", "expected_genres", "years"),
    (
        ("周星馳主演的喜劇", "actor", "1992-01-01", "喜劇", ("喜劇",), (None, None)),
        ("周星馳的1990年代電影", None, "1992-01-01", "動作", (), (1990, 1999)),
    ),
)
def test_bare_person_role_and_year_queries_preserve_hard_constraints(
    question: str,
    resolved_role: str | None,
    release_date: str,
    genre: str,
    expected_genres: tuple[str, ...],
    years: tuple[int | None, int | None],
) -> None:
    """Breaks if person resolution erases an explicit role, genre, or year predicate."""
    passages = [
        _passage(
            f"metadata:bounded-person-{index}",
            f"bounded-person-{index}",
            chinese_title=f"人物約束電影{index}",
            english_title=f"Bounded Person Film {index}",
            release_date=release_date,
            genre=genre,
            cast="周星馳、吳孟達",
        )
        for index in range(5)
    ]
    citation_ids = tuple(str(item["passage_id"]) for item in passages)
    answer = "\n".join(
        f"《人物約束電影{index}》：符合人物硬條件。[metadata:bounded-person-{index}]"
        for index in range(5)
    )
    service, repository, _, _ = _service(passages, answer, citation_ids)
    repository.person_resolutions[question] = ("周星馳", resolved_role)

    result = service.answer(question)

    assert len(result.movies) == 5
    assert repository.calls == []
    assert repository.person_resolution_calls == [(RELEASE_ID, question)]
    assert len(repository.recommendation_calls) == 1
    plan = repository.recommendation_calls[0]
    assert (plan.person_name, plan.person_role) == ("周星馳", resolved_role)
    assert plan.genres == expected_genres
    assert (plan.year_from, plan.year_to) == years
    assert plan.excluded_movie_ids == ()


def test_longer_release_person_uses_exact_structured_retrieval() -> None:
    """Breaks if a five-character Release credit degrades to generic vector search."""
    question = "大島由加里的電影"
    passages = [
        _passage(
            f"metadata:yukari-{index}",
            f"yukari-{index}",
            chinese_title=f"大島由加里電影{index}",
            english_title=f"Yukari Film {index}",
            cast="大島由加里、測試演員",
        )
        for index in range(5)
    ]
    citation_ids = tuple(str(item["passage_id"]) for item in passages)
    answer = "\n".join(
        f"《大島由加里電影{index}》：符合精確人物條件。[metadata:yukari-{index}]"
        for index in range(5)
    )
    service, repository, embedder, generator = _service(passages, answer, citation_ids)
    repository.person_resolutions[question] = ("大島由加里", None)

    result = service.answer(question)

    assert len(result.movies) == 5
    assert repository.person_resolution_calls == [(RELEASE_ID, question)]
    assert repository.calls == []
    assert len(repository.recommendation_calls) == 1
    plan = repository.recommendation_calls[0]
    assert (plan.person_name, plan.person_exact_names) == (
        "大島由加里",
        ("大島由加里",),
    )
    assert len(embedder.calls) == 1
    assert len(generator.calls) == 1


@pytest.mark.parametrize(
    "question",
    (
        "推薦周星馳和成龍的電影",
        "推薦成龍和甄子丹的電影",
        "推薦周星馳與成龍電影",
        "周星馳的電影和成龍的作品",
        "推薦周星馳的電影以及成龍的作品",
        "周星馳主演的電影或成龍主演的作品",
        "推薦袁和平和成龍的電影",
        "甲乙和丙丁和戊己和庚辛和壬癸和天地和寅卯和辰巳和午未和申酉和戌亥和玄黃的電影",
    ),
)
def test_multiple_governed_people_clarifies_before_resolution_or_embedding(
    question: str,
) -> None:
    """Breaks if a multi-person request silently selects the first governed identity."""
    service, repository, embedder, generator = _service([], "must not be used", ())

    result = service.answer(question)

    assert result.answer_markdown == (
        "目前一次只支援一位正向人物；請只指定一位導演或演員後再試。"
    )
    assert result.citations == ()
    assert result.movies == ()
    assert repository.person_resolution_calls == []
    assert repository.recommendation_calls == []
    assert repository.deep_recommendation_calls == []
    assert repository.calls == []
    assert repository.poster_batch_calls == []
    assert embedder.calls == []
    assert generator.calls == []


def test_strong_ood_returns_before_person_resolution_or_recommendation_work() -> None:
    """Breaks if deterministic OOD rejection depends on Release person SQL succeeding."""
    question = "推薦周星馳的電影，最近哪隻股票值得買？"
    service, repository, embedder, generator = _service([], "must not be used", ())

    def fail_person_resolution(_release_id: str, _question: str) -> object:
        raise RagDatabaseError("person SQL must not run for strong OOD")

    repository.resolve_recommendation_person = fail_person_resolution  # type: ignore[method-assign]

    result = service.answer(question)

    assert result == ChatAnswer(
        answer_markdown="我目前只能回答这个 Release 内的香港电影问题。",
        citations=(),
        movies=(),
    )
    assert repository.explicit_target_calls == [(RELEASE_ID, question)]
    assert repository.person_resolution_calls == []
    assert repository.recommendation_calls == []
    assert repository.deep_recommendation_calls == []
    assert repository.calls == []
    assert repository.poster_batch_calls == []
    assert repository.readiness_calls == []
    assert repository.state_calls == []
    assert embedder.calls == []
    assert generator.calls == []


@pytest.mark.parametrize(
    "question",
    (
        "推薦電影，周星馳跟成龍都可以",
        "想看點電影，周星馳或成龍都行",
        "請比較周星馳跟成龍，推薦電影",
    ),
)
def test_repository_person_ambiguity_maps_to_exact_service_clarification(
    question: str,
) -> None:
    """Breaks if repository ambiguity is mistaken for no current identity."""
    service, repository, embedder, generator = _service([], "must not be used", ())
    repository.ambiguous_person_questions.add(question)

    result = service.answer(question)

    assert result == ChatAnswer(
        answer_markdown="目前一次只支援一位正向人物；請只指定一位導演或演員後再試。",
        citations=(),
        movies=(),
    )
    assert repository.person_resolution_calls == [(RELEASE_ID, question)]
    assert repository.recommendation_calls == []
    assert repository.deep_recommendation_calls == []
    assert repository.calls == []
    assert repository.poster_batch_calls == []
    assert repository.readiness_calls == []
    assert repository.state_calls == []
    assert embedder.calls == []
    assert generator.calls == []


def test_decorated_mixed_script_people_clarify_without_any_downstream_calls() -> None:
    """Breaks if decorated mixed-script people bypass the one-person boundary."""
    question = "想看周星驰和成龍電影，推薦幾部"
    service, repository, embedder, generator = _service([], "must not be used", ())

    result = service.answer(question)

    assert result == ChatAnswer(
        answer_markdown="目前一次只支援一位正向人物；請只指定一位導演或演員後再試。",
        citations=(),
        movies=(),
    )
    assert repository.person_resolution_calls == []
    assert repository.recommendation_calls == []
    assert repository.deep_recommendation_calls == []
    assert repository.calls == []
    assert repository.poster_batch_calls == []
    assert embedder.calls == []
    assert generator.calls == []


def test_exclusionary_person_clarifies_before_resolution_or_embedding() -> None:
    """Breaks if a negative person predicate is widened into generic retrieval."""
    question = "不要周星馳的喜劇"
    service, repository, embedder, generator = _service([], "must not be used", ())

    result = service.answer(question)

    assert result.answer_markdown == (
        "目前一次只支援一位正向人物；請只指定一位導演或演員後再試。"
    )
    assert result.citations == ()
    assert result.movies == ()
    assert repository.person_resolution_calls == []
    assert repository.recommendation_calls == []
    assert repository.calls == []
    assert embedder.calls == []
    assert generator.calls == []


@pytest.mark.parametrize(
    "question",
    (
        "別推薦周星馳的電影",
        "不想看周星馳的電影",
        "請不要推薦周星馳的電影",
        "请别推荐周星驰的电影",
        "再推薦三部，別周星馳",
        "再推薦三部，不想看周星馳",
    ),
)
def test_polite_exclusionary_person_forms_stop_before_every_downstream_boundary(
    question: str,
) -> None:
    """Breaks if polite negative wording becomes a positive recommendation."""
    service, repository, embedder, generator = _service([], "must not be used", ())

    result = service.answer(question)

    assert result == ChatAnswer(
        answer_markdown="目前一次只支援一位正向人物；請只指定一位導演或演員後再試。",
        citations=(),
        movies=(),
    )
    assert repository.person_resolution_calls == []
    assert repository.recommendation_calls == []
    assert repository.deep_recommendation_calls == []
    assert repository.calls == []
    assert repository.poster_batch_calls == []
    assert repository.readiness_calls == []
    assert repository.state_calls == []
    assert embedder.calls == []
    assert generator.calls == []


@pytest.mark.parametrize(
    (
        "current_question",
        "previous_question",
        "person_resolution",
        "expected_genres",
        "expected_person",
        "expected_role",
    ),
    (
        (
            "再推薦三部，不要重複。",
            "請推薦三部喜劇電影。",
            None,
            ("喜劇",),
            None,
            None,
        ),
        (
            "再推薦3部，不要重複。",
            "推薦3部周星馳主演的電影。",
            ("周星馳", "actor"),
            (),
            "周星馳",
            "actor",
        ),
        (
            "recommend 3 more movies without repeats",
            "請推薦三部喜劇電影。",
            None,
            ("喜劇",),
            None,
            None,
        ),
    ),
    ids=("generic-comedy", "person-history", "english-generic-comedy"),
)
def test_no_repeat_follow_up_reaches_structured_retrieval_without_person_clarification(
    current_question: str,
    previous_question: str,
    person_resolution: tuple[str, str] | None,
    expected_genres: tuple[str, ...],
    expected_person: str | None,
    expected_role: str | None,
) -> None:
    """Breaks if the live-smoke continuation is mistaken for a negative person."""
    passages = [
        _passage(
            "metadata:new-first",
            "new-first",
            chinese_title="新喜劇一",
            english_title="New Comedy One",
            genre="喜劇",
            cast="周星馳、測試演員",
        ),
        _passage(
            "metadata:new-second",
            "new-second",
            chinese_title="新喜劇二",
            english_title="New Comedy Two",
            genre="喜劇",
            cast="周星馳、測試演員",
        ),
        _passage(
            "metadata:new-third",
            "new-third",
            chinese_title="新喜劇三",
            english_title="New Comedy Three",
            genre="喜劇",
            cast="周星馳、測試演員",
        ),
    ]
    service, repository, embedder, generator = _service(
        passages,
        (
            "《新喜劇一》：第一個新選擇。[metadata:new-first]\n"
            "《新喜劇二》：第二個新選擇。[metadata:new-second]\n"
            "《新喜劇三》：第三個新選擇。[metadata:new-third]"
        ),
        ("metadata:new-first", "metadata:new-second", "metadata:new-third"),
    )
    if person_resolution is not None:
        repository.person_resolutions[previous_question] = person_resolution
    history = (
        ConversationExchange(
            previous_question,
            "上一輪回答不可以成為證據。",
            ("old-first", "old-second", "old-third"),
        ),
    )

    result = service.answer(current_question, history)

    assert [movie.movie_id for movie in result.movies] == [
        "new-first",
        "new-second",
        "new-third",
    ]
    assert repository.person_resolution_calls == [
        (RELEASE_ID, current_question),
        (RELEASE_ID, previous_question),
    ]
    assert len(repository.recommendation_calls) == 1
    plan = repository.recommendation_calls[0]
    assert plan.requested_count == 3
    assert plan.genres == expected_genres
    assert plan.continuation is True
    assert plan.excluded_movie_ids == ("old-first", "old-second", "old-third")
    assert (plan.person_name, plan.person_role) == (expected_person, expected_role)
    assert len(embedder.calls) == 1
    assert len(generator.calls) == 1


def test_explicit_same_release_person_dedup_inherits_the_successful_chain() -> None:
    """Breaks if a canonical same-person restatement resets no-repeat history."""
    previous = "推薦3部1980年代S級周星馳主演的喜劇電影"
    current = "推薦周星馳主演的電影，不要重複"
    history = (
        ConversationExchange(previous, "上一輪。", ("old-a", "old-b", "old-c")),
    )
    baseline = retrieval.plan_recommendation(current, history)
    assert baseline is not None
    assert baseline.continuation is False
    assert baseline.excluded_movie_ids == ()
    assert baseline.context_text == current

    passages = [
        _passage(
            f"metadata:same-person-{index}",
            f"same-person-{index}",
            chinese_title=f"周星馳喜劇{index}",
            english_title=f"Same Person Comedy {index}",
            release_date=f"198{index}-01-01",
            genre="喜劇",
            tier="S",
            cast="周星馳、測試演員",
        )
        for index in range(1, 4)
    ]
    service, repository, _, _ = _service(
        passages,
        "\n".join(
            f"《周星馳喜劇{index}》：同一人物的新選擇。[metadata:same-person-{index}]"
            for index in range(1, 4)
        ),
        tuple(f"metadata:same-person-{index}" for index in range(1, 4)),
    )
    exact_names = ("周星馳", "周星驰")
    repository.person_resolutions[current] = ("周星馳", "actor", exact_names)
    repository.person_resolutions[previous] = ("周星驰", "actor", exact_names)

    result = service.answer(current, history)

    assert [movie.movie_id for movie in result.movies] == [
        "same-person-1",
        "same-person-2",
        "same-person-3",
    ]
    assert repository.person_resolution_calls == [
        (RELEASE_ID, current),
        (RELEASE_ID, previous),
    ]
    plan = repository.recommendation_calls[0]
    assert plan.requested_count == 3
    assert plan.genres == ("喜劇",)
    assert plan.tier == "S"
    assert (plan.year_from, plan.year_to) == (1980, 1989)
    assert plan.continuation is True
    assert plan.excluded_movie_ids == ("old-a", "old-b", "old-c")
    assert plan.context_text == f"{previous}\n{current}"
    assert (plan.person_name, plan.person_role) == ("周星馳", "actor")


def test_explicit_different_release_person_dedup_keeps_the_current_reset() -> None:
    """Breaks if no-repeat wording leaks an older person's state into a new person."""
    previous = "推薦3部1980年代S級周星馳主演的喜劇電影"
    current = "推薦成龍主演的電影，不要重複"
    history = (
        ConversationExchange(previous, "上一輪。", ("old-a", "old-b", "old-c")),
    )
    passages = [
        _passage(
            f"metadata:different-person-{index}",
            f"different-person-{index}",
            chinese_title=f"成龍電影{index}",
            english_title=f"Different Person Film {index}",
            release_date=f"199{index}-01-01",
            genre="動作",
            tier="A",
            cast="成龍、測試演員",
        )
        for index in range(1, 6)
    ]
    service, repository, _, _ = _service(
        passages,
        "\n".join(
            f"《成龍電影{index}》：新人物選擇。[metadata:different-person-{index}]"
            for index in range(1, 6)
        ),
        tuple(f"metadata:different-person-{index}" for index in range(1, 6)),
    )
    repository.person_resolutions[current] = ("成龍", "actor")
    repository.person_resolutions[previous] = ("周星馳", "actor")

    result = service.answer(current, history)

    assert len(result.movies) == 5
    assert repository.person_resolution_calls == [
        (RELEASE_ID, current),
        (RELEASE_ID, previous),
    ]
    plan = repository.recommendation_calls[0]
    assert plan.requested_count == 5
    assert plan.genres == ()
    assert plan.tier is None
    assert (plan.year_from, plan.year_to) == (None, None)
    assert plan.continuation is False
    assert plan.excluded_movie_ids == ()
    assert plan.context_text == current
    assert (plan.person_name, plan.person_role) == ("成龍", "actor")


def test_same_person_dedup_authority_survives_a_third_turn_replay() -> None:
    """Breaks if a proven same-person continuation is only fixed for one request."""
    first = "推薦3部1980年代S級周星馳主演的喜劇電影"
    second = "推薦周星馳主演的電影，不要重複"
    current = "再推薦3部，不要重複"
    history = (
        ConversationExchange(first, "第一輪。", ("old-a", "old-b", "old-c")),
        ConversationExchange(second, "第二輪。", ("old-d", "old-e", "old-f")),
    )
    passages = [
        _passage(
            f"metadata:persistent-same-{index}",
            f"persistent-same-{index}",
            chinese_title=f"延續喜劇{index}",
            english_title=f"Persistent Comedy {index}",
            release_date=f"198{index}-01-01",
            genre="喜劇",
            tier="S",
            cast="周星馳、測試演員",
        )
        for index in range(1, 4)
    ]
    service, repository, _, _ = _service(
        passages,
        "\n".join(
            f"《延續喜劇{index}》：同一人物續問。[metadata:persistent-same-{index}]"
            for index in range(1, 4)
        ),
        tuple(f"metadata:persistent-same-{index}" for index in range(1, 4)),
    )
    exact_names = ("周星馳", "周星驰")
    repository.person_resolutions[first] = ("周星馳", "actor", exact_names)
    repository.person_resolutions[second] = ("周星驰", "actor", exact_names)

    result = service.answer(current, history)

    assert len(result.movies) == 3
    plan = repository.recommendation_calls[0]
    assert plan.requested_count == 3
    assert plan.genres == ("喜劇",)
    assert plan.tier == "S"
    assert (plan.year_from, plan.year_to) == (1980, 1989)
    assert plan.continuation is True
    assert plan.excluded_movie_ids == (
        "old-a",
        "old-b",
        "old-c",
        "old-d",
        "old-e",
        "old-f",
    )
    assert plan.context_text == f"{first}\n{second}\n{current}"
    assert plan.person_name in exact_names
    assert plan.person_role == "actor"


def test_different_person_dedup_authority_survives_a_third_turn_replay() -> None:
    """Breaks if a proven person reset later resurrects the previous identity."""
    first = "推薦3部1980年代S級周星馳主演的喜劇電影"
    second = "推薦成龍主演的電影，不要重複"
    current = "再推薦5部，不要重複"
    history = (
        ConversationExchange(first, "第一輪。", ("old-chow-a", "old-chow-b")),
        ConversationExchange(second, "第二輪。", ("old-jackie-a", "old-jackie-b")),
    )
    passages = [
        _passage(
            f"metadata:persistent-reset-{index}",
            f"persistent-reset-{index}",
            chinese_title=f"成龍動作片{index}",
            english_title=f"Persistent Reset {index}",
            release_date=f"199{index}-01-01",
            genre="動作",
            tier="A",
            cast="成龍、測試演員",
        )
        for index in range(1, 6)
    ]
    service, repository, _, _ = _service(
        passages,
        "\n".join(
            f"《成龍動作片{index}》：人物切換後續問。[metadata:persistent-reset-{index}]"
            for index in range(1, 6)
        ),
        tuple(f"metadata:persistent-reset-{index}" for index in range(1, 6)),
    )
    repository.person_resolutions[first] = ("周星馳", "actor")
    repository.person_resolutions[second] = ("成龍", "actor")

    result = service.answer(current, history)

    assert len(result.movies) == 5
    plan = repository.recommendation_calls[0]
    assert plan.requested_count == 5
    assert plan.genres == ()
    assert plan.tier is None
    assert (plan.year_from, plan.year_to) == (None, None)
    assert plan.continuation is True
    assert plan.excluded_movie_ids == ("old-jackie-a", "old-jackie-b")
    assert plan.context_text == f"{second}\n{current}"
    assert (plan.person_name, plan.person_role) == ("成龍", "actor")


@pytest.mark.parametrize(
    "current",
    (
        "推薦成龍電影，不要重複",
        "推薦成龍主演電影，不要重複",
        "推薦3部成龍的動作片，不要重複",
        "recommend 3 成龍 movies, no duplicates",
    ),
)
def test_repository_resolved_person_reset_does_not_depend_on_parser_shape(
    current: str,
) -> None:
    """Breaks if parser gaps let a new Release person inherit the old person's state."""
    previous = "推薦3部1980年代S級周星馳主演的喜劇電影"
    history = (
        ConversationExchange(previous, "上一輪。", ("old-a", "old-b", "old-c")),
    )
    service, repository, _, _ = _service([], "must not be used", ())
    repository.person_resolutions[previous] = ("周星馳", "actor")
    repository.person_resolutions[current] = ("成龍", "actor")

    service.answer(current, history)

    plan = repository.recommendation_calls[0]
    assert (plan.person_name, plan.person_role) == ("成龍", "actor")
    assert plan.continuation is False
    assert plan.excluded_movie_ids == ()
    assert plan.tier is None
    assert (plan.year_from, plan.year_to) == (None, None)
    assert "喜劇" not in plan.genres
    assert "周星馳" not in plan.context_text


@pytest.mark.parametrize(
    "current",
    (
        "再推薦3部成龍主演的電影",
        "recommend 3 more 成龍 movies",
    ),
)
def test_repository_resolved_current_person_resets_even_without_dedup(
    current: str,
) -> None:
    """Breaks if identity precedence is limited to no-repeat wording."""
    previous = "推薦3部1980年代S級周星馳主演的喜劇電影"
    history = (
        ConversationExchange(previous, "上一輪。", ("old-a", "old-b", "old-c")),
    )
    service, repository, _, _ = _service([], "must not be used", ())
    repository.person_resolutions[previous] = ("周星馳", "actor")
    repository.person_resolutions[current] = ("成龍", "actor")

    service.answer(current, history)

    plan = repository.recommendation_calls[0]
    assert (plan.person_name, plan.person_role) == ("成龍", "actor")
    assert plan.requested_count == 3
    assert plan.continuation is False
    assert plan.genres == ()
    assert plan.tier is None
    assert (plan.year_from, plan.year_to) == (None, None)
    assert plan.excluded_movie_ids == ()
    assert plan.context_text == current


def test_repository_confirmed_same_person_overrides_lexical_switch_boundary() -> None:
    """Breaks if a surface switch discards exclusions after identity equivalence."""
    previous = "推薦3部1980年代S級周星馳主演的喜劇電影"
    current = "那周星馳呢，不要重複"
    history = (
        ConversationExchange(previous, "上一輪。", ("old-a", "old-b", "old-c")),
    )
    service, repository, _, _ = _service([], "must not be used", ())
    exact_names = ("周星馳", "周星驰")
    repository.person_resolutions[previous] = ("周星馳", "actor", exact_names)
    repository.person_resolutions[current] = ("周星驰", "actor", exact_names)

    service.answer(current, history)

    plan = repository.recommendation_calls[0]
    assert plan.continuation is True
    assert plan.genres == ("喜劇",)
    assert plan.tier == "S"
    assert (plan.year_from, plan.year_to) == (1980, 1989)
    assert plan.excluded_movie_ids == ("old-a", "old-b", "old-c")
    assert plan.context_text == f"{previous}\n{current}"
    assert (plan.person_name, plan.person_role) == ("周星驰", "actor")


def test_same_identity_lexical_switch_survives_a_third_turn_replay() -> None:
    """Breaks if an authority-confirmed continuation is cut on later replay."""
    first = "推薦3部1980年代S級周星馳主演的喜劇電影"
    second = "那周星馳呢，不要重複"
    current = "再推薦3部，不要重複"
    history = (
        ConversationExchange(first, "第一輪。", ("a", "b", "c")),
        ConversationExchange(second, "第二輪。", ("d", "e", "f")),
    )
    service, repository, _, _ = _service([], "must not be used", ())
    exact_names = ("周星馳", "周星驰")
    repository.person_resolutions[first] = ("周星馳", "actor", exact_names)
    repository.person_resolutions[second] = ("周星驰", "actor", exact_names)

    service.answer(current, history)

    plan = repository.recommendation_calls[0]
    assert plan.continuation is True
    assert plan.excluded_movie_ids == ("a", "b", "c", "d", "e", "f")
    assert plan.context_text == f"{first}\n{second}\n{current}"
    assert plan.person_name in exact_names


def test_parser_gap_person_reset_without_dedup_survives_third_turn_replay() -> None:
    """Breaks if a Release-confirmed reset is forgotten after its response."""
    first = "推薦3部1980年代S級周星馳主演的喜劇電影"
    second = "recommend 3 more 成龍 movies"
    current = "再推薦3部，不要重複"
    history = (
        ConversationExchange(first, "第一輪。", ("chow-a", "chow-b", "chow-c")),
        ConversationExchange(second, "第二輪。", ("chan-a", "chan-b", "chan-c")),
    )
    service, repository, _, _ = _service([], "must not be used", ())
    repository.person_resolutions[first] = ("周星馳", "actor")
    repository.person_resolutions[second] = ("成龍", "actor")

    service.answer(current, history)

    plan = repository.recommendation_calls[0]
    assert (plan.person_name, plan.person_role) == ("成龍", "actor")
    assert plan.genres == ()
    assert plan.tier is None
    assert (plan.year_from, plan.year_to) == (None, None)
    assert plan.excluded_movie_ids == ("chan-a", "chan-b", "chan-c")
    assert plan.context_text == f"{second}\n{current}"


def test_terse_different_person_dedup_cannot_fall_back_to_old_plan() -> None:
    """Breaks if a failed reset rebuild silently reuses the old person's state."""
    previous = "推薦3部1980年代S級周星馳主演的喜劇電影"
    current = "成龍，不要重複"
    history = (
        ConversationExchange(previous, "上一輪。", ("old-a", "old-b", "old-c")),
    )
    service, repository, _, _ = _service([], "must not be used", ())
    repository.person_resolutions[previous] = ("周星馳", "actor")
    repository.person_resolutions[current] = ("成龍", "actor")

    service.answer(current, history)

    plan = repository.recommendation_calls[0]
    assert (plan.person_name, plan.person_role) == ("成龍", "actor")
    assert plan.requested_count == 5
    assert plan.continuation is False
    assert plan.genres == ()
    assert plan.tier is None
    assert plan.excluded_movie_ids == ()
    assert plan.context_text == current


def test_generic_continuation_fails_closed_on_active_ambiguous_person_history() -> None:
    """Breaks if an ambiguous historical person is silently broadened to any movie."""
    previous = "推薦周星馳或成龍的喜劇電影，不要重複"
    current = "再推薦3部，不要重複"
    history = (ConversationExchange(previous, "舊客戶歷史。", ("a", "b", "c")),)
    service, repository, embedder, generator = _service([], "must not be used", ())
    repository.ambiguous_person_questions.add(previous)

    result = service.answer(current, history)

    assert result.answer_markdown == (
        "目前一次只支援一位正向人物；請只指定一位導演或演員後再試。"
    )
    assert repository.recommendation_calls == []
    assert repository.calls == []
    assert embedder.calls == []
    assert generator.calls == []


def test_unresolved_english_person_dedup_cannot_fall_back_to_history_identity() -> None:
    """Breaks if an explicit unresolved current person silently becomes the old one."""
    previous = "推薦3部周星馳主演的喜劇電影"
    current = "recommend 3 Jackie Chan movies, no duplicates"
    history = (
        ConversationExchange(previous, "上一輪。", ("old-a", "old-b", "old-c")),
    )
    service, repository, embedder, generator = _service([], "must not be used", ())
    repository.person_resolutions[previous] = ("周星馳", "actor")

    result = service.answer(current, history)

    assert "人物" in result.answer_markdown
    assert "精確解析" in result.answer_markdown
    assert result.citations == ()
    assert result.movies == ()
    assert repository.recommendation_calls == []
    assert repository.deep_recommendation_calls == []
    assert repository.calls == []
    assert embedder.calls == []
    assert generator.calls == []


def test_explicit_person_dedup_ignores_inactive_ambiguous_history() -> None:
    """Breaks if a failed multi-person turn can override one valid current identity."""
    previous = "推薦周星馳或成龍的電影"
    current = "推薦成龍主演的電影，不要重複"
    service, repository, _, _ = _service([], "must not be used", ())
    repository.person_resolutions[current] = ("成龍", "actor")
    repository.ambiguous_person_questions.add(previous)
    history = (ConversationExchange(previous, "舊客戶歷史。", ("old-card",)),)

    result = service.answer(current, history)

    assert result.answer_markdown != (
        "目前一次只支援一位正向人物；請只指定一位導演或演員後再試。"
    )
    assert repository.person_resolution_calls == [(RELEASE_ID, current)]
    plan = repository.recommendation_calls[0]
    assert plan.continuation is False
    assert plan.excluded_movie_ids == ()
    assert plan.context_text == current
    assert (plan.person_name, plan.person_role) == ("成龍", "actor")


@pytest.mark.parametrize(
    "history",
    (
        (),
        (ConversationExchange("推薦三部喜劇電影", "沒有結果。", ()),),
    ),
    ids=("no-history", "zero-card-history"),
)
def test_no_repeat_follow_up_without_successful_history_stops_before_retrieval(
    history: tuple[ConversationExchange, ...],
) -> None:
    """Breaks if deduplication proceeds when there are no prior movie IDs."""
    service, repository, embedder, generator = _service([], "must not be used", ())

    result = service.answer("再推薦三部，不要重複。", history)

    assert result == ChatAnswer(
        answer_markdown="找不到可延續的成功推薦；請先提出一個正向電影推薦問題。",
        citations=(),
        movies=(),
    )
    assert repository.person_resolution_calls == []
    assert repository.recommendation_calls == []
    assert repository.deep_recommendation_calls == []
    assert repository.calls == []
    assert repository.poster_batch_calls == []
    assert repository.readiness_calls == []
    assert repository.state_calls == []
    assert embedder.calls == []
    assert generator.calls == []


@pytest.mark.parametrize(
    "question",
    ("recommend 3 more movies without repeats", "no repeats"),
)
def test_english_no_repeat_follow_up_without_history_stops_before_retrieval(
    question: str,
) -> None:
    """Breaks if English continuation wording invents cards to exclude."""
    service, repository, embedder, generator = _service([], "must not be used", ())

    result = service.answer(question)

    assert result.answer_markdown == (
        "找不到可延續的成功推薦；請先提出一個正向電影推薦問題。"
    )
    assert repository.person_resolution_calls == []
    assert repository.recommendation_calls == []
    assert repository.calls == []
    assert embedder.calls == []
    assert generator.calls == []


@pytest.mark.parametrize(
    "question",
    (
        "再給我三部，別重複",
        "給我三部，別重複",
        "再推薦三部新的，不要和前面一樣",
        "再推薦三部，唔好重複",
        "再推薦三部，勿重複",
        "recommend 3 more, none of the same",
        "recommend 3 new movies, not the same ones as before",
        "recommend 3 more, don't repeat",
    ),
)
def test_natural_no_repeat_shell_without_history_stops_before_retrieval(
    question: str,
) -> None:
    """Breaks if a generic no-repeat shell invents history or a fake person."""
    service, repository, embedder, generator = _service([], "must not be used", ())

    result = service.answer(question)

    assert result.answer_markdown == (
        "找不到可延續的成功推薦；請先提出一個正向電影推薦問題。"
    )
    assert repository.person_resolution_calls == []
    assert repository.recommendation_calls == []
    assert repository.calls == []
    assert embedder.calls == []
    assert generator.calls == []


@pytest.mark.parametrize(
    "question",
    (
        "再推薦三部，不要犯罪片。",
        "再推薦三部，不要恐怖片。",
        "再推薦三部，不要S級。",
        "再推薦三部，不要1980年代。",
        "recommend 3 movies, no horror",
        "recommend 3 movies without crime",
        "recommend 3 movies, not from the 1980s",
        "請不要推薦喜劇電影",
        "再推薦三部不要1980年代",
        "再推薦三部，別喜劇",
        "請不要推薦B級電影",
        "do not recommend horror movies",
        "recommend 3 movies without any horror",
        "推薦3部不要恐怖片",
        "推薦3部電影但不要S級",
        "推薦3部，別看恐怖片",
        "推薦3部，非恐怖片",
        "recommend 3 movies, don't recommend horror",
        "recommend 3 movies, no Hong Kong horror",
        "推薦三部，不推薦恐怖片",
        "推薦喜劇，但不含恐怖片",
        "推薦不是S級的電影",
        "推薦3部，唔好恐怖片",
        "recommend 3 movies, no more horror movies",
        "recommend 3 movies excluding all B-tier films",
        "推薦非恐怖片",
        "推薦非S級電影",
        "推薦3部，不想要恐怖片",
        "推薦3部，不包括S級電影",
        "推薦3部，避免1980年代電影",
        "推薦3部電影，恐怖片除外",
        "推薦3部電影，S級電影不要",
        "推薦3部電影，1980年代電影除外",
        "recommend 3 movies, avoid horror",
        "recommend 3 movies, anything but horror",
        "recommend 3 movies, except horror",
        "recommend 3 movies, other than horror",
        "recommend 3 movies, skip horror",
        "我不看恐怖片，推薦三部",
        "我不喜歡恐怖片，推薦三部",
        "推薦三部恐怖以外的電影",
        "除恐怖片外推薦三部",
        "推薦三部，唔要恐怖片",
        "推薦三部電影，不考慮恐怖片",
        "recommend 3 non-horror movies",
        "recommend 3 movies that aren't horror",
        "recommend 3 horror-free movies",
        "I dislike horror; recommend 3 movies",
        "I hate horror; recommend 3 movies",
        "我不太喜歡恐怖片，推薦三部",
        "我唔睇恐怖片，推薦三部",
        "recommend 3 movies, I detest horror",
        "推薦三部，唔想睇恐怖片",
        "推薦三部，冇恐怖片",
        "推薦三部沒有恐怖元素的電影",
        "recommend 3 movies that shouldn't be horror",
        "recommend 3 movies that cannot be horror",
        "recommend 3 movies free of horror",
        "recommend 3 comedies sans horror",
        "recommend 3 movies apart from horror",
        "recommend 3 movies barring horror",
        "recommend 3 movies with the exception of horror",
        "推薦3部喜劇，唔使恐怖片",
        "推薦3部喜劇，唔駛恐怖片",
        "推薦3部喜劇，毋須恐怖片",
        "推薦3部喜劇，不必恐怖片",
        "推薦3部喜劇，不用介紹恐怖片",
    ),
)
def test_unsupported_negative_filters_refuse_before_structured_or_vector_search(
    question: str,
) -> None:
    """Breaks if a negative filter is ignored or inverted into a positive query."""
    service, repository, embedder, generator = _service([], "must not be used", ())
    history = (
        ConversationExchange(
            "請推薦三部喜劇電影。",
            "上一輪回答不可以成為證據。",
            ("old-first", "old-second", "old-third"),
        ),
    )

    result = service.answer(question, history)

    assert result == ChatAnswer(
        answer_markdown=(
            "目前不支援「不要／排除」類型、級別或年份條件；"
            "請改為指定想看的正向條件。"
        ),
        citations=(),
        movies=(),
    )
    assert repository.person_resolution_calls == []
    assert repository.recommendation_calls == []
    assert repository.deep_recommendation_calls == []
    assert repository.calls == []
    assert repository.poster_batch_calls == []
    assert repository.readiness_calls == []
    assert repository.state_calls == []
    assert embedder.calls == []
    assert generator.calls == []


def test_exact_title_text_owns_embedded_english_negative_words() -> None:
    """Breaks if ``no crime`` inside a governed title becomes a negative filter."""
    question = "Stealing is no crime 好看嗎？"
    passage = _passage(
        "metadata:stealing-is-no-crime",
        "stealing-is-no-crime",
        chinese_title="法內情",
        english_title="Stealing is no crime",
        genre="劇情",
    )
    service, repository, _, _ = _service(
        [passage],
        "《法內情》是本次查詢的精確片名結果。[metadata:stealing-is-no-crime]",
        ("metadata:stealing-is-no-crime",),
    )

    result = service.answer(question)

    assert repository.explicit_target_calls == [(RELEASE_ID, question)]
    assert repository.calls[0]["target_movie_ids"] == ("stealing-is-no-crime",)
    assert [movie.movie_id for movie in result.movies] == ["stealing-is-no-crime"]
    assert result.answer_markdown != (
        "目前不支援「不要／排除」類型、級別或年份條件；"
        "請改為指定想看的正向條件。"
    )


def test_unquoted_exact_title_after_recommendation_prefix_owns_negation_words() -> None:
    """Breaks if a full governed title is reinterpreted as ``no + crime``."""
    question = "推薦Stealing is no crime"
    passage = _passage(
        "metadata:stealing-is-no-crime-recommend",
        "stealing-is-no-crime-recommend",
        chinese_title="法內情",
        english_title="Stealing is no crime",
        genre="劇情",
    )
    service, repository, _, _ = _service(
        [passage],
        "《法內情》是精確片名結果。[metadata:stealing-is-no-crime-recommend]",
        ("metadata:stealing-is-no-crime-recommend",),
    )

    result = service.answer(question)

    assert repository.explicit_target_calls == [(RELEASE_ID, question)]
    assert repository.calls[0]["target_movie_ids"] == (
        "stealing-is-no-crime-recommend",
    )
    assert [movie.movie_id for movie in result.movies] == [
        "stealing-is-no-crime-recommend"
    ]


def test_exact_title_residual_negative_filter_still_fails_closed() -> None:
    """Breaks if title ownership hides a real negative filter in residual text."""
    question = "比較《Stealing is no crime》，但不要恐怖片"
    passage = _passage(
        "metadata:stealing-is-no-crime",
        "stealing-is-no-crime",
        chinese_title="法內情",
        english_title="Stealing is no crime",
        genre="劇情",
    )
    service, repository, embedder, generator = _service(
        [passage], "must not be used", ()
    )

    result = service.answer(question)

    assert result.answer_markdown == (
        "目前不支援「不要／排除」類型、級別或年份條件；"
        "請改為指定想看的正向條件。"
    )
    assert repository.explicit_target_calls == [(RELEASE_ID, question)]
    assert repository.calls == []
    assert repository.recommendation_calls == []
    assert embedder.calls == []
    assert generator.calls == []


@pytest.mark.parametrize(
    "question",
    (
        "比較《醉拳》和周星馳主演的電影",
        "比較《醉拳》和周星馳、成龍主演的電影",
        "《醉拳》和周星馳既主演又導演的電影相比如何？",
    ),
)
def test_exact_title_plus_person_catalog_scope_fails_closed(question: str) -> None:
    """Breaks if exact-title ownership silently deletes a person catalog clause."""
    passage = _passage(
        "metadata:drunken-master-mixed-scope",
        "1978_ZQ_001",
        chinese_title="醉拳",
        english_title="Drunken Master",
    )
    service, repository, embedder, generator = _service(
        [passage], "must not be used", ()
    )

    result = service.answer(question)

    assert result == ChatAnswer(
        answer_markdown=(
            "目前不支援在同一問題中同時指定電影與人物片單；請分開提問。"
        ),
        citations=(),
        movies=(),
    )
    assert repository.person_resolution_calls == []
    assert repository.recommendation_calls == []
    assert repository.calls == []
    assert repository.readiness_calls == []
    assert embedder.calls == []
    assert generator.calls == []


def test_fact_negation_about_an_exact_title_is_not_a_recommendation_filter() -> None:
    """Breaks if a factual correction question is refused as a negative filter."""
    question = "醉拳好看嗎？不是喜劇嗎？"
    passage = _passage(
        "metadata:drunken-master-fact-negation",
        "drunken-master-fact-negation",
        chinese_title="醉拳",
        english_title="Drunken Master",
        genre="喜劇、動作",
    )
    service, repository, _, _ = _service(
        [passage],
        "《醉拳》的 Release 類型含喜劇。[metadata:drunken-master-fact-negation]",
        ("metadata:drunken-master-fact-negation",),
    )

    result = service.answer(question)

    assert result.answer_markdown != (
        "目前不支援「不要／排除」類型、級別或年份條件；"
        "請改為指定想看的正向條件。"
    )
    assert repository.calls[0]["target_movie_ids"] == (
        "drunken-master-fact-negation",
    )


@pytest.mark.parametrize(
    "question",
    (
        "推薦3部喜劇電影，不要劇透，不要解釋，只列片名",
        "推薦3部喜劇電影，不用多說",
        "推薦3部喜劇電影，別長篇大論",
        "推薦3部喜劇電影，不用介紹劇情",
        "推薦3部喜劇電影，不用介紹",
        "推薦3部喜劇電影，不用理由",
        "推薦3部喜劇電影，無需介紹",
        "推薦3部喜劇電影，唔使介紹",
        "recommend 3 comedies, no commentary",
        "recommend 3 comedies, do not elaborate",
    ),
)
def test_recommendation_output_instruction_does_not_block_structured_search(
    question: str,
) -> None:
    """Breaks if 'no spoilers/explanation' is treated as a movie exclusion."""
    service, repository, _, _ = _service([], "must not be used", ())

    result = service.answer(question)

    assert result.answer_markdown not in {
        "目前不支援「不要／排除」類型、級別或年份條件；請改為指定想看的正向條件。",
        "目前不支援「不要／排除」人物或其他負向推薦條件；請改為指定想看的正向人物與條件。",
    }
    assert len(repository.recommendation_calls) == 1
    assert repository.recommendation_calls[0].genres == ("喜劇",)


@pytest.mark.parametrize(
    "question",
    (
        "recommend 3 movies, no preference",
        "recommend 3 movies, no specific genre",
        "推薦3部電影，沒有特別偏好",
        "推薦3部電影，不限定類型",
    ),
)
def test_neutral_preference_language_reaches_structured_search(question: str) -> None:
    service, repository, _, _ = _service([], "must not be used", ())

    result = service.answer(question)

    assert "不要／排除" not in result.answer_markdown
    assert len(repository.recommendation_calls) == 1
    assert repository.calls == []


@pytest.mark.parametrize(
    "question",
    (
        "don't recommend Stephen Chow movies",
        "recommend movies without Stephen Chow",
    ),
)
def test_unmodeled_english_negative_person_stops_before_resolution(
    question: str,
) -> None:
    """Breaks if an English person exclusion reaches Release resolution/search."""
    service, repository, embedder, generator = _service([], "must not be used", ())

    result = service.answer(question)

    assert result == ChatAnswer(
        answer_markdown=(
            "目前不支援「不要／排除」人物或其他負向推薦條件；"
            "請改為指定想看的正向人物與條件。"
        ),
        citations=(),
        movies=(),
    )
    assert repository.person_resolution_calls == []
    assert repository.recommendation_calls == []
    assert repository.deep_recommendation_calls == []
    assert repository.calls == []
    assert repository.readiness_calls == []
    assert repository.state_calls == []
    assert embedder.calls == []
    assert generator.calls == []


@pytest.mark.parametrize(
    "question",
    (
        "推薦三部電影，但周星馳的不要",
        "推薦三部電影，唔想睇周星馳",
        "recommend 3 movies, anything but Stephen Chow",
        "recommend 3 movies, leave Stephen Chow out",
        "recommend 3 movies, except Stephen Chow",
        "recommend 3 films other than Jackie Chan",
        "我不喜歡周星馳，推薦三部電影",
        "推薦三部電影，不考慮周星馳",
        "recommend movies Stephen Chow isn't in",
        "I dislike Stephen Chow; recommend 3 movies",
        "avoid Stephen Chow movies",
        "no Stephen Chow movies",
        "I don't want Stephen Chow movies",
        "stay away from Stephen Chow movies",
        "steer away from Stephen Chow movies",
        "keep away from Stephen Chow movies",
        "I hate Stephen Chow movies",
        "I dislike Stephen Chow movies",
        "不要周星馳電影",
        "我唔鍾意周星馳電影",
        "推薦非周星馳作品",
        "推薦3部非周星馳電影",
        "想看非成龍電影",
    ),
)
def test_additional_negative_person_surfaces_stop_before_resolution(
    question: str,
) -> None:
    """Breaks if a negative person is resolved and returned as a positive card."""
    service, repository, embedder, generator = _service([], "must not be used", ())

    result = service.answer(question)

    assert result.answer_markdown == (
        "目前不支援「不要／排除」人物或其他負向推薦條件；"
        "請改為指定想看的正向人物與條件。"
    )
    assert repository.person_resolution_calls == []
    assert repository.recommendation_calls == []
    assert repository.calls == []
    assert embedder.calls == []
    assert generator.calls == []


def test_negative_looking_release_credit_reaches_exact_person_retrieval() -> None:
    """Breaks if the governed credit `不是女人` is mistaken for a negative filter."""
    question = "推薦不是女人的電影"
    service, repository, _, _ = _service([], "must not be used", ())
    repository.person_resolutions[question] = ("不是女人", None)

    result = service.answer(question)

    assert "不要／排除" not in result.answer_markdown
    assert repository.person_resolution_calls == [(RELEASE_ID, question)]
    assert len(repository.recommendation_calls) == 1
    assert repository.recommendation_calls[0].person_name == "不是女人"


def test_bare_negative_looking_release_credit_reaches_release_resolution() -> None:
    question = "不是女人電影"
    service, repository, _, _ = _service([], "must not be used", ())
    repository.person_resolutions[question] = ("不是女人", None)

    result = service.answer(question)

    assert "不要／排除" not in result.answer_markdown
    assert repository.person_resolution_calls == [(RELEASE_ID, question)]
    assert len(repository.recommendation_calls) == 1
    assert repository.recommendation_calls[0].person_name == "不是女人"


def test_actor_catalog_question_uses_exact_person_retrieval_not_vector() -> None:
    """Breaks if an actor catalog query becomes an unscoped semantic search."""
    question = "周星馳出演過哪些影片？"
    service, repository, _, _ = _service([], "must not be used", ())
    repository.person_resolutions[question] = ("周星馳", "actor")

    service.answer(question)

    assert repository.person_resolution_calls == [(RELEASE_ID, question)]
    assert len(repository.recommendation_calls) == 1
    assert repository.recommendation_calls[0].person_name == "周星馳"
    assert repository.recommendation_calls[0].person_role == "actor"
    assert repository.calls == []


@pytest.mark.parametrize(
    "question",
    (
        "推薦周星馳主演或導演的電影",
        "推薦周星馳主演或者導演的電影",
        "推薦周星馳主演和導演電影",
        "推薦周星馳主演、導演電影",
        "推薦周星馳演員兼導演電影",
        "推薦周星馳既主演又導演的電影",
        "推薦周星馳既是主演又是導演的電影",
    ),
)
def test_simultaneous_actor_and_director_roles_clarify_before_resolution(
    question: str,
) -> None:
    """Breaks if conflicting role constraints are weakened to actor-or-director."""
    service, repository, embedder, generator = _service([], "must not be used", ())

    result = service.answer(question)

    assert result == ChatAnswer(
        answer_markdown="目前不支援同時指定演員與導演角色；請只指定導演或演員後再試。",
        citations=(),
        movies=(),
    )
    assert repository.explicit_target_calls == [(RELEASE_ID, question)]
    assert repository.person_resolution_calls == []
    assert repository.recommendation_calls == []
    assert repository.deep_recommendation_calls == []
    assert repository.calls == []
    assert repository.poster_batch_calls == []
    assert repository.readiness_calls == []
    assert repository.state_calls == []
    assert embedder.calls == []
    assert generator.calls == []


def test_strong_ood_precedes_role_conflict_after_explicit_title_resolution() -> None:
    """Breaks if role grammar masks a deterministic non-movie domain rejection."""
    question = "推薦周星馳主演或導演的電影，最近哪隻股票值得買？"
    service, repository, embedder, generator = _service([], "must not be used", ())

    result = service.answer(question)

    assert result == ChatAnswer(
        answer_markdown="我目前只能回答这个 Release 内的香港电影问题。",
        citations=(),
        movies=(),
    )
    assert repository.explicit_target_calls == [(RELEASE_ID, question)]
    assert repository.person_resolution_calls == []
    assert repository.recommendation_calls == []
    assert repository.deep_recommendation_calls == []
    assert repository.calls == []
    assert repository.poster_batch_calls == []
    assert repository.readiness_calls == []
    assert repository.state_calls == []
    assert embedder.calls == []
    assert generator.calls == []


def test_multiple_people_precede_role_conflict_clarification() -> None:
    """Breaks if a secondary role conflict hides the exactly-one-person boundary."""
    question = "推薦袁和平和成龍主演或導演的電影"
    service, repository, embedder, generator = _service([], "must not be used", ())

    result = service.answer(question)

    assert result == ChatAnswer(
        answer_markdown="目前一次只支援一位正向人物；請只指定一位導演或演員後再試。",
        citations=(),
        movies=(),
    )
    assert repository.explicit_target_calls == [(RELEASE_ID, question)]
    assert repository.person_resolution_calls == []
    assert repository.recommendation_calls == []
    assert repository.deep_recommendation_calls == []
    assert repository.calls == []
    assert repository.poster_batch_calls == []
    assert repository.readiness_calls == []
    assert repository.state_calls == []
    assert embedder.calls == []
    assert generator.calls == []


def test_quoted_exact_title_owns_role_words_before_person_gates() -> None:
    """Breaks if role grammar captures words that belong to a quoted movie title."""
    question = "推薦《臨時演員》或導演的電影"
    passage = _passage(
        "metadata:temporary-actor-role-control",
        "temporary-actor-role-control",
        chinese_title="臨時演員",
        english_title="Temporary Actor",
        director="測試導演",
    )
    service, repository, _, _ = _service(
        [passage], "must not be used", ()
    )

    result = service.answer(question)

    assert repository.explicit_target_calls == [(RELEASE_ID, question)]
    assert repository.calls[0]["target_movie_ids"] == (
        "temporary-actor-role-control",
    )
    assert result.answer_markdown != (
        "目前不支援同時指定演員與導演角色；請只指定導演或演員後再試。"
    )
    assert [movie.movie_id for movie in result.movies] == [
        "temporary-actor-role-control"
    ]


def test_unresolved_bare_person_genres_fail_closed_before_generic_search() -> None:
    """Breaks if the parsed person shape is discarded when release identity is unknown."""
    question = "陳假的動作喜劇"
    service, repository, embedder, generator = _service([], "must not be used", ())

    result = service.answer(question)

    assert "人物" in result.answer_markdown
    assert "精確解析" in result.answer_markdown
    assert result.citations == ()
    assert result.movies == ()
    assert repository.person_resolution_calls == [(RELEASE_ID, question)]
    assert repository.recommendation_calls == []
    assert repository.calls == []
    assert embedder.calls == []
    assert generator.calls == []


def test_unknown_stage_name_actor_shape_fails_closed_before_generic_search() -> None:
    """Breaks if a current parsed actor shape depends on a surname heuristic."""
    question = "阿飛主演的動作電影"
    service, repository, embedder, generator = _service([], "must not be used", ())

    result = service.answer(question)

    assert "演員" in result.answer_markdown
    assert "精確解析" in result.answer_markdown
    assert result.citations == ()
    assert result.movies == ()
    assert repository.person_resolution_calls == [(RELEASE_ID, question)]
    assert repository.recommendation_calls == []
    assert repository.calls == []
    assert embedder.calls == []
    assert generator.calls == []


def test_explicit_role_person_shape_overrides_candidate_blacklist() -> None:
    """Breaks if explicit actor syntax is discarded because the candidate looks topical."""
    question = "方言主演的動作電影"
    service, repository, embedder, generator = _service([], "must not be used", ())

    result = service.answer(question)

    assert "演員" in result.answer_markdown
    assert "精確解析" in result.answer_markdown
    assert result.citations == ()
    assert result.movies == ()
    assert repository.person_resolution_calls == [(RELEASE_ID, question)]
    assert repository.recommendation_calls == []
    assert repository.calls == []
    assert embedder.calls == []
    assert generator.calls == []


@pytest.mark.parametrize("question", ("周星馳的歌曲", "王家衛的書"))
def test_bare_person_nonmovie_object_keeps_domain_rejection(question: str) -> None:
    """Breaks if person parsing overrides the stronger non-movie object gate."""
    service, repository, embedder, generator = _service([], "must not be used", ())

    result = service.answer(question)

    assert result.answer_markdown == "我目前只能回答这个 Release 内的香港电影问题。"
    assert result.citations == ()
    assert result.movies == ()
    assert repository.person_resolution_calls == []
    assert repository.recommendation_calls == []
    assert repository.calls == []
    assert embedder.calls == []
    assert generator.calls == []


def test_conjunctive_genre_post_validation_rejects_a_partial_candidate() -> None:
    """Breaks if a two-genre AND plan accepts a row carrying only one genre token."""
    question = "周星馳的動作喜劇"
    partial = _passage(
        "metadata:partial-conjunction",
        "partial-conjunction",
        chinese_title="只有喜劇",
        english_title="Comedy Only",
        genre="喜劇",
        cast="周星馳、吳孟達",
    )
    service, repository, _, generator = _service([partial], "must not be used", ())
    repository.person_resolutions[question] = ("周星馳", None)

    with pytest.raises(GroundingError, match="typed constraints"):
        service.answer(question)

    assert repository.calls == []
    assert generator.calls == []


def test_identity_before_role_returns_constrained_zero_match_for_known_person() -> None:
    """Breaks if a known person with no requested-role rows is reported as unknown."""
    question = "周星馳導演的作品"
    service, repository, _, generator = _service([], "must not be used", ())
    repository.person_resolutions[question] = ("周星馳", "director")

    result = service.answer(question)

    assert "沒有符合目前條件的結果" in result.answer_markdown
    assert "導演周星馳" in result.answer_markdown
    assert "精確解析" not in result.answer_markdown
    assert result.citations == ()
    assert result.movies == ()
    assert repository.person_resolution_calls == [(RELEASE_ID, question)]
    assert len(repository.recommendation_calls) == 1
    assert repository.recommendation_calls[0].person_role == "director"
    assert repository.calls == []
    assert generator.calls == []


def test_person_continuation_inherits_the_newest_resolvable_history_turn() -> None:
    """Breaks if continuation loses its person or scans unrelated answer text."""
    current = "還有別的嗎？"
    previous = "推薦1部周星馳主演的電影"
    passage = _passage(
        "metadata:next-stephen-chow",
        "movie-next-stephen-chow",
        chinese_title="下一部周星馳電影",
        english_title="Another Stephen Chow Film",
        cast="周星馳、吳孟達",
    )
    service, repository, _, _ = _service(
        [passage],
        "《下一部周星馳電影》：符合延續的人物條件。[metadata:next-stephen-chow]",
        ("metadata:next-stephen-chow",),
    )
    repository.person_resolutions[previous] = ("周星馳", "actor")
    history = (ConversationExchange(previous, "上一輪。", ("old-movie",)),)

    service.answer(current, history)

    assert repository.person_resolution_calls == [
        (RELEASE_ID, current),
        (RELEASE_ID, previous),
    ]
    plan = repository.recommendation_calls[0]
    assert (plan.person_name, plan.person_role) == ("周星馳", "actor")
    assert plan.excluded_movie_ids == ("old-movie",)


def test_continuation_inherits_person_from_a_natural_successful_recommendation() -> None:
    """Breaks if history inheritance depends on the narrow possessive qualifier regex."""
    current = "還有別的嗎？"
    previous = "有周星馳的電影推薦嗎？"
    passage = _passage(
        "metadata:next-natural",
        "movie-next-natural",
        chinese_title="下一部自然問法電影",
        english_title="Another Natural Query Film",
        cast="周星馳、吳孟達",
    )
    service, repository, _, _ = _service([passage], "must not be used", ())
    repository.person_resolutions[previous] = ("周星馳", None)
    history = (ConversationExchange(previous, "上一輪。", ("old-natural",)),)

    service.answer(current, history)

    assert repository.person_resolution_calls == [
        (RELEASE_ID, current),
        (RELEASE_ID, previous),
    ]
    plan = repository.recommendation_calls[0]
    assert (plan.person_name, plan.person_role) == ("周星馳", None)


def test_continuation_resolves_each_successful_chain_turn_before_heuristic_fallback() -> None:
    """Breaks if history inheritance is limited to the fail-closed grammar."""
    current = "還有別的嗎？"
    previous = "想看周星馳電影，推薦幾部"
    passage = _passage(
        "metadata:next-canonical-only",
        "movie-next-canonical-only",
        chinese_title="另一部周星馳電影",
        english_title="Another Canonical Person Film",
        cast="周星馳、吳孟達",
    )
    service, repository, _, _ = _service(
        [passage],
        "《另一部周星馳電影》：符合延續的人物條件。[metadata:next-canonical-only]",
        ("metadata:next-canonical-only",),
    )
    repository.person_resolutions[previous] = ("周星馳", None)
    history = (ConversationExchange(previous, "上一輪。", ("old-canonical",)),)

    service.answer(current, history)

    assert repository.person_resolution_calls == [
        (RELEASE_ID, current),
        (RELEASE_ID, previous),
    ]
    plan = repository.recommendation_calls[0]
    assert (plan.person_name, plan.person_role) == ("周星馳", None)


def test_non_recommendation_role_question_does_not_break_person_continuation() -> None:
    """Breaks if an intervening metadata question becomes the recommendation history boundary."""
    current = "還有別的嗎？"
    recommendation = "推薦1部周星馳主演的電影"
    metadata_question = "這部電影的導演是誰？"
    passage = _passage(
        "metadata:after-metadata",
        "movie-after-metadata",
        chinese_title="後續推薦",
        english_title="Recommendation After Metadata",
        cast="周星馳、吳孟達",
    )
    service, repository, _, _ = _service(
        [passage],
        "《後續推薦》：符合延續的人物條件。[metadata:after-metadata]",
        ("metadata:after-metadata",),
    )
    repository.person_resolutions[recommendation] = ("周星馳", "actor")
    history = (
        ConversationExchange(recommendation, "第一輪。", ("old-recommendation",)),
        ConversationExchange(metadata_question, "導演資料。", ("old-recommendation",)),
    )

    result = service.answer(current, history)

    assert [movie.movie_id for movie in result.movies] == ["movie-after-metadata"]
    assert (RELEASE_ID, metadata_question) not in repository.person_resolution_calls
    plan = repository.recommendation_calls[0]
    assert (plan.person_name, plan.person_role) == ("周星馳", "actor")


def test_later_successful_generic_recommendation_resets_an_older_person_chain() -> None:
    """Breaks if continuation resurrects a person from before a standalone generic reset."""
    current = "還有別的嗎？"
    old_person_question = "推薦1部周星馳主演的電影"
    generic_reset = "推薦1部喜劇電影"
    passage = _passage(
        "metadata:generic-reset",
        "movie-generic-reset",
        chinese_title="通用喜劇",
        english_title="Generic Comedy",
        genre="喜劇",
        cast="其他演員",
    )
    service, repository, _, _ = _service(
        [passage],
        "《通用喜劇》：符合延續的喜劇條件。[metadata:generic-reset]",
        ("metadata:generic-reset",),
    )
    repository.person_resolutions[old_person_question] = ("周星馳", "actor")
    history = (
        ConversationExchange(old_person_question, "第一輪。", ("old-person",)),
        ConversationExchange(generic_reset, "第二輪。", ("old-generic",)),
    )

    result = service.answer(current, history)

    assert [movie.movie_id for movie in result.movies] == ["movie-generic-reset"]
    assert (RELEASE_ID, old_person_question) not in repository.person_resolution_calls
    plan = repository.recommendation_calls[0]
    assert (plan.person_name, plan.person_role) == (None, None)


def test_failed_zero_card_person_turn_cannot_contaminate_older_active_chain() -> None:
    """Breaks if failed filters merge with the identity from an older successful turn."""
    current = "還有別的嗎？"
    wong = "推薦1部1980年代S級王家衛導演的愛情電影"
    failed_yamada = "山田太郎的殭屍電影"
    service, repository, _, _ = _service([], "must not be used", ())
    repository.person_resolutions[wong] = ("王家衛", "director")
    history = (
        ConversationExchange(wong, "第一輪。", ("old-wong",)),
        ConversationExchange(failed_yamada, "沒有結果。", ()),
    )

    service.answer(current, history)

    assert repository.person_resolution_calls == [
        (RELEASE_ID, current),
        (RELEASE_ID, wong),
    ]
    plan = repository.recommendation_calls[0]
    assert (plan.person_name, plan.person_role) == ("王家衛", "director")
    assert plan.genres == ("愛情",)
    assert plan.tier == "S"
    assert (plan.year_from, plan.year_to) == (1980, 1989)
    assert plan.excluded_movie_ids == ("old-wong",)
    assert failed_yamada not in plan.context_text


def test_new_successful_person_reset_replaces_old_identity_exclusions_and_context() -> None:
    """Breaks if a successful reset leaves any prior recommendation chain state active."""
    current = "還有別的嗎？"
    old_wong = "推薦1部1980年代S級王家衛導演的愛情電影"
    new_yamada = "山田太郎的殭屍電影"
    service, repository, _, _ = _service([], "must not be used", ())
    repository.person_resolutions[old_wong] = ("王家衛", "director")
    repository.person_resolutions[new_yamada] = ("山田太郎", None)
    history = (
        ConversationExchange(old_wong, "第一輪。", ("old-wong",)),
        ConversationExchange(new_yamada, "第二輪。", ("new-yamada",)),
    )

    service.answer(current, history)

    assert repository.person_resolution_calls == [
        (RELEASE_ID, current),
        (RELEASE_ID, new_yamada),
    ]
    plan = repository.recommendation_calls[0]
    assert (plan.person_name, plan.person_role) == ("山田太郎", None)
    assert plan.genres == ("恐怖", "喜劇")
    assert plan.tier is None
    assert (plan.year_from, plan.year_to) == (None, None)
    assert plan.excluded_movie_ids == ("new-yamada",)
    assert plan.context_text == f"{new_yamada}\n{current}"


def test_successful_person_switch_bounds_followup_identity_exclusions_and_context() -> None:
    """Breaks if a follow-up replays the pre-switch person as active context."""
    current = "還有別的嗎？"
    old_chow = "推薦1部1980年代S級周星馳主演的喜劇電影"
    switch_jackie = "換成1部成龍主演的電影"
    service, repository, _, _ = _service([], "must not be used", ())
    repository.person_resolutions[old_chow] = ("周星馳", "actor")
    repository.person_resolutions[switch_jackie] = ("成龍", "actor")
    history = (
        ConversationExchange(old_chow, "第一輪。", ("old-chow",)),
        ConversationExchange(switch_jackie, "第二輪。", ("old-jackie",)),
    )

    service.answer(current, history)

    assert repository.person_resolution_calls == [
        (RELEASE_ID, current),
        (RELEASE_ID, switch_jackie),
    ]
    plan = repository.recommendation_calls[0]
    assert (plan.person_name, plan.person_role) == ("成龍", "actor")
    assert plan.requested_count == 1
    assert plan.genres == ()
    assert plan.tier is None
    assert (plan.year_from, plan.year_to) == (None, None)
    assert plan.excluded_movie_ids == ("old-jackie",)
    assert plan.context_text == f"{switch_jackie}\n{current}"


def test_zero_card_person_switch_keeps_the_last_successful_person_active() -> None:
    """Breaks if a failed person switch replaces the successful identity boundary."""
    current = "還有別的嗎？"
    old_chow = "推薦1部1980年代S級周星馳主演的喜劇電影"
    failed_switch = "換成1部山田太郎主演的電影"
    service, repository, _, _ = _service([], "must not be used", ())
    repository.person_resolutions[old_chow] = ("周星馳", "actor")
    repository.person_resolutions[failed_switch] = ("山田太郎", "actor")
    history = (
        ConversationExchange(old_chow, "第一輪。", ("old-chow",)),
        ConversationExchange(failed_switch, "沒有結果。", ()),
    )

    service.answer(current, history)

    assert repository.person_resolution_calls == [
        (RELEASE_ID, current),
        (RELEASE_ID, old_chow),
    ]
    plan = repository.recommendation_calls[0]
    assert (plan.person_name, plan.person_role) == ("周星馳", "actor")
    assert plan.genres == ("喜劇",)
    assert plan.tier == "S"
    assert (plan.year_from, plan.year_to) == (1980, 1989)
    assert plan.excluded_movie_ids == ("old-chow",)
    assert plan.context_text == f"{old_chow}\n{current}"


def test_generic_history_shape_false_positive_does_not_become_a_person() -> None:
    """Breaks if a quality phrase parsed from history becomes an unresolved identity."""
    current = "還有別的嗎？"
    previous = "推薦高評分的好電影"
    passages = [
        _passage(
            f"metadata:generic-more-{index}",
            f"generic-more-{index}",
            chinese_title=f"通用後續電影{index}",
            english_title=f"Generic Follow-up {index}",
        )
        for index in range(5)
    ]
    citation_ids = tuple(str(item["passage_id"]) for item in passages)
    answer = "\n".join(
        f"《通用後續電影{index}》：符合後續條件。[metadata:generic-more-{index}]"
        for index in range(5)
    )
    service, repository, _, _ = _service(passages, answer, citation_ids)
    history = (ConversationExchange(previous, "上一輪。", ("old-generic",)),)

    result = service.answer(current, history)

    assert len(result.movies) == 5
    assert "精確解析" not in result.answer_markdown
    assert repository.calls == []
    assert repository.person_resolution_calls == [
        (RELEASE_ID, current),
        (RELEASE_ID, previous),
    ]
    plan = repository.recommendation_calls[0]
    assert (plan.person_name, plan.person_role) == (None, None)
    assert plan.excluded_movie_ids == ("old-generic",)


def test_current_actor_switch_wins_without_resolving_an_older_person() -> None:
    """Breaks if a continuation actor switch is overwritten by prior-person inheritance."""
    current = "換成1部成龍主演的電影"
    previous = "推薦1部周星馳主演的電影"
    passage = _passage(
        "metadata:jackie-chan",
        "movie-jackie-chan",
        chinese_title="成龍電影",
        english_title="Jackie Chan Film",
        cast="成龍、元彪",
    )
    service, repository, _, _ = _service(
        [passage],
        "《成龍電影》：符合当前人物條件。[metadata:jackie-chan]",
        ("metadata:jackie-chan",),
    )
    repository.person_resolutions[current] = ("成龍", "actor")
    repository.person_resolutions[previous] = ("周星馳", "actor")
    history = (ConversationExchange(previous, "上一輪。", ("old-movie",)),)

    service.answer(current, history)

    assert repository.person_resolution_calls == [(RELEASE_ID, current)]
    plan = repository.recommendation_calls[0]
    assert (plan.person_name, plan.person_role) == ("成龍", "actor")


def test_continuation_inherits_the_latest_person_switch_inside_the_active_chain() -> None:
    """Breaks if history inheritance consults only the chain's standalone reset."""
    current = "還有別的嗎？"
    reset_question = "推薦1部周星馳主演的電影"
    switch_question = "換成1部成龍主演的電影"
    passage = _passage(
        "metadata:after-jackie-switch",
        "movie-after-jackie-switch",
        chinese_title="另一部成龍電影",
        english_title="Another Jackie Chan Film",
        cast="成龍、元彪",
    )
    service, repository, _, _ = _service(
        [passage],
        "《另一部成龍電影》：符合延續的人物條件。[metadata:after-jackie-switch]",
        ("metadata:after-jackie-switch",),
    )
    repository.person_resolutions[reset_question] = ("周星馳", "actor")
    repository.person_resolutions[switch_question] = ("成龍", "actor")
    history = (
        ConversationExchange(reset_question, "第一輪。", ("old-stephen",)),
        ConversationExchange(switch_question, "第二輪。", ("old-jackie",)),
    )

    service.answer(current, history)

    assert repository.person_resolution_calls == [
        (RELEASE_ID, current),
        (RELEASE_ID, switch_question),
    ]
    plan = repository.recommendation_calls[0]
    assert (plan.person_name, plan.person_role) == ("成龍", "actor")


@pytest.mark.parametrize(
    "history",
    (
        (),
        (
            ConversationExchange(
                "推薦1部周星馳主演的電影",
                "上一輪。",
                ("old-chow",),
            ),
        ),
    ),
)
def test_current_person_continuation_unknown_fails_before_history_or_downstream_calls(
    history: tuple[ConversationExchange, ...],
) -> None:
    """Breaks if an unresolved current person embeds or resolves a historical person."""
    question = "山田太郎的殭屍電影還有嗎"
    service, repository, embedder, generator = _service([], "must not be used", ())
    repository.person_resolutions["推薦1部周星馳主演的電影"] = (
        "周星馳",
        "actor",
    )

    result = service.answer(question, history)

    assert result == ChatAnswer(
        answer_markdown=(
            "無法在目前 Release 精確解析指定的人物；"
            "為避免錯配，不會以向量搜尋替代。"
        ),
        citations=(),
        movies=(),
    )
    assert repository.person_resolution_calls == [(RELEASE_ID, question)]
    assert repository.recommendation_calls == []
    assert repository.deep_recommendation_calls == []
    assert repository.calls == []
    assert repository.poster_batch_calls == []
    assert embedder.calls == []
    assert generator.calls == []


def test_known_current_person_reset_discards_historical_filters_and_exclusions() -> None:
    """Breaks if a complete current person inherits historical tier, years, or exclusions."""
    current = "成龍的動作電影還有嗎"
    previous = "推薦1部1980年代S級王家衛導演的電影"
    passages = [
        _passage(
            f"metadata:current-jackie-{index}",
            f"movie-current-jackie-{index}",
            chinese_title=f"當代成龍動作片{index}",
            english_title=f"Current Jackie Chan Action {index}",
            release_date="1995-01-01",
            genre="動作",
            tier="A",
            director="其他導演",
            cast="成龍、其他演員",
        )
        for index in range(5)
    ]
    citation_ids = tuple(str(passage["passage_id"]) for passage in passages)
    service, repository, _, _ = _service(
        passages,
        "\n".join(
            f"《當代成龍動作片{index}》：符合目前人物條件。"
            f"[metadata:current-jackie-{index}]"
            for index in range(5)
        ),
        citation_ids,
    )
    repository.person_resolutions[current] = ("成龍", "actor")
    repository.person_resolutions[previous] = ("王家衛", "director")
    history = (ConversationExchange(previous, "上一輪。", ("old-wong",)),)

    result = service.answer(current, history)

    assert repository.person_resolution_calls == [(RELEASE_ID, current)]
    assert repository.calls == []
    assert len(repository.recommendation_calls) == 1
    plan = repository.recommendation_calls[0]
    assert (plan.person_name, plan.person_role) == ("成龍", "actor")
    assert plan.genres == ("動作",)
    assert plan.tier is None
    assert (plan.year_from, plan.year_to) == (None, None)
    assert plan.excluded_movie_ids == ()
    expected_movie_ids = [f"movie-current-jackie-{index}" for index in range(5)]
    assert [citation.movie_id for citation in result.citations] == expected_movie_ids
    assert [movie.movie_id for movie in result.movies] == expected_movie_ids


@pytest.mark.parametrize(
    ("question", "expected_role"),
    (
        ("推薦演員陳假的電影", "演員"),
        ("推薦陳假的好電影", "人物"),
        ("推薦陳大假的好電影", "人物"),
    ),
)
def test_unresolved_explicit_person_fails_closed_before_embedding_or_vector_fallback(
    question: str,
    expected_role: str,
) -> None:
    """Breaks if an unresolved explicit person silently degrades to semantic retrieval."""
    service, repository, embedder, generator = _service([], "must not be used", ())

    result = service.answer(question)

    assert expected_role in result.answer_markdown
    assert "精確解析" in result.answer_markdown
    assert result.citations == ()
    assert result.movies == ()
    assert repository.person_resolution_calls == [(RELEASE_ID, question)]
    assert repository.recommendation_calls == []
    assert repository.calls == []
    assert embedder.calls == []
    assert generator.calls == []


@pytest.mark.parametrize(
    "question",
    ("山田太郎的殭屍電影", "山田太郎的賭片"),
)
def test_unknown_person_in_controlled_title_scope_fails_closed_before_embedding(
    question: str,
) -> None:
    """Breaks if a controlled title policy discards a positive person shape."""
    service, repository, embedder, generator = _service([], "must not be used", ())

    result = service.answer(question)

    assert "人物" in result.answer_markdown
    assert "精確解析" in result.answer_markdown
    assert result.citations == ()
    assert result.movies == ()
    assert repository.person_resolution_calls == [(RELEASE_ID, question)]
    assert repository.recommendation_calls == []
    assert repository.deep_recommendation_calls == []
    assert repository.calls == []
    assert repository.poster_batch_calls == []
    assert embedder.calls == []
    assert generator.calls == []


def test_person_with_controlled_credit_group_is_rejected_before_embedding() -> None:
    """Breaks if incompatible person and governed-group predicates reach retrieval."""
    question = "山田太郎的女性導演電影"
    service, repository, embedder, generator = _service([], "must not be used", ())
    repository.person_resolutions[question] = ("山田太郎", None)

    result = service.answer(question)

    assert "不支援" in result.answer_markdown
    assert "人物" in result.answer_markdown
    assert result.citations == ()
    assert result.movies == ()
    assert repository.person_resolution_calls == []
    assert repository.recommendation_calls == []
    assert repository.deep_recommendation_calls == []
    assert repository.calls == []
    assert repository.poster_batch_calls == []
    assert embedder.calls == []
    assert generator.calls == []


@pytest.mark.parametrize(
    "question",
    (
        "推薦演員陣容好的電影",
        "推薦導演作品好的電影",
        "推薦高評分的好電影",
        "推薦奧斯卡的好電影",
        "推薦九十年代的好電影",
        "推薦有內涵的好電影",
        "推薦古裝的好電影",
        "推薦田園的好電影",
        "推薦方言的好電影",
        "推薦白話的好電影",
        "推薦演員演技好的電影",
        "推薦導演功力好的電影",
        "推薦主演表現好的電影",
    ),
)
def test_non_person_recommendation_phrases_do_not_trigger_person_fail_closed(
    question: str,
) -> None:
    """Breaks if a role word or topical possessive is treated as a named person."""
    service, repository, embedder, generator = _service([], "must not be used", ())

    result = service.answer(question)

    assert "精確解析" not in result.answer_markdown
    assert "沒有符合目前條件的結果" in result.answer_markdown
    assert len(repository.recommendation_calls) == 1
    assert len(embedder.calls) == 1
    assert generator.calls == []


@pytest.mark.parametrize("question", ("推薦香港的好電影", "推薦新加坡的好電影"))
def test_region_possessive_recommendation_is_not_misread_as_an_unresolved_person(
    question: str,
) -> None:
    """Breaks if a common region qualifier is blocked by the person fallback heuristic."""
    service, repository, embedder, generator = _service([], "must not be used", ())

    result = service.answer(question)

    assert "精確解析" not in result.answer_markdown
    assert "沒有符合目前條件的結果" in result.answer_markdown
    assert len(repository.recommendation_calls) == 1
    assert len(embedder.calls) == 1
    assert generator.calls == []


def test_resolved_person_with_no_matches_names_the_person_and_role_without_fallback() -> None:
    """Breaks if an empty person-qualified result hides which hard predicate was applied."""
    question = "推薦3部周星馳主演的電影"
    service, repository, _, generator = _service([], "must not be used", ())
    repository.person_resolutions[question] = ("周星馳", "actor")

    result = service.answer(question)

    assert "周星馳" in result.answer_markdown
    assert "演員" in result.answer_markdown
    assert result.movies == ()
    assert repository.calls == []
    assert generator.calls == []


def test_comedy_recommendation_selects_requested_count_and_requires_all_citations() -> None:
    passages = [
        _passage(
            "metadata:comedy-s",
            "comedy-s",
            chinese_title="喜劇甲",
            english_title="Comedy S",
            genre="喜劇",
            tier="S",
            tier_reason="最高資料庫層級",
            pilot_movie=True,
        ),
        _passage(
            "metadata:comedy-a",
            "comedy-a",
            chinese_title="喜劇乙",
            english_title="Comedy A",
            genre="喜劇",
            tier="A",
        ),
        _passage(
            "metadata:comedy-b",
            "comedy-b",
            chinese_title="喜劇丙",
            english_title="Comedy B",
            genre="喜劇",
            tier="B",
        ),
        _passage(
            "metadata:comedy-extra",
            "comedy-extra",
            chinese_title="喜劇丁",
            english_title="Comedy Extra",
            genre="喜劇",
            tier="B",
        ),
    ]
    service, repository, _, generator = _service(
        passages,
        (
            "《喜劇甲》：屬喜劇及 S 級。[metadata:comedy-s]\n"
            "《喜劇乙》：屬喜劇及 A 級。[metadata:comedy-a]\n"
            "《喜劇丙》：屬喜劇及 B 級。[metadata:comedy-b]"
        ),
        ("metadata:comedy-s", "metadata:comedy-a", "metadata:comedy-b"),
    )

    result = service.answer("有沒有好的喜劇推薦？請推薦3部")

    assert [movie.movie_id for movie in result.movies] == [
        "comedy-s",
        "comedy-a",
        "comedy-b",
    ]
    assert [(movie.tier, movie.pilot_movie) for movie in result.movies] == [
        ("S", True),
        ("A", False),
        ("B", False),
    ]
    assert [citation.citation_id for citation in result.citations] == [
        "metadata:comedy-s",
        "metadata:comedy-a",
        "metadata:comedy-b",
    ]
    assert "不能提供沒有深度文檔" not in result.answer_markdown
    assert repository.recommendation_calls[0].genres == ("喜劇",)
    assert repository.recommendation_calls[0].scope_label == "metadata"
    assert generator.calls[0][2:] == (
        "recommendation",
        (),
        ("metadata:comedy-s", "metadata:comedy-a", "metadata:comedy-b"),
    )


@pytest.mark.parametrize(
    (
        "question",
        "chinese_title",
        "director",
        "cast",
        "genre",
        "expected_reason",
    ),
    [
        (
            "推薦1部關於兄弟情的香港電影",
            "兄弟新篇",
            "測試導演",
            "測試演員",
            "劇情 / 動作",
            "只因片名命中受控關鍵詞而入選；這不證明其劇情或主題包含相關元素。",
        ),
        (
            "推薦1部女性導演的香港電影",
            "城市故事",
            "許鞍華",
            "測試演員",
            "劇情",
            "導演欄位命中 Release 受控女性導演名單；這只基於欄位中的精確姓名匹配。",
        ),
        (
            "想找一部由女演員擔綱主角的劇情片。",
            "歲月故事",
            "測試導演",
            "張曼玉、測試演員",
            "劇情",
            (
                "演員欄位命中 Release 受控女性演員名單；"
                "這只證明卡司欄位的精確姓名匹配，不代表她擔任主角。"
            ),
        ),
        (
            "推薦1部適合全家觀看的港片",
            "歡樂一家",
            "測試導演",
            "測試演員",
            "家庭 / 喜劇",
            (
                "Release 類型欄位含「家庭」；"
                "這不是年齡分級，也不證明適合所有兒童。"
            ),
        ),
    ],
    ids=[
        "title-keyword-proxy",
        "controlled-female-directors",
        "controlled-female-actors",
        "family-genre-proxy",
    ],
)
def test_scoped_recommendations_use_server_evidence_without_vertex(
    question: str,
    chinese_title: str,
    director: str,
    cast: str,
    genre: str,
    expected_reason: str,
) -> None:
    passage = _passage(
        "metadata:scoped",
        "scoped",
        chinese_title=chinese_title,
        english_title="Scoped Film",
        director=director,
        cast=cast,
        genre=genre,
    )
    passage["body"] = "不可用自由文本：口碑極佳、主題深刻、節奏明快、適合兒童。"
    service, repository, _, generator = _service([passage], "must not be used", ())

    result = service.answer(question)

    assert result.answer_markdown == (
        f"《{chinese_title}》：{expected_reason}[metadata:scoped]"
    )
    assert [citation.citation_id for citation in result.citations] == [
        "metadata:scoped"
    ]
    assert [movie.movie_id for movie in result.movies] == ["scoped"]
    assert generator.calls == []
    assert repository.calls == []
    assert repository.recommendation_calls[0].scope_label != "metadata"
    assert "口碑極佳" not in result.answer_markdown
    assert "節奏明快" not in result.answer_markdown


@pytest.mark.parametrize(
    ("question", "chinese_title", "director", "cast", "genre"),
    [
        (
            "推薦1部關於兄弟情的香港電影",
            "無關片名",
            "測試導演",
            "測試演員",
            "劇情",
        ),
        (
            "推薦1部女性導演的香港電影",
            "城市故事",
            "不在名單的導演",
            "測試演員",
            "劇情",
        ),
        (
            "想找一部由女演員擔綱主角的劇情片。",
            "歲月故事",
            "測試導演",
            "不在名單的演員",
            "劇情",
        ),
        (
            "推薦1部適合全家觀看的港片",
            "歡樂一家",
            "測試導演",
            "測試演員",
            "喜劇",
        ),
    ],
    ids=[
        "title-term-mismatch",
        "director-group-mismatch",
        "actor-group-mismatch",
        "family-genre-mismatch",
    ],
)
def test_scoped_recommendations_fail_closed_on_mismatched_evidence(
    question: str,
    chinese_title: str,
    director: str,
    cast: str,
    genre: str,
) -> None:
    passage = _passage(
        "metadata:mismatch",
        "mismatch",
        chinese_title=chinese_title,
        english_title="Mismatch Film",
        director=director,
        cast=cast,
        genre=genre,
    )
    service, _, _, generator = _service([passage], "must not be used", ())

    with pytest.raises(GroundingError, match="scoped recommendation"):
        service.answer(question)

    assert generator.calls == []


@pytest.mark.parametrize("mutation", ["unknown-scope", "unknown-term", "unknown-group"])
def test_deterministic_scoped_answer_rejects_unknown_plan_authority(
    mutation: str,
) -> None:
    passage = _evidence_from_record(
        _passage(
            "metadata:authority",
            "authority",
            chinese_title="兄弟篇",
            english_title="Brothers",
            genre="劇情",
        )
    )
    plan = RecommendationPlan(
        requested_count=1,
        genres=("劇情",),
        tier=None,
        year_from=None,
        year_to=None,
        diversify_decades=False,
        continuation=False,
        excluded_movie_ids=(),
        context_text="受控測試",
        title_terms_any=("兄弟",),
        scope_label="title_keyword_proxy",
    )
    if mutation == "unknown-scope":
        plan = replace(plan, scope_label="unknown")  # type: ignore[arg-type]
    elif mutation == "unknown-term":
        plan = replace(plan, title_terms_any=("未受控詞",))
    else:
        plan = replace(
            plan,
            title_terms_any=(),
            credit_group_id="missing_group",
            credit_role="director",
            credit_exact_names=("測試導演",),
            scope_label="controlled_credit_group",
        )

    with pytest.raises(GroundingError, match="scoped recommendation"):
        _deterministic_scoped_recommendation(
            plan,
            (passage,),
            ("metadata:authority",),
        )


def test_metadata_scope_cannot_hide_scoped_plan_authority(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    malformed_plan = RecommendationPlan(
        requested_count=1,
        genres=("劇情",),
        tier=None,
        year_from=None,
        year_to=None,
        diversify_decades=False,
        continuation=False,
        excluded_movie_ids=(),
        context_text="推薦一部電影",
        genres_any=("劇情", "動作", "犯罪"),
        title_terms_any=("兄弟", "手足"),
        scope_label="metadata",
    )
    monkeypatch.setattr(
        "hk_movie_rag.rag_query.plan_recommendation",
        lambda question, history: malformed_plan,
    )
    passage = _passage(
        "metadata:hidden-scope",
        "hidden-scope",
        chinese_title="兄弟篇",
        english_title="Brothers",
        genre="劇情",
    )
    service, _, _, generator = _service(
        [passage],
        "《兄弟篇》：模型理由。[metadata:hidden-scope]",
        ("metadata:hidden-scope",),
    )

    with pytest.raises(GroundingError, match="recommendation scope"):
        service.answer("推薦一部電影")

    assert generator.calls == []


@pytest.mark.parametrize(
    ("plan_changes", "passage_changes"),
    [
        (
            {"genres_all": ("喜劇",), "genres_any": ("動作", "功夫")},
            {"genre": "喜劇"},
        ),
        ({"tier": "S"}, {"tier": "A"}),
        ({"year_from": 1980, "year_to": 1989}, {"release_date": "1990-01-01"}),
        ({"year_from": 1980, "year_to": 1989}, {"release_date": "1985"}),
        ({"excluded_movie_ids": ("candidate",)}, {}),
        (
            {
                "title_terms_any": ("賭", "千王", "雀聖", "麻雀", "撲克"),
                "scope_label": "title_keyword_proxy",
            },
            {"chinese_title": "無關片名"},
        ),
        (
            {
                "credit_group_id": "female_directors_v1",
                "credit_role": "director",
                "credit_exact_names": (
                    "許鞍華",
                    "張婉婷",
                    "张婉婷",
                    "羅卓瑤",
                    "麥曦茵",
                    "麦曦茵",
                    "黃真真",
                    "岸西",
                ),
                "scope_label": "controlled_credit_group",
            },
            {"director": "王晶"},
        ),
    ],
    ids=(
        "genre-any",
        "tier",
        "year",
        "year-format",
        "excluded",
        "title-proxy",
        "credit-group",
    ),
)
def test_recommendation_postfilter_rejects_records_outside_typed_plan(
    plan_changes: dict[str, object], passage_changes: dict[str, object]
) -> None:
    plan = replace(
        RecommendationPlan(
            1, (), None, None, None, False, False, (), "typed recommendation"
        ),
        **plan_changes,
    )
    passage_kwargs: dict[str, object] = {
        "chinese_title": "受控候選",
        "english_title": "Controlled Candidate",
        "genre": "喜劇 / 動作",
        "tier": "S",
        "release_date": "1988-01-01",
        "director": "許鞍華",
        "cast": "測試演員",
    }
    passage_kwargs.update(passage_changes)
    record = _passage(
        "metadata:candidate",
        "candidate",
        **passage_kwargs,  # type: ignore[arg-type]
    )

    with pytest.raises(GroundingError, match="typed constraints"):
        _selected_recommendation_evidence(
            RecommendationSearch((record,), 1),
            plan,
        )


def test_title_proxy_keeps_explicit_genre_as_an_independent_constraint() -> None:
    plan = replace(
        RecommendationPlan(
            1, ("喜劇",), None, None, None, False, False, (), "推薦喜劇兄弟情電影"
        ),
        genres_any=("劇情", "動作", "犯罪"),
        title_terms_any=("兄弟", "手足"),
        scope_label="title_keyword_proxy",
    )
    drama_only = _passage(
        "metadata:brothers-drama",
        "brothers-drama",
        chinese_title="兄弟風雲",
        english_title="Brothers",
        genre="劇情",
    )

    with pytest.raises(GroundingError, match="typed constraints"):
        _selected_recommendation_evidence(
            RecommendationSearch((drama_only,), 1),
            plan,
        )


def test_title_proxy_uses_the_same_chinese_or_english_title_authority_when_rendering() -> None:
    plan = replace(
        RecommendationPlan(
            1, (), None, None, None, False, False, (), "有什麼關於兄弟情的香港電影"
        ),
        genres_any=("劇情", "動作", "犯罪"),
        title_terms_any=("兄弟", "手足"),
        scope_label="title_keyword_proxy",
    )
    passage = _evidence_from_record(
        _passage(
            "metadata:english-brothers",
            "english-brothers",
            chinese_title="情義風雲",
            english_title="兄弟 Brothers Forever",
            genre="劇情",
        )
    )

    generated = _deterministic_scoped_recommendation(
        plan,
        (passage,),
        ("metadata:english-brothers",),
    )

    assert "只因片名命中受控關鍵詞" in generated.answer_markdown
    assert generated.citation_ids == ("metadata:english-brothers",)


def test_deep_recommendation_postfilter_rejects_off_plan_pdf_record() -> None:
    plan = RecommendationPlan(
        1, ("喜劇",), None, None, None, False, False, (), "推薦喜劇深度分析"
    )
    action_pdf = _passage(
        "pdf:off-plan:p1",
        "off-plan",
        chinese_title="動作片",
        english_title="Action Film",
        genre="動作",
        passage_kind="pdf",
        page_number=1,
        source_filename="action.pdf",
    )

    with pytest.raises(GroundingError, match="outside typed constraints"):
        _selected_deep_recommendation_evidence(
            RecommendationSearch((action_pdf,), 1),
            plan,
        )


def test_scoped_recommendation_continuation_preserves_scope_without_vertex() -> None:
    passage = _passage(
        "metadata:new-zombie",
        "new-zombie",
        chinese_title="殭屍新篇",
        english_title="New Zombie",
        genre="恐怖 / 喜劇",
    )
    passage["body"] = "不可用自由文本：這是一部節奏明快的佳作。"
    service, repository, _, generator = _service([passage], "must not be used", ())
    history = (
        ConversationExchange(
            "推薦1部香港殭屍電影",
            "上一輪回答。",
            ("old-zombie",),
        ),
    )

    result = service.answer("還有別的嗎？", history)

    assert "只因片名命中受控關鍵詞" in result.answer_markdown
    assert "不證明其劇情或主題" in result.answer_markdown
    assert generator.calls == []
    plan = repository.recommendation_calls[0]
    assert plan.scope_label == "title_keyword_proxy"
    assert plan.title_terms_any == ("殭屍", "僵屍")
    assert plan.excluded_movie_ids == ("old-zombie",)


def test_representative_1980s_police_crime_query_uses_structured_metadata() -> None:
    """Breaks if a bounded list query falls into generic/deep metadata refusal."""
    passages = [
        _passage(
            f"metadata:police-{index}",
            f"police-{index}",
            chinese_title=f"八十年代警匪片{index}",
            english_title=f"Police Crime Film {index}",
            release_date=f"198{index}-01-01",
            genre="犯罪 / 動作",
        )
        for index in range(1, 6)
    ]
    citation_ids = tuple(str(passage["passage_id"]) for passage in passages)
    answer_markdown = "\n".join(
        f"《八十年代警匪片{index}》：八十年代犯罪動作片。[metadata:police-{index}]"
        for index in range(1, 6)
    )
    service, repository, _, generator = _service(
        passages,
        answer_markdown,
        citation_ids,
    )

    result = service.answer("八十年代警匪片有哪些代表？")

    assert [movie.movie_id for movie in result.movies] == [
        f"police-{index}" for index in range(1, 6)
    ]
    assert repository.calls == []
    assert repository.deep_recommendation_calls == []
    assert len(repository.recommendation_calls) == 1
    plan = repository.recommendation_calls[0]
    assert plan.genres == ("犯罪",)
    assert (plan.year_from, plan.year_to) == (1980, 1989)
    assert generator.calls[0][2] == "recommendation"


def test_s_tier_follow_up_keeps_tier_and_excludes_prior_ids() -> None:
    passage = _passage(
        "metadata:s-two",
        "s-two",
        chinese_title="第二部S級片",
        english_title="Second S Film",
        tier="S",
    )
    service, repository, embedder, generator = _service(
        [passage],
        "《第二部S級片》：符合 S 級條件。[metadata:s-two]",
        ("metadata:s-two",),
    )
    history = (ConversationExchange("推薦一部S級電影", "上一輪。", ("s-one",)),)

    service.answer("還有別的嗎？", history)

    plan = repository.recommendation_calls[0]
    assert plan.tier == "S"
    assert plan.excluded_movie_ids == ("s-one",)
    assert embedder.calls == [
        "task: search result | query: 推薦一部S級電影\n還有別的嗎？"
    ]
    assert generator.calls[0][3] == ("推薦一部S級電影",)


def test_conversation_history_is_validated_and_only_latest_four_questions_are_context() -> None:
    passage = _passage(
        "metadata:ia",
        "ia",
        chinese_title="無間道",
        english_title="Infernal Affairs",
    )
    service, repository, embedder, generator = _service(
        [passage], "導演有記錄。[metadata:ia]", ("metadata:ia",)
    )
    history = tuple(
        ConversationExchange(f"問題{index}", f"秘密答案{index}", ())
        for index in range(5)
    )

    service.answer("無間道的導演是誰？", history)

    assert "問題0" not in embedder.calls[0]
    assert "秘密答案" not in embedder.calls[0]
    assert generator.calls == []
    assert repository.recommendation_calls == []

    with pytest.raises(QueryValidationError, match="history"):
        service.answer("問題", (object(),))  # type: ignore[arg-type]
    with pytest.raises(QueryValidationError, match="history"):
        service.answer(
            "問題", (ConversationExchange("問" * 12001, "答", ()),)
        )


@pytest.mark.parametrize(
    ("question", "expected_movie_id"),
    [
        ("第一部的導演是誰？", "movie-1"),
        ("第二部的导演是谁？", "movie-2"),
        ("Who directed the first one?", "movie-1"),
        ("Who directed the second one?", "movie-2"),
        ("Who directed the third one?", "movie-3"),
        ("Who directed the fourth one?", "movie-4"),
        ("Who directed the fifth one?", "movie-5"),
        ("Who directed the sixth one?", "movie-6"),
        ("Who directed the seventh one?", "movie-7"),
        ("Who directed the eighth one?", "movie-8"),
    ],
)
def test_latest_exchange_ordinals_select_one_fresh_database_target(
    question: str, expected_movie_id: str
) -> None:
    """Breaks if an ordinal is embedded as prose instead of selecting the prior ordered ID."""
    passages = [
        _passage(
            f"metadata:{index}",
            f"movie-{index}",
            chinese_title=f"測試片{index}",
            english_title=f"Test Film {index}",
        )
        for index in range(1, 9)
    ]
    citation_id = expected_movie_id.replace("movie-", "metadata:")
    service, repository, _, generator = _service(
        passages,
        f"導演有新鮮資料。[{citation_id}]",
        (citation_id,),
    )
    history = (
        ConversationExchange(
            "推薦八部電影",
            "上一輪模型文字不是證據。",
            tuple(f"movie-{index}" for index in range(1, 9)),
        ),
    )

    result = service.answer(question, history)

    assert repository.calls[0]["target_movie_ids"] == (expected_movie_id,)
    assert generator.calls == []
    assert [item.movie_id for item in result.movies] == [expected_movie_id]


def test_singular_pronoun_selects_the_only_latest_movie_and_ignores_answer_prose() -> None:
    """Breaks if pronoun resolution consumes prior answer claims or retrieves another movie."""
    target = _passage(
        "metadata:target",
        "target",
        chinese_title="目標片",
        english_title="Target Film",
    )
    unrelated = _passage(
        "metadata:unrelated",
        "unrelated",
        chinese_title="其他片",
        english_title="Other Film",
        distance=0.001,
    )
    service, repository, embedder, generator = _service(
        [unrelated, target],
        "目標片導演有新鮮資料。[metadata:target]",
        ("metadata:target",),
    )
    history = (
        ConversationExchange(
            "上一輪只選一部",
            "SECRET CLAIM: 其他片導演是錯誤答案。",
            ("target",),
        ),
    )

    result = service.answer("它的導演是誰？", history)

    assert repository.calls[0]["target_movie_ids"] == ("target",)
    assert generator.calls == []
    assert "SECRET CLAIM" not in embedder.calls[0]
    assert [item.movie_id for item in result.movies] == ["target"]


@pytest.mark.parametrize(
    "question",
    (
        "Could you tell me about it?",
        "Please tell me who directed it.",
    ),
)
def test_common_title_free_bare_pronouns_fall_back_to_latest_movie(
    question: str,
) -> None:
    target = _passage(
        "metadata:target",
        "target",
        chinese_title="目標片",
        english_title="Target Film",
    )
    service, repository, _, _ = _service(
        [target],
        "目標片有新鮮資料。[metadata:target]",
        ("metadata:target",),
    )
    history = (
        ConversationExchange("上一輪只選一部", "上一輪。", ("target",)),
    )

    result = service.answer(question, history)

    assert repository.explicit_target_calls == [(RELEASE_ID, question)]
    assert repository.calls[0]["target_movie_ids"] == ("target",)
    assert [movie.movie_id for movie in result.movies] == ["target"]


def test_no_card_exchange_does_not_erase_the_latest_successful_movie_referent() -> None:
    """Breaks if an intervening clarification with no cards clears the last usable referent."""
    target = _passage(
        "metadata:target",
        "target",
        chinese_title="目標片",
        english_title="Target Film",
    )
    service, repository, _, generator = _service(
        [target],
        "目標片導演有新鮮資料。[metadata:target]",
        ("metadata:target",),
    )
    history = (
        ConversationExchange("先選一部", "有卡片。", ("target",)),
        ConversationExchange("請再說清楚", "請問你想知道甚麼？", ()),
    )

    result = service.answer("它的導演是誰？", history)

    assert repository.calls[0]["target_movie_ids"] == ("target",)
    assert generator.calls == []
    assert [item.movie_id for item in result.movies] == ["target"]


@pytest.mark.parametrize(
    "history",
    [
        (),
        (ConversationExchange("上一輪選了別片", "上一輪。", ("unrelated",)),),
    ],
)
def test_delimited_explicit_title_suppresses_generic_pronoun_resolution(
    history: tuple[ConversationExchange, ...],
) -> None:
    """Breaks if 'this movie' hijacks an explicitly delimited title into history targeting."""
    drunken = _passage(
        "metadata:drunken",
        "drunken",
        chinese_title="醉拳",
        english_title="Drunken Master",
    )
    unrelated = _passage(
        "metadata:unrelated",
        "unrelated",
        chinese_title="其他片",
        english_title="Other Film",
        distance=0.001,
    )
    service, repository, _, generator = _service(
        [unrelated, drunken],
        "《醉拳》導演有新鮮資料。[metadata:drunken]",
        ("metadata:drunken",),
    )

    result = service.answer("《醉拳》这部电影的导演是谁？", history)

    assert repository.calls[0]["target_movie_ids"] == ("drunken",)
    assert generator.calls == []
    assert [item.movie_id for item in result.movies] == ["drunken"]


@pytest.mark.parametrize(
    "question",
    (
        "这部叫醉拳的电影，导演是谁？",
        "醉拳这部电影的导演是谁？",
        "醉拳，它的导演是谁？",
        "Who directed Drunken Master, this movie?",
        "Drunken Master, what is its director?",
        "What about this movie, Drunken Master?",
    ),
)
def test_undelimited_explicit_title_wins_over_history_demonstrative(
    question: str,
) -> None:
    drunken = _passage(
        "metadata:drunken",
        "drunken",
        chinese_title="醉拳",
        english_title="Drunken Master",
    )
    unrelated = _passage(
        "metadata:unrelated",
        "unrelated",
        chinese_title="其他片",
        english_title="Other Film",
        distance=0.001,
    )
    service, repository, _, generator = _service(
        [unrelated, drunken],
        "《醉拳》導演有新鮮資料。[metadata:drunken]",
        ("metadata:drunken",),
    )
    history = (
        ConversationExchange("上一輪選了別片", "上一輪。", ("unrelated",)),
    )

    result = service.answer(question, history)

    assert repository.explicit_target_calls == [(RELEASE_ID, question)]
    assert repository.calls[0]["target_movie_ids"] == ("drunken",)
    assert generator.calls == []
    assert [item.movie_id for item in result.movies] == ["drunken"]


def test_current_explicit_title_resolves_before_history_fallback_for_rating() -> None:
    infernal = _passage(
        "metadata:infernal",
        "infernal",
        chinese_title="無間道",
        english_title="Infernal Affairs",
    )
    infernal["body"] = f"{infernal['body']}\nrating: 8.0"
    unrelated = _passage(
        "metadata:unrelated",
        "unrelated",
        chinese_title="其他片",
        english_title="Other Film",
        distance=0.001,
    )
    service, repository, _, generator = _service(
        [unrelated, infernal],
        "《無間道》評分有新鮮資料。[metadata:infernal]",
        ("metadata:infernal",),
    )
    history = (
        ConversationExchange("上一輪選了別片", "上一輪。", ("unrelated",)),
    )

    result = service.answer("What is its rating, Infernal Affairs?", history)

    assert repository.explicit_target_calls == [
        (RELEASE_ID, "What is its rating, Infernal Affairs?")
    ]
    assert repository.calls[0]["target_movie_ids"] == ("infernal",)
    assert generator.calls == []
    assert [item.movie_id for item in result.movies] == ["infernal"]


def test_ambiguous_multi_movie_pronoun_returns_uncited_clarification_without_remote_calls() -> None:
    """Breaks if a singular pronoun arbitrarily selects one movie from a multi-card turn."""
    service, repository, embedder, generator = _service(
        [_passage("metadata:a", "a", chinese_title="甲", english_title="A")],
        "must not be used",
        (),
    )
    history = (
        ConversationExchange("推薦兩部電影", "上一輪。", ("a", "b")),
    )

    result = service.answer("它的導演是誰？", history)

    assert "第幾部" in result.answer_markdown
    assert result.citations == ()
    assert result.movies == ()
    assert repository.readiness_calls == []
    assert repository.calls == []
    assert repository.recommendation_calls == []
    assert embedder.calls == []
    assert generator.calls == []


def test_deep_pronoun_with_metadata_only_target_refuses_without_unrelated_pdf() -> None:
    """Breaks if a resolved deep question borrows a different movie's PDF analysis."""
    target = _passage(
        "metadata:target",
        "target",
        chinese_title="目標片",
        english_title="Target Film",
    )
    unrelated_pdf = _passage(
        "pdf:unrelated:p1",
        "unrelated",
        chinese_title="其他片",
        english_title="Other Film",
        passage_kind="pdf",
        page_number=1,
        source_filename="other.pdf",
        distance=0.001,
    )
    service, repository, _, generator = _service(
        [unrelated_pdf, target],
        "不得借用其他片。[pdf:unrelated:p1]",
        ("pdf:unrelated:p1",),
    )
    history = (
        ConversationExchange("上一輪只選一部", "上一輪。", ("target",)),
    )

    result = service.answer("它的攝影風格如何？", history)

    assert repository.calls[0]["target_movie_ids"] == ("target",)
    assert "目前只有結構化電影資料" in result.answer_markdown
    assert [item.citation_id for item in result.citations] == ["metadata:target"]
    assert [item.movie_id for item in result.movies] == ["target"]
    assert generator.calls == []


def test_typed_history_keeps_that_its_visual_or_sound_on_latest_movie_id() -> None:
    target_metadata, target_pdf = _flying_mr_boo_evidence()
    unrelated_pdf = _passage(
        "pdf:unrelated:p1",
        "unrelated",
        chinese_title="其他片",
        english_title="Other Film",
        passage_kind="pdf",
        page_number=1,
        source_filename="other.pdf",
        distance=0.001,
    )
    service, repository, _, generator = _service(
        [unrelated_pdf, target_metadata, target_pdf],
        "擠迫構圖與喧鬧聲場形成喜劇張力。[pdf:fgbr:p3]",
        ("pdf:fgbr:p3",),
    )
    history = (
        ConversationExchange(
            "上一輪選片",
            "模型答案提到其他片和未受控事實。",
            ("1987_FGBR_001",),
        ),
    )

    result = service.answer("那它的視覺或聲音如何？", history)

    assert repository.calls[0]["target_movie_ids"] == ("1987_FGBR_001",)
    assert [item.movie_id for item in generator.calls[0][1]] == ["1987_FGBR_001"]
    assert [item.citation_id for item in result.citations] == ["pdf:fgbr:p3"]
    assert "模型答案" not in generator.calls[0][3]


def test_generic_deep_recommendation_uses_only_matching_genre_pdf_evidence() -> None:
    """Breaks if generic deep recommendations can cite metadata or another genre's PDF."""
    comedy_metadata = _passage(
        "metadata:comedy",
        "comedy",
        chinese_title="喜劇片",
        english_title="Comedy Film",
        genre="喜劇",
    )
    comedy_pdfs = [
        _passage(
            f"pdf:comedy-{index}:p1",
            f"comedy-{index}",
            chinese_title=f"喜劇片{index}",
            english_title=f"Comedy Film {index}",
            genre="喜劇",
            passage_kind="pdf",
            page_number=1,
            source_filename=f"comedy-{index}.pdf",
        )
        for index in range(1, 4)
    ]
    action_pdf = _passage(
        "pdf:action:p1",
        "action",
        chinese_title="動作片",
        english_title="Action Film",
        genre="動作",
        passage_kind="pdf",
        page_number=1,
        source_filename="action.pdf",
        distance=0.001,
    )
    service, repository, _, generator = _service(
        [action_pdf, comedy_metadata, *comedy_pdfs],
        (
            "《喜劇片1》：有匹配的深度文檔。[pdf:comedy-1:p1]\n"
            "《喜劇片2》：有匹配的深度文檔。[pdf:comedy-2:p1]\n"
            "《喜劇片3》：有匹配的深度文檔。[pdf:comedy-3:p1]"
        ),
        ("pdf:comedy-1:p1", "pdf:comedy-2:p1", "pdf:comedy-3:p1"),
    )

    result = service.answer("推荐3部摄影风格鲜明的喜剧")

    assert repository.recommendation_calls == []
    assert repository.calls == []
    assert repository.deep_recommendation_calls == [
        RecommendationPlan(3, ("喜劇",), None, None, None, False, False, (), "推荐3部摄影风格鲜明的喜剧")
    ]
    assert [item.passage_id for item in generator.calls[0][1]] == [
        "pdf:comedy-1:p1",
        "pdf:comedy-2:p1",
        "pdf:comedy-3:p1",
    ]
    assert generator.calls[0][2] == "deep_recommendation"
    assert generator.calls[0][4] == (
        "pdf:comedy-1:p1",
        "pdf:comedy-2:p1",
        "pdf:comedy-3:p1",
    )
    assert [item.citation_id for item in result.citations] == [
        "pdf:comedy-1:p1",
        "pdf:comedy-2:p1",
        "pdf:comedy-3:p1",
    ]


def test_generic_deep_recommendation_with_too_few_unique_pdf_movies_is_evidence_limited() -> None:
    """Breaks if two deep documents can be stretched into three generated recommendations."""
    matching_pdfs = [
        _passage(
            f"pdf:comedy-{index}:p1",
            f"comedy-{index}",
            chinese_title=f"喜劇片{index}",
            english_title=f"Comedy Film {index}",
            genre="喜劇",
            passage_kind="pdf",
            page_number=1,
            source_filename=f"comedy-{index}.pdf",
        )
        for index in range(1, 3)
    ]
    duplicate_page = _passage(
        "pdf:comedy-1:p2",
        "comedy-1",
        chinese_title="喜劇片1",
        english_title="Comedy Film 1",
        genre="喜劇",
        passage_kind="pdf",
        page_number=2,
        source_filename="comedy-1.pdf",
    )
    service, _, _, generator = _service(
        [*matching_pdfs, duplicate_page],
        "must not be used",
        (),
    )

    result = service.answer("推荐3部摄影风格鲜明的喜剧")

    assert "2 部" in result.answer_markdown
    assert "3 部" in result.answer_markdown
    assert [item.citation_id for item in result.citations] == [
        "pdf:comedy-1:p1",
        "pdf:comedy-2:p1",
    ]
    assert [item.movie_id for item in result.movies] == ["comedy-1", "comedy-2"]
    assert generator.calls == []


def test_generic_deep_recommendation_without_matching_pdf_fails_closed() -> None:
    """Breaks if a structured genre row or unrelated PDF substitutes for deep evidence."""
    comedy_metadata = _passage(
        "metadata:comedy",
        "comedy",
        chinese_title="喜劇片",
        english_title="Comedy Film",
        genre="喜劇",
    )
    action_pdf = _passage(
        "pdf:action:p1",
        "action",
        chinese_title="動作片",
        english_title="Action Film",
        genre="動作",
        passage_kind="pdf",
        page_number=1,
        source_filename="action.pdf",
    )
    service, repository, _, generator = _service(
        [action_pdf, comedy_metadata],
        "must not be used",
        (),
    )

    result = service.answer("推荐3部摄影风格鲜明的喜剧")

    assert repository.recommendation_calls == []
    assert len(repository.deep_recommendation_calls) == 1
    assert repository.calls == []
    assert "深度文檔證據" in result.answer_markdown
    assert result.citations == ()
    assert result.movies == ()
    assert generator.calls == []


def test_deep_recommendation_uses_pdf_sql_boundary_before_generic_top_eight() -> None:
    """Breaks if metadata/unrelated crowding can hide eligible PDF movies before LIMIT."""
    crowded = [
        _passage(
            f"metadata:crowd-{index}",
            f"crowd-{index}",
            chinese_title=f"擁擠資料{index}",
            english_title=f"Crowding {index}",
            genre="喜劇",
            distance=0.001 + index / 1000,
        )
        for index in range(9)
    ]
    eligible = [
        _passage(
            f"pdf:eligible-{index}:p1",
            f"eligible-{index}",
            chinese_title=f"深度喜劇{index}",
            english_title=f"Deep Comedy {index}",
            genre="喜劇",
            passage_kind="pdf",
            page_number=1,
            source_filename=f"eligible-{index}.pdf",
            distance=0.5 + index / 100,
        )
        for index in range(1, 3)
    ]
    service, repository, _, generator = _service(
        [*crowded, *eligible],
        "《深度喜劇1》：有匹配的深度文檔。[pdf:eligible-1:p1]\n"
        "《深度喜劇2》：有匹配的深度文檔。[pdf:eligible-2:p1]",
        ("pdf:eligible-1:p1", "pdf:eligible-2:p1"),
    )

    result = service.answer("推荐2部摄影风格鲜明的喜剧")

    assert repository.calls == []
    assert len(repository.deep_recommendation_calls) == 1
    assert [passage.passage_id for passage in generator.calls[0][1]] == [
        "pdf:eligible-1:p1",
        "pdf:eligible-2:p1",
    ]
    assert [citation.citation_id for citation in result.citations] == [
        "pdf:eligible-1:p1",
        "pdf:eligible-2:p1",
    ]


def test_unfiltered_deep_recommendation_uses_pdf_boundary_and_default_count() -> None:
    passages = [
        _passage(
            f"pdf:deep-{index}:p1",
            f"deep-{index}",
            chinese_title=f"深度片{index}",
            english_title=f"Deep Film {index}",
            passage_kind="pdf",
            page_number=1,
            source_filename=f"deep-{index}.pdf",
        )
        for index in range(1, 6)
    ]
    citation_ids = tuple(str(passage["passage_id"]) for passage in passages)
    service, repository, _, generator = _service(
        passages,
        "\n".join(
            f"《深度片{index}》：有匹配的深度文檔。[pdf:deep-{index}:p1]"
            for index in range(1, 6)
        ),
        citation_ids,
    )

    service.answer("推荐摄影出色的电影")

    assert repository.calls == []
    assert repository.deep_recommendation_calls[0].requested_count == 5
    assert repository.deep_recommendation_calls[0].genres == ()
    assert generator.calls[0][2] == "deep_recommendation"


def test_deep_recommendation_preserves_all_structured_plan_constraints() -> None:
    eligible = [
        _passage(
            f"pdf:eligible-{index}:p1",
            f"eligible-{index}",
            chinese_title=f"八十年代深度片{index}",
            english_title=f"Eighties Deep {index}",
            genre="喜劇",
            tier="S",
            release_date=f"198{index}-01-01",
            passage_kind="pdf",
            page_number=1,
            source_filename=f"eligible-{index}.pdf",
        )
        for index in range(1, 3)
    ]
    ineligible = [
        _passage(
            "pdf:wrong-tier:p1",
            "wrong-tier",
            chinese_title="錯誤級別",
            english_title="Wrong Tier",
            genre="喜劇",
            tier="A",
            release_date="1984-01-01",
            passage_kind="pdf",
            page_number=1,
            source_filename="wrong-tier.pdf",
        ),
        _passage(
            "pdf:wrong-year:p1",
            "wrong-year",
            chinese_title="錯誤年份",
            english_title="Wrong Year",
            genre="喜劇",
            tier="S",
            release_date="1994-01-01",
            passage_kind="pdf",
            page_number=1,
            source_filename="wrong-year.pdf",
        ),
    ]
    service, repository, _, _ = _service(
        [*ineligible, *eligible],
        "《八十年代深度片1》：符合受控條件。[pdf:eligible-1:p1]\n"
        "《八十年代深度片2》：符合受控條件。[pdf:eligible-2:p1]",
        ("pdf:eligible-1:p1", "pdf:eligible-2:p1"),
    )

    service.answer("推荐2部1980年代S级摄影出色的喜剧")

    assert repository.deep_recommendation_calls == [
        RecommendationPlan(
            2,
            ("喜劇",),
            "S",
            1980,
            1989,
            False,
            False,
            (),
            "推荐2部1980年代S级摄影出色的喜剧",
        )
    ]


def test_deep_recommendation_preserves_typed_all_any_genre_constraints() -> None:
    passage = _passage(
        "pdf:martial-comedy:p1",
        "martial-comedy",
        chinese_title="武打喜劇",
        english_title="Martial Comedy",
        genre="喜劇 / 動作",
        passage_kind="pdf",
        page_number=1,
        source_filename="martial-comedy.pdf",
    )
    service, repository, _, _ = _service(
        [passage],
        "《武打喜劇》：有匹配的深度文檔。[pdf:martial-comedy:p1]",
        ("pdf:martial-comedy:p1",),
    )

    service.answer("推荐1部摄影出色的武打喜剧")

    plan = repository.deep_recommendation_calls[0]
    assert plan.genres_all == ("喜劇",)
    assert plan.genres_any == ("動作", "功夫", "武俠")
    assert plan.scope_label == "metadata"


@pytest.mark.parametrize(
    "question",
    (
        "推薦成龍的動作美學電影",
        "推薦成龍的動作美學電影還有嗎",
    ),
)
def test_complete_current_person_resets_deep_recommendation_history(
    question: str,
) -> None:
    """Breaks if deep planning uses lexical continuation or unconditional history text."""
    old_wong = "推薦2部1980年代S級王家衛導演的愛情電影"
    service, repository, embedder, generator = _service([], "must not be used", ())
    repository.person_resolutions[question] = ("成龍", None)
    repository.person_resolutions[old_wong] = ("王家衛", "director")
    history = (ConversationExchange(old_wong, "上一輪。", ("old-wong",)),)

    service.answer(question, history)

    assert repository.person_resolution_calls == [(RELEASE_ID, question)]
    assert repository.recommendation_calls == []
    assert len(repository.deep_recommendation_calls) == 1
    plan = repository.deep_recommendation_calls[0]
    assert (plan.person_name, plan.person_role) == ("成龍", None)
    assert plan.requested_count == 5
    assert plan.genres == ("動作",)
    assert plan.tier is None
    assert (plan.year_from, plan.year_to) == (None, None)
    assert plan.continuation is False
    assert plan.excluded_movie_ids == ()
    assert plan.context_text == question
    assert embedder.calls == [f"task: search result | query: {question}"]
    assert generator.calls == []


def test_repository_resolved_deep_person_switch_resets_without_dedup() -> None:
    """Breaks if deep recommendation keeps an older person's filters on switch."""
    previous = "推薦2部1980年代S級周星馳主演的喜劇電影"
    current = "再推薦2部成龍主演的攝影出色電影"
    history = (
        ConversationExchange(previous, "上一輪。", ("old-a", "old-b")),
    )
    service, repository, _, _ = _service([], "must not be used", ())
    repository.person_resolutions[previous] = ("周星馳", "actor")
    repository.person_resolutions[current] = ("成龍", "actor")

    service.answer(current, history)

    assert repository.recommendation_calls == []
    plan = repository.deep_recommendation_calls[0]
    assert (plan.person_name, plan.person_role) == ("成龍", "actor")
    assert plan.requested_count == 2
    assert plan.continuation is False
    assert plan.genres == ()
    assert plan.tier is None
    assert (plan.year_from, plan.year_to) == (None, None)
    assert plan.excluded_movie_ids == ()
    assert plan.context_text == current


def test_deep_release_year_recommendation_remains_replayable_history() -> None:
    """Breaks if ``上映`` discards a successful deep recommendation turn."""
    previous = "推薦2部攝影出色、1970年代上映的電影"
    current = "再推薦2部攝影出色的電影，不要重複"
    history = (
        ConversationExchange(previous, "上一輪。", ("old-a", "old-b")),
    )
    service, repository, _, _ = _service([], "must not be used", ())

    service.answer(current, history)

    assert repository.recommendation_calls == []
    plan = repository.deep_recommendation_calls[0]
    assert plan.requested_count == 2
    assert (plan.year_from, plan.year_to) == (1970, 1979)
    assert plan.continuation is True
    assert plan.excluded_movie_ids == ("old-a", "old-b")
    assert plan.context_text == f"{previous}\n{current}"


def test_deep_same_release_person_dedup_inherits_authoritative_history() -> None:
    """Breaks if deep and metadata recommendation paths disagree on identity."""
    previous = "推薦2部1980年代S級周星馳主演的喜劇電影"
    current = "推薦周星馳主演的攝影出色電影，不要重複"
    service, repository, _, _ = _service([], "must not be used", ())
    exact_names = ("周星馳", "周星驰")
    repository.person_resolutions[current] = ("周星馳", "actor", exact_names)
    repository.person_resolutions[previous] = ("周星驰", "actor", exact_names)
    history = (
        ConversationExchange(previous, "上一輪。", ("old-deep-a", "old-deep-b")),
    )

    service.answer(current, history)

    assert repository.person_resolution_calls == [
        (RELEASE_ID, current),
        (RELEASE_ID, previous),
    ]
    assert repository.recommendation_calls == []
    plan = repository.deep_recommendation_calls[0]
    assert (plan.person_name, plan.person_role) == ("周星馳", "actor")
    assert plan.requested_count == 2
    assert plan.genres == ("喜劇",)
    assert plan.tier == "S"
    assert (plan.year_from, plan.year_to) == (1980, 1989)
    assert plan.continuation is True
    assert plan.excluded_movie_ids == ("old-deep-a", "old-deep-b")
    assert plan.context_text == f"{previous}\n{current}"


def test_deep_same_person_dedup_authority_survives_a_third_turn_replay() -> None:
    """Breaks if deep recommendation history loses a proven identity continuation."""
    first = "推薦2部1980年代S級周星馳主演的攝影出色喜劇電影"
    second = "推薦周星馳主演的攝影出色電影，不要重複"
    current = "再推薦2部攝影出色電影，不要重複"
    history = (
        ConversationExchange(first, "第一輪。", ("old-deep-a", "old-deep-b")),
        ConversationExchange(second, "第二輪。", ("old-deep-c", "old-deep-d")),
    )
    passages = [
        _passage(
            f"pdf:persistent-deep-{index}:p1",
            f"persistent-deep-{index}",
            chinese_title=f"深度延續片{index}",
            english_title=f"Persistent Deep {index}",
            release_date=f"198{index}-01-01",
            genre="喜劇",
            tier="S",
            cast="周星馳、測試演員",
            passage_kind="pdf",
            page_number=1,
            source_filename=f"persistent-deep-{index}.pdf",
        )
        for index in range(1, 3)
    ]
    service, repository, _, _ = _service(
        passages,
        "\n".join(
            f"《深度延續片{index}》：深度續問。[pdf:persistent-deep-{index}:p1]"
            for index in range(1, 3)
        ),
        tuple(f"pdf:persistent-deep-{index}:p1" for index in range(1, 3)),
    )
    exact_names = ("周星馳", "周星驰")
    repository.person_resolutions[first] = ("周星馳", "actor", exact_names)
    repository.person_resolutions[second] = ("周星驰", "actor", exact_names)

    result = service.answer(current, history)

    assert len(result.movies) == 2
    plan = repository.deep_recommendation_calls[0]
    assert plan.requested_count == 2
    assert plan.genres == ("喜劇",)
    assert plan.tier == "S"
    assert (plan.year_from, plan.year_to) == (1980, 1989)
    assert plan.continuation is True
    assert plan.excluded_movie_ids == (
        "old-deep-a",
        "old-deep-b",
        "old-deep-c",
        "old-deep-d",
    )
    assert plan.context_text == f"{first}\n{second}\n{current}"
    assert plan.person_name in exact_names
    assert plan.person_role == "actor"


def test_failed_deep_turn_cannot_donate_constraints_or_context() -> None:
    """Breaks if deep continuation replays a zero-card recommendation turn."""
    old_success = "推薦1部1980年代S級攝影出色的喜劇電影"
    failed_reset = "推薦1部1990年代A級攝影出色的動作電影"
    current = "再推薦1部攝影出色的電影"
    service, repository, _, _ = _service([], "must not be used", ())
    history = (
        ConversationExchange(old_success, "第一輪。", ("old-deep",)),
        ConversationExchange(failed_reset, "沒有結果。", ()),
    )

    service.answer(current, history)

    plan = repository.deep_recommendation_calls[0]
    assert plan.genres == ("喜劇",)
    assert plan.tier == "S"
    assert (plan.year_from, plan.year_to) == (1980, 1989)
    assert plan.excluded_movie_ids == ("old-deep",)
    assert plan.context_text == f"{old_success}\n{current}"


def test_successful_deep_reset_starts_a_new_active_chain() -> None:
    """Breaks if deep continuation retains exclusions or text before a successful reset."""
    old_success = "推薦1部1980年代S級攝影出色的喜劇電影"
    new_success = "推薦1部1990年代A級攝影出色的動作電影"
    current = "再推薦1部攝影出色的電影"
    service, repository, _, _ = _service([], "must not be used", ())
    history = (
        ConversationExchange(old_success, "第一輪。", ("old-deep",)),
        ConversationExchange(new_success, "第二輪。", ("new-deep",)),
    )

    service.answer(current, history)

    plan = repository.deep_recommendation_calls[0]
    assert plan.genres == ("動作",)
    assert plan.tier == "A"
    assert (plan.year_from, plan.year_to) == (1990, 1999)
    assert plan.excluded_movie_ids == ("new-deep",)
    assert plan.context_text == f"{new_success}\n{current}"


def test_deep_followup_bounds_exclusions_and_context_at_successful_person_switch() -> None:
    """Breaks if deep planning reintroduces IDs or prompt text from before a switch."""
    old_chow = "推薦1部1980年代S級周星馳主演的攝影出色喜劇電影"
    switch_jackie = "換成1部成龍主演的攝影出色電影"
    current = "再推薦1部攝影出色的電影"
    service, repository, _, _ = _service([], "must not be used", ())
    repository.person_resolutions[switch_jackie] = ("成龍", "actor")
    history = (
        ConversationExchange(old_chow, "第一輪。", ("old-chow",)),
        ConversationExchange(switch_jackie, "第二輪。", ("old-jackie",)),
    )

    service.answer(current, history)

    assert repository.person_resolution_calls == [
        (RELEASE_ID, current),
        (RELEASE_ID, switch_jackie),
    ]
    plan = repository.deep_recommendation_calls[0]
    assert (plan.person_name, plan.person_role) == ("成龍", "actor")
    assert plan.genres == ()
    assert plan.tier is None
    assert (plan.year_from, plan.year_to) == (None, None)
    assert plan.excluded_movie_ids == ("old-jackie",)
    assert plan.context_text == f"{switch_jackie}\n{current}"


@pytest.mark.parametrize(
    "question",
    (
        "再推荐2部摄影出色的电影",
        "再来一些摄影出色的",
    ),
)
def test_deep_recommendation_continuation_inherits_prior_structured_constraints(
    question: str,
) -> None:
    eligible = [
        _passage(
            f"pdf:eligible-{index}:p1",
            f"eligible-{index}",
            chinese_title=f"八十年代深度片{index}",
            english_title=f"Eighties Deep {index}",
            genre="喜劇",
            tier="S",
            release_date=f"198{index}-01-01",
            passage_kind="pdf",
            page_number=1,
            source_filename=f"eligible-{index}.pdf",
        )
        for index in range(1, 3)
    ]
    service, repository, _, _ = _service(
        eligible,
        "《八十年代深度片1》：符合受控條件。[pdf:eligible-1:p1]\n"
        "《八十年代深度片2》：符合受控條件。[pdf:eligible-2:p1]",
        ("pdf:eligible-1:p1", "pdf:eligible-2:p1"),
    )
    history = (
        ConversationExchange(
            "推荐2部1980年代S级摄影出色的喜剧电影",
            "上一輪。",
            ("old-1", "old-2"),
        ),
    )

    service.answer(question, history)

    plan = repository.deep_recommendation_calls[0]
    assert plan.requested_count == 2
    assert plan.genres == ("喜劇",)
    assert plan.tier == "S"
    assert plan.year_from == 1980
    assert plan.year_to == 1989
    assert plan.continuation is True
    assert plan.excluded_movie_ids == ("old-1", "old-2")


@pytest.mark.parametrize(
    ("question", "expected_years", "expected_citation"),
    [
        ("推荐1部1994年摄影出色的剧情片", (1994, 1994), "pdf:1994:p1"),
        ("推荐1部2000年以前摄影出色的剧情片", (None, 2000), "pdf:1994:p1"),
        ("推荐1部2000年以后摄影出色的剧情片", (2000, None), "pdf:2004:p1"),
    ],
)
def test_deep_recommendation_preserves_exact_before_and_after_years(
    question: str,
    expected_years: tuple[int | None, int | None],
    expected_citation: str,
) -> None:
    passages = [
        _passage(
            f"pdf:{year}:p1",
            f"movie-{year}",
            chinese_title=f"{year}深度片",
            english_title=f"Deep Film {year}",
            genre="劇情",
            release_date=f"{year}-01-01",
            passage_kind="pdf",
            page_number=1,
            source_filename=f"{year}.pdf",
        )
        for year in (1994, 2004)
    ]
    service, repository, _, _ = _service(
        passages,
        f"《{expected_citation.split(':')[1]}深度片》：有匹配的深度證據。[{expected_citation}]",
        (expected_citation,),
    )

    service.answer(question)

    plan = repository.deep_recommendation_calls[0]
    assert (plan.year_from, plan.year_to) == expected_years
    assert plan.requested_count == 1
    assert plan.genres == ("劇情",)


def test_deep_recommendation_continuation_preserves_diversity_and_exclusions() -> None:
    passages = [
        _passage(
            "pdf:old:p1",
            "old",
            chinese_title="已推薦",
            english_title="Already Recommended",
            genre="劇情",
            release_date="1984-01-01",
            passage_kind="pdf",
            page_number=1,
            source_filename="old.pdf",
        ),
        _passage(
            "pdf:new:p1",
            "new",
            chinese_title="新推薦",
            english_title="New Recommendation",
            genre="劇情",
            release_date="1994-01-01",
            passage_kind="pdf",
            page_number=1,
            source_filename="new.pdf",
        ),
    ]
    service, repository, _, _ = _service(
        passages,
        "《新推薦》：有匹配的深度證據。[pdf:new:p1]",
        ("pdf:new:p1",),
    )
    history = (
        ConversationExchange("推薦一部劇情片", "已推薦。", ("old",)),
    )

    service.answer("再推荐1部不同年代摄影出色的剧情片", history)

    plan = repository.deep_recommendation_calls[0]
    assert plan.requested_count == 1
    assert plan.diversify_decades is True
    assert plan.continuation is True
    assert plan.excluded_movie_ids == ("old",)
    assert plan.context_text == (
        "推薦一部劇情片\n再推荐1部不同年代摄影出色的剧情片"
    )


def test_deep_recommendation_requires_every_selected_pdf_citation_in_order() -> None:
    passages = [
        _passage(
            f"pdf:deep-{index}:p1",
            f"deep-{index}",
            chinese_title=f"深度片{index}",
            english_title=f"Deep Film {index}",
            genre="喜劇",
            passage_kind="pdf",
            page_number=1,
            source_filename=f"deep-{index}.pdf",
        )
        for index in range(1, 3)
    ]
    service, _, _, _ = _service(
        passages,
        "只引用第一部。[pdf:deep-1:p1]",
        ("pdf:deep-1:p1",),
    )

    with pytest.raises(GroundingError, match="required recommendation citations"):
        service.answer("推荐2部摄影出色的喜剧")


def test_different_decades_selects_ranked_candidates_from_distinct_decades_first() -> None:
    passages = [
        _passage(
            "metadata:1981-first",
            "1981-first",
            chinese_title="八十年代甲",
            english_title="Eighties One",
            release_date="1981-01-01",
        ),
        _passage(
            "metadata:1985-second",
            "1985-second",
            chinese_title="八十年代乙",
            english_title="Eighties Two",
            release_date="1985-01-01",
        ),
        _passage(
            "metadata:1992-third",
            "1992-third",
            chinese_title="九十年代甲",
            english_title="Nineties One",
            release_date="1992-01-01",
        ),
        _passage(
            "metadata:2001-fourth",
            "2001-fourth",
            chinese_title="千禧年代甲",
            english_title="Two Thousands One",
            release_date="2001-01-01",
        ),
    ]
    service, repository, _, _ = _service(
        passages,
        (
            "《八十年代甲》：符合不同年代條件。[metadata:1981-first]\n"
            "《九十年代甲》：符合不同年代條件。[metadata:1992-third]\n"
            "《千禧年代甲》：符合不同年代條件。[metadata:2001-fourth]"
        ),
        ("metadata:1981-first", "metadata:1992-third", "metadata:2001-fourth"),
    )

    result = service.answer("推薦3部不同年代的電影")

    assert [movie.movie_id for movie in result.movies] == [
        "1981-first",
        "1992-third",
        "2001-fourth",
    ]
    assert repository.recommendation_calls[0].requested_count == 8


def test_zero_recommendation_matches_does_not_fall_back_to_vector_search() -> None:
    service, repository, _, generator = _service([], "must not be used", ())

    result = service.answer("推薦3部1980年代喜劇")

    assert "沒有符合目前條件的結果" in result.answer_markdown
    assert "喜劇" in result.answer_markdown
    assert "0 個符合結果" in result.answer_markdown
    assert "0 部可用候選" in result.answer_markdown
    assert "3 部" in result.answer_markdown
    assert result.citations == ()
    assert result.movies == ()
    assert len(repository.recommendation_calls) == 1
    assert repository.calls == []
    assert generator.calls == []


def test_partial_recommendation_matches_return_counts_without_generation_or_cards() -> None:
    passages = [
        _passage(
            "metadata:first", "first", chinese_title="第一部", english_title="First", genre="喜劇"
        ),
        _passage(
            "metadata:second", "second", chinese_title="第二部", english_title="Second", genre="喜劇"
        ),
    ]
    service, repository, _, generator = _service(passages, "must not be used", ())
    repository.recommendation_total_matches = 5

    result = service.answer("推薦3部喜劇電影")

    assert "5 個符合結果" in result.answer_markdown
    assert "2 部可用候選" in result.answer_markdown
    assert "3 部" in result.answer_markdown
    assert "喜劇" in result.answer_markdown
    assert result.citations == ()
    assert result.movies == ()
    assert generator.calls == []


def test_recommendation_rejects_missing_required_candidate_citation() -> None:
    passages = [
        _passage("metadata:first", "first", chinese_title="第一部", english_title="First"),
        _passage("metadata:second", "second", chinese_title="第二部", english_title="Second"),
    ]
    service, *_ = _service(
        passages,
        "模型只引用第一部。[metadata:first]",
        ("metadata:first",),
    )

    with pytest.raises(GroundingError, match="required recommendation citations"):
        service.answer("推薦2部電影")


def test_recommendation_rejects_swapped_server_title_and_citation_identity() -> None:
    """Breaks if movie B can be named on movie A's citation/card line."""
    passages = _two_citation_passages()
    service, *_ = _service(
        passages,
        (
            "《第二部》：模型把第二部名稱放到第一部證據上。[metadata:first]\n"
            "《第一部》：模型把第一部名稱放到第二部證據上。[metadata:second]"
        ),
        ("metadata:first", "metadata:second"),
    )

    with pytest.raises(GroundingError, match="recommendation line identity"):
        service.answer("推薦2部電影")


def test_recommendation_line_citation_and_card_orders_share_one_evidence_identity() -> None:
    """Breaks if validated answer order can diverge from citation or card order."""
    passages = _two_citation_passages()
    service, *_ = _service(
        passages,
        (
            "《第一部》：符合受控條件的推薦理由。[metadata:first]\n"
            "《第二部》：也符合受控條件。[metadata:second]"
        ),
        ("metadata:first", "metadata:second"),
    )

    result = service.answer("推薦2部電影")

    assert result.answer_markdown.splitlines() == [
        "《第一部》：符合受控條件的推薦理由。[metadata:first]",
        "《第二部》：也符合受控條件。[metadata:second]",
    ]
    assert [citation.citation_id for citation in result.citations] == [
        "metadata:first",
        "metadata:second",
    ]
    assert [citation.movie_id for citation in result.citations] == ["first", "second"]
    assert [movie.movie_id for movie in result.movies] == ["first", "second"]


@pytest.mark.parametrize(
    "answer_markdown",
    [
        (
            "《第一部》：理由後有額外引用。[metadata:second][metadata:first]\n"
            "《第二部》：正常理由。[metadata:second]"
        ),
        (
            "《第一部》：理由後引用不是唯一結尾。[metadata:first] extra\n"
            "《第二部》：正常理由。[metadata:second]"
        ),
        (
            "前綴《第一部》：理由。[metadata:first]\n"
            "《第二部》：正常理由。[metadata:second]"
        ),
    ],
)
def test_recommendation_rejects_nonsole_marker_or_nonexact_title_prefix(
    answer_markdown: str,
) -> None:
    """Breaks if foreign identity text can hide around an otherwise valid marker."""
    service, *_ = _service(
        _two_citation_passages(),
        answer_markdown,
        ("metadata:first", "metadata:second"),
    )

    with pytest.raises(GroundingError):
        service.answer("推薦2部電影")


def test_recommendation_requires_one_ordered_citation_line_per_candidate() -> None:
    """Breaks if one model sentence can collapse the server-selected recommendation set."""
    passages = [
        _passage("metadata:first", "first", chinese_title="第一部", english_title="First"),
        _passage("metadata:second", "second", chinese_title="第二部", english_title="Second"),
    ]
    service, *_ = _service(
        passages,
        "模型把兩部合成一句。[metadata:first][metadata:second]",
        ("metadata:first", "metadata:second"),
    )

    with pytest.raises(GroundingError, match="one cited line per candidate"):
        service.answer("推薦2部電影")


def test_recommendation_normalizes_one_line_with_ordered_candidate_citations() -> None:
    """Breaks if a safe one-line Vertex recommendation cannot reach the client."""
    passages = [
        _passage(
            "metadata:first", "first", chinese_title="第一部", english_title="First", genre="喜劇"
        ),
        _passage(
            "metadata:second",
            "second",
            chinese_title="第二部",
            english_title="Second",
            genre="喜劇",
        ),
        _passage(
            "metadata:third", "third", chinese_title="第三部", english_title="Third", genre="喜劇"
        ),
    ]
    service, *_ = _service(
        passages,
        (
            "《第一部》：輕鬆有趣。[metadata:first]"
            "《第二部》：節奏明快。[metadata:second]"
            "《第三部》：笑料密集。[metadata:third]"
        ),
        ("metadata:first", "metadata:second", "metadata:third"),
    )

    result = service.answer("請推薦三部喜劇電影。")

    assert result.answer_markdown == (
        "《第一部》：輕鬆有趣。[metadata:first]\n"
        "《第二部》：節奏明快。[metadata:second]\n"
        "《第三部》：笑料密集。[metadata:third]"
    )
    assert [citation.citation_id for citation in result.citations] == [
        "metadata:first",
        "metadata:second",
        "metadata:third",
    ]
    assert [movie.movie_id for movie in result.movies] == ["first", "second", "third"]


@pytest.mark.parametrize(
    "answer_markdown",
    [
        (
            "第一部有完整推薦。[metadata:first]"
            "[metadata:second]"
            "第三部有完整推薦。[metadata:third]"
        ),
        (
            "第二部排在前面。[metadata:second]"
            "第一部排在後面。[metadata:first]"
            "第三部維持最後。[metadata:third]"
        ),
        (
            "第一部重複引用。[metadata:first]"
            "第二部第一次引用。[metadata:second]"
            "第二部再次引用。[metadata:second]"
            "第三部完整引用。[metadata:third]"
        ),
        (
            "第一部有完整推薦。[metadata:first]"
            "第二部有完整推薦。[metadata:second]"
            "第三部有完整推薦。[metadata:third]"
            "另外還推薦第四部。"
        ),
        (
            "推薦如下：\n"
            "第一部有完整推薦。[metadata:first]\n"
            "第二部有完整推薦。[metadata:second]\n"
            "第三部有完整推薦。[metadata:third]"
        ),
    ],
    ids=[
        "empty-grouped-marker-segment",
        "out-of-order-markers",
        "duplicate-marker",
        "substantive-trailing-claim",
        "extra-uncited-header-line",
    ],
)
def test_recommendation_rejects_unsafe_line_normalization(
    answer_markdown: str,
) -> None:
    passages = [
        _passage(
            "metadata:first", "first", chinese_title="第一部", english_title="First", genre="喜劇"
        ),
        _passage(
            "metadata:second",
            "second",
            chinese_title="第二部",
            english_title="Second",
            genre="喜劇",
        ),
        _passage(
            "metadata:third", "third", chinese_title="第三部", english_title="Third", genre="喜劇"
        ),
    ]
    service, *_ = _service(
        passages,
        answer_markdown,
        ("metadata:first", "metadata:second", "metadata:third"),
    )

    with pytest.raises(GroundingError, match="one cited line per candidate"):
        service.answer("請推薦三部喜劇電影。")


def test_recommendation_normalization_preserves_uri_rejection() -> None:
    passages = [
        _passage(
            "metadata:first", "first", chinese_title="第一部", english_title="First", genre="喜劇"
        ),
        _passage(
            "metadata:second",
            "second",
            chinese_title="第二部",
            english_title="Second",
            genre="喜劇",
        ),
        _passage(
            "metadata:third", "third", chinese_title="第三部", english_title="Third", genre="喜劇"
        ),
    ]
    service, *_ = _service(
        passages,
        (
            "urn[metadata:first]"
            ":uuid，第二部有完整推薦。[metadata:second]"
            "第三部有完整推薦。[metadata:third]"
        ),
        ("metadata:first", "metadata:second", "metadata:third"),
    )

    with pytest.raises(GroundingError, match="URL or Markdown link"):
        service.answer("請推薦三部喜劇電影。")


def test_generic_movie_word_never_exact_matches_one_character_title() -> None:
    evidence = (
        EvidencePassage(
            passage_id="metadata:ying",
            movie_id="ying",
            source_kind="movie_metadata",
            body="movie_id: ying\nchinese_title: 影",
            page_number=None,
            source_filename=None,
            movie={"movie_id": "ying", "chinese_title": "影"},
            poster_available=False,
        ),
    )

    assert _exact_target_movie_ids("請列出三部喜劇電影", evidence) == ()
    for left, right in (("《", "》"), ("「", "」"), ("『", "』"), ("【", "】")):
        assert _exact_target_movie_ids(f"{left}影{right}的導演是誰？", evidence) == (
            "ying",
        )
    assert _exact_target_movie_ids("影", evidence) == ("ying",)


def test_short_ascii_titles_require_token_boundaries_in_python_policy() -> None:
    evidence = (
        EvidencePassage(
            passage_id="metadata:ex",
            movie_id="ex-movie",
            source_kind="movie_metadata",
            body="movie_id: ex-movie\nenglish_title: EX",
            page_number=None,
            source_filename=None,
            movie={"movie_id": "ex-movie", "english_title": "EX"},
            poster_available=False,
        ),
        EvidencePassage(
            passage_id="metadata:av",
            movie_id="av-movie",
            source_kind="movie_metadata",
            body="movie_id: av-movie\nenglish_title: AV",
            page_number=None,
            source_filename=None,
            movie={"movie_id": "av-movie", "english_title": "AV"},
            poster_available=False,
        ),
    )

    assert _exact_target_movie_ids("please explain the ending", evidence) == ()
    assert _exact_target_movie_ids("what movies have this style", evidence) == ()
    assert _exact_target_movie_ids("EX", evidence) == ("ex-movie",)
    assert _exact_target_movie_ids("比较《AV》与《EX》", evidence) == (
        "av-movie",
        "ex-movie",
    )


def test_delimited_movie_target_ignores_incidental_genre_title_collision() -> None:
    """Breaks if a genre word that is also a title hijacks one quoted movie target."""
    resolution = retrieval.resolve_explicit_movie_identity_context(
        "《少林足球》如何把足球、功夫和喜劇結合？",
        (
            ("2001_SLZQ_001", "少林足球", "Shaolin Soccer"),
            ("2004_GF_001", "功夫", "Kung Fu Hustle"),
        ),
    )

    assert resolution.movie_ids == ("2001_SLZQ_001",)
    assert resolution.ambiguous is False


def test_long_explicit_title_does_not_also_resolve_its_short_title_prefix() -> None:
    evidence = (
        EvidencePassage(
            passage_id="metadata:hero",
            movie_id="hero",
            source_kind="movie_metadata",
            body="movie_id: hero\nchinese_title: 英雄",
            page_number=None,
            source_filename=None,
            movie={"movie_id": "hero", "chinese_title": "英雄"},
            poster_available=False,
        ),
        EvidencePassage(
            passage_id="metadata:heroic-colors",
            movie_id="heroic-colors",
            source_kind="movie_metadata",
            body="movie_id: heroic-colors\nchinese_title: 英雄本色",
            page_number=None,
            source_filename=None,
            movie={"movie_id": "heroic-colors", "chinese_title": "英雄本色"},
            poster_available=False,
        ),
    )

    assert _exact_target_movie_ids("比較英雄本色與其他電影", evidence) == (
        "heroic-colors",
    )


def test_one_duplicate_release_title_mention_requires_disambiguation() -> None:
    passages = [
        _passage(
            "metadata:1980_YX_001",
            "1980_YX_001",
            chinese_title="英雄",
            english_title="Hero 1980",
        ),
        _passage(
            "metadata:2002_YX_001",
            "2002_YX_001",
            chinese_title="英雄",
            english_title="Hero 2002",
        ),
    ]
    service, repository, embedder, generator = _service(
        passages,
        "不應使用。[metadata:1980_YX_001]",
        ("metadata:1980_YX_001",),
    )

    result = service.answer("《英雄》的導演是誰？")

    assert "同名" in result.answer_markdown
    assert "movie ID" in result.answer_markdown
    assert "1980_YX_001" in result.answer_markdown
    assert "2002_YX_001" in result.answer_markdown
    assert "上映年份" not in result.answer_markdown
    assert result.citations == ()
    assert result.movies == ()
    assert repository.calls == []
    assert embedder.calls == []
    assert generator.calls == []


def test_duplicate_title_resolution_exposes_ambiguity_without_target_ids() -> None:
    resolution = retrieval.resolve_explicit_movie_identity_context(
        "《英雄》的導演是誰？",
        (
            ("1980_YX_001", "英雄", "Hero 1980"),
            ("2002_YX_001", "英雄", "Hero 2002"),
        ),
    )

    assert resolution.movie_ids == ()
    assert resolution.ambiguous is True
    assert resolution.ambiguous_movie_ids == (
        "1980_YX_001",
        "2002_YX_001",
    )


@pytest.mark.parametrize(
    "reason",
    [
        "避免影射第\u200b二部",
        "避免影射第 二 部",
        "避免影射第-二部",
        "避免影射第\ufe0f二部",
        "避免影射第\u034f二部",
        "正常理由\u202e",
        "Use Ｓｅｃｏｎｄ as a hidden identity",
        "",
    ],
    ids=[
        "zero-width",
        "spaced-identity",
        "punctuated-identity",
        "variation-selector",
        "combining-grapheme-joiner",
        "bidi-control",
        "fullwidth-identity",
        "empty",
    ],
)
def test_query_service_rejects_unicode_identity_injection_in_model_reason(
    reason: str,
) -> None:
    service, *_ = _service(
        _two_citation_passages(),
        (
            f"《第一部》：{reason}。[metadata:first]\n"
            "《第二部》：正常理由。[metadata:second]"
        ),
        ("metadata:first", "metadata:second"),
    )

    with pytest.raises(GroundingError, match="recommendation line identity"):
        service.answer("推薦2部電影")


class _GenerationModels:
    def __init__(self, response: object | None = None) -> None:
        self.calls: list[dict[str, object]] = []
        self.response = response or SimpleNamespace(
            parsed={
                "items": [
                    {
                        "citation_id": "metadata:movie",
                        "reason": "符合受控條件",
                    }
                ],
            },
            text=None,
        )

    def generate_content(self, **kwargs: object) -> object:
        self.calls.append(kwargs)
        return self.response


def _vertex_evidence() -> tuple[EvidencePassage, ...]:
    return (
        EvidencePassage(
            passage_id="metadata:movie",
            movie_id="movie",
            source_kind="movie_metadata",
            body="受控資料",
            page_number=None,
            source_filename=None,
            movie={"movie_id": "movie", "chinese_title": "電影"},
            poster_available=False,
        ),
    )


def test_vertex_generation_uses_fixed_model_json_schema_and_no_custom_sampling(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    models = _GenerationModels()
    observed_client: dict[str, object] = {}

    def fake_client(**kwargs: object) -> object:
        observed_client.update(kwargs)
        return SimpleNamespace(models=models)

    monkeypatch.setattr("hk_movie_rag.vertex_clients.genai.Client", fake_client)
    client = VertexGenerationClient(
        "motionexpaiweb", "global", "gemini-3.5-flash-lite"
    )
    evidence = _vertex_evidence()

    result = client.generate_grounded(
        "電影是甚麼？",
        evidence,
        mode="recommendation",
        conversation_context=("先推薦香港喜劇",),
        required_citation_ids=("metadata:movie",),
    )

    assert result == GeneratedAnswer(
        "《電影》：符合受控條件。[metadata:movie]", ("metadata:movie",)
    )
    assert observed_client["vertexai"] is True
    assert observed_client["project"] == "motionexpaiweb"
    assert observed_client["location"] == "global"
    call = models.calls[0]
    assert call["model"] == "gemini-3.5-flash-lite"
    config = call["config"]
    assert config.max_output_tokens == 1024
    assert config.response_mime_type == "application/json"
    assert config.response_json_schema["required"] == ["items"]
    item_schema = config.response_json_schema["properties"]["items"]
    assert item_schema["minItems"] == 1
    assert item_schema["maxItems"] == 1
    assert item_schema["prefixItems"][0]["properties"]["citation_id"]["enum"] == [
        "metadata:movie"
    ]
    assert config.temperature is None
    assert config.top_k is None
    assert config.top_p is None
    assert "只可使用" in config.system_instruction
    assert "目前只有結構化電影資料" in config.system_instruction
    assert "獨立方括號" in config.system_instruction
    assert "[id1][id2]" in config.system_instruction
    assert "conversation_context_not_evidence" in config.system_instruction
    assert "攝影、視覺、動作設計" in config.system_instruction
    assert "推薦字眼不能覆蓋深度分析判斷" in config.system_instruction
    payload = json.loads(str(call["contents"]))
    assert payload["answer_mode"] == "recommendation"
    assert payload["conversation_context_not_evidence"] == ["先推薦香港喜劇"]
    assert payload["required_citation_ids"] == ["metadata:movie"]
    assert payload["supplied_evidence"][0]["movie"]["movie_id"] == "movie"


def test_vertex_rejects_deep_criteria_in_recommendation_mode_before_generation(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Breaks if a planner regression can send deep criteria to recommendation generation."""
    models = _GenerationModels()
    monkeypatch.setattr(
        "hk_movie_rag.vertex_clients.genai.Client",
        lambda **_kwargs: SimpleNamespace(models=models),
    )
    client = VertexGenerationClient(
        "motionexpaiweb", "global", "gemini-3.5-flash-lite"
    )

    with pytest.raises(VertexGenerationError, match="deep-analysis"):
        client.generate_grounded(
            "推薦攝影、視覺與動作設計出色的電影",
            _vertex_evidence(),
            mode="recommendation",
            conversation_context=(),
            required_citation_ids=("metadata:movie",),
        )

    assert models.calls == []


def test_vertex_deep_recommendation_mode_requires_all_ordered_pdf_passages(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    evidence = tuple(
        EvidencePassage(
            passage_id=f"pdf:movie-{index}:p1",
            movie_id=f"movie-{index}",
            source_kind="pdf_page",
            body=f"第 {index} 部的深度文檔證據",
            page_number=1,
            source_filename=f"movie-{index}.pdf",
            movie={"movie_id": f"movie-{index}", "chinese_title": f"電影{index}"},
            poster_available=False,
        )
        for index in range(1, 3)
    )
    response = SimpleNamespace(
        parsed={
            "items": [
                {
                    "citation_id": "pdf:movie-1:p1",
                    "reason": "深度證據符合條件",
                },
                {
                    "citation_id": "pdf:movie-2:p1",
                    "reason": "另一份深度證據也符合條件",
                },
            ],
        },
        text=None,
    )
    models = _GenerationModels(response)
    monkeypatch.setattr(
        "hk_movie_rag.vertex_clients.genai.Client",
        lambda **_kwargs: SimpleNamespace(models=models),
    )
    client = VertexGenerationClient(
        "motionexpaiweb", "global", "gemini-3.5-flash-lite"
    )

    result = client.generate_grounded(
        "推荐2部摄影风格鲜明的喜剧",
        evidence,
        mode="deep_recommendation",
        conversation_context=(),
        required_citation_ids=("pdf:movie-1:p1", "pdf:movie-2:p1"),
    )

    assert result.citation_ids == ("pdf:movie-1:p1", "pdf:movie-2:p1")
    payload = json.loads(str(models.calls[0]["contents"]))
    assert payload["answer_mode"] == "deep_recommendation"
    assert payload["required_citation_ids"] == [
        "pdf:movie-1:p1",
        "pdf:movie-2:p1",
    ]
    assert "深度文檔" in models.calls[0]["config"].system_instruction


@pytest.mark.parametrize(
    ("question", "evidence", "required_ids"),
    [
        ("推荐2部喜剧", "pdf", ("pdf:movie-1:p1", "pdf:movie-2:p1")),
        ("推荐2部摄影出色的喜剧", "metadata", ("metadata:movie",)),
        ("推荐2部摄影出色的喜剧", "pdf", ("pdf:movie-1:p1",)),
    ],
)
def test_vertex_rejects_invalid_deep_recommendation_mode_contract(
    monkeypatch: pytest.MonkeyPatch,
    question: str,
    evidence: str,
    required_ids: tuple[str, ...],
) -> None:
    models = _GenerationModels()
    monkeypatch.setattr(
        "hk_movie_rag.vertex_clients.genai.Client",
        lambda **_kwargs: SimpleNamespace(models=models),
    )
    client = VertexGenerationClient(
        "motionexpaiweb", "global", "gemini-3.5-flash-lite"
    )
    pdf_evidence = tuple(
        EvidencePassage(
            passage_id=f"pdf:movie-{index}:p1",
            movie_id=f"movie-{index}",
            source_kind="pdf_page",
            body="深度證據",
            page_number=1,
            source_filename=f"movie-{index}.pdf",
            movie={"movie_id": f"movie-{index}"},
            poster_available=False,
        )
        for index in range(1, 3)
    )

    with pytest.raises(VertexGenerationError, match="deep recommendation"):
        client.generate_grounded(
            question,
            pdf_evidence if evidence == "pdf" else _vertex_evidence(),
            mode="deep_recommendation",
            conversation_context=(),
            required_citation_ids=required_ids,
        )

    assert models.calls == []


@pytest.mark.parametrize(
    "response",
    [
        SimpleNamespace(parsed=None, text="{not-json"),
        SimpleNamespace(
            parsed={
                "answer_markdown": "有證據。[metadata:movie]",
                "citation_ids": ["metadata:movie"],
                "poster_url": "https://model-controlled.invalid/poster",
            },
            text=None,
        ),
    ],
)
def test_vertex_generation_rejects_malformed_json_and_extra_keys(
    monkeypatch: pytest.MonkeyPatch, response: object
) -> None:
    models = _GenerationModels(response)
    monkeypatch.setattr(
        "hk_movie_rag.vertex_clients.genai.Client",
        lambda **_kwargs: SimpleNamespace(models=models),
    )
    client = VertexGenerationClient(
        "motionexpaiweb", "global", "gemini-3.5-flash-lite"
    )

    with pytest.raises(VertexGenerationError, match="generation response"):
        client.generate_grounded(
            "問題",
            _vertex_evidence(),
            mode="general",
            conversation_context=(),
            required_citation_ids=(),
        )


@dataclass(frozen=True)
class _SqlCall:
    sql: str
    params: tuple[object, ...]


@dataclass
class _SearchConnection:
    calls: list[_SqlCall] = field(default_factory=list)
    contract_matches: bool = True
    facet_stats: tuple[int, int, int, int, int] = (4658, 50, 313, 4295, 24)
    explicit_target_rows: list[tuple[object, ...]] = field(
        default_factory=lambda: [("1978_ZQ_001", "醉拳", "Drunken Master", 1)]
    )

    @contextmanager
    def transaction(self) -> Iterator[_SearchConnection]:
        yield self

    @contextmanager
    def cursor(self) -> Iterator[_SearchCursor]:
        yield _SearchCursor(self)


def _search_release_contract() -> RagReleaseContract:
    return RagReleaseContract(
        schema_version="1.2",
        rag_release_id=RELEASE_ID,
        parent_release_manifest_sha256="c" * 64,
        manifest_sha256=RELEASE_MANIFEST_SHA256,
        bundle_sha256="b" * 64,
        derived_inventory_sha256="d" * 64,
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
        relevance_policy_sha256=RELEVANCE_POLICY_SHA256,
        poster_authority_sha256="e" * 64,
        access_mode="restricted_demo",
    )


class _SearchCursor:
    def __init__(self, connection: _SearchConnection) -> None:
        self.connection = connection
        self.sql = ""

    def execute(self, sql: str, params: tuple[object, ...] | None = None) -> None:
        self.sql = sql
        self.connection.calls.append(_SqlCall(sql, params or ()))

    def fetchone(self) -> tuple[object, ...] | None:
        if "FROM movie_facets" in self.sql:
            return self.connection.facet_stats
        if "release.expected_movies" in self.sql and "FOR SHARE" in self.sql:
            if not self.connection.contract_matches:
                return None
            contract = _search_release_contract()
            return (
                "active",
                4658,
                4658,
                4658,
                2,
                6,
                contract.embedding_model,
                contract.embedding_dimension,
                contract.generation_model,
                contract.access_mode,
                contract.manifest_sha256,
                contract.document_embedding_profile,
                asdict(contract),
                4664,
            )
        return (RELEASE_ID,) if self.connection.contract_matches else None

    def fetchall(self) -> list[tuple[object, ...]]:
        if "broad_match.broad_position" in self.sql:
            return self.connection.explicit_target_rows
        return [
            (
                "pdf:drunken:p1",
                "1978_ZQ_001",
                "pdf",
                "page body",
                1,
                "drunken-doc",
                "drunken.pdf",
                {
                    "movie_id": "1978_ZQ_001",
                    "chinese_title": "醉拳",
                    "tier": "S",
                    "tier_reason": "fixture tier",
                    "pilot_movie": False,
                    "pilot_evidence": None,
                },
                True,
                0.25,
            )
        ]


def test_repository_question_search_returns_grounding_records_and_target_id_sql() -> None:
    connection = _SearchConnection()
    repository = RagRepository(connection)

    result = repository.search(
        RELEASE_ID,
        [0.1] * 768,
        limit=8,
        question="比較醉拳與其他電影",
        embedding_model="gemini-embedding-2",
        embedding_dimension=768,
    )

    assert result[0]["page_number"] == 1
    assert result[0]["movie"] == {
        "movie_id": "1978_ZQ_001",
        "chinese_title": "醉拳",
        "tier": "S",
        "tier_reason": "fixture tier",
        "pilot_movie": False,
        "pilot_evidence": None,
    }
    assert result[0]["poster_available"] is True
    contract_call = connection.calls[0]
    assert "FOR SHARE" in contract_call.sql
    assert "release.contract_json" in contract_call.sql
    assert contract_call.params == (RELEASE_ID,)
    assert "FROM movie_facets" in connection.calls[1].sql
    call = connection.calls[2]
    assert "exact_position" in call.sql
    assert "array_position(query_input.target_movie_ids, chunk.movie_id)" in call.sql
    assert "normalized_question" not in call.sql
    assert "payload ->> 'chinese_title'" not in call.sql
    assert "payload ->> 'english_title'" not in call.sql
    assert call.sql.count("%s") == len(call.params)
    assert "quality_status" in call.sql
    assert "asset.is_primary" in call.sql
    assert "asset.derived_content_sha256 ~ '^[0-9a-f]{64}$'" in call.sql
    assert "asset.derived_byte_length > 0" in call.sql
    assert "asset.derived_mime_type = 'image/webp'" in call.sql
    assert call.params[-1] == 8
    assert "比較醉拳與其他電影" not in call.params


def test_repository_resolves_current_explicit_targets_before_history_fallback() -> None:
    connection = _SearchConnection()
    repository = RagRepository(connection)

    result = repository.resolve_explicit_movie_ids(
        RELEASE_ID, "What about this movie, Drunken Master?"
    )

    assert result == ("1978_ZQ_001",)
    call = connection.calls[-1]
    assert "broad_match.broad_position" in call.sql
    assert "broad_match.broad_position IS NOT NULL" in call.sql
    assert "movie.payload ->> 'chinese_title'" in call.sql
    assert "movie.payload ->> 'english_title'" in call.sql
    assert "GREATEST(char_length(chinese_title), char_length(english_title)) DESC" in call.sql
    assert call.params[-1] == 64
    assert call.sql.count("%s") == len(call.params)


def test_repository_broad_title_candidates_receive_opencc_normalized_question() -> None:
    """Breaks if candidate SQL misses a simplified title outside hand-written mappings."""
    question = "最佳拍档的導演是誰？"
    connection = _SearchConnection(
        explicit_target_rows=[
            ("1982_ZJPD_001", "最佳拍檔", "Aces Go Places", 1)
        ]
    )
    repository = RagRepository(connection)

    result = repository.resolve_explicit_movie_ids(RELEASE_ID, question)

    assert result == ("1982_ZJPD_001",)
    call = connection.calls[-1]
    assert call.params[0] == question
    assert call.params[1] == retrieval.normalize_query_text(question)
    assert call.params[1] != question


@pytest.mark.parametrize(
    ("query_title", "stored_title"),
    (
        ("导火线", "導火綫"),
        ("聊斋艳谭", "聊齋艷譚"),
        ("匯豐號黃金大風暴", "匯豐號黃金大風暴"),
    ),
)
def test_query_and_stored_hk_title_variants_share_one_equivalence_key(
    query_title: str, stored_title: str
) -> None:
    query_key = retrieval.normalize_title_text(
        retrieval.normalize_query_text(query_title)
    )
    stored_key = retrieval.normalize_title_text(stored_title)

    assert query_key == stored_key


@pytest.mark.parametrize(
    ("question", "stored_title"),
    (
        ("讲一下多謝老闆娘的独特之处", "多謝老闆娘"),
        ("讲一下梁山怪招的独特之处", "梁山怪招"),
        ("讲一下冲天炮的独特之处", "沖天炮"),
        ("讲一下回转寿尸的独特之处", "迴轉壽屍"),
        ("讲一下摆渡人的独特之处", "擺渡人"),
        ("看表哥到怎么样", "表哥到"),
        ("方世玉与胡惠干与醉拳比较", "方世玉與胡惠乾"),
        ("錯體追擊組合中", "錯體追擊組合"),
        ("有没有云南奇趣录", "雲南奇趣錄"),
        ("两个只能活一个中谁演的", "兩個只能活一個"),
    ),
)
def test_context_sensitive_opencc_title_variants_share_one_equivalence_key(
    question: str, stored_title: str
) -> None:
    normalized_question = retrieval.normalize_title_text(
        retrieval.normalize_query_text(question)
    )

    assert retrieval.normalize_title_text(stored_title) in normalized_question


def test_title_equivalence_map_is_order_independent_across_python_and_sql() -> None:
    """Breaks if nested SQL replacements can cascade unlike ``str.translate``."""
    sources = [source for source, _ in retrieval.TITLE_NORMALIZATION_REPLACEMENTS]
    targets = [target for _, target in retrieval.TITLE_NORMALIZATION_REPLACEMENTS]

    assert all(
        len(source) == len(target) == 1
        for source, target in retrieval.TITLE_NORMALIZATION_REPLACEMENTS
    )
    assert len(sources) == len(set(sources))
    assert set(sources).isdisjoint(targets)


@pytest.mark.parametrize(
    ("question", "movie_id", "stored_title"),
    (
        ("《导火线》的导演是谁？", "2007_DHX_001", "導火綫"),
        ("《聊斋艳谭》的导演是谁？", "1990_LZYT_001", "聊齋艷譚"),
        (
            "《匯豐號黃金大風暴》的导演是谁？",
            "1979_HFHHJDFB_001",
            "匯豐號黃金大風暴",
        ),
    ),
)
def test_repository_resolves_release_title_orthographic_variants(
    question: str, movie_id: str, stored_title: str
) -> None:
    connection = _SearchConnection(
        explicit_target_rows=[(movie_id, stored_title, "Fixture", 1)]
    )
    repository = RagRepository(connection)

    assert repository.resolve_explicit_movie_ids(RELEASE_ID, question) == (movie_id,)


@pytest.mark.parametrize(
    ("question", "expected_movie_ids", "expected_ambiguous"),
    (
        ("《證人》的導演是誰？", ("1993_ZR_001",), False),
        ("《証人》的導演是誰？", ("2008_ZR_001",), False),
        ("《证人》的导演是谁？", (), True),
    ),
)
def test_exact_raw_title_precedes_orthographic_equivalence_collision(
    question: str,
    expected_movie_ids: tuple[str, ...],
    expected_ambiguous: bool,
) -> None:
    resolution = retrieval.resolve_explicit_movie_identity_context(
        question,
        (
            ("1993_ZR_001", "證人", "The Beheaded 1000"),
            ("2008_ZR_001", "証人", "The Beast Stalker"),
        ),
    )

    assert resolution.movie_ids == expected_movie_ids
    assert resolution.ambiguous is expected_ambiguous


@pytest.mark.parametrize(
    "question",
    (
        "英雄联盟怎麼玩？",
        "讲一下超级英雄的成长",
        "講一下超級英雄的成長",
    ),
)
def test_repository_strictly_rejects_short_title_candidate_inside_longer_phrase(
    question: str,
) -> None:
    connection = _SearchConnection(
        explicit_target_rows=[("1980_YX_001", "英雄", "Hero", 1)]
    )
    repository = RagRepository(connection)

    result = repository.resolve_explicit_movie_ids(RELEASE_ID, question)

    assert result == ()


@pytest.mark.parametrize(
    ("question", "governed_title"),
    (
        ("講一下她的想法", "她"),
        ("屋的面積怎麼算？", "屋"),
        ("聊聊朋友之間的信任", "朋友"),
        ("談談天堂是否存在", "天堂"),
        ("請分析海燕的遷徙習性", "海燕"),
        ("讲一下英雄的成长", "英雄"),
        ("講一下英雄的成長", "英雄"),
    ),
)
def test_repository_rejects_topic_independent_common_short_titles(
    question: str, governed_title: str
) -> None:
    connection = _SearchConnection(
        explicit_target_rows=[("1980_CM_001", governed_title, "Common Title", 1)]
    )
    repository = RagRepository(connection)

    assert repository.resolve_explicit_movie_ids(RELEASE_ID, question) == ()


@pytest.mark.parametrize(
    "question",
    (
        "讲一下富贵逼人的喜剧创作",
        "講下富貴逼人的喜劇創作",
        "聊聊富貴逼人的家庭喜劇",
        "我想了解富貴逼人的創作特色",
        "请分析一下富贵逼人的笑点设计",
        "富貴逼人的喜劇創作有何特色？",
        "讲一下富贵逼人这部电影的喜剧创作",
        "说说富贵逼人的喜剧创作",
        "请帮我讲一下富贵逼人的喜剧创作",
        "请你讲一下富贵逼人的喜剧创作",
        "讲一讲富贵逼人的喜剧创作",
        "请，讲一下富贵逼人的喜剧创作",
        "麻烦你讲一下富贵逼人的喜剧创作",
        "讲一下，富贵逼人的喜剧创作",
        "讲一下富贵逼人的喜剧书写",
    ),
)
def test_repository_resolves_conversational_release_title_without_topic_whitelist(
    question: str,
) -> None:
    """Breaks if a safe title mention must use one pre-enumerated movie topic."""
    connection = _SearchConnection(
        explicit_target_rows=[
            ("1987_FGBR_001", "富貴逼人", "It's a Mad, Mad, Mad World", 1)
        ]
    )
    repository = RagRepository(connection)

    assert repository.resolve_explicit_movie_ids(RELEASE_ID, question) == (
        "1987_FGBR_001",
    )


@pytest.mark.parametrize(
    "question",
    [
        "《英雄》小說講什麼？",
        "《英雄》這本小說講什麼？",
        "《英雄》這本書講什麼？",
        "《英雄》書講什麼？",
        "《英雄》书讲什么？",
        "《英雄》書的內容？",
        "電影英雄書講什麼？",
        "香港電影英雄書的內容？",
        "電影富貴逼人書講什麼？",
        "《英雄》書值得看嗎？",
        "《英雄》書的作者是誰？",
        "《英雄》書出版於哪年？",
        "《英雄》書評怎麼樣？",
        "《英雄》書讀起來如何？",
        "《英雄》書和電影比較",
        "《英雄》改編成的小說講什麼？",
        "《英雄》相關的遊戲怎麼樣？",
        "《英雄》遊戲怎麼樣？",
        "1980_YX_001小說講什麼？",
        "1980_YX_001改編的小說講什麼？",
    ],
)
def test_repository_rejects_candidate_owned_nonmovie_object(question: str) -> None:
    connection = _SearchConnection(
        explicit_target_rows=[("1980_YX_001", "英雄", "Hero", 1)]
    )
    repository = RagRepository(connection)

    assert repository.resolve_explicit_movie_ids(RELEASE_ID, question) == ()


@pytest.mark.parametrize(
    "question",
    [
        "《死亡遊戲》好看嗎？",
        "死亡遊戲怎麼樣？",
        "推薦電影《死亡遊戲》",
        "推薦《死亡遊戲》",
    ],
)
def test_repository_preserves_full_title_containing_nonmovie_word(
    question: str,
) -> None:
    connection = _SearchConnection(
        explicit_target_rows=[
            ("1978_SWYX_001", "死亡遊戲", "Game of Death", 1)
        ]
    )
    repository = RagRepository(connection)

    assert repository.resolve_explicit_movie_ids(RELEASE_ID, question) == (
        "1978_SWYX_001",
    )


def test_repository_collectively_masks_all_comparison_title_candidates() -> None:
    connection = _SearchConnection(
        explicit_target_rows=[
            ("1978_SWYX_001", "死亡遊戲", "Game of Death", 3),
            ("1978_ZQ_001", "醉拳", "Drunken Master", 12),
        ]
    )
    repository = RagRepository(connection)

    assert repository.resolve_explicit_movie_ids(
        RELEASE_ID, "比較《死亡遊戲》和《醉拳》"
    ) == ("1978_SWYX_001", "1978_ZQ_001")


def test_repository_search_trusts_resolved_target_ids_without_wide_title_matching() -> None:
    connection = _SearchConnection()
    repository = RagRepository(connection)

    repository.search(
        RELEASE_ID,
        [0.1] * 768,
        limit=8,
        question="英雄联盟怎麼玩？",
        embedding_model="gemini-embedding-2",
        embedding_dimension=768,
        target_movie_ids=("1980_YX_001",),
    )

    call = connection.calls[-1]
    assert "array_position(query_input.target_movie_ids, chunk.movie_id)" in call.sql
    assert "normalized_question" not in call.sql
    assert "payload ->> 'chinese_title'" not in call.sql
    assert "payload ->> 'english_title'" not in call.sql


def test_repository_exact_title_delimiters_are_validated_by_shared_python_policy() -> None:
    """Delimiter authority lives in the strict shared policy, not broad candidate SQL."""
    connection = _SearchConnection()
    repository = RagRepository(connection)

    result = repository.resolve_explicit_movie_ids(
        RELEASE_ID, "比較《醉拳》與其他電影"
    )

    assert result == ("1978_ZQ_001",)
    call = connection.calls[-1]
    assert "broad_position" in call.sql
    assert "concat(%s::text, normalized_title.chinese_title" not in call.sql


def test_repository_question_search_filters_fresh_records_to_exact_target_ids() -> None:
    """Breaks if prior IDs affect only query prose while SQL can return unrelated movies."""
    connection = _SearchConnection()
    repository = RagRepository(connection)

    repository.search(
        RELEASE_ID,
        [0.1] * 768,
        limit=8,
        question="第二部的導演是誰？",
        embedding_model="gemini-embedding-2",
        embedding_dimension=768,
        target_movie_ids=("1978_ZQ_001",),
    )

    call = connection.calls[-1]
    assert "chunk.movie_id = ANY(query_input.target_movie_ids)" in call.sql
    assert "cardinality(query_input.target_movie_ids) = 0" in call.sql
    assert ["1978_ZQ_001"] in call.params
    assert "1978_ZQ_001" not in call.params
    assert "第二部的導演是誰？" not in call.params
    assert "normalized_question" not in call.sql


@pytest.mark.parametrize(
    "target_movie_ids",
    [
        ["1978_ZQ_001"],
        ("1978_ZQ_001", "1978_ZQ_001"),
        tuple(f"1978_A{index}_001" for index in range(9)),
        ("not-a-governed-id",),
    ],
)
def test_repository_rejects_unbounded_or_invalid_target_movie_ids_before_sql(
    target_movie_ids: object,
) -> None:
    """Breaks if malformed history identities can reach the target SQL parameter."""
    connection = _SearchConnection()
    repository = RagRepository(connection)

    with pytest.raises(RagDatabaseError, match="target movie IDs"):
        repository.search(
            RELEASE_ID,
            [0.1] * 768,
            limit=8,
            question="問題",
            embedding_model="gemini-embedding-2",
            embedding_dimension=768,
            target_movie_ids=target_movie_ids,  # type: ignore[arg-type]
        )

    assert connection.calls == []


@pytest.mark.parametrize(
    ("question", "governed_title", "expected_match"),
    [
        ("please explain the ending", "EX", False),
        ("what movies have this style", "AV", False),
        ("EX", "EX", True),
        ("比較《EX》與其他電影", "EX", True),
        ("AV / EX", "AV", True),
    ],
)
def test_repository_short_ascii_title_uses_shared_strict_python_policy(
    question: str, governed_title: str, expected_match: bool
) -> None:
    """Breaks if broad EX/AV candidates bypass the shared strict title policy."""
    connection = _SearchConnection(
        explicit_target_rows=[("1978_TEST_001", "", governed_title, 1)]
    )
    repository = RagRepository(connection)

    result = repository.resolve_explicit_movie_ids(RELEASE_ID, question)

    call = connection.calls[-1]
    assert "broad_match.broad_position" in call.sql
    assert question in call.params
    assert bool(result) is expected_match


def test_repository_question_search_fails_if_active_contract_changes_before_search() -> None:
    connection = _SearchConnection(contract_matches=False)
    repository = RagRepository(connection)

    with pytest.raises(RagDatabaseError, match="query embedding contract"):
        repository.search(
            RELEASE_ID,
            [0.1] * 768,
            limit=8,
            question="醉拳",
            embedding_model="gemini-embedding-2",
            embedding_dimension=768,
        )

    assert len(connection.calls) == 1
    assert "FOR SHARE" in connection.calls[0].sql


@pytest.mark.parametrize(
    "reset_question",
    (
        "推薦3部成龍主演的電影",
        "recommend 3 more 成龍 movies",
    ),
    ids=("explicit-reset", "parser-gap-reset"),
)
def test_successful_person_reset_deactivates_older_ambiguous_history(
    reset_question: str,
) -> None:
    """An older ambiguous turn must not be replayed after a proven reset."""
    ambiguous = "推薦周星馳或成龍的電影"
    current = "再推薦3部，不要重複"
    history = (
        ConversationExchange(ambiguous, "第一輪。", ("ambiguous-card",)),
        ConversationExchange(reset_question, "第二輪。", ("chan-a", "chan-b")),
    )
    service, repository, _, _ = _service([], "must not be used", ())
    repository.ambiguous_person_questions.add(ambiguous)
    repository.person_resolutions[reset_question] = ("成龍", "actor")

    service.answer(current, history)

    assert (RELEASE_ID, ambiguous) not in repository.person_resolution_calls
    plan = repository.recommendation_calls[0]
    assert (plan.person_name, plan.person_role) == ("成龍", "actor")
    assert plan.excluded_movie_ids == ("chan-a", "chan-b")
    assert plan.context_text == f"{reset_question}\n{current}"


def test_same_person_lexical_switch_without_dedup_replays_full_chain() -> None:
    """A same-identity lexical switch remains a continuation on the next turn."""
    first = "推薦3部1980年代S級周星馳主演的喜劇電影"
    switch = "那周星馳呢"
    current = "再推薦3部，不要重複"
    history = (
        ConversationExchange(first, "第一輪。", ("chow-a", "chow-b")),
        ConversationExchange(switch, "第二輪。", ("chow-c", "chow-d")),
    )
    service, repository, _, _ = _service([], "must not be used", ())
    exact_names = ("周星馳", "周星驰")
    repository.person_resolutions[first] = ("周星馳", "actor", exact_names)
    repository.person_resolutions[switch] = ("周星驰", "actor", exact_names)

    service.answer(current, history)

    plan = repository.recommendation_calls[0]
    assert plan.continuation is True
    assert plan.genres == ("喜劇",)
    assert plan.tier == "S"
    assert (plan.year_from, plan.year_to) == (1980, 1989)
    assert plan.excluded_movie_ids == ("chow-a", "chow-b", "chow-c", "chow-d")
    assert plan.context_text == f"{first}\n{switch}\n{current}"
    assert plan.person_name in exact_names


def test_cross_language_same_person_switch_replays_the_full_release_identity() -> None:
    first = "recommend 3 movies starring Stephen Chow"
    switch = "那周星馳呢"
    current = "recommend 3 more, no duplicates"
    history = (
        ConversationExchange(first, "First turn.", ("chow-a",)),
        ConversationExchange(switch, "第二輪。", ("chow-b",)),
    )
    service, repository, _, _ = _service([], "must not be used", ())
    exact_names = ("周星馳", "周星驰")
    repository.person_resolutions[first] = ("周星馳", "actor", exact_names)
    repository.person_resolutions[switch] = ("周星馳", "actor", exact_names)

    service.answer(current, history)

    assert repository.person_resolution_calls[0] == (RELEASE_ID, current)
    assert set(repository.person_resolution_calls[1:]) == {
        (RELEASE_ID, switch),
        (RELEASE_ID, first),
    }
    assert len(repository.person_resolution_calls) == 3
    plan = repository.recommendation_calls[0]
    assert plan.excluded_movie_ids == ("chow-a", "chow-b")
    assert plan.context_text == f"{first}\n{switch}\n{current}"
    assert plan.person_name == "周星馳"


@pytest.mark.parametrize(
    ("previous", "current"),
    (
        ("Stephen Chow movies", "Stephen Chow movies, no duplicates"),
        ("show me Stephen Chow movies", "周星馳電影，不要重複"),
        ("周星馳電影", "周星驰电影，不要重复"),
    ),
)
def test_successful_bare_person_catalog_is_replayable_recommendation_history(
    previous: str,
    current: str,
) -> None:
    history = (ConversationExchange(previous, "第一輪。", ("chow-a", "chow-b")),)
    service, repository, _, _ = _service([], "must not be used", ())
    exact_names = ("周星馳", "周星驰")
    repository.person_resolutions[previous] = ("周星馳", "actor", exact_names)
    repository.person_resolutions[current] = ("周星馳", "actor", exact_names)

    service.answer(current, history)

    plan = repository.recommendation_calls[0]
    assert plan.person_name == "周星馳"
    assert plan.excluded_movie_ids == ("chow-a", "chow-b")
    assert plan.context_text == f"{previous}\n{current}"


@pytest.mark.parametrize(
    "reset_question",
    ("那成龍呢", "成龍"),
    ids=("different-lexical-reset", "terse-reset"),
)
def test_different_person_reset_replay_clears_old_filters(
    reset_question: str,
) -> None:
    """A successful different-person turn becomes the sole replay boundary."""
    first = "推薦3部1980年代S級周星馳主演的喜劇電影"
    current = "再推薦3部，不要重複"
    history = (
        ConversationExchange(first, "第一輪。", ("chow-a", "chow-b")),
        ConversationExchange(reset_question, "第二輪。", ("chan-a", "chan-b")),
    )
    service, repository, _, _ = _service([], "must not be used", ())
    repository.person_resolutions[first] = ("周星馳", "actor")
    repository.person_resolutions[reset_question] = ("成龍", "actor")

    service.answer(current, history)

    plan = repository.recommendation_calls[0]
    assert (plan.person_name, plan.person_role) == ("成龍", "actor")
    assert plan.genres == ()
    assert plan.tier is None
    assert (plan.year_from, plan.year_to) == (None, None)
    assert plan.excluded_movie_ids == ("chan-a", "chan-b")
    assert plan.context_text == f"{reset_question}\n{current}"


@pytest.mark.parametrize(
    "detail_question",
    (
        "它好看嗎？",
        "它講什麼？",
        "它值得看嗎？",
        "你剛才推薦的醉拳好看嗎？",
        "你剛才推薦的醉拳值得看嗎？",
        "你剛才推薦的醉拳有哪些演員？",
        "你剛才推薦的醉拳攝影風格如何？",
    ),
)
def test_referential_movie_detail_does_not_replace_recommendation_chain(
    detail_question: str,
) -> None:
    """A detail question can reference a card without becoming a new recommendation."""
    recommendation = "推薦1部周星馳主演的電影"
    current = "還有別的嗎？"
    history = (
        ConversationExchange(recommendation, "第一輪。", ("old-chow",)),
        ConversationExchange(detail_question, "電影詳情。", ("old-chow",)),
    )
    service, repository, _, _ = _service([], "must not be used", ())
    repository.person_resolutions[recommendation] = ("周星馳", "actor")

    service.answer(current, history)

    assert (RELEASE_ID, detail_question) not in repository.person_resolution_calls
    plan = repository.recommendation_calls[0]
    assert (plan.person_name, plan.person_role) == ("周星馳", "actor")
    assert plan.excluded_movie_ids == ("old-chow",)
    assert plan.context_text == f"{recommendation}\n{current}"


@pytest.mark.parametrize(
    ("question", "canonical_name"),
    (
        ("哪些電影是周星馳主演", "周星馳"),
        ("show me movies starring Stephen Chow", "周星馳"),
        ("劉的之出演過哪些電影", "劉的之"),
    ),
)
def test_natural_actor_catalog_queries_use_exact_structured_retrieval(
    question: str,
    canonical_name: str,
) -> None:
    """Natural catalog grammar must never degrade to corpus-wide vector search."""
    service, repository, _, _ = _service([], "must not be used", ())
    repository.person_resolutions[question] = (canonical_name, "actor")

    service.answer(question)

    assert repository.person_resolution_calls == [(RELEASE_ID, question)]
    assert len(repository.recommendation_calls) == 1
    plan = repository.recommendation_calls[0]
    assert (plan.person_name, plan.person_role) == (canonical_name, "actor")
    assert repository.calls == []


def test_exact_title_plus_unsupported_english_person_catalog_fails_closed() -> None:
    """Exact-title ownership must not erase an English person-catalog clause."""
    question = "Tell me about Drunken Master and show me movies starring Stephen Chow"
    passage = _passage(
        "metadata:drunken-master-english-mixed",
        "1978_ZQ_001",
        chinese_title="醉拳",
        english_title="Drunken Master",
    )
    service, repository, embedder, generator = _service(
        [passage], "must not be used", ()
    )
    repository.person_resolutions[question] = ("周星馳", "actor")

    result = service.answer(question)

    assert result == ChatAnswer(
        answer_markdown="目前不支援在同一問題中同時指定電影與人物片單；請分開提問。",
        citations=(),
        movies=(),
    )
    assert repository.recommendation_calls == []
    assert repository.calls == []
    assert embedder.calls == []
    assert generator.calls == []


@pytest.mark.parametrize("question", ("講下醉拳", "聊聊醉拳"))
def test_conversational_lead_in_resolves_the_exact_title(question: str) -> None:
    """Short conversational lead-ins still bind the governed movie title."""
    passage = _passage(
        "metadata:drunken-master-chat",
        "1978_ZQ_001",
        chinese_title="醉拳",
        english_title="Drunken Master",
    )
    service, repository, _, _ = _service(
        [passage],
        "《醉拳》是本次精確片名結果。[metadata:drunken-master-chat]",
        ("metadata:drunken-master-chat",),
    )

    result = service.answer(question)

    assert repository.explicit_target_calls == [(RELEASE_ID, question)]
    assert repository.calls[0]["target_movie_ids"] == ("1978_ZQ_001",)
    assert [movie.movie_id for movie in result.movies] == ["1978_ZQ_001"]


@pytest.mark.parametrize(
    "question",
    (
        "which films feature Stephen Chow",
        "show me Stephen Chow movies",
        "Stephen Chow movies",
        "周星馳電影",
    ),
)
def test_additional_person_catalog_queries_use_structured_retrieval(
    question: str,
) -> None:
    service, repository, _, _ = _service([], "must not be used", ())
    repository.person_resolutions[question] = ("周星馳", "actor")

    service.answer(question)

    assert repository.person_resolution_calls == [(RELEASE_ID, question)]
    assert len(repository.recommendation_calls) == 1
    assert repository.recommendation_calls[0].person_name == "周星馳"
    assert repository.calls == []


@pytest.mark.parametrize(
    ("question", "canonical_name", "role"),
    (
        ("movies starring Stephen Chow from the 1990s", "周星馳", "actor"),
        ("films directed by Wong Kar Wai in the 1990s", "王家衛", "director"),
        ("movies with Stephen Chow from the 1990s", "周星馳", "actor"),
        ("films featuring Stephen Chow in the 1990s", "周星馳", "actor"),
        ("movies starring 周星馳", "周星馳", "actor"),
        ("films directed by 王家衛", "王家衛", "director"),
        ("movies with 周星馳", "周星馳", "actor"),
        ("films featuring 成龍", "成龍", "actor"),
        ("show me movies starring 中井貴一", "中井貴一", "actor"),
    ),
)
def test_person_catalog_suffixes_and_mixed_scripts_use_structured_retrieval(
    question: str,
    canonical_name: str,
    role: str,
) -> None:
    service, repository, _, _ = _service([], "must not be used", ())
    repository.person_resolutions[question] = (canonical_name, role)

    service.answer(question)

    assert repository.person_resolution_calls == [(RELEASE_ID, question)]
    assert len(repository.recommendation_calls) == 1
    plan = repository.recommendation_calls[0]
    assert (plan.person_name, plan.person_role) == (canonical_name, role)
    assert repository.calls == []


@pytest.mark.parametrize(
    "question",
    (
        "movies starring Stephen Chow and Jackie Chan",
        "movies directed by John Woo and Wong Kar Wai",
    ),
)
def test_coordinated_english_people_fail_closed_before_any_downstream_call(
    question: str,
) -> None:
    service, repository, embedder, generator = _service([], "must not be used", ())

    result = service.answer(question)

    assert result.answer_markdown == (
        "目前一次只支援一位正向人物；請只指定一位導演或演員後再試。"
    )
    assert repository.person_resolution_calls == []
    assert repository.recommendation_calls == []
    assert repository.calls == []
    assert embedder.calls == []
    assert generator.calls == []


@pytest.mark.parametrize(
    ("question", "canonical_name"),
    (("中井貴一電影", "中井貴一"), ("千葉真一電影", "千葉真一")),
)
def test_numeric_glyph_release_names_use_structured_person_retrieval(
    question: str,
    canonical_name: str,
) -> None:
    service, repository, _, _ = _service([], "must not be used", ())
    repository.person_resolutions[question] = (canonical_name, "actor")

    service.answer(question)

    assert repository.person_resolution_calls == [(RELEASE_ID, question)]
    assert len(repository.recommendation_calls) == 1
    assert repository.recommendation_calls[0].person_name == canonical_name
    assert repository.calls == []


@pytest.mark.parametrize(
    "question",
    (
        "Tell me about Drunken Master and show me Stephen Chow movies",
        "Compare Drunken Master with Stephen Chow movies",
        "Tell me about Drunken Master and other Stephen Chow movies",
        "Compare Drunken Master with other Stephen Chow movies",
        "比較《醉拳》和周星馳電影",
    ),
)
def test_additional_exact_title_person_catalog_shapes_fail_closed(
    question: str,
) -> None:
    passage = _passage(
        "metadata:drunken-master-additional-mixed",
        "1978_ZQ_001",
        chinese_title="醉拳",
        english_title="Drunken Master",
    )
    service, repository, embedder, generator = _service(
        [passage], "must not be used", ()
    )

    result = service.answer(question)

    assert result == ChatAnswer(
        answer_markdown="目前不支援在同一問題中同時指定電影與人物片單；請分開提問。",
        citations=(),
        movies=(),
    )
    assert repository.person_resolution_calls == []
    assert repository.recommendation_calls == []
    assert repository.calls == []
    assert embedder.calls == []
    assert generator.calls == []


@pytest.mark.parametrize(
    "question",
    (
        "說說醉拳這部電影",
        "介绍一下醉拳这部电影",
        "談談醉拳該部電影",
        "聊聊醉拳该部影片",
    ),
)
def test_exact_title_deictic_movie_residual_stays_title_owned(
    question: str,
) -> None:
    passage = _passage(
        "metadata:drunken-master-deictic",
        "1978_ZQ_001",
        chinese_title="醉拳",
        english_title="Drunken Master",
    )
    service, repository, _, _ = _service(
        [passage],
        "《醉拳》是這次問題指定的電影。[metadata:drunken-master-deictic]",
        ("metadata:drunken-master-deictic",),
    )

    result = service.answer(question)

    assert repository.explicit_target_calls == [(RELEASE_ID, question)]
    assert repository.person_resolution_calls == []
    assert repository.recommendation_calls == []
    assert repository.calls[0]["target_movie_ids"] == ("1978_ZQ_001",)
    assert result.answer_markdown != (
        "目前不支援在同一問題中同時指定電影與人物片單；請分開提問。"
    )
    assert [citation.citation_id for citation in result.citations] == [
        "metadata:drunken-master-deictic"
    ]
    assert [movie.movie_id for movie in result.movies] == ["1978_ZQ_001"]


@pytest.mark.parametrize(
    "question",
    (
        "Compare Drunken Master with martial arts movies",
        "Compare Drunken Master with award winning movies",
        "比較《醉拳》和新浪潮電影",
    ),
)
def test_exact_title_plus_tentative_catalog_never_drops_the_second_scope(
    question: str,
) -> None:
    passage = _passage(
        "metadata:drunken-master-mixed-catalog",
        "1978_ZQ_001",
        chinese_title="醉拳",
        english_title="Drunken Master",
    )
    service, repository, embedder, generator = _service(
        [passage], "must not be used", ()
    )

    result = service.answer(question)

    assert result == ChatAnswer(
        answer_markdown="目前不支援在同一問題中同時指定電影與人物片單；請分開提問。",
        citations=(),
        movies=(),
    )
    assert repository.person_resolution_calls == []
    assert repository.recommendation_calls == []
    assert repository.calls == []
    assert embedder.calls == []
    assert generator.calls == []


@pytest.mark.parametrize(
    "current",
    (
        "還有周星馳電影嗎",
        "再來些周星馳電影",
        "其他周星馳電影",
        "周星馳還有什麼電影",
    ),
)
def test_unresolved_cjk_person_catalog_continuation_never_revives_history(
    current: str,
) -> None:
    previous = "推薦3部王家衛導演的愛情電影"
    history = (ConversationExchange(previous, "第一輪。", ("old-wong",)),)
    service, repository, embedder, generator = _service([], "must not be used", ())
    repository.person_resolutions[previous] = ("王家衛", "director")

    result = service.answer(current, history)

    assert "人物" in result.answer_markdown
    assert "精確解析" in result.answer_markdown
    assert repository.person_resolution_calls == [(RELEASE_ID, current)]
    assert repository.recommendation_calls == []
    assert repository.calls == []
    assert embedder.calls == []
    assert generator.calls == []


@pytest.mark.parametrize(
    "question",
    (
        "Hong Kong movies",
        "Martial arts movies",
        "新浪潮電影",
        "好看的電影",
        "show me Award Winning movies",
        "show me Family Friendly movies",
        "show me New Wave movies",
        "movies with martial arts",
        "films featuring martial arts",
        "movies with award winning performances",
        "movies with Hong Kong settings",
    ),
)
def test_tentative_person_catalog_miss_falls_back_to_movie_catalog(
    question: str,
) -> None:
    service, repository, _, _ = _service([], "must not be used", ())

    result = service.answer(question)

    assert "無法在目前 Release 精確解析指定的人物" not in result.answer_markdown
    assert repository.person_resolution_calls == [(RELEASE_ID, question)]
    assert len(repository.recommendation_calls) == 1
    assert repository.recommendation_calls[0].person_name is None
    assert repository.calls == []


@pytest.mark.parametrize(
    "detail_question",
    ("它有哪些演員？", "它誰主演？", "它的導演是誰？"),
)
def test_referential_credit_detail_cannot_replace_recommendation_chain(
    detail_question: str,
) -> None:
    recommendation = "推薦1部周星馳主演的電影"
    current = "還有別的嗎？"
    history = (
        ConversationExchange(recommendation, "第一輪。", ("old-chow",)),
        ConversationExchange(detail_question, "電影詳情。", ("old-chow",)),
    )
    service, repository, _, _ = _service([], "must not be used", ())
    repository.person_resolutions[recommendation] = ("周星馳", "actor")

    service.answer(current, history)

    assert (RELEASE_ID, detail_question) not in repository.person_resolution_calls
    plan = repository.recommendation_calls[0]
    assert (plan.person_name, plan.person_role) == ("周星馳", "actor")
    assert plan.excluded_movie_ids == ("old-chow",)
    assert plan.context_text == f"{recommendation}\n{current}"


def test_valid_person_reset_clears_older_dedup_ambiguity() -> None:
    ambiguous = "推薦周星馳或成龍的電影，不要重複"
    reset = "推薦3部成龍主演的電影"
    current = "再推薦3部，不要重複"
    history = (
        ConversationExchange(ambiguous, "第一輪。", ("ambiguous-card",)),
        ConversationExchange(reset, "第二輪。", ("chan-a", "chan-b")),
    )
    service, repository, _, _ = _service([], "must not be used", ())
    repository.ambiguous_person_questions.add(ambiguous)
    repository.person_resolutions[reset] = ("成龍", "actor")

    service.answer(current, history)

    plan = repository.recommendation_calls[0]
    assert (plan.person_name, plan.person_role) == ("成龍", "actor")
    assert plan.excluded_movie_ids == ("chan-a", "chan-b")
    assert plan.context_text == f"{reset}\n{current}"


@pytest.mark.parametrize(
    "question",
    (
        "movies with Stephen Chow and Jackie Chan",
        "films featuring Stephen Chow and Jackie Chan",
    ),
)
def test_tentative_coordinated_people_use_release_ambiguity_before_clarifying(
    question: str,
) -> None:
    service, repository, embedder, generator = _service([], "must not be used", ())
    repository.ambiguous_person_questions.add(question)

    result = service.answer(question)

    assert result == ChatAnswer(
        answer_markdown="目前一次只支援一位正向人物；請只指定一位導演或演員後再試。",
        citations=(),
        movies=(),
    )
    assert repository.person_resolution_calls == [(RELEASE_ID, question)]
    assert repository.recommendation_calls == []
    assert repository.calls == []
    assert embedder.calls == []
    assert generator.calls == []


@pytest.mark.parametrize(
    "question",
    (
        "movies with Hong Kong and martial arts",
        "movies with martial arts and award winning performances",
        "movies with Hong Kong settings and award winning performances",
        "movies featuring martial arts and family friendly stories",
    ),
)
def test_tentative_coordinated_descriptors_fall_back_after_one_release_miss(
    question: str,
) -> None:
    service, repository, _, _ = _service([], "must not be used", ())

    result = service.answer(question)

    assert "一次只支援一位" not in result.answer_markdown
    assert "無法在目前 Release 精確解析指定的人物" not in result.answer_markdown
    assert repository.person_resolution_calls == [(RELEASE_ID, question)]
    assert len(repository.recommendation_calls) == 1
    assert repository.recommendation_calls[0].person_name is None
    assert repository.calls == []


@pytest.mark.parametrize(
    "question",
    (
        "還有周星馳電影嗎",
        "再推薦周星馳電影",
        "更多周星馳電影",
        "別的周星馳電影",
        "換成周星馳電影",
        "換一部周星馳電影",
        "還有周星馳主演的電影嗎",
        "再推薦周星馳主演的電影",
    ),
)
def test_unresolved_current_person_shell_fails_before_history_or_retrieval(
    question: str,
) -> None:
    previous = "推薦3部王家衛導演的電影"
    history = (
        ConversationExchange(previous, "上一輪。", ("old-a", "old-b", "old-c")),
    )
    service, repository, embedder, generator = _service([], "must not be used", ())
    repository.person_resolutions[previous] = ("王家衛", "director")

    result = service.answer(question, history)

    assert "無法在目前 Release 精確解析指定的" in result.answer_markdown
    assert repository.person_resolution_calls == [(RELEASE_ID, question)]
    assert repository.recommendation_calls == []
    assert repository.deep_recommendation_calls == []
    assert repository.calls == []
    assert embedder.calls == []
    assert generator.calls == []


@pytest.mark.parametrize(
    ("fresh_catalog_question", "expected_genres"),
    (
        ("好看的電影", ()),
        ("新浪潮電影", ()),
        ("最近的電影", ()),
        ("高分冷門電影", ()),
        ("動作電影", ("動作",)),
        ("功夫電影", ("功夫",)),
        ("家庭電影", ("家庭",)),
        ("香港電影", ()),
        ("經典電影", ()),
        ("action movies", ("動作",)),
        ("Hong Kong movies", ()),
        ("martial arts movies", ()),
        ("family friendly movies", ("家庭",)),
        ("award winning movies", ()),
        ("new wave movies", ()),
        ("crime thriller movies", ("犯罪", "驚悚")),
    ),
)
@pytest.mark.parametrize(
    "generic_movie_ids",
    (("generic-a",), ("generic-a", "generic-b", "generic-c")),
)
def test_successful_fresh_catalog_history_resets_older_person_authority(
    fresh_catalog_question: str,
    expected_genres: tuple[str, ...],
    generic_movie_ids: tuple[str, ...],
) -> None:
    old_person = "推薦3部王家衛導演的愛情電影"
    current = "再推薦3部，不要重複"
    history = (
        ConversationExchange(old_person, "第一輪。", ("wong-a", "wong-b", "wong-c")),
        ConversationExchange(
            fresh_catalog_question,
            "第二輪。",
            generic_movie_ids,
        ),
    )
    service, repository, _, _ = _service([], "must not be used", ())
    repository.person_resolutions[old_person] = ("王家衛", "director")

    service.answer(current, history)

    assert len(repository.recommendation_calls) == 1
    plan = repository.recommendation_calls[0]
    assert (plan.person_name, plan.person_role) == (None, None)
    assert plan.genres == expected_genres
    assert plan.excluded_movie_ids == generic_movie_ids
    assert plan.context_text == f"{fresh_catalog_question}\n{current}"


@pytest.mark.parametrize(
    "question",
    (
        "more yamada taro movies",
        "other yamada taro movies",
        "show me more yamada taro movies",
    ),
)
def test_unresolved_lowercase_current_person_fails_before_history_or_retrieval(
    question: str,
) -> None:
    previous = "recommend 3 movies directed by Wong Kar Wai"
    history = (ConversationExchange(previous, "Previous.", ("old-wong",)),)
    service, repository, embedder, generator = _service([], "must not be used", ())
    repository.person_resolutions[previous] = ("Wong Kar Wai", "director")

    result = service.answer(question, history)

    assert "無法在目前 Release 精確解析指定的人物" in result.answer_markdown
    assert repository.person_resolution_calls == [(RELEASE_ID, question)]
    assert repository.recommendation_calls == []
    assert repository.deep_recommendation_calls == []
    assert repository.calls == []
    assert embedder.calls == []
    assert generator.calls == []


@pytest.mark.parametrize(
    ("question", "expected_role", "expected_from", "expected_to"),
    (
        ("movies starring Stephen Chow after 1990", "actor", 1990, None),
        ("movies starring Stephen Chow since 1990", "actor", 1990, None),
        ("movies starring Stephen Chow before 2000", "actor", None, 2000),
        ("films directed by Wong Kar Wai after 1990", "director", 1990, None),
        ("movies with Stephen Chow after 1990", None, 1990, None),
        ("films featuring Stephen Chow before 2000", None, None, 2000),
        (
            "movies starring Stephen Chow between 1990 and 2000",
            "actor",
            1990,
            2000,
        ),
    ),
)
def test_person_catalog_with_year_suffix_never_falls_to_generic_vector_search(
    question: str,
    expected_role: str | None,
    expected_from: int | None,
    expected_to: int | None,
) -> None:
    person_name = "Wong Kar Wai" if "Wong Kar Wai" in question else "Stephen Chow"
    service, repository, _, _ = _service([], "must not be used", ())
    repository.person_resolutions[question] = (person_name, expected_role)

    service.answer(question)

    assert repository.person_resolution_calls == [(RELEASE_ID, question)]
    assert len(repository.recommendation_calls) == 1
    plan = repository.recommendation_calls[0]
    assert (plan.person_name, plan.person_role) == (person_name, expected_role)
    assert (plan.year_from, plan.year_to) == (expected_from, expected_to)
    assert repository.calls == []


@pytest.mark.parametrize(
    ("question", "expected_count"),
    (
        ("別的電影還有嗎？", 3),
        ("別的呢？", 3),
        ("有別的嗎？", 3),
        ("有其他的嗎？", 3),
        ("給我別的電影", 3),
        ("來點別的", 3),
        ("換別的", 3),
        ("更多", 3),
        ("更多電影", 3),
        ("另一部", 1),
        ("另一些", 3),
        ("再推薦三部，別的也可以", 3),
    ),
)
def test_generic_cjk_continuation_shell_reaches_structured_history(
    question: str,
    expected_count: int,
) -> None:
    passages = [
        _passage(
            f"metadata:generic-shell-{index}",
            f"generic-shell-{index}",
            chinese_title=f"後續喜劇{index}",
            english_title=f"Continuation Comedy {index}",
            genre="喜劇",
        )
        for index in range(1, expected_count + 1)
    ]
    service, repository, _, _ = _service(
        passages,
        "\n".join(
            f"《後續喜劇{index}》：後續選擇。[metadata:generic-shell-{index}]"
            for index in range(1, expected_count + 1)
        ),
        tuple(
            f"metadata:generic-shell-{index}"
            for index in range(1, expected_count + 1)
        ),
    )
    history = (
        ConversationExchange(
            "推薦3部喜劇",
            "第一輪。",
            ("old-a", "old-b", "old-c"),
        ),
    )

    result = service.answer(question, history)

    assert len(result.movies) == expected_count
    assert len(repository.recommendation_calls) == 1
    plan = repository.recommendation_calls[0]
    assert plan.requested_count == expected_count
    assert plan.continuation is True
    assert plan.genres == ("喜劇",)
    assert plan.excluded_movie_ids == ("old-a", "old-b", "old-c")
    assert repository.calls == []


@pytest.mark.parametrize(
    "current",
    (
        "還有周星馳電影嗎",
        "再推薦周星馳電影",
        "更多周星馳電影",
        "還有周星馳主演的電影嗎",
        "more Stephen Chow movies",
        "show me more movies starring Stephen Chow",
        "other Stephen Chow movies",
    ),
)
def test_resolved_same_person_semantic_continuation_preserves_active_chain(
    current: str,
) -> None:
    previous = "推薦3部周星馳主演的喜劇電影"
    history = (
        ConversationExchange(previous, "第一輪。", ("old-a", "old-b")),
    )
    service, repository, _, _ = _service([], "must not be used", ())
    exact_names = ("周星馳", "周星驰", "Stephen Chow")
    repository.person_resolutions[current] = (
        "周星馳",
        "actor",
        exact_names,
    )
    repository.person_resolutions[previous] = (
        "周星馳",
        "actor",
        exact_names,
    )

    service.answer(current, history)

    assert repository.person_resolution_calls == [
        (RELEASE_ID, current),
        (RELEASE_ID, previous),
    ]
    plan = repository.recommendation_calls[0]
    assert (plan.person_name, plan.person_role) == ("周星馳", "actor")
    assert plan.genres == ("喜劇",)
    assert plan.continuation is True
    assert plan.excluded_movie_ids == ("old-a", "old-b")
    assert plan.context_text == f"{previous}\n{current}"


@pytest.mark.parametrize(
    "current",
    (
        "還有周星馳電影嗎",
        "再推薦周星馳主演的電影",
        "more Stephen Chow movies",
        "show me more movies starring Stephen Chow",
    ),
)
def test_resolved_different_person_semantic_continuation_resets_active_chain(
    current: str,
) -> None:
    previous = "推薦3部王家衛導演的愛情電影"
    history = (
        ConversationExchange(previous, "第一輪。", ("old-a", "old-b")),
    )
    service, repository, _, _ = _service([], "must not be used", ())
    repository.person_resolutions[current] = ("周星馳", "actor")
    repository.person_resolutions[previous] = ("王家衛", "director")

    service.answer(current, history)

    assert repository.person_resolution_calls == [
        (RELEASE_ID, current),
        (RELEASE_ID, previous),
    ]
    plan = repository.recommendation_calls[0]
    assert (plan.person_name, plan.person_role) == ("周星馳", "actor")
    assert plan.genres == ()
    assert plan.continuation is False
    assert plan.excluded_movie_ids == ()
    assert plan.context_text == current
