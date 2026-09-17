from __future__ import annotations

import os
import sqlite3
import warnings
from collections.abc import Generator
from pathlib import Path
from typing import Any

import pytest

from antigona.database import Database
from antigona.worker.hitl import (
    ConfirmationPolicy,
    get_confirmation_policy,
    set_confirmation_policy,
)

# ── File-descriptor hygiene ──────────────────────────────────────────────────
#
# The suite runs ~4700 tests under ``ulimit -Sn 1024``.  Two kinds of object
# created by a test keep file descriptors open after that test has finished, and
# garbage collection does not release either of them:
#
#   * ``starlette.testclient.TestClient`` — ``__enter__`` starts an AnyIO
#     blocking portal: a real event loop in a dedicated thread (1 ``eventpoll``
#     + 1 ``socketpair``) plus the application lifespan task.  Only ``__exit__``
#     stops it, so a fixture written as
#     ``client = TestClient(app); client.__enter__()`` leaks the portal, its
#     loop, its three descriptors and everything the app holds on to.
#   * SQLite handles — ``SessionDatabase`` (aiosqlite), ad-hoc
#     ``sqlite3.connect`` call sites and SQLAlchemy engines are closed only on
#     the happy path of the code under test.  An aiosqlite worker thread keeps
#     its handle (``.db`` + ``-wal`` + ``-shm`` = 3 descriptors) alive for the
#     rest of the process even after the owning object is dropped.
#
# Together that is 6+ descriptors per test, so a few thousand tests in the soft
# limit is exhausted and every later test dies with
# ``OSError: [Errno 24] Too many open files`` — a failure that has nothing to do
# with the code under test.  The trackers below record what the running test
# created; ``release_test_file_descriptors`` releases it once the test is over.
_TRACKED_CLIENTS: list[Any] = []
_TRACKED_ENGINES: list[Any] = []
_TRACKED_CONNECTIONS: list[sqlite3.Connection] = []


@pytest.fixture(autouse=True, scope="session")
def track_file_descriptor_holders() -> Generator[None, None, None]:
    """Record every TestClient / SQLAlchemy Engine / SQLite handle opened."""
    import sqlalchemy.engine

    with warnings.catch_warnings():
        # Importing starlette's TestClient emits a deprecation warning; without
        # this it would be reported against whichever test happened to run first.
        warnings.simplefilter("ignore")
        from starlette.testclient import TestClient

    original_connect = sqlite3.connect
    # ``Engine.__new__`` rather than ``Engine.__init__``: SQLAlchemy validates the
    # ``create_engine()`` keyword arguments by reflecting the ``__init__``
    # signature (``util.get_cls_kwargs``), so wrapping ``__init__`` with a
    # ``*args, **kwargs`` wrapper makes ``create_engine(..., echo=...)`` raise
    # "Invalid argument(s) 'echo' sent to create_engine()".  ``__new__`` is not
    # reflected on, and every Engine goes through it.
    original_engine_new = sqlalchemy.engine.Engine.__new__
    original_client_enter = TestClient.__enter__
    original_client_exit = TestClient.__exit__

    def tracked_connect(*args: Any, **kwargs: Any) -> sqlite3.Connection:
        connection = original_connect(*args, **kwargs)
        _TRACKED_CONNECTIONS.append(connection)
        return connection

    def tracked_engine_new(cls: Any, *args: Any, **kwargs: Any) -> Any:
        engine = original_engine_new(cls)
        _TRACKED_ENGINES.append(engine)
        return engine

    def tracked_client_enter(self: Any) -> Any:
        _TRACKED_CLIENTS.append(self)
        return original_client_enter(self)

    def tracked_client_exit(self: Any, *args: Any) -> Any:
        if self in _TRACKED_CLIENTS:
            _TRACKED_CLIENTS.remove(self)
        return original_client_exit(self, *args)

    patches: list[tuple[Any, str, Any]] = [
        (sqlite3, "connect", tracked_connect),
        (sqlalchemy.engine.Engine, "__new__", tracked_engine_new),
        (TestClient, "__enter__", tracked_client_enter),
        (TestClient, "__exit__", tracked_client_exit),
    ]
    originals = [(target, name, getattr(target, name)) for target, name, _ in patches]
    for target, name, replacement in patches:
        setattr(target, name, replacement)
    try:
        yield
    finally:
        for target, name, original in reversed(originals):
            setattr(target, name, original)
        _TRACKED_CLIENTS.clear()
        _TRACKED_ENGINES.clear()
        _TRACKED_CONNECTIONS.clear()


@pytest.fixture(autouse=True, scope="function")
def release_test_file_descriptors() -> Generator[None, None, None]:
    """Close what this test opened but did not close itself."""
    del _TRACKED_CLIENTS[:]
    del _TRACKED_ENGINES[:]
    del _TRACKED_CONNECTIONS[:]
    yield
    # 1. Starlette's public ``__exit__`` for clients the test entered but never
    #    exited: app lifespan shutdown, then portal stop, which ends the portal
    #    thread and closes its event loop.
    clients = list(_TRACKED_CLIENTS)
    _TRACKED_CLIENTS.clear()
    for client in clients:
        try:
            client.__exit__(None, None, None)
        except Exception:  # already shut down, or never fully started
            pass
    # 2. Empty the SQLAlchemy pools.  A disposed engine stays usable — it opens
    #    fresh connections on demand — so this is safe even for a shared engine.
    for engine in _TRACKED_ENGINES:
        try:
            if getattr(engine.dialect, "is_async", False):
                # Async (aiosqlite) pools dispose through a coroutine that cannot be
                # awaited from a teardown hook; step 3 releases their descriptors.
                continue
            engine.dispose()
        except Exception:
            # A partially constructed Engine (its ``__init__`` raised after
            # ``__new__`` was tracked) has no dialect yet, and an async pool refuses
            # a synchronous dispose — neither is worth failing a passing test over.
            pass
    del _TRACKED_ENGINES[:]
    # 3. Close the SQLite handles themselves.  This is what actually releases the
    #    ``.db`` / ``-wal`` / ``-shm`` descriptors of a leaked aiosqlite
    #    connection whose worker thread keeps it alive; closing an already closed
    #    handle is a no-op.
    for connection in _TRACKED_CONNECTIONS:
        try:
            connection.close()
        except Exception:
            pass
    del _TRACKED_CONNECTIONS[:]


@pytest.fixture(autouse=True, scope="function")
def isolate_runtime_roots(
    tmp_path_factory: pytest.TempPathFactory,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Keep runtime state disposable but outside filesystem test boundaries."""
    state_root = tmp_path_factory.mktemp("state_root")
    monkeypatch.setenv("ANTIGONA_STATE_ROOT", str(state_root))


@pytest.fixture(autouse=True, scope="function")
def acknowledge_weaker_sandbox_runtime(monkeypatch: pytest.MonkeyPatch) -> None:
    """Run the suite in the ACKNOWLEDGED weaker-runtime mode.

    Sandboxed-command unit tests stub ``subprocess`` and exercise command
    normalisation / output capping, not the isolation gate; requiring a live
    gVisor for them would make the suite non-hermetic.  The gate itself is
    verified by ``tests/sandbox/test_isolation_failclosed.py``, which explicitly
    DELETES this env var to prove the fail-closed DEFAULT.
    """
    monkeypatch.setenv("ANTIGONA_SANDBOX_ALLOW_RUNC_FALLBACK", "1")


@pytest.fixture(autouse=True, scope="function")
def isolate_telegram_turn_ledger(
    tmp_path_factory: pytest.TempPathFactory,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Give every test its own durable Telegram turn ledger.

    The production default lives at ``<tasks_dir>/telegram_turns.db`` and is
    deliberately persistent — that is what makes "exactly one agent run per
    message" survive a restart.  Left unredirected it would also persist
    *between tests and between runs*, so a second test reusing a chat/message
    id would silently replay the first one's cached payload instead of calling
    the Gateway.
    """
    ledger = tmp_path_factory.mktemp("turn_ledger") / "telegram_turns.db"
    monkeypatch.setenv("ANTIGONA_TELEGRAM_TURN_LEDGER", str(Path(ledger)))


@pytest.fixture(autouse=True, scope="function")
def track_and_dispose_databases(monkeypatch: pytest.MonkeyPatch) -> Generator[None, None, None]:
    """Fixture to track and dispose of all Database instances created during a test."""
    instances: list[Database] = []
    original_init = Database.__init__

    def wrapped_init(self: Database, *args: Any, **kwargs: Any) -> None:
        original_init(self, *args, **kwargs)
        instances.append(self)

    monkeypatch.setattr(Database, "__init__", wrapped_init)
    yield
    for instance in instances:
        instance.dispose()


@pytest.fixture(autouse=True, scope="function")
def restore_confirmation_policy() -> Generator[None, None, None]:
    """Restore process-global confirmation policy after each test."""
    previous = get_confirmation_policy()
    previous_copy = ConfirmationPolicy(previous.mode, previous.timeout_seconds)
    yield
    set_confirmation_policy(previous_copy)

# Warn (never fail) if descriptors still accumulate across a whole session: that
# is the signature of a new leak, and under ``ulimit -Sn 1024`` it would take the
# late-running test modules down with ``OSError: [Errno 24]`` before anyone
# notices why.
_FD_GROWTH_WARN_THRESHOLD = 256
_FD_BASELINE = 0


def _open_fd_count() -> int:
    try:
        return len(os.listdir("/proc/self/fd"))
    except OSError:  # not Linux, or /proc unavailable
        return 0


def pytest_sessionstart(session: pytest.Session) -> None:
    global _FD_BASELINE
    _FD_BASELINE = _open_fd_count()


def pytest_sessionfinish(session: pytest.Session, exitstatus: int) -> None:
    growth = _open_fd_count() - _FD_BASELINE
    if growth > _FD_GROWTH_WARN_THRESHOLD:
        warnings.warn(
            f"test session leaked {growth} file descriptors "
            f"({_FD_BASELINE} -> {_open_fd_count()}); see "
            "tests/conftest.py::track_file_descriptor_holders",
            UserWarning,
            stacklevel=1,
        )


# ── Checkout-pollution guard ─────────────────────────────────────────────────
#
# ``<checkout>/.memory`` is the gitignored *development default* of the file
# memory root (``antigona.memory.file_memory.MEMORY_DIR``).  No test in this
# suite may create it: a configured runtime root (``ANTIGONA_STATE_ROOT``) must
# win, and an immutable deployment must fail closed instead of falling back to
# the read-only code root.  A stray ``<checkout>/.memory`` is nevertheless
# possible — an out-of-suite CLI/agent run in the same tree creates it — and it
# used to turn into a spurious failure of
# ``tests/unit/test_memory_runtime_root.py::test_no_memory_dir_created_under_source_root``
# (see defect B44).  The test itself is now hermetic; this guard is the loud
# counterpart that catches the *suite* doing it.  It fails ONLY when the path
# was absent at session start and is present at session end, and the failure
# message names the evidence (statx birth time of the directory and its files,
# plus every process whose cwd is the checkout and its command line) so the next
# pollution can be attributed instead of re-blamed on the suite.  It is
# deliberately read-only and takes no snapshot beyond a single ``exists()``
# check, so it is cheap and cannot change behaviour for other tests.

_CHECKOUT_ROOT = Path(__file__).resolve().parents[1]
_CHECKOUT_MEMORY_DIR = _CHECKOUT_ROOT / ".memory"


def _stat_line(path: Path) -> str:
    """One-line ``stat`` evidence for *path* (birth time when available)."""
    try:
        import subprocess

        proc = subprocess.run(
            ["stat", "-c", "%n | birth=%w | mtime=%y | inode=%i | size=%s", str(path)],
            capture_output=True,
            text=True,
            timeout=10,
        )
        if proc.returncode == 0 and proc.stdout.strip():
            return proc.stdout.strip()
    except Exception:  # stat(1) missing or failed — fall back to os.stat
        pass
    try:
        st = path.stat()
    except OSError as exc:
        return f"{path} | <stat failed: {exc!r}>"
    return (
        f"{path} | mtime={st.st_mtime_ns} | ctime={st.st_ctime_ns} "
        f"| inode={st.st_ino} | size={st.st_size} | mode={oct(st.st_mode & 0o777)}"
    )


def _checkout_cwd_processes() -> list[str]:
    """``pid cwd cmdline`` for every process whose cwd is inside the checkout."""
    try:
        pids = [name for name in os.listdir("/proc") if name.isdigit()]
    except OSError:
        return []
    root = str(_CHECKOUT_ROOT)
    found: list[str] = []
    for pid in pids:
        try:
            cwd = os.readlink(f"/proc/{pid}/cwd")
        except OSError:
            continue
        if cwd != root and not cwd.startswith(root + os.sep):
            continue
        try:
            with open(f"/proc/{pid}/cmdline", "rb") as handle:
                cmdline = (
                    handle.read().replace(b"\x00", b" ").decode("utf-8", "replace").strip()
                )
        except OSError:
            cmdline = "<cmdline unavailable>"
        found.append(f"pid={pid} cwd={cwd} cmdline={cmdline!r}")
    return found


def _checkout_pollution_report(target: Path) -> str:
    """Loud failure message naming the pollution and its likely actor."""
    lines = [
        "CHECKOUT POLLUTION (defect B44): this test session created the "
        "gitignored file-memory default",
        f"    {target}",
        "which did NOT exist at session start. Nothing in the suite may write "
        "into the code root;",
        "ANTIGONA_STATE_ROOT must route memory outside the checkout. Evidence:",
        f"  dir   {_stat_line(target)}",
    ]
    try:
        children = sorted(target.iterdir())
    except OSError as exc:
        children = []
        lines.append(f"  <cannot list children: {exc!r}>")
    for child in children[:50]:
        lines.append(f"  file  {_stat_line(child)}")
    if len(children) > 50:
        lines.append(f"  ... {len(children) - 50} more entries")
    processes = _checkout_cwd_processes()
    if processes:
        lines.append("  processes whose cwd is the checkout:")
        lines.extend(f"    {entry}" for entry in processes)
    else:
        lines.append("  processes whose cwd is the checkout: none (actor already exited)")
    return "\n".join(lines)


@pytest.fixture(autouse=True, scope="session")
def guard_checkout_memory_dir_pollution() -> Generator[None, None, None]:
    """Fail the session if this run creates ``<checkout>/.memory``.

    Robust by construction: a tree without ``pyproject.toml`` (tests not run
    from the checkout) or a code root this process cannot write to is not
    policed at all, and an OSError while probing disarms the guard — it must
    never fail spuriously.  A pre-existing directory is recorded as such and
    accepted.
    """
    try:
        if not (_CHECKOUT_ROOT / "pyproject.toml").is_file():
            yield
            return
        if not os.access(_CHECKOUT_ROOT, os.W_OK):
            yield
            return
        existed_before = _CHECKOUT_MEMORY_DIR.exists()
    except OSError:
        yield
        return

    yield

    if existed_before:
        return
    try:
        if not _CHECKOUT_MEMORY_DIR.exists():
            return
    except OSError:
        return
    pytest.fail(_checkout_pollution_report(_CHECKOUT_MEMORY_DIR), pytrace=False)
