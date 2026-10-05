# Conversational Hong Kong Movie RAG MVP Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** Turn the deployed single-turn technical demo into a recommendation-capable, bounded multi-turn Hong Kong movie chatbot whose answers, tiers, citations, cards, and posters all come from the governed 4,658-movie release.

**Architecture:** Add an immutable `movie_facets` projection from the already-authorized tier and pilot artifacts without changing stored passages or embeddings. Route recommendation/filter questions through deterministic constraints plus structured SQL and vector ranking, keep deep questions on the existing PDF/pgvector path, carry at most four client exchanges as non-evidence context, and make the server own the selected movie/card set.

**Tech Stack:** Python 3.11+, PyArrow, psycopg 3, pgvector, Google Gen AI on Vertex AI, Google Cloud Storage, FastAPI/Pydantic, browser JavaScript/CSS, Cloud SQL PostgreSQL 16, Cloud Run service and Jobs, PowerShell/gcloud deployment.

## Global Constraints

- Preserve `data/release/v1.2/release_manifest.json` and all six parent artifact bytes.
- Parent manifest SHA-256 remains `e9842db33208e7146d78d86689b13f81fffcf41288d79d84e599b93050445e2c`.
- Existing `v1.2-demo` movies, assets, documents, chunks, and 4,664 embeddings remain byte/identity unchanged.
- Facet contract is exactly 4,658 rows: `S=50`, `A=313`, `B=4,295`, `pilot_movie=true=24`.
- Existing poster contract remains exactly 4,658 primary rows: 4,545 approved private WebP objects and 113 unavailable rows.
- Generation remains `gemini-3.5-flash-lite`; embeddings remain `gemini-embedding-2` at 768 dimensions.
- Every factual answer line has at least one retrieved citation; history is context only and never evidence.
- Recommendation count defaults to 5 and is capped at 8; cards exactly match service-selected cited movies.
- Deep analysis without a matching PDF remains fail-closed; metadata filtering/recommendation is allowed.
- Poster URLs remain authenticated same-origin routes; never emit a raw GCS URL.
- Do not touch Motion FE/BE, MySQL, end-user auth, billing, or unrelated Phase-1 cleanup.
- Publish the upgraded bundle under `rag/v1.2-demo-mvp1/`; never overwrite prior GCS objects.
- The worktree lacks non-Git Phase-1 sources. Task gates use complete RAG/demo suites; the legacy full-suite baseline (`884 passed, 12 failed, 31 errors, 6 skipped`) remains separately reported.

## Planned file map

```text
config/rag_demo.yaml                                  facet counts/schema version
src/hk_movie_rag/schemas/rag_release_manifest.schema.json
src/hk_movie_rag/rag_bundle.py                        tier/pilot bundle records
src/hk_movie_rag/migrations/0002_movie_facets.sql     immutable facet projection
src/hk_movie_rag/rag_db.py                            migration, reconcile, structured SQL
src/hk_movie_rag/rag_ingest.py                        active-release facet reconciliation
src/hk_movie_rag/retrieval.py                         context and recommendation planner
src/hk_movie_rag/rag_query.py                         routing, selection, cards, grounding
src/hk_movie_rag/vertex_clients.py                    recommendation generation envelope
src/hk_movie_rag/demo_api.py                          bounded history API and facet health
src/hk_movie_rag/static/{index.html,app.js,styles.css} append-only chat UI
src/hk_movie_rag/poster_fleet.py                      exhaustive DB/GCS verification
scripts/gcp/{deploy-demo.ps1,live-smoke.py}            cloud gates
tests/                                                 focused red/green regressions
README.md                                              behavior and operations
```

---

### Task 1: Add authoritative tier and pilot facet records to the RAG bundle

**Files:**
- Modify: `config/rag_demo.yaml`
- Modify: `src/hk_movie_rag/schemas/rag_release_manifest.schema.json`
- Modify: `src/hk_movie_rag/rag_bundle.py`
- Modify: `src/hk_movie_rag/cli.py`
- Modify: `tests/test_rag_bundle.py`
- Modify: `tests/integration/test_rag_demo_source.py`

**Interfaces:**
- Consumes manifest-declared `movie_tiers.parquet` and `pilot_movies.parquet`.
- Produces one `record_kind="facet"` per movie with tier/pilot evidence and `content_sha256`.
- Extends `RagBundleResult`/`VerifiedRagBundle` with `facet_count`, `tier_s_count`, `tier_a_count`, `tier_b_count`, and `pilot_count`.
- Manifest schema `1.1` requires those five counts.

- [ ] **Step 1: Write failing facet bundle tests**

```python
def test_bundle_includes_one_authoritative_facet_per_movie(rag_source, tmp_path):
    result = build_rag_bundle(rag_source.root, rag_source.config, tmp_path / "out")
    facets = [r for r in verify_rag_bundle(result.manifest_path).iter_records()
              if r["record_kind"] == "facet"]
    assert result.facet_count == 3
    assert (result.tier_s_count, result.tier_a_count, result.tier_b_count) == (1, 1, 1)
    assert result.pilot_count == 1
    assert [row["movie_id"] for row in facets] == ["1970_A_001", "1980_B_001", "1990_C_001"]

def test_bundle_rejects_tier_or_pilot_membership_drift(rag_source, tmp_path):
    rewrite_tier_fixture_with_missing_movie(rag_source)
    with pytest.raises(RagBundleError, match="facet movie membership"):
        build_rag_bundle(rag_source.root, rag_source.config, tmp_path / "out")
```

Expectations use literal fixture values, not bundle helpers. These tests catch missing, duplicated,
or cross-artifact-inconsistent facet evidence.

- [ ] **Step 2: Verify RED**

Run: `uv run pytest tests/test_rag_bundle.py tests/integration/test_rag_demo_source.py -q`

Expected: failures because facet records/counts and schema `1.1` do not exist.

- [ ] **Step 3: Implement deterministic facet construction**

Read both parquet files only through `_declared_path`; validate membership, vocabulary, booleans,
literal production counts, and pilot agreement. Hash compact sorted UTF-8 facet JSON before adding
`record_kind` and `content_sha256`. Add `facet` to ordering, count verification, dataclasses,
manifest schema, config, and CLI JSON.

- [ ] **Step 4: Verify GREEN**

Run: `uv run pytest tests/test_rag_bundle.py tests/integration/test_rag_demo_source.py -q`

Expected: fixture and real Release prove exact facet membership/counts and deterministic bytes.

- [ ] **Step 5: Commit**

```powershell
git add config/rag_demo.yaml src/hk_movie_rag/schemas/rag_release_manifest.schema.json src/hk_movie_rag/rag_bundle.py src/hk_movie_rag/cli.py tests/test_rag_bundle.py tests/integration/test_rag_demo_source.py
git commit -m "feat: add governed movie facets to RAG bundle"
```

---

### Task 2: Persist immutable facets and expose structured recommendation retrieval

**Files:**
- Create: `src/hk_movie_rag/migrations/0002_movie_facets.sql`
- Create: `src/hk_movie_rag/retrieval.py`
- Modify: `src/hk_movie_rag/rag_db.py`
- Modify: `src/hk_movie_rag/rag_ingest.py`
- Modify: `tests/test_rag_db.py`
- Modify: `tests/test_rag_ingest.py`

**Interfaces:**
- `FacetStats(total: int, tier_s: int, tier_a: int, tier_b: int, pilots: int)`.
- `ConversationExchange(question: str, answer: str, movie_ids: tuple[str, ...])`.
- `RecommendationPlan(requested_count: int, genres: tuple[str, ...], tier: str | None,
  year_from: int | None, year_to: int | None, diversify_decades: bool, continuation: bool,
  excluded_movie_ids: tuple[str, ...], context_text: str)`.
- `RecommendationSearch(records: tuple[dict[str, object], ...], total_matches: int)`.
- Repository methods: `reconcile_facets`, `facet_stats`, and `search_recommendations`.

- [ ] **Step 1: Write failing migration/reconcile/search tests**

```python
def test_active_release_reconciles_facets_idempotently_and_rejects_conflict():
    repository.reconcile_facets(RELEASE_ID, literal_facet_records)
    repository.reconcile_facets(RELEASE_ID, literal_facet_records)
    assert repository.facet_stats(RELEASE_ID) == FacetStats(4658, 50, 313, 4295, 24)
    with pytest.raises(RagDatabaseError, match="facet content conflict"):
        repository.reconcile_facets(RELEASE_ID, conflicting_facet_records)

def test_recommendation_sql_filters_tier_genre_year_and_exclusions():
    plan = RecommendationPlan(3, ("喜劇",), "S", 1980, 1989, False, True,
                              ("1982_ZJPD_001",), "喜劇 S級 1980年代")
    result = repository.search_recommendations(RELEASE_ID, [0.1] * 768, plan)
    assert result.total_matches == 18
    assert result.records[0]["movie"]["tier"] == "S"
```

The fake SQL row must mirror production: metadata passage, complete movie/facet mapping, poster
state, distance, and window count.

- [ ] **Step 2: Verify RED**

Run: `uv run pytest tests/test_rag_db.py tests/test_rag_ingest.py -q`

Expected: missing migration/types/methods fail.

- [ ] **Step 3: Implement migration and active-release reconciliation**

Create the composite FK table, tier/content-hash checks, tier/pilot indexes, and immutable trigger.
`ensure_schema()` applies migration 0002 only over a complete 0001 contract. Reconciliation accepts
active/loading releases, inserts missing rows transactionally, then compares every stored hash and
the exact five facet counts. `ingest_bundle()` reconciles before its active/loading branch, so an
active release gains facets with zero new embeddings.

- [ ] **Step 4: Implement structured recommendation SQL**

Join metadata chunks, movies, facets, and governed poster state. Apply optional tier, canonical
genre, safe year bounds, and excluded IDs. Return distinct movies ordered by tier (`S/A/B`), pilot,
vector distance, year, ID, plus `COUNT(*) OVER()`. Merge facet fields into the movie mapping and
retrieved evidence body; never change stored chunk bytes.

- [ ] **Step 5: Verify GREEN**

Run: `uv run pytest tests/test_rag_db.py tests/test_rag_ingest.py -q`

Expected: all selected DB/ingestion tests pass, including resume and poster semantics.

- [ ] **Step 6: Commit**

```powershell
git add src/hk_movie_rag/migrations/0002_movie_facets.sql src/hk_movie_rag/retrieval.py src/hk_movie_rag/rag_db.py src/hk_movie_rag/rag_ingest.py tests/test_rag_db.py tests/test_rag_ingest.py
git commit -m "feat: add structured movie facet retrieval"
```

---

### Task 3: Route recommendation and follow-up questions through grounded conversation logic

**Files:**
- Modify: `src/hk_movie_rag/retrieval.py`
- Modify: `src/hk_movie_rag/rag_query.py`
- Modify: `src/hk_movie_rag/rag_db.py`
- Modify: `src/hk_movie_rag/vertex_clients.py`
- Modify: `tests/test_rag_query.py`

**Interfaces:**
- `plan_recommendation(question, history) -> RecommendationPlan | None`.
- `QueryService.answer(question, history=()) -> ChatAnswer`.
- `GenerationClient.generate_grounded(question, passages, *, mode, conversation_context, required_citation_ids) -> GeneratedAnswer`.
- `MovieCard` adds `tier: str` and `pilot_movie: bool`.

- [ ] **Step 1: Write failing planner/service regressions**

```python
def test_comedy_recommendation_selects_requested_count_and_requires_all_citations():
    result = service.answer("有沒有好的喜劇推薦？請推薦3部")
    assert [m.movie_id for m in result.movies] == ["comedy-s", "comedy-a", "comedy-b"]
    assert len(result.citations) == 3
    assert "不能提供沒有深度文檔" not in result.answer_markdown

def test_s_tier_follow_up_keeps_tier_and_excludes_prior_ids():
    history = (ConversationExchange("推薦一部S級電影", "上一輪。", ("s-one",)),)
    service.answer("還有別的嗎？", history)
    plan = repository.recommendation_calls[0]
    assert plan.tier == "S"
    assert plan.excluded_movie_ids == ("s-one",)

def test_generic_movie_word_never_exact_matches_one_character_title():
    assert _exact_target_movie_ids("請列出三部喜劇電影", evidence_for_title_ying) == ()
```

Also add literal tests for 1980s, `2010年以後` override, simplified `S风暴`, malformed/oversized
history, decade diversity, zero matches, and missing required recommendation citations.

- [ ] **Step 2: Verify RED**

Run: `uv run pytest tests/test_rag_query.py -q`

Expected: failures for missing history/planner/structured path/card facets/title guard.

- [ ] **Step 3: Implement deterministic planning and safe exact matching**

Recognize recommendation and continuation wording. Inherit constraints from the newest prior
recommendation and let explicit current constraints override. Normalize Release genres and common
simplified/English aliases. Clamp count to 1–8 and history to four exchanges. Exclude prior IDs
only for continuation/replacement. Embed current plus prior user questions, never prior answers.
Require brackets or whole-title intent for one-character Chinese titles in Python and SQL; add the
tested conservative simplified/traditional mappings.

- [ ] **Step 4: Implement recommendation generation and grounding gates**

Select exactly the requested distinct candidates, with deterministic decade diversity when asked.
Put bounded history under `conversation_context_not_evidence`. Recommendation mode produces one
concise, metadata/facet-based, cited line per candidate and never uses the deep-analysis refusal.
Require citation IDs to equal the selected passage IDs; build cards in selected order. Preserve
existing deep-analysis, URL/path, unknown citation, and per-line citation gates for other modes.

- [ ] **Step 5: Verify GREEN**

Run: `uv run pytest tests/test_rag_query.py -q`

Expected: all query/Vertex/citation/metadata/recommendation/history/title tests pass.

- [ ] **Step 6: Commit**

```powershell
git add src/hk_movie_rag/retrieval.py src/hk_movie_rag/rag_query.py src/hk_movie_rag/rag_db.py src/hk_movie_rag/vertex_clients.py tests/test_rag_query.py
git commit -m "feat: add grounded conversational recommendations"
```

---

### Task 4: Deliver append-only chat UX and complete poster-card behavior

**Files:**
- Modify: `src/hk_movie_rag/demo_api.py`
- Modify: `src/hk_movie_rag/static/index.html`
- Modify: `src/hk_movie_rag/static/app.js`
- Modify: `src/hk_movie_rag/static/styles.css`
- Modify: `tests/test_demo_api.py`
- Modify: `tests/static_app_ui_contract.mjs`

**Interfaces:**
- `/api/chat` accepts zero to four strict history exchanges (`question`, `answer`, `movie_ids`) with
  at most 12,000 total characters.
- `/api/config` adds exact facet/tier/pilot counts.
- Movie JSON adds `tier` and `pilot_movie` while retaining governed `poster_url`.

- [ ] **Step 1: Write failing API and real DOM tests**

```python
def test_chat_passes_bounded_history_and_returns_facet_cards(client, login, query_service):
    response = client.post("/api/chat", json={
        "question": "還有別的嗎？",
        "history": [{"question": "推薦喜劇", "answer": "三部。", "movie_ids": ["a", "b", "c"]}],
    })
    assert response.status_code == 200
    assert query_service.calls[0].history[0].movie_ids == ("a", "b", "c")
    assert response.json()["movies"][0]["tier"] == "S"

def test_chat_rejects_fifth_history_exchange(client, login, five_exchanges):
    response = client.post("/api/chat", json={"question": "下一部", "history": five_exchanges})
    assert response.status_code == 422
```

The Node test executes the real app script and proves two submissions leave two user/assistant
turns, the second request carries one exchange, null poster renders “本資料庫暫無海報” without an
image fetch, new-chat clears state, and an error does not delete prior turns.

- [ ] **Step 2: Verify RED**

Run: `uv run pytest tests/test_demo_api.py -q; node tests/static_app_ui_contract.mjs`

Expected: history/facet and append-only/null-poster assertions fail.

- [ ] **Step 3: Implement strict history API and facet health**

Add strict Pydantic exchange models, per-field/list/total validators, and immutable conversion.
Require `FacetStats(4658, 50, 313, 4295, 24)` in config health. Extend card validation with tier
vocabulary and a real boolean pilot flag.

- [ ] **Step 4: Implement append-only transcript and stable cards**

Keep at most four successful exchanges in memory. Append questions and answer groups, store only
successful responses, put citations in `<details>`, add “新對話”, clear on login/logout/new-chat,
and retain old turns during loading/error. Always reserve the poster column: URL uses `<img>` and
existing fallback; null immediately renders the fixed placeholder. Show tier/pilot compactly.

- [ ] **Step 5: Verify GREEN**

Run: `uv run pytest tests/test_demo_api.py -q; node tests/static_app_ui_contract.mjs`

Expected: all API/security/poster and DOM conversation tests pass.

- [ ] **Step 6: Commit**

```powershell
git add src/hk_movie_rag/demo_api.py src/hk_movie_rag/static/index.html src/hk_movie_rag/static/app.js src/hk_movie_rag/static/styles.css tests/test_demo_api.py tests/static_app_ui_contract.mjs
git commit -m "feat: add multi-turn movie chat experience"
```

---

### Task 5: Add exhaustive poster verification and deployment acceptance gates

**Files:**
- Create: `src/hk_movie_rag/poster_fleet.py`
- Create: `tests/test_poster_fleet.py`
- Modify: `src/hk_movie_rag/rag_db.py`
- Modify: `src/hk_movie_rag/cli.py`
- Modify: `scripts/gcp/provision-demo.ps1`
- Modify: `scripts/gcp/deploy-demo.ps1`
- Modify: `scripts/gcp/live-smoke.py`
- Modify: `tests/test_gcp_demo_scripts.py`
- Modify: `tests/test_gcp_deploy_apply.py`
- Modify: `README.md`

**Interfaces:**
- `PosterFleetReport(release_id, primary_rows, approved, unavailable, verified, failures, passed)`.
- `verify_poster_fleet(repository, storage, release_id, bucket, workers=16) -> PosterFleetReport`.
- CLI `hk-movie-rag verify-poster-fleet` emits one redacted JSON object.
- Cloud Run Job `hk-movie-rag-poster-verify` uses existing service account/DB secret/Cloud SQL/GCS viewer.

- [ ] **Step 1: Write failing fleet and deploy-gate tests**

```python
def test_fleet_verifies_all_approved_bytes_and_unavailable_states():
    report = verify_poster_fleet(repository_4545_113, byte_storage,
                                 "v1.2-demo", "private-bucket", workers=4)
    assert (report.primary_rows, report.approved, report.unavailable) == (4658, 4545, 113)
    assert report.verified == 4545
    assert report.failures == ()
    assert report.passed is True

def test_fleet_fails_on_one_hash_size_or_mime_mismatch():
    report = verify_poster_fleet(repository_one_bad, byte_storage,
                                 "v1.2-demo", "private-bucket", workers=2)
    assert report.passed is False
    assert report.verified == 4544
    assert len(report.failures) == 1
```

Fake-gcloud tests execute plan/apply and assert immutable mvp1 paths, verifier Job, exact facets,
and non-zero propagation. Storage fakes return real byte streams; tests assert report behavior,
not mock call existence.

- [ ] **Step 2: Verify RED**

Run: `uv run pytest tests/test_poster_fleet.py tests/test_gcp_demo_scripts.py tests/test_gcp_deploy_apply.py -q`

Expected: verifier/CLI/job/path contracts are missing.

- [ ] **Step 3: Implement bounded-concurrency fleet verification**

Read all poster rows in one release-scoped SQL query and require 4,658 primary rows before storage.
Reuse runtime canonical key/hash/size/MIME semantics for at most 16 concurrent reads, close every
stream, and require unavailable rows to have no derivative. Sort redacted failures by movie ID;
never emit credentials, signed URLs, DB settings, or bytes.

- [ ] **Step 4: Update deployment and live smoke**

Publish upgraded bundle create-only under `rag/v1.2-demo-mvp1/`. Execute ingestion/migration/facet
reconcile and require `embedded=0, skipped=4664`; then execute poster verifier before service
delivery. Extend smoke with exactly three comedy recommendations, a history follow-up retaining
comedy and excluding prior IDs, S-tier-only cards excluding `2016_SFB_001`, and poster/null checks.
Retain existing auth, metadata, two-PDF, citation, poster, logout, and unauthorized checks.

- [ ] **Step 5: Verify GREEN**

Run: `uv run pytest tests/test_poster_fleet.py tests/test_gcp_demo_scripts.py tests/test_gcp_deploy_apply.py -q`

Expected: all verifier and fake-cloud tests pass without secret output.

- [ ] **Step 6: Document and commit**

Document facet semantics, bounded tab history, recommendation limits, fleet report, immutable GCS
path, deployment commands, and explicit demo-to-MVP limits.

```powershell
git add src/hk_movie_rag/poster_fleet.py src/hk_movie_rag/rag_db.py src/hk_movie_rag/cli.py scripts/gcp/provision-demo.ps1 scripts/gcp/deploy-demo.ps1 scripts/gcp/live-smoke.py tests/test_poster_fleet.py tests/test_gcp_demo_scripts.py tests/test_gcp_deploy_apply.py README.md
git commit -m "feat: gate deployment on chat and poster fleet"
```

---

### Task 6: Verify, review, deploy, and prove the public conversation loop

**Files:**
- No production code unless fresh verification/review exposes a defect; fixes use a reviewed commit.

**Interfaces:**
- Consumes clean committed branch plus main-workspace source assets.
- Produces immutable image, completed facet ingestion and poster Jobs, ready 100%-traffic revision,
  and redacted live-smoke evidence.

- [ ] **Step 1: Run complete relevant local gates**

```powershell
uv run pytest tests/test_rag_bundle.py tests/test_rag_db.py tests/test_rag_ingest.py tests/test_rag_query.py tests/test_demo_api.py tests/test_poster_fleet.py tests/test_gcp_demo_scripts.py tests/test_gcp_deploy_apply.py tests/test_gcp_secret_manager_api.py tests/test_gcp_sql_admin_api.py tests/test_gcp_provision_hardening.py -q
uv run ruff check .
uv run mypy src/hk_movie_rag/rag_bundle.py src/hk_movie_rag/rag_db.py src/hk_movie_rag/rag_ingest.py src/hk_movie_rag/retrieval.py src/hk_movie_rag/rag_query.py src/hk_movie_rag/vertex_clients.py src/hk_movie_rag/demo_api.py src/hk_movie_rag/poster_fleet.py
node tests/static_app_ui_contract.mjs
git diff --check
git status --short --branch
```

Expected: zero relevant-test failures, tools exit 0, clean worktree.

- [ ] **Step 2: Run whole-branch review and resolve findings**

Review from merge base through HEAD against every design item, especially migration safety,
active-release idempotency, citations, non-evidence history, recommendation count, poster fleet,
and secret-safe scripts. One fix wave and one scoped re-review address Critical/Important findings.

- [ ] **Step 3: Verify live GCP authority before mutation**

Run read-only access-token, project, service, and Cloud SQL describes. If operator credentials still
require interactive reauthentication, stop before mutation and report the exact blocker; never
weaken IAM, persist a token, or swap identity to bypass it.

- [ ] **Step 4: Deploy clean commit**

```powershell
pwsh -NoProfile -File scripts/gcp/deploy-demo.ps1 -Apply -SourceRoot 'D:\VS_PROJECT\HK_Movie_KnowledgeGraph\RAG'
```

Expected: facet stats `4658/50/313/4295/24`, ingest `embedded=0, skipped=4664`, poster fleet
`4545 verified + 113 unavailable`, immutable image digest, ready revision, 100% traffic.

- [ ] **Step 5: Run fresh public acceptance**

Load access code `123456` only into a temporary process environment. Run updated live smoke and
literal design conversations. Record redacted result IDs/counts, constraints, citation sets,
poster status/MIME, revision/digest, and latency. Require zero metadata-recommendation refusals,
exact requested counts, correct tier/year/genre, follow-up exclusion, valid citations, and every
returned approved poster.

- [ ] **Step 6: Hand off evidence**

Report stable URL/code, revision/digest, facets/poster counts, tests/smoke totals, limitations, and
branch/commit. Do not claim durable history, end-user auth, or public-poster readiness.
