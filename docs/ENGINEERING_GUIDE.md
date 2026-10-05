# Detailed engineering guide

The original engineering procedures below are retained for reference. Run commands from the repository root; historical release examples are not the current evaluated configuration. Use the [course README](../README.md) for the final report and setup overview.

This is the author's native course project. **Start with [START_HERE.md](../START_HERE.md)** for setup, source tests, course evaluation scope and source provenance.

The [60-turn challenge and BM25 comparison](../course/CHALLENGE_RESULTS.md) includes real outputs, explicit AI review and disclosed protocol failures. It preserves the original 30-case diagnostic study and does not claim overall method superiority.

This publication contains core source code and test fixtures. External movie data, raw PDFs/posters and operational receipts are not included. The retired memory experiment is not part of this repository. The [final report PDF](../report/COMP4136_HK_Movie_RAG_Report.pdf) is now included; presentation creation remains deferred at the author's request.

The original engineering documentation follows. Its full data-build procedure is separate from running the repository's offline source tests.

---

# Hong Kong Movie minimum verifiable RAG demo

This branch adds one public technical-demo Vertex AI chatbot vertical slice on top of the governed
Release v1.2 data foundation. It is intentionally separate from any pre-existing movie product:
one deterministic RAG bundle, Cloud SQL PostgreSQL/pgvector, ingestion and poster-verification
Cloud Run Jobs, and one Cloud Run API/UI service.

The slice proves these exact boundaries:

- The active R3 child release has 4,659 movies with deterministic metadata passages and
  768-dimensional embeddings.
- The unchanged 24-movie pilot remains a validation cohort, not a query restriction.
- Exactly five governed PDFs add 21 non-empty page passages: `醉拳` (`1978_ZQ_001`, three
  pages), `最佳拍檔` (`1982_ZJPD_001`, three pages), `殭屍先生` (`1985_JSXS_001`, four
  pages), `富貴逼人` (`1987_FGBR_001`, six pages), and `少林足球` (`2001_SLZQ_001`, five
  pages). Every PDF-supported answer must return a page citation.
- All 4,659 RAG poster-state rows are derived as `is_primary=true`; the original source value is
  retained as `source_is_primary` for audit. Exactly 4,546 canonical WebPs are available and 113
  rows deliberately have no object.
- The operator has explicitly authorized public technical-demo access, including available poster
  responses. This runtime authorization is not legal clearance and does not mutate the immutable
  source-rights or release provenance. Objects remain in private GCS and are returned only through
  the same-origin integrity-checked proxy; no raw GCS URL is exposed.
- Generation uses `gemini-3.5-flash-lite`; embeddings use `gemini-embedding-2` at 768 dimensions.

This is a minimum verifiable product slice, not a production-readiness, public-CDN, end-user auth,
or 768-vs-1,536 evaluation claim.

## Build and verify the R2 RAG bundle

Code, governed Release data, and raw PDF inputs are separate authorities. Run the code from a clean
tracked worktree. `SourceRoot` points to the existing governed Release v1.2 data and 4,545 derived
WebPs. `DocumentSourceRoot` is an operator-controlled directory outside the code worktree containing
only the three exact PDF filenames declared by `config/rag_demo_r2.yaml`; raw PDFs are not copied
into Git or the container image. The builder reads only these explicit paths and does not recursively
discover additional PDFs or movies.

```powershell
$CodeWorktree = 'D:\VS_PROJECT\HK_Movie_KnowledgeGraph\RAG'
$SourceRoot = 'D:\VS_PROJECT\HK_Movie_KnowledgeGraph\RAG'
$DocumentSourceRoot = 'D:\VS_PROJECT\HK_Movie_KnowledgeGraph\RAG_INPUTS\v1.2-demo-r2'
$RagConfigPath = Join-Path $CodeWorktree 'config\rag_demo_r2.yaml'
$BundleDirectory = Join-Path $CodeWorktree 'artifacts\rag-demo\v1.2-demo-r2'

if (-not (Test-Path -LiteralPath $DocumentSourceRoot -PathType Container)) {
  throw 'prepare the external R2 document source directory first'
}
Set-Location $CodeWorktree
uv sync --frozen
uv run hk-movie-rag build-rag-bundle `
  --source-root $SourceRoot `
  --document-source-root $DocumentSourceRoot `
  --config $RagConfigPath `
  --output $BundleDirectory
uv run hk-movie-rag verify-rag-bundle `
  (Join-Path $BundleDirectory 'rag_release_manifest.json')
```

The external directory is deliberately not created or populated by Git. Before running the command,
stage the exact config-bound files there without renaming them: `S 級港⽚-醉拳.pdf`,
`S 級港⽚-最佳拍檔.pdf`, and `S級港片-富貴逼人.pdf`. The checked-in SHA-256 and page-count
bindings, not the directory contents alone, decide whether each file is accepted.

Expected counts are 4,658 movies, 4,658 metadata passages, 4,658 primary poster-state rows,
4,545 private poster objects, three documents, 12 PDF passages, and 4,670 total embeddings. Build
fails closed if any declared PDF is missing, renamed, symlinked, hash-mismatched, or has a non-empty
page range different from its configured `page_count`. Verification re-hashes the manifest and
bundle and emits the exact release, model, profile, policy, poster-authority, count, document, and
canonical `rag/v1.2-demo-r2/` GCS-prefix authority consumed by deployment.

### Historical R1 record

The original `config/rag_demo.yaml` remains the historical R1 definition: database release ID
`v1.2-demo`, two PDFs, six PDF passages, and 4,664 embeddings. Its already-upgraded immutable cloud
bundle is addressed at `rag/v1.2-demo-mvp1/`; this different object prefix does not rename the
database release. R2 is a new child release and never rewrites either the R1 config or R1 objects.

The conversational layer supports structured genre, decade/year, and S/A/B tier facets. A request
may return at most eight recommendation cards. Each browser tab supplies at most the last four
conversation exchanges, bounded to 12,000 total history characters; history is not a server-side
profile or durable user memory. Cards expose a same-origin poster path only when the governed row
is approved, otherwise `poster_url` is `null`.

Natural one-person movie requests are resolved from the active Release credit lexicon before
retrieval. A complete current-person request resets an earlier person context, while a true
continuation may inherit it. Continuation vocabulary such as `还有吗` does not override a complete
current person: `周星驰的动作喜剧还有吗` resets an earlier person and filter state. Adjacent genres
are conjunctive: `周星驰的动作喜剧` requires both `動作` and `喜劇`, not either genre alone. Unknown,
multiple, or exclusionary person requests fail closed; no name is a runtime allowlist.

### Query relevance and grounded-answer contract

Before embedding, a deterministic positive domain gate accepts only a release movie title/person,
an explicit movie signal, a governed movie facet, or a bounded successful movie continuation.
Clear non-movie requests return the fixed domain message with no embedding, semantic/recommendation
retrieval, Vertex generation, citation, or card. Similarity is not the domain classifier: after
that gate, generic vector rows
must carry finite pgvector cosine distance in `[0, 2]`, and rows above `0.36` are discarded. Exact
title/history targets and structured recommendation/person/deep searches bypass only the cutoff;
their distances remain mandatory and validated.

Release-title scope is resolved before topic classification. Besides IDs and `《》` delimiters,
the resolver accepts an exact active-Release title behind a bounded conversational lead-in such as
`讲一下`, `聊聊`, `我想了解`, or `请分析一下`, while retaining strict longest-title, short-title,
token-boundary, and explicit non-movie residual checks. Topic vocabulary such as `喜剧创作` is not
used as an entity-recognition whitelist. This lets `讲一下富贵逼人的喜剧创作` enter target-only
PDF retrieval without allowing `英雄` to escape from unrelated phrases such as `超级英雄` or
`英雄联盟`. Topic-independent lead-ins require at least four CJK identity characters (or eight
alphanumeric characters for non-CJK titles); shorter/common titles still require delimiters or a
bounded request plus explicit movie-specific context such as director, plot, visual style, action
design, or comedy creation. Explicit book predicates (`作者`, `出版`, `書評`, `值得讀`) remain
out of domain, while compounds such as `書寫`, `書信`, and `書架` do not become false book matches.
Python and SQL share one audited orthographic-equivalence map covering all
4,658 active titles. If normalization merges genuinely different same-spelling titles, an exact raw
Traditional title wins; an unresolved alias or duplicate title returns the exact candidate movie IDs
instead of silently choosing a film.

Recommendation generation returns ordered `{citation_id, reason}` items. The server, not the
model, renders each line's title and final citation marker from the same ordered evidence tuple used
for Citation and MovieCard objects. Missing, duplicate, reordered, foreign, or identity-bearing
items fail closed. Vertex generation has a 20-second request timeout and three total SDK attempts
only for HTTP `408`, `429`, `500`, `502`, `503`, and `504`; permanent and exhausted failures expose
one fixed redacted error. Runtime startup also requires `RAG_RELEVANCE_POLICY_SHA256` to equal the
packaged release/model/dimension/metric/cutoff/golden policy digest; `/api/config` exposes that
non-secret digest for verification.

The same identity invariant applies to generic grounded answers: movie cards and poster-authority
lookups are derived only from the validated citation selection, never from uncited vector
neighbours. The exact returned movie IDs are therefore also the only IDs eligible to become
pronoun/follow-up anchors in the browser's next history exchange.

Poster serving has a separate machine-readable authority at
`src/hk_movie_rag/authorities/poster_serving_authority_v2.json`. R2 binds the SHA-256 of its exact
canonical artifact bytes, including the required terminating newline,
as `poster_authority_sha256` in `config/rag_demo_r2.yaml`; verification carries that value into the
manifest, and deployment derives `RAG_POSTER_AUTHORITY_SHA256` only from the verified contract.
Startup must match the packaged authority, and `/api/config` exposes the same non-secret digest.
The immutable authority artifact records `authenticated_same_origin_private_proxy` delivery for the
restricted source release. The currently approved public technical-demo runtime deliberately removes
that access gate while retaining the same-origin proxy and byte/size/MIME/allowlist checks. Its
`source_rights_status` remains `unknown`; operator confirmation is not a legal-rights determination.

Run the immutable 30-calibration/10-holdout domain evaluation offline:

```powershell
uv run python -m hk_movie_rag.relevance_eval `
  --golden evals/general_relevance_golden.jsonl
```

An explicitly requested diagnostic run may add `--live --observations <canonical-jsonl>`. Those
pre-captured distances are reported by case ID only and do not imply a deployment, live traffic,
or production-readiness claim; observed OOD distances are pre-gate diagnostics, while the runtime
gate still rejects OOD questions before embedding.

## Reproducible GCP deployment

Run a redacted authentication preflight without printing account names or token material:

```powershell
$activeAccounts = @(gcloud auth list --filter=status:ACTIVE --format='value(account)')
if ($activeAccounts.Count -ne 1) { throw 'exactly one active gcloud account is required' }
$configuredProject = (gcloud config get-value project 2>$null).Trim()
if ($configuredProject -cne 'motionexpaiweb') { throw 'gcloud project is not motionexpaiweb' }
$adcProbe = gcloud auth application-default print-access-token 2>$null
if ([string]::IsNullOrWhiteSpace($adcProbe)) { throw 'ADC token probe failed' }
Remove-Variable activeAccounts, adcProbe
gcloud services list --enabled --project=motionexpaiweb --limit=1 `
  --format='value(config.name)' | Out-Null
```

Do not paste the captured account or ADC token into reports. Both GCP scripts pass project and
region explicitly and remove ambient `GOOGLE_API_KEY`, `GOOGLE_CLOUD_LOCATION`, and
`GOOGLE_CLOUD_PROJECT` only from gcloud child processes, preventing local shell overrides from
silently retargeting a command. Test-only `RAG_DEMO_*` and pytest controls remain available to the
offline fake-cloud gates.

First inspect deterministic, non-mutating plans:

```powershell
pwsh scripts/gcp/provision-demo.ps1 `
  -ProjectId motionexpaiweb -Region us-central1 -PlanOnly
pwsh scripts/gcp/deploy-demo.ps1 `
  -ProjectId motionexpaiweb -Region us-central1 `
  -SourceRoot $SourceRoot `
  -RagConfigPath $RagConfigPath `
  -DocumentSourceRoot $DocumentSourceRoot `
  -ReuseFromReleaseId v1.2-demo `
  -BundleDirectory $BundleDirectory `
  -PlanOnly
```

The R2 procedure assumes the separately provisioned `motionexpaiweb` resources pass the deployment
preflight. Keep provisioning plan-only for audit unless a separately reviewed change authorizes
resource mutation. Apply deployment only from the clean tracked `$CodeWorktree`. The script itself
runs public candidate and stable acceptance smokes and deliberately removes any ambient legacy
`RAG_DEMO_ACCESS_KEY` from child processes:

```powershell
$BundleDirectory = 'D:\VS_PROJECT\HK_Movie_KnowledgeGraph\RAG\artifacts\rag-demo\v1.2-demo-r3-deploy'
pwsh scripts/gcp/deploy-demo.ps1 `
  -ProjectId motionexpaiweb -Region us-central1 `
  -SourceRoot $SourceRoot `
  -RagConfigPath $RagConfigPath `
  -DocumentSourceRoot $DocumentSourceRoot `
  -ReuseFromReleaseId v1.2-demo `
  -BundleDirectory $BundleDirectory `
  -Apply
```

The deployment preserves the existing database secret and its active numeric version. It does not
bind the legacy demo access-code secret, generate or replace secret values, or hardcode a secret in this
runbook. For a separately authorized fresh environment, the provisioning script is create-if-absent
and fails on incompatible drift; it is not part of this existing-environment deployment procedure.

Deployment rebuilds and verifies the bundle, treats verifier JSON as the sole release authority,
hashes the exact poster allowlist, and publishes the manifest and JSONL create-only beneath the
immutable child prefix `rag/v1.2-demo-r2/`. An existing object may be accepted only when its exact
SHA-256, size, content type, cache control, and custom metadata match; it is never overwritten.
The image context is extracted solely from committed `git archive HEAD` bytes into a validated
temporary directory, so raw PDFs and untracked or ignored workspace files cannot enter Cloud Build.

Before R2 can reuse embeddings, deployment first verifies the exact immutable legacy manifest
`gs://motionexpaiweb-880586285913-hk-movie-rag-release/rag/v1.2-demo-mvp1/rag_release_manifest.json`
with remote SHA-256
`18be34d0c36d0d010fceb1b3ff4c2dea734479f899f8ef28ae7541d8f43fc9a0`. That manifest describes
the database release ID `v1.2-demo`. The ingestion Job runs it with
`--require-existing-active --require-embedded 0 --require-skipped 4664`, which permits the one-time
legacy identity claim only after the complete old bundle is verified. A null or incompatible legacy
row cannot become reuse authority by release name alone.

The first R2 ingestion then requires the final state `source_eligible_total=4658` and
`non_reusable_embedded_total=12`: all metadata embeddings are eligible for exact-profile reuse and
the 12 new PDF page passages are embedded. This is a final-state resume gate, so a safely resumed
partial run may report different per-attempt work without failing. Deployment immediately reruns the
same R2 manifest as an already-ACTIVE release and requires the strict no-op result
`embedded=0, skipped=4670`. It then runs `hk-movie-rag-poster-verify`: all 4,545 approved WebPs are
fully streamed and checked against their canonical DB key, SHA-256, byte length, and MIME type,
while all 113 unavailable rows must have no canonical object. The verifier uses at most 16 workers,
closes every stream, and emits one redacted JSON report. Service deployment is blocked unless the
legacy claim, both R2 ingestion executions, and poster-verification Job all succeed. The script does
not delete resources, weaken bucket IAM, or make GCS public.

The service rollout is candidate acceptance, not an immediate traffic switch. Cloud Run creates
`a-<sha8>-<64-bit nonce>` on the new revision with `--no-traffic`, verifies the distinct candidate URL,
and runs the public smoke against that URL. Only an exact Cloud Run v2 state and ETag may be
compare-and-swap promoted to 100% while retaining the tag. The stable service URL must then pass the
same public revision/image/manifest smoke before a second ETag-conditional update removes
the candidate tag. On any failure, rollback or tag cleanup is attempted only against the exact state
and ETag owned by this deployment; concurrent drift is never overwritten and acceptance fails.

The exact resources are:

- Project/region: `motionexpaiweb` / `us-central1` (Vertex uses the global endpoint).
- Cloud SQL: `hk-movie-rag-pg`, PostgreSQL 16 Enterprise edition, `db-g1-small`, 10 GB SSD, zonal;
  database/user `hk_movie_rag` / `hk_rag_app`.
- Private bucket: `motionexpaiweb-880586285913-hk-movie-rag-release`.
- Artifact Registry repository: `hk-movie-rag`.
- Runtime identity: `hk-rag-runtime@motionexpaiweb.iam.gserviceaccount.com`.
- Bound runtime secret: `hk-rag-db-password`. The legacy `hk-rag-demo-key` is not bound.
- Cloud Run Jobs/service: `hk-movie-rag-ingest`, `hk-movie-rag-poster-verify`,
  `hk-movie-rag-demo`.

The fleet verifier can also be executed directly in the configured runtime environment. The remote
manifest, not a free-form release ID, supplies its complete authority:

```powershell
uv run hk-movie-rag verify-poster-fleet `
  --manifest 'gs://motionexpaiweb-880586285913-hk-movie-rag-release/rag/v1.2-demo-r2/rag_release_manifest.json' `
  --bucket motionexpaiweb-880586285913-hk-movie-rag-release `
  --workers 16
```

The service link is reachable because deployment disables the Cloud Run invoker IAM check and then
verifies the resulting service annotation. This avoids a public IAM grant that the project's domain
restriction blocks. Chat, config, and approved poster routes are intentionally public for this
technical demo; there is no login, session cookie, or end-user authentication/authorization.

## Live acceptance

`deploy-demo.ps1 -Apply` already runs the fail-closed smoke first against the zero-traffic candidate
URL and then against the stable URL after conditional promotion. An operator may independently rerun
the same public smoke using identity values from the redacted deployment-result JSON:

```powershell
$BundleDirectory = 'D:\VS_PROJECT\HK_Movie_KnowledgeGraph\RAG\artifacts\rag-demo\v1.2-demo-r3-deploy'
uv run python scripts/gcp/live-smoke.py `
  --base-url 'https://SERVICE-URL.run.app' `
  --expected-revision 'hk-movie-rag-demo-00000-xxx' `
  --expected-image-digest 'sha256:DEPLOYMENT-RESULT-DIGEST' `
  --manifest (Join-Path $BundleDirectory 'rag_release_manifest.json') `
  --public-access `
  --output 'artifacts/rag-demo/live-smoke.json'
```

Take both expected identity values from the same redacted deployment-result JSON. The smoke checks
the public config before and after the query suite, so a stable URL serving another revision or
image fails acceptance. The smoke reports redacted JSON and retains public-health, legacy-session
removal, active counts/models, release-manifest identity, metadata, all five page-cited PDFs, known
poster bytes, and public config/chat/poster checks. It also asks the exact unbracketed production
regression `讲一下富贵逼人的喜剧创作` and requires only `1987_FGBR_001`, the governed PDF page-3
citation, substantive comedy-mechanism terms, and the verified poster response. It
also gates a natural short-title director question, a context-sensitive HK-variant title, deterministic
movie-ID-only clarification for the Simplified `《证人》` alias, and explicit short-title/book OOD
controls. It requires exactly three comedy cards, a history follow-up that inherits comedy while excluding
prior IDs, and the exact mixed-person request `想看周星驰和成龍電影，推薦幾部`, which must return
`目前一次只支援一位正向人物；請只指定一位導演或演員後再試。` without citations or cards. It
also gates both the standalone `周星驰的动作喜剧` query and the `王家卫的电影` then history-bearing
`周星驰的动作喜剧还有吗` reset sequence. Each action-comedy response must contain five ordered
metadata citations/cards, canonical `周星馳` credit, and both `動作` and `喜劇` on every card; the
history-bearing result must not reuse any prior `王家衛` movie ID. Every available returned poster is
authenticated and checked at the same origin. It also requires S-tier-only cards that exclude known
B-tier `2016_SFB_001`, and valid poster-path/null behavior. The URL is deliverable only after the
legacy claim, R2 initial and ACTIVE no-op ingestion,
poster verification, candidate/stable smokes, conditional promotion, and tag cleanup all pass.

This remains a demo-to-MVP acceptance slice, not production readiness: access is intentionally public,
tab history is bounded and non-durable, there is no per-user authorization, public poster
CDN, online source refresh, production SLO/on-call contract, or evaluation proving model/embedding
optimality. README text, local builds, local tests, and plan output do not prove that any GCS object,
database row, Cloud Run Job, revision, traffic update, or public link exists. Only a successful live
apply plus its redacted Job, revision, digest, candidate/stable smoke, ETag-CAS, and final service
observations can support that claim.

## Add another governed PDF

Schema 1.2 already supports one or more explicit deep documents, so another ordinary text PDF does
not require a new parser or database table. It does require a new immutable child release and new
acceptance bindings; never edit a published manifest, JSONL, or immutable release prefix.

1. Put the exact PDF bytes directly under a new operator-controlled `DocumentSourceRoot`, then run
   the read-only binding preflight. The movie and document identities remain explicit operator
   inputs; rights and quality decisions have no defaults and therefore cannot be silently inferred:

   ```powershell
   uv run hk-movie-rag preflight-pdf-binding `
     --source-root $SourceRoot `
     --document-source-root $DocumentSourceRoot `
     --config config/rag_demo_r2.yaml `
     --movie-id YYYY_CODE_001 `
     --document-id unique-fourth-analysis-v1 `
     --source-filename 'EXACT-FOURTH-FILENAME.pdf' `
     --rights-status restricted `
     --quality-status manual_approved
   ```

   The canonical JSON result checks exact parent movie membership, direct regular `.pdf` input,
   duplicate document/filename/content identities, streamed SHA-256 before and after extraction,
   the configured extraction profile, and a contiguous non-empty page range beginning at page 1.
   It emits the config-ready `binding` plus the next document, PDF-passage, and embedding counts;
   it does not modify the governed config, PDF, or Release artifacts and includes no local path or
   extracted text. The existing Release verifier may create/update local `.locks` coordination
   metadata. `valid:true` proves only that this local binding preflight passed; it does not prove the
   operator's rights/quality assertions, authorize a child release, or satisfy cloud deployment and
   live-smoke acceptance.

2. Copy `config/rag_demo_r2.yaml` to a new tracked config such as `config/rag_demo_r3.yaml`, assign a
   never-published child `rag_release_id`, and append the preflight result's exact `binding` object.
   Do not edit the generated SHA or page count, and do not reuse a prior release ID:

   ```yaml
   - movie_id: YYYY_CODE_001
     document_id: unique-fourth-analysis-v1
     source_filename: EXACT-FOURTH-FILENAME.pdf
     source_sha256: <preflight-source-sha256>
     rights_status: restricted
     quality_status: manual_approved
     page_count: <preflight-page-count>
   ```

3. Build into a new worktree-local output directory and run `verify-rag-bundle` exactly as above.
   With no other content change, the expected totals become four documents, `12 + P` PDF passages,
   and `4670 + P` embeddings. Inspect the verifier JSON; do not calculate a cloud prefix from an
   unverified config value.

4. Before cloud apply, extend the deployment's exact contract and regression tests for the new
   release ID/prefix, four document bindings, release-bound relevance/poster authority digests,
   totals, and smoke cases. If active R2 is the reuse source and the embedding model, dimension,
   title-text profile, and existing passage bytes are unchanged, the final-state gates should be
   `source_eligible_total=4670` and `non_reusable_embedded_total=P`; the ACTIVE no-op rerun should be
   `embedded=0, skipped=4670+P`. The bundle verifier supplies observed evidence, not its own release
   authority. Freeze the expected target/prefix, parent and poster identities, exact documents,
   policy/authority digests, models/counts, reuse-source object identity, ingestion deltas, and deep
   smoke cases in a separate tracked, operator-reviewed acceptance oracle; both deploy and live
   smoke must compare their observations to it.

5. Publish only under the verifier's new create-only child prefix, run poster fleet verification,
   and use the same tagged-candidate public smoke, ETag-CAS promotion, stable smoke, and
   owned rollback/tag-cleanup sequence. A fourth PDF is complete only when its page-cited questions
   are included in the live acceptance suite and the redacted live evidence passes.

## Cost and cleanup boundary

Cloud SQL is the main persistent cost even when Cloud Run scales to zero. GCS, Artifact Registry,
Vertex embedding/generation, Cloud Build, and Cloud Run add storage or usage-based charges.
Any separately authorized future provisioning requires a billing review. Cloud SQL storage
auto-resize is disabled and deletion protection is enabled. Cleanup requires a separately reviewed
action that
first disables deletion protection for the exact instance; neither provisioning nor deployment
performs that destructive transition.

No automated cleanup is included: deletion could remove the only demo database or verified release
objects. When the demo is no longer needed, separately review the exact Cloud Run service/Job,
Cloud SQL instance, Artifact Registry repository, release bucket, runtime service account, and two
secrets before deleting anything. The source archive and Terraform-state buckets are never demo
cleanup targets.

## Governed Release v1.2 foundation

The only authorized base-data entrypoint is `data/release/v1.2/release_manifest.json`. Recursive
discovery of raw folders, staging submissions, Quarantine rows, or issue-ledger rows is not a
production input.

## Release v1.2 contract

- 4,658 formal Release movies and 4,658 unique `影片唯一ID` values.
- Tier counts: S 50, A 313, and B 4,295.
- 24 pilot movies and 1,008 provenance/audit rows.
- 1,264 Quarantine records and 769 excluded issue rows remain outside production.
- Poster reconciliation: 4,545 `machine_passed`, 73 `content_conflict`, 9
  `placeholder`, and 31 `missing`.
- The parent Release preserves its historical poster-rights field. The RAG demo's confirmed-use
  decision is additive and does not rewrite that immutable six-artifact Release manifest.

Build and verify the deterministic local Release with:

```powershell
uv run hk-movie-rag build-release-data
uv run hk-movie-rag build-posters
uv run hk-movie-rag build-manifest
uv run hk-movie-rag verify-release data/release/v1.2/release_manifest.json
```

## Future posters and movie analysis

New posters must be submitted through the versioned poster staging contract documented in
`docs/source-contract/poster-submission.md`. A filename stem joins only to an exact formal
Release `影片唯一ID`; fuzzy title matching is prohibited. Technical validation does not
grant publication rights, and no staged asset enters production until it is explicitly
reviewed and promoted into a later Release manifest.

New single-movie or deep-analysis documents must use the versioned contract in
`docs/source-contract/movie-analysis-submission.md`. They remain staging inputs until their
movie ID, provenance, rights, content hash, and review state pass the documented gates and a
new Release manifest authorizes them.

## Immutable archive and recovery

The v1.2 source archive is private GCS data under:

```text
gs://motionexpaiweb-880586285913-hk-movie-rag-source-archive/source/v1.2/files/<workspace-relative-path>
```

The frozen source inventory is under
`source/v1.2/manifests/source_inventory.jsonl`; immutable verification reports are under
`source/v1.2/manifests/verification-<UTC timestamp>.json`. Recovery must use the pinned
object generation and validate byte length plus streamed SHA-256 against the inventory.
Metadata alone is not recovery proof.

## Archive-gated cleanup boundary

`cleanup-plan` accepts only the frozen inventory, its sibling `duplicate_proof.json`, and the
exact reviewed archive verification artifact. A valid plan may contain only:

- `posters/__MACOSX`
- `1-1500posters/1-1500posters`
- `posters1`
- inventoried `.DS_Store` files below the four configured raw roots

`FinalDelivery_2026-07-16`, outer `1-1500posters`, `posters/posters`, unique source files,
canonical Release outputs, and canonical poster assets are never cleanup targets. Plan
generation is a dry run:

```powershell
uv run hk-movie-rag cleanup-plan `
  --inventory artifacts/inventory/v1.2/source_inventory.jsonl `
  --archive-verification artifacts/archive/v1.2/verification.json `
  --quarantine-root 'D:\approved-hk-movie-quarantine\v1.2-<reviewed-run-id>' `
  --output artifacts/cleanup/v1.2/plan.json
```

The quarantine root is a caller-approved exact absolute path. It must be absent, outside the
workspace, on the same volume, and reached only through stable non-reparse ancestors. The
plan binds those ancestor identities and one deterministic, non-overlapping logical slot per
target.

Cleanup execution requires a separately reviewed immutable plan and the exact confirmation
`v1.2`. It atomically renames one exact target at a time into that external quarantine and
stops at the first transaction failure or observed drift:

```powershell
uv run hk-movie-rag cleanup-execute `
  --plan artifacts/cleanup/v1.2/plan.json `
  --confirm-release v1.2 `
  --output artifacts/cleanup/v1.2/report.json
```

Generating a plan does not authorize executing it. Archive verification does not authorize
detach by itself. A recorded detach does not authorize purge, recycle-provider use, ingestion,
RAG readiness, or any other destructive follow-up; those require a future, separately reviewed
action. The
plan output is fixed to
`artifacts/cleanup/v1.2/plan.json`, is created once, and never replaces an existing file. A
plan is physically bound to the workspace that created it; copying it to another checkout or
worktree makes validation fail before source evidence or targets are read.

The execution report output is fixed to `artifacts/cleanup/v1.2/report.json` and is also
create-only. It is an append-only JSON Lines checkpoint journal: every complete line is one
full cleanup report. Execution fsyncs a `prepared` zero-action checkpoint before it reserves
the exact external quarantine root create-only. A collision or unsafe destination stops before
any source move. `cleanup-report/v4` persists all four source-root device/inode identities;
post-reservation records also persist the reserved quarantine-root identity even when no active
intent exists.

For each target, execution fully revalidates the source, creates only its bound empty slot
parents, and fsyncs an `in_progress` intent containing the exact source, slot, and proof before
the no-replace atomic rename. A `detached_to_quarantine` event then records the approved source,
exact slot, plan/content digests, quarantine-root identity, original root device/inode observed
before and immediately after the rename, and the time interval bracketing the syscall. That event
is immutable historical evidence of the rename; it is not a claim that two later namespace reads
were atomic.

After persisting the event, execution records a separately timed `current_observation` with
`atomic: false`. It samples source-root identities, the old source name, exact slot root identity,
and current slot contents. A recreated source, missing/replaced slot, content change, unsafe path,
or unreadable state never rewrites the event. Instead execution stops with
`detached_with_drift`; only a drift-free complete run returns `detached`. Later changes require a
fresh `assess_cleanup_recovery()` observation. The journal never overwrites or rolls back either
namespace. Both terminal statuses are published only after the release guard exits successfully;
a guard-exit failure remains a recoverable terminal `failed` checkpoint.

A short write, disk-full error, fsync failure, or process interruption can damage only the
journal tail. `recover_cleanup_report_checkpoint()` returns the last complete schema-valid
line without first requiring an intentionally moved source root to exist.
`assess_cleanup_recovery()` then validates the report bindings and external quarantine without
changing either namespace. After a crash between rename and event checkpoint, it infers the
historical event only when the exact original root device/inode is at the exact reserved slot and
is no longer at the source. It does not require the mutable tree hash to remain unchanged. The
inferred event uses the durable intent time and recovery observation time as honest bounds instead
of fabricating the syscall time. Both/neither/wrong-slot root-identity states remain `conflict`.
Validated events must also form the complete ordered prefix of every planned target before recovery
can return a terminal state. An incomplete prefix returns `partial` even when its fresh observation
finds no drift, and its observation still records any content or namespace drift. Only a complete
prefix maps a drift-free observation to `detached` or a blocking drift observation to
`detached_with_drift`.

Every report contains `rag_readiness: not_authorized`. Cleanup paths, detach events, quarantine
slots, and the dirty-path plan are never ingestion authority. Any later RAG readiness decision
requires a fresh post-cleanup inventory reconciled against the release manifest and canonical
allowlist. The report embeds the reviewed inventory,
duplicate-proof, archive-verification, plan and per-target size/source-hash/content-proof
bindings. CLI stdout remains a single final report object on ordinary completion/failure; the
on-disk `report.json` is the durable JSONL history.

## Phase boundary

The original Phase 1 manifest still governs data eligibility and remains unchanged. The minimum
RAG demo is the separately reviewed consumer implemented here; it does not authorize cleanup,
public object publication, production SLOs, or integration with another frontend/backend.

## R3: reusable five-document pipeline

`v1.2-demo-r3` is an immutable RAG child overlay. It preserves all 4,658 rows in the governed
v1.2 parent and explicitly adds `2001_SLZQ_001` (`少林足球`) as one supplemental, provenance-bound
movie. Runtime records remain uniform: the resulting bundle has 4,659 movie metadata passages,
five document bindings, 21 PDF passages and 4,680 expected embeddings.

The external input directory must contain exactly these governed PDF bytes:

- `S 級港⽚-醉拳.pdf` — 3 pages
- `S 級港⽚-最佳拍檔.pdf` — 3 pages
- `S 級港⽚-殭屍先生.pdf` — 4 pages
- `S級港片-富貴逼人.pdf` — 6 pages
- `S 級港⽚-少林足球.pdf` — 5 pages

The supplemental poster source is retained beside those inputs for provenance. Its bounded WebP
derivative is `assets/posters/derived/2001_SLZQ_001.webp` in the governed source root. The
configuration binds both hashes; public poster delivery remains same-origin and integrity-checked.

Build twice from the same immutable inputs and verify byte equality before deployment:

```powershell
$SourceRoot = 'D:\VS_PROJECT\HK_Movie_KnowledgeGraph\RAG'
$DocumentRoot = 'D:\VS_PROJECT\HK_Movie_KnowledgeGraph\RAG_INPUTS\v1.2-demo-r3'

uv run hk-movie-rag build-rag-bundle `
  --source-root $SourceRoot `
  --document-source-root $DocumentRoot `
  --config config/rag_demo_r3.yaml `
  --output artifacts/rag-demo/v1.2-demo-r3-a

uv run hk-movie-rag build-rag-bundle `
  --source-root $SourceRoot `
  --document-source-root $DocumentRoot `
  --config config/rag_demo_r3.yaml `
  --output artifacts/rag-demo/v1.2-demo-r3-b

uv run hk-movie-rag verify-rag-bundle `
  artifacts/rag-demo/v1.2-demo-r3-a/rag_release_manifest.json
```

The two `rag_bundle.jsonl` files and two manifests must be byte-identical. The verified contract is
4,659 movies, 4,659 poster rows, 4,546 approved poster objects, 113 unavailable posters, 51/313/4,295
S/A/B facets, 24 pilot movies, five documents and 21 PDF passages.

Deploy from a clean code worktree. The deployment harness verifies the exact schema, source hashes,
poster authority, relevance policy, R2 reuse objects and all counts before any mutation. It reuses
4,670 R2 embeddings and allows exactly ten new embeddings:

```powershell
pwsh -NoProfile -File scripts/gcp/deploy-demo.ps1 `
  -ExpectedRagReleaseId v1.2-demo-r3 `
  -RagConfigPath config/rag_demo_r3.yaml `
  -SourceRoot 'D:\VS_PROJECT\HK_Movie_KnowledgeGraph\RAG' `
  -DocumentSourceRoot 'D:\VS_PROJECT\HK_Movie_KnowledgeGraph\RAG_INPUTS\v1.2-demo-r3' `
  -ReuseFromReleaseId v1.2-demo-r2 `
  -BundleDirectory artifacts/rag-demo/v1.2-demo-r3-deploy `
  -Apply
```

Publication is create-only under `rag/v1.2-demo-r3/`. A no-traffic Cloud Run candidate must pass
the ten immutable deep-answer cases, poster byte checks, title/person routing regressions and the
generic movie/OOD golden gate before stable promotion. Rollback selects `v1.2-demo-r2` as the active
database release and restores the last accepted Cloud Run revision; it never deletes R3 evidence.
