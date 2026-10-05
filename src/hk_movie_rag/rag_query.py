"""Release-scoped retrieval, grounded generation, and citation validation."""

from __future__ import annotations

import math
import re
from collections.abc import Collection, Mapping, Sequence
from dataclasses import dataclass, replace
from typing import Literal, Protocol
from urllib.parse import quote

from .identity_security import (
    contains_adversarial_identity,
    has_forbidden_identity_controls,
)
from .rag_db import RagDatabaseError
from .relevance import (
    RelevancePolicyError,
    has_static_movie_domain_signal,
    has_strong_out_of_domain_signal,
    load_general_relevance_policy,
)
from .retrieval import (
    TITLE_DELIMITERS,
    AmbiguousPersonResolutionError,
    BoundedAnalysisPlan,
    ConversationExchange,
    ExplicitMovieResolution,
    PersonQueryShape,
    PersonRole,
    RecommendationPlan,
    RecommendationSearch,
    ResolvedPerson,
    _effective_recommendation_constraints,
    _is_bare_person_history_shape,
    _is_bounded_movie_detail_history_question,
    _latest_successful_recommendation_chain,
    _merged_recommendation_constraints,
    _question_constraints,
    _recommendation_exclusion_context_chain,
    _semantic_question_constraints,
    controlled_credit_group,
    controlled_title_policy,
    deduplication_requires_successful_history,
    explicit_movie_id_intent_position,
    explicit_title_intent_position,
    has_deep_analysis_intent,
    has_explicit_nonmovie_object_term,
    has_explicit_person_catalog_intent,
    has_multiple_person_catalog_intent,
    has_person_catalog_continuation_shell,
    has_recommendation_deduplication_request,
    has_tentative_person_catalog_intent,
    has_unmodeled_negative_recommendation_condition,
    has_unsupported_broad_analysis_intent,
    has_unsupported_negative_recommendation_filter,
    normalize_query_text,
    parse_person_query_shape,
    plan_bounded_analysis,
    plan_recommendation,
    resolve_explicit_movie_identities,
)

_MAX_QUESTION_CHARACTERS = 1000
_MAX_PASSAGES = 8
_MAX_HISTORY_CHARACTERS = 12_000
_DOMAIN_REJECTION = "我目前只能回答这个 Release 内的香港电影问题。"
_UNSUPPORTED_BROAD_ANALYSIS = (
    "這個問題需要結構化欄位以外的分析證據；目前 Release "
    "不足以可靠判斷這項條件。請改問具體片名，或使用年代、類型、"
    "導演、演員等已治理欄位。"
)
_PERSON_SELECTION_CLARIFICATION = (
    "目前一次只支援一位正向人物；請只指定一位導演或演員後再試。"
)
_PERSON_ROLE_CONFLICT_CLARIFICATION = (
    "目前不支援同時指定演員與導演角色；請只指定導演或演員後再試。"
)
_UNSUPPORTED_NEGATIVE_FILTER_CLARIFICATION = (
    "目前不支援「不要／排除」類型、級別或年份條件；"
    "請改為指定想看的正向條件。"
)
_MISSING_RECOMMENDATION_HISTORY_CLARIFICATION = (
    "找不到可延續的成功推薦；請先提出一個正向電影推薦問題。"
)
_UNMODELED_NEGATIVE_CONDITION_CLARIFICATION = (
    "目前不支援「不要／排除」人物或其他負向推薦條件；"
    "請改為指定想看的正向人物與條件。"
)
_UNSUPPORTED_PERSON_SCOPE_CLARIFICATION = (
    "目前不支援同時指定一位人物與受控人物群組；請只保留其中一項條件。"
)
_MIXED_MOVIE_PERSON_SCOPE_CLARIFICATION = (
    "目前不支援在同一問題中同時指定電影與人物片單；請分開提問。"
)
_MAX_RECOMMENDATION_REASON_CHARACTERS = 240
_CJK_INTERCHAR_WHITESPACE = re.compile(
    r"(?<=[\u3400-\u9fff])\s+(?=[\u3400-\u9fff])"
)
_CHINESE_ORDINAL_PATTERN = re.compile(
    r"第(?P<ordinal>[一二三四五六七八1-8])\s*(?:部|套|齣|出|個|个)"
)
_ENGLISH_ORDINAL_PATTERN = re.compile(
    r"(?i)\b(?P<ordinal>first|second|third|fourth|fifth|sixth|seventh|eighth)"
    r"(?:\s+(?:one|movie|film))?\b"
)
_ENGLISH_PRONOUN_PATTERN = re.compile(
    r"(?i)\b(?:it|its|this one|that one|this movie|that movie|this film|that film)\b"
)
_PERSON_NAME_TEXT = r"[\u3400-\u9fff·]{2,4}?"
_PERSON_RECOMMENDATION_PREFIX = (
    r"(?:推薦|推介|建議|列出|換成)\s*(?:\d{1,3}\s*部\s*)?"
)
_PERSON_RELATIONSHIP_PATTERNS: tuple[tuple[re.Pattern[str], PersonRole | None], ...] = (
    (
        re.compile(
            _PERSON_RECOMMENDATION_PREFIX
            + rf"(?:演員|主演)\s*(?P<name>{_PERSON_NAME_TEXT})\s*的?\s*"
            r"(?:電影|影片|港片|作品)"
        ),
        "actor",
    ),
    (
        re.compile(
            _PERSON_RECOMMENDATION_PREFIX
            + rf"(?:導演)\s*(?P<name>{_PERSON_NAME_TEXT})\s*的?\s*"
            r"(?:電影|影片|港片|作品)"
        ),
        "director",
    ),
    (
        re.compile(
            _PERSON_RECOMMENDATION_PREFIX
            + rf"(?P<name>{_PERSON_NAME_TEXT})\s*"
            r"(?:主演|演出|出演過|參演過|演過|出演|參演)\s*的?\s*"
            r"(?:電影|影片|港片|作品)"
        ),
        "actor",
    ),
    (
        re.compile(
            _PERSON_RECOMMENDATION_PREFIX
            + rf"(?P<name>{_PERSON_NAME_TEXT})\s*(?:導演|執導)\s*的?\s*"
            r"(?:電影|影片|港片|作品)"
        ),
        "director",
    ),
    (
        re.compile(
            _PERSON_RECOMMENDATION_PREFIX
            + rf"(?P<name>{_PERSON_NAME_TEXT})\s*的\s*"
            r"(?:好|經典|優秀|出色)?\s*(?:電影|影片|港片)"
        ),
        None,
    ),
    (
        re.compile(
            rf"(?:有|找)\s*(?P<name>{_PERSON_NAME_TEXT})\s*的\s*"
            r"(?:電影|影片|港片|作品).{0,8}(?:推薦|推介)"
        ),
        None,
    ),
)
_NON_PERSON_POSSESSIVE_QUALIFIERS = frozenset(
    {
        "香港",
        "中國",
        "中國內地",
        "內地",
        "大陸",
        "台灣",
        "澳門",
        "韓國",
        "日本",
        "新加坡",
        "馬來西亞",
    }
)
_NON_PERSON_NAME_FRAGMENTS = (
    "演員",
    "主演",
    "導演",
    "出演",
    "參演",
    "演過",
    "陣容",
    "作品",
    "評分",
    "評價",
    "高分",
    "奧斯卡",
    "金像",
    "金馬",
    "年代",
    "獲獎",
    "口碑",
    "票房",
    "類型",
    "風格",
    "題材",
    "內涵",
    "古裝",
    "田園",
    "方言",
    "白話",
    "演技",
    "功力",
    "表現",
    "有沒有",
    "好歌",
)
_ENGLISH_PERSON_RECOMMENDATION_PATTERNS = (
    re.compile(
        r"(?<![A-Za-z])(?:recommend|suggest|find|show)(?:\s+me)?\s+"
        r"(?:\d+\s+)?"
        r"(?P<name>[A-Z][A-Za-z'’.-]+(?:\s+[A-Z][A-Za-z'’.-]+){1,3})\s+"
        r"(?:movies|films)\b"
    ),
    re.compile(
        r"\b(?:movies|films)\s+(?:by|with|starring)\s+"
        r"(?P<name>[A-Z][A-Za-z'’.-]+(?:\s+[A-Z][A-Za-z'’.-]+){1,3})\b"
    ),
)
_ENGLISH_NON_PERSON_QUALIFIERS = frozenset(
    {
        "action comedy",
        "hong kong",
        "martial arts",
        "new hong kong",
        "romantic comedy",
    }
)
_COMMON_CHINESE_SURNAME_INITIALS = frozenset(
    "陳林黃張李王吳劉蔡楊許鄭謝郭洪曾邱廖賴徐周葉蘇莊呂江何蕭羅高潘"
    "簡朱鍾游彭詹胡施沈余盧梁趙顏柯翁魏孫戴范方宋鄧杜傅侯曹薛丁阮"
    "馬董唐卓藍馮姚石紀歐連古汪湯姜白田康鄒熊秦嚴尹毛龔史陶黎賀顧"
    "萬錢"
)
_CHINESE_HISTORY_DEMONSTRATIVE_PATTERN = re.compile(
    r"^\s*(?:(?:請問|请问|我想知道|想知道|那麼|那么|所以|那)\s*)?"
    r"(?:這部|这部|該片|该片)(?:電影|电影|影片)?"
)
_CHINESE_HISTORY_BARE_PREFIX = re.compile(
    r"^\s*(?:(?:請問|请问|我想知道|想知道|那麼|那么|所以|那|"
    r"可以告訴我|可以告诉我)\s*)?$"
)
_ENGLISH_HISTORY_DEMONSTRATIVE_PREFIX = re.compile(
    r"(?i)^(?:please\s+)?(?:what about|tell me about|can you tell me about|"
    r"could you tell me about|tell me who directed|who directed|who stars in|"
    r"what is|what was|how is|how was|is|was)?$"
)
_ORDINAL_INDEX = {
    "一": 0,
    "1": 0,
    "first": 0,
    "二": 1,
    "2": 1,
    "second": 1,
    "三": 2,
    "3": 2,
    "third": 2,
    "四": 3,
    "4": 3,
    "fourth": 3,
    "五": 4,
    "5": 4,
    "fifth": 4,
    "六": 5,
    "6": 5,
    "sixth": 5,
    "七": 6,
    "7": 6,
    "seventh": 6,
    "八": 7,
    "8": 7,
    "eighth": 7,
}
# This is a positive whitelist, not an attempt to enumerate every possible analytical wording.
# An exactly named movie without PDF evidence may ask only for these canonical metadata fields.
_METADATA_INTENT_ALIASES: tuple[tuple[str, tuple[str, ...]], ...] = (
    (
        "__overview__",
        (
            "basic information",
            "movie information",
            "film information",
            "tell me about",
            "what about",
            "metadata",
            "information",
            "details",
            "基本資料",
            "基本资料",
            "基本資訊",
            "基本信息",
            "電影資料",
            "电影资料",
            "資料",
            "资料",
            "資訊",
            "信息",
        ),
    ),
    ("movie_id", ("movie id", "movie_id", "電影編號", "电影编号")),
    (
        "english_title",
        ("english title", "english name", "英文片名", "英文名"),
    ),
    (
        "chinese_title",
        ("chinese title", "chinese name", "中文片名", "中文名", "片名", "title"),
    ),
    (
        "release_date",
        (
            "release date",
            "release year",
            "year released",
            "when released",
            "released",
            "release",
            "year",
            "上映日期",
            "上映年份",
            "上映時間",
            "上映时间",
            "上映",
            "年份",
            "哪年",
        ),
    ),
    (
        "production_region",
        (
            "production region",
            "country of origin",
            "region",
            "產地",
            "产地",
            "製作地區",
            "制作地区",
            "地區",
            "地区",
        ),
    ),
    ("director", ("directed", "director", "導演", "导演", "執導", "执导")),
    (
        "screenwriter",
        ("screenwriter", "writer", "編劇", "编剧"),
    ),
    (
        "cast",
        ("who stars", "actors", "actor", "cast", "stars", "主演", "演員", "演员", "卡司"),
    ),
    ("genre", ("movie genre", "film genre", "genre", "類型", "类型")),
    (
        "runtime_minutes",
        ("running time", "how long", "runtime", "duration", "片長", "片长", "時長", "时长"),
    ),
    (
        "production_company",
        ("production company", "studio", "製作公司", "制作公司", "出品公司"),
    ),
    ("data_source", ("data source", "資料來源", "资料来源")),
    ("language", ("language", "語言", "语言")),
    ("rating", ("rating", "評分", "评分")),
)
_SAFE_METADATA_ENGLISH = frozenset(
    {
        "a",
        "about",
        "and",
        "are",
        "compare",
        "comparison",
        "film",
        "for",
        "in",
        "is",
        "its",
        "me",
        "movie",
        "of",
        "please",
        "show",
        "tell",
        "the",
        "this",
        "was",
        "were",
        "what",
        "when",
        "which",
        "who",
        "with",
    }
)
_SAFE_METADATA_CHINESE = (
    "我想問一下",
    "我想问一下",
    "請問一下",
    "请问一下",
    "我想知道",
    "告訴我",
    "告诉我",
    "什麼時候",
    "什么时候",
    "哪一年",
    "有哪些",
    "請問",
    "请问",
    "這部",
    "这部",
    "該片",
    "该片",
    "相關",
    "相关",
    "比較",
    "比较",
    "列出",
    "提供",
    "顯示",
    "显示",
    "使用",
    "名單",
    "名单",
    "甚麼",
    "什么",
    "何時",
    "何时",
    "多久",
    "多少",
    "還有",
    "还有",
    "電影",
    "电影",
    "影片",
    "可以",
    "能否",
    "一部",
    "有誰",
    "有谁",
    "以及",
    "和",
    "及",
    "與",
    "与",
    "的",
    "是",
    "為",
    "为",
    "由",
    "誰",
    "谁",
    "哪",
    "幾",
    "几",
    "有",
    "叫",
    "呢",
    "嗎",
    "吗",
    "呀",
    "了",
)
_CANONICAL_METADATA_FIELDS = frozenset(
    {
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
        "language",
        "rating",
    }
)
_BRACKETED_TOKEN = re.compile(r"\[([^\[\]\r\n]+)\]")
_MARKDOWN_LINK = re.compile(r"\[[^\[\]\r\n]+\]\([^\r\n)]+\)")
_URI_SCHEME = re.compile(r"(?i)(?<![a-z0-9+.-])[a-z][a-z0-9+.-]{0,31}:(?=\S)")
_DOMAIN = re.compile(
    r"(?i)\b(?:www\.|(?:[a-z0-9](?:[a-z0-9-]{0,62})\.)+)"
    r"[a-z]{2,63}(?:[/?:#][^\s<>]*)?"
)
_PATH_REFERENCE = re.compile(
    r"(?:^|[\s:：,，;；!！?？(（\[【{<='\"“”])(?:\.\.?/|/)[^\s<>\[\]]+"
)
_HTML_LINK = re.compile(r"(?is)<\s*a\b[^>]*>|\b(?:href|src)\s*=")
class QueryValidationError(ValueError):
    """Raised when a user question is outside the bounded demo contract."""


class GroundingError(RuntimeError):
    """Raised when retrieval or generation cannot prove a grounded answer."""


@dataclass(frozen=True)
class GeneratedAnswer:
    """Strict output accepted from a generation adapter before grounding checks."""

    answer_markdown: str
    citation_ids: tuple[str, ...]


@dataclass(frozen=True)
class EvidencePassage:
    """One immutable repository passage supplied to the generator."""

    passage_id: str
    movie_id: str
    source_kind: Literal["movie_metadata", "pdf_page"]
    body: str
    page_number: int | None
    source_filename: str | None
    movie: Mapping[str, object]
    poster_available: bool
    distance: float | None = None


@dataclass(frozen=True)
class Citation:
    """Server-built citation; every field originates in a repository record."""

    citation_id: str
    movie_id: str
    movie_title: str
    source_kind: Literal["movie_metadata", "pdf_page"]
    page_number: int | None
    source_filename: str | None
    excerpt: str


@dataclass(frozen=True)
class MovieCard:
    """Deduplicated movie metadata and an optional same-origin poster route."""

    movie_id: str
    chinese_title: str
    english_title: str
    release_date: str
    director: str
    cast: str
    genre: str
    tier: str
    pilot_movie: bool
    poster_url: str | None


@dataclass(frozen=True)
class ChatAnswer:
    """Validated answer returned by the application service."""

    answer_markdown: str
    citations: tuple[Citation, ...]
    movies: tuple[MovieCard, ...]


@dataclass(frozen=True)
class _ExactTargetAuthority:
    """Server-owned evidence branches for an exactly resolved movie target."""

    canonical_fields: frozenset[str]
    metadata: tuple[EvidencePassage, ...]
    qualitative: tuple[EvidencePassage, ...]
    qualitative_question: str | None
    blocked: bool = False


@dataclass(frozen=True)
class _QualitativeSegmentCandidate:
    citation_id: str
    passage_order: int
    segment_order: int
    text: str
    score: int
    matched_terms: frozenset[str]
    covered_groups: frozenset[int]


class QueryRepository(Protocol):
    """Read boundary required by :class:`QueryService`."""

    def release_state(self, release_id: str) -> QueryReleaseState: ...

    def assert_query_ready(self, release_id: str) -> None: ...

    def resolve_explicit_movie_ids(
        self, release_id: str, question: str
    ) -> tuple[str, ...]: ...

    def resolve_explicit_movie_context(
        self, release_id: str, question: str
    ) -> ExplicitMovieResolution: ...

    def resolve_recommendation_person(
        self, release_id: str, question: str
    ) -> ResolvedPerson | None: ...

    def search(
        self,
        release_id: str,
        embedding: Sequence[float],
        limit: int = _MAX_PASSAGES,
        *,
        question: str | None = None,
        embedding_model: str | None = None,
        embedding_dimension: int | None = None,
        target_movie_ids: tuple[str, ...] = (),
    ) -> list[dict[str, object]]: ...

    def search_recommendations(
        self,
        release_id: str,
        embedding: Sequence[float],
        plan: RecommendationPlan,
    ) -> RecommendationSearch: ...

    def search_deep_recommendations(
        self,
        release_id: str,
        embedding: Sequence[float],
        plan: RecommendationPlan,
    ) -> RecommendationSearch: ...

    def get_approved_poster_assets(
        self, release_id: str, movie_ids: tuple[str, ...]
    ) -> Mapping[str, Mapping[str, object]]: ...


class QueryReleaseState(Protocol):
    """Minimum active-release contract needed before query embedding."""

    status: str
    embedding_model: str
    embedding_dimension: int
    manifest_sha256: str | None


class QueryEmbeddingClient(Protocol):
    """Query client identity must match the active stored-vector contract."""

    model: str
    dimension: int

    def embed_query(self, text: str) -> list[float]: ...


class GenerationClient(Protocol):
    """Grounded generation boundary implemented by the Vertex adapter."""

    def generate_grounded(
        self,
        question: str,
        passages: tuple[EvidencePassage, ...],
        *,
        mode: Literal["general", "recommendation", "deep_recommendation"],
        conversation_context: tuple[str, ...],
        required_citation_ids: tuple[str, ...],
    ) -> GeneratedAnswer: ...


class QueryService:
    """Answer bounded questions from one active RAG release."""

    def __init__(
        self,
        release_id: str,
        expected_manifest_sha256: str,
        repository: QueryRepository,
        embedding_client: QueryEmbeddingClient,
        generator: GenerationClient,
        *,
        expected_relevance_policy_sha256: str,
    ) -> None:
        if not release_id.strip():
            raise QueryValidationError("release ID must be non-empty")
        if re.fullmatch(r"[0-9a-f]{64}", expected_manifest_sha256) is None:
            raise QueryValidationError("release manifest SHA-256 is invalid")
        try:
            policy = load_general_relevance_policy(
                release_id, expected_relevance_policy_sha256
            )
            embedding_model, embedding_dimension = _validated_query_contract(
                embedding_client
            )
        except (RelevancePolicyError, GroundingError):
            raise GroundingError("relevance policy contract is invalid") from None
        if (
            policy.artifact_sha256 != expected_relevance_policy_sha256
            or policy.release_id != release_id
            or policy.embedding_model != embedding_model
            or policy.embedding_dimension != embedding_dimension
            or policy.distance_metric != "pgvector_cosine_distance"
        ):
            raise GroundingError("relevance policy contract is invalid")
        self.release_id = release_id
        self.expected_manifest_sha256 = expected_manifest_sha256
        self.repository = repository
        self.embedding_client = embedding_client
        self.generator = generator
        self.relevance_policy = policy

    def _latest_history_person_resolution(
        self,
        history: Sequence[ConversationExchange],
        *,
        authority_confirmed_continuations: frozenset[
            ConversationExchange
        ] = frozenset(),
        authority_confirmed_resets: frozenset[ConversationExchange] = frozenset(),
        resolved_history_people: Mapping[
            ConversationExchange, ResolvedPerson | None
        ]
        | None = None,
        ambiguous_history_exchanges: frozenset[
            ConversationExchange
        ] = frozenset(),
    ) -> tuple[ResolvedPerson | None, str | None]:
        """Resolve the newest valid person boundary in one successful chain."""
        chain = _latest_successful_recommendation_chain(
            history,
            authority_confirmed_continuations=authority_confirmed_continuations,
            authority_confirmed_resets=authority_confirmed_resets,
        )
        for index in range(len(chain) - 1, -1, -1):
            exchange = chain[index]
            history_question = exchange.question
            prefix = chain[:index]
            history_shape = parse_person_query_shape(history_question)
            if history_shape is not None and (
                len(history_shape.candidate_names) != 1
                or history_shape.exclusionary
                or history_shape.role_conflict
            ):
                raise AmbiguousPersonResolutionError
            authority_owned = bool(
                exchange in authority_confirmed_continuations
                or exchange in authority_confirmed_resets
            )
            history_plan = _deep_recommendation_plan(
                history_question,
                prefix,
                _authority_confirmed_continuations=authority_confirmed_continuations,
                _authority_confirmed_resets=authority_confirmed_resets,
            ) or plan_recommendation(
                history_question,
                prefix,
                _authority_confirmed_continuations=authority_confirmed_continuations,
                _authority_confirmed_resets=authority_confirmed_resets,
            )
            if history_plan is None and not authority_owned:
                continue
            if exchange in ambiguous_history_exchanges:
                raise AmbiguousPersonResolutionError
            resolved = (
                resolved_history_people[exchange]
                if resolved_history_people is not None
                and exchange in resolved_history_people
                else self.repository.resolve_recommendation_person(
                    self.release_id, history_question
                )
            )
            if resolved is not None:
                _validate_resolved_person(resolved)
                return resolved, None
            history_qualifier = (
                _person_role_label(history_shape.role)
                if history_shape is not None
                and len(history_shape.candidate_names) == 1
                and not history_shape.exclusionary
                and history_plan is not None
                and _plausible_person_name(
                    history_shape.candidate_names[0], history_plan
                )
                else (
                    _person_qualifier_label(history_question, history_plan)
                    if history_plan is not None
                    else None
                )
            )
            if history_qualifier is not None:
                return None, history_qualifier
        return None, None

    def _history_recommendation_authority(
        self,
        history: Sequence[ConversationExchange],
    ) -> tuple[
        frozenset[ConversationExchange],
        frozenset[ConversationExchange],
        bool,
        Mapping[ConversationExchange, ResolvedPerson | None],
        frozenset[ConversationExchange],
    ]:
        """Rebuild Release-backed continuation/reset decisions for successful turns."""
        continuations: set[ConversationExchange] = set()
        resets: set[ConversationExchange] = set()
        resolutions: dict[ConversationExchange, ResolvedPerson | None] = {}
        ambiguous_exchanges: set[ConversationExchange] = set()
        has_active_ambiguous_person_history = False
        for index, exchange in enumerate(history):
            requires_authority = _history_exchange_requires_person_authority(exchange)
            if not requires_authority and has_active_ambiguous_person_history:
                constraints = _question_constraints(exchange.question)
                shape = constraints.person_query_shape
                requires_authority = bool(
                    exchange.movie_ids
                    and not _is_bounded_movie_detail_history_question(
                        exchange.question
                    )
                    and (
                        (
                            shape is not None
                            and len(shape.candidate_names) == 1
                            and not shape.exclusionary
                            and not shape.role_conflict
                            and constraints.complete_current_person
                        )
                        or has_explicit_person_catalog_intent(exchange.question)
                        or has_tentative_person_catalog_intent(exchange.question)
                    )
                )
            if not requires_authority:
                continue
            prefix = tuple(history[:index])
            try:
                resolved = self.repository.resolve_recommendation_person(
                    self.release_id, exchange.question
                )
            except AmbiguousPersonResolutionError:
                resets.add(exchange)
                ambiguous_exchanges.add(exchange)
                has_active_ambiguous_person_history = True
                continue
            resolutions[exchange] = resolved
            active_chain = _latest_successful_recommendation_chain(
                prefix,
                authority_confirmed_continuations=frozenset(continuations),
                authority_confirmed_resets=frozenset(resets),
            )
            if not active_chain:
                resets.add(exchange)
                has_active_ambiguous_person_history = False
                continue
            if resolved is None:
                if _question_constraints(exchange.question).continuation:
                    continuations.add(exchange)
                else:
                    resets.add(exchange)
                    continuations.discard(exchange)
                    has_active_ambiguous_person_history = False
                continue
            _validate_resolved_person(resolved)
            exchange_shape = parse_person_query_shape(exchange.question)
            exchange_constraints = _question_constraints(exchange.question)
            hard_switch = bool(
                exchange_shape is not None
                and exchange_shape.transition == "switch"
                and normalize_query_text(exchange.question)
                .lstrip()
                .startswith(("換成", "换成", "換一", "换一"))
            )
            hard_complete_person_reset = bool(
                exchange_shape is not None
                and exchange_shape.transition == "new"
                and exchange_constraints.complete_current_person
                and not has_recommendation_deduplication_request(
                    exchange.question
                )
            )
            if (
                has_active_ambiguous_person_history
                or hard_switch
                or hard_complete_person_reset
            ):
                resets.add(exchange)
                continuations.discard(exchange)
                has_active_ambiguous_person_history = False
                continue
            try:
                previous_person, _ = self._latest_history_person_resolution(
                    prefix,
                    authority_confirmed_continuations=frozenset(continuations),
                    authority_confirmed_resets=frozenset(resets),
                    resolved_history_people=resolutions,
                    ambiguous_history_exchanges=frozenset(ambiguous_exchanges),
                )
            except AmbiguousPersonResolutionError:
                previous_person = None
            latest_shape = parse_person_query_shape(active_chain[-1].question)
            if previous_person is not None:
                same_identity = _same_release_person_identity(
                    resolved, previous_person
                )
            elif (
                latest_shape is not None
                and len(latest_shape.candidate_names) == 1
                and not latest_shape.exclusionary
                and not latest_shape.role_conflict
            ):
                resolved_names = {
                    normalize_query_text(name).casefold()
                    for name in resolved.exact_names or (resolved.name,)
                }
                latest_names = {
                    normalize_query_text(name).casefold()
                    for name in latest_shape.candidate_names
                }
                same_identity = not resolved_names.isdisjoint(latest_names)
            elif exchange_shape is not None:
                resolved_names = {
                    normalize_query_text(name).casefold()
                    for name in resolved.exact_names or (resolved.name,)
                }
                active_questions = tuple(
                    normalize_query_text(item.question).casefold()
                    for item in active_chain
                )
                same_identity = any(
                    exact_name in active_question
                    for exact_name in resolved_names
                    for active_question in active_questions
                )
            else:
                same_identity = False
            if same_identity:
                continuations.add(exchange)
            else:
                resets.add(exchange)
                has_active_ambiguous_person_history = False
        active_chain = _latest_successful_recommendation_chain(
            history,
            authority_confirmed_continuations=frozenset(continuations),
            authority_confirmed_resets=frozenset(resets),
        )
        has_active_ambiguous_person_history = any(
            exchange in ambiguous_exchanges for exchange in active_chain
        )
        return (
            frozenset(continuations),
            frozenset(resets),
            has_active_ambiguous_person_history,
            resolutions,
            frozenset(ambiguous_exchanges),
        )

    def _resolved_recommendation_plan(
        self,
        question: str,
        history: Sequence[ConversationExchange],
        plan: RecommendationPlan,
        person_query_shape: PersonQueryShape | None,
        *,
        is_deep_plan: bool,
        authority_confirmed_continuations: frozenset[
            ConversationExchange
        ] = frozenset(),
        authority_confirmed_resets: frozenset[ConversationExchange] = frozenset(),
        has_ambiguous_person_history: bool = False,
        requires_current_person_resolution: bool = False,
        allow_surface_person_qualifier: bool = True,
        current_person_resolution_attempted: bool = False,
        current_person_resolution: ResolvedPerson | None = None,
        resolved_history_people: Mapping[
            ConversationExchange, ResolvedPerson | None
        ]
        | None = None,
        ambiguous_history_exchanges: frozenset[
            ConversationExchange
        ] = frozenset(),
    ) -> tuple[RecommendationPlan, str | None]:
        """Resolve current person first, then one newest explicit history qualifier."""
        resolved = (
            current_person_resolution
            if current_person_resolution_attempted
            else self.repository.resolve_recommendation_person(
                self.release_id, question
            )
        )
        if resolved is not None:
            _validate_resolved_person(resolved)
            resolved_plan = plan
            active_chain = _latest_successful_recommendation_chain(
                history,
                authority_confirmed_continuations=authority_confirmed_continuations,
                authority_confirmed_resets=authority_confirmed_resets,
            )
            if (
                plan.continuation
                or has_person_catalog_continuation_shell(question)
                or has_recommendation_deduplication_request(question)
            ) and active_chain:
                previous_person = None
                known_different_switch = False
                if (
                    person_query_shape is not None
                    and person_query_shape.transition == "switch"
                    and len(person_query_shape.candidate_names) == 1
                ):
                    latest_shape = parse_person_query_shape(active_chain[-1].question)
                    if latest_shape is not None and len(
                        latest_shape.candidate_names
                    ) == 1:
                        current_exact_names = {
                            normalize_query_text(name).casefold()
                            for name in resolved.exact_names or (resolved.name,)
                        }
                        latest_names = {
                            normalize_query_text(name).casefold()
                            for name in latest_shape.candidate_names
                        }
                        known_different_switch = current_exact_names.isdisjoint(
                            latest_names
                        )
                local_ambiguous_history = has_ambiguous_person_history
                if not local_ambiguous_history and not known_different_switch:
                    try:
                        previous_person, _ = self._latest_history_person_resolution(
                            history,
                            authority_confirmed_continuations=(
                                authority_confirmed_continuations
                            ),
                            authority_confirmed_resets=authority_confirmed_resets,
                            resolved_history_people=resolved_history_people,
                            ambiguous_history_exchanges=ambiguous_history_exchanges,
                        )
                    except AmbiguousPersonResolutionError:
                        local_ambiguous_history = True
                preserve = bool(
                    not local_ambiguous_history
                    and not known_different_switch
                    and (
                        previous_person is None
                        or _same_release_person_identity(resolved, previous_person)
                    )
                )
                rebuilt_plan = (
                    _deep_recommendation_plan(
                        question,
                        history,
                        _preserve_complete_current_person_continuation=preserve,
                        _force_current_reset=not preserve,
                        _authority_confirmed_continuations=authority_confirmed_continuations,
                        _authority_confirmed_resets=authority_confirmed_resets,
                    )
                    if is_deep_plan
                    else plan_recommendation(
                        question,
                        history,
                        _preserve_complete_current_person_continuation=preserve,
                        _force_current_reset=not preserve,
                        _authority_confirmed_continuations=authority_confirmed_continuations,
                        _authority_confirmed_resets=authority_confirmed_resets,
                    )
                )
                if rebuilt_plan is None and not preserve:
                    rebuilt_plan = replace(
                        plan,
                        requested_count=5,
                        genres=(),
                        tier=None,
                        year_from=None,
                        year_to=None,
                        diversify_decades=False,
                        continuation=False,
                        excluded_movie_ids=(),
                        context_text=question,
                        genres_all=(),
                        genres_any=(),
                        title_terms_any=(),
                        credit_group_id=None,
                        credit_role=None,
                        credit_exact_names=(),
                        scope_label="metadata",
                    )
                if rebuilt_plan is not None:
                    resolved_plan = rebuilt_plan
            return replace(
                resolved_plan,
                person_name=resolved.name,
                person_role=resolved.role,
                person_exact_names=resolved.exact_names or (resolved.name,),
            ), None
        qualifier = (
            _person_role_label(person_query_shape.role)
            if person_query_shape is not None
            and (
                person_query_shape.role is not None
                or not _has_non_person_candidate_signal(
                    person_query_shape.candidate_names[0]
                )
            )
            else (
                _person_qualifier_label(question, plan)
                if allow_surface_person_qualifier
                else None
            )
        )
        tentative_continuation_requires_resolution = bool(
            not allow_surface_person_qualifier
            and (
                plan.continuation
                or has_recommendation_deduplication_request(question)
            )
            and _latest_successful_recommendation_chain(
                history,
                authority_confirmed_continuations=(
                    authority_confirmed_continuations
                ),
                authority_confirmed_resets=authority_confirmed_resets,
            )
        )
        if qualifier is None and (
            requires_current_person_resolution
            or tentative_continuation_requires_resolution
        ):
            qualifier = "人物"
        if qualifier is not None:
            return plan, qualifier
        if has_ambiguous_person_history and plan.continuation:
            raise AmbiguousPersonResolutionError
        if not plan.continuation:
            return plan, None
        history_person, history_qualifier = self._latest_history_person_resolution(
            history,
            authority_confirmed_continuations=authority_confirmed_continuations,
            authority_confirmed_resets=authority_confirmed_resets,
            resolved_history_people=resolved_history_people,
            ambiguous_history_exchanges=ambiguous_history_exchanges,
        )
        if history_person is not None:
            return replace(
                plan,
                person_name=history_person.name,
                person_role=history_person.role,
                person_exact_names=history_person.exact_names
                or (history_person.name,),
            ), None
        if history_qualifier is not None:
            return plan, history_qualifier
        return plan, None

    def answer(
        self, question: str, history: Sequence[ConversationExchange] = ()
    ) -> ChatAnswer:
        """Retrieve at most eight unique passages and return a cited answer."""
        normalized = _validated_question(question)
        bounded_history = _validated_history(history)
        conversation_context = tuple(exchange.question for exchange in bounded_history)
        explicit_resolution = self.repository.resolve_explicit_movie_context(
            self.release_id, normalized
        )
        target_movie_ids = explicit_resolution.movie_ids
        if not target_movie_ids and has_explicit_nonmovie_object_term(
            explicit_resolution.residual_question
        ):
            return ChatAnswer(
                answer_markdown=_DOMAIN_REJECTION,
                citations=(),
                movies=(),
            )
        if explicit_resolution.ambiguous:
            candidate_ids = "、".join(
                f"`{movie_id}`"
                for movie_id in explicit_resolution.ambiguous_movie_ids
            )
            clarification = (
                "這個片名在 Release 內對應多部同名電影；"
                f"請改用以下其中一個 movie ID：{candidate_ids}。"
                if candidate_ids
                else "這個片名在 Release 內對應多部同名電影；請改用 movie ID。"
            )
            return ChatAnswer(
                answer_markdown=clarification,
                citations=(),
                movies=(),
            )
        strong_ood = has_strong_out_of_domain_signal(
            explicit_resolution.residual_question
            if explicit_resolution.movie_ids
            else normalized
        )
        if strong_ood and not target_movie_ids:
            return ChatAnswer(
                answer_markdown=_DOMAIN_REJECTION,
                citations=(),
                movies=(),
            )
        residual_person_shape = (
            _residual_person_query_shape(explicit_resolution.residual_question)
            if target_movie_ids
            else None
        )
        residual_explicit_person_catalog = bool(
            target_movie_ids
            and has_explicit_person_catalog_intent(
                explicit_resolution.residual_question
            )
        )
        residual_tentative_person_catalog = bool(
            target_movie_ids
            and has_tentative_person_catalog_intent(
                explicit_resolution.residual_question
            )
        )
        residual_multiple_person_catalog = bool(
            target_movie_ids
            and has_multiple_person_catalog_intent(
                explicit_resolution.residual_question
            )
        )
        if target_movie_ids and (
            residual_person_shape is not None
            or residual_explicit_person_catalog
            or residual_tentative_person_catalog
            or residual_multiple_person_catalog
        ):
            return ChatAnswer(
                answer_markdown=_MIXED_MOVIE_PERSON_SCOPE_CLARIFICATION,
                citations=(),
                movies=(),
            )
        person_query_shape = (
            None
            if target_movie_ids
            else parse_person_query_shape(normalized)
        )
        explicit_person_catalog_intent = bool(
            not target_movie_ids
            and has_explicit_person_catalog_intent(normalized)
        )
        tentative_person_catalog_intent = bool(
            not target_movie_ids
            and has_tentative_person_catalog_intent(normalized)
        )
        person_catalog_intent = bool(
            explicit_person_catalog_intent or tentative_person_catalog_intent
        )
        if not target_movie_ids and has_multiple_person_catalog_intent(normalized):
            return ChatAnswer(
                answer_markdown=_PERSON_SELECTION_CLARIFICATION,
                citations=(),
                movies=(),
            )
        if person_query_shape is not None and (
            len(person_query_shape.candidate_names) != 1
            or person_query_shape.exclusionary
        ):
            return ChatAnswer(
                answer_markdown=_PERSON_SELECTION_CLARIFICATION,
                citations=(),
                movies=(),
            )
        if person_query_shape is not None and person_query_shape.role_conflict:
            return ChatAnswer(
                answer_markdown=_PERSON_ROLE_CONFLICT_CLARIFICATION,
                citations=(),
                movies=(),
            )
        negative_filter_question = (
            explicit_resolution.residual_question if target_movie_ids else normalized
        )
        if has_unsupported_negative_recommendation_filter(negative_filter_question):
            return ChatAnswer(
                answer_markdown=_UNSUPPORTED_NEGATIVE_FILTER_CLARIFICATION,
                citations=(),
                movies=(),
            )
        if has_unmodeled_negative_recommendation_condition(
            negative_filter_question
        ):
            return ChatAnswer(
                answer_markdown=_UNMODELED_NEGATIVE_CONDITION_CLARIFICATION,
                citations=(),
                movies=(),
            )
        history_has_person_authority_candidate = any(
            _history_exchange_requires_person_authority(exchange)
            for exchange in bounded_history
        )
        current_constraints = _question_constraints(normalized)
        current_positive_person_shape = bool(
            person_query_shape is not None
            and len(person_query_shape.candidate_names) == 1
            and not person_query_shape.exclusionary
            and not person_query_shape.role_conflict
            and not _has_non_person_candidate_signal(
                person_query_shape.candidate_names[0]
            )
        )
        current_deduplication_request = has_recommendation_deduplication_request(
            normalized
        )
        preliminary_history_authority_need = bool(
            history_has_person_authority_candidate
            and (
                current_constraints.continuation
                or current_deduplication_request
            )
        )
        current_requires_person_authority = bool(
            explicit_person_catalog_intent
            or (
                tentative_person_catalog_intent
                and (
                    current_constraints.continuation
                    or current_deduplication_request
                )
            )
            or (
                current_positive_person_shape
                and preliminary_history_authority_need
            )
        )
        current_person_resolution_attempted = False
        current_person_resolution: ResolvedPerson | None = None
        if (
            not target_movie_ids
            and (
                current_requires_person_authority
                or preliminary_history_authority_need
            )
            and (
                current_constraints.recommendation_intent
                or current_constraints.continuation
                or current_constraints.deep_analysis_intent
                or person_catalog_intent
            )
        ):
            try:
                current_person_resolution = (
                    self.repository.resolve_recommendation_person(
                        self.release_id, normalized
                    )
                )
            except AmbiguousPersonResolutionError:
                return ChatAnswer(
                    answer_markdown=_PERSON_SELECTION_CLARIFICATION,
                    citations=(),
                    movies=(),
                )
            if current_person_resolution is not None:
                _validate_resolved_person(current_person_resolution)
            current_person_resolution_attempted = True
        if (
            current_person_resolution_attempted
            and current_person_resolution is None
            and current_requires_person_authority
        ):
            qualifier = (
                _person_role_label(person_query_shape.role)
                if person_query_shape is not None
                else "人物"
            )
            return ChatAnswer(
                answer_markdown=(
                    f"無法在目前 Release 精確解析指定的{qualifier}；"
                    "為避免錯配，不會以向量搜尋替代。"
                ),
                citations=(),
                movies=(),
            )
        current_hard_person_switch = normalize_query_text(normalized).lstrip().startswith(
            ("換成", "换成", "換一", "换一")
        )
        current_person_catalog_continuation = (
            has_person_catalog_continuation_shell(normalized)
        )
        current_resolved_person_reset = bool(
            current_requires_person_authority
            and current_person_resolution is not None
            and not current_deduplication_request
            and (
                current_hard_person_switch
                or not current_person_catalog_continuation
            )
        )
        history_needs_person_authority = bool(
            preliminary_history_authority_need
            and not current_resolved_person_reset
        )
        recommendation_history = (
            () if current_resolved_person_reset else bounded_history
        )
        (
            authority_confirmed_continuations,
            authority_confirmed_resets,
            has_ambiguous_person_history,
            resolved_history_people,
            ambiguous_history_exchanges,
        ) = (
            self._history_recommendation_authority(bounded_history)
            if history_needs_person_authority
            else (frozenset(), frozenset(), False, {}, frozenset())
        )
        if deduplication_requires_successful_history(
            negative_filter_question
        ) and not _latest_successful_recommendation_chain(
            bounded_history,
            authority_confirmed_continuations=authority_confirmed_continuations,
            authority_confirmed_resets=authority_confirmed_resets,
        ):
            return ChatAnswer(
                answer_markdown=_MISSING_RECOMMENDATION_HISTORY_CLARIFICATION,
                citations=(),
                movies=(),
            )
        needs_target_clarification = False
        if not target_movie_ids:
            target_movie_ids, needs_target_clarification = _history_target_movie_ids(
                normalized, bounded_history
            )
        if needs_target_clarification:
            return ChatAnswer(
                answer_markdown="請指出你指的是上一輪的第幾部電影。",
                citations=(),
                movies=(),
            )
        bounded_analysis_plan = (
            None if target_movie_ids else plan_bounded_analysis(normalized)
        )
        deep_recommendation_plan = (
            None
            if target_movie_ids or bounded_analysis_plan is not None
            else _deep_recommendation_plan(
                normalized,
                recommendation_history,
                _authority_confirmed_continuations=authority_confirmed_continuations,
                _authority_confirmed_resets=authority_confirmed_resets,
            )
        )
        recommendation_plan = None
        if not (
            target_movie_ids
            or bounded_analysis_plan is not None
            or deep_recommendation_plan is not None
        ):
            if authority_confirmed_continuations or authority_confirmed_resets:
                recommendation_plan = plan_recommendation(
                    normalized,
                    recommendation_history,
                    _force_person_catalog_intent=person_catalog_intent,
                    _authority_confirmed_continuations=(
                        authority_confirmed_continuations
                    ),
                    _authority_confirmed_resets=authority_confirmed_resets,
                )
            elif person_catalog_intent:
                recommendation_plan = plan_recommendation(
                    normalized,
                    recommendation_history,
                    _force_person_catalog_intent=True,
                )
            else:
                recommendation_plan = plan_recommendation(
                    normalized,
                    recommendation_history,
                )
        active_recommendation_plan = (
            deep_recommendation_plan
            if deep_recommendation_plan is not None
            else recommendation_plan
        )
        if active_recommendation_plan is not None:
            if (
                person_query_shape is not None
                and active_recommendation_plan.scope_label
                == "controlled_credit_group"
            ):
                return ChatAnswer(
                    answer_markdown=_UNSUPPORTED_PERSON_SCOPE_CLARIFICATION,
                    citations=(),
                    movies=(),
                )
            current_person_shape = (
                None
                if tentative_person_catalog_intent
                and not explicit_person_catalog_intent
                and person_query_shape is None
                else person_query_shape
            )
            try:
                resolved_plan, unresolved_qualifier = self._resolved_recommendation_plan(
                    normalized,
                    recommendation_history,
                    active_recommendation_plan,
                    current_person_shape,
                    is_deep_plan=deep_recommendation_plan is not None,
                    authority_confirmed_continuations=authority_confirmed_continuations,
                    authority_confirmed_resets=authority_confirmed_resets,
                    has_ambiguous_person_history=has_ambiguous_person_history,
                    requires_current_person_resolution=(
                        explicit_person_catalog_intent
                    ),
                    allow_surface_person_qualifier=(
                        not tentative_person_catalog_intent
                    ),
                    current_person_resolution_attempted=(
                        current_person_resolution_attempted
                    ),
                    current_person_resolution=current_person_resolution,
                    resolved_history_people=resolved_history_people,
                    ambiguous_history_exchanges=ambiguous_history_exchanges,
                )
            except AmbiguousPersonResolutionError:
                return ChatAnswer(
                    answer_markdown=_PERSON_SELECTION_CLARIFICATION,
                    citations=(),
                    movies=(),
                )
            if unresolved_qualifier is not None:
                return ChatAnswer(
                    answer_markdown=(
                        f"無法在目前 Release 精確解析指定的{unresolved_qualifier}；"
                        "為避免錯配，不會以向量搜尋替代。"
                    ),
                    citations=(),
                    movies=(),
                )
            if deep_recommendation_plan is not None:
                deep_recommendation_plan = resolved_plan
            else:
                recommendation_plan = resolved_plan
        active_recommendation_plan = (
            deep_recommendation_plan
            if deep_recommendation_plan is not None
            else recommendation_plan
        )
        successful_continuation = bool(
            active_recommendation_plan is not None
            and active_recommendation_plan.continuation
            and _latest_successful_recommendation_chain(
                recommendation_history,
                authority_confirmed_continuations=authority_confirmed_continuations,
                authority_confirmed_resets=authority_confirmed_resets,
            )
        )
        resolved_person_semantics = bool(
            active_recommendation_plan is not None
            and active_recommendation_plan.person_name
        )
        static_movie_signal = has_static_movie_domain_signal(normalized)
        trusted_target_analysis = bool(
            target_movie_ids
            and (static_movie_signal or has_deep_analysis_intent(normalized))
        )
        if (strong_ood and not trusted_target_analysis) or (
            not target_movie_ids
            and not (
                static_movie_signal
                or resolved_person_semantics
                or successful_continuation
            )
        ):
            return ChatAnswer(
                answer_markdown=_DOMAIN_REJECTION,
                citations=(),
                movies=(),
            )
        if (
            not target_movie_ids
            and has_unsupported_broad_analysis_intent(normalized)
        ):
            return ChatAnswer(
                answer_markdown=_UNSUPPORTED_BROAD_ANALYSIS,
                citations=(),
                movies=(),
            )
        embedding_model, embedding_dimension = _validated_query_contract(
            self.embedding_client
        )
        try:
            self.repository.assert_query_ready(self.release_id)
        except RagDatabaseError as exc:
            raise GroundingError("active release contract is incomplete") from exc
        release_state = self.repository.release_state(self.release_id)
        if (
            release_state.status != "active"
            or release_state.embedding_model != embedding_model
            or release_state.embedding_dimension != embedding_dimension
            or release_state.manifest_sha256 != self.expected_manifest_sha256
        ):
            raise GroundingError(
                "active release identity does not match query embedding contract"
            )
        context_text = (
            normalized
            if bounded_analysis_plan is not None
            else deep_recommendation_plan.context_text
            if deep_recommendation_plan is not None
            else recommendation_plan.context_text
            if recommendation_plan is not None
            else "\n".join((*conversation_context, normalized))
        )
        query_text = f"task: search result | query: {context_text}"
        query_embedding = self.embedding_client.embed_query(query_text)
        if deep_recommendation_plan is not None:
            deep_search = self.repository.search_deep_recommendations(
                self.release_id, query_embedding, deep_recommendation_plan
            )
            evidence = _selected_deep_recommendation_evidence(
                deep_search, deep_recommendation_plan
            )
            if len(evidence) < deep_recommendation_plan.requested_count:
                return _insufficient_deep_recommendation(
                    evidence,
                    plan=deep_recommendation_plan,
                    total_matches=deep_search.total_matches,
                    approved_poster_movie_ids=self._approved_poster_movie_ids(
                        tuple(dict.fromkeys(item.movie_id for item in evidence))
                    ),
                )
            required_citation_ids = tuple(
                passage.passage_id for passage in evidence
            )
            generated = self.generator.generate_grounded(
                normalized,
                evidence,
                mode="deep_recommendation",
                conversation_context=conversation_context,
                required_citation_ids=required_citation_ids,
            )
            answer_markdown, citation_ids = _validated_generated_answer(
                generated,
                evidence,
                required_citation_ids=required_citation_ids,
            )
            citations = _build_citations(citation_ids, evidence)
            return ChatAnswer(
                answer_markdown=answer_markdown,
                citations=citations,
                movies=self._authoritative_movie_cards(evidence),
            )
        if recommendation_plan is not None:
            search_plan = recommendation_plan
            if recommendation_plan.diversify_decades:
                search_plan = replace(recommendation_plan, requested_count=_MAX_PASSAGES)
            recommendation_search = self.repository.search_recommendations(
                self.release_id, query_embedding, search_plan
            )
            evidence = _selected_recommendation_evidence(
                recommendation_search, recommendation_plan
            )
            if len(evidence) < recommendation_plan.requested_count:
                return ChatAnswer(
                    answer_markdown=_insufficient_recommendation_matches(
                        recommendation_plan,
                        total_matches=recommendation_search.total_matches,
                        available_count=len(evidence),
                    ),
                    citations=(),
                    movies=(),
                )
            required_citation_ids = tuple(
                passage.passage_id for passage in evidence
            )
            if recommendation_plan.scope_label == "metadata":
                _validate_metadata_recommendation_scope(recommendation_plan)
                generated = self.generator.generate_grounded(
                    normalized,
                    evidence,
                    mode="recommendation",
                    conversation_context=conversation_context,
                    required_citation_ids=required_citation_ids,
                )
            else:
                generated = _deterministic_scoped_recommendation(
                    recommendation_plan,
                    evidence,
                    required_citation_ids,
                )
            answer_markdown, citation_ids = _validated_generated_answer(
                generated,
                evidence,
                required_citation_ids=required_citation_ids,
            )
            citations = _build_citations(citation_ids, evidence)
            return ChatAnswer(
                answer_markdown=answer_markdown,
                citations=citations,
                movies=self._authoritative_movie_cards(evidence),
            )

        if bounded_analysis_plan is not None:
            bounded_bindings = _validated_bounded_analysis_bindings(
                bounded_analysis_plan
            )
            bounded_evidence_items: list[EvidencePassage] = []
            for movie_id, passage_id, _excerpt_terms in bounded_bindings:
                movie_records = self.repository.search(
                    self.release_id,
                    query_embedding,
                    limit=_MAX_PASSAGES,
                    question=normalized,
                    embedding_model=embedding_model,
                    embedding_dimension=embedding_dimension,
                    target_movie_ids=(movie_id,),
                )
                movie_evidence = _ordered_unique_evidence(
                    normalized,
                    movie_records,
                    distance_cutoff=None,
                )
                if any(
                    passage.movie_id != movie_id for passage in movie_evidence
                ):
                    raise GroundingError(
                        "bounded analysis search returned an unrelated movie"
                    )
                selected = tuple(
                    passage
                    for passage in movie_evidence
                    if passage.passage_id == passage_id
                    and passage.movie_id == movie_id
                )
                if len(selected) != 1:
                    return ChatAnswer(
                        answer_markdown=_UNSUPPORTED_BROAD_ANALYSIS,
                        citations=(),
                        movies=(),
                    )
                bounded_evidence_items.append(selected[0])
            bounded_evidence = _selected_bounded_analysis_evidence(
                tuple(bounded_evidence_items), bounded_analysis_plan
            )
            if not bounded_evidence:
                return ChatAnswer(
                    answer_markdown=_UNSUPPORTED_BROAD_ANALYSIS,
                    citations=(),
                    movies=(),
                )
            citation_ids = tuple(
                passage.passage_id for passage in bounded_evidence
            )
            citations = _build_citations(
                citation_ids,
                bounded_evidence,
                excerpt_terms_by_id={
                    passage_id: excerpt_terms
                    for _, passage_id, excerpt_terms in bounded_bindings
                },
            )
            return ChatAnswer(
                answer_markdown=_bounded_analysis_markdown(
                    bounded_analysis_plan, bounded_evidence
                ),
                citations=citations,
                movies=self._authoritative_movie_cards(bounded_evidence),
            )

        retrieval_target_movie_ids = target_movie_ids
        records = self.repository.search(
            self.release_id,
            query_embedding,
            limit=_MAX_PASSAGES,
            question=normalized,
            embedding_model=embedding_model,
            embedding_dimension=embedding_dimension,
            target_movie_ids=retrieval_target_movie_ids,
        )
        evidence = _ordered_unique_evidence(
            normalized,
            records,
            distance_cutoff=(
                None
                if retrieval_target_movie_ids
                else self.relevance_policy.distance_cutoff
            ),
        )
        if target_movie_ids and any(
            passage.movie_id not in target_movie_ids for passage in evidence
        ):
            raise GroundingError("targeted search returned an unrelated movie")
        if not evidence:
            return ChatAnswer(
                answer_markdown="目前沒有足夠的檢索證據回答這個問題。",
                citations=(),
                movies=(),
            )

        target_authority = _exact_target_authority(
            normalized,
            evidence,
            resolved_target_movie_ids=target_movie_ids,
        )
        if target_authority is not None and target_authority.blocked:
            citation_ids = tuple(
                passage.passage_id for passage in target_authority.metadata
            )
            citations = _build_citations(citation_ids, target_authority.metadata)
            markers = "".join(f"[{citation_id}]" for citation_id in citation_ids)
            return ChatAnswer(
                answer_markdown=(
                    "目前只有結構化電影資料，現有欄位不足以支持這個問題。"
                    f"{markers}"
                ),
                citations=citations,
                movies=self._authoritative_movie_cards(target_authority.metadata),
            )

        if target_authority is not None and target_authority.canonical_fields:
            canonical_answer, canonical_ids = _canonical_metadata_answer(
                target_authority.metadata,
                target_authority.canonical_fields,
            )
            canonical_citations = _build_citations(
                canonical_ids, target_authority.metadata
            )
            if target_authority.qualitative_question is None:
                return ChatAnswer(
                    answer_markdown=canonical_answer,
                    citations=canonical_citations,
                    movies=self._authoritative_movie_cards(target_authority.metadata),
                )
            generated = self.generator.generate_grounded(
                target_authority.qualitative_question,
                target_authority.qualitative,
                mode="general",
                conversation_context=conversation_context,
                required_citation_ids=(),
            )
            selected_qualitative_ids = _selected_qualitative_citation_ids(
                generated,
                target_authority.qualitative_question,
                target_authority.qualitative,
            )
            qualitative_answer, qualitative_ids = _extractive_qualitative_answer(
                target_authority.qualitative_question,
                selected_qualitative_ids,
                target_authority.qualitative,
            )
            qualitative_citations = _build_citations(
                qualitative_ids, target_authority.qualitative
            )
            selected_evidence = (
                *target_authority.metadata,
                *target_authority.qualitative,
            )
            return ChatAnswer(
                answer_markdown=f"{canonical_answer}\n{qualitative_answer}",
                citations=(*canonical_citations, *qualitative_citations),
                movies=self._authoritative_movie_cards(selected_evidence),
            )

        if target_authority is not None:
            evidence = target_authority.qualitative

        generated = self.generator.generate_grounded(
            normalized,
            evidence,
            mode="general",
            conversation_context=conversation_context,
            required_citation_ids=(),
        )
        if target_authority is not None:
            selected_citation_ids = _selected_qualitative_citation_ids(
                generated,
                normalized,
                evidence,
            )
            answer_markdown, citation_ids = _extractive_qualitative_answer(
                normalized, selected_citation_ids, evidence
            )
            if not citation_ids:
                return ChatAnswer(
                    answer_markdown=answer_markdown,
                    citations=(),
                    movies=(),
                )
        else:
            answer_markdown, citation_ids = _validated_generated_answer(
                generated, evidence
            )
        citations = _build_citations(citation_ids, evidence)
        evidence_by_id = {passage.passage_id: passage for passage in evidence}
        selected_evidence = tuple(
            evidence_by_id[citation_id] for citation_id in citation_ids
        )
        return ChatAnswer(
            answer_markdown=answer_markdown,
            citations=citations,
            movies=self._authoritative_movie_cards(selected_evidence),
        )

    def _approved_poster_movie_ids(
        self, movie_ids: tuple[str, ...]
    ) -> frozenset[str]:
        if not movie_ids:
            return frozenset()
        if len(movie_ids) > _MAX_PASSAGES or len(set(movie_ids)) != len(movie_ids):
            raise GroundingError("poster authority request is invalid")
        try:
            assets = self.repository.get_approved_poster_assets(
                self.release_id, movie_ids
            )
        except RagDatabaseError as exc:
            raise GroundingError("poster authority is unavailable") from exc
        if not isinstance(assets, Mapping):
            raise GroundingError("poster authority response is invalid")
        approved: set[str] = set()
        for movie_id, asset in assets.items():
            if (
                movie_id not in movie_ids
                or movie_id in approved
                or not isinstance(asset, Mapping)
                or asset.get("movie_id") != movie_id
            ):
                raise GroundingError("poster authority response is invalid")
            approved.add(movie_id)
        return frozenset(approved)

    def _authoritative_movie_cards(
        self,
        evidence: Sequence[EvidencePassage],
    ) -> tuple[MovieCard, ...]:
        movie_ids = tuple(dict.fromkeys(passage.movie_id for passage in evidence))
        approved = self._approved_poster_movie_ids(movie_ids)
        return _build_movie_cards(
            movie_ids,
            evidence,
            approved_poster_movie_ids=approved,
        )


def _validated_question(question: str) -> str:
    if not isinstance(question, str):
        raise QueryValidationError("question must be text")
    normalized = question.strip()
    if not normalized:
        raise QueryValidationError("question must be non-empty")
    if len(normalized) > _MAX_QUESTION_CHARACTERS:
        raise QueryValidationError("question must contain at most 1000 characters")
    return normalized


def _residual_person_query_shape(residual_question: str) -> PersonQueryShape | None:
    """Parse only the non-title clause left after exact movie ownership."""
    normalized = normalize_query_text(residual_question)
    for left, right in TITLE_DELIMITERS:
        normalized = re.sub(
            rf"{re.escape(left)}\s*{re.escape(right)}",
            " ",
            normalized,
        )
    normalized = normalized.strip(" \t\r\n。！？!?，,、:：;；.")
    normalized = re.sub(
        r"^(?:(?:請問|請|麻煩|可以|能否|可否)\s*)?"
        r"(?:比較|对比|推薦|推介|建議|想看|想找)\s*",
        "",
        normalized,
    )
    normalized = re.sub(
        r"^(?:和|與|与|及|跟|或|、|，|,|vs\.?)\s*",
        "",
        normalized,
        flags=re.IGNORECASE,
    ).strip(" \t\r\n。！？!?，,、:：;；.")
    if not normalized:
        return None
    shape = parse_person_query_shape(normalized)
    if shape is None:
        return None
    if (shape.role is not None or shape.role_conflict) and all(
        not _has_non_person_candidate_signal(name)
        for name in shape.candidate_names
    ):
        return shape
    if all(_plausible_person_candidate(name) for name in shape.candidate_names):
        return shape
    return None


def _validate_resolved_person(person: ResolvedPerson) -> None:
    if (
        not isinstance(person, ResolvedPerson)
        or not isinstance(person.name, str)
        or not person.name.strip()
        or person.role not in {None, "actor", "director"}
        or not isinstance(person.exact_names, tuple)
        or any(
            not isinstance(name, str) or not name.strip()
            for name in person.exact_names
        )
        or len(set(person.exact_names)) != len(person.exact_names)
        or (person.exact_names and person.name not in person.exact_names)
    ):
        raise GroundingError("resolved recommendation person is invalid")


def _same_release_person_identity(
    current: ResolvedPerson, previous: ResolvedPerson
) -> bool:
    """Compare only repository-authoritative names within the active Release."""
    current_names = frozenset(current.exact_names or (current.name,))
    previous_names = frozenset(previous.exact_names or (previous.name,))
    return not current_names.isdisjoint(previous_names)


def _history_exchange_requires_person_authority(
    exchange: ConversationExchange,
) -> bool:
    if not exchange.movie_ids:
        return False
    if (
        _is_bounded_movie_detail_history_question(exchange.question)
        and not has_tentative_person_catalog_intent(exchange.question)
    ):
        return False
    constraints = _question_constraints(exchange.question)
    shape = constraints.person_query_shape
    has_special_person_transition = bool(
        shape is not None
        and len(shape.candidate_names) == 1
        and not shape.exclusionary
        and not shape.role_conflict
        and not _has_non_person_candidate_signal(shape.candidate_names[0])
        and (
            shape.transition == "switch"
            or _is_bare_person_history_shape(exchange.question, shape)
        )
    )
    return bool(
        has_recommendation_deduplication_request(exchange.question)
        or has_tentative_person_catalog_intent(exchange.question)
        or has_special_person_transition
        or (
            constraints.continuation
            and constraints.person_query_shape is None
        )
    )


def _person_qualifier_label(
    question: str, plan: RecommendationPlan
) -> str | None:
    normalized = normalize_query_text(question)
    for pattern, role in _PERSON_RELATIONSHIP_PATTERNS:
        match = pattern.search(normalized)
        if match is None:
            continue
        candidate = match.group("name").strip()
        if _plausible_person_name(candidate, plan):
            return _person_role_label(role)
    for pattern in _ENGLISH_PERSON_RECOMMENDATION_PATTERNS:
        match = pattern.search(question)
        if match is None:
            continue
        candidate = match.group("name").strip()
        if candidate.casefold() not in _ENGLISH_NON_PERSON_QUALIFIERS:
            return "人物"
    return None


def _plausible_person_name(
    candidate: str,
    plan: RecommendationPlan,
) -> bool:
    if not _plausible_person_candidate(candidate):
        return False
    candidate_constraints = _question_constraints(candidate)
    return not (
        candidate in plan.genres
        or candidate_constraints.genres
        or candidate_constraints.tier is not None
        or candidate_constraints.has_year_constraint
        or candidate_constraints.diversify_decades
        or candidate_constraints.deep_analysis_intent
    )


def _plausible_person_candidate(candidate: str) -> bool:
    if _has_non_person_candidate_signal(candidate):
        return False
    return candidate[0] in _COMMON_CHINESE_SURNAME_INITIALS


def _has_non_person_candidate_signal(candidate: str) -> bool:
    return candidate in _NON_PERSON_POSSESSIVE_QUALIFIERS or any(
        fragment in candidate for fragment in _NON_PERSON_NAME_FRAGMENTS
    )


def _person_role_label(role: PersonRole | None) -> str:
    if role == "actor":
        return "演員"
    if role == "director":
        return "導演"
    return "人物"


def _validated_history(
    history: Sequence[ConversationExchange],
) -> tuple[ConversationExchange, ...]:
    if isinstance(history, (str, bytes)) or not isinstance(history, Sequence):
        raise QueryValidationError("history must be a sequence of conversation exchanges")
    exchanges = tuple(history)
    if any(not isinstance(exchange, ConversationExchange) for exchange in exchanges):
        raise QueryValidationError("history contains an invalid conversation exchange")
    bounded = exchanges[-4:]
    total_characters = 0
    for exchange in bounded:
        if (
            not isinstance(exchange.question, str)
            or not exchange.question.strip()
            or not isinstance(exchange.answer, str)
            or not exchange.answer.strip()
            or not isinstance(exchange.movie_ids, tuple)
            or len(exchange.movie_ids) > _MAX_PASSAGES
            or any(
                not isinstance(movie_id, str) or not movie_id.strip()
                for movie_id in exchange.movie_ids
            )
            or len(set(exchange.movie_ids)) != len(exchange.movie_ids)
        ):
            raise QueryValidationError("history contains an invalid conversation exchange")
        total_characters += len(exchange.question) + len(exchange.answer)
        total_characters += sum(len(movie_id) for movie_id in exchange.movie_ids)
    if total_characters > _MAX_HISTORY_CHARACTERS:
        raise QueryValidationError("history exceeds the 12000 character limit")
    return bounded


def _history_target_movie_ids(
    question: str, history: Sequence[ConversationExchange]
) -> tuple[tuple[str, ...], bool]:
    """Resolve one strict latest-turn reference or request a non-factual clarification."""
    if any(
        left in question
        and (left_position := question.find(left)) >= 0
        and question.find(right, left_position + len(left)) > left_position + len(left)
        for left, right in TITLE_DELIMITERS
    ):
        return (), False
    latest_movie_ids = next(
        (
            exchange.movie_ids
            for exchange in reversed(tuple(history))
            if exchange.movie_ids
        ),
        (),
    )
    if not latest_movie_ids:
        return (), False
    ordinal_match = _CHINESE_ORDINAL_PATTERN.search(question)
    if ordinal_match is None:
        ordinal_match = _ENGLISH_ORDINAL_PATTERN.search(question)
    if ordinal_match is not None:
        ordinal = ordinal_match.group("ordinal").casefold()
        index = _ORDINAL_INDEX[ordinal]
        if index >= len(latest_movie_ids):
            return (), True
        return (latest_movie_ids[index],), False
    if _has_singular_history_pronoun(question):
        if len(latest_movie_ids) != 1:
            return (), True
        return (latest_movie_ids[0],), False
    return (), False


def _has_history_reference_candidate(
    question: str, history: Sequence[ConversationExchange]
) -> bool:
    """Return whether a fresh explicit-title check must precede history binding."""
    if not any(exchange.movie_ids for exchange in history):
        return False
    return (
        _CHINESE_ORDINAL_PATTERN.search(question) is not None
        or _ENGLISH_ORDINAL_PATTERN.search(question) is not None
        or any(phrase in question for phrase in ("這部", "这部", "該片", "该片", "它"))
        or _ENGLISH_PRONOUN_PATTERN.search(question) is not None
    )


def _has_singular_history_pronoun(question: str) -> bool:
    if _CHINESE_HISTORY_DEMONSTRATIVE_PATTERN.search(question) is not None:
        return True
    if "它" in question and not any(
        phrase in question for phrase in ("其它", "它們", "它们")
    ):
        position = question.find("它")
        prefix = question[:position].strip(" \t\r\n,，:：;；!?！？")
        return _CHINESE_HISTORY_BARE_PREFIX.fullmatch(prefix) is not None
    match = _ENGLISH_PRONOUN_PATTERN.search(question)
    if match is None:
        return False
    prefix = question[: match.start()].strip(" \t\r\n,，:：;；!?！？")
    return _ENGLISH_HISTORY_DEMONSTRATIVE_PREFIX.fullmatch(prefix) is not None


def _without_history_reference(text: str) -> str:
    residual = _CHINESE_ORDINAL_PATTERN.sub(" ", text)
    residual = _ENGLISH_ORDINAL_PATTERN.sub(" ", residual)
    residual = _ENGLISH_PRONOUN_PATTERN.sub(" ", residual)
    for phrase in ("這部", "这部", "該片", "该片", "它"):
        residual = residual.replace(phrase, " ")
    return residual


def _validated_query_contract(client: QueryEmbeddingClient) -> tuple[str, int]:
    model = getattr(client, "model", None)
    dimension = getattr(client, "dimension", None)
    if (
        not isinstance(model, str)
        or not model.strip()
        or not isinstance(dimension, int)
        or isinstance(dimension, bool)
        or dimension != 768
    ):
        raise GroundingError("query embedding contract is invalid")
    return model, dimension


def _ordered_unique_evidence(
    question: str,
    records: Sequence[Mapping[str, object]],
    *,
    distance_cutoff: float | None,
) -> tuple[EvidencePassage, ...]:
    parsed = [
        _evidence_from_record(record, require_distance=True)
        for record in records
    ]
    if distance_cutoff is not None:
        parsed = [
            passage
            for passage in parsed
            if passage.distance is not None and passage.distance <= distance_cutoff
        ]
    indexed = list(enumerate(parsed))
    indexed.sort(key=lambda item: _evidence_order(question, item[1], item[0]))
    unique: list[EvidencePassage] = []
    seen: set[str] = set()
    for _, passage in indexed:
        if passage.passage_id in seen:
            continue
        seen.add(passage.passage_id)
        unique.append(passage)
        if len(unique) == _MAX_PASSAGES:
            break
    return tuple(unique)


def _validated_bounded_analysis_bindings(
    plan: BoundedAnalysisPlan,
) -> tuple[tuple[str, str, tuple[str, ...]], ...]:
    if (
        not plan.movie_ids
        or len(plan.movie_ids) != len(plan.passage_ids)
        or len(set(plan.movie_ids)) != len(plan.movie_ids)
        or len(set(plan.passage_ids)) != len(plan.passage_ids)
        or len(plan.excerpt_terms) != len(plan.passage_ids)
        or any(
            not terms
            or any(not isinstance(term, str) or not term.strip() for term in terms)
            for terms in plan.excerpt_terms
        )
    ):
        raise GroundingError("bounded analysis plan is invalid")
    return tuple(
        zip(
            plan.movie_ids,
            plan.passage_ids,
            plan.excerpt_terms,
            strict=True,
        )
    )


def _selected_bounded_analysis_evidence(
    evidence: Sequence[EvidencePassage], plan: BoundedAnalysisPlan
) -> tuple[EvidencePassage, ...]:
    bindings = _validated_bounded_analysis_bindings(plan)
    if any(passage.movie_id not in plan.movie_ids for passage in evidence):
        raise GroundingError("bounded analysis search returned an unrelated movie")
    by_passage_id = {passage.passage_id: passage for passage in evidence}
    selected: list[EvidencePassage] = []
    for movie_id, passage_id, _excerpt_terms in bindings:
        passage = by_passage_id.get(passage_id)
        if passage is None:
            return ()
        if passage.movie_id != movie_id or passage.source_kind != "pdf_page":
            raise GroundingError("bounded analysis evidence authority is invalid")
        selected.append(passage)
    return tuple(selected)


def _bounded_analysis_markdown(
    plan: BoundedAnalysisPlan, evidence: Sequence[EvidencePassage]
) -> str:
    bindings = _validated_bounded_analysis_bindings(plan)
    if (
        len(evidence) != len(plan.passage_ids)
        or not plan.scope_note.strip()
        or not plan.answer_summary.strip()
        or _BRACKETED_TOKEN.search(plan.answer_summary) is not None
        or _contains_uri_or_path(plan.answer_summary)
    ):
        raise GroundingError("bounded analysis evidence is incomplete")
    summary_citations = "".join(f"[{passage_id}]" for passage_id in plan.passage_ids)
    lines = [plan.scope_note, f"{plan.answer_summary} {summary_citations}"]
    for passage, (_movie_id, passage_id, excerpt_terms) in zip(
        evidence, bindings, strict=True
    ):
        title = _recommendation_evidence_title(passage)
        excerpt = _relevant_excerpt(passage.body, excerpt_terms)
        if (
            not excerpt
            or _BRACKETED_TOKEN.search(excerpt) is not None
            or _contains_uri_or_path(excerpt)
        ):
            raise GroundingError("bounded analysis excerpt is invalid")
        lines.append(f"- 《{title}》：{excerpt} [{passage_id}]")
    return "\n".join(lines)


def _selected_recommendation_evidence(
    search: RecommendationSearch, plan: RecommendationPlan
) -> tuple[EvidencePassage, ...]:
    if (
        not isinstance(search, RecommendationSearch)
        or not isinstance(search.total_matches, int)
        or isinstance(search.total_matches, bool)
        or search.total_matches < len(search.records)
    ):
        raise GroundingError("recommendation search response is invalid")
    candidates: list[EvidencePassage] = []
    seen_passages: set[str] = set()
    seen_movies: set[str] = set()
    for record in search.records:
        passage = _evidence_from_record(record, require_distance=True)
        if passage.source_kind != "movie_metadata":
            raise GroundingError("recommendation evidence must be movie metadata")
        if not _matches_person_constraint(passage.movie, plan):
            raise GroundingError(
                "person-qualified search returned an unrelated movie"
            )
        if not _recommendation_evidence_matches_plan(
            passage.movie_id, passage.movie, plan
        ):
            scope = (
                "scoped recommendation"
                if plan.scope_label != "metadata"
                else "recommendation"
            )
            raise GroundingError(
                f"{scope} search returned a movie outside typed constraints"
            )
        if passage.passage_id in seen_passages or passage.movie_id in seen_movies:
            continue
        seen_passages.add(passage.passage_id)
        seen_movies.add(passage.movie_id)
        candidates.append(passage)
        if len(candidates) == _MAX_PASSAGES:
            break
    if not plan.diversify_decades:
        return tuple(candidates[: plan.requested_count])

    selected: list[EvidencePassage] = []
    selected_ids: set[str] = set()
    decades: set[int] = set()
    for passage in candidates:
        decade = _movie_decade(passage.movie)
        if decade is None or decade in decades:
            continue
        decades.add(decade)
        selected.append(passage)
        selected_ids.add(passage.passage_id)
        if len(selected) == plan.requested_count:
            return tuple(selected)
    for passage in candidates:
        if passage.passage_id in selected_ids:
            continue
        selected.append(passage)
        if len(selected) == plan.requested_count:
            break
    return tuple(selected)


def _deterministic_scoped_recommendation(
    plan: RecommendationPlan,
    evidence: Sequence[EvidencePassage],
    required_citation_ids: tuple[str, ...],
) -> GeneratedAnswer:
    """Build bounded proxy disclosures from structured movie fields, never passage body."""
    _validate_scoped_plan_authority(plan)
    passage_ids = tuple(passage.passage_id for passage in evidence)
    movie_ids = tuple(passage.movie_id for passage in evidence)
    if (
        not evidence
        or len(evidence) != plan.requested_count
        or not isinstance(required_citation_ids, tuple)
        or required_citation_ids != passage_ids
        or len(set(passage_ids)) != len(passage_ids)
        or len(set(movie_ids)) != len(movie_ids)
    ):
        raise GroundingError("scoped recommendation identity is invalid")

    lines: list[str] = []
    for passage, citation_id in zip(evidence, required_citation_ids, strict=True):
        if (
            passage.source_kind != "movie_metadata"
            or _movie_text(passage.movie, "movie_id") != passage.movie_id
            or passage.movie_id in plan.excluded_movie_ids
            or not _recommendation_evidence_matches_plan(
                passage.movie_id, passage.movie, plan
            )
        ):
            raise GroundingError("scoped recommendation evidence is invalid")
        title = _recommendation_evidence_title(passage)
        reason = _scoped_recommendation_reason(plan, passage.movie)
        lines.append(f"《{title}》：{reason}[{citation_id}]")
    return GeneratedAnswer(
        answer_markdown="\n".join(lines),
        citation_ids=required_citation_ids,
    )


def _validate_scoped_plan_authority(plan: RecommendationPlan) -> None:
    if not isinstance(plan, RecommendationPlan):
        raise GroundingError("scoped recommendation plan is invalid")
    if plan.scope_label == "title_keyword_proxy":
        title_policy = controlled_title_policy(plan.title_terms_any)
        if (
            title_policy is None
            or plan.genres_all != title_policy[0]
            or plan.genres_any != title_policy[1]
            or plan.credit_group_id is not None
            or plan.credit_role is not None
            or plan.credit_exact_names
        ):
            raise GroundingError("scoped recommendation title authority is invalid")
        return
    if plan.scope_label == "controlled_credit_group":
        authority = (
            controlled_credit_group(plan.credit_group_id)
            if isinstance(plan.credit_group_id, str)
            else None
        )
        if (
            authority is None
            or plan.title_terms_any
            or plan.credit_role != authority[0]
            or plan.credit_exact_names != authority[1]
            or plan.credit_group_id
            not in {"female_directors_v1", "female_actors_v1"}
        ):
            raise GroundingError("scoped recommendation credit authority is invalid")
        return
    if plan.scope_label == "family_genre_proxy":
        if (
            plan.genres_all != ("家庭",)
            or plan.genres_any
            or plan.title_terms_any
            or plan.credit_group_id is not None
            or plan.credit_role is not None
            or plan.credit_exact_names
        ):
            raise GroundingError("scoped recommendation family authority is invalid")
        return
    raise GroundingError("scoped recommendation scope is invalid")


def _validate_metadata_recommendation_scope(plan: RecommendationPlan) -> None:
    if (
        plan.scope_label != "metadata"
        or plan.title_terms_any
        or plan.credit_group_id is not None
        or plan.credit_role is not None
        or plan.credit_exact_names
    ):
        raise GroundingError("recommendation scope is invalid")


def _recommendation_evidence_matches_plan(
    movie_id: str, movie: Mapping[str, object], plan: RecommendationPlan
) -> bool:
    if movie_id in plan.excluded_movie_ids:
        return False
    genre_tokens = {
        token.casefold() for token in _metadata_tokens(_movie_text(movie, "genre"))
    }
    explicit_genres = {genre.strip().casefold() for genre in plan.genres}
    genres_all = {genre.strip().casefold() for genre in plan.genres_all}
    genres_any = {genre.strip().casefold() for genre in plan.genres_any}
    if explicit_genres and not explicit_genres.intersection(genre_tokens):
        return False
    if not genres_all.issubset(genre_tokens):
        return False
    if genres_any and not genres_any.intersection(genre_tokens):
        return False
    if plan.tier is not None and _movie_text(movie, "tier") != plan.tier:
        return False
    if not _matches_person_constraint(movie, plan):
        return False
    if plan.title_terms_any and not _matches_title_terms(movie, plan.title_terms_any):
        return False
    if plan.credit_group_id is not None:
        authority = controlled_credit_group(plan.credit_group_id)
        if authority != (plan.credit_role, plan.credit_exact_names):
            return False
        field = "director" if plan.credit_role == "director" else "cast"
        if not set(plan.credit_exact_names).intersection(
            _metadata_tokens(_movie_text(movie, field))
        ):
            return False
    if plan.year_from is None and plan.year_to is None:
        return True
    release_date = _movie_text(movie, "release_date")
    if re.fullmatch(r"\d{4}-\d{2}-\d{2}", release_date) is None:
        return False
    release_year = int(release_date[:4])
    return not (
        plan.year_from is not None
        and release_year < plan.year_from
        or plan.year_to is not None
        and release_year > plan.year_to
    )


def _scoped_recommendation_reason(
    plan: RecommendationPlan, movie: Mapping[str, object]
) -> str:
    if plan.scope_label == "title_keyword_proxy":
        if not _matches_title_terms(movie, plan.title_terms_any):
            raise GroundingError("scoped recommendation title evidence is invalid")
        return "只因片名命中受控關鍵詞而入選；這不證明其劇情或主題包含相關元素。"

    if plan.scope_label == "controlled_credit_group":
        field = "director" if plan.credit_role == "director" else "cast"
        if not set(plan.credit_exact_names).intersection(
            _metadata_tokens(_movie_text(movie, field))
        ):
            raise GroundingError("scoped recommendation credit evidence is invalid")
        if plan.credit_group_id == "female_directors_v1":
            return (
                "導演欄位命中 Release 受控女性導演名單；"
                "這只基於欄位中的精確姓名匹配。"
            )
        if plan.credit_group_id == "female_actors_v1":
            return (
                "演員欄位命中 Release 受控女性演員名單；"
                "這只證明卡司欄位的精確姓名匹配，不代表她擔任主角。"
            )
        raise GroundingError("scoped recommendation credit group is invalid")

    if plan.scope_label == "family_genre_proxy":
        if "家庭" not in _metadata_tokens(_movie_text(movie, "genre")):
            raise GroundingError("scoped recommendation family evidence is invalid")
        return "Release 類型欄位含「家庭」；這不是年齡分級，也不證明適合所有兒童。"
    raise GroundingError("scoped recommendation scope is invalid")


def _matches_title_terms(
    movie: Mapping[str, object], title_terms: Sequence[str]
) -> bool:
    title_text = "\n".join(
        (
            _movie_text(movie, "chinese_title"),
            _movie_text(movie, "english_title"),
        )
    ).casefold()
    return bool(title_text.strip()) and any(
        term.strip().casefold() in title_text for term in title_terms
    )


def _metadata_tokens(value: str) -> set[str]:
    return {
        token.strip()
        for token in re.split(r"[、,，/;；]", value)
        if token.strip()
    }


def _deep_recommendation_plan(
    question: str,
    history: Sequence[ConversationExchange],
    *,
    _preserve_complete_current_person_continuation: bool = False,
    _force_current_reset: bool = False,
    _authority_confirmed_continuations: frozenset[ConversationExchange] = frozenset(),
    _authority_confirmed_resets: frozenset[ConversationExchange] = frozenset(),
) -> RecommendationPlan | None:
    constraints = _question_constraints(question)
    if not constraints.deep_analysis_intent:
        return None
    semantic_constraints = (
        replace(constraints, continuation=False)
        if _force_current_reset
        else constraints
        if _preserve_complete_current_person_continuation
        else _semantic_question_constraints(constraints)
    )
    bounded_history = tuple(history)[-4:]
    active_chain = (
        _latest_successful_recommendation_chain(
            bounded_history,
            authority_confirmed_continuations=_authority_confirmed_continuations,
            authority_confirmed_resets=_authority_confirmed_resets,
        )
        if semantic_constraints.continuation
        else ()
    )
    exclusion_context_chain = _recommendation_exclusion_context_chain(
        active_chain,
        authority_confirmed_continuations=_authority_confirmed_continuations,
    )
    previous = (
        _effective_recommendation_constraints(
            active_chain,
            include_deep_analysis=True,
            authority_confirmed_continuations=_authority_confirmed_continuations,
            authority_confirmed_resets=_authority_confirmed_resets,
        )
        if semantic_constraints.continuation
        else None
    )
    if not semantic_constraints.recommendation_intent and not (
        semantic_constraints.continuation and previous is not None
    ):
        return None
    effective = _merged_recommendation_constraints(
        semantic_constraints,
        previous if semantic_constraints.continuation else None,
    )
    excluded_movie_ids: tuple[str, ...] = ()
    if semantic_constraints.continuation and not (
        not _preserve_complete_current_person_continuation
        and
        constraints.person_query_shape is not None
        and constraints.person_query_shape.transition == "switch"
    ):
        excluded_movie_ids = tuple(
            dict.fromkeys(
                movie_id
                for exchange in exclusion_context_chain
                for movie_id in exchange.movie_ids
            )
        )
    context_text = (
        "\n".join(
            [
                *(exchange.question.strip() for exchange in exclusion_context_chain),
                question,
            ]
        )
        if semantic_constraints.continuation
        else question
    )
    return RecommendationPlan(
        requested_count=effective.requested_count or 5,
        genres=effective.genres,
        tier=effective.tier,
        year_from=effective.year_from,
        year_to=effective.year_to,
        diversify_decades=effective.diversify_decades,
        continuation=semantic_constraints.continuation,
        excluded_movie_ids=excluded_movie_ids,
        context_text=context_text,
        genres_all=effective.genres_all,
        genres_any=effective.genres_any,
        title_terms_any=effective.title_terms_any,
        credit_group_id=effective.credit_group_id,
        credit_role=effective.credit_role,
        credit_exact_names=effective.credit_exact_names,
        scope_label=effective.scope_label,
    )


def _selected_deep_recommendation_evidence(
    search: RecommendationSearch, plan: RecommendationPlan
) -> tuple[EvidencePassage, ...]:
    if (
        not isinstance(search, RecommendationSearch)
        or not isinstance(search.total_matches, int)
        or isinstance(search.total_matches, bool)
        or search.total_matches < len(search.records)
    ):
        raise GroundingError("deep recommendation search response is invalid")
    candidates: list[EvidencePassage] = []
    seen_passages: set[str] = set()
    seen_movies: set[str] = set()
    for record in search.records:
        passage = _evidence_from_record(record, require_distance=True)
        if (
            passage.source_kind != "pdf_page"
            or passage.passage_id in seen_passages
            or passage.movie_id in seen_movies
        ):
            raise GroundingError(
                "deep recommendation evidence must be one PDF passage per movie"
            )
        if not _recommendation_evidence_matches_plan(
            passage.movie_id, passage.movie, plan
        ):
            raise GroundingError(
                "deep recommendation returned a movie outside typed constraints"
            )
        seen_passages.add(passage.passage_id)
        seen_movies.add(passage.movie_id)
        candidates.append(passage)
        if len(candidates) > _MAX_PASSAGES:
            raise GroundingError("deep recommendation search returned too many records")
    if search.total_matches < plan.requested_count:
        if len(candidates) != search.total_matches:
            raise GroundingError(
                "deep recommendation partial result does not match authoritative total"
            )
        return tuple(candidates)
    if len(candidates) < plan.requested_count:
        raise GroundingError("deep recommendation search omitted eligible candidates")
    if not plan.diversify_decades:
        return tuple(candidates[: plan.requested_count])

    selected: list[EvidencePassage] = []
    selected_ids: set[str] = set()
    decades: set[int] = set()
    for passage in candidates:
        decade = _movie_decade(passage.movie)
        if decade is None or decade in decades:
            continue
        decades.add(decade)
        selected.append(passage)
        selected_ids.add(passage.passage_id)
        if len(selected) == plan.requested_count:
            return tuple(selected)
    for passage in candidates:
        if passage.passage_id in selected_ids:
            continue
        selected.append(passage)
        if len(selected) == plan.requested_count:
            break
    return tuple(selected)


def _insufficient_deep_recommendation(
    evidence: Sequence[EvidencePassage],
    *,
    plan: RecommendationPlan,
    total_matches: int,
    approved_poster_movie_ids: frozenset[str],
) -> ChatAnswer:
    person_detail = _person_plan_label(plan)
    if not evidence:
        detail = "、".join(
            (*((person_detail,) if person_detail is not None else ()), *plan.genres)
        ) or "目前條件"
        return ChatAnswer(
            answer_markdown=(
                f"目前沒有符合{detail}的深度文檔證據，不能以結構化資料或其他電影的"
                "分析代替。"
            ),
            citations=(),
            movies=(),
        )
    citation_ids = tuple(passage.passage_id for passage in evidence)
    citations = _build_citations(citation_ids, evidence)
    markers = "".join(f"[{citation_id}]" for citation_id in citation_ids)
    person_prefix = f"符合{person_detail}條件的" if person_detail else ""
    return ChatAnswer(
        answer_markdown=(
            f"目前{person_prefix}只有 "
            f"{total_matches} 部電影具備符合條件的深度文檔證據，少於要求的 "
            f"{plan.requested_count} 部；不會用其他電影或結構化資料補足。{markers}"
        ),
        citations=citations,
        movies=_build_movie_cards(
            tuple(dict.fromkeys(passage.movie_id for passage in evidence)),
            evidence,
            approved_poster_movie_ids=approved_poster_movie_ids,
        ),
    )


def _movie_decade(movie: Mapping[str, object]) -> int | None:
    release_date = _movie_text(movie, "release_date")
    if not re.fullmatch(r"\d{4}(?:-\d{2}-\d{2})?", release_date):
        return None
    return int(release_date[:4]) // 10 * 10


def _matches_person_constraint(
    movie: Mapping[str, object], plan: RecommendationPlan
) -> bool:
    if plan.person_name is None:
        return True
    exact_names = set(plan.person_exact_names or (plan.person_name,))
    keys = (
        ("cast",)
        if plan.person_role == "actor"
        else ("director",)
        if plan.person_role == "director"
        else ("cast", "director")
    )
    return any(
        exact_names
        & {
            token.strip()
            for token in re.split(r"[、,，/;；]", _movie_text(movie, key))
            if token.strip()
        }
        for key in keys
    )


def _person_plan_label(plan: RecommendationPlan) -> str | None:
    if plan.person_name is None:
        return None
    return f"{_person_role_label(plan.person_role)}{plan.person_name}"


def _insufficient_recommendation_matches(
    plan: RecommendationPlan, *, total_matches: int, available_count: int
) -> str:
    constraints: list[str] = []
    if person_detail := _person_plan_label(plan):
        constraints.append(person_detail)
    constraints.extend(plan.genres)
    if plan.tier is not None:
        constraints.append(f"{plan.tier}級")
    if plan.year_from is not None and plan.year_to is not None:
        constraints.append(f"{plan.year_from}–{plan.year_to}年")
    elif plan.year_from is not None:
        constraints.append(f"{plan.year_from}年以後")
    elif plan.year_to is not None:
        constraints.append(f"{plan.year_to}年以前")
    detail = "、".join(constraints) or "目前條件"
    prefix = (
        "沒有符合目前條件的結果"
        if total_matches == 0
        else "符合目前條件的結果不足"
    )
    return (
        f"{prefix}：目前條件共有 {total_matches} 個符合結果，本次有 "
        f"{available_count} 部可用候選，少於要求的 {plan.requested_count} 部"
        f"（{detail}）。可嘗試放寬其中一項條件。"
    )


def _evidence_order(
    question: str, passage: EvidencePassage, original_index: int
) -> tuple[int, int, int]:
    terms = (
        (passage.movie_id, False),
        (_movie_text(passage.movie, "chinese_title"), True),
        (_movie_text(passage.movie, "english_title"), True),
    )
    positions = [
        position
        for term, is_title in terms
        if term.strip()
        and (position := _exact_term_position(question, term, is_title=is_title)) >= 0
    ]
    if positions:
        return (0, min(positions), original_index)
    return (1, original_index, original_index)


def _evidence_from_record(
    record: Mapping[str, object], *, require_distance: bool = False
) -> EvidencePassage:
    passage_id = _required_record_text(record, "passage_id")
    movie_id = _required_record_text(record, "movie_id")
    body = _required_record_text(record, "body")
    passage_kind = _required_record_text(record, "passage_kind")
    movie = record.get("movie")
    if not isinstance(movie, Mapping):
        raise GroundingError("repository passage has no movie record")
    movie_record = dict(movie)
    if _movie_text(movie_record, "movie_id") != movie_id:
        raise GroundingError("repository passage movie identity mismatch")
    poster_available = record.get("poster_available", False)
    if not isinstance(poster_available, bool):
        raise GroundingError("repository passage poster state is invalid")
    distance: float | None = None
    if require_distance:
        raw_distance = record.get("distance")
        if (
            isinstance(raw_distance, bool)
            or not isinstance(raw_distance, (int, float))
            or not math.isfinite(float(raw_distance))
            or float(raw_distance) < 0
            or float(raw_distance) > 2
        ):
            raise GroundingError("repository passage distance is invalid")
        distance = float(raw_distance)

    if passage_kind == "metadata":
        source_kind: Literal["movie_metadata", "pdf_page"] = "movie_metadata"
        page_number = None
        source_filename = None
    elif passage_kind == "pdf":
        source_kind = "pdf_page"
        raw_page = record.get("page_number")
        if not isinstance(raw_page, int) or isinstance(raw_page, bool) or raw_page < 1:
            raise GroundingError("repository PDF passage page number is invalid")
        page_number = raw_page
        raw_filename = record.get("source_filename")
        if not isinstance(raw_filename, str) or not raw_filename:
            raise GroundingError("repository PDF passage source is invalid")
        source_filename = raw_filename
    else:
        raise GroundingError("repository passage source kind is invalid")

    return EvidencePassage(
        passage_id=passage_id,
        movie_id=movie_id,
        source_kind=source_kind,
        body=body,
        page_number=page_number,
        source_filename=source_filename,
        movie=movie_record,
        poster_available=poster_available,
        distance=distance,
    )


def _validated_generated_answer(
    generated: GeneratedAnswer,
    evidence: Sequence[EvidencePassage],
    *,
    required_citation_ids: tuple[str, ...] = (),
) -> tuple[str, tuple[str, ...]]:
    if not isinstance(generated, GeneratedAnswer):
        raise GroundingError("generation response is invalid")
    answer_markdown = generated.answer_markdown.strip()
    if not answer_markdown:
        raise GroundingError("generation response is empty")
    if not isinstance(generated.citation_ids, tuple) or not all(
        isinstance(citation_id, str) and citation_id
        for citation_id in generated.citation_ids
    ):
        raise GroundingError("generation citation list is invalid")
    citation_ids = generated.citation_ids
    if len(set(citation_ids)) != len(citation_ids):
        raise GroundingError("duplicate citation ID")
    known = {passage.passage_id for passage in evidence}
    unknown = [citation_id for citation_id in citation_ids if citation_id not in known]
    if unknown:
        raise GroundingError("unknown citation ID")
    if not citation_ids:
        raise GroundingError("uncited generated claim")
    if required_citation_ids and citation_ids != required_citation_ids:
        raise GroundingError("required recommendation citations are missing or out of order")

    markers = {f"[{citation_id}]" for citation_id in citation_ids}
    without_declared_citations = answer_markdown
    for marker in markers:
        without_declared_citations = without_declared_citations.replace(marker, "")
    if (
        _MARKDOWN_LINK.search(without_declared_citations)
        or _HTML_LINK.search(without_declared_citations)
    ):
        raise GroundingError("generated answer contains a URL or Markdown link")
    answer_markdown = _normalized_grouped_citations(answer_markdown, known)
    inline_ids = _BRACKETED_TOKEN.findall(answer_markdown)
    if any(token not in known for token in inline_ids):
        raise GroundingError("unknown citation ID")
    if set(inline_ids) != set(citation_ids):
        raise GroundingError("inline citations do not exactly match declared citation IDs")
    without_citations = answer_markdown
    for citation_id in set(inline_ids):
        without_citations = without_citations.replace(f"[{citation_id}]", "")
    if _contains_uri_or_path(without_citations):
        raise GroundingError("generated answer contains a URL or Markdown link")
    if required_citation_ids:
        answer_markdown = _normalized_recommendation_candidate_lines(
            answer_markdown, required_citation_ids
        )
        recommendation_lines = [
            line for line in answer_markdown.splitlines() if line.strip()
        ]
        if len(recommendation_lines) != len(required_citation_ids) or any(
            _BRACKETED_TOKEN.findall(line) != [required_citation_id]
            for line, required_citation_id in zip(
                recommendation_lines, required_citation_ids, strict=True
            )
        ):
            raise GroundingError(
                "recommendation answer must contain one cited line per candidate"
            )
        _validate_recommendation_line_identity(
            recommendation_lines,
            evidence,
            required_citation_ids,
        )
    without_citations = answer_markdown
    for citation_id in set(inline_ids):
        without_citations = without_citations.replace(f"[{citation_id}]", "")
    if _contains_uri_or_path(without_citations):
        raise GroundingError("generated answer contains a URL or Markdown link")
    if not any(character.isalnum() for character in without_citations):
        raise GroundingError("generated answer has no substantive text")
    for line in answer_markdown.splitlines():
        if line.strip() and not any(marker in line for marker in markers):
            raise GroundingError("uncited generated claim")
    return answer_markdown, citation_ids


def _validated_selected_pdf_citation_ids(
    generated: GeneratedAnswer,
    evidence: Sequence[EvidencePassage],
) -> tuple[str, ...]:
    """Validate Vertex's exact-target role as an ordered PDF ID selector only."""
    if not isinstance(generated, GeneratedAnswer):
        raise GroundingError("generation response is invalid")
    if not isinstance(generated.citation_ids, tuple) or not all(
        isinstance(citation_id, str) and citation_id
        for citation_id in generated.citation_ids
    ):
        raise GroundingError("generation citation list is invalid")
    citation_ids = generated.citation_ids
    if len(set(citation_ids)) != len(citation_ids):
        raise GroundingError("duplicate citation ID")
    if not citation_ids:
        raise GroundingError("qualitative answer requires PDF evidence")

    evidence_by_id = {passage.passage_id: passage for passage in evidence}
    if len(evidence_by_id) != len(evidence) or any(
        passage.source_kind != "pdf_page" for passage in evidence
    ):
        raise GroundingError("qualitative PDF evidence is invalid")
    if any(citation_id not in evidence_by_id for citation_id in citation_ids):
        raise GroundingError("unknown citation ID")
    eligible_movie_ids = {passage.movie_id for passage in evidence}
    selected_movie_ids = {
        evidence_by_id[citation_id].movie_id for citation_id in citation_ids
    }
    if len(eligible_movie_ids) != 1 or selected_movie_ids != eligible_movie_ids:
        raise GroundingError("qualitative PDF evidence must bind one exact movie")
    return citation_ids


def _selected_qualitative_citation_ids(
    generated: GeneratedAnswer,
    question: str,
    evidence: Sequence[EvidencePassage],
) -> tuple[str, ...]:
    """Constrain Vertex's page choices to deterministic question-relevant authority."""
    if (
        isinstance(generated, GeneratedAnswer)
        and isinstance(generated.citation_ids, tuple)
        and all(
            isinstance(citation_id, str) and citation_id
            for citation_id in generated.citation_ids
        )
    ):
        generated = GeneratedAnswer(
            answer_markdown=generated.answer_markdown,
            citation_ids=tuple(dict.fromkeys(generated.citation_ids)),
        )
    validated = (
        ()
        if isinstance(generated, GeneratedAnswer) and generated.citation_ids == ()
        else _validated_selected_pdf_citation_ids(generated, evidence)
    )
    try:
        deterministic = _deterministic_qualitative_citation_ids(question, evidence)
    except GroundingError as exc:
        if str(exc) == "qualitative answer requires a grounded PDF page":
            return validated
        raise
    validated_set = set(validated)
    selected = tuple(
        citation_id for citation_id in deterministic if citation_id in validated_set
    )
    if not selected:
        selected = deterministic[:1]

    required_groups = _qualitative_intent_groups(question, evidence)
    if not required_groups:
        return selected
    segments, covers_all = _best_qualitative_segment_combination(
        question,
        deterministic,
        evidence,
        required_groups=required_groups,
    )
    if not covers_all:
        return selected
    selected_set = {segment.citation_id for segment in segments}
    return tuple(
        citation_id for citation_id in deterministic if citation_id in selected_set
    )


def _qualitative_intent_groups(
    question: str, evidence: Sequence[EvidencePassage]
) -> tuple[tuple[str, ...], ...]:
    normalized = normalize_query_text(question).casefold()
    target_aliases = {
        alias
        for passage in evidence
        for alias in (
            passage.movie_id,
            _movie_text(passage.movie, "chinese_title"),
            _movie_text(passage.movie, "english_title"),
        )
        if alias
    }
    for raw_alias in sorted(target_aliases, key=len, reverse=True):
        normalized, _ = _remove_intent_phrase(
            normalized, normalize_query_text(raw_alias).casefold()
        )
    groups: list[tuple[str, ...]] = []
    for triggers, expansions in _QUALITATIVE_TERM_EXPANSIONS:
        if not any(
            normalize_query_text(trigger).casefold() in normalized
            for trigger in triggers
        ):
            continue
        active_expansions = expansions
        if triggers == ("喜劇", "喜剧") and any(
            marker in normalized for marker in _QUALITATIVE_COMEDY_MECHANISM_INTENTS
        ):
            active_expansions = tuple(
                term for term in expansions if term not in {"喜劇", "喜剧"}
            )
        groups.append(
            tuple(normalize_query_text(term).casefold() for term in active_expansions)
        )
    return tuple(groups)


def _validate_recommendation_line_identity(
    lines: Sequence[str],
    evidence: Sequence[EvidencePassage],
    required_citation_ids: tuple[str, ...],
) -> None:
    evidence_by_id = {passage.passage_id: passage for passage in evidence}
    if len(evidence_by_id) != len(evidence):
        raise GroundingError("recommendation evidence identity is invalid")
    forbidden_identities = tuple(
        identity
        for passage in evidence
        for identity in (
            passage.passage_id,
            passage.movie_id,
            _movie_text(passage.movie, "chinese_title"),
            _movie_text(passage.movie, "english_title"),
        )
        if identity
    )
    for line, citation_id in zip(lines, required_citation_ids, strict=True):
        passage = evidence_by_id.get(citation_id)
        if passage is None:
            raise GroundingError("recommendation line identity is invalid")
        title = _recommendation_evidence_title(passage)
        prefix = f"《{title}》："
        suffix = f"[{citation_id}]"
        if not line.startswith(prefix) or not line.endswith(suffix):
            raise GroundingError("recommendation line identity is invalid")
        reason = line[len(prefix) : -len(suffix)].strip()
        if (
            not reason
            or len(reason) > _MAX_RECOMMENDATION_REASON_CHARACTERS + 1
            or not any(character.isalnum() for character in reason)
            or _BRACKETED_TOKEN.search(reason) is not None
            or has_forbidden_identity_controls(reason)
            or contains_adversarial_identity(reason, forbidden_identities)
        ):
            raise GroundingError("recommendation line identity is invalid")


def _recommendation_evidence_title(passage: EvidencePassage) -> str:
    title = _movie_text(passage.movie, "chinese_title") or _movie_text(
        passage.movie, "english_title"
    )
    if not title.strip() or any(character in title for character in "\r\n《》"):
        raise GroundingError("recommendation evidence title is invalid")
    return title


def _normalized_recommendation_candidate_lines(
    answer_markdown: str, required_citation_ids: tuple[str, ...]
) -> str:
    nonempty_lines = [line for line in answer_markdown.splitlines() if line.strip()]
    if len(required_citation_ids) < 2 or len(nonempty_lines) != 1:
        return answer_markdown

    marker_matches = tuple(_BRACKETED_TOKEN.finditer(answer_markdown))
    if tuple(match.group(1) for match in marker_matches) != required_citation_ids:
        return answer_markdown

    candidate_lines: list[str] = []
    candidate_start = 0
    for marker_match in marker_matches:
        candidate_text = answer_markdown[candidate_start : marker_match.start()]
        if not any(character.isalnum() for character in candidate_text):
            return answer_markdown
        candidate_lines.append(
            answer_markdown[candidate_start : marker_match.end()].strip()
        )
        candidate_start = marker_match.end()

    trailing_text = answer_markdown[candidate_start:]
    if any(character.isalnum() for character in trailing_text):
        return answer_markdown
    candidate_lines[-1] = f"{candidate_lines[-1]}{trailing_text}"
    return "\n".join(candidate_lines)


def _normalized_grouped_citations(answer_markdown: str, known: set[str]) -> str:
    def normalize(match: re.Match[str]) -> str:
        token = match.group(1)
        if token in known:
            return match.group(0)
        parts = re.split(r"[,，]", token)
        citation_ids = tuple(part.strip() for part in parts)
        if len(citation_ids) < 2 or any(
            not citation_id or citation_id not in known for citation_id in citation_ids
        ):
            return match.group(0)
        return "".join(f"[{citation_id}]" for citation_id in citation_ids)

    return _BRACKETED_TOKEN.sub(normalize, answer_markdown)


def _build_citations(
    citation_ids: Sequence[str],
    evidence: Sequence[EvidencePassage],
    *,
    excerpt_terms_by_id: Mapping[str, Sequence[str]] | None = None,
) -> tuple[Citation, ...]:
    evidence_by_id = {passage.passage_id: passage for passage in evidence}
    citations: list[Citation] = []
    for citation_id in citation_ids:
        passage = evidence_by_id[citation_id]
        movie_title = _movie_text(passage.movie, "chinese_title") or _movie_text(
            passage.movie, "english_title"
        )
        citations.append(
            Citation(
                citation_id=citation_id,
                movie_id=passage.movie_id,
                movie_title=movie_title or passage.movie_id,
                source_kind=passage.source_kind,
                page_number=passage.page_number,
                source_filename=passage.source_filename,
                excerpt=(
                    _excerpt(passage.body)
                    if excerpt_terms_by_id is None
                    else _relevant_excerpt(
                        passage.body,
                        excerpt_terms_by_id.get(citation_id, ()),
                    )
                ),
            )
        )
    return tuple(citations)


def _build_movie_cards(
    movie_ids: Sequence[str],
    evidence: Sequence[EvidencePassage],
    *,
    approved_poster_movie_ids: frozenset[str] | None = None,
) -> tuple[MovieCard, ...]:
    selected_movie_ids = dict.fromkeys(movie_ids)
    cards: list[MovieCard] = []
    for movie_id in selected_movie_ids:
        movie_passages = [passage for passage in evidence if passage.movie_id == movie_id]
        if not movie_passages:
            raise GroundingError("citation movie record is unavailable")
        movie = movie_passages[0].movie
        poster_available = (
            movie_id in approved_poster_movie_ids
            if approved_poster_movie_ids is not None
            else any(passage.poster_available for passage in movie_passages)
        )
        tier = _movie_text(movie, "tier")
        pilot_movie = movie.get("pilot_movie")
        if tier not in {"S", "A", "B"} or not isinstance(pilot_movie, bool):
            raise GroundingError("movie facet record is invalid")
        cards.append(
            MovieCard(
                movie_id=movie_id,
                chinese_title=_movie_text(movie, "chinese_title"),
                english_title=_movie_text(movie, "english_title"),
                release_date=_movie_text(movie, "release_date"),
                director=_movie_text(movie, "director"),
                cast=_movie_text(movie, "cast"),
                genre=_movie_text(movie, "genre"),
                tier=tier,
                pilot_movie=pilot_movie,
                poster_url=(
                    f"/api/posters/{quote(movie_id, safe='')}" if poster_available else None
                ),
            )
        )
    return tuple(cards)


def _required_record_text(record: Mapping[str, object], key: str) -> str:
    value = record.get(key)
    if not isinstance(value, str) or not value:
        raise GroundingError(f"repository passage field is invalid: {key}")
    return value


def _movie_text(movie: Mapping[str, object], key: str) -> str:
    value = movie.get(key)
    return value if isinstance(value, str) else ""


def _excerpt(body: str, limit: int = 360) -> str:
    normalized = _normalized_excerpt_text(body)
    if len(normalized) <= limit:
        return normalized
    return f"{normalized[: limit - 1].rstrip()}…"


def _normalized_excerpt_text(body: str) -> str:
    return _CJK_INTERCHAR_WHITESPACE.sub("", " ".join(body.split()))


def _relevant_excerpt(
    body: str, required_terms: Sequence[str], limit: int = 360
) -> str:
    normalized = _normalized_excerpt_text(body)
    terms = tuple(term.strip() for term in required_terms if term.strip())
    if not normalized or not terms or not 0 < limit <= 360:
        raise GroundingError("bounded analysis excerpt anchors are invalid")

    search_text = normalized
    matches: list[tuple[int, int, int]] = []
    for term_index, term in enumerate(terms):
        needle = term
        start = 0
        found = False
        while (position := search_text.find(needle, start)) >= 0:
            found = True
            matches.append((position, position + len(needle), term_index))
            start = position + max(1, len(needle))
        if not found:
            raise GroundingError("bounded analysis excerpt anchor is missing")

    matches.sort()
    counts = [0] * len(terms)
    covered = 0
    left = 0
    best: tuple[int, int] | None = None
    for right, (_, _, term_index) in enumerate(matches):
        if counts[term_index] == 0:
            covered += 1
        counts[term_index] += 1
        while covered == len(terms):
            window = matches[left : right + 1]
            core_start = window[0][0]
            core_end = max(match[1] for match in window)
            candidate = (core_end - core_start, core_start)
            if best is None or candidate < (best[1] - best[0], best[0]):
                best = (core_start, core_end)
            left_term_index = matches[left][2]
            counts[left_term_index] -= 1
            if counts[left_term_index] == 0:
                covered -= 1
            left += 1

    if best is None:
        raise GroundingError("bounded analysis excerpt anchors are incomplete")
    core_start, core_end = best
    ellipsis_budget = int(core_start > 0) + int(core_end < len(normalized))
    content_limit = limit - ellipsis_budget
    if core_end - core_start > content_limit:
        raise GroundingError("bounded analysis excerpt anchors are too far apart")

    remaining = content_limit - (core_end - core_start)
    left_context = min(core_start, remaining // 2)
    excerpt_start = core_start - left_context
    remaining -= left_context
    right_context = min(len(normalized) - core_end, remaining)
    excerpt_end = core_end + right_context
    remaining -= right_context
    if remaining and excerpt_start > 0:
        extra_left = min(excerpt_start, remaining)
        excerpt_start -= extra_left

    excerpt = normalized[excerpt_start:excerpt_end].strip()
    if excerpt_start > 0:
        excerpt = f"…{excerpt}"
    if excerpt_end < len(normalized):
        excerpt = f"{excerpt}…"
    if len(excerpt) > limit or any(term not in excerpt for term in terms):
        raise GroundingError("bounded analysis excerpt failed its final contract")
    return excerpt


def _contains_uri_or_path(text: str) -> bool:
    return any(
        pattern.search(text) is not None
        for pattern in (
            _URI_SCHEME,
            _DOMAIN,
            _PATH_REFERENCE,
        )
    )


def _exact_target_authority(
    question: str,
    evidence: Sequence[EvidencePassage],
    *,
    resolved_target_movie_ids: Sequence[str] = (),
) -> _ExactTargetAuthority | None:
    """Partition exact-target evidence without crossing canonical/PDF authority."""
    exact_targets = tuple(resolved_target_movie_ids) or _exact_target_movie_ids(
        question, evidence
    )
    if not exact_targets:
        return None
    singular_target = len(exact_targets) == 1
    target_ids = set(exact_targets)
    metadata = tuple(
        passage
        for passage in evidence
        if passage.movie_id in target_ids and passage.source_kind == "movie_metadata"
    )
    qualitative = tuple(
        passage
        for passage in evidence
        if passage.movie_id in target_ids and passage.source_kind == "pdf_page"
    )

    requested_fields = _canonical_metadata_intents(question, exact_targets, evidence)
    qualitative_question: str | None = None
    if requested_fields is None and has_deep_analysis_intent(question):
        requested_fields, qualitative_question = _split_mixed_target_question(
            question, exact_targets, evidence
        )
    if requested_fields is not None:
        if qualitative_question is not None and not singular_target:
            return _ExactTargetAuthority(
                requested_fields, metadata, (), None, blocked=True
            )
        if {passage.movie_id for passage in metadata} != target_ids:
            raise GroundingError("exact metadata target has no metadata passage")
        if not _metadata_fields_cover_targets(
            requested_fields, target_ids, metadata
        ):
            return _ExactTargetAuthority(
                requested_fields, metadata, (), None, blocked=True
            )
        if qualitative_question is not None:
            if not qualitative:
                return _ExactTargetAuthority(
                    requested_fields, metadata, (), None, blocked=True
                )
            return _ExactTargetAuthority(
                requested_fields,
                metadata,
                qualitative,
                qualitative_question,
            )
        return _ExactTargetAuthority(requested_fields, metadata, (), None)

    if _mentioned_canonical_fields(question, exact_targets, evidence):
        if not singular_target:
            return _ExactTargetAuthority(
                frozenset(), metadata, (), None, blocked=True
            )
        if {passage.movie_id for passage in metadata} != target_ids:
            raise GroundingError("exact metadata target has no metadata passage")
        return _ExactTargetAuthority(frozenset(), metadata, (), None, blocked=True)
    if not singular_target:
        return None
    if not qualitative:
        return _ExactTargetAuthority(frozenset(), metadata, (), None, blocked=True)
    return _ExactTargetAuthority(
        frozenset(), metadata, qualitative, question
    )


def _split_mixed_target_question(
    question: str,
    exact_targets: Sequence[str],
    evidence: Sequence[EvidencePassage],
) -> tuple[frozenset[str] | None, str | None]:
    clauses = tuple(
        clause.strip()
        for clause in re.split(r"(?:[,，;；。]|並且|并且|並|并)", question)
        if clause.strip()
    )
    canonical_fields: set[str] = set()
    qualitative_clauses: list[str] = []
    for clause in clauses:
        fields = _canonical_metadata_intents(clause, exact_targets, evidence)
        if fields is not None and not has_deep_analysis_intent(clause):
            canonical_fields.update(fields)
            continue
        conjunction_parts = _safe_authority_conjunction_parts(
            clause, exact_targets, evidence
        )
        if len(conjunction_parts) == 1:
            qualitative_clauses.append(clause)
            continue
        for part in conjunction_parts:
            part_fields = _canonical_metadata_intents(
                part, exact_targets, evidence
            )
            if part_fields is not None and not has_deep_analysis_intent(part):
                canonical_fields.update(part_fields)
            else:
                qualitative_clauses.append(part)
    qualitative_question = "；".join(qualitative_clauses).strip()
    if (
        not canonical_fields
        or not qualitative_question
        or not has_deep_analysis_intent(qualitative_question)
    ):
        return None, None
    return frozenset(canonical_fields), qualitative_question


def _safe_authority_conjunction_parts(
    clause: str,
    exact_targets: Sequence[str],
    evidence: Sequence[EvidencePassage],
) -> tuple[str, ...]:
    """Split field conjunctions while preserving conjunctions inside exact titles."""
    folded = clause.casefold()
    protected_spans: list[tuple[int, int]] = []
    aliases: set[str] = set()
    for passage in evidence:
        if passage.movie_id not in exact_targets:
            continue
        aliases.update(
            {
                passage.movie_id,
                _movie_text(passage.movie, "chinese_title"),
                _movie_text(passage.movie, "english_title"),
            }
        )
    for alias in aliases:
        normalized_alias = alias.casefold()
        if not normalized_alias:
            continue
        start = folded.find(normalized_alias)
        while start >= 0:
            protected_spans.append((start, start + len(normalized_alias)))
            start = folded.find(normalized_alias, start + len(normalized_alias))

    boundaries = [0]
    for match in re.finditer(
        r"(?i)以及|和|跟|及|與|与|(?<![a-z0-9_])and(?![a-z0-9_])", clause
    ):
        if any(start <= match.start() < end for start, end in protected_spans):
            continue
        boundaries.extend((match.start(), match.end()))
    boundaries.append(len(clause))
    if len(boundaries) == 2:
        return (clause,)
    return tuple(
        clause[boundaries[index] : boundaries[index + 1]].strip()
        for index in range(0, len(boundaries) - 1, 2)
        if clause[boundaries[index] : boundaries[index + 1]].strip()
    )


def _mentioned_canonical_fields(
    question: str,
    exact_targets: Sequence[str],
    evidence: Sequence[EvidencePassage],
) -> frozenset[str]:
    residual = normalize_query_text(_without_history_reference(question)).casefold()
    aliases: set[str] = set()
    for passage in evidence:
        if passage.movie_id not in exact_targets:
            continue
        aliases.update(
            {
                passage.movie_id,
                _movie_text(passage.movie, "chinese_title"),
                _movie_text(passage.movie, "english_title"),
            }
        )
    for alias in sorted((item for item in aliases if item), key=len, reverse=True):
        normalized_alias = normalize_query_text(alias).casefold()
        residual, _ = _remove_intent_phrase(residual, normalized_alias)
    return frozenset(
        field
        for field, aliases in _METADATA_INTENT_ALIASES
        if any(_normalized_alias_occurs(residual, alias) for alias in aliases)
    )


def _normalized_alias_occurs(normalized_text: str, alias: str) -> bool:
    normalized_alias = normalize_query_text(alias).casefold()
    if normalized_alias.isascii():
        return (
            re.search(
                rf"(?<![a-z0-9_]){re.escape(normalized_alias)}(?![a-z0-9_])",
                normalized_text,
            )
            is not None
        )
    return normalized_alias in normalized_text


def _canonical_metadata_intents(
    question: str,
    exact_targets: Sequence[str],
    evidence: Sequence[EvidencePassage],
) -> frozenset[str] | None:
    residual = normalize_query_text(_without_history_reference(question)).casefold()
    aliases: set[str] = set()
    for passage in evidence:
        if passage.movie_id not in exact_targets:
            continue
        aliases.update(
            {
                passage.movie_id,
                _movie_text(passage.movie, "chinese_title"),
                _movie_text(passage.movie, "english_title"),
            }
        )
    normalized_target_aliases = {
        normalize_query_text(alias).casefold() for alias in aliases if alias
    }
    for alias in sorted(normalized_target_aliases, key=len, reverse=True):
        residual, _ = _remove_intent_phrase(residual, alias)

    requested: set[str] = set()
    intent_aliases = sorted(
        (
            (normalize_query_text(alias).casefold(), field)
            for field, field_aliases in _METADATA_INTENT_ALIASES
            for alias in field_aliases
        ),
        key=lambda item: len(item[0]),
        reverse=True,
    )
    for alias, field in intent_aliases:
        residual, found = _remove_intent_phrase(residual, alias)
        if found:
            requested.add(field)
    if not requested:
        return None

    safe_chinese_phrases = {
        normalize_query_text(phrase).casefold() for phrase in _SAFE_METADATA_CHINESE
    }
    for phrase in sorted(safe_chinese_phrases, key=len, reverse=True):
        residual = residual.replace(phrase, " ")
    word_tokens = re.findall(r"[a-z0-9_]+|[\u3400-\u9fff]+", residual)
    for token in word_tokens:
        if token.isascii() and (token.isdigit() or token in _SAFE_METADATA_ENGLISH):
            continue
        return None
    without_words = re.sub(r"[a-z0-9_]+|[\u3400-\u9fff]+", "", residual)
    if any(character.isalnum() for character in without_words):
        return None
    return frozenset(requested)


def _remove_intent_phrase(text: str, phrase: str) -> tuple[str, bool]:
    if phrase.isascii():
        pattern = re.compile(rf"(?<![a-z0-9_]){re.escape(phrase)}(?![a-z0-9_])")
        updated, count = pattern.subn(" ", text)
        return updated, bool(count)
    found = phrase in text
    return text.replace(phrase, " "), found


def _metadata_fields_cover_targets(
    requested_fields: Collection[str],
    target_ids: set[str],
    metadata_evidence: Sequence[EvidencePassage],
) -> bool:
    required = set(requested_fields) - {"__overview__"}
    for movie_id in target_ids:
        present: set[str] = set()
        for passage in metadata_evidence:
            if passage.movie_id == movie_id:
                present.update(_present_metadata_fields(passage.body))
        if not present or not required.issubset(present):
            return False
    return True


_CANONICAL_FIELD_LABELS: Mapping[str, str] = {
    "movie_id": "電影編號",
    "chinese_title": "中文片名",
    "english_title": "英文片名",
    "release_date": "上映日期",
    "production_region": "製作地區",
    "director": "導演",
    "screenwriter": "編劇",
    "cast": "主演",
    "genre": "類型",
    "runtime_minutes": "片長（分鐘）",
    "production_company": "製作公司",
    "data_source": "資料來源",
    "language": "語言",
    "rating": "評分",
}
_CANONICAL_FIELD_ORDER = tuple(_CANONICAL_FIELD_LABELS)
_MINUTE_DURATION_PATTERN = re.compile(
    r"(?i)(?:\d+(?:\.\d+)?|[零〇一二兩两三四五六七八九十百千]+)\s*"
    r"(?:分鐘|分钟|mins?|minutes?)"
)
_QUALITATIVE_INSUFFICIENCY = "目前沒有可安全抽取的定性分析證據。"
_MAX_QUALITATIVE_EXCERPT_CHARACTERS = 240
_MAX_QUALITATIVE_LINES = 2
_QUALITATIVE_SEGMENT_PATTERN = re.compile(r"[^。！？!?；;]+(?:[。！？!?；;]+|$)")
_QUALITATIVE_FIELD_PREFIXES = frozenset(
    {
        "",
        "本片",
        "該片",
        "该片",
        "電影",
        "电影",
        "影片",
        "另稱",
        "另称",
        "分析稱",
        "分析称",
        "文中稱",
        "文中称",
        "資料稱",
        "资料称",
    }
)
_QUALITATIVE_FIELD_CLAIM_CUES = (
    ":",
    "：",
    "=",
    "是",
    "為",
    "为",
    "由",
    "有",
    "還有",
    "还有",
    "包括",
    "包含",
    "共",
    "約",
    "约",
    "達",
    "达",
    "在",
    "於",
    "于",
)
_QUALITATIVE_FIELD_ANALYSIS_SUFFIXES = (
    "手法",
    "風格",
    "风格",
    "視角",
    "视角",
    "調度",
    "调度",
    "設計",
    "设计",
    "選擇",
    "选择",
    "處理",
    "处理",
    "語言",
    "语言",
    "美學",
    "美学",
    "演技",
    "表演",
    "作用",
    "融合",
    "張力",
    "张力",
    "之後",
    "之后",
    "後",
    "后",
)
_QUALITATIVE_COMEDY_MECHANISM_INTENTS = (
    "喜劇節奏",
    "喜剧节奏",
    "喜劇風格",
    "喜剧风格",
    "喜劇機制",
    "喜剧机制",
    "喜劇創作",
    "喜剧创作",
)
_QUALITATIVE_TERM_EXPANSIONS: tuple[tuple[tuple[str, ...], tuple[str, ...]], ...] = (
    (
        ("動作", "动作", "武打", "功夫"),
        (
            "動作",
            "动作",
            "武打",
            "功夫",
            "武術",
            "武术",
            "招式",
            "拳腳",
            "拳脚",
            "雜耍",
            "杂耍",
        ),
    ),
    (
        ("喜劇", "喜剧"),
        (
            "喜劇",
            "喜剧",
            "笑點",
            "笑点",
            "詼諧",
            "诙谐",
            "諧趣",
            "谐趣",
            "喜感",
            "笑料",
            "荒誕",
            "荒诞",
            "雜耍",
            "杂耍",
        ),
    ),
    (
        ("視覺", "视觉"),
        ("畫面", "画面", "構圖", "构图", "鏡頭", "镜头", "空間", "空间", "色彩", "光影"),
    ),
    (
        ("聲音", "声音", "聲效", "声效"),
        (
            "聲場",
            "声场",
            "聲效",
            "声效",
            "音樂",
            "音乐",
            "音響",
            "音响",
            "配樂",
            "配乐",
            "電子琴",
            "电子琴",
            "環境聲",
            "环境声",
            "樂器",
            "乐器",
            "噪音",
        ),
    ),
    (
        ("節奏", "节奏"),
        (
            "節拍",
            "节拍",
            "拍節",
            "拍节",
            "鑼鼓",
            "锣鼓",
            "聲畫",
            "声画",
            "音效",
            "戲曲",
            "戏曲",
        ),
    ),
    (
        ("象徵", "象征", "寓意"),
        ("象徵", "象征", "隱喻", "隐喻", "寓意"),
    ),
)


def _canonical_metadata_answer(
    metadata: Sequence[EvidencePassage],
    requested_fields: Collection[str],
) -> tuple[str, tuple[str, ...]]:
    lines: list[str] = []
    citation_ids: list[str] = []
    seen_movies: set[str] = set()
    for passage in metadata:
        if passage.movie_id in seen_movies:
            continue
        seen_movies.add(passage.movie_id)
        values = _canonical_metadata_values(passage.body)
        fields = (
            tuple(field for field in _CANONICAL_FIELD_ORDER if field in values)
            if "__overview__" in requested_fields
            else tuple(
                field
                for field in _CANONICAL_FIELD_ORDER
                if field in requested_fields
            )
        )
        if not fields or any(field not in values for field in fields):
            raise GroundingError("canonical metadata answer is incomplete")
        facts = "；".join(
            f"{_CANONICAL_FIELD_LABELS[field]}：{values[field]}" for field in fields
        )
        title = _movie_text(passage.movie, "chinese_title") or passage.movie_id
        lines.append(f"《{title}》{facts}。[{passage.passage_id}]")
        citation_ids.append(passage.passage_id)
    return "\n".join(lines), tuple(citation_ids)


def _canonical_metadata_values(body: str) -> dict[str, str]:
    values: dict[str, str] = {}
    for line in body.splitlines():
        field, separator, value = line.partition(":")
        normalized = field.strip()
        normalized_value = value.strip()
        if (
            separator
            and normalized in _CANONICAL_METADATA_FIELDS
            and normalized_value
        ):
            existing = values.setdefault(normalized, normalized_value)
            if existing != normalized_value:
                raise GroundingError("canonical metadata passage is inconsistent")
    return values


def _qualitative_segment_candidates(
    question: str,
    citation_ids: Sequence[str],
    evidence: Sequence[EvidencePassage],
    required_groups: tuple[tuple[str, ...], ...],
) -> tuple[_QualitativeSegmentCandidate, ...]:
    evidence_by_id = {passage.passage_id: passage for passage in evidence}
    query_terms = _qualitative_query_terms(question, evidence)
    candidates: list[_QualitativeSegmentCandidate] = []
    for passage_order, citation_id in enumerate(citation_ids):
        passage = evidence_by_id[citation_id]
        normalized_source = _normalized_qualitative_source(passage.body)
        for segment_order, match in enumerate(
            _QUALITATIVE_SEGMENT_PATTERN.finditer(normalized_source)
        ):
            segment = match.group(0).strip()
            if not _safe_qualitative_segment(segment, normalized_source):
                continue
            normalized_segment = normalize_query_text(segment).casefold()
            matched_terms = frozenset(
                term for term in query_terms if term in normalized_segment
            )
            candidates.append(
                _QualitativeSegmentCandidate(
                    citation_id=citation_id,
                    passage_order=passage_order,
                    segment_order=segment_order,
                    text=segment,
                    score=sum(len(term) for term in matched_terms),
                    matched_terms=matched_terms,
                    covered_groups=frozenset(
                        index
                        for index, group in enumerate(required_groups)
                        if any(term in normalized_segment for term in group)
                    ),
                )
            )
    return tuple(candidates)


def _best_qualitative_segment_combination(
    question: str,
    citation_ids: Sequence[str],
    evidence: Sequence[EvidencePassage],
    *,
    required_groups: tuple[tuple[str, ...], ...] | None = None,
) -> tuple[tuple[_QualitativeSegmentCandidate, ...], bool]:
    groups = (
        _qualitative_intent_groups(question, evidence)
        if required_groups is None
        else required_groups
    )
    candidates = _qualitative_segment_candidates(
        question, citation_ids, evidence, groups
    )
    if not candidates:
        return (), False
    literal_anchors = frozenset(
        term
        for term in _qualitative_query_terms(question, evidence)
        if re.fullmatch(r"[a-z0-9_]+", term)
    )
    anchored = [
        candidate
        for candidate in candidates
        if literal_anchors
        and literal_anchors.issubset(candidate.matched_terms)
    ]
    if anchored:
        selected_anchor = min(
            anchored,
            key=lambda candidate: (
                -candidate.score,
                candidate.passage_order,
                candidate.segment_order,
            ),
        )
        return (selected_anchor,), True
    options: list[tuple[_QualitativeSegmentCandidate, ...]] = [
        (candidate,) for candidate in candidates
    ]
    options.extend(
        (left, right)
        for left_index, left in enumerate(candidates)
        for right in candidates[left_index + 1 :]
    )
    required_group_indexes = frozenset(range(len(groups)))

    def covered(option: tuple[_QualitativeSegmentCandidate, ...]) -> bool:
        present = frozenset(
            group
            for candidate in option
            for group in candidate.covered_groups
        )
        return required_group_indexes.issubset(present)

    complete = [option for option in options if covered(option)]

    if not complete:
        ranked_candidates = sorted(
            candidates,
            key=lambda candidate: (
                -candidate.score,
                candidate.passage_order,
                candidate.segment_order,
            ),
        )
        selected_fallback: list[_QualitativeSegmentCandidate] = []
        selected_citations: set[str] = set()
        for candidate in ranked_candidates:
            if candidate.citation_id in selected_citations:
                continue
            selected_fallback.append(candidate)
            selected_citations.add(candidate.citation_id)
            if len(selected_fallback) == _MAX_QUALITATIVE_LINES:
                break
        for candidate in ranked_candidates:
            if len(selected_fallback) == _MAX_QUALITATIVE_LINES:
                break
            if candidate not in selected_fallback:
                selected_fallback.append(candidate)
        return (
            tuple(
                sorted(
                    selected_fallback,
                    key=lambda candidate: (
                        candidate.passage_order,
                        candidate.segment_order,
                    ),
                )
            ),
            False,
        )

    def rank(
        option: tuple[_QualitativeSegmentCandidate, ...],
    ) -> tuple[object, ...]:
        matched_terms = {
            term for candidate in option for term in candidate.matched_terms
        }
        return (
            len({candidate.citation_id for candidate in option}),
            -sum(len(term) for term in matched_terms),
            len(option),
            tuple(
                (candidate.passage_order, candidate.segment_order)
                for candidate in option
            ),
        )

    selected = min(complete, key=rank)
    selected = tuple(
        sorted(
            selected,
            key=lambda candidate: (
                candidate.passage_order,
                candidate.segment_order,
            ),
        )
    )
    return selected, bool(complete)


def _extractive_qualitative_answer(
    question: str,
    citation_ids: Sequence[str],
    evidence: Sequence[EvidencePassage],
) -> tuple[str, tuple[str, ...]]:
    evidence_by_id = {passage.passage_id: passage for passage in evidence}
    eligible_movie_ids = {passage.movie_id for passage in evidence}
    selected_movie_ids = {
        evidence_by_id[citation_id].movie_id
        for citation_id in citation_ids
        if citation_id in evidence_by_id
    }
    if (
        len(evidence_by_id) != len(evidence)
        or not citation_ids
        or len(set(citation_ids)) != len(citation_ids)
        or any(citation_id not in evidence_by_id for citation_id in citation_ids)
        or any(
            evidence_by_id[citation_id].source_kind != "pdf_page"
            for citation_id in citation_ids
        )
        or len(eligible_movie_ids) != 1
        or selected_movie_ids != eligible_movie_ids
    ):
        raise GroundingError("qualitative PDF selection must bind one exact movie")

    segments, _ = _best_qualitative_segment_combination(
        question, citation_ids, evidence
    )
    selected_lines: list[str] = []
    selected_ids: list[str] = []
    for segment in segments:
        selected_lines.append(f"{segment.text}[{segment.citation_id}]")
        if segment.citation_id not in selected_ids:
            selected_ids.append(segment.citation_id)

    if not selected_lines:
        return _QUALITATIVE_INSUFFICIENCY, ()
    first_passage = evidence_by_id[selected_ids[0]]
    title = _movie_text(first_passage.movie, "chinese_title") or first_passage.movie_id
    selected_lines[0] = f"《{title}》提供的分析指出：{selected_lines[0]}"
    return "\n".join(selected_lines), tuple(selected_ids)


def _deterministic_qualitative_citation_ids(
    question: str,
    evidence: Sequence[EvidencePassage],
) -> tuple[str, ...]:
    evidence_by_id = {passage.passage_id: passage for passage in evidence}
    if (
        len(evidence_by_id) != len(evidence)
        or any(passage.source_kind != "pdf_page" for passage in evidence)
        or len({passage.movie_id for passage in evidence}) != 1
    ):
        raise GroundingError("qualitative PDF fallback evidence is invalid")

    query_terms = _qualitative_query_terms(question, evidence)
    ranked: list[tuple[int, int, str]] = []
    for passage_index, passage in enumerate(evidence):
        normalized_source = _normalized_qualitative_source(passage.body)
        best_score = 0
        for match in _QUALITATIVE_SEGMENT_PATTERN.finditer(normalized_source):
            segment = match.group(0).strip()
            if not _safe_qualitative_segment(segment, normalized_source):
                continue
            normalized_segment = normalize_query_text(segment).casefold()
            best_score = max(
                best_score,
                sum(len(term) for term in query_terms if term in normalized_segment),
            )
        if best_score:
            ranked.append((-best_score, passage_index, passage.passage_id))
    if not ranked:
        raise GroundingError("qualitative answer requires a grounded PDF page")

    ordered = sorted(ranked)
    strongest_score = -ordered[0][0]
    selected = {
        passage_id
        for negative_score, _, passage_id in ordered
        if (-negative_score * 2) > strongest_score
    }
    if len(selected) > _MAX_QUALITATIVE_LINES:
        selected = {
            passage_id
            for _, _, passage_id in ordered[:_MAX_QUALITATIVE_LINES]
        }
    return tuple(
        passage.passage_id for passage in evidence if passage.passage_id in selected
    )


def _normalized_qualitative_source(body: str) -> str:
    normalized_newlines = body.replace("\r\n", "\n").replace("\r", "\n")
    layout_bounded = re.sub(
        (
            r"(?m)^(?=(?:片名|年份|導演|导演|主演|類型標籤|类型标签|"
            r"開創性定位|开创性定位|影片來源|影片来源|視覺美學|视觉美学|"
            r"敘事美學|叙事美学|聲音美學|声音美学|一級類型|一级类型|"
            r"二級風格|二级风格|三級特徵|三级特征)\s*[：:])"
        ),
        "。",
        normalized_newlines,
    )
    # PDF text extraction can insert blank lines inside one English token (for
    # example ``zoom\n\nin``).  Treating every blank line as a sentence boundary
    # creates fabricated punctuation and lets an extract start mid-clause.  An
    # explicit section heading is a reliable boundary; all other layout
    # whitespace is collapsed without inventing prose punctuation.
    section_bounded = re.sub(
        r"(?<![。！？!?；;])\n\s*\n+(?=\s*(?:【|#{1,6}(?:\s|$)))",
        "。",
        layout_bounded,
    )
    return _normalized_excerpt_text(section_bounded)


def _safe_qualitative_segment(segment: str, normalized_source: str) -> bool:
    return bool(
        segment
        and segment in normalized_source
        and len(segment) <= _MAX_QUALITATIVE_EXCERPT_CHARACTERS
        and any(character.isalnum() for character in segment)
        and _BRACKETED_TOKEN.search(segment) is None
        and _MINUTE_DURATION_PATTERN.search(segment) is None
        and not _contains_uri_or_path(segment)
        and not _has_explicit_metadata_field_claim(segment)
    )


def _has_explicit_metadata_field_claim(segment: str) -> bool:
    normalized = normalize_query_text(segment).casefold().strip(
        " \t\r\n-*#•·|()（）[]【】"
    )
    labels: set[str] = set()
    for field, aliases in _METADATA_INTENT_ALIASES:
        if field == "__overview__":
            continue
        labels.update((field, field.replace("_", " "), *aliases))
        display_label = _CANONICAL_FIELD_LABELS.get(field)
        if display_label is not None:
            labels.add(display_label)

    for raw_label in sorted(labels, key=len, reverse=True):
        label = normalize_query_text(raw_label).casefold()
        pattern = (
            re.compile(rf"(?<![a-z0-9_]){re.escape(label)}(?![a-z0-9_])")
            if label.isascii()
            else re.compile(re.escape(label))
        )
        for match in pattern.finditer(normalized):
            prefix = re.sub(
                r"[\s\-—–:：,，。;；()（）【】\[\]#*]+",
                "",
                normalized[: match.start()],
            )
            tail = normalized[match.end() :].lstrip()
            if tail.startswith(_QUALITATIVE_FIELD_ANALYSIS_SUFFIXES):
                continue
            if (
                not tail
                or tail.startswith(_QUALITATIVE_FIELD_CLAIM_CUES)
                or (tail and tail[0].isdigit())
                or prefix in _QUALITATIVE_FIELD_PREFIXES
            ):
                return True
    return False


def _qualitative_query_terms(
    question: str, evidence: Sequence[EvidencePassage]
) -> tuple[str, ...]:
    normalized = normalize_query_text(question).casefold()
    target_aliases = {
        alias
        for passage in evidence
        for alias in (
            passage.movie_id,
            _movie_text(passage.movie, "chinese_title"),
            _movie_text(passage.movie, "english_title"),
        )
        if alias
    }
    for raw_alias in sorted(target_aliases, key=len, reverse=True):
        alias = normalize_query_text(raw_alias).casefold()
        normalized, _ = _remove_intent_phrase(normalized, alias)

    terms: dict[str, None] = {}

    def add_term(raw_term: str) -> None:
        term = normalize_query_text(raw_term).casefold().strip()
        if 1 < len(term) <= 24 and len(terms) < 128:
            terms.setdefault(term, None)

    for triggers, expansions in _QUALITATIVE_TERM_EXPANSIONS:
        if any(normalize_query_text(trigger).casefold() in normalized for trigger in triggers):
            for expansion in expansions:
                add_term(expansion)
    for token in re.findall(r"[a-z0-9_]+", normalized):
        add_term(token)
    for run in re.findall(r"[\u3400-\u9fff]+", normalized):
        for width in (4, 3, 2):
            for start in range(len(run) - width + 1):
                add_term(run[start : start + width])
    return tuple(terms)


def _present_metadata_fields(body: str) -> frozenset[str]:
    present: set[str] = set()
    for line in body.splitlines():
        field, separator, value = line.partition(":")
        normalized = field.strip()
        if separator and value.strip() and normalized in _CANONICAL_METADATA_FIELDS:
            present.add(normalized)
    return frozenset(present)


def _exact_target_movie_ids(
    question: str, evidence: Sequence[EvidencePassage]
) -> tuple[str, ...]:
    candidates = tuple(
        (
            passage.movie_id,
            _movie_text(passage.movie, "chinese_title"),
            _movie_text(passage.movie, "english_title"),
        )
        for passage in evidence
    )
    return resolve_explicit_movie_identities(question, candidates)


def _exact_term_position(question: str, term: str, *, is_title: bool) -> int:
    if is_title:
        return explicit_title_intent_position(question, term)
    return explicit_movie_id_intent_position(question, term)
