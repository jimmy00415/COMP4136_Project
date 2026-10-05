# RAG-R2 Reusable Document Pipeline Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use
> `superpowers:subagent-driven-development` to execute this plan one task at a time,
> with requirements and quality review after every task.

**Goal:** Publish `v1.2-demo-r2` with 4,658 metadata passages, three deep-analysis
documents, 12 page passages, 4,670 embeddings, canonical metadata precedence, working
poster cards, and a smoke-verified Cloud Run chatbot link; leave a data-driven pipeline
that accepts another declared PDF without another database redesign.

**Architecture:** Keep the governed Release v1.2 data and current `v1.2-demo`
immutable. Build a schema-1.2 RAG child manifest into a parallel release, bind its exact
manifest identity into Cloud SQL and Cloud Run, copy only byte-and-contract-identical
vectors from the active predecessor, embed only new/changed passages, and validate a
zero-traffic tagged revision before promotion. Raw PDFs live in a separately supplied,
hash-bound document root and never enter Git or the image.

**Tech Stack:** Python 3.11+, pytest, JSON Schema 2020-12, pypdf, PostgreSQL 16 with
pgvector, psycopg, Vertex AI `gemini-embedding-2` and `gemini-3.5-flash-lite`, Google
Cloud Storage, Cloud Run Jobs, Cloud Run, PowerShell, Google Cloud CLI.

## Global constraints

- Work only in `D:\VS_PROJECT\HK_Movie_KnowledgeGraph\RAG\.worktrees\rag-r2` on
  branch `codex/rag-r2`; preserve the user's untracked files in the primary worktree.
- Use `apply_patch` for source and documentation edits.
- For every behavior change, first add the smallest failing test and record the expected
  failure before implementation.
- Historical `config/rag_demo.yaml`, schema 1.1 validation, and `v1.2-demo` tests must
  remain valid.
- Never print credentials, access codes, database URLs, signed URLs, or tokens.
- Never overwrite immutable GCS objects. Same key plus identical bytes is idempotent;
  same key plus different bytes is a hard failure.
- Never send traffic to the candidate until authenticated functional smoke passes on its
  tag URL.
- No fallback from a failed 4,658-vector reuse contract to a full re-embedding run.
- Do not upload the raw PDFs or widen the runtime service account's access to the
  separately governed source archive in this release.
- Baseline `pytest -q` exceeded ten minutes before implementation. Run targeted files
  after each task and partition the final suite to identify any slow deployment tests.

---

## Task 1: Versioned manifest and deterministic PDF input contract

**Files:**

- Modify: `src/hk_movie_rag/pdf_extract.py`
- Modify: `src/hk_movie_rag/rag_bundle.py`
- Modify: `src/hk_movie_rag/cli.py`
- Create: `schemas/rag_release_manifest_v1_2.schema.json`
- Create: `src/hk_movie_rag/schemas/rag_release_manifest_v1_2.schema.json`
- Modify: `tests/test_pdf_extract.py`
- Modify: `tests/test_rag_bundle.py`
- Modify: `tests/test_cli.py`
- Modify: `tests/test_schemas.py`

- [ ] Add failing PDF normalization tests proving `富 貴 逼 人` and CJK
  punctuation collapse deterministically while `It's a mad, mad, mad world`, URLs,
  ASCII words, blank-line paragraph boundaries, and timestamps keep meaningful spaces.

- [ ] Add failing schema/bundle tests proving schema 1.1 still requires exactly two
  documents, schema 1.2 accepts any positive document count, and schema 1.2 rejects
  duplicate document IDs, duplicate source bindings, duplicate page IDs, page-count
  drift, non-contiguous/non-positive pages, empty pages, invalid rights/quality states,
  missing policy hashes, and unknown profiles.

- [ ] Add a failing CLI test for optional `--document-source-root`; it must default to
  `--source-root`, resolve to a directory, reject symlink/path escape, and allow only
  directly declared filenames under that root.

- [ ] Implement `cjk-layout-v1` in `normalize_pdf_text()` and add a profile argument to
  extraction. Preserve the historical profile for schema 1.1 verification; use
  `cjk-layout-v1` for schema 1.2 construction.

- [ ] Add a frozen `RagReleaseCounts` and `RagReleaseContract`. Populate contract fields
  from the verified manifest, including manifest SHA, bundle SHA, models, extraction and
  embedding-input profiles, policy hashes, access mode, and dynamic counts.

- [ ] Dispatch `_validate_manifest()` by exact schema version. Do not edit the existing
  schema-1.1 resources. Add Python cross-field validation:

  ```python
  counts.documents == len(documents)
  counts.pdf_passages == sum(document.page_count for document in documents)
  counts.metadata_passages + counts.pdf_passages == contract.expected_embeddings
  ```

- [ ] Generalize document collection to one or more unique declarations and bind each
  declaration to the extracted page count. Preserve page-number and content-hash
  determinism.

- [ ] Run:

  ```powershell
  uv run pytest tests/test_pdf_extract.py tests/test_rag_bundle.py tests/test_cli.py tests/test_schemas.py -q
  uv run ruff check src/hk_movie_rag/pdf_extract.py src/hk_movie_rag/rag_bundle.py src/hk_movie_rag/cli.py tests/test_pdf_extract.py tests/test_rag_bundle.py tests/test_cli.py tests/test_schemas.py
  uv run mypy src/hk_movie_rag/pdf_extract.py src/hk_movie_rag/rag_bundle.py src/hk_movie_rag/cli.py
  ```

- [ ] Commit: `feat: add versioned reusable RAG bundle contract`

---

## Task 2: Bind database releases to manifests and reuse exact embeddings

**Files:**

- Create: `src/hk_movie_rag/migrations/0004_release_manifest_identity.sql`
- Modify: `src/hk_movie_rag/rag_db.py`
- Modify: `src/hk_movie_rag/rag_ingest.py`
- Modify: `src/hk_movie_rag/cli.py`
- Modify: `tests/test_rag_db.py`
- Modify: `tests/test_rag_ingest.py`
- Modify: `tests/test_cli.py`

- [ ] Add failing migration tests proving `rag_releases` stores a 64-hex
  `manifest_sha256` and a non-empty `document_embedding_profile`. Only legacy rows may
  start null; a locked legacy row may make one null-to-verified claim from a freshly
  downloaded and verified immutable manifest, and identity cannot drift afterward.
  New rows require both identities at insert time.

- [ ] Add failing repository tests proving `begin_release(bundle.contract)` inserts
  dynamic counts, rejects reuse of a release ID with a different manifest/count/model/
  profile, and allows an identical interrupted loading run.

- [ ] Add failing `reuse_embeddings()` tests. Source must be active, target loading,
  release identities distinct, models/dimensions/profiles equal, and these passage
  fields exactly equal: ID, movie ID, kind, document ID, page, body, content SHA, and
  canonical embedding title. Only null target vectors may be filled.

- [ ] Implement one database-side `UPDATE ... FROM` reuse operation and return an exact
  count. Do not export vectors to Python.

- [ ] Add `reuse_from_release_id` and final-state reuse gates to ingestion/CLI. Perform
  all reuse validation before the first Vertex call. Reports expose
  `preexisting`, `copied_this_run`, `embedded_this_run`, `skipped_this_run`,
  `source_eligible_total`, and `non_reusable_embedded_total`; define skipped as passages
  for which the current run made no Vertex call.

- [ ] Make readiness and activation compare observed rows to the selected release row's
  expected counts and contract rather than module constants. Expected embeddings are
  metadata plus PDF passages. Keep vector dimension 768 as the current physical schema
  constraint.

- [ ] Add the active rerun path: an identical active bundle returns
  `embedded_this_run=0, copied_this_run=0, skipped_this_run=4670`; a non-identical active
  bundle fails. Both clean and resumed loading runs must finish with
  `source_eligible_total=4658` and `non_reusable_embedded_total=12`.

- [ ] Run:

  ```powershell
  uv run pytest tests/test_rag_db.py tests/test_rag_ingest.py tests/test_cli.py -q
  uv run ruff check src/hk_movie_rag/rag_db.py src/hk_movie_rag/rag_ingest.py src/hk_movie_rag/cli.py tests/test_rag_db.py tests/test_rag_ingest.py tests/test_cli.py
  uv run mypy src/hk_movie_rag/rag_db.py src/hk_movie_rag/rag_ingest.py src/hk_movie_rag/cli.py
  ```

- [ ] Commit: `feat: bind releases and reuse unchanged embeddings`

---

## Task 3: Runtime identity and canonical metadata precedence

**Files:**

- Modify: `src/hk_movie_rag/demo_api.py`
- Modify: `src/hk_movie_rag/rag_query.py`
- Modify: `src/hk_movie_rag/retrieval.py`
- Modify: `tests/test_demo_api.py`
- Modify: `tests/test_rag_query.py`
- Modify: `tests/test_retrieval.py`

- [ ] Add failing settings/startup tests requiring
  `RAG_RELEASE_MANIFEST_SHA256`, validating 64 lowercase hex, and rejecting DB state
  whose release ID, manifest SHA, model, dimension, or dynamic counts differ.

- [ ] Remove runtime `2/6/4664` gates. Read exact expectations from the DB row bound to
  the expected manifest identity; retain fixed parent-dataset/poster/facet expectations
  where those are dataset invariants.

- [ ] Add a failing query regression for `《富貴逼人》的主演和片長？`: even with PDF
  passages present, target policy must select only `metadata:1987_FGBR_001` and preserve
  the canonical 100-minute record.

- [ ] Change metadata intent policy so canonical fields always target the metadata
  passage, regardless of whether that movie has a deep document. Keep qualitative named
  questions constrained to that movie's PDF passages. A mixed intent such as
  `片長和視覺` must retrieve both branches, cite metadata for canonical facts and PDF for
  analysis, and never source canonical facts from the PDF.

- [ ] Replace corpus-wide literal prose such as “only two documents” with evidence-scope
  wording based on the passages used in the current answer. Preserve limited-evidence
  disclosures for broad conclusions.

- [ ] Run:

  ```powershell
  uv run pytest tests/test_demo_api.py tests/test_rag_query.py tests/test_retrieval.py -q
  uv run ruff check src/hk_movie_rag/demo_api.py src/hk_movie_rag/rag_query.py src/hk_movie_rag/retrieval.py tests/test_demo_api.py tests/test_rag_query.py tests/test_retrieval.py
  uv run mypy src/hk_movie_rag/demo_api.py src/hk_movie_rag/rag_query.py src/hk_movie_rag/retrieval.py
  ```

- [ ] Commit: `fix: keep canonical movie facts authoritative`

---

## Task 4: Data-driven relevance, poster, and live-answer authorities

**Files:**

- Create: `src/hk_movie_rag/policies/general_relevance_v1_2_demo_r2.json`
- Create: `src/hk_movie_rag/policies/deep_document_cases_v1_2_demo_r2.jsonl`
- Create: `src/hk_movie_rag/authorities/poster_serving_authority_v2.json`
- Modify: `src/hk_movie_rag/relevance.py`
- Modify: `src/hk_movie_rag/poster_authority.py`
- Modify: `src/hk_movie_rag/poster_fleet.py`
- Modify: `scripts/gcp/live-smoke.py`
- Modify: `tests/test_relevance.py`
- Modify: `tests/test_poster_authority.py`
- Modify: `tests/test_poster_fleet.py`
- Modify: `tests/test_gcp_demo_scripts.py`

- [ ] Add failing policy-loader tests for exact release selection and digest validation;
  preserve the old policy and add an R2 policy carrying the same 40-case domain gate.
  Keep governed `富貴逼人` conversational cases in the separate deep-case JSONL and
  require both artifacts' exact release/digest identities.

- [ ] Add failing poster-authority tests for v2 dataset scope. Bind the Release v1.2
  parent manifest, derived poster inventory, private same-origin proxy, operator
  permission, and rights boundary without binding document count. Keep v1 valid for the
  old release.

- [ ] Make poster-fleet verification accept a verified release contract/authority rather
  than an old release module constant. Do not regenerate unchanged poster bytes.

- [ ] Replace live smoke's static document dictionary and p1-p3 regex with manifest plus
  governed JSONL authority. Syntax permits any positive page; semantic validation
  requires the cited page to exist for the cited document.

- [ ] Add deep-answer cases for:

  ```text
  《富貴逼人》的視覺風格與空間設計？
  那它的聲音設計呢？
  《富貴逼人》的主演和片長？
  ```

  Require movie `1987_FGBR_001`, the appropriate PDF/metadata authority, conversation
  continuity, successful poster bytes, dual evidence for a mixed metadata/analysis
  question, and explicit “the supplied analysis argues” attribution for disputed
  symbolism or creative-intent claims.

- [ ] Run:

  ```powershell
  uv run pytest tests/test_relevance.py tests/test_poster_authority.py tests/test_poster_fleet.py tests/test_gcp_demo_scripts.py -q
  uv run ruff check src/hk_movie_rag/relevance.py src/hk_movie_rag/poster_authority.py src/hk_movie_rag/poster_fleet.py scripts/gcp/live-smoke.py tests/test_relevance.py tests/test_poster_authority.py tests/test_poster_fleet.py tests/test_gcp_demo_scripts.py
  uv run mypy src/hk_movie_rag/relevance.py src/hk_movie_rag/poster_authority.py src/hk_movie_rag/poster_fleet.py scripts/gcp/live-smoke.py
  ```

- [ ] Commit: `feat: drive RAG answer authorities from release data`

---

## Task 5: R2 config, safe candidate deployment, and operator documentation

**Files:**

- Create: `config/rag_demo_r2.yaml`
- Modify: `scripts/gcp/deploy-demo.ps1`
- Modify: `src/hk_movie_rag/static/index.html`
- Modify: `README.md`
- Modify: `tests/test_gcp_deploy_apply.py`
- Modify: `tests/test_gcp_demo_scripts.py`
- Modify: `tests/test_rag_bundle.py`

- [ ] Add the three exact document bindings, six declared pages for `富貴逼人`, model
  profiles, and verified R2 policy/authority digests to `config/rag_demo_r2.yaml`.
  Preserve `config/rag_demo.yaml` byte-for-byte.

- [ ] Add failing deployment tests for `-RagConfigPath`,
  `-DocumentSourceRoot`, and `-ReuseFromReleaseId`. Parse build verification JSON as the
  only source for release ID, prefix, counts, manifest hash, models, and policy digests.

- [ ] Add failing traffic-order tests requiring: capture old revision/etag, deploy an
  immutable image with zero traffic and a unique tag, run authenticated live smoke
  against the tag URL, promote by conditional traffic update only after success, smoke
  the stable URL, conditionally roll back on post-promotion failure, and conditionally
  remove the temporary tag after success, failure, or rollback. The access code may only
  enter smoke through its existing environment/secret boundary and must never appear in
  arguments or reports.

- [ ] Update ingestion acceptance to require final-state
  `source_eligible_total=4658, non_reusable_embedded_total=12`. A clean first R2 run is
  expected to report `copied_this_run=4658, embedded_this_run=12`; a resumed run may
  report smaller deltas. Follow with an active rerun of
  `copied_this_run=0, embedded_this_run=0, skipped_this_run=4670`.

- [ ] Derive create-only GCS objects beneath `rag/v1.2-demo-r2/`; reject unsafe release
  IDs before object construction.

- [ ] Update the UI/README to describe three deep documents and the reusable command
  sequence. Document raw-PDF separation, immutable child releases, reuse gates,
  candidate smoke, rollback, and how to add the fourth PDF.

- [ ] Run:

  ```powershell
  uv run pytest tests/test_gcp_deploy_apply.py tests/test_gcp_demo_scripts.py tests/test_rag_bundle.py -q
  uv run ruff check scripts/gcp/live-smoke.py tests/test_gcp_deploy_apply.py tests/test_gcp_demo_scripts.py tests/test_rag_bundle.py
  ```

- [ ] Commit: `feat: deploy R2 behind candidate acceptance`

---

## Task 6: Build the real three-document release and verify locally

**Files/data:**

- External input root: `D:\VS_PROJECT\HK_Movie_KnowledgeGraph\RAG_INPUTS\v1.2-demo-r2`
- Source PDFs: the two existing governed PDFs plus
  `C:\Users\陈奕炜\Downloads\S級港片-富貴逼人.pdf`
- Generated, ignored output: `artifacts/rag-demo/v1.2-demo-r2`

- [ ] Create the exact external input directory, copy only the three named PDFs, resolve
  each final absolute path, and verify all three declared SHA-256 values before build.
  Never delete or move the user's originals.

- [ ] Build twice into separate ignored directories and prove identical manifest and
  bundle SHA-256 values:

  ```powershell
  uv run hk-movie-rag build-rag-bundle --source-root . --document-source-root 'D:\VS_PROJECT\HK_Movie_KnowledgeGraph\RAG_INPUTS\v1.2-demo-r2' --config config/rag_demo_r2.yaml --output artifacts/rag-demo/v1.2-demo-r2-a
  uv run hk-movie-rag build-rag-bundle --source-root . --document-source-root 'D:\VS_PROJECT\HK_Movie_KnowledgeGraph\RAG_INPUTS\v1.2-demo-r2' --config config/rag_demo_r2.yaml --output artifacts/rag-demo/v1.2-demo-r2-b
  ```

- [ ] Verify exact output: 4,658 movies/posters/facets/metadata, 3 documents, 12 PDF
  passages, expected 4,670 embeddings, 4,545 approved poster objects, 113 unavailable,
  and B-tier `1987_FGBR_001` with its existing poster identity.

- [ ] Inspect all six new page bodies for non-empty normalized CJK text, exact page
  numbering, and absence of hidden file paths or credentials.

- [ ] Run the full local gate in partitions, then the complete command with an explicit
  long timeout:

  ```powershell
  uv run pytest tests/test_pdf_extract.py tests/test_rag_bundle.py tests/test_rag_db.py tests/test_rag_ingest.py tests/test_demo_api.py tests/test_rag_query.py tests/test_retrieval.py tests/test_relevance.py tests/test_poster_authority.py tests/test_poster_fleet.py -q
  uv run pytest tests/test_gcp_demo_scripts.py tests/test_gcp_deploy_apply.py -q
  uv run ruff check .
  uv run mypy src
  uv run pytest -q
  ```

- [ ] Commit any deterministic fixture/digest updates as:
  `test: lock R2 release acceptance evidence`

---

## Task 7: Authenticate, deploy, smoke, and hand off the stable link

**Cloud scope:** project `motionexpaiweb`; existing SQL, private bucket, service, Jobs,
service accounts, Artifact Registry, and Secret Manager resources only. Creating the new
immutable R2 objects/release/revision is authorized; destructive cleanup is not.

- [ ] Reauthenticate the configured Motion Expert operator for both gcloud and ADC using
  the already logged-in Chrome profile. Verify only account/project identifiers and
  required API access; do not print tokens.

- [ ] Freshly describe the existing Cloud Run service, Cloud SQL instance, ingestion and
  poster jobs, release bucket, old RAG objects, and poster inventory. Record old revision,
  traffic, image digest, DB active release, and object generations for rollback evidence.
  Download and verify the exact active `v1.2-demo` manifest before making the legacy DB
  row's one allowed manifest/profile identity claim; do not use a guessed local digest.

- [ ] Run deployment from the clean R2 worktree with explicit project, config, document
  root, and reuse source. Keep ambient project overrides out of the child process and
  preload the existing access code through the approved environment/secret path without
  printing it.

- [ ] Require successful migration, bundle upload, 4,658-vector reuse, 12 new Vertex
  embeddings, activation, idempotent rerun, 4,545 poster verification, tagged-candidate
  smoke, conditional promotion, stable smoke, and removal of the temporary tag.

- [ ] Independently verify through HTTP:

  - `/health` and UI are 200;
  - authenticated `/api/config` identifies `v1.2-demo-r2`, exact manifest SHA, and
    `4658/3/12/4670` counts;
  - a comedy recommendation returns governed movies and citations;
  - the three `富貴逼人` acceptance turns preserve authority and context;
  - `/api/posters/1987_FGBR_001` is authenticated WebP with the governed 44,874-byte
    object identity;
  - unauthorized access and logout rejection still work.

- [ ] Ask a final independent reviewer to compare the implemented diff and live evidence
  to this plan. Fix all blocking findings and rerun affected gates.

- [ ] Use `superpowers:verification-before-completion`, then
  `superpowers:finishing-a-development-branch`. Push/integrate only after tests and live
  acceptance pass. Deliver the stable Cloud Run link, active release/manifest identity,
  exact reuse/new-embedding evidence, rollback state, and the reusable fourth-PDF command.
