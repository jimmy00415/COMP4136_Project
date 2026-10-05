# Course frontend

[Open the course chatbot](https://hk-movie-course-frontend-4l6lw3rnaa-uc.a.run.app).

A separate, unbranded browser entry point for HK Movie RAG. The page preserves the original layout, colors, examples, citations, movie cards, poster handling, and conversation behavior. Only the HTML title, heading label, beta label, and ME logo differ. `public/static/app.js` and `styles.css` are byte-identical to the UI shipped by the existing backend.

This container serves static files and forwards approved requests to **https://hk-movie-rag-demo-4l6lw3rnaa-uc.a.run.app**. It contains no retrieval engine, database client, catalog, model configuration, Google credentials, or release data. The original backend and its branded entry point remain available.

## Request flow

```text
Browser → course frontend (NGINX) → existing HK Movie RAG backend
                                  → existing PostgreSQL / Vertex AI
```

The browser uses relative URLs, so chat requests and poster images remain on its own origin. NGINX forwards only `GET /health`, `GET /api/config`, `POST /api/chat`, and `GET /api/posters/{movie_id}` to the fixed HTTPS backend. It verifies the upstream certificate, forwards the body unchanged, preserves API status codes and responses, drops client authentication/cookie headers, and does not retry or cache chat requests. Other API paths are rejected. This avoids requiring a backend CORS change.

## Run locally

With Docker and Python 3.13 installed:

```bash
docker build -t hk-movie-course-ui frontend/course
docker run --rm -p 8080:8080 hk-movie-course-ui
```

Open `http://localhost:8080`. In another terminal:

```bash
python frontend/course/smoke.py http://localhost:8080
```

This needs network access to the existing public backend. Smoke checks verify unbranded HTML, exact deployed CSS/JS parity, identical release configuration, validation-error pass-through, large request buffering, route restrictions, and poster byte/header parity. They intentionally make no model-generation request. Browser chat checks are additional operational verification, separate from the paired evaluation.

## Deploy to the existing project

Use the existing authenticated Google Cloud CLI. Build from this directory only, with a unique image tag:

```bash
gcloud builds submit frontend/course \
  --config=frontend/course/cloudbuild.yaml \
  --project=motionexpaiweb \
  --substitutions=_IMAGE=us-central1-docker.pkg.dev/motionexpaiweb/hk-movie-rag/course-frontend:YOUR_UNIQUE_TAG
```

The build validates NGINX configuration, starts the container as the nginx UID, and runs the smoke script against that container before publishing its image. Read the successful build's `results.images[0].digest`, then deploy that immutable digest:

```bash
gcloud run deploy hk-movie-course-frontend \
  --project=motionexpaiweb --region=us-central1 \
  --image=us-central1-docker.pkg.dev/motionexpaiweb/hk-movie-rag/course-frontend@sha256:YOUR_VERIFIED_DIGEST \
  --service-account=hk-movie-course-frontend@motionexpaiweb.iam.gserviceaccount.com \
  --port=8080 --cpu=1 --memory=256Mi --concurrency=20 \
  --min-instances=0 --max-instances=1 --timeout=360 \
  --ingress=all --no-invoker-iam-check
```

The dedicated runtime service account has no project IAM roles. Public backend access requires no Google credential. The frontend scales to zero and shares the existing backend's capacity; it does not add inference replicas. No Cloud SQL connection, secret binding, bucket, or new GCP project is required. The immutable upstream URL is configured in `backend-proxy.conf`.

## Scope of evidence

The paired study evaluates the existing backend revision. Introducing this browser entry point does not rerun that study or alter its scores. The evaluation protocol, frozen responses, results, and report remain unchanged. Operational smoke checks show that the new entry point connects to the same release and preserves browser assets and API behavior.
