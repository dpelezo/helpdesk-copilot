"""Score knowledge extraction against the cause catalog (issue #26).

Extraction reads real vendor documentation, so it describes causes in its own
words. A separate judge decides whether an extracted cause is the same thing as a
catalog cause (``app/mock/faults.py``); extraction never labels its own output.
The judge answers one narrow yes/no question per pair, and is trusted only after
it agrees with the hand labels in ``evals/extraction/judge_labels.jsonl``.

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
    "You compare two descriptions of why an IT problem happens. Answer only yes or no. "
    "Answer yes when both name the same underlying cause, even in different words or with "
    "different detail. Answer no when they name different causes, even if they concern the "
    "same symptom or the same product."
)


# ── Inputs ─────────────────────────────────────────────────────────────


@dataclass(frozen=True)
class ExtractedCause:
    issue: str
    text: str
    evidence: str
    source_id: str


class Judge(Protocol):
    def same_cause(self, extracted: str, reference: str) -> bool: ...


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
        hits = []
        for cause in causes_for(item.issue):
            calls += 1
            if judge.same_cause(item.text, cause.summary):
                hits.append(cause.id)
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


def parse_yes_no(text: str | None) -> bool | None:
    """yes -> True, no -> False, anything else -> None. Looks at the last word given."""
    words = re.findall(r"\b(yes|no)\b", (text or "").lower())
    return {"yes": True, "no": False}[words[-1]] if words else None


@dataclass
class LLMJudge:
    model: Model
    unparsed: int = 0

    def same_cause(self, extracted: str, reference: str) -> bool:
        messages = [
            {"role": "system", "content": JUDGE_SYSTEM},
            {"role": "user", "content": f"A: {extracted}\nB: {reference}\nSame cause? Answer yes or no."},
        ]
        try:
            reply = self.model.complete(messages, max_tokens=JUDGE_MAX_TOKENS)
        except LLMError:
            self.unparsed += 1
            return False
        answer = parse_yes_no(reply.message.get("content"))
        if answer is None:
            self.unparsed += 1
            return False
        return answer


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
    by_id = {c.id: c for c in CAUSES}
    false_yes, false_no = [], []
    for lab in labels:
        said = judge.same_cause(lab.extracted, by_id[lab.cause].summary)
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
        judge = LLMJudge(ChatClient.from_env())
        result = judge_agreement(judge, load_labels())
    except LLMError as error:
        print(f"judge check: {error}", file=sys.stderr)
        return 1
    print(f"Judge agreement: {result.agreement}% on {result.labels} labels "
          f"(needs {MIN_JUDGE_AGREEMENT}%); unparsed replies: {judge.unparsed}")
    for wrong in result.false_yes:
        print(f"  said same, label says different: {wrong}")
    for wrong in result.false_no:
        print(f"  said different, label says same: {wrong}")
    print("TRUSTED" if result.trusted else "NOT TRUSTED")
    return 0 if result.trusted else 1


if __name__ == "__main__":
    raise SystemExit(main())
