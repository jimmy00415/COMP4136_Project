# Person-aware Retrieval and Mobile Poster Reliability Design

**Date:** 2026-08-03

**Status:** Approved by the user's explicit instruction to investigate, decide, and execute
directly

## Objective

Repair the deployed RAG chatbot so a person-qualified recommendation such as
“推荐周星驰的好电影” returns only movies whose governed cast/director metadata contains that
person, and so all governed poster cards survive normal mobile parallel loading. The same change
must remove the Cloud Run deployment path that can route production traffic before the candidate
revision has been validated.

This is a correctness and reliability increment over the current 4,658-movie RAG release. It does
not rebuild embeddings, change source data, add Motion FE/BE work, or broaden the authorization
model.

## Production evidence and root causes

### Person-qualified recommendations

The production request “推荐周星驰的好电影” returned five cards, but only the first movie
contained 周星馳 in its governed credits. Source inspection and a deterministic service
reproduction established that:

- `RecommendationPlan` has tier, genre, year, count, diversity, continuation, and exclusion
  fields, but no person field;
- `search_recommendations()` and `search_deep_recommendations()` contain no cast/director
  predicate;
- tier and pilot ordering therefore promotes semantically nearby but unrelated movies after the
  first vector match;
- cards faithfully render the selected server evidence, so this is not a browser-card mix-up;
- the release stores the canonical name as `周星馳`, while the observed query uses simplified
  `周星驰`.

This rules out a prompt-only repair. Vertex must never receive an unrelated candidate set.

### Mobile poster failures

Cloud Run request logs for the exact iPhone session show successful login, config, and chat calls,
followed by simultaneous poster requests where one returned `200` and the others returned `503`.
The same poster identities later returned `200` from the same revision and user agent. A live
parallel `/api/config` probe reproduced the same one-success/multiple-503 pattern, while sequential
requests remained healthy.

The runtime creates one `psycopg.Connection`, shares one `RagRepository` across the application,
and serves Cloud Run with concurrency 20. Every repository method opens a transaction/cursor on
that same connection. Concurrent ASGI worker-thread requests therefore overlap operations on one
stateful database connection. The poster UI then permanently replaces any failed image after its
first error, making a transient backend fault look like missing content.

This rules out missing GCS objects, WebP decoding, mobile cookies, or poster permissions as the
incident root cause.

### Deployment safety

The deployment script currently deploys a revision, immediately runs `update-traffic
--to-latest`, and only then validates revision identity, readiness, image, and service state.
`LATEST` is also a floating traffic target. A failed validation can therefore leave traffic
changed, and a future revision can inherit the floating target.

## Chosen architecture

### 1. Resolve a governed person, then apply an exact credit predicate

Person resolution remains deterministic and release-scoped:

1. Normalize the current question from simplified Chinese to Hong Kong traditional Chinese using
   OpenCC's `s2hk` conversion.
2. Split governed `cast` and `director` strings on the release's canonical separators.
3. Find an exact normalized credit token contained in the current question. Prefer the longest
   token and limit the result to one person for this MVP.
4. Respect an explicit role word: actor/cast wording searches cast, director wording searches
   director, and an unqualified possessive recommendation searches either field.
5. On a continuation, resolve the current question first; only when it contains no new person,
   walk successful history from newest to oldest. A new person replaces the old one.

`RecommendationPlan` gains optional `person_name` and `person_role` fields. Both normal and deep
recommendation SQL apply an exact, parameter-bound token predicate. Person, genre, tier, year, and
exclusion predicates are AND-composed. If an explicit person qualifier cannot be resolved, or if
the resulting hard filter has no rows, the service returns an insufficient-evidence answer and
never falls back to broad vector results.

The model still writes the answer, but only from the service-selected, person-matching evidence.
Citation and card validation remain unchanged.

### 2. Use a bounded PostgreSQL connection pool at the serving boundary

CLI/ingestion code may continue using one direct connection because it is single-process,
sequential work. The FastAPI runtime instead creates a synchronous `psycopg_pool.ConnectionPool`
at startup, waits for readiness, and closes it during lifespan shutdown. `RagRepository` accepts
either its existing direct connection or an explicit pool; `_cursor()` checks out one connection
per repository operation.

The serving pool is bounded to five connections per Cloud Run instance. With the existing maximum
of three instances this caps this service at fifteen database connections, while excess
concurrent requests wait for a checkout instead of corrupting a shared transaction.

API failures log only a fixed stage and exception type. Logs never include database credentials,
access codes, GCS object names, or poster bytes.

The browser retries an approved same-origin poster at most twice with bounded backoff. After the
last failure it renders an accessible “海報暫時無法載入” state. This is defense in depth, not a
replacement for the pool fix. Governed `poster_url=null` continues to render “暫無海報” without
making a request.

The live smoke must use an authenticated mobile user agent and request returned poster URLs in
parallel. Sequential checks alone are no longer sufficient evidence.

### 3. Validate an immutable Cloud Run revision before exact promotion

Deployment follows this order:

1. Resolve numeric Secret Manager versions for both database password and demo access key.
2. Capture the currently served exact revision when one exists.
3. Deploy the candidate with `--no-traffic` and immutable image digest.
4. Read the exact latest-created revision and require it to be ready.
5. Validate its immutable image, service account, Cloud SQL binding, fixed environment contract,
   numeric secret bindings, concurrency, and timeout before any traffic command.
6. Promote only with `--to-revisions=<candidate>=100`.
7. Re-read the service and require one untagged 100% entry for that exact revision.
8. If post-promotion validation fails and a prior exact revision exists, restore it with one exact
   revision command and exit non-zero.

No `--to-latest` traffic target remains.

## Failure behavior

- Explicit recognized person with no qualifying rows: return zero unrelated cards and name the
  constrained person in the insufficiency message.
- Explicit person-like qualifier that is not a release credit: ask the user to check the name;
  do not issue a broad recommendation.
- Pool startup cannot establish a connection: application startup fails; Cloud Run never marks the
  candidate ready.
- Pool checkout/query failure: return the existing controlled `503` and emit a redacted stage log.
- Governed poster identity or integrity failure: preserve the existing fail-closed `404` behavior;
  a client retry must not turn an invalid asset into success.
- Candidate revision/config validation failure: preserve current traffic and exit non-zero.
- Post-promotion validation failure: restore the previous exact revision when available and exit
  non-zero.

## Acceptance scenarios

1. “推荐周星驰的好电影” returns only rows whose exact cast or director token is `周星馳`.
2. “推薦周星馳主演的喜劇” filters exact cast plus genre; “周星馳導演的電影” filters exact
   director.
3. Simplified and traditional forms resolve to the same canonical person.
4. “再来三部，不要重复” inherits the canonical person and excludes prior IDs; “换成成龙”
   replaces it.
5. A nonexistent explicit person never broadens to unrelated vectors.
6. Normal and deep recommendation SQL both contain bound, exact person predicates.
7. Twenty parallel repository-backed requests do not share one connection and complete without the
   reproduced transaction failure.
8. Parallel authenticated mobile poster fetches for all returned cards are `200 image/webp` when
   their governed rows are available.
9. A transient browser image error retries only within the fixed budget; a permanent error becomes
   an accessible placeholder.
10. Deployment tests prove traffic is untouched before candidate validation, exact revision
    promotion is used, `--to-latest` is absent, and rollback occurs after a post-promotion mismatch.
11. Existing citation, authentication, poster-integrity, tier/genre/year, conversation, and source
    immutability tests remain green.

## Deferred

- Entity tables, aliases, knowledge-graph person IDs, fuzzy spelling, or multiple-person Boolean
  grammar.
- Re-embedding movies to add normalized person aliases.
- Public poster URLs/CDN and end-user identity.
- Durable preference memory or Motion application integration.
