"""Issue #8: compare Telnyx models on tool calling, using the tuning tickets only.

Each ticket gets a fresh world (the mock company with its cause planted) and the
five read-only mock systems as tools, plus ``submit_diagnosis``. The account-
changing ``actions`` server is never offered. The model checks systems until it
submits a diagnosis or runs out of steps.

This is a minimal loop to judge models, not the copilot's agent (week 3).

    python -m app.evals.model_choice --models zai-org/GLM-5.3-Flash,moonshotai/Kimi-K2.6
"""

from __future__ import annotations

import argparse
import json
import statistics
import sys
from collections.abc import Callable
from contextlib import AsyncExitStack
from dataclasses import asdict, dataclass, field
from pathlib import Path
from typing import Any, Protocol

import anyio
from mcp import Client

from app.llm import ChatClient, LLMError, Reply
from app.mock import tickets as ticket_set
from app.mock.company import Company, generate
from app.mock.systems import SERVERS, World

READ_ONLY_SERVERS = ("identity", "devices", "vpn", "status", "tickets")
MAX_STEPS = 8
OUT_DIR = Path("evals/model_choice/results")

SYSTEM = (
    "You are an IT helpdesk diagnostic assistant for Kestrel Ridge Logistics. "
    "Find the root cause of the employee's problem by running checks with the tools. "
    "Run one check at a time and base your conclusion only on what the checks return. "
    "Then call submit_diagnosis with one of two outcomes:\n"
    "- 'fix': a check shows what is wrong. Use this even when the repair needs a "
    "technician, a hardware visit, an approval or simply waiting for a service to recover; "
    "the helpdesk agent handles that once the cause is known.\n"
    "- 'escalate': you ran the relevant checks and every one came back healthy, so the "
    "cause is not visible in these systems."
)

SUBMIT = {
    "type": "function",
    "function": {
        "name": "submit_diagnosis",
        "description": (
            "Finish. outcome 'fix' when a check shows the cause (even if a technician or approval "
            "is needed to repair it); 'escalate' only when every relevant check is healthy."
        ),
        "parameters": {
            "type": "object",
            "properties": {
                "root_cause": {"type": "string", "description": "The cause, in one sentence."},
                "evidence": {"type": "string", "description": "Which checks showed it."},
                "outcome": {
                    "type": "string",
                    "enum": ["fix", "escalate"],
                    "description": "fix = a check found the cause; escalate = checks found nothing wrong.",
                },
            },
            "required": ["root_cause", "evidence", "outcome"],
        },
    },
}


class Model(Protocol):
    model: str

    def complete(self, messages: list[dict[str, Any]], tools: list[dict[str, Any]] | None = None) -> Reply: ...


@dataclass
class Call:
    tool: str
    valid: bool
    employee_ok: bool | None = None  # None when the tool takes no employee


@dataclass
class TicketRun:
    ticket: str
    expected_outcome: str
    reveals: list[str]
    calls: list[Call] = field(default_factory=list)
    submitted: bool = False
    outcome: str | None = None
    root_cause: str | None = None
    steps: int = 0
    seconds: float = 0.0
    prompt_tokens: int = 0
    completion_tokens: int = 0
    reasoned: bool = False
    error: str | None = None

    @property
    def outcome_correct(self) -> bool:
        return self.submitted and self.outcome == self.expected_outcome

    @property
    def evidence_found(self) -> bool:
        checked = {c.tool for c in self.calls if c.valid}
        return self.outcome_correct and set(self.reveals) <= checked


def _openai_tool(tool: Any) -> dict[str, Any]:
    return {
        "type": "function",
        "function": {"name": tool.name, "description": tool.description or "", "parameters": tool.input_schema},
    }


def _employee_matches(world: World, ref: Any, employee_id: str) -> bool:
    if not isinstance(ref, str):
        return False
    try:
        return world.employee(ref).id == employee_id
    except Exception:  # an unknown name is simply a wrong argument
        return False


async def run_ticket(model: Model, ticket: ticket_set.Ticket, company: Company) -> TicketRun:
    world = World(ticket_set.world_for(ticket, company))
    employee = next(e for e in world.company.employees if e.id == ticket.employee_id)
    run = TicketRun(ticket.id, ticket.answer.outcome, list(ticket.answer.reveals))
    messages: list[dict[str, Any]] = [
        {"role": "system", "content": SYSTEM},
        {"role": "user", "content": (
            f"From: {employee.name} <{employee.email}>\nSubject: {ticket.subject}\n\n{ticket.message}"
        )},
    ]
    async with AsyncExitStack() as stack:
        clients: dict[str, Client] = {}
        tools: list[dict[str, Any]] = [SUBMIT]
        takes_employee: set[str] = set()
        for name in READ_ONLY_SERVERS:
            client = await stack.enter_async_context(Client(SERVERS[name](world)))
            for tool in (await client.list_tools()).tools:
                clients[tool.name] = client
                tools.append(_openai_tool(tool))
                if "employee" in (tool.input_schema.get("properties") or {}):
                    takes_employee.add(tool.name)

        for _ in range(MAX_STEPS):
            run.steps += 1
            try:
                reply = await anyio.to_thread.run_sync(lambda: model.complete(messages, tools))
            except LLMError as error:
                run.error = str(error)
                return run
            run.seconds += reply.seconds
            run.prompt_tokens += int(reply.usage.get("prompt_tokens") or 0)
            run.completion_tokens += int(reply.usage.get("completion_tokens") or 0)
            run.reasoned = run.reasoned or reply.reasoned
            message = {k: v for k, v in reply.message.items() if k in ("role", "content", "tool_calls")}
            message.setdefault("role", "assistant")
            messages.append(message)
            if not reply.tool_calls:
                return run  # stopped without submitting

            for call in reply.tool_calls:
                fn = call.get("function") or {}
                name = fn.get("name", "")
                try:
                    args = json.loads(fn.get("arguments") or "{}")
                    if not isinstance(args, dict):
                        raise ValueError
                except ValueError:
                    run.calls.append(Call(name, valid=False))
                    messages.append(_tool_reply(call, "Invalid JSON arguments."))
                    continue
                if name == "submit_diagnosis":
                    ok = args.get("outcome") in ("fix", "escalate") and bool(args.get("root_cause"))
                    run.calls.append(Call(name, valid=ok))
                    if ok:
                        run.submitted, run.outcome, run.root_cause = True, args["outcome"], args["root_cause"]
                        return run
                    messages.append(_tool_reply(call, "submit_diagnosis needs root_cause and outcome."))
                    continue
                if name not in clients:
                    run.calls.append(Call(name, valid=False))
                    messages.append(_tool_reply(call, f"Unknown tool {name!r}."))
                    continue
                result = await clients[name].call_tool(name, args)
                employee_ok = (
                    _employee_matches(world, args.get("employee"), ticket.employee_id)
                    if name in takes_employee else None
                )
                run.calls.append(Call(name, valid=not result.is_error, employee_ok=employee_ok))
                content = (
                    json.dumps(result.structured_content) if result.structured_content is not None
                    else " ".join(getattr(c, "text", "") for c in result.content)
                )
                messages.append(_tool_reply(call, content))
    return run


def _tool_reply(call: dict[str, Any], content: str) -> dict[str, Any]:
    return {"role": "tool", "tool_call_id": call.get("id", ""), "content": content}


@dataclass
class Summary:
    model: str
    tickets: int
    valid_tool_calls: float
    correct_arguments: float
    submitted: float
    outcome_correct: float
    evidence_found: float
    seconds_per_ticket: float
    tokens_per_ticket: float
    steps_per_ticket: float
    reasoned_tickets: int
    errors: int


def _pct(part: int, whole: int) -> float:
    return round(100 * part / whole, 1) if whole else 0.0


def summarize(model: str, runs: list[TicketRun]) -> Summary:
    calls = [c for r in runs for c in r.calls]
    arg_calls = [c for c in calls if c.employee_ok is not None and c.valid]
    n = len(runs)
    return Summary(
        model=model,
        tickets=n,
        valid_tool_calls=_pct(sum(c.valid for c in calls), len(calls)),
        correct_arguments=_pct(sum(bool(c.employee_ok) for c in arg_calls), len(arg_calls)),
        submitted=_pct(sum(r.submitted for r in runs), n),
        outcome_correct=_pct(sum(r.outcome_correct for r in runs), n),
        evidence_found=_pct(sum(r.evidence_found for r in runs), n),
        seconds_per_ticket=round(statistics.fmean(r.seconds for r in runs), 2) if runs else 0.0,
        tokens_per_ticket=round(statistics.fmean(r.prompt_tokens + r.completion_tokens for r in runs)) if runs else 0,
        steps_per_ticket=round(statistics.fmean(r.steps for r in runs), 1) if runs else 0.0,
        reasoned_tickets=sum(r.reasoned for r in runs),
        errors=sum(r.error is not None for r in runs),
    )


def table(summaries: list[Summary]) -> str:
    head = ("| Model | Valid tool calls | Correct employee | Submitted | Fix/escalate right | "
            "Evidence found | s/ticket | tokens/ticket | steps | reasoned | errors |")
    rows = [head, "| --- | ---: | ---: | ---: | ---: | ---: | ---: | ---: | ---: | ---: | ---: |"]
    for s in summaries:
        rows.append(
            f"| `{s.model}` | {s.valid_tool_calls}% | {s.correct_arguments}% | {s.submitted}% | "
            f"{s.outcome_correct}% | {s.evidence_found}% | {s.seconds_per_ticket} | {s.tokens_per_ticket:,} | "
            f"{s.steps_per_ticket} | {s.reasoned_tickets}/{s.tickets} | {s.errors} |"
        )
    return "\n".join(rows)


async def evaluate(
    models: list[str],
    make_client: Callable[[str], Model] = ChatClient.from_env,
    split: str = "tuning",
    out_dir: Path = OUT_DIR,
) -> list[Summary]:
    company = generate()
    selected = ticket_set.load(split)  # type: ignore[arg-type]
    out_dir.mkdir(parents=True, exist_ok=True)
    summaries = []
    for name in models:
        client = make_client(name)
        runs = [await run_ticket(client, t, company) for t in selected]
        summary = summarize(name, runs)
        summaries.append(summary)
        slug = name.replace("/", "__")
        (out_dir / f"{slug}.json").write_text(json.dumps(
            {"summary": asdict(summary), "runs": [asdict(r) for r in runs]}, indent=2) + "\n")
    return summaries


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    parser.add_argument("--models", required=True, help="comma-separated model ids")
    parser.add_argument("--split", default="tuning", choices=["tuning"])  # held-out is never used here
    parser.add_argument("--out", type=Path, default=OUT_DIR)
    args = parser.parse_args(argv)
    try:
        summaries = anyio.run(lambda: evaluate(args.models.split(","), split=args.split, out_dir=args.out))
    except LLMError as error:
        print(f"model_choice: {error}", file=sys.stderr)
        return 1
    print(table(summaries))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
