"""Tests for the planted causes and the eval tickets. Eval ground truth: keep these strict."""

from collections import Counter

import pytest
from mcp import Client

from app.mock import faults, tickets
from app.mock.company import generate
from app.mock.systems import ACCOUNT_CHANGING_TOOLS, SERVERS, World

COMPANY = generate()
EMPLOYEE_TOOLS = {
    "identity": ("account_status",),
    "devices": ("device_health", "installed_software"),
    "vpn": ("vpn_certificate_status", "vpn_last_session"),
    "tickets": ("recent_tickets",),
}
GLOBAL_TOOLS = {"status": ("service_status", "printer_status")}


async def snapshot(company, employee_id: str) -> dict[str, object]:
    """Every read-only check's answer for one employee."""
    world = World(company)
    out: dict[str, object] = {}
    for server, tools in {**EMPLOYEE_TOOLS, **GLOBAL_TOOLS}.items():
        async with Client(SERVERS[server](world)) as client:
            for tool in tools:
                args = {"employee": employee_id} if server in EMPLOYEE_TOOLS else {}
                result = await client.call_tool(tool, args)
                assert not result.is_error, result.content
                out[tool] = result.structured_content
    return out


async def changed_checks(cause_id: str, employee_id: str) -> set[str]:
    before = await snapshot(COMPANY, employee_id)
    after = await snapshot(faults.plant(COMPANY, cause_id, employee_id), employee_id)
    return {tool for tool in before if before[tool] != after[tool]}


# ── Causes ─────────────────────────────────────────────────────────────


def test_every_issue_has_two_to_four_causes():
    counts = Counter(c.issue for c in faults.CAUSES)
    assert set(counts) == set(faults.ISSUES)
    assert all(2 <= n <= 4 for n in counts.values()), counts


def test_cause_ids_are_unique_and_reveal_only_read_only_checks():
    assert len(faults.BY_ID) == len(faults.CAUSES)
    known = {t for tools in {**EMPLOYEE_TOOLS, **GLOBAL_TOOLS}.values() for t in tools}
    for cause in faults.CAUSES:
        assert set(cause.reveals) <= known, cause.id
        assert not set(cause.reveals) & ACCOUNT_CHANGING_TOOLS


def test_only_account_changes_need_approval():
    approval = {c.id for c in faults.CAUSES if c.requires_approval}
    assert approval == {
        "vpn-account-locked", "phishing-account-locked", "locked-failed-sign-ins",
        "locked-expired-password", "mfa-locked", "mfa-not-enrolled",
    }


@pytest.mark.parametrize("cause", faults.CAUSES, ids=lambda c: c.id)
async def test_a_planted_cause_changes_exactly_the_checks_it_lists(cause):
    employee = next(e for e in COMPANY.employees if cause.eligible(e))
    assert await changed_checks(cause.id, employee.id) == set(cause.reveals)


async def test_planting_leaves_the_original_company_untouched():
    before = COMPANY.model_dump_json()
    faults.plant(COMPANY, "locked-failed-sign-ins", "E001")
    assert COMPANY.model_dump_json() == before


async def test_an_escalation_ticket_changes_nothing():
    assert await changed_checks(faults.ESCALATE, "E001") == set()


# ── Tickets ────────────────────────────────────────────────────────────


@pytest.fixture(scope="module")
def generated():
    return {s: tickets.generate_split(s, COMPANY) for s in ("tuning", "heldout")}


@pytest.mark.parametrize("split", ["tuning", "heldout"])
def test_the_committed_tickets_match_the_generator(split, generated):
    """Regenerate with `make tickets`, in its own PR, if this fails after a deliberate change."""
    assert (tickets.OUT_DIR / f"{split}.jsonl").read_text() == tickets.render(generated[split])


def test_split_sizes(generated):
    assert len(generated["tuning"]) == 20 and len(generated["heldout"]) == 100
    assert Counter(t.answer.outcome for t in generated["heldout"]) == {"fix": 92, "escalate": 8}
    assert Counter(t.answer.outcome for t in generated["tuning"]) == {"fix": 18, "escalate": 2}


def test_splits_share_no_issue_and_held_out_covers_ten(generated):
    tuning = {t.issue for t in generated["tuning"]}
    heldout = {t.issue for t in generated["heldout"]}
    assert tuning == set(tickets.TUNING_ISSUES)
    assert heldout == set(tickets.HELDOUT_ISSUES) and len(heldout) == 10
    assert not tuning & heldout


def test_held_out_uses_every_one_of_its_causes_and_both_approval_issues(generated):
    used = {t.answer.cause for t in generated["heldout"]}
    wanted = {c.id for c in faults.CAUSES if c.issue in tickets.HELDOUT_ISSUES}
    assert wanted <= used
    approval = [t for t in generated["heldout"] if t.answer.requires_approval]
    assert {t.issue for t in approval} >= {"identity--account-locked", "identity--mfa-lockout"}


def test_every_ticket_is_consistent_with_its_cause(generated):
    employees = {e.id: e for e in COMPANY.employees}
    for t in [*generated["tuning"], *generated["heldout"]]:
        if t.answer.outcome == "escalate":
            assert t.answer.cause == faults.ESCALATE and t.answer.reveals == [] and t.answer.fix is None
            continue
        cause = faults.BY_ID[t.answer.cause]
        assert cause.issue == t.issue
        assert cause.eligible(employees[t.employee_id]), t.id
        assert t.answer.reveals == list(cause.reveals)
        assert t.answer.requires_approval == cause.requires_approval


def test_messages_describe_symptoms_and_never_name_the_cause(generated):
    leaks = ("certificate", "quota", "outage", "throttl", "compliance", "firmware", "mfa_locked",
             "failed_logins", "expired")
    for t in [*generated["tuning"], *generated["heldout"]]:
        text = (t.subject + " " + t.message).lower()
        assert not [w for w in leaks if w in text], (t.id, text)
        assert t.message.startswith("Hi IT,")


def test_generation_is_deterministic():
    assert tickets.generate_split("heldout") == tickets.generate_split("heldout")


def test_world_for_plants_the_tickets_cause(generated):
    t = next(x for x in generated["heldout"] if x.answer.cause == "mfa-locked")
    account = next(a for a in tickets.world_for(t, COMPANY).accounts if a.employee_id == t.employee_id)
    assert account.mfa_locked


def test_load_reads_back_what_was_written(tmp_path):
    counts = tickets.write(tmp_path)
    assert counts == {"tuning": 20, "heldout": 100}
    assert tickets.load("heldout", tmp_path) == tickets.generate_split("heldout")
