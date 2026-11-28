"""Collect the sources Workflow A extracts from (issue #25).

For each in-scope seed issue, fetch the documentation pages the seed links to,
plus the curated official articles in ``knowledge/sources/curated.yml`` for issues
the seed's links do not cover, clean them to text, and record them in a manifest. Only the manifest is
committed (``knowledge/sources/manifest.json``); the cleaned text is cached in
``/data/`` and never committed, because vendor pages are not ours to
redistribute (ADR 0005, AGENTS.md). The IT policy handbook is a local source.

Runs in GitHub Actions (this workspace cannot reach the web):

    python -m app.knowledge.seed       # download the seed first
    python -m app.knowledge.sources    # fetch, clean, write the manifest
"""

from __future__ import annotations

import argparse
import hashlib
import json
import re
import sys
import time
from dataclasses import asdict, dataclass, field
from datetime import date
from html.parser import HTMLParser
from pathlib import Path
from urllib.parse import urlparse

import httpx
import yaml

from app.knowledge.policy import HANDBOOK
from app.knowledge.seed import DATA_DIR, canonicalize, load_rows
from app.mock.faults import ISSUES

MANIFEST = Path("knowledge/sources/manifest.json")
CURATED = Path("knowledge/sources/curated.yml")
# Curated sources may come only from these official vendor domains.
CURATED_VENDORS = frozenset({"microsoft.com", "apple.com"})
CACHE = Path("data/sources")
USER_AGENT = "helpdesk-copilot-sources/0.1 (+https://github.com/dpelezo/helpdesk-copilot)"
DELAY_SECONDS = 1.0
MAX_BYTES = 3_000_000
# Less text than this is a bot challenge, a cookie wall or a page that needs JavaScript.
MIN_CHARS = 500
LICENCE_NOTE = "Vendor documentation. Cleaned text cached locally for extraction only; not redistributed."

# Issues written by hand (no seed row, so no seed links): sourced from the handbook.
HANDBOOK_ISSUES = ("identity--account-locked", "identity--mfa-lockout")
SEED_ISSUES = tuple(i for i in ISSUES if i not in HANDBOOK_ISSUES)

_SKIP = {"script", "style", "noscript", "nav", "header", "footer", "aside", "form", "svg", "button"}
_BLOCK = {"p", "div", "li", "h1", "h2", "h3", "h4", "h5", "h6", "tr", "br", "section", "article", "pre", "td"}
# Words a page must mention to count as being about the issue (any one is enough).
KEYWORDS: dict[str, tuple[str, ...]] = {
    "network--vpn-not-connecting": ("vpn",),
    "network--wi-fi-not-connecting": ("wi-fi", "wifi", "wireless"),
    "microsoft-office--outlook-not-sending-emails": ("outlook", "email", "e-mail"),
    "system--disk-space-running-out": ("disk space", "storage", "free space"),
    "system--system-running-slow": ("slow", "performance"),
    "hardware--laptop-overheating": ("overheat", "temperature", "fan"),
    "software--program-won-t-install": ("install",),
    "software--application-not-updating": ("update",),
    "security--antivirus-not-updating": ("antivirus", "virus", "defender"),
    "security--phishing-email-detected": ("phishing",),
    "hardware--printer-not-responding": ("printer", "print"),
}


class _Text(HTMLParser):
    def __init__(self) -> None:
        super().__init__(convert_charrefs=True)
        self.parts: list[str] = []
        self.skipping = 0
        self.title = ""
        self._in_title = False

    def handle_starttag(self, tag: str, attrs: list[tuple[str, str | None]]) -> None:
        if tag in _SKIP:
            self.skipping += 1
        elif tag == "title":
            self._in_title = True
        elif tag in _BLOCK:
            self.parts.append("\n")

    def handle_endtag(self, tag: str) -> None:
        if tag in _SKIP and self.skipping:
            self.skipping -= 1
        elif tag == "title":
            self._in_title = False
        elif tag in _BLOCK:
            self.parts.append("\n")

    def handle_data(self, data: str) -> None:
        if self._in_title:
            self.title += data
        elif not self.skipping:
            self.parts.append(data)


def clean_html(html: str) -> tuple[str, str]:
    """(title, text): visible text with scripts, navigation and boilerplate blocks removed."""
    parser = _Text()
    parser.feed(html)
    lines = (re.sub(r"[ \t\r\f\v]+", " ", line).strip() for line in "".join(parser.parts).split("\n"))
    text = "\n".join(line for line in lines if line)
    return " ".join(parser.title.split()), text


def seed_links(rows: list[dict[str, str]]) -> dict[str, list[str]]:
    """In-scope seed issue id -> the documentation links its rows give, sorted."""
    links: dict[str, list[str]] = {}
    for issue in canonicalize(rows):
        if issue.id not in SEED_ISSUES:
            continue
        found = {issue.documentation_link, *issue.conflicts.get("Documentation Link", [])}
        links[issue.id] = sorted(u for u in found if u)
    return links


def vendor(host: str | None) -> str:
    """The vendor's domain: the last two labels of a host (support.microsoft.com -> microsoft.com)."""
    return ".".join((host or "").split(".")[-2:])


def allowed_hosts(links: dict[str, list[str]]) -> frozenset[str]:
    """Vendor domains of the seed's links. Any host under one of them is allowed."""
    return frozenset(vendor(urlparse(u).hostname) for urls in links.values() for u in urls)


def relevant(issue: str, text: str) -> bool:
    """A page is about the issue when it mentions one of the issue's keywords."""
    lowered = text.lower()
    return any(word in lowered for word in KEYWORDS.get(issue, ()))


@dataclass
class Source:
    id: str
    url: str
    issues: list[str]
    fetched: str
    status: str  # "ok", or why it failed
    final_url: str | None = None
    title: str | None = None
    sha256: str | None = None
    chars: int = 0
    relevant_to: list[str] = field(default_factory=list)
    licence: str = LICENCE_NOTE
    origin: str = "seed"  # "seed", "curated" or "local"
    reason: str | None = None  # why a curated source was chosen


def _source_id(url: str) -> str:
    parsed = urlparse(url)
    slug = re.sub(r"[^a-z0-9]+", "-", f"{parsed.hostname}{parsed.path}".lower()).strip("-")
    return slug[:100]


def fetch(url: str, issues: list[str], hosts: frozenset[str], client: httpx.Client, today: str) -> tuple[Source, str]:
    """Fetch one page. Returns the manifest entry and the cleaned text ("" on failure)."""
    entry = Source(id=_source_id(url), url=url, issues=issues, fetched=today, status="ok")
    if urlparse(url).scheme != "https" or vendor(urlparse(url).hostname) not in hosts:
        entry.status = "skipped: not an allowlisted https URL"
        return entry, ""
    try:
        response = client.get(url)
    except httpx.HTTPError as error:
        entry.status = f"failed: {type(error).__name__}"
        return entry, ""
    final = str(response.url)
    entry.final_url = final if final != url else None
    if vendor(urlparse(final).hostname) not in hosts:
        entry.status = f"skipped: redirected off the allowlist to {urlparse(final).hostname}"
        return entry, ""
    if response.status_code != 200:
        entry.status = f"failed: HTTP {response.status_code}"
        return entry, ""
    if len(response.content) > MAX_BYTES:
        entry.status = "skipped: page larger than 3 MB"
        return entry, ""
    title, text = clean_html(response.text)
    if len(text) < MIN_CHARS:
        entry.status = f"failed: only {len(text)} characters of text (bot challenge, cookie wall or script-only page)"
        return entry, ""
    entry.title = title or None
    entry.sha256 = hashlib.sha256(text.encode()).hexdigest()
    entry.chars = len(text)
    entry.relevant_to = [i for i in issues if relevant(i, f"{title}\n{text}")]
    return entry, text


def handbook_source(today: str) -> Source:
    text = HANDBOOK.read_text()
    return Source(
        id="handbook-it-policy",
        url=f"repo:{HANDBOOK.as_posix()}",
        issues=list(HANDBOOK_ISSUES),
        fetched=today,
        status="ok",
        title="Kestrel Ridge Logistics: IT policy handbook",
        sha256=hashlib.sha256(text.encode()).hexdigest(),
        chars=len(text),
        relevant_to=list(HANDBOOK_ISSUES),
        licence="Written for this repo (fictional company).",
        origin="local",
    )


@dataclass(frozen=True)
class Curated:
    url: str
    issues: tuple[str, ...]
    reason: str


def load_curated(path: Path = CURATED) -> list[Curated]:
    """The curated list, checked: known issues, official vendors, a reason, no repeats."""
    entries = [Curated(e["url"], tuple(e["issues"]), e["reason"]) for e in yaml.safe_load(path.read_text())]
    problems = []
    for e in entries:
        if vendor(urlparse(e.url).hostname) not in CURATED_VENDORS or urlparse(e.url).scheme != "https":
            problems.append(f"{e.url}: not an https URL on {sorted(CURATED_VENDORS)}")
        if unknown := [i for i in e.issues if i not in SEED_ISSUES]:
            problems.append(f"{e.url}: unknown or out-of-scope issues {unknown}")
        if not e.reason.strip():
            problems.append(f"{e.url}: no reason given")
    if len({e.url for e in entries}) != len(entries):
        problems.append("a curated URL is listed twice")
    if problems:
        raise ValueError("; ".join(problems))
    return entries


def collect(rows: list[dict[str, str]], client: httpx.Client, cache: Path = CACHE,
            today: str | None = None, delay: float = DELAY_SECONDS,
            curated: list[Curated] | None = None) -> list[Source]:
    today = today or date.today().isoformat()
    links = seed_links(rows)
    curated = curated or []
    hosts = allowed_hosts(links) | {vendor(urlparse(c.url).hostname) for c in curated}
    planned: dict[str, tuple[set[str], str, str | None]] = {}
    for issue, urls in links.items():
        for url in urls:
            planned.setdefault(url, (set(), "seed", None))[0].add(issue)
    for c in curated:
        issues, origin, reason = planned.get(c.url, (set(), "curated", c.reason))
        planned[c.url] = (issues | set(c.issues), origin, reason)
    cache.mkdir(parents=True, exist_ok=True)
    sources = [handbook_source(today)]
    for n, (url, (issues, origin, reason)) in enumerate(sorted(planned.items())):
        if n and delay:
            time.sleep(delay)
        entry, text = fetch(url, sorted(issues), hosts, client, today)
        entry.origin, entry.reason = origin, reason
        if text:
            (cache / f"{entry.id}.txt").write_text(text)
        sources.append(entry)
    return sources


def coverage(sources: list[Source]) -> dict[str, list[str]]:
    """Issue id -> ids of usable sources (fetched and about the issue)."""
    found: dict[str, list[str]] = {i: [] for i in ISSUES}
    for s in sources:
        if s.status == "ok":
            for issue in s.relevant_to:
                found[issue].append(s.id)
    return found


def write_manifest(sources: list[Source], path: Path = MANIFEST) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(json.dumps([asdict(s) for s in sources], indent=2) + "\n")


def load_manifest(path: Path = MANIFEST) -> list[Source]:
    return [Source(**row) for row in json.loads(path.read_text())]


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description="Fetch and record the extraction sources.")
    parser.add_argument("--seed", type=Path, default=DATA_DIR / "seed.csv")
    args = parser.parse_args(argv)
    if not args.seed.exists():
        print(f"sources: {args.seed} not found; run `python -m app.knowledge.seed` first", file=sys.stderr)
        return 1
    headers = {"User-Agent": USER_AGENT, "Accept": "text/html"}
    with httpx.Client(timeout=30, follow_redirects=True, headers=headers, max_redirects=5) as client:
        sources = collect(load_rows(args.seed), client, curated=load_curated())
    write_manifest(sources)
    usable = coverage(sources)
    for issue, ids in usable.items():
        print(f"{issue}: {len(ids)} usable source(s)")
    print(f"{sum(s.status == 'ok' for s in sources)}/{len(sources)} sources ok -> {MANIFEST}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
