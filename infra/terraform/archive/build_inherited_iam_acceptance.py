"""Build the governed inherited-IAM acceptance contract from saved JSON evidence.

This helper is deliberately offline: it reads three explicit files and writes the
candidate contract to stdout. It never invokes gcloud or writes an output file.
"""

from __future__ import annotations

import argparse
import hashlib
import json
import sys
from pathlib import Path
from typing import Any

PROJECT_ID = "motionexpaiweb"
PROJECT_NUMBER = "880586285913"
ORGANIZATION_ID = "797368190621"
RATIONALE = [
    "Existing application service accounts are accepted only through the exact reviewed snapshots.",
    "Broad user instruction accepts the current security posture captured by these snapshots.",
    "No IAM removal is authorized by this bootstrap.",
]


class ContractError(ValueError):
    """Raised when input evidence cannot form the governed contract."""


def _reject_duplicate_keys(pairs: list[tuple[str, Any]]) -> dict[str, Any]:
    value: dict[str, Any] = {}
    for key, item in pairs:
        if key in value:
            raise ContractError(f"duplicate JSON property: {key}")
        value[key] = item
    return value


def _read_json(path: Path, context: str) -> Any:
    try:
        return json.loads(
            path.read_text(encoding="utf-8-sig"),
            object_pairs_hook=_reject_duplicate_keys,
        )
    except (OSError, UnicodeError, json.JSONDecodeError) as exc:
        raise ContractError(f"could not read {context} JSON: {exc}") from exc


def _require_object(value: Any, context: str) -> dict[str, Any]:
    if not isinstance(value, dict):
        raise ContractError(f"{context} must be a JSON object")
    return value


def _require_nonempty_string(value: Any, context: str) -> str:
    if not isinstance(value, str) or not value.strip():
        raise ContractError(f"{context} must be a non-empty string")
    return value


def _validate_project_description(value: Any) -> None:
    project = _require_object(value, "project description")
    expected = {
        "projectId": PROJECT_ID,
        "projectNumber": PROJECT_NUMBER,
        "lifecycleState": "ACTIVE",
    }
    for field, expected_value in expected.items():
        if str(project.get(field, "")) != expected_value:
            raise ContractError(
                f"project description {field} must be exactly {expected_value}"
            )
    parent = _require_object(project.get("parent"), "project description parent")
    if parent.get("type") != "organization" or str(parent.get("id", "")) != ORGANIZATION_ID:
        raise ContractError(
            "project description parent must be organization " + ORGANIZATION_ID
        )


def _canonical_policy(value: Any, context: str) -> dict[str, Any]:
    policy = _require_object(value, context)
    unknown = set(policy) - {"bindings", "version", "etag"}
    if unknown:
        raise ContractError(
            f"{context} contains unexpected top-level field {min(unknown)!r}"
        )
    bindings_value = policy.get("bindings")
    if not isinstance(bindings_value, list):
        raise ContractError(f"{context} bindings must be an array")
    version = policy.get("version", 1)
    if isinstance(version, bool) or not isinstance(version, int) or version not in {1, 3}:
        raise ContractError(f"{context} has unsupported IAM policy version {version!r}")

    canonical_bindings: list[dict[str, Any]] = []
    seen_bindings: set[str] = set()
    for index, raw_binding in enumerate(bindings_value):
        binding_context = f"{context} binding {index}"
        binding = _require_object(raw_binding, binding_context)
        unknown = set(binding) - {"role", "members", "condition"}
        if unknown:
            raise ContractError(
                f"{binding_context} contains unexpected field {min(unknown)!r}"
            )
        role = _require_nonempty_string(binding.get("role"), f"{binding_context} role")
        members_value = binding.get("members")
        if not isinstance(members_value, list) or not members_value:
            raise ContractError(f"{binding_context} members must be a non-empty array")
        members = sorted(
            _require_nonempty_string(member, f"{binding_context} member")
            for member in members_value
        )
        if len(members) != len(set(members)):
            raise ContractError(f"{binding_context} contains duplicate members")

        canonical_binding: dict[str, Any] = {"role": role, "members": members}
        if "condition" in binding:
            condition = _require_object(
                binding["condition"], f"{binding_context} condition"
            )
            unknown = set(condition) - {"title", "expression", "description"}
            if unknown:
                raise ContractError(
                    f"{binding_context} condition contains unexpected field "
                    f"{min(unknown)!r}"
                )
            canonical_condition = {
                "title": _require_nonempty_string(
                    condition.get("title"), f"{binding_context} condition title"
                ),
                "expression": _require_nonempty_string(
                    condition.get("expression"),
                    f"{binding_context} condition expression",
                ),
            }
            if "description" in condition:
                if not isinstance(condition["description"], str):
                    raise ContractError(
                        f"{binding_context} condition description must be a string"
                    )
                canonical_condition["description"] = condition["description"]
            canonical_binding["condition"] = canonical_condition

        compact = json.dumps(
            canonical_binding, ensure_ascii=False, separators=(",", ":")
        )
        if compact in seen_bindings:
            raise ContractError(f"{context} contains duplicate bindings")
        seen_bindings.add(compact)
        canonical_bindings.append(canonical_binding)

    canonical_bindings.sort(
        key=lambda binding: json.dumps(
            binding, ensure_ascii=False, separators=(",", ":")
        )
    )
    return {"version": version, "bindings": canonical_bindings}


def _policy_record(policy: dict[str, Any]) -> dict[str, Any]:
    compact = json.dumps(policy, ensure_ascii=False, separators=(",", ":"))
    return {
        "sha256": hashlib.sha256(compact.encode("utf-8")).hexdigest(),
        "policy": policy,
    }


def _build_contract(
    project_description: Any,
    project_policy_value: Any,
    organization_policy_value: Any,
) -> dict[str, Any]:
    _validate_project_description(project_description)
    project_policy = _canonical_policy(project_policy_value, "project IAM policy")
    organization_policy = _canonical_policy(
        organization_policy_value, "organization IAM policy"
    )
    accepted_principals = sorted(
        {
            member
            for policy in (project_policy, organization_policy)
            for binding in policy["bindings"]
            for member in binding["members"]
        }
    )
    return {
        "schema_version": 1,
        "status": "accepted",
        "project": {
            "id": PROJECT_ID,
            "number": PROJECT_NUMBER,
            "parent": {"type": "organization", "id": ORGANIZATION_ID},
        },
        "accepted_principals": accepted_principals,
        "rationale": RATIONALE,
        "project_policy": _policy_record(project_policy),
        "organization_policy": _policy_record(organization_policy),
    }


def _parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(
        description="Build an inherited-IAM acceptance candidate from saved JSON."
    )
    parser.add_argument("--project-description", type=Path, required=True)
    parser.add_argument("--project-policy", type=Path, required=True)
    parser.add_argument("--organization-policy", type=Path, required=True)
    return parser


def main(argv: list[str] | None = None) -> int:
    args = _parser().parse_args(argv)
    try:
        contract = _build_contract(
            _read_json(args.project_description, "project description"),
            _read_json(args.project_policy, "project IAM policy"),
            _read_json(args.organization_policy, "organization IAM policy"),
        )
    except ContractError as exc:
        print(f"acceptance contract rejected: {exc}", file=sys.stderr)
        return 2
    print(json.dumps(contract, ensure_ascii=False, indent=2))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
