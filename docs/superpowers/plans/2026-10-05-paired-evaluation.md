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

- [x] Add meaningful failing tests for retrieval, grounded output validation, scoring, and arm-specific histories.
- [x] Implement deterministic cases, BM25 baseline and paired runner; pass focused tests.
- [x] Validate catalog feasibility, masked CLI authentication and deployed release; smoke the baseline once outside the benchmark.
- [x] Freeze exact inputs and code, run the real 120-call paired study once, retain all results (separate corrected run justified below).
- [x] Review every response pair and retrieval evidence; write honest quantitative/qualitative results and limitations.
- [x] Fresh whole-change review, tests and hashes. Publication verification is recorded in the remote delivery receipt after the final commit.

## Rulings and ledger

- Ruling: execute without another design approval — the user explicitly requests thinking, planning and finishing both steps and has delegated implementation. The design above makes that decision reviewable.
- Ruling: reuse the isolated course submission clone on a codex branch — it already targets the correct HK Movie repository; a worktree of the root repository would bring in the retired project.
- Ruling: no new cloud resources — the current demo is reachable and the existing account supplies a real token. Only bounded model inference is needed.
- Ruling: compare implementations, not claim a causal routing benefit — prompts and runtime paths cannot be made identical without changing the deployed system. The report must state this limitation.
- Ruling: preserve raw data locally, publish evaluation code, selected questions/gold and summaries — full catalog and operational receipts stay outside tracked outputs. Publishing updated HK evaluation code is within the user's existing course-repository authorization.
- Pre-run independent reviewer found four mechanical false-positive classes and one identical language pair. Representative RED tests confirmed the bugs; fixes and explicit semantic audit rules were added before any benchmark dispatch. Five deployed PDFs remain present; missing-evidence targets exclude those five films. Ordinary metadata questions can concern them but require metadata citations. No PDF evidence is supplied to the baseline.

## Completion evidence

Actual evidence is recorded below. Failed runs remain failed; no substituted scores.

Pre-dispatch start at protocol commit ccbcd5f exited with PackageNotFoundError because the manifest requested opencc-python-reimplemented whereas the installed locked distribution is OpenCC 1.4.1. The output directory was empty: no freeze, responses or benchmark dispatches existed. Preserve a startup failure receipt in that directory. Correct only runtime version lookup, add a regression test, and use a fresh -run1 directory for the one actual benchmark. Cases, prompt, scoring and system pins remain unchanged.

Ruling after run1: the baseline adapter rejected bare movie IDs in citation_ids even when the unmodified answer used the correct metadata passage marker and the movie was actually retrieved. This is an identity-format compatibility defect, not a retrieval or answer error. It also removed real first-turn answers from later baseline histories. Therefore run1 does not fulfill the requested fair implementation comparison; its 43/60 vs 29/60 mechanical totals must not establish method superiority. Preserve all 120 records, original freeze and exact frozen source bytes (commit 28efaa2); do not pool them with a corrected run.

The no-rerun rule was intended to prevent answer-driven optimization. Override it solely for this verified protocol defect to complete the user's requested valid comparison: canonicalize bare IDs only if present in supplied evidence, keep answer text unchanged, continue rejecting unknown IDs and unmarked citations. Add a RED/GREEN regression. The question set, gold, BM25 parameters, tokenization, prompt, model, scoring, release pins and turn order remain byte-for-byte/semantically unchanged. Run a separately frozen citation-adapter-v2 once in a new -run2 namespace with real arm-specific history, and record the parent hashes. Do not claim an unseen held-out benchmark: the same author-selected questions were already observed during diagnosis. No scores are relabeled in run1 and no science results are hidden.

### Verified execution ledger

- Implementation commits: ccbcd5f (protocol/tests), 28efaa2 (runtime distribution lookup), 6365c9f (validated citation adapter and explicit offline AI audit).
- Before execution, independent AI review exposed four grader false-positive classes and an identical language pair. Representative failures were observed and fixed; both initial and adapter regressions passed before the corrected study.
- Corrected run2 completed 120 attempts, 59 answers per arm, 2026-10-05 10:40:00–10:43:23 UTC. One RemoteDisconnected and one ClientError remain counted, no case retries. Configuration and frozen inputs remained stable.
- Raw run2 SHA-256 ab7edac1319e7281e4f8cabcaec927a8367039e4ba5da6d4abde2abfb3ac10fe. AI audit SHA-256 85b94e4715b2135300b912825be136fadfde6825940f4d12b91c0d3df6c136e0. Cases SHA-256 2ab256dc99112b6864f9c41f51f77d216ebff8aeccfa6458c30107f54787e7f5.
- All 120 actual outputs/errors explicitly reviewed by Root AI. Seven audit overrides preserved separately; raw mechanical scores unchanged. Mechanical 42/60 versus 38/60; reviewed 41/60 versus 44/60; dialogue 18/20 versus 8/20. No overall superiority claim.
- Fresh root focused suite: 33 passed in 0.15s; Ruff check and five-file format check passed; git diff --check passed. Full external-data integration is not claimed.
- Offline delivery verifier reproduced mechanical/AI arms and paired statistics, exact 120 public projections, builder cases, source/parent hashes, and preserved original study hash. Application src/web have no differences from base9129242.
- Ruling: publish the safe 120-answer projection plus explicit audit, not only a summary — reviewers need actual response evidence; full provider/catalog receipts remain external. Cost if wrong: broadened public disclosure of these nonsecret diagnostic questions/answers; no credentials or full dataset are included.
- Root AI review, human_audits pending, submission_ready false. No PDF/presentation or production change.
- Fresh whole-branch reviewer: no Critical/Important findings. Independently reproduced all 120 projections and own histories, every mechanical score, all 59 baseline rankings, quantitative/AI counts, seven overrides, 912 card fields and hashes. Reviewer read every actual answer/error and found no unsupported audit verdict; 33 focused tests passed. AI review, not human review.
- Final: minor (deferred): --smoke checks output-directory existence after inference. Reusing an existing smoke directory incurs inference then fails to write its receipt. This did not affect the actual exclusive smoke/benchmark outputs; leave frozen source unchanged and address before a future runner revision. No extra smoke calls were made.
- Final integration ruling: fast-forward the authorized HK Movie repository main directly, preserving source/evidence bytes — the user already instructed pushing the core course code and authorized autonomous completion. No force push or production change. Cost if wrong: public evaluation changes appear on the coursework main branch; Git history remains recoverable.
- Final checks: 33 tests, Ruff/format, staged diff whitespace check (CRLF evidence explicitly preserved), staged byte hashes and selected-file credential scan passed. Six frozen source/input/evidence files match their staged bytes. Sixteen selected HK files were mirrored to the course workspace with exact hash comparison.
