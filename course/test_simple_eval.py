"""Offline checks of evaluation integrity; never calls the live service."""
import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch

import run_simple_eval as ev


CATALOG = {
    "M1": {"movie_id": "M1", "passage_id": "metadata:M1", "director": "成龍",
           "genre": "動作、喜劇", "release_date": "1985-12-14", "tier": "S"},
    "M2": {"movie_id": "M2", "passage_id": "metadata:M2", "director": "吳宇森",
           "genre": "動作", "release_date": "1986-01-01", "tier": "A"},
}


def citation(mid="M1"):
    return {"movie_id": mid, "source_kind": "movie_metadata",
            "citation_id": "metadata:" + mid}


def response(answer="成龍[metadata:M1]", mids=("M1",)):
    return {"answer_markdown": answer, "citations": [citation(x) for x in mids],
            "movies": [{"movie_id": x} for x in mids]}


class EvaluationIntegrity(unittest.TestCase):
    def test_fact_requires_gold_text_and_correct_source(self):
        case = {"kind": "fact", "movie_id": "M1", "expected_terms": ["成龍"]}
        self.assertTrue(ev.score(case, response(), CATALOG)["pass"])
        self.assertFalse(ev.score(case, response(mids=("M2",)), CATALOG)["pass"])
        self.assertFalse(ev.score(case, response("吳宇森[metadata:M1]"), CATALOG)["pass"])

    def test_unknown_or_unmarked_citations_fail(self):
        case = {"kind": "fact", "movie_id": "M1", "expected_terms": ["成龍"]}
        self.assertFalse(ev.score(case, response("成龍"), CATALOG)["pass"])
        self.assertFalse(ev.score(case, response("成龍[metadata:BAD]", ("BAD",)), CATALOG)["pass"])

    def test_pdf_citation_is_outside_evaluation_scope(self):
        payload = response()
        payload["citations"][0]["source_kind"] = "pdf_page"
        self.assertFalse(ev.score({"kind": "fact", "movie_id": "M1",
                                   "expected_terms": ["成龍"]}, payload, CATALOG)["pass"])

    def test_recommendation_checks_actual_catalog_not_returned_fields(self):
        case = {"kind": "recommendation", "count": 1, "constraints": {"director": "成龍"}}
        self.assertTrue(ev.score(case, response(), CATALOG)["pass"])
        fake = response("推薦[metadata:M2]", ("M2",))
        fake["movies"][0]["director"] = "成龍"
        self.assertFalse(ev.score(case, fake, CATALOG)["pass"])
        self.assertFalse(ev.score(case, response(mids=("M1", "M1")), CATALOG)["pass"])

    def test_refusal_phrase_alone_with_unrelated_answer_is_not_enough(self):
        case = {"kind": "refusal", "subtype": "out_of_domain"}
        self.assertTrue(ev.score(case, response("目前沒有足夠的檢索證據回答這個問題。", ()), CATALOG)["pass"])
        self.assertFalse(ev.score(case, response("無法回答；成龍[metadata:M1]"), CATALOG)["pass"])

    def test_malformed_response_is_recorded_as_failure(self):
        self.assertFalse(ev.score({"kind": "refusal", "subtype": "out_of_domain"}, {}, CATALOG)["pass"])
        self.assertFalse(ev.score({"kind": "fact", "movie_id": "M1", "expected_terms": ["成龍"]},
                                  {"answer_markdown": "成龍", "citations": [None], "movies": []}, CATALOG)["pass"])

    def test_partial_run_keeps_planned_denominator_and_skips_latency_claims(self):
        result = ev.summarize([{"kind": "fact", "http_status": 503, "latency_seconds": 1.0,
                                "score": {"pass": False}}], planned=30)
        self.assertEqual(result["planned"], 30)
        self.assertEqual(result["attempted"], 1)
        self.assertEqual(result["not_attempted"], 29)
        self.assertEqual(result["status"], "incomplete")
        self.assertIsNone(result["successful_response_latency"])

    def test_no_overwrite_of_previous_outputs(self):
        with tempfile.TemporaryDirectory() as tmp:
            path = Path(tmp) / "results"
            ev.create_output(path)
            with self.assertRaises(FileExistsError):
                ev.create_output(path)

    def test_pin_change_rejected(self):
        with self.assertRaises(ValueError):
            ev.verify_config({"manifest_sha256": "wrong"}, {"manifest_sha256": "right"})

    def test_default_preflight_never_calls_chat(self):
        config = ev.PINS | {"counts": {"movies": 4659}}
        calls = []
        def transport(route, payload=None):
            calls.append((route, payload))
            return 200, {"status": "ok"} if route == "/health" else config, 0.01, None
        with tempfile.TemporaryDirectory() as tmp:
            with patch.object(ev, "load_inputs", return_value=(CATALOG, [], {})), \
                    patch.object(ev, "http_json", side_effect=transport), \
                    patch("sys.argv", ["eval", "--output", str(Path(tmp) / "preflight")]):
                self.assertEqual(ev.main(), 0)
        self.assertEqual(calls, [("/health", None), ("/api/config", None)])

    def test_three_shared_failures_stop_without_retry(self):
        config = ev.PINS | {"counts": {"movies": 4659}}
        calls = []
        def transport(route, payload=None):
            calls.append(route)
            if route == "/api/chat":
                return 503, {"detail": "unavailable"}, 0.01, "HTTPError"
            return 200, {"status": "ok"} if route == "/health" else config, 0.01, None
        cases = [{"case_id": str(i), "kind": "fact", "question": "test"} for i in range(30)]
        with tempfile.TemporaryDirectory() as tmp:
            out = Path(tmp) / "results"
            with patch.object(ev, "load_inputs", return_value=(CATALOG, cases, {})), \
                    patch.object(ev, "http_json", side_effect=transport), \
                    patch("sys.argv", ["eval", "--execute", "--output", str(out)]):
                self.assertEqual(ev.main(), 2)
            import json
            summary = json.loads((out / "summary.json").read_text())
            self.assertEqual(summary["not_attempted"], 27)
            self.assertEqual(summary["status"], "incomplete")
        self.assertEqual(calls.count("/api/chat"), 3)


if __name__ == "__main__":
    unittest.main()
