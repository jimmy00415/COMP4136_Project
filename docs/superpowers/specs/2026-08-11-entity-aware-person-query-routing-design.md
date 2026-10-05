# Entity-aware Person Query Routing Design

**Date:** 2026-08-11

**Status:** Approved by the user's explicit instruction to choose an MVP path and execute directly

## Objective

Make natural, release-scoped person queries such as `王家卫的电影` and
`周星驰的动作喜剧` use exact structured retrieval instead of accidentally using generic vector
search or being rejected before retrieval. The fix must generalize across governed people, roles,
genres, years, scripts, and bounded conversation history without hard-coding celebrity names or
adding an LLM query router.

This is a query-understanding and retrieval-routing change. It does not change the database schema,
embeddings, source Release, poster authority, authentication, or Motion frontend/backend.

## Evidence and root cause

The current service has a circular dependency:

1. `plan_recommendation()` must first decide that a structured plan exists.
2. Release-scoped person resolution runs only after that plan exists.
3. Bare person selection phrases do not contain a recommendation/list trigger, so they create no
   plan.
4. The domain gate cannot use the unresolved person as movie evidence and either rejects the query
   or lets an explicit word such as `电影` fall through to generic vector search.

This explains both observed symptoms. `王家卫的电影` happens to retrieve plausible titles through
semantic similarity, while `周星驰的动作喜剧` is rejected before embedding. Repeating the second
request with empty history produces the same result, proving that browser history is not causal.

There is a second correctness defect on the same path: ordinary `genres` are implemented as an
OR predicate. Consequently, even after the person route is repaired, `动作喜剧` could return a pure
action film or a pure comedy. Natural adjacent genre constraints need conjunction semantics unless
the question explicitly says `或`, `或者`, or `or`.

## Options considered

### 1. Add more trigger words

Adding `的电影` or specific celebrity names would be fast but would preserve the split between the
planner, person resolver, history replay, and domain gate. It would continue to miss role, year,
switch, and script variants. This option is rejected.

### 2. Deterministic entity-aware query compiler

Parse a bounded person-query shape, resolve its candidate against the active Release credit
lexicon, and compile the combined entity and filter semantics into the existing
`RecommendationPlan`. The same shape is replayable from history and is also the authority for
fail-closed unresolved/multiple-person behavior. Existing exact SQL, post-retrieval validation,
Vertex generation, citations, cards, and posters remain unchanged.

This is the selected MVP option. It is deterministic, testable without Vertex, adds no model call,
and fixes the architecture at the point where the bad decision originates.

### 3. Vertex structured-output router

A model-based planner could recognize more language, but it would add latency, cost, and a new
non-deterministic authorization boundary. Its output would still require deterministic validation.
It is deferred until measured query-evaluation failures justify it.

## Chosen architecture

### Shared person-query shape

`retrieval.py` owns one deterministic `PersonQueryShape` parser. It recognizes only bounded,
movie-selection forms:

- possessive work/catalog requests: `王家卫的电影`, `王家卫的作品`;
- person plus governed filters: `周星驰的动作喜剧`, `周星驰1990年代的电影`;
- explicit credit roles: `周星驰主演的喜剧`, `王家卫导演的作品`;
- bounded person switches in an active recommendation chain: `换成成龙`, `那周星驰呢`;
- simplified and Hong Kong traditional variants through the existing query normalization.

The parser returns candidate names, optional role, transition kind, and polarity. It does not claim
that a name exists. The active Release credit lexicon remains the only authority that can resolve a
person. A unique positive candidate may produce a structured plan; an unknown candidate fails
closed; multiple candidates or exclusionary Boolean wording receive a clarification because this
MVP supports exactly one positive person predicate.

A bare name such as `周星驰` does not silently become a recommendation. A person combined with an
explicit non-movie object such as `歌曲`, `收入`, `股票`, `游戏`, or `天气` remains out of domain.

### Query compilation and history

`_QuestionConstraints` carries the shared person-query shape and transition. The planner treats a
valid person-selection shape as structured selection intent even when no recommendation verb is
present. Therefore the same pure planner can reconstruct successful history without relying on
answer text or hard-coded names.

For a complete new person request, current constraints replace previous person and filter state and
do not inherit exclusions. For `more` or pronoun continuation, the latest successful structured
chain supplies filters, person, and prior movie IDs. For an explicit person switch, the new person
replaces the old person; non-person filters may be inherited, while exclusions from the previous
person are cleared. A later standalone generic recommendation still resets an older person chain.

The service resolves the current person first within an active structured plan. Only a true
continuation may consult history, newest first. The resolved person is copied into the existing
`RecommendationPlan.person_*` fields before any embedding or SQL search.

### Genre Boolean semantics

One named genre continues to use the existing exact token predicate. Two or more genres without an
explicit alternative conjunction compile to `genres_all`; `动作喜剧` therefore requires both
`動作` and `喜劇`. Explicit alternative wording compiles to `genres_any`. The existing SQL and
post-retrieval validator already implement exact `genres_all`/`genres_any` semantics and remain the
enforcement boundary.

### Person identity and role

Person identity resolution searches the full active-Release credit lexicon. A requested actor or
director role is applied as a retrieval predicate after identity resolution rather than filtering
the identity lexicon first. This distinguishes “known person with no rows in that role” from
“unknown person” and lets the existing zero-match response report the requested role accurately.

No query may silently select the first of multiple named people. Multiple-person AND/OR retrieval
is deferred, but detection and clarification are required now.

## Failure behavior

- Unknown plausible person in a movie-selection shape: no embedding, vector fallback, citation, or
  card; ask the user to check the name.
- Multiple people or negative/exclusionary person request: explain the one-positive-person MVP
  boundary and ask for one person; do not choose the first name.
- Known person plus an unsupported role or filters with zero rows: return the existing constrained
  zero-match response naming the person/role; do not broaden the predicate.
- Deep aesthetic/narrative questions continue to require governed PDF evidence and never become
  metadata recommendations merely because a person or genre is present.
- Strong non-movie objects take precedence over person recognition and remain rejected before
  retrieval.

## Verification contract

Local tests must prove behavior, not just response copy:

1. Simplified/traditional bare person catalog, role, genre, and year variants create a structured
   plan with the canonical Release person and never call generic search.
2. `王家卫的电影` followed by `周星驰的动作喜剧` resolves only the current person, resets history,
   returns five rows, and requires both genres.
3. `周星驰的动作喜剧` followed by `还有吗` or a person pronoun inherits person and filters while
   excluding earlier IDs.
4. Explicit person switch replaces the person without resurrecting earlier exclusions; a generic
   reset prevents old-person resurrection.
5. Unknown, multiple, negative, and non-movie person queries fail before embedding and Vertex.
6. Repository and service post-validation reject any row missing the exact person, requested role,
   or every conjunctive genre.
7. Existing exact-title, deep-document, citation, poster, authentication, and generic relevance
   tests remain green.

The authenticated candidate and stable live smoke both execute the exact production sequence. The
second answer must contain five metadata citations and five movie cards, every card must carry the
canonical `周星馳` credit and both `動作` and `喜劇`, and every available poster URL must pass the
existing protected poster check.

## Deployment

Deployment uses the existing clean-worktree, immutable-image, zero-traffic candidate flow. The
candidate must pass the full authenticated smoke before the ETag-conditional exact-revision traffic
promotion. The stable URL then runs the same smoke before the candidate tag is removed. Any failed
candidate leaves stable traffic unchanged; a failed post-promotion validation uses the deployment's
owned ETag rollback path.

## Deferred

- Multi-person Boolean retrieval after the clarification boundary.
- Negative/exclusionary person predicates beyond fail-closed clarification.
- Fuzzy names, romanized aliases, entity IDs, or a dedicated people table.
- LLM planning or reranking, new embeddings, durable server-side chat memory, and end-user auth.
