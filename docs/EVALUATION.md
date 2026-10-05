# Evaluation and reproducibility

[Project overview](../README.md) · [Complete results](../course/FINAL_COMPARISON_RESULTS.md) · [Final report](../report/COMP4136_HK_Movie_RAG_Report.pdf)

## What was compared

One fresh paired development study ran on **5 October 2026, 21:08:06-21:11:51 HKT**. The deployed system and a BM25 + LLM comparator each received the same 60 turns, yielding **120 retained responses**. Method order alternated by case, and each method used its own actual earlier answers in ten two-turn dialogues.

| Control | Recorded design |
|---|---|
| Questions | Same frozen 60-turn case set, known during development |
| Evidence | Same 4,659-record catalog snapshot |
| Generation | Same configured `gemini-3.5-flash-lite`, 1,024-token output cap |
| Outer attempts | One question-level attempt per method/turn; no replacement or failed-case retry |
| History | Separate actual question/answer/movie-ID history per method |
| Reference isolation | Expected actions, gold fields and eligible-ID sets excluded from inference |
| Identity | Source/import/prompt/data/config hashes frozen before dispatch and checked afterward |

## Comparator

The baseline indexes uniformly serialized metadata with **BM25, k1 = 1.5, b = 0.75, top-k = 8 positive matches**. NFKC/OpenCC normalization, Chinese unigrams/bigrams, and preserved Latin/movie-ID tokens support mixed text. The generator receives retrieved evidence, the current question, and actual history. Its prompt requires all constraints, evidence citations, clarification and refusal. Returned cards use retrieved canonical records after identity checks.

The baseline is evidence-controlled; it is not an intentionally ungrounded chatbot. It does not include the deployed system's title router, structured predicate queries, or deterministic canonical-field answer route.

Internal computation differs. Baseline SDK attempts are one; the deployed application permits three SDK attempts, up to two recommendation outputs, direct metadata rendering, and deterministic fallback. Internal invocation counts are not exposed. The experiment compares complete application behavior, not equal-compute generation or a single retrieval module.

## Scoring and inspection

Mechanical checks examine factual fields, film/citation identities, recommendation predicates, count, uniqueness, and history-dependent exclusions. Root AI then inspected all 120 complete responses against catalog evidence; an independent AI reviewer also inspected answers and narrative facts. Outcomes distinguish correct answers, appropriate clarification, reasonable refusal, and error.

Baseline mechanical scoring accepted **43/60**. Qualitative review corrected seven false negatives:

- **A2a-A5a:** valid clarification expressed outside the automatic phrase rules.
- **B05-B07:** valid refusal of unsupported budget, revenue, or rating questions.

The reviewed score is **50/60**. Original verdicts and explicit correction reasons remain available. Human review is pending.

## Results

![Paired task completion](../report/figures/family_results.png)

| Result | Deployed system | BM25 + LLM |
|---|---:|---:|
| AI-reviewed task completion | 60/60 | 50/60 |
| Correct answers | 44 | 34 |
| Clarifications / refusals | 5 / 11 | 5 / 11 |
| Recommendation-task completion | 24/24 | 14/24 |
| Movie cards / metadata citations | 98 / 98 | 85 / 95 |
| Catalog card-field agreement | 784/784 | 680/680 |
| Median latency | 0.763 s | 1.349 s |
| Observed maximum latency | 51.523 s | 14.430 s |

There are **50 both-pass pairs and ten system-only pairs**, giving a descriptive difference of **16.7 percentage points**. The extra completed tasks occur in compound recommendations and dialogue.

All ten baseline failures are incomplete recommendations: its top-eight evidence contains fewer than three eligible films despite sufficient catalog-wide candidates. A refusal can be responsible within retrieved evidence and still fail a catalog-supported count requirement. These results do not demonstrate baseline hallucination or superior system card factuality: both methods' audited fields match the catalog.

The eight checked card fields are ID, Chinese/English title, release date, director, cast, genre and tier. Agreement establishes consistency with this reference snapshot, not independently verified film history. PDF reasoning, posters, concurrency, costs and unseen generalization are not measured. The system's lower median does not erase its worse observed latency tail.

## Evidence index

| Artifact | Contents |
|---|---|
| [Questions](../course/challenge_cases.jsonl) | Frozen cases and reference criteria |
| [Responses](../course/final_paired_answers.jsonl) | All 120 answers, actual histories and baseline retrieval/generation metadata |
| [Review](../course/final_paired_review.json) | Per-case decisions and retained field audit |
| [Summary](../course/final_paired_summary.json) | Reviewed counts, original scores, overrides and diagnostics |
| [Freeze](../course/final_paired_freeze.json) | Exact runner/import/prompt/catalog/config identities |
| [Measured runner](../course/final_paired_eval.py) | Actual execution source |
| [Runner tests](../course/test_final_paired_eval.py) | Attempt retention, ordering, history isolation and fresh-directory behavior |
| [Deployment summary](../course/query_fix_summary.json) | Serving image and earlier build/promotion receipts |

Earlier experiments are not pooled with this run. Hashes in the report and builder bind the evidence to exact exported bytes.

## Reproduce deliberately

Inspect published records without any model calls. To independently repeat catalog field and eligibility checks, supply the exact external catalog:

```text
Catalog SHA-256: 1dbea26150a816ae4fc4d189a2a08e39628cbee997f3966b0dd5cd16843e907b
Cases SHA-256:   2ab256dc99112b6864f9c41f51f77d216ebff8aeccfa6458c30107f54787e7f5
```

Read the runner's interface:

```bash
uv run --frozen python course/final_paired_eval.py --help
```

Example GET-only preflight, from the repository root; substitute the path to the exact catalog and use an unused output directory:

```bash
uv run --frozen python course/final_paired_eval.py --catalog /path/to/catalog.jsonl --output course/results/my-fresh-paired-run --revision hk-movie-rag-demo-00001-qfix-0a81de7 --image-digest sha256:6c031ad921888783e1167dca78e0138cf45ff4c34b0a17f58a1c9847ada92cbd
```

Without `--execute`, the runner validates the public configuration through GET requests and dispatches no chat or generation calls. An actual paired run additionally needs usable Google CLI credentials for the baseline. The measured runner defaults to the original Windows Google CLI path; use `--gcloud /path/to/gcloud` for another installation. Adding `--execute` creates a new billable run; it does not overwrite evidence, retry failed cases, or change production. The runner uses the configured project and service from its imported evaluation modules. A different service/model/release requires an explicitly designed new study rather than silently changing identity pins.

For document reproduction, follow [report/BUILD.md](../report/BUILD.md). The offline builder validates exact evidence hashes, reconstructs metrics and figures, and creates the PDF without model calls. Changed PDF bytes require fresh full-page visual inspection.
