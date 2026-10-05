# Terraform Backend IAM Hardening Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** Remove bearer-token handling from the backend bootstrap and converge the direct bucket IAM policy to one exact object-admin binding.

**Architecture:** All Cloud Storage operations cross the existing injected `gcloud` executable boundary; production code has no HTTP endpoint or OAuth-token interface. The script classifies direct bucket IAM as either the exact target policy, the exact documented new-bucket convenience policy, or an unrecoverable mismatch, and only the exact default policy may be replaced using its etag and a transient JSON policy file.

**Tech Stack:** PowerShell, Google Cloud CLI, Python/pytest fake-command integration tests, Terraform 1.15.8.

## Global Constraints

- Use strict TDD: every production behavior change must be preceded by a focused failing test.
- Do not contact or mutate live GCP and do not run remote backend initialization, plan, apply, upload, or cleanup.
- Preserve the stale-plan no-reuse boundary and keep all plan/state artifacts absent.
- Target direct bucket IAM is exactly `roles/storage.objectAdmin` for `user:admin@motionexp.com`.

---

### Task 1: Credential-safe gcloud boundary

**Files:**
- Modify: `tests/test_terraform_backend_bootstrap.py`
- Modify: `infra/terraform/archive/bootstrap_backend.ps1`

**Interfaces:**
- Consumes: `-GcloudExecutable` test injection and fixed account/project constants.
- Produces: bucket creation and updates expressed only as exact `gcloud storage` argv.

- [ ] **Step 1: Write the failing integration test**

Replace the loopback HTTP fake with a fake `gcloud` create/update implementation. Assert an Ensure run creates through `gcloud storage buckets create`, never calls `auth print-access-token`, and exposes no storage endpoint parameter.

- [ ] **Step 2: Run the focused test to verify RED**

Run: `uv run pytest tests/test_terraform_backend_bootstrap.py -q`

Expected: failure because the production script still requires `StorageApiBaseUri` and calls `Invoke-RestMethod` with a bearer token.

- [ ] **Step 3: Implement the minimal credential-boundary fix**

Remove `StorageApiBaseUri`, `Invoke-GcloudText`, token acquisition, and `Invoke-RestMethod`. Create the bucket with exact location, uniform access, public-access prevention, and soft-delete flags; set exact labels and versioning through `gcloud storage buckets update`.

- [ ] **Step 4: Run the focused tests to verify GREEN**

Run: `uv run pytest tests/test_terraform_backend_bootstrap.py -q`

Expected: credential-boundary and creation tests pass.

### Task 2: Exact direct IAM convergence

**Files:**
- Modify: `tests/test_terraform_backend_bootstrap.py`
- Modify: `infra/terraform/archive/bootstrap_backend.ps1`

**Interfaces:**
- Consumes: direct bucket policy returned by `gcloud storage buckets get-iam-policy`.
- Produces: deterministic status matrix for exact-target/default IAM and versioning state; etag-guarded policy replacement using a transient JSON file.

- [ ] **Step 1: Write failing IAM classification tests**

Add literal fixtures for the exact target and four-binding uniform-access default policy. Test target idempotence; Check-mode `would_harden_iam`; combined `would_recover_versioning_and_harden_iam`; and fail-closed outsider, arbitrary role, missing/extra binding/member, condition, and wrong-project convenience policies.

- [ ] **Step 2: Run focused IAM tests to verify RED**

Run: `uv run pytest tests/test_terraform_backend_bootstrap.py -q`

Expected: failures because the current code accepts every non-public policy and cannot harden IAM.

- [ ] **Step 3: Implement exact IAM classification and recovery**

Validate metadata and labels first. Accept only exact target or exact default bindings, allowing only required policy envelope fields. In Ensure, recover versioning first when needed, then replace only exact-default IAM using `gcloud storage buckets set-iam-policy`, the observed etag, and a create-new temporary policy file removed in `finally`; post-read and require the exact target.

- [ ] **Step 4: Run focused tests to verify GREEN**

Run: `uv run pytest tests/test_terraform_backend_bootstrap.py -q`

Expected: all credential, IAM, metadata, preflight, and partial-state tests pass.

### Task 3: Operator documentation and final gates

**Files:**
- Modify: `infra/terraform/archive/README.md`
- Modify: `.superpowers/sdd/2026-07-27-rag-data-foundation-phase1/task-8-report.md` (ignored report)

**Interfaces:**
- Produces: an explicit short-lived Terraform authentication procedure and current validation evidence.

- [ ] **Step 1: Document the authentication boundary**

Document setting `GOOGLE_OAUTH_ACCESS_TOKEN` from `gcloud auth print-access-token --account=admin@motionexp.com` immediately before approved remote Terraform commands and clearing it in `finally`, plus the alternative requirement to prove ADC identity. State that tokens must not enter arguments, files, or logs.

- [ ] **Step 2: Run all code-only gates**

Run focused pytest, Terraform `init -backend=false`/fmt/validate/native test, PowerShell parser, Ruff, full pytest, diff/secret scans, and plan/state absence checks.

- [ ] **Step 3: Review and commit**

Review the exact diff, commit only code/tests/docs, record the resulting SHA in the ignored report, and confirm no live cloud or Terraform mutation occurred.
