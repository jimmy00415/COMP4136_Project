# Hong Kong Movie Knowledge Graph + RAG Graph Atlas Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** Add a deterministic Release v1.2 knowledge-graph projection and make an interactive Graph Atlas the primary authenticated demo, while keeping the existing grounded RAG chat available as a secondary relationship-explanation capability.

**Architecture:** Build an immutable four-file graph projection from the manifest-authorized movie Parquet and two committed policy files; load and re-derive it atomically beside the existing RAG tables in PostgreSQL; expose six bounded, authenticated graph routes through focused repository/query/router modules; and serve a locally bundled Cytoscape.js Graph Atlas from the existing FastAPI application. The feature remains disabled by default and becomes queryable only when the packaged projection authority, environment digest, active RAG release, and active database projection all match exactly.

**Tech Stack:** Python 3.11-3.13, FastAPI, Pydantic v2, psycopg 3, PostgreSQL 16, pgvector, PyArrow 21.0.0, OpenCC 1.4.1, Google Cloud Storage, Vertex AI, vanilla ES modules, Cytoscape.js 3.34.0, Node.js test runner, pytest, Ruff, mypy, PowerShell, Cloud Run, and Cloud Run Jobs.

## Global Constraints

- Graph entity and edge authority is only `data/release/v1.2/release_manifest.json` plus its authorized `movies.parquet`; governed PDFs may support explanations but may not create nodes or edges.
- Projection ID is exactly `v1.2-kg1`, parent RAG release is exactly `v1.2-demo`, and the create-only GCS prefix is exactly `graph/v1.2-kg1/`.
- The builder reads exactly five inputs: the Release manifest, its authorized Parquet, `config/rag_demo.yaml`, `config/kg/normalization.v1.json`, and `config/kg/recommendation.v1.json`. It performs no recursive discovery or network enrichment.
- Expected projection counts are exactly 10,730 entities, 14,602 aliases, and 32,083 unique direct edges, including every per-type and per-predicate count in the approved design.
- The 14 duplicate screenwriter occurrences must remain in evidence positions while collapsing to one direct edge; all three simplified alias collisions must remain explicit candidate sets.
- Direct predicates are only `DIRECTED_BY`, `WRITTEN_BY`, `ACTED_IN`, and `HAS_GENRE`. Reverse traversal is computed, and no collaboration/co-starring edge is persisted.
- Person entities are Release exact-name equivalence clusters with `identity_status=name_only`; no person node may claim external identity resolution.
- Deterministic JSON, IDs, sort orders, evidence digests, artifact hashes, and projection digest must follow the approved canonical encoding byte for byte.
- Graph persistence stays in additive PostgreSQL tables. Do not add Neo4j, AGE, GraphQL, another database, new entity/edge embeddings, or model-generated SQL.
- Activation must re-derive the complete graph from the locked active database movie payloads and compare it with staged artifacts before setting `status=active`; uploaded JSONL is never trusted by itself.
- Projection rows are immutable after a terminal state. A conflicting replay must not mutate an active projection or leave queryable partial rows.
- Every online graph transaction sets a local 2,000 ms PostgreSQL statement timeout; ingestion/activation uses a separate 600,000 ms timeout.
- All graph routes authenticate before revealing feature state. Disabled routes return `404` with no projection metadata; an invalid or unavailable bound projection returns a controlled error and never falls back to generated facts.
- Search and neighborhood cursors are base64url/HMAC authenticated, endpoint/query/projection bound, and expire within 600 seconds. The existing access key derives the cursor key; no new secret is introduced.
- Traversal is at most three edges. The server reloads all caller-supplied IDs and paths from the exact active projection and accepts no caller-supplied predicates, edge bodies, SQL, citation IDs, or evidence text.
- Default related-movie ranking is the committed same-role graph heuristic. Graph score must be positive; cross-role-only connections are explorable but never recommendation candidates.
- Empty preference text causes zero embedding and zero Gemini calls. Non-empty preference text may cause exactly one query embedding and may only tie-break equal graph-score groups. Recommendations never call Gemini.
- Relationship explanation is single-turn. Gemini receives only the current question plus server-reloaded evidence; every selected graph citation must occur exactly once and in path order, and foreign/missing/duplicate/reordered citations reject the complete generated answer.
- Movie posters remain private and are exposed only as `/api/posters/{movie_id}` after the existing poster authority check. Graph tables and responses never expose GCS object paths.
- Graph Atlas is the primary enabled home; `/api/chat` remains intact. With `RAG_GRAPH_ENABLED=false`, the existing chat home and API behavior are unchanged.
- Use locally vendored Cytoscape.js `3.34.0` only. Commit the ESM bytes, MIT license, lockfile, and computed SHA authority; the browser must make no CDN request.
- The UI uses the approved dark Graph Atlas hierarchy, explicit `Follow relationship` actions, a semantic list equivalent, desktop/tablet/mobile layouts, keyboard operation, focus management, and reduced-motion support. Node inspection alone never changes the breadcrumb.
- Do not add generated faces, scraped portraits, poster crops as person images, emoji, handcrafted SVG, or decorative factual content.
- Preserve all unrelated user changes and untracked PDFs/`tmp/`. Stage only files named by the current task.
- Local green tests prove implementation readiness only. GCS upload, Job execution, Cloud Run deployment, traffic promotion, and production readiness remain separate evidence states.
- Cloud execution is not authorized by this plan. Stop after local/dry-run gates unless the user separately authorizes live cloud mutation.

---

## Exact File Map

Create these committed files:

```text
config/kg/normalization.v1.json
config/kg/recommendation.v1.json
migrations/0004_knowledge_graph.sql
package.json
package-lock.json
schemas/kg_alias.schema.json
schemas/kg_edge.schema.json
schemas/kg_entity.schema.json
schemas/kg_projection_manifest.schema.json
scripts/vendor-cytoscape.mjs
docs/runbooks/graph-atlas-rollout.md
src/hk_movie_rag/authorities/graph_atlas_assets_v1.json
src/hk_movie_rag/authorities/kg_projection_v1_2_kg1.json
src/hk_movie_rag/kg_api.py
src/hk_movie_rag/kg_bundle.py
src/hk_movie_rag/kg_canonical.py
src/hk_movie_rag/kg_cursor.py
src/hk_movie_rag/kg_db.py
src/hk_movie_rag/kg_demo.py
src/hk_movie_rag/kg_grounding.py
src/hk_movie_rag/kg_ingest.py
src/hk_movie_rag/kg_policy.py
src/hk_movie_rag/kg_projection.py
src/hk_movie_rag/kg_query.py
src/hk_movie_rag/kg_runtime.py
src/hk_movie_rag/kg_types.py
src/hk_movie_rag/rag_payload.py
src/hk_movie_rag/migrations/0004_knowledge_graph.sql
src/hk_movie_rag/policies/kg_normalization_v1.json
src/hk_movie_rag/policies/kg_recommendation_v1.json
src/hk_movie_rag/schemas/kg_alias.schema.json
src/hk_movie_rag/schemas/kg_edge.schema.json
src/hk_movie_rag/schemas/kg_entity.schema.json
src/hk_movie_rag/schemas/kg_projection_manifest.schema.json
src/hk_movie_rag/static/api.js
src/hk_movie_rag/static/chat.js
src/hk_movie_rag/static/cytoscape-3.34.0.LICENSE.txt
src/hk_movie_rag/static/cytoscape-3.34.0.esm.min.mjs
src/hk_movie_rag/static/graph-atlas.js
src/hk_movie_rag/static/graph-canvas.js
src/hk_movie_rag/static/graph-explanation.js
src/hk_movie_rag/static/graph-inspector.js
src/hk_movie_rag/static/graph-state.js
tests/graph_atlas_ui_contract.mjs
tests/graph_state_contract.mjs
tests/integration/test_kg_postgres.py
tests/integration/test_kg_v12_projection.py
tests/static_graph_asset_contract.mjs
tests/test_graph_asset_package.py
tests/test_kg_api.py
tests/test_kg_bundle.py
tests/test_kg_canonical.py
tests/test_kg_cursor.py
tests/test_kg_db.py
tests/test_kg_grounding.py
tests/test_kg_ingest.py
tests/test_kg_policy.py
tests/test_kg_query.py
tests/test_kg_runtime.py
tests/test_rag_payload.py
```

Modify these existing files:

```text
.env.example
.gitattributes
.gitignore
README.md
pyproject.toml
uv.lock
scripts/gcp/deploy-demo.ps1
scripts/gcp/live-smoke.py
src/hk_movie_rag/cli.py
src/hk_movie_rag/demo_api.py
src/hk_movie_rag/rag_bundle.py
src/hk_movie_rag/rag_db.py
src/hk_movie_rag/vertex_clients.py
src/hk_movie_rag/static/app.js
src/hk_movie_rag/static/index.html
src/hk_movie_rag/static/styles.css
tests/static_app_ui_contract.mjs
tests/test_demo_api.py
tests/test_gcp_demo_scripts.py
tests/test_rag_bundle.py
tests/test_rag_db.py
tests/test_vertex_clients.py
```

Generated local projection outputs under `artifacts/kg/` and `node_modules/` are ignored. The committed packaged projection authority and vendored browser asset are reviewed source artifacts, not ignored build debris. Do not modify `infra/terraform/archive/**`, `Dockerfile`, or `.dockerignore`: the graph uses the existing release bucket/service account and every runtime file is packaged below `src/`.

---

### Task 1: Extract one authoritative RAG movie-payload serializer

**Files:**
- Create: `src/hk_movie_rag/rag_payload.py`
- Create: `tests/test_rag_payload.py`
- Modify: `src/hk_movie_rag/rag_bundle.py`
- Modify: `src/hk_movie_rag/rag_db.py`
- Modify: `tests/test_rag_bundle.py`
- Modify: `tests/test_rag_db.py`

**Interfaces:**
- Consumes: one validated Release movie mapping using the existing RAG storage columns.
- Produces: `build_rag_movie_record(movie: Mapping[str, object]) -> dict[str, object]`.
- Produces: `canonical_rag_movie_payload_bytes(record: Mapping[str, object]) -> bytes`.
- Produces: `rag_movie_payload_sha256(record: Mapping[str, object]) -> str`.
- Invariant: the RAG bundle, database upsert, active-record verification, and graph projection all hash the identical full movie record, including `record_kind="movie"` and every governed storage field.

- [ ] **Step 1: Write a failing golden-byte test before moving implementation code**

```python
def test_rag_movie_payload_has_one_canonical_byte_contract() -> None:
    record = build_rag_movie_record(
        {"movie_id": "1978_ZQ_001", "chinese_title": "醉拳"}
    )

    assert canonical_rag_movie_payload_bytes(record) == (
        '{"chinese_title":"醉拳","movie_id":"1978_ZQ_001",'
        '"record_kind":"movie"}'
    ).encode("utf-8")
    assert rag_movie_payload_sha256(record) == (
        "81599e7fbea2c8fd12173094a1ca9e1ffddd67b2f3e91767f8da77334a2f5d85"
    )
```

- [ ] **Step 2: Prove the new module is absent**

Run: `uv run pytest tests/test_rag_payload.py -q`

Expected: FAIL with `ModuleNotFoundError: No module named 'hk_movie_rag.rag_payload'`.

- [ ] **Step 3: Implement strict canonical bytes and validation**

```python
def canonical_rag_movie_payload_bytes(record: Mapping[str, object]) -> bytes:
    if record.get("record_kind") != "movie":
        raise RagPayloadError("RAG movie record_kind must be movie")
    return json.dumps(
        dict(record),
        ensure_ascii=False,
        sort_keys=True,
        separators=(",", ":"),
        allow_nan=False,
    ).encode("utf-8")


def rag_movie_payload_sha256(record: Mapping[str, object]) -> str:
    return hashlib.sha256(canonical_rag_movie_payload_bytes(record)).hexdigest()
```

`build_rag_movie_record()` must reject a caller-supplied conflicting `record_kind`, copy the mapping, and prepend exactly `record_kind="movie"` without mutating the input.

- [ ] **Step 4: Replace every duplicate movie-record/hash path**

Make `rag_bundle._records()`, `RagRepository._active_bundle_records()`, and movie upsert use the public serializer. Keep existing non-movie canonical JSON behavior unchanged. Add regression assertions that a built bundle's movie record and persisted `payload_sha256` still match prior semantics.

- [ ] **Step 5: Run focused compatibility tests**

Run: `uv run pytest tests/test_rag_payload.py tests/test_rag_bundle.py tests/test_rag_db.py -q`

Expected: PASS; existing RAG bundle bytes and database replay tests remain green.

- [ ] **Step 6: Commit only this extraction**

```powershell
git add src/hk_movie_rag/rag_payload.py src/hk_movie_rag/rag_bundle.py src/hk_movie_rag/rag_db.py tests/test_rag_payload.py tests/test_rag_bundle.py tests/test_rag_db.py
git commit -m "refactor: centralize rag movie payload hashing"
```

---

### Task 2: Define graph policies, canonical records, schemas, and IDs

**Files:**
- Create: `config/kg/normalization.v1.json`
- Create: `config/kg/recommendation.v1.json`
- Create: `src/hk_movie_rag/policies/kg_normalization_v1.json`
- Create: `src/hk_movie_rag/policies/kg_recommendation_v1.json`
- Create: `schemas/kg_entity.schema.json`
- Create: `schemas/kg_alias.schema.json`
- Create: `schemas/kg_edge.schema.json`
- Create: `schemas/kg_projection_manifest.schema.json`
- Create: `src/hk_movie_rag/schemas/kg_entity.schema.json`
- Create: `src/hk_movie_rag/schemas/kg_alias.schema.json`
- Create: `src/hk_movie_rag/schemas/kg_edge.schema.json`
- Create: `src/hk_movie_rag/schemas/kg_projection_manifest.schema.json`
- Create: `src/hk_movie_rag/kg_types.py`
- Create: `src/hk_movie_rag/kg_policy.py`
- Create: `src/hk_movie_rag/kg_canonical.py`
- Create: `tests/test_kg_policy.py`
- Create: `tests/test_kg_canonical.py`
- Modify: `pyproject.toml`
- Modify: `uv.lock`
- Modify: `.gitattributes`

**Interfaces:**
- Consumes: committed root policy bytes and exact OpenCC package/config versions.
- Produces: `GraphNormalizationPolicy`, `GraphRecommendationPolicy`, `GraphEntityRecord`, `GraphAliasRecord`, `GraphEdgeRecord`, and `GraphProjectionRecords` frozen dataclasses.
- Produces: `load_graph_normalization_policy(path: Path) -> GraphNormalizationPolicy` and `load_graph_recommendation_policy(path: Path) -> GraphRecommendationPolicy`.
- Produces: `canonical_json_bytes(value: object) -> bytes`, `sha256_hex(domain: str, payload: bytes) -> str`, `normalize_source_label(value: str) -> str`, `normalize_query_key(value: str) -> str`, and `split_source_value(value: str) -> tuple[tuple[int, str], ...]`.
- Produces deterministic `person_entity_id()`, `genre_entity_id()`, `alias_id()`, `edge_id()`, evidence-set digest, edge-payload digest, and RAG movie-set digest helpers.
- Invariant: each root policy/schema file is byte-identical to its packaged mirror.

- [ ] **Step 1: Pin the normalization engine and write policy parity failures**

Change the runtime dependency from `opencc>=1.3,<2` to exactly `opencc==1.4.1`, regenerate `uv.lock`, and write tests that reject any policy whose declared package version/config differs from the imported runtime.

The canonical root normalization policy must encode these exact decisions:

```json
{"entity_id_max_ascii_bytes":256,"opencc":{"equivalence_config":"hk2t","package":"opencc","simplified_query_config":"hk2s","version":"1.4.1"},"policy_id":"kg-normalization-v1","projection_id":"v1.2-kg1","query_casefold":"unicode-default","schema_version":"1.0","separator_pattern":"[、,，/;；]","source_field_max_codepoints":4096,"source_label_max_codepoints":256,"unicode_normalization":"NFC"}
```

The canonical recommendation policy must encode exactly:

```json
{"candidate_requires_positive_graph_score":true,"component_order":["shared_director","shared_screenwriter","shared_cast","shared_genre"],"contribution_caps":{"shared_cast":3,"shared_genre":2},"policy_id":"kg-recommendation-v1","same_role_only":true,"schema_version":"1.0","semantic_tiebreak":{"embedding_calls":1,"enabled":true,"max_preference_codepoints":500,"passage_kind":"movie_metadata","within_equal_graph_score_only":true},"weights":{"shared_cast":2,"shared_director":5,"shared_genre":1,"shared_screenwriter":4}}
```

Both files use UTF-8 without BOM, canonical key order, no spaces, and one final newline. The packaged copies must match with `read_bytes()` equality.

- [ ] **Step 2: Write failing canonical-vector and schema tests**

```python
def test_domain_separated_ids_match_golden_vectors() -> None:
    assert person_entity_id("成龍") == (
        "person-name:480cd3f8abdb3584fe4f70eeb134ef4d97a2dcba5dc1bbdeb7d1d7940b028bcd"
    )
    assert genre_entity_id("動作") == "genre:%E5%8B%95%E4%BD%9C"
    assert alias_id("v1.2-kg1", "movie:1978_ZQ_001", "醉拳") == (
        "4e286f22db5352543547848ba87a0222d6859339b36208e7a130eb6fddf8e883"
    )
    assert edge_id(
        "v1.2-kg1",
        "v1.2-demo",
        "movie:1978_ZQ_001",
        "DIRECTED_BY",
        "person-name:62630d4373189c93198bfe2348f71812a1d495b0a582b66c6a52e97caf9fb51a",
        "1978_ZQ_001",
        "director",
    ) == "27bbe6a800ab906970b45d51523e59a18d01454e0c3a3131e81ba9857c71aa49"


def test_split_positions_precede_empty_token_removal() -> None:
    assert split_source_value("袁和平、、成龍") == ((0, "袁和平"), (2, "成龍"))
```

Add tests for strict UTF-8/NFC handling, source-label whitespace preservation, query-key whitespace collapse/casefold, OpenCC `hk2t` equivalence, `hk2s` derived search keys, RFC 3986 genre IDs, canonical JSON NaN rejection, final JSONL newline, deterministic sort keys, and entity/alias/edge `additionalProperties=false` schema rejection.

- [ ] **Step 3: Confirm RED**

Run: `uv run pytest tests/test_kg_policy.py tests/test_kg_canonical.py -q`

Expected: FAIL because graph policy/canonical modules and schemas do not exist.

- [ ] **Step 4: Implement canonical types and functions without projection I/O**

Use literals rather than free strings:

```python
GraphEntityType = Literal["movie", "person", "genre"]
GraphPredicate = Literal["DIRECTED_BY", "WRITTEN_BY", "ACTED_IN", "HAS_GENRE"]
GraphIdentityStatus = Literal["authoritative", "name_only"]


def sha256_hex(domain: str, payload: bytes) -> str:
    return hashlib.sha256(domain.encode("utf-8") + b"\0" + payload).hexdigest()
```

Implement the exact occurrence-record fields and sort key from the approved design. `source_value_sha256` hashes the untouched Parquet string bytes; display labels are separately NFC-normalized/trimmed. Validate predicate directions and movie/person/genre digest nullability in dataclass constructors and JSON Schema.

- [ ] **Step 5: Verify policy/schema package parity and types**

Run: `uv run pytest tests/test_kg_policy.py tests/test_kg_canonical.py -q`

Expected: PASS, including byte parity between root and packaged resources.

Run: `uv run mypy src/hk_movie_rag/kg_types.py src/hk_movie_rag/kg_policy.py src/hk_movie_rag/kg_canonical.py`

Expected: PASS with no type errors.

- [ ] **Step 6: Commit the complete canonical contract**

```powershell
git add config/kg/normalization.v1.json config/kg/recommendation.v1.json schemas/kg_entity.schema.json schemas/kg_alias.schema.json schemas/kg_edge.schema.json schemas/kg_projection_manifest.schema.json src/hk_movie_rag/policies/kg_normalization_v1.json src/hk_movie_rag/policies/kg_recommendation_v1.json src/hk_movie_rag/schemas/kg_entity.schema.json src/hk_movie_rag/schemas/kg_alias.schema.json src/hk_movie_rag/schemas/kg_edge.schema.json src/hk_movie_rag/schemas/kg_projection_manifest.schema.json src/hk_movie_rag/kg_types.py src/hk_movie_rag/kg_policy.py src/hk_movie_rag/kg_canonical.py tests/test_kg_policy.py tests/test_kg_canonical.py pyproject.toml uv.lock .gitattributes
git commit -m "feat: define governed graph contracts"
```

---

### Task 3: Build, verify, and bind the deterministic projection authority

**Files:**
- Create: `src/hk_movie_rag/kg_projection.py`
- Create: `src/hk_movie_rag/kg_bundle.py`
- Create: `src/hk_movie_rag/authorities/kg_projection_v1_2_kg1.json`
- Create: `tests/test_kg_bundle.py`
- Create: `tests/integration/test_kg_v12_projection.py`
- Modify: `src/hk_movie_rag/cli.py`
- Modify: `.gitattributes`

**Interfaces:**
- Consumes: exactly the five authorized inputs and the shared RAG payload serializer from Task 1.
- Produces: `derive_graph_projection(movie_rows: Sequence[Mapping[str, object]], *, projection_id: str, rag_release_id: str, policy: GraphNormalizationPolicy) -> GraphProjectionRecords`.
- Produces: `build_graph_projection(source_root: Path, rag_config_path: Path, normalization_policy_path: Path, recommendation_policy_path: Path, output_dir: Path) -> GraphProjectionResult`.
- Produces: `verify_graph_projection(manifest_path: Path, *, expected_authority_path: Path | None = None) -> VerifiedGraphProjection`.
- `VerifiedGraphProjection` contains the validated manifest plus contained paths for the exact entities, aliases, and edges JSONL artifacts.
- Produces: `bind_graph_projection_authority(manifest_path: Path, authority_path: Path, *, check: bool = False) -> None`.
- Produces CLI commands `build-kg-projection`, `verify-kg-projection`, and `bind-kg-projection-authority`.
- Artifact set is exactly `kg_entities.jsonl`, `kg_aliases.jsonl`, `kg_edges.jsonl`, and `kg_projection_manifest.json`.

- [ ] **Step 1: Write a tiny committed-behavior fixture test using temporary Parquet**

```python
def test_projection_build_is_byte_identical(tmp_path: Path) -> None:
    source_root = write_tiny_authorized_release(tmp_path / "source")
    first = build_graph_projection(
        source_root,
        RAG_CONFIG,
        NORMALIZATION_POLICY,
        RECOMMENDATION_POLICY,
        tmp_path / "first",
    )
    second = build_graph_projection(
        source_root,
        RAG_CONFIG,
        NORMALIZATION_POLICY,
        RECOMMENDATION_POLICY,
        tmp_path / "second",
    )

    assert first.projection_digest == second.projection_digest
    for name in GRAPH_ARTIFACT_NAMES:
        assert (first.output_dir / name).read_bytes() == (second.output_dir / name).read_bytes()
```

The helper writes a minimal `movies.parquet` with PyArrow plus a manifest declaring its exact byte length/SHA. Tests must cover title aliases, multi-role people, an empty split segment, a repeated equivalent token, all four predicates, alias aggregation, occurrence preservation, endpoint validation, and manifest tamper rejection.

- [ ] **Step 2: Confirm RED**

Run: `uv run pytest tests/test_kg_bundle.py -q`

Expected: FAIL because `kg_projection` and `kg_bundle` are absent.

- [ ] **Step 3: Implement pure projection before filesystem packaging**

Use these direct mappings only:

```python
EDGE_RULES = (
    ("director", "DIRECTED_BY", "movie_to_person"),
    ("screenwriter", "WRITTEN_BY", "movie_to_person"),
    ("cast", "ACTED_IN", "person_to_movie"),
    ("genre", "HAS_GENRE", "movie_to_genre"),
)
```

Aggregate occurrences by the approved entity/alias/edge identities, choose canonical person labels by UTF-8 byte order, and then serialize entities, aliases, and edges in their exact sort orders. Never use locale sorting, timestamps, path-dependent data, network calls, or model output.

- [ ] **Step 4: Implement manifest verification and authority binding**

The manifest must bind the three source digests, exact `config/rag_demo.yaml` byte SHA, shared RAG movie-set digest, both policy byte SHAs, exact count object, each JSONL path/size/row-count/SHA, and the domain-separated aggregate digest. Reject extra/missing artifacts, duplicate JSON keys, noncanonical bytes, absolute/traversing paths, self-inclusion, and mismatched final newlines.

`bind_graph_projection_authority()` writes the exact verified manifest bytes into the packaged authority location; `--check` compares only and exits nonzero on drift.

- [ ] **Step 5: Exercise CLI success and tamper failures**

Run: `uv run pytest tests/test_kg_bundle.py -q`

Expected: PASS with byte-identical builds, exact manifest structure, CLI JSON output, path containment, and artifact/policy/source tamper rejection.

- [ ] **Step 6: Run the opt-in real v1.2 acceptance fixture twice**

```powershell
$env:HK_MOVIE_RAG_SOURCE_ROOT='D:\VS_PROJECT\HK_Movie_KnowledgeGraph\RAG'
uv run pytest tests/integration/test_kg_v12_projection.py -q
```

Expected: PASS with exactly:

```text
entities: movie=4658 person=6039 genre=33 total=10730
aliases:  movie=8526 person=6043 genre=33 total=14602
edges:    DIRECTED_BY=5185 WRITTEN_BY=6969 ACTED_IN=13740 HAS_GENRE=6189 total=32083
duplicate screenwriter occurrences preserved=14
person equivalence keys with multiple spellings=4
simplified query collisions=3
movies with one non-empty label present in both title fields=16
```

The same integration test must assert the current immutable source bindings:

```text
release_manifest_file_sha256=e9842db33208e7146d78d86689b13f81fffcf41288d79d84e599b93050445e2c
upstream_source_manifest_sha256=cbd529e126e8b72dccac302d9746fcfa63318caecbcda24548c05e282e13f974
movies_artifact_sha256=c3f7b30a6305dec7f2cabca1397974012fc8dcfba9ee96ec69fe385dfdd3a58f
movies_artifact_size_bytes=297665
rag_config_sha256=81a3794b29a2e02048a7e2b4bd7b3c1e38c82491b60132c0a04c75c4ba1ce0d0
```

It must also prove every current source field/label fits the committed normalization limits; a future value outside them blocks the build rather than truncating data.

The test skips with an explicit reason when `HK_MOVIE_RAG_SOURCE_ROOT` is absent so normal CI does not depend on ignored source data.

- [ ] **Step 7: Build twice from the actual source and bind the exact packaged manifest**

```powershell
uv run hk-movie-rag build-kg-projection --source-root 'D:\VS_PROJECT\HK_Movie_KnowledgeGraph\RAG' --rag-config config/rag_demo.yaml --normalization-policy config/kg/normalization.v1.json --recommendation-policy config/kg/recommendation.v1.json --output artifacts/kg/v1.2-kg1-a
uv run hk-movie-rag build-kg-projection --source-root 'D:\VS_PROJECT\HK_Movie_KnowledgeGraph\RAG' --rag-config config/rag_demo.yaml --normalization-policy config/kg/normalization.v1.json --recommendation-policy config/kg/recommendation.v1.json --output artifacts/kg/v1.2-kg1-b
uv run hk-movie-rag verify-kg-projection artifacts/kg/v1.2-kg1-a/kg_projection_manifest.json
uv run hk-movie-rag bind-kg-projection-authority artifacts/kg/v1.2-kg1-a/kg_projection_manifest.json src/hk_movie_rag/authorities/kg_projection_v1_2_kg1.json
uv run hk-movie-rag bind-kg-projection-authority artifacts/kg/v1.2-kg1-b/kg_projection_manifest.json src/hk_movie_rag/authorities/kg_projection_v1_2_kg1.json --check
```

Expected: both builds and the committed authority are byte-identical; CLI output names `projection_id=v1.2-kg1`, the exact projection digest, and all three row counts. Inspect `git diff` to confirm no generated JSONL entered Git.

- [ ] **Step 8: Commit projection code and the reviewed exact authority**

```powershell
git add src/hk_movie_rag/kg_projection.py src/hk_movie_rag/kg_bundle.py src/hk_movie_rag/authorities/kg_projection_v1_2_kg1.json src/hk_movie_rag/cli.py tests/test_kg_bundle.py tests/integration/test_kg_v12_projection.py .gitattributes
git commit -m "feat: build deterministic graph projection"
```

---

### Task 4: Add immutable graph tables and atomic database activation

**Files:**
- Create: `migrations/0004_knowledge_graph.sql`
- Create: `src/hk_movie_rag/migrations/0004_knowledge_graph.sql`
- Create: `src/hk_movie_rag/kg_db.py`
- Create: `tests/test_kg_db.py`

**Interfaces:**
- Consumes: a `VerifiedGraphProjection`, its three verified JSONL streams, the packaged authority, and the active parent RAG movie rows.
- Produces: `GraphProjectionState(projection_id, rag_release_id, projection_digest, status, expected_counts, actual_counts)`.
- Produces: `GraphProjectionStats(entity_count, alias_count, edge_count, predicate_counts)`.
- Produces: `GraphRepository.from_pool(pool: ConnectionPool) -> GraphRepository`.
- Produces write methods `ensure_schema()`, `begin_projection(projection, run_id)`, `load_and_activate_projection(projection, run_id)`, `finish_projection_run(run_id, stats)`, and `fail_projection_ingestion(projection_id, rag_release_id, run_id, error_code)`.
- Produces readiness method `assert_projection_ready(projection_id: str, projection_digest: str, rag_release_id: str) -> GraphProjectionState`.
- Invariant: graph persistence lives outside the existing 2,400-line `RagRepository`; the existing repository is modified only through Task 1's shared serializer.

- [ ] **Step 1: Write migration parity and lifecycle tests first**

```python
def test_root_and_packaged_graph_migration_are_identical() -> None:
    assert ROOT_MIGRATION.read_bytes() == PACKAGED_MIGRATION.read_bytes()


def test_active_projection_conflict_is_recorded_without_mutation(
    graph_repository: GraphRepository,
    active_projection: VerifiedGraphProjection,
    conflicting_projection: VerifiedGraphProjection,
) -> None:
    before = graph_repository.assert_projection_ready(
        active_projection.projection_id,
        active_projection.projection_digest,
        active_projection.rag_release_id,
    )

    with pytest.raises(GraphConflictError):
        graph_repository.load_and_activate_projection(conflicting_projection, "run-conflict")

    assert graph_repository.assert_projection_ready(
        before.projection_id, before.projection_digest, before.rag_release_id
    ) == before
```

Add DB-free scripted-connection tests for migration ordering, `SET LOCAL` timeouts, full-row replay comparisons, failed first load cleanup, failed-ID non-retry, active identical replay, missing parent release, wrong movie payload digest, orphan endpoints, source movie mismatch, counter mismatch, and redacted run failure.

- [ ] **Step 2: Confirm RED**

Run: `uv run pytest tests/test_kg_db.py -q`

Expected: FAIL because the migration and `GraphRepository` are absent.

- [ ] **Step 3: Add the exact additive schema**

The mirrored migration must create these four tables and extend `ingestion_runs` without changing existing RAG rows:

```sql
ALTER TABLE ingestion_runs
    ADD COLUMN run_kind text NOT NULL DEFAULT 'rag_release'
        CHECK (run_kind IN ('rag_release', 'kg_projection')),
    ADD COLUMN projection_id text;

CREATE TABLE kg_projections (
    projection_id text PRIMARY KEY,
    rag_release_id text NOT NULL REFERENCES rag_releases(release_id) ON DELETE RESTRICT,
    schema_version text NOT NULL,
    release_manifest_file_sha256 text NOT NULL CHECK (release_manifest_file_sha256 ~ '^[0-9a-f]{64}$'),
    upstream_source_manifest_sha256 text NOT NULL CHECK (upstream_source_manifest_sha256 ~ '^[0-9a-f]{64}$'),
    movies_artifact_sha256 text NOT NULL CHECK (movies_artifact_sha256 ~ '^[0-9a-f]{64}$'),
    rag_config_sha256 text NOT NULL CHECK (rag_config_sha256 ~ '^[0-9a-f]{64}$'),
    rag_movie_set_sha256 text NOT NULL CHECK (rag_movie_set_sha256 ~ '^[0-9a-f]{64}$'),
    normalization_policy_sha256 text NOT NULL CHECK (normalization_policy_sha256 ~ '^[0-9a-f]{64}$'),
    recommendation_policy_sha256 text NOT NULL CHECK (recommendation_policy_sha256 ~ '^[0-9a-f]{64}$'),
    projection_digest text NOT NULL UNIQUE CHECK (projection_digest ~ '^[0-9a-f]{64}$'),
    expected_counts jsonb NOT NULL,
    actual_counts jsonb,
    status text NOT NULL CHECK (status IN ('loading', 'active', 'failed')),
    created_at timestamptz NOT NULL DEFAULT now(),
    activated_at timestamptz,
    UNIQUE (projection_id, rag_release_id)
);
```

`kg_entities`, `kg_aliases`, and `kg_edges` must implement every column, composite key, predicate direction, identity-status/hash pairing, occurrence/count check, source-movie composite FK, and same-projection endpoint FK in the approved design. Add exactly the two alias indexes and two forward/reverse edge indexes specified there.

Add triggers that:

- reject every update/delete on `kg_entities`, `kg_aliases`, and `kg_edges`;
- allow `kg_projections` to change only loading counters and one `loading -> active|failed` transition;
- reject update/delete of an active or failed projection;
- reject a retry of a failed projection ID.

The `ingestion_runs(projection_id, release_id)` composite FK may be nullable for legacy RAG runs but must be non-null when `run_kind='kg_projection'`.

- [ ] **Step 4: Implement staged activation with independent re-derivation**

`load_and_activate_projection()` must run in one transaction with:

```sql
SELECT set_config('statement_timeout', '600000ms', true);
SELECT pg_advisory_xact_lock(hashtextextended(%(projection_id)s, 0));
```

Then it must:

1. lock the exact `rag_releases` and `kg_projections` rows;
2. require the parent release to be active;
3. recompute `rag_movie_set_sha256` from sorted database `(movie_id, payload_sha256)` pairs;
4. parse the locked `movies.payload` rows with the shared serializer and re-run `derive_graph_projection()`;
5. load downloaded and re-derived records into separate temporary tables;
6. compare both directions with `EXCEPT`, including arrays/JSON/digests/counts;
7. insert only byte/field-identical missing permanent rows, or verify every stored field on replay;
8. prove endpoints, source movies, counts, evidence digests, policy/authority bindings, and absence of path-affecting duplicates;
9. set exact `actual_counts`, transition to `active`, and complete the ingestion run.

Any exception rolls back this transaction. `fail_projection_ingestion()` uses a separate short transaction to mark a still-loading first projection `failed` and the run `failed`; it never changes an already-active projection.

- [ ] **Step 5: Implement fail-closed readiness**

`assert_projection_ready()` must select exactly one row by projection ID, digest, parent release, and `status='active'`; compare actual/expected counts; and recheck the active parent RAG state. It must not auto-select latest or return a partially populated state.

- [ ] **Step 6: Run focused migration/repository tests**

Run: `uv run pytest tests/test_kg_db.py -q`

Expected: PASS for exact SQL, mirrored bytes, activation lifecycle, immutable replay, count/source/digest failures, and readiness binding.

- [ ] **Step 7: Commit the database slice**

```powershell
git add migrations/0004_knowledge_graph.sql src/hk_movie_rag/migrations/0004_knowledge_graph.sql src/hk_movie_rag/kg_db.py tests/test_kg_db.py
git commit -m "feat: add immutable graph projection storage"
```

---

### Task 5: Add verified local/GCS projection ingestion and CLI orchestration

**Files:**
- Create: `src/hk_movie_rag/kg_ingest.py`
- Create: `tests/test_kg_ingest.py`
- Modify: `src/hk_movie_rag/cli.py`

**Interfaces:**
- Consumes: a local manifest path or a GCS URI in the configured private release bucket whose object name is exactly `graph/v1.2-kg1/kg_projection_manifest.json`.
- Produces: `verified_graph_projection_source(source: str | Path, *, project_id: str, storage_client: StorageClient | None = None) -> ContextManager[VerifiedGraphProjection]`.
- Produces: `ingest_graph_projection(projection: VerifiedGraphProjection, repository: GraphRepository, *, packaged_authority: Path, run_id: str) -> GraphIngestionSummary`.
- Produces CLI command `ingest-kg-projection MANIFEST --require-projection-id v1.2-kg1 --require-projection-sha256`, where the final option value is loaded from the verified packaged authority before orchestration.
- Invariant: a remote source downloads only the three relative artifact names declared by the verified manifest and compares the manifest bytes to the packaged authority before any database write.

- [ ] **Step 1: Write fake-storage and orchestration failures**

```python
def test_remote_source_downloads_only_manifest_declared_siblings(
    fake_storage: FakeStorage,
) -> None:
    with verified_graph_projection_source(
        "gs://release/graph/v1.2-kg1/kg_projection_manifest.json",
        project_id="motionexpaiweb",
        storage_client=fake_storage,
    ) as bundle:
        assert bundle.manifest.projection_id == "v1.2-kg1"

    assert fake_storage.requested == (
        "graph/v1.2-kg1/kg_projection_manifest.json",
        "graph/v1.2-kg1/kg_aliases.jsonl",
        "graph/v1.2-kg1/kg_edges.jsonl",
        "graph/v1.2-kg1/kg_entities.jsonl",
    )
```

Also test wrong bucket/prefix, encoded traversal, extra artifact, local symlink escape, object byte/hash mismatch, manifest/authority drift, required ID/digest mismatch, first-load failure recording, active identical replay, active conflict preservation, no Vertex calls, and redacted CLI stderr/JSON.

- [ ] **Step 2: Confirm RED**

Run: `uv run pytest tests/test_kg_ingest.py -q`

Expected: FAIL because graph ingestion is absent.

- [ ] **Step 3: Implement verified source containment**

For GCS, fetch the manifest first, require its object name to end at the exact projection prefix, parse and verify it, then download only manifest-declared sibling objects into a private temporary directory. Reject generation/size/hash changes between metadata inspection and download. For local paths, resolve strictly and require every artifact to remain inside the manifest directory.

- [ ] **Step 4: Implement conflict-intolerant orchestration**

```python
def ingest_graph_projection(
    projection: VerifiedGraphProjection,
    repository: GraphRepository,
    *,
    packaged_authority: Path,
    run_id: str,
) -> GraphIngestionSummary:
    verify_graph_projection(
        projection.manifest_path,
        expected_authority_path=packaged_authority,
    )
    repository.ensure_schema()
    repository.begin_projection(projection.manifest, run_id)
    try:
        stats = repository.load_and_activate_projection(projection, run_id)
    except Exception as exc:
        repository.fail_projection_ingestion(
            projection.manifest.projection_id,
            projection.manifest.rag_release_id,
            run_id,
            graph_error_code(exc),
        )
        raise
    return GraphIngestionSummary.from_stats(projection.manifest, run_id, stats)
```

Never store raw exception strings, credentials, DSNs, object URLs, or GCS paths in `error_summary`.

- [ ] **Step 5: Wire the CLI through existing database/storage settings**

The command must print one canonical JSON summary containing projection ID/digest, run ID, and counts. It must verify `--require-*` before `begin_projection()`. Reuse the existing Cloud SQL connection configuration and storage credentials; do not create a second Job-specific credential path.

- [ ] **Step 6: Run focused tests**

Run: `uv run pytest tests/test_kg_ingest.py tests/test_kg_db.py -q`

Expected: PASS with zero network calls in local tests and no embedding/generation calls in any ingestion path.

- [ ] **Step 7: Commit ingestion**

```powershell
git add src/hk_movie_rag/kg_ingest.py src/hk_movie_rag/cli.py tests/test_kg_ingest.py
git commit -m "feat: ingest verified graph projections"
```

---

### Task 6: Implement signed cursors, search, entity detail, and one-hop expansion

**Files:**
- Create: `src/hk_movie_rag/kg_cursor.py`
- Create: `src/hk_movie_rag/kg_query.py`
- Create: `tests/test_kg_cursor.py`
- Create: `tests/test_kg_query.py`
- Modify: `src/hk_movie_rag/kg_db.py`
- Modify: `tests/test_kg_db.py`

**Interfaces:**
- Consumes: a validated `GraphBinding(projection_id, projection_digest, rag_release_id)`, `GraphRepository`, existing RAG poster repository, and a domain-derived cursor key.
- Produces: `derive_graph_cursor_key(access_key: str) -> bytes` and `GraphCursorCodec(key: bytes, ttl_seconds: int = 600, clock: Callable[[], float] = time.time)`.
- Produces: `GraphQueryService.search(request: GraphSearchRequest) -> GraphSearchResponse`.
- Produces: `GraphQueryService.entity(entity_id: str) -> GraphEntityDetailResponse`.
- Produces: `GraphQueryService.neighborhood(request: GraphNeighborhoodRequest) -> GraphNeighborhoodResponse`.
- Produces repository reads `search_entities()`, `get_entity()`, `load_path()`, and `neighborhood()` returning stable `next_after` keys rather than unsigned client cursors.

- [ ] **Step 1: Write cursor misuse tests**

```python
def test_cursor_rejects_cross_projection_and_expiry() -> None:
    codec = GraphCursorCodec(
        derive_graph_cursor_key("local-only-code"),
        ttl_seconds=600,
        clock=lambda: 1_000.0,
    )
    token = codec.encode(
        endpoint="search",
        projection_digest="a" * 64,
        query_fingerprint="b" * 64,
        page_size=20,
        last_sort_key=("movie", "醉拳", "movie:1978_ZQ_001"),
    )

    with pytest.raises(GraphCursorError, match="projection"):
        codec.decode(token, endpoint="search", projection_digest="c" * 64,
                     query_fingerprint="b" * 64, page_size=20)
```

Cover bit tampering, invalid base64url, padding/ASCII/1,024-character bounds, wrong endpoint, wrong filters, wrong page size, wrong query, future-issued token, and expiry at 601 seconds. Derive the key as HMAC-SHA256 with the access key as secret and message `hk-movie-kg/cursor-key/v1`; never expose it in config or logs.

- [ ] **Step 2: Write service/repository RED tests**

Add cases for exact source-label ranking, unique simplified key, ambiguous simplified key, prefix, contained label, duplicate movie title, empty result, continuation, invalid entity grammar, movie/person/genre detail, private poster proxy/null, path discontinuity, non-terminal expansion, one-layer depth, parallel-edge grouping, adjacent-group pagination, 30-group/120-edge budgets, UI-safe response fields, and SQL-like text passed only as a parameter.

Run: `uv run pytest tests/test_kg_cursor.py tests/test_kg_query.py tests/test_kg_db.py -q`

Expected: FAIL because cursor/query reads are absent.

- [ ] **Step 3: Implement canonical cursor payloads**

The signed JSON payload has exactly `version`, `endpoint`, `projection_digest`, `query_fingerprint`, `page_size`, `last_sort_key`, `issued_at`, and `expires_at`. Encode canonical JSON plus a 32-byte HMAC tag with URL-safe base64 and no padding. Reject unknown keys and require `expires_at - issued_at <= 600`.

- [ ] **Step 4: Implement parameterized repository reads**

Every method opens a read transaction and runs:

```sql
SELECT set_config('statement_timeout', '2000ms', true);
```

Search ranking is exact source label, unique derived query key, prefix, then contained label, followed by entity type, canonical-label UTF-8 byte order, and entity ID. Search queries both exact/simplified keys but never fuzzy-resolve or auto-select a collision.

Neighborhood must validate the complete simple path, origin, terminal expansion node, and depth before querying one edge layer. Group all parallel edges for one adjacent entity, sort a group's edges by `(predicate, edge_id)`, and paginate by complete adjacent group. Apply the approved movie/date, person, and genre group order and return at most 30 groups/120 edges.

- [ ] **Step 5: Implement service validation and poster authority reuse**

`GraphQueryService` signs repository `next_after` values only after binding them to normalized request filters. It revalidates every returned entity/edge against the active binding. For validated movie IDs only, batch-call `RagRepository.get_approved_poster_assets()` and expose `/api/posters/{movie_id}` or `None`; do not copy poster predicates into graph SQL.

- [ ] **Step 6: Prove no AI calls are reachable**

Inject embedding/generation spies and assert search, entity, and neighborhood each make zero calls. Invalid repository rows or poster object URIs must fail the request rather than leak a raw path.

- [ ] **Step 7: Run focused tests and commit**

Run: `uv run pytest tests/test_kg_cursor.py tests/test_kg_query.py tests/test_kg_db.py -q`

Expected: PASS for search, detail, expansion, cursor, timeout, path validation, and poster boundaries.

```powershell
git add src/hk_movie_rag/kg_cursor.py src/hk_movie_rag/kg_query.py src/hk_movie_rag/kg_db.py tests/test_kg_cursor.py tests/test_kg_query.py tests/test_kg_db.py
git commit -m "feat: query bounded graph neighborhoods"
```

---

### Task 7: Add shortest paths and transparent related-movie ranking

**Files:**
- Modify: `src/hk_movie_rag/kg_query.py`
- Modify: `src/hk_movie_rag/kg_db.py`
- Modify: `tests/test_kg_query.py`
- Modify: `tests/test_kg_db.py`
- Create: `tests/integration/test_kg_postgres.py`

**Interfaces:**
- Produces: `GraphQueryService.paths(request: GraphPathsRequest) -> GraphPathsResponse`.
- Produces: `GraphQueryService.recommendations(request: GraphRecommendationRequest) -> GraphRecommendationResponse`.
- Produces repository reads `shortest_paths()` and `recommend_movies()` with fixed SQL and server-computed evidence edges.
- Consumes: existing `QueryEmbeddingClient` only when stripped preference text is non-empty.
- Invariant: repository results carry exact anchor/candidate edge pairs; Gemini never authors a reason, score, title, citation, or order.
- `GraphPathsResponse` contains every returned path's validated entity payloads and ordered edge payloads, plus `incomplete`; it never returns only opaque IDs that the UI would have to guess how to label.

- [ ] **Step 1: Write shortest-path RED tests**

Test two canonical endpoints, identical endpoint rejection, `max_depth=1..3`, `max_paths=1..20`, shortest simple paths only, cycle prevention, edge-ID sequence ordering, multiple shortest paths, `incomplete=true` truncation, no-path response, fourth-edge rejection, malformed/cross-projection IDs, fixed predicate allowlist, and controlled timeout.

- [ ] **Step 2: Write exact recommendation score tests**

```python
def test_same_role_score_and_reason_order_are_exact(query_service: GraphQueryService) -> None:
    response = query_service.recommendations(
        GraphRecommendationRequest(anchor_entity_id="movie:anchor", limit=8)
    )

    winner = response.items[0]
    assert winner.graph_score == 5 + 4 + 2 * 3 + 1 * 2
    assert [reason.component for reason in winner.reasons] == [
        "shared_director", "shared_screenwriter", "shared_cast", "shared_genre"
    ]
    assert all(reason.anchor_edge_id and reason.candidate_edge_id for reason in winner.reasons)
```

Add cases for component caps/additional counts, UTF-8 label/entity tie order, cross-role-only exclusion, required person/genre AND semantics, explicit path not becoming an implicit filter, year/tier bounds, anchor/path/excluded movie removal, limit 1..8, exclusion max 32, required IDs max 8, and `graph_score > 0`.

- [ ] **Step 3: Confirm RED**

Run: `uv run pytest tests/test_kg_query.py tests/test_kg_db.py -q`

Expected: FAIL for absent path/recommendation methods.

- [ ] **Step 4: Implement fixed recursive and scoring SQL**

Shortest paths use a recursive CTE with a visited-entity array, fixed direct predicates, depth parameter capped at three, and deterministic final order `(edge_count, edge_id_sequence)`. Query one extra path to set `incomplete` accurately.

Recommendation SQL/materialization must implement exactly:

```text
5 * has_shared_director
+ 4 * has_shared_screenwriter
+ 2 * min(shared_cast_count, 3)
+ 1 * min(shared_genre_count, 2)
```

Same-role matching is mandatory. Select contributing entities in component order, then canonical-label UTF-8/entity-ID order, carrying the exact two direct edge IDs per reason. Return extra shared counts but never extra score/citations beyond caps.

- [ ] **Step 5: Add optional semantic tie-breaking without candidate broadening**

For empty/whitespace preference, pass `semantic_embedding=None` and assert zero embedding calls. Otherwise validate at most 500 code points, call `embed_query()` once, and compute cosine distance only against each already-qualified candidate's single metadata passage. Final sort is graph score descending, valid distance ascending within the equal-score group, then movie ID ascending. PDF passages never influence this distance.

- [ ] **Step 6: Add disposable PostgreSQL 16 query-plan coverage**

`tests/integration/test_kg_postgres.py` must skip unless `HK_MOVIE_RAG_TEST_DATABASE_URL` is set. Against a disposable PostgreSQL 16 + pgvector database, apply migrations 0001 through 0004, load/analyze a small fixture, and run `EXPLAIN (FORMAT JSON, COSTS OFF)` for search, entity, path reload, neighborhood, shortest path, and recommendations. Assert the exact projection predicates/index conditions appear and that no query lacks a projection filter or uses an unbounded recursive depth.

After setting `HK_MOVIE_RAG_TEST_DATABASE_URL` to an explicitly disposable PostgreSQL 16 + pgvector database, run:

```powershell
if (-not $env:HK_MOVIE_RAG_TEST_DATABASE_URL) { throw 'disposable KG test database URL is required' }
uv run pytest tests/integration/test_kg_postgres.py -q
```

Expected: PASS against an explicitly disposable database. Never point this variable at the production instance.

- [ ] **Step 7: Run focused unit tests and commit**

Run: `uv run pytest tests/test_kg_query.py tests/test_kg_db.py -q`

Expected: PASS; graph-only recommendations show zero embedding/Gemini calls, semantic preference shows exactly one embedding/zero Gemini calls.

```powershell
git add src/hk_movie_rag/kg_query.py src/hk_movie_rag/kg_db.py tests/test_kg_query.py tests/test_kg_db.py tests/integration/test_kg_postgres.py
git commit -m "feat: add graph paths and related movies"
```

---

### Task 8: Add graph evidence and single-turn grounded relationship explanation

**Files:**
- Create: `src/hk_movie_rag/kg_grounding.py`
- Create: `tests/test_kg_grounding.py`
- Modify: `src/hk_movie_rag/kg_query.py`
- Modify: `src/hk_movie_rag/vertex_clients.py`
- Modify: `tests/test_kg_query.py`
- Modify: `tests/test_vertex_clients.py`

**Interfaces:**
- Produces: `GraphEdgeEvidence(evidence_id, edge_id, predicate, subject_entity_id, subject_label, object_entity_id, object_label, source_movie_id, source_field, source_value_sha256)`.
- Produces: `GraphGenerationClient.generate_graph_explanation(question: str, supplied_evidence: tuple[SuppliedEvidence, ...], required_graph_citation_ids: tuple[str, ...]) -> GeneratedGraphAnswer` protocol.
- Produces: `validate_graph_generated_answer(answer: GeneratedGraphAnswer, supplied_evidence: tuple[SuppliedEvidence, ...], required_graph_citation_ids: tuple[str, ...]) -> GraphExplanation`.
- Produces: `GraphExplanationService.explain(question: str, path_entity_ids: tuple[str, ...], path_edge_ids: tuple[str, ...]) -> GraphExplanation`.
- `SuppliedEvidence` is the discriminated union of `GraphEdgeEvidence` and the existing immutable metadata/PDF evidence representation.
- Invariant: keep the existing `GenerationClient.generate_grounded()` and `/api/chat` exact-citation behavior unchanged; add a separate Vertex method for graph explanation.

- [ ] **Step 1: Write citation-subsequence RED tests**

```python
@pytest.mark.parametrize(
    "citation_ids",
    [
        ("graph:e2", "graph:e1"),
        ("graph:e1",),
        ("graph:e1", "graph:e1", "graph:e2"),
        ("graph:e1", "invented:x", "graph:e2"),
    ],
)
def test_required_graph_citations_reject_reordered_missing_duplicate_or_foreign(
    citation_ids: tuple[str, ...],
) -> None:
    with pytest.raises(GraphGroundingError):
        validate_graph_generated_answer(
            GeneratedGraphAnswer("answer", citation_ids),
            supplied_evidence=SUPPLIED_EVIDENCE,
            required_graph_citation_ids=("graph:e1", "graph:e2"),
        )
```

Add a passing case where optional `movie_metadata`/`pdf_page` citations interleave while the graph subsequence stays exact. Validate every returned citation against the supplied set, and require every mandatory graph ID exactly once in both text markers and structured `citation_ids`.

- [ ] **Step 2: Write path reload/provider boundary tests**

Test 2..4 entities, 1..3 edges, discontinuous/tampered path, no conversation history parameter, server-created edge bodies only, optional RAG search restricted to path movie IDs, metadata/PDF passage budgets, no optional evidence, unsupported qualitative/deep claim handling, Vertex timeout/invalid JSON, redacted errors, and preservation of graph/path state on failure.

Run: `uv run pytest tests/test_kg_grounding.py tests/test_kg_query.py tests/test_vertex_clients.py -q`

Expected: FAIL because graph grounding is absent.

- [ ] **Step 3: Implement immutable graph evidence construction**

Reload the selected path from `GraphRepository`; verify its exact entity/edge sequence and binding; then map each edge to `evidence_id=f"graph:{edge_id}"`. The caller never supplies evidence bodies or IDs. Optional retrieval may embed the current question and call existing RAG search only with validated movie IDs from the path; do not aggregate or invent graph facts from passages.

- [ ] **Step 4: Add a separate graph mode to the Vertex adapter**

`VertexGenerationClient.generate_graph_explanation()` must retain the existing 20-second timeout, retry, structured JSON, and redaction boundaries but use a graph-specific prompt. Serialize only `question`, `supplied_evidence`, and `required_graph_citation_ids`; omit conversation context entirely. The prompt must state that unsupported qualitative claims are unavailable and that citation order/uniqueness is mandatory.

- [ ] **Step 5: Reject the whole answer on any grounding defect**

Do not return partial generated prose. The explanation response contains validated Markdown, ordered citation objects, and the same graph binding. Provider/validation failure raises a controlled graph-explanation error while leaving the caller's selected path untouched.

- [ ] **Step 6: Run focused tests and commit**

Run: `uv run pytest tests/test_kg_grounding.py tests/test_kg_query.py tests/test_vertex_clients.py -q`

Expected: PASS for path reload, supplied-body boundary, exact graph citation subsequence, optional citation interleave, single-turn prompt, and provider failure.

```powershell
git add src/hk_movie_rag/kg_grounding.py src/hk_movie_rag/kg_query.py src/hk_movie_rag/vertex_clients.py tests/test_kg_grounding.py tests/test_kg_query.py tests/test_vertex_clients.py
git commit -m "feat: ground relationship explanations"
```

---

### Task 9: Bind runtime configuration and expose six authenticated graph routes

**Files:**
- Create: `src/hk_movie_rag/kg_runtime.py`
- Create: `src/hk_movie_rag/kg_api.py`
- Create: `src/hk_movie_rag/kg_demo.py`
- Create: `tests/test_kg_runtime.py`
- Create: `tests/test_kg_api.py`
- Modify: `src/hk_movie_rag/demo_api.py`
- Modify: `tests/test_demo_api.py`
- Modify: `.env.example`

**Interfaces:**
- Produces: `GraphRuntimeBinding(projection_id, rag_release_id, projection_digest, manifest_file_sha256, graph_asset_sha256)`.
- Produces: `load_packaged_graph_binding() -> GraphRuntimeBinding`, with canonical-byte and duplicate-key validation.
- Extends `DemoSettings` with `graph_enabled: bool = False` and `graph_projection_sha256: str | None = None`; projection ID is never an environment variable.
- Produces strict Pydantic v2 request/response models with `extra="forbid"`.
- Produces: `create_graph_router(*, require_session, get_graph_service, get_graph_binding) -> APIRouter`.
- Registers exactly six routes: `GET /api/graph/search`, `GET /api/graph/entities/{entity_id}`, and POST `/api/graph/neighborhood`, `/api/graph/paths`, `/api/graph/recommendations`, `/api/graph/explain`.
- Produces a deterministic offline graph fixture service used only by `create_offline_app()` when the existing offline/K_SERVICE guard passes.

- [ ] **Step 1: Write runtime flag and package-authority RED tests**

```python
@pytest.mark.parametrize("value", ["1", "TRUE", "yes", "False ", ""])
def test_graph_flag_rejects_every_non_literal_boolean(value: str) -> None:
    env = valid_demo_env(RAG_GRAPH_ENABLED=value)
    with pytest.raises(DemoConfigurationError, match="graph configuration"):
        DemoSettings.from_env(env)


def test_disabled_graph_needs_no_projection_digest() -> None:
    settings = DemoSettings.from_env(valid_demo_env(RAG_GRAPH_ENABLED="false"))
    assert settings.graph_enabled is False
    assert settings.graph_projection_sha256 is None
```

Add tests for default false, enabled missing digest, uppercase/non-hex digest, packaged manifest tamper, duplicate key, noncanonical bytes, environment/authority mismatch, parent RAG mismatch, computed manifest-file SHA, asset-authority binding, and fresh-wheel resource extraction.

- [ ] **Step 2: Write route contract RED tests before registering the router**

For all six routes, test authentication occurs first; disabled authenticated access is `404` without binding; malformed request is `422`; missing entity is `404`; projection/readiness/timeout/grounding failure is safe `503`; every graph error contains a 32-character lowercase-hex `request_id`; and success contains exact `projection_id`, `projection_digest`, and `rag_release_id`.

Also cover:

- search `q` 1..128, entity type subset, `limit` 1..20, ASCII cursor <=1,024, and unknown query-key rejection;
- entity ID ASCII grammar/256-byte limit and no GCS path acceptance;
- neighborhood path lengths, four-predicate subset, page size 1..30, and strict JSON;
- paths depth 1..3 and count 1..20;
- recommendations movie anchor, optional verified path, required IDs <=8, inclusive year order, tier subset, preference <=500, limit 1..8, exclusions <=32;
- explain question 1..1,000, entities 2..4, edges 1..3, and rejection of caller edge/evidence/citation fields;
- same-origin/session behavior and raw GCS-path absence in every response.

Run: `uv run pytest tests/test_kg_runtime.py tests/test_kg_api.py tests/test_demo_api.py -q`

Expected: FAIL because runtime binding/router fields are absent.

- [ ] **Step 3: Implement strict packaged binding and startup behavior**

When `RAG_GRAPH_ENABLED=false`, do not read the packaged graph authority, require a graph digest, open a graph repository, or instantiate Vertex for graph use.

When true:

1. require `RAG_GRAPH_PROJECTION_SHA256` as 64 lowercase hex;
2. load the exact packaged projection manifest and asset authority;
3. require the environment value to equal `projection_digest` in that manifest;
4. require manifest RAG release `v1.2-demo` and projection ID `v1.2-kg1`;
5. call `GraphRepository.assert_projection_ready()` with all three binding values;
6. construct `GraphQueryService` only after readiness succeeds.

Environment/package syntax mismatch is a `DemoConfigurationError` and blocks the candidate from starting. Database readiness failure must not invent a fallback projection; graph/config readiness returns controlled `503`, while existing RAG data remains untouched.

- [ ] **Step 4: Implement strict API models and error mapping**

```python
class GraphPathSelection(BaseModel):
    model_config = ConfigDict(extra="forbid", strict=True)
    entity_ids: tuple[str, ...]
    edge_ids: tuple[str, ...]


class GraphNeighborhoodRequest(BaseModel):
    model_config = ConfigDict(extra="forbid", strict=True)
    origin_entity_id: str
    expanded_entity_id: str
    path: GraphPathSelection
    predicates: tuple[GraphPredicate, ...]
    page_size: int = Field(ge=1, le=30)
    cursor: str | None = None
```

Perform cross-field checks after scalar bounds: path cardinality/continuity is ultimately reloaded by the service, year minimum may not exceed maximum, entity type/predicate lists contain no duplicates, and only `application/json` POST bodies are accepted.

Graph errors are built as `{"detail": public_detail, "request_id": request_id}` plus binding fields only after binding validation; generate `request_id` with `secrets.token_hex(16)`. Tests require `public_detail` from a fixed allowlist and `request_id` as 32 lowercase hexadecimal characters. Never include exception text, SQL, credentials, model payload, GCS URI, or filesystem path.

- [ ] **Step 5: Preserve legacy routes and extend authenticated config**

`/api/config` always includes `graph_enabled`. When false it omits graph binding keys. When true and ready, tests must assert the dynamic values directly against the validated binding:

```python
assert payload["graph_asset_sha256"] == binding.graph_asset_sha256
assert payload["graph_enabled"] is True
assert payload["graph_projection_id"] == "v1.2-kg1"
assert payload["graph_projection_sha256"] == binding.projection_digest
```

These values come from already-validated packaged authorities, never from request input. Keep every existing config identity/count check. `/api/chat`, session cookies, logout, and poster proxy retain their exact contracts.

- [ ] **Step 6: Add the offline fixture behind the existing safety guard**

`kg_demo.py` must expose an in-memory implementation of the same query-service protocol with governed-looking but explicitly fixed test records for `醉拳`, `袁和平`, people, movies, and genres. It must support search, parallel relationships, a three-edge breadcrumb, deterministic recommendations, poster available/unavailable states, and one valid graph explanation. It is reachable only when `RAG_DEMO_OFFLINE=1` and `K_SERVICE` is absent; production app creation must never import/use its records as authority.

- [ ] **Step 7: Run focused routes/runtime regressions**

Run: `uv run pytest tests/test_kg_runtime.py tests/test_kg_api.py tests/test_demo_api.py -q`

Expected: PASS for feature false/true behavior, six strict routes, auth-first ordering, safe errors/request IDs, package binding, existing chat/session/poster compatibility, and offline safety guard.

- [ ] **Step 8: Commit API/runtime binding**

```powershell
git add src/hk_movie_rag/kg_runtime.py src/hk_movie_rag/kg_api.py src/hk_movie_rag/kg_demo.py src/hk_movie_rag/demo_api.py tests/test_kg_runtime.py tests/test_kg_api.py tests/test_demo_api.py .env.example
git commit -m "feat: expose authenticated graph api"
```

---

### Task 10: Establish the local Cytoscape supply-chain and static-module boundary

**Files:**
- Create: `package.json`
- Create: `package-lock.json`
- Create: `scripts/vendor-cytoscape.mjs`
- Create: `src/hk_movie_rag/static/cytoscape-3.34.0.esm.min.mjs`
- Create: `src/hk_movie_rag/static/cytoscape-3.34.0.LICENSE.txt`
- Create: `src/hk_movie_rag/authorities/graph_atlas_assets_v1.json`
- Create: `tests/static_graph_asset_contract.mjs`
- Create: `tests/test_graph_asset_package.py`
- Modify: `pyproject.toml`
- Modify: `.gitignore`
- Modify: `.gitattributes`
- Modify: `src/hk_movie_rag/demo_api.py`
- Modify: `tests/test_demo_api.py`

**Interfaces:**
- Consumes: exact npm package `cytoscape@3.34.0` from `package-lock.json`.
- Produces: `npm run vendor:sync`, `npm run vendor:check`, and `npm run test:static`.
- Produces: a committed ESM asset copied exactly from `node_modules/cytoscape/dist/cytoscape.esm.min.mjs`, its upstream MIT license, and canonical asset authority containing path/version/license/size/SHA-256.
- Produces: an explicit same-origin static allowlist; it never becomes a generic filesystem path route.

- [ ] **Step 1: Add supply-chain tests before installing**

Tests must require:

```text
package version: 3.34.0
npm package integrity: sha512-62rNSrioXw93uliKFBwjukeQyeWwH2PqDrTac31r2P6464u3AUvTk0xS4LVvT251g7IgkFunrI48ZEZGjywSOg==
npm package shasum: 5fbe2eb1cf76b070a8ecd5647c35f65aa097c9c6
vendored ESM size: 433927 bytes
license: MIT
runtime source: same-origin only
```

The asset SHA test computes SHA-256 from committed bytes and compares it with the concrete 64-hex value emitted into `graph_atlas_assets_v1.json`. Also require no `http://`, `https://`, `unpkg`, `jsdelivr`, or CDN reference in HTML/JS/CSS.

- [ ] **Step 2: Confirm RED**

Run: `node --test tests/static_graph_asset_contract.mjs`

Expected: FAIL because `package.json`, vendored bytes, license, and authority do not exist.

Run: `uv run pytest tests/test_graph_asset_package.py tests/test_demo_api.py -q`

Expected: FAIL because the wheel/static route does not contain the new asset types.

- [ ] **Step 3: Create the exact Node contract and lockfile**

```json
{
  "name":"hk-movie-rag-graph-atlas",
  "private":true,
  "type":"module",
  "scripts":{
    "test:static":"node --test tests/static_app_ui_contract.mjs tests/static_graph_asset_contract.mjs tests/graph_state_contract.mjs tests/graph_atlas_ui_contract.mjs",
    "vendor:check":"node scripts/vendor-cytoscape.mjs --check",
    "vendor:sync":"node scripts/vendor-cytoscape.mjs --write"
  },
  "dependencies":{"cytoscape":"3.34.0"}
}
```

Run `npm install --package-lock-only`, inspect that Cytoscape resolves exactly once to 3.34.0 with the required integrity, then run `npm ci`. Add only `node_modules/` to `.gitignore`; never ignore the lockfile or vendored asset.

- [ ] **Step 4: Implement deterministic vendor sync/check**

`scripts/vendor-cytoscape.mjs` must read the locked package asset/license, verify package version/integrity and 433,927-byte ESM size, then either:

- `--write`: atomically write the exact ESM, license, and canonical one-line asset authority with final newline;
- `--check`: compare all three committed files byte for byte and exit nonzero on drift.

The script makes no network call. `npm ci` is the only dependency-fetch step.

- [ ] **Step 5: Package and serve only allowlisted static files**

Extend Hatch includes with `src/hk_movie_rag/static/*.mjs` and `*.txt`. Add an explicit mapping in `demo_api.py` for every Graph Atlas module/asset with correct JavaScript/text MIME type. Unknown names, nested paths, encoded slashes, and traversal return `404`. Retain CSP `script-src 'self'` and do not add `unsafe-inline` or a CDN host.

- [ ] **Step 6: Verify Node, HTTP, and wheel parity**

```powershell
npm ci
npm run vendor:check
node --test tests/static_graph_asset_contract.mjs
uv run pytest tests/test_graph_asset_package.py tests/test_demo_api.py -q
uv build
```

Expected: PASS; a fresh wheel contains the exact ESM/license/authority bytes, same-origin requests return correct MIME/CSP, and no network URL appears in the UI source.

- [ ] **Step 7: Commit the reviewed vendored dependency**

```powershell
git add package.json package-lock.json scripts/vendor-cytoscape.mjs src/hk_movie_rag/static/cytoscape-3.34.0.esm.min.mjs src/hk_movie_rag/static/cytoscape-3.34.0.LICENSE.txt src/hk_movie_rag/authorities/graph_atlas_assets_v1.json tests/static_graph_asset_contract.mjs tests/test_graph_asset_package.py pyproject.toml .gitignore .gitattributes src/hk_movie_rag/demo_api.py tests/test_demo_api.py
git commit -m "build: vendor graph atlas runtime"
```

---

### Task 11: Build the Graph Atlas shell, pure state machine, and canvas interactions

**Files:**
- Create: `src/hk_movie_rag/static/api.js`
- Create: `src/hk_movie_rag/static/chat.js`
- Create: `src/hk_movie_rag/static/graph-state.js`
- Create: `src/hk_movie_rag/static/graph-canvas.js`
- Create: `src/hk_movie_rag/static/graph-atlas.js`
- Create: `tests/graph_state_contract.mjs`
- Modify: `src/hk_movie_rag/static/app.js`
- Modify: `src/hk_movie_rag/static/index.html`
- Modify: `src/hk_movie_rag/static/styles.css`
- Modify: `tests/static_app_ui_contract.mjs`

**Interfaces:**
- Produces: `createApiClient(fetchImpl)`, whose methods map one-to-one to session/config/chat/poster and the six graph endpoints.
- Produces pure state functions `createGraphState()`, `mergeNeighborhood()`, `inspectEntity()`, `followRelationship()`, `backtrackPath()`, `clearPath()`, `setPredicateFilters()`, and `canExpandTerminal()`.
- Produces: `createGraphCanvas({cytoscapeFactory, container, onInspectEntity})` with `render(state)`, `fit()`, `reset()`, `setReducedMotion()`, and `destroy()`.
- Produces: `mountGraphAtlas(root, {api, cytoscapeFactory, initialConfig})`.
- Refactors existing chat behavior into `mountChat()` without changing its network/session/citation/card semantics.
- Invariant: a canvas node click only inspects. Only `followRelationship(edge_id, adjacent_entity_id)` mutates the breadcrumb.

- [ ] **Step 1: Freeze the disabled-state legacy behavior before refactoring**

Extend the existing Node test so `graph_enabled=false` proves login, `/api/config`, chat submit, continuation context, citations/cards, poster handling, `401` recovery, and logout still call the same endpoints and render the same user-visible states.

Run: `node --test tests/static_app_ui_contract.mjs`

Expected: PASS against the current single-file app before refactoring.

- [ ] **Step 2: Write the pure graph-state RED suite**

```javascript
test('inspection never changes the committed path', () => {
  const before = seededStateWithParallelEdges();
  const after = inspectEntity(before, 'person-name:yuen');
  assert.deepEqual(after.path, before.path);
  assert.equal(after.selectedEntityId, 'person-name:yuen');
});

test('following one exact parallel edge appends only that edge', () => {
  const after = followRelationship(
    seededStateWithParallelEdges(),
    'edge:directed-by',
    'person-name:yuen',
  );
  assert.deepEqual(after.path.edgeIds, ['edge:directed-by']);
  assert.deepEqual(after.path.entityIds, ['movie:1978_ZQ_001', 'person-name:yuen']);
});
```

Also test node/edge de-duplication, retaining every parallel edge, terminal-only expansion, path continuity, three-edge cap, fourth-edge rejection, inspection of loaded nonterminal nodes, backtrack removal only for now-unreferenced graph elements, clear/reset, pagination merge, stale-response binding rejection, and predicate filter stability.

Run: `node --test tests/graph_state_contract.mjs`

Expected: FAIL because `graph-state.js` is absent.

- [ ] **Step 3: Split network and legacy chat code without visual changes**

Move same-origin `fetch` behavior to `api.js`; require `credentials: 'same-origin'`, JSON content type for POSTs, controlled handling of non-JSON/error bodies, and no dynamic URL from a GCS/object path. Move existing chat DOM behavior to `chat.js`. Keep `app.js` as the session/config bootstrap:

```javascript
const config = await api.getConfig();
if (config.graph_enabled) {
  await mountGraphAtlas(graphRoot, { api, cytoscapeFactory: cytoscape, initialConfig: config });
} else {
  mountChat(chatRoot, { api, config });
}
```

The real implementation imports Cytoscape from `/static/cytoscape-3.34.0.esm.min.mjs`; tests inject a fake factory.

- [ ] **Step 4: Implement immutable/pure graph state**

State shape is explicit and JSON-safe:

```javascript
{
  binding: { projectionId, projectionDigest, ragReleaseId },
  nodesById: new Map(),
  edgesById: new Map(),
  selectedEntityId: null,
  path: { entityIds: [], edgeIds: [] },
  predicateFilters: new Set(['DIRECTED_BY', 'WRITTEN_BY', 'ACTED_IN', 'HAS_GENRE']),
  neighborhoodCursors: new Map(),
}
```

Functions return new state and validate IDs/edge endpoints/binding before mutation. A returned neighborhood group is merged even when its adjacent entity already exists; edges remain keyed independently.

- [ ] **Step 5: Build the desktop shell and canvas adapter**

`index.html` must contain semantic regions for header/search/status/logout, left legend/filter/viewport controls, central canvas, hidden semantic relationship list, right inspector container, and bottom breadcrumb. Visible factual text starts empty and comes only from API responses.

The canvas adapter uses locally imported Cytoscape, built-in shapes, text labels, deterministic node-type styles, explicit selected/terminal classes, bounded layout settings, and injected callbacks. Cytoscape events may call `inspectEntity` only; follow buttons are rendered by the relationship list/inspector with an exact edge ID.

Use the approved tokens:

```css
:root {
  --atlas-bg: #0d1111;
  --atlas-ivory: #f6f0e5;
  --atlas-coral: #ef5b3f;
  --atlas-coral-bright: #ff795e;
  --atlas-jade: #84b89b;
}
```

- [ ] **Step 6: Add search/ambiguity/expansion coordination**

Search shows separate candidates and never auto-selects a collision. Selecting a candidate establishes a one-entity origin/path and loads its detail/neighborhood. Expansion is enabled only on the breadcrumb terminal, sends the exact selected entity/edge sequence, and focuses the newly added semantic relationship group. Search continuation and neighborhood continuation preserve their independent signed cursors.

- [ ] **Step 7: Run Node contract tests**

```powershell
node --test tests/static_app_ui_contract.mjs tests/graph_state_contract.mjs
```

Expected: PASS for false-flag chat parity, true-flag Graph Atlas bootstrap, explicit path mutation, de-duplication, three-hop budget, search ambiguity, and no CDN/image fabrication.

- [ ] **Step 8: Commit the UI foundation**

```powershell
git add src/hk_movie_rag/static/api.js src/hk_movie_rag/static/chat.js src/hk_movie_rag/static/graph-state.js src/hk_movie_rag/static/graph-canvas.js src/hk_movie_rag/static/graph-atlas.js src/hk_movie_rag/static/app.js src/hk_movie_rag/static/index.html src/hk_movie_rag/static/styles.css tests/static_app_ui_contract.mjs tests/graph_state_contract.mjs
git commit -m "feat: build graph atlas exploration shell"
```

---

### Task 12: Complete inspector, recommendations, explanation, responsive UX, and accessibility

**Files:**
- Create: `src/hk_movie_rag/static/graph-inspector.js`
- Create: `src/hk_movie_rag/static/graph-explanation.js`
- Create: `tests/graph_atlas_ui_contract.mjs`
- Modify: `src/hk_movie_rag/static/graph-atlas.js`
- Modify: `src/hk_movie_rag/static/index.html`
- Modify: `src/hk_movie_rag/static/styles.css`
- Modify: `tests/static_app_ui_contract.mjs`

**Interfaces:**
- Produces: `renderGraphInspector(container, viewModel, actions)` and `renderRelationshipList(container, groups, actions)`.
- Produces: `createGraphExplanationPanel(container, {api, getSelectedPath})`.
- Produces an explicit shortest-path finder that searches two governed endpoints, displays every returned candidate path, and commits one only after the user activates `Use this path`.
- Produces movie recommendation controls only for a movie anchor; person/genre related-film panels reuse the normal neighborhood endpoint with an independent one-entity inspector origin.
- Produces exact responsive modes: desktop `>=1024px`, tablet `768..1023px`, mobile `<768px`.
- Invariant: every canvas relationship/action has an equivalent keyboard/screen-reader list action.

- [ ] **Step 1: Write UI behavior RED tests**

Cover:

- movie inspector metadata/source/poster state and explicit unavailable poster with zero image requests;
- person inspector exact caveat: `Matched by exact governed credit name within this Release; same-name people may be combined.`;
- genre/person adjacent-movie pagination without changing canvas breadcrumb;
- recommendation score reasons, exact shared entity labels, paired edge citations, filters, exclusions, semantic preference, and no unexplained percentage;
- explanation open/submit/citations/close, single-turn input, provider failure preserving nodes/path, and unsupported evidence message;
- shortest-path endpoint search, no-path state, multiple deterministic candidates, `incomplete` warning, and explicit path choice with no automatic first-path selection;
- parallel-edge `Follow relationship` choice, back/clear, depth state, and fourth-hop disabled text;
- loading/empty/ambiguity/422/503/retry states without stale graph rendering;
- focus movement after expansion, focus return after sheet/dialog close, keyboard search/selection/follow/back/paginate/explain/logout;
- semantic relationship list parity and node type conveyed by text/shape, not color alone;
- reduced motion disabling layout transitions;
- no fake face, external image, emoji, inline SVG, raw GCS URI, or unsupplied factual text.

Run: `node --test tests/graph_atlas_ui_contract.mjs`

Expected: FAIL because inspector/explanation modules and responsive behavior are absent.

- [ ] **Step 2: Implement evidence-first inspector rendering**

Render via DOM text properties, never API-provided HTML. Movie posters use only a validated same-origin `/api/posters/{movie_id}` value. Person nodes use name/initials/shape only. Relationship rows display predicate label, endpoint, source movie/field summary, identity caveat where applicable, and an exact follow action carrying the returned edge ID.

Movie recommendations render server score components and their exact relationships; they do not calculate scores client-side. Person/genre inspectors call neighborhood with their own one-entity origin/path and movie-connecting predicates, leaving the main breadcrumb unchanged.

- [ ] **Step 3: Implement the secondary relationship explanation panel**

Enable `Ask about this relationship` only for a selected path of one through three edges. Submit only the current question plus `path.entityIds` and `path.edgeIds`. Render validated answer Markdown through the existing safe renderer and link each citation to an inspector evidence item; never send conversation history or client evidence bodies. On failure, keep graph/selection/path visible and show a controlled retry message.

Add `Find a relationship path` as a separate explicit action. Reuse governed entity search for the start/end selectors, call `/api/graph/paths` with fixed depth at most three, render each returned path's labels/predicates in server order, show `incomplete` honestly, and require `Use this path` before replacing the current breadcrumb. A no-path response never calls the model.

- [ ] **Step 4: Implement exact responsive layouts**

- Desktop (`min-width:1024px`): graph and right inspector visible side by side, persistent bottom breadcrumb.
- Tablet (`768px..1023px`): graph fills main area and inspector is a dismissible, focus-managed bottom sheet.
- Mobile (`max-width:767px`): canvas is not the default interaction surface; show the equivalent relationship list first with the same search, entities, edges, path, evidence, pagination, recommendation, and explanation actions.

Do not merely shrink the desktop graph. Preserve 44px minimum touch targets, readable focus rings, safe-area padding, and no horizontal page overflow.

- [ ] **Step 5: Implement accessibility and reduced motion**

Use a real heading hierarchy, labels, buttons, status/live regions, list semantics, `aria-current` for selected breadcrumb, and an accessible name/description for the canvas plus link to the equivalent list. Manage focus deterministically; do not trap focus except in the tablet sheet/explanation dialog while open. Under `prefers-reduced-motion: reduce`, disable animated Cytoscape transitions, smooth scroll, and CSS motion.

- [ ] **Step 6: Run all static contracts**

```powershell
npm run vendor:check
npm run test:static
```

Expected: PASS for legacy false-flag chat, Graph Atlas state, asset supply chain, inspector/recommendation/explanation behavior, responsive semantics, keyboard operation, and reduced motion.

- [ ] **Step 7: Run local authenticated browser acceptance at three viewports**

In one terminal, load the packaged digest dynamically and start only the guarded offline app:

```powershell
$binding = Get-Content -LiteralPath 'src/hk_movie_rag/authorities/kg_projection_v1_2_kg1.json' -Raw | ConvertFrom-Json
$env:RAG_DEMO_OFFLINE='1'
$env:RAG_DEMO_ACCESS_KEY='local-only-code'
$env:RAG_GRAPH_ENABLED='true'
$env:RAG_GRAPH_PROJECTION_SHA256=$binding.projection_digest
uv run uvicorn hk_movie_rag.demo_api:create_offline_app --factory --host 127.0.0.1 --port 8000
```

Use the available in-app browser controller (or the verified `agent-browser` tool if installed; do not assume a shell executable exists) and verify at `1440x900`, `900x1024`, and `390x844`:

1. log in and confirm Graph Atlas, not a blank chatbot, is home;
2. search `醉拳`, select the exact movie, and inspect governed fields/poster state;
3. explicitly follow the `袁和平` relationship, continue to a movie and then genre for three edges, and prove a fourth is blocked;
4. click another loaded node and prove the breadcrumb is unchanged;
5. backtrack, paginate, resolve an ambiguous alias by explicit selection, and inspect the person identity caveat;
6. use the two-endpoint path finder, compare multiple returned paths, and explicitly commit one without an automatic first-path choice;
7. open a recommendation and see exact shared-relation reasons;
8. ask about the selected relationship and verify ordered graph citations;
9. exercise keyboard-only list flow, tablet sheet, mobile list-first mode, reduced motion, unavailable poster, logout, and unauthenticated redirect;
10. inspect browser console/network: zero errors, zero CDN/person-image/GCS requests, and posters only through same-origin proxy.

Also restart with `RAG_GRAPH_ENABLED=false` and prove the legacy chat home still completes its established contract.

- [ ] **Step 8: Commit the completed responsive product surface**

```powershell
git add src/hk_movie_rag/static/graph-inspector.js src/hk_movie_rag/static/graph-explanation.js src/hk_movie_rag/static/graph-atlas.js src/hk_movie_rag/static/index.html src/hk_movie_rag/static/styles.css tests/graph_atlas_ui_contract.mjs tests/static_app_ui_contract.mjs
git commit -m "feat: complete graph atlas demo ux"
```

---

### Task 13: Extend immutable deployment, candidate smoke, rollback, and runbook contracts

**Files:**
- Create: `docs/runbooks/graph-atlas-rollout.md`
- Modify: `scripts/gcp/deploy-demo.ps1`
- Modify: `scripts/gcp/live-smoke.py`
- Modify: `tests/test_gcp_demo_scripts.py`
- Modify: `.env.example`
- Modify: `README.md`

**Interfaces:**
- Extends `deploy-demo.ps1` with `-EnableGraphAtlas` and a typed `-GraphProjectionDirectory` path parameter that must resolve inside the clean implementation worktree; default invocation remains graph-disabled.
- Uses constants `v1.2-kg1` and `graph/v1.2-kg1/`; no floating projection name is accepted.
- Reuses existing `Assert-ImmutableReleaseObject`, private release bucket, runtime service account, Cloud SQL instance, one ingestion Job, and no-traffic/etag-CAS deployment machinery.
- Extends `live-smoke.py` to accept one explicit service/tag base URL and verify graph config plus full graph API scenarios.
- Produces deployment receipt fields for graph projection ID/digest, manifest file SHA, graph Job execution, entity/alias/edge counts, candidate smoke, promotion, stable smoke, and rollback if used.
- Invariant: no Terraform/API/IAM/database/service/secret is added. `infra/terraform/archive/**` remains unchanged.

- [ ] **Step 1: Write fake-GCP deployment RED tests**

Add tests for both graph-disabled compatibility and enabled deployment. Enabled cases must cover:

- contained projection directory and exact packaged-authority byte match;
- local `verify-kg-projection` before any gcloud mutation;
- create-only upload order `entities -> aliases -> edges -> manifest`;
- JSONL `application/x-ndjson`, manifest `application/json`, private `no-store`, generation-match zero, SHA metadata/bytes/MIME/cache replay verification;
- identical object replay success and one-byte existing conflict failure;
- manifest-last publication and zero Job/service calls if an earlier artifact fails;
- reuse of `hk-movie-rag-ingest`, zero retries, exact graph CLI args, active-execution rejection, and graph activation before service candidate;
- exact service environment allowlist with literal `RAG_GRAPH_ENABLED=true` and exact `RAG_GRAPH_PROJECTION_SHA256`; reject duplicates, uppercase Boolean, malformed/missing/extra values;
- no-traffic candidate with an owned lowercase tag, tag URL discovery, authenticated smoke before promotion, and no promotion call when candidate smoke fails;
- etag-CAS promotion, one owned rollback only, prior revision capture, and no blind/latest rollback;
- secret/version/access-key absence from child gcloud environments, argv, logs, and receipts;
- complete graph receipt values and redacted failure receipts;
- graph-disabled deploy has `RAG_GRAPH_ENABLED=false`, no projection digest/upload/graph Job, and unchanged legacy smoke.

Run: `uv run pytest tests/test_gcp_demo_scripts.py -q`

Expected: FAIL because deployment scripts do not yet know the projection or pre-promotion graph smoke.

- [ ] **Step 2: Add local projection verification and immutable upload**

When `-EnableGraphAtlas` is present, require the contained directory to hold exactly the four verified files and require its manifest bytes to equal `src/hk_movie_rag/authorities/kg_projection_v1_2_kg1.json`. Reuse `Assert-ImmutableReleaseObject` and publish:

```text
graph/v1.2-kg1/kg_entities.jsonl
graph/v1.2-kg1/kg_aliases.jsonl
graph/v1.2-kg1/kg_edges.jsonl
graph/v1.2-kg1/kg_projection_manifest.json
```

Upload the manifest only after all three JSONL objects independently verify. Record the manifest file SHA separately from the projection digest; never put the manifest into its own digest input.

- [ ] **Step 3: Reconfigure/reuse the existing ingestion Job for graph activation**

After the existing RAG resume-ingestion gate, update the same Job image/Cloud SQL/service-account identity to run exactly:

```text
ingest-kg-projection
$graphManifestUri
--require-projection-id
v1.2-kg1
--require-projection-sha256
$projectionDigest
```

`$graphManifestUri` and `$projectionDigest` are deployment variables already resolved and validated by the script; tests must assert their concrete fake values. Execute with zero retries; require terminal success and exact summary counts before poster verification or service deployment. A failure leaves stable service traffic unchanged.

- [ ] **Step 4: Add exact service binding and candidate tag smoke**

Always deploy `RAG_GRAPH_ENABLED`; include `RAG_GRAPH_PROJECTION_SHA256` only when enabled. Extend the exact candidate environment allowlist and reject every extra variable.

Deploy the enabled revision with `--no-traffic --tag graph-atlas-candidate`. Google Cloud documents this as the supported way to test a tagged revision that serves no stable traffic: [Cloud Run rollouts, rollbacks, and traffic migration](https://docs.cloud.google.com/run/docs/rollouts-rollbacks-traffic-migration).

Discover the returned tag URL from the service/revision description rather than constructing a hostname. Run the complete authenticated smoke against that tag URL before any `update-traffic` call. On failure, keep stable traffic on the prior revision and emit a diagnostic receipt identifying the unpromoted candidate.

- [ ] **Step 5: Extend live smoke with graph identity and product scenarios**

The smoke must first assert exact revision/image/RAG config plus:

```python
assert config["graph_enabled"] is True
assert config["graph_projection_id"] == "v1.2-kg1"
assert config["graph_projection_sha256"] == expected_projection_digest
assert config["graph_asset_sha256"] == expected_asset_digest
```

Then exercise authenticated HTTP flows for exact `醉拳` search/detail, `袁和平` expansion and `name_only` caveat, three-hop path/fourth-hop rejection, shortest paths, deterministic recommendation reasons, simplified alias ambiguity, graph explanation citation sequence, private/absent poster behavior, legacy `/api/chat`, logout, and unauthenticated rejection. Validate no response contains `gs://`, bucket names, SQL, credentials, or raw provider errors.

- [ ] **Step 6: Promote with CAS, smoke stable URL, and preserve one rollback**

Only after candidate smoke passes, re-read service state, prove no unowned revision/traffic/env change occurred, and promote the exact candidate through the existing etag-CAS path. Run the same full smoke against the stable URL. If stable smoke fails, perform the existing single owned rollback to the captured prior serving revision and verify that revision; never choose “latest.” Remove the owned candidate tag only after successful stable verification or documented rollback cleanup.

Required execution order is:

```text
verify local RAG and graph artifacts
-> create-only graph JSONL upload
-> create-only graph manifest upload
-> existing RAG resume ingestion
-> graph ingestion and activation
-> poster fleet verification
-> no-traffic tagged service candidate
-> candidate full smoke
-> etag-CAS promotion
-> stable full smoke
```

- [ ] **Step 7: Write exact runbook and evidence boundaries**

Document that the current data workspace contains user-owned untracked PDFs/`tmp/`, while `deploy-demo.ps1 -Apply` requires the implementation worktree itself to be completely clean. Run any future Apply from a clean implementation worktree and pass `D:\VS_PROJECT\HK_Movie_KnowledgeGraph\RAG` as read-only `-SourceRoot` for ignored governed data.

The runbook must distinguish:

1. code/tests green;
2. graph artifacts built and authority-bound;
3. immutable objects uploaded;
4. projection activated in Cloud SQL;
5. candidate deployed but not serving stable traffic;
6. candidate smoke verified;
7. stable traffic promoted and smoke verified;
8. production readiness, which this restricted demo still does not claim.

Include rollback-by-prior-revision and graph-flag-off redeploy procedures. State that additive tables/artifacts remain for diagnosis and existing movies/embeddings/documents/posters/chat/source archive are never deleted.

- [ ] **Step 8: Run deployment unit/dry-run gates only**

```powershell
uv run pytest tests/test_gcp_demo_scripts.py -q
pwsh -File scripts/gcp/provision-demo.ps1 -ProjectId motionexpaiweb -Region us-central1 -PlanOnly
pwsh -File scripts/gcp/deploy-demo.ps1 -ProjectId motionexpaiweb -Region us-central1 -SourceRoot 'D:\VS_PROJECT\HK_Movie_KnowledgeGraph\RAG' -EnableGraphAtlas -GraphProjectionDirectory 'artifacts\kg\v1.2-kg1-a' -PlanOnly
```

Expected: tests PASS; both scripts report plans without resource mutation. Do not run `-Apply` in this task without a separate user authorization.

- [ ] **Step 9: Commit deployment contracts and runbook**

```powershell
git add scripts/gcp/deploy-demo.ps1 scripts/gcp/live-smoke.py tests/test_gcp_demo_scripts.py docs/runbooks/graph-atlas-rollout.md README.md .env.example
git commit -m "feat: gate graph atlas deployment"
```

---

### Task 14: Run the complete local verification and evidence handoff

**Files:**
- Verify all files in this plan.
- Modify only files required to fix a newly reproduced failure; add the regression to the owning task's test file.

**Interfaces:**
- Consumes: one clean implementation branch, the ignored governed Release v1.2 source root, a disposable PostgreSQL 16 + pgvector DSN for the database integration gate, and no cloud mutation authority.
- Produces: fresh command evidence for source determinism, package integrity, Python/Node tests, static typing/lint, database query plans, wheel/container packaging, local browser UX, and cloud dry-run plans.
- Invariant: completion claims name the gates actually run and keep cloud deployment/prod readiness explicitly separate.

- [ ] **Step 1: Recreate both dependency environments from locks**

```powershell
uv sync --frozen
npm ci
npm run vendor:check
```

Expected: PASS with no lockfile diff and exact vendored asset/authority parity.

- [ ] **Step 2: Run focused Python graph suites**

```powershell
uv run pytest tests/test_rag_payload.py tests/test_kg_policy.py tests/test_kg_canonical.py tests/test_kg_bundle.py -q
uv run pytest tests/test_kg_db.py tests/test_kg_ingest.py tests/test_kg_cursor.py tests/test_kg_query.py -q
uv run pytest tests/test_kg_grounding.py tests/test_kg_runtime.py tests/test_kg_api.py -q
uv run pytest tests/test_demo_api.py tests/test_vertex_clients.py tests/test_graph_asset_package.py tests/test_gcp_demo_scripts.py -q
```

Expected: all PASS; no skip except tests explicitly gated on external source/database variables.

- [ ] **Step 3: Re-run the real source determinism/count gate**

```powershell
$env:HK_MOVIE_RAG_SOURCE_ROOT='D:\VS_PROJECT\HK_Movie_KnowledgeGraph\RAG'
uv run pytest tests/integration/test_kg_v12_projection.py -q
uv run hk-movie-rag verify-kg-projection artifacts/kg/v1.2-kg1-a/kg_projection_manifest.json
uv run hk-movie-rag bind-kg-projection-authority artifacts/kg/v1.2-kg1-a/kg_projection_manifest.json src/hk_movie_rag/authorities/kg_projection_v1_2_kg1.json --check
```

Expected: PASS with the exact 10,730/14,602/32,083 totals, detailed fixture counts, and no packaged-authority drift.

- [ ] **Step 4: Run the disposable PostgreSQL integration/query-plan gate**

Set `HK_MOVIE_RAG_TEST_DATABASE_URL` to an explicitly disposable PostgreSQL 16 database with pgvector, never the production Cloud SQL DSN, then run:

```powershell
uv run pytest tests/integration/test_kg_postgres.py -q
```

Expected: PASS for migrations 0001..0004, activation/replay/failure lifecycle, six graph query plans, local timeouts, projection predicates, and bounded recursion. If no disposable database is available, report this gate as not run and do not claim database integration completion.

- [ ] **Step 5: Run global static analysis and all unit/integration-default tests**

```powershell
uv run ruff check .
uv run mypy src/hk_movie_rag
uv run pytest -q
npm run test:static
```

Expected: all PASS. The default pytest run may show only the two documented opt-in integration skips when their environment variables are absent.

- [ ] **Step 6: Verify wheel and container runtime contents**

```powershell
uv build
docker build --tag hk-movie-rag:graph-atlas-local .
```

Expected: PASS. Inspect the fresh wheel/container to prove projection/policy/schema/migration/asset authorities, every JS module, the Cytoscape ESM, and MIT license are present; `node_modules`, raw source data, credentials, and generated projection JSONL are absent.

- [ ] **Step 7: Repeat the browser acceptance from Task 12**

Run the enabled and disabled guarded offline app modes and repeat desktop/tablet/mobile, keyboard, reduced-motion, console, and network verification. Capture screenshots/logs as local evidence under ignored `artifacts/verification/`; do not commit fabricated fixture screenshots as product data.

- [ ] **Step 8: Run cloud plan-only checks and stop**

```powershell
pwsh -File scripts/gcp/provision-demo.ps1 -ProjectId motionexpaiweb -Region us-central1 -PlanOnly
pwsh -File scripts/gcp/deploy-demo.ps1 -ProjectId motionexpaiweb -Region us-central1 -SourceRoot 'D:\VS_PROJECT\HK_Movie_KnowledgeGraph\RAG' -EnableGraphAtlas -GraphProjectionDirectory 'artifacts\kg\v1.2-kg1-a' -PlanOnly
```

Expected: PASS without uploads, Job executions, service revisions, traffic changes, IAM/API/Terraform mutation, or secret access. Live Apply remains a new authorization checkpoint.

- [ ] **Step 9: Inspect repository scope and produce the handoff**

```powershell
git diff --check
git status --short
git log --oneline --decorate -15
```

Expected: no whitespace errors; implementation files are committed in task-sized commits; the only unrelated untracked entries remain the user's pre-existing PDFs and `tmp/`.

The handoff must report exact command outcomes and separately label: implemented code, committed authorities/assets, locally verified real projection, disposable-DB verification, local browser verification, cloud dry-run verification, and live deployment not executed. Do not claim complete deployment or production readiness.

---

## Implementation Completion Criteria

Implementation is complete only when Tasks 1 through 14 are checked, every mandatory local gate above has fresh passing evidence, the real v1.2 authority matches a repeat build, the disposable PostgreSQL integration gate passes, and the enabled/disabled browser flows pass. Cloud upload/deployment remains deliberately incomplete until separately authorized; after that authorization, completion additionally requires candidate smoke before promotion and stable-URL smoke after promotion.
