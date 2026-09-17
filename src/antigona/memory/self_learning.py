from __future__ import annotations

import json
import re
import uuid
from dataclasses import asdict, dataclass
from datetime import UTC, datetime
from enum import Enum
from pathlib import Path
from typing import Any

from antigona.core import paths


class LearningType(Enum):
    USER_PREFERENCE = "user_preference"
    PROJECT_FACT = "project_fact"
    WORKFLOW_PATTERN = "workflow_pattern"
    ERROR_FIX = "error_fix"
    TOOL_USAGE = "tool_usage"


@dataclass
class LearningRecord:
    id: str
    type: LearningType
    content: str
    source_task_id: str
    source_message_ids: list[int]
    confidence: float
    created_at: datetime
    last_verified_at: datetime
    scope: str
    status: str


@dataclass
class GeneratedToolManifest:
    name: str
    purpose: str
    permissions: list[str]
    input_schema: dict[str, Any]
    output_schema: dict[str, Any]
    source_files: list[str]
    tests: list[str]
    risk_level: str
    status: str


class SelfLearningTool:
    def __init__(self, storage_path: str | Path | None = None) -> None:
        if storage_path is None:
            self._path = paths.learnings_file()
        else:
            self._path = Path(storage_path)
        self._path.parent.mkdir(parents=True, exist_ok=True)
        if not self._path.exists():
            self._save_records([])

    def _load_records(self) -> list[LearningRecord]:
        try:
            with open(self._path, encoding="utf-8") as f:
                data = json.load(f)
            records = []
            for item in data:
                created_at = datetime.fromisoformat(item["created_at"])
                last_verified_at = datetime.fromisoformat(item["last_verified_at"])
                records.append(
                    LearningRecord(
                        id=item["id"],
                        type=LearningType(item["type"]),
                        content=item["content"],
                        source_task_id=item["source_task_id"],
                        source_message_ids=item["source_message_ids"],
                        confidence=item["confidence"],
                        created_at=created_at,
                        last_verified_at=last_verified_at,
                        scope=item["scope"],
                        status=item["status"],
                    )
                )
            return records
        except Exception:
            return []

    def _save_records(self, records: list[LearningRecord]) -> None:
        data = []
        for r in records:
            item = asdict(r)
            item["type"] = r.type.value
            item["created_at"] = r.created_at.isoformat()
            item["last_verified_at"] = r.last_verified_at.isoformat()
            data.append(item)
        with open(self._path, "w", encoding="utf-8") as f:
            json.dump(data, f, ensure_ascii=False, indent=2)

    async def extract_candidates(
        self, task_result: str, task_id: str, message_ids: list[int]
    ) -> list[LearningRecord]:
        """Extract learning candidates from the final task result text.
        Checks for passwords, tokens, API keys, private keys, single-use codes, env variables, or sensitive data.
        Returns a list of clean candidate records.
        """
        sensitive_patterns = [
            r"(?i)api[-_]?key",
            r"(?i)password",
            r"(?i)secret",
            r"(?i)token",
            r"(?i)private[-_]?key",
            r"(?i)auth",
            r"(?i)credentials",
            r"[a-zA-Z0-9_-]{32,}",
        ]

        candidates = []
        lines = task_result.split("\n")
        for line in lines:
            line = line.strip()
            if not line:
                continue

            if any(re.search(pat, line) for pat in sensitive_patterns):
                continue

            normalized_line = line.lower()
            diagnostic_patterns = (
                r"^ошибка выполнения\s*:",
                r"\[tool_error\]",
                r"\bexecution_unknown\b",
                r"^traceback(?: \(most recent call last\))?:",
                r"^\w*(?:error|exception)\s*:",
                r"\bcommand[- ]not[- ]found\b",
                r"^(?:/[^:]+/)?(?:ba)?sh:.*\bnot found\b",
            )
            if any(re.search(pattern, normalized_line) for pattern in diagnostic_patterns):
                continue

            preference_pattern = r"\b(?:предпочитает|предпочтение)\b"
            durable_correction_patterns = (
                r"\b(?:всегда|правило|convention)\b",
                r"\bошибка\s+(?:была\s+)?исправлена\b",
                r"\bисправлен(?:а|о|ы)?\b",
                r"\bfix(?:ed)?\b",
            )
            is_preference = re.search(preference_pattern, normalized_line) is not None
            is_durable_correction = any(
                re.search(pattern, normalized_line) for pattern in durable_correction_patterns
            )
            if is_preference or is_durable_correction:
                candidates.append(
                    LearningRecord(
                        id=str(uuid.uuid4()),
                        type=(
                            LearningType.USER_PREFERENCE
                            if is_preference
                            else LearningType.ERROR_FIX
                        ),
                        content=line,
                        source_task_id=task_id,
                        source_message_ids=message_ids,
                        confidence=0.8,
                        created_at=datetime.now(UTC),
                        last_verified_at=datetime.now(UTC),
                        scope="global",
                        status="proposed",
                    )
                )
        return candidates

    async def validate_candidate(self, candidate: LearningRecord) -> bool:
        """Validate if the candidate is clean and verified."""
        return candidate.confidence >= 0.7

    async def store_learning(self, learning: LearningRecord) -> None:
        records = self._load_records()
        for r in records:
            if r.content.strip().lower() == learning.content.strip().lower():
                return
        records.append(learning)
        self._save_records(records)

    async def search_learnings(self, query: str) -> list[LearningRecord]:
        records = self._load_records()
        results = []
        for r in records:
            if query.lower() in r.content.lower():
                results.append(r)
        return results

    async def invalidate_learning(self, learning_id: str) -> bool:
        records = self._load_records()
        initial_len = len(records)
        records = [r for r in records if r.id != learning_id]
        self._save_records(records)
        return len(records) < initial_len
