"""Workflow A, extraction (issue #26): typed knowledge with evidence from each source.

For each usable source and each issue it is about, one model call reads the
source text and returns symptoms, possible causes, diagnostics and solutions for
that issue, each with an evidence span copied from the text. Output is validated;
an item whose evidence cannot be found in the source is rejected, not repaired.

Every item keeps its source id and evidence span, which is what gets committed:
short quotes with their URL, never the page (ADR 0005). The extracted causes are
then scored against the cause catalog by the trusted judge
(``app/evals/extraction_score.py``).

    python -m app.knowledge.sources   # fills the local text cache first
    python -m app.knowledge.extract   # extract, score, write evals/results/extraction.json
"""

from __future__ import annotations

import argparse
import json
import re
import sys
from dataclasses import asdict, dataclass, field
from datetime import date
from pathlib import Path
from typing import Any, Literal, Protocol

from pydantic import BaseModel, ConfigDict, Field, ValidationError

from app.evals.extraction_score import ExtractedCause, LLMJudge, evidence_found, score_extraction
from app.knowledge.policy import HANDBOOK
from app.knowledge.sources import CACHE, Source, load_manifest
from app.llm import ChatClient, LLMError, Reply
from app.mock.faults import ISSUES

MAX_SOURCE_CHARS = 40_000
MAX_TOKENS = 8192  # room for reasoning, which Telnyx cannot switch off (ADR 0003, #9)
RESULTS = Path("evals/results/extraction.json")

Kind = Literal["symptom", "cause", "diagnostic", "solution"]
KINDS: tuple[Kind, ...] = ("symptom", "cause", "diagnostic", "solution")

SYSTEM = (
    "You extract IT troubleshooting knowledge from documentation. Use only what the text says. "
    "For the issue you are given, return JSON with four lists: symptoms (what the user sees), "
    "causes (why the problem happens, not how to fix it), diagnostics (checks that tell causes "
    "apart), and solutions (how to fix a cause). Every item has 'text' (one sentence in your own "
    "words) and 'evidence' (an exact quote of at most 30 words copied character for character "
    "from the documentation, supporting the item). Leave a list empty rather than guess. Give at "
    "most 8 items per list. Reply with the JSON object only."
)

_JSON = re.compile(r"\{.*\}", re.DOTALL)


class Item(BaseModel):
    model_config = ConfigDict(extra="ignore")

    text: str = Field(min_length=1)
    evidence: str = Field(min_length=1)


class Extraction(BaseModel):
    model_config = ConfigDict(extra="ignore")

    symptoms: list[Item] = Field(default_factory=list)
    causes: list[Item] = Field(default_factory=list)
    diagnostics: list[Item] = Field(default_factory=list)
    solutions: list[Item] = Field(default_factory=list)


class Model(Protocol):
    model: str

    def complete(self, messages: list[dict[str, Any]], tools: list[dict[str, Any]] | None = None, *,
                 max_tokens: int = ...) -> Reply: ...


@dataclass
class Extracted:
    issue: str
    source_id: str
    kind: Kind
    text: str
    evidence: str
    evidence_ok: bool


@dataclass
class Call:
    issue: str
    source_id: str
    items: int = 0
    rejected: int = 0
    error: str | None = None
    prompt_tokens: int = 0
    completion_tokens: int = 0
    seconds: float = 0.0


def parse(text: str | None) -> Extraction | None:
    match = _JSON.search(text or "")
    if not match:
        return None
    try:
        return Extraction.model_validate(json.loads(match.group(0)))
    except (ValueError, ValidationError):
        return None


def source_text(source: Source, cache: Path = CACHE) -> str:
    if source.origin == "local":
        return HANDBOOK.read_text()
    return (cache / f"{source.id}.txt").read_text()


def extract_one(model: Model, issue: str, source: Source, text: str) -> tuple[list[Extracted], Call]:
    call = Call(issue, source.id)
    messages = [
        {"role": "system", "content": SYSTEM},
        {"role": "user", "content": (
            f"Issue: {ISSUES[issue]}\nSource: {source.title or source.url}\n\n"
            f"Documentation:\n{text[:MAX_SOURCE_CHARS]}"
        )},
    ]
    try:
        reply = model.complete(messages, max_tokens=MAX_TOKENS)
    except LLMError as error:
        call.error = str(error)
        return [], call
    call.prompt_tokens = int(reply.usage.get("prompt_tokens") or 0)
    call.completion_tokens = int(reply.usage.get("completion_tokens") or 0)
    call.seconds = reply.seconds
    result = parse(reply.message.get("content"))
    if result is None:
        call.error = "reply cut off at the token limit" if reply.truncated else "no valid JSON in the reply"
        return [], call
    items = []
    for kind, values in zip(KINDS, (result.symptoms, result.causes, result.diagnostics, result.solutions),
                            strict=True):
        for value in values:
            ok = evidence_found(value.evidence, text)
            items.append(Extracted(issue, source.id, kind, value.text.strip(), value.evidence.strip(), ok))
    call.items = sum(i.evidence_ok for i in items)
    call.rejected = sum(not i.evidence_ok for i in items)
    return items, call


@dataclass
class Run:
    model: str
    date: str
    items: list[Extracted] = field(default_factory=list)
    calls: list[Call] = field(default_factory=list)

    @property
    def kept(self) -> list[Extracted]:
        """Items whose evidence was found in the source. Rejected items are never published."""
        return [i for i in self.items if i.evidence_ok]


def extract_all(model: Model, sources: list[Source], cache: Path = CACHE, today: str | None = None) -> Run:
    run = Run(model.model, today or date.today().isoformat())
    for source in sources:
        if source.status != "ok":
            continue
        text = source_text(source, cache)
        for issue in source.relevant_to:
            items, call = extract_one(model, issue, source, text)
            run.items += items
            run.calls.append(call)
    return run


def causes_for_scoring(run: Run) -> list[ExtractedCause]:
    """Every extracted cause, kept or rejected: evidence validity is part of the score."""
    return [ExtractedCause(i.issue, i.text, i.evidence, i.source_id) for i in run.items if i.kind == "cause"]


def summary(run: Run, score: dict[str, Any]) -> str:
    kinds = {k: sum(i.kind == k for i in run.kept) for k in KINDS}
    lines = [
        f"### Extraction ({run.model}, {run.date})",
        "",
        f"{len(run.calls)} calls, {len(run.items)} items extracted, {len(run.kept)} kept "
        f"({len(run.items) - len(run.kept)} rejected: evidence not found in the source), "
        f"{sum(c.error is not None for c in run.calls)} failed calls.",
        f"Kept by kind: {', '.join(f'{n} {k}s' for k, n in kinds.items())}.",
        "",
        "| Cause recall | Extra causes | Evidence valid (causes) | Ambiguous |",
        "| ---: | ---: | ---: | ---: |",
        f"| {score['recall']}% | {score['extra_causes']} | {score['evidence_valid']}% | {score['ambiguous']} |",
        "",
        "| Issue | Recall | Causes extracted |",
        "| --- | ---: | ---: |",
    ]
    lines += [f"| `{i}` | {r}% | {score['per_issue_extracted'][i]} |" for i, r in score["per_issue_recall"].items()]
    lines += ["", "Catalog causes no source describes: " + (", ".join(f"`{m}`" for m in score["missed"]) or "none")]
    return "\n".join(lines)


def main(argv: list[str] | None = None) -> int:
    argparse.ArgumentParser(description="Extract knowledge from the sources and score it.").parse_args(argv)
    try:
        client = ChatClient.from_env()
        run = extract_all(client, load_manifest())
        judge = LLMJudge(client)
        texts = {s.id: source_text(s) for s in load_manifest() if s.status == "ok"}
        score = asdict(score_extraction(causes_for_scoring(run), judge, texts.__getitem__))
    except (LLMError, FileNotFoundError) as error:
        print(f"extract: {error}", file=sys.stderr)
        return 1
    score["judge_unparsed"] = judge.unparsed
    RESULTS.parent.mkdir(parents=True, exist_ok=True)
    RESULTS.write_text(json.dumps({
        "model": run.model, "date": run.date, "score": score,
        "calls": [asdict(c) for c in run.calls], "items": [asdict(i) for i in run.items],
    }, indent=2) + "\n")
    print(summary(run, score))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
