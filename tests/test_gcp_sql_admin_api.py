from __future__ import annotations

import json
import os
import shutil
import subprocess
import sys
import threading
from collections.abc import Iterator
from contextlib import contextmanager
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path
from typing import Any

import pytest
from _gcp_secret_manager_fake import DB_SECRET, DEMO_SECRET, secret_manager_server

ROOT = Path(__file__).resolve().parents[1]
PROVISION = ROOT / "scripts" / "gcp" / "provision-demo.ps1"
_PASSWORD = "P" * 43
_TOKEN = "fake-access-token"


def _powershell() -> str:
    executable = shutil.which("pwsh") or shutil.which("powershell")
    if executable is None:
        pytest.skip("PowerShell is unavailable")
    return executable


def _install_fake_gcloud(tmp_path: Path) -> Path:
    fake = tmp_path / "fake_gcloud_sql_admin.py"
    fake.write_text(
        r"""
import json
import os
import sys

args = sys.argv[1:]
key = tuple(item for item in args if not item.startswith("--"))
project = "motionexpaiweb"
email = f"hk-rag-runtime@{project}.iam.gserviceaccount.com"
member = f"serviceAccount:{email}"
bucket = "motionexpaiweb-880586285913-hk-movie-rag-release"
apis = [
    "run.googleapis.com", "cloudbuild.googleapis.com",
    "artifactregistry.googleapis.com", "secretmanager.googleapis.com",
    "sqladmin.googleapis.com", "sql-component.googleapis.com",
    "aiplatform.googleapis.com", "storage.googleapis.com",
]
responses = {
    ("services", "list"): [{"config": {"name": name}} for name in apis],
    ("iam", "service-accounts", "list"): [{"email": email, "disabled": False}],
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
    ("sql", "instances", "list"): [{
        "name": "hk-movie-rag-pg",
        "databaseVersion": "POSTGRES_16",
        "region": "us-central1",
        "settings": {
            "edition": "ENTERPRISE",
            "tier": "db-g1-small",
            "dataDiskSizeGb": "10",
            "dataDiskType": "PD_SSD",
            "availabilityType": "ZONAL",
            "storageAutoResize": False,
            "deletionProtectionEnabled": True,
        },
    }],
    ("secrets", "list"): [
        {"name": f"projects/{project}/secrets/hk-rag-db-password"},
        {"name": f"projects/{project}/secrets/hk-rag-demo-key"},
    ],
    ("secrets", "describe", "hk-rag-db-password"): {"replication": {"automatic": {}}},
    ("secrets", "describe", "hk-rag-demo-key"): {"replication": {"automatic": {}}},
    ("secrets", "versions", "list", "hk-rag-db-password"): [
        {"name": "1", "state": "ENABLED"}
    ],
    ("secrets", "versions", "list", "hk-rag-demo-key"): [
        {"name": "1", "state": "ENABLED"}
    ],
    ("secrets", "versions", "access", "latest"): os.environ["FAKE_DB_PASSWORD"],
    ("sql", "databases", "list"): [{"name": "hk_movie_rag"}],
    ("sql", "users", "list"): json.loads(os.environ["FAKE_SQL_USERS_JSON"]),
    ("auth", "print-access-token"): os.environ["FAKE_ACCESS_TOKEN"],
    ("projects", "get-iam-policy", project): {"bindings": [
        {"role": "roles/aiplatform.user", "members": [member]},
        {"role": "roles/cloudsql.client", "members": [member]},
    ]},
    ("storage", "buckets", "get-iam-policy", f"gs://{bucket}"): {
        "bindings": [{"role": "roles/storage.objectViewer", "members": [member]}]
    },
    ("secrets", "get-iam-policy", "hk-rag-db-password"): {
        "bindings": [{"role": "roles/secretmanager.secretAccessor", "members": [member]}]
    },
    ("secrets", "get-iam-policy", "hk-rag-demo-key"): {
        "bindings": [{"role": "roles/secretmanager.secretAccessor", "members": [member]}]
    },
}
with open(os.environ["FAKE_GCLOUD_LOG"], "a", encoding="utf-8") as stream:
    stream.write(json.dumps(args) + "\n")
if key not in responses:
    print(json.dumps({"unexpected": args}), file=sys.stderr)
    raise SystemExit(9)
value = responses[key]
print(value if isinstance(value, str) else json.dumps(value))
""".strip()
        + "\n",
        encoding="utf-8",
    )
    (tmp_path / "gcloud.ps1").write_text(
        f'& "{sys.executable}" "{fake}" @args\nexit $LASTEXITCODE\n',
        encoding="utf-8",
    )
    return tmp_path / "gcloud-log.jsonl"


@contextmanager
def _sql_admin_server(
    *, operation_fails: bool = False, request_fails: bool = False
) -> Iterator[tuple[str, list[dict[str, Any]]]]:
    calls: list[dict[str, Any]] = []
    poll_count = 0

    class Handler(BaseHTTPRequestHandler):
        def log_message(self, format: str, *args: object) -> None:
            del format, args

        def do_POST(self) -> None:
            self._mutation("POST")

        def do_PUT(self) -> None:
            self._mutation("PUT")

        def do_GET(self) -> None:
            nonlocal poll_count
            calls.append(
                {
                    "method": "GET",
                    "path": self.path,
                    "authorization": self.headers.get("Authorization"),
                    "body": None,
                }
            )
            poll_count += 1
            if operation_fails and poll_count > 1:
                self._json(
                    200,
                    {
                        "name": "operation-user-1",
                        "status": "DONE",
                        "error": {
                            "errors": [
                                {
                                    "code": "FAILED_PRECONDITION",
                                    "message": _PASSWORD,
                                }
                            ]
                        },
                    },
                )
                return
            status = "RUNNING" if poll_count == 1 else "DONE"
            self._json(200, {"name": "operation-user-1", "status": status})

        def _mutation(self, method: str) -> None:
            raw = self.rfile.read(int(self.headers.get("Content-Length", "0")))
            calls.append(
                {
                    "method": method,
                    "path": self.path,
                    "authorization": self.headers.get("Authorization"),
                    "content_type": self.headers.get("Content-Type"),
                    "body": json.loads(raw),
                }
            )
            if request_fails:
                self._json(400, {"error": {"message": _PASSWORD}})
                return
            self._json(200, {"name": "operation-user-1", "status": "PENDING"})

        def _json(self, status: int, payload: dict[str, Any]) -> None:
            body = json.dumps(payload).encode()
            self.send_response(status)
            self.send_header("Content-Type", "application/json")
            self.send_header("Content-Length", str(len(body)))
            self.end_headers()
            self.wfile.write(body)

    server = ThreadingHTTPServer(("127.0.0.1", 0), Handler)
    thread = threading.Thread(target=server.serve_forever, daemon=True)
    thread.start()
    try:
        host, port = server.server_address
        yield f"http://{host}:{port}", calls
    finally:
        server.shutdown()
        thread.join(timeout=5)
        server.server_close()


def _run_apply(
    tmp_path: Path,
    *,
    user_exists: bool,
    endpoint: str,
    sql_users: list[dict[str, Any]] | None = None,
    allow_test_endpoint: bool = True,
    process_timeout_seconds: int | None = None,
    pytest_context: bool = True,
) -> tuple[subprocess.CompletedProcess[str], list[list[str]]]:
    log = _install_fake_gcloud(tmp_path)
    env = os.environ.copy()
    env["PATH"] = f"{tmp_path}{os.pathsep}{env['PATH']}"
    env["FAKE_GCLOUD_LOG"] = str(log)
    env["FAKE_DB_PASSWORD"] = _PASSWORD
    env["FAKE_ACCESS_TOKEN"] = _TOKEN
    if sql_users is None:
        sql_users = [{"name": "hk_rag_app", "type": "BUILT_IN"}] if user_exists else []
    env["FAKE_SQL_USERS_JSON"] = json.dumps(sql_users)
    if allow_test_endpoint:
        env["RAG_DEMO_ALLOW_TEST_SQL_ADMIN_API"] = "1"
    else:
        env.pop("RAG_DEMO_ALLOW_TEST_SQL_ADMIN_API", None)
    env["RAG_DEMO_TEST_SQL_ADMIN_API_BASE_URI"] = endpoint
    if not pytest_context:
        env.pop("PYTEST_CURRENT_TEST", None)
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
    with secret_manager_server({DB_SECRET: _PASSWORD.encode(), DEMO_SECRET: b"Q" * 43}) as (
        secret_endpoint,
        _,
    ):
        env["RAG_DEMO_ALLOW_TEST_SECRET_MANAGER_API"] = "1"
        env["RAG_DEMO_TEST_SECRET_MANAGER_API_BASE_URI"] = secret_endpoint
        result = subprocess.run(
            command,
            check=False,
            capture_output=True,
            text=True,
            encoding="utf-8",
            env=env,
            timeout=30,
        )
    calls = (
        [json.loads(line) for line in log.read_text(encoding="utf-8").splitlines()]
        if log.exists()
        else []
    )
    return result, calls


def test_existing_user_password_uses_admin_api_put_and_waits_for_operation(
    tmp_path: Path,
) -> None:
    with _sql_admin_server() as (endpoint, api_calls):
        result, gcloud_calls = _run_apply(tmp_path, user_exists=True, endpoint=endpoint)

    assert result.returncode == 0, result.stdout + result.stderr
    assert json.loads(result.stdout)["status"] == "applied"
    assert _PASSWORD not in result.stdout + result.stderr
    assert all(_PASSWORD not in argument for call in gcloud_calls for argument in call)
    assert not any(call[:3] == ["sql", "users", "create"] for call in gcloud_calls)
    assert not any(call[:3] == ["sql", "users", "set-password"] for call in gcloud_calls)
    assert [call["method"] for call in api_calls] == ["PUT", "GET", "GET"]
    assert api_calls[0] == {
        "method": "PUT",
        "path": (
            "/sql/v1beta4/projects/motionexpaiweb/instances/hk-movie-rag-pg/users?name=hk_rag_app"
        ),
        "authorization": f"Bearer {_TOKEN}",
        "content_type": "application/json; charset=utf-8",
        "body": {
            "name": "hk_rag_app",
            "password": _PASSWORD,
            "type": "BUILT_IN",
        },
    }
    assert [call["path"] for call in api_calls[1:]] == [
        "/sql/v1beta4/projects/motionexpaiweb/operations/operation-user-1",
        "/sql/v1beta4/projects/motionexpaiweb/operations/operation-user-1",
    ]
    assert all(call["authorization"] == f"Bearer {_TOKEN}" for call in api_calls)


def test_existing_user_without_type_uses_exact_live_identity_shape(tmp_path: Path) -> None:
    live_user = {
        "etag": "fixture-etag",
        "host": "",
        "instance": "hk-movie-rag-pg",
        "kind": "sql#user",
        "name": "hk_rag_app",
        "project": "motionexpaiweb",
    }
    with _sql_admin_server() as (endpoint, api_calls):
        result, _ = _run_apply(
            tmp_path,
            user_exists=True,
            endpoint=endpoint,
            sql_users=[live_user],
        )

    assert result.returncode == 0, result.stdout + result.stderr
    assert [call["method"] for call in api_calls] == ["PUT", "GET", "GET"]


def test_non_target_user_preserves_fresh_create_path(tmp_path: Path) -> None:
    other_user = {
        "etag": "fixture-etag",
        "host": "",
        "instance": "hk-movie-rag-pg",
        "kind": "sql#user",
        "name": "postgres",
        "project": "motionexpaiweb",
    }
    with _sql_admin_server() as (endpoint, api_calls):
        result, _ = _run_apply(
            tmp_path,
            user_exists=False,
            endpoint=endpoint,
            sql_users=[other_user],
        )

    assert result.returncode == 0, result.stdout + result.stderr
    assert [call["method"] for call in api_calls] == ["POST", "GET", "GET"]


@pytest.mark.parametrize(
    "sql_users",
    [
        [{"name": "hk_rag_app", "type": "CLOUD_IAM_USER"}],
        [
            {"name": "hk_rag_app", "type": "BUILT_IN"},
            {"name": "hk_rag_app", "type": "BUILT_IN"},
        ],
        [
            {
                "etag": "fixture-etag",
                "host": "%",
                "instance": "hk-movie-rag-pg",
                "kind": "sql#user",
                "name": "hk_rag_app",
                "project": "motionexpaiweb",
            }
        ],
        [
            {
                "etag": "fixture-etag",
                "host": "",
                "instance": "hk-movie-rag-pg",
                "kind": "unexpected-kind",
                "name": "hk_rag_app",
                "project": "motionexpaiweb",
            }
        ],
        [
            {
                "etag": "fixture-etag",
                "host": "",
                "instance": "other-instance",
                "kind": "sql#user",
                "name": "hk_rag_app",
                "project": "motionexpaiweb",
            }
        ],
        [
            {
                "etag": "fixture-etag",
                "host": "",
                "instance": "hk-movie-rag-pg",
                "kind": "sql#user",
                "name": "hk_rag_app",
                "project": "other-project",
            }
        ],
    ],
    ids=[
        "explicit-non-built-in",
        "duplicate-target",
        "wrong-host",
        "wrong-kind",
        "wrong-instance",
        "wrong-project",
    ],
)
def test_incompatible_or_ambiguous_target_user_is_rejected_before_password_access(
    tmp_path: Path,
    sql_users: list[dict[str, Any]],
) -> None:
    with _sql_admin_server() as (endpoint, api_calls):
        result, gcloud_calls = _run_apply(
            tmp_path,
            user_exists=True,
            endpoint=endpoint,
            sql_users=sql_users,
        )

    combined = result.stdout + result.stderr
    assert result.returncode != 0
    assert "Cloud SQL user exists with incompatible or ambiguous identity" in combined
    assert _PASSWORD not in combined
    assert not any(call[:4] == ["secrets", "versions", "access", "latest"] for call in gcloud_calls)
    assert api_calls == []


def test_missing_user_is_created_atomically_with_admin_api_post(tmp_path: Path) -> None:
    with _sql_admin_server() as (endpoint, api_calls):
        result, gcloud_calls = _run_apply(tmp_path, user_exists=False, endpoint=endpoint)

    assert result.returncode == 0, result.stdout + result.stderr
    assert _PASSWORD not in result.stdout + result.stderr
    assert all(_PASSWORD not in argument for call in gcloud_calls for argument in call)
    assert not any(call[:3] == ["sql", "users", "create"] for call in gcloud_calls)
    assert not any(call[:3] == ["sql", "users", "set-password"] for call in gcloud_calls)
    assert [call["method"] for call in api_calls] == ["POST", "GET", "GET"]
    assert api_calls[0] == {
        "method": "POST",
        "path": ("/sql/v1beta4/projects/motionexpaiweb/instances/hk-movie-rag-pg/users"),
        "authorization": f"Bearer {_TOKEN}",
        "content_type": "application/json; charset=utf-8",
        "body": {
            "name": "hk_rag_app",
            "password": _PASSWORD,
            "type": "BUILT_IN",
        },
    }


def test_operation_error_fails_without_exposing_api_error_body(tmp_path: Path) -> None:
    with _sql_admin_server(operation_fails=True) as (endpoint, api_calls):
        result, _ = _run_apply(tmp_path, user_exists=True, endpoint=endpoint)

    combined = result.stdout + result.stderr
    assert result.returncode != 0
    assert _PASSWORD not in combined
    assert "Cloud SQL Admin API operation failed without exposing error details" in combined
    assert [call["method"] for call in api_calls] == ["PUT", "GET", "GET"]


def test_http_error_fails_without_exposing_api_error_body(tmp_path: Path) -> None:
    with _sql_admin_server(request_fails=True) as (endpoint, api_calls):
        result, _ = _run_apply(tmp_path, user_exists=True, endpoint=endpoint)

    combined = result.stdout + result.stderr
    assert result.returncode != 0
    assert _PASSWORD not in combined
    assert (
        "Cloud SQL Admin API request failed without exposing request or response content "
        "(HTTP 400)" in combined
    )
    assert [call["method"] for call in api_calls] == ["PUT"]


def test_local_admin_api_endpoint_requires_explicit_test_opt_in(tmp_path: Path) -> None:
    with _sql_admin_server() as (endpoint, api_calls):
        result, gcloud_calls = _run_apply(
            tmp_path,
            user_exists=True,
            endpoint=endpoint,
            allow_test_endpoint=False,
        )

    combined = result.stdout + result.stderr
    assert result.returncode != 0
    assert "test Cloud SQL Admin API endpoint requires explicit opt-in" in combined
    assert _PASSWORD not in combined
    assert gcloud_calls == []
    assert api_calls == []


def test_local_admin_api_endpoint_requires_active_pytest_context(tmp_path: Path) -> None:
    with _sql_admin_server() as (endpoint, api_calls):
        result, gcloud_calls = _run_apply(
            tmp_path,
            user_exists=True,
            endpoint=endpoint,
            pytest_context=False,
        )

    combined = result.stdout + result.stderr
    assert result.returncode != 0
    assert "test Cloud SQL Admin API endpoint requires active pytest context" in combined
    assert _PASSWORD not in combined
    assert gcloud_calls == []
    assert api_calls == []


def test_test_admin_api_endpoint_rejects_non_loopback_origin(tmp_path: Path) -> None:
    result, gcloud_calls = _run_apply(
        tmp_path,
        user_exists=True,
        endpoint="http://192.0.2.1:1",
        process_timeout_seconds=1,
    )

    combined = result.stdout + result.stderr
    assert result.returncode != 0
    assert (
        "test Cloud SQL Admin API endpoint must be an unqualified HTTP loopback origin" in combined
    )
    assert _PASSWORD not in combined
    assert gcloud_calls == []


def test_plan_has_no_gcloud_sql_user_password_path(tmp_path: Path) -> None:
    result = subprocess.run(
        [
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
            "-PlanOnly",
        ],
        cwd=tmp_path,
        check=False,
        capture_output=True,
        text=True,
        encoding="utf-8",
        timeout=15,
    )

    assert result.returncode == 0, result.stdout + result.stderr
    commands = json.loads(result.stdout)["commands"]
    assert "gcloud auth print-access-token --project=motionexpaiweb" in commands
    assert any("Cloud SQL Admin v1beta4 users.insert/users.update" in item for item in commands)
    assert all("gcloud sql users create" not in item for item in commands)
    assert all("gcloud sql users set-password" not in item for item in commands)
    assert all("--prompt-for-password" not in item for item in commands)
