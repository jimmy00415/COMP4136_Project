# Task 10 Acceptance and Recovery Fix Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development
> (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use
> checkbox (`- [ ]`) syntax for tracking.

**Goal:** Close the final quarantine-acceptance race and let journal recovery reason about an
intentionally absent target source root without weakening workspace, plan, report, or external
quarantine validation.

**Architecture:** Acceptance becomes a provisional journal commit followed immediately by
post-append verification. Identity-bound descriptors remain open from the final proof through
the provisional append and post-append proof, without claiming that an open directory descriptor
prevents child additions; a gap
mutation produces a later overriding failed checkpoint whose accepted-target list excludes the
current target. Recovery reads the exact report without first requiring every source root to
exist, then validates every non-target source root while allowing only the active target root
itself to be absent.

**Tech Stack:** Python 3.11, Windows identity-bound file and directory handles, append-only
JSONL checkpoints, pathlib/os identity checks, pytest, Ruff, mypy, uv.

## Global Constraints

- Strict TDD: every production change follows a deterministic failing regression test.
- Use pytest temporary workspaces only.
- Do not generate a real cleanup plan or quarantine root.
- Do not call GCP, Cloud SDK, Storage, Terraform, or touch raw workspace data.
- Do not introduce ACL/DACL mutation, a provider, rollback, overwrite, or destructive follow-up.
- Acceptance is point-in-time proof; post-acceptance external drift is detected only by a later
  explicit verification and is never described as permanently prevented.

---

### Task 1: Close both acceptance-append schedules

**Files:**
- Modify: `tests/test_cleanup.py`
- Modify: `src/hk_movie_rag/cleanup.py`

**Interfaces:**
- Produces: `_hold_quarantine_acceptance_tree(...)` and a provisional
  `cleanup-report/v2` acceptance record that retains the active intent until post-append
  verification passes.

- [x] Add a RED test that rewrites the slot after final proof and before the provisional
      acceptance append; assert the schedule ran, the last report is failed, the current target
      is absent from `quarantined_targets`, and the durable journal agrees.
- [x] Add a RED test that recreates the source after the final absence check and before the
      provisional append; assert the schedule ran and an overriding failed checkpoint follows
      the provisional record.
- [x] Hold identity-bound descriptors for every slot directory and source across final
      proof, provisional acceptance append, and post-append source/slot verification.
- [x] Append the stable accepted checkpoint only after post-append verification succeeds.
- [x] Run the two schedule tests and the existing mutation/partial-failure tests to green.

### Task 2: Decouple journal reading from target-root existence

**Files:**
- Modify: `tests/test_cleanup.py`
- Modify: `src/hk_movie_rag/cleanup.py`

**Interfaces:**
- Produces: lexical protected-root validation for report reads and
  `_validate_recovery_source_roots(..., active_target=...)`.

- [x] Add RED tests for recovery after a normal complete quarantine and for a final
      `posters1` move whose acceptance append fails.
- [x] Add RED tests proving an absent non-target source root is rejected while the exact active
      target root `posters1` may be absent.
- [x] Stop calling existence-requiring source-root validation before reading the journal.
- [x] After parsing an active intent, strictly validate all existing configured roots and reject
      any missing root except the exact root represented by that active target.
- [x] Run the focused recovery tests and the full cleanup module to green.

### Task 3: Document the acceptance boundary

**Files:**
- Modify: `README.md`
- Modify: `.superpowers/sdd/2026-07-27-rag-data-foundation-phase1/task-10-report.md`
- Modify: `docs/superpowers/plans/2026-07-29-task-10-quarantine-only-fix.md`

**Interfaces:**
- Produces: accurate point-in-time acceptance and post-acceptance drift language.

- [x] Document provisional append, immediate post-append verification, overriding failure, and
      the stable acceptance checkpoint.
- [x] State that held handles close after acceptance and do not promise permanent immutability.
- [x] Record exact RED/GREEN and final verification evidence.

### Task 4: Verify and commit

**Files:** all modified files above.

- [x] Run `uv run pytest tests/test_cleanup.py -q`.
- [x] Run `uv run pytest -q`.
- [x] Run `uv run ruff check .`.
- [x] Run `uv run --with mypy mypy src/hk_movie_rag/cleanup.py src/hk_movie_rag/cli.py`.
- [x] Run `uv lock --check`, the forbidden-surface scan, and `git diff --check`.
- [x] Review staged scope, create one commit, and confirm a clean worktree.

Round 6 supersedes the stable-checkpoint boundary above: stable/no-intent append is phase one,
post-stable proof is the point-in-time acceptance, and terminal recovery re-proves every accepted
slot. See `2026-07-29-task-10-stable-acceptance-recovery-fix.md`.
