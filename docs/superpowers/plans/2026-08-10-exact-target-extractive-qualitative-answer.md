# Exact-Target Extractive Qualitative Answer Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** Make exact-target qualitative and mixed answers return only bounded, server-selected verbatim PDF spans while retaining deterministic canonical metadata answers.

**Architecture:** Vertex remains an ordered selector of eligible same-movie PDF passage IDs, but its prose is ignored. New pure helpers validate selected IDs, segment and filter normalized passage bodies, rank safe spans by qualitative query terms, and render at most two cited lines. The existing model-prose validation path remains unchanged for general/non-exact queries.

**Tech Stack:** Python 3.11, dataclasses, regular expressions, pytest, Ruff, mypy.

## Global Constraints

- Work only in `D:\VS_PROJECT\HK_Movie_KnowledgeGraph\RAG\.worktrees\rag-r2-task3`.
- Do not modify cloud resources, release artifacts, manifests, or deployment state.
- Ignore `GeneratedAnswer.answer_markdown` for exact-target qualitative branches.
- Apply extractive rendering only when exactly one movie resolves; multi-title
  qualitative comparisons retain the existing general generation path, canonical-only
  queries remain deterministic metadata, and mixed/ambiguous queries fail controlled.
- Do not blanket-reject four-digit years, canonical names, or genre words.
- Render at most one excerpt per selected passage, at most two lines, and at most 240 characters per excerpt.
- Keep general/non-exact generation behavior unchanged.

---

### Task 1: Lock the extractive authority contract with failing regressions

**Files:**
- Modify: `tests/test_rag_query.py:1227-1460`

**Interfaces:**
- Consumes: existing `_service()` fake repository/generator setup.
- Produces: regressions for selector-only prose handling, safe verbatim extraction, controlled insufficiency, selected-ID validation, and the real FGBR genre fixture.

- [ ] **Step 1: Update the FGBR fixture and write failing end-to-end tests**

Set the metadata fixture genre to `喜劇、家庭、奇幻` and make the PDF body contain a safe sentence with `喜劇張力` plus a separate field-labelled/runtime sentence. Replace blacklist-era exception assertions with these contracts:

```python
result = service.answer("富貴逼人的視覺或聲音如何營造喜劇感？")
assert "喜劇張力" in result.answer_markdown
assert "虛構者" not in result.answer_markdown
assert "1988" not in result.answer_markdown
assert result.answer_markdown.endswith("[pdf:fgbr:p3]")
```

Add an unsafe-only passage case that returns the fixed controlled insufficiency text, and parametrized unknown/duplicate/non-PDF selected-ID cases that raise `GroundingError`.

- [ ] **Step 2: Run the focused regressions and confirm RED**

Run:

```powershell
uv run pytest -q tests/test_rag_query.py -k "exact_target or qualitative or mixed or simplified"
```

Expected: failures show exact-target paths still return or validate Vertex prose and lack the new insufficiency behavior.

- [ ] **Step 3: Inspect the failure signatures**

Confirm failures are behavioral mismatches, not fixture-construction or repository setup errors. Correct only test mistakes before implementation.

---

### Task 2: Implement citation-only selection and deterministic PDF extraction

**Files:**
- Modify: `src/hk_movie_rag/rag_query.py:962-1045`
- Modify: `src/hk_movie_rag/rag_query.py:1947-2026`
- Modify: `src/hk_movie_rag/rag_query.py:2568-2730`
- Test: `tests/test_rag_query.py`

**Interfaces:**
- Consumes: `GeneratedAnswer`, `EvidencePassage`, `_normalized_excerpt_text()`, `_contains_uri_or_path()`, and `_build_citations()`.
- Produces: `_validated_selected_pdf_citation_ids(generated, evidence) -> tuple[str, ...]` and `_extractive_qualitative_answer(question, citation_ids, evidence) -> tuple[str, tuple[str, ...]]`.

- [ ] **Step 1: Add the selector validator**

Implement a validator that checks only the `GeneratedAnswer` type and `citation_ids`: tuple, non-empty strings, unique, all IDs present in the eligible PDF evidence, and all eligible/selected passages belonging to exactly one movie. It must never inspect `answer_markdown`.

- [ ] **Step 2: Add bounded segmentation and filtering helpers**

Normalize CJK layout whitespace, split deterministically on line/sentence punctuation, prove every candidate is a substring of the normalized selected passage, and reject candidates that are empty, over 240 characters, runtime claims, URI/path noise, or explicit metadata/header/field-labelled claims. Keep legitimate years, names, and `喜劇張力` eligible.

- [ ] **Step 3: Add stable qualitative ranking and rendering**

Derive normalized query terms after removing exact movie IDs/titles and generic question glue. Rank safe segments by descending term match count, then selected-passage order and source-segment order. Select at most one segment per passage and two total; render each line as `<verbatim span>[<passage_id>]`. Return a fixed controlled insufficiency with no citation IDs when nothing survives.

- [ ] **Step 4: Route exact-target branches through the helpers**

For qualitative-only and mixed branches, call Vertex with PDF-only evidence, validate selected IDs, ignore its prose, and server-render the result. Mixed answers keep canonical metadata first and use only safe qualitative citation IDs. If extraction is insufficient, qualitative-only returns no citations/cards; mixed retains metadata citations/cards and appends the fixed qualifier.

Make `_exact_target_authority()` decline extraction when the resolved target count is
not exactly one: canonical-only queries still return metadata authority, qualitative
comparisons return `None` for general generation, and mixed/canonical-ambiguous queries
return a blocked authority before generation.

- [ ] **Step 5: Run focused tests and confirm GREEN**

Run:

```powershell
uv run pytest -q tests/test_rag_query.py -k "exact_target or qualitative or mixed or simplified"
```

Expected: all selected regressions pass.

---

### Task 3: Normalize simplified canonical intent aliases without regressions

**Files:**
- Modify: `src/hk_movie_rag/rag_query.py:2493-2540`
- Modify: `tests/test_rag_query.py:1436-1490`

**Interfaces:**
- Consumes: `normalize_query_text()` and `_remove_intent_phrase()`.
- Produces: normalized `_canonical_metadata_intents()` behavior for the question, exact titles/IDs, and field aliases.

- [ ] **Step 1: Add failing simplified-title canonical tests**

Add parameterized `临时演员` queries for `上映日期` and `主演` and assert metadata-only citations plus zero generator calls.

- [ ] **Step 2: Run the simplified-title tests and confirm RED**

Run:

```powershell
uv run pytest -q tests/test_rag_query.py -k "simplified and canonical"
```

Expected: the simplified exact title is not cleanly removed or the intent is not classified as deterministic metadata.

- [ ] **Step 3: Normalize every participant in intent removal**

Normalize the history-stripped question first, normalize exact target IDs/titles before removal, and normalize each metadata intent alias before matching/removal. Preserve longest-first matching and the existing safe residual-token gate.

- [ ] **Step 4: Run the focused tests and confirm GREEN**

Run:

```powershell
uv run pytest -q tests/test_rag_query.py -k "simplified or canonical_metadata_intents or exact_title"
```

Expected: all selected tests pass and the generator is unused for canonical queries.

---

### Task 4: Verify preserved behavior and commit

**Files:**
- Modify only if a proven regression requires a scoped correction: `src/hk_movie_rag/rag_query.py`, `tests/test_rag_query.py`

**Interfaces:**
- Consumes: completed Tasks 1-3.
- Produces: machine-checked branch ready for parent review.

- [ ] **Step 1: Run the full query module tests**

```powershell
uv run pytest -q tests/test_rag_query.py
```

- [ ] **Step 2: Run static checks**

```powershell
uv run ruff check src/hk_movie_rag/rag_query.py tests/test_rag_query.py
uv run mypy src
```

- [ ] **Step 3: Run the repository test suite**

```powershell
uv run pytest -q
```

- [ ] **Step 4: Inspect the final diff and worktree**

```powershell
git diff --check
git diff --stat
git status --short
```

Confirm only the approved design/plan, query implementation, and focused tests changed.

- [ ] **Step 5: Commit the implementation**

```powershell
git add -- src/hk_movie_rag/rag_query.py tests/test_rag_query.py docs/superpowers/plans/2026-08-10-exact-target-extractive-qualitative-answer.md
git commit -m "fix: render exact-target analysis extractively"
```
