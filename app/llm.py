"""The one model client (ADR 0003): OpenAI-compatible chat completions over httpx.

Provider, model and key come from the environment, so switching provider is a
configuration change. The key is sent only in the Authorization header and is
never logged, printed or included in an error message.

    LLM_BASE_URL   default https://api.telnyx.com/v2/ai/openai
    LLM_MODEL      required
    LLM_API_KEY    or TELNYX_API_KEY
    LLM_EXTRA_BODY optional JSON merged into each request, e.g. to turn reasoning off
"""

from __future__ import annotations

import json
import os
import time
from dataclasses import dataclass, field
from typing import Any

import httpx

DEFAULT_BASE_URL = "https://api.telnyx.com/v2/ai/openai"
RETRY_STATUS = {429, 500, 502, 503, 504}


class LLMError(Exception):
    """A model call failed. The message never contains the API key."""


@dataclass
class Reply:
    message: dict[str, Any]
    usage: dict[str, Any] = field(default_factory=dict)
    seconds: float = 0.0

    @property
    def tool_calls(self) -> list[dict[str, Any]]:
        return self.message.get("tool_calls") or []

    @property
    def reasoned(self) -> bool:
        """True when the provider returned reasoning text or counted reasoning tokens."""
        details = self.usage.get("completion_tokens_details") or {}
        return bool(self.message.get("reasoning_content") or details.get("reasoning_tokens"))


@dataclass
class ChatClient:
    model: str
    api_key: str = field(repr=False)
    base_url: str = DEFAULT_BASE_URL
    timeout: float = 60.0
    retries: int = 3
    transport: httpx.BaseTransport | None = field(default=None, repr=False)
    # Provider-specific fields merged into every request, such as a switch that turns
    # reasoning off. Set with LLM_EXTRA_BODY (JSON). Never holds secrets.
    extra_body: dict[str, Any] = field(default_factory=dict)

    @classmethod
    def from_env(cls, model: str | None = None) -> ChatClient:
        key = os.environ.get("LLM_API_KEY") or os.environ.get("TELNYX_API_KEY")
        chosen = model or os.environ.get("LLM_MODEL")
        if not key:
            raise LLMError("set LLM_API_KEY or TELNYX_API_KEY")
        if not chosen:
            raise LLMError("set LLM_MODEL")
        extra = os.environ.get("LLM_EXTRA_BODY") or "{}"
        try:
            extra_body = json.loads(extra)
        except ValueError:
            raise LLMError("LLM_EXTRA_BODY is not valid JSON") from None
        if not isinstance(extra_body, dict):
            raise LLMError("LLM_EXTRA_BODY must be a JSON object")
        return cls(
            model=chosen,
            api_key=key,
            base_url=os.environ.get("LLM_BASE_URL") or DEFAULT_BASE_URL,
            extra_body=extra_body,
        )

    def complete(
        self,
        messages: list[dict[str, Any]],
        tools: list[dict[str, Any]] | None = None,
        *,
        temperature: float = 0.0,
        max_tokens: int = 1024,
    ) -> Reply:
        body: dict[str, Any] = {
            **self.extra_body,
            "model": self.model,
            "messages": messages,
            "temperature": temperature,
            "max_tokens": max_tokens,
        }
        if tools:
            body["tools"] = tools
            body["tool_choice"] = "auto"
        headers = {"Authorization": f"Bearer {self.api_key}", "Content-Type": "application/json"}
        url = f"{self.base_url.rstrip('/')}/chat/completions"

        with httpx.Client(timeout=self.timeout, transport=self.transport) as http:
            for attempt in range(self.retries + 1):
                started = time.perf_counter()
                try:
                    response = http.post(url, json=body, headers=headers)
                except httpx.HTTPError as error:
                    if attempt == self.retries:
                        raise LLMError(f"{self.model}: request failed ({type(error).__name__})") from None
                    time.sleep(2**attempt)
                    continue
                seconds = time.perf_counter() - started
                if response.status_code in RETRY_STATUS and attempt < self.retries:
                    time.sleep(2**attempt)
                    continue
                if response.status_code != 200:
                    raise LLMError(f"{self.model}: HTTP {response.status_code}: {_safe(response.text)}")
                data = response.json()
                try:
                    message = data["choices"][0]["message"]
                except (KeyError, IndexError, TypeError):
                    raise LLMError(f"{self.model}: no message in the response") from None
                return Reply(message=message, usage=data.get("usage") or {}, seconds=seconds)
        raise LLMError(f"{self.model}: no response")  # pragma: no cover


def _safe(text: str, limit: int = 300) -> str:
    """Error bodies are shown shortened; they never contain our key, which is only in a header."""
    return " ".join(text.split())[:limit]
