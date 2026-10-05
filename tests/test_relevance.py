"""Immutable relevance policy, golden cases, and redacted evaluator contracts."""

from __future__ import annotations

import hashlib
import json
from importlib.resources import files
from pathlib import Path

import pytest

from hk_movie_rag.relevance import (
    RelevancePolicyError,
    has_static_movie_domain_signal,
    load_deep_answer_cases,
    load_general_relevance_policy,
    validate_general_relevance_policy,
)
from hk_movie_rag.relevance_eval import evaluate_general_relevance

GOLDEN_PATH = Path("evals/general_relevance_golden.jsonl")
REPORT_PATH = Path("evals/general_relevance_report.json")
R2_RELEASE_ID = "v1.2-demo-r2"
R2_POLICY_RESOURCE = "general_relevance_v1_2_demo_r2.json"
R2_DEEP_CASES_RESOURCE = "deep_document_cases_v1_2_demo_r2.jsonl"
R3_RELEASE_ID = "v1.2-demo-r3"
R3_POLICY_RESOURCE = "general_relevance_v1_2_demo_r3.json"
R3_DEEP_CASES_RESOURCE = "deep_document_cases_v1_2_demo_r3.jsonl"

EXPECTED_QUESTIONS = (
    "香港動作喜劇有什麼代表特色？",
    "七十年代功夫片常見什麼元素？",
    "八十年代警匪片有哪些代表？",
    "九十年代香港喜劇電影有什麼特點？",
    "香港電影裡的師徒關係如何表現？",
    "港產片如何呈現都市空間？",
    "哪些電影適合喜歡武打喜劇的觀眾？",
    "想看節奏明快的香港動作片",
    "有什麼關於兄弟情的香港電影？",
    "香港犯罪電影中常見哪些角色？",
    "有沒有女性導演的香港電影？",
    "香港電影裡的殭屍題材",
    "港片中的賭片有哪些？",
    "經典武俠電影有哪些？",
    "香港電影如何表現身份認同？",
    "今天天氣怎麼樣？",
    "Python 怎麼排序列表？",
    "幫我寫一封請假郵件。",
    "比特幣今天多少錢？",
    "法國首都是哪裡？",
    "番茄炒蛋怎麼做？",
    "量子力學是什麼？",
    "這週 NBA 比賽結果。",
    "推薦一本好書。",
    "公司財報應該怎麼看？",
    "把這段中文改寫成英文。",
    "頭痛應該吃什麼藥？",
    "你好。",
    "幫我制定健身計劃。",
    "如何學好粵語？",
    "六七十年代的香港武俠片有什麼代表作品？",
    "香港電影中有哪些經典愛情片？",
    "想找一部由女演員擔綱主角的劇情片。",
    "香港恐怖電影有哪些類型？",
    "哪幾部港片適合全家觀看？",
    "推薦一款好玩的電子遊戲。",
    "最近哪隻股票值得買？",
    "北京有什麼好吃的餐廳？",
    "幫我總結這份合同。",
    "寫一首關於香港的詩。",
)


def _golden_cases() -> list[dict[str, object]]:
    return [json.loads(line) for line in GOLDEN_PATH.read_text(encoding="utf-8").splitlines()]


def test_general_relevance_golden_is_exact_deterministic_40_case_contract() -> None:
    """Breaks if calibration/holdout membership or canonical JSONL bytes drift silently."""
    raw = GOLDEN_PATH.read_bytes()
    assert raw.endswith(b"\n")
    cases = _golden_cases()

    assert len(cases) == 40
    assert len({str(case["case_id"]) for case in cases}) == 40
    assert tuple(case["question"] for case in cases) == EXPECTED_QUESTIONS
    assert [
        (case["split"], case["expected_domain"])
        for case in cases
    ].count(("calibration", "movie")) == 15
    assert [
        (case["split"], case["expected_domain"])
        for case in cases
    ].count(("calibration", "ood")) == 15
    assert [
        (case["split"], case["expected_domain"])
        for case in cases
    ].count(("holdout", "movie")) == 5
    assert [
        (case["split"], case["expected_domain"])
        for case in cases
    ].count(("holdout", "ood")) == 5
    for line, case in zip(raw.decode("utf-8").splitlines(), cases, strict=True):
        assert line == json.dumps(
            case, ensure_ascii=False, sort_keys=True, separators=(",", ":")
        )


def test_packaged_policy_is_bound_to_release_model_metric_cutoff_and_golden_digest() -> None:
    """Breaks if runtime policy bytes or query identity can drift from the evaluated release."""
    policy = load_general_relevance_policy()

    assert policy.policy_id == "general-relevance/v1"
    assert policy.release_id == "v1.2-demo"
    assert policy.embedding_model == "gemini-embedding-2"
    assert policy.embedding_dimension == 768
    assert policy.distance_metric == "pgvector_cosine_distance"
    assert policy.distance_cutoff == 0.36
    assert len(policy.golden_sha256) == 64
    assert len(policy.artifact_sha256) == 64
    assert policy.golden_sha256 == policy.sha256_bytes(GOLDEN_PATH.read_bytes())


def test_r2_policy_requires_exact_release_digest_and_binds_the_unchanged_golden() -> None:
    """Breaks if R2 can fall back to v1 or select an unreviewed policy by release alone."""
    raw = files("hk_movie_rag.policies").joinpath(R2_POLICY_RESOURCE).read_bytes()
    digest = hashlib.sha256(raw).hexdigest()

    policy = load_general_relevance_policy(R2_RELEASE_ID, digest)

    assert policy.release_id == R2_RELEASE_ID
    assert policy.golden_sha256 == hashlib.sha256(GOLDEN_PATH.read_bytes()).hexdigest()
    assert policy.deep_cases_sha256 == hashlib.sha256(
        files("hk_movie_rag.policies").joinpath(R2_DEEP_CASES_RESOURCE).read_bytes()
    ).hexdigest()
    assert policy.artifact_sha256 == digest
    with pytest.raises(RelevancePolicyError):
        load_general_relevance_policy(R2_RELEASE_ID, "0" * 64)
    with pytest.raises(RelevancePolicyError):
        load_general_relevance_policy("v1.2-unknown", digest)
    with pytest.raises(RelevancePolicyError):
        load_general_relevance_policy(R2_RELEASE_ID)


def test_r3_policy_binds_new_deep_cases_without_changing_generic_golden() -> None:
    """Breaks if the five-document release borrows R2 answer gates or loses new citations."""
    raw = files("hk_movie_rag.policies").joinpath(R3_POLICY_RESOURCE).read_bytes()
    digest = hashlib.sha256(raw).hexdigest()

    policy = load_general_relevance_policy(R3_RELEASE_ID, digest)
    cases = load_deep_answer_cases(R3_RELEASE_ID, digest)

    assert digest == "d6db5e118aeea5dc0484f3c591baac666952f78f4d41bfdcdbbb58693f4f6bba"
    assert policy.release_id == R3_RELEASE_ID
    assert policy.golden_sha256 == hashlib.sha256(GOLDEN_PATH.read_bytes()).hexdigest()
    assert policy.deep_cases_sha256 == hashlib.sha256(
        files("hk_movie_rag.policies").joinpath(R3_DEEP_CASES_RESOURCE).read_bytes()
    ).hexdigest()
    by_id = {str(case["case_id"]): case for case in cases}
    assert len(by_id) == 10
    assert by_id["mr-vampire-visual-p1"]["required_citation_ids"] == [
        "pdf:mr-vampire-deep-analysis-v1:p1"
    ]
    assert by_id["shaolin-soccer-fusion-p1"]["required_citation_ids"] == [
        "pdf:shaolin-soccer-deep-analysis-v1:p1"
    ]


def test_r2_deep_cases_are_strict_canonical_and_cover_each_fgbr_answer_authority() -> None:
    """Breaks if a conversational case can omit exact evidence, history, or false-answer gates."""
    policy_raw = files("hk_movie_rag.policies").joinpath(R2_POLICY_RESOURCE).read_bytes()
    policy_digest = hashlib.sha256(policy_raw).hexdigest()
    raw = files("hk_movie_rag.policies").joinpath(R2_DEEP_CASES_RESOURCE).read_bytes()

    cases = load_deep_answer_cases(R2_RELEASE_ID, policy_digest)

    assert raw.endswith(b"\n")
    assert len(cases) == 6
    assert tuple(case["case_id"] for case in cases) == (
        "fgbr-visual-p1",
        "fgbr-sound-followup-p5",
        "fgbr-cast-runtime-metadata",
        "fgbr-runtime-visual-mixed",
        "fgbr-comb-symbolism-p2",
        "fgbr-bounded-broad-analysis",
    )
    assert all(case["expected_movie_ids"] == ["1987_FGBR_001"] for case in cases[:5])
    assert cases[0]["required_citation_ids"] == [
        "pdf:its-a-mad-mad-mad-world-deep-analysis-v1:p1"
    ]
    assert cases[1]["required_citation_ids"] == [
        "pdf:its-a-mad-mad-mad-world-deep-analysis-v1:p5"
    ]
    assert cases[1]["history"] == [
        {
            "answer": "《富貴逼人》的視覺與空間分析集中在第一頁。",
            "movie_ids": ["1987_FGBR_001"],
            "question": "《富貴逼人》的視覺風格與空間設計？",
        }
    ]
    assert cases[2]["required_citation_ids"] == ["metadata:1987_FGBR_001"]
    assert cases[2]["forbidden_source_kinds"] == ["pdf_page"]
    assert cases[2]["required_answer_terms"] == ["董驃", "沈殿霞", "曾志偉", "100"]
    assert "99" in cases[2]["forbidden_answer_terms"]
    assert cases[3]["required_citation_ids"] == [
        "metadata:1987_FGBR_001",
        "pdf:its-a-mad-mad-mad-world-deep-analysis-v1:p1",
    ]
    assert cases[4]["required_citation_ids"] == [
        "pdf:its-a-mad-mad-mad-world-deep-analysis-v1:p2"
    ]
    assert cases[4]["required_any_answer_terms"]
    assert cases[5]["requires_limited_evidence_disclosure"] is True
    for line, case in zip(raw.decode("utf-8").splitlines(), cases, strict=True):
        assert line == json.dumps(
            case, ensure_ascii=False, sort_keys=True, separators=(",", ":")
        )


def test_deep_case_loader_rejects_policy_pair_or_artifact_tamper(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Breaks if deep cases are loaded without the selected policy's exact digest chain."""
    policy_raw = files("hk_movie_rag.policies").joinpath(R2_POLICY_RESOURCE).read_bytes()
    policy_digest = hashlib.sha256(policy_raw).hexdigest()
    monkeypatch.setattr(
        "hk_movie_rag.relevance._read_policy_resource",
        lambda name: (
            b'{"case_id":"tampered"}\n'
            if name == R2_DEEP_CASES_RESOURCE
            else files("hk_movie_rag.policies").joinpath(name).read_bytes()
        ),
    )

    with pytest.raises(RelevancePolicyError):
        load_deep_answer_cases(R2_RELEASE_ID, policy_digest)


@pytest.mark.parametrize(
    ("field", "value"),
    [
        ("release_id", "v1.2-other"),
        ("embedding_model", "other-model"),
        ("embedding_dimension", 1536),
        ("distance_metric", "inner_product"),
        ("distance_cutoff", 0.37),
        ("golden_sha256", "0" * 64),
    ],
)
def test_policy_validation_rejects_every_identity_or_digest_mismatch(
    field: str, value: object
) -> None:
    """Breaks if a same-shaped but unevaluated policy can reach QueryService startup."""
    policy = load_general_relevance_policy()
    payload = dict(policy.payload)
    payload[field] = value

    with pytest.raises(RelevancePolicyError):
        validate_general_relevance_policy(payload, GOLDEN_PATH.read_bytes())


def test_all_40_static_golden_domains_match_without_vertex_or_gcp() -> None:
    """Breaks if deterministic gate vocabulary regresses on calibration or holdout cases."""
    decisions = {
        str(case["case_id"]): has_static_movie_domain_signal(str(case["question"]))
        for case in _golden_cases()
    }

    assert all(
        decisions[str(case["case_id"])] is (case["expected_domain"] == "movie")
        for case in _golden_cases()
    )


@pytest.mark.parametrize(
    "question",
    [
        "推荐一本喜剧小说",
        "推薦一本關於電影的好書",
        "recommend a movie-themed comedy novel",
        "推薦一款香港電影遊戲",
    ],
)
def test_explicit_non_movie_object_overrides_movie_or_genre_vocabulary(
    question: str,
) -> None:
    """Breaks if movie-adjacent wording converts a book or game request into RAG evidence."""
    assert has_static_movie_domain_signal(question) is False


@pytest.mark.parametrize(
    "question",
    [
        "香港電影的動作場面怎麼做？",
        "港片的特效是怎么做的？",
    ],
)
def test_how_movies_are_made_is_not_misclassified_as_a_recipe(question: str) -> None:
    """Breaks if a broad recipe phrase overrides an explicit movie question."""
    assert has_static_movie_domain_signal(question) is True


@pytest.mark.parametrize(
    ("question", "expected"),
    [
        ("香港動作喜劇有哪些？", True),
        ("香港喜劇有哪些？", True),
        ("香港犯罪率是多少？", False),
        ("香港戰爭歷史", False),
        ("香港家庭理財有哪些方法？", False),
        ("推薦香港犯罪片", True),
        ("有没有好的喜剧推荐", True),
        ("推薦家庭理財方法", False),
        ("推薦1995年新聞", False),
        ("推薦S級", False),
    ],
)
def test_hong_kong_genre_words_require_movie_or_controlled_list_semantics(
    question: str, expected: bool
) -> None:
    assert has_static_movie_domain_signal(question) is expected


def test_offline_evaluator_emits_redacted_machine_report_without_question_text() -> None:
    """Breaks if evaluation needs cloud access or leaks raw user questions into its report."""
    report = evaluate_general_relevance(GOLDEN_PATH, live=False)
    serialized = json.dumps(report, ensure_ascii=False, sort_keys=True)

    assert report["status"] == "pass"
    assert report["mode"] == "offline"
    assert report["case_count"] == 40
    assert report["movie_allowed"] == 20
    assert report["ood_rejected"] == 20
    assert [case["case_id"] for case in report["cases"]] == [
        case["case_id"] for case in _golden_cases()
    ]
    assert all("question" not in case for case in report["cases"])
    assert all(question not in serialized for question in EXPECTED_QUESTIONS)


def test_committed_offline_report_is_canonical_and_matches_the_evaluator() -> None:
    """Breaks if the reviewed redacted report drifts from policy code or golden bytes."""
    raw = REPORT_PATH.read_bytes()
    report = json.loads(raw)

    assert raw.endswith(b"\n")
    assert raw == (
        json.dumps(
            report, ensure_ascii=False, sort_keys=True, separators=(",", ":")
        ).encode("utf-8")
        + b"\n"
    )
    assert report == evaluate_general_relevance(GOLDEN_PATH, live=False)


def test_evaluator_requires_explicit_live_flag_before_accepting_observed_distances(
    tmp_path: Path,
) -> None:
    """Breaks if an observations file can silently turn an offline gate into a live claim."""
    observations = tmp_path / "observations.jsonl"
    observations.write_text(
        '{"case_id":"cal-movie-01","distances":[0.31]}\n', encoding="utf-8"
    )

    with pytest.raises(RelevancePolicyError, match="live"):
        evaluate_general_relevance(
            GOLDEN_PATH,
            live=False,
            observations_path=observations,
        )


def test_live_evaluator_treats_ood_calibration_distances_as_diagnostics(
    tmp_path: Path,
) -> None:
    """Breaks if pre-gate OOD calibration is mistaken for runtime retrieval."""
    observations = tmp_path / "observations.jsonl"
    rows = [
        {"case_id": case["case_id"], "distances": [0.31]}
        for case in _golden_cases()
        if case["expected_domain"] == "movie"
    ]
    rows.append({"case_id": "cal-ood-01", "distances": [0.30]})
    observations.write_text(
        "".join(
            json.dumps(row, ensure_ascii=False, sort_keys=True, separators=(",", ":"))
            + "\n"
            for row in rows
        ),
        encoding="utf-8",
    )

    report = evaluate_general_relevance(
        GOLDEN_PATH,
        live=True,
        observations_path=observations,
    )

    ood_case = next(case for case in report["cases"] if case["case_id"] == "cal-ood-01")
    assert report["status"] == "pass"
    assert ood_case["decision"] == "reject"
    assert ood_case["pre_gate_observed_distances"] == [0.3]
