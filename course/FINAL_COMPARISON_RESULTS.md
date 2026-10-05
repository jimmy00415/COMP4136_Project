# Final repaired system versus BM25 + LLM

One fresh paired development run on **5 October 2026, 21:08:06-21:11:51 HKT** completed **120 actual responses**, 60 per arm, one outer request per turn. Calls alternated arm order. Each dialogue used its own actual history. Catalog, questions, generation-model identity and task scoring were shared; no data, code, model or cloud-resource change occurred during the run.

| Family | Deployed system | BM25 + LLM, AI-reviewed |
|---|---:|---:|
| Ambiguity / explicit ID | 10/10 | 10/10 |
| Traditional / Simplified | 10/10 | 10/10 |
| Compound recommendations | 10/10 | 7/10 |
| Evidence / domain boundary | 10/10 | 10/10 |
| Dialogue | 20/20 | 13/20 |
| Total | **60/60 (100%)** | **50/60 (83.3%)** |

The observed difference is **16.7 percentage points**: 50 both-pass pairs, 10 system-only pairs, no baseline-only or both-fail pairs. All ten baseline failures are incomplete recommendations: its top-eight evidence has only 0-2 eligible films despite sufficient catalog-wide candidates. These generally conservative answers are task failures, not proof of hallucination. The system completes all 24 recommendation tasks; baseline completes 14/24.

Mechanical baseline scoring was 43/60. Root AI read all 120 complete answers and corrected seven false negatives: A2a/A3a/A4a/A5a are reasonable clarifications; B05/B06/B07 are reasonable refusals. An independent AI reviewer checked the verdicts and narrative facts. Two baseline answers have minor count-word inconsistencies; M05.2 adds redundant clarification but supplies the requested year. These quality notes are retained; they do not invalidate actual task completion.

| Evidence / latency | System | Baseline |
|---|---:|---:|
| Movie cards / citations | 98 / 98 | 85 / 95 |
| Catalog card-field agreement | 784/784 | 680/680 |
| Median latency | 0.763 s | 1.349 s |
| Maximum observed latency | 51.523 s | 14.430 s |

Eight fields were checked: ID, Chinese/English title, date, director, cast, genre and tier. Agreement is catalog consistency, not independent real-world truth or poster/pilot verification. System latency has a worse observed tail; exact internal model-call count is not exposed.

## Protocol and scope

BM25 uses k1=1.5, b=.75, top_k=8, NFKC/OpenCC and Chinese unigrams/bigrams, same configured generator and 1024 output-token cap. Its evidence-only prompt requires all constraints, citations, clarification and refusal. No gold or complete eligible-ID set enters inference.

Baseline SDK attempts=1. The deployed application permits up to 3 SDK attempts per generation, up to 2 recommendation outputs, and canonical metadata fallback. Therefore this is a whole-application task comparison, not equal-compute generation or a causal retrieval ablation. Known development questions were observed during repair, so no unseen generalization or arbitrary-query guarantee is claimed. Human audit remains pending; submission_ready=false.

## Inspect the real evidence

- [Actual complete answers/history and baseline retrieval/generation metadata](final_paired_answers.jsonl)
- [Explicit Root AI audit and card-field checks](final_paired_review.json)
- [Reviewed aggregates, mechanical scores, overrides and retrieval diagnostics](final_paired_summary.json)
- [Pre-dispatch freeze, operational asymmetries and source identities](final_paired_freeze.json)
- [Exact measured runner](final_paired_eval.py) and [retention/order/history tests](test_final_paired_eval.py)

The runner is GET-only without --execute. New live runs require the exact external catalog, an unused output directory and explicit --execute; no failed-case retry or overwrite is performed. Report authoring is offline. Raw run and source identities are hash-bound in the freeze and report manifest. Earlier observations are not pooled into this final comparison.
