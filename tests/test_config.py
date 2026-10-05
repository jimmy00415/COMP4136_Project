import subprocess
from pathlib import Path

import pytest
import yaml

from hk_movie_rag.config import ConfigError, Settings


def test_settings_loads_fixed_release_contract(repo_root: Path, tmp_path: Path) -> None:
    settings = Settings.load(_repo_with_source_config(repo_root, tmp_path))
    assert settings.release_version == "v1.2"
    assert settings.expected.release_movies == 4658
    assert settings.gcp.project_id == "motionexpaiweb"
    assert settings.gcp.region == "us-central1"


def test_resolve_input_rejects_escape(repo_root: Path, tmp_path: Path) -> None:
    settings = Settings.load(_repo_with_source_config(repo_root, tmp_path))
    with pytest.raises(ConfigError, match="outside workspace"):
        settings.resolve_input("../outside.csv")


def test_load_retains_declared_inputs_as_resolved_workspace_paths(tmp_path: Path) -> None:
    configured_root = _configured_repo(tmp_path)

    settings = Settings.load(configured_root)

    assert settings.movies_csv == (configured_root / "inputs" / "movies.csv").resolve()
    assert settings.inventory_roots == ((configured_root / "inputs" / "inventory").resolve(),)
    assert settings.resolve_input("inputs/movies.csv") == settings.movies_csv


@pytest.mark.parametrize(
    ("configured_path", "error"),
    [
        ("../outside.csv", "outside workspace"),
        ("absolute", "outside workspace"),
        ("inputs/missing.csv", "configured input is missing"),
    ],
)
def test_load_rejects_invalid_configured_input_paths(
    tmp_path: Path, configured_path: str, error: str
) -> None:
    configured_root = _configured_repo(tmp_path)
    if configured_path == "absolute":
        configured_path = str((tmp_path / "outside.csv").resolve())
    _replace_local_value(configured_root, "source_manifest", configured_path)

    with pytest.raises(ConfigError, match=error):
        Settings.load(configured_root)


def test_load_rejects_configured_reparse_escape(tmp_path: Path) -> None:
    configured_root = _configured_repo(tmp_path)
    outside = tmp_path / "outside"
    outside.mkdir()
    escape_link = configured_root / "inputs" / "escape"
    try:
        escape_link.symlink_to(outside, target_is_directory=True)
    except OSError:
        result = subprocess.run(
            ["cmd", "/c", "mklink", "/J", str(escape_link), str(outside)],
            capture_output=True,
            check=False,
            encoding="utf-8",
            errors="replace",
            text=True,
        )
        assert result.returncode == 0, result.stderr
    _replace_local_value(configured_root, "inventory_roots", ["inputs/escape"])

    with pytest.raises(ConfigError, match="no-follow containment"):
        Settings.load(configured_root)

def test_load_retains_an_absent_detached_inventory_root(
    tmp_path: Path,
) -> None:
    """Breaks if read-only post-cleanup checks require archived raw roots to exist."""
    configured_root = _configured_repo(tmp_path)
    detached_root = configured_root / "inputs" / "inventory"
    detached_root.rmdir()

    settings = Settings.load(configured_root)

    assert settings.inventory_roots == (detached_root,)


def test_load_rejects_configured_source_reparse_inside_workspace(
    tmp_path: Path,
) -> None:
    """Breaks if resolve() erases a configured source's linked ancestry."""
    configured_root = _configured_repo(tmp_path)
    real_source = configured_root / "inputs" / "real-source"
    real_source.mkdir()
    (real_source / "manifest.json").touch()
    source_link = configured_root / "inputs" / "source-link"
    try:
        source_link.symlink_to(real_source, target_is_directory=True)
    except OSError:
        result = subprocess.run(
            ["cmd", "/c", "mklink", "/J", str(source_link), str(real_source)],
            capture_output=True,
            check=False,
            encoding="utf-8",
            errors="replace",
            text=True,
        )
        if result.returncode != 0:
            pytest.skip(f"cannot create a source reparse fixture: {result.stderr}")
    _replace_local_value(
        configured_root, "source_manifest", "inputs/source-link/manifest.json"
    )

    with pytest.raises(ConfigError, match="no-follow"):
        Settings.load(configured_root)


def _repo_with_source_config(repo_root: Path, tmp_path: Path) -> Path:
    """Retain actual release/GCP settings with controlled existing input paths."""
    fixture_root = _configured_repo(tmp_path)
    fixture_local = yaml.safe_load((fixture_root / "config/local.yaml").read_text())
    source_local = yaml.safe_load((repo_root / "config/local.yaml").read_text(encoding="utf-8"))
    for field in (
        "source_manifest", "source_validation", "movies_csv", "movies_xlsx",
        "tiers_xlsx", "issue_ledger_xlsx", "inventory_roots", "canonical_poster_roots",
    ):
        source_local[field] = fixture_local[field]
    (fixture_root / "config/local.yaml").write_text(
        yaml.safe_dump(source_local, allow_unicode=True), encoding="utf-8"
    )
    (fixture_root / "config/gcp.yaml").write_bytes((repo_root / "config/gcp.yaml").read_bytes())
    return fixture_root


def _configured_repo(tmp_path: Path) -> Path:
    repo_root = tmp_path / "repo"
    local_config = {
        "release_version": "v1.2",
        "source_manifest": "inputs/manifest.json",
        "source_validation": "inputs/validation.json",
        "movies_csv": "inputs/movies.csv",
        "movies_xlsx": "inputs/movies.xlsx",
        "tiers_xlsx": "inputs/tiers.xlsx",
        "issue_ledger_xlsx": "inputs/issues.xlsx",
        "inventory_roots": ["inputs/inventory"],
        "canonical_poster_roots": [{"path": "inputs/posters", "recursive": False}],
        "expected": {
            "release_movies": 4658,
            "quarantine_movies": 1264,
            "tier_counts": {"S": 50, "A": 313, "B": 4295},
            "pilot_movies": 24,
            "audit_rows": 1008,
            "poster_states": {
                "machine_passed": 4545,
                "content_conflict": 73,
                "placeholder": 9,
                "missing": 31,
            },
        },
    }
    gcp_config = {
        "project_id": "motionexpaiweb",
        "region": "us-central1",
        "required_account": "admin@motionexp.com",
        "archive_bucket_template": "{project_id}-{project_number}-hk-movie-rag-source-archive",
        "archive_prefix": "source/v1.2",
        "required_services": ["storage.googleapis.com"],
    }
    (repo_root / "config").mkdir(parents=True)
    with (repo_root / "config" / "local.yaml").open("w", encoding="utf-8") as file:
        yaml.safe_dump(local_config, file, allow_unicode=True)
    with (repo_root / "config" / "gcp.yaml").open("w", encoding="utf-8") as file:
        yaml.safe_dump(gcp_config, file, allow_unicode=True)
    (repo_root / "inputs").mkdir()
    for relative_path in (
        "inputs/manifest.json",
        "inputs/validation.json",
        "inputs/movies.csv",
        "inputs/movies.xlsx",
        "inputs/tiers.xlsx",
        "inputs/issues.xlsx",
    ):
        (repo_root / relative_path).touch()
    (repo_root / "inputs" / "inventory").mkdir()
    (repo_root / "inputs" / "posters").mkdir()
    return repo_root


def _replace_local_value(repo_root: Path, key: str, value: object) -> None:
    config_path = repo_root / "config" / "local.yaml"
    with config_path.open(encoding="utf-8") as file:
        local_config = yaml.safe_load(file)
    local_config[key] = value
    with config_path.open("w", encoding="utf-8") as file:
        yaml.safe_dump(local_config, file, allow_unicode=True)
