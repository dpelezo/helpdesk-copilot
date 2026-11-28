"""Score diagnoses against the ticket answer keys. Shared by every eval condition.

A prediction names one cause id, or ``escalate``. It is right when it equals the
ticket's answer: the planted cause for a fix ticket, ``escalate`` for a ticket with
nothing to find. Accuracy is reported overall and per issue, so a weak issue is
visible instead of averaged away (accuracy is per canonical issue, ADR 0005).

This is eval ground truth: change it only in its own pull request (AGENTS.md).
"""

from __future__ import annotations

from collections import defaultdict
from dataclasses import dataclass, field

from app.mock.faults import ESCALATE, causes_for
from app.mock.tickets import Ticket


@dataclass(frozen=True)
class Prediction:
    ticket: str
    cause: str | None  # a cause id, "escalate", or None when the run produced no answer
    error: str | None = None


@dataclass
class Score:
    tickets: int
    root_cause_accuracy: float
    answered: float
    per_issue: dict[str, float] = field(default_factory=dict)
    # Escalation: precision = of the escalations, how many were right; recall = of the
    # tickets that should escalate, how many did.
    escalation_precision: float = 0.0
    escalation_recall: float = 0.0
    # Fix tickets wrongly escalated, and escalate tickets given a made-up cause.
    missed_causes: int = 0
    invented_causes: int = 0
    errors: int = 0


def _pct(part: int, whole: int) -> float:
    return round(100 * part / whole, 1) if whole else 0.0


def score(tickets: list[Ticket], predictions: list[Prediction]) -> Score:
    by_ticket = {p.ticket: p for p in predictions}
    unknown = set(by_ticket) - {t.id for t in tickets}
    if unknown:
        raise ValueError(f"predictions for tickets not in the set: {', '.join(sorted(unknown))}")

    right = 0
    answered = 0
    issue_right: dict[str, int] = defaultdict(int)
    issue_total: dict[str, int] = defaultdict(int)
    escalated = escalated_right = should_escalate = 0
    missed = invented = errors = 0

    for t in tickets:
        p = by_ticket.get(t.id, Prediction(t.id, None, error="missing"))
        expected = t.answer.cause
        issue_total[t.issue] += 1
        if p.error:
            errors += 1
        if p.cause is not None:
            answered += 1
        if p.cause == expected:
            right += 1
            issue_right[t.issue] += 1
        if expected == ESCALATE:
            should_escalate += 1
        if p.cause == ESCALATE:
            escalated += 1
            escalated_right += expected == ESCALATE
            missed += expected != ESCALATE
        elif p.cause is not None and expected == ESCALATE:
            invented += 1

    return Score(
        tickets=len(tickets),
        root_cause_accuracy=_pct(right, len(tickets)),
        answered=_pct(answered, len(tickets)),
        per_issue={i: _pct(issue_right[i], n) for i, n in sorted(issue_total.items())},
        escalation_precision=_pct(escalated_right, escalated),
        escalation_recall=_pct(escalated_right, should_escalate),
        missed_causes=missed,
        invented_causes=invented,
        errors=errors,
    )


def chance_accuracy(tickets: list[Ticket]) -> float:
    """Expected accuracy of guessing uniformly among each issue's causes plus escalate."""
    if not tickets:
        return 0.0
    return round(100 * sum(1 / (len(causes_for(t.issue)) + 1) for t in tickets) / len(tickets), 1)
