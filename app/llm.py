"""The one model client (ADR 0003): OpenAI-compatible chat completions over httpx.

Provider, model and key come from the environment, so switching provider is a
configuration change. The key is sent only in the Authorization header and is
never logged, printed or included in an error message.

    LLM_BASE_URL   default https://api.telnyx.com/v2/ai/openai
    LLM_MODEL      required
    LLM_API_KEY    or TELNYX_API_KEY
    LLM_EXTRA_BODY optional JSON merged into each request, e.g. to turn reasoning off
    LLM_TOKEN_BUDGET optional hard limit on prompt + completion tokens for this client
    LLM_EMBED_MODEL  the embeddings model, for ``embed`` (#30)

Every client counts the tokens it uses. With a budget, a call is refused once the
budget is spent (the call in flight can overshoot by at most its own size), so a
run stops paying instead of running up a bill.
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


class BudgetExceeded(LLMError):
    """The client's token budget is spent; no further calls are made."""


@dataclass
class Reply:
    message: dict[str, Any]
    usage: dict[str, Any] = field(default_factory=dict)
    seconds: float = 0.0
    finish_reason: str | None = None

    @property
    def truncated(self) -> bool:
        """True when the reply stopped at the token limit, so its answer may be missing."""
        return self.finish_reason == "length"

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
    budget: int | None = None
    embed_model: str | None = None
    prompt_tokens: int = field(default=0, init=False)
    completion_tokens: int = field(default=0, init=False)
    calls: int = field(default=0, init=False)

    @property
    def used(self) -> int:
        return self.prompt_tokens + self.completion_tokens

    @property
    def exhausted(self) -> bool:
        return self.budget is not None and self.used >= self.budget

    def usage_report(self) -> dict[str, int | None]:
        return {"calls": self.calls, "prompt_tokens": self.prompt_tokens,
                "completion_tokens": self.completion_tokens, "budget": self.budget}

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
        raw_budget = os.environ.get("LLM_TOKEN_BUDGET") or ""
        if raw_budget and not raw_budget.isdigit():
            raise LLMError("LLM_TOKEN_BUDGET must be a whole number of tokens")
        return cls(
            model=chosen,
            api_key=key,
            base_url=os.environ.get("LLM_BASE_URL") or DEFAULT_BASE_URL,
            extra_body=extra_body,
            budget=int(raw_budget) if raw_budget else None,
            embed_model=os.environ.get("LLM_EMBED_MODEL") or None,
        )

    def complete(
        self,
        messages: list[dict[str, Any]],
        tools: list[dict[str, Any]] | None = None,
        *,
        temperature: float = 0.0,
        max_tokens: int = 1024,
    ) -> Reply:
        if self.exhausted:
            raise BudgetExceeded(f"{self.model}: token budget of {self.budget:,} spent ({self.used:,} used)")
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
        data, seconds = self._post("chat/completions", body, self.model)
        try:
            choice = data["choices"][0]
            message = choice["message"]
        except (KeyError, IndexError, TypeError):
            raise LLMError(f"{self.model}: no message in the response") from None
        usage = data.get("usage") or {}
        self.calls += 1
        self.prompt_tokens += int(usage.get("prompt_tokens") or 0)
        self.completion_tokens += int(usage.get("completion_tokens") or 0)
        return Reply(message=message, usage=usage, seconds=seconds, finish_reason=choice.get("finish_reason"))

    def embed(self, texts: list[str], model: str | None = None) -> list[list[float]]:
        """One vector per text, in order, from the OpenAI-compatible embeddings endpoint."""
        chosen = model or self.embed_model
        if not chosen:
            raise LLMError("set LLM_EMBED_MODEL")
        if self.exhausted:
            raise BudgetExceeded(f"{chosen}: token budget of {self.budget:,} spent ({self.used:,} used)")
        if not texts:
            return []
        data, _ = self._post("embeddings", {"model": chosen, "input": texts}, chosen)
        try:
            rows = sorted(data["data"], key=lambda row: row["index"])
            vectors = [[float(x) for x in row["embedding"]] for row in rows]
        except (KeyError, TypeError, ValueError):
            raise LLMError(f"{chosen}: no embeddings in the response") from None
        if len(vectors) != len(texts):
            raise LLMError(f"{chosen}: {len(vectors)} embeddings for {len(texts)} texts")
        self.calls += 1
        self.prompt_tokens += int((data.get("usage") or {}).get("prompt_tokens") or 0)
        return vectors

    def _post(self, path: str, body: dict[str, Any], label: str) -> tuple[dict[str, Any], float]:
        """POST with retries on rate limits and server errors. Errors never include the key."""
        headers = {"Authorization": f"Bearer {self.api_key}", "Content-Type": "application/json"}
        url = f"{self.base_url.rstrip('/')}/{path}"
        with httpx.Client(timeout=self.timeout, transport=self.transport) as http:
            for attempt in range(self.retries + 1):
                started = time.perf_counter()
                try:
                    response = http.post(url, json=body, headers=headers)
                except httpx.HTTPError as error:
                    if attempt == self.retries:
                        raise LLMError(f"{label}: request failed ({type(error).__name__})") from None
                    time.sleep(2**attempt)
                    continue
                seconds = time.perf_counter() - started
                if response.status_code in RETRY_STATUS and attempt < self.retries:
                    time.sleep(2**attempt)
                    continue
                if response.status_code != 200:
                    raise LLMError(f"{label}: HTTP {response.status_code}: {_safe(response.text, self.api_key)}")
                return response.json(), seconds
        raise LLMError(f"{label}: no response")  # pragma: no cover


def _safe(text: str, secret: str = "", limit: int = 300) -> str:
    """An error body, shortened, with the key removed in case a provider echoes it back."""
    if secret:
        text = text.replace(secret, "[redacted]")
    return " ".join(text.split())[:limit]
