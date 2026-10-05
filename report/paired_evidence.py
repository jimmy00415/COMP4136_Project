"""Offline exact-source checks and figure for the completed paired study."""

import hashlib
import json
import statistics
from collections import Counter
from pathlib import Path

import matplotlib.pyplot as plt

EXPECTED = {
    "challenge_cases.jsonl": "2ab256dc99112b6864f9c41f51f77d216ebff8aeccfa6458c30107f54787e7f5",
    "final_paired_answers.jsonl": "6567f1d86c43f03c18234580c54df22917a283a8b362d782a16032ab4d17fedb",
    "final_paired_review.json": "bc2d7f467f74868eaf30b5879bd5296c786bc31aa710e6dd6bc8325bb069b491",
    "final_paired_summary.json": "d73deb6b22b00902856584f24d9599b33afc10d76ad3a9ca8fd26b8d472c5760",
    "final_paired_freeze.json": "93476c67efd75c522e1641a74f871f400d8008869565aa6fb75651ca3b901213",
    "final_paired_eval.py": "b0aa2684ba1e52424039b63a49aceb55cf947f7f933a50fb0bf88ef17c03fae1",
}


def evidence():
    root = Path(__file__).resolve().parents[1] / "course"
    for name, expected in EXPECTED.items():
        if hashlib.sha256((root / name).read_bytes()).hexdigest() != expected:
            raise ValueError("Evidence byte mismatch: " + name)

    def load(name):
        return json.loads((root / name).read_text(encoding="utf8"))

    cases = [
        json.loads(x)
        for x in (root / "challenge_cases.jsonl").read_text(encoding="utf8").splitlines()
    ]
    rows = [
        json.loads(x)
        for x in (root / "final_paired_answers.jsonl").read_text(encoding="utf8").splitlines()
    ]
    audit, summary, freeze = (
        load("final_paired_" + name + ".json") for name in ("review", "summary", "freeze")
    )
    ids = {c["id"] for c in cases}
    lookup = {(e["case_id"], e["arm"]): e for e in audit["entries"]}
    assert len(cases) == len(ids) == 60
    assert len(rows) == len(audit["entries"]) == len(lookup) == 120
    assert set(lookup) == {(i, arm) for i in ids for arm in ("system", "baseline")}
    assert {(r["case_id"], r["arm"]) for r in rows} == set(lookup)
    assert freeze["source_sha256"] == EXPECTED["final_paired_eval.py"]
    assert freeze["cases_sha256"] == EXPECTED["challenge_cases.jsonl"]
    assert (
        audit["responses_sha256"]
        == summary["responses_sha256"]
        == EXPECTED["final_paired_answers.jsonl"]
    )
    assert summary["audit_sha256"] == EXPECTED["final_paired_review.json"]
    assert summary["freeze_sha256"] == EXPECTED["final_paired_freeze.json"]
    assert audit["status"] == "complete" and not summary["unseen_holdout"]
    assert (
        summary["execution"]["release_stable"] and summary["execution"]["frozen_inputs_unchanged"]
    )
    assert not summary["execution"]["stopped_early"]
    assert audit["human_audits"] == summary["human_audits"] == "pending"
    assert not audit["submission_ready"] and not summary["submission_ready"]
    arms = {}
    for arm in ("system", "baseline"):
        rr = [r for r in rows if r["arm"] == arm]
        verdicts = [lookup[r["case_id"], arm] for r in rr]
        published = summary["arms"][arm]
        assert len(rr) == 60 and sum(v["pass"] for v in verdicts) == published["passed"]
        assert dict(Counter(v["outcome"] for v in verdicts)) == published["outcomes"]
        families = {}
        for family in ("ambiguity", "language", "compound", "boundary", "dialogue"):
            fr = [r for r in rr if r["family"] == family]
            families[family] = {
                "attempted": len(fr),
                "passed": sum(lookup[r["case_id"], arm]["pass"] for r in fr),
            }
        assert families == published["families"]
        assert sum(r["score"]["pass"] for r in rr) == summary["mechanical"]["arms"][arm]["passed"]
        cards = sum(len(r["response"]["movies"]) for r in rr)
        citations = [c for r in rr for c in r["response"]["citations"]]
        assert all(c["source_kind"] in ("metadata", "movie_metadata") for c in citations)
        field_audit = audit["card_field_audit"][arm]
        assert cards * 8 == field_audit["checked"] and field_audit["mismatches"] == 0
        latency = [r["latency_s"] for r in rr]
        assert statistics.median(latency) == published["median_latency_s"]
        arms[arm] = {
            "passed": published["passed"],
            "families": families,
            "cards": cards,
            "citations": len(citations),
            "card_fields": field_audit["checked"],
            "median_latency_s": statistics.median(latency),
            "max_latency_s": max(latency),
            "outcomes": published["outcomes"],
        }
    pairs = Counter((lookup[i, "system"]["pass"], lookup[i, "baseline"]["pass"]) for i in ids)
    assert pairs == Counter({(True, True): 50, (True, False): 10})
    assert summary["paired"]["both_pass"] == 50 and summary["paired"]["system_only"] == 10
    assert len(summary["overrides"]) == 7
    return {"arms": arms, "paired": summary["paired"], "overrides": 7}


def family_chart(result, here):
    names = [
        "Ambiguity / explicit ID",
        "Traditional / Simplified",
        "Compound recommendations",
        "Evidence / domain boundary",
        "Dialogue",
    ]
    keys = ["ambiguity", "language", "compound", "boundary", "dialogue"]
    fig, ax = plt.subplots(figsize=(10.2, 4.5))
    for arm, offset, color in (("system", -0.17, "#205A83"), ("baseline", 0.17, "#AD650D")):
        fam = result["arms"][arm]["families"]
        vals = [100 * fam[k]["passed"] / fam[k]["attempted"] for k in keys]
        ax.barh(
            [i + offset for i in range(5)],
            vals,
            height=0.29,
            color=color if arm == "system" else "white",
            edgecolor=color,
            hatch=None if arm == "system" else "///",
            label="Deployed system" if arm == "system" else "BM25 + LLM",
        )
        for i, k in enumerate(keys):
            ax.text(
                vals[i] + 1.5,
                i + offset,
                f"{fam[k]['passed']}/{fam[k]['attempted']}",
                va="center",
                color=color,
                fontsize=11,
            )
    ax.set_yticks(range(5), names)
    ax.invert_yaxis()
    ax.set_xlim(0, 116)
    ax.set_xticks([0, 25, 50, 75, 100], ["0%", "25%", "50%", "75%", "100%"])
    ax.set_xlabel("AI-reviewed task completion on known development cases")
    ax.spines[["top", "right", "left"]].set_visible(False)
    ax.tick_params(axis="y", length=0)
    ax.set_axisbelow(True)
    ax.grid(axis="x", color="#DDE5EC", lw=0.6)
    ax.legend(loc="lower center", bbox_to_anchor=(0.42, 1.01), ncol=2, frameon=False)
    fig.subplots_adjust(left=0.32, right=0.985, top=0.84, bottom=0.16)
    for extension in ("png", "svg"):
        fig.savefig(here / "figures" / ("family_results." + extension), dpi=300, facecolor="white")
    plt.close(fig)
