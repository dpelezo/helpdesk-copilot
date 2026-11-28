"""Condition B (issue #31): graph retrieval and one model call, still no checks.

What changes from the baseline (condition A) is retrieval, and only retrieval:

1. **Entry linking.** Vector search over the graph's issue and cause nodes finds the
   ``ENTRY_K`` closest to the ticket (vectors, not hybrid: the tuning split of #30 chose them).
2. **Bounded traversal.** The issues those nodes belong to, at most ``MAX_ISSUES``, in rank
   order; for each, every ``HAS_POSSIBLE_CAUSE`` cause with its ``MITIGATED_BY`` fix and the
   checks that ``TESTS_FOR`` it.
3. **Handbook.** The graph has no policy node, so the ``POLICY_K`` closest handbook sections
   are added as flat text, as the baseline had them.

The graph is the cause catalog (``app.knowledge.catalog_graph``): the same facts as the
baseline's documents. The prompt, model, reply format and scorer are the baseline's. Every
setting here was fixed before the first run.

    NEO4J_URI=bolt://localhost:7687 python -m app.evals.graph --split heldout
"""

from __future__ import annotations

import argparse
import json
import statistics
import sys
from dataclasses import asdict, dataclass, field
from datetime import date
from pathlib import Path
from typing import Any, Literal

from app.evals.baseline import MAX_TOKENS, SYSTEM, Model, Run, parse_cause
from app.diagnosis.hypotheses import Hypothesis
from app.evals.score import Prediction, chance_accuracy, score
from app.knowledge import catalog_graph
from app.knowledge.corpus import CAUSE_PREFIX, POLICY_PREFIX, documents
from app.knowledge.publish import GraphStore, PublishError, connect
from app.llm import ChatClient, LLMError
from app.mock import tickets as ticket_set
from app.mock.company import generate
from app.mock.faults import ESCALATE
from app.retrieval.vectors import CachedEmbedder, VectorIndex

ENTRY_K = 5
MAX_ISSUES = 2
POLICY_K = 2
OUT_DIR = Path("evals/results")

_CAUSES = """
MATCH (i:Issue {release_id: $rid, id: $issue})-[:HAS_POSSIBLE_CAUSE]->(c:Cause {release_id: $rid})
OPTIONAL MATCH (c)-[:MITIGATED_BY]->(s:Solution {release_id: $rid})
OPTIONAL MATCH (d:Diagnostic {release_id: $rid})-[:TESTS_FOR]->(c)
OPTIONAL MATCH (s)-[:IMPLEMENTED_BY]->(r:Runbook {release_id: $rid})
RETURN i.text AS issue, c.id AS id, c.text AS summary, c.pos AS pos, collect(DISTINCT s.text) AS fixes,
       collect(DISTINCT d.data) AS checks, c.data AS data, collect(DISTINCT r.data) AS runbooks
ORDER BY pos
"""
_OWNERS = """
MATCH (i:Issue {release_id: $rid})-[:HAS_POSSIBLE_CAUSE]->(c:Cause {release_id: $rid})
RETURN c.id AS cause, i.id AS issue
"""


def issues_for(entry: list[str], owner: dict[str, str], limit: int = MAX_ISSUES) -> list[str]:
    """Each entry node's issue (an issue is its own), first appearance first, at most ``limit``."""
    out: list[str] = []
    for node in entry:
        issue = owner.get(node, node)
        if issue not in out:
            out.append(issue)
    return out[:limit]


def account_change(runbooks: list[str]) -> str | None:
    """The account-changing tool a fix's runbook names, if any."""
    for raw in runbooks:
        for step in json.loads(raw)["steps"]:
            if step.get("changes_account"):
                return step["tool"]
    return None


@dataclass
class Retrieved:
    entry: list[str]  # entry node ids, best first
    issues: list[str]
    shown: list[str]  # document ids shown to the model: cause:<id>, policy:<id>
    text: str
    hypotheses: list[Hypothesis] = field(default_factory=list)
    policy: list[str] = field(default_factory=list)  # handbook section texts shown


class GraphRetriever:
    def __init__(self, store: GraphStore, embed: CachedEmbedder, model: str,
                 release_id: str = catalog_graph.RELEASE_ID) -> None:
        self.store, self.embed, self.model, self.release_id = store, embed, model, release_id
        rows = store.query(_OWNERS, rid=release_id)
        self.owner = {r["cause"]: r["issue"] for r in rows}
        self.policy = VectorIndex([d for d in documents() if d.id.startswith(POLICY_PREFIX)], embed)

    def causes(self, issue: str) -> list[dict[str, Any]]:
        return self.store.query(_CAUSES, rid=self.release_id, issue=issue)

    def retrieve(self, query: str) -> Retrieved:
        entry = [node for node, _ in self.store.similar(query, self.embed, self.model, k=ENTRY_K,
                                                         kinds=("issue", "cause"), release_id=self.release_id)]
        issues = issues_for(entry, self.owner)
        hypotheses = []
        for issue in issues:
            for c in self.causes(issue):
                sources = tuple(dict.fromkeys(p["source_url"] for p in json.loads(c["data"])["provenance"]))
                hypotheses.append(Hypothesis(
                    cause=c["id"], issue=c["issue"], summary=c["summary"], fix=" ".join(c["fixes"]),
                    checks=tuple(sorted(json.loads(d)["tool"] for d in c["checks"])), sources=sources,
                    action=account_change(c["runbooks"])))
        lines = [h.line() for h in hypotheses]
        shown = [f"{CAUSE_PREFIX}{h.cause}" for h in hypotheses]
        policy = []
        for hit in self.policy.search(query, POLICY_K):
            policy.append(next(d.text for d in self.policy.documents if d.id == hit.id))
            lines.append(f"- {policy[-1]}")
            shown.append(hit.id)
        return Retrieved(entry, issues, shown, "\n".join(lines) or "(no documents matched)", hypotheses, policy)


@dataclass
class GraphRun(Run):
    entry: list[str] | None = None
    issues: list[str] | None = None


def run_ticket(model: Model, retriever: GraphRetriever, ticket: ticket_set.Ticket) -> GraphRun:
    got = retriever.retrieve(f"{ticket.subject}\n{ticket.message}")
    expected = ticket.answer.cause
    run = GraphRun(ticket.id, expected, None, got.shown,
                   None if expected == ESCALATE else f"{CAUSE_PREFIX}{expected}" in got.shown,
                   entry=got.entry, issues=got.issues)
    messages = [
        {"role": "system", "content": SYSTEM},
        {"role": "user", "content": f"Subject: {ticket.subject}\n\n{ticket.message}\n\nDocuments:\n{got.text}"},
    ]
    try:
        reply = model.complete(messages, max_tokens=MAX_TOKENS)
    except LLMError as error:
        run.error = str(error)
        return run
    run.seconds = reply.seconds
    run.prompt_tokens = int(reply.usage.get("prompt_tokens") or 0)
    run.completion_tokens = int(reply.usage.get("completion_tokens") or 0)
    run.predicted = parse_cause(reply.message.get("content"))
    if run.predicted is None:
        run.error = "reply cut off at the token limit" if reply.truncated else "no valid cause id in the reply"
    return run


def evaluate(model: Model, retriever: GraphRetriever, split: Literal["tuning", "heldout"]) -> dict[str, Any]:
    selected = ticket_set.generate_split(split, generate())
    runs = [run_ticket(model, retriever, t) for t in selected]
    result = score(selected, [Prediction(r.ticket, r.predicted, r.error) for r in runs])
    fix_runs = [r for r in runs if r.retrieval_hit is not None]
    return {
        "condition": "B: graph retrieval (vector entry linking, bounded traversal), no checks",
        "model": model.model,
        "embedding_model": retriever.model,
        "split": split,
        "date": date.today().isoformat(),
        "settings": {"entry_k": ENTRY_K, "max_issues": MAX_ISSUES, "policy_k": POLICY_K},
        "score": asdict(result),
        "chance_accuracy": chance_accuracy(selected),
        "right_cause_shown": round(100 * sum(bool(r.retrieval_hit) for r in fix_runs) / len(fix_runs), 1),
        "documents_per_ticket": round(statistics.fmean(len(r.retrieved) for r in runs), 1),
        "seconds_per_ticket": round(statistics.fmean(r.seconds for r in runs), 2),
        "tokens_per_ticket": round(statistics.fmean(r.prompt_tokens + r.completion_tokens for r in runs)),
        "runs": [asdict(r) for r in runs],
    }


def summary(result: dict[str, Any]) -> str:
    s = result["score"]
    lines = [
        f"### {result['condition']} ({result['split']}, {result['model']})",
        "",
        "| Root-cause accuracy | Chance | Right cause shown | Documents shown | Escalation precision / recall | "
        "Invalid replies | s/ticket | tokens/ticket |",
        "| ---: | ---: | ---: | ---: | ---: | ---: | ---: | ---: |",
        f"| {s['root_cause_accuracy']}% | {result['chance_accuracy']}% | {result['right_cause_shown']}% | "
        f"{result['documents_per_ticket']} | {s['escalation_precision']}% / {s['escalation_recall']}% | "
        f"{s['errors']} | {result['seconds_per_ticket']} | {result['tokens_per_ticket']:,} |",
        "",
        "| Issue | Accuracy |",
        "| --- | ---: |",
    ]
    lines += [f"| `{issue}` | {acc}% |" for issue, acc in s["per_issue"].items()]
    return "\n".join(lines)


def prepare(store: GraphStore, embed: CachedEmbedder, model: str) -> None:
    """Publish the catalog graph and embed it. Refuses a graph that already holds product releases."""
    current = store.current()
    if current is not None and current.release_id != catalog_graph.RELEASE_ID:
        raise PublishError(f"the graph's current release is {current.release_id}; run condition B on an empty "
                           "graph, never on the product's")
    store.publish(catalog_graph.build())
    store.embed(embed, model, catalog_graph.RELEASE_ID)


def main(argv: list[str] | None = None, driver=None) -> int:
    parser = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    parser.add_argument("--split", choices=["tuning", "heldout"], default="heldout")
    parser.add_argument("--out", type=Path, default=OUT_DIR)
    args = parser.parse_args(argv)
    owned = driver is None
    try:
        client = ChatClient.from_env()
        if not client.embed_model:
            raise LLMError("set LLM_EMBED_MODEL")
        embed = CachedEmbedder(client, client.embed_model)
        driver = driver or connect()
        store = GraphStore(driver)
        prepare(store, embed, client.embed_model)
        result = evaluate(client, GraphRetriever(store, embed, client.embed_model), args.split)
    except (LLMError, PublishError) as error:
        print(f"condition B: {error}", file=sys.stderr)
        return 1
    finally:
        if owned and driver is not None:
            driver.close()
    result["tokens"] = client.usage_report()
    args.out.mkdir(parents=True, exist_ok=True)
    (args.out / f"graph-{args.split}.json").write_text(json.dumps(result, indent=2) + "\n")
    print(summary(result))
    print(f"\nTokens: {client.used:,} in {client.calls} calls (chat and embeddings)")
    if client.exhausted:
        print("INCOMPLETE: the token budget ran out, so some tickets were not scored. Raise LLM_TOKEN_BUDGET.")
        return 1
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
