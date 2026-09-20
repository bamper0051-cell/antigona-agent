"""Unified Tool Execution Layer for Antigona.

Provides a single, mandatory execution entry point for all tool requests coming from:
  - DialogueEngine
  - AgentLoop / AutonomousLoop
  - Worker / TaskFlow
  - Legacy ActionExecutor
  - Owner / CLI

Enforces:
  1. PolicyEngine verification for all requests.
  2. Owner & PIN authentication context propagation.
  3. Single-use command-specific approval check for privileged operations (e.g. run_shell).
  4. Unified Audit Logging (audit_log.db + correlation_id).
"""

from __future__ import annotations

import asyncio
import hashlib
import json
import logging
import os
from collections.abc import Awaitable, Callable
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any, cast

from antigona.durable.tool_ledger import DurableToolLedger
from antigona.policy.engine import PolicyEngine, normalize_grant_args
from antigona.security.approval_grant import ApprovalGrantStore
from antigona.security.audit import SystemAuditLogger
from antigona.tools.owner_shell import OwnerShellDenied, run_owner_shell
from antigona.tools.shell_command import strip_shell_tool_prefix

logger = logging.getLogger(__name__)


def compute_call_hash(
    turn_id: str,
    tool_name: str,
    params: dict[str, Any],
    call_ordinal: int = 0,
    tool_call_id: str = "",
) -> str:
    """Compute deterministic SHA-256 call identity for a logical tool call.

    Includes turn_id, tool_name, slot (tool_call_id or call_ordinal), and canonical
    sorted JSON parameters to ensure intentional identical calls in the same turn do
    not collide, while retries of the same logical call yield the same hash.
    """
    clean_params = {k: v for k, v in params.items() if not str(k).startswith("_")}
    canonical_json = json.dumps(clean_params, sort_keys=True, ensure_ascii=False)
    slot = tool_call_id or str(call_ordinal)
    raw = f"{turn_id}:{tool_name}:{slot}:{canonical_json}"
    return hashlib.sha256(raw.encode("utf-8")).hexdigest()


_READ_ONLY_TOOLS = {
    "count_tokens", "rss",
}

#: Registry tools whose handler enforces the ownership write fence
#: (DF-WO2-003-full).  Unified mints a live fencing token for these on the
#: dialogue/model path, where no workspace object carries one, and forwards it
#: as the trusted ``_ownership`` dispatch parameter (never a model argument).
_OWNERSHIP_AWARE_TOOLS = frozenset({"write_file"})

_RESERVED_TOOL_ARGS = frozenset(
    {
        "actor",
        "approval_token",
        "auth_context",
        "call_ordinal",
        "channel",
        "correlation_id",
        "owner_approval_grant",
        "owner_id",
        "requester",
        "session_id",
        "token",
        "tool_call_id",
        "turn_id",
        "user_id",
    }
)


def _clean_tool_params(params: dict[str, Any] | None) -> dict[str, Any]:
    """Remove model-controlled control-plane fields from handler arguments."""
    return {
        key: value
        for key, value in (params or {}).items()
        if not str(key).startswith("_") and str(key) not in _RESERVED_TOOL_ARGS
    }


#: Safe aliases for tool names the model may emit with a non-canonical name.
#: An alias NEVER resolves to a more privileged tool: ``sandbox_shell`` maps to
#: the same sandboxed ``sandbox.shell`` (never the host ``run_shell`` path).
_TOOL_NAME_ALIASES: dict[str, str] = {
    "sandbox_shell": "sandbox.shell",
    "sandbox-shell": "sandbox.shell",
    "sandboxshell": "sandbox.shell",
    "speech_tts": "speech.tts",
    "text_to_speech": "speech.tts",
}


def canonical_tool_name(name: str) -> str:
    """Resolve a model-emitted tool name to its canonical contract name."""
    raw = (name or "").strip()
    if not raw:
        return raw
    if raw in _TOOL_NAME_ALIASES:
        return _TOOL_NAME_ALIASES[raw]
    return _TOOL_NAME_ALIASES.get(raw.casefold(), raw)


def is_read_only_tool(tool_name: str, params: dict[str, Any]) -> bool:
    """Determine if a tool call is strictly read-only and safe for auto-retry."""
    if tool_name in _READ_ONLY_TOOLS:
        return True
    if tool_name == "kanban" and params.get("action") in ("list", "get"):
        return True
    if tool_name == "mcp" and params.get("action") == "list":
        return True
    if tool_name == "acp" and params.get("action") == "list":
        return True
    if tool_name == "tmux" and params.get("action") in ("list", "read", "status"):
        return True
    return False


#: Sentinel turn id the layer itself invents when a caller supplies neither a
#: turn nor a correlation id.  It names NO turn, so it can never identify "the
#: same logical call".
_AUTO_TURN_ID = "corr-auto"

#: Last-resort identities callers substitute for a missing turn id.  They name
#: a kind of call (tool name / session), not a turn, so they are treated as
#: "no identity" too.
_AUTO_TURN_ID_PREFIXES = ("llm-tool-", "direct-shell-")


def has_real_turn_identity(request: ToolExecutionRequest) -> bool:
    """Whether idempotency may key on this request at all (A-CORE-002).

    Fail-closed: an idempotency key is only meaningful for a call that carries a
    REAL turn identity supplied by its caller.  When ``turn_id`` and
    ``correlation_id`` are both empty the layer falls back to ``"corr-auto"`` —
    one shared key for every caller — so two different requesters/tasks issuing
    the same tool with the same params would collapse into one logical call and
    the second one would be served (and authorized) by the first one's ledger
    entry.  Such calls are therefore never deduplicated: each is its own call
    and goes through the full authorization gate.
    """
    explicit = request.turn_id or request.correlation_id
    if not explicit:
        return False
    if explicit == _AUTO_TURN_ID:
        return False
    if explicit.startswith(_AUTO_TURN_ID_PREFIXES):
        return False
    return True


#: Keys a *successful* answer uses to name an artifact it produced.  New-style
#: handlers return them at the top level (``write_file`` →
#: ``{"success": true, "path": ...}``); a legacy contract tool returns them
#: under ``data`` and/or in ``artifacts`` (the registry serializes
#: ``{"success": true, "data": ..., "artifacts": ...}`` — ``archive.create``,
#: ``workspace.create_document``).
#:
#: R-20260918T2327Z_ARTIFACT_KEYS_NARROW: this tuple is a *path-naming* key set,
#: not the historical three.  A key whose NAME alone declares an artifact
#: (``filename``, ``filepath``, ``dest``, ``destination``, ``target_path``, …)
#: is a claim whenever its value is a non-empty string.  Keys that merely
#: *hint* at a path are handled by :data:`_PATH_LIKE_KEY_HINTS` below, so the
#: contract stays fail-closed for unrecognised path-named keys instead of
#: trusting them blindly.
_ARTIFACT_KEYS: tuple[str, ...] = (
    "path",
    "file",
    "output_path",
    "filename",
    "filepath",
    "file_path",
    "output_file",
    "dest",
    "destination",
    "target_path",
)

#: Substrings that mark a key as *possibly* naming a path.  A key that is not
#: in :data:`_ARTIFACT_KEYS` but hits a hint is treated as an artifact claim
#: ONLY when its string value looks like a path (contains a path separator) —
#: that boundedness keeps free-text answers (``{"success": true, "output": "4",
#: "text": "see /etc/hosts"}``) replayable while still refusing a stale success
#: whose ``output``/``destination``/``target`` is a concrete absent path.
_PATH_LIKE_KEY_HINTS: tuple[str, ...] = (
    "path",
    "file",
    "dir",
    "dest",
    "target",
    "output",
    "artifact",
    "location",
    "uri",
)


def _is_artifact_claim(key: str, value: Any) -> bool:
    """Whether ``key: value`` in a successful answer claims an artifact path.

    R-20260918T2327Z_ARTIFACT_KEYS_NARROW — fail-closed direction: a false
    positive costs one re-execution of an otherwise-cacheable call, while a
    false negative replays a stale "success" whose artifact is gone.  So the
    check errs toward *claim*: any non-empty string under a path-naming key is
    a claim, and an unrecognised key whose name hints at a path is a claim
    whenever the value itself looks like a path.  Booleans/None/ints/lists and
    blank strings are never claims.
    """
    if not isinstance(value, str):
        return False
    if not value.strip():
        return False
    if key in _ARTIFACT_KEYS:
        return True
    lowered = key.lower()
    if not any(hint in lowered for hint in _PATH_LIKE_KEY_HINTS):
        return False
    return "/" in value or "\\" in value or os.sep in value


def declared_artifact_paths(tool_name: str, result: str) -> tuple[str, ...]:
    """Every artifact path a *successful* result claims to have produced.

    F-20260918T1627Z_ARTIFACT_TOOLS_GAP: the claim is read from the ANSWER
    ITSELF, never from a hardcoded tool-name allowlist.  Any registered tool
    that declares an artifact on success is therefore covered by construction —
    the contract set is the set of answers that declare one, so it cannot
    silently drift when a new artifact tool is registered.  ``tool_name`` is
    kept for call-site clarity and future per-tool policy; it does not gate the
    contract.

    R-20260918T2327Z_ARTIFACT_KEYS_NARROW: the claim set is also fail-closed
    across KEY NAMES — a path-named key outside the historical three (or an
    unrecognised key whose name hints at a path and whose value looks like one)
    is treated as a claim, so the answer-side contract cannot be narrowed by
    simply renaming the key that carries the artifact.  Direction of error: a
    needless re-execution, never a replayed stale success.

    An empty tuple means the recorded answer is not a successful declaration of
    a concrete artifact — then there is nothing to verify and the answer is
    replayed as-is.
    """
    try:
        payload = json.loads(result)
    except (TypeError, ValueError):
        return ()
    if not isinstance(payload, dict) or payload.get("success") is not True:
        return ()
    found: list[str] = []
    seen: set[str] = set()

    def _collect(mapping: Any) -> None:
        if not isinstance(mapping, dict):
            return
        for key, value in mapping.items():
            if not isinstance(key, str):
                continue
            if not _is_artifact_claim(key, value) or value in seen:
                continue
            seen.add(value)
            found.append(value)

    _collect(payload)
    _collect(payload.get("data"))
    artifacts = payload.get("artifacts")
    if isinstance(artifacts, list):
        for entry in artifacts:
            _collect(entry)
    return tuple(found)


def declared_artifact_path(tool_name: str, result: str) -> str | None:
    """The first artifact path a successful result claims to have produced."""
    paths = declared_artifact_paths(tool_name, result)
    return paths[0] if paths else None


#: R-20260919T0230Z_REPLAY_FIDELITY_DECLARED_HASH — hash-named ANSWER keys whose
#: string value may carry a CONTENT digest for a co-located artifact claim.
#: Case-insensitive substring match, bounded to the key NAME only.
_DIGEST_KEY_HINTS: tuple[str, ...] = (
    "sha256",
    "sha-256",
    "sha_256",
    "hash",
    "digest",
    "checksum",
)

#: Hex-digest length -> hashlib algorithm.  ONLY these lengths are trusted: any
#: other value shape is ambiguous and the claim falls back to existence-only, as
#: documented on :func:`_replay_is_faithful`.
_DIGEST_HEX_LENGTHS: dict[int, str] = {64: "sha256", 40: "sha1"}

_HEX_DIGITS = frozenset("0123456789abcdef")


def _is_digest_key(key: str) -> bool:
    """Whether ``key``'s NAME marks it as a content-digest carrier."""
    lowered = key.lower()
    return any(hint in lowered for hint in _DIGEST_KEY_HINTS)


def _as_hex_digest(value: Any) -> str | None:
    """Normalise ``value`` to a lowercase hex digest, else ``None``.

    Accepts only a string of exactly 64 (sha256) or 40 (sha1) hexadecimal
    characters.  Anything else -> ``None`` (the caller must NOT guess).
    """
    if not isinstance(value, str):
        return None
    candidate = value.strip().lower()
    if len(candidate) not in _DIGEST_HEX_LENGTHS:
        return None
    if any(char not in _HEX_DIGITS for char in candidate):
        return None
    return candidate


def _claim_scopes(payload: dict[str, Any]) -> list[dict[str, Any]]:
    """The JSON objects :func:`declared_artifact_paths` reads claims from.

    Same three shapes: the top level, ``payload["data"]`` and each dict entry of
    ``payload["artifacts"]``.  Kept separate from ``declared_artifact_paths`` so
    that its public contract is untouched.
    """
    scopes: list[dict[str, Any]] = [payload]
    data = payload.get("data")
    if isinstance(data, dict):
        scopes.append(data)
    artifacts = payload.get("artifacts")
    if isinstance(artifacts, list):
        scopes.extend(entry for entry in artifacts if isinstance(entry, dict))
    return scopes


def declared_artifact_digests(tool_name: str, result: str) -> dict[str, str]:
    """Content digest declared for an artifact claim, keyed by the claim path.

    F-20260919T0230Z_REPLAY_FIDELITY_DECLARED_HASH: a cached success may name the
    file it produced AND its digest (``{"success": true, "path": ..., "sha256":
    ...}`` or ``{"artifacts": [{"path": ..., "sha256": ...}]}``).  This maps the
    claim string to the declared digest, but ONLY when the association is
    unambiguous: exactly one artifact claim and exactly one hash-named hex
    digest inside the SAME JSON object.  A different object, a different shape,
    multiple claims or multiple digests => no association => the claim is
    absent from the returned mapping and the caller falls back to the
    existence/is-file check (never guesses).  Direction of error is
    fail-closed: a missed digest costs one re-execution, a forged one replays a
    stale success.

    ``tool_name`` is kept for call-site parity with
    :func:`declared_artifact_paths`; it does not gate the mapping.
    """
    try:
        payload = json.loads(result)
    except (TypeError, ValueError):
        return {}
    if not isinstance(payload, dict) or payload.get("success") is not True:
        return {}
    digests: dict[str, str] = {}
    for scope in _claim_scopes(payload):
        claims: list[str] = []
        found: list[str] = []
        for key, value in scope.items():
            if not isinstance(key, str):
                continue
            if _is_artifact_claim(key, value):
                claims.append(value)
            elif _is_digest_key(key):
                digest = _as_hex_digest(value)
                if digest is not None:
                    found.append(digest)
        if len(claims) == 1 and len(found) == 1:
            digests.setdefault(claims[0], found[0])
    return digests


def _file_digest(path: Path, algorithm: str) -> str:
    """Hex digest of ``path``'s bytes (``algorithm`` = 'sha256' | 'sha1').

    Notes
    -----
    Uses the builtin ``open`` so an unreadable file surfaces as ``OSError`` at
    the caller; the caller is responsible for converting that into a
    non-faithful verdict.
    """
    digest = hashlib.new(algorithm)
    with open(path, "rb") as handle:
        for chunk in iter(lambda: handle.read(1 << 20), b""):
            digest.update(chunk)
    return digest.hexdigest()


@dataclass(frozen=True)
class OwnerAuthContext:
    """Security & authorization context for owner operations.

    ``approval_token`` is NOT a free-form string: it must be a raw token issued
    by :class:`~antigona.security.approval_grant.ApprovalGrantStore`, bound to
    actor + tool + exact arguments, and it is consumed exactly once by
    :meth:`UnifiedToolExecutionLayer._consume_approval_grant`.
    """
    is_owner: bool = False
    pin_verified: bool = False
    approval_token: str = ""
    approved_command_id: str = ""
    approved_command_text: str = ""
    expiry_timestamp: float = 0.0


@dataclass
class ToolExecutionRequest:
    """Structured representation of a tool call request."""
    tool_name: str
    params: dict[str, Any]
    requester: str  # "llm", "owner", "taskflow", "worker", "dialogue"
    channel: str = "cli"
    user_id: str = "owner"
    session_id: str = "cli-session"
    correlation_id: str = ""
    turn_id: str = ""
    owner_auth: OwnerAuthContext = field(default_factory=OwnerAuthContext)


class UnifiedToolExecutionLayer:
    """Single, canonical tool execution layer for Antigona."""

    def __init__(
        self,
        policy_engine: PolicyEngine | None = None,
        audit_logger: SystemAuditLogger | None = None,
        registry: Any | None = None,
        durable_ledger: DurableToolLedger | None = None,
        sandbox_shell_tool: Any | None = None,
        grant_store: ApprovalGrantStore | None = None,
        settings: Any | None = None,
    ) -> None:
        self.policy_engine = policy_engine or PolicyEngine(grant_store=grant_store)
        self.audit_logger = audit_logger or SystemAuditLogger()
        self.registry = registry
        self.durable_ledger = durable_ledger or DurableToolLedger()
        # A-3: the model-initiated privileged shell is authorized by a durable
        # one-shot grant, never by a self-invented token. Built lazily so the
        # store (and its SQLite file) is only touched when a shell is executed.
        self.grant_store = grant_store
        # E-1: dialogue/model ``sandbox.shell`` MUST run inside the Docker
        # sandbox (antigona.shell.DockerShellTool), never on the host shell.
        # Injectable for tests; built lazily from Settings otherwise.
        self.sandbox_shell_tool = sandbox_shell_tool
        # Injected runtime settings (tests / embedders).  ``None`` -> resolved
        # from the environment at the moment a token is minted.
        self.settings = settings
        self._ledger: dict[str, dict[str, Any]] = {}
        self._lock = asyncio.Lock()

    def _replay_is_faithful(self, tool_name: str, cached_result: str) -> bool:
        """Whether a COMPLETED ledger entry may be replayed as-is (A-CORE-002).

        A cached success that *declares* an artifact is only replayed while that
        artifact is still on disk: no effect on disk means the claim is stale,
        and a stale claim must never be returned as a success.  An answer that
        declares no artifact has nothing to contradict it and is replayed.
        F-20260918T1627Z: the declared artifacts are read from the answer, so
        EVERY tool that declares one is covered — not a single hardcoded name.

        F-20260919T0230Z_REPLAY_FIDELITY_DECLARED_HASH: "still on disk" is now
        verified beyond bare existence, still fail-closed (a needless
        re-execution is acceptable; a replayed stale/forged success is not):

        * existence — an absent declared path is NOT faithful (unchanged);
        * regular file — a claim that resolves to an existing NON-regular path
          (directory, fifo, device, broken symlink) is NOT faithful; a symlink
          pointing AT a regular file stays faithful (``Path.is_file()`` follows
          the link);
        * readability — a regular file that cannot be read (``open()`` raising
          ``OSError``, e.g. permission denied) is NOT faithful;
        * declared digest — when the SAME JSON object that carries the path
          claim also carries a hash-named key (name containing ``sha256``/
          ``sha-256``/``sha_256``/``hash``/``digest``/``checksum``,
          case-insensitive) whose value is a 64-char (sha256) or 40-char (sha1)
          hex string, the file's digest is recomputed and compared
          case-insensitively; a mismatch is NOT faithful.  The ``artifacts``
          list form is handled per entry when that entry dict carries both the
          path and the digest.

        Deliberate NON-guessing: when no digest is associated with a claim — no
        hash-named key, the digest lives in a different JSON object, multiple
        claims/digests share one object, or the value is not a recognised hex
        digest — the check falls back to existence + regular-file + readability
        only (today's behaviour), and never invents an association.
        """
        claimed = declared_artifact_paths(tool_name, cached_result)
        if not claimed:
            return True
        declared_digests = declared_artifact_digests(tool_name, cached_result)
        workspace = getattr(self.settings, "workspace", None)
        for claim in claimed:
            path = Path(claim)
            if not path.is_absolute():
                if workspace is None:
                    # No anchor to resolve a relative claim against -> the claim
                    # cannot be confirmed, so it is not replayed as-is.
                    return False
                path = Path(workspace) / path
            if not path.exists():
                return False
            if not path.is_file():
                # A directory / fifo / device / broken symlink is not the
                # artifact the cached answer declared.
                return False
            declared = declared_digests.get(claim)
            if declared is not None:
                algorithm = _DIGEST_HEX_LENGTHS[len(declared)]
                try:
                    actual = _file_digest(path, algorithm)
                except OSError:
                    return False
                if actual != declared:
                    return False
            else:
                # No unambiguous digest declared: existence/is-file only, but the
                # bytes must at least be readable.
                try:
                    with open(path, "rb") as handle:
                        handle.read(1)
                except OSError:
                    return False
        return True

    def _discard_stale_replay(self, call_hash: str, tool_name: str) -> None:
        """Drop a ledger entry whose declared effect is no longer on disk.

        Caller already holds ``self._lock``.  The next step re-executes the call
        through the normal (already authorized) path, so a fresh, verified
        effect replaces the stale claim.
        """
        logger.warning(
            "UnifiedExecution: dropping stale ledger entry tool=%s call_hash=%s (declared artifact is absent)",
            tool_name,
            call_hash[:8],
        )
        self._ledger.pop(call_hash, None)
        self.durable_ledger.clear(call_hash)

    async def _update_ledger(
        self,
        call_hash: str,
        status: str,
        result: str | None = None,
        error: str | None = None,
        read_only: bool = False,
    ) -> None:
        async with self._lock:
            entry = self._ledger.get(call_hash, {})
            event: asyncio.Event | None = entry.get("event")
            if status == "CLEARED" and read_only:
                self._ledger.pop(call_hash, None)
                self.durable_ledger.clear(call_hash)
            else:
                self._ledger[call_hash] = {
                    "status": status,
                    "result": result,
                    "error": error,
                    "event": event,
                }
                self.durable_ledger.settle(call_hash, status, result=result, error=error)
            if event is not None:
                event.set()

    async def execute(self, request: ToolExecutionRequest) -> str:
        """Execute a tool request through the unified safety pipeline."""
        # Accept canonical names plus safe aliases (e.g. ``sandbox_shell`` ->
        # ``sandbox.shell``) so a legitimate model intent is never truthfully
        # denied merely because of a name mismatch. Aliases never escalate.
        tool_name = canonical_tool_name(request.tool_name)
        raw_params = dict(request.params or {})
        params = _clean_tool_params(raw_params)
        shell_tool = tool_name in ("run_shell", "owner_shell", "sandbox.shell")
        privileged_shell = tool_name in ("run_shell", "owner_shell")
        if privileged_shell:
            auth = request.owner_auth
            if request.requester == "owner":
                if not auth.is_owner or not auth.pin_verified:
                    return json.dumps({"error": "Owner shell denied: PIN or Owner authorization missing"})
            elif not auth.approval_token or not auth.approved_command_text:
                return json.dumps({"error": "Model shell denied: exact owner approval is required"})

        correlation_id = request.correlation_id or "corr-auto"
        turn_id = request.turn_id or correlation_id
        if request.requester in {"llm", "dialogue"}:
            tool_call_id = ""
            call_ordinal = 0
        else:
            tool_call_id = str(
                raw_params.get("_tool_call_id", raw_params.get("tool_call_id", ""))
            )
            call_ordinal = int(raw_params.get("_call_ordinal", 0))
        hash_params = dict(params)
        if shell_tool:
            hash_params["requester"] = request.requester
            hash_params["approved_command_text"] = request.owner_auth.approved_command_text
        call_hash = compute_call_hash(
            turn_id, tool_name, hash_params, call_ordinal=call_ordinal, tool_call_id=tool_call_id
        )
        read_only = is_read_only_tool(tool_name, params)

        # A-CORE-002: idempotency requires a REAL turn identity, and the
        # privileged shell path is authorized per action by a one-shot grant
        # (inside ``_execute_shell``) — so neither is served from, or recorded
        # in, the ledger.  Every other call reaches the authorization gate
        # below before the ledger is even consulted.
        use_ledger = has_real_turn_identity(request) and not shell_tool

        logger.info(
            "UnifiedExecution: tool=%s requester=%s correlation_id=%s turn_id=%s call_hash=%s dedup=%s",
            tool_name, request.requester, correlation_id, turn_id, call_hash[:8], use_ledger,
        )

        # ── Authorization gate (A-CORE-002) ─────────────────────────────────
        # The policy/approval decision is NEVER cached and NEVER replaced by a
        # ledger replay: it is taken here, for EVERY request, BEFORE any cached
        # answer can be returned.  The ledger below may only shorten the
        # EXECUTION of an already-authorized call.
        if not shell_tool:
            policy_verdict = await self.policy_engine.check(
                action=tool_name,
                params=params,
                context={
                    "channel": request.channel,
                    "user_id": request.user_id,
                    "session_id": request.session_id,
                    "requester": request.requester,
                    "turn_id": turn_id,
                },
            )

            if not policy_verdict.get("allowed", False):
                risk = str(policy_verdict.get("risk_level") or "").upper()
                needs_grant = bool(
                    policy_verdict.get("requires_2step_confirmation")
                ) or (
                    bool(policy_verdict.get("requires_approval"))
                    and risk in {"HIGH", "CRITICAL"}
                )
                # The registry is the final side-effect boundary and consumes the
                # grant.  Unified may forward only the trusted token carried by
                # OwnerAuthContext; model params were stripped above.
                can_redispatch_with_grant = bool(
                    needs_grant and request.owner_auth.approval_token
                )
                if can_redispatch_with_grant:
                    logger.info(
                        "UnifiedExecution: deferring exact grant validation to registry tool=%s",
                        tool_name,
                    )
                else:
                    reason = policy_verdict.get("reason", "Policy denied execution")
                    self.audit_logger.log_action(
                        channel=request.channel,
                        user_id=request.user_id,
                        session_id=request.session_id,
                        command=f"TOOL:{tool_name}",
                        exit_code=403,
                        status="DENIED",
                        details={"reason": reason, "correlation_id": correlation_id, "turn_id": turn_id},
                    )
                    # A denial is an authorization decision, not an effect: it is
                    # never written to the ledger, so a cached denial can never
                    # outlive the approval that followed it.
                    return json.dumps({
                        "success": False,
                        "error": reason,
                        "requires_approval": bool(
                            policy_verdict.get("requires_approval")
                            or policy_verdict.get("requires_2step_confirmation", False)
                        ),
                    })

        # Durable SQLite + In-memory Idempotency check via call_hash.
        # Reached only AFTER the gate above authorized this very request.
        in_flight_event: asyncio.Event | None = None
        if use_ledger:
            async with self._lock:
                # 1. Check in-flight RAM ledger first for active coroutines
                existing = self._ledger.get(call_hash)
                if existing:
                    status = existing.get("status")
                    if status == "COMPLETED":
                        cached_result = str(existing.get("result", ""))
                        if self._replay_is_faithful(tool_name, cached_result):
                            logger.info("UnifiedExecution: returning cached COMPLETED result for call_hash=%s", call_hash[:8])
                            return cached_result
                        self._discard_stale_replay(call_hash, tool_name)
                    elif status == "PENDING":
                        event = existing.get("event")
                        if isinstance(event, asyncio.Event):
                            in_flight_event = event

                # 2. Check persistent SQLite ledger if not an in-flight RAM call
                if in_flight_event is None:
                    durable_entry = self.durable_ledger.get(call_hash)
                    if durable_entry and not read_only:
                        d_status = str(durable_entry.get("status"))
                        if d_status == "COMPLETED" and durable_entry.get("result") is not None:
                            cached_result = str(durable_entry["result"])
                            if self._replay_is_faithful(tool_name, cached_result):
                                logger.info("UnifiedExecution: returning persistent COMPLETED result for call_hash=%s", call_hash[:8])
                                return cached_result
                            self._discard_stale_replay(call_hash, tool_name)
                        elif d_status in ("PENDING", "EXECUTION_UNKNOWN"):
                            logger.warning("UnifiedExecution: fail closed EXECUTION_UNKNOWN for call_hash=%s", call_hash[:8])
                            return json.dumps({
                                "error": "EXECUTION_UNKNOWN: non-idempotent tool invocation in unknown or pending state",
                                "execution_unknown": True,
                                "call_hash": call_hash,
                            })

                if in_flight_event is None:
                    evt = asyncio.Event()
                    self._ledger[call_hash] = {"status": "PENDING", "event": evt}
                    self.durable_ledger.reserve(call_hash, turn_id, tool_name)

        if in_flight_event is not None:
            await in_flight_event.wait()
            async with self._lock:
                entry = self._ledger.get(call_hash, {})
                if entry.get("status") == "COMPLETED":
                    joined_result = str(entry.get("result", ""))
                    # The same rule applies to a result joined from a parallel
                    # identical call: a success whose declared artifact is not on
                    # disk is not returned as a success (A-CORE-002 rule 3).
                    if self._replay_is_faithful(tool_name, joined_result):
                        return joined_result
                return json.dumps({
                    "error": "EXECUTION_UNKNOWN: parallel execution ended with unknown status",
                    "execution_unknown": True,
                    "call_hash": call_hash,
                })

        # Special privileged handling for run_shell
        if shell_tool:
            # A-CORE-002: the privileged path authorizes inside
            # ``_execute_shell`` (PIN for the owner, exact approved command plus
            # a one-shot grant for the model) and NEVER replays a cached answer:
            # a replay would skip that per-action authorization.
            return await self._execute_shell(request)

        if not self.registry:
            from antigona.tools.registry import ToolRegistry, register_builtins
            self.registry = ToolRegistry()
            register_builtins(self.registry)

        if getattr(self.registry, "policy_engine", None) is None:
            self.registry.policy_engine = self.policy_engine
        if getattr(self.registry, "audit_logger", None) is None:
            self.registry.audit_logger = self.audit_logger
        if self.grant_store is not None:
            self.registry.grant_store = self.grant_store

        dispatch_params = dict(params)
        dispatch_params["_correlation_id"] = correlation_id
        dispatch_params["_turn_id"] = turn_id
        dispatch_params["_channel"] = request.channel
        dispatch_params["_user_id"] = request.user_id
        dispatch_params["_session_id"] = request.session_id
        dispatch_params["_approval_token"] = request.owner_auth.approval_token
        action_ownership: Any = None
        if tool_name in _OWNERSHIP_AWARE_TOOLS:
            # Trusted, non-model-controlled: stripped from model params by
            # ``_clean_tool_params`` (leading underscore) and consumed by
            # ``ToolRegistry.dispatch`` before the handler runs.
            action_ownership = self._mint_execution_ownership()
            dispatch_params["_ownership"] = action_ownership

        try:
            result = await self.registry.dispatch(tool_name, **dispatch_params)
            if use_ledger:
                await self._update_ledger(call_hash, "COMPLETED", result=result)
            return result
        except Exception as exc:
            if use_ledger:
                if read_only:
                    await self._update_ledger(call_hash, "CLEARED", read_only=True)
                else:
                    await self._update_ledger(call_hash, "EXECUTION_UNKNOWN", error=str(exc))
            raise
        finally:
            # One action == one minted token: never carry the lease to the next
            # action (and never reuse it across actions).
            self._release_execution_ownership(action_ownership)



    def _get_grant_store(self) -> ApprovalGrantStore:
        """Return the durable approval-grant store, creating it on first use."""
        if self.grant_store is None:
            self.grant_store = ApprovalGrantStore()
        return self.grant_store

    def _consume_approval_grant(
        self, request: ToolExecutionRequest, command: str
    ) -> tuple[bool, str]:
        """Verify AND consume the supplied approval token (one-shot).

        A-3: the token is checked against the canonical ApprovalGrantStore and
        must be bound to this actor, this tool and this exact command. A forged,
        expired, foreign or already-consumed token is refused. Any store error
        is refused too — fail-closed, never an allow.
        """
        token = str(request.owner_auth.approval_token or "")
        try:
            verdict = self._get_grant_store().verify_and_consume(
                token,
                actor=str(request.user_id),
                tool_name=request.tool_name,
                args=normalize_grant_args({"command": command}),
                consumed_by=f"unified:{request.requester}",
            )
        except Exception:  # noqa: BLE001 — fail-closed boundary
            logger.exception("approval grant verification failed (fail-closed)")
            return False, "grant_store_error"
        if not verdict.valid:
            return False, verdict.reason.value if verdict.reason is not None else "invalid"
        return True, verdict.grant_id

    async def _execute_shell(self, request: ToolExecutionRequest) -> str:
        """Handle privileged run_shell / owner_shell execution with strict authorization check."""
        command = str(request.params.get("command") or "").strip()
        if not command:
            return json.dumps({"error": "Empty shell command"})

        auth = request.owner_auth
        correlation_id = request.correlation_id or "corr-shell"

        # Case 1: Directly initiated by authenticated Owner
        if request.requester == "owner":
            if not auth.is_owner or not auth.pin_verified:
                self.audit_logger.log_action(
                    channel=request.channel,
                    user_id=request.user_id,
                    session_id=request.session_id,
                    command=f"OWNER_SHELL:{command}",
                    exit_code=403,
                    status="DENIED_NOT_OWNER",
                    details={"correlation_id": correlation_id},
                )
                return json.dumps({"error": "Owner shell denied: PIN or Owner authorization missing"})

            try:
                res = run_owner_shell(command, is_owner=True)
                self.audit_logger.log_action(
                    channel=request.channel,
                    user_id=request.user_id,
                    session_id=request.session_id,
                    command=f"OWNER_SHELL:{command}",
                    exit_code=res.exit_code,
                    status="SUCCESS" if res.exit_code == 0 else "FAILED",
                    details={"correlation_id": correlation_id},
                )
                return json.dumps({
                    "success": res.exit_code == 0,
                    "exit_code": res.exit_code,
                    "output": res.stdout or res.stderr,
                })
            except OwnerShellDenied as exc:
                return json.dumps({"error": str(exc)})

        # Privileged host-shell requests fail closed without concrete exact approval.
        if request.tool_name in ("run_shell", "owner_shell"):
            if not auth.approval_token or not auth.approved_command_text:
                return json.dumps({
                    "error": "Model shell denied: exact owner approval is required"
                })

        # Exact command match is checked BEFORE the grant is consumed, so a
        # mismatched attempt cannot burn a grant the owner issued for another
        # command (the grant stays valid for its own, approved command).
        if (
            request.tool_name in ("run_shell", "owner_shell")
            and auth.approved_command_text != command
        ):
            self.audit_logger.log_action(
                channel=request.channel,
                user_id=request.user_id,
                session_id=request.session_id,
                command=f"MODEL_SHELL:{command}",
                exit_code=403,
                status="DENIED_COMMAND_MISMATCH",
                details={
                    "approved": auth.approved_command_text,
                    "attempted": command,
                    "correlation_id": correlation_id,
                },
            )
            return json.dumps({"error": "Approval mismatch: approved command does not match attempted command"})

        policy_verdict = await self.policy_engine.check(
            action="run_shell",
            params={"command": command, "token": auth.approval_token},
            context={
                "channel": request.channel,
                "user_id": request.user_id,
                "session_id": request.session_id,
                "requester": request.requester,
            },
        )

        grant_consumed = False
        if not policy_verdict.get("allowed", False):
            # Canonical model (same as kernel/executor.py): a HIGH/CRITICAL
            # denial is not a dead end — it is an approval requirement, and a
            # valid one-shot grant for this exact command satisfies it.
            risk = str(policy_verdict.get("risk_level") or "").upper()
            needs_grant = bool(policy_verdict.get("requires_2step_confirmation")) or (
                bool(policy_verdict.get("requires_approval")) and risk in {"HIGH", "CRITICAL"}
            )
            granted = False
            detail = "no_grant"
            if needs_grant and request.tool_name in ("run_shell", "owner_shell"):
                granted, detail = self._consume_approval_grant(request, command)
                grant_consumed = granted
            if not granted:
                reason = policy_verdict.get("reason", "Shell execution denied by policy")
                self.audit_logger.log_action(
                    channel=request.channel,
                    user_id=request.user_id,
                    session_id=request.session_id,
                    command=f"MODEL_SHELL:{command}",
                    exit_code=403,
                    status="DENIED",
                    details={
                        "reason": reason,
                        "grant": detail,
                        "correlation_id": correlation_id,
                    },
                )
                return json.dumps({"error": reason})

        # A-3: an allowed-by-policy privileged shell still needs its approval
        # token to be a REAL grant — "non-empty string" is not an approval.
        if request.tool_name in ("run_shell", "owner_shell") and not grant_consumed:
            ok, detail = self._consume_approval_grant(request, command)
            if not ok:
                self.audit_logger.log_action(
                    channel=request.channel,
                    user_id=request.user_id,
                    session_id=request.session_id,
                    command=f"MODEL_SHELL:{command}",
                    exit_code=403,
                    status="DENIED_INVALID_GRANT",
                    details={"grant": detail, "correlation_id": correlation_id},
                )
                return json.dumps({
                    "error": (
                        "Model shell denied: approval token is not a valid one-shot "
                        f"grant ({detail})"
                    )
                })

        # E-1: ``sandbox.shell`` is the dialogue/model-initiated tool. Its name
        # is a contract: it MUST execute inside the Docker sandbox, not on the
        # host. It never reaches registry.dispatch("run_shell") (which is
        # asyncio.create_subprocess_shell on the host). The PIN-gated
        # owner/run_shell/owner_shell host path above is untouched.
        if request.tool_name == "sandbox.shell":
            return await self._execute_sandbox_shell(command, request, correlation_id)

        # Dispatch shell execution
        if not self.registry:
            from antigona.tools.registry import ToolRegistry, register_builtins
            self.registry = ToolRegistry()
            register_builtins(self.registry)

        tool = self.registry.get("run_shell")
        handler = getattr(tool, "handler", None)
        if callable(handler):
            typed_handler = cast(Callable[..., Awaitable[str]], handler)
            result = await typed_handler(command=command)
            return result if isinstance(result, str) else str(result)

        return await self.registry.dispatch(
            "run_shell",
            command=command,
            _correlation_id=correlation_id,
            _channel=request.channel,
            _user_id=request.user_id,
            _session_id=request.session_id,
        )

    def _mint_execution_ownership(self) -> Any:
        """Mint a live per-action ownership fencing token for a write surface.

        DF-WO2-003-full: the dialogue/model path (``sandbox.shell``,
        ``write_file``) executes without a workspace object, so it must mint its
        own token from the canonical authority.  Ownership DISABLED -> ``None``
        (backward-compatible).  Ownership ENABLED -> a live context, or a
        DENY-ALL context when the repo is held by another live owner / the
        authority is unavailable, so the fence still fails closed.
        """
        from antigona.ownership.wiring import mint_execution_ownership

        settings = self.settings
        if settings is None:
            from antigona.config import Settings

            settings = Settings.from_env()
        workspace = getattr(settings, "workspace", None)
        if workspace is None:
            # No workspace anchor -> no repo identity -> no token.  The surface
            # then fails closed on its own fence check (never silently unfenced).
            return None
        return mint_execution_ownership(workspace, settings=settings)

    def _bind_surface_ownership(self, tool: Any, ownership: Any) -> None:
        """Bind (or clear) an ownership token on a live execution surface."""
        bind = getattr(tool, "bind_ownership", None)
        if callable(bind):
            bind(ownership)
        else:  # pragma: no cover - every shipped surface exposes bind_ownership
            tool.ownership = ownership

    @staticmethod
    def _release_execution_ownership(ownership: Any) -> None:
        """Release a per-action ownership lease after the single action ran.

        DF-WO2-003-full: the token is a SHORT-LIVED lease.  Releasing it (and
        closing its ledger) right after the action keeps the next action's mint
        from colliding with this one and preserves the one-action-one-token
        rule.  Idempotent and best-effort: a release failure never converts a
        denied action into an allowed one.
        """
        if ownership is None:
            return
        try:
            from antigona.ownership.wiring import release_workspace_ownership

            release_workspace_ownership(ownership)
        except Exception:  # noqa: BLE001 - never fail an action on lease cleanup
            logger.warning("ownership release after action failed", exc_info=True)

    def _get_sandbox_shell_tool(self) -> Any:
        """Lazily build the hardened Docker sandbox shell tool (E-1)."""
        if self.sandbox_shell_tool is None:
            from antigona.config import Settings
            from antigona.sandbox.runner import (
                ISOLATION_REFUSED,
                SandboxIsolationError,
                resolve_runtime,
            )
            from antigona.shell import DockerShellTool

            settings = Settings.from_env()
            # Fail-closed: if gVisor is unavailable, pin the refusal sentinel so
            # every command is refused rather than silently downgraded to runc.
            try:
                runtime = resolve_runtime(settings.sandbox_runtime)
            except SandboxIsolationError as exc:
                logger.error("sandbox isolation REFUSED: %s", exc)
                runtime = ISOLATION_REFUSED
            self.sandbox_shell_tool = DockerShellTool(
                settings.workspace,
                settings.docker_image,
                timeout_seconds=max(int(settings.tool_timeout_seconds), 1),
                # "auto"/"runsc" must be resolved to a runtime docker can
                # actually launch (same contract the worker uses).
                runtime=runtime,
            )
        return self.sandbox_shell_tool

    async def _probe_missing_command(self, tool: Any, command: str) -> str:
        """Return the leading token when it does not exist inside the sandbox."""
        import shlex

        from antigona.shell import ShellInput

        try:
            tokens = shlex.split(command)
        except ValueError:
            return ""
        if not tokens:
            return ""
        first = tokens[0]
        if not first or "/" in first or "=" in first:
            return ""
        try:
            probe = await asyncio.to_thread(
                tool.execute,
                ShellInput(command=("/bin/sh", "-c", f"command -v {shlex.quote(first)}")),
            )
        except Exception:  # pragma: no cover - probe is best-effort
            return ""
        return "" if probe.ok else first

    async def _execute_sandbox_shell(
        self,
        command: str,
        request: ToolExecutionRequest,
        correlation_id: str,
    ) -> str:
        """Execute a dialogue/model shell command inside the Docker sandbox.

        Confinement guarantee (E-1): this path never touches the host shell —
        the command runs in a throwaway container over the workspace mount.
        """
        from antigona.shell import ShellInput

        command = strip_shell_tool_prefix(command)
        try:
            tool = self._get_sandbox_shell_tool()
        except Exception as exc:  # pragma: no cover - config/runtime failure
            logger.warning("sandbox.shell unavailable: %s", exc)
            return json.dumps({"success": False, "error": f"sandbox unavailable: {exc}"})

        # DF-WO2-003-full: mint a LIVE fencing token for THIS action and bind it
        # onto the sandbox surface.  Ownership disabled -> None (no-op, exactly
        # as before); enabled -> a live token, or a DENY-ALL token when the repo
        # is held elsewhere, which makes the fence below deny fail-closed.  The
        # token is dropped in the ``finally`` so it can never be reused across
        # actions (one action == one minted token).
        ownership = self._mint_execution_ownership()
        self._bind_surface_ownership(tool, ownership)
        try:
            result = await asyncio.to_thread(
                tool.execute,
                ShellInput(command=(command,), execution_id=correlation_id),
            )
        finally:
            self._bind_surface_ownership(tool, None)
            self._release_execution_ownership(ownership)
        output = str(result.data.get("output", "")) if result.data else ""
        self.audit_logger.log_action(
            channel=request.channel,
            user_id=request.user_id,
            session_id=request.session_id,
            command=f"SANDBOX_SHELL:{command}",
            exit_code=0 if result.ok else 1,
            status="SUCCESS" if result.ok else "FAILED",
            details={"correlation_id": correlation_id, "sandboxed": True},
        )
        if not result.ok:
            error = result.error or "sandbox shell failed"
            if error.startswith("tool exited non-zero"):
                # The sandbox deliberately never exposes container stderr, so a
                # generic non-zero result says nothing usable. Resolve the one
                # honest, non-leaking distinction (unknown command) with an
                # in-sandbox probe instead of guessing.
                missing = await self._probe_missing_command(tool, command)
                if missing:
                    error = f"command not found: {missing}"
            # Keep the failure shape callers already parse (``error`` key).
            return json.dumps({
                "success": False,
                "exit_code": next((int(item.value) for item in result.evidence if item.kind == "returncode" and item.value.isdigit()), 1),
                "error": error,
                "diagnostic": next((item.value for item in result.evidence if item.kind == "diagnostic"), "unknown"),
                "sandboxed": True,
            })
        return json.dumps({
            "success": True,
            "exit_code": 0,
            "output": output,
            "sandboxed": True,
        })
