# Task 10 Stable Acceptance and Terminal Recovery Fix Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development
> (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use
> checkbox (`- [ ]`) syntax for tracking.

**Goal:** Make stable quarantine acceptance recoverable and force every no-intent checkpoint to
re-prove all accepted slots, source absence, source-root safety, and quarantine-root identity.

**Architecture:** `cleanup-report/v3` persists all source-root identities and the reserved
quarantine-root device/inode even when `intent` is null. Per-target acceptance appends the stable
no-intent record as phase one, then immediately performs a complete post-stable proof; any
mutation appends an overriding failed checkpoint that excludes the current target and retains its
intent. Recovery treats every terminal or stable no-intent record as an assertion to verify,
never as proof by itself.

**Tech Stack:** Python 3.11, append-only JSONL, pathlib/os identity and hash proofs, Windows/POSIX
filesystem test schedules, pytest, Ruff, mypy, uv.

## Global Constraints

- Strict TDD: observe every reviewer schedule fail before production changes.
- Use pytest temporary workspaces only; never generate or execute a live cleanup plan.
- Do not call GCP, Cloud SDK, Storage, Terraform, or touch raw movie/poster data.
- Do not add ACL/DACL/provider/recycle/delete/rollback behavior.
- Directory descriptors do not prevent a child entry from being added; documentation and code
  comments must not claim otherwise.
- Acceptance is point-in-time at completion of post-stable validation. Later drift is detected
  only by an explicit post-cleanup/recovery acceptance check.

---

### Task 1: Reproduce the real stable-acceptance schedules

**Files:**
- Modify: `tests/test_cleanup.py`

**Interfaces:**
- Consumes: `_ReportReservation.publish`, `_require_source_namespace_absent`, and real `os.close`.
- Produces: deterministic tests that identify phases from durable report state or descriptor
  identity, never an incidental call count.

- [x] Add a RED test that adds an unbound file while the target directory descriptor is still
  open, after the last complete held-tree proof and before stable append; require an overriding
  failed checkpoint with the current target excluded.
- [x] Replace the source `lstat` call-count hook with a semantic wrapper that recreates the source
  only after the provisional accepted record exists and the final absence helper returns.
- [x] Add a RED test that durably appends stable/no-intent, raises a simulated process crash, then
  mutates the accepted slot; recovery must return conflict.
- [x] Run only these tests and record failures that show the current stable record is blindly
  accepted or recovery returns `no_intent` without proof.

### Task 2: Reproduce terminal recovery omissions

**Files:**
- Modify: `tests/test_cleanup.py`

**Interfaces:**
- Consumes: `assess_cleanup_recovery(...)` and a real completed temporary quarantine.
- Produces: recovery tests for accepted-root exceptions and all non-target/slot invariants.

- [x] Add a RED test that moves `FinalDelivery_2026-07-16` after a complete cleanup and requires
  terminal recovery to reject the missing non-target root.
- [x] Add a RED test that adds a file to an accepted terminal slot and requires `conflict`.
- [x] Retain the successful complete-recovery test proving the accepted exact root target
  `posters1` may be absent when its slot proof is exact.
- [x] Run the terminal recovery selection and confirm both new tests fail for the intended blind
  no-intent branch.

### Task 3: Implement report v3 and two-phase stable acceptance

**Files:**
- Modify: `src/hk_movie_rag/cleanup.py`
- Modify: `tests/test_cleanup.py`

**Interfaces:**
- Produces: `CleanupReport.quarantine_root_device`,
  `CleanupReport.quarantine_root_inode`, `CleanupReport.source_root_identities`, and
  `_verify_quarantine_acceptance_state(...)`.

- [x] Change the exact report schema to `cleanup-report/v3`; prepared records use null root
  identity, while every post-reservation record persists the exact reserved device/inode.
- [x] After stable/no-intent append, call `_verify_quarantine_acceptance_state(...)` immediately
  to recheck root identity, source absence, and the complete slot identity/hash proof.
- [x] If post-stable proof fails, append `failed` with only prior accepted targets and the current
  intent; preserve both namespaces and all proof evidence.
- [x] Remove any docstring or prose claim that directory handles block child additions.
- [x] Run the stable schedule tests to GREEN.

### Task 4: Re-prove all no-intent accepted targets

**Files:**
- Modify: `src/hk_movie_rag/cleanup.py`
- Modify: `tests/test_cleanup.py`

**Interfaces:**
- Produces: `_assess_accepted_quarantines(...)` and terminal source-root validation based on exact
  accepted root targets.

- [x] Parse and validate unique `quarantined_targets` against the embedded target table and
  allowlist; validate persisted quarantine-root identity before any accepted-slot proof.
- [x] Require every configured source root to remain an existing safe directory unless that exact
  one-component root is itself in `quarantined_targets`; reject physical identity replacement.
- [x] For every accepted target, require its source namespace absent and its exact bound slot to
  pass complete identity/hash proof; return conflict on namespace or slot drift.
- [x] Preserve active-intent recovery behavior while requiring its intent root identity to equal
  the report root identity.
- [x] Run all recovery and cleanup tests to GREEN.

### Task 5: Documentation, verification, and commit

**Files:**
- Modify: `README.md`
- Modify: `.superpowers/sdd/2026-07-27-rag-data-foundation-phase1/task-10-report.md`
- Modify: `docs/superpowers/plans/2026-07-29-task-10-acceptance-recovery-fix.md`

**Interfaces:**
- Produces: accurate two-phase, report-v3, terminal-reproof, and point-in-time operator contract.

- [x] Document stable append followed by post-stable validation and overriding failure.
- [x] State that open directory descriptors do not block child additions and acceptance is only
  the post-stable validation instant; later drift needs explicit recovery/post-cleanup proof.
- [x] Run `uv run pytest tests/test_cleanup.py -q` and `uv run pytest -q`.
- [x] Run `uv run ruff check .`, focused mypy, `uv lock --check`, forbidden-surface scan, and
  `git diff --check`.
- [x] Record exact RED/GREEN evidence, review staged scope, commit once, and confirm clean status.

## Verification Evidence

- Reviewer schedules RED: `5 failed, 88 deselected`; GREEN: `5 passed, 88 deselected`.
- Source-root identity swap RED: `1 failed, 93 deselected`.
- Recovery: `17 passed, 77 deselected`.
- Cleanup: `93 passed, 1 skipped in 42.01s`.
- Full suite: `639 passed, 5 skipped in 593.44s (0:09:53)`.
- Ruff, mypy, lock, diff, and forbidden-surface checks: pass.
- No live plan, quarantine, GCP, archive, or raw-data operation was run.
