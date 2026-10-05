from __future__ import annotations

import hashlib
import json
import os
import sys
from contextlib import contextmanager
from dataclasses import replace
from pathlib import Path, PurePosixPath
from types import SimpleNamespace

import pytest

from hk_movie_rag import cleanup, cli
from hk_movie_rag.cleanup import (
    CleanupError,
    build_cleanup_plan,
    execute_cleanup,
    load_cleanup_plan,
    validate_cleanup_plan,
    write_cleanup_plan,
)
from hk_movie_rag.release_lock import ReleaseLockError


def _json_bytes(value: object) -> bytes:
    return (
        json.dumps(value, ensure_ascii=False, separators=(",", ":"), sort_keys=True) + "\n"
    ).encode()


def _inventory_line(relative_path: str, content: bytes, classification: str) -> dict[str, object]:
    return {
        "classification": classification,
        "relative_path": relative_path,
        "sha256": hashlib.sha256(content).hexdigest(),
        "size_bytes": len(content),
    }


@pytest.fixture
def cleanup_inputs(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> SimpleNamespace:
    root = tmp_path / "repo"
    roots = tuple(
        root / name
        for name in (
            "FinalDelivery_2026-07-16",
            "1-1500posters",
            "posters",
            "posters1",
        )
    )
    for path in roots:
        path.mkdir(parents=True)
    quarantine_parent = tmp_path / "approved-quarantine"
    quarantine_parent.mkdir()
    quarantine_root = quarantine_parent / "v1.2-run-001"
    files = {
        "FinalDelivery_2026-07-16/formal.csv": (b"formal", "formal_release_source"),
        "1-1500posters/a.jpg": (b"outer", "poster_candidate"),
        "1-1500posters/1-1500posters/a.jpg": (b"outer", "duplicate_candidate"),
        "posters/posters/b.jpg": (b"canonical", "poster_candidate"),
        "posters1/b.jpg": (b"canonical", "duplicate_candidate"),
        "posters/__MACOSX/._b.jpg": (b"metadata", "macos_metadata"),
        "posters/__MACOSX/deep/leaf/._x.jpg": (b"deep-metadata", "macos_metadata"),
        "posters/posters/.DS_Store": (b"finder", "macos_metadata"),
    }
    entries: list[dict[str, object]] = []
    for relative_path, (content, classification) in files.items():
        path = root / Path(relative_path)
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_bytes(content)
        entries.append(_inventory_line(relative_path, content, classification))
    entries.sort(key=lambda item: str(item["relative_path"]))
    evidence = root / "artifacts" / "inventory" / "v1.2"
    evidence.mkdir(parents=True)
    inventory = evidence / "source_inventory.jsonl"
    inventory.write_bytes(b"".join(_json_bytes(entry) for entry in entries))
    inventory_digest = hashlib.sha256(inventory.read_bytes()).hexdigest()
    duplicate_proof = evidence / "duplicate_proof.json"
    duplicate_proof.write_bytes(
        _json_bytes(
            {
                "pairs": [
                    {
                        "canonical_root": "1-1500posters",
                        "duplicate_root": "nested_1-1500posters",
                        "file_count": 1,
                    },
                    {
                        "canonical_root": "posters/posters",
                        "duplicate_root": "posters1",
                        "file_count": 1,
                    },
                ]
            }
        )
    )
    archive = root / "artifacts" / "archive" / "v1.2" / "verification.json"
    archive.parent.mkdir(parents=True)
    total_bytes = sum(int(entry["size_bytes"]) for entry in entries)
    bucket = "test-project-123-hk-movie-rag-source-archive"
    prefix = "source/v1.2"
    report = {
        "bucket": bucket,
        "inventory_digest": inventory_digest,
        "object_count": len(entries),
        "objects": [
            {
                "action": "verified",
                "generation": index + 1,
                "hash_verified": True,
                "object_name": f"{prefix}/files/{entry['relative_path']}",
                "sha256": entry["sha256"],
            }
            for index, entry in enumerate(entries)
        ],
        "prefix": prefix,
        "total_bytes": total_bytes,
        "verification_timestamp": "2026-07-29T07:27:16.597310+00:00",
        "verified": True,
    }
    archive.write_bytes(_json_bytes(report))
    authority = cleanup._CleanupAuthority(
        archive_report_sha256=hashlib.sha256(archive.read_bytes()).hexdigest(),
        bucket=bucket,
        inventory_digest=inventory_digest,
        object_count=len(entries),
        prefix=prefix,
        total_bytes=total_bytes,
        mirror_counts=(1, 1),
    )
    monkeypatch.setattr(cleanup, "_V12_AUTHORITY", authority)
    source_root_identities = cleanup._capture_source_root_identities(root, roots)
    kwargs = {
        "workspace_root": root,
        "quarantine_root": quarantine_root,
        "release_version": "v1.2",
        "source_roots": roots,
        "inventory_path": inventory,
        "duplicate_proof_path": duplicate_proof,
        "archive_verification_path": archive,
    }
    return SimpleNamespace(
        root=root,
        quarantine_root=quarantine_root,
        roots=roots,
        entries=entries,
        inventory=inventory,
        duplicate_proof=duplicate_proof,
        archive=archive,
        report=report,
        source_root_identities=source_root_identities,
        kwargs=kwargs,
    )


def _execute(
    plan: cleanup.CleanupPlan,
    cleanup_inputs: SimpleNamespace,
    *,
    confirmation: str = "v1.2",
) -> cleanup.CleanupReport:
    return execute_cleanup(
        plan,
        confirmation,
        repo_root=cleanup_inputs.root,
        report_output=Path("artifacts/cleanup/v1.2/report.json"),
        protected_roots=cleanup_inputs.roots,
    )


def _checkpoint_records(path: Path) -> list[dict[str, object]]:
    records: list[dict[str, object]] = []
    for line in path.read_bytes().splitlines():
        try:
            value = json.loads(line)
        except json.JSONDecodeError:
            break
        assert isinstance(value, dict)
        records.append(value)
    return records


def _publish_then_mutate_first_detach(
    monkeypatch: pytest.MonkeyPatch,
    mutation: object,
) -> None:
    original_publish = cleanup._ReportReservation.publish
    scheduled = False

    def publish_and_mutate(
        reservation: cleanup._ReportReservation,
        report: cleanup.CleanupReport,
    ) -> None:
        nonlocal scheduled
        original_publish(reservation, report)
        if (
            not scheduled
            and report.status == "in_progress"
            and report.intent is None
            and len(report.detach_events) == 1
            and report.current_observation is None
        ):
            scheduled = True
            assert callable(mutation)
            mutation(report.detach_events[0])

    monkeypatch.setattr(cleanup._ReportReservation, "publish", publish_and_mutate)


def test_cleanup_report_v4_separates_detach_events_from_current_observation(
    cleanup_inputs: SimpleNamespace,
) -> None:
    plan = build_cleanup_plan(**cleanup_inputs.kwargs)

    report = _execute(plan, cleanup_inputs)
    payload = json.loads(cleanup.report_to_json(report))

    assert report.status == "detached"
    assert report.rag_readiness == "not_authorized"
    assert report.current_observation is not None
    assert report.current_observation.atomic is False
    assert (
        report.current_observation.observed_at_start
        <= report.current_observation.observed_at_end
    )
    assert report.current_observation.assessment == "no_drift_observed"
    assert len(report.detach_events) == len(plan.targets)
    assert payload["schema_version"] == "cleanup-report/v4"
    assert payload["rag_readiness"] == "not_authorized"
    assert "detach_events" in payload
    assert "current_observation" in payload
    assert "quarantined_targets" not in payload


def test_cleanup_slot_drift_after_event_checkpoint_preserves_event_truth(
    cleanup_inputs: SimpleNamespace,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    plan = build_cleanup_plan(**cleanup_inputs.kwargs)

    def mutate_slot(event: cleanup.DetachEvent) -> None:
        slot = cleanup_inputs.quarantine_root / PurePosixPath(event.quarantine_slot)
        (slot / "late-drift.jpg").write_bytes(b"late-slot-drift")

    _publish_then_mutate_first_detach(monkeypatch, mutate_slot)

    report = _execute(plan, cleanup_inputs)

    assert report.status == "detached_with_drift"
    assert len(report.detach_events) == 1
    event = report.detach_events[0]
    assert event.event_type == "detached_to_quarantine"
    assert event.source_relative_path == plan.targets[0].relative_path
    assert event.quarantine_slot == plan.targets[0].quarantine_slot
    assert event.pre_move_root_device == event.post_move_root_device
    assert event.pre_move_root_inode == event.post_move_root_inode
    assert event.rename_not_before <= event.rename_observed_by
    assert report.current_observation is not None
    assert report.current_observation.assessment == "drift_observed"
    assert report.current_observation.targets[0].slot_state == "content_drift"


def test_cleanup_source_recreation_after_event_checkpoint_is_current_drift(
    cleanup_inputs: SimpleNamespace,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    plan = build_cleanup_plan(**cleanup_inputs.kwargs)

    def recreate_source(event: cleanup.DetachEvent) -> None:
        source = cleanup_inputs.root / PurePosixPath(event.source_relative_path)
        source.mkdir(parents=True)
        (source / "replacement.txt").write_text("replacement", encoding="utf-8")

    _publish_then_mutate_first_detach(monkeypatch, recreate_source)

    report = _execute(plan, cleanup_inputs)

    assert report.status == "detached_with_drift"
    assert len(report.detach_events) == 1
    assert report.detach_events[0].event_type == "detached_to_quarantine"
    assert report.current_observation is not None
    assert report.current_observation.targets[0].source_state == "recreated"
    assert report.current_observation.assessment == "drift_observed"


def test_cleanup_recovery_infers_event_from_root_identity_despite_content_drift(
    cleanup_inputs: SimpleNamespace,
) -> None:
    plan = build_cleanup_plan(**cleanup_inputs.kwargs)
    target = plan.targets[0]
    slot = cleanup_inputs.quarantine_root / PurePosixPath(target.quarantine_slot)
    slot.parent.mkdir(parents=True)
    quarantine_stat = cleanup_inputs.quarantine_root.lstat()
    intent = cleanup.QuarantineIntent(
        source_relative_path=target.relative_path,
        quarantine_slot=target.quarantine_slot,
        quarantine_root_device=quarantine_stat.st_dev,
        quarantine_root_inode=quarantine_stat.st_ino,
        device=target.device,
        inode=target.inode,
        content_digest=target.content_digest,
        recorded_at="2026-07-29T12:00:00+00:00",
    )
    report_path = Path("artifacts/cleanup/v1.2/report.json")
    with cleanup._reserve_cleanup_report(
        cleanup_inputs.root,
        report_path,
        protected_roots=cleanup_inputs.roots,
        initial=cleanup._report_for(
            plan,
            "prepared",
            (),
            source_root_identities=cleanup_inputs.source_root_identities,
        ),
    ) as reservation:
        reservation.publish(
            cleanup._report_for(
                plan,
                "in_progress",
                (),
                source_root_identities=cleanup_inputs.source_root_identities,
                quarantine_root_identity=(
                    quarantine_stat.st_dev,
                    quarantine_stat.st_ino,
                ),
                intent=intent,
                active_target=target.relative_path,
            )
        )
    source = cleanup_inputs.root / PurePosixPath(target.relative_path)
    source.rename(slot)
    (slot / "a.jpg").write_bytes(b"content-drift-after-crash")

    recovery = cleanup.assess_cleanup_recovery(
        report_path,
        repo_root=cleanup_inputs.root,
        protected_roots=cleanup_inputs.roots,
    )

    assert recovery.state == "partial"
    assert recovery.current_observation is not None
    assert recovery.current_observation.assessment == "drift_observed"
    assert len(recovery.detach_events) == 1
    event = recovery.detach_events[0]
    assert event.evidence_origin == "recovery_inference"
    assert event.rename_not_before == intent.recorded_at
    assert event.rename_not_before <= event.rename_observed_by
    assert event.pre_move_root_inode == target.inode
    assert event.post_move_root_inode == target.inode
    assert recovery.current_observation is not None
    assert recovery.current_observation.targets[0].slot_state == "content_drift"


def test_cleanup_recovery_source_replacement_does_not_erase_inferred_event(
    cleanup_inputs: SimpleNamespace,
) -> None:
    plan = build_cleanup_plan(**cleanup_inputs.kwargs)
    target = plan.targets[0]
    slot = cleanup_inputs.quarantine_root / PurePosixPath(target.quarantine_slot)
    slot.parent.mkdir(parents=True)
    quarantine_stat = cleanup_inputs.quarantine_root.lstat()
    intent = cleanup.QuarantineIntent(
        source_relative_path=target.relative_path,
        quarantine_slot=target.quarantine_slot,
        quarantine_root_device=quarantine_stat.st_dev,
        quarantine_root_inode=quarantine_stat.st_ino,
        device=target.device,
        inode=target.inode,
        content_digest=target.content_digest,
        recorded_at="2026-07-29T12:00:00+00:00",
    )
    report_path = Path("artifacts/cleanup/v1.2/report.json")
    with cleanup._reserve_cleanup_report(
        cleanup_inputs.root,
        report_path,
        protected_roots=cleanup_inputs.roots,
        initial=cleanup._report_for(
            plan,
            "prepared",
            (),
            source_root_identities=cleanup_inputs.source_root_identities,
        ),
    ) as reservation:
        reservation.publish(
            cleanup._report_for(
                plan,
                "in_progress",
                (),
                source_root_identities=cleanup_inputs.source_root_identities,
                quarantine_root_identity=(
                    quarantine_stat.st_dev,
                    quarantine_stat.st_ino,
                ),
                intent=intent,
                active_target=target.relative_path,
            )
        )
    source = cleanup_inputs.root / PurePosixPath(target.relative_path)
    source.rename(slot)
    source.mkdir()
    (source / "replacement.txt").write_text("new object", encoding="utf-8")

    recovery = cleanup.assess_cleanup_recovery(
        report_path,
        repo_root=cleanup_inputs.root,
        protected_roots=cleanup_inputs.roots,
    )

    assert recovery.state == "partial"
    assert recovery.current_observation is not None
    assert recovery.current_observation.assessment == "drift_observed"
    assert len(recovery.detach_events) == 1
    assert recovery.current_observation is not None
    assert recovery.current_observation.targets[0].source_state == "recreated"




def test_cleanup_plan_contains_only_exact_generated_allowlist(cleanup_inputs: SimpleNamespace) -> None:
    plan = build_cleanup_plan(**cleanup_inputs.kwargs)

    assert [target.relative_path for target in plan.targets] == [
        "1-1500posters/1-1500posters",
        "posters/__MACOSX",
        "posters/posters/.DS_Store",
        "posters1",
    ]
    assert all(not target.relative_path.startswith("FinalDelivery_2026-07-16") for target in plan.targets)
    assert "1-1500posters" not in {target.relative_path for target in plan.targets}
    assert "posters/posters" not in {target.relative_path for target in plan.targets}


def test_cleanup_plan_is_deterministic_while_sources_are_unchanged(
    cleanup_inputs: SimpleNamespace,
) -> None:
    first = build_cleanup_plan(**cleanup_inputs.kwargs)
    second = build_cleanup_plan(**cleanup_inputs.kwargs)

    assert first == second
    assert first.plan_sha256 == second.plan_sha256


def test_cleanup_module_has_no_provider_or_acl_mutation_surface() -> None:
    forbidden = {
        "_WindowsAclBinding",
        "_apply_windows_quarantine_dacl",
        "_read_windows_dacl",
        "_restore_quarantined_target",
        "_send_to_trash",
        "_set_windows_handle_dacl",
        "_try_restore_quarantined_target",
    }

    assert forbidden.isdisjoint(vars(cleanup))


def test_atomic_quarantine_move_never_replaces_an_occupied_slot(
    tmp_path: Path,
) -> None:
    source = tmp_path / "source"
    destination = tmp_path / "destination"
    source.write_bytes(b"source")
    destination.write_bytes(b"occupied")

    with pytest.raises(OSError):
        cleanup._atomic_rename_no_replace(source, destination)

    assert source.read_bytes() == b"source"
    assert destination.read_bytes() == b"occupied"


def test_cleanup_execute_stops_at_proof_verified_external_quarantine(
    cleanup_inputs: SimpleNamespace,
) -> None:
    quarantine_root = cleanup_inputs.quarantine_root
    plan = build_cleanup_plan(**cleanup_inputs.kwargs)

    report = _execute(plan, cleanup_inputs)

    assert report.status == "detached"
    assert tuple(event.source_relative_path for event in report.detach_events) == tuple(
        target.relative_path for target in plan.targets
    )
    assert Path(plan.quarantine_root) == quarantine_root.resolve(strict=True)
    for target in plan.targets:
        source = cleanup_inputs.root / PurePosixPath(target.relative_path)
        quarantined = quarantine_root / PurePosixPath(target.quarantine_slot)
        assert not source.exists()
        assert quarantined.exists()
        cleanup._verify_target_bindings(
            quarantine_root / PurePosixPath(target.quarantine_slot).parts[0],
            target,
            tuple(cleanup._parse_inventory(cleanup_inputs.inventory.read_bytes())),
        )


@pytest.mark.parametrize("kind", ["relative", "workspace", "alias"])
def test_cleanup_plan_rejects_non_exact_or_internal_quarantine_root(
    cleanup_inputs: SimpleNamespace,
    kind: str,
) -> None:
    if kind == "relative":
        quarantine_root = Path("approved-quarantine/v1.2-run-001")
    elif kind == "workspace":
        quarantine_root = cleanup_inputs.root / "internal-quarantine"
    else:
        alias_parent = cleanup_inputs.quarantine_root.parent / "alias-parent"
        alias_parent.mkdir()
        quarantine_root = alias_parent / ".." / cleanup_inputs.quarantine_root.name

    with pytest.raises(CleanupError, match="quarantine root"):
        build_cleanup_plan(
            **{**cleanup_inputs.kwargs, "quarantine_root": quarantine_root}
        )


def test_cleanup_plan_rejects_cross_volume_quarantine_root(
    cleanup_inputs: SimpleNamespace,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setattr(
        cleanup, "_same_volume", lambda left, right: False, raising=False
    )

    with pytest.raises(CleanupError, match="workspace volume"):
        build_cleanup_plan(**cleanup_inputs.kwargs)


def test_cleanup_plan_rejects_reparse_quarantine_parent(
    cleanup_inputs: SimpleNamespace,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    parent_stat = cleanup_inputs.quarantine_root.parent.lstat()
    original = cleanup._is_reparse
    monkeypatch.setattr(
        cleanup,
        "_is_reparse",
        lambda value: (value.st_dev, value.st_ino)
        == (parent_stat.st_dev, parent_stat.st_ino)
        or original(value),
    )

    with pytest.raises(CleanupError, match="quarantine parent"):
        build_cleanup_plan(**cleanup_inputs.kwargs)


@pytest.mark.parametrize("leaf_kind", ["file", "directory"])
def test_cleanup_plan_rejects_existing_quarantine_leaf(
    cleanup_inputs: SimpleNamespace,
    leaf_kind: str,
) -> None:
    if leaf_kind == "file":
        cleanup_inputs.quarantine_root.write_text("occupied", encoding="utf-8")
    else:
        cleanup_inputs.quarantine_root.mkdir()

    with pytest.raises(CleanupError, match="already exists"):
        build_cleanup_plan(**cleanup_inputs.kwargs)


def test_cleanup_plan_binds_unique_deterministic_quarantine_slots(
    cleanup_inputs: SimpleNamespace,
) -> None:
    plan = build_cleanup_plan(**cleanup_inputs.kwargs)

    assert [target.quarantine_slot for target in plan.targets] == [
        f"{index:04d}/{target.relative_path}"
        for index, target in enumerate(plan.targets)
    ]
    assert len({target.quarantine_slot for target in plan.targets}) == len(plan.targets)


def test_cleanup_quarantine_leaf_collision_is_journaled_before_zero_moves(
    cleanup_inputs: SimpleNamespace,
) -> None:
    plan = build_cleanup_plan(**cleanup_inputs.kwargs)
    cleanup_inputs.quarantine_root.mkdir()

    with pytest.raises(CleanupError, match="already exists"):
        _execute(plan, cleanup_inputs)

    records = _checkpoint_records(
        cleanup_inputs.root / "artifacts" / "cleanup" / "v1.2" / "report.json"
    )
    assert [record["status"] for record in records] == ["prepared"]
    assert records[0]["detach_events"] == []


def test_cleanup_rejects_reserved_quarantine_root_identity_swap(
    cleanup_inputs: SimpleNamespace,
) -> None:
    plan = build_cleanup_plan(**cleanup_inputs.kwargs)
    held_root = cleanup_inputs.quarantine_root.with_name("held-root")
    cleanup_inputs.quarantine_root.mkdir()
    reserved = cleanup_inputs.quarantine_root.lstat()
    cleanup_inputs.quarantine_root.rename(held_root)
    cleanup_inputs.quarantine_root.mkdir()

    with pytest.raises(CleanupError, match="root identity changed"):
        cleanup._require_external_quarantine_binding(
            plan,
            root_created=True,
            expected_root_identity=(reserved.st_dev, reserved.st_ino),
        )

    assert held_root.exists()


@pytest.mark.parametrize(
    ("scenario", "expected"),
    [
        ("no-intent", "no_event"),
        ("not-moved", "not_detached"),
        ("quarantined", "partial"),
        ("both-present", "partial"),
        ("neither-present", "conflict"),
        ("mutated-slot", "partial"),
        ("replaced-root", "conflict"),
    ],
)
def test_cleanup_recovery_classifies_intent_without_moving_or_overwriting(
    cleanup_inputs: SimpleNamespace,
    scenario: str,
    expected: str,
) -> None:
    plan = build_cleanup_plan(**cleanup_inputs.kwargs)
    target = plan.targets[0]
    source = cleanup_inputs.root / PurePosixPath(target.relative_path)
    slot = cleanup_inputs.quarantine_root / PurePosixPath(target.quarantine_slot)
    slot.parent.mkdir(parents=True)
    quarantine_stat = cleanup_inputs.quarantine_root.lstat()
    intent = cleanup.QuarantineIntent(
        source_relative_path=target.relative_path,
        quarantine_slot=target.quarantine_slot,
        quarantine_root_device=quarantine_stat.st_dev,
        quarantine_root_inode=quarantine_stat.st_ino,
        device=target.device,
        inode=target.inode,
        content_digest=target.content_digest,
    )
    report_path = Path("artifacts/cleanup/v1.2/report.json")
    with cleanup._reserve_cleanup_report(
        cleanup_inputs.root,
        report_path,
        protected_roots=cleanup_inputs.roots,
        initial=cleanup._report_for(
            plan,
            "prepared",
            (),
            source_root_identities=cleanup_inputs.source_root_identities,
        ),
    ) as reservation:
        if scenario != "no-intent":
            reservation.publish(
                cleanup._report_for(
                    plan,
                    "in_progress",
                    (),
                    source_root_identities=cleanup_inputs.source_root_identities,
                    intent=intent,
                    active_target=target.relative_path,
                )
            )

    if scenario in {"quarantined", "both-present", "mutated-slot"}:
        source.rename(slot)
    elif scenario == "neither-present":
        source.rename(cleanup_inputs.root.parent / "held-away")
    elif scenario == "replaced-root":
        cleanup_inputs.quarantine_root.rename(
            cleanup_inputs.quarantine_root.with_name("held-quarantine-root")
        )
        slot.parent.mkdir(parents=True)
    if scenario == "both-present":
        source.mkdir(parents=True)
        (source / "unauthorized.txt").write_text("new", encoding="utf-8")
    elif scenario == "mutated-slot":
        (slot / "a.jpg").write_bytes(b"mutated")

    before_source = source.exists()
    before_slot = slot.exists()
    recovery = cleanup.assess_cleanup_recovery(
        report_path,
        repo_root=cleanup_inputs.root,
        protected_roots=cleanup_inputs.roots,
    )

    assert recovery.state == expected
    assert source.exists() is before_source
    assert slot.exists() is before_slot
    assert recovery.target == (
        None if scenario in {"no-intent", "replaced-root"} else target.relative_path
    )


def test_cleanup_recovery_revalidates_external_quarantine_root(
    cleanup_inputs: SimpleNamespace,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    plan = build_cleanup_plan(**cleanup_inputs.kwargs)
    target = plan.targets[0]
    slot = cleanup_inputs.quarantine_root / PurePosixPath(target.quarantine_slot)
    slot.parent.mkdir(parents=True)
    quarantine_stat = cleanup_inputs.quarantine_root.lstat()
    intent = cleanup.QuarantineIntent(
        source_relative_path=target.relative_path,
        quarantine_slot=target.quarantine_slot,
        quarantine_root_device=quarantine_stat.st_dev,
        quarantine_root_inode=quarantine_stat.st_ino,
        device=target.device,
        inode=target.inode,
        content_digest=target.content_digest,
    )
    report_path = Path("artifacts/cleanup/v1.2/report.json")
    with cleanup._reserve_cleanup_report(
        cleanup_inputs.root,
        report_path,
        protected_roots=cleanup_inputs.roots,
        initial=cleanup._report_for(
            plan,
            "prepared",
            (),
            source_root_identities=cleanup_inputs.source_root_identities,
        ),
    ) as reservation:
        reservation.publish(
            cleanup._report_for(
                plan,
                "in_progress",
                (),
                source_root_identities=cleanup_inputs.source_root_identities,
                intent=intent,
                active_target=target.relative_path,
            )
        )
    monkeypatch.setattr(cleanup, "_same_volume", lambda left, right: False)

    with pytest.raises(CleanupError, match="workspace volume"):
        cleanup.assess_cleanup_recovery(
            report_path,
            repo_root=cleanup_inputs.root,
            protected_roots=cleanup_inputs.roots,
        )


def test_cleanup_recovery_reads_normal_complete_quarantine(
    cleanup_inputs: SimpleNamespace,
) -> None:
    plan = build_cleanup_plan(**cleanup_inputs.kwargs)

    report = _execute(plan, cleanup_inputs)
    recovered = cleanup.recover_cleanup_report_checkpoint(
        Path("artifacts/cleanup/v1.2/report.json"),
        repo_root=cleanup_inputs.root,
        protected_roots=cleanup_inputs.roots,
    )
    recovery = cleanup.assess_cleanup_recovery(
        Path("artifacts/cleanup/v1.2/report.json"),
        repo_root=cleanup_inputs.root,
        protected_roots=cleanup_inputs.roots,
    )

    assert report.status == "detached"
    assert recovered["status"] == "detached"
    assert recovery.state == "detached"


def test_cleanup_terminal_recovery_observes_missing_non_target_source_root_as_drift(
    cleanup_inputs: SimpleNamespace,
) -> None:
    plan = build_cleanup_plan(**cleanup_inputs.kwargs)
    report_path = Path("artifacts/cleanup/v1.2/report.json")
    _execute(plan, cleanup_inputs)
    non_target_root = cleanup_inputs.root / "FinalDelivery_2026-07-16"
    non_target_root.rename(cleanup_inputs.root.parent / "held-terminal-root")

    recovery = cleanup.assess_cleanup_recovery(
        report_path,
        repo_root=cleanup_inputs.root,
        protected_roots=cleanup_inputs.roots,
    )

    assert recovery.state == "detached_with_drift"
    assert recovery.current_observation is not None
    assert recovery.current_observation.source_roots[0].state == "missing"


def test_cleanup_terminal_recovery_observes_non_target_source_root_identity_swap(
    cleanup_inputs: SimpleNamespace,
) -> None:
    plan = build_cleanup_plan(**cleanup_inputs.kwargs)
    report_path = Path("artifacts/cleanup/v1.2/report.json")
    _execute(plan, cleanup_inputs)
    non_target_root = cleanup_inputs.root / "FinalDelivery_2026-07-16"
    non_target_root.rename(cleanup_inputs.root.parent / "held-terminal-root")
    non_target_root.mkdir()

    recovery = cleanup.assess_cleanup_recovery(
        report_path,
        repo_root=cleanup_inputs.root,
        protected_roots=cleanup_inputs.roots,
    )

    assert recovery.state == "detached_with_drift"
    assert recovery.current_observation is not None
    assert recovery.current_observation.source_roots[0].state == "identity_changed"


def test_cleanup_terminal_recovery_detects_detached_slot_mutation(
    cleanup_inputs: SimpleNamespace,
) -> None:
    plan = build_cleanup_plan(**cleanup_inputs.kwargs)
    target = plan.targets[0]
    report_path = Path("artifacts/cleanup/v1.2/report.json")
    _execute(plan, cleanup_inputs)
    slot = cleanup_inputs.quarantine_root / PurePosixPath(target.quarantine_slot)
    (slot / "late-unbound-file.jpg").write_bytes(b"terminal-slot-drift")

    recovery = cleanup.assess_cleanup_recovery(
        report_path,
        repo_root=cleanup_inputs.root,
        protected_roots=cleanup_inputs.roots,
    )

    assert recovery.state == "detached_with_drift"
    assert recovery.target is None
    assert recovery.current_observation is not None
    assert recovery.current_observation.targets[0].source_state == "absent"
    assert recovery.current_observation.targets[0].slot_state == "content_drift"


def test_cleanup_recovery_without_event_observes_missing_source_root(
    cleanup_inputs: SimpleNamespace,
) -> None:
    plan = build_cleanup_plan(**cleanup_inputs.kwargs)
    report_path = Path("artifacts/cleanup/v1.2/report.json")
    with cleanup._reserve_cleanup_report(
        cleanup_inputs.root,
        report_path,
        protected_roots=cleanup_inputs.roots,
        initial=cleanup._report_for(
            plan,
            "prepared",
            (),
            source_root_identities=cleanup_inputs.source_root_identities,
        ),
    ):
        pass
    missing_root = cleanup_inputs.root / "posters1"
    missing_root.rename(cleanup_inputs.root.parent / "held-no-intent-root")

    recovered = cleanup.recover_cleanup_report_checkpoint(
        report_path,
        repo_root=cleanup_inputs.root,
        protected_roots=cleanup_inputs.roots,
    )
    assert recovered["status"] == "prepared"
    recovery = cleanup.assess_cleanup_recovery(
        report_path,
        repo_root=cleanup_inputs.root,
        protected_roots=cleanup_inputs.roots,
    )
    assert recovery.state == "no_event"
    assert recovery.current_observation is not None
    assert recovery.current_observation.source_roots[-1].state == "missing"


def test_cleanup_recovery_classifies_final_posters1_after_event_write_failure(
    cleanup_inputs: SimpleNamespace,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    plan = build_cleanup_plan(**cleanup_inputs.kwargs)
    target = plan.targets[-1]
    assert target.relative_path == "posters1"
    original_write_all = cleanup._write_all
    writes = 0

    def fail_final_event_checkpoint(
        descriptor: int,
        content: bytes,
    ) -> None:
        nonlocal writes
        writes += 1
        if writes == 12:
            cleanup.os.write(descriptor, content[: max(1, len(content) // 3)])
            raise OSError("simulated final event write failure")
        original_write_all(descriptor, content)

    monkeypatch.setattr(cleanup, "_write_all", fail_final_event_checkpoint)

    with pytest.raises(CleanupError, match="publish cleanup report"):
        _execute(plan, cleanup_inputs)

    source = cleanup_inputs.root / "posters1"
    slot = cleanup_inputs.quarantine_root / PurePosixPath(target.quarantine_slot)
    assert not source.exists()
    assert slot.exists()
    recovered = cleanup.recover_cleanup_report_checkpoint(
        Path("artifacts/cleanup/v1.2/report.json"),
        repo_root=cleanup_inputs.root,
        protected_roots=cleanup_inputs.roots,
    )
    recovery = cleanup.assess_cleanup_recovery(
        Path("artifacts/cleanup/v1.2/report.json"),
        repo_root=cleanup_inputs.root,
        protected_roots=cleanup_inputs.roots,
    )
    assert recovered["intent"]["source_relative_path"] == "posters1"
    assert recovery.state == "detached"
    assert recovery.detach_events[-1].evidence_origin == "recovery_inference"


def test_cleanup_recovery_observes_missing_non_target_source_root_after_journal_read(
    cleanup_inputs: SimpleNamespace,
) -> None:
    plan = build_cleanup_plan(**cleanup_inputs.kwargs)
    target = plan.targets[0]
    slot = cleanup_inputs.quarantine_root / PurePosixPath(target.quarantine_slot)
    slot.parent.mkdir(parents=True)
    quarantine_stat = cleanup_inputs.quarantine_root.lstat()
    intent = cleanup.QuarantineIntent(
        source_relative_path=target.relative_path,
        quarantine_slot=target.quarantine_slot,
        quarantine_root_device=quarantine_stat.st_dev,
        quarantine_root_inode=quarantine_stat.st_ino,
        device=target.device,
        inode=target.inode,
        content_digest=target.content_digest,
    )
    report_path = Path("artifacts/cleanup/v1.2/report.json")
    with cleanup._reserve_cleanup_report(
        cleanup_inputs.root,
        report_path,
        protected_roots=cleanup_inputs.roots,
        initial=cleanup._report_for(
            plan,
            "prepared",
            (),
            source_root_identities=cleanup_inputs.source_root_identities,
        ),
    ) as reservation:
        reservation.publish(
            cleanup._report_for(
                plan,
                "in_progress",
                (),
                source_root_identities=cleanup_inputs.source_root_identities,
                intent=intent,
                active_target=target.relative_path,
            )
        )
    (cleanup_inputs.root / PurePosixPath(target.relative_path)).rename(slot)
    missing_root = cleanup_inputs.root / "FinalDelivery_2026-07-16"
    missing_root.rename(cleanup_inputs.root.parent / "held-non-target-root")

    recovered = cleanup.recover_cleanup_report_checkpoint(
        report_path,
        repo_root=cleanup_inputs.root,
        protected_roots=cleanup_inputs.roots,
    )
    assert recovered["status"] == "in_progress"
    recovery = cleanup.assess_cleanup_recovery(
        report_path,
        repo_root=cleanup_inputs.root,
        protected_roots=cleanup_inputs.roots,
    )
    assert recovery.state == "partial"
    assert recovery.current_observation is not None
    assert recovery.current_observation.assessment == "drift_observed"
    assert recovery.current_observation.source_roots[0].state == "missing"


def test_cleanup_recovery_allows_exact_active_target_to_be_absent(
    cleanup_inputs: SimpleNamespace,
) -> None:
    plan = build_cleanup_plan(**cleanup_inputs.kwargs)
    target = plan.targets[0]
    slot = cleanup_inputs.quarantine_root / PurePosixPath(target.quarantine_slot)
    slot.parent.mkdir(parents=True)
    quarantine_stat = cleanup_inputs.quarantine_root.lstat()
    intent = cleanup.QuarantineIntent(
        source_relative_path=target.relative_path,
        quarantine_slot=target.quarantine_slot,
        quarantine_root_device=quarantine_stat.st_dev,
        quarantine_root_inode=quarantine_stat.st_ino,
        device=target.device,
        inode=target.inode,
        content_digest=target.content_digest,
    )
    report_path = Path("artifacts/cleanup/v1.2/report.json")
    with cleanup._reserve_cleanup_report(
        cleanup_inputs.root,
        report_path,
        protected_roots=cleanup_inputs.roots,
        initial=cleanup._report_for(
            plan,
            "prepared",
            (),
            source_root_identities=cleanup_inputs.source_root_identities,
        ),
    ) as reservation:
        reservation.publish(
            cleanup._report_for(
                plan,
                "in_progress",
                (),
                source_root_identities=cleanup_inputs.source_root_identities,
                intent=intent,
                active_target=target.relative_path,
            )
        )
    (cleanup_inputs.root / PurePosixPath(target.relative_path)).rename(slot)

    recovered = cleanup.recover_cleanup_report_checkpoint(
        report_path,
        repo_root=cleanup_inputs.root,
        protected_roots=cleanup_inputs.roots,
    )
    recovery = cleanup.assess_cleanup_recovery(
        report_path,
        repo_root=cleanup_inputs.root,
        protected_roots=cleanup_inputs.roots,
    )

    assert recovered["status"] == "in_progress"
    assert recovery.state == "partial"


def test_cleanup_crash_after_move_recovers_from_durable_intent(
    cleanup_inputs: SimpleNamespace,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    plan = build_cleanup_plan(**cleanup_inputs.kwargs)
    original_write_all = cleanup._write_all
    writes = 0

    def fail_event_checkpoint(descriptor: int, content: bytes) -> None:
        nonlocal writes
        writes += 1
        if writes == 3:
            cleanup.os.write(descriptor, content[: max(1, len(content) // 3)])
            raise OSError("simulated crash after atomic quarantine move")
        original_write_all(descriptor, content)

    monkeypatch.setattr(cleanup, "_write_all", fail_event_checkpoint)

    with pytest.raises(CleanupError, match="publish cleanup report"):
        _execute(plan, cleanup_inputs)

    records = _checkpoint_records(
        cleanup_inputs.root / "artifacts" / "cleanup" / "v1.2" / "report.json"
    )
    assert [record["status"] for record in records] == ["prepared", "in_progress"]
    assert records[-1]["intent"]["quarantine_slot"] == plan.targets[0].quarantine_slot
    quarantine_stat = cleanup_inputs.quarantine_root.lstat()
    assert records[-1]["intent"]["quarantine_root_device"] == quarantine_stat.st_dev
    assert records[-1]["intent"]["quarantine_root_inode"] == quarantine_stat.st_ino
    source = cleanup_inputs.root / PurePosixPath(plan.targets[0].relative_path)
    slot = cleanup_inputs.quarantine_root / PurePosixPath(
        plan.targets[0].quarantine_slot
    )
    assert not source.exists()
    assert slot.exists()
    recovery = cleanup.assess_cleanup_recovery(
        Path("artifacts/cleanup/v1.2/report.json"),
        repo_root=cleanup_inputs.root,
        protected_roots=cleanup_inputs.roots,
    )
    assert recovery.state == "partial"
    assert len(recovery.detach_events) == 1 < len(plan.targets)
    assert recovery.detach_events[0].evidence_origin == "recovery_inference"


def test_cleanup_content_drift_after_detach_preserves_event_and_reports_drift(
    cleanup_inputs: SimpleNamespace,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    plan = build_cleanup_plan(**cleanup_inputs.kwargs)
    original_detach = cleanup._detach_target_to_quarantine

    def mutate_after_move(
        source: Path,
        destination: Path,
        target: cleanup.CleanupTarget,
        **kwargs: object,
    ) -> cleanup.DetachEvent:
        event = original_detach(source, destination, target, **kwargs)  # type: ignore[arg-type]
        (destination / "a.jpg").write_bytes(b"mutated-in-quarantine")
        return event

    monkeypatch.setattr(cleanup, "_detach_target_to_quarantine", mutate_after_move)

    report = _execute(plan, cleanup_inputs)

    assert report.status == "detached_with_drift"
    assert len(report.detach_events) == 1
    assert report.intent is None
    assert report.current_observation is not None
    assert report.current_observation.targets[0].slot_state == "content_drift"
    source = cleanup_inputs.root / PurePosixPath(plan.targets[0].relative_path)
    slot = cleanup_inputs.quarantine_root / PurePosixPath(
        plan.targets[0].quarantine_slot
    )
    assert not source.exists()
    assert (slot / "a.jpg").read_bytes() == b"mutated-in-quarantine"
    recovery = cleanup.assess_cleanup_recovery(
        Path("artifacts/cleanup/v1.2/report.json"),
        repo_root=cleanup_inputs.root,
        protected_roots=cleanup_inputs.roots,
    )
    assert recovery.state == "partial"
    assert recovery.current_observation is not None
    assert recovery.current_observation.assessment == "drift_observed"


def test_cleanup_mutation_during_current_observation_is_reported_as_drift(
    cleanup_inputs: SimpleNamespace,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    plan = build_cleanup_plan(**cleanup_inputs.kwargs)
    original_verify = cleanup._verify_target_bindings
    mutated = False

    def mutate_after_initial_quarantine_proof(
        root: Path,
        target: cleanup.CleanupTarget,
        entries: object,
    ) -> None:
        nonlocal mutated
        original_verify(root, target, entries)  # type: ignore[arg-type]
        if (
            not mutated
            and root != cleanup_inputs.root
            and target.relative_path == plan.targets[0].relative_path
        ):
            mutated = True
            (root / target.relative_path / "a.jpg").write_bytes(
                b"mutated-after-initial-proof"
            )

    monkeypatch.setattr(
        cleanup,
        "_verify_target_bindings",
        mutate_after_initial_quarantine_proof,
    )

    report = _execute(plan, cleanup_inputs)

    assert report.status == "detached_with_drift"
    assert len(report.detach_events) == 1
    assert report.current_observation is not None
    assert report.current_observation.targets[0].slot_state == "content_drift"
    slot = cleanup_inputs.quarantine_root / PurePosixPath(
        plan.targets[0].quarantine_slot
    )
    assert (slot / "a.jpg").read_bytes() == b"mutated-after-initial-proof"


def test_cleanup_mutation_during_non_atomic_sample_is_reported_as_drift(
    cleanup_inputs: SimpleNamespace,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    plan = build_cleanup_plan(**cleanup_inputs.kwargs)
    target = plan.targets[0]
    source = cleanup_inputs.root / PurePosixPath(target.relative_path)
    slot = cleanup_inputs.quarantine_root / PurePosixPath(target.quarantine_slot)
    original_verify = cleanup._verify_target_bindings
    original_lstat = Path.lstat
    quarantine_proof_complete = False
    mutated = False

    def mark_initial_quarantine_proof(
        root: Path,
        candidate: cleanup.CleanupTarget,
        entries: object,
    ) -> None:
        nonlocal quarantine_proof_complete
        original_verify(root, candidate, entries)  # type: ignore[arg-type]
        if (
            root != cleanup_inputs.root
            and candidate.relative_path == target.relative_path
        ):
            quarantine_proof_complete = True

    def mutate_before_source_absence_check(path: Path) -> os.stat_result:
        nonlocal mutated
        if (
            path == source
            and quarantine_proof_complete
            and not mutated
            and slot.exists()
        ):
            mutated = True
            (slot / "a.jpg").write_bytes(b"mutated-during-observation")
            raise FileNotFoundError(str(path))
        return original_lstat(path)

    monkeypatch.setattr(cleanup, "_verify_target_bindings", mark_initial_quarantine_proof)
    monkeypatch.setattr(Path, "lstat", mutate_before_source_absence_check)

    report = _execute(plan, cleanup_inputs)

    assert report.status == "detached_with_drift"
    assert len(report.detach_events) >= 1
    assert report.current_observation is not None
    assert report.current_observation.targets[0].slot_state == "content_drift"
    assert (slot / "a.jpg").read_bytes() == b"mutated-during-observation"


def test_cleanup_slot_rewrite_after_event_checkpoint_is_observed_as_drift(
    cleanup_inputs: SimpleNamespace,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    plan = build_cleanup_plan(**cleanup_inputs.kwargs)
    target = plan.targets[0]
    slot = cleanup_inputs.quarantine_root / PurePosixPath(target.quarantine_slot)
    original_publish = cleanup._ReportReservation.publish
    scheduled = False

    def rewrite_after_event_append(
        reservation: cleanup._ReportReservation,
        report: cleanup.CleanupReport,
    ) -> None:
        nonlocal scheduled
        if (
            not scheduled
            and report.intent is None
            and report.current_observation is None
            and tuple(event.source_relative_path for event in report.detach_events)
            == (target.relative_path,)
        ):
            scheduled = True
            (slot / "a.jpg").write_bytes(b"rewrite-in-final-append-gap")
        original_publish(reservation, report)

    monkeypatch.setattr(
        cleanup._ReportReservation,
        "publish",
        rewrite_after_event_append,
    )

    report = _execute(plan, cleanup_inputs)

    assert scheduled is True
    assert report.status == "detached_with_drift"
    assert len(report.detach_events) == 1
    assert report.current_observation is not None
    assert report.current_observation.targets[0].slot_state == "content_drift"
    records = _checkpoint_records(
        cleanup_inputs.root / "artifacts" / "cleanup" / "v1.2" / "report.json"
    )
    assert records[-1]["status"] == "detached_with_drift"
    assert len(records[-1]["detach_events"]) == 1
    assert records[-1]["intent"] is None


def test_cleanup_recovery_conflicts_when_original_file_identity_exists_in_both_namespaces(
    cleanup_inputs: SimpleNamespace,
) -> None:
    plan = build_cleanup_plan(**cleanup_inputs.kwargs)
    target = plan.targets[2]
    assert target.kind == "file"
    source = cleanup_inputs.root / PurePosixPath(target.relative_path)
    cleanup_inputs.quarantine_root.mkdir()
    quarantine_stat = cleanup_inputs.quarantine_root.lstat()
    quarantine_identity = (quarantine_stat.st_dev, quarantine_stat.st_ino)
    report_path = Path("artifacts/cleanup/v1.2/report.json")
    with cleanup._reserve_cleanup_report(
        cleanup_inputs.root,
        report_path,
        protected_roots=cleanup_inputs.roots,
        initial=cleanup._report_for(
            plan,
            "prepared",
            (),
            source_root_identities=cleanup_inputs.source_root_identities,
        ),
    ) as reservation:
        events: list[cleanup.DetachEvent] = []
        for prior in plan.targets[:2]:
            _, prior_slot = cleanup._prepare_quarantine_slot(
                cleanup_inputs.quarantine_root, prior
            )
            prior_intent = cleanup.QuarantineIntent(
                source_relative_path=prior.relative_path,
                quarantine_slot=prior.quarantine_slot,
                quarantine_root_device=quarantine_identity[0],
                quarantine_root_inode=quarantine_identity[1],
                device=prior.device,
                inode=prior.inode,
                content_digest=prior.content_digest,
            )
            reservation.publish(
                cleanup._report_for(
                    plan,
                    "in_progress",
                    events,
                    quarantine_root_identity=quarantine_identity,
                    source_root_identities=cleanup_inputs.source_root_identities,
                    intent=prior_intent,
                    active_target=prior.relative_path,
                )
            )
            events.append(
                cleanup._detach_target_to_quarantine(
                    cleanup_inputs.root / PurePosixPath(prior.relative_path),
                    prior_slot,
                    prior,
                    plan_sha256=plan.plan_sha256,
                    quarantine_root_identity=quarantine_identity,
                )
            )
            reservation.publish(
                cleanup._report_for(
                    plan,
                    "in_progress",
                    events,
                    quarantine_root_identity=quarantine_identity,
                    source_root_identities=cleanup_inputs.source_root_identities,
                )
            )
        _, slot = cleanup._prepare_quarantine_slot(cleanup_inputs.quarantine_root, target)
        intent = cleanup.QuarantineIntent(
            source_relative_path=target.relative_path,
            quarantine_slot=target.quarantine_slot,
            quarantine_root_device=quarantine_identity[0],
            quarantine_root_inode=quarantine_identity[1],
            device=target.device,
            inode=target.inode,
            content_digest=target.content_digest,
        )
        reservation.publish(
            cleanup._report_for(
                plan,
                "in_progress",
                events,
                quarantine_root_identity=quarantine_identity,
                source_root_identities=cleanup_inputs.source_root_identities,
                intent=intent,
                active_target=target.relative_path,
            )
        )
    source.rename(slot)
    os.link(slot, source)

    recovery = cleanup.assess_cleanup_recovery(
        report_path,
        repo_root=cleanup_inputs.root,
        protected_roots=cleanup_inputs.roots,
    )

    assert recovery.state == "conflict"
    assert len(recovery.detach_events) == 2


def test_cleanup_journal_rejects_incomplete_detached_terminal_claim(
    cleanup_inputs: SimpleNamespace,
) -> None:
    plan = build_cleanup_plan(**cleanup_inputs.kwargs)
    target = plan.targets[0]
    source = cleanup_inputs.root / PurePosixPath(target.relative_path)
    slot = cleanup_inputs.quarantine_root / PurePosixPath(target.quarantine_slot)
    slot.parent.mkdir(parents=True)
    quarantine_stat = cleanup_inputs.quarantine_root.lstat()
    quarantine_identity = (quarantine_stat.st_dev, quarantine_stat.st_ino)
    intent = cleanup.QuarantineIntent(
        source_relative_path=target.relative_path,
        quarantine_slot=target.quarantine_slot,
        quarantine_root_device=quarantine_identity[0],
        quarantine_root_inode=quarantine_identity[1],
        device=target.device,
        inode=target.inode,
        content_digest=target.content_digest,
    )
    relative_report = Path("artifacts/cleanup/v1.2/report.json")
    report_path = cleanup_inputs.root / relative_report
    with cleanup._reserve_cleanup_report(
        cleanup_inputs.root,
        relative_report,
        protected_roots=cleanup_inputs.roots,
        initial=cleanup._report_for(
            plan,
            "prepared",
            (),
            source_root_identities=cleanup_inputs.source_root_identities,
        ),
    ) as reservation:
        reservation.publish(
            cleanup._report_for(
                plan,
                "in_progress",
                (),
                quarantine_root_identity=quarantine_identity,
                source_root_identities=cleanup_inputs.source_root_identities,
                intent=intent,
                active_target=target.relative_path,
            )
        )
        event = cleanup._detach_target_to_quarantine(
            source,
            slot,
            target,
            plan_sha256=plan.plan_sha256,
            quarantine_root_identity=quarantine_identity,
        )
        event_checkpoint = cleanup._report_for(
            plan,
            "in_progress",
            (event,),
            quarantine_root_identity=quarantine_identity,
            source_root_identities=cleanup_inputs.source_root_identities,
        )
        reservation.publish(event_checkpoint)
    forged = json.loads(cleanup.report_to_json(event_checkpoint))
    forged["status"] = "detached"
    with report_path.open("ab") as stream:
        stream.write(_json_bytes(forged))

    recovered = cleanup.recover_cleanup_report_checkpoint(
        relative_report,
        repo_root=cleanup_inputs.root,
        protected_roots=cleanup_inputs.roots,
    )

    assert recovered["status"] == "in_progress"
    assert len(recovered["detach_events"]) == 1


def test_cleanup_later_source_drift_requires_fresh_recovery_observation(
    cleanup_inputs: SimpleNamespace,
) -> None:
    plan = build_cleanup_plan(**cleanup_inputs.kwargs)
    target = plan.targets[0]
    source = cleanup_inputs.root / PurePosixPath(target.relative_path)
    report_path = (
        cleanup_inputs.root / "artifacts" / "cleanup" / "v1.2" / "report.json"
    )
    report = _execute(plan, cleanup_inputs)
    original_events = report.detach_events
    source.mkdir(parents=True)
    (source / "new.txt").write_text("later-drift", encoding="utf-8")

    recovery = cleanup.assess_cleanup_recovery(
        report_path.relative_to(cleanup_inputs.root),
        repo_root=cleanup_inputs.root,
        protected_roots=cleanup_inputs.roots,
    )

    assert report.status == "detached"
    assert report.detach_events == original_events
    assert recovery.state == "detached_with_drift"
    assert recovery.detach_events == original_events
    assert recovery.current_observation is not None
    assert recovery.current_observation.targets[0].source_state == "recreated"


def test_cleanup_later_quarantine_root_drift_does_not_erase_detach_events(
    cleanup_inputs: SimpleNamespace,
) -> None:
    plan = build_cleanup_plan(**cleanup_inputs.kwargs)
    report = _execute(plan, cleanup_inputs)
    moved_root = cleanup_inputs.quarantine_root.with_name("moved-after-cleanup")
    cleanup_inputs.quarantine_root.rename(moved_root)

    recovery = cleanup.assess_cleanup_recovery(
        Path("artifacts/cleanup/v1.2/report.json"),
        repo_root=cleanup_inputs.root,
        protected_roots=cleanup_inputs.roots,
    )

    assert recovery.state == "detached_with_drift"
    assert recovery.detach_events == report.detach_events
    assert recovery.current_observation is not None
    assert all(
        target.slot_state == "root_identity_changed"
        for target in recovery.current_observation.targets
    )


def test_cleanup_journal_cannot_erase_durable_detach_event_history(
    cleanup_inputs: SimpleNamespace,
) -> None:
    plan = build_cleanup_plan(**cleanup_inputs.kwargs)
    report = _execute(plan, cleanup_inputs)
    report_path = (
        cleanup_inputs.root / "artifacts" / "cleanup" / "v1.2" / "report.json"
    )
    forged = json.loads(cleanup.report_to_json(report))
    forged["status"] = "in_progress"
    forged["detach_events"] = []
    forged["current_observation"] = None
    with report_path.open("ab") as stream:
        stream.write(_json_bytes(forged))

    recovered = cleanup.recover_cleanup_report_checkpoint(
        report_path.relative_to(cleanup_inputs.root),
        repo_root=cleanup_inputs.root,
        protected_roots=cleanup_inputs.roots,
    )
    recovery = cleanup.assess_cleanup_recovery(
        report_path.relative_to(cleanup_inputs.root),
        repo_root=cleanup_inputs.root,
        protected_roots=cleanup_inputs.roots,
    )

    assert len(recovered["detach_events"]) == len(plan.targets)
    assert recovery.detach_events == report.detach_events


def test_cleanup_journal_cannot_erase_durable_active_intent(
    cleanup_inputs: SimpleNamespace,
) -> None:
    plan = build_cleanup_plan(**cleanup_inputs.kwargs)
    target = plan.targets[0]
    slot = cleanup_inputs.quarantine_root / PurePosixPath(target.quarantine_slot)
    slot.parent.mkdir(parents=True)
    quarantine_stat = cleanup_inputs.quarantine_root.lstat()
    intent = cleanup.QuarantineIntent(
        source_relative_path=target.relative_path,
        quarantine_slot=target.quarantine_slot,
        quarantine_root_device=quarantine_stat.st_dev,
        quarantine_root_inode=quarantine_stat.st_ino,
        device=target.device,
        inode=target.inode,
        content_digest=target.content_digest,
    )
    relative_report = Path("artifacts/cleanup/v1.2/report.json")
    report_path = cleanup_inputs.root / relative_report
    with cleanup._reserve_cleanup_report(
        cleanup_inputs.root,
        relative_report,
        protected_roots=cleanup_inputs.roots,
        initial=cleanup._report_for(
            plan,
            "prepared",
            (),
            source_root_identities=cleanup_inputs.source_root_identities,
        ),
    ) as reservation:
        active = cleanup._report_for(
            plan,
            "in_progress",
            (),
            quarantine_root_identity=(quarantine_stat.st_dev, quarantine_stat.st_ino),
            source_root_identities=cleanup_inputs.source_root_identities,
            intent=intent,
            active_target=target.relative_path,
        )
        reservation.publish(active)
    forged = json.loads(cleanup.report_to_json(active))
    forged["intent"] = None
    forged["active_target"] = None
    with report_path.open("ab") as stream:
        stream.write(_json_bytes(forged))
    (cleanup_inputs.root / PurePosixPath(target.relative_path)).rename(slot)

    recovered = cleanup.recover_cleanup_report_checkpoint(
        relative_report,
        repo_root=cleanup_inputs.root,
        protected_roots=cleanup_inputs.roots,
    )
    recovery = cleanup.assess_cleanup_recovery(
        relative_report,
        repo_root=cleanup_inputs.root,
        protected_roots=cleanup_inputs.roots,
    )

    assert recovered["intent"] is not None
    assert recovery.state == "partial"
    assert recovery.detach_events[0].evidence_origin == "recovery_inference"


def test_cleanup_journal_rejects_event_not_fully_bound_to_intent_and_plan(
    cleanup_inputs: SimpleNamespace,
) -> None:
    plan = build_cleanup_plan(**cleanup_inputs.kwargs)
    target = plan.targets[0]
    source = cleanup_inputs.root / PurePosixPath(target.relative_path)
    slot = cleanup_inputs.quarantine_root / PurePosixPath(target.quarantine_slot)
    slot.parent.mkdir(parents=True)
    quarantine_stat = cleanup_inputs.quarantine_root.lstat()
    quarantine_identity = (quarantine_stat.st_dev, quarantine_stat.st_ino)
    intent = cleanup.QuarantineIntent(
        source_relative_path=target.relative_path,
        quarantine_slot=target.quarantine_slot,
        quarantine_root_device=quarantine_identity[0],
        quarantine_root_inode=quarantine_identity[1],
        device=target.device,
        inode=target.inode,
        content_digest=target.content_digest,
    )
    relative_report = Path("artifacts/cleanup/v1.2/report.json")
    report_path = cleanup_inputs.root / relative_report
    with cleanup._reserve_cleanup_report(
        cleanup_inputs.root,
        relative_report,
        protected_roots=cleanup_inputs.roots,
        initial=cleanup._report_for(
            plan,
            "prepared",
            (),
            source_root_identities=cleanup_inputs.source_root_identities,
        ),
    ) as reservation:
        reservation.publish(
            cleanup._report_for(
                plan,
                "in_progress",
                (),
                quarantine_root_identity=quarantine_identity,
                source_root_identities=cleanup_inputs.source_root_identities,
                intent=intent,
                active_target=target.relative_path,
            )
        )
    event = cleanup._detach_target_to_quarantine(
        source,
        slot,
        target,
        plan_sha256=plan.plan_sha256,
        quarantine_root_identity=quarantine_identity,
    )
    contradictory = replace(event, plan_sha256="0" * 64)
    forged = cleanup._report_for(
        plan,
        "in_progress",
        (contradictory,),
        quarantine_root_identity=quarantine_identity,
        source_root_identities=cleanup_inputs.source_root_identities,
    )
    with report_path.open("ab") as stream:
        stream.write(cleanup.report_to_json(forged).encode())

    recovered = cleanup.recover_cleanup_report_checkpoint(
        relative_report,
        repo_root=cleanup_inputs.root,
        protected_roots=cleanup_inputs.roots,
    )

    assert recovered["intent"] is not None
    assert recovered["detach_events"] == []


def test_cleanup_recovery_unreadable_source_identity_cannot_infer_event(
    cleanup_inputs: SimpleNamespace,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    plan = build_cleanup_plan(**cleanup_inputs.kwargs)
    target = plan.targets[0]
    source = cleanup_inputs.root / PurePosixPath(target.relative_path)
    slot = cleanup_inputs.quarantine_root / PurePosixPath(target.quarantine_slot)
    slot.parent.mkdir(parents=True)
    quarantine_stat = cleanup_inputs.quarantine_root.lstat()
    intent = cleanup.QuarantineIntent(
        source_relative_path=target.relative_path,
        quarantine_slot=target.quarantine_slot,
        quarantine_root_device=quarantine_stat.st_dev,
        quarantine_root_inode=quarantine_stat.st_ino,
        device=target.device,
        inode=target.inode,
        content_digest=target.content_digest,
    )
    report_path = Path("artifacts/cleanup/v1.2/report.json")
    with cleanup._reserve_cleanup_report(
        cleanup_inputs.root,
        report_path,
        protected_roots=cleanup_inputs.roots,
        initial=cleanup._report_for(
            plan,
            "prepared",
            (),
            source_root_identities=cleanup_inputs.source_root_identities,
        ),
    ) as reservation:
        reservation.publish(
            cleanup._report_for(
                plan,
                "in_progress",
                (),
                quarantine_root_identity=(quarantine_stat.st_dev, quarantine_stat.st_ino),
                source_root_identities=cleanup_inputs.source_root_identities,
                intent=intent,
                active_target=target.relative_path,
            )
        )
    source.rename(slot)
    original_lstat = Path.lstat

    def unreadable_original(path: Path) -> os.stat_result:
        if path == source:
            raise PermissionError(str(path))
        return original_lstat(path)

    monkeypatch.setattr(Path, "lstat", unreadable_original)

    recovery = cleanup.assess_cleanup_recovery(
        report_path,
        repo_root=cleanup_inputs.root,
        protected_roots=cleanup_inputs.roots,
    )

    assert recovery.state == "conflict"
    assert recovery.detach_events == ()


def test_cleanup_recovery_rejects_escaped_slot_before_identity_lookup(
    cleanup_inputs: SimpleNamespace,
) -> None:
    plan = build_cleanup_plan(**cleanup_inputs.kwargs)
    target = plan.targets[0]
    escaped_slot = "../../held-original-root"
    forged_target = replace(target, quarantine_slot=escaped_slot)
    forged_plan = replace(
        plan,
        targets=(forged_target, *plan.targets[1:]),
    )
    cleanup_inputs.quarantine_root.mkdir()
    quarantine_stat = cleanup_inputs.quarantine_root.lstat()
    intent = cleanup.QuarantineIntent(
        source_relative_path=target.relative_path,
        quarantine_slot=escaped_slot,
        quarantine_root_device=quarantine_stat.st_dev,
        quarantine_root_inode=quarantine_stat.st_ino,
        device=target.device,
        inode=target.inode,
        content_digest=target.content_digest,
    )
    report_path = Path("artifacts/cleanup/v1.2/report.json")
    with cleanup._reserve_cleanup_report(
        cleanup_inputs.root,
        report_path,
        protected_roots=cleanup_inputs.roots,
        initial=cleanup._report_for(
            forged_plan,
            "prepared",
            (),
            source_root_identities=cleanup_inputs.source_root_identities,
        ),
    ) as reservation:
        reservation.publish(
            cleanup._report_for(
                forged_plan,
                "in_progress",
                (),
                quarantine_root_identity=(quarantine_stat.st_dev, quarantine_stat.st_ino),
                source_root_identities=cleanup_inputs.source_root_identities,
                intent=intent,
                active_target=target.relative_path,
            )
        )
    escaped_destination = cleanup_inputs.quarantine_root / PurePosixPath(escaped_slot)
    (cleanup_inputs.root / PurePosixPath(target.relative_path)).rename(
        escaped_destination
    )

    with pytest.raises(CleanupError, match="slot binding"):
        cleanup.assess_cleanup_recovery(
            report_path,
            repo_root=cleanup_inputs.root,
            protected_roots=cleanup_inputs.roots,
        )


def test_cleanup_recovery_classifies_partial_event_checkpoint_as_partial(
    cleanup_inputs: SimpleNamespace,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    class SimulatedProcessInterruption(BaseException):
        pass

    plan = build_cleanup_plan(**cleanup_inputs.kwargs)
    target = plan.targets[0]
    original_publish = cleanup._ReportReservation.publish
    event_appended = False

    def interrupt_after_event_append(
        reservation: cleanup._ReportReservation,
        report: cleanup.CleanupReport,
    ) -> None:
        nonlocal event_appended
        original_publish(reservation, report)
        if (
            not event_appended
            and report.status == "in_progress"
            and report.intent is None
            and tuple(event.source_relative_path for event in report.detach_events)
            == (target.relative_path,)
            and report.current_observation is None
        ):
            event_appended = True
            raise SimulatedProcessInterruption

    monkeypatch.setattr(
        cleanup._ReportReservation,
        "publish",
        interrupt_after_event_append,
    )

    with pytest.raises(SimulatedProcessInterruption):
        _execute(plan, cleanup_inputs)

    recovery = cleanup.assess_cleanup_recovery(
        Path("artifacts/cleanup/v1.2/report.json"),
        repo_root=cleanup_inputs.root,
        protected_roots=cleanup_inputs.roots,
    )

    assert event_appended is True
    assert recovery.state == "partial"
    assert recovery.target is None
    assert len(recovery.detach_events) == 1 < len(plan.targets)
    assert recovery.current_observation is not None
    assert recovery.current_observation.assessment == "no_drift_observed"


def test_cleanup_recovery_observes_drift_after_event_process_interruption(
    cleanup_inputs: SimpleNamespace,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    class SimulatedProcessInterruption(BaseException):
        pass

    plan = build_cleanup_plan(**cleanup_inputs.kwargs)
    target = plan.targets[0]
    slot = cleanup_inputs.quarantine_root / PurePosixPath(target.quarantine_slot)
    original_publish = cleanup._ReportReservation.publish
    event_appended = False

    def interrupt_after_event_append(
        reservation: cleanup._ReportReservation,
        report: cleanup.CleanupReport,
    ) -> None:
        nonlocal event_appended
        original_publish(reservation, report)
        if (
            not event_appended
            and report.status == "in_progress"
            and report.intent is None
            and tuple(event.source_relative_path for event in report.detach_events)
            == (target.relative_path,)
            and report.current_observation is None
        ):
            event_appended = True
            raise SimulatedProcessInterruption

    monkeypatch.setattr(
        cleanup._ReportReservation,
        "publish",
        interrupt_after_event_append,
    )

    with pytest.raises(SimulatedProcessInterruption):
        _execute(plan, cleanup_inputs)

    assert event_appended is True
    (slot / "late-unbound-file.jpg").write_bytes(b"post-crash-drift")
    recovery = cleanup.assess_cleanup_recovery(
        Path("artifacts/cleanup/v1.2/report.json"),
        repo_root=cleanup_inputs.root,
        protected_roots=cleanup_inputs.roots,
    )
    assert recovery.state == "partial"
    assert recovery.target is None
    assert recovery.current_observation is not None
    assert recovery.current_observation.targets[0].slot_state == "content_drift"


def test_cleanup_source_recreation_during_detach_is_preserved_as_current_drift(
    cleanup_inputs: SimpleNamespace,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    plan = build_cleanup_plan(**cleanup_inputs.kwargs)
    original_detach = cleanup._detach_target_to_quarantine

    def recreate_after_move(
        source: Path,
        destination: Path,
        target: cleanup.CleanupTarget,
        **kwargs: object,
    ) -> cleanup.DetachEvent:
        event = original_detach(source, destination, target, **kwargs)  # type: ignore[arg-type]
        source.mkdir(parents=True)
        (source / "unauthorized.txt").write_text("new", encoding="utf-8")
        return event

    monkeypatch.setattr(cleanup, "_detach_target_to_quarantine", recreate_after_move)

    report = _execute(plan, cleanup_inputs)

    assert report.status == "detached_with_drift"
    assert len(report.detach_events) == 1
    assert report.current_observation is not None
    assert report.current_observation.targets[0].source_state == "recreated"
    source = cleanup_inputs.root / PurePosixPath(plan.targets[0].relative_path)
    assert (source / "unauthorized.txt").read_text(encoding="utf-8") == "new"
    slot = cleanup_inputs.quarantine_root / PurePosixPath(
        plan.targets[0].quarantine_slot
    )
    assert (slot / "a.jpg").read_bytes() == b"outer"


def test_cleanup_multi_target_partial_failure_preserves_prior_detach_events(
    cleanup_inputs: SimpleNamespace,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    plan = build_cleanup_plan(**cleanup_inputs.kwargs)
    original_detach = cleanup._detach_target_to_quarantine
    calls = 0

    def fail_second(
        source: Path,
        destination: Path,
        target: cleanup.CleanupTarget,
        **kwargs: object,
    ) -> cleanup.DetachEvent:
        nonlocal calls
        calls += 1
        if calls == 2:
            raise OSError("simulated second quarantine failure")
        return original_detach(source, destination, target, **kwargs)  # type: ignore[arg-type]

    monkeypatch.setattr(cleanup, "_detach_target_to_quarantine", fail_second)

    report = _execute(plan, cleanup_inputs)

    assert report.status == "failed"
    assert tuple(event.source_relative_path for event in report.detach_events) == (
        plan.targets[0].relative_path,
    )
    assert report.intent is not None
    assert report.intent.source_relative_path == plan.targets[1].relative_path
    first_slot = cleanup_inputs.quarantine_root / PurePosixPath(
        plan.targets[0].quarantine_slot
    )
    second_source = cleanup_inputs.root / PurePosixPath(plan.targets[1].relative_path)
    assert first_slot.exists()
    assert second_source.exists()
    recovery = cleanup.assess_cleanup_recovery(
        Path("artifacts/cleanup/v1.2/report.json"),
        repo_root=cleanup_inputs.root,
        protected_roots=cleanup_inputs.roots,
    )
    assert recovery.state == "not_detached"
    assert recovery.detach_events == report.detach_events
    assert len(recovery.detach_events) == 1 < len(plan.targets)


def test_cleanup_revalidates_realistic_1631_file_nested_target(
    cleanup_inputs: SimpleNamespace, monkeypatch: pytest.MonkeyPatch
) -> None:
    entries = list(cleanup_inputs.entries)
    existing = sum(
        1
        for entry in entries
        if str(entry["relative_path"]).startswith("posters/__MACOSX/")
    )
    for index in range(1631 - existing):
        relative = f"posters/__MACOSX/scale/{index // 100:02d}/._{index:04d}.jpg"
        content = f"m{index}".encode()
        path = cleanup_inputs.root / relative
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_bytes(content)
        entries.append(_inventory_line(relative, content, "macos_metadata"))
    entries.sort(key=lambda item: str(item["relative_path"]))
    cleanup_inputs.inventory.write_bytes(b"".join(_json_bytes(entry) for entry in entries))
    inventory_digest = hashlib.sha256(cleanup_inputs.inventory.read_bytes()).hexdigest()
    total_bytes = sum(int(entry["size_bytes"]) for entry in entries)
    report = dict(cleanup_inputs.report)
    report.update(
        {
            "inventory_digest": inventory_digest,
            "object_count": len(entries),
            "objects": [
                {
                    "action": "verified",
                    "generation": index + 1,
                    "hash_verified": True,
                    "object_name": f"{cleanup._V12_AUTHORITY.prefix}/files/{entry['relative_path']}",
                    "sha256": entry["sha256"],
                }
                for index, entry in enumerate(entries)
            ],
            "total_bytes": total_bytes,
        }
    )
    cleanup_inputs.archive.write_bytes(_json_bytes(report))
    monkeypatch.setattr(
        cleanup,
        "_V12_AUTHORITY",
        replace(
            cleanup._V12_AUTHORITY,
            archive_report_sha256=hashlib.sha256(cleanup_inputs.archive.read_bytes()).hexdigest(),
            inventory_digest=inventory_digest,
            object_count=len(entries),
            total_bytes=total_bytes,
        ),
    )

    plan = build_cleanup_plan(**cleanup_inputs.kwargs)
    target = next(item for item in plan.targets if item.relative_path == "posters/__MACOSX")

    assert target.file_count == 1631
    validate_cleanup_plan(plan, repo_root=cleanup_inputs.root)


def test_cleanup_refuses_unverified_archive(cleanup_inputs: SimpleNamespace) -> None:
    payload = dict(cleanup_inputs.report)
    payload["verified"] = False
    cleanup_inputs.archive.write_bytes(_json_bytes(payload))
    cleanup._V12_AUTHORITY = replace(
        cleanup._V12_AUTHORITY,
        archive_report_sha256=hashlib.sha256(cleanup_inputs.archive.read_bytes()).hexdigest(),
    )

    with pytest.raises(CleanupError, match="archive is not verified"):
        build_cleanup_plan(**cleanup_inputs.kwargs)


@pytest.mark.parametrize(
    ("mutator", "message"),
    [
        (lambda report: report.update({"inventory_digest": "0" * 64}), "inventory digest"),
        (lambda report: report["objects"].pop(), "object set"),
        (lambda report: report["objects"][0].update({"generation": "1"}), "generation"),
        (lambda report: report["objects"][0].update({"action": "uploaded"}), "object schema"),
        (lambda report: report["objects"][0].update({"hash_verified": False}), "object schema"),
        (lambda report: report["objects"][0].update({"sha256": "0" * 64}), "inventory SHA"),
        (lambda report: report.update({"bucket": "wrong"}), "authority"),
        (lambda report: report.update({"prefix": "source/v1.1"}), "authority"),
        (lambda report: report.update({"object_count": 1}), "counts"),
        (lambda report: report.update({"total_bytes": 1}), "counts"),
        (lambda report: report.update({"extra": True}), "report schema"),
    ],
)
def test_cleanup_rejects_wrong_archive_report_contract(
    cleanup_inputs: SimpleNamespace,
    mutator: object,
    message: str,
) -> None:
    payload = json.loads(json.dumps(cleanup_inputs.report))
    mutator(payload)  # type: ignore[operator]
    cleanup_inputs.archive.write_bytes(_json_bytes(payload))
    cleanup._V12_AUTHORITY = replace(
        cleanup._V12_AUTHORITY,
        archive_report_sha256=hashlib.sha256(cleanup_inputs.archive.read_bytes()).hexdigest(),
    )

    with pytest.raises(CleanupError, match=message):
        build_cleanup_plan(**cleanup_inputs.kwargs)


def test_cleanup_rejects_archive_report_with_wrong_artifact_sha256(
    cleanup_inputs: SimpleNamespace,
) -> None:
    cleanup_inputs.archive.write_bytes(cleanup_inputs.archive.read_bytes() + b" ")

    with pytest.raises(CleanupError, match="archive verification artifact SHA-256"):
        build_cleanup_plan(**cleanup_inputs.kwargs)


def test_cleanup_rejects_tampered_inventory(cleanup_inputs: SimpleNamespace) -> None:
    cleanup_inputs.inventory.write_bytes(cleanup_inputs.inventory.read_bytes() + b"\n")

    with pytest.raises(CleanupError, match="inventory digest"):
        build_cleanup_plan(**cleanup_inputs.kwargs)


def test_cleanup_recomputes_duplicate_proof(cleanup_inputs: SimpleNamespace) -> None:
    cleanup_inputs.duplicate_proof.write_bytes(
        _json_bytes(
            {
                "pairs": [
                    {
                        "canonical_root": "1-1500posters",
                        "duplicate_root": "nested_1-1500posters",
                        "file_count": 1,
                    }
                ]
            }
        )
    )

    with pytest.raises(CleanupError, match="duplicate proof"):
        build_cleanup_plan(**cleanup_inputs.kwargs)


def test_cleanup_rejects_workspace_root_or_unlisted_path(cleanup_inputs: SimpleNamespace) -> None:
    plan = build_cleanup_plan(**cleanup_inputs.kwargs)
    root_target = replace(plan.targets[0], relative_path=".")
    unlisted_target = replace(plan.targets[0], relative_path="FinalDelivery_2026-07-16")

    with pytest.raises(CleanupError, match="target is not allowlisted"):
        validate_cleanup_plan(
            replace(plan, targets=(root_target,), plan_sha256="ignored"),
            repo_root=cleanup_inputs.root,
        )
    with pytest.raises(CleanupError, match="target is not allowlisted"):
        validate_cleanup_plan(
            replace(plan, targets=(unlisted_target,), plan_sha256="ignored"),
            repo_root=cleanup_inputs.root,
        )


def test_cleanup_rejects_reparse_target(cleanup_inputs: SimpleNamespace) -> None:
    target = cleanup_inputs.root / "posters" / "__MACOSX"
    real = cleanup_inputs.root / "metadata-real"
    target.rename(real)
    try:
        target.symlink_to(real, target_is_directory=True)
    except OSError:
        pytest.skip("directory symlink creation is unavailable")

    with pytest.raises(CleanupError, match="no-follow"):
        build_cleanup_plan(**cleanup_inputs.kwargs)


def test_cleanup_rejects_target_marked_as_reparse_without_platform_symlink_privilege(
    cleanup_inputs: SimpleNamespace, monkeypatch: pytest.MonkeyPatch
) -> None:
    target = cleanup_inputs.root / "posters" / "__MACOSX"
    target_identity = (target.lstat().st_dev, target.lstat().st_ino)
    real_is_reparse = cleanup._is_reparse

    def mark_target_reparse(value: os.stat_result) -> bool:
        return (value.st_dev, value.st_ino) == target_identity or real_is_reparse(value)

    monkeypatch.setattr(cleanup, "_is_reparse", mark_target_reparse)

    with pytest.raises(CleanupError, match="no-follow"):
        build_cleanup_plan(**cleanup_inputs.kwargs)


def test_cleanup_rejects_hardlinked_target_content(
    cleanup_inputs: SimpleNamespace,
) -> None:
    target = cleanup_inputs.root / "posters" / "posters" / ".DS_Store"
    linked = cleanup_inputs.root / "linked-dirty-metadata"
    os.link(target, linked)

    with pytest.raises(CleanupError, match="hard link"):
        build_cleanup_plan(**cleanup_inputs.kwargs)


def test_cleanup_plan_writer_rejects_protected_alias_reparse_and_hardlink_outputs(
    cleanup_inputs: SimpleNamespace,
) -> None:
    plan = build_cleanup_plan(**cleanup_inputs.kwargs)
    with pytest.raises(CleanupError, match="exact v1.2 plan path"):
        write_cleanup_plan(
            plan,
            Path("posters/plan.json"),
            repo_root=cleanup_inputs.root,
            protected_roots=cleanup_inputs.roots,
        )
    with pytest.raises(CleanupError, match="exact v1.2 plan path"):
        write_cleanup_plan(
            plan,
            Path("artifacts/../posters/plan.json"),
            repo_root=cleanup_inputs.root,
            protected_roots=cleanup_inputs.roots,
        )
    for unsafe_alias in (
        Path("artifacts/plan.json:alternate"),
        Path("ARTIFA~1/plan.json"),
        Path("CON/plan.json"),
    ):
        with pytest.raises(CleanupError, match="exact v1.2 plan path"):
            write_cleanup_plan(
                plan,
                unsafe_alias,
                repo_root=cleanup_inputs.root,
                protected_roots=cleanup_inputs.roots,
            )

    hardlink = cleanup_inputs.root / "artifacts" / "cleanup" / "v1.2" / "plan.json"
    hardlink.parent.mkdir(parents=True)
    original = hardlink.with_name("other.json")
    original.write_text("{}", encoding="utf-8")
    os.link(original, hardlink)
    with pytest.raises(CleanupError, match="already exists"):
        write_cleanup_plan(
            plan,
            hardlink.relative_to(cleanup_inputs.root),
            repo_root=cleanup_inputs.root,
            protected_roots=cleanup_inputs.roots,
        )

    linked_parent = cleanup_inputs.root / "linked-output"
    real_parent = cleanup_inputs.root / "real-output"
    real_parent.mkdir()
    try:
        linked_parent.symlink_to(real_parent, target_is_directory=True)
    except OSError:
        return
    with pytest.raises(CleanupError, match="exact v1.2 plan path"):
        write_cleanup_plan(
            plan,
            Path("linked-output/plan.json"),
            repo_root=cleanup_inputs.root,
            protected_roots=cleanup_inputs.roots,
        )


def test_cleanup_plan_output_is_exact_and_create_only(cleanup_inputs: SimpleNamespace) -> None:
    plan = build_cleanup_plan(**cleanup_inputs.kwargs)
    for output in (
        Path("README.md"),
        Path(".git/config"),
        Path("data/release/v1.2/release_manifest.json"),
        Path("artifacts/inventory/v1.2/source_inventory.jsonl"),
        Path("artifacts/archive/v1.2/verification.json"),
        Path("artifacts/cleanup/v1.2/report.json"),
    ):
        with pytest.raises(CleanupError, match="exact v1.2 plan path"):
            write_cleanup_plan(
                plan,
                output,
                repo_root=cleanup_inputs.root,
                protected_roots=cleanup_inputs.roots,
            )

    output = Path("artifacts/cleanup/v1.2/plan.json")
    write_cleanup_plan(
        plan,
        output,
        repo_root=cleanup_inputs.root,
        protected_roots=cleanup_inputs.roots,
    )
    original = (cleanup_inputs.root / output).read_bytes()
    with pytest.raises(CleanupError, match="already exists"):
        write_cleanup_plan(
            plan,
            output,
            repo_root=cleanup_inputs.root,
            protected_roots=cleanup_inputs.roots,
        )
    assert (cleanup_inputs.root / output).read_bytes() == original


@pytest.mark.parametrize(
    "unsafe_output",
    [
        Path("README.md"),
        Path(".git/config"),
        Path("artifacts/cleanup/v1.2/plan.json"),
        Path("artifacts/archive/v1.2/verification.json"),
    ],
)
def test_cleanup_unsafe_report_output_causes_zero_actions(
    cleanup_inputs: SimpleNamespace,
    monkeypatch: pytest.MonkeyPatch,
    unsafe_output: Path,
) -> None:
    plan = build_cleanup_plan(**cleanup_inputs.kwargs)
    calls: list[str] = []
    monkeypatch.setattr(
        cleanup,
        "_detach_target_to_quarantine",
        lambda source, destination, target: calls.append(str(source)),
    )

    with pytest.raises(CleanupError, match="exact v1.2 report path"):
        execute_cleanup(
            plan,
            "v1.2",
            repo_root=cleanup_inputs.root,
            report_output=unsafe_output,
            protected_roots=cleanup_inputs.roots,
        )

    assert calls == []


def test_cleanup_existing_or_unwritable_report_causes_zero_actions(
    cleanup_inputs: SimpleNamespace, monkeypatch: pytest.MonkeyPatch
) -> None:
    plan = build_cleanup_plan(**cleanup_inputs.kwargs)
    report = cleanup_inputs.root / "artifacts" / "cleanup" / "v1.2" / "report.json"
    report.parent.mkdir(parents=True)
    report.write_text("reviewed", encoding="utf-8")
    calls: list[str] = []
    monkeypatch.setattr(
        cleanup,
        "_detach_target_to_quarantine",
        lambda source, destination, target: calls.append(str(source)),
    )

    with pytest.raises(CleanupError, match="already exists"):
        _execute(plan, cleanup_inputs)
    assert report.read_text(encoding="utf-8") == "reviewed"
    assert calls == []

    report.unlink()
    monkeypatch.setattr(
        cleanup,
        "_open_create_only_descriptor",
        lambda path: (_ for _ in ()).throw(PermissionError("unwritable")),
    )
    with pytest.raises(CleanupError, match="cannot reserve cleanup report"):
        _execute(plan, cleanup_inputs)
    assert calls == []


def test_cleanup_report_parent_guard_failure_causes_zero_actions(
    cleanup_inputs: SimpleNamespace, monkeypatch: pytest.MonkeyPatch
) -> None:
    plan = build_cleanup_plan(**cleanup_inputs.kwargs)
    calls: list[str] = []
    original = cleanup._require_ancestor_identities

    def reject_report_parent(root: Path, parent: Path, expected: object) -> None:
        if parent == root / "artifacts" / "cleanup" / "v1.2":
            raise cleanup.PosterError("output ancestor identity changed")
        original(root, parent, expected)  # type: ignore[arg-type]

    monkeypatch.setattr(cleanup, "_require_ancestor_identities", reject_report_parent)
    monkeypatch.setattr(
        cleanup,
        "_detach_target_to_quarantine",
        lambda source, destination, target: calls.append(str(source)),
    )

    with pytest.raises(CleanupError, match="cannot reserve cleanup report"):
        _execute(plan, cleanup_inputs)

    assert calls == []


def test_cleanup_requires_exact_release_confirmation(cleanup_inputs: SimpleNamespace) -> None:
    plan = build_cleanup_plan(**cleanup_inputs.kwargs)

    with pytest.raises(CleanupError, match="confirmation mismatch"):
        _execute(plan, cleanup_inputs, confirmation="v1.1")


def test_cleanup_tampered_plan_causes_no_action(
    cleanup_inputs: SimpleNamespace, monkeypatch: pytest.MonkeyPatch
) -> None:
    plan = build_cleanup_plan(**cleanup_inputs.kwargs)
    tampered = replace(plan, release_version="v1.3")
    calls: list[str] = []
    monkeypatch.setattr(
        cleanup,
        "_detach_target_to_quarantine",
        lambda source, destination, target: calls.append(str(source)),
    )

    with pytest.raises(CleanupError, match="plan digest"):
        _execute(tampered, cleanup_inputs, confirmation="v1.3")

    assert calls == []


def test_cleanup_plan_rejects_absolute_evidence_alias_even_with_recomputed_digest(
    cleanup_inputs: SimpleNamespace,
) -> None:
    plan = build_cleanup_plan(**cleanup_inputs.kwargs)
    aliased = replace(
        plan,
        inventory=replace(plan.inventory, relative_path=str(cleanup_inputs.inventory)),
        plan_sha256="",
    )
    aliased = replace(aliased, plan_sha256=cleanup._plan_digest(aliased))

    with pytest.raises(CleanupError, match="evidence path"):
        validate_cleanup_plan(aliased, repo_root=cleanup_inputs.root)


def test_cleanup_plan_cannot_cross_workspace_roots(
    cleanup_inputs: SimpleNamespace, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    plan = build_cleanup_plan(**cleanup_inputs.kwargs)
    write_cleanup_plan(
        plan,
        Path("artifacts/cleanup/v1.2/plan.json"),
        repo_root=cleanup_inputs.root,
        protected_roots=cleanup_inputs.roots,
    )
    other = tmp_path / "other-worktree"
    other_roots = tuple(other / name for name in cleanup._SOURCE_ROOTS)
    for root in other_roots:
        root.mkdir(parents=True)
    copied = other / "artifacts" / "cleanup" / "v1.2" / "plan.json"
    copied.parent.mkdir(parents=True)
    copied.write_bytes(
        (cleanup_inputs.root / "artifacts" / "cleanup" / "v1.2" / "plan.json").read_bytes()
    )
    calls: list[str] = []
    monkeypatch.setattr(
        cleanup,
        "_detach_target_to_quarantine",
        lambda source, destination, target: calls.append(str(source)),
    )

    with pytest.raises(CleanupError, match="workspace root mismatch"):
        load_cleanup_plan(copied, repo_root=other, protected_roots=other_roots)
    with pytest.raises(CleanupError, match="workspace root mismatch"):
        execute_cleanup(
            plan,
            "v1.2",
            repo_root=other,
            report_output=Path("artifacts/cleanup/v1.2/report.json"),
            protected_roots=other_roots,
        )

    assert calls == []


def test_cleanup_plan_binds_workspace_physical_identity(cleanup_inputs: SimpleNamespace) -> None:
    plan = build_cleanup_plan(**cleanup_inputs.kwargs)
    tampered = replace(plan, workspace_inode=plan.workspace_inode + 1, plan_sha256="")
    tampered = replace(tampered, plan_sha256=cleanup._plan_digest(tampered))

    with pytest.raises(CleanupError, match="workspace physical identity mismatch"):
        validate_cleanup_plan(tampered, repo_root=cleanup_inputs.root)


def test_cleanup_reacquisition_failure_causes_no_action(
    cleanup_inputs: SimpleNamespace, monkeypatch: pytest.MonkeyPatch
) -> None:
    plan = build_cleanup_plan(**cleanup_inputs.kwargs)
    calls: list[str] = []
    original = cleanup._revalidate_target

    def fail_first(*args: object, **kwargs: object) -> None:
        raise CleanupError("target ancestor identity changed")

    monkeypatch.setattr(cleanup, "_revalidate_target", fail_first)
    monkeypatch.setattr(
        cleanup,
        "_detach_target_to_quarantine",
        lambda source, destination, target: calls.append(str(source)),
    )

    with pytest.raises(CleanupError, match="ancestor identity changed"):
        _execute(plan, cleanup_inputs)

    assert calls == []
    monkeypatch.setattr(cleanup, "_revalidate_target", original)


def test_cleanup_parent_swap_is_detected_before_first_quarantine_action(
    cleanup_inputs: SimpleNamespace, monkeypatch: pytest.MonkeyPatch
) -> None:
    plan = build_cleanup_plan(**cleanup_inputs.kwargs)
    original = cleanup._revalidate_target
    validation_calls = 0
    trash_calls: list[str] = []

    def swap_before_first_action(
        root: Path,
        target: cleanup.CleanupTarget,
        entries: object,
    ) -> None:
        nonlocal validation_calls
        validation_calls += 1
        if validation_calls == len(plan.targets) + 1:
            parent = root / "1-1500posters"
            parent.rename(root / "swapped-parent")
            (parent / "1-1500posters").mkdir(parents=True)
            (parent / "1-1500posters" / "a.jpg").write_bytes(b"outer")
        original(root, target, entries)  # type: ignore[arg-type]

    monkeypatch.setattr(cleanup, "_revalidate_target", swap_before_first_action)
    monkeypatch.setattr(
        cleanup,
        "_detach_target_to_quarantine",
        lambda source, destination, target: trash_calls.append(str(source)),
    )

    with pytest.raises(CleanupError, match="cannot reserve cleanup quarantine root"):
        _execute(plan, cleanup_inputs)

    assert trash_calls == []


@pytest.mark.parametrize("mutation", ["add", "replace", "directory-swap"])
def test_cleanup_deep_mutation_after_scan_causes_zero_actions(
    cleanup_inputs: SimpleNamespace,
    monkeypatch: pytest.MonkeyPatch,
    mutation: str,
) -> None:
    plan = build_cleanup_plan(**cleanup_inputs.kwargs)
    original = cleanup._verify_target_bindings
    mutated = False
    calls: list[str] = []

    def mutate_then_verify(
        root: Path,
        target: cleanup.CleanupTarget,
        entries: object,
    ) -> None:
        nonlocal mutated
        if not mutated and target.relative_path == "posters/__MACOSX":
            mutated = True
            deep = root / "posters" / "__MACOSX" / "deep"
            if mutation == "add":
                (deep / "uninventoried.txt").write_text("new", encoding="utf-8")
            elif mutation == "replace":
                source = deep / "leaf" / "._x.jpg"
                source.unlink()
                source.write_bytes(b"deep-metadata")
            else:
                leaf = deep / "leaf"
                leaf.rename(deep / "old-leaf")
                leaf.mkdir()
                (leaf / "._x.jpg").write_bytes(b"deep-metadata")
        original(root, target, entries)  # type: ignore[arg-type]

    monkeypatch.setattr(cleanup, "_verify_target_bindings", mutate_then_verify)
    monkeypatch.setattr(
        cleanup,
        "_detach_target_to_quarantine",
        lambda source, destination, target: calls.append(str(source)),
    )

    with pytest.raises(
        CleanupError,
        match="target .*changed|differs from inventory|uninventoried source",
    ):
        _execute(plan, cleanup_inputs)

    assert calls == []


def test_cleanup_deep_add_just_before_send_causes_zero_actions(
    cleanup_inputs: SimpleNamespace, monkeypatch: pytest.MonkeyPatch
) -> None:
    plan = build_cleanup_plan(**cleanup_inputs.kwargs)
    original = cleanup._verify_target_bindings
    calls_for_first_target = 0
    trash_calls: list[str] = []

    def add_before_final_verify(
        root: Path,
        target: cleanup.CleanupTarget,
        entries: object,
    ) -> None:
        nonlocal calls_for_first_target
        if target.relative_path == plan.targets[0].relative_path:
            calls_for_first_target += 1
            if calls_for_first_target == 3:
                (root / target.relative_path / "late.txt").write_text("late", encoding="utf-8")
        original(root, target, entries)  # type: ignore[arg-type]

    monkeypatch.setattr(cleanup, "_verify_target_bindings", add_before_final_verify)
    monkeypatch.setattr(
        cleanup,
        "_detach_target_to_quarantine",
        lambda source, destination, target: trash_calls.append(str(source)),
    )

    with pytest.raises(CleanupError, match="target changed after binding"):
        _execute(plan, cleanup_inputs)

    assert trash_calls == []




def test_cleanup_rechecks_early_source_after_later_deep_source_is_scanned(
    cleanup_inputs: SimpleNamespace, monkeypatch: pytest.MonkeyPatch
) -> None:
    plan = build_cleanup_plan(**cleanup_inputs.kwargs)
    original = cleanup._source_binding
    mutated = False
    calls: list[str] = []

    def replace_early_source(
        root: Path,
        path: Path,
        expected_entries: object,
    ) -> cleanup.SourceBinding:
        nonlocal mutated
        if not mutated and path.name == "._x.jpg":
            mutated = True
            early = root / "posters" / "__MACOSX" / "._b.jpg"
            early.unlink()
            early.write_bytes(b"metadata")
        return original(root, path, expected_entries)  # type: ignore[arg-type]

    monkeypatch.setattr(cleanup, "_source_binding", replace_early_source)
    monkeypatch.setattr(
        cleanup,
        "_detach_target_to_quarantine",
        lambda source, destination, target: calls.append(str(source)),
    )

    with pytest.raises(CleanupError, match="source bindings changed|target changed"):
        _execute(plan, cleanup_inputs)

    assert calls == []




@pytest.mark.parametrize(
    "fault",
    ["short-write", "write-error", "fsync-error", "interruption-before-fsync"],
)
def test_cleanup_checkpoint_fault_preserves_prior_parseable_record(
    cleanup_inputs: SimpleNamespace,
    monkeypatch: pytest.MonkeyPatch,
    fault: str,
) -> None:
    plan = build_cleanup_plan(**cleanup_inputs.kwargs)
    root = cleanup_inputs.root
    report_path = root / "artifacts" / "cleanup" / "v1.2" / "report.json"
    prepared = cleanup._report_for(
        plan,
        "prepared",
        (),
        source_root_identities=cleanup_inputs.source_root_identities,
    )
    original_write_all = cleanup._write_all
    original_fsync = cleanup.os.fsync

    with cleanup._reserve_cleanup_report(
        root,
        Path("artifacts/cleanup/v1.2/report.json"),
        protected_roots=cleanup_inputs.roots,
        initial=prepared,
    ) as reservation:
        prior = report_path.read_bytes()

        if fault == "short-write":
            def fail_write(descriptor: int, content: bytes) -> None:
                cleanup.os.write(descriptor, content[: max(1, len(content) // 2)])
                raise OSError("simulated short write")

            monkeypatch.setattr(cleanup, "_write_all", fail_write)
        elif fault == "write-error":
            monkeypatch.setattr(
                cleanup,
                "_write_all",
                lambda descriptor, content: (_ for _ in ()).throw(OSError("disk full")),
            )
        elif fault == "fsync-error":
            monkeypatch.setattr(
                cleanup.os,
                "fsync",
                lambda descriptor: (_ for _ in ()).throw(OSError("flush failed")),
            )
        else:
            def interrupt_after_write(descriptor: int, content: bytes) -> None:
                original_write_all(descriptor, content)
                raise KeyboardInterrupt("simulated process interruption")

            monkeypatch.setattr(cleanup, "_write_all", interrupt_after_write)

        raised = KeyboardInterrupt if fault == "interruption-before-fsync" else CleanupError
        with pytest.raises(raised):
            reservation.publish(
                cleanup._report_for(
                    plan,
                    "in_progress",
                    (),
                    quarantine_root_identity=(1, 2),
                    source_root_identities=cleanup_inputs.source_root_identities,
                )
            )

        current = report_path.read_bytes()
        assert current.startswith(prior)
        records = _checkpoint_records(report_path)
        assert records
        assert records[0]["status"] == "prepared"
        monkeypatch.setattr(cleanup, "_write_all", original_write_all)
        monkeypatch.setattr(cleanup.os, "fsync", original_fsync)

    recovered = cleanup.recover_cleanup_report_checkpoint(
        Path("artifacts/cleanup/v1.2/report.json"),
        repo_root=root,
        protected_roots=cleanup_inputs.roots,
    )
    assert recovered["status"] == records[-1]["status"]


def test_cleanup_release_guard_exit_failure_preserves_all_detach_events(
    cleanup_inputs: SimpleNamespace, monkeypatch: pytest.MonkeyPatch
) -> None:
    plan = build_cleanup_plan(**cleanup_inputs.kwargs)
    @contextmanager
    def failing_guard(*args: object, **kwargs: object) -> object:
        yield
        raise ReleaseLockError("simulated guard release failure")

    monkeypatch.setattr(cleanup, "release_guard", failing_guard)

    report = _execute(plan, cleanup_inputs)

    assert report.status == "failed"
    assert tuple(event.source_relative_path for event in report.detach_events) == tuple(
        target.relative_path for target in plan.targets
    )
    assert report.failed_target is None
    assert report.failure_reason == "release guard failed"


def test_cleanup_drift_then_guard_exit_failure_recovers_failed_checkpoint(
    cleanup_inputs: SimpleNamespace,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    plan = build_cleanup_plan(**cleanup_inputs.kwargs)
    original_detach = cleanup._detach_target_to_quarantine

    def mutate_first_slot(
        source: Path,
        destination: Path,
        target: cleanup.CleanupTarget,
        **kwargs: object,
    ) -> cleanup.DetachEvent:
        event = original_detach(source, destination, target, **kwargs)  # type: ignore[arg-type]
        (destination / "a.jpg").write_bytes(b"first-event-drift")
        return event

    @contextmanager
    def failing_guard(*args: object, **kwargs: object) -> object:
        yield
        raise ReleaseLockError("simulated guard release failure after drift")

    monkeypatch.setattr(cleanup, "_detach_target_to_quarantine", mutate_first_slot)
    monkeypatch.setattr(cleanup, "release_guard", failing_guard)

    report = _execute(plan, cleanup_inputs)
    relative_report = Path("artifacts/cleanup/v1.2/report.json")
    records = _checkpoint_records(cleanup_inputs.root / relative_report)
    recovered = cleanup.recover_cleanup_report_checkpoint(
        relative_report,
        repo_root=cleanup_inputs.root,
        protected_roots=cleanup_inputs.roots,
    )
    recovery = cleanup.assess_cleanup_recovery(
        relative_report,
        repo_root=cleanup_inputs.root,
        protected_roots=cleanup_inputs.roots,
    )

    assert report.status == "failed"
    assert report.failure_reason == "release guard failed"
    assert len(report.detach_events) == 1 < len(plan.targets)
    assert records[-1]["status"] == "failed"
    assert recovered["status"] == "failed"
    assert recovery.state == "partial"
    assert recovery.current_observation is not None
    assert recovery.current_observation.assessment == "drift_observed"


def test_cleanup_revalidates_all_targets_before_first_action(
    cleanup_inputs: SimpleNamespace, monkeypatch: pytest.MonkeyPatch
) -> None:
    plan = build_cleanup_plan(**cleanup_inputs.kwargs)
    last_source = cleanup_inputs.root / plan.targets[-1].sources[0].relative_path
    last_source.write_bytes(b"changed")
    calls: list[str] = []
    monkeypatch.setattr(
        cleanup,
        "_detach_target_to_quarantine",
        lambda source, destination, target: calls.append(str(source)),
    )

    with pytest.raises(CleanupError, match="target source changed"):
        _execute(plan, cleanup_inputs)

    assert calls == []




def test_cleanup_plan_load_accepts_the_bound_create_only_artifact(
    cleanup_inputs: SimpleNamespace,
) -> None:
    plan = build_cleanup_plan(**cleanup_inputs.kwargs)
    path = Path("artifacts/cleanup/v1.2/plan.json")
    write_cleanup_plan(
        plan,
        path,
        repo_root=cleanup_inputs.root,
        protected_roots=cleanup_inputs.roots,
    )
    loaded = load_cleanup_plan(
        cleanup_inputs.root / path,
        repo_root=cleanup_inputs.root,
        protected_roots=cleanup_inputs.roots,
    )
    assert loaded == plan


def test_cleanup_plan_cli_binds_caller_approved_external_quarantine_root(
    cleanup_inputs: SimpleNamespace,
    monkeypatch: pytest.MonkeyPatch,
    capsys: pytest.CaptureFixture[str],
) -> None:
    monkeypatch.setattr(
        cli.Settings,
        "load",
        lambda _: SimpleNamespace(
            repo_root=cleanup_inputs.root,
            release_version="v1.2",
            inventory_roots=cleanup_inputs.roots,
        ),
    )
    monkeypatch.setattr(
        sys,
        "argv",
        [
            "hk-movie-rag",
            "cleanup-plan",
            "--inventory",
            str(cleanup_inputs.inventory),
            "--archive-verification",
            str(cleanup_inputs.archive),
            "--quarantine-root",
            str(cleanup_inputs.quarantine_root),
            "--output",
            "artifacts/cleanup/v1.2/plan.json",
        ],
    )

    cli.main()

    emitted = json.loads(capsys.readouterr().out)
    assert emitted["schema_version"] == "cleanup-plan/v2"
    assert emitted["quarantine_root"] == str(
        cleanup_inputs.quarantine_root.resolve(strict=False)
    )


def test_cleanup_execute_cli_accepts_only_drift_free_detach_completion(
    cleanup_inputs: SimpleNamespace,
    monkeypatch: pytest.MonkeyPatch,
    capsys: pytest.CaptureFixture[str],
) -> None:
    plan = build_cleanup_plan(**cleanup_inputs.kwargs)
    plan_path = Path("artifacts/cleanup/v1.2/plan.json")
    write_cleanup_plan(
        plan,
        plan_path,
        repo_root=cleanup_inputs.root,
        protected_roots=cleanup_inputs.roots,
    )
    monkeypatch.setattr(
        cli.Settings,
        "load",
        lambda _: SimpleNamespace(
            repo_root=cleanup_inputs.root,
            release_version="v1.2",
            inventory_roots=cleanup_inputs.roots,
        ),
    )
    monkeypatch.setattr(
        sys,
        "argv",
        [
            "hk-movie-rag",
            "cleanup-execute",
            "--plan",
            str(plan_path),
            "--confirm-release",
            "v1.2",
            "--output",
            "artifacts/cleanup/v1.2/report.json",
        ],
    )

    cli.main()

    emitted = json.loads(capsys.readouterr().out)
    assert emitted["schema_version"] == "cleanup-report/v4"
    assert emitted["status"] == "detached"
    assert emitted["rag_readiness"] == "not_authorized"


def test_cleanup_cli_emits_json_without_traceback_on_confirmation_failure(
    cleanup_inputs: SimpleNamespace,
    monkeypatch: pytest.MonkeyPatch,
    capsys: pytest.CaptureFixture[str],
) -> None:
    plan = build_cleanup_plan(**cleanup_inputs.kwargs)
    plan_path = Path("artifacts/cleanup/v1.2/plan.json")
    write_cleanup_plan(
        plan,
        plan_path,
        repo_root=cleanup_inputs.root,
        protected_roots=cleanup_inputs.roots,
    )
    monkeypatch.setattr(
        cli.Settings,
        "load",
        lambda _: SimpleNamespace(
            repo_root=cleanup_inputs.root,
            release_version="v1.2",
            inventory_roots=cleanup_inputs.roots,
        ),
    )
    monkeypatch.setattr(
        sys,
        "argv",
        [
            "hk-movie-rag",
            "cleanup-execute",
            "--plan",
            str(plan_path),
            "--confirm-release",
            "v1.1",
            "--output",
            "artifacts/cleanup/v1.2/report.json",
        ],
    )

    with pytest.raises(SystemExit) as raised:
        cli.main()

    assert raised.value.code == 1
    captured = capsys.readouterr()
    assert captured.err == ""
    assert json.loads(captured.out) == {
        "error": "cleanup_operation_failed",
        "ok": False,
        "reason": "confirmation mismatch",
    }
    assert "Traceback" not in captured.out


def test_cleanup_cli_persists_partial_failure_before_nonzero_exit(
    cleanup_inputs: SimpleNamespace,
    monkeypatch: pytest.MonkeyPatch,
    capsys: pytest.CaptureFixture[str],
) -> None:
    plan = build_cleanup_plan(**cleanup_inputs.kwargs)
    plan_path = Path("artifacts/cleanup/v1.2/plan.json")
    write_cleanup_plan(
        plan,
        plan_path,
        repo_root=cleanup_inputs.root,
        protected_roots=cleanup_inputs.roots,
    )
    monkeypatch.setattr(
        cli.Settings,
        "load",
        lambda _: SimpleNamespace(
            repo_root=cleanup_inputs.root,
            release_version="v1.2",
            inventory_roots=cleanup_inputs.roots,
        ),
    )
    calls = 0
    original_detach = cleanup._detach_target_to_quarantine

    def fail_second(
        source: Path,
        destination: Path,
        target: cleanup.CleanupTarget,
        **kwargs: object,
    ) -> cleanup.DetachEvent:
        nonlocal calls
        calls += 1
        if calls == 2:
            raise OSError("simulated quarantine failure")
        return original_detach(source, destination, target, **kwargs)  # type: ignore[arg-type]

    monkeypatch.setattr(cleanup, "_detach_target_to_quarantine", fail_second)
    monkeypatch.setattr(
        sys,
        "argv",
        [
            "hk-movie-rag",
            "cleanup-execute",
            "--plan",
            str(plan_path),
            "--confirm-release",
            "v1.2",
            "--output",
            "artifacts/cleanup/v1.2/report.json",
        ],
    )

    with pytest.raises(SystemExit) as raised:
        cli.main()

    assert raised.value.code == 1
    captured = capsys.readouterr()
    assert captured.err == ""
    emitted = json.loads(captured.out)
    durable = _checkpoint_records(
        cleanup_inputs.root / "artifacts" / "cleanup" / "v1.2" / "report.json"
    )[-1]
    assert emitted == durable
    assert durable["status"] == "failed"
    assert [
        event["source_relative_path"] for event in durable["detach_events"]
    ] == [plan.targets[0].relative_path]
    assert durable["failed_target"] == plan.targets[1].relative_path
    assert "Traceback" not in captured.out


def test_cleanup_cli_checkpoint_failure_after_move_is_nonzero_and_prior_record_recovers(
    cleanup_inputs: SimpleNamespace,
    monkeypatch: pytest.MonkeyPatch,
    capsys: pytest.CaptureFixture[str],
) -> None:
    plan = build_cleanup_plan(**cleanup_inputs.kwargs)
    plan_path = Path("artifacts/cleanup/v1.2/plan.json")
    write_cleanup_plan(
        plan,
        plan_path,
        repo_root=cleanup_inputs.root,
        protected_roots=cleanup_inputs.roots,
    )
    monkeypatch.setattr(
        cli.Settings,
        "load",
        lambda _: SimpleNamespace(
            repo_root=cleanup_inputs.root,
            release_version="v1.2",
            inventory_roots=cleanup_inputs.roots,
        ),
    )
    original_write_all = cleanup._write_all
    writes = 0

    def fail_first_post_move_checkpoint(descriptor: int, content: bytes) -> None:
        nonlocal writes
        writes += 1
        if writes == 3:
            cleanup.os.write(descriptor, content[: max(1, len(content) // 3)])
            raise OSError("simulated disk full after quarantine")
        original_write_all(descriptor, content)

    monkeypatch.setattr(cleanup, "_write_all", fail_first_post_move_checkpoint)
    monkeypatch.setattr(
        sys,
        "argv",
        [
            "hk-movie-rag",
            "cleanup-execute",
            "--plan",
            str(plan_path),
            "--confirm-release",
            "v1.2",
            "--output",
            "artifacts/cleanup/v1.2/report.json",
        ],
    )

    with pytest.raises(SystemExit) as raised:
        cli.main()

    assert raised.value.code != 0
    captured = capsys.readouterr()
    assert captured.err == ""
    error = json.loads(captured.out)
    assert error["ok"] is False
    assert error["reason"] == "cannot publish cleanup report"
    first_quarantined = (
        cleanup_inputs.quarantine_root
        / PurePosixPath(plan.targets[0].quarantine_slot)
    )
    assert first_quarantined.exists()
    report_path = cleanup_inputs.root / "artifacts" / "cleanup" / "v1.2" / "report.json"
    records = _checkpoint_records(report_path)
    assert [record["status"] for record in records] == ["prepared", "in_progress"]
    assert records[-1]["active_target"] == plan.targets[0].relative_path
    assert records[-1]["detach_events"] == []
    recovered = cleanup.recover_cleanup_report_checkpoint(
        Path("artifacts/cleanup/v1.2/report.json"),
        repo_root=cleanup_inputs.root,
        protected_roots=cleanup_inputs.roots,
    )
    assert recovered == records[-1]


def test_cleanup_cli_unsafe_report_output_exits_before_any_action(
    cleanup_inputs: SimpleNamespace,
    monkeypatch: pytest.MonkeyPatch,
    capsys: pytest.CaptureFixture[str],
) -> None:
    plan = build_cleanup_plan(**cleanup_inputs.kwargs)
    plan_path = Path("artifacts/cleanup/v1.2/plan.json")
    write_cleanup_plan(
        plan,
        plan_path,
        repo_root=cleanup_inputs.root,
        protected_roots=cleanup_inputs.roots,
    )
    monkeypatch.setattr(
        cli.Settings,
        "load",
        lambda _: SimpleNamespace(
            repo_root=cleanup_inputs.root,
            release_version="v1.2",
            inventory_roots=cleanup_inputs.roots,
        ),
    )
    calls: list[str] = []
    monkeypatch.setattr(
        cleanup,
        "_detach_target_to_quarantine",
        lambda source, destination, target: calls.append(str(source)),
    )
    monkeypatch.setattr(
        sys,
        "argv",
        [
            "hk-movie-rag",
            "cleanup-execute",
            "--plan",
            str(plan_path),
            "--confirm-release",
            "v1.2",
            "--output",
            "README.md",
        ],
    )

    with pytest.raises(SystemExit) as raised:
        cli.main()

    assert raised.value.code == 1
    assert calls == []
    captured = capsys.readouterr()
    assert captured.err == ""
    assert json.loads(captured.out)["reason"] == (
        "cleanup output must use the exact v1.2 report path"
    )
