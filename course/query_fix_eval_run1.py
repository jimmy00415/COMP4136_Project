"""Explicitly pinned, one-attempt development regression of the repaired service.

This reuses the original 60 known questions, not an unseen holdout or a new
paired baseline study. Historical cases, evaluators and results stay unchanged.
Without --execute it performs only config/health GETs and local input checks.
"""

from __future__ import annotations

import argparse
import json
import time
from collections import defaultdict
from pathlib import Path

from challenge_eval import CATALOG_SHA, build_history, classify, digest, get_json, utc
from run_simple_eval import PINS

CASES_SHA = "2ab256dc99112b6864f9c41f51f77d216ebff8aeccfa6458c30107f54787e7f5"


def expected_pins(revision: str, image_digest: str) -> dict:
    import re

    if not re.fullmatch(r"hk-movie-rag-demo-[a-z0-9-]+", revision):
        raise ValueError("revision outside the existing service")
    if not re.fullmatch(r"sha256:[a-f0-9]{64}", image_digest):
        raise ValueError("immutable image digest required")
    return PINS | {"serving_revision": revision, "image_digest": image_digest}


def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--service", required=True)
    parser.add_argument("--revision", required=True)
    parser.add_argument("--image-digest", required=True)
    parser.add_argument("--catalog", type=Path, required=True)
    parser.add_argument("--output", type=Path, required=True)
    parser.add_argument("--execute", action="store_true")
    args = parser.parse_args()
    if not args.service.startswith("https://") or not args.service.endswith(".run.app"):
        raise ValueError("Cloud Run HTTPS endpoint required")
    cases_path = Path(__file__).with_name("challenge_cases.jsonl")
    if digest(args.catalog) != CATALOG_SHA or digest(cases_path) != CASES_SHA:
        raise ValueError("original catalog or questions changed")
    catalog = {
        row["movie_id"]: row
        for row in map(json.loads, args.catalog.read_text(encoding="utf8").splitlines())
    }
    cases = list(map(json.loads, cases_path.read_text(encoding="utf8").splitlines()))
    if len(catalog) != 4659 or len(cases) != 60:
        raise ValueError("unexpected input count")
    pins = expected_pins(args.revision, args.image_digest)
    before = get_json(args.service + "/api/config")
    if any(before.get(key) != value for key, value in pins.items()):
        raise RuntimeError("candidate config drift")
    if get_json(args.service + "/health").get("status") != "ok":
        raise RuntimeError("candidate unhealthy")
    if not args.execute:
        print(json.dumps({"preflight": "passed", "chat_calls": 0, "turns": 60}))
        return
    args.output.mkdir(parents=True, exist_ok=False)
    freeze = {
        "at": utc(),
        "study": "known-case development regression; system only",
        "unseen_holdout": False,
        "baseline_rerun": False,
        "catalog_sha256": digest(args.catalog),
        "cases_sha256": digest(cases_path),
        "runner_sha256": digest(__file__),
        "original_classifier_sha256": digest(Path(__file__).with_name("challenge_eval.py")),
        "config_before": before,
        "planned_calls": 60,
        "attempts_per_case": 1,
        "human_audits": "pending",
        "submission_ready": False,
    }
    (args.output / "freeze.json").write_text(json.dumps(freeze, indent=2), encoding="utf8")
    (args.output / "cases.jsonl").write_bytes(cases_path.read_bytes())
    histories = defaultdict(list)
    records = []
    for index, case in enumerate(cases):
        history = build_history(histories[case["session"]]) if case.get("session") else []
        record = {
            "case_id": case["id"],
            "family": case["family"],
            "arm": "system",
            "question": case["question"],
            "history": history,
            "started_at": utc(),
        }
        start = time.monotonic()
        try:
            response = get_json(
                args.service + "/api/chat", {"question": case["question"], "history": history}
            )
            record.update(response=response, score=classify(case, response, catalog, history))
        except Exception as exc:  # noqa: BLE001 - record class only, no provider body or secrets
            record.update(
                response=None,
                error_type=type(exc).__name__,
                score={"pass": False, "outcome": "transport_error"},
            )
        record.update(ended_at=utc(), latency_s=time.monotonic() - start)
        with (args.output / "responses.jsonl").open("a", encoding="utf8") as handle:
            handle.write(json.dumps(record, ensure_ascii=False) + "\n")
        records.append(record)
        if case.get("session"):
            histories[case["session"]].append(record)
        print(f"{index + 1:02d}/60 {case['id']}: {record['score']['outcome']}", flush=True)
    after = get_json(args.service + "/api/config")
    stable = all(after.get(key) == value for key, value in pins.items())
    summary = {
        "at": utc(),
        "attempts": len(records),
        "responses": sum(r["response"] is not None for r in records),
        "mechanical_passes": sum(r["score"]["pass"] for r in records),
        "config_stable": stable,
        "responses_sha256": digest(args.output / "responses.jsonl"),
        "inputs_unchanged": digest(args.catalog) == CATALOG_SHA and digest(cases_path) == CASES_SHA,
        "runner_unchanged": digest(__file__) == freeze["runner_sha256"],
        "human_audits": "pending",
        "submission_ready": False,
        "config_after": after,
    }
    (args.output / "summary.json").write_text(json.dumps(summary, indent=2), encoding="utf8")
    print(
        json.dumps(
            {key: value for key, value in summary.items() if key != "config_after"}, indent=2
        )
    )


if __name__ == "__main__":
    main()
