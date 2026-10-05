# Person Routing Residual Closure Design

## Goal

Close the two remaining load-bearing person-routing defects before deploying the Hong Kong movie
RAG demo:

1. a decorated mixed-script multi-person request must never select one Release identity; and
2. a complete positive person request must own the current turn even when it contains continuation
   vocabulary such as `還有嗎`.

The completed behavior must preserve the existing one-positive-person MVP, Release-scoped identity
authority, exact structured filters, citations, cards, posters, and candidate-to-stable deployment
contract.

## Evidence and root causes

At commit `a2990cef08b49efd4c1457c169e70ddc40071fc0`, the exact request
`想看周星驰和成龍電影，推薦幾部` produces no `PersonQueryShape`. The leading discovery phrase and
trailing recommendation phrase prevent the coordinated-catalog full match. The generic
recommendation path then asks the repository to resolve the whole question. Repository resolution
checks exact Release spellings first; it sees exact `成龍`, skips the normalized-alias branch that
would see `周星驰 -> 周星馳`, and incorrectly returns only `成龍`.

The exact request `山田太郎的殭屍電影還有嗎` does produce one positive current-person shape, but
the lexical token `還有` marks the plan as a continuation. The planner consequently merges history
and prior exclusions. The service then discards the current shape for every continuation except an
explicit `switch`. If current identity resolution returns zero, it can resolve the previous person,
embed mixed history/current context, and execute a personless or wrong-person scoped search. Even
when the current identity resolves, old exclusions and history still contaminate a request that is
self-contained.

These are boundary-ordering defects, not embedding or Vertex model defects.

## Options considered

### 1. Patch only the two reported strings

Adding literal checks for `想看` and `還有嗎` would be small, but another recommendation wrapper,
script mixture, or controlled scope would recreate the same failure. This option is rejected.

### 2. Enforce one current-turn identity invariant at parser, repository, and service boundaries

The parser recognizes bounded discovery/recommendation wrappers; the repository evaluates exact
and normalized-alias evidence as one identity set; and the planner/service make a complete current
person authoritative before history. This keeps the existing public repository protocol and
structured-search model while making the failure impossible at three independent boundaries.

This is the selected MVP approach.

### 3. Replace repository resolution with a new resolved/unknown/ambiguous result protocol

A typed outcome would distinguish ambiguity from absence even when no syntax is recognized. It
would also change the repository protocol, every fake, service orchestration, and integration
surface. The selected approach already obtains deterministic early ambiguity detection and a
repository defense without that migration. This larger API change is deferred unless future work
adds multi-person Boolean retrieval or fuzzy/romanized identity resolution.

## Architecture and invariants

### 1. Bounded current-turn person compilation

`retrieval.py` remains the owner of syntactic person selection. Its normalizer must accept bounded
discovery wrappers (`想看`, `想找`) and a bounded trailing recommendation/count clause while retaining
the existing recommendation wrappers. It must then expose every coordinated candidate in catalog
forms rather than returning no shape or the first name.

The parser does not assert that candidates exist in the Release. It only establishes current-turn
selection semantics. Multiple or exclusionary candidates still produce a shape so `QueryService`
can return the existing one-positive-person clarification before any repository, embedding,
retrieval, generation, citation, card, or poster work.

Wrapper removal is bounded to known request grammar. It must not scan arbitrary prose for CJK
substrings or convert a bare person name into recommendation intent.

### 2. Unified Release identity evidence

`rag_db.py` remains the Release identity authority. For one question, resolution must construct a
single set of identity evidence from:

- exact canonical credit occurrences in the raw question; and
- simplified query-alias occurrences in the normalized question.

An exact canonical span owns that same span, preserving existing exact Traditional homograph
behavior such as `張沖` versus `張衝`. Alias evidence is still evaluated at every other span, so an
exact `成龍` occurrence cannot hide a separate simplified `周星驰` occurrence. Contained shorter
credit tokens must not turn a longer exact credit into a false multi-person request.

Resolution succeeds only when the resulting evidence belongs to one `(simplified alias,
traditional equivalence key)` identity class. Multiple distinct classes, an unqualified simplified
homograph, or a compiler-declared multi-person/exclusionary shape returns no person. All canonical
spellings in the selected equivalence class continue to populate `ResolvedPerson.exact_names`.

No schema, SQL credit-token query, stored metadata, or role-filter contract changes.

### 3. Current person precedes continuation history

A complete positive current-person request with transition `new` is a self-contained reset even if
it contains lexical continuation words. Its current filters are compiled without previous filter
state, previous person, previous result exclusions, or history text in the retrieval context. The
service must retain the current shape for resolution and fail closed on zero identity before
embedding.

History is consulted for person identity only when the current turn has no complete positive person
selector. Two existing behaviors remain distinct:

- a pure continuation such as `還有別的嗎` inherits the latest successful person, filters, and
  prior movie exclusions; and
- an explicit switch such as `換成成龍` replaces the person, may inherit non-person filters, and
  clears exclusions from the previous person.

This precedence applies equally to metadata, title-keyword proxy, and supported deep/scoped
recommendation routes. Existing exact-movie, strong non-movie, unsupported deep-analysis, and
incompatible controlled-credit-group gates remain higher priority.

## Data flow

1. Normalize and resolve any exact movie target.
2. Apply strong non-movie/domain precedence.
3. Parse the complete current-turn person shape.
4. Reject multiple/exclusionary shapes immediately.
5. Compile structured recommendation constraints. A complete `new` person resets history; a true
   continuation or explicit switch follows its existing inheritance rule.
6. Resolve all current-question Release identity evidence as one set.
7. If the current shape is positive and resolution is empty, return deterministic clarification.
8. Only with no current person may a true continuation scan successful history.
9. Embed and execute exact structured retrieval.
10. Validate person, role, genre, citations, cards, and posters as already implemented.

## Failure behavior

- Mixed-script or decorated multiple-person selection: existing one-positive-person clarification;
  zero downstream calls and zero citations/cards.
- Unknown complete current person, including controlled title scopes and `還有嗎`: current-person
  clarification; no history lookup, embedding, search, generation, citation, card, or poster.
- Known complete current person after another person: current identity only, current filters only,
  no prior exclusions.
- Multiple Release identity classes discovered defensively by the repository: no selected person.
- Zero rows under a valid known person and supported filters: retain the existing constrained
  zero-match response; never broaden to vector retrieval.

## Verification design

### Parser and planner

- Mixed Simplified/Traditional discovery-plus-suffix forms expose both canonical candidates and
  produce no recommendation plan.
- A single-person equivalent remains a valid structured plan.
- A complete `new` person containing `還有嗎` is not a semantic history continuation and carries no
  old exclusions or history retrieval context.
- A pure continuation and an explicit switch retain their established inheritance behavior.

### Repository

- `想看周星驰和成龍電影，推薦幾部` and the inverse-script/order variants do not resolve one person.
- A single simplified alias, a single exact Traditional spelling, an exact Traditional homograph,
  and multiple stored spellings from one equivalence class continue to resolve correctly.

### Query service

- Mixed-person requests terminate before all external and presentation-producing boundaries.
- Unknown current-person `還有嗎` requests, standalone and after resolvable person history, resolve
  only the current question and terminate before embedding.
- A known current person replaces prior-person history, retains its exact filters, and clears old
  exclusions.
- A pure continuation still resolves history and excludes previous results.

### Production acceptance

The authenticated live smoke must add:

- the decorated mixed-script multi-person request and its fail-closed empty-artifact response; and
- a `王家卫` first turn followed by a history-bearing, complete `周星驰...还有吗` request that must
  return only canonical `周星馳` action-comedy cards with no history carry-over.

Both the zero-traffic candidate and stable URL must pass the same identity-bound smoke before tag
cleanup. Promotion remains exact-revision, exact-image, secret-version, and ETag-CAS guarded.

## Scope boundaries

This change does not add multi-person Boolean retrieval, fuzzy names, romanized aliases, a people
table, durable server-side memory, new embeddings, new models, new PDFs, database migrations, or
Motion FE/BE integration. It closes the current RAG demo's person-routing safety and continuity
contract only.
