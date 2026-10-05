"""Archive-gated, exact-path cleanup planning and recoverable execution."""

from __future__ import annotations

import hashlib
import json
import os
import re
import stat
from collections.abc import Iterator, Mapping, Sequence
from contextlib import contextmanager
from dataclasses import asdict, dataclass, field, replace
from datetime import datetime
from pathlib import Path, PurePosixPath
from typing import Any, Literal, cast

from .gcs_archive import (
    ArchiveError,
    _hold_archive_file,
    _open_protected_archive_descriptor,
    _read_descriptor,
    _require_windows_canonical_relative_path,
    _valid_relative_path,
    _verify_held_archive_file,
)
from .inventory import InventoryEntry
from .posters import (
    PosterError,
    _acquire_windows_directory_locks,
    _capture_ancestor_identities,
    _is_reparse,
    _locked_output_ancestors,
    _release_windows_directory_locks,
    _require_ancestor_identities,
    _safe_directory,
)
from .release_lock import ReleaseLockError, release_guard


class CleanupError(ValueError):
    """Raised before cleanup whenever immutable evidence or a target is unsafe."""


@dataclass(frozen=True)
class _CleanupAuthority:
    archive_report_sha256: str
    bucket: str
    inventory_digest: str
    object_count: int
    prefix: str
    total_bytes: int
    mirror_counts: tuple[int, int]


_V12_AUTHORITY = _CleanupAuthority(
    archive_report_sha256="289a889769e742345857278184e9387eebc2332c41008bd925cb7e36c0527bce",
    bucket="motionexpaiweb-880586285913-hk-movie-rag-source-archive",
    inventory_digest="f0f0ee1b7f791b7b5c20fca1cfb115c161150de1d37466095dda3e145215ca7c",
    object_count=9265,
    prefix="source/v1.2",
    total_bytes=251769627,
    mirror_counts=(1497, 1500),
)

_SOURCE_ROOTS = (
    "FinalDelivery_2026-07-16",
    "1-1500posters",
    "posters",
    "posters1",
)
_DIRECTORY_TARGETS = (
    "1-1500posters/1-1500posters",
    "posters/__MACOSX",
    "posters1",
)
_INVENTORY_FIELDS = {"classification", "relative_path", "sha256", "size_bytes"}
_ARCHIVE_REPORT_FIELDS = {
    "bucket",
    "inventory_digest",
    "object_count",
    "objects",
    "prefix",
    "total_bytes",
    "verification_timestamp",
    "verified",
}
_ARCHIVE_OBJECT_FIELDS = {
    "action",
    "generation",
    "hash_verified",
    "object_name",
    "sha256",
}
_REPORT_FIELDS = {
    "active_target",
    "archive_verification",
    "bucket",
    "duplicate_proof",
    "failed_target",
    "failure_reason",
    "inventory",
    "inventory_digest",
    "intent",
    "plan_sha256",
    "prefix",
    "quarantine_root",
    "quarantine_root_device",
    "quarantine_root_inode",
    "current_observation",
    "detach_events",
    "rag_readiness",
    "release_version",
    "schema_version",
    "source_root_identities",
    "status",
    "targets",
    "total_targets",
}
_CLASSIFICATIONS = {
    "formal_release_source",
    "governance_evidence",
    "issue_ledger",
    "poster_candidate",
    "duplicate_candidate",
    "macos_metadata",
}
_SHA256 = re.compile(r"[0-9a-f]{64}")
_PLAN_OUTPUT = Path("artifacts/cleanup/v1.2/plan.json")
_REPORT_OUTPUT = Path("artifacts/cleanup/v1.2/report.json")


@dataclass(frozen=True)
class EvidenceBinding:
    relative_path: str
    sha256: str


@dataclass(frozen=True)
class DirectoryBinding:
    relative_path: str
    device: int
    inode: int
    modified_ns: int


@dataclass(frozen=True)
class ExternalDirectoryBinding:
    path: str
    device: int
    inode: int


@dataclass(frozen=True)
class SourceBinding:
    relative_path: str
    device: int
    inode: int
    size_bytes: int
    modified_ns: int
    link_count: int
    sha256: str


@dataclass(frozen=True)
class CleanupTarget:
    relative_path: str
    quarantine_slot: str
    kind: Literal["directory", "file"]
    device: int
    inode: int
    node_size_bytes: int
    modified_ns: int
    link_count: int
    file_count: int
    total_bytes: int
    content_digest: str
    directories: tuple[DirectoryBinding, ...]
    sources: tuple[SourceBinding, ...]


@dataclass(frozen=True)
class CleanupPlan:
    schema_version: str
    verified: bool
    release_version: str
    workspace_root: str
    workspace_device: int
    workspace_inode: int
    quarantine_root: str
    quarantine_ancestors: tuple[ExternalDirectoryBinding, ...]
    source_roots: tuple[str, ...]
    inventory: EvidenceBinding
    duplicate_proof: EvidenceBinding
    archive_verification: EvidenceBinding
    bucket: str
    prefix: str
    object_count: int
    total_bytes: int
    inventory_digest: str
    targets: tuple[CleanupTarget, ...]
    plan_sha256: str


@dataclass(frozen=True)
class CleanupReport:
    schema_version: str
    release_version: str
    plan_sha256: str
    inventory: EvidenceBinding
    duplicate_proof: EvidenceBinding
    archive_verification: EvidenceBinding
    inventory_digest: str
    bucket: str
    prefix: str
    quarantine_root: str
    quarantine_root_device: int | None
    quarantine_root_inode: int | None
    source_root_identities: tuple[ExternalDirectoryBinding, ...]
    targets: tuple[CleanupTarget, ...]
    status: Literal[
        "prepared", "in_progress", "detached", "detached_with_drift", "failed"
    ]
    total_targets: int
    detach_events: tuple[DetachEvent, ...]
    current_observation: CurrentObservation | None
    rag_readiness: Literal["not_authorized"]
    intent: QuarantineIntent | None
    active_target: str | None
    failed_target: str | None
    failure_reason: str | None


@dataclass(frozen=True)
class QuarantineIntent:
    source_relative_path: str
    quarantine_slot: str
    quarantine_root_device: int
    quarantine_root_inode: int
    device: int
    inode: int
    content_digest: str
    recorded_at: str = field(default_factory=lambda: datetime.now().astimezone().isoformat())


@dataclass(frozen=True)
class DetachEvent:
    event_type: Literal["detached_to_quarantine"]
    evidence_origin: Literal["executor", "recovery_inference"]
    source_relative_path: str
    quarantine_slot: str
    plan_sha256: str
    content_digest: str
    quarantine_root_device: int
    quarantine_root_inode: int
    pre_move_root_device: int
    pre_move_root_inode: int
    post_move_root_device: int
    post_move_root_inode: int
    rename_not_before: str
    rename_observed_by: str


@dataclass(frozen=True)
class TargetObservation:
    source_relative_path: str
    quarantine_slot: str
    source_state: Literal["absent", "recreated", "original_present", "unreadable"]
    slot_state: Literal[
        "matches_plan",
        "content_drift",
        "root_identity_changed",
        "missing",
        "unsafe",
        "unreadable",
    ]


@dataclass(frozen=True)
class SourceRootObservation:
    path: str
    state: Literal[
        "matches_recorded_identity",
        "detached_root_absent",
        "missing",
        "identity_changed",
        "unsafe",
        "unreadable",
    ]


@dataclass(frozen=True)
class CurrentObservation:
    observed_at_start: str
    observed_at_end: str
    atomic: Literal[False]
    assessment: Literal["no_drift_observed", "drift_observed", "indeterminate"]
    source_roots: tuple[SourceRootObservation, ...]
    targets: tuple[TargetObservation, ...]


@dataclass(frozen=True)
class CleanupRecovery:
    state: Literal[
        "no_event",
        "not_detached",
        "partial",
        "detached",
        "detached_with_drift",
        "conflict",
    ]
    target: str | None
    detach_events: tuple[DetachEvent, ...]
    current_observation: CurrentObservation | None


@dataclass(frozen=True)
class _CleanupEvidence:
    entries: tuple[InventoryEntry, ...]
    inventory_bytes: bytes
    inventory: EvidenceBinding
    duplicate_proof: EvidenceBinding
    archive_verification: EvidenceBinding


@dataclass
class _ReportReservation:
    root: Path
    path: Path
    descriptor: int
    binding: tuple[int, int]
    ancestors: tuple[Any, ...]
    committed_size: int

    def publish(self, report: CleanupReport) -> None:
        """Durably update only the create-new report file held by this reservation."""
        try:
            _require_ancestor_identities(self.root, self.path.parent, self.ancestors)
            opened = os.fstat(self.descriptor)
            named = self.path.lstat()
        except (OSError, PosterError) as exc:
            raise CleanupError("cleanup report destination identity changed") from exc
        if (
            not stat.S_ISREG(opened.st_mode)
            or not stat.S_ISREG(named.st_mode)
            or _is_reparse(opened)
            or _is_reparse(named)
            or opened.st_nlink != 1
            or named.st_nlink != 1
            or (opened.st_dev, opened.st_ino) != self.binding
            or (named.st_dev, named.st_ino) != self.binding
        ):
            raise CleanupError("cleanup report destination identity changed")
        payload = report_to_json(report).encode()
        try:
            if opened.st_size != self.committed_size:
                raise CleanupError("cleanup report journal tail changed")
            start = os.lseek(self.descriptor, 0, os.SEEK_END)
            if start != self.committed_size:
                raise CleanupError("cleanup report journal tail changed")
            _write_all(self.descriptor, payload)
            os.fsync(self.descriptor)
            installed = _read_descriptor(self.descriptor)
            final = os.fstat(self.descriptor)
        except OSError as exc:
            raise CleanupError("cannot publish cleanup report") from exc
        expected_size = self.committed_size + len(payload)
        if (
            final.st_size != expected_size
            or len(installed) != expected_size
            or installed[self.committed_size :] != payload
        ):
            raise CleanupError("cannot publish cleanup report")
        try:
            _require_ancestor_identities(self.root, self.path.parent, self.ancestors)
            named = self.path.lstat()
        except (OSError, PosterError) as exc:
            raise CleanupError("cleanup report destination identity changed") from exc
        if (named.st_dev, named.st_ino) != self.binding or named.st_nlink != 1:
            raise CleanupError("cleanup report destination identity changed")
        self.committed_size = expected_size


def build_cleanup_plan(
    *,
    workspace_root: Path,
    quarantine_root: Path,
    release_version: str,
    source_roots: Sequence[Path],
    inventory_path: Path,
    duplicate_proof_path: Path,
    archive_verification_path: Path,
) -> CleanupPlan:
    """Build an immutable plan from the exact v1.2 archive evidence."""
    root = _workspace_root(workspace_root)
    external_root, quarantine_ancestors = _validate_external_quarantine_root(
        root, quarantine_root, require_absent=True
    )
    roots = _validate_source_roots(root, source_roots)
    if release_version != "v1.2":
        raise CleanupError("cleanup release version must be v1.2")
    evidence = _load_cleanup_evidence(
        root,
        roots,
        inventory_path,
        duplicate_proof_path,
        archive_verification_path,
    )
    targets = tuple(
        replace(
            _capture_target(root, relative_path, evidence.entries),
            quarantine_slot=f"{index:04d}/{relative_path}",
        )
        for index, relative_path in enumerate(_expected_target_paths(evidence.entries))
    )
    plan = CleanupPlan(
        schema_version="cleanup-plan/v2",
        verified=True,
        release_version=release_version,
        workspace_root=str(root),
        workspace_device=root.lstat().st_dev,
        workspace_inode=root.lstat().st_ino,
        quarantine_root=str(external_root),
        quarantine_ancestors=quarantine_ancestors,
        source_roots=_SOURCE_ROOTS,
        inventory=evidence.inventory,
        duplicate_proof=evidence.duplicate_proof,
        archive_verification=evidence.archive_verification,
        bucket=_V12_AUTHORITY.bucket,
        prefix=_V12_AUTHORITY.prefix,
        object_count=_V12_AUTHORITY.object_count,
        total_bytes=_V12_AUTHORITY.total_bytes,
        inventory_digest=_V12_AUTHORITY.inventory_digest,
        targets=targets,
        plan_sha256="",
    )
    return replace(plan, plan_sha256=_plan_digest(plan))


def validate_cleanup_plan(
    plan: CleanupPlan,
    *,
    repo_root: Path,
    quarantine_must_be_absent: bool | None = True,
) -> None:
    """Rebind every immutable artifact and local target before any action."""
    _validate_target_allowlist(plan)
    if plan.plan_sha256 != _plan_digest(plan):
        raise CleanupError("cleanup plan digest mismatch")
    _validate_static_plan(plan)
    root = _workspace_root(repo_root)
    _validate_plan_workspace(plan, root)
    external_root, quarantine_ancestors = _validate_external_quarantine_root(
        root,
        Path(plan.quarantine_root),
        require_absent=quarantine_must_be_absent,
    )
    if str(external_root) != plan.quarantine_root or quarantine_ancestors != plan.quarantine_ancestors:
        raise CleanupError("cleanup quarantine root binding mismatch")
    roots = tuple(root / relative for relative in plan.source_roots)
    evidence = _load_cleanup_evidence(
        root,
        roots,
        root / plan.inventory.relative_path,
        root / plan.duplicate_proof.relative_path,
        root / plan.archive_verification.relative_path,
    )
    if (
        plan.inventory != evidence.inventory
        or plan.duplicate_proof != evidence.duplicate_proof
        or plan.archive_verification != evidence.archive_verification
    ):
        raise CleanupError("cleanup plan evidence digest mismatch")
    expected_paths = _expected_target_paths(evidence.entries)
    if tuple(target.relative_path for target in plan.targets) != expected_paths:
        raise CleanupError("cleanup target is not allowlisted")
    for index, target in enumerate(plan.targets):
        if target.quarantine_slot != f"{index:04d}/{target.relative_path}":
            raise CleanupError("cleanup quarantine slot binding mismatch")
        _revalidate_target(root, target, evidence.entries)


def execute_cleanup(
    plan: CleanupPlan,
    confirmation: str,
    *,
    repo_root: Path,
    report_output: Path,
    protected_roots: Sequence[Path],
) -> CleanupReport:
    """Record exact atomic detach events and separately observe current drift."""
    validate_cleanup_plan(
        plan, repo_root=repo_root, quarantine_must_be_absent=None
    )
    if confirmation != "v1.2" or confirmation != plan.release_version:
        raise CleanupError("confirmation mismatch")
    root = _workspace_root(repo_root)
    _require_exact_output(report_output, _REPORT_OUTPUT, "report")
    evidence = _load_cleanup_evidence(
        root,
        tuple(root / relative for relative in plan.source_roots),
        root / plan.inventory.relative_path,
        root / plan.duplicate_proof.relative_path,
        root / plan.archive_verification.relative_path,
    )
    source_root_identities = _capture_source_root_identities(root, protected_roots)
    detach_events: list[DetachEvent] = []
    prepared = _report_for(
        plan,
        "prepared",
        detach_events,
        source_root_identities=source_root_identities,
    )
    with _reserve_cleanup_report(  # noqa: SIM117 - report must reserve first
        root,
        report_output,
        protected_roots=protected_roots,
        initial=prepared,
    ) as reservation:
        with _reserve_external_quarantine(root, plan) as reserved_quarantine:
            quarantine, quarantine_identity = reserved_quarantine
            terminal_observation: CurrentObservation | None = None
            try:
                with release_guard(root, plan.release_version, exclusive=True):
                    for target in plan.targets:
                        absolute = root / PurePosixPath(target.relative_path)
                        _, quarantined = _prepare_quarantine_slot(
                            quarantine, target
                        )
                        intent = QuarantineIntent(
                            source_relative_path=target.relative_path,
                            quarantine_slot=target.quarantine_slot,
                            quarantine_root_device=quarantine_identity[0],
                            quarantine_root_inode=quarantine_identity[1],
                            device=target.device,
                            inode=target.inode,
                            content_digest=target.content_digest,
                        )
                        _revalidate_target(root, target, evidence.entries)
                        _verify_target_bindings(root, target, evidence.entries)
                        reservation.publish(
                            _report_for(
                                plan,
                                "in_progress",
                                detach_events,
                                quarantine_root_identity=quarantine_identity,
                                source_root_identities=source_root_identities,
                                intent=intent,
                                active_target=target.relative_path,
                            )
                        )
                        try:
                            with (
                                _locked_output_ancestors(
                                    root, absolute.parent
                                ) as source_guard,
                                _locked_output_ancestors(
                                    quarantine, quarantined.parent
                                ) as quarantine_guard,
                            ):
                                _revalidate_target(root, target, evidence.entries)
                                _verify_target_bindings(root, target, evidence.entries)
                                _require_external_quarantine_binding(
                                    plan,
                                    root_created=True,
                                    expected_root_identity=quarantine_identity,
                                )
                                if os.name == "nt":
                                    _release_windows_directory_locks(source_guard)
                                    _release_windows_directory_locks(quarantine_guard)
                                try:
                                    event = _detach_target_to_quarantine(
                                        absolute,
                                        quarantined,
                                        target,
                                        plan_sha256=plan.plan_sha256,
                                        quarantine_root_identity=quarantine_identity,
                                    )
                                finally:
                                    if os.name == "nt":
                                        _acquire_windows_directory_locks(source_guard)
                                        _acquire_windows_directory_locks(
                                            quarantine_guard
                                        )
                        except (CleanupError, OSError, PosterError):
                            failed = _failed_report(
                                plan,
                                detach_events,
                                target.relative_path,
                                "detach transaction failed",
                                quarantine_root_identity=quarantine_identity,
                                source_root_identities=source_root_identities,
                                intent=intent,
                            )
                            reservation.publish(failed)
                            return failed
                        detach_events.append(event)
                        # The event checkpoint is the durable historical record. Current
                        # namespace/content state is deliberately sampled afterwards.
                        reservation.publish(
                            _report_for(
                                plan,
                                "in_progress",
                                detach_events,
                                quarantine_root_identity=quarantine_identity,
                                source_root_identities=source_root_identities,
                            )
                        )
                        observation = _observe_current_cleanup_state(
                            root,
                            plan.targets,
                            Path(plan.quarantine_root),
                            detach_events,
                            source_root_identities,
                            evidence.entries,
                            quarantine_identity,
                        )
                        observed = _report_for(
                            plan,
                            "in_progress",
                            detach_events,
                            quarantine_root_identity=quarantine_identity,
                            source_root_identities=source_root_identities,
                            current_observation=observation,
                        )
                        reservation.publish(observed)
                        if observation.assessment != "no_drift_observed":
                            terminal_observation = observation
                            break
            except ReleaseLockError:
                failed = _failed_report(
                    plan,
                    detach_events,
                    None,
                    "release guard failed",
                    quarantine_root_identity=quarantine_identity,
                    source_root_identities=source_root_identities,
                )
                reservation.publish(failed)
                return failed
            if terminal_observation is None:
                final_observation = _observe_current_cleanup_state(
                    root,
                    plan.targets,
                    Path(plan.quarantine_root),
                    detach_events,
                    source_root_identities,
                    evidence.entries,
                    quarantine_identity,
                )
            else:
                final_observation = terminal_observation
            final = _detached_report(
                plan,
                tuple(detach_events),
                quarantine_root_identity=quarantine_identity,
                source_root_identities=source_root_identities,
                current_observation=final_observation,
            )
            reservation.publish(final)
            return final


def write_cleanup_plan(
    plan: CleanupPlan,
    output: Path,
    *,
    repo_root: Path,
    protected_roots: Sequence[Path],
) -> None:
    """Atomically write a plan outside all raw/generated protected roots."""
    _require_exact_output(output, _PLAN_OUTPUT, "plan")
    validate_cleanup_plan(plan, repo_root=repo_root)
    _write_create_only_json_artifact(
        asdict(plan), output, repo_root=repo_root, protected_roots=protected_roots
    )


def load_cleanup_plan(
    path: Path,
    *,
    repo_root: Path,
    protected_roots: Sequence[Path],
) -> CleanupPlan:
    """Load one no-follow, single-link cleanup plan with an exact schema."""
    root = _workspace_root(repo_root)
    protected = _validate_source_roots(root, protected_roots)
    artifact = _exact_artifact_input(root, path, _PLAN_OUTPUT, protected, "cleanup plan")
    content = _read_bound_artifact(root, artifact, "cleanup plan")
    payload = _json_object(content, "cleanup plan")
    plan = _parse_cleanup_plan(payload)
    validate_cleanup_plan(plan, repo_root=root)
    return plan


def recover_cleanup_report_checkpoint(
    path: Path,
    *,
    repo_root: Path,
    protected_roots: Sequence[Path],
) -> dict[str, Any]:
    """Return the last complete, schema-valid record from the append-only report."""
    root = _workspace_root(repo_root)
    protected = _configured_source_root_paths(root, protected_roots)
    artifact = _exact_artifact_input(
        root, path, _REPORT_OUTPUT, protected, "cleanup report"
    )
    content = _read_bound_artifact(root, artifact, "cleanup report")
    recovered: dict[str, Any] | None = None
    for line in content.splitlines(keepends=True):
        if not line.endswith(b"\n"):
            break
        try:
            value = json.loads(line)
        except (UnicodeDecodeError, json.JSONDecodeError):
            break
        if (
            not isinstance(value, dict)
            or not _valid_report_checkpoint(value)
            or not _valid_report_transition(recovered, value)
        ):
            break
        recovered = cast(dict[str, Any], value)
    if recovered is None:
        raise CleanupError("cleanup report has no recoverable checkpoint")
    return recovered


def _valid_report_transition(
    previous: Mapping[str, Any] | None,
    current: Mapping[str, Any],
) -> bool:
    events = cast(list[Any], current["detach_events"])
    if previous is None:
        return (
            current.get("status") == "prepared"
            and events == []
            and current.get("intent") is None
            and current.get("current_observation") is None
            and current.get("quarantine_root_device") is None
            and current.get("quarantine_root_inode") is None
        )
    if previous.get("status") in {"detached", "detached_with_drift", "failed"}:
        return False
    dynamic_fields = {
        "active_target",
        "current_observation",
        "detach_events",
        "failed_target",
        "failure_reason",
        "intent",
        "quarantine_root_device",
        "quarantine_root_inode",
        "status",
    }
    if any(
        previous.get(name) != current.get(name)
        for name in _REPORT_FIELDS - dynamic_fields
    ):
        return False
    previous_device = previous.get("quarantine_root_device")
    previous_inode = previous.get("quarantine_root_inode")
    current_device = current.get("quarantine_root_device")
    current_inode = current.get("quarantine_root_inode")
    if previous_device is not None and (
        current_device != previous_device or current_inode != previous_inode
    ):
        return False
    previous_events = cast(list[Any], previous["detach_events"])
    if (
        len(events) < len(previous_events)
        or len(events) > len(previous_events) + 1
        or events[: len(previous_events)] != previous_events
    ):
        return False
    if len(events) == len(previous_events) + 1:
        intent = previous.get("intent")
        event = events[-1]
        if not isinstance(intent, dict) or not isinstance(event, dict):
            return False
        if (
            current.get("intent") is not None
            or current.get("current_observation") is not None
            or event.get("evidence_origin") != "executor"
            or event.get("source_relative_path") != intent.get("source_relative_path")
            or event.get("quarantine_slot") != intent.get("quarantine_slot")
            or event.get("content_digest") != intent.get("content_digest")
            or event.get("pre_move_root_device") != intent.get("device")
            or event.get("pre_move_root_inode") != intent.get("inode")
            or event.get("post_move_root_device") != intent.get("device")
            or event.get("post_move_root_inode") != intent.get("inode")
            or event.get("plan_sha256") != current.get("plan_sha256")
            or event.get("quarantine_root_device")
            != current.get("quarantine_root_device")
            or event.get("quarantine_root_inode")
            != current.get("quarantine_root_inode")
            or not _timestamps_ordered(
                intent.get("recorded_at"), event.get("rename_not_before")
            )
        ):
            return False
    elif isinstance(previous.get("intent"), dict):
        if (
            current.get("intent") != previous.get("intent")
            or current.get("active_target") != previous.get("active_target")
        ):
            return False
    elif isinstance(current.get("intent"), dict):
        intent = cast(dict[str, Any], current["intent"])
        targets = cast(list[Any], current["targets"])
        if len(events) >= len(targets):
            return False
        next_target = targets[len(events)]
        if (
            current.get("status") != "in_progress"
            or not isinstance(next_target, dict)
            or current.get("active_target") != intent.get("source_relative_path")
            or next_target.get("relative_path") != intent.get("source_relative_path")
            or next_target.get("quarantine_slot") != intent.get("quarantine_slot")
            or next_target.get("device") != intent.get("device")
            or next_target.get("inode") != intent.get("inode")
            or next_target.get("content_digest") != intent.get("content_digest")
        ):
            return False
    return True


def assess_cleanup_recovery(
    path: Path,
    *,
    repo_root: Path,
    protected_roots: Sequence[Path],
) -> CleanupRecovery:
    """Recover immutable detach events and take a fresh read-only observation."""
    checkpoint = recover_cleanup_report_checkpoint(
        path,
        repo_root=repo_root,
        protected_roots=protected_roots,
    )
    root = _workspace_root(repo_root)
    reported_quarantine_root = Path(
        cast(str, checkpoint.get("quarantine_root"))
    )
    source_identity_payload = checkpoint.get("source_root_identities")
    if not isinstance(source_identity_payload, list):
        raise CleanupError("cleanup recovery source root evidence is invalid")
    source_root_identities = tuple(
        _parse_external_directory(item) for item in source_identity_payload
    )
    configured_source_roots = _configured_source_root_paths(root, protected_roots)
    _require_recovery_source_root_identity_paths(
        configured_source_roots, source_root_identities
    )
    targets = checkpoint.get("targets")
    if not isinstance(targets, list):
        raise CleanupError("cleanup recovery target schema is invalid")
    parsed_targets = tuple(_parse_target(item) for item in targets)
    target_map = {target.relative_path: target for target in parsed_targets}
    entries = _recovery_inventory_entries(root, checkpoint)
    _validate_recovery_target_table(parsed_targets, entries)
    event_payload = checkpoint.get("detach_events")
    if not isinstance(event_payload, list):
        raise CleanupError("cleanup recovery detach event schema is invalid")
    detach_events = tuple(_parse_detach_event(item) for item in event_payload)
    _validate_recovery_detach_events(
        detach_events,
        parsed_targets,
        cast(str, checkpoint["plan_sha256"]),
        checkpoint.get("quarantine_root_device"),
        checkpoint.get("quarantine_root_inode"),
    )
    if (
        not reported_quarantine_root.is_absolute()
        or reported_quarantine_root == root
        or reported_quarantine_root.is_relative_to(root)
    ):
        raise CleanupError("cleanup recovery path binding is invalid")
    payload = checkpoint.get("intent")
    root_device = checkpoint.get("quarantine_root_device")
    root_inode = checkpoint.get("quarantine_root_inode")
    if payload is None and not detach_events:
        observation = _observe_current_cleanup_state(
            root,
            parsed_targets,
            reported_quarantine_root,
            (),
            source_root_identities,
            entries,
            (-1, -1),
        )
        return CleanupRecovery("no_event", None, (), observation)
    root_device = checkpoint.get("quarantine_root_device")
    root_inode = checkpoint.get("quarantine_root_inode")
    if type(root_device) is not int or type(root_inode) is not int:
        raise CleanupError("cleanup recovery quarantine root evidence is invalid")
    if payload is None:
        observation = _observe_current_cleanup_state(
            root,
            parsed_targets,
            reported_quarantine_root,
            detach_events,
            source_root_identities,
            entries,
            (int(root_device), int(root_inode)),
        )
        state: Literal["partial", "detached", "detached_with_drift"]
        if len(detach_events) != len(parsed_targets):
            state = "partial"
        else:
            state = (
                "detached"
                if observation.assessment == "no_drift_observed"
                else "detached_with_drift"
            )
        return CleanupRecovery(state, None, detach_events, observation)
    quarantine_root = _validate_recovery_quarantine_root(
        root, reported_quarantine_root, require_present=True
    )
    if os.path.normcase(str(quarantine_root)) != os.path.normcase(
        str(reported_quarantine_root)
    ):
        raise CleanupError("cleanup recovery quarantine root binding is invalid")
    try:
        quarantine_stat = quarantine_root.lstat()
    except OSError:
        return CleanupRecovery("conflict", None, detach_events, None)
    if (
        not stat.S_ISDIR(quarantine_stat.st_mode)
        or _is_reparse(quarantine_stat)
        or (quarantine_stat.st_dev, quarantine_stat.st_ino)
        != (root_device, root_inode)
    ):
        return CleanupRecovery("conflict", None, detach_events, None)
    intent = _parse_quarantine_intent(payload)
    active_target = intent.source_relative_path
    target = target_map.get(intent.source_relative_path)
    if target is None or (
        target.quarantine_slot != intent.quarantine_slot
        or target.device != intent.device
        or target.inode != intent.inode
        or target.content_digest != intent.content_digest
    ):
        raise CleanupError("cleanup recovery intent proof is invalid")
    source = root / PurePosixPath(intent.source_relative_path)
    slot_path = reported_quarantine_root / PurePosixPath(intent.quarantine_slot)
    source_identity = _path_identity_state(source, intent.device, intent.inode)
    slot_identity = _path_identity_state(slot_path, intent.device, intent.inode)
    if "unreadable" in {source_identity, slot_identity}:
        return CleanupRecovery("conflict", active_target, detach_events, None)
    if source_identity == "matches" and slot_identity == "missing":
        return CleanupRecovery("not_detached", active_target, detach_events, None)
    if source_identity == "matches" or slot_identity != "matches":
        return CleanupRecovery("conflict", active_target, detach_events, None)
    inferred_observed_by = datetime.now().astimezone().isoformat()
    if not _timestamps_ordered(intent.recorded_at, inferred_observed_by):
        raise CleanupError("cleanup recovery intent time is invalid")
    inferred = DetachEvent(
        event_type="detached_to_quarantine",
        evidence_origin="recovery_inference",
        source_relative_path=target.relative_path,
        quarantine_slot=target.quarantine_slot,
        plan_sha256=cast(str, checkpoint["plan_sha256"]),
        content_digest=target.content_digest,
        quarantine_root_device=int(root_device),
        quarantine_root_inode=int(root_inode),
        pre_move_root_device=target.device,
        pre_move_root_inode=target.inode,
        post_move_root_device=target.device,
        post_move_root_inode=target.inode,
        rename_not_before=intent.recorded_at,
        rename_observed_by=inferred_observed_by,
    )
    if target.relative_path in {event.source_relative_path for event in detach_events}:
        raise CleanupError("cleanup recovery detach events are contradictory")
    detach_events = (*detach_events, inferred)
    observation = _observe_current_cleanup_state(
        root,
        parsed_targets,
        reported_quarantine_root,
        detach_events,
        source_root_identities,
        entries,
        (int(root_device), int(root_inode)),
    )
    recovery_state: Literal["partial", "detached", "detached_with_drift"]
    if len(detach_events) != len(parsed_targets):
        recovery_state = "partial"
    else:
        recovery_state = (
            "detached"
            if observation.assessment == "no_drift_observed"
            else "detached_with_drift"
        )
    return CleanupRecovery(recovery_state, active_target, detach_events, observation)


def _path_identity_state(
    path: Path, device: int, inode: int
) -> Literal["matches", "different", "missing", "unreadable"]:
    try:
        value = path.lstat()
    except FileNotFoundError:
        return "missing"
    except OSError:
        return "unreadable"
    return "matches" if (value.st_dev, value.st_ino) == (device, inode) else "different"


def _validate_recovery_detach_events(
    events: Sequence[DetachEvent],
    targets: Sequence[CleanupTarget],
    plan_sha256: str,
    quarantine_root_device: object,
    quarantine_root_inode: object,
) -> None:
    target_map = {target.relative_path: target for target in targets}
    names: list[str] = []
    for event in events:
        target = target_map.get(event.source_relative_path)
        if target is None or (
            event.quarantine_slot != target.quarantine_slot
            or event.plan_sha256 != plan_sha256
            or event.content_digest != target.content_digest
            or (event.pre_move_root_device, event.pre_move_root_inode)
            != (target.device, target.inode)
            or (event.post_move_root_device, event.post_move_root_inode)
            != (target.device, target.inode)
            or event.quarantine_root_device != quarantine_root_device
            or event.quarantine_root_inode != quarantine_root_inode
        ):
            raise CleanupError("cleanup recovery detach event binding is invalid")
        names.append(event.source_relative_path)
    expected = [target.relative_path for target in targets]
    if len(names) != len(set(names)) or names != expected[: len(names)]:
        raise CleanupError("cleanup recovery detach events are contradictory")


def _validate_recovery_target_table(
    targets: Sequence[CleanupTarget], entries: Sequence[InventoryEntry]
) -> None:
    expected_paths = _expected_target_paths(entries)
    if tuple(target.relative_path for target in targets) != expected_paths:
        raise CleanupError("cleanup recovery target table is invalid")
    entry_map = {entry.relative_path: entry for entry in entries}
    for index, target in enumerate(targets):
        expected_slot = f"{index:04d}/{target.relative_path}"
        slot = PurePosixPath(target.quarantine_slot)
        try:
            _valid_relative_path(target.relative_path)
            _valid_relative_path(target.quarantine_slot)
        except ArchiveError as exc:
            raise CleanupError("cleanup recovery slot binding is invalid") from exc
        if (
            target.quarantine_slot != expected_slot
            or slot.is_absolute()
            or ".." in slot.parts
            or len(slot.parts) < 2
            or PurePosixPath(*slot.parts[1:])
            != PurePosixPath(target.relative_path)
        ):
            raise CleanupError("cleanup recovery slot binding is invalid")
        logical = PurePosixPath(target.relative_path)
        expected_sources = tuple(
            sorted(
                path
                for path in entry_map
                if PurePosixPath(path) == logical
                or PurePosixPath(path).is_relative_to(logical)
            )
        )
        source_paths = tuple(source.relative_path for source in target.sources)
        if source_paths != expected_sources or len(source_paths) != len(set(source_paths)):
            raise CleanupError("cleanup recovery target source table is invalid")
        for source in target.sources:
            inventory_entry = entry_map[source.relative_path]
            if (
                source.sha256 != inventory_entry.sha256
                or source.size_bytes != inventory_entry.size_bytes
            ):
                raise CleanupError("cleanup recovery target source proof is invalid")
        recomputed_digest = hashlib.sha256(
            _compact_json([asdict(source) for source in target.sources]).encode()
        ).hexdigest()
        if (
            target.file_count != len(target.sources)
            or target.total_bytes != sum(source.size_bytes for source in target.sources)
            or target.content_digest != recomputed_digest
        ):
            raise CleanupError("cleanup recovery target content proof is invalid")
        directory_paths = tuple(item.relative_path for item in target.directories)
        if len(directory_paths) != len(set(directory_paths)) or any(
            not (
                PurePosixPath(path) == logical
                or PurePosixPath(path).is_relative_to(logical)
            )
            for path in directory_paths
        ):
            raise CleanupError("cleanup recovery target directory table is invalid")
        if target.kind == "directory":
            roots = [
                item
                for item in target.directories
                if item.relative_path == target.relative_path
            ]
            if len(roots) != 1 or (roots[0].device, roots[0].inode) != (
                target.device,
                target.inode,
            ):
                raise CleanupError("cleanup recovery target root proof is invalid")
        elif target.directories or len(target.sources) != 1 or (
            target.sources[0].device,
            target.sources[0].inode,
        ) != (target.device, target.inode):
            raise CleanupError("cleanup recovery target root proof is invalid")


def _recovery_inventory_entries(
    root: Path, checkpoint: Mapping[str, Any]
) -> tuple[InventoryEntry, ...]:
    inventory = _parse_evidence(checkpoint.get("inventory"))
    try:
        _valid_relative_path(inventory.relative_path)
    except ArchiveError as exc:
        raise CleanupError("cleanup recovery inventory path is invalid") from exc
    content = _read_bound_artifact(
        root,
        root / PurePosixPath(inventory.relative_path),
        "cleanup recovery inventory",
    )
    if hashlib.sha256(content).hexdigest() != inventory.sha256:
        raise CleanupError("cleanup recovery inventory digest changed")
    return _parse_inventory(content)


def _parse_quarantine_intent(value: object) -> QuarantineIntent:
    if not isinstance(value, dict) or not _valid_intent_payload(value):
        raise CleanupError("cleanup quarantine intent schema is invalid")
    return QuarantineIntent(
        source_relative_path=_strict_string(
            value, "source_relative_path", "cleanup quarantine intent schema"
        ),
        quarantine_slot=_strict_string(
            value, "quarantine_slot", "cleanup quarantine intent schema"
        ),
        quarantine_root_device=_strict_int(
            value,
            "quarantine_root_device",
            "cleanup quarantine intent schema",
        ),
        quarantine_root_inode=_strict_int(
            value,
            "quarantine_root_inode",
            "cleanup quarantine intent schema",
        ),
        device=_strict_int(value, "device", "cleanup quarantine intent schema"),
        inode=_strict_int(value, "inode", "cleanup quarantine intent schema"),
        content_digest=_strict_sha(
            value, "content_digest", "cleanup quarantine intent schema"
        ),
        recorded_at=_strict_string(
            value, "recorded_at", "cleanup quarantine intent schema"
        ),
    )


def _parse_detach_event(value: object) -> DetachEvent:
    if not _valid_detach_event_payload(value):
        raise CleanupError("cleanup detach event schema is invalid")
    assert isinstance(value, dict)
    return DetachEvent(
        event_type="detached_to_quarantine",
        evidence_origin=cast(
            Literal["executor", "recovery_inference"], value["evidence_origin"]
        ),
        source_relative_path=cast(str, value["source_relative_path"]),
        quarantine_slot=cast(str, value["quarantine_slot"]),
        plan_sha256=cast(str, value["plan_sha256"]),
        content_digest=cast(str, value["content_digest"]),
        quarantine_root_device=cast(int, value["quarantine_root_device"]),
        quarantine_root_inode=cast(int, value["quarantine_root_inode"]),
        pre_move_root_device=cast(int, value["pre_move_root_device"]),
        pre_move_root_inode=cast(int, value["pre_move_root_inode"]),
        post_move_root_device=cast(int, value["post_move_root_device"]),
        post_move_root_inode=cast(int, value["post_move_root_inode"]),
        rename_not_before=cast(str, value["rename_not_before"]),
        rename_observed_by=cast(str, value["rename_observed_by"]),
    )


def plan_to_json(plan: CleanupPlan) -> str:
    return _compact_json(asdict(plan)) + "\n"


def report_to_json(report: CleanupReport) -> str:
    return _compact_json(asdict(report)) + "\n"


def _detached_report(
    plan: CleanupPlan,
    detach_events: tuple[DetachEvent, ...],
    *,
    quarantine_root_identity: tuple[int, int],
    source_root_identities: Sequence[ExternalDirectoryBinding],
    current_observation: CurrentObservation,
) -> CleanupReport:
    status: Literal["detached", "detached_with_drift"] = (
        "detached"
        if current_observation.assessment == "no_drift_observed"
        else "detached_with_drift"
    )
    return _report_for(
        plan,
        status,
        detach_events,
        quarantine_root_identity=quarantine_root_identity,
        source_root_identities=source_root_identities,
        current_observation=current_observation,
    )


def _failed_report(
    plan: CleanupPlan,
    detach_events: Sequence[DetachEvent],
    failed_target: str | None,
    reason: str,
    *,
    quarantine_root_identity: tuple[int, int],
    source_root_identities: Sequence[ExternalDirectoryBinding],
    intent: QuarantineIntent | None = None,
    current_observation: CurrentObservation | None = None,
) -> CleanupReport:
    return _report_for(
        plan,
        "failed",
        detach_events,
        quarantine_root_identity=quarantine_root_identity,
        source_root_identities=source_root_identities,
        intent=intent,
        current_observation=current_observation,
        failed_target=failed_target,
        failure_reason=reason,
    )


def _report_for(
    plan: CleanupPlan,
    status: Literal[
        "prepared", "in_progress", "detached", "detached_with_drift", "failed"
    ],
    detach_events: Sequence[DetachEvent],
    *,
    source_root_identities: Sequence[ExternalDirectoryBinding],
    quarantine_root_identity: tuple[int, int] | None = None,
    intent: QuarantineIntent | None = None,
    active_target: str | None = None,
    failed_target: str | None = None,
    failure_reason: str | None = None,
    current_observation: CurrentObservation | None = None,
) -> CleanupReport:
    if quarantine_root_identity is None and intent is not None:
        quarantine_root_identity = (
            intent.quarantine_root_device,
            intent.quarantine_root_inode,
        )
    return CleanupReport(
        schema_version="cleanup-report/v4",
        release_version=plan.release_version,
        plan_sha256=plan.plan_sha256,
        inventory=plan.inventory,
        duplicate_proof=plan.duplicate_proof,
        archive_verification=plan.archive_verification,
        inventory_digest=plan.inventory_digest,
        bucket=plan.bucket,
        prefix=plan.prefix,
        quarantine_root=plan.quarantine_root,
        quarantine_root_device=(
            None
            if quarantine_root_identity is None
            else quarantine_root_identity[0]
        ),
        quarantine_root_inode=(
            None
            if quarantine_root_identity is None
            else quarantine_root_identity[1]
        ),
        source_root_identities=tuple(source_root_identities),
        targets=plan.targets,
        status=status,
        total_targets=len(plan.targets),
        detach_events=tuple(detach_events),
        current_observation=current_observation,
        rag_readiness="not_authorized",
        intent=intent,
        active_target=active_target,
        failed_target=failed_target,
        failure_reason=failure_reason,
    )


def _valid_report_checkpoint(value: Mapping[str, Any]) -> bool:
    root_device = value.get("quarantine_root_device")
    root_inode = value.get("quarantine_root_inode")
    root_identity_is_absent = root_device is None and root_inode is None
    root_identity_is_bound = type(root_device) is int and type(root_inode) is int
    source_root_identities = value.get("source_root_identities")
    intent = value.get("intent")
    return (
        set(value) == _REPORT_FIELDS
        and value.get("schema_version") == "cleanup-report/v4"
        and value.get("release_version") == "v1.2"
        and isinstance(value.get("plan_sha256"), str)
        and _SHA256.fullmatch(cast(str, value["plan_sha256"])) is not None
        and isinstance(value.get("targets"), list)
        and isinstance(value.get("total_targets"), int)
        and not isinstance(value.get("total_targets"), bool)
        and cast(int, value["total_targets"]) == len(cast(list[Any], value["targets"]))
        and isinstance(value.get("detach_events"), list)
        and all(_valid_detach_event_payload(item) for item in cast(list[Any], value["detach_events"]))
        and _valid_current_observation_payload(value.get("current_observation"))
        and value.get("rag_readiness") == "not_authorized"
        and isinstance(value.get("quarantine_root"), str)
        and isinstance(source_root_identities, list)
        and len(source_root_identities) == len(_SOURCE_ROOTS)
        and all(
            _valid_external_directory_payload(item)
            for item in source_root_identities
        )
        and (root_identity_is_absent or root_identity_is_bound)
        and value.get("status")
        in {"prepared", "in_progress", "detached", "detached_with_drift", "failed"}
        and _valid_intent_payload(intent)
        and (
            not isinstance(intent, dict)
            or (
                intent.get("quarantine_root_device") == root_device
                and intent.get("quarantine_root_inode") == root_inode
            )
        )
        and (
            value.get("status") == "prepared"
            and root_identity_is_absent
            or value.get("status") != "prepared"
            and root_identity_is_bound
        )
        and (
            value.get("active_target") is None
            or isinstance(value.get("active_target"), str)
        )
        and (
            value.get("failed_target") is None
            or isinstance(value.get("failed_target"), str)
        )
        and (
            value.get("failure_reason") is None
            or isinstance(value.get("failure_reason"), str)
        )
        and _valid_report_state(value)
    )


def _valid_report_state(value: Mapping[str, Any]) -> bool:
    status = value.get("status")
    events = value.get("detach_events")
    observation = value.get("current_observation")
    if not isinstance(events, list):
        return False
    if status == "detached":
        return (
            len(events) == value.get("total_targets")
            and value.get("intent") is None
            and value.get("active_target") is None
            and value.get("failed_target") is None
            and value.get("failure_reason") is None
            and isinstance(observation, dict)
            and observation.get("assessment") == "no_drift_observed"
            and _observation_matches_report(value, observation)
        )
    if status == "detached_with_drift":
        return (
            len(events) > 0
            and value.get("intent") is None
            and value.get("active_target") is None
            and value.get("failed_target") is None
            and value.get("failure_reason") is None
            and isinstance(observation, dict)
            and observation.get("assessment")
            in {"drift_observed", "indeterminate"}
            and _observation_matches_report(value, observation)
        )
    return True


def _observation_matches_report(
    report: Mapping[str, Any], observation: Mapping[str, Any]
) -> bool:
    source_roots = report.get("source_root_identities")
    observed_roots = observation.get("source_roots")
    events = report.get("detach_events")
    observed_targets = observation.get("targets")
    if not all(
        isinstance(item, list)
        for item in (source_roots, observed_roots, events, observed_targets)
    ):
        return False
    assert isinstance(source_roots, list)
    assert isinstance(observed_roots, list)
    assert isinstance(events, list)
    assert isinstance(observed_targets, list)
    return (
        [item.get("path") for item in observed_roots if isinstance(item, dict)]
        == [item.get("path") for item in source_roots if isinstance(item, dict)]
        and [
            (item.get("source_relative_path"), item.get("quarantine_slot"))
            for item in observed_targets
            if isinstance(item, dict)
        ]
        == [
            (item.get("source_relative_path"), item.get("quarantine_slot"))
            for item in events
            if isinstance(item, dict)
        ]
        and len(observed_roots) == len(source_roots)
        and len(observed_targets) == len(events)
    )


def _valid_intent_payload(value: object) -> bool:
    if value is None:
        return True
    if not isinstance(value, dict):
        return False
    return (
        set(value)
        == {
            "source_relative_path",
            "quarantine_slot",
            "quarantine_root_device",
            "quarantine_root_inode",
            "device",
            "inode",
            "content_digest",
            "recorded_at",
        }
        and isinstance(value.get("source_relative_path"), str)
        and isinstance(value.get("quarantine_slot"), str)
        and type(value.get("quarantine_root_device")) is int
        and type(value.get("quarantine_root_inode")) is int
        and type(value.get("device")) is int
        and type(value.get("inode")) is int
        and isinstance(value.get("content_digest"), str)
        and _SHA256.fullmatch(cast(str, value["content_digest"])) is not None
        and _valid_timestamp(value.get("recorded_at"))
    )


def _valid_timestamp(value: object) -> bool:
    if not isinstance(value, str):
        return False
    try:
        parsed = datetime.fromisoformat(value)
    except ValueError:
        return False
    return parsed.tzinfo is not None and parsed.utcoffset() is not None


def _timestamps_ordered(start: object, end: object) -> bool:
    if not _valid_timestamp(start) or not _valid_timestamp(end):
        return False
    assert isinstance(start, str) and isinstance(end, str)
    return datetime.fromisoformat(start) <= datetime.fromisoformat(end)


def _valid_detach_event_payload(value: object) -> bool:
    if not isinstance(value, dict):
        return False
    fields = {
        "event_type",
        "evidence_origin",
        "source_relative_path",
        "quarantine_slot",
        "plan_sha256",
        "content_digest",
        "quarantine_root_device",
        "quarantine_root_inode",
        "pre_move_root_device",
        "pre_move_root_inode",
        "post_move_root_device",
        "post_move_root_inode",
        "rename_not_before",
        "rename_observed_by",
    }
    return (
        set(value) == fields
        and value.get("event_type") == "detached_to_quarantine"
        and value.get("evidence_origin") in {"executor", "recovery_inference"}
        and isinstance(value.get("source_relative_path"), str)
        and isinstance(value.get("quarantine_slot"), str)
        and isinstance(value.get("plan_sha256"), str)
        and _SHA256.fullmatch(cast(str, value["plan_sha256"])) is not None
        and isinstance(value.get("content_digest"), str)
        and _SHA256.fullmatch(cast(str, value["content_digest"])) is not None
        and all(type(value.get(name)) is int for name in fields if name.endswith(("device", "inode")))
        and _timestamps_ordered(
            value.get("rename_not_before"), value.get("rename_observed_by")
        )
    )


def _valid_current_observation_payload(value: object) -> bool:
    if value is None:
        return True
    if not isinstance(value, dict):
        return False
    if set(value) != {
        "observed_at_start",
        "observed_at_end",
        "atomic",
        "assessment",
        "source_roots",
        "targets",
    }:
        return False
    roots = value.get("source_roots")
    targets = value.get("targets")
    return (
        _timestamps_ordered(
            value.get("observed_at_start"), value.get("observed_at_end")
        )
        and value.get("atomic") is False
        and value.get("assessment")
        in {"no_drift_observed", "drift_observed", "indeterminate"}
        and isinstance(roots, list)
        and all(
            isinstance(item, dict)
            and set(item) == {"path", "state"}
            and isinstance(item.get("path"), str)
            and item.get("state")
            in {
                "matches_recorded_identity",
                "detached_root_absent",
                "missing",
                "identity_changed",
                "unsafe",
                "unreadable",
            }
            for item in roots
        )
        and isinstance(targets, list)
        and all(
            isinstance(item, dict)
            and set(item)
            == {"source_relative_path", "quarantine_slot", "source_state", "slot_state"}
            and isinstance(item.get("source_relative_path"), str)
            and isinstance(item.get("quarantine_slot"), str)
            and item.get("source_state")
            in {"absent", "recreated", "original_present", "unreadable"}
            and item.get("slot_state")
            in {
                "matches_plan",
                "content_drift",
                "root_identity_changed",
                "missing",
                "unsafe",
                "unreadable",
            }
            for item in targets
        )
    )


def _valid_external_directory_payload(value: object) -> bool:
    return (
        isinstance(value, dict)
        and set(value) == {"path", "device", "inode"}
        and isinstance(value.get("path"), str)
        and type(value.get("device")) is int
        and type(value.get("inode")) is int
    )


def _validate_static_plan(plan: CleanupPlan) -> None:
    if (
        plan.schema_version != "cleanup-plan/v2"
        or plan.verified is not True
        or plan.release_version != "v1.2"
        or plan.source_roots != _SOURCE_ROOTS
        or plan.bucket != _V12_AUTHORITY.bucket
        or plan.prefix != _V12_AUTHORITY.prefix
        or plan.object_count != _V12_AUTHORITY.object_count
        or plan.total_bytes != _V12_AUTHORITY.total_bytes
        or plan.inventory_digest != _V12_AUTHORITY.inventory_digest
    ):
        raise CleanupError("cleanup plan authority mismatch")
    for evidence in (plan.inventory, plan.duplicate_proof, plan.archive_verification):
        try:
            _valid_relative_path(evidence.relative_path)
        except ArchiveError as exc:
            raise CleanupError("cleanup plan evidence path is invalid") from exc
        if _is_below_source_root(evidence.relative_path):
            raise CleanupError("cleanup plan evidence path is under a protected source root")


def _validate_plan_workspace(plan: CleanupPlan, root: Path) -> None:
    supplied = Path(plan.workspace_root)
    if not supplied.is_absolute():
        raise CleanupError("cleanup plan workspace root mismatch")
    lexical = Path(os.path.abspath(supplied))
    if os.path.normcase(str(lexical)) != os.path.normcase(str(root)):
        raise CleanupError("cleanup plan workspace root mismatch")
    try:
        root_stat = root.lstat()
    except OSError as exc:
        raise CleanupError("cleanup workspace root is unreadable") from exc
    if (root_stat.st_dev, root_stat.st_ino) != (
        plan.workspace_device,
        plan.workspace_inode,
    ):
        raise CleanupError("cleanup plan workspace physical identity mismatch")


def _validate_target_allowlist(plan: CleanupPlan) -> None:
    allowed = set(_DIRECTORY_TARGETS)
    allowed.update(
        target.relative_path
        for target in plan.targets
        if PurePosixPath(target.relative_path).name == ".DS_Store"
        and _is_below_source_root(target.relative_path)
    )
    for target in plan.targets:
        if target.relative_path not in allowed or target.relative_path in {"", "."}:
            raise CleanupError("cleanup target is not allowlisted")


def _load_cleanup_evidence(
    root: Path,
    source_roots: Sequence[Path],
    inventory_path: Path,
    duplicate_proof_path: Path,
    archive_verification_path: Path,
) -> _CleanupEvidence:
    roots = _validate_source_roots(root, source_roots)
    inventory = _artifact_input(root, inventory_path, roots, "inventory")
    duplicate = _artifact_input(root, duplicate_proof_path, roots, "duplicate proof")
    archive = _artifact_input(
        root, archive_verification_path, roots, "archive verification"
    )
    inventory_bytes = _read_bound_artifact(root, inventory, "inventory")
    inventory_digest = hashlib.sha256(inventory_bytes).hexdigest()
    if inventory_digest != _V12_AUTHORITY.inventory_digest:
        raise CleanupError("inventory digest mismatch")
    entries = _parse_inventory(inventory_bytes)
    if len(entries) != _V12_AUTHORITY.object_count:
        raise CleanupError("inventory object count mismatch")
    if sum(entry.size_bytes for entry in entries) != _V12_AUTHORITY.total_bytes:
        raise CleanupError("inventory byte total mismatch")
    duplicate_bytes = _read_bound_artifact(root, duplicate, "duplicate proof")
    _verify_duplicate_proof(entries, duplicate_bytes)
    archive_bytes = _read_bound_artifact(root, archive, "archive verification")
    archive_sha256 = hashlib.sha256(archive_bytes).hexdigest()
    if archive_sha256 != _V12_AUTHORITY.archive_report_sha256:
        raise CleanupError("archive verification artifact SHA-256 mismatch")
    _verify_archive_report(entries, archive_bytes)
    return _CleanupEvidence(
        entries=entries,
        inventory_bytes=inventory_bytes,
        inventory=_evidence_binding(root, inventory, inventory_digest),
        duplicate_proof=_evidence_binding(
            root, duplicate, hashlib.sha256(duplicate_bytes).hexdigest()
        ),
        archive_verification=_evidence_binding(root, archive, archive_sha256),
    )


def _parse_inventory(content: bytes) -> tuple[InventoryEntry, ...]:
    try:
        text = content.decode("utf-8")
    except UnicodeDecodeError as exc:
        raise CleanupError("inventory is not UTF-8") from exc
    lines = text.splitlines()
    if not lines or any(not line for line in lines):
        raise CleanupError("inventory schema is invalid")
    entries: list[InventoryEntry] = []
    seen: set[str] = set()
    for line in lines:
        payload = _json_object(line.encode(), "inventory entry")
        _require_keys(payload, _INVENTORY_FIELDS, "inventory schema")
        relative_path = _strict_string(payload, "relative_path", "inventory schema")
        try:
            _valid_relative_path(relative_path)
        except ArchiveError as exc:
            raise CleanupError("inventory path is invalid") from exc
        if not _is_below_source_root(relative_path):
            raise CleanupError("inventory path is outside configured source roots")
        canonical = relative_path.casefold()
        if canonical in seen:
            raise CleanupError("inventory contains duplicate paths")
        seen.add(canonical)
        size = _strict_int(payload, "size_bytes", "inventory schema")
        sha256 = _strict_sha(payload, "sha256", "inventory schema")
        classification = _strict_string(payload, "classification", "inventory schema")
        if classification not in _CLASSIFICATIONS or size < 0:
            raise CleanupError("inventory schema is invalid")
        entries.append(
            InventoryEntry(
                relative_path=relative_path,
                size_bytes=size,
                sha256=sha256,
                classification=cast(Any, classification),
            )
        )
    if [entry.relative_path for entry in entries] != sorted(
        entry.relative_path for entry in entries
    ):
        raise CleanupError("inventory is not sorted")
    return tuple(entries)


def _verify_duplicate_proof(entries: Sequence[InventoryEntry], content: bytes) -> None:
    nested = _entry_map(entries, "1-1500posters/1-1500posters/", direct=False)
    outer = _entry_map(entries, "1-1500posters/", direct=True)
    posters1 = _entry_map(entries, "posters1/", direct=False)
    canonical = _entry_map(entries, "posters/posters/", direct=False)
    if nested != outer or any(canonical.get(name) != value for name, value in posters1.items()):
        raise CleanupError("duplicate proof recomputation failed")
    if (len(nested), len(posters1)) != _V12_AUTHORITY.mirror_counts:
        raise CleanupError("duplicate proof recomputation failed")
    payload = _json_object(content, "duplicate proof")
    _require_keys(payload, {"pairs"}, "duplicate proof")
    expected = {
        "pairs": [
            {
                "canonical_root": "1-1500posters",
                "duplicate_root": "nested_1-1500posters",
                "file_count": _V12_AUTHORITY.mirror_counts[0],
            },
            {
                "canonical_root": "posters/posters",
                "duplicate_root": "posters1",
                "file_count": _V12_AUTHORITY.mirror_counts[1],
            },
        ]
    }
    if payload != expected:
        raise CleanupError("duplicate proof artifact mismatch")


def _entry_map(
    entries: Sequence[InventoryEntry], prefix: str, *, direct: bool
) -> dict[str, tuple[int, str]]:
    result: dict[str, tuple[int, str]] = {}
    for entry in entries:
        if not entry.relative_path.startswith(prefix):
            continue
        relative = entry.relative_path.removeprefix(prefix)
        if direct and "/" in relative:
            continue
        result[relative] = (entry.size_bytes, entry.sha256)
    return result


def _verify_archive_report(entries: Sequence[InventoryEntry], content: bytes) -> None:
    report = _json_object(content, "archive verification report")
    _require_keys(report, _ARCHIVE_REPORT_FIELDS, "archive verification report schema")
    if report.get("verified") is not True:
        raise CleanupError("archive is not verified")
    if report.get("bucket") != _V12_AUTHORITY.bucket or report.get("prefix") != _V12_AUTHORITY.prefix:
        raise CleanupError("archive verification authority mismatch")
    if report.get("inventory_digest") != _V12_AUTHORITY.inventory_digest:
        raise CleanupError("archive verification inventory digest mismatch")
    if (
        type(report.get("object_count")) is not int
        or report["object_count"] != _V12_AUTHORITY.object_count
        or type(report.get("total_bytes")) is not int
        or report["total_bytes"] != _V12_AUTHORITY.total_bytes
    ):
        raise CleanupError("archive verification counts mismatch")
    timestamp = report.get("verification_timestamp")
    if not isinstance(timestamp, str):
        raise CleanupError("archive verification report schema is invalid")
    try:
        parsed_timestamp = datetime.fromisoformat(timestamp)
    except ValueError as exc:
        raise CleanupError("archive verification report schema is invalid") from exc
    if parsed_timestamp.tzinfo is None or parsed_timestamp.utcoffset() is None:
        raise CleanupError("archive verification report schema is invalid")
    objects = report.get("objects")
    if not isinstance(objects, list):
        raise CleanupError("archive verification report schema is invalid")
    expected = {
        f"{_V12_AUTHORITY.prefix}/files/{entry.relative_path}": entry
        for entry in entries
    }
    observed: set[str] = set()
    for item in objects:
        if not isinstance(item, dict):
            raise CleanupError("archive verification object schema is invalid")
        _require_keys(item, _ARCHIVE_OBJECT_FIELDS, "archive verification object schema")
        object_name = item.get("object_name")
        if not isinstance(object_name, str) or object_name in observed:
            raise CleanupError("archive verification object set mismatch")
        observed.add(object_name)
        entry = expected.get(object_name)
        if entry is None:
            raise CleanupError("archive verification object set mismatch")
        if item.get("action") != "verified" or item.get("hash_verified") is not True:
            raise CleanupError("archive verification object schema is invalid")
        generation = item.get("generation")
        if type(generation) is not int or generation <= 0:
            raise CleanupError("archive verification generation is invalid")
        if item.get("sha256") != entry.sha256:
            raise CleanupError("archive verification inventory SHA mismatch")
    if observed != set(expected) or len(objects) != len(expected):
        raise CleanupError("archive verification object set mismatch")


def _expected_target_paths(entries: Sequence[InventoryEntry]) -> tuple[str, ...]:
    targets = set(_DIRECTORY_TARGETS)
    for entry in entries:
        if (
            PurePosixPath(entry.relative_path).name == ".DS_Store"
            and entry.classification == "macos_metadata"
            and _is_below_source_root(entry.relative_path)
        ):
            targets.add(entry.relative_path)
    return tuple(sorted(targets))


def _capture_target(
    root: Path, relative_path: str, entries: Sequence[InventoryEntry]
) -> CleanupTarget:
    if relative_path not in set(_expected_target_paths(entries)):
        raise CleanupError("cleanup target is not allowlisted")
    path = root / PurePosixPath(relative_path)
    _assert_no_reparse_path(root, path)
    try:
        target_stat = path.lstat()
    except OSError as exc:
        raise CleanupError("cleanup target is missing") from exc
    expected_kind = "directory" if relative_path in _DIRECTORY_TARGETS else "file"
    if _is_reparse(target_stat):
        raise CleanupError("cleanup target violates no-follow containment")
    if expected_kind == "directory" and not stat.S_ISDIR(target_stat.st_mode):
        raise CleanupError("cleanup target type changed")
    if expected_kind == "file" and not stat.S_ISREG(target_stat.st_mode):
        raise CleanupError("cleanup target type changed")
    try:
        ancestors = _capture_ancestor_identities(root, path.parent)
    except PosterError as exc:
        raise CleanupError("cleanup target ancestor is unsafe") from exc
    expected_entries = {
        entry.relative_path: entry
        for entry in entries
        if entry.relative_path == relative_path
        or entry.relative_path.startswith(relative_path.rstrip("/") + "/")
    }
    if not expected_entries:
        raise CleanupError("cleanup target has no inventory evidence")
    directories: list[DirectoryBinding] = []
    sources: list[SourceBinding] = []
    if expected_kind == "directory":
        pending = [path]
        while pending:
            directory = pending.pop()
            try:
                directory_stat = directory.lstat()
            except OSError as exc:
                raise CleanupError("cleanup target is unreadable") from exc
            if not stat.S_ISDIR(directory_stat.st_mode) or _is_reparse(directory_stat):
                raise CleanupError("cleanup target violates no-follow containment")
            directories.append(_directory_binding(root, directory, directory_stat))
            try:
                with os.scandir(directory) as iterator:
                    children = sorted(iterator, key=lambda item: item.name)
            except OSError as exc:
                raise CleanupError("cleanup target is unreadable") from exc
            for child in children:
                child_path = Path(child.path)
                try:
                    child_stat = child.stat(follow_symlinks=False)
                except OSError as exc:
                    raise CleanupError("cleanup target is unreadable") from exc
                if _is_reparse(child_stat):
                    raise CleanupError("cleanup target violates no-follow containment")
                if stat.S_ISDIR(child_stat.st_mode):
                    pending.append(child_path)
                elif stat.S_ISREG(child_stat.st_mode):
                    sources.append(_source_binding(root, child_path, expected_entries))
                else:
                    raise CleanupError("cleanup target contains a special file")
    else:
        sources.append(_source_binding(root, path, expected_entries))
    sources.sort(key=lambda item: item.relative_path)
    directories.sort(key=lambda item: item.relative_path)
    if {source.relative_path for source in sources} != set(expected_entries):
        raise CleanupError("cleanup target differs from inventory")
    try:
        _require_ancestor_identities(root, path.parent, ancestors)
    except PosterError as exc:
        raise CleanupError("cleanup target ancestor identity changed") from exc
    try:
        final = path.lstat()
    except OSError as exc:
        raise CleanupError("cleanup target changed during binding") from exc
    if _node_identity(final) != _node_identity(target_stat):
        raise CleanupError("cleanup target changed during binding")
    content_digest = hashlib.sha256(
        _compact_json([asdict(source) for source in sources]).encode()
    ).hexdigest()
    target = CleanupTarget(
        relative_path=relative_path,
        quarantine_slot="",
        kind=cast(Literal["directory", "file"], expected_kind),
        device=target_stat.st_dev,
        inode=target_stat.st_ino,
        node_size_bytes=target_stat.st_size,
        modified_ns=target_stat.st_mtime_ns,
        link_count=target_stat.st_nlink,
        file_count=len(sources),
        total_bytes=sum(source.size_bytes for source in sources),
        content_digest=content_digest,
        directories=tuple(directories),
        sources=tuple(sources),
    )
    _verify_target_bindings(root, target, entries)
    return target


def _source_binding(
    root: Path,
    path: Path,
    expected_entries: Mapping[str, InventoryEntry],
) -> SourceBinding:
    relative = path.relative_to(root).as_posix()
    expected = expected_entries.get(relative)
    if expected is None:
        raise CleanupError("cleanup target contains an uninventoried source")
    _assert_no_reparse_path(root, path)
    try:
        before = path.lstat()
        descriptor = _open_protected_archive_descriptor(path)
    except (ArchiveError, OSError) as exc:
        raise CleanupError("cleanup target source is unreadable") from exc
    try:
        opened = os.fstat(descriptor)
        if (
            not stat.S_ISREG(before.st_mode)
            or not stat.S_ISREG(opened.st_mode)
            or _is_reparse(before)
            or _is_reparse(opened)
            or before.st_nlink != 1
            or opened.st_nlink != 1
            or _node_identity(before) != _node_identity(opened)
        ):
            raise CleanupError("cleanup target source is unsafe or has a hard link")
        try:
            _require_windows_canonical_relative_path(root, path, descriptor, "cleanup target")
        except ArchiveError as exc:
            raise CleanupError("cleanup target source uses an unsafe alias") from exc
        content = _read_descriptor(descriptor)
        final_handle = os.fstat(descriptor)
    finally:
        os.close(descriptor)
    try:
        named = path.lstat()
    except OSError as exc:
        raise CleanupError("cleanup target source changed during binding") from exc
    if (
        final_handle.st_nlink != 1
        or named.st_nlink != 1
        or _node_identity(before) != _node_identity(final_handle)
        or _node_identity(before) != _node_identity(named)
    ):
        raise CleanupError("cleanup target source changed during binding")
    digest = hashlib.sha256(content).hexdigest()
    if len(content) != expected.size_bytes or digest != expected.sha256:
        raise CleanupError("cleanup target source changed from inventory")
    return SourceBinding(
        relative_path=relative,
        device=before.st_dev,
        inode=before.st_ino,
        size_bytes=before.st_size,
        modified_ns=before.st_mtime_ns,
        link_count=before.st_nlink,
        sha256=digest,
    )


def _revalidate_target(
    root: Path, target: CleanupTarget, entries: Sequence[InventoryEntry]
) -> None:
    try:
        current = _capture_target(root, target.relative_path, entries)
    except CleanupError:
        raise
    except OSError as exc:
        raise CleanupError("cleanup target is unreadable during reacquisition") from exc
    if replace(current, quarantine_slot=target.quarantine_slot) != target:
        raise CleanupError("cleanup target source changed after plan binding")


def _verify_target_bindings(
    root: Path, target: CleanupTarget, entries: Sequence[InventoryEntry]
) -> None:
    """Recheck the complete bound tree immediately before a quarantine move."""
    path = root / PurePosixPath(target.relative_path)
    _assert_no_reparse_path(root, path)
    try:
        target_stat = path.lstat()
    except OSError as exc:
        raise CleanupError("cleanup target changed after binding") from exc
    if _node_identity(target_stat) != (
        target.device,
        target.inode,
        target.node_size_bytes,
        target.modified_ns,
        target.link_count,
    ):
        raise CleanupError("cleanup target changed after binding")
    expected_entries = {
        entry.relative_path: entry
        for entry in entries
        if entry.relative_path == target.relative_path
        or entry.relative_path.startswith(target.relative_path.rstrip("/") + "/")
    }
    observed_directories: dict[str, DirectoryBinding] = {}
    observed_sources: list[SourceBinding] = []
    if target.kind == "directory":
        pending = [path]
        while pending:
            directory = pending.pop()
            try:
                directory_stat = directory.lstat()
            except OSError as exc:
                raise CleanupError("cleanup target directory changed after binding") from exc
            if not stat.S_ISDIR(directory_stat.st_mode) or _is_reparse(directory_stat):
                raise CleanupError("cleanup target directory changed after binding")
            binding = _directory_binding(root, directory, directory_stat)
            observed_directories[binding.relative_path] = binding
            try:
                with os.scandir(directory) as iterator:
                    children = sorted(iterator, key=lambda item: item.name)
            except OSError as exc:
                raise CleanupError("cleanup target directory is unreadable") from exc
            for child in children:
                child_path = Path(child.path)
                try:
                    child_stat = child.stat(follow_symlinks=False)
                except OSError as exc:
                    raise CleanupError("cleanup target entry is unreadable") from exc
                if _is_reparse(child_stat):
                    raise CleanupError("cleanup target violates no-follow containment")
                if stat.S_ISDIR(child_stat.st_mode):
                    pending.append(child_path)
                elif stat.S_ISREG(child_stat.st_mode):
                    observed_sources.append(
                        _source_binding(root, child_path, expected_entries)
                    )
                else:
                    raise CleanupError("cleanup target contains a special file")
    else:
        observed_sources.append(_source_binding(root, path, expected_entries))
    observed_sources.sort(key=lambda item: item.relative_path)
    expected_directories = {item.relative_path: item for item in target.directories}
    if observed_directories != expected_directories:
        raise CleanupError("cleanup target directory bindings changed")
    if tuple(observed_sources) != target.sources:
        raise CleanupError("cleanup target source bindings changed")
    if {item.relative_path for item in observed_sources} != set(expected_entries):
        raise CleanupError("cleanup target differs from inventory")
    content_digest = hashlib.sha256(
        _compact_json([asdict(source) for source in observed_sources]).encode()
    ).hexdigest()
    if (
        len(observed_sources) != target.file_count
        or sum(source.size_bytes for source in observed_sources) != target.total_bytes
        or content_digest != target.content_digest
    ):
        raise CleanupError("cleanup target content proof changed")
    _recheck_all_target_bindings(
        root,
        target,
        expected_entries,
        expected_directories=set(expected_directories),
        expected_sources={source.relative_path for source in target.sources},
    )


def _recheck_all_target_bindings(
    root: Path,
    target: CleanupTarget,
    expected_entries: Mapping[str, InventoryEntry],
    *,
    expected_directories: set[str],
    expected_sources: set[str],
) -> None:
    """Second full pass catches changes made while the first verification was running."""
    path = root / PurePosixPath(target.relative_path)
    observed_directories: set[str] = set()
    observed_sources: set[str] = set()
    directory_bindings = {item.relative_path: item for item in target.directories}
    source_bindings = {item.relative_path: item for item in target.sources}
    if target.kind == "directory":
        pending = [path]
        while pending:
            directory = pending.pop()
            try:
                directory_stat = directory.lstat()
            except OSError as exc:
                raise CleanupError("cleanup target directory changed after verification") from exc
            binding = _directory_binding(root, directory, directory_stat)
            expected_binding = directory_bindings.get(binding.relative_path)
            if (
                not stat.S_ISDIR(directory_stat.st_mode)
                or _is_reparse(directory_stat)
                or binding != expected_binding
            ):
                raise CleanupError("cleanup target directory bindings changed")
            observed_directories.add(binding.relative_path)
            try:
                with os.scandir(directory) as iterator:
                    children = sorted(iterator, key=lambda item: item.name)
            except OSError as exc:
                raise CleanupError("cleanup target directory is unreadable") from exc
            for child in children:
                child_path = Path(child.path)
                try:
                    child_stat = child.stat(follow_symlinks=False)
                except OSError as exc:
                    raise CleanupError("cleanup target entry is unreadable") from exc
                if _is_reparse(child_stat):
                    raise CleanupError("cleanup target violates no-follow containment")
                if stat.S_ISDIR(child_stat.st_mode):
                    pending.append(child_path)
                elif stat.S_ISREG(child_stat.st_mode):
                    relative = child_path.relative_to(root).as_posix()
                    expected_source = source_bindings.get(relative)
                    if expected_source is None or _source_binding(
                        root, child_path, expected_entries
                    ) != expected_source:
                        raise CleanupError("cleanup target source bindings changed")
                    observed_sources.add(relative)
                else:
                    raise CleanupError("cleanup target contains a special file")
    else:
        expected_source = target.sources[0] if len(target.sources) == 1 else None
        if expected_source is None or _source_binding(
            root, path, expected_entries
        ) != expected_source:
            raise CleanupError("cleanup target source bindings changed")
        observed_sources.add(target.relative_path)
    if observed_directories != expected_directories or observed_sources != expected_sources:
        raise CleanupError("cleanup target differs from inventory")
    try:
        final = path.lstat()
    except OSError as exc:
        raise CleanupError("cleanup target changed after verification") from exc
    if _node_identity(final) != (
        target.device,
        target.inode,
        target.node_size_bytes,
        target.modified_ns,
        target.link_count,
    ):
        raise CleanupError("cleanup target changed after verification")


def _directory_binding(root: Path, path: Path, value: os.stat_result) -> DirectoryBinding:
    return DirectoryBinding(
        relative_path=path.relative_to(root).as_posix(),
        device=value.st_dev,
        inode=value.st_ino,
        modified_ns=value.st_mtime_ns,
    )


def _node_identity(value: os.stat_result) -> tuple[int, int, int, int, int]:
    return (
        value.st_dev,
        value.st_ino,
        value.st_size,
        value.st_mtime_ns,
        value.st_nlink,
    )


def _plan_digest(plan: CleanupPlan) -> str:
    payload = asdict(plan)
    payload.pop("plan_sha256", None)
    return hashlib.sha256(_compact_json(payload).encode()).hexdigest()


def _evidence_binding(root: Path, path: Path, digest: str) -> EvidenceBinding:
    return EvidenceBinding(relative_path=path.relative_to(root).as_posix(), sha256=digest)


def _workspace_root(path: Path) -> Path:
    try:
        root_stat = path.lstat()
        root = path.resolve(strict=True)
    except OSError as exc:
        raise CleanupError("workspace root is missing") from exc
    if not stat.S_ISDIR(root_stat.st_mode) or _is_reparse(root_stat):
        raise CleanupError("workspace root must be a no-follow directory")
    return root


def _validate_external_quarantine_root(
    workspace_root: Path,
    supplied: Path,
    *,
    require_absent: bool | None,
) -> tuple[Path, tuple[ExternalDirectoryBinding, ...]]:
    if (
        not supplied.is_absolute()
        or supplied.name in {"", ".", ".."}
        or ".." in supplied.parts
    ):
        raise CleanupError("cleanup quarantine root must be an exact absolute path")
    lexical = Path(os.path.abspath(supplied))
    try:
        parent = lexical.parent.resolve(strict=True)
        parent_stat = parent.lstat()
    except OSError as exc:
        raise CleanupError("cleanup quarantine parent must be an existing directory") from exc
    if os.path.normcase(str(parent)) != os.path.normcase(str(lexical.parent)):
        raise CleanupError("cleanup quarantine root uses an unsafe alias")
    if not stat.S_ISDIR(parent_stat.st_mode) or _is_reparse(parent_stat):
        raise CleanupError("cleanup quarantine parent is unsafe")
    candidate = parent / lexical.name
    if candidate == workspace_root or candidate.is_relative_to(workspace_root):
        raise CleanupError("cleanup quarantine root must be outside workspace")
    if not _same_volume(workspace_root, parent):
        raise CleanupError("cleanup quarantine root must be on the workspace volume")
    bindings = _capture_external_directory_bindings(parent)
    try:
        leaf = candidate.lstat()
    except FileNotFoundError:
        if require_absent is False:
            raise CleanupError("cleanup quarantine root is missing") from None
    except OSError as exc:
        raise CleanupError("cleanup quarantine root cannot be inspected") from exc
    else:
        if require_absent is True:
            raise CleanupError("cleanup quarantine root already exists")
        if require_absent is None:
            return candidate, bindings
        if not stat.S_ISDIR(leaf.st_mode) or _is_reparse(leaf):
            raise CleanupError("cleanup quarantine root is unsafe")
        if leaf.st_dev != workspace_root.lstat().st_dev:
            raise CleanupError("cleanup quarantine root must be on the workspace volume")
    return candidate, bindings


def _same_volume(left: Path, right: Path) -> bool:
    return left.lstat().st_dev == right.lstat().st_dev


def _capture_external_directory_bindings(
    parent: Path,
) -> tuple[ExternalDirectoryBinding, ...]:
    anchor = Path(parent.anchor)
    try:
        relative = parent.relative_to(anchor)
    except ValueError as exc:
        raise CleanupError("cleanup quarantine parent is unsafe") from exc
    paths = [anchor]
    current = anchor
    for part in relative.parts:
        current = current / part
        paths.append(current)
    bindings: list[ExternalDirectoryBinding] = []
    for path in paths:
        try:
            value = path.lstat()
        except OSError as exc:
            raise CleanupError("cleanup quarantine ancestor is unreadable") from exc
        if not stat.S_ISDIR(value.st_mode) or _is_reparse(value):
            raise CleanupError("cleanup quarantine ancestor is unsafe")
        bindings.append(
            ExternalDirectoryBinding(
                path=str(path.resolve(strict=True)),
                device=value.st_dev,
                inode=value.st_ino,
            )
        )
    return tuple(bindings)


def _validate_source_roots(root: Path, source_roots: Sequence[Path]) -> tuple[Path, ...]:
    result = _configured_source_root_paths(root, source_roots)
    for lexical in result:
        _assert_no_reparse_path(root, lexical)
        try:
            value = lexical.lstat()
        except OSError as exc:
            raise CleanupError("configured cleanup source root is missing") from exc
        if not stat.S_ISDIR(value.st_mode) or _is_reparse(value):
            raise CleanupError("configured cleanup source root is unsafe")
    return result


def _capture_source_root_identities(
    root: Path,
    source_roots: Sequence[Path],
) -> tuple[ExternalDirectoryBinding, ...]:
    configured = _validate_source_roots(root, source_roots)
    identities: list[ExternalDirectoryBinding] = []
    for path in configured:
        value = path.lstat()
        identities.append(
            ExternalDirectoryBinding(
                path=str(path),
                device=value.st_dev,
                inode=value.st_ino,
            )
        )
    return tuple(identities)


def _configured_source_root_paths(
    root: Path,
    source_roots: Sequence[Path],
) -> tuple[Path, ...]:
    if len(source_roots) != len(_SOURCE_ROOTS):
        raise CleanupError("configured cleanup source roots are invalid")
    result: list[Path] = []
    for expected, supplied in zip(_SOURCE_ROOTS, source_roots, strict=True):
        lexical = Path(os.path.abspath(supplied if supplied.is_absolute() else root / supplied))
        if lexical != root / expected:
            raise CleanupError("configured cleanup source roots are invalid")
        result.append(lexical)
    return tuple(result)


def _require_recovery_source_root_identity_paths(
    configured: Sequence[Path],
    expected_identities: Sequence[ExternalDirectoryBinding],
) -> None:
    if len(configured) != len(expected_identities) or any(
        os.path.normcase(str(path)) != os.path.normcase(identity.path)
        for path, identity in zip(configured, expected_identities, strict=True)
    ):
        raise CleanupError("cleanup recovery source root evidence is invalid")


def _validate_recovery_quarantine_root(
    workspace_root: Path,
    quarantine_root: Path,
    *,
    require_present: bool,
) -> Path:
    if require_present:
        validated, _ = _validate_external_quarantine_root(
            workspace_root,
            quarantine_root,
            require_absent=False,
        )
        return validated
    try:
        quarantine_root.lstat()
    except FileNotFoundError:
        validated, _ = _validate_external_quarantine_root(
            workspace_root,
            quarantine_root,
            require_absent=True,
        )
    except OSError as exc:
        raise CleanupError(
            "cleanup recovery quarantine root cannot be inspected"
        ) from exc
    else:
        validated, _ = _validate_external_quarantine_root(
            workspace_root,
            quarantine_root,
            require_absent=False,
        )
    return validated


def _artifact_input(
    root: Path,
    path: Path,
    protected_roots: Sequence[Path],
    label: str,
) -> Path:
    candidate = path if path.is_absolute() else root / path
    lexical = Path(os.path.abspath(candidate))
    if lexical == root or not lexical.is_relative_to(root):
        raise CleanupError(f"{label} is outside workspace")
    protected = tuple(Path(os.path.abspath(item)) for item in protected_roots)
    if any(lexical.is_relative_to(item) for item in protected):
        raise CleanupError(f"{label} is under a protected source root")
    return lexical


def _exact_artifact_input(
    root: Path,
    path: Path,
    expected: Path,
    protected_roots: Sequence[Path],
    label: str,
) -> Path:
    candidate = path if path.is_absolute() else root / path
    lexical = Path(os.path.abspath(candidate))
    if os.path.normcase(str(lexical)) != os.path.normcase(str(root / expected)):
        kind = "plan" if expected == _PLAN_OUTPUT else "report"
        raise CleanupError(f"{label} must use the exact v1.2 {kind} path")
    return _artifact_input(root, lexical, protected_roots, label)


def _read_bound_artifact(root: Path, path: Path, label: str) -> bytes:
    try:
        with _hold_archive_file(root, path, label) as held:
            content = _read_descriptor(held.descriptor)
            _verify_held_archive_file(held)
        return content
    except ArchiveError as exc:
        raise CleanupError(f"{label} is unsafe or unreadable") from exc


def _write_create_only_json_artifact(
    value: object,
    output: Path,
    *,
    repo_root: Path,
    protected_roots: Sequence[Path],
) -> None:
    root = _workspace_root(repo_root)
    protected = _validate_source_roots(root, protected_roots)
    destination = _artifact_input(root, output, protected, "cleanup artifact output")
    try:
        parent = _safe_directory(root, destination.parent.relative_to(root))
    except PosterError as exc:
        raise CleanupError("cleanup artifact output parent is unsafe") from exc
    destination = parent / destination.name
    payload = (_compact_json(value) + "\n").encode()
    with _locked_output_ancestors(root, parent) as guard:
        try:
            _require_ancestor_identities(root, parent, guard.identities)
            descriptor = _open_create_only_descriptor(destination)
        except FileExistsError as exc:
            raise CleanupError("cleanup artifact already exists") from exc
        except (OSError, PosterError) as exc:
            raise CleanupError("cannot create cleanup artifact") from exc
        try:
            _verify_created_descriptor(root, destination, descriptor)
            _write_all(descriptor, payload)
            os.fsync(descriptor)
            os.lseek(descriptor, 0, os.SEEK_SET)
            if _read_descriptor(descriptor) != payload:
                raise CleanupError("cannot publish cleanup artifact")
            _require_ancestor_identities(root, parent, guard.identities)
            _verify_created_descriptor(root, destination, descriptor)
        except (OSError, PosterError) as exc:
            raise CleanupError("cannot publish cleanup artifact") from exc
        finally:
            os.close(descriptor)


def _require_exact_output(output: Path, expected: Path, kind: str) -> None:
    if output.is_absolute() or output.as_posix() != expected.as_posix():
        raise CleanupError(f"cleanup output must use the exact v1.2 {kind} path")


@contextmanager
def _reserve_cleanup_report(
    root: Path,
    output: Path,
    *,
    protected_roots: Sequence[Path],
    initial: CleanupReport,
) -> Iterator[_ReportReservation]:
    _require_exact_output(output, _REPORT_OUTPUT, "report")
    protected = _validate_source_roots(root, protected_roots)
    destination = _artifact_input(root, output, protected, "cleanup report")
    try:
        parent = _safe_directory(root, destination.parent.relative_to(root))
    except PosterError as exc:
        raise CleanupError("cleanup report destination is unsafe") from exc
    destination = parent / destination.name
    descriptor: int | None = None
    with _locked_output_ancestors(root, parent) as guard:
        try:
            _require_ancestor_identities(root, parent, guard.identities)
            descriptor = _open_create_only_descriptor(destination)
        except FileExistsError as exc:
            raise CleanupError("cleanup report already exists") from exc
        except (OSError, PosterError) as exc:
            raise CleanupError("cannot reserve cleanup report") from exc
        try:
            _verify_created_descriptor(root, destination, descriptor)
            opened = os.fstat(descriptor)
            reservation = _ReportReservation(
                root=root,
                path=destination,
                descriptor=descriptor,
                binding=(opened.st_dev, opened.st_ino),
                ancestors=guard.identities,
                committed_size=0,
            )
            reservation.publish(initial)
            yield reservation
        finally:
            if descriptor is not None:
                os.close(descriptor)


@contextmanager
def _reserve_external_quarantine(
    workspace_root: Path, plan: CleanupPlan
) -> Iterator[tuple[Path, tuple[int, int]]]:
    """Create the caller-bound external quarantine root exactly once."""
    destination, bindings = _validate_external_quarantine_root(
        workspace_root, Path(plan.quarantine_root), require_absent=True
    )
    if bindings != plan.quarantine_ancestors:
        raise CleanupError("cleanup quarantine ancestor identity changed")
    parent = destination.parent
    with _locked_output_ancestors(parent, parent) as guard:
        try:
            _require_external_quarantine_binding(plan)
            destination.mkdir()
            value = destination.lstat()
            if (
                not stat.S_ISDIR(value.st_mode)
                or _is_reparse(value)
                or value.st_dev != workspace_root.lstat().st_dev
            ):
                raise CleanupError("cleanup quarantine root is unsafe")
            root_identity = (value.st_dev, value.st_ino)
            _require_external_quarantine_binding(
                plan,
                root_created=True,
                expected_root_identity=root_identity,
            )
            yield destination, root_identity
            _require_ancestor_identities(parent, parent, guard.identities)
            _require_external_quarantine_binding(
                plan,
                root_created=True,
                expected_root_identity=root_identity,
            )
        except FileExistsError as exc:
            raise CleanupError("cleanup quarantine root already exists") from exc
        except PosterError as exc:
            raise CleanupError("cleanup quarantine parent identity changed") from exc
        except OSError as exc:
            raise CleanupError("cannot reserve cleanup quarantine root") from exc


def _require_external_quarantine_binding(
    plan: CleanupPlan,
    *,
    root_created: bool = False,
    expected_root_identity: tuple[int, int] | None = None,
) -> Path:
    root = Path(plan.workspace_root)
    destination, bindings = _validate_external_quarantine_root(
        root,
        Path(plan.quarantine_root),
        require_absent=not root_created,
    )
    if bindings != plan.quarantine_ancestors:
        raise CleanupError("cleanup quarantine ancestor identity changed")
    if root_created:
        if expected_root_identity is None:
            raise CleanupError("cleanup quarantine root identity is missing")
        try:
            value = destination.lstat()
        except OSError as exc:
            raise CleanupError("cleanup quarantine root identity changed") from exc
        if (value.st_dev, value.st_ino) != expected_root_identity:
            raise CleanupError("cleanup quarantine root identity changed")
    return destination


def _prepare_quarantine_slot(
    quarantine: Path,
    target: CleanupTarget,
) -> tuple[Path, Path]:
    slot = PurePosixPath(target.quarantine_slot)
    logical = PurePosixPath(target.relative_path)
    if (
        slot.is_absolute()
        or ".." in slot.parts
        or len(slot.parts) < 2
        or PurePosixPath(*slot.parts[1:]) != logical
    ):
        raise CleanupError("cleanup quarantine slot binding is unsafe")
    slot_root = quarantine / slot.parts[0]
    current = slot_root
    try:
        current.mkdir()
        for part in logical.parent.parts:
            current = current / part
            current.mkdir()
        _assert_no_reparse_path(quarantine, current)
        destination = current / logical.name
        try:
            destination.lstat()
        except FileNotFoundError:
            return slot_root, destination
        raise CleanupError("cleanup quarantine target already exists")
    except CleanupError:
        _remove_empty_quarantine_slot(quarantine, current / logical.name)
        raise
    except OSError as exc:
        _remove_empty_quarantine_slot(quarantine, current / logical.name)
        raise CleanupError("cannot prepare cleanup quarantine slot") from exc


def _detach_target_to_quarantine(
    source: Path,
    destination: Path,
    target: CleanupTarget,
    *,
    plan_sha256: str,
    quarantine_root_identity: tuple[int, int],
) -> DetachEvent:
    rename_not_before = datetime.now().astimezone().isoformat()
    try:
        _atomic_rename_no_replace(source, destination)
        rename_observed_by = datetime.now().astimezone().isoformat()
        moved = destination.lstat()
    except OSError as exc:
        raise CleanupError("cannot detach cleanup target into quarantine") from exc
    if (moved.st_dev, moved.st_ino) != (target.device, target.inode):
        raise CleanupError("detached cleanup target root identity changed")
    return DetachEvent(
        event_type="detached_to_quarantine",
        evidence_origin="executor",
        source_relative_path=target.relative_path,
        quarantine_slot=target.quarantine_slot,
        plan_sha256=plan_sha256,
        content_digest=target.content_digest,
        quarantine_root_device=quarantine_root_identity[0],
        quarantine_root_inode=quarantine_root_identity[1],
        pre_move_root_device=target.device,
        pre_move_root_inode=target.inode,
        post_move_root_device=moved.st_dev,
        post_move_root_inode=moved.st_ino,
        rename_not_before=rename_not_before,
        rename_observed_by=rename_observed_by,
    )


def _observe_current_cleanup_state(
    root: Path,
    targets: Sequence[CleanupTarget],
    quarantine: Path,
    detach_events: Sequence[DetachEvent],
    source_root_identities: Sequence[ExternalDirectoryBinding],
    entries: Sequence[InventoryEntry],
    quarantine_root_identity: tuple[int, int],
) -> CurrentObservation:
    """Take one explicitly non-atomic sample without changing historical events."""
    observed_at_start = datetime.now().astimezone().isoformat()
    event_names = {event.source_relative_path for event in detach_events}
    source_observations: list[SourceRootObservation] = []
    for binding in source_root_identities:
        path = Path(binding.path)
        relative = path.relative_to(root).as_posix()
        exact_root_detached = relative in event_names
        try:
            value = path.lstat()
        except FileNotFoundError:
            state: Literal[
                "matches_recorded_identity",
                "detached_root_absent",
                "missing",
                "identity_changed",
                "unsafe",
                "unreadable",
            ] = "detached_root_absent" if exact_root_detached else "missing"
        except OSError:
            state = "unreadable"
        else:
            if not stat.S_ISDIR(value.st_mode) or _is_reparse(value):
                state = "unsafe"
            elif (value.st_dev, value.st_ino) == (binding.device, binding.inode):
                state = "matches_recorded_identity"
            else:
                state = "identity_changed"
        source_observations.append(SourceRootObservation(path=binding.path, state=state))

    target_map = {target.relative_path: target for target in targets}
    target_observations: list[TargetObservation] = []
    try:
        quarantine_stat = quarantine.lstat()
        quarantine_matches = (
            stat.S_ISDIR(quarantine_stat.st_mode)
            and not _is_reparse(quarantine_stat)
            and (quarantine_stat.st_dev, quarantine_stat.st_ino)
            == quarantine_root_identity
        )
    except OSError:
        quarantine_matches = False
    for event in detach_events:
        target = target_map[event.source_relative_path]
        source = root / PurePosixPath(event.source_relative_path)
        try:
            source_stat = source.lstat()
        except FileNotFoundError:
            source_state: Literal[
                "absent", "recreated", "original_present", "unreadable"
            ] = "absent"
        except OSError:
            source_state = "unreadable"
        else:
            source_state = (
                "original_present"
                if (source_stat.st_dev, source_stat.st_ino)
                == (event.pre_move_root_device, event.pre_move_root_inode)
                else "recreated"
            )
        slot = quarantine / PurePosixPath(event.quarantine_slot)
        if not quarantine_matches:
            slot_state: Literal[
                "matches_plan",
                "content_drift",
                "root_identity_changed",
                "missing",
                "unsafe",
                "unreadable",
            ] = "root_identity_changed"
        else:
            try:
                slot_stat = slot.lstat()
            except FileNotFoundError:
                slot_state = "missing"
            except OSError:
                slot_state = "unreadable"
            else:
                if _is_reparse(slot_stat):
                    slot_state = "unsafe"
                elif (slot_stat.st_dev, slot_stat.st_ino) != (
                    event.post_move_root_device,
                    event.post_move_root_inode,
                ):
                    slot_state = "root_identity_changed"
                else:
                    slot_root = quarantine / PurePosixPath(event.quarantine_slot).parts[0]
                    try:
                        _revalidate_target(slot_root, target, entries)
                        _verify_target_bindings(slot_root, target, entries)
                    except CleanupError:
                        slot_state = "content_drift"
                    else:
                        slot_state = "matches_plan"
        target_observations.append(
            TargetObservation(
                source_relative_path=event.source_relative_path,
                quarantine_slot=event.quarantine_slot,
                source_state=source_state,
                slot_state=slot_state,
            )
        )
    indeterminate = any(
        item.state == "unreadable" for item in source_observations
    ) or any(
        item.source_state == "unreadable" or item.slot_state == "unreadable"
        for item in target_observations
    )
    drift = any(
        item.state not in {"matches_recorded_identity", "detached_root_absent"}
        for item in source_observations
    ) or any(
        item.source_state != "absent" or item.slot_state != "matches_plan"
        for item in target_observations
    )
    assessment: Literal[
        "no_drift_observed", "drift_observed", "indeterminate"
    ] = (
        "indeterminate"
        if indeterminate
        else "drift_observed"
        if drift
        else "no_drift_observed"
    )
    return CurrentObservation(
        observed_at_start=observed_at_start,
        observed_at_end=datetime.now().astimezone().isoformat(),
        atomic=False,
        assessment=assessment,
        source_roots=tuple(source_observations),
        targets=tuple(target_observations),
    )


def _atomic_rename_no_replace(source: Path, destination: Path) -> None:
    """Rename atomically while failing if the exact destination is occupied."""
    if os.name == "nt":
        os.rename(source, destination)
        return

    import ctypes
    import errno
    import sys

    library = ctypes.CDLL(None, use_errno=True)
    if sys.platform == "darwin":
        function_name = "renameatx_np"
        no_replace_flag = 0x00000004
        at_cwd = -2
    else:
        function_name = "renameat2"
        no_replace_flag = 1
        at_cwd = -100
    try:
        rename_no_replace = getattr(library, function_name)
    except AttributeError as exc:
        raise OSError(
            errno.ENOTSUP,
            "atomic no-replace rename is unavailable",
            str(source),
            str(destination),
        ) from exc
    rename_no_replace.argtypes = [
        ctypes.c_int,
        ctypes.c_char_p,
        ctypes.c_int,
        ctypes.c_char_p,
        ctypes.c_uint,
    ]
    rename_no_replace.restype = ctypes.c_int
    result = rename_no_replace(
        at_cwd,
        os.fsencode(source),
        at_cwd,
        os.fsencode(destination),
        no_replace_flag,
    )
    if result != 0:
        error = ctypes.get_errno()
        raise OSError(error, os.strerror(error), str(source), str(destination))


def _remove_empty_quarantine_slot(quarantine: Path, target_path: Path) -> None:
    current = target_path.parent
    while current != quarantine and current.is_relative_to(quarantine):
        try:
            current.rmdir()
        except OSError:
            return
        current = current.parent






def _open_create_only_descriptor(path: Path) -> int:
    if os.name != "nt":
        return os.open(
            path,
            os.O_CREAT
            | os.O_EXCL
            | os.O_RDWR
            | getattr(os, "O_CLOEXEC", 0)
            | getattr(os, "O_NOFOLLOW", 0),
            0o600,
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
        str(path),
        0x80000000 | 0x40000000 | 0x80,
        0x1,
        None,
        1,
        0x00200000,
        None,
    )
    invalid = ctypes.c_void_p(-1).value
    if handle == invalid:
        error = ctypes.get_last_error()
        if error in {80, 183}:
            raise FileExistsError(error, "cleanup artifact already exists", str(path))
        raise OSError(error, "cannot create cleanup artifact", str(path))
    try:
        return msvcrt.open_osfhandle(int(handle), os.O_BINARY | os.O_RDWR)
    except OSError:
        kernel32.CloseHandle(ctypes.c_void_p(handle))
        raise


def _verify_created_descriptor(root: Path, path: Path, descriptor: int) -> None:
    opened = os.fstat(descriptor)
    named = path.lstat()
    if (
        not stat.S_ISREG(opened.st_mode)
        or not stat.S_ISREG(named.st_mode)
        or _is_reparse(opened)
        or _is_reparse(named)
        or opened.st_nlink != 1
        or named.st_nlink != 1
        or (opened.st_dev, opened.st_ino) != (named.st_dev, named.st_ino)
    ):
        raise CleanupError("cleanup artifact destination is unsafe")
    try:
        _require_windows_canonical_relative_path(root, path, descriptor, "cleanup artifact")
    except ArchiveError as exc:
        raise CleanupError("cleanup artifact output uses an unsafe alias") from exc


def _write_all(descriptor: int, content: bytes) -> None:
    view = memoryview(content)
    while view:
        written = os.write(descriptor, view)
        if written <= 0:
            raise OSError("short cleanup artifact write")
        view = view[written:]


def _assert_no_reparse_path(root: Path, target: Path) -> None:
    lexical = Path(os.path.abspath(target))
    if lexical == root or not lexical.is_relative_to(root):
        raise CleanupError("cleanup path is outside workspace")
    current = root
    for part in lexical.relative_to(root).parts:
        current = current / part
        try:
            value = current.lstat()
        except OSError as exc:
            raise CleanupError("cleanup path is missing") from exc
        if _is_reparse(value):
            raise CleanupError("cleanup path violates no-follow containment")


def _is_below_source_root(relative_path: str) -> bool:
    path = PurePosixPath(relative_path)
    return bool(path.parts) and path.parts[0] in _SOURCE_ROOTS and path.as_posix() == relative_path


def _json_object(content: bytes, label: str) -> dict[str, Any]:
    def reject_duplicates(pairs: list[tuple[str, Any]]) -> dict[str, Any]:
        result: dict[str, Any] = {}
        for key, value in pairs:
            if key in result:
                raise CleanupError(f"{label} contains duplicate JSON keys")
            result[key] = value
        return result

    try:
        value = json.loads(content.decode("utf-8"), object_pairs_hook=reject_duplicates)
    except (UnicodeDecodeError, json.JSONDecodeError) as exc:
        raise CleanupError(f"{label} is invalid JSON") from exc
    if not isinstance(value, dict):
        raise CleanupError(f"{label} must be a JSON object")
    return value


def _require_keys(value: Mapping[str, Any], expected: set[str], label: str) -> None:
    if set(value) != expected:
        raise CleanupError(f"{label} is invalid")


def _strict_string(value: Mapping[str, Any], key: str, label: str) -> str:
    item = value.get(key)
    if not isinstance(item, str) or not item:
        raise CleanupError(f"{label} is invalid")
    return item


def _strict_int(value: Mapping[str, Any], key: str, label: str) -> int:
    item = value.get(key)
    if type(item) is not int:
        raise CleanupError(f"{label} is invalid")
    return item


def _strict_sha(value: Mapping[str, Any], key: str, label: str) -> str:
    item = _strict_string(value, key, label)
    if _SHA256.fullmatch(item) is None:
        raise CleanupError(f"{label} is invalid")
    return item


def _parse_cleanup_plan(payload: Mapping[str, Any]) -> CleanupPlan:
    expected = {
        "schema_version",
        "verified",
        "release_version",
        "workspace_root",
        "workspace_device",
        "workspace_inode",
        "quarantine_root",
        "quarantine_ancestors",
        "source_roots",
        "inventory",
        "duplicate_proof",
        "archive_verification",
        "bucket",
        "prefix",
        "object_count",
        "total_bytes",
        "inventory_digest",
        "targets",
        "plan_sha256",
    }
    _require_keys(payload, expected, "cleanup plan schema")
    source_roots = payload.get("source_roots")
    quarantine_ancestors = payload.get("quarantine_ancestors")
    targets = payload.get("targets")
    if not isinstance(source_roots, list) or not all(isinstance(item, str) for item in source_roots):
        raise CleanupError("cleanup plan schema is invalid")
    if not isinstance(targets, list):
        raise CleanupError("cleanup plan schema is invalid")
    if not isinstance(quarantine_ancestors, list):
        raise CleanupError("cleanup plan schema is invalid")
    return CleanupPlan(
        schema_version=_strict_string(payload, "schema_version", "cleanup plan schema"),
        verified=payload.get("verified") is True,
        release_version=_strict_string(payload, "release_version", "cleanup plan schema"),
        workspace_root=_strict_string(payload, "workspace_root", "cleanup plan schema"),
        workspace_device=_strict_int(payload, "workspace_device", "cleanup plan schema"),
        workspace_inode=_strict_int(payload, "workspace_inode", "cleanup plan schema"),
        quarantine_root=_strict_string(
            payload, "quarantine_root", "cleanup plan schema"
        ),
        quarantine_ancestors=tuple(
            _parse_external_directory(item) for item in quarantine_ancestors
        ),
        source_roots=tuple(cast(list[str], source_roots)),
        inventory=_parse_evidence(payload.get("inventory")),
        duplicate_proof=_parse_evidence(payload.get("duplicate_proof")),
        archive_verification=_parse_evidence(payload.get("archive_verification")),
        bucket=_strict_string(payload, "bucket", "cleanup plan schema"),
        prefix=_strict_string(payload, "prefix", "cleanup plan schema"),
        object_count=_strict_int(payload, "object_count", "cleanup plan schema"),
        total_bytes=_strict_int(payload, "total_bytes", "cleanup plan schema"),
        inventory_digest=_strict_sha(payload, "inventory_digest", "cleanup plan schema"),
        targets=tuple(_parse_target(item) for item in targets),
        plan_sha256=_strict_sha(payload, "plan_sha256", "cleanup plan schema"),
    )


def _parse_evidence(value: object) -> EvidenceBinding:
    if not isinstance(value, dict):
        raise CleanupError("cleanup plan evidence schema is invalid")
    _require_keys(value, {"relative_path", "sha256"}, "cleanup plan evidence schema")
    return EvidenceBinding(
        relative_path=_strict_string(value, "relative_path", "cleanup plan evidence schema"),
        sha256=_strict_sha(value, "sha256", "cleanup plan evidence schema"),
    )


def _parse_external_directory(value: object) -> ExternalDirectoryBinding:
    if not isinstance(value, dict):
        raise CleanupError("cleanup quarantine ancestor schema is invalid")
    _require_keys(
        value,
        {"path", "device", "inode"},
        "cleanup quarantine ancestor schema",
    )
    return ExternalDirectoryBinding(
        path=_strict_string(value, "path", "cleanup quarantine ancestor schema"),
        device=_strict_int(value, "device", "cleanup quarantine ancestor schema"),
        inode=_strict_int(value, "inode", "cleanup quarantine ancestor schema"),
    )


def _parse_target(value: object) -> CleanupTarget:
    if not isinstance(value, dict):
        raise CleanupError("cleanup target schema is invalid")
    expected = {
        "relative_path",
        "quarantine_slot",
        "kind",
        "device",
        "inode",
        "node_size_bytes",
        "modified_ns",
        "link_count",
        "file_count",
        "total_bytes",
        "content_digest",
        "directories",
        "sources",
    }
    _require_keys(value, expected, "cleanup target schema")
    kind = value.get("kind")
    directories = value.get("directories")
    sources = value.get("sources")
    if kind not in {"directory", "file"} or not isinstance(directories, list) or not isinstance(sources, list):
        raise CleanupError("cleanup target schema is invalid")
    return CleanupTarget(
        relative_path=_strict_string(value, "relative_path", "cleanup target schema"),
        quarantine_slot=_strict_string(
            value, "quarantine_slot", "cleanup target schema"
        ),
        kind=cast(Literal["directory", "file"], kind),
        device=_strict_int(value, "device", "cleanup target schema"),
        inode=_strict_int(value, "inode", "cleanup target schema"),
        node_size_bytes=_strict_int(value, "node_size_bytes", "cleanup target schema"),
        modified_ns=_strict_int(value, "modified_ns", "cleanup target schema"),
        link_count=_strict_int(value, "link_count", "cleanup target schema"),
        file_count=_strict_int(value, "file_count", "cleanup target schema"),
        total_bytes=_strict_int(value, "total_bytes", "cleanup target schema"),
        content_digest=_strict_sha(value, "content_digest", "cleanup target schema"),
        directories=tuple(_parse_directory(item) for item in directories),
        sources=tuple(_parse_source(item) for item in sources),
    )


def _parse_directory(value: object) -> DirectoryBinding:
    if not isinstance(value, dict):
        raise CleanupError("cleanup directory binding schema is invalid")
    _require_keys(
        value,
        {"relative_path", "device", "inode", "modified_ns"},
        "cleanup directory binding schema",
    )
    return DirectoryBinding(
        relative_path=_strict_string(value, "relative_path", "cleanup directory binding schema"),
        device=_strict_int(value, "device", "cleanup directory binding schema"),
        inode=_strict_int(value, "inode", "cleanup directory binding schema"),
        modified_ns=_strict_int(value, "modified_ns", "cleanup directory binding schema"),
    )


def _parse_source(value: object) -> SourceBinding:
    if not isinstance(value, dict):
        raise CleanupError("cleanup source binding schema is invalid")
    expected = {
        "relative_path",
        "device",
        "inode",
        "size_bytes",
        "modified_ns",
        "link_count",
        "sha256",
    }
    _require_keys(value, expected, "cleanup source binding schema")
    return SourceBinding(
        relative_path=_strict_string(value, "relative_path", "cleanup source binding schema"),
        device=_strict_int(value, "device", "cleanup source binding schema"),
        inode=_strict_int(value, "inode", "cleanup source binding schema"),
        size_bytes=_strict_int(value, "size_bytes", "cleanup source binding schema"),
        modified_ns=_strict_int(value, "modified_ns", "cleanup source binding schema"),
        link_count=_strict_int(value, "link_count", "cleanup source binding schema"),
        sha256=_strict_sha(value, "sha256", "cleanup source binding schema"),
    )


def _compact_json(value: object) -> str:
    return json.dumps(value, ensure_ascii=False, separators=(",", ":"), sort_keys=True)
