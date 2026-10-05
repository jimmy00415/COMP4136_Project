# Task 10 Quarantine-Only Fix Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development
> (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use
> checkbox (`- [x]`) syntax for tracking.

**Goal:** Replace recycle-provider cleanup with a fail-closed, non-destructive, externally
approved quarantine transaction whose journal can resolve a crash between intent and move
checkpoint without overwriting either namespace.

**Architecture:** `cleanup-plan/v2` binds the canonical workspace and an exact absent quarantine
root outside the workspace on the same volume, including stable ancestor identities and a unique
slot for every target. `cleanup-execute` reserves its report first, durably writes a per-target
intent, atomically renames the verified target into its slot, revalidates the complete quarantined
proof, then uses a provisional append plus immediate post-append proof before the stable
acceptance checkpoint. No provider, ACL mutation, deletion, purge, or rollback is in scope.

**Tech Stack:** Python 3.11, pathlib/os atomic rename, append-only JSONL journal, pytest, Ruff,
mypy, uv.

## Global Constraints

- Use only pytest temporary workspaces; do not generate a real plan or quarantine root.
- Do not call GCP, Cloud SDK, Storage, `send2trash`, or operate on raw workspace data.
- The quarantine root is caller-supplied, absolute, external to the workspace, same-volume,
  no-follow, absent at plan/execute validation, and reserved create-only.
- A move is accepted only after the complete target identity/hash proof passes at the exact slot.
- Never overwrite or roll back a recreated source path or an occupied quarantine slot.
- Final recycle or purge requires a future, separately reviewed authorization.

---

### Task 1: Freeze the non-destructive v2 contract

**Files:**
- Modify: `tests/test_cleanup.py`
- Modify: `src/hk_movie_rag/cleanup.py`

**Interfaces:**
- Consumes: current v1 plan/report and exact archive evidence.
- Produces: provider/ACL-free execution and the mandatory `quarantine_root` plan input.

- [x] Add failing tests asserting provider/ACL symbols are absent and execution stops at a
      proof-verified quarantine slot.
- [x] Run only those tests and record the expected failures.
- [x] Remove provider/ACL execution and implement the smallest quarantine-only path.
- [x] Re-run the tests to green.

### Task 2: Bind and reserve the external quarantine root

**Files:**
- Modify: `src/hk_movie_rag/cleanup.py`
- Modify: `src/hk_movie_rag/cli.py`
- Modify: `tests/test_cleanup.py`

**Interfaces:**
- Consumes: `build_cleanup_plan(..., quarantine_root: Path)` and CLI
  `cleanup-plan --quarantine-root ABSOLUTE_PATH`.
- Produces: `cleanup-plan/v2` fields `quarantine_root`, `quarantine_ancestors`, and per-target
  `quarantine_slot`.

- [x] Add RED tests for relative/in-workspace/cross-volume/reparse/alias roots, existing leaf
      collisions, ancestor identity changes, and deterministic unique slots.
- [x] Implement canonical external-root validation and plan parsing/digest binding.
- [x] Implement create-only root reservation with before/after ancestor identity checks.
- [x] Re-run focused validation tests to green.

### Task 3: Add durable intent and crash reconciliation

**Files:**
- Modify: `src/hk_movie_rag/cleanup.py`
- Modify: `tests/test_cleanup.py`

**Interfaces:**
- Produces: `cleanup-report/v2`, `QuarantineIntent`, `quarantined_targets`, and
  `assess_cleanup_recovery(...) -> QuarantineRecovery`.

- [x] Add RED tests that inspect the intent checkpoint written before rename.
- [x] Add RED crash tests for source-only, slot-only with valid proof, both-present, neither,
      and mutated-slot states.
- [x] Publish intent before rename and acceptance after complete post-detach proof.
- [x] Implement read-only reconciliation without moving or overwriting either path.
- [x] Re-run crash/recovery tests to green.

Follow-up hardening holds the complete slot tree against write/delete through final proof,
provisional append, and immediate post-append proof. The journal reader no longer requires an
intentionally moved target root to exist before it can recover the active intent. Acceptance is
point-in-time evidence and does not claim permanent immutability after those handles close; see
`2026-07-29-task-10-acceptance-recovery-fix.md`.

### Task 4: Preserve partial-failure truth

**Files:**
- Modify: `src/hk_movie_rag/cleanup.py`
- Modify: `tests/test_cleanup.py`

**Interfaces:**
- Consumes: v2 journal and exact slots.
- Produces: reports that distinguish accepted quarantines from the current unresolved intent.

- [x] Add RED tests for multi-target partial failure, old-namespace recreation, slot mutation,
      post-move checkpoint failure, and Release-guard failure.
- [x] Stop at the first failure, preserve recreated/new content, never roll back, and report only
      proof-accepted `quarantined_targets`.
- [x] Re-run the full cleanup test module to green.

### Task 5: Remove destructive dependencies and close documentation

**Files:**
- Modify: `pyproject.toml`
- Modify: `uv.lock`
- Modify: `README.md`
- Modify: `.superpowers/sdd/2026-07-27-rag-data-foundation-phase1/task-10-report.md`
- Modify: `docs/superpowers/plans/2026-07-27-rag-data-foundation-phase1.md`

**Interfaces:**
- Produces: quarantine-only operator language and no Send2Trash dependency.

- [x] Remove Send2Trash runtime/type dependencies and regenerate the lockfile.
- [x] Replace deleted/recycled wording with quarantined wording and state the future authorization
      boundary for recycle/purge.
- [x] Run `rg` to prove provider/DACL/recycle execution surfaces are absent.

### Task 6: Verification and commit

**Files:** all modified files above.

- [x] Run `uv run pytest tests/test_cleanup.py -q`.
- [x] Run `uv run pytest -q`.
- [x] Run `uv run ruff check .`.
- [x] Run `uv run --with mypy mypy src/hk_movie_rag/cleanup.py src/hk_movie_rag/cli.py`.
- [x] Run `uv lock --check` and `git diff --check`.
- [x] Review staged scope, commit once, and confirm a clean worktree.
