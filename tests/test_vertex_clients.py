"""Strict generation identity and SDK resilience contracts."""

from __future__ import annotations

import json
import logging
import traceback
from collections.abc import Callable, Iterator
from contextlib import contextmanager
from types import SimpleNamespace

import httpx
import pytest
from google.genai import Client as RealGenaiClient
from google.oauth2.credentials import Credentials

from hk_movie_rag.rag_query import EvidencePassage, GeneratedAnswer
from hk_movie_rag.vertex_clients import VertexGenerationClient, VertexGenerationError


def _evidence() -> tuple[EvidencePassage, ...]:
    return (
        EvidencePassage(
            passage_id="metadata:first",
            movie_id="first_movie",
            source_kind="movie_metadata",
            body="第一部的受控資料",
            page_number=None,
            source_filename=None,
            movie={
                "movie_id": "first_movie",
                "chinese_title": "第一部",
                "english_title": "First Film",
            },
            poster_available=False,
        ),
        EvidencePassage(
            passage_id="metadata:second",
            movie_id="second_movie",
            source_kind="movie_metadata",
            body="第二部的受控資料",
            page_number=None,
            source_filename=None,
            movie={
                "movie_id": "second_movie",
                "chinese_title": "第二部",
                "english_title": "Second Film",
            },
            poster_available=False,
        ),
    )


def _fallback_evidence() -> tuple[EvidencePassage, ...]:
    return (
        EvidencePassage(
            passage_id="metadata:first",
            movie_id="first_movie",
            source_kind="movie_metadata",
            body="不可用誘餌：口碑極佳、主題深刻、節奏明快、適合全家。",
            page_number=None,
            source_filename=None,
            movie={
                "movie_id": "first_movie",
                "chinese_title": "第一部",
                "english_title": "First Film",
                "release_date": "1988-01-02",
                "director": "甲導演",
                "cast": "甲演員、乙演員",
                "genre": "犯罪 / 動作",
                "tier": "S",
                "pilot_movie": True,
                "rating": "口碑極佳",
                "plot": "主題深刻而且節奏明快",
            },
            poster_available=False,
        ),
        EvidencePassage(
            passage_id="metadata:second",
            movie_id="second_movie",
            source_kind="movie_metadata",
            body="不可用誘餌：觀眾一致推薦。",
            page_number=None,
            source_filename=None,
            movie={
                "movie_id": "second_movie",
                "chinese_title": "第二部",
                "english_title": "Second Film",
                "release_date": "1992-03-04",
                "director": "乙導演",
                "cast": "丙演員",
                "genre": "喜劇",
                "tier": "A",
                "pilot_movie": False,
                "rating": "五星",
            },
            poster_available=False,
        ),
    )


def _deep_evidence() -> tuple[EvidencePassage, ...]:
    return (
        EvidencePassage(
            passage_id="pdf:first:p1",
            movie_id="first_movie",
            source_kind="pdf_page",
            body="第一部的視覺美學深度證據。",
            page_number=1,
            source_filename="first-deep.pdf",
            movie={
                "movie_id": "first_movie",
                "chinese_title": "第一部",
                "english_title": "First Film",
            },
            poster_available=False,
        ),
    )


class _Models:
    def __init__(self, payload: dict[str, object]) -> None:
        self.payload = payload
        self.calls: list[dict[str, object]] = []

    def generate_content(self, **kwargs: object) -> object:
        self.calls.append(kwargs)
        return SimpleNamespace(parsed=self.payload, text=None)


class _SequenceModels:
    def __init__(self, outcomes: list[object]) -> None:
        self.outcomes = list(outcomes)
        self.calls: list[dict[str, object]] = []

    def generate_content(self, **kwargs: object) -> object:
        self.calls.append(kwargs)
        if not self.outcomes:
            raise AssertionError("unexpected extra outer generation attempt")
        outcome = self.outcomes.pop(0)
        if isinstance(outcome, BaseException):
            raise outcome
        return SimpleNamespace(parsed=outcome, text=None)


def _capture_client(
    monkeypatch: pytest.MonkeyPatch, payload: dict[str, object]
) -> tuple[VertexGenerationClient, _Models, dict[str, object]]:
    models = _Models(payload)
    observed: dict[str, object] = {}

    def fake_client(**kwargs: object) -> object:
        observed.update(kwargs)
        return SimpleNamespace(models=models)

    monkeypatch.setattr("hk_movie_rag.vertex_clients.genai.Client", fake_client)
    return VertexGenerationClient("motionexpaiweb", "global"), models, observed


def _capture_sequence_client(
    monkeypatch: pytest.MonkeyPatch, outcomes: list[object]
) -> tuple[VertexGenerationClient, _SequenceModels]:
    models = _SequenceModels(outcomes)

    def fake_client(**kwargs: object) -> object:
        return SimpleNamespace(models=models)

    monkeypatch.setattr("hk_movie_rag.vertex_clients.genai.Client", fake_client)
    return VertexGenerationClient("motionexpaiweb", "global"), models


def _expected_metadata_fallback() -> GeneratedAnswer:
    return GeneratedAnswer(
        "《第一部》：上映日期：1988-01-02；導演：甲導演；演員：甲演員、乙演員；"
        "類型：犯罪 / 動作；資料分級：S 級；pilot：是。[metadata:first]\n"
        "《第二部》：上映日期：1992-03-04；導演：乙導演；演員：丙演員；"
        "類型：喜劇；資料分級：A 級；pilot：否。[metadata:second]",
        ("metadata:first", "metadata:second"),
    )


def test_recommendation_generation_uses_position_bound_items_and_server_owned_lines(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Breaks if the model can own a recommendation title, marker, or item order."""
    client, models, _ = _capture_client(
        monkeypatch,
        {
            "items": [
                {"citation_id": "metadata:first", "reason": "有受控理由"},
                {"citation_id": "metadata:second", "reason": "也有受控理由"},
            ]
        },
    )

    result = client.generate_grounded(
        "推薦兩部電影",
        _evidence(),
        mode="recommendation",
        conversation_context=(),
        required_citation_ids=("metadata:first", "metadata:second"),
    )

    assert result == GeneratedAnswer(
        "《第一部》：有受控理由。[metadata:first]\n"
        "《第二部》：也有受控理由。[metadata:second]",
        ("metadata:first", "metadata:second"),
    )
    config = models.calls[0]["config"]
    schema = config.response_json_schema
    assert schema["required"] == ["items"]
    assert schema["properties"]["items"]["minItems"] == 2
    assert schema["properties"]["items"]["maxItems"] == 2
    assert [
        item["properties"]["citation_id"]["enum"]
        for item in schema["properties"]["items"]["prefixItems"]
    ] == [["metadata:first"], ["metadata:second"]]


def test_recommendation_provider_failure_uses_only_supplied_metadata_fallback(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    client, models = _capture_sequence_client(
        monkeypatch, [RuntimeError("provider detail must not escape")]
    )

    result = client.generate_grounded(
        "推薦兩部電影",
        _fallback_evidence(),
        mode="recommendation",
        conversation_context=(),
        required_citation_ids=("metadata:first", "metadata:second"),
    )

    assert result == _expected_metadata_fallback()
    assert len(models.calls) == 1
    assert not any(
        forbidden in result.answer_markdown
        for forbidden in ("口碑", "主題", "節奏", "適合", "五星", "觀眾")
    )


def test_invalid_recommendation_output_retries_once_then_uses_bound_fallback(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    swapped = {
        "items": [
            {"citation_id": "metadata:second", "reason": "錯配第一項"},
            {"citation_id": "metadata:first", "reason": "錯配第二項"},
        ]
    }
    client, models = _capture_sequence_client(monkeypatch, [swapped, swapped])

    result = client.generate_grounded(
        "推薦兩部電影",
        _fallback_evidence(),
        mode="recommendation",
        conversation_context=(),
        required_citation_ids=("metadata:first", "metadata:second"),
    )

    assert result == _expected_metadata_fallback()
    assert len(models.calls) == 2
    first_line, second_line = result.answer_markdown.splitlines()
    assert first_line.startswith("《第一部》：上映日期：1988-01-02")
    assert first_line.endswith("[metadata:first]")
    assert second_line.startswith("《第二部》：上映日期：1992-03-04")
    assert second_line.endswith("[metadata:second]")


def test_invalid_recommendation_output_gets_one_bounded_retry_before_success(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    invalid = {"items": [{"citation_id": "metadata:first", "reason": "少一項"}]}
    valid = {
        "items": [
            {"citation_id": "metadata:first", "reason": "第二次輸出有效甲"},
            {"citation_id": "metadata:second", "reason": "第二次輸出有效乙"},
        ]
    }
    client, models = _capture_sequence_client(monkeypatch, [invalid, valid])

    result = client.generate_grounded(
        "推薦兩部電影",
        _fallback_evidence(),
        mode="recommendation",
        conversation_context=(),
        required_citation_ids=("metadata:first", "metadata:second"),
    )

    assert result == GeneratedAnswer(
        "《第一部》：第二次輸出有效甲。[metadata:first]\n"
        "《第二部》：第二次輸出有效乙。[metadata:second]",
        ("metadata:first", "metadata:second"),
    )
    assert len(models.calls) == 2


@pytest.mark.parametrize("mode", ["general", "deep_recommendation"])
@pytest.mark.parametrize("failure_kind", ["provider", "invalid"])
def test_general_and_deep_generation_fail_closed_without_metadata_fallback(
    monkeypatch: pytest.MonkeyPatch,
    mode: str,
    failure_kind: str,
) -> None:
    if failure_kind == "provider":
        outcome: object = RuntimeError("provider detail must not escape")
    elif mode == "general":
        outcome = {"unexpected": "payload"}
    else:
        outcome = {"items": []}
    client, models = _capture_sequence_client(monkeypatch, [outcome])
    passages = _fallback_evidence()[:1] if mode == "general" else _deep_evidence()
    question = "香港電影資料" if mode == "general" else "分析第一部的視覺美學"
    required_ids = () if mode == "general" else ("pdf:first:p1",)

    with pytest.raises(VertexGenerationError):
        client.generate_grounded(
            question,
            passages,
            mode=mode,
            conversation_context=(),
            required_citation_ids=required_ids,
        )

    assert len(models.calls) == 1


def test_recommendation_rejects_non_metadata_evidence_before_provider_call(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    client, models = _capture_sequence_client(
        monkeypatch, [RuntimeError("must not be called")]
    )

    with pytest.raises(VertexGenerationError, match="recommendation"):
        client.generate_grounded(
            "推薦一部電影",
            _deep_evidence(),
            mode="recommendation",
            conversation_context=(),
            required_citation_ids=("pdf:first:p1",),
        )

    assert models.calls == []


@pytest.mark.parametrize(
    "items",
    [
        [{"citation_id": "metadata:first", "reason": "只有一項"}],
        [
            {"citation_id": "metadata:second", "reason": "次序交換"},
            {"citation_id": "metadata:first", "reason": "次序交換"},
        ],
        [
            {"citation_id": "metadata:first", "reason": "重複"},
            {"citation_id": "metadata:first", "reason": "重複"},
        ],
        [
            {"citation_id": "metadata:first", "reason": "正常"},
            {"citation_id": "metadata:foreign", "reason": "外來引用"},
        ],
    ],
    ids=["missing", "swapped", "duplicate", "foreign"],
)
def test_recommendation_generation_revalidates_exact_ordered_citation_items(
    monkeypatch: pytest.MonkeyPatch, items: list[dict[str, str]]
) -> None:
    """Breaks if response-schema drift alone can authorize a foreign or reordered item."""
    client, _, _ = _capture_client(monkeypatch, {"items": items})

    with pytest.raises(VertexGenerationError, match="recommendation"):
        client.generate_grounded(
            "推薦兩部電影",
            _evidence(),
            mode="recommendation",
            conversation_context=(),
            required_citation_ids=("metadata:first", "metadata:second"),
        )


@pytest.mark.parametrize(
    "reason",
    [
        "加入 [metadata:first] 引用",
        "直接提及 metadata:second",
        "外來電影 second_movie",
        "改名為第二部",
        "Use First Film as the title",
        "詳見 https://example.invalid/path",
        "詳見 /private/source.pdf",
        "理由\n第二行",
        "字" * 241,
        "避免影射第\u200b二部",
        "避免影射第 二 部",
        "避免影射第-二部",
        "避免影射第\ufe0f二部",
        "避免影射第\u034f二部",
        "正常理由\u202e",
        "Use Ｓｅｃｏｎｄ Ｆｉｌｍ as a hidden identity",
        "",
    ],
    ids=[
        "citation-marker",
        "citation-id",
        "movie-id",
        "chinese-title",
        "english-title",
        "url",
        "path",
        "newline",
        "overlong",
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
def test_recommendation_reason_rejects_model_controlled_identity_or_location(
    monkeypatch: pytest.MonkeyPatch, reason: str
) -> None:
    """Breaks if untrusted reason text can inject a title, identity, URL, or extra line."""
    client, _, _ = _capture_client(
        monkeypatch,
        {
            "items": [
                {"citation_id": "metadata:first", "reason": reason},
                {"citation_id": "metadata:second", "reason": "正常理由"},
            ]
        },
    )

    with pytest.raises(VertexGenerationError, match="recommendation"):
        client.generate_grounded(
            "推薦兩部電影",
            _evidence(),
            mode="recommendation",
            conversation_context=(),
            required_citation_ids=("metadata:first", "metadata:second"),
        )


def test_general_generation_keeps_the_existing_answer_and_citation_schema(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Breaks if recommendation hardening changes the public general-generation contract."""
    client, models, _ = _capture_client(
        monkeypatch,
        {
            "answer_markdown": "有受控資料。[metadata:first]",
            "citation_ids": ["metadata:first"],
        },
    )

    result = client.generate_grounded(
        "香港電影資料",
        (_evidence()[0],),
        mode="general",
        conversation_context=(),
        required_citation_ids=(),
    )

    assert result == GeneratedAnswer(
        "有受控資料。[metadata:first]", ("metadata:first",)
    )
    assert models.calls[0]["config"].response_json_schema["required"] == [
        "answer_markdown",
        "citation_ids",
    ]
    citation_schema = models.calls[0]["config"].response_json_schema["properties"][
        "citation_ids"
    ]
    assert citation_schema["minItems"] == 1
    assert citation_schema["maxItems"] == 1
    assert citation_schema["items"]["enum"] == ["metadata:first"]


def test_generation_client_configures_exact_bounded_sdk_retry_and_timeout(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Breaks if generation becomes unbounded or retries a permanent HTTP status."""
    _, _, observed = _capture_client(
        monkeypatch,
        {"answer_markdown": "有證據。[metadata:first]", "citation_ids": ["metadata:first"]},
    )

    options = observed["http_options"]
    retry = options.retry_options
    assert options.timeout == 20_000
    assert retry.attempts == 3
    assert retry.initial_delay == 0.25
    assert retry.max_delay == 1.0
    assert retry.exp_base == 2.0
    assert retry.jitter == 0.1
    assert retry.http_status_codes == [408, 429, 500, 502, 503, 504]


def _vertex_success(payload: dict[str, object]) -> httpx.Response:
    body = json.dumps(payload, ensure_ascii=False, separators=(",", ":"))
    return httpx.Response(
        200,
        json={
            "candidates": [
                {
                    "content": {"role": "model", "parts": [{"text": body}]},
                    "finishReason": "STOP",
                }
            ]
        },
    )


@contextmanager
def _real_transport_client(
    monkeypatch: pytest.MonkeyPatch,
    handler: Callable[[httpx.Request], httpx.Response],
) -> Iterator[VertexGenerationClient]:
    transport = httpx.MockTransport(handler)
    clients: list[httpx.Client] = []

    def factory(**kwargs: object) -> object:
        options = kwargs["http_options"]
        http_client = httpx.Client(transport=transport)
        clients.append(http_client)
        options.httpx_client = http_client
        return RealGenaiClient(**kwargs)

    monkeypatch.setattr("hk_movie_rag.vertex_clients.genai.Client", factory)
    try:
        yield VertexGenerationClient(
            "motionexpaiweb",
            "global",
            credentials=Credentials(token="test-access-token"),
        )
    finally:
        for client in clients:
            client.close()


def test_real_sdk_recommendation_request_uses_only_vertex_supported_schema_fields(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Breaks if the SDK sends JSON Schema string keywords rejected by Vertex."""
    observed: dict[str, object] = {}

    def handler(request: httpx.Request) -> httpx.Response:
        observed.update(json.loads(request.content))
        return _vertex_success(
            {
                "items": [
                    {"citation_id": "metadata:first", "reason": "受控理由"},
                    {"citation_id": "metadata:second", "reason": "另一受控理由"},
                ]
            }
        )

    with _real_transport_client(monkeypatch, handler) as client:
        client.generate_grounded(
            "推薦兩部電影",
            _evidence(),
            mode="recommendation",
            conversation_context=(),
            required_citation_ids=("metadata:first", "metadata:second"),
        )

    schema = observed["generationConfig"]["responseJsonSchema"]  # type: ignore[index]
    encoded = json.dumps(schema, ensure_ascii=False)
    assert '"minLength"' not in encoded
    assert '"maxLength"' not in encoded
    assert '"pattern"' not in encoded
    item_schema = schema["properties"]["items"]  # type: ignore[index]
    assert item_schema["minItems"] == 2
    assert item_schema["maxItems"] == 2
    assert [
        item["properties"]["citation_id"]["enum"]
        for item in item_schema["prefixItems"]
    ] == [["metadata:first"], ["metadata:second"]]


def test_sdk_retries_transient_http_to_success_without_logging_remote_detail(
    monkeypatch: pytest.MonkeyPatch, caplog: pytest.LogCaptureFixture
) -> None:
    """Breaks if the configured SDK logs remote bodies or does not reach attempt three."""
    attempts = 0
    canary = "vertex-server-secret-must-not-be-logged"

    def handler(request: httpx.Request) -> httpx.Response:
        nonlocal attempts
        attempts += 1
        if attempts < 3:
            return httpx.Response(
                503,
                request=request,
                json={"error": {"code": 503, "message": canary, "status": "UNAVAILABLE"}},
            )
        return _vertex_success(
            {
                "answer_markdown": "有受控資料。[metadata:first]",
                "citation_ids": ["metadata:first"],
            }
        )

    caplog.set_level(logging.INFO)
    with _real_transport_client(monkeypatch, handler) as client:
        result = client.generate_grounded(
            "香港電影資料",
            (_evidence()[0],),
            mode="general",
            conversation_context=(),
            required_citation_ids=(),
        )

    assert attempts == 3
    assert result.citation_ids == ("metadata:first",)
    assert canary not in caplog.text


def test_sdk_does_not_retry_permanent_http_and_adapter_traceback_is_redacted(
    monkeypatch: pytest.MonkeyPatch, caplog: pytest.LogCaptureFixture
) -> None:
    """Breaks if permanent failures retry or their remote detail survives exception chaining."""
    attempts = 0
    canary = "vertex-permanent-secret-must-not-escape"

    def handler(request: httpx.Request) -> httpx.Response:
        nonlocal attempts
        attempts += 1
        return httpx.Response(
            400,
            request=request,
            json={"error": {"code": 400, "message": canary, "status": "INVALID_ARGUMENT"}},
        )

    caplog.set_level(logging.INFO)
    with (
        _real_transport_client(monkeypatch, handler) as client,
        pytest.raises(
            VertexGenerationError, match="grounded generation request failed"
        ) as raised,
    ):
        client.generate_grounded(
            "香港電影資料",
            (_evidence()[0],),
            mode="general",
            conversation_context=(),
            required_citation_ids=(),
        )

    formatted = "".join(traceback.format_exception(raised.value))
    assert attempts == 1
    assert canary not in str(raised.value)
    assert canary not in formatted
    assert canary not in caplog.text


@pytest.mark.parametrize("failure", ["connect", "timeout"])
def test_sdk_retries_supported_transient_transport_classes_three_total_attempts(
    monkeypatch: pytest.MonkeyPatch, failure: str
) -> None:
    """Breaks if the installed SDK transport retry boundary is not activated."""
    attempts = 0

    def handler(request: httpx.Request) -> httpx.Response:
        nonlocal attempts
        attempts += 1
        if attempts < 3:
            if failure == "connect":
                raise httpx.ConnectError("transport canary", request=request)
            raise httpx.ReadTimeout("transport canary", request=request)
        return _vertex_success(
            {
                "answer_markdown": "有受控資料。[metadata:first]",
                "citation_ids": ["metadata:first"],
            }
        )

    with _real_transport_client(monkeypatch, handler) as client:
        result = client.generate_grounded(
            "香港電影資料",
            (_evidence()[0],),
            mode="general",
            conversation_context=(),
            required_citation_ids=(),
        )

    assert attempts == 3
    assert result.citation_ids == ("metadata:first",)


def test_exhausted_transient_failure_is_fixed_redacted_and_three_attempts(
    monkeypatch: pytest.MonkeyPatch, caplog: pytest.LogCaptureFixture
) -> None:
    """Breaks if exhausted retries expose the server body through logs or exception causes."""
    attempts = 0
    canary = "vertex-exhausted-secret-must-not-escape"

    def handler(request: httpx.Request) -> httpx.Response:
        nonlocal attempts
        attempts += 1
        return httpx.Response(
            503,
            request=request,
            json={"error": {"code": 503, "message": canary, "status": "UNAVAILABLE"}},
        )

    caplog.set_level(logging.INFO)
    with (
        _real_transport_client(monkeypatch, handler) as client,
        pytest.raises(
            VertexGenerationError, match="grounded generation request failed"
        ) as raised,
    ):
        client.generate_grounded(
            "香港電影資料",
            (_evidence()[0],),
            mode="general",
            conversation_context=(),
            required_citation_ids=(),
        )

    formatted = "".join(traceback.format_exception(raised.value))
    assert attempts == 3
    assert canary not in str(raised.value)
    assert canary not in formatted
    assert canary not in caplog.text
