"""Unit tests for the model client. Mock transport only; no network."""

import json

import httpx
import pytest

from app import llm

KEY = "sk-test-not-a-real-key-123"


def client(handler, **kw) -> llm.ChatClient:
    kw.setdefault("retries", 2)
    return llm.ChatClient(model="m", api_key=KEY, transport=httpx.MockTransport(handler), **kw)


def ok_body(message=None, usage=None):
    return {"choices": [{"message": message or {"role": "assistant", "content": "hi"}}],
            "usage": usage or {"prompt_tokens": 3, "completion_tokens": 1}}


def test_sends_bearer_key_model_and_tools_to_chat_completions():
    seen = {}

    def handler(request):
        seen["url"] = str(request.url)
        seen["auth"] = request.headers["Authorization"]
        seen["body"] = json.loads(request.content)
        return httpx.Response(200, json=ok_body())

    reply = client(handler).complete([{"role": "user", "content": "x"}], tools=[{"type": "function"}])
    assert seen["url"] == "https://api.telnyx.com/v2/ai/openai/chat/completions"
    assert seen["auth"] == f"Bearer {KEY}"
    assert seen["body"]["model"] == "m" and seen["body"]["tool_choice"] == "auto"
    assert seen["body"]["temperature"] == 0.0
    assert reply.message["content"] == "hi" and reply.usage["prompt_tokens"] == 3


def test_no_tools_means_no_tool_choice():
    def handler(request):
        assert "tools" not in json.loads(request.content)
        return httpx.Response(200, json=ok_body())

    client(handler).complete([{"role": "user", "content": "x"}])


def test_retries_rate_limits_then_succeeds(monkeypatch):
    monkeypatch.setattr(llm.time, "sleep", lambda s: None)
    statuses = iter([429, 503, 200])

    def handler(request):
        code = next(statuses)
        return httpx.Response(code, json=ok_body() if code == 200 else {"error": "busy"})

    assert client(handler).complete([]).message["content"] == "hi"


def test_gives_up_after_retries_and_never_leaks_the_key(monkeypatch):
    monkeypatch.setattr(llm.time, "sleep", lambda s: None)

    def handler(request):
        return httpx.Response(429, text="rate limited")

    with pytest.raises(llm.LLMError) as error:
        client(handler).complete([])
    assert "HTTP 429" in str(error.value) and KEY not in str(error.value)
    assert KEY not in repr(client(handler))


def test_a_client_error_is_not_retried():
    calls = []

    def handler(request):
        calls.append(1)
        return httpx.Response(400, json={"error": "bad model"})

    with pytest.raises(llm.LLMError, match="HTTP 400"):
        client(handler).complete([])
    assert len(calls) == 1


def test_network_errors_are_reported_without_detail(monkeypatch):
    monkeypatch.setattr(llm.time, "sleep", lambda s: None)

    def handler(request):
        raise httpx.ConnectError("boom", request=request)

    with pytest.raises(llm.LLMError, match="ConnectError"):
        client(handler).complete([])


def test_a_response_without_a_message_is_an_error():
    with pytest.raises(llm.LLMError, match="no message"):
        client(lambda r: httpx.Response(200, json={"choices": []})).complete([])


def test_reasoning_is_detected_from_text_or_token_count():
    assert llm.Reply({"reasoning_content": "thinking"}).reasoned
    assert llm.Reply({}, {"completion_tokens_details": {"reasoning_tokens": 12}}).reasoned
    assert not llm.Reply({"content": "x"}, {"completion_tokens": 2}).reasoned


def test_from_env_reads_the_telnyx_key_and_requires_a_model(monkeypatch):
    for name in ("LLM_API_KEY", "TELNYX_API_KEY", "LLM_MODEL", "LLM_BASE_URL"):
        monkeypatch.delenv(name, raising=False)
    with pytest.raises(llm.LLMError, match="TELNYX_API_KEY"):
        llm.ChatClient.from_env()
    monkeypatch.setenv("TELNYX_API_KEY", KEY)
    with pytest.raises(llm.LLMError, match="LLM_MODEL"):
        llm.ChatClient.from_env()
    made = llm.ChatClient.from_env("zai-org/GLM-5.3-Flash")
    assert made.model == "zai-org/GLM-5.3-Flash" and made.base_url == llm.DEFAULT_BASE_URL


def test_extra_body_is_merged_but_cannot_override_the_model():
    seen = {}

    def handler(request):
        seen.update(json.loads(request.content))
        return httpx.Response(200, json=ok_body())

    client(handler, extra_body={"chat_template_kwargs": {"enable_thinking": False}, "model": "other"}).complete([])
    assert seen["chat_template_kwargs"] == {"enable_thinking": False}
    assert seen["model"] == "m"


def test_from_env_parses_and_validates_extra_body(monkeypatch):
    monkeypatch.setenv("TELNYX_API_KEY", KEY)
    monkeypatch.setenv("LLM_EXTRA_BODY", '{"chat_template_kwargs": {"enable_thinking": false}}')
    assert llm.ChatClient.from_env("m").extra_body == {"chat_template_kwargs": {"enable_thinking": False}}
    monkeypatch.setenv("LLM_EXTRA_BODY", "not json")
    with pytest.raises(llm.LLMError, match="not valid JSON"):
        llm.ChatClient.from_env("m")
    monkeypatch.setenv("LLM_EXTRA_BODY", "[1]")
    with pytest.raises(llm.LLMError, match="JSON object"):
        llm.ChatClient.from_env("m")


def test_finish_reason_marks_a_truncated_reply():
    body = {"choices": [{"message": {"content": ""}, "finish_reason": "length"}], "usage": {}}
    reply = client(lambda r: httpx.Response(200, json=body)).complete([])
    assert reply.finish_reason == "length" and reply.truncated
    assert not client(lambda r: httpx.Response(200, json=ok_body())).complete([]).truncated


def test_the_client_counts_every_token():
    c = client(lambda r: httpx.Response(200, json=ok_body(usage={"prompt_tokens": 30, "completion_tokens": 70})))
    c.complete([])
    c.complete([])
    assert (c.calls, c.prompt_tokens, c.completion_tokens, c.used) == (2, 60, 140, 200)
    assert c.usage_report() == {"calls": 2, "prompt_tokens": 60, "completion_tokens": 140, "budget": None}


def test_a_spent_budget_refuses_further_calls_without_sending_them():
    sent = []

    def handler(request):
        sent.append(1)
        return httpx.Response(200, json=ok_body(usage={"prompt_tokens": 60, "completion_tokens": 60}))

    c = client(handler, budget=100)
    c.complete([])  # 120 used: the call in flight may overshoot
    assert c.exhausted
    with pytest.raises(llm.BudgetExceeded, match="token budget of 100 spent"):
        c.complete([])
    assert len(sent) == 1
    assert isinstance(llm.BudgetExceeded("x"), llm.LLMError)


def test_from_env_reads_the_budget(monkeypatch):
    monkeypatch.setenv("TELNYX_API_KEY", KEY)
    monkeypatch.delenv("LLM_EXTRA_BODY", raising=False)
    monkeypatch.setenv("LLM_TOKEN_BUDGET", "500000")
    assert llm.ChatClient.from_env("m").budget == 500_000
    monkeypatch.setenv("LLM_TOKEN_BUDGET", "lots")
    with pytest.raises(llm.LLMError, match="whole number"):
        llm.ChatClient.from_env("m")
    monkeypatch.delenv("LLM_TOKEN_BUDGET")
    assert llm.ChatClient.from_env("m").budget is None
