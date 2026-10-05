# Task 10 Atomic Detach Event and Current Observation Redesign Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:executing-plans to implement this
> plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** Replace the impossible "stable quarantine acceptance" claim with an immutable historical
`detached_to_quarantine` event, while recording current filesystem drift separately and preventing
cleanup evidence from authorizing RAG ingestion or readiness.

**Architecture:** `cleanup-report/v4` records each exact no-replace atomic rename as an immutable
event bound to the approved source path, exact reserved slot, plan digest, content digest, and the
original root device/inode observed both before and immediately after the rename. Rename timing is
represented honestly as a bounded interval: a normal executor brackets the syscall; recovery after
a crash uses the durable intent time as the lower bound and recovery observation time as the upper
bound rather than inventing an exact timestamp. A separate, explicitly non-atomic
`current_observation` samples the source and slot after the event. Drift changes the overall status
to `detached_with_drift` but never rewrites the historical event. Recovery infers a missed event
from intent plus root identity/location, not from a mutable tree hash.

**Tech Stack:** Python 3.11, append-only JSONL, pathlib/os filesystem identity checks, pytest,
Ruff, mypy, uv.

## Global Constraints

- Strict TDD: each new behavioral contract must fail for the intended reason before production
  code changes.
- Use pytest temporary workspaces only; never generate or execute a live cleanup plan.
- Do not call GCP, Cloud SDK, Storage, Terraform, or touch raw movie/poster data.
- Do not delete, recycle, roll back, or add ACL/DACL/provider behavior.
- The detach event is historical syscall evidence; it must not claim the source and slot were
  atomically validated as a pair after the syscall.
- `current_observation` is timed and non-atomic. It may detect drift, but cannot invalidate or
  mutate an already-persisted detach event.
- Crash recovery may infer a detach event only when the exact original root identity is at the
  exact reserved slot and is not still present at the source path.
- Cleanup reports always state `rag_readiness: not_authorized`. A fresh post-cleanup inventory,
  release manifest, and canonical allowlist are required before any ingestion/readiness decision.
- Dirty paths and quarantine slots are never ingestion authority.

---

### Task 1: Specify report v4 and event/observation separation with RED tests

**Files:**
- Modify: `tests/test_cleanup.py`

**Interfaces:**
- Produces: `DetachEvent`, `CurrentObservation`, v4 report serialization, and readiness denial.

- [x] Add a RED test requiring `cleanup-report/v4`, immutable `detach_events`, a separately timed
  `current_observation`, and `rag_readiness == "not_authorized"`.
- [x] Require ordered observation timestamps and an explicit `atomic: false` marker.
- [x] Require the old `quarantined_targets`/stable-acceptance vocabulary to be absent from the
  serialized contract.
- [x] Run only these tests and confirm failures are missing-schema failures, not fixture errors.

### Task 2: Specify executor event truth under source and slot drift

**Files:**
- Modify: `tests/test_cleanup.py`

**Interfaces:**
- Consumes: the final detach/checkpoint boundary and the current-observation boundary.
- Produces: deterministic source-recreation and slot-content-drift schedules.

- [x] Add a RED test that mutates the exact slot after the event checkpoint and requires
  `detached_with_drift` while retaining the exact historical event.
- [x] Add a RED test that recreates the source after the event checkpoint and requires the same
  event-preserving drift result.
- [x] Assert the event binds source path, exact slot, plan/content digests, pre/post root identity,
  and ordered rename interval bounds.
- [x] Run the focused executor schedules and record the intended old-model failures.

### Task 3: Specify identity-based crash recovery with RED tests

**Files:**
- Modify: `tests/test_cleanup.py`

**Interfaces:**
- Consumes: durable intent, source root identity, exact slot root identity, and mutable contents.
- Produces: inferred detach event plus fresh current observation.

- [x] Add a RED test for crash after rename/before event checkpoint: recovery infers the event even
  when slot contents later drift, because the original root identity is at the exact slot and no
  longer at the source.
- [x] Add a RED test where a different object is recreated at the source; recovery still infers
  the event and reports current drift.
- [x] Retain fail-closed conflicts when the original root identity exists at both source and slot,
  at neither location, or at the wrong slot.
- [x] Assert recovery timing is an honest intent-to-observation interval marked as inferred, not a
  fabricated exact syscall time.
- [x] Run the recovery selection and confirm RED failures match the old hash/stability model.

### Task 4: Implement the v4 datamodel and append-only report parser

**Files:**
- Modify: `src/hk_movie_rag/cleanup.py`
- Modify: `tests/test_cleanup.py`

**Interfaces:**
- Produces: `DetachEvent`, target/root observation records, `CurrentObservation`, v4
  `CleanupReport`, and v4 journal validation.

- [x] Add frozen dataclasses and literals for event evidence origin, observation state, and
  `detached`/`detached_with_drift` statuses.
- [x] Replace `quarantined_targets` with `detach_events`; add `current_observation` and the constant
  `rag_readiness` denial.
- [x] Validate event/report paths, digests, root identities, timestamp ordering, and plan binding.
- [x] Reject duplicate or contradictory events without re-validating historical content hashes.
- [x] Make the Task 1 schema tests pass.

### Task 5: Implement event-first atomic detach and current observation

**Files:**
- Modify: `src/hk_movie_rag/cleanup.py`
- Modify: `tests/test_cleanup.py`

**Interfaces:**
- Changes: `_detach_target_to_quarantine(...) -> DetachEvent`.
- Produces: `_observe_current_cleanup_state(...) -> CurrentObservation`.

- [x] Before rename, fully re-prove the approved plan target and persist the intent.
- [x] Bracket `_atomic_rename_no_replace` with timestamps and immediately confirm the destination
  root device/inode matches the original root identity.
- [x] Persist the immutable event checkpoint immediately after the rename witness; do not require
  a sequential source/slot stability proof to make the event true.
- [x] Sample current source roots and event slots separately with start/end timestamps and classify
  any unsafe, unreadable, missing, recreated, identity, or content drift fail-closed.
- [x] Return `detached_with_drift` on observation drift while keeping all durable events intact.
- [x] Make the Task 2 executor tests pass.

### Task 6: Implement recovery inference and fresh observation

**Files:**
- Modify: `src/hk_movie_rag/cleanup.py`
- Modify: `tests/test_cleanup.py`

**Interfaces:**
- Changes: recovery result from stable acceptance to historical event plus observation.

- [x] Validate durable events as immutable plan/path/root-identity evidence without treating
  current tree hashes as historical proof.
- [x] For an active intent, classify the original root identity at source and exact slot; infer an
  event only for the unambiguous moved state.
- [x] Represent recovered timing as the durable intent lower bound and recovery observation upper
  bound with `evidence_origin: recovery_inference`.
- [x] Generate a fresh current observation for every recovered event and classify drift separately.
- [x] Keep ambiguous identity/location states as conflict and keep recovery read-only.
- [x] Make the Task 3 recovery tests pass.

### Task 7: Remove misleading stable-acceptance machinery and align existing tests

**Files:**
- Modify: `src/hk_movie_rag/cleanup.py`
- Modify: `tests/test_cleanup.py`

**Interfaces:**
- Removes: provisional/stable acceptance descriptors and proof loops.

- [x] Delete `_AcceptancePublishFailure`, held-tree acceptance descriptors, provisional/stable
  checkpoints, and their obsolete sequential proof helpers.
- [x] Rewrite existing path, root-identity, no-replace, journal-integrity, interruption, and recovery
  tests against the event/observation contract; retain their safety guarantees.
- [x] Remove tests whose only contract is the impossible atomicity of sequential final checks.
- [x] Run `pytest tests/test_cleanup.py -q` and resolve only behavior inconsistent with v4.

### Task 8: Align CLI/docs and state the RAG authority boundary

**Files:**
- Modify: `src/hk_movie_rag/cleanup.py`
- Modify: `README.md`
- Modify: relevant schema/docs if present
- Modify: `tests/test_cleanup.py`

**Interfaces:**
- Produces: user-facing status language and readiness boundary.

- [x] Replace "quarantined/stably accepted" claims with "detached" historical event language.
- [x] Document `detached_with_drift` as blocking readiness without negating the recorded rename.
- [x] State that cleanup evidence never authorizes deletion, recycling, ingestion, or RAG readiness.
- [x] State the required fresh inventory plus release manifest/canonical allowlist gate.
- [x] Assert CLI/report output contains the readiness denial and no obsolete authority claim.

### Task 9: Verify, review, and commit

**Files:**
- Modify: this plan as tasks complete

- [x] Run focused cleanup tests.
- [x] Run the full pytest suite.
- [x] Run Ruff, mypy, lock verification, forbidden-action scans, and a live-data diff check.
- [x] Inspect the final diff for dead v3/stable terminology and ensure no raw data or cloud state
  changed.
- [x] Request code review, resolve findings under TDD, rerun all gates, and commit the verified
  round 7 change.

### Task 10: Reject incomplete recovery event prefixes as terminal

**Files:**
- Modify: `src/hk_movie_rag/cleanup.py`
- Modify: `tests/test_cleanup.py`
- Modify: `README.md`
- Modify: Task 10 report

**Interfaces:**
- Changes: `CleanupRecovery.state` adds the explicit non-terminal value `partial`.

- [x] Add deterministic RED coverage for a process interruption immediately after the first
  durable event checkpoint, with no active intent and no drift.
- [x] Require an inferred active-target event to remain `partial` while its resulting event prefix
  is shorter than the complete ordered target table.
- [x] Keep partial `detached_with_drift` and `failed` journal evidence non-terminal without erasing
  their immutable events or fresh observations.
- [x] Permit `detached` and `detached_with_drift` recovery states only when the validated event
  prefix exactly covers all planned targets.
- [x] Defer `detached_with_drift` terminal publication until the release guard exits successfully,
  so a later guard-exit failure remains the last recoverable `failed` checkpoint.
- [x] Run focused/full verification, static checks, protected-data checks, independent review, and
  prepare the Round 8 fix for commit.
