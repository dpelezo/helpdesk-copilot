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
import hashlib
import json
import re
import sys
from dataclasses import asdict, dataclass, field
from datetime import date
from pathlib import Path
from typing import Any, Literal, Protocol

from pydantic import BaseModel, ConfigDict, Field, ValidationError

from app.evals.extraction_score import JUDGE_CACHE, ExtractedCause, LLMJudge, evidence_found, score_extraction
from app.knowledge.policy import HANDBOOK
from app.knowledge.sources import CACHE, Source, load_manifest
from app.llm import BudgetExceeded, ChatClient, LLMError, Reply
from app.mock.faults import ISSUES

MAX_SOURCE_CHARS = 40_000
# Room for reasoning, which Telnyx cannot switch off (ADR 0003, #9). 8,192 cut off 5 of 22
# calls in the first run, including the two most relevant printer and phishing articles.
MAX_TOKENS = 16384
MAX_ATTEMPTS = 2  # a cut-off reply is retried once
RESULTS = Path("evals/results/extraction.json")
# Successful replies, keyed by everything that shapes them, so a re-run only pays for what
# changed. Kept between workflow runs with actions/cache; never committed (it holds quotes).
REPLY_CACHE = Path("data/cache/extraction")

Kind = Literal["symptom", "cause", "diagnostic", "solution"]
KINDS: tuple[Kind, ...] = ("symptom", "cause", "diagnostic", "solution")

SYSTEM = (
    "You extract IT troubleshooting knowledge from documentation. Use only what the text says. "
    "For the issue you are given, return JSON with four lists: symptoms (what the user sees), "
    "causes (why the problem happens, not how to fix it), diagnostics (checks that tell causes "
    "apart), and solutions (how to fix a cause). Every item has 'text' (one sentence in your own "
    "words) and 'evidence' (an exact quote of at most 30 words copied character for character "
    "from the documentation, supporting the item). Each item names exactly one thing: when the "
    "text lists several causes in one sentence (for example 'low paper, an open cover or a paper "
    "jam'), make a separate item for each, each with its own quote. Leave a list empty rather "
    "than guess. Give at most 12 items per list. Reply with the JSON object only."
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
    cached: bool = False
    stopped_by_budget: bool = False
    attempts: int = 0


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


def cache_key(model: str, messages: list[dict[str, Any]]) -> str:
    """Model, prompt, source text and token limit: anything that can change the reply."""
    blob = json.dumps({"model": model, "messages": messages, "max_tokens": MAX_TOKENS}, sort_keys=True)
    return hashlib.sha256(blob.encode()).hexdigest()


def extract_one(model: Model, issue: str, source: Source, text: str,
                reply_cache: Path | None = None) -> tuple[list[Extracted], Call]:
    call = Call(issue, source.id)
    messages = [
        {"role": "system", "content": SYSTEM},
        {"role": "user", "content": (
            f"Issue: {ISSUES[issue]}\nSource: {source.title or source.url}\n\n"
            f"Documentation:\n{text[:MAX_SOURCE_CHARS]}"
        )},
    ]
    cached_path = reply_cache / f"{cache_key(model.model, messages)}.json" if reply_cache else None
    if cached_path and cached_path.exists():
        saved = json.loads(cached_path.read_text())
        reply = Reply({"role": "assistant", "content": saved["content"]}, saved["usage"])
        call.cached = True
        result = parse(reply.message.get("content"))
    else:
        # A reply cut off at the token limit is reasoning that never converged; the same call
        # usually finishes on a second try (run 4b: 4 of 5). Retry those once, never other errors.
        while True:
            call.attempts += 1
            try:
                reply = model.complete(messages, max_tokens=MAX_TOKENS)
            except LLMError as error:
                call.error = str(error)
                call.stopped_by_budget = isinstance(error, BudgetExceeded)
                return [], call
            call.prompt_tokens += int(reply.usage.get("prompt_tokens") or 0)
            call.completion_tokens += int(reply.usage.get("completion_tokens") or 0)
            call.seconds += reply.seconds
            result = parse(reply.message.get("content"))
            if result is not None or not reply.truncated or call.attempts >= MAX_ATTEMPTS:
                break
    if call.cached:
        call.prompt_tokens = int(reply.usage.get("prompt_tokens") or 0)
        call.completion_tokens = int(reply.usage.get("completion_tokens") or 0)
    if result is None:
        call.error = "reply cut off at the token limit" if reply.truncated else "no valid JSON in the reply"
        return [], call
    if cached_path and not call.cached:  # only good replies are cached, so failures are retried
        cached_path.parent.mkdir(parents=True, exist_ok=True)
        cached_path.write_text(json.dumps({"content": reply.message.get("content"), "usage": reply.usage}))
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


def extract_all(model: Model, sources: list[Source], cache: Path = CACHE, today: str | None = None,
                reply_cache: Path | None = None) -> Run:
    run = Run(model.model, today or date.today().isoformat())
    for source in sources:
        if source.status != "ok":
            continue
        text = source_text(source, cache)
        for issue in source.relevant_to:
            items, call = extract_one(model, issue, source, text, reply_cache)
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
        f"{len(run.calls)} calls ({sum(c.cached for c in run.calls)} from cache, "
        f"{sum(c.attempts > 1 for c in run.calls)} retried), {len(run.items)} items extracted, {len(run.kept)} kept "
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
    parser = argparse.ArgumentParser(description="Extract knowledge from the sources and score it.")
    parser.add_argument("--no-cache", action="store_true",
                        help="call the model for every source (for run-to-run variance), ignoring cached replies")
    args = parser.parse_args(argv)
    try:
        # Two clients, so extraction and judging each have their own token count and budget.
        extractor = ChatClient.from_env()
        judge_client = ChatClient.from_env()
        run = extract_all(extractor, load_manifest(), reply_cache=None if args.no_cache else REPLY_CACHE)
        judge = LLMJudge(judge_client, cache=None if args.no_cache else JUDGE_CACHE)
        texts = {s.id: source_text(s) for s in load_manifest() if s.status == "ok"}
        score = asdict(score_extraction(causes_for_scoring(run), judge, texts.__getitem__))
    except (LLMError, FileNotFoundError) as error:
        print(f"extract: {error}", file=sys.stderr)
        return 1
    score["judge_unparsed"] = judge.unparsed
    score["judge_cached"] = judge.cached
    tokens = {"extraction": extractor.usage_report(), "judge": judge_client.usage_report()}
    # A judge that ran out of budget answered "no" to the rest, so the score would be wrong.
    complete = not judge_client.exhausted and not any(c.stopped_by_budget for c in run.calls)
    RESULTS.parent.mkdir(parents=True, exist_ok=True)
    RESULTS.write_text(json.dumps({
        "model": run.model, "date": run.date, "complete": complete, "tokens": tokens, "score": score,
        "calls": [asdict(c) for c in run.calls], "items": [asdict(i) for i in run.items],
    }, indent=2) + "\n")
    print(summary(run, score))
    print(f"\nTokens: extraction {extractor.used:,} in {extractor.calls} calls; "
          f"judge {judge_client.used:,} in {judge_client.calls} calls")
    if not complete:
        print("INCOMPLETE: a token budget ran out, so this score is not valid. Raise LLM_TOKEN_BUDGET.")
        return 1
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
