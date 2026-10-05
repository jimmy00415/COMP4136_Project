"""Strict loader for the restricted-demo poster-serving operator authority."""

from __future__ import annotations

import hashlib
import json
from collections.abc import Sequence
from dataclasses import dataclass
from importlib.resources import files
from typing import Any

from .rag_bundle import RagReleaseContract

POSTER_SERVING_AUTHORITY_SHA256 = (
    "ca99a5c04d8e61029f58e90cb3017730dc2b3511361834d7a0cf4860b54005e4"
)
POSTER_SERVING_AUTHORITY_V2_SHA256 = (
    "9e56adda2a1fdfa648869c53d7e60a085db5d1c6d11db90dbec13bfe12f0eeed"
)
POSTER_SERVING_AUTHORITY_V3_SHA256 = (
    "c50ff06a88f91296b63bd74a1e4a6485785bdc4c07e73d09b407a126927c8137"
)
_AUTHORITY_FILENAME = "poster_serving_authority_v1.json"
_AUTHORITY_V2_FILENAME = "poster_serving_authority_v2.json"
_AUTHORITY_V3_FILENAME = "poster_serving_authority_v3.json"
_INVALID_AUTHORITY = "poster serving authority is invalid"
_EXPECTED_PAYLOAD: dict[str, object] = {
    "access_mode": "restricted_demo",
    "approved_poster_objects": 4545,
    "decision": "authorized",
    "delivery_scope": "authenticated_same_origin_private_proxy",
    "derived_inventory_sha256": (
        "b754727129bc044b6c9da1e6db22067c8279c5e7f8dd41d0350e365376538154"
    ),
    "operator_permission_confirmed": True,
    "parent_release_manifest_sha256": (
        "cbd529e126e8b72dccac302d9746fcfa63318caecbcda24548c05e282e13f974"
    ),
    "primary_poster_rows": 4658,
    "rag_release_id": "v1.2-demo",
    "schema_version": "poster-serving-authority/v1",
    "source_rights_mutation": "forbidden",
    "source_rights_status": "unknown",
    "unavailable_poster_rows": 113,
}
_EXPECTED_V2_PAYLOAD: dict[str, object] = {
    "access_mode": "restricted_demo",
    "approved_poster_objects": 4545,
    "dataset_id": "v1.2",
    "decision": "authorized",
    "delivery_scope": "authenticated_same_origin_private_proxy",
    "derived_inventory_sha256": (
        "b754727129bc044b6c9da1e6db22067c8279c5e7f8dd41d0350e365376538154"
    ),
    "operator_permission_confirmed": True,
    "parent_release_manifest_sha256": (
        "e9842db33208e7146d78d86689b13f81fffcf41288d79d84e599b93050445e2c"
    ),
    "primary_poster_rows": 4658,
    "schema_version": "poster-serving-authority/v2",
    "source_rights_mutation": "forbidden",
    "source_rights_status": "unknown",
    "unavailable_poster_rows": 113,
}
_EXPECTED_V3_PAYLOAD: dict[str, object] = {
    "access_mode": "restricted_demo",
    "approved_poster_objects": 4546,
    "dataset_id": "v1.2+rag-r3-supplemental-1",
    "decision": "authorized",
    "delivery_scope": "authenticated_same_origin_private_proxy",
    "derived_inventory_sha256": (
        "3e3cedee507c3017821de1081bc70bbb01d0c73f8683ab484a464d1fc1f7c1db"
    ),
    "operator_permission_confirmed": True,
    "parent_release_manifest_sha256": (
        "e9842db33208e7146d78d86689b13f81fffcf41288d79d84e599b93050445e2c"
    ),
    "primary_poster_rows": 4659,
    "rag_release_id": "v1.2-demo-r3",
    "schema_version": "poster-serving-authority/v3",
    "source_rights_mutation": "forbidden",
    "source_rights_status": "unknown_or_restricted",
    "unavailable_poster_rows": 113,
}
_AUTHORITY_SELECTIONS: dict[
    tuple[str, str], tuple[str, dict[str, object]]
] = {
    ("v1.2-demo", POSTER_SERVING_AUTHORITY_SHA256): (
        _AUTHORITY_FILENAME,
        _EXPECTED_PAYLOAD,
    ),
    ("v1.2-demo-r2", POSTER_SERVING_AUTHORITY_V2_SHA256): (
        _AUTHORITY_V2_FILENAME,
        _EXPECTED_V2_PAYLOAD,
    ),
    ("v1.2-demo-r3", POSTER_SERVING_AUTHORITY_V3_SHA256): (
        _AUTHORITY_V3_FILENAME,
        _EXPECTED_V3_PAYLOAD,
    ),
}


class PosterAuthorityError(RuntimeError):
    """Raised when packaged serving authority is missing, malformed, or unapproved."""


@dataclass(frozen=True)
class PosterServingAuthority:
    """Exact non-secret operator decision for restricted poster delivery."""

    access_mode: str
    approved_poster_objects: int
    decision: str
    delivery_scope: str
    derived_inventory_sha256: str
    dataset_id: str | None
    operator_permission_confirmed: bool
    parent_release_manifest_sha256: str
    primary_poster_rows: int
    rag_release_id: str | None
    schema_version: str
    source_rights_mutation: str
    source_rights_status: str
    unavailable_poster_rows: int
    artifact_sha256: str


def _read_authority_bytes() -> bytes:
    return files("hk_movie_rag.authorities").joinpath(_AUTHORITY_FILENAME).read_bytes()


def _read_selected_authority_bytes(filename: str) -> bytes:
    if filename == _AUTHORITY_FILENAME:
        return _read_authority_bytes()
    return files("hk_movie_rag.authorities").joinpath(filename).read_bytes()


def _reject_duplicate_keys(pairs: Sequence[tuple[str, Any]]) -> dict[str, Any]:
    value: dict[str, Any] = {}
    for key, item in pairs:
        if key in value:
            raise PosterAuthorityError(_INVALID_AUTHORITY)
        value[key] = item
    return value


def _reject_json_constant(_value: str) -> None:
    raise PosterAuthorityError(_INVALID_AUTHORITY)


def _canonical_bytes(value: object) -> bytes:
    return json.dumps(
        value,
        ensure_ascii=False,
        sort_keys=True,
        separators=(",", ":"),
        allow_nan=False,
    ).encode("utf-8")


def load_poster_serving_authority(
    release_id: str | None = None,
    expected_artifact_sha256: str | None = None,
    *,
    contract: RagReleaseContract | None = None,
) -> PosterServingAuthority:
    """Select one reviewed authority exactly; v2 additionally binds a verified child."""
    try:
        if release_id is None and expected_artifact_sha256 is None:
            release_id = "v1.2-demo"
            expected_artifact_sha256 = POSTER_SERVING_AUTHORITY_SHA256
        elif release_id is None or expected_artifact_sha256 is None:
            raise PosterAuthorityError(_INVALID_AUTHORITY)
        if not isinstance(release_id, str) or not isinstance(expected_artifact_sha256, str):
            raise PosterAuthorityError(_INVALID_AUTHORITY)
        selection = _AUTHORITY_SELECTIONS.get((release_id, expected_artifact_sha256))
        if selection is None:
            raise PosterAuthorityError(_INVALID_AUTHORITY)
        filename, expected_payload = selection
        raw = _read_selected_authority_bytes(filename)
        parsed = json.loads(
            raw.decode("utf-8"),
            object_pairs_hook=_reject_duplicate_keys,
            parse_constant=_reject_json_constant,
        )
        if not isinstance(parsed, dict) or set(parsed) != set(expected_payload):
            raise PosterAuthorityError(_INVALID_AUTHORITY)
        count_fields = (
            "approved_poster_objects",
            "primary_poster_rows",
            "unavailable_poster_rows",
        )
        if any(type(parsed[field_name]) is not int for field_name in count_fields):
            raise PosterAuthorityError(_INVALID_AUTHORITY)
        if parsed["operator_permission_confirmed"] is not True:
            raise PosterAuthorityError(_INVALID_AUTHORITY)
        if any(parsed[key] != expected for key, expected in expected_payload.items()):
            raise PosterAuthorityError(_INVALID_AUTHORITY)
        canonical = _canonical_bytes(parsed)
        if raw != canonical + b"\n":
            raise PosterAuthorityError(_INVALID_AUTHORITY)
        digest_source = (
            raw
            if expected_payload in (_EXPECTED_V2_PAYLOAD, _EXPECTED_V3_PAYLOAD)
            else canonical
        )
        digest = hashlib.sha256(digest_source).hexdigest()
        if digest != expected_artifact_sha256:
            raise PosterAuthorityError(_INVALID_AUTHORITY)
        if expected_payload in (_EXPECTED_V2_PAYLOAD, _EXPECTED_V3_PAYLOAD):
            _assert_scoped_contract(
                contract, release_id, expected_artifact_sha256, parsed
            )
        elif contract is not None:
            raise PosterAuthorityError(_INVALID_AUTHORITY)
        normalized = {
            **parsed,
            "dataset_id": parsed.get("dataset_id"),
            "rag_release_id": parsed.get("rag_release_id"),
        }
        return PosterServingAuthority(
            **normalized,
            artifact_sha256=digest,
        )
    except PosterAuthorityError:
        raise
    except (OSError, UnicodeError, json.JSONDecodeError, TypeError, ValueError):
        raise PosterAuthorityError(_INVALID_AUTHORITY) from None


def _assert_scoped_contract(
    contract: RagReleaseContract | None,
    release_id: str,
    expected_artifact_sha256: str,
    authority: dict[str, Any],
) -> None:
    if contract is None:
        raise PosterAuthorityError(_INVALID_AUTHORITY)
    counts = contract.counts
    if (
        contract.rag_release_id != release_id
        or contract.poster_authority_sha256 != expected_artifact_sha256
        or contract.parent_release_manifest_sha256
        != authority["parent_release_manifest_sha256"]
        or contract.derived_inventory_sha256 != authority["derived_inventory_sha256"]
        or contract.access_mode != authority["access_mode"]
        or counts.primary_poster_rows != authority["primary_poster_rows"]
        or counts.approved_poster_objects != authority["approved_poster_objects"]
        or counts.unavailable_poster_rows != authority["unavailable_poster_rows"]
    ):
        raise PosterAuthorityError(_INVALID_AUTHORITY)
