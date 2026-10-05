# Archive Terraform stack

This stack defines the private HK Movie RAG source archive and its narrow writer identity. Its Terraform state is stored separately in the dedicated GCS backend:

- bucket: `motionexpaiweb-880586285913-hk-movie-rag-tfstate`
- prefix: `hk-movie-rag/archive`

The backend bucket cannot be created by the stack that depends on it. `bootstrap_backend.ps1` is therefore a separate, one-time operational boundary. It creates only this backend bucket and converges its direct IAM policy; it creates no archive resources, identities, keys, or other infrastructure.

## Bootstrap safety contract

The script defaults to `-Mode Check`, which is read-only. Before it reports readiness it verifies the local, generated, git-ignored preflight artifact; the checked-in inherited-IAM acceptance contract; and live GCP state:

- active/configured account is exactly `admin@motionexp.com`;
- project ID and number are exactly `motionexpaiweb` and `880586285913`;
- lifecycle is `ACTIVE`, billing is enabled, the project parent is exactly organization `797368190621`, and the preflight has no blockers;
- the complete live project and organization IAM policies, after order-only canonicalization and removal of live `etag` values, exactly match the accepted snapshots and SHA-256 digests in `inherited_iam_acceptance.json`;
- an existing backend bucket belongs to that project and has the exact location, access controls, recovery settings, and bootstrap labels;
- the direct bucket IAM policy is exactly the target policy below, or the exact documented new-bucket convenience policy eligible for hardening;
- no retention policy, outsider, public member, extra role, conditional binding, or extra direct-policy field exists.

Bucket metadata is read with exact `gcloud storage buckets describe ... --raw --format=json` arguments. The fail-closed validator therefore consumes the raw Storage API camelCase schema, including `projectNumber`; the standardized snake_case output returned without `--raw` is not accepted as ownership or security evidence.

The exact target direct policy is two bindings for the operator and no other binding:

```text
roles/storage.legacyBucketOwner -> user:admin@motionexp.com
roles/storage.objectAdmin -> user:admin@motionexp.com
```

`roles/storage.legacyBucketOwner` is granted directly on this bucket, never at project scope. It supplies the bucket metadata and bucket-IAM permissions needed to verify and maintain the backend without granting project-wide Storage administration. `roles/storage.objectAdmin` supplies the state-object permissions. Removing the bucket-owner binding can leave an operator able to manage objects but unable to verify or repair the bucket itself.

When the bucket is absent, explicit `-Mode Ensure` uses only `gcloud storage` commands. Creation fixes `US-CENTRAL1`, uniform bucket-level access, enforced public access prevention, and seven-day soft delete; the immediately following update enables versioning and sets exactly these provenance labels:

```text
managed-by=hk-rag-backend-bootstrap
purpose=terraform-state
```

The create command contains no ACL or predefined ACL. The script never obtains an OAuth token, accepts no API endpoint override, and performs no raw HTTP request; `gcloud` owns authentication and transport. Temporary label and IAM JSON files contain no token or secret, are created with unpredictable names, remain open with read-only sharing while `gcloud` consumes them, and are closed and removed in `finally` even when `gcloud` fails.

Cloud Storage gives a new uniform-access bucket four direct convenience bindings: viewer convenience members receive the legacy bucket-reader and object-reader roles, while editor and owner convenience members receive the legacy bucket-owner and object-owner roles. The script recognizes that exact four-binding policy only when each convenience principal uses the project ID `motionexpaiweb`; project-number forms are rejected. Recognition happens only after all metadata and labels match. In Ensure mode the script replaces that exact intermediate with the target policy using the observed IAM etag as an optimistic-concurrency precondition, then reads back and requires the exact target. Any wrong convenience principal, missing or extra binding/member, different role, condition, or outsider fails closed without IAM mutation. See the [Cloud Storage IAM role behavior](https://cloud.google.com/storage/docs/access-control/iam-roles) and [`gcloud storage buckets set-iam-policy --etag`](https://cloud.google.com/sdk/gcloud/reference/storage/buckets/set-iam-policy).

Check mode reports `would_harden_iam`, `would_recover_versioning`, or `would_recover_versioning_and_harden_iam` without mutation. Ensure mode performs versioning recovery first when required, re-reads IAM to detect races, then hardens only the exact default policy. Missing, wrong, or extra labels block every recovery path.

The inherited-IAM acceptance is a governed snapshot, not a wildcard allowance. Any binding, member, condition, version, or unexpected top-level policy field added or removed at either scope changes the canonical snapshot and blocks before any bucket read or mutation. A contract whose status is not `accepted`, whose digest is inconsistent, or whose principal inventory is not the exact union of both snapshots also blocks before the first `gcloud` call. `etag` is deliberately absent because it is volatile transport metadata, not accepted authority.

To prepare a candidate contract from separately saved, read-only `gcloud --format=json` results, use the offline helper below. It invokes no cloud command, writes no file, emits the candidate JSON only to stdout, and emits validation failures only to stderr:

```powershell
uv run python infra/terraform/archive/build_inherited_iam_acceptance.py `
  --project-description <project.json> `
  --project-policy <project-iam.json> `
  --organization-policy <org-iam.json>
```

Review the candidate and update the checked-in contract through the normal reviewed patch workflow. Do not accept a changed snapshot merely to make the bootstrap pass.

## Operator procedure

From the repository root, refresh and review the local `artifacts/gcp/preflight.json`. This generated preflight is intentionally ignored by git and must not be committed. Then run the read-only check:

```powershell
pwsh -NoProfile -File infra/terraform/archive/bootstrap_backend.ps1 -Mode Check
```

If it reports `would_create`, `would_harden_iam`, `would_recover_versioning`, or the combined recovery state, obtain independent approval before the only mutating bootstrap command:

```powershell
pwsh -NoProfile -File infra/terraform/archive/bootstrap_backend.ps1 -Mode Ensure
```

If bucket creation succeeds but a later label, versioning, IAM, or post-verification step fails, stop automation. Do not delete, recreate, or automatically adopt the partially created bucket, and do not bypass the required provenance labels. Under separate recovery authorization, manually inspect the exact bucket metadata, versioning and recovery settings, direct bucket IAM, current inherited project and organization IAM, and Cloud Audit Logs for the failed attempt. Compare all evidence with this contract, resolve the discrepancy explicitly, and only then rerun Check. A partial bucket without the exact bootstrap labels is intentionally non-adoptable by this script.

If the direct policy is the specifically observed object-admin-only lockout policy, do not weaken this bootstrap's inherited-IAM gate and do not retry `Ensure`. Use the separately reviewed, fail-closed [backend IAM recovery runbook](RECOVERY.md). Its temporary `roles/storage.admin` grant is a conditional project binding restricted to the exact backend bucket, and its final durable policy is the two-binding target above.

The source archive uses a separate authoritative `google_storage_bucket_iam_policy`. Its complete direct policy contains exactly `roles/storage.objectCreator` and `roles/storage.objectViewer`, each with only the `hk-rag-archive-writer` service account. Do not replace it with additive `google_storage_bucket_iam_member` or `google_storage_bucket_iam_binding` resources: those would preserve the four default convenience bindings described above and would violate the archive's IAM-isolation contract. Project- or organization-inherited IAM remains outside this bucket-policy resource and is governed separately.

The accepted project snapshot includes one persistent `roles/storage.admin` binding for `user:admin@motionexp.com`, conditional on both the Cloud Storage `Bucket` resource type and the exact source-archive bucket resource name. This supplies bucket metadata and bucket-IAM maintenance permissions required by Terraform, including destructive bucket-level operations such as bucket deletion. Because the condition accepts only the `Bucket` resource type, the role's object permissions do not apply to archive objects. The binding must not be broadened, copied to another principal, or added to direct bucket IAM. If a partial archive apply created the bucket and policy but Terraform could not read them back, follow the source-archive operator procedure in [RECOVERY.md](RECOVERY.md); do not retry apply first.

After the backend reports `ready_existing` or a verified mutation result, establish explicit short-lived Terraform authentication immediately before any separately approved remote initialization, plan, or apply. The preferred procedure is:

```powershell
$hadPriorTerraformToken = Test-Path Env:GOOGLE_OAUTH_ACCESS_TOKEN
$priorTerraformToken = if ($hadPriorTerraformToken) {
    $env:GOOGLE_OAUTH_ACCESS_TOKEN
} else {
    $null
}
$candidateTerraformToken = $null
$tokenOutput = $null
try {
    $tokenOutput = @(& gcloud auth print-access-token --account=admin@motionexp.com)
    if ($LASTEXITCODE -ne 0) {
        throw 'gcloud access-token acquisition failed'
    }
    $candidateTerraformToken = ($tokenOutput -join "`n").Trim()
    $tokenOutput = $null
    if ([string]::IsNullOrWhiteSpace($candidateTerraformToken)) {
        throw 'gcloud returned an empty access token'
    }
    $env:GOOGLE_OAUTH_ACCESS_TOKEN = $candidateTerraformToken
    $candidateTerraformToken = $null
    terraform -chdir=infra/terraform/archive init -reconfigure
    if ($LASTEXITCODE -ne 0) { throw 'terraform init failed' }
    # Run plan or apply here only when each operation has its own approval.
}
finally {
    $tokenOutput = $null
    $candidateTerraformToken = $null
    if ($hadPriorTerraformToken) {
        $env:GOOGLE_OAUTH_ACCESS_TOKEN = $priorTerraformToken
    }
    else {
        Remove-Item Env:GOOGLE_OAUTH_ACCESS_TOKEN -ErrorAction SilentlyContinue
    }
    $priorTerraformToken = $null
}
```

Do not print the token or place it in command arguments, files, transcripts, or logs. Application Default Credentials may be used instead only when the operator produces independently reviewed evidence that ADC resolves to `admin@motionexp.com` for `motionexpaiweb`; the active `gcloud` account alone is not proof of ADC identity.

Any plan produced before this backend configuration—including the prior local-backend `archive.tfplan`—is stale and must never be applied. Create a fresh saved plan only after successful remote initialization, review its resource actions independently, and do not apply without separate authorization.

For code-only validation that must not contact the backend, use:

```powershell
terraform -chdir=infra/terraform/archive init -backend=false
terraform -chdir=infra/terraform/archive fmt -check -recursive
terraform -chdir=infra/terraform/archive validate
terraform -chdir=infra/terraform/archive test
```

The generated `.terraform.lock.hcl` is committed and pins Google provider `7.41.0`; Terraform itself is pinned to `1.15.8` in `versions.tf`. State and plan files remain ignored.
