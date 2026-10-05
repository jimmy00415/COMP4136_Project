import re
from pathlib import Path

import pyarrow.parquet as pq

from hk_movie_rag.config import Settings
from hk_movie_rag.hashing import sha256_file
from hk_movie_rag.posters import build_posters, load_release_ids
from hk_movie_rag.release_data import build_release_data


def test_v12_release_reconciles_exactly(repo_root: Path) -> None:
    """Breaks if any governed Release population or source-boundary count drifts."""
    settings = Settings.load(repo_root)
    source_paths = _source_paths(settings)
    source_hashes_before = {path: sha256_file(path) for path in source_paths}
    output_dir = repo_root / "data" / "release" / "v1.2"

    first = build_release_data(settings, output_dir)
    first_hashes = _artifact_hashes(output_dir)
    second = build_release_data(settings, output_dir)
    second_hashes = _artifact_hashes(output_dir)

    assert first.movie_count == 4658
    assert first.unique_movie_ids == 4658
    assert first.tier_counts == {"S": 50, "A": 313, "B": 4295}
    assert first.pilot_count == 24
    assert first.provenance_count == 1008
    assert first.quarantine_overlap == 0
    assert second == first
    assert second_hashes == first_hashes
    assert {path: sha256_file(path) for path in source_paths} == source_hashes_before


def test_v12_release_artifact_schemas_are_exact(repo_root: Path) -> None:
    """Breaks if generated artifacts expose source-only columns or rename storage fields."""
    output_dir = repo_root / "data" / "release" / "v1.2"
    build_release_data(Settings.load(repo_root), output_dir)

    assert pq.read_schema(output_dir / "movies.parquet").names == [
        "movie_id",
        "chinese_title",
        "english_title",
        "release_date",
        "production_region",
        "director",
        "screenwriter",
        "cast",
        "genre",
        "runtime_minutes",
        "production_company",
        "data_source",
    ]
    assert pq.read_schema(output_dir / "movie_tiers.parquet").names == [
        "movie_id",
        "tier",
        "tier_reason",
        "human_review",
        "pilot_movie",
    ]
    assert pq.read_schema(output_dir / "pilot_movies.parquet").names == [
        "movie_id",
        "handbook_genre",
        "evidence_note",
        "evidence_url",
        "source_record_id",
    ]
    assert pq.read_schema(output_dir / "enrichment_provenance.parquet").names == [
        "movie_id",
        "chinese_title",
        "release_date",
        "field_name",
        "original_value",
        "filled_value",
        "source_label",
        "source_url",
        "source_record_id",
        "match_method",
        "confidence",
        "matched_imdb_id",
        "retrieved_at",
        "notes",
    ]
    provenance_dates = pq.read_table(
        output_dir / "enrichment_provenance.parquet", columns=["release_date"]
    ).column("release_date").to_pylist()
    assert all(re.fullmatch(r"\d{4}-\d{2}-\d{2}", value) for value in provenance_dates)
    csv_bytes = (output_dir / "movies.csv").read_bytes()
    assert b"\r\n" not in csv_bytes
    assert csv_bytes.startswith(b"movie_id,chinese_title,english_title,")


def test_v12_retrieved_at_is_fixed_naive_iso(repo_root: Path) -> None:
    """Breaks if live provenance emits an Excel serial or inconsistent timestamp text."""
    output_dir = repo_root / "data" / "release" / "v1.2"
    build_release_data(Settings.load(repo_root), output_dir)
    provenance = pq.read_table(
        output_dir / "enrichment_provenance.parquet",
        columns=["movie_id", "retrieved_at"],
    ).to_pylist()

    assert all(
        re.fullmatch(r"\d{4}-\d{2}-\d{2}T\d{2}:\d{2}:\d{2}\.\d{6}", row["retrieved_at"])
        for row in provenance
    )
    assert next(
        row["retrieved_at"] for row in provenance if row["movie_id"] == "1997_YB2DZ_001"
    ) == "2026-07-17T02:30:35.000000"


def test_v12_poster_population_reconciles(repo_root: Path) -> None:
    """Breaks if exact poster keys, content groups, or detected formats drift."""
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


def _artifact_hashes(output_dir: Path) -> dict[str, str]:
    return {
        path.name: sha256_file(path)
        for path in sorted(output_dir.iterdir())
        if path.is_file()
    }


def _source_paths(settings: Settings) -> tuple[Path, ...]:
    return (
        settings.source_manifest,
        settings.source_validation,
        settings.movies_csv,
        settings.movies_xlsx,
        settings.tiers_xlsx,
        settings.issue_ledger_xlsx,
    )
