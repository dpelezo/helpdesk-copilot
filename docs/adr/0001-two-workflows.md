# 0001. Two workflows: knowledge and diagnosis

- Status: accepted
- Date: 2026-11-27

## Context

The product is reviewed, traceable troubleshooting knowledge, not the chat window. Building knowledge and using it have different needs: building is slow, batch, and needs human review; diagnosing is interactive and per ticket. Mixing them lets a live session publish facts or a background job change accounts.

## Decision

Split the system into two independent workflows with hard boundaries.

- **Workflow A (knowledge)** ingests sources, extracts typed knowledge, validates it, waits for human review, and publishes a versioned release. It cannot call any tool that changes a system.
- **Workflow B (diagnosis)** reads only published releases, ranks hypotheses, runs checks, and recommends a fix. It cannot publish knowledge.
- A policy gate in code, not the model, decides which registered tools may run. Account changes pause for the agent's approval.
- Feedback (the `flag_gap` tool and verified ticket outcomes) enters Workflow A as drafts and goes through the same review.

## Consequences

- Two human gates: knowledge before publish, actions before they run.
- The live demo hosts only Workflow B; Workflow A runs offline.
- Each workflow is tested and evaluated separately.
