"""The flat document set the baseline searches (issue #9).

One document per possible cause (from the cause catalog) and one per handbook
section. It holds the same facts week 2's knowledge graph will hold, as plain
text with no links between them, so the baseline differs from the later
conditions only in retrieval and checks, not in what is known.

Cause documents say what can go wrong for an issue, never which cause is planted
in a ticket.
"""

from __future__ import annotations

from app.knowledge.policy import sections
from app.mock.faults import CAUSES, ISSUES
from app.retrieval.bm25 import Document

CAUSE_PREFIX = "cause:"
POLICY_PREFIX = "policy:"


def documents() -> list[Document]:
    docs = [
        Document(
            f"{CAUSE_PREFIX}{c.id}",
            f"Issue: {ISSUES[c.issue]}. Possible cause ({c.id}): {c.summary} Fix: {c.fix}",
        )
        for c in CAUSES
    ]
    docs += [
        Document(f"{POLICY_PREFIX}{s.id}", f"{s.topic}. {s.title}. {s.text}")
        for s in sections().values()
    ]
    return docs


def text_of(doc_id: str, docs: list[Document]) -> str:
    return next(d.text for d in docs if d.id == doc_id)
