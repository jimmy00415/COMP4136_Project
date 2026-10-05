from __future__ import annotations

import base64
import copy
import json
import os
import re
import shutil
import subprocess
import sys
import threading
from collections.abc import Iterator
from contextlib import contextmanager
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path
from typing import Any
from urllib.parse import parse_qs, urlsplit

import pytest

ROOT = Path(__file__).resolve().parents[1]
PROVISION = ROOT / "scripts" / "gcp" / "provision-demo.ps1"
PROJECT = "motionexpaiweb"
PROJECT_NUMBER = "880586285913"
DB_SECRET = "hk-rag-db-password"
DEMO_SECRET = "hk-rag-demo-key"
TOKEN = "secret-manager-test-access-token"
CANONICAL_A = b"A" * 43
CANONICAL_B = b"B" * 43


def _powershell() -> str:
    executable = shutil.which("pwsh") or shutil.which("powershell")
    if executable is None:
        pytest.skip("PowerShell is unavailable")
    return executable


def _version(name: str, number: int, payload: bytes, state: str = "ENABLED") -> dict[str, Any]:
    return {
        "name": f"projects/{PROJECT_NUMBER}/secrets/{name}/versions/{number}",
        "payload": payload,
        "state": state,
    }


def _install_fake_gcloud(tmp_path: Path) -> Path:
    fake = tmp_path / "fake_gcloud_secret_manager.py"
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
    ("sql", "databases", "list"): [{"name": "hk_movie_rag"}],
    ("sql", "users", "list"): [{"name": "hk_rag_app", "type": "BUILT_IN"}],
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
def _google_api_server(
    initial: dict[str, dict[int, dict[str, Any]]],
) -> Iterator[
    tuple[
        str,
        list[dict[str, Any]],
        dict[str, dict[int, dict[str, Any]]],
        list[dict[str, Any]],
    ]
]:
    state = copy.deepcopy(initial)
    secret_calls: list[dict[str, Any]] = []
    sql_calls: list[dict[str, Any]] = []
    secret_prefix = rf"/v1/projects/{PROJECT}/secrets/([^/]+)"

    class Handler(BaseHTTPRequestHandler):
        def log_message(self, format: str, *args: object) -> None:
            del format, args

        def do_GET(self) -> None:
            parsed = urlsplit(self.path)
            list_match = re.fullmatch(secret_prefix + r"/versions", parsed.path)
            access_match = re.fullmatch(secret_prefix + r"/versions/(\d+):access", parsed.path)
            if list_match:
                assert parse_qs(parsed.query) == {"filter": ["state:ENABLED"]}
                name = list_match.group(1)
                secret_calls.append({"event": "list", "name": name})
                versions = [
                    {"name": item["name"], "state": item["state"]}
                    for _, item in sorted(state[name].items(), reverse=True)
                    if item["state"] == "ENABLED"
                ]
                self._json(200, {"versions": versions} if versions else {})
                return
            if access_match:
                name = access_match.group(1)
                number = int(access_match.group(2))
                item = state[name][number]
                secret_calls.append({"event": "access", "name": name, "version": number})
                self._json(
                    200,
                    {
                        "name": item["name"],
                        "payload": {"data": base64.b64encode(item["payload"]).decode("ascii")},
                    },
                )
                return
            self._json(404, {"error": "unexpected GET"})

        def do_POST(self) -> None:
            parsed = urlsplit(self.path)
            add_match = re.fullmatch(secret_prefix + r":addVersion", parsed.path)
            disable_match = re.fullmatch(secret_prefix + r"/versions/(\d+):disable", parsed.path)
            raw = self.rfile.read(int(self.headers.get("Content-Length", "0")))
            body = json.loads(raw or b"{}")
            if add_match:
                name = add_match.group(1)
                payload = base64.b64decode(body["payload"]["data"], validate=True)
                number = max(state[name], default=0) + 1
                state[name][number] = _version(name, number, payload)
                secret_calls.append(
                    {
                        "event": "add",
                        "name": name,
                        "version": number,
                        "length": len(payload),
                        "authorization": self.headers.get("Authorization"),
                    }
                )
                self._json(
                    200,
                    {"name": state[name][number]["name"], "state": "ENABLED"},
                )
                return
            if disable_match:
                name = disable_match.group(1)
                number = int(disable_match.group(2))
                assert body == {}
                state[name][number]["state"] = "DISABLED"
                secret_calls.append({"event": "disable", "name": name, "version": number})
                self._json(
                    200,
                    {"name": state[name][number]["name"], "state": "DISABLED"},
                )
                return
            if parsed.path.startswith("/sql/v1beta4/"):
                sql_calls.append(
                    {
                        "method": "POST",
                        "path": self.path,
                        "body": body,
                        "authorization": self.headers.get("Authorization"),
                    }
                )
                self._json(200, {"name": "fixture-operation", "status": "DONE"})
                return
            self._json(404, {"error": "unexpected POST"})

        def do_PUT(self) -> None:
            raw = self.rfile.read(int(self.headers.get("Content-Length", "0")))
            body = json.loads(raw)
            sql_calls.append(
                {
                    "method": "PUT",
                    "path": self.path,
                    "body": body,
                    "authorization": self.headers.get("Authorization"),
                }
            )
            self._json(200, {"name": "fixture-operation", "status": "DONE"})

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
        yield f"http://{host}:{port}", secret_calls, state, sql_calls
    finally:
        server.shutdown()
        thread.join(timeout=5)
        server.server_close()


def _run_apply(
    tmp_path: Path,
    *,
    sql_endpoint: str | None,
    secret_endpoint: str,
    allow_secret_endpoint: bool = True,
    pytest_context: bool = True,
    process_timeout_seconds: int | None = None,
) -> tuple[subprocess.CompletedProcess[str], list[list[str]]]:
    log = _install_fake_gcloud(tmp_path)
    env = os.environ.copy()
    env["PATH"] = f"{tmp_path}{os.pathsep}{env['PATH']}"
    env["FAKE_GCLOUD_LOG"] = str(log)
    env["FAKE_ACCESS_TOKEN"] = TOKEN
    if sql_endpoint is None:
        env.pop("RAG_DEMO_ALLOW_TEST_SQL_ADMIN_API", None)
        env.pop("RAG_DEMO_TEST_SQL_ADMIN_API_BASE_URI", None)
    else:
        env["RAG_DEMO_ALLOW_TEST_SQL_ADMIN_API"] = "1"
        env["RAG_DEMO_TEST_SQL_ADMIN_API_BASE_URI"] = sql_endpoint
    env["RAG_DEMO_TEST_SECRET_MANAGER_API_BASE_URI"] = secret_endpoint
    if allow_secret_endpoint:
        env["RAG_DEMO_ALLOW_TEST_SECRET_MANAGER_API"] = "1"
    else:
        env.pop("RAG_DEMO_ALLOW_TEST_SECRET_MANAGER_API", None)
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
        PROJECT,
        "-Region",
        "us-central1",
        "-Apply",
    ]
    if process_timeout_seconds is not None:
        command.extend(["-ProcessTimeoutSeconds", str(process_timeout_seconds)])
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


def _enabled_payloads(state: dict[str, dict[int, dict[str, Any]]], name: str) -> list[bytes]:
    return [item["payload"] for item in state[name].values() if item["state"] == "ENABLED"]


def _assert_secrets_not_exposed(
    tmp_path: Path,
    result: subprocess.CompletedProcess[str],
    gcloud_calls: list[list[str]],
    secrets: list[bytes],
) -> None:
    combined = (result.stdout + result.stderr).encode()
    argv = json.dumps(gcloud_calls).encode()
    persisted = b"\n".join(path.read_bytes() for path in tmp_path.rglob("*") if path.is_file())
    for secret in secrets:
        reversible_forms = {secret, base64.b64encode(secret), base64.urlsafe_b64encode(secret)}
        for reversible in reversible_forms:
            assert reversible not in combined
            assert reversible not in argv
            assert reversible not in persisted


def test_fresh_secrets_use_rest_add_version_readback_and_never_leak(tmp_path: Path) -> None:
    initial = {DB_SECRET: {}, DEMO_SECRET: {}}
    with _google_api_server(initial) as (endpoint, calls, state, sql_calls):
        result, gcloud_calls = _run_apply(tmp_path, sql_endpoint=endpoint, secret_endpoint=endpoint)

    assert result.returncode == 0, result.stdout + result.stderr
    final_payloads = [_enabled_payloads(state, name)[0] for name in (DB_SECRET, DEMO_SECRET)]
    assert all(len(_enabled_payloads(state, name)) == 1 for name in (DB_SECRET, DEMO_SECRET))
    assert all(re.fullmatch(rb"[A-Za-z0-9_-]{43}", payload) for payload in final_payloads)
    assert all(call[:3] != ["secrets", "versions", "add"] for call in gcloud_calls)
    assert all(call[:4] != ["secrets", "versions", "access", "latest"] for call in gcloud_calls)
    for name in (DB_SECRET, DEMO_SECRET):
        events = [call["event"] for call in calls if call["name"] == name]
        assert events == ["list", "add", "access", "list", "access"]
        add = next(call for call in calls if call["name"] == name and call["event"] == "add")
        assert add["length"] == 43
        assert add["authorization"] == f"Bearer {TOKEN}"
    assert sql_calls[0]["body"]["password"].encode() == final_payloads[0]
    _assert_secrets_not_exposed(tmp_path, result, gcloud_calls, final_payloads)


def test_legacy_crlf_versions_migrate_after_readback_and_feed_canonical_db_password(
    tmp_path: Path,
) -> None:
    initial = {
        DB_SECRET: {1: _version(DB_SECRET, 1, CANONICAL_A + b"\r\n")},
        DEMO_SECRET: {1: _version(DEMO_SECRET, 1, CANONICAL_B + b"\r\n")},
    }
    with _google_api_server(initial) as (endpoint, calls, state, sql_calls):
        result, gcloud_calls = _run_apply(tmp_path, sql_endpoint=endpoint, secret_endpoint=endpoint)

    assert result.returncode == 0, result.stdout + result.stderr
    for name, canonical in ((DB_SECRET, CANONICAL_A), (DEMO_SECRET, CANONICAL_B)):
        assert state[name][1]["state"] == "DISABLED"
        assert _enabled_payloads(state, name) == [canonical]
        named_calls = [call for call in calls if call["name"] == name]
        assert [call["event"] for call in named_calls] == [
            "list",
            "access",
            "add",
            "access",
            "disable",
            "list",
            "access",
        ]
        assert named_calls[1]["version"] == 1
        assert named_calls[3]["version"] == 2
        assert named_calls[4]["version"] == 1
    assert sql_calls[0]["body"]["password"] == CANONICAL_A.decode()
    _assert_secrets_not_exposed(
        tmp_path,
        result,
        gcloud_calls,
        [CANONICAL_A, CANONICAL_A + b"\r\n", CANONICAL_B, CANONICAL_B + b"\r\n"],
    )


def test_exact_resume_disables_matching_legacy_only_after_both_versions_are_read(
    tmp_path: Path,
) -> None:
    initial = {
        DB_SECRET: {
            1: _version(DB_SECRET, 1, CANONICAL_A + b"\r\n"),
            2: _version(DB_SECRET, 2, CANONICAL_A),
        },
        DEMO_SECRET: {
            1: _version(DEMO_SECRET, 1, CANONICAL_B + b"\r\n"),
            2: _version(DEMO_SECRET, 2, CANONICAL_B),
        },
    }
    with _google_api_server(initial) as (endpoint, calls, state, _):
        result, _ = _run_apply(tmp_path, sql_endpoint=endpoint, secret_endpoint=endpoint)

    assert result.returncode == 0, result.stdout + result.stderr
    for name, canonical in ((DB_SECRET, CANONICAL_A), (DEMO_SECRET, CANONICAL_B)):
        named_calls = [call for call in calls if call["name"] == name]
        assert "add" not in [call["event"] for call in named_calls]
        disable_index = next(
            index for index, call in enumerate(named_calls) if call["event"] == "disable"
        )
        assert {
            call["version"] for call in named_calls[:disable_index] if call["event"] == "access"
        } == {1, 2}
        assert named_calls[disable_index]["version"] == 1
        assert _enabled_payloads(state, name) == [canonical]


@pytest.mark.parametrize(
    "db_versions",
    [
        {1: _version(DB_SECRET, 1, CANONICAL_A + b"\n")},
        {
            1: _version(DB_SECRET, 1, CANONICAL_A),
            2: _version(DB_SECRET, 2, CANONICAL_A),
        },
        {
            1: _version(DB_SECRET, 1, CANONICAL_A),
            2: _version(DB_SECRET, 2, CANONICAL_B + b"\r\n"),
        },
        {
            1: _version(DB_SECRET, 1, CANONICAL_A),
            2: _version(DB_SECRET, 2, CANONICAL_A + b"\r\n"),
            3: _version(DB_SECRET, 3, CANONICAL_A + b"\r\n"),
        },
    ],
    ids=["bare-lf", "two-canonical", "mismatched-pair", "three-enabled"],
)
def test_malformed_or_ambiguous_enabled_states_fail_before_secret_mutation(
    tmp_path: Path, db_versions: dict[int, dict[str, Any]]
) -> None:
    initial = {
        DB_SECRET: db_versions,
        DEMO_SECRET: {1: _version(DEMO_SECRET, 1, CANONICAL_B)},
    }
    with _google_api_server(initial) as (endpoint, calls, _, sql_calls):
        result, gcloud_calls = _run_apply(tmp_path, sql_endpoint=endpoint, secret_endpoint=endpoint)

    combined = result.stdout + result.stderr
    assert result.returncode != 0
    assert f"secret {DB_SECRET}" in combined
    assert not any(call["event"] in {"add", "disable"} for call in calls)
    assert sql_calls == []
    all_payloads = [item["payload"] for item in db_versions.values()]
    _assert_secrets_not_exposed(tmp_path, result, gcloud_calls, all_payloads)


def test_secret_manager_test_endpoint_requires_explicit_opt_in(tmp_path: Path) -> None:
    initial = {
        DB_SECRET: {1: _version(DB_SECRET, 1, CANONICAL_A)},
        DEMO_SECRET: {1: _version(DEMO_SECRET, 1, CANONICAL_B)},
    }
    with _google_api_server(initial) as (endpoint, calls, _, _):
        result, gcloud_calls = _run_apply(
            tmp_path,
            sql_endpoint=endpoint,
            secret_endpoint=endpoint,
            allow_secret_endpoint=False,
        )

    assert result.returncode != 0
    assert "test Secret Manager API endpoint requires explicit opt-in" in (
        result.stdout + result.stderr
    )
    assert calls == []
    assert gcloud_calls == []


def test_secret_manager_test_endpoint_requires_active_pytest_context(tmp_path: Path) -> None:
    initial = {
        DB_SECRET: {1: _version(DB_SECRET, 1, CANONICAL_A)},
        DEMO_SECRET: {1: _version(DEMO_SECRET, 1, CANONICAL_B)},
    }
    with _google_api_server(initial) as (endpoint, calls, _, _):
        result, gcloud_calls = _run_apply(
            tmp_path,
            sql_endpoint=None,
            secret_endpoint=endpoint,
            pytest_context=False,
        )

    assert result.returncode != 0
    assert "test Secret Manager API endpoint requires active pytest context" in (
        result.stdout + result.stderr
    )
    assert calls == []
    assert gcloud_calls == []


def test_secret_manager_test_endpoint_rejects_non_loopback_origin(tmp_path: Path) -> None:
    initial = {
        DB_SECRET: {1: _version(DB_SECRET, 1, CANONICAL_A)},
        DEMO_SECRET: {1: _version(DEMO_SECRET, 1, CANONICAL_B)},
    }
    with _google_api_server(initial) as (endpoint, calls, _, _):
        result, gcloud_calls = _run_apply(
            tmp_path,
            sql_endpoint=endpoint,
            secret_endpoint="http://192.0.2.1:1",
            process_timeout_seconds=1,
        )

    assert result.returncode != 0
    assert "test Secret Manager API endpoint must be an unqualified HTTP loopback origin" in (
        result.stdout + result.stderr
    )
    assert calls == []
    assert gcloud_calls == []
