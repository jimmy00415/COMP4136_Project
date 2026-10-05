"""Safety and lineage checks for the source-only development regression."""

import hashlib
import json
import sys

import pytest
import query_fix_eval as evaluator
from run_simple_eval import PINS

REVISION = "hk-movie-rag-demo-00001-qfix-0a81de7"
IMAGE = "sha256:" + "a" * 64


def test_new_revision_pins_leave_historical_pins_unchanged():
    original = dict(PINS)
    pins = evaluator.expected_pins(REVISION, IMAGE)
    assert pins["serving_revision"] == REVISION
    assert pins["image_digest"] == IMAGE
    assert PINS == original
    assert pins["manifest_sha256"] == original["manifest_sha256"]


@pytest.mark.parametrize(
    "revision,image",
    [
        ("other-service-123", IMAGE),
        (REVISION, "latest"),
        (REVISION, "sha256:short"),
    ],
)
def test_candidate_identity_requires_existing_service_and_immutable_digest(revision, image):
    with pytest.raises(ValueError):
        evaluator.expected_pins(revision, image)


@pytest.mark.parametrize("execute", [False, True])
def test_default_is_get_only_and_execute_retains_each_failed_attempt(
    monkeypatch, tmp_path, execute
):
    catalog = tmp_path / "catalog.jsonl"
    catalog.write_text(
        "\n".join(json.dumps({"movie_id": str(n)}) for n in range(4659)), encoding="utf8"
    )
    output = tmp_path / "run"
    original_digest = evaluator.digest
    monkeypatch.setattr(
        evaluator, "digest", lambda p: evaluator.CATALOG_SHA if p == catalog else original_digest(p)
    )
    argv = [
        "query_fix_eval",
        "--service",
        "https://candidate.run.app",
        "--revision",
        REVISION,
        "--image-digest",
        IMAGE,
        "--catalog",
        str(catalog),
        "--output",
        str(output),
    ]
    if execute:
        argv.append("--execute")
    monkeypatch.setattr(sys, "argv", argv)
    calls = []

    def remote(url, payload=None):
        calls.append((url, payload))
        if url.endswith("/api/config"):
            return evaluator.expected_pins(REVISION, IMAGE)
        if url.endswith("/health"):
            return {"status": "ok"}
        assert payload is not None
        assert "expected" not in payload and "constraints" not in payload
        raise TimeoutError("secret provider body must never be saved")

    monkeypatch.setattr(evaluator, "get_json", remote)
    evaluator.main()
    posts = [call for call in calls if call[1] is not None]
    if not execute:
        assert posts == []
        assert not output.exists()
        return
    assert len(posts) == 60
    raw = (output / "responses.jsonl").read_bytes()
    records = list(map(json.loads, raw.decode().splitlines()))
    assert len(records) == 60
    assert all(
        record["error_type"] == "TimeoutError" and not record["score"]["pass"] for record in records
    )
    assert b"secret provider body" not in raw
    summary = json.loads((output / "summary.json").read_text())
    assert summary["attempts"] == 60 and summary["responses"] == 0
    assert summary["responses_sha256"] == hashlib.sha256(raw).hexdigest()
    assert summary["submission_ready"] is False


def test_local_classifier_failure_keeps_successful_response_and_history(monkeypatch, tmp_path):
    catalog = tmp_path / "catalog.jsonl"
    catalog.write_text(
        "\n".join(json.dumps({"movie_id": str(n)}) for n in range(4659)), encoding="utf8"
    )
    original_digest = evaluator.digest
    monkeypatch.setattr(
        evaluator, "digest", lambda p: evaluator.CATALOG_SHA if p == catalog else original_digest(p)
    )
    output = tmp_path / "run"
    monkeypatch.setattr(
        sys,
        "argv",
        [
            "runner",
            "--service",
            "https://candidate.run.app",
            "--revision",
            REVISION,
            "--image-digest",
            IMAGE,
            "--catalog",
            str(catalog),
            "--output",
            str(output),
            "--execute",
        ],
    )
    posts = []

    def remote(url, payload=None):
        if url.endswith("/api/config"):
            return evaluator.expected_pins(REVISION, IMAGE)
        if url.endswith("/health"):
            return {"status": "ok"}
        posts.append(payload)
        return {"answer_markdown": "actual successful response", "movies": [], "citations": []}

    def classifier(*args):
        raise ValueError("private classifier exception body")

    monkeypatch.setattr(evaluator, "get_json", remote)
    monkeypatch.setattr(evaluator, "classify", classifier)
    evaluator.main()
    raw = (output / "responses.jsonl").read_text(encoding="utf8")
    records = list(map(json.loads, raw.splitlines()))
    assert all(r["response"]["answer_markdown"] == "actual successful response" for r in records)
    assert all(
        r["score"]["outcome"] == "classifier_error" and r["error_type"] == "ValueError"
        for r in records
    )
    assert any(payload["history"] for payload in posts)
    assert "private classifier exception body" not in raw
    summary = json.loads((output / "summary.json").read_text())
    assert summary["responses"] == 60 and summary["mechanical_passes"] == 0
