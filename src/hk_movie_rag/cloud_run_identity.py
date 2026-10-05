"""Shared exact Cloud Run service and revision identity validation."""

from __future__ import annotations

import re

CLOUD_RUN_SERVICE_NAME = "hk-movie-rag-demo"
_DNS_LABEL = re.compile(r"^[a-z](?:[a-z0-9-]{0,61}[a-z0-9])?$")


def is_cloud_run_revision_name(
    value: object,
    *,
    service_name: str = CLOUD_RUN_SERVICE_NAME,
) -> bool:
    """Return whether *value* is one bounded revision of the exact demo service."""
    if not isinstance(value, str) or _DNS_LABEL.fullmatch(value) is None:
        return False
    prefix = f"{service_name}-"
    return value.startswith(prefix) and len(value) > len(prefix)
