"""Hardened single-instance pid-lock regressions (fail-closed contract).

The bot must never report "another instance is already running" when the real
failure is that the pid lock could not be *created* (read-only code root,
EACCES/EPERM, missing pid directory, ...). Such failures are configuration
errors and must fail closed with an actionable, non-ambiguous error.
"""

from __future__ import annotations

import errno
import os
import subprocess
import sys
import time
import traceback

import pytest

from antigona.core import paths
from antigona.transport.telegram import PidLockError, acquire_pid_lock

# ── resolution ────────────────────────────────────────────────────────────────


def test_default_resolution_goes_through_paths_pid_file(
    tmp_path: pytest.TempPathFactory, monkeypatch: pytest.MonkeyPatch
) -> None:
    """No argument => ``paths.pid_file()``, which honours ANTIGONA_PID_FILE."""
    pid_path = tmp_path / "governed" / "bot.pid"
    pid_path.parent.mkdir()
    monkeypatch.setenv("ANTIGONA_PID_FILE", str(pid_path))

    assert paths.pid_file() == pid_path

    lock = acquire_pid_lock()
    try:
        assert lock is not None
        assert pid_path.is_file()
        assert pid_path.read_text().strip() == str(os.getpid())
    finally:
        if lock is not None:
            lock.close()


def test_paths_pid_file_default_without_env(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.delenv("ANTIGONA_PID_FILE", raising=False)
    assert str(paths.pid_file()) == "/tmp/antigona_bot.pid"


# ── fail-closed setup failures (never a phantom "another instance") ────────────


@pytest.mark.parametrize(
    ("err", "code"),
    [
        (errno.EROFS, "EROFS"),
        (errno.EACCES, "EACCES"),
        (errno.EPERM, "EPERM"),
    ],
)
def test_read_only_or_denied_code_root_fails_closed(
    tmp_path: pytest.TempPathFactory,
    monkeypatch: pytest.MonkeyPatch,
    err: int,
    code: str,
) -> None:
    """A read-only fs / permission denial raises PidLockError, never None."""
    pid_path = tmp_path / "bot.pid"

    real_open = os.open

    def _denied(path: str | bytes | os.PathLike[str], flags: int, *a: object) -> int:
        raise OSError(err, os.strerror(err), str(path))

    monkeypatch.setattr(os, "open", _denied)
    try:
        with pytest.raises(PidLockError) as excinfo:
            acquire_pid_lock(str(pid_path))
    finally:
        monkeypatch.setattr(os, "open", real_open)

    msg = str(excinfo.value)
    assert code in msg
    assert "another instance" not in msg.lower()
    # actionable: names the offending path and a governed writable location
    assert str(pid_path) in msg
    assert "ANTIGONA_PID_FILE" in msg
    assert not pid_path.exists()


def test_missing_pid_directory_fails_closed(tmp_path: pytest.TempPathFactory) -> None:
    """A non-existent pid directory is a setup error -> PidLockError (ENOENT)."""
    pid_path = tmp_path / "does" / "not" / "exist" / "bot.pid"
    with pytest.raises(PidLockError) as excinfo:
        acquire_pid_lock(str(pid_path))
    msg = str(excinfo.value)
    assert "ENOENT" in msg
    assert "another instance" not in msg.lower()


def test_unwritable_pid_directory_fails_closed_for_service_user(
    tmp_path: pytest.TempPathFactory,
) -> None:
    """Real EACCES: a directory the unprivileged service user cannot write to.

    The privilege drop is explicit and *complete* — ``os.setgroups([])`` first,
    then ``setgid``/``setuid`` — and runs inside a forked child that calls the
    already-imported ``acquire_pid_lock`` **in-process**. Nothing is ``exec``'d,
    so the outcome cannot depend on the caller's supplementary groups (the old
    ``preexec_fn`` child inherited group 0 and therefore passed) nor on the
    interpreter being reachable through a ``/opt/antigona-home`` traversal.
    """
    import pwd

    if os.geteuid() != 0:
        pytest.skip("needs root to drop privileges")

    nobody = pwd.getpwnam("nobody")
    ro_dir = tmp_path / "ro"
    ro_dir.mkdir()
    ro_dir.chmod(0o500)
    pid_path = ro_dir / "bot.pid"
    read_fd, write_fd = os.pipe()
    try:
        pid = os.fork()
        if pid == 0:  # pragma: no cover — child never returns into pytest
            try:
                os.close(read_fd)
                # Explicit, complete privilege drop: supplementary groups first,
                # otherwise a group-0 caller would keep write access implicitly.
                os.setgroups([])
                os.setgid(nobody.pw_gid)
                os.setuid(nobody.pw_uid)
                outcome = acquire_pid_lock(str(pid_path))
                if outcome is None:
                    report = "None -> reported as another instance\n"
                else:
                    outcome.close()
                    report = f"acquire_pid_lock unexpectedly succeeded: {pid_path}\n"
            except BaseException:  # noqa: BLE001 — surface the real failure verbatim
                report = traceback.format_exc()
            os.write(write_fd, report.encode())
            os.close(write_fd)
            os._exit(1)
        os.close(write_fd)
        chunks: list[bytes] = []
        while True:
            chunk = os.read(read_fd, 65536)
            if not chunk:
                break
            chunks.append(chunk)
        os.close(read_fd)
        _, status = os.waitpid(pid, 0)
        report = b"".join(chunks).decode(errors="replace")
        rc = os.waitstatus_to_exitcode(status)
        assert rc != 0, report
        assert "PidLockError" in report, report
        assert "another instance" not in report.lower(), report
    finally:
        ro_dir.chmod(0o700)


def pathlib_src() -> str:
    from pathlib import Path

    return str(Path(__file__).resolve().parents[2] / "src")


# ── healthy paths still work ──────────────────────────────────────────────────


def test_stale_pid_file_is_reclaimed(tmp_path: pytest.TempPathFactory) -> None:
    """A leftover pid file from a dead process is reclaimed, not treated as a peer."""
    pid_path = tmp_path / "bot.pid"
    pid_path.write_text("999999\n")  # stale, no holder
    lock = acquire_pid_lock(str(pid_path))
    try:
        assert lock is not None
        assert pid_path.read_text().strip() == str(os.getpid())
    finally:
        if lock is not None:
            lock.close()


def test_genuine_concurrent_holder_same_process_returns_none(
    tmp_path: pytest.TempPathFactory,
) -> None:
    pid_path = str(tmp_path / "bot.pid")
    lock1 = acquire_pid_lock(pid_path)
    assert lock1 is not None
    try:
        assert acquire_pid_lock(pid_path) is None
    finally:
        lock1.close()
    # released -> reacquirable
    lock3 = acquire_pid_lock(pid_path)
    assert lock3 is not None
    lock3.close()


def test_genuine_concurrent_holder_across_process_returns_none(
    tmp_path: pytest.TempPathFactory,
) -> None:
    """Cross-process proof: a real holder => None (the ONLY 'another instance')."""
    pid_path = tmp_path / "bot.pid"
    holder = subprocess.Popen(
        [
            sys.executable,
            "-c",
            (
                f"import sys, time; sys.path.insert(0, {pathlib_src()!r});"
                "from antigona.transport.telegram import acquire_pid_lock;"
                f"lock = acquire_pid_lock({str(pid_path)!r});"
                "assert lock is not None, 'holder failed to lock';"
                "print('LOCKED', flush=True); time.sleep(30)"
            ),
        ],
        stdout=subprocess.PIPE,
        text=True,
    )
    try:
        assert holder.stdout is not None
        line = holder.stdout.readline()
        assert "LOCKED" in line, line
        deadline = time.time() + 5
        while time.time() < deadline:
            if acquire_pid_lock(str(pid_path)) is None:
                break
            time.sleep(0.05)
        else:
            raise AssertionError("expected None for a genuine concurrent holder")
    finally:
        holder.terminate()
        holder.wait(timeout=10)
    # holder gone -> lock is free again
    lock = acquire_pid_lock(str(pid_path))
    assert lock is not None
    lock.close()


def test_pid_lock_error_message_is_actionable_not_ambiguous() -> None:
    from antigona.transport.telegram import _pid_lock_hint

    msg = _pid_lock_hint("/opt/antigona-home/.antigona/.antigona/bot.pid", "OSError errno=30 (EROFS)")
    assert "EROFS" in msg
    assert "/opt/antigona-home/.antigona/.antigona/bot.pid" in msg
    assert "ANTIGONA_PID_FILE" in msg
    assert "another instance" not in msg.lower()


def test_bot_main_fails_closed_on_pid_lock_setup_failure(
    tmp_path: pytest.TempPathFactory, monkeypatch: pytest.MonkeyPatch
) -> None:
    """main() exits non-zero with an actionable error, never 'another instance'."""
    from antigona.channels.telegram import bot as bot_mod

    def _boom(path: str | None = None) -> None:
        raise bot_mod.PidLockError("cannot establish the lock at '/x/bot.pid': EROFS")

    monkeypatch.setattr(bot_mod, "acquire_pid_lock", _boom)
    with pytest.raises(SystemExit) as excinfo:
        bot_mod.main()
    assert excinfo.value.code == 2
