# Conversational Hong Kong Movie RAG MVP Design

**Date:** 2026-08-03

**Status:** Approved by the user's explicit instruction to design, decide, and execute directly

## Objective

Upgrade the deployed minimum RAG demo into a useful movie chatbot without rebuilding the
existing 4,664 embeddings. A user must be able to ask for recommendations such as “有沒有好的
喜劇推薦？”, refine them with follow-ups, receive answers grounded in the 4,658-movie database,
and see every technically available poster for the returned movies.

The answer is the product. Retrieval, citations, tiers, and posters support the answer; they must
not dominate it or turn ordinary recommendation requests into refusals.

## Evidence and root causes

Fresh production requests and source inspection established five independent defects:

1. `movie_tiers.parquet` contains 4,658 authoritative rows (`S=50`, `A=313`, `B=4,295`) and
   `pilot_movies.parquet` contains 24 rows, but neither artifact is present in the RAG bundle or
   query schema. “S級” therefore retrieves titles containing the letter S, including the B-tier
   movie 《S風暴》.
2. The database retrieves plausible comedy candidates, but the generation prompt treats a
   metadata-supported recommendation as unsupported deep analysis and refuses to answer.
3. The API accepts only the current question, the browser sends only that question, and the UI
   replaces the prior response. The deployed service has no conversational context.
4. Exact-title ranking uses unrestricted substring matching. The one-character title 《影》 is
   incorrectly considered an exact match inside the common word “電影”.
5. Poster delivery is technically fail-closed, but cards are created only for model-cited movies,
   unavailable posters have no visible placeholder, and only two poster endpoints are covered by
   the existing live smoke.

These findings rule out a prompt-only fix.

## Product scope

### Required now

- Ingest all 4,658 tier/pilot facet rows as immutable, release-scoped database evidence.
- Answer metadata-supported recommendation and filtering questions without requiring a PDF.
- Support deterministic filters for tier, genre, requested count, explicit year/decade, and
  “more/other” exclusion; retain vector similarity as the semantic ranker.
- Preserve the current fail-closed rule for aesthetics, narrative, themes, cinematography, and
  other analysis that requires deep documents.
- Carry a bounded successful conversation history so follow-ups retain prior constraints and
  returned movie IDs. Every factual answer must still retrieve current database evidence.
- Return one server-owned movie card per selected recommendation, in answer order, with a
  same-origin poster URL whenever an approved poster exists.
- Render a stable “本資料庫暫無海報” placeholder for the 113 unavailable poster rows.
- Add a fleet verifier for the 4,545 approved GCS posters and the 113 unavailable states.
- Keep the existing access-code gate, citation validation, private GCS objects, and Cloud Run URL.

### Deferred to post-MVP

- Durable cross-device conversation history and end-user accounts.
- Personal taste profiles, collaborative filtering, watch providers, ratings, billing, or admin UI.
- A second LLM call for free-form query planning or reranking.
- Re-embedding the corpus solely to add tier/pilot text.
- Public poster URLs or a CDN until the product has an explicit publication policy.

## Chosen architecture

```text
Release v1.2 movies/posters/chunks (unchanged)
             +
movie_tiers.parquet + pilot_movies.parquet
             |
   deterministic immutable facet records
             |
Cloud SQL: movie_facets ──────┐
                              |
question + bounded history -> deterministic intent/constraints
                              |
             ┌────────────────┴───────────────┐
             |                                |
 recommendation/filter                 exact/deep/general
 structured SQL filter                 exact + pgvector
 + vector ranking                      retrieval
             └────────────────┬───────────────┘
                              |
       selected, distinct, grounded movie evidence
                              |
               Vertex grounded answer
                              |
       answer -> movie cards/posters -> citations
```

This is an additive projection over the already active `v1.2-demo` release. Existing movie,
poster, document, chunk, and embedding identities remain unchanged. A new immutable GCS bundle
path prevents rewriting the previous published bundle. The ingestion Job applies migration 0002
and reconciles only missing facet rows into the active release in one transaction.

## Immutable facet contract

`movie_facets` is keyed by `(release_id, movie_id)` and stores:

- `tier`: exactly `S`, `A`, or `B`;
- `tier_reason` and `human_review` from `movie_tiers.parquet`;
- `pilot_movie`, with exactly 24 true rows;
- `handbook_genre`, `evidence_note`, `evidence_url`, and `source_record_id` for pilot rows;
- a deterministic SHA-256 of the canonical facet record.

The builder must prove exact membership with the 4,658 movies, exact tier counts, exact pilot
membership between both parent artifacts, and parent-manifest hashes before producing records.
The database rejects conflicting content for an existing facet identity. Runtime configuration is
healthy only when 4,658 facets, 50 S rows, 313 A rows, 4,295 B rows, and 24 pilots are present.

Facet fields are appended to retrieved metadata evidence at query time. Existing stored passage
text and embeddings do not change, so no embedding recomputation is required.

## Query modes

### Exact movie and metadata lookup

Movie ID and explicit title matches remain ahead of vector distance. Input receives conservative
simplified/traditional normalization for supported domain terms. One-character Chinese titles
match only when explicitly quoted with `《》` or when the normalized question is the title itself;
they never match inside generic words such as “電影”.

The current positive whitelist for canonical metadata fields remains fail-closed for a named movie
that has no deep document.

### Recommendation and structured filtering

A deterministic planner recognizes recommendation/selection wording and extracts only bounded
constraints:

- requested count: default 5, maximum 8;
- tier: `S/A/B級`;
- genres from the Release genre vocabulary and common simplified/English aliases;
- four-digit year, decade, “before”, and “after” bounds;
- continuation wording such as “還有”, “再來”, “別的”, “換一部”, or “more”.

The repository filters metadata rows by those constraints, excludes prior result IDs for
continuations, and returns distinct movies. Ranking is tier (`S`, then `A`, then `B`), pilot flag,
constraint match, then vector distance. “不同年代” applies a deterministic decade-diversity
selection over the ranked candidates.

The generation prompt receives `answer_mode=recommendation`, the bounded dialogue context, and
only the selected database evidence. It must give one concise reason per selected movie using
metadata/tier fields and cite that movie on the same line. “好” means preferred by the database's
tier/pilot and requested-field match; the model must not invent ratings or subjective consensus.

The service, not Vertex, owns the selected movie set. A recommendation is accepted only when the
declared citations cover every selected movie exactly once. Movie cards follow that selected
order, preventing poster coverage from depending on an accidental citation subset.

### Deep and general questions

Deep questions continue through exact-title/vector retrieval and require PDF-page evidence.
Metadata-only movies still receive a clear refusal for unsupported aesthetic or narrative claims.
General factual questions continue to use exact/vector retrieval, with facet evidence available
when relevant. A top-k result may never be used to claim that no other database row exists.

## Bounded conversation context

The browser sends at most four successful exchanges. Each exchange contains the prior user
question, the server-returned answer text, and the server-returned movie IDs. The API enforces
per-field, per-list, and total-size limits and rejects malformed history.

History is used only to recover current intent, constraints, pronouns, and exclusion IDs. It is
explicitly marked as non-evidence in the Vertex request. All facts in the new answer must come
from freshly retrieved passages and pass the unchanged citation whitelist.

The initial MVP keeps this state in the current browser tab. Login, logout, and a “new chat” action
clear it, so separate sessions do not inherit context. Durable Cloud SQL threads become the next
step when end-user identity exists; adding anonymous persistent conversations now would add data
retention and authorization complexity without improving the demo's core answer loop.

## API and UI contract

`POST /api/chat` accepts:

```json
{
  "question": "還有別的嗎？",
  "history": [
    {
      "question": "推薦三部喜劇。",
      "answer": "...",
      "movie_ids": ["...", "...", "..."]
    }
  ]
}
```

The response keeps `answer_markdown`, `citations`, and `movies`. Movie cards add `tier` and
`pilot_movie`; poster URLs remain server-generated same-origin paths.

The UI appends user and assistant turns instead of replacing them. Within each assistant turn the
answer appears first, movie cards second, and citations in a collapsible section last. Loading and
errors never delete prior turns. Every movie card reserves a poster column; unavailable rows show
the explicit placeholder without making an image request.

## Poster guarantee

At query time, an approved poster URL is returned only when the DB row has the canonical object
name, SHA-256, positive size, WebP MIME, primary state, and approved quality state. The proxy
continues to download and verify the exact bytes before returning them.

The release gate additionally verifies all 4,545 approved identities against private GCS bytes and
proves the remaining 113 rows have no derivative identity. The verifier emits a redacted JSON
report with counts and failures; a non-zero mismatch fails deployment. The HTTP smoke samples
returned recommendation cards, while the fleet Job supplies exhaustive object evidence.

## Failure behavior

- Facet source/count/hash mismatch: stop before bundle publication.
- Facet conflict in an active release: fail the transaction; never overwrite the existing row.
- Structured filters with no matches: return a grounded “沒有符合目前條件的結果” response and
  suggest relaxing a named constraint; do not fall back to unrelated vector results.
- Malformed or oversized history: return `422`; never forward it to Vertex.
- Vertex omits a selected recommendation citation: return a controlled error, not partial cards.
- Poster unavailable by governed state: render the placeholder; do not request GCS.
- Poster object/hash/MIME mismatch: fail the fleet gate and return `404` at runtime.

## Acceptance scenarios

1. “有沒有好的喜劇推薦？請推薦3部” returns exactly three genre-matching movies, useful
   metadata-based reasons, three citations, and no deep-analysis refusal.
2. “推薦3部1980年代香港喜劇，給年份和導演” returns exactly three rows whose years are
   1980–1989 and whose genres contain 喜劇.
3. “有哪些S級港片？” returns only facet `tier=S`; it never returns B-tier 《S風暴》.
4. “S风暴是什么电影？” resolves to `2016_SFB_001`; “列出喜劇電影” never exact-matches 《影》.
5. After a comedy recommendation, “還有別的嗎？” retains the genre and excludes prior IDs.
6. “換成2010年以後” retains the active recommendation intent and returns only years >= 2010.
7. A follow-up about one of the prior movie IDs re-retrieves and cites that movie.
8. A new login/session has no prior context; logout and new-chat clear browser history.
9. Deep questions for 《醉拳》 and 《最佳拍檔》 still cite PDF pages; unsupported deep questions
   for other films still fail closed.
10. Every selected movie is cited and has one card. Approved posters return authenticated
    `200 image/webp`; unavailable posters render a fixed placeholder and have no URL.
11. Fleet verification proves `4,545 available + 113 unavailable = 4,658` with zero mismatch.
12. Existing authentication, citation, path/URL rejection, and release-count tests remain green.

## Demo-to-MVP path

This release establishes a credible recommendation and multi-turn answer loop. The next MVP
milestones are measurable rather than architectural guesswork: save anonymous success/failure
metrics, add user identity, persist threads under that identity, learn preference profiles from
explicit feedback, and evaluate retrieval/ranking quality against a curated question set. None of
those steps should begin until the acceptance scenarios above pass on the public Cloud Run link.
