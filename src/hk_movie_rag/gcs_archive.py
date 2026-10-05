"""Immutable GCS archive upload and streamed verification for frozen source inventories."""

from __future__ import annotations

import hashlib
import io
import json
import os
import re
import stat
import tempfile
from collections.abc import Buffer, Callable, Iterable, Iterator, Sequence
from contextlib import contextmanager
from dataclasses import asdict, dataclass
from datetime import UTC, datetime, timedelta
from pathlib import Path, PurePosixPath
from typing import Any, BinaryIO, Protocol, cast

from google.api_core.exceptions import PreconditionFailed
from google.auth.credentials import Credentials
from google.auth.exceptions import RefreshError

from .config import Settings
from .gcp_preflight import (
    CommandRunner,
    GcpPreflightReport,
    archive_required_services_ready,
    normalize_preflight_blockers,
    run_preflight,
)
from .inventory import InventoryEntry
from .posters import (
    PosterError,
    _atomic_replace_bytes,
    _capture_ancestor_identities,
    _is_reparse,
    _require_ancestor_identities,
    _safe_directory,
    _safe_output_root,
)


class ArchiveError(ValueError):
    """Raised when immutable archive bytes cannot be proven correct."""


class Blob(Protocol):
    name: str
    size: int | None
    metadata: dict[str, str] | None
    generation: str | int | None

    def upload_from_file(self, stream: BinaryIO, *, if_generation_match: int) -> None: ...

    def download_to_file(self, stream: BinaryIO, *, if_generation_match: str | int) -> None: ...


class Bucket(Protocol):
    name: str
    project_number: int | None
    location: str | None
    iam_configuration: object
    versioning_enabled: bool | None
    soft_delete_policy: object
    lifecycle_rules: object
    metageneration: str | int | None

    def blob(self, name: str) -> Blob: ...

    def get_blob(
        self,
        name: str,
        *,
        generation: str | int | None = None,
    ) -> Blob | None: ...

    def list_blobs(self, *, prefix: str, versions: bool = False) -> Iterable[Blob]: ...

    def reload(self) -> None: ...

    def get_iam_policy(self, *, requested_policy_version: int) -> object: ...

    def test_iam_permissions(self, permissions: list[str]) -> list[str]: ...


class StorageClient(Protocol):
    """One explicitly authenticated Storage client for all archive authority and writes."""

    project: str | None

    def bucket(self, name: str) -> Bucket: ...


class AccessTokenRunner(Protocol):
    def access_token(self, account: str) -> str:
        """Return a short-lived token for exactly one previously verified account."""


_TERRAFORM_BACKEND_BUCKET = "motionexpaiweb-880586285913-hk-movie-rag-tfstate"
_TERRAFORM_STATE_OBJECT = "hk-movie-rag/archive/default.tfstate"
_MAX_TERRAFORM_STATE_BYTES = 4 * 1024 * 1024
_INVALID_TERRAFORM_STATE_OUTPUT = object()
_CANONICAL_PROVIDER_UTC_TIMESTAMP = re.compile(r"\d{4}-\d{2}-\d{2}T\d{2}:\d{2}:\d{2}\.\d{3}Z")


class _HashingSink(io.RawIOBase):
    """Write-only download target that computes a SHA-256 without retaining bytes."""

    def __init__(self) -> None:
        self.byte_count = 0
        self._digest = hashlib.sha256()

    def writable(self) -> bool:
        return True

    def write(self, content: Buffer) -> int:
        chunk = memoryview(content)
        self._digest.update(chunk)
        self.byte_count += len(chunk)
        return len(chunk)

    @property
    def sha256(self) -> str:
        return self._digest.hexdigest()


class _BoundedBuffer(io.RawIOBase):
    """Small bounded authority-state receiver; never accepts an unbounded remote object."""

    def __init__(self, limit: int) -> None:
        self._limit = limit
        self._content = bytearray()

    def writable(self) -> bool:
        return True

    def write(self, content: Buffer) -> int:
        chunk = memoryview(content)
        if len(self._content) + len(chunk) > self._limit:
            raise ArchiveError("Terraform state exceeds authority size limit")
        self._content.extend(chunk)
        return len(chunk)

    @property
    def content(self) -> bytes:
        return bytes(self._content)


@dataclass(frozen=True)
class ArchiveObject:
    inventory: InventoryEntry
    local_path: Path
    object_name: str
    repo_root: Path | None = None
    source_identity: tuple[int, int] | None = None


@dataclass(frozen=True)
class ArchiveObjectResult:
    object_name: str
    generation: str | int | None
    sha256: str
    action: str
    hash_verified: bool


@dataclass(frozen=True)
class RemoteSnapshot:
    object_name: str
    generation: str | int


@dataclass(frozen=True)
class ArchiveUploadReport:
    bucket: str
    prefix: str
    object_count: int
    total_bytes: int
    inventory_digest: str
    objects: tuple[ArchiveObjectResult, ...]


@dataclass(frozen=True)
class ArchiveVerificationReport:
    bucket: str
    prefix: str
    object_count: int
    total_bytes: int
    inventory_digest: str
    objects: tuple[ArchiveObjectResult, ...]
    verification_timestamp: str
    verified: bool


def upload_archive(
    bucket: Bucket,
    *,
    repo_root: Path,
    inventory_path: Path,
    release_version: str,
    prefix: str,
    source_roots: Sequence[Path] | None = None,
    authority_guard: Callable[[], None] | None = None,
) -> ArchiveUploadReport:
    """Create immutable source and inventory objects, rejecting every mismatch."""
    inventory_digest, local_inventory_size, objects = _load_archive_objects(
        repo_root, inventory_path, prefix, source_roots
    )
    results = tuple(
        upload_one(
            bucket,
            entry,
            release_version=release_version,
            inventory_digest=inventory_digest,
            repo_root=repo_root,
            authority_guard=authority_guard,
        )
        for entry in objects
    )
    inventory_object = _inventory_object(prefix)
    _upload_immutable_file(
        bucket,
        repo_root=repo_root,
        local_path=inventory_path,
        object_name=inventory_object,
        expected_size=local_inventory_size,
        expected_sha256=inventory_digest,
        metadata=_metadata(inventory_digest, release_version, inventory_digest),
        authority_guard=authority_guard,
    )
    return ArchiveUploadReport(
        bucket=bucket.name,
        prefix=prefix,
        object_count=len(objects),
        total_bytes=sum(entry.inventory.size_bytes for entry in objects),
        inventory_digest=inventory_digest,
        objects=results,
    )


def verify_archive(
    bucket: Bucket,
    *,
    repo_root: Path,
    inventory_path: Path,
    release_version: str,
    prefix: str,
    source_roots: Sequence[Path] | None = None,
    now: datetime | None = None,
    authority_guard: Callable[[], None] | None = None,
) -> ArchiveVerificationReport:
    """Stream-hash every archived source object and create an immutable report."""
    inventory_digest, local_inventory_size, objects = _load_archive_objects(
        repo_root,
        inventory_path,
        prefix,
        source_roots,
        require_local_sources=False,
    )
    snapshots = _verify_exact_listing(bucket, objects, prefix)
    generations = {snapshot.object_name: snapshot.generation for snapshot in snapshots}
    inventory_blob = _get_required_blob(bucket, _inventory_object(prefix))
    inventory_generation = _require_generation(inventory_blob)
    remote_inventory_size, inventory_sha256 = _stream_blob(inventory_blob, inventory_generation)
    if remote_inventory_size != local_inventory_size or inventory_sha256 != inventory_digest:
        raise ArchiveError("remote inventory mismatch")
    expected_inventory_metadata = _metadata(inventory_digest, release_version, inventory_digest)
    if not _metadata_matches(inventory_blob.metadata, expected_inventory_metadata):
        raise ArchiveError("remote inventory metadata mismatch")
    results = tuple(
        verify_one(
            bucket,
            entry,
            release_version=release_version,
            inventory_digest=inventory_digest,
            generation=generations[entry.object_name],
        )
        for entry in objects
    )
    timestamp = (now or datetime.now(UTC)).astimezone(UTC).isoformat()
    report = ArchiveVerificationReport(
        bucket=bucket.name,
        prefix=prefix,
        object_count=len(objects),
        total_bytes=sum(entry.inventory.size_bytes for entry in objects),
        inventory_digest=inventory_digest,
        objects=results,
        verification_timestamp=timestamp,
        verified=True,
    )
    report_name = _verification_report_object(prefix, timestamp)
    payload = report_to_json(report).encode("utf-8")
    if _verify_exact_listing(bucket, objects, prefix) != snapshots:
        raise ArchiveError("remote archive changed during verification")
    final_inventory = _get_required_blob(bucket, _inventory_object(prefix))
    if _require_generation(final_inventory) != inventory_generation:
        raise ArchiveError("remote inventory changed during verification")
    _upload_immutable_bytes(
        bucket,
        object_name=report_name,
        content=payload,
        metadata=_metadata(hashlib.sha256(payload).hexdigest(), release_version, inventory_digest),
        authority_guard=authority_guard,
    )
    return report


def upload_one(
    bucket: Bucket,
    entry: ArchiveObject,
    *,
    release_version: str,
    inventory_digest: str,
    repo_root: Path | None = None,
    authority_guard: Callable[[], None] | None = None,
) -> ArchiveObjectResult:
    """Upload one local object once, or prove that its existing bytes are identical."""
    metadata = _metadata(entry.inventory.sha256, release_version, inventory_digest)
    root = repo_root or entry.repo_root or entry.local_path.parent
    existing = _get_optional_blob(bucket, entry.object_name)
    if existing is not None:
        _assert_existing_matches(
            existing, entry.inventory.size_bytes, entry.inventory.sha256, metadata
        )
        _assert_local_matches(
            root,
            entry.local_path,
            entry.object_name,
            entry.inventory.size_bytes,
            entry.inventory.sha256,
            expected_identity=entry.source_identity,
        )
        return ArchiveObjectResult(
            object_name=entry.object_name,
            generation=existing.generation,
            sha256=entry.inventory.sha256,
            action="skipped_existing",
            hash_verified=True,
        )
    return _upload_immutable_file(
        bucket,
        repo_root=root,
        local_path=entry.local_path,
        object_name=entry.object_name,
        expected_size=entry.inventory.size_bytes,
        expected_sha256=entry.inventory.sha256,
        metadata=metadata,
        expected_identity=entry.source_identity,
        authority_guard=authority_guard,
    )


def verify_one(
    bucket: Bucket,
    entry: ArchiveObject,
    *,
    release_version: str | None = None,
    inventory_digest: str | None = None,
    generation: str | int | None = None,
) -> ArchiveObjectResult:
    """Prove one remote source object with a streamed SHA-256 calculation."""
    blob = _get_required_blob(bucket, entry.object_name, generation)
    pinned_generation = _require_generation(blob)
    if generation is not None and pinned_generation != generation:
        raise ArchiveError(f"remote generation changed: {entry.object_name}")
    actual_size, actual_sha256 = _stream_blob(blob, pinned_generation)
    if actual_size != entry.inventory.size_bytes:
        raise ArchiveError(f"remote size mismatch: {entry.object_name}")
    if actual_sha256 != entry.inventory.sha256:
        raise ArchiveError(f"remote SHA-256 mismatch: {entry.object_name}")
    if release_version is not None and inventory_digest is not None:
        expected_metadata = _metadata(entry.inventory.sha256, release_version, inventory_digest)
        if not _metadata_matches(blob.metadata, expected_metadata):
            raise ArchiveError(f"remote metadata mismatch: {entry.object_name}")
    return ArchiveObjectResult(
        object_name=entry.object_name,
        generation=pinned_generation,
        sha256=actual_sha256,
        action="verified",
        hash_verified=True,
    )


def report_to_json(report: ArchiveUploadReport | ArchiveVerificationReport) -> str:
    """Return a deterministic, newline-terminated archive report payload."""
    return json.dumps(asdict(report), sort_keys=True, separators=(",", ":")) + "\n"


def write_verification_report(
    report: ArchiveVerificationReport,
    output: Path,
    *,
    repo_root: Path,
    protected_roots: Sequence[Path],
) -> None:
    """Atomically write a local verification copy outside protected source roots."""
    try:
        root = _safe_output_root(repo_root)
        candidate = output if output.is_absolute() else root / output
        lexical = Path(os.path.abspath(candidate))
        if lexical == root or not lexical.is_relative_to(root):
            raise ArchiveError("verification report is outside workspace")
        protected = tuple(_protected_output_root(root, path) for path in protected_roots)
        _validate_output_separation(lexical, protected)
        parent = _safe_directory(root, lexical.parent.relative_to(root))
        expected_ancestors = _capture_ancestor_identities(root, parent)
        destination = parent / lexical.name

        def validate_destination() -> None:
            _require_ancestor_identities(root, parent, expected_ancestors)
            _validate_output_separation(destination, protected)
            try:
                destination_stat = destination.lstat()
            except FileNotFoundError:
                return
            if (
                not destination.is_file()
                or _is_reparse(destination_stat)
                or destination_stat.st_nlink != 1
            ):
                raise ArchiveError("verification report destination is unsafe")

        _atomic_replace_bytes(
            root,
            destination,
            report_to_json(report).encode("utf-8"),
            pre_install=validate_destination,
        )
    except (ArchiveError, OSError, PosterError, ValueError) as exc:
        raise ArchiveError("verification report could not be published") from exc


def create_storage_client(
    token: str,
    project_id: str = "motionexpaiweb",
    *,
    token_refresher: Callable[[], str] | None = None,
) -> StorageClient:
    """Build a client from one explicit short-lived token; ambient ADC is never consulted."""
    try:
        from google.cloud import storage  # type: ignore[import-untyped]
    except ImportError as exc:  # pragma: no cover - exercised in deployed CLI environments.
        raise ArchiveError(
            "google-cloud-storage dependency is required for archive commands"
        ) from exc
    try:
        from google.api_core.client_options import ClientOptions
        from google.oauth2.credentials import Credentials as StaticCredentials

        if not token or any(character.isspace() for character in token):
            raise ValueError
        poisoned = {
                "STORAGE_EMULATOR_HOST", "API_ENDPOINT_OVERRIDE", "GOOGLE_CLOUD_PROJECT",
                "GCLOUD_PROJECT", "CLOUDSDK_CORE_PROJECT", "GOOGLE_APPLICATION_CREDENTIALS",
                "GOOGLE_OAUTH_ACCESS_TOKEN", "CLOUDSDK_API_ENDPOINT_OVERRIDES_STORAGE",
                "GOOGLE_CLOUD_STORAGE_ENDPOINT",
        }
        if any(os.environ.get(name) for name in poisoned):
            raise ValueError
        credentials: Credentials
        if token_refresher is None:
            credentials = StaticCredentials(token=token)
        else:
            credentials = _ExactAccountCredentials(token, token_refresher)
        return storage.Client(
            project=project_id,
            credentials=credentials,
            client_options=ClientOptions(api_endpoint="https://storage.googleapis.com"),
        )
    except Exception:  # noqa: BLE001 - provider failures must be sanitized at this boundary.
        raise ArchiveError("storage client is unavailable") from None


class _ExactAccountCredentials(Credentials):
    """Refresh a short-lived access token only through the exact-account command runner."""

    def __init__(self, token: str, refresher: Callable[[], str]) -> None:
        super().__init__()
        self.token = token
        self.expiry = _credential_expiry()
        self._refresher = refresher

    def refresh(self, request: object) -> None:
        try:
            token = self._refresher()
        except Exception as exc:
            raise RefreshError("exact account token refresh failed") from exc
        if not isinstance(token, str) or not token or any(character.isspace() for character in token):
            raise RefreshError("exact account token refresh failed")
        self.token = token
        self.expiry = _credential_expiry()


def _credential_expiry() -> datetime:
    """Match google-auth's naive UTC expiry representation."""
    return datetime.now(UTC).replace(tzinfo=None) + timedelta(minutes=45)


def create_bucket(bucket_name: str, token: str) -> Bucket:
    """Compatibility helper for explicit-token callers; never uses ADC or environment tokens."""
    return _client_bucket(create_storage_client(token), bucket_name)


@dataclass(frozen=True)
class ArchiveAuthority:
    bucket: Bucket
    backend_bucket: Bucket
    client: StorageClient
    state_generation: str | int
    project_id: str
    project_number: str
    bucket_metageneration: str | int
    policy_etag: str


def establish_archive_authority(
    settings: Settings,
    bucket: str,
    *,
    preflight_runner: CommandRunner,
    token_runner: AccessTokenRunner,
    client_factory: Any,
    preflight_reader: Any = run_preflight,
) -> ArchiveAuthority:
    """Bind the verified account, remote Terraform state, and archive bucket to one client."""
    identity_validator = getattr(preflight_runner, "validate_identity", None)
    if callable(identity_validator):
        try:
            identity_validator()
        except Exception as exc:
            raise ArchiveError("archive Cloud SDK identity is unavailable") from exc
    try:
        report: GcpPreflightReport = preflight_reader(settings, preflight_runner)
    except Exception as exc:
        if isinstance(exc, ArchiveError):
            raise
        raise ArchiveError("archive preflight is unavailable") from exc
    project_number = report.project_number
    if (
        not report.ready_for_archive
        or report.active_account != settings.gcp.required_account
        or report.active_project != settings.gcp.project_id
        or report.project_lifecycle != "ACTIVE"
        or report.billing_enabled is not True
        or report.caller_iam_visible is not True
        or report.blockers != []
        or not isinstance(project_number, str)
        or not project_number.isascii()
        or not project_number.isdecimal()
        or not archive_required_services_ready(
            report.required_services,
            getattr(settings.gcp, "required_services", ("storage.googleapis.com",)),
        )
    ):
        raise ArchiveError(
            f"archive preflight is not ready: {_normalized_preflight_blockers(report)}"
        )
    try:
        derived_bucket = settings.gcp.archive_bucket_template.format(
            project_id=settings.gcp.project_id,
            project_number=project_number,
        )
    except (KeyError, ValueError):
        raise ArchiveError("archive bucket template is invalid") from None
    if not derived_bucket or bucket != derived_bucket:
        raise ArchiveError("archive bucket is not the configured authority")
    try:
        token = token_runner.access_token(settings.gcp.required_account)
    except Exception as exc:
        raise ArchiveError("archive account token is unavailable") from exc
    if not isinstance(token, str) or not token or len(token) > 16384:
        raise ArchiveError("archive account token is invalid")
    try:
        client: StorageClient = client_factory(token)
    except Exception as exc:
        raise ArchiveError("archive storage client is unavailable") from exc
    _validate_client_project(client, settings.gcp.project_id)
    backend = _client_bucket(client, _TERRAFORM_BACKEND_BUCKET)
    state_blob = _get_required_blob(backend, _TERRAFORM_STATE_OBJECT)
    state_generation = _require_generation(state_blob)
    state = _read_terraform_state(state_blob, state_generation)
    _validate_terraform_state(state, bucket, settings.gcp.project_id)
    archive_bucket = _client_bucket(client, bucket)
    bucket_metageneration, policy_etag = _validate_live_bucket(
        archive_bucket, bucket, settings.gcp.project_id, project_number
    )
    return ArchiveAuthority(
        archive_bucket,
        backend,
        client,
        state_generation,
        settings.gcp.project_id,
        project_number,
        bucket_metageneration,
        policy_etag,
    )


def assert_authority_state_current(authority: ArchiveAuthority) -> None:
    """Reject authority drift before an archive operation can create an object."""
    state = _get_required_blob(authority.backend_bucket, _TERRAFORM_STATE_OBJECT)
    if _require_generation(state) != authority.state_generation:
        raise ArchiveError("Terraform authority state changed")


def _normalized_preflight_blockers(report: GcpPreflightReport) -> str:
    """Expose only deterministic normalized blocker codes, never provider diagnostics."""
    normalized = normalize_preflight_blockers(getattr(report, "blockers", None))
    return ",".join(normalized) if normalized else "preflight_contract_mismatch"


def assert_archive_authority_current(authority: ArchiveAuthority) -> None:
    """Re-read every authority surface at an archive-operation boundary."""
    _validate_client_project(authority.client, authority.project_id)
    state = _get_required_blob(authority.backend_bucket, _TERRAFORM_STATE_OBJECT)
    generation = _require_generation(state)
    if generation != authority.state_generation:
        raise ArchiveError("Terraform authority state changed")
    _validate_terraform_state(
        _read_terraform_state(state, generation), authority.bucket.name, authority.project_id
    )
    metageneration, policy_etag = _validate_live_bucket(
        authority.bucket,
        authority.bucket.name,
        authority.project_id,
        authority.project_number,
    )
    if (
        metageneration != authority.bucket_metageneration
        or policy_etag != authority.policy_etag
    ):
        raise ArchiveError("archive bucket authority changed")


def _validate_client_project(client: StorageClient, expected_project: str) -> None:
    try:
        if client.project != expected_project:
            raise ArchiveError("archive storage client project is invalid")
    except ArchiveError:
        raise
    except Exception:  # noqa: BLE001 - provider property access must not escape this boundary.
        raise ArchiveError("archive storage client is unavailable") from None


def _validate_live_bucket(
    bucket: Bucket,
    expected_bucket: str,
    expected_project: str,
    expected_project_number: str,
) -> tuple[str | int, str]:
    try:
        bucket.reload()
        policy = bucket.get_iam_policy(requested_policy_version=3)
        permissions = bucket.test_iam_permissions(
            ["storage.objects.create", "storage.objects.get", "storage.objects.list"]
        )
        iam = getattr(bucket, "iam_configuration", None)
        soft_delete = getattr(bucket, "soft_delete_policy", None)
        retention = getattr(soft_delete, "retention_duration_seconds", None)
        if (
            bucket.name != expected_bucket
            or str(bucket.project_number) != expected_project_number
            or bucket.location != "US-CENTRAL1"
            or getattr(iam, "uniform_bucket_level_access_enabled", None) is not True
            or getattr(iam, "public_access_prevention", None) != "enforced"
            or bucket.versioning_enabled is not True
            or retention != 604800
            or tuple(cast(Iterable[object], bucket.lifecycle_rules)) != ()
            or bucket.metageneration is None
        ):
            raise ArchiveError("archive bucket authority is invalid")
        bindings = _normalize_policy_bindings(getattr(policy, "bindings", None))
        etag = getattr(policy, "etag", None)
        expected_writer = (
            f"serviceAccount:hk-rag-archive-writer@{expected_project}.iam.gserviceaccount.com"
        )
        expected_bindings = [
            {"role": "roles/storage.objectCreator", "members": [expected_writer]},
            {"role": "roles/storage.objectViewer", "members": [expected_writer]},
        ]
        if (
            getattr(policy, "version", None) not in {1, 3}
            or bindings != expected_bindings
            or not isinstance(etag, str)
            or not etag
        ):
            raise ArchiveError("archive bucket IAM authority is invalid")
        required = {"storage.objects.create", "storage.objects.get", "storage.objects.list"}
        if not isinstance(permissions, list) or set(permissions) != required:
            raise ArchiveError("archive caller permissions are insufficient")
        return bucket.metageneration, etag
    except ArchiveError:
        raise
    except Exception:  # noqa: BLE001 - provider property access must not escape this boundary.
        raise ArchiveError("archive bucket authority is unavailable") from None


def _normalize_policy_bindings(value: object) -> list[dict[str, object]] | None:
    if not isinstance(value, list):
        return None
    normalized: list[dict[str, object]] = []
    for binding in value:
        if not isinstance(binding, dict) or set(binding) != {"role", "members"}:
            return None
        role, members = binding.get("role"), binding.get("members")
        if (
            not isinstance(role, str)
            or not isinstance(members, (set, list, tuple))
            or not all(isinstance(member, str) for member in members)
        ):
            return None
        normalized.append({"role": role, "members": sorted(members)})
    return sorted(normalized, key=lambda binding: str(binding["role"]))


def _client_bucket(client: StorageClient, name: str) -> Bucket:
    try:
        return client.bucket(name)
    except Exception as exc:
        raise ArchiveError("archive storage client is unavailable") from exc


def _read_terraform_state(blob: Blob, generation: str | int) -> dict[str, object]:
    receiver = _BoundedBuffer(_MAX_TERRAFORM_STATE_BYTES)
    try:
        blob.download_to_file(cast(BinaryIO, receiver), if_generation_match=generation)
    except ArchiveError:
        raise
    except Exception as exc:
        raise ArchiveError("Terraform authority state is unreadable") from exc
    try:
        state = json.loads(receiver.content.decode("utf-8"))
    except (UnicodeDecodeError, json.JSONDecodeError):
        raise ArchiveError("Terraform authority state is invalid") from None
    if not isinstance(state, dict):
        raise ArchiveError("Terraform authority state is invalid")
    return cast(dict[str, object], state)


def _validate_terraform_state(state: dict[str, object], bucket: str, project: str) -> None:
    if (
        state.get("version") != 4
        or not isinstance(state.get("serial"), int)
        or not isinstance(state.get("lineage"), str)
        or not state["lineage"]
    ):
        raise ArchiveError("Terraform authority state schema is invalid")
    outputs = state.get("outputs")
    resources = state.get("resources")
    if not isinstance(outputs, dict) or not isinstance(resources, list):
        raise ArchiveError("Terraform authority state schema is invalid")
    if _terraform_state_output(outputs, "archive_bucket_name") != bucket:
        raise ArchiveError("Terraform authority state does not authorize this bucket")
    expected_writer = f"hk-rag-archive-writer@{project}.iam.gserviceaccount.com"
    writer = _terraform_state_output(outputs, "archive_writer_service_account")
    if writer != expected_writer:
        raise ArchiveError("Terraform authority writer output is invalid")
    addresses: dict[tuple[str, str], dict[str, object]] = {}
    for resource in resources:
        if not isinstance(resource, dict) or resource.get("mode") != "managed":
            raise ArchiveError("Terraform authority state schema is invalid")
        resource_type = resource.get("type")
        name = resource.get("name")
        instances = resource.get("instances")
        if not isinstance(resource_type, str) or not isinstance(name, str) or not isinstance(instances, list):
            raise ArchiveError("Terraform authority state schema is invalid")
        if len(instances) != 1 or not isinstance(instances[0], dict):
            raise ArchiveError("Terraform authority state schema is invalid")
        attributes = instances[0].get("attributes")
        if not isinstance(attributes, dict):
            raise ArchiveError("Terraform authority state schema is invalid")
        addresses[(resource_type, name)] = attributes
    expected_addresses = {
        ("google_storage_bucket", "source_archive"),
        ("google_service_account", "archive_writer"),
        ("google_storage_bucket_iam_policy", "archive_writer"),
    }
    if set(addresses) != expected_addresses:
        raise ArchiveError("Terraform authority resources are invalid")
    archive = addresses[("google_storage_bucket", "source_archive")]
    if (
        archive.get("name") != bucket
        or archive.get("project") != project
        or archive.get("location") != "US-CENTRAL1"
        or archive.get("uniform_bucket_level_access") is not True
        or archive.get("public_access_prevention") != "enforced"
        or archive.get("force_destroy") is not False
        or archive.get("versioning") != [{"enabled": True}]
        or not _valid_terraform_soft_delete_policy(archive.get("soft_delete_policy"))
        or archive.get("lifecycle_rule") not in ([], None)
    ):
        raise ArchiveError("Terraform authority archive bucket is invalid")
    writer_resource = addresses[("google_service_account", "archive_writer")]
    if (
        writer_resource.get("account_id") != "hk-rag-archive-writer"
        or writer_resource.get("email") != expected_writer
        or writer_resource.get("project") != project
    ):
        raise ArchiveError("Terraform authority writer resource is invalid")
    iam_resource = addresses[("google_storage_bucket_iam_policy", "archive_writer")]
    if iam_resource.get("bucket") != f"b/{bucket}" or not _valid_iam_policy_data(
        iam_resource.get("policy_data"), expected_writer
    ):
        raise ArchiveError("Terraform authority IAM resource is invalid")


def _terraform_state_output(outputs: dict[object, object], name: str) -> object:
    output = outputs.get(name)
    if (
        isinstance(output, dict)
        and set(output) in ({"type", "value"}, {"sensitive", "type", "value"})
        and output.get("sensitive", False) is False
        and output.get("type") == "string"
    ):
        return output.get("value")
    return _INVALID_TERRAFORM_STATE_OUTPUT


def _valid_terraform_soft_delete_policy(value: object) -> bool:
    if not isinstance(value, list) or len(value) != 1 or not isinstance(value[0], dict):
        return False
    policy = value[0]
    if set(policy) != {"effective_time", "retention_duration_seconds"}:
        return False
    effective_time = policy.get("effective_time")
    retention = policy.get("retention_duration_seconds")
    if (
        not isinstance(effective_time, str)
        or _CANONICAL_PROVIDER_UTC_TIMESTAMP.fullmatch(effective_time) is None
        or type(retention) is not int
        or retention != 604800
    ):
        return False
    try:
        datetime.fromisoformat(f"{effective_time[:-1]}+00:00")
    except ValueError:
        return False
    return True


def _valid_iam_policy_data(value: object, writer: str) -> bool:
    if not isinstance(value, str):
        return False
    try:
        policy = json.loads(value)
    except json.JSONDecodeError:
        return False
    expected = {
        "bindings": [
            {"role": "roles/storage.objectCreator", "members": [f"serviceAccount:{writer}"]},
            {"role": "roles/storage.objectViewer", "members": [f"serviceAccount:{writer}"]},
        ]
    }
    return policy == expected


def _load_archive_objects(
    repo_root: Path,
    inventory_path: Path,
    prefix: str,
    source_roots: Sequence[Path] | None,
    *,
    require_local_sources: bool = True,
) -> tuple[str, int, tuple[ArchiveObject, ...]]:
    resolved_root = repo_root.resolve(strict=True)
    inventory = _contained_regular_file(resolved_root, inventory_path)
    with _hold_archive_file(resolved_root, inventory, "inventory") as held_inventory:
        inventory_bytes = _read_descriptor(held_inventory.descriptor)
        _verify_held_archive_file(held_inventory)
    entries = _read_inventory(inventory_bytes)
    roots = tuple(
        Path(os.path.abspath(root if root.is_absolute() else resolved_root / root))
        for root in (source_roots or ())
    )
    if any(not root.is_relative_to(resolved_root) for root in roots):
        raise ArchiveError("configured source root is outside workspace")
    objects: list[ArchiveObject] = []
    physical_sources: set[tuple[int, int]] = set()
    for entry in entries:
        if require_local_sources:
            source = _inventory_source_path(resolved_root, entry.relative_path, roots)
            with _hold_archive_file(
                resolved_root, source, f"source {entry.relative_path}"
            ) as held:
                identity = (held.binding.device, held.binding.inode)
                _verify_held_archive_file(held)
            if identity in physical_sources:
                raise ArchiveError(
                    f"duplicate physical inventory source: {entry.relative_path}"
                )
            physical_sources.add(identity)
        else:
            source = _inventory_declared_path(
                resolved_root,
                entry.relative_path,
                roots,
            )
            identity = None
        objects.append(
            ArchiveObject(
                inventory=entry,
                local_path=source,
                object_name=_source_object_name(prefix, entry.relative_path),
                repo_root=resolved_root,
                source_identity=identity,
            )
        )
    return hashlib.sha256(inventory_bytes).hexdigest(), len(inventory_bytes), tuple(objects)


def _read_inventory(content: bytes) -> tuple[InventoryEntry, ...]:
    entries: list[InventoryEntry] = []
    seen: set[str] = set()
    try:
        lines = content.decode("utf-8").splitlines()
    except UnicodeDecodeError as exc:
        raise ArchiveError("cannot read inventory") from exc
    for line_number, line in enumerate(lines, start=1):
        try:
            entry = InventoryEntry.model_validate_json(line)
        except ValueError as exc:
            raise ArchiveError(f"invalid inventory entry at line {line_number}") from exc
        _valid_relative_path(entry.relative_path)
        canonical_path = entry.relative_path.casefold()
        if canonical_path in seen:
            raise ArchiveError(f"duplicate inventory path: {entry.relative_path}")
        seen.add(canonical_path)
        entries.append(entry)
    if not entries:
        raise ArchiveError("inventory is empty")
    return tuple(entries)


@dataclass(frozen=True)
class _ArchiveFileBinding:
    device: int
    inode: int
    size_bytes: int
    modified_ns: int


@dataclass(frozen=True)
class _HeldArchiveFile:
    root: Path
    path: Path
    label: str
    descriptor: int
    binding: _ArchiveFileBinding
    ancestors: tuple[Any, ...]


@contextmanager
def _hold_archive_file(root: Path, path: Path, label: str) -> Iterator[_HeldArchiveFile]:
    before = _require_safe_regular_file(root, path, label)
    try:
        ancestors = _capture_ancestor_identities(root, path.parent)
    except PosterError:
        raise ArchiveError(f"{label} ancestor is unsafe") from None
    descriptor: int | None = None
    try:
        descriptor = _open_protected_archive_descriptor(path)
        opened = os.fstat(descriptor)
        if not _same_archive_identity(before, opened):
            raise ArchiveError(f"{label} changed during protected open")
        _require_windows_canonical_relative_path(root, path, descriptor, label)
        try:
            _require_ancestor_identities(root, path.parent, ancestors)
        except PosterError:
            raise ArchiveError(f"{label} ancestor identity changed") from None
        held = _HeldArchiveFile(
            root=root,
            path=path,
            label=label,
            descriptor=descriptor,
            binding=_archive_binding(opened),
            ancestors=ancestors,
        )
        descriptor = None
        try:
            yield held
            _verify_held_archive_file(held)
        finally:
            os.close(held.descriptor)
    except ArchiveError:
        raise
    except OSError:
        raise ArchiveError(f"{label} is unreadable") from None
    finally:
        if descriptor is not None:
            os.close(descriptor)


def _open_protected_archive_descriptor(path: Path) -> int:
    if os.name != "nt":
        return os.open(
            path,
            os.O_RDONLY | getattr(os, "O_CLOEXEC", 0) | getattr(os, "O_NOFOLLOW", 0),
        )
    import ctypes
    import msvcrt

    kernel32 = ctypes.WinDLL("kernel32", use_last_error=True)
    create_file = kernel32.CreateFileW
    create_file.argtypes = [
        ctypes.c_wchar_p,
        ctypes.c_uint32,
        ctypes.c_uint32,
        ctypes.c_void_p,
        ctypes.c_uint32,
        ctypes.c_uint32,
        ctypes.c_void_p,
    ]
    create_file.restype = ctypes.c_void_p
    handle = create_file(
        str(path), 0x80000000 | 0x80, 0x1, None, 3, 0x00200000, None
    )
    invalid_handle = ctypes.c_void_p(-1).value
    if handle == invalid_handle:
        raise OSError(ctypes.get_last_error(), "cannot open protected archive file")
    try:
        return msvcrt.open_osfhandle(int(handle), os.O_BINARY | os.O_RDONLY)
    except OSError:
        kernel32.CloseHandle(ctypes.c_void_p(handle))
        raise


def _require_windows_canonical_relative_path(
    root: Path, path: Path, descriptor: int, label: str
) -> None:
    if os.name != "nt":
        return
    import ctypes
    import msvcrt

    kernel32 = ctypes.WinDLL("kernel32", use_last_error=True)
    final_path = _windows_path_from_handle(kernel32, msvcrt.get_osfhandle(descriptor))
    long_path = _windows_long_path(kernel32, path)
    long_root = _windows_long_path(kernel32, root)
    display_final = _strip_windows_extended_path(final_path)
    display_long = _strip_windows_extended_path(long_path)
    display_root = _strip_windows_extended_path(long_root)
    normalized_final = _normalize_windows_path(display_final)
    normalized_long = _normalize_windows_path(display_long)
    normalized_root = _normalize_windows_path(display_root)
    if normalized_final != normalized_long or not normalized_long.startswith(normalized_root + "\\"):
        raise ArchiveError(f"{label} canonical path changed")
    relative = display_long[len(display_root) + 1 :].replace("\\", "/")
    if relative != path.relative_to(root).as_posix():
        raise ArchiveError(f"{label} uses a Windows short-name alias")


def _windows_path_from_handle(kernel32: Any, handle: int) -> str:
    import ctypes

    buffer = ctypes.create_unicode_buffer(32768)
    result = kernel32.GetFinalPathNameByHandleW(handle, buffer, len(buffer), 0)
    if result == 0 or result >= len(buffer):
        raise ArchiveError("cannot resolve protected Windows path")
    return buffer.value


def _windows_long_path(kernel32: Any, path: Path) -> str:
    import ctypes

    buffer = ctypes.create_unicode_buffer(32768)
    result = kernel32.GetLongPathNameW(str(path), buffer, len(buffer))
    if result == 0 or result >= len(buffer):
        raise ArchiveError("cannot resolve protected Windows path")
    return buffer.value


def _normalize_windows_path(path: str) -> str:
    return os.path.normcase(_strip_windows_extended_path(path))


def _strip_windows_extended_path(path: str) -> str:
    return path.removeprefix("\\\\?\\").rstrip("\\")


def _read_descriptor(descriptor: int) -> bytes:
    os.lseek(descriptor, 0, os.SEEK_SET)
    chunks: list[bytes] = []
    while chunk := os.read(descriptor, 1024 * 1024):
        chunks.append(chunk)
    return b"".join(chunks)


def _archive_binding(path_stat: os.stat_result) -> _ArchiveFileBinding:
    return _ArchiveFileBinding(
        path_stat.st_dev, path_stat.st_ino, path_stat.st_size, path_stat.st_mtime_ns
    )


def _same_archive_identity(left: os.stat_result, right: os.stat_result) -> bool:
    return (
        stat.S_ISREG(left.st_mode)
        and stat.S_ISREG(right.st_mode)
        and not _is_reparse(left)
        and not _is_reparse(right)
        and left.st_nlink == 1
        and right.st_nlink == 1
        and (left.st_dev, left.st_ino) == (right.st_dev, right.st_ino)
    )


def _verify_held_archive_file(held: _HeldArchiveFile) -> None:
    try:
        current = os.fstat(held.descriptor)
        if _archive_binding(current) != held.binding:
            raise ArchiveError(f"{held.label} changed after binding")
        _require_ancestor_identities(held.root, held.path.parent, held.ancestors)
        named = _require_safe_regular_file(held.root, held.path, held.label)
    except PosterError:
        raise ArchiveError(f"{held.label} ancestor identity changed") from None
    except OSError:
        raise ArchiveError(f"{held.label} changed after binding") from None
    if _archive_binding(named) != held.binding:
        raise ArchiveError(f"{held.label} changed after binding")


def _inventory_source_path(repo_root: Path, relative_path: str, roots: Sequence[Path]) -> Path:
    source = _contained_regular_file(repo_root, Path(relative_path))
    if roots and not any(source.is_relative_to(root.resolve(strict=True)) for root in roots):
        raise ArchiveError(f"inventory path is outside configured source roots: {relative_path}")
    return source


def _inventory_declared_path(
    repo_root: Path,
    relative_path: str,
    roots: Sequence[Path],
) -> Path:
    source = Path(os.path.abspath(repo_root / Path(relative_path)))
    if not source.is_relative_to(repo_root):
        raise ArchiveError(f"inventory path is outside workspace: {relative_path}")
    if roots and not any(source.is_relative_to(root) for root in roots):
        raise ArchiveError(
            f"inventory path is outside configured source roots: {relative_path}"
        )
    return source


def _contained_regular_file(repo_root: Path, path: Path) -> Path:
    candidate = path if path.is_absolute() else repo_root / path
    lexical = Path(os.path.abspath(candidate))
    if not lexical.is_relative_to(repo_root):
        raise ArchiveError(f"path is outside workspace: {path}")
    _require_safe_regular_file(repo_root, lexical, "source path")
    return lexical


def _require_safe_regular_file(repo_root: Path, path: Path, label: str) -> os.stat_result:
    _assert_no_reparse_path(repo_root, path)
    try:
        path_stat = path.lstat()
    except OSError as exc:
        raise ArchiveError(f"{label} is missing") from exc
    if (
        not stat.S_ISREG(path_stat.st_mode)
        or _is_reparse(path_stat)
        or path_stat.st_nlink != 1
    ):
        raise ArchiveError(f"{label} is not a regular file")
    return path_stat


def _assert_no_reparse_path(repo_root: Path, target: Path) -> None:
    relative_parts = target.relative_to(repo_root).parts
    for index in range(1, len(relative_parts) + 1):
        current = repo_root / Path(*relative_parts[:index])
        try:
            current_stat = current.lstat()
        except OSError as exc:
            raise ArchiveError(f"path is missing: {target}") from exc
        attributes = getattr(current_stat, "st_file_attributes", 0)
        if stat.S_ISLNK(current_stat.st_mode) or attributes & getattr(
            stat, "FILE_ATTRIBUTE_REPARSE_POINT", 0
        ):
            raise ArchiveError(f"path violates no-follow containment: {target}")


def _valid_relative_path(relative_path: str) -> None:
    path = PurePosixPath(relative_path)
    windows_devices = {
        "CON",
        "PRN",
        "AUX",
        "NUL",
        "CONIN$",
        "CONOUT$",
        *(f"COM{value}" for value in range(1, 10)),
        *(f"LPT{value}" for value in range(1, 10)),
        "COM¹",
        "COM²",
        "COM³",
        "LPT¹",
        "LPT²",
        "LPT³",
    }
    if (
        not relative_path
        or "\\" in relative_path
        or ":" in relative_path
        or path.is_absolute()
        or ".." in path.parts
        or any(part in {"", ".", ".."} or part.rstrip(". ") != part for part in path.parts)
        or any(re.fullmatch(r"[^.]{1,6}~[1-9](?:\.[^.]{0,3})?", part, re.IGNORECASE) for part in path.parts)
        or any(part.split(".", 1)[0].upper() in windows_devices for part in path.parts)
        or path.as_posix() != relative_path
    ):
        raise ArchiveError(f"invalid inventory relative path: {relative_path}")


def _source_object_name(prefix: str, relative_path: str) -> str:
    _valid_relative_path(relative_path)
    return f"{prefix}/files/{relative_path}"


def _inventory_object(prefix: str) -> str:
    return f"{prefix}/manifests/source_inventory.jsonl"


def _verification_report_object(prefix: str, timestamp: str) -> str:
    compact = datetime.fromisoformat(timestamp).astimezone(UTC).strftime("%Y%m%dT%H%M%S%fZ")
    return f"{prefix}/manifests/verification-{compact}.json"


def _metadata(source_sha256: str, release_version: str, inventory_digest: str) -> dict[str, str]:
    return {
        "source_sha256": source_sha256,
        "release_version": release_version,
        "inventory_digest": inventory_digest,
    }


def _metadata_matches(actual: dict[str, str] | None, expected: dict[str, str]) -> bool:
    return actual is not None and all(actual.get(key) == value for key, value in expected.items())


def _upload_immutable_file(
    bucket: Bucket,
    *,
    repo_root: Path,
    local_path: Path,
    object_name: str,
    expected_size: int,
    expected_sha256: str,
    metadata: dict[str, str],
    expected_identity: tuple[int, int] | None = None,
    authority_guard: Callable[[], None] | None = None,
) -> ArchiveObjectResult:
    existing = _get_optional_blob(bucket, object_name)
    if existing is not None:
        _assert_local_matches(
            repo_root,
            local_path,
            object_name,
            expected_size,
            expected_sha256,
            expected_identity=expected_identity,
        )
        _assert_existing_matches(existing, expected_size, expected_sha256, metadata)
        return ArchiveObjectResult(
            object_name=object_name,
            generation=existing.generation,
            sha256=expected_sha256,
            action="skipped_existing",
            hash_verified=True,
        )
    blob = _new_blob(bucket, object_name)
    blob.metadata = metadata
    try:
        with tempfile.TemporaryFile(mode="w+b") as staged:
            _stage_verified_source(
                repo_root,
                local_path,
                object_name,
                expected_size,
                expected_sha256,
                cast(BinaryIO, staged),
                expected_identity=expected_identity,
            )
            staged.seek(0)
            _run_authority_guard(authority_guard)
            blob.upload_from_file(cast(BinaryIO, staged), if_generation_match=0)
            _run_authority_guard(authority_guard)
    except PreconditionFailed:
        _assert_local_matches(
            repo_root,
            local_path,
            object_name,
            expected_size,
            expected_sha256,
            expected_identity=expected_identity,
        )
        winner = _get_required_blob(bucket, object_name)
        _assert_existing_matches(winner, expected_size, expected_sha256, metadata)
        _run_authority_guard(authority_guard)
        return ArchiveObjectResult(
            object_name=object_name,
            generation=_require_generation(winner),
            sha256=expected_sha256,
            action="skipped_existing",
            hash_verified=True,
        )
    except Exception as exc:
        if isinstance(exc, ArchiveError):
            raise
        raise ArchiveError(f"create-only upload failed: {object_name}") from exc
    return ArchiveObjectResult(
        object_name=object_name,
        generation=_require_generation(blob),
        sha256=expected_sha256,
        action="uploaded",
        hash_verified=True,
    )


def _assert_local_matches(
    repo_root: Path,
    local_path: Path,
    object_name: str,
    expected_size: int,
    expected_sha256: str,
    *,
    expected_identity: tuple[int, int] | None,
) -> None:
    try:
        with _hold_archive_file(repo_root, local_path, f"source {object_name}") as held:
            if expected_identity is not None and (
                held.binding.device,
                held.binding.inode,
            ) != expected_identity:
                raise ArchiveError("source identity changed")
            content = _read_descriptor(held.descriptor)
            _verify_held_archive_file(held)
        actual_size = len(content)
        actual_sha256 = hashlib.sha256(content).hexdigest()
    except ArchiveError:
        raise ArchiveError(f"local source mismatch: {object_name}") from None
    if actual_size != expected_size or actual_sha256 != expected_sha256:
        raise ArchiveError(f"local source mismatch: {object_name}")


def _stage_verified_source(
    repo_root: Path,
    local_path: Path,
    object_name: str,
    expected_size: int,
    expected_sha256: str,
    staged: BinaryIO,
    *,
    expected_identity: tuple[int, int] | None,
) -> None:
    digest = hashlib.sha256()
    byte_count = 0
    try:
        with _hold_archive_file(repo_root, local_path, f"source {object_name}") as held:
            if expected_identity is not None and (
                held.binding.device,
                held.binding.inode,
            ) != expected_identity:
                raise ArchiveError("source identity changed")
            os.lseek(held.descriptor, 0, os.SEEK_SET)
            while chunk := os.read(held.descriptor, 1024 * 1024):
                staged.write(chunk)
                digest.update(chunk)
                byte_count += len(chunk)
            _verify_held_archive_file(held)
    except ArchiveError:
        raise ArchiveError(f"local source mismatch: {object_name}") from None
    if byte_count != expected_size or digest.hexdigest() != expected_sha256:
        raise ArchiveError(f"local source mismatch: {object_name}")


def _upload_immutable_bytes(
    bucket: Bucket,
    *,
    object_name: str,
    content: bytes,
    metadata: dict[str, str],
    authority_guard: Callable[[], None] | None = None,
) -> ArchiveObjectResult:
    blob = _new_blob(bucket, object_name)
    blob.metadata = metadata
    try:
        _run_authority_guard(authority_guard)
        blob.upload_from_file(io.BytesIO(content), if_generation_match=0)
        _run_authority_guard(authority_guard)
    except PreconditionFailed:
        winner = _get_required_blob(bucket, object_name)
        expected_sha256 = hashlib.sha256(content).hexdigest()
        _assert_existing_matches(winner, len(content), expected_sha256, metadata)
        _run_authority_guard(authority_guard)
        return ArchiveObjectResult(
            object_name=object_name,
            generation=_require_generation(winner),
            sha256=expected_sha256,
            action="skipped_existing",
            hash_verified=True,
        )
    except Exception as exc:
        raise ArchiveError(f"create-only upload failed: {object_name}") from exc
    return ArchiveObjectResult(
        object_name=object_name,
        generation=_require_generation(blob),
        sha256=hashlib.sha256(content).hexdigest(),
        action="uploaded",
        hash_verified=True,
    )


def _run_authority_guard(authority_guard: Callable[[], None] | None) -> None:
    if authority_guard is None:
        return
    try:
        authority_guard()
    except ArchiveError:
        raise
    except Exception as exc:
        raise ArchiveError("archive authority guard failed") from exc


def _assert_existing_matches(
    blob: Blob, expected_size: int, expected_sha256: str, expected_metadata: dict[str, str]
) -> None:
    actual_size, actual_sha256 = _stream_blob(blob)
    if (
        blob.size != expected_size
        or actual_size != expected_size
        or actual_sha256 != expected_sha256
        or not _metadata_matches(blob.metadata, expected_metadata)
    ):
        raise ArchiveError(f"existing object mismatch: {blob.name}")


def _verify_exact_listing(
    bucket: Bucket, objects: Sequence[ArchiveObject], prefix: str
) -> tuple[RemoteSnapshot, ...]:
    expected = {entry.object_name for entry in objects}
    try:
        listed = tuple(bucket.list_blobs(prefix=f"{prefix}/files/", versions=False))
    except Exception as exc:
        raise ArchiveError("cannot list remote archive objects") from exc
    actual = {blob.name for blob in listed}
    missing = sorted(expected - actual)
    extra = sorted(actual - expected)
    if missing:
        raise ArchiveError(f"missing remote objects: {', '.join(missing)}")
    if extra:
        raise ArchiveError(f"unexpected remote objects: {', '.join(extra)}")
    if len(actual) != len(listed):
        raise ArchiveError("remote archive listing contains duplicate object names")
    return tuple(
        RemoteSnapshot(blob.name, _require_generation(blob))
        for blob in sorted(listed, key=lambda item: item.name)
    )


def _get_required_blob(
    bucket: Bucket, object_name: str, generation: str | int | None = None
) -> Blob:
    try:
        blob = bucket.get_blob(object_name, generation=generation)
    except Exception as exc:
        raise ArchiveError(f"cannot fetch remote object: {object_name}") from exc
    if blob is None:
        raise ArchiveError(f"missing remote object: {object_name}")
    return blob


def _get_optional_blob(bucket: Bucket, object_name: str) -> Blob | None:
    try:
        return bucket.get_blob(object_name)
    except Exception as exc:
        raise ArchiveError(f"cannot fetch remote object: {object_name}") from exc


def _new_blob(bucket: Bucket, object_name: str) -> Blob:
    try:
        return bucket.blob(object_name)
    except Exception as exc:
        raise ArchiveError(f"cannot prepare remote object: {object_name}") from exc


def _require_generation(blob: Blob) -> str | int:
    if blob.generation is None:
        raise ArchiveError(f"remote object has no generation: {blob.name}")
    return blob.generation


def _stream_blob(blob: Blob, generation: str | int | None = None) -> tuple[int, str]:
    stream = _HashingSink()
    try:
        pinned_generation = generation if generation is not None else _require_generation(blob)
        blob.download_to_file(cast(BinaryIO, stream), if_generation_match=pinned_generation)
    except Exception as exc:
        raise ArchiveError(f"cannot stream remote object: {blob.name}") from exc
    return stream.byte_count, stream.sha256


def _protected_output_root(root: Path, protected: Path) -> Path:
    lexical = Path(os.path.abspath(protected))
    if not lexical.is_relative_to(root):
        raise ArchiveError("protected source root is outside workspace")
    return lexical


def _validate_output_separation(path: Path, protected_roots: Sequence[Path]) -> None:
    if any(path.is_relative_to(protected) for protected in protected_roots):
        raise ArchiveError("verification report is under a protected source root")
