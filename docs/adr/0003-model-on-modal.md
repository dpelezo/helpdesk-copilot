# 0003. An open model on Modal, behind an OpenAI-compatible interface

- Status: accepted
- Date: 2026-11-27

## Context

The proof of concept called Fireworks directly, with the model name and URL in the agent code. The project should run on a self-hosted open model and stay portable.

## Decision

- All model calls go through one client that speaks the OpenAI-compatible API. Base URL, model name and key come from the environment.
- The main model is an open model served with vLLM on Modal.
- Claude Haiku is an optional comparison row in the eval, if credits allow.
- The specific open model is chosen in week 1 by how reliably it calls tools on 10 sample tickets.

## Consequences

- Switching models is a configuration change.
- Scale-to-zero GPUs have cold starts; the live demo needs a warm-up call or cached runs.
- Confirm that self-deployed GPU time is covered by Modal's plan credits before the first deploy.
