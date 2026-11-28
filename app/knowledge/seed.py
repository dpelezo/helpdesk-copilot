"""Download the seed dataset, group its rows into canonical issues, and profile it.

The seed is raw material, not ground truth (ADR 0005). It is downloaded at a
pinned revision and never committed. This module turns its 10,500 rows into
canonical issues and writes a report of what the data does and does not hold.

Run it with:

    python -m app.knowledge.seed            # download, profile, write the report
    python -m app.knowledge.seed --offline  # reuse the file already downloaded
"""

from __future__ import annotations

import argparse
import csv
import hashlib
import json
import re
import sys
from collections import Counter, defaultdict
from dataclasses import asdict, dataclass, field
from pathlib import Path
from urllib.parse import quote

import httpx

SEED_REPO = "UmerSajid/IT-Troubleshooting-Dataset"
# Pinned revision (commit sha on the Hugging Face Hub, last modified 2026-09-14).
SEED_REVISION = "96e942456ec39db167718507987981d340f5a624"
# The file name on the Hub really has a space before ".csv".
SEED_FILE = "Enhanced_IT_Troubleshooting_Dataset_with_Links .csv"

EXPECTED_COLUMNS = (
    "ID",
    "Category",
    "Issue",
    "Symptoms",
    "Solution Steps",
    "Severity",
    "Estimated Resolution Time",
    "Common Causes",
    "Keywords",
    "Urdu Solution",
    "Documentation Link",
)
# Columns whose values describe an issue. Rows of one canonical issue must agree on them.
CONTENT_COLUMNS = ("Symptoms", "Solution Steps", "Keywords", "Documentation Link")
# Columns that vary per row and are profiled, not kept as knowledge.
PER_ROW_COLUMNS = ("Severity", "Estimated Resolution Time")
# Dropped on load: it repeats the English solution text (ADR 0005).
DROPPED_COLUMNS = ("Urdu Solution",)

DATA_DIR = Path("data/seed")
REPORT_PATH = Path("reports/seed_profile.md")

_VARIANT = re.compile(r"\s*-\s*Variant\s+\d+\s*$", re.IGNORECASE)
_SPACES = re.compile(r"\s+")
_NOT_SLUG = re.compile(r"[^a-z0-9]+")


class SeedError(Exception):
    """The downloaded seed does not look like the pinned dataset."""


def seed_url(revision: str = SEED_REVISION) -> str:
    return f"https://huggingface.co/datasets/{SEED_REPO}/resolve/{revision}/{quote(SEED_FILE)}"


def download(dest: Path, client: httpx.Client | None = None) -> tuple[Path, str]:
    """Download the pinned CSV to ``dest`` and return its path and sha256."""
    dest.parent.mkdir(parents=True, exist_ok=True)
    own = client is None
    http = client or httpx.Client(timeout=60, follow_redirects=True)
    try:
        response = http.get(seed_url())
        response.raise_for_status()
        dest.write_bytes(response.content)
    finally:
        if own:
            http.close()
    return dest, sha256(dest)


def sha256(path: Path) -> str:
    return hashlib.sha256(path.read_bytes()).hexdigest()


def clean(text: str) -> str:
    return _SPACES.sub(" ", text).strip()


def base_issue(issue: str) -> str:
    """'AWS instance not starting - Variant 30' -> 'AWS instance not starting'."""
    return clean(_VARIANT.sub("", issue))


def slug(text: str) -> str:
    return _NOT_SLUG.sub("-", text.lower()).strip("-")


def load_rows(path: Path) -> list[dict[str, str]]:
    """Read the CSV and check it has the expected columns."""
    with path.open(newline="", encoding="utf-8-sig") as handle:
        reader = csv.DictReader(handle)
        columns = tuple(clean(name) for name in reader.fieldnames or ())
        missing = [name for name in EXPECTED_COLUMNS if name not in columns]
        if missing:
            raise SeedError(f"seed is missing columns: {', '.join(missing)}")
        rows = []
        for raw in reader:
            row = {clean(k): clean(v or "") for k, v in raw.items() if k is not None}
            for name in DROPPED_COLUMNS:
                row.pop(name, None)
            rows.append(row)
    return rows


@dataclass
class CanonicalIssue:
    id: str
    category: str
    issue: str
    symptoms: str
    solution_steps: str
    keywords: str
    documentation_link: str
    row_ids: list[str] = field(default_factory=list)
    # Content columns where rows of this issue disagree; empty when they all agree.
    conflicts: dict[str, list[str]] = field(default_factory=dict)

    @property
    def variant_count(self) -> int:
        return len(self.row_ids)


def canonicalize(rows: list[dict[str, str]]) -> list[CanonicalIssue]:
    """Group rows by category and base issue name, keeping lineage to the row ids."""
    groups: dict[tuple[str, str], list[dict[str, str]]] = defaultdict(list)
    for row in rows:
        groups[(row["Category"], base_issue(row["Issue"]))].append(row)

    issues = []
    for (category, issue), members in sorted(groups.items()):
        first = members[0]
        conflicts = {}
        for column in CONTENT_COLUMNS:
            values = sorted({m[column] for m in members})
            if len(values) > 1:
                conflicts[column] = values
        issues.append(
            CanonicalIssue(
                id=f"{slug(category)}--{slug(issue)}",
                category=category,
                issue=issue,
                symptoms=first["Symptoms"],
                solution_steps=first["Solution Steps"],
                keywords=first["Keywords"],
                documentation_link=first["Documentation Link"],
                row_ids=[m["ID"] for m in members],
                conflicts=conflicts,
            )
        )
    return issues


@dataclass
class Profile:
    rows: int
    canonical_issues: int
    duplicate_ratio: float
    per_category: dict[str, int]
    distinct_values: dict[str, int]
    constant_columns: list[str]
    issues_with_conflicts: int
    issues_with_varying_per_row_columns: dict[str, int]


def profile(rows: list[dict[str, str]], issues: list[CanonicalIssue]) -> Profile:
    columns = [c for c in EXPECTED_COLUMNS if c not in DROPPED_COLUMNS and c != "ID"]
    distinct = {c: len({r[c] for r in rows}) for c in columns}
    by_id = {r["ID"]: r for r in rows}
    varying = {}
    for column in PER_ROW_COLUMNS:
        varying[column] = sum(
            1 for issue in issues if len({by_id[i][column] for i in issue.row_ids}) > 1
        )
    return Profile(
        rows=len(rows),
        canonical_issues=len(issues),
        duplicate_ratio=round(1 - len(issues) / len(rows), 4) if rows else 0.0,
        per_category=dict(sorted(Counter(i.category for i in issues).items())),
        distinct_values=distinct,
        constant_columns=[c for c, n in distinct.items() if n == 1],
        issues_with_conflicts=sum(1 for i in issues if i.conflicts),
        issues_with_varying_per_row_columns=varying,
    )


def render_report(p: Profile, issues: list[CanonicalIssue], digest: str) -> str:
    lines = [
        "# Seed dataset profile",
        "",
        f"Source: [{SEED_REPO}](https://huggingface.co/datasets/{SEED_REPO}), "
        f"revision `{SEED_REVISION[:12]}`, file sha256 `{digest[:16]}`. "
        "Generated by `python -m app.knowledge.seed`; the data itself is not committed (ADR 0005).",
        "",
        f"**{p.rows:,} rows collapse to {p.canonical_issues} canonical issues** "
        f"({p.duplicate_ratio:.1%} of rows are variants of another row).",
        "",
        "## What the columns hold",
        "",
        "| Column | Distinct values | Note |",
        "| --- | ---: | --- |",
    ]
    for column, count in p.distinct_values.items():
        note = ""
        if column in p.constant_columns:
            note = "Same value on every row: carries no information"
        elif column in PER_ROW_COLUMNS:
            n = p.issues_with_varying_per_row_columns[column]
            note = f"Varies inside {n} of {p.canonical_issues} issues: random per row, not knowledge"
        lines.append(f"| {column} | {count:,} | {note} |")
    lines += [
        "",
        f"Rows of the same issue disagree on symptoms, solution, keywords or link in "
        f"{p.issues_with_conflicts} of {p.canonical_issues} issues.",
        "",
        "## Canonical issues by category",
        "",
        "| Category | Issues |",
        "| --- | ---: |",
    ]
    lines += [f"| {c} | {n} |" for c, n in p.per_category.items()]
    lines += [
        "",
        "## Canonical issues",
        "",
        "| Id | Category | Issue | Rows |",
        "| --- | --- | --- | ---: |",
    ]
    lines += [f"| `{i.id}` | {i.category} | {i.issue} | {i.variant_count} |" for i in issues]
    lines += [
        "",
        "## What this means for the build",
        "",
        "- The seed names issues, symptoms and a generic fix. It holds no causes, so causes, "
        "diagnostics and runbooks come from vendor documentation and hand-written gold procedures.",
        "- Severity and resolution time are dropped: they change between copies of the same issue.",
        "- Accuracy is reported per canonical issue, never per row, and held-out tickets are split "
        "by canonical issue.",
        "",
    ]
    return "\n".join(lines)


def run(offline: bool, data_dir: Path = DATA_DIR, report: Path = REPORT_PATH) -> Profile:
    csv_path = data_dir / "seed.csv"
    if offline:
        if not csv_path.exists():
            raise SeedError(f"{csv_path} not found; run once without --offline")
        digest = sha256(csv_path)
    else:
        csv_path, digest = download(csv_path)
    rows = load_rows(csv_path)
    issues = canonicalize(rows)
    result = profile(rows, issues)
    (data_dir / "canonical_issues.json").write_text(
        json.dumps([asdict(i) | {"variant_count": i.variant_count} for i in issues], indent=2)
    )
    report.parent.mkdir(parents=True, exist_ok=True)
    report.write_text(render_report(result, issues, digest))
    return result


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    parser.add_argument("--offline", action="store_true", help="reuse the downloaded file")
    args = parser.parse_args(argv)
    try:
        result = run(args.offline)
    except (SeedError, httpx.HTTPError) as error:
        print(f"seed: {error}", file=sys.stderr)
        return 1
    print(f"{result.rows} rows -> {result.canonical_issues} canonical issues; report: {REPORT_PATH}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
