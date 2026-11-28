"""The policy gate (#51): no account change without the agent's approval, and never twice.

The restart test also runs on Postgres when ``CHECKPOINT_DB_URL`` is set; CI provides one and
sets ``REQUIRE_POSTGRES_TESTS=1``, so there a skip is a failure.
"""

import json
import os

import pytest
from pydantic import ValidationError

from app.diagnosis import agent as ag
from app.diagnosis.checks import CheckRunner
from app.diagnosis.gate import ActionRunner, Approval, Refused
from app.diagnosis.hypotheses import Hypothesis
from app.diagnosis.store import checkpointer
from app.llm import Reply
from app.mock import company as c
from app.mock import faults

LOCKED = Hypothesis("locked-failed-sign-ins", "Account locked", "Too many failed sign-ins.", "Unlock the account.",
                    ("account_status",), ("https://example.org/catalog",), action="unlock_account")
EXPIRED = Hypothesis("locked-expired-password", "Account locked", "The password expired.", "Unlock, then reset.",
                     ("account_status",), action="unlock_account")
OK = {"action": "approve", "approver": "dpelezo", "identity_verified": True}


class Scripted:
    model = "fake-chat"

    def __init__(self, cause="locked-failed-sign-ins"):
        self.cause, self.calls = cause, 0

    def complete(self, messages, tools=None, *, max_tokens=None):
        self.calls += 1
        return Reply({"content": json.dumps({"cause": self.cause, "reason": "locked: true", "reply": "Unlocking.",
                                             "next_check": None})}, {"prompt_tokens": 10, "completion_tokens": 5})


@pytest.fixture(scope="module")
def locked_world():
    base = c.generate()
    emp = next(e for e in base.employees if faults.BY_ID["locked-failed-sign-ins"].eligible(e))
    return faults.plant(base, "locked-failed-sign-ins", emp.id), emp.id


def ticket(emp, tid="T-1"):
    return {"id": tid, "subject": "Locked out", "message": "I can't sign in.", "employee_id": emp}


async def waiting(locked_world, saver=None, tid="T-1"):
    world, emp = locked_world
    d = ag.Diagnoser(Scripted(), checkpointer=saver)
    actions = ActionRunner(world)
    result = await d.diagnose(ticket(emp, tid), [LOCKED, EXPIRED], [], CheckRunner(world), actions=actions)
    return d, result, actions, world, emp


# ── The approval itself ────────────────────────────────────────────────


@pytest.mark.parametrize(("answer", "message"), [
    ({"action": "approve", "approver": "dpelezo"}, "verify the employee's identity"),
    ({"action": "edit", "approver": "dpelezo", "identity_verified": True}, "edit needs the account change"),
    ({**OK, "tool": "reset_mfa"}, "only edit names"),
    ({"action": "reject", "approver": "dpelezo"}, "a rejection needs a reason"),
    ({**OK, "approver": " "}, "approver's name"),
    ({**OK, "action": "edit", "tool": "delete_account"}, "unlock_account"),
])
def test_an_approval_must_be_complete(answer, message):
    with pytest.raises(ValidationError, match=message):
        Approval.model_validate(answer)


async def test_the_action_runner_runs_only_account_changes(locked_world):
    world, emp = locked_world
    with pytest.raises(Refused, match="not an account change"):
        await ActionRunner(world).run("account_status", emp)


def test_only_the_approval_step_leads_to_the_action():
    graph = ag.Diagnoser(Scripted()).graph.get_graph()
    into_act = {e.source for e in graph.edges if e.target == "act"}
    assert into_act == {"approve"}


# ── Through the graph ──────────────────────────────────────────────────


async def test_a_supported_account_change_waits_and_nothing_runs(locked_world):
    d, result, actions, world, emp = await waiting(locked_world)
    assert result.awaiting_approval and result.cause == "locked-failed-sign-ins" and result.stop == "supported"
    p = result.proposal
    assert (p["tool"], p["employee_id"], p["cause"]) == ("unlock_account", emp, "locked-failed-sign-ins")
    assert p["evidence"][0]["tool"] == "account_status" and p["evidence"][0]["result"]["locked"] is True
    assert "pw-4" in p["identity_check"]
    assert actions.calls == [] and actions.world.account(emp).locked is True


async def test_an_approved_change_runs_exactly_once_and_is_recorded(locked_world):
    d, _, actions, world, emp = await waiting(locked_world)
    done = await d.resume("T-1", OK, CheckRunner(world), actions)
    assert actions.calls == [("unlock_account", emp)] and actions.world.account(emp).locked is False
    assert not done.awaiting_approval
    assert done.action["tool"] == "unlock_account" and done.action["result"]["changed"] is True
    assert done.action["approver"] == "dpelezo" and done.approval["identity_verified"] is True and done.approval["at"]
    with pytest.raises(ValueError, match="not waiting for an approval"):
        await d.resume("T-1", OK, CheckRunner(world), actions)
    assert len(actions.calls) == 1


async def test_a_rejected_change_never_runs(locked_world):
    d, _, actions, world, emp = await waiting(locked_world)
    done = await d.resume("T-1", {"action": "reject", "approver": "dpelezo", "reason": "Caller could not verify."},
                          CheckRunner(world), actions)
    assert actions.calls == [] and actions.world.account(emp).locked is True
    assert done.action is None and done.approval["reason"] == "Caller could not verify."


async def test_an_edit_runs_the_chosen_change_instead(locked_world):
    d, _, actions, world, emp = await waiting(locked_world)
    done = await d.resume("T-1", {**OK, "action": "edit", "tool": "reset_mfa"}, CheckRunner(world), actions)
    assert actions.calls == [("reset_mfa", emp)] and done.action["tool"] == "reset_mfa"


async def test_an_invalid_approval_is_refused_before_the_graph_moves(locked_world):
    d, _, actions, world, emp = await waiting(locked_world)
    with pytest.raises(ValidationError):
        await d.resume("T-1", {"action": "approve", "approver": "dpelezo"}, CheckRunner(world), actions)
    still = await d.result({"configurable": {"thread_id": "T-1"}})
    assert still.awaiting_approval and actions.calls == []


async def test_an_invalid_answer_inside_the_graph_is_asked_again(locked_world):
    from langgraph.types import Command

    d, _, actions, world, emp = await waiting(locked_world)
    config = ag.Diagnoser._config("T-1", CheckRunner(world), actions)
    await d.graph.ainvoke(Command(resume={"action": "approve", "approver": "x"}), config)  # bypasses resume()
    again = await d.result(config)
    assert again.awaiting_approval and "verify the employee's identity" in again.proposal["error"]
    assert actions.calls == []


async def test_the_eval_runs_without_the_gate(locked_world):
    world, emp = locked_world
    actions = ActionRunner(world)
    result = await ag.Diagnoser(Scripted(), gate=False).diagnose(ticket(emp), [LOCKED], [], CheckRunner(world),
                                                                  actions=actions)
    assert not result.awaiting_approval and result.cause == "locked-failed-sign-ins" and actions.calls == []


async def test_a_fix_with_no_account_change_ends_without_the_gate(locked_world):
    world, emp = locked_world
    plain = Hypothesis("vpn-client-outdated", "VPN", "Old client.", "Update the client.", ("vpn_last_session",))
    result = await ag.Diagnoser(Scripted("vpn-client-outdated")).diagnose(ticket(emp), [plain], [], CheckRunner(world))
    assert not result.awaiting_approval and result.proposal is None


# ── Restart ────────────────────────────────────────────────────────────


async def restart_case(locked_world, saver_a, saver_b):
    """Diagnose with one graph, then resume with a new one on the same store: as after a restart."""
    first, waiting_result, actions, world, emp = await waiting(locked_world, saver_a, tid="T-restart")
    assert waiting_result.awaiting_approval
    model = Scripted()
    second = ag.Diagnoser(model, checkpointer=saver_b)
    done = await second.resume("T-restart", OK, CheckRunner(world), actions)
    assert model.calls == 0  # the diagnosis is not repeated
    assert actions.calls == [("unlock_account", emp)] and done.action["result"]["changed"] is True
    with pytest.raises(ValueError, match="not waiting"):
        await second.resume("T-restart", OK, CheckRunner(world), actions)
    assert len(actions.calls) == 1


async def test_after_a_restart_the_approval_resumes_and_runs_once(locked_world):
    async with checkpointer(None) as saver:  # in memory: one store shared by two graphs
        await restart_case(locked_world, saver, saver)


async def test_after_a_restart_on_postgres(locked_world):
    url = os.getenv("CHECKPOINT_DB_URL")
    if not url:
        if os.getenv("REQUIRE_POSTGRES_TESTS") == "1":
            pytest.fail("REQUIRE_POSTGRES_TESTS=1 but CHECKPOINT_DB_URL is not set")
        pytest.skip("no Postgres here: set CHECKPOINT_DB_URL to run the checkpoint test")
    import uuid

    tid = f"T-restart-{uuid.uuid4()}"
    world, emp = locked_world
    actions = ActionRunner(world)
    async with checkpointer(url) as saver:
        first = ag.Diagnoser(Scripted(), checkpointer=saver)
        result = await first.diagnose(ticket(emp, tid), [LOCKED], [], CheckRunner(world), actions=actions)
        assert result.awaiting_approval
    async with checkpointer(url) as saver:  # a new connection: nothing kept in memory
        second = ag.Diagnoser(Scripted(), checkpointer=saver)
        done = await second.resume(tid, OK, CheckRunner(world), actions)
        assert actions.calls == [("unlock_account", emp)] and done.action["tool"] == "unlock_account"
        with pytest.raises(ValueError, match="not waiting"):
            await second.resume(tid, OK, CheckRunner(world), actions)
    assert len(actions.calls) == 1
