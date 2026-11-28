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
         "<p>Check the certificate expiry.</p><footer>Copyright</footer></body></html>",
    PRINT: "<html><head><title>Printer offline</title></head><body><p>Power-cycle the printer.</p></body></html>",
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
    assert sources.allowed_hosts(links) == {"docs.vendor.example", "support.printco.example"}


def test_clean_html_drops_scripts_navigation_and_footer():
    title, text = sources.clean_html(PAGES[VPN])
    assert title == "VPN troubleshooting"
    assert text == "VPN client cannot connect\nCheck the certificate expiry."


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


def test_a_redirect_off_the_allowlist_is_not_followed_into_the_manifest(rows, tmp_path):
    with web(redirects={VPN: "https://tracker.elsewhere.example/landing"}) as client:
        found = {s.url: s for s in sources.collect(rows, client, cache=tmp_path, delay=0)}
    assert found[VPN].status == "skipped: redirected off the allowlist to tracker.elsewhere.example"


def test_non_https_and_unlisted_urls_are_skipped(tmp_path):
    hosts = frozenset({"docs.vendor.example"})
    with web() as client:
        http, _ = sources.fetch("http://docs.vendor.example/x", [], hosts, client, "d")
        other, _ = sources.fetch("https://evil.example/x", [], hosts, client, "d")
    assert http.status == other.status == "skipped: not an allowlisted https URL"


def test_network_errors_and_huge_pages_are_recorded(tmp_path, monkeypatch):
    def boom(request):
        raise httpx.ConnectTimeout("slow", request=request)

    hosts = frozenset({"docs.vendor.example"})
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
