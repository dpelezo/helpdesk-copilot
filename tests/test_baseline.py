"""Tests for BM25, the baseline document set and the baseline runner. No network."""

import json

import pytest

from app.evals import baseline as bl
from app.knowledge.corpus import documents, text_of
from app.llm import LLMError, Reply
from app.mock import faults
from app.mock import tickets as ticket_set
from app.mock.company import generate
from app.retrieval.bm25 import BM25, Document, tokenize

TUNING = ticket_set.generate_split("tuning", generate())


# ── BM25 ───────────────────────────────────────────────────────────────


def test_tokenize_lowercases_and_drops_stopwords():
    assert tokenize("Hi IT, the VPN won't connect!") == ["vpn", "won", "t", "connect"]


def test_bm25_ranks_the_matching_document_first_and_skips_non_matches():
    index = BM25([Document("a", "printer paper jam"), Document("b", "vpn certificate expired"),
                  Document("c", "printer offline network cable")])
    hits = index.search("my printer has a paper jam", k=5)
    assert [h.id for h in hits] == ["a", "c"]
    assert hits[0].score > hits[1].score
    assert index.search("nothing relevant here") == []


def test_bm25_rejects_duplicate_ids():
    with pytest.raises(ValueError, match="unique"):
        BM25([Document("a", "x"), Document("a", "y")])


# ── Document set ───────────────────────────────────────────────────────


def test_one_document_per_cause_and_per_handbook_section():
    docs = documents()
    assert len(docs) == len(faults.CAUSES) + 23
    assert "(vpn-certificate-expired)" in text_of("cause:vpn-certificate-expired", docs)
    assert text_of("policy:pw-2", docs).startswith("Passwords and sign-in. Account lockout.")


# ── Parsing ────────────────────────────────────────────────────────────


@pytest.mark.parametrize(("reply", "expected"), [
    ('{"cause": "vpn-client-outdated", "reason": "old"}', "vpn-client-outdated"),
    ('```json\n{"cause": "(mailbox-full)", "reason": "x"}\n```', "mailbox-full"),
    ('{"cause": "cause:printer-offline"}', "printer-offline"),
    ('{"cause": "escalate", "reason": "nothing fits"}', "escalate"),
    ('{"cause": "made-up-cause"}', None),
    ("I think it's the VPN.", None),
    ('{"cause": 3}', None),
    (None, None),
])
def test_parse_cause_accepts_only_known_ids(reply, expected):
    assert bl.parse_cause(reply) == expected


# ── Runner ─────────────────────────────────────────────────────────────


class Fixed:
    model = "fake"

    def __init__(self, answer):
        self.answer = answer
        self.seen = []

    def complete(self, messages, tools=None, max_tokens=None):
        assert tools is None  # the baseline never offers tools
        assert max_tokens == bl.MAX_TOKENS
        self.seen.append(messages)
        cause = self.answer(messages) if callable(self.answer) else self.answer
        return Reply({"role": "assistant", "content": json.dumps({"cause": cause, "reason": "r"})},
                     {"prompt_tokens": 50, "completion_tokens": 10}, seconds=0.2)


def test_a_run_retrieves_documents_and_records_the_prediction():
    t = next(x for x in TUNING if x.answer.outcome == "fix")
    model = Fixed(t.answer.cause)
    run = bl.run_ticket(model, BM25(documents()), t)
    assert run.predicted == t.answer.cause and run.error is None
    assert len(run.retrieved) == bl.TOP_K
    assert "Documents:" in model.seen[0][1]["content"]
    assert run.prompt_tokens == 50 and run.seconds == 0.2


def test_an_invalid_reply_is_an_error_and_no_prediction():
    run = bl.run_ticket(Fixed("not-a-cause"), BM25(documents()), TUNING[0])
    assert run.predicted is None and run.error == "no valid cause id in the reply"


def test_a_model_error_is_recorded():
    class Broken:
        model = "broken"

        def complete(self, messages, tools=None, max_tokens=None):
            raise LLMError("broken: HTTP 503: busy")

    run = bl.run_ticket(Broken(), BM25(documents()), TUNING[0])
    assert run.error == "broken: HTTP 503: busy" and run.predicted is None


def test_evaluate_scores_an_oracle_at_100_and_reports_retrieval():
    answers = {t.message: t.answer.cause for t in TUNING}

    def oracle(messages):
        return next(cause for msg, cause in answers.items() if msg in messages[1]["content"])

    result = bl.evaluate(Fixed(oracle), "tuning")
    assert result["score"]["root_cause_accuracy"] == 100.0
    assert result["split"] == "tuning" and len(result["runs"]) == 20
    assert 0 <= result["retrieval_hit_at_k"] <= 100
    assert "| 100.0% |" in bl.summary(result)


def test_always_escalate_scores_only_the_escalation_tickets():
    result = bl.evaluate(Fixed("escalate"), "tuning")
    assert result["score"]["root_cause_accuracy"] == 10.0  # 2 of 20


def test_a_reply_cut_off_at_the_token_limit_is_named_as_such():
    class Truncated:
        model = "t"

        def complete(self, messages, tools=None, max_tokens=None):
            return Reply({"role": "assistant", "content": ""}, {"completion_tokens": max_tokens},
                         finish_reason="length")

    run = bl.run_ticket(Truncated(), BM25(documents()), TUNING[0])
    assert run.predicted is None and run.error == "reply cut off at the token limit"
