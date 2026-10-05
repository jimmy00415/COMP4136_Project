# Release-title query routing implementation plan

**Goal:** Make natural questions that safely name a Release movie reach scoped RAG retrieval and
grounded generation, without weakening the existing out-of-domain and evidence-authority gates.

**Architecture:** Separate deterministic Release-entity recognition from question-topic
classification. Extend only the structural title-span resolver, then preserve the existing
target-scoped search, metadata/PDF authority partition, citation validation, poster authority, and
candidate/stable deployment sequence.

## Task 1: Lock the observed defect with failing tests

- Add table-driven resolver cases for Simplified/Traditional conversational lead-ins and possessive
  qualitative questions about `富貴逼人`.
- Add negative cases for short-title collisions and title-owned non-movie objects.
- Add an end-to-end `QueryService.answer` regression that proves the reported question reaches
  target-only retrieval and returns PDF citations plus the correct movie card.
- Run the new tests before production edits and record the expected failures.

## Task 2: Decouple safe title resolution from topic vocabulary

- Introduce a bounded conversational title-lead-in grammar in `retrieval.py`.
- Update `_explicit_title_spans` so an undelimited title can be accepted through structural title
  grammar without requiring `_TITLE_CONTEXT_TERMS`.
- Keep longest-match dominance, residual non-movie masking, and current strict short/ASCII title
  handling intact.
- Run resolver and query-service regressions until green.

## Task 3: Close the deployment acceptance gap

- Add the reported query structure and exact expected movie/citation source contract to
  `scripts/gcp/live-smoke.py`.
- Update script tests so missing, unrelated, metadata-only, or citation-free responses fail.
- Do not edit `deep_document_cases_v1_2_demo_r2.jsonl` or the published relevance policy digest.

## Task 4: Align citations, cards, posters, and history anchors

- Add a failing generic-search regression with three retrieved movies and one selected citation.
- Build generic movie cards only from passages selected by validated citation IDs.
- Add a follow-up regression proving the cited movie is the only pronoun-resolution anchor.
- Leave recommendation and bounded-analysis branches unchanged because they already require their
  full selected evidence set.

## Task 5: Verify locally

- Audit every active Release title through the shared Python/SQL equivalence mapping; require zero
  verbatim or Simplified-to-HK key mismatches, deterministic non-cascading replacements, and explicit
  ambiguity handling for any cross-title collision.
- Exercise compositional lead-ins, common short-title negatives, duplicate-title clarification,
  exact-raw orthographic precedence, and explicit non-movie residual objects.
- Run focused retrieval, relevance, query, API, and GCP script tests.
- Run Ruff and mypy.
- Run the complete pytest suite with sufficient timeout and inspect any slow-test boundary.
- Run `git diff --check` and review the final diff for unrelated changes.

## Task 6: Deploy and verify live

- Use the existing immutable R2 bundle and existing database release; build only a new code image.
- Run the existing deployment script so ingest resume/no-op, poster verification, zero-traffic
  candidate smoke, ETag-CAS promotion, stable smoke, rollback guard, and tag cleanup remain enforced.
- Independently query the stable URL with the reported question and paraphrase/OOD controls.
- Record revision, immutable image digest, response movie ID, PDF citation IDs, and poster HTTP
  result without exposing access codes or credentials.
