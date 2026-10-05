# Release-title query routing design

## Problem

The live query `讲一下富贵逼人的喜剧创作` is in scope and has governed PDF evidence, but the
service returns the fixed out-of-domain response before embedding, database retrieval, or Vertex
generation. Simplified-to-Hong-Kong-Traditional normalization succeeds; explicit movie resolution
still returns no movie because undelimited title matching is gated by a static list of recognized
question topics. `喜剧创作` and the generic request lead-in `讲一下` are not in that list.

This couples two separate decisions:

1. **Entity scope:** does the request safely identify a movie in the active Release?
2. **Evidence capability:** do metadata or governed PDF passages support the requested answer?

A Release title should establish movie scope before the evidence router decides how much can be
answered. Topic vocabulary must not be a prerequisite for recognizing that title.

A second live defect exposes a separate selection mismatch. When a title false-negative falls back
to corpus-wide vector search, citations are built from the model's validated selection but movie
cards are built from every retrieved neighbour. The browser records all returned card IDs as the
next history anchor, so uncited neighbours can also corrupt follow-up reference resolution.

## Target flow

1. Normalize user text with the existing OpenCC and conservative title normalization.
2. Resolve explicit movie IDs/titles using structural evidence:
   - exact movie ID;
   - title delimiters;
   - whole-query title;
   - an undelimited title with safe lexical boundaries or a bounded conversational title lead-in.
   The Python resolver and SQL candidate selector use the same audited one-character orthographic
   equivalence map. Exact raw spellings disambiguate equivalent HK variants; a normalized alias
   shared by multiple movies fails closed and returns the exact candidate movie IDs.
3. Mask all accepted identity spans, then reject explicit non-movie residual objects such as a
   novel, game, television series, restaurant, or recipe. Book scope uses explicit grammatical
   predicates rather than a global `書/书` substring, so `書寫`, `書信`, and `書架` remain valid
   movie-language contexts.
4. Treat a resolved Release movie as a positive domain signal independently of topic wording.
5. Retrieve only that movie's metadata/PDF passages. Canonical fields remain server-owned;
   qualitative answers remain grounded in PDF evidence and return page citations.
6. If the resolved movie has no qualitative evidence, return the existing evidence-limitation
   answer instead of guessing.
7. For generic grounded generation, derive citations, movie cards, poster checks, and future history
   anchors from the same validated citation selection.

## Safety invariants

- Longest matching title wins when candidates overlap.
- Short/common titles and short ASCII titles retain strict boundary/context rules; they cannot use
  topic-independent conversational routing unless they carry at least four CJK or eight non-CJK
  identity characters. A bounded request lead-in may resolve a shorter title only when the suffix
  itself is movie-specific (for example director, plot, visual style, or action design).
- Duplicate titles and unresolved normalized aliases return a clarification, never an arbitrary ID.
- Exact raw title spellings take precedence only within an otherwise ambiguous normalized alias.
- A title embedded in an unrelated longer token, such as `英雄` in `英雄联盟`, remains unresolved.
- A matched title followed by an explicit non-movie residual object remains out of domain.
- No fuzzy title matching, recursive source discovery, or LLM-based scope classifier is added.
- OOD rejection still happens before embedding and Vertex generation.
- Uncited retrieval neighbours never become cards, poster-authority lookups, or history anchors.
- The published `v1.2-demo-r2` manifest, bundle, relevance-policy digest, PDFs, embeddings, and
  database rows are unchanged. This is a code routing fix against the existing immutable Release.

## Acceptance contract

The following classes must work in both Simplified and Traditional Chinese:

- conversational lead-in + title + qualitative topic;
- title + possessive qualitative topic;
- delimited title + qualitative topic;
- canonical metadata questions;
- bounded follow-ups after a successful movie answer.

The response for the reported query must identify `1987_FGBR_001`, return at least one governed PDF
page citation, and return the authoritative movie card/poster state. Negative controls must prove
that short-title collisions and explicit novel/game requests still produce no retrieval, citation,
or movie card. Inventory validation must prove that verbatim and Simplified inputs for all 4,658
active titles reach the stored equivalence key, and tests must cover the sole cross-title collision
with exact-raw precedence plus a fail-closed Simplified alias.

A generic multi-neighbour retrieval regression must also prove that a one-movie citation selection
returns one matching card and gives a follow-up pronoun only that movie ID.

Deployment acceptance must exercise the reported natural-language structure against both the
zero-traffic candidate and the stable URL. This runtime smoke is code-level acceptance and does not
change the immutable R2 relevance policy or its digest.
