"""Access-code verification and domain-separated signed demo sessions."""

from __future__ import annotations

import base64
import hashlib
import hmac
import json
import secrets
import time
from collections.abc import Callable

_KEY_DERIVATION_DOMAIN = b"hk-movie-rag/demo-session-signing-key/v1"
_TOKEN_SIGNATURE_DOMAIN = b"hk-movie-rag/demo-session-token/v1\x00"


class SessionAuth:
    """Verify one demo code and issue short-lived, stateless session markers."""

    __slots__ = ("_access_code", "_now", "_signing_key", "ttl_seconds")

    def __init__(
        self,
        access_code: str,
        *,
        ttl_seconds: int = 3600,
        now: Callable[[], float] = time.time,
    ) -> None:
        if not isinstance(access_code, str) or not access_code:
            raise ValueError("demo access code must be non-empty")
        if not isinstance(ttl_seconds, int) or isinstance(ttl_seconds, bool) or ttl_seconds < 1:
            raise ValueError("session TTL must be a positive integer")
        self._access_code = access_code.encode("utf-8")
        self._signing_key = hmac.digest(
            self._access_code,
            _KEY_DERIVATION_DOMAIN,
            hashlib.sha256,
        )
        self.ttl_seconds = ttl_seconds
        self._now = now

    def __repr__(self) -> str:
        return f"SessionAuth(ttl_seconds={self.ttl_seconds})"

    def matches_access_code(self, candidate: object) -> bool:
        """Compare a submitted code without an early-exit equality operation."""
        if not isinstance(candidate, str):
            return False
        return hmac.compare_digest(self._access_code, candidate.encode("utf-8"))

    def issue_token(self) -> str:
        """Return a signed marker containing only expiry, nonce, and format version."""
        payload = json.dumps(
            {
                "e": int(self._now()) + self.ttl_seconds,
                "n": _base64url_encode(secrets.token_bytes(16)),
                "v": 1,
            },
            sort_keys=True,
            separators=(",", ":"),
        ).encode("ascii")
        encoded_payload = _base64url_encode(payload)
        signature = hmac.digest(
            self._signing_key,
            _TOKEN_SIGNATURE_DOMAIN + encoded_payload.encode("ascii"),
            hashlib.sha256,
        )
        return f"{encoded_payload}.{_base64url_encode(signature)}"

    def is_valid_token(self, token: object) -> bool:
        """Validate signature and strict expiry without exposing parse failures."""
        if not isinstance(token, str) or token.count(".") != 1:
            return False
        encoded_payload, encoded_signature = token.split(".", 1)
        try:
            signature = _base64url_decode(encoded_signature)
            encoded_payload_bytes = encoded_payload.encode("ascii", errors="strict")
        except (ValueError, UnicodeEncodeError):
            return False
        expected = hmac.digest(
            self._signing_key,
            _TOKEN_SIGNATURE_DOMAIN + encoded_payload_bytes,
            hashlib.sha256,
        )
        if not hmac.compare_digest(expected, signature):
            return False
        try:
            payload = json.loads(_base64url_decode(encoded_payload))
        except (ValueError, json.JSONDecodeError, UnicodeDecodeError):
            return False
        if not isinstance(payload, dict) or set(payload) != {"e", "n", "v"}:
            return False
        expires_at = payload.get("e")
        nonce = payload.get("n")
        version = payload.get("v")
        if (
            not isinstance(expires_at, int)
            or isinstance(expires_at, bool)
            or not isinstance(nonce, str)
            or not nonce
            or version != 1
        ):
            return False
        try:
            nonce_bytes = _base64url_decode(nonce)
        except ValueError:
            return False
        return len(nonce_bytes) == 16 and expires_at > int(self._now())


def _base64url_encode(value: bytes) -> str:
    return base64.urlsafe_b64encode(value).rstrip(b"=").decode("ascii")


def _base64url_decode(value: str) -> bytes:
    if not value or not value.isascii():
        raise ValueError("invalid base64url value")
    padding = "=" * (-len(value) % 4)
    try:
        decoded = base64.b64decode(value + padding, altchars=b"-_", validate=True)
    except (ValueError, TypeError) as exc:
        raise ValueError("invalid base64url value") from exc
    if _base64url_encode(decoded) != value:
        raise ValueError("non-canonical base64url value")
    return decoded
