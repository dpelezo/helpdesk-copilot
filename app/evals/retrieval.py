"""Retrieval eval (issue #30): is the right cause in the top 5, for BM25, vectors and hybrid?

The same tickets, corpus and query as the baseline (#9): one document per catalog cause and
per handbook section, and the ticket's subject and message as the query. Only the ranking
differs, so the three hit@5 numbers compare directly with the baseline's 82.6%. Escalate
tickets have no right cause and are left out, as in the baseline.

No chat model is called: only the embeddings model, for the documents once and each query.
Settings (``RRF_K``, hybrid depth, the model) are fixed before the run, never tuned here.

    python -m app.evals.retrieval --split heldout
"""

from __future__ import annotations

import argparse
import json
import sys
from collections import defaultdict
from datetime import date
from pathlib import Path
from typing import Any, Literal

from app.knowledge.corpus import CAUSE_PREFIX, documents
from app.llm import ChatClient, LLMError
from app.mock import tickets as ticket_set
from app.mock.company import generate
from app.mock.faults import BY_ID, ESCALATE
from app.retrieval.bm25 import BM25
from app.retrieval.vectors import RRF_K, CachedEmbedder, Hybrid, VectorIndex

TOP_K = 5
OUT_DIR = Path("evals/results")
METHODS = ("bm25", "vector", "hybrid")


def rank_of(doc_id: str, hits) -> int | None:
    return next((i for i, h in enumerate(hits, start=1) if h.id == doc_id), None)


def evaluate(embed: CachedEmbedder, split: Literal["tuning", "heldout"]) -> dict[str, Any]:
    docs = documents()
    keyword = BM25(docs)
    vector = VectorIndex(docs, embed)
    methods = {"bm25": keyword, "vector": vector, "hybrid": Hybrid(keyword, vector)}
    tickets = [t for t in ticket_set.generate_split(split, generate()) if t.answer.cause != ESCALATE]

    rows = []
    for t in tickets:
        query = f"{t.subject}\n{t.message}"
        target = f"{CAUSE_PREFIX}{t.answer.cause}"
        row: dict[str, Any] = {"ticket": t.id, "issue": BY_ID[t.answer.cause].issue, "cause": t.answer.cause}
        for name, index in methods.items():
            hits = index.search(query, TOP_K)
            row[name] = {"rank": rank_of(target, hits), "top": [h.id for h in hits]}
        rows.append(row)

    def hit_rate(name: str, subset: list[dict[str, Any]]) -> float:
        return round(100 * sum(r[name]["rank"] is not None for r in subset) / len(subset), 1)

    def mrr(name: str) -> float:
        return round(sum(1 / r[name]["rank"] for r in rows if r[name]["rank"]) / len(rows), 3)

    by_issue: dict[str, list[dict[str, Any]]] = defaultdict(list)
    for r in rows:
        by_issue[r["issue"]].append(r)
    return {
        "eval": "retrieval: right cause in the top 5",
        "split": split,
        "date": date.today().isoformat(),
        "embedding_model": embed.model,
        "settings": {"top_k": TOP_K, "rrf_k": RRF_K, "hybrid_depth": methods["hybrid"].depth},
        "tickets": len(rows),
        "documents": len(docs),
        "hit_at_5": {m: hit_rate(m, rows) for m in METHODS},
        "mrr": {m: mrr(m) for m in METHODS},
        "per_issue": {issue: {"tickets": len(rs), **{m: hit_rate(m, rs) for m in METHODS}}
                      for issue, rs in sorted(by_issue.items())},
        "embedded": {"texts": embed.embedded, "from_cache": embed.cached},
        "rows": rows,
    }


def summary(result: dict[str, Any]) -> str:
    h, m = result["hit_at_5"], result["mrr"]
    lines = [
        f"### Retrieval ({result['split']}, {result['tickets']} tickets, {result['embedding_model']})",
        "",
        "| Method | Right cause in top 5 | MRR |",
        "| --- | ---: | ---: |",
        *[f"| {name} | {h[name]}% | {m[name]} |" for name in METHODS],
        "",
        "| Issue | Tickets | BM25 | Vector | Hybrid |",
        "| --- | ---: | ---: | ---: | ---: |",
        *[f"| `{issue}` | {v['tickets']} | {v['bm25']}% | {v['vector']}% | {v['hybrid']}% |"
          for issue, v in result["per_issue"].items()],
    ]
    return "\n".join(lines)


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    parser.add_argument("--split", choices=["tuning", "heldout"], default="heldout")
    parser.add_argument("--out", type=Path, default=OUT_DIR)
    args = parser.parse_args(argv)
    try:
        client = ChatClient.from_env()
        if not client.embed_model:
            raise LLMError("set LLM_EMBED_MODEL")
        embed = CachedEmbedder(client, client.embed_model)
        result = evaluate(embed, args.split)
    except LLMError as error:
        print(f"retrieval: {error}", file=sys.stderr)
        return 1
    result["tokens"] = client.usage_report()
    args.out.mkdir(parents=True, exist_ok=True)
    (args.out / f"retrieval-{args.split}.json").write_text(json.dumps(result, indent=2) + "\n")
    print(summary(result))
    print(f"\nTokens: {client.used:,} in {client.calls} calls; "
          f"{result['embedded']['texts']} texts embedded, {result['embedded']['from_cache']} from cache")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
