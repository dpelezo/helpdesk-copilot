"""Unit tests for the seed profiler. Synthetic rows only; no network."""

import csv
import hashlib
from pathlib import Path

import httpx
import pytest

from app.knowledge import seed

HEADER = list(seed.EXPECTED_COLUMNS)


def row(id_, category, issue, *, symptoms="Screen stays black", solution="Reseat the cable",
        severity="Low", hours="4 hours", link="https://example.com/doc"):
    return [id_, category, issue, symptoms, solution, severity, hours,
            "Unknown", "display, cable", solution, link]


def write_csv(path: Path, rows, header=HEADER, bom=False) -> Path:
    with path.open("w", newline="", encoding="utf-8-sig" if bom else "utf-8") as handle:
        writer = csv.writer(handle)
        writer.writerow(header)
        writer.writerows(rows)
    return path


@pytest.fixture
def sample(tmp_path: Path) -> Path:
    return write_csv(tmp_path / "seed.csv", [
        row("1", "Hardware", "Monitor blank - Variant 3", severity="High", hours="2 hours"),
        row("2", "Hardware", "Monitor blank - Variant 9"),
        row("3", "Hardware", "Monitor blank - variant 3"),
        row("4", "Network", "VPN drops", symptoms="Disconnects hourly", solution="Renew cert"),
        row("5", "Network", "VPN drops - Variant 1", symptoms="Disconnects hourly",
            solution="Restart client"),
    ])


@pytest.mark.parametrize(("issue", "expected"), [
    ("AWS instance not starting - Variant 30", "AWS instance not starting"),
    ("AWS instance not starting -Variant 4 ", "AWS instance not starting"),
    ("Printer offline  - VARIANT 12", "Printer offline"),
    ("Outlook - Calendar not syncing", "Outlook - Calendar not syncing"),
])
def test_base_issue_strips_only_the_variant_suffix(issue, expected):
    assert seed.base_issue(issue) == expected


def test_seed_url_is_pinned_and_encodes_the_space():
    url = seed.seed_url()
    assert seed.SEED_REVISION in url
    assert url.endswith("with_Links%20.csv")


def test_load_rows_drops_the_urdu_column_and_reads_a_bom(tmp_path):
    path = write_csv(tmp_path / "bom.csv", [row("1", "Hardware", "Monitor blank")], bom=True)
    rows = seed.load_rows(path)
    assert rows[0]["ID"] == "1"
    assert "Urdu Solution" not in rows[0]


def test_load_rows_rejects_a_file_with_missing_columns(tmp_path):
    path = write_csv(tmp_path / "bad.csv", [["1", "Hardware"]], header=["ID", "Category"])
    with pytest.raises(seed.SeedError, match="missing columns"):
        seed.load_rows(path)


def test_canonicalize_groups_variants_and_keeps_lineage(sample):
    issues = {i.id: i for i in seed.canonicalize(seed.load_rows(sample))}
    assert set(issues) == {"hardware--monitor-blank", "network--vpn-drops"}
    monitor = issues["hardware--monitor-blank"]
    assert monitor.row_ids == ["1", "2", "3"]
    assert monitor.variant_count == 3
    assert monitor.conflicts == {}


def test_canonicalize_flags_rows_that_disagree(sample):
    vpn = {i.id: i for i in seed.canonicalize(seed.load_rows(sample))}["network--vpn-drops"]
    assert vpn.conflicts["Solution Steps"] == ["Renew cert", "Restart client"]


def test_profile_counts_duplicates_constants_and_noisy_columns(sample):
    rows = seed.load_rows(sample)
    issues = seed.canonicalize(rows)
    p = seed.profile(rows, issues)
    assert (p.rows, p.canonical_issues) == (5, 2)
    assert p.duplicate_ratio == 0.6
    assert "Common Causes" in p.constant_columns
    assert p.issues_with_varying_per_row_columns == {
        "Severity": 1, "Estimated Resolution Time": 1,
    }
    assert p.issues_with_conflicts == 1
    assert p.per_category == {"Hardware": 1, "Network": 1}


def test_report_leads_with_the_collapse_and_lists_issues(sample):
    rows = seed.load_rows(sample)
    issues = seed.canonicalize(rows)
    text = seed.render_report(seed.profile(rows, issues), issues, "ab" * 32)
    assert "**5 rows collapse to 2 canonical issues**" in text
    assert "`hardware--monitor-blank`" in text
    assert "carries no information" in text
    assert "| `network--vpn-drops` | Solution Steps | 2 |" in text


def test_download_fetches_the_pinned_file(tmp_path):
    body = b"ID,Category\n"
    seen = []

    def handler(request: httpx.Request) -> httpx.Response:
        seen.append(str(request.url))
        return httpx.Response(200, content=body)

    with httpx.Client(transport=httpx.MockTransport(handler)) as client:
        path, digest = seed.download(tmp_path / "out" / "seed.csv", client)
    assert seen == [seed.seed_url()]
    assert path.read_bytes() == body
    assert digest == hashlib.sha256(body).hexdigest()


def test_download_raises_on_http_error(tmp_path):
    transport = httpx.MockTransport(lambda request: httpx.Response(404))
    with httpx.Client(transport=transport) as client, pytest.raises(httpx.HTTPStatusError):
        seed.download(tmp_path / "seed.csv", client)


def test_run_offline_writes_report_and_canonical_json(sample, tmp_path):
    report = tmp_path / "reports" / "seed_profile.md"
    result = seed.run(offline=True, data_dir=sample.parent, report=report)
    assert result.canonical_issues == 2
    assert report.read_text().startswith("# Seed dataset profile")
    assert (sample.parent / "canonical_issues.json").exists()


def test_run_offline_without_a_download_fails_clearly(tmp_path):
    with pytest.raises(seed.SeedError, match="run once without --offline"):
        seed.run(offline=True, data_dir=tmp_path, report=tmp_path / "r.md")


def test_main_returns_1_and_reports_the_error(monkeypatch, capsys):
    monkeypatch.setattr(seed, "DATA_DIR", Path("/nonexistent"))

    def boom(offline):
        raise seed.SeedError("no file")

    monkeypatch.setattr(seed, "run", boom)
    assert seed.main(["--offline"]) == 1
    assert "seed: no file" in capsys.readouterr().err
