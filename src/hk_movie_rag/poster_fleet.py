"""Exhaustive, redacted verification of the private v1.2 demo poster fleet."""

from __future__ import annotations

import hashlib
import re
from collections.abc import Mapping, Sequence
from concurrent.futures import ThreadPoolExecutor
from contextlib import closing, suppress
from dataclasses import asdict, dataclass
from typing import IO, Protocol

from google.cloud import storage as google_storage

from .poster_authority import (
    PosterAuthorityError,
    PosterServingAuthority,
    load_poster_serving_authority,
)
from .poster_policy import MAX_POSTER_BYTES as _MAX_POSTER_BYTES
from .rag_bundle import RagReleaseContract

_APPROVED_STATES = frozenset({"machine_passed", "manual_approved"})
_UNAVAILABLE_STATES = frozenset({"content_conflict", "missing", "placeholder"})
_MOVIE_ID = re.compile(r"^[A-Za-z0-9][A-Za-z0-9_-]{0,127}$")
_RELEASE_ID = re.compile(r"^[A-Za-z0-9][A-Za-z0-9._-]{0,127}$")
_SHA256 = re.compile(r"^[0-9a-f]{64}$")
_EXPECTED_PRIMARY = 4_658
_EXPECTED_APPROVED = 4_545
_EXPECTED_UNAVAILABLE = 113
_MAX_WORKERS = 16
_CHUNK_BYTES = 64 * 1024
POSTER_FLEET_RELEASE_ID = "v1.2-demo"


class PosterFleetRepository(Protocol):
    def poster_fleet_rows(self, release_id: str) -> Sequence[Mapping[str, object]]: ...


class VerifiedPosterBundle(Protocol):
    @property
    def rag_release_id(self) -> str: ...

    @property
    def contract(self) -> RagReleaseContract: ...


@dataclass(frozen=True)
class PosterObject:
    """A private object stream plus provider-declared identity metadata."""

    stream: IO[bytes]
    size: int
    mime_type: str


class PosterFleetStorage(Protocol):
    def open_object(self, bucket: str, object_name: str) -> PosterObject | None: ...


class GcsPosterFleetStorage:
    """Open exact private GCS objects without logging their locations or bytes."""

    def __init__(self, client: google_storage.Client | None = None) -> None:
        self._client = client if client is not None else google_storage.Client()

    def open_object(self, bucket: str, object_name: str) -> PosterObject | None:
        blob = self._client.bucket(bucket).get_blob(object_name)
        if blob is None:
            return None
        size = blob.size
        content_type = blob.content_type
        if not isinstance(size, int) or isinstance(size, bool):
            size = -1
        if not isinstance(content_type, str):
            content_type = ""
        return PosterObject(blob.open("rb"), size, content_type)


@dataclass(frozen=True, order=True)
class PosterFleetFailure:
    """One deliberately low-cardinality failure safe for logs and JSON reports."""

    movie_id: str
    reason: str


@dataclass(frozen=True)
class PosterFleetReport:
    release_id: str
    primary_rows: int
    approved: int
    unavailable: int
    verified: int
    failures: tuple[PosterFleetFailure, ...]
    passed: bool

    def to_dict(self) -> dict[str, object]:
        return {
            "release_id": self.release_id,
            "primary_rows": self.primary_rows,
            "approved": self.approved,
            "unavailable": self.unavailable,
            "verified": self.verified,
            "failures": [asdict(failure) for failure in self.failures],
            "passed": self.passed,
        }


def verify_poster_fleet(
    repository: PosterFleetRepository,
    storage: PosterFleetStorage,
    release: str | VerifiedPosterBundle,
    bucket: str,
    workers: int = 16,
    *,
    authority: PosterServingAuthority | None = None,
) -> PosterFleetReport:
    """Read the release rows once and prove every canonical derivative or absence."""
    if (
        not isinstance(workers, int)
        or isinstance(workers, bool)
        or not 1 <= workers <= _MAX_WORKERS
    ):
        raise ValueError("workers must be between 1 and 16")
    selected = _selected_fleet_contract(release, authority)
    if selected is None:
        if isinstance(release, str):
            return _fleet_failure(POSTER_FLEET_RELEASE_ID, "release_id_invalid")
        release_id = getattr(release, "rag_release_id", "")
        safe_release_id = release_id if isinstance(release_id, str) and _RELEASE_ID.fullmatch(
            release_id
        ) else POSTER_FLEET_RELEASE_ID
        return _fleet_failure(safe_release_id, "authority_invalid")
    release_id, expected_primary, expected_approved, expected_unavailable = selected
    if isinstance(release, str) and release_id != POSTER_FLEET_RELEASE_ID:
        return _fleet_failure(POSTER_FLEET_RELEASE_ID, "release_id_invalid")
    if not isinstance(bucket, str) or not bucket:
        return _fleet_failure(release_id, "bucket_invalid")
    try:
        rows = list(repository.poster_fleet_rows(release_id))
    except Exception:  # noqa: BLE001 - repository implementations are an external boundary
        return _fleet_failure(release_id, "repository_read_failed")
    primary_rows = len(rows)
    if primary_rows != expected_primary:
        return PosterFleetReport(
            release_id,
            primary_rows,
            0,
            0,
            0,
            (PosterFleetFailure("[fleet]", "primary_row_count_mismatch"),),
            False,
        )

    normalized: list[Mapping[str, object]] = []
    failures: list[PosterFleetFailure] = []
    seen: set[str] = set()
    approved = 0
    unavailable = 0
    for row in rows:
        if not isinstance(row, Mapping):
            failures.append(PosterFleetFailure("[fleet]", "row_invalid"))
            continue
        movie_id = row.get("movie_id")
        if not isinstance(movie_id, str) or _MOVIE_ID.fullmatch(movie_id) is None:
            failures.append(PosterFleetFailure("[fleet]", "movie_id_invalid"))
            continue
        if movie_id in seen:
            failures.append(PosterFleetFailure(movie_id, "movie_id_duplicate"))
            continue
        seen.add(movie_id)
        quality_status = row.get("quality_status")
        if quality_status in _APPROVED_STATES:
            approved += 1
        elif quality_status in _UNAVAILABLE_STATES:
            unavailable += 1
        else:
            failures.append(PosterFleetFailure(movie_id, "quality_status_invalid"))
            continue
        normalized.append(row)

    if approved != expected_approved:
        failures.append(PosterFleetFailure("[fleet]", "approved_count_mismatch"))
    if unavailable != expected_unavailable:
        failures.append(PosterFleetFailure("[fleet]", "unavailable_count_mismatch"))

    verified = 0
    if not failures:
        with ThreadPoolExecutor(max_workers=workers, thread_name_prefix="poster-fleet") as executor:
            for item_verified, item_failure in executor.map(
                lambda row: _verify_row(storage, bucket, row), normalized
            ):
                verified += int(item_verified)
                if item_failure is not None:
                    failures.append(item_failure)

    ordered = tuple(sorted(failures))
    passed = (
        not ordered
        and approved == expected_approved
        and unavailable == expected_unavailable
        and verified == expected_approved
    )
    return PosterFleetReport(
        release_id,
        primary_rows,
        approved,
        unavailable,
        verified,
        ordered,
        passed,
    )


def _selected_fleet_contract(
    release: str | VerifiedPosterBundle,
    authority: PosterServingAuthority | None,
) -> tuple[str, int, int, int] | None:
    if isinstance(release, str):
        if release != POSTER_FLEET_RELEASE_ID or authority is not None:
            return None
        return (
            POSTER_FLEET_RELEASE_ID,
            _EXPECTED_PRIMARY,
            _EXPECTED_APPROVED,
            _EXPECTED_UNAVAILABLE,
        )
    try:
        release_id = release.rag_release_id
        contract = release.contract
        counts = contract.counts
        selected_authority = load_poster_serving_authority(
            release_id,
            contract.poster_authority_sha256,
            contract=contract,
        )
        if (
            not isinstance(release_id, str)
            or _RELEASE_ID.fullmatch(release_id) is None
            or contract.rag_release_id != release_id
            or authority is None
            or authority != selected_authority
            or authority.artifact_sha256 != contract.poster_authority_sha256
            or authority.parent_release_manifest_sha256
            != contract.parent_release_manifest_sha256
            or authority.derived_inventory_sha256 != contract.derived_inventory_sha256
            or authority.access_mode != contract.access_mode
            or authority.primary_poster_rows != counts.primary_poster_rows
            or authority.approved_poster_objects != counts.approved_poster_objects
            or authority.unavailable_poster_rows != counts.unavailable_poster_rows
        ):
            return None
        return (
            release_id,
            authority.primary_poster_rows,
            authority.approved_poster_objects,
            authority.unavailable_poster_rows,
        )
    except (AttributeError, PosterAuthorityError, TypeError):
        return None


def _verify_row(
    storage: PosterFleetStorage, bucket: str, row: Mapping[str, object]
) -> tuple[bool, PosterFleetFailure | None]:
    movie_id = str(row["movie_id"])
    canonical_key = f"assets/posters/derived/{movie_id}.webp"
    if row["quality_status"] in _UNAVAILABLE_STATES:
        if (
            row.get("derived_object_uri") != ""
            or row.get("derived_content_sha256") != ""
            or row.get("derived_byte_length") is not None
            or row.get("derived_mime_type") != ""
        ):
            return False, PosterFleetFailure(movie_id, "unavailable_identity_present")
        try:
            remote = storage.open_object(bucket, canonical_key)
        except Exception:  # noqa: BLE001 - storage provider failures must stay redacted
            return False, PosterFleetFailure(movie_id, "object_read_failed")
        if remote is None:
            return False, None
        with suppress(Exception):
            remote.stream.close()
        return False, PosterFleetFailure(movie_id, "unavailable_object_exists")

    expected_hash = row.get("derived_content_sha256")
    expected_size = row.get("derived_byte_length")
    expected_mime = row.get("derived_mime_type")
    if (
        row.get("derived_object_uri") != canonical_key
        or not isinstance(expected_hash, str)
        or _SHA256.fullmatch(expected_hash) is None
        or not isinstance(expected_size, int)
        or isinstance(expected_size, bool)
        or not 1 <= expected_size <= _MAX_POSTER_BYTES
        or expected_mime != "image/webp"
    ):
        return False, PosterFleetFailure(movie_id, "approved_identity_invalid")
    try:
        remote = storage.open_object(bucket, canonical_key)
        if remote is None:
            return False, PosterFleetFailure(movie_id, "approved_object_missing")
        with closing(remote.stream) as source:
            if remote.mime_type != expected_mime:
                return False, PosterFleetFailure(movie_id, "object_mime_mismatch")
            if remote.size != expected_size or not 1 <= remote.size <= _MAX_POSTER_BYTES:
                return False, PosterFleetFailure(movie_id, "object_size_mismatch")
            digest = hashlib.sha256()
            observed = 0
            while True:
                remaining = min(remote.size, _MAX_POSTER_BYTES) - observed
                chunk = source.read(min(_CHUNK_BYTES, remaining + 1))
                if not chunk:
                    break
                if not isinstance(chunk, bytes):
                    return False, PosterFleetFailure(movie_id, "object_read_failed")
                observed += len(chunk)
                if observed > remote.size or observed > _MAX_POSTER_BYTES:
                    return False, PosterFleetFailure(movie_id, "object_size_mismatch")
                digest.update(chunk)
    except Exception:  # noqa: BLE001 - stream/provider failures must stay redacted
        return False, PosterFleetFailure(movie_id, "object_read_failed")
    if observed != expected_size or observed != remote.size:
        return False, PosterFleetFailure(movie_id, "object_size_mismatch")
    if digest.hexdigest() != expected_hash:
        return False, PosterFleetFailure(movie_id, "content_sha256_mismatch")
    return True, None


def _fleet_failure(release_id: str, reason: str) -> PosterFleetReport:
    return PosterFleetReport(
        release_id,
        0,
        0,
        0,
        0,
        (PosterFleetFailure("[fleet]", reason),),
        False,
    )
