"""Resolve and validate: turn extracted items into a draft ``KnowledgeRelease`` (issue #27).

Runs between extraction and human review, with no model calls: everything here is
deterministic, so the same extraction always gives the same draft and the same report.

- **Canonical issues.** Every extracted item already carries one of the 13 canonical ids
  (``app.mock.faults.ISSUES``): 11 from ``reports/seed_profile.md``, two written by hand.
- **Duplicates.** Items of the same kind for the same issue are merged when their wording is
  the same once case, punctuation and filler words are set aside. The merged node keeps the
  provenance of every source. Items that are only similar are *not* merged: they are listed
  for the reviewer as possible duplicates.
- **Conflicts.** Two similar items from different sources that state the same kind of quantity
  with different values (an attachment limit of 20 MB in one source and 25 MB in another) are
  a disagreement. Error codes and versions are not quantities: they never conflict. Both are kept and flagged; picking
  one would be false certainty.
- **Tools.** A diagnostic becomes a node only when a rule maps it to a registered read-only
  tool on the mock systems. A diagnostic with no tool is listed, never given an invented one.
- **Account changes.** A runbook step that changes an account must name ``unlock_account`` or
  ``reset_mfa``, and a step naming either must be marked as changing an account.

Diagnostics and solutions are not yet linked to causes (``TESTS_FOR``, ``MITIGATED_BY``):
extraction does not say which check tests which cause, so the reviewer links them (#28).
"""

from __future__ import annotations

import argparse
import asyncio
import hashlib
import json
import re
import sys
from collections import defaultdict
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any

from mcp import Client

from app.knowledge.extract import KINDS, RESULTS
from app.knowledge.model import (
    Cause,
    Diagnostic,
    HasPossibleCause,
    HasSymptom,
    Issue,
    KnowledgeRelease,
    Provenance,
    Solution,
    Symptom,
)
from app.knowledge.sources import HANDBOOK_ISSUES, MANIFEST, Source, load_manifest
from app.mock import company
from app.mock.faults import ISSUES
from app.mock.systems import ACCOUNT_CHANGING_TOOLS, SERVERS, World

DRAFT = Path("knowledge/draft/release.json")
REPORT = Path("reports/validation.md")
REPO_URL = "https://github.com/dpelezo/helpdesk-copilot/blob/main"

# Similar enough to show the reviewer as a possible duplicate.
SIMILAR = 0.6
# Two items stating the same kind of quantity (MB, days, attempts) with different values are a
# conflict at a lower similarity: "attachments larger than 20MB" against "larger than 25MB".
CONFLICT_SIMILAR = 0.5

_FILLER = frozenset(
    "a an the and or of to in on for from with by is are be it its this that your you if "
    "whether check verify see make sure have has than".split()
)
_TOKEN = re.compile(r"[a-z0-9]+(?:\.[0-9]+)?")
# A quantity is a number with a unit. Error codes, versions and ports are identifiers: they
# keep two items apart but never make them disagree.
_QUANTITY = re.compile(
    r"\b(\d+(?:\.\d+)?)\s*(kb|mb|gb|tb|%|percent|days?|hours?|minutes?|seconds?|attempts?|times|characters)\b"
)

# Diagnostic → read-only tool, first match wins. Each rule reads a field the tool returns
# (app/mock/systems.py), so a mapped check can actually be run. Kept narrow on purpose:
# an unmapped diagnostic costs the reviewer a look; a wrong tool misleads the copilot.
TOOL_RULES: tuple[tuple[str, re.Pattern[str]], ...] = tuple(
    (tool, re.compile(pattern, re.IGNORECASE))
    for tool, pattern in (
        ("printer_status", r"\bprinter('s)? (status|queue)|\bprint(er)? queue|pending print jobs|printer (shows|displays) (as )?(an )?offline"),
        ("vpn_certificate_status", r"\bcertificates?\b.*\b(user|client|valid|expir)"),
        ("vpn_last_session", r"\brasclient\b|\berror 812\b|\bvpn client\b"),
        ("service_status", r"\boutage\b|\bfor everyone\b|\bservice (status|health)\b"),
        ("account_status", r"\bfailed sign-in|\baccount (is )?locked|\block(ed)? ?out\b|\bpassword (was )?(recently )?(changed|expired)|\bmfa\b|\binbox is full\b|\bmailbox storage\b"),
        ("device_health", r"\bdisk space\b|\bstorage pane\b|\b(cpu|processor) temperature\b|\bavailable (windows )?updates\b(?! (in|to|for) microsoft store)|\bfor windows updates\b|\bpending installation\b|\bcompliant\b|\b(definitions?|security intelligence) (are |is )?(out of date|age|version)"),
        ("installed_software", r"\bprimary antivirus\b|\bthird-party antivirus\b|\bis installed\b"),
    )
)


# ── Tools ──────────────────────────────────────────────────────────────


@dataclass(frozen=True)
class Registry:
    read_only: frozenset[str]
    changes_account: frozenset[str]


async def _list_tools() -> Registry:
    world = World(company.generate())
    read_only, changing = set(), set()
    for build in SERVERS.values():
        async with Client(build(world)) as client:
            for tool in (await client.list_tools()).tools:
                (read_only if tool.annotations and tool.annotations.read_only_hint else changing).add(tool.name)
    return Registry(frozenset(read_only), frozenset(changing))


def registered_tools() -> Registry:
    """The tools the mock systems actually register, read from the servers themselves."""
    return asyncio.run(_list_tools())


# A question for the user is not a system check, whatever it mentions.
_ASK = re.compile(r"^\s*(ask|contact)\b", re.IGNORECASE)


def map_tool(text: str) -> str | None:
    if _ASK.search(text):
        return None
    for tool, pattern in TOOL_RULES:
        if pattern.search(text):
            return tool
    return None


# ── Text comparison ────────────────────────────────────────────────────


def _singular(word: str) -> str:
    return word[:-1] if len(word) > 3 and word.endswith("s") and not word.endswith("ss") else word


def words(text: str) -> frozenset[str]:
    """Content words, singular, without numbers, codes or filler."""
    return frozenset(_singular(t) for t in _TOKEN.findall(text.lower())
                     if not any(ch.isdigit() for ch in t) and t not in _FILLER)


def identifiers(text: str) -> frozenset[str]:
    """Every token with a digit in it: numbers, error codes, versions."""
    return frozenset(t for t in _TOKEN.findall(text.lower()) if any(ch.isdigit() for ch in t))


def quantities(text: str) -> dict[str, frozenset[str]]:
    """Values by unit: {"mb": {"20"}}. Plural and singular units are the same unit."""
    found: dict[str, set[str]] = defaultdict(set)
    for value, unit in _QUANTITY.findall(text.lower()):
        found[_singular(unit)].add(value)
    return {unit: frozenset(values) for unit, values in found.items()}


def key(text: str) -> tuple[frozenset[str], frozenset[str]]:
    """Wording once case, punctuation, plurals and filler are set aside; numbers and codes kept."""
    return words(text), identifiers(text)


def disagree(a: str, b: str) -> bool:
    """Both state a quantity in the same unit, with different values."""
    qa, qb = quantities(a), quantities(b)
    return any(qa[u] != qb[u] for u in qa.keys() & qb.keys())


def similarity(a: str, b: str) -> float:
    wa, wb = words(a), words(b)
    return len(wa & wb) / len(wa | wb) if wa | wb else 0.0


# ── Resolve ────────────────────────────────────────────────────────────


@dataclass
class Merged:
    """One node's worth of extracted items that say the same thing."""

    issue: str
    kind: str
    text: str
    items: list[dict[str, Any]]

    @property
    def sources(self) -> list[str]:
        return sorted({i["source_id"] for i in self.items})

    @property
    def node_id(self) -> str:
        digest = hashlib.sha256(f"{self.issue}|{self.kind}|{self.text}".encode()).hexdigest()[:8]
        return f"{self.issue}--{self.kind}-{digest}"


@dataclass
class Pair:
    issue: str
    kind: str
    a: Merged
    b: Merged
    similarity: float


@dataclass
class Report:
    release_id: str
    extraction_date: str
    per_issue: dict[str, dict[str, int]] = field(default_factory=dict)
    merged: list[Merged] = field(default_factory=list)
    conflicts: list[Pair] = field(default_factory=list)
    possible_duplicates: list[Pair] = field(default_factory=list)
    unmapped: list[Merged] = field(default_factory=list)
    mapped: list[tuple[Merged, str]] = field(default_factory=list)
    missing_evidence: list[dict[str, Any]] = field(default_factory=list)
    problems: list[str] = field(default_factory=list)

    @property
    def valid(self) -> bool:
        return not self.problems


def source_url(source: Source) -> str:
    if source.url.startswith("repo:"):
        return f"{REPO_URL}/{source.url.removeprefix('repo:')}"
    return source.final_url or source.url


def merge(items: list[dict[str, Any]]) -> list[Merged]:
    """Group items with the same wording. The first item's text names the group."""
    groups: dict[tuple[str, str, Any], Merged] = {}
    for item in items:
        k = (item["issue"], item["kind"], key(item["text"]))
        if k in groups:
            groups[k].items.append(item)
        else:
            groups[k] = Merged(item["issue"], item["kind"], item["text"].strip(), [item])
    return list(groups.values())


def compare(groups: list[Merged]) -> tuple[list[Pair], list[Pair]]:
    """Similar pairs from different sources: conflicts if a quantity differs, else possible duplicates."""
    conflicts, duplicates = [], []
    by_scope: dict[tuple[str, str], list[Merged]] = defaultdict(list)
    for g in groups:
        by_scope[(g.issue, g.kind)].append(g)
    for (issue, kind), scoped in by_scope.items():
        for i, a in enumerate(scoped):
            for b in scoped[i + 1:]:
                if set(a.sources) == set(b.sources):
                    continue  # one source saying two things is not a disagreement between sources
                score = similarity(a.text, b.text)
                pair = Pair(issue, kind, a, b, round(score, 2))
                if disagree(a.text, b.text):
                    if score >= CONFLICT_SIMILAR:
                        conflicts.append(pair)
                elif score >= SIMILAR:
                    duplicates.append(pair)
    return conflicts, duplicates


def provenance(group: Merged, sources: dict[str, Source]) -> list[Provenance]:
    seen, out = set(), []
    for item in group.items:
        if (item["source_id"], item["evidence"]) in seen:
            continue
        seen.add((item["source_id"], item["evidence"]))
        out.append(Provenance(source_id=item["source_id"], source_url=source_url(sources[item["source_id"]]),
                              evidence_span=item["evidence"]))
    return out


def resolve(extraction: dict[str, Any], sources: list[Source], tools: Registry) -> tuple[KnowledgeRelease | None, Report]:
    by_id = {s.id: s for s in sources}
    release_id = f"draft-{extraction['date']}"
    report = Report(release_id, extraction["date"])

    kept = [i for i in extraction["items"] if i["evidence_ok"]]
    report.missing_evidence = [i for i in extraction["items"] if not i["evidence_ok"]]
    unknown = sorted({i["issue"] for i in kept} - set(ISSUES))
    if unknown:
        report.problems.append(f"items for issues that are not canonical: {', '.join(unknown)}")
    unknown_sources = sorted({i["source_id"] for i in kept} - set(by_id))
    if unknown_sources:
        report.problems.append(f"items from sources not in the manifest: {', '.join(unknown_sources)}")
    if report.problems:
        return None, report

    groups = merge(kept)
    report.merged = [g for g in groups if len(g.items) > 1]
    report.conflicts, report.possible_duplicates = compare(groups)

    nodes: list[Any] = []
    edges: list[Any] = []
    for issue, title in ISSUES.items():
        mine = [g for g in groups if g.issue == issue]
        issue_sources = sorted({s for g in mine for s in g.sources})
        counts = {kind: sum(g.kind == kind for g in mine) for kind in KINDS}
        if not issue_sources:
            report.problems.append(f"{issue}: nothing extracted, so the issue has no provenance")
            report.per_issue[issue] = {**counts, "mapped": 0}
            continue
        anchor = next(g for g in mine)
        nodes.append(Issue(
            id=issue, title=title, category=issue.split("--")[0].replace("-", " ").title(),
            seed_id=None if issue in HANDBOOK_ISSUES else issue,
            provenance=provenance(Merged(issue, "issue", title, [anchor.items[0]]), by_id),
        ))
        mapped = 0
        for g in mine:
            prov = provenance(g, by_id)
            if g.kind == "symptom":
                nodes.append(Symptom(id=g.node_id, description=g.text, provenance=prov))
                edges.append(HasSymptom(source=issue, target=g.node_id, provenance=prov))
            elif g.kind == "cause":
                nodes.append(Cause(id=g.node_id, description=g.text, provenance=prov))
                edges.append(HasPossibleCause(source=issue, target=g.node_id, provenance=prov))
            elif g.kind == "solution":
                nodes.append(Solution(id=g.node_id, description=g.text, provenance=prov))
            else:
                tool = map_tool(g.text)
                if tool is None:
                    report.unmapped.append(g)
                    continue
                mapped += 1
                report.mapped.append((g, tool))
                nodes.append(Diagnostic(id=g.node_id, description=g.text, tool=tool, provenance=prov))
        report.per_issue[issue] = {**counts, "mapped": mapped}
        if counts["cause"] == 0:
            report.problems.append(f"{issue}: no possible causes extracted")

    try:
        release = KnowledgeRelease(release_id=release_id, nodes=nodes, edges=edges)
    except ValueError as error:
        report.problems.append(f"release does not build: {error}")
        return None, report
    report.problems.extend(validate(release, tools))
    return release, report


def validate(release: KnowledgeRelease, tools: Registry) -> list[str]:
    """Checks the model cannot make on its own, because they need the tool registry."""
    problems = []
    for node in release.nodes:
        if node.kind == "diagnostic" and node.tool not in tools.read_only:
            problems.append(f"{node.id}: tool {node.tool} is not a registered read-only tool")
        if node.kind == "runbook":
            for step in node.steps:
                if step.changes_account and step.tool not in ACCOUNT_CHANGING_TOOLS:
                    problems.append(f"{node.id} step {step.order}: an account change must use "
                                    f"{' or '.join(sorted(ACCOUNT_CHANGING_TOOLS))}, not {step.tool}")
                if step.tool in ACCOUNT_CHANGING_TOOLS and not step.changes_account:
                    problems.append(f"{node.id} step {step.order}: {step.tool} changes an account; "
                                    "mark the step so it needs approval")
                if step.tool and step.tool not in tools.read_only | tools.changes_account:
                    problems.append(f"{node.id} step {step.order}: {step.tool} is not a registered tool")
    if ACCOUNT_CHANGING_TOOLS != tools.changes_account:
        problems.append(f"the account-changing tools registered ({', '.join(sorted(tools.changes_account))}) "
                        f"do not match the policy list ({', '.join(sorted(ACCOUNT_CHANGING_TOOLS))})")
    return problems


# ── Report ─────────────────────────────────────────────────────────────


def _n(count: int, noun: str) -> str:
    return f"{count} {noun}{'' if count == 1 else 's'}"


def _cell(text: str) -> str:
    return text.replace("|", "\\|").replace("\n", " ")


def render(report: Report) -> str:
    total = {k: sum(c[k] for c in report.per_issue.values()) for k in (*KINDS, "mapped")}
    lines = [
        f"# Validation report: `{report.release_id}`",
        "",
        f"Draft built by `python -m app.knowledge.resolve` from the extraction of {report.extraction_date} "
        f"(`{RESULTS.as_posix()}`). No model calls; the same extraction always gives this report.",
        "",
        f"**{'Valid' if report.valid else 'Not valid'}.** "
        f"{len(report.per_issue)} issues; {total['symptom']} symptoms, {total['cause']} possible causes, "
        f"{total['solution']} solutions, {total['diagnostic']} diagnostics, of which {total['mapped']} map to a tool. "
        f"For review: **{_n(len(report.conflicts), 'conflict')}**, {_n(len(report.possible_duplicates), 'possible duplicate')}, "
        f"{_n(len(report.unmapped), 'diagnostic')} with no tool. "
        f"{_n(len(report.merged), 'group')} merged; {_n(len(report.missing_evidence), 'item')} left out for missing evidence.",
        "",
        "Diagnostics and solutions are not linked to causes yet: extraction does not say which check tests "
        "which cause, so the reviewer links them.",
    ]
    if report.problems:
        lines += ["", "## Problems (block the release)", ""] + [f"- {p}" for p in report.problems]
    lines += ["", "## By issue", "", "| Issue | Symptoms | Causes | Diagnostics (with a tool) | Solutions |",
              "| --- | ---: | ---: | ---: | ---: |"]
    for issue, c in report.per_issue.items():
        lines.append(f"| `{issue}` | {c['symptom']} | {c['cause']} | {c['diagnostic']} ({c['mapped']}) | {c['solution']} |")

    lines += ["", "## Conflicts: sources disagree, both kept", ""]
    if report.conflicts:
        lines += ["| Issue | Kind | One source says | Another says |", "| --- | --- | --- | --- |"]
        for p in report.conflicts:
            lines.append(f"| `{p.issue}` | {p.kind} | {_cell(p.a.text)} ({', '.join(p.a.sources)}) "
                         f"| {_cell(p.b.text)} ({', '.join(p.b.sources)}) |")
    else:
        lines.append("None found.")

    lines += ["", "## Possible duplicates: similar wording from different sources, not merged", ""]
    if report.possible_duplicates:
        lines += ["| Issue | Kind | A | B | Similarity |", "| --- | --- | --- | --- | ---: |"]
        for p in report.possible_duplicates:
            lines.append(f"| `{p.issue}` | {p.kind} | {_cell(p.a.text)} | {_cell(p.b.text)} | {p.similarity:.2f} |")
    else:
        lines.append("None found.")

    lines += ["", "## Diagnostics with no tool", "",
              "Kept out of the release: the copilot cannot run them on the company's systems. "
              "A reviewer can map one to a tool or leave it as advice for the agent.", ""]
    by_issue: dict[str, list[Merged]] = defaultdict(list)
    for g in report.unmapped:
        by_issue[g.issue].append(g)
    for issue, groups in by_issue.items():
        lines.append(f"- `{issue}`: " + "; ".join(_cell(g.text) for g in groups))

    lines += ["", "## Diagnostics mapped to a tool", "", "| Issue | Diagnostic | Tool |", "| --- | --- | --- |"]
    for g, tool in report.mapped:
        lines.append(f"| `{g.issue}` | {_cell(g.text)} | `{tool}` |")

    lines += ["", "## Left out: evidence not found in the source", ""]
    lines += [f"- `{i['issue']}` {i['kind']}: {_cell(i['text'])} ({i['source_id']})" for i in report.missing_evidence] or ["None."]
    return "\n".join(lines) + "\n"


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description="Build and validate the draft knowledge release.")
    parser.add_argument("--extraction", type=Path, default=RESULTS)
    parser.add_argument("--manifest", type=Path, default=MANIFEST)
    parser.add_argument("--out", type=Path, default=DRAFT)
    parser.add_argument("--report", type=Path, default=REPORT)
    args = parser.parse_args(argv)

    release, report = resolve(json.loads(args.extraction.read_text()), load_manifest(args.manifest),
                              registered_tools())
    args.report.parent.mkdir(parents=True, exist_ok=True)
    args.report.write_text(render(report))
    if release is not None:
        args.out.parent.mkdir(parents=True, exist_ok=True)
        args.out.write_text(release.model_dump_json(indent=2) + "\n")
    print(render(report).split("\n## ")[0])
    return 0 if report.valid else 1


if __name__ == "__main__":
    sys.exit(main())
