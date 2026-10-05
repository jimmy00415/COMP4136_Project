from __future__ import annotations

import hashlib
import http.cookiejar
import importlib.util
import io
import json
import os
import shutil
import subprocess
import sys
import threading
import urllib.error
import urllib.parse
import urllib.request
from collections.abc import Iterator
from contextlib import contextmanager
from dataclasses import replace
from http.cookies import SimpleCookie
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path
from typing import Any, ClassVar, Self

import pytest
from _gcp_secret_manager_fake import (
    DB_SECRET,
    DEFAULT_DB_SECRET,
    DEFAULT_DEMO_SECRET,
    DEMO_SECRET,
    enabled_payloads,
    secret_manager_server,
)
from PIL import Image

ROOT = Path(__file__).resolve().parents[1]
PROVISION = ROOT / "scripts" / "gcp" / "provision-demo.ps1"
DEPLOY = ROOT / "scripts" / "gcp" / "deploy-demo.ps1"
SMOKE = ROOT / "scripts" / "gcp" / "live-smoke.py"
GENERAL_RELEVANCE_GOLDEN = ROOT / "evals" / "general_relevance_golden.jsonl"
GENERAL_RELEVANCE_ROWS = tuple(
    json.loads(line)
    for line in GENERAL_RELEVANCE_GOLDEN.read_text(encoding="utf-8").splitlines()
)
GENERAL_RELEVANCE_MOVIE_QUESTIONS = frozenset(
    str(row["question"])
    for row in GENERAL_RELEVANCE_ROWS
    if row["expected_domain"] == "movie"
)
GENERAL_RELEVANCE_OOD_QUESTIONS = frozenset(
    str(row["question"])
    for row in GENERAL_RELEVANCE_ROWS
    if row["expected_domain"] == "ood"
)
GENERAL_RELEVANCE_MOVIE_CASE_BY_QUESTION = {
    str(row["question"]): str(row["case_id"])
    for row in GENERAL_RELEVANCE_ROWS
    if row["expected_domain"] == "movie"
}
GENERAL_RELEVANCE_CONTROLLED_INSUFFICIENT_CASE_IDS = frozenset(
    {
        "cal-movie-04",
        "cal-movie-10",
        "cal-movie-15",
        "hold-movie-04",
    }
)
GENERAL_RELEVANCE_BOUNDED_PDF_CASE_IDS = frozenset(
    {
        "cal-movie-01",
        "cal-movie-02",
        "cal-movie-05",
        "cal-movie-06",
        "cal-movie-08",
    }
)
GENERAL_RELEVANCE_CONTROLLED_INSUFFICIENCY = (
    "這個問題需要結構化欄位以外的分析證據；目前 Release "
    "不足以可靠判斷這項條件。請改問具體片名，或使用年代、類型、"
    "導演、演員等已治理欄位。"
)
GENERAL_RELEVANCE_MOVIE_FIXTURES = {
    "cal-movie-01": (
        "GOLDEN_CAL_01",
        "動作喜劇代表",
        "動作與喜劇結合快節奏笑料",
        "1985-01-01",
        "測試導演",
        "測試演員",
        "動作 / 喜劇",
    ),
    "cal-movie-02": (
        "GOLDEN_CAL_02",
        "七十年代功夫片",
        "功夫電影常以武打與師門衝突推進",
        "1975-01-01",
        "測試導演",
        "測試演員",
        "功夫 / 動作",
    ),
    "cal-movie-03": (
        "GOLDEN_CAL_03",
        "八十年代警匪片",
        "警匪對立與犯罪追查是代表元素",
        "1986-01-01",
        "測試導演",
        "測試演員",
        "犯罪 / 動作",
    ),
    "cal-movie-04": (
        "GOLDEN_CAL_04",
        "九十年代喜劇",
        "喜劇以都市生活與明星表演形成特色",
        "1994-01-01",
        "測試導演",
        "測試演員",
        "喜劇",
    ),
    "cal-movie-05": (
        "GOLDEN_CAL_05",
        "師徒電影",
        "師徒關係透過傳承與衝突呈現",
        "1980-01-01",
        "測試導演",
        "測試演員",
        "劇情",
    ),
    "cal-movie-06": (
        "GOLDEN_CAL_06",
        "都市空間電影",
        "都市空間透過街道與密集城市景觀呈現",
        "1988-01-01",
        "測試導演",
        "測試演員",
        "劇情",
    ),
    "cal-movie-07": (
        "GOLDEN_CAL_07",
        "武打喜劇",
        "功夫武打與喜劇節奏適合相關觀眾",
        "1978-01-01",
        "測試導演",
        "測試演員",
        "功夫 / 喜劇",
    ),
    "cal-movie-08": (
        "GOLDEN_CAL_08",
        "明快動作片",
        "節奏明快並以動作場面推進",
        "1988-01-01",
        "測試導演",
        "測試演員",
        "動作",
    ),
    "cal-movie-09": (
        "GOLDEN_CAL_09",
        "兄弟情電影",
        "只因片名命中兄弟關鍵詞而入選；這不證明其劇情或主題包含相關元素",
        "1986-01-01",
        "測試導演",
        "測試演員",
        "劇情",
    ),
    "cal-movie-10": (
        "GOLDEN_CAL_10",
        "犯罪角色電影",
        "警察、臥底與黑幫是犯罪片常見角色",
        "2002-01-01",
        "測試導演",
        "測試演員",
        "犯罪",
    ),
    "cal-movie-11": (
        "GOLDEN_CAL_11",
        "女性導演電影",
        "女性導演欄位命中 Release 受控名單；這只基於精確姓名匹配",
        "1990-01-01",
        "許鞍華",
        "測試演員",
        "劇情",
    ),
    "cal-movie-12": (
        "GOLDEN_CAL_12",
        "殭屍電影",
        "只因片名命中殭屍關鍵詞而入選；這不證明其劇情或主題包含相關元素",
        "1985-01-01",
        "測試導演",
        "測試演員",
        "恐怖 / 喜劇",
    ),
    "cal-movie-13": (
        "GOLDEN_CAL_13",
        "賭片",
        "只因片名命中賭字關鍵詞而入選；這不證明其劇情或主題包含相關元素",
        "1989-01-01",
        "測試導演",
        "測試演員",
        "劇情",
    ),
    "cal-movie-14": (
        "GOLDEN_CAL_14",
        "經典武俠片",
        "武俠世界以江湖與劍術展開",
        "1967-01-01",
        "測試導演",
        "測試演員",
        "武俠",
    ),
    "cal-movie-15": (
        "GOLDEN_CAL_15",
        "身份認同電影",
        "身份認同與歸屬感推動人物選擇",
        "1997-01-01",
        "測試導演",
        "測試演員",
        "劇情",
    ),
    "hold-movie-01": (
        "GOLDEN_HOLD_01",
        "六七十年代武俠",
        "武俠代表作品以江湖衝突見長",
        "1969-01-01",
        "測試導演",
        "測試演員",
        "武俠",
    ),
    "hold-movie-02": (
        "GOLDEN_HOLD_02",
        "經典愛情片",
        "愛情與離合構成經典敘事",
        "1996-01-01",
        "測試導演",
        "測試演員",
        "愛情",
    ),
    "hold-movie-03": (
        "GOLDEN_HOLD_03",
        "女性主角劇情片",
        "演員卡司欄位精確命中受控名單；這不代表她擔任主角",
        "1992-01-01",
        "測試導演",
        "張曼玉",
        "劇情",
    ),
    "hold-movie-04": (
        "GOLDEN_HOLD_04",
        "香港恐怖片",
        "恐怖類型包含鬼怪與靈異題材",
        "1982-01-01",
        "測試導演",
        "測試演員",
        "恐怖",
    ),
    "hold-movie-05": (
        "GOLDEN_HOLD_05",
        "家庭電影",
        "Release 類型欄位含家庭；這不是年齡分級，也不證明適合所有兒童",
        "2000-01-01",
        "測試導演",
        "測試演員",
        "家庭",
    ),
}
DEFAULT_SMOKE_BODY_LIMIT = 2 * 1024 * 1024
POSTER_SMOKE_BODY_LIMIT = 16 * 1024 * 1024
TAMPERED_SESSION_COOKIES = (
    "rag_demo_session=opaque-session-token-tampered",
    "rag_demo_session=tampered-opaque-session-token",
    "rag_demo_session=xpaque-session-token",
    "rag_demo_session=opaque-session-tokea",
    "rag_demo_session=opaque-session-toke",
)
TAMPERED_PROTECTED_ENDPOINTS = (
    ("GET", "/api/config"),
    ("POST", "/api/chat"),
    ("GET", "/api/posters/1978_ZQ_001"),
)


def _powershell() -> str:
    executable = shutil.which("pwsh") or shutil.which("powershell")
    if executable is None:
        pytest.skip("PowerShell is unavailable")
    return executable


@contextmanager
def _provision_sql_admin_server() -> Iterator[tuple[str, list[dict[str, Any]]]]:
    calls: list[dict[str, Any]] = []

    class Handler(BaseHTTPRequestHandler):
        def log_message(self, format: str, *args: object) -> None:
            del format, args

        def do_POST(self) -> None:
            self._mutation("POST")

        def do_PUT(self) -> None:
            self._mutation("PUT")

        def _mutation(self, method: str) -> None:
            raw = self.rfile.read(int(self.headers.get("Content-Length", "0")))
            calls.append({"method": method, "path": self.path, "body": json.loads(raw)})
            body = json.dumps({"name": "fixture-operation", "status": "DONE"}).encode()
            self.send_response(200)
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


def _run_plan(script: Path, tmp_path: Path) -> dict[str, Any]:
    before = sorted(path.relative_to(tmp_path).as_posix() for path in tmp_path.rglob("*"))
    result = subprocess.run(
        [
            _powershell(),
            "-NoLogo",
            "-NoProfile",
            "-NonInteractive",
            "-File",
            str(script),
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
    )
    assert result.returncode == 0, result.stderr
    after = sorted(path.relative_to(tmp_path).as_posix() for path in tmp_path.rglob("*"))
    assert after == before
    assert result.stderr == ""
    return json.loads(result.stdout)


def test_provision_plan_is_deterministic_exact_and_non_mutating(tmp_path: Path) -> None:
    first = _run_plan(PROVISION, tmp_path)
    second = _run_plan(PROVISION, tmp_path)

    assert first == second
    assert first["mode"] == "plan"
    assert first["project_id"] == "motionexpaiweb"
    assert first["region"] == "us-central1"
    assert first["apis"] == [
        "run.googleapis.com",
        "cloudbuild.googleapis.com",
        "artifactregistry.googleapis.com",
        "secretmanager.googleapis.com",
        "sqladmin.googleapis.com",
        "sql-component.googleapis.com",
        "aiplatform.googleapis.com",
        "storage.googleapis.com",
    ]
    assert first["cloud_sql_instance"] == "hk-movie-rag-pg"
    assert first["cloud_sql"] == {
        "database_version": "POSTGRES_16",
        "edition": "ENTERPRISE",
        "tier": "db-g1-small",
        "storage_gb": 10,
        "storage_type": "SSD",
        "availability_type": "ZONAL",
        "storage_auto_resize": False,
        "deletion_protection": True,
    }
    assert first["database"] == "hk_movie_rag"
    assert first["database_user"] == "hk_rag_app"
    assert first["release_bucket"] == ("motionexpaiweb-880586285913-hk-movie-rag-release")
    assert first["artifact_registry"] == "hk-movie-rag"
    assert first["runtime_service_account"] == (
        "hk-rag-runtime@motionexpaiweb.iam.gserviceaccount.com"
    )
    assert first["secrets"] == ["hk-rag-db-password", "hk-rag-demo-key"]
    assert first["cloud_run_job"] == "hk-movie-rag-ingest"
    assert first["poster_verify_job"] == "hk-movie-rag-poster-verify"
    assert first["cloud_run_service"] == "hk-movie-rag-demo"
    commands = first["commands"]
    assert commands
    assert all("keys create" not in command for command in commands)
    assert all("--project=motionexpaiweb" in command for command in commands)
    assert sum("Secret Manager v1" in command for command in commands) == 2
    assert all("secrets versions add" not in command for command in commands)
    assert all("--data-file=-" not in command for command in commands)
    serialized = json.dumps(first, sort_keys=True)
    assert "secret-value" not in serialized
    assert "password=" not in serialized.lower()


def test_provision_apply_is_idempotent_when_exact_resources_exist(tmp_path: Path) -> None:
    fake = tmp_path / "fake_gcloud.py"
    fake.write_text(
        """
import json
import os
import sys

args = sys.argv[1:]
with open(os.environ["FAKE_GCLOUD_LOG"], "a", encoding="utf-8") as stream:
    stream.write(json.dumps(args) + "\\n")

key = tuple(item for item in args if not item.startswith("--"))
project = "motionexpaiweb"
member = "serviceAccount:hk-rag-runtime@motionexpaiweb.iam.gserviceaccount.com"
responses = {
    ("services", "list"): [{"config": {"name": name}} for name in [
        "run.googleapis.com", "cloudbuild.googleapis.com",
        "artifactregistry.googleapis.com", "secretmanager.googleapis.com",
        "sqladmin.googleapis.com", "sql-component.googleapis.com",
        "aiplatform.googleapis.com", "storage.googleapis.com",
    ]],
    ("iam", "service-accounts", "list"): [
        {"email": f"hk-rag-runtime@{project}.iam.gserviceaccount.com", "disabled": False}
    ],
    ("artifacts", "repositories", "list"): [{
        "name": f"projects/{project}/locations/us-central1/repositories/hk-movie-rag",
        "format": "DOCKER",
    }],
    ("storage", "buckets", "list"): [{
        "name": "motionexpaiweb-880586285913-hk-movie-rag-release",
        "location": "US-CENTRAL1",
        "uniform_bucket_level_access": True,
        "public_access_prevention": "enforced",
    }],
    ("sql", "instances", "list"): [{
        "name": "hk-movie-rag-pg", "databaseVersion": "POSTGRES_16",
        "region": "us-central1", "settings": {
            "edition": "ENTERPRISE", "tier": "db-g1-small", "dataDiskSizeGb": "10",
            "dataDiskType": "PD_SSD", "availabilityType": "ZONAL",
            "storageAutoResize": False, "deletionProtectionEnabled": True,
        },
    }],
    ("secrets", "list"): [
        {"name": f"projects/{project}/secrets/hk-rag-db-password", "replication": {"automatic": {}}},
        {"name": f"projects/{project}/secrets/hk-rag-demo-key", "replication": {"automatic": {}}},
    ],
    ("secrets", "versions", "list", "hk-rag-db-password"): [
        {"name": "1", "state": "ENABLED"}
    ],
    ("secrets", "versions", "list", "hk-rag-demo-key"): [
        {"name": "1", "state": "ENABLED"}
    ],
    ("secrets", "versions", "access", "latest"): "db-secret-value",
    ("secrets", "describe", "hk-rag-db-password"): {"replication": {"automatic": {}}},
    ("secrets", "describe", "hk-rag-demo-key"): {"replication": {"automatic": {}}},
    ("sql", "databases", "list"): [{"name": "hk_movie_rag"}],
    ("sql", "users", "list"): [{"name": "hk_rag_app", "type": "BUILT_IN"}],
    ("auth", "print-access-token"): "fake-access-token",
    ("projects", "get-iam-policy", project): {"bindings": [
        {"role": "roles/aiplatform.user", "members": [member]},
        {"role": "roles/cloudsql.client", "members": [member]},
    ]},
    ("storage", "buckets", "get-iam-policy", "gs://motionexpaiweb-880586285913-hk-movie-rag-release"): {
        "bindings": [{"role": "roles/storage.objectViewer", "members": [member]}]
    },
    ("secrets", "get-iam-policy", "hk-rag-db-password"): {
        "bindings": [{"role": "roles/secretmanager.secretAccessor", "members": [member]}]
    },
    ("secrets", "get-iam-policy", "hk-rag-demo-key"): {
        "bindings": [{"role": "roles/secretmanager.secretAccessor", "members": [member]}]
    },
}
if key not in responses:
    print(json.dumps({"unexpected": args}), file=sys.stderr)
    raise SystemExit(9)
print("gcloud informational stderr", file=sys.stderr)
value = responses[key]
print(value if isinstance(value, str) else json.dumps(value))
""".strip()
        + "\n",
        encoding="utf-8",
    )
    if os.name == "nt":
        (tmp_path / "gcloud.ps1").write_text(
            f'& "{sys.executable}" "{fake}" @args\nexit $LASTEXITCODE\n', encoding="utf-8"
        )
    else:
        shim = tmp_path / "gcloud"
        shim.write_text(f"#!{sys.executable}\nexec(open({str(fake)!r}).read())\n", encoding="utf-8")
        shim.chmod(0o755)
    log = tmp_path / "gcloud-log.jsonl"
    env = os.environ.copy()
    env["PATH"] = f"{tmp_path}{os.pathsep}{env['PATH']}"
    env["FAKE_GCLOUD_LOG"] = str(log)
    with (
        _provision_sql_admin_server() as (endpoint, api_calls),
        secret_manager_server() as (
            secret_endpoint,
            _,
        ),
    ):
        env["RAG_DEMO_ALLOW_TEST_SQL_ADMIN_API"] = "1"
        env["RAG_DEMO_TEST_SQL_ADMIN_API_BASE_URI"] = endpoint
        env["RAG_DEMO_ALLOW_TEST_SECRET_MANAGER_API"] = "1"
        env["RAG_DEMO_TEST_SECRET_MANAGER_API_BASE_URI"] = secret_endpoint
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
                "-Apply",
            ],
            check=False,
            capture_output=True,
            text=True,
            encoding="utf-8",
            env=env,
        )

    assert result.returncode == 0, result.stdout + result.stderr
    assert DEFAULT_DB_SECRET.decode() not in result.stdout + result.stderr
    assert json.loads(result.stdout)["status"] == "applied"
    calls = [json.loads(line) for line in log.read_text(encoding="utf-8").splitlines()]
    forbidden_mutating = {
        ("services", "enable"),
        ("iam", "service-accounts", "create"),
        ("artifacts", "repositories", "create"),
        ("storage", "buckets", "create"),
        ("sql", "instances", "create"),
        ("secrets", "create"),
        ("sql", "databases", "create"),
    }
    assert not any(
        tuple(call[: len(prefix)]) == prefix for call in calls for prefix in forbidden_mutating
    )
    assert not any(call[:3] == ["sql", "users", "create"] for call in calls)
    assert not any(call[:3] == ["sql", "users", "set-password"] for call in calls)
    assert [call["method"] for call in api_calls] == ["PUT"]
    assert api_calls[0]["body"]["password"] == DEFAULT_DB_SECRET.decode()
    assert all(not argument.startswith("--password") for call in calls for argument in call)
    assert all(DEFAULT_DB_SECRET.decode() not in argument for call in calls for argument in call)


@pytest.mark.parametrize("scenario", ["fresh", "partial"])
def test_provision_apply_converges_fresh_and_partial_without_secret_argv(
    tmp_path: Path, scenario: str
) -> None:
    fake = tmp_path / "fake_provision.py"
    fake.write_text(
        r"""
import json
import os
import sys

args = sys.argv[1:]
key = tuple(item for item in args if not item.startswith("--"))
scenario = os.environ["FAKE_SCENARIO"]
project = "motionexpaiweb"
email = f"hk-rag-runtime@{project}.iam.gserviceaccount.com"
member = f"serviceAccount:{email}"
apis = [
    "run.googleapis.com", "cloudbuild.googleapis.com", "artifactregistry.googleapis.com",
    "secretmanager.googleapis.com", "sqladmin.googleapis.com", "sql-component.googleapis.com",
    "aiplatform.googleapis.com", "storage.googleapis.com",
]
bucket = "motionexpaiweb-880586285913-hk-movie-rag-release"
instance = {
    "name": "hk-movie-rag-pg", "databaseVersion": "POSTGRES_16",
    "region": "us-central1", "connectionName": f"{project}:us-central1:hk-movie-rag-pg",
    "settings": {
        "edition": "ENTERPRISE", "tier": "db-g1-small", "dataDiskSizeGb": "10",
        "dataDiskType": "PD_SSD",
        "availabilityType": "ZONAL", "storageAutoResize": False,
        "deletionProtectionEnabled": True,
    },
}
bucket_value = {
    "name": bucket, "location": "US-CENTRAL1", "uniform_bucket_level_access": True,
    "public_access_prevention": "enforced",
}
secret_value = {"replication": {"automatic": {}}}
unconditional_project = {"bindings": [
    {"role": "roles/aiplatform.user", "members": [member]},
    {"role": "roles/cloudsql.client", "members": [member]},
]}
unconditional_bucket = {"bindings": [
    {"role": "roles/storage.objectViewer", "members": [member]}
]}
unconditional_secret = {"bindings": [
    {"role": "roles/secretmanager.secretAccessor", "members": [member]}
]}
partial = scenario == "partial"
responses = {
    ("services", "list"): [{"config": {"name": name}} for name in apis] if partial else [],
    ("iam", "service-accounts", "list"): [{"email": email, "disabled": False}] if partial else [],
    ("artifacts", "repositories", "list"): [{
        "name": f"projects/{project}/locations/us-central1/repositories/hk-movie-rag",
        "format": "DOCKER",
    }] if partial else [],
    ("storage", "buckets", "list"): [bucket_value] if partial else [],
    ("storage", "buckets", "describe", f"gs://{bucket}"): bucket_value,
    ("sql", "instances", "list"): [instance] if partial else [],
    ("sql", "instances", "describe", "hk-movie-rag-pg"): instance,
    ("secrets", "list"): [
        {"name": f"projects/{project}/secrets/hk-rag-db-password"},
        {"name": f"projects/{project}/secrets/hk-rag-demo-key"},
    ] if partial else [],
    ("secrets", "describe", "hk-rag-db-password"): secret_value,
    ("secrets", "describe", "hk-rag-demo-key"): secret_value,
    ("secrets", "versions", "list", "hk-rag-db-password"): (
        [{"name": "1", "state": "ENABLED"}] if partial else []
    ),
    ("secrets", "versions", "list", "hk-rag-demo-key"): (
        [{"name": "1", "state": "ENABLED"}] if partial else []
    ),
    ("secrets", "versions", "access", "latest"): "db-secret-value",
    ("sql", "databases", "list"): [{"name": "hk_movie_rag"}] if partial else [],
    ("sql", "users", "list"): [],
    ("auth", "print-access-token"): "fake-access-token",
    ("projects", "get-iam-policy", project): unconditional_project if partial else {"bindings": []},
    ("storage", "buckets", "get-iam-policy", f"gs://{bucket}"): (
        unconditional_bucket if partial else {"bindings": []}
    ),
    ("secrets", "get-iam-policy", "hk-rag-db-password"): (
        unconditional_secret if partial else {"bindings": []}
    ),
    ("secrets", "get-iam-policy", "hk-rag-demo-key"): (
        unconditional_secret if partial else {"bindings": []}
    ),
}
mutating = {
    ("services", "enable"), ("iam", "service-accounts", "create"),
    ("artifacts", "repositories", "create"), ("storage", "buckets", "create"),
    ("sql", "instances", "create"), ("secrets", "create"),
    ("secrets", "versions", "add"), ("sql", "databases", "create"),
    ("projects", "add-iam-policy-binding"),
    ("storage", "buckets", "add-iam-policy-binding"),
    ("secrets", "add-iam-policy-binding"),
}
stdin_kind = None
if key[:3] == ("secrets", "versions", "add"):
    value = sys.stdin.read()
    assert value and "\n" not in value and "\r" not in value
    stdin_kind = "secret-version"
with open(os.environ["FAKE_GCLOUD_LOG"], "a", encoding="utf-8") as stream:
    stream.write(json.dumps({
        "args": args,
        "stdin_kind": stdin_kind,
        "ambient_google": {
            name: os.environ.get(name)
            for name in ("GOOGLE_API_KEY", "GOOGLE_CLOUD_LOCATION", "GOOGLE_CLOUD_PROJECT")
        },
        "test_scenario": os.environ.get("FAKE_SCENARIO"),
    }) + "\n")
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
        f'& "{sys.executable}" "{fake}" @args\nexit $LASTEXITCODE\n', encoding="utf-8"
    )
    log = tmp_path / "provision-log.jsonl"
    env = os.environ.copy()
    env["PATH"] = f"{tmp_path}{os.pathsep}{env['PATH']}"
    env["FAKE_GCLOUD_LOG"] = str(log)
    env["FAKE_SCENARIO"] = scenario
    env["GOOGLE_API_KEY"] = "must-not-reach-gcloud"
    env["GOOGLE_CLOUD_LOCATION"] = "wrong-location"
    env["GOOGLE_CLOUD_PROJECT"] = "wrong-project"
    initial_payloads = {
        DB_SECRET: DEFAULT_DB_SECRET if scenario == "partial" else None,
        DEMO_SECRET: DEFAULT_DEMO_SECRET if scenario == "partial" else None,
    }
    with (
        _provision_sql_admin_server() as (endpoint, api_calls),
        secret_manager_server(initial_payloads) as (secret_endpoint, secret_state),
    ):
        env["RAG_DEMO_ALLOW_TEST_SQL_ADMIN_API"] = "1"
        env["RAG_DEMO_TEST_SQL_ADMIN_API_BASE_URI"] = endpoint
        env["RAG_DEMO_ALLOW_TEST_SECRET_MANAGER_API"] = "1"
        env["RAG_DEMO_TEST_SECRET_MANAGER_API_BASE_URI"] = secret_endpoint
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
                "-Apply",
            ],
            check=False,
            capture_output=True,
            text=True,
            encoding="utf-8",
            env=env,
            timeout=60,
        )
    assert result.returncode == 0, result.stdout + result.stderr
    database_password = enabled_payloads(secret_state, DB_SECRET)[0].decode()
    assert database_password not in result.stdout + result.stderr
    entries = [json.loads(line) for line in log.read_text(encoding="utf-8").splitlines()]
    calls = [entry["args"] for entry in entries]
    assert all(set(entry["ambient_google"].values()) == {None} for entry in entries)
    assert all(entry["test_scenario"] == scenario for entry in entries)
    assert all(not arg.startswith("--password") for call in calls for arg in call)
    assert all(database_password not in arg for call in calls for arg in call)
    secret_version_list_calls = [
        call for call in calls if call[:3] == ["secrets", "versions", "list"]
    ]
    assert secret_version_list_calls == []
    assert all(
        not argument.startswith("--secret=")
        for call in secret_version_list_calls
        for argument in call
    )
    assert not any(call[:3] == ["sql", "users", "create"] for call in calls)
    assert not any(call[:3] == ["sql", "users", "set-password"] for call in calls)
    assert [call["method"] for call in api_calls] == ["POST"]
    assert api_calls[0]["body"]["password"] == database_password
    if scenario == "fresh":
        service_enable_calls = [call for call in calls if call[:2] == ["services", "enable"]]
        assert service_enable_calls == [
            [
                "services",
                "enable",
                "run.googleapis.com",
                "cloudbuild.googleapis.com",
                "artifactregistry.googleapis.com",
                "secretmanager.googleapis.com",
                "sqladmin.googleapis.com",
                "sql-component.googleapis.com",
                "aiplatform.googleapis.com",
                "storage.googleapis.com",
                "--project=motionexpaiweb",
                "--quiet",
            ]
        ]
        assert [entry["stdin_kind"] for entry in entries].count("secret-version") == 0
        sql_create_calls = [call for call in calls if call[:3] == ["sql", "instances", "create"]]
        assert sql_create_calls == [
            [
                "sql",
                "instances",
                "create",
                "hk-movie-rag-pg",
                "--database-version=POSTGRES_16",
                "--edition=ENTERPRISE",
                "--tier=db-g1-small",
                "--storage-size=10",
                "--storage-type=SSD",
                "--availability-type=zonal",
                "--no-storage-auto-increase",
                "--deletion-protection",
                "--region=us-central1",
                "--project=motionexpaiweb",
                "--quiet",
            ]
        ]
        runtime_member = "serviceAccount:hk-rag-runtime@motionexpaiweb.iam.gserviceaccount.com"
        iam_binding_calls = [
            call
            for call in calls
            if call[:2] == ["projects", "add-iam-policy-binding"]
            or call[:3] == ["storage", "buckets", "add-iam-policy-binding"]
            or call[:2] == ["secrets", "add-iam-policy-binding"]
        ]
        assert iam_binding_calls == [
            [
                "projects",
                "add-iam-policy-binding",
                "motionexpaiweb",
                "--member",
                runtime_member,
                "--role=roles/aiplatform.user",
                "--condition=None",
                "--project=motionexpaiweb",
                "--quiet",
            ],
            [
                "projects",
                "add-iam-policy-binding",
                "motionexpaiweb",
                "--member",
                runtime_member,
                "--role=roles/cloudsql.client",
                "--condition=None",
                "--project=motionexpaiweb",
                "--quiet",
            ],
            [
                "storage",
                "buckets",
                "add-iam-policy-binding",
                "gs://motionexpaiweb-880586285913-hk-movie-rag-release",
                "--member",
                runtime_member,
                "--role=roles/storage.objectViewer",
                "--project=motionexpaiweb",
                "--quiet",
            ],
            [
                "secrets",
                "add-iam-policy-binding",
                "hk-rag-db-password",
                "--member",
                runtime_member,
                "--role=roles/secretmanager.secretAccessor",
                "--condition=None",
                "--project=motionexpaiweb",
                "--quiet",
            ],
            [
                "secrets",
                "add-iam-policy-binding",
                "hk-rag-demo-key",
                "--member",
                runtime_member,
                "--role=roles/secretmanager.secretAccessor",
                "--condition=None",
                "--project=motionexpaiweb",
                "--quiet",
            ],
        ]
        assert all(
            not any(argument.startswith("--member=") for argument in call)
            for call in iam_binding_calls
        )
        assert all(
            "hk-rag-runtime@motionexpaiweb.iam.gserviceaccount.com" not in call
            for call in iam_binding_calls
        )
    else:
        assert [entry["stdin_kind"] for entry in entries].count("secret-version") == 0
        assert not any(call[:3] == ["sql", "instances", "create"] for call in calls)


def test_deploy_plan_is_deterministic_and_covers_the_release_contract(tmp_path: Path) -> None:
    first = _run_plan(DEPLOY, tmp_path)
    second = _run_plan(DEPLOY, tmp_path)

    assert first == second
    assert first["mode"] == "plan"
    assert first["project_id"] == "motionexpaiweb"
    assert first["region"] == "us-central1"
    assert first["inputs"] == {
        "expected_rag_release_id": "v1.2-demo-r2",
        "rag_config_path": "config/rag_demo_r2.yaml",
        "document_source_root": "",
        "reuse_from_release_id": "v1.2-demo",
    }
    assert first["release_authority"] == "verify-rag-bundle JSON"
    assert "release_id" not in first
    assert "bundle_counts" not in first
    assert first["poster_upload"] == {
        "count": 4545,
        "content_type": "image/webp",
        "destination_prefix": "assets/posters/derived/",
        "public": False,
    }
    assert first["bundle_upload"] == {
        "destination_prefix": "[FROM_VERIFIED_RELEASE]",
        "immutable_create_only": True,
    }
    assert first["image_template"] == (
        "us-central1-docker.pkg.dev/motionexpaiweb/hk-movie-rag/demo:<git-sha-12>"
    )
    assert first["steps"] == [
        "verify_source_release",
        "build_and_verify_bundle",
        "validate_exact_poster_allowlist",
        "upload_private_release",
        "build_image",
        "deploy_ingest_job",
        "execute_and_wait_ingest_job",
        "require_4658_reusable_and_12_new_final_state",
        "execute_and_wait_active_noop_rerun",
        "deploy_poster_verify_job",
        "execute_and_wait_poster_verify_job",
        "deploy_zero_traffic_tagged_candidate",
        "authenticated_candidate_smoke",
        "conditional_promotion",
        "authenticated_stable_smoke",
        "conditional_tag_cleanup",
        "emit_redacted_result",
    ]
    assert first["cloud_run_job"] == "hk-movie-rag-ingest"
    assert first["poster_verify_job"] == "hk-movie-rag-poster-verify"
    assert first["cloud_run_service"] == "hk-movie-rag-demo"
    assert first["service_iam"] == "invoker_iam_check_disabled_with_app_access_gate"
    assert first["gcs_publication"] is False
    serialized = json.dumps(first, sort_keys=True)
    assert "RAG_DEMO_ACCESS_KEY=" not in serialized
    assert "DB_PASSWORD=" not in serialized
    assert "delete" not in serialized.lower()


def test_artifact_describe_matches_current_sdk_parser_and_has_no_location_flag() -> None:
    gcloud = shutil.which("gcloud")
    if gcloud is None:
        pytest.skip("gcloud is unavailable")
    help_result = subprocess.run(
        [gcloud, "artifacts", "docker", "images", "describe", "--help"],
        check=False,
        capture_output=True,
        text=True,
        encoding="utf-8",
        timeout=60,
    )
    assert help_result.returncode == 0, help_result.stderr
    synopsis = help_result.stdout.split("DESCRIPTION", 1)[0]
    assert "--location" not in synopsis
    deploy_source = DEPLOY.read_text(encoding="utf-8")
    describe_block = deploy_source.split("'artifacts', 'docker', 'images', 'describe'", 1)[1].split(
        ")", 1
    )[0]
    assert "--location" not in describe_block


def test_deploy_rejects_untracked_provenance_but_ignores_gitignored_outputs(
    tmp_path: Path,
) -> None:
    repo = tmp_path / "repo"
    script_dir = repo / "scripts" / "gcp"
    script_dir.mkdir(parents=True)
    shutil.copy2(DEPLOY, script_dir / DEPLOY.name)
    (repo / "config").mkdir()
    (repo / "config" / "rag_demo_r2.yaml").write_text("fixture: true\n", encoding="utf-8")
    source = repo / "source"
    source.mkdir()
    (source / ".gitkeep").write_text("", encoding="utf-8")
    (repo / ".gitignore").write_text("ignored/\n", encoding="utf-8")
    _git(repo, "init")
    _git(repo, "config", "user.email", "tests@example.invalid")
    _git(repo, "config", "user.name", "tests")
    _git(repo, "add", ".")
    _git(repo, "commit", "-m", "fixture")
    (repo / "ignored").mkdir()
    (repo / "ignored" / "generated.tmp").write_text("ignored", encoding="utf-8")
    rogue = repo / "rogue.txt"
    rogue.write_text("must fail", encoding="utf-8")
    fake_bin = tmp_path / "bin"
    fake_bin.mkdir()
    (fake_bin / "gcloud.ps1").write_text("exit 99\n", encoding="utf-8")
    env = os.environ.copy()
    env["PATH"] = f"{fake_bin}{os.pathsep}{env['PATH']}"

    dirty = _run_deploy_apply(script_dir / DEPLOY.name, source, env)
    assert dirty.returncode != 0
    assert "clean worktree" in dirty.stderr

    rogue.unlink()
    ignored_only = _run_deploy_apply(script_dir / DEPLOY.name, source, env)
    assert ignored_only.returncode != 0
    assert "clean worktree" not in ignored_only.stderr


def _git(repo: Path, *args: str) -> None:
    subprocess.run(["git", *args], cwd=repo, check=True, capture_output=True)


def _run_deploy_apply(
    script: Path, source: Path, env: dict[str, str]
) -> subprocess.CompletedProcess[str]:
    return subprocess.run(
        [
            _powershell(),
            "-NoLogo",
            "-NoProfile",
            "-NonInteractive",
            "-File",
            str(script),
            "-ProjectId",
            "motionexpaiweb",
            "-Region",
            "us-central1",
            "-SourceRoot",
            str(source),
            "-Apply",
        ],
        check=False,
        capture_output=True,
        text=True,
        encoding="utf-8",
        env=env,
        timeout=30,
    )


def test_powershell_scripts_parse_without_execution() -> None:
    for script in (PROVISION, DEPLOY):
        command = (
            "$errors=$null; "
            f"[System.Management.Automation.Language.Parser]::ParseFile('{script}',"
            "[ref]$null,[ref]$errors) | Out-Null; "
            "if ($errors.Count) { $errors | ForEach-Object { $_.ToString() }; exit 1 }"
        )
        result = subprocess.run(
            [_powershell(), "-NoLogo", "-NoProfile", "-NonInteractive", "-Command", command],
            check=False,
            capture_output=True,
            text=True,
            encoding="utf-8",
        )
        assert result.returncode == 0, result.stdout + result.stderr


def test_deploy_preflight_rejects_posters_above_the_serving_ceiling() -> None:
    """Breaks if deployment mutates cloud state before detecting an oversized poster."""
    source = DEPLOY.read_text(encoding="utf-8")
    allowlist = source.split("function Assert-ExactPosterAllowlist", 1)[1].split(
        "function ", 1
    )[0]

    assert "[int64]$record.derived_byte_length -gt 16777216" in allowlist
    assert "$file.Length -gt 16777216" in allowlist


def test_container_context_excludes_data_pdfs_and_credentials() -> None:
    dockerfile = (ROOT / "Dockerfile").read_text(encoding="utf-8")
    dockerignore = (ROOT / ".dockerignore").read_text(encoding="utf-8").splitlines()

    assert dockerignore[0] == "**"
    assert "!src/**" in dockerignore
    assert "!pyproject.toml" in dockerignore
    assert "!uv.lock" in dockerignore
    assert all("data" not in line.lower() for line in dockerignore[1:])
    assert all(".env" not in line.lower() for line in dockerignore[1:])
    assert dockerfile.count(
        "python:3.13-slim@sha256:ffb752e139c0a19692a43af8d8523b274222dd68eebad5d583b45c2201c6e30a"
    ) == 2
    assert (
        "ghcr.io/astral-sh/uv:0.8.15@sha256:"
        "a5727064a0de127bdb7c9d3c1383f3a9ac307d9f2d8a391edc7896c54289ced0"
        in dockerfile
    )
    assert "uv sync --frozen --no-dev --no-editable" in dockerfile
    assert "USER app" in dockerfile
    assert "${PORT:-8080}" in dockerfile
    assert "+'/health'" in dockerfile
    assert "healthz" not in dockerfile
    assert "COPY ." not in dockerfile
    assert "src/**/__pycache__/" in dockerignore
    assert "src/**/*.py[cod]" in dockerignore


def _webp_bytes(color: tuple[int, int, int]) -> bytes:
    stream = io.BytesIO()
    Image.new("RGB", (3, 2), color).save(stream, format="WEBP", lossless=True)
    return stream.getvalue()


def _session_cookie_jar(
    *,
    domain_specified: bool = False,
    domain_initial_dot: bool = False,
    secure: bool = True,
    path: str = "/",
) -> http.cookiejar.CookieJar:
    jar = http.cookiejar.CookieJar()
    jar.set_cookie(
        http.cookiejar.Cookie(
            version=0,
            name="rag_demo_session",
            value=_SmokeHandler.session_token,
            port=None,
            port_specified=False,
            domain=".127.0.0.1" if domain_initial_dot else "127.0.0.1",
            domain_specified=domain_specified,
            domain_initial_dot=domain_initial_dot,
            path=path,
            path_specified=True,
            secure=secure,
            expires=None,
            discard=True,
            comment=None,
            comment_url=None,
            rest={},
        )
    )
    return jar


class _SmokeHandler(BaseHTTPRequestHandler):
    access_code = "top-secret-never-print"
    session_token = "opaque-session-token-never-print"
    serving_revision = (
        "hk-movie-rag-demo-00000-r2-abcdef123456-0123456789abcdef"
    )
    image_digest = f"sha256:{'1' * 64}"
    unavailable_movie_id = "1970_MTXD_001"
    extra_recommendation: ClassVar[str | None] = None
    person_credit_mismatch: ClassVar[bool] = False
    person_followup_overlap: ClassVar[bool] = False
    person_genre_mismatch: ClassVar[bool] = False
    comedy_non_exact_genre: ClassVar[bool] = False
    comedy_without_returned_poster: ClassVar[bool] = False
    followup_non_exact_genre: ClassVar[bool] = False
    person_non_exact_genre: ClassVar[bool] = False
    mixed_person_omits_clarification: ClassVar[bool] = False
    mixed_person_citation_only_leak: ClassVar[bool] = False
    mixed_person_movie_only_leak: ClassVar[bool] = False
    continuation_reset_wrong_person: ClassVar[bool] = False
    continuation_reset_wrong_genres: ClassVar[bool] = False
    continuation_reset_wrong_count: ClassVar[bool] = False
    continuation_reset_wong_overlap: ClassVar[bool] = False
    metadata_answer_missing_year: ClassVar[bool] = False
    metadata_answer_missing_director: ClassVar[bool] = False
    metadata_wrong_release_date: ClassVar[bool] = False
    metadata_wrong_director: ClassVar[bool] = False
    drunken_topic_drift: ClassVar[bool] = False
    aces_topic_drift: ClassVar[bool] = False
    drunken_question_echo: ClassVar[bool] = False
    aces_question_echo: ClassVar[bool] = False
    ood_evidence_leak: ClassVar[bool] = False
    irrelevant_answer: ClassVar[bool] = False
    wrong_citation: ClassVar[bool] = False
    null_poster_serves: ClassVar[bool] = False
    wrong_rights_status: ClassVar[bool] = False
    config_extra_secret: ClassVar[bool] = False
    accept_any_access_code: ClassVar[bool] = False
    wrong_code_sets_session_cookie: ClassVar[bool] = False
    accept_any_session_cookie: ClassVar[bool] = False
    public_access: ClassVar[bool] = False
    accept_tampered_config_cookie: ClassVar[bool] = False
    accept_tampered_chat_cookie: ClassVar[bool] = False
    accept_tampered_poster_cookie: ClassVar[bool] = False
    accept_session_token_suffix: ClassVar[bool] = False
    session_cookie_missing_attributes: ClassVar[bool] = False
    session_cookie_domain_attribute: ClassVar[bool] = False
    logout_cookie_missing_attributes: ClassVar[bool] = False
    logout_cookie_domain_attribute: ClassVar[bool] = False
    redirect_health: ClassVar[bool] = False
    redirect_config: ClassVar[bool] = False
    chat_requests: ClassVar[list[dict[str, Any]]] = []
    requested_posters: ClassVar[list[str]] = []
    poster_request_headers: ClassVar[list[tuple[str, str]]] = []
    poster_barrier: ClassVar[threading.Barrier | None] = None
    poster_active: ClassVar[int] = 0
    poster_max_active: ClassVar[int] = 0
    poster_body_size: ClassVar[int | None] = None
    poster_lock: ClassVar[threading.Lock] = threading.Lock()
    posters: ClassVar[dict[str, bytes]] = {
        "1978_ZQ_001": _webp_bytes((255, 0, 0)),
        "1982_ZJPD_001": _webp_bytes((0, 0, 255)),
        "CHOW_001": _webp_bytes((255, 128, 0)),
        "CHOW_002": _webp_bytes((255, 192, 0)),
        "CHOW_003": _webp_bytes((255, 255, 0)),
        "CHOW_004": _webp_bytes((0, 255, 0)),
        "CHOW_005": _webp_bytes((0, 255, 128)),
        "CHOW_006": _webp_bytes((0, 255, 255)),
        "CHOW_COMEDY_001": _webp_bytes((128, 0, 255)),
        "CHOW_COMEDY_002": _webp_bytes((192, 0, 255)),
        "CHOW_COMEDY_003": _webp_bytes((255, 0, 255)),
        "CHOW_ACTION_001": _webp_bytes((32, 128, 255)),
        "CHOW_ACTION_002": _webp_bytes((64, 160, 255)),
        "CHOW_ACTION_003": _webp_bytes((96, 192, 255)),
        "CHOW_ACTION_004": _webp_bytes((128, 224, 255)),
        "CHOW_ACTION_005": _webp_bytes((160, 255, 255)),
        "GOLDEN_MOVIE_001": _webp_bytes((96, 96, 96)),
    }

    def log_message(self, format: str, *args: object) -> None:
        del format, args

    def do_GET(self) -> None:
        if self.path in {"/oversized-success", "/oversized-error"}:
            body = b"x" * (DEFAULT_SMOKE_BODY_LIMIT + 1)
            self.send_response(413 if self.path == "/oversized-error" else 200)
            self.send_header("Content-Type", "application/json")
            self.send_header("Content-Length", str(len(body)))
            self.end_headers()
            self.wfile.write(body)
            return
        if self.path in {"/header-success", "/header-error"}:
            body = b"header-value-preservation"
            self.send_response(200 if self.path == "/header-success" else 418)
            self.send_header("X-CaSe-Probe", "MiXeD Value, 123")
            self.send_header("Content-Type", "application/octet-stream")
            self.send_header("Content-Length", str(len(body)))
            self.end_headers()
            self.wfile.write(body)
            return
        if self.path == "/health" and self.redirect_health:
            self.send_response(302)
            self.send_header("Location", "/redirected-health")
            self.end_headers()
            return
        if self.path in {"/health", "/redirected-health"}:
            self._json(200, {"status": "ok"})
            return
        if self.path == "/api/config" and self.redirect_config:
            self.send_response(302)
            self.send_header("Location", "/redirected-config")
            self.end_headers()
            return
        if self.path in {"/api/config", "/redirected-config"}:
            if not self._authenticated():
                self._json(401, {"detail": "authentication required"})
                return
            config: dict[str, Any] = {
                    "access_mode": "public_tech_demo" if self.public_access else "restricted_demo",
                    "rag_release_id": "v1.2-demo",
                    "status": "active",
                    "embedding_model": "gemini-embedding-2",
                    "embedding_dimension": 768,
                    "generation_model": "gemini-3.5-flash-lite",
                    "relevance_policy_sha256": (
                        "5242afc5e5c91b74dd012b558ed5eb929f4acf78e98defe6d14bf4f7cce1a8c2"
                    ),
                    "poster_authority_sha256": (
                        "ca99a5c04d8e61029f58e90cb3017730dc2b3511361834d7a0cf4860b54005e4"
                    ),
                    "serving_revision": self.serving_revision,
                    "image_digest": self.image_digest,
                    "counts": {
                        "movies": 4658,
                        "media_assets": 4658,
                        "metadata_passages": 4658,
                        "movie_documents": 2,
                        "pdf_passages": 6,
                        "embeddings": 4664,
                    },
                    "facets": {
                        "total": 4658,
                        "tier_s": 50,
                        "tier_a": 313,
                        "tier_b": 4295,
                        "pilot_movies": 24,
                    },
                }
            if self.config_extra_secret:
                config["database_password"] = "fixture-secret-must-not-leak"
            self._json(200, config)
            return
        if self.path.startswith("/api/posters/"):
            if not self._authenticated():
                self._json(401, {"detail": "authentication required"})
                return
            movie_id = self.path.rsplit("/", 1)[-1]
            self.requested_posters.append(movie_id)
            poster = self.posters.get(movie_id)
            if poster is not None and self.poster_body_size is not None:
                poster = b"x" * self.poster_body_size
            if poster is None and self.null_poster_serves:
                poster = self.posters["1978_ZQ_001"]
            if poster is None:
                self._json(404, {"detail": "not found"})
                return
            handler = type(self)
            handler.poster_request_headers.append(
                (self.headers.get("Cookie", ""), self.headers.get("User-Agent", ""))
            )
            barrier = handler.poster_barrier
            if barrier is not None:
                with handler.poster_lock:
                    handler.poster_active += 1
                    handler.poster_max_active = max(
                        handler.poster_max_active, handler.poster_active
                    )
                try:
                    barrier.wait(timeout=5)
                finally:
                    with handler.poster_lock:
                        handler.poster_active -= 1
            self.send_response(200)
            self.send_header("content-type", "image/webp")
            self.send_header("Content-Length", str(len(poster)))
            self.send_header("cache-control", "private, no-store")
            self.send_header(
                "x-rag-rights-status",
                "restricted" if self.wrong_rights_status else "unknown",
            )
            self.end_headers()
            self.wfile.write(poster)
            return
        self._json(404, {"detail": "not found"})

    def do_POST(self) -> None:
        body = self.rfile.read(int(self.headers.get("Content-Length", "0")))
        payload = json.loads(body or b"{}")
        if self.path == "/api/session":
            if self.public_access:
                self._json(404, {"detail": "not found"})
                return
            if (
                not self.accept_any_access_code
                and payload.get("access_code") != self.access_code
            ):
                if self.wrong_code_sets_session_cookie:
                    self.send_response(401)
                    self.send_header(
                        "Set-Cookie",
                        (
                            f"rag_demo_session={self.session_token}; Max-Age=3600; "
                            "Path=/; Secure; HttpOnly; SameSite=Strict"
                        ),
                    )
                    self.send_header(
                        "Set-Cookie", "benign_probe=present; Max-Age=60; Path=/; Secure"
                    )
                    self.end_headers()
                    return
                self._json(401, {"detail": "invalid demo code"})
                return
            self.send_response(204)
            attributes = (
                "Path=/; HttpOnly"
                if self.session_cookie_missing_attributes
                else "Max-Age=3600; Path=/; Secure; HttpOnly; SameSite=Strict"
            )
            if self.session_cookie_domain_attribute:
                attributes = f"{attributes}; Domain=127.0.0.1"
            self.send_header(
                "Set-Cookie",
                f"rag_demo_session={self.session_token}; {attributes}",
            )
            self.end_headers()
            return
        if self.path == "/api/chat":
            if not self._authenticated():
                self._json(401, {"detail": "authentication required"})
                return
            question = str(payload.get("question", ""))
            self.chat_requests.append(payload)
            if question in {
                "講一下英雄的成長",
                "聊聊朋友之間的信任",
                "《英雄》這本書值得讀嗎？",
            }:
                self._json(
                    200,
                    {
                        "answer_markdown": "我目前只能回答这个 Release 内的香港电影问题。",
                        "citations": [],
                        "movies": [],
                    },
                )
                return
            if question == "《证人》的导演是谁？":
                self._json(
                    200,
                    {
                        "answer_markdown": (
                            "這個片名在 Release 內對應多部同名電影；請改用以下其中一個 "
                            "movie ID：`1993_ZR_001`、`2008_ZR_001`。"
                        ),
                        "citations": [],
                        "movies": [],
                    },
                )
                return
            if question == "錯體追擊組合中誰是導演？":
                citation_id = "metadata:1995_CTZJZH_001"
                self._json(
                    200,
                    {
                        "answer_markdown": (
                            "《錯體追擊組合》的導演是董煒（即董瑋）。 "
                            f"[{citation_id}]"
                        ),
                        "citations": [
                            {
                                "citation_id": citation_id,
                                "movie_id": "1995_CTZJZH_001",
                                "movie_title": "錯體追擊組合",
                                "source_kind": "movie_metadata",
                                "page_number": None,
                                "source_filename": None,
                                "excerpt": "受控摘錄",
                            }
                        ],
                        "movies": [
                            {
                                "movie_id": "1995_CTZJZH_001",
                                "chinese_title": "錯體追擊組合",
                                "english_title": "Fox hunter",
                                "release_date": "1995-10-07",
                                "director": "董煒 (即董瑋)",
                                "cast": "梁琤、陳小春、於榮光",
                                "genre": "動作 / 犯罪",
                                "tier": "A",
                                "pilot_movie": False,
                                "poster_url": None,
                            }
                        ],
                    },
                )
                return
            if question in GENERAL_RELEVANCE_OOD_QUESTIONS:
                response: dict[str, Any] = {
                    "answer_markdown": "我目前只能回答这个 Release 内的香港电影问题。",
                    "citations": [],
                    "movies": [],
                }
                if self.ood_evidence_leak:
                    response["citations"] = [
                        {
                            "citation_id": "metadata:1978_ZQ_001",
                            "movie_id": "1978_ZQ_001",
                        }
                    ]
                    response["movies"] = [
                        {"movie_id": "1978_ZQ_001", "poster_url": None}
                    ]
                self._json(200, response)
                return
            if question in GENERAL_RELEVANCE_MOVIE_QUESTIONS:
                self._general_relevance_movie_response(
                    GENERAL_RELEVANCE_MOVIE_CASE_BY_QUESTION[question]
                )
                return
            if question == "想看周星驰和成龍電影，推薦幾部":
                clarification = (
                    "請只指定一位人物後再試。"
                    if self.mixed_person_omits_clarification
                    else "目前一次只支援一位正向人物；請只指定一位導演或演員後再試。"
                )
                citation_id = "metadata:WONG_001"
                citations = (
                    [
                        {
                            "citation_id": citation_id,
                            "movie_id": "WONG_001",
                            "movie_title": "測試電影 WONG_001",
                            "source_kind": "movie_metadata",
                            "page_number": None,
                            "source_filename": None,
                            "excerpt": "不應出現的受控摘錄",
                        }
                    ]
                    if self.mixed_person_citation_only_leak
                    else []
                )
                movies = (
                    [
                        {
                            "movie_id": "WONG_001",
                            "chinese_title": "測試電影 WONG_001",
                            "english_title": "Fixture",
                            "release_date": "2000-01-01",
                            "director": "王家衛",
                            "cast": "測試演員",
                            "genre": "劇情",
                            "tier": "S",
                            "pilot_movie": False,
                            "poster_url": None,
                        }
                    ]
                    if self.mixed_person_movie_only_leak
                    else []
                )
                self._json(
                    200,
                    {
                        "answer_markdown": clarification,
                        "citations": citations,
                        "movies": movies,
                    },
                )
                return
            if "只推薦 S 級" in question:
                movies = [
                    ("1978_ZQ_001", "喜劇 / 動作", "S", "/api/posters/1978_ZQ_001"),
                    ("1982_ZJPD_001", "喜劇 / 動作", "S", "/api/posters/1982_ZJPD_001"),
                    ("S_WITHOUT_POSTER", "喜劇", "S", None),
                ]
                if self.extra_recommendation == "s_tier":
                    movies.append(("S_EXTRA", "喜劇", "S", None))
                self._recommendations(movies)
                return
            history = payload.get("history")
            history_questions = [
                str(exchange.get("question", ""))
                for exchange in history
                if isinstance(history, list) and isinstance(exchange, dict)
            ] if isinstance(history, list) else []
            if "再推薦" in question and any(
                "周星馳" in item or "周星驰" in item for item in history_questions
            ):
                movies = [
                    ("CHOW_004", "動作 / 喜劇", "A", "/api/posters/CHOW_004"),
                    ("CHOW_005", "喜劇", "B", "/api/posters/CHOW_005"),
                    ("CHOW_006", "喜劇 / 劇情", "S", "/api/posters/CHOW_006"),
                ]
                if self.person_followup_overlap:
                    movies[0] = (
                        "CHOW_001",
                        "動作 / 喜劇",
                        "A",
                        "/api/posters/CHOW_001",
                    )
                self._recommendations(movies, cast="周星馳、吳孟達")
                return
            if "再推薦" in question:
                if not isinstance(history, list) or not history:
                    self._json(422, {"detail": "history required"})
                    return
                movies = [
                    (
                        "FOLLOWUP_001",
                        "非喜劇" if self.followup_non_exact_genre else "喜劇",
                        "A",
                        None,
                    ),
                    ("FOLLOWUP_002", "動作 / 喜劇", "B", None),
                    ("FOLLOWUP_003", "喜劇", "S", None),
                ]
                if self.extra_recommendation == "followup":
                    movies.append(("FOLLOWUP_004", "喜劇", "A", None))
                self._recommendations(movies)
                return
            if "推薦三部喜劇" in question:
                if self.comedy_without_returned_poster:
                    self._recommendations(
                        [
                            ("COMEDY_NULL_001", "喜劇", "S", None),
                            ("COMEDY_NULL_002", "喜劇 / 動作", "A", None),
                            ("COMEDY_NULL_003", "喜劇", "B", None),
                        ]
                    )
                    return
                self._recommendations(
                    [
                        (
                            "1978_ZQ_001",
                            "非喜劇" if self.comedy_non_exact_genre else "喜劇 / 動作",
                            "S",
                            "/api/posters/1978_ZQ_001",
                        ),
                        ("1982_ZJPD_001", "喜劇 / 動作", "S", "/api/posters/1982_ZJPD_001"),
                        ("2016_SFB_001", "喜劇", "B", None),
                    ]
                )
                return
            if question == "王家卫的电影":
                self._recommendations(
                    [
                        ("WONG_001", "劇情", "S", None),
                        ("WONG_002", "愛情", "A", None),
                        ("WONG_003", "犯罪", "A", None),
                        ("WONG_004", "劇情", "B", None),
                        ("WONG_005", "愛情", "B", None),
                    ],
                    director="王家衞",
                )
                return
            if question in {"周星驰的动作喜剧", "周星驰的动作喜剧还有吗"}:
                is_wang_history = history_questions == ["王家卫的电影"]
                movies = [
                    ("CHOW_ACTION_001", "動作 / 喜劇", "S", "/api/posters/CHOW_ACTION_001"),
                    ("CHOW_ACTION_002", "動作 / 喜劇", "A", "/api/posters/CHOW_ACTION_002"),
                    ("CHOW_ACTION_003", "動作 / 喜劇", "A", "/api/posters/CHOW_ACTION_003"),
                    ("CHOW_ACTION_004", "動作 / 喜劇", "B", "/api/posters/CHOW_ACTION_004"),
                    ("CHOW_ACTION_005", "動作 / 喜劇", "B", "/api/posters/CHOW_ACTION_005"),
                ]
                is_continuation_reset = (
                    question == "周星驰的动作喜剧还有吗" and is_wang_history
                )
                if self.continuation_reset_wrong_genres and is_continuation_reset:
                    movies[0] = (
                        "CHOW_ACTION_001",
                        "喜劇",
                        "S",
                        "/api/posters/CHOW_ACTION_001",
                    )
                if self.continuation_reset_wrong_count and is_continuation_reset:
                    movies.pop()
                if self.continuation_reset_wong_overlap and is_continuation_reset:
                    movies[0] = ("WONG_001", "動作 / 喜劇", "S", None)
                self._recommendations(
                    movies,
                    cast=(
                        "王家衛"
                        if self.continuation_reset_wrong_person and is_wang_history
                        else "周星馳、吳孟達"
                    ),
                )
                return
            if ("周星馳" in question or "周星驰" in question) and "喜劇" in question:
                genre = (
                    "非喜劇"
                    if self.person_non_exact_genre
                    else "動作"
                    if self.person_genre_mismatch
                    else "喜劇 / 動作"
                )
                self._recommendations(
                    [
                        (
                            "CHOW_COMEDY_001",
                            genre,
                            "S",
                            "/api/posters/CHOW_COMEDY_001",
                        ),
                        (
                            "CHOW_COMEDY_002",
                            "喜劇",
                            "A",
                            "/api/posters/CHOW_COMEDY_002",
                        ),
                        (
                            "CHOW_COMEDY_003",
                            "喜劇 / 劇情",
                            "B",
                            "/api/posters/CHOW_COMEDY_003",
                        ),
                    ],
                    cast="周星馳、吳孟達",
                )
                return
            if question in {"推荐周星驰的好电影", "推薦周星馳的好電影"}:
                self._recommendations(
                    [
                        ("CHOW_001", "喜劇", "S", "/api/posters/CHOW_001"),
                        ("CHOW_002", "動作", "A", "/api/posters/CHOW_002"),
                        ("CHOW_003", "劇情", "B", "/api/posters/CHOW_003"),
                        ("CHOW_004", "喜劇", "A", "/api/posters/CHOW_004"),
                        ("CHOW_005", "動作", "B", "/api/posters/CHOW_005"),
                    ],
                    director="周星馳",
                    cast="其他演員",
                )
                return
            if "周星馳" in question or "周星驰" in question:
                self._recommendations(
                    [
                        ("CHOW_001", "喜劇", "S", "/api/posters/CHOW_001"),
                        ("CHOW_002", "動作", "A", "/api/posters/CHOW_002"),
                        ("CHOW_003", "劇情", "B", "/api/posters/CHOW_003"),
                    ],
                    cast=("其他演員" if self.person_credit_mismatch else "周星馳、吳孟達"),
                )
                return
            if "最佳拍檔" in question:
                citation_id = "pdf:aces-go-places-deep-analysis-v1:p2"
                movie_id = "1982_ZJPD_001"
                page = 2
                kind = "pdf_page"
                source_filename = "S 級港⽚-最佳拍檔.pdf"
                if self.aces_topic_drift:
                    answer_text = "《警察故事》是一部城市動作喜劇，風格鮮明。"
                elif self.aces_question_echo:
                    answer_text = "《最佳拍檔》的都市動作與喜劇風格有何特色？"
                else:
                    answer_text = (
                        "《最佳拍檔》把中環與海底隧道變成飛車特技舞台，融入本土"
                        "俚語與荒誕笑料，形成現代港片範式。"
                    )
            elif "美學" in question:
                citation_id = "pdf:drunken-master-deep-analysis-v1:p1"
                movie_id = "1978_ZQ_001"
                page = 1
                kind = "pdf_page"
                source_filename = "S 級港⽚-醉拳.pdf"
                if self.drunken_topic_drift:
                    answer_text = "《少林寺》以功夫鏡頭製造笑點，並配合鑼鼓。"
                elif self.drunken_question_echo:
                    answer_text = "《醉拳》的動作美學如何結合喜劇節奏？"
                else:
                    answer_text = "《醉拳》以功夫鏡頭、身體笑點及戲曲拍節形成諧趣。"
            else:
                citation_id = "metadata:1978_ZQ_001"
                movie_id = "1978_ZQ_001"
                page = None
                kind = "movie_metadata"
                source_filename = None
                if self.metadata_answer_missing_year:
                    answer_text = "《醉拳》的導演是袁和平。"
                elif self.metadata_answer_missing_director:
                    answer_text = "《醉拳》於 1978 年上映。"
                else:
                    answer_text = "《醉拳》於 1978 年上映，導演是袁和平。"
            if "滿天星斗" in question:
                citation_id = "metadata:1970_MTXD_001"
                movie_id = "1970_MTXD_001"
                page = None
                kind = "movie_metadata"
                source_filename = None
                answer_text = "《滿天星斗》的受控資料。"
            movie_title = {
                "1970_MTXD_001": "滿天星斗",
                "1978_ZQ_001": "醉拳",
                "1982_ZJPD_001": "最佳拍檔",
            }[movie_id]
            self._json(
                200,
                {
                    "answer_markdown": f"{answer_text} [{citation_id}]",
                    "citations": [
                        {
                            "citation_id": citation_id,
                            "movie_id": movie_id,
                            "movie_title": movie_title,
                            "source_kind": kind,
                            "page_number": page,
                            "source_filename": source_filename,
                            "excerpt": "受控摘錄",
                        }
                    ],
                    "movies": [
                        {
                            "movie_id": movie_id,
                            "chinese_title": movie_title,
                            "english_title": "Fixture",
                            "release_date": (
                                "2000-01-01"
                                if self.metadata_wrong_release_date
                                else "1978-10-05"
                                if movie_id == "1978_ZQ_001"
                                else "2000-01-01"
                            ),
                            "director": (
                                "錯誤導演"
                                if self.metadata_wrong_director
                                else "袁和平"
                                if movie_id == "1978_ZQ_001"
                                else "測試導演"
                            ),
                            "cast": "測試演員",
                            "genre": "喜劇 / 動作",
                            "tier": "S",
                            "pilot_movie": False,
                            "poster_url": (
                                None
                                if movie_id == self.unavailable_movie_id
                                else f"/api/posters/{movie_id}"
                            ),
                        }
                    ],
                },
            )
            return
        self._json(404, {"detail": "not found"})

    def _general_relevance_movie_response(self, case_id: str) -> None:
        if case_id in GENERAL_RELEVANCE_CONTROLLED_INSUFFICIENT_CASE_IDS:
            self._json(
                200,
                {
                    "answer_markdown": GENERAL_RELEVANCE_CONTROLLED_INSUFFICIENCY,
                    "citations": [],
                    "movies": [],
                },
            )
            return
        if case_id in {
            "cal-movie-01",
            "cal-movie-02",
            "cal-movie-05",
            "cal-movie-06",
            "cal-movie-08",
        }:
            self._bounded_pdf_movie_response(case_id)
            return
        (
            movie_id,
            chinese_title,
            answer_text,
            release_date,
            director,
            cast,
            genre,
        ) = GENERAL_RELEVANCE_MOVIE_FIXTURES[case_id]
        citation_id = f"metadata:{movie_id}"
        self._json(
            200,
            {
                "answer_markdown": f"《{chinese_title}》：{answer_text}。[{citation_id}]",
                "citations": [
                    {
                        "citation_id": citation_id,
                        "movie_id": movie_id,
                        "movie_title": chinese_title,
                        "source_kind": "movie_metadata",
                        "page_number": None,
                        "source_filename": None,
                        "excerpt": "個案專屬受控摘錄",
                    }
                ],
                "movies": [
                    {
                        "movie_id": movie_id,
                        "chinese_title": chinese_title,
                        "english_title": "Case-specific fixture",
                        "release_date": release_date,
                        "director": director,
                        "cast": cast,
                        "genre": genre,
                        "tier": "A",
                        "pilot_movie": False,
                        "poster_url": None,
                    }
                ],
            },
        )

    def _bounded_pdf_movie_response(self, case_id: str) -> None:
        scope_notes = {
            "cal-movie-01": (
                "基於目前 Release 內僅有的兩份深度文檔，以下只比較《醉拳》和"
                "《最佳拍檔》的動作喜劇特色，不能外推為所有香港動作喜劇的共同特徵。"
            ),
            "cal-movie-02": (
                "目前 Release 只有《醉拳》一部七十年代功夫片的深度文檔，因此"
                "以下是單片觀察，不能證明七十年代功夫片的「常見」元素。"
            ),
            "cal-movie-05": (
                "目前深度文檔只能以《醉拳》為例，說明師徒關係如何表現，"
                "不能概括全部香港電影。"
            ),
            "cal-movie-06": (
                "目前深度文檔只能以《最佳拍檔》為例，說明都市空間如何呈現，"
                "不能概括全部港產片。"
            ),
            "cal-movie-08": (
                "在目前有深度文檔的電影中，《最佳拍檔》可被可靠確認為節奏明快的"
                "香港動作片；這不代表未被本次證據覆蓋的影片也已驗證。"
            ),
        }
        passage_ids = {
            "cal-movie-01": (
                "pdf:drunken-master-deep-analysis-v1:p1",
                "pdf:aces-go-places-deep-analysis-v1:p1",
            ),
            "cal-movie-02": ("pdf:drunken-master-deep-analysis-v1:p1",),
            "cal-movie-05": ("pdf:drunken-master-deep-analysis-v1:p2",),
            "cal-movie-06": ("pdf:aces-go-places-deep-analysis-v1:p1",),
            "cal-movie-08": ("pdf:aces-go-places-deep-analysis-v1:p1",),
        }[case_id]
        passage_data = {
            "pdf:drunken-master-deep-analysis-v1:p1": (
                "1978_ZQ_001",
                "醉拳",
                1,
                "S 級港⽚-醉拳.pdf",
                "功夫武打以長鏡頭、道具和動作笑點形成諧趣。",
            ),
            "pdf:drunken-master-deep-analysis-v1:p2": (
                "1978_ZQ_001",
                "醉拳",
                2,
                "S 級港⽚-醉拳.pdf",
                "黃飛鴻拜蘇乞兒為師，訓練推動師徒關係的成長與轉變。",
            ),
            "pdf:aces-go-places-deep-analysis-v1:p1": (
                "1982_ZJPD_001",
                "最佳拍檔",
                1,
                "S 級港⽚-最佳拍檔.pdf",
                "中環、海底隧道與貨櫃場承載飛車爆破，剪輯節奏明快。",
            ),
        }
        citations = []
        movies = []
        answer_lines = [scope_notes[case_id]]
        for passage_id in passage_ids:
            movie_id, title, page, source_filename, excerpt = passage_data[passage_id]
            citations.append(
                {
                    "citation_id": passage_id,
                    "movie_id": movie_id,
                    "movie_title": title,
                    "source_kind": "pdf_page",
                    "page_number": page,
                    "source_filename": source_filename,
                    "excerpt": excerpt,
                }
            )
            movies.append(
                {
                    "movie_id": movie_id,
                    "chinese_title": title,
                    "english_title": (
                        "Drunken Master"
                        if movie_id == "1978_ZQ_001"
                        else "Aces Go Places"
                    ),
                    "release_date": (
                        "1978-10-05" if movie_id == "1978_ZQ_001" else "1982-01-16"
                    ),
                    "director": "袁和平" if movie_id == "1978_ZQ_001" else "曾志偉",
                    "cast": "成龍、袁小田" if movie_id == "1978_ZQ_001" else "許冠傑、麥嘉",
                    "genre": (
                        "功夫 / 動作 / 喜劇"
                        if movie_id == "1978_ZQ_001"
                        else "動作 / 喜劇"
                    ),
                    "tier": "S",
                    "pilot_movie": True,
                    "poster_url": f"/api/posters/{movie_id}",
                }
            )
            answer_lines.append(f"- 《{title}》：{excerpt} [{passage_id}]")
        self._json(
            200,
            {
                "answer_markdown": "\n".join(answer_lines),
                "citations": citations,
                "movies": movies,
            },
        )

    def _recommendations(
        self,
        movies: list[tuple[str, str, str, str | None]],
        *,
        cast: str = "測試演員",
        director: str = "測試導演",
    ) -> None:
        records = [
            {
                "movie_id": movie_id,
                "chinese_title": f"測試電影 {movie_id}",
                "genre": genre,
                "tier": tier,
                "poster_url": poster,
            }
            for movie_id, genre, tier, poster in movies
        ]
        answer_lines = [
            (
                "天氣很好。" if self.irrelevant_answer and index == 0 else
                f"《{record['chinese_title']}》：符合受控推薦條件。"
            )
            + (
                "[metadata:WRONG_MOVIE]"
                if self.wrong_citation and index == 0
                else f"[metadata:{record['movie_id']}]"
            )
            for index, record in enumerate(records)
        ]
        self._json(
            200,
            {
                "answer_markdown": "\n".join(answer_lines),
                "citations": [
                    {
                        "citation_id": f"metadata:{movie_id}",
                        "movie_id": movie_id,
                        "movie_title": record["chinese_title"],
                        "source_kind": "movie_metadata",
                        "page_number": None,
                        "source_filename": None,
                        "excerpt": "受控摘錄",
                    }
                    for record, (movie_id, _genre, _tier, _poster) in zip(
                        records, movies, strict=True
                    )
                ],
                "movies": [
                    {
                        "movie_id": movie_id,
                        "chinese_title": record["chinese_title"],
                        "english_title": "Fixture",
                        "release_date": "2000-01-01",
                        "director": director,
                        "cast": cast,
                        "genre": genre,
                        "tier": tier,
                        "pilot_movie": False,
                        "poster_url": poster,
                    }
                    for record, (movie_id, genre, tier, poster) in zip(
                        records, movies, strict=True
                    )
                ],
            },
        )

    def do_DELETE(self) -> None:
        if self.path == "/api/session":
            if self.public_access:
                self._json(404, {"detail": "not found"})
                return
            self.send_response(204)
            attributes = (
                "Max-Age=0; Path=/; HttpOnly"
                if self.logout_cookie_missing_attributes
                else "Max-Age=0; Path=/; Secure; HttpOnly; SameSite=Strict"
            )
            if self.logout_cookie_domain_attribute:
                attributes = f"{attributes}; Domain=127.0.0.1"
            self.send_header("Set-Cookie", f"rag_demo_session=; {attributes}")
            self.end_headers()
            return
        self._json(404, {"detail": "not found"})

    def _authenticated(self) -> bool:
        if self.public_access:
            return True
        raw_cookie = self.headers.get("Cookie", "")
        assignments = [
            part.partition("=")
            for part in raw_cookie.split(";")
            if part.strip()
        ]
        session_assignments = [
            value.strip()
            for name, separator, value in assignments
            if separator == "=" and name.strip() == "rag_demo_session"
        ]
        if len(session_assignments) != 1:
            return False
        parsed = SimpleCookie()
        parsed.load(raw_cookie)
        morsel = parsed.get("rag_demo_session")
        session_value = morsel.value if morsel is not None else None
        if self.accept_any_session_cookie:
            return session_value is not None
        if (
            self.accept_session_token_suffix
            and isinstance(session_value, str)
            and session_value.startswith(f"{self.session_token}-")
        ):
            return True
        accepts_tampered = (
            self.path == "/api/config" and self.accept_tampered_config_cookie
            or self.path == "/api/chat" and self.accept_tampered_chat_cookie
            or self.path.startswith("/api/posters/")
            and self.accept_tampered_poster_cookie
        )
        if (
            accepts_tampered
            and session_value is not None
            and session_value != self.session_token
        ):
            return True
        return session_value == self.session_token

    def _json(self, status: int, payload: dict[str, Any]) -> None:
        body = json.dumps(payload).encode("utf-8")
        self.send_response(status)
        self.send_header("Content-Type", "application/json")
        self.send_header("Content-Length", str(len(body)))
        self.end_headers()
        self.wfile.write(body)


@contextmanager
def _smoke_server(*, poster_barrier: threading.Barrier | None = None) -> Iterator[str]:
    _SmokeHandler.requested_posters = []
    _SmokeHandler.poster_request_headers = []
    _SmokeHandler.poster_barrier = poster_barrier
    _SmokeHandler.poster_active = 0
    _SmokeHandler.poster_max_active = 0
    _SmokeHandler.chat_requests = []
    server = ThreadingHTTPServer(("127.0.0.1", 0), _SmokeHandler)
    thread = threading.Thread(target=server.serve_forever, daemon=True)
    thread.start()
    try:
        host, port = server.server_address
        yield f"http://{host}:{port}"
    finally:
        _SmokeHandler.poster_barrier = None
        server.shutdown()
        thread.join(timeout=5)
        server.server_close()


@contextmanager
def _redirecting_poster_servers() -> Iterator[tuple[str, str, list[str]]]:
    sink_cookies: list[str] = []
    poster = _SmokeHandler.posters["1978_ZQ_001"]

    class SinkHandler(BaseHTTPRequestHandler):
        def log_message(self, format: str, *args: object) -> None:
            del format, args

        def do_GET(self) -> None:
            sink_cookies.append(self.headers.get("Cookie", ""))
            self.send_response(200)
            self.send_header("Content-Type", "image/webp")
            self.send_header("Content-Length", str(len(poster)))
            self.send_header("Cache-Control", "private, no-store")
            self.send_header("X-RAG-Rights-Status", "unknown")
            self.end_headers()
            self.wfile.write(poster)

    sink = ThreadingHTTPServer(("127.0.0.1", 0), SinkHandler)
    sink_thread = threading.Thread(target=sink.serve_forever, daemon=True)
    sink_thread.start()
    sink_host, sink_port = sink.server_address
    sink_url = f"http://{sink_host}:{sink_port}"

    class RedirectHandler(BaseHTTPRequestHandler):
        def log_message(self, format: str, *args: object) -> None:
            del format, args

        def do_GET(self) -> None:
            self.send_response(302)
            self.send_header("Location", f"{sink_url}/captured-poster")
            self.end_headers()

    origin = ThreadingHTTPServer(("127.0.0.1", 0), RedirectHandler)
    origin_thread = threading.Thread(target=origin.serve_forever, daemon=True)
    origin_thread.start()
    try:
        origin_host, origin_port = origin.server_address
        yield f"http://{origin_host}:{origin_port}", sink_url, sink_cookies
    finally:
        origin.shutdown()
        origin_thread.join(timeout=5)
        origin.server_close()
        sink.shutdown()
        sink_thread.join(timeout=5)
        sink.server_close()


@pytest.mark.parametrize(
    ("path", "expected_status"),
    [("/header-success", 200), ("/header-error", 418)],
)
def test_live_smoke_request_casefolds_success_and_error_headers_without_changing_values(
    path: str, expected_status: int
) -> None:
    module_spec = importlib.util.spec_from_file_location("header_case_live_smoke", SMOKE)
    assert module_spec is not None and module_spec.loader is not None
    module = importlib.util.module_from_spec(module_spec)
    sys.modules[module_spec.name] = module
    module_spec.loader.exec_module(module)

    with _smoke_server() as base_url:
        result = module._request(
            urllib.request.build_opener(),
            base_url,
            "GET",
            path,
            timeout=5,
        )

    assert result.status == expected_status
    assert result.body == b"header-value-preservation"
    assert result.headers["x-case-probe"] == "MiXeD Value, 123"
    assert result.headers["content-type"] == "application/octet-stream"
    assert all(name == name.casefold() for name in result.headers)


def test_live_smoke_success_response_reads_only_default_limit_plus_one() -> None:
    module_spec = importlib.util.spec_from_file_location("bounded_success_smoke", SMOKE)
    assert module_spec is not None and module_spec.loader is not None
    module = importlib.util.module_from_spec(module_spec)
    sys.modules[module_spec.name] = module
    module_spec.loader.exec_module(module)
    read_sizes: list[int] = []

    class Response:
        status = 200
        headers: ClassVar[dict[str, str]] = {}

        def __enter__(self) -> Self:
            return self

        def __exit__(self, *_args: object) -> None:
            return None

        def read(self, size: int = -1) -> bytes:
            read_sizes.append(size)
            return b"{}"

    class Opener:
        def open(self, _request: object, *, timeout: float) -> Response:
            del timeout
            return Response()

    result = module._request(Opener(), "http://localhost", "GET", "/health", timeout=5)

    assert result.status == 200
    assert read_sizes == [DEFAULT_SMOKE_BODY_LIMIT + 1]


def test_live_smoke_error_response_reads_only_default_limit_plus_one() -> None:
    module_spec = importlib.util.spec_from_file_location("bounded_error_smoke", SMOKE)
    assert module_spec is not None and module_spec.loader is not None
    module = importlib.util.module_from_spec(module_spec)
    sys.modules[module_spec.name] = module
    module_spec.loader.exec_module(module)
    read_sizes: list[int] = []

    class Body(io.BytesIO):
        def read(self, size: int = -1) -> bytes:
            read_sizes.append(size)
            return super().read(size)

    class Opener:
        def open(self, request: object, *, timeout: float) -> None:
            del timeout
            raise urllib.error.HTTPError(
                request.full_url,
                413,
                "payload too large",
                {},
                Body(b"{}"),
            )

    result = module._request(Opener(), "http://localhost", "GET", "/error", timeout=5)

    assert result.status == 413
    assert read_sizes == [DEFAULT_SMOKE_BODY_LIMIT + 1]


@pytest.mark.parametrize("path", ["/oversized-success", "/oversized-error"])
def test_live_smoke_rejects_oversized_success_and_error_bodies(path: str) -> None:
    module_spec = importlib.util.spec_from_file_location("oversized_http_smoke", SMOKE)
    assert module_spec is not None and module_spec.loader is not None
    module = importlib.util.module_from_spec(module_spec)
    sys.modules[module_spec.name] = module
    module_spec.loader.exec_module(module)
    with (
        _smoke_server() as base_url,
        pytest.raises(module.SmokeFailure, match="response body exceeds limit"),
    ):
        module._request(
            urllib.request.build_opener(),
            base_url,
            "GET",
            path,
            timeout=5,
        )


def test_live_smoke_poster_body_uses_larger_bounded_limit() -> None:
    module_spec = importlib.util.spec_from_file_location("poster_limit_smoke", SMOKE)
    assert module_spec is not None and module_spec.loader is not None
    module = importlib.util.module_from_spec(module_spec)
    sys.modules[module_spec.name] = module
    module_spec.loader.exec_module(module)
    _SmokeHandler.poster_body_size = DEFAULT_SMOKE_BODY_LIMIT + 1
    try:
        with _smoke_server() as base_url:
            results = module._parallel_mobile_poster_requests(
                base_url,
                ["/api/posters/1978_ZQ_001"],
                5,
                f"rag_demo_session={_SmokeHandler.session_token}",
            )
    finally:
        _SmokeHandler.poster_body_size = None

    assert len(results) == 1
    assert len(results[0].body) == DEFAULT_SMOKE_BODY_LIMIT + 1


def test_live_smoke_rejects_poster_body_over_poster_limit() -> None:
    module_spec = importlib.util.spec_from_file_location("poster_oversized_smoke", SMOKE)
    assert module_spec is not None and module_spec.loader is not None
    module = importlib.util.module_from_spec(module_spec)
    sys.modules[module_spec.name] = module
    module_spec.loader.exec_module(module)
    _SmokeHandler.poster_body_size = POSTER_SMOKE_BODY_LIMIT + 1
    try:
        with (
            _smoke_server() as base_url,
            pytest.raises(module.SmokeFailure, match="response body exceeds limit"),
        ):
            module._parallel_mobile_poster_requests(
                base_url,
                ["/api/posters/1978_ZQ_001"],
                5,
                f"rag_demo_session={_SmokeHandler.session_token}",
            )
    finally:
        _SmokeHandler.poster_body_size = None


@pytest.mark.parametrize("configured", [None, "0" * 64])
def test_live_smoke_requires_exact_poster_authority_digest(configured: str | None) -> None:
    """Breaks if live acceptance can approve an unreviewed poster-serving decision."""
    module_spec = importlib.util.spec_from_file_location("authority_live_smoke", SMOKE)
    assert module_spec is not None and module_spec.loader is not None
    module = importlib.util.module_from_spec(module_spec)
    sys.modules[module_spec.name] = module
    module_spec.loader.exec_module(module)
    payload: dict[str, object] = {
        "access_mode": "restricted_demo",
        "rag_release_id": "v1.2-demo",
        "status": "active",
        "embedding_model": "gemini-embedding-2",
        "embedding_dimension": 768,
        "generation_model": "gemini-3.5-flash-lite",
        "relevance_policy_sha256": (
            "5242afc5e5c91b74dd012b558ed5eb929f4acf78e98defe6d14bf4f7cce1a8c2"
        ),
        "counts": {
            "movies": 4658,
            "media_assets": 4658,
            "metadata_passages": 4658,
            "movie_documents": 2,
            "pdf_passages": 6,
            "embeddings": 4664,
        },
        "facets": {
            "total": 4658,
            "tier_s": 50,
            "tier_a": 313,
            "tier_b": 4295,
            "pilot_movies": 24,
        },
    }
    if configured is not None:
        payload["poster_authority_sha256"] = configured
    result = module.HttpResult(200, {}, json.dumps(payload).encode("utf-8"))

    with pytest.raises(module.SmokeFailure, match="release contract"):
        module._assert_config(
            result,
            expected_revision=_SmokeHandler.serving_revision,
            expected_image_digest=_SmokeHandler.image_digest,
        )


@pytest.mark.parametrize("configured", [None, "0" * 64])
def test_live_smoke_requires_exact_relevance_policy_digest(
    configured: str | None,
) -> None:
    """Breaks if live acceptance can approve a revision with another relevance policy."""
    module_spec = importlib.util.spec_from_file_location("relevance_live_smoke", SMOKE)
    assert module_spec is not None and module_spec.loader is not None
    module = importlib.util.module_from_spec(module_spec)
    sys.modules[module_spec.name] = module
    module_spec.loader.exec_module(module)
    payload: dict[str, object] = {
        "access_mode": "restricted_demo",
        "rag_release_id": "v1.2-demo",
        "status": "active",
        "embedding_model": "gemini-embedding-2",
        "embedding_dimension": 768,
        "generation_model": "gemini-3.5-flash-lite",
        "poster_authority_sha256": (
            "ca99a5c04d8e61029f58e90cb3017730dc2b3511361834d7a0cf4860b54005e4"
        ),
        "counts": {
            "movies": 4658,
            "media_assets": 4658,
            "metadata_passages": 4658,
            "movie_documents": 2,
            "pdf_passages": 6,
            "embeddings": 4664,
        },
        "facets": {
            "total": 4658,
            "tier_s": 50,
            "tier_a": 313,
            "tier_b": 4295,
            "pilot_movies": 24,
        },
    }
    if configured is not None:
        payload["relevance_policy_sha256"] = configured
    result = module.HttpResult(200, {}, json.dumps(payload).encode("utf-8"))

    with pytest.raises(module.SmokeFailure, match="release contract"):
        module._assert_config(
            result,
            expected_revision=_SmokeHandler.serving_revision,
            expected_image_digest=_SmokeHandler.image_digest,
        )


def test_live_smoke_binds_r2_config_to_verified_manifest_and_dynamic_counts() -> None:
    """Breaks if an R2 deployment can substitute another child manifest or old counts."""
    module_spec = importlib.util.spec_from_file_location("r2_config_live_smoke", SMOKE)
    assert module_spec is not None and module_spec.loader is not None
    module = importlib.util.module_from_spec(module_spec)
    sys.modules[module_spec.name] = module
    module_spec.loader.exec_module(module)
    counts = {
        "movies": 4658,
        "media_assets": 4658,
        "metadata_passages": 4658,
        "movie_documents": 3,
        "pdf_passages": 12,
        "embeddings": 4670,
    }
    facets = {
        "total": 4658,
        "tier_s": 50,
        "tier_a": 313,
        "tier_b": 4295,
        "pilot_movies": 24,
    }
    authorities = module._SmokeAuthorities(
        release_id="v1.2-demo-r2",
        manifest_sha256="c" * 64,
        access_mode="restricted_demo",
        embedding_model="gemini-embedding-2",
        embedding_dimension=768,
        generation_model="gemini-3.5-flash-lite",
        relevance_policy_sha256="a" * 64,
        poster_authority_sha256="b" * 64,
        counts=counts,
        facets=facets,
        citation_authorities={},
        poster_expectations={},
        pilot_movie_ids=frozenset(),
        deep_cases=(),
    )
    payload = {
        "access_mode": "restricted_demo",
        "rag_release_id": "v1.2-demo-r2",
        "manifest_sha256": "c" * 64,
        "status": "active",
        "embedding_model": "gemini-embedding-2",
        "embedding_dimension": 768,
        "generation_model": "gemini-3.5-flash-lite",
        "relevance_policy_sha256": "a" * 64,
        "poster_authority_sha256": "b" * 64,
        "serving_revision": _SmokeHandler.serving_revision,
        "image_digest": _SmokeHandler.image_digest,
        "counts": counts,
        "facets": facets,
    }
    result = module.HttpResult(200, {}, json.dumps(payload).encode())

    module._assert_config(
        result,
        expected_revision=_SmokeHandler.serving_revision,
        expected_image_digest=_SmokeHandler.image_digest,
        authorities=authorities,
    )

    payload["manifest_sha256"] = "d" * 64
    drifted = module.HttpResult(200, {}, json.dumps(payload).encode())
    with pytest.raises(module.SmokeFailure, match="release contract"):
        module._assert_config(
            drifted,
            expected_revision=_SmokeHandler.serving_revision,
            expected_image_digest=_SmokeHandler.image_digest,
            authorities=authorities,
        )


def _verified_r2_smoke_bundle(module: Any) -> object:
    from hk_movie_rag.rag_bundle import RagReleaseContract, RagReleaseCounts

    documents = (
        {
            "movie_id": "1978_ZQ_001",
            "document_id": "drunken-master-deep-analysis-v1",
            "source_filename": "S 級港⽚-醉拳.pdf",
            "source_sha256": (
                "ab27baf8e22ceb63a476b65dbfb02951a63a9e9bf39d96a47cd8e3f9fd314a75"
            ),
            "rights_status": "restricted",
            "quality_status": "manual_approved",
            "page_count": 3,
        },
        {
            "movie_id": "1982_ZJPD_001",
            "document_id": "aces-go-places-deep-analysis-v1",
            "source_filename": "S 級港⽚-最佳拍檔.pdf",
            "source_sha256": (
                "69fa1ad28916c043689ef4fe826cde816c05a3b254e5a0372d1a57ca2ce63bce"
            ),
            "rights_status": "restricted",
            "quality_status": "manual_approved",
            "page_count": 3,
        },
        {
            "movie_id": "1987_FGBR_001",
            "document_id": "its-a-mad-mad-mad-world-deep-analysis-v1",
            "source_filename": "S級港片-富貴逼人.pdf",
            "source_sha256": (
                "5aef7f8684bc0d91d77ffb6d7f89cb323358e2804ef8eaacbc0a02cfeb3d3df4"
            ),
            "rights_status": "restricted",
            "quality_status": "manual_approved",
            "page_count": 6,
        },
    )
    movie_ids = (
        "1978_ZQ_001",
        "1982_ZJPD_001",
        "1987_FGBR_001",
        "1970_MTXD_001",
        *(f"R2_FIXTURE_{index:04d}" for index in range(4654)),
    )
    unavailable_ids = {"1970_MTXD_001", *movie_ids[-112:]}
    records: list[dict[str, object]] = []
    for index, movie_id in enumerate(movie_ids):
        metadata_body = f"movie_id: {movie_id}"
        records.extend(
            (
                {
                    "record_kind": "facet",
                    "movie_id": movie_id,
                    "pilot_movie": index < 24,
                },
                {
                    "record_kind": "passage",
                    "passage_kind": "metadata",
                    "passage_id": f"metadata:{movie_id}",
                    "movie_id": movie_id,
                    "document_id": "",
                    "page_number": 0,
                    "body": metadata_body,
                    "content_sha256": hashlib.sha256(
                        metadata_body.encode("utf-8")
                    ).hexdigest(),
                },
                {
                    "record_kind": "poster",
                    "movie_id": movie_id,
                    "is_primary": True,
                    "quality_status": (
                        "missing" if movie_id in unavailable_ids else "machine_passed"
                    ),
                    "rights_status": "unknown",
                    **(
                        {}
                        if movie_id in unavailable_ids
                        else {
                            "derived_content_sha256": hashlib.sha256(
                                movie_id.encode("utf-8")
                            ).hexdigest(),
                            "derived_byte_length": 12,
                            "derived_mime_type": "image/webp",
                        }
                    ),
                },
            )
        )
    for document in documents:
        records.append({"record_kind": "document", **document})
        for page_number in range(1, int(document["page_count"]) + 1):
            body = f"{document['document_id']} page {page_number}"
            records.append(
                {
                    "record_kind": "passage",
                    "passage_kind": "pdf",
                    "passage_id": (
                        f"pdf:{document['document_id']}:p{page_number}"
                    ),
                    "movie_id": document["movie_id"],
                    "document_id": document["document_id"],
                    "page_number": page_number,
                    "body": body,
                    "content_sha256": hashlib.sha256(body.encode("utf-8")).hexdigest(),
                }
            )

    contract = RagReleaseContract(
        schema_version="1.2",
        rag_release_id="v1.2-demo-r2",
        parent_release_manifest_sha256=(
            "e9842db33208e7146d78d86689b13f81fffcf41288d79d84e599b93050445e2c"
        ),
        manifest_sha256="c" * 64,
        bundle_sha256="d" * 64,
        derived_inventory_sha256=(
            "b754727129bc044b6c9da1e6db22067c8279c5e7f8dd41d0350e365376538154"
        ),
        counts=RagReleaseCounts(
            movies=4658,
            facet_count=4658,
            tier_s_count=50,
            tier_a_count=313,
            tier_b_count=4295,
            pilot_count=24,
            poster_rows=4658,
            primary_poster_rows=4658,
            approved_poster_objects=4545,
            unavailable_poster_rows=113,
            derived_poster_bytes=54_540,
            metadata_passages=4658,
            documents=3,
            pdf_passages=12,
        ),
        embedding_model="gemini-embedding-2",
        embedding_dimension=768,
        generation_model="gemini-3.5-flash-lite",
        text_extraction_profile="cjk-layout-v1",
        document_embedding_profile="vertex-title-text-v1",
        relevance_policy_sha256=(
            "a4ecce727a469689a2a08c6d42e0d7d1eb975761173e3b6e4d1eaf34db7b2aba"
        ),
        poster_authority_sha256=(
            "9e56adda2a1fdfa648869c53d7e60a085db5d1c6d11db90dbec13bfe12f0eeed"
        ),
        access_mode="restricted_demo",
    )

    class Bundle:
        def __init__(self) -> None:
            self.contract = contract
            self._records = records

        def iter_records(self) -> Iterator[dict[str, object]]:
            return iter(self._records)

    return Bundle()


def _verified_r3_smoke_bundle(module: Any) -> object:
    from dataclasses import replace

    bundle = _verified_r2_smoke_bundle(module)
    new_documents = (
        {
            "movie_id": "1985_JSXS_001",
            "document_id": "mr-vampire-deep-analysis-v1",
            "source_filename": "S 級港⽚-殭屍先生.pdf",
            "source_sha256": (
                "2dce5ec8a1459999c53e9a56f603225e3970061cc7d2f7a35b8d53388096e576"
            ),
            "rights_status": "restricted",
            "quality_status": "manual_approved",
            "page_count": 4,
        },
        {
            "movie_id": "2001_SLZQ_001",
            "document_id": "shaolin-soccer-deep-analysis-v1",
            "source_filename": "S 級港⽚-少林足球.pdf",
            "source_sha256": (
                "c6f713e2f5c60381d7b8745829f89680ebcfcb631c24d86814a9c6dfcd49e863"
            ),
            "rights_status": "restricted",
            "quality_status": "manual_approved",
            "page_count": 5,
        },
    )
    metadata_body = "movie_id: 2001_SLZQ_001\nchinese_title: 少林足球"
    bundle._records.extend(
        (
            {
                "record_kind": "facet",
                "movie_id": "2001_SLZQ_001",
                "pilot_movie": False,
            },
            {
                "record_kind": "passage",
                "passage_kind": "metadata",
                "passage_id": "metadata:2001_SLZQ_001",
                "movie_id": "2001_SLZQ_001",
                "document_id": "",
                "page_number": 0,
                "body": metadata_body,
                "content_sha256": hashlib.sha256(metadata_body.encode()).hexdigest(),
            },
            {
                "record_kind": "poster",
                "movie_id": "2001_SLZQ_001",
                "is_primary": True,
                "quality_status": "manual_approved",
                "rights_status": "restricted",
                "derived_content_sha256": "9" * 64,
                "derived_byte_length": 162212,
                "derived_mime_type": "image/webp",
            },
        )
    )
    for document in new_documents:
        bundle._records.append({"record_kind": "document", **document})
        for page_number in range(1, int(document["page_count"]) + 1):
            body = f"{document['document_id']} page {page_number}"
            bundle._records.append(
                {
                    "record_kind": "passage",
                    "passage_kind": "pdf",
                    "passage_id": f"pdf:{document['document_id']}:p{page_number}",
                    "movie_id": document["movie_id"],
                    "document_id": document["document_id"],
                    "page_number": page_number,
                    "body": body,
                    "content_sha256": hashlib.sha256(body.encode()).hexdigest(),
                }
            )
    bundle.contract = replace(
        bundle.contract,
        schema_version="1.3",
        rag_release_id="v1.2-demo-r3",
        derived_inventory_sha256=(
            "3e3cedee507c3017821de1081bc70bbb01d0c73f8683ab484a464d1fc1f7c1db"
        ),
        counts=replace(
            bundle.contract.counts,
            movies=4659,
            facet_count=4659,
            tier_s_count=51,
            poster_rows=4659,
            primary_poster_rows=4659,
            approved_poster_objects=4546,
            metadata_passages=4659,
            documents=5,
            pdf_passages=21,
        ),
        relevance_policy_sha256=(
            "d6db5e118aeea5dc0484f3c591baac666952f78f4d41bfdcdbbb58693f4f6bba"
        ),
        poster_authority_sha256=(
            "c50ff06a88f91296b63bd74a1e4a6485785bdc4c07e73d09b407a126927c8137"
        ),
    )
    return bundle


def test_live_smoke_accepts_only_the_exact_r2_contract_and_document_authorities(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Breaks if a valid-but-different child or PDF binding can reach promotion."""
    module_spec = importlib.util.spec_from_file_location("exact_r2_live_smoke", SMOKE)
    assert module_spec is not None and module_spec.loader is not None
    module = importlib.util.module_from_spec(module_spec)
    sys.modules[module_spec.name] = module
    module_spec.loader.exec_module(module)
    bundle = _verified_r2_smoke_bundle(module)
    monkeypatch.setattr(module, "verify_rag_bundle", lambda _path: bundle)
    policy_calls: list[tuple[str, str]] = []
    poster_calls: list[tuple[str, str, object]] = []
    real_policy_loader = module.load_general_relevance_policy
    real_poster_loader = module.load_poster_serving_authority

    def policy_loader(release_id: str, digest: str) -> object:
        policy_calls.append((release_id, digest))
        return real_policy_loader(release_id, digest)

    def poster_loader(
        release_id: str, digest: str, *, contract: object
    ) -> object:
        poster_calls.append((release_id, digest, contract))
        return real_poster_loader(release_id, digest, contract=contract)

    monkeypatch.setattr(module, "load_general_relevance_policy", policy_loader)
    monkeypatch.setattr(module, "load_poster_serving_authority", poster_loader)

    authorities = module._load_smoke_authorities(Path("verified-r2.json"))

    assert authorities.release_id == "v1.2-demo-r2"
    assert authorities.counts == {
        "movies": 4658,
        "media_assets": 4658,
        "metadata_passages": 4658,
        "movie_documents": 3,
        "pdf_passages": 12,
        "embeddings": 4670,
    }
    assert authorities.citation_authorities[
        "pdf:its-a-mad-mad-mad-world-deep-analysis-v1:p5"
    ] == module._CitationAuthority(
        "1987_FGBR_001", "pdf_page", 5, "S級港片-富貴逼人.pdf"
    )
    assert policy_calls == [
        (
            "v1.2-demo-r2",
            "a4ecce727a469689a2a08c6d42e0d7d1eb975761173e3b6e4d1eaf34db7b2aba",
        )
    ]
    assert poster_calls == [
        (
            "v1.2-demo-r2",
            "9e56adda2a1fdfa648869c53d7e60a085db5d1c6d11db90dbec13bfe12f0eeed",
            bundle.contract,
        )
    ]
    fgbr_cases = {
        str(case["case_id"]): case
        for case in authorities.deep_cases
        if case["case_id"] in {"fgbr-visual-p1", "fgbr-sound-followup-p5"}
    }
    assert fgbr_cases["fgbr-visual-p1"]["required_citation_ids"] == [
        "pdf:its-a-mad-mad-mad-world-deep-analysis-v1:p1"
    ]
    assert fgbr_cases["fgbr-sound-followup-p5"]["history"] == [
        {
            "answer": "《富貴逼人》的視覺與空間分析集中在第一頁。",
            "movie_ids": ["1987_FGBR_001"],
            "question": "《富貴逼人》的視覺風格與空間設計？",
        }
    ]
    assert fgbr_cases["fgbr-sound-followup-p5"]["required_citation_ids"] == [
        "pdf:its-a-mad-mad-mad-world-deep-analysis-v1:p5"
    ]

    bundle.contract = replace(bundle.contract, schema_version="1.1")
    with pytest.raises(module.SmokeFailure, match="verified release authorities"):
        module._load_smoke_authorities(Path("wrong-schema.json"))

    bundle.contract = replace(bundle.contract, schema_version="1.2")
    fgbr_document = next(
        record
        for record in bundle._records
        if record.get("record_kind") == "document"
        and record.get("movie_id") == "1987_FGBR_001"
    )
    fgbr_document["source_filename"] = "substituted-secret-document.pdf"
    with pytest.raises(module.SmokeFailure, match="verified release authorities") as exc:
        module._load_smoke_authorities(Path("wrong-document.json"))
    assert "substituted-secret-document" not in str(exc.value)


def test_live_smoke_accepts_exact_r3_authorities_and_both_new_document_pages(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Breaks if deployment smoke remains R2-only or omits either newly governed PDF."""
    module_spec = importlib.util.spec_from_file_location("exact_r3_live_smoke", SMOKE)
    assert module_spec is not None and module_spec.loader is not None
    module = importlib.util.module_from_spec(module_spec)
    sys.modules[module_spec.name] = module
    module_spec.loader.exec_module(module)
    bundle = _verified_r3_smoke_bundle(module)
    monkeypatch.setattr(module, "verify_rag_bundle", lambda _path: bundle)

    authorities = module._load_smoke_authorities(Path("verified-r3.json"))

    assert authorities.release_id == "v1.2-demo-r3"
    assert authorities.counts == {
        "movies": 4659,
        "media_assets": 4659,
        "metadata_passages": 4659,
        "movie_documents": 5,
        "pdf_passages": 21,
        "embeddings": 4680,
    }
    assert len(authorities.deep_cases) == 10
    assert authorities.citation_authorities[
        "pdf:mr-vampire-deep-analysis-v1:p4"
    ].movie_id == "1985_JSXS_001"
    assert authorities.citation_authorities[
        "pdf:shaolin-soccer-deep-analysis-v1:p5"
    ].movie_id == "2001_SLZQ_001"
    assert authorities.poster_expectations["2001_SLZQ_001"].available is True


@pytest.mark.parametrize(
    ("expected_revision", "expected_image_digest"),
    (
        ("hk-movie-rag-demo-00009-old", _SmokeHandler.image_digest),
        (_SmokeHandler.serving_revision, f"sha256:{'2' * 64}"),
    ),
)
def test_live_smoke_rejects_a_stale_revision_or_image_identity(
    expected_revision: str, expected_image_digest: str
) -> None:
    """Breaks if a stable service URL can accidentally validate an older deployment."""
    module_spec = importlib.util.spec_from_file_location("identity_live_smoke", SMOKE)
    assert module_spec is not None and module_spec.loader is not None
    module = importlib.util.module_from_spec(module_spec)
    sys.modules[module_spec.name] = module
    module_spec.loader.exec_module(module)
    payload = {
        "access_mode": "restricted_demo",
        "rag_release_id": "v1.2-demo",
        "status": "active",
        "embedding_model": "gemini-embedding-2",
        "embedding_dimension": 768,
        "generation_model": "gemini-3.5-flash-lite",
        "relevance_policy_sha256": (
            "5242afc5e5c91b74dd012b558ed5eb929f4acf78e98defe6d14bf4f7cce1a8c2"
        ),
        "poster_authority_sha256": (
            "ca99a5c04d8e61029f58e90cb3017730dc2b3511361834d7a0cf4860b54005e4"
        ),
        "serving_revision": _SmokeHandler.serving_revision,
        "image_digest": _SmokeHandler.image_digest,
        "counts": {
            "movies": 4658,
            "media_assets": 4658,
            "metadata_passages": 4658,
            "movie_documents": 2,
            "pdf_passages": 6,
            "embeddings": 4664,
        },
        "facets": {
            "total": 4658,
            "tier_s": 50,
            "tier_a": 313,
            "tier_b": 4295,
            "pilot_movies": 24,
        },
    }
    result = module.HttpResult(200, {}, json.dumps(payload).encode("utf-8"))

    with pytest.raises(module.SmokeFailure, match="release contract"):
        module._assert_config(
            result,
            expected_revision=expected_revision,
            expected_image_digest=expected_image_digest,
        )


def test_live_smoke_loads_only_the_pinned_20_movie_and_20_ood_golden_cases(
    tmp_path: Path,
) -> None:
    """Breaks if the live evaluator can run a changed file or a different case identity set."""
    module_spec = importlib.util.spec_from_file_location("golden_live_smoke", SMOKE)
    assert module_spec is not None and module_spec.loader is not None
    module = importlib.util.module_from_spec(module_spec)
    sys.modules[module_spec.name] = module
    module_spec.loader.exec_module(module)

    cases = module._load_general_relevance_golden()

    assert tuple(case["case_id"] for case in cases if case["expected_domain"] == "movie") == (
        "cal-movie-01",
        "cal-movie-02",
        "cal-movie-03",
        "cal-movie-04",
        "cal-movie-05",
        "cal-movie-06",
        "cal-movie-07",
        "cal-movie-08",
        "cal-movie-09",
        "cal-movie-10",
        "cal-movie-11",
        "cal-movie-12",
        "cal-movie-13",
        "cal-movie-14",
        "cal-movie-15",
        "hold-movie-01",
        "hold-movie-02",
        "hold-movie-03",
        "hold-movie-04",
        "hold-movie-05",
    )
    assert tuple(case["case_id"] for case in cases if case["expected_domain"] == "ood") == (
        "cal-ood-01",
        "cal-ood-02",
        "cal-ood-03",
        "cal-ood-04",
        "cal-ood-05",
        "cal-ood-06",
        "cal-ood-07",
        "cal-ood-08",
        "cal-ood-09",
        "cal-ood-10",
        "cal-ood-11",
        "cal-ood-12",
        "cal-ood-13",
        "cal-ood-14",
        "cal-ood-15",
        "hold-ood-01",
        "hold-ood-02",
        "hold-ood-03",
        "hold-ood-04",
        "hold-ood-05",
    )
    assert frozenset(module._CONTROLLED_INSUFFICIENT_MOVIE_CASE_IDS) == (
        GENERAL_RELEVANCE_CONTROLLED_INSUFFICIENT_CASE_IDS
    )
    assert frozenset(module._BOUNDED_PDF_MOVIE_CASE_IDS) == (
        GENERAL_RELEVANCE_BOUNDED_PDF_CASE_IDS
    )
    assert tuple(module._MOVIE_CASE_OUTCOMES.values()).count("structured") == 11
    assert tuple(module._MOVIE_CASE_OUTCOMES.values()).count("bounded_pdf") == 5
    assert (
        tuple(module._MOVIE_CASE_OUTCOMES.values()).count("controlled_insufficient")
        == 4
    )
    assert set(module._MOVIE_CASE_CONTRACTS).isdisjoint(
        module._CONTROLLED_INSUFFICIENT_MOVIE_CASE_IDS
    )
    assert set(module._MOVIE_CASE_CONTRACTS) | set(
        module._CONTROLLED_INSUFFICIENT_MOVIE_CASE_IDS
    ) == {
        case["case_id"] for case in cases if case["expected_domain"] == "movie"
    }
    tampered = tmp_path / "general_relevance_golden.jsonl"
    tampered.write_bytes(GENERAL_RELEVANCE_GOLDEN.read_bytes() + b"\n")
    with pytest.raises(module.SmokeFailure, match="golden artifact"):
        module._load_general_relevance_golden(tampered)


def test_live_smoke_bounded_pdf_outcomes_require_exact_pdf_authority() -> None:
    """Breaks if a bounded case can silently lose its exact movie/passage contract."""
    module_spec = importlib.util.spec_from_file_location("bounded_contract_smoke", SMOKE)
    assert module_spec is not None and module_spec.loader is not None
    module = importlib.util.module_from_spec(module_spec)
    sys.modules[module_spec.name] = module
    module_spec.loader.exec_module(module)
    original = module._MOVIE_CASE_CONTRACTS["cal-movie-01"]
    module._MOVIE_CASE_CONTRACTS["cal-movie-01"] = module._MovieCaseContract(
        answer_term_groups=original.answer_term_groups,
        expected_movie_ids=original.expected_movie_ids,
        expected_passage_ids=(),
        expected_source_kind=None,
        expected_movie_count=original.expected_movie_count,
    )

    with pytest.raises(module.SmokeFailure, match="golden artifact"):
        module._load_general_relevance_golden()


def test_live_smoke_bounded_disclosure_does_not_require_an_obsolete_document_count() -> None:
    """Breaks if adding a governed document makes the unchanged 40-case gate impossible."""
    module_spec = importlib.util.spec_from_file_location("bounded_disclosure_smoke", SMOKE)
    assert module_spec is not None and module_spec.loader is not None
    module = importlib.util.module_from_spec(module_spec)
    sys.modules[module_spec.name] = module
    module_spec.loader.exec_module(module)
    answer = {
        "answer_markdown": (
            "基於本次答案選取的深度文檔，以下只比較《醉拳》和《最佳拍檔》的"
            "動作喜劇特色，不能外推為所有香港動作喜劇的共同特徵。"
        ),
        "citations": [
            {
                "citation_id": "pdf:drunken-master-deep-analysis-v1:p1",
                "movie_id": "1978_ZQ_001",
                "source_kind": "pdf_page",
            },
            {
                "citation_id": "pdf:aces-go-places-deep-analysis-v1:p1",
                "movie_id": "1982_ZJPD_001",
                "source_kind": "pdf_page",
            },
        ],
        "movies": [
            {"movie_id": "1978_ZQ_001"},
            {"movie_id": "1982_ZJPD_001"},
        ],
    }

    module._assert_general_relevance_movie_case("cal-movie-01", answer)


def test_live_smoke_bounded_tempo_contract_accepts_the_production_scope_note() -> None:
    """Breaks if the smoke oracle drifts from the deterministic bounded planner."""
    from hk_movie_rag.retrieval import plan_bounded_analysis

    module_spec = importlib.util.spec_from_file_location("tempo_scope_live_smoke", SMOKE)
    assert module_spec is not None and module_spec.loader is not None
    module = importlib.util.module_from_spec(module_spec)
    sys.modules[module_spec.name] = module
    module_spec.loader.exec_module(module)
    plan = plan_bounded_analysis("想看節奏明快的香港動作片")
    assert plan is not None
    answer = {
        "answer_markdown": f"{plan.scope_note}\n{plan.answer_summary}",
        "citations": [
            {
                "citation_id": plan.passage_ids[0],
                "movie_id": plan.movie_ids[0],
                "source_kind": "pdf_page",
            }
        ],
        "movies": [{"movie_id": plan.movie_ids[0], "genre": "動作"}],
    }

    module._assert_general_relevance_movie_case("cal-movie-08", answer)


def test_live_smoke_runs_governed_deep_cases_with_typed_history(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Breaks if the follow-up case is hard-coded or loses its governed history shape."""
    module_spec = importlib.util.spec_from_file_location("deep_cases_live_smoke", SMOKE)
    assert module_spec is not None and module_spec.loader is not None
    module = importlib.util.module_from_spec(module_spec)
    sys.modules[module_spec.name] = module
    module_spec.loader.exec_module(module)
    citation_id = "pdf:its-a-mad-mad-mad-world-deep-analysis-v1:p5"
    history = [
        {
            "question": "《富貴逼人》的視覺風格與空間設計？",
            "answer": "受控前文",
            "movie_ids": ["1987_FGBR_001"],
        }
    ]
    case = {
        "case_id": "fgbr-sound-followup-p5",
        "expected_movie_ids": ["1987_FGBR_001"],
        "forbidden_answer_terms": [],
        "forbidden_source_kinds": ["movie_metadata"],
        "history": history,
        "question": "那它的聲音設計呢？",
        "required_answer_terms": [],
        "required_any_answer_terms": ["聲音"],
        "required_citation_ids": [citation_id],
        "requires_limited_evidence_disclosure": False,
    }
    authorities = module._SmokeAuthorities(
        release_id="v1.2-demo-r2",
        manifest_sha256="c" * 64,
        access_mode="restricted_demo",
        embedding_model="gemini-embedding-2",
        embedding_dimension=768,
        generation_model="gemini-3.5-flash-lite",
        relevance_policy_sha256="a" * 64,
        poster_authority_sha256="b" * 64,
        counts={},
        facets={},
        citation_authorities={},
        poster_expectations={},
        pilot_movie_ids=frozenset(),
        deep_cases=(case,),
    )
    seen: list[object] = []

    def fake_chat(*_args: object, **kwargs: object) -> dict[str, object]:
        seen.append(kwargs.get("history"))
        return {
            "answer_markdown": f"這段聲音設計分析。[{citation_id}]",
            "citations": [{"citation_id": citation_id, "source_kind": "pdf_page"}],
            "movies": [{"movie_id": "1987_FGBR_001"}],
        }

    monkeypatch.setattr(module, "_chat", fake_chat)

    assert (
        module._run_deep_answer_gate(
            object(), "https://example.test", 5, "rag_demo_session=x", authorities
        )
        == 1
    )
    assert seen == [history]


def test_live_smoke_accepts_the_verified_release_deep_case_count() -> None:
    """Breaks if an R2 case-count constant rejects a larger immutable R3 authority."""
    module_spec = importlib.util.spec_from_file_location("deep_count_live_smoke", SMOKE)
    assert module_spec is not None and module_spec.loader is not None
    module = importlib.util.module_from_spec(module_spec)
    sys.modules[module_spec.name] = module
    module_spec.loader.exec_module(module)
    cases = tuple({"case_id": f"case-{index}"} for index in range(10))

    module._assert_completed_deep_case_count(10, cases)
    with pytest.raises(module.SmokeFailure, match="deep answer case authority"):
        module._assert_completed_deep_case_count(6, cases)


def _release_title_deep_answer() -> dict[str, object]:
    citation_id = "pdf:its-a-mad-mad-mad-world-deep-analysis-v1:p3"
    return {
        "answer_markdown": (
            "《富貴逼人》的喜劇機制來自家庭成員的階層焦慮、互相算計，"
            f"以及相聲式對白節奏。[{citation_id}]"
        ),
        "citations": [
            {
                "citation_id": citation_id,
                "movie_id": "1987_FGBR_001",
                "source_kind": "pdf_page",
            }
        ],
        "movies": [{"movie_id": "1987_FGBR_001"}],
    }


def test_live_smoke_runs_unbracketed_release_title_deep_query(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Breaks if deployment can pass without exercising the reported query shape."""
    module_spec = importlib.util.spec_from_file_location("title_route_live_smoke", SMOKE)
    assert module_spec is not None and module_spec.loader is not None
    module = importlib.util.module_from_spec(module_spec)
    sys.modules[module_spec.name] = module
    module_spec.loader.exec_module(module)
    seen: list[tuple[object, object]] = []

    def fake_chat(*args: object, **kwargs: object) -> dict[str, object]:
        seen.append((args[2], kwargs.get("history")))
        return _release_title_deep_answer()

    monkeypatch.setattr(module, "_chat", fake_chat)

    assert module._run_release_title_deep_query_gate(
        object(), "https://example.test", 5, "rag_demo_session=x", object()
    )
    assert seen == [("讲一下富贵逼人的喜剧创作", [])]


@pytest.mark.parametrize(
    "invalid_case",
    (
        "domain_refusal",
        "wrong_movie",
        "metadata_only",
        "missing_page_three",
        "missing_semantics",
        "genre_only",
        "extra_movie_card",
    ),
)
def test_live_smoke_rejects_invalid_release_title_deep_answers(
    invalid_case: str,
) -> None:
    module_spec = importlib.util.spec_from_file_location(
        f"title_route_{invalid_case}_live_smoke", SMOKE
    )
    assert module_spec is not None and module_spec.loader is not None
    module = importlib.util.module_from_spec(module_spec)
    sys.modules[module_spec.name] = module
    module_spec.loader.exec_module(module)
    answer = _release_title_deep_answer()
    citations = answer["citations"]
    movies = answer["movies"]
    assert isinstance(citations, list)
    assert isinstance(movies, list)
    if invalid_case == "domain_refusal":
        answer["answer_markdown"] = "我目前只能回答这个 Release 内的香港电影问题。"
        citations.clear()
        movies.clear()
    elif invalid_case == "wrong_movie":
        assert isinstance(citations[0], dict)
        citations[0]["movie_id"] = "1988_FGZBR_001"
        movies[0] = {"movie_id": "1988_FGZBR_001"}
    elif invalid_case == "metadata_only":
        assert isinstance(citations[0], dict)
        citations[0].update(
            {
                "citation_id": "metadata:1987_FGBR_001",
                "source_kind": "movie_metadata",
            }
        )
    elif invalid_case == "missing_page_three":
        assert isinstance(citations[0], dict)
        citations[0]["citation_id"] = (
            "pdf:its-a-mad-mad-mad-world-deep-analysis-v1:p4"
        )
    elif invalid_case == "missing_semantics":
        answer["answer_markdown"] = (
            "《富貴逼人》是一部香港電影。"
            "[pdf:its-a-mad-mad-mad-world-deep-analysis-v1:p3]"
        )
    elif invalid_case == "genre_only":
        answer["answer_markdown"] = (
            "《富貴逼人》是一部家庭喜劇。"
            "[pdf:its-a-mad-mad-mad-world-deep-analysis-v1:p3]"
        )
    else:
        movies.append({"movie_id": "1988_FGZBR_001"})

    with pytest.raises(module.SmokeFailure, match="release title deep query"):
        module._assert_release_title_deep_query(answer)


def test_live_smoke_requires_semantic_limited_evidence_disclosure() -> None:
    """Breaks if mere source attribution can masquerade as an evidence-limit disclosure."""
    module_spec = importlib.util.spec_from_file_location("limited_evidence_smoke", SMOKE)
    assert module_spec is not None and module_spec.loader is not None
    module = importlib.util.module_from_spec(module_spec)
    sys.modules[module_spec.name] = module
    module_spec.loader.exec_module(module)
    case = {
        "case_id": "bounded-broad",
        "expected_movie_ids": ["A"],
        "forbidden_answer_terms": [],
        "forbidden_source_kinds": [],
        "history": [],
        "question": "這些電影有什麼共同特色？",
        "required_answer_terms": [],
        "required_any_answer_terms": ["提供的分析"],
        "required_citation_ids": ["metadata:A"],
        "requires_limited_evidence_disclosure": True,
    }
    answer = {
        "answer_markdown": "根據提供的分析，香港電影普遍都如此。[metadata:A]",
        "citations": [{"citation_id": "metadata:A", "source_kind": "movie_metadata"}],
        "movies": [{"movie_id": "A"}],
    }

    with pytest.raises(module.SmokeFailure, match="deep answer case contract"):
        module._assert_deep_answer_case(case, answer)

    answer["answer_markdown"] = (
        "根據提供的分析，製作預算有限，但香港電影普遍都如此。[metadata:A]"
    )
    with pytest.raises(module.SmokeFailure, match="deep answer case contract"):
        module._assert_deep_answer_case(case, answer)

    answer["answer_markdown"] = (
        "僅基於這份有限的、所提供的分析，不能外推至所有香港電影。[metadata:A]"
    )
    module._assert_deep_answer_case(case, answer)

    answer["answer_markdown"] = (
        "只基於所提供的分析，這個結論限於這些電影。[metadata:A]"
    )
    module._assert_deep_answer_case(case, answer)

    answer["answer_markdown"] = (
        "所提供的分析證據有限，這個結論限於這些電影。[metadata:A]"
    )
    module._assert_deep_answer_case(case, answer)


def test_live_smoke_wrong_code_probe_is_fixed_and_not_credential_derived() -> None:
    """Breaks if a low-entropy access code becomes a log-visible verifier."""

    module_spec = importlib.util.spec_from_file_location("wrong_code_live_smoke", SMOKE)
    assert module_spec is not None and module_spec.loader is not None
    module = importlib.util.module_from_spec(module_spec)
    sys.modules[module_spec.name] = module
    module_spec.loader.exec_module(module)
    first = "__invalid_rag_demo_access_code_probe_1__"
    second = "__invalid_rag_demo_access_code_probe_2__"

    assert module._deterministic_wrong_access_code("123456") == first
    assert module._deterministic_wrong_access_code(first) == second
    assert module._deterministic_wrong_access_code(second) == first


def test_live_smoke_rejects_one_self_consistent_wrong_movie_for_all_movie_cases(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Breaks if 20 live questions can all pass on one irrelevant grounded response."""
    module_spec = importlib.util.spec_from_file_location("movie_oracle_live_smoke", SMOKE)
    assert module_spec is not None and module_spec.loader is not None
    module = importlib.util.module_from_spec(module_spec)
    sys.modules[module_spec.name] = module
    module_spec.loader.exec_module(module)
    wrong_answer = {
        "answer_markdown": "《同一錯誤電影》：今天天氣很好。[metadata:WRONG_001]",
        "citations": [
            {
                "citation_id": "metadata:WRONG_001",
                "movie_id": "WRONG_001",
                "movie_title": "同一錯誤電影",
                "source_kind": "movie_metadata",
                "page_number": None,
                "source_filename": None,
                "excerpt": "自洽但答非所問",
            }
        ],
        "movies": [
            {
                "movie_id": "WRONG_001",
                "chinese_title": "同一錯誤電影",
                "release_date": "2000-01-01",
                "director": "錯誤導演",
                "cast": "錯誤演員",
                "genre": "劇情",
                "tier": "B",
                "poster_url": None,
            }
        ],
    }
    monkeypatch.setattr(module, "_chat", lambda *_args, **_kwargs: wrong_answer)
    monkeypatch.setattr(
        module,
        "_request",
        lambda *_args, **_kwargs: module.HttpResult(
            200,
            {"content-type": "application/json"},
            json.dumps(
                {
                    "answer_markdown": "我目前只能回答这个 Release 内的香港电影问题。",
                    "citations": [],
                    "movies": [],
                },
                ensure_ascii=False,
            ).encode("utf-8"),
        ),
    )

    with pytest.raises(module.SmokeFailure, match="movie case contract"):
        module._run_general_relevance_gate(
            urllib.request.build_opener(),
            "http://localhost",
            5,
            "rag_demo_session=fixture",
        )


@pytest.mark.parametrize(
    "case_id",
    tuple(
        case_id
        for case_id in GENERAL_RELEVANCE_MOVIE_FIXTURES
        if case_id
        not in (
            GENERAL_RELEVANCE_CONTROLLED_INSUFFICIENT_CASE_IDS
            | GENERAL_RELEVANCE_BOUNDED_PDF_CASE_IDS
        )
    ),
)
def test_live_smoke_each_movie_case_rejects_wrong_topic_and_card_metadata(
    case_id: str,
) -> None:
    """Breaks if any pinned case lacks both a topic and card-metadata oracle."""
    module_spec = importlib.util.spec_from_file_location(f"{case_id}_oracle_smoke", SMOKE)
    assert module_spec is not None and module_spec.loader is not None
    module = importlib.util.module_from_spec(module_spec)
    sys.modules[module_spec.name] = module
    module_spec.loader.exec_module(module)
    (
        movie_id,
        chinese_title,
        answer_text,
        release_date,
        director,
        cast,
        genre,
    ) = GENERAL_RELEVANCE_MOVIE_FIXTURES[case_id]
    valid_movie = {
        "movie_id": movie_id,
        "chinese_title": chinese_title,
        "release_date": release_date,
        "director": director,
        "cast": cast,
        "genre": genre,
    }
    valid_answer = {"answer_markdown": answer_text, "movies": [valid_movie]}

    module._assert_general_relevance_movie_case(case_id, valid_answer)

    with pytest.raises(module.SmokeFailure, match="movie case contract"):
        module._assert_general_relevance_movie_case(
            case_id,
            {"answer_markdown": "今天天氣很好", "movies": [valid_movie]},
        )

    wrong_movie = {
        **valid_movie,
        "chinese_title": "無關片名",
        "english_title": "Unrelated title",
        "release_date": "1800-01-01",
        "director": "錯誤導演",
        "cast": "錯誤演員",
        "genre": "科幻",
    }
    with pytest.raises(module.SmokeFailure, match="movie case contract"):
        module._assert_general_relevance_movie_case(
            case_id,
            {"answer_markdown": answer_text, "movies": [wrong_movie]},
        )


@pytest.mark.parametrize("release_date", ["1985", "1988-02-29"])
def test_live_smoke_accepts_real_year_only_or_calendar_release_dates(
    release_date: str,
) -> None:
    module_spec = importlib.util.spec_from_file_location("valid_date_live_smoke", SMOKE)
    assert module_spec is not None and module_spec.loader is not None
    module = importlib.util.module_from_spec(module_spec)
    sys.modules[module_spec.name] = module
    module_spec.loader.exec_module(module)
    module._assert_general_relevance_movie_case(
        "cal-movie-03",
        {
            "answer_markdown": "八十年代犯罪警匪代表",
            "movies": [{"release_date": release_date, "genre": "犯罪 / 動作"}],
        },
    )


@pytest.mark.parametrize(
    "release_date",
    ["1985-13-01", "1985-99-99", "1985-02-29", "1985-04-31"],
)
def test_live_smoke_rejects_impossible_calendar_release_dates(
    release_date: str,
) -> None:
    module_spec = importlib.util.spec_from_file_location("invalid_date_live_smoke", SMOKE)
    assert module_spec is not None and module_spec.loader is not None
    module = importlib.util.module_from_spec(module_spec)
    sys.modules[module_spec.name] = module
    module_spec.loader.exec_module(module)
    with pytest.raises(module.SmokeFailure, match="movie case contract"):
        module._assert_general_relevance_movie_case(
            "cal-movie-03",
            {
                "answer_markdown": "八十年代犯罪警匪代表",
                "movies": [{"release_date": release_date, "genre": "犯罪 / 動作"}],
            },
        )


def test_live_smoke_general_relevance_genre_requires_an_exact_split_token() -> None:
    module_spec = importlib.util.spec_from_file_location("exact_genre_live_smoke", SMOKE)
    assert module_spec is not None and module_spec.loader is not None
    module = importlib.util.module_from_spec(module_spec)
    sys.modules[module_spec.name] = module
    module_spec.loader.exec_module(module)
    with pytest.raises(module.SmokeFailure, match="movie case contract"):
        module._assert_general_relevance_movie_case(
            "cal-movie-07",
            {
                "answer_markdown": "武打功夫與喜劇",
                "movies": [{"release_date": "1994", "genre": "非喜劇 / 動作"}],
            },
        )


def _pdf_answer(
    *,
    citation_id: str,
    movie_id: str,
    movie_title: str,
    page_number: int,
    source_filename: str,
) -> dict[str, object]:
    return {
        "answer_markdown": f"受控深度答案。[{citation_id}]",
        "citations": [
            {
                "citation_id": citation_id,
                "movie_id": movie_id,
                "movie_title": movie_title,
                "source_kind": "pdf_page",
                "page_number": page_number,
                "source_filename": source_filename,
                "excerpt": "受控摘錄",
            }
        ],
        "movies": [{"movie_id": movie_id, "chinese_title": movie_title}],
    }


@pytest.mark.parametrize(
    ("citation_id", "movie_id", "movie_title", "page_number", "source_filename"),
    [
        (
            "pdf:drunken-master-deep-analysis-v1:p1",
            "1978_ZQ_001",
            "醉拳",
            1,
            "S 級港⽚-醉拳.pdf",
        ),
        (
            "pdf:aces-go-places-deep-analysis-v1:p2",
            "1982_ZJPD_001",
            "最佳拍檔",
            2,
            "S 級港⽚-最佳拍檔.pdf",
        ),
    ],
)
def test_live_smoke_accepts_only_known_exact_pdf_bindings(
    citation_id: str,
    movie_id: str,
    movie_title: str,
    page_number: int,
    source_filename: str,
) -> None:
    module_spec = importlib.util.spec_from_file_location("valid_pdf_live_smoke", SMOKE)
    assert module_spec is not None and module_spec.loader is not None
    module = importlib.util.module_from_spec(module_spec)
    sys.modules[module_spec.name] = module
    module_spec.loader.exec_module(module)
    module._assert_ordered_citation_card_equality(
        _pdf_answer(
            citation_id=citation_id,
            movie_id=movie_id,
            movie_title=movie_title,
            page_number=page_number,
            source_filename=source_filename,
        )
    )


def test_live_smoke_accepts_any_positive_page_only_when_verified_bundle_contains_it() -> None:
    """Breaks if syntax alone authorizes a page or if p5 is still rejected statically."""
    module_spec = importlib.util.spec_from_file_location("dynamic_pdf_live_smoke", SMOKE)
    assert module_spec is not None and module_spec.loader is not None
    module = importlib.util.module_from_spec(module_spec)
    sys.modules[module_spec.name] = module
    module_spec.loader.exec_module(module)
    citation_id = "pdf:its-a-mad-mad-mad-world-deep-analysis-v1:p5"
    authorities = {
        citation_id: module._CitationAuthority(
            "1987_FGBR_001", "pdf_page", 5, "S 級港片-富貴逼人.pdf"
        )
    }
    answer = _pdf_answer(
        citation_id=citation_id,
        movie_id="1987_FGBR_001",
        movie_title="富貴逼人",
        page_number=5,
        source_filename="S 級港片-富貴逼人.pdf",
    )

    module._assert_ordered_citation_card_equality(
        answer, citation_authorities=authorities
    )

    missing_page = _pdf_answer(
        citation_id="pdf:its-a-mad-mad-mad-world-deep-analysis-v1:p6",
        movie_id="1987_FGBR_001",
        movie_title="富貴逼人",
        page_number=6,
        source_filename="S 級港片-富貴逼人.pdf",
    )
    with pytest.raises(module.SmokeFailure, match="grounded answer contract"):
        module._assert_ordered_citation_card_equality(
            missing_page, citation_authorities=authorities
        )


def test_live_smoke_derives_document_page_and_poster_authorities_from_bundle() -> None:
    """Breaks if smoke authority silently returns to a static document/poster table."""
    module_spec = importlib.util.spec_from_file_location("bundle_authority_live_smoke", SMOKE)
    assert module_spec is not None and module_spec.loader is not None
    module = importlib.util.module_from_spec(module_spec)
    sys.modules[module_spec.name] = module
    module_spec.loader.exec_module(module)
    document_id = "its-a-mad-mad-mad-world-deep-analysis-v1"
    poster_sha256 = "a" * 64
    records = (
        {
            "record_kind": "document",
            "document_id": document_id,
            "movie_id": "1987_FGBR_001",
            "source_filename": "S 級港片-富貴逼人.pdf",
        },
        {
            "record_kind": "passage",
            "passage_kind": "metadata",
            "passage_id": "metadata:1987_FGBR_001",
            "movie_id": "1987_FGBR_001",
        },
        {
            "record_kind": "passage",
            "passage_kind": "pdf",
            "passage_id": f"pdf:{document_id}:p5",
            "movie_id": "1987_FGBR_001",
            "document_id": document_id,
            "page_number": 5,
        },
        {
            "record_kind": "poster",
            "movie_id": "1987_FGBR_001",
            "is_primary": True,
            "quality_status": "machine_passed",
            "rights_status": "unknown",
            "derived_content_sha256": poster_sha256,
            "derived_byte_length": 123,
            "derived_mime_type": "image/webp",
        },
        {
            "record_kind": "facet",
            "movie_id": "1987_FGBR_001",
            "pilot_movie": False,
        },
    )

    class Bundle:
        contract = type("Contract", (), {"counts": type("Counts", (), {"facet_count": 1})()})()

        @staticmethod
        def iter_records() -> Iterator[dict[str, object]]:
            return iter(records)

    citations, posters, pilots = module._derive_bundle_authorities(Bundle())

    assert citations[f"pdf:{document_id}:p5"] == module._CitationAuthority(
        "1987_FGBR_001", "pdf_page", 5, "S 級港片-富貴逼人.pdf"
    )
    assert citations["metadata:1987_FGBR_001"] == module._CitationAuthority(
        "1987_FGBR_001", "movie_metadata", None, None
    )
    assert posters["1987_FGBR_001"] == module._PosterExpectation(
        True, poster_sha256, 123, "image/webp", "unknown"
    )
    assert pilots == set()


@pytest.mark.parametrize(
    ("citation_id", "movie_id", "movie_title", "page_number", "source_filename"),
    [
        (
            "pdf:drunken-master-deep-analysis-v1:p1evil",
            "1978_ZQ_001",
            "醉拳",
            1,
            "S 級港⽚-醉拳.pdf",
        ),
        (
            "pdf:drunken-master-deep-analysis-v1:p2",
            "1978_ZQ_001",
            "醉拳",
            1,
            "S 級港⽚-醉拳.pdf",
        ),
        (
            "pdf:drunken-master-deep-analysis-v1:p4",
            "1978_ZQ_001",
            "醉拳",
            4,
            "S 級港⽚-醉拳.pdf",
        ),
        (
            "pdf:drunken-master-deep-analysis-v1:p1",
            "1982_ZJPD_001",
            "最佳拍檔",
            1,
            "S 級港⽚-醉拳.pdf",
        ),
        (
            "pdf:drunken-master-deep-analysis-v1:p1",
            "1978_ZQ_001",
            "醉拳",
            1,
            "analysis.pdf",
        ),
        (
            "pdf:unknown-deep-analysis-v1:p1",
            "1978_ZQ_001",
            "醉拳",
            1,
            "S 級港⽚-醉拳.pdf",
        ),
    ],
)
def test_live_smoke_rejects_pdf_binding_drift(
    citation_id: str,
    movie_id: str,
    movie_title: str,
    page_number: int,
    source_filename: str,
) -> None:
    module_spec = importlib.util.spec_from_file_location("invalid_pdf_live_smoke", SMOKE)
    assert module_spec is not None and module_spec.loader is not None
    module = importlib.util.module_from_spec(module_spec)
    sys.modules[module_spec.name] = module
    module_spec.loader.exec_module(module)
    with pytest.raises(module.SmokeFailure, match="grounded answer contract"):
        module._assert_ordered_citation_card_equality(
            _pdf_answer(
                citation_id=citation_id,
                movie_id=movie_id,
                movie_title=movie_title,
                page_number=page_number,
                source_filename=source_filename,
            )
        )


def _metadata_answer(movie_id: str) -> dict[str, object]:
    citation_id = f"metadata:{movie_id}"
    return {
        "answer_markdown": f"受控答案。[{citation_id}]",
        "citations": [
            {
                "citation_id": citation_id,
                "movie_id": movie_id,
                "movie_title": "受控電影",
                "source_kind": "movie_metadata",
                "page_number": None,
                "source_filename": None,
                "excerpt": "受控摘錄",
            }
        ],
        "movies": [{"movie_id": movie_id, "chinese_title": "受控電影"}],
    }


@pytest.mark.parametrize("movie_id", ["A", "A" + "_" * 127])
def test_live_smoke_accepts_backend_authorized_movie_id_boundaries(
    movie_id: str,
) -> None:
    module_spec = importlib.util.spec_from_file_location("valid_movie_id_smoke", SMOKE)
    assert module_spec is not None and module_spec.loader is not None
    module = importlib.util.module_from_spec(module_spec)
    sys.modules[module_spec.name] = module
    module_spec.loader.exec_module(module)
    module._assert_ordered_citation_card_equality(_metadata_answer(movie_id))


@pytest.mark.parametrize(
    "movie_id",
    ["", "_A", "-A", "A/B", "A.B", "A%2FB", "A B", "A\nB", "電影A", "A" * 129],
)
def test_live_smoke_rejects_movie_ids_outside_backend_authority(
    movie_id: str,
) -> None:
    module_spec = importlib.util.spec_from_file_location("invalid_movie_id_smoke", SMOKE)
    assert module_spec is not None and module_spec.loader is not None
    module = importlib.util.module_from_spec(module_spec)
    sys.modules[module_spec.name] = module
    module_spec.loader.exec_module(module)
    with pytest.raises(module.SmokeFailure, match="grounded answer contract"):
        module._assert_ordered_citation_card_equality(_metadata_answer(movie_id))


def test_live_smoke_recommendation_cards_reject_invalid_movie_ids() -> None:
    module_spec = importlib.util.spec_from_file_location("card_movie_id_smoke", SMOKE)
    assert module_spec is not None and module_spec.loader is not None
    module = importlib.util.module_from_spec(module_spec)
    sys.modules[module_spec.name] = module
    module_spec.loader.exec_module(module)
    answer = _metadata_answer("../A")
    movie = answer["movies"][0]
    assert isinstance(movie, dict)
    movie.update({"genre": "喜劇", "tier": "A", "poster_url": None})
    with pytest.raises(module.SmokeFailure, match="recommendation movie card"):
        module._movie_cards(answer)


def test_live_smoke_validates_movie_id_before_building_poster_path(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    module_spec = importlib.util.spec_from_file_location("poster_movie_id_smoke", SMOKE)
    assert module_spec is not None and module_spec.loader is not None
    module = importlib.util.module_from_spec(module_spec)
    sys.modules[module_spec.name] = module
    module_spec.loader.exec_module(module)
    monkeypatch.setattr(
        module,
        "_parallel_mobile_poster_requests",
        lambda *_args, **_kwargs: [module.HttpResult(404, {}, b"")],
    )
    with pytest.raises(module.SmokeFailure, match="returned movie card is invalid"):
        module._assert_returned_posters(
            "http://localhost",
            {"movies": [{"movie_id": "../A", "poster_url": None}]},
            5,
            "rag_demo_session=fixture",
        )


def test_live_smoke_fetches_returned_posters_concurrently_with_isolated_mobile_requests(
    capsys: pytest.CaptureFixture[str],
) -> None:
    """Breaks if poster workers share auth state, serialize, or omit the mobile contract."""
    module_spec = importlib.util.spec_from_file_location("parallel_mobile_live_smoke", SMOKE)
    assert module_spec is not None and module_spec.loader is not None
    module = importlib.util.module_from_spec(module_spec)
    sys.modules[module_spec.name] = module
    module_spec.loader.exec_module(module)
    answer = {
        "movies": [
            {
                "movie_id": movie_id,
                "poster_url": f"/api/posters/{movie_id}",
            }
            for movie_id in ("1978_ZQ_001", "1982_ZJPD_001")
        ]
    }
    cookie_header = f"rag_demo_session={_SmokeHandler.session_token}"

    with _smoke_server(poster_barrier=threading.Barrier(2)) as base_url:
        module._assert_returned_posters(base_url, answer, 5, cookie_header)

    captured = capsys.readouterr()
    assert captured.out == ""
    assert captured.err == ""
    assert _SmokeHandler.session_token not in captured.out + captured.err
    assert _SmokeHandler.poster_max_active == 2
    assert _SmokeHandler.poster_active == 0
    assert len(_SmokeHandler.poster_request_headers) == 2
    assert all(cookie == cookie_header for cookie, _user_agent in _SmokeHandler.poster_request_headers)
    assert all(
        "iPhone" in user_agent and "Mobile" in user_agent
        for _cookie, user_agent in _SmokeHandler.poster_request_headers
    )


def test_live_smoke_checks_every_returned_poster_against_bundle_identity(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Breaks if a card can define its own availability or bypass exact poster bytes."""
    module_spec = importlib.util.spec_from_file_location("poster_identity_live_smoke", SMOKE)
    assert module_spec is not None and module_spec.loader is not None
    module = importlib.util.module_from_spec(module_spec)
    sys.modules[module_spec.name] = module
    module_spec.loader.exec_module(module)
    body = b"RIFF" + b"\x04\x00\x00\x00" + b"WEBP"
    expected = {
        "1987_FGBR_001": module._PosterExpectation(
            True,
            hashlib.sha256(body).hexdigest(),
            len(body),
            "image/webp",
            "restricted",
        ),
        "1970_MTXD_001": module._PosterExpectation(
            False, None, None, None, "unknown"
        ),
    }
    results = [
        module.HttpResult(
            200,
            {
                "content-type": "image/webp",
                "cache-control": "private, no-store",
                "x-rag-rights-status": "restricted",
            },
            body,
        ),
        module.HttpResult(404, {}, b""),
    ]
    monkeypatch.setattr(
        module, "_parallel_mobile_poster_requests", lambda *_args, **_kwargs: results
    )
    answer = {
        "movies": [
            {
                "movie_id": "1987_FGBR_001",
                "poster_url": "/api/posters/1987_FGBR_001",
            },
            {"movie_id": "1970_MTXD_001", "poster_url": None},
        ]
    }

    module._assert_returned_posters(
        "http://localhost",
        answer,
        5,
        "rag_demo_session=fixture",
        poster_expectations=expected,
    )

    bad = [module.HttpResult(200, results[0].headers, body + b"drift"), results[1]]
    monkeypatch.setattr(
        module, "_parallel_mobile_poster_requests", lambda *_args, **_kwargs: bad
    )
    with pytest.raises(module.SmokeFailure, match="poster proxy contract"):
        module._assert_returned_posters(
            "http://localhost",
            answer,
            5,
            "rag_demo_session=fixture",
            poster_expectations=expected,
        )


def test_live_smoke_requires_explicit_null_poster_url_for_unavailable_card(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Breaks if an omitted API field is mistaken for the governed explicit null state."""
    module_spec = importlib.util.spec_from_file_location("null_poster_field_smoke", SMOKE)
    assert module_spec is not None and module_spec.loader is not None
    module = importlib.util.module_from_spec(module_spec)
    sys.modules[module_spec.name] = module
    module_spec.loader.exec_module(module)
    monkeypatch.setattr(
        module,
        "_parallel_mobile_poster_requests",
        lambda *_args, **_kwargs: [module.HttpResult(404, {}, b"")],
    )

    with pytest.raises(module.SmokeFailure, match="poster URL is invalid"):
        module._assert_returned_posters(
            "http://localhost",
            {"movies": [{"movie_id": "1970_MTXD_001"}]},
            5,
            "rag_demo_session=fixture",
            poster_expectations={
                "1970_MTXD_001": module._PosterExpectation(
                    False, None, None, None, "unknown"
                )
            },
        )


def test_live_smoke_refuses_cross_origin_poster_redirect_without_forwarding_cookie() -> None:
    """Breaks if a poster redirect can carry the authenticated Cookie to another origin."""
    module_spec = importlib.util.spec_from_file_location("redirect_safe_live_smoke", SMOKE)
    assert module_spec is not None and module_spec.loader is not None
    module = importlib.util.module_from_spec(module_spec)
    sys.modules[module_spec.name] = module
    module_spec.loader.exec_module(module)
    answer = {
        "movies": [
            {
                "movie_id": "1978_ZQ_001",
                "poster_url": "/api/posters/1978_ZQ_001",
            }
        ]
    }
    cookie_header = f"rag_demo_session={_SmokeHandler.session_token}"
    failure: Exception | None = None

    with _redirecting_poster_servers() as (origin_url, sink_url, sink_cookies):
        assert urllib.parse.urlsplit(origin_url).netloc != urllib.parse.urlsplit(sink_url).netloc
        try:
            module._assert_returned_posters(origin_url, answer, 5, cookie_header)
        except module.SmokeFailure as exc:
            failure = exc

    assert sink_cookies == []
    assert failure is not None
    assert "redirect" in str(failure)


@pytest.mark.parametrize(
    "answer_markdown",
    [
        "目前沒有足夠的檢索證據回答這個問題。",
        "抱歉，我無法推薦三部電影。",
        "I am unable to recommend three films.",
    ],
)
def test_live_smoke_rejects_refusal_language_in_successful_chat_payloads(
    answer_markdown: str,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Breaks if an HTTP-200 refusal can satisfy the live answer gate."""
    module_spec = importlib.util.spec_from_file_location("refusal_live_smoke", SMOKE)
    assert module_spec is not None and module_spec.loader is not None
    module = importlib.util.module_from_spec(module_spec)
    sys.modules[module_spec.name] = module
    module_spec.loader.exec_module(module)
    payload = {
        "answer_markdown": answer_markdown,
        "citations": [{"citation_id": "metadata:A", "movie_id": "A"}],
        "movies": [{"movie_id": "A", "poster_url": None}],
    }
    monkeypatch.setattr(
        module,
        "_request",
        lambda *_args, **_kwargs: module.HttpResult(
            200,
            {"content-type": "application/json"},
            json.dumps(payload).encode("utf-8"),
        ),
    )

    with pytest.raises(module.SmokeFailure, match="refusal"):
        module._chat(urllib.request.build_opener(), "http://localhost", "question", 5)


def test_live_smoke_requires_exact_ordered_citation_card_equality(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Breaks if set equality disguises reversed or extra recommendation evidence."""
    module_spec = importlib.util.spec_from_file_location("ordered_live_smoke", SMOKE)
    assert module_spec is not None and module_spec.loader is not None
    module = importlib.util.module_from_spec(module_spec)
    sys.modules[module_spec.name] = module
    module_spec.loader.exec_module(module)
    payload = {
        "answer_markdown": "第二部。[metadata:B]\n第一部。[metadata:A]",
        "citations": [
            {
                "citation_id": "metadata:B",
                "movie_id": "B",
                "movie_title": "第二部",
                "source_kind": "movie_metadata",
                "page_number": None,
                "source_filename": None,
                "excerpt": "第二部摘錄",
            },
            {
                "citation_id": "metadata:A",
                "movie_id": "A",
                "movie_title": "第一部",
                "source_kind": "movie_metadata",
                "page_number": None,
                "source_filename": None,
                "excerpt": "第一部摘錄",
            },
        ],
        "movies": [
            {"movie_id": "A", "chinese_title": "第一部", "poster_url": None},
            {"movie_id": "B", "chinese_title": "第二部", "poster_url": None},
        ],
    }
    monkeypatch.setattr(
        module,
        "_request",
        lambda *_args, **_kwargs: module.HttpResult(
            200,
            {"content-type": "application/json"},
            json.dumps(payload).encode("utf-8"),
        ),
    )

    with pytest.raises(module.SmokeFailure, match="ordered citation/card"):
        module._chat(urllib.request.build_opener(), "http://localhost", "question", 5)


def test_live_smoke_allows_repeated_page_citations_for_one_ordered_movie(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Breaks if repeated evidence pages are mistaken for extra movie cards."""
    module_spec = importlib.util.spec_from_file_location("repeated_page_live_smoke", SMOKE)
    assert module_spec is not None and module_spec.loader is not None
    module = importlib.util.module_from_spec(module_spec)
    sys.modules[module_spec.name] = module
    module_spec.loader.exec_module(module)
    payload = {
        "answer_markdown": (
            "兩頁證據支持同一部電影。"
            "[pdf:drunken-master-deep-analysis-v1:p1]"
            "[pdf:drunken-master-deep-analysis-v1:p2]"
        ),
        "citations": [
            {
                "citation_id": "pdf:drunken-master-deep-analysis-v1:p1",
                "movie_id": "1978_ZQ_001",
                "movie_title": "醉拳",
                "source_kind": "pdf_page",
                "page_number": 1,
                "source_filename": "S 級港⽚-醉拳.pdf",
                "excerpt": "第一頁摘錄",
            },
            {
                "citation_id": "pdf:drunken-master-deep-analysis-v1:p2",
                "movie_id": "1978_ZQ_001",
                "movie_title": "醉拳",
                "source_kind": "pdf_page",
                "page_number": 2,
                "source_filename": "S 級港⽚-醉拳.pdf",
                "excerpt": "第二頁摘錄",
            },
        ],
        "movies": [
            {
                "movie_id": "1978_ZQ_001",
                "chinese_title": "醉拳",
                "poster_url": None,
            }
        ],
    }

    def request(
        _opener: object,
        _base_url: str,
        _method: str,
        path: str,
        **_kwargs: object,
    ) -> object:
        if path == "/api/posters/1978_ZQ_001":
            return module.HttpResult(404, {"content-type": "application/json"}, b"{}")
        return module.HttpResult(
            200,
            {"content-type": "application/json"},
            json.dumps(payload).encode("utf-8"),
        )

    monkeypatch.setattr(
        module,
        "_request",
        request,
    )

    assert module._chat(
        urllib.request.build_opener(),
        "http://localhost",
        "question",
        5,
        session_cookie="rag_demo_session=fixture",
    ) == payload


def test_live_smoke_recommendations_require_raw_one_to_one_citation_card_order() -> None:
    """Breaks if duplicate recommendation citations collapse into one accepted card."""
    module_spec = importlib.util.spec_from_file_location("one_to_one_live_smoke", SMOKE)
    assert module_spec is not None and module_spec.loader is not None
    module = importlib.util.module_from_spec(module_spec)
    sys.modules[module_spec.name] = module
    module_spec.loader.exec_module(module)
    payload = {
        "answer_markdown": "受控推薦。",
        "citations": [
            {"citation_id": "metadata:A:first", "movie_id": "A"},
            {"citation_id": "metadata:A:duplicate", "movie_id": "A"},
        ],
        "movies": [
            {
                "movie_id": "A",
                "genre": "喜劇",
                "tier": "S",
                "poster_url": None,
            }
        ],
    }

    with pytest.raises(module.SmokeFailure, match="recommendation citation/card"):
        module._movie_cards(payload)


@pytest.mark.parametrize("extra_recommendation", ["followup", "s_tier"])
def test_live_smoke_requires_exactly_three_results_for_every_recommendation_turn(
    extra_recommendation: str,
    monkeypatch: pytest.MonkeyPatch,
    capsys: pytest.CaptureFixture[str],
) -> None:
    """Breaks if follow-up or S-tier acceptance permits more than three cards."""
    module_spec = importlib.util.spec_from_file_location(
        f"exact_three_{extra_recommendation}_live_smoke", SMOKE
    )
    assert module_spec is not None and module_spec.loader is not None
    module = importlib.util.module_from_spec(module_spec)
    sys.modules[module_spec.name] = module
    module_spec.loader.exec_module(module)
    monkeypatch.setattr(module, "_load_smoke_authorities", lambda _path: None)
    module._EXPECTED_POSTERS = {
        movie_id: {
            "sha256": hashlib.sha256(body).hexdigest(),
            "size": len(body),
            "mime_type": "image/webp",
            "rights_status": "unknown",
        }
        for movie_id, body in _SmokeHandler.posters.items()
    }
    _SmokeHandler.extra_recommendation = extra_recommendation
    try:
        with _smoke_server() as base_url:
            monkeypatch.setenv("RAG_DEMO_ACCESS_KEY", _SmokeHandler.access_code)
            monkeypatch.setattr(
                sys,
                "argv",
                [
                    str(SMOKE),
                    "--base-url",
                    base_url,
                    "--expected-revision",
                    _SmokeHandler.serving_revision,
                    "--expected-image-digest",
                    _SmokeHandler.image_digest,
                    "--manifest",
                    "verified-manifest.json",
                ],
            )
            exit_code = module.main()
    finally:
        _SmokeHandler.extra_recommendation = None

    report = json.loads(capsys.readouterr().out)
    assert exit_code == 1
    assert report["passed"] is False
    assert "contract failed" in report["error"]


def test_live_smoke_public_mode_needs_no_access_code_and_sends_no_session_cookie(
    monkeypatch: pytest.MonkeyPatch,
    capsys: pytest.CaptureFixture[str],
) -> None:
    """Breaks if public deployment acceptance silently retains the removed code gate."""
    module_spec = importlib.util.spec_from_file_location("public_access_live_smoke", SMOKE)
    assert module_spec is not None and module_spec.loader is not None
    module = importlib.util.module_from_spec(module_spec)
    sys.modules[module_spec.name] = module
    module_spec.loader.exec_module(module)
    monkeypatch.setattr(module, "_load_smoke_authorities", lambda _path: None)
    module._EXPECTED_POSTERS = {
        movie_id: {
            "sha256": hashlib.sha256(body).hexdigest(),
            "size": len(body),
            "mime_type": "image/webp",
            "rights_status": "unknown",
        }
        for movie_id, body in _SmokeHandler.posters.items()
    }
    monkeypatch.delenv("RAG_DEMO_ACCESS_KEY", raising=False)
    _SmokeHandler.public_access = True
    try:
        with _smoke_server() as base_url:
            monkeypatch.setattr(
                sys,
                "argv",
                [
                    str(SMOKE),
                    "--base-url",
                    base_url,
                    "--expected-revision",
                    _SmokeHandler.serving_revision,
                    "--expected-image-digest",
                    _SmokeHandler.image_digest,
                    "--manifest",
                    "verified-manifest.json",
                    "--public-access",
                ],
            )
            exit_code = module.main()
    finally:
        _SmokeHandler.public_access = False

    report = json.loads(capsys.readouterr().out)
    assert exit_code == 0
    assert report["passed"] is True
    assert report["access_mode"] == "public_tech_demo"
    assert "access_code" not in report
    assert report["checks"]["public_config"] is True
    assert report["checks"]["public_chat"] is True
    assert report["checks"]["public_poster"] is True
    assert report["checks"]["legacy_session_removed"] is True
    assert all(not cookie for cookie, _user_agent in _SmokeHandler.poster_request_headers)


@pytest.mark.parametrize(
    ("scenario", "expected_error"),
    [
        ("person_credit_mismatch", "person recommendation contract failed"),
        ("person_followup_overlap", "person history follow-up contract failed"),
        ("person_genre_mismatch", "person recommendation contract failed"),
        ("comedy_non_exact_genre", "comedy recommendation contract failed"),
        (
            "comedy_without_returned_poster",
            "comedy recommendation contract failed",
        ),
        ("followup_non_exact_genre", "history follow-up contract failed"),
        ("person_non_exact_genre", "person recommendation contract failed"),
        (
            "mixed_person_omits_clarification",
            "mixed person selection contract failed",
        ),
        (
            "mixed_person_citation_only_leak",
            "mixed person selection contract failed",
        ),
        (
            "mixed_person_movie_only_leak",
            "mixed person selection contract failed",
        ),
        (
            "continuation_reset_wrong_person",
            "current person continuation reset contract failed",
        ),
        (
            "continuation_reset_wrong_genres",
            "current person continuation reset contract failed",
        ),
        (
            "continuation_reset_wrong_count",
            "current person continuation reset contract failed",
        ),
        (
            "continuation_reset_wong_overlap",
            "current person continuation reset contract failed",
        ),
        ("metadata_answer_missing_year", "known metadata answer contract failed"),
        ("metadata_answer_missing_director", "known metadata answer contract failed"),
        ("metadata_wrong_release_date", "known metadata answer contract failed"),
        ("metadata_wrong_director", "known metadata answer contract failed"),
        ("drunken_topic_drift", "deep answer topic contract failed"),
        ("aces_topic_drift", "deep answer topic contract failed"),
        ("drunken_question_echo", "deep answer topic contract failed"),
        ("aces_question_echo", "deep answer topic contract failed"),
        ("ood_evidence_leak", "general relevance OOD contract failed"),
        ("irrelevant_answer", "structured recommendation answer contract failed"),
        ("wrong_citation", "grounded answer contract failed"),
        ("null_poster_serves", "null poster endpoint was not absent"),
        ("wrong_rights_status", "returned poster proxy contract failed"),
    ],
)
def test_live_smoke_fails_closed_on_person_or_ood_contract_drift(
    scenario: str,
    expected_error: str,
    monkeypatch: pytest.MonkeyPatch,
    capsys: pytest.CaptureFixture[str],
) -> None:
    """Breaks if the live gate observes but does not enforce the new retrieval boundaries."""
    module_spec = importlib.util.spec_from_file_location(f"{scenario}_live_smoke", SMOKE)
    assert module_spec is not None and module_spec.loader is not None
    module = importlib.util.module_from_spec(module_spec)
    sys.modules[module_spec.name] = module
    module_spec.loader.exec_module(module)
    monkeypatch.setattr(module, "_load_smoke_authorities", lambda _path: None)
    module._EXPECTED_POSTERS = {
        movie_id: {
            "sha256": hashlib.sha256(body).hexdigest(),
            "size": len(body),
            "mime_type": "image/webp",
            "rights_status": "unknown",
        }
        for movie_id, body in _SmokeHandler.posters.items()
    }
    setattr(_SmokeHandler, scenario, True)
    try:
        with _smoke_server() as base_url:
            monkeypatch.setenv("RAG_DEMO_ACCESS_KEY", _SmokeHandler.access_code)
            monkeypatch.setattr(
                sys,
                "argv",
                [
                    str(SMOKE),
                    "--base-url",
                    base_url,
                    "--expected-revision",
                    _SmokeHandler.serving_revision,
                    "--expected-image-digest",
                    _SmokeHandler.image_digest,
                    "--manifest",
                    "verified-manifest.json",
                ],
            )
            exit_code = module.main()
    finally:
        setattr(_SmokeHandler, scenario, False)

    captured = capsys.readouterr()
    report = json.loads(captured.out)
    assert exit_code == 1
    assert report["passed"] is False
    assert report["error"] == expected_error
    assert _SmokeHandler.access_code not in captured.out + captured.err
    assert _SmokeHandler.session_token not in captured.out + captured.err


def test_live_smoke_probes_every_tamper_shape_against_every_protected_endpoint(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    module_spec = importlib.util.spec_from_file_location("tamper_matrix_live_smoke", SMOKE)
    assert module_spec is not None and module_spec.loader is not None
    module = importlib.util.module_from_spec(module_spec)
    sys.modules[module_spec.name] = module
    module_spec.loader.exec_module(module)
    calls: list[tuple[str, str, str]] = []

    def request(
        _opener: object,
        _base_url: str,
        method: str,
        path: str,
        **kwargs: object,
    ) -> object:
        headers = kwargs.get("headers")
        assert isinstance(headers, dict)
        cookie = headers.get("Cookie")
        assert isinstance(cookie, str)
        calls.append((method, path, cookie))
        return module.HttpResult(401, {}, b"")

    monkeypatch.setattr(module, "_request", request)
    module._assert_tampered_session_rejected(
        urllib.request.build_opener(),
        "http://localhost",
        "rag_demo_session=opaque-session-token",
        5,
    )

    assert calls == [
        (method, path, cookie)
        for cookie in TAMPERED_SESSION_COOKIES
        for method, path in TAMPERED_PROTECTED_ENDPOINTS
    ]


@pytest.mark.parametrize(
    ("target_method", "target_path", "target_cookie"),
    [
        (method, path, cookie)
        for cookie in TAMPERED_SESSION_COOKIES
        for method, path in TAMPERED_PROTECTED_ENDPOINTS
    ],
)
def test_live_smoke_rejects_if_any_tamper_shape_is_accepted_by_any_endpoint(
    target_method: str,
    target_path: str,
    target_cookie: str,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    module_spec = importlib.util.spec_from_file_location("tamper_target_live_smoke", SMOKE)
    assert module_spec is not None and module_spec.loader is not None
    module = importlib.util.module_from_spec(module_spec)
    sys.modules[module_spec.name] = module
    module_spec.loader.exec_module(module)

    def request(
        _opener: object,
        _base_url: str,
        method: str,
        path: str,
        **kwargs: object,
    ) -> object:
        headers = kwargs.get("headers")
        assert isinstance(headers, dict)
        status = (
            200
            if (
                method == target_method
                and path == target_path
                and headers.get("Cookie") == target_cookie
            )
            else 401
        )
        return module.HttpResult(status, {}, b"")

    monkeypatch.setattr(module, "_request", request)
    with pytest.raises(module.SmokeFailure, match="tampered session cookie"):
        module._assert_tampered_session_rejected(
            urllib.request.build_opener(),
            "http://localhost",
            "rag_demo_session=opaque-session-token",
            5,
        )


@pytest.mark.parametrize(
    ("cookie_kwargs", "case_id"),
    [
        ({"domain_specified": True}, "domain-specified"),
        ({"domain_initial_dot": True}, "domain-initial-dot"),
        ({"secure": False}, "not-secure"),
        ({"path": "/api"}, "wrong-path"),
    ],
)
def test_live_smoke_login_requires_host_only_secure_root_cookiejar_state(
    cookie_kwargs: dict[str, object],
    case_id: str,
) -> None:
    del case_id
    module_spec = importlib.util.spec_from_file_location("cookiejar_state_smoke", SMOKE)
    assert module_spec is not None and module_spec.loader is not None
    module = importlib.util.module_from_spec(module_spec)
    sys.modules[module_spec.name] = module
    module_spec.loader.exec_module(module)
    raw_cookie = (
        f"rag_demo_session={_SmokeHandler.session_token}; Max-Age=3600; "
        "Path=/; Secure; HttpOnly; SameSite=Strict"
    )
    result = module.HttpResult(204, {"set-cookie": raw_cookie}, b"", (raw_cookie,))
    with pytest.raises(module.SmokeFailure, match="cookie attributes"):
        module._validated_session_cookie(
            result,
            _session_cookie_jar(**cookie_kwargs),
            _SmokeHandler.access_code,
        )


def test_live_smoke_login_rejects_domain_attribute_even_with_host_only_cookiejar() -> None:
    module_spec = importlib.util.spec_from_file_location("domain_header_smoke", SMOKE)
    assert module_spec is not None and module_spec.loader is not None
    module = importlib.util.module_from_spec(module_spec)
    sys.modules[module_spec.name] = module
    module_spec.loader.exec_module(module)
    raw_cookie = (
        f"rag_demo_session={_SmokeHandler.session_token}; Max-Age=3600; "
        "Domain=127.0.0.1; Path=/; Secure; HttpOnly; SameSite=Strict"
    )
    result = module.HttpResult(204, {"set-cookie": raw_cookie}, b"", (raw_cookie,))
    with pytest.raises(module.SmokeFailure, match="cookie attributes"):
        module._validated_session_cookie(
            result,
            _session_cookie_jar(),
            _SmokeHandler.access_code,
        )


@pytest.mark.parametrize(
    ("scenario", "expected_error"),
    [
        ("accept_any_access_code", "incorrect access code was accepted"),
        ("wrong_code_sets_session_cookie", "incorrect access code was accepted"),
        ("accept_any_session_cookie", "tampered session cookie was not rejected"),
        ("accept_tampered_config_cookie", "tampered session cookie was not rejected"),
        ("accept_tampered_chat_cookie", "tampered session cookie was not rejected"),
        ("accept_tampered_poster_cookie", "tampered session cookie was not rejected"),
        ("accept_session_token_suffix", "tampered session cookie was not rejected"),
        ("session_cookie_missing_attributes", "demo session cookie attributes are invalid"),
        ("session_cookie_domain_attribute", "demo session cookie attributes are invalid"),
        ("logout_cookie_missing_attributes", "logout session cookie is invalid"),
        ("logout_cookie_domain_attribute", "logout session cookie is invalid"),
        ("redirect_health", "public health check failed"),
        ("redirect_config", "authenticated config request failed"),
    ],
)
def test_live_smoke_fails_closed_on_auth_or_redirect_drift(
    scenario: str,
    expected_error: str,
    monkeypatch: pytest.MonkeyPatch,
    capsys: pytest.CaptureFixture[str],
) -> None:
    """Breaks if a weak session boundary can hide behind otherwise valid responses."""
    module_spec = importlib.util.spec_from_file_location(f"{scenario}_auth_smoke", SMOKE)
    assert module_spec is not None and module_spec.loader is not None
    module = importlib.util.module_from_spec(module_spec)
    sys.modules[module_spec.name] = module
    module_spec.loader.exec_module(module)
    monkeypatch.setattr(module, "_load_smoke_authorities", lambda _path: None)
    module._EXPECTED_POSTERS = {
        movie_id: {
            "sha256": hashlib.sha256(body).hexdigest(),
            "size": len(body),
            "mime_type": "image/webp",
            "rights_status": "unknown",
        }
        for movie_id, body in _SmokeHandler.posters.items()
    }
    setattr(_SmokeHandler, scenario, True)
    try:
        with _smoke_server() as base_url:
            monkeypatch.setenv("RAG_DEMO_ACCESS_KEY", _SmokeHandler.access_code)
            monkeypatch.setattr(
                sys,
                "argv",
                [
                    str(SMOKE),
                    "--base-url",
                    base_url,
                    "--expected-revision",
                    _SmokeHandler.serving_revision,
                    "--expected-image-digest",
                    _SmokeHandler.image_digest,
                    "--manifest",
                    "verified-manifest.json",
                ],
            )
            exit_code = module.main()
    finally:
        setattr(_SmokeHandler, scenario, False)

    captured = capsys.readouterr()
    report = json.loads(captured.out)
    assert exit_code == 1
    assert report["passed"] is False
    assert report["error"] == expected_error
    assert _SmokeHandler.access_code not in captured.out + captured.err
    assert _SmokeHandler.session_token not in captured.out + captured.err


def test_live_smoke_rejects_extra_config_key_without_leaking_its_value(
    monkeypatch: pytest.MonkeyPatch,
    capsys: pytest.CaptureFixture[str],
) -> None:
    """Breaks if an unexpected infrastructure field can pass or reach the report."""
    module_spec = importlib.util.spec_from_file_location("extra_config_live_smoke", SMOKE)
    assert module_spec is not None and module_spec.loader is not None
    module = importlib.util.module_from_spec(module_spec)
    sys.modules[module_spec.name] = module
    module_spec.loader.exec_module(module)
    monkeypatch.setattr(module, "_load_smoke_authorities", lambda _path: None)
    _SmokeHandler.config_extra_secret = True
    try:
        with _smoke_server() as base_url:
            monkeypatch.setenv("RAG_DEMO_ACCESS_KEY", _SmokeHandler.access_code)
            monkeypatch.setattr(
                sys,
                "argv",
                [
                    str(SMOKE),
                    "--base-url",
                    base_url,
                    "--expected-revision",
                    _SmokeHandler.serving_revision,
                    "--expected-image-digest",
                    _SmokeHandler.image_digest,
                    "--manifest",
                    "verified-manifest.json",
                ],
            )
            exit_code = module.main()
    finally:
        _SmokeHandler.config_extra_secret = False

    captured = capsys.readouterr()
    report = json.loads(captured.out)
    assert exit_code == 1
    assert report["error"] == "deployed config does not match the release contract"
    assert "fixture-secret-must-not-leak" not in captured.out + captured.err
    assert "database_password" not in captured.out + captured.err


def test_live_smoke_executes_full_contract_and_atomically_writes_redacted_report(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    capsys: pytest.CaptureFixture[str],
) -> None:
    module_spec = importlib.util.spec_from_file_location("task6_live_smoke", SMOKE)
    assert module_spec is not None and module_spec.loader is not None
    module = importlib.util.module_from_spec(module_spec)
    sys.modules[module_spec.name] = module
    module_spec.loader.exec_module(module)
    monkeypatch.setattr(module, "_load_smoke_authorities", lambda _path: None)
    module._EXPECTED_POSTERS = {
        movie_id: {
            "sha256": hashlib.sha256(body).hexdigest(),
            "size": len(body),
            "mime_type": "image/webp",
            "rights_status": "unknown",
        }
        for movie_id, body in _SmokeHandler.posters.items()
    }
    output = tmp_path / "nested" / "smoke.json"
    with _smoke_server() as base_url:
        monkeypatch.setenv("RAG_DEMO_ACCESS_KEY", _SmokeHandler.access_code)
        monkeypatch.setattr(
            sys,
            "argv",
            [
                str(SMOKE),
                "--base-url",
                base_url,
                "--expected-revision",
                _SmokeHandler.serving_revision,
                "--expected-image-digest",
                _SmokeHandler.image_digest,
                "--manifest",
                "verified-manifest.json",
                "--access-code-env",
                "RAG_DEMO_ACCESS_KEY",
                "--output",
                str(output),
            ],
        )
        exit_code = module.main()

    captured = capsys.readouterr()
    assert exit_code == 0, captured.out + captured.err
    assert _SmokeHandler.access_code not in captured.out
    assert _SmokeHandler.access_code not in captured.err
    assert _SmokeHandler.session_token not in captured.out
    assert _SmokeHandler.session_token not in captured.err
    assert output.is_file()
    assert not list(output.parent.glob(f".{output.name}.*.tmp"))
    assert output.read_text(encoding="utf-8").strip() == captured.out.strip()
    report = json.loads(captured.out)
    assert _SmokeHandler.session_token not in json.dumps(report)
    assert report["passed"] is True
    assert report["access_code"] == "[REDACTED]"
    assert report["checks"] == {
        "authenticated_config": True,
        "comedy_exact_three": True,
        "corpus_wide_recommendation": False,
        "deep_aces_go_places": True,
        "deep_document_cases": 0,
        "deep_drunken_master": True,
        "followup_inherits_comedy_and_excludes_prior": True,
        "general_relevance_movie_cases": 20,
        "general_relevance_ood_cases": 20,
        "incorrect_access_code_rejected": True,
        "login": True,
        "logout": True,
        "metadata_answer": True,
        "mixed_script_multi_person_rejected": True,
        "known_unavailable_poster": True,
        "known_unavailable_poster_card": True,
        "poster_aces_go_places": True,
        "poster_drunken_master": True,
        "person_and_genre_exact_three": True,
        "person_action_comedy_exact_five": True,
        "person_followup_excludes_prior": True,
        "person_history_reset_action_comedy_exact_five": True,
        "person_continuation_vocabulary_resets_history": True,
        "person_simplified_exact_three": True,
        "person_simplified_default_five": True,
        "person_traditional_exact_three": True,
        "person_traditional_default_five": True,
        "release_title_deep_query": False,
        "returned_posters": True,
        "out_of_domain_rejected": True,
        "post_logout_chat_rejected": True,
        "post_logout_config_rejected": True,
        "public_health": True,
        "s_tier_only_excludes_b_tier": True,
        "tampered_session_cookie_rejected": True,
        "title_routing_regressions": True,
        "unauthenticated_chat_rejected": True,
        "unauthenticated_poster_rejected": True,
    }
    assert _SmokeHandler.requested_posters.count("1978_ZQ_001") == 9
    assert _SmokeHandler.requested_posters.count("1982_ZJPD_001") == 7
    assert _SmokeHandler.requested_posters.count(_SmokeHandler.unavailable_movie_id) == 1
    for movie_id in (
        "CHOW_ACTION_001",
        "CHOW_ACTION_002",
        "CHOW_ACTION_003",
        "CHOW_ACTION_004",
        "CHOW_ACTION_005",
    ):
        assert _SmokeHandler.requested_posters.count(movie_id) == 2
    questions = [str(request.get("question", "")) for request in _SmokeHandler.chat_requests]
    assert "推荐3部周星驰主演的电影。" in questions
    assert "推薦3部周星馳主演的電影。" in questions
    assert "推荐周星驰的好电影" in questions
    assert "推薦周星馳的好電影" in questions
    assert "再推薦3部，不要重複。" in questions
    assert "推薦3部周星馳主演的喜劇電影。" in questions
    assert "想看周星驰和成龍電影，推薦幾部" in questions
    assert "王家卫的电影" in questions
    assert questions.count("周星驰的动作喜剧") == 1
    assert questions.count("周星驰的动作喜剧还有吗") == 1
    assert "最近哪隻股票值得買？" in questions
    assert "請問一下醉拳的導演是誰？" in questions
    assert "錯體追擊組合中誰是導演？" in questions
    assert "《证人》的导演是谁？" in questions
    assert "講一下英雄的成長" in questions
    assert "聊聊朋友之間的信任" in questions
    assert "《英雄》這本書值得讀嗎？" in questions
    assert sum(question in GENERAL_RELEVANCE_MOVIE_QUESTIONS for question in questions) == 20
    assert sum(question in GENERAL_RELEVANCE_OOD_QUESTIONS for question in questions) == 20
    mixed_person_request = next(
        request
        for request in _SmokeHandler.chat_requests
        if request.get("question") == "想看周星驰和成龍電影，推薦幾部"
    )
    assert mixed_person_request["history"] == []
    history_bearing_action_comedy = next(
        request
        for request in _SmokeHandler.chat_requests
        if request.get("question") == "周星驰的动作喜剧还有吗"
        and request.get("history")
    )
    assert history_bearing_action_comedy["history"] == [
        {
            "question": "王家卫的电影",
            "answer": (
                "《測試電影 WONG_001》：符合受控推薦條件。[metadata:WONG_001]\n"
                "《測試電影 WONG_002》：符合受控推薦條件。[metadata:WONG_002]\n"
                "《測試電影 WONG_003》：符合受控推薦條件。[metadata:WONG_003]\n"
                "《測試電影 WONG_004》：符合受控推薦條件。[metadata:WONG_004]\n"
                "《測試電影 WONG_005》：符合受控推薦條件。[metadata:WONG_005]"
            ),
            "movie_ids": ["WONG_001", "WONG_002", "WONG_003", "WONG_004", "WONG_005"],
        }
    ]
