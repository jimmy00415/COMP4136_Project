"""Cross-process coordination for one canonical Release namespace."""

from __future__ import annotations

import os
import stat
from collections.abc import Iterator
from contextlib import contextmanager
from dataclasses import dataclass
from pathlib import Path
from typing import BinaryIO


class ReleaseLockError(OSError):
    """Raised when a Release namespace cannot be locked safely."""


@dataclass(frozen=True)
class _FileIdentity:
    device: int
    inode: int


@dataclass(frozen=True)
class _DirectoryIdentity:
    path: Path
    device: int
    inode: int


@contextmanager
def release_guard(
    root: Path, release_version: str, *, exclusive: bool
) -> Iterator[None]:
    """Hold a stable, no-follow interprocess lock for one Release version."""
    root = Path(os.path.abspath(root))
    if Path(release_version).name != release_version or release_version in {"", ".", ".."}:
        raise ReleaseLockError("release version is unsafe")
    lock_directory = root / "data" / "release" / ".locks"
    lock_path = lock_directory / f"{release_version}.lock"

    with _protected_lock_directory(root, lock_directory) as directory_descriptor:
        try:
            lock_file = _open_lock_file(
                lock_path, directory_descriptor=directory_descriptor
            )
        except ReleaseLockError:
            raise
        except OSError as exc:
            raise ReleaseLockError(
                f"cannot open Release publication lock: {exc}"
            ) from exc
        locked = False
        try:
            identity = _require_lock_name_identity(lock_file, lock_path)
            _lock_file(lock_file, exclusive=exclusive)
            locked = True
            _require_lock_name_identity(lock_file, lock_path, expected=identity)
        except ReleaseLockError:
            _discard_lock_file(lock_file, locked=locked)
            raise
        except OSError as exc:
            _discard_lock_file(lock_file, locked=locked)
            raise ReleaseLockError(
                f"cannot acquire Release publication lock: {exc}"
            ) from exc

        body_failed = False
        try:
            yield
        except BaseException:
            body_failed = True
            raise
        finally:
            cleanup_error: ReleaseLockError | None = None
            try:
                _require_lock_name_identity(lock_file, lock_path, expected=identity)
            except ReleaseLockError as exc:
                cleanup_error = exc
            except OSError as exc:
                cleanup_error = ReleaseLockError(
                    f"Release lock name identity changed: {exc}"
                )
                cleanup_error.__cause__ = exc
            try:
                _unlock_file(lock_file)
            except ReleaseLockError as exc:
                if cleanup_error is None:
                    cleanup_error = ReleaseLockError(
                        f"cannot release Release publication lock: {exc}"
                    )
                    cleanup_error.__cause__ = exc
            except OSError as exc:
                if cleanup_error is None:
                    cleanup_error = ReleaseLockError(
                        f"cannot release Release publication lock: {exc}"
                    )
                    cleanup_error.__cause__ = exc
            try:
                lock_file.close()
            except ReleaseLockError as exc:
                if cleanup_error is None:
                    cleanup_error = exc
            except OSError as exc:
                if cleanup_error is None:
                    cleanup_error = ReleaseLockError(
                        f"cannot close Release publication lock: {exc}"
                    )
                    cleanup_error.__cause__ = exc
            if cleanup_error is not None and not body_failed:
                raise cleanup_error


def _discard_lock_file(lock_file: BinaryIO, *, locked: bool) -> None:
    """Best-effort cleanup without replacing an already-governed setup error."""
    if locked:
        try:
            _unlock_file(lock_file)
        except OSError:
            pass
    try:
        lock_file.close()
    except OSError:
        pass


@contextmanager
def _protected_lock_directory(root: Path, directory: Path) -> Iterator[int | None]:
    if os.name == "nt":
        try:
            _require_or_create_windows_directory(root, directory)
            identities = _capture_windows_directory_identities(root, directory)
        except ReleaseLockError:
            raise
        except OSError as exc:
            raise ReleaseLockError(
                "cannot protect Release lock directory"
            ) from exc
        handles: list[int] = []
        body_failed = False
        try:
            try:
                for identity in identities:
                    handles.append(_open_windows_directory_handle(identity.path))
                _require_windows_directory_identities(root, directory, identities)
            except ReleaseLockError:
                raise
            except OSError as exc:
                raise ReleaseLockError(
                    f"cannot protect Release lock directory: {exc}"
                ) from exc
            try:
                yield None
            except BaseException:
                body_failed = True
                raise
        finally:
            cleanup_error: ReleaseLockError | None = None
            if handles:
                try:
                    _require_windows_directory_identities(root, directory, identities)
                except ReleaseLockError as exc:
                    cleanup_error = exc
                except OSError as exc:
                    cleanup_error = ReleaseLockError(
                        f"Release lock directory identity changed: {exc}"
                    )
                    cleanup_error.__cause__ = exc
            for handle in reversed(handles):
                try:
                    _close_windows_handle(handle)
                except ReleaseLockError as exc:
                    if cleanup_error is None:
                        cleanup_error = exc
                except OSError as exc:
                    if cleanup_error is None:
                        cleanup_error = ReleaseLockError(
                            f"cannot release Release lock directory: {exc}"
                        )
                        cleanup_error.__cause__ = exc
            if cleanup_error is not None and not body_failed:
                raise cleanup_error
        return

    release_root = directory.parent
    authority_descriptor = _open_posix_lock_directory(root, release_root)
    authority_identity = _require_posix_directory_identity(
        authority_descriptor, release_root, label="Release root directory"
    )
    authority_locked = False
    descriptor: int | None = None
    directory_identity: _DirectoryIdentity | None = None
    directory_locked = False
    body_failed = False
    try:
        try:
            _lock_posix_descriptor(authority_descriptor, exclusive=True)
        except OSError as exc:
            raise ReleaseLockError(
                f"cannot acquire stable Release root authority: {exc}"
            ) from exc
        authority_locked = True
        _require_posix_directory_identity(
            authority_descriptor,
            release_root,
            label="Release root directory",
            expected=authority_identity,
        )
        descriptor = _open_posix_child_directory(
            authority_descriptor, directory, directory.name
        )
        directory_identity = _require_posix_directory_identity(
            descriptor, directory, label="Release lock directory"
        )
        try:
            _lock_posix_descriptor(descriptor, exclusive=True)
        except OSError as exc:
            raise ReleaseLockError(
                f"cannot acquire Release lock directory: {exc}"
            ) from exc
        directory_locked = True
        _require_posix_directory_identity(
            descriptor,
            directory,
            label="Release lock directory",
            expected=directory_identity,
        )
        try:
            yield descriptor
        except BaseException:
            body_failed = True
            raise
    finally:
        posix_cleanup_error: ReleaseLockError | None = None
        if descriptor is not None:
            if directory_identity is not None:
                try:
                    _require_posix_directory_identity(
                        descriptor,
                        directory,
                        label="Release lock directory",
                        expected=directory_identity,
                    )
                except ReleaseLockError as exc:
                    posix_cleanup_error = exc
            if directory_locked:
                try:
                    _unlock_posix_descriptor(descriptor)
                except OSError as exc:
                    if posix_cleanup_error is None:
                        posix_cleanup_error = ReleaseLockError(
                            f"cannot release Release lock directory: {exc}"
                        )
                        posix_cleanup_error.__cause__ = exc
            try:
                os.close(descriptor)
            except OSError as exc:
                if posix_cleanup_error is None:
                    posix_cleanup_error = ReleaseLockError(
                        f"cannot close Release lock directory: {exc}"
                    )
                    posix_cleanup_error.__cause__ = exc
        try:
            _require_posix_directory_identity(
                authority_descriptor,
                release_root,
                label="Release root directory",
                expected=authority_identity,
            )
        except ReleaseLockError as exc:
            if posix_cleanup_error is None:
                posix_cleanup_error = exc
        if authority_locked:
            try:
                _unlock_posix_descriptor(authority_descriptor)
            except OSError as exc:
                if posix_cleanup_error is None:
                    posix_cleanup_error = ReleaseLockError(
                        f"cannot release stable Release root authority: {exc}"
                    )
                    posix_cleanup_error.__cause__ = exc
        try:
            os.close(authority_descriptor)
        except OSError as exc:
            if posix_cleanup_error is None:
                posix_cleanup_error = ReleaseLockError(
                    f"cannot close stable Release root authority: {exc}"
                )
                posix_cleanup_error.__cause__ = exc
        if posix_cleanup_error is not None and not body_failed:
            raise posix_cleanup_error


def _open_posix_lock_directory(root: Path, directory: Path) -> int:
    flags = (
        os.O_RDONLY
        | getattr(os, "O_CLOEXEC", 0)
        | getattr(os, "O_DIRECTORY", 0)
        | getattr(os, "O_NOFOLLOW", 0)
    )
    descriptor: int | None = None
    try:
        descriptor = os.open(root, flags)
        for part in directory.relative_to(root).parts:
            try:
                os.mkdir(part, mode=0o700, dir_fd=descriptor)
            except FileExistsError:
                pass
            child = os.open(part, flags, dir_fd=descriptor)
            child_stat = os.fstat(child)
            if not stat.S_ISDIR(child_stat.st_mode):
                os.close(child)
                raise ReleaseLockError(
                    "Release lock directory violates no-follow containment"
                )
            os.close(descriptor)
            descriptor = child
        return descriptor
    except ReleaseLockError:
        if descriptor is not None:
            try:
                os.close(descriptor)
            except OSError:
                pass
        raise
    except OSError as exc:
        if descriptor is not None:
            try:
                os.close(descriptor)
            except OSError:
                pass
        raise ReleaseLockError(
            f"cannot create Release lock directory: {exc}"
        ) from exc


def _open_posix_child_directory(
    parent_descriptor: int, directory: Path, name: str
) -> int:
    flags = (
        os.O_RDONLY
        | getattr(os, "O_CLOEXEC", 0)
        | getattr(os, "O_DIRECTORY", 0)
        | getattr(os, "O_NOFOLLOW", 0)
    )
    try:
        try:
            os.mkdir(name, mode=0o700, dir_fd=parent_descriptor)
        except FileExistsError:
            pass
        descriptor = os.open(name, flags, dir_fd=parent_descriptor)
        opened = os.fstat(descriptor)
        if not stat.S_ISDIR(opened.st_mode) or _is_reparse(opened):
            try:
                os.close(descriptor)
            except OSError:
                pass
            raise ReleaseLockError(
                "Release lock directory violates no-follow containment"
            )
        return descriptor
    except ReleaseLockError:
        raise
    except OSError as exc:
        raise ReleaseLockError(
            f"cannot create Release lock directory {directory}: {exc}"
        ) from exc


def _require_posix_directory_identity(
    descriptor: int,
    path: Path,
    *,
    label: str,
    expected: _DirectoryIdentity | None = None,
) -> _DirectoryIdentity:
    try:
        opened = os.fstat(descriptor)
        named = path.lstat()
    except OSError as exc:
        raise ReleaseLockError(f"{label} identity changed: {exc}") from exc
    if (
        not stat.S_ISDIR(opened.st_mode)
        or not stat.S_ISDIR(named.st_mode)
        or _is_reparse(opened)
        or _is_reparse(named)
    ):
        raise ReleaseLockError(f"{label} violates no-follow containment")
    identity = _DirectoryIdentity(path, opened.st_dev, opened.st_ino)
    if (named.st_dev, named.st_ino) != (identity.device, identity.inode):
        raise ReleaseLockError(f"{label} identity changed")
    if expected is not None and identity != expected:
        raise ReleaseLockError(f"{label} identity changed")
    return identity


def _open_lock_file(
    lock_path: Path, *, directory_descriptor: int | None
) -> BinaryIO:
    if os.name == "nt":
        return _open_windows_lock_file(lock_path)
    assert directory_descriptor is not None
    flags = (
        os.O_RDWR
        | os.O_CREAT
        | getattr(os, "O_CLOEXEC", 0)
        | getattr(os, "O_NOFOLLOW", 0)
    )
    try:
        descriptor = os.open(
            lock_path.name,
            flags,
            0o600,
            dir_fd=directory_descriptor,
        )
    except OSError as exc:
        raise ReleaseLockError(
            f"cannot open no-follow Release lock file: {exc}"
        ) from exc
    try:
        return os.fdopen(descriptor, "r+b", buffering=0)
    except OSError as exc:
        try:
            os.close(descriptor)
        except OSError:
            pass
        raise ReleaseLockError(
            f"cannot wrap Release publication lock: {exc}"
        ) from exc


def _open_windows_lock_file(lock_path: Path) -> BinaryIO:
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
    handle = create_file(
        str(lock_path),
        0x80000000 | 0x40000000 | 0x80,  # GENERIC_READ | WRITE | READ_ATTRIBUTES
        0x1 | 0x2,  # share READ and WRITE, but never DELETE/replacement
        None,
        4,  # OPEN_ALWAYS
        0x00200000,  # FILE_FLAG_OPEN_REPARSE_POINT
        None,
    )
    invalid_handle = ctypes.c_void_p(-1).value
    if handle == invalid_handle:
        error = ctypes.get_last_error()
        raise ReleaseLockError("cannot open no-follow Release lock file") from OSError(
            error, os.strerror(error)
        )
    try:
        descriptor = msvcrt.open_osfhandle(int(handle), os.O_BINARY | os.O_RDWR)
    except OSError as exc:
        try:
            _close_windows_handle(int(handle))
        except ReleaseLockError:
            pass
        raise ReleaseLockError(
            f"cannot wrap Release publication lock handle: {exc}"
        ) from exc
    try:
        return os.fdopen(descriptor, "r+b", buffering=0)
    except OSError as exc:
        try:
            os.close(descriptor)
        except OSError:
            pass
        raise ReleaseLockError(
            f"cannot wrap Release publication lock: {exc}"
        ) from exc


def _require_lock_name_identity(
    lock_file: BinaryIO,
    lock_path: Path,
    *,
    expected: _FileIdentity | None = None,
) -> _FileIdentity:
    try:
        opened = os.fstat(lock_file.fileno())
        named = lock_path.lstat()
    except OSError as exc:
        raise ReleaseLockError("Release lock name identity changed") from exc
    if (
        not stat.S_ISREG(opened.st_mode)
        or not stat.S_ISREG(named.st_mode)
        or _is_reparse(opened)
        or _is_reparse(named)
    ):
        raise ReleaseLockError(
            "Release lock name violates no-follow regular-file containment"
        )
    identity = _FileIdentity(opened.st_dev, opened.st_ino)
    if (named.st_dev, named.st_ino) != (identity.device, identity.inode):
        raise ReleaseLockError("Release lock name identity changed")
    if expected is not None and identity != expected:
        raise ReleaseLockError("Release lock name identity changed")
    return identity


def _lock_file(lock_file: BinaryIO, *, exclusive: bool) -> None:
    if os.name == "nt":
        _lock_windows(lock_file, exclusive=exclusive)
    else:
        _lock_posix(lock_file, exclusive=exclusive)


def _unlock_file(lock_file: BinaryIO) -> None:
    if os.name == "nt":
        _unlock_windows(lock_file)
    else:
        _unlock_posix(lock_file)


def _require_or_create_windows_directory(root: Path, directory: Path) -> None:
    try:
        root_stat = root.lstat()
    except OSError as exc:
        raise ReleaseLockError("repository root is missing") from exc
    if not stat.S_ISDIR(root_stat.st_mode) or _is_reparse(root_stat):
        raise ReleaseLockError("repository root violates no-follow containment")
    current = root
    for part in directory.relative_to(root).parts:
        current = current / part
        try:
            current.mkdir(mode=0o700)
        except FileExistsError:
            pass
        except OSError as exc:
            raise ReleaseLockError("cannot create Release lock directory") from exc
        current_stat = current.lstat()
        if not stat.S_ISDIR(current_stat.st_mode) or _is_reparse(current_stat):
            raise ReleaseLockError(
                "Release lock directory violates no-follow containment"
            )


def _capture_windows_directory_identities(
    root: Path, directory: Path
) -> tuple[_DirectoryIdentity, ...]:
    paths = [root]
    current = root
    for part in directory.relative_to(root).parts:
        current = current / part
        paths.append(current)
    identities: list[_DirectoryIdentity] = []
    for path in paths:
        path_stat = path.lstat()
        if not stat.S_ISDIR(path_stat.st_mode) or _is_reparse(path_stat):
            raise ReleaseLockError(
                "Release lock directory violates no-follow containment"
            )
        identities.append(
            _DirectoryIdentity(path, path_stat.st_dev, path_stat.st_ino)
        )
    return tuple(identities)


def _require_windows_directory_identities(
    root: Path,
    directory: Path,
    expected: tuple[_DirectoryIdentity, ...],
) -> None:
    if _capture_windows_directory_identities(root, directory) != expected:
        raise ReleaseLockError("Release lock directory identity changed")


def _open_windows_directory_handle(path: Path) -> int:
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
        0x80,  # FILE_READ_ATTRIBUTES
        0x1 | 0x2,  # share READ and WRITE, but never DELETE/replacement
        None,
        3,  # OPEN_EXISTING
        0x02000000 | 0x00200000,  # BACKUP_SEMANTICS | OPEN_REPARSE_POINT
        None,
    )
    invalid_handle = ctypes.c_void_p(-1).value
    if handle == invalid_handle:
        error = ctypes.get_last_error()
        raise ReleaseLockError("cannot protect Release lock directory") from OSError(
            error, os.strerror(error)
        )
    return int(handle)


def _lock_posix(lock_file: BinaryIO, *, exclusive: bool) -> None:
    _lock_posix_descriptor(lock_file.fileno(), exclusive=exclusive)


def _unlock_posix(lock_file: BinaryIO) -> None:
    _unlock_posix_descriptor(lock_file.fileno())


def _lock_posix_descriptor(descriptor: int, *, exclusive: bool) -> None:
    import fcntl

    fcntl.flock(  # type: ignore[attr-defined]
        descriptor,
        fcntl.LOCK_EX if exclusive else fcntl.LOCK_SH,  # type: ignore[attr-defined]
    )


def _unlock_posix_descriptor(descriptor: int) -> None:
    import fcntl

    fcntl.flock(descriptor, fcntl.LOCK_UN)  # type: ignore[attr-defined]


def _lock_windows(lock_file: BinaryIO, *, exclusive: bool) -> None:
    import ctypes
    import msvcrt
    from ctypes import wintypes

    class Overlapped(ctypes.Structure):
        _fields_ = [
            ("Internal", ctypes.c_size_t),
            ("InternalHigh", ctypes.c_size_t),
            ("Offset", wintypes.DWORD),
            ("OffsetHigh", wintypes.DWORD),
            ("hEvent", wintypes.HANDLE),
        ]

    lock_file.seek(0)
    overlapped = Overlapped()
    kernel32 = ctypes.WinDLL("kernel32", use_last_error=True)
    lock_file_ex = kernel32.LockFileEx
    lock_file_ex.argtypes = [
        wintypes.HANDLE,
        wintypes.DWORD,
        wintypes.DWORD,
        wintypes.DWORD,
        wintypes.DWORD,
        ctypes.POINTER(Overlapped),
    ]
    lock_file_ex.restype = wintypes.BOOL
    flags = 0x2 if exclusive else 0  # LOCKFILE_EXCLUSIVE_LOCK
    handle = msvcrt.get_osfhandle(lock_file.fileno())
    if not lock_file_ex(handle, flags, 0, 1, 0, ctypes.byref(overlapped)):
        error = ctypes.get_last_error()
        raise ReleaseLockError("cannot acquire Release publication lock") from OSError(
            error, os.strerror(error)
        )


def _unlock_windows(lock_file: BinaryIO) -> None:
    import ctypes
    import msvcrt
    from ctypes import wintypes

    class Overlapped(ctypes.Structure):
        _fields_ = [
            ("Internal", ctypes.c_size_t),
            ("InternalHigh", ctypes.c_size_t),
            ("Offset", wintypes.DWORD),
            ("OffsetHigh", wintypes.DWORD),
            ("hEvent", wintypes.HANDLE),
        ]

    overlapped = Overlapped()
    kernel32 = ctypes.WinDLL("kernel32", use_last_error=True)
    unlock_file_ex = kernel32.UnlockFileEx
    unlock_file_ex.argtypes = [
        wintypes.HANDLE,
        wintypes.DWORD,
        wintypes.DWORD,
        wintypes.DWORD,
        ctypes.POINTER(Overlapped),
    ]
    unlock_file_ex.restype = wintypes.BOOL
    handle = msvcrt.get_osfhandle(lock_file.fileno())
    if not unlock_file_ex(handle, 0, 1, 0, ctypes.byref(overlapped)):
        error = ctypes.get_last_error()
        raise ReleaseLockError("cannot release Release publication lock") from OSError(
            error, os.strerror(error)
        )


def _close_windows_handle(handle: int) -> None:
    import ctypes

    kernel32 = ctypes.WinDLL("kernel32", use_last_error=True)
    if not kernel32.CloseHandle(ctypes.c_void_p(handle)):
        error = ctypes.get_last_error()
        raise ReleaseLockError("cannot release Release lock directory") from OSError(
            error, os.strerror(error)
        )


def _is_reparse(path_stat: os.stat_result) -> bool:
    attributes = getattr(path_stat, "st_file_attributes", 0)
    reparse_flag = getattr(stat, "FILE_ATTRIBUTE_REPARSE_POINT", 0x400)
    return bool(attributes & reparse_flag) or stat.S_ISLNK(path_stat.st_mode)
