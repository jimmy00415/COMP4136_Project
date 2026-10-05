# COMP4136 Hong Kong Movie RAG

The author developed this project and its engineering contributions for COMP4136. This is the core source-code repository. It contains the application, UI, database contracts, deployment tools, source tests and course evaluation code. The retired personal-memory experiment is excluded.

The live service now runs the targeted query repair at source commit `0a81de7447b1ddbfaf7f7786bb97d52815469939`. Its separate, previously observed 60-turn development regression passed **60/60**, with all actual answers and reviews in [deployed repair results](course/QUERY_FIX_RESULTS.md). The original paired study and PDF remain historical records; this new measurement is not an unseen holdout or a rerun of the baseline.

## Install and test

Use Python 3.13 and uv for the full application, matching the Dockerfile. The inherited package metadata says Python 3.11+, but the archive module's `collections.abc.Buffer` import cannot load on Python 3.11. From this repository root:

```shell
uv sync --frozen --python 3.13
uv run pytest -q tests/test_retrieval.py tests/test_rag_query.py -k "not every_bounded_release_credit"
uv run python -m unittest discover -s course -p test_simple_eval.py
uv run pytest -q course/test_simple_eval.py course/test_challenge_eval.py course/test_review_challenge.py course/test_query_fix_eval.py
node --test tests/static_app_ui_contract.mjs
```

The relevant application command uses controlled fixtures and excludes one release-credit test needing an external CSV. `tests/integration` additionally requires external release/PDF inputs. Full-repository certification is not claimed. Final checks: 1,464 relevant application tests, 40 course tests and 7 browser-script tests passed. Course unit tests use mocked HTTP transports and do not invoke cloud models. The standalone course evaluator itself supports Python 3.11+.

## Run the application

The backend requires PostgreSQL with pgvector, a populated release, and Google Cloud credentials. `.env.example` lists configuration names with empty password/access-key fields. Do not commit `.env` or credentials. The Dockerfile builds the API/UI service. The engineering README explains the full data and deployment contract.

The raw movie catalog, PDFs, posters, cloud receipts and private operating history are outside this core-code publication. The course evaluation scripts require an external `course/data/catalog.jsonl`, fixed cases and their `course/preparation/input-pins.json`. The offline unit tests above do not require these inputs. Data preparation code is provided in `course/prepare_cases.py`; the full live evaluator is `course/run_simple_eval.py`.

## Course diagnostic result

The completed 4 October 2026 pass used 30 sequential service requests: all HTTP 200; 28/30 original automatic proxy passes. Two questions about 英雄本色 prompted clarification because both 1973 and 1986 films exist. The median observed response time was 0.959 seconds and maximum 2.962 seconds. These are small diagnostic results, not a general accuracy benchmark or a causal comparison.

Root AI reviewed all responses. Human audit remains pending. Names, student numbers and group details remain blank at the author's request. The [final report PDF](report/COMP4136_HK_Movie_RAG_Report.pdf) and [editable report](report/FINAL_REPORT.md) now bring the system and studies together. No presentation is included.

## Challenge evaluation and paired baseline

The separate 5 October study covers 60 harder turns per implementation: ambiguous titles/explicit IDs, Traditional/Simplified variants, compound recommendations, evidence boundaries and ten two-turn dialogues. Read [the full results](course/CHALLENGE_RESULTS.md), [reviewed summary](course/challenge_summary.json), [120 actual answer projections](course/challenge_answers.jsonl) and [explicit Root AI audit](course/challenge_review.json).

After the disclosed citation-adapter correction, Root AI task completion was 41/60 for the deployed system versus 44/60 for BM25 plus the same configured generator; dialogue was 18/20 versus 8/20. This supports a dialogue advantage on these selected tasks, not overall superiority or a causal module effect. Both request failures remain counted. The original 30-case study is unchanged. Human audit remains pending; submission_ready is false.

The four course test files above contain 40 offline tests and require no catalog or credentials. Live reruns require the external catalog with the report's exact hash and an unused output directory. The historical `course/challenge_eval.py` deliberately pins the older revision and will reject the currently repaired service. Use the [separate current query-fix runner](course/QUERY_FIX_RESULTS.md#inspect-and-reproduce): default GET-only validation; `--execute` explicitly enables a new billable run. `course/review_challenge.py` works offline on complete original receipts. Do not regenerate cases or substitute data and label that the same run.

## Source provenance

The initial application source comes from the author's course project repository at commit `7b1b87002a0b0bc3548fc365a46095c00ea683da`: https://github.com/jimmy00415/HK_Movie_RAG_Chatbot/tree/7b1b87002a0b0bc3548fc365a46095c00ea683da . The subsequent query repair is separately versioned in this repository. This is not a prior-course assignment being resubmitted. AI assistance supported query fixes, evaluation, testing, documentation and repository preparation.
