"""OpenAI-compatible provider — uses httpx to call any OpenAI-compatible API."""

from __future__ import annotations

import logging
from typing import Any

import httpx

from antigona.providers.base import BaseProvider, ProviderError

logger = logging.getLogger(__name__)

# Reasoning models (deepseek-v4-flash and similar) spend ``max_tokens`` on
# ``reasoning_content`` first. A small budget yields HTTP 200, empty
# ``message.content``, and ``finish_reason="length"``. Retry once with a
# budget large enough for both thoughts and the actual answer.
_DEFAULT_MAX_TOKENS = 1024
_REASONING_RETRY_MAX_TOKENS = 4096


class OpenAICompatibleProvider(BaseProvider):
    """Provider that calls an OpenAI-compatible chat completion endpoint.

    Accepts any base_url, api_key, and model name — works with OpenAI,
    Nous, OpenRouter, Ollama (via /v1/chat/completions), and any other
    OpenAI-compatible API.
    """

    name: str = "openai_compatible"

    def __init__(
        self,
        base_url: str = "https://api.openai.com/v1",
        api_key: str = "",
        model: str = "gpt-4o-mini",
        timeout_seconds: int = 30,
    ) -> None:
        """Initialize the OpenAI-compatible provider.

        Args:
            base_url: Base URL of the API (e.g. "https://api.openai.com/v1").
            api_key: API key for authentication.
            model: Model name to use (e.g. "gpt-4o-mini", "claude-sonnet-4-20250514").
            timeout_seconds: HTTP request timeout in seconds.
        """
        self._base_url = base_url.rstrip("/")
        self._api_key = api_key
        self._model = model
        self._timeout = timeout_seconds
        self._client: httpx.Client | None = None

    @property
    def base_url(self) -> str:
        """Resolved API base URL (no trailing slash)."""
        return self._base_url

    @property
    def api_key(self) -> str:
        """Resolved API credential. Empty string when unauthenticated (local)."""
        return self._api_key

    @property
    def model(self) -> str:
        """Resolved model identifier."""
        return self._model

    @property
    def _http_client(self) -> httpx.Client:
        """Lazy-initialized httpx client."""
        if self._client is None:
            is_local = "127.0.0.1" in self._base_url or "localhost" in self._base_url
            self._client = httpx.Client(timeout=self._timeout, trust_env=not is_local)
        return self._client

    def generate(self, messages: list[dict[str, str]], context: dict[str, Any] | None = None) -> str:
        """Send messages to the API and return the response text.

        Args:
            messages: OpenAI-compatible message list
                ([{"role": "user"|"assistant"|"system", "content": "..."}, ...]).
            context: Optional overrides — supports "temperature", "max_tokens",
                "model" (overrides the constructor model).

        Returns:
            The assistant's reply text.

        Raises:
            ProviderError: On HTTP or API errors.
        """
        ctx = context or {}
        model = ctx.get("model", self._model)
        temperature = ctx.get("temperature", 0.7)
        max_tokens = int(ctx.get("max_tokens", _DEFAULT_MAX_TOKENS) or _DEFAULT_MAX_TOKENS)
        if max_tokens <= 0:
            max_tokens = _DEFAULT_MAX_TOKENS

        content, finish_reason, reasoning = self._chat_complete(
            messages,
            model=str(model),
            temperature=temperature,
            max_tokens=max_tokens,
        )
        if (
            not content.strip()
            and finish_reason == "length"
            and reasoning.strip()
        ):
            retry_tokens = max(max_tokens * 2, _REASONING_RETRY_MAX_TOKENS)
            if retry_tokens > max_tokens:
                logger.warning(
                    "empty content with finish_reason=length "
                    "(reasoning consumed max_tokens=%s); retrying with %s",
                    max_tokens,
                    retry_tokens,
                )
                content, _finish_reason, _reasoning = self._chat_complete(
                    messages,
                    model=str(model),
                    temperature=temperature,
                    max_tokens=retry_tokens,
                )
        return content

    def _chat_complete(
        self,
        messages: list[dict[str, str]],
        *,
        model: str,
        temperature: Any,
        max_tokens: int,
    ) -> tuple[str, str, str]:
        """POST /chat/completions and return (content, finish_reason, reasoning)."""
        headers: dict[str, str] = {
            "Content-Type": "application/json",
        }
        if self._api_key:
            headers["Authorization"] = f"Bearer {self._api_key}"

        payload: dict[str, Any] = {
            "model": model,
            "messages": messages,
            "temperature": temperature,
            "max_tokens": max_tokens,
        }

        try:
            response = self._http_client.post(
                f"{self._base_url}/chat/completions",
                headers=headers,
                json=payload,
            )
        except httpx.RequestError as exc:
            raise ProviderError(f"HTTP request failed: {exc}") from exc

        if response.status_code != 200:
            detail = response.text[:500] if response.text else "no detail"
            raise ProviderError(
                f"API returned {response.status_code}: {detail}"
            )

        try:
            body = response.json()
            choice = body["choices"][0]
            message = choice["message"]
            raw_content = message.get("content") or ""
            content = raw_content if isinstance(raw_content, str) else str(raw_content)
            finish_reason = str(choice.get("finish_reason") or "")
            raw_reasoning = message.get("reasoning_content") or ""
            reasoning = (
                raw_reasoning if isinstance(raw_reasoning, str) else str(raw_reasoning)
            )
            return content, finish_reason, reasoning
        except (KeyError, IndexError, ValueError, TypeError, AttributeError) as exc:
            raise ProviderError(f"Unexpected API response format: {exc}") from exc

    def close(self) -> None:
        """Close the underlying HTTP client."""
        if self._client is not None:
            self._client.close()
            self._client = None
