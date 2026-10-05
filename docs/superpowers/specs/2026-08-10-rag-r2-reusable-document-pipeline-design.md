# RAG-R2 Reusable Document Pipeline Design

## Outcome

Publish a new immutable child RAG release, `v1.2-demo-r2`, that preserves all 4,658
governed movies, adds the six-page `富貴逼人` analysis as the third deep document,
reuses only provably identical embeddings, and serves the upgraded corpus through the
existing Cloud Run chatbot URL after candidate-revision acceptance.

The release must verify these exact totals:

- 4,658 movies, poster rows, facet rows, and metadata passages;
- 50 S-tier, 313 A-tier, 4,295 B-tier, and 24 pilot facet memberships;
- three deep documents and 12 page-addressable PDF passages;
- 4,670 embeddings;
- 4,545 approved private poster objects and 113 deliberately unavailable posters;
- unchanged 24-movie pilot membership;
- `gemini-embedding-2` at 768 dimensions and `gemini-3.5-flash-lite` generation.

The existing active `v1.2-demo` release and its Cloud Run revision remain rollback
authorities until the new revision passes all acceptance gates.

## Selected approach

Three approaches were considered.

1. Replacing every `2/6/4664` literal with `3/12/4670` is a one-release patch that
   repeats on the next document and risks readiness drift.
2. A versioned manifest contract, DB-bound manifest identity, and exact cross-release
   vector reuse make document addition a reusable release operation. This is selected.
3. A global embedding-cache table would generalize beyond releases but adds lifecycle,
   eviction, and multi-model concerns that the current corpus does not need.

The selected design keeps fail-closed behavior while centralizing release-specific
authority in the verified manifest and runtime contract rather than module constants.

## Source and document authority

The raw source is bound as follows:

```text
movie_id: 1987_FGBR_001
document_id: its-a-mad-mad-mad-world-deep-analysis-v1
source_filename: S級港片-富貴逼人.pdf
source_sha256: 5aef7f8684bc0d91d77ffb6d7f89cb323358e2804ef8eaacbc0a02cfeb3d3df4
rights_status: restricted
quality_status: manual_approved
page_count: 6
```

The 86,488,712-byte raw PDF remains outside Git and the Cloud Build image. Bundle build
accepts a separate `--document-source-root`; deployment exposes the same boundary as
`-DocumentSourceRoot`. The path is resolved independently from the clean code/release
root, but each configured filename must remain directly beneath that document root and
must match its declared hash. Only canonical page text enters the RAG bundle. R2 does
not upload the raw PDF or widen the runtime service account's source access. Extending
the separately governed source archive is a later archive operation, not an implicit
side effect of publishing this chatbot release.

`quality_status=manual_approved` authorizes use in the access-controlled demo, not
independent verification of every analytical claim. PDF-derived answers must attribute
interpretive or otherwise non-canonical statements to the analysis. The PDF filename's
`S級` label does not alter the canonical B tier, and PDF credits or runtime never
overwrite the canonical movie record.

## Deterministic text extraction

Add extraction profile `cjk-layout-v1` to remove layout whitespace between CJK
characters and punctuation while preserving:

- blank-line paragraph boundaries;
- meaningful ASCII word spacing;
- English titles, URLs, and timestamps;
- exact PDF page numbers and deterministic page hashes.

The same profile applies to all three deep PDFs. Consequently all 12 PDF page
embeddings are generated for R2, while unchanged metadata embeddings remain reusable.
Every physical page must contain non-empty normalized text and page numbers must be the
exact contiguous set `1..page_count`. Empty documents or pages, duplicate document IDs,
duplicate source bindings, duplicate passage IDs, duplicate page numbers, and declared
page-count drift fail bundle construction.

## Versioned manifest contract

Historical schema `1.1` retains its exact two-document meaning and remains verifiable.
New schema `1.2` supports a positive document count and adds:

- `page_count` per document;
- `text_extraction_profile`;
- `document_embedding_profile=vertex-title-text-v1`;
- relevance-policy and poster-authority SHA-256 identities.

Python verification enforces these manifest relationships and derives expected
embeddings as a `RagReleaseContract` property rather than another manifest field:

```text
counts.documents == len(documents)
counts.pdf_passages == sum(document.page_count)
counts.metadata_passages + counts.pdf_passages == expected embeddings
```

`verify_rag_bundle()` returns one `RagReleaseContract` containing schema version,
release ID, manifest and bundle hashes, counts, models, profiles, access mode, and policy
digests. DB ingestion, API readiness, deployment, and smoke consume this contract.
Unknown schema versions or inconsistent identities fail before database writes.

Keep `config/rag_demo.yaml` as the historical reproduction config. Add
`config/rag_demo_r2.yaml` for the new child release. The new GCS prefix is derived from
the validated release identity:

```text
rag/v1.2-demo-r2/
```

Both manifest and bundle uploads remain create-only with generation-match zero. An
existing identical object is idempotent; any different byte or metadata identity fails.

## Database identity and embedding reuse

Existing release-scoped tables already support parallel releases. Add a migration that
stores immutable `manifest_sha256` and embedding-input profile on `rag_releases`; it does
not change pgvector dimension or rebuild Cloud SQL. The migration permits these fields
to be empty only on pre-migration legacy rows. Before reuse, deployment downloads and
verifies the exact active immutable manifest object, locks the legacy `v1.2-demo` row,
and makes its one allowed null-to-verified identity claim. No local digest is guessed.
After that transition, triggers reject identity changes; all new release rows require
both fields at insert time.

`begin_release()` inserts the verified contract's actual counts rather than global
literals. Reusing a release ID with a different manifest hash, counts, model, dimension,
or profile is rejected. Query readiness receives the expected manifest hash and compares
actual row counts to the expectations stored for that exact release.

`reuse_embeddings(target, source)` copies vectors inside PostgreSQL only when:

- source is active and target is loading;
- model, dimension, and embedding-input profile match;
- passage ID, movie ID, kind, document ID, page, body, content hash, and canonical title
  all match;
- source vector is non-null and target vector is null.

For a clean first R2 run the expected deltas are 4,658 copied metadata embeddings and 12
new Vertex document embeddings. To support interrupted resume, acceptance is based on
final corpus state: exactly 4,658 target vectors must remain strictly source-eligible and
equal to the active source vectors, and exactly 12 populated vectors must be non-reusable
R2 PDF passages. Reports separately expose `preexisting`, `copied_this_run`,
`embedded_this_run`, and `skipped_this_run`; they never pretend a resumed run copied all
4,658 rows. A completed active rerun performs a full byte-bound bundle comparison, makes
zero Vertex calls, and reports 4,670 skipped passages. Activation requires 4,670 vectors
and exact movie, poster, facet, document, and passage counts.

## Query correctness

Exact canonical metadata intent takes precedence over vector evidence even when the
movie has a deep PDF. For example, asking for `富貴逼人` cast or runtime must return
`metadata:1987_FGBR_001`, the canonical 100-minute record, and never the PDF's differing
credits or 99-minute statement. A mixed request such as “片長和視覺” uses two evidence
branches: canonical fields come only from metadata, qualitative analysis comes only
from that movie's PDF, and the final answer must cite both authorities.

Named qualitative questions search only that movie's governed PDF pages. Follow-up
history continues to use the existing bounded four-exchange client-supplied context.
PDF answers retain page citations and movie cards; interpretive claims are phrased as
the supplied analysis's view rather than independently verified authorial intent. The
existing poster row supplies `assets/posters/derived/1987_FGBR_001.webp` through the
authenticated private proxy.

Corpus-scope prose must describe the evidence used for the question, not hard-code the
total number of deep documents. Broad conclusions continue to disclose limited evidence
and cannot generalize one film to an entire era or genre.

## Relevance and poster authorities

Publish a release-specific R2 relevance policy artifact and run its existing 40-case
domain gate unchanged. Keep conversational deep-answer cases in a separate governed
JSONL artifact with their own citation, attribution, context, and poster pass criteria;
the policy loader selects both artifacts by exact release and digest.

Publish poster-serving authority v2 at the parent dataset/inventory scope: it binds the
raw `data/release/v1.2/release_manifest.json` SHA-256
`e9842db33208e7146d78d86689b13f81fffcf41288d79d84e599b93050445e2c`, derived inventory
SHA-256 `b754727129bc044b6c9da1e6db22067c8279c5e7f8dd41d0350e365376538154`,
private same-origin delivery, operator permission, and rights boundary. It does not make
unchanged poster bytes depend on a RAG document count. Historical poster authority v1
remains valid only for the old release.

Poster-fleet verification consumes the verified release contract instead of global
release/count constants.

## Deployment and traffic safety

The deployment command accepts an explicit RAG config, document-source root, and reuse
source. It builds and verifies the bundle locally, uploads create-only objects, applies
migrations, ingests and activates R2, verifies all poster objects, and deploys an
immutable-image candidate with zero traffic.

The candidate revision receives a temporary tag URL. The access code is passed only
through the existing secret/environment boundary and never command output. Authentication, config identity, counts,
metadata priority, all three deep-document citations, `富貴逼人` page 1-6 authority,
follow-up context, recommendations, and poster bytes are tested against that tag before
traffic promotion. Failure leaves the old revision at 100% traffic. Only a passing
candidate is promoted to the stable service URL; the same stable URL is smoked again
after promotion. The temporary tag is removed by a conditional traffic update after
success, failure, or rollback so an unaccepted candidate never remains directly
addressable.

Live smoke reads document/page authority and test cases from the verified R2 manifest
and a governed deep-case JSONL file. Citation syntax permits any positive page number,
but acceptance requires membership in the exact manifest page set.

## Acceptance cases

At minimum the deployed candidate must prove:

1. `《富貴逼人》的視覺風格與空間設計？` returns only that movie, an R2 PDF page
   citation, and its poster card.
2. `那它的聲音設計呢？` preserves the target and cites page 5.
3. `《富貴逼人》的主演和片長？` cites only canonical metadata and returns the
   100-minute governed record.
4. `《富貴逼人》的片長和視覺風格？` cites metadata for runtime and PDF evidence
   for analysis without blending their authority.
5. An interpretive question explicitly attributes disputed symbolism or creative intent
   to the supplied analysis.
6. A bounded broad question discloses that its conclusion uses a limited set of deep
   documents.
7. The poster endpoint returns authenticated WebP bytes whose size and SHA match the
   authority.
8. Unauthorized config, chat, and poster requests remain rejected; logout revokes the
   session.

## Out of scope

- Changing the 4,658-movie canonical dataset or 24-film pilot membership.
- A Cloud SQL rebuild, pgvector dimension change, or embedding-model evaluation.
- OCR, PDF image retrieval, raw-PDF GCS archival, public GCS/CDN access, or end-user
  identity authorization.
- A general CMS or staging-to-publication workflow.
- Fact-checking every interpretive statement in the supplied restricted analysis.

## Failure and rollback semantics

- Source, page count, schema, authority, or manifest drift stops before ingestion.
- Unsafe reuse stops before any Vertex call; individual non-matching passages remain
  pending and are newly embedded only when the overall expected reuse contract permits.
- Incomplete counts keep R2 loading and unavailable to queries.
- Candidate smoke failure leaves the old active revision serving all traffic.
- Promotion smoke failure triggers immediate traffic rollback to the recorded old
  revision; no database release or immutable GCS object is deleted.
