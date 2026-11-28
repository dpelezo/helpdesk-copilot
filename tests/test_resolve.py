"""Resolve and validate (#27): canonical issues, merging, conflicts, tool mapping, account changes."""

import json
from pathlib import Path

import pytest

from app.knowledge import resolve as r
from app.knowledge.model import KnowledgeRelease, Provenance, Runbook, RunbookStep
from app.knowledge.sources import Source, load_manifest
from app.mock.faults import ISSUES
from app.mock.systems import ACCOUNT_CHANGING_TOOLS

OUTLOOK = "microsoft-office--outlook-not-sending-emails"
VPN = "network--vpn-not-connecting"


def src(id, url=None):
    return Source(id=id, url=url or f"https://learn.microsoft.com/{id}", issues=[], fetched="2026-11-28", status="ok")


SOURCES = [src("ms-a"), src("ms-b"), src("handbook-it-policy", "repo:knowledge/handbook/it-policy.md")]


def item(issue, kind, text, source="ms-a", evidence="quoted words", ok=True):
    return {"issue": issue, "source_id": source, "kind": kind, "text": text, "evidence": evidence, "evidence_ok": ok}


def every_issue():
    """One cause per canonical issue, so the issue checks pass and a test can focus on one thing."""
    return [item(i, "cause", f"Base cause for {i}.", evidence=f"cause of {i}") for i in ISSUES]


def run(*items, base=True):
    extraction = {"date": "2026-11-28", "items": [*(every_issue() if base else []), *items]}
    return r.resolve(extraction, SOURCES, TOOLS)


@pytest.fixture(scope="module", autouse=True)
def _tools():
    global TOOLS
    TOOLS = r.registered_tools()


def nodes(release, kind):
    return [n for n in release.nodes if n.kind == kind]


# ── Tools ──────────────────────────────────────────────────────────────


def test_the_registry_is_read_from_the_mock_servers():
    assert TOOLS.changes_account == ACCOUNT_CHANGING_TOOLS == {"unlock_account", "reset_mfa"}
    assert {"account_status", "device_health", "printer_status", "service_status"} <= TOOLS.read_only
    assert not TOOLS.read_only & TOOLS.changes_account


def test_every_rule_names_a_registered_read_only_tool():
    assert {tool for tool, _ in r.TOOL_RULES} <= TOOLS.read_only


@pytest.mark.parametrize(("text", "tool"), [
    ("Check if the printer displays an offline status.", "printer_status"),
    ("Open the printer queue to check for any pending print jobs.", "printer_status"),
    ("Run the Certutil command to check for valid certificates in the user's certificate store.", "vpn_certificate_status"),
    ("Look for events labeled RasClient in the application logs.", "vpn_last_session"),
    ("Determine whether sign-in fails for everyone or just one user.", "service_status"),
    ("Check whether the Inbox is full.", "account_status"),
    ("Check the total disk space left in File Explorer.", "device_health"),
    ("Check for available Windows updates.", "device_health"),
    ("Verify that Microsoft Defender Antivirus is set as the primary antivirus.", "installed_software"),
    # What the tools do not report stays unmapped.
    ("Check the ambient temperature outside the device.", None),
    ("Monitor CPU, memory, and disk usage to find heavy processes.", None),
    ("Verify that the Windows Update service and BITS are both started.", None),
    ("Check for available updates in Microsoft Store.", None),
    ("Run ipconfig /all and check the DNS server.", None),
    # A question for the user is not a system check.
    ("Ask the user whether they made the failed sign-in attempts.", None),
])
def test_diagnostics_map_to_a_tool_only_when_the_tool_reports_it(text, tool):
    assert r.map_tool(text) == tool


def test_an_unmapped_diagnostic_is_reported_never_given_a_tool():
    release, report = run(item(VPN, "diagnostic", "Run ipconfig /all."),
                          item(VPN, "diagnostic", "Check the RasClient events.", evidence="RasClient"))
    assert report.valid
    assert [g.text for g in report.unmapped] == ["Run ipconfig /all."]
    assert [(n.description, n.tool) for n in nodes(release, "diagnostic")] == [("Check the RasClient events.", "vpn_last_session")]


# ── Merging and conflicts ──────────────────────────────────────────────


def test_the_same_wording_from_two_sources_is_one_node_with_both_sources():
    release, report = run(item(OUTLOOK, "cause", "The mailbox is full.", "ms-a", "mailbox is full"),
                          item(OUTLOOK, "cause", "the Mailbox is FULL", "ms-b", "your mailbox is full"))
    [merged] = [n for n in nodes(release, "cause") if "mailbox" in n.description.lower()]
    assert [p.source_id for p in merged.provenance] == ["ms-a", "ms-b"]
    assert len(report.merged) == 1


def test_plurals_and_filler_words_do_not_keep_duplicates_apart():
    assert r.key("Check the attachments.") == r.key("Attachment.")


def test_different_error_codes_are_different_items_and_never_a_conflict():
    release, report = run(item(VPN, "symptom", "Error code 0x80240022 appears.", "ms-a"),
                          item(VPN, "symptom", "Error code 0x80070422 appears.", "ms-b"))
    assert len(nodes(release, "symptom")) == 2
    assert not report.conflicts


def test_different_quantities_from_two_sources_are_a_conflict_and_both_are_kept():
    release, report = run(
        item(OUTLOOK, "diagnostic", "Check if stuck messages in the Outbox have attachments larger than 20MB.", "ms-a"),
        item(OUTLOOK, "diagnostic", "Check whether the message attachment is larger than 25MB.", "ms-b"),
    )
    [conflict] = report.conflicts
    assert {conflict.a.sources[0], conflict.b.sources[0]} == {"ms-a", "ms-b"}
    assert not report.possible_duplicates


def test_one_source_saying_two_things_is_not_a_conflict():
    _, report = run(item(OUTLOOK, "diagnostic", "Attachments larger than 20MB fail.", "ms-a"),
                    item(OUTLOOK, "diagnostic", "Attachments larger than 25MB fail.", "ms-a"))
    assert not report.conflicts


def test_similar_items_are_flagged_as_possible_duplicates_not_merged():
    release, report = run(
        item(VPN, "cause", "Required URLs are blocked by a firewall or proxy.", "ms-a"),
        item(VPN, "cause", "The firewall or proxy is blocking the required URLs.", "ms-b"),
    )
    assert len(report.possible_duplicates) == 1 and not report.merged
    assert sum("proxy" in n.description for n in nodes(release, "cause")) == 2


def test_items_are_only_compared_within_one_issue():
    _, report = run(item(VPN, "cause", "Disk full.", "ms-a"), item(OUTLOOK, "cause", "Disk full.", "ms-b"))
    assert not report.merged and not report.possible_duplicates


# ── Issues, evidence, provenance ───────────────────────────────────────


def test_all_thirteen_issues_build_with_seed_ids_for_the_seed_ones():
    release, report = run()
    issues = {n.id: n for n in nodes(release, "issue")}
    assert set(issues) == set(ISSUES) and report.valid
    assert issues["identity--mfa-lockout"].seed_id is None
    assert issues[VPN].seed_id == VPN and issues[VPN].category == "Network"


def test_items_without_evidence_are_left_out_and_reported():
    release, report = run(item(VPN, "cause", "Invented cause.", ok=False))
    assert not any(getattr(n, "description", None) == "Invented cause." for n in release.nodes)
    assert [i["text"] for i in report.missing_evidence] == ["Invented cause."]


def test_an_issue_with_nothing_extracted_blocks_the_release():
    _, report = run(*[i for i in every_issue() if i["issue"] != VPN], base=False)
    assert not report.valid and any(p.startswith(f"{VPN}: nothing extracted") for p in report.problems)


def test_an_issue_with_no_cause_blocks_the_release():
    items = [i for i in every_issue() if i["issue"] != VPN] + [item(VPN, "symptom", "It fails.")]
    _, report = run(*items, base=False)
    assert f"{VPN}: no possible causes extracted" in report.problems


def test_a_non_canonical_issue_or_unknown_source_stops_the_build():
    release, report = run(item("network--dns-broken", "cause", "x"), item(VPN, "cause", "y", source="nowhere"))
    assert release is None
    assert any("not canonical: network--dns-broken" in p for p in report.problems)
    assert any("not in the manifest: nowhere" in p for p in report.problems)


def test_the_handbook_is_cited_by_its_github_url():
    release, _ = run(item("identity--account-locked", "cause", "Five failed sign-ins lock the account.",
                          "handbook-it-policy", "After 5 failed sign-in attempts"))
    [cause] = [n for n in nodes(release, "cause") if "Five" in n.description]
    assert str(cause.provenance[0].source_url) == f"{r.REPO_URL}/knowledge/handbook/it-policy.md"


def test_node_ids_are_stable_and_valid():
    first, _ = run(item(VPN, "cause", "Certificate expired."))
    second, _ = run(item(VPN, "cause", "Certificate expired."))
    assert [n.id for n in first.nodes] == [n.id for n in second.nodes]
    KnowledgeRelease.model_validate_json(first.model_dump_json())  # ids pass the model's pattern


# ── Validation that needs the registry ─────────────────────────────────


def _release(*nodes):
    return KnowledgeRelease(release_id="draft-x", nodes=list(nodes), edges=[])


PROV = [Provenance(source_id="ms-a", source_url="https://learn.microsoft.com/a", evidence_span="words")]


def runbook(*steps):
    return Runbook(id="rb", title="Unlock", provenance=PROV,
                   steps=[RunbookStep(order=n, **s) for n, s in enumerate(steps, start=1)])


def test_an_account_change_must_use_an_account_changing_tool():
    problems = r.validate(_release(runbook({"instruction": "Unlock it", "tool": "account_status",
                                            "changes_account": True})), TOOLS)
    assert problems == ["rb step 1: an account change must use reset_mfa or unlock_account, not account_status"]


def test_a_step_naming_unlock_account_must_be_marked_as_an_account_change():
    problems = r.validate(_release(runbook({"instruction": "Unlock it", "tool": "unlock_account"})), TOOLS)
    assert problems == ["rb step 1: unlock_account changes an account; mark the step so it needs approval"]


def test_a_correct_runbook_validates_and_unknown_tools_do_not():
    good = runbook({"instruction": "Check the account", "tool": "account_status"},
                   {"instruction": "Unlock it", "tool": "unlock_account", "changes_account": True})
    assert r.validate(_release(good), TOOLS) == []
    bad = runbook({"instruction": "Reboot", "tool": "reboot_device"})
    assert r.validate(_release(bad), TOOLS) == ["rb step 1: reboot_device is not a registered tool"]


def test_a_diagnostic_on_an_unregistered_or_account_changing_tool_is_a_problem():
    from app.knowledge.model import Diagnostic

    d = Diagnostic(id="d", description="Unlock to test", tool="unlock_account", provenance=PROV)
    assert r.validate(_release(d), TOOLS) == ["d: tool unlock_account is not a registered read-only tool"]


# ── The committed draft ────────────────────────────────────────────────


def test_the_committed_extraction_builds_a_valid_draft_for_all_issues():
    extraction = json.loads(r.RESULTS.read_text())
    release, report = r.resolve(extraction, load_manifest(), TOOLS)
    assert report.valid, report.problems
    assert {n.id for n in nodes(release, "issue")} == set(ISSUES)
    assert all(n.tool in TOOLS.read_only for n in nodes(release, "diagnostic"))


def test_the_committed_report_and_draft_are_up_to_date():
    """Regenerate with `python -m app.knowledge.resolve` after a new extraction."""
    extraction = json.loads(r.RESULTS.read_text())
    release, report = r.resolve(extraction, load_manifest(), TOOLS)
    assert r.REPORT.read_text() == r.render(report)
    assert r.DRAFT.read_text() == release.model_dump_json(indent=2) + "\n"


def test_main_writes_both_files_and_fails_on_problems(tmp_path, capsys):
    out, report = tmp_path / "release.json", tmp_path / "report.md"
    assert r.main(["--out", str(out), "--report", str(report)]) == 0
    assert out.exists() and "**Valid.**" in report.read_text()
    bad = tmp_path / "extraction.json"
    bad.write_text(json.dumps({"date": "2026-11-28", "items": []}))
    assert r.main(["--extraction", str(bad), "--out", str(tmp_path / "x.json"), "--report", str(report)]) == 1
    assert "**Not valid.**" in report.read_text()
    assert "Not valid" in capsys.readouterr().out


def test_the_draft_is_not_publishable():
    release = KnowledgeRelease.model_validate_json(Path(r.DRAFT).read_text())
    assert release.unpublishable()  # every fact is a draft until a reviewer approves it
