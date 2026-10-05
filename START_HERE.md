# COMP4136 Hong Kong Movie RAG

The author developed this project and its engineering contributions for COMP4136. This is the core source-code repository. It contains the application, UI, database contracts, deployment tools, source tests and course evaluation code. The retired personal-memory experiment is excluded.

## Install and test

Use Python 3.13 and uv for the full application, matching the Dockerfile. The inherited package metadata says Python 3.11+, but the archive module's `collections.abc.Buffer` import cannot load on Python 3.11. From this repository root:

```shell
uv sync --frozen --python 3.13
uv run pytest -q --ignore=tests/integration
uv run python -m unittest discover -s course -p test_simple_eval.py
uv run pytest -q course/test_simple_eval.py course/test_challenge_eval.py course/test_review_challenge.py
```

The offline source tests use controlled fixtures. `tests/integration` additionally requires the externally held governed release/PDF inputs and is excluded from this offline command. A full `pytest` run without those inputs fails; this is not a claim that external-data integration tests have passed. Course unit tests use mocked HTTP transports and do not invoke cloud models. The standalone course evaluator itself supports Python 3.11+.

## Run the application

The backend requires PostgreSQL with pgvector, a populated release, and Google Cloud credentials. `.env.example` lists configuration names with empty password/access-key fields. Do not commit `.env` or credentials. The Dockerfile builds the API/UI service. The engineering README explains the full data and deployment contract.

The raw movie catalog, PDFs, posters, cloud receipts and private operating history are outside this core-code publication. The course evaluation scripts require an external `course/data/catalog.jsonl`, fixed cases and their `course/preparation/input-pins.json`. The offline unit tests above do not require these inputs. Data preparation code is provided in `course/prepare_cases.py`; the full live evaluator is `course/run_simple_eval.py`.

## Course diagnostic result

The completed 4 October 2026 pass used 30 sequential service requests: all HTTP 200; 28/30 original automatic proxy passes. Two questions about 英雄本色 prompted clarification because both 1973 and 1986 films exist. The median observed response time was 0.959 seconds and maximum 2.962 seconds. These are small diagnostic results, not a general accuracy benchmark or a causal comparison.

Root AI reviewed all responses. Human audit remains pending. Names, student numbers and group details remain blank at the author's request. No report PDF or presentation is included.

## Challenge evaluation and paired baseline

The separate 5 October study covers 60 harder turns per implementation: ambiguous titles/explicit IDs, Traditional/Simplified variants, compound recommendations, evidence boundaries and ten two-turn dialogues. Read [the full results](course/CHALLENGE_RESULTS.md), [reviewed summary](course/challenge_summary.json), [120 actual answer projections](course/challenge_answers.jsonl) and [explicit Root AI audit](course/challenge_review.json).

After the disclosed citation-adapter correction, Root AI task completion was 41/60 for the deployed system versus 44/60 for BM25 plus the same configured generator; dialogue was 18/20 versus 8/20. This supports a dialogue advantage on these selected tasks, not overall superiority or a causal module effect. Both request failures remain counted. The original 30-case study is unchanged. Human audit remains pending; submission_ready is false.

The three course test files above contain 33 offline tests and require no catalog or credentials. Live reruns require the external catalog with the report's exact hash, the existing Google Cloud account and an unused output directory. `course/challenge_eval.py --catalog /path/to/catalog.jsonl --output /path/to/new-run` defaults to configuration checks; `--execute` explicitly enables paid model/service calls. `course/review_challenge.py` works offline on complete local receipts. Do not regenerate cases or substitute data and label that the same run.

## Source provenance

The application source comes from the author's course project repository at commit `7b1b87002a0b0bc3548fc365a46095c00ea683da`: https://github.com/jimmy00415/HK_Movie_RAG_Chatbot/tree/7b1b87002a0b0bc3548fc365a46095c00ea683da . This is not a prior-course assignment being resubmitted. AI assistance supported the evaluation harness, testing and repository preparation.
