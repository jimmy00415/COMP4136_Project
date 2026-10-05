"""Offline evidence dossier and explicit AI audit; never calls either system."""

from __future__ import annotations

import argparse
import copy
import json
from pathlib import Path

from challenge_eval import digest, eligible, summarize, utc


def audited_records(records, audit):
    if audit.get("reviewer_type") != "AI":
        raise ValueError("This route records AI review only, never a human audit")
    entries = audit.get("entries", [])
    lookup = {(e["case_id"], e["arm"]): e for e in entries}
    keys = {(r["case_id"], r["arm"]) for r in records}
    if len(lookup) != len(entries) or set(lookup) != keys:
        raise ValueError("Audit must cover each actual record exactly once")
    reviewed = []
    overrides = []
    allowed = {
        "correct_answer",
        "reasonable_clarification",
        "reasonable_refusal",
        "error",
        "transport_error",
    }
    for record in records:
        e = lookup[(record["case_id"], record["arm"])]
        if (
            not isinstance(e.get("pass"), bool)
            or e.get("outcome") not in allowed
            or not e.get("reason", "").strip()
        ):
            raise ValueError("Pending or incomplete audit verdict")
        if e["pass"] != (
            e["outcome"] in {"correct_answer", "reasonable_clarification", "reasonable_refusal"}
        ):
            raise ValueError("Audit pass/outcome conflict")
        r = copy.deepcopy(record)
        r["score"].update({"pass": e["pass"], "outcome": e["outcome"], "audit_reason": e["reason"]})
        reviewed.append(r)
        if any(e[k] != record["score"][k] for k in ("pass", "outcome")):
            overrides.append(
                {
                    "case_id": record["case_id"],
                    "arm": record["arm"],
                    "mechanical_score": record["score"],
                    "audit_pass": e["pass"],
                    "audit_outcome": e["outcome"],
                    "reason": e["reason"],
                }
            )
    return reviewed, overrides


def main():
    ap = argparse.ArgumentParser(description=__doc__)
    ap.add_argument("--run", type=Path, required=True)
    ap.add_argument("--catalog", type=Path, required=True)
    ap.add_argument("--prepare", action="store_true")
    ap.add_argument("--finalize", action="store_true")
    args = ap.parse_args()
    run = args.run
    records = [
        json.loads(x) for x in (run / "responses.jsonl").read_text(encoding="utf8").splitlines()
    ]
    cases = [json.loads(x) for x in (run / "cases.jsonl").read_text(encoding="utf8").splitlines()]
    summary = json.loads((run / "summary.json").read_text(encoding="utf8"))
    freeze = json.loads((run / "freeze.json").read_text(encoding="utf8"))
    if (
        digest(run / "responses.jsonl") != summary["responses_sha256"]
        or digest(args.catalog) != freeze["catalog_sha256"]
    ):
        raise ValueError("Actual evidence hash mismatch")
    if digest(run / "cases.jsonl") != freeze["cases_sha256"]:
        raise ValueError("Case snapshot differs from freeze")
    mechanical = summarize(records, len(cases))
    if any(mechanical[k] != summary[k] for k in ("arms", "paired")):
        raise ValueError("Mechanical summary cannot be reproduced")
    catalog = {
        r["movie_id"]: r
        for r in (json.loads(x) for x in args.catalog.read_text(encoding="utf8").splitlines())
    }
    lookup = {(r["case_id"], r["arm"]): r for r in records}
    if args.prepare:
        lines = [
            "# Full paired evidence for Root AI review",
            "",
            f"Raw responses SHA-256: {summary['responses_sha256']}",
            "",
        ]
        for case in cases:
            lines.extend(
                [
                    f"## {case['id']}: {case['question']}",
                    "",
                    f"Gold: `{json.dumps(case, ensure_ascii=False)}`",
                    "",
                ]
            )
            for arm in ("system", "baseline"):
                r = lookup.get((case["id"], arm))
                if r is None:
                    lines.extend([arm + ": unattempted", ""])
                    continue
                lines.extend(
                    [
                        f"### {arm}",
                        "",
                        f"Mechanical: `{json.dumps(r['score'], ensure_ascii=False)}`",
                        f"Actual history IDs: `{[h['movie_ids'] for h in r['history']]}`",
                        "",
                    ]
                )
                resp = r.get("response")
                if resp:
                    lines.extend(
                        [
                            resp["answer_markdown"],
                            "",
                            f"Cards: `{[m['movie_id'] for m in resp['movies']]}`",
                            "",
                        ]
                    )
                    for m in resp["movies"]:
                        mid = m["movie_id"]
                        lines.append(
                            f"Catalog {mid}: `{json.dumps(catalog[mid], ensure_ascii=False)}`"
                        )
                else:
                    lines.extend(
                        [
                            f"Error class: {r.get('error_type')}",
                            r.get("metadata", {}).get("raw_generation") or "(no generated answer)",
                            "",
                        ]
                    )
                if arm == "baseline":
                    lines.append(f"Retrieved: `{r.get('metadata', {}).get('retrieved_ids')}`")
            lines.append("")
        (run / "review-dossier.md").write_text("\n".join(lines), encoding="utf8")
        audit = {
            "reviewer": "Root AI",
            "reviewer_type": "AI",
            "status": "pending",
            "responses_sha256": summary["responses_sha256"],
            "human_audits": "pending",
            "submission_ready": False,
            "entries": [
                {
                    "case_id": r["case_id"],
                    "arm": r["arm"],
                    "pass": None,
                    "outcome": "pending",
                    "reason": "",
                }
                for r in records
            ],
        }
        with (run / "root-ai-review.json").open("x", encoding="utf8") as f:
            json.dump(audit, f, ensure_ascii=False, indent=2)
        print("Dossier written. Every audit verdict is pending until explicitly reviewed.")
    if args.finalize:
        audit = json.loads((run / "root-ai-review.json").read_text(encoding="utf8"))
        if (
            audit.get("responses_sha256") != summary["responses_sha256"]
            or audit.get("status") != "complete"
        ):
            raise ValueError("Review is pending or bound to different evidence")
        reviewed, overrides = audited_records(records, audit)
        scored = summarize(reviewed, len(cases))
        capabilities = []
        for case in cases:
            r = lookup.get((case["id"], "baseline"))
            if r is None:
                continue
            retrieved = r.get("metadata", {}).get("retrieved_ids")
            if retrieved is None:
                continue
            eligible_ids = None
            if case["expected"] == "recommend":
                excluded = (
                    {mid for h in r["history"] for mid in h["movie_ids"]}
                    if case.get("exclude_previous")
                    else set()
                )
                eligible_ids = [
                    mid
                    for mid in retrieved
                    if eligible(catalog[mid], case["constraints"]) and mid not in excluded
                ]
            mid = case.get("movie_id")
            if case["expected"] == "fact" and "history_ordinal" in case:
                prior = next((h for h in reversed(r["history"]) if h["movie_ids"]), None)
                ordinal = case["history_ordinal"] - 1
                mid = (
                    prior["movie_ids"][ordinal]
                    if prior and len(prior["movie_ids"]) > ordinal
                    else None
                )
            capabilities.append(
                {
                    "case_id": case["id"],
                    "expected": case["expected"],
                    "retrieved_ids": retrieved,
                    "eligible_retrieved_ids": eligible_ids,
                    "fact_target_retrieved": mid in retrieved if mid else None,
                }
            )
        usages = [
            r["metadata"]["usage"]
            for r in records
            if r["arm"] == "baseline" and r.get("metadata", {}).get("usage")
        ]
        scored.update(
            {
                "mechanical": mechanical,
                "overrides": overrides,
                "baseline_retrieval_diagnostics": capabilities,
                "baseline_token_totals": {
                    key: sum(u.get(key) or 0 for u in usages)
                    for key in ("prompt_token_count", "candidates_token_count", "total_token_count")
                },
                "baseline_usage_records": len(usages),
                "responses_sha256": summary["responses_sha256"],
                "audit_sha256": digest(run / "root-ai-review.json"),
                "reviewer_type": "AI",
                "human_audits": "pending",
                "submission_ready": False,
                "computed_at": utc(),
            }
        )
        with (run / "reviewed-summary.json").open("x", encoding="utf8") as f:
            json.dump(scored, f, ensure_ascii=False, indent=2)
        print(
            json.dumps(
                {
                    k: v
                    for k, v in scored.items()
                    if k not in {"baseline_retrieval_diagnostics", "overrides", "mechanical"}
                },
                ensure_ascii=False,
                indent=2,
            )
        )


if __name__ == "__main__":
    main()
