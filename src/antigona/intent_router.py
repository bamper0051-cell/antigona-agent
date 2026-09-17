"""Backward-compatibility shim — re-exports from antigona.router.intent_router.

This file exists so existing imports (``from antigona.intent_router import ...``)
continue to work after the module was moved to ``antigona.router.intent_router``.
New code should import directly from ``antigona.router.intent_router``.
"""
from __future__ import annotations

# ruff: noqa: F401 — we intentionally re-export everything
from antigona.router.intent_router import (
    ConversationState,
    IntentDecision,
    IntentRouter,
    clarify_reply,
)

__all__ = [
    "ConversationState",
    "IntentDecision",
    "IntentRouter",
    "clarify_reply",
]
