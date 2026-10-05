# Contributing

Thank you for helping improve HK Movie RAG. Focused bug reports, documentation fixes, regression tests, and domain-retrieval improvements are welcome.

## Report a problem

Open a [GitHub issue](https://github.com/jimmy00415/COMP4136_Project/issues) with:

- The exact question and minimal relevant prior exchanges.
- Expected behavior and the actual answer, citations, or movie IDs.
- Source commit, Python version, and nonsecret release/serving identity where applicable.
- Steps to reproduce with a controlled fixture if possible.

Remove credentials and private data from logs. An evidence limitation, same-title clarification, or unsupported-field refusal may be intended behavior; explain what catalog-supported behavior you expected.

## Propose a change

1. Install the locked Python 3.13 environment using [Getting started](docs/GETTING_STARTED.md).
2. Create a branch for one focused change. Keep application edits separate from external release-data or infrastructure changes.
3. For behavior changes, add a regression that checks the intended rule rather than a benchmark-specific answer. Use controlled fixtures where possible.
4. Run the relevant tests, include their exact scope/results, and update affected documentation.
5. Open a pull request explaining the problem, resulting behavior, and validation. Identify required external data rather than claiming unavailable checks passed.

## Useful checks

```bash
uv run --frozen pytest -q course/test_simple_eval.py course/test_challenge_eval.py course/test_review_challenge.py course/test_query_fix_eval.py course/test_final_paired_eval.py
uv run --frozen pytest -q tests/test_retrieval.py tests/test_rag_query.py -k "not every_bounded_release_credit"
node --test tests/static_app_ui_contract.mjs
```

The latter application subset excludes one external-CSV check. Full release integration requires external inputs. No paid live evaluation or infrastructure change is needed for a documentation contribution.

## Preserve scientific evidence

Published `course/final_paired_*` records, questions, and measured source are frozen evidence. Do not rewrite old answers, scores, prompts, source snapshots, or hashes to make a change look better. Create a new versioned study for a changed implementation or protocol, retaining original automatic judgments and explicit review corrections. See [Evaluation](docs/EVALUATION.md).

Source changes do not imply authorization to deploy against the maintained cloud demo. Describe deployment/data requirements in the pull request so they can be reviewed separately.

## License

Contributions to the source project follow its [MIT License](LICENSE). Include provenance for new data or media; external assets can have different rights from the code.
