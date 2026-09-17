"""Message adapter — convert between Antigona and Hermes message formats.

The Antigona message format uses simple ``{"role": ..., "content": ...}`` dicts.
The Hermes/OpenAI provider format is the same for text messages but uses a
structured ``tool_calls`` field for assistant tool-call messages and individual
``tool_call_id``-based ``tool`` role messages for results.

This module handles those conversions bidirectionally.
"""

from __future__ import annotations

from typing import Any


def message_to_hermes(msg: dict[str, Any]) -> dict[str, Any]:
    """Convert an Antigona message dict to the provider message format.

    The Antigona format is already close to OpenAI format:
      - ``role``: ``system`` | ``user`` | ``assistant`` | ``tool``
      - ``content``: text content

    Assistant messages may carry an optional ``tool_calls`` list.
    Tool messages carry a ``tool_call_id`` field.

    Args:
        msg: An Antigona message dict.

    Returns:
        A provider-format message dict, unchanged if already compatible.
    """
    role = msg.get("role", "user")
    hermes: dict[str, Any] = {"role": role}

    # Content — may be absent for assistant tool-call-only messages
    if "content" in msg:
        hermes["content"] = msg["content"] or ""
    elif role == "assistant" and "tool_calls" not in msg:
        hermes["content"] = ""

    # Tool calls (assistant messages only)
    if role == "assistant" and "tool_calls" in msg:
        hermes["tool_calls"] = msg["tool_calls"]

    # Tool call id (tool-role messages)
    if role == "tool":
        hermes["tool_call_id"] = msg.get("tool_call_id", "")
        # Ensure content is a string
        if hermes.get("content") is None:
            hermes["content"] = ""

    # Name field (optional, for function/tool messages)
    if "name" in msg:
        hermes["name"] = msg["name"]

    return hermes


def message_from_hermes(hermes_msg: dict[str, Any]) -> dict[str, Any]:
    """Convert a provider-format message to an Antigona-format message.

    Args:
        hermes_msg: A provider-format message dict.

    Returns:
        An Antigona-format message dict.
    """
    msg: dict[str, Any] = {}

    role = hermes_msg.get("role", "")
    msg["role"] = role

    # Content
    content = hermes_msg.get("content")
    if content is not None:
        msg["content"] = content
    elif role == "assistant":
        # tool-call-only messages may have empty content
        msg["content"] = ""

    # Tool calls (assistant messages)
    if "tool_calls" in hermes_msg:
        msg["tool_calls"] = hermes_msg["tool_calls"]

    # Tool call id (tool messages)
    if "tool_call_id" in hermes_msg:
        msg["tool_call_id"] = hermes_msg["tool_call_id"]

    # Name
    if "name" in hermes_msg:
        msg["name"] = hermes_msg["name"]

    return msg


def convert_messages_to_provider(messages: list[dict[str, Any]]) -> list[dict[str, Any]]:
    """Convert a list of Antigona messages to provider format."""
    return [message_to_hermes(m) for m in messages]


def convert_messages_from_provider(hermes_messages: list[dict[str, Any]]) -> list[dict[str, Any]]:
    """Convert a list of provider messages to Antigona format."""
    return [message_from_hermes(m) for m in hermes_messages]
