import hashlib
import json
import subprocess
import sys
from pathlib import Path

import pytest
import yaml

from hk_movie_rag.inventory import (
    InventoryEntries,
    InventoryEntry,
    InventoryError,
    InventoryResult,
    MirrorPairProof,
    MirrorProof,
    inventory_paths,
    prove_mirrors,
    write_inventory,
)


def test_inventory_is_sorted_and_hashes_bytes(tmp_path: Path) -> None:
    """Breaks if inventory order or digest is derived from names rather than file bytes."""
    (tmp_path / "root").mkdir()
    (tmp_path / "root" / "b.bin").write_bytes(b"b")
    (tmp_path / "root" / "a.bin").write_bytes(b"a")

    entries = inventory_paths(tmp_path, [Path("root")])

    assert [entry.relative_path for entry in entries] == ["root/a.bin", "root/b.bin"]
    assert entries[0].sha256 == hashlib.sha256(b"a").hexdigest()
    assert entries[0].size_bytes == 1


def test_mirror_proof_fails_on_one_changed_byte(tmp_path: Path) -> None:
    """Breaks if mirror validation compares names or sizes without checking bytes."""
    _write_mirror_fixture(tmp_path, changed=True)
    entries = inventory_paths(
        tmp_path,
        [
            Path("1-1500posters"),
            Path("posters"),
            Path("posters1"),
        ],
    )

    with pytest.raises(InventoryError, match="mirror mismatch"):
        prove_mirrors(entries)


def test_mirror_proof_rejects_extra_or_missing_relative_paths(tmp_path: Path) -> None:
    """Breaks if a mirror pair can pass when either side has a different file set."""
    _write_mirror_fixture(tmp_path)
    (tmp_path / "posters1" / "extra.jpg").write_bytes(b"extra")
    entries = inventory_paths(
        tmp_path,
        [
            Path("1-1500posters"),
            Path("posters"),
            Path("posters1"),
        ],
    )

    with pytest.raises(InventoryError, match="mirror mismatch"):
        prove_mirrors(entries)


@pytest.mark.parametrize(
    "make_entries",
    [
        lambda tmp_path: InventoryEntries(),
        lambda tmp_path: inventory_paths(
            tmp_path,
            [Path("1-1500posters"), Path("posters"), Path("posters1")],
        ),
    ],
    ids=["empty", "short-but-equal"],
)
def test_mirror_proof_requires_exact_v12_cardinalities(tmp_path: Path, make_entries: object) -> None:
    """Breaks if absent or short but matching mirror trees can receive a successful proof."""
    _write_mirror_fixture(tmp_path)

    with pytest.raises(InventoryError, match="expected=1497"):
        prove_mirrors(make_entries(tmp_path))  # type: ignore[operator]


@pytest.mark.parametrize(
    "output_path",
    [
        "inputs/raw/inventory",
        "inputs/one/inventory",
        "inputs/posters/inventory",
        "inputs/posters1/inventory",
        "raw-alias/inventory",
    ],
)
def test_inventory_cli_rejects_output_under_every_inventory_root(
    tmp_path: Path, output_path: str
) -> None:
    """Breaks if any protected root or its resolved alias accepts generated output."""
    repo_root, protected_root = _write_cli_repo(tmp_path)
    alias = repo_root / "raw-alias"
    _make_directory_link(alias, protected_root)

    result = subprocess.run(
        [
            sys.executable,
            "-c",
            "from hk_movie_rag.cli import main; main()",
            "inventory",
            "--output",
            output_path,
        ],
        capture_output=True,
        cwd=repo_root,
        encoding="utf-8",
        errors="replace",
        text=True,
        check=False,
    )

    assert result.returncode != 0
    assert "protected source root" in result.stderr
    assert not (repo_root / output_path / "source_inventory.jsonl").exists()


def test_inventory_summary_contains_duplicate_proof_records(tmp_path: Path) -> None:
    """Breaks if the summary cannot itself evidence both duplicate-mirror checks."""
    proof = MirrorProof(
        pairs=(
            MirrorPairProof(
                duplicate_root="nested_1-1500posters",
                canonical_root="1-1500posters",
                file_count=1497,
            ),
            MirrorPairProof(
                duplicate_root="posters1",
                canonical_root="posters/posters",
                file_count=1500,
            ),
        )
    )
    result = InventoryResult(
        entries=InventoryEntries(
            [
                InventoryEntry(
                    relative_path="FinalDelivery_2026-07-16/manifest_v1.2.json",
                    size_bytes=1,
                    sha256=hashlib.sha256(b"x").hexdigest(),
                    classification="governance_evidence",
                )
            ]
        ),
        source_roots=("FinalDelivery_2026-07-16",),
        duplicate_proof=proof,
    )

    write_inventory(result, tmp_path / "artifacts")

    summary = json.loads((tmp_path / "artifacts" / "source_inventory.summary.json").read_text())
    assert summary["duplicate_proof"] == proof.model_dump(mode="json")


def _write_mirror_fixture(tmp_path: Path, *, changed: bool = False) -> None:
    outer = tmp_path / "1-1500posters"
    nested = outer / "1-1500posters"
    canonical = tmp_path / "posters" / "posters"
    duplicate = tmp_path / "posters1"
    for directory in (outer, nested, canonical, duplicate):
        directory.mkdir(parents=True, exist_ok=True)
    (outer / "outer.jpg").write_bytes(b"outer")
    (nested / "outer.jpg").write_bytes(b"outer" if not changed else b"otter")
    (canonical / "canonical.jpg").write_bytes(b"canonical")
    (duplicate / "canonical.jpg").write_bytes(b"canonical")


def _write_cli_repo(tmp_path: Path) -> tuple[Path, Path]:
    repo_root = tmp_path / "repo"
    protected_root = repo_root / "inputs" / "raw"
    local_config = {
        "release_version": "v1.2",
        "source_manifest": "inputs/raw/manifest.json",
        "source_validation": "inputs/raw/validation.json",
        "movies_csv": "inputs/raw/movies.csv",
        "movies_xlsx": "inputs/raw/movies.xlsx",
        "tiers_xlsx": "inputs/raw/tiers.xlsx",
        "issue_ledger_xlsx": "inputs/raw/issues.xlsx",
        "inventory_roots": ["inputs/raw", "inputs/one", "inputs/posters", "inputs/posters1"],
        "canonical_poster_roots": [
            {"path": "inputs/one", "recursive": False},
            {"path": "inputs/posters/posters", "recursive": False},
        ],
        "expected": {
            "release_movies": 1,
            "quarantine_movies": 0,
            "tier_counts": {"S": 1},
            "pilot_movies": 1,
            "audit_rows": 1,
            "poster_states": {"machine_passed": 1},
        },
    }
    gcp_config = {
        "project_id": "test-project",
        "region": "test-region",
        "required_account": "test@example.com",
        "archive_bucket_template": "{project_id}-archive",
        "archive_prefix": "source/v1.2",
        "required_services": ["storage.googleapis.com"],
    }
    (repo_root / "config").mkdir(parents=True)
    (repo_root / "config" / "local.yaml").write_text(yaml.safe_dump(local_config), encoding="utf-8")
    (repo_root / "config" / "gcp.yaml").write_text(yaml.safe_dump(gcp_config), encoding="utf-8")
    protected_root.mkdir(parents=True)
    for filename in ("manifest.json", "validation.json", "movies.csv", "movies.xlsx", "tiers.xlsx", "issues.xlsx"):
        (protected_root / filename).touch()
    (repo_root / "inputs" / "one").mkdir()
    (repo_root / "inputs" / "posters" / "posters").mkdir(parents=True)
    (repo_root / "inputs" / "posters1").mkdir()
    return repo_root, protected_root


def _make_directory_link(link: Path, target: Path) -> None:
    try:
        link.symlink_to(target, target_is_directory=True)
    except OSError:
        result = subprocess.run(
            ["cmd", "/c", "mklink", "/J", str(link), str(target)],
            capture_output=True,
            check=False,
            encoding="utf-8",
            errors="replace",
            text=True,
        )
        assert result.returncode == 0, result.stderr
