# Minimum Verifiable Hong Kong Movie RAG Demo Design

**Date:** 2026-07-30

**Status:** Approved in conversation on 2026-07-30; implementation authorized

## Objective

Deliver one access-controlled Cloud Run link that proves a usable RAG database exists for
the 4,658 formal Release v1.2 movies. Every movie is retrievable through one deterministic
metadata passage and is joined to its governed poster state. Exactly two user-provided PDF
analyses add page-addressable deep knowledge for `1978_ZQ_001` (醉拳) and
`1982_ZJPD_001` (最佳拍檔).

The demo must answer with Vertex AI, cite only retrieved passages, and return a related
poster through a private same-origin proxy when a technically valid asset exists. It is a
minimum verifiable product slice, not a production-readiness claim.

## Scope

### Required

- Preserve the Phase 1 Release v1.2 six-artifact manifest unchanged.
- Bind a new RAG release manifest to the exact SHA-256 of the Phase 1 manifest.
- Load exactly 4,658 movie rows and 4,658 poster-state rows.
- Generate exactly one metadata passage and one 768-dimensional embedding per movie.
- Extract exactly the non-empty pages of the two declared PDFs and retain PDF page citations.
- Store movies, assets, documents, passages, vectors, and ingestion-run state in Cloud SQL
  PostgreSQL with `pgvector`.
- Retrieve from PostgreSQL, generate with Vertex AI, validate citation IDs server-side, and
  display answers, citations, movie metadata, and restricted poster images in one small web UI.
- Deploy the API and UI as one Cloud Run service and ingestion as one Cloud Run Job.
- Protect the demo with one access code stored in Secret Manager. This is an operational
  demo gate, not an end-user authentication system.

### Explicitly deferred

- MotionExpert frontend, backend, MySQL, corpus metadata, and authentication.
- Deep documents for the other 4,656 movies.
- A statistically meaningful 768-vs-1,536 evaluation; two PDFs can prove the pipeline but
  cannot choose an embedding dimension on quality evidence.
- Public poster publication, CDN delivery, rights adjudication, multitenancy, billing,
  conversation persistence, reranking, and production SLOs.
- Terraform conversion. The minimum deployment is recorded as explicit, idempotent `gcloud`
  operations; infrastructure-as-code can follow after the demo proves product value.

## Authorized inputs

The existing `data/release/v1.2/release_manifest.json` remains the only authority for the
canonical movie, tier, pilot, provenance, and poster-state artifacts. Its current SHA-256 is:

```text
e9842db33208e7146d78d86689b13f81fffcf41288d79d84e599b93050445e2c
```

The deep-document smoke set is independent of the 24-movie pilot cohort:

| Movie | movie_id | PDF SHA-256 | Release pilot |
|---|---|---|---|
| 醉拳 | `1978_ZQ_001` | `ab27baf8e22ceb63a476b65dbfb02951a63a9e9bf39d96a47cd8e3f9fd314a75` | yes |
| 最佳拍檔 | `1982_ZJPD_001` | `69fa1ad28916c043689ef4fe826cde816c05a3b254e5a0372d1a57ca2ce63bce` | no |

No filename or title fuzzy match can authorize a document. The committed demo configuration
binds each filename, exact digest, and movie ID. The builder resolves movie identity against
the verified 4,658-row parent release before reading PDF content.

## Architecture

```text
verified Release v1.2 manifest + six artifacts
                         +
two hash-bound PDF analyses + governed poster derivatives
                         |
                  deterministic bundle
                         |
             private GCS demo-release bucket
                         |
                 Cloud Run ingestion Job
                         |
        Cloud SQL PostgreSQL 16 + pgvector(768)
                         |
 question -> Cloud Run API -> exact-title/vector retrieval
                         |                  |
                         |             private poster proxy
                         v
              Vertex Gemini grounded answer
                         |
               answer + citations + poster
```

The GCP region is `us-central1`, matching the current project resources and source archive.
Vertex model calls use the global endpoint.

## RAG release and bundle

`config/rag_demo.yaml` declares the exact parent manifest digest, document bindings, model
defaults, counts, and access mode. `build-rag-bundle` performs these steps in order:

1. Run the existing Release verifier against the caller-supplied Phase 1 manifest.
2. Require 4,658 unique movies and 4,658 poster-state rows from manifest-listed artifacts.
3. Render one deterministic Traditional Chinese metadata passage from the 12 canonical fields.
4. Verify both PDF byte digests before extraction.
5. Extract text per page, normalize line endings and whitespace deterministically, and skip
   only empty pages. Each non-empty PDF page becomes one deep passage.
6. Emit canonical JSON Lines ordered by record kind, movie ID, document ID, and page.
7. Write a RAG release manifest containing the parent digest, bundle size/SHA-256/counts,
   document source digests, and selected model configuration.

The builder never recursively scans the workspace. Repeating it over unchanged inputs must
produce byte-identical bundle and manifest bytes.

## Database

PostgreSQL owns six tables:

- `rag_releases`: parent and bundle digests, expected counts, model identity, and state.
- `ingestion_runs`: one resumable execution record with counters and failure detail.
- `movies`: the 4,658 authoritative metadata rows for a RAG release.
- `media_assets`: one governed poster-state row per movie and the private release-bucket key.
- `movie_documents`: the two versioned PDF source records.
- `document_chunks`: 4,658 metadata passages plus page-addressable PDF passages and
  `vector(768)` embeddings.

All child rows reference `(rag_release_id, movie_id)`. Passage IDs and content hashes are
deterministic. Upserts may repair an interrupted run but may not change an existing identity to
different content. The RAG release becomes `active` only after the expected counts, foreign keys,
non-null embeddings, and model/dimension invariants pass in one final transaction.

The first demo deliberately uses one application database credential for both the ingestion Job
and query service. This is a minimum-slice compromise recorded in the health metadata; split
read/write database roles are required before production.

## Vertex models and retrieval

- Primary embedding model: `gemini-embedding-2`, output dimension `768`.
- Deterministic fallback: `gemini-embedding-001`, also at `768`, only when the primary live
  smoke returns a model-not-found/unavailable response before any passage is embedded.
- Generation model: `gemini-3.5-flash-lite`.
- Retrieval limit: eight passages.

Documents use `title: <title> | text: <body>`. Queries use
`task: search result | query: <question>`. The selected embedding model is stored on the active
RAG release; query-time embeddings must use the same model and dimension.

Retrieval first includes exact movie-ID/title matches present in the question, then fills the
remaining slots by cosine similarity. Duplicate passage IDs are removed while preserving exact
matches first.

Gemini receives numbered immutable passage IDs. It must return JSON containing
`answer_markdown` and `citation_ids`. The API rejects citation IDs that are absent from the
retrieved set. When retrieved evidence is insufficient, the answer states that only metadata or
no deep analysis is available.

## Restricted poster delivery

Release v1.2 poster rights are `unknown`, so the application does not publish bucket URLs or
claim that an asset is production-publishable. The access-code-protected route
`GET /api/posters/{movie_id}` resolves only a stored movie ID, accepts no caller-controlled object
path, and proxies only `machine_passed` or `manual_approved` private objects. Responses use
`Cache-Control: private, no-store` and include the governed rights state. Missing, conflicted,
placeholder, or quarantined assets return `404`.

This restricted demo behavior does not mutate `rights_status` or `publishable` in Release v1.2.

## API and UI

- `GET /health`: public liveness only; no database or secret details.
- `POST /api/session`: accepts the demo code and sets a Secure, HttpOnly, SameSite=Strict cookie.
- `DELETE /api/session`: removes the cookie.
- `GET /api/config`: authenticated, returns active release/model/count metadata without secrets.
- `POST /api/chat`: authenticated, validates a bounded question, retrieves, generates, and returns
  answer/citations/movie cards.
- `GET /api/posters/{movie_id}`: authenticated private proxy.
- `GET /`: minimal responsive chat UI served by the same process.

The cookie is an HMAC-signed, expiring session marker derived from the Secret Manager access
code; the code itself is never returned to JavaScript or logs. Questions are limited to 1,000
Unicode characters and output to 1,024 model tokens.

## GCP resources

Exact demo resource names:

- Project: `motionexpaiweb`
- Region: `us-central1`
- Cloud SQL: `hk-movie-rag-pg`, PostgreSQL 16, `db-g1-small`, 10 GB SSD, zonal
- Database/user: `hk_movie_rag` / `hk_rag_app`
- Release bucket: `motionexpaiweb-880586285913-hk-movie-rag-release`
- Artifact Registry: `hk-movie-rag`
- Runtime service account: `hk-rag-runtime@motionexpaiweb.iam.gserviceaccount.com`
- Secrets: `hk-rag-db-password`, `hk-rag-demo-key`
- Cloud Run service: `hk-movie-rag-demo`
- Cloud Run Job: `hk-movie-rag-ingest`

The bucket is private with uniform access and public-access prevention. The service account gets
only Vertex user, Cloud SQL client, bucket object viewer, and accessor on the two named secrets.
The human operator performs bundle/poster upload and infrastructure creation with existing owner
authority. No service-account key file is created.

## Failure behavior

- Parent manifest or artifact mismatch: stop before bundle publication.
- PDF digest, movie membership, extraction, or count mismatch: stop before GCS upload.
- Embedding throttling/transient error: retry with bounded exponential backoff; keep the release
  non-active and resume only missing/stale passages on rerun.
- Model fallback: permitted only before the first stored embedding for that release.
- Database conflict with different content for the same identity: fail the run; never overwrite.
- Gemini failure or invalid citations: return a controlled error without an uncited answer.
- Poster state or object mismatch: return `404`; never substitute another movie's image.
- Cloud deployment smoke failure: keep the URL but report it as not delivered; do not claim demo
  completion.

## Acceptance

The final link is deliverable only when fresh evidence proves:

- 4,658 `movies`, 4,658 `media_assets`, and 4,658 metadata passages.
- Exactly two `movie_documents` and six non-empty PDF page passages for the current files.
- Zero active passages with a null embedding, wrong dimension, or wrong model.
- A second ingestion execution changes no content counts or hashes.
- Exact-title/ID checks retrieve the correct movie in top five for all 24 pilot records.
- A metadata question, one deep question per PDF, one cross-film comparison, and one unsupported
  deep question return the expected grounded behavior.
- Every returned citation ID belongs to the retrieved passage set.
- Both known poster routes return the correct image MIME/hash through the authenticated proxy.
- Unauthenticated chat/poster access is rejected.
- Cloud Run `/health`, authenticated `/api/config`, browser login/chat/poster rendering, and live
  Vertex generation all pass against the deployed revision.
