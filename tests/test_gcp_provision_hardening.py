from __future__ import annotations

import json
import os
import shutil
import subprocess
import sys
import threading
import time
from collections.abc import Iterator
from contextlib import contextmanager
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path

import pytest
from _gcp_secret_manager_fake import DB_SECRET, DEMO_SECRET, secret_manager_server

ROOT = Path(__file__).resolve().parents[1]
PROVISION = ROOT / "scripts" / "gcp" / "provision-demo.ps1"
_FAKE_SECRET = "R" * 43


def _powershell() -> str:
    executable = shutil.which("pwsh") or shutil.which("powershell")
    if executable is None:
        pytest.skip("PowerShell is unavailable")
    return executable


@contextmanager
def _sql_admin_server(scenario: str) -> Iterator[str]:
    class Handler(BaseHTTPRequestHandler):
        def log_message(self, format: str, *args: object) -> None:
            del format, args

        def do_POST(self) -> None:
            self._mutation()

        def do_PUT(self) -> None:
            self._mutation()

        def _mutation(self) -> None:
            self.rfile.read(int(self.headers.get("Content-Length", "0")))
            if scenario == "admin_api_timeout":
                time.sleep(5)
            body = b'{"name":"fixture-operation","status":"DONE"}'
            try:
                self.send_response(200)
                self.send_header("Content-Type", "application/json")
                self.send_header("Content-Length", str(len(body)))
                self.end_headers()
                self.wfile.write(body)
            except (BrokenPipeError, ConnectionResetError):
                pass

    server = ThreadingHTTPServer(("127.0.0.1", 0), Handler)
    thread = threading.Thread(target=server.serve_forever, daemon=True)
    thread.start()
    try:
        host, port = server.server_address
        yield f"http://{host}:{port}"
    finally:
        server.shutdown()
        thread.join(timeout=5)
        server.server_close()


def _install_fake_gcloud(tmp_path: Path) -> None:
    fake = tmp_path / "fake_provision_hardening.py"
    fake.write_text(
        r"""
import json
import os
from pathlib import Path
import subprocess
import sys
import time

args = sys.argv[1:]
key = tuple(item for item in args if not item.startswith("--"))
scenario = os.environ["FAKE_SCENARIO"]
project = "motionexpaiweb"
email = f"hk-rag-runtime@{project}.iam.gserviceaccount.com"
member = f"serviceAccount:{email}"
bucket = "motionexpaiweb-880586285913-hk-movie-rag-release"
secret = "fake-password-must-stay-redacted"

def start_survivor_and_hang():
    marker = os.environ["FAKE_SURVIVOR_MARKER"]
    child_code = (
        "import time; from pathlib import Path; time.sleep(2); "
        f"Path({marker!r}).write_text('survived', encoding='utf-8')"
    )
    subprocess.Popen([sys.executable, "-c", child_code])
    print(secret, file=sys.stderr, flush=True)
    time.sleep(30)

if scenario == "process_timeout":
    start_survivor_and_hang()

apis = [
    "run.googleapis.com", "cloudbuild.googleapis.com",
    "artifactregistry.googleapis.com", "secretmanager.googleapis.com",
    "sqladmin.googleapis.com", "sql-component.googleapis.com",
    "aiplatform.googleapis.com", "storage.googleapis.com",
]
instance = {
    "name": "hk-movie-rag-pg",
    "databaseVersion": "POSTGRES_16",
    "region": "us-central1",
    "settings": {
        "edition": "ENTERPRISE",
        "tier": "db-g1-small",
        "dataDiskSizeGb": "10",
        "dataDiskType": "PD_SSD",
        "availabilityType": "ZONAL",
        "storageAutoResize": scenario == "sql_auto_resize",
        "deletionProtectionEnabled": scenario != "sql_deletion_protection",
    },
}
if scenario not in {"sql_auto_resize", "sql_deletion_protection"}:
    instance["settings"]["storageAutoResize"] = False
    instance["settings"]["deletionProtectionEnabled"] = True
if scenario == "sql_missing_edition":
    del instance["settings"]["edition"]
elif scenario == "sql_wrong_edition":
    instance["settings"]["edition"] = "ENTERPRISE_PLUS"

project_bindings = [
    {"role": "roles/aiplatform.user", "members": [member]},
    {"role": "roles/cloudsql.client", "members": [member]},
]
if scenario == "extra_iam":
    project_bindings.append({"role": "roles/viewer", "members": [member]})
if scenario == "conditional_iam":
    project_bindings[0]["condition"] = {
        "title": "unexpected-condition",
        "expression": "request.time < timestamp('2099-01-01T00:00:00Z')",
    }

automatic_replication = {"replication": {"automatic": {}}}
drifted_replication = {
    "replication": {
        "userManaged": {"replicas": [{"location": "us-central1"}]}
    }
}
responses = {
    ("services", "list"): [{"config": {"name": name}} for name in apis],
    ("iam", "service-accounts", "list"): [
        {"email": email, "disabled": scenario == "disabled_sa"}
    ],
    ("artifacts", "repositories", "list"): [{
        "name": f"projects/{project}/locations/us-central1/repositories/hk-movie-rag",
        "format": "DOCKER",
    }],
    ("storage", "buckets", "list"): [{
        "name": bucket,
        "location": "US-CENTRAL1",
        "uniform_bucket_level_access": True,
        "public_access_prevention": "enforced",
    }],
    ("sql", "instances", "list"): [instance],
    ("secrets", "list"): [
        {"name": f"projects/{project}/secrets/hk-rag-db-password"},
        {"name": f"projects/{project}/secrets/hk-rag-demo-key"},
    ],
    ("secrets", "describe", "hk-rag-db-password"): (
        drifted_replication if scenario == "secret_replication" else automatic_replication
    ),
    ("secrets", "describe", "hk-rag-demo-key"): automatic_replication,
    ("secrets", "versions", "list", "hk-rag-db-password"): [
        {"name": "1", "state": "ENABLED"}
    ],
    ("secrets", "versions", "list", "hk-rag-demo-key"): [
        {"name": "1", "state": "ENABLED"}
    ],
    ("secrets", "versions", "access", "latest"): secret,
    ("sql", "databases", "list"): [{"name": "hk_movie_rag"}],
    ("sql", "users", "list"): [{"name": "hk_rag_app", "type": "BUILT_IN"}],
    ("auth", "print-access-token"): "fake-access-token",
    ("projects", "get-iam-policy", project): {"bindings": project_bindings},
    ("storage", "buckets", "get-iam-policy", f"gs://{bucket}"): {
        "bindings": [{"role": "roles/storage.objectViewer", "members": [member]}]
    },
    ("secrets", "get-iam-policy", "hk-rag-db-password"): {
        "bindings": [
            {"role": "roles/secretmanager.secretAccessor", "members": [member]}
        ]
    },
    ("secrets", "get-iam-policy", "hk-rag-demo-key"): {
        "bindings": [
            {"role": "roles/secretmanager.secretAccessor", "members": [member]}
        ]
    },
}

if scenario == "secret_omitted_bindings":
    responses[("secrets", "get-iam-policy", "hk-rag-db-password")] = {
        "etag": "ACAB"
    }
    responses[("secrets", "get-iam-policy", "hk-rag-demo-key")] = {
        "etag": "ACAB"
    }
elif scenario == "all_null_bindings":
    responses[("projects", "get-iam-policy", project)] = {
        "etag": "ACAB", "bindings": None
    }
    responses[("storage", "buckets", "get-iam-policy", f"gs://{bucket}")] = {
        "etag": "ACAB", "bindings": None
    }
    responses[("secrets", "get-iam-policy", "hk-rag-db-password")] = {
        "etag": "ACAB", "bindings": None
    }
    responses[("secrets", "get-iam-policy", "hk-rag-demo-key")] = {
        "etag": "ACAB", "bindings": None
    }
elif scenario == "bucket_extra_iam":
    responses[("storage", "buckets", "get-iam-policy", f"gs://{bucket}")][
        "bindings"
    ].append({"role": "roles/storage.admin", "members": [member]})
elif scenario == "bucket_conditional_iam":
    responses[("storage", "buckets", "get-iam-policy", f"gs://{bucket}")][
        "bindings"
    ][0]["condition"] = {"title": "unexpected", "expression": "true"}
elif scenario == "secret_extra_iam":
    responses[("secrets", "get-iam-policy", "hk-rag-db-password")][
        "bindings"
    ].append({"role": "roles/secretmanager.admin", "members": [member]})
elif scenario == "secret_conditional_iam":
    responses[("secrets", "get-iam-policy", "hk-rag-db-password")][
        "bindings"
    ][0]["condition"] = {"title": "unexpected", "expression": "true"}
elif scenario == "malformed_iam":
    responses[("secrets", "get-iam-policy", "hk-rag-db-password")] = {
        "etag": "ACAB", "bindings": [{"role": "roles/secretmanager.secretAccessor"}]
    }
elif scenario == "scalar_bindings":
    responses[("secrets", "get-iam-policy", "hk-rag-db-password")] = {
        "etag": "ACAB",
        "bindings": {
            "role": "roles/secretmanager.secretAccessor",
            "members": [member],
        },
    }
elif scenario == "scalar_members":
    responses[("secrets", "get-iam-policy", "hk-rag-db-password")] = {
        "etag": "ACAB",
        "bindings": [
            {
                "role": "roles/secretmanager.secretAccessor",
                "members": member,
            }
        ],
    }

mutating = {
    ("projects", "add-iam-policy-binding"),
    ("storage", "buckets", "add-iam-policy-binding"),
    ("secrets", "add-iam-policy-binding"),
}
with open(os.environ["FAKE_GCLOUD_LOG"], "a", encoding="utf-8") as stream:
    stream.write(json.dumps(args) + "\n")

if key in responses:
    value = responses[key]
    print(value if isinstance(value, str) else json.dumps(value))
elif any(key[:len(prefix)] == prefix for prefix in mutating):
    print("{}")
else:
    print(json.dumps({"unexpected": args}), file=sys.stderr)
    raise SystemExit(9)
""".strip()
        + "\n",
        encoding="utf-8",
    )
    (tmp_path / "gcloud.ps1").write_text(
        f'& "{sys.executable}" "{fake}" @args\nexit $LASTEXITCODE\n',
        encoding="utf-8",
    )


def _run_apply(
    tmp_path: Path,
    scenario: str,
    *,
    process_timeout_seconds: int | None = None,
) -> subprocess.CompletedProcess[str]:
    _install_fake_gcloud(tmp_path)
    env = os.environ.copy()
    env["PATH"] = f"{tmp_path}{os.pathsep}{env['PATH']}"
    env["FAKE_SCENARIO"] = scenario
    env["FAKE_SURVIVOR_MARKER"] = str(tmp_path / "survivor.txt")
    env["FAKE_GCLOUD_LOG"] = str(tmp_path / "gcloud-log.jsonl")
    command = [
        _powershell(),
        "-NoLogo",
        "-NoProfile",
        "-NonInteractive",
        "-File",
        str(PROVISION),
        "-ProjectId",
        "motionexpaiweb",
        "-Region",
        "us-central1",
        "-Apply",
    ]
    if process_timeout_seconds is not None:
        command.extend(["-ProcessTimeoutSeconds", str(process_timeout_seconds)])
    with (
        _sql_admin_server(scenario) as endpoint,
        secret_manager_server({DB_SECRET: _FAKE_SECRET.encode(), DEMO_SECRET: b"S" * 43}) as (
            secret_endpoint,
            _,
        ),
    ):
        env["RAG_DEMO_ALLOW_TEST_SQL_ADMIN_API"] = "1"
        env["RAG_DEMO_TEST_SQL_ADMIN_API_BASE_URI"] = endpoint
        env["RAG_DEMO_ALLOW_TEST_SECRET_MANAGER_API"] = "1"
        env["RAG_DEMO_TEST_SECRET_MANAGER_API_BASE_URI"] = secret_endpoint
        return subprocess.run(
            command,
            check=False,
            capture_output=True,
            text=True,
            encoding="utf-8",
            env=env,
            timeout=25,
        )


@pytest.mark.parametrize(
    ("scenario", "expected_error"),
    [
        ("disabled_sa", "runtime service account exists but is disabled"),
        (
            "extra_iam",
            "runtime service account project IAM has extra or conditional roles",
        ),
        (
            "conditional_iam",
            "runtime service account project IAM has extra or conditional roles",
        ),
        (
            "secret_replication",
            "secret hk-rag-db-password does not use exact automatic replication",
        ),
        (
            "sql_auto_resize",
            "Cloud SQL instance exists with incompatible immutable demo configuration",
        ),
        (
            "sql_deletion_protection",
            "Cloud SQL instance exists with incompatible immutable demo configuration",
        ),
        (
            "sql_missing_edition",
            "Cloud SQL instance exists with incompatible immutable demo configuration",
        ),
        (
            "sql_wrong_edition",
            "Cloud SQL instance exists with incompatible immutable demo configuration",
        ),
    ],
)
def test_apply_rejects_security_and_cloud_sql_drift(
    tmp_path: Path,
    scenario: str,
    expected_error: str,
) -> None:
    result = _run_apply(tmp_path, scenario)

    combined = result.stdout + result.stderr
    assert result.returncode != 0
    assert expected_error in combined
    assert '"status":"applied"' not in result.stdout
    assert _FAKE_SECRET not in combined


@pytest.mark.parametrize(
    ("scenario", "expected_binding_count"),
    [
        ("secret_omitted_bindings", 2),
        ("all_null_bindings", 5),
    ],
)
def test_apply_treats_omitted_or_null_iam_bindings_as_empty_policy(
    tmp_path: Path,
    scenario: str,
    expected_binding_count: int,
) -> None:
    result = _run_apply(tmp_path, scenario)

    assert result.returncode == 0, result.stdout + result.stderr
    assert json.loads(result.stdout)["status"] == "applied"
    calls = [
        json.loads(line)
        for line in (tmp_path / "gcloud-log.jsonl").read_text(encoding="utf-8").splitlines()
    ]
    iam_adds = [
        call
        for call in calls
        if call[:2] == ["projects", "add-iam-policy-binding"]
        or call[:3] == ["storage", "buckets", "add-iam-policy-binding"]
        or call[:2] == ["secrets", "add-iam-policy-binding"]
    ]
    assert len(iam_adds) == expected_binding_count


@pytest.mark.parametrize(
    ("scenario", "expected_error"),
    [
        ("bucket_extra_iam", "release bucket IAM has extra or conditional runtime bindings"),
        (
            "bucket_conditional_iam",
            "release bucket IAM has extra or conditional runtime bindings",
        ),
        (
            "secret_extra_iam",
            "secret hk-rag-db-password IAM has extra or conditional runtime bindings",
        ),
        (
            "secret_conditional_iam",
            "secret hk-rag-db-password IAM has extra or conditional runtime bindings",
        ),
        ("malformed_iam", "secret hk-rag-db-password IAM policy has malformed bindings"),
        ("scalar_bindings", "secret hk-rag-db-password IAM policy has malformed bindings"),
        ("scalar_members", "secret hk-rag-db-password IAM policy has malformed bindings"),
    ],
)
def test_apply_rejects_present_extra_conditional_or_malformed_iam_bindings(
    tmp_path: Path,
    scenario: str,
    expected_error: str,
) -> None:
    result = _run_apply(tmp_path, scenario)

    combined = result.stdout + result.stderr
    assert result.returncode != 0
    assert expected_error in combined
    assert '"status":"applied"' not in result.stdout
    assert _FAKE_SECRET not in combined


@pytest.mark.parametrize(
    ("scenario", "expected_error"),
    [
        (
            "process_timeout",
            "gcloud command timed out after 1 seconds without exposing command output",
        ),
        (
            "admin_api_timeout",
            "Cloud SQL Admin API request failed without exposing request or response content",
        ),
    ],
)
def test_apply_times_out_kills_the_process_tree_and_redacts_output(
    tmp_path: Path,
    scenario: str,
    expected_error: str,
) -> None:
    started = time.monotonic()
    result = _run_apply(tmp_path, scenario, process_timeout_seconds=1)
    elapsed = time.monotonic() - started

    combined = result.stdout + result.stderr
    assert result.returncode != 0
    assert elapsed < 15
    assert expected_error in combined
    assert _FAKE_SECRET not in combined
    time.sleep(2.5)
    assert not (tmp_path / "survivor.txt").exists()


def test_current_gcloud_sdk_supports_access_token_command() -> None:
    gcloud = shutil.which("gcloud")
    if gcloud is None:
        pytest.skip("gcloud is unavailable")

    result = subprocess.run(
        [gcloud, "auth", "print-access-token", "--help"],
        check=False,
        capture_output=True,
        text=True,
        encoding="utf-8",
        timeout=90,
    )

    assert result.returncode == 0, result.stderr
    assert "print-access-token" in result.stdout
