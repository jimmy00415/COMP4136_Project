from __future__ import annotations

import hashlib
import json
import os
import shutil
import subprocess
import sys
from pathlib import Path
from typing import Any

import hcl2
import pytest

REPO_ROOT = Path(__file__).resolve().parents[1]
ARCHIVE_DIR = REPO_ROOT / "infra" / "terraform" / "archive"
BOOTSTRAP_SCRIPT = ARCHIVE_DIR / "bootstrap_backend.ps1"
BACKEND_BUCKET = "motionexpaiweb-880586285913-hk-movie-rag-tfstate"
BACKEND_URI = f"gs://{BACKEND_BUCKET}"
PROJECT_ID = "motionexpaiweb"
PROJECT_NUMBER = "880586285913"
ACCOUNT = "admin@motionexp.com"
ORGANIZATION_ID = "797368190621"
ACCEPTANCE_PATH = ARCHIVE_DIR / "inherited_iam_acceptance.json"
ACCEPTANCE_BUILDER = ARCHIVE_DIR / "build_inherited_iam_acceptance.py"
_DEFAULT_ACCEPTANCE = object()


def _load_hcl(path: Path) -> dict[str, Any]:
    with path.open(encoding="utf-8") as handle:
        return hcl2.load(handle)


def _unquote(value: str) -> str:
    return value[1:-1] if value.startswith('"') and value.endswith('"') else value


def _resource(document: dict[str, Any], resource_type: str, name: str) -> dict[str, Any]:
    for resource in document["resource"]:
        typed = resource.get(f'"{resource_type}"')
        if typed and f'"{name}"' in typed:
            return typed[f'"{name}"']
    raise AssertionError(f"missing Terraform resource {resource_type}.{name}")


def test_archive_stack_uses_the_dedicated_remote_backend() -> None:
    document = _load_hcl(ARCHIVE_DIR / "versions.tf")
    terraform = document["terraform"][0]

    backend = terraform["backend"][0]['"gcs"']

    assert _unquote(backend["bucket"]) == BACKEND_BUCKET
    assert _unquote(backend["prefix"]) == "hk-movie-rag/archive"


def test_archive_bucket_cannot_be_destroyed_by_terraform() -> None:
    bucket = _resource(
        _load_hcl(ARCHIVE_DIR / "main.tf"), "google_storage_bucket", "source_archive"
    )

    assert bucket["lifecycle"][0]["prevent_destroy"] is True


def test_provider_lock_is_tracked_and_pins_google_provider() -> None:
    lock_path = ARCHIVE_DIR / ".terraform.lock.hcl"
    tracked = subprocess.run(
        ["git", "ls-files", "--error-unmatch", str(lock_path)],
        cwd=REPO_ROOT,
        capture_output=True,
        text=True,
        check=False,
    )

    assert tracked.returncode == 0, tracked.stderr
    lock = _load_hcl(lock_path)
    provider = lock["provider"][0]['"registry.terraform.io/hashicorp/google"']
    assert _unquote(provider["version"]) == "7.41.0"
    assert _unquote(provider["constraints"]) == "7.41.0"
    assert provider["hashes"]


def _valid_preflight() -> dict[str, Any]:
    return {
        "active_account": ACCOUNT,
        "active_project": PROJECT_ID,
        "billing_enabled": True,
        "blockers": [],
        "project_lifecycle": "ACTIVE",
        "project_number": PROJECT_NUMBER,
        "ready_for_archive": True,
    }


def _project_iam() -> dict[str, Any]:
    return {
        "bindings": [
            {
                "role": "roles/owner",
                "members": [
                    f"user:{ACCOUNT}",
                    "serviceAccount:motion-app@motionexpaiweb.iam.gserviceaccount.com",
                ],
            },
            {
                "role": "roles/storage.admin",
                "members": [
                    "serviceAccount:archive-app@motionexpaiweb.iam.gserviceaccount.com"
                ],
            },
            {
                "role": "roles/storage.objectAdmin",
                "members": [
                    "serviceAccount:object-worker@motionexpaiweb.iam.gserviceaccount.com"
                ],
            },
            {
                "role": "roles/viewer",
                "members": ["group:auditors@motionexp.com"],
                "condition": {
                    "title": "review-window",
                    "description": "Test-only conditional binding",
                    "expression": "request.time < timestamp('2030-01-01T00:00:00Z')",
                },
            },
        ],
        "etag": "live-project-etag",
        "version": 3,
    }


def _organization_iam() -> dict[str, Any]:
    return {
        "bindings": [
            {
                "role": "roles/storage.objectAdmin",
                "members": [f"user:{ACCOUNT}"],
            },
            {
                "role": "roles/resourcemanager.organizationViewer",
                "members": [f"user:{ACCOUNT}"],
            },
        ],
        "etag": "live-organization-etag",
        "version": 1,
    }


def _canonical_policy(policy: dict[str, Any]) -> dict[str, Any]:
    bindings = []
    for binding in policy["bindings"]:
        canonical_binding: dict[str, Any] = {
            "role": binding["role"],
            "members": sorted(binding["members"]),
        }
        if "condition" in binding:
            condition = binding["condition"]
            canonical_condition = {
                "title": condition["title"],
                "expression": condition["expression"],
            }
            if "description" in condition:
                canonical_condition["description"] = condition["description"]
            canonical_binding["condition"] = canonical_condition
        bindings.append(canonical_binding)
    bindings.sort(
        key=lambda binding: json.dumps(
            binding, ensure_ascii=False, separators=(",", ":")
        )
    )
    return {"version": policy.get("version", 1), "bindings": bindings}


def _policy_digest(policy: dict[str, Any]) -> str:
    canonical = json.dumps(
        _canonical_policy(policy),
        ensure_ascii=False,
        separators=(",", ":"),
    )
    return hashlib.sha256(canonical.encode()).hexdigest()


def _accepted_inherited_iam() -> dict[str, Any]:
    project_policy = _canonical_policy(_project_iam())
    organization_policy = _canonical_policy(_organization_iam())
    principals = sorted(
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
        "accepted_principals": principals,
        "rationale": [
            "Existing application service accounts are accepted only through the exact reviewed snapshots.",
            "Broad user instruction accepts the current security posture captured by these snapshots.",
            "No IAM removal is authorized by this bootstrap.",
        ],
        "project_policy": {
            "sha256": _policy_digest(project_policy),
            "policy": project_policy,
        },
        "organization_policy": {
            "sha256": _policy_digest(organization_policy),
            "policy": organization_policy,
        },
    }


def _pending_inherited_iam() -> dict[str, Any]:
    contract = _accepted_inherited_iam()
    contract["status"] = "pending_live_snapshot"
    contract["accepted_principals"] = []
    contract["project_policy"] = None
    contract["organization_policy"] = None
    return contract


def _run_acceptance_builder(
    tmp_path: Path,
    *,
    project_description: dict[str, Any] | None = None,
    project_policy: dict[str, Any] | None = None,
    organization_policy: dict[str, Any] | None = None,
) -> subprocess.CompletedProcess[str]:
    inputs = {
        "project-description.json": project_description
        or {
            "projectId": PROJECT_ID,
            "projectNumber": PROJECT_NUMBER,
            "lifecycleState": "ACTIVE",
            "parent": {"type": "organization", "id": ORGANIZATION_ID},
        },
        "project-policy.json": project_policy or _project_iam(),
        "organization-policy.json": organization_policy or _organization_iam(),
    }
    for name, value in inputs.items():
        (tmp_path / name).write_text(json.dumps(value), encoding="utf-8")

    return subprocess.run(
        [
            sys.executable,
            str(ACCEPTANCE_BUILDER),
            "--project-description",
            str(tmp_path / "project-description.json"),
            "--project-policy",
            str(tmp_path / "project-policy.json"),
            "--organization-policy",
            str(tmp_path / "organization-policy.json"),
        ],
        cwd=tmp_path,
        capture_output=True,
        text=True,
        check=False,
    )


def test_acceptance_builder_emits_exact_contract_to_stdout_only(tmp_path: Path) -> None:
    before = sorted(path.name for path in tmp_path.iterdir())
    completed = _run_acceptance_builder(tmp_path)
    after = sorted(path.name for path in tmp_path.iterdir())

    assert completed.returncode == 0, completed.stderr
    assert json.loads(completed.stdout) == _accepted_inherited_iam()
    assert "etag" not in completed.stdout.lower()
    assert completed.stderr == ""
    assert after == sorted(
        before
        + [
            "organization-policy.json",
            "project-description.json",
            "project-policy.json",
        ]
    )


def test_acceptance_builder_canonicalizes_order_without_changing_output(
    tmp_path: Path,
) -> None:
    project_policy = _project_iam()
    project_policy["bindings"].reverse()
    project_policy["bindings"][0]["members"].reverse()
    condition = project_policy["bindings"][0]["condition"]
    project_policy["bindings"][0]["condition"] = {
        "expression": condition["expression"],
        "description": condition["description"],
        "title": condition["title"],
    }
    organization_policy = _organization_iam()
    organization_policy["bindings"].reverse()

    completed = _run_acceptance_builder(
        tmp_path,
        project_policy=project_policy,
        organization_policy=organization_policy,
    )

    assert completed.returncode == 0, completed.stderr
    assert json.loads(completed.stdout) == _accepted_inherited_iam()


@pytest.mark.parametrize(
    "mutation",
    ["wrong_parent", "unknown_policy_field", "malformed_condition"],
)
def test_acceptance_builder_fails_closed_without_stdout(
    tmp_path: Path, mutation: str
) -> None:
    project_description = {
        "projectId": PROJECT_ID,
        "projectNumber": PROJECT_NUMBER,
        "lifecycleState": "ACTIVE",
        "parent": {"type": "organization", "id": ORGANIZATION_ID},
    }
    project_policy = _project_iam()
    if mutation == "wrong_parent":
        project_description["parent"] = {"type": "folder", "id": "123456789"}
    elif mutation == "unknown_policy_field":
        project_policy["auditConfigs"] = []
    else:
        project_policy["bindings"][3]["condition"].pop("expression")

    completed = _run_acceptance_builder(
        tmp_path,
        project_description=project_description,
        project_policy=project_policy,
    )

    assert completed.returncode != 0
    assert completed.stdout == ""
    assert completed.stderr.strip()


def _valid_bucket() -> dict[str, Any]:
    return {
        "name": BACKEND_BUCKET,
        "projectNumber": PROJECT_NUMBER,
        "location": "US-CENTRAL1",
        "iamConfiguration": {
            "uniformBucketLevelAccess": {"enabled": True},
            "publicAccessPrevention": "enforced",
        },
        "versioning": {"enabled": True},
        "softDeletePolicy": {"retentionDurationSeconds": "604800"},
        "labels": {
            "managed-by": "hk-rag-backend-bootstrap",
            "purpose": "terraform-state",
        },
    }


def _standardized_bucket() -> dict[str, Any]:
    return {
        "name": BACKEND_BUCKET,
        "location": "US-CENTRAL1",
        "iam_configuration": {
            "uniform_bucket_level_access": {"enabled": True},
            "public_access_prevention": "enforced",
        },
        "versioning": {"enabled": True},
        "soft_delete_policy": {"retention_duration_seconds": "604800"},
        "labels": {
            "managed-by": "hk-rag-backend-bootstrap",
            "purpose": "terraform-state",
        },
    }


def _target_iam(*, etag: str = "target-etag") -> dict[str, Any]:
    return {
        "bindings": [
            {
                "role": "roles/storage.legacyBucketOwner",
                "members": [f"user:{ACCOUNT}"],
            },
            {
                "role": "roles/storage.objectAdmin",
                "members": [f"user:{ACCOUNT}"],
            }
        ],
        "etag": etag,
        "version": 1,
    }


def _default_iam(
    *, project_id: str = PROJECT_ID, etag: str = "default-etag"
) -> dict[str, Any]:
    return {
        "bindings": [
            {
                "role": "roles/storage.legacyBucketOwner",
                "members": [
                    f"projectEditor:{project_id}",
                    f"projectOwner:{project_id}",
                ],
            },
            {
                "role": "roles/storage.legacyBucketReader",
                "members": [f"projectViewer:{project_id}"],
            },
            {
                "role": "roles/storage.legacyObjectOwner",
                "members": [
                    f"projectEditor:{project_id}",
                    f"projectOwner:{project_id}",
                ],
            },
            {
                "role": "roles/storage.legacyObjectReader",
                "members": [f"projectViewer:{project_id}"],
            },
        ],
        "etag": etag,
        "version": 1,
    }


def _valid_state() -> dict[str, Any]:
    return {
        "account": ACCOUNT,
        "configured_account": ACCOUNT,
        "configured_project": PROJECT_ID,
        "project": {
            "projectId": PROJECT_ID,
            "projectNumber": PROJECT_NUMBER,
            "lifecycleState": "ACTIVE",
            "parent": {"type": "organization", "id": ORGANIZATION_ID},
        },
        "billing": {"billingEnabled": True, "projectId": PROJECT_ID},
        "project_iam": _project_iam(),
        "organization_iam": _organization_iam(),
        "bucket_status": "exists",
        "bucket": _valid_bucket(),
        "standardized_bucket": _standardized_bucket(),
        "iam": _target_iam(),
    }


FAKE_GCLOUD = r'''
import json
import os
from pathlib import Path
import sys

state_path = Path(os.environ["FAKE_GCLOUD_STATE"])
log_path = Path(os.environ["FAKE_GCLOUD_LOG"])
state = json.loads(state_path.read_text(encoding="utf-8"))
args = sys.argv[1:]
with log_path.open("a", encoding="utf-8") as handle:
    handle.write(json.dumps(args) + "\n")

forced_error_prefix = state.get("forced_error_prefix")
if forced_error_prefix and args[:len(forced_error_prefix)] == forced_error_prefix:
    print('{"must_not_leak":true}')
    print("ERROR:\x01 simulated forced failure", file=sys.stderr)
    raise SystemExit(1)

if args[:2] == ["auth", "list"]:
    print(json.dumps([{"account": state["account"], "status": "ACTIVE"}]))
elif args[:2] == ["config", "list"]:
    print(json.dumps({"core": {
        "account": state["configured_account"],
        "project": state["configured_project"],
    }}))
elif args[:2] == ["projects", "describe"]:
    print(json.dumps(state["project"]))
elif args[:2] == ["projects", "get-iam-policy"]:
    if state.get("project_iam_status") == "error":
        print("ERROR: simulated project IAM query failure", file=sys.stderr)
        raise SystemExit(1)
    print(json.dumps(state["project_iam"]))
elif args[:2] == ["organizations", "get-iam-policy"]:
    if state.get("organization_iam_status") == "error":
        print("ERROR: simulated organization IAM query failure", file=sys.stderr)
        raise SystemExit(1)
    print(json.dumps(state["organization_iam"]))
elif args[:3] == ["billing", "projects", "describe"]:
    print(json.dumps(state["billing"]))
elif args[:3] == ["storage", "buckets", "describe"]:
    if state["bucket_status"] == "absent":
        print(
            "ERROR: (gcloud.storage.buckets.describe) "
            "gs://motionexpaiweb-880586285913-hk-movie-rag-tfstate "
            "not found: 404.",
            file=sys.stderr,
        )
        raise SystemExit(1)
    if state["bucket_status"] == "forbidden":
        print(
            "ERROR: HTTPError 403: The bucket does not exist or you do not have access.",
            file=sys.stderr,
        )
        raise SystemExit(1)
    if state["bucket_status"] == "not_found_without_404":
        print("ERROR: requested bucket was not found.", file=sys.stderr)
        raise SystemExit(1)
    if state["bucket_status"] == "not_found_404_with_permission_error":
        print(
            "ERROR: requested bucket was not found: 404. Permission denied: 403.",
            file=sys.stderr,
        )
        raise SystemExit(1)
    if state["bucket_status"] == "stdout_404":
        print(
            "ERROR: (gcloud.storage.buckets.describe) "
            "gs://motionexpaiweb-880586285913-hk-movie-rag-tfstate "
            "not found: 404."
        )
        print("ERROR: unrelated storage failure", file=sys.stderr)
        raise SystemExit(1)
    raw_argv = [
        "storage",
        "buckets",
        "describe",
        "gs://motionexpaiweb-880586285913-hk-movie-rag-tfstate",
        "--project=motionexpaiweb",
        "--raw",
        "--format=json",
    ]
    standardized_argv = [
        "storage",
        "buckets",
        "describe",
        "gs://motionexpaiweb-880586285913-hk-movie-rag-tfstate",
        "--project=motionexpaiweb",
        "--format=json",
    ]
    if args == raw_argv:
        print(json.dumps(state["bucket"]))
    elif args == standardized_argv:
        print(json.dumps(state["standardized_bucket"]))
    else:
        print(f"unexpected bucket describe argv: {args!r}", file=sys.stderr)
        raise SystemExit(2)
elif args[:3] == ["storage", "buckets", "get-iam-policy"]:
    print(json.dumps(state["iam"]))
elif args[:3] == ["storage", "buckets", "create"]:
    if state["bucket_status"] != "absent":
        print("ERROR: bucket already exists", file=sys.stderr)
        raise SystemExit(1)
    state["bucket_status"] = "exists"
    state["bucket"] = {
        "name": "motionexpaiweb-880586285913-hk-movie-rag-tfstate",
        "projectNumber": "880586285913",
        "location": "US-CENTRAL1",
        "iamConfiguration": {
            "uniformBucketLevelAccess": {"enabled": True},
            "publicAccessPrevention": "enforced",
        },
        "versioning": {"enabled": False},
        "softDeletePolicy": {"retentionDurationSeconds": "604800"},
    }
    state["iam"] = {
        "bindings": [
            {
                "role": "roles/storage.legacyBucketOwner",
                "members": [
                    "projectEditor:motionexpaiweb",
                    "projectOwner:motionexpaiweb",
                ],
            },
            {
                "role": "roles/storage.legacyBucketReader",
                "members": ["projectViewer:motionexpaiweb"],
            },
            {
                "role": "roles/storage.legacyObjectOwner",
                "members": [
                    "projectEditor:motionexpaiweb",
                    "projectOwner:motionexpaiweb",
                ],
            },
            {
                "role": "roles/storage.legacyObjectReader",
                "members": ["projectViewer:motionexpaiweb"],
            },
        ],
        "etag": "default-etag",
        "version": 1,
    }
    state_path.write_text(json.dumps(state), encoding="utf-8")
    print(json.dumps(state["bucket"]))
elif args[:3] == ["storage", "buckets", "update"]:
    if "--versioning" in args:
        state["bucket"]["versioning"] = {"enabled": True}
    labels_file = next(
        (arg.removeprefix("--labels-file=") for arg in args
         if arg.startswith("--labels-file=")),
        None,
    )
    if labels_file is not None:
        labels_path = Path(labels_file)
        labels = json.loads(labels_path.read_text(encoding="utf-8-sig"))
        try:
            with labels_path.open("a", encoding="utf-8"):
                pass
            write_blocked = False
        except PermissionError:
            write_blocked = True
        try:
            labels_path.unlink()
            delete_blocked = False
        except PermissionError:
            delete_blocked = True
        state.setdefault("file_probes", []).append(
            {
                "delete_blocked": delete_blocked,
                "kind": "labels",
                "path": str(labels_path),
                "write_blocked": write_blocked,
            }
        )
        state["bucket"]["labels"] = labels
    if state.get("update_failure"):
        state_path.write_text(json.dumps(state), encoding="utf-8")
        print("ERROR: simulated bucket update failure", file=sys.stderr)
        raise SystemExit(1)
    state_path.write_text(json.dumps(state), encoding="utf-8")
    print(json.dumps(state["bucket"]))
elif args[:3] == ["storage", "buckets", "set-iam-policy"]:
    policy_path = Path(args[4])
    requested_etag = next(
        (arg.removeprefix("--etag=") for arg in args if arg.startswith("--etag=")),
        None,
    )
    if requested_etag != state["iam"].get("etag"):
        print("ERROR: IAM etag precondition failed", file=sys.stderr)
        raise SystemExit(1)
    policy = json.loads(policy_path.read_text(encoding="utf-8-sig"))
    try:
        with policy_path.open("a", encoding="utf-8"):
            pass
        write_blocked = False
    except PermissionError:
        write_blocked = True
    try:
        policy_path.unlink()
        delete_blocked = False
    except PermissionError:
        delete_blocked = True
    state.setdefault("file_probes", []).append(
        {
            "delete_blocked": delete_blocked,
            "kind": "iam-policy",
            "path": str(policy_path),
            "write_blocked": write_blocked,
        }
    )
    state.setdefault("policy_writes", []).append(
        {"etag": requested_etag, "path": str(policy_path), "policy": policy}
    )
    if state.get("set_iam_failure"):
        state_path.write_text(json.dumps(state), encoding="utf-8")
        print("ERROR: simulated IAM policy update failure", file=sys.stderr)
        raise SystemExit(1)
    state["iam"] = {**policy, "etag": "target-etag-after-set"}
    state_path.write_text(json.dumps(state), encoding="utf-8")
    print(json.dumps(state["iam"]))
else:
    print(f"unexpected fake gcloud argv: {args!r}", file=sys.stderr)
    raise SystemExit(2)

if any(
    args[:len(prefix)] == prefix
    for prefix in state.get("success_stderr_prefixes", [])
):
    print(
        state.get("success_stderr_text", "simulated clean-success gcloud warning"),
        file=sys.stderr,
    )
'''


def _run_bootstrap(
    tmp_path: Path,
    *,
    mode: str = "Check",
    preflight: dict[str, Any] | None = None,
    state: dict[str, Any] | None = None,
    acceptance: dict[str, Any] | None | object = _DEFAULT_ACCEPTANCE,
) -> tuple[subprocess.CompletedProcess[str], list[list[str]], dict[str, Any]]:
    assert BOOTSTRAP_SCRIPT.exists(), "backend bootstrap script is missing"
    powershell = shutil.which("pwsh") or shutil.which("powershell")
    assert powershell is not None, "PowerShell is required for backend bootstrap tests"

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
    if acceptance is _DEFAULT_ACCEPTANCE:
        acceptance = _accepted_inherited_iam()
    if acceptance is not None:
        acceptance_path.write_text(json.dumps(acceptance), encoding="utf-8")
    fake_python.write_text(FAKE_GCLOUD, encoding="utf-8")
    fake_command.write_text("# fake gcloud launcher marker\n", encoding="utf-8")

    env = os.environ.copy()
    env["FAKE_GCLOUD_STATE"] = str(state_path)
    env["FAKE_GCLOUD_LOG"] = str(log_path)
    env["CLOUDSDK_PYTHON"] = sys.executable

    command = [
            powershell,
            "-NoProfile",
            "-File",
            str(BOOTSTRAP_SCRIPT),
            "-Mode",
            mode,
            "-PreflightPath",
            str(preflight_path),
            "-GcloudExecutable",
            str(fake_command),
        ]
    if acceptance is not None:
        command.extend(["-InheritedIamAcceptancePath", str(acceptance_path)])
    completed = subprocess.run(
        command,
        cwd=REPO_ROOT,
        env=env,
        capture_output=True,
        encoding="utf-8",
        errors="replace",
        check=False,
        timeout=30,
    )
    assert "A parameter cannot be found" not in completed.stderr
    calls = []
    if log_path.exists():
        calls = [json.loads(line) for line in log_path.read_text(encoding="utf-8").splitlines()]
    final_state = json.loads(state_path.read_text(encoding="utf-8"))
    return completed, calls, final_state


def _bucket_calls(calls: list[list[str]]) -> list[list[str]]:
    return [call for call in calls if call[:2] == ["storage", "buckets"]]


def _bucket_describe_calls(calls: list[list[str]]) -> list[list[str]]:
    return [call for call in calls if call[:3] == ["storage", "buckets", "describe"]]


def _mutating_calls(calls: list[list[str]]) -> list[list[str]]:
    return [call for call in calls if call[:3] in (
        ["storage", "buckets", "create"],
        ["storage", "buckets", "update"],
        ["storage", "buckets", "set-iam-policy"],
    )]


def test_inherited_iam_acceptance_is_order_independent_and_precedes_bucket_access(
    tmp_path: Path,
) -> None:
    state = _valid_state()
    state["project_iam"]["bindings"].reverse()
    state["project_iam"]["bindings"][0]["members"].reverse()
    state["organization_iam"]["bindings"].reverse()

    completed, calls, _ = _run_bootstrap(
        tmp_path, state=state, acceptance=_accepted_inherited_iam()
    )

    assert completed.returncode == 0, completed.stderr
    assert json.loads(completed.stdout)["status"] == "ready_existing"
    project_iam_index = next(
        index
        for index, call in enumerate(calls)
        if call[:2] == ["projects", "get-iam-policy"]
    )
    organization_iam_index = next(
        index
        for index, call in enumerate(calls)
        if call[:2] == ["organizations", "get-iam-policy"]
    )
    bucket_index = next(
        index
        for index, call in enumerate(calls)
        if call[:2] == ["storage", "buckets"]
    )
    assert project_iam_index < organization_iam_index < bucket_index


def test_pending_inherited_iam_acceptance_fails_before_gcloud(tmp_path: Path) -> None:
    completed, calls, _ = _run_bootstrap(
        tmp_path, acceptance=_pending_inherited_iam()
    )

    assert completed.returncode != 0
    assert calls == []


@pytest.mark.parametrize(
    "mutation",
    [
        "project_binding_added",
        "project_binding_removed",
        "project_member_added",
        "project_condition_changed",
        "organization_binding_removed",
        "organization_member_added",
        "project_unknown_field",
        "organization_unknown_field",
    ],
)
def test_inherited_iam_drift_fails_before_bucket_access(
    tmp_path: Path, mutation: str
) -> None:
    state = _valid_state()
    if mutation == "project_binding_added":
        state["project_iam"]["bindings"].append(
            {"role": "roles/browser", "members": [f"user:{ACCOUNT}"]}
        )
    elif mutation == "project_binding_removed":
        state["project_iam"]["bindings"].pop()
    elif mutation == "project_member_added":
        state["project_iam"]["bindings"][0]["members"].append(
            "user:outsider@example.com"
        )
    elif mutation == "project_condition_changed":
        state["project_iam"]["bindings"][3]["condition"]["expression"] = "true"
    elif mutation == "organization_binding_removed":
        state["organization_iam"]["bindings"].pop()
    elif mutation == "organization_member_added":
        state["organization_iam"]["bindings"][0]["members"].append(
            "serviceAccount:unexpected@motionexpaiweb.iam.gserviceaccount.com"
        )
    elif mutation == "project_unknown_field":
        state["project_iam"]["auditConfigs"] = []
    elif mutation == "organization_unknown_field":
        state["organization_iam"]["unexpected"] = True

    completed, calls, _ = _run_bootstrap(
        tmp_path, mode="Ensure", state=state, acceptance=_accepted_inherited_iam()
    )

    assert completed.returncode != 0
    assert _bucket_calls(calls) == []
    assert _mutating_calls(calls) == []


def test_folder_parent_fails_before_inherited_iam_and_bucket_queries(
    tmp_path: Path,
) -> None:
    state = _valid_state()
    state["project"]["parent"] = {"type": "folder", "id": "123456789"}

    completed, calls, _ = _run_bootstrap(
        tmp_path, mode="Ensure", state=state, acceptance=_accepted_inherited_iam()
    )

    assert completed.returncode != 0
    assert not any(call[:2] == ["projects", "get-iam-policy"] for call in calls)
    assert not any(call[:2] == ["organizations", "get-iam-policy"] for call in calls)
    assert _bucket_calls(calls) == []


@pytest.mark.parametrize("scope", ["project", "organization"])
def test_inherited_iam_query_failure_precedes_bucket_access(
    tmp_path: Path, scope: str
) -> None:
    state = _valid_state()
    state[f"{scope}_iam_status"] = "error"

    completed, calls, _ = _run_bootstrap(
        tmp_path, mode="Ensure", state=state, acceptance=_accepted_inherited_iam()
    )

    assert completed.returncode != 0
    assert _bucket_calls(calls) == []
    assert _mutating_calls(calls) == []


@pytest.mark.parametrize("mutation", ["digest", "unknown_field", "principal_list"])
def test_invalid_inherited_acceptance_fails_before_gcloud(
    tmp_path: Path, mutation: str
) -> None:
    acceptance = _accepted_inherited_iam()
    if mutation == "digest":
        acceptance["project_policy"]["sha256"] = "0" * 64
    elif mutation == "unknown_field":
        acceptance["unexpected"] = True
    elif mutation == "principal_list":
        acceptance["accepted_principals"].append("user:outsider@example.com")

    completed, calls, _ = _run_bootstrap(tmp_path, acceptance=acceptance)

    assert completed.returncode != 0
    assert calls == []


def test_checked_in_inherited_acceptance_is_tracked_accepted_and_has_no_etag() -> None:
    tracked = subprocess.run(
        ["git", "ls-files", "--error-unmatch", str(ACCEPTANCE_PATH)],
        cwd=REPO_ROOT,
        capture_output=True,
        text=True,
        check=False,
    )

    assert tracked.returncode == 0, tracked.stderr
    contract = json.loads(ACCEPTANCE_PATH.read_text(encoding="utf-8"))
    assert contract["status"] == "accepted"
    assert contract["project"] == {
        "id": PROJECT_ID,
        "number": PROJECT_NUMBER,
        "parent": {"type": "organization", "id": ORGANIZATION_ID},
    }
    assert len(contract["accepted_principals"]) == 24
    assert contract["project_policy"]["sha256"] == (
        "bbd7ea0bbbebdec16bb6988330f013bf7f3bf1d5c7c298e407f0270116fe3d93"
    )
    assert contract["organization_policy"]["sha256"] == (
        "e194d4659e3da6f8600671d590a8b753768e5c0c5a86b433834f70715cabf572"
    )
    for scope in ("project_policy", "organization_policy"):
        assert contract[scope]["sha256"] == _policy_digest(contract[scope]["policy"])
    assert contract["project_policy"]["policy"]["version"] == 3
    assert [
        binding
        for binding in contract["project_policy"]["policy"]["bindings"]
        if binding.get("condition", {}).get("title") == "hk-rag-archive-operator"
    ] == [
        {
            "role": "roles/storage.admin",
            "members": [f"user:{ACCOUNT}"],
            "condition": {
                "title": "hk-rag-archive-operator",
                "expression": (
                    'resource.type == "storage.googleapis.com/Bucket" && '
                    'resource.name == "projects/_/buckets/'
                    'motionexpaiweb-880586285913-hk-movie-rag-source-archive"'
                ),
                "description": (
                    "Persistent bucket-scoped management access for the HK Movie "
                    "RAG source archive."
                ),
            },
        }
    ]
    assert "etag" not in ACCEPTANCE_PATH.read_text(encoding="utf-8").lower()


def test_checked_in_inherited_acceptance_loads_and_matches_live_shape(
    tmp_path: Path,
) -> None:
    contract = json.loads(ACCEPTANCE_PATH.read_text(encoding="utf-8"))
    state = _valid_state()
    state["project_iam"] = {
        **contract["project_policy"]["policy"],
        "etag": "volatile-project-etag",
    }
    state["organization_iam"] = {
        **contract["organization_policy"]["policy"],
        "etag": "volatile-organization-etag",
    }

    completed, calls, _ = _run_bootstrap(
        tmp_path, state=state, acceptance=contract
    )

    assert completed.returncode == 0, completed.stderr
    assert json.loads(completed.stdout)["status"] == "ready_existing"
    assert any(call[:2] == ["projects", "get-iam-policy"] for call in calls)
    assert any(call[:2] == ["organizations", "get-iam-policy"] for call in calls)


def test_check_mode_reports_absent_bucket_without_mutating(tmp_path: Path) -> None:
    state = _valid_state()
    state["bucket_status"] = "absent"

    completed, calls, final_state = _run_bootstrap(tmp_path, state=state)

    assert completed.returncode == 0, completed.stderr
    report = json.loads(completed.stdout)
    assert report == {
        "bucket": BACKEND_BUCKET,
        "mode": "Check",
        "mutated": False,
        "status": "would_create",
    }
    assert _mutating_calls(calls) == []
    assert final_state["bucket_status"] == "absent"


def test_ensure_mode_creates_only_the_exact_bucket_and_post_verifies(tmp_path: Path) -> None:
    state = _valid_state()
    state["bucket_status"] = "absent"

    completed, calls, final_state = _run_bootstrap(tmp_path, mode="Ensure", state=state)

    assert completed.returncode == 0, completed.stderr
    assert json.loads(completed.stdout)["status"] == "created"
    mutating_calls = _mutating_calls(calls)
    assert mutating_calls[0] == [
        "storage",
        "buckets",
        "create",
        BACKEND_URI,
        f"--project={PROJECT_ID}",
        f"--account={ACCOUNT}",
        "--location=US-CENTRAL1",
        "--uniform-bucket-level-access",
        "--public-access-prevention",
        "--soft-delete-duration=7d",
        "--quiet",
        "--format=json",
    ]
    assert mutating_calls[1][:7] == [
        "storage",
        "buckets",
        "update",
        BACKEND_URI,
        f"--project={PROJECT_ID}",
        f"--account={ACCOUNT}",
        "--versioning",
    ]
    assert mutating_calls[1][-2:] == ["--quiet", "--format=json"]
    labels_file_arg = mutating_calls[1][7]
    assert labels_file_arg.startswith("--labels-file=")
    assert not Path(labels_file_arg.removeprefix("--labels-file=")).exists()
    assert mutating_calls[2][:5] == [
        "storage",
        "buckets",
        "set-iam-policy",
        BACKEND_URI,
        final_state["policy_writes"][0]["path"],
    ]
    assert mutating_calls[2][5:] == [
        f"--project={PROJECT_ID}",
        f"--account={ACCOUNT}",
        "--etag=default-etag",
        "--quiet",
        "--format=json",
    ]
    assert final_state["policy_writes"][0]["policy"] == {
        "bindings": [
            {
                "members": [f"user:{ACCOUNT}"],
                "role": "roles/storage.legacyBucketOwner",
            },
            {
                "members": [f"user:{ACCOUNT}"],
                "role": "roles/storage.objectAdmin",
            }
        ],
        "version": 1,
    }
    assert not Path(final_state["policy_writes"][0]["path"]).exists()
    assert [
        {key: probe[key] for key in ("kind", "write_blocked", "delete_blocked")}
        for probe in final_state["file_probes"]
    ] == [
        {"kind": "labels", "write_blocked": True, "delete_blocked": True},
        {"kind": "iam-policy", "write_blocked": True, "delete_blocked": True},
    ]
    assert not any(call[:2] == ["auth", "print-access-token"] for call in calls)
    assert final_state["bucket"] == _valid_bucket()
    assert final_state["iam"] == _target_iam(etag="target-etag-after-set")


def test_bootstrap_interface_has_no_storage_api_endpoint_override() -> None:
    powershell = shutil.which("pwsh") or shutil.which("powershell")
    assert powershell is not None, "PowerShell is required for backend bootstrap tests"

    completed = subprocess.run(
        [
            powershell,
            "-NoProfile",
            "-Command",
            (
                f"(Get-Command '{BOOTSTRAP_SCRIPT}').Parameters.Keys "
                "| ConvertTo-Json -Compress"
            ),
        ],
        cwd=REPO_ROOT,
        capture_output=True,
        encoding="utf-8",
        errors="replace",
        check=False,
        timeout=30,
    )

    assert completed.returncode == 0, completed.stderr
    assert "StorageApiBaseUri" not in json.loads(completed.stdout)


def test_bootstrap_contains_no_raw_token_or_http_transport_path() -> None:
    source = BOOTSTRAP_SCRIPT.read_text(encoding="utf-8")

    for forbidden in (
        "Invoke-RestMethod",
        "Invoke-WebRequest",
        "print-access-token",
        "Authorization",
        "Bearer ",
        "StorageApiBaseUri",
    ):
        assert forbidden not in source


def test_gcloud_runner_uses_exact_argument_list_without_shell_evaluation() -> None:
    source = BOOTSTRAP_SCRIPT.read_text(encoding="utf-8")

    assert "$startInfo.UseShellExecute = $false" in source
    assert "$startInfo.ArgumentList.Add($argument)" in source
    for forbidden in ("Invoke-Expression", "Start-Process", ".Arguments ="):
        assert forbidden not in source


def test_existing_compatible_bucket_is_idempotent(tmp_path: Path) -> None:
    completed, calls, _ = _run_bootstrap(tmp_path, mode="Ensure")

    assert completed.returncode == 0, completed.stderr
    assert json.loads(completed.stdout)["status"] == "ready_existing"
    assert _mutating_calls(calls) == []


def test_bucket_describe_requests_exact_raw_storage_api_schema(tmp_path: Path) -> None:
    completed, calls, _ = _run_bootstrap(tmp_path)

    assert _bucket_describe_calls(calls) == [
        [
            "storage",
            "buckets",
            "describe",
            BACKEND_URI,
            f"--project={PROJECT_ID}",
            "--raw",
            "--format=json",
        ]
    ]
    assert completed.returncode == 0, completed.stderr
    assert json.loads(completed.stdout)["status"] == "ready_existing"
    assert _mutating_calls(calls) == []


def test_read_commands_parse_stdout_when_success_also_emits_stderr(
    tmp_path: Path,
) -> None:
    state = _valid_state()
    state["success_stderr_prefixes"] = [
        ["auth", "list"],
        ["config", "list"],
        ["projects", "describe"],
        ["projects", "get-iam-policy"],
        ["organizations", "get-iam-policy"],
        ["billing", "projects", "describe"],
        ["storage", "buckets", "describe"],
        ["storage", "buckets", "get-iam-policy"],
    ]

    completed, calls, _ = _run_bootstrap(tmp_path, state=state)

    assert completed.returncode == 0, completed.stderr
    assert json.loads(completed.stdout)["status"] == "ready_existing"
    assert "simulated clean-success" not in completed.stdout
    assert "simulated clean-success" not in completed.stderr
    assert _mutating_calls(calls) == []


def test_gcloud_failure_reports_sanitized_stderr_without_stdout(
    tmp_path: Path,
) -> None:
    state = _valid_state()
    state["forced_error_prefix"] = ["config", "list"]

    completed, calls, _ = _run_bootstrap(tmp_path, state=state)

    assert completed.returncode != 0
    assert [call[:2] for call in calls] == [["auth", "list"], ["config", "list"]]
    assert "simulated forced failure" in completed.stderr
    assert "ERROR:?" in completed.stderr
    assert "must_not_leak" not in completed.stderr
    assert "must_not_leak" not in completed.stdout


def test_mutations_parse_stdout_when_success_also_emits_stderr(tmp_path: Path) -> None:
    state = _valid_state()
    state["bucket_status"] = "absent"
    state["success_stderr_prefixes"] = [
        ["storage", "buckets", "create"],
        ["storage", "buckets", "update"],
        ["storage", "buckets", "set-iam-policy"],
    ]

    completed, calls, final_state = _run_bootstrap(
        tmp_path, mode="Ensure", state=state
    )

    assert completed.returncode == 0, completed.stderr
    assert json.loads(completed.stdout)["status"] == "created"
    assert [call[2] for call in _mutating_calls(calls)] == [
        "create",
        "update",
        "set-iam-policy",
    ]
    assert "simulated clean-success" not in completed.stdout
    assert "simulated clean-success" not in completed.stderr
    assert final_state["iam"] == _target_iam(etag="target-etag-after-set")


def test_check_mode_reports_exact_default_iam_without_mutating(tmp_path: Path) -> None:
    state = _valid_state()
    state["iam"] = _default_iam()
    state["iam"]["bindings"].reverse()
    state["iam"]["bindings"][-1]["members"].reverse()

    completed, calls, final_state = _run_bootstrap(tmp_path, state=state)

    assert completed.returncode == 0, completed.stderr
    assert json.loads(completed.stdout)["status"] == "would_harden_iam"
    assert _mutating_calls(calls) == []
    assert final_state["iam"] == state["iam"]


def test_ensure_mode_hardens_exact_default_iam_with_etag(tmp_path: Path) -> None:
    state = _valid_state()
    state["iam"] = _default_iam()

    completed, calls, final_state = _run_bootstrap(
        tmp_path, mode="Ensure", state=state
    )

    assert completed.returncode == 0, completed.stderr
    assert json.loads(completed.stdout)["status"] == "hardened_iam"
    assert len(_mutating_calls(calls)) == 1
    set_call = _mutating_calls(calls)[0]
    assert set_call[:4] == [
        "storage",
        "buckets",
        "set-iam-policy",
        BACKEND_URI,
    ]
    assert set_call[5:] == [
        f"--project={PROJECT_ID}",
        f"--account={ACCOUNT}",
        "--etag=default-etag",
        "--quiet",
        "--format=json",
    ]
    assert final_state["policy_writes"] == [
        {
            "etag": "default-etag",
            "path": set_call[4],
            "policy": {
                "bindings": [
                    {
                        "members": [f"user:{ACCOUNT}"],
                        "role": "roles/storage.legacyBucketOwner",
                    },
                    {
                        "members": [f"user:{ACCOUNT}"],
                        "role": "roles/storage.objectAdmin",
                    }
                ],
                "version": 1,
            },
        }
    ]
    assert not Path(set_call[4]).exists()
    assert final_state["iam"] == _target_iam(etag="target-etag-after-set")


def test_transient_iam_policy_file_is_removed_when_gcloud_fails(
    tmp_path: Path,
) -> None:
    state = _valid_state()
    state["iam"] = _default_iam()
    state["set_iam_failure"] = True

    completed, _, final_state = _run_bootstrap(tmp_path, mode="Ensure", state=state)

    assert completed.returncode != 0
    assert len(final_state["policy_writes"]) == 1
    assert not Path(final_state["policy_writes"][0]["path"]).exists()


def test_transient_labels_file_is_removed_when_gcloud_fails(tmp_path: Path) -> None:
    state = _valid_state()
    state["bucket_status"] = "absent"
    state["update_failure"] = True

    completed, _, final_state = _run_bootstrap(tmp_path, mode="Ensure", state=state)

    assert completed.returncode != 0
    labels_probe = next(
        probe for probe in final_state["file_probes"] if probe["kind"] == "labels"
    )
    assert not Path(labels_probe["path"]).exists()


def test_check_mode_reports_combined_versioning_and_iam_recovery(
    tmp_path: Path,
) -> None:
    state = _valid_state()
    state["bucket"]["versioning"]["enabled"] = False
    state["iam"] = _default_iam()

    completed, calls, final_state = _run_bootstrap(tmp_path, state=state)

    assert completed.returncode == 0, completed.stderr
    assert (
        json.loads(completed.stdout)["status"]
        == "would_recover_versioning_and_harden_iam"
    )
    assert _mutating_calls(calls) == []
    assert final_state["bucket"]["versioning"]["enabled"] is False
    assert final_state["iam"] == state["iam"]


def test_ensure_mode_recovers_versioning_before_hardening_iam(
    tmp_path: Path,
) -> None:
    state = _valid_state()
    state["bucket"]["versioning"]["enabled"] = False
    state["iam"] = _default_iam()

    completed, calls, final_state = _run_bootstrap(
        tmp_path, mode="Ensure", state=state
    )

    assert completed.returncode == 0, completed.stderr
    assert (
        json.loads(completed.stdout)["status"]
        == "recovered_versioning_and_hardened_iam"
    )
    assert [call[2] for call in _mutating_calls(calls)] == [
        "update",
        "set-iam-policy",
    ]
    assert final_state["bucket"]["versioning"]["enabled"] is True
    assert final_state["iam"] == _target_iam(etag="target-etag-after-set")


def _incompatible_iam_cases() -> list[dict[str, Any]]:
    outsider = _target_iam()
    outsider["bindings"][0]["members"].append("user:outsider@example.com")
    arbitrary_role = _target_iam()
    arbitrary_role["bindings"][0]["role"] = "roles/storage.admin"
    missing_binding = _default_iam()
    missing_binding["bindings"].pop()
    extra_binding = _default_iam()
    extra_binding["bindings"].append(
        {"role": "roles/storage.objectViewer", "members": [f"user:{ACCOUNT}"]}
    )
    missing_member = _default_iam()
    missing_member["bindings"][0]["members"].pop()
    extra_member = _default_iam()
    extra_member["bindings"][1]["members"].append("user:outsider@example.com")
    wrong_project = _default_iam(project_id="wrong-project")
    numeric_project = _default_iam(project_id=PROJECT_NUMBER)
    conditional = _target_iam()
    conditional["bindings"][0]["condition"] = {
        "title": "unexpected",
        "expression": "request.time < timestamp('2030-01-01T00:00:00Z')",
    }
    extra_envelope = _target_iam()
    extra_envelope["auditConfigs"] = []
    return [
        outsider,
        arbitrary_role,
        missing_binding,
        extra_binding,
        missing_member,
        extra_member,
        wrong_project,
        numeric_project,
        conditional,
        extra_envelope,
    ]


@pytest.mark.parametrize("iam", _incompatible_iam_cases())
def test_noncanonical_bucket_iam_fails_closed_without_mutating(
    tmp_path: Path, iam: dict[str, Any]
) -> None:
    state = _valid_state()
    state["iam"] = iam

    completed, calls, final_state = _run_bootstrap(
        tmp_path, mode="Ensure", state=state
    )

    assert completed.returncode != 0
    assert _mutating_calls(calls) == []
    assert final_state["iam"] == iam


def test_partial_labeled_bucket_is_recovered_with_versioning_only(tmp_path: Path) -> None:
    state = _valid_state()
    state["bucket"]["versioning"]["enabled"] = False

    completed, calls, final_state = _run_bootstrap(tmp_path, mode="Ensure", state=state)

    assert completed.returncode == 0, completed.stderr
    assert json.loads(completed.stdout)["status"] == "recovered_versioning"
    assert _mutating_calls(calls) == [
        [
            "storage",
            "buckets",
                "update",
                BACKEND_URI,
                f"--project={PROJECT_ID}",
                f"--account={ACCOUNT}",
                "--versioning",
            "--quiet",
            "--format=json",
        ]
    ]
    assert final_state["bucket"]["versioning"]["enabled"] is True


def test_check_mode_reports_partial_labeled_bucket_without_recovering(tmp_path: Path) -> None:
    state = _valid_state()
    state["bucket"]["versioning"]["enabled"] = False

    completed, calls, final_state = _run_bootstrap(tmp_path, state=state)

    assert completed.returncode == 0, completed.stderr
    assert json.loads(completed.stdout)["status"] == "would_recover_versioning"
    assert _mutating_calls(calls) == []
    assert final_state["bucket"]["versioning"]["enabled"] is False


@pytest.mark.parametrize(
    "labels",
    [
        {},
        {"managed-by": "hk-rag-backend-bootstrap"},
        {"managed-by": "forged", "purpose": "terraform-state"},
        {"managed-by": "hk-rag-backend-bootstrap", "purpose": "forged"},
        {
            "managed-by": "hk-rag-backend-bootstrap",
            "purpose": "terraform-state",
            "unexpected": "label",
        },
    ],
)
def test_partial_bucket_with_missing_or_wrong_labels_is_not_recovered(
    tmp_path: Path, labels: dict[str, str]
) -> None:
    state = _valid_state()
    state["bucket"]["versioning"]["enabled"] = False
    state["bucket"]["labels"] = labels

    completed, calls, _ = _run_bootstrap(tmp_path, mode="Ensure", state=state)

    assert completed.returncode != 0
    assert _mutating_calls(calls) == []


@pytest.mark.parametrize(
    ("path", "value"),
    [
        (("projectNumber",), "999999999999"),
        (("location",), "US-EAST1"),
        (("iamConfiguration", "uniformBucketLevelAccess", "enabled"), False),
        (("iamConfiguration", "publicAccessPrevention"), "inherited"),
        (("softDeletePolicy", "retentionDurationSeconds"), "1209600"),
        (("retentionPolicy",), {"isLocked": True, "retentionPeriod": "86400"}),
    ],
)
def test_existing_incompatible_bucket_fails_closed(
    tmp_path: Path, path: tuple[str, ...], value: Any
) -> None:
    state = _valid_state()
    target = state["bucket"]
    for key in path[:-1]:
        target = target[key]
    target[path[-1]] = value

    completed, calls, _ = _run_bootstrap(tmp_path, mode="Ensure", state=state)

    assert completed.returncode != 0
    assert _mutating_calls(calls) == []


@pytest.mark.parametrize(
    "path",
    [
        ("projectNumber",),
        ("iamConfiguration",),
        ("iamConfiguration", "uniformBucketLevelAccess"),
        ("iamConfiguration", "uniformBucketLevelAccess", "enabled"),
        ("iamConfiguration", "publicAccessPrevention"),
        ("softDeletePolicy",),
        ("softDeletePolicy", "retentionDurationSeconds"),
        ("versioning",),
        ("versioning", "enabled"),
    ],
)
def test_existing_bucket_missing_raw_ownership_or_security_field_fails_closed(
    tmp_path: Path, path: tuple[str, ...]
) -> None:
    state = _valid_state()
    target = state["bucket"]
    for key in path[:-1]:
        target = target[key]
    del target[path[-1]]

    completed, calls, _ = _run_bootstrap(tmp_path, mode="Ensure", state=state)

    assert completed.returncode != 0
    assert _mutating_calls(calls) == []


def test_standardized_bucket_payload_cannot_satisfy_raw_contract(
    tmp_path: Path,
) -> None:
    state = _valid_state()
    state["bucket"] = state["standardized_bucket"]

    completed, calls, _ = _run_bootstrap(tmp_path, mode="Ensure", state=state)

    assert _bucket_describe_calls(calls) == [
        [
            "storage",
            "buckets",
            "describe",
            BACKEND_URI,
            f"--project={PROJECT_ID}",
            "--raw",
            "--format=json",
        ]
    ]
    assert completed.returncode != 0
    assert _mutating_calls(calls) == []


def test_existing_public_iam_member_fails_closed(tmp_path: Path) -> None:
    state = _valid_state()
    state["iam"] = {
        "bindings": [{"role": "roles/storage.objectViewer", "members": ["allUsers"]}]
    }

    completed, calls, _ = _run_bootstrap(tmp_path, mode="Ensure", state=state)

    assert completed.returncode != 0
    assert _mutating_calls(calls) == []


def test_inaccessible_existing_bucket_is_not_treated_as_absent(tmp_path: Path) -> None:
    state = _valid_state()
    state["bucket_status"] = "forbidden"

    completed, calls, _ = _run_bootstrap(tmp_path, mode="Ensure", state=state)

    assert completed.returncode != 0
    assert _mutating_calls(calls) == []


@pytest.mark.parametrize(
    "bucket_status",
    [
        "not_found_without_404",
        "not_found_404_with_permission_error",
        "stdout_404",
    ],
)
def test_noncanonical_not_found_evidence_is_not_treated_as_absent(
    tmp_path: Path, bucket_status: str
) -> None:
    state = _valid_state()
    state["bucket_status"] = bucket_status

    completed, calls, _ = _run_bootstrap(tmp_path, mode="Ensure", state=state)

    assert completed.returncode != 0
    assert _mutating_calls(calls) == []


def test_success_stderr_with_not_found_404_is_not_treated_as_absent(
    tmp_path: Path,
) -> None:
    state = _valid_state()
    state["success_stderr_prefixes"] = [["storage", "buckets", "describe"]]
    state["success_stderr_text"] = (
        "ERROR: (gcloud.storage.buckets.describe) "
        f"{BACKEND_URI} not found: 404."
    )

    completed, calls, _ = _run_bootstrap(tmp_path, mode="Ensure", state=state)

    assert completed.returncode == 0, completed.stderr
    assert json.loads(completed.stdout)["status"] == "ready_existing"
    assert _mutating_calls(calls) == []


@pytest.mark.parametrize(
    ("field", "value"),
    [
        ("active_account", "other@example.com"),
        ("active_project", "wrong-project"),
        ("project_number", "999999999999"),
        ("billing_enabled", False),
        ("project_lifecycle", "DELETE_REQUESTED"),
        ("ready_for_archive", False),
        ("blockers", ["not_ready"]),
    ],
)
def test_invalid_preflight_fails_before_gcloud(
    tmp_path: Path, field: str, value: Any
) -> None:
    preflight = _valid_preflight()
    preflight[field] = value

    completed, calls, _ = _run_bootstrap(tmp_path, preflight=preflight)

    assert completed.returncode != 0
    assert calls == []


@pytest.mark.parametrize(
    ("field", "value"),
    [
        ("account", "other@example.com"),
        ("configured_account", "other@example.com"),
        ("configured_project", "wrong-project"),
    ],
)
def test_live_identity_mismatch_fails_before_bucket_access(
    tmp_path: Path, field: str, value: str
) -> None:
    state = _valid_state()
    state[field] = value

    completed, calls, _ = _run_bootstrap(tmp_path, state=state)

    assert completed.returncode != 0
    assert not any(call[:2] == ["storage", "buckets"] for call in calls)


def test_live_project_or_billing_mismatch_fails_before_bucket_access(tmp_path: Path) -> None:
    state = _valid_state()
    state["billing"]["billingEnabled"] = False

    completed, calls, _ = _run_bootstrap(tmp_path, state=state)

    assert completed.returncode != 0
    assert not any(call[:2] == ["storage", "buckets"] for call in calls)
