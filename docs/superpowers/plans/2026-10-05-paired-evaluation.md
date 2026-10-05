# HK Movie challenge evaluation and simple baseline

## Goal and binding constraints

Complete the user's two selected improvements: a harder, outcome-aware evaluation and a paired simple retrieval baseline. Preserve study-01 (28/30 under its original proxy). Produce code, real observations and Markdown analysis; do not produce PDFs. Do not modify the deployed application, its release, or the retired memory experiment.

## Design

Freeze 60 author-selected turns before model execution: ambiguity (10), language variants (10), compound recommendations (10), evidence/domain boundaries (10), and ten two-turn conversations (20). Metadata-derived gold and dynamic history rules are separate from inference inputs. Language variants share a cluster; conversations share a cluster. Gold is checked for feasibility, not selected using model answers.

Compare the pinned deployed implementation with local BM25 (k1=1.5, b=0.75, top eight metadata records) plus Vertex gemini-3.5-flash-lite. Both receive the same questions and the same 4,659-movie metadata universe. Each arm receives its own actual previous outputs. Generic OpenCC normalization and CJK character tokens are available to the baseline, without the application's specialized routing or structured filtering. Baseline generation uses a competent evidence-only prompt, bounded history, 1,024 output tokens, and no manual retries. The deployed system may use deterministic answers/fallbacks and internal retries. This is an end-to-end implementation comparison, not a controlled retriever ablation. Neither arm is given expected answers or eligible IDs.

Run arms in alternating order per turn. Freeze the catalog hash, cases, executable sources, baseline prompt and deployed pins before the one formal run. Store raw outputs and timings; retain transport/provider failures. Stop after three consecutive transport failures in either arm. Do not rerun the benchmark or tune it against observed failures. A separate, non-benchmark smoke request is allowed before the freeze.

Primary scoring checks intended action, target identity, metadata-grounded facts, exact recommendation count/constraints, and history exclusions. Report answer / clarification / refusal / transport behavior separately. Mechanical checks are proxies; Root AI reviews every pair and records limitations. Confidence intervals bootstrap whole conversation/language clusters and remain descriptive for this small author-selected set. No human review or full-course completion claim.

Frozen semantic audit rules: a disclaimer followed by unsupported requested content is a failure, including invented numbers, code, plot or character analysis. A generic confirmation after answering one unqualified version does not resolve title ambiguity. Negating or contradicting the gold fact fails. A refusal with eligible cards is not a successful recommendation. A supported answer may end with an optional follow-up invitation without being a clarification instead of an answer. Apply these rules to both arms identically, preserve mechanical scores, and record every audit override separately with its reason. Only Root AI, not a human, performs this audit.

## Execution plan

- [ ] Add meaningful failing tests for retrieval, grounded output validation, scoring, and arm-specific histories.
- [ ] Implement deterministic cases, BM25 baseline and paired runner; pass focused tests.
- [ ] Validate catalog feasibility, masked CLI authentication and deployed release; smoke the baseline once outside the benchmark.
- [ ] Freeze exact inputs and code, run the real 120-call paired study once, retain all results.
- [ ] Review every response pair and retrieval evidence; write honest quantitative/qualitative results and limitations.
- [ ] Fresh whole-change review, tests, hashes and remote publication verification; deliver links.

## Rulings and ledger

- Ruling: execute without another design approval — the user explicitly requests thinking, planning and finishing both steps and has delegated implementation. The design above makes that decision reviewable.
- Ruling: reuse the isolated course submission clone on a codex branch — it already targets the correct HK Movie repository; a worktree of the root repository would bring in the retired project.
- Ruling: no new cloud resources — the current demo is reachable and the existing account supplies a real token. Only bounded model inference is needed.
- Ruling: compare implementations, not claim a causal routing benefit — prompts and runtime paths cannot be made identical without changing the deployed system. The report must state this limitation.
- Ruling: preserve raw data locally, publish evaluation code, selected questions/gold and summaries — full catalog and operational receipts stay outside tracked outputs. Publishing updated HK evaluation code is within the user's existing course-repository authorization.
- Pre-run independent reviewer found four mechanical false-positive classes and one identical language pair. Representative RED tests confirmed the bugs; fixes and explicit semantic audit rules were added before any benchmark dispatch. Five deployed PDFs remain present; missing-evidence targets exclude those five films. Ordinary metadata questions can concern them but require metadata citations. No PDF evidence is supplied to the baseline.

## Completion evidence

To be filled with actual test output, freeze/response hashes, counts, review findings and the published commit. Failed runs remain failed; no substituted scores.
