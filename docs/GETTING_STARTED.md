# Getting started

[Project overview](../README.md) · [Evaluation](EVALUATION.md) · [Engineering guide](ENGINEERING_GUIDE.md)

## Choose a starting point

| Goal | Requirements |
|---|---|
| Try the [hosted chatbot](https://hk-movie-course-frontend-4l6lw3rnaa-uc.a.run.app) | A browser |
| Install source and run controlled tests | Git, uv, Python 3.13; Node.js for browser-script tests |
| Serve a local backend | Matching release data, PostgreSQL/pgvector, Google Cloud credentials, and configured process environment |
| Repeat live evaluation | Exact external catalog, serving revision/image pins, usable Google credentials, and a fresh output directory |

## Install

Install [uv](https://docs.astral.sh/uv/getting-started/installation/) and Git, then:

```bash
git clone https://github.com/jimmy00415/COMP4136_Project.git
cd COMP4136_Project
uv sync --frozen --python 3.13
```

Use Python **3.13**, matching the pinned Docker runtime. Package metadata still declares Python 3.11+, but the complete archive module imports `collections.abc.Buffer`, which prevents full application loading on Python 3.11. The application dependency lock is `uv.lock`.

## Run offline checks

Evaluation-tool tests use controlled fixtures and do not require the external catalog or cloud credentials:

```bash
uv run --frozen pytest -q course/test_simple_eval.py course/test_challenge_eval.py course/test_review_challenge.py course/test_query_fix_eval.py course/test_final_paired_eval.py
```

Relevant application and UI regressions:

```bash
uv run --frozen pytest -q tests/test_retrieval.py tests/test_rag_query.py -k "not every_bounded_release_credit"
node --test tests/static_app_ui_contract.mjs
```

Recorded verification for the evaluated code: **45 evaluation-tool tests**, **1,464 relevant application tests** with one external-CSV test deselected, and **seven browser-script tests** passing. These are recorded runs, not a CI status or certification of every repository test.

`tests/integration/` requires external release inputs. One release-credit test outside that directory also needs a release CSV, so `pytest --ignore=tests/integration` alone is not an offline-only suite. Run full integration after preparing the declared external data.

## Configure a backend

A source checkout does not include the populated database or full external evidence collection. Prepare those inputs using the [engineering guide](ENGINEERING_GUIDE.md), whose historical release examples must be reconciled with the release you intend to serve.

| Configuration | Purpose |
|---|---|
| `DB_NAME`, `DB_USER`, `DB_PASSWORD` | Database identity and credentials |
| `DB_HOST`, `DB_PORT` or `INSTANCE_CONNECTION_NAME` | Local PostgreSQL or Cloud SQL connection |
| Google Cloud project/location and credentials | Authorized Vertex AI and storage access |
| `RAG_RELEASE_ID`, manifest and policy hashes | A matching verified evidence release |
| Embedding/generation model and vector dimension | Compatibility with stored vectors and serving models |
| Serving revision and image identity | Deployment/source traceability |

See [`.env.example`](../.env.example) for configuration names. It contains **historical release values**, not the final evaluated R3 configuration. Use the [freeze](../course/final_paired_freeze.json) and [deployment summary](../course/query_fix_summary.json) to inspect current identities. Supply credentials through your environment or approved secret mechanism; keep them out of Git and issue reports.

The API reads process environment variables. It does **not** automatically load `.env`. A valid configuration must point to an ingested and compatible release before starting:

```bash
uv run --frozen uvicorn hk_movie_rag.demo_api:app --host 127.0.0.1 --port 8080
```

Open `http://127.0.0.1:8080`; inspect `/health` and `/api/config`. Successful source installation alone does not prove backend readiness.

The [Dockerfile](../Dockerfile) provides a pinned Python 3.13 image and nonroot runtime. Deployment scripts are in [`scripts/gcp/`](../scripts/gcp/). They operate on real infrastructure and are separate from offline tests.

## API and history

The hosted demo exposes the same `/api/chat` contract. This example submits a factual question and then asks about the actually selected film:

```python
import json
from urllib.request import Request, urlopen

BASE_URL = "https://hk-movie-course-frontend-4l6lw3rnaa-uc.a.run.app"

def ask(question, history):
    payload = {"question": question, "history": history}
    request = Request(
        BASE_URL + "/api/chat",
        data=json.dumps(payload, ensure_ascii=False).encode("utf-8"),
        headers={"Content-Type": "application/json"},
    )
    with urlopen(request, timeout=90) as response:
        return json.load(response)

question = "《少林足球》的電影類型有哪些？"
first = ask(question, [])
history = [{
    "question": question,
    "answer": first["answer_markdown"],
    "movie_ids": [movie["movie_id"] for movie in first["movies"]],
}]
second = ask("這部電影是哪一年上映的？", history)
print(second["answer_markdown"])
```

These are usage examples, not additional benchmark observations. Actual model-backed calls may incur provider costs.

Responses contain:

| Field | Meaning |
|---|---|
| `answer_markdown` | Answer, clarification, or refusal text |
| `citations` | Selected source identities and metadata/document references |
| `movies` | Canonical structured cards for returned films |

History is client-owned, with at most **four exchanges** and **12,000 total characters**. Send actual prior answers and IDs; do not insert reference answers. Malformed input returns validation errors; unavailable backend requests may return HTTP 503. Citation identity is an inspectable evidence link, not proof that every generated sentence is supported.

## Troubleshooting

| Symptom | Check |
|---|---|
| `Buffer` import error | Use Python 3.13 and recreate/sync the environment for that interpreter. |
| Missing configuration at startup | Export the required process variables; copying `.env` does not load it. |
| Database or release validation failure | Check connectivity, ingestion, active release, manifest/policy hashes, and vector dimension. |
| Vertex authentication error | Restore authorization for the intended Google account/project; do not paste tokens or passwords into issue reports. |
| Full pytest fails on missing files | Prepare external release inputs, or run the controlled subsets above. |

The hosted service is an externally operated technical demo. Published answers, reviews, source, and the report remain inspectable even when the endpoint is unavailable.
