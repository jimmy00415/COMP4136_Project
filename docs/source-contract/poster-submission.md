# Poster submission contract

Poster staging is untrusted and read-only. Submit exactly two direct regular files:

```text
data/staging/posters/<movie_id>/<submission_id>/poster.<jpg|png|webp>
data/staging/posters/<movie_id>/<submission_id>/metadata.json
```

`metadata.json` uses schema version `1.0` and requires `schema_version`, `movie_id`,
`submission_id`, `source_url`, `retrieved_at`, `rights_status`, and
`expected_sha256`. Optional governed fields are `expected_mime_type` and
`quality_status`. No other properties are accepted. The complete JSON Schema is the
`posterSubmission` definition in `schemas/media_asset.schema.json`.

The two directory names are part of the contract: `<movie_id>` must equal the
sidecar `movie_id`, and `<submission_id>` must equal sidecar `submission_id`.
Validation rejects either mismatch before reading or decoding poster bytes.

The movie ID must belong to canonical
`data/release/<release_version>/movies.parquet`; source, Quarantine, and issue-ledger
identifiers are not accepted. The validator recomputes SHA-256 from the poster bytes,
decodes the image, and checks the detected MIME type and canonical extension. It does
not copy, rewrite, rename, or otherwise mutate the submission.

Run:

```powershell
uv run hk-movie-rag validate-staging --kind poster data/staging/posters/<movie_id>/<submission_id>
```

Publication requires `quality_status` equal to `machine_passed` or
`manual_approved` **and** `rights_status` equal to `cleared`. A missing or unknown
rights determination is therefore not publishable. Promotion into a Release remains
a separate manifest-authorized operation.
