"""Score knowledge extraction against the cause catalog (issue #26).

Extraction reads real vendor documentation, so it describes causes in its own
words. A separate judge decides whether an extracted cause is the same thing as a
catalog cause (``app/mock/faults.py``); extraction never labels its own output.
The judge sees one extracted cause and the 2 to 4 catalog causes of its issue, and
names the ones that are the same cause (usually one, or none): one call per
extracted cause, not one per pair. Answers are cached, so the same question is
never paid for twice. It is trusted only after it agrees with the hand labels in
``evals/extraction/judge_labels.jsonl``, each judged with all of its issue's causes
in view, as in real use.

Metrics:

- **Recall**: catalog causes described by at least one extracted cause, per issue
  and overall. Real docs may not mention every planted cause; the misses say which.
- **Extra causes**: extracted causes that match nothing in the catalog. Counted, not
  penalised: documentation lists more causes than the mock world plants.
- **Evidence validity**: extracted items whose evidence span appears in the source
  text (case and whitespace ignored). A span that cannot be found is not evidence.

This is eval ground truth: change it only in its own pull request (AGENTS.md).
"""

from __future__ import annotations

import hashlib
import json
import re
from collections.abc import Callable
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any, Protocol

from app.llm import LLMError, Reply
from app.mock.faults import CAUSES, ISSUES, causes_for

LABELS = Path("evals/extraction/judge_labels.jsonl")
MIN_JUDGE_AGREEMENT = 90.0
JUDGE_MAX_TOKENS = 2048  # reasoning cannot be switched off at Telnyx (ADR 0003)

JUDGE_SYSTEM = (
    "You compare descriptions of why an IT problem happens. You get one description A and a "
    "numbered list of known causes. Name every known cause that is the same underlying cause as "
    "A, even in different words or with different detail. A cause that only shares the symptom or "
    "the product is not the same. Usually one matches, or none. End your reply with a final line "
    "holding only the matching numbers separated by commas, or the word none."
)
JUDGE_CACHE = Path("data/cache/judge")


# ── Inputs ─────────────────────────────────────────────────────────────


@dataclass(frozen=True)
class ExtractedCause:
    issue: str
    text: str
    evidence: str
    source_id: str


class Judge(Protocol):
    def match(self, extracted: str, references: list[str]) -> list[int]:
        """Indexes into ``references`` of the causes that are the same as ``extracted``."""
        ...


# ── Evidence ───────────────────────────────────────────────────────────


def _norm(text: str) -> str:
    text = text.lower().replace("’", "'").replace("‘", "'").replace("“", '"').replace("”", '"')
    return re.sub(r"\s+", " ", text).strip()


def evidence_found(evidence: str, source_text: str) -> bool:
    """True when the span appears in the source, ignoring case, whitespace and curly quotes."""
    span = _norm(evidence)
    return bool(span) and span in _norm(source_text)


# ── Scoring ────────────────────────────────────────────────────────────


@dataclass
class ExtractionScore:
    extracted: int
    recall: float
    per_issue_recall: dict[str, float]
    matched: dict[str, list[str]]  # catalog cause id -> extracted texts that describe it
    missed: list[str]  # catalog cause ids no extracted cause describes
    extra_causes: int
    evidence_valid: float
    ambiguous: int  # extracted causes the judge matched to more than one catalog cause
    judge_calls: int = 0
    per_issue_extracted: dict[str, int] = field(default_factory=dict)


def _pct(part: int, whole: int) -> float:
    return round(100 * part / whole, 1) if whole else 0.0


def score_extraction(
    extracted: list[ExtractedCause],
    judge: Judge,
    source_text: Callable[[str], str],
    issues: list[str] | None = None,
) -> ExtractionScore:
    """Score extracted causes for ``issues`` (default: all 13) against the catalog."""
    issues = issues or list(ISSUES)
    unknown = {e.issue for e in extracted} - set(ISSUES)
    if unknown:
        raise ValueError(f"extracted causes for unknown issues: {sorted(unknown)}")

    matched: dict[str, list[str]] = {c.id: [] for c in CAUSES if c.issue in issues}
    extra = ambiguous = calls = valid = 0
    considered = [e for e in extracted if e.issue in issues]
    for item in considered:
        if evidence_found(item.evidence, source_text(item.source_id)):
            valid += 1
        candidates = causes_for(item.issue)
        calls += 1
        hits = [candidates[i].id for i in judge.match(item.text, [c.summary for c in candidates])]
        if not hits:
            extra += 1
        if len(hits) > 1:
            ambiguous += 1
        for cause_id in hits:
            matched[cause_id].append(item.text)

    per_issue = {}
    for issue in issues:
        ids = [c.id for c in causes_for(issue)]
        per_issue[issue] = _pct(sum(bool(matched[i]) for i in ids), len(ids))
    return ExtractionScore(
        extracted=len(considered),
        recall=_pct(sum(bool(v) for v in matched.values()), len(matched)),
        per_issue_recall=per_issue,
        matched={k: v for k, v in matched.items() if v},
        missed=sorted(k for k, v in matched.items() if not v),
        extra_causes=extra,
        evidence_valid=_pct(valid, len(considered)),
        ambiguous=ambiguous,
        judge_calls=calls,
        per_issue_extracted={i: sum(e.issue == i for e in considered) for i in issues},
    )


# ── The judge and its check ────────────────────────────────────────────


class Model(Protocol):
    model: str

    def complete(self, messages: list[dict[str, Any]], tools: list[dict[str, Any]] | None = None, *,
                 max_tokens: int = ...) -> Reply: ...


def parse_choice(text: str | None, count: int) -> list[int] | None:
    """The judge's final line: '2' or '1, 3' -> 0-based indexes; 'none' -> []; else None."""
    lines = [line.strip().lower() for line in (text or "").strip().splitlines() if line.strip()]
    if not lines:
        return None
    last = lines[-1].strip(" .*`")
    if last == "none":
        return []
    if not re.fullmatch(r"\d+(\s*,\s*\d+)*", last):
        return None
    picked = sorted({int(n) - 1 for n in re.findall(r"\d+", last)})
    return picked if all(0 <= i < count for i in picked) else None


@dataclass
class LLMJudge:
    model: Model
    cache: Path | None = None
    unparsed: int = 0
    cached: int = 0

    def _key(self, extracted: str, references: list[str]) -> str:
        blob = json.dumps({"model": self.model.model, "system": JUDGE_SYSTEM, "a": extracted, "refs": references})
        return hashlib.sha256(blob.encode()).hexdigest()

    def match(self, extracted: str, references: list[str]) -> list[int]:
        path = self.cache / f"{self._key(extracted, references)}.json" if self.cache else None
        if path and path.exists():
            self.cached += 1
            return json.loads(path.read_text())
        listed = "\n".join(f"{n}. {ref}" for n, ref in enumerate(references, start=1))
        messages = [
            {"role": "system", "content": JUDGE_SYSTEM},
            {"role": "user", "content": f"A: {extracted}\n\nKnown causes:\n{listed}"},
        ]
        try:
            reply = self.model.complete(messages, max_tokens=JUDGE_MAX_TOKENS)
        except LLMError:
            self.unparsed += 1
            return []
        picked = parse_choice(reply.message.get("content"), len(references))
        if picked is None:
            self.unparsed += 1
            return []
        if path:  # only clean answers are cached
            path.parent.mkdir(parents=True, exist_ok=True)
            path.write_text(json.dumps(picked))
        return picked


@dataclass(frozen=True)
class Label:
    extracted: str
    cause: str  # a catalog cause id
    same: bool
    note: str = ""


def load_labels(path: Path = LABELS) -> list[Label]:
    labels = [Label(**json.loads(line)) for line in path.read_text().splitlines() if line.strip()]
    ids = {c.id for c in CAUSES}
    unknown = [lab.cause for lab in labels if lab.cause not in ids]
    if unknown:
        raise ValueError(f"labels name unknown causes: {unknown}")
    return labels


@dataclass
class Agreement:
    labels: int
    agreement: float
    false_yes: list[str]  # judge said same, label says different
    false_no: list[str]
    trusted: bool


def judge_agreement(judge: Judge, labels: list[Label]) -> Agreement:
    """Each label is judged as in real use: the extracted text against all its issue's causes."""
    by_id = {c.id: c for c in CAUSES}
    answers: dict[tuple[str, str], list[str]] = {}
    false_yes, false_no = [], []
    for lab in labels:
        issue = by_id[lab.cause].issue
        if (lab.extracted, issue) not in answers:
            candidates = causes_for(issue)
            picked = judge.match(lab.extracted, [c.summary for c in candidates])
            answers[(lab.extracted, issue)] = [candidates[i].id for i in picked]
        said = lab.cause in answers[(lab.extracted, issue)]
        if said and not lab.same:
            false_yes.append(f"{lab.cause}: {lab.extracted}")
        elif lab.same and not said:
            false_no.append(f"{lab.cause}: {lab.extracted}")
    agree = _pct(len(labels) - len(false_yes) - len(false_no), len(labels))
    return Agreement(len(labels), agree, false_yes, false_no, agree >= MIN_JUDGE_AGREEMENT)


def main(argv: list[str] | None = None) -> int:
    """Check the live judge against the hand labels: ``python -m app.evals.extraction_score``."""
    import argparse
    import sys

    from app.llm import ChatClient

    argparse.ArgumentParser(description="Check the cause-matching judge against the hand labels.").parse_args(argv)
    try:
        client = ChatClient.from_env()
        judge = LLMJudge(client)
        result = judge_agreement(judge, load_labels())
    except LLMError as error:
        print(f"judge check: {error}", file=sys.stderr)
        return 1
    print(f"Judge agreement: {result.agreement}% on {result.labels} labels "
          f"(needs {MIN_JUDGE_AGREEMENT}%); unparsed replies: {judge.unparsed}; "
          f"tokens: {client.used:,} in {client.calls} calls")
    for wrong in result.false_yes:
        print(f"  said same, label says different: {wrong}")
    for wrong in result.false_no:
        print(f"  said different, label says same: {wrong}")
    print("TRUSTED" if result.trusted else "NOT TRUSTED")
    return 0 if result.trusted else 1


if __name__ == "__main__":
    raise SystemExit(main())
