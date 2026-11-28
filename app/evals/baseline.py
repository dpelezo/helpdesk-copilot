"""Condition A, the baseline (issue #9): keyword search and one model call, no checks.

For each ticket, BM25 retrieves the top documents (possible causes and handbook
sections) and the model names the single most likely cause id, or ``escalate``.
It cannot look at any system, so it can only reason from the employee's words and
the retrieved text. Later conditions add the graph and live checks; this number is
what they have to beat.

    python -m app.evals.baseline --split heldout
"""

from __future__ import annotations

import argparse
import json
import re
import statistics
import sys
from dataclasses import asdict, dataclass
from datetime import date
from pathlib import Path
from typing import Any, Literal, Protocol

from app.evals.score import Prediction, chance_accuracy, score
from app.knowledge.corpus import CAUSE_PREFIX, documents
from app.llm import ChatClient, LLMError, Reply
from app.mock import tickets as ticket_set
from app.mock.company import generate
from app.mock.faults import BY_ID, ESCALATE
from app.retrieval.bm25 import BM25

TOP_K = 5
OUT_DIR = Path("evals/results")

SYSTEM = (
    "You are an IT helpdesk assistant for Kestrel Ridge Logistics. You cannot run any checks "
    "on company systems. Using only the employee's message and the documents provided, pick the "
    "single most likely root cause. Each possible cause has an id in parentheses, such as "
    "(vpn-certificate-expired). Reply with JSON only, no other text: "
    '{"cause": "<a cause id from the documents, or escalate>", "reason": "<one sentence>"}. '
    'Use "escalate" only when no document describes a cause that fits.'
)

_JSON = re.compile(r"\{.*\}", re.DOTALL)


class Model(Protocol):
    model: str

    def complete(self, messages: list[dict[str, Any]], tools: list[dict[str, Any]] | None = None) -> Reply: ...


def parse_cause(text: str | None) -> str | None:
    """The cause id in a reply, or None when the reply has no valid one."""
    if not text:
        return None
    match = _JSON.search(text)
    if not match:
        return None
    try:
        cause = json.loads(match.group(0)).get("cause")
    except (ValueError, AttributeError):
        return None
    if not isinstance(cause, str):
        return None
    cause = cause.strip().removeprefix(CAUSE_PREFIX).strip("()")
    return cause if cause == ESCALATE or cause in BY_ID else None


@dataclass
class Run:
    ticket: str
    expected: str
    predicted: str | None
    retrieved: list[str]
    retrieval_hit: bool | None  # was the right cause among the documents? None for escalate tickets
    seconds: float = 0.0
    prompt_tokens: int = 0
    completion_tokens: int = 0
    error: str | None = None


def run_ticket(model: Model, index: BM25, ticket: ticket_set.Ticket) -> Run:
    query = f"{ticket.subject}\n{ticket.message}"
    hits = index.search(query, TOP_K)
    texts = {d.id: d.text for d in index.documents}
    docs = "\n".join(f"- {texts[h.id]}" for h in hits) or "(no documents matched)"
    expected = ticket.answer.cause
    run = Run(
        ticket.id, expected, None, [h.id for h in hits],
        None if expected == ESCALATE else f"{CAUSE_PREFIX}{expected}" in {h.id for h in hits},
    )
    messages = [
        {"role": "system", "content": SYSTEM},
        {"role": "user", "content": f"Subject: {ticket.subject}\n\n{ticket.message}\n\nDocuments:\n{docs}"},
    ]
    try:
        reply = model.complete(messages)
    except LLMError as error:
        run.error = str(error)
        return run
    run.seconds = reply.seconds
    run.prompt_tokens = int(reply.usage.get("prompt_tokens") or 0)
    run.completion_tokens = int(reply.usage.get("completion_tokens") or 0)
    run.predicted = parse_cause(reply.message.get("content"))
    if run.predicted is None:
        run.error = "no valid cause id in the reply"
    return run


def evaluate(model: Model, split: Literal["tuning", "heldout"]) -> dict[str, Any]:
    selected = ticket_set.generate_split(split, generate())
    index = BM25(documents())
    runs = [run_ticket(model, index, t) for t in selected]
    result = score(selected, [Prediction(r.ticket, r.predicted, r.error) for r in runs])
    fix_runs = [r for r in runs if r.retrieval_hit is not None]
    return {
        "condition": "A: BM25 keyword search, no graph, no checks",
        "model": model.model,
        "split": split,
        "date": date.today().isoformat(),
        "top_k": TOP_K,
        "score": asdict(result),
        "chance_accuracy": chance_accuracy(selected),
        "retrieval_hit_at_k": round(100 * sum(bool(r.retrieval_hit) for r in fix_runs) / len(fix_runs), 1),
        "seconds_per_ticket": round(statistics.fmean(r.seconds for r in runs), 2),
        "tokens_per_ticket": round(statistics.fmean(r.prompt_tokens + r.completion_tokens for r in runs)),
        "runs": [asdict(r) for r in runs],
    }


def summary(result: dict[str, Any]) -> str:
    s = result["score"]
    lines = [
        f"### {result['condition']} ({result['split']}, {result['model']})",
        "",
        "| Root-cause accuracy | Chance | Right cause retrieved (top 5) | Escalation precision / recall | "
        "Invalid replies | s/ticket | tokens/ticket |",
        "| ---: | ---: | ---: | ---: | ---: | ---: | ---: |",
        f"| {s['root_cause_accuracy']}% | {result['chance_accuracy']}% | {result['retrieval_hit_at_k']}% | "
        f"{s['escalation_precision']}% / {s['escalation_recall']}% | {s['errors']} | "
        f"{result['seconds_per_ticket']} | {result['tokens_per_ticket']:,} |",
        "",
        "| Issue | Accuracy |",
        "| --- | ---: |",
    ]
    lines += [f"| `{issue}` | {acc}% |" for issue, acc in s["per_issue"].items()]
    return "\n".join(lines)


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    parser.add_argument("--split", choices=["tuning", "heldout"], default="heldout")
    parser.add_argument("--out", type=Path, default=OUT_DIR)
    args = parser.parse_args(argv)
    try:
        result = evaluate(ChatClient.from_env(), args.split)
    except LLMError as error:
        print(f"baseline: {error}", file=sys.stderr)
        return 1
    args.out.mkdir(parents=True, exist_ok=True)
    (args.out / f"baseline-bm25-{args.split}.json").write_text(json.dumps(result, indent=2) + "\n")
    print(summary(result))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
