import ctypes
import json
import os
import struct
from collections import Counter
from dataclasses import replace
from pathlib import Path
from typing import Self

import pyarrow.parquet as pq
import pytest
from jsonschema import Draft202012Validator, FormatChecker

from hk_movie_rag import posters
from hk_movie_rag.config import PosterRoot, Settings
from hk_movie_rag.hashing import sha256_file
from hk_movie_rag.posters import (
    PosterError,
    PosterScanResult,
    build_posters,
    classify_hash_groups,
    inspect_image,
    materialize_posters,
    scan_candidate,
    scan_posters,
)

FIXTURES = Path(__file__).parent / "fixtures" / "images"


def test_atomic_writer_detects_post_install_name_replacement(tmp_path: Path) -> None:
    """Covers POSIX replacement detection and Windows delete-share exclusion."""
    destination = tmp_path / "authority.json"

    def replace_installed_name() -> None:
        destination.unlink()
        destination.write_bytes(b"foreign")

    with pytest.raises(PosterError, match="cannot write poster artifact"):
        posters._atomic_replace_bytes(
            tmp_path,
            destination,
            b"governed",
            post_install=replace_installed_name,
        )
    if os.name == "nt":
        assert not destination.exists()
    else:
        assert destination.read_bytes() == b"foreign"


def test_atomic_writer_detects_post_install_hardlink_mutation(tmp_path: Path) -> None:
    """Breaks if installed content is checked only before the post-install gate."""
    destination = tmp_path / "authority.json"
    alias = tmp_path / "authority-alias.json"

    def mutate_installed_inode() -> None:
        os.link(destination, alias)
        alias.write_bytes(b"mutated!")

    with pytest.raises(PosterError, match="cannot write poster artifact|content changed"):
        posters._atomic_replace_bytes(
            tmp_path,
            destination,
            b"governed",
            post_install=mutate_installed_inode,
        )
    assert not destination.exists()


def test_unknown_filename_stem_is_rejected(release_ids: set[str], tmp_path: Path) -> None:
    """Breaks if poster candidates are joined fuzzily or unknown keys are ignored."""
    image = tmp_path / "unknown.jpg"
    image.write_bytes(valid_jpeg_bytes())

    with pytest.raises(PosterError, match="orphan poster key"):
        scan_candidate(image, release_ids)


def test_mime_comes_from_content_not_suffix(tmp_path: Path) -> None:
    """Breaks if a mislabeled PNG is recorded or copied as JPEG."""
    image = tmp_path / "1970_GSQ_001.jpg"
    image.write_bytes(valid_png_bytes())

    candidate = inspect_image(image, "1970_GSQ_001")

    assert candidate.detected_format == "PNG"
    assert candidate.detected_mime_type == "image/png"
    assert candidate.detected_extension == ".png"
    assert (candidate.width, candidate.height, candidate.mode) == (2, 3, "RGB")
    assert candidate.byte_length == len(valid_png_bytes())
    assert candidate.content_sha256 == sha256_file(image)


def test_cross_key_equal_hash_is_never_promoted(tmp_path: Path) -> None:
    """Breaks if a cross-key content collision leaves either candidate active."""
    candidates = []
    for movie_id in ("1970_GSQ_001", "1970_YCCS_001"):
        path = tmp_path / f"{movie_id}.jpg"
        path.write_bytes(valid_jpeg_bytes())
        candidates.append(inspect_image(path, movie_id))

    result = classify_hash_groups(candidates)

    assert {row.quality_status for row in result} == {"content_conflict"}
    assert not any(row.is_primary for row in result)
    assert not any(row.publishable for row in result)


def test_same_key_different_content_fails_closed(repo_root: Path, tmp_path: Path) -> None:
    """Breaks if ambiguous candidates for one Release key are silently selected."""
    first_root = tmp_path / "first"
    second_root = tmp_path / "second"
    first_root.mkdir()
    second_root.mkdir()
    (first_root / "1970_GSQ_001.jpg").write_bytes(valid_jpeg_bytes())
    (second_root / "1970_GSQ_001.jpg").write_bytes(valid_png_bytes())
    settings = _poster_settings(repo_root, first_root, second_root)

    with pytest.raises(PosterError, match="same poster key has different content"):
        scan_posters(settings, {"1970_GSQ_001"})


def test_nested_files_and_metadata_are_not_candidates(repo_root: Path, tmp_path: Path) -> None:
    """Breaks if canonical scanning becomes recursive or treats sidecars as posters."""
    first_root = tmp_path / "first"
    second_root = tmp_path / "second"
    nested = first_root / "nested"
    nested.mkdir(parents=True)
    second_root.mkdir()
    (nested / "unknown.jpg").write_bytes(valid_jpeg_bytes())
    (second_root / ".DS_Store").write_bytes(b"metadata")
    (first_root / "1970_GSQ_001.jpg").write_bytes(valid_jpeg_bytes())

    scan = scan_posters(
        _poster_settings(repo_root, first_root, second_root),
        {"1970_GSQ_001", "1970_YCCS_001"},
    )

    assert scan.present_keys == 1
    assert scan.orphan_keys == set()
    assert Counter(row.quality_status for row in scan.rows) == {
        "machine_passed": 1,
        "missing": 1,
    }


def test_placeholder_signature_requires_exact_nine_key_group(tmp_path: Path) -> None:
    """Breaks if any small image or arbitrary repeat count is called the placeholder."""
    candidates = []
    for index in range(2):
        movie_id = f"1970_TEST_{index:03d}"
        path = tmp_path / f"{movie_id}.jpg"
        path.write_bytes(valid_jpeg_bytes())
        candidate = inspect_image(path, movie_id)
        candidates.append(replace(candidate, width=65, height=91))

    with pytest.raises(PosterError, match="65x91 repeated hash group must contain exactly 9 keys"):
        classify_hash_groups(candidates)


def test_build_writes_one_row_per_movie_and_only_promotes_machine_passed(
    repo_root: Path, tmp_path: Path
) -> None:
    """Breaks if missing rows vanish or unresolved poster bytes enter active assets."""
    first_root = tmp_path / "first"
    second_root = tmp_path / "second"
    output_root = tmp_path / "output"
    first_root.mkdir()
    second_root.mkdir()
    output_root.mkdir()
    source = first_root / "1970_GSQ_001.jpg"
    source.write_bytes(valid_png_bytes())
    source_hash = sha256_file(source)

    result = build_posters(
        _poster_settings(repo_root, first_root, second_root),
        {"1970_GSQ_001", "1970_YCCS_001"},
        output_root,
    )
    rows = pq.read_table(result.manifest_path).to_pylist()

    assert [row["movie_id"] for row in rows] == ["1970_GSQ_001", "1970_YCCS_001"]
    assert result.state_counts == {"machine_passed": 1, "missing": 1}
    assert all(row["rights_status"] == "unknown" for row in rows)
    assert not any(row["is_primary"] or row["publishable"] for row in rows)
    assert (output_root / "assets/posters/originals/1970_GSQ_001.png").read_bytes() == (
        source.read_bytes()
    )
    assert (output_root / "assets/posters/derived/1970_GSQ_001.webp").is_file()
    assert not (output_root / "assets/posters/originals/1970_YCCS_001.jpg").exists()
    assert sha256_file(source) == source_hash


def test_poster_producer_emits_rows_matching_the_media_asset_contract(
    repo_root: Path, tmp_path: Path
) -> None:
    """Breaks if canonical poster rows omit or invent governed media evidence."""
    source_root = tmp_path / "source"
    empty_root = tmp_path / "empty"
    output_root = tmp_path / "output"
    source_root.mkdir()
    empty_root.mkdir()
    output_root.mkdir()
    (source_root / "1970_GSQ_001.jpg").write_bytes(valid_jpeg_bytes())

    result = build_posters(
        _poster_settings(repo_root, source_root, empty_root),
        {"1970_GSQ_001", "1970_YCCS_001"},
        output_root,
    )
    table = pq.read_table(result.manifest_path)
    schema = json.loads(
        (repo_root / "schemas" / "media_asset.schema.json").read_text(encoding="utf-8")
    )
    validator = Draft202012Validator(schema, format_checker=FormatChecker())

    assert table.schema.names == [
        "asset_id",
        "movie_id",
        "asset_type",
        "source_object_uri",
        "original_object_uri",
        "derived_object_uri",
        "quarantine_object_uri",
        "content_sha256",
        "detected_format",
        "detected_mime_type",
        "detected_extension",
        "width",
        "height",
        "color_mode",
        "byte_length",
        "quality_status",
        "identity_confidence",
        "source_url",
        "rights_status",
        "version",
        "is_primary",
        "publishable",
        "created_at",
        "reviewed_at",
    ]
    rows = table.to_pylist()
    for row in rows:
        validator.validate(row)
    assert {
        (
            row["identity_confidence"],
            row["source_url"],
            row["created_at"],
            row["reviewed_at"],
        )
        for row in rows
    } == {(None, None, None, None)}


def test_build_replaces_existing_hardlink_without_mutating_target(
    repo_root: Path, tmp_path: Path
) -> None:
    """Breaks if a generated original follows an existing hard link into protected data."""
    first_root = tmp_path / "first"
    second_root = tmp_path / "second"
    output_root = tmp_path / "output"
    first_root.mkdir()
    second_root.mkdir()
    output_root.mkdir()
    source = first_root / "1970_GSQ_001.jpg"
    source.write_bytes(valid_png_bytes())
    protected = tmp_path / "protected.png"
    protected.write_bytes(b"protected")
    destination = output_root / "assets/posters/originals/1970_GSQ_001.png"
    destination.parent.mkdir(parents=True)
    os.link(protected, destination)

    build_posters(
        _poster_settings(repo_root, first_root, second_root),
        {"1970_GSQ_001"},
        output_root,
    )

    assert protected.read_bytes() == b"protected"
    assert not os.path.samefile(protected, destination)
    assert destination.read_bytes() == valid_png_bytes()


def test_consecutive_builds_are_byte_deterministic(repo_root: Path, tmp_path: Path) -> None:
    """Breaks if manifests or image encoders incorporate run-specific state."""
    first_root = tmp_path / "first"
    second_root = tmp_path / "second"
    output_root = tmp_path / "output"
    first_root.mkdir()
    second_root.mkdir()
    output_root.mkdir()
    (first_root / "1970_GSQ_001.jpg").write_bytes(valid_jpeg_bytes())
    settings = _poster_settings(repo_root, first_root, second_root)

    first = build_posters(settings, {"1970_GSQ_001"}, output_root)
    first_hashes = _generated_hashes(output_root)
    second = build_posters(settings, {"1970_GSQ_001"}, output_root)

    assert second == first
    assert _generated_hashes(output_root) == first_hashes


@pytest.mark.parametrize("next_state", ["missing", "placeholder", "content_conflict"])
def test_state_transition_rejects_stale_active_paths_before_any_mutation(
    tmp_path: Path, next_state: str
) -> None:
    """Breaks if a non-active rebuild leaves prior active bytes beside its new manifest."""
    output_root = tmp_path / "output"
    output_root.mkdir()
    source = tmp_path / "1970_GSQ_001.jpg"
    source.write_bytes(valid_jpeg_bytes())
    active = inspect_image(source, "1970_GSQ_001")
    materialize_posters(_scan(active), output_root)
    before = _generated_tree_state(output_root)

    if next_state == "missing":
        transitioned = replace(
            active,
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
    else:
        transitioned = replace(active, quality_status=next_state)

    with pytest.raises(PosterError, match="stale generated poster path"):
        materialize_posters(_scan(transitioned), output_root)

    assert _generated_tree_state(output_root) == before


def test_format_transition_rejects_obsolete_original_before_any_mutation(
    tmp_path: Path,
) -> None:
    """Breaks if a detected-extension change leaves the obsolete original in place."""
    output_root = tmp_path / "output"
    output_root.mkdir()
    jpeg_source = tmp_path / "first.jpg"
    jpeg_source.write_bytes(valid_jpeg_bytes())
    materialize_posters(
        _scan(inspect_image(jpeg_source, "1970_GSQ_001")), output_root
    )
    before = _generated_tree_state(output_root)
    png_source = tmp_path / "second.jpg"
    png_source.write_bytes(valid_png_bytes())

    with pytest.raises(PosterError, match="stale generated poster path"):
        materialize_posters(
            _scan(inspect_image(png_source, "1970_GSQ_001")), output_root
        )

    assert _generated_tree_state(output_root) == before


@pytest.mark.skipif(os.name != "nt", reason="Windows hard-link race regression")
def test_temporary_path_substitution_cannot_redirect_writes(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """Breaks if the create-time temp handle is closed and reopened by pathname."""
    output_root = tmp_path / "output"
    output_root.mkdir()
    protected = tmp_path / "protected.bin"
    protected.write_bytes(b"protected-outside-bytes")
    source = tmp_path / "source.jpg"
    source.write_bytes(valid_jpeg_bytes())
    scan = _scan(inspect_image(source, "1970_GSQ_001"))
    real_create_temporary = posters._create_windows_temporary_file
    attack = {"attempted": False, "blocked": False}
    displaced_temporary: list[Path] = []

    def substitute_temporary_path(temporary_path: Path) -> None:
        if attack["attempted"]:
            return
        attack["attempted"] = True
        displaced = temporary_path.with_name(f"{temporary_path.name}.displaced")
        try:
            os.replace(temporary_path, displaced)
        except PermissionError:
            attack["blocked"] = True
            return
        os.link(protected, temporary_path)
        displaced_temporary.append(displaced)

    class AttackingTemporary:
        def __init__(self, inner: object, temporary_path: Path) -> None:
            self._inner = inner
            self._temporary_path = temporary_path

        def __enter__(self) -> Self:
            self._inner.__enter__()
            return self

        def __exit__(self, *args: object) -> object:
            result = self._inner.__exit__(*args)
            if not attack["attempted"]:
                substitute_temporary_path(self._temporary_path)
            return result

        def flush(self) -> None:
            self._inner.flush()
            substitute_temporary_path(self._temporary_path)

        def __getattr__(self, name: str) -> object:
            return getattr(self._inner, name)

    def attacking_create_temporary(path: Path) -> tuple[object, Path]:
        temporary, temporary_path = real_create_temporary(path)
        return AttackingTemporary(temporary, temporary_path), temporary_path

    monkeypatch.setattr(
        posters, "_create_windows_temporary_file", attacking_create_temporary
    )

    materialize_posters(scan, output_root)

    assert attack == {"attempted": True, "blocked": True}
    assert protected.read_bytes() == b"protected-outside-bytes"
    assert (output_root / "assets/posters/originals/1970_GSQ_001.jpg").read_bytes() == (
        valid_jpeg_bytes()
    )
    assert all(not path.exists() for path in displaced_temporary)
    assert not list(output_root.rglob(".*.tmp"))


@pytest.mark.skipif(os.name != "nt", reason="Windows live-handle sharing regression")
def test_live_temporary_file_denies_second_writer_and_rebinds_content(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """Breaks if another writer can alter the live temp after the intended flush."""
    output_root = tmp_path / "output"
    output_root.mkdir()
    destination = output_root / "assets/posters/originals/1970_GSQ_001.jpg"
    destination.parent.mkdir(parents=True)
    protected = tmp_path / "protected-sentinel.bin"
    sentinel = b"protected-sentinel-bytes"
    protected.write_bytes(sentinel)
    os.link(protected, destination)
    source = tmp_path / "source.jpg"
    intended = valid_jpeg_bytes()
    source.write_bytes(intended)
    scan = _scan(inspect_image(source, "1970_GSQ_001"))
    real_create_temporary = posters._create_windows_temporary_file
    attempt = {"attempted": False, "opened": False, "error": None}

    class SecondWriterTemporary:
        def __init__(self, inner: object, temporary_path: Path) -> None:
            self._inner = inner
            self._temporary_path = temporary_path

        def __enter__(self) -> Self:
            self._inner.__enter__()
            return self

        def __exit__(self, *args: object) -> object:
            return self._inner.__exit__(*args)

        def flush(self) -> None:
            self._inner.flush()
            if not attempt["attempted"]:
                attempt["attempted"] = True
                opened, error = _try_append_with_windows_writer(
                    self._temporary_path, b"attacker-appended-bytes"
                )
                attempt["opened"] = opened
                attempt["error"] = error

        def __getattr__(self, name: str) -> object:
            return getattr(self._inner, name)

    def attacking_create_temporary(path: Path) -> tuple[object, Path]:
        temporary, temporary_path = real_create_temporary(path)
        return SecondWriterTemporary(temporary, temporary_path), temporary_path

    monkeypatch.setattr(
        posters, "_create_windows_temporary_file", attacking_create_temporary
    )

    materialize_posters(scan, output_root)

    assert attempt == {"attempted": True, "opened": False, "error": 32}
    assert destination.read_bytes() == intended
    assert protected.read_bytes() == sentinel
    assert not os.path.samefile(protected, destination)
    assert not list(output_root.rglob(".*.tmp"))


@pytest.mark.skipif(os.name != "nt", reason="Windows reparse race regression")
def test_in_place_reparse_mutation_cannot_redirect_temp_creation(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """Breaks if guarded output ancestors share write access with an attacker."""
    output_root = tmp_path / "output"
    output_root.mkdir()
    unlocked = tmp_path / "unlocked-control"
    unlocked.mkdir()
    unlocked_target = tmp_path / "unlocked-target"
    unlocked_target.mkdir()
    control_mutated, control_error = _try_set_directory_mount_point(
        unlocked, unlocked_target
    )
    assert (control_mutated, control_error) == (True, 0)
    unlocked.rmdir()
    assert unlocked_target.is_dir()
    outside = tmp_path / "outside"
    outside.mkdir()
    protected = outside / "protected.bin"
    protected.write_bytes(b"protected-outside-bytes")
    source = tmp_path / "source.jpg"
    source.write_bytes(valid_jpeg_bytes())
    scan = _scan(inspect_image(source, "1970_GSQ_001"))
    real_create_temporary = posters._create_windows_temporary_file
    real_fsync = posters.os.fsync
    attack: dict[str, object] = {
        "attempted": False,
        "mutated": False,
        "error": None,
        "outside_write_observed": False,
    }

    def attacking_create_temporary(path: Path) -> tuple[object, Path]:
        parent = path.parent
        if parent.name == "originals" and not attack["attempted"]:
            attack["attempted"] = True
            mutated, error = _try_set_directory_mount_point(parent, outside)
            attack["mutated"] = mutated
            attack["error"] = error
        return real_create_temporary(path)

    def fsync_observing_outside(file_descriptor: int) -> None:
        real_fsync(file_descriptor)
        attack["outside_write_observed"] = any(
            path != protected and path.is_file() and path.stat().st_size > 0
            for path in outside.iterdir()
        )

    monkeypatch.setattr(
        posters, "_create_windows_temporary_file", attacking_create_temporary
    )
    monkeypatch.setattr(posters.os, "fsync", fsync_observing_outside)

    materialize_posters(scan, output_root)

    assert attack["attempted"] is True
    assert attack["mutated"] is False
    assert attack["error"] in {5, 32}
    assert attack["outside_write_observed"] is False
    assert protected.read_bytes() == b"protected-outside-bytes"
    assert sorted(path.name for path in outside.iterdir()) == ["protected.bin"]
    assert (output_root / "assets/posters/originals/1970_GSQ_001.jpg").read_bytes() == (
        valid_jpeg_bytes()
    )
    assert not list(output_root.rglob(".*.tmp"))


@pytest.fixture
def release_ids() -> set[str]:
    return {"1970_GSQ_001"}


def valid_jpeg_bytes() -> bytes:
    return (FIXTURES / "valid.jpg").read_bytes()


def valid_png_bytes() -> bytes:
    return (FIXTURES / "mislabeled.jpg").read_bytes()


def _try_set_directory_mount_point(path: Path, target: Path) -> tuple[bool, int]:
    """Attempt the exact in-place reparse mutation covered by the race test."""
    kernel32 = ctypes.WinDLL("kernel32", use_last_error=True)
    kernel32.CreateFileW.argtypes = (
        ctypes.c_wchar_p,
        ctypes.c_uint32,
        ctypes.c_uint32,
        ctypes.c_void_p,
        ctypes.c_uint32,
        ctypes.c_uint32,
        ctypes.c_void_p,
    )
    kernel32.CreateFileW.restype = ctypes.c_void_p
    kernel32.DeviceIoControl.argtypes = (
        ctypes.c_void_p,
        ctypes.c_uint32,
        ctypes.c_void_p,
        ctypes.c_uint32,
        ctypes.c_void_p,
        ctypes.c_uint32,
        ctypes.POINTER(ctypes.c_uint32),
        ctypes.c_void_p,
    )
    kernel32.DeviceIoControl.restype = ctypes.c_int
    kernel32.CloseHandle.argtypes = (ctypes.c_void_p,)
    kernel32.CloseHandle.restype = ctypes.c_int

    handle = kernel32.CreateFileW(
        str(path),
        0x40000000,  # GENERIC_WRITE, required by FSCTL_SET_REPARSE_POINT
        0x00000001 | 0x00000002 | 0x00000004,
        None,
        3,  # OPEN_EXISTING
        0x02000000 | 0x00200000,  # BACKUP_SEMANTICS | OPEN_REPARSE_POINT
        None,
    )
    invalid_handle = ctypes.c_void_p(-1).value
    if handle == invalid_handle:
        return False, ctypes.get_last_error()

    try:
        substitute_name = f"\\??\\{target.resolve()}".encode("utf-16-le")
        print_name = str(target.resolve()).encode("utf-16-le")
        path_buffer = substitute_name + b"\x00\x00" + print_name + b"\x00\x00"
        reparse_buffer = struct.pack(
            "<IHHHHHH",
            0xA0000003,  # IO_REPARSE_TAG_MOUNT_POINT
            8 + len(path_buffer),
            0,
            0,
            len(substitute_name),
            len(substitute_name) + 2,
            len(print_name),
        ) + path_buffer
        raw_buffer = ctypes.create_string_buffer(reparse_buffer)
        bytes_returned = ctypes.c_uint32()
        succeeded = kernel32.DeviceIoControl(
            handle,
            0x000900A4,  # FSCTL_SET_REPARSE_POINT
            raw_buffer,
            len(reparse_buffer),
            None,
            0,
            ctypes.byref(bytes_returned),
            None,
        )
        return bool(succeeded), 0 if succeeded else ctypes.get_last_error()
    finally:
        kernel32.CloseHandle(handle)


def _try_append_with_windows_writer(path: Path, payload: bytes) -> tuple[bool, int]:
    kernel32 = ctypes.WinDLL("kernel32", use_last_error=True)
    kernel32.CreateFileW.argtypes = (
        ctypes.c_wchar_p,
        ctypes.c_uint32,
        ctypes.c_uint32,
        ctypes.c_void_p,
        ctypes.c_uint32,
        ctypes.c_uint32,
        ctypes.c_void_p,
    )
    kernel32.CreateFileW.restype = ctypes.c_void_p
    kernel32.SetFilePointerEx.argtypes = (
        ctypes.c_void_p,
        ctypes.c_longlong,
        ctypes.c_void_p,
        ctypes.c_uint32,
    )
    kernel32.SetFilePointerEx.restype = ctypes.c_int
    kernel32.WriteFile.argtypes = (
        ctypes.c_void_p,
        ctypes.c_void_p,
        ctypes.c_uint32,
        ctypes.POINTER(ctypes.c_uint32),
        ctypes.c_void_p,
    )
    kernel32.WriteFile.restype = ctypes.c_int
    kernel32.FlushFileBuffers.argtypes = (ctypes.c_void_p,)
    kernel32.FlushFileBuffers.restype = ctypes.c_int
    kernel32.CloseHandle.argtypes = (ctypes.c_void_p,)
    kernel32.CloseHandle.restype = ctypes.c_int

    handle = kernel32.CreateFileW(
        str(path),
        0x40000000,  # GENERIC_WRITE
        0x00000001 | 0x00000002 | 0x00000004,
        None,
        3,  # OPEN_EXISTING
        0x00200000,  # OPEN_REPARSE_POINT
        None,
    )
    invalid_handle = ctypes.c_void_p(-1).value
    if handle == invalid_handle:
        return False, ctypes.get_last_error()

    try:
        if not kernel32.SetFilePointerEx(handle, 0, None, 2):  # FILE_END
            error = ctypes.get_last_error()
            raise OSError(error, os.strerror(error))
        buffer = ctypes.create_string_buffer(payload)
        written = ctypes.c_uint32()
        if not kernel32.WriteFile(
            handle,
            buffer,
            len(payload),
            ctypes.byref(written),
            None,
        ):
            error = ctypes.get_last_error()
            raise OSError(error, os.strerror(error))
        if written.value != len(payload):
            raise OSError("short Windows test write")
        if not kernel32.FlushFileBuffers(handle):
            error = ctypes.get_last_error()
            raise OSError(error, os.strerror(error))
        return True, 0
    finally:
        kernel32.CloseHandle(handle)


def _poster_settings(repo_root: Path, *roots: Path) -> Settings:
    return replace(
        Settings.load(repo_root),
        canonical_poster_roots=tuple(PosterRoot(path=root, recursive=False) for root in roots),
    )


def _scan(row: posters.PosterRow) -> PosterScanResult:
    detected_formats = {row.detected_format: 1} if row.detected_format else {}
    return PosterScanResult(
        release_version="v1.2",
        rows=(row,),
        present_keys=int(row.source_path is not None),
        orphan_keys=frozenset(),
        detected_formats=detected_formats,
    )


def _generated_hashes(output_root: Path) -> dict[str, str]:
    return {
        path.relative_to(output_root).as_posix(): sha256_file(path)
        for path in sorted(output_root.rglob("*"))
        if path.is_file()
    }


def _generated_tree_state(output_root: Path) -> dict[str, str | None]:
    return {
        path.relative_to(output_root).as_posix(): sha256_file(path) if path.is_file() else None
        for path in sorted(output_root.rglob("*"))
    }
