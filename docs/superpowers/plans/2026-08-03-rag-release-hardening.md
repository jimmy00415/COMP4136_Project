# RAG Release Hardening Implementation Plan

> **For agentic workers:** execute test-first with `superpowers:subagent-driven-development`.

**Goal:** Prevent answer/citation/card mismatch and domain-irrelevant retrieval, tolerate bounded
Vertex transients, and require the exact active release plus the operator-confirmed poster scope
before the immutable Cloud Run candidate is promoted.

**Design:**
`docs/superpowers/specs/2026-08-03-rag-release-hardening-design.md`

## Global constraints

- Preserve release `v1.2-demo`, 4,658 movies/assets/metadata passages, 2 documents, 6 PDF
  passages, 4,664 embeddings, 4,658 facets, and the 4,545/113 poster split.
- Do not alter source rights values, publish raw GCS URLs, weaken citations, or call Vertex for a
  query rejected by the domain gate.
- Calibration may read GCP but may not mutate the database, release objects, service, or traffic.
- Each task requires RED evidence, focused GREEN gates, one commit, and independent review.

## Task 3A: Bind answers and gate generic retrieval

**Primary files:** `retrieval.py`, `rag_query.py`, `vertex_clients.py`, a small relevance policy
module/artifact, evaluation fixture/script, focused retrieval/query/Vertex tests, and README.

- [ ] Reproduce and reject swapped-title, swapped-citation, missing/duplicate item, and foreign
  movie-ID recommendation outputs.
- [ ] Make recommendation/deep-generation return ordered `{citation_id, reason}` items; render
  title and marker server-side from the same evidence used by Citation/MovieCard.
- [ ] Add a deterministic positive movie-domain gate. Prove `推荐一本好书`, weather, greeting,
  rewrite, and stock questions return the bounded domain message before embedding/generation.
- [ ] Preserve exact movie, governed genre/tier/year, resolved-person, and successful-history
  behavior.
- [ ] Preserve and validate finite general-search distance; filter generic evidence above `0.36`.
  Exact/history targets and structured recommendation/person searches bypass the cutoff.
- [ ] Commit 30 calibration and 10 holdout cases plus a release/model/dimension-bound policy and
  redacted evaluation report. Require 20/20 allowed movie-domain and 20/20 rejected OOD cases.
- [ ] Configure Vertex generation for a 20-second timeout and three total transient attempts;
  prove transient-to-success, permanent no-retry, and exhausted redacted failure.
- [ ] Run focused retrieval/query/Vertex/relevance tests, Ruff, mypy, lock check, and diff check.
- [ ] Commit as `fix: bind grounded answers and reject irrelevant queries`.

## Task 3B: Require complete release and poster-serving authority

**Primary files:** `rag_db.py`, `demo_api.py`, immutable authority JSON/loader, focused DB/API
tests, README. Deployment wiring belongs to the later safe-promotion task.

- [ ] Make `assert_query_ready()` validate active state; 4,658/4,658/4,658/2/6/4,664 release
  counts; 4,658/50/313/4,295/24 facets; and 4,658/4,545/113 poster semantics in one checkout.
- [ ] Make `/api/config` and `QueryService.answer()` reuse the same readiness gate and fail closed
  on every single-count tamper, including 4,663 embeddings.
- [ ] Add the canonical restricted-demo poster authority artifact bound to the source manifest,
  derived inventory, access mode/scope, counts, and operator confirmation without mutating source
  rights.
- [ ] Require the exact authority digest in runtime settings; expose the digest in authenticated
  config for deployment/live smoke verification. Missing/wrong digest or scope refuses startup.
- [ ] Run focused DB/API/authority tests, Ruff, mypy, lock check, and diff check.
- [ ] Commit as `fix: require complete RAG release authority`.

## Task 4: Validate a no-traffic revision before exact Cloud Run promotion

Execute Task 3 from the original plan only after 3A and 3B are independently approved. In
addition to its existing contract, deploy and candidate validation must set and verify the exact
poster-authority digest and relevance-policy identity. No `latest` secret versions or floating
`LATEST` traffic target may remain.

## Task 5: Full verification, deployment, and live acceptance

Execute the original cross-cutting verification task with these added probes:

- answer line, citation, and card order/identity equality for every recommendation;
- 20 movie-domain and 20 OOD golden cases, with OOD producing no cards/citations;
- transient-safe Vertex config, complete release counts, and exact authority digest;
- simplified/traditional person recommendations, history continuation, parallel config, and all
  returned iPhone poster requests;
- latest-created = latest-ready = exactly one untagged 100% served revision.
