# RAG R3 Supplemental Movie And Two-PDF Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** Publish `v1.2-demo-r3` on GCP with 4,659 queryable movies and five deep-analysis PDFs, including complete cited answers and poster cards for `殭屍先生` and `少林足球`.

**Architecture:** Extend the immutable RAG manifest with a schema-1.3 supplemental overlay that leaves the governed 4,658-row parent archive and R2 child untouched. Emit the supplemental movie into the existing runtime record shapes, reuse all 4,670 eligible R2 embeddings, create ten new Vertex embeddings, then promote a no-traffic Cloud Run candidate only after live acceptance gates pass.

**Tech Stack:** Python 3.12, pydantic-style validation helpers, pypdf, PostgreSQL/pgvector, Vertex AI embeddings and generation, Cloud SQL, Cloud Storage, Cloud Run, PowerShell deployment harness, pytest, Ruff, mypy.

**Spec:** `docs/superpowers/specs/2026-08-23-rag-r3-supplemental-movie-two-pdf-design.md`

## Global Constraints

- Release id is exactly `v1.2-demo-r3`; manifest schema is exactly `hk_movie_rag_release_manifest/1.3`.
- Preserve `v1.2-demo-r2`, its GCS prefix, database rows and stable deployment until candidate acceptance passes.
- Target counts are exactly 4,659 movies, 5 documents, 21 PDF passages and 4,680 embeddings.
- Incremental ingestion must reuse exactly 4,670 embeddings and create exactly 10.
- Supplemental movie id is exactly `2001_SLZQ_001`; it must have explicit provenance, facet and poster bindings.
- No raw GCS URI is exposed to browser clients; poster delivery stays behind the same-origin proxy.
- All builds and deployments fail closed on identity, count, extraction, citation, poster, reuse or live-smoke mismatches.

---

### Task 1: Add schema-1.3 supplemental overlay validation

**Files:**
- Modify: `src/hk_movie_rag/rag_bundle.py`
- Modify: `tests/test_rag_bundle.py`
- Modify: `tests/test_schemas.py`

**Interfaces:**
- Consumes: existing `build_rag_bundle(...)`, `verify_rag_bundle(...)`, schema-1.2 record and manifest contracts.
- Produces: schema-1.3 config keys `parent_rag_release_id`, `supplemental_movies`, `supplemental_facets`, and `supplemental_posters`; normal runtime records and a verified manifest contract.

- [ ] Write failing tests proving one complete supplemental movie is merged into movie/poster/facet records and target counts.
- [ ] Write failing tests proving parent-id collisions, duplicate ids, missing canonical fields, invalid runtime/date, missing provenance, facet or poster fail before output.
- [ ] Write a failing compatibility test proving schema 1.2 still enforces its original 4,658-row facet artifact equality rule.
- [ ] Run `uv run pytest tests/test_rag_bundle.py tests/test_schemas.py -q` and confirm the new tests fail for missing 1.3 behavior.
- [ ] Implement a focused overlay parser and merge step in `rag_bundle.py`; retain the current schema-1.2 code path unchanged.
- [ ] Extend manifest verification so 1.3 requires parent artifact rows plus supplemental rows to equal the target, and recomputes supplemental identities.
- [ ] Run `uv run pytest tests/test_rag_bundle.py tests/test_schemas.py -q` and require PASS.
- [ ] Commit with `git commit -m "feat: add immutable RAG supplemental movie overlay"`.

### Task 2: Bind the R3 poster authority and release identity

**Files:**
- Create: `src/hk_movie_rag/data/poster_serving_authority_v3.json`
- Modify: `src/hk_movie_rag/poster_authority.py`
- Modify: `src/hk_movie_rag/relevance.py`
- Modify: `scripts/gcp/live-smoke.py`
- Modify: `tests/test_poster_authority.py`
- Modify: `tests/test_relevance.py`
- Modify: `tests/test_gcp_demo_scripts.py`

**Interfaces:**
- Consumes: R3 poster overlay identity and exact manifest SHA produced by the deterministic build.
- Produces: a release-to-authority mapping for `v1.2-demo-r3` and live-smoke acceptance of immutable R3 identities without weakening R2.

- [ ] Write failing tests for 4,659 poster mappings, 4,546 approved posters, exact R3 release mapping and rejection of an R2/R3 authority mismatch.
- [ ] Write failing tests that relevance and live smoke accept the configured R3 release rather than a hard-coded R2-only branch.
- [ ] Run `uv run pytest tests/test_poster_authority.py tests/test_relevance.py tests/test_gcp_demo_scripts.py -q` and confirm the new assertions fail.
- [ ] Generate the v3 authority from the validated parent inventory plus one `2001_SLZQ_001` asset; store the canonical JSON as package data.
- [ ] Replace R2-specific branching with an immutable per-release contract map for R2 and R3.
- [ ] Run the focused tests plus `uv run ruff check` and `uv run mypy` on the modified modules.
- [ ] Commit with `git commit -m "feat: bind R3 poster and retrieval authorities"`.

### Task 3: Parameterize deployment without weakening immutable gates

**Files:**
- Modify: `scripts/gcp/deploy-demo.ps1`
- Modify: `tests/test_gcp_deploy_apply.py`
- Modify: `tests/test_gcp_demo_scripts.py`

**Interfaces:**
- Consumes: verified bundle manifest fields and expected contract arguments supplied by the caller.
- Produces: create-only GCS publication and no-traffic candidate deployment for either the existing R2 contract or the exact R3 contract.

- [ ] Write failing tests that a caller can select the R3 bundle directory and pass exact expected release, schema, count and authority identities.
- [ ] Write failing tests that missing or mismatched expected R3 identities abort before GCS, Cloud SQL or Cloud Run mutation.
- [ ] Run `uv run pytest tests/test_gcp_deploy_apply.py tests/test_gcp_demo_scripts.py -q` and confirm failure.
- [ ] Replace embedded R2 constants with mandatory expected-contract parameters that default only for the legacy R2 path.
- [ ] Preserve generation-match/create-only GCS writes, candidate-first traffic, database activation and rollback behavior.
- [ ] Run the focused tests and static checks; require PASS.
- [ ] Commit with `git commit -m "feat: parameterize immutable RAG deployment contracts"`.

### Task 4: Create the external R3 input and deterministic bundle

**Files:**
- Create outside Git: `D:\VS_PROJECT\HK_Movie_KnowledgeGraph\RAG_INPUTS\v1.2-demo-r3\` containing exactly five PDFs and one restricted poster source/derivative.
- Create: `config/rag_demo_r3.yaml`
- Create ignored output: `artifacts/rag-demo/v1.2-demo-r3-a`
- Create ignored output: `artifacts/rag-demo/v1.2-demo-r3-b`
- Modify: `README.md`
- Test: `tests/integration/test_rag_demo_source.py`

**Interfaces:**
- Consumes: the three R2 PDF inputs, the two user PDFs, governed v1.2 source root, approved supplemental metadata and poster source.
- Produces: byte-identical verified R3 bundles and documented repeatable build commands.

- [ ] Copy the five PDFs to the R3 input root without changing source bytes; verify each SHA-256 against configuration.
- [ ] Fetch the selected poster source, record its public source page and SHA-256, derive a valid bounded WebP and record its dimensions and digest.
- [ ] Add a failing integration test for exact R3 counts, document ids, source hashes, supplemental identity and poster asset.
- [ ] Add `config/rag_demo_r3.yaml` with the exact metadata and identities from the specification.
- [ ] Build directories `v1.2-demo-r3-a` and `v1.2-demo-r3-b` with `uv run hk-movie-rag build-rag-bundle ...`.
- [ ] Compare manifest and JSONL SHA-256 values between builds and require byte equality.
- [ ] Verify both bundles with `uv run hk-movie-rag verify-rag-bundle` and run the focused integration test against the explicit governed source root.
- [ ] Update README with the five-document input contract, R3 build, verify, deploy and rollback commands.
- [ ] Commit config, test and README with `git commit -m "feat: define reproducible five-document R3 release"`.

### Task 5: Verify local retrieval and incremental reuse behavior

**Files:**
- Modify only if a confirmed defect is found: `src/hk_movie_rag/rag_ingest.py`, `src/hk_movie_rag/rag_db.py`, `src/hk_movie_rag/retrieval.py`, `src/hk_movie_rag/rag_query.py`
- Test: corresponding `tests/test_*.py` files for any confirmed defect

**Interfaces:**
- Consumes: verified R3 bundle and existing R2 release rows.
- Produces: exact 4,670/10 reuse plan and cited retrieval results for both new documents.

- [ ] Run ingestion planning/dry-run against the verified bundle and assert 4,670 reusable plus 10 new embedding rows.
- [ ] Run offline retrieval fixtures for title, director/cast, thematic and cross-movie follow-up queries.
- [ ] If any result is wrong, first add a minimal failing regression test reproducing the defect, then implement only the confirmed fix and rerun the focused suite.
- [ ] Run `uv run pytest tests/test_pdf_extract.py tests/test_rag_bundle.py tests/test_rag_db.py tests/test_rag_ingest.py tests/test_rag_query.py tests/test_retrieval.py tests/test_relevance.py -q` and require PASS.
- [ ] Commit confirmed retrieval fixes separately; if none are needed, record the green evidence without a source commit.

### Task 6: Publish a GCP candidate and ingest R3

**Files:**
- No tracked file changes; generated deployment evidence remains ignored.

**Interfaces:**
- Consumes: exact verified R3 manifest, bundle, authority digest and current GCP credentials.
- Produces: create-only R3 GCS objects, an R3 Cloud SQL release, and a no-traffic Cloud Run candidate URL.

- [ ] Verify `gcloud auth list`, ADC token, project, billing, Cloud Storage, Cloud SQL, Vertex, Artifact Registry, Cloud Build and Cloud Run access.
- [ ] Upload the verified objects beneath `gs://motionexpaiweb-880586285913-hk-movie-rag-release/rag/v1.2-demo-r3/` with generation-zero preconditions.
- [ ] Run the deployment harness in candidate/no-traffic mode with exact R3 release, manifest, count and authority arguments.
- [ ] Require ingestion evidence of 4,670 reused, 10 generated and 4,680 total embeddings before activation.
- [ ] Record candidate revision, image digest, bundle digest, manifest digest and database status.

### Task 7: Run live acceptance and promote stable traffic

**Files:**
- No tracked file changes unless a live test reveals a confirmed bug; any such fix returns to the relevant TDD task.

**Interfaces:**
- Consumes: candidate URL, access code and exact R3 authorities.
- Produces: an independently verified stable chatbot URL serving R3.

- [ ] Authenticate to the candidate and assert `/api/config` reports 4,659 movies, 5 documents, 21 PDF passages, 4,680 embeddings and exact R3 identities.
- [ ] Ask the two acceptance questions from the spec and assert substantive answers, correct deep-document citations and correct movie cards.
- [ ] Fetch both poster proxy URLs and require HTTP 200, `image/webp`, nonzero bytes and matching authority identity.
- [ ] Run the cross-movie follow-up and `周星馳的動作喜劇`; assert correct current-person routing and no stale Wang Kar-wai/person intent.
- [ ] Activate R3, route stable traffic to the accepted revision, then repeat config, both movie questions, citations and poster checks through the stable URL.
- [ ] Run `uv run pytest -q` where the worktree has all required fixtures; report the governed-source integration suite separately if worktree containment prevents that subset.
- [ ] Commit any final documentation evidence with `git commit -m "docs: record R3 deployment verification"`.

### Task 8: Final audit and delivery

**Files:**
- Review: all changed tracked files and ignored evidence paths.

**Interfaces:**
- Consumes: committed implementation, verified release evidence and stable live results.
- Produces: final link, reusable pipeline command and explicit readiness/rollback statement.

- [ ] Run `git diff --check`, `git status --short`, focused pytest suites, Ruff and mypy with fresh output.
- [ ] Verify GCS object generations, Cloud SQL active release, Cloud Run stable revision and public chatbot page independently of deployment-script output.
- [ ] Confirm no R2 object or unrelated user worktree file was modified.
- [ ] Deliver the stable URL, access code, exact release counts, two tested questions, reproducible pipeline command, immutable evidence identities and rollback target.
