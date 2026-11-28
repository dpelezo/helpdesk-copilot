"""Unit tests for the knowledge model."""

from datetime import date

import pytest
from pydantic import ValidationError

from app.knowledge import model as m

RELEASE = "r-2026-12-01"


def prov(status=m.ReviewStatus.approved, **overrides):
    values = {
        "source_id": "ms-learn-vpn",
        "source_url": "https://learn.microsoft.com/vpn",
        "evidence_span": "An expired certificate stops the client from connecting.",
        "status": status,
    }
    if status is m.ReviewStatus.approved:
        values |= {"reviewer": "it-lead", "reviewed_on": date(2026, 12, 1), "release_id": RELEASE}
    return m.Provenance(**(values | overrides))


P = [prov()]


def vpn_release(**overrides):
    nodes = [
        m.Issue(id="network--vpn-not-connecting", title="VPN not connecting",
                category="Network", seed_id="network--vpn-not-connecting", provenance=P),
        m.Symptom(id="vpn-auth-error", description="Client shows an authentication error", provenance=P),
        m.Cause(id="vpn-cert-expired", description="Client certificate expired", provenance=P),
        m.Diagnostic(id="check-vpn-cert", description="Read the certificate expiry",
                     tool="vpn_certificate_status", provenance=P),
        m.Solution(id="renew-vpn-cert", description="Issue a new client certificate", provenance=P),
        m.Runbook(id="rb-renew-vpn-cert", title="Renew a VPN certificate", provenance=P, steps=[
            m.RunbookStep(order=1, instruction="Confirm the expiry date"),
            m.RunbookStep(order=2, instruction="Issue the certificate", tool="vpn_issue_certificate"),
        ]),
        m.Incident(id="inc-1042", ticket_id="T-1042", closed_on=date(2026, 12, 2), provenance=P),
    ]
    edges = [
        m.HasSymptom(source="network--vpn-not-connecting", target="vpn-auth-error", provenance=P),
        m.HasPossibleCause(source="network--vpn-not-connecting", target="vpn-cert-expired", provenance=P),
        m.TestsFor(source="check-vpn-cert", target="vpn-cert-expired",
                   supports_if="expires_at is in the past", rules_out_if="expires_at is > 7 days away",
                   provenance=P),
        m.MitigatedBy(source="vpn-cert-expired", target="renew-vpn-cert", provenance=P),
        m.ImplementedBy(source="renew-vpn-cert", target="rb-renew-vpn-cert", provenance=P),
        m.ConfirmedCause(source="inc-1042", target="vpn-cert-expired",
                         verified_by="agent-7", verified_on=date(2026, 12, 2), provenance=P),
    ]
    return m.KnowledgeRelease(**({"release_id": RELEASE, "nodes": nodes, "edges": edges} | overrides))


# ── Provenance ─────────────────────────────────────────────────────────


@pytest.mark.parametrize("span", ["", "   "])
def test_a_fact_with_no_evidence_span_is_rejected(span):
    with pytest.raises(ValidationError, match="evidence_span"):
        prov(evidence_span=span)


def test_a_node_without_provenance_is_rejected():
    with pytest.raises(ValidationError, match="provenance"):
        m.Cause(id="c", description="Something", provenance=[])


@pytest.mark.parametrize("missing", ["reviewer", "reviewed_on", "release_id"])
def test_an_approved_fact_needs_reviewer_date_and_release(missing):
    with pytest.raises(ValidationError, match=missing):
        prov(**{missing: None})


def test_a_draft_fact_needs_no_review_yet():
    assert prov(status=m.ReviewStatus.draft).reviewer is None


def test_provenance_rejects_a_non_http_source():
    with pytest.raises(ValidationError, match="source_url"):
        prov(source_url="file:///etc/passwd")


@pytest.mark.parametrize("bad", ["Has Space", "UPPER", "-leading", "trailing-", "a---b"])
def test_ids_are_slugs(bad):
    with pytest.raises(ValidationError, match="id"):
        m.Cause(id=bad, description="x", provenance=P)


def test_models_are_frozen_and_reject_unknown_fields():
    cause = m.Cause(id="c", description="x", provenance=P)
    with pytest.raises(ValidationError):
        cause.description = "changed"  # type: ignore[misc]
    with pytest.raises(ValidationError, match="extra"):
        m.Cause(id="c", description="x", provenance=P, confidence=0.9)


# ── Possible vs confirmed causes ───────────────────────────────────────


def test_possible_and_confirmed_causes_are_different_types():
    release = vpn_release()
    kinds = {type(e) for e in release.edges if e.target == "vpn-cert-expired"}
    assert {m.HasPossibleCause, m.ConfirmedCause} <= kinds
    assert not issubclass(m.ConfirmedCause, m.HasPossibleCause)


def test_a_confirmed_cause_must_come_from_an_incident_not_an_issue():
    edge = m.ConfirmedCause(source="network--vpn-not-connecting", target="vpn-cert-expired",
                            verified_by="a", verified_on=date(2026, 12, 2), provenance=P)
    with pytest.raises(ValidationError, match="CONFIRMED_CAUSE .* expected incident -> cause"):
        vpn_release(edges=[edge])


# ── Runbooks ───────────────────────────────────────────────────────────


def test_a_runbook_with_an_account_change_requires_approval():
    unlock = m.Runbook(id="rb-unlock", title="Unlock account", provenance=P, steps=[
        m.RunbookStep(order=1, instruction="Unlock", tool="identity_unlock_account", changes_account=True),
    ])
    assert unlock.requires_approval
    assert not vpn_release().nodes[5].requires_approval


def test_an_account_change_must_name_its_tool():
    with pytest.raises(ValidationError, match="must name the registered tool"):
        m.RunbookStep(order=1, instruction="Reset MFA", changes_account=True)


def test_runbook_steps_must_be_in_order():
    with pytest.raises(ValidationError, match="numbered 1..n"):
        m.Runbook(id="rb", title="x", provenance=P, steps=[
            m.RunbookStep(order=2, instruction="b"), m.RunbookStep(order=1, instruction="a"),
        ])


def test_diagnostic_risk_is_none_or_low_only():
    with pytest.raises(ValidationError, match="risk"):
        m.Diagnostic(id="d", description="x", tool="t", risk="high", provenance=P)


# ── Releases ───────────────────────────────────────────────────────────


def test_a_complete_approved_release_is_publishable():
    assert vpn_release().unpublishable() == []


def test_a_release_rejects_edges_to_missing_or_wrong_nodes():
    bad = m.MitigatedBy(source="vpn-cert-expired", target="no-such-solution", provenance=P)
    with pytest.raises(ValidationError, match="found cause -> missing"):
        vpn_release(edges=[bad])


def test_a_release_rejects_duplicate_ids():
    nodes = vpn_release().nodes
    with pytest.raises(ValidationError, match="duplicate node ids: vpn-cert-expired"):
        vpn_release(nodes=[*nodes, m.Cause(id="vpn-cert-expired", description="again", provenance=P)])


def test_drafts_and_other_releases_block_publishing():
    nodes = list(vpn_release().nodes)
    nodes[2] = m.Cause(id="vpn-cert-expired", description="Client certificate expired",
                       provenance=[prov(status=m.ReviewStatus.draft)])
    nodes[3] = m.Diagnostic(id="check-vpn-cert", description="Read the certificate expiry",
                            tool="vpn_certificate_status", provenance=[prov(release_id="r-old")])
    blocked = vpn_release(nodes=nodes).unpublishable()
    assert blocked == ["vpn-cert-expired (ms-learn-vpn: draft)", "check-vpn-cert (ms-learn-vpn: approved)"]


def test_a_release_round_trips_through_json():
    release = vpn_release()
    again = m.KnowledgeRelease.model_validate_json(release.model_dump_json())
    assert again == release
    assert isinstance(again.edges[2], m.TestsFor)
