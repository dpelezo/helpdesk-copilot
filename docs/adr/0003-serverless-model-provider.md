# 0003. Telnyx Inference behind an OpenAI-compatible client

- Status: accepted (amended 2026-11-27: Telnyx chosen; the model is picked by #8)
- Date: 2026-11-27

## Context

The proof of concept called Fireworks directly, with the model name, URL and a key fallback in the agent code. The project should use open-weight models and stay portable across providers.

Four providers were compared: Modal, Fireworks, Telnyx and DeepInfra. All four expose an OpenAI-compatible API.

- **Modal** means running vLLM ourselves on GPUs: setup work, cold starts and idle cost. At eval volumes, pay-per-token serverless is cheaper and simpler.
- **Fireworks** retires serverless models in waves and pauses all calls at a zero prepaid balance. A model disappearing mid-project would break the eval's comparability.
- **DeepInfra** has the widest choice and low prices, but no telephony.
- **Telnyx** hosts a curated set of open models and runs speech, telephony and no-code voice assistants on the same platform and bill. Its voice assistants support tool calling, webhooks, handoff and transfer, and can call MCP servers, so a v2 phone assistant can use the same mock systems and checks as the chat copilot.

## Decision

- **Provider: Telnyx Inference.** Chosen by the owner on 2026-11-27, because voice (v2) is planned and lands on the same platform with no new vendor.
- All model calls go through one client that speaks the OpenAI-compatible API. Base URL, model name and key (`TELNYX_API_KEY`) come from the environment. No provider SDK in agent code.
- **Model: picked by #8** from the Telnyx models verified for voice assistants, so chat and voice share one model: `zai-org/GLM-5.3-Flash` (the assistants' default), `moonshotai/Kimi-K2.6` and `zai-org/GLM-5.2`. The test runs with reasoning off, because voice calls always disable it.
- Claude Haiku is an optional comparison row in the eval, if credits allow.

## Consequences

- Switching provider or model stays a configuration change; DeepInfra remains the fallback if a Telnyx model is retired or tool calling falls short.
- No GPUs to run, so no cold starts in the live demo.
- The chosen model and #8's numbers are added to this ADR when #8 closes.
- Telnyx's model page does not state tool-calling support per model, so #8 measures it rather than assuming it.
