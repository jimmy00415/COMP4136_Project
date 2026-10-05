from __future__ import annotations

import base64
import json
import re
import threading
from collections.abc import Iterator, Mapping
from contextlib import contextmanager
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from typing import Any
from urllib.parse import parse_qs, urlsplit

PROJECT = "motionexpaiweb"
PROJECT_NUMBER = "880586285913"
DB_SECRET = "hk-rag-db-password"
DEMO_SECRET = "hk-rag-demo-key"
DEFAULT_DB_SECRET = b"D" * 43
DEFAULT_DEMO_SECRET = b"E" * 43

SecretState = dict[str, dict[int, dict[str, Any]]]


def enabled_payloads(state: SecretState, name: str) -> list[bytes]:
    return [item["payload"] for item in state[name].values() if item["state"] == "ENABLED"]


@contextmanager
def secret_manager_server(
    initial_payloads: Mapping[str, bytes | None] | None = None,
) -> Iterator[tuple[str, SecretState]]:
    if initial_payloads is None:
        initial_payloads = {
            DB_SECRET: DEFAULT_DB_SECRET,
            DEMO_SECRET: DEFAULT_DEMO_SECRET,
        }
    state: SecretState = {
        name: (
            {
                1: {
                    "name": f"projects/{PROJECT_NUMBER}/secrets/{name}/versions/1",
                    "payload": payload,
                    "state": "ENABLED",
                }
            }
            if payload is not None
            else {}
        )
        for name, payload in initial_payloads.items()
    }
    prefix = rf"/v1/projects/{PROJECT}/secrets/([^/]+)"

    class Handler(BaseHTTPRequestHandler):
        def log_message(self, format: str, *args: object) -> None:
            del format, args

        def do_GET(self) -> None:
            parsed = urlsplit(self.path)
            list_match = re.fullmatch(prefix + r"/versions", parsed.path)
            access_match = re.fullmatch(prefix + r"/versions/(\d+):access", parsed.path)
            if list_match:
                if parse_qs(parsed.query) != {"filter": ["state:ENABLED"]}:
                    self._json(400, {"error": "unexpected filter"})
                    return
                name = list_match.group(1)
                versions = [
                    {"name": item["name"], "state": item["state"]}
                    for _, item in sorted(state[name].items(), reverse=True)
                    if item["state"] == "ENABLED"
                ]
                self._json(200, {"versions": versions} if versions else {})
                return
            if access_match:
                name = access_match.group(1)
                item = state[name][int(access_match.group(2))]
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
            add_match = re.fullmatch(prefix + r":addVersion", parsed.path)
            disable_match = re.fullmatch(prefix + r"/versions/(\d+):disable", parsed.path)
            raw = self.rfile.read(int(self.headers.get("Content-Length", "0")))
            body = json.loads(raw or b"{}")
            if add_match:
                name = add_match.group(1)
                payload = base64.b64decode(body["payload"]["data"], validate=True)
                number = max(state[name], default=0) + 1
                resource_name = f"projects/{PROJECT_NUMBER}/secrets/{name}/versions/{number}"
                state[name][number] = {
                    "name": resource_name,
                    "payload": payload,
                    "state": "ENABLED",
                }
                self._json(200, {"name": resource_name, "state": "ENABLED"})
                return
            if disable_match:
                name = disable_match.group(1)
                number = int(disable_match.group(2))
                state[name][number]["state"] = "DISABLED"
                self._json(
                    200,
                    {"name": state[name][number]["name"], "state": "DISABLED"},
                )
                return
            self._json(404, {"error": "unexpected POST"})

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
        yield f"http://{host}:{port}", state
    finally:
        server.shutdown()
        thread.join(timeout=5)
        server.server_close()
