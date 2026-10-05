# Movie analysis submission contract

Store each untrusted analysis as:

```text
data/staging/analyses/<movie_id>/<document_id>.md
```

The UTF-8 Markdown file begins with YAML front matter. Schema version `1.0` requires
`schema_version`, `movie_id`, `document_id`, `document_type`, `language`, `title`,
`source_urls`, `rights_status`, `authorship_method`, `content_version`, and
`content_sha256`. Optional `quality_status` defaults to `machine_passed`. Additional
properties and duplicate YAML keys are rejected. See
`schemas/movie_document.schema.json` for the exact enums and shapes.

The parent directory must equal front-matter `movie_id`, and the filename must be
exactly `<document_id>.md`. Either mismatch is rejected before body hashing.

`content_sha256` is the lowercase SHA-256 of every byte after the closing front-matter
delimiter, including its line endings. It is never trusted: validation recomputes and
compares it. The deterministic document identity is SHA-256 over UTF-8 `movie_id`, a
NUL separator, UTF-8 `document_id`, a NUL separator, and the exact Markdown body
bytes. Retrying unchanged bytes is idempotent; changed bytes have a different identity
and must use a new `content_version`. Reusing one movie/document/version key for
different bytes is rejected by
`validate_analysis_collection(paths, release_ids)`. That future-promotion registry
returns one entry for repeated, identical submissions and raises `StagingError` on
any content or governed-metadata conflict for the same
`(movie_id, document_id, content_version)` key.

The movie ID must exist in canonical
`data/release/<release_version>/movies.parquet`. Analysis cannot update authoritative
movie metadata. Publication requires `quality_status` equal to `machine_passed` or
`manual_approved` and `rights_status` equal to `cleared`.

Run:

```powershell
uv run hk-movie-rag validate-staging --kind analysis data/staging/analyses/<movie_id>/<document_id>.md
```

Validation reads only and emits a deterministic JSON summary. Release promotion and
chunk generation are separate, future manifest-authorized steps.
