"""Read-only evaluator for the deterministic domain gate and observed relevance distances."""

from __future__ import annotations

import argparse
import json
import math
from collections.abc import Sequence
from pathlib import Path

from .relevance import (
    RelevancePolicyError,
    has_static_movie_domain_signal,
    load_general_relevance_policy,
)

_CASE_KEYS = frozenset({"case_id", "split", "expected_domain", "question"})
_OBSERVATION_KEYS = frozenset({"case_id", "distances"})


def evaluate_general_relevance(
    golden_path: Path,
    *,
    live: bool,
    observations_path: Path | None = None,
) -> dict[str, object]:
    """Evaluate without network calls and return only case IDs, decisions, and distances."""
    if observations_path is not None and not live:
        raise RelevancePolicyError("explicit live mode is required for observations")
    if live and observations_path is None:
        raise RelevancePolicyError("live observations are required")
    policy = load_general_relevance_policy()
    try:
        golden_bytes = golden_path.read_bytes()
    except OSError:
        raise RelevancePolicyError("general relevance evaluation input is invalid") from None
    if policy.sha256_bytes(golden_bytes) != policy.golden_sha256:
        raise RelevancePolicyError("general relevance evaluation input is invalid")
    cases = _parse_jsonl(golden_bytes, expected_keys=_CASE_KEYS)
    if len(cases) != 40 or len({case["case_id"] for case in cases}) != 40:
        raise RelevancePolicyError("general relevance evaluation input is invalid")

    observations = (
        _load_observations(observations_path, policy.distance_cutoff)
        if observations_path is not None
        else {}
    )
    report_cases: list[dict[str, object]] = []
    movie_allowed = 0
    ood_rejected = 0
    status = "pass"
    for case in cases:
        case_id = case["case_id"]
        expected_domain = case["expected_domain"]
        question = case["question"]
        if (
            not isinstance(case_id, str)
            or not case_id
            or not isinstance(expected_domain, str)
            or not expected_domain
            or not isinstance(question, str)
            or not question
        ):
            raise RelevancePolicyError("general relevance evaluation input is invalid")
        if case["split"] not in {"calibration", "holdout"} or expected_domain not in {
            "movie",
            "ood",
        }:
            raise RelevancePolicyError("general relevance evaluation input is invalid")
        allowed = has_static_movie_domain_signal(question)
        expected_allowed = expected_domain == "movie"
        if allowed == expected_allowed:
            movie_allowed += int(expected_allowed)
            ood_rejected += int(not expected_allowed)
        else:
            status = "fail"
        item: dict[str, object] = {
            "case_id": case_id,
            "decision": "allow" if allowed else "reject",
            "expected_domain": expected_domain,
        }
        if live:
            distances = observations.get(case_id, ())
            if expected_allowed and not distances:
                status = "fail"
            # Historical calibration can intentionally probe embeddings before the
            # production domain gate.  Those OOD distances are diagnostic only;
            # the runtime decision above still rejects the case before retrieval.
            item["pre_gate_observed_distances"] = list(distances)
        report_cases.append(item)

    if live and set(observations) - {str(case["case_id"]) for case in cases}:
        raise RelevancePolicyError("general relevance evaluation input is invalid")
    return {
        "schema_version": "general-relevance-evaluation/v1",
        "status": status,
        "mode": "live-observations" if live else "offline",
        "policy_sha256": policy.artifact_sha256,
        "golden_sha256": policy.golden_sha256,
        "case_count": len(cases),
        "movie_allowed": movie_allowed,
        "ood_rejected": ood_rejected,
        "cases": report_cases,
    }


def _parse_jsonl(raw: bytes, *, expected_keys: frozenset[str]) -> list[dict[str, object]]:
    try:
        text = raw.decode("utf-8")
        if not text.endswith("\n"):
            raise ValueError
        result: list[dict[str, object]] = []
        for line in text.splitlines():
            value = json.loads(line)
            if not isinstance(value, dict) or set(value) != expected_keys:
                raise ValueError
            canonical = json.dumps(
                value, ensure_ascii=False, sort_keys=True, separators=(",", ":")
            )
            if line != canonical:
                raise ValueError
            result.append(value)
        return result
    except (UnicodeDecodeError, json.JSONDecodeError, ValueError, TypeError):
        raise RelevancePolicyError("general relevance evaluation input is invalid") from None


def _load_observations(path: Path, cutoff: float) -> dict[str, tuple[float, ...]]:
    try:
        values = _parse_jsonl(path.read_bytes(), expected_keys=_OBSERVATION_KEYS)
    except OSError:
        raise RelevancePolicyError("general relevance evaluation input is invalid") from None
    observations: dict[str, tuple[float, ...]] = {}
    for value in values:
        case_id = value["case_id"]
        raw_distances = value["distances"]
        if (
            not isinstance(case_id, str)
            or not case_id
            or case_id in observations
            or isinstance(raw_distances, (str, bytes))
            or not isinstance(raw_distances, Sequence)
        ):
            raise RelevancePolicyError("general relevance evaluation input is invalid")
        distances: list[float] = []
        for raw_distance in raw_distances:
            if (
                isinstance(raw_distance, bool)
                or not isinstance(raw_distance, (int, float))
                or not math.isfinite(float(raw_distance))
                or float(raw_distance) < 0
                or float(raw_distance) > 2
            ):
                raise RelevancePolicyError("general relevance evaluation input is invalid")
            distance = float(raw_distance)
            if distance <= cutoff:
                distances.append(distance)
        observations[case_id] = tuple(distances)
    return observations


def main(argv: Sequence[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description="Evaluate the immutable general relevance policy")
    parser.add_argument("--golden", type=Path, required=True)
    parser.add_argument("--live", action="store_true")
    parser.add_argument("--observations", type=Path)
    args = parser.parse_args(argv)
    try:
        report = evaluate_general_relevance(
            args.golden,
            live=args.live,
            observations_path=args.observations,
        )
    except RelevancePolicyError:
        print('{"schema_version":"general-relevance-evaluation/v1","status":"error"}')
        return 2
    print(json.dumps(report, ensure_ascii=False, sort_keys=True, separators=(",", ":")))
    return 0 if report["status"] == "pass" else 1


if __name__ == "__main__":  # pragma: no cover - exercised through main()
    raise SystemExit(main())
