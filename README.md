# helpdesk-copilot

[![CI](https://github.com/dpelezo/helpdesk-copilot/actions/workflows/ci.yml/badge.svg?branch=main)](https://github.com/dpelezo/helpdesk-copilot/actions/workflows/ci.yml) [![security](https://github.com/dpelezo/helpdesk-copilot/actions/workflows/security.yml/badge.svg?branch=main)](https://github.com/dpelezo/helpdesk-copilot/actions/workflows/security.yml)

An assistant for IT helpdesk agents. It walks a troubleshooting procedure step by step, runs one check per step against the company's systems, and recommends a fix with evidence. A human approves any account change before it happens.

> **Status: week 2 of 4.** Knowledge is extracted, resolved and reviewed; publishing to the graph and the diagnosis agent come next. This repo began from a proof of concept (a KB-drafting agent with a Temporal approval workflow); the POC's agent, routes and database table are retired, and its review gate was rebuilt for knowledge releases.

## How knowledge gets in (Workflow A)

| Step | Command | Output |
| --- | --- | --- |
| Collect sources | `python -m app.knowledge.sources` | `knowledge/sources/manifest.json` (URLs and hashes; no page text) |
| Extract (paid, on demand) | `extraction` workflow | `evals/results/extraction.json` |
| Resolve and validate | `make resolve` | `knowledge/draft/release.json`, [`reports/validation.md`](reports/validation.md) |
| Review (Temporal) | `python -m app.workflows.review_cli start --release-id <id>` | `knowledge/releases/<id>.json` with approval provenance |
| Publish (Neo4j) | `make publish RELEASE=knowledge/releases/<id>.json` | A versioned release in the graph; `python -m app.knowledge.publish history` / `rollback` |

A reviewer approves, edits, rejects or sends back each issue, from the CLI or `/api/v1/reviews` (API-key auth). Only approved facts reach the release.

## Data

**Seed profile:** the 10,500-row seed collapses to 33 canonical issues, and its cause column is empty; see [reports/seed_profile.md](reports/seed_profile.md).

No real customer or company data is used. The planned ticket source is a public dataset that is downloaded at build time and not committed to this repo.

## Results so far

| Condition | Root-cause accuracy (100 held-out tickets) |
| --- | ---: |
| Chance | 26.9% |
| **A. Keyword search, no checks (baseline)** | **29.0%** |
| B. Graph retrieval | week 2 |
| C. Graph + live checks | week 3 |

Search alone puts the right cause in front of the model 82.6% of the time and still barely beats chance: without checking systems it cannot tell which cause is real. Details in [evals/results](evals/results/README.md).

## Mock company and systems

A generated company, Kestrel Ridge Logistics (50 employees, `make company`), backs six MCP servers in `app/mock/systems.py`:

| Server | Tools |
| --- | --- |
| identity | `account_status` |
| devices | `device_health`, `installed_software` |
| vpn | `vpn_certificate_status`, `vpn_last_session` |
| status | `service_status`, `printer_status` |
| tickets | `recent_tickets` |
| actions | `unlock_account`, `reset_mfa` (change an account; approval required) |

```bash
python -m app.mock.systems identity            # stdio
python -m app.mock.systems vpn --http --port 8003
make conformance                               # official MCP conformance suite, all six
```

## Run locally

```bash
cp .env.example .env   # fill in your own values; never commit .env
docker compose up --build
```

`TELNYX_API_KEY`, `API_KEYS`, `NEO4J_PASSWORD`, `POSTGRES_USER` and `POSTGRES_PASSWORD` (Temporal's database) have no defaults and must be set in `.env`. The worker runs the review workflow; the Temporal UI is on port 8080. Deployment (CD) is added back once the copilot is ready to ship.

## Contributing

Working rules for people and AI coding agents are in [AGENTS.md](AGENTS.md).
