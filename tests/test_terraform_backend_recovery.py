from __future__ import annotations

import json
import os
import shutil
import subprocess
import sys
from copy import deepcopy
from pathlib import Path
from typing import Any

import pytest
from test_terraform_backend_bootstrap import (
    ACCOUNT,
    ARCHIVE_DIR,
    BACKEND_BUCKET,
    BACKEND_URI,
    ORGANIZATION_ID,
    PROJECT_ID,
    _accepted_inherited_iam,
    _canonical_policy,
    _organization_iam,
    _policy_digest,
    _project_iam,
    _target_iam,
    _valid_bucket,
    _valid_preflight,
)

RECOVERY_SCRIPT = ARCHIVE_DIR / "recover_backend_iam.ps1"
TEMP_TITLE = "hk-rag-backend-iam-recovery"
TEMP_DESCRIPTION = "Temporary bucket-scoped access for approved backend IAM recovery."
TEMP_EXPRESSION = (
    'resource.type == "storage.googleapis.com/Bucket" && '
    f'resource.name == "projects/_/buckets/{BACKEND_BUCKET}"'
)
ARCHIVE_BUCKET = "motionexpaiweb-880586285913-hk-movie-rag-source-archive"
ARCHIVE_URI = f"gs://{ARCHIVE_BUCKET}"
ARCHIVE_WRITER = (
    "serviceAccount:hk-rag-archive-writer@motionexpaiweb.iam.gserviceaccount.com"
)
ARCHIVE_OPERATOR_TITLE = "hk-rag-archive-operator"
ARCHIVE_OPERATOR_DESCRIPTION = (
    "Persistent bucket-scoped management access for the HK Movie RAG source archive."
)
ARCHIVE_OPERATOR_EXPRESSION = (
    'resource.type == "storage.googleapis.com/Bucket" && '
    f'resource.name == "projects/_/buckets/{ARCHIVE_BUCKET}"'
)


def _lockout_iam(*, etag: str = "lockout-etag") -> dict[str, Any]:
    return {
        "bindings": [
            {
                "role": "roles/storage.objectAdmin",
                "members": [f"user:{ACCOUNT}"],
            }
        ],
        "etag": etag,
        "version": 1,
    }


def _temporary_binding() -> dict[str, Any]:
    return {
        "role": "roles/storage.admin",
        "members": [f"user:{ACCOUNT}"],
        "condition": {
            "title": TEMP_TITLE,
            "description": TEMP_DESCRIPTION,
            "expression": TEMP_EXPRESSION,
        },
    }


def _temporary_project_iam(*, etag: str = "temporary-etag") -> dict[str, Any]:
    policy = deepcopy(_project_iam())
    policy["bindings"].append(_temporary_binding())
    policy["etag"] = etag
    policy["version"] = 3
    return policy


def _archive_operator_binding() -> dict[str, Any]:
    return {
        "role": "roles/storage.admin",
        "members": [f"user:{ACCOUNT}"],
        "condition": {
            "title": ARCHIVE_OPERATOR_TITLE,
            "description": ARCHIVE_OPERATOR_DESCRIPTION,
            "expression": ARCHIVE_OPERATOR_EXPRESSION,
        },
    }


def _accepted_with_archive_operator() -> dict[str, Any]:
    acceptance = deepcopy(_accepted_inherited_iam())
    project_policy = deepcopy(_project_iam())
    project_policy["bindings"].append(_archive_operator_binding())
    project_policy["version"] = 3
    canonical = _canonical_policy(project_policy)
    acceptance["project_policy"] = {
        "sha256": _policy_digest(project_policy),
        "policy": canonical,
    }
    return acceptance


def _archive_bucket() -> dict[str, Any]:
    return {
        "name": ARCHIVE_BUCKET,
        "projectNumber": "880586285913",
        "location": "US-CENTRAL1",
        "storageClass": "STANDARD",
        "iamConfiguration": {
            "uniformBucketLevelAccess": {"enabled": True},
            "publicAccessPrevention": "enforced",
        },
        "softDeletePolicy": {"retentionDurationSeconds": "604800"},
        "versioning": {"enabled": True},
        "labels": {"goog-terraform-provisioned": "true"},
    }


def _archive_iam() -> dict[str, Any]:
    return {
        "bindings": [
            {"role": "roles/storage.objectCreator", "members": [ARCHIVE_WRITER]},
            {"role": "roles/storage.objectViewer", "members": [ARCHIVE_WRITER]},
        ],
        "etag": "archive-etag",
        "version": 1,
    }


def _valid_state() -> dict[str, Any]:
    return {
        "gcloud_version": "531.0.0",
        "account": ACCOUNT,
        "configured_account": ACCOUNT,
        "configured_project": PROJECT_ID,
        "project": {
            "projectId": PROJECT_ID,
            "projectNumber": "880586285913",
            "lifecycleState": "ACTIVE",
            "parent": {"type": "organization", "id": ORGANIZATION_ID},
        },
        "billing": {"billingEnabled": True, "projectId": PROJECT_ID},
        "project_iam": _project_iam(),
        "organization_iam": _organization_iam(),
        "bucket_status": "forbidden",
        "bucket": _valid_bucket(),
        "iam": _lockout_iam(),
        "archive_bucket_status": "forbidden",
        "archive_bucket": _archive_bucket(),
        "archive_iam": _archive_iam(),
        "project_policy_writes": [],
        "bucket_policy_writes": [],
        "file_probes": [],
    }


def _unconditional_recovery_state() -> tuple[dict[str, Any], dict[str, Any]]:
    acceptance = json.loads(
        (ARCHIVE_DIR / "inherited_iam_acceptance.json").read_text(encoding="utf-8")
    )
    accepted_bindings = deepcopy(acceptance["project_policy"]["policy"]["bindings"])
    state = _valid_state()
    state["project_iam"] = {
        "bindings": accepted_bindings + [_temporary_binding()],
        "etag": "temporary-etag",
        "version": 3,
    }
    state["organization_iam"] = {
        "bindings": deepcopy(
            acceptance["organization_policy"]["policy"]["bindings"]
        ),
        "etag": "live-organization-etag",
        "version": acceptance["organization_policy"]["policy"]["version"],
    }
    state["bucket_status"] = "exists"
    state["iam"] = _target_iam()
    return state, acceptance


FAKE_GCLOUD = r'''
import json
import os
from pathlib import Path
import sys

state_path = Path(os.environ["FAKE_GCLOUD_STATE"])
log_path = Path(os.environ["FAKE_GCLOUD_LOG"])
state = json.loads(state_path.read_text(encoding="utf-8"))
args = sys.argv[1:]
backend_bucket = "motionexpaiweb-880586285913-hk-movie-rag-tfstate"
archive_bucket = "motionexpaiweb-880586285913-hk-movie-rag-source-archive"
with log_path.open("a", encoding="utf-8") as handle:
    handle.write(json.dumps(args) + "\n")

def persist():
    state_path.write_text(json.dumps(state), encoding="utf-8")

def probe_file(path, kind):
    try:
        with path.open("a", encoding="utf-8"):
            pass
        write_blocked = False
    except PermissionError:
        write_blocked = True
    try:
        path.unlink()
        delete_blocked = False
    except PermissionError:
        delete_blocked = True
    state["file_probes"].append({
        "kind": kind,
        "path": str(path),
        "write_blocked": write_blocked,
        "delete_blocked": delete_blocked,
    })

def permission_denied(command, permission, bucket=backend_bucket):
    return (
        f"ERROR: (gcloud.storage.buckets.{command}) [admin@motionexp.com] does not "
        "have permission to access b instance "
        f"[{bucket}] (or it may not exist): "
        f"admin@motionexp.com does not have {permission} access to the Google Cloud "
        f"Storage bucket. Permission '{permission}' denied on resource "
        "'//storage.googleapis.com/projects/_/buckets/"
        f"{bucket}' (or it may not exist). "
        "Remediate access with this Troubleshooter URL or share it with your "
        "administrator - https://console.cloud.google.com/iam-admin/troubleshooter/"
        "summary;errorId=opaque-test-id . This command is authenticated as "
        "admin@motionexp.com which is the active account specified by the "
        "[core/account] property."
    )

authenticated_prefixes = [
    ["projects", "describe"],
    ["billing", "projects", "describe"],
    ["projects", "get-iam-policy"],
    ["projects", "set-iam-policy"],
    ["organizations", "get-iam-policy"],
    ["storage", "buckets", "describe"],
    ["storage", "buckets", "get-iam-policy"],
    ["storage", "buckets", "set-iam-policy"],
]
if state.get("require_explicit_account") and any(
    args[:len(prefix)] == prefix for prefix in authenticated_prefixes
):
    if "--account=admin@motionexp.com" not in args:
        print("ERROR: authenticated command omitted the explicit account", file=sys.stderr)
        raise SystemExit(1)

if args == ["version", "--format=json"]:
    print(json.dumps({"Google Cloud SDK": state["gcloud_version"]}))
elif args[:2] == ["auth", "list"]:
    print(json.dumps([{"account": state["account"], "status": "ACTIVE"}]))
elif args[:2] == ["config", "list"]:
    print(json.dumps({"core": {
        "account": state["configured_account"],
        "project": state["configured_project"],
    }}))
    if state.get("flip_config_after_list"):
        state["configured_account"] = "other@example.com"
        state["configured_project"] = "wrong-project"
        persist()
elif args[:2] == ["projects", "describe"]:
    print(json.dumps(state["project"]))
elif args[:3] == ["billing", "projects", "describe"]:
    print(json.dumps(state["billing"]))
elif args[:2] == ["projects", "get-iam-policy"]:
    state["project_get_count"] = state.get("project_get_count", 0) + 1
    persist()
    if state.get("project_drift_on_get_count") == state["project_get_count"]:
        state["project_iam"]["bindings"].append({
            "role": "roles/viewer", "members": ["user:concurrent@example.com"]
        })
        state["project_iam"]["etag"] = "concurrent-project-etag"
        persist()
    pending = state.get("pending_project_iam")
    remaining = state.get("project_visibility_delay", 0)
    if pending is not None and remaining > 0:
        state["project_visibility_delay"] = remaining - 1
        persist()
    elif pending is not None:
        state["project_iam"] = pending
        state["pending_project_iam"] = None
        persist()
    print(json.dumps(state["project_iam"]))
elif args[:2] == ["projects", "set-iam-policy"]:
    policy_path = Path(args[3])
    policy = json.loads(policy_path.read_text(encoding="utf-8-sig"))
    probe_file(policy_path, "project-policy")
    state["project_policy_writes"].append({"path": str(policy_path), "policy": policy})
    if policy.get("etag") != state["project_iam"].get("etag"):
        persist()
        print("ERROR: project IAM etag precondition failed", file=sys.stderr)
        raise SystemExit(1)
    result = json.loads(json.dumps(policy))
    result["etag"] = "project-etag-after-set"
    if state.get("unexpected_project_after_write"):
        result["bindings"].append({
            "role": "roles/viewer", "members": ["user:outsider@example.com"]
        })
    if state.get("lossy_project_after_write"):
        temporary = next(
            binding
            for binding in result["bindings"]
            if binding.get("condition", {}).get("title")
            == "hk-rag-backend-iam-recovery"
        )
        temporary["role"] = "roles/storage.admin_withcond_deadbeef"
        temporary.pop("condition")
    stored_result = json.loads(json.dumps(result))
    if (
        not state.get("preserve_unconditional_project_version_after_write")
        and not any("condition" in binding for binding in stored_result["bindings"])
    ):
        stored_result["version"] = 1
    delay = state.get("next_project_visibility_delay", 0)
    if delay:
        state["pending_project_iam"] = stored_result
        state["project_visibility_delay"] = delay
    else:
        state["project_iam"] = stored_result
    if any(binding.get("condition", {}).get("title") == "hk-rag-backend-iam-recovery"
           for binding in result["bindings"]):
        delay = state.get("next_bucket_access_delay", 0)
        if delay:
            state["bucket_access_delay"] = delay
        else:
            state["bucket_status"] = "exists"
    if any(binding.get("condition", {}).get("title") == "hk-rag-archive-operator"
           for binding in result["bindings"]):
        delay = state.get("next_archive_access_delay", 0)
        if delay:
            state["archive_access_delay"] = delay
        else:
            state["archive_bucket_status"] = "exists"
    persist()
    print(json.dumps(result))
elif args[:2] == ["organizations", "get-iam-policy"]:
    print(json.dumps(state["organization_iam"]))
    if state.get("make_bucket_accessible_after_organization_read"):
        state["bucket_status"] = "exists"
        persist()
elif args[:3] == ["storage", "buckets", "describe"]:
    requested_bucket = args[3].removeprefix("gs://")
    if requested_bucket == archive_bucket:
        if state["archive_bucket_status"] == "forbidden":
            if "archive_access_delay" in state and state["archive_access_delay"] == 0:
                state["archive_bucket_status"] = "exists"
                persist()
            else:
                if "archive_access_delay" in state:
                    state["archive_access_delay"] -= 1
                    persist()
                print(
                    state.get(
                        "archive_bucket_permission_error",
                        permission_denied(
                            "describe", "storage.buckets.get", archive_bucket
                        ),
                    ),
                    file=sys.stderr,
                )
                raise SystemExit(1)
        print(json.dumps(state["archive_bucket"]))
    elif state["bucket_status"] == "forbidden":
        if "bucket_access_delay" in state and state["bucket_access_delay"] == 0:
            state["bucket_status"] = "exists"
            persist()
        else:
            if "bucket_access_delay" in state:
                state["bucket_access_delay"] -= 1
                persist()
            print(
                state.get(
                    "bucket_permission_error",
                    permission_denied("describe", "storage.buckets.get"),
                ),
                file=sys.stderr,
            )
            raise SystemExit(1)
        print(json.dumps(state["bucket"]))
    else:
        print(json.dumps(state["bucket"]))
elif args[:3] == ["storage", "buckets", "get-iam-policy"]:
    requested_bucket = args[3].removeprefix("gs://")
    if requested_bucket == archive_bucket:
        if state["archive_bucket_status"] == "forbidden":
            print(
                state.get(
                    "archive_iam_permission_error",
                    permission_denied(
                        "get-iam-policy",
                        "storage.buckets.getIamPolicy",
                        archive_bucket,
                    ),
                ),
                file=sys.stderr,
            )
            raise SystemExit(1)
        if state.get("project_drift_on_archive_iam_read"):
            state["project_iam"]["bindings"].append(
                state["project_drift_on_archive_iam_read"]
            )
            state["project_iam"]["etag"] = "project-etag-after-archive-read"
            state["project_drift_on_archive_iam_read"] = None
            persist()
        if state.get("organization_drift_on_archive_iam_read"):
            state["organization_iam"]["bindings"].append(
                state["organization_drift_on_archive_iam_read"]
            )
            state["organization_iam"]["etag"] = "organization-etag-after-archive-read"
            state["organization_drift_on_archive_iam_read"] = None
            persist()
        print(json.dumps(state["archive_iam"]))
    else:
        if (
            state.get("project_drift_on_bucket_iam_read")
            and not state.get("project_drift_on_bucket_iam_read_applied")
        ):
            state["project_iam"]["bindings"].append(
                state["project_drift_on_bucket_iam_read"]
            )
            state["project_iam"]["etag"] = "etag-after-bucket-verification"
            state["project_drift_on_bucket_iam_read_applied"] = True
            persist()
        remaining_permission = state.get("bucket_iam_access_delay", 0)
        if remaining_permission > 0:
            state["bucket_iam_access_delay"] = remaining_permission - 1
            persist()
            print(
                state.get(
                    "bucket_iam_permission_error",
                    permission_denied("get-iam-policy", "storage.buckets.getIamPolicy"),
                ),
                file=sys.stderr,
            )
            raise SystemExit(1)
        pending = state.get("pending_bucket_iam")
        remaining = state.get("bucket_visibility_delay", 0)
        if pending is not None and remaining > 0:
            state["bucket_visibility_delay"] = remaining - 1
            persist()
        elif pending is not None:
            state["iam"] = pending
            state["pending_bucket_iam"] = None
            persist()
        print(json.dumps(state["iam"]))
elif args[:3] == ["storage", "buckets", "set-iam-policy"]:
    policy_path = Path(args[4])
    policy = json.loads(policy_path.read_text(encoding="utf-8-sig"))
    probe_file(policy_path, "bucket-policy")
    requested_etag = next(
        (arg.removeprefix("--etag=") for arg in args if arg.startswith("--etag=")), None
    )
    state["bucket_policy_writes"].append({
        "etag": requested_etag, "path": str(policy_path), "policy": policy
    })
    if requested_etag != state["iam"].get("etag"):
        persist()
        print("ERROR: bucket IAM etag precondition failed", file=sys.stderr)
        raise SystemExit(1)
    result = {**policy, "etag": "bucket-etag-after-set"}
    if state.get("unexpected_bucket_after_write"):
        result["bindings"].append({
            "role": "roles/storage.objectViewer",
            "members": ["user:outsider@example.com"],
        })
    delay = state.get("next_bucket_visibility_delay", 0)
    if delay:
        state["pending_bucket_iam"] = result
        state["bucket_visibility_delay"] = delay
    else:
        state["iam"] = result
    persist()
    print(json.dumps(result))
else:
    print(f"unexpected fake gcloud argv: {args!r}", file=sys.stderr)
    raise SystemExit(2)
'''


def _run_recovery(
    tmp_path: Path,
    *,
    mode: str = "Check",
    state: dict[str, Any] | None = None,
    preflight: dict[str, Any] | None = None,
    acceptance: dict[str, Any] | None = None,
) -> tuple[subprocess.CompletedProcess[str], list[list[str]], dict[str, Any]]:
    assert RECOVERY_SCRIPT.exists(), "dedicated recovery script is missing"
    powershell = shutil.which("pwsh") or shutil.which("powershell")
    assert powershell is not None

    preflight_path = tmp_path / "preflight.json"
    state_path = tmp_path / "state.json"
    log_path = tmp_path / "calls.jsonl"
    acceptance_path = tmp_path / "inherited_iam_acceptance.json"
    fake_sdk = tmp_path / "fake-cloud-sdk"
    fake_bin = fake_sdk / "bin"
    fake_lib = fake_sdk / "lib"
    fake_bin.mkdir(parents=True)
    fake_lib.mkdir(parents=True)
    fake_python = fake_lib / "gcloud.py"
    fake_command = fake_bin / "gcloud.ps1"
    preflight_path.write_text(json.dumps(preflight or _valid_preflight()), encoding="utf-8")
    state_path.write_text(json.dumps(state or _valid_state()), encoding="utf-8")
    acceptance_path.write_text(
        json.dumps(acceptance or _accepted_inherited_iam()), encoding="utf-8"
    )
    fake_python.write_text(FAKE_GCLOUD, encoding="utf-8")
    fake_command.write_text("# fake gcloud launcher marker\n", encoding="utf-8")

    env = os.environ.copy()
    env["FAKE_GCLOUD_STATE"] = str(state_path)
    env["FAKE_GCLOUD_LOG"] = str(log_path)
    env["CLOUDSDK_PYTHON"] = sys.executable
    completed = subprocess.run(
        [
            powershell,
            "-NoProfile",
            "-File",
            str(RECOVERY_SCRIPT),
            "-Mode",
            mode,
            "-PreflightPath",
            str(preflight_path),
            "-InheritedIamAcceptancePath",
            str(acceptance_path),
            "-GcloudExecutable",
            str(fake_command),
            "-MaxProbeAttempts",
            "5",
            "-ProbeIntervalSeconds",
            "0",
        ],
        cwd=ARCHIVE_DIR.parents[2],
        env=env,
        capture_output=True,
        encoding="utf-8",
        errors="replace",
        check=False,
        timeout=30,
    )
    calls = []
    if log_path.exists():
        calls = [json.loads(line) for line in log_path.read_text(encoding="utf-8").splitlines()]
    final_state = json.loads(state_path.read_text(encoding="utf-8"))
    return completed, calls, final_state


def _writes(calls: list[list[str]]) -> list[list[str]]:
    return [
        call
        for call in calls
        if call[:2] == ["projects", "set-iam-policy"]
        or call[:3] == ["storage", "buckets", "set-iam-policy"]
    ]


def test_archive_operator_check_reports_exact_missing_binding_without_mutation(
    tmp_path: Path,
) -> None:
    acceptance = _accepted_with_archive_operator()
    completed, calls, _ = _run_recovery(
        tmp_path,
        mode="CheckArchiveOperator",
        acceptance=acceptance,
    )

    assert completed.returncode == 0, completed.stderr
    assert json.loads(completed.stdout) == {
        "bucket": ARCHIVE_BUCKET,
        "mode": "CheckArchiveOperator",
        "mutated": False,
        "status": "requires_archive_operator_grant",
    }
    assert _writes(calls) == []
    assert [call for call in calls if call[:3] == ["storage", "buckets", "describe"]] == [
        [
            "storage",
            "buckets",
            "describe",
            ARCHIVE_URI,
            f"--project={PROJECT_ID}",
            f"--account={ACCOUNT}",
            "--raw",
            "--format=json",
        ]
    ]


def test_archive_operator_grant_writes_exact_target_once_and_verifies_archive(
    tmp_path: Path,
) -> None:
    acceptance = _accepted_with_archive_operator()
    state = _valid_state()
    completed, calls, final_state = _run_recovery(
        tmp_path,
        mode="GrantArchiveOperator",
        state=state,
        acceptance=acceptance,
    )

    assert completed.returncode == 0, completed.stderr
    assert json.loads(completed.stdout) == {
        "bucket": ARCHIVE_BUCKET,
        "mode": "GrantArchiveOperator",
        "mutated": True,
        "status": "archive_operator_granted",
    }
    writes = _writes(calls)
    assert len(writes) == 1
    assert writes[0][:3] == ["projects", "set-iam-policy", PROJECT_ID]
    assert writes[0][4:] == [
        f"--account={ACCOUNT}",
        "--quiet",
        "--format=json",
    ]
    record = final_state["project_policy_writes"][0]
    assert record["policy"] == {
        "bindings": acceptance["project_policy"]["policy"]["bindings"],
        "etag": "live-project-etag",
        "version": 3,
    }
    assert not Path(record["path"]).exists()
    assert final_state["archive_bucket_status"] == "exists"
    assert any(
        call[:4] == ["storage", "buckets", "get-iam-policy", ARCHIVE_URI]
        for call in calls
    )


def test_archive_operator_grant_is_idempotent_only_after_full_archive_verification(
    tmp_path: Path,
) -> None:
    acceptance = _accepted_with_archive_operator()
    state = _valid_state()
    state["project_iam"] = {
        **deepcopy(acceptance["project_policy"]["policy"]),
        "etag": "archive-operator-etag",
    }
    state["archive_bucket_status"] = "exists"
    completed, calls, _ = _run_recovery(
        tmp_path,
        mode="GrantArchiveOperator",
        state=state,
        acceptance=acceptance,
    )

    assert completed.returncode == 0, completed.stderr
    assert json.loads(completed.stdout)["status"] == "archive_operator_ready"
    assert json.loads(completed.stdout)["mutated"] is False
    assert _writes(calls) == []


@pytest.mark.parametrize(
    "mutation",
    [
        "condition",
        "member",
        "extra_binding",
        "archive_iam",
        "archive_metadata",
        "archive_lifecycle",
    ],
)
def test_archive_operator_drift_fails_closed_without_mutation(
    tmp_path: Path,
    mutation: str,
) -> None:
    acceptance = _accepted_with_archive_operator()
    state = _valid_state()
    state["project_iam"] = {
        **deepcopy(acceptance["project_policy"]["policy"]),
        "etag": "archive-operator-etag",
    }
    state["archive_bucket_status"] = "exists"
    operator_binding = next(
        binding
        for binding in state["project_iam"]["bindings"]
        if binding.get("condition", {}).get("title") == ARCHIVE_OPERATOR_TITLE
    )
    if mutation == "condition":
        operator_binding["condition"]["expression"] = "true"
    elif mutation == "member":
        operator_binding["members"] = ["user:outsider@example.com"]
    elif mutation == "extra_binding":
        state["project_iam"]["bindings"].append(
            {"role": "roles/viewer", "members": ["user:outsider@example.com"]}
        )
    elif mutation == "archive_iam":
        state["archive_iam"]["bindings"].append(
            {"role": "roles/storage.objectViewer", "members": ["allUsers"]}
        )
    elif mutation == "archive_metadata":
        state["archive_bucket"]["iamConfiguration"]["publicAccessPrevention"] = (
            "inherited"
        )
    elif mutation == "archive_lifecycle":
        state["archive_bucket"]["lifecycle"] = {
            "rule": [{"action": {"type": "Delete"}, "condition": {"age": 30}}]
        }

    completed, calls, _ = _run_recovery(
        tmp_path,
        mode="CheckArchiveOperator",
        state=state,
        acceptance=acceptance,
    )

    assert completed.returncode != 0
    assert "Cannot validate argument on parameter 'Mode'" not in completed.stderr
    assert _writes(calls) == []


@pytest.mark.parametrize("scope", ["project", "organization"])
def test_archive_operator_final_verification_rejects_inherited_iam_race(
    tmp_path: Path,
    scope: str,
) -> None:
    acceptance = _accepted_with_archive_operator()
    state = _valid_state()
    state["project_iam"] = {
        **deepcopy(acceptance["project_policy"]["policy"]),
        "etag": "archive-operator-etag",
    }
    state["archive_bucket_status"] = "exists"
    state[f"{scope}_drift_on_archive_iam_read"] = {
        "role": "roles/viewer",
        "members": [f"user:concurrent-{scope}@example.com"],
    }

    completed, calls, _ = _run_recovery(
        tmp_path,
        mode="CheckArchiveOperator",
        state=state,
        acceptance=acceptance,
    )

    assert completed.returncode != 0
    assert _writes(calls) == []
    archive_iam_index = next(
        index
        for index, call in enumerate(calls)
        if call[:4] == ["storage", "buckets", "get-iam-policy", ARCHIVE_URI]
    )
    assert any(
        call[:2] == [f"{scope}s", "get-iam-policy"]
        for call in calls[archive_iam_index + 1 :]
    )


def test_archive_operator_grant_does_not_report_success_after_final_iam_race(
    tmp_path: Path,
) -> None:
    acceptance = _accepted_with_archive_operator()
    state = _valid_state()
    state["project_drift_on_archive_iam_read"] = {
        "role": "roles/viewer",
        "members": ["user:concurrent-project@example.com"],
    }

    completed, calls, final_state = _run_recovery(
        tmp_path,
        mode="GrantArchiveOperator",
        state=state,
        acceptance=acceptance,
    )

    assert completed.returncode != 0
    assert len(_writes(calls)) == 1
    assert final_state["project_iam"]["etag"] == "project-etag-after-archive-read"
    archive_iam_index = next(
        index
        for index, call in enumerate(calls)
        if call[:4] == ["storage", "buckets", "get-iam-policy", ARCHIVE_URI]
    )
    assert any(
        call[:2] == ["projects", "get-iam-policy"]
        for call in calls[archive_iam_index + 1 :]
    )


def test_archive_operator_rechecks_latest_project_etag_before_write(
    tmp_path: Path,
) -> None:
    acceptance = _accepted_with_archive_operator()
    state = _valid_state()
    state["project_drift_on_get_count"] = 2
    completed, calls, final_state = _run_recovery(
        tmp_path,
        mode="GrantArchiveOperator",
        state=state,
        acceptance=acceptance,
    )

    assert completed.returncode != 0
    assert "Cannot validate argument on parameter 'Mode'" not in completed.stderr
    assert _writes(calls) == []
    assert final_state["project_iam"]["etag"] == "concurrent-project-etag"


def test_archive_operator_mode_requires_target_acceptance_before_gcloud_identity(
    tmp_path: Path,
) -> None:
    completed, calls, _ = _run_recovery(
        tmp_path,
        mode="CheckArchiveOperator",
        acceptance=_accepted_inherited_iam(),
    )

    assert completed.returncode != 0
    assert "Cannot validate argument on parameter 'Mode'" not in completed.stderr
    assert calls == [["version", "--format=json"]]


def test_check_reports_lockout_without_mutation(tmp_path: Path) -> None:
    completed, calls, _ = _run_recovery(tmp_path)

    assert completed.returncode == 0, completed.stderr
    assert json.loads(completed.stdout) == {
        "bucket": BACKEND_BUCKET,
        "mode": "Check",
        "mutated": False,
        "status": "requires_temporary_grant",
    }
    assert _writes(calls) == []


def test_check_reports_already_durable_baseline_without_mutation(tmp_path: Path) -> None:
    state = _valid_state()
    state["bucket_status"] = "exists"
    state["iam"] = _target_iam()
    completed, calls, _ = _run_recovery(tmp_path, state=state)

    assert completed.returncode == 0, completed.stderr
    assert json.loads(completed.stdout)["status"] == "recovery_complete"
    assert _writes(calls) == []


def test_check_rejects_accessible_lockout_policy_without_mutation(tmp_path: Path) -> None:
    state = _valid_state()
    state["bucket_status"] = "exists"
    completed, calls, _ = _run_recovery(tmp_path, state=state)

    assert completed.returncode != 0
    assert _writes(calls) == []


def test_check_rejects_generic_permission_error_without_mutation(tmp_path: Path) -> None:
    state = _valid_state()
    state["bucket_permission_error"] = "ERROR: HTTPError 403: generic forbidden"
    completed, calls, _ = _run_recovery(tmp_path, state=state)

    assert completed.returncode != 0
    assert _writes(calls) == []


def test_check_rejects_exact_get_iam_denial_after_describe_success(
    tmp_path: Path,
) -> None:
    state = _valid_state()
    state["bucket_status"] = "exists"
    state["bucket_iam_access_delay"] = 1
    completed, calls, _ = _run_recovery(tmp_path, state=state)

    assert completed.returncode != 0
    assert len(
        [call for call in calls if call[:3] == ["storage", "buckets", "describe"]]
    ) == 1
    assert _writes(calls) == []


def test_grant_temporary_writes_exact_conditional_full_policy_once(tmp_path: Path) -> None:
    state = _valid_state()
    state["next_project_visibility_delay"] = 2
    completed, calls, final_state = _run_recovery(
        tmp_path, mode="GrantTemporary", state=state
    )

    assert completed.returncode == 0, completed.stderr
    assert json.loads(completed.stdout)["status"] == "temporary_granted"
    writes = _writes(calls)
    assert len(writes) == 1
    write = writes[0]
    assert write[:3] == ["projects", "set-iam-policy", PROJECT_ID]
    assert write[4:] == [f"--account={ACCOUNT}", "--quiet", "--format=json"]
    record = final_state["project_policy_writes"][0]
    assert record["policy"] == {
        "bindings": _accepted_inherited_iam()["project_policy"]["policy"]["bindings"]
        + [_temporary_binding()],
        "etag": "live-project-etag",
        "version": 3,
    }
    assert not Path(record["path"]).exists()
    assert final_state["file_probes"] == [
        {
            "kind": "project-policy",
            "path": record["path"],
            "write_blocked": True,
            "delete_blocked": True,
        }
    ]
    assert len([call for call in calls if call[:2] == ["projects", "set-iam-policy"]]) == 1
    assert len([call for call in calls if call[:2] == ["projects", "get-iam-policy"]]) >= 3


def test_grant_reprobes_denial_and_blocks_state_change_before_write(
    tmp_path: Path,
) -> None:
    state = _valid_state()
    state["make_bucket_accessible_after_organization_read"] = True
    completed, calls, _ = _run_recovery(
        tmp_path, mode="GrantTemporary", state=state
    )

    assert completed.returncode != 0
    describe_calls = [
        call for call in calls if call[:3] == ["storage", "buckets", "describe"]
    ]
    assert len(describe_calls) == 1
    assert _writes(calls) == []


def test_old_gcloud_is_blocked_before_any_authenticated_command_or_write(
    tmp_path: Path,
) -> None:
    state = _valid_state()
    state["gcloud_version"] = "262.0.0"
    completed, calls, _ = _run_recovery(
        tmp_path, mode="GrantTemporary", state=state
    )

    assert completed.returncode != 0
    assert calls == [["version", "--format=json"]]


def test_lossy_withcond_read_after_grant_blocks_before_bucket_access(
    tmp_path: Path,
) -> None:
    state = _valid_state()
    state["lossy_project_after_write"] = True
    completed, calls, _ = _run_recovery(
        tmp_path, mode="GrantTemporary", state=state
    )

    assert completed.returncode != 0
    assert len(_writes(calls)) == 1
    assert "_withcond_ placeholder" in completed.stderr
    assert not any(
        call[:3] == ["storage", "buckets", "get-iam-policy"] for call in calls
    )


def test_initial_withcond_placeholder_blocks_before_bucket_access_or_write(
    tmp_path: Path,
) -> None:
    state = _valid_state()
    state["project_iam"] = _temporary_project_iam()
    temporary = state["project_iam"]["bindings"][-1]
    temporary["role"] = "roles/storage.admin_withcond_deadbeef"
    temporary.pop("condition")
    state["bucket_status"] = "exists"
    completed, calls, _ = _run_recovery(
        tmp_path, mode="RepairBucket", state=state
    )

    assert completed.returncode != 0
    assert "_withcond_ placeholder" in completed.stderr
    assert not any(call[:2] == ["storage", "buckets"] for call in calls)
    assert _writes(calls) == []


def test_organization_withcond_placeholder_blocks_before_write(tmp_path: Path) -> None:
    state = _valid_state()
    state["organization_iam"]["bindings"][0]["role"] = (
        "roles/storage.objectAdmin_withcond_deadbeef"
    )
    completed, calls, _ = _run_recovery(
        tmp_path, mode="GrantTemporary", state=state
    )

    assert completed.returncode != 0
    assert "_withcond_ placeholder" in completed.stderr
    assert _writes(calls) == []


def test_authenticated_reads_pin_account_despite_shared_config_race(
    tmp_path: Path,
) -> None:
    state = _valid_state()
    state["bucket_status"] = "exists"
    state["iam"] = _target_iam()
    state["require_explicit_account"] = True
    state["flip_config_after_list"] = True
    completed, calls, _ = _run_recovery(tmp_path, state=state)

    assert completed.returncode == 0, completed.stderr
    assert json.loads(completed.stdout)["status"] == "recovery_complete"
    authenticated_calls = [
        call
        for call in calls
        if any(call[: len(prefix)] == prefix for prefix in [
            ["projects", "describe"],
            ["billing", "projects", "describe"],
            ["projects", "get-iam-policy"],
            ["organizations", "get-iam-policy"],
            ["storage", "buckets", "describe"],
            ["storage", "buckets", "get-iam-policy"],
        ])
    ]
    assert authenticated_calls
    assert all(f"--account={ACCOUNT}" in call for call in authenticated_calls)


def test_grant_waits_on_exact_live_bucket_permission_errors_without_rewriting(
    tmp_path: Path,
) -> None:
    state = _valid_state()
    state["next_bucket_access_delay"] = 2
    state["bucket_iam_access_delay"] = 1
    completed, calls, _ = _run_recovery(
        tmp_path, mode="GrantTemporary", state=state
    )

    assert completed.returncode == 0, completed.stderr
    assert json.loads(completed.stdout)["status"] == "temporary_granted"
    assert len(_writes(calls)) == 1
    assert len(
        [call for call in calls if call[:3] == ["storage", "buckets", "describe"]]
    ) == 5
    assert len(
        [
            call
            for call in calls
            if call[:3] == ["storage", "buckets", "get-iam-policy"]
        ]
    ) == 2


@pytest.mark.parametrize(
    "error_text",
    [
        "ERROR: HTTPError 403: generic forbidden",
        (
            "ERROR: (gcloud.storage.buckets.describe) [admin@motionexp.com] does not "
            "have storage.buckets.get access. Permission denied 403 plus unrelated error."
        ),
    ],
)
def test_grant_rejects_nonexact_permission_errors_before_writing(
    tmp_path: Path, error_text: str
) -> None:
    state = _valid_state()
    state["next_bucket_access_delay"] = 2
    state["bucket_permission_error"] = error_text
    completed, calls, _ = _run_recovery(
        tmp_path, mode="GrantTemporary", state=state
    )

    assert completed.returncode != 0
    assert _writes(calls) == []


def test_check_rejects_nonexact_get_iam_permission_error(tmp_path: Path) -> None:
    state = _valid_state()
    state["project_iam"] = _temporary_project_iam()
    state["bucket_status"] = "exists"
    state["bucket_iam_access_delay"] = 1
    state["bucket_iam_permission_error"] = (
        "ERROR: HTTPError 403: generic getIamPolicy permission failure"
    )
    completed, calls, _ = _run_recovery(tmp_path, state=state)

    assert completed.returncode != 0
    assert _writes(calls) == []


def test_repair_bucket_requires_exact_lockout_policy_and_uses_etag(tmp_path: Path) -> None:
    state = _valid_state()
    state["project_iam"] = _temporary_project_iam()
    state["bucket_status"] = "exists"
    state["next_bucket_visibility_delay"] = 2
    completed, calls, final_state = _run_recovery(
        tmp_path, mode="RepairBucket", state=state
    )

    assert completed.returncode == 0, completed.stderr
    assert json.loads(completed.stdout)["status"] == "bucket_repaired"
    writes = _writes(calls)
    assert len(writes) == 1
    assert writes[0][:4] == ["storage", "buckets", "set-iam-policy", BACKEND_URI]
    assert writes[0][5:] == [
        f"--project={PROJECT_ID}",
        f"--account={ACCOUNT}",
        "--etag=lockout-etag",
        "--quiet",
        "--format=json",
    ]
    record = final_state["bucket_policy_writes"][0]
    assert record["policy"] == {
        "bindings": _target_iam()["bindings"],
        "version": 1,
    }
    assert not Path(record["path"]).exists()
    assert len([call for call in calls if call[:3] == ["storage", "buckets", "set-iam-policy"]]) == 1
    assert len([call for call in calls if call[:3] == ["storage", "buckets", "get-iam-policy"]]) >= 3


def test_revoke_temporary_restores_exact_baseline_and_final_verifies(tmp_path: Path) -> None:
    state, acceptance = _unconditional_recovery_state()
    accepted_bindings = deepcopy(acceptance["project_policy"]["policy"]["bindings"])
    state["next_project_visibility_delay"] = 2
    completed, calls, final_state = _run_recovery(
        tmp_path, mode="RevokeTemporary", state=state, acceptance=acceptance
    )

    assert completed.returncode == 0, completed.stderr
    assert json.loads(completed.stdout)["status"] == "recovery_complete"
    writes = _writes(calls)
    assert len(writes) == 1
    record = final_state["project_policy_writes"][0]
    assert record["policy"] == {
        "bindings": accepted_bindings,
        "etag": "temporary-etag",
        "version": 3,
    }
    assert final_state["project_iam"] == {
        "bindings": accepted_bindings,
        "etag": "project-etag-after-set",
        "version": 3,
    }
    assert final_state["iam"] == _target_iam()
    assert not Path(record["path"]).exists()
    assert len([call for call in calls if call[:2] == ["projects", "set-iam-policy"]]) == 1
    assert calls[-1][:3] == ["storage", "buckets", "get-iam-policy"]


def test_revoke_accepts_safe_unconditional_version3_post_read(tmp_path: Path) -> None:
    state, acceptance = _unconditional_recovery_state()
    state["preserve_unconditional_project_version_after_write"] = True

    completed, calls, final_state = _run_recovery(
        tmp_path, mode="RevokeTemporary", state=state, acceptance=acceptance
    )

    assert completed.returncode == 0, completed.stderr
    assert json.loads(completed.stdout)["status"] == "recovery_complete"
    assert len(_writes(calls)) == 1
    assert final_state["project_policy_writes"][0]["policy"]["version"] == 3
    assert final_state["project_iam"]["version"] == 3


def test_revoke_rejects_condition_policy_version1_before_write(tmp_path: Path) -> None:
    state = _valid_state()
    state["project_iam"] = _temporary_project_iam()
    state["project_iam"]["version"] = 1
    state["bucket_status"] = "exists"
    state["iam"] = _target_iam()

    completed, calls, final_state = _run_recovery(
        tmp_path, mode="RevokeTemporary", state=state
    )

    assert completed.returncode != 0
    assert "condition-capable policy version 3" in completed.stderr
    assert _writes(calls) == []
    assert any(
        binding.get("condition", {}).get("title") == TEMP_TITLE
        for binding in final_state["project_iam"]["bindings"]
    )


def test_revoke_rejects_withcond_placeholder_before_write(tmp_path: Path) -> None:
    state = _valid_state()
    state["project_iam"] = _temporary_project_iam()
    state["project_iam"]["bindings"][0]["role"] = (
        "roles/owner_withcond_deadbeef"
    )
    state["bucket_status"] = "exists"
    state["iam"] = _target_iam()

    completed, calls, final_state = _run_recovery(
        tmp_path, mode="RevokeTemporary", state=state
    )

    assert completed.returncode != 0
    assert "_withcond_ placeholder" in completed.stderr
    assert _writes(calls) == []
    assert any(
        binding.get("condition", {}).get("title") == TEMP_TITLE
        for binding in final_state["project_iam"]["bindings"]
    )


def test_revoke_rejects_unconditional_version3_post_read_with_drift(
    tmp_path: Path,
) -> None:
    state, acceptance = _unconditional_recovery_state()
    state["preserve_unconditional_project_version_after_write"] = True
    state["unexpected_project_after_write"] = True

    completed, calls, final_state = _run_recovery(
        tmp_path, mode="RevokeTemporary", state=state, acceptance=acceptance
    )

    assert completed.returncode != 0
    assert len(_writes(calls)) == 1
    assert any(
        binding["members"] == ["user:outsider@example.com"]
        for binding in final_state["project_iam"]["bindings"]
    )


def test_revoke_removes_only_exact_temp_and_preserves_project_drift(
    tmp_path: Path,
) -> None:
    state = _valid_state()
    state["project_iam"] = _temporary_project_iam()
    drift_binding = {
        "role": "roles/browser",
        "members": ["group:emergency-reviewers@motionexp.com"],
        "condition": {
            "title": "emergency-review-window",
            "description": "Unrelated concurrent drift must be preserved",
            "expression": "request.time < timestamp('2031-01-01T00:00:00Z')",
        },
    }
    state["project_iam"]["bindings"].insert(1, drift_binding)
    state["bucket_status"] = "exists"
    state["iam"] = _target_iam()
    original_bindings = deepcopy(state["project_iam"]["bindings"])

    completed, calls, final_state = _run_recovery(
        tmp_path, mode="RevokeTemporary", state=state
    )

    assert completed.returncode != 0
    assert json.loads(completed.stdout) == {
        "bucket": BACKEND_BUCKET,
        "mode": "RevokeTemporary",
        "mutated": True,
        "status": "temporary_removed_drift_requires_escalation",
    }
    assert len(_writes(calls)) == 1
    written = final_state["project_policy_writes"][0]["policy"]
    assert written["bindings"] == original_bindings[:-1]
    assert written["etag"] == "temporary-etag"
    assert written["version"] == 3
    assert drift_binding in final_state["project_iam"]["bindings"]
    assert not any(
        binding.get("condition", {}).get("title") == TEMP_TITLE
        for binding in final_state["project_iam"]["bindings"]
    )


def test_revoke_removes_temp_even_when_organization_has_drift(tmp_path: Path) -> None:
    state = _valid_state()
    state["project_iam"] = _temporary_project_iam()
    state["organization_iam"]["bindings"].append(
        {"role": "roles/browser", "members": ["user:outsider@example.com"]}
    )
    state["bucket_status"] = "exists"
    state["iam"] = _target_iam()

    completed, calls, final_state = _run_recovery(
        tmp_path, mode="RevokeTemporary", state=state
    )

    assert completed.returncode != 0
    assert json.loads(completed.stdout)["status"] == (
        "temporary_removed_drift_requires_escalation"
    )
    assert len(_writes(calls)) == 1
    assert not any(
        binding.get("condition", {}).get("title") == TEMP_TITLE
        for binding in final_state["project_iam"]["bindings"]
    )


def test_revoke_refreshes_project_after_bucket_verification(
    tmp_path: Path,
) -> None:
    state = _valid_state()
    state["project_iam"] = _temporary_project_iam()
    concurrent_binding = {
        "role": "roles/browser",
        "members": ["group:concurrent-reviewers@motionexp.com"],
        "condition": {
            "title": "concurrent-review",
            "description": "Appeared while bucket IAM was being verified",
            "expression": "request.time < timestamp('2032-01-01T00:00:00Z')",
        },
    }
    state["project_drift_on_bucket_iam_read"] = concurrent_binding
    state["bucket_status"] = "exists"
    state["iam"] = _target_iam()

    completed, calls, final_state = _run_recovery(
        tmp_path, mode="RevokeTemporary", state=state
    )

    assert completed.returncode != 0
    assert json.loads(completed.stdout)["status"] == (
        "temporary_removed_drift_requires_escalation"
    )
    assert len(_writes(calls)) == 1
    written = final_state["project_policy_writes"][0]["policy"]
    assert written["etag"] == "etag-after-bucket-verification"
    assert concurrent_binding in written["bindings"]
    assert concurrent_binding in final_state["project_iam"]["bindings"]
    bucket_read_index = next(
        index
        for index, call in enumerate(calls)
        if call[:3] == ["storage", "buckets", "get-iam-policy"]
    )
    project_read_after_bucket = next(
        index
        for index, call in enumerate(calls)
        if index > bucket_read_index
        and call[:2] == ["projects", "get-iam-policy"]
    )
    project_write_index = next(
        index
        for index, call in enumerate(calls)
        if call[:2] == ["projects", "set-iam-policy"]
    )
    assert bucket_read_index < project_read_after_bucket < project_write_index


def test_revoke_preserves_concurrent_condition_location_and_removes_temp(
    tmp_path: Path,
) -> None:
    state = _valid_state()
    state["project_iam"] = _temporary_project_iam()
    concurrent_binding = {
        "role": "roles/browser",
        "members": ["group:location-reviewers@motionexp.com"],
        "condition": {
            "location": "iam-policy.json:17",
            "expression": "request.time < timestamp('2032-06-01T00:00:00Z')",
            "title": "location-review",
            "description": "Preserve the live condition object exactly",
        },
    }
    audit_configs = [
        {
            "service": "storage.googleapis.com",
            "auditLogConfigs": [
                {
                    "logType": "DATA_WRITE",
                    "exemptedMembers": ["group:location-reviewers@motionexp.com"],
                }
            ],
        }
    ]
    state["project_iam"]["auditConfigs"] = audit_configs
    state["project_drift_on_bucket_iam_read"] = concurrent_binding
    state["bucket_status"] = "exists"
    state["iam"] = _target_iam()

    completed, calls, final_state = _run_recovery(
        tmp_path, mode="RevokeTemporary", state=state
    )

    assert len(_writes(calls)) == 1
    assert completed.returncode != 0
    assert json.loads(completed.stdout)["status"] == (
        "temporary_removed_drift_requires_escalation"
    )
    written = final_state["project_policy_writes"][0]["policy"]
    assert written["etag"] == "etag-after-bucket-verification"
    assert written["version"] == 3
    assert written["auditConfigs"] == audit_configs
    assert final_state["project_iam"]["auditConfigs"] == audit_configs
    assert final_state["project_iam"]["version"] == 3
    written_binding = next(
        binding
        for binding in written["bindings"]
        if binding["members"] == concurrent_binding["members"]
    )
    assert written_binding == concurrent_binding
    assert list(written_binding["condition"]) == [
        "location",
        "expression",
        "title",
        "description",
    ]
    assert not any(
        binding.get("condition", {}).get("title") == TEMP_TITLE
        for binding in final_state["project_iam"]["bindings"]
    )


def test_revoke_rejects_unexpected_condition_fields_without_removing_temp(
    tmp_path: Path,
) -> None:
    state = _valid_state()
    state["project_iam"] = _temporary_project_iam()
    state["project_iam"]["bindings"].insert(
        0,
        {
            "role": "roles/browser",
            "members": ["group:unsupported-condition@motionexp.com"],
            "condition": {
                "title": "unsupported-condition",
                "expression": "request.time < timestamp('2032-01-01T00:00:00Z')",
                "debugContext": "not part of google.type.Expr",
            },
        },
    )
    state["bucket_status"] = "exists"
    state["iam"] = _target_iam()

    completed, calls, final_state = _run_recovery(
        tmp_path, mode="RevokeTemporary", state=state
    )

    assert completed.returncode != 0
    assert _writes(calls) == []
    assert "unexpected field 'debugContext'" in completed.stderr
    assert any(
        binding.get("condition", {}).get("title") == TEMP_TITLE
        for binding in final_state["project_iam"]["bindings"]
    )


def test_revoke_cleans_temp_despite_metadata_drift(tmp_path: Path) -> None:
    state = _valid_state()
    state["project_iam"] = _temporary_project_iam()
    state["bucket_status"] = "exists"
    state["bucket"]["labels"]["purpose"] = "unexpected-drift"
    state["iam"] = _target_iam()

    completed, calls, final_state = _run_recovery(
        tmp_path, mode="RevokeTemporary", state=state
    )

    assert completed.returncode != 0
    assert json.loads(completed.stdout)["status"] == (
        "temporary_removed_drift_requires_escalation"
    )
    assert len(_writes(calls)) == 1
    assert not any(
        binding.get("condition", {}).get("title") == TEMP_TITLE
        for binding in final_state["project_iam"]["bindings"]
    )


def test_revoke_preserves_audit_configs_while_removing_temp(tmp_path: Path) -> None:
    state = _valid_state()
    state["project_iam"] = _temporary_project_iam()
    audit_configs = [
        {
            "service": "storage.googleapis.com",
            "auditLogConfigs": [
                {
                    "logType": "DATA_READ",
                    "exemptedMembers": ["user:auditor@motionexp.com"],
                }
            ],
        }
    ]
    state["project_iam"]["auditConfigs"] = audit_configs
    state["bucket_status"] = "exists"
    state["iam"] = _target_iam()

    completed, calls, final_state = _run_recovery(
        tmp_path, mode="RevokeTemporary", state=state
    )

    assert completed.returncode != 0
    assert json.loads(completed.stdout)["status"] == (
        "temporary_removed_drift_requires_escalation"
    )
    assert len(_writes(calls)) == 1
    written = final_state["project_policy_writes"][0]["policy"]
    assert written["auditConfigs"] == audit_configs
    assert final_state["project_iam"]["auditConfigs"] == audit_configs


def test_revoke_allows_benign_withcond_text_outside_role_names(tmp_path: Path) -> None:
    state = _valid_state()
    state["project_iam"] = _temporary_project_iam()
    benign_binding = {
        "role": "roles/browser",
        "members": ["group:benign_withcond_text@motionexp.com"],
        "condition": {
            "title": "benign_withcond_title",
            "description": "A benign _withcond_ description is not a role placeholder",
            "expression": "request.time < timestamp('2033-01-01T00:00:00Z')",
        },
    }
    state["project_iam"]["bindings"].insert(0, benign_binding)
    state["bucket_status"] = "exists"
    state["iam"] = _target_iam()

    completed, calls, final_state = _run_recovery(
        tmp_path, mode="RevokeTemporary", state=state
    )

    assert completed.returncode != 0
    assert json.loads(completed.stdout)["status"] == (
        "temporary_removed_drift_requires_escalation"
    )
    assert len(_writes(calls)) == 1
    assert benign_binding in final_state["project_iam"]["bindings"]


@pytest.mark.parametrize("scope", ["project", "organization"])
def test_grant_blocks_inherited_iam_drift_before_any_write(
    tmp_path: Path, scope: str
) -> None:
    state = _valid_state()
    state[f"{scope}_iam"]["bindings"].append(
        {"role": "roles/viewer", "members": ["user:outsider@example.com"]}
    )
    completed, calls, _ = _run_recovery(
        tmp_path, mode="GrantTemporary", state=state
    )

    assert completed.returncode != 0
    assert _writes(calls) == []


def test_repair_blocks_noncanonical_bucket_policy_before_write(tmp_path: Path) -> None:
    state = _valid_state()
    state["project_iam"] = _temporary_project_iam()
    state["bucket_status"] = "exists"
    state["iam"]["bindings"][0]["members"].append("user:outsider@example.com")
    completed, calls, _ = _run_recovery(
        tmp_path, mode="RepairBucket", state=state
    )

    assert completed.returncode != 0
    assert _writes(calls) == []


@pytest.mark.parametrize("mutation", ["expression", "title", "member"])
def test_repair_blocks_nonexact_temporary_binding_before_bucket_access(
    tmp_path: Path, mutation: str
) -> None:
    state = _valid_state()
    state["project_iam"] = _temporary_project_iam()
    temporary = state["project_iam"]["bindings"][-1]
    if mutation == "expression":
        temporary["condition"]["expression"] = "true"
    elif mutation == "title":
        temporary["condition"]["title"] = "wrong-title"
    else:
        temporary["members"] = ["user:outsider@example.com"]
    state["bucket_status"] = "exists"
    completed, calls, _ = _run_recovery(
        tmp_path, mode="RepairBucket", state=state
    )

    assert completed.returncode != 0
    assert not any(call[:2] == ["storage", "buckets"] for call in calls)
    assert _writes(calls) == []


def test_revoke_blocks_until_durable_bucket_target_is_verified(tmp_path: Path) -> None:
    state = _valid_state()
    state["project_iam"] = _temporary_project_iam()
    state["bucket_status"] = "exists"
    completed, calls, _ = _run_recovery(
        tmp_path, mode="RevokeTemporary", state=state
    )

    assert completed.returncode != 0
    assert _writes(calls) == []


@pytest.mark.parametrize("phase", ["GrantTemporary", "RepairBucket"])
def test_post_write_unrelated_drift_fails_without_repeating_write(
    tmp_path: Path, phase: str
) -> None:
    state = _valid_state()
    if phase == "GrantTemporary":
        state["unexpected_project_after_write"] = True
    else:
        state["project_iam"] = _temporary_project_iam()
        state["bucket_status"] = "exists"
        state["unexpected_bucket_after_write"] = True
    completed, calls, _ = _run_recovery(tmp_path, mode=phase, state=state)

    assert completed.returncode != 0
    assert len(_writes(calls)) == 1


def test_final_state_is_idempotently_reported_without_write(tmp_path: Path) -> None:
    state = _valid_state()
    state["bucket_status"] = "exists"
    state["iam"] = _target_iam()
    completed, calls, _ = _run_recovery(
        tmp_path, mode="RevokeTemporary", state=state
    )

    assert completed.returncode == 0, completed.stderr
    assert json.loads(completed.stdout)["status"] == "recovery_complete"
    assert _writes(calls) == []


def test_recovery_interface_has_no_token_or_http_transport_path() -> None:
    source = RECOVERY_SCRIPT.read_text(encoding="utf-8")

    for forbidden in (
        "Invoke-RestMethod",
        "Invoke-WebRequest",
        "print-access-token",
        "Authorization",
        "Bearer ",
    ):
        assert forbidden not in source
