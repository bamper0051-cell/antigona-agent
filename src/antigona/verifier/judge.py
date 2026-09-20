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
class EffectContext:
    """Verbatim execution facts of the step that performed the effect (FP-L23).

    The secondary judge can only judge what it is handed. A bare artifact
    read-back is not enough for it to relate the artifact to the requested
    effect: with ``ls | head -2`` (live witness ``fa6e102b``) the artifact text
    collapsed into one opaque token and the judge answered *"Artifact content
    not provided"* — a false rejection of a real, hash-verified effect. These
    facts are the step's own durable record (never an assertion that the goal
    succeeded); the judge stays the decider.
    """

    artifact_path: str = ""
    artifact_size: int = 0
    artifact_sha256: str = ""
    tool: str = ""
    command: str = ""
    status: str = ""
    recorded_stdout: str = ""
    #: False when the artifact read-back carries no judgeable text (binary or
    #: blank) and the content below is the step's recorded tool output instead.
    artifact_content_usable: bool = True

    def render(self, artifact_content: str) -> str:
        """Labeled report block; the recorded stdout is omitted when identical."""
        lines = [f"Artifact path: {self.artifact_path or '(unknown)'}"]
        lines.append(f"Artifact size: {self.artifact_size} bytes")
        if self.artifact_content_usable:
            lines.append("Artifact content (verbatim):")
            lines.append(_ARTIFACT_OPEN)
            lines.append(artifact_content)
            lines.append(_ARTIFACT_CLOSE)
        else:
            lines.append(
                "Artifact content: the artifact read-back carries no judgeable "
                "text (binary or blank) — judge the recorded tool output below."
            )
        if self.tool:
            lines.append(f"Executed tool: {self.tool}")
        if self.command:
            lines.append(f"Executed command: {self.command}")
        if self.status:
            lines.append(f"Tool status: {self.status}")
        if self.recorded_stdout and (
            not self.artifact_content_usable or self.recorded_stdout != artifact_content
        ):
            lines.append("Tool stdout (recorded, verbatim):")
            lines.append(_STDOUT_OPEN)
            lines.append(self.recorded_stdout)
            lines.append(_STDOUT_CLOSE)
        if self.artifact_sha256:
            lines.append(f"Artifact sha256: {self.artifact_sha256}")
        return "\n".join(lines)


@dataclass(frozen=True)
class JudgeRequest:
    goal: str
    criteria: str
    actual_content: str
    evidence: dict[str, Any]
    #: Optional, so every existing provider/fake keeps its contract.
    effect_context: EffectContext | None = None


@dataclass(frozen=True)
class ProviderResult:
    approved: bool
    reason: str
    actual_model: str


class VerifierProvider(Protocol):
    def evaluate(self, request: JudgeRequest, *, model: str) -> ProviderResult: ...


#: Labels delimiting the artifact content inside the judge prompt.
_ARTIFACT_OPEN = "<<<ARTIFACT_CONTENT"
_ARTIFACT_CLOSE = "ARTIFACT_CONTENT>>>"
_STDOUT_OPEN = "<<<TOOL_STDOUT"
_STDOUT_CLOSE = "TOOL_STDOUT>>>"


class HTTPVerifierProvider:
    """OpenAI-compatible transport. It never substitutes a local verdict."""

    def __init__(self, api_url: str, api_key: str, *, timeout_seconds: float = 60) -> None:
        if not api_key:
            raise ProviderTransportError("verifier provider credential is missing")
        self.api_url = api_url
        self.api_key = api_key
        self.timeout_seconds = timeout_seconds

    @staticmethod
    def build_prompt(request: JudgeRequest) -> str:
        """The exact user message the secondary model is asked to judge.

        The artifact content is explicitly labelled, and — when available —
        the execution facts recorded for the effect are appended, so the model
        is never left guessing whether the artifact *is* the effect trace
        (FP-L23: an unlabelled 72-char stdout token was read as "only a
        SHA-256 hash" and the task was failed despite a real effect).
        """
        prompt = (
            "Evaluate the artifact against the private criteria. JSON only: "
            '{"decision":"DONE|REPLAN","reason":"..."}.\n'
            f"Goal: {request.goal}\nPrivate criteria: {request.criteria}\n"
            f"Artifact: {request.actual_content}\nEvidence: {json.dumps(request.evidence)}"
        )
        if request.effect_context is not None:
            prompt = f"{prompt}\n{request.effect_context.render(request.actual_content)}"
        return prompt

    def evaluate(self, request: JudgeRequest, *, model: str) -> ProviderResult:
        prompt = self.build_prompt(request)
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
        effect_context: EffectContext | None = None,
    ) -> ProviderResult:
        result = self.provider.evaluate(
            JudgeRequest(
                goal, criteria, actual_content, evidence or {}, effect_context
            ),
            model=self.model_name,
        )
        if result.actual_model != self.model_name:
            raise ProviderModelMismatch(
                f"configured verifier model {self.model_name!r} but provider used {result.actual_model!r}"
            )
        return result
