"""MemorySummarizer — turn buffer, session summary, active topic tracking.

Maintains an in-memory record of the current conversation for context-aware
routing and followup resolution.

Turn buffer
  Stores the last N messages (user + assistant) for the current dialog.
  Each entry records role, content, intent, and timestamp.

Session summary
  A brief free-text summary of the session: who the user is, what was done,
  the last intent. Updated lazily as messages arrive.

Active topic
  The last discussed topic, extracted from IntentDecision entities and user
  message text. Used by IntentRouter to resolve followups without full context.

Usage::

    summarizer = MemorySummarizer(buffer_size=20)
    summarizer.push_user_turn("создай файл test.txt", intent="task.file_write")
    summarizer.push_assistant_turn("task_preview", intent="task.file_write")
    summarizer.update_topic("создай файл test.txt", intent="task.file_write")
    print(summarizer.active_topic)
    print(summarizer.session_summary)
"""

from __future__ import annotations

from datetime import UTC, datetime
from typing import Any


class MemorySummarizer:
    """In-memory conversation summarizer.

    Attributes:
        turn_buffer: Rolling list of the last ``buffer_size`` turns.
        session_summary: Brief free-text summary of the current session.
        active_topic: The last inferred discussion topic.
    """

    def __init__(self, buffer_size: int = 20, persist_path: str = "") -> None:
        self._buffer_size = buffer_size
        self._persist_path = persist_path
        self.turn_buffer: list[dict[str, Any]] = []
        self.session_summary: str = ""
        self.active_topic: str = ""
        self._turn_count: int = 0
        if persist_path:
            self._load_persisted()

    # ── Turn buffer ────────────────────────────────────────────────────────

    def push_turn(
        self,
        role: str,
        content: str,
        intent: str = "",
        metadata: dict[str, Any] | None = None,
    ) -> None:
        """Add a single turn to the buffer. Evicts oldest if over capacity.

        Args:
            role: 'user' or 'assistant'.
            content: The message text.
            intent: The intent string from the router (or 'command.…').
            metadata: Optional additional key/value pairs.
        """
        entry: dict[str, Any] = {
            "role": role,
            "content": content,
            "intent": intent,
            "timestamp": datetime.now(UTC).isoformat(),
        }
        if metadata:
            entry["metadata"] = metadata

        self.turn_buffer.append(entry)
        self._turn_count += 1

        if len(self.turn_buffer) > self._buffer_size:
            self.turn_buffer.pop(0)

        if self._persist_path:
            self._save_persisted()

    def push_user_turn(
        self,
        content: str,
        intent: str = "",
        metadata: dict[str, Any] | None = None,
    ) -> None:
        """Convenience: push a user turn."""
        self.push_turn("user", content, intent=intent, metadata=metadata)

    def push_assistant_turn(
        self,
        content: str,
        intent: str = "",
        metadata: dict[str, Any] | None = None,
    ) -> None:
        """Convenience: push an assistant turn."""
        self.push_turn("assistant", content, intent=intent, metadata=metadata)

    def get_recent_turns(self, n: int = 5) -> list[dict[str, Any]]:
        """Return the last *n* turns (or fewer if buffer is shorter)."""
        return self.turn_buffer[-n:]

    def clear_buffer(self) -> None:
        """Clear the turn buffer (keeps summary and topic)."""
        self.turn_buffer.clear()
        if self._persist_path:
            self._save_persisted()

    def _save_persisted(self) -> None:
        """Save turn_buffer and summary to JSON file."""
        if not self._persist_path:
            return
        import json
        import pathlib
        pathlib.Path(self._persist_path).parent.mkdir(parents=True, exist_ok=True)
        data = {
            "turn_buffer": self.turn_buffer,
            "session_summary": self.session_summary,
            "active_topic": self.active_topic,
            "turn_count": self._turn_count,
        }
        pathlib.Path(self._persist_path).write_text(json.dumps(data, default=str))
        import os
        os.chmod(self._persist_path, 0o600)

    def _load_persisted(self) -> None:
        """Load turn_buffer and summary from JSON file."""
        import json
        import pathlib
        try:
            data = json.loads(pathlib.Path(self._persist_path).read_text())
            self.turn_buffer = data.get("turn_buffer", [])
            self.session_summary = data.get("session_summary", "")
            self.active_topic = data.get("active_topic", "")
            self._turn_count = data.get("turn_count", 0)
        except (FileNotFoundError, json.JSONDecodeError):
            pass

    @property
    def turn_count(self) -> int:
        """Total number of turns ever pushed (not capped by buffer_size)."""
        return self._turn_count

    # ── Session summary ────────────────────────────────────────────────────

    def update_summary(
        self,
        user_id: str = "",
        user_name: str = "",
        task_type: str = "",
        last_intent: str = "",
    ) -> str:
        """Generate or update the session summary from current state.

        Combines caller-supplied metadata with the active topic and turn count
        into a concise summary string.

        Args:
            user_id: Optional user identifer (chat ID, telegram ID).
            user_name: Optional user display name.
            task_type: The type of task currently being worked on.
            last_intent: The last intent string from the router.

        Returns:
            The updated summary string.
        """
        parts: list[str] = []

        if user_name:
            parts.append(f"User: {user_name}")
        elif user_id:
            parts.append(f"User ID: {user_id}")

        parts.append(f"Turns: {self._turn_count}")

        if self.active_topic:
            # Truncate long topics in the summary
            topic = self.active_topic[:120]
            if len(self.active_topic) > 120:
                topic += "…"
            parts.append(f"Topic: {topic}")

        if task_type:
            parts.append(f"Task: {task_type}")
        elif last_intent:
            # Derive a human-readable task description from intent
            parts.append(f"Last intent: {last_intent}")

        self.session_summary = " | ".join(parts)
        return self.session_summary

    # ── Active topic ───────────────────────────────────────────────────────

    def update_topic(
        self,
        text: str,
        intent: str = "",
        entities: dict[str, Any] | None = None,
    ) -> str:
        """Extract and store the active topic from a message and its decision.

        Priority order for topic extraction:
          1. Explicit ``topic`` key from *entities*.
          2. ``path`` from *entities* (file path is a strong topic signal).
          3. ``command`` from *entities* (shell command context).
          4. First sentence/clause of *text* (capped at 200 chars).

        Args:
            text: The raw message text.
            intent: The router intent string.
            entities: Optional entities dict from the IntentDecision.

        Returns:
            The updated active_topic string.
        """
        if entities:
            topic = entities.get("topic")
            if topic and isinstance(topic, str) and topic.strip():
                self.active_topic = topic.strip()[:200]
                return self.active_topic

            path = entities.get("path")
            if path and isinstance(path, str) and path.strip():
                self.active_topic = path.strip()[:200]
                return self.active_topic

            command = entities.get("command")
            if command and isinstance(command, str) and command.strip():
                self.active_topic = command.strip()[:200]
                return self.active_topic

        # Fall back to text content
        stripped = text.strip()
        if stripped:
            # Take first sentence/clause as topic hint
            topic = stripped.split(".")[0].split("?")[0].split("!")[0][:200]
            if topic:
                self.active_topic = topic

        return self.active_topic

    # ── Context compression ──────────────────────────────────────────────────

    def should_compress(self) -> bool:
        """Check if the turn buffer is large enough to warrant compression.

        Returns:
            True when the buffer has more than 15 entries.
        """
        return len(self.turn_buffer) > 15

    def get_compressible_turns(self, count: int = 10) -> list[dict[str, Any]]:
        """Return the oldest *count* turns for compression (not the most recent).

        Returns:
            List of the oldest turn dicts, or fewer if the buffer is small.
        """
        if len(self.turn_buffer) <= count:
            return []
        return self.turn_buffer[:count]

    def apply_compression(
        self, old_turns: list[dict[str, Any]], compressed_summary: str
    ) -> None:
        """Remove *old_turns* from the buffer and inject the compressed summary.

        Args:
            old_turns: The turns that were compressed (must be the oldest entries).
            compressed_summary: The LLM-generated summary text.
        """
        count = len(old_turns)
        if count == 0:
            return
        # Remove the oldest N entries (they must match the front of the buffer)
        del self.turn_buffer[:count]

        # Update session summary with the compressed content
        if compressed_summary:
            if self.session_summary:
                self.session_summary = (
                    f"[Compressed: {compressed_summary}] | {self.session_summary}"
                )
            else:
                self.session_summary = f"[Compressed: {compressed_summary}]"

        if self._persist_path:
            self._save_persisted()

    # ── Reset ──────────────────────────────────────────────────────────────

    def reset(self, buffer_size: int | None = None) -> None:
        """Reset all state (buffer, summary, topic, counter).

        Args:
            buffer_size: Optional new buffer size. If None, keeps the current one.
        """
        self.turn_buffer.clear()
        self.session_summary = ""
        self.active_topic = ""
        self._turn_count = 0
        if buffer_size is not None:
            self._buffer_size = buffer_size
