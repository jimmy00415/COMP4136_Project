"""Live contract checks for the thin frontend, using only the standard library.

These smoke requests are operational checks, separate from the paired study.
They never change the backend deployment or submit a model-generation request.
"""

import hashlib
import json
import sys
import time
import urllib.error
import urllib.request
from pathlib import Path

BACKEND = "https://hk-movie-rag-demo-4l6lw3rnaa-uc.a.run.app"


def request(origin, path, method="GET", payload=None):
    req = urllib.request.Request(
        origin + path,
        data=payload,
        method=method,
        headers={"Content-Type": "application/json"},
    )
    try:
        response = urllib.request.urlopen(req, timeout=90)
    except urllib.error.HTTPError as error:
        response = error
    with response:
        headers = {key.lower(): value for key, value in response.headers.items()}
        return response.code, headers, response.read()


def verify(origin):
    # Allow the detached build container to start, without replaying chat POSTs.
    for attempt in range(15):
        try:
            status, _, html = request(origin, "/")
            break
        except urllib.error.URLError:
            if attempt == 14:
                raise
            time.sleep(1)
    assert status == 200
    text = html.decode("utf-8")
    assert "motionexpert" not in text.lower()
    assert '>ME<' not in text
    assert 'id="question-form"' in text
    for asset in ("app.js", "styles.css"):
        code, _, body = request(origin, "/static/" + asset)
        original_code, _, original = request(BACKEND, "/static/" + asset)
        assert code == original_code == 200
        assert body == original, f"{asset} differs from the deployed backend UI"
        assert body == (Path(__file__).parent / "public" / "static" / asset).read_bytes()

    code, _, config = request(origin, "/api/config")
    original_code, _, original_config = request(BACKEND, "/api/config")
    assert code == original_code == 200
    assert json.loads(config) == json.loads(original_config)
    assert request(origin, "/health")[0] == 200

    # Verify error status/payload pass-through without an actual inference call.
    invalid = b'{"question":"","history":[]}'
    bad = request(origin, "/api/chat", "POST", invalid)
    original_bad = request(BACKEND, "/api/chat", "POST", invalid)
    assert bad[0] == original_bad[0] == 422
    assert json.loads(bad[2]) == json.loads(original_bad[2])

    # Real dialogue bodies can exceed NGINX's in-memory request buffer.
    # An empty question ensures this exercises buffering without inference.
    large_invalid = json.dumps({
        "question": "", "history": [], "padding": "x" * 32768
    }).encode()
    large_bad = request(origin, "/api/chat", "POST", large_invalid)
    original_large_bad = request(BACKEND, "/api/chat", "POST", large_invalid)
    assert large_bad[0] == original_large_bad[0] == 422
    assert json.loads(large_bad[2]) == json.loads(original_large_bad[2])

    for path in ("/api/config", "/health", "/api/posters/1978_ZQ_001"):
        assert request(origin, path, "POST", b"{}")[0] == 405
    assert request(origin, "/api/chat")[0] == 405
    for path in ("/api/admin", "/.env", "/nginx.conf", "/static/index.html"):
        assert request(origin, path)[0] == 404

    code, headers, poster = request(origin, "/api/posters/1978_ZQ_001")
    original_code, original_headers, original_poster = request(
        BACKEND, "/api/posters/1978_ZQ_001"
    )
    assert code == original_code == 200
    assert headers.get("content-type", "").startswith("image/")
    assert headers.get("content-type") == original_headers.get("content-type")
    assert headers.get("cache-control") == original_headers.get("cache-control")
    assert poster == original_poster
    print(json.dumps({
        "status": "PASS",
        "origin": origin,
        "backend": BACKEND,
        "asset_parity": True,
        "config_parity": True,
        "validation_status_passthrough": True,
        "poster_sha256": hashlib.sha256(poster).hexdigest(),
        "brand_removed": True,
    }, ensure_ascii=False))


if __name__ == "__main__":
    verify(sys.argv[1].rstrip("/"))
