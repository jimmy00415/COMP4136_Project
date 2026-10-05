# RAG Answer and Release Hardening Design

**Status:** Approved addendum to the person-retrieval/poster-concurrency design.

## Why this addendum exists

The screenshot bugs exposed two direct defects, but the independent architecture review found
three additional release risks on the answer path:

1. recommendation text can name movie B while carrying movie A's citation and card;
2. generic vector search always returns nearest neighbours, including for non-movie questions;
3. a transient Vertex generation failure immediately becomes a failed chat turn.

The active query gate also checks only facets, and the operator's confirmed poster permission is
not represented by a machine-readable restricted-demo serving decision.

These are release gates because they can produce a wrong answer, a misleading citation, or a
needlessly failed demo turn. They are not a reason to restart the broader governance program.

## Evidence boundary

A read-only calibration against release `v1.2-demo`, model `gemini-embedding-2`, dimension 768,
and pgvector cosine distance used 15 broad in-domain questions and 15 out-of-domain questions.
The in-domain minimum-record distances ranged from approximately `0.2355` to `0.32937`.
Out-of-domain questions such as weather (`0.31277`) and a greeting (`0.30674`) overlapped that
range. Therefore a scalar similarity threshold is not an out-of-domain classifier.

The Cloud SQL Auth Proxy used for calibration was the Google-signed v2.23.0 Windows binary. The
evaluation was read-only, credentials were kept in process memory, and the proxy was stopped when
the run completed.

## Chosen architecture

### 1. Bind recommendation text, citation, and card on the server

For `recommendation` and `deep_recommendation`, Vertex returns an ordered list of
`{citation_id, reason}` items. The list must exactly equal `required_citation_ids`, and each reason
is bounded text without a title, citation marker, URL, or path. The adapter renders every line from
the corresponding immutable evidence:

```text
《server-owned title》：model reason。[server-owned citation_id]
```

The query service independently validates the rendered title/citation prefix before it builds the
Citation and MovieCard from the same EvidencePassage. The API response shape remains unchanged.
General answers retain the existing cited-markdown contract.

### 2. Use a positive movie-domain gate before embeddings

An input reaches embedding/retrieval only when at least one deterministic signal applies:

- an exact release movie ID/title or a bounded history movie reference is resolved;
- the question contains an explicit movie/credit/metadata term;
- a recommendation contains a governed genre, tier, year, movie term, or resolved release person;
- a bounded continuation inherits a successful movie recommendation.

Recommendation language alone is not a movie signal. `推荐一本好书` and other explicit
non-movie objects return `我目前只能回答这个 Release 内的香港电影问题。` with no citations or
cards and without calling Vertex. `有没有好的喜剧推荐` remains a valid movie recommendation.

### 3. Apply a calibrated tail cutoff only to generic vector evidence

After the domain gate, generic general-search records must carry a finite cosine distance in
`[0, 2]`; records above `0.36` are discarded. The value leaves approximately `0.03063` absolute
margin above the observed calibration maximum, but it is not used as the domain classifier.

Exact title/ID targets, bounded history targets, and structured recommendation/person SQL are
identity- or predicate-constrained and do not use this cutoff. Missing/invalid distances fail
closed. A versioned policy binds the cutoff to release/model/dimension and the golden-query digest.

The release gate uses 40 questions: 30 calibration cases plus 10 untouched holdouts. Production
must allow all 20 movie-domain cases, reject all 20 out-of-domain cases before embedding, and
retain evidence for every semantic in-domain case.

### 4. Bound Vertex generation retries and time

Generation uses an explicit 20-second SDK timeout and three total attempts for transient HTTP
statuses only (`408`, `429`, `500`, `502`, `503`, `504`), with bounded backoff. Permanent client
errors and local JSON/schema/grounding validation do not retry. The public failure remains a
redacted `VertexGenerationError`.

### 5. Expand query readiness without building a new control plane

`assert_query_ready()` validates the active release, exact movie/asset/passage/document/embedding
counts, facet census, and poster semantic census in one release-scoped transaction. Both
`QueryService.answer()` and `/api/config` use this gate.

Poster serving is authorized by a small immutable operator-attestation artifact bound to:

- release `v1.2-demo`;
- parent source manifest SHA-256
  `cbd529e126e8b72dccac302d9746fcfa63318caecbcda24548c05e282e13f974`;
- derived poster inventory SHA-256
  `b754727129bc044b6c9da1e6db22067c8279c5e7f8dd41d0350e365376538154`;
- restricted, authenticated, same-origin private proxy delivery;
- 4,658 primary rows, 4,545 approved objects, and 113 unavailable rows.

The artifact records the user's operator permission confirmation. It does not rewrite the source
`rights_status=unknown` to `cleared`, does not claim a legal copyright determination, and does not
authorize raw GCS or public CDN access. Its canonical digest is a fixed runtime/deploy setting and
is exposed non-secretly in `/api/config` for smoke verification.

No new authority database, KMS workflow, re-ingestion, or release schema migration is introduced
for this demo. Those would recreate the governance/product imbalance the project is correcting.

## Explicitly deferred to demo-to-MVP

- high-entropy independent session signing, login/chat rate limits, cost quotas, and user identity;
- durable server-side conversation state beyond bounded movie IDs and constraints;
- a read-only runtime database role and database-level active-row DELETE/embedding immutability;
- legal-rights workflow, public poster CDN, and multi-release authority management.

The shared code `123456` remains a short-lived restricted-demo access mechanism, not MVP
authentication.
