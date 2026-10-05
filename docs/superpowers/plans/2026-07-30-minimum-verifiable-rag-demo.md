# Minimum Verifiable Hong Kong Movie RAG Demo Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** Deploy an access-controlled Cloud Run chatbot that retrieves a Cloud SQL/pgvector database containing one metadata embedding and poster-state join for all 4,658 Release movies plus page-cited deep chunks from exactly two PDFs.

**Architecture:** A deterministic RAG bundle is derived only from the verified Phase 1 manifest and two hash-bound PDFs, uploaded to a private GCS release bucket, and idempotently ingested by a Cloud Run Job into PostgreSQL 16 with pgvector. One FastAPI Cloud Run service performs exact-title/vector retrieval, grounded Vertex generation, citation validation, restricted poster proxying, and serves a minimal same-origin chat UI.

**Tech Stack:** Python 3.11+, PyArrow, pypdf, psycopg 3, pgvector, google-genai, google-cloud-storage, FastAPI, Uvicorn, Cloud SQL PostgreSQL 16, Cloud Run, Cloud Build, Artifact Registry, Secret Manager, Vertex AI.

## Global Constraints

- Keep `data/release/v1.2/release_manifest.json` and its six-artifact contract unchanged.
- The formal population is exactly 4,658 unique Release movie IDs; no Quarantine or recursive discovery.
- Parent manifest SHA-256 is `e9842db33208e7146d78d86689b13f81fffcf41288d79d84e599b93050445e2c`.
- Deep documents are exactly `1978_ZQ_001`/醉拳 and `1982_ZJPD_001`/最佳拍檔 with the SHA-256 values in the approved design.
- The 24-movie pilot remains unchanged; the two-document smoke set is separate.
- Default embedding is `gemini-embedding-2` at 768 dimensions; fallback to `gemini-embedding-001` is allowed only before the first stored embedding and only after a live primary-model unavailable result.
- Generation uses `gemini-3.5-flash-lite`; retrieval returns at most eight unique passages.
- All 4,658 poster rows are loaded, but only technically approved objects are exposed through the authenticated same-origin proxy; never return a raw/public GCS URL.
- The demo uses `us-central1`, project `motionexpaiweb`, the exact resource names in the design, and no service-account key files.
- No MotionExpert frontend/backend/MySQL code or data is read or modified.
- No production-readiness claim; this plan proves a minimum vertical slice.

## Planned file map

```text
config/rag_demo.yaml                         exact release/document/model bindings
schemas/rag_release_manifest.schema.json    parent-bound bundle authority
migrations/0001_rag_demo.sql                PostgreSQL/pgvector schema
src/hk_movie_rag/rag_bundle.py              deterministic bundle builder/verifier
src/hk_movie_rag/pdf_extract.py             page-addressable PDF extraction
src/hk_movie_rag/rag_db.py                  psycopg connection/repository
src/hk_movie_rag/vertex_clients.py           embedding/generation adapters
src/hk_movie_rag/rag_ingest.py              resumable idempotent ingestion
src/hk_movie_rag/rag_query.py               retrieval, prompting, citation validation
src/hk_movie_rag/demo_auth.py               access-code session cookie
src/hk_movie_rag/demo_api.py                FastAPI routes and same-origin UI
src/hk_movie_rag/static/index.html           minimal chatbot
src/hk_movie_rag/static/app.js               login/chat/rendering behavior
src/hk_movie_rag/static/styles.css           responsive visual styling
scripts/gcp/provision-demo.ps1               idempotent GCP provisioning
scripts/gcp/deploy-demo.ps1                  image/job/service deployment
scripts/gcp/live-smoke.py                     deployed contract checks
Dockerfile                                  one image for service and Job
.dockerignore                               minimal deterministic build context
tests/test_rag_bundle.py                     bundle and PDF gates
tests/test_rag_db.py                         SQL/repository contracts
tests/test_rag_ingest.py                     retry/resume/idempotency behavior
tests/test_rag_query.py                      retrieval and citation grounding
tests/test_demo_api.py                       auth/API/poster behavior
tests/test_gcp_demo_scripts.py               plan-only provisioning/deploy behavior
```

---

### Task 1: Build the parent-bound deterministic RAG bundle

**Files:**
- Create: `config/rag_demo.yaml`
- Create: `schemas/rag_release_manifest.schema.json`
- Create: `src/hk_movie_rag/pdf_extract.py`
- Create: `src/hk_movie_rag/rag_bundle.py`
- Modify: `src/hk_movie_rag/cli.py`
- Modify: `pyproject.toml`
- Test: `tests/test_rag_bundle.py`

**Interfaces:**
- Consumes: explicit Phase 1 manifest path, explicit source workspace, and `config/rag_demo.yaml`.
- Produces: `build_rag_bundle(source_root: Path, config_path: Path, output_dir: Path) -> RagBundleResult`.
- Produces: `verify_rag_bundle(manifest_path: Path) -> VerifiedRagBundle` with paths, exact counts, selected model configuration, and ordered record iterator.
- CLI commands: `uv run hk-movie-rag build-rag-bundle --source-root 'D:\VS_PROJECT\HK_Movie_KnowledgeGraph\RAG' --config config/rag_demo.yaml --output artifacts/rag-demo/v1.2-demo` and `uv run hk-movie-rag verify-rag-bundle artifacts/rag-demo/v1.2-demo/rag_release_manifest.json`.

- [ ] **Step 1: Add failing tests for exact authority and deterministic records**

Create controlled fixtures with a six-artifact parent manifest, two one-page PDFs, three movies,
and three poster rows. The core tests must include literal expectations:

```python
def test_bundle_is_parent_bound_and_byte_deterministic(rag_source, tmp_path):
    first = build_rag_bundle(rag_source.root, rag_source.config, tmp_path / "one")
    second = build_rag_bundle(rag_source.root, rag_source.config, tmp_path / "two")
    assert first.movie_count == 3
    assert first.metadata_passage_count == 3
    assert first.document_count == 2
    assert first.pdf_passage_count == 2
    assert first.bundle_sha256 == second.bundle_sha256
    assert first.manifest_bytes == second.manifest_bytes

def test_bundle_rejects_changed_pdf_before_extraction(rag_source, tmp_path):
    rag_source.pdf_paths[0].write_bytes(b"changed")
    with pytest.raises(RagBundleError, match="PDF SHA-256 mismatch"):
        build_rag_bundle(rag_source.root, rag_source.config, tmp_path / "out")

def test_bundle_never_discovers_a_third_pdf(rag_source, tmp_path):
    (rag_source.root / "undeclared.pdf").write_bytes(rag_source.pdf_paths[0].read_bytes())
    result = build_rag_bundle(rag_source.root, rag_source.config, tmp_path / "out")
    assert result.document_count == 2
```

- [ ] **Step 2: Run the focused test and verify RED**

Run: `uv run pytest tests/test_rag_bundle.py -q`

Expected: collection or import failure because `rag_bundle` and `pdf_extract` do not exist.

- [ ] **Step 3: Implement page extraction and the bundle contract**

Use pypdf to extract each page independently. Normalize CRLF/CR to LF, collapse horizontal
whitespace, preserve paragraph breaks, skip only pages whose normalized text is empty, and hash
the UTF-8 normalized body. Render metadata passages from the 12 canonical fields in a fixed
field order. Write canonical JSON with UTF-8, sorted keys, compact separators, LF endings, and
one record per line.

`config/rag_demo.yaml` must bind these exact values:

```yaml
schema_version: "1.0"
rag_release_id: v1.2-demo
parent_release_manifest_sha256: e9842db33208e7146d78d86689b13f81fffcf41288d79d84e599b93050445e2c
expected_movies: 4658
expected_poster_rows: 4658
embedding_model: gemini-embedding-2
embedding_dimension: 768
generation_model: gemini-3.5-flash-lite
access_mode: restricted_demo
deep_documents:
  - movie_id: 1978_ZQ_001
    document_id: drunken-master-deep-analysis-v1
    source_filename: S 級港⽚-醉拳.pdf
    source_sha256: ab27baf8e22ceb63a476b65dbfb02951a63a9e9bf39d96a47cd8e3f9fd314a75
    rights_status: restricted
    quality_status: manual_approved
  - movie_id: 1982_ZJPD_001
    document_id: aces-go-places-deep-analysis-v1
    source_filename: S 級港⽚-最佳拍檔.pdf
    source_sha256: 69fa1ad28916c043689ef4fe826cde816c05a3b254e5a0372d1a57ca2ce63bce
    rights_status: restricted
    quality_status: manual_approved
```

The manifest schema requires the parent digest, bundle digest/size/counts, two document source
digests, model IDs, dimension, and `restricted_demo` access mode. Add `pypdf>=6,<7` to project
dependencies and expose the two CLI commands without changing existing command behavior.

- [ ] **Step 4: Run focused tests and verify GREEN**

Run: `uv run pytest tests/test_rag_bundle.py tests/test_staging.py tests/test_release_manifest.py -q`

Expected: all pass.

- [ ] **Step 5: Build and verify the real bundle twice**

Run both builds against `D:\VS_PROJECT\HK_Movie_KnowledgeGraph\RAG` into separate ignored
temporary directories, verify both manifests, and compare their bundle and manifest SHA-256.
Expected counts: 4,658 movies, 4,658 poster rows, two documents, six non-empty PDF passages.

- [ ] **Step 6: Commit Task 1**

Stage only config/schema/source/tests/lockfile/CLI changes; never stage the PDFs or generated
bundle. Commit: `feat: build deterministic minimum rag bundle`.

---

### Task 2: Add the pgvector schema and repository boundary

**Files:**
- Create: `migrations/0001_rag_demo.sql`
- Create: `src/hk_movie_rag/rag_db.py`
- Modify: `pyproject.toml`
- Test: `tests/test_rag_db.py`

**Interfaces:**
- Produces: `DatabaseSettings.from_env() -> DatabaseSettings` with secret-safe validation.
- Produces: `connect_db(settings: DatabaseSettings) -> psycopg.Connection` using a Cloud Run
  Unix socket when `INSTANCE_CONNECTION_NAME` exists and explicit host/port only for local tests.
- Produces: `RagRepository` methods `migrate()`, `begin_release()`, `upsert_bundle_records()`,
  `pending_passages()`, `store_embedding()`, `activate_release()`, `search()`,
  `get_poster_asset()`, and `release_stats()`.

- [ ] **Step 1: Write failing repository and migration contract tests**

Tests inspect the parsed migration statements and exercise repository behavior through a strict
recording connection double that mirrors psycopg cursor/transaction behavior:

```python
def test_migration_creates_vector_768_hnsw_and_all_six_tables(migration_sql):
    assert "CREATE EXTENSION IF NOT EXISTS vector" in migration_sql
    assert "embedding vector(768)" in migration_sql
    assert "USING hnsw" in migration_sql
    assert table_names(migration_sql) == {
        "rag_releases", "ingestion_runs", "movies", "media_assets",
        "movie_documents", "document_chunks",
    }

def test_activate_release_requires_exact_counts(repository):
    repository.record_stats(movies=4658, assets=4658, metadata=4657, documents=2, pdf=6)
    with pytest.raises(RagDatabaseError, match="metadata passages: expected 4658, got 4657"):
        repository.activate_release(EXPECTED_RELEASE)
```

- [ ] **Step 2: Run and verify RED**

Run: `uv run pytest tests/test_rag_db.py -q`

Expected: missing migration/repository imports.

- [ ] **Step 3: Implement schema and repository**

Use `psycopg[binary]>=3.2,<4` and `pgvector>=0.4,<1`. Enforce composite release/movie foreign
keys, deterministic primary keys, content-hash immutability, a partial primary-poster uniqueness
index, `vector(768)`, and HNSW cosine index. A release can move from `loading` to `active` only
after literal expected-count and embedding checks pass. SQL errors must roll back and be wrapped
without including passwords or full connection strings.

- [ ] **Step 4: Run focused tests and verify GREEN**

Run: `uv run pytest tests/test_rag_db.py -q`

Expected: all pass.

- [ ] **Step 5: Commit Task 2**

Commit: `feat: add pgvector rag repository`.

---

### Task 3: Implement resumable Vertex embedding ingestion

**Files:**
- Create: `src/hk_movie_rag/vertex_clients.py`
- Create: `src/hk_movie_rag/rag_ingest.py`
- Modify: `src/hk_movie_rag/cli.py`
- Modify: `pyproject.toml`
- Test: `tests/test_rag_ingest.py`

**Interfaces:**
- Produces protocol `EmbeddingClient.embed_document(text: str, *, title: str) -> list[float]` and
  `embed_query(text: str) -> list[float]`.
- Produces `VertexEmbeddingClient(project_id, location, model, dimension)` using `google-genai`.
- Produces `ingest_bundle(bundle, repository, embedding_client) -> IngestionSummary`.
- CLI commands: `hk-movie-rag ingest-rag-bundle gs://motionexpaiweb-880586285913-hk-movie-rag-release/rag/v1.2-demo/rag_release_manifest.json` and `hk-movie-rag rag-db-stats`.

- [ ] **Step 1: Write failing tests for resume, retry, and model invariants**

```python
def test_ingestion_resumes_only_missing_embeddings(verified_bundle, repository, embedding_client):
    repository.seed_embedded_passages(4650)
    result = ingest_bundle(verified_bundle, repository, embedding_client)
    assert result.embedded == 14
    assert embedding_client.document_calls == 14
    assert result.active is True

def test_ingestion_never_switches_model_after_first_embedding(...):
    repository.seed_release(model="gemini-embedding-2", embedded=1)
    with pytest.raises(IngestionError, match="embedding model is immutable"):
        ingest_bundle(bundle_with_model("gemini-embedding-001"), repository, fallback_client)
```

Include a transient 429/503 fixture that succeeds within five attempts and a permanent error
fixture that leaves the release `loading` with an error summary.

- [ ] **Step 2: Run and verify RED**

Run: `uv run pytest tests/test_rag_ingest.py -q`

Expected: missing ingestion/client modules.

- [ ] **Step 3: Implement minimal resumable ingestion**

Add `google-genai>=1.30,<2`. Use ADC and `genai.Client(vertexai=True, project=..., location="global")`.
Prepare document text as `title: ... | text: ...`; query format is reserved for Task 4. Validate
exactly 768 finite floats. Retry only 429, 500, 502, 503, and 504 with bounded exponential backoff
plus jitter. Persist each successful embedding immediately; an interrupted rerun selects only
missing or content/model-stale rows. Final activation uses the repository gate.

- [ ] **Step 4: Run focused tests and verify GREEN**

Run: `uv run pytest tests/test_rag_ingest.py tests/test_rag_db.py -q`

Expected: all pass.

- [ ] **Step 5: Run one live model preflight**

Against project `motionexpaiweb`, call `gemini-embedding-2` once at 768 dimensions. If and only if
the response is model unavailable/not found, call `gemini-embedding-001`, record the selected
model in the generated RAG manifest, rebuild the bundle, and rerun deterministic verification.
Do not begin bulk embedding until this decision is recorded.

- [ ] **Step 6: Commit Task 3**

Commit: `feat: ingest rag bundle with vertex embeddings`.

---

### Task 4: Add retrieval, grounded generation, and citation validation

**Files:**
- Create: `src/hk_movie_rag/rag_query.py`
- Test: `tests/test_rag_query.py`

**Interfaces:**
- Produces: `QueryService.answer(question: str) -> ChatAnswer`.
- `ChatAnswer` contains `answer_markdown`, validated `citations`, and deduplicated `movies` with
  same-origin poster route or null.
- Consumes: `RagRepository.search()`, `EmbeddingClient.embed_query()`, and protocol
  `GenerationClient.generate_grounded(question, passages) -> GeneratedAnswer`.

- [ ] **Step 1: Write failing grounding behavior tests**

```python
def test_answer_rejects_model_citation_not_in_retrieval(query_service):
    query_service.generator.answer = {
        "answer_markdown": "unsupported [P:missing]", "citation_ids": ["P:missing"]
    }
    with pytest.raises(GroundingError, match="unknown citation"):
        query_service.answer("問題")

def test_metadata_only_movie_does_not_gain_deep_analysis(query_service):
    result = query_service.answer("分析一部沒有深度文檔的電影美學")
    assert "目前只有結構化電影資料" in result.answer_markdown
    assert all(c.source_kind == "movie_metadata" for c in result.citations)
```

Also cover exact-title precedence, mixed Chinese/English questions, duplicate removal, top-eight
limit, empty/over-1,000-character rejection, one PDF page citation, and a cross-film answer.

- [ ] **Step 2: Run and verify RED**

Run: `uv run pytest tests/test_rag_query.py -q`

Expected: missing query service.

- [ ] **Step 3: Implement query and Vertex generation**

Create exact-ID/title matches first and fill remaining candidates with cosine search. Embed query
as the f-string `f"task: search result | query: {question}"`. The system prompt permits only supplied passages,
requires refusal when evidence is absent, and requests JSON schema
`{answer_markdown: str, citation_ids: list[str]}`. Use `gemini-3.5-flash-lite`, max 1,024 output
tokens, and no custom temperature/top-k/top-p. Reject unknown/duplicate citation IDs and build
links from repository data, never model output.

- [ ] **Step 4: Run focused tests and verify GREEN**

Run: `uv run pytest tests/test_rag_query.py tests/test_rag_ingest.py -q`

Expected: all pass.

- [ ] **Step 5: Commit Task 4**

Commit: `feat: answer grounded movie questions with citations`.

---

### Task 5: Build the restricted same-origin demo application

**Files:**
- Create: `src/hk_movie_rag/demo_auth.py`
- Create: `src/hk_movie_rag/demo_api.py`
- Create: `src/hk_movie_rag/static/index.html`
- Create: `src/hk_movie_rag/static/app.js`
- Create: `src/hk_movie_rag/static/styles.css`
- Modify: `pyproject.toml`
- Test: `tests/test_demo_api.py`

**Interfaces:**
- ASGI entrypoint: `hk_movie_rag.demo_api:create_app` and module-level `app`.
- Required environment: project/model/DB/bucket settings and `RAG_DEMO_ACCESS_KEY`; a
  domain-separated HMAC key for session cookies is derived from the demo key and neither value
  is ever returned or logged.
- Public: `/`, `/health`, `/api/session`; authenticated: `/api/config`, `/api/chat`,
  `/api/posters/{movie_id}`.

- [ ] **Step 1: Write failing API and poster tests**

Using FastAPI TestClient with real route handling and injected repository/query/storage fakes:

```python
def test_chat_and_poster_require_demo_session(client):
    assert client.post("/api/chat", json={"question": "醉拳導演是誰？"}).status_code == 401
    assert client.get("/api/posters/1978_ZQ_001").status_code == 401

def test_authenticated_chat_returns_citations_and_same_origin_poster(client, login):
    login("correct-demo-code")
    response = client.post("/api/chat", json={"question": "醉拳的視覺美學？"})
    assert response.status_code == 200
    assert response.json()["movies"][0]["poster_url"] == "/api/posters/1978_ZQ_001"

def test_poster_proxy_rejects_conflict_and_never_accepts_object_path(client, login):
    login("correct-demo-code")
    assert client.get("/api/posters/conflicted_movie").status_code == 404
    assert client.get("/api/posters/../../secret").status_code in {404, 422}
```

- [ ] **Step 2: Run and verify RED**

Run: `uv run pytest tests/test_demo_api.py -q`

Expected: missing demo API/auth modules.

- [ ] **Step 3: Implement the access gate, API, and poster proxy**

Add `fastapi>=0.116,<1`, `uvicorn[standard]>=0.35,<1`, and `httpx>=0.28,<1` for tests. Compare
the submitted code with `hmac.compare_digest`; set an HMAC-signed expiring cookie that is Secure,
HttpOnly, SameSite=Strict, and scoped to `/`. Stream only the exact stored private GCS object for
machine/manual-approved assets, preserve MIME and ETag, use `Cache-Control: private, no-store`,
and include `X-RAG-Rights-Status`.

- [ ] **Step 4: Implement the minimal UI**

The first screen requests the demo code. The chat screen contains example questions, question
input, loading/error states, answer Markdown rendered as text-safe paragraphs, citation cards,
movie metadata, and poster images using only same-origin API paths. It must work at 360 px and
desktop width, have keyboard labels/focus visibility, and display a visible “restricted technical
demo; poster rights not cleared for public publication” notice.

- [ ] **Step 5: Run focused tests and local browser smoke**

Run: `uv run pytest tests/test_demo_api.py tests/test_rag_query.py -q`.
Then start Uvicorn with injected fake/offline dependencies, open the UI, and verify login, one
answer, citations, poster success, missing poster, logout, mobile width, and no console errors.

- [ ] **Step 6: Commit Task 5**

Commit: `feat: add access controlled rag chatbot demo`.

---

### Task 6: Containerize and make GCP deployment reproducible

**Files:**
- Create: `Dockerfile`
- Create: `.dockerignore`
- Create: `scripts/gcp/provision-demo.ps1`
- Create: `scripts/gcp/deploy-demo.ps1`
- Create: `scripts/gcp/live-smoke.py`
- Create: `tests/test_gcp_demo_scripts.py`
- Modify: `.env.example`
- Modify: `README.md`

**Interfaces:**
- `provision-demo.ps1 -ProjectId motionexpaiweb -Region us-central1 -PlanOnly` emits deterministic
  JSON describing enabled APIs and exact resources; `-Apply` performs create-if-absent operations.
- `deploy-demo.ps1` uploads the verified bundle and poster allowlist, builds one image, deploys
  the Job/service, executes ingestion, and emits the service URI/revision/image digest.
- `live-smoke.py --base-url ... --access-code-env RAG_DEMO_ACCESS_KEY` verifies the deployed
  contract without printing the access code.

- [ ] **Step 1: Write failing plan-only deployment contract tests**

```python
def test_provision_plan_has_exact_region_and_never_creates_a_key(run_plan):
    plan = run_plan("scripts/gcp/provision-demo.ps1")
    assert plan["region"] == "us-central1"
    assert plan["cloud_sql_instance"] == "hk-movie-rag-pg"
    assert all("keys create" not in command for command in plan["commands"])
```

- [ ] **Step 2: Run and verify RED**

Run: `uv run pytest tests/test_gcp_demo_scripts.py -q`

Expected: missing scripts and Dockerfile.

- [ ] **Step 3: Implement container and idempotent scripts**

Use Python 3.13 slim, non-root runtime user, locked `uv sync --frozen --no-dev`, port `$PORT`, and
no source data or credentials in the image. The actual container behavior is verified by the
local/Cloud Build image build and health smoke, not by grepping Dockerfile text. Provision APIs `run`, `cloudbuild`, `artifactregistry`,
`secretmanager`, `sqladmin`, `aiplatform`, and `storage`; create the exact design resources only
when absent; create random DB/demo secrets without echoing values; derive the cookie HMAC key
inside the application using domain separation; grant the runtime
service account only the scoped roles in the design.

`deploy-demo.ps1` must:

1. verify the local RAG manifest/bundle;
2. upload bundle/manifest and 4,545 derived poster objects to the private release bucket;
3. set `$gitSha = git rev-parse --short=12 HEAD` and build/push `us-central1-docker.pkg.dev/motionexpaiweb/hk-movie-rag/demo:$gitSha`;
4. deploy/update `hk-movie-rag-ingest` attached to Cloud SQL and secrets;
5. execute the Job and wait for success;
6. deploy/update public-at-IAM `hk-movie-rag-demo`, with app-level access-code protection;
7. output JSON with the URL, revision, image, Job execution, and no secret values.

- [ ] **Step 4: Implement live smoke behavior**

The smoke script checks public health, rejects unauthenticated chat/poster, logs in with an env-only
code, asserts `/api/config` counts/model, asks one metadata question and one question for each PDF,
checks citation IDs/page numbers, fetches both posters as images, and logs out. It exits nonzero on
any mismatch and emits a redacted JSON report.

- [ ] **Step 5: Run tests, lint, type checks, and local container smoke**

Run:

```powershell
uv run pytest tests/test_rag_bundle.py tests/test_rag_db.py tests/test_rag_ingest.py tests/test_rag_query.py tests/test_demo_api.py tests/test_gcp_demo_scripts.py -q
uv run ruff check src tests scripts
uv run mypy src
docker build -t hk-rag-demo:local .
```

If Docker is unavailable locally, record that environment limitation and rely on Cloud Build as
the required image-build gate; do not mark image verification complete until Cloud Build succeeds.

- [ ] **Step 6: Update README and commit Task 6**

README must explain the minimum slice, exact data-depth boundary, commands, environment, GCP
resources, cost/cleanup implications, access-code behavior, poster restriction, and live acceptance.
Commit: `feat: package and deploy minimum vertex rag demo`.

---

### Task 7: Provision GCP, ingest, deploy, and prove the live link

**Files:**
- Generated, ignored: `artifacts/rag-demo/v1.2-demo/*`
- Generated, ignored: `artifacts/rag-demo/live-verification.json`
- No additional source file is authorized unless a failing live test first proves a defect.

**Interfaces:**
- Consumes the exact scripts and verified bundle from Tasks 1–6.
- Produces the final Cloud Run HTTPS URL plus redacted resource/ingestion/verification evidence.

- [ ] **Step 1: Run full pre-provision verification**

Run the existing Release verifier against the source workspace, build/verify the real RAG bundle,
run all new unit tests, existing source-independent tests, Ruff, and mypy. Record the known isolated
worktree fixture limitation separately; never rewrite Phase 1 tests to hide missing ignored inputs.

- [ ] **Step 2: Apply provisioning and inspect every created resource**

Run `provision-demo.ps1 -Apply`, then use read-only `gcloud ... describe` commands to verify project,
region, APIs, Cloud SQL engine/tier/deletion protection, private bucket controls, service-account
roles, secret references, and Artifact Registry. Stop on any name/region/IAM drift.

- [ ] **Step 3: Deploy and execute ingestion**

Run `deploy-demo.ps1`, wait for Cloud Build, Cloud Run Job, and Cloud Run service readiness. Query
database stats through the Job command and require 4,658 movies, 4,658 assets, 4,658 metadata
passages, two documents, six PDF passages, zero null/wrong embeddings, and active release.

- [ ] **Step 4: Prove idempotency**

Execute the same ingestion Job again. Require identical content counts, release/bundle digests,
and zero newly embedded passages. A rerun that re-embeds or changes counts is a failed acceptance.

- [ ] **Step 5: Run API and browser acceptance**

Run `live-smoke.py` and inspect its redacted JSON. Then use a browser against the actual Cloud Run
URL to verify the login screen, example questions, Traditional Chinese answer, citations, both
posters, unsupported deep-question refusal, mobile rendering, network errors, and console errors.

- [ ] **Step 6: Run the full verification gate**

Fresh commands immediately before completion:

```powershell
uv run pytest -q
uv run ruff check .
uv run mypy src
$demoUrl = gcloud run services describe hk-movie-rag-demo --project motionexpaiweb --region us-central1 --format='value(status.url)'
python scripts/gcp/live-smoke.py --base-url $demoUrl --output artifacts/rag-demo/live-verification.json
gcloud run services describe hk-movie-rag-demo --project motionexpaiweb --region us-central1 --format=json
gcloud run jobs executions list --job hk-movie-rag-ingest --project motionexpaiweb --region us-central1 --limit=2 --format=json
```

Interpret the full pytest result honestly: the isolated worktree may still report only the
pre-existing missing-ignored-source fixture failures. All new tests and every live gate must pass.

- [ ] **Step 7: Commit evidence-safe documentation only**

Do not commit secrets, generated bundles, raw PDFs, posters, database dumps, or access codes. If
README needs the final resource names/URL contract, commit that change as
`docs: record minimum rag demo operations`.

- [ ] **Step 8: Final review and delivery**

Run a whole-branch code review, fix all Critical/Important findings, rerun live verification after
the final image revision, and only then deliver the HTTPS link plus the separately stated demo
access code and limitations.
