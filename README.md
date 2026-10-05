# Hong Kong Movie RAG - COMP4136

A Chinese-language cinema assistant for evidence-grounded factual questions, constrained recommendations and conversational follow-ups. The owner developed this native project for COMP4136; it is not a previously submitted assignment.

**[Live chatbot](https://hk-movie-rag-demo-4l6lw3rnaa-uc.a.run.app)** | **[Final report PDF](report/COMP4136_HK_Movie_RAG_Report.pdf)** | **[Editable report](report/FINAL_REPORT.md)**

The final report includes a **fresh paired comparison: deployed system 60/60 versus BM25 + LLM 50/60** on the same known development questions, catalog and configured generator. It explains the observed advantage in complete constrained recommendations and dialogue, while showing ties on ambiguity, script variants and evidence boundaries. This is not an unseen-test accuracy estimate or an equal-compute experiment.

## Evidence and deliverables

| Resource | Purpose |
|---|---|
| [Final paired results](course/FINAL_COMPARISON_RESULTS.md) | Family comparison, failure cases, fairness and limitations |
| [Actual paired answers and histories](course/final_paired_answers.jsonl) | All 120 retained responses, baseline retrieval and generation metadata |
| [Root AI review](course/final_paired_review.json) | All per-case verdicts and card-field audit |
| [Reviewed paired summary](course/final_paired_summary.json) | Recountable aggregates and seven transparent scoring corrections |
| [Pre-dispatch freeze](course/final_paired_freeze.json) | Runner, imported modules, catalog, cases, configuration and model identities |
| [Exact measured runner](course/final_paired_eval.py) | GET-only by default; explicit execution creates a fresh paired run |
| [Deployment receipts](course/query_fix_summary.json) | Source/image pins, cloud build and promotion before the paired run |
| [Report build / visual review](report/BUILD.md) | Offline figures, PDF build and actual page inspection |
| [Engineering guide](docs/ENGINEERING_GUIDE.md) | Data-build, release and deployment procedures |

Author name, student ID and group number remain blank at the owner's request. Human audit and course-platform submission are pending. This deliverable contains a report; no presentation is included.

## Problem, data and architecture

Users ask who directed a film, request three movies satisfying multiple predicates, switch a decade or director, or refer to a previous selection. The system must distinguish same-title records and avoid inventing unavailable facts.

The active release contains **4,659 films**, **4,659 metadata passages**, **five PDFs / 21 document passages**, and **4,680 embeddings**. Metadata agreement is the evaluation reference, not independent verification of every film fact. This experiment measures metadata tasks; PDF comprehension and poster correctness are not certified.

![Evidence-controlled system](report/figures/architecture.png)

1. FastAPI receives a question and bounded actual history.
2. Identity, domain and constraint handling chooses a structured or dense-retrieval route.
3. PostgreSQL/pgvector supplies release-bound evidence.
4. Canonical facts are rendered directly; other supported answers use evidence and Vertex AI.
5. Grounding checks expose citations and canonical movie cards. Ambiguity or insufficient support triggers clarification or refusal.

The code changes are generic parser/answering repairs, not model-weight training or hardcoded benchmark answers. Data, embeddings, managed models and existing resource configuration were retained.

| Component | Location |
|---|---|
| API and UI | `src/hk_movie_rag/demo_api.py`, `src/hk_movie_rag/static/` |
| Answer orchestration / grounding | `src/hk_movie_rag/rag_query.py` |
| Query / recommendation / history parsing | `src/hk_movie_rag/retrieval.py` |
| PostgreSQL / pgvector | `src/hk_movie_rag/rag_db.py`, `migrations/` |
| Managed-model clients | `src/hk_movie_rag/vertex_clients.py` |

## Install and test

Use **Python 3.13** and **uv**, matching Docker. Inherited package metadata declares Python 3.11+, but the original archive module imports `collections.abc.Buffer`; the complete application cannot load on Python 3.11.

```shell
git clone https://github.com/jimmy00415/COMP4136_Project.git
cd COMP4136_Project
uv sync --frozen --python 3.13

# Course tests: no catalog, credentials or live model calls.
uv run pytest -q course/test_simple_eval.py course/test_challenge_eval.py course/test_review_challenge.py course/test_query_fix_eval.py course/test_final_paired_eval.py

# Relevant application regressions; the excluded test needs an external CSV.
uv run pytest -q tests/test_retrieval.py tests/test_rag_query.py -k "not every_bounded_release_credit"
node --test tests/static_app_ui_contract.mjs

# Broader source tests, excluding external-release integration tests.
uv run pytest -q --ignore=tests/integration
```

After the deployed repair, **45 course tests**, **1,464 relevant application tests** (one external-catalog test deselected) and **7 browser-script tests** passed. Earlier publication verified 171 configuration/deployment-script tests. A full pytest run without external release inputs is not expected to pass; even `--ignore=tests/integration` needs the external CSV for one release-credit test.

The repository provides application code, controlled fixtures, policies, migrations, Dockerfile and dependency lock. A populated database, full catalog, raw PDFs/posters, credentials and complete operating receipts are external.

### Run the backend

A real backend needs populated PostgreSQL/pgvector, the matching verified release, Google Cloud credentials and serving/policy identities. Copy `.env.example` only as a configuration-name template and supply environment variables securely. It contains historical release values; **it is not the evaluated R3 configuration**. Current revision/image pins and actual cloud readbacks are in [runtime summary](course/query_fix_summary.json).

After supplying all matching data and environment variables:

```shell
uv run uvicorn hk_movie_rag.demo_api:app --host 127.0.0.1 --port 8080
```

This command reads the process environment; it does not automatically load `.env`. Follow the engineering guide for ingestion/deployment. Installing source does not provision cloud resources.

The [Cloud Run technical demo](https://hk-movie-rag-demo-4l6lw3rnaa-uc.a.run.app) now serves revision `hk-movie-rag-demo-00001-qfix-0a81de7`. It is externally operated, not a guaranteed permanent grading endpoint. Offline tests and published observations remain inspectable without it.

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

## Final paired development evaluation

Actual paired requests ran on **5 October 2026, 21:08:06-21:11:51 HKT**, one question-level attempt per arm/turn. Arm order alternates; each dialogue uses its own actual earlier outputs. Every response was retained and read in full by Root AI, with independent AI review. No expected answer or eligible-ID set entered inference.

![Paired task completion](report/figures/family_results.png)

| Task family | Deployed system | BM25 + LLM |
|---|---:|---:|
| Ambiguity / explicit ID | 10/10 | 10/10 |
| Traditional / Simplified variants | 10/10 | 10/10 |
| Compound recommendations | 10/10 | 7/10 |
| Evidence / domain boundary | 10/10 | 10/10 |
| Dialogue | 20/20 | 13/20 |
| **Total** | **60/60** | **50/60** |

The difference is **16.7 percentage points**: 50 both-pass turns and ten system-only turns. All ten baseline failures are incomplete recommendations: the top-eight BM25 context contains fewer than three eligible movies, even though the catalog has sufficient valid candidates. These conservative incomplete answers do not demonstrate hallucination. Recommendation tasks complete at **24/24 versus 14/24**.

Both methods appropriately clarify five and refuse eleven requests. The system gives 44 correct answers; baseline 34. Baseline mechanical scoring initially accepted 43 turns; qualitative review corrects seven wording-related false negatives, with reasons retained in the audit. Both methods' returned cards match the catalog: **784/784 system fields and 680/680 baseline fields**, all metadata citations.

Median latency is **0.763 s versus 1.349 s**, but the system has a worse observed maximum (**51.523 s versus 14.430 s**). The public API does not expose its exact internal model-call count. Baseline uses one SDK attempt; the deployed application permits three SDK attempts, up to two recommendation generations, direct rendering and deterministic fallback. This is a complete-application comparison, not equal-compute retrieval or an isolated module ablation.

These cases were known during development. Earlier observations are not pooled into this report. Unseen accuracy, universal superiority, PDF reasoning, concurrency and cost are not inferred. Catalog agreement is dataset consistency, not independently established real-world truth. Human review remains pending.

## Inspect or reproduce

The raw answers, reviews, summary, freeze and exact runner are published above. Cases: `course/challenge_cases.jsonl`, SHA-256 `2ab256dc99112b6864f9c41f51f77d216ebff8aeccfa6458c30107f54787e7f5`. Independent field/eligibility re-auditing requires the external 4,659-record catalog, SHA-256 `1dbea26150a816ae4fc4d189a2a08e39628cbee997f3966b0dd5cd16843e907b`.

The fresh paired runner defaults to **GET-only configuration validation**. Its `--help` describes required catalog, revision/image pins and a fresh output directory. Adding `--execute` makes a new billable 120-response paired run; it never overwrites retained outputs or retries failed cases. Google credentials and matching release inputs are external. Report authoring needs no model calls.

[Build instructions](report/BUILD.md) explain the separate document environment and fonts. Building verifies exact paired evidence hashes, reconstructs aggregate metrics and generates figures offline. A changed PDF requires fresh full-page visual review.

## Provenance and contribution

The owner confirms that this native project was developed for COMP4136 and was not submitted for another course. Initial source is versioned at [7b1b870](https://github.com/jimmy00415/HK_Movie_RAG_Chatbot/tree/7b1b87002a0b0bc3548fc365a46095c00ea683da); the deployed repair is [0a81de7](https://github.com/jimmy00415/COMP4136_Project/tree/0a81de7447b1ddbfaf7f7786bb97d52815469939).

AI assistance supported repairs, regression tests, evaluation, audit, figures and documentation. AI is not a human member. Names/IDs/group number remain unfilled. GitHub publication does not replace registration or Moodle submission. The retired memory experiment is excluded from this repository. The existing [LICENSE](LICENSE) does not independently grant rights to external metadata, posters or PDFs.
