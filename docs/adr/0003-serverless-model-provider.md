# 0003. A serverless open model behind an OpenAI-compatible client

- Status: accepted (amended 2026-11-27: provider shortlist changed from Modal to Telnyx and DeepInfra)
- Date: 2026-11-27

## Context

The proof of concept called Fireworks directly, with the model name, URL and a key fallback in the agent code. The project should use open-weight models and stay portable across providers.

Four providers were compared: Modal, Fireworks, Telnyx and DeepInfra. All four expose an OpenAI-compatible API.

- **Modal** means running vLLM ourselves on GPUs: setup work, cold starts, and idle cost. At eval volumes (thousands of tickets, not millions of tokens a day) pay-per-token serverless is cheaper and simpler.
- **Fireworks** has a large catalog, but retires serverless models in waves and moved to prepaid billing that pauses all calls at a zero balance. A model disappearing mid-project would break the eval's comparability.
- **Telnyx** hosts a short, curated list of strong open models and runs speech, voice AI and telephony on the same platform and bill.
- **DeepInfra** has the widest choice of open models with tool calling, low prices, and BGE-M3 embeddings.

## Decision

- All model calls go through one client that speaks the OpenAI-compatible API. Base URL, model name and key come from the environment. No provider SDK in the agent code.
- Shortlist **Telnyx and DeepInfra**. Issue #8 runs the same 10 sample tickets through both on a model family both host, and measures valid tool calls, correct arguments, latency and cost per ticket.
- The better result wins. If the results are close, Telnyx wins, because voice (v2) then needs no new vendor.
- Voice does not decide the LLM provider on its own: a voice platform can call any OpenAI-compatible LLM.
- Claude Haiku is an optional comparison row in the eval, if credits allow.

## Consequences

- Switching provider or model is a configuration change.
- No GPUs to run, so no cold starts in the live demo.
- The chosen provider and model, with #8's numbers, are added to this ADR when #8 closes.
- Self-hosting (for example vLLM on Modal) stays possible later through the same client.
