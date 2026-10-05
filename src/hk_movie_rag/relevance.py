"""Immutable generic-search relevance policy and deterministic movie-domain signals."""

from __future__ import annotations

import hashlib
import json
import math
import re
from collections.abc import Mapping
from dataclasses import dataclass
from functools import cache
from importlib import resources
from types import MappingProxyType
from typing import cast

from .retrieval import (
    has_controlled_movie_list_intent,
    has_explicit_nonmovie_object_term,
    has_explicit_recommendation_intent,
    normalize_query_text,
    unambiguous_movie_genres,
)

_POLICY_PACKAGE = "hk_movie_rag.policies"
_LEGACY_POLICY_RESOURCE = "general_relevance_policy.json"
_R2_POLICY_RESOURCE = "general_relevance_v1_2_demo_r2.json"
_R3_POLICY_RESOURCE = "general_relevance_v1_2_demo_r3.json"
_GOLDEN_RESOURCE = "general_relevance_golden.jsonl"
_R2_DEEP_CASES_RESOURCE = "deep_document_cases_v1_2_demo_r2.jsonl"
_R3_DEEP_CASES_RESOURCE = "deep_document_cases_v1_2_demo_r3.jsonl"
_POLICY_KEYS = frozenset(
    {
        "schema_version",
        "policy_id",
        "release_id",
        "embedding_model",
        "embedding_dimension",
        "distance_metric",
        "distance_cutoff",
        "golden_sha256",
    }
)
_R2_POLICY_KEYS = _POLICY_KEYS | {"deep_cases_sha256"}
_EXPECTED_LEGACY_POLICY: Mapping[str, object] = MappingProxyType(
    {
        "schema_version": "1.0",
        "policy_id": "general-relevance/v1",
        "release_id": "v1.2-demo",
        "embedding_model": "gemini-embedding-2",
        "embedding_dimension": 768,
        "distance_metric": "pgvector_cosine_distance",
        "distance_cutoff": 0.36,
        "golden_sha256": "f56b81c15d8876f1078356913740f6d028a7c13addd84252c01629301ae3c489",
    }
)
_EXPECTED_R2_POLICY: Mapping[str, object] = MappingProxyType(
    {
        "schema_version": "1.1",
        "policy_id": "general-relevance/v2",
        "release_id": "v1.2-demo-r2",
        "embedding_model": "gemini-embedding-2",
        "embedding_dimension": 768,
        "distance_metric": "pgvector_cosine_distance",
        "distance_cutoff": 0.36,
        "golden_sha256": "f56b81c15d8876f1078356913740f6d028a7c13addd84252c01629301ae3c489",
        "deep_cases_sha256": "99c34194a8ee863bbf2c823f791feedb2dabdcb71a07e38a1c59f81bfa905d51",
    }
)
_EXPECTED_R3_POLICY: Mapping[str, object] = MappingProxyType(
    {
        "schema_version": "1.2",
        "policy_id": "general-relevance/v3",
        "release_id": "v1.2-demo-r3",
        "embedding_model": "gemini-embedding-2",
        "embedding_dimension": 768,
        "distance_metric": "pgvector_cosine_distance",
        "distance_cutoff": 0.36,
        "golden_sha256": "f56b81c15d8876f1078356913740f6d028a7c13addd84252c01629301ae3c489",
        "deep_cases_sha256": "8af52b415f3af8a91f2f52ec94c534c8a126ef443a81d8b351b56ac44b2483d0",
    }
)
_DEEP_POLICY_PAYLOADS = (_EXPECTED_R2_POLICY, _EXPECTED_R3_POLICY)
_POLICY_SELECTIONS: Mapping[tuple[str, str], tuple[str, Mapping[str, object], str | None]] = (
    MappingProxyType(
        {
            (
                "v1.2-demo",
                "5242afc5e5c91b74dd012b558ed5eb929f4acf78e98defe6d14bf4f7cce1a8c2",
            ): (_LEGACY_POLICY_RESOURCE, _EXPECTED_LEGACY_POLICY, None),
            (
                "v1.2-demo-r2",
                "a4ecce727a469689a2a08c6d42e0d7d1eb975761173e3b6e4d1eaf34db7b2aba",
            ): (_R2_POLICY_RESOURCE, _EXPECTED_R2_POLICY, _R2_DEEP_CASES_RESOURCE),
            (
                "v1.2-demo-r3",
                "d6db5e118aeea5dc0484f3c591baac666952f78f4d41bfdcdbbb58693f4f6bba",
            ): (_R3_POLICY_RESOURCE, _EXPECTED_R3_POLICY, _R3_DEEP_CASES_RESOURCE),
        }
    )
)
_LEGACY_POLICY_PAIR = next(
    pair for pair in _POLICY_SELECTIONS if pair[0] == "v1.2-demo"
)
_DEEP_CASE_KEYS = frozenset(
    {
        "case_id",
        "expected_movie_ids",
        "forbidden_answer_terms",
        "forbidden_source_kinds",
        "history",
        "question",
        "required_answer_terms",
        "required_any_answer_terms",
        "required_citation_ids",
        "requires_limited_evidence_disclosure",
    }
)
_HISTORY_KEYS = frozenset({"answer", "movie_ids", "question"})
_CITATION_ID = re.compile(
    r"(?:metadata:[A-Za-z0-9][A-Za-z0-9_-]{0,127}|pdf:[a-z0-9-]+:p[1-9][0-9]*)"
)

_EXPLICIT_MOVIE_TERMS = (
    "電影",
    "电影",
    "影片",
    "港片",
    "港產片",
    "港产片",
    "功夫片",
    "警匪片",
    "動作片",
    "动作片",
    "武俠片",
    "武侠片",
    "愛情片",
    "爱情片",
    "劇情片",
    "剧情片",
    "恐怖片",
    "喜劇片",
    "喜剧片",
    "犯罪片",
    "賭片",
    "赌片",
    "殭屍片",
    "僵尸片",
    "movie",
    "movies",
    "film",
    "films",
)
_CREDIT_OR_METADATA_TERMS = (
    "導演",
    "导演",
    "執導",
    "执导",
    "主演",
    "演員",
    "演员",
    "編劇",
    "编剧",
    "上映",
    "片長",
    "片长",
    "票房",
    "卡司",
    "director",
    "directed by",
    "actor",
    "actress",
    "cast",
    "screenwriter",
    "release date",
    "runtime",
)
_UNAMBIGUOUS_OOD_PATTERNS = tuple(
    re.compile(pattern, re.IGNORECASE)
    for pattern in (
        r"(?:今天|今日|明天|這週|这周).{0,8}(?:天氣|天气)",
        r"(?:天氣|天气).{0,8}(?:怎麼樣|怎么样|如何|預報|预报)",
        (
            r"^\s*(?:what|how)(?:'s|\s+is)?\s+(?:the\s+)?weather"
            r"(?:.{0,12}(?:today|to-day|tomorrow|forecast))?[。.!！?？]*\s*$"
        ),
        r"(?:股票|股市|比特幣|比特币|加密貨幣|加密货币|stock|crypto|bitcoin)",
        r"(?:郵件|邮件|email|改寫|改写|rewrite|translate)",
        (
            r"(?:總結|总结|審閱|审阅|分析).{0,8}"
            r"(?:這份|这份|該份|该份)(?:合同|合約|合约)"
        ),
        r"(?:summari[sz]e|review|analy[sz]e).{0,8}(?:this|the)\s+contract",
        (
            r"(?:合同|合約|合约|contract).{0,12}"
            r"(?:條款|条款|法律|簽署|签署|風險|风险|terms?|legal|sign(?:ing)?|risks?)"
        ),
        (
            r"(?:條款|条款|法律|簽署|签署|風險|风险|terms?|legal|sign(?:ing)?|risks?)"
            r".{0,12}(?:合同|合約|合约|contract)"
        ),
        r"(?:頭痛|头痛|headache).{0,24}(?:吃|用|take|藥|药|medicine)",
        r"(?:藥|药|medicine).{0,24}(?:頭痛|头痛|headache)",
        r"(?:制定|制訂|制订|make|create|build).{0,16}(?:健身|fitness|workout)",
        r"(?:健身|fitness|workout).{0,12}(?:計劃|计划|plan|routine|schedule)",
        r"(?:nba|足球|籃球|篮球|sports?\s+result|比賽結果|比赛结果)",
        r"(?:菜|蛋|飯|饭|food).{0,8}(?:怎麼做|怎么做|how\s+to\s+(?:cook|make))",
        r"(?:怎麼做|怎么做|how\s+to\s+(?:cook|make)).{0,8}(?:菜|蛋|飯|饭|food)",
        r"(?:學好|学好|learn).{0,10}(?:粵語|粤语|cantonese|language)",
        r"(?:python|量子力學|量子力学|quantum mechanics)",
        r"(?:法國首都|法国首都|capital of france)",
        r"(?:財報|财报|financial statements?)",
        r"(?:寫一首|写一首|write).{0,10}(?:詩|诗|poem)",
        r"^\s*(?:你好|hello|hi)[。.!！?？]*\s*$",
    )
)
_STRONG_OOD_PATTERNS = _UNAMBIGUOUS_OOD_PATTERNS


class RelevancePolicyError(RuntimeError):
    """Raised when a policy, golden fixture, or evaluation input is not authoritative."""


@dataclass(frozen=True)
class GeneralRelevancePolicy:
    """Validated immutable identity and cutoff for generic vector evidence."""

    policy_id: str
    release_id: str
    embedding_model: str
    embedding_dimension: int
    distance_metric: str
    distance_cutoff: float
    golden_sha256: str
    deep_cases_sha256: str | None
    artifact_sha256: str
    payload: Mapping[str, object]

    @staticmethod
    def sha256_bytes(value: bytes) -> str:
        return hashlib.sha256(value).hexdigest()


def has_strong_out_of_domain_signal(question: str) -> bool:
    """Return whether bounded wording clearly requests a non-movie object or task."""
    if not isinstance(question, str) or not question.strip():
        return False
    return any(pattern.search(question) is not None for pattern in _STRONG_OOD_PATTERNS)


def has_movie_credit_or_metadata_signal(question: str) -> bool:
    """Return whether the wording explicitly asks for a governed movie field."""
    if not isinstance(question, str) or not question.strip():
        return False
    normalized = normalize_query_text(question).casefold()
    return any(
        _contains_term(normalized, term.casefold())
        for term in _CREDIT_OR_METADATA_TERMS
    )


def has_static_movie_domain_signal(question: str) -> bool:
    """Classify only deterministic text signals; release title/person signals are dynamic."""
    if not isinstance(question, str) or not question.strip():
        return False
    normalized = normalize_query_text(question).casefold()
    explicit_movie = any(_contains_term(normalized, term.casefold()) for term in _EXPLICIT_MOVIE_TERMS)
    cinematic_genres = unambiguous_movie_genres(normalized)
    recommendation_intent = has_explicit_recommendation_intent(normalized)
    controlled_list_intent = has_controlled_movie_list_intent(normalized)
    hong_kong_context = "香港" in normalized or "hong kong" in normalized
    credit_or_metadata = has_movie_credit_or_metadata_signal(normalized)
    if has_strong_out_of_domain_signal(normalized):
        return False
    explicit_non_movie_object = has_explicit_nonmovie_object_term(normalized)
    if explicit_non_movie_object:
        return False
    return bool(
        explicit_movie
        or credit_or_metadata
        or (recommendation_intent and bool(cinematic_genres))
        or (
            hong_kong_context
            and bool(cinematic_genres)
            and (controlled_list_intent or len(cinematic_genres) >= 2)
        )
    )


def _contains_term(text: str, term: str) -> bool:
    if term.isascii():
        return re.search(rf"(?<![a-z0-9]){re.escape(term)}(?![a-z0-9])", text) is not None
    return term in text


def _read_policy_resource(name: str) -> bytes:
    return resources.files(_POLICY_PACKAGE).joinpath(name).read_bytes()


@cache
def load_general_relevance_policy(
    release_id: str | None = None,
    expected_artifact_sha256: str | None = None,
) -> GeneralRelevancePolicy:
    """Select one packaged policy by its exact release/digest pair, with legacy default."""
    try:
        if release_id is None and expected_artifact_sha256 is None:
            release_id, expected_artifact_sha256 = _LEGACY_POLICY_PAIR
        elif release_id is None or expected_artifact_sha256 is None:
            raise RelevancePolicyError("general relevance policy is invalid")
        if not isinstance(release_id, str) or not isinstance(expected_artifact_sha256, str):
            raise RelevancePolicyError("general relevance policy is invalid")
        selection = _POLICY_SELECTIONS.get((release_id, expected_artifact_sha256))
        if selection is None:
            raise RelevancePolicyError("general relevance policy is invalid")
        policy_resource, expected_payload, deep_cases_resource = selection
        policy_bytes = _read_policy_resource(policy_resource)
        golden_bytes = _read_policy_resource(_GOLDEN_RESOURCE)
        if hashlib.sha256(policy_bytes).hexdigest() != expected_artifact_sha256:
            raise RelevancePolicyError("general relevance policy is invalid")
        deep_cases_bytes = (
            None
            if deep_cases_resource is None
            else _read_policy_resource(deep_cases_resource)
        )
        payload = json.loads(policy_bytes.decode("utf-8"))
        if not isinstance(payload, dict):
            raise RelevancePolicyError("general relevance policy is invalid")
        canonical = (
            json.dumps(
                payload,
                ensure_ascii=False,
                sort_keys=True,
                separators=(",", ":"),
            ).encode("utf-8")
            + b"\n"
        )
        if canonical != policy_bytes:
            raise RelevancePolicyError("general relevance policy is invalid")
        return validate_general_relevance_policy(
            payload,
            golden_bytes,
            artifact_sha256=hashlib.sha256(policy_bytes).hexdigest(),
            deep_cases_bytes=deep_cases_bytes,
            expected_payload=expected_payload,
        )
    except RelevancePolicyError:
        raise
    except Exception:  # noqa: BLE001 - packaged policy loading must fail closed and redact details
        raise RelevancePolicyError("general relevance policy is invalid") from None


def validate_general_relevance_policy(
    payload: Mapping[str, object],
    golden_bytes: bytes,
    *,
    artifact_sha256: str | None = None,
    deep_cases_bytes: bytes | None = None,
    expected_payload: Mapping[str, object] | None = None,
) -> GeneralRelevancePolicy:
    """Validate strict policy identity against the immutable golden byte stream."""
    try:
        if expected_payload is None:
            expected_payload = {
                "v1.2-demo-r2": _EXPECTED_R2_POLICY,
                "v1.2-demo-r3": _EXPECTED_R3_POLICY,
            }.get(str(payload.get("release_id")), _EXPECTED_LEGACY_POLICY)
        expected_keys = (
            _R2_POLICY_KEYS
            if expected_payload in _DEEP_POLICY_PAYLOADS
            else _POLICY_KEYS
        )
        if not isinstance(payload, Mapping) or set(payload) != expected_keys:
            raise RelevancePolicyError("general relevance policy is invalid")
        if any(payload.get(key) != value for key, value in expected_payload.items()):
            raise RelevancePolicyError("general relevance policy is invalid")
        if not isinstance(golden_bytes, bytes) or not golden_bytes.endswith(b"\n"):
            raise RelevancePolicyError("general relevance policy is invalid")
        golden_sha256 = hashlib.sha256(golden_bytes).hexdigest()
        if golden_sha256 != payload["golden_sha256"]:
            raise RelevancePolicyError("general relevance policy is invalid")
        deep_cases_sha256: str | None = None
        if expected_payload in _DEEP_POLICY_PAYLOADS:
            if deep_cases_bytes is None:
                raise RelevancePolicyError("general relevance policy is invalid")
            deep_cases_sha256 = hashlib.sha256(deep_cases_bytes).hexdigest()
            if deep_cases_sha256 != payload["deep_cases_sha256"]:
                raise RelevancePolicyError("general relevance policy is invalid")
            _parse_deep_answer_cases(
                deep_cases_bytes,
                expected_count=10 if expected_payload is _EXPECTED_R3_POLICY else 6,
            )
        elif deep_cases_bytes is not None:
            raise RelevancePolicyError("general relevance policy is invalid")
        if artifact_sha256 is None:
            canonical = (
                json.dumps(
                    dict(payload),
                    ensure_ascii=False,
                    sort_keys=True,
                    separators=(",", ":"),
                ).encode("utf-8")
                + b"\n"
            )
            artifact_sha256 = hashlib.sha256(canonical).hexdigest()
        if not re.fullmatch(r"[0-9a-f]{64}", artifact_sha256):
            raise RelevancePolicyError("general relevance policy is invalid")
        cutoff = payload["distance_cutoff"]
        if (
            isinstance(cutoff, bool)
            or not isinstance(cutoff, (int, float))
            or not math.isfinite(float(cutoff))
        ):
            raise RelevancePolicyError("general relevance policy is invalid")
        frozen_payload = MappingProxyType(dict(payload))
        return GeneralRelevancePolicy(
            policy_id=str(payload["policy_id"]),
            release_id=str(payload["release_id"]),
            embedding_model=str(payload["embedding_model"]),
            embedding_dimension=cast(int, payload["embedding_dimension"]),
            distance_metric=str(payload["distance_metric"]),
            distance_cutoff=float(cutoff),
            golden_sha256=golden_sha256,
            deep_cases_sha256=deep_cases_sha256,
            artifact_sha256=artifact_sha256,
            payload=frozen_payload,
        )
    except RelevancePolicyError:
        raise
    except Exception:  # noqa: BLE001 - arbitrary Mapping implementations must fail closed
        raise RelevancePolicyError("general relevance policy is invalid") from None


def load_deep_answer_cases(
    release_id: str,
    expected_policy_sha256: str,
) -> tuple[dict[str, object], ...]:
    """Load the exact canonical deep cases bound by the selected release policy."""
    policy = load_general_relevance_policy(release_id, expected_policy_sha256)
    selection = _POLICY_SELECTIONS.get((release_id, expected_policy_sha256))
    if selection is None or selection[2] is None or policy.deep_cases_sha256 is None:
        raise RelevancePolicyError("general relevance policy is invalid")
    try:
        raw = _read_policy_resource(selection[2])
        if hashlib.sha256(raw).hexdigest() != policy.deep_cases_sha256:
            raise RelevancePolicyError("general relevance policy is invalid")
        return _parse_deep_answer_cases(
            raw, expected_count=10 if release_id == "v1.2-demo-r3" else 6
        )
    except RelevancePolicyError:
        raise
    except Exception:  # noqa: BLE001 - packaged artifact loading must stay redacted
        raise RelevancePolicyError("general relevance policy is invalid") from None


def _parse_deep_answer_cases(
    raw: bytes, *, expected_count: int = 6
) -> tuple[dict[str, object], ...]:
    try:
        text = raw.decode("utf-8")
        if not text.endswith("\n"):
            raise ValueError
        cases: list[dict[str, object]] = []
        seen: set[str] = set()
        for line in text.splitlines():
            value = json.loads(line)
            if not isinstance(value, dict) or set(value) != _DEEP_CASE_KEYS:
                raise ValueError
            canonical = json.dumps(
                value,
                ensure_ascii=False,
                sort_keys=True,
                separators=(",", ":"),
                allow_nan=False,
            )
            if canonical != line:
                raise ValueError
            case_id = value["case_id"]
            question = value["question"]
            if (
                not isinstance(case_id, str)
                or not case_id
                or case_id in seen
                or not isinstance(question, str)
                or not question
                or type(value["requires_limited_evidence_disclosure"]) is not bool
            ):
                raise ValueError
            seen.add(case_id)
            for key in (
                "expected_movie_ids",
                "forbidden_answer_terms",
                "forbidden_source_kinds",
                "required_answer_terms",
                "required_any_answer_terms",
                "required_citation_ids",
            ):
                if not _is_nonempty_text_list(value[key], allow_empty=key != "expected_movie_ids"):
                    raise ValueError
            if not value["required_citation_ids"] or not all(
                _CITATION_ID.fullmatch(item) is not None
                for item in cast(list[str], value["required_citation_ids"])
            ):
                raise ValueError
            if any(
                item not in {"movie_metadata", "pdf_page"}
                for item in cast(list[str], value["forbidden_source_kinds"])
            ):
                raise ValueError
            history = value["history"]
            if not isinstance(history, list) or len(history) > 4:
                raise ValueError
            for exchange in history:
                if (
                    not isinstance(exchange, dict)
                    or set(exchange) != _HISTORY_KEYS
                    or not isinstance(exchange["question"], str)
                    or not exchange["question"]
                    or not isinstance(exchange["answer"], str)
                    or not exchange["answer"]
                    or not _is_nonempty_text_list(exchange["movie_ids"], allow_empty=True)
                ):
                    raise ValueError
            cases.append(value)
        if len(cases) != expected_count:
            raise ValueError
        return tuple(cases)
    except (UnicodeDecodeError, json.JSONDecodeError, TypeError, ValueError):
        raise RelevancePolicyError("general relevance policy is invalid") from None


def _is_nonempty_text_list(value: object, *, allow_empty: bool) -> bool:
    return bool(
        isinstance(value, list)
        and (allow_empty or value)
        and all(isinstance(item, str) and item for item in value)
        and len(set(value)) == len(value)
    )


GENERAL_RELEVANCE_POLICY_SHA256 = load_general_relevance_policy().artifact_sha256
