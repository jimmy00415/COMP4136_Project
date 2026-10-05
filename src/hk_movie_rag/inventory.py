"""Deterministic inventory and duplicate-mirror validation for source inputs."""

from __future__ import annotations

import json
from collections import Counter
from collections.abc import Sequence
from dataclasses import dataclass
from pathlib import Path
from typing import Literal

from pydantic import BaseModel, ConfigDict

from .config import Settings
from .hashing import sha256_file

Classification = Literal[
    "formal_release_source",
    "governance_evidence",
    "issue_ledger",
    "poster_candidate",
    "duplicate_candidate",
    "macos_metadata",
]

_GOVERNANCE_FILES = frozenset({"manifest_v1.2.json", "validation_report_v1.2.json"})
_ISSUE_LEDGER_FILE = "待補全問題台帳_v1.2.xlsx"
_NESTED_MIRROR = "1-1500posters/1-1500posters/"
_OUTER_MIRROR = "1-1500posters/"
_POSTERS1_MIRROR = "posters1/"
_CANONICAL_POSTERS = "posters/posters/"
_EXPECTED_MIRROR_COUNTS = {
    "nested_1-1500posters": 1497,
    "posters1": 1500,
}


class InventoryError(ValueError):
    """Raised when source inventory or mirror evidence is incomplete."""


class InventoryEntry(BaseModel):
    """Immutable evidence for one source file."""

    model_config = ConfigDict(frozen=True)

    relative_path: str
    size_bytes: int
    sha256: str
    classification: Classification


class MirrorPairProof(BaseModel):
    """Evidence that a duplicate tree exactly matches its canonical counterpart."""

    duplicate_root: str
    canonical_root: str
    file_count: int


class MirrorProof(BaseModel):
    """All duplicate-mirror checks performed for an inventory."""

    pairs: tuple[MirrorPairProof, ...]


class InventoryEntries(tuple[InventoryEntry, ...]):
    """Tuple-compatible entries with the legacy ``.entries`` view used by the plan."""

    @property
    def entries(self) -> InventoryEntries:
        return self


@dataclass(frozen=True)
class InventoryResult:
    entries: InventoryEntries
    source_roots: tuple[str, ...]
    duplicate_proof: MirrorProof


def inventory_paths(repo_root: Path, roots: Sequence[Path]) -> InventoryEntries:
    """Hash every regular file under explicit, contained source roots in stable order."""
    resolved_repo = repo_root.resolve(strict=True)
    files: list[InventoryEntry] = []
    for root in roots:
        resolved_root = _resolve_root(resolved_repo, root)
        for path in sorted(resolved_root.rglob("*"), key=lambda item: item.as_posix()):
            if not path.is_file():
                continue
            resolved_path = path.resolve(strict=True)
            if not resolved_path.is_relative_to(resolved_repo):
                raise InventoryError(f"source path is outside workspace: {path}")
            relative_path = resolved_path.relative_to(resolved_repo).as_posix()
            files.append(
                InventoryEntry(
                    relative_path=relative_path,
                    size_bytes=resolved_path.stat().st_size,
                    sha256=sha256_file(resolved_path),
                    classification=_classify(relative_path),
                )
            )
    return InventoryEntries(sorted(files, key=lambda entry: entry.relative_path))


def build_inventory(settings: Settings) -> InventoryResult:
    """Build source evidence and enforce the v1.2 duplicate-mirror cardinalities."""
    entries = inventory_paths(settings.repo_root, settings.inventory_roots)
    proof = prove_mirrors(entries)
    actual_counts = {pair.duplicate_root: pair.file_count for pair in proof.pairs}
    for duplicate_root, expected_count in _EXPECTED_MIRROR_COUNTS.items():
        if actual_counts.get(duplicate_root) != expected_count:
            raise InventoryError(
                f"mirror mismatch: {duplicate_root} files={actual_counts.get(duplicate_root)} "
                f"expected={expected_count}"
            )
    return InventoryResult(
        entries=entries,
        source_roots=tuple(root.relative_to(settings.repo_root).as_posix() for root in settings.inventory_roots),
        duplicate_proof=proof,
    )


def prove_mirrors(entries: Sequence[InventoryEntry]) -> MirrorProof:
    """Prove both duplicate trees agree on relative name, byte length, and SHA-256."""
    by_path = {entry.relative_path: entry for entry in entries}
    nested = _entries_below(entries, _NESTED_MIRROR)
    outer = _direct_entries(entries, _OUTER_MIRROR)
    posters1 = _entries_below(entries, _POSTERS1_MIRROR)
    canonical = _entries_below(entries, _CANONICAL_POSTERS)
    _prove_pair(nested, outer, "nested_1-1500posters", "1-1500posters")
    _prove_subset(posters1, canonical, "posters1", "posters/posters")
    _require_expected_count("nested_1-1500posters", len(nested))
    _require_expected_count("posters1", len(posters1))
    if len(by_path) != len(entries):
        raise InventoryError("inventory contains duplicate relative paths")
    return MirrorProof(
        pairs=(
            MirrorPairProof(
                duplicate_root="nested_1-1500posters",
                canonical_root="1-1500posters",
                file_count=len(nested),
            ),
            MirrorPairProof(
                duplicate_root="posters1",
                canonical_root="posters/posters",
                file_count=len(posters1),
            ),
        )
    )


def write_inventory(result: InventoryResult, output_dir: Path) -> None:
    """Write deterministic inventory, summary, and duplicate-proof artifacts."""
    output_dir.mkdir(parents=True, exist_ok=True)
    inventory_path = output_dir / "source_inventory.jsonl"
    with inventory_path.open("w", encoding="utf-8", newline="\n") as output:
        for entry in result.entries:
            output.write(_compact_json(entry.model_dump()))
            output.write("\n")
    summary = {
        "classification_counts": dict(sorted(Counter(entry.classification for entry in result.entries).items())),
        "duplicate_proof": result.duplicate_proof.model_dump(mode="json"),
        "file_count": len(result.entries),
        "inventory_sha256": sha256_file(inventory_path),
        "source_roots": list(result.source_roots),
        "total_bytes": sum(entry.size_bytes for entry in result.entries),
    }
    (output_dir / "source_inventory.summary.json").write_text(
        _compact_json(summary) + "\n", encoding="utf-8", newline="\n"
    )
    (output_dir / "duplicate_proof.json").write_text(
        _compact_json(result.duplicate_proof.model_dump()) + "\n", encoding="utf-8", newline="\n"
    )


def _resolve_root(repo_root: Path, root: Path) -> Path:
    candidate = root if root.is_absolute() else repo_root / root
    try:
        resolved = candidate.resolve(strict=True)
    except FileNotFoundError as exc:
        raise InventoryError(f"configured inventory root is missing: {root}") from exc
    if not resolved.is_dir() or not resolved.is_relative_to(repo_root):
        raise InventoryError(f"configured inventory root is outside workspace: {root}")
    return resolved


def _classify(relative_path: str) -> Classification:
    parts = Path(relative_path).parts
    filename = parts[-1]
    if filename == ".DS_Store" or "__MACOSX" in parts:
        return "macos_metadata"
    if relative_path.startswith((_NESTED_MIRROR, _POSTERS1_MIRROR)):
        return "duplicate_candidate"
    if parts[0] == "FinalDelivery_2026-07-16":
        if filename == _ISSUE_LEDGER_FILE:
            return "issue_ledger"
        if filename in _GOVERNANCE_FILES:
            return "governance_evidence"
        return "formal_release_source"
    if parts[0] in {"1-1500posters", "posters"}:
        return "poster_candidate"
    return "formal_release_source"


def _entries_below(entries: Sequence[InventoryEntry], prefix: str) -> dict[str, InventoryEntry]:
    return {
        entry.relative_path.removeprefix(prefix): entry
        for entry in entries
        if entry.relative_path.startswith(prefix)
    }


def _direct_entries(entries: Sequence[InventoryEntry], prefix: str) -> dict[str, InventoryEntry]:
    return {
        entry.relative_path.removeprefix(prefix): entry
        for entry in entries
        if entry.relative_path.startswith(prefix)
        and "/" not in entry.relative_path.removeprefix(prefix)
    }


def _prove_pair(
    duplicate: dict[str, InventoryEntry],
    canonical: dict[str, InventoryEntry],
    duplicate_root: str,
    canonical_root: str,
) -> None:
    if duplicate.keys() != canonical.keys():
        raise InventoryError(f"mirror mismatch: {duplicate_root} vs {canonical_root} relative paths")
    _prove_subset(duplicate, canonical, duplicate_root, canonical_root)


def _require_expected_count(duplicate_root: str, actual_count: int) -> None:
    expected_count = _EXPECTED_MIRROR_COUNTS[duplicate_root]
    if actual_count != expected_count:
        raise InventoryError(
            f"mirror mismatch: {duplicate_root} files={actual_count} expected={expected_count}"
        )


def _prove_subset(
    duplicate: dict[str, InventoryEntry],
    canonical: dict[str, InventoryEntry],
    duplicate_root: str,
    canonical_root: str,
) -> None:
    for relative_path, duplicate_entry in duplicate.items():
        canonical_entry = canonical.get(relative_path)
        if canonical_entry is None or (
            duplicate_entry.size_bytes != canonical_entry.size_bytes
            or duplicate_entry.sha256 != canonical_entry.sha256
        ):
            raise InventoryError(
                f"mirror mismatch: {duplicate_root} vs {canonical_root} at {relative_path}"
            )


def _compact_json(value: object) -> str:
    return json.dumps(value, ensure_ascii=False, separators=(",", ":"), sort_keys=True)
