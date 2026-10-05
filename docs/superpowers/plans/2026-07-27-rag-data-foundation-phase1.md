# RAG Data Foundation Phase 1 Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** Build and verify a deterministic local Release v1.2 data foundation, archive the untouched source inputs to GCS, and remove only the proven redundant or non-business files after the archive gate passes.

**Architecture:** A Python package turns explicit source paths into canonical movie, tier, provenance, and poster manifests; production inputs are selected only by a hash-bound release manifest, never by recursive discovery. A separate fail-closed GCP preflight and GCS archive verifier must succeed before a narrow, recoverable cleanup command can act on reviewed absolute paths.

**Tech Stack:** Python 3.11-3.12, uv, Pydantic, JSON Schema, PyArrow, Pillow, openpyxl read-only parsing, Typer, pytest, Ruff, mypy, Google Cloud Storage client, Terraform, GCS, PowerShell.

## Global Constraints

- Production population is exactly 4,658 formal Release movies with exactly 4,658 unique `影片唯一ID` values.
- The 1,264 Quarantine records, 769 excluded issue rows, and the complete issue ledger must never enter production datasets.
- Initial poster reconciliation must be exactly 4,545 `machine_passed`, 73 `content_conflict`, 9 `placeholder`, and 31 `missing`, totaling 4,658.
- Poster filename stem is joined only to the exact formal Release `影片唯一ID`; fuzzy title matching is prohibited.
- Original source bytes remain immutable in the GCS archive; normalized media is always a derivative with its own hash.
- Unknown poster rights block publication even when image quality passes.
- Production ingestion is authorized only by `data/release/v1.2/release_manifest.json`.
- No permanent or recoverable cleanup action is allowed until GCS object count, byte length, and streamed SHA-256 read-back all match the local inventory.
- Cleanup targets are limited to `posters/__MACOSX`, `.DS_Store` files, `1-1500posters/1-1500posters`, and `posters1`; unique source files and `FinalDelivery_2026-07-16` are never quarantine targets.
- GCP project is `motionexpaiweb`, region is `us-central1`, and interactive authentication must use `admin@motionexp.com`; no service-account JSON keys may be created or stored.
- GCP resource creation is prohibited until lifecycle, billing, IAM visibility, and required-API preflight checks pass.
- Every destructive path must resolve below `D:\VS_PROJECT\HK_Movie_KnowledgeGraph\RAG` and must exactly match the generated cleanup plan.
- Generated timestamps may appear in run reports, but deterministic Release artifacts must derive their version/time from the v1.2 source manifest so repeated builds are byte-identical.
- This plan stops after the data foundation and archive-gated cleanup. Cloud SQL, pgvector, embeddings, retrieval evaluation, and the query service require a separate implementation plan.

---

## File Structure

The phase creates or modifies these focused units:

```text
RAG/
├── .env.example                         # non-secret local configuration names
├── .gitignore                           # excludes raw/generated data, credentials, state, caches
├── README.md                            # operator workflow and safety boundaries
├── pyproject.toml                       # package, CLI, lint, type, and test configuration
├── uv.lock                              # exact reproducible Python dependency resolution
├── config/
│   ├── local.yaml                       # exact v1.2 source paths and expected counts
│   └── gcp.yaml                         # project, region, archive bucket naming policy
├── schemas/
│   ├── movie.schema.json                # canonical 12-field movie contract
│   ├── media_asset.schema.json          # one poster-state row per Release movie
│   ├── movie_document.schema.json       # future versioned analysis submission contract
│   └── release_manifest.schema.json     # only production-ingestion authority
├── src/hk_movie_rag/
│   ├── __init__.py
│   ├── cli.py                           # command routing; no hidden recursive ingestion
│   ├── config.py                        # typed config and contained path resolution
│   ├── hashing.py                       # streaming SHA-256 helpers
│   ├── inventory.py                     # immutable source inventory and classifications
│   ├── release_data.py                  # movie/tier/provenance canonicalization
│   ├── posters.py                       # key join, image detection, collision states, derivatives
│   ├── staging.py                       # future poster and analysis submission validation
│   ├── release_manifest.py              # deterministic manifest build and verification
│   ├── gcp_preflight.py                 # read-only gcloud checks
│   ├── gcs_archive.py                   # idempotent upload and streamed remote hash verification
│   └── cleanup.py                       # exact allowlist plan and recoverable execution
├── tests/
│   ├── fixtures/                        # small non-workbook JSON/image/text fixtures
│   ├── conftest.py                      # shared repository and fixture builders
│   ├── test_config.py
│   ├── test_inventory.py
│   ├── test_release_data.py
│   ├── test_posters.py
│   ├── test_staging.py
│   ├── test_release_manifest.py
│   ├── test_gcp_preflight.py
│   ├── test_gcs_archive.py
│   ├── test_cleanup.py
│   └── integration/test_v12_source.py    # exact live-source reconciliation gates
├── infra/terraform/archive/
│   ├── versions.tf
│   ├── variables.tf
│   ├── main.tf
│   └── outputs.tf
└── docs/source-contract/
    ├── release-v1.2.md
    ├── poster-submission.md
    └── movie-analysis-submission.md
```

Generated `artifacts/`, `data/release/v1.2/*.csv`, `data/release/v1.2/*.parquet`, `assets/posters/`, and `data/quarantine/poster_conflicts/` are local/cloud load artifacts, not Git payloads. Schemas, release manifest, code, tests, configuration, and documentation are committed.

### Task 1: Bootstrap the governed project and contained configuration

**Files:**
- Create: `.gitignore`
- Create: `.env.example`
- Create: `pyproject.toml`
- Create: `uv.lock`
- Create: `config/local.yaml`
- Create: `config/gcp.yaml`
- Create: `src/hk_movie_rag/__init__.py`
- Create: `src/hk_movie_rag/config.py`
- Create: `src/hk_movie_rag/cli.py`
- Create: `tests/conftest.py`
- Create: `tests/test_config.py`

**Interfaces:**
- Produces: `Settings.load(repo_root: Path) -> Settings`
- Produces: `Settings.resolve_input(relative_path: str) -> Path`, which rejects absolute paths, traversal, symlinks escaping the workspace, and missing configured inputs.
- Produces: CLI entry point `hk-movie-rag` with commands added by later tasks.

- [ ] **Step 1: Write the failing containment and configuration tests**

```python
def test_settings_loads_fixed_release_contract(repo_root: Path) -> None:
    settings = Settings.load(repo_root)
    assert settings.release_version == "v1.2"
    assert settings.expected.release_movies == 4658
    assert settings.gcp.project_id == "motionexpaiweb"
    assert settings.gcp.region == "us-central1"

def test_resolve_input_rejects_escape(repo_root: Path) -> None:
    settings = Settings.load(repo_root)
    with pytest.raises(ConfigError, match="outside workspace"):
        settings.resolve_input("../outside.csv")
```

- [ ] **Step 2: Run the tests and confirm the package/configuration do not exist yet**

Run: `uv run pytest tests/test_config.py -v`

Expected: FAIL during import with `ModuleNotFoundError: No module named 'hk_movie_rag'`.

- [ ] **Step 3: Add the package contract and exact local configuration**

`config/local.yaml` must name inputs explicitly:

```yaml
release_version: v1.2
source_manifest: FinalDelivery_2026-07-16/manifest_v1.2.json
source_validation: FinalDelivery_2026-07-16/validation_report_v1.2.json
movies_csv: FinalDelivery_2026-07-16/1970-2026香港電影標準化元數據集_v1.2.csv
movies_xlsx: FinalDelivery_2026-07-16/1970-2026香港電影標準化元數據集_v1.2.xlsx
tiers_xlsx: FinalDelivery_2026-07-16/分級片單_S-A-B_v1.2.xlsx
issue_ledger_xlsx: FinalDelivery_2026-07-16/待補全問題台帳_v1.2.xlsx
inventory_roots:
  - FinalDelivery_2026-07-16
  - 1-1500posters
  - posters
  - posters1
canonical_poster_roots:
  - path: 1-1500posters
    recursive: false
  - path: posters/posters
    recursive: false
expected:
  release_movies: 4658
  quarantine_movies: 1264
  tier_counts: {S: 50, A: 313, B: 4295}
  pilot_movies: 24
  audit_rows: 1008
  poster_states:
    machine_passed: 4545
    content_conflict: 73
    placeholder: 9
    missing: 31
```

`config/gcp.yaml` must use a globally deterministic bucket name:

```yaml
project_id: motionexpaiweb
region: us-central1
required_account: admin@motionexp.com
archive_bucket_template: "{project_id}-{project_number}-hk-movie-rag-source-archive"
archive_prefix: source/v1.2
required_services:
  - cloudresourcemanager.googleapis.com
  - serviceusage.googleapis.com
  - storage.googleapis.com
```

Implement `resolve_input()` with `Path.resolve(strict=True)` and `is_relative_to(repo_root.resolve())`. Add `.gitignore` rules for `.env`, credentials, `.venv`, caches, Terraform state, raw input roots, generated data/assets, and `artifacts/`; do not ignore `schemas/`, `config/`, or `data/release/v1.2/release_manifest.json`.

- [ ] **Step 4: Lock dependencies and run focused quality checks**

Run: `uv lock`

Run: `uv run pytest tests/test_config.py -v`

Run: `uv run ruff check src/hk_movie_rag/config.py src/hk_movie_rag/cli.py tests/test_config.py`

Expected: all commands PASS.

- [ ] **Step 5: Commit the governed scaffold**

```powershell
git add .gitignore .env.example pyproject.toml uv.lock config src/hk_movie_rag/__init__.py src/hk_movie_rag/config.py src/hk_movie_rag/cli.py tests/conftest.py tests/test_config.py
git commit -m "build: scaffold governed RAG data foundation"
```

### Task 2: Build the immutable source inventory and duplicate evidence

**Files:**
- Create: `src/hk_movie_rag/hashing.py`
- Create: `src/hk_movie_rag/inventory.py`
- Create: `tests/test_inventory.py`
- Modify: `src/hk_movie_rag/cli.py`

**Interfaces:**
- Produces: `sha256_file(path: Path, chunk_size: int = 1024 * 1024) -> str`
- Produces: `inventory_paths(repo_root: Path, roots: Sequence[Path]) -> tuple[InventoryEntry, ...]`
- Produces: `build_inventory(settings: Settings) -> InventoryResult`
- Produces: `prove_mirrors(entries: Sequence[InventoryEntry]) -> MirrorProof`
- Produces: CLI `hk-movie-rag inventory --output artifacts/inventory/v1.2`.
- Output: sorted UTF-8 `source_inventory.jsonl`, `source_inventory.summary.json`, and `duplicate_proof.json`.

- [ ] **Step 1: Write failing tests for stable inventory and exact mirror proof**

```python
def test_inventory_is_sorted_and_hashes_bytes(tmp_path: Path) -> None:
    (tmp_path / "root").mkdir()
    (tmp_path / "root" / "b.bin").write_bytes(b"b")
    (tmp_path / "root" / "a.bin").write_bytes(b"a")
    result = inventory_paths(tmp_path, [Path("root")])
    assert [e.relative_path for e in result.entries] == ["root/a.bin", "root/b.bin"]
    assert result.entries[0].sha256 == hashlib.sha256(b"a").hexdigest()

def test_mirror_proof_fails_on_one_changed_byte(tmp_path: Path) -> None:
    entries = make_mirror_fixture(tmp_path, changed=True)
    with pytest.raises(InventoryError, match="mirror mismatch"):
        prove_mirrors(entries)
```

- [ ] **Step 2: Run the focused tests and confirm they fail**

Run: `uv run pytest tests/test_inventory.py -v`

Expected: FAIL because `inventory_paths` and `prove_mirrors` are undefined.

- [ ] **Step 3: Implement classification, hashing, and mirror proofs**

Every file entry must have this exact shape:

```python
class InventoryEntry(BaseModel):
    relative_path: str
    size_bytes: int
    sha256: str
    classification: Literal[
        "formal_release_source", "governance_evidence", "issue_ledger",
        "poster_candidate", "duplicate_candidate", "macos_metadata"
    ]
```

Classification must use configured roots plus exact structural rules, not age or filename alone. Mirror proof must compare relative filename, byte length, and SHA-256 for all 1,497 nested poster files and all 1,500 `posters1` files; extra, missing, or mismatched entries fail the run. JSONL serialization must use sorted entries, UTF-8, LF, compact separators, and no wall-clock field.

- [ ] **Step 4: Run tests and build the live inventory without changing inputs**

Run: `uv run pytest tests/test_inventory.py -v`

Run: `uv run hk-movie-rag inventory --output artifacts/inventory/v1.2`

Expected: tests PASS; the live summary reports the four configured source roots and duplicate proofs of exactly 1,497 and 1,500 files.

- [ ] **Step 5: Commit the inventory implementation**

```powershell
git add src/hk_movie_rag/hashing.py src/hk_movie_rag/inventory.py src/hk_movie_rag/cli.py tests/test_inventory.py
git commit -m "feat: add immutable source inventory"
```

### Task 3: Produce the canonical Release movie, tier, pilot, and provenance datasets

**Files:**
- Create: `src/hk_movie_rag/release_data.py`
- Create: `tests/test_release_data.py`
- Create: `tests/integration/test_v12_source.py`
- Modify: `src/hk_movie_rag/cli.py`

**Interfaces:**
- Consumes: `Settings`, source manifest, validation JSON, the formal Release CSV, and read-only sheets `補全審計`, `分級片單`, and `首批試點`.
- Produces: `build_release_data(settings: Settings, output_dir: Path) -> ReleaseDataResult`.
- Produces: `movies.csv`, `movies.parquet`, `movie_tiers.parquet`, `pilot_movies.parquet`, and `enrichment_provenance.parquet`.
- Produces: CLI `hk-movie-rag build-release-data`.

- [ ] **Step 1: Write failing mapping and boundary tests**

```python
MOVIE_COLUMNS = [
    "影片唯一ID", "中文片名", "英文片名", "上映日期", "出品地區", "導演",
    "編劇", "主演", "影片類型", "片長", "出品公司", "數據來源",
]

def test_release_rows_reject_duplicate_movie_id() -> None:
    rows = [movie_row("1970_GSQ_001"), movie_row("1970_GSQ_001")]
    with pytest.raises(ReleaseDataError, match="duplicate movie id"):
        validate_movie_rows(rows)

def test_quarantine_ids_cannot_enter_release() -> None:
    with pytest.raises(ReleaseDataError, match="quarantine overlap"):
        assert_disjoint({"1970_GSQ_001"}, {"1970_GSQ_001"})

def test_tier_join_is_exact_id_only() -> None:
    result = join_tiers([movie_row("1970_GSQ_001")], [tier_row("1970_GSQ_001", "B")])
    assert result[0].movie_id == "1970_GSQ_001"
    assert result[0].tier == "B"
```

- [ ] **Step 2: Run tests and verify the functions are absent**

Run: `uv run pytest tests/test_release_data.py -v`

Expected: FAIL on missing release-data functions.

- [ ] **Step 3: Implement deterministic extraction and serialization**

Read CSV with `encoding="utf-8-sig"`, preserve identifier/text columns as strings, normalize only line endings in the generated CSV, and reject any column-order change. Read Excel using `openpyxl.load_workbook(..., read_only=True, data_only=True)`; never save the source workbooks. Convert Excel dates to `YYYY-MM-DD`, sort every dataset by stable business keys, and write Parquet with fixed PyArrow settings and the locked dependency version.

The output schemas are:

```text
movies: the exact 12 source columns, renamed to documented English storage names
movie_tiers: movie_id, tier, tier_reason, human_review, pilot_movie
pilot_movies: movie_id, handbook_genre, evidence_note, evidence_url, source_record_id
enrichment_provenance: movie_id, chinese_title, release_date, field_name,
  original_value, filled_value, source_label, source_url, source_record_id,
  match_method, confidence, matched_imdb_id, retrieved_at, notes
```

Require the source manifest hash/size entries to match before reading business rows. Require source validation `ok=true` and `22/22`. Do not extract `問題明細` or `隔離記錄` into any release output.

- [ ] **Step 4: Add the live-source integration assertions**

```python
def test_v12_release_reconciles_exactly(repo_root: Path) -> None:
    result = build_release_data(Settings.load(repo_root), repo_root / "data/release/v1.2")
    assert result.movie_count == 4658
    assert result.unique_movie_ids == 4658
    assert result.tier_counts == {"S": 50, "A": 313, "B": 4295}
    assert result.pilot_count == 24
    assert result.provenance_count == 1008
    assert result.quarantine_overlap == 0
```

- [ ] **Step 5: Run focused and live-source tests twice**

Run: `uv run pytest tests/test_release_data.py tests/integration/test_v12_source.py -v`

Run: `uv run hk-movie-rag build-release-data`

Run: `Get-FileHash -Algorithm SHA256 data/release/v1.2/* | Sort-Object Path`

Run the build and hash command a second time.

Expected: all tests PASS and every generated artifact hash is unchanged on the second run.

- [ ] **Step 6: Commit canonical Release transformation**

```powershell
git add src/hk_movie_rag/release_data.py src/hk_movie_rag/cli.py tests/test_release_data.py tests/integration/test_v12_source.py
git commit -m "feat: build canonical Release v1.2 datasets"
```

### Task 4: Reconcile poster keys, quarantine collisions, and build derivatives

**Files:**
- Create: `src/hk_movie_rag/posters.py`
- Create: `tests/test_posters.py`
- Create: `tests/fixtures/images/valid.jpg`
- Create: `tests/fixtures/images/mislabeled.jpg`
- Modify: `tests/integration/test_v12_source.py`
- Modify: `src/hk_movie_rag/cli.py`

**Interfaces:**
- Consumes: canonical movie IDs and only the two configured canonical poster roots.
- Produces: `scan_posters(settings: Settings, movie_ids: set[str]) -> PosterScanResult`.
- Produces: `materialize_posters(scan: PosterScanResult, output_root: Path) -> PosterBuildResult`.
- Produces: `build_posters(settings: Settings, movie_ids: set[str], output_root: Path) -> PosterBuildResult`, the composed public operation used by the CLI and integration test.
- Produces: one-row-per-movie `data/release/v1.2/poster_manifest.parquet`.
- Produces: canonical originals/derivatives for machine-passed items and content-addressed conflict copies under `data/quarantine/poster_conflicts/`.
- Produces: CLI `hk-movie-rag build-posters`.

- [ ] **Step 1: Write failing tests for exact-key matching and image states**

```python
def test_unknown_filename_stem_is_rejected(release_ids: set[str], tmp_path: Path) -> None:
    image = tmp_path / "unknown.jpg"
    image.write_bytes(valid_jpeg_bytes())
    with pytest.raises(PosterError, match="orphan poster key"):
        scan_candidate(image, release_ids)

def test_mime_comes_from_content_not_suffix(tmp_path: Path) -> None:
    image = tmp_path / "1970_GSQ_001.jpg"
    image.write_bytes(valid_png_bytes())
    candidate = inspect_image(image, "1970_GSQ_001")
    assert candidate.detected_mime_type == "image/png"
    assert candidate.detected_extension == ".png"

def test_cross_key_equal_hash_is_never_promoted() -> None:
    result = classify_hash_groups(two_keys_same_bytes())
    assert {row.quality_status for row in result} == {"content_conflict"}
    assert not any(row.is_primary for row in result)
```

- [ ] **Step 2: Run the poster tests and confirm they fail**

Run: `uv run pytest tests/test_posters.py -v`

Expected: FAIL because poster inspection and classification are not implemented.

- [ ] **Step 3: Implement content detection, classification, and deterministic derivatives**

Use Pillow `Image.verify()` followed by a fresh decode. Capture detected MIME, width, height, mode, byte length, and SHA-256. Fail on same-key/different-content candidates. Treat the exact 65×91 repeated hash group as `placeholder`; if its key count is not 9, fail instead of generalizing the signature. Treat other cross-key hashes as `content_conflict`. Every Release ID must receive exactly one poster-state row, including `missing`.

For `machine_passed` assets only:

```python
with Image.open(source) as image:
    normalized = ImageOps.exif_transpose(image).convert("RGB")
    normalized.save(destination, format="WEBP", quality=90, method=6, exif=b"")
```

Name canonical originals using the detected extension, not the source suffix. Set `rights_status="unknown"`, `is_primary=False`, and `publishable=False` for every initial asset. Never copy placeholder bytes into active assets. Store conflict candidates under `<content_sha256>/<movie_id>.<detected-extension>`.

- [ ] **Step 4: Add exact live poster reconciliation gates**

```python
def test_v12_poster_population_reconciles(repo_root: Path) -> None:
    settings = Settings.load(repo_root)
    movie_ids = load_release_ids(repo_root / "data/release/v1.2/movies.parquet")
    result = build_posters(settings, movie_ids, repo_root)
    assert result.state_counts == {
        "machine_passed": 4545,
        "content_conflict": 73,
        "placeholder": 9,
        "missing": 31,
    }
    assert result.present_keys == 4627
    assert result.orphan_keys == set()
    assert result.detected_formats == {"JPEG": 4571, "PNG": 14, "WEBP": 42}
```

- [ ] **Step 5: Run poster tests and repeat the live build**

Run: `uv run pytest tests/test_posters.py tests/integration/test_v12_source.py -v`

Run: `uv run hk-movie-rag build-posters`

Run the build a second time and compare `poster_manifest.parquet` plus all derived hashes.

Expected: tests PASS; counts match exactly; the second build changes no hash.

- [ ] **Step 6: Commit the poster pipeline**

```powershell
git add src/hk_movie_rag/posters.py src/hk_movie_rag/cli.py tests/test_posters.py tests/fixtures/images tests/integration/test_v12_source.py
git commit -m "feat: reconcile and quarantine poster assets"
```

### Task 5: Enforce current and future data contracts

**Files:**
- Create: `schemas/movie.schema.json`
- Create: `schemas/media_asset.schema.json`
- Create: `schemas/movie_document.schema.json`
- Create: `schemas/release_manifest.schema.json`
- Create: `src/hk_movie_rag/staging.py`
- Create: `tests/test_staging.py`
- Create: `tests/fixtures/analysis/valid.md`
- Create: `docs/source-contract/poster-submission.md`
- Create: `docs/source-contract/movie-analysis-submission.md`
- Modify: `src/hk_movie_rag/cli.py`

**Interfaces:**
- Produces: `validate_poster_submission(path: Path, release_ids: set[str]) -> PosterSubmission`.
- Produces: `validate_analysis_submission(path: Path, release_ids: set[str]) -> MovieDocumentSubmission`.
- Produces: CLI `hk-movie-rag validate-staging --kind poster|analysis PATH`.

- [ ] **Step 1: Write failing contract tests**

```python
def test_analysis_rejects_unknown_movie_id(tmp_path: Path) -> None:
    path = write_analysis(tmp_path, movie_id="2099_UNKNOWN_001")
    with pytest.raises(StagingError, match="not in formal Release"):
        validate_analysis_submission(path, {"1970_GSQ_001"})

def test_poster_requires_source_rights_and_expected_hash(tmp_path: Path) -> None:
    path = write_poster_submission(tmp_path, metadata={"movie_id": "1970_GSQ_001"})
    with pytest.raises(StagingError, match="source_url.*rights_status.*expected_sha256"):
        validate_poster_submission(path, {"1970_GSQ_001"})

def test_unchanged_analysis_hash_is_idempotent() -> None:
    first = document_identity("1970_GSQ_001", "analysis-1", b"same")
    second = document_identity("1970_GSQ_001", "analysis-1", b"same")
    assert first == second
```

- [ ] **Step 2: Run the contract tests and confirm failure**

Run: `uv run pytest tests/test_staging.py -v`

Expected: FAIL on missing validators.

- [ ] **Step 3: Implement exact schema enums and validation**

Poster sidecars must require `schema_version`, `movie_id`, `submission_id`, `source_url`, `retrieved_at`, `rights_status`, and `expected_sha256`. Analysis front matter must require `schema_version`, `movie_id`, `document_id`, `document_type`, `language`, `title`, `source_urls`, `rights_status`, `authorship_method`, `content_version`, and `content_sha256`.

Reject additional properties unless the schema explicitly defines them. Validate IDs against the formal Release set before any copying. Recompute file/content SHA-256 and require exact agreement. Define publishability as `quality_status in {machine_passed, manual_approved}` and `rights_status == cleared`; no initial poster satisfies the rights condition.

- [ ] **Step 4: Run contract and schema tests**

Run: `uv run pytest tests/test_staging.py -v`

Run: `uv run hk-movie-rag validate-staging --kind analysis tests/fixtures/analysis/valid.md`

Expected: tests PASS and the valid fixture reports its deterministic document identity.

- [ ] **Step 5: Commit the contracts**

```powershell
git add schemas src/hk_movie_rag/staging.py src/hk_movie_rag/cli.py tests/test_staging.py docs/source-contract/poster-submission.md docs/source-contract/movie-analysis-submission.md
git commit -m "feat: enforce incremental data contracts"
```

### Task 6: Build the only production-authorizing release manifest

**Files:**
- Create: `src/hk_movie_rag/release_manifest.py`
- Create: `tests/test_release_manifest.py`
- Create: `docs/source-contract/release-v1.2.md`
- Create: `data/release/v1.2/release_manifest.json`
- Modify: `src/hk_movie_rag/cli.py`

**Interfaces:**
- Consumes: all canonical Release artifacts and their schema files.
- Produces: `build_release_manifest(settings: Settings) -> ReleaseManifest`.
- Produces: `verify_release_manifest(path: Path) -> VerificationResult`.
- Produces: CLI `hk-movie-rag build-manifest` and `hk-movie-rag verify-release`.

- [ ] **Step 1: Write failing manifest tamper and reconciliation tests**

```python
def test_manifest_verifier_rejects_tampered_artifact(tmp_path: Path) -> None:
    manifest = make_valid_manifest(tmp_path)
    (tmp_path / "movies.csv").write_bytes(b"tampered")
    with pytest.raises(ManifestError, match="SHA-256 mismatch"):
        verify_release_manifest(manifest)

def test_manifest_requires_one_poster_state_per_movie(valid_release: ReleaseFixture) -> None:
    valid_release.poster_rows.pop()
    with pytest.raises(ManifestError, match="poster rows=4657"):
        build_release_manifest(valid_release.settings)
```

- [ ] **Step 2: Run tests and confirm failure**

Run: `uv run pytest tests/test_release_manifest.py -v`

Expected: FAIL because manifest construction and verification are absent.

- [ ] **Step 3: Implement deterministic manifest construction**

Each artifact entry must contain `relative_path`, `schema_path`, `size_bytes`, `sha256`, and `row_count`. The manifest root must contain `release_version`, the source-manifest SHA-256, source `generated_at`, all expected count reconciliations, poster-state counts, and a sorted artifact list. It must not contain an absolute path, a current timestamp, a credential, or a GCS access token.

Verification order is: schema validity → source-manifest identity → artifact existence → size/hash → row counts → unique IDs → foreign-key membership → poster-state reconciliation → Quarantine disjointness. Stop at the first blocking failure and do not publish a partial manifest.

- [ ] **Step 4: Build and verify twice**

Run: `uv run pytest tests/test_release_manifest.py tests/integration/test_v12_source.py -v`

Run: `uv run hk-movie-rag build-manifest`

Run: `uv run hk-movie-rag verify-release data/release/v1.2/release_manifest.json`

Run the build and verification a second time.

Expected: both runs PASS and `Get-FileHash data/release/v1.2/release_manifest.json` is unchanged.

- [ ] **Step 5: Commit the release authority**

```powershell
git add src/hk_movie_rag/release_manifest.py src/hk_movie_rag/cli.py tests/test_release_manifest.py docs/source-contract/release-v1.2.md data/release/v1.2/release_manifest.json
git commit -m "feat: add fail-closed Release v1.2 manifest"
```

### Task 7: Implement and run the GCP authentication/read-only preflight

**Files:**
- Create: `src/hk_movie_rag/gcp_preflight.py`
- Create: `tests/test_gcp_preflight.py`
- Modify: `src/hk_movie_rag/cli.py`

**Interfaces:**
- Produces: `run_preflight(settings: Settings, runner: CommandRunner) -> GcpPreflightReport`.
- Produces: CLI `hk-movie-rag gcp-preflight --output artifacts/gcp/preflight.json`.
- Report fields: active account/project, project number/lifecycle, billing state, caller IAM visibility, required service states, existing bucket/Cloud SQL inventory, and `ready_for_archive`.

- [ ] **Step 1: Write failing tests for expired auth and wrong project**

```python
def test_preflight_fails_closed_on_expired_oauth(fake_gcloud: FakeRunner) -> None:
    fake_gcloud.fail("auth print-access-token", stderr="invalid_grant")
    report = run_preflight(settings(), fake_gcloud)
    assert report.ready_for_archive is False
    assert report.blockers == ["oauth_refresh_failed"]

def test_preflight_rejects_wrong_active_project(fake_gcloud: FakeRunner) -> None:
    fake_gcloud.project = "another-project"
    report = run_preflight(settings(), fake_gcloud)
    assert "active_project_mismatch" in report.blockers
```

- [ ] **Step 2: Run tests and confirm preflight is absent**

Run: `uv run pytest tests/test_gcp_preflight.py -v`

Expected: FAIL on missing preflight implementation.

- [ ] **Step 3: Implement bounded JSON gcloud queries**

Use argument arrays with `subprocess.run(..., shell=False, capture_output=True, text=True, timeout=60)`. Query `gcloud auth list`, `gcloud config get project`, `gcloud projects describe`, `gcloud billing projects describe`, `gcloud projects get-iam-policy`, `gcloud services list --enabled`, `gcloud storage buckets list`, and `gcloud sql instances list`, always with `--format=json` where supported. Redact tokens and credential paths from the report.

- [ ] **Step 4: Run tests, then perform the required interactive authentication checkpoint**

Run: `uv run pytest tests/test_gcp_preflight.py -v`

Run: `gcloud auth login admin@motionexp.com --update-adc`

Expected: a browser opens for the company account. The executor must pause for the user if the browser requires account selection, MFA, or consent; it must not switch to the secondary Gmail account.

- [ ] **Step 5: Run and inspect the live read-only preflight**

Run: `uv run hk-movie-rag gcp-preflight --output artifacts/gcp/preflight.json`

Expected: `ready_for_archive=true`, project `motionexpaiweb`, active account `admin@motionexp.com`, lifecycle `ACTIVE`, billing enabled, and all required services enabled or explicitly eligible to enable. If any field fails, stop before Terraform, upload, or cleanup.

- [ ] **Step 6: Commit the preflight implementation**

```powershell
git add src/hk_movie_rag/gcp_preflight.py src/hk_movie_rag/cli.py tests/test_gcp_preflight.py
git commit -m "feat: add fail-closed GCP preflight"
```

### Task 8: Provision the isolated archive bucket and writer identity

**Files:**
- Create: `infra/terraform/archive/versions.tf`
- Create: `infra/terraform/archive/variables.tf`
- Create: `infra/terraform/archive/main.tf`
- Create: `infra/terraform/archive/outputs.tf`
- Modify: `.gitignore`

**Interfaces:**
- Consumes: verified `artifacts/gcp/preflight.json` and project number.
- Produces: bucket `motionexpaiweb-${project_number}-hk-movie-rag-source-archive` and service account `hk-rag-archive-writer`.
- Produces: Terraform outputs `archive_bucket_name` and `archive_writer_service_account`.

- [ ] **Step 1: Write Terraform checks before applying**

`main.tf` must express these exact controls:

```hcl
resource "google_storage_bucket" "source_archive" {
  name                        = "${var.project_id}-${var.project_number}-hk-movie-rag-source-archive"
  project                     = var.project_id
  location                    = "US-CENTRAL1"
  uniform_bucket_level_access = true
  public_access_prevention    = "enforced"
  force_destroy               = false
  versioning { enabled = true }
  soft_delete_policy { retention_duration_seconds = 604800 }
}
```

Add only object create/view permissions required for the writer and do not add public members, owner/editor grants, retention lock, or key creation.

- [ ] **Step 2: Initialize and validate without mutation**

Run: `terraform -chdir=infra/terraform/archive init`

Run: `terraform -chdir=infra/terraform/archive fmt -check`

Run: `terraform -chdir=infra/terraform/archive validate`

Expected: all commands PASS.

- [ ] **Step 3: Create and review the plan**

```powershell
$preflight = Get-Content -Raw -LiteralPath 'artifacts/gcp/preflight.json' | ConvertFrom-Json
if (-not $preflight.ready_for_archive) { throw 'GCP archive preflight is not ready' }
$projectNumber = [string]$preflight.project_number
terraform -chdir=infra/terraform/archive plan -out=archive.tfplan -var="project_id=motionexpaiweb" -var="project_number=$projectNumber"
```

Expected: only one private bucket, one service account, and narrow IAM bindings are proposed. No delete/replacement action is allowed.

- [ ] **Step 4: Apply the reviewed archive plan**

Run: `terraform -chdir=infra/terraform/archive apply archive.tfplan`

Run: `gcloud storage buckets describe gs://<configured-archive-bucket> --format=json`

Expected: location `US-CENTRAL1`, uniform bucket-level access enabled, public access prevention enforced, versioning enabled, and seven-day soft delete active.

- [ ] **Step 5: Commit infrastructure code, never state or plan files**

```powershell
git add infra/terraform/archive .gitignore
git commit -m "infra: define private GCS source archive"
```

### Task 9: Upload the unchanged source inventory and verify every remote SHA-256

**Files:**
- Create: `src/hk_movie_rag/gcs_archive.py`
- Create: `tests/test_gcs_archive.py`
- Modify: `src/hk_movie_rag/cli.py`

**Interfaces:**
- Consumes: `source_inventory.jsonl`, its digest, verified GCP preflight, and Terraform bucket output.
- Produces: `upload_archive(...) -> ArchiveUploadReport`.
- Produces: `verify_archive(...) -> ArchiveVerificationReport`.
- Produces: CLI `hk-movie-rag archive-upload` and `hk-movie-rag archive-verify`.
- Writes remote objects below `source/v1.2/files/<inventory-relative-path>` plus immutable inventory/report objects below `source/v1.2/manifests/`.

- [ ] **Step 1: Write failing idempotency and read-back tests with a fake GCS client**

```python
def test_existing_object_with_different_hash_aborts(fake_bucket: FakeBucket) -> None:
    fake_bucket.add("source/v1.2/files/a", b"different", metadata={"source_sha256": "bad"})
    with pytest.raises(ArchiveError, match="existing object mismatch"):
        upload_one(fake_bucket, inventory_entry("a", b"expected"))

def test_verify_streams_remote_bytes_not_only_metadata(fake_bucket: FakeBucket) -> None:
    entry = inventory_entry("a", b"expected")
    fake_bucket.add(entry.object_name, b"tampered", metadata={"source_sha256": entry.sha256})
    with pytest.raises(ArchiveError, match="remote SHA-256 mismatch"):
        verify_one(fake_bucket, entry)
```

- [ ] **Step 2: Run tests and confirm archive functions are missing**

Run: `uv run pytest tests/test_gcs_archive.py -v`

Expected: FAIL on missing archive functions.

- [ ] **Step 3: Implement idempotent upload and streamed verification**

Before upload, recompute each local file hash and compare it with the frozen inventory. New objects use `if_generation_match=0` and metadata `source_sha256`, `release_version`, and `inventory_digest`. Existing objects are skipped only after size, metadata hash, and streamed remote hash all agree. Verification lists exactly the expected prefix, rejects extras/missing objects, downloads every object as a stream, and computes SHA-256 over returned bytes.

The successful report must include bucket, prefix, object count, total bytes, inventory digest, per-object generation, per-object hash result, verification timestamp, and `verified=true`. Upload the final verification report with a create-only precondition; never overwrite a prior successful run.

- [ ] **Step 4: Run unit tests, upload, and verify**

Run: `uv run pytest tests/test_gcs_archive.py -v`

Run: `uv run hk-movie-rag archive-upload --inventory artifacts/inventory/v1.2/source_inventory.jsonl --bucket <configured-archive-bucket>`

Run: `uv run hk-movie-rag archive-verify --inventory artifacts/inventory/v1.2/source_inventory.jsonl --bucket <configured-archive-bucket> --output artifacts/archive/v1.2/verification.json`

At execution, both archive commands run the current read-only GCP preflight, obtain a short-lived access token for the exact verified account, and construct one explicit-token Storage client. That same client reads the generation-pinned, size-bounded default state object from the fixed Terraform backend bucket/prefix and validates the strict archive bucket, writer, and resource contract before any archive request. The supplied bucket must equal the bucket derived from `archive_bucket_template` and the validated remote state; caller-provided evidence files, ambient ADC/tokens, local Terraform execution, and command substitutions do not authorize an archive.

Expected: exact object count, exact total bytes, zero missing/extra objects, zero size mismatches, zero streamed SHA-256 mismatches, and `verified=true`. Any failure stops Task 10.

- [ ] **Step 5: Commit archive implementation**

```powershell
git add src/hk_movie_rag/gcs_archive.py src/hk_movie_rag/cli.py tests/test_gcs_archive.py
git commit -m "feat: archive and verify immutable source bytes"
```

### Task 10: Execute only the archive-gated safe cleanup and close Phase 1

**Files:**
- Create: `src/hk_movie_rag/cleanup.py`
- Create: `tests/test_cleanup.py`
- Modify: `src/hk_movie_rag/cli.py`
- Modify: `README.md`

**Interfaces:**
- Consumes: source inventory, duplicate proof, archive verification report, workspace root, and release version.
- Produces: `build_cleanup_plan(...) -> CleanupPlan` with exact resolved targets and proof digests.
- Produces: `execute_cleanup(plan: CleanupPlan, confirmation: str) -> CleanupReport` that stops at proof-verified external quarantine.
- Produces: CLI `hk-movie-rag cleanup-plan` and `hk-movie-rag cleanup-execute`.

- [ ] **Step 1: Write failing tests for every destructive boundary**

```python
def test_cleanup_refuses_unverified_archive(valid_inputs: CleanupFixture) -> None:
    valid_inputs.archive_report["verified"] = False
    with pytest.raises(CleanupError, match="archive is not verified"):
        build_cleanup_plan(**valid_inputs.kwargs)

def test_cleanup_rejects_workspace_root_or_unlisted_path(valid_plan: CleanupPlan) -> None:
    valid_plan.targets.append(valid_plan.workspace_root)
    with pytest.raises(CleanupError, match="target is not allowlisted"):
        validate_cleanup_plan(valid_plan)

def test_cleanup_requires_exact_release_confirmation(valid_plan: CleanupPlan) -> None:
    with pytest.raises(CleanupError, match="confirmation mismatch"):
        execute_cleanup(valid_plan, confirmation="v1.1")
```

- [ ] **Step 2: Run cleanup tests and confirm failure**

Run: `uv run pytest tests/test_cleanup.py -v`

Expected: FAIL on missing cleanup plan/executor.

- [ ] **Step 3: Implement exact, recoverable cleanup**

The plan may contain only:

```text
<workspace>/posters/__MACOSX
<workspace>/1-1500posters/1-1500posters
<workspace>/posters1
each inventoried .DS_Store below the four configured source roots
```

Resolve every target immediately before action. Require it to be below the workspace, not equal to the workspace, not a symlink/reparse point escaping containment, and exactly present in the allowlist. Recompute mirror proofs and compare the inventory and archive-verification digests. Bind a caller-approved exact quarantine root that is absent, workspace-external, same-volume, and reached through stable non-reparse ancestors. Journal intent before the atomic rename, accept a target only after complete proof at its unique slot, and never overwrite or roll back either namespace. If one target fails, stop and report only earlier proof-accepted quarantines.

- [ ] **Step 4: Generate and review the dry-run plan**

Run: `uv run hk-movie-rag cleanup-plan --inventory artifacts/inventory/v1.2/source_inventory.jsonl --archive-verification artifacts/archive/v1.2/verification.json --quarantine-root <reviewed-absolute-external-same-volume-path> --output artifacts/cleanup/v1.2/plan.json`

Run: `Get-Content -Raw artifacts/cleanup/v1.2/plan.json`

Expected: only the three exact directories plus inventoried `.DS_Store` files appear. `FinalDelivery_2026-07-16`, outer `1-1500posters`, `posters/posters`, `.git`, `docs`, canonical data, and canonical assets do not appear.

- [ ] **Step 5: Execute with an exact release confirmation**

Run: `uv run hk-movie-rag cleanup-execute --plan artifacts/cleanup/v1.2/plan.json --confirm-release v1.2 --output artifacts/cleanup/v1.2/report.json`

Expected: all planned targets are proof-verified in their bound external quarantine slots and no unrelated path changes. The report records exact targets, slots, sizes, source hashes/proof digests, durable intents, and quarantine status. Any purge or other destructive follow-up remains separately unauthorized.

- [ ] **Step 6: Re-run the complete local acceptance ladder**

Run: `uv run ruff check .`

Run: `uv run mypy src/hk_movie_rag`

Run: `uv run pytest -q`

Run: `uv run hk-movie-rag verify-release data/release/v1.2/release_manifest.json`

Run: `uv run hk-movie-rag archive-verify --inventory artifacts/inventory/v1.2/source_inventory.jsonl --bucket <configured-archive-bucket> --output artifacts/archive/v1.2/post-cleanup-verification.json`

Run: `Get-ChildItem -LiteralPath . -Force | Select-Object Name`

Expected: lint, types, tests, Release verification, and remote archive verification PASS; the redundant directories and Mac metadata are absent; unique sources and canonical outputs remain.

- [ ] **Step 7: Document the operator boundary and commit Phase 1**

`README.md` must state the only authorized production entrypoint, all current dataset counts, poster rights blocking, how to add future posters/analysis documents, GCS recovery location format, and that Cloud SQL/embedding/query work has not yet been implemented.

```powershell
git add src/hk_movie_rag/cleanup.py src/hk_movie_rag/cli.py tests/test_cleanup.py README.md
git commit -m "feat: complete archive-gated data cleanup"
git status --short
```

Expected: only intentionally ignored raw/generated artifacts remain outside Git; no unexpected tracked or untracked code/configuration file is present.

## Phase 1 Completion Evidence

Phase 1 is complete only when one final report can point to all of the following:

- the committed source inventory implementation and local inventory digest;
- exact Release v1.2 row, ID, tier, pilot, provenance, and poster-state reconciliations;
- deterministic Release artifact hashes from two consecutive builds;
- a manifest verification result that excludes Quarantine/issue-ledger rows;
- current GCP account/project/billing/API preflight evidence;
- Terraform state showing only the private archive bucket and archive-writer identity for this phase;
- a GCS verification report based on streamed remote SHA-256, not metadata alone;
- the reviewed cleanup plan and exact cleanup report;
- post-cleanup Ruff, mypy, pytest, Release verification, and remote archive verification results;
- a clean Git status for all code, contracts, tests, docs, and infrastructure definitions.

The next plan begins only after this evidence is reviewed. It will cover Cloud SQL PostgreSQL/pgvector, database migrations, idempotent loading, Cloud Run Jobs, Vertex AI embedding evaluation, and the query service.
