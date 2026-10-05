from __future__ import annotations

import hashlib
import http.server
import json
import os
import re
import shutil
import subprocess
import sys
import threading
import urllib.parse
from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parents[1]
DEPLOY = ROOT / "scripts" / "gcp" / "deploy-demo.ps1"

_V2_SERVICE_NAME = (
    "projects/motionexpaiweb/locations/us-central1/services/hk-movie-rag-demo"
)
_V2_SERVICE_PATH = f"/v2/{_V2_SERVICE_NAME}"
_V2_OPERATION_PREFIX = "/v2/projects/motionexpaiweb/locations/us-central1/operations/"
_OLD_REVISION = "hk-movie-rag-demo-00004-old"
_NEW_REVISION = "hk-movie-rag-demo-00006-new"
_SERVICE_URL = "https://hk-movie-rag-demo-abc-uc.a.run.app"
_TEST_BEARER = "offline-cloud-run-v2-test-token"
_ANSI_SGR = re.compile(r"\x1b\[[0-9;]*m")


def _plain_stderr(result: subprocess.CompletedProcess[str]) -> str:
    return _ANSI_SGR.sub("", result.stderr).rstrip()


def _assert_redacted_failure_status(
    result: subprocess.CompletedProcess[str], expected_status: str
) -> None:
    """Assert controlled error fields without depending on PowerShell line wrapping."""
    stderr = " ".join(_plain_stderr(result).replace("|", " ").split())
    assert f"; {expected_status};" in stderr
    assert "redacted_report=not_available" in stderr


class _FakeCloudRunV2:
    """Real loopback HTTP boundary for the PowerShell Cloud Run v2 client."""

    def __init__(self, scenario: str, event_log: Path) -> None:
        self.scenario = scenario
        self.event_log = event_log
        self.state = "prior"
        self.pending_target: str | None = None
        self.service_get_count = 0
        self.operation_get_counts: dict[str, int] = {}
        self.concurrent_etag = False
        self.candidate_tag_preserved = False
        controller = self

        class Handler(http.server.BaseHTTPRequestHandler):
            def do_GET(self) -> None:
                controller._handle_get(self)

            def do_PATCH(self) -> None:
                controller._handle_patch(self)

            def log_message(self, _format: str, *args: object) -> None:
                del args

        self.server = http.server.ThreadingHTTPServer(("127.0.0.1", 0), Handler)
        self.thread = threading.Thread(target=self.server.serve_forever, daemon=True)

    @property
    def origin(self) -> str:
        host, port = self.server.server_address[:2]
        return f"http://{host}:{port}"

    def start(self) -> None:
        self.thread.start()

    def close(self) -> None:
        self.server.shutdown()
        self.server.server_close()
        self.thread.join(timeout=5)

    def _record(self, **values: object) -> None:
        event = {"tool": "cloud_run_v2", **values}
        with self.event_log.open("a", encoding="utf-8") as stream:
            stream.write(json.dumps(event, separators=(",", ":")) + "\n")

    def _events(self) -> list[dict[str, object]]:
        if not self.event_log.exists():
            return []
        return [
            json.loads(line)
            for line in self.event_log.read_text(encoding="utf-8").splitlines()
        ]

    def _deploy_args(self) -> list[str] | None:
        for event in self._events():
            args = event.get("args")
            if (
                event.get("tool") == "gcloud"
                and isinstance(args, list)
                and args[:3] == ["run", "deploy", "hk-movie-rag-demo"]
            ):
                return args
        return None

    def _candidate_tag(self) -> str:
        args = self._deploy_args()
        if args is not None and "--tag" in args:
            return args[args.index("--tag") + 1]
        return "accept-000000000000"

    def _candidate_url(self) -> str:
        return f"https://{self._candidate_tag()}---hk-movie-rag-demo-abc-uc.a.run.app"

    def _accepted_patches(self) -> list[dict[str, object]]:
        return [
            event
            for event in self._events()
            if event.get("tool") == "cloud_run_v2"
            and event.get("method") == "PATCH"
            and event.get("accepted") is True
        ]

    @staticmethod
    def _send(
        handler: http.server.BaseHTTPRequestHandler,
        status: int,
        value: object,
        *,
        headers: dict[str, str] | None = None,
    ) -> None:
        body = json.dumps(value, separators=(",", ":")).encode("utf-8")
        handler.send_response(status)
        handler.send_header("Content-Type", "application/json")
        handler.send_header("Content-Length", str(len(body)))
        for name, header_value in (headers or {}).items():
            handler.send_header(name, header_value)
        handler.end_headers()
        handler.wfile.write(body)

    def _authorized(self, handler: http.server.BaseHTTPRequestHandler) -> bool:
        return handler.headers.get("Authorization") == f"Bearer {_TEST_BEARER}"

    def _current_etag(self) -> str:
        if self.concurrent_etag:
            return "fixture-etag-concurrent"
        if self.state == "prior":
            if self.scenario in {
                "preexisting-no-traffic-candidate",
                "preexisting-no-traffic-candidate-spec-drift",
                "preexisting-no-traffic-candidate-etag-race",
                "preexisting-no-traffic-candidate-v1-traffic-race",
                "preexisting-no-traffic-candidate-v2-traffic-race",
                "preexisting-candidate-is-ready",
                "preexisting-no-traffic-candidate-stable-smoke-failure",
                "preexisting-no-traffic-candidate-post-state-mismatch",
            }:
                return "fixture-etag-preexisting"
            return (
                "fixture-etag-tagged-zero"
                if self._deploy_args() is not None
                else "fixture-etag-prior"
            )
        if self.state == "candidate":
            return (
                "fixture-etag-clean-new"
                if len(self._accepted_patches()) >= 2
                else "fixture-etag-candidate"
            )
        return "fixture-etag-rollback"

    def _service(self) -> dict[str, object]:
        deployed = self._deploy_args() is not None
        if self.scenario in {
            "deploy-timeout-after-tag",
            "prior-clean-newer-created-delayed-visibility",
        } and self.service_get_count == 2:
            deployed = False
        accepted_patches = self._accepted_patches()
        preexisting_candidate = self.scenario in {
            "preexisting-no-traffic-candidate",
            "preexisting-no-traffic-candidate-spec-drift",
            "preexisting-no-traffic-candidate-etag-race",
            "preexisting-no-traffic-candidate-v1-traffic-race",
            "preexisting-no-traffic-candidate-v2-traffic-race",
            "preexisting-candidate-is-ready",
            "preexisting-no-traffic-candidate-stable-smoke-failure",
            "preexisting-no-traffic-candidate-post-state-mismatch",
        }
        tagged_zero = (
            self.state == "prior" and (deployed or preexisting_candidate)
        ) or (self.state == "rollback" and self.candidate_tag_preserved)
        tagged_promoted = self.state == "candidate" and len(accepted_patches) == 1
        clean_new = self.state == "candidate" and len(accepted_patches) >= 2
        revision = _OLD_REVISION if self.state in {"prior", "rollback"} else _NEW_REVISION
        generation = {"prior": "8", "candidate": "9", "rollback": "10"}[self.state]
        latest_ready_revision = (
            _OLD_REVISION if self.state == "prior" else _NEW_REVISION
        )
        if (
            self.scenario == "candidate-ready-is-candidate" and deployed
            or self.scenario == "preexisting-candidate-is-ready"
        ) and self.state == "prior":
            latest_ready_revision = _NEW_REVISION
        if (
            self.scenario == "post-state-mismatch-rollback-ready-prior"
            and self.state == "rollback"
        ):
            latest_ready_revision = _OLD_REVISION
        elif (
            self.scenario == "post-state-mismatch-rollback-ready-third"
            and self.state == "rollback"
        ):
            latest_ready_revision = "hk-movie-rag-demo-00005-stale"
        target: dict[str, object] = {
            "type": "TRAFFIC_TARGET_ALLOCATION_TYPE_REVISION",
            "revision": revision,
            "percent": 100,
        }
        desired_traffic: list[dict[str, object]] = [dict(target)]
        observed_traffic: list[dict[str, object]] = [dict(target)]
        if tagged_zero:
            tagged_revision = (
                "hk-movie-rag-demo-00007-other"
                if self.scenario == "concurrent-same-tag-different-revision"
                else _NEW_REVISION
            )
            candidate = {
                "type": "TRAFFIC_TARGET_ALLOCATION_TYPE_REVISION",
                "revision": tagged_revision,
                "percent": 0,
                "tag": self._candidate_tag(),
            }
            desired_candidate = dict(candidate)
            observed_candidate = dict(candidate, uri=self._candidate_url())
            if self.scenario == "v2-candidate-desired-zero-omitted":
                desired_candidate.pop("percent")
            elif self.scenario == "v2-candidate-observed-zero-omitted":
                observed_candidate.pop("percent")
            elif self.scenario == "v2-candidate-desired-zero-null":
                desired_candidate["percent"] = None
            elif self.scenario == "v2-candidate-observed-zero-null":
                observed_candidate["percent"] = None
            elif self.scenario == "v2-candidate-desired-zero-string":
                desired_candidate["percent"] = "0"
            elif self.scenario == "v2-candidate-desired-zero-bool":
                desired_candidate["percent"] = False
            elif self.scenario == "v2-candidate-desired-zero-nonzero":
                desired_candidate["percent"] = 1
            desired_traffic.append(desired_candidate)
            observed_traffic.append(observed_candidate)
        elif tagged_promoted:
            candidate = {
                "type": "TRAFFIC_TARGET_ALLOCATION_TYPE_REVISION",
                "revision": _NEW_REVISION,
                "percent": 100,
                "tag": self._candidate_tag(),
            }
            desired_traffic = [dict(candidate)]
            observed_traffic = [dict(candidate, uri=self._candidate_url())]
        elif clean_new:
            target = {
                "type": "TRAFFIC_TARGET_ALLOCATION_TYPE_REVISION",
                "revision": _NEW_REVISION,
                "percent": 100,
            }
            desired_traffic = [dict(target)]
            observed_traffic = [dict(target)]
        value: dict[str, object] = {
            "name": _V2_SERVICE_NAME,
            "generation": generation,
            "observedGeneration": generation,
            "terminalCondition": {"state": "CONDITION_SUCCEEDED"},
            "latestCreatedRevision": (
                f"{_V2_SERVICE_NAME}/revisions/"
                + (
                    "hk-movie-rag-demo-00007-other"
                    if deployed
                    and self.scenario == "concurrent-same-tag-different-revision"
                    else "hk-movie-rag-demo-00005-stale"
                    if not deployed
                    and self.scenario == "prior-clean-newer-created-delayed-visibility"
                    else _NEW_REVISION
                    if deployed or preexisting_candidate or self.state != "prior"
                    else _OLD_REVISION
                )
            ),
            "latestReadyRevision": (
                f"{_V2_SERVICE_NAME}/revisions/{latest_ready_revision}"
            ),
            "traffic": desired_traffic,
            "trafficStatuses": observed_traffic,
            "uri": _SERVICE_URL,
            "etag": self._current_etag(),
        }
        if (
            self.scenario == "preexisting-no-traffic-candidate-etag-race"
            and self.state == "prior"
            and self.service_get_count >= 2
        ):
            value["etag"] = "fixture-etag-concurrent"
        elif (
            self.scenario == "preexisting-no-traffic-candidate-v2-traffic-race"
            and self.state == "prior"
            and self.service_get_count >= 2
        ):
            value["traffic"] = [dict(target, revision=_NEW_REVISION)]
            value["trafficStatuses"] = [dict(target, revision=_NEW_REVISION)]
        if self.service_get_count == 1 and self.state == "prior":
            if self.scenario == "v2-pre-etag-missing":
                value.pop("etag")
            elif self.scenario == "v2-pre-etag-bool":
                value["etag"] = True
            elif self.scenario == "v2-pre-generation-mismatch":
                value["observedGeneration"] = "7"
            elif self.scenario == "v2-pre-reconciling":
                value["reconciling"] = True
            elif self.scenario == "v2-pre-reconciling-string":
                value["reconciling"] = "false"
            elif self.scenario == "v2-pre-terminal-failed":
                value["terminalCondition"] = {"state": "CONDITION_FAILED"}
            elif self.scenario == "v2-pre-traffic-object":
                value["traffic"] = dict(target)
            elif self.scenario == "v2-pre-status-object":
                value["trafficStatuses"] = dict(target)
            elif self.scenario == "v2-pre-string-percent":
                value["traffic"] = [dict(target, percent="100")]
            elif self.scenario == "v2-pre-floating":
                value["traffic"] = [
                    {
                        "type": "TRAFFIC_TARGET_ALLOCATION_TYPE_LATEST",
                        "revision": _OLD_REVISION,
                        "percent": 100,
                    }
                ]
            elif self.scenario == "v2-pre-tagged":
                value["traffic"] = [dict(target, tag="stable")]
            elif self.scenario == "v2-pre-status-mismatch":
                value["trafficStatuses"] = [
                    dict(target, revision=_NEW_REVISION)
                ]
            elif self.scenario in {
                "v2-pre-latest-mismatch",
                "v2-pre-latest-ready-mismatch",
            }:
                value["latestReadyRevision"] = (
                    f"{_V2_SERVICE_NAME}/revisions/hk-movie-rag-demo-00005-stale"
                )
            elif self.scenario == "v2-pre-latest-created-mismatch":
                value["latestCreatedRevision"] = (
                    f"{_V2_SERVICE_NAME}/revisions/hk-movie-rag-demo-00005-stale"
                )
            elif self.scenario == "v2-pre-ready-changed-after-v1":
                value["latestReadyRevision"] = (
                    f"{_V2_SERVICE_NAME}/revisions/{_NEW_REVISION}"
                )
            elif self.scenario == "v2-pre-status-tagged":
                value["trafficStatuses"] = [dict(target, tag="stable")]
            elif self.scenario == "v2-pre-status-uri-mismatch":
                value["trafficStatuses"] = [
                    dict(target, uri="https://changed-abc-uc.a.run.app")
                ]
            elif self.scenario == "v2-observed-uri-present":
                value["trafficStatuses"] = [dict(target, uri=_SERVICE_URL)]
            elif self.scenario == "v2-pre-url-mismatch":
                value["uri"] = "https://changed-abc-uc.a.run.app"
        if self.scenario == "v2-post-readback-etag-missing" and self.state == "candidate":
            value.pop("etag")
        if self.scenario == "rollback-verification-mismatch" and self.state == "rollback":
            value["traffic"] = [dict(target, revision=_NEW_REVISION)]
        if self.scenario == "rollback-url-mismatch" and self.state == "rollback":
            value["uri"] = "https://changed-abc-uc.a.run.app"
        if self.scenario == "rollback-service-not-ready" and self.state == "rollback":
            value["terminalCondition"] = {"state": "CONDITION_FAILED"}
        return value

    def _handle_get(self, handler: http.server.BaseHTTPRequestHandler) -> None:
        if not self._authorized(handler):
            self._record(method="GET", path=handler.path, auth_valid=False)
            self._send(handler, 401, {"error": "unauthorized"})
            return
        parsed = urllib.parse.urlsplit(handler.path)
        if parsed.path == _V2_SERVICE_PATH and not parsed.query:
            self.service_get_count += 1
            if (
                self.scenario == "v2-concurrent-before-rollback"
                and self.state == "candidate"
                and self.service_get_count >= 3
            ):
                self.concurrent_etag = True
            if (
                self.scenario == "v2-promotion-same-state-etag-race"
                and self.state == "candidate"
                and self.service_get_count >= 2
            ):
                self.concurrent_etag = True
            if (
                self.scenario == "v2-post-validation-same-state-etag-race"
                and self.state == "candidate"
                and self.service_get_count >= 3
            ):
                self.concurrent_etag = True
            if (
                self.scenario == "v2-rollback-same-state-etag-race"
                and self.state == "rollback"
                and self.service_get_count >= 4
            ):
                self.concurrent_etag = True
            if (
                self.scenario == "v2-rollback-post-validation-same-state-etag-race"
                and self.state == "rollback"
                and self.service_get_count >= 5
            ):
                self.concurrent_etag = True
            self._record(
                method="GET",
                resource="service",
                state=self.state,
                auth_valid=True,
            )
            self._send(handler, 200, self._service())
            return
        if parsed.path.startswith(_V2_OPERATION_PREFIX) and not parsed.query:
            operation_id = parsed.path.removeprefix(_V2_OPERATION_PREFIX)
            expected_id = "op-promote" if self.pending_target == _NEW_REVISION else "op-rollback"
            count = self.operation_get_counts.get(operation_id, 0) + 1
            self.operation_get_counts[operation_id] = count
            self._record(
                method="GET",
                resource="operation",
                operation=operation_id,
                auth_valid=True,
            )
            if operation_id != expected_id or self.pending_target is None:
                self._send(handler, 404, {"error": "unknown operation"})
                return
            if self.scenario == "v2-promotion-operation-timeout" and operation_id == "op-promote":
                self._send(handler, 200, {"name": parsed.path.removeprefix("/v2/")})
                return
            if count == 1:
                self._send(handler, 200, {"name": parsed.path.removeprefix("/v2/")})
                return
            if self.scenario == "v2-promotion-operation-failure" and operation_id == "op-promote":
                self._send(
                    handler,
                    200,
                    {
                        "name": parsed.path.removeprefix("/v2/"),
                        "done": True,
                        "error": {
                            "code": 13,
                            "message": "LEAK-ME-operation fixture-etag-prior",
                        },
                    },
                )
                return
            if self.scenario == "v2-promotion-operation-done-string" and operation_id == "op-promote":
                self._send(
                    handler,
                    200,
                    {"name": parsed.path.removeprefix("/v2/"), "done": "true"},
                )
                return
            self.state = "candidate" if self.pending_target == _NEW_REVISION else "rollback"
            self.pending_target = None
            self._send(
                handler,
                200,
                {
                    "name": parsed.path.removeprefix("/v2/"),
                    "done": True,
                    "response": self._service(),
                },
            )
            return
        self._record(method="GET", path=handler.path, auth_valid=True)
        self._send(handler, 404, {"error": "unexpected path"})

    def _handle_patch(self, handler: http.server.BaseHTTPRequestHandler) -> None:
        if not self._authorized(handler):
            self._record(method="PATCH", auth_valid=False, accepted=False)
            self._send(handler, 401, {"error": "unauthorized"})
            return
        parsed = urllib.parse.urlsplit(handler.path)
        query = urllib.parse.parse_qs(parsed.query, keep_blank_values=True)
        try:
            length = int(handler.headers.get("Content-Length", "0"))
            if length < 1 or length > 16_384:
                raise ValueError("invalid content length")
            body = json.loads(handler.rfile.read(length).decode("utf-8"))
        except (UnicodeDecodeError, ValueError, json.JSONDecodeError):
            self._record(method="PATCH", auth_valid=True, accepted=False, request_valid=False)
            self._send(handler, 400, {"error": "invalid JSON"})
            return
        body_keys = sorted(body) if isinstance(body, dict) else []
        traffic = body.get("traffic") if isinstance(body, dict) else None
        single_entry = (
            traffic[0] if isinstance(traffic, list) and len(traffic) == 1 else None
        )
        single_target = (
            single_entry.get("revision") if isinstance(single_entry, dict) else None
        )
        single_keys = set(single_entry) if isinstance(single_entry, dict) else set()
        single_tag = single_entry.get("tag") if isinstance(single_entry, dict) else None
        exact_single_entry = (
            isinstance(single_entry, dict)
            and single_keys
            in (
                {"type", "revision", "percent"},
                {"type", "revision", "percent", "tag"},
            )
            and single_entry.get("type") == "TRAFFIC_TARGET_ALLOCATION_TYPE_REVISION"
            and single_target in {_NEW_REVISION, _OLD_REVISION}
            and type(single_entry.get("percent")) is int
            and single_entry.get("percent") == 100
            and (
                ("tag" not in single_entry and single_tag is None)
                or (
                    single_target == _NEW_REVISION
                    and single_tag == self._candidate_tag()
                )
            )
        )
        stable_entry = (
            traffic[0] if isinstance(traffic, list) and len(traffic) == 2 else None
        )
        candidate_entry = (
            traffic[1] if isinstance(traffic, list) and len(traffic) == 2 else None
        )
        exact_tagged_rollback = (
            isinstance(stable_entry, dict)
            and set(stable_entry) == {"type", "revision", "percent"}
            and stable_entry.get("type")
            == "TRAFFIC_TARGET_ALLOCATION_TYPE_REVISION"
            and stable_entry.get("revision") == _OLD_REVISION
            and type(stable_entry.get("percent")) is int
            and stable_entry.get("percent") == 100
            and isinstance(candidate_entry, dict)
            and set(candidate_entry) == {"type", "revision", "percent", "tag"}
            and candidate_entry.get("type")
            == "TRAFFIC_TARGET_ALLOCATION_TYPE_REVISION"
            and candidate_entry.get("revision") == _NEW_REVISION
            and type(candidate_entry.get("percent")) is int
            and candidate_entry.get("percent") == 0
            and candidate_entry.get("tag") == self._candidate_tag()
        )
        target_revision = _OLD_REVISION if exact_tagged_rollback else single_target
        exact_traffic = exact_single_entry or exact_tagged_rollback
        summary_entry = stable_entry if exact_tagged_rollback else single_entry
        request_valid = (
            parsed.path == _V2_SERVICE_PATH
            and query
            == {
                "allowMissing": ["false"],
                "forceNewRevision": ["false"],
                "updateMask": ["traffic"],
            }
            and isinstance(body, dict)
            and set(body) == {"name", "etag", "traffic"}
            and body.get("name") == _V2_SERVICE_NAME
            and body.get("etag") == self._current_etag()
            and exact_traffic
        )
        summary = {
            "method": "PATCH",
            "auth_valid": True,
            "body_keys": body_keys,
            "traffic_keys": (
                sorted(summary_entry) if isinstance(summary_entry, dict) else []
            ),
            "traffic_count": len(traffic) if isinstance(traffic, list) else 0,
            "target_revision": target_revision,
            "percent": (
                summary_entry.get("percent")
                if isinstance(summary_entry, dict)
                else None
            ),
            "allocation_type": (
                summary_entry.get("type") if isinstance(summary_entry, dict) else None
            ),
            "tagged": exact_tagged_rollback
            or ("tag" in single_entry if isinstance(single_entry, dict) else False),
            "etag_matched": body.get("etag") == self._current_etag()
            if isinstance(body, dict)
            else False,
            "request_valid": request_valid,
        }
        if not request_valid:
            self._record(**summary, accepted=False)
            self._send(handler, 400, {"error": "invalid conditional traffic request"})
            return
        if self.scenario == "v2-promotion-redirect" and target_revision == _NEW_REVISION:
            self._record(**summary, accepted=False, redirected=True)
            self._send(
                handler,
                302,
                {"error": "redirect forbidden"},
                headers={"Location": "http://example.invalid/token-steal"},
            )
            return
        if (
            self.scenario in {"promotion-failure", "v2-promotion-conflict"}
            and target_revision == _NEW_REVISION
        ) or (
            self.scenario in {"rollback-failure", "v2-concurrent-rollback-race"}
            and target_revision == _OLD_REVISION
        ):
            self._record(**summary, accepted=False, conflict=True)
            self._send(
                handler,
                409,
                {
                    "error": (
                        "LEAK-ME-http-body offline-cloud-run-v2-test-token "
                        "fixture-etag-candidate"
                    )
                },
            )
            return
        self.candidate_tag_preserved = exact_tagged_rollback
        self.pending_target = str(target_revision)
        operation_id = "op-promote" if target_revision == _NEW_REVISION else "op-rollback"
        operation_name = (
            "projects/motionexpaiweb/locations/us-central1/operations/" + operation_id
        )
        self._record(**summary, accepted=True)
        self._send(handler, 200, {"name": operation_name})


def _powershell() -> str:
    executable = shutil.which("pwsh")
    if executable is None:
        pytest.skip("PowerShell 7 is unavailable")
    return executable


def _git(repo: Path, *args: str) -> None:
    subprocess.run(["git", *args], cwd=repo, check=True, capture_output=True, timeout=30)


def _write_release_fixture(
    source: Path, bundle_dir: Path
) -> tuple[Path, list[str]]:
    bundle_dir.mkdir(parents=True)
    derived = source / "assets" / "posters" / "derived"
    derived.mkdir(parents=True)
    poster_records: list[dict[str, object]] = []
    movie_ids: list[str] = []
    for index in range(4_545):
        movie_id = f"T{index:05d}"
        body = b"RIFF" + index.to_bytes(4, "little") + b"WEBPfixture"
        path = derived / f"{movie_id}.webp"
        path.write_bytes(body)
        movie_ids.append(movie_id)
        poster_records.append(
            {
                "record_kind": "poster",
                "movie_id": movie_id,
                "is_primary": True,
                "quality_status": "machine_passed",
                "derived_object_uri": f"assets/posters/derived/{movie_id}.webp",
                "derived_mime_type": "image/webp",
                "derived_content_sha256": hashlib.sha256(body).hexdigest(),
                "derived_byte_length": len(body),
            }
        )
    for index in range(113):
        poster_records.append(
            {
                "record_kind": "poster",
                "movie_id": f"U{index:05d}",
                "is_primary": True,
                "quality_status": "unavailable",
            }
        )
    bundle = bundle_dir / "rag_bundle.jsonl"
    bundle.write_text(
        "".join(json.dumps(row, separators=(",", ":")) + "\n" for row in poster_records),
        encoding="utf-8",
    )
    manifest = bundle_dir / "rag_release_manifest.json"
    manifest.write_text('{"fixture":"immutable"}\n', encoding="utf-8")
    return bundle_dir, movie_ids


def _write_fake_tools(bin_dir: Path) -> None:
    fake_uv = bin_dir / "fake_uv.py"
    fake_uv.write_text(
        r"""
import json
import os
import pathlib
import subprocess
import sys

args = sys.argv[1:]
with open(os.environ["FAKE_EVENT_LOG"], "a", encoding="utf-8") as stream:
    stream.write(json.dumps({
        "tool": "uv",
        "args": args,
        "demo_access_key_present": "RAG_DEMO_ACCESS_KEY" in os.environ,
    }) + "\n")
if (
    os.environ["FAKE_DEPLOY_SCENARIO"] == "head-advance"
    and args[:3] == ["run", "hk-movie-rag", "build-rag-bundle"]
):
    marker = pathlib.Path.cwd() / "head-drift.txt"
    marker.write_text(marker.read_text(encoding="utf-8") + "x" if marker.exists() else "x")
    subprocess.run(["git", "add", "head-drift.txt"], check=True)
    subprocess.run(["git", "commit", "-m", "advance fixture head"], check=True)
if args[:3] == ["run", "hk-movie-rag", "verify-rag-bundle"]:
    bundle_dir = pathlib.Path(os.environ["FAKE_BUNDLE_DIR"])
    manifest_sha256 = __import__("hashlib").sha256(
        (bundle_dir / "rag_release_manifest.json").read_bytes()
    ).hexdigest()
    bundle_sha256 = __import__("hashlib").sha256(
        (bundle_dir / "rag_bundle.jsonl").read_bytes()
    ).hexdigest()
    print(json.dumps({
        "access_mode": "restricted_demo",
        "approved_poster_object_count": 4545,
        "bundle_sha256": bundle_sha256,
        "derived_inventory_sha256": "b754727129bc044b6c9da1e6db22067c8279c5e7f8dd41d0350e365376538154",
        "derived_poster_bytes": 123456,
        "document_count": 3,
        "document_embedding_profile": "vertex-title-text-v1",
        "documents": [
            {
                "document_id": "drunken-master-deep-analysis-v1",
                "movie_id": "1978_ZQ_001",
                "page_count": 3,
                "quality_status": "manual_approved",
                "rights_status": "restricted",
                "source_filename": "S 級港⽚-醉拳.pdf",
                "source_sha256": "ab27baf8e22ceb63a476b65dbfb02951a63a9e9bf39d96a47cd8e3f9fd314a75",
            },
            {
                "document_id": "aces-go-places-deep-analysis-v1",
                "movie_id": "1982_ZJPD_001",
                "page_count": 3,
                "quality_status": "manual_approved",
                "rights_status": "restricted",
                "source_filename": "S 級港⽚-最佳拍檔.pdf",
                "source_sha256": "69fa1ad28916c043689ef4fe826cde816c05a3b254e5a0372d1a57ca2ce63bce",
            },
            {
                "document_id": "its-a-mad-mad-mad-world-deep-analysis-v1",
                "movie_id": "1987_FGBR_001",
                "page_count": 6,
                "quality_status": "manual_approved",
                "rights_status": "restricted",
                "source_filename": "S級港片-富貴逼人.pdf",
                "source_sha256": "5aef7f8684bc0d91d77ffb6d7f89cb323358e2804ef8eaacbc0a02cfeb3d3df4",
            },
        ],
        "embedding_dimension": 768,
        "embedding_model": "gemini-embedding-2",
        "expected_embedding_count": 4670,
        "facet_count": 4658,
        "gcs_prefix": "rag/v1.2-demo-r2/",
        "generation_model": "gemini-3.5-flash-lite",
        "manifest_sha256": manifest_sha256,
        "metadata_passage_count": 4658,
        "valid": True,
        "movie_count": 4658,
        "parent_release_manifest_sha256": "e9842db33208e7146d78d86689b13f81fffcf41288d79d84e599b93050445e2c",
        "pdf_passage_count": 12,
        "pilot_count": 24,
        "poster_authority_sha256": "9e56adda2a1fdfa648869c53d7e60a085db5d1c6d11db90dbec13bfe12f0eeed",
        "poster_row_count": 4658,
        "primary_poster_row_count": 4658,
        "rag_release_id": "v1.2-demo-r2",
        "rag_schema_version": "1.2",
        "relevance_policy_sha256": "a4ecce727a469689a2a08c6d42e0d7d1eb975761173e3b6e4d1eaf34db7b2aba",
        "schema_version": "rag-bundle-verification/v2",
        "text_extraction_profile": "cjk-layout-v1",
        "tier_a_count": 313,
        "tier_b_count": 4295,
        "tier_s_count": 50,
        "unavailable_poster_row_count": 113,
    }))
elif args[:2] == ["run", "python"] and "live-smoke.py" in args[2]:
    base_url = args[args.index("--base-url") + 1]
    stable_failure = (
        os.environ["FAKE_DEPLOY_SCENARIO"]
        in {
            "stable-smoke-failure",
            "preexisting-no-traffic-candidate-stable-smoke-failure",
        }
        and base_url == "https://hk-movie-rag-demo-abc-uc.a.run.app"
    )
    print(json.dumps({
        "schema_version": "rag-demo-live-smoke/v1",
        "access_mode": "public_tech_demo",
        "passed": not stable_failure,
        "base_url": base_url,
    }))
elif args[:3] not in (
    ["run", "hk-movie-rag", "verify-release"],
    ["run", "hk-movie-rag", "build-rag-bundle"],
):
    raise SystemExit(8)
""".strip()
        + "\n",
        encoding="utf-8",
    )
    fake_gcloud = bin_dir / "fake_gcloud.py"
    fake_gcloud.write_text(
        r"""
import hashlib
import json
import os
import pathlib
import sys
import time

args = sys.argv[1:]
key = tuple(item for item in args if not item.startswith("--"))
scenario = os.environ["FAKE_DEPLOY_SCENARIO"]
event_log = pathlib.Path(os.environ["FAKE_EVENT_LOG"])
prior = []
if event_log.exists():
    prior = [json.loads(line) for line in event_log.read_text(encoding="utf-8").splitlines()]
with event_log.open("a", encoding="utf-8") as stream:
    stream.write(json.dumps({
        "tool": "gcloud",
        "args": args,
        "ambient_google": {
            name: os.environ.get(name)
            for name in ("GOOGLE_API_KEY", "GOOGLE_CLOUD_LOCATION", "GOOGLE_CLOUD_PROJECT")
        },
        "test_mode": os.environ.get("RAG_DEMO_TEST_MODE"),
        "demo_access_key_present": "RAG_DEMO_ACCESS_KEY" in os.environ,
    }) + "\n")
if "RAG_DEMO_ACCESS_KEY" in os.environ:
    raise SystemExit(19)

project = "motionexpaiweb"
bucket = "motionexpaiweb-880586285913-hk-movie-rag-release"
bundle_dir = pathlib.Path(os.environ["FAKE_BUNDLE_DIR"])
manifest_uri = f"gs://{bucket}/rag/v1.2-demo-r2/rag_release_manifest.json"
bundle_uri = f"gs://{bucket}/rag/v1.2-demo-r2/rag_bundle.jsonl"
legacy_manifest_uri = f"gs://{bucket}/rag/v1.2-demo-mvp1/rag_release_manifest.json"
legacy_bundle_uri = f"gs://{bucket}/rag/v1.2-demo-mvp1/rag_bundle.jsonl"
new_revision = "hk-movie-rag-demo-00006-new"
old_revision = "hk-movie-rag-demo-00004-old"
immutable_image = (
    "us-central1-docker.pkg.dev/motionexpaiweb/hk-movie-rag/demo@sha256:" + "a" * 64
)
instance_connection = "motionexpaiweb:us-central1:hk-movie-rag-pg"
service_account = "hk-rag-runtime@motionexpaiweb.iam.gserviceaccount.com"
relevance_digest = "a4ecce727a469689a2a08c6d42e0d7d1eb975761173e3b6e4d1eaf34db7b2aba"
authority_digest = "9e56adda2a1fdfa648869c53d7e60a085db5d1c6d11db90dbec13bfe12f0eeed"

def object_value(uri):
    if uri == legacy_manifest_uri:
        return {
            "custom_fields": {
                "sha256": "18be34d0c36d0d010fceb1b3ff4c2dea734479f899f8ef28ae7541d8f43fc9a0"
            },
            "generation": "1785741058239832",
            "size": "1024",
            "content_type": "application/json",
            "cache_control": "no-store",
        }
    if uri == legacy_bundle_uri:
        return {
            "custom_fields": {
                "sha256": "beb745fe0fa2944cff594c445b5bb7b7097c6d103aba02f4c8a762142d4cc30a"
            },
            "generation": "1785741077175031",
            "size": "10926913",
            "content_type": "application/x-ndjson",
            "cache_control": "no-store",
        }
    if uri not in {manifest_uri, bundle_uri}:
        print("unexpected object URI", file=sys.stderr)
        raise SystemExit(9)
    local = bundle_dir / ("rag_release_manifest.json" if uri == manifest_uri else "rag_bundle.jsonl")
    digest = hashlib.sha256(local.read_bytes()).hexdigest()
    if scenario in {"drift", "metadata-wrong-hash"} and uri == manifest_uri:
        digest = "0" * 64
    value = {
        "custom_fields": {"sha256": digest},
        "generation": "999",
        "size": str(local.stat().st_size),
        "content_type": "application/json" if uri == manifest_uri else "application/x-ndjson",
        "cache_control": "no-store",
    }
    if scenario == "legacy-metadata":
        value["metadata"] = value.pop("custom_fields")
    elif scenario == "metadata-both":
        value["metadata"] = {"sha256": digest}
    elif scenario == "metadata-missing":
        value.pop("custom_fields")
    elif scenario == "metadata-null":
        value["custom_fields"] = None
    elif scenario == "metadata-scalar":
        value["custom_fields"] = digest
    elif scenario == "metadata-array":
        value["custom_fields"] = [{"sha256": digest}]
    elif scenario == "metadata-extra-key":
        value["custom_fields"]["undeclared"] = "not-allowed"
    elif scenario == "metadata-wrong-size":
        value["size"] = str(local.stat().st_size + 1)
    elif scenario == "metadata-wrong-content-type":
        value["content_type"] = "application/octet-stream"
    elif scenario == "metadata-wrong-cache-control":
        value["cache_control"] = "public,max-age=3600"
    return value

def require_separate_argv(flag):
    if any(item.startswith(flag + "=") for item in args):
        print(f"{flag} was passed in equals form", file=sys.stderr)
        raise SystemExit(9)
    if flag not in args or args.index(flag) + 1 >= len(args):
        print(f"{flag} value was not passed as separate argv", file=sys.stderr)
        raise SystemExit(9)
    return args[args.index(flag) + 1]

if key[:4] == ("artifacts", "docker", "images", "describe") and any(
    item.startswith("--location") for item in args
):
    print("unsupported --location", file=sys.stderr)
    raise SystemExit(9)
if key[:3] == ("storage", "objects", "update") or "delete" in key:
    print("forbidden mutation", file=sys.stderr)
    raise SystemExit(9)
if "--public-access-prevention=unspecified" in args:
    print("forbidden public access", file=sys.stderr)
    raise SystemExit(9)

if key[:3] == ("storage", "buckets", "describe"):
    value = {
        "name": bucket,
        "location": "US-CENTRAL1",
        "uniform_bucket_level_access": True,
        "public_access_prevention": "enforced",
    }
elif key[:3] == ("storage", "objects", "describe"):
    uri = key[3]
    already_uploaded = any(
        event["tool"] == "gcloud"
        and event["args"][:2] == ["storage", "cp"]
        and uri in event["args"]
        for event in prior
    )
    if scenario == "fresh" and uri in {manifest_uri, bundle_uri} and not already_uploaded:
        print("ERROR: HTTP 404 not found", file=sys.stderr)
        raise SystemExit(1)
    object_description = object_value(uri)
    if scenario == "description-array-singleton":
        value = [object_description]
    elif scenario == "description-array-multiple":
        value = [object_description, object_description]
    else:
        value = object_description
elif key[:2] == ("storage", "cp"):
    value = {}
elif key[:2] == ("storage", "rsync"):
    if "--no-clobber" not in args:
        print("existing poster metadata would be mutated", file=sys.stderr)
        raise SystemExit(1)
    value = {}
elif key[:2] == ("storage", "ls"):
    for index in range(4545):
        print(f"gs://{bucket}/assets/posters/derived/T{index:05d}.webp")
    if scenario == "prefix-extra":
        print(f"gs://{bucket}/assets/posters/derived/rogue.txt")
    raise SystemExit(0)
elif key[:2] == ("builds", "submit"):
    if scenario == "windows-colon-argv":
        require_separate_argv("--tag")
    context = pathlib.Path(key[2]).resolve()
    repo = pathlib.Path(os.environ["FAKE_REPO_ROOT"]).resolve()
    forbidden = [
        context / "src" / ".env",
        context / "src" / "nested" / "private.pem",
        context / "src" / "key.json",
        context / "credentials" / "service-account.json",
        context / ".git",
    ]
    if context == repo or not (context / "src" / "app.py").is_file() or any(
        path.exists() for path in forbidden
    ):
        print("build context is not the clean committed archive", file=sys.stderr)
        raise SystemExit(9)
    value = {}
elif key[:4] == ("artifacts", "docker", "images", "describe"):
    value = {"image_summary": {"digest": "sha256:" + "a" * 64}}
elif key[:3] == ("secrets", "versions", "list"):
    secret_name = key[3]
    if secret_name not in {"hk-rag-db-password", "hk-rag-demo-key"}:
        print("unexpected secret inventory", file=sys.stderr)
        raise SystemExit(9)
    list_count = sum(
        event["tool"] == "gcloud"
        and event["args"][:4] == ["secrets", "versions", "list", secret_name]
        for event in prior
    )
    version_number = "5" if secret_name == "hk-rag-db-password" else "3"
    if list_count > 1 and (
        scenario == "db-secret-version-changed-before-promotion"
        and secret_name == "hk-rag-db-password"
        or scenario == "demo-secret-version-changed-before-promotion"
        and secret_name == "hk-rag-demo-key"
    ):
        version_number = "6" if secret_name == "hk-rag-db-password" else "4"
    version = {
        "name": f"projects/880586285913/secrets/{secret_name}/versions/{version_number}",
        "state": "ENABLED",
    }
    scenario_applies = (
        scenario.startswith("db-secret-version-") and secret_name == "hk-rag-db-password"
    ) or (
        scenario.startswith("secret-version-") and secret_name == "hk-rag-demo-key"
    )
    if scenario_applies and scenario.endswith("ambiguous"):
        value = [
            version,
            {
                "name": f"projects/880586285913/secrets/{secret_name}/versions/9",
                "state": "ENABLED",
            },
        ]
    elif scenario_applies and scenario.endswith("disabled"):
        version["state"] = "DISABLED"
        value = [version]
    elif scenario_applies and scenario.endswith("malformed"):
        version["name"] = f"projects/880586285913/secrets/{secret_name}/versions/latest"
        value = [version]
    elif scenario_applies and scenario.endswith("object"):
        value = version
    elif scenario_applies and scenario.endswith("scalar"):
        value = "not-an-array"
    elif scenario_applies and scenario.endswith("null"):
        value = None
    else:
        value = [version]
elif key[:3] == ("sql", "instances", "describe"):
    value = {"connectionName": f"{project}:us-central1:hk-movie-rag-pg"}
elif key[:4] == ("run", "jobs", "executions", "list"):
    job_name = next(item.split("=", 1)[1] for item in args if item.startswith("--job="))
    if scenario == "job-active":
        value = [{
            "metadata": {"name": f"{job_name}-live1"},
            "completionStatus": "EXECUTION_RUNNING",
        }]
    else:
        value = []
elif key[:3] == ("run", "jobs", "deploy"):
    if scenario == "windows-colon-argv":
        for flag in (
            "--image",
            "--set-cloudsql-instances",
            "--set-env-vars",
            "--set-secrets",
            "--args",
        ):
            require_separate_argv(flag)
    value = {}
elif key[:3] == ("run", "jobs", "execute"):
    job_name = key[3]
    execute_count = sum(
        event["tool"] == "gcloud"
        and event["args"][:4] == ["run", "jobs", "execute", job_name]
        for event in prior
    )
    suffixes = ("abcde", "fghij", "klmno")
    suffix = suffixes[min(execute_count, len(suffixes) - 1)]
    execution_id = f"{job_name}-{suffix}"
    full_name = "projects/motionexpaiweb/locations/us-central1/jobs/" \
                f"{job_name}/executions/{execution_id}"
    if scenario == "existing":
        value = {"name": full_name}
    elif scenario == "job-wrong-name":
        value = {"metadata": {"name": "other-job-abcde"}}
    elif scenario == "job-ambiguous-name":
        value = {
            "name": full_name,
            "metadata": {"name": f"{job_name}-fghij"},
        }
    else:
        value = {"metadata": {"name": execution_id}}
elif key[:4] == ("run", "jobs", "executions", "describe"):
    execution_id = key[4]
    job_name = execution_id.rsplit("-", 1)[0]
    prior_describes = sum(
        event["tool"] == "gcloud"
        and event["args"][:4] == ["run", "jobs", "executions", "describe"]
        and event["args"][4] == execution_id
        for event in prior
    )
    if scenario == "job-failure" and job_name == "hk-movie-rag-ingest":
        status = "False"
    elif scenario == "poster-job-failure" and job_name == "hk-movie-rag-poster-verify":
        status = "False"
    elif scenario == "job-timeout" or prior_describes == 0:
        status = "Unknown"
    else:
        status = "True"
    if scenario == "existing":
        state = "CONDITION_SUCCEEDED" if status == "True" else "CONDITION_RECONCILING"
        value = {
            "name": "projects/motionexpaiweb/locations/us-central1/jobs/"
                    f"{job_name}/executions/{key[4]}",
            "conditions": [{"type": "Completed", "state": state}],
        }
    else:
        value = {
            "metadata": {"name": key[4]},
            "status": {"conditions": [{"type": "Completed", "status": status}]},
        }
elif key[:2] == ("run", "deploy"):
    if scenario == "windows-colon-argv":
        for flag in (
            "--image",
            "--set-cloudsql-instances",
            "--set-env-vars",
            "--set-secrets",
        ):
            require_separate_argv(flag)
    if scenario == "deploy-nonzero-after-tag":
        print("simulated post-mutation CLI failure LEAK-ME", file=sys.stderr)
        raise SystemExit(8)
    if scenario == "deploy-timeout-after-tag":
        time.sleep(5)
    deploy_candidate = new_revision
    if scenario == "deploy-candidate-equals-prior":
        deploy_candidate = old_revision
    elif scenario == "deploy-candidate-outside-service":
        deploy_candidate = "other-service-00006-new"
    elif scenario == "deploy-candidate-readback-mismatch":
        deploy_candidate = "hk-movie-rag-demo-00007-output"
    deploy_generation = 8
    deploy_observed_generation = 8
    deploy_ready = "True"
    deploy_invoker_disabled = "true"
    deploy_conditions: object = [{"type": "Ready", "status": deploy_ready}]
    if scenario == "deploy-result-stale-generation":
        deploy_observed_generation = 7
    elif scenario == "deploy-result-not-ready":
        deploy_conditions = [{"type": "Ready", "status": "False"}]
    elif scenario == "deploy-result-ready-bool":
        deploy_conditions = [{"type": "Ready", "status": True}]
    elif scenario == "deploy-result-conditions-object":
        deploy_conditions = {"type": "Ready", "status": "True"}
    elif scenario == "deploy-result-annotation-bool":
        deploy_invoker_disabled = True
    value = {
        "metadata": {
            "name": "hk-movie-rag-demo",
            "generation": deploy_generation,
            "annotations": {
                "run.googleapis.com/invoker-iam-disabled": deploy_invoker_disabled,
            },
        },
        "status": {
            "url": "https://hk-movie-rag-demo-abc-uc.a.run.app",
            "observedGeneration": deploy_observed_generation,
            "conditions": deploy_conditions,
            "latestCreatedRevisionName": deploy_candidate,
        },
    }
    if scenario == "deploy-result-array-singleton":
        value = [value]
    elif scenario == "deploy-result-scalar":
        value = "invalid-deploy-result"
    elif scenario == "deploy-result-null":
        value = None
elif key[:3] == ("run", "services", "update-traffic"):
    target = next((item for item in args if item.startswith("--to-revisions=")), "")
    if scenario == "promotion-failure" and target == f"--to-revisions={new_revision}=100":
        print("simulated promotion failure", file=sys.stderr)
        raise SystemExit(8)
    if scenario == "rollback-failure" and target == f"--to-revisions={old_revision}=100":
        print("simulated rollback failure", file=sys.stderr)
        raise SystemExit(8)
    value = {}
elif key[:3] == ("run", "services", "describe"):
    describe_count = sum(
        event["tool"] == "gcloud"
        and event["args"][:4] == ["run", "services", "describe", "hk-movie-rag-demo"]
        for event in prior
    )
    deployed = any(
        event["tool"] == "gcloud"
        and event["args"][:3] == ["run", "deploy", "hk-movie-rag-demo"]
        for event in prior
    )
    deploy_event = next(
        (
            event
            for event in prior
            if event["tool"] == "gcloud"
            and event["args"][:3] == ["run", "deploy", "hk-movie-rag-demo"]
        ),
        None,
    )
    candidate_tag = "accept-000000000000"
    if deploy_event is not None and "--tag" in deploy_event["args"]:
        candidate_tag = deploy_event["args"][deploy_event["args"].index("--tag") + 1]
    candidate_url = (
        f"https://{candidate_tag}---hk-movie-rag-demo-abc-uc.a.run.app"
    )
    promoted = any(
        (
            event["tool"] == "gcloud"
            and event["args"][:4]
            == ["run", "services", "update-traffic", "hk-movie-rag-demo"]
            and f"--to-revisions={new_revision}=100" in event["args"]
        )
        or (
            event["tool"] == "cloud_run_v2"
            and event.get("method") == "PATCH"
            and event.get("target_revision") == new_revision
            and event.get("accepted") is True
        )
        for event in prior
    )
    rolled_back = any(
        (
            event["tool"] == "gcloud"
            and event["args"][:4]
            == ["run", "services", "update-traffic", "hk-movie-rag-demo"]
            and f"--to-revisions={old_revision}=100" in event["args"]
        )
        or (
            event["tool"] == "cloud_run_v2"
            and event.get("method") == "PATCH"
            and event.get("target_revision") == old_revision
            and event.get("accepted") is True
        )
        for event in prior
    )
    annotations = {"run.googleapis.com/invoker-iam-disabled": "true"}
    if describe_count == 0 and scenario == "pre-service-annotation-false":
        annotations["run.googleapis.com/invoker-iam-disabled"] = "false"
    elif describe_count == 0 and scenario == "pre-service-annotation-missing":
        annotations = {}
    if deployed and not promoted and scenario == "service-annotation-false":
        annotations["run.googleapis.com/invoker-iam-disabled"] = "false"
    elif deployed and not promoted and scenario == "service-annotation-missing":
        annotations = {}
    url = "https://hk-movie-rag-demo-abc-uc.a.run.app"
    if describe_count == 0 and scenario == "pre-service-url-path-spoof":
        url = "https://evil.example/path.run.app"
    if deployed and not promoted and scenario == "service-url-invalid":
        url = "http://invalid.example"

    if promoted and not rolled_back and scenario == "post-url-mismatch":
        url = "https://changed-abc-uc.a.run.app"
    if rolled_back and scenario == "rollback-url-mismatch":
        url = "https://changed-abc-uc.a.run.app"

    generation = 7 if describe_count == 0 else 8
    observed_generation = generation
    service_ready = "True"
    if describe_count == 0 and scenario == "pre-service-stale-generation":
        observed_generation = 6
    elif describe_count == 0 and scenario == "pre-service-not-ready":
        service_ready = "False"
    elif deployed and not promoted and scenario == "candidate-service-stale-generation":
        observed_generation = 7
    elif deployed and not promoted and scenario == "candidate-service-not-ready":
        service_ready = "False"
    elif promoted and not rolled_back and scenario == "post-service-stale-generation":
        observed_generation = 7
    elif promoted and not rolled_back and scenario == "post-service-not-ready":
        service_ready = "False"
    elif rolled_back and scenario == "rollback-service-not-ready":
        service_ready = "False"
    if describe_count == 0 and scenario == "pre-service-ready-bool":
        service_ready = True
    if describe_count == 0 and scenario == "pre-service-annotation-bool":
        annotations["run.googleapis.com/invoker-iam-disabled"] = True

    if describe_count == 0:
        latest_created = old_revision
        latest_ready = old_revision
        traffic = [{
            "revisionName": old_revision,
            "percent": 100,
            "tag": None,
            "latestRevision": None,
        }]
        if scenario == "prior-traffic-missing":
            traffic = []
        elif scenario == "prior-traffic-split":
            traffic = [
                {"revisionName": old_revision, "percent": 90},
                {"revisionName": "hk-movie-rag-demo-00003-older", "percent": 10},
            ]
        elif scenario == "prior-traffic-tagged":
            traffic = [{"revisionName": old_revision, "percent": 100, "tag": "stable"}]
        elif scenario == "prior-traffic-non-100":
            traffic = [{"revisionName": old_revision, "percent": 99}]
        elif scenario == "prior-traffic-floating":
            traffic = [{"revisionName": old_revision, "latestRevision": True, "percent": 100}]
        elif scenario == "prior-traffic-latest-string":
            traffic = [{"revisionName": old_revision, "latestRevision": "false", "percent": 100}]
        elif scenario == "prior-traffic-string-percent":
            traffic = [{"revisionName": old_revision, "percent": "100"}]
        elif scenario == "prior-traffic-object":
            traffic = {"revisionName": old_revision, "percent": 100}
        elif scenario == "prior-latest-created-outside-service":
            latest_created = "other-service-00004-old"
        elif scenario == "prior-clean-newer-created-delayed-visibility":
            latest_created = "hk-movie-rag-demo-00005-stale"
        elif scenario in {
            "preexisting-no-traffic-candidate",
            "preexisting-no-traffic-candidate-spec-drift",
            "preexisting-no-traffic-candidate-etag-race",
            "preexisting-no-traffic-candidate-v1-traffic-race",
            "preexisting-no-traffic-candidate-v2-traffic-race",
            "preexisting-no-traffic-candidate-stable-smoke-failure",
            "preexisting-no-traffic-candidate-post-state-mismatch",
        }:
            latest_created = new_revision
            traffic.append({
                "revisionName": new_revision,
                "percent": 0,
                "tag": candidate_tag,
                "url": candidate_url,
                "latestRevision": None,
            })
        elif scenario == "preexisting-candidate-is-ready":
            latest_created = new_revision
            latest_ready = new_revision
            traffic.append({
                "revisionName": new_revision,
                "percent": 0,
                "tag": candidate_tag,
                "url": candidate_url,
                "latestRevision": None,
            })
        elif scenario == "prior-latest-ready-third":
            latest_created = new_revision
            latest_ready = "hk-movie-rag-demo-00005-stale"
    elif deployed and not promoted:
        latest_created = new_revision
        latest_ready = (
            new_revision
            if scenario in {
                "candidate-ready-is-candidate",
                "preexisting-candidate-is-ready",
            }
            else old_revision
        )
        traffic = [
            {"revisionName": old_revision, "percent": 100, "latestRevision": None},
            {
                "revisionName": new_revision,
                "percent": 0,
                "tag": candidate_tag,
                "url": candidate_url,
                "latestRevision": None,
            },
        ]
        if scenario == "candidate-latest-mismatch":
            latest_ready = "hk-movie-rag-demo-00005-stale"
        elif scenario == "candidate-prior-traffic-changed":
            traffic = [{"revisionName": new_revision, "percent": 100}]
        elif scenario == "preexisting-no-traffic-candidate-v1-traffic-race":
            traffic = [{"revisionName": new_revision, "percent": 100}]
        elif scenario == "candidate-prior-traffic-floating":
            traffic[0]["latestRevision"] = True
        candidate_percent_values = {
            "candidate-v1-zero-null": None,
            "candidate-v1-zero-string": "0",
            "candidate-v1-zero-bool": False,
            "candidate-v1-zero-nonzero": 1,
        }
        if scenario == "candidate-v1-zero-omitted":
            traffic[1].pop("percent")
        elif scenario in candidate_percent_values:
            traffic[1]["percent"] = candidate_percent_values[scenario]
    elif rolled_back:
        latest_created = new_revision
        latest_ready = new_revision
        traffic = [{"revisionName": old_revision, "percent": 100, "latestRevision": None}]
        if scenario == "post-state-mismatch-rollback-ready-prior":
            latest_ready = old_revision
        elif scenario == "post-state-mismatch-rollback-ready-third":
            latest_ready = "hk-movie-rag-demo-00005-stale"
        if scenario == "rollback-verification-mismatch":
            traffic = [{"revisionName": new_revision, "percent": 100}]
    else:
        latest_created = new_revision
        latest_ready = new_revision
        traffic = [{"revisionName": new_revision, "percent": 100, "latestRevision": None}]
        if scenario in {
            "post-state-mismatch",
            "post-state-mismatch-rollback-ready-prior",
            "post-state-mismatch-rollback-ready-third",
            "preexisting-no-traffic-candidate-post-state-mismatch",
            "rollback-failure",
            "rollback-verification-mismatch",
            "rollback-url-mismatch",
            "rollback-service-not-ready",
            "v2-concurrent-before-rollback",
            "v2-concurrent-rollback-race",
            "v2-rollback-same-state-etag-race",
            "v2-rollback-post-validation-same-state-etag-race",
        }:
            traffic = [{"revisionName": old_revision, "percent": 100}]
        elif scenario == "post-traffic-floating":
            traffic[0]["latestRevision"] = True
    service_name = "hk-movie-rag-demo"
    if describe_count == 0 and scenario == "pre-service-name-mismatch":
        service_name = "other-service"
    elif deployed and not promoted and scenario == "candidate-service-name-mismatch":
        service_name = "other-service"
    value = {
        "metadata": {"name": service_name, "generation": generation, "annotations": annotations},
        "status": {
            "url": url,
            "observedGeneration": observed_generation,
            "conditions": [{"type": "Ready", "status": service_ready}],
            "latestCreatedRevisionName": latest_created,
            "latestReadyRevisionName": latest_ready,
            "traffic": traffic,
        },
    }
    if describe_count == 0 and scenario == "pre-service-conditions-object":
        value["status"]["conditions"] = {"type": "Ready", "status": "True"}
    if describe_count == 0 and scenario == "pre-service-array-singleton":
        value = [value]
    elif describe_count == 0 and scenario == "pre-service-scalar":
        value = "invalid-service"
    elif deployed and not promoted and scenario == "candidate-service-array-singleton":
        value = [value]
    elif promoted and not rolled_back and scenario == "post-service-array-singleton":
        value = [value]
elif key[:3] == ("run", "revisions", "describe"):
    requested_revision = key[3]
    revision_name = requested_revision
    ready_status = "True"
    image = immutable_image
    revision_service_account = service_account
    cloudsql = instance_connection
    concurrency = 20
    timeout = 300
    ports = [{"containerPort": 8080, "name": "http1"}]
    resources = {"limits": {"cpu": "1", "memory": "1Gi"}}
    revision_annotations = {
        "run.googleapis.com/cloudsql-instances": cloudsql,
        "autoscaling.knative.dev/maxScale": "3",
    }
    command = None
    container_args = None
    volume_mounts = None
    volumes = None
    plain_env = {
        "GOOGLE_CLOUD_PROJECT": project,
        "VERTEX_LOCATION": "global",
        "RAG_RELEASE_ID": "v1.2-demo-r2",
        "RAG_RELEASE_MANIFEST_SHA256": hashlib.sha256(
            (bundle_dir / "rag_release_manifest.json").read_bytes()
        ).hexdigest(),
        "RAG_EMBEDDING_MODEL": "gemini-embedding-2",
        "RAG_EMBEDDING_DIMENSION": "768",
        "RAG_GENERATION_MODEL": "gemini-3.5-flash-lite",
        "RAG_RELEASE_BUCKET": bucket,
        "RAG_RELEVANCE_POLICY_SHA256": relevance_digest,
        "RAG_POSTER_AUTHORITY_SHA256": authority_digest,
        "RAG_IMAGE_DIGEST": immutable_image.split("@", 1)[1],
        "DB_NAME": "hk_movie_rag",
        "DB_USER": "hk_rag_app",
        "INSTANCE_CONNECTION_NAME": instance_connection,
    }
    secret_env = {
        "DB_PASSWORD": {"name": "hk-rag-db-password", "key": "5"},
    }
    if scenario == "revision-name-mismatch":
        revision_name = old_revision
    elif scenario == "revision-not-ready":
        ready_status = "False"
    elif scenario in {
        "revision-image-mismatch",
        "preexisting-no-traffic-candidate-spec-drift",
    }:
        image = image.replace("a" * 64, "b" * 64)
    elif scenario == "revision-service-account-mismatch":
        revision_service_account = "wrong@motionexpaiweb.iam.gserviceaccount.com"
    elif scenario == "revision-cloudsql-mismatch":
        cloudsql = "motionexpaiweb:us-central1:wrong-instance"
        revision_annotations["run.googleapis.com/cloudsql-instances"] = cloudsql
    elif scenario == "revision-env-mismatch":
        plain_env["RAG_RELEVANCE_POLICY_SHA256"] = "0" * 64
    elif scenario == "revision-image-digest-env-mismatch":
        plain_env["RAG_IMAGE_DIGEST"] = "sha256:" + "b" * 64
    elif scenario == "revision-secret-mismatch":
        secret_env["DB_PASSWORD"]["key"] = "latest"
    elif scenario == "revision-concurrency-mismatch":
        concurrency = 19
    elif scenario == "revision-timeout-mismatch":
        timeout = 299
    elif scenario == "revision-port-mismatch":
        ports = [{"containerPort": 8081, "name": "http1"}]
    elif scenario == "revision-port-name-mismatch":
        ports = [{"containerPort": 8080, "name": "wrong"}]
    elif scenario == "revision-cpu-mismatch":
        resources["limits"]["cpu"] = "1000m"
    elif scenario == "revision-memory-mismatch":
        resources["limits"]["memory"] = "2Gi"
    elif scenario == "revision-max-scale-mismatch":
        revision_annotations["autoscaling.knative.dev/maxScale"] = "4"
    elif scenario == "revision-min-scale-present":
        revision_annotations["autoscaling.knative.dev/minScale"] = "0"
    elif scenario == "revision-secret-alias-present":
        revision_annotations["run.googleapis.com/secrets"] = (
            "alias:projects/880586285913/secrets/hk-rag-db-password"
        )
    elif scenario == "revision-secret-alias-null":
        revision_annotations["run.googleapis.com/secrets"] = None
    elif scenario == "revision-ready-bool":
        ready_status = True
    elif scenario == "revision-image-bool":
        image = True
    elif scenario == "revision-service-account-bool":
        revision_service_account = True
    elif scenario == "revision-cloudsql-bool":
        revision_annotations["run.googleapis.com/cloudsql-instances"] = True
    elif scenario == "revision-max-scale-bool":
        revision_annotations["autoscaling.knative.dev/maxScale"] = True
    elif scenario == "revision-cpu-bool":
        resources["limits"]["cpu"] = True
    elif scenario == "revision-memory-bool":
        resources["limits"]["memory"] = True
    elif scenario == "revision-port-name-bool":
        ports = [{"containerPort": 8080, "name": True}]
    elif scenario == "revision-plain-env-bool":
        plain_env["GOOGLE_CLOUD_PROJECT"] = True
    elif scenario == "revision-secret-name-bool":
        secret_env["DB_PASSWORD"]["name"] = True
    elif scenario == "revision-secret-key-number":
        secret_env["DB_PASSWORD"]["key"] = 5
    elif scenario == "revision-command-present":
        command = ["python"]
    elif scenario == "revision-args-present":
        container_args = ["-m", "other"]
    elif scenario == "revision-volume-present":
        volumes = [{"name": "unexpected"}]
    elif scenario == "revision-volume-mount-present":
        volume_mounts = [{"name": "unexpected", "mountPath": "/unexpected"}]
    revision_describe_count = sum(
        event["tool"] == "gcloud"
        and event["args"][:3] == ["run", "revisions", "describe"]
        for event in prior
    )
    generation = 8
    observed_generation = 8
    if scenario == "revision-stale-generation":
        observed_generation = 7
    if scenario == "post-revision-config-mismatch" and revision_describe_count > 0:
        resources["limits"]["memory"] = "2Gi"
    env = [{"name": name, "value": value} for name, value in plain_env.items()]
    env.extend(
        {
            "name": name,
            "valueFrom": {"secretKeyRef": value},
        }
        for name, value in secret_env.items()
    )
    if scenario == "revision-plain-env-extra-property":
        env[0]["unexpected"] = True
    elif scenario == "revision-secret-entry-extra-property":
        env[-1]["unexpected"] = True
    elif scenario == "revision-valuefrom-extra-property":
        env[-1]["valueFrom"]["unexpected"] = True
    elif scenario == "revision-secretref-extra-property":
        env[-1]["valueFrom"]["secretKeyRef"]["unexpected"] = True
    elif scenario == "revision-resources-extra-property":
        resources["unexpected"] = True
    elif scenario == "revision-port-extra-property":
        ports[0]["unexpected"] = True
    value = {
        "metadata": {
            "name": revision_name,
            "generation": generation,
            "annotations": revision_annotations,
        },
        "spec": {
            "serviceAccountName": revision_service_account,
            "containerConcurrency": concurrency,
            "timeoutSeconds": timeout,
            "containers": [{
                "image": image,
                "ports": ports,
                "env": env,
                "resources": resources,
                **({"command": command} if command is not None else {}),
                **({"args": container_args} if container_args is not None else {}),
                **({"volumeMounts": volume_mounts} if volume_mounts is not None else {}),
            }],
            **({"volumes": volumes} if volumes is not None else {}),
        },
        "status": {
            "observedGeneration": observed_generation,
            "conditions": [
                {"type": "Ready", "status": ready_status, "reason": "Retired"}
            ],
        },
    }
    if scenario == "revision-conditions-object":
        value["status"]["conditions"] = {"type": "Ready", "status": "True"}
    elif scenario == "revision-containers-object":
        value["spec"]["containers"] = value["spec"]["containers"][0]
    elif scenario == "revision-ports-object":
        value["spec"]["containers"][0]["ports"] = ports[0]
    if scenario == "revision-array-singleton":
        value = [value]
    elif scenario == "revision-scalar":
        value = "invalid-revision"
else:
    print(json.dumps({"unexpected": args}), file=sys.stderr)
    raise SystemExit(9)
print(json.dumps(value))
""".strip()
        + "\n",
        encoding="utf-8",
    )
    (bin_dir / "uv.ps1").write_text(
        f'& "{sys.executable}" "{fake_uv}" @args\nexit $LASTEXITCODE\n',
        encoding="utf-8",
    )
    (bin_dir / "gcloud.ps1").write_text(
        f'& "{sys.executable}" "{fake_gcloud}" @args\nexit $LASTEXITCODE\n',
        encoding="utf-8",
    )


@pytest.fixture(scope="module")
def deploy_fixture(tmp_path_factory: pytest.TempPathFactory) -> dict[str, object]:
    temp = tmp_path_factory.mktemp("full-deploy")
    repo = temp / "repo"
    script_dir = repo / "scripts" / "gcp"
    script_dir.mkdir(parents=True)
    full_script = script_dir / DEPLOY.name
    shutil.copy2(DEPLOY, full_script)
    transaction_script = script_dir / "deploy-demo-transaction-test.ps1"
    production_source = DEPLOY.read_text(encoding="utf-8")
    poster_gate_start = production_source.index("function Assert-ExactPosterAllowlist {")
    poster_gate_end = production_source.index("\n$trackedStatus = ", poster_gate_start)
    poster_gate_source = production_source[poster_gate_start:poster_gate_end]
    assert poster_gate_source.count("function ") == 1
    assert poster_gate_source.rstrip().endswith("return $resolvedDerivedRoot\n}")
    transaction_source = (
        production_source[:poster_gate_start]
        + """function Assert-ExactPosterAllowlist {
    param([string]$RagBundlePath)
    # Transaction-state tests exercise the cloud mutation protocol, not the
    # independent 4,545-file byte gate. The exact production script is still
    # exercised by the full-poster-gate success and tamper regressions below.
    return (Resolve-Path -LiteralPath (Join-Path $resolvedSourceRoot 'assets/posters/derived')).Path
}
"""
        + production_source[poster_gate_end:]
    )
    transaction_script.write_text(transaction_source, encoding="utf-8")
    (repo / "config").mkdir()
    (repo / "config" / "rag_demo_r2.yaml").write_text(
        "fixture: true\n", encoding="utf-8"
    )
    (script_dir / "live-smoke.py").write_text("# offline fixture\n", encoding="utf-8")
    (repo / "src").mkdir()
    (repo / "src" / "app.py").write_text("SAFE = True\n", encoding="utf-8")
    (repo / "Dockerfile").write_text("FROM scratch\n", encoding="utf-8")
    (repo / "pyproject.toml").write_text("[project]\nname='fixture'\n", encoding="utf-8")
    (repo / "uv.lock").write_text("version = 1\n", encoding="utf-8")
    (repo / ".gitignore").write_text(
        ".env\n*.pem\nkey.json\n*_key.json\ncredentials/\nartifacts/\n",
        encoding="utf-8",
    )
    _git(repo, "init")
    _git(repo, "config", "user.email", "tests@example.invalid")
    _git(repo, "config", "user.name", "tests")
    _git(repo, "add", ".")
    _git(repo, "commit", "-m", "fixture")
    (repo / "src" / ".env").write_text("SECRET=ignored\n", encoding="utf-8")
    (repo / "src" / "nested").mkdir()
    (repo / "src" / "nested" / "private.pem").write_text("ignored pem\n", encoding="utf-8")
    (repo / "src" / "key.json").write_text('{"ignored":"key"}\n', encoding="utf-8")
    (repo / "credentials").mkdir()
    (repo / "credentials" / "service-account.json").write_text(
        '{"ignored":"credential"}\n', encoding="utf-8"
    )
    source = temp / "source"
    bundle_dir = repo / "artifacts" / "rag-demo" / "deploy"
    bundle_dir, movie_ids = _write_release_fixture(source, bundle_dir)
    documents = temp / "documents"
    documents.mkdir()
    for filename in (
        "S 級港⽚-醉拳.pdf",
        "S 級港⽚-最佳拍檔.pdf",
        "S級港片-富貴逼人.pdf",
    ):
        (documents / filename).write_bytes(b"%PDF-1.4\noffline fixture\n")
    bin_dir = temp / "bin"
    bin_dir.mkdir()
    _write_fake_tools(bin_dir)
    return {
        "script": transaction_script,
        "full_script": full_script,
        "source": source,
        "documents": documents,
        "bundle_dir": bundle_dir,
        "movie_ids": movie_ids,
        "bin": bin_dir,
        "temp": temp,
    }


def _run_apply(
    fixture: dict[str, object],
    scenario: str,
    *,
    exact_v2_test_sentinel: bool = True,
    reuse_from_release_id: str = "v1.2-demo",
    validate_full_poster_gate: bool = False,
) -> tuple[subprocess.CompletedProcess[str], list[dict[str, object]]]:
    temp = fixture["temp"]
    assert isinstance(temp, Path)
    log = temp / f"events-{scenario}.jsonl"
    log.unlink(missing_ok=True)
    cloud_run_v2 = _FakeCloudRunV2(scenario, log)
    cloud_run_v2.start()
    env = os.environ.copy()
    env["PATH"] = f"{fixture['bin']}{os.pathsep}{env['PATH']}"
    env["FAKE_DEPLOY_SCENARIO"] = scenario
    env["FAKE_EVENT_LOG"] = str(log)
    env["FAKE_BUNDLE_DIR"] = str(fixture["bundle_dir"])
    selected_script = fixture["full_script"] if validate_full_poster_gate else fixture["script"]
    env["FAKE_REPO_ROOT"] = str(Path(selected_script).parents[2])
    env["RAG_DEMO_ACCESS_KEY"] = "offline-access-code-never-log"
    if exact_v2_test_sentinel:
        env["RAG_DEMO_TEST_MODE"] = "offline-fake-gcloud"
    else:
        env.pop("RAG_DEMO_TEST_MODE", None)
    env["GOOGLE_API_KEY"] = "must-not-reach-gcloud"
    env["GOOGLE_CLOUD_LOCATION"] = "wrong-location"
    env["GOOGLE_CLOUD_PROJECT"] = "wrong-project"
    try:
        command = [
                _powershell(),
                "-NoLogo",
                "-NoProfile",
                "-NonInteractive",
                "-File",
                str(selected_script),
                "-ProjectId",
                "motionexpaiweb",
                "-Region",
                "us-central1",
                "-SourceRoot",
                str(fixture["source"]),
                "-RagConfigPath",
                "config/rag_demo_r2.yaml",
                "-DocumentSourceRoot",
                str(fixture["documents"]),
                "-ReuseFromReleaseId",
                reuse_from_release_id,
                "-BundleDirectory",
                str(fixture["bundle_dir"]),
                "-TestCloudRunV2Origin",
                cloud_run_v2.origin,
                "-Apply",
            ]
        if exact_v2_test_sentinel:
            apply_index = command.index("-Apply")
            command[apply_index:apply_index] = [
                "-TestJobPollMilliseconds",
                "10",
                "-TestJobDeadlineSeconds",
                "10",
                "-TestServiceDeployTimeoutSeconds",
                "1",
            ]
        result = subprocess.run(
            command,
            check=False,
            capture_output=True,
            text=True,
            encoding="utf-8",
            env=env,
            # The module fixture intentionally contains the full 4,545-file poster
            # allowlist. A cold Windows Defender scan can exceed two minutes under
            # concurrent workspace I/O even though later runs are warm; keep this
            # subprocess deadline bounded without turning host contention into a
            # false deployment-contract failure.
            timeout=180,
        )
    finally:
        cloud_run_v2.close()
    events = (
        [json.loads(line) for line in log.read_text(encoding="utf-8").splitlines()]
        if log.exists()
        else []
    )
    return result, events


def _gcloud_calls(events: list[dict[str, object]]) -> list[list[str]]:
    return [event["args"] for event in events if event["tool"] == "gcloud"]  # type: ignore[misc]


def _traffic_calls(calls: list[list[str]]) -> list[list[str]]:
    return [
        call
        for call in calls
        if call[:4] == ["run", "services", "update-traffic", "hk-movie-rag-demo"]
    ]


def _v2_events(events: list[dict[str, object]]) -> list[dict[str, object]]:
    return [event for event in events if event["tool"] == "cloud_run_v2"]


def _v2_patches(events: list[dict[str, object]]) -> list[dict[str, object]]:
    return [event for event in _v2_events(events) if event.get("method") == "PATCH"]


def _mutating_calls(calls: list[list[str]]) -> list[list[str]]:
    return [
        call
        for call in calls
        if call[:2] in (["storage", "cp"], ["storage", "rsync"], ["builds", "submit"])
        or call[:3] in (["run", "jobs", "deploy"], ["run", "jobs", "execute"])
        or call[:2] == ["run", "deploy"]
        or call[:3] == ["run", "services", "update-traffic"]
    ]


def test_cloud_run_v2_origin_override_requires_the_exact_offline_fixture_sentinel(
    deploy_fixture: dict[str, object],
) -> None:
    """Breaks if a production credential can be redirected by an endpoint argument."""
    result, events = _run_apply(
        deploy_fixture,
        "fresh",
        exact_v2_test_sentinel=False,
    )
    assert result.returncode != 0
    assert not result.stdout.strip()
    assert not events
    assert _TEST_BEARER not in result.stderr
    assert "fixture-etag" not in result.stderr


def test_deploy_processes_have_bounded_redacted_deadlines() -> None:
    source = DEPLOY.read_text(encoding="utf-8")
    assert ".WaitForExit()" not in source
    assert source.count("WaitForExit($TimeoutSeconds * 1000)") >= 2
    assert "failed without exposing command output" in source


def test_public_deploy_never_inherits_or_binds_the_removed_access_code(
    deploy_fixture: dict[str, object],
) -> None:
    result, events = _run_apply(deploy_fixture, "fresh")
    assert result.returncode == 0, result.stdout + result.stderr

    gcloud_events = [event for event in events if event["tool"] == "gcloud"]
    assert gcloud_events
    assert all(event["demo_access_key_present"] is False for event in gcloud_events)
    uv_events = [event for event in events if event["tool"] == "uv"]
    smoke_events = [
        event
        for event in uv_events
        if event["args"][:2] == ["run", "python"]
        and "live-smoke.py" in event["args"][2]
    ]
    assert len(smoke_events) == 2
    assert all(event["demo_access_key_present"] is False for event in uv_events)
    assert all("--public-access" in event["args"] for event in smoke_events)
    service_deploy = next(
        event["args"]
        for event in gcloud_events
        if event["args"][:3] == ["run", "deploy", "hk-movie-rag-demo"]
    )
    secret_bindings = service_deploy[service_deploy.index("--set-secrets") + 1]
    assert secret_bindings == "DB_PASSWORD=hk-rag-db-password:5"


def test_current_sdk_accepts_immutable_storage_preconditions() -> None:
    gcloud = shutil.which("gcloud")
    if gcloud is None:
        pytest.skip("gcloud is unavailable")
    for command in ([gcloud, "storage", "cp", "--help"], [gcloud, "storage", "rsync", "--help"]):
        result = subprocess.run(
            command,
            check=False,
            capture_output=True,
            text=True,
            encoding="utf-8",
            timeout=60,
        )
        assert result.returncode == 0, result.stderr
        assert "--if-generation-match" in result.stdout


def test_current_sdk_supports_nonblocking_job_start_list_and_describe() -> None:
    gcloud = shutil.which("gcloud")
    if gcloud is None:
        pytest.skip("gcloud is unavailable")
    expectations = [
        ([gcloud, "run", "jobs", "execute", "--help"], ["--async", "--wait"]),
        ([gcloud, "run", "jobs", "executions", "list", "--help"], ["--job"]),
        ([gcloud, "run", "jobs", "executions", "describe", "--help"], ["EXECUTION"]),
    ]
    for command, expected in expectations:
        result = subprocess.run(
            command,
            check=False,
            capture_output=True,
            text=True,
            encoding="utf-8",
            timeout=60,
        )
        assert result.returncode == 0, result.stderr
        synopsis = result.stdout.split("DESCRIPTION", 1)[0]
        assert all(flag in synopsis for flag in expected)

    source = DEPLOY.read_text(encoding="utf-8")
    execute_block = source.split("'run', 'jobs', 'execute'", 1)[1].split(")", 1)[0]
    assert "--wait" not in execute_block and "--async" not in execute_block
    assert "'run', 'jobs', 'executions', 'list'" in source
    assert "'run', 'jobs', 'executions', 'describe'" in source


def test_current_sdk_supports_disabling_the_invoker_iam_check() -> None:
    gcloud = shutil.which("gcloud")
    if gcloud is None:
        pytest.skip("gcloud is unavailable")
    result = subprocess.run(
        [gcloud, "run", "deploy", "--help"],
        check=False,
        capture_output=True,
        text=True,
        encoding="utf-8",
        timeout=60,
    )
    assert result.returncode == 0, result.stderr
    assert "--[no-]invoker-iam-check" in result.stdout


def test_service_deploy_disables_invoker_check_without_public_iam_grant() -> None:
    source = DEPLOY.read_text(encoding="utf-8")
    service_block = source.split("'run', 'deploy', $service", 1)[1].split(")", 1)[0]
    assert "--no-invoker-iam-check" in service_block
    assert "--allow-unauthenticated" not in service_block
    assert "allUsers" not in source


def test_r2_deploy_parameters_keep_code_release_and_raw_documents_separate() -> None:
    """Breaks if the reusable deploy silently falls back to repo-local PDFs or config."""
    result = subprocess.run(
        [
            _powershell(),
            "-NoLogo",
            "-NoProfile",
            "-NonInteractive",
            "-File",
            str(DEPLOY),
            "-RagConfigPath",
            str(ROOT / "config" / "rag_demo_r2.yaml"),
            "-DocumentSourceRoot",
            str(ROOT),
            "-ReuseFromReleaseId",
            "v1.2-demo",
            "-PlanOnly",
        ],
        check=False,
        capture_output=True,
        text=True,
        encoding="utf-8",
        timeout=30,
    )

    assert result.returncode == 0, result.stdout + result.stderr
    plan = json.loads(result.stdout)
    assert plan["inputs"] == {
        "document_source_root": str(ROOT),
        "expected_rag_release_id": "v1.2-demo-r2",
        "rag_config_path": str(ROOT / "config" / "rag_demo_r2.yaml"),
        "reuse_from_release_id": "v1.2-demo",
    }
    assert plan["release_authority"] == "verify-rag-bundle JSON"
    assert "release_id" not in plan
    assert "bundle_counts" not in plan


def test_r3_plan_declares_exact_overlay_release_and_r2_reuse_authority() -> None:
    """Breaks if the reusable deploy cannot select R3 without weakening its source release."""
    result = subprocess.run(
        [
            _powershell(),
            "-NoLogo",
            "-NoProfile",
            "-NonInteractive",
            "-File",
            str(DEPLOY),
            "-ExpectedRagReleaseId",
            "v1.2-demo-r3",
            "-RagConfigPath",
            str(ROOT / "config" / "rag_demo_r3.yaml"),
            "-DocumentSourceRoot",
            str(ROOT),
            "-ReuseFromReleaseId",
            "v1.2-demo-r2",
            "-PlanOnly",
        ],
        check=False,
        capture_output=True,
        text=True,
        encoding="utf-8",
        timeout=30,
    )

    assert result.returncode == 0, result.stdout + result.stderr
    plan = json.loads(result.stdout)
    assert plan["inputs"] == {
        "document_source_root": str(ROOT),
        "expected_rag_release_id": "v1.2-demo-r3",
        "rag_config_path": str(ROOT / "config" / "rag_demo_r3.yaml"),
        "reuse_from_release_id": "v1.2-demo-r2",
    }
    assert plan["poster_upload"]["count"] == 4546
    assert "require_4670_reusable_and_10_new_final_state" in plan["steps"]


def test_r2_deploy_rejects_any_non_authoritative_reuse_release_before_mutation(
    deploy_fixture: dict[str, object],
) -> None:
    """The fixed legacy object/claim authority can only describe v1.2-demo."""
    result, events = _run_apply(
        deploy_fixture,
        "fresh",
        reuse_from_release_id="v1.2-demo-copy",
    )

    assert result.returncode != 0
    assert not result.stdout.strip()
    assert "selected immutable deployment contract" in result.stderr
    assert not events


def test_r2_candidate_acceptance_orders_tagged_smoke_cas_and_cleanup() -> None:
    """Breaks if an unsmoked or still-tagged candidate can become the stable service."""
    source = DEPLOY.read_text(encoding="utf-8")
    flow = source[source.index("$serviceDeploymentResult =") :]
    deploy = flow.index("'run', 'deploy', $service")
    candidate_smoke = flow.index("Invoke-LiveSmoke -BaseUrl $candidateTagUrl")
    promote = flow.index("Invoke-CloudRunV2TaggedPromotionCas")
    stable_smoke = flow.index("Invoke-LiveSmoke -BaseUrl $priorServiceUrl")
    cleanup = flow.index("Invoke-CloudRunV2TagCleanupCas")

    assert deploy < candidate_smoke < promote < stable_smoke < cleanup
    deploy_block = flow[deploy : flow.index(")", deploy)]
    assert "'--no-traffic'" in deploy_block
    assert "'--tag', $candidateTag" in deploy_block
    assert "'--revision-suffix', $candidateRevisionSuffix" in deploy_block
    assert "'--public-access'" in source
    assert "'--access-code-env', 'RAG_DEMO_ACCESS_KEY'" not in source
    assert "RAG_DEMO_ACCESS_KEY=$demoSecret" not in source


def test_r2_candidate_tag_respects_cloud_run_combined_name_limit() -> None:
    """Cloud Run rejects service plus traffic-tag names longer than 46 chars."""
    source = DEPLOY.read_text(encoding="utf-8")

    assert '$candidateTag = "a-$($gitSha.Substring(0, 8))-$candidateNonce"' in source
    assert "$service.Length + $candidateTag.Length -gt 46" in source


def test_r2_deploy_persists_the_redacted_live_smoke_report() -> None:
    """A candidate assertion failure must remain diagnosable after safe rollback."""
    source = DEPLOY.read_text(encoding="utf-8")

    assert (
        "$liveSmokeReportDirectory = Join-Path $repoRoot 'artifacts/rag-demo'"
    ) in source
    assert '"live-smoke-$candidateRevisionSuffix-$Phase.json"' in source
    assert "'--output', $script:LiveSmokeReportPath" in source
    assert "function Test-CurrentLiveSmokeFailureReport" in source
    assert "$report.base_url -cne $script:LiveSmokeExpectedBaseUrl" in source
    assert "$report.access_mode -cne 'public_tech_demo'" in source
    assert "redacted_report=$($script:LiveSmokeReportRelativePath)" in source
    assert "Remove-Item -LiteralPath $liveSmokeReportPath" not in source


def test_selected_ingestion_contract_accepts_resume_then_proves_active_noop() -> None:
    """Breaks if R2/R3 deploys stop deriving exact counts from the selected contract."""
    source = DEPLOY.read_text(encoding="utf-8")
    initial = source.index("$initialIngestArguments")
    rerun = source.index("$activeRerunArguments")

    assert initial < rerun
    assert "--require-source-eligible-total,$($deploymentContract.reusable_count)" in source
    assert (
        "--require-non-reusable-embedded-total,"
        "$($deploymentContract.new_embedding_count)"
    ) in source
    assert (
        "--require-embedded,0,--require-skipped,"
        "$($deploymentContract.embedding_count)"
    ) in source
    assert "active_rerun_execution" in source


def test_selected_contract_claims_verified_source_before_any_reuse() -> None:
    """Breaks if R2/R3 asks PostgreSQL to reuse vectors from an unbound row."""
    source = DEPLOY.read_text(encoding="utf-8")
    legacy = source.index("$legacyClaimArguments")
    target = source.index("$initialIngestArguments")

    assert legacy < target
    assert (
        '"gs://$bucket/$($deploymentContract.reuse_manifest_prefix)/'
        'rag_release_manifest.json"'
    ) in source
    assert "$deploymentContract.reuse_manifest_sha256" in source
    assert (
        "--require-skipped,$($deploymentContract.reuse_release_embedding_count)"
    ) in source
    assert "legacy_claim_execution" in source


def test_selected_contract_binds_both_source_objects_before_reuse() -> None:
    """Breaks if a selected manifest can name a replaced or drifted source bundle."""
    source = DEPLOY.read_text(encoding="utf-8")
    legacy_start = source.index("$legacyManifestUri")
    legacy_gate = source[
        legacy_start : source.index("Assert-ImmutableReleaseObject", legacy_start)
    ]

    assert "$deploymentContract.reuse_manifest_prefix" in legacy_gate
    assert "$deploymentContract.reuse_manifest_generation" in legacy_gate
    assert "$deploymentContract.reuse_bundle_sha256" in legacy_gate
    assert "$deploymentContract.reuse_bundle_generation" in legacy_gate
    assert "$deploymentContract.reuse_bundle_size" in legacy_gate
    assert "application/x-ndjson" in legacy_gate


def test_r2_accepts_the_complete_cli_verification_authority() -> None:
    """Breaks if deploy rejects a field emitted by its own bundle verifier."""
    source = DEPLOY.read_text(encoding="utf-8")
    verifier_gate = source[
        source.index("function Assert-ExactBundleContract") : source.index(
            "function Assert-ExactPosterAllowlist"
        )
    ]

    assert "'derived_poster_bytes'" in verifier_gate
    assert "$Verification.derived_poster_bytes -isnot [long]" in verifier_gate


def test_image_tag_and_archive_share_one_full_immutable_commit() -> None:
    source = DEPLOY.read_text(encoding="utf-8")
    assert "'rev-parse', '--verify', 'HEAD^{commit}'" in source
    assert "'^(?:[0-9a-f]{40}|[0-9a-f]{64})$'" in source
    assert "$gitSha = $gitCommit.Substring(0, 12)" in source
    archive_block = source.split("'archive', '--format=zip'", 1)[1].split(")", 1)[0]
    assert "$gitCommit" in archive_block
    assert "'HEAD'" not in archive_block


def test_deploy_rejects_tracked_and_staged_dirt_before_archive(tmp_path: Path) -> None:
    repo = tmp_path / "repo"
    script_dir = repo / "scripts" / "gcp"
    script_dir.mkdir(parents=True)
    shutil.copy2(DEPLOY, script_dir / DEPLOY.name)
    (repo / "config").mkdir()
    config = repo / "config" / "rag_demo_r2.yaml"
    config.write_text("fixture: clean\n", encoding="utf-8")
    _git(repo, "init")
    _git(repo, "config", "user.email", "tests@example.invalid")
    _git(repo, "config", "user.name", "tests")
    _git(repo, "add", ".")
    _git(repo, "commit", "-m", "fixture")
    source = tmp_path / "source"
    source.mkdir()
    fake_bin = tmp_path / "bin"
    fake_bin.mkdir()
    marker = tmp_path / "gcloud-called"
    (fake_bin / "gcloud.ps1").write_text(
        f'Set-Content -LiteralPath "{marker}" -Value called\nexit 97\n', encoding="utf-8"
    )
    env = os.environ.copy()
    env["PATH"] = f"{fake_bin}{os.pathsep}{env['PATH']}"

    def run() -> subprocess.CompletedProcess[str]:
        return subprocess.run(
            [
                _powershell(),
                "-NoLogo",
                "-NoProfile",
                "-NonInteractive",
                "-File",
                str(script_dir / DEPLOY.name),
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

    config.write_text("fixture: tracked-dirty\n", encoding="utf-8")
    tracked = run()
    assert tracked.returncode != 0
    assert "clean worktree" in tracked.stderr
    assert not marker.exists()

    _git(repo, "add", str(config.relative_to(repo)))
    staged = run()
    assert staged.returncode != 0
    assert "clean worktree" in staged.stderr
    assert not marker.exists()


def test_deploy_fails_before_gcp_when_head_advances_after_capture(
    deploy_fixture: dict[str, object],
) -> None:
    result, events = _run_apply(deploy_fixture, "head-advance")
    assert result.returncode != 0
    assert "source commit changed" in result.stderr
    assert not any(event["tool"] == "gcloud" for event in events)


def test_full_poster_gate_rejects_tampered_bytes_before_gcp(
    deploy_fixture: dict[str, object],
) -> None:
    poster = (
        Path(deploy_fixture["source"])
        / "assets"
        / "posters"
        / "derived"
        / "T00000.webp"
    )
    original = poster.read_bytes()
    poster.write_bytes(bytes([original[0] ^ 1]) + original[1:])
    try:
        result, events = _run_apply(
            deploy_fixture,
            "local-poster-tamper",
            validate_full_poster_gate=True,
        )
    finally:
        poster.write_bytes(original)

    assert result.returncode != 0
    assert "derived poster bytes changed" in result.stderr
    assert not any(event["tool"] == "gcloud" for event in events)


def test_full_poster_gate_rejects_missing_and_extra_files_before_gcp(
    deploy_fixture: dict[str, object],
) -> None:
    derived = Path(deploy_fixture["source"]) / "assets" / "posters" / "derived"
    poster = derived / "T00000.webp"
    held = derived.parent / "T00000.webp.held"
    poster.replace(held)
    try:
        missing_result, missing_events = _run_apply(
            deploy_fixture,
            "local-poster-missing",
            validate_full_poster_gate=True,
        )
    finally:
        held.replace(poster)

    extra = derived / "rogue.webp"
    extra.write_bytes(b"RIFFrogueWEBPfixture")
    try:
        extra_result, extra_events = _run_apply(
            deploy_fixture,
            "local-poster-extra",
            validate_full_poster_gate=True,
        )
    finally:
        extra.unlink()

    assert missing_result.returncode != 0
    assert "selected exact file allowlist" in missing_result.stderr
    assert not any(event["tool"] == "gcloud" for event in missing_events)
    assert extra_result.returncode != 0
    assert "selected exact file allowlist" in extra_result.stderr
    assert not any(event["tool"] == "gcloud" for event in extra_events)


@pytest.mark.parametrize("scenario", ["fresh", "existing", "legacy-metadata"])
def test_full_fake_deploy_apply_is_immutable_ordered_and_redacted(
    deploy_fixture: dict[str, object], scenario: str
) -> None:
    result, events = _run_apply(
        deploy_fixture,
        scenario,
        validate_full_poster_gate=scenario == "fresh",
    )
    assert result.returncode == 0, result.stdout + result.stderr
    report = json.loads(result.stdout)
    assert report["poster_object_count"] == 4_545
    assert "access_code" not in report
    assert report["legacy_claim_execution"] == "hk-movie-rag-ingest-abcde"
    assert report["job_execution"] == "hk-movie-rag-ingest-fghij"
    assert report["active_rerun_execution"] == "hk-movie-rag-ingest-klmno"
    assert report["poster_verify_execution"] == "hk-movie-rag-poster-verify-abcde"
    assert report["candidate_smoke_passed"] is True
    assert report["stable_smoke_passed"] is True
    assert report["candidate_tag_removed"] is True
    assert "secret" not in result.stderr.lower()

    gcloud_calls = [event["args"] for event in events if event["tool"] == "gcloud"]
    gcloud_events = [event for event in events if event["tool"] == "gcloud"]
    assert all(set(event["ambient_google"].values()) == {None} for event in gcloud_events)
    assert all(event["test_mode"] == "offline-fake-gcloud" for event in gcloud_events)
    cp_calls = [call for call in gcloud_calls if call[:2] == ["storage", "cp"]]
    assert len(cp_calls) == (2 if scenario == "fresh" else 0)
    assert all("--if-generation-match=0" in call for call in cp_calls)
    assert all("/rag/v1.2-demo-r2/" in call[3] for call in cp_calls)
    rsync = next(call for call in gcloud_calls if call[:2] == ["storage", "rsync"])
    assert "--checksums-only" in rsync
    assert "--no-clobber" in rsync
    assert "--if-generation-match=0" in rsync
    assert not any(call[:3] == ["storage", "objects", "update"] for call in gcloud_calls)
    assert not any("delete" in call for call in gcloud_calls)
    service_deploy = next(
        call for call in gcloud_calls if call[:3] == ["run", "deploy", "hk-movie-rag-demo"]
    )
    assert "--no-invoker-iam-check" in service_deploy
    assert "--allow-unauthenticated" not in service_deploy
    assert all("allUsers" not in argument for call in gcloud_calls for argument in call)
    build_call = next(call for call in gcloud_calls if call[:2] == ["builds", "submit"])
    build_context = Path(build_call[2])
    assert build_context != Path(deploy_fixture["script"]).parents[2]
    assert not build_context.exists()
    execute_calls = [call for call in gcloud_calls if call[:3] == ["run", "jobs", "execute"]]
    assert [call[3] for call in execute_calls] == [
        "hk-movie-rag-ingest",
        "hk-movie-rag-ingest",
        "hk-movie-rag-ingest",
        "hk-movie-rag-poster-verify",
    ]
    for execute_call in execute_calls:
        job_name = execute_call[3]
        execution_list_index = next(
            index
            for index, call in enumerate(gcloud_calls)
            if call[:4] == ["run", "jobs", "executions", "list"]
            and f"--job={job_name}" in call
        )
        execution_start_index = gcloud_calls.index(execute_call)
        assert execution_list_index < execution_start_index
        assert "--wait" not in execute_call and "--async" not in execute_call
    describe_calls = [
        call for call in gcloud_calls if call[:4] == ["run", "jobs", "executions", "describe"]
    ]
    assert len(describe_calls) == 8
    assert {call[4] for call in describe_calls} == {
        "hk-movie-rag-ingest-abcde",
        "hk-movie-rag-ingest-fghij",
        "hk-movie-rag-ingest-klmno",
        "hk-movie-rag-poster-verify-abcde",
    }

    first_gcloud = next(index for index, event in enumerate(events) if event["tool"] == "gcloud")
    assert [event["args"][2] for event in events[:first_gcloud]] == [
        "verify-release",
        "build-rag-bundle",
        "verify-rag-bundle",
    ]
    prefixes = [tuple(call[:3]) for call in gcloud_calls]
    assert prefixes.index(("storage", "buckets", "describe")) < prefixes.index(
        ("storage", "rsync", str(deploy_fixture["source"]) + "\\assets\\posters\\derived")
    )
    assert prefixes.index(("builds", "submit", str(build_context))) < prefixes.index(
        ("run", "jobs", "deploy")
    )
    verifier_execute_index = next(
        index
        for index, call in enumerate(gcloud_calls)
        if call[:4] == ["run", "jobs", "execute", "hk-movie-rag-poster-verify"]
    )
    assert verifier_execute_index < prefixes.index(
        ("run", "deploy", "hk-movie-rag-demo")
    )
    service_describes = [
        index
        for index, call in enumerate(gcloud_calls)
        if call[:4] == ["run", "services", "describe", "hk-movie-rag-demo"]
    ]
    assert len(service_describes) == 3
    assert service_describes[0] < prefixes.index(
        ("storage", "rsync", str(deploy_fixture["source"]) + "\\assets\\posters\\derived")
    )
    assert prefixes.index(("run", "deploy", "hk-movie-rag-demo")) < service_describes[1]


def test_deploy_validates_candidate_before_exact_revision_promotion(
    deploy_fixture: dict[str, object],
) -> None:
    """Breaks if any floating/latest or pre-validation promotion path returns."""
    result, events = _run_apply(deploy_fixture, "fresh")
    assert result.returncode == 0, result.stdout + result.stderr
    report = json.loads(result.stdout)
    assert report["revision"] == "hk-movie-rag-demo-00006-new"

    calls = _gcloud_calls(events)
    deploy_index = next(
        index
        for index, call in enumerate(calls)
        if call[:3] == ["run", "deploy", "hk-movie-rag-demo"]
    )
    deploy_call = calls[deploy_index]
    assert "--no-traffic" in deploy_call
    assert "--to-latest" not in deploy_call

    assert not _traffic_calls(calls)
    assert not any("--to-latest" in call for call in calls)
    patches = _v2_patches(events)
    assert len(patches) == 2
    assert [patch["target_revision"] for patch in patches] == [
        _NEW_REVISION,
        _NEW_REVISION,
    ]
    assert [patch["tagged"] for patch in patches] == [True, False]
    assert all(patch["etag_matched"] is True for patch in patches)
    assert all(patch["request_valid"] is True for patch in patches)
    assert all(patch["accepted"] is True for patch in patches)
    v2 = _v2_events(events)
    v2_get_resources = [
        event.get("resource") for event in v2 if event.get("method") == "GET"
    ]
    assert v2_get_resources[0] == "service"
    assert v2_get_resources[-1] == "service"
    assert v2_get_resources.count("operation") >= 2
    serialized_events = json.dumps(events)
    assert _TEST_BEARER not in serialized_events
    assert "fixture-etag" not in serialized_events
    assert _TEST_BEARER not in result.stdout + result.stderr
    assert "fixture-etag" not in result.stdout + result.stderr
    assert not any(call[:3] == ["auth", "print-access-token"] for call in calls)

    service_describes = [
        index
        for index, call in enumerate(calls)
        if call[:4] == ["run", "services", "describe", "hk-movie-rag-demo"]
    ]
    assert len(service_describes) == 3
    first_mutation = calls.index(_mutating_calls(calls)[0])
    revision_index = next(
        index
        for index, call in enumerate(calls)
        if call[:4]
        == ["run", "revisions", "describe", "hk-movie-rag-demo-00006-new"]
    )
    assert service_describes[0] < first_mutation
    assert deploy_index < service_describes[1] < revision_index < service_describes[2]

    version_lists = [call for call in calls if call[:3] == ["secrets", "versions", "list"]]
    assert [call[3] for call in version_lists] == [
        "hk-rag-db-password",
        "hk-rag-db-password",
        "hk-rag-db-password",
    ]

    job_deploys = [call for call in calls if call[:3] == ["run", "jobs", "deploy"]]
    service_deploy = next(
        call for call in calls if call[:3] == ["run", "deploy", "hk-movie-rag-demo"]
    )
    secret_bindings = [
        call[call.index("--set-secrets") + 1] for call in [*job_deploys, service_deploy]
    ]
    assert secret_bindings == [
        "DB_PASSWORD=hk-rag-db-password:5",
        "DB_PASSWORD=hk-rag-db-password:5",
        "DB_PASSWORD=hk-rag-db-password:5",
        "DB_PASSWORD=hk-rag-db-password:5",
        "DB_PASSWORD=hk-rag-db-password:5",
    ]
    assert not any("latest" in binding.lower() for binding in secret_bindings)
    service_env = service_deploy[service_deploy.index("--set-env-vars") + 1]
    assert (
        "RAG_RELEVANCE_POLICY_SHA256="
        "a4ecce727a469689a2a08c6d42e0d7d1eb975761173e3b6e4d1eaf34db7b2aba"
    ) in service_env.split(",")
    assert (
        "RAG_POSTER_AUTHORITY_SHA256="
        "9e56adda2a1fdfa648869c53d7e60a085db5d1c6d11db90dbec13bfe12f0eeed"
    ) in service_env.split(",")
    assert ("RAG_IMAGE_DIGEST=sha256:" + "a" * 64) in service_env.split(",")


@pytest.mark.parametrize(
    "scenario",
    [
        "candidate-ready-is-candidate",
        "candidate-v1-zero-omitted",
        "preexisting-no-traffic-candidate",
        "preexisting-candidate-is-ready",
        "v2-observed-uri-present",
    ],
)
def test_deploy_accepts_both_no_traffic_ready_shapes_and_safe_candidate_reuse(
    deploy_fixture: dict[str, object], scenario: str
) -> None:
    result, events = _run_apply(deploy_fixture, scenario)
    assert result.returncode == 0, result.stdout + result.stderr
    assert json.loads(result.stdout)["revision"] == _NEW_REVISION
    calls = _gcloud_calls(events)
    deploy_call = next(
        call for call in calls if call[:3] == ["run", "deploy", "hk-movie-rag-demo"]
    )
    assert "--no-traffic" in deploy_call
    assert deploy_call[deploy_call.index("--revision-suffix") + 1] == "00006-new"
    assert any(
        call[:4] == ["run", "revisions", "describe", _NEW_REVISION]
        for call in calls
    )
    assert [patch["target_revision"] for patch in _v2_patches(events)] == [
        _NEW_REVISION,
        _NEW_REVISION,
    ]


@pytest.mark.parametrize(
    "scenario",
    [
        "v2-candidate-desired-zero-omitted",
        "v2-candidate-observed-zero-omitted",
    ],
)
def test_deploy_accepts_an_omitted_default_zero_candidate_percent(
    deploy_fixture: dict[str, object], scenario: str
) -> None:
    """Cloud Run v2 may omit its proto-default zero in desired or observed traffic."""
    result, events = _run_apply(deploy_fixture, scenario)

    assert result.returncode == 0, result.stdout + result.stderr
    assert json.loads(result.stdout)["revision"] == _NEW_REVISION
    assert [patch["target_revision"] for patch in _v2_patches(events)] == [
        _NEW_REVISION,
        _NEW_REVISION,
    ]


@pytest.mark.parametrize(
    "scenario",
    [
        "v2-candidate-desired-zero-null",
        "v2-candidate-observed-zero-null",
        "v2-candidate-desired-zero-string",
        "v2-candidate-desired-zero-bool",
        "v2-candidate-desired-zero-nonzero",
    ],
)
def test_deploy_rejects_present_noncanonical_zero_candidate_percent(
    deploy_fixture: dict[str, object], scenario: str
) -> None:
    """Only field absence carries the proto default; present values remain exact."""
    result, events = _run_apply(deploy_fixture, scenario)

    assert result.returncode != 0
    assert not result.stdout.strip()
    assert not any(
        patch["target_revision"] == _NEW_REVISION for patch in _v2_patches(events)
    )


@pytest.mark.parametrize(
    "scenario",
    [
        "prior-traffic-missing",
        "prior-traffic-split",
        "prior-traffic-tagged",
        "prior-traffic-non-100",
        "prior-traffic-floating",
        "prior-traffic-latest-string",
        "prior-traffic-string-percent",
        "prior-traffic-object",
        "prior-latest-created-outside-service",
        "prior-latest-ready-third",
        "pre-service-annotation-false",
        "pre-service-annotation-missing",
        "pre-service-annotation-bool",
        "pre-service-stale-generation",
        "pre-service-not-ready",
        "pre-service-ready-bool",
        "pre-service-conditions-object",
        "pre-service-array-singleton",
        "pre-service-scalar",
        "pre-service-name-mismatch",
        "pre-service-url-path-spoof",
    ],
)
def test_deploy_rejects_malformed_prior_traffic_before_every_mutation(
    deploy_fixture: dict[str, object], scenario: str
) -> None:
    """Breaks if prior traffic is read late or accepts a non-exact pinned revision."""
    result, events = _run_apply(deploy_fixture, scenario)
    assert result.returncode != 0
    assert not result.stdout.strip()
    calls = _gcloud_calls(events)
    assert not _mutating_calls(calls)
    assert len(
        [
            call
            for call in calls
            if call[:4] == ["run", "services", "describe", "hk-movie-rag-demo"]
        ]
    ) == 1


@pytest.mark.parametrize(
    "scenario",
    [
        "candidate-latest-mismatch",
        "candidate-prior-traffic-changed",
        "candidate-prior-traffic-floating",
        "candidate-v1-zero-null",
        "candidate-v1-zero-string",
        "candidate-v1-zero-bool",
        "candidate-v1-zero-nonzero",
        "candidate-service-stale-generation",
        "candidate-service-not-ready",
        "candidate-service-array-singleton",
        "candidate-service-name-mismatch",
        "deploy-candidate-equals-prior",
        "deploy-candidate-outside-service",
        "deploy-candidate-readback-mismatch",
        "deploy-result-array-singleton",
        "deploy-result-scalar",
        "deploy-result-null",
        "deploy-result-stale-generation",
        "deploy-result-not-ready",
        "deploy-result-ready-bool",
        "deploy-result-conditions-object",
        "deploy-result-annotation-bool",
        "service-annotation-false",
        "service-annotation-missing",
        "service-url-invalid",
        "revision-name-mismatch",
        "revision-not-ready",
        "revision-image-mismatch",
        "revision-service-account-mismatch",
        "revision-cloudsql-mismatch",
        "revision-env-mismatch",
        "revision-image-digest-env-mismatch",
        "revision-secret-mismatch",
        "revision-concurrency-mismatch",
        "revision-timeout-mismatch",
        "revision-port-mismatch",
        "revision-port-name-mismatch",
        "revision-cpu-mismatch",
        "revision-memory-mismatch",
        "revision-max-scale-mismatch",
        "revision-min-scale-present",
        "revision-secret-alias-present",
        "revision-secret-alias-null",
        "revision-ready-bool",
        "revision-image-bool",
        "revision-service-account-bool",
        "revision-cloudsql-bool",
        "revision-max-scale-bool",
        "revision-cpu-bool",
        "revision-memory-bool",
        "revision-port-name-bool",
        "revision-plain-env-bool",
        "revision-secret-name-bool",
        "revision-secret-key-number",
        "revision-plain-env-extra-property",
        "revision-secret-entry-extra-property",
        "revision-valuefrom-extra-property",
        "revision-secretref-extra-property",
        "revision-resources-extra-property",
        "revision-port-extra-property",
        "revision-conditions-object",
        "revision-containers-object",
        "revision-ports-object",
        "revision-command-present",
        "revision-args-present",
        "revision-volume-present",
        "revision-volume-mount-present",
        "revision-stale-generation",
        "revision-array-singleton",
        "revision-scalar",
    ],
)
def test_deploy_rejects_every_candidate_config_drift_before_promotion(
    deploy_fixture: dict[str, object], scenario: str
) -> None:
    """Breaks if candidate validation is incomplete or occurs after a traffic command."""
    result, events = _run_apply(deploy_fixture, scenario)
    assert result.returncode != 0
    assert not result.stdout.strip()
    calls = _gcloud_calls(events)
    service_deploy = next(
        call for call in calls if call[:3] == ["run", "deploy", "hk-movie-rag-demo"]
    )
    assert "--no-traffic" in service_deploy
    assert not _traffic_calls(calls)
    patches = _v2_patches(events)
    assert len(patches) == 1
    assert patches[0]["target_revision"] == _OLD_REVISION
    assert patches[0]["tagged"] is False
    assert patches[0]["accepted"] is True


def test_deploy_rejects_drifted_preexisting_candidate_before_promotion(
    deploy_fixture: dict[str, object],
) -> None:
    """Safe reuse still validates the exact captured candidate before promotion."""
    result, events = _run_apply(
        deploy_fixture, "preexisting-no-traffic-candidate-spec-drift"
    )
    assert result.returncode != 0
    assert not result.stdout.strip()
    calls = _gcloud_calls(events)
    assert any(
        call[:4]
        == ["run", "revisions", "describe", "hk-movie-rag-demo-00006-new"]
        for call in calls
    )
    assert not _traffic_calls(calls)
    assert not _v2_patches(events)


def test_deploy_nonzero_after_server_side_tag_is_conditionally_cleaned(
    deploy_fixture: dict[str, object],
) -> None:
    """A CLI failure after server mutation must not strand the owned public tag."""
    result, events = _run_apply(deploy_fixture, "deploy-nonzero-after-tag")

    assert result.returncode != 0
    assert not result.stdout.strip()
    assert "LEAK-ME" not in result.stderr
    patches = _v2_patches(events)
    assert len(patches) == 1
    assert patches[0]["target_revision"] == _OLD_REVISION
    assert patches[0]["tagged"] is False
    assert patches[0]["etag_matched"] is True
    assert patches[0]["accepted"] is True


def test_deploy_timeout_after_server_side_tag_is_conditionally_cleaned(
    deploy_fixture: dict[str, object],
) -> None:
    """A timed-out local CLI must still discover and remove its exact server tag."""
    result, events = _run_apply(deploy_fixture, "deploy-timeout-after-tag")

    assert result.returncode != 0
    assert not result.stdout.strip()
    patches = _v2_patches(events)
    assert len(patches) == 1
    assert patches[0]["target_revision"] == _OLD_REVISION
    assert patches[0]["etag_matched"] is True
    assert patches[0]["accepted"] is True


def test_delayed_candidate_after_cleaned_prior_candidate_is_owned_and_accepted(
    deploy_fixture: dict[str, object],
) -> None:
    """The captured newer latest-created revision is a bounded pending state."""
    result, events = _run_apply(
        deploy_fixture,
        "prior-clean-newer-created-delayed-visibility",
    )

    assert result.returncode == 0, result.stdout + result.stderr
    assert [patch["target_revision"] for patch in _v2_patches(events)] == [
        _NEW_REVISION,
        _NEW_REVISION,
    ]


def test_concurrent_same_tag_different_revision_is_never_claimed_or_patched(
    deploy_fixture: dict[str, object],
) -> None:
    """A matching tag is not ownership without this apply's exact revision name."""
    result, events = _run_apply(
        deploy_fixture,
        "concurrent-same-tag-different-revision",
    )

    assert result.returncode != 0
    assert not result.stdout.strip()
    assert not _v2_patches(events)
    calls = _gcloud_calls(events)
    deploy_call = next(
        call for call in calls if call[:3] == ["run", "deploy", "hk-movie-rag-demo"]
    )
    assert deploy_call[deploy_call.index("--revision-suffix") + 1] == "00006-new"


@pytest.mark.parametrize(
    "scenario,expected_v2_reads",
    [
        ("preexisting-no-traffic-candidate-v1-traffic-race", 2),
        ("preexisting-no-traffic-candidate-v2-traffic-race", 2),
        ("preexisting-no-traffic-candidate-etag-race", 2),
    ],
)
def test_deploy_reuse_rejects_v1_v2_traffic_or_etag_races(
    deploy_fixture: dict[str, object], scenario: str, expected_v2_reads: int
) -> None:
    result, events = _run_apply(deploy_fixture, scenario)
    assert result.returncode != 0
    assert not result.stdout.strip()
    assert not _traffic_calls(_gcloud_calls(events))
    assert not _v2_patches(events)
    service_reads = [
        event
        for event in _v2_events(events)
        if event.get("method") == "GET" and event.get("resource") == "service"
    ]
    assert len(service_reads) == expected_v2_reads


@pytest.mark.parametrize(
    "scenario",
    [
        "v2-pre-etag-missing",
        "v2-pre-etag-bool",
        "v2-pre-generation-mismatch",
        "v2-pre-reconciling",
        "v2-pre-reconciling-string",
        "v2-pre-terminal-failed",
        "v2-pre-traffic-object",
        "v2-pre-status-object",
        "v2-pre-string-percent",
        "v2-pre-floating",
        "v2-pre-tagged",
        "v2-pre-status-mismatch",
        "v2-pre-latest-mismatch",
        "v2-pre-latest-ready-mismatch",
        "v2-pre-latest-created-mismatch",
        "v2-pre-ready-changed-after-v1",
        "v2-pre-status-tagged",
        "v2-pre-status-uri-mismatch",
        "v2-pre-url-mismatch",
    ],
)
def test_deploy_rejects_malformed_v2_cas_snapshot_without_any_traffic_patch(
    deploy_fixture: dict[str, object], scenario: str
) -> None:
    """Breaks if an ambiguous desired/observed snapshot can authorize promotion."""
    result, events = _run_apply(deploy_fixture, scenario)
    assert result.returncode != 0
    assert not result.stdout.strip()
    assert not _traffic_calls(_gcloud_calls(events))
    assert not _v2_patches(events)
    service_reads = [
        event
        for event in _v2_events(events)
        if event.get("method") == "GET" and event.get("resource") == "service"
    ]
    assert len(service_reads) == 1


@pytest.mark.parametrize(
    "scenario",
    [
        "promotion-failure",
        "v2-promotion-conflict",
        "v2-promotion-redirect",
        "v2-promotion-operation-failure",
        "v2-promotion-operation-done-string",
        "v2-post-readback-etag-missing",
        "v2-promotion-same-state-etag-race",
        "v2-post-validation-same-state-etag-race",
    ],
)
def test_deploy_never_rolls_back_an_unowned_or_uncertain_promotion(
    deploy_fixture: dict[str, object], scenario: str
) -> None:
    """An uncertain promotion may only clean an unchanged owned zero-traffic tag."""
    result, events = _run_apply(deploy_fixture, scenario)
    assert result.returncode != 0
    assert not result.stdout.strip()
    assert not _traffic_calls(_gcloud_calls(events))
    patches = _v2_patches(events)
    assert patches[0]["target_revision"] == _NEW_REVISION
    if len(patches) == 2:
        assert patches[1]["target_revision"] == _OLD_REVISION
        assert patches[1]["tagged"] is False
        assert patches[1]["etag_matched"] is True
        assert patches[1]["accepted"] is True
        expected_status = "candidate_tag_removed"
    else:
        assert len(patches) == 1
        expected_status = "rollback_skipped_unowned"
    _assert_redacted_failure_status(result, expected_status)
    assert "rollback_verified" not in result.stderr
    assert "rollback_failed_or_unverified" not in result.stderr
    combined = result.stdout + result.stderr
    for forbidden in (
        _TEST_BEARER,
        "fixture-etag-prior",
        "fixture-etag-candidate",
        "LEAK-ME-http-body",
        "LEAK-ME-operation",
    ):
        assert forbidden not in combined


def test_deploy_times_out_promotion_and_only_cleans_the_exact_unchanged_tag(
    deploy_fixture: dict[str, object],
) -> None:
    """A timed-out LRO may clean only the still-owned unchanged zero-traffic tag."""
    result, events = _run_apply(deploy_fixture, "v2-promotion-operation-timeout")
    assert result.returncode != 0
    assert not result.stdout.strip()
    patches = _v2_patches(events)
    assert [patch["target_revision"] for patch in patches] == [
        _NEW_REVISION,
        _OLD_REVISION,
    ]
    assert patches[1]["tagged"] is False
    assert patches[1]["etag_matched"] is True
    assert patches[1]["accepted"] is True
    _assert_redacted_failure_status(result, "candidate_tag_removed")
    assert _TEST_BEARER not in result.stderr
    assert "fixture-etag" not in result.stderr


@pytest.mark.parametrize(
    ("scenario", "expected_targets", "expected_status"),
    (
        (
            "stable-smoke-failure",
            (_NEW_REVISION, _OLD_REVISION, _OLD_REVISION),
            "rollback_and_tag_cleanup_verified",
        ),
        (
            "preexisting-no-traffic-candidate-stable-smoke-failure",
            (_NEW_REVISION, _OLD_REVISION),
            "rollback_with_preexisting_tag_verified",
        ),
    ),
)
def test_stable_smoke_failure_restores_candidate_tag_ownership_boundary(
    deploy_fixture: dict[str, object],
    scenario: str,
    expected_targets: tuple[str, ...],
    expected_status: str,
) -> None:
    """A rollback may remove only a candidate tag created by this apply."""
    result, events = _run_apply(deploy_fixture, scenario)

    assert result.returncode != 0
    assert not result.stdout.strip()
    patches = _v2_patches(events)
    assert tuple(patch["target_revision"] for patch in patches) == expected_targets
    assert patches[0]["tagged"] is True
    assert patches[1]["tagged"] is True
    assert patches[1]["traffic_count"] == 2
    if scenario == "stable-smoke-failure":
        assert patches[2]["tagged"] is False
        assert patches[2]["traffic_count"] == 1
    _assert_redacted_failure_status(result, expected_status)
    assert "failure_stage=stable_smoke" in _plain_stderr(result)
    assert _TEST_BEARER not in result.stderr
    assert "fixture-etag" not in result.stderr
    assert "LEAK-ME" not in result.stderr


def test_post_cleanup_failure_restores_preexisting_candidate_tag(
    deploy_fixture: dict[str, object],
) -> None:
    """A clean-state rollback must restore a candidate tag that predated this apply."""
    result, events = _run_apply(
        deploy_fixture, "preexisting-no-traffic-candidate-post-state-mismatch"
    )

    assert result.returncode != 0
    assert not result.stdout.strip()
    patches = _v2_patches(events)
    assert [patch["target_revision"] for patch in patches] == [
        _NEW_REVISION,
        _NEW_REVISION,
        _OLD_REVISION,
    ]
    assert patches[0]["tagged"] is True
    assert patches[0]["traffic_count"] == 1
    assert patches[1]["tagged"] is False
    assert patches[1]["traffic_count"] == 1
    assert patches[2]["tagged"] is True
    assert patches[2]["traffic_count"] == 2
    _assert_redacted_failure_status(
        result, "post_cleanup_rollback_with_preexisting_tag_verified"
    )
    assert "failure_stage=post_cleanup_validation" in _plain_stderr(result)
    assert _TEST_BEARER not in result.stderr
    assert "fixture-etag" not in result.stderr
    assert "LEAK-ME" not in result.stderr


@pytest.mark.parametrize(
    "scenario",
    [
        "post-state-mismatch",
        "post-state-mismatch-rollback-ready-prior",
        "post-state-mismatch-rollback-ready-third",
        "post-url-mismatch",
        "post-traffic-floating",
        "post-service-stale-generation",
        "post-service-not-ready",
        "post-service-array-singleton",
        "post-revision-config-mismatch",
        "rollback-failure",
        "rollback-verification-mismatch",
        "rollback-url-mismatch",
        "rollback-service-not-ready",
        "v2-concurrent-before-rollback",
        "v2-concurrent-rollback-race",
        "v2-rollback-same-state-etag-race",
        "v2-rollback-post-validation-same-state-etag-race",
    ],
)
def test_deploy_rolls_back_once_only_when_the_promoted_etag_is_still_owned(
    deploy_fixture: dict[str, object], scenario: str
) -> None:
    """Breaks if rollback is unconditional, retried, or can overwrite a concurrent owner."""
    result, events = _run_apply(deploy_fixture, scenario)
    assert result.returncode != 0
    assert not result.stdout.strip()
    calls = _gcloud_calls(events)
    assert not _traffic_calls(calls)
    patches = _v2_patches(events)
    if scenario == "v2-concurrent-before-rollback":
        assert [patch["target_revision"] for patch in patches] == [_NEW_REVISION]
    else:
        assert [patch["target_revision"] for patch in patches] == [
            _NEW_REVISION,
            _NEW_REVISION,
            _OLD_REVISION,
        ]
    assert "hk-rag-db-password" not in result.stderr
    assert "hk-rag-demo-key" not in result.stderr
    rollback_unverified = {
        "rollback-failure",
        "rollback-verification-mismatch",
        "rollback-url-mismatch",
        "rollback-service-not-ready",
        "v2-concurrent-rollback-race",
        "v2-rollback-same-state-etag-race",
        "post-state-mismatch-rollback-ready-third",
        "v2-rollback-post-validation-same-state-etag-race",
    }
    if scenario == "v2-concurrent-before-rollback" or scenario in rollback_unverified:
        expected_status = "rollback_skipped_unowned"
        unexpected_statuses = {
            "rollback_and_tag_cleanup_verified",
            "post_cleanup_rollback_verified",
        }
    else:
        expected_status = "post_cleanup_rollback_verified"
        unexpected_statuses = {
            "rollback_and_tag_cleanup_verified",
            "rollback_skipped_unowned",
        }
    _assert_redacted_failure_status(result, expected_status)
    assert all(status not in result.stderr for status in unexpected_statuses)
    assert _TEST_BEARER not in result.stderr
    assert "fixture-etag" not in result.stderr
    assert "LEAK-ME" not in result.stderr
    service_describes = [
        call
        for call in calls
        if call[:4] == ["run", "services", "describe", "hk-movie-rag-demo"]
    ]
    expected_describes = 2 if scenario == "v2-concurrent-before-rollback" else 3
    assert len(service_describes) == expected_describes


def test_colon_bearing_cloud_build_and_cloud_run_values_use_separate_windows_argv(
    deploy_fixture: dict[str, object],
) -> None:
    result, events = _run_apply(deploy_fixture, "windows-colon-argv")
    assert result.returncode == 0, result.stdout + result.stderr

    repo = Path(deploy_fixture["script"]).parents[2]
    commit = subprocess.run(
        ["git", "rev-parse", "--verify", "HEAD^{commit}"],
        cwd=repo,
        check=True,
        capture_output=True,
        text=True,
        encoding="utf-8",
        timeout=30,
    ).stdout.strip()
    expected_image = "us-central1-docker.pkg.dev/motionexpaiweb/hk-movie-rag/demo:" + commit[:12]
    immutable_image = (
        "us-central1-docker.pkg.dev/motionexpaiweb/hk-movie-rag/demo@sha256:" + "a" * 64
    )
    manifest_sha = hashlib.sha256(
        (Path(deploy_fixture["bundle_dir"]) / "rag_release_manifest.json").read_bytes()
    ).hexdigest()
    gcloud_calls = [event["args"] for event in events if event["tool"] == "gcloud"]
    build_call = next(call for call in gcloud_calls if call[:2] == ["builds", "submit"])
    job_call = next(
        call
        for call in gcloud_calls
        if call[:4] == ["run", "jobs", "deploy", "hk-movie-rag-ingest"]
    )
    verifier_job_call = next(
        call
        for call in gcloud_calls
        if call[:4] == ["run", "jobs", "deploy", "hk-movie-rag-poster-verify"]
    )
    service_call = next(
        call for call in gcloud_calls if call[:3] == ["run", "deploy", "hk-movie-rag-demo"]
    )
    connection = "motionexpaiweb:us-central1:hk-movie-rag-pg"
    expected_job_values = {
        "--image": immutable_image,
        "--set-cloudsql-instances": connection,
        "--set-env-vars": (
            "DB_NAME=hk_movie_rag,DB_USER=hk_rag_app,"
            f"INSTANCE_CONNECTION_NAME={connection},"
            "RAG_GCP_PROJECT_ID=motionexpaiweb,RAG_VERTEX_LOCATION=global"
        ),
        "--set-secrets": "DB_PASSWORD=hk-rag-db-password:5",
        "--args": (
            "ingest-rag-bundle,gs://motionexpaiweb-880586285913-hk-movie-rag-release/"
            "rag/v1.2-demo-mvp1/rag_release_manifest.json,"
            "--require-existing-active,--require-embedded,0,--require-skipped,4664"
        ),
    }
    expected_verifier_job_values = {
        "--image": immutable_image,
        "--set-cloudsql-instances": connection,
        "--set-env-vars": (
            "DB_NAME=hk_movie_rag,DB_USER=hk_rag_app,"
            f"INSTANCE_CONNECTION_NAME={connection},"
            "GOOGLE_CLOUD_PROJECT=motionexpaiweb"
        ),
        "--set-secrets": "DB_PASSWORD=hk-rag-db-password:5",
        "--args": (
            "verify-poster-fleet,--manifest,gs://"
            "motionexpaiweb-880586285913-hk-movie-rag-release/"
            "rag/v1.2-demo-r2/rag_release_manifest.json,--bucket,"
            "motionexpaiweb-880586285913-hk-movie-rag-release,--workers,16"
        ),
    }
    expected_service_values = {
        "--image": immutable_image,
        "--set-cloudsql-instances": connection,
        "--revision-suffix": "00006-new",
        "--set-env-vars": (
            "GOOGLE_CLOUD_PROJECT=motionexpaiweb,VERTEX_LOCATION=global,"
            "RAG_RELEASE_ID=v1.2-demo-r2,"
            f"RAG_RELEASE_MANIFEST_SHA256={manifest_sha},"
            "RAG_EMBEDDING_MODEL=gemini-embedding-2,"
            "RAG_EMBEDDING_DIMENSION=768,RAG_GENERATION_MODEL=gemini-3.5-flash-lite,"
            "RAG_RELEASE_BUCKET=motionexpaiweb-880586285913-hk-movie-rag-release,"
            "RAG_RELEVANCE_POLICY_SHA256="
            "a4ecce727a469689a2a08c6d42e0d7d1eb975761173e3b6e4d1eaf34db7b2aba,"
            "RAG_POSTER_AUTHORITY_SHA256="
            "9e56adda2a1fdfa648869c53d7e60a085db5d1c6d11db90dbec13bfe12f0eeed,"
            "RAG_IMAGE_DIGEST=sha256:"
            + "a" * 64
            + ","
            "DB_NAME=hk_movie_rag,DB_USER=hk_rag_app,"
            f"INSTANCE_CONNECTION_NAME={connection}"
        ),
        "--set-secrets": (
            "DB_PASSWORD=hk-rag-db-password:5"
        ),
    }

    def assert_separate_values(call: list[str], expected: dict[str, str]) -> None:
        for flag, value in expected.items():
            index = call.index(flag)
            assert call[index + 1] == value
            assert not any(argument.startswith(flag + "=") for argument in call)

    assert_separate_values(build_call, {"--tag": expected_image})
    assert_separate_values(job_call, expected_job_values)
    assert_separate_values(verifier_job_call, expected_verifier_job_values)
    assert_separate_values(service_call, expected_service_values)
    assert "--project=motionexpaiweb" in build_call
    assert "--project=motionexpaiweb" in job_call
    assert "--project=motionexpaiweb" in verifier_job_call
    assert "--project=motionexpaiweb" in service_call


def test_deploy_resolves_one_enabled_numeric_db_secret_version_without_payload_access(
    deploy_fixture: dict[str, object],
) -> None:
    """Breaks if deployment binds latest, reads the code, or skips enabled-version ambiguity."""
    result, events = _run_apply(deploy_fixture, "fresh")
    assert result.returncode == 0, result.stdout + result.stderr
    calls = [event["args"] for event in events if event["tool"] == "gcloud"]
    version_lists = [call for call in calls if call[:3] == ["secrets", "versions", "list"]]
    assert [call[3] for call in version_lists] == [
        "hk-rag-db-password",
        "hk-rag-db-password",
        "hk-rag-db-password",
    ]
    assert all("--filter=state=ENABLED" in call for call in version_lists)
    assert not any(call[:3] == ["secrets", "versions", "access"] for call in calls)
    jobs = [call for call in calls if call[:3] == ["run", "jobs", "deploy"]]
    service = next(call for call in calls if call[:3] == ["run", "deploy", "hk-movie-rag-demo"])
    assert all(
        job[job.index("--set-secrets") + 1] == "DB_PASSWORD=hk-rag-db-password:5"
        for job in jobs
    )
    bindings = service[service.index("--set-secrets") + 1]
    assert "DB_PASSWORD=hk-rag-db-password:5" in bindings.split(",")
    assert "RAG_DEMO_ACCESS_KEY" not in bindings
    assert "latest" not in bindings.lower()


@pytest.mark.parametrize(
    "scenario",
    [
        "db-secret-version-ambiguous",
        "db-secret-version-disabled",
        "db-secret-version-malformed",
        "db-secret-version-object",
        "db-secret-version-scalar",
        "db-secret-version-null",
    ],
)
def test_deploy_fails_closed_before_mutation_on_ambiguous_disabled_or_malformed_secret_version(
    deploy_fixture: dict[str, object], scenario: str
) -> None:
    """Breaks if a non-unique enabled numeric code version can receive service traffic."""
    result, events = _run_apply(deploy_fixture, scenario)
    assert result.returncode != 0
    calls = _gcloud_calls(events)
    assert not _mutating_calls(calls)
    assert not any(call[:3] == ["secrets", "versions", "access"] for call in calls)


@pytest.mark.parametrize("scenario", ["db-secret-version-changed-before-promotion"])
def test_deploy_rechecks_numeric_secret_versions_immediately_before_promotion(
    deploy_fixture: dict[str, object], scenario: str
) -> None:
    """Breaks if a disabled/replaced secret version can receive candidate traffic."""
    result, events = _run_apply(deploy_fixture, scenario)
    assert result.returncode != 0
    assert not result.stdout.strip()
    calls = _gcloud_calls(events)
    assert [
        call[3] for call in calls if call[:3] == ["secrets", "versions", "list"]
    ] == [
        "hk-rag-db-password",
        "hk-rag-db-password",
        "hk-rag-db-password",
    ]
    assert not _traffic_calls(calls)
    live_smoke_index = next(
        index
        for index, event in enumerate(events)
        if event["tool"] == "uv"
        and event["args"][:2] == ["run", "python"]
        and "live-smoke.py" in event["args"][2]
    )
    final_secret_index = max(
        index
        for index, event in enumerate(events)
        if event["tool"] == "gcloud"
        and event["args"][:3] == ["secrets", "versions", "list"]
    )
    cleanup_patch_index = next(
        index
        for index, event in enumerate(events)
        if event["tool"] == "cloud_run_v2"
        and event.get("method") == "PATCH"
    )
    assert live_smoke_index < final_secret_index < cleanup_patch_index
    assert _v2_patches(events)[0]["target_revision"] == _OLD_REVISION


@pytest.mark.parametrize("scenario", ["drift", "prefix-extra"])
def test_deploy_fails_closed_on_immutable_or_reserved_prefix_drift(
    deploy_fixture: dict[str, object], scenario: str
) -> None:
    result, events = _run_apply(deploy_fixture, scenario)
    assert result.returncode != 0
    calls = [event["args"] for event in events if event["tool"] == "gcloud"]
    if scenario == "drift":
        assert not any(call[:2] in (["storage", "cp"], ["storage", "rsync"]) for call in calls)
    assert not any(call[:2] == ["builds", "submit"] for call in calls)


@pytest.mark.parametrize(
    "scenario",
    [
        "metadata-both",
        "metadata-missing",
        "metadata-null",
        "metadata-scalar",
        "metadata-array",
        "metadata-extra-key",
        "metadata-wrong-hash",
        "metadata-wrong-size",
        "metadata-wrong-content-type",
        "metadata-wrong-cache-control",
    ],
)
def test_deploy_rejects_ambiguous_malformed_or_drifted_object_metadata(
    deploy_fixture: dict[str, object], scenario: str
) -> None:
    """Breaks if immutable object verification accepts a non-exact GCS description."""
    result, events = _run_apply(deploy_fixture, scenario)
    assert result.returncode != 0
    calls = [event["args"] for event in events if event["tool"] == "gcloud"]
    assert not any(call[:2] in (["storage", "cp"], ["storage", "rsync"]) for call in calls)
    assert not any(call[:2] == ["builds", "submit"] for call in calls)


@pytest.mark.parametrize("scenario", ["description-array-singleton", "description-array-multiple"])
def test_deploy_rejects_top_level_array_object_descriptions(
    deploy_fixture: dict[str, object], scenario: str
) -> None:
    """Breaks if PowerShell pipeline enumeration disguises a JSON array as one object."""
    result, events = _run_apply(deploy_fixture, scenario)
    assert result.returncode != 0
    calls = [event["args"] for event in events if event["tool"] == "gcloud"]
    assert not any(call[:2] in (["storage", "cp"], ["storage", "rsync"]) for call in calls)
    assert not any(call[:2] == ["builds", "submit"] for call in calls)


@pytest.mark.parametrize(
    "scenario",
    [
        "job-failure",
        "job-timeout",
        "job-active",
        "job-wrong-name",
        "job-ambiguous-name",
    ],
)
def test_deploy_prevents_overlapping_job_and_fails_on_terminal_or_deadline(
    deploy_fixture: dict[str, object], scenario: str
) -> None:
    result, events = _run_apply(deploy_fixture, scenario)
    assert result.returncode != 0
    calls = [event["args"] for event in events if event["tool"] == "gcloud"]
    execution_starts = [call for call in calls if call[:3] == ["run", "jobs", "execute"]]
    if scenario == "job-active":
        assert not execution_starts
    else:
        assert len(execution_starts) == 1
        if scenario in {"job-failure", "job-timeout"}:
            assert "hk-movie-rag-ingest-abcde" in result.stderr
    if scenario == "job-wrong-name":
        assert "outside the exact job scope" in result.stderr
    if scenario == "job-ambiguous-name":
        assert "ambiguous execution identities" in result.stderr
    assert not any(call[:2] == ["run", "deploy"] for call in calls)


def test_deploy_propagates_poster_verifier_failure_before_service_promotion(
    deploy_fixture: dict[str, object],
) -> None:
    """Breaks if a failed exhaustive fleet job can still promote service traffic."""
    result, events = _run_apply(deploy_fixture, "poster-job-failure")
    assert result.returncode != 0
    assert "hk-movie-rag-poster-verify-abcde" in result.stderr
    calls = [event["args"] for event in events if event["tool"] == "gcloud"]
    assert not any(call[:2] == ["run", "deploy"] for call in calls)


def test_deploy_initial_ingest_failure_has_no_retry_path_to_wash_acceptance(
    deploy_fixture: dict[str, object],
) -> None:
    """Breaks if a failed precondition/count run can retry after mutating the release active."""
    result, events = _run_apply(deploy_fixture, "job-failure")
    assert result.returncode != 0
    calls = [event["args"] for event in events if event["tool"] == "gcloud"]
    ingest_job = next(
        call
        for call in calls
        if call[:4] == ["run", "jobs", "deploy", "hk-movie-rag-ingest"]
    )
    assert "--max-retries=0" in ingest_job
    args_value = ingest_job[ingest_job.index("--args") + 1]
    assert "--require-existing-active" in args_value.split(",")
    assert len(
        [
            call
            for call in calls
            if call[:4] == ["run", "jobs", "execute", "hk-movie-rag-ingest"]
        ]
    ) == 1
    assert not any(call[:2] == ["run", "deploy"] for call in calls)


@pytest.mark.parametrize("scenario", ["service-annotation-false", "service-annotation-missing"])
def test_deploy_fails_closed_when_invoker_check_annotation_is_not_true(
    deploy_fixture: dict[str, object], scenario: str
) -> None:
    result, events = _run_apply(deploy_fixture, scenario)
    assert result.returncode != 0
    stderr = _plain_stderr(result)
    assert "failure_stage=candidate_service_validation" in stderr
    assert "redacted_report=not_available" in stderr
    calls = [event["args"] for event in events if event["tool"] == "gcloud"]
    service_deploys = [call for call in calls if call[:3] == ["run", "deploy", "hk-movie-rag-demo"]]
    assert len(service_deploys) == 1
    assert "--no-invoker-iam-check" in service_deploys[0]
    assert not any("allUsers" in argument for call in calls for argument in call)
