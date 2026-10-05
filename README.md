# Hong Kong Movie RAG — COMP4136

A Chinese-language Hong Kong cinema assistant for **evidence-grounded factual questions, constrained recommendations and conversational follow-ups**. This is the author's native COMP4136 project, not a previously submitted assignment.

The project combines a governed film release, structured query handling, PostgreSQL/pgvector retrieval, Vertex AI generation and citation validation. Its course evaluation compares the deployed implementation with a simple **BM25 + LLM baseline** on the same questions.

**Main finding:** the deployed system completed more of the selected dialogue turns, but did **not** outperform the baseline overall. Both gains and failures are preserved.

## Read this first

| Resource | Purpose |
|---|---|
| [Final report](report/COMP4136_HK_Movie_RAG_Report.pdf) | Methods, experiments, cases, limitations and references |
| [Editable report](report/FINAL_REPORT.md) | Complete report text and figure captions |
| [Challenge results](course/CHALLENGE_RESULTS.md) | Detailed Chinese analysis and exact run hashes |
| [Actual answer records](course/challenge_answers.jsonl) | 120 safe projections of real outputs/errors |
| [AI review](course/challenge_review.json) / [summary](course/challenge_summary.json) | Per-record verdicts, automatic scores and paired statistics |
| [START_HERE](START_HERE.md) | Quick setup and source provenance |
| [Engineering guide](docs/ENGINEERING_GUIDE.md) | Original detailed data-build, release and deployment procedures |

Author name, student ID and group number remain blank at the owner's request. Human audit and course-platform submission are pending. No presentation is included.

## Problem and scope

Users may ask who directed a film, request three films satisfying several conditions, switch a decade or director in a follow-up, or refer to the second movie returned. The assistant must recognize duplicate titles and avoid inventing unsupported information.

The evaluated release contains **4,659 films**. Metadata includes movie ID, Chinese/English title, date, director, cast, genre, production information and tier. Metadata agreement is the reference; it is not independent verification of every real-world film fact.

The deployed release additionally contains **five PDFs / 21 passages**. This study evaluates metadata tasks; all observed citations were metadata. PDFs remain in the service and were not supplied to the baseline. PDF interpretation, poster quality, concurrency and production readiness are outside this evaluation.

## Architecture

![Architecture and evaluation boundary](report/figures/architecture.png)

1. The browser sends a question and bounded history to FastAPI.
2. Domain, entity and history handling identifies film/person context, clarification needs and recommendation constraints.
3. The release-bound repository supplies structured or vector-retrieved evidence. Canonical facts can be rendered directly; other supported requests use the configured Vertex generator.
4. Grounding checks validate citation identities and derive movie cards from selected evidence. Unsupported requests receive clarification or refusal.
5. The evaluator sends the same questions to both implementations, maintaining each arm's **own actual history**.

Rules, database retrieval and generation all contribute. A successful API answer is not automatically an LLM call or proof of an individual module's causal benefit.

| Component | Location |
|---|---|
| API and UI | `src/hk_movie_rag/demo_api.py`, `src/hk_movie_rag/static/` |
| Answer orchestration/grounding | `src/hk_movie_rag/rag_query.py` |
| Query/recommendation/history parsing | `src/hk_movie_rag/retrieval.py` |
| PostgreSQL/pgvector | `src/hk_movie_rag/rag_db.py`, `migrations/` |
| Vertex clients | `src/hk_movie_rag/vertex_clients.py` |
| Release/evidence contracts | `rag_bundle.py`, `policies/`, `schemas/` |
| Course baseline/evaluator | `course/challenge_eval.py`, `course/prepare_challenge.py` |
| Offline audit | `course/review_challenge.py` |

## Install and test

Use **Python 3.13** and **uv**, matching Docker. Inherited package metadata declares Python 3.11+, but the original archive module imports `collections.abc.Buffer`; the complete application cannot load on Python 3.11.

```shell
git clone https://github.com/jimmy00415/COMP4136_Project.git
cd COMP4136_Project
uv sync --frozen --python 3.13

# Course tests: no catalog, credentials or live model calls.
uv run pytest -q course/test_simple_eval.py course/test_challenge_eval.py course/test_review_challenge.py

# Broader source tests, excluding external-release integration tests.
uv run pytest -q --ignore=tests/integration
```

The focused course suite was verified at **33 passing tests**. Earlier publication verified 171 configuration/deployment-script tests. These are stated suite results, not a claim that all external-data integration tests passed. A full pytest run without external release inputs is not expected to pass.

The repository provides application code, controlled fixtures, policies, migrations, Dockerfile and dependency lock. A populated database, full catalog, raw PDFs/posters, credentials and complete operating receipts are external.

### Run the backend

A real backend needs populated PostgreSQL/pgvector, the matching verified release, Google Cloud credentials and serving/policy identities. Copy `.env.example` only as a configuration-name template and supply environment variables securely. It contains historical release values; **it is not the evaluated R3 configuration**. Evaluated pins are in `course/run_simple_eval.py` and the report.

After supplying all matching data and environment variables:

```shell
uv run uvicorn hk_movie_rag.demo_api:app --host 127.0.0.1 --port 8080
```

This command reads the process environment; it does not automatically load `.env`. Follow the engineering guide for ingestion/deployment. Installing source does not provision cloud resources.

The evaluated [Cloud Run technical demo](https://hk-movie-rag-demo-4l6lw3rnaa-uc.a.run.app) is externally operated, not a guaranteed permanent grading endpoint. Offline tests and published observations remain inspectable without it.

## API

| Endpoint | Function |
|---|---|
| `GET /` | Browser chat |
| `GET /health` | Health |
| `GET /api/config` | Nonsecret release/model/policy/serving configuration |
| `POST /api/chat` | Answers/recommendations |
| `GET /api/posters/{movie_id}` | Validated same-origin poster proxy |

Illustrative request, not an additional experiment:

```json
{
  "question": "推薦三部1990年代吳宇森導演的動作電影。",
  "history": []
}
```

Responses contain `answer_markdown`, `citations` and `movies`. History uses `question`, `answer` and `movie_ids`, bounded to four exchanges and 12,000 characters. Citation identity validation does not guarantee correct parsing of natural-language conditions.

## Evaluation

### Original 30-case diagnostic

The unchanged 4 October study sent 30 sequential requests, all HTTP 200; the original mechanical proxy passed **28/30**. Two other responses clarified an ambiguous title but did not exercise their intended tasks. This selected, easier study has no baseline and does not establish overall accuracy.

### Paired 60-turn challenge

The 5 October study freezes 60 turns per arm: ambiguity/explicit IDs (10), five Traditional/Simplified pairs (10), compound recommendations (10), evidence/domain boundaries (10), and ten two-turn conversations (20).

The baseline uses local BM25 (`k1=1.5, b=0.75, top_k=8`), uniformly serialized metadata, generic OpenCC normalization, and Chinese unigrams/bigrams. Vertex uses the same **configured** generator, `gemini-3.5-flash-lite`, an evidence-only prompt, 1,024 output tokens and one SDK attempt. No gold or full-corpus eligible-ID list enters inference.

Scoring checks facts, identity/citations, all recommendation conditions, count, non-repetition and appropriate clarification/refusal. Each arm receives its own actual previous outputs. Root AI explicitly reviewed all 120 outputs/errors; a second AI reviewer independently checked the evidence and conclusions. Human review remains pending.

| Family | Turns | Deployed system | BM25 + LLM |
|---|---:|---:|---:|
| Ambiguity and explicit IDs | 10 | 5/10 | 10/10 |
| Traditional/Simplified variants | 10 | 8/10 | 10/10 |
| Compound recommendations | 10 | 2/10 | 6/10 |
| Evidence/domain boundaries | 10 | 8/10 | 10/10 |
| Dialogue | 20 | 18/20 | 8/20 |
| **Total, AI-reviewed** | **60** | **41/60 (68.3%)** | **44/60 (73.3%)** |

![Reviewed task completion by family](report/figures/family_results.png)

Mechanical scores were 42/60 versus 38/60; seven explicit audit overrides explain the final totals. Both arms had one request failure, retained in the denominator. The system-minus-baseline difference is −5.0 percentage points; its descriptive clustered-bootstrap interval spans −24.6 to +15.0 points.

This supports a dialogue advantage on the selected tasks, not overall superiority. Different prompts, deterministic paths, internal retries and evidence handling make this an **implementation comparison**, not a causal retriever ablation or random generalization estimate.

### Disclosed protocol repair

The first completed run rejected legitimate bare IDs in the baseline citation adapter and lost useful dialogue history. Its 120 records and scores are preserved locally but cannot establish method superiority. The correction only canonicalizes IDs actually in retrieved evidence; answers, cases, model, prompt, retrieval parameters and scoring were unchanged.

The report uses the separately frozen corrected run. The same questions were observed during diagnosis, so it is **not an unseen holdout**. A separate package-name startup failure dispatched zero benchmark calls. Failed runs were not silently replaced or pooled.

## Reproduce and inspect

Tracked cases: `course/challenge_cases.jsonl`, SHA-256 `2ab256dc99112b6864f9c41f51f77d216ebff8aeccfa6458c30107f54787e7f5`.

External catalog: 4,659 records, SHA-256 `1dbea26150a816ae4fc4d189a2a08e39628cbee997f3966b0dd5cd16843e907b`.

```shell
# Default: configuration GETs only; no chat/generation.
uv run python course/challenge_eval.py --catalog /path/to/catalog.jsonl --output /path/to/new-run

# Explicitly authorized NEW study; output directory must not exist.
uv run python course/challenge_eval.py --catalog /path/to/catalog.jsonl --output /path/to/new-run --execute
```

The frozen study is complete. New calls yield new observations, not reproductions of identical stochastic answers. The runner keeps access tokens private and suppresses credential-bearing errors. Do not commit authentication files.

The optional `--smoke` has a recorded directory-ordering minor: an existing path is checked after inference. Use a fresh path. It did not affect the recorded study.

Full local receipts support offline `course/review_challenge.py`. Public projections are not substitutes for its complete raw format. Report figures use tracked reviewed summaries and actual projections. See [report build instructions](report/BUILD.md) for document dependencies and fonts.

## Limitations and priorities

- Explicit-ID and some natural metadata queries are over-refused.
- Chinese year ranges can collapse to the first year; genre intersections can be misinterpreted.
- Some director-switch/no-repeat follow-ups still fail.
- BM25 can retrieve too few eligible films; its reader can ignore co-director/alias evidence already retrieved.
- Selected short conversations and AI judgments do not establish population accuracy, human satisfaction or absence of hallucination.
- Timings are not throughput/SLO measurements. Production token usage is unavailable, so no cost comparison is claimed.

Production behavior was not modified during evaluation or report preparation. Repairs should retain the reported evidence and be measured as a separate version.

## Authorship and submission

The owner confirms that the system and engineering were developed for COMP4136 and were not submitted for another course. Native source is preserved from [commit 7b1b870](https://github.com/jimmy00415/HK_Movie_RAG_Chatbot/tree/7b1b87002a0b0bc3548fc365a46095c00ea683da). Evaluated harness checkpoint: `6365c9f`. Published evidence: [commit f405285](https://github.com/jimmy00415/COMP4136_Project/tree/f4052852e802db88762fb97aff0e7dab97c7ceb7).

AI assistance supported the harness/cases, analysis, auditing, figures and documentation; the report discloses it. AI is not a human member. Names/IDs/group number remain unfilled, and no registration or Moodle submission is claimed. GitHub publication does not replace the required course submission.

Only HK Movie material is published; the retired memory experiment is excluded. The existing [LICENSE](LICENSE) is retained and does not independently grant rights to external metadata, posters or PDFs.
