"""Agent package — autonomous agent loop with tool use.

Provides AgentLoop for iterative LLM → action → execute → observe cycles.
"""

from antigona.agent.loop import ActionObservation, AgentLoop, AgentState, LoopResult

__all__ = [
    "ActionObservation",
    "AgentLoop",
    "AgentState",
    "LoopResult",
]
