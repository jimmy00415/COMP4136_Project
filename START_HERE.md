# COMP4136 Hong Kong Movie RAG

Start with the [complete README](README.md), [final report PDF](report/COMP4136_HK_Movie_RAG_Report.pdf) or [live chatbot](https://hk-movie-rag-demo-4l6lw3rnaa-uc.a.run.app).

The final report compares the repaired serving revision `hk-movie-rag-demo-00001-qfix-0a81de7` with BM25 + LLM in one fresh paired development run: **60/60 versus 50/60**. The system completes more constrained recommendation and dialogue tasks; both methods return catalog-consistent cards and appropriate clarification/refusal. All 120 outputs, reviews, freeze identities, and exact runner are linked in [final paired results](course/FINAL_COMPARISON_RESULTS.md). Known-case and unequal-computation limitations are explicit.

## Install and verify

Use Python 3.13 and uv, matching Docker. The inherited package metadata permits Python 3.11+, but the complete archive module requires Python 3.13's `collections.abc.Buffer`.

```shell
uv sync --frozen --python 3.13
uv run pytest -q tests/test_retrieval.py tests/test_rag_query.py -k "not every_bounded_release_credit"
uv run pytest -q course/test_simple_eval.py course/test_challenge_eval.py course/test_review_challenge.py course/test_query_fix_eval.py course/test_final_paired_eval.py
```

The offline tests use controlled fixtures and do not need cloud credentials. Full release integration and independent catalog audit need external data. Installing code does not populate PostgreSQL, supply credentials or provision cloud resources. See README for backend setup, API contracts, live runner behavior and limitations.

The project and engineering were developed for COMP4136. Only HK Movie material is published. AI assistance and the AI nature of qualitative review are disclosed in the report. Member fields remain blank, human audit is pending, and no course submission or presentation is claimed.
