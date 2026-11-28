"""Unit tests for the model-choice harness, with a scripted fake model."""

import json
from itertools import count

import anyio
import pytest

from app.evals import model_choice as mc
from app.llm import LLMError, Reply
from app.mock import tickets as ticket_set
from app.mock.company import generate
from app.mock.systems import ACCOUNT_CHANGING_TOOLS

COMPANY = generate()
TUNING = ticket_set.generate_split("tuning", COMPANY)
_ids = count()


def call(name, args):
    raw = args if isinstance(args, str) else json.dumps(args)
    return {"id": f"c{next(_ids)}", "type": "function", "function": {"name": name, "arguments": raw}}


def reply(*calls, content=None, reasoning=None):
    message = {"role": "assistant", "content": content, "tool_calls": list(calls) or None}
    if reasoning:
        message["reasoning_content"] = reasoning
    return Reply(message, {"prompt_tokens": 100, "completion_tokens": 20}, seconds=0.5)


class Scripted:
    def __init__(self, *replies, model="fake"):
        self.model = model
        self.replies = list(replies)
        self.seen_tools: list[list[str]] = []

    def complete(self, messages, tools=None):
        self.seen_tools.append([t["function"]["name"] for t in tools or []])
        return self.replies.pop(0)


def ticket(outcome="fix"):
    return next(t for t in TUNING if t.answer.outcome == outcome)


def email(t):
    return next(e.email for e in COMPANY.employees if e.id == t.employee_id)


def run(model, t):
    return anyio.run(mc.run_ticket, model, t, COMPANY)


def checks_for(t):
    args = {"employee": email(t)}
    return [call(tool, {} if tool in ("service_status", "printer_status") else args) for tool in t.answer.reveals]


def test_a_model_that_checks_and_submits_finds_the_evidence():
    t = ticket()
    model = Scripted(reply(*checks_for(t)),
                     reply(call("submit_diagnosis", {"root_cause": "x", "evidence": "y", "outcome": "fix"})))
    r = run(model, t)
    assert r.submitted and r.outcome_correct and r.evidence_found
    assert all(c.valid for c in r.calls)
    assert all(c.employee_ok in (True, None) for c in r.calls)
    assert r.steps == 2 and r.prompt_tokens == 200 and r.seconds == 1.0


def test_the_account_changing_tools_are_never_offered():
    t = ticket()
    model = Scripted(reply(call("submit_diagnosis", {"root_cause": "x", "evidence": "y", "outcome": "fix"})))
    run(model, t)
    offered = set(model.seen_tools[0])
    assert "submit_diagnosis" in offered and "account_status" in offered
    assert not offered & ACCOUNT_CHANGING_TOOLS


def test_calling_an_actions_tool_counts_as_invalid():
    t = ticket()
    model = Scripted(reply(call("unlock_account", {"employee": email(t)})),
                     reply(call("submit_diagnosis", {"root_cause": "x", "evidence": "y", "outcome": "fix"})))
    r = run(model, t)
    assert r.calls[0].tool == "unlock_account" and not r.calls[0].valid


def test_bad_json_wrong_employee_and_tool_errors_are_counted():
    t = ticket()
    model = Scripted(
        reply(call("account_status", "{not json"),
              call("account_status", {"employee": "E999"}),
              call("device_health", {"employee": "E001" if t.employee_id != "E001" else "E002"})),
        reply(call("submit_diagnosis", {"root_cause": "x", "evidence": "y", "outcome": "fix"})),
    )
    r = run(model, t)
    assert [c.valid for c in r.calls[:3]] == [False, False, True]  # E999 is a tool error
    assert r.calls[2].employee_ok is False
    assert not r.evidence_found or t.answer.reveals == []


def test_stopping_without_submitting_is_not_a_diagnosis():
    r = run(Scripted(reply(content="I think it's the printer.")), ticket())
    assert not r.submitted and not r.outcome_correct


def test_an_incomplete_submission_gets_another_turn():
    t = ticket("escalate")
    model = Scripted(reply(call("submit_diagnosis", {"outcome": "escalate"})),
                     reply(call("submit_diagnosis", {"root_cause": "nothing found", "evidence": "all healthy",
                                                     "outcome": "escalate"})))
    r = run(model, t)
    assert [c.valid for c in r.calls] == [False, True]
    assert r.outcome_correct and r.evidence_found


def test_the_step_budget_stops_a_looping_model():
    t = ticket()
    model = Scripted(*[reply(call("service_status", {})) for _ in range(mc.MAX_STEPS)])
    r = run(model, t)
    assert r.steps == mc.MAX_STEPS and not r.submitted


def test_a_model_error_ends_the_ticket_and_is_recorded():
    class Failing:
        model = "broken"

        def complete(self, messages, tools=None):
            raise LLMError("broken: HTTP 400: unknown model")

    r = run(Failing(), ticket())
    assert r.error == "broken: HTTP 400: unknown model" and not r.submitted


def test_reasoning_is_recorded():
    model = Scripted(reply(call("submit_diagnosis", {"root_cause": "x", "evidence": "y", "outcome": "fix"}),
                           reasoning="let me think"))
    assert run(model, ticket()).reasoned


def test_summary_percentages_and_table():
    good = mc.TicketRun("A", "fix", ["account_status"], [mc.Call("account_status", True, True),
                                                         mc.Call("submit_diagnosis", True)],
                        submitted=True, outcome="fix", steps=2, seconds=2.0, prompt_tokens=90, completion_tokens=10)
    bad = mc.TicketRun("B", "escalate", [], [mc.Call("x", False)], steps=1, seconds=1.0, error="boom")
    s = mc.summarize("m", [good, bad])
    assert (s.valid_tool_calls, s.correct_arguments, s.submitted, s.outcome_correct, s.evidence_found) == (
        66.7, 100.0, 50.0, 50.0, 50.0)
    assert (s.seconds_per_ticket, s.tokens_per_ticket, s.steps_per_ticket, s.errors) == (1.5, 50, 1.5, 1)
    assert "| `m` | 66.7% |" in mc.table([s])


def test_evaluate_writes_one_result_file_per_model(tmp_path):
    def make(name):
        return Scripted(*[reply(call("submit_diagnosis", {"root_cause": "x", "evidence": "y", "outcome": "escalate"}))
                          for _ in TUNING], model=name)

    summaries = anyio.run(lambda: mc.evaluate(["a/one", "b/two"], make, out_dir=tmp_path))
    assert [s.model for s in summaries] == ["a/one", "b/two"]
    saved = json.loads((tmp_path / "a__one.json").read_text())
    assert saved["summary"]["tickets"] == 20 and len(saved["runs"]) == 20


def test_main_refuses_the_held_out_split(capsys):
    with pytest.raises(SystemExit):
        mc.main(["--models", "m", "--split", "heldout"])
