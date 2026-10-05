# RAG R3 Supplemental Movie And Two-PDF Design

## Outcome

Publish an immutable child RAG release, `v1.2-demo-r3`, that keeps the governed
Release v1.2 source package unchanged while making five deep-analysis documents
and 4,659 movies queryable in the GCP chatbot. The two newly accepted documents
are for `1985_JSXS_001` (`殭屍先生`) and the supplemental movie
`2001_SLZQ_001` (`少林足球`).

R3 is an additive RAG product release, not a rewritten source archive. Its
manifest must prove that 4,658 movie rows came from the immutable v1.2 parent and
that exactly one independently sourced movie row was added by an explicit,
auditable overlay.

## Fixed Release Contract

| Property | R2 parent | R3 target |
| --- | ---: | ---: |
| Searchable movies | 4,658 | 4,659 |
| Deep documents | 3 | 5 |
| PDF passages | 12 | 21 |
| Embedding rows | 4,670 | 4,680 |
| Reusable embedding rows | n/a | 4,670 |
| Newly embedded rows | n/a | 10 |
| S / A / B tiers | 50 / 313 / 4,295 | 51 / 313 / 4,295 |
| Pilot movies | 24 | 24 |

The R3 manifest schema is `hk_movie_rag_release_manifest/1.3`. Existing 1.1 and
1.2 bundles remain valid and are not mutated.

## Source And Identity Boundaries

### Governed parent

- Parent source release: `FinalDelivery_2026-07-16/Release`
- Parent movie count: exactly 4,658
- Parent manifest SHA-256: the existing value already bound in
  `config/rag_demo_r2.yaml`
- R2 RAG release: `v1.2-demo-r2`; its GCS objects and database rows are read-only

### New deep documents

- `S 級港⽚-殭屍先生.pdf`
  - movie: `1985_JSXS_001`
  - document id: `mr-vampire-deep-analysis-v1`
  - expected SHA-256:
    `2dce5ec8a1459999c53e9a56f603225e3970061cc7d2f7a35b8d53388096e576`
  - expected pages/passages: 4
- `S 級港⽚-少林足球.pdf`
  - movie: `2001_SLZQ_001`
  - document id: `shaolin-soccer-deep-analysis-v1`
  - expected SHA-256:
    `c6f713e2f5c60381d7b8745829f89680ebcfcb631c24d86814a9c6dfcd49e863`
  - expected pages/passages: 5

All five PDFs are copied to an external, immutable input root. Build
configuration records exact filenames, hashes, extraction profile and page
counts. Extraction remains `pypdf-pages-v1` plus `cjk-layout-v1`; an empty page,
hash mismatch or count mismatch fails the build.

### Supplemental movie

`2001_SLZQ_001` is declared in the R3 configuration and emitted into the normal
movie, poster and facet record shapes:

```yaml
movie_id: 2001_SLZQ_001
chinese_title: 少林足球
english_title: Shaolin Soccer
release_date: 2001-07-12
production_region: 香港
director: 周星馳
screenwriter: 周星馳、曾謹昌、馮勉恆、馮志強
cast: 周星馳、趙薇、吳孟達、黃一飛
genre: 動作、喜劇、運動
runtime_minutes: 112
production_company: 星輝海外有限公司、寰宇娛樂有限公司
data_source: Wikidata + 香港電影資料館 + R3 operator approval
```

The configuration also declares tier `S`, `human_review: manual_approved`, and
`pilot_movie: false`. This is an explicit R3 product-classification decision;
the tier is not inferred from the PDF filename.

Every supplemental movie must have exactly the twelve canonical movie fields,
a unique movie id, a non-empty provenance string, one facet declaration and one
poster declaration. A collision with a parent movie id, duplicate supplemental
id, missing field, invalid date/runtime, missing facet or missing poster must
fail closed before bundle output is written.

## Poster Contract

The parent 4,658-row poster inventory remains untouched. R3 adds one restricted
poster asset for `2001_SLZQ_001`, derives a WebP under
`assets/posters/derived/2001_SLZQ_001.webp`, and records source URL, fetch time,
source SHA-256, derived SHA-256, byte size, dimensions and access policy in the
R3 poster overlay.

The serving policy remains same-origin proxy only; raw GCS URIs are not exposed
to the browser. If the source cannot be fetched or a valid WebP cannot be
derived, the R3 build and deployment stop rather than silently publishing a
broken card. Target poster counts are 4,659 rows, 4,546 approved assets and 113
unavailable assets.

## Bundle And Verification

Schema 1.3 adds these signed manifest properties:

- `parent_rag_release_id: v1.2-demo-r2`
- `supplemental_movies`: canonical identity and provenance for exactly one row
- `supplemental_facets`: exactly one facet row
- `supplemental_posters`: exactly one poster asset identity
- facet artifact bindings for the 4,658-row parent plus supplemental counts
- the existing extraction, chunking, embedding, retrieval, reranking, access,
  poster-authority and prompt-policy identities

Verification recomputes all record counts and content identities. It accepts
the facet total as `parent_artifact_rows + supplemental_rows`, while schema 1.2
continues to require the artifact row count to equal the target directly.

The bundle contains normal records only; runtime query code does not need a
special path for supplemental movies. `2001_SLZQ_001` is therefore searchable by
title, cast, director, genre and semantic analysis with the same citation and
movie-card behavior as parent movies.

## Incremental Ingestion

Ingestion loads R3 in `loading` state and reuses embeddings from R2 only when
the source type, source identity, content SHA-256, embedding model and task type
match. Expected reuse is exactly 4,670 rows. The new metadata row and nine new
PDF pages are embedded with the configured Vertex model, producing exactly ten
new embeddings and 4,680 total rows.

Any reuse-count mismatch, missing poster, missing citation, zero-result live
query or wrong movie card blocks activation.

## GCP Publication And Rollback

1. Build the same input twice and require byte-identical manifest and JSONL.
2. Upload with create-only semantics beneath `rag/v1.2-demo-r3/`.
3. Ingest R3 without changing the current stable release.
4. Deploy a Cloud Run candidate revision with no traffic.
5. Run authenticated API and browser smoke tests for both new movies, including
   citations, deep-document content, follow-up continuity and poster delivery.
6. Activate R3 and route stable traffic only after every gate passes.

Rollback is a database active-release switch back to `v1.2-demo-r2` plus Cloud
Run traffic restoration. R3 GCS objects and database rows remain immutable for
audit; no destructive cleanup is part of this delivery.

## Acceptance Scenarios

- `殭屍先生的視覺美學有甚麼特色？` returns substantive analysis citing
  `mr-vampire-deep-analysis-v1` and a working `殭屍先生` movie card.
- `少林足球如何把足球、功夫和喜劇結合？` returns substantive analysis citing
  `shaolin-soccer-deep-analysis-v1` and a working `少林足球` movie card.
- A follow-up such as `那它和殭屍先生的喜劇節奏有何不同？` resolves the current
  conversational referent and cites evidence for both movies.
- `周星馳的動作喜劇` includes `少林足球` without leaking a previous person intent.
- `/api/config` reports `v1.2-demo-r3`, 4,659 movies, five documents, 21 PDF
  passages, 4,680 embeddings and the exact immutable manifest identity.
