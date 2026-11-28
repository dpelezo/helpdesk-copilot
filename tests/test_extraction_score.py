"""Tests for the extraction scorer and its judge. Fake judges and models only."""

import pytest

from app.evals import extraction_score as es
from app.llm import LLMError, Reply
from app.mock.faults import BY_ID

SOURCE = {"ms-outlook": "If your Microsoft cloud storage is full, you cannot send or receive email.\n"
                        "Exchange Online may have a service incident.",
          "ms-printer": "The printer shows as Offline in Printers &amp; scanners. Paper is stuck."}


class KeywordJudge:
    """Picks every reference whose listed keywords all appear in the extracted text."""

    KEYS = {
        "mailbox-full": ["storage is full"],
        "email-service-outage": ["service incident"],
        "printer-offline": ["offline"],
        "printer-paper-jam": ["stuck"],
    }

    def __init__(self):
        self.calls = 0

    def match(self, extracted, references):
        self.calls += 1
        picked = []
        for i, reference in enumerate(references):
            cause = next(c for c in BY_ID.values() if c.summary == reference)
            keys = self.KEYS.get(cause.id)
            if keys and all(k in extracted.lower() for k in keys):
                picked.append(i)
        return picked


def item(issue, text, evidence, source):
    return es.ExtractedCause(issue, text, evidence, source)


OUTLOOK = "microsoft-office--outlook-not-sending-emails"
PRINTER = "hardware--printer-not-responding"


# ── Evidence ───────────────────────────────────────────────────────────


@pytest.mark.parametrize(("span", "found"), [
    ("cloud storage is full", True),
    ("CLOUD   storage\nis FULL", True),
    ("you can’t send", False),
    ("cloud storage is nearly full", False),
    ("", False),
])
def test_evidence_must_appear_in_the_source(span, found):
    assert es.evidence_found(span, SOURCE["ms-outlook"]) is found


def test_curly_quotes_match_straight_quotes():
    assert es.evidence_found("it’s full", "Yes, it's full.")


# ── Scoring ────────────────────────────────────────────────────────────


def test_recall_extra_causes_and_evidence():
    extracted = [
        item(OUTLOOK, "Cloud storage is full, so email can't be sent", "cloud storage is full", "ms-outlook"),
        item(OUTLOOK, "A service incident in Exchange Online", "service incident", "ms-outlook"),
        item(OUTLOOK, "Outlook is in Work Offline mode", "Work Offline", "ms-outlook"),  # extra, bad evidence
        item(PRINTER, "The printer is offline", "shows as Offline", "ms-printer"),
    ]
    judge = KeywordJudge()
    s = es.score_extraction(extracted, judge, SOURCE.__getitem__, issues=[OUTLOOK, PRINTER])
    assert s.extracted == 4
    assert s.per_issue_recall == {OUTLOOK: 66.7, PRINTER: 33.3}
    assert s.recall == 50.0  # 3 of 6 catalog causes
    assert set(s.missed) == {"outlook-password-expired", "printer-paper-jam", "printing-service-outage"}
    assert s.extra_causes == 1 and s.evidence_valid == 75.0
    assert s.matched["mailbox-full"] == ["Cloud storage is full, so email can't be sent"]
    assert s.judge_calls == judge.calls == 4  # one call per extracted cause


def test_a_cause_matching_two_catalog_causes_is_counted_as_ambiguous():
    extracted = [item(PRINTER, "Offline because paper is stuck", "Paper is stuck", "ms-printer")]
    s = es.score_extraction(extracted, KeywordJudge(), SOURCE.__getitem__, issues=[PRINTER])
    assert s.ambiguous == 1 and set(s.matched) == {"printer-offline", "printer-paper-jam"}


def test_unknown_issues_are_rejected():
    with pytest.raises(ValueError, match="unknown issues"):
        es.score_extraction([item("gaming--lag", "x", "x", "s")], KeywordJudge(), lambda s: "")


def test_nothing_extracted_scores_zero_recall():
    s = es.score_extraction([], KeywordJudge(), lambda s: "", issues=[OUTLOOK])
    assert s.recall == 0.0 and s.evidence_valid == 0.0 and len(s.missed) == 3


# ── Judge ──────────────────────────────────────────────────────────────


@pytest.mark.parametrize(("text", "count", "picked"), [
    ("2", 3, [1]), ("1, 3", 3, [0, 2]), ("Thinking it over.\nnone", 3, []), ("**2**", 3, [1]),
    ("A matches the second.\n2", 4, [1]), ("4", 3, None), ("maybe 2", 3, None), ("", 3, None), (None, 3, None),
])
def test_parse_choice_reads_the_final_line(text, count, picked):
    assert es.parse_choice(text, count) == picked


class Scripted:
    model = "fake"

    def __init__(self, *answers):
        self.answers = list(answers)
        self.seen = []

    def complete(self, messages, tools=None, *, max_tokens=None):
        assert max_tokens == es.JUDGE_MAX_TOKENS
        self.seen.append(messages)
        answer = self.answers.pop(0)
        if isinstance(answer, Exception):
            raise answer
        return Reply({"role": "assistant", "content": answer})


def test_llm_judge_lists_the_candidates_once_and_counts_unparsed():
    judge = es.LLMJudge(Scripted("2", "none", "it depends", LLMError("HTTP 503")))
    assert judge.match("A", ["x", "y", "z"]) == [1]
    prompt = judge.model.seen[0][1]["content"]
    assert prompt.startswith("A: A") and "1. x\n2. y\n3. z" in prompt
    assert judge.match("A", ["x"]) == []
    assert judge.match("A", ["x"]) == [] and judge.match("A", ["x"]) == []
    assert judge.unparsed == 2


def test_the_judge_cache_answers_repeat_questions_without_a_call(tmp_path):
    first = es.LLMJudge(Scripted("1"), cache=tmp_path)
    assert first.match("A", ["x", "y"]) == [0]
    second = es.LLMJudge(Scripted(), cache=tmp_path)  # no answers scripted: a call would fail
    assert second.match("A", ["x", "y"]) == [0] and second.cached == 1
    third = es.LLMJudge(Scripted("none"), cache=tmp_path)
    assert third.match("A", ["x", "y", "new"]) == [] and third.cached == 0  # different candidates


def test_unparsed_answers_are_not_cached(tmp_path):
    es.LLMJudge(Scripted("hmm"), cache=tmp_path).match("A", ["x"])
    assert not list(tmp_path.iterdir())


# ── Labels and agreement ───────────────────────────────────────────────


def test_the_hand_labels_are_valid_and_balanced():
    labels = es.load_labels()
    assert len(labels) == 24
    assert sum(lab.same for lab in labels) == 14 and sum(not lab.same for lab in labels) == 10
    for lab in labels:
        if not lab.same:
            assert lab.note, "every negative label explains why"


def test_labels_naming_an_unknown_cause_are_rejected(tmp_path):
    path = tmp_path / "l.jsonl"
    path.write_text('{"extracted": "x", "cause": "no-such-cause", "same": true}\n')
    with pytest.raises(ValueError, match="unknown causes"):
        es.load_labels(path)


class Oracle:
    """Answers from the labels; with flip=n, the first n labelled pairs it sees come out wrong."""

    def __init__(self, labels, flip=0):
        by_id = {c.id: c for c in BY_ID.values()}
        self.truth = {(lab.extracted, by_id[lab.cause].summary): lab.same for lab in labels}
        self.flip = flip
        self.unparsed = 0

    def match(self, extracted, references):
        picked = []
        for i, ref in enumerate(references):
            answer = self.truth.get((extracted, ref))
            if answer is None:
                continue
            if self.flip:
                self.flip -= 1
                answer = not answer
            if answer:
                picked.append(i)
        return picked


def test_a_judge_that_agrees_with_the_labels_is_trusted():
    labels = es.load_labels()
    result = es.judge_agreement(Oracle(labels), labels)
    assert result.agreement == 100.0 and result.trusted and not result.false_yes and not result.false_no


def test_a_judge_below_ninety_percent_is_not_trusted():
    labels = es.load_labels()
    result = es.judge_agreement(Oracle(labels, flip=3), labels)  # 3 wrong: 21 of 24 = 87.5%
    assert result.agreement == 87.5 and not result.trusted
    assert len(result.false_yes) + len(result.false_no) == 3


def test_main_reports_agreement_and_fails_when_not_trusted(monkeypatch, capsys):
    labels = es.load_labels()
    monkeypatch.setenv("TELNYX_API_KEY", "k")
    monkeypatch.setenv("LLM_MODEL", "m")
    monkeypatch.setattr(es, "LLMJudge", lambda model: Oracle(labels, flip=5))
    assert es.main([]) == 1
    out = capsys.readouterr().out
    assert "Judge agreement: 79.2% on 24 labels" in out and "NOT TRUSTED" in out
    assert "tokens: 0 in 0 calls" in out
