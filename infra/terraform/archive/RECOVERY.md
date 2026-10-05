# Backend IAM lockout recovery

This runbook is only for the reviewed failure in which the backend bucket already has the exact required metadata, versioning, labels, uniform bucket-level access, public access prevention and soft-delete configuration, but its direct IAM policy contains only:

```text
roles/storage.objectAdmin -> user:admin@motionexp.com
```

That role can manage state objects but cannot read bucket metadata or bucket IAM. The dedicated `recover_backend_iam.ps1` tool restores a durable, bucket-scoped policy without weakening `bootstrap_backend.ps1` or changing the accepted project and organization IAM baseline.

## Recovery invariants

Every phase revalidates the local preflight, exact account/project/organization identity, billing, and the checked-in inherited-IAM acceptance contract. `Check`, `GrantTemporary`, and `RepairBucket` require the organization policy to equal its accepted snapshot and the project policy to be only one of two states:

1. the exact accepted baseline; or
2. that full baseline plus this single conditional binding:

```text
role: roles/storage.admin
member: user:admin@motionexp.com
title: hk-rag-backend-iam-recovery
description: Temporary bucket-scoped access for approved backend IAM recovery.
expression: resource.type == "storage.googleapis.com/Bucket" && resource.name == "projects/_/buckets/motionexpaiweb-880586285913-hk-movie-rag-tfstate"
```

The resource-type guard is required before the resource-name comparison. The temporary role is never placed in bucket IAM, and `roles/storage.legacyBucketOwner` is never granted at project scope.

Before any authenticated read or mutation, the tool requires an exactly parsed Google Cloud SDK version of at least `263.0.0`, which is the minimum condition-capable version. Every authenticated read pins `--account=admin@motionexp.com`; a concurrent shared-configuration change therefore cannot redirect it. A role name in the actual `_withcond_<hex>` placeholder form proves a lossy conditional-policy read and blocks all mutation. Benign `_withcond_` text in members, condition titles or descriptions is not treated as a placeholder.

Project IAM changes use the current full policy, version 3 and live etag. Bucket IAM repair uses the current bucket IAM etag. Each mutating phase performs at most one write. Propagation is handled only by bounded, read-only probes; a write is never repeated automatically. Only the exact observed permission-denied messages for `storage.buckets.get` and `storage.buckets.getIamPolicy` are treated as propagation. Generic 403, mixed errors, unknown policies and probe exhaustion fail closed.

Temporary policy files use unpredictable names, remain open with read-only sharing while `gcloud` reads them, and contain no token or credential. Cleanup is attempted in `finally`; if deletion fails, the tool emits a sanitized, bounded warning to stderr so the operator can remove the named residual file. The tool performs no raw HTTP request and never acquires or prints an OAuth token.

## Source-archive operator recovery

The same tool has two separate modes for the reviewed partial source-archive apply. They do not change backend bucket IAM and do not run Terraform. The accepted project snapshot must contain exactly this persistent binding:

```text
role: roles/storage.admin
member: user:admin@motionexp.com
title: hk-rag-archive-operator
description: Persistent bucket-scoped management access for the HK Movie RAG source archive.
expression: resource.type == "storage.googleapis.com/Bucket" && resource.name == "projects/_/buckets/motionexpaiweb-880586285913-hk-movie-rag-source-archive"
```

The pre-grant state is derived only by removing that exact binding from the accepted target; all remaining bindings, members and conditions must match. The organization policy must still equal its accepted snapshot. Start with the read-only mode:

```powershell
pwsh -NoProfile -File infra/terraform/archive/recover_backend_iam.ps1 -Mode CheckArchiveOperator
```

`requires_archive_operator_grant` is reported only when the project policy is the exact pre-grant state and an exact fresh `storage.buckets.get` denial is observed for the source-archive bucket. `archive_operator_ready` is reported only when the exact target project policy, archive metadata and authoritative writer-only direct IAM are all read back successfully. Any other project policy, condition, bucket metadata or direct IAM fails closed.

After independent authorization, the only mutating archive-operator mode is:

```powershell
pwsh -NoProfile -File infra/terraform/archive/recover_backend_iam.ps1 -Mode GrantArchiveOperator
```

Immediately before its one full-project-policy write, the mode repeats the project read and requires the exact pre-grant state with its latest etag. It writes the exact accepted version-3 target, polls only with read-only calls, then requires exact source-archive metadata and exactly two direct writer bindings: `roles/storage.objectCreator` and `roles/storage.objectViewer`, both containing only `hk-rag-archive-writer@motionexpaiweb.iam.gserviceaccount.com`. A successful write and full readback reports `archive_operator_granted`; an already-correct and fully verified state is idempotent and reports `archive_operator_ready` without mutation. On any failure, stop without retrying the IAM write or Terraform apply; preserve Audit Logs and inspect current state.

## Phased procedure

Run every command from the repository root. Review the JSON report and audit evidence after each phase before continuing. The default is read-only:

```powershell
pwsh -NoProfile -File infra/terraform/archive/recover_backend_iam.ps1 -Mode Check
```

For the accepted baseline, `Check` probes the bucket. It reports `requires_temporary_grant` only when the first bucket-describe probe returns the exact reviewed `storage.buckets.get` denial. If describe succeeds, any denial from the subsequent bucket-IAM read fails closed; an accessible durable policy reports `recovery_complete`, while an accessible lockout or unrecognized policy also fails closed. Grant the exact temporary conditional binding only for the first result:

```powershell
pwsh -NoProfile -File infra/terraform/archive/recover_backend_iam.ps1 -Mode GrantTemporary
```

Immediately before its single project-policy write, this phase repeats the bucket-describe authorization probe. Only a fresh exact `storage.buckets.get` denial authorizes the write; a newly accessible bucket, a generic 403, a mixed error or any other state change blocks mutation. The phase then waits for the exact temporary project policy and bucket access using read-only probes, and requires either the exact object-admin-only lockout policy or the already repaired durable policy. A successful unrepaired result is `temporary_granted`.

Repair the direct bucket policy:

```powershell
pwsh -NoProfile -File infra/terraform/archive/recover_backend_iam.ps1 -Mode RepairBucket
```

Before its single write, this phase requires the exact temporary project policy, exact bucket metadata, and exact object-admin-only direct policy. It sets and verifies exactly:

```text
roles/storage.legacyBucketOwner -> user:admin@motionexp.com
roles/storage.objectAdmin -> user:admin@motionexp.com
```

Revoke the temporary project binding only after the durable bucket target is verified:

```powershell
pwsh -NoProfile -File infra/terraform/archive/recover_backend_iam.ps1 -Mode RevokeTemporary
```

This cleanup phase deliberately remains reachable if unrelated project, organization or bucket-metadata drift appears after bucket repair. Its initial cleanup probe verifies the exact durable direct bucket IAM policy without using bucket metadata as a gate, so metadata drift cannot strand the temporary grant. Immediately after that durable-IAM probe, it freshly reads the complete condition-capable project policy and requires at most one copy of the exact temporary binding. Its single write uses that latest etag and removes only that binding while preserving every other binding, member, condition, the requested policy version, optional `auditConfigs` and any concurrent supported policy content; it never writes the stale accepted snapshot over concurrent changes. If removing the last condition leaves an unconditional policy, a subsequent Google Cloud read may normalize the version-3 write to version 1. Cleanup comparison treats unconditional version-1 and version-3 envelopes as the same semantic version 1 only when every binding and optional audit config also matches; any policy that still contains a condition must remain version 3. It then freshly verifies removal, project acceptance, organization acceptance, bucket metadata and durable bucket policy.

If both inherited policies and bucket metadata are again accepted, the terminal status is `recovery_complete`. If the exact temporary binding was removed but unrelated project, organization or bucket-metadata drift remains, the tool emits `temporary_removed_drift_requires_escalation` and exits nonzero. This is a successful privilege cleanup but not recovery completion: stop, preserve the audit evidence, and escalate the remaining drift for separate review. Organization or metadata drift never justifies leaving the exact temporary grant in place. Rerunning `RevokeTemporary` after full completion is read-only and verifies the same terminal state.

After recovery, run the normal bootstrap check:

```powershell
pwsh -NoProfile -File infra/terraform/archive/bootstrap_backend.ps1 -Mode Check
```

It must report `ready_existing` before any Terraform initialization, plan or apply. Do not proceed if any recovery phase fails. Inspect the current policies and Cloud Audit Logs; do not broaden the condition, add convenience members, manually retry a write, or update the acceptance snapshot merely to bypass a failure.
