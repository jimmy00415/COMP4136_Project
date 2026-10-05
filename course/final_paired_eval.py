"""Fresh current-revision paired development study; default is GET-only.

No gold enters inference. One request per arm/turn, alternating arm order,
actual independent histories, fresh append-only output, no production changes.
"""

from __future__ import annotations

import argparse
import hashlib
import json
import sys
import time
from collections import Counter, defaultdict
from pathlib import Path

import challenge_eval as original
from query_fix_eval import CASES_SHA, expected_pins


def attempt(case, history, catalog, call, classifier=original.classify):
    """Retain successful provider output even if subsequent scoring fails."""
    try:
        response, metadata = call()
    except original.OutputValidationError as exc:
        return {
            "response": None,
            "metadata": exc.metadata,
            "error_type": type(exc).__name__,
            "score": {
                "pass": False,
                "outcome": "error",
                "reason": "generation_output_validation_failure",
            },
        }
    except Exception as exc:  # noqa: BLE001 - class only; never provider body
        return {
            "response": None,
            "error_type": type(exc).__name__,
            "score": {"pass": False, "outcome": "transport_error", "reason": "call_failure"},
        }
    result = {"response": response, "metadata": metadata}
    try:
        result["score"] = classifier(case, response, catalog, history)
    except Exception as exc:  # noqa: BLE001 - preserve actual output; class only
        result.update(
            error_type=type(exc).__name__,
            score={"pass": False, "outcome": "classifier_error", "reason": "scoring_failure"},
        )
    return result


def main():
    ap = argparse.ArgumentParser(description=__doc__)
    ap.add_argument("--catalog", required=True, type=Path)
    ap.add_argument("--output", required=True, type=Path)
    ap.add_argument("--revision", required=True)
    ap.add_argument("--image-digest", required=True)
    ap.add_argument(
        "--gcloud",
        type=Path,
        default=Path(
            "C:/Users/OWNER/AppData/Local/Google/Cloud SDK/google-cloud-sdk/bin/gcloud.cmd"
        ),
    )
    ap.add_argument("--execute", action="store_true")
    args = ap.parse_args()
    cases_path = Path(__file__).with_name("challenge_cases.jsonl")
    if (
        original.digest(args.catalog) != original.CATALOG_SHA
        or original.digest(cases_path) != CASES_SHA
    ):
        raise ValueError("catalog or cases differ from frozen inputs")
    cases = [json.loads(x) for x in cases_path.read_text(encoding="utf8").splitlines()]
    rows = [json.loads(x) for x in args.catalog.read_text(encoding="utf8").splitlines()]
    catalog = {row["movie_id"]: row for row in rows}
    if (
        len(rows) != len(catalog)
        or len(catalog) != 4659
        or len(cases) != 60
        or len({c["id"] for c in cases}) != 60
    ):
        raise ValueError("unexpected input count or duplicate identity")
    pins = expected_pins(args.revision, args.image_digest)
    before = original.get_json(original.SERVICE + "/api/config")
    if any(before.get(k) != v for k, v in pins.items()):
        raise RuntimeError("current deployment identity drift")
    if original.get_json(original.SERVICE + "/health").get("status") != "ok":
        raise RuntimeError("service unhealthy")
    if not args.execute:
        print(json.dumps({"preflight": "passed", "planned_calls": 120, "actual_calls": 0}))
        return
    args.output.mkdir(parents=True, exist_ok=False)
    freeze = {
        "at": original.utc(),
        "study": "current-revision paired known-case development comparison",
        "unseen_holdout": False,
        "planned_turns": 60,
        "planned_calls": 120,
        "attempts_per_arm_turn": 1,
        "catalog_sha256": original.digest(args.catalog),
        "cases_sha256": original.digest(cases_path),
        "source_sha256": original.digest(__file__),
        "original_module_sha256": original.digest(original.__file__),
        "pin_module_sha256": original.digest(Path(__file__).with_name("query_fix_eval.py")),
        "original_pin_module_sha256": original.digest(
            Path(__file__).with_name("run_simple_eval.py")
        ),
        "prompt_sha256": hashlib.sha256(original.BASELINE_PROMPT.encode("utf8")).hexdigest(),
        "config_before": before,
        "expected_pins": pins,
        "baseline_model": original.MODEL,
        "baseline_project": original.PROJECT,
        "baseline_location": "global",
        "baseline_config": {
            "k1": 1.5,
            "b": 0.75,
            "top_k": 8,
            "max_output_tokens": 1024,
            "sdk_attempts": 1,
            "temperature": "provider default",
        },
        "system_sdk_generation_attempts_configured": 3,
        "system_recommendation_output_attempts_configured": 2,
        "system_metadata_recommendation_fallback": True,
        "system_actual_model_call_count": "not exposed",
        "operational_scope": "same configured model/output cap; different prompts/routes and internal retry policies; not equal compute",
        "order": "system first on even indexes; baseline first on odd indexes",
        "history": "each arm's own actual responses",
        "human_audits": "pending",
        "submission_ready": False,
        "python": sys.version,
        **original.runtime_versions(),
    }
    (args.output / "freeze.json").write_text(
        json.dumps(freeze, indent=2) + "\n", encoding="utf8", newline="\n"
    )
    (args.output / "cases.jsonl").write_bytes(cases_path.read_bytes())
    baseline = original.Baseline(catalog, args.gcloud)
    histories, records, consecutive = defaultdict(list), [], Counter()
    stopped = False
    for index, case in enumerate(cases):
        for arm in ("system", "baseline") if index % 2 == 0 else ("baseline", "system"):
            history = (
                original.build_history(histories[(arm, case["session"])])
                if case.get("session")
                else []
            )
            record = {
                "case_id": case["id"],
                "cluster": case["cluster"],
                "family": case["family"],
                "session": case.get("session"),
                "arm": arm,
                "question": case["question"],
                "history": history,
                "started_at": original.utc(),
            }
            start = time.monotonic()
            if arm == "baseline":
                call = lambda case=case, history=history: baseline.call(case["question"], history)
            else:
                call = lambda case=case, history=history: (
                    original.get_json(
                        original.SERVICE + "/api/chat",
                        {"question": case["question"], "history": history},
                    ),
                    {"note": "internal retrieval trace/model usage not exposed"},
                )
            record.update(attempt(case, history, catalog, call))
            record.update(ended_at=original.utc(), latency_s=time.monotonic() - start)
            with (args.output / "responses.jsonl").open(
                "a", encoding="utf8", newline="\n"
            ) as handle:
                handle.write(json.dumps(record, ensure_ascii=False) + "\n")
                handle.flush()
            records.append(record)
            if case.get("session"):
                histories[(arm, case["session"])].append(record)
            print(f"{index + 1:02}/60 {case['id']} {arm}: {record['score']['outcome']}", flush=True)
            consecutive[arm] = (
                consecutive[arm] + 1 if record["score"]["outcome"] == "transport_error" else 0
            )
            if consecutive[arm] >= 3:
                stopped = True
                break
        if stopped:
            break
    try:
        after = original.get_json(original.SERVICE + "/api/config")
        stable = all(after.get(k) == v for k, v in pins.items())
    except Exception as exc:  # noqa: BLE001 - class only
        after, stable = {"readback_error_type": type(exc).__name__}, False
    result = original.summarize(records, 60)
    result.update(
        completed_at=original.utc(),
        stopped_early=stopped,
        release_stable=stable,
        responses_sha256=original.digest(args.output / "responses.jsonl"),
        config_after=after,
        frozen_inputs_unchanged=(
            original.digest(args.catalog) == freeze["catalog_sha256"]
            and original.digest(cases_path) == freeze["cases_sha256"]
            and original.digest(__file__) == freeze["source_sha256"]
            and original.digest(original.__file__) == freeze["original_module_sha256"]
            and original.digest(Path(__file__).with_name("query_fix_eval.py"))
            == freeze["pin_module_sha256"]
            and original.digest(Path(__file__).with_name("run_simple_eval.py"))
            == freeze["original_pin_module_sha256"]
        ),
        human_audits="pending",
        submission_ready=False,
    )
    (args.output / "summary.json").write_text(
        json.dumps(result, indent=2) + "\n", encoding="utf8", newline="\n"
    )
    print(
        json.dumps({k: v for k, v in result.items() if k != "config_after"}, indent=2), flush=True
    )
    if stopped or not stable or not result["frozen_inputs_unchanged"]:
        raise SystemExit(2)


if __name__ == "__main__":
    main()
