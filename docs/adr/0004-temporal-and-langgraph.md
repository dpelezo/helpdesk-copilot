# 0004. Temporal for Workflow A, LangGraph for Workflow B

- Status: accepted
- Date: 2026-11-27

## Context

The proof of concept used Temporal for review and LangGraph for the agent. Using two orchestrators needs a clear reason for each.

## Decision

- **Temporal runs Workflow A.** It holds long waits for reviewers (hours or days), retries failed steps, runs scheduled source refreshes and curator jobs, and keeps a history of every job. LangGraph extraction steps run as Temporal activities.
- **LangGraph runs Workflow B**, with a Postgres checkpointer and an interrupt for the agent's approval. Sessions are short and interactive, so durable orchestration is not needed.
- Langfuse traces LLM calls in both.

## Consequences

- Each orchestrator does the job it is best at, and neither sits in the other's path.
- Temporal only runs offline, so the hosted demo needs no Temporal server.
