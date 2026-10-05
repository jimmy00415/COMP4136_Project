# Final paired development comparison

Goal: report an actual comparison of the repaired HK Movie system and simple BM25 + matched LLM under the same catalog, questions, model identity and task acceptance rules.

The user explicitly requested this comparison after reviewing the system-only final report. This supersedes the report's system-only scope. No production source, data, model, resource or historical benchmark file is changed.

- [x] Inspect baseline and current revision, catalog and case identities.
- [x] Add one fresh paired runner that defaults to GET-only and preserves classifier/validation failures.
- [x] Verify failure-retention tests (red import failure, then passing tests), related course tests (45 final checks passed) and lint.
- [x] Read-only independent AI source review, then freeze runner/imported modules/prompt/cases/catalog/config before one 120-request run.
- [x] Retain all outputs and each arm's actual history, inspect every answer and catalog constraints, distinguish reasonable clarification/refusal from errors.
- [x] Compare family completion, card agreement, constraints, citations and actual failure cases; disclose known-case development scope and unequal internal retry/routing policies.
- [x] Update final report, README, figures and evidence links; render and inspect all13 final pages; prepare verified HK Movie publication to the authorized course repository.

Protocol: each arm receives the same 60 case questions. Alternate arm order; each two-turn session uses its own actual earlier response. One outer request per arm/turn; no failed-case retry or replacement. Baseline retains original BM25 k1=1.5, b=.75, top_k=8, generic OpenCC unigram/bigram tokenizer, evidence-only structured output prompt, same generation model and 1024 output-token cap. Baseline SDK attempts=1; deployed SDK generation permits 3 internally. This is an end-to-end development comparison, not equal-compute inference, a new holdout, a causal ablation, or a guarantee of all-query correctness. Human audit remains pending.

Review risks: history leakage, gold entering inference, silent discarded failures, historical/current revision drift, publication of secrets. Existing helper interfaces, failure-retention tests, source identity freeze and read-only source review address these risks before dispatch.

Final verification: 45 offline course tests and focused Ruff checks passed; six evidence hashes and all13 page renders were verified. Publication uses a normal commit and push, followed by actual remote-byte verification.
