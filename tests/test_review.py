"""Review rules (#28): decisions, checks against the draft, and the approved release."""

from datetime import date

import pytest
from pydantic import ValidationError

from app.knowledge import resolve
from app.knowledge import review as rv
from app.knowledge.model import ReviewStatus
from app.knowledge.sources import Source
from app.mock.faults import ISSUES

VPN = "network--vpn-not-connecting"
MFA = "identity--mfa-lockout"
DAY = date(2026, 11, 30)


def _src(id):
    return Source(id=id, url=f"https://learn.microsoft.com/{id}", issues=[], fetched="2026-11-28", status="ok")


def _item(issue, kind, text, evidence="quoted words"):
    return {"issue": issue, "source_id": "ms-a", "kind": kind, "text": text, "evidence": evidence, "evidence_ok": True}


@pytest.fixture(scope="module")
def draft():
    items = [_item(i, "cause", f"Cause of {i}.") for i in ISSUES] + [
        _item(VPN, "symptom", "The VPN client shows error 812."),
        _item(VPN, "cause", "The VPN certificate expired."),
        _item(VPN, "diagnostic", "Run the Certutil command to check for valid certificates in the user's store."),
        _item(VPN, "solution", "Renew the VPN certificate."),
    ]
    release, report = resolve.resolve({"date": "2026-11-28", "items": items}, [_src("ms-a")], resolve.registered_tools())
    assert report.valid
    return release


def ids(draft, issue, kind):
    return [n.id for n in draft.nodes if n.kind == kind and n.id.startswith(f"{issue}--")]


def decide(issue, action="approve", **kw):
    return rv.Recorded(decision=rv.Decision(issue=issue, action=action, reviewer="dpelezo", **kw), decided_on=DAY)


# ── Decisions on their own ─────────────────────────────────────────────


@pytest.mark.parametrize(("kw", "message"), [
    ({"action": "send_back"}, "send_back needs feedback"),
    ({"action": "edit"}, "edit needs at least one text edit or link"),
    ({"action": "approve", "edits": [{"node_id": "a", "text": "b"}]}, "come with the edit action"),
    ({"action": "reject", "exclude": ["a"]}, "exclude applies to approve and edit"),
])
def test_a_decision_must_fit_its_action(kw, message):
    with pytest.raises(ValidationError, match=message):
        rv.Decision(issue=VPN, reviewer="dpelezo", **kw)


def test_a_tests_for_link_must_say_what_supports_the_cause():
    with pytest.raises(ValidationError, match="needs supports_if"):
        rv.Link(kind="TESTS_FOR", source="a", target="b")
    with pytest.raises(ValidationError, match="TESTS_FOR links only"):
        rv.Link(kind="MITIGATED_BY", source="a", target="b", supports_if="x")


def test_a_decision_needs_a_reviewer():
    with pytest.raises(ValidationError):
        rv.Decision(issue=VPN, action="approve", reviewer="  ")


# ── Checks against the draft ───────────────────────────────────────────


def test_the_index_groups_every_node_under_its_issue(draft):
    idx = rv.index(draft, "sha")
    assert set(idx.issues) == set(ISSUES)
    assert sum(len(v) for v in idx.issues.values()) == len(draft.nodes)
    assert idx.issues[VPN][0] == VPN and len(idx.issues[VPN]) == 6  # issue, 2 causes, symptom, diagnostic, solution


def test_a_decision_for_another_issues_nodes_is_refused(draft):
    idx = rv.index(draft, "sha")
    mfa_cause = ids(draft, MFA, "cause")[0]
    d = rv.Decision(issue=VPN, action="edit", reviewer="r", edits=[{"node_id": mfa_cause, "text": "x"}])
    assert rv.check(d, idx) == [f"edit: {mfa_cause} is not part of {VPN}"]
    assert rv.check(rv.Decision(issue="network--dns", action="approve", reviewer="r"), idx) == [
        "network--dns is not an issue in draft-2026-11-28"]


def test_links_must_join_the_right_kinds(draft):
    idx = rv.index(draft, "sha")
    [diag], [sol] = ids(draft, VPN, "diagnostic"), ids(draft, VPN, "solution")
    cause = ids(draft, VPN, "cause")[0]
    wrong = rv.Decision(issue=VPN, action="edit", reviewer="r",
                        links=[{"kind": "TESTS_FOR", "source": sol, "target": cause, "supports_if": "x"}])
    assert rv.check(wrong, idx) == ["link TESTS_FOR must go diagnostic -> cause, not solution -> cause"]
    right = rv.Decision(issue=VPN, action="edit", reviewer="r",
                        links=[{"kind": "TESTS_FOR", "source": diag, "target": cause, "supports_if": "expired"},
                               {"kind": "MITIGATED_BY", "source": cause, "target": sol}])
    assert rv.check(right, idx) == []


def test_the_issue_itself_cannot_be_excluded_and_excluded_nodes_cannot_be_linked(draft):
    idx = rv.index(draft, "sha")
    [diag] = ids(draft, VPN, "diagnostic")
    cause = ids(draft, VPN, "cause")[0]
    d = rv.Decision(issue=VPN, action="edit", reviewer="r", exclude=[VPN, cause],
                    links=[{"kind": "TESTS_FOR", "source": diag, "target": cause, "supports_if": "x"}])
    problems = rv.check(d, idx)
    assert "exclude: the issue itself cannot be excluded; reject it instead" in problems
    assert any("one end is excluded" in p for p in problems)


# ── The approved release ───────────────────────────────────────────────


def test_only_approved_issues_reach_the_release_with_full_approval_provenance(draft):
    release, record = rv.apply(draft, "sha", {VPN: decide(VPN), MFA: decide(MFA, "reject")}, "release-2026-11-30")
    assert {n.id for n in release.nodes} == set(rv.index(draft, "sha").issues[VPN])
    assert release.unpublishable() == []
    for item in [*release.nodes, *release.edges]:
        for p in item.provenance:
            assert (p.status, p.reviewer, p.reviewed_on, p.release_id) == (
                ReviewStatus.approved, "dpelezo", DAY, "release-2026-11-30")
    assert {e.kind for e in release.edges} == {"HAS_SYMPTOM", "HAS_POSSIBLE_CAUSE"}


def test_an_edit_rewords_text_adds_links_and_excludes_nodes(draft):
    [diag], [sol], [symptom] = ids(draft, VPN, "diagnostic"), ids(draft, VPN, "solution"), ids(draft, VPN, "symptom")
    cert = next(n.id for n in draft.nodes if getattr(n, "description", "") == "The VPN certificate expired.")
    other = next(c for c in ids(draft, VPN, "cause") if c != cert)
    edit = decide(VPN, "edit", exclude=[other], edits=[{"node_id": cert, "text": "The client certificate has expired."},
                                                       {"node_id": VPN, "text": "VPN will not connect"}],
                  links=[{"kind": "TESTS_FOR", "source": diag, "target": cert, "supports_if": "certificate expired"},
                         {"kind": "MITIGATED_BY", "source": cert, "target": sol}])
    release, record = rv.apply(draft, "sha", {VPN: edit}, "release-x")
    by_id = {n.id: n for n in release.nodes}
    assert by_id[cert].description == "The client certificate has expired."
    assert by_id[VPN].title == "VPN will not connect"
    assert other not in by_id and symptom in by_id
    # The excluded cause takes its HAS_POSSIBLE_CAUSE edge with it.
    assert not [e for e in release.edges if e.target == other]
    tests_for = next(e for e in release.edges if e.kind == "TESTS_FOR")
    assert (tests_for.source, tests_for.target, tests_for.supports_if) == (diag, cert, "certificate expired")
    # A reviewer's link cites the documentation of the node it starts from.
    assert tests_for.provenance[0].evidence_span == by_id[diag].provenance[0].evidence_span
    assert release.unpublishable() == []
    [vpn] = [o for o in record.outcomes if o.issue == VPN]
    assert (vpn.edits, vpn.links, vpn.excluded, vpn.nodes) == (2, 2, 1, 5)


def test_rejected_sent_back_and_undecided_issues_leave_nothing_and_are_recorded(draft):
    decisions = {VPN: decide(VPN, "send_back", feedback="Use the Always On VPN article."), MFA: decide(MFA, "reject")}
    release, record = rv.apply(draft, "sha", decisions, "release-x")
    assert release.nodes == [] and release.edges == []
    assert record.sent_back == {VPN: "Use the Always On VPN article."}
    assert record.approved_issues == []
    undecided = [o for o in record.outcomes if o.action is None]
    assert len(undecided) == len(ISSUES) - 2


def test_apply_refuses_decisions_that_do_not_fit_the_draft(draft):
    bad = decide(VPN, "approve", exclude=["network--vpn-not-connecting--cause-00000000"])
    with pytest.raises(ValueError, match="is not part of"):
        rv.apply(draft, "sha", {VPN: bad}, "release-x")


def test_the_draft_itself_is_left_unchanged(draft):
    before = draft.model_dump_json()
    rv.apply(draft, "sha", {VPN: decide(VPN)}, "release-x")
    assert draft.model_dump_json() == before


def test_owner_matches_the_longest_issue_id():
    assert rv.owner("a--b--cause-1", ["a--b", "a"]) == "a--b"
    assert rv.owner("zzz", ["a"]) is None
