# 0006. Every release is its own subgraph

- Status: accepted
- Date: 2026-11-28

## Context

Issue #29: publishing must be idempotent, a new release must supersede the old one without overwriting it silently, and a bad release must be rolled back. Diagnosis (Workflow B) reads one release at a time, and every answer must say which knowledge it used.

Two shapes were considered:

- **One shared graph, upserted in place**, with validity dates on nodes and relationships. It stores each fact once, but every read must filter by date, a rollback has to undo upserts, and a fact that two releases word differently needs merge rules.
- **One subgraph per release**, with a `:Release` manifest node and a pointer to the current one. Nothing is ever updated in place.

## Decision

One subgraph per release.

- Nodes carry `release_id` and `uid = <release_id>/<id>`, unique under a constraint. Relationships join nodes of the same release.
- Each publish writes one `:Release` node: content hash, counts, `supersedes`, time and status (`current`, `superseded`, `rolled_back`). Exactly one is current. `(:Release)-[:SUPERSEDES]->(:Release)` records the chain.
- The same release id with the same content is a no-op; with different content it is refused. A correction is a new release.
- A new release must name the current one in `supersedes`, so two people cannot each replace the same release unnoticed.
- Rollback moves the pointer back; nothing is deleted.
- Each node and relationship also stores its full JSON, so reading back gives exactly the `KnowledgeRelease` that was published. The test suite checks this.

## Consequences

- Storage grows with every release. A release is about 400 nodes for 13 issues, so the free tier's node limit allows hundreds of releases; old ones can be archived if that ever matters.
- Retrieval (#30, #31) filters by the current `release_id`, and every diagnosis can cite the release it used.
- Facts are not shared across releases, so "when did this cause first appear" needs a join on `id` across releases rather than one node's history.
