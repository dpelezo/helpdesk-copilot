"""Tests for the IT policy handbook and the policy questions (eval ground truth)."""

import re
from collections import Counter

import pytest

from app.knowledge import policy
from app.mock.company import generate
from app.mock.faults import BY_ID

SECTIONS = policy.sections()
QUESTIONS = policy.questions()
COMPANY = generate()


def version(text: str) -> tuple[int, ...]:
    return tuple(int(part) for part in text.split("."))


# ── Handbook ───────────────────────────────────────────────────────────


def test_the_handbook_has_23_sections_in_six_topics():
    assert len(SECTIONS) == 23
    assert Counter(s.topic for s in SECTIONS.values()) == {
        "Passwords and sign-in": 4, "VPN and remote work": 4, "Software": 4,
        "Devices": 4, "Email and phishing": 4, "Printing": 3,
    }
    assert all(s.text for s in SECTIONS.values())


def test_a_repeated_section_id_is_rejected(tmp_path):
    path = tmp_path / "h.md"
    path.write_text("## T\n\n### [pw-1] A\n\nOne.\n\n### [pw-1] B\n\nTwo.\n")
    with pytest.raises(ValueError, match="repeated: pw-1"):
        policy.sections(path)


def test_cross_references_point_at_real_sections():
    for s in SECTIONS.values():
        for ref in re.findall(r"\[([a-z]+-\d+)\]", s.text):
            assert ref in SECTIONS, (s.id, ref)


def test_the_handbook_agrees_with_the_mock_world():
    """The copilot's answers and the mock systems must not contradict each other."""
    assert f"expire every {COMPANY.password_max_age_days} days" in SECTIONS["pw-1"].text
    assert f"After {COMPANY.lockout_threshold} failed sign-in attempts" in SECTIONS["pw-2"].text
    quota = int(COMPANY.accounts[0].mailbox_quota_gb)
    assert f"holds {quota} GB" in SECTIONS["mail-1"].text
    assert all(p.id in SECTIONS["prn-1"].text for p in COMPANY.printers)
    # Every generated client is supported; the planted outdated client is not.
    assert "older than 5.2" in SECTIONS["vpn-3"].text
    assert all(version(v.client_version) >= (5, 2) for v in COMPANY.vpn)
    assert version(BY_ID["vpn-client-outdated"].plant(COMPANY, "E001").vpn[0].client_version) < (5, 2)


def test_approval_rules_match_the_cause_catalog():
    assert "needs the agent's approval" in SECTIONS["pw-3"].text
    assert "agent must approve" in SECTIONS["pw-2"].text
    assert "does not need the agent's approval" in SECTIONS["vpn-2"].text
    assert BY_ID["locked-failed-sign-ins"].requires_approval and BY_ID["mfa-locked"].requires_approval
    assert not BY_ID["vpn-certificate-expired"].requires_approval
    assert "MFA is not reset" in SECTIONS["pw-3"].text and not BY_ID["mfa-sso-outage"].requires_approval


# ── Questions ──────────────────────────────────────────────────────────


def test_thirty_questions_with_unique_ids():
    assert len(QUESTIONS) == 30
    assert len({q.id for q in QUESTIONS}) == 30


def test_split_by_topic_with_no_shared_section():
    tuning = [q for q in QUESTIONS if q.split == "tuning"]
    heldout = [q for q in QUESTIONS if q.split == "heldout"]
    assert (len(tuning), len(heldout)) == (8, 22)
    assert {q.topic for q in tuning} == {"Devices", "Printing"}
    assert not {q.topic for q in tuning} & {q.topic for q in heldout}
    assert not {s for q in tuning for s in q.sections} & {s for q in heldout for s in q.sections}


def test_every_question_cites_real_sections_of_its_topic_or_a_reference():
    for q in QUESTIONS:
        assert q.route == "policy"
        assert all(s in SECTIONS for s in q.sections), q.id
        assert SECTIONS[q.sections[0]].topic == q.topic, q.id


def test_every_must_mention_fact_is_in_the_cited_sections():
    for q in QUESTIONS:
        text = " ".join(SECTIONS[s].text for s in q.sections).lower()
        for fact in q.must_mention:
            assert fact.lower() in text, (q.id, fact)


def test_every_section_is_asked_about_at_least_once():
    assert {s for q in QUESTIONS for s in q.sections} == set(SECTIONS)
