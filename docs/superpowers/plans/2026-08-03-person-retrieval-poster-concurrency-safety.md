# Person Retrieval, Poster Concurrency, and Safe Promotion Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: use
> `superpowers:subagent-driven-development` and execute each task test-first.

**Goal:** Make person-qualified recommendations exact, eliminate the shared-database-connection
mobile poster failure, and promote only a validated immutable Cloud Run revision.

**Architecture:** Resolve one canonical governed cast/director name before retrieval and add it as
an AND-composed SQL constraint. Serve FastAPI through a bounded psycopg connection pool and add a
bounded UI retry plus parallel mobile smoke. Deploy candidates with no traffic, validate the exact
revision/security contract, then pin 100% traffic to that revision.

**Tech stack:** Python 3.11, OpenCC, psycopg 3/psycopg-pool, PostgreSQL/pgvector, FastAPI,
browser JavaScript, PowerShell/gcloud, Cloud Run, Cloud SQL, GCS, Vertex AI.

## Global constraints

- Preserve Release v1.2 source bytes, active release `v1.2-demo`, 4,658 movie/facet/poster rows,
  4,664 embeddings, and all existing citation/integrity gates.
- Never use prompt text as a substitute for a hard person predicate.
- Never serialize credentials, access codes, database DSNs, raw exception messages, GCS object
  names, or poster bytes into logs or reports.
- Keep current Cloud Run resource bounds: concurrency 20, maximum three instances, 1 CPU, 1 GiB.
- Use test-driven development: add the behavioral test, observe the expected failure, then write
  production code.
- Each task ends with focused tests, self-review, an implementer report, and one scoped commit.

---

## Task 1: Add deterministic person-aware recommendation retrieval

**Files:**
- Modify: `pyproject.toml`
- Modify: `uv.lock`
- Modify: `src/hk_movie_rag/retrieval.py`
- Modify: `src/hk_movie_rag/rag_query.py`
- Modify: `src/hk_movie_rag/rag_db.py`
- Modify: `tests/test_retrieval.py`
- Modify: `tests/test_rag_query.py`
- Modify: `tests/test_rag_db.py`

**Required interfaces:**
- `ResolvedPerson(name, role)` or an equivalently typed immutable result.
- `RecommendationPlan.person_name: str | None` and `person_role` with backward-compatible
  defaults.
- `QueryRepository.resolve_recommendation_person(release_id, question)`.
- One query-service helper that resolves the current turn first, then the newest applicable
  history turn only for continuation.

- [ ] Add failing planner/service tests for simplified `推荐周星驰的好电影`, traditional input,
  actor-only, director-only, person+genre+year, continuation inheritance, actor switch, and
  unresolved/no-match fail-closed behavior. Prove unrelated candidates never reach the generator
  or cards.
- [ ] Add failing repository tests that inspect parameterized SQL and demonstrate exact split-token
  matching in both `search_recommendations` and `search_deep_recommendations`.
- [ ] Run the new focused tests and record the expected RED failures in the task report.
- [ ] Add pinned-compatible `opencc>=1.3,<2`, initialize one `s2hk` converter, and normalize only
  query text. Do not rewrite stored release data.
- [ ] Implement release-scoped exact credit resolution, role detection, one-person selection, and
  the hard SQL predicates.
- [ ] Include person/role in insufficient-results messages and reject an unresolved explicit
  person qualifier without vector fallback.
- [ ] Run:
  `uv run pytest tests/test_retrieval.py tests/test_rag_query.py tests/test_rag_db.py -q`.
- [ ] Run `uv run ruff check` and `uv run mypy` on the modified production modules.
- [ ] Commit as `fix: enforce person-qualified movie retrieval`.

---

## Task 2: Replace the shared runtime connection and harden mobile poster delivery

**Files:**
- Modify: `pyproject.toml`
- Modify: `uv.lock`
- Modify: `src/hk_movie_rag/rag_db.py`
- Modify: `src/hk_movie_rag/demo_api.py`
- Modify: `src/hk_movie_rag/static/app.js`
- Modify: `scripts/gcp/live-smoke.py`
- Modify: `tests/test_rag_db.py`
- Modify: `tests/test_demo_api.py`
- Modify: `tests/static_app_ui_contract.mjs`
- Modify: `tests/test_gcp_live_smoke.py`

**Required interfaces:**
- A redaction-safe `create_db_pool(DatabaseSettings)` startup boundary with minimum one, maximum
  five connections, health check, bounded checkout timeout, and explicit readiness wait.
- `RagRepository.from_pool(pool)` or an equivalent explicit pooled constructor. Direct connection
  behavior must remain available for ingestion and existing tests.
- One connection checkout per `_cursor()` operation.

- [ ] Add a failing concurrent repository/runtime test whose fake connection rejects overlapping
  transactions; prove the existing single connection reproduces the failure and the new pool gives
  each operation a separate checked-out connection.
- [ ] Add failing startup/close tests that ensure a pool is awaited, supplied to the repository,
  and closed by FastAPI lifespan without leaking connection settings.
- [ ] Add failing API logging tests for fixed `config_db`, `chat_backend`, `poster_db`, and
  `poster_storage` stages with exception type only.
- [ ] Add failing DOM tests for bounded retry then success, bounded retry exhaustion, null-poster
  no-request behavior, and accessible final text.
- [ ] Add a failing live-smoke test that requests returned approved poster paths concurrently using
  an iPhone user agent and requires every response to satisfy the poster contract.
- [ ] Run the new tests and record RED.
- [ ] Add `psycopg[pool,binary]>=3.3,<4` (or the equivalent explicit `psycopg-pool` dependency),
  implement the bounded pool, and keep CLI code on its direct connection.
- [ ] Add redacted stage logging, bounded client retry/backoff, and parallel mobile live smoke.
  Do not retry governed 404/no-poster states in server code.
- [ ] Run:
  `uv run pytest tests/test_rag_db.py tests/test_demo_api.py tests/test_gcp_live_smoke.py -q`
  and `node --test tests/static_app_ui_contract.mjs`.
- [ ] Run `uv run ruff check` and `uv run mypy` on the modified production/smoke modules.
- [ ] Commit as `fix: isolate concurrent demo database requests`.

---

## Task 3: Validate a no-traffic revision before exact Cloud Run promotion

**Files:**
- Modify: `scripts/gcp/deploy-demo.ps1`
- Modify: `tests/test_gcp_deploy_apply.py`
- Modify: `tests/test_gcp_demo_scripts.py`

- [ ] Add failing fake-gcloud scenarios proving: deploy includes `--no-traffic`; no traffic command
  occurs on pre-promotion revision/config failure; promotion uses
  `--to-revisions=<candidate>=100`; `--to-latest` is absent; both secrets use numeric versions;
  and a post-promotion mismatch restores the prior exact revision.
- [ ] Add failing validation scenarios for service account, Cloud SQL binding, fixed env values,
  secret name/version, concurrency, timeout, image, revision identity, and readiness.
- [ ] Run the new focused tests and record RED.
- [ ] Capture current exact traffic, resolve both numeric secret versions, deploy `--no-traffic`,
  validate the exact candidate revision/security contract, promote by exact revision, and validate
  final traffic. Restore prior exact traffic on post-promotion failure.
- [ ] Preserve fail-closed behavior for ambiguous prior traffic and gcloud schema drift.
- [ ] Run:
  `uv run pytest tests/test_gcp_deploy_apply.py tests/test_gcp_demo_scripts.py -q`.
- [ ] Commit as `fix: validate Cloud Run revision before promotion`.

---

## Task 4: Cross-cutting verification, review, deployment, and production acceptance

**Files:**
- Modify if needed: `README.md`
- Modify if needed: focused tests only for review defects

- [ ] Generate a review package for every task range and obtain a code-quality/spec review; fix all
  Critical and Important findings before continuing.
- [ ] Update README support/operations text for person filters, pooled serving, poster retry, and
  exact revision promotion.
- [ ] Restore the three ignored source fixture directories into the isolated worktree from the
  repository root, run the entire test suite, then remove only those exact ignored worktree copies
  after a dry-run listing.
- [ ] Run full gates: `uv run pytest -q`, `uv run ruff check .`, `uv run mypy src/hk_movie_rag`,
  and `node --test tests/static_app_ui_contract.mjs`.
- [ ] Deploy through the corrected script to project `motionexpaiweb`, region `us-central1`.
- [ ] Run the complete live smoke and independent authenticated probes for simplified/traditional
  周星馳 queries, continuation with no duplicates, combined person+genre, parallel config, and all
  returned mobile poster URLs.
- [ ] Require latest-created = latest-ready = exactly served revision, one untagged 100% traffic
  entry, and the expected immutable image digest.
- [ ] Perform final whole-branch review, keep the branch isolated, and report the stable demo URL,
  access code, exact revision, test evidence, and any remaining product limitations.
