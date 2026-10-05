# Deployed query repair: actual 60-turn development regression

On 5 October 2026 the repaired HK Movie service completed **60/60** known
challenge turns: 60 actual responses, 60 mechanical passes and 60 accepted
Root AI reviews. No question-level retries or omitted failures were used.

| Family | Repaired system, AI-reviewed |
|---|---:|
| Ambiguity / explicit ID | 10/10 |
| Traditional / Simplified variants | 10/10 |
| Compound recommendations | 10/10 |
| Evidence / domain boundary | 10/10 |
| Dialogue | 20/20 |
| Total | **60/60** |

This is **development regression on previously observed questions**, not an
unseen holdout. The baseline was not rerun. These before/after observations
cannot establish population accuracy, causal module effects, or a new paired
comparison with BM25. They do not guarantee a course grade or arbitrary future
queries. This page retains the earlier system-only regression. The final report now uses a separate [fresh paired comparison](FINAL_COMPARISON_RESULTS.md); observations are not pooled.

## What changed

Application source commit: `0a81de7447b1ddbfaf7f7786bb97d52815469939`.

1. Recognize the `ID` and `請查詢` wrappers for already identified movies and
   present canonical fields, retaining refusal for absent budgets and analysis.
2. Parse inclusive Chinese `1980至1999` / `1980年到1999年` ranges before a
   single-year fallback, with boundaries protecting movie IDs.
3. Apply explicit genre conjunctions/intersections without a person filter.
   Genre OR/AND connectors are scoped to adjacent genre names, preserving
   alternatives with movie suffixes and preventing unrelated formatting OR
   or a separate comparison sentence from weakening the intersection.
4. Keep an analysis verb from becoming a tentative person-catalog name.
5. Strip the bounded `現在換成` shell before director identity resolution.
6. Interpret `不要與剛才重複` as history deduplication, not a person exclusion.

The fixes contain no answers or challenge-specific movie IDs. Local regressions
cover alternate spellings, refusal controls, AND/OR scope, invalid evidence,
director switching and deduplication. The existing evidence validators remain.
No model weight training, dataset changes, ingestion or re-embedding occurred.

## Actual cloud and evidence identities

- Service: [HK Movie live demo](https://hk-movie-rag-demo-4l6lw3rnaa-uc.a.run.app)
- Project/region: `motionexpaiweb` / `us-central1`.
- Serving revision: `hk-movie-rag-demo-00001-qfix-0a81de7`.
- Image: `sha256:6c031ad921888783e1167dca78e0138cf45ff4c34b0a17f58a1c9847ada92cbd`.
- Build: `b0120b3e-c465-4297-a307-6b2b7b6f87c4`, actual status `SUCCESS`.
- Release, catalog, models, policy identities, database, service account,
  secrets, CPU/memory and scaling settings match the previous configuration.
- The candidate was tested at zero traffic; etag-guarded traffic promotion
  and production config, health and explicit-ID answer readbacks succeeded.
- Actual candidate calls: **2026-10-05 20:38:26–20:39:41 HKT**;
  exact timestamps are in the raw records/freeze.
- Responses SHA-256:
  `7fb4912cebfe21aa9fdf5c10dae8687e64200773a69a2cea7dfca47f3440f4f6`.
- Cases SHA-256:
  `2ab256dc99112b6864f9c41f51f77d216ebff8aeccfa6458c30107f54787e7f5`.
- Catalog SHA-256:
  `1dbea26150a816ae4fc4d189a2a08e39628cbee997f3966b0dd5cd16843e907b`.

Root AI read all 60 complete answer texts and checked facts against catalog
gold. All **784 returned catalog card-field checks** matched: ID, Chinese/
English title, date, director, cast, genre and tier. This checks dataset
agreement, not independent truth of every real-world film fact. Auxiliary
pilot/poster fields are not independently verified by the catalog audit.
Human audits remain pending; `submission_ready` remains false.

## Inspect and reproduce

- [Actual complete API answers and histories](query_fix_answers.jsonl)
- [Per-case Root AI review](query_fix_review.json)
- [Freeze, summary, build and promotion evidence](query_fix_summary.json)
- [Exact measured runner snapshot](query_fix_eval_run1.py)
- [Current runner](query_fix_eval.py) and [runner tests](test_query_fix_eval.py)

The measured runner snapshot SHA is
`563d787d79a8a97a4aff68436478ef1d084f4be7888c341b720273d5b29b07e9`.
After this run completed, review found that a *local classifier exception*
would have discarded a successful response in that runner. There were **no
such exceptions in this run**, and all 60 responses are retained. The current
runner separately records classifier failures while retaining actual responses
and history; a failing-then-passing regression verifies the correction. Its
source is a subsequent version; the recorded run is bound to the exact snapshot,
not falsely attributed to this later runner.

```shell
# GET-only validation against the repaired production revision.
uv run python course/query_fix_eval.py --service https://hk-movie-rag-demo-4l6lw3rnaa-uc.a.run.app --revision hk-movie-rag-demo-00001-qfix-0a81de7 --image-digest sha256:6c031ad921888783e1167dca78e0138cf45ff4c34b0a17f58a1c9847ada92cbd --catalog /path/to/catalog.jsonl --output /path/to/new-run
```

Adding `--execute` creates a **new**, billable 60-call development run in a
fresh directory. It never overwrites this observation or retries a case.
The historical paired evaluator deliberately pins the older revision and
will reject the current service as drift; use the separate current runner.

Final relevant checks: **1,464 application tests passed**, one external-catalog
test explicitly deselected; **40 course tests passed**; **7 browser-script
contract tests passed**. Full repository integration certification requires
external release inputs and is not claimed.
