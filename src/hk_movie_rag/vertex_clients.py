"""Small, retry-bounded Vertex AI adapters used by ingestion and querying."""

from __future__ import annotations

import json
import logging
import math
import random
import re
import time
from collections.abc import Mapping, Sequence
from typing import TYPE_CHECKING, Any, Protocol

from google import genai
from google.auth.credentials import Credentials
from google.genai import types

from .identity_security import (
    contains_adversarial_identity,
    has_forbidden_identity_controls,
)
from .retrieval import has_deep_analysis_intent

if TYPE_CHECKING:
    from .rag_query import EvidencePassage, GeneratedAnswer

_TRANSIENT_STATUS_CODES = frozenset({429, 500, 502, 503, 504})
_GENERATION_TRANSIENT_STATUS_CODES = [408, 429, 500, 502, 503, 504]
_MAX_ATTEMPTS = 5
_MAX_RECOMMENDATION_REASON_CHARACTERS = 240
_MAX_RECOMMENDATION_OUTPUT_ATTEMPTS = 2
_MAX_FALLBACK_METADATA_CHARACTERS = 120
_SDK_RETRY_LOGGER = logging.getLogger("google_genai._api_client")
_SDK_RETRY_LOGGER.setLevel(logging.WARNING)


class VertexEmbeddingError(RuntimeError):
    """Raised when Vertex cannot return one safe embedding vector."""


class VertexGenerationError(RuntimeError):
    """Raised when Vertex cannot return the strict grounded JSON contract."""


class EmbeddingClient(Protocol):
    """Embedding boundary shared by ingestion and the query service."""

    model: str
    dimension: int

    def embed_document(self, text: str, *, title: str) -> list[float]: ...

    def embed_query(self, text: str) -> list[float]: ...


class VertexEmbeddingClient:
    """Vertex AI embedding client with narrow retries and strict vector validation."""

    def __init__(
        self,
        project_id: str,
        location: str,
        model: str,
        dimension: int,
        *,
        credentials: Credentials | None = None,
    ) -> None:
        if not project_id.strip():
            raise VertexEmbeddingError("project_id must be non-empty")
        if location != "global":
            raise VertexEmbeddingError("Vertex embedding location must be global")
        if not model.strip():
            raise VertexEmbeddingError("embedding model must be non-empty")
        if dimension != 768:
            raise VertexEmbeddingError("embedding dimension must be 768")
        self.project_id = project_id
        self.location = location
        self.model = model
        self.dimension = dimension
        self._client = genai.Client(
            vertexai=True,
            project=project_id,
            location=location,
            credentials=credentials,
            http_options=types.HttpOptions(
                retry_options=types.HttpRetryOptions(attempts=1)
            ),
        )

    def embed_document(self, text: str, *, title: str) -> list[float]:
        """Embed a passage using the release's deterministic document envelope."""
        return self._embed(f"title: {title} | text: {text}")

    def embed_query(self, text: str) -> list[float]:
        """Embed a caller-prepared query task string without rewriting it."""
        return self._embed(text)

    def _embed(self, contents: str) -> list[float]:
        response: Any | None = None
        for attempt in range(1, _MAX_ATTEMPTS + 1):
            try:
                response = self._client.models.embed_content(
                    model=self.model,
                    contents=contents,
                    config=types.EmbedContentConfig(output_dimensionality=self.dimension),
                )
                break
            except Exception as exc:
                status = _status_code(exc)
                if status in _TRANSIENT_STATUS_CODES and attempt < _MAX_ATTEMPTS:
                    delay = min(8.0, float(2 ** (attempt - 1))) + random.uniform(0.0, 0.25)
                    time.sleep(delay)
                    continue
                suffix = "attempt" if attempt == 1 else "attempts"
                status_text = f"status {status} " if status is not None else ""
                raise VertexEmbeddingError(
                    f"embedding request failed ({status_text}after {attempt} {suffix})"
                ) from exc
        if response is None:  # pragma: no cover - the bounded loop always returns or raises
            raise VertexEmbeddingError("embedding request failed")
        return _validated_vector(response, self.dimension)


_GENERATION_MODEL = "gemini-3.5-flash-lite"
_GROUNDING_SYSTEM_PROMPT = """你是香港電影資料庫的受控回答器。
只可使用使用者訊息內 supplied_evidence 的內容，不可使用模型記憶、常識或外部資料。
conversation_context_not_evidence 只協助理解當前問題，絕對不是事實證據。
每個事實性輸出行都必須包含至少一個完全相同的 [citation_id]；不可創造引用 ID。
同一行需要多個引用時，每個 citation_id 必須使用獨立方括號，例如 [id1][id2]。
只輸出簡潔的繁體中文段落，不要輸出標題、Markdown 連結、URL 或未引用的補充。
若問題要求攝影、視覺、動作設計、美學、敘事、主題或其他深度分析，而證據只有
movie_metadata，必須回答
「目前只有結構化電影資料，不能提供沒有深度文檔支持的分析。」並引用該 metadata。
推薦字眼不能覆蓋深度分析判斷；深度問題不可當成普通 metadata 推薦理由回答。
若證據不足，必須明確說明證據不足，且不得猜測。
當 answer_mode 是 recommendation，每個 required_citation_id 都必須按原順序恰好使用一次：
每部電影一行，只可依據該片的 metadata、tier、pilot_movie、年份、導演、演員及類型給出
簡潔推薦理由。推薦模式不可套用「不能提供沒有深度文檔支持的分析」的拒絕句，也不可
臆測評分、口碑、主題、美學、敘事或觀眾共識。
當 answer_mode 是 deep_recommendation，每個 supplied_evidence 都是已篩選的 PDF 深度文檔
段落；必須按 required_citation_ids 原順序每部電影恰好一行，且該行只能依據該片對應段落
說明它如何符合問題中的深度條件，不可遺漏、合併或借用其他電影的證據。
推薦理由不可包含任何候選片名、movie_id、citation_id、引用標記、URL 或路徑。"""
_GENERAL_GROUNDING_SYSTEM_PROMPT = (
    _GROUNDING_SYSTEM_PROMPT
    + "\n輸出必須符合 JSON schema：answer_markdown 字串與 citation_ids 字串陣列。"
)
_RECOMMENDATION_GROUNDING_SYSTEM_PROMPT = (
    _GROUNDING_SYSTEM_PROMPT
    + "\n推薦模式只輸出 items 陣列；每項只含 exact citation_id 與不含身份文字的 reason。"
)
class VertexGenerationClient:
    """Vertex adapter with a fixed model, JSON schema, and grounding prompt."""

    def __init__(
        self,
        project_id: str,
        location: str,
        model: str = _GENERATION_MODEL,
        *,
        credentials: Credentials | None = None,
    ) -> None:
        if not project_id.strip():
            raise VertexGenerationError("project_id must be non-empty")
        if location != "global":
            raise VertexGenerationError("Vertex generation location must be global")
        if model != _GENERATION_MODEL:
            raise VertexGenerationError(f"generation model must be {_GENERATION_MODEL}")
        self.project_id = project_id
        self.location = location
        self.model = model
        self._client = genai.Client(
            vertexai=True,
            project=project_id,
            location=location,
            credentials=credentials,
            http_options=types.HttpOptions(
                timeout=20_000,
                retry_options=types.HttpRetryOptions(
                    attempts=3,
                    initial_delay=0.25,
                    max_delay=1.0,
                    exp_base=2.0,
                    jitter=0.1,
                    http_status_codes=list(_GENERATION_TRANSIENT_STATUS_CODES),
                ),
            ),
        )

    def generate_grounded(
        self,
        question: str,
        passages: Sequence[EvidencePassage],
        *,
        mode: str,
        conversation_context: tuple[str, ...],
        required_citation_ids: tuple[str, ...],
    ) -> GeneratedAnswer:
        """Generate strict JSON from only the supplied repository evidence."""
        if not question.strip() or not passages:
            raise VertexGenerationError("grounded generation requires a question and evidence")
        if mode not in {"general", "recommendation", "deep_recommendation"}:
            raise VertexGenerationError("grounded generation mode is invalid")
        if mode == "recommendation" and has_deep_analysis_intent(question):
            raise VertexGenerationError(
                "deep-analysis question cannot use recommendation generation"
            )
        if (
            not isinstance(conversation_context, tuple)
            or len(conversation_context) > 4
            or any(
                not isinstance(item, str) or not item.strip()
                for item in conversation_context
            )
        ):
            raise VertexGenerationError("grounded conversation context is invalid")
        passage_ids = tuple(passage.passage_id for passage in passages)
        if mode == "deep_recommendation" and (
            not has_deep_analysis_intent(question)
            or any(passage.source_kind != "pdf_page" for passage in passages)
            or not isinstance(required_citation_ids, tuple)
            or required_citation_ids != passage_ids
            or len(set(required_citation_ids)) != len(required_citation_ids)
        ):
            raise VertexGenerationError("deep recommendation contract is invalid")
        if (
            not isinstance(required_citation_ids, tuple)
            or len(set(required_citation_ids)) != len(required_citation_ids)
            or any(citation_id not in passage_ids for citation_id in required_citation_ids)
            or (mode == "recommendation" and required_citation_ids != passage_ids)
            or (mode == "general" and required_citation_ids)
        ):
            raise VertexGenerationError("required citation IDs are invalid")
        if mode == "recommendation":
            _validate_metadata_recommendation_evidence(passages)
        contents = json.dumps(
            {
                "answer_mode": mode,
                "conversation_context_not_evidence": list(conversation_context),
                "question": question,
                "required_citation_ids": list(required_citation_ids),
                "supplied_evidence": [
                    {
                        "citation_id": passage.passage_id,
                        "movie_id": passage.movie_id,
                        "source_kind": passage.source_kind,
                        "page_number": passage.page_number,
                        "source_filename": passage.source_filename,
                        "body": passage.body,
                        "movie": dict(passage.movie),
                    }
                    for passage in passages
                ],
            },
            ensure_ascii=False,
            sort_keys=True,
            separators=(",", ":"),
        )
        is_recommendation = mode in {"recommendation", "deep_recommendation"}
        response_schema = (
            _recommendation_response_schema(required_citation_ids)
            if is_recommendation
            else _general_response_schema(passage_ids)
        )
        system_prompt = (
            _RECOMMENDATION_GROUNDING_SYSTEM_PROMPT
            if is_recommendation
            else _GENERAL_GROUNDING_SYSTEM_PROMPT
        )
        fallback_enabled = mode == "recommendation"
        output_attempts = _MAX_RECOMMENDATION_OUTPUT_ATTEMPTS if fallback_enabled else 1
        for output_attempt in range(output_attempts):
            retry_instruction = (
                "\n上一次輸出未通過伺服器驗證；這是最後一次，必須完全符合 schema 與次序。"
                if output_attempt
                else ""
            )
            try:
                response = self._client.models.generate_content(
                    model=self.model,
                    contents=contents,
                    config=types.GenerateContentConfig(
                        system_instruction=system_prompt + retry_instruction,
                        response_mime_type="application/json",
                        response_json_schema=response_schema,
                        max_output_tokens=1024,
                    ),
                )
            except Exception:  # noqa: BLE001 - provider failures cross a redacted boundary
                if fallback_enabled:
                    return _metadata_recommendation_fallback(
                        passages, required_citation_ids
                    )
                raise VertexGenerationError("grounded generation request failed") from None
            try:
                payload = _generation_payload(response)
                if is_recommendation:
                    return _validated_recommendation_payload(
                        payload,
                        passages,
                        required_citation_ids,
                    )
                return _validated_general_payload(payload)
            except VertexGenerationError:
                if not fallback_enabled:
                    raise
                if output_attempt + 1 < output_attempts:
                    continue
                return _metadata_recommendation_fallback(
                    passages, required_citation_ids
                )
        raise VertexGenerationError("grounded generation request failed")  # pragma: no cover


def _general_response_schema(passage_ids: tuple[str, ...]) -> dict[str, object]:
    if not passage_ids or len(set(passage_ids)) != len(passage_ids):
        raise VertexGenerationError("general generation evidence identity is invalid")
    return {
        "type": "object",
        "properties": {
            "answer_markdown": {"type": "string"},
            "citation_ids": {
                "type": "array",
                "items": {"type": "string", "enum": list(passage_ids)},
                "minItems": 1,
                "maxItems": len(passage_ids),
            },
        },
        "required": ["answer_markdown", "citation_ids"],
        "additionalProperties": False,
    }


def _recommendation_response_schema(
    required_citation_ids: tuple[str, ...],
) -> dict[str, object]:
    reason_schema: dict[str, object] = {
        "type": "string",
    }

    def item_schema(citation_values: list[str] | None = None) -> dict[str, object]:
        citation_schema: dict[str, object] = {"type": "string"}
        if citation_values is not None:
            citation_schema["enum"] = citation_values
        return {
            "type": "object",
            "properties": {
                "citation_id": citation_schema,
                "reason": dict(reason_schema),
            },
            "required": ["citation_id", "reason"],
            "additionalProperties": False,
        }

    return {
        "type": "object",
        "properties": {
            "items": {
                "type": "array",
                "items": item_schema(),
                "prefixItems": [
                    item_schema([citation_id])
                    for citation_id in required_citation_ids
                ],
                "minItems": len(required_citation_ids),
                "maxItems": len(required_citation_ids),
            }
        },
        "required": ["items"],
        "additionalProperties": False,
    }


def _validated_recommendation_payload(
    payload: Mapping[str, object],
    passages: Sequence[EvidencePassage],
    required_citation_ids: tuple[str, ...],
) -> GeneratedAnswer:
    if set(payload) != {"items"}:
        raise VertexGenerationError("recommendation response has unexpected JSON keys")
    raw_items = payload.get("items")
    if (
        not isinstance(raw_items, list)
        or len(raw_items) != len(required_citation_ids)
    ):
        raise VertexGenerationError("recommendation response items are invalid")
    passage_by_id = {passage.passage_id: passage for passage in passages}
    if len(passage_by_id) != len(passages):
        raise VertexGenerationError("recommendation evidence identity is invalid")
    forbidden_identities = _recommendation_identities(passages)
    lines: list[str] = []
    for raw_item, expected_citation_id in zip(
        raw_items, required_citation_ids, strict=True
    ):
        if not isinstance(raw_item, Mapping) or set(raw_item) != {
            "citation_id",
            "reason",
        }:
            raise VertexGenerationError("recommendation response item is invalid")
        citation_id = raw_item.get("citation_id")
        reason = raw_item.get("reason")
        if citation_id != expected_citation_id:
            raise VertexGenerationError("recommendation citation order is invalid")
        if not isinstance(reason, str):
            raise VertexGenerationError("recommendation reason is invalid")
        normalized_reason = reason.strip()
        if (
            not normalized_reason
            or len(normalized_reason) > _MAX_RECOMMENDATION_REASON_CHARACTERS
            or any(character in normalized_reason for character in "\r\n")
            or has_forbidden_identity_controls(normalized_reason)
            or _contains_uri_or_path(normalized_reason)
            or contains_adversarial_identity(
                normalized_reason, forbidden_identities
            )
            or re.search(r"\[[^\[\]\r\n]+\]", normalized_reason) is not None
        ):
            raise VertexGenerationError("recommendation reason is invalid")
        passage = passage_by_id.get(expected_citation_id)
        if passage is None:
            raise VertexGenerationError("recommendation evidence identity is invalid")
        title = _recommendation_title(passage)
        sentence = normalized_reason.rstrip("。！？!?;；,.，")
        if not sentence:
            raise VertexGenerationError("recommendation reason is invalid")
        lines.append(f"《{title}》：{sentence}。[{expected_citation_id}]")
    from .rag_query import GeneratedAnswer

    return GeneratedAnswer(
        answer_markdown="\n".join(lines),
        citation_ids=required_citation_ids,
    )


def _validated_general_payload(payload: Mapping[str, object]) -> GeneratedAnswer:
    if set(payload) != {"answer_markdown", "citation_ids"}:
        raise VertexGenerationError("generation response has unexpected JSON keys")
    answer = payload.get("answer_markdown")
    citation_ids = payload.get("citation_ids")
    if not isinstance(answer, str) or not answer.strip():
        raise VertexGenerationError("generation response has invalid answer_markdown")
    if not isinstance(citation_ids, list) or not all(
        isinstance(citation_id, str) and citation_id for citation_id in citation_ids
    ):
        raise VertexGenerationError("generation response has invalid citation_ids")
    from .rag_query import GeneratedAnswer

    return GeneratedAnswer(answer_markdown=answer, citation_ids=tuple(citation_ids))


def _validate_metadata_recommendation_evidence(
    passages: Sequence[EvidencePassage],
) -> None:
    movie_ids: set[str] = set()
    for passage in passages:
        if (
            passage.source_kind != "movie_metadata"
            or not passage.movie_id
            or passage.movie.get("movie_id") != passage.movie_id
            or passage.movie_id in movie_ids
        ):
            raise VertexGenerationError("recommendation evidence contract is invalid")
        movie_ids.add(passage.movie_id)
        _recommendation_title(passage)


def _metadata_recommendation_fallback(
    passages: Sequence[EvidencePassage],
    required_citation_ids: tuple[str, ...],
) -> GeneratedAnswer:
    _validate_metadata_recommendation_evidence(passages)
    passage_by_id = {passage.passage_id: passage for passage in passages}
    if (
        len(passage_by_id) != len(passages)
        or tuple(passage_by_id) != required_citation_ids
    ):
        raise VertexGenerationError("recommendation fallback identity is invalid")
    forbidden_identities = _recommendation_identities(passages)
    lines: list[str] = []
    for citation_id in required_citation_ids:
        passage = passage_by_id.get(citation_id)
        if passage is None:  # pragma: no cover - exact tuple equality proves membership
            raise VertexGenerationError("recommendation fallback identity is invalid")
        title = _recommendation_title(passage)
        reason = _metadata_recommendation_reason(passage, forbidden_identities)
        lines.append(f"《{title}》：{reason}。[{citation_id}]")
    from .rag_query import GeneratedAnswer

    return GeneratedAnswer(
        answer_markdown="\n".join(lines),
        citation_ids=required_citation_ids,
    )


def _metadata_recommendation_reason(
    passage: EvidencePassage,
    forbidden_identities: Sequence[str],
) -> str:
    facts: list[str] = []
    release_date = _passage_movie_text(passage, "release_date").strip()
    if re.fullmatch(r"(?:18|19|20)\d{2}(?:-\d{2}-\d{2})?", release_date):
        facts.append(f"上映日期：{release_date}")
    for key, label in (
        ("director", "導演"),
        ("cast", "演員"),
        ("genre", "類型"),
    ):
        value = _safe_fallback_metadata_text(
            _passage_movie_text(passage, key), forbidden_identities
        )
        if value is not None:
            facts.append(f"{label}：{value}")
    tier = passage.movie.get("tier")
    if tier in {"S", "A", "B"}:
        facts.append(f"資料分級：{tier} 級")
    pilot_movie = passage.movie.get("pilot_movie")
    if isinstance(pilot_movie, bool):
        facts.append(f"pilot：{'是' if pilot_movie else '否'}")

    selected: list[str] = []
    for fact in facts:
        candidate = "；".join((*selected, fact))
        if len(candidate) <= _MAX_RECOMMENDATION_REASON_CHARACTERS:
            selected.append(fact)
    if not selected:
        raise VertexGenerationError("recommendation fallback metadata is insufficient")
    return "；".join(selected)


def _safe_fallback_metadata_text(
    value: str,
    forbidden_identities: Sequence[str],
) -> str | None:
    normalized = value.strip()
    if (
        not normalized
        or len(normalized) > _MAX_FALLBACK_METADATA_CHARACTERS
        or any(character.isspace() and character != " " for character in normalized)
        or any(character in normalized for character in "[]《》")
        or has_forbidden_identity_controls(normalized)
        or _contains_uri_or_path(normalized)
        or contains_adversarial_identity(normalized, forbidden_identities)
    ):
        return None
    return normalized


def _recommendation_identities(
    passages: Sequence[EvidencePassage],
) -> tuple[str, ...]:
    return tuple(
        identity
        for passage in passages
        for identity in (
            passage.passage_id,
            passage.movie_id,
            _passage_movie_text(passage, "chinese_title"),
            _passage_movie_text(passage, "english_title"),
        )
        if identity
    )


def _recommendation_title(passage: EvidencePassage) -> str:
    title = _passage_movie_text(passage, "chinese_title") or _passage_movie_text(
        passage, "english_title"
    )
    if not title.strip() or any(character in title for character in "\r\n《》"):
        raise VertexGenerationError("recommendation evidence title is invalid")
    return title


def _passage_movie_text(passage: EvidencePassage, key: str) -> str:
    value = passage.movie.get(key)
    return value if isinstance(value, str) else ""


_URI_SCHEME = re.compile(r"(?i)(?<![a-z0-9+.-])[a-z][a-z0-9+.-]{0,31}:(?=\S)")
_DOMAIN = re.compile(
    r"(?i)\b(?:www\.|(?:[a-z0-9](?:[a-z0-9-]{0,62})\.)+)"
    r"[a-z]{2,63}(?:[/?:#][^\s<>]*)?"
)
_PATH_REFERENCE = re.compile(
    r"(?:^|[\s:：,，;；!！?？(（\[【{<='\"“”])(?:\.\.?/|/)[^\s<>\[\]]+"
)


def _contains_uri_or_path(text: str) -> bool:
    return any(
        pattern.search(text) is not None
        for pattern in (_URI_SCHEME, _DOMAIN, _PATH_REFERENCE)
    )


def _generation_payload(response: object) -> Mapping[str, object]:
    parsed = getattr(response, "parsed", None)
    if isinstance(parsed, Mapping):
        return parsed
    text = getattr(response, "text", None)
    if not isinstance(text, str):
        raise VertexGenerationError("generation response is not JSON")
    try:
        payload = json.loads(text)
    except json.JSONDecodeError as exc:
        raise VertexGenerationError("generation response is not JSON") from exc
    if not isinstance(payload, dict):
        raise VertexGenerationError("generation response is not a JSON object")
    return payload


def _status_code(exc: Exception) -> int | None:
    for candidate in (
        getattr(exc, "status_code", None),
        getattr(exc, "code", None),
        getattr(getattr(exc, "response", None), "status_code", None),
    ):
        if isinstance(candidate, int) and not isinstance(candidate, bool):
            return candidate
    return None


def _validated_vector(response: object, dimension: int) -> list[float]:
    embeddings = getattr(response, "embeddings", None)
    if not isinstance(embeddings, Sequence) or len(embeddings) != 1:
        raise VertexEmbeddingError(f"embedding response must contain exactly {dimension} finite floats")
    values = getattr(embeddings[0], "values", None)
    if not isinstance(values, Sequence) or isinstance(values, (str, bytes)):
        raise VertexEmbeddingError(f"embedding response must contain exactly {dimension} finite floats")
    try:
        vector = [float(value) for value in values if not isinstance(value, bool)]
    except (TypeError, ValueError, OverflowError) as exc:
        raise VertexEmbeddingError(
            f"embedding response must contain exactly {dimension} finite floats"
        ) from exc
    if len(vector) != dimension or len(values) != dimension or not all(
        math.isfinite(value) for value in vector
    ):
        raise VertexEmbeddingError(f"embedding response must contain exactly {dimension} finite floats")
    return vector
