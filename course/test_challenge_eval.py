import json

import pytest
from challenge_eval import BM25, build_history, classify, summarize, validate_generated

CAT = {
    "a": {
        "movie_id": "a",
        "chinese_title": "警察故事",
        "english_title": "Police Story",
        "director": "成龍",
        "cast": "成龍、張曼玉",
        "genre": "動作、喜劇",
        "release_date": "1985-12-14",
        "tier": "S",
    },
    "b": {
        "movie_id": "b",
        "chinese_title": "重慶森林",
        "director": "王家衞",
        "cast": "梁朝偉",
        "genre": "劇情",
        "release_date": "1994-07-14",
        "tier": "B",
    },
    "c": {
        "movie_id": "c",
        "chinese_title": "動作片",
        "director": "吳宇森",
        "cast": "成龍",
        "genre": "動作",
        "release_date": "1999-01-01",
        "tier": "B",
    },
}


def response(answer, ids=()):
    return {
        "answer_markdown": answer,
        "movies": [CAT[i] for i in ids],
        "citations": [
            {
                "citation_id": "metadata:" + i,
                "movie_id": i,
                "source_kind": "metadata",
                "excerpt": json.dumps(CAT[i], ensure_ascii=False),
            }
            for i in ids
        ],
    }


def test_bm25_normalizes_script_and_ranks_title():
    index = BM25(list(CAT.values()))
    assert index.search("《警察故事》的导演是谁？", 2)[0]["movie_id"] == "a"
    assert index.search("重庆森林", 1)[0]["movie_id"] == "b"
    assert index.search("xyznotincatalog", 8) == []


def test_clarification_is_the_correct_action_for_ambiguous_title():
    case = {"expected": "clarify", "candidates": ["a", "b"]}
    result = classify(case, response("請提供年份或電影 ID，您指的是哪一部？"), CAT, [])
    assert result["pass"] and result["outcome"] == "reasonable_clarification"
    assert not classify(case, response("導演是成龍 [metadata:a]", ["a"]), CAT, [])["pass"]


def test_fact_checks_identity_and_evidence_not_only_answer_substring():
    case = {"expected": "fact", "movie_id": "a", "field": "director"}
    assert classify(case, response("导演是成龙 [metadata:a]", ["a"]), CAT, [])["pass"]
    assert not classify(case, response("成龍 [metadata:b]", ["b"]), CAT, [])["pass"]
    assert not classify(case, response("成龍 [metadata:a]"), CAT, [])["pass"]


def test_recommendation_checks_all_filters_and_no_repeats():
    case = {
        "expected": "recommend",
        "count": 2,
        "constraints": {"cast": "成龍", "genre": "動作", "year_min": 1980, "year_max": 1999},
    }
    assert classify(case, response("推薦 [metadata:a] [metadata:c]", ["a", "c"]), CAT, [])["pass"]
    assert not classify(case, response("推薦 [metadata:a] [metadata:b]", ["a", "b"]), CAT, [])[
        "pass"
    ]
    case["exclude_previous"] = True
    assert not classify(
        case, response("推薦 [metadata:a] [metadata:c]", ["a", "c"]), CAT, [{"movie_ids": ["a"]}]
    )["pass"]


def test_refusal_must_not_smuggle_a_budget_answer():
    case = {"expected": "refuse", "unsupported": "budget"}
    assert classify(case, response("資料未提供製作預算，無法回答。"), CAT, [])["pass"]
    assert not classify(case, response("資料不足，但製作預算是100萬元。"), CAT, [])["pass"]


def test_dynamic_ordinal_gold_comes_from_each_arms_own_history():
    case = {"expected": "fact", "history_ordinal": 2, "field": "director"}
    hist = [{"question": "推薦2部", "answer": "", "movie_ids": ["a", "b"]}]
    assert classify(case, response("王家衞 [metadata:b]", ["b"]), CAT, hist)["pass"]
    assert not classify(case, response("成龍 [metadata:a]", ["a"]), CAT, hist)["pass"]
    assert classify(case, response("無法回答"), CAT, [])["reason"] == "missing_history_target"


def test_validate_generation_blocks_unretrieved_ids_and_uncited_claims():
    with pytest.raises(ValueError):
        validate_generated(
            {
                "answer_markdown": "成龍 [metadata:c]",
                "movie_ids": ["c"],
                "citation_ids": ["metadata:c"],
            },
            [CAT["a"]],
        )
    with pytest.raises(ValueError):
        validate_generated(
            {"answer_markdown": "成龍", "movie_ids": ["a"], "citation_ids": ["metadata:a"]},
            [CAT["a"]],
        )
    out = validate_generated(
        {
            "answer_markdown": "成龍 [metadata:a]",
            "movie_ids": ["a"],
            "citation_ids": ["metadata:a"],
        },
        [CAT["a"]],
    )
    assert out["citations"][0]["movie_id"] == "a"


def test_history_is_bounded_and_uses_only_actual_arm_outputs():
    records = [
        {"question": str(i), "response": response("answer", [("a", "b")[i % 2]])} for i in range(6)
    ]
    hist = build_history(records)
    assert len(hist) == 4 and hist[0]["question"] == "2" and hist[-1]["movie_ids"] == ["b"]
    assert "expected" not in hist[0]


def test_summary_retains_failed_calls_and_pairs_by_id():
    records = [
        {
            "case_id": "x",
            "cluster": "x",
            "family": "fact",
            "arm": "system",
            "score": {"pass": True, "outcome": "correct_answer"},
            "latency_s": 1,
        },
        {
            "case_id": "x",
            "cluster": "x",
            "family": "fact",
            "arm": "baseline",
            "score": {"pass": False, "outcome": "transport_error"},
            "latency_s": 2,
        },
    ]
    out = summarize(records, planned_turns=2)
    assert out["arms"]["system"]["passed"] == 1 and out["arms"]["system"]["planned"] == 2
    assert out["arms"]["baseline"]["outcomes"]["transport_error"] == 1
    assert out["paired"]["system_only"] == 1 and out["paired"]["complete_pairs"] == 1


def test_public_api_metadata_kind_and_native_ambiguity_wording():
    resp = response("導演：成龍。[metadata:a]", ["a"])
    resp["citations"][0]["source_kind"] = "movie_metadata"
    assert classify({"expected": "fact", "movie_id": "a", "field": "director"}, resp, CAT, [])[
        "pass"
    ]
    native = response(
        "這個片名在 Release 內對應多部同名電影；請改用以下其中一個 movie ID：`a`、`b`。"
    )
    assert classify({"expected": "clarify"}, native, CAT, [])["pass"]


def test_metadata_card_does_not_turn_evidence_limit_refusal_into_error():
    resp = response("本電影元數據未提供預算，無法回答。[metadata:a]", ["a"])
    assert classify({"expected": "refuse", "unsupported": "budget"}, resp, CAT, [])["pass"]
    assert not classify({"expected": "refuse", "unsupported": "domain"}, resp, CAT, [])["pass"]


def test_invalid_generation_retains_actual_output_for_review():
    from types import SimpleNamespace

    from challenge_eval import Baseline, OutputValidationError

    baseline = Baseline.__new__(Baseline)
    baseline.index = BM25(list(CAT.values()))
    raw = '{"answer_markdown":"成龍 [metadata:not-retrieved]","movie_ids":[],"citation_ids":["metadata:not-retrieved"]}'
    baseline.client = SimpleNamespace(
        models=SimpleNamespace(
            generate_content=lambda **kwargs: SimpleNamespace(text=raw, usage_metadata=None)
        )
    )
    baseline.types = SimpleNamespace(GenerateContentConfig=lambda **kwargs: kwargs)
    with pytest.raises(OutputValidationError) as caught:
        baseline.call("警察故事", [])
    assert caught.value.metadata["raw_generation"] == raw
    assert "a" in caught.value.metadata["retrieved_ids"]


def test_year_inside_a_citation_id_is_not_an_answer():
    mid = "1985_TEST_001"
    row = CAT["a"] | {"movie_id": mid}
    resp = {
        "answer_markdown": f"上映年份見引用。[metadata:{mid}]",
        "movies": [row],
        "citations": [
            {"citation_id": "metadata:" + mid, "movie_id": mid, "source_kind": "movie_metadata"}
        ],
    }
    assert not classify(
        {"expected": "fact", "movie_id": mid, "field": "year"}, resp, {mid: row}, []
    )["pass"]


def test_disclaimer_does_not_hide_an_unsupported_answer():
    for kind, answer in [
        ("domain", "我只能回答電影問題，但以下是排序程式：sorted([3,1,2])"),
        ("budget", "資料未提供預算，但這部電影花了100萬港元。"),
        ("rating", "評分資料不足，但 IMDb 給它8.3分。"),
    ]:
        assert not classify({"expected": "refuse", "unsupported": kind}, response(answer), CAT, [])[
            "pass"
        ]


def test_generic_confirmation_does_not_disambiguate_a_title():
    resp = response("導演是成龍 [metadata:a]。請確認你是否想繼續查詢？", ["a"])
    assert not classify({"expected": "clarify", "candidates": ["a", "b"]}, resp, CAT, [])["pass"]


def test_refusal_cards_are_not_successful_recommendations():
    case = {"expected": "recommend", "count": 2, "constraints": {"genre": "動作"}}
    resp = response("無法推薦任何符合條件的電影 [metadata:a] [metadata:c]", ["a", "c"])
    assert not classify(case, resp, CAT, [])["pass"]


def test_negated_gold_fact_is_an_error():
    resp = response("導演不是成龍，是王家衞。[metadata:a]", ["a"])
    assert not classify({"expected": "fact", "movie_id": "a", "field": "director"}, resp, CAT, [])[
        "pass"
    ]


def test_runtime_manifest_resolves_actual_installed_opencc_distribution():
    from challenge_eval import runtime_versions

    versions = runtime_versions()
    assert versions["opencc_distribution"] == "OpenCC"
    assert versions["opencc_version"]
    assert versions["google_genai_version"]


def test_retrieved_bare_citation_ids_are_canonicalized_without_rewriting_answer():
    obj = {"answer_markdown": "成龍 [metadata:a]", "movie_ids": ["a"], "citation_ids": ["a"]}
    response = validate_generated(obj, [CAT["a"]])
    assert response["answer_markdown"] == obj["answer_markdown"]
    assert response["citations"][0]["citation_id"] == "metadata:a"
    assert obj["citation_ids"] == ["a"]
    with pytest.raises(ValueError):
        validate_generated(obj | {"citation_ids": ["unknown"]}, [CAT["a"]])
