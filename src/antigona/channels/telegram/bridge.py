"""Thin Telegram transport orchestration; no routing and no reasoning.

The bridge is the single Telegram-side orchestration owner.  It normalises an
update into a canonical :class:`TurnIdentity`, enforces durable exactly-once
semantics through :mod:`antigona.channels.telegram.turn_ledger`, serialises
turns per chat (FIFO, one active), bounds both the per-chat queue and the
global number of live chat sessions, and then makes exactly one call into the
existing singleton Gateway/AntigonaBrain runtime.

Everything that is *not* orchestration lives elsewhere: delivery belongs to
``antigona.presentation.presenter``, reasoning belongs to the core.  The helper
functions at the bottom of this module are pure validation primitives shared by
the inbound-attachment and outbound-artifact paths.
"""

from __future__ import annotations

import asyncio
import logging
import os
import re
import secrets
import stat
from collections import deque
from collections.abc import Sequence
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any, Final, Protocol

from antigona.channels.telegram.turn_ledger import (
    AmbiguousTurn,
    ClaimOutcome,
    InMemoryTurnLedger,
    QueueStatus,
    TurnLedger,
)

logger = logging.getLogger(__name__)

# Bounded shutdown grace for in-flight drain workers (seconds).
_CLOSE_GRACE_SECONDS = 5.0

__all__ = [
    "AmbiguousTurn",
    "AttachmentRejected",
    "BridgeCapacityExceeded",
    "BridgeOverflow",
    "BridgeResult",
    "DialogueGateway",
    "TelegramBridge",
    "TurnCancelled",
    "TurnIdentity",
    "ValidatedArtifact",
    "discard_inbound_file",
    "ensure_task_tag_in_first_paragraph",
    "extract_task_tag",
    "finalize_inbound_file",
    "prepare_inbound_file",
    "prepare_inbound_path",
    "resolve_outbound_artifact",
    "sanitize_filename",
    "split_telegram_html",
    "split_telegram_text",
    "utf16_length",
]

#: Telegram caps a text message at 4096 UTF-16 code units.
TELEGRAM_TEXT_LIMIT: Final = 4096

#: Default ceiling for both inbound downloads and outbound artifacts.
DEFAULT_MAX_FILE_BYTES: Final = 20 * 1024 * 1024

#: Default number of chat sessions that may be live at the same time.
DEFAULT_MAX_SESSIONS: Final = 64

#: Default per-chat FIFO depth (excluding the turn currently running).
DEFAULT_MAX_QUEUE_PER_SESSION: Final = 8

#: Accepted turns between opportunistic ledger prunes.  Cheap enough to run
#: inline and frequent enough that a long-lived bot never accumulates rows.
_PRUNE_EVERY: Final = 500

_SAFE_NAME = re.compile(r"[^A-Za-z0-9._-]+")

#: A ``[Txxx]`` task tag — any number of digits, e.g. ``[T2]``/``[T002]``.
_TASK_TAG_RE: Final = re.compile(r"\[T\d+\]")


def extract_task_tag(text: str) -> str | None:
    """Return the first ``[T<digits>]`` task tag in *text*, or ``None``."""
    match = _TASK_TAG_RE.search(text or "")
    return match.group(0) if match else None


def ensure_task_tag_in_first_paragraph(text: str, tag: str | None) -> str:
    """Guarantee *tag* opens the first paragraph of *text* when it was requested.

    A reply correlates to its request by more than ``reply_to_message_id``:
    when the inbound message carried a ``[Txxx]`` tag, the reply must surface
    it too, even if the core forgot to echo it back.  Prepending it (rather
    than searching-and-replacing) is idempotent and never disturbs text the
    core already produced.
    """
    if not tag:
        return text
    first_paragraph = text.split("\n\n", 1)[0]
    if tag in first_paragraph:
        return text
    return f"{tag} {text}" if text else tag

#: Windows device names — harmless here but they break naive tooling and are a
#: classic sanitisation gap, so they are neutralised too.
_RESERVED_STEMS: Final = frozenset(
    {"con", "prn", "aux", "nul"}
    | {f"com{n}" for n in range(1, 10)}
    | {f"lpt{n}" for n in range(1, 10)}
)

#: Whole path components that must never appear in a deliverable artifact.
_SECRET_COMPONENTS: Final = frozenset(
    {
        ".ssh",
        ".gnupg",
        ".aws",
        ".security",
        ".secrets",
        "credentials",
        "secrets",
        "vault",
        "keys",
        "private",
    }
)

#: File name stems/substrings that mark a credential-bearing file.
_SECRET_MARKERS: Final = (
    "credential",
    "secret",
    "password",
    "passwd",
    "apikey",
    "api_key",
    "id_rsa",
    "id_dsa",
    "id_ecdsa",
    "id_ed25519",
    "netrc",
    "npmrc",
    "pgpass",
    "htpasswd",
)

_SECRET_EXACT_NAMES: Final = frozenset(
    {".env", ".git-credentials", "token", "tokens", "authorized_keys"}
)

_SECRET_SUFFIXES: Final = frozenset(
    {".key", ".pem", ".p12", ".pfx", ".jks", ".kdbx", ".keystore", ".asc", ".gpg"}
)


class BridgeOverflow(RuntimeError):
    """The per-chat FIFO queue is full."""


class BridgeCapacityExceeded(BridgeOverflow):
    """The global chat-session budget is exhausted."""


class TurnCancelled(RuntimeError):
    """A queued turn was dropped before it reached the runtime."""


class AttachmentRejected(ValueError):
    """An inbound or outbound file failed a security precondition."""


class DialogueGateway(Protocol):
    async def send_dialogue_turn(
        self,
        text: str,
        session_id: str,
        channel: str = "cli",
        user_id: str = "default",
        turn_id: str = "",
    ) -> dict[str, Any]: ...


@dataclass(frozen=True, slots=True)
class TurnIdentity:
    """Canonical identity of one Telegram-originated agent turn.

    ``message_id`` alone is not an identity: an edit reuses the id of the
    message it edits, and two different chats number their messages
    independently.  The key therefore carries chat, forum topic, update kind
    and edit revision as well.
    """

    chat_id: int
    message_id: int
    kind: str = "message"
    revision: int = 0
    thread_id: int | None = None

    @property
    def key(self) -> str:
        thread = self.thread_id if self.thread_id is not None else "-"
        return (
            f"telegram:{self.chat_id}:{thread}:{self.kind}"
            f":{self.message_id}:{self.revision}"
        )

    @property
    def session_id(self) -> str:
        """Runtime session id — forum topics are isolated conversations."""
        if self.thread_id is None:
            return f"telegram:{self.chat_id}"
        return f"telegram:{self.chat_id}:topic:{self.thread_id}"


@dataclass(frozen=True, slots=True)
class BridgeResult:
    payload: dict[str, Any]
    duplicate: bool = False
    replayed: bool = False


@dataclass(slots=True)
class _Request:
    identity: TurnIdentity
    text: str
    user_id: int
    future: asyncio.Future[dict[str, Any]]
    queue_id: str = ""
    message_id: int = 0
    task_tag: str | None = None
    reply_to_message_id: int | None = None
    attachment_ids: tuple[str, ...] = ()

    @property
    def key(self) -> str:
        return self.identity.key


@dataclass(slots=True)
class _Session:
    pending: deque[_Request] = field(default_factory=deque)
    active: bool = False
    worker: asyncio.Task[None] | None = None


class TelegramBridge:
    """Durable exactly-once, bounded, per-chat FIFO in front of the Gateway."""

    def __init__(
        self,
        gateway: DialogueGateway,
        *,
        ledger: TurnLedger | None = None,
        max_queue_per_session: int = DEFAULT_MAX_QUEUE_PER_SESSION,
        max_sessions: int = DEFAULT_MAX_SESSIONS,
    ) -> None:
        if max_queue_per_session < 1 or max_sessions < 1:
            raise ValueError("capacities must be positive")
        self._gateway = gateway
        self._ledger = ledger if ledger is not None else InMemoryTurnLedger()
        self._max_queue = max_queue_per_session
        self._max_sessions = max_sessions
        self._sessions: dict[int, _Session] = {}
        self._inflight: dict[str, asyncio.Future[dict[str, Any]]] = {}
        self._lock = asyncio.Lock()
        self._closed = False
        self._since_prune = 0

    @property
    def session_count(self) -> int:
        return len(self._sessions)

    @property
    def ledger(self) -> TurnLedger:
        return self._ledger

    def rebind_gateway(self, gateway: DialogueGateway) -> None:
        """Point the bridge at the transport's current Gateway client."""
        self._gateway = gateway

    async def recover_on_startup(self) -> int:
        """Reclaim orphaned turns and resume draining any durable pending queue items."""
        async with self._lock:
            if self._closed:
                return 0
            if hasattr(self._ledger, "reclaim_orphaned_turns"):
                reclaimed = await self._ledger.reclaim_orphaned_turns()
                if reclaimed:
                    logger.info("Reclaimed %d orphaned turns on startup", reclaimed)
            if hasattr(self._ledger, "queue_fetch_pending"):
                pending = await self._ledger.queue_fetch_pending()
                resumed = 0
                for item in pending:
                    chat_id = item.get("chat_id")
                    if chat_id is None:
                        continue
                    state = self._sessions.get(chat_id)
                    if state is None:
                        try:
                            state = self._reserve_session(chat_id)
                        except Exception:
                            continue
                    if state.worker is None or state.worker.done():
                        state.worker = asyncio.create_task(self._drain(chat_id))
                        resumed += 1
                return len(pending)
            return 0

    async def turn(
        self,
        *,
        identity: TurnIdentity,
        text: str,
        user_id: int,
        attachment_ids: Sequence[str] = (),
    ) -> BridgeResult:
        """Run exactly one agent turn for ``identity`` and return its payload.

        Raises :class:`BridgeOverflow`/:class:`BridgeCapacityExceeded` when the
        transport is saturated, :class:`AmbiguousTurn` when a previous process
        may already have executed this turn, and whatever the Gateway raised
        when submission failed (that case stays retryable).
        """
        key = identity.key
        duplicate = False
        task_tag = extract_task_tag(text)
        queue_id = secrets.token_hex(8)

        async with self._lock:
            if self._closed:
                raise RuntimeError("bridge is closed")

            existing = self._inflight.get(key)
            if existing is not None:
                future = existing
                duplicate = True
            else:
                claim = await self._ledger.claim(key, identity.chat_id)
                if claim.outcome is ClaimOutcome.REPLAY:
                    await self._record_queue_event(
                        queue_id=queue_id,
                        identity=identity,
                        text=text,
                        task_tag=task_tag,
                        attachment_ids=attachment_ids,
                        status=QueueStatus.DROPPED_DUPLICATE,
                    )
                    return BridgeResult(
                        claim.payload or {}, duplicate=True, replayed=True
                    )
                if claim.outcome in (
                    ClaimOutcome.AMBIGUOUS,
                    ClaimOutcome.IN_FLIGHT_LOCAL,
                ):
                    # Either a previous process, or this process before losing
                    # the in-memory future.  The runtime may already have run
                    # this turn, so a second invocation is not permitted.
                    raise AmbiguousTurn(key)

                try:
                    state = self._reserve_session(identity.chat_id)
                except BridgeOverflow:
                    # Nothing was submitted, so keep the turn retryable.
                    await self._ledger.fail(key)
                    raise

                future = asyncio.get_running_loop().create_future()
                self._inflight[key] = future
                state.pending.append(
                    _Request(
                        identity,
                        text,
                        user_id,
                        future,
                        queue_id=queue_id,
                        message_id=identity.message_id,
                        task_tag=task_tag,
                        reply_to_message_id=identity.message_id,
                        attachment_ids=tuple(attachment_ids),
                    )
                )
                if state.worker is None or state.worker.done():
                    state.worker = asyncio.create_task(self._drain(identity.chat_id))
                self._since_prune += 1

        if not duplicate:
            await self._record_queue_event(
                queue_id=queue_id,
                identity=identity,
                text=text,
                task_tag=task_tag,
                attachment_ids=attachment_ids,
                status=QueueStatus.QUEUED,
            )
        await self._maybe_prune()
        payload = await asyncio.shield(future)
        return BridgeResult(payload, duplicate=duplicate)

    async def _record_queue_event(
        self,
        *,
        queue_id: str,
        identity: TurnIdentity,
        text: str,
        task_tag: str | None,
        attachment_ids: Sequence[str],
        status: QueueStatus,
    ) -> None:
        """Best-effort observability write; never blocks turn execution."""
        try:
            await self._ledger.queue_enqueue(
                queue_id=queue_id,
                chat_id=identity.chat_id,
                message_id=identity.message_id,
                task_tag=task_tag,
                text=text,
                attachment_ids=attachment_ids,
                reply_to_message_id=identity.message_id,
                status=status,
            )
        except Exception:
            logger.exception("Queue ledger write failed for %s", queue_id)

    async def _record_queue_status(self, queue_id: str, status: QueueStatus) -> None:
        if not queue_id:
            return
        try:
            await self._ledger.queue_set_status(queue_id, status)
        except Exception:
            logger.exception("Queue status update failed for %s", queue_id)

    async def _maybe_prune(self) -> None:
        """Keep the durable ledger bounded without a background task."""
        if self._since_prune < _PRUNE_EVERY:
            return
        self._since_prune = 0
        try:
            removed = await self._ledger.prune()
        except Exception:
            logger.exception("Turn ledger prune failed")
            return
        if removed:
            logger.info("Pruned %d settled turn ledger rows", removed)

    def _reserve_session(self, chat_id: int) -> _Session:
        """Return this chat's session, enforcing both capacity bounds."""
        state = self._sessions.get(chat_id)
        if state is None:
            if len(self._sessions) >= self._max_sessions:
                raise BridgeCapacityExceeded(
                    "Слишком много активных чатов; повторите позже."
                )
            state = _Session()
            self._sessions[chat_id] = state
        queued = len(state.pending)
        if queued >= self._max_queue:
            raise BridgeOverflow("Слишком много сообщений в очереди; повторите позже.")
        return state

    async def _drain(self, chat_id: int) -> None:
        """Serialise one chat's turns; exit decisions are lock-atomic.

        The empty-queue check, the worker handle reset and the session teardown
        happen in a single critical section, so an enqueue can never slip in
        between them and be left without a worker.
        """
        while True:
            async with self._lock:
                state = self._sessions.get(chat_id)
                if state is None:
                    return
                if not state.pending:
                    state.active = False
                    state.worker = None
                    self._sessions.pop(chat_id, None)
                    return
                request = state.pending.popleft()
                state.active = True

            await self._record_queue_status(request.queue_id, QueueStatus.RUNNING)
            try:
                await self._execute(request)
            finally:
                async with self._lock:
                    live = self._sessions.get(chat_id)
                    if live is not None:
                        live.active = False

    async def _execute(self, request: _Request) -> None:
        """One Gateway submission, settled in the ledger before the future."""
        try:
            payload = await self._gateway.send_dialogue_turn(
                text=request.text,
                session_id=request.identity.session_id,
                channel="telegram",
                user_id=str(request.user_id),
                turn_id=request.key,
            )
        except BaseException as exc:
            # The runtime never accepted this turn: release it for retry.
            await self._settle(request, exception=exc)
            if isinstance(exc, asyncio.CancelledError):
                raise
        else:
            await self._settle(request, payload=payload)

    async def _settle(
        self,
        request: _Request,
        *,
        payload: dict[str, Any] | None = None,
        exception: BaseException | None = None,
    ) -> None:
        try:
            if payload is not None:
                await self._ledger.complete(request.key, payload)
            else:
                await self._ledger.fail(request.key)
        except Exception:
            logger.exception("Turn ledger write failed for %s", request.key)
        await self._record_queue_status(
            request.queue_id,
            QueueStatus.DONE if payload is not None else QueueStatus.FAILED,
        )
        async with self._lock:
            self._inflight.pop(request.key, None)
        if request.future.done():
            return
        if payload is not None:
            request.future.set_result(payload)
        else:
            request.future.set_exception(
                exception or RuntimeError("turn failed without a cause")
            )

    async def cancel_chat(self, chat_id: int) -> int:
        """Drop this chat's queued turns.  The active turn is the core's to cancel."""
        async with self._lock:
            state = self._sessions.get(chat_id)
            if state is None:
                return 0
            dropped = list(state.pending)
            state.pending.clear()
        for request in dropped:
            try:
                await self._ledger.fail(request.key)
            except Exception:
                logger.exception("Turn ledger write failed for %s", request.key)
            await self._record_queue_status(request.queue_id, QueueStatus.FAILED)
            async with self._lock:
                self._inflight.pop(request.key, None)
            if not request.future.done():
                request.future.set_exception(TurnCancelled(request.key))
        return len(dropped)

    async def close(self) -> None:
        """Stop accepting turns, let in-flight work settle, release resources."""
        async with self._lock:
            self._closed = True
            workers = [
                session.worker
                for session in self._sessions.values()
                if session.worker is not None
            ]
        if workers:
            # Give in-flight drain workers a bounded grace period, then cancel
            # any that are stuck (e.g. blocked on an external gateway that
            # never responds). A hard wait here would hang shutdown forever.
            _, pending = await asyncio.wait(
                set(workers), timeout=_CLOSE_GRACE_SECONDS
            )
            if pending:
                for w in pending:
                    w.cancel()
                await asyncio.gather(*pending, return_exceptions=True)
        async with self._lock:
            self._sessions.clear()
            self._inflight.clear()
        await self._ledger.close()


# ── Inbound attachments ───────────────────────────────────────────────────────


def sanitize_filename(filename: str | None) -> str:
    """Reduce an untrusted Telegram file name to one safe path component."""
    raw = (filename or "").replace("\\", "/")
    name = Path(raw).name
    name = _SAFE_NAME.sub("_", name).strip("._")
    if not name:
        return "attachment"
    if Path(name).stem.lower() in _RESERVED_STEMS:
        name = f"file_{name}"
    return name[:180]


@dataclass(frozen=True, slots=True)
class InboundSlot:
    """An exclusively created, symlink-free destination for a download."""

    path: Path
    temporary: Path
    handle: int  # open fd on ``temporary``


def prepare_inbound_path(
    root: Path,
    filename: str | None,
    *,
    size: int,
    max_size: int = DEFAULT_MAX_FILE_BYTES,
) -> Path:
    """Resolve an untrusted file name to a path confined to ``root``.

    Pure name/size validation: it enforces the declared-size limit, sanitises
    the name down to a single component, and proves the result still sits
    directly inside the canonical root (so ``../../.env`` cannot escape).  It
    performs no I/O on the file itself — :func:`prepare_inbound_file` owns the
    exclusive-creation step.
    """
    if size < 0 or size > max_size:
        raise AttachmentRejected("attachment size exceeds configured limit")

    canonical = root.resolve()
    canonical.mkdir(parents=True, exist_ok=True)
    try:
        os.chmod(canonical, 0o700)
    except OSError:  # pragma: no cover - filesystem without POSIX modes
        logger.debug("Could not tighten permissions on attachment root")

    target = canonical / sanitize_filename(filename)
    if target.parent != canonical:
        raise AttachmentRejected("invalid attachment path")
    return target


def prepare_inbound_file(
    root: Path,
    filename: str | None,
    *,
    size: int,
    max_size: int = DEFAULT_MAX_FILE_BYTES,
) -> InboundSlot:
    """Create a unique, exclusive, no-follow temp file under ``root``.

    ``O_CREAT | O_EXCL | O_NOFOLLOW`` closes the "attacker pre-creates a
    symlink at the download path" race that a post-download ``is_symlink()``
    check cannot: by the time such a check runs the write already followed the
    link.  The random prefix keeps two uploads of the same name from colliding.
    The caller downloads into ``handle`` and then promotes ``temporary`` to
    ``path``.
    """
    base = prepare_inbound_path(root, filename, size=size, max_size=max_size)
    target = base.with_name(f"{secrets.token_hex(8)}_{base.name}")
    temporary = target.with_name(target.name + ".part")
    try:
        # BUG ANT-007 (wave3, class G1): O_NOFOLLOW does not exist on Windows.
        # NTFS junctions are not followed by plain open(); the post-open
        # S_ISREG + fstat checks below still reject non-regular files.
        _nf = getattr(os, "O_NOFOLLOW", 0)
        handle = os.open(
            temporary,
            os.O_CREAT | os.O_EXCL | os.O_WRONLY | _nf,
            0o600,
        )
    except OSError as exc:
        raise AttachmentRejected("could not create a safe attachment slot") from exc
    return InboundSlot(path=target, temporary=temporary, handle=handle)


def finalize_inbound_file(slot: InboundSlot, *, max_size: int) -> int:
    """Validate the downloaded bytes and promote the temp file. Returns size."""
    info = os.fstat(slot.handle)
    if not stat.S_ISREG(info.st_mode):
        raise AttachmentRejected("attachment is not a regular file")
    if info.st_size > max_size:
        raise AttachmentRejected("attachment size exceeds configured limit")
    # BUG ANT-007 (wave3, class G1b): Windows cannot os.replace() a file whose
    # handle is still open (WinError 32). Close the slot handle before the
    # rename; discard_inbound_file's best-effort close then hits EBADF and is
    # swallowed (single-threaded bot loop — no fd-reuse window in practice).
    try:
        os.close(slot.handle)
    except OSError:
        pass
    os.replace(slot.temporary, slot.path)
    return int(info.st_size)


def discard_inbound_file(slot: InboundSlot) -> None:
    """Best-effort cleanup for success, failure and cancellation alike."""
    try:
        os.close(slot.handle)
    except OSError:
        pass
    try:
        slot.temporary.unlink(missing_ok=True)
    except OSError:
        logger.debug("Could not remove partial attachment")


# ── Outbound artifacts ────────────────────────────────────────────────────────


@dataclass(frozen=True, slots=True)
class ValidatedArtifact:
    """An artifact that passed every check *and* was read through one fd."""

    path: Path
    name: str
    size: int
    data: bytes


def _is_secret_like(canonical: Path) -> bool:
    parts = [part.lower() for part in canonical.parts]
    if set(parts) & _SECRET_COMPONENTS:
        return True
    name = canonical.name.lower()
    if name in _SECRET_EXACT_NAMES or name.startswith(".env"):
        return True
    if canonical.suffix.lower() in _SECRET_SUFFIXES:
        return True
    return any(marker in name for marker in _SECRET_MARKERS)


def resolve_outbound_artifact(
    path: Path,
    allowed_roots: tuple[Path, ...],
    *,
    max_size: int = DEFAULT_MAX_FILE_BYTES,
) -> ValidatedArtifact:
    """Validate and read one artifact, or refuse it before any I/O leaves.

    Every component of the requested path is checked for symlinks (not just the
    leaf), the canonical result must live under an allowed root, credential and
    configuration families are refused by name, and the file is finally opened
    with ``O_NOFOLLOW`` and validated through ``fstat`` on that very descriptor
    so the checked object and the sent object cannot diverge.
    """
    if not allowed_roots:
        raise AttachmentRejected("no artifact roots are configured")

    candidate = Path(path)
    if not candidate.is_absolute():
        raise AttachmentRejected("artifact path must be absolute")

    # All-component symlink check on the *requested* path.
    walked = Path(candidate.anchor)
    for part in candidate.parts[1:]:
        walked = walked / part
        if walked.is_symlink():
            raise AttachmentRejected("symbolic links are not deliverable")

    try:
        canonical = candidate.resolve(strict=True)
        roots = tuple(root.resolve(strict=True) for root in allowed_roots)
    except OSError as exc:
        raise AttachmentRejected("artifact path is unavailable") from exc

    if not any(
        canonical == root or canonical.is_relative_to(root) for root in roots
    ):
        raise AttachmentRejected("artifact is outside allowed roots")
    if _is_secret_like(canonical):
        raise AttachmentRejected("secret/config artifacts are not deliverable")

    # O_NONBLOCK matters even though the target should be a regular file: a
    # FIFO left in an artifact root would otherwise block ``open`` until some
    # writer appears, hanging the whole event loop.  For regular files the flag
    # is a no-op, and the S_ISREG check below rejects everything else.
    try:
        # BUG ANT-007 (wave3, class G1): O_NOFOLLOW/O_NONBLOCK missing on
        # Windows; O_BINARY required for raw byte reads (Ctrl-Z truncation).
        _nf = getattr(os, "O_NOFOLLOW", 0)
        _nb = getattr(os, "O_NONBLOCK", 0)
        _bn = getattr(os, "O_BINARY", 0)
        handle = os.open(canonical, os.O_RDONLY | _nf | _nb | _bn)
    except OSError as exc:
        raise AttachmentRejected("artifact could not be opened safely") from exc
    try:
        info = os.fstat(handle)
        if not stat.S_ISREG(info.st_mode):
            raise AttachmentRejected("artifact is not a regular file")
        if info.st_size > max_size:
            raise AttachmentRejected("artifact exceeds the delivery size limit")
        data = os.read(handle, max_size + 1)
    finally:
        os.close(handle)

    if len(data) > max_size:
        raise AttachmentRejected("artifact exceeds the delivery size limit")
    return ValidatedArtifact(
        path=canonical,
        name=sanitize_filename(canonical.name),
        size=len(data),
        data=data,
    )


# ── Long messages ─────────────────────────────────────────────────────────────


#: HTML entities produced by ``html.escape`` (plus numeric forms) are atomic —
#: splitting one in half yields text Telegram cannot parse.
_HTML_ENTITY = re.compile(
    r"&(?:#\d{1,7}|#[xX][0-9A-Fa-f]{1,6}|[A-Za-z][A-Za-z0-9]{1,31});"
)


def utf16_length(text: str) -> int:
    """Length in UTF-16 code units — the unit Telegram actually counts."""
    return len(text.encode("utf-16-le")) // 2


def split_telegram_html(
    text: str,
    *,
    limit: int = TELEGRAM_TEXT_LIMIT,
) -> list[str]:
    """Split HTML-escaped text without ever cutting an entity in half."""
    if limit < 1:
        raise ValueError("limit must be positive")
    if not text:
        return [""]

    tokens: list[str] = []
    index = 0
    while index < len(text):
        match = _HTML_ENTITY.match(text, index)
        if match is not None:
            tokens.append(match.group(0))
            index = match.end()
        else:
            tokens.append(text[index])
            index += 1

    chunks: list[str] = []
    current: list[str] = []
    units = 0
    for token in tokens:
        width = utf16_length(token)
        if current and units + width > limit:
            chunks.append("".join(current))
            current = []
            units = 0
        current.append(token)
        units += width
    chunks.append("".join(current))
    return chunks


def split_telegram_text(
    text: str,
    *,
    limit: int = TELEGRAM_TEXT_LIMIT,
) -> list[str]:
    """Split on UTF-16 code units — Telegram's actual unit — preserving order.

    Astral characters (emoji) count as two units, so a naive ``len()`` split
    silently overshoots the API limit.
    """
    if limit < 1:
        raise ValueError("limit must be positive")
    if not text:
        return [""]

    chunks: list[str] = []
    start = 0
    units = 0
    for index, char in enumerate(text):
        width = len(char.encode("utf-16-le")) // 2
        if units + width > limit:
            chunks.append(text[start:index])
            start = index
            units = 0
        units += width
    chunks.append(text[start:])
    return chunks
