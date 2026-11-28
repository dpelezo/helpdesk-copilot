# 0003. Telnyx Inference behind an OpenAI-compatible client

- Status: accepted (amended 2026-11-27: Telnyx chosen; `zai-org/GLM-5.2` picked by #8)
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

## Model choice (#8, 2026-11-27): `zai-org/GLM-5.2`

The three Telnyx models verified for voice assistants ran the 20 tuning tickets (never held-out) through a minimal tool loop: the five read-only mock systems over MCP plus `submit_diagnosis`, temperature 0, at most 8 steps. Harness: `app/evals/model_choice.py`; workflow: `model-choice.yml`. One run per model per round.

**Round 1.** The prompt did not say what counts as a fix.

| Model | Valid tool calls | Right employee | Fix/escalate right | s/ticket | tokens/ticket |
| --- | ---: | ---: | ---: | ---: | ---: |
| `zai-org/GLM-5.3-Flash` | 100% | 100% | 50% | 8.71 | 5,650 |
| `moonshotai/Kimi-K2.6` | 100% | 100% | 60% | 11.13 | 6,029 |
| `zai-org/GLM-5.2` | 100% | 100% | 35% | 4.17 | 5,481 |

Every model ran the check that reveals the planted cause on 17 or 18 of the 18 fix tickets. Most misses were "saw the cause, escalated anyway": a printer that was offline or a fan that was failing was read as "a human must fix this". That was a harness ambiguity, not a model weakness.

**Round 2.** The prompt now defines "fix" as "a check shows the cause, even if a technician or approval is needed" and "escalate" as "every relevant check is healthy". The request also sent `chat_template_kwargs.enable_thinking: false` to turn reasoning off.

| Model | Valid tool calls | Right employee | Submitted | Fix/escalate right | s/ticket | tokens/ticket | Steps |
| --- | ---: | ---: | ---: | ---: | ---: | ---: | ---: |
| `zai-org/GLM-5.3-Flash` | 100% | 100% | 100% | 95% | 5.82 | 4,940 | 3.0 |
| `moonshotai/Kimi-K2.6` | 100% | 100% | 90% | 90% | 7.37 | 5,120 | 3.3 |
| **`zai-org/GLM-5.2`** | 100% | 100% | 95% | 95% | **3.61** | **4,601** | 2.8 |

Remaining misses: GLM-5.2 ran out of steps once; GLM-5.3-Flash did not treat pending OS updates as the cause once; Kimi-K2.6 stopped without submitting twice.

**Decision, by the rule set before the run** (tool-call reliability first, then latency): tool calls tie at 100%, so the faster model wins. **`zai-org/GLM-5.2`** ties for the most correct decisions (95%), is the fastest (1.6× Flash, 2× Kimi) and uses the fewest tokens. It is verified for Telnyx voice assistants, so chat and voice v2 share one model.

**Limits.**
- 20 tickets and one run per model per round. The 5-point gaps are within run-to-run noise.
- Reasoning stayed on in all 60 runs of both rounds. Telnyx ignored `enable_thinking: false` and documents no switch. Voice calls always disable reasoning, so voice behaviour is untested; check it when building v2.
- "Fix/escalate right" is a stand-in until #9 scores root causes against the answer key.

