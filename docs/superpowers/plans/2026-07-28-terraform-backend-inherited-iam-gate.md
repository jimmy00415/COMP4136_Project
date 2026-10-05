# Terraform Backend Inherited IAM Gate Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** Make backend readiness depend on exact direct and inherited IAM evidence while preserving gcloud stderr/stdout separation and a fail-closed governed production acceptance contract.

**Architecture:** The PowerShell bootstrap launches the resolved native application, or the Cloud SDK Python entrypoint discovered from its PowerShell launcher, through `System.Diagnostics.Process` with an exact `ArgumentList`, capturing stdout and stderr independently. Before bucket access, it verifies the project parent and canonical, etag-free project and organization IAM policies against a checked-in acceptance document populated from separately reviewed read-only evidence.

**Tech Stack:** PowerShell 7, Google Cloud CLI, Python/pytest fake-gcloud integration tests, JSON/SHA-256, Terraform 1.15.8.

## Global Constraints

- Strict TDD: run each new behavioral test against the prior production implementation and observe the expected failure before editing production.
- Code, tests, and documentation only; never execute live GCP Check/Ensure, remote Terraform init, plan, apply, upload, or cleanup.
- The exact organization is `797368190621`; the exact project is `motionexpaiweb` / `880586285913`.
- Acceptance snapshots contain full direct project and organization policies, conditions included and etags excluded.
- The checked-in acceptance document becomes ready only after exact live snapshot values are supplied and reviewed by the root task.

---

### Task 1: Correct UBLA convenience identities and repository lock test

**Files:**
- Modify: `tests/test_terraform_backend_bootstrap.py`
- Modify: `infra/terraform/archive/bootstrap_backend.ps1`

**Interfaces:**
- Consumes: fixed project ID `motionexpaiweb`.
- Produces: exact default bindings using `projectOwner:motionexpaiweb`, `projectEditor:motionexpaiweb`, and `projectViewer:motionexpaiweb` only.

- [x] Write literal project-ID default-policy tests and a numeric-convenience rejection test.
- [x] Run those tests and observe the current number-based implementation fail.
- [x] Replace number-based convenience principals with project-ID principals.
- [x] Change the lock-file test to require `git ls-files --error-unmatch` success and rerun the focused tests.

### Task 2: Separate native stdout and stderr

**Files:**
- Modify: `tests/test_terraform_backend_bootstrap.py`
- Modify: `infra/terraform/archive/bootstrap_backend.ps1`

**Interfaces:**
- Consumes: `GcloudExecutable` resolving to a native application or `.ps1` launcher and a string-array argv.
- Produces: `Invoke-GcloudJson` that checks process exit status, parses stdout only, ignores clean-success stderr, and includes only bounded control-character-sanitized stderr on failure.

- [x] Add fake-gcloud cases that emit valid JSON on stdout and warnings/status on stderr for every read path and for create, update, and set-IAM success.
- [x] Run focused tests and observe merged-stream JSON failures.
- [x] Implement `System.Diagnostics.ProcessStartInfo` with `UseShellExecute=false`, redirected stdout/stderr, asynchronous reads, and exact `ArgumentList`; resolve the `.ps1` launcher to the Cloud SDK Python entrypoint without constructing a command string.
- [x] Run focused tests and require success stderr not to appear in reports or cause JSON failures.

### Task 3: Inherited IAM acceptance gate

**Files:**
- Create: `infra/terraform/archive/inherited_iam_acceptance.json`
- Modify: `infra/terraform/archive/bootstrap_backend.ps1`
- Modify: `tests/test_terraform_backend_bootstrap.py`

**Interfaces:**
- Consumes: local acceptance JSON, live `projects describe`, `projects get-iam-policy`, and `organizations get-iam-policy` JSON.
- Produces: a pre-bucket fail-closed gate over exact parent organization plus full canonical project/organization direct-policy snapshots and SHA-256 digests.

- [x] Add a complete accepted test fixture with bindings, member sets, and a condition; add tests for order-independent acceptance.
- [x] Add failing cases for pending status, digest mismatch, added/removed binding or member, changed condition, unknown fields, folder parent, and project/organization query errors; require zero bucket/mutation calls.
- [x] Run the inherited-IAM subset and observe failures because no acceptance gate exists.
- [x] Implement strict schema validation, etag stripping, policy canonicalization, digest verification, exact parent validation, and live policy comparison before bucket access.
- [x] Add the human-readable production contract, initially pending and then populated by the root task from fresh read-only evidence; verify it is tracked and contains no etag.
- [x] Run all backend tests and require both the injected fixture and populated production contract to load and match exact live-shaped policies.

### Task 4: Documentation, final gates, and commit

**Files:**
- Modify: `infra/terraform/archive/README.md`
- Modify: `.superpowers/sdd/2026-07-27-rag-data-foundation-phase1/task-8-report.md` (ignored report)

**Interfaces:**
- Produces: operator guidance for ignored local preflight, partial-create manual recovery, inherited acceptance status, and safe Terraform token restoration.

- [x] Document manual inspection of exact metadata, direct/inherited IAM, and audit logs after partial creation; forbid automatic adoption of an unlabeled bucket.
- [x] Correct the Terraform token example so acquisition occurs inside `try`, `$LASTEXITCODE` is checked before environment replacement, and any prior value is restored or the new value removed in `finally`.
- [x] Replace “checked-in preflight” with “local ignored preflight artifact” and document that inherited acceptance was populated from reviewed live evidence.
- [x] Run backend tests, full pytest, Ruff, lock check, Terraform backend-disabled init/fmt/validate/native test, PowerShell parser, diff/secret scans, and zero plan/state checks.
- [x] Review and commit only the intended code/tests/docs; update the ignored report with the exact commit and state explicitly that no cloud action occurred.
