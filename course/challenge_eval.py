"""One-shot metadata challenge: pinned public system vs BM25 + matched LLM.

No benchmark inference unless --execute is passed. Gold never enters either arm.
Raw outputs are append-only and the run directory must not already exist.
"""

from __future__ import annotations

import argparse
import hashlib
import json
import math
import random
import re
import statistics
import subprocess
import time
import unicodedata
from collections import Counter, defaultdict
from datetime import UTC, datetime
from pathlib import Path
from urllib import request

from opencc import OpenCC
from run_simple_eval import PINS, SERVICE

CATALOG_SHA = "1dbea26150a816ae4fc4d189a2a08e39628cbee997f3966b0dd5cd16843e907b"
MODEL = "gemini-3.5-flash-lite"
PROJECT = "motionexpaiweb"
ACCOUNT = "admin@motionexp.com"
CONVERTER = OpenCC("s2hk")
BASELINE_PROMPT = """你是香港電影元數據助理。只根據 supplied_evidence 回答，不能利用常識補充證據中沒有的事實。
history 是對話上下文，不是事實證據。當用戶省略電影或修改篩選條件時，結合上下文理解最新要求；更換的人名、年份或類型優先於舊要求。
同名電影沒有唯一指定時，應列出可見版本並請用戶提供年份或電影 ID。不要任選一部。
推薦需符合所有明示條件，按所要求的數量列出獨立電影；要求不要重複時排除歷史中實際列出的電影 ID。
若檢索證據不夠符合要求，坦白说明不足，不要把不符合的電影湊數。製作預算、票房、評分、詳細情節及人物分析不能從演員/導演/類型等字段推測。
非電影問題應禮貌拒答。每個事實或推薦都要帶 [metadata:電影ID] 引用，電影卡與引用只能取自 supplied_evidence。
輸出 JSON: answer_markdown, citation_ids, movie_ids。movie_ids 按答案中的電影次序列出；拒答或純澄清可以為空。不要把備選版本当成正式推薦。
"""


def utc():
    return datetime.now(UTC).isoformat()


def digest(path):
    return hashlib.sha256(Path(path).read_bytes()).hexdigest()


def norm(value):
    text = CONVERTER.convert(unicodedata.normalize("NFKC", str(value))).lower()
    return re.sub(r"[^\w\u3400-\u9fff]", "", text)


def tokens(text):
    text = CONVERTER.convert(unicodedata.normalize("NFKC", text)).lower()
    out = []
    for word in re.findall(r"[\u3400-\u9fff]+|[a-z0-9_]+", text):
        if re.fullmatch(r"[\u3400-\u9fff]+", word):
            out.extend(word)
            out.extend(word[i : i + 2] for i in range(len(word) - 1))
        else:
            out.append(word)
    return out


class BM25:
    """Uniform metadata fields, generic script normalization, no query router."""

    def __init__(self, rows):
        self.rows = sorted(rows, key=lambda r: r["movie_id"])
        self.tf = [Counter(tokens(json.dumps(r, ensure_ascii=False))) for r in self.rows]
        self.lengths = [sum(x.values()) for x in self.tf]
        self.avg = statistics.mean(self.lengths) if self.lengths else 1
        self.df = Counter(t for doc in self.tf for t in doc)

    def search(self, query, k=8):
        query_terms = set(tokens(query))
        scores = []
        n = len(self.rows)
        for i, doc in enumerate(self.tf):
            s = 0.0
            for t in query_terms & doc.keys():
                idf = math.log(1 + (n - self.df[t] + 0.5) / (self.df[t] + 0.5))
                f = doc[t]
                s += idf * f * 2.5 / (f + 1.5 * (0.25 + 0.75 * self.lengths[i] / self.avg))
            if s > 0:
                scores.append((s, i))
        scores.sort(key=lambda x: (-x[0], self.rows[x[1]]["movie_id"]))
        return [self.rows[i] for _, i in scores[:k]]


def eligible(row, constraints):
    for key, value in constraints.items():
        if key in {"director", "cast", "genre"} and norm(value) not in norm(row.get(key, "")):
            return False
        if key == "genres_all" and any(norm(x) not in norm(row.get("genre", "")) for x in value):
            return False
        if key == "tier" and row.get("tier") != value:
            return False
        if key == "year_min" and row.get("release_date", "")[:4] < str(value):
            return False
        if key == "year_max" and row.get("release_date", "")[:4] > str(value):
            return False
        if key not in {"director", "cast", "genre", "genres_all", "tier", "year_min", "year_max"}:
            raise ValueError("unknown gold constraint " + key)
    return True


def validate_generated(obj, evidence):
    if (
        not isinstance(obj, dict)
        or not isinstance(obj.get("answer_markdown"), str)
        or not obj["answer_markdown"].strip()
    ):
        raise ValueError("invalid generated answer")
    lookup = {r["movie_id"]: r for r in evidence}
    mids, cids = obj.get("movie_ids"), obj.get("citation_ids")
    if (
        not isinstance(mids, list)
        or not isinstance(cids, list)
        or any(not isinstance(x, str) for x in mids + cids)
    ):
        raise ValueError("invalid generated identities")
    if len(set(mids)) != len(mids) or len(set(cids)) != len(cids):
        raise ValueError("duplicate generated identities")
    for cid in cids:
        if (
            not cid.startswith("metadata:")
            or cid[9:] not in lookup
            or "[" + cid + "]" not in obj["answer_markdown"]
        ):
            raise ValueError("unretrieved or unmarked citation")
    if any(mid not in lookup or "metadata:" + mid not in cids for mid in mids):
        raise ValueError("unretrieved or uncited movie")
    markers = re.findall(r"\[(metadata:[^\]\s]+)\]", obj["answer_markdown"])
    if any(cid not in cids for cid in markers):
        raise ValueError("unreturned citation marker")
    return {
        "answer_markdown": obj["answer_markdown"],
        "movies": [lookup[mid] for mid in mids],
        "citations": [
            {
                "citation_id": cid,
                "movie_id": cid[9:],
                "source_kind": "movie_metadata",
                "excerpt": json.dumps(lookup[cid[9:]], ensure_ascii=False),
            }
            for cid in cids
        ],
    }


def build_history(records):
    history = []
    for r in records:
        resp = r.get("response")
        if isinstance(resp, dict) and isinstance(resp.get("answer_markdown"), str):
            history.append(
                {
                    "question": r["question"],
                    "answer": resp["answer_markdown"],
                    "movie_ids": [
                        m["movie_id"]
                        for m in resp.get("movies", [])
                        if isinstance(m, dict) and "movie_id" in m
                    ],
                }
            )
    history = history[-4:]
    while sum(len(h["question"]) + len(h["answer"]) for h in history) > 12000:
        history.pop(0)
    return history


def classify(case, response, catalog, history):
    fail = {
        "pass": False,
        "outcome": "error",
        "observed_action": "unknown",
        "reason": "malformed_response",
        "valid_citations": False,
    }
    if not isinstance(response, dict):
        return fail
    answer = response.get("answer_markdown")
    citations, movies = response.get("citations"), response.get("movies")
    if (
        not isinstance(answer, str)
        or not answer.strip()
        or not isinstance(citations, list)
        or not isinstance(movies, list)
    ):
        return fail
    if any(not isinstance(c, dict) for c in citations + movies):
        return fail
    mids = [m.get("movie_id") for m in movies]
    if any(not isinstance(mid, str) or mid not in catalog for mid in mids):
        return fail | {"reason": "unknown_movie"}
    cids = [c.get("citation_id") for c in citations]
    valid = all(
        isinstance(c.get("citation_id"), str)
        and c["citation_id"] == "metadata:" + str(c.get("movie_id"))
        and c.get("movie_id") in catalog
        and c.get("source_kind") in {"metadata", "movie_metadata"}
        and "[" + c["citation_id"] + "]" in answer
        for c in citations
    )
    valid = valid and len(set(cids)) == len(cids)
    valid = valid and all(c in cids for c in re.findall(r"\[(metadata:[^\]\s]+)\]", answer))
    clean = norm(answer)
    clarify = any(
        norm(s) in clean
        for s in (
            "哪一部",
            "哪個版本",
            "哪個年份",
            "請提供年份",
            "請指定",
            "請選擇",
            "想查的是",
            "是指",
            "請改用movie ID",
            "which version",
            "which movie",
        )
    )
    clarify = clarify or (
        norm("多部同名電影") in clean and norm("請改用") in clean and "movieid" in clean
    )
    refused = any(
        norm(s) in clean
        for s in (
            "只能回答",
            "無法",
            "不能回答",
            "不足",
            "未提供",
            "沒有提供",
            "沒有足夠",
            "未收錄",
            "不支援",
            "不支持",
            "找不到",
            "cannot",
            "not enough",
            "不包含",
            "缺乏",
        )
    )
    action = "clarify" if clarify else ("refuse" if refused else "answer")
    base = fail | {"observed_action": action, "valid_citations": valid}
    expected = case["expected"]
    if expected == "clarify":
        candidates = set(case.get("candidates", catalog))
        cited_movies = {c.get("movie_id") for c in citations}
        mentioned_ids = set(re.findall(r"\b\d{4}_[A-Za-z0-9]+_\d{3}\b", answer))
        ok = clarify and valid and cited_movies <= candidates and mentioned_ids <= candidates
        return base | {
            "pass": ok,
            "outcome": "reasonable_clarification" if ok else "error",
            "reason": "appropriate_clarification"
            if ok
            else "ambiguous_title_answered_or_not_clarified",
        }
    if expected == "refuse":
        hallucinated = bool(
            re.search(r"(?:預算|预算|票房|評分|评分).{0,8}(?:是|為|为|約|约)\s*\d", answer)
        )
        kind = case.get("unsupported")
        if kind in {"budget", "box_office"}:
            hallucinated |= bool(re.search(r"\d+(?:\.\d+)?\s*(?:萬|万|億|亿|港元|美元)", answer))
        if kind == "rating":
            hallucinated |= bool(
                re.search(
                    r"(?:IMDb|評分|评分).{0,15}\d+(?:\.\d+)?\s*(?:分|/10)", answer, re.IGNORECASE
                )
            )
        if kind == "domain":
            hallucinated |= bool(
                re.search(r"```|\bsorted\s*\(|\bdef\s+\w+\s*\(|\bprint\s*\(", answer)
            )
        ok = (
            refused
            and not hallucinated
            and (not mids or case.get("unsupported") != "domain")
            and valid
        )
        return base | {
            "pass": ok,
            "outcome": "reasonable_refusal" if ok else "error",
            "reason": "appropriate_refusal" if ok else "unsupported_request_not_safely_refused",
        }
    if expected == "fact":
        mid = case.get("movie_id")
        if "history_ordinal" in case:
            prior = next((h for h in reversed(history) if h.get("movie_ids")), None)
            ordinal = case["history_ordinal"] - 1
            if prior is None or len(prior["movie_ids"]) <= ordinal:
                return base | {"reason": "missing_history_target"}
            mid = prior["movie_ids"][ordinal]
        row = catalog[mid]
        value = (
            row.get(case["field"], "")
            if case["field"] != "year"
            else row.get("release_date", "")[:4]
        )
        parts = [p for p in re.split(r"[、,，;；]", value) if p.strip()]
        fact_text = re.sub(r"\[[^\]]+\]", "", answer)
        fact_text = re.sub(r"\b\d{4}_[A-Za-z0-9]+_\d{3}\b", "", fact_text)
        matched = bool(parts) and all(norm(p) in norm(fact_text) for p in parts)
        contradicted = any(
            re.search(r"(?:不是|並非|并非|not)" + re.escape(norm(p)), norm(fact_text))
            for p in parts
        )
        grounded = valid and "metadata:" + mid in cids
        ok = matched and grounded and not clarify and not refused and not contradicted
        return base | {
            "pass": ok,
            "outcome": "correct_answer" if ok else "error",
            "reason": "grounded_fact"
            if ok
            else (
                "unnecessary_clarification"
                if clarify
                else "fact_identity_value_or_evidence_mismatch"
            ),
            "resolved_gold_movie_id": mid,
        }
    if expected == "recommend":
        exclusions = (
            {mid for h in history for mid in h.get("movie_ids", [])}
            if case.get("exclude_previous")
            else set()
        )
        count_ok = len(mids) == case.get("count", 3) and len(set(mids)) == len(mids)
        filters_ok = all(
            eligible(catalog[mid], case["constraints"]) and mid not in exclusions for mid in mids
        )
        grounded = bool(mids) and valid and all("metadata:" + mid in cids for mid in mids)
        ok = count_ok and filters_ok and grounded and not refused and not clarify
        return base | {
            "pass": ok,
            "outcome": "correct_answer" if ok else "error",
            "count_ok": count_ok,
            "filters_ok": filters_ok,
            "reason": "grounded_constrained_recommendation"
            if ok
            else "recommendation_count_filter_or_evidence_mismatch",
        }
    raise ValueError("unknown expected action")


def summarize(records, planned_turns):
    arms = {}
    for arm in ("system", "baseline"):
        rr = [r for r in records if r["arm"] == arm]
        arms[arm] = {
            "planned": planned_turns,
            "attempted": len(rr),
            "passed": sum(r["score"]["pass"] for r in rr),
            "outcomes": dict(Counter(r["score"]["outcome"] for r in rr)),
            "median_latency_s": statistics.median(r["latency_s"] for r in rr) if rr else None,
            "families": {
                f: {
                    "attempted": sum(r["family"] == f for r in rr),
                    "passed": sum(r["family"] == f and r["score"]["pass"] for r in rr),
                }
                for f in sorted({r["family"] for r in rr})
            },
        }
    pairs = defaultdict(dict)
    for r in records:
        pairs[r["case_id"]][r["arm"]] = r
    complete = [p for p in pairs.values() if len(p) == 2]
    both = sum(p["system"]["score"]["pass"] and p["baseline"]["score"]["pass"] for p in complete)
    s_only = sum(
        p["system"]["score"]["pass"] and not p["baseline"]["score"]["pass"] for p in complete
    )
    b_only = sum(
        p["baseline"]["score"]["pass"] and not p["system"]["score"]["pass"] for p in complete
    )
    clusters = defaultdict(list)
    for p in complete:
        clusters[p["system"]["cluster"]].append(
            int(p["system"]["score"]["pass"]) - int(p["baseline"]["score"]["pass"])
        )
    ci = None
    if clusters:
        groups = list(clusters.values())
        rng = random.Random(4136)
        diffs = []
        for _ in range(10000):
            sample = [rng.choice(groups) for _ in groups]
            diffs.append(sum(sum(g) for g in sample) / sum(len(g) for g in sample))
        diffs.sort()
        ci = [diffs[249], diffs[9749]]
    return {
        "arms": arms,
        "paired": {
            "complete_pairs": len(complete),
            "both_pass": both,
            "system_only": s_only,
            "baseline_only": b_only,
            "both_fail": len(complete) - both - s_only - b_only,
            "difference": (s_only - b_only) / len(complete) if complete else None,
            "cluster_bootstrap_95pct": ci,
            "clusters": len(clusters),
        },
    }


def get_json(url, payload=None):
    data = None if payload is None else json.dumps(payload, ensure_ascii=False).encode("utf8")
    req = request.Request(url, data=data, headers={"Content-Type": "application/json"})
    with request.urlopen(req, timeout=60) as res:
        return json.load(res)


class OutputValidationError(ValueError):
    def __init__(self, metadata):
        super().__init__("generated output failed contract validation")
        self.metadata = metadata


class Baseline:
    def __init__(self, catalog, gcloud):
        from google import genai
        from google.genai import types
        from google.oauth2.credentials import Credentials

        token = subprocess.run(
            [str(gcloud), "auth", "print-access-token", "--account=" + ACCOUNT],
            capture_output=True,
            text=True,
            timeout=45,
            check=False,
        )
        if token.returncode or not token.stdout.strip():
            raise RuntimeError("CLI authentication unavailable; token and stderr suppressed")
        self.client = genai.Client(
            vertexai=True,
            project=PROJECT,
            location="global",
            credentials=Credentials(token=token.stdout.strip()),
            http_options=types.HttpOptions(
                timeout=60000, retry_options=types.HttpRetryOptions(attempts=1)
            ),
        )
        self.index = BM25(list(catalog.values()))
        self.types = types

    def call(self, question, history):
        query = "\n".join(
            [question] + [h["question"] + " " + " ".join(h["movie_ids"]) for h in history]
        )
        evidence = self.index.search(query, 8)
        payload = {"question": question, "history": history, "supplied_evidence": evidence}
        schema = {
            "type": "object",
            "properties": {
                "answer_markdown": {"type": "string"},
                "movie_ids": {"type": "array", "items": {"type": "string"}},
                "citation_ids": {"type": "array", "items": {"type": "string"}},
            },
            "required": ["answer_markdown", "movie_ids", "citation_ids"],
        }
        generated = self.client.models.generate_content(
            model=MODEL,
            contents=json.dumps(payload, ensure_ascii=False),
            config=self.types.GenerateContentConfig(
                system_instruction=BASELINE_PROMPT,
                max_output_tokens=1024,
                response_mime_type="application/json",
                response_json_schema=schema,
            ),
        )
        raw = generated.text
        metadata = {
            "retrieved_ids": [r["movie_id"] for r in evidence],
            "raw_generation": raw,
            "model_version": getattr(generated, "model_version", None),
            "usage": generated.usage_metadata.model_dump(mode="json")
            if generated.usage_metadata
            else None,
        }
        try:
            obj = json.loads(raw)
            response = validate_generated(obj, evidence)
        except (ValueError, TypeError) as exc:
            raise OutputValidationError(metadata) from exc
        return response, metadata


def main():
    ap = argparse.ArgumentParser(description=__doc__)
    ap.add_argument("--catalog", type=Path, required=True)
    ap.add_argument("--cases", type=Path, default=Path(__file__).with_name("challenge_cases.jsonl"))
    ap.add_argument("--output", type=Path, required=True)
    ap.add_argument(
        "--gcloud",
        type=Path,
        default=Path(
            "C:/Users/OWNER/AppData/Local/Google/Cloud SDK/google-cloud-sdk/bin/gcloud.cmd"
        ),
    )
    ap.add_argument("--smoke", action="store_true")
    ap.add_argument("--execute", action="store_true")
    args = ap.parse_args()
    if digest(args.catalog) != CATALOG_SHA:
        raise ValueError("catalog differs from original release-matched metadata")
    catalog = {
        r["movie_id"]: r
        for r in (json.loads(x) for x in args.catalog.read_text(encoding="utf8").splitlines())
    }
    cases = [json.loads(x) for x in args.cases.read_text(encoding="utf8").splitlines()]
    if len(catalog) != 4659 or len(cases) != 60 or len({c["id"] for c in cases}) != 60:
        raise ValueError("unexpected challenge size or duplicate IDs")
    for c in cases:
        if c["expected"] == "recommend" and sum(
            eligible(r, c["constraints"]) for r in catalog.values()
        ) < c["count"] + (3 if c.get("exclude_previous") else 0):
            raise ValueError("infeasible recommendation " + c["id"])
        if (
            c["expected"] == "fact"
            and "movie_id" in c
            and (
                c["movie_id"] not in catalog
                or not catalog[c["movie_id"]].get(
                    c["field"] if c["field"] != "year" else "release_date"
                )
            )
        ):
            raise ValueError("missing fact gold " + c["id"])
    before = get_json(SERVICE + "/api/config")
    if any(before.get(k) != v for k, v in PINS.items()):
        raise RuntimeError("deployed release drift")
    if get_json(SERVICE + "/health").get("status") != "ok":
        raise RuntimeError("deployed service unhealthy")
    if not args.execute and not args.smoke:
        print(
            json.dumps(
                {
                    "preflight": "passed",
                    "turns": 60,
                    "catalog_sha256": digest(args.catalog),
                    "cases_sha256": digest(args.cases),
                    "chat_or_generation_calls": 0,
                },
                indent=2,
            )
        )
        return
    baseline = Baseline(catalog, args.gcloud)
    if args.smoke:
        response, meta = baseline.call("請問《醉拳》是哪一年上映的？", [])
        args.output.mkdir(parents=True, exist_ok=False)
        (args.output / "smoke.json").write_text(
            json.dumps(
                {
                    "question": "請問《醉拳》是哪一年上映的？",
                    "response": response,
                    "metadata": meta,
                    "at": utc(),
                },
                ensure_ascii=False,
                indent=2,
            ),
            encoding="utf8",
        )
        print("Separate baseline smoke recorded; no benchmark questions executed.")
        return
    args.output.mkdir(parents=True, exist_ok=False)
    freeze = {
        "at": utc(),
        "catalog_sha256": digest(args.catalog),
        "cases_sha256": digest(args.cases),
        "source_sha256": digest(__file__),
        "case_builder_sha256": digest(Path(__file__).with_name("prepare_challenge.py")),
        "original_evaluator_sha256": digest(Path(__file__).with_name("run_simple_eval.py")),
        "prompt_sha256": hashlib.sha256(BASELINE_PROMPT.encode("utf8")).hexdigest(),
        "baseline_model": MODEL,
        "baseline_project": PROJECT,
        "baseline_location": "global",
        "planned_turns": 60,
        "planned_calls": 120,
        "config_before": before,
        "python": __import__("sys").version,
        "google_genai_version": __import__("importlib.metadata", fromlist=["version"]).version(
            "google-genai"
        ),
        "opencc_version": __import__("importlib.metadata", fromlist=["version"]).version(
            "opencc-python-reimplemented"
        ),
        "baseline_config": {
            "k1": 1.5,
            "b": 0.75,
            "top_k": 8,
            "max_output_tokens": 1024,
            "sdk_attempts": 1,
            "temperature": "provider default",
        },
        "order": "system first on even turn indexes, baseline first on odd indexes",
        "human_audits": "pending",
        "submission_ready": False,
    }
    (args.output / "freeze.json").write_text(
        json.dumps(freeze, ensure_ascii=False, indent=2), encoding="utf8"
    )
    (args.output / "cases.jsonl").write_bytes(args.cases.read_bytes())
    histories = defaultdict(list)
    records = []
    consecutive_errors = Counter()
    stopped = False
    for i, case in enumerate(cases):
        for arm in ("system", "baseline") if i % 2 == 0 else ("baseline", "system"):
            history = (
                build_history(histories[(arm, case.get("session"))]) if case.get("session") else []
            )
            record = {
                "case_id": case["id"],
                "cluster": case["cluster"],
                "family": case["family"],
                "session": case.get("session"),
                "arm": arm,
                "question": case["question"],
                "history": history,
                "started_at": utc(),
            }
            start = time.monotonic()
            try:
                if arm == "system":
                    resp = get_json(
                        SERVICE + "/api/chat", {"question": case["question"], "history": history}
                    )
                    metadata = {
                        "retrieved_ids": None,
                        "note": "internal retrieval trace/usage is not exposed by deployed API",
                    }
                else:
                    resp, metadata = baseline.call(case["question"], history)
                record.update(
                    response=resp, metadata=metadata, score=classify(case, resp, catalog, history)
                )
                consecutive_errors[arm] = 0
            except OutputValidationError as exc:
                record.update(
                    response=None,
                    error_type=type(exc).__name__,
                    metadata=exc.metadata,
                    score={
                        "pass": False,
                        "outcome": "error",
                        "observed_action": "unknown",
                        "valid_citations": False,
                        "reason": "generation_output_validation_failure",
                    },
                )
                consecutive_errors[arm] = 0
            except Exception as exc:  # noqa: BLE001 - retain real call failures, never provider bodies
                # Never print or persist provider error bodies, credentials or token strings.
                record.update(
                    response=None,
                    error_type=type(exc).__name__,
                    score={
                        "pass": False,
                        "outcome": "transport_error",
                        "observed_action": "none",
                        "valid_citations": False,
                        "reason": "call_failure",
                    },
                )
                consecutive_errors[arm] += 1
            record.update(latency_s=time.monotonic() - start, ended_at=utc())
            with (args.output / "responses.jsonl").open("a", encoding="utf8") as f:
                f.write(json.dumps(record, ensure_ascii=False) + "\n")
                f.flush()
            records.append(record)
            if case.get("session"):
                histories[(arm, case["session"])].append(record)
            print(
                f"{i + 1:02d}/60 {case['id']} {arm}: {record['score']['outcome']} ({record['latency_s']:.2f}s)",
                flush=True,
            )
            if consecutive_errors[arm] >= 3:
                stopped = True
                break
        if stopped:
            break
    after = get_json(SERVICE + "/api/config")
    stable = all(after.get(k) == v for k, v in PINS.items())
    summary = summarize(records, 60)
    summary.update(
        completed_at=utc(),
        stopped_early=stopped,
        release_stable=stable,
        responses_sha256=digest(args.output / "responses.jsonl"),
        config_after=after,
        frozen_inputs_unchanged=(
            digest(args.cases) == freeze["cases_sha256"]
            and digest(__file__) == freeze["source_sha256"]
            and digest(args.catalog) == freeze["catalog_sha256"]
        ),
    )
    (args.output / "summary.json").write_text(
        json.dumps(summary, ensure_ascii=False, indent=2), encoding="utf8"
    )
    print(
        json.dumps(
            {k: v for k, v in summary.items() if k not in {"config_after"}},
            ensure_ascii=False,
            indent=2,
        )
    )
    if stopped or not stable:
        raise SystemExit(2)


if __name__ == "__main__":
    main()
