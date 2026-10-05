import pytest
from challenge_eval import OutputValidationError
from final_paired_eval import attempt


def test_successful_api_output_survives_classifier_failure():
    answer = {"answer_markdown": "actual", "movies": [], "citations": []}

    def broken(*args):
        raise ValueError("private detail")

    result = attempt({"id": "x"}, [], {}, lambda: (answer, {}), broken)
    assert result["response"] == answer
    assert result["score"]["outcome"] == "classifier_error"
    assert result["error_type"] == "ValueError"
    assert "private detail" not in str(result)


def test_invalid_generation_preserves_actual_generation_metadata():
    metadata = {"raw_generation": "actual invalid output", "retrieved_ids": ["x"]}

    def invalid():
        raise OutputValidationError(metadata)

    result = attempt({}, [], {}, invalid, lambda *args: None)
    assert result["response"] is None
    assert result["metadata"] == metadata
    assert result["score"]["reason"] == "generation_output_validation_failure"


def test_transport_failure_records_class_only_without_retry():
    calls = []

    def failed():
        calls.append(1)
        raise RuntimeError("secret provider body")

    result = attempt({}, [], {}, failed, lambda *args: None)
    assert len(calls) == 1
    assert result["score"]["outcome"] == "transport_error"
    assert "secret provider body" not in str(result)


def test_success_retains_response_and_metadata():
    result = attempt(
        {},
        [],
        {},
        lambda: ({"answer_markdown": "ok"}, {"usage": 2}),
        lambda *args: {"pass": True, "outcome": "correct_answer"},
    )
    assert result["score"]["pass"]
    assert result["metadata"] == {"usage": 2}


def test_runner_preflight_order_isolated_history_and_source_drift(monkeypatch, tmp_path):
    import json
    import sys
    from pathlib import Path

    import final_paired_eval as evaluator

    catalog = tmp_path / "catalog.jsonl"
    catalog.write_text(
        "\n".join(json.dumps({"movie_id": str(n)}) for n in range(4659)), encoding="utf8"
    )
    output = tmp_path / "run"
    revision = "hk-movie-rag-demo-00001-qfix-0a81de7"
    image = "sha256:" + "a" * 64
    config = evaluator.expected_pins(revision, image)
    argv = [
        "final_paired_eval",
        "--catalog",
        str(catalog),
        "--output",
        str(output),
        "--revision",
        revision,
        "--image-digest",
        image,
    ]
    monkeypatch.setattr(sys, "argv", argv)
    old_digest = evaluator.original.digest
    calls = []
    constructors = []
    drift = [False]

    def digest(path):
        if Path(path) == catalog:
            return evaluator.original.CATALOG_SHA
        if drift[0] and len(calls) == 120 and Path(path) == Path(evaluator.__file__):
            return "changed"
        return old_digest(path)

    def answer(arm, question, history):
        assert all(h["answer"].startswith(arm) for h in history)
        assert all(set(h) == {"question", "answer", "movie_ids"} for h in history)
        calls.append((arm, question, history))
        return {
            "answer_markdown": arm + " actual response",
            "movies": [{"movie_id": arm}],
            "citations": [],
        }

    def api(url, payload=None):
        if url.endswith("/api/config"):
            return config
        if url.endswith("/health"):
            return {"status": "ok"}
        assert set(payload) == {"question", "history"}
        return answer("system", payload["question"], payload["history"])

    class FakeBaseline:
        def __init__(self, *args):
            constructors.append(1)

        def call(self, question, history):
            return answer("baseline", question, history), {}

    def classifier(*args):
        return {"pass": True, "outcome": "correct_answer"}

    monkeypatch.setattr(evaluator.original, "digest", digest)
    monkeypatch.setattr(evaluator.original, "get_json", api)
    monkeypatch.setattr(evaluator.original, "Baseline", FakeBaseline)
    monkeypatch.setattr(
        evaluator,
        "attempt",
        lambda case, history, cat, call: {"response": call()[0], "score": classifier()},
    )
    evaluator.main()
    assert calls == constructors == [] and not output.exists()
    argv.append("--execute")
    evaluator.main()
    assert len(calls) == 120
    assert [x[0] for x in calls[:4]] == ["system", "baseline", "baseline", "system"]
    assert sum(bool(x[2]) for x in calls) == 20
    with pytest.raises(FileExistsError):
        evaluator.main()
    assert len(calls) == 120
    argv[argv.index(str(output))] = str(tmp_path / "drift-run")
    calls.clear()
    drift[0] = True
    with pytest.raises(SystemExit) as exc:
        evaluator.main()
    assert exc.value.code == 2
    assert not json.loads((tmp_path / "drift-run/summary.json").read_text(encoding="utf8"))[
        "frozen_inputs_unchanged"
    ]
