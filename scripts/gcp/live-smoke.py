"""Fail-closed deployed demo smoke with secret-safe JSON output."""

from __future__ import annotations

import argparse
import hashlib
import http.cookiejar
import json
import os
import re
import sys
import tempfile
import urllib.error
import urllib.parse
import urllib.request
from collections.abc import Mapping, Sequence
from concurrent.futures import ThreadPoolExecutor
from dataclasses import dataclass
from datetime import date
from http.cookies import CookieError, Morsel, SimpleCookie
from pathlib import Path
from typing import Any, Literal, TypeGuard, cast

from hk_movie_rag.cloud_run_identity import is_cloud_run_revision_name
from hk_movie_rag.poster_authority import load_poster_serving_authority
from hk_movie_rag.rag_bundle import VerifiedRagBundle, verify_rag_bundle
from hk_movie_rag.relevance import (
    load_deep_answer_cases,
    load_general_relevance_policy,
)

_ENV_NAME = re.compile(r"^[A-Z_][A-Z0-9_]*$")
_IMAGE_DIGEST = re.compile(r"^sha256:[0-9a-f]{64}$")
_REFUSAL_LANGUAGE = re.compile(
    r"(?:目前沒有足夠的檢索證據|目前只有結構化電影資料|"
    r"不能提供沒有深度文檔支持的分析|抱歉|無法(?:回答|推薦|提供)|"
    r"\b(?:i (?:am )?unable to|i cannot|i can't|cannot|unable to)\b)",
    re.IGNORECASE,
)
_KNOWN_UNAVAILABLE_MOVIE_ID = "1970_MTXD_001"
_SESSION_COOKIE_NAME = "rag_demo_session"
_MAX_SESSION_TTL_SECONDS = 86_400
_DEFAULT_RESPONSE_BODY_LIMIT = 2 * 1024 * 1024
_POSTER_RESPONSE_BODY_LIMIT = 16 * 1024 * 1024
_MOVIE_ID = re.compile(r"^[A-Za-z0-9][A-Za-z0-9_-]{0,127}$")
_PDF_CITATION_ID = re.compile(r"pdf:([a-z0-9-]+):p([1-9][0-9]*)")
_PDF_AUTHORITIES = {
    "drunken-master-deep-analysis-v1": (
        "1978_ZQ_001",
        "S 級港⽚-醉拳.pdf",
    ),
    "aces-go-places-deep-analysis-v1": (
        "1982_ZJPD_001",
        "S 級港⽚-最佳拍檔.pdf",
    ),
}
_R2_RELEASE_ID = "v1.2-demo-r2"
_R2_PARENT_RELEASE_MANIFEST_SHA256 = (
    "e9842db33208e7146d78d86689b13f81fffcf41288d79d84e599b93050445e2c"
)
_R2_DERIVED_INVENTORY_SHA256 = (
    "b754727129bc044b6c9da1e6db22067c8279c5e7f8dd41d0350e365376538154"
)
_R2_RELEVANCE_POLICY_SHA256 = (
    "a4ecce727a469689a2a08c6d42e0d7d1eb975761173e3b6e4d1eaf34db7b2aba"
)
_R2_POSTER_AUTHORITY_SHA256 = (
    "9e56adda2a1fdfa648869c53d7e60a085db5d1c6d11db90dbec13bfe12f0eeed"
)
_R3_RELEASE_ID = "v1.2-demo-r3"
_R3_DERIVED_INVENTORY_SHA256 = (
    "3e3cedee507c3017821de1081bc70bbb01d0c73f8683ab484a464d1fc1f7c1db"
)
_R3_RELEVANCE_POLICY_SHA256 = (
    "d6db5e118aeea5dc0484f3c591baac666952f78f4d41bfdcdbbb58693f4f6bba"
)
_R3_POSTER_AUTHORITY_SHA256 = (
    "c50ff06a88f91296b63bd74a1e4a6485785bdc4c07e73d09b407a126927c8137"
)
_R2_RELEASE_TITLE_DEEP_QUESTION = "讲一下富贵逼人的喜剧创作"
_R2_RELEASE_TITLE_DEEP_MOVIE_ID = "1987_FGBR_001"
_R2_RELEASE_TITLE_DEEP_CITATION_ID = (
    "pdf:its-a-mad-mad-mad-world-deep-analysis-v1:p3"
)
_TITLE_ROUTING_SHORT_QUESTION = "請問一下醉拳的導演是誰？"
_TITLE_ROUTING_CONTEXTUAL_QUESTION = "錯體追擊組合中誰是導演？"
_TITLE_ROUTING_AMBIGUOUS_QUESTION = "《证人》的导演是谁？"
_TITLE_ROUTING_AMBIGUOUS_ANSWER = (
    "這個片名在 Release 內對應多部同名電影；請改用以下其中一個 "
    "movie ID：`1993_ZR_001`、`2008_ZR_001`。"
)
_TITLE_ROUTING_OOD_QUESTIONS = (
    "講一下英雄的成長",
    "聊聊朋友之間的信任",
    "《英雄》這本書值得讀嗎？",
)
_R2_DOCUMENT_AUTHORITIES: dict[str, dict[str, object]] = {
    "drunken-master-deep-analysis-v1": {
        "movie_id": "1978_ZQ_001",
        "document_id": "drunken-master-deep-analysis-v1",
        "source_filename": "S 級港⽚-醉拳.pdf",
        "source_sha256": (
            "ab27baf8e22ceb63a476b65dbfb02951a63a9e9bf39d96a47cd8e3f9fd314a75"
        ),
        "rights_status": "restricted",
        "quality_status": "manual_approved",
        "page_count": 3,
    },
    "aces-go-places-deep-analysis-v1": {
        "movie_id": "1982_ZJPD_001",
        "document_id": "aces-go-places-deep-analysis-v1",
        "source_filename": "S 級港⽚-最佳拍檔.pdf",
        "source_sha256": (
            "69fa1ad28916c043689ef4fe826cde816c05a3b254e5a0372d1a57ca2ce63bce"
        ),
        "rights_status": "restricted",
        "quality_status": "manual_approved",
        "page_count": 3,
    },
    "its-a-mad-mad-mad-world-deep-analysis-v1": {
        "movie_id": "1987_FGBR_001",
        "document_id": "its-a-mad-mad-mad-world-deep-analysis-v1",
        "source_filename": "S級港片-富貴逼人.pdf",
        "source_sha256": (
            "5aef7f8684bc0d91d77ffb6d7f89cb323358e2804ef8eaacbc0a02cfeb3d3df4"
        ),
        "rights_status": "restricted",
        "quality_status": "manual_approved",
        "page_count": 6,
    },
}
_R3_DOCUMENT_AUTHORITIES: dict[str, dict[str, object]] = {
    **_R2_DOCUMENT_AUTHORITIES,
    "mr-vampire-deep-analysis-v1": {
        "movie_id": "1985_JSXS_001",
        "document_id": "mr-vampire-deep-analysis-v1",
        "source_filename": "S 級港⽚-殭屍先生.pdf",
        "source_sha256": (
            "2dce5ec8a1459999c53e9a56f603225e3970061cc7d2f7a35b8d53388096e576"
        ),
        "rights_status": "restricted",
        "quality_status": "manual_approved",
        "page_count": 4,
    },
    "shaolin-soccer-deep-analysis-v1": {
        "movie_id": "2001_SLZQ_001",
        "document_id": "shaolin-soccer-deep-analysis-v1",
        "source_filename": "S 級港⽚-少林足球.pdf",
        "source_sha256": (
            "c6f713e2f5c60381d7b8745829f89680ebcfcb631c24d86814a9c6dfcd49e863"
        ),
        "rights_status": "restricted",
        "quality_status": "manual_approved",
        "page_count": 5,
    },
}
_EXPECTED_RELEVANCE_POLICY_SHA256 = (
    "5242afc5e5c91b74dd012b558ed5eb929f4acf78e98defe6d14bf4f7cce1a8c2"
)
_EXPECTED_DOMAIN_REFUSAL = "我目前只能回答这个 Release 内的香港电影问题。"
_EXPECTED_CONTROLLED_INSUFFICIENCY = (
    "這個問題需要結構化欄位以外的分析證據；目前 Release "
    "不足以可靠判斷這項條件。請改問具體片名，或使用年代、類型、"
    "導演、演員等已治理欄位。"
)
_LIMITED_EVIDENCE_DISCLOSURE = re.compile(
    r"(?:"
    r"(?:不能|不可)外推"
    r"|(?:僅|仅|只)基(?:於|于)"
    r"[^，,。！？!?；;：:\r\n]{0,32}(?:分析|深度文[檔档]|文[檔档]|[證证]據)"
    r"|(?:分析|深度文[檔档]|文[檔档]|[證证]據)"
    r"[^，,。！？!?；;：:\r\n]{0,16}(?:有限|少量)"
    r"|(?:有限|少量)"
    r"[^，,。！？!?；;：:\r\n]{0,16}(?:分析|深度文[檔档]|文[檔档]|[證证]據)"
    r")"
)
_CANONICAL_CHOW_NAME = "周星馳"
_CREDIT_SEPARATOR = re.compile(r"[、,，;/／|]+")
_INLINE_CITATION = re.compile(r"\[([^\[\]\r\n]+)\]")
_CITATION_KEYS = frozenset(
    {
        "citation_id",
        "movie_id",
        "movie_title",
        "source_kind",
        "page_number",
        "source_filename",
        "excerpt",
    }
)
_CONFIG_KEYS = frozenset(
    {
        "access_mode",
        "rag_release_id",
        "status",
        "embedding_model",
        "embedding_dimension",
        "generation_model",
        "relevance_policy_sha256",
        "poster_authority_sha256",
        "serving_revision",
        "image_digest",
        "counts",
        "facets",
    }
)
_R2_CONFIG_KEYS = frozenset((*_CONFIG_KEYS, "manifest_sha256"))
_GENERAL_RELEVANCE_GOLDEN_PATH = (
    Path(__file__).resolve().parents[2] / "evals" / "general_relevance_golden.jsonl"
)
_GENERAL_RELEVANCE_GOLDEN_SHA256 = (
    "f56b81c15d8876f1078356913740f6d028a7c13addd84252c01629301ae3c489"
)
_GENERAL_RELEVANCE_MOVIE_CASE_IDS = (
    "cal-movie-01",
    "cal-movie-02",
    "cal-movie-03",
    "cal-movie-04",
    "cal-movie-05",
    "cal-movie-06",
    "cal-movie-07",
    "cal-movie-08",
    "cal-movie-09",
    "cal-movie-10",
    "cal-movie-11",
    "cal-movie-12",
    "cal-movie-13",
    "cal-movie-14",
    "cal-movie-15",
    "hold-movie-01",
    "hold-movie-02",
    "hold-movie-03",
    "hold-movie-04",
    "hold-movie-05",
)
_GENERAL_RELEVANCE_OOD_CASE_IDS = (
    "cal-ood-01",
    "cal-ood-02",
    "cal-ood-03",
    "cal-ood-04",
    "cal-ood-05",
    "cal-ood-06",
    "cal-ood-07",
    "cal-ood-08",
    "cal-ood-09",
    "cal-ood-10",
    "cal-ood-11",
    "cal-ood-12",
    "cal-ood-13",
    "cal-ood-14",
    "cal-ood-15",
    "hold-ood-01",
    "hold-ood-02",
    "hold-ood-03",
    "hold-ood-04",
    "hold-ood-05",
)
_MovieCaseOutcome = Literal["structured", "bounded_pdf", "controlled_insufficient"]
_MOVIE_CASE_OUTCOMES: dict[str, _MovieCaseOutcome] = {
    "cal-movie-01": "bounded_pdf",
    "cal-movie-02": "bounded_pdf",
    "cal-movie-03": "structured",
    "cal-movie-04": "controlled_insufficient",
    "cal-movie-05": "bounded_pdf",
    "cal-movie-06": "bounded_pdf",
    "cal-movie-07": "structured",
    "cal-movie-08": "bounded_pdf",
    "cal-movie-09": "structured",
    "cal-movie-10": "controlled_insufficient",
    "cal-movie-11": "structured",
    "cal-movie-12": "structured",
    "cal-movie-13": "structured",
    "cal-movie-14": "structured",
    "cal-movie-15": "controlled_insufficient",
    "hold-movie-01": "structured",
    "hold-movie-02": "structured",
    "hold-movie-03": "structured",
    "hold-movie-04": "controlled_insufficient",
    "hold-movie-05": "structured",
}
_CONTROLLED_INSUFFICIENT_MOVIE_CASE_IDS = tuple(
    case_id
    for case_id, outcome in _MOVIE_CASE_OUTCOMES.items()
    if outcome == "controlled_insufficient"
)
_BOUNDED_PDF_MOVIE_CASE_IDS = tuple(
    case_id
    for case_id, outcome in _MOVIE_CASE_OUTCOMES.items()
    if outcome == "bounded_pdf"
)
_IPHONE_USER_AGENT = (
    "Mozilla/5.0 (iPhone; CPU iPhone OS 17_0 like Mac OS X) "
    "AppleWebKit/605.1.15 (KHTML, like Gecko) Version/17.0 "
    "Mobile/15E148 Safari/604.1"
)
_EXPECTED_COUNTS = {
    "movies": 4658,
    "media_assets": 4658,
    "metadata_passages": 4658,
    "movie_documents": 2,
    "pdf_passages": 6,
    "embeddings": 4664,
}
_EXPECTED_FACETS = {
    "total": 4658,
    "tier_s": 50,
    "tier_a": 313,
    "tier_b": 4295,
    "pilot_movies": 24,
}
_EXPECTED_POSTER_AUTHORITY_SHA256 = (
    "ca99a5c04d8e61029f58e90cb3017730dc2b3511361834d7a0cf4860b54005e4"
)
_EXPECTED_POSTERS: dict[str, dict[str, object]] = {
    "1978_ZQ_001": {
        "sha256": "a968160d2bafc410199e326a6dba6e357606ca0de8c38e6d0d9afd9caf0f8b3d",
        "size": 38140,
        "mime_type": "image/webp",
        "rights_status": "unknown",
    },
    "1982_ZJPD_001": {
        "sha256": "596ddd1d82855e6b7f3ca4c5e3c58af3655cb78d263cb21679464730dbba5b2b",
        "size": 41166,
        "mime_type": "image/webp",
        "rights_status": "unknown",
    },
}


class SmokeFailure(RuntimeError):
    """Raised when any live release contract is not proven."""


class _RejectRedirects(urllib.request.HTTPRedirectHandler):
    """Turn every redirect into an inspectable response without following it."""

    def redirect_request(
        self,
        request: urllib.request.Request,
        file_pointer: Any,
        code: int,
        message: str,
        headers: Any,
        new_url: str,
    ) -> None:
        del new_url
        raise urllib.error.HTTPError(
            request.full_url,
            code,
            message,
            headers,
            file_pointer,
        )


def _no_redirect_opener(
    *handlers: urllib.request.BaseHandler,
) -> urllib.request.OpenerDirector:
    return urllib.request.build_opener(_RejectRedirects(), *handlers)


@dataclass(frozen=True)
class HttpResult:
    status: int
    headers: Mapping[str, str]
    body: bytes
    set_cookie_headers: tuple[str, ...] = ()

    def json(self) -> Mapping[str, Any]:
        try:
            value = json.loads(self.body.decode("utf-8"))
        except (UnicodeDecodeError, json.JSONDecodeError) as exc:
            raise SmokeFailure("response is not valid UTF-8 JSON") from exc
        if not isinstance(value, dict):
            raise SmokeFailure("response JSON is not an object")
        return value


@dataclass(frozen=True)
class _CitationAuthority:
    movie_id: str
    source_kind: Literal["movie_metadata", "pdf_page"]
    page_number: int | None
    source_filename: str | None


@dataclass(frozen=True)
class _PosterExpectation:
    available: bool
    sha256: str | None
    size: int | None
    mime_type: str | None
    rights_status: str


@dataclass(frozen=True)
class _SmokeAuthorities:
    release_id: str
    manifest_sha256: str
    access_mode: str
    embedding_model: str
    embedding_dimension: int
    generation_model: str
    relevance_policy_sha256: str
    poster_authority_sha256: str
    counts: Mapping[str, int]
    facets: Mapping[str, int]
    citation_authorities: Mapping[str, _CitationAuthority]
    poster_expectations: Mapping[str, _PosterExpectation]
    pilot_movie_ids: frozenset[str]
    deep_cases: tuple[dict[str, object], ...]


@dataclass(frozen=True)
class _MovieCaseContract:
    """Observable per-case checks; these do not claim to judge open-ended prose."""

    answer_term_groups: tuple[tuple[str, ...], ...]
    genre_all: tuple[str, ...] = ()
    genre_any: tuple[str, ...] = ()
    year_from: int | None = None
    year_to: int | None = None
    director_any: tuple[str, ...] = ()
    cast_any: tuple[str, ...] = ()
    title_terms_any: tuple[str, ...] = ()
    expected_movie_ids: tuple[str, ...] = ()
    expected_passage_ids: tuple[str, ...] = ()
    expected_source_kind: str | None = None
    expected_movie_count: int | None = None


_MOVIE_CASE_CONTRACTS: dict[str, _MovieCaseContract] = {
    "cal-movie-01": _MovieCaseContract(
        (
            ("動作", "动作"),
            ("喜劇", "喜剧"),
            (
                "本次答案選取的深度文檔",
                "本次答案选取的深度文档",
                "目前 Release 內",
                "目前 Release 内",
            ),
            ("不能外推",),
        ),
        expected_movie_ids=("1978_ZQ_001", "1982_ZJPD_001"),
        expected_passage_ids=(
            "pdf:drunken-master-deep-analysis-v1:p1",
            "pdf:aces-go-places-deep-analysis-v1:p1",
        ),
        expected_source_kind="pdf_page",
        expected_movie_count=2,
    ),
    "cal-movie-02": _MovieCaseContract(
        (("功夫", "武打"), ("單片", "单片"), ("常見", "常见")),
        genre_any=("功夫", "動作"),
        year_from=1970,
        year_to=1979,
        expected_movie_ids=("1978_ZQ_001",),
        expected_passage_ids=("pdf:drunken-master-deep-analysis-v1:p1",),
        expected_source_kind="pdf_page",
        expected_movie_count=1,
    ),
    "cal-movie-03": _MovieCaseContract(
        (("警匪", "警察", "犯罪", "黑幫", "黑帮"),),
        genre_all=("犯罪",),
        year_from=1980,
        year_to=1989,
    ),
    "cal-movie-05": _MovieCaseContract(
        (("師徒", "师徒"), ("訓練", "训练"), ("不能概括",)),
        genre_any=("劇情", "動作", "功夫", "武俠"),
        expected_movie_ids=("1978_ZQ_001",),
        expected_passage_ids=("pdf:drunken-master-deep-analysis-v1:p2",),
        expected_source_kind="pdf_page",
        expected_movie_count=1,
    ),
    "cal-movie-06": _MovieCaseContract(
        (
            ("都市", "城市"),
            ("空間", "空间"),
            ("中環", "中环", "隧道", "貨櫃", "货柜"),
            ("不能概括",),
        ),
        genre_any=("劇情", "犯罪", "愛情", "動作", "喜劇"),
        expected_movie_ids=("1982_ZJPD_001",),
        expected_passage_ids=("pdf:aces-go-places-deep-analysis-v1:p1",),
        expected_source_kind="pdf_page",
        expected_movie_count=1,
    ),
    "cal-movie-07": _MovieCaseContract(
        (("武打", "功夫", "動作", "动作"), ("喜劇", "喜剧")),
        genre_all=("喜劇",),
        genre_any=("功夫", "動作", "武俠"),
    ),
    "cal-movie-08": _MovieCaseContract(
        (
            ("節奏明快", "节奏明快", "高密度節奏"),
            ("動作", "动作"),
            ("未被本次證據覆蓋", "未被本次证据覆盖"),
        ),
        genre_any=("動作",),
        expected_movie_ids=("1982_ZJPD_001",),
        expected_passage_ids=("pdf:aces-go-places-deep-analysis-v1:p1",),
        expected_source_kind="pdf_page",
        expected_movie_count=1,
    ),
    "cal-movie-09": _MovieCaseContract(
        (("兄弟", "手足"), ("片名",), ("不證明", "不证明")),
        genre_any=("劇情", "動作", "犯罪"),
        title_terms_any=("兄弟", "手足"),
    ),
    "cal-movie-11": _MovieCaseContract(
        (("女性", "女導演", "女导演"), ("受控",), ("精確姓名", "精确姓名")),
        director_any=(
            "許鞍華",
            "张婉婷",
            "張婉婷",
            "羅卓瑤",
            "麦曦茵",
            "麥曦茵",
            "黃真真",
            "岸西",
        ),
    ),
    "cal-movie-12": _MovieCaseContract(
        (("殭屍", "僵尸"), ("片名",), ("不證明", "不证明")),
        genre_any=("恐怖", "喜劇"),
        title_terms_any=("殭屍", "僵屍"),
    ),
    "cal-movie-13": _MovieCaseContract(
        (("賭", "赌"), ("片名",), ("不證明", "不证明")),
        title_terms_any=("賭", "千王", "雀聖", "麻雀", "撲克"),
    ),
    "cal-movie-14": _MovieCaseContract((("武俠", "武侠"),), genre_all=("武俠",)),
    "hold-movie-01": _MovieCaseContract(
        (("武俠", "武侠"),), genre_all=("武俠",), year_from=1960, year_to=1979
    ),
    "hold-movie-02": _MovieCaseContract(
        (("愛情", "爱情", "戀愛", "恋爱"),), genre_all=("愛情",)
    ),
    "hold-movie-03": _MovieCaseContract(
        (
            ("演員", "演员", "卡司"),
            ("不代表",),
            ("主角",),
        ),
        genre_all=("劇情",),
        cast_any=(
            "張曼玉",
            "林青霞",
            "梅艷芳",
            "蕭芳芳",
            "鄭秀文",
            "惠英紅",
            "楊紫瓊",
            "袁詠儀",
            "舒淇",
            "王祖賢",
            "鍾楚紅",
        ),
        expected_movie_count=1,
    ),
    "hold-movie-05": _MovieCaseContract(
        (
            ("家庭",),
            ("年齡分級", "年龄分级"),
            ("不證明", "不证明"),
        ),
        genre_all=("家庭",),
    ),
}


def _bounded_case_contract_is_exact(case_id: str) -> bool:
    contract = _MOVIE_CASE_CONTRACTS.get(case_id)
    if (
        contract is None
        or not contract.answer_term_groups
        or not contract.expected_movie_ids
        or len(contract.expected_movie_ids) != len(contract.expected_passage_ids)
        or len(set(contract.expected_movie_ids)) != len(contract.expected_movie_ids)
        or len(set(contract.expected_passage_ids)) != len(
            contract.expected_passage_ids
        )
        or contract.expected_source_kind != "pdf_page"
        or contract.expected_movie_count != len(contract.expected_movie_ids)
    ):
        return False
    for movie_id, passage_id in zip(
        contract.expected_movie_ids, contract.expected_passage_ids, strict=True
    ):
        match = _PDF_CITATION_ID.fullmatch(passage_id)
        if match is None:
            return False
        authority = _PDF_AUTHORITIES.get(match.group(1))
        if authority is None or authority[0] != movie_id:
            return False
    return True


def _load_general_relevance_golden(
    path: Path | None = None,
) -> tuple[dict[str, str], ...]:
    artifact = path or _GENERAL_RELEVANCE_GOLDEN_PATH
    try:
        raw = artifact.read_bytes()
        if hashlib.sha256(raw).hexdigest() != _GENERAL_RELEVANCE_GOLDEN_SHA256:
            raise SmokeFailure("general relevance golden artifact is invalid")
        rows: list[dict[str, str]] = []
        for line in raw.decode("utf-8").splitlines():
            value = json.loads(line)
            if (
                not isinstance(value, dict)
                or set(value) != {"case_id", "expected_domain", "question", "split"}
                or not all(isinstance(item, str) and item for item in value.values())
                or value["expected_domain"] not in {"movie", "ood"}
                or value["split"] not in {"calibration", "holdout"}
            ):
                raise SmokeFailure("general relevance golden artifact is invalid")
            rows.append(cast(dict[str, str], value))
    except (OSError, UnicodeDecodeError, json.JSONDecodeError, SmokeFailure) as exc:
        raise SmokeFailure("general relevance golden artifact is invalid") from exc
    movie_ids = tuple(
        row["case_id"] for row in rows if row["expected_domain"] == "movie"
    )
    ood_ids = tuple(row["case_id"] for row in rows if row["expected_domain"] == "ood")
    if (
        movie_ids != _GENERAL_RELEVANCE_MOVIE_CASE_IDS
        or ood_ids != _GENERAL_RELEVANCE_OOD_CASE_IDS
        or len({row["case_id"] for row in rows}) != 40
        or tuple(_MOVIE_CASE_OUTCOMES) != _GENERAL_RELEVANCE_MOVIE_CASE_IDS
        or tuple(_MOVIE_CASE_OUTCOMES.values()).count("structured") != 11
        or tuple(_MOVIE_CASE_OUTCOMES.values()).count("bounded_pdf") != 5
        or tuple(_MOVIE_CASE_OUTCOMES.values()).count("controlled_insufficient") != 4
        or set(_MOVIE_CASE_CONTRACTS)
        != {
            case_id
            for case_id, outcome in _MOVIE_CASE_OUTCOMES.items()
            if outcome != "controlled_insufficient"
        }
        or len(set(_MOVIE_CASE_CONTRACTS.values())) != len(_MOVIE_CASE_CONTRACTS)
        or not all(
            _bounded_case_contract_is_exact(case_id)
            for case_id in _BOUNDED_PDF_MOVIE_CASE_IDS
        )
    ):
        raise SmokeFailure("general relevance golden artifact is invalid")
    return tuple(rows)


def _load_smoke_authorities(manifest_path: Path) -> _SmokeAuthorities:
    """Verify the immutable bundle, then derive every live-answer authority from it."""
    try:
        bundle = verify_rag_bundle(manifest_path)
        contract = bundle.contract
        _assert_exact_release_authorities(bundle)
        counts = contract.counts
        if (
            contract.relevance_policy_sha256 is None
            or contract.poster_authority_sha256 is None
        ):
            raise SmokeFailure("verified release authorities are invalid")
        policy = load_general_relevance_policy(
            contract.rag_release_id, contract.relevance_policy_sha256
        )
        deep_cases = load_deep_answer_cases(
            contract.rag_release_id, contract.relevance_policy_sha256
        )
        poster_authority = load_poster_serving_authority(
            contract.rag_release_id,
            contract.poster_authority_sha256,
            contract=contract,
        )
        citations, posters, pilot_movie_ids = _derive_bundle_authorities(bundle)
        if (
            policy.artifact_sha256 != contract.relevance_policy_sha256
            or poster_authority.artifact_sha256 != contract.poster_authority_sha256
            or len(citations)
            != counts.metadata_passages + counts.pdf_passages
            or len(posters) != counts.poster_rows
            or len(pilot_movie_ids) != counts.pilot_count
            or _KNOWN_UNAVAILABLE_MOVIE_ID not in posters
            or posters[_KNOWN_UNAVAILABLE_MOVIE_ID].available
            or not _deep_cases_match_verified_authorities(deep_cases, citations)
        ):
            raise SmokeFailure("verified release authorities are invalid")
        return _SmokeAuthorities(
            release_id=contract.rag_release_id,
            manifest_sha256=contract.manifest_sha256,
            access_mode=contract.access_mode,
            embedding_model=contract.embedding_model,
            embedding_dimension=contract.embedding_dimension,
            generation_model=contract.generation_model,
            relevance_policy_sha256=policy.artifact_sha256,
            poster_authority_sha256=poster_authority.artifact_sha256,
            counts={
                "movies": counts.movies,
                "media_assets": counts.poster_rows,
                "metadata_passages": counts.metadata_passages,
                "movie_documents": counts.documents,
                "pdf_passages": counts.pdf_passages,
                "embeddings": contract.expected_embeddings,
            },
            facets={
                "total": counts.facet_count,
                "tier_s": counts.tier_s_count,
                "tier_a": counts.tier_a_count,
                "tier_b": counts.tier_b_count,
                "pilot_movies": counts.pilot_count,
            },
            citation_authorities=citations,
            poster_expectations=posters,
            pilot_movie_ids=frozenset(pilot_movie_ids),
            deep_cases=deep_cases,
        )
    except SmokeFailure:
        raise
    except Exception:  # noqa: BLE001 - artifact failures must remain one redacted boundary
        raise SmokeFailure("verified release authorities are invalid") from None


def _assert_exact_release_authorities(bundle: VerifiedRagBundle) -> None:
    """Accept only a reviewed immutable child and its exact document bindings."""
    contract = bundle.contract
    counts = contract.counts
    if contract.rag_release_id == _R2_RELEASE_ID:
        expected = (
            "1.2",
            _R2_DERIVED_INVENTORY_SHA256,
            4658,
            4658,
            50,
            4545,
            3,
            12,
            4670,
            _R2_RELEVANCE_POLICY_SHA256,
            _R2_POSTER_AUTHORITY_SHA256,
            _R2_DOCUMENT_AUTHORITIES,
        )
    elif contract.rag_release_id == _R3_RELEASE_ID:
        expected = (
            "1.3",
            _R3_DERIVED_INVENTORY_SHA256,
            4659,
            4659,
            51,
            4546,
            5,
            21,
            4680,
            _R3_RELEVANCE_POLICY_SHA256,
            _R3_POSTER_AUTHORITY_SHA256,
            _R3_DOCUMENT_AUTHORITIES,
        )
    else:
        raise SmokeFailure("verified release authorities are invalid")
    (
        expected_schema,
        expected_inventory,
        expected_movies,
        expected_facets,
        expected_tier_s,
        expected_approved_posters,
        expected_documents,
        expected_pdf_passages,
        expected_embeddings,
        expected_relevance_policy,
        expected_poster_authority,
        expected_document_authorities,
    ) = expected
    expected_document_authorities = cast(
        dict[str, dict[str, object]], expected_document_authorities
    )
    if (
        contract.schema_version != expected_schema
        or contract.parent_release_manifest_sha256
        != _R2_PARENT_RELEASE_MANIFEST_SHA256
        or re.fullmatch(r"[0-9a-f]{64}", contract.manifest_sha256) is None
        or re.fullmatch(r"[0-9a-f]{64}", contract.bundle_sha256) is None
        or contract.derived_inventory_sha256 != expected_inventory
        or counts.movies != expected_movies
        or counts.facet_count != expected_facets
        or counts.tier_s_count != expected_tier_s
        or counts.tier_a_count != 313
        or counts.tier_b_count != 4295
        or counts.pilot_count != 24
        or counts.poster_rows != expected_movies
        or counts.primary_poster_rows != expected_movies
        or counts.approved_poster_objects != expected_approved_posters
        or counts.unavailable_poster_rows != 113
        or counts.derived_poster_bytes <= 0
        or counts.metadata_passages != expected_movies
        or counts.documents != expected_documents
        or counts.pdf_passages != expected_pdf_passages
        or contract.expected_embeddings != expected_embeddings
        or contract.embedding_model != "gemini-embedding-2"
        or contract.embedding_dimension != 768
        or contract.generation_model != "gemini-3.5-flash-lite"
        or contract.text_extraction_profile != "cjk-layout-v1"
        or contract.document_embedding_profile != "vertex-title-text-v1"
        or contract.relevance_policy_sha256 != expected_relevance_policy
        or contract.poster_authority_sha256 != expected_poster_authority
        or contract.access_mode != "restricted_demo"
    ):
        raise SmokeFailure("verified release authorities are invalid")

    records = tuple(bundle.iter_records())
    observed_documents: dict[str, dict[str, object]] = {}
    observed_pdf_pages: set[tuple[str, str, str, int]] = set()
    for record in records:
        if record.get("record_kind") == "document":
            document_id = record.get("document_id")
            if not isinstance(document_id, str) or document_id in observed_documents:
                raise SmokeFailure("verified release authorities are invalid")
            observed_documents[document_id] = {
                key: value for key, value in record.items() if key != "record_kind"
            }
        elif record.get("record_kind") == "passage" and record.get(
            "passage_kind"
        ) == "pdf":
            passage_id = record.get("passage_id")
            movie_id = record.get("movie_id")
            document_id = record.get("document_id")
            page_number = record.get("page_number")
            if (
                not isinstance(passage_id, str)
                or not isinstance(movie_id, str)
                or not isinstance(document_id, str)
                or not isinstance(page_number, int)
                or isinstance(page_number, bool)
            ):
                raise SmokeFailure("verified release authorities are invalid")
            observed_pdf_pages.add(
                (passage_id, movie_id, document_id, page_number)
            )
    expected_pdf_pages = {
        (
            f"pdf:{document_id}:p{page_number}",
            str(document["movie_id"]),
            document_id,
            page_number,
        )
        for document_id, document in expected_document_authorities.items()
        for page_number in range(1, cast(int, document["page_count"]) + 1)
    }
    if (
        observed_documents != expected_document_authorities
        or observed_pdf_pages != expected_pdf_pages
    ):
        raise SmokeFailure("verified release authorities are invalid")


def _deep_cases_match_verified_authorities(
    deep_cases: tuple[dict[str, object], ...],
    citations: Mapping[str, _CitationAuthority],
) -> bool:
    if not deep_cases:
        return False
    for case in deep_cases:
        required_citations = case.get("required_citation_ids")
        expected_movie_ids = case.get("expected_movie_ids")
        if not isinstance(required_citations, list) or not isinstance(
            expected_movie_ids, list
        ):
            return False
        resolved = [citations.get(citation_id) for citation_id in required_citations]
        if any(authority is None for authority in resolved):
            return False
        resolved_movie_ids = list(
            dict.fromkeys(
                authority.movie_id
                for authority in resolved
                if authority is not None
            )
        )
        if resolved_movie_ids != expected_movie_ids:
            return False
    return True


def _derive_bundle_authorities(
    bundle: VerifiedRagBundle,
) -> tuple[
    dict[str, _CitationAuthority],
    dict[str, _PosterExpectation],
    set[str],
]:
    records = tuple(bundle.iter_records())
    documents: dict[str, tuple[str, str]] = {}
    for record in records:
        if record.get("record_kind") != "document":
            continue
        document_id = record.get("document_id")
        movie_id = record.get("movie_id")
        source_filename = record.get("source_filename")
        if (
            not isinstance(document_id, str)
            or re.fullmatch(r"[a-z0-9-]+", document_id) is None
            or not _valid_movie_id(movie_id)
            or not isinstance(source_filename, str)
            or not source_filename
            or document_id in documents
        ):
            raise SmokeFailure("verified release authorities are invalid")
        documents[document_id] = (movie_id, source_filename)

    citations: dict[str, _CitationAuthority] = {}
    posters: dict[str, _PosterExpectation] = {}
    pilot_movie_ids: set[str] = set()
    facet_count = 0
    for record in records:
        kind = record.get("record_kind")
        movie_id = record.get("movie_id")
        if kind == "facet":
            facet_count += 1
            if record.get("pilot_movie") is True and _valid_movie_id(movie_id):
                pilot_movie_ids.add(movie_id)
        elif kind == "passage":
            _add_passage_authority(citations, documents, record)
        elif kind == "poster":
            _add_poster_expectation(posters, record)
    if facet_count != bundle.contract.counts.facet_count:
        raise SmokeFailure("verified release authorities are invalid")
    return citations, posters, pilot_movie_ids


def _add_passage_authority(
    citations: dict[str, _CitationAuthority],
    documents: Mapping[str, tuple[str, str]],
    record: Mapping[str, object],
) -> None:
    passage_id = record.get("passage_id")
    movie_id = record.get("movie_id")
    if not isinstance(passage_id, str) or not _valid_movie_id(movie_id):
        raise SmokeFailure("verified release authorities are invalid")
    if record.get("passage_kind") == "metadata":
        authority = _CitationAuthority(movie_id, "movie_metadata", None, None)
        if passage_id != f"metadata:{movie_id}":
            raise SmokeFailure("verified release authorities are invalid")
    elif record.get("passage_kind") == "pdf":
        document_id = record.get("document_id")
        page_number = record.get("page_number")
        match = _PDF_CITATION_ID.fullmatch(passage_id)
        document = documents.get(document_id) if isinstance(document_id, str) else None
        if (
            match is None
            or document is None
            or match.group(1) != document_id
            or not isinstance(page_number, int)
            or isinstance(page_number, bool)
            or page_number <= 0
            or int(match.group(2)) != page_number
            or document[0] != movie_id
        ):
            raise SmokeFailure("verified release authorities are invalid")
        authority = _CitationAuthority(movie_id, "pdf_page", page_number, document[1])
    else:
        raise SmokeFailure("verified release authorities are invalid")
    if passage_id in citations:
        raise SmokeFailure("verified release authorities are invalid")
    citations[passage_id] = authority


def _add_poster_expectation(
    posters: dict[str, _PosterExpectation], record: Mapping[str, object]
) -> None:
    movie_id = record.get("movie_id")
    quality_status = record.get("quality_status")
    rights_status = record.get("rights_status")
    if (
        not _valid_movie_id(movie_id)
        or movie_id in posters
        or rights_status not in {"unknown", "restricted"}
        or record.get("is_primary") is not True
    ):
        raise SmokeFailure("verified release authorities are invalid")
    if quality_status in {"machine_passed", "manual_approved"}:
        digest = record.get("derived_content_sha256")
        size = record.get("derived_byte_length")
        mime_type = record.get("derived_mime_type")
        if (
            not isinstance(digest, str)
            or re.fullmatch(r"[0-9a-f]{64}", digest) is None
            or not isinstance(size, int)
            or isinstance(size, bool)
            or size <= 0
            or mime_type != "image/webp"
        ):
            raise SmokeFailure("verified release authorities are invalid")
        expectation = _PosterExpectation(True, digest, size, mime_type, rights_status)
    elif quality_status in {"content_conflict", "missing", "placeholder"}:
        if any(
            record.get(field) not in {None, ""}
            for field in (
                "derived_object_uri",
                "derived_content_sha256",
                "derived_byte_length",
                "derived_mime_type",
            )
        ):
            raise SmokeFailure("verified release authorities are invalid")
        expectation = _PosterExpectation(False, None, None, None, rights_status)
    else:
        raise SmokeFailure("verified release authorities are invalid")
    posters[movie_id] = expectation


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--base-url", required=True)
    parser.add_argument("--expected-revision", required=True)
    parser.add_argument("--expected-image-digest", required=True)
    parser.add_argument("--manifest", required=True, type=Path)
    parser.add_argument("--access-code-env", default="RAG_DEMO_ACCESS_KEY")
    parser.add_argument("--public-access", action="store_true")
    parser.add_argument("--timeout-seconds", type=float, default=30.0)
    parser.add_argument("--output", type=Path)
    args = parser.parse_args()

    checks = _empty_checks(public_access=args.public_access)
    report: dict[str, object] = {
        "base_url": "[UNVALIDATED]",
        "checks": checks,
        "passed": False,
        "schema_version": "rag-demo-live-smoke/v1",
    }
    if args.public_access:
        report["access_mode"] = "public_tech_demo"
    else:
        report["access_code"] = "[REDACTED]"
    exit_code = 0
    try:
        report["base_url"] = _validated_base_url(args.base_url)
        access_code: str | None = None
        if not args.public_access:
            if not _ENV_NAME.fullmatch(args.access_code_env):
                raise SmokeFailure("access-code environment variable name is invalid")
            access_code = os.environ.get(args.access_code_env)
            if not access_code:
                raise SmokeFailure("access-code environment variable is missing")
        if not 0 < args.timeout_seconds <= 120:
            raise SmokeFailure("timeout must be between 0 and 120 seconds")
        if (
            not is_cloud_run_revision_name(args.expected_revision)
            or _IMAGE_DIGEST.fullmatch(args.expected_image_digest) is None
        ):
            raise SmokeFailure("expected deployment identity is invalid")
        authorities = _load_smoke_authorities(args.manifest)
        _execute(
            report["base_url"],
            access_code,
            args.timeout_seconds,
            checks,
            expected_revision=args.expected_revision,
            expected_image_digest=args.expected_image_digest,
            authorities=authorities,
            public_access=args.public_access,
        )
        report["passed"] = True
    except SmokeFailure as exc:
        report["error"] = str(exc)
        exit_code = 1
    encoded = _encoded_report(report)
    if args.output is not None:
        try:
            _atomic_write(args.output, f"{encoded}\n")
        except OSError:
            report["passed"] = False
            report["error"] = "output report write failed"
            encoded = _encoded_report(report)
            exit_code = 1
    print(encoded)
    return exit_code


def _execute(
    base_url: object,
    access_code: str | None,
    timeout: float,
    checks: dict[str, bool | int],
    *,
    expected_revision: str,
    expected_image_digest: str,
    authorities: _SmokeAuthorities | None = None,
    public_access: bool = False,
) -> None:
    if not isinstance(base_url, str):  # pragma: no cover - constructed locally
        raise SmokeFailure("base URL is invalid")
    public = _no_redirect_opener()
    health = _request(public, base_url, "GET", "/health", timeout=timeout)
    if health.status != 200 or health.json() != {"status": "ok"}:
        raise SmokeFailure("public health check failed")
    checks["public_health"] = True

    if public_access:
        legacy_session = _request(
            public,
            base_url,
            "POST",
            "/api/session",
            payload={"access_code": "removed-public-demo-probe"},
            timeout=timeout,
        )
        if legacy_session.status != 404 or _sets_session_cookie(legacy_session):
            raise SmokeFailure("legacy session endpoint is still available")
        checks["legacy_session_removed"] = True
        authenticated = public
        session_cookie = ""
    else:
        if access_code is None:  # pragma: no cover - guarded by main
            raise SmokeFailure("access-code environment variable is missing")
        rejected_chat = _request(
            public,
            base_url,
            "POST",
            "/api/chat",
            payload={"question": "醉拳是哪一年上映？"},
            timeout=timeout,
        )
        if rejected_chat.status != 401:
            raise SmokeFailure("unauthenticated chat was not rejected")
        checks["unauthenticated_chat_rejected"] = True

        rejected_poster = _request(
            public,
            base_url,
            "GET",
            "/api/posters/1978_ZQ_001",
            timeout=timeout,
        )
        if rejected_poster.status != 401:
            raise SmokeFailure("unauthenticated poster was not rejected")
        checks["unauthenticated_poster_rejected"] = True

        wrong_login = _request(
            public,
            base_url,
            "POST",
            "/api/session",
            payload={"access_code": _deterministic_wrong_access_code(access_code)},
            timeout=timeout,
        )
        if wrong_login.status != 401 or _sets_session_cookie(wrong_login):
            raise SmokeFailure("incorrect access code was accepted")
        checks["incorrect_access_code_rejected"] = True

        cookies = http.cookiejar.CookieJar()
        authenticated = _no_redirect_opener(urllib.request.HTTPCookieProcessor(cookies))
        login = _request(
            authenticated,
            base_url,
            "POST",
            "/api/session",
            payload={"access_code": access_code},
            timeout=timeout,
        )
        if login.status != 204:
            raise SmokeFailure("demo login failed")
        session_cookie = _validated_session_cookie(login, cookies, access_code)
        checks["login"] = True

    config = _request(
        authenticated,
        base_url,
        "GET",
        "/api/config",
        headers={"Cookie": session_cookie} if session_cookie else None,
        timeout=timeout,
    )
    _assert_config(
        config,
        expected_revision=expected_revision,
        expected_image_digest=expected_image_digest,
        authorities=authorities,
        expected_access_mode="public_tech_demo" if public_access else "restricted_demo",
    )
    checks["public_config" if public_access else "authenticated_config"] = True

    if not public_access:
        _assert_tampered_session_rejected(public, base_url, session_cookie, timeout)
        checks["tampered_session_cookie_rejected"] = True

    movie_case_count, ood_case_count = _run_general_relevance_gate(
        authenticated,
        base_url,
        timeout,
        session_cookie,
        authorities=authorities,
    )
    checks["general_relevance_movie_cases"] = movie_case_count
    checks["general_relevance_ood_cases"] = ood_case_count
    checks["out_of_domain_rejected"] = True

    metadata = _chat(
        authenticated,
        base_url,
        "醉拳是哪一年上映，導演是誰？",
        timeout,
        session_cookie=session_cookie,
        authorities=authorities,
    )
    _assert_citation(metadata, "metadata:1978_ZQ_001", "1978_ZQ_001", "movie_metadata")
    _assert_known_metadata_answer(metadata)
    checks["metadata_answer"] = True
    if public_access:
        checks["public_chat"] = True

    unavailable = _chat(
        authenticated,
        base_url,
        "《滿天星斗》是哪一年上映，導演是誰？",
        timeout,
        session_cookie=session_cookie,
        authorities=authorities,
    )
    _assert_citation(
        unavailable,
        f"metadata:{_KNOWN_UNAVAILABLE_MOVIE_ID}",
        _KNOWN_UNAVAILABLE_MOVIE_ID,
        "movie_metadata",
    )
    _assert_null_poster_card(unavailable, _KNOWN_UNAVAILABLE_MOVIE_ID)
    checks["known_unavailable_poster_card"] = True
    checks["known_unavailable_poster"] = True

    checks["title_routing_regressions"] = _run_title_routing_regression_gate(
        authenticated,
        base_url,
        timeout,
        session_cookie,
        authorities=authorities,
    )

    drunken = _chat(
        authenticated,
        base_url,
        "《醉拳》的動作美學如何結合喜劇節奏？",
        timeout,
        session_cookie=session_cookie,
        authorities=authorities,
    )
    _assert_citation_prefix(
        drunken,
        "pdf:drunken-master-deep-analysis-v1:p",
        "1978_ZQ_001",
    )
    _assert_answer_term_groups(
        drunken,
        (
            ("醉拳", "drunken master"),
            ("功夫", "武打", "招式", "視覺", "视觉", "鏡頭", "镜头", "聲畫", "声画"),
            ("笑點", "笑点", "詼諧", "诙谐", "諧趣", "谐趣", "雜耍", "杂耍"),
            ("拍節", "拍节", "鑼鼓", "锣鼓"),
        ),
        "deep answer topic contract failed",
    )
    checks["deep_drunken_master"] = True

    aces = _chat(
        authenticated,
        base_url,
        "《最佳拍檔》的都市動作與喜劇風格有何特色？",
        timeout,
        session_cookie=session_cookie,
        authorities=authorities,
    )
    _assert_citation_prefix(
        aces,
        "pdf:aces-go-places-deep-analysis-v1:p",
        "1982_ZJPD_001",
    )
    _assert_answer_term_groups(
        aces,
        (
            ("最佳拍檔", "最佳拍档", "aces go places"),
            (
                "中環",
                "中环",
                "隧道",
                "貨櫃場",
                "货柜场",
                "茶餐廳",
                "茶餐厅",
                "公屋",
                "停車場",
                "停车场",
                "香港化",
            ),
            ("特技", "飛車", "飞车", "追逐", "廣角", "广角", "跟拍", "高密度"),
            (
                "喜感",
                "笑料",
                "荒誕",
                "荒诞",
                "俚語",
                "俚语",
                "智商錯位",
                "智商错位",
                "表情反差",
                "漫畫化",
                "漫画化",
                "市井",
            ),
            ("範式", "范式", "定位", "特徵", "特征", "三幕", "任務驅動", "任务驱动"),
        ),
        "deep answer topic contract failed",
    )
    checks["deep_aces_go_places"] = True

    if authorities is not None:
        checks["release_title_deep_query"] = _run_release_title_deep_query_gate(
            authenticated,
            base_url,
            timeout,
            session_cookie,
            authorities,
        )
        checks["deep_document_cases"] = _run_deep_answer_gate(
            authenticated,
            base_url,
            timeout,
            session_cookie,
            authorities,
        )
        _assert_completed_deep_case_count(
            checks["deep_document_cases"], authorities.deep_cases
        )

    comedy = _chat(
        authenticated,
        base_url,
        "請推薦三部喜劇電影。",
        timeout,
        session_cookie=session_cookie,
        authorities=authorities,
    )
    comedy_cards = _assert_structured_recommendation(comedy)
    if len(comedy_cards) != 3 or any(
        not _card_has_genre(card, "喜劇") for card in comedy_cards
    ) or not any(card.get("poster_url") is not None for card in comedy_cards):
        raise SmokeFailure("comedy recommendation contract failed")
    comedy_ordered_ids = [card["movie_id"] for card in comedy_cards]
    comedy_ids = {card["movie_id"] for card in comedy_cards}
    checks["comedy_exact_three"] = True

    followup = _chat(
        authenticated,
        base_url,
        "再推薦三部，不要重複。",
        timeout,
        history=[
            {
                "question": "請推薦三部喜劇電影。",
                "answer": str(comedy["answer_markdown"]),
                "movie_ids": comedy_ordered_ids,
            }
        ],
        session_cookie=session_cookie,
        authorities=authorities,
    )
    followup_cards = _assert_structured_recommendation(followup)
    followup_ids = {card["movie_id"] for card in followup_cards}
    if (
        len(followup_cards) != 3
        or any(not _card_has_genre(card, "喜劇") for card in followup_cards)
        or comedy_ids.intersection(followup_ids)
    ):
        raise SmokeFailure("history follow-up contract failed")
    checks["followup_inherits_comedy_and_excludes_prior"] = True

    person_default_simplified = _chat(
        authenticated,
        base_url,
        "推荐周星驰的好电影",
        timeout,
        session_cookie=session_cookie,
        authorities=authorities,
    )
    _assert_person_recommendation(
        person_default_simplified,
        expected_person="周星馳",
        allowed_roles=("cast", "director"),
        required_genres=frozenset(),
        expected_count=5,
    )
    checks["person_simplified_default_five"] = True

    person_default_traditional = _chat(
        authenticated,
        base_url,
        "推薦周星馳的好電影",
        timeout,
        session_cookie=session_cookie,
        authorities=authorities,
    )
    _assert_person_recommendation(
        person_default_traditional,
        expected_person="周星馳",
        allowed_roles=("cast", "director"),
        required_genres=frozenset(),
        expected_count=5,
    )
    checks["person_traditional_default_five"] = True

    person_simplified = _chat(
        authenticated,
        base_url,
        "推荐3部周星驰主演的电影。",
        timeout,
        session_cookie=session_cookie,
        authorities=authorities,
    )
    _assert_person_recommendation(
        person_simplified,
        expected_person="周星馳",
        allowed_roles=("cast",),
        required_genres=frozenset(),
        expected_count=3,
    )
    checks["person_simplified_exact_three"] = True

    person_traditional_question = "推薦3部周星馳主演的電影。"
    person_traditional = _chat(
        authenticated,
        base_url,
        person_traditional_question,
        timeout,
        session_cookie=session_cookie,
        authorities=authorities,
    )
    _assert_person_recommendation(
        person_traditional,
        expected_person="周星馳",
        allowed_roles=("cast",),
        required_genres=frozenset(),
        expected_count=3,
    )
    person_traditional_cards = _assert_structured_recommendation(person_traditional)
    checks["person_traditional_exact_three"] = True

    person_ordered_ids = [card["movie_id"] for card in person_traditional_cards]
    person_ids = set(person_ordered_ids)
    person_followup = _chat(
        authenticated,
        base_url,
        "再推薦3部，不要重複。",
        timeout,
        history=[
            {
                "question": person_traditional_question,
                "answer": str(person_traditional["answer_markdown"]),
                "movie_ids": person_ordered_ids,
            }
        ],
        session_cookie=session_cookie,
        authorities=authorities,
    )
    _assert_person_recommendation(
        person_followup,
        expected_person="周星馳",
        allowed_roles=("cast",),
        required_genres=frozenset(),
        expected_count=3,
    )
    person_followup_cards = _assert_structured_recommendation(person_followup)
    if person_ids.intersection(card["movie_id"] for card in person_followup_cards):
        raise SmokeFailure("person history follow-up contract failed")
    checks["person_followup_excludes_prior"] = True

    person_comedy = _chat(
        authenticated,
        base_url,
        "推薦3部周星馳主演的喜劇電影。",
        timeout,
        session_cookie=session_cookie,
        authorities=authorities,
    )
    _assert_person_recommendation(
        person_comedy,
        expected_person="周星馳",
        allowed_roles=("cast",),
        required_genres=frozenset({"喜劇"}),
        expected_count=3,
    )
    checks["person_and_genre_exact_three"] = True

    mixed_person = _chat(
        authenticated,
        base_url,
        "想看周星驰和成龍電影，推薦幾部",
        timeout,
        session_cookie=session_cookie,
        authorities=authorities,
        defer_artifact_validation=True,
    )
    if (
        mixed_person.get("answer_markdown")
        != "目前一次只支援一位正向人物；請只指定一位導演或演員後再試。"
        or mixed_person.get("citations") != []
        or mixed_person.get("movies") != []
    ):
        raise SmokeFailure("mixed person selection contract failed")
    checks["mixed_script_multi_person_rejected"] = True

    person_action_comedy_question = "周星驰的动作喜剧"
    person_action_comedy = _chat(
        authenticated,
        base_url,
        person_action_comedy_question,
        timeout,
        session_cookie=session_cookie,
        authorities=authorities,
    )
    _assert_person_recommendation(
        person_action_comedy,
        expected_person="周星馳",
        allowed_roles=("cast", "director"),
        required_genres=frozenset({"動作", "喜劇"}),
        expected_count=5,
    )
    checks["person_action_comedy_exact_five"] = True

    wang_kar_wai_question = "王家卫的电影"
    wang_kar_wai = _chat(
        authenticated,
        base_url,
        wang_kar_wai_question,
        timeout,
        session_cookie=session_cookie,
        authorities=authorities,
    )
    _assert_person_recommendation(
        wang_kar_wai,
        expected_person="王家衞",
        allowed_roles=("cast", "director"),
        required_genres=frozenset(),
        expected_count=5,
    )
    wang_kar_wai_cards = _assert_structured_recommendation(wang_kar_wai)
    wang_kar_wai_ids = {card["movie_id"] for card in wang_kar_wai_cards}
    person_continuation_reset_question = "周星驰的动作喜剧还有吗"
    person_action_comedy_after_wang = _chat(
        authenticated,
        base_url,
        person_continuation_reset_question,
        timeout,
        history=[
            {
                "question": wang_kar_wai_question,
                "answer": str(wang_kar_wai["answer_markdown"]),
                "movie_ids": [card["movie_id"] for card in wang_kar_wai_cards],
            }
        ],
        session_cookie=session_cookie,
        authorities=authorities,
    )
    try:
        _assert_person_recommendation(
            person_action_comedy_after_wang,
            expected_person="周星馳",
            allowed_roles=("cast", "director"),
            required_genres=frozenset({"動作", "喜劇"}),
            expected_count=5,
        )
        person_action_comedy_after_wang_cards = _assert_structured_recommendation(
            person_action_comedy_after_wang
        )
        if wang_kar_wai_ids.intersection(
            card["movie_id"] for card in person_action_comedy_after_wang_cards
        ):
            raise SmokeFailure("current person continuation reset contract failed")
    except SmokeFailure as exc:
        raise SmokeFailure("current person continuation reset contract failed") from exc
    checks["person_history_reset_action_comedy_exact_five"] = True
    checks["person_continuation_vocabulary_resets_history"] = True

    s_tier = _chat(
        authenticated,
        base_url,
        "只推薦 S 級電影三部。",
        timeout,
        session_cookie=session_cookie,
        authorities=authorities,
    )
    s_tier_cards = _assert_structured_recommendation(s_tier)
    if (
        len(s_tier_cards) != 3
        or any(card["tier"] != "S" for card in s_tier_cards)
        or any(card["movie_id"] == "2016_SFB_001" for card in s_tier_cards)
    ):
        raise SmokeFailure("S-tier recommendation contract failed")
    checks["s_tier_only_excludes_b_tier"] = True
    checks["returned_posters"] = True

    if authorities is not None:
        b_tier = _chat(
            authenticated,
            base_url,
            "只推薦 B 級電影三部。",
            timeout,
            session_cookie=session_cookie,
            authorities=authorities,
        )
        b_tier_cards = _assert_structured_recommendation(b_tier)
        if (
            len(b_tier_cards) != 3
            or any(card["tier"] != "B" for card in b_tier_cards)
            or all(
                cast(str, card["movie_id"]) in authorities.pilot_movie_ids
                for card in b_tier_cards
            )
        ):
            raise SmokeFailure("corpus-wide recommendation contract failed")
        checks["corpus_wide_recommendation"] = True

    expected_poster_ids = (
        tuple(
            dict.fromkeys(
                authority.movie_id
                for authority in authorities.citation_authorities.values()
                if authority.source_kind == "pdf_page"
            )
        )
        if authorities is not None
        else ("1978_ZQ_001", "1982_ZJPD_001")
    )
    _assert_expected_posters(
        base_url,
        expected_poster_ids,
        timeout,
        session_cookie,
        poster_expectations=(
            authorities.poster_expectations if authorities is not None else None
        ),
    )
    checks["poster_drunken_master"] = True
    checks["poster_aces_go_places"] = True
    if public_access:
        checks["public_poster"] = True

    final_config = _request(
        authenticated,
        base_url,
        "GET",
        "/api/config",
        headers={"Cookie": session_cookie} if session_cookie else None,
        timeout=timeout,
    )
    _assert_config(
        final_config,
        expected_revision=expected_revision,
        expected_image_digest=expected_image_digest,
        authorities=authorities,
        expected_access_mode="public_tech_demo" if public_access else "restricted_demo",
    )

    if not public_access:
        logout = _request(
            authenticated,
            base_url,
            "DELETE",
            "/api/session",
            headers={"Cookie": session_cookie},
            timeout=timeout,
        )
        if logout.status != 204:
            raise SmokeFailure("demo logout failed")
        _assert_logout_cookie(logout)
        checks["logout"] = True
        post_logout_config = _request(public, base_url, "GET", "/api/config", timeout=timeout)
        if post_logout_config.status != 401:
            raise SmokeFailure("logout did not revoke config access")
        checks["post_logout_config_rejected"] = True
        post_logout_chat = _request(
            public,
            base_url,
            "POST",
            "/api/chat",
            payload={"question": "醉拳是哪一年上映？"},
            timeout=timeout,
        )
        if post_logout_chat.status != 401:
            raise SmokeFailure("logout did not revoke chat access")
        checks["post_logout_chat_rejected"] = True


def _run_deep_answer_gate(
    opener: urllib.request.OpenerDirector,
    base_url: str,
    timeout: float,
    session_cookie: str,
    authorities: _SmokeAuthorities,
) -> int:
    completed = 0
    for case in authorities.deep_cases:
        question = case.get("question")
        history = case.get("history")
        if not isinstance(question, str) or not isinstance(history, list):
            raise SmokeFailure("deep answer case authority is invalid")
        answer = _chat(
            opener,
            base_url,
            question,
            timeout,
            history=cast(list[dict[str, object]], history),
            session_cookie=session_cookie,
            authorities=authorities,
        )
        _assert_deep_answer_case(case, answer)
        completed += 1
    return completed


def _assert_completed_deep_case_count(
    completed: int, deep_cases: Sequence[Mapping[str, object]]
) -> None:
    if not deep_cases or completed != len(deep_cases):
        raise SmokeFailure("deep answer case authority is invalid")


def _run_release_title_deep_query_gate(
    opener: urllib.request.OpenerDirector,
    base_url: str,
    timeout: float,
    session_cookie: str,
    authorities: _SmokeAuthorities,
) -> bool:
    answer = _chat(
        opener,
        base_url,
        _R2_RELEASE_TITLE_DEEP_QUESTION,
        timeout,
        history=[],
        session_cookie=session_cookie,
        authorities=authorities,
    )
    _assert_release_title_deep_query(answer)
    return True


def _run_title_routing_regression_gate(
    opener: urllib.request.OpenerDirector,
    base_url: str,
    timeout: float,
    session_cookie: str,
    *,
    authorities: _SmokeAuthorities | None = None,
) -> bool:
    short_title = _chat(
        opener,
        base_url,
        _TITLE_ROUTING_SHORT_QUESTION,
        timeout,
        history=[],
        session_cookie=session_cookie,
        authorities=authorities,
    )
    _assert_citation(short_title, "metadata:1978_ZQ_001", "1978_ZQ_001", "movie_metadata")
    _assert_answer_term_groups(
        short_title,
        (("醉拳",), ("袁和平",)),
        "short-title routing regression failed",
    )

    contextual = _chat(
        opener,
        base_url,
        _TITLE_ROUTING_CONTEXTUAL_QUESTION,
        timeout,
        history=[],
        session_cookie=session_cookie,
        authorities=authorities,
    )
    _assert_citation(
        contextual,
        "metadata:1995_CTZJZH_001",
        "1995_CTZJZH_001",
        "movie_metadata",
    )
    _assert_answer_term_groups(
        contextual,
        (("錯體追擊組合", "错体追击组合"), ("董煒", "董瑋", "董炜", "董玮")),
        "contextual title routing regression failed",
    )

    ambiguous = _request(
        opener,
        base_url,
        "POST",
        "/api/chat",
        payload={"question": _TITLE_ROUTING_AMBIGUOUS_QUESTION, "history": []},
        headers={"Cookie": session_cookie},
        timeout=timeout,
    )
    if ambiguous.status != 200 or ambiguous.json() != {
        "answer_markdown": _TITLE_ROUTING_AMBIGUOUS_ANSWER,
        "citations": [],
        "movies": [],
    }:
        raise SmokeFailure("ambiguous title routing regression failed")

    for question in _TITLE_ROUTING_OOD_QUESTIONS:
        result = _request(
            opener,
            base_url,
            "POST",
            "/api/chat",
            payload={"question": question, "history": []},
            headers={"Cookie": session_cookie},
            timeout=timeout,
        )
        if result.status != 200 or result.json() != {
            "answer_markdown": _EXPECTED_DOMAIN_REFUSAL,
            "citations": [],
            "movies": [],
        }:
            raise SmokeFailure("title routing OOD regression failed")
    return True


def _assert_release_title_deep_query(answer: Mapping[str, Any]) -> None:
    citations = answer.get("citations")
    movies = answer.get("movies")
    if not isinstance(citations, list) or not isinstance(movies, list):
        raise SmokeFailure("release title deep query contract failed")
    citation_ids = [
        citation.get("citation_id") if isinstance(citation, dict) else None
        for citation in citations
    ]
    if (
        not citations
        or len(set(citation_ids)) != len(citation_ids)
        or _R2_RELEASE_TITLE_DEEP_CITATION_ID not in citation_ids
        or any(
            not isinstance(citation, dict)
            or citation.get("movie_id") != _R2_RELEASE_TITLE_DEEP_MOVIE_ID
            or citation.get("source_kind") != "pdf_page"
            for citation in citations
        )
        or [
            movie.get("movie_id") if isinstance(movie, dict) else None
            for movie in movies
        ]
        != [_R2_RELEASE_TITLE_DEEP_MOVIE_ID]
    ):
        raise SmokeFailure("release title deep query contract failed")
    _assert_answer_term_groups(
        answer,
        (
            ("富貴逼人", "富贵逼人"),
            ("喜劇", "喜剧", "笑點", "笑点", "處境", "处境"),
            (
                "階層焦慮",
                "阶层焦虑",
                "互相算計",
                "互相算计",
                "對白",
                "对白",
                "相聲",
                "相声",
            ),
        ),
        "release title deep query contract failed",
    )


def _assert_deep_answer_case(
    case: Mapping[str, object], answer: Mapping[str, Any]
) -> None:
    answer_markdown = answer.get("answer_markdown")
    citations = answer.get("citations")
    movies = answer.get("movies")
    if (
        not isinstance(answer_markdown, str)
        or not isinstance(citations, list)
        or not isinstance(movies, list)
    ):
        raise SmokeFailure("deep answer case contract failed")
    required_terms = cast(list[str], case.get("required_answer_terms"))
    required_any_terms = cast(list[str], case.get("required_any_answer_terms"))
    forbidden_terms = cast(list[str], case.get("forbidden_answer_terms"))
    required_citations = cast(list[str], case.get("required_citation_ids"))
    expected_movie_ids = cast(list[str], case.get("expected_movie_ids"))
    forbidden_source_kinds = cast(list[str], case.get("forbidden_source_kinds"))
    requires_limited_disclosure = case.get("requires_limited_evidence_disclosure")
    if (
        type(requires_limited_disclosure) is not bool
        or not all(term.casefold() in answer_markdown.casefold() for term in required_terms)
        or (
            required_any_terms
            and not any(
                term.casefold() in answer_markdown.casefold()
                for term in required_any_terms
            )
        )
        or any(term.casefold() in answer_markdown.casefold() for term in forbidden_terms)
        or (
            requires_limited_disclosure
            and _LIMITED_EVIDENCE_DISCLOSURE.search(answer_markdown) is None
        )
        or [
            citation.get("citation_id") if isinstance(citation, dict) else None
            for citation in citations
        ]
        != required_citations
        or any(
            isinstance(citation, dict)
            and citation.get("source_kind") in forbidden_source_kinds
            for citation in citations
        )
        or [
            movie.get("movie_id") if isinstance(movie, dict) else None
            for movie in movies
        ]
        != expected_movie_ids
    ):
        raise SmokeFailure("deep answer case contract failed")


def _run_general_relevance_gate(
    opener: urllib.request.OpenerDirector,
    base_url: str,
    timeout: float,
    session_cookie: str,
    *,
    authorities: _SmokeAuthorities | None = None,
) -> tuple[int, int]:
    movie_cases = 0
    ood_cases = 0
    for case in _load_general_relevance_golden():
        question = case["question"]
        if case["expected_domain"] == "movie":
            if case["case_id"] in _CONTROLLED_INSUFFICIENT_MOVIE_CASE_IDS:
                result = _request(
                    opener,
                    base_url,
                    "POST",
                    "/api/chat",
                    payload={"question": question, "history": []},
                    headers={"Cookie": session_cookie},
                    timeout=timeout,
                )
                if result.status != 200 or result.json() != {
                    "answer_markdown": _EXPECTED_CONTROLLED_INSUFFICIENCY,
                    "citations": [],
                    "movies": [],
                }:
                    raise SmokeFailure(
                        "general relevance controlled insufficiency contract failed"
                    )
                movie_cases += 1
                continue
            answer = _chat(
                opener,
                base_url,
                question,
                timeout,
                session_cookie=session_cookie,
                authorities=authorities,
            )
            _assert_general_relevance_movie_case(case["case_id"], answer)
            movie_cases += 1
            continue
        result = _request(
            opener,
            base_url,
            "POST",
            "/api/chat",
            payload={"question": question, "history": []},
            headers={"Cookie": session_cookie},
            timeout=timeout,
        )
        if result.status != 200 or result.json() != {
            "answer_markdown": _EXPECTED_DOMAIN_REFUSAL,
            "citations": [],
            "movies": [],
        }:
            raise SmokeFailure("general relevance OOD contract failed")
        ood_cases += 1
    if (movie_cases, ood_cases) != (20, 20):  # fail closed if constants drift
        raise SmokeFailure("general relevance live case count is invalid")
    return movie_cases, ood_cases


def _assert_general_relevance_movie_case(
    case_id: str, answer: Mapping[str, Any]
) -> None:
    contract = _MOVIE_CASE_CONTRACTS.get(case_id)
    answer_markdown = answer.get("answer_markdown")
    movies = answer.get("movies")
    citations = answer.get("citations")
    if (
        contract is None
        or not isinstance(answer_markdown, str)
        or not isinstance(movies, list)
        or not movies
    ):
        raise SmokeFailure("general relevance movie case contract failed")
    _assert_answer_term_groups(
        answer,
        contract.answer_term_groups,
        "general relevance movie case contract failed",
    )
    if any(not isinstance(movie, dict) for movie in movies):
        raise SmokeFailure("general relevance movie case contract failed")
    if contract.expected_movie_count is not None and len(movies) != contract.expected_movie_count:
        raise SmokeFailure("general relevance movie case contract failed")
    if contract.expected_movie_ids and (
        not isinstance(citations, list)
        or any(not isinstance(citation, dict) for citation in citations)
        or tuple(movie.get("movie_id") for movie in movies) != contract.expected_movie_ids
        or tuple(citation.get("movie_id") for citation in citations)
        != contract.expected_movie_ids
        or tuple(citation.get("citation_id") for citation in citations)
        != contract.expected_passage_ids
        or any(
            citation.get("source_kind") != contract.expected_source_kind
            for citation in citations
        )
    ):
        raise SmokeFailure("general relevance movie case contract failed")

    for movie in movies:
        genres = _split_card_values(movie.get("genre"))
        title_text = "\n".join(
            str(movie.get(field, ""))
            for field in ("chinese_title", "english_title")
        ).casefold()
        if (
            not set(contract.genre_all).issubset(genres)
            or (contract.genre_any and not set(contract.genre_any).intersection(genres))
            or (
                contract.director_any
                and not set(contract.director_any).intersection(
                    _split_card_values(movie.get("director"))
                )
            )
            or (
                contract.cast_any
                and not set(contract.cast_any).intersection(
                    _split_card_values(movie.get("cast"))
                )
            )
            or (
                contract.title_terms_any
                and not any(
                    term.casefold() in title_text
                    for term in contract.title_terms_any
                )
            )
        ):
            raise SmokeFailure("general relevance movie case contract failed")
        if contract.year_from is not None or contract.year_to is not None:
            year = _release_year(movie.get("release_date"))
            if year is None:
                raise SmokeFailure("general relevance movie case contract failed")
            if (
                contract.year_from is not None
                and year < contract.year_from
                or contract.year_to is not None
                and year > contract.year_to
            ):
                raise SmokeFailure("general relevance movie case contract failed")


def _assert_answer_term_groups(
    answer: Mapping[str, Any],
    term_groups: tuple[tuple[str, ...], ...],
    failure_message: str,
) -> None:
    answer_markdown = answer.get("answer_markdown")
    if not isinstance(answer_markdown, str):
        raise SmokeFailure(failure_message)
    folded_answer = answer_markdown.casefold()
    if any(
        not any(term.casefold() in folded_answer for term in alternatives)
        for alternatives in term_groups
    ):
        raise SmokeFailure(failure_message)


def _release_year(value: object) -> int | None:
    if not isinstance(value, str):
        return None
    if re.fullmatch(r"[0-9]{4}", value):
        year = int(value)
        try:
            date(year, 1, 1)
        except ValueError:
            return None
        return year
    if re.fullmatch(r"[0-9]{4}-[0-9]{2}-[0-9]{2}", value) is None:
        return None
    try:
        return date.fromisoformat(value).year
    except ValueError:
        return None


def _split_card_values(value: object) -> set[str]:
    if not isinstance(value, str):
        return set()
    return {
        item.strip()
        for item in _CREDIT_SEPARATOR.split(value)
        if item.strip()
    }


def _card_has_genre(card: Mapping[str, object], required_genre: str) -> bool:
    return required_genre in _split_card_values(card.get("genre"))


def _read_bounded(stream: Any, max_body_bytes: int) -> bytes:
    body = stream.read(max_body_bytes + 1)
    if not isinstance(body, bytes):
        raise SmokeFailure("HTTP response body is invalid")
    if len(body) > max_body_bytes:
        raise SmokeFailure("HTTP response body exceeds limit")
    return body


def _request(
    opener: urllib.request.OpenerDirector,
    base_url: str,
    method: str,
    path: str,
    *,
    payload: Mapping[str, object] | None = None,
    headers: Mapping[str, str] | None = None,
    timeout: float,
    max_body_bytes: int = _DEFAULT_RESPONSE_BODY_LIMIT,
) -> HttpResult:
    if max_body_bytes < 1:
        raise SmokeFailure("HTTP response body limit is invalid")
    body = None
    request_headers = {
        "Accept": "application/json",
        "User-Agent": "hk-rag-live-smoke/1",
        **(headers or {}),
    }
    if payload is not None:
        body = json.dumps(payload, ensure_ascii=False, separators=(",", ":")).encode("utf-8")
        request_headers["Content-Type"] = "application/json"
    request = urllib.request.Request(
        urllib.parse.urljoin(f"{base_url}/", path.lstrip("/")),
        data=body,
        headers=request_headers,
        method=method,
    )
    try:
        with opener.open(request, timeout=timeout) as response:
            return HttpResult(
                response.status,
                _casefolded_headers(response.headers),
                _read_bounded(response, max_body_bytes),
                _set_cookie_header_values(response.headers),
            )
    except urllib.error.HTTPError as exc:
        return HttpResult(
            exc.code,
            _casefolded_headers(cast(Mapping[str, str], exc.headers)),
            _read_bounded(exc, max_body_bytes),
            _set_cookie_header_values(exc.headers),
        )
    except (urllib.error.URLError, TimeoutError, OSError) as exc:
        raise SmokeFailure(f"HTTP request failed: {method} {path}") from exc


def _casefolded_headers(headers: Mapping[str, str]) -> dict[str, str]:
    return {name.casefold(): value for name, value in headers.items()}


def _set_cookie_header_values(headers: object) -> tuple[str, ...]:
    get_all = getattr(headers, "get_all", None)
    if callable(get_all):
        values = get_all("Set-Cookie")
        if values:
            return tuple(str(value) for value in values)
    if isinstance(headers, Mapping):
        value = next(
            (
                item
                for name, item in headers.items()
                if isinstance(name, str) and name.casefold() == "set-cookie"
            ),
            None,
        )
        if isinstance(value, str) and value:
            return (value,)
    return ()


def _response_set_cookie_headers(result: HttpResult) -> tuple[str, ...]:
    if result.set_cookie_headers:
        return result.set_cookie_headers
    raw_cookie = result.headers.get("set-cookie")
    return (raw_cookie,) if raw_cookie else ()


def _deterministic_wrong_access_code(access_code: str) -> str:
    # A credential-derived digest would be an offline verifier for a low-entropy
    # demo code if request bodies were logged. Use fixed probes only.
    candidates = (
        "__invalid_rag_demo_access_code_probe_1__",
        "__invalid_rag_demo_access_code_probe_2__",
    )
    return candidates[1] if access_code == candidates[0] else candidates[0]


def _sets_session_cookie(result: HttpResult) -> bool:
    return any(
        re.search(
            rf"(?:^|[,;\s]){re.escape(_SESSION_COOKIE_NAME)}\s*=",
            raw_cookie,
            re.IGNORECASE,
        )
        is not None
        for raw_cookie in _response_set_cookie_headers(result)
    )


def _session_cookie_morsel(
    result: HttpResult, failure_message: str
) -> Morsel[str]:
    session_morsels: list[Morsel[str]] = []
    session_assignments = 0
    for raw_cookie in _response_set_cookie_headers(result):
        session_assignments += len(
            re.findall(
                rf"(?:^|[,;\s]){re.escape(_SESSION_COOKIE_NAME)}\s*=",
                raw_cookie,
                re.IGNORECASE,
            )
        )
        parsed = SimpleCookie()
        try:
            parsed.load(raw_cookie)
        except CookieError as exc:
            raise SmokeFailure(failure_message) from exc
        morsel = parsed.get(_SESSION_COOKIE_NAME)
        if morsel is not None:
            session_morsels.append(morsel)
    if session_assignments != 1 or len(session_morsels) != 1:
        raise SmokeFailure(failure_message)
    return session_morsels[0]


def _has_required_cookie_attributes(morsel: Morsel[str]) -> bool:
    return (
        morsel["secure"] is True
        and morsel["httponly"] is True
        and morsel["samesite"].casefold() == "strict"
        and morsel["path"] == "/"
        and not morsel["domain"]
    )


def _validated_session_cookie(
    result: HttpResult,
    cookies: http.cookiejar.CookieJar,
    access_code: str,
) -> str:
    failure_message = "demo session cookie attributes are invalid"
    morsel = _session_cookie_morsel(result, failure_message)
    value = morsel.value
    try:
        max_age = int(morsel["max-age"])
    except (TypeError, ValueError) as exc:
        raise SmokeFailure(failure_message) from exc
    if (
        not value
        or value == access_code
        or any(character in value for character in "\r\n;")
        or not _has_required_cookie_attributes(morsel)
        or not 0 < max_age <= _MAX_SESSION_TTL_SECONDS
    ):
        raise SmokeFailure(failure_message)

    session_cookies = [
        cookie
        for cookie in cookies
        if cookie.name == _SESSION_COOKIE_NAME and cookie.value
    ]
    if len(session_cookies) != 1:
        raise SmokeFailure(failure_message)
    cookie = session_cookies[0]
    if (
        cookie.value != value
        or cookie.domain_specified
        or cookie.domain_initial_dot
        or not cookie.secure
        or cookie.path != "/"
    ):
        raise SmokeFailure(failure_message)
    return f"{cookie.name}={value}"


def _tampered_session_cookies(session_cookie: str) -> tuple[str, ...]:
    name, separator, value = session_cookie.partition("=")
    if name != _SESSION_COOKIE_NAME or separator != "=" or not value:
        raise SmokeFailure("demo session cookie attributes are invalid")
    first_replacement = "y" if value[0] == "x" else "x"
    last_replacement = "b" if value[-1] == "a" else "a"
    tampered_values = (
        f"{value}-tampered",
        f"tampered-{value}",
        f"{first_replacement}{value[1:]}",
        f"{value[:-1]}{last_replacement}",
        value[:-1],
    )
    if len(set(tampered_values)) != len(tampered_values):
        raise SmokeFailure("demo session cookie attributes are invalid")
    return tuple(f"{name}={tampered}" for tampered in tampered_values)


def _assert_tampered_session_rejected(
    opener: urllib.request.OpenerDirector,
    base_url: str,
    session_cookie: str,
    timeout: float,
) -> None:
    requests: tuple[tuple[str, str, Mapping[str, object] | None], ...] = (
        ("GET", "/api/config", None),
        ("POST", "/api/chat", {"question": "醉拳是哪一年上映？"}),
        ("GET", "/api/posters/1978_ZQ_001", None),
    )
    for tampered_cookie in _tampered_session_cookies(session_cookie):
        for method, path, payload in requests:
            result = _request(
                opener,
                base_url,
                method,
                path,
                payload=payload,
                headers={"Cookie": tampered_cookie},
                timeout=timeout,
            )
            if result.status != 401:
                raise SmokeFailure("tampered session cookie was not rejected")


def _assert_logout_cookie(result: HttpResult) -> None:
    failure_message = "logout session cookie is invalid"
    morsel = _session_cookie_morsel(result, failure_message)
    try:
        max_age = int(morsel["max-age"])
    except (TypeError, ValueError) as exc:
        raise SmokeFailure(failure_message) from exc
    if (
        morsel.value
        or max_age != 0
        or not _has_required_cookie_attributes(morsel)
    ):
        raise SmokeFailure(failure_message)


def _assert_config(
    result: HttpResult,
    *,
    expected_revision: str,
    expected_image_digest: str,
    authorities: _SmokeAuthorities | None = None,
    expected_access_mode: str | None = None,
) -> None:
    if result.status != 200:
        request_scope = (
            "public" if expected_access_mode == "public_tech_demo" else "authenticated"
        )
        raise SmokeFailure(f"{request_scope} config request failed")
    value = result.json()
    expected_release_id = authorities.release_id if authorities else "v1.2-demo"
    configured_access_mode = expected_access_mode or (
        authorities.access_mode if authorities else "restricted_demo"
    )
    expected_embedding_model = authorities.embedding_model if authorities else "gemini-embedding-2"
    expected_embedding_dimension = authorities.embedding_dimension if authorities else 768
    expected_generation_model = authorities.generation_model if authorities else "gemini-3.5-flash-lite"
    expected_relevance_policy = (
        authorities.relevance_policy_sha256
        if authorities
        else _EXPECTED_RELEVANCE_POLICY_SHA256
    )
    expected_poster_authority = (
        authorities.poster_authority_sha256
        if authorities
        else _EXPECTED_POSTER_AUTHORITY_SHA256
    )
    expected_counts = authorities.counts if authorities else _EXPECTED_COUNTS
    expected_facets = authorities.facets if authorities else _EXPECTED_FACETS
    expected_config_keys = _R2_CONFIG_KEYS if authorities else _CONFIG_KEYS
    if (
        set(value) != expected_config_keys
        or value.get("access_mode") != configured_access_mode
        or value.get("rag_release_id") != expected_release_id
        or value.get("status") != "active"
        or value.get("embedding_model") != expected_embedding_model
        or value.get("embedding_dimension") != expected_embedding_dimension
        or value.get("generation_model") != expected_generation_model
        or value.get("relevance_policy_sha256") != expected_relevance_policy
        or value.get("poster_authority_sha256") != expected_poster_authority
        or value.get("serving_revision") != expected_revision
        or value.get("image_digest") != expected_image_digest
        or (
            authorities is not None
            and value.get("manifest_sha256") != authorities.manifest_sha256
        )
        or value.get("counts") != expected_counts
        or value.get("facets") != expected_facets
    ):
        raise SmokeFailure("deployed config does not match the release contract")


def _chat(
    opener: urllib.request.OpenerDirector,
    base_url: str,
    question: str,
    timeout: float,
    history: list[dict[str, object]] | None = None,
    *,
    session_cookie: str = "",
    authorities: _SmokeAuthorities | None = None,
    defer_artifact_validation: bool = False,
) -> Mapping[str, Any]:
    result = _request(
        opener,
        base_url,
        "POST",
        "/api/chat",
        payload={"question": question, "history": history or []},
        headers={"Cookie": session_cookie} if session_cookie else None,
        timeout=timeout,
    )
    if result.status != 200:
        raise SmokeFailure("chat request failed")
    value = result.json()
    if not isinstance(value.get("answer_markdown"), str) or not value["answer_markdown"].strip():
        raise SmokeFailure("chat answer is empty")
    if _REFUSAL_LANGUAGE.search(value["answer_markdown"]):
        raise SmokeFailure("chat answer contains refusal language")
    if defer_artifact_validation:
        return value
    citation_authorities = authorities.citation_authorities if authorities else None
    poster_expectations = authorities.poster_expectations if authorities else None
    _assert_ordered_citation_card_equality(
        value, citation_authorities=citation_authorities
    )
    _assert_returned_posters(
        base_url,
        value,
        timeout,
        session_cookie,
        poster_expectations=poster_expectations,
    )
    return value


def _valid_movie_id(value: object) -> TypeGuard[str]:
    return isinstance(value, str) and _MOVIE_ID.fullmatch(value) is not None


def _valid_pdf_citation_binding(
    citation_id: object,
    movie_id: object,
    page_number: object,
    source_filename: object,
    citation_authorities: Mapping[str, _CitationAuthority] | None = None,
) -> bool:
    if (
        not isinstance(citation_id, str)
        or not _valid_movie_id(movie_id)
        or not isinstance(page_number, int)
        or isinstance(page_number, bool)
        or not isinstance(source_filename, str)
    ):
        return False
    if citation_authorities is not None:
        dynamic_authority = citation_authorities.get(citation_id)
        return (
            dynamic_authority is not None
            and dynamic_authority.source_kind == "pdf_page"
            and dynamic_authority.movie_id == movie_id
            and dynamic_authority.page_number == page_number
            and dynamic_authority.source_filename == source_filename
        )
    match = _PDF_CITATION_ID.fullmatch(citation_id)
    if match is None:
        return False
    legacy_authority = _PDF_AUTHORITIES.get(match.group(1))
    return (
        legacy_authority is not None
        and 1 <= int(match.group(2)) <= 3
        and page_number == int(match.group(2))
        and movie_id == legacy_authority[0]
        and source_filename == legacy_authority[1]
    )


def _assert_ordered_citation_card_equality(
    answer: Mapping[str, Any],
    *,
    citation_authorities: Mapping[str, _CitationAuthority] | None = None,
) -> None:
    citations = answer.get("citations")
    movies = answer.get("movies")
    answer_markdown = answer.get("answer_markdown")
    if (
        not isinstance(citations, list)
        or not isinstance(movies, list)
        or not isinstance(answer_markdown, str)
        or not citations
        or not movies
    ):
        raise SmokeFailure("ordered citation/card evidence is missing")
    citation_movie_ids: list[str] = []
    citation_ids: list[str] = []
    titles_by_movie: dict[str, str] = {}
    card_movie_ids: list[str] = []
    for citation in citations:
        if not isinstance(citation, dict) or set(citation) != _CITATION_KEYS:
            raise SmokeFailure("grounded answer contract failed")
        citation_id = citation.get("citation_id")
        movie_id = citation.get("movie_id")
        movie_title = citation.get("movie_title")
        source_kind = citation.get("source_kind")
        page_number = citation.get("page_number")
        source_filename = citation.get("source_filename")
        excerpt = citation.get("excerpt")
        if (
            not isinstance(citation_id, str)
            or not citation_id
            or not _valid_movie_id(movie_id)
            or not isinstance(movie_title, str)
            or not movie_title
            or not isinstance(excerpt, str)
            or not excerpt
            or source_kind not in {"movie_metadata", "pdf_page"}
            or (
                citation_authorities is not None
                and citation_authorities.get(citation_id)
                != _CitationAuthority(
                    movie_id,
                    cast(Literal["movie_metadata", "pdf_page"], source_kind),
                    cast(int | None, page_number),
                    cast(str | None, source_filename),
                )
            )
            or (
                source_kind == "movie_metadata"
                and (
                    citation_id != f"metadata:{movie_id}"
                    or page_number is not None
                    or source_filename is not None
                )
            )
            or (
                source_kind == "pdf_page"
                and not _valid_pdf_citation_binding(
                    citation_id,
                    movie_id,
                    page_number,
                    source_filename,
                    citation_authorities,
                )
            )
        ):
            raise SmokeFailure("grounded answer contract failed")
        if movie_id in titles_by_movie and titles_by_movie[movie_id] != movie_title:
            raise SmokeFailure("grounded answer contract failed")
        titles_by_movie[movie_id] = movie_title
        citation_ids.append(citation_id)
        citation_movie_ids.append(movie_id)
    for movie in movies:
        if not isinstance(movie, dict):
            raise SmokeFailure("grounded answer contract failed")
        movie_id = movie.get("movie_id")
        chinese_title = movie.get("chinese_title")
        if (
            not _valid_movie_id(movie_id)
            or not isinstance(chinese_title, str)
            or not chinese_title
            or titles_by_movie.get(movie_id) != chinese_title
        ):
            raise SmokeFailure("grounded answer contract failed")
        card_movie_ids.append(movie_id)
    inline_ids = _INLINE_CITATION.findall(answer_markdown)
    if (
        len(set(citation_ids)) != len(citation_ids)
        or list(dict.fromkeys(inline_ids)) != citation_ids
    ):
        raise SmokeFailure("grounded answer contract failed")
    if (
        len(set(card_movie_ids)) != len(card_movie_ids)
        or list(dict.fromkeys(citation_movie_ids)) != card_movie_ids
    ):
        raise SmokeFailure("ordered citation/card equality failed")


def _assert_returned_posters(
    base_url: str,
    answer: Mapping[str, Any],
    timeout: float,
    session_cookie: str,
    *,
    poster_expectations: Mapping[str, _PosterExpectation] | None = None,
) -> None:
    movies = answer.get("movies")
    if not isinstance(movies, list):  # guarded by ordered equality; retained fail-closed
        raise SmokeFailure("returned movie cards are invalid")
    poster_paths: list[str] = []
    expected_posters: list[tuple[_PosterExpectation | None, bool]] = []
    for movie in movies:
        if not isinstance(movie, dict):
            raise SmokeFailure("returned movie card is invalid")
        movie_id = movie.get("movie_id")
        poster_url = movie.get("poster_url")
        if not _valid_movie_id(movie_id):
            raise SmokeFailure("returned movie card is invalid")
        expected_url = f"/api/posters/{urllib.parse.quote(movie_id, safe='')}"
        expected = (
            poster_expectations.get(movie_id)
            if poster_expectations is not None
            else None
        )
        if "poster_url" not in movie:
            raise SmokeFailure("returned poster URL is invalid")
        if poster_expectations is not None and expected is None:
            raise SmokeFailure("returned poster authority is missing")
        expected_available = expected.available if expected is not None else poster_url is not None
        if poster_url != (expected_url if expected_available else None):
            raise SmokeFailure("returned poster URL is invalid")
        poster_paths.append(expected_url)
        expected_posters.append((expected, expected_available))
    results = _parallel_mobile_poster_requests(
        base_url, poster_paths, timeout, session_cookie
    )
    for (expected, available), result in zip(expected_posters, results, strict=True):
        if available:
            _assert_webp_response(
                result,
                expected_rights_status=(
                    expected.rights_status if expected is not None else "unknown"
                ),
            )
            if expected is not None and (
                len(result.body) != expected.size
                or hashlib.sha256(result.body).hexdigest() != expected.sha256
                or result.headers.get("content-type", "").split(";", 1)[0].strip().lower()
                != expected.mime_type
                or result.headers.get("x-rag-rights-status", "")
                != expected.rights_status
            ):
                raise SmokeFailure("returned poster proxy contract failed")
        elif result.status != 404:
            raise SmokeFailure("null poster endpoint was not absent")


def _parallel_mobile_poster_requests(
    base_url: str,
    poster_paths: list[str],
    timeout: float,
    session_cookie: str,
) -> list[HttpResult]:
    if not poster_paths:
        return []

    def fetch(path: str) -> HttpResult:
        opener = _no_redirect_opener()
        headers = {"User-Agent": _IPHONE_USER_AGENT}
        if session_cookie:
            headers["Cookie"] = session_cookie
        result = _request(
            opener,
            base_url,
            "GET",
            path,
            headers=headers,
            timeout=timeout,
            max_body_bytes=_POSTER_RESPONSE_BODY_LIMIT,
        )
        if 300 <= result.status < 400:
            raise SmokeFailure("poster redirect is forbidden")
        return result

    with ThreadPoolExecutor(max_workers=min(5, len(poster_paths))) as executor:
        return list(executor.map(fetch, poster_paths))


def _assert_null_poster_card(answer: Mapping[str, Any], movie_id: str) -> None:
    movies = answer.get("movies")
    if (
        not isinstance(movies, list)
        or len(movies) != 1
        or not isinstance(movies[0], dict)
        or movies[0].get("movie_id") != movie_id
        or movies[0].get("poster_url") is not None
    ):
        raise SmokeFailure("known unavailable poster card is invalid")


def _movie_cards(answer: Mapping[str, Any]) -> list[dict[str, Any]]:
    movies = answer.get("movies")
    if not isinstance(movies, list) or not movies:
        raise SmokeFailure("recommendation movie cards are missing")
    cards: list[dict[str, Any]] = []
    movie_ids: set[str] = set()
    for movie in movies:
        if not isinstance(movie, dict):
            raise SmokeFailure("recommendation movie card is invalid")
        movie_id = movie.get("movie_id")
        genre = movie.get("genre")
        tier = movie.get("tier")
        poster_url = movie.get("poster_url")
        if (
            not _valid_movie_id(movie_id)
            or movie_id in movie_ids
            or not isinstance(genre, str)
            or not genre
            or tier not in {"S", "A", "B"}
            or (
                poster_url is not None
                and poster_url != f"/api/posters/{urllib.parse.quote(movie_id, safe='')}"
            )
        ):
            raise SmokeFailure("recommendation movie card is invalid")
        movie_ids.add(movie_id)
        cards.append(movie)
    citations = answer.get("citations")
    if not isinstance(citations, list):
        raise SmokeFailure("recommendation citation/card evidence is missing")
    citation_movie_ids = [
        citation.get("movie_id") if isinstance(citation, dict) else None
        for citation in citations
    ]
    if citation_movie_ids != [card["movie_id"] for card in cards]:
        raise SmokeFailure("recommendation citation/card equality failed")
    return cards


def _assert_structured_recommendation(
    answer: Mapping[str, Any],
) -> list[dict[str, Any]]:
    cards = _movie_cards(answer)
    citations = answer.get("citations")
    answer_markdown = answer.get("answer_markdown")
    if not isinstance(citations, list) or not isinstance(answer_markdown, str):
        raise SmokeFailure("structured recommendation answer contract failed")
    lines = [line.strip() for line in answer_markdown.splitlines() if line.strip()]
    if len(lines) != len(cards) or len(citations) != len(cards):
        raise SmokeFailure("structured recommendation answer contract failed")
    for line, citation, card in zip(lines, citations, cards, strict=True):
        if not isinstance(citation, dict):  # guarded by grounded validation
            raise SmokeFailure("structured recommendation answer contract failed")
        movie_id = card["movie_id"]
        chinese_title = card.get("chinese_title")
        expected_citation = f"metadata:{movie_id}"
        if (
            not isinstance(chinese_title, str)
            or not chinese_title
            or f"《{chinese_title}》" not in line
            or _INLINE_CITATION.findall(line) != [expected_citation]
            or citation.get("citation_id") != expected_citation
            or citation.get("movie_id") != movie_id
            or citation.get("movie_title") != chinese_title
            or citation.get("source_kind") != "movie_metadata"
            or citation.get("page_number") is not None
            or citation.get("source_filename") is not None
        ):
            raise SmokeFailure("structured recommendation answer contract failed")
    return cards


def _assert_person_recommendation(
    answer: Mapping[str, Any],
    *,
    expected_person: str,
    allowed_roles: tuple[str, ...],
    required_genres: frozenset[str],
    expected_count: int,
) -> None:
    cards = _assert_structured_recommendation(answer)
    if len(cards) != expected_count:
        raise SmokeFailure("person recommendation contract failed")
    for card in cards:
        if (
            not any(
                expected_person in _split_card_values(card.get(role))
                for role in allowed_roles
            )
            or not required_genres.issubset(_split_card_values(card.get("genre")))
        ):
            raise SmokeFailure("person recommendation contract failed")


def _assert_known_metadata_answer(answer: Mapping[str, Any]) -> None:
    answer_markdown = answer.get("answer_markdown")
    movies = answer.get("movies")
    answer_prose = (
        _INLINE_CITATION.sub("", answer_markdown)
        if isinstance(answer_markdown, str)
        else ""
    )
    if (
        not isinstance(answer_markdown, str)
        or re.search(r"(?<![0-9])1978(?![0-9])", answer_prose) is None
        or "袁和平" not in answer_prose
        or not isinstance(movies, list)
        or len(movies) != 1
        or not isinstance(movies[0], dict)
        or movies[0].get("movie_id") != "1978_ZQ_001"
        or movies[0].get("release_date") != "1978-10-05"
        or movies[0].get("director") != "袁和平"
    ):
        raise SmokeFailure("known metadata answer contract failed")


def _assert_citation(
    answer: Mapping[str, Any], citation_id: str, movie_id: str, source_kind: str
) -> None:
    citations = answer.get("citations")
    if not isinstance(citations, list):
        raise SmokeFailure("chat citations are missing")
    match = next(
        (
            item
            for item in citations
            if isinstance(item, dict) and item.get("citation_id") == citation_id
        ),
        None,
    )
    if (
        match is None
        or match.get("movie_id") != movie_id
        or match.get("source_kind") != source_kind
        or (source_kind == "movie_metadata" and match.get("page_number") is not None)
    ):
        raise SmokeFailure("expected citation was not returned")


def _assert_citation_prefix(answer: Mapping[str, Any], prefix: str, movie_id: str) -> None:
    citations = answer.get("citations")
    if not isinstance(citations, list):
        raise SmokeFailure("chat citations are missing")
    matches = [
        item
        for item in citations
        if isinstance(item, dict)
        and isinstance(item.get("citation_id"), str)
        and item["citation_id"].startswith(prefix)
        and item.get("movie_id") == movie_id
        and item.get("source_kind") == "pdf_page"
        and isinstance(item.get("page_number"), int)
        and not isinstance(item.get("page_number"), bool)
        and item["page_number"] > 0
    ]
    if not matches:
        raise SmokeFailure("expected page-addressed PDF citation was not returned")


def _assert_webp_response(
    result: HttpResult, *, expected_rights_status: str
) -> None:
    content_type = result.headers.get("content-type", "").split(";", 1)[0].strip().lower()
    cache_directives = {
        directive.strip().lower()
        for directive in result.headers.get("cache-control", "").split(",")
        if directive.strip()
    }
    rights_status = result.headers.get("x-rag-rights-status", "")
    if (
        expected_rights_status not in {"restricted", "unknown"}
        or result.status != 200
        or content_type != "image/webp"
        or not 12 <= len(result.body) <= _POSTER_RESPONSE_BODY_LIMIT
        or result.body[:4] != b"RIFF"
        or result.body[8:12] != b"WEBP"
        or cache_directives != {"private", "no-store"}
        or rights_status != expected_rights_status
    ):
        raise SmokeFailure("returned poster proxy contract failed")


def _assert_expected_posters(
    base_url: str,
    movie_ids: tuple[str, ...],
    timeout: float,
    session_cookie: str,
    *,
    poster_expectations: Mapping[str, _PosterExpectation] | None = None,
) -> None:
    if any(not _valid_movie_id(movie_id) for movie_id in movie_ids):
        raise SmokeFailure("poster expectation movie ID is invalid")
    paths = [
        f"/api/posters/{urllib.parse.quote(movie_id, safe='')}"
        for movie_id in movie_ids
    ]
    results = _parallel_mobile_poster_requests(
        base_url, paths, timeout, session_cookie
    )
    for movie_id, result in zip(movie_ids, results, strict=True):
        expected = (
            poster_expectations.get(movie_id)
            if poster_expectations is not None
            else _EXPECTED_POSTERS.get(movie_id)
        )
        if expected is None:
            raise SmokeFailure("poster expectation is missing")
        if isinstance(expected, _PosterExpectation) and not expected.available:
            raise SmokeFailure("poster expectation is missing")
        rights_status = result.headers.get("x-rag-rights-status", "")
        expected_size = expected.size if isinstance(expected, _PosterExpectation) else expected["size"]
        expected_sha256 = (
            expected.sha256 if isinstance(expected, _PosterExpectation) else expected["sha256"]
        )
        expected_rights = (
            expected.rights_status
            if isinstance(expected, _PosterExpectation)
            else expected["rights_status"]
        )
        if not isinstance(expected_rights, str):
            raise SmokeFailure("poster rights expectation is invalid")
        _assert_webp_response(
            result, expected_rights_status=expected_rights
        )
        if (
            len(result.body) != expected_size
            or hashlib.sha256(result.body).hexdigest() != expected_sha256
            or rights_status != expected_rights
        ):
            raise SmokeFailure("poster proxy contract failed")


def _validated_base_url(raw: str) -> str:
    parsed = urllib.parse.urlsplit(raw.strip())
    if (
        parsed.scheme not in {"http", "https"}
        or not parsed.hostname
        or parsed.username is not None
        or parsed.password is not None
        or parsed.query
        or parsed.fragment
        or parsed.path not in {"", "/"}
    ):
        raise SmokeFailure(
            "base URL must be an origin without credentials, path, query, or fragment"
        )
    if parsed.scheme != "https" and parsed.hostname not in {"localhost", "127.0.0.1", "::1"}:
        raise SmokeFailure("non-local live smoke requires HTTPS")
    return raw.strip().rstrip("/")


def _empty_checks(*, public_access: bool = False) -> dict[str, bool | int]:
    checks: dict[str, bool | int] = {
        "comedy_exact_three": False,
        "corpus_wide_recommendation": False,
        "deep_aces_go_places": False,
        "deep_document_cases": 0,
        "deep_drunken_master": False,
        "followup_inherits_comedy_and_excludes_prior": False,
        "general_relevance_movie_cases": 0,
        "general_relevance_ood_cases": 0,
        "metadata_answer": False,
        "mixed_script_multi_person_rejected": False,
        "known_unavailable_poster": False,
        "known_unavailable_poster_card": False,
        "poster_aces_go_places": False,
        "poster_drunken_master": False,
        "person_and_genre_exact_three": False,
        "person_action_comedy_exact_five": False,
        "person_followup_excludes_prior": False,
        "person_history_reset_action_comedy_exact_five": False,
        "person_continuation_vocabulary_resets_history": False,
        "person_simplified_exact_three": False,
        "person_simplified_default_five": False,
        "person_traditional_exact_three": False,
        "person_traditional_default_five": False,
        "release_title_deep_query": False,
        "returned_posters": False,
        "out_of_domain_rejected": False,
        "public_health": False,
        "s_tier_only_excludes_b_tier": False,
        "title_routing_regressions": False,
    }
    if public_access:
        checks.update(
            {
                "legacy_session_removed": False,
                "public_chat": False,
                "public_config": False,
                "public_poster": False,
            }
        )
    else:
        checks.update(
            {
                "authenticated_config": False,
                "incorrect_access_code_rejected": False,
                "login": False,
                "logout": False,
                "post_logout_chat_rejected": False,
                "post_logout_config_rejected": False,
                "tampered_session_cookie_rejected": False,
                "unauthenticated_chat_rejected": False,
                "unauthenticated_poster_rejected": False,
            }
        )
    return checks


def _encoded_report(report: Mapping[str, object]) -> str:
    return json.dumps(report, ensure_ascii=False, sort_keys=True, separators=(",", ":"))


def _atomic_write(path: Path, content: str) -> None:
    destination = path.resolve()
    destination.parent.mkdir(parents=True, exist_ok=True)
    descriptor, raw_temporary = tempfile.mkstemp(
        prefix=f".{destination.name}.", suffix=".tmp", dir=destination.parent
    )
    temporary = Path(raw_temporary)
    try:
        with os.fdopen(descriptor, "w", encoding="utf-8", newline="\n") as stream:
            stream.write(content)
            stream.flush()
            os.fsync(stream.fileno())
        os.replace(temporary, destination)
    finally:
        temporary.unlink(missing_ok=True)


if __name__ == "__main__":
    sys.exit(main())
