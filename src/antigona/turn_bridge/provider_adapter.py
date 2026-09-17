"""ProviderAdapter — async OpenAI-compatible LLM provider wrapper.

Wraps an HTTP-based chat-completion API with error classification,
iteration budget tracking, and optional fallback chain.
"""

from __future__ import annotations

import json
import logging
from dataclasses import dataclass
from enum import StrEnum
from typing import Any, cast

import httpx

LOGGER = logging.getLogger(__name__)

# ── Error classification ───────────────────────────────────────────────────


class ErrorCategory(StrEnum):
    """Machine-readable error categories for provider responses."""

    NONE = "none"
    RATE_LIMIT = "rate_limit"
    TIMEOUT = "timeout"
    AUTH_ERROR = "auth_error"
    BAD_REQUEST = "bad_request"
    OVERLOADED = "overloaded"
    CONTEXT_LENGTH = "context_length"
    SERVICE_ERROR = "service_error"
    UNKNOWN = "unknown"


@dataclass
class ProviderError(Exception):
    """An error returned by or detected in the LLM provider.

    Attributes:
        category: Machine-readable error category.
        message: Human-readable description.
        status_code: HTTP status code, if applicable.
        retryable: Whether the caller may reasonably retry.
    """

    category: ErrorCategory
    message: str
    status_code: int | None = None
    retryable: bool = False

    def __str__(self) -> str:
        parts = [f"[{self.category.value}]"]
        if self.status_code:
            parts.append(f"HTTP {self.status_code}")
        parts.append(self.message)
        return " ".join(parts)


def classify_error(exc: Exception, response_body: str | None = None) -> ProviderError:
    """Classify an arbitrary exception into a :class:`ProviderError`.

    Args:
        exc: The original exception.
        response_body: Optional raw response body for inspecting message content.

    Returns:
        A classified ProviderError.
    """
    status_code: int | None = getattr(exc, "status_code", None)
    if status_code is None and hasattr(exc, "response"):
        status_code = getattr(exc.response, "status_code", None)

    text = str(exc).lower() + (response_body or "").lower()

    # Rate limits
    if status_code == 429 or "rate limit" in text or "too many requests" in text:
        return ProviderError(
            category=ErrorCategory.RATE_LIMIT,
            message=str(exc),
            status_code=status_code,
            retryable=True,
        )

    # Auth errors
    if status_code == 401 or "unauthorized" in text or "invalid api key" in text:
        return ProviderError(
            category=ErrorCategory.AUTH_ERROR,
            message=str(exc),
            status_code=status_code,
            retryable=False,
        )

    # Context length
    if "context_length" in text or "maximum context" in text or "max tokens" in text:
        return ProviderError(
            category=ErrorCategory.CONTEXT_LENGTH,
            message=str(exc),
            status_code=status_code,
            retryable=False,
        )

    # Timeouts
    if isinstance(exc, TimeoutError) or "timeout" in text:
        return ProviderError(
            category=ErrorCategory.TIMEOUT,
            message=str(exc),
            status_code=status_code,
            retryable=True,
        )

    # Overloaded
    if status_code == 503 or "overloaded" in text or "service unavailable" in text:
        return ProviderError(
            category=ErrorCategory.OVERLOADED,
            message=str(exc),
            status_code=status_code,
            retryable=True,
        )

    # Bad requests
    if status_code == 400 or "bad request" in text:
        return ProviderError(
            category=ErrorCategory.BAD_REQUEST,
            message=str(exc),
            status_code=status_code,
            retryable=False,
        )

    # HTTP 5xx
    if status_code and 500 <= status_code < 600:
        return ProviderError(
            category=ErrorCategory.SERVICE_ERROR,
            message=str(exc),
            status_code=status_code,
            retryable=True,
        )

    return ProviderError(
        category=ErrorCategory.UNKNOWN,
        message=str(exc),
        status_code=status_code,
        retryable=False,
    )


# ── Provider adapter config ────────────────────────────────────────────────


@dataclass
class ProviderAdapterConfig:
    """Configuration for a single LLM provider.

    Attributes:
        base_url: OpenAI-compatible API base URL (e.g. ``https://api.openai.com/v1``).
        api_key: API key for authentication.
        model: Model identifier (e.g. ``gpt-4o``, ``deepseek-v4-flash``).
        timeout_seconds: HTTP request timeout.  Default 120.
        max_retries: How many times to retry on transient errors.  Default 2.
    """

    base_url: str
    api_key: str
    model: str
    timeout_seconds: int = 120
    max_retries: int = 2


# ── Provider adapter ───────────────────────────────────────────────────────


class ProviderAdapter:
    """Async adapter for an OpenAI-compatible chat-completion API.

    Wraps the HTTP transport, error classification, and transient retry logic
    behind a simple ``chat()`` method.
    """

    def __init__(
        self,
        config: ProviderAdapterConfig,
        client: httpx.AsyncClient | None = None,
    ) -> None:
        self._config = config
        self._client = client or httpx.AsyncClient(timeout=config.timeout_seconds)

    @property
    def config(self) -> ProviderAdapterConfig:
        return self._config

    @property
    def model(self) -> str:
        return self._config.model

    async def chat(
        self,
        messages: list[dict[str, Any]],
        tools: list[dict[str, Any]] | None = None,
        *,
        temperature: float = 0.0,
        max_tokens: int | None = None,
    ) -> dict[str, Any]:
        """Send a chat-completion request and return the parsed response.

        Args:
            messages: List of message dicts (``role``, ``content``, etc.).
            tools: Optional list of tool definitions in OpenAI tool format.
            temperature: Sampling temperature (0.0 = deterministic).
            max_tokens: Maximum tokens in the response.

        Returns:
            The parsed JSON response body as a dict.

        Raises:
            ProviderError: Classified provider error after exhausting retries.
        """
        url = f"{self._config.base_url.rstrip('/')}/chat/completions"
        headers = {
            "Authorization": f"Bearer {self._config.api_key}",
            "Content-Type": "application/json",
        }
        body: dict[str, Any] = {
            "model": self._config.model,
            "messages": messages,
            "temperature": temperature,
        }
        if tools is not None:
            body["tools"] = tools
            body["tool_choice"] = "auto"
        if max_tokens is not None:
            body["max_tokens"] = max_tokens

        last_error: ProviderError | None = None

        for attempt in range(self._config.max_retries + 1):
            try:
                response = await self._client.post(url, headers=headers, json=body)
                body_text = response.text
            except httpx.TimeoutException as exc:
                last_error = classify_error(exc)
                LOGGER.warning(
                    "Provider timeout (attempt %d/%d): %s",
                    attempt + 1,
                    self._config.max_retries + 1,
                    last_error,
                )
                if last_error.retryable and attempt < self._config.max_retries:
                    continue
                raise last_error from exc
            except httpx.HTTPStatusError as exc:
                last_error = classify_error(exc, response_body=exc.response.text if hasattr(exc, "response") else None)
                LOGGER.warning(
                    "Provider HTTP error (attempt %d/%d): %s",
                    attempt + 1,
                    self._config.max_retries + 1,
                    last_error,
                )
                if last_error.retryable and attempt < self._config.max_retries:
                    continue
                raise last_error from exc
            except httpx.RequestError as exc:
                last_error = classify_error(exc)
                LOGGER.warning(
                    "Provider request error (attempt %d/%d): %s",
                    attempt + 1,
                    self._config.max_retries + 1,
                    last_error,
                )
                if attempt < self._config.max_retries:
                    continue
                raise last_error from exc

            # Successful response — parse and validate
            try:
                parsed = response.json()
            except json.JSONDecodeError as exc:
                last_error = ProviderError(
                    category=ErrorCategory.SERVICE_ERROR,
                    message=f"Invalid JSON response: {exc}",
                    status_code=response.status_code,
                    retryable=False,
                )
                raise last_error from exc

            if not response.is_success:
                error_message = _extract_error_message(parsed) or body_text[:500]
                last_error = classify_error(
                    httpx.HTTPStatusError(
                        error_message,
                        request=response.request,
                        response=response,
                    ),
                    response_body=body_text,
                )
                LOGGER.warning(
                    "Provider non-200 (attempt %d/%d): %s",
                    attempt + 1,
                    self._config.max_retries + 1,
                    last_error,
                )
                if last_error.retryable and attempt < self._config.max_retries:
                    continue
                raise last_error

            return cast(dict[str, Any], parsed)

        # Should be unreachable — last_error is always set if we get here
        raise last_error or ProviderError(
            category=ErrorCategory.UNKNOWN,
            message="Exhausted retries without a classified error",
            retryable=False,
        )

    async def close(self) -> None:
        """Close the underlying HTTP client."""
        await self._client.aclose()


def _extract_error_message(parsed: dict[str, Any]) -> str | None:
    """Extract a human-readable error message from a provider error body."""
    error = parsed.get("error")
    if isinstance(error, dict):
        return error.get("message") or error.get("code") or None
    if isinstance(error, str):
        return error
    return None
