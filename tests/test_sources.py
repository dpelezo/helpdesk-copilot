"""Tests for collecting extraction sources. Synthetic rows and a mock web; no network."""

import httpx
import pytest

from app.knowledge import seed, sources
from tests.test_seed import row, write_csv

VPN = "https://docs.vendor.example/vpn/troubleshoot"
PRINT = "https://support.printco.example/printer-offline"
PRINT_2 = "https://support.printco.example/print-queue"

PAGES = {
    VPN: "<html><head><title>VPN troubleshooting</title><script>var x=1</script></head>"
         "<body><nav>Home | Products</nav><h1>VPN client cannot connect</h1>"
         "<p>Check the certificate expiry.</p>" + "<p>" + "Reinstall the client if it still fails. " * 20 + "</p>"
         "<footer>Copyright</footer></body></html>",
    PRINT: "<html><head><title>Printer offline</title></head><body><p>" + "Power-cycle the printer. " * 30
           + "</p></body></html>",
}


@pytest.fixture
def rows(tmp_path):
    path = write_csv(tmp_path / "seed.csv", [
        row("1", "Network", "VPN not connecting - Variant 1", link=VPN),
        row("2", "Network", "VPN not connecting - Variant 2", link=VPN),
        row("3", "Hardware", "Printer not responding", link=PRINT),
        row("4", "Hardware", "Printer not responding - Variant 9", link=PRINT_2),
        row("5", "Gaming", "Console lag", link="https://games.example/lag"),
    ])
    return seed.load_rows(path)


def web(pages=PAGES, redirects=None):
    redirects = redirects or {}

    def handler(request: httpx.Request) -> httpx.Response:
        url = str(request.url)
        assert request.headers["User-Agent"] == sources.USER_AGENT
        if url in redirects:
            return httpx.Response(301, headers={"Location": redirects[url]})
        if url in pages:
            return httpx.Response(200, text=pages[url], headers={"Content-Type": "text/html"})
        return httpx.Response(404)

    return httpx.Client(transport=httpx.MockTransport(handler), follow_redirects=True,
                        headers={"User-Agent": sources.USER_AGENT})


def test_links_for_in_scope_issues_include_alternate_links(rows):
    links = sources.seed_links(rows)
    assert links == {
        "network--vpn-not-connecting": [VPN],
        "hardware--printer-not-responding": [PRINT_2, PRINT],
    }
    assert sources.allowed_hosts(links) == {"vendor.example", "printco.example"}


def test_clean_html_drops_scripts_navigation_and_footer():
    title, text = sources.clean_html(PAGES[VPN])
    assert title == "VPN troubleshooting"
    assert text.startswith("VPN client cannot connect\nCheck the certificate expiry.\nReinstall")
    assert "Home" not in text and "Copyright" not in text and "var x" not in text


@pytest.mark.parametrize(("issue", "text", "expected"), [
    ("network--vpn-not-connecting", "Configure the VPN client", True),
    ("network--wi-fi-not-connecting", "Connecting to wireless networks", True),
    ("network--wi-fi-not-connecting", "Connecting a monitor", False),
    ("hardware--printer-not-responding", "Cookie policy", False),
])
def test_relevance_needs_an_issue_keyword(issue, text, expected):
    assert sources.relevant(issue, text) is expected


def test_every_seed_issue_in_scope_has_keywords():
    assert set(sources.KEYWORDS) == set(sources.SEED_ISSUES)


def test_collect_fetches_caches_and_records_failures(rows, tmp_path):
    with web() as client:
        found = sources.collect(rows, client, cache=tmp_path / "cache", today="2026-11-28", delay=0)
    by_url = {s.url: s for s in found}
    handbook = found[0]
    assert handbook.id == "handbook-it-policy" and handbook.issues == list(sources.HANDBOOK_ISSUES)

    vpn = by_url[VPN]
    assert vpn.status == "ok" and vpn.relevant_to == ["network--vpn-not-connecting"]
    assert vpn.sha256 and vpn.chars == len((tmp_path / "cache" / f"{vpn.id}.txt").read_text())
    assert vpn.fetched == "2026-11-28"

    assert by_url[PRINT].status == "ok"
    assert by_url[PRINT_2].status == "failed: HTTP 404"
    assert not (tmp_path / "cache" / f"{by_url[PRINT_2].id}.txt").exists()
    assert "https://games.example/lag" not in by_url  # out of scope, never fetched


def test_a_redirect_within_the_same_vendor_is_followed(rows, tmp_path):
    moved = "https://learn.vendor.example/vpn"
    pages = {**PAGES, moved: PAGES[VPN]}
    with web(pages, redirects={VPN: moved}) as client:
        found = {s.url: s for s in sources.collect(rows, client, cache=tmp_path, delay=0)}
    assert found[VPN].status == "ok" and found[VPN].final_url == moved


def test_a_page_with_too_little_text_is_a_failure(tmp_path):
    hosts = frozenset({"vendor.example"})
    tiny = {VPN: "<html><title>Client Challenge</title><body>Please enable JavaScript.</body></html>"}
    with web(tiny) as client:
        entry, text = sources.fetch(VPN, [], hosts, client, "d")
    assert entry.status.startswith("failed: only 25 characters") and text == ""


def test_a_redirect_off_the_allowlist_is_not_followed_into_the_manifest(rows, tmp_path):
    with web(redirects={VPN: "https://tracker.elsewhere.example/landing"}) as client:
        found = {s.url: s for s in sources.collect(rows, client, cache=tmp_path, delay=0)}
    assert found[VPN].status == "skipped: redirected off the allowlist to tracker.elsewhere.example"


def test_non_https_and_unlisted_urls_are_skipped(tmp_path):
    hosts = frozenset({"vendor.example"})
    with web() as client:
        http, _ = sources.fetch("http://docs.vendor.example/x", [], hosts, client, "d")
        other, _ = sources.fetch("https://evil.example/x", [], hosts, client, "d")
    assert http.status == other.status == "skipped: not an allowlisted https URL"


def test_network_errors_and_huge_pages_are_recorded(tmp_path, monkeypatch):
    def boom(request):
        raise httpx.ConnectTimeout("slow", request=request)

    hosts = frozenset({"vendor.example"})
    with httpx.Client(transport=httpx.MockTransport(boom)) as client:
        entry, text = sources.fetch(VPN, [], hosts, client, "d")
    assert entry.status == "failed: ConnectTimeout" and text == ""

    monkeypatch.setattr(sources, "MAX_BYTES", 10)
    with web() as client:
        entry, _ = sources.fetch(VPN, [], hosts, client, "d")
    assert entry.status == "skipped: page larger than 3 MB"


def test_coverage_counts_only_usable_sources(rows, tmp_path):
    with web() as client:
        found = sources.collect(rows, client, cache=tmp_path, delay=0)
    usable = sources.coverage(found)
    assert usable["network--vpn-not-connecting"] and usable["identity--mfa-lockout"] == ["handbook-it-policy"]
    assert usable["system--disk-space-running-out"] == []


def test_manifest_round_trips(rows, tmp_path):
    with web() as client:
        found = sources.collect(rows, client, cache=tmp_path / "c", delay=0)
    sources.write_manifest(found, tmp_path / "manifest.json")
    assert sources.load_manifest(tmp_path / "manifest.json") == found


def test_main_needs_the_seed_first(tmp_path, capsys):
    assert sources.main(["--seed", str(tmp_path / "missing.csv")]) == 1
    assert "run `python -m app.knowledge.seed` first" in capsys.readouterr().err


# ── Curated sources ────────────────────────────────────────────────────


def write_yaml(path, text):
    path.write_text(text)
    return path


def test_the_committed_curated_list_is_valid_and_covers_the_gaps():
    curated = sources.load_curated()
    covered = {i for c in curated for i in c.issues}
    assert {"system--disk-space-running-out", "system--system-running-slow", "hardware--laptop-overheating",
            "software--program-won-t-install", "software--application-not-updating",
            "hardware--printer-not-responding", "microsoft-office--outlook-not-sending-emails"} <= covered


@pytest.mark.parametrize(("entry", "problem"), [
    ("- url: https://random-blog.example/fix\n  issues: [network--vpn-not-connecting]\n  reason: r\n",
     "not an https URL on"),
    ("- url: http://support.microsoft.com/x\n  issues: [network--vpn-not-connecting]\n  reason: r\n",
     "not an https URL on"),
    ("- url: https://support.microsoft.com/x\n  issues: [identity--mfa-lockout]\n  reason: r\n",
     "out-of-scope issues"),
    ("- url: https://support.microsoft.com/x\n  issues: [network--vpn-not-connecting]\n  reason: ' '\n",
     "no reason given"),
    ("- url: https://support.microsoft.com/x\n  issues: [network--vpn-not-connecting]\n  reason: r\n"
     "- url: https://support.microsoft.com/x\n  issues: [network--vpn-not-connecting]\n  reason: r\n",
     "listed twice"),
])
def test_bad_curated_entries_are_rejected(tmp_path, entry, problem):
    with pytest.raises(ValueError, match=problem):
        sources.load_curated(write_yaml(tmp_path / "c.yml", entry))


def test_collect_adds_curated_sources_with_origin_and_reason(rows, tmp_path):
    article = "https://support.microsoft.com/en-us/printer-offline"
    pages = {**PAGES, article: "<html><title>Printer offline</title><body><p>" + "Restart the print spooler. " * 30
             + "</p></body></html>"}
    curated = [sources.Curated(article, ("hardware--printer-not-responding",), "Specific printer article.")]
    with web(pages) as client:
        found = {s.url: s for s in sources.collect(rows, client, cache=tmp_path, delay=0, curated=curated)}
    assert found[article].origin == "curated" and found[article].reason == "Specific printer article."
    assert found[article].status == "ok" and found[article].relevant_to == ["hardware--printer-not-responding"]
    assert found[VPN].origin == "seed" and found[VPN].reason is None


def test_the_committed_manifest_gives_every_issue_a_usable_source():
    """Done-when of #25: each in-scope issue has at least one fetched source about it."""
    manifest = sources.load_manifest()
    assert {i: ids for i, ids in sources.coverage(manifest).items() if not ids} == {}
    for s in manifest:
        assert s.status == "ok" or s.status.startswith(("failed:", "skipped:")), s.status
        assert s.origin in ("seed", "curated", "local")
        if s.origin == "curated":
            assert s.reason
