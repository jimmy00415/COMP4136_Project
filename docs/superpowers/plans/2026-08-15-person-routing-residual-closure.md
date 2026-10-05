# Person Routing Residual Closure Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** Make every complete current-turn person selection authoritative across script variants,
continuation vocabulary, Release identity resolution, and production smoke acceptance before GCP
deployment.

**Architecture:** Keep the existing deterministic compiler and repository protocol. First normalize
bounded request wrappers and classify a complete `new` person as a history reset. Then resolve exact
and alias occurrences as one span-aware Release identity set and retain the current shape through
service resolution. Finally add the two residual scenarios to the identity-bound candidate/stable
live smoke.

**Tech Stack:** Python 3.12, dataclasses, OpenCC, pytest, Ruff, mypy, PowerShell deployment harness,
Cloud Run live-smoke HTTP client.

## Global Constraints

- The active Release credit lexicon is the only person-identity authority; no runtime celebrity
  allowlist, fuzzy name, romanized alias, people table, or database migration is allowed.
- The MVP supports exactly one positive person. Unknown, multiple, and exclusionary selections fail
  before embedding, generic/structured/deep search, Vertex generation, poster lookup, citations, or
  cards.
- A complete positive current-person request with transition `new` replaces earlier person and
  filter state and inherits no exclusions, even if it contains `還有` or another lexical
  continuation token.
- A pure continuation inherits the latest successful person, filters, and exclusions. An explicit
  `switch` replaces the person, may inherit non-person filters, and clears previous-person
  exclusions.
- Exact Traditional spelling must continue to disambiguate same-span homographs. Alias evidence at
  every other span must still participate in the unified identity set.
- Exact movie, strong non-movie, unsupported deep-analysis, and incompatible controlled-credit-group
  gates retain their current higher precedence.
- Existing SQL credit-token extraction, role predicate, embeddings (`gemini-embedding-2`, 768D),
  citations, cards, governed posters, authentication, release identity, ETag-CAS promotion, and
  rollback contracts must not be weakened.
- Follow strict RED-GREEN-REFACTOR. Every new test must name the production break it catches, use
  hand-derived literal expectations, and be observed failing for the intended reason before source
  changes.
- Do not mutate GCP in Tasks 1-3. Deployment occurs only after task reviews, final whole-branch
  review, and fresh complete local verification are clean.

## File structure

```text
src/hk_movie_rag/retrieval.py       bounded person syntax and semantic continuation/reset
src/hk_movie_rag/rag_db.py          span-aware unified Release identity evidence
src/hk_movie_rag/rag_query.py       current-person-before-history service orchestration
tests/test_retrieval.py              parser/planner RED-GREEN regressions
tests/test_rag_db.py                 repository identity-set RED-GREEN regressions
tests/test_rag_query.py              service call-boundary and result-contract regressions
scripts/gcp/live-smoke.py            candidate/stable production acceptance scenarios
tests/test_gcp_demo_scripts.py       executable live-smoke fake-server contracts
README.md                            operator-visible query and deployment acceptance contract
```

---

### Task 1: Compile decorated person selection and semantic current-turn ownership

**Files:**
- Modify: `src/hk_movie_rag/retrieval.py:204-235, 698-855, 858-1085`
- Modify: `tests/test_retrieval.py:400-500`

**Interfaces:**
- Consumes: `PersonQueryShape`, `_QuestionConstraints`, `plan_recommendation()`,
  `parse_person_query_shape()`, and the existing switch/continuation grammar.
- Produces: the unchanged public `PersonQueryShape` and `RecommendationPlan` types; internally,
  `_QuestionConstraints.complete_current_person: bool` and a semantic `plan.continuation` that is
  false for complete `new` current-person resets.

- [ ] **Step 1: Write parser RED tests for bounded decorated mixed-person forms**

Add a parameterized test with these literal cases:

```python
@pytest.mark.parametrize(
    ("question", "expected_names"),
    (
        ("想看周星驰和成龍電影，推薦幾部", ("周星馳", "成龍")),
        ("想找成龙與周星馳作品，推介三部", ("成龍", "周星馳")),
    ),
)
def test_decorated_mixed_script_people_retain_every_current_candidate(
    question: str, expected_names: tuple[str, ...],
) -> None:
    """Breaks if discovery or trailing request wrappers hide a second person."""
    shape = retrieval.parse_person_query_shape(question)

    assert shape is not None
    assert shape.candidate_names == expected_names
    assert shape.exclusionary is False
    assert plan_recommendation(question, ()) is None
```

Add the single-person control `想看周星驰的電影，推薦幾部`; it must yield only `周星馳` and a
non-`None` structured plan. This catches an over-broad wrapper strip that destroys valid selection.

- [ ] **Step 2: Run the new parser tests and record RED**

Run:

```powershell
uv run pytest -q tests/test_retrieval.py -k "decorated_mixed_script or decorated_single_person"
```

Expected: the mixed cases fail because `parse_person_query_shape()` returns `None`; the
single-person control may also fail for the same wrapper boundary, not from fixture or encoding
errors.

- [ ] **Step 3: Write planner RED tests for complete current person plus continuation vocabulary**

Use a prior recommendation with S tier, 1980s years, romance, and movie ID `old-wong`. Assert the
literal current query `山田太郎的殭屍電影還有嗎` produces a plan with:

```python
assert plan is not None
assert plan.continuation is False
assert plan.scope_label == "title_keyword_proxy"
assert plan.tier is None
assert (plan.year_from, plan.year_to) == (None, None)
assert plan.excluded_movie_ids == ()
assert plan.context_text == "山田太郎的殭屍電影還有嗎"
```

Also retain literal controls proving `還有別的嗎？` remains a true continuation with inherited
person/filter state and exclusions, while `換成成龍` remains a switch that inherits non-person
filters and clears exclusions.

- [ ] **Step 4: Run the planner tests and record RED**

Run:

```powershell
uv run pytest -q tests/test_retrieval.py -k "current_person_with_continuation or true_person_continuation or person_switch"
```

Expected: the complete-current case fails with `continuation=True`, inherited history context, and
`old-wong`; the two controls pass.

- [ ] **Step 5: Implement bounded wrapper normalization**

Extend the anchored person prefix grammar to include `想看|想找`. Add a bounded trailing pattern
that removes only a comma/semicolon-delimited `推薦|推介|建議` clause with an optional Chinese or
Arabic count and movie-count unit. Apply it in `parse_person_query_shape()` before exclusions and
transition parsing, then strip only `_PERSON_QUERY_PUNCTUATION` again.

The resulting transformation for the reported case must be exactly:

```text
想看周星馳和成龍電影，推薦幾部
-> 周星馳和成龍電影，推薦幾部
-> 周星馳和成龍電影
```

Do not use a free-text `search()` for arbitrary CJK names; feed the bounded remainder through the
existing coordinated/repeated parser.

- [ ] **Step 6: Implement semantic continuation after complete-person classification**

Add `complete_current_person: bool` to `_QuestionConstraints`. Compute it from the existing
single-positive shape and `_is_complete_person_movie_request()` result. In
`plan_recommendation()`, derive:

```python
current_person_reset = bool(
    current.complete_current_person
    and current.person_query_shape is not None
    and current.person_query_shape.transition == "new"
)
semantic_current = replace(
    current,
    continuation=current.continuation and not current_person_reset,
)
previous = (
    _effective_recommendation_constraints(bounded_history)
    if semantic_current.continuation
    else None
)
effective = _merged_recommendation_constraints(semantic_current, previous)
```

Build `excluded_movie_ids` and multi-turn `context_text` only from
`semantic_current.continuation`. For a reset, `context_text` is the normalized current question.
Propagate `complete_current_person` through both internal `_QuestionConstraints` constructors.

- [ ] **Step 7: Run Task 1 GREEN and focused regression suites**

Run:

```powershell
uv run pytest -q tests/test_retrieval.py
uv run pytest -q tests/test_rag_query.py -k "continuation or person_switch or current_person"
uv run ruff check src/hk_movie_rag/retrieval.py tests/test_retrieval.py
git diff --check
```

Expected: all selected tests pass with no Ruff or whitespace errors.

- [ ] **Step 8: Self-review mutations and commit**

Mentally mutate: remove `想看`, stop stripping the suffix, restore lexical continuation, or append
history unconditionally. At least one new test must fail for each mutation. Commit only the Task 1
files:

```powershell
git add -- src/hk_movie_rag/retrieval.py tests/test_retrieval.py
git commit -m "fix: make current person own recommendation turns"
```

---

### Task 2: Unify Release identity evidence and retain the current shape through service resolution

**Files:**
- Modify: `src/hk_movie_rag/rag_db.py:1493-1609`
- Modify: `src/hk_movie_rag/rag_query.py:642-704, 746-827`
- Modify: `tests/test_rag_db.py:528-769`
- Modify: `tests/test_rag_query.py:3600-4215`

**Interfaces:**
- Consumes: Task 1's parser and semantic `RecommendationPlan.continuation` contract.
- Produces: unchanged `QueryRepository.resolve_recommendation_person(...) -> ResolvedPerson | None`;
  `None` now also defends against mixed exact/alias identity sets, and `QueryService` retains every
  current positive shape until it resolves or fails closed.

- [ ] **Step 1: Write repository RED tests for one unified identity set**

With `connection.person_rows = [("周星馳", "actor"), ("成龍", "actor")]`, assert `None` for:

```python
(
    "想看周星驰和成龍電影，推薦幾部",
    "想看成龙和周星馳電影，推薦幾部",
)
```

Retain literal controls that `推荐周星驰的好电影` resolves `周星馳`, `成龍的電影` resolves
`成龍`, exact `張沖` selects only that exact Traditional equivalence class when both `張沖` and
`張衝` exist, and `廖启智` returns all stored spellings in its one equivalence class.

- [ ] **Step 2: Run repository tests and record RED**

Run:

```powershell
uv run pytest -q tests/test_rag_db.py -k "unified_identity or mixed_script or homograph or equivalence_class"
```

Expected: the reported mixed query resolves `成龍` instead of `None`; existing control behavior
remains green.

- [ ] **Step 3: Implement span-aware exact-plus-alias evidence**

Within `resolve_recommendation_person()`, replace the direct-then-alias branch with one evaluation.
Use one small private immutable match record or an equivalent focused helper carrying:

```python
start: int
end: int
canonical_name: str
simplified_alias: str
equivalence_key: str
exact: bool
```

Enumerate every relevant occurrence, subject to the compiler candidate when it exists. Retain only
maximal exact spans so a longer exact Release name suppresses a contained shorter name. Exact spans
own their same positions; ignore alias candidates that overlap those positions. At all remaining
positions, retain maximal aliases and every equivalence key for the alias. Form one final set of
`(simplified_alias, equivalence_key)` classes.

Apply these literal selection rules:

```text
zero classes                   -> None
more than one distinct class   -> None
one class with exact evidence  -> earliest, then longest exact canonical spelling
one class with alias evidence  -> lexicographically first canonical spelling in that class
```

Then populate `exact_names` from every lexicon row in the selected class and keep the parsed or
inferred requested role. Do not change `_release_credit_lexicon()` or SQL.

- [ ] **Step 4: Run repository GREEN and its full file**

Run:

```powershell
uv run pytest -q tests/test_rag_db.py -k "person_resolution or homograph or equivalence_class or unified_identity or mixed_script"
uv run pytest -q tests/test_rag_db.py
```

Expected: all tests pass; no query returns an arbitrary first person.

- [ ] **Step 5: Write service RED tests for all no-call and reset boundaries**

Add three observable behavior tests:

1. `想看周星驰和成龍電影，推薦幾部` returns the exact existing one-positive-person
   clarification with empty citations/movies and zero person-resolution, embedding,
   recommendation/deep/generic search, generation, and poster calls.
2. Unknown `山田太郎的殭屍電影還有嗎`, both standalone and after resolvable `周星馳` history,
   calls person resolution only for the current question, returns a current-person exact-resolution
   clarification, and makes every downstream call list remain empty.
3. Known `成龍的動作電影還有嗎` after a prior S-tier 1980s `王家衛` recommendation resolves only
   the current question. Its structured plan has `person_name="成龍"`, `genres=("動作",)`, no tier
   or years, and `excluded_movie_ids == ()`; generic search remains unused and citations/cards align
   to the current result.

Retain the pure `還有別的嗎` history-inheritance controls.

- [ ] **Step 6: Run service tests and record RED**

Run:

```powershell
uv run pytest -q tests/test_rag_query.py -k "decorated_mixed_script or current_person_continuation or known_current_person_reset"
```

Expected before source changes: the mixed query is not clarified, and the unresolved current query
embeds or resolves the historical person.

- [ ] **Step 7: Retain the current positive shape until resolution**

After the existing multiple/exclusionary and incompatible controlled-credit-group gates,
`current_person_shape` is the current parsed `person_query_shape` without a continuation-based
discard:

```python
current_person_shape = person_query_shape
```

Pass it to `_resolved_recommendation_plan()`. Current-question Release resolution stays first. When
it returns `None`, the retained positive shape supplies the current qualifier and terminates before
history. History scanning remains reachable only when no current shape exists and the semantic plan
is a true continuation.

- [ ] **Step 8: Run Task 2 GREEN and broad routing regressions**

Run:

```powershell
uv run pytest -q tests/test_rag_db.py tests/test_rag_query.py tests/test_retrieval.py
uv run pytest -q tests/test_relevance_policy.py tests/test_demo_api.py
uv run ruff check src/hk_movie_rag/rag_db.py src/hk_movie_rag/rag_query.py tests/test_rag_db.py tests/test_rag_query.py
uv run mypy src/hk_movie_rag/rag_db.py src/hk_movie_rag/rag_query.py src/hk_movie_rag/retrieval.py
git diff --check
```

Expected: all commands pass. The only acceptable existing warning is the repository's previously
recorded Starlette/httpx deprecation warning; no new warnings.

- [ ] **Step 9: Self-review mutations and commit**

Mentally mutate: restore direct-match short-circuit, count overlapping aliases under an exact
homograph, discard current shape on continuation, or allow history after current resolution zero.
Each mutation must be caught by a new test. Commit only Task 2 files:

```powershell
git add -- src/hk_movie_rag/rag_db.py src/hk_movie_rag/rag_query.py tests/test_rag_db.py tests/test_rag_query.py
git commit -m "fix: resolve current person before aliases and history"
```

---

### Task 3: Gate both residuals in candidate and stable production smoke

**Files:**
- Modify: `scripts/gcp/live-smoke.py:120-145, 1230-1450, 2780-2821`
- Modify: `tests/test_gcp_demo_scripts.py:1100-1575, 3990-4470`
- Modify: `README.md:78-96, 307-319`

**Interfaces:**
- Consumes: Task 2's user-visible clarification and current-person reset behavior.
- Produces: two new redacted smoke checks,
  `mixed_script_multi_person_rejected` and
  `person_continuation_vocabulary_resets_history`, enforced identically for tagged candidate and
  stable URL by the existing deployment script.

- [ ] **Step 1: Write live-smoke RED tests against the executable fake server**

Extend `_SmokeHandler` to model the exact two requests. Add tests proving the smoke sends:

```text
想看周星驰和成龍電影，推薦幾部
周星驰的动作喜剧还有吗
```

The second request must carry exactly the existing one-turn `王家卫的电影` history. The output
checks dictionary must contain both new keys with literal `True` values.

Add two negative fake-server scenarios:

- mixed-person response includes a card or citation, or omits the one-positive-person
  clarification -> `mixed person selection contract failed`;
- the history-bearing complete-current response returns `王家衛`, wrong genres, wrong count, or
  prior WONG movie overlap -> `current person continuation reset contract failed`.

- [ ] **Step 2: Run the new smoke tests and record RED**

Run:

```powershell
uv run pytest -q tests/test_gcp_demo_scripts.py -k "mixed_person or continuation_reset or live_smoke"
```

Expected: new check keys/questions are absent and the negative scenarios are not detected.

- [ ] **Step 3: Add the production smoke assertions**

Add the mixed-person `_chat()` call after the existing one-person checks. Require:

```python
answer_markdown == "目前一次只支援一位正向人物；請只指定一位導演或演員後再試。"
citations == []
movies == []
```

Replace only the history-bearing action-comedy reset question with
`周星驰的动作喜剧还有吗`; keep the standalone action-comedy question unchanged. Reuse
`_assert_person_recommendation()` and additionally reject overlap with the WONG card IDs. Set both
new check keys only after their complete contracts pass. Add both keys with initial `False` values
to the redacted report skeleton.

- [ ] **Step 4: Update the fake handler and README contract**

Make the fake handler return the fixed clarification for the mixed query and the existing five
canonical Chow action-comedy rows for either action-comedy wording. Preserve its drift injection
flags so wrong-person, wrong-genre, and overlap cases still exercise real smoke assertions.

Update README query semantics to state that continuation vocabulary does not override a complete
current person, and update the production smoke paragraph with both exact new gates.

- [ ] **Step 5: Run Task 3 GREEN and deployment harness tests**

Run:

```powershell
uv run pytest -q tests/test_gcp_demo_scripts.py -k "live_smoke"
uv run pytest -q tests/test_gcp_demo_scripts.py tests/test_gcp_deploy_apply.py
uv run ruff check scripts/gcp/live-smoke.py tests/test_gcp_demo_scripts.py
git diff --check
```

Expected: all live-smoke and combined deployment-harness tests pass. Allow the combined command up
to 120 minutes on this Windows host; timeout is no verdict and must be rerun or reported accurately.

- [ ] **Step 6: Self-review mutations and commit**

Mentally mutate each new query, check key, history payload, clarification artifact assertion, and
wrong-person/overlap branch. The executable fake-server tests must catch each change. Commit only
Task 3 files:

```powershell
git add -- scripts/gcp/live-smoke.py tests/test_gcp_demo_scripts.py README.md
git commit -m "test: gate current person routing in live smoke"
```

---

## Final local and deployment gate

After all three task reviews and the whole-branch review are clean, the controller must run on the
exact final HEAD:

```powershell
uv lock --check
uv run ruff check .
uv run mypy src
uv run pytest -q
uv run python -m hk_movie_rag.relevance_eval --golden evals/general_relevance_golden.jsonl
git diff --check
git status --short --branch
```

Then reverify the active `admin@motionexp.com` CLI/ADC authority, project `motionexpaiweb`, required
Cloud Run/Secret Manager/Cloud SQL/Artifact Registry/Cloud Build/Vertex permissions, current stable
revision/traffic/ETag, and secret `hk-rag-demo-key` without printing its value. Both provision and
deploy `-PlanOnly` commands must pass before Apply.

Deploy from this clean committed worktree with `config/rag_demo_r2.yaml`, document source
`D:\VS_PROJECT\HK_Movie_KnowledgeGraph\RAG_INPUTS\v1.2-demo-r2`, reuse Release `v1.2-demo`, and bundle
directory `artifacts\rag-demo\v1.2-demo-r2`. The existing deployment script must create a
zero-traffic candidate, run the complete authenticated smoke, promote by exact ETag CAS, run the
same stable smoke, and remove the candidate tag only after success. Deliver the stable URL only with
the exact revision, image digest, redacted check report, and independent post-deploy verification.
