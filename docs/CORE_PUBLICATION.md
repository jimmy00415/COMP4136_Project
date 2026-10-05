# Core HK Movie source publication

Only the Hong Kong Movie RAG system is published here. The former memory experiment, its source, frozen TEST data, model files and report artifacts are not part of this Git tree.

The application's 33 Python files, browser UI and packaged policies/migrations/schemas retain the exact source-repository blobs from commit 7b1b87002a0b0bc3548fc365a46095c00ea683da. The destination repository's original LICENSE is preserved. Documentation adds coursework setup and provenance. Two configuration tests now use existing controlled input paths while retaining actual release/GCP settings, so a source checkout does not require private source files for those unit tests.

Use Python 3.13, matching Docker. Python 3.11 fails to import the original archive module's Buffer API. The source package's inherited broad Python version declaration has not been silently rewritten.

Verified locally before publication:

- Frozen uv dependency installation with Python 3.13.
- 171 configuration/deployment-script tests passed.
- 11 offline course evaluator unit tests passed. These use mocked transports and send no live requests.
- Staged core files and Git destination checked; credential-pattern scan found no matches.

External-data integration tests require the governed film release and PDFs and are not claimed to pass in this source-only checkout. Live study inputs and receipts remain outside this core publication. This Git push does not represent course-platform submission or human approval.
