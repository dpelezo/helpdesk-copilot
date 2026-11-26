# helpdesk-copilot

An assistant for IT helpdesk agents. It walks a troubleshooting procedure step by step, runs one check per step against the company's systems, and recommends a fix with evidence. A human approves any account change before it happens.

> **Status: just started.** This repo began from a proof of concept: a knowledge-base drafting agent with a Temporal approval workflow. The approval workflow, API auth, database layer, Docker setup and CI carry over. The agent is being rewritten for procedure walking.

## What carries over

- Temporal workflow with a human review gate (accept, accept with feedback, regenerate, reject).
- FastAPI with API-key auth, SQLAlchemy and Postgres.
- Docker, Compose and GitHub Actions CI.

## What is being rewritten

- The agent: from drafting a KB article to a supervisor that walks procedures.
- Error handling: failures surface instead of returning a silent zero score.
- Duplicate checks: embeddings are cached instead of recomputed per request.
- LLM output: validated against a schema instead of stripped and parsed by hand.

## Data

No real customer or company data is used. The planned ticket source is a public dataset that is downloaded at build time and not committed to this repo.

## Run locally

```bash
cp .env.example .env   # fill in your own values; never commit .env
docker compose up --build
```

`FIREWORKS_API_KEY`, `POSTGRES_USER` and `POSTGRES_PASSWORD` have no defaults and must be set in `.env`. Deployment (CD) is added back once the copilot is ready to ship.
