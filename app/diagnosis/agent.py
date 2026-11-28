"""The diagnosis loop (issue #50, ADR 0004): test hypotheses against the systems, then decide.

A LangGraph state graph with four nodes:

    start ─► plan ─► check ─► plan ─► … ─► decide ─► (check ─► decide) ─► end
               └──────────────────────────► decide            (no check left, or budget spent)
    start ─► end                                              (no hypothesis: escalate, no model call)

- **plan** (no model): the untried check linked by ``TESTS_FOR`` to the most hypotheses,
  first-listed on a tie. Planning stops at ``CHECK_BUDGET`` checks or when no linked check is left.
- **check**: runs that read-only tool for the ticket's employee through the MCP servers.
- **decide** (one model call): the message, the hypotheses, the handbook sections and every
  check result; the model names a cause or ``escalate``, says why, drafts the reply, and may
  ask for one more check while budget is left (at most ``MAX_DECISIONS`` calls).

Everything the model reads is in the state and the checkpoint, so a run can be inspected step
by step. The graph reads a published release; it never writes knowledge (ADR 0001), and the
account-changing tools are not reachable from it (#51).

Settings were fixed before any eval run: ``CHECK_BUDGET = 4``, ``MAX_DECISIONS = 2``.
"""

from __future__ import annotations

import asyncio
import json
import re
from dataclasses import asdict, dataclass, field
from typing import Any, Literal, TypedDict

from langgraph.checkpoint.memory import InMemorySaver
from langgraph.graph import END, START, StateGraph

from app.diagnosis.checks import CheckRunner
from app.diagnosis.hypotheses import Hypothesis
from app.llm import LLMError
from app.mock.faults import BY_ID, ESCALATE

CHECK_BUDGET = 4
MAX_DECISIONS = 2
MAX_TOKENS = 4096  # the baseline's limit: reasoning cannot be switched off (ADR 0003)

SYSTEM = (
    "You are an IT helpdesk assistant for Kestrel Ridge Logistics. You diagnose an employee's "
    "problem. You get their message, the possible causes (each with an id in parentheses, its fix "
    "and the checks that reveal it), relevant handbook sections, and the results of read-only "
    "checks already run on the company's systems for this employee. A check result that is not "
    "what a healthy record looks like (locked, expired, out of date, degraded, full, failing) is "
    "evidence for the causes that check reveals. Name the cause the evidence supports. Reply with "
    "JSON only, no other text: "
    '{"cause": "<a cause id from the list, or escalate>", "reason": "<one sentence citing the '
    'check results>", "reply": "<two or three sentences to the employee>", '
    '"next_check": "<one tool from the allowed list, or null>"}. '
    'Ask for a next_check only if the evidence cannot yet separate the causes and a check is '
    'allowed. Use "escalate" only when no listed cause fits the evidence.'
)

_JSON = re.compile(r"\{.*\}", re.DOTALL)
Stop = Literal["supported", "escalated", "budget", "no_hypotheses", "invalid", "error"]


class State(TypedDict, total=False):
    ticket: dict[str, str]
    hypotheses: list[dict[str, Any]]
    policy: list[str]
    checks: list[dict[str, Any]]
    next_check: str | None
    decisions: int
    usage: list[dict[str, int]]
    outcome: dict[str, Any]


@dataclass
class Diagnosis:
    ticket: str
    cause: str | None  # a cause id, "escalate", or None when the reply was invalid
    stop: Stop
    reason: str = ""
    reply: str = ""
    fix: str | None = None
    checks: list[dict[str, Any]] = field(default_factory=list)
    hypotheses: list[str] = field(default_factory=list)
    sources: list[str] = field(default_factory=list)
    decisions: int = 0
    prompt_tokens: int = 0
    completion_tokens: int = 0
    error: str | None = None


# ── Pure parts ─────────────────────────────────────────────────────────


def next_check(hypotheses: list[dict[str, Any]], tried: list[str], allowed: set[str]) -> str | None:
    """The untried, allowed check linked to the most hypotheses; the first listed wins a tie."""
    counts: dict[str, int] = {}
    for h in hypotheses:
        for tool in h["checks"]:
            if tool not in tried and tool in allowed:
                counts[tool] = counts.get(tool, 0) + 1
    return max(counts, key=lambda t: counts[t]) if counts else None  # dicts keep first-seen order


def parse(text: str | None) -> dict[str, Any] | None:
    """The model's JSON reply, or None. ``cause`` is kept only if it is a catalog id or escalate."""
    match = _JSON.search(text or "")
    if not match:
        return None
    try:
        data = json.loads(match.group(0))
    except ValueError:
        return None
    if not isinstance(data, dict):
        return None
    cause = data.get("cause")
    cause = cause.strip().strip("()").removeprefix("cause:").strip() if isinstance(cause, str) else None
    data["cause"] = cause if cause == ESCALATE or cause in BY_ID else None
    nxt = data.get("next_check")
    data["next_check"] = nxt.strip() if isinstance(nxt, str) and nxt.strip() and nxt.strip() != "null" else None
    return data


def prompt(state: State, allowed_next: list[str]) -> list[dict[str, str]]:
    t = state["ticket"]
    causes = "\n".join(Hypothesis(**h).line() for h in state["hypotheses"])
    handbook = "\n".join(f"- {p}" for p in state.get("policy", [])) or "(none)"
    checks = "\n".join(f"- {c['tool']}: {json.dumps(c['result'], sort_keys=True, default=str)}"
                       for c in state.get("checks", [])) or "(no checks run)"
    more = (f"You may request one more check from: {', '.join(allowed_next)}." if allowed_next
            else "No more checks can be run: decide now.")
    return [
        {"role": "system", "content": SYSTEM},
        {"role": "user", "content": f"Subject: {t['subject']}\n\n{t['message']}\n\nPossible causes:\n{causes}\n\n"
                                    f"Handbook:\n{handbook}\n\nCheck results for this employee:\n{checks}\n\n{more}"},
    ]


# ── The graph ──────────────────────────────────────────────────────────


class Diagnoser:
    """Builds the graph once; ``diagnose`` runs it for one ticket against one company."""

    def __init__(self, model, checkpointer=None) -> None:
        self.model = model
        self.graph = self._build().compile(checkpointer=checkpointer or InMemorySaver())

    def _build(self) -> StateGraph:
        g = StateGraph(State)
        g.add_node("plan", self._plan)
        g.add_node("check", self._check)
        g.add_node("decide", self._decide)
        g.add_conditional_edges(START, lambda s: "plan" if s["hypotheses"] else END)
        g.add_conditional_edges("plan", lambda s: "check" if s.get("next_check") else "decide")
        g.add_conditional_edges("check", lambda s: "decide" if s.get("decisions") else "plan")
        g.add_conditional_edges("decide", lambda s: "check" if s.get("next_check") else END)
        return g

    @staticmethod
    def _runner(config) -> CheckRunner:
        return config["configurable"]["runner"]

    async def _plan(self, state: State, config) -> dict[str, Any]:
        checks = state.get("checks", [])
        if len(checks) >= CHECK_BUDGET:
            return {"next_check": None}
        allowed = set(await self._runner(config).tools())
        return {"next_check": next_check(state["hypotheses"], [c["tool"] for c in checks], allowed)}

    async def _check(self, state: State, config) -> dict[str, Any]:
        result = await self._runner(config).run(state["next_check"], state["ticket"]["employee_id"])
        return {"checks": [*state.get("checks", []), asdict(result)], "next_check": None}

    async def _decide(self, state: State, config) -> dict[str, Any]:
        checks, decisions = state.get("checks", []), state.get("decisions", 0) + 1
        allowed = set(await self._runner(config).tools())
        tried = {c["tool"] for c in checks}
        can_ask = len(checks) < CHECK_BUDGET and decisions < MAX_DECISIONS
        offer = sorted(allowed - tried) if can_ask else []
        try:
            reply = await asyncio.to_thread(self.model.complete, prompt(state, offer), max_tokens=MAX_TOKENS)
        except LLMError as error:
            return {"decisions": decisions, "next_check": None,
                    "outcome": {"stop": "error", "cause": None, "error": str(error)}}
        usage = [*state.get("usage", []), {"prompt_tokens": int(reply.usage.get("prompt_tokens") or 0),
                                           "completion_tokens": int(reply.usage.get("completion_tokens") or 0)}]
        data = parse(reply.message.get("content"))
        if data is None:
            error = "reply cut off at the token limit" if reply.truncated else "no valid JSON in the reply"
            return {"decisions": decisions, "usage": usage, "next_check": None,
                    "outcome": {"stop": "invalid", "cause": None, "error": error}}
        wanted = data["next_check"]
        if wanted and wanted in offer:
            return {"decisions": decisions, "usage": usage, "next_check": wanted}
        cause = data["cause"]
        if cause is None:
            stop: Stop = "invalid"
        elif wanted:  # asked for a check it could not have: out of budget, or not an allowed tool
            stop = "budget"
        else:
            stop = "escalated" if cause == ESCALATE else "supported"
        return {"decisions": decisions, "usage": usage, "next_check": None,
                "outcome": {"stop": stop, "cause": cause, "reason": data.get("reason") or "",
                            "reply": data.get("reply") or "",
                            "error": None if cause else "no valid cause id in the reply"}}

    async def diagnose(self, ticket: dict[str, str], hypotheses: list[Hypothesis], policy: list[str],
                       runner: CheckRunner, thread: str | None = None) -> Diagnosis:
        state: State = {"ticket": ticket, "hypotheses": [asdict(h) for h in hypotheses], "policy": policy,
                        "checks": [], "decisions": 0, "usage": []}
        config = {"configurable": {"thread_id": thread or ticket["id"], "runner": runner}}
        final = await self.graph.ainvoke(state, config)
        outcome = final.get("outcome") or {"stop": "no_hypotheses", "cause": ESCALATE,
                                           "reason": "No possible cause matched the ticket."}
        cause = outcome.get("cause")
        by_cause = {h.cause: h for h in hypotheses}
        chosen = by_cause.get(cause)
        return Diagnosis(
            ticket=ticket["id"], cause=cause, stop=outcome["stop"], reason=outcome.get("reason", ""),
            reply=outcome.get("reply", ""), fix=chosen.fix if chosen else None,
            checks=final.get("checks", []), hypotheses=[h.cause for h in hypotheses],
            sources=list(chosen.sources) if chosen else [], decisions=final.get("decisions", 0),
            prompt_tokens=sum(u["prompt_tokens"] for u in final.get("usage", [])),
            completion_tokens=sum(u["completion_tokens"] for u in final.get("usage", [])),
            error=outcome.get("error"),
        )
