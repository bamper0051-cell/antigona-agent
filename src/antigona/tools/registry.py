"""Tool Registry — register, discover, and dispatch tools.

Provides:
  - ``Tool`` dataclass with name, toolset, JSON schema, handler, guards
  - ``ToolRegistry`` class with register(), list(), dispatch(), and auto-discovery
  - Migration shim for old ``ActionType``-based tools
"""

from __future__ import annotations

import asyncio
import builtins
import importlib
import inspect
import json
import logging
import os
from collections.abc import Callable, Coroutine
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any

from antigona.core import paths
from antigona.ownership.epoch import FenceDeniedError, OwnershipContext
from antigona.ownership.wiring import enforce_write_fence
from antigona.tools.contracts import Tool as ToolABC
from antigona.tools.contracts import ToolStatus

logger = logging.getLogger(__name__)

# Process-live ACP agent registry (shared across _handle_acp calls).
_ACP_REGISTRY = None

# ─── Exceptions ────────────────────────────────────────────────────────────────


class ToolRegistrationError(Exception):
    """Raised when tool registration fails."""


class ToolNotFoundError(Exception):
    """Raised when a requested tool is not found in the registry."""


# ─── Simple Tool Model (new-style) ─────────────────────────────────────────────


@dataclass
class Tool:
    """A lightweight tool descriptor.

    Attributes:
        name: Unique tool name (e.g. ``"write_file"``).
        toolset: Logical group (e.g. ``"filesystem"``, ``"shell"``, ``"system"``).
        schema: JSON Schema dict describing the tool's input parameters.
        handler: Async callable(**kwargs) -> str (JSON result).
        check_fn: Optional sync callable(**kwargs) -> str | None.
            Return an error string to reject, or *None* to allow.
        requires_env: Optional list of env var names that must be set.
        requires_approval: NEXT-1B — when True the tool MUST NOT execute
            without an explicit approval token, even when the policy engine
            returns ``allowed=True``.
    """

    name: str
    toolset: str
    schema: dict[str, Any]
    handler: Callable[..., Coroutine[Any, Any, str]]
    check_fn: Callable[..., str | None] | None = None
    requires_env: list[str] = field(default_factory=list)
    requires_approval: bool = False


# ─── Enhanced Registry ─────────────────────────────────────────────────────────


class ToolRegistry:
    """Central registry of all available tools.

    Supports both the new *Tool* descriptor model and the old *ToolABC*
    contract model.  Tools are discovered automatically from
    ``tools/*.py`` modules that expose a top-level ``register()`` function.
    """

    def __init__(self) -> None:
        # New-style store: name -> Tool
        self._tools: dict[str, Tool] = {}
        # Backward-compat store: name -> ToolABC
        self._contracts: dict[str, ToolABC] = {}
        # Override from config (env-based or file-based)
        self._config_overrides: dict[str, dict[str, Any]] = {}
        # Optional shared security dependencies.  They stay injectable for
        # tests and are otherwise created lazily at the dispatch boundary.
        self.policy_engine: Any | None = None
        self.audit_logger: Any | None = None
        self.grant_store: Any | None = None

    # ── Registration ───────────────────────────────────────────────────────

    def register(
        self,
        name_or_tool: str | ToolABC,
        toolset: str = "",
        schema: dict[str, Any] | None = None,
        handler: Callable[..., Coroutine[Any, Any, str]] | None = None,
        *,
        check_fn: Callable[..., str | None] | None = None,
        requires_env: list[str] | None = None,
        requires_approval: bool = False,
        replace: bool = False,
    ) -> None:
        """Register a new tool or legacy ToolABC contract tool."""
        if isinstance(name_or_tool, ToolABC):
            contract = name_or_tool
            name = contract.spec.name
            if not replace and (name in self._tools or name in self._contracts):
                raise ToolRegistrationError(f"Tool '{name}' is already registered")
            self._contracts[name] = contract
            logger.debug("Registered contract tool '%s'", name)
            return

        name = name_or_tool
        if not replace and name in self._tools:
            raise ToolRegistrationError(f"Tool '{name}' is already registered")

        new_tool = Tool(
            name=name,
            toolset=toolset,
            schema=schema or {},
            handler=handler,  # type: ignore
            check_fn=check_fn,
            requires_env=requires_env or [],
            requires_approval=requires_approval,
        )
        self._tools[name] = new_tool
        logger.debug("Registered tool '%s' (toolset=%s)", name, toolset)

    def register_contract(self, tool: ToolABC, *, replace: bool = False) -> None:
        """Register a legacy *ToolABC* contract tool."""
        name = tool.spec.name
        if not replace and name in self._contracts:
            raise ToolRegistrationError(f"Contract tool '{name}' is already registered")
        self._contracts[name] = tool

    def unregister(self, name: str) -> None:
        """Remove a tool by name (both stores)."""
        if name not in self._tools and name not in self._contracts:
            raise ToolNotFoundError(f"Tool '{name}' is not registered")
        self._tools.pop(name, None)
        self._contracts.pop(name, None)

    # ── Lookup ─────────────────────────────────────────────────────────────

    def get(self, name: str) -> Any:
        """Get a tool by name (checking new-style first, then contract)."""
        tool: Any = self._tools.get(name)
        if tool is None:
            tool = self._contracts.get(name)
        if tool is None:
            raise ToolNotFoundError(
                f"Tool '{name}' is not registered. "
                f"Available: {', '.join(sorted(self._tools.keys() | self._contracts.keys()))}"
            )
        return tool

    def get_contract(self, name: str) -> ToolABC:
        """Get a legacy contract tool by name."""
        tool = self._contracts.get(name)
        if tool is None:
            raise ToolNotFoundError(
                f"Contract tool '{name}' is not registered. "
                f"Available: {', '.join(sorted(self._contracts.keys()))}"
            )
        return tool

    def get_spec(self, name: str) -> Any:
        """Get the specification/descriptor of a tool by name."""
        tool = self.get(name)
        if hasattr(tool, "spec"):
            return tool.spec
        return tool

    # ── Listing ────────────────────────────────────────────────────────────

    def list(self, toolset: str | None = None) -> list[Any]:
        """List registered tools (new-style + contracts), optionally filtered."""
        results: list[Any] = list(self._tools.values())
        results.extend(self._contracts.values())
        if toolset is None:
            return results
        filtered = []
        for t in results:
            if getattr(t, "toolset", None) == toolset:
                filtered.append(t)
            elif hasattr(t, "spec") and getattr(t.spec, "category", None) and t.spec.category.value == toolset:
                filtered.append(t)
        return filtered

    # ── Dispatch ───────────────────────────────────────────────────────────

    def _consume_approval_grant(
        self,
        *,
        token: str,
        actor: str,
        tool_name: str,
        args: dict[str, Any],
        policy_engine: Any,
    ) -> tuple[bool, str, str]:
        """Consume a durable exact one-shot grant, failing closed on errors."""
        from antigona.policy.engine import normalize_grant_args
        from antigona.security.approval_grant import ApprovalGrantStore

        store = self.grant_store or getattr(policy_engine, "grant_store", None)
        if store is None:
            store = ApprovalGrantStore()
            self.grant_store = store
        try:
            verdict = store.verify_and_consume(
                token,
                actor=actor,
                tool_name=tool_name,
                args=normalize_grant_args(args),
                consumed_by=f"registry:{actor}",
            )
        except Exception:  # noqa: BLE001 - authorization fails closed
            logger.exception("approval grant verification failed (fail-closed)")
            return False, "grant_store_error", ""
        if not verdict.valid:
            detail = verdict.reason.value if verdict.reason is not None else "invalid"
            return False, detail, verdict.grant_id
        return True, "", verdict.grant_id

    async def dispatch(self, name: str, **kwargs: Any) -> str:
        """Execute a tool by name and return its JSON result.

        This method:
          1. Looks up the tool.
          2. Checks environment requirements.
          3. Checks PolicyEngine (safety & owner approval).
          4. Enforces the NEXT-1B approval gate (``requires_approval`` tools
             need an explicit approval token, even when the policy allows).
          5. Calls *check_fn* (if set).
          6. Calls the handler.
          7. Records execution in SystemAuditLogger.
          8. Returns the JSON string.

        Args:
            name: Tool name.
            **kwargs: Arguments passed to the tool's handler.

        Returns:
            A JSON string.  On failure ``{"error": "..."}``.
        """
        correlation_id = str(kwargs.pop("_correlation_id", kwargs.pop("correlation_id", "")))
        channel = str(kwargs.pop("_channel", kwargs.pop("channel", "cli")))
        user_id = str(kwargs.pop("_user_id", kwargs.pop("user_id", "owner")))
        session_id = str(kwargs.pop("_session_id", kwargs.pop("session_id", "cli-session")))
        approval_token = str(kwargs.pop("_approval_token", kwargs.pop("approval_token", "")))
        turn_id = str(kwargs.pop("_turn_id", kwargs.pop("turn_id", "")))
        # Never let a caller-supplied handler kwarg impersonate trusted owner
        # context. tmux receives an owner id only after registry validation.
        # DF-WO2-003-full: a TRUSTED internal fencing token (never model-supplied;
        # it is stripped from clean params upstream).  Consumed here and forwarded
        # only to handlers that enforce the ownership write fence.
        _ownership = kwargs.pop("_ownership", None)
        # A-CORE-001/A-00: the "absolute out-of-workspace write is owner-approved"
        # proof is minted HERE, after a real grant is consumed — never accepted
        # from the caller.  A model/param-supplied value (in either spelling) is
        # discarded, otherwise the handler gate could be walked around by simply
        # passing the flag.
        kwargs.pop("owner_approval_grant", None)
        kwargs.pop("_owner_approval_grant", None)
        kwargs.pop("_owner_id", None)
        kwargs.pop("owner_id", None)
        kwargs.pop("auth_context", None)

        try:
            tool = self.get(name)
        except ToolNotFoundError as exc:
            return json.dumps({"error": str(exc)})

        # Environment guard
        requires_env = getattr(tool, "requires_env", [])
        missing = [v for v in requires_env if not os.environ.get(v)]
        if missing:
            return json.dumps(
                {"error": f"Missing required env var(s): {', '.join(missing)}"}
            )

        # Policy Engine Safety Check
        try:
            from antigona.policy.engine import PolicyEngine
            from antigona.security.audit import SystemAuditLogger

            policy_engine = self.policy_engine or PolicyEngine(
                grant_store=self.grant_store
            )
            audit_logger = self.audit_logger or SystemAuditLogger()

            if name == "tmux":
                from antigona.security.owner_identity import OwnerIdentity

                identity = OwnerIdentity()
                try:
                    actor_is_owner = identity.is_configured and identity.is_owner(
                        int(user_id)
                    )
                except (TypeError, ValueError):
                    actor_is_owner = False
                if not actor_is_owner:
                    audit_logger.log_action(
                        channel=channel,
                        user_id=user_id,
                        session_id=session_id,
                        command="TOOL:tmux",
                        exit_code=403,
                        status="DENIED_NOT_OWNER",
                        details={
                            "reason": "TMUX_OWNER_REQUIRED",
                            "correlation_id": correlation_id,
                            "turn_id": turn_id,
                        },
                    )
                    return json.dumps(
                        {"error": "tmux: access denied; authenticated owner required"}
                    )

            command_arg = str(kwargs.get("command") or kwargs.get("content") or kwargs.get("script") or "")
            path_arg = str(kwargs.get("path") or kwargs.get("filename") or "")

            policy_verdict = await policy_engine.check(
                action=name,
                params=dict(kwargs),
                context={"channel": channel, "user_id": user_id, "session_id": session_id, "turn_id": turn_id},
            )

            grant_consumed = False
            grant_id = ""
            policy_allowed = bool(policy_verdict.get("allowed", False))
            policy_requires_approval = bool(
                policy_verdict.get("requires_approval")
                or policy_verdict.get("requires_2step_confirmation", False)
            )

            # DF-WO2-003-full: handlers in _OWNERSHIP_AWARE_HANDLERS authorize themselves
            # through the per-action ownership write fence (enforced inside the handler and
            # fail-closed when the token is missing, foreign, released or expired).  They must
            # not be intercepted by the generic approval-grant gate, which would convert a
            # fenced write into a grant request and break the documented contract.  Every
            # other tool keeps the P1-002 fail-closed approval gate.
            # Fail-closed approval gate.  A grant is REQUIRED when the policy demands a
            # second step, or when an approval-flagged action is HIGH/CRITICAL risk.
            # MEDIUM actions that the policy explicitly ALLOWS run without a grant - the
            # blanket "any requires_approval needs a grant" rule (wave 4a) blocked 17
            # ordinary tools (send_email, web_fetch, rss, mcp, kanban, ...) and 58 tests,
            # turning the agent into an approval prompt for read-only work.  HIGH/CRITICAL
            # and 2-step actions stay strictly gated; fenced writers keep the ownership
            # fence inside their handler.
            _risk = str(policy_verdict.get("risk_level") or "").upper()
            # Shell-class tools are ALWAYS gated when the policy asks for approval, even if
            # the tool descriptor says otherwise (wave 3/4A UTEL contract).  Any other tool
            # is gated only for HIGH/CRITICAL risk or a required second step: forcing a grant
            # on every MEDIUM verdict blocked 17 ordinary tools (send_email, web_fetch, rss,
            # mcp, kanban, ...) and 24 tests, which is not what the policy says ("allowed").
            _shell_class = (
                str(getattr(tool, "toolset", "") or "").lower() == "shell"
                or name == "run_shell"
            )
            # AUDIT-C12/D — structural blindness of the six contract tools: they
            # are registered via ``register_contract`` and carry no ``toolset``
            # attribute, so ``getattr(tool, "toolset", "")`` is always empty and a
            # name-only shell check can never classify them.  Fall back to the
            # contract spec's category (a ``ToolCategory`` StrEnum) so a contract
            # tool routed through the shell executor is still visible to the gate.
            _tool_toolset = str(getattr(tool, "toolset", "") or "")
            if not _tool_toolset:
                _spec = getattr(tool, "spec", None)
                _spec_category = getattr(_spec, "category", None)
                if _spec_category is not None:
                    _tool_toolset = str(getattr(_spec_category, "value", _spec_category))
            _shell_class = _shell_class or _tool_toolset.lower() == "shell"
            # AUDIT-C12/B — code-execution surfaces.  A MEDIUM verdict that merely
            # says ``requires_approval`` is not enough for a tool that spawns a
            # host process or runs caller-supplied code: the shell class already
            # proves the product wants an owner grant for process execution, and
            # these tools execute processes just as much as ``run_shell`` does.
            # MCP/ACP are execution surfaces only for their *mutating* actions:
            # ``mcp add`` registers an arbitrary stdio command and ``mcp call``
            # invokes it; ``acp add``/``remove`` register an external executor.
            # Their read-only actions (``list``, ``tools``) stay ungated so
            # ordinary discovery work is not turned into an approval prompt
            # (the 17-tool wave-4a regression is forbidden).
            # AUDIT-C13-WHITESPACE-BYPASS (P1, 2026-09-18): the gate used to classify
            # the RAW action (``str(kwargs.get("action") or "").lower()``) while the
            # handlers normalize with ``.strip().lower()``.  A caller could therefore
            # send ``action="start "`` / ``" start"`` / ``"start\t"`` and the composite
            # terms below never matched, so the spawn ran with no grant
            # (driver-reproduced: ``subprocess.Popen(['ollama','serve'])``).  The
            # classification must use the SAME normalization the handler uses,
            # otherwise the gate and the executed action can disagree.
            _composite_action = str(kwargs.get("action") or "").strip().lower()
            _code_exec_class = (
                _shell_class
                or name in _CODE_EXECUTION_ACTIONS
                or (name == "mcp" and _composite_action in {"add", "call"})
                or (name == "acp" and _composite_action in {"add", "remove"})
            )
            # F-20260918T2000Z_OLLAMA_CODE_EXEC_UNGATED — the ``ollama`` tool spawns a
            # host process (``subprocess.Popen(["ollama", "serve"])`` on ``start``), runs
            # ``subprocess.run(["ollama", "pull", model])`` on ``pull`` and mutates the
            # active LLM provider on ``switch``.  It is registered with
            # ``toolset="llm"`` and is neither shell-class, code-exec-class nor a file
            # write, so the gate above never classified it: a MEDIUM verdict that merely
            # said ``requires_approval`` produced no grant and the spawn ran ungated.
            # Force the owner grant for exactly these executing/mutating actions,
            # independently of the policy's ``requires_approval`` flag (the defect was
            # precisely that the flag is not authoritative for a toolset="llm" surface).
            # The ``status``/``list`` actions are read-only discovery and stay UNGATED
            # (wave-4a forbids turning ordinary read-only work into an approval prompt).
            # ``policy_allowed`` keeps this from turning a non-approval denial (missing
            # identity, INTERNAL_ERROR) into a grantable action.
            _ollama_exec_action = (
                policy_allowed
                and name == "ollama"
                and _composite_action in {"start", "pull", "switch"}
            )
            # P1-002 parity with the KernelExecutor gate (2026-09-18).  A *file-write* tool
            # the policy already answers with ``requires_approval: true`` (MEDIUM/SENSITIVE,
            # e.g. an absolute target outside the workspace) is a mutation, not a read:
            # the narrow risk-only gate let that verdict through, so the model path could
            # write an arbitrary absolute path with no grant (A-00, live-reproduced on M29).
            # In-workspace writes stay auto-allowed (LOW/SAFE, requires_approval=false) and
            # read-only tools stay untouched, so no ordinary tool is converted into an
            # approval prompt.  The decision uses the policy verdict itself (the same
            # authority the KernelExecutor path trusts) - no second workspace computation
            # that could disagree with the configured workspace root.
            _write_action = False
            if name in _OWNERSHIP_AWARE_HANDLERS:
                _write_action = True
            else:
                try:
                    from antigona.security.risk_classifier import is_write_action

                    _write_action = bool(is_write_action(name))
                except Exception:  # noqa: BLE001 - fail-closed: an unclassified action
                    # must not silently widen access.  Treat it as a potential write so
                    # the approval gate below still applies (the mutation gate only
                    # fires when the policy itself already demands approval, so this
                    # cannot convert a read-only verdict into a prompt).
                    _write_action = True
            # P2 (FP-L04): the extra ``_risk not in {"", "LOW"}`` filter masked risk.
            # Only an explicit LOW verdict is a safe (auto-approved) write; MEDIUM,
            # HIGH, CRITICAL and a missing/unknown risk level all need the grant.
            _mutation_needs_owner = bool(
                policy_requires_approval and _write_action and _risk != "LOW"
            )
            _needs_grant = bool(
                policy_verdict.get("requires_2step_confirmation")
            ) or _ollama_exec_action or (
                policy_requires_approval
                and (_risk in {"HIGH", "CRITICAL"} or _code_exec_class or _mutation_needs_owner)
            )
            # The ownership-fence handlers authorize themselves, but only for writes the
            # policy does not already flag for owner approval: an approval-flagged mutation
            # must not inherit that exemption.
            _fence_exempt = name in _OWNERSHIP_AWARE_HANDLERS and not _mutation_needs_owner
            if _needs_grant and not _fence_exempt:
                grant_consumed, detail, grant_id = self._consume_approval_grant(
                    token=approval_token,
                    actor=user_id,
                    tool_name=name,
                    args=kwargs,
                    policy_engine=policy_engine,
                )
                if not grant_consumed:
                    reason = policy_verdict.get("reason", "Policy requires approval grant before execution")
                    formatted_msg = policy_verdict.get("formatted_message", "")
                    audit_logger.log_action(
                        channel=channel,
                        user_id=user_id,
                        session_id=session_id,
                        command=f"TOOL:{name} {command_arg or path_arg}".strip(),
                        exit_code=403,
                        status="DENIED",
                        details={
                            "reason": reason,
                            "grant": detail,
                            "grant_id": grant_id,
                            "correlation_id": correlation_id,
                            "turn_id": turn_id,
                        },
                    )
                    return json.dumps({
                        "error": reason,
                        "requires_approval": True,
                        "formatted_message": formatted_msg,
                    })
            elif not policy_allowed:
                reason = policy_verdict.get("reason", "Policy denied tool execution")
                formatted_msg = policy_verdict.get("formatted_message", "")
                audit_logger.log_action(
                    channel=channel,
                    user_id=user_id,
                    session_id=session_id,
                    command=f"TOOL:{name} {command_arg or path_arg}".strip(),
                    exit_code=403,
                    status="DENIED",
                    details={
                        "reason": reason,
                        "grant": "no_grant",
                        "grant_id": "",
                        "correlation_id": correlation_id,
                        "turn_id": turn_id,
                    },
                )
                return json.dumps({
                    "error": reason,
                    "requires_approval": False,
                    "formatted_message": formatted_msg,
                })
        except Exception as p_exc:
            logger.warning(
                "Policy check error for tool '%s': %s (denying, fail-closed)", name, p_exc
            )
            try:
                _cmd = str(
                    kwargs.get("command")
                    or kwargs.get("content")
                    or kwargs.get("script")
                    or ""
                )
                _path = str(kwargs.get("path") or kwargs.get("filename") or "")
                SystemAuditLogger().log_action(
                    channel=channel,
                    user_id=user_id,
                    session_id=session_id,
                    command=f"TOOL:{name} {_cmd or _path}".strip(),
                    exit_code=500,
                    status="DENIED",
                    details={
                        "reason": "POLICY_INTERNAL_ERROR",
                        "correlation_id": correlation_id,
                        "turn_id": turn_id,
                    },
                )
            except Exception:
                pass
            return json.dumps({
                "error": (
                    "POLICY_INTERNAL_ERROR: policy evaluation failed — "
                    "action denied (fail-closed)."
                ),
                "requires_approval": True,
                "formatted_message": (
                    "🚫 Ошибка policy-движка — действие отклонено (fail-closed)."
                ),
            })

        # Tool metadata is an independent gate. A non-empty caller string is
        # not approval: it must verify and atomically consume a durable grant.
        tool_spec = getattr(tool, "spec", None)
        tool_requires_approval = bool(getattr(tool, "requires_approval", False))
        if tool_spec is not None:
            tool_requires_approval = tool_requires_approval or bool(
                getattr(tool_spec, "requires_approval", False)
            )
        if tool_requires_approval and not grant_consumed:
            ok, detail, grant_id = self._consume_approval_grant(
                token=approval_token,
                actor=user_id,
                tool_name=name,
                args=kwargs,
                policy_engine=policy_engine,
            )
            if not ok:
                audit_logger.log_action(
                    channel=channel,
                    user_id=user_id,
                    session_id=session_id,
                    command=f"TOOL:{name}",
                    exit_code=403,
                    status="DENIED",
                    details={
                        "reason": "REQUIRES_VALID_APPROVAL_GRANT",
                        "grant": detail,
                        "grant_id": grant_id,
                        "correlation_id": correlation_id,
                        "turn_id": turn_id,
                    },
                )
                return json.dumps({
                    "error": "Tool requires a valid one-shot approval grant",
                    "requires_approval": True,
                })
            grant_consumed = True

        if name == "tmux":
            kwargs["_owner_id"] = user_id

        # Pre-execution check
        check_fn = getattr(tool, "check_fn", None)
        if check_fn is not None:
            err = check_fn(**kwargs)
            if err is not None:
                if 'audit_logger' in locals():
                    audit_logger.log_action(
                        channel=channel,
                        user_id=user_id,
                        session_id=session_id,
                        command=f"TOOL:{name}",
                        exit_code=1,
                        status="CHECK_FAILED",
                        details={"error": err, "correlation_id": correlation_id, "turn_id": turn_id},
                    )
                return json.dumps({"error": err})

        # Execute
        # DF-WO2-003-full: forward the trusted fencing token ONLY to handlers that
        # enforce the ownership write fence; other handlers must not receive an
        # unexpected kwarg.  Injected after check_fn so a strict check signature
        # is never handed a new keyword.
        if _ownership is not None and name in _OWNERSHIP_AWARE_HANDLERS:
            kwargs["ownership"] = _ownership
        # A-CORE-001/A-00: an absolute write outside the workspace is only
        # written when THIS dispatch consumed a real one-shot owner grant.  The
        # flag is minted here, after the gate above, and can never come from the
        # model: underscore-prefixed params are stripped upstream and this kwarg
        # is added only for the ownership-aware handler.
        if name in _OWNERSHIP_AWARE_HANDLERS and grant_consumed:
            kwargs["owner_approval_grant"] = True
        try:
            from antigona.tools.contracts import ToolInput
            if isinstance(tool, ToolABC):
                inp = ToolInput(tool_name=name, params=kwargs)
                val_errs = tool.validate(inp)
                if val_errs:
                    return json.dumps({"error": "; ".join(val_errs)})
                tool_out = await tool.execute(inp)
                if tool_out.success:
                    result: str = json.dumps({"success": True, "data": tool_out.data, "artifacts": tool_out.artifacts})
                else:
                    result = json.dumps({"error": tool_out.error or "Tool execution failed"})
            else:
                result = await tool.handler(**kwargs)
            if 'audit_logger' in locals():
                audit_logger.log_action(
                    channel=channel,
                    user_id=user_id,
                    session_id=session_id,
                    command=f"TOOL:{name}",
                    exit_code=0,
                    status="SUCCESS",
                    details={
                        "correlation_id": correlation_id,
                        "turn_id": turn_id,
                        "grant_id": grant_id,
                    },
                )
            return result
        except Exception as exc:
            logger.exception("Tool '%s' raised", name)
            if 'audit_logger' in locals():
                audit_logger.log_action(
                    channel=channel,
                    user_id=user_id,
                    session_id=session_id,
                    command=f"TOOL:{name}",
                    exit_code=500,
                    status="EXCEPTION",
                    details={"error": str(exc), "correlation_id": correlation_id, "turn_id": turn_id},
                )
            return json.dumps({"error": str(exc)})

    # ── Auto-discovery ─────────────────────────────────────────────────────

    def discover(self, package_dir: str | None = None) -> int:
        """Auto-discover tools from ``tools/*.py`` modules.

        Scans all ``.py`` files in the tools directory (or the given
        *package_dir*), imports each module, and calls any top-level
        ``register(registry)`` function found.

        Returns:
            Number of discovered tool modules.
        """
        if package_dir is None:
            base = os.path.dirname(__file__)
        else:
            base = package_dir

        count = 0
        for entry in sorted(os.listdir(base)):
            if not entry.endswith(".py") or entry.startswith("_"):
                continue
            mod_name = entry[:-3]
            try:
                mod = importlib.import_module(f"antigona.tools.{mod_name}")
            except Exception:
                logger.warning("Failed to import antigona.tools.%s", mod_name)
                continue

            if hasattr(mod, "register") and inspect.isfunction(mod.register):
                try:
                    mod.register(self)
                    count += 1
                    logger.debug("Discovered tools from antigona.tools.%s", mod_name)
                except Exception:
                    logger.exception("register() failed in antigona.tools.%s", mod_name)
        logger.info("Discovered %d tool modules", count)
        return count

    # ── Backward-compat helpers ────────────────────────────────────────────

    def list_tools(
        self,
        category: str | None = None,
        status: ToolStatus | None = None,
    ) -> builtins.list[Any]:
        """Legacy listing — delegates to old *list_tools* API for contracts."""
        result: list[Any] = []
        for tool in self._contracts.values():
            spec = tool.spec
            if category and spec.category.value != category:
                continue
            if status and spec.status != status:
                continue
            result.append(spec)
        return result

    # ── Properties ─────────────────────────────────────────────────────────

    @property
    def count(self) -> int:
        return len(self._tools) + len(self._contracts)

    @property
    def contract_count(self) -> int:
        return len(self._contracts)

    def __contains__(self, name: str) -> bool:
        return name in self._tools or name in self._contracts


# ═══════════════════════════════════════════════════════════════════════════════
# Built-in tool handlers  (migrated from ActionExecutor)
# ═══════════════════════════════════════════════════════════════════════════════


#: Registry handlers that enforce the ownership write fence (DF-WO2-003-full).
_OWNERSHIP_AWARE_HANDLERS = frozenset({"write_file"})


#: Code-execution surfaces (AUDIT-C12): handlers that spawn a host process or run
#: caller-supplied code.  A MEDIUM verdict with ``requires_approval`` is not enough
#: for these — the shell class already proves the product wants an owner grant for
#: process execution, and these execute processes just as much as ``run_shell``:
#:   * ``run_shell``      — asyncio.create_subprocess_shell
#:   * ``tmux``           — asyncio.create_subprocess_exec
#:   * ``frontend_build`` — subprocess.run npm/npx/vite (npm run <script>)
#:   * ``run_code``       — reserved by RiskClassifier._classify_run_code
_CODE_EXECUTION_ACTIONS: frozenset[str] = frozenset({
    "run_shell",
    "tmux",
    "frontend_build",
    "run_code",
})


async def _handle_write_file(
    *,
    path: str,
    content: str,
    ownership: OwnershipContext | None = None,
    owner_approval_grant: bool = False,
    **kwargs: Any,
) -> str:
    """WRITE_FILE handler.

    Returns the real resolved absolute path the bytes were written to (not
    the raw, possibly-relative input) so a later "where is the file" answer
    is grounded in the actual filesystem result, not the caller's guess.

    There is exactly ONE writable root: the canonical workspace
    (``paths.workspace_dir()`` / ``ANTIGONA_WORKSPACE``), shared with the policy
    layer.  A relative path is resolved against it (FP-L04-relative); an
    ABSOLUTE path outside it is written ONLY when the caller proves a real
    owner approval was consumed (``owner_approval_grant``, set by
    ``ToolRegistry.dispatch`` and never by a model — model params are stripped of
    underscore keys upstream).  Without that proof the write is refused with no
    side effect (A-CORE-001/A-00: "absolute paths are written as-is" let the
    model path place a file anywhere on the host whenever the policy verdict was
    ``allowed=True`` — which an elevated session produces even for an
    out-of-workspace target).

    FP-L04-relative: a *relative* path is resolved against the SAME canonical
    workspace root the policy layer uses (``resolve_confined_workspace_path`` →
    ``paths.workspace_dir()``).  Resolving against ``paths.project_root()`` made
    the policy and the handler disagree: the policy classified
    ``write_file path="evil.txt"`` as an in-workspace LOW write (relative to the
    workspace) while the handler wrote it into the code tree, i.e. a code-tree
    write with no approval.  The path must also stay inside the fence; an
    escaping relative path is refused with no side effect (fail-closed) instead
    of being silently widened to another root.

    DF-WO2-003-full: this is a live write surface, so it enforces the
    ownership fence BEFORE any filesystem mutation.  ``ownership`` is the
    caller's live fencing token (``OwnershipContext | None``).  When ownership
    is disabled it is a no-op (backward-compat); when enabled and the token is
    absent (or stale), the write is DENIED with no side effect (fail-closed,
    INV-06).
    """
    try:
        try:
            enforce_write_fence(ownership, "registry.write_file")
        except FenceDeniedError as exc:
            return json.dumps({"error": f"protected write denied: {exc.check.reason}"})
        from antigona.security.risk_classifier import resolve_confined_workspace_path

        raw_path = Path(path)
        # Single root shared with the policy classifier.  ``None`` means "no
        # canonical workspace" or "outside the fence" — both fail closed.
        confined = resolve_confined_workspace_path(path)
        if confined is not None:
            p = confined
        elif raw_path.is_absolute():
            if not owner_approval_grant:
                return json.dumps(
                    {
                        "error": (
                            "absolute write outside the workspace requires owner "
                            f"approval: {path}"
                        ),
                        "denied": True,
                        "requires_approval": True,
                        "path": path,
                    }
                )
            p = raw_path.resolve()
        else:
            # Not a grantable requirement: no grant authorizes a relative
            # target that leaves the fence, so the refusal says so instead of
            # asking for an approval that could never work.
            return json.dumps(
                {
                    "error": (
                        "path boundary violation: relative write target "
                        f"escapes the workspace fence: {path}"
                    ),
                    "denied": True,
                    "path": path,
                }
            )
        p.parent.mkdir(parents=True, exist_ok=True)
        p.write_text(content, encoding="utf-8")
        return json.dumps({"success": True, "path": str(p), "bytes": p.stat().st_size})
    except Exception as e:
        return json.dumps({"error": str(e)})


_READ_FILE_CAP = 100_000


async def _handle_read_file(*, path: str, **kwargs: Any) -> str:
    """READ_FILE handler — honest existence/permission/size checks, real content.

    Never fabricates content: a missing path returns a distinct NOT_FOUND
    error, a permission failure returns a distinct PERMISSION error, and
    success always echoes the real resolved path the bytes came from.

    A relative path is resolved strictly against the configured workspace root
    and must stay inside it — never ``project_root()`` or the process CWD.
    Those fallbacks let a bare name escape the workspace fence the policy layer
    enforces (deployed units run workers with a ``WorkingDirectory`` outside
    both the repo and the workspace).  This self-check is defence-in-depth: the
    policy layer already classifies an out-of-workspace read HIGH and gates it
    behind an approval grant; an absolute path is left as-is so a genuinely
    granted absolute read still works.
    """
    raw_path = Path(path)
    if raw_path.is_absolute():
        p = raw_path
    else:
        from antigona.security.risk_classifier import resolve_workspace_root

        ws_root = resolve_workspace_root()
        if ws_root is None:
            # Fail closed rather than widening the fence to project_root().
            return json.dumps(
                {"error": f"NOT_FOUND: no such file: {path}", "path": path}
            )
        try:
            p = (ws_root / raw_path).resolve()
            p.relative_to(ws_root)
        except (OSError, ValueError, RuntimeError):
            return json.dumps(
                {"error": f"NOT_FOUND: no such file: {path}", "path": path}
            )
    if not p.exists():
        return json.dumps({"error": f"NOT_FOUND: no such file: {path}", "path": str(p)})
    if not p.is_file():
        return json.dumps({"error": f"NOT_FOUND: not a regular file: {path}", "path": str(p)})
    try:
        raw = p.read_bytes()
    except PermissionError as e:
        return json.dumps({"error": f"PERMISSION: {e}", "path": str(p)})
    except Exception as e:
        return json.dumps({"error": str(e), "path": str(p)})

    truncated = len(raw) > _READ_FILE_CAP
    text = raw[:_READ_FILE_CAP].decode(errors="replace")
    return json.dumps({
        "success": True,
        "path": str(p),
        "bytes": len(raw),
        "content": text,
        "truncated": truncated,
    })


async def _handle_send_file(*, path: str, caption: str = "", chat_id: str = "", **kwargs: Any) -> str:
    """SEND_FILE handler — send a file to Telegram (via TelegramAdapter), not just validate.

    Authorization is enforced by the caller (owner ID in Telegram, PIN in CLI);
    secret files (.pem/.key/.env) require explicit owner confirmation.
    """
    p = Path(path)
    if not p.exists():
        alt = paths.project_root() / path
        if alt.exists():
            p = alt
        else:
            return json.dumps({"error": f"File not found: {path}"})
    if not p.is_file():
        return json.dumps({"error": f"Not a file: {path}"})

    # Secret protection: dangerous extensions require explicit confirmation (CRITICAL tier).
    _SECRET_EXT = (".pem", ".key", ".p12", ".pfx", ".crt", ".env", ".jks")
    if p.suffix.lower() in _SECRET_EXT:
        return json.dumps({
            "error": "Sending secret files (.pem/.key/.env) requires explicit owner confirmation.",
            "requires_confirmation": True,
            "path": str(p),
        })

    try:
        from antigona.delivery.adapter import TelegramAdapter
        _bot_token = (
            os.environ.get("ANTIGONA_DELIVERY_TELEGRAM_BOT_TOKEN")
            or os.environ.get("TELEGRAM_BOT_TOKEN")
        )
        _chat_id = chat_id or os.environ.get("ANTIGONA_DELIVERY_TELEGRAM_CHAT_ID")
        _mock = os.environ.get("ANTIGONA_DELIVERY_MOCK", "0") in ("1", "true", "True")
        adapter = TelegramAdapter(
            bot_token=_bot_token,
            chat_id=_chat_id,
            mock=_mock,
        )
        result = adapter.send_file(path=str(p), caption=caption or "")
        return json.dumps({"success": True, "path": str(p), "size": p.stat().st_size, **result})
    except Exception as e:
        return json.dumps({"error": str(e)})

async def _handle_send_voice(*, path: str, caption: str = "", **kwargs: Any) -> str:
    """Send an Ogg Opus file to Telegram as a voice message (sendVoice)."""
    import json as _json
    from pathlib import Path as _Path
    p = _Path(path)
    if not p.exists():
        alt = paths.project_root() / path
        if alt.exists():
            p = alt
        else:
            return _json.dumps({"error": f"File not found: {path}"})
    if not p.is_file():
        return _json.dumps({"error": f"Not a file: {path}"})
    try:
        from antigona.delivery.adapter import TelegramAdapter
        _bot_token = (
            os.environ.get("ANTIGONA_DELIVERY_TELEGRAM_BOT_TOKEN")
            or os.environ.get("TELEGRAM_BOT_TOKEN")
        )
        _chat_id = os.environ.get("ANTIGONA_DELIVERY_TELEGRAM_CHAT_ID")
        _mock = os.environ.get("ANTIGONA_DELIVERY_MOCK", "0") in ("1", "true", "True")
        adapter = TelegramAdapter(bot_token=_bot_token, chat_id=_chat_id, mock=_mock)
        result = adapter.send_voice(path=str(p), caption=caption or "")
        return _json.dumps({"success": True, "path": str(p), "size": p.stat().st_size, **result})
    except Exception as e:
        return _json.dumps({"error": str(e)})


# Blocked shell commands
_BLOCKED_SUBSTRINGS = {
    "shutdown", "reboot", "rm -rf /", "mkfs", "dd if=/dev/zero",
    "passwd", "iptables -F", "systemctl",
}


async def _handle_run_shell(*, command: str, **kwargs: Any) -> str:
    """RUN_SHELL handler."""
    # Case-insensitive leading-token normalization (dbefc870 intent): a user
    # typing "Pwd"/"Apt install uv" means pwd/apt — but /bin/sh is
    # case-sensitive, so the bare first token must be lowercased here (the
    # helper leaves path/assignment/expansion/quoted tokens untouched).
    try:
        from antigona.tools.shell_command import normalize_shell_command_first_token
        command = normalize_shell_command_first_token(command)
    except Exception:
        pass
    # Interactive aliases have no meaning in a non-interactive /bin/sh, but
    # users type them: expand the well-known ones on the leading token.
    first = command.lstrip().split(" ", 1)
    if first and first[0].lower() == "ll":
        command = "ls -alF" + (" " + first[1] if len(first) > 1 and first[1] else "")
    cmd_lower = command.lower()
    for b in _BLOCKED_SUBSTRINGS:
        if b in cmd_lower:
            return json.dumps({"error": f"Command blocked: {command}"})
    try:
        proc = await asyncio.create_subprocess_shell(
            command,
            stdout=asyncio.subprocess.PIPE,
            stderr=asyncio.subprocess.PIPE,
        )
        stdout, stderr = await asyncio.wait_for(proc.communicate(), timeout=30)
        out = stdout.decode(errors="replace").strip()
        err = stderr.decode(errors="replace").strip()
        if proc.returncode != 0:
            return json.dumps({"error": err or f"Exit code {proc.returncode}"})
        return json.dumps({"success": True, "output": out[:2000]})
    except TimeoutError:
        return json.dumps({"error": "Command timed out after 30s"})
    except Exception as e:
        return json.dumps({"error": str(e)})


async def _handle_generate_image(*, prompt: str, **kwargs: Any) -> str:
    """GENERATE_IMAGE handler."""
    if not prompt:
        return json.dumps({"error": "Empty prompt"})
    try:
        from antigona.tools.image_gen import ImageGenerator

        generator = ImageGenerator()
        filepath = await generator.generate_and_send(prompt=prompt)
        return json.dumps({"success": True, "path": filepath, "prompt": prompt[:80]})
    except Exception as e:
        return json.dumps({"error": str(e)})


async def _handle_configure_key(*, provider: str, key: str, **kwargs: Any) -> str:
    """CONFIGURE_KEY handler."""
    try:
        from antigona.tools.key_manager import configure_full_keyflow

        result = configure_full_keyflow(provider, key)
        if result.get("success"):
            return json.dumps({"success": True, "provider": provider})
        return json.dumps({"error": result.get("error", "Key configuration failed")})
    except Exception as e:
        return json.dumps({"error": str(e)})


async def _handle_memorize(*, store: str, content: str, title: str | None = None, **kwargs: Any) -> str:
    """MEMORIZE handler."""
    if not content:
        return json.dumps({"error": "Empty content"})
    try:
        from antigona.memory.file_memory import FileMemory

        memory = FileMemory()
        current = memory.get_content(store)
        estimated = len(current) + len(title or "") + len(content) + 10
        limit = 2200 if store == "memory" else 1375
        if estimated > limit:
            return json.dumps({"error": f"Storage '{store}' full (limit {limit} chars)"})
        memory.add_entry(store, title or content[:50].strip(), content)
        return json.dumps({"success": True, "store": store, "title": title or content[:50].strip()})
    except Exception as e:
        return json.dumps({"error": str(e)})


# ─── Default registration ──────────────────────────────────────────────────────



async def _handle_count_tokens(*, text: str, **kwargs: Any) -> str:
    try:
        from antigona.core.tokenizer import count_tokens
        n = count_tokens(text or "")
        return json.dumps({"success": True, "tokens": n, "text": (text or "")[:80]})
    except Exception as e:
        return json.dumps({"error": str(e)})


async def _handle_kanban(*, action: str = "list", title: str = "", card_id: str = "", column: str = "in-progress", board: str = "", body: str = "", **kwargs: Any) -> str:
    try:
        from antigona.core.kanban import KanbanBoard
        # R1-PORTABILITY-01: default board resolves through the canonical path
        # API (<workspace>/.kanban), never a hardcoded /var/lib/antigona.
        b = KanbanBoard(board or str(paths.kanban_dir()))
        action = (action or "list").lower()
        if action == "create":
            cid = b.create(title=title or "task", body=body)
            return json.dumps({"success": True, "id": cid, "column": "todo"})
        if action == "move":
            return json.dumps({"success": b.move(card_id, column or "in-progress"), "id": card_id})
        if action == "done":
            return json.dumps({"success": b.done(card_id), "id": card_id})
        if action == "get":
            c = b.get(card_id)
            return json.dumps({"success": c is not None, "card": c})
        cards = b.list()
        return json.dumps({"success": True, "count": len(cards), "cards": cards})
    except Exception as e:
        return json.dumps({"error": str(e)})


async def _handle_loop(*, max_iterations: int = 10, **kwargs: Any) -> str:
    try:
        from antigona.core.loop import MAX_ITERATIONS_DEFAULT, run_loop
        n = max_iterations or MAX_ITERATIONS_DEFAULT
        out = run_loop(steps=[lambda c: "ok"], judge=lambda r: ("PASS", ""), max_iterations=n)
        return json.dumps({"success": True, "status": out.status, "iterations": out.iterations, "verdict": out.verdict})
    except Exception as e:
        return json.dumps({"error": str(e)})


async def _handle_rss(*, url: str = "", limit: int = 5, **kwargs: Any) -> str:
    try:
        from antigona.core.rss import fetch_feed
        items = await asyncio.to_thread(fetch_feed, url or "", limit or 5, 15)
        return json.dumps({"success": True, "count": len(items), "items": [i.to_dict() for i in items]})
    except Exception as e:
        return json.dumps({"error": str(e)})


async def _handle_mcp(
    *,
    action: str = "list",
    server: str = "",
    kind: str = "stdio",
    url: str = "",
    command: str = "",
    args: list[str] | None = None,
    tool: str = "",
    arguments: dict[str, Any] | None = None,
    **kwargs: Any,
) -> str:
    """MCP handler — list/add/remove registered servers, and actually use one.

    Registration (``add``/``remove``) is persisted to
    ``~/.antigona/mcp_servers.json`` via ``MCPRegistry.load()``/``save()``
    (see ``antigona.core.mcp``), so a server registered in one call is still
    there on the next. ``tools``/``call`` are the bridge from config to a
    live connection: they build a real ``MCPClient`` from the registry entry
    (via ``connect_from_entry``) and either list what the server exposes or
    invoke one of its tools — this is how a registered server's tools
    actually become callable, not just listed as config.
    """
    try:
        from antigona.core.mcp import MCPClient, MCPRegistry, connect_from_entry

        reg = MCPRegistry.load()
        action = (action or "list").lower()

        if action == "add" and command:
            reg.add_stdio(server or command, command, args)
            reg.save()
            return json.dumps({"success": True, "server": server or command, "kind": "stdio"})
        if action == "add" and url:
            reg.add_http(server or url, url)
            reg.save()
            return json.dumps({"success": True, "server": server or url, "kind": "http"})
        if action == "remove":
            removed = reg.remove(server)
            if removed:
                reg.save()
            return json.dumps({"success": removed, "server": server})

        if action in ("tools", "call"):
            entry = reg.servers.get(server)
            if entry is None:
                return json.dumps({
                    "error": f"MCP server '{server}' is not registered",
                    "servers": reg.names(),
                })
            client: MCPClient = await connect_from_entry(entry)
            try:
                if action == "tools":
                    found = await client.list_tools()
                    return json.dumps({
                        "success": True,
                        "server": server,
                        "tools": [
                            {"name": t.name, "description": t.description, "input_schema": t.input_schema}
                            for t in found
                        ],
                    })
                if not tool:
                    return json.dumps({"error": "action=call requires 'tool' (the MCP tool name)"})
                result = await client.call_tool(tool, arguments or {})
                return json.dumps({"success": True, "server": server, "tool": tool, "result": result})
            finally:
                await client.aclose()

        return json.dumps({"success": True, "servers": reg.names()})
    except Exception as e:
        return json.dumps({"error": str(e)})


async def _handle_send_email(
    *,
    to: str = "",
    subject: str = "",
    body: str = "",
    attachment: str = "",
    **kwargs: Any,
) -> str:
    """Отправить письмо через Gmail SMTP (общий sender Antigony)."""
    import asyncio

    from antigona.core.email_sender import send_email as _send

    try:
        confirmation = await asyncio.to_thread(
            _send,
            to=to,
            subject=subject or "Antigona delivery",
            body=body,
            attachments=[attachment] if attachment else [],
        )
        return json.dumps({"success": True, "result": confirmation}, ensure_ascii=False)
    except Exception as e:
        return json.dumps({"error": str(e)}, ensure_ascii=False)


async def _handle_acp(*, action: str = "list", agent: str = "", base_url: str = "", **kwargs: Any) -> str:
    try:
        from antigona.core.acp import ACPRegistry
        # Process-live singleton: a per-call ACPRegistry() was throwaway, so
        # `add` mutated nothing and `list` always returned [].
        global _ACP_REGISTRY
        if _ACP_REGISTRY is None:
            _ACP_REGISTRY = ACPRegistry()
        reg = _ACP_REGISTRY
        action = (action or "list").lower()
        if action == "add" and base_url:
            reg.add(agent or base_url, base_url)
            return json.dumps({"success": True, "agent": agent or base_url})
        if action == "remove":
            return json.dumps({"success": reg.remove(agent), "agent": agent})
        return json.dumps({"success": True, "agents": reg.names()})
    except Exception as e:
        return json.dumps({"error": str(e)})


_BUILTIN_TOOLS: list[tuple[str, str, dict[str, Any], Callable[..., Any], dict[str, Any]]] = [
    (
        "write_file",
        "filesystem",
        {
            "type": "object",
            "properties": {
                "path": {"type": "string", "description": "File path to write"},
                "content": {"type": "string", "description": "Content to write"},
            },
            "required": ["path", "content"],
        },
        _handle_write_file,
        {},
    ),
    (
        "read_file",
        "filesystem",
        {
            "type": "object",
            "properties": {
                "path": {"type": "string", "description": "File path to read"},
            },
            "required": ["path"],
        },
        _handle_read_file,
        {},
    ),
    (
        "send_file",
        "filesystem",
        {
            "type": "object",
            "properties": {
                "path": {"type": "string", "description": "File path to send"},
            },
            "required": ["path"],
        },
        _handle_send_file,
        {},
    ),
    (
        "run_shell",
        "shell",
        {
            "type": "object",
            "properties": {
                "command": {"type": "string", "description": "Shell command to execute"},
            },
            "required": ["command"],
        },
        _handle_run_shell,
        {"requires_approval": True},
    ),
    (
        "generate_image",
        "media",
        {
            "type": "object",
            "properties": {
                "prompt": {"type": "string", "description": "Image generation prompt"},
            },
            "required": ["prompt"],
        },
        _handle_generate_image,
        {},
    ),
    (
        "configure_key",
        "system",
        {
            "type": "object",
            "properties": {
                "provider": {"type": "string", "description": "Provider name"},
                "key": {"type": "string", "description": "API key"},
            },
            "required": ["provider", "key"],
        },
        _handle_configure_key,
        {},
    ),
    (
        "memorize",
        "memory",
        {
            "type": "object",
            "properties": {
                "store": {"type": "string", "description": "Store name (memory or user)"},
                "content": {"type": "string", "description": "Content to remember"},
                "title": {"type": "string", "description": "Optional title"},
            },
            "required": ["store", "content"],
        },
        _handle_memorize,
        {},
    ),

    (
        "count_tokens",
        "system",
        {
            "type": "object",
            "properties": {"text": {"type": "string", "description": "Text to count tokens for"}},
            "required": ["text"],
        },
        _handle_count_tokens,
        {},
    ),
    (
        "kanban",
        "productivity",
        {
            "type": "object",
            "properties": {
                "action": {"type": "string", "description": "create|move|done|get|list"},
                "title": {"type": "string", "description": "Card title (for create)"},
                "card_id": {"type": "string", "description": "Card id"},
                "column": {"type": "string", "description": "Target column"},
            },
            "required": ["action"],
        },
        _handle_kanban,
        {},
    ),
    (
        "loop",
        "productivity",
        {
            "type": "object",
            "properties": {"max_iterations": {"type": "integer", "description": "Loop cap"}},
        },
        _handle_loop,
        {},
    ),
    (
        "rss",
        "web",
        {
            "type": "object",
            "properties": {
                "url": {"type": "string", "description": "RSS/Atom feed URL"},
                "limit": {"type": "integer", "description": "Max items"},
            },
            "required": ["url"],
        },
        _handle_rss,
        {},
    ),
    (
        "mcp",
        "system",
        {
            "type": "object",
            "properties": {
                "action": {
                    "type": "string",
                    "description": (
                        "list = показать зарегистрированные серверы; "
                        "tools = показать инструменты сервера (нужен server); "
                        "call = ВЫЗВАТЬ инструмент РЕАЛЬНО зарегистрированного сервера "
                        "(нужны server, tool, arguments); "
                        "add = зарегистрировать НОВЫЙ сервер (нужны command или url, редко); "
                        "remove = удалить сервер. Перед вызовом убедись, что сервер есть "
                        "в списке (action='list') — незарегистрированный сервер вызвать нельзя."
                    ),
                },
                "server": {"type": "string", "description": "Имя сервера из списка зарегистрированных (action='list'). Для call/tools обязателен."},
                "command": {"type": "string", "description": "stdio command (только для add)"},
                "url": {"type": "string", "description": "http url (только для add)"},
                "args": {"type": "array", "items": {"type": "string"}, "description": "stdio command args (только для add)"},
                "tool": {"type": "string", "description": "Имя MCP-инструмента для вызова (для call). Возможные имена смотри через action='tools' у конкретного сервера — не выдумывай их."},
                "arguments": {"type": "object", "description": "Аргументы MCP-инструмента (для call), например {'text': '...', 'voice': 'ru-RU-SvetlanaNeural'} для speak/speak_to_file"},
            },
            "required": ["action"],
        },
        _handle_mcp,
        {},
    ),
    (
        "send_email",
        "system",
        {
            "type": "object",
            "properties": {
                "to": {"type": "string", "description": "Email получателя (по умолчанию — env ANTIGONA_DELIVERY_EMAIL_TO)"},
                "subject": {"type": "string", "description": "Тема письма"},
                "body": {"type": "string", "description": "Текст письма"},
                "attachment": {"type": "string", "description": "Путь к файлу-вложению (абсолютный или относительно workspace), например mp3-артефакт задачи"},
            },
        },
        _handle_send_email,
        {},
    ),
    (
        "acp",
        "system",
        {
            "type": "object",
            "properties": {
                "action": {"type": "string", "description": "list|add|remove"},
                "agent": {"type": "string", "description": "Agent name"},
                "base_url": {"type": "string", "description": "ACP server base URL (for add)"},
            },
            "required": ["action"],
        },
        _handle_acp,
        {},
    ),
]


def register_builtins(registry: ToolRegistry) -> None:
    """Register all built-in tools on the given *registry*."""
    for name, toolset, schema, handler, kw in _BUILTIN_TOOLS:
        registry.register(
            name,
            toolset=toolset,
            schema=schema,
            handler=handler,
            replace=True,
            **kw,
        )
    # Tmux session tool: quiet detached background sessions, owner-gated
    # (hard deny against ANTIGONA_OWNER_ID). Registered here so every runtime
    # that calls register_builtins() (gateway brain, legacy server) exposes it.
    from antigona.tools import tmux_session

    tmux_session.register(registry)
    from antigona.tools import frontend_build

    frontend_build.register(registry)
    # External integrations (email read/triage + graceful-fallback cloud tools).
    from antigona.tools import integrations

    integrations.register(registry)
    from antigona.tools.archive_ops import ArchiveCreateTool, ArchiveExtractTool, ArchiveInspectTool
    from antigona.tools.document_ops import CreateDocumentTool
    from antigona.tools.system_time import SystemTimeTool
    from antigona.tools.tts_tool import TTSTool

    registry.register_contract(ArchiveInspectTool(), replace=True)
    registry.register_contract(ArchiveExtractTool(), replace=True)
    registry.register_contract(ArchiveCreateTool(), replace=True)
    registry.register_contract(CreateDocumentTool(), replace=True)
    registry.register_contract(SystemTimeTool(), replace=True)
    registry.register_contract(TTSTool(), replace=True)
