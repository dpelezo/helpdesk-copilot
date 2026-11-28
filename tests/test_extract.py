"""Tests for extraction. Scripted model replies and a temporary text cache; no network."""

import json

from app.knowledge import extract as ex
from app.knowledge.sources import Source
from app.llm import LLMError, Reply

TEXT = ("If your Microsoft cloud storage is full, you cannot send or receive email. "
        "Open the Outbox and check for attachments larger than 20 MB. Delete old mail to free space.")
OUTLOOK = "microsoft-office--outlook-not-sending-emails"


def source(id_="ms-outlook", relevant=(OUTLOOK,), status="ok", origin="curated"):
    return Source(id=id_, url=f"https://support.microsoft.com/{id_}", issues=list(relevant), fetched="2026-11-28",
                  status=status, title="Outlook help", relevant_to=list(relevant), origin=origin)


def reply_json(payload, **kw):
    return Reply({"role": "assistant", "content": json.dumps(payload)}, {"prompt_tokens": 900, "completion_tokens": 300},
                 seconds=4.0, **kw)


GOOD = {
    "symptoms": [{"text": "Mail stays in the Outbox.", "evidence": "you cannot send or receive email"}],
    "causes": [
        {"text": "The mailbox storage is full.", "evidence": "If your Microsoft cloud storage is full"},
        {"text": "A very large attachment.", "evidence": "attachments larger than 20 MB"},
        {"text": "Invented cause.", "evidence": "the server exploded"},
    ],
    "diagnostics": [{"text": "Check the Outbox for big attachments.",
                     "evidence": "Open the Outbox and check for attachments larger than 20 MB"}],
    "solutions": [{"text": "Delete old mail.", "evidence": "Delete old mail to free space."}],
}


class Scripted:
    model = "fake"

    def __init__(self, *replies):
        self.replies = list(replies)
        self.seen = []

    def complete(self, messages, tools=None, *, max_tokens=None):
        assert tools is None and max_tokens == ex.MAX_TOKENS
        self.seen.append(messages)
        r = self.replies.pop(0)
        if isinstance(r, Exception):
            raise r
        return r


def test_parse_accepts_fenced_json_and_rejects_garbage():
    assert ex.parse("```json\n" + json.dumps(GOOD) + "\n```").causes[0].text == "The mailbox storage is full."
    assert ex.parse("no json here") is None
    assert ex.parse('{"causes": [{"text": "", "evidence": "x"}]}') is None
    assert ex.parse('{"causes": "not a list"}') is None


def test_extract_one_keeps_items_with_real_evidence_and_rejects_the_rest():
    model = Scripted(reply_json(GOOD))
    items, call = ex.extract_one(model, OUTLOOK, source(), TEXT)
    assert [i.kind for i in items] == ["symptom", "cause", "cause", "cause", "diagnostic", "solution"]
    rejected = [i for i in items if not i.evidence_ok]
    assert [i.text for i in rejected] == ["Invented cause."]
    assert (call.items, call.rejected, call.error) == (5, 1, None)
    assert call.prompt_tokens == 900 and call.seconds == 4.0
    prompt = model.seen[0][1]["content"]
    assert "Issue: Outlook not sending emails" in prompt and TEXT in prompt


def test_long_sources_are_cut_to_the_limit(monkeypatch):
    monkeypatch.setattr(ex, "MAX_SOURCE_CHARS", 50)
    model = Scripted(reply_json({}))
    ex.extract_one(model, OUTLOOK, source(), TEXT)
    assert TEXT[:50] in model.seen[0][1]["content"] and TEXT[:51] not in model.seen[0][1]["content"]


def test_failures_are_recorded_not_raised():
    model = Scripted(Reply({"content": "sorry"}), Reply({"content": ""}, finish_reason="length"),
                     LLMError("fake: HTTP 503: busy"))
    errors = [ex.extract_one(model, OUTLOOK, source(), TEXT)[1].error for _ in range(3)]
    assert errors == ["no valid JSON in the reply", "reply cut off at the token limit", "fake: HTTP 503: busy"]


def test_extract_all_reads_the_cache_and_skips_failed_sources(tmp_path):
    (tmp_path / "ms-outlook.txt").write_text(TEXT)
    sources = [source(), source("hp-home", relevant=(), status="failed: only 0 characters")]
    run = ex.extract_all(Scripted(reply_json(GOOD)), sources, cache=tmp_path, today="2026-11-28")
    assert len(run.calls) == 1 and run.date == "2026-11-28"
    assert len(run.items) == 6 and len(run.kept) == 5


def test_the_handbook_is_read_from_the_repo(tmp_path):
    local = source("handbook-it-policy", relevant=("identity--account-locked",), origin="local")
    assert "Account lockout" in ex.source_text(local, tmp_path)


def test_causes_for_scoring_include_rejected_ones(tmp_path):
    (tmp_path / "ms-outlook.txt").write_text(TEXT)
    run = ex.extract_all(Scripted(reply_json(GOOD)), [source()], cache=tmp_path)
    causes = ex.causes_for_scoring(run)
    assert [c.text for c in causes] == ["The mailbox storage is full.", "A very large attachment.", "Invented cause."]
    assert {c.source_id for c in causes} == {"ms-outlook"}


def test_summary_reports_counts_recall_and_misses(tmp_path):
    (tmp_path / "ms-outlook.txt").write_text(TEXT)
    run = ex.extract_all(Scripted(reply_json(GOOD)), [source()], cache=tmp_path, today="2026-11-28")
    score = {"recall": 33.3, "extra_causes": 2, "evidence_valid": 66.7, "ambiguous": 0,
             "per_issue_recall": {OUTLOOK: 33.3}, "per_issue_extracted": {OUTLOOK: 3},
             "missed": ["email-service-outage", "outlook-password-expired"]}
    text = ex.summary(run, score)
    assert "1 calls (0 from cache), 6 items extracted, 5 kept (1 rejected" in text
    assert "| 33.3% | 2 | 66.7% | 0 |" in text and "`outlook-password-expired`" in text


# ── Cost controls ──────────────────────────────────────────────────────


def test_a_good_reply_is_cached_and_reused_without_a_call(tmp_path):
    cache = tmp_path / "replies"
    first = Scripted(reply_json(GOOD))
    items, call = ex.extract_one(first, OUTLOOK, source(), TEXT, cache)
    assert not call.cached and len(list(cache.iterdir())) == 1
    second = Scripted()  # no replies scripted: any call would fail
    again, call2 = ex.extract_one(second, OUTLOOK, source(), TEXT, cache)
    assert call2.cached and second.seen == [] and again == items


def test_the_cache_key_changes_with_the_source_text_or_the_model(tmp_path):
    cache = tmp_path / "replies"
    ex.extract_one(Scripted(reply_json(GOOD)), OUTLOOK, source(), TEXT, cache)
    changed = Scripted(reply_json(GOOD))
    ex.extract_one(changed, OUTLOOK, source(), TEXT + " New paragraph.", cache)
    assert len(changed.seen) == 1
    other = Scripted(reply_json(GOOD))
    other.model = "another-model"
    ex.extract_one(other, OUTLOOK, source(), TEXT, cache)
    assert len(other.seen) == 1 and len(list(cache.iterdir())) == 3


def test_failed_replies_are_not_cached_so_they_are_retried(tmp_path):
    cache = tmp_path / "replies"
    ex.extract_one(Scripted(Reply({"content": ""}, finish_reason="length")), OUTLOOK, source(), TEXT, cache)
    assert not cache.exists() or not list(cache.iterdir())


def test_a_budget_stop_is_marked_on_the_call():
    from app.llm import BudgetExceeded

    _, call = ex.extract_one(Scripted(BudgetExceeded("m: token budget of 10 spent")), OUTLOOK, source(), TEXT)
    assert call.stopped_by_budget and call.error.startswith("m: token budget")
    _, other = ex.extract_one(Scripted(LLMError("m: HTTP 503")), OUTLOOK, source(), TEXT)
    assert not other.stopped_by_budget
