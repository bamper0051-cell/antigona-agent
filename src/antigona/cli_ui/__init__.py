"""Static package boundary for the future canonical task-oriented CLI client.

Checkpoint 1 intentionally contains metadata only: no commands, prompts, renderer,
provider integration, persistence, or runtime implementation.
"""

PROMPT_HISTORY_DEFAULT = False
CLIENT_MODE = "task-oriented-thin-client"
CHAT_CONTRACT = "future-antigona-chat"

__all__ = ["CHAT_CONTRACT", "CLIENT_MODE", "PROMPT_HISTORY_DEFAULT"]
