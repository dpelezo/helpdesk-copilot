"""The diagnosis loop (#50): planner, checks through MCP, and every stopping path. No live model."""

import json

import pytest

from app.diagnosis import agent as ag
from app.diagnosis.checks import CheckRefused, CheckRunner
from app.diagnosis.hypotheses import Hypothesis
from app.llm import LLMError, Reply
from app.mock import company as c
from app.mock import faults
from app.mock.faults import CAUSES, ISSUES

LOCKED = "identity--account-locked"


@pytest.fixture(scope="module")
def base():
    return c.generate()


def employee(base):
    return base.employees[0].id


def hypotheses_for(issue):
    """The catalog's causes for an issue, as condition B's retrieval returns them."""
    return [Hypothesis(cause=x.id, issue=ISSUES[issue], summary=x.summary, fix=x.fix, checks=tuple(sorted(x.reveals)),
                       sources=("https://github.com/dpelezo/helpdesk-copilot/blob/main/app/mock/faults.py",))
            for x in CAUSES if x.issue == issue]


def ticket(emp, tid="T-1"):
    return {"id": tid, "subject": "Locked out", "message": "I can't sign in.", "employee_id": emp}


class Scripted:
    model = "fake-chat"

    def __init__(self, *replies):
        self.replies, self.seen = list(replies), []

    def complete(self, messages, tools=None, *, max_tokens=None):
        assert max_tokens == ag.MAX_TOKENS
        self.seen.append(messages)
        r = self.replies.pop(0)
        if isinstance(r, Exception):
            raise r
        if isinstance(r, Reply):
            return r
        return Reply({"content": json.dumps(r)}, {"prompt_tokens": 100, "completion_tokens": 20})


def answer(cause, next_check=None):
    return {"cause": cause, "reason": "account_status shows locked: true", "reply": "Your account is locked.",
            "next_check": next_check}


# ── Pure parts ─────────────────────────────────────────────────────────


def test_the_planner_picks_the_check_linked_to_most_hypotheses():
    hyps = [{"checks": ("account_status",)}, {"checks": ("account_status", "service_status")},
            {"checks": ("service_status", "vpn_last_session")}, {"checks": ("vpn_last_session",)}]
    allowed = {"account_status", "service_status", "vpn_last_session"}
    assert ag.next_check(hyps, [], allowed) == "account_status"  # tie at 2: first listed wins
    assert ag.next_check(hyps, ["account_status"], allowed) == "service_status"
    assert ag.next_check(hyps, ["account_status", "service_status", "vpn_last_session"], allowed) is None
    assert ag.next_check(hyps, [], {"vpn_last_session"}) == "vpn_last_session"


@pytest.mark.parametrize(("text", "cause", "nxt"), [
    ('{"cause": "mfa-locked", "next_check": null}', "mfa-locked", None),
    ('thinking...\n{"cause": "(cause:mfa-locked)", "next_check": "account_status"}', "mfa-locked", "account_status"),
    ('{"cause": "escalate", "next_check": "null"}', "escalate", None),
    ('{"cause": "made-up-cause"}', None, None),
])
def test_replies_are_parsed_and_unknown_causes_dropped(text, cause, nxt):
    data = ag.parse(text)
    assert (data["cause"], data["next_check"]) == (cause, nxt)


def test_garbage_is_no_reply():
    assert ag.parse("no json") is None and ag.parse("[1, 2]") is None and ag.parse(None) is None


def test_the_settings_are_the_ones_fixed_before_any_run():
    assert (ag.CHECK_BUDGET, ag.MAX_DECISIONS) == (4, 2)


# ── Checks through MCP ─────────────────────────────────────────────────


async def test_only_read_only_tools_are_reachable(base):
    tools = await CheckRunner(base).tools()
    assert "account_status" in tools and "device_health" in tools and "service_status" in tools
    assert not {"unlock_account", "reset_mfa"} & set(tools)


async def test_account_changing_and_unknown_tools_are_refused(base):
    runner = CheckRunner(base)
    with pytest.raises(CheckRefused, match="only the policy gate"):
        await runner.run("unlock_account", employee(base))
    with pytest.raises(CheckRefused, match="not a registered read-only check"):
        await runner.run("reboot_device", employee(base))


async def test_a_check_reads_the_planted_fault(base):
    emp = next(e for e in base.employees if faults.BY_ID["locked-failed-sign-ins"].eligible(e))
    planted = faults.plant(base, "locked-failed-sign-ins", emp.id)
    healthy = await CheckRunner(base).run("account_status", emp.id)
    broken = await CheckRunner(planted).run("account_status", emp.id)
    assert healthy.ok and broken.ok and healthy.arguments == {"employee": emp.id}
    assert healthy.result["locked"] is False and broken.result["locked"] is True


async def test_a_check_without_an_employee_parameter_gets_no_arguments(base):
    result = await CheckRunner(base).run("service_status", employee(base))
    assert result.ok and result.arguments == {} and isinstance(result.result, list)


async def test_a_failing_tool_is_a_result_not_a_crash(base):
    result = await CheckRunner(base).run("account_status", "nobody-here")
    assert not result.ok and "error" in result.result


# ── The loop ───────────────────────────────────────────────────────────


async def test_supported_the_linked_checks_run_then_one_decision(base):
    emp = employee(base)
    hyps = hypotheses_for(LOCKED)
    model = Scripted(answer(hyps[0].cause))
    d = await ag.Diagnoser(model).diagnose(ticket(emp), hyps, ["After 5 failed sign-in attempts the account locks."],
                                           CheckRunner(base))
    linked = {t for h in hyps for t in h.checks}
    assert (d.stop, d.cause, d.decisions) == ("supported", hyps[0].cause, 1)
    assert {x["tool"] for x in d.checks} == linked and len(d.checks) <= ag.CHECK_BUDGET
    assert d.fix == hyps[0].fix and d.sources and d.reply and d.prompt_tokens == 100
    prompt = model.seen[0][1]["content"]
    assert hyps[0].line() in prompt and "account_status:" in prompt and "After 5 failed" in prompt


async def test_no_hypotheses_escalates_without_a_model_call(base):
    model = Scripted()
    d = await ag.Diagnoser(model).diagnose(ticket(employee(base)), [], [], CheckRunner(base))
    assert (d.stop, d.cause, d.decisions, d.checks) == ("no_hypotheses", "escalate", 0, [])
    assert model.seen == []


async def test_the_model_can_ask_for_one_more_check(base):
    emp = employee(base)
    hyps = [Hypothesis("locked-failed-sign-ins", "Account locked", "Too many failed sign-ins.", "Unlock.",
                       ("account_status",))]
    model = Scripted(answer("locked-failed-sign-ins", next_check="recent_tickets"), answer("locked-failed-sign-ins"))
    d = await ag.Diagnoser(model).diagnose(ticket(emp), hyps, [], CheckRunner(base))
    assert [x["tool"] for x in d.checks] == ["account_status", "recent_tickets"]
    assert (d.stop, d.decisions) == ("supported", 2)
    assert "You may request one more check from" in model.seen[0][1]["content"]
    assert "No more checks can be run" in model.seen[1][1]["content"]  # the last decision gets no offer


async def test_budget_spent_the_model_must_decide(base):
    emp = employee(base)
    tools = ("account_status", "device_health", "installed_software", "vpn_certificate_status", "vpn_last_session")
    hyps = [Hypothesis("locked-failed-sign-ins", "Account locked", "x.", "y.", tools)]
    model = Scripted(answer("locked-failed-sign-ins", next_check="service_status"))
    d = await ag.Diagnoser(model).diagnose(ticket(emp), hyps, [], CheckRunner(base))
    assert len(d.checks) == ag.CHECK_BUDGET and "No more checks can be run" in model.seen[0][1]["content"]
    assert (d.stop, d.cause, d.decisions) == ("budget", "locked-failed-sign-ins", 1)


async def test_an_account_change_asked_for_as_a_check_never_runs(base):
    emp = next(e for e in base.employees if faults.BY_ID["locked-failed-sign-ins"].eligible(e))
    planted = faults.plant(base, "locked-failed-sign-ins", emp.id)
    runner = CheckRunner(planted)
    hyps = [Hypothesis("locked-failed-sign-ins", "Account locked", "x.", "y.", ("account_status",))]
    model = Scripted(answer("locked-failed-sign-ins", next_check="unlock_account"))
    d = await ag.Diagnoser(model).diagnose(ticket(emp.id), hyps, [], runner)
    assert [x["tool"] for x in d.checks] == ["account_status"]
    assert "unlock_account" not in model.seen[0][1]["content"].split("allowed")[-1]
    still = await runner.run("account_status", emp.id)
    assert still.result["locked"] is True  # nothing unlocked it


async def test_escalation_invalid_replies_and_model_errors_are_recorded(base):
    emp, hyps = employee(base), hypotheses_for(LOCKED)
    escalated = await ag.Diagnoser(Scripted(answer("escalate"))).diagnose(ticket(emp), hyps, [], CheckRunner(base))
    assert (escalated.stop, escalated.cause, escalated.fix) == ("escalated", "escalate", None)
    invalid = await ag.Diagnoser(Scripted(Reply({"content": "hmm"}, finish_reason="length"))).diagnose(
        ticket(emp), hyps, [], CheckRunner(base))
    assert (invalid.stop, invalid.cause, invalid.error) == ("invalid", None, "reply cut off at the token limit")
    failed = await ag.Diagnoser(Scripted(LLMError("m: HTTP 503"))).diagnose(ticket(emp), hyps, [], CheckRunner(base))
    assert (failed.stop, failed.cause, failed.error) == ("error", None, "m: HTTP 503")


async def test_every_step_is_checkpointed(base):
    emp, hyps = employee(base), hypotheses_for(LOCKED)
    diagnoser = ag.Diagnoser(Scripted(answer(hyps[0].cause)))
    await diagnoser.diagnose(ticket(emp, "T-9"), hyps, [], CheckRunner(base))
    state = diagnoser.graph.get_state({"configurable": {"thread_id": "T-9"}})
    assert state.values["outcome"]["stop"] == "supported" and state.values["checks"]
    history = [s for s in diagnoser.graph.get_state_history({"configurable": {"thread_id": "T-9"}})]
    assert len(history) >= 4  # start, plan, check(s), decide
