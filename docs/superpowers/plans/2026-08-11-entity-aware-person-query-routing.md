# Entity-aware Person Query Routing Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** Route bounded natural person/movie queries through exact Release-scoped structured retrieval, preserve conversation semantics, and require conjunctive genres without adding an LLM router.

**Architecture:** A deterministic `PersonQueryShape` parser becomes the shared pre-retrieval query signal. The existing planner compiles that signal and governed filters into `RecommendationPlan`; the existing Release credit lexicon then authorizes the person and the current exact SQL, post-validation, Vertex, citations, cards, and posters remain in force.

**Tech Stack:** Python 3.13, pytest, FastAPI service contracts, PostgreSQL/pgvector repository, Vertex AI, PowerShell Cloud Run deployment, Node UI contract tests.

## Global Constraints

- Do not hard-code `王家衛`, `周星馳`, or any other person into runtime relevance/domain allowlists.
- The active Release credit lexicon is the only person-identity authority.
- Support exactly one positive person predicate; unknown, multiple, and exclusionary person semantics fail closed before embedding.
- Strong non-movie object intent wins over any person signal.
- Adjacent multiple genres mean AND; only explicit `或`, `或者`, `還是`, or English `or` means OR.
- Complete current-person queries reset prior person state; true continuations inherit; explicit switches inherit non-person filters but clear old-person exclusions.
- Do not change database schema, embeddings, source Release, poster authority, authentication, or Motion FE/BE.
- Preserve every existing exact-title, PDF evidence, citation, card, poster, and release-readiness gate.
- Modify and deploy only from the clean isolated worktree; do not move, delete, stage, or package root-workspace PDFs or `tmp/`.

---

### Task 1: Compile person-query shapes and Boolean genre semantics

**Files:**
- Modify: `src/hk_movie_rag/retrieval.py`
- Test: `tests/test_retrieval.py`

**Interfaces:**
- Produces: `PersonQueryShape(candidate_names: tuple[str, ...], role: PersonRole | None, transition: Literal["new", "switch"], exclusionary: bool)`.
- Produces: `parse_person_query_shape(question: str) -> PersonQueryShape | None`.
- Changes: `_QuestionConstraints.person_query_shape` carries the parsed result.
- Preserves: `plan_recommendation(question, history) -> RecommendationPlan | None` public signature.

- [ ] **Step 1: Write failing parser and planner tests**

Add table-driven tests covering:

```python
@pytest.mark.parametrize(
    ("question", "names", "role", "transition"),
    [
        ("王家卫的电影", ("王家衛",), None, "new"),
        ("周星驰的动作喜剧", ("周星馳",), None, "new"),
        ("周星馳主演的喜劇", ("周星馳",), "actor", "new"),
        ("王家衛導演的作品", ("王家衛",), "director", "new"),
        ("換成成龍", ("成龍",), None, "switch"),
        ("那周星馳呢", ("周星馳",), None, "switch"),
    ],
)
def test_person_query_shape_is_name_agnostic_and_script_normalized(
    question: str,
    names: tuple[str, ...],
    role: retrieval.PersonRole | None,
    transition: str,
) -> None:
    shape = retrieval.parse_person_query_shape(question)

    assert shape is not None
    assert shape.candidate_names == names
    assert shape.role == role
    assert shape.transition == transition
    assert shape.exclusionary is False
```

Assert `plan_recommendation()` is non-null for the first four forms, remains null for a bare name,
and reconstructs a prior bare-person request for `還有別的嗎？`.

Add genre Boolean tests:

```python
def test_adjacent_genres_compile_as_conjunction():
    plan = plan_recommendation("周星馳的動作喜劇", ())
    assert plan is not None
    assert plan.genres_all == ("喜劇", "動作")
    assert plan.genres_any == ()

def test_explicit_or_genres_compile_as_alternatives():
    plan = plan_recommendation("周星馳的動作或喜劇電影", ())
    assert plan is not None
    assert plan.genres_all == ()
    assert plan.genres_any == ("喜劇", "動作")
```

- [ ] **Step 2: Run the new tests and verify RED**

Run:

```powershell
uv run pytest -q tests/test_retrieval.py -k "person_query_shape or adjacent_genres or explicit_or_genres"
```

Expected: failures because `PersonQueryShape`/`parse_person_query_shape` do not exist and bare
person queries return no plan.

- [ ] **Step 3: Implement the minimal deterministic parser and compiler**

Implement normalized, bounded Chinese person-selection grammar without a name allowlist. Validate
candidate shape and exclude known region/topic qualifiers; store all coordinated candidates so the
service can reject more than one. Mark `不要`, `別要`, `除了`, and `排除` as exclusionary. Treat
`換成...` and `那...呢` as switch transitions.

In `_question_constraints()`, make a unique positive `PersonQueryShape` a structured selection
intent unless deep-analysis or unsupported non-movie semantics suppress it. For multiple natural
genres populate `genres_all`; for explicit alternatives populate `genres_any`. Preserve existing
special structured policies.

For switches, set continuation semantics, inherit previous non-person constraints, and produce no
old-person exclusion IDs. Complete new-person queries remain non-continuations.

- [ ] **Step 4: Run retrieval tests and verify GREEN**

Run:

```powershell
uv run pytest -q tests/test_retrieval.py
```

Expected: all retrieval tests pass.

- [ ] **Step 5: Commit Task 1**

```powershell
git add src/hk_movie_rag/retrieval.py tests/test_retrieval.py
git commit -m "fix: compile natural person movie queries"
```

### Task 2: Enforce entity resolution, ambiguity, history, and exact constraints

**Files:**
- Modify: `src/hk_movie_rag/rag_db.py`
- Modify: `src/hk_movie_rag/rag_query.py`
- Test: `tests/test_rag_db.py`
- Test: `tests/test_rag_query.py`

**Interfaces:**
- Consumes: `parse_person_query_shape()` and `RecommendationPlan` from Task 1.
- Preserves: `RagRepository.resolve_recommendation_person(release_id, question) -> ResolvedPerson | None`.
- Preserves: `QueryService.answer(question, history) -> ChatAnswer` and the public API schema.
- Produces: deterministic pre-embedding clarification for multiple/exclusionary person shapes.

- [ ] **Step 1: Write failing service regressions for the production bug**

Use `FakeRepository`, `_service()`, and `_passage()` to add the named tests below. The first test
uses this complete behavior contract; the remaining tests repeat the same call-boundary assertions
for history, role, and year:

```python
@pytest.mark.parametrize("question", ("周星驰的动作喜剧", "周星馳的動作喜劇"))
def test_bare_person_genre_query_uses_exact_structured_retrieval(
    question: str,
) -> None:
    passages = [
        _passage(
            f"metadata:chow-{index}",
            f"chow-{index}",
            chinese_title=f"周星馳動作喜劇{index}",
            english_title=f"Chow Action Comedy {index}",
            genre="喜劇、動作",
            cast="周星馳、吳孟達",
        )
        for index in range(5)
    ]
    citation_ids = tuple(str(item["passage_id"]) for item in passages)
    answer = "\n".join(
        f"《周星馳動作喜劇{index}》：符合人物及雙類型條件。[metadata:chow-{index}]"
        for index in range(5)
    )
    service, repository, _, _ = _service(passages, answer, citation_ids)
    repository.person_resolutions[question] = ("周星馳", None)

    result = service.answer(question)

    assert len(result.movies) == 5
    assert repository.calls == []
    assert repository.person_resolution_calls == [(RELEASE_ID, question)]
    plan = repository.recommendation_calls[0]
    assert (plan.person_name, plan.person_role) == ("周星馳", None)
    assert plan.genres_all == ("喜劇", "動作")
    assert plan.excluded_movie_ids == ()
```

Add separate functions named
`test_fresh_bare_person_query_resets_prior_person_context`,
`test_bare_person_continuation_inherits_person_genres_and_exclusions`, and
`test_bare_person_role_and_year_queries_preserve_hard_constraints` with the exact scenarios in the
verification contract.

Each test asserts the canonical person, role/year/genre plan fields,
`repository.calls == []`, exact recommendation calls, and no historical person lookup for a
complete current-person reset.

- [ ] **Step 2: Write failing safety and post-validation tests**

Add tests proving:

- `推薦周星馳和成龍的電影` returns a one-person clarification before resolution/embedding;
- `不要周星馳的喜劇` fails closed before resolution/embedding;
- `陳假的動作喜劇` fails closed as an unresolved person and never uses generic search;
- `周星馳的歌曲` and `王家衛的書` retain the domain rejection with zero search/embed/generation;
- a repository candidate carrying only one of `動作`/`喜劇` raises `GroundingError`;
- a known person requested in a role with no matching row resolves identity and returns the existing
  constrained zero-match response rather than “unknown person”.

In `test_rag_db.py`, prove person identity is resolved from the full credit lexicon before the
requested role is applied to the returned `ResolvedPerson`.

- [ ] **Step 3: Run focused tests and verify RED**

Run:

```powershell
uv run pytest -q tests/test_rag_query.py tests/test_rag_db.py `
  -k "bare_person or multiple_governed_people or exclusionary_person or conjunctive_genre or identity_before_role"
```

Expected: the current request is rejected/generic, multi-person silently selects one person, and
role-filtered identity resolution returns no person.

- [ ] **Step 4: Implement one shared service decision path**

Parse the current `PersonQueryShape` after explicit-title/non-movie-object checks. Return a fixed
clarification for multiple or exclusionary shapes. Let positive unique shapes reach the structured
planner; resolve the current person before any history person. Use the same parsed shape when an
unresolved qualifier must fail closed.

Update history-chain replay to recognize Task 1 bare-person plans. Preserve the existing rule that
only a true continuation scans successful history; complete current-person queries reset it.

In `rag_db.py`, match person identity against the full lexicon and apply `infer_person_role()` only
to `ResolvedPerson.role`, leaving the SQL credit predicate to enforce the requested role. Do not
change the database or credit token SQL.

- [ ] **Step 5: Run service/repository suites and verify GREEN**

Run:

```powershell
uv run pytest -q tests/test_rag_query.py tests/test_rag_db.py tests/test_relevance.py tests/test_demo_api.py
```

Expected: all selected tests pass and the immutable 40-case static relevance golden remains
unchanged.

- [ ] **Step 6: Commit Task 2**

```powershell
git add src/hk_movie_rag/rag_db.py src/hk_movie_rag/rag_query.py `
  tests/test_rag_db.py tests/test_rag_query.py
git commit -m "fix: enforce entity-aware person retrieval"
```

### Task 3: Make the production sequence a permanent live acceptance gate

**Files:**
- Modify: `scripts/gcp/live-smoke.py`
- Modify: `tests/test_gcp_demo_scripts.py`
- Modify: `README.md`

**Interfaces:**
- Changes: `_assert_person_recommendation(answer, *, expected_person: str,
  allowed_roles: tuple[str, ...], required_genres: frozenset[str], expected_count: int) -> None`
  replaces the hard-coded celebrity/one-genre assertion.
- Preserves: redacted canonical live-smoke JSON and authenticated same-origin poster checks.
- Adds: exact standalone and two-turn production queries to candidate and stable smoke.

- [ ] **Step 1: Write failing smoke-contract tests**

Extend the fake live server and exact question inventory to require:

1. standalone `周星驰的动作喜剧`;
2. `王家卫的电影` followed by a history-bearing `周星驰的动作喜剧`.

Assert five ordered metadata citations/cards for the second query, canonical `周星馳` credit on
every card, both `動作` and `喜劇` on every card, no Wang Kar-wai carry-over, and existing poster
checks for every available returned poster.

- [ ] **Step 2: Run the smoke tests and verify RED**

Run:

```powershell
uv run pytest -q tests/test_gcp_demo_scripts.py -k "live_smoke"
```

Expected: failure because the exact new question sequence/check keys are absent.

- [ ] **Step 3: Implement generalized live assertions and documentation**

Parameterize the current person assertion, add required-genre subset checking, issue the two new
queries with the exact successful-history payload, and record only redacted boolean/count checks.
Update README query-routing and live-acceptance sections. Correct the documented candidate tag from
the stale `accept-<git-sha-12>` form to the implemented `a-<sha8>-<64-bit nonce>` form.

- [ ] **Step 4: Run deployment-script and UI tests**

Run:

```powershell
uv run pytest -q tests/test_gcp_demo_scripts.py tests/test_gcp_deploy_apply.py
node --test tests/static_app_ui_contract.mjs
```

Expected: all tests pass.

- [ ] **Step 5: Commit Task 3**

```powershell
git add scripts/gcp/live-smoke.py tests/test_gcp_demo_scripts.py README.md
git commit -m "test: gate natural person query sequences"
```

### Task 4: Verify, review, deploy candidate, and promote stable

**Files:**
- Verify only: entire committed worktree
- Produce locally ignored artifacts: `artifacts/rag-demo/v1.2-demo-r2/`

**Interfaces:**
- Consumes: clean committed code and existing `config/rag_demo_r2.yaml`.
- Produces: redacted deployment result containing exact revision, image digest,
  `candidate_smoke_passed=true`, `stable_smoke_passed=true`, and
  `candidate_tag_removed=true`.

- [ ] **Step 1: Run complete local verification**

```powershell
uv lock --check
uv run ruff check .
uv run mypy src
uv run pytest -q
uv run python -m hk_movie_rag.relevance_eval --golden evals/general_relevance_golden.jsonl
git diff --check
git status --porcelain --untracked-files=all
```

Expected: all commands pass and the final command prints nothing.

- [ ] **Step 2: Obtain an independent whole-branch review**

Review the full `main..HEAD` diff for spec compliance, retrieval authorization, history reset,
genre Boolean semantics, fail-closed behavior, secret handling, and deployment smoke coverage.
Resolve every Critical/Important finding and rerun the covering tests.

- [ ] **Step 3: Verify GCP identity without mutation**

Require exactly one active CLI account, project `motionexpaiweb`, valid ADC, Cloud Run read access,
Secret Manager access without printing secret values, Cloud SQL read access, Artifact Registry read
access, Cloud Build access, and Vertex API access. Run both provisioning and deployment `-PlanOnly`
commands. Do not run provisioning `-Apply`.

- [ ] **Step 4: Deploy through the existing owned candidate workflow**

From the clean worktree, preload the existing demo access key without printing it and run:

```powershell
pwsh scripts/gcp/deploy-demo.ps1 `
  -ProjectId motionexpaiweb -Region us-central1 `
  -SourceRoot 'D:\VS_PROJECT\HK_Movie_KnowledgeGraph\RAG' `
  -RagConfigPath 'config\rag_demo_r2.yaml' `
  -DocumentSourceRoot 'D:\VS_PROJECT\HK_Movie_KnowledgeGraph\RAG_INPUTS\v1.2-demo-r2' `
  -ReuseFromReleaseId v1.2-demo `
  -BundleDirectory 'artifacts\rag-demo\v1.2-demo-r2' `
  -Apply
```

Require the script's immutable candidate smoke, ETag-CAS promotion, stable smoke, and candidate-tag
cleanup to succeed. Never run a manual `gcloud run services update-traffic` around this gate.

- [ ] **Step 5: Independently verify the stable URL**

Run `scripts/gcp/live-smoke.py` once more against the exact stable service URL/revision/image digest
from the deployment result. Confirm the standalone and history-bearing person queries, citations,
cards, and protected posters. Remove the temporary access-key environment variable afterward.
