"""Tests for the shared scorer."""

import pytest

from app.evals.score import Prediction, chance_accuracy, score
from app.mock import tickets as ticket_set
from app.mock.company import generate

HELDOUT = ticket_set.generate_split("heldout", generate())


def perfect():
    return [Prediction(t.id, t.answer.cause) for t in HELDOUT]


def test_perfect_predictions_score_100():
    s = score(HELDOUT, perfect())
    assert s.root_cause_accuracy == 100.0 and s.answered == 100.0
    assert set(s.per_issue.values()) == {100.0}
    assert (s.escalation_precision, s.escalation_recall) == (100.0, 100.0)
    assert (s.missed_causes, s.invented_causes, s.errors) == (0, 0, 0)


def test_always_escalating_gets_only_the_escalation_tickets():
    s = score(HELDOUT, [Prediction(t.id, "escalate") for t in HELDOUT])
    assert s.root_cause_accuracy == 8.0
    assert s.escalation_recall == 100.0 and s.escalation_precision == 8.0
    assert s.missed_causes == 92


def test_a_cause_for_an_escalation_ticket_is_invented():
    t = next(x for x in HELDOUT if x.answer.outcome == "escalate")
    preds = [p if p.ticket != t.id else Prediction(t.id, "vpn-gateway-outage") for p in perfect()]
    s = score(HELDOUT, preds)
    assert s.invented_causes == 1 and s.root_cause_accuracy == 99.0
    assert s.escalation_recall == 87.5


def test_the_wrong_cause_of_the_right_issue_is_wrong():
    t = next(x for x in HELDOUT if x.answer.cause == "vpn-certificate-expired")
    preds = [p if p.ticket != t.id else Prediction(t.id, "vpn-client-outdated") for p in perfect()]
    s = score(HELDOUT, preds)
    assert s.root_cause_accuracy == 99.0
    assert s.per_issue["network--vpn-not-connecting"] == 90.0


def test_missing_and_failed_predictions_count_as_wrong_and_unanswered():
    preds = perfect()[2:] + [Prediction(HELDOUT[1].id, None, error="HTTP 500")]
    s = score(HELDOUT, preds)
    assert s.root_cause_accuracy == 98.0 and s.answered == 98.0 and s.errors == 2


def test_predictions_for_unknown_tickets_are_rejected():
    with pytest.raises(ValueError, match="not in the set: XX-1"):
        score(HELDOUT, [Prediction("XX-1", "escalate")])


def test_chance_accuracy_is_the_uniform_guess_over_causes_and_escalate():
    vpn = [t for t in HELDOUT if t.issue == "network--vpn-not-connecting"]
    assert chance_accuracy(vpn) == 20.0  # 4 causes + escalate
    assert 20.0 < chance_accuracy(HELDOUT) < 34.0
    assert chance_accuracy([]) == 0.0
