"""Small metadata-only diagnostic study. Default mode NEVER sends chat POSTs."""
from __future__ import annotations

import argparse
from collections import Counter
from datetime import datetime, timezone
import hashlib
import json
from pathlib import Path
import re
import statistics
import time
import unicodedata
from urllib import error, request

HERE = Path(__file__).resolve().parent
SERVICE = "https://hk-movie-rag-demo-4l6lw3rnaa-uc.a.run.app"
PINS = {
    "rag_release_id": "v1.2-demo-r3",
    "manifest_sha256": "97a8ca8f431f4904e967f0646fc6bb99387c4944dddea2570d816874a4f0478a",
    "serving_revision": "hk-movie-rag-demo-00000-ui-7b1b870",
    "image_digest": "sha256:1230971b4f1acf1ed6e126d30f999d4ec8588d0436cbcfcda19aa65f96b928fd",
    "embedding_model": "gemini-embedding-2",
    "embedding_dimension": 768,
    "generation_model": "gemini-3.5-flash-lite",
    "status": "active",
    "relevance_policy_sha256": "d6db5e118aeea5dc0484f3c591baac666952f78f4d41bfdcdbbb58693f4f6bba",
    "poster_authority_sha256": "c50ff06a88f91296b63bd74a1e4a6485785bdc4c07e73d09b407a126927c8137",
}
REFUSAL_PHRASES = (
    "只能回答", "沒有足夠", "没有足够", "不足以", "欄位不足", "字段不足",
    "無法", "无法", "不支援", "不支持", "未提供", "沒有提供", "没有提供",
    "不知道", "不能確定", "不能确定", "not enough", "cannot answer",
)


def utc():
    return datetime.now(timezone.utc).isoformat()


def normalize(text):
    return re.sub(r"\s+", "", unicodedata.normalize("NFKC", str(text))).lower()


def eligible(movie, constraints):
    for key, value in constraints.items():
        if key == "year_min" and movie.get("release_date", "")[:4] < str(value):
            return False
        if key == "year_max" and movie.get("release_date", "")[:4] > str(value):
            return False
        if key in {"director", "cast", "genre"}:
            if normalize(value) not in normalize(movie.get(key, "")):
                return False
        if key == "tier" and movie.get("tier") != value:
            return False
        if key not in {"year_min", "year_max", "director", "cast", "genre", "tier"}:
            raise ValueError("unknown constraint: " + key)
    return True


def score(case, response, catalog):
    """Conservative mechanical proxies; qualitative review remains necessary."""
    failed = {"pass": False, "valid_metadata_citations": False, "reason": "malformed_response"}
    if not isinstance(response, dict):
        return failed
    answer, citations, movies = (response.get(k) for k in ("answer_markdown", "citations", "movies"))
    if (not isinstance(answer, str) or not answer.strip() or not isinstance(citations, list)
            or not isinstance(movies, list)
            or any(not isinstance(c, dict) for c in citations + movies)):
        return failed
    mids = [m.get("movie_id") for m in movies]
    if any(not isinstance(mid, str) or mid not in catalog for mid in mids):
        return failed | {"reason": "unknown_movie"}
    cids = [c.get("citation_id") for c in citations]
    valid_citations = all(
        isinstance(c.get("movie_id"), str)
        and c["movie_id"] in catalog
        and c.get("source_kind") == "movie_metadata"
        and c.get("citation_id") == catalog[c["movie_id"]]["passage_id"]
        and f'[{c["citation_id"]}]' in answer
        for c in citations
    )
    # Structured citations and inline evidence identifiers must agree exactly.
    inline = set(re.findall(r"\[([^\[\]]+)\]", answer))
    valid_citations = valid_citations and all(isinstance(x, str) for x in cids)
    valid_citations = valid_citations and len(cids) == len(set(map(str, cids))) and inline == set(map(str, cids))
    cited = {c.get("movie_id") for c in citations if isinstance(c.get("movie_id"), str)}
    unique_movies = len(mids) == len(set(mids))
    kind = case["kind"]
    if kind == "fact":
        passed = (valid_citations and case["movie_id"] in cited
                  and cited == {case["movie_id"]}
                  and all(normalize(t) in normalize(answer) for t in case["expected_terms"]))
        reason = "gold_text_and_metadata_identity" if passed else "fact_or_citation_mismatch"
    elif kind == "recommendation":
        passed = (valid_citations and unique_movies and len(mids) == case["count"]
                  and set(mids) == cited
                  and all(eligible(catalog[mid], case["constraints"]) for mid in mids))
        reason = "count_constraints_and_citations" if passed else "constraint_count_or_citation_mismatch"
    elif kind == "refusal":
        refusal = any(normalize(p) in normalize(answer) for p in REFUSAL_PHRASES)
        allowed = {case["movie_id"]} if case.get("movie_id") else set()
        passed = refusal and valid_citations and cited <= allowed and set(mids) <= allowed
        reason = "refusal_proxy_needs_qualitative_review" if passed else "refusal_or_scope_mismatch"
    else:
        raise ValueError("unknown case kind")
    return {"pass": bool(passed), "valid_metadata_citations": bool(valid_citations),
            "citation_count": len(citations), "reason": reason,
            "qualitative_review": "pending"}


def summarize(records, planned):
    successful = [r for r in records if r["http_status"] == 200 and not r.get("error")]
    latency = sorted(r["latency_seconds"] for r in successful)
    groups = {}
    for kind in ("fact", "recommendation", "refusal"):
        group = [r for r in records if r["kind"] == kind]
        groups[kind] = {"attempted": len(group), "proxy_passes": sum(r["score"]["pass"] for r in group)}
    return {
        "status": "completed" if len(records) == planned else "incomplete",
        "planned": planned, "attempted": len(records), "not_attempted": planned - len(records),
        "http_200_valid_json": len(successful), "request_failures": len(records) - len(successful),
        "proxy_passes": sum(r["score"]["pass"] for r in records), "groups": groups,
        "successful_response_latency": ({"median_seconds": statistics.median(latency),
                                         "max_seconds": max(latency)} if latency else None),
        "human_audits": "pending", "qualitative_review": "pending", "submission_ready": False,
        "token_usage": None, "monetary_cost": None,
    }


def create_output(path):
    path.mkdir(parents=True, exist_ok=False)


def save(path, value):
    path.write_text(json.dumps(value, ensure_ascii=False, indent=2) + "\n", encoding="utf-8")


def verify_config(actual, expected):
    if not isinstance(actual, dict) or any(actual.get(k) != v for k, v in expected.items()):
        raise ValueError("service configuration differs from frozen pins")


def http_json(route, payload=None):
    data = None if payload is None else json.dumps(payload, ensure_ascii=False).encode("utf-8")
    req = request.Request(SERVICE + route, data=data, headers={"Content-Type": "application/json"})
    start = time.perf_counter()
    try:
        with request.urlopen(req, timeout=45) as reply:
            status, raw = reply.status, reply.read(1_048_577)
        if len(raw) > 1_048_576:
            return status, None, time.perf_counter() - start, "ResponseTooLarge"
        try:
            result = json.loads(raw)
        except (ValueError, UnicodeDecodeError):
            return status, {"raw_text": raw.decode("utf-8", errors="replace")}, time.perf_counter() - start, "InvalidJSON"
        return status, result, time.perf_counter() - start, None
    except error.HTTPError as exc:
        return exc.code, {"raw_text": exc.read(1_048_576).decode("utf-8", errors="replace")}, time.perf_counter() - start, "HTTPError"
    except (error.URLError, TimeoutError, OSError) as exc:
        return None, None, time.perf_counter() - start, type(exc).__name__


def load_inputs():
    paths = [HERE / "data" / name for name in ("catalog.jsonl", "cases.jsonl")]
    movies, cases = ([json.loads(line) for line in p.read_text(encoding="utf-8").splitlines() if line] for p in paths)
    catalog = {m["movie_id"]: m for m in movies}
    if len(catalog) != 4659 or len(catalog) != len(movies):
        raise ValueError("catalog count or movie identities invalid")
    if len(cases) != 30 or Counter(c["kind"] for c in cases) != {"fact": 10, "recommendation": 10, "refusal": 10}:
        raise ValueError("expected 30 cases in three equal groups")
    if len({c["case_id"] for c in cases}) != 30:
        raise ValueError("duplicate case identity")
    for c in cases:
        if c["kind"] == "fact":
            m = catalog[c["movie_id"]]
            gold = m[c["field"]][:4] if c["field"] == "release_date" else m[c["field"]]
            if any(normalize(t) not in normalize(gold) for t in c["expected_terms"]):
                raise ValueError("fact gold not supported by catalog")
        if c["kind"] == "recommendation":
            actual = sorted(mid for mid, m in catalog.items() if eligible(m, c["constraints"]))
            if actual != c["eligible_movie_ids"] or len(actual) < c["count"]:
                raise ValueError("recommendation gold set invalid")
    pins = json.loads((HERE / "preparation" / "input-pins.json").read_text(encoding="utf-8"))
    hashes = {p.name: hashlib.sha256(p.read_bytes()).hexdigest() for p in paths}
    if hashes != pins["input_sha256"]:
        raise ValueError("evaluation inputs changed after preparation")
    return catalog, cases, hashes


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--execute", action="store_true", help="Send exactly one pass of up to 30 chat requests.")
    parser.add_argument("--output", type=Path)
    args = parser.parse_args()
    catalog, cases, hashes = load_inputs()
    stamp = datetime.now(timezone.utc).strftime("%Y%m%dT%H%M%S%fZ")
    out = args.output or HERE / ("results" if args.execute else "preparation") / stamp
    create_output(out)
    receipt = {"started_at_utc": utc(), "mode": "execute" if args.execute else "GET_only_preflight",
               "service": SERVICE, "input_sha256": hashes, "chat_requests_attempted": 0,
               "human_audits": "pending", "submission_ready": False}
    save(out / "receipt.json", receipt)
    hs, health, _, he = http_json("/health")
    cs, config, _, ce = http_json("/api/config")
    save(out / "health.json", {"http_status": hs, "payload": health, "error": he})
    save(out / "config-before.json", {"http_status": cs, "payload": config, "error": ce})
    try:
        if hs != 200 or he or not isinstance(health, dict) or health.get("status") != "ok" or cs != 200 or ce:
            raise ValueError("preflight unavailable")
        verify_config(config, PINS)
        if config.get("counts", {}).get("movies") != 4659:
            raise ValueError("live catalog count changed")
        if args.execute:
            records, consecutive_failures = [], 0
            for case in cases:
                # Record the attempt before dispatch so interruption never implies no request was sent.
                receipt["chat_requests_attempted"] += 1
                save(out / "receipt.json", receipt)
                status, payload, seconds, failure = http_json("/api/chat", {"question": case["question"], "history": []})
                result = {"case_id": case["case_id"], "kind": case["kind"], "question": case["question"],
                          "http_status": status, "error": failure, "latency_seconds": seconds,
                          "response": payload, "score": score(case, payload, catalog) if status == 200 and not failure
                          else {"pass": False, "reason": "request_failure"}}
                with (out / "responses.jsonl").open("a", encoding="utf-8") as stream:
                    stream.write(json.dumps(result, ensure_ascii=False) + "\n")
                    stream.flush()
                records.append(result)
                save(out / "summary.json", summarize(records, len(cases)))
                consecutive_failures = consecutive_failures + 1 if status != 200 or failure else 0
                if consecutive_failures >= 3:
                    receipt["stop_reason"] = "three_consecutive_request_failures_no_retry"
                    break
            after_status, after, _, after_error = http_json("/api/config")
            save(out / "config-after.json", {"http_status": after_status, "payload": after, "error": after_error})
            if after_status != 200 or after_error or after != config:
                raise ValueError("service configuration changed or final identity check failed")
            receipt["status"] = summarize(records, len(cases))["status"]
        else:
            receipt["status"] = "preflight_passed_formal_evaluation_not_started"
        receipt["finished_at_utc"] = utc()
        save(out / "receipt.json", receipt)
        print(json.dumps({"output": str(out), "status": receipt["status"], "chat_requests_attempted": receipt["chat_requests_attempted"]}))
        return 0 if "stop_reason" not in receipt else 2
    except (ValueError, OSError, KeyboardInterrupt) as exc:
        receipt.update(status="interrupted" if isinstance(exc, KeyboardInterrupt) else "failed", error_type=type(exc).__name__, finished_at_utc=utc())
        save(out / "receipt.json", receipt)
        print(json.dumps({"output": str(out), "status": receipt["status"], "error_type": receipt["error_type"]}))
        return 2


if __name__ == "__main__":
    raise SystemExit(main())
