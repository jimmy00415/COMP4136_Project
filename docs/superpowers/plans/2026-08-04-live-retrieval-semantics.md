# Live Retrieval Semantics and Reliability Plan

## Goal

Make the deployed demo answer database-supported movie discovery questions reliably while refusing unsupported corpus-wide analysis without hallucinating. The release remains `v1.2-demo`: 4,658 metadata records, 4,545 available posters, 113 explicitly unavailable posters, and two deep-analysis PDFs.

## Evidence boundary

- Structured metadata may prove title, date, director, cast, genre, tier, pilot status, and poster availability.
- Controlled credit groups may filter exact director or cast tokens, but may not invent biographies.
- Title-proxy topics may only be described as title-keyword matches, not as verified plot themes.
- Only the two governed PDFs may support deep claims. A corpus-wide claim without matching document evidence returns controlled insufficient evidence.
- Every successful movie result must preserve answer/citation/card identity and the poster endpoint contract.

## Task 1: Typed structured retrieval

Extend the recommendation plan with separate `genres_all`, `genres_any`, title-proxy terms, and controlled credit groups. Parse the bounded production queries for police/crime decades, action-comedy, female directors, female cast, gambling-title proxies, zombie-title proxies, and family-genre discovery. Preserve exact-person, continuation, tier, year, and exclusions.

Tests first:

- planner emits exact canonical constraints for each supported query;
- deep or unsupported wording never silently becomes a metadata claim;
- simplified Chinese normalizes to the same canonical policy values.

## Task 2: SQL and post-retrieval enforcement

Apply parameterized predicates for genre-all, genre-any, title terms, credit role/names, years, tier, and exclusions. Validate every returned record against the same plan in the query service; a repository row outside the plan fails closed.

Tests first:

- exact SQL parameters and all/any semantics;
- male-director, wrong-cast, non-gambling-title, non-family, wrong-year, and wrong-genre rows are rejected;
- no raw user text becomes an SQL constraint.

## Task 3: Generation reliability

Keep Vertex AI for grounded prose and the two scoped deep-document paths. For structured recommendation output, use a bounded Vertex attempt with a deterministic metadata-only fallback when the provider or strict response schema fails. The fallback may state only release date, director, cast, genre, tier, and pilot status, and must emit one exact cited line per card. General/deep failures remain fail closed.

Tests first:

- provider failure and invalid JSON produce a valid structured fallback;
- general and deep modes never use the metadata fallback;
- citation order and card identity remain exact.

## Task 4: Honest live acceptance

Split production acceptance into:

- supported structured queries: exact typed constraints, citations, cards, and posters;
- scoped deep queries: known PDF/movie/page authorities and scoped wording;
- controlled insufficient queries: stable HTTP 200 limitation response with no fabricated cards;
- out-of-domain queries: exact domain refusal and no evidence.

The screenshot query `推荐周星驰的好电影` and its traditional form remain mandatory five-card tests, followed by a non-repeating continuation.

## Task 5: Release and verification

Run focused TDD, Ruff, mypy, the full affected pytest partitions, deploy through the no-traffic/etag-CAS gate, then run the complete live smoke against the stable Cloud Run URL. Promote only after all checks pass; otherwise leave or restore the previously captured serving revision without blind rollback.
