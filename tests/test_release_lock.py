from __future__ import annotations

import os
import subprocess
import sys
import threading
import time
from pathlib import Path

import pytest

from hk_movie_rag import release_lock
from hk_movie_rag.release_lock import ReleaseLockError, release_guard


@pytest.mark.skipif(
    os.name == "nt",
    reason="POSIX flock namespace replacement protocol is not available on Windows",
)
def test_posix_lock_directory_replacement_cannot_create_split_brain(
    tmp_path: Path,
) -> None:
    """Breaks if replacing .locks lets a second governed process enter."""
    child_started = tmp_path / "child-started"
    child_entered = tmp_path / "child-entered"
    script = """
import sys
from pathlib import Path

from hk_movie_rag.release_lock import release_guard

root = Path(sys.argv[1])
started = Path(sys.argv[2])
entered = Path(sys.argv[3])
started.write_text("started", encoding="utf-8")
with release_guard(root, "v0.1", exclusive=True):
    entered.write_text("entered", encoding="utf-8")
"""
    process: subprocess.Popen[str] | None = None
    first_exit_error: ReleaseLockError | None = None
    try:
        try:
            with release_guard(tmp_path, "v0.1", exclusive=True):
                lock_directory = tmp_path / "data" / "release" / ".locks"
                displaced = lock_directory.with_name(".locks-displaced")
                lock_directory.rename(displaced)
                lock_directory.mkdir(mode=0o700)
                process = subprocess.Popen(
                    [
                        sys.executable,
                        "-c",
                        script,
                        str(tmp_path),
                        str(child_started),
                        str(child_entered),
                    ],
                    cwd=Path(__file__).resolve().parents[1],
                    text=True,
                )
                deadline = time.monotonic() + 5
                while not child_started.exists() and time.monotonic() < deadline:
                    time.sleep(0.01)
                assert child_started.exists()
                assert not child_entered.exists()
                time.sleep(0.25)
                assert not child_entered.exists()
        except ReleaseLockError as exc:
            first_exit_error = exc

        assert first_exit_error is not None
        assert "directory" in str(first_exit_error) or "identity" in str(first_exit_error)
        assert process is not None
        assert process.wait(timeout=5) == 0
        assert child_entered.exists()
    finally:
        if process is not None and process.poll() is None:
            process.kill()
            process.wait(timeout=5)


def test_lock_final_leaf_symlink_never_creates_outside_target(tmp_path: Path) -> None:
    lock_directory = tmp_path / "data" / "release" / ".locks"
    lock_directory.mkdir(parents=True)
    outside = tmp_path / "outside.lock"
    lock_path = lock_directory / "v0.1.lock"
    try:
        lock_path.symlink_to(outside)
    except OSError as exc:
        pytest.skip(f"cannot create lock symlink fixture: {exc}")

    with (
        pytest.raises(ReleaseLockError, match="no-follow|lock"),
        release_guard(tmp_path, "v0.1", exclusive=True),
    ):
        pytest.fail("a final-leaf link must never become the authority lock")

    assert not outside.exists()
    assert lock_path.is_symlink()


@pytest.mark.skipif(os.name != "nt", reason="directory junctions are Windows-only")
def test_lock_final_leaf_junction_never_touches_outside_tree(tmp_path: Path) -> None:
    lock_directory = tmp_path / "data" / "release" / ".locks"
    lock_directory.mkdir(parents=True)
    outside = tmp_path / "outside"
    outside.mkdir()
    lock_path = lock_directory / "v0.1.lock"
    result = subprocess.run(
        ["cmd", "/c", "mklink", "/J", str(lock_path), str(outside)],
        capture_output=True,
        text=True,
        encoding="utf-8",
        errors="replace",
        check=False,
    )
    if result.returncode != 0:
        pytest.skip(f"cannot create lock junction fixture: {result.stderr}")
    try:
        with (
            pytest.raises(ReleaseLockError),
            release_guard(tmp_path, "v0.1", exclusive=True),
        ):
            pytest.fail("a final-leaf junction must never be opened")
        assert list(outside.iterdir()) == []
    finally:
        os.rmdir(lock_path)


def test_lock_name_replacement_cannot_create_split_brain(tmp_path: Path) -> None:
    entered_second = threading.Event()
    second_finished = threading.Event()
    second_errors: list[BaseException] = []
    replacement_succeeded = False
    first_exit_error: ReleaseLockError | None = None

    def second_publisher() -> None:
        try:
            with release_guard(tmp_path, "v0.1", exclusive=True):
                entered_second.set()
        except OSError as exc:  # pragma: no cover - asserted below
            second_errors.append(exc)
        finally:
            second_finished.set()

    contender = threading.Thread(target=second_publisher, daemon=True)
    try:
        with release_guard(tmp_path, "v0.1", exclusive=True):
            lock_path = tmp_path / "data" / "release" / ".locks" / "v0.1.lock"
            replacement = lock_path.with_name("replacement.lock")
            replacement.write_bytes(b"replacement")
            try:
                os.replace(replacement, lock_path)
            except PermissionError:
                replacement.unlink()
            else:
                replacement_succeeded = True
            contender.start()
            assert not entered_second.wait(timeout=0.25)
    except ReleaseLockError as exc:
        first_exit_error = exc

    assert contender.is_alive() or second_finished.is_set()
    assert second_finished.wait(timeout=5)
    contender.join(timeout=1)
    assert second_errors == []
    assert entered_second.is_set()
    if replacement_succeeded:
        assert first_exit_error is not None
        assert "identity" in str(first_exit_error) or "name" in str(first_exit_error)
    else:
        assert first_exit_error is None
    assert (tmp_path / "data" / "release" / ".locks" / "v0.1.lock").exists()


@pytest.mark.parametrize("drift_call,body_enters", [(2, False), (3, True)])
def test_lock_name_is_rebound_after_acquire_and_at_exit(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    drift_call: int,
    body_enters: bool,
) -> None:
    lock_directory = tmp_path / "data" / "release" / ".locks"
    lock_directory.mkdir(parents=True)
    lock_path = lock_directory / "v0.1.lock"
    lock_path.write_bytes(b"")
    different = lock_directory / "different.lock"
    different.write_bytes(b"")
    different_stat = different.lstat()
    original_lstat = Path.lstat
    lock_name_stats = 0
    entered = False

    def drifting_lstat(path: Path) -> os.stat_result:
        nonlocal lock_name_stats
        if Path(os.path.abspath(path)) == lock_path:
            lock_name_stats += 1
            if lock_name_stats >= drift_call:
                return different_stat
        return original_lstat(path)

    monkeypatch.setattr(release_lock.Path, "lstat", drifting_lstat)

    with (
        pytest.raises(ReleaseLockError, match="identity|name"),
        release_guard(tmp_path, "v0.1", exclusive=True),
    ):
        entered = True

    assert entered is body_enters
    assert lock_name_stats >= drift_call


def test_unlock_failure_closes_handle_without_deleting_lock_name(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    original_unlock = release_lock._unlock_file

    def unlock_then_fail(lock_file: object) -> None:
        original_unlock(lock_file)  # type: ignore[arg-type]
        raise ReleaseLockError("synthetic unlock failure")

    monkeypatch.setattr(release_lock, "_unlock_file", unlock_then_fail)
    with (
        pytest.raises(
            ReleaseLockError, match="release Release publication lock"
        ),
        release_guard(tmp_path, "v0.1", exclusive=True),
    ):
        pass

    lock_path = tmp_path / "data" / "release" / ".locks" / "v0.1.lock"
    identity = (lock_path.stat().st_dev, lock_path.stat().st_ino)
    monkeypatch.setattr(release_lock, "_unlock_file", original_unlock)
    with release_guard(tmp_path, "v0.1", exclusive=True):
        assert (lock_path.stat().st_dev, lock_path.stat().st_ino) == identity


@pytest.mark.parametrize("phase", ["identity", "acquire", "release"])
def test_raw_kernel_and_identity_os_errors_are_normalized(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    phase: str,
) -> None:
    """Breaks if raw kernel/identity failures escape as ungoverned OSError."""
    if phase == "identity":
        def fail_identity(*args: object, **kwargs: object) -> object:
            del args, kwargs
            raise OSError("synthetic raw identity failure")

        monkeypatch.setattr(release_lock, "_require_lock_name_identity", fail_identity)
    elif phase == "acquire":
        def fail_acquire(*args: object, **kwargs: object) -> None:
            del args, kwargs
            raise OSError("synthetic raw acquire failure")

        monkeypatch.setattr(release_lock, "_lock_file", fail_acquire)
    else:
        def fail_release(*args: object, **kwargs: object) -> None:
            del args, kwargs
            raise OSError("synthetic raw release failure")

        monkeypatch.setattr(release_lock, "_unlock_file", fail_release)

    with (
        pytest.raises(ReleaseLockError, match=f"synthetic raw {phase} failure"),
        release_guard(tmp_path, "v0.1", exclusive=True),
    ):
        pass


@pytest.mark.skipif(os.name != "nt", reason="open_osfhandle is Windows-only")
def test_windows_open_osfhandle_oserror_is_normalized(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """Breaks if CRT handle wrapping escapes the ReleaseLockError boundary."""
    import msvcrt

    def fail_wrap(*args: object, **kwargs: object) -> int:
        del args, kwargs
        raise OSError("synthetic raw open_osfhandle failure")

    monkeypatch.setattr(msvcrt, "open_osfhandle", fail_wrap)
    with (
        pytest.raises(ReleaseLockError, match="synthetic raw open_osfhandle failure"),
        release_guard(tmp_path, "v0.1", exclusive=True),
    ):
        pass


@pytest.mark.skipif(os.name != "nt", reason="Windows directory identity protocol only")
def test_windows_exit_identity_oserror_is_normalized(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """Breaks if a final Windows path-identity OSError bypasses governance."""
    original = release_lock._require_windows_directory_identities
    calls = 0

    def fail_on_exit(*args: object, **kwargs: object) -> None:
        nonlocal calls
        calls += 1
        if calls == 2:
            raise OSError("synthetic raw directory identity failure")
        original(*args, **kwargs)

    monkeypatch.setattr(
        release_lock, "_require_windows_directory_identities", fail_on_exit
    )
    with (
        pytest.raises(ReleaseLockError, match="synthetic raw directory identity failure"),
        release_guard(tmp_path, "v0.1", exclusive=True),
    ):
        pass
