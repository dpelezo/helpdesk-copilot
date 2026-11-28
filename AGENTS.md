# Working rules for AI coding agents in this repo

These rules apply to any AI assistant (Claude Code, Codex, Cursor and others) and to humans. Add a rule here whenever an agent or a person makes a mistake that a rule would have prevented.

## Before changing code

- Read the files you will touch, the tests that cover them, and the ADRs in `docs/adr/`.
- Run the baseline first: lint and tests must pass on `main` before you change anything, so an inherited failure is never blamed on new code.
- For anything expensive to undo (deleting data, changing the knowledge model, changing eval tickets or scoring), write the plan first and wait for approval.

## While changing code

- One change per branch, small commits, [Conventional Commits](https://www.conventionalcommits.org/) messages (`feat:`, `fix:`, `docs:`, `test:`, `chore:`).
- No new dependency, and no dependency upgrade, without asking. Pin every dependency and every GitHub Action (actions by commit SHA, with the version in a comment).
- Never print, log or commit secrets. Refer to environment variables by name only; `.env` is ignored by git and `.env.example` holds names only. Never put a key as a default value in code.
- Model calls go through the single OpenAI-compatible client (ADR 0003). No provider SDK or hard-coded model name in agent code.
- Unit tests never call a live model or the network. Use recorded responses. Live calls belong in the eval suite.
- Keep the eval honest: never change a ticket, an expected answer or a scorer in the same pull request as the code it measures. Held-out tickets are never used for tuning.
- The seed dataset is downloaded at build time and never committed (ADR 0005). Raw vendor pages are never committed; store URLs and evidence spans.
- Workflow boundaries (ADR 0001): Workflow A never calls a tool that changes a system; Workflow B never publishes knowledge. Account-changing tools run only through the policy gate, after approval.
- No real personal or company data anywhere. The mock company is generated.
- Workflows that call a paid model (`baseline`, `model-choice`) run only on demand (`workflow_dispatch`), never on pull requests, so reopening or pushing a PR never spends credit.

## Before saying "done"

- `ruff check app/ tests/` and `pytest` pass locally.
- New behaviour has tests, including the failure paths.
- If you changed what a function returns, search for the tests and callers that read it.
- Summarise what changed, what you verified, and what you did not verify.

## Project facts

- Python 3.13, dependencies pinned in `requirements.txt`.
- Run locally: `cp .env.example .env`, fill in values, `docker compose up --build`.
- Tests need Postgres; CI starts a throwaway container.
- `main` changes only through pull requests with passing checks; merges happen when the owner approves.
- Decisions are recorded in `docs/adr/`. Read them before proposing a change to the design.
