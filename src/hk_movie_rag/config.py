"""Contained configuration loading for the governed RAG data foundation."""

from __future__ import annotations

import os
import stat
from collections.abc import Mapping
from dataclasses import dataclass
from pathlib import Path
from typing import Any

import yaml


class ConfigError(ValueError):
    """Raised when project configuration is invalid or reaches outside the workspace."""


@dataclass(frozen=True)
class ExpectedCounts:
    release_movies: int
    quarantine_movies: int
    tier_counts: Mapping[str, int]
    pilot_movies: int
    audit_rows: int
    poster_states: Mapping[str, int]


@dataclass(frozen=True)
class GcpSettings:
    project_id: str
    region: str
    required_account: str
    archive_bucket_template: str
    archive_prefix: str
    required_services: tuple[str, ...]


@dataclass(frozen=True)
class PosterRoot:
    path: Path
    recursive: bool


@dataclass(frozen=True)
class Settings:
    repo_root: Path
    release_version: str
    source_manifest: Path
    source_validation: Path
    movies_csv: Path
    movies_xlsx: Path
    tiers_xlsx: Path
    issue_ledger_xlsx: Path
    inventory_roots: tuple[Path, ...]
    canonical_poster_roots: tuple[PosterRoot, ...]
    expected: ExpectedCounts
    gcp: GcpSettings

    @classmethod
    def load(cls, repo_root: Path) -> Settings:
        resolved_root = repo_root.resolve(strict=True)
        local = _load_yaml(resolved_root / "config" / "local.yaml")
        gcp = _load_yaml(resolved_root / "config" / "gcp.yaml")
        expected = _mapping(local, "expected")
        return cls(
            repo_root=resolved_root,
            release_version=_string(local, "release_version"),
            source_manifest=_resolve_input(resolved_root, _string(local, "source_manifest")),
            source_validation=_resolve_input(resolved_root, _string(local, "source_validation")),
            movies_csv=_resolve_input(resolved_root, _string(local, "movies_csv")),
            movies_xlsx=_resolve_input(resolved_root, _string(local, "movies_xlsx")),
            tiers_xlsx=_resolve_input(resolved_root, _string(local, "tiers_xlsx")),
            issue_ledger_xlsx=_resolve_input(resolved_root, _string(local, "issue_ledger_xlsx")),
            inventory_roots=tuple(
                _resolve_input(resolved_root, path, allow_missing=True)
                for path in _strings(local, "inventory_roots")
            ),
            canonical_poster_roots=tuple(
                PosterRoot(
                    path=_resolve_input(resolved_root, _string(root, "path")),
                    recursive=_boolean(root, "recursive"),
                )
                for root in _mappings(local, "canonical_poster_roots")
            ),
            expected=ExpectedCounts(
                release_movies=_integer(expected, "release_movies"),
                quarantine_movies=_integer(expected, "quarantine_movies"),
                tier_counts=_integer_mapping(expected, "tier_counts"),
                pilot_movies=_integer(expected, "pilot_movies"),
                audit_rows=_integer(expected, "audit_rows"),
                poster_states=_integer_mapping(expected, "poster_states"),
            ),
            gcp=GcpSettings(
                project_id=_string(gcp, "project_id"),
                region=_string(gcp, "region"),
                required_account=_string(gcp, "required_account"),
                archive_bucket_template=_string(gcp, "archive_bucket_template"),
                archive_prefix=_string(gcp, "archive_prefix"),
                required_services=tuple(_strings(gcp, "required_services")),
            ),
        )

    def resolve_input(self, relative_path: str) -> Path:
        return _resolve_input(self.repo_root, relative_path)


def _resolve_input(
    repo_root: Path,
    relative_path: str,
    *,
    allow_missing: bool = False,
) -> Path:
    candidate = Path(relative_path)
    if candidate.is_absolute() or ".." in candidate.parts:
        raise ConfigError("input path is outside workspace")
    lexical = Path(os.path.abspath(repo_root / candidate))
    if not lexical.is_relative_to(repo_root):
        raise ConfigError("input path is outside workspace")
    current = repo_root
    relative_parts = lexical.relative_to(repo_root).parts
    try:
        for index, part in enumerate(relative_parts):
            current = current / part
            current_stat = current.lstat()
            if _is_reparse(current_stat):
                raise ConfigError(
                    f"configured input violates no-follow containment: {relative_path}"
                )
            if index < len(relative_parts) - 1 and not stat.S_ISDIR(current_stat.st_mode):
                raise ConfigError(f"configured input is missing: {relative_path}")
    except FileNotFoundError as exc:
        if allow_missing:
            return lexical
        raise ConfigError(f"configured input is missing: {relative_path}") from exc
    except OSError as exc:
        raise ConfigError(f"configured input is unreadable: {relative_path}") from exc
    try:
        resolved = lexical.resolve(strict=True)
    except OSError as exc:
        raise ConfigError(f"configured input is unreadable: {relative_path}") from exc
    if not resolved.is_relative_to(repo_root):
        raise ConfigError("input path is outside workspace")
    return lexical


def _load_yaml(path: Path) -> Mapping[str, Any]:
    try:
        with path.open(encoding="utf-8") as file:
            content = yaml.safe_load(file)
    except yaml.YAMLError as exc:
        raise ConfigError(f"configuration YAML is invalid: {path}") from exc
    except OSError as exc:
        raise ConfigError(f"configuration file is missing: {path}") from exc
    if not isinstance(content, dict):
        raise ConfigError(f"configuration must be a mapping: {path}")
    return content


def _mapping(data: Mapping[str, Any], key: str) -> Mapping[str, Any]:
    value = data.get(key)
    if not isinstance(value, dict):
        raise ConfigError(f"configuration key must be a mapping: {key}")
    return value


def _mappings(data: Mapping[str, Any], key: str) -> list[Mapping[str, Any]]:
    value = data.get(key)
    if not isinstance(value, list) or not all(isinstance(item, dict) for item in value):
        raise ConfigError(f"configuration key must be a list of mappings: {key}")
    return value


def _strings(data: Mapping[str, Any], key: str) -> list[str]:
    value = data.get(key)
    if not isinstance(value, list) or not all(isinstance(item, str) for item in value):
        raise ConfigError(f"configuration key must be a list of strings: {key}")
    return value


def _string(data: Mapping[str, Any], key: str) -> str:
    value = data.get(key)
    if not isinstance(value, str) or not value:
        raise ConfigError(f"configuration key must be a non-empty string: {key}")
    return value


def _boolean(data: Mapping[str, Any], key: str) -> bool:
    value = data.get(key)
    if not isinstance(value, bool):
        raise ConfigError(f"configuration key must be a boolean: {key}")
    return value


def _integer(data: Mapping[str, Any], key: str) -> int:
    value = data.get(key)
    if not isinstance(value, int) or isinstance(value, bool):
        raise ConfigError(f"configuration key must be an integer: {key}")
    return value


def _integer_mapping(data: Mapping[str, Any], key: str) -> Mapping[str, int]:
    value = _mapping(data, key)
    if not all(isinstance(name, str) and isinstance(count, int) and not isinstance(count, bool)
               for name, count in value.items()):
        raise ConfigError(f"configuration key must map strings to integers: {key}")
    return value


def _is_reparse(path_stat: os.stat_result) -> bool:
    attributes = getattr(path_stat, "st_file_attributes", 0)
    return stat.S_ISLNK(path_stat.st_mode) or bool(
        attributes & getattr(stat, "FILE_ATTRIBUTE_REPARSE_POINT", 0)
    )
