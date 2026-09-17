"""Agent Loop — iterative LLM → action → execute → observe cycle.

The loop performs autonomous task execution:
    tick() → one iteration: LLM generates action → execute → observe result
    run(goal, context) → loop until max_iterations or final answer

Integration with TaskRuntime: each tick can create and execute task steps
via the runtime.

Architecture:
    AgentLoop
      ├── tick() — single iteration
      ├── run(goal, context) — loop
      ├── _call_llm() — generate action
      ├── _execute_actions() — run tools
      └── _observe() — process results
"""

from __future__ import annotations

import logging
import time
from dataclasses import dataclass, field
from typing import Any

logger = logging.getLogger(__name__)

# ─── Constants ───────────────────────────────────────────────────────────────

DEFAULT_MAX_ITERATIONS: int = 10
DEFAULT_ITERATION_TIMEOUT: float = 60.0
"""Max seconds per tick() call."""

_AGENT_SYSTEM_PROMPT: str = (
    "Ты — Antigona Agent Loop. Твоя задача — выполнить цель пользователя "
    "автономно, используя доступные инструменты.\n\n"
    "На каждом шаге:\n"
    "1. Проанализируй текущее состояние\n"
    "2. Реши, какое действие выполнить\n"
    "3. Выполни действие одной из команд:\n"
    "   WRITE_FILE|путь|содержимое\n"
    "   RUN_SHELL|команда\n"
    "   SEND_FILE|путь\n"
    "   WEB_SEARCH|запрос\n"
    "   SEARCH_AND_FORMAT|запрос\n"
    "4. Если цель достигнута, ответь ФИНАЛ: твой ответ\n\n"
    "Если нужно найти информацию в интернете, используй SEARCH_AND_FORMAT.\n"
    "После получения результатов продолжай анализ.\n"
    "Не повторяй одни и те же действия — если что-то не работает, попробуй другой подход."
)


# ─── Data classes ────────────────────────────────────────────────────────────


@dataclass
class ActionObservation:
    """An action taken by the agent and its observed result.

    Attributes:
        iteration: The tick number.
        action: The action text the LLM generated.
        result: The result of executing the action.
        success: Whether execution succeeded.
    """

    iteration: int
    action: str
    result: str = ""
    success: bool = True


@dataclass
class AgentState:
    """Current state of the agent loop.

    Attributes:
        goal: The original user goal.
        context: Additional context dict.
        iterations: Number of ticks completed.
        history: List of action/observation pairs.
        final_answer: The final answer if the loop completed.
        status: Current loop status.
        start_time: Timestamp when the loop started.
    """

    goal: str
    context: dict[str, Any] = field(default_factory=dict)
    iterations: int = 0
    history: list[ActionObservation] = field(default_factory=list)
    final_answer: str = ""
    status: str = "running"  # running, completed, failed, timeout
    start_time: float = 0.0


@dataclass
class LoopResult:
    """Result of an AgentLoop.run() call.

    Attributes:
        success: Whether the loop completed successfully.
        final_answer: The agent's final answer (if any).
        iterations: Number of ticks executed.
        status: Final status string.
        error: Error message if failed.
        duration: Wall-clock duration in seconds.
        history: Full action/observation history.
    """

    success: bool = True
    final_answer: str = ""
    iterations: int = 0
    status: str = "completed"
    error: str = ""
    duration: float = 0.0
    history: list[ActionObservation] = field(default_factory=list)


# ─── Agent Loop ──────────────────────────────────────────────────────────────


class AgentLoop:
    """Iterative autonomous agent loop.

    Attributes:
        provider: LLM provider instance with ``generate(messages, context)``.
        executor: ActionExecutor instance for executing tool commands.
        max_iterations: Maximum ticks before loop stops (default 10).
        iteration_timeout: Max seconds per tick (default 60).
        state: Current AgentState (populated after run()).
    """

    def __init__(
        self,
        provider: Any,
        executor: Any | None = None,
        max_iterations: int = DEFAULT_MAX_ITERATIONS,
        iteration_timeout: float = DEFAULT_ITERATION_TIMEOUT,
        web_search_tool: Any | None = None,
    ) -> None:
        self._provider = provider
        self._executor = executor
        self.max_iterations = max_iterations
        self.iteration_timeout = iteration_timeout
        self._web_search_tool = web_search_tool
        self.state: AgentState | None = None

    # ── Tick ───────────────────────────────────────────────────────────────

    def tick(self, state: AgentState) -> ActionObservation:
        """Execute one LLM → action → execute → observe iteration.

        Args:
            state: The current agent state (will be mutated with results).

        Returns:
            ActionObservation for this tick.
        """
        state.iterations += 1
        iteration = state.iterations

        # 1. Build messages
        messages = self._build_messages(state)

        # 2. Call LLM
        try:
            llm_response = self._provider.generate(
                messages=messages,
                context={"temperature": 0.5, "max_tokens": 2000},
            )
        except Exception as exc:
            obs = ActionObservation(
                iteration=iteration,
                action="<LLM call>",
                result=f"❌ LLM error: {exc}",
                success=False,
            )
            state.history.append(obs)
            return obs

        llm_text = (llm_response or "").strip()
        if not llm_text:
            obs = ActionObservation(
                iteration=iteration,
                action="<empty response>",
                result="❌ LLM вернул пустой ответ.",
                success=False,
            )
            state.history.append(obs)
            return obs

        # 3. Check for FINAL answer
        final_idx = llm_text.find("ФИНАЛ:")
        if final_idx >= 0:
            final_answer = llm_text[final_idx + len("ФИНАЛ:"):].strip()
            state.final_answer = final_answer
            state.status = "completed"
            obs = ActionObservation(
                iteration=iteration,
                action=llm_text,
                result=f"✅ {final_answer}",
                success=True,
            )
            state.history.append(obs)
            return obs

        # 4. Execute actions
        result_text, success = self._execute_actions(llm_text)

        obs = ActionObservation(
            iteration=iteration,
            action=llm_text,
            result=result_text,
            success=success,
        )
        state.history.append(obs)
        return obs

    # ── Run ────────────────────────────────────────────────────────────────

    def run(
        self,
        goal: str,
        context: dict[str, Any] | None = None,
    ) -> LoopResult:
        """Run the agent loop until completion or limit.

        Args:
            goal: The user's goal description.
            context: Optional additional context.

        Returns:
            LoopResult with final answer and stats.
        """
        start_time = time.monotonic()

        self.state = AgentState(
            goal=goal,
            context=context or {},
            start_time=start_time,
        )

        for iteration in range(1, self.max_iterations + 1):
            # Check timeout
            elapsed = time.monotonic() - start_time
            max_total = self.max_iterations * self.iteration_timeout * 1.5
            if elapsed > max_total:
                self.state.status = "timeout"
                break

            try:
                self.tick(self.state)
            except Exception as exc:
                self.state.status = "failed"
                duration = time.monotonic() - start_time
                logger.exception("Agent loop tick %d failed", iteration)
                return LoopResult(
                    success=False,
                    status="failed",
                    error=str(exc),
                    iterations=iteration,
                    duration=duration,
                    history=self.state.history,
                )

            # Check if finished
            if self.state.status == "completed":
                duration = time.monotonic() - start_time
                return LoopResult(
                    success=True,
                    final_answer=self.state.final_answer,
                    iterations=iteration,
                    status="completed",
                    duration=duration,
                    history=self.state.history,
                )

            # Check for persistent failures (3 consecutive fails)
            if len(self.state.history) >= 3:
                last_three = self.state.history[-3:]
                if all(not h.success for h in last_three):
                    # Check if it's the same action repeated (same parameters)
                    actions = [h.action[:100] for h in last_three]
                    if actions[0] == actions[1] == actions[2]:
                        error_msg = f"Инструмент недоступен после 3 попыток: {actions[0][:80]}"
                    else:
                        error_msg = "3 последовательных ошибки выполнения"
                    self.state.status = "failed"
                    duration = time.monotonic() - start_time
                    return LoopResult(
                        success=False,
                        status="failed",
                        error=error_msg,
                        iterations=iteration,
                        duration=duration,
                        history=self.state.history,
                    )

        # Exhausted max iterations
        self.state.status = "max_iterations"
        duration = time.monotonic() - start_time
        return LoopResult(
            success=False,
            status="max_iterations",
            error=f"Достигнут лимит в {self.max_iterations} итераций",
            iterations=self.max_iterations,
            duration=duration,
            history=self.state.history,
        )

    # ── Internal ───────────────────────────────────────────────────────────

    def _build_messages(self, state: AgentState) -> list[dict[str, str]]:
        """Build the LLM message list from current state.

        Args:
            state: Current agent state.

        Returns:
            Message list for the LLM.
        """
        messages: list[dict[str, str]] = [
            {"role": "system", "content": _AGENT_SYSTEM_PROMPT},
            {"role": "user", "content": f"Цель: {state.goal}"},
        ]

        # Add history as context
        for obs in state.history[-10:]:  # last 10 observations
            messages.append({"role": "assistant", "content": obs.action})
            result_msg = obs.result[:500] if obs.result else "(empty)"
            if obs.success:
                messages.append({"role": "tool", "content": f"Результат: {result_msg}"})
            else:
                messages.append({"role": "tool", "content": f"Ошибка: {result_msg}"})

        # Add context
        if state.context:
            ctx_lines: list[str] = []
            for k, v in state.context.items():
                ctx_lines.append(f"{k}: {v}")
            if ctx_lines:
                messages.append({"role": "user", "content": "Контекст:\n" + "\n".join(ctx_lines)})

        return messages

    def _execute_actions(self, llm_text: str) -> tuple[str, bool]:
        """Execute actions from LLM response.

        Args:
            llm_text: The LLM response text.

        Returns:
            Tuple of (result_text, success).
        """
        import re

        # Check for WEB_SEARCH command
        web_search_match = re.search(
            r"SEARCH_AND_FORMAT\|(.+)", llm_text, re.IGNORECASE
        )
        if web_search_match:
            query = web_search_match.group(1).strip()
            if self._web_search_tool is not None:
                try:
                    formatted = self._web_search_tool.search_and_format(query)
                    return formatted, True
                except RuntimeError as exc:
                    return f"❌ Search error: {exc}", False
            return (
                "❌ Web search tool not configured. "
                "Set web_search_tool on AgentLoop.",
                False,
            )

        # Check for WEB_SEARCH (alias)
        web_search_re = re.search(
            r"WEB_SEARCH\|(.+)", llm_text, re.IGNORECASE
        )
        if web_search_re:
            query = web_search_re.group(1).strip()
            if self._web_search_tool is not None:
                try:
                    formatted = self._web_search_tool.search_and_format(query)
                    return formatted, True
                except RuntimeError as exc:
                    return f"❌ Search error: {exc}", False
            return "❌ Web search tool not configured.", False

        # Use ActionExecutor for standard commands
        if self._executor is not None:
            try:

                actions = self._executor.parse_action_from_llm(llm_text)
                if actions:
                    # Try to run sync via asyncio.run (simplified)

                    results = []
                    all_success = True
                    for action in actions:
                        action_type = action.type.value if hasattr(action.type, "value") else str(action.type)
                        if action_type == "WRITE_FILE":
                            from pathlib import Path

                            p = Path(action.path)
                            p.parent.mkdir(parents=True, exist_ok=True)
                            p.write_text(action.content, encoding="utf-8")
                            results.append(f"✅ Файл {action.path} создан ({p.stat().st_size} байт).")
                        elif action_type == "RUN_SHELL":
                            import shlex
                            import subprocess

                            try:
                                parts = shlex.split(action.command)
                                proc = subprocess.run(
                                    parts,
                                    shell=False,
                                    capture_output=True,
                                    text=True,
                                    timeout=30,
                                )
                                if proc.returncode == 0:
                                    output = proc.stdout.strip()[:500]
                                    results.append(f"✅ Shell: {output or 'OK'}")
                                else:
                                    results.append(f"❌ Shell: {proc.stderr.strip()[:200]}")
                                    all_success = False
                            except subprocess.TimeoutExpired:
                                results.append("❌ Shell: timeout")
                                all_success = False
                        else:
                            results.append(f"ℹ️ {action_type}: {action.path or action.content[:80]}")
                    return "\n".join(results), all_success
            except Exception as exc:
                logger.warning("ActionExecutor failed: %s", exc)

        # Fallback: return the LLM text as-is
        return llm_text, True
