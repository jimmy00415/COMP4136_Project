# Hong Kong Movie RAG Data Foundation Design

**Date:** 2026-07-27

**Status:** Approved by user on 2026-07-27; implementation planning authorized

## 1. Objective

Turn `D:\VS_PROJECT\HK_Movie_KnowledgeGraph\RAG` into a governed, reproducible data foundation for a Google Cloud hosted RAG system while preserving exactly 4,658 formal Release movies as the only production movie population.

The design must:

- prevent Quarantine, Excluded, issue-ledger, macOS sidecar, placeholder, and conflicted poster data from entering production;
- support future poster additions without changing movie identity;
- support multiple versioned deep-analysis documents for each movie;
- retain source, rights, content hash, schema version, and ingestion-run provenance;
- use fail-closed promotion gates;
- preserve a recoverable GCS source snapshot before any permanent local deletion;
- keep every phase small, verifiable, and reversible.

## 2. Non-goals

This design does not:

- invent person or company entity IDs from unresolved names;
- import the 1,264 Quarantine records into production;
- treat file existence as proof that a poster belongs to a movie;
- make poster availability a prerequisite for loading the 4,658 movie records;
- publish assets whose rights status is unknown;
- create GCP resources before project authentication, billing, IAM, and API preflight pass;
- claim that the current movie-level metadata alone provides a complete semantic RAG experience.

## 3. Verified Current Baseline

### 3.1 Formal movie release

- Formal Release movie rows: 4,658.
- Unique `影片唯一ID` values: 4,658.
- Candidate population: 5,922.
- Quarantine population excluded from production: 1,264.
- Release validation: 22 of 22 checks passed in `validation_report_v1.2.json`.
- Manifest-tracked delivery artifacts: all current sizes and SHA-256 hashes match.

### 3.2 Poster key coverage

The canonical movie key is `影片唯一ID`. Poster filenames use the same key as the filename stem.

Current union:

| State | Movie keys | Meaning |
|---|---:|---|
| `machine_passed` | 4,545 | Decodable, release-key matched, and not in an exact cross-key content collision group |
| `content_conflict` | 73 | Same content hash assigned to different movie keys; manual or stronger identity validation required |
| `placeholder` | 9 | The same 65 x 91 trash-can placeholder image |
| `missing` | 31 | No poster file found |
| **Total** | **4,658** | One explicit poster state for every Release movie |

The 4,627 present-key files are all decodable and all reference a formal Release key. There are no orphan poster keys.

### 3.3 Redundant physical copies

- `1-1500posters/` outer layer contains 1,497 release-key files.
- `1-1500posters/1-1500posters/` contains byte-identical copies of all 1,497 outer files.
- `posters1/` contains 1,500 files that are byte-identical to the same keys already present in `posters/posters/`.
- `posters/__MACOSX/` contains 1,632 macOS metadata files.
- `.DS_Store` files are non-business metadata.

### 3.4 Poster encoding issues

Among the 4,627 present-key candidates, detected image formats are:

- JPEG: 4,571;
- PNG: 14;
- WebP: 42.

Some PNG and WebP content is mislabeled with a `.jpg` extension. The source object must remain immutable in archive storage; a normalized RGB/sRGB derivative must be created for serving and multimodal processing.

## 4. Core Design Decisions

### 4.1 Formal Release is the only movie parent table

Every production movie, media asset, analysis document, and chunk must have a foreign key to one of the 4,658 formal Release movie IDs. The validator must reject unknown, Quarantine, blank, or malformed movie IDs.

### 4.2 Posters are optional, versioned media assets

The database loads all 4,658 movie records even when a poster is absent or unresolved. Poster completeness is represented as governed state, not by substituting placeholder content.

Only `manual_approved` or policy-approved `machine_passed` assets may become primary production posters. `content_conflict`, `placeholder`, `missing`, `rejected`, and `rights_blocked` assets cannot be served as production posters.

### 4.3 Deep analysis never overwrites authoritative metadata

Future deep analysis is stored as a versioned `movie_document` with its own sources, rights status, language, authorship method, content hash, and publication status. It may produce RAG chunks, but it cannot mutate the authoritative 12-field movie record without a separate metadata-release process.

### 4.4 Promotion is manifest-driven and fail-closed

Production ingestion reads only a versioned release manifest. It must never recursively scan the project tree or ingest directly from staging folders.

A failed validation run publishes nothing. Database changes must use a transaction or a new release namespace followed by an atomic active-version switch.

## 5. Target Local Layout

```text
RAG/
├── README.md
├── pyproject.toml
├── .env.example
├── .gitignore
├── config/
│   ├── local.yaml
│   └── gcp.yaml
├── schemas/
│   ├── movie.schema.json
│   ├── media_asset.schema.json
│   ├── movie_document.schema.json
│   └── release_manifest.schema.json
├── data/
│   ├── release/
│   │   └── v1.2/
│   │       ├── movies.csv
│   │       ├── movies.parquet
│   │       ├── movie_tiers.parquet
│   │       ├── enrichment_provenance.parquet
│   │       ├── poster_manifest.parquet
│   │       └── release_manifest.json
│   ├── staging/
│   │   ├── posters/
│   │   └── analyses/
│   └── quarantine/
│       └── poster_conflicts/
├── assets/
│   └── posters/
│       ├── originals/
│       └── derived/
├── src/hk_movie_rag/
│   ├── contracts/
│   ├── validation/
│   ├── ingestion/
│   ├── media/
│   ├── chunking/
│   ├── embedding/
│   └── retrieval/
├── migrations/
├── tests/
├── infra/terraform/
└── docs/
    ├── source-contract/
    └── superpowers/
```

Only `data/release/<version>/release_manifest.json` may authorize production ingestion.

## 6. Cleanup and Migration Policy

### 6.1 Mandatory archive gate

Before permanent deletion:

1. Generate a complete local inventory containing path, byte length, SHA-256, and classification.
2. Upload the unchanged source tree to a restricted GCS archive prefix for release `v1.2`.
3. Read back the GCS object inventory.
4. Require exact object count, byte length, and SHA-256 agreement.
5. Record the successful archive verification in an immutable ingestion-run record.

If GCP authentication or archive verification fails, no permanent deletion is permitted.

### 6.2 Proven safe-delete set after archive verification

- `posters/__MACOSX/`;
- every `.DS_Store` file under the RAG workspace;
- `1-1500posters/1-1500posters/`, because all 1,497 entries are byte-identical to the outer copy;
- `posters1/`, because all 1,500 entries are byte-identical to files already in `posters/posters/`.

The executor must re-check resolved absolute paths immediately before deletion and must not use a broad workspace-root recursive delete.

### 6.3 Archive or transform, but do not classify as junk

- The issue-ledger workbook is excluded from production but retained in the source archive.
- The main workbook's enrichment audit is transformed into release-only provenance before the workbook leaves the active data path.
- The tier workbook is transformed into canonical tier and pilot columns or tables.
- Validation JSON, manifest JSON, and source-contract PDFs remain governance evidence.
- Conflicted poster candidates are moved to a non-production quarantine location until reviewed; they are not deleted by automated cleanup.

## 7. Poster Asset Contract

Each media asset record contains:

- `asset_id`;
- `movie_id`;
- `asset_type`;
- `source_object_uri`;
- `derived_object_uri`;
- `content_sha256`;
- `detected_mime_type`;
- `width`, `height`, and `color_mode`;
- `quality_status`;
- `identity_confidence`;
- `source_url`;
- `rights_status`;
- `version`;
- `is_primary`;
- `created_at` and `reviewed_at`.

Allowed quality states are:

- `machine_passed`;
- `content_conflict`;
- `placeholder`;
- `missing`;
- `manual_approved`;
- `rejected`;
- `rights_blocked`.

At most one asset per movie may be the active primary poster. The database enforces this with a partial unique index.

## 8. Future Incremental Inputs

### 8.1 Poster submission

```text
data/staging/posters/<movie_id>/<submission_id>/poster.<detected-extension>
data/staging/posters/<movie_id>/<submission_id>/metadata.json
```

The sidecar must include movie ID, source URL, retrieval timestamp, rights status, and expected SHA-256. Promotion requires release-key membership, successful decode, detected MIME agreement, content-hash checks, and identity validation.

### 8.2 Deep-analysis submission

```text
data/staging/analyses/<movie_id>/<document_id>.md
```

The document front matter must include schema version, movie ID, document type, language, title, source URLs, rights status, authorship method, content version, and content SHA-256.

Each changed document produces a new version. Idempotent re-ingestion of unchanged content must produce no new document or chunk rows.

## 9. GCP Architecture

### 9.1 Object storage

Use three isolated Cloud Storage buckets or equivalent IAM-isolated managed folders:

- source archive: immutable source snapshots and original uploads;
- staging: untrusted incoming posters and analysis documents;
- release: validated canonical files and approved derived media.

Use soft delete or object versioning with lifecycle rules for recoverability. Do not enable an irreversible retention lock during the pilot.

### 9.2 Database and retrieval

Use Cloud SQL for PostgreSQL with `pgvector` for the pilot and initial product:

- `movies` stores formal Release metadata;
- `media_assets` stores poster metadata and GCS URIs;
- `movie_documents` stores versioned source documents;
- `document_chunks` stores text, chunk metadata, model identity, and vectors;
- `ingestion_runs` stores manifests, counts, checks, and state transitions.

Use Vertex AI embeddings. Start evaluation at 768 dimensions and compare against 1,536 dimensions on the 24-movie pilot. Adopt 768 unless Recall@5 falls by more than two percentage points against 1,536 on the same adjudicated query set.

Use Cloud Run Jobs for validation, transformation, backfill, embedding, and promotion. Use a separate Cloud Run service for the query API.

### 9.3 IAM

Use separate service identities:

- archive writer: write and verify source snapshots;
- ingestion service: read staging, write release, call Vertex AI, and connect to Cloud SQL;
- query service: read approved release objects and query Cloud SQL;
- infrastructure deployer: manage Terraform-controlled resources.

Do not deploy service-account JSON keys with the application. Use local user ADC for development and Cloud Run service identity in GCP. Store secrets in Secret Manager.

## 10. Current GCP Preflight State

- Google Cloud SDK is installed.
- Configured project is `motionexpaiweb`.
- Configured region is `us-central1`.
- The configured `admin@motionexp.com` OAuth session cannot refresh non-interactively.
- The secondary local account does not have access to the project.
- The backend `.env` references a Google credential file that is currently absent.

The first cloud execution step is an interactive reauthentication of the company account, followed by read-only checks for project lifecycle, billing, IAM, required APIs, buckets, Cloud SQL instances, and Vertex AI access. Resource creation is prohibited until this preflight passes.

## 11. Failure Handling

- Unknown movie key: reject the asset or document into staging error output.
- Duplicate content hash across movie keys: mark every involved asset `content_conflict`; publish none automatically.
- Placeholder signature or undersized image: mark `placeholder` and do not create a production derivative.
- MIME/extension mismatch: preserve the original, record detected MIME, and create a correctly encoded derivative.
- Rights status unknown: mark `rights_blocked`; storage in the restricted archive is allowed, public serving is not.
- Hash or row-count mismatch: abort the run before database or release-bucket publication.
- Partial database failure: roll back the transaction or retain the previous active release version.
- Embedding failure: retain the prior chunk/vector version and mark the run failed.
- GCP authentication failure: stop before upload, deletion, or resource mutation.

## 12. Verification and Acceptance Criteria

### 12.1 Local canonical release

- exactly 4,658 movie rows and 4,658 unique movie IDs;
- zero Quarantine or Excluded movie rows;
- exact field agreement with the current formal Release CSV;
- exactly one poster-state record for each movie;
- initial poster-state reconciliation equals 4,545 machine-passed, 73 content-conflict, 9 placeholder, and 31 missing;
- zero orphan poster keys;
- all active files covered by the release manifest and SHA-256 inventory;
- repeat execution produces byte-identical canonical outputs.

### 12.2 Cleanup

- GCS archive read-back verification passes before deletion;
- no `__MACOSX`, `.DS_Store`, nested 1,497-file copy, or redundant `posters1` copy remains in the active tree;
- no conflicted or placeholder asset is silently promoted;
- deleted targets are exactly the reviewed absolute paths.

### 12.3 GCP load

- row counts reconcile between canonical files and Cloud SQL;
- all foreign keys pass;
- a rerun creates no duplicate movie, asset, document, or chunk rows;
- query service cannot read staging or source-archive content;
- ingestion service cannot make a conflicted asset primary;
- backup and restore are tested with a non-production recovery target.

### 12.4 Initial RAG evaluation

- exact-title and exact-ID queries retrieve the correct movie in top 5 for 100% of the adjudicated pilot queries;
- semantic queries report Recall@5 for both 768- and 1,536-dimensional embeddings;
- every answer-level factual claim is traceable to retrieved source content;
- no production-readiness claim is made before the pilot evaluation passes.

## 13. Delivery Sequence

1. Authenticate and run read-only GCP preflight.
2. Create and verify the source archive snapshot.
3. Build local schemas, validators, manifests, and deterministic canonical transforms.
4. Produce the release-only movie, tier, provenance, and poster-state datasets.
5. Execute only the proven safe-delete set.
6. Create Terraform-controlled GCP storage, Cloud SQL, identities, and jobs.
7. Load formal Release data and approved media assets.
8. Build the 24-movie retrieval evaluation and freeze embedding configuration.
9. Enable incremental poster and deep-analysis promotion.

Each step must end with tests and a reviewable commit. A failed step leaves the prior verified state intact.
