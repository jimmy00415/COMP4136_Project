# Release v1.2 production-authority contract

`data/release/v1.2/release_manifest.json` is the only local file that may
authorize production ingestion for Release v1.2. A canonical artifact, generated
asset, staging item, source workbook, or Quarantine record is not production input
unless this manifest names and successfully verifies it.

## Authorized artifact set

The manifest must contain exactly these six repository-relative artifacts, sorted
by `relative_path`:

| Artifact | Governing entity schema | Required reconciliation |
|---|---|---|
| `movies.csv` | `schemas/movie.schema.json` | 4,658 rows and unique movie IDs |
| `movies.parquet` | `schemas/movie.schema.json` | exact row agreement with `movies.csv` |
| `movie_tiers.parquet` | `schemas/movie.schema.json` | 4,658 unique Release foreign keys; S=50, A=313, B=4,295 |
| `pilot_movies.parquet` | `schemas/movie.schema.json` | 24 unique Release foreign keys |
| `enrichment_provenance.parquet` | `schemas/movie.schema.json` | 1,008 unique provenance keys with Release foreign keys |
| `poster_manifest.parquet` | `schemas/media_asset.schema.json` | one unique state row for every Release movie |

For the three movie-child datasets, `schema_path` identifies the governing movie
identity contract. Their exact storage columns are fixed by the canonical producer
and checked by the verifier before business reconciliation. The poster artifact is
also validated row-by-row against the complete media-asset JSON Schema.

Each artifact declaration contains only `relative_path`, `schema_path`,
`size_bytes`, `sha256`, and `row_count`. Absolute paths, backslashes, traversal,
Windows device aliases, credentials, and access tokens are rejected by the manifest
schema and the verifier's exact-path mapping.

## Root evidence and counts

The root records:

- `release_version=v1.2`;
- the SHA-256 of `FinalDelivery_2026-07-16/manifest_v1.2.json`;
- `source_generated_at` copied byte-for-value from that source manifest, never from
  the current clock;
- source-bound expected counts: 4,658 Release movies, 1,264 Quarantine movies,
  S/A/B counts 50/313/4,295, 24 pilot movies, and 1,008 audit rows;
- poster states: 4,545 `machine_passed`, 73 `content_conflict`, 9 `placeholder`,
  and 31 `missing`.

The source identity gate rebinds every artifact declared by the source manifest to
its declared size and SHA-256, requires the source manifest's declared artifact
count, and parses the bound validation report to require `ok=true` and all
22 checks passing. The verifier deterministically derives the expected movie, tier,
pilot, and enrichment rows from those bound CSV/workbook bytes and requires exact
agreement with the five canonical artifacts. The Quarantine workbook is read only
from those already-bound bytes during the final disjointness check.

Release v1.2 is a frozen snapshot, not a moving agreement between configuration and
source files. Its source-manifest SHA-256, source timestamp, counts, exact ordered
22-check validation identities, and semantic digests for every canonical dataset are
pinned in the verifier. Coordinated edits to configuration, source evidence,
canonical artifacts, and a regenerated manifest cannot redefine the `v1.2` name.

All 4,658 poster rows in this snapshot have `rights_status=unknown`,
`publishable=false`, and `is_primary=false`. Machine quality does not imply cleared
rights: even the 4,545 `machine_passed` rows remain blocked from publication until a
future, separately governed rights review produces a new release contract.

## Verification order

Verification stops at the first blocking failure in this order:

1. manifest JSON Schema validity, canonical location, exact sorted artifact set,
   schema paths, and valid referenced schemas;
2. source-manifest SHA-256, version, `generated_at`, delivery counts, and configured
   source-artifact identity;
3. existence of the complete authoritative Release namespace;
4. current artifact SHA-256 and byte length;
5. declared and governed row counts plus tier counts;
6. unique movie, child, asset, and provenance identifiers, including exact movie
   CSV/Parquet agreement;
7. child-to-Release foreign-key membership and required full coverage;
8. one poster row per movie, the immutable v1.2 poster counts and complete semantic
   digest, exact poster-state reconciliation, and cross-field publication policy
   (approved quality plus cleared rights, primary only when publishable, and
   state-appropriate active/Quarantine paths);
9. formal Release and Quarantine ID disjointness.

All reads require physical repository containment and reject symbolic links,
reparse points, non-regular files, and identity changes during the read. Verification
uses the hash-bound bytes retained in memory for later checks rather than reopening
artifact paths. The six artifact names, their physical identities, sizes, and hashes
are rebound after semantic checks and at the final `valid=true`/publication boundary;
the manifest identity is likewise rebound by the verifier. Manifest publication
occurs only after every gate passes and uses a same-directory, atomic, no-follow
replacement. The exact namespace is rechecked after installation, and a failed
post-install content, namespace, or rebind gate removes the new manifest or restores
the exact previous manifest bytes; a failed build does not publish a partial
authority.

Unexpected or stale files in `data/release/v1.2/` fail closed. Generated Release
payloads remain ignored by Git; only `release_manifest.json` is intentionally
tracked.

## Operator workflow

Build the authority only after the canonical movie and poster builders have
completed:

```powershell
uv run hk-movie-rag build-manifest
uv run hk-movie-rag verify-release data/release/v1.2/release_manifest.json
```

A successful verification emits compact JSON with `"valid":true`, release version,
artifact count, and movie count. A blocking build or verification failure is emitted
by the CLI as an argument-parser error and exits with status 2. Downstream ingestion
must require a successful verification result; presence of the JSON file alone is
not authorization.

Rebuilding without changing any source or canonical artifact must produce the same
manifest bytes and SHA-256. This contract does not authorize GCP upload, database
load, cleanup, public poster serving, or promotion of Quarantine, placeholder,
conflicted, or rights-unknown media.
