from __future__ import annotations

import json
import urllib.error
import urllib.request
from dataclasses import dataclass
from typing import Any, Protocol


class VerifierProviderError(RuntimeError):
    """Base class for failures at the independent-model boundary."""


class ProviderTransportError(VerifierProviderError):
    pass


class ProviderMalformedResponse(VerifierProviderError):
    pass


class ProviderModelMismatch(VerifierProviderError):
    pass


class ModelCollisionError(ValueError):
    pass


@dataclass(frozen=True)
class JudgeRequest:
    goal: str
    criteria: str
    actual_content: str
    evidence: dict[str, Any]


@dataclass(frozen=True)
class ProviderResult:
    approved: bool
    reason: str
    actual_model: str


class VerifierProvider(Protocol):
    def evaluate(self, request: JudgeRequest, *, model: str) -> ProviderResult: ...


class HTTPVerifierProvider:
    """OpenAI-compatible transport. It never substitutes a local verdict."""

    def __init__(self, api_url: str, api_key: str, *, timeout_seconds: float = 60) -> None:
        if not api_key:
            raise ProviderTransportError("verifier provider credential is missing")
        self.api_url = api_url
        self.api_key = api_key
        self.timeout_seconds = timeout_seconds

    def evaluate(self, request: JudgeRequest, *, model: str) -> ProviderResult:
        prompt = (
            "Evaluate the artifact against the private criteria. JSON only: "
            '{"decision":"DONE|REPLAN","reason":"..."}.\n'
            f"Goal: {request.goal}\nPrivate criteria: {request.criteria}\n"
            f"Artifact: {request.actual_content}\nEvidence: {json.dumps(request.evidence)}"
        )
        payload = json.dumps({
            "model": model,
            "messages": [
                {"role": "system", "content": "You are an independent strict verifier."},
                {"role": "user", "content": prompt},
            ],
            "response_format": {"type": "json_object"},
            "temperature": 0.0,
        }).encode()
        req = urllib.request.Request(
            self.api_url,
            data=payload,
            headers={"Authorization": f"Bearer {self.api_key}", "Content-Type": "application/json"},
            method="POST",
        )
        try:
            with urllib.request.urlopen(req, timeout=self.timeout_seconds) as response:
                raw: object = json.loads(response.read().decode())
        except (OSError, urllib.error.URLError, TimeoutError) as exc:
            raise ProviderTransportError("verifier provider unavailable") from exc
        except (UnicodeError, json.JSONDecodeError) as exc:
            raise ProviderMalformedResponse("provider response is not valid JSON") from exc
        try:
            if not isinstance(raw, dict):
                raise TypeError
            actual_model = raw["model"]
            content = raw["choices"][0]["message"]["content"]
            verdict = json.loads(content)
            decision = verdict["decision"]
            reason = verdict["reason"]
            if not isinstance(actual_model, str) or decision not in {"DONE", "REPLAN"} or not isinstance(reason, str):
                raise TypeError
        except (KeyError, IndexError, TypeError, json.JSONDecodeError) as exc:
            raise ProviderMalformedResponse("provider response has an invalid schema") from exc
        return ProviderResult(decision == "DONE", reason, actual_model)


class LLMJudge:
    def __init__(
        self,
        *,
        primary_model: str = "legacy-primary-test-model",
        verifier_model: str = "legacy-verifier-test-model",
        provider: VerifierProvider | None = None,
    ) -> None:
        if primary_model == verifier_model:
            raise ModelCollisionError("primary_model and verifier_model must differ")
        self.primary_model = primary_model
        self.model_name = verifier_model
        if provider is None:
            raise ProviderTransportError("verifier provider is required")
        self.provider = provider

    def evaluate(
        self,
        goal: str,
        criteria: str,
        actual_content: str,
        evidence: dict[str, Any] | None = None,
    ) -> ProviderResult:
        result = self.provider.evaluate(
            JudgeRequest(goal, criteria, actual_content, evidence or {}), model=self.model_name
        )
        if result.actual_model != self.model_name:
            raise ProviderModelMismatch(
                f"configured verifier model {self.model_name!r} but provider used {result.actual_model!r}"
            )
        return result
