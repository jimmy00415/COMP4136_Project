# Core HK Movie source publication

Documentation update: the [complete course README](../README.md) and [final report](../report/COMP4136_HK_Movie_RAG_Report.pdf) now consolidate the unchanged implementation and recorded studies. The no-PDF statements below describe earlier publication stages. No presentation, course-platform submission or human approval is claimed.

Only the Hong Kong Movie RAG system is published here. The former memory experiment, its source, frozen TEST data, model files and report artifacts are not part of this Git tree.

The application's 33 Python files, browser UI and packaged policies/migrations/schemas retain the exact source-repository blobs from commit 7b1b87002a0b0bc3548fc365a46095c00ea683da. The destination repository's original LICENSE is preserved. Documentation adds coursework setup and provenance. Two configuration tests now use existing controlled input paths while retaining actual release/GCP settings, so a source checkout does not require private source files for those unit tests.

Use Python 3.13, matching Docker. Python 3.11 fails to import the original archive module's Buffer API. The source package's inherited broad Python version declaration has not been silently rewritten.

Verified locally before publication:

- Frozen uv dependency installation with Python 3.13.
- 171 configuration/deployment-script tests passed.
- 11 offline course evaluator unit tests passed. These use mocked transports and send no live requests.
- Staged core files and Git destination checked; credential-pattern scan found no matches.

External-data integration tests require the governed film release and PDFs and are not claimed to pass in this source-only checkout. This Git push does not represent course-platform submission or human approval.

## 5 October challenge supplement

The new paired evaluation adds 60 frozen turns per implementation, a local BM25 baseline with the same configured generator, strict output checks, actual arm-specific history, and offline explicit AI review. The corrected 120-call run and its parent protocol failure are disclosed in [CHALLENGE_RESULTS.md](../course/CHALLENGE_RESULTS.md). Root AI reviewed all 120 outputs/errors: deployed system 41/60, baseline 44/60; dialogue 18/20 versus 8/20. This is an author-selected implementation comparison, not an independent holdout or causal retriever ablation.

Public files include the 60 cases, safe projections of actual answers, explicit AI verdicts and reviewed summaries. Full catalog, raw operating/provider receipts, original study inputs and credentials remain external. Application source, release and cloud resource configuration are unchanged. No PDF or presentation was generated. The new focused course suite has 33 tests; the earlier 171 configuration/deployment checks are historical validation, not a newly rerun full integration suite. Human audits are pending and submission_ready remains false.
