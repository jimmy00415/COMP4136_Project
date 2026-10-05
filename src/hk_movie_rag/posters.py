"""Exact-key poster reconciliation and deterministic media materialization."""

from __future__ import annotations

import hashlib
import io
import os
import secrets
import stat
from collections import Counter, defaultdict
from collections.abc import Callable, Iterable, Iterator, Sequence
from contextlib import contextmanager
from dataclasses import dataclass, replace
from pathlib import Path
from tempfile import NamedTemporaryFile
from typing import BinaryIO

import pyarrow as pa
import pyarrow.parquet as pq
from PIL import Image, ImageOps, UnidentifiedImageError

from .config import Settings
from .release_lock import release_guard

_MIME_BY_FORMAT = {
    "JPEG": ("image/jpeg", ".jpg"),
    "PNG": ("image/png", ".png"),
    "WEBP": ("image/webp", ".webp"),
}
_METADATA_FILENAMES = {".DS_Store"}


class PosterError(ValueError):
    """Raised when poster evidence violates the release contract."""


@dataclass(frozen=True)
class PosterRow:
    movie_id: str
    source_path: Path | None
    detected_format: str
    detected_mime_type: str
    detected_extension: str
    width: int | None
    height: int | None
    mode: str
    byte_length: int | None
    content_sha256: str
    quality_status: str = "machine_passed"
    rights_status: str = "unknown"
    is_primary: bool = False
    publishable: bool = False


@dataclass(frozen=True)
class PosterScanResult:
    release_version: str
    rows: tuple[PosterRow, ...]
    present_keys: int
    orphan_keys: frozenset[str]
    detected_formats: dict[str, int]


@dataclass(frozen=True)
class PosterBuildResult:
    manifest_path: Path
    state_counts: dict[str, int]
    present_keys: int
    orphan_keys: frozenset[str]
    detected_formats: dict[str, int]


@dataclass(frozen=True)
class _DirectoryIdentity:
    path: Path
    device: int
    inode: int


@dataclass(frozen=True)
class _AncestorGuard:
    identities: tuple[_DirectoryIdentity, ...]
    parent_handle: int | None
    lock_handles: list[int]


def inspect_image(path: Path, movie_id: str) -> PosterRow:
    """Decode *path* twice and describe its content rather than its suffix."""
    try:
        source_stat = path.lstat()
    except OSError as exc:
        raise PosterError(f"poster is unreadable: {path.name}") from exc
    if not stat.S_ISREG(source_stat.st_mode) or _is_reparse(source_stat):
        raise PosterError(f"poster candidate is not a direct regular file: {path.name}")
    try:
        content = path.read_bytes()
        with Image.open(io.BytesIO(content)) as image:
            image.verify()
        with Image.open(io.BytesIO(content)) as image:
            image.load()
            detected_format = image.format
            width, height = image.size
            mode = image.mode
    except (OSError, UnidentifiedImageError, ValueError) as exc:
        raise PosterError(f"poster image is invalid: {path.name}") from exc
    if detected_format not in _MIME_BY_FORMAT:
        raise PosterError(f"unsupported poster format: {detected_format}")
    mime_type, extension = _MIME_BY_FORMAT[detected_format]
    return PosterRow(
        movie_id=movie_id,
        source_path=path,
        detected_format=detected_format,
        detected_mime_type=mime_type,
        detected_extension=extension,
        width=width,
        height=height,
        mode=mode,
        byte_length=len(content),
        content_sha256=hashlib.sha256(content).hexdigest(),
    )


def scan_candidate(path: Path, release_ids: set[str]) -> PosterRow:
    """Join one candidate filename stem to one exact Release identifier."""
    movie_id = path.stem
    if movie_id not in release_ids:
        raise PosterError(f"orphan poster key: {movie_id}")
    return inspect_image(path, movie_id)


def classify_hash_groups(candidates: Iterable[PosterRow]) -> tuple[PosterRow, ...]:
    """Quarantine cross-key repeats and recognize only the exact placeholder group."""
    rows = tuple(candidates)
    groups: dict[str, list[PosterRow]] = defaultdict(list)
    for row in rows:
        if row.source_path is None or not row.content_sha256:
            raise PosterError("hash classification requires present poster candidates")
        groups[row.content_sha256].append(row)

    classified: list[PosterRow] = []
    for content_hash in sorted(groups):
        group = groups[content_hash]
        keys = {row.movie_id for row in group}
        dimensions = {(row.width, row.height) for row in group}
        if len(keys) > 1 and dimensions == {(65, 91)}:
            if len(keys) != 9:
                raise PosterError("65x91 repeated hash group must contain exactly 9 keys")
            quality_status = "placeholder"
        elif len(keys) > 1:
            quality_status = "content_conflict"
        else:
            quality_status = "machine_passed"
        classified.extend(replace(row, quality_status=quality_status) for row in group)
    return tuple(sorted(classified, key=lambda row: (row.movie_id, str(row.source_path))))


def scan_posters(settings: Settings, movie_ids: set[str]) -> PosterScanResult:
    """Inspect only configured direct files and return one state row per Release ID."""
    if not movie_ids or "" in movie_ids:
        raise PosterError("Release movie IDs must be non-empty")
    candidates: list[PosterRow] = []
    for configured_root in settings.canonical_poster_roots:
        if configured_root.recursive:
            raise PosterError("canonical poster roots must be non-recursive")
        root = configured_root.path
        try:
            root_stat = root.lstat()
        except OSError as exc:
            raise PosterError(f"canonical poster root is unreadable: {root}") from exc
        if not stat.S_ISDIR(root_stat.st_mode) or _is_reparse(root_stat):
            raise PosterError(f"canonical poster root is not a direct directory: {root}")
        try:
            with os.scandir(root) as iterator:
                entries = sorted(iterator, key=lambda entry: entry.name)
        except OSError as exc:
            raise PosterError(f"canonical poster root is unreadable: {root}") from exc
        for entry in entries:
            if entry.name in _METADATA_FILENAMES:
                continue
            try:
                if not entry.is_file(follow_symlinks=False):
                    continue
                entry_stat = entry.stat(follow_symlinks=False)
            except OSError as exc:
                raise PosterError(f"poster candidate is unreadable: {entry.name}") from exc
            if _is_reparse(entry_stat):
                raise PosterError(
                    f"poster candidate is not a direct regular file: {entry.name}"
                )
            candidates.append(scan_candidate(Path(entry.path), movie_ids))

    candidates_by_key: dict[str, list[PosterRow]] = defaultdict(list)
    for candidate in candidates:
        candidates_by_key[candidate.movie_id].append(candidate)
    unique_candidates: list[PosterRow] = []
    for movie_id in sorted(candidates_by_key):
        same_key = candidates_by_key[movie_id]
        hashes = {candidate.content_sha256 for candidate in same_key}
        if len(hashes) != 1:
            raise PosterError(f"same poster key has different content: {movie_id}")
        unique_candidates.append(
            min(same_key, key=lambda candidate: str(candidate.source_path))
        )

    classified = classify_hash_groups(unique_candidates)
    present_ids = {row.movie_id for row in classified}
    missing = tuple(
        PosterRow(
            movie_id=movie_id,
            source_path=None,
            detected_format="",
            detected_mime_type="",
            detected_extension="",
            width=None,
            height=None,
            mode="",
            byte_length=None,
            content_sha256="",
            quality_status="missing",
        )
        for movie_id in sorted(movie_ids - present_ids)
    )
    rows = tuple(sorted((*classified, *missing), key=lambda row: row.movie_id))
    if len(rows) != len(movie_ids) or {row.movie_id for row in rows} != movie_ids:
        raise PosterError("poster manifest must contain exactly one row per Release movie")
    return PosterScanResult(
        release_version=settings.release_version,
        rows=rows,
        present_keys=len(present_ids),
        orphan_keys=frozenset(),
        detected_formats=dict(
            sorted(Counter(row.detected_format for row in classified).items())
        ),
    )


def materialize_posters(scan: PosterScanResult, output_root: Path) -> PosterBuildResult:
    """Atomically write governed poster outputs below *output_root*."""
    root = _safe_output_root(output_root)
    with release_guard(root, scan.release_version, exclusive=True):
        return _materialize_posters_locked(scan, root)


def _materialize_posters_locked(
    scan: PosterScanResult, root: Path
) -> PosterBuildResult:
    """Materialize while holding the Release version's publication lock."""
    expected_files, expected_directories = _expected_generated_paths(scan)
    _assert_generated_tree_compatible(root, expected_files, expected_directories)
    release_dir = _safe_directory(root, Path("data") / "release" / scan.release_version)
    originals_dir = _safe_directory(root, Path("assets") / "posters" / "originals")
    derived_dir = _safe_directory(root, Path("assets") / "posters" / "derived")
    _safe_directory(
        root, Path("data") / "quarantine" / "poster_conflicts"
    )

    manifest_rows: list[dict[str, object]] = []
    for row in scan.rows:
        original_uri = ""
        derived_uri = ""
        quarantine_uri = ""
        if row.quality_status == "machine_passed":
            assert row.source_path is not None
            original_relative = (
                Path("assets")
                / "posters"
                / "originals"
                / f"{row.movie_id}{row.detected_extension}"
            )
            derived_relative = (
                Path("assets") / "posters" / "derived" / f"{row.movie_id}.webp"
            )
            content = _verified_source_bytes(row)
            _atomic_replace_bytes(
                root,
                _safe_destination(root, originals_dir, original_relative.name),
                content,
            )
            _atomic_replace_bytes(
                root,
                _safe_destination(root, derived_dir, derived_relative.name),
                _webp_derivative(content, row),
            )
            original_uri = original_relative.as_posix()
            derived_uri = derived_relative.as_posix()
        elif row.quality_status == "content_conflict":
            assert row.source_path is not None
            hash_relative = (
                Path("data")
                / "quarantine"
                / "poster_conflicts"
                / row.content_sha256
            )
            hash_dir = _safe_directory(root, hash_relative)
            quarantine_relative = hash_relative / f"{row.movie_id}{row.detected_extension}"
            _atomic_replace_bytes(
                root,
                _safe_destination(
                    root,
                    hash_dir,
                    f"{row.movie_id}{row.detected_extension}",
                ),
                _verified_source_bytes(row),
            )
            quarantine_uri = quarantine_relative.as_posix()
        elif row.quality_status not in {"placeholder", "missing"}:
            raise PosterError(f"unsupported poster quality state: {row.quality_status}")

        manifest_rows.append(
            {
                "asset_id": f"poster:{row.movie_id}:v1",
                "movie_id": row.movie_id,
                "asset_type": "poster",
                "source_object_uri": row.source_path.name if row.source_path else "",
                "original_object_uri": original_uri,
                "derived_object_uri": derived_uri,
                "quarantine_object_uri": quarantine_uri,
                "content_sha256": row.content_sha256,
                "detected_format": row.detected_format,
                "detected_mime_type": row.detected_mime_type,
                "detected_extension": row.detected_extension,
                "width": row.width,
                "height": row.height,
                "color_mode": row.mode,
                "byte_length": row.byte_length,
                "quality_status": row.quality_status,
                "identity_confidence": None,
                "source_url": None,
                "rights_status": row.rights_status,
                "version": 1,
                "is_primary": row.is_primary,
                "publishable": row.publishable,
                "created_at": None,
                "reviewed_at": None,
            }
        )

    manifest_path = _safe_destination(root, release_dir, "poster_manifest.parquet")
    _write_manifest(root, manifest_path, manifest_rows)
    return PosterBuildResult(
        manifest_path=manifest_path,
        state_counts=dict(
            Counter(row.quality_status for row in scan.rows)
        ),
        present_keys=scan.present_keys,
        orphan_keys=scan.orphan_keys,
        detected_formats=scan.detected_formats,
    )


def build_posters(
    settings: Settings, movie_ids: set[str], output_root: Path
) -> PosterBuildResult:
    """Reconcile canonical poster roots and materialize governed local outputs."""
    return materialize_posters(scan_posters(settings, movie_ids), output_root)


def load_release_ids(path: Path) -> set[str]:
    """Load and validate unique canonical IDs from a Release movies Parquet file."""
    try:
        table = pq.read_table(path, columns=["movie_id"])
    except (OSError, pa.ArrowException) as exc:
        raise PosterError("Release movie IDs are unreadable") from exc
    values = table.column("movie_id").to_pylist()
    if not all(isinstance(value, str) and value for value in values):
        raise PosterError("Release movie IDs must be non-empty strings")
    movie_ids = set(values)
    if len(movie_ids) != len(values):
        raise PosterError("Release movie IDs must be unique")
    return movie_ids


def _verified_source_bytes(row: PosterRow) -> bytes:
    assert row.source_path is not None
    try:
        source_stat = row.source_path.lstat()
        if not stat.S_ISREG(source_stat.st_mode) or _is_reparse(source_stat):
            raise PosterError(f"poster source changed after scan: {row.movie_id}")
        content = row.source_path.read_bytes()
    except OSError as exc:
        raise PosterError(f"poster source changed after scan: {row.movie_id}") from exc
    if (
        len(content) != row.byte_length
        or hashlib.sha256(content).hexdigest() != row.content_sha256
    ):
        raise PosterError(f"poster source changed after scan: {row.movie_id}")
    return content


def _webp_derivative(content: bytes, row: PosterRow) -> bytes:
    try:
        with Image.open(io.BytesIO(content)) as image:
            image.load()
            normalized = ImageOps.exif_transpose(image).convert("RGB")
            destination = io.BytesIO()
            normalized.save(
                destination,
                format="WEBP",
                quality=90,
                method=6,
                exif=b"",
            )
    except (OSError, UnidentifiedImageError, ValueError) as exc:
        raise PosterError(f"cannot derive poster: {row.movie_id}") from exc
    return destination.getvalue()


def _write_manifest(
    root: Path, path: Path, rows: Sequence[dict[str, object]]
) -> None:
    schema = pa.schema(
        [
            ("asset_id", pa.string()),
            ("movie_id", pa.string()),
            ("asset_type", pa.string()),
            ("source_object_uri", pa.string()),
            ("original_object_uri", pa.string()),
            ("derived_object_uri", pa.string()),
            ("quarantine_object_uri", pa.string()),
            ("content_sha256", pa.string()),
            ("detected_format", pa.string()),
            ("detected_mime_type", pa.string()),
            ("detected_extension", pa.string()),
            ("width", pa.int32()),
            ("height", pa.int32()),
            ("color_mode", pa.string()),
            ("byte_length", pa.int64()),
            ("quality_status", pa.string()),
            ("identity_confidence", pa.float64()),
            ("source_url", pa.string()),
            ("rights_status", pa.string()),
            ("version", pa.int32()),
            ("is_primary", pa.bool_()),
            ("publishable", pa.bool_()),
            ("created_at", pa.string()),
            ("reviewed_at", pa.string()),
        ]
    )
    table = pa.Table.from_pylist(list(rows), schema=schema)
    sink = pa.BufferOutputStream()
    pq.write_table(
        table,
        sink,
        compression="zstd",
        version="2.6",
        data_page_version="2.0",
        use_dictionary=False,
        write_statistics=True,
    )
    _atomic_replace_bytes(root, path, sink.getvalue().to_pybytes())


def _expected_generated_paths(
    scan: PosterScanResult,
) -> tuple[frozenset[Path], frozenset[Path]]:
    files: set[Path] = set()
    directories = {
        Path("assets/posters"),
        Path("assets/posters/originals"),
        Path("assets/posters/derived"),
        Path("data/quarantine/poster_conflicts"),
    }
    for row in scan.rows:
        if row.quality_status == "machine_passed":
            files.add(
                Path("assets/posters/originals")
                / _safe_generated_filename(row.movie_id, row.detected_extension)
            )
            files.add(
                Path("assets/posters/derived")
                / _safe_generated_filename(row.movie_id, ".webp")
            )
        elif row.quality_status == "content_conflict":
            if not _is_sha256(row.content_sha256):
                raise PosterError("conflict poster hash is invalid")
            hash_directory = (
                Path("data/quarantine/poster_conflicts") / row.content_sha256
            )
            directories.add(hash_directory)
            files.add(
                hash_directory
                / _safe_generated_filename(row.movie_id, row.detected_extension)
            )
        elif row.quality_status not in {"placeholder", "missing"}:
            raise PosterError(f"unsupported poster quality state: {row.quality_status}")
    return frozenset(files), frozenset(directories)


def _safe_generated_filename(movie_id: str, extension: str) -> str:
    filename = f"{movie_id}{extension}"
    if Path(filename).name != filename or filename in {"", ".", ".."}:
        raise PosterError("output filename is unsafe")
    return filename


def _is_sha256(value: str) -> bool:
    return len(value) == 64 and all(character in "0123456789abcdef" for character in value)


def _assert_generated_tree_compatible(
    root: Path,
    expected_files: frozenset[Path],
    expected_directories: frozenset[Path],
) -> None:
    for relative_tree in (
        Path("assets/posters"),
        Path("data/quarantine/poster_conflicts"),
    ):
        tree = root / relative_tree
        try:
            tree_stat = tree.lstat()
        except FileNotFoundError:
            continue
        except OSError as exc:
            raise PosterError("generated poster tree is unreadable") from exc
        if not stat.S_ISDIR(tree_stat.st_mode) or _is_reparse(tree_stat):
            raise PosterError("output path contains a followed link")
        _capture_ancestor_identities(root, tree)
        pending = [tree]
        while pending:
            directory = pending.pop()
            try:
                with os.scandir(directory) as iterator:
                    entries = sorted(iterator, key=lambda entry: entry.name)
            except OSError as exc:
                raise PosterError("generated poster tree is unreadable") from exc
            for entry in entries:
                path = Path(entry.path)
                relative = path.relative_to(root)
                try:
                    entry_stat = entry.stat(follow_symlinks=False)
                except OSError as exc:
                    raise PosterError("generated poster tree is unreadable") from exc
                if _is_reparse(entry_stat):
                    raise PosterError("output path contains a followed link")
                if stat.S_ISDIR(entry_stat.st_mode):
                    if relative not in expected_directories:
                        raise PosterError(f"stale generated poster path: {relative.as_posix()}")
                    pending.append(path)
                elif stat.S_ISREG(entry_stat.st_mode):
                    if relative not in expected_files:
                        raise PosterError(f"stale generated poster path: {relative.as_posix()}")
                else:
                    raise PosterError(f"stale generated poster path: {relative.as_posix()}")


def _safe_output_root(output_root: Path) -> Path:
    try:
        root_stat = output_root.lstat()
        root = output_root.resolve(strict=True)
    except OSError as exc:
        raise PosterError("output root must be an existing directory") from exc
    if not stat.S_ISDIR(root_stat.st_mode) or _is_reparse(root_stat):
        raise PosterError("output root must be a no-follow directory")
    return root


def _safe_directory(root: Path, relative: Path) -> Path:
    if relative.is_absolute() or ".." in relative.parts:
        raise PosterError("output path is outside output root")
    current = root
    for part in relative.parts:
        current = current / part
        if current.exists():
            current_stat = current.lstat()
            if not stat.S_ISDIR(current_stat.st_mode) or _is_reparse(current_stat):
                raise PosterError("output path contains a followed link")
        else:
            with _locked_output_ancestors(root, current.parent):
                try:
                    current.mkdir()
                except OSError as exc:
                    raise PosterError(f"cannot create output directory: {relative}") from exc
                current_stat = current.lstat()
                if not stat.S_ISDIR(current_stat.st_mode) or _is_reparse(current_stat):
                    raise PosterError("output path contains a followed link")
    resolved = current.resolve(strict=True)
    if not resolved.is_relative_to(root):
        raise PosterError("output path is outside output root")
    return resolved


def _safe_destination(root: Path, parent: Path, filename: str) -> Path:
    if Path(filename).name != filename or filename in {"", ".", ".."}:
        raise PosterError("output filename is unsafe")
    parent_stat = parent.lstat()
    if not stat.S_ISDIR(parent_stat.st_mode) or _is_reparse(parent_stat):
        raise PosterError("output path contains a followed link")
    destination = parent / filename
    if not destination.parent.resolve(strict=True).is_relative_to(root):
        raise PosterError("output path is outside output root")
    return destination


def _atomic_replace_bytes(
    root: Path,
    path: Path,
    content: bytes,
    *,
    pre_install: Callable[[], None] | None = None,
    post_install: Callable[[], None] | None = None,
) -> None:
    temporary_path: Path | None = None
    committed = False
    installed_identity: tuple[int, int] | None = None
    with _locked_output_ancestors(root, path.parent) as guard:
        try:
            _require_ancestor_identities(root, path.parent, guard.identities)
            if pre_install is not None:
                pre_install()
                _require_ancestor_identities(root, path.parent, guard.identities)
            if os.name == "nt":
                import msvcrt

                windows_temporary, temporary_path = _create_windows_temporary_file(path)
                with windows_temporary:
                    install_handle = msvcrt.get_osfhandle(windows_temporary.fileno())
                    windows_temporary.write(content)
                    windows_temporary.flush()
                    os.fsync(windows_temporary.fileno())
                    _require_ancestor_identities(root, path.parent, guard.identities)
                    assert guard.parent_handle is not None
                    _release_windows_directory_locks(guard)
                    try:
                        _rename_windows_file_handle(
                            install_handle, guard.parent_handle, path.name
                        )
                        _acquire_windows_directory_locks(guard)
                        _require_ancestor_identities(
                            root, path.parent, guard.identities
                        )
                        installed_stat = os.fstat(windows_temporary.fileno())
                        if not stat.S_ISREG(installed_stat.st_mode) or _is_reparse(
                            installed_stat
                        ):
                            raise PosterError(
                                f"cannot write poster artifact: {path.name}"
                            )
                        windows_temporary.seek(0)
                        installed_content = windows_temporary.read()
                        if (
                            installed_stat.st_size != len(content)
                            or installed_content != content
                        ):
                            raise PosterError(
                                f"installed poster artifact content changed: {path.name}"
                            )
                        if post_install is not None:
                            post_install()
                            _require_ancestor_identities(
                                root, path.parent, guard.identities
                            )
                        windows_temporary.seek(0)
                        final_handle_stat = os.fstat(windows_temporary.fileno())
                        if (
                            final_handle_stat.st_size != len(content)
                            or windows_temporary.read() != content
                        ):
                            raise PosterError(
                                f"installed poster artifact content changed: {path.name}"
                            )
                        installed_name_stat = path.lstat()
                        if (
                            not stat.S_ISREG(installed_name_stat.st_mode)
                            or _is_reparse(installed_name_stat)
                            or (
                                installed_name_stat.st_dev,
                                installed_name_stat.st_ino,
                            )
                            != (installed_stat.st_dev, installed_stat.st_ino)
                        ):
                            raise PosterError(
                                f"cannot write poster artifact: {path.name}"
                            )
                        committed = True
                    finally:
                        if not committed:
                            _set_windows_file_disposition(
                                install_handle, delete_file=True
                            )
            else:
                with NamedTemporaryFile(
                    mode="wb",
                    dir=path.parent,
                    prefix=f".{path.name}.",
                    suffix=".tmp",
                    delete=False,
                ) as temporary:
                    temporary_path = Path(temporary.name)
                with temporary_path.open("w+b") as temporary:
                    temporary.write(content)
                    temporary.flush()
                    os.fsync(temporary.fileno())
                    _require_ancestor_identities(root, path.parent, guard.identities)
                    os.replace(temporary_path, path)
                    _require_ancestor_identities(root, path.parent, guard.identities)
                    installed_stat = path.lstat()
                    handle_stat = os.fstat(temporary.fileno())
                    installed_identity = (installed_stat.st_dev, installed_stat.st_ino)
                    if (
                        not stat.S_ISREG(installed_stat.st_mode)
                        or _is_reparse(installed_stat)
                        or installed_identity
                        != (handle_stat.st_dev, handle_stat.st_ino)
                    ):
                        raise PosterError(f"cannot write poster artifact: {path.name}")
                    temporary.seek(0)
                    if (
                        handle_stat.st_size != len(content)
                        or temporary.read() != content
                    ):
                        raise PosterError(
                            f"installed poster artifact content changed: {path.name}"
                        )
                    if post_install is not None:
                        post_install()
                        _require_ancestor_identities(
                            root, path.parent, guard.identities
                        )
                    temporary.seek(0)
                    final_handle_stat = os.fstat(temporary.fileno())
                    if (
                        final_handle_stat.st_size != len(content)
                        or temporary.read() != content
                    ):
                        raise PosterError(
                            f"installed poster artifact content changed: {path.name}"
                        )
                    installed_name_stat = path.lstat()
                    if (
                        not stat.S_ISREG(installed_name_stat.st_mode)
                        or _is_reparse(installed_name_stat)
                        or (
                            installed_name_stat.st_dev,
                            installed_name_stat.st_ino,
                        )
                        != installed_identity
                    ):
                        raise PosterError(
                            f"cannot write poster artifact: {path.name}"
                        )
                    committed = True
        except OSError as exc:
            raise PosterError(f"cannot write poster artifact: {path.name}") from exc
        finally:
            if temporary_path is not None:
                try:
                    _require_ancestor_identities(root, path.parent, guard.identities)
                except PosterError:
                    pass
                else:
                    temporary_path.unlink(missing_ok=True)
            if not committed and installed_identity is not None:
                try:
                    installed_stat = path.lstat()
                except OSError:
                    pass
                else:
                    if (installed_stat.st_dev, installed_stat.st_ino) == installed_identity:
                        path.unlink(missing_ok=True)


@contextmanager
def _locked_output_ancestors(
    root: Path, parent: Path
) -> Iterator[_AncestorGuard]:
    identities = _capture_ancestor_identities(root, parent)
    parent_handle: int | None = None
    lock_handles: list[int] = []
    try:
        if os.name == "nt":
            parent_handle = _open_windows_directory_handle(
                parent, share_write=True
            )
            _acquire_windows_directory_locks(
                _AncestorGuard(identities, parent_handle, lock_handles)
            )
        _require_ancestor_identities(root, parent, identities)
        yield _AncestorGuard(
            identities=identities,
            parent_handle=parent_handle,
            lock_handles=lock_handles,
        )
    finally:
        for handle in reversed(lock_handles):
            _close_windows_handle(handle)
        lock_handles.clear()
        if parent_handle is not None:
            _close_windows_handle(parent_handle)


def _capture_ancestor_identities(
    root: Path, parent: Path
) -> tuple[_DirectoryIdentity, ...]:
    try:
        relative = parent.relative_to(root)
    except ValueError as exc:
        raise PosterError("output path is outside output root") from exc
    paths = [root]
    current = root
    for part in relative.parts:
        current = current / part
        paths.append(current)
    identities: list[_DirectoryIdentity] = []
    for path in paths:
        try:
            path_stat = path.lstat()
        except OSError as exc:
            raise PosterError("output ancestor is unreadable") from exc
        if not stat.S_ISDIR(path_stat.st_mode) or _is_reparse(path_stat):
            raise PosterError("output path contains a followed link")
        identities.append(
            _DirectoryIdentity(path=path, device=path_stat.st_dev, inode=path_stat.st_ino)
        )
    if not parent.resolve(strict=True).is_relative_to(root):
        raise PosterError("output path is outside output root")
    return tuple(identities)


def _require_ancestor_identities(
    root: Path,
    parent: Path,
    expected: tuple[_DirectoryIdentity, ...],
) -> None:
    if _capture_ancestor_identities(root, parent) != expected:
        raise PosterError("output ancestor identity changed")


def _open_windows_directory_handle(path: Path, *, share_write: bool) -> int:
    import ctypes

    kernel32 = ctypes.WinDLL("kernel32", use_last_error=True)
    create_file = kernel32.CreateFileW
    create_file.argtypes = [
        ctypes.c_wchar_p,
        ctypes.c_uint32,
        ctypes.c_uint32,
        ctypes.c_void_p,
        ctypes.c_uint32,
        ctypes.c_uint32,
        ctypes.c_void_p,
    ]
    create_file.restype = ctypes.c_void_p
    handle = create_file(
        str(path),
        0x1 | 0x80,  # FILE_LIST_DIRECTORY | FILE_READ_ATTRIBUTES
        0x1 | (0x2 if share_write else 0),  # never share DELETE
        None,
        3,  # OPEN_EXISTING
        0x02000000 | 0x00200000,  # BACKUP_SEMANTICS | OPEN_REPARSE_POINT
        None,
    )
    invalid_handle = ctypes.c_void_p(-1).value
    if handle == invalid_handle:
        error = ctypes.get_last_error()
        raise PosterError(f"cannot lock output ancestor: {path}") from OSError(error, os.strerror(error))
    return int(handle)


def _release_windows_directory_locks(guard: _AncestorGuard) -> None:
    for handle in reversed(guard.lock_handles):
        _close_windows_handle(handle)
    guard.lock_handles.clear()


def _acquire_windows_directory_locks(guard: _AncestorGuard) -> None:
    for identity in guard.identities:
        guard.lock_handles.append(
            _open_windows_directory_handle(identity.path, share_write=False)
        )


def _create_windows_temporary_file(path: Path) -> tuple[BinaryIO, Path]:
    import ctypes
    import msvcrt

    kernel32 = ctypes.WinDLL("kernel32", use_last_error=True)
    create_file = kernel32.CreateFileW
    create_file.argtypes = [
        ctypes.c_wchar_p,
        ctypes.c_uint32,
        ctypes.c_uint32,
        ctypes.c_void_p,
        ctypes.c_uint32,
        ctypes.c_uint32,
        ctypes.c_void_p,
    ]
    create_file.restype = ctypes.c_void_p
    invalid_handle = ctypes.c_void_p(-1).value
    for _ in range(100):
        temporary_path = path.with_name(
            f".{path.name}.{secrets.token_hex(8)}.tmp"
        )
        handle = create_file(
            str(temporary_path),
            0x80000000 | 0x40000000 | 0x00010000 | 0x80,
            # GENERIC_READ | GENERIC_WRITE | DELETE | READ_ATTRIBUTES
            0x1,  # share READ, but not WRITE or DELETE
            None,
            1,  # CREATE_NEW
            0x100 | 0x00200000,  # TEMPORARY | OPEN_REPARSE_POINT
            None,
        )
        if handle != invalid_handle:
            break
        error = ctypes.get_last_error()
        if error not in {80, 183}:  # FILE_EXISTS | ALREADY_EXISTS
            raise PosterError("cannot create poster temporary file") from OSError(
                error, os.strerror(error)
            )
    else:
        raise PosterError("cannot create unique poster temporary file")

    try:
        descriptor = msvcrt.open_osfhandle(
            int(handle), os.O_BINARY | os.O_RDWR
        )
    except OSError:
        _close_windows_handle(int(handle))
        raise
    return os.fdopen(descriptor, "w+b"), temporary_path


def _rename_windows_file_handle(
    file_handle: int, parent_handle: int, filename: str
) -> None:
    import ctypes
    from ctypes import wintypes

    class FileRenameInfo(ctypes.Structure):
        _fields_ = [
            ("Flags", wintypes.DWORD),
            ("RootDirectory", wintypes.HANDLE),
            ("FileNameLength", wintypes.DWORD),
            ("FileName", wintypes.WCHAR * (len(filename) + 1)),
        ]

    class IoStatusBlock(ctypes.Structure):
        _fields_ = [
            ("Status", ctypes.c_void_p),
            ("Information", ctypes.c_size_t),
        ]

    information = FileRenameInfo()
    information.Flags = 0x1  # FILE_RENAME_FLAG_REPLACE_IF_EXISTS
    information.RootDirectory = ctypes.c_void_p(parent_handle)
    information.FileNameLength = len(filename.encode("utf-16-le"))
    information.FileName = filename
    ntdll = ctypes.WinDLL("ntdll")
    set_information = ntdll.NtSetInformationFile
    set_information.argtypes = [
        wintypes.HANDLE,
        ctypes.POINTER(IoStatusBlock),
        ctypes.c_void_p,
        wintypes.DWORD,
        ctypes.c_int,
    ]
    set_information.restype = ctypes.c_long
    io_status = IoStatusBlock()
    status = set_information(
        ctypes.c_void_p(file_handle),
        ctypes.byref(io_status),
        ctypes.byref(information),
        ctypes.sizeof(information),
        10,  # FileRenameInformation
    )
    if status < 0:
        convert_status = ntdll.RtlNtStatusToDosError
        convert_status.argtypes = [ctypes.c_long]
        convert_status.restype = wintypes.ULONG
        error = convert_status(status)
        raise PosterError("cannot install poster artifact by directory identity") from OSError(
            error, os.strerror(error)
        )


def _set_windows_file_disposition(file_handle: int, *, delete_file: bool) -> None:
    import ctypes
    from ctypes import wintypes

    class FileDispositionInfo(ctypes.Structure):
        _fields_ = [("DeleteFile", wintypes.BOOL)]

    information = FileDispositionInfo(DeleteFile=delete_file)
    kernel32 = ctypes.WinDLL("kernel32", use_last_error=True)
    set_information = kernel32.SetFileInformationByHandle
    set_information.argtypes = [
        wintypes.HANDLE,
        ctypes.c_int,
        ctypes.c_void_p,
        wintypes.DWORD,
    ]
    set_information.restype = wintypes.BOOL
    if not set_information(
        ctypes.c_void_p(file_handle),
        4,  # FileDispositionInfo
        ctypes.byref(information),
        ctypes.sizeof(information),
    ):
        error = ctypes.get_last_error()
        raise PosterError("cannot clean poster temporary file") from OSError(
            error, os.strerror(error)
        )


def _close_windows_handle(handle: int) -> None:
    import ctypes

    kernel32 = ctypes.WinDLL("kernel32", use_last_error=True)
    if not kernel32.CloseHandle(ctypes.c_void_p(handle)):
        error = ctypes.get_last_error()
        raise PosterError("cannot release output ancestor lock") from OSError(
            error, os.strerror(error)
        )


def _is_reparse(path_stat: os.stat_result) -> bool:
    attributes = getattr(path_stat, "st_file_attributes", 0)
    reparse_flag = getattr(stat, "FILE_ATTRIBUTE_REPARSE_POINT", 0x400)
    return bool(attributes & reparse_flag)
