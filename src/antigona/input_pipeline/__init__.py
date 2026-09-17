"""InputPipeline — normalise, resolve, route, submit.

This subpackage provides the single entry point for all user input
processing in Antigona, regardless of source channel.
"""

from __future__ import annotations

from antigona.input_pipeline.binding_repository import BindingRepository
from antigona.input_pipeline.models import (
    ProcessingOutcome,
    ProcessingResult,
    UserInputEnvelope,
)
from antigona.input_pipeline.pipeline import (
    process_user_input,
    recover_all_contexts,
    recover_context_after_restart,
)

__all__ = [
    "BindingRepository",
    "ProcessingOutcome",
    "ProcessingResult",
    "UserInputEnvelope",
    "process_user_input",
    "recover_context_after_restart",
    "recover_all_contexts",
]
