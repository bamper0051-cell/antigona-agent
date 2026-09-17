"""Unit test for multi-file clarification with paths in AntigonaBrain."""

from __future__ import annotations

import pytest

from antigona.core.brain import AntigonaBrain, ResponseType
from antigona.router.intent_router import IntentDecision


@pytest.mark.asyncio
async def test_handle_clarification_with_multi_file_paths() -> None:
    brain = AntigonaBrain()
    intent = IntentDecision(
        intent="ambiguous.mixed_intent",
        confidence=0.85,
        response_mode="clarify",
        requires_planner=False,
        requires_approval=False,
        entities={"path": "l3_a.txt", "paths": ["l3_a.txt", "l3_b.txt"]},
        reason_code="multi_file_requires_spec",
    )

    response = await brain._handle_clarification("обнови l3_a.txt и l3_b.txt", "session_1", intent)

    assert response.response_type == ResponseType.CLARIFICATION
    assert "l3_a.txt" in response.text
    assert "l3_b.txt" in response.text
    assert response.text == "Обнаружено несколько файлов: l3_a.txt, l3_b.txt. Уточните, что именно нужно сделать."


@pytest.mark.asyncio
async def test_handle_clarification_generic_fallback() -> None:
    brain = AntigonaBrain()
    intent = IntentDecision(
        intent="ambiguous.mixed_intent",
        confidence=0.85,
        response_mode="clarify",
        requires_planner=False,
        requires_approval=False,
        entities={},
        reason_code="ambiguous_context",
    )

    response = await brain._handle_clarification("что-то непонятное", "session_1", intent)

    assert response.response_type == ResponseType.CLARIFICATION
    assert "Уточните, пожалуйста, что нужно сделать" in response.text
