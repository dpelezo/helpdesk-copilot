"""Review from the terminal (issue #28). Talks to the same Temporal workflow as the API.

    python -m app.workflows.review_cli start --release-id release-2026-11-28
    python -m app.workflows.review_cli status release-2026-11-28
    python -m app.workflows.review_cli decide release-2026-11-28 --issue identity--mfa-lockout \\
        --action approve --reviewer dpelezo
    python -m app.workflows.review_cli decide release-2026-11-28 --issue network--vpn-not-connecting \\
        --action edit --reviewer dpelezo --edit '<node id>=Clearer wording.' \\
        --link 'TESTS_FOR:<diagnostic id>:<cause id>:certificate expired'
    python -m app.workflows.review_cli finish release-2026-11-28

Start a worker first: ``python temporal/workers/worker.py``.
"""

from __future__ import annotations

import argparse
import asyncio
import sys

from pydantic import ValidationError
from temporalio.client import WorkflowUpdateFailedError

from app.knowledge.resolve import DRAFT
from app.knowledge.review import Decision, Link, TextEdit
from app.workflows.review import TASK_QUEUE, ReviewRequest, ReviewState, ReviewWorkflow, connect, workflow_id


def parse_edit(value: str) -> TextEdit:
    node_id, sep, text = value.partition("=")
    if not sep:
        raise argparse.ArgumentTypeError("an edit is NODE_ID=new text")
    return TextEdit(node_id=node_id.strip(), text=text)


def parse_link(value: str) -> Link:
    parts = value.split(":", 4)
    if len(parts) < 3:
        raise argparse.ArgumentTypeError("a link is KIND:SOURCE:TARGET[:supports_if[:rules_out_if]]")
    kind, source, target, *rest = parts
    return Link(kind=kind, source=source, target=target, supports_if=rest[0] if rest else None,
                rules_out_if=rest[1] if len(rest) > 1 else None)


def render(state: ReviewState) -> str:
    lines = [f"{state.release_id} ({state.stage}), draft {state.draft_id or '-'}",
             f"Decided: {len(state.decided)}; open: {len(state.open_issues)}"]
    for issue, recorded in state.decided.items():
        d = recorded.decision
        extra = f": {d.feedback}" if d.feedback else ""
        lines.append(f"  {issue}: {d.action.value} by {d.reviewer} on {recorded.decided_on}{extra}")
    lines += [f"  {issue}: open" for issue in state.open_issues]
    return "\n".join(lines)


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(description="Review a draft knowledge release.")
    sub = parser.add_subparsers(dest="command", required=True)
    start = sub.add_parser("start", help="Start a review of a draft release.")
    start.add_argument("--release-id", required=True)
    start.add_argument("--draft", default=DRAFT.as_posix())
    start.add_argument("--supersedes")
    for name in ("status", "finish"):
        sub.add_parser(name).add_argument("release_id")
    decide = sub.add_parser("decide", help="Approve, edit, reject or send back one issue.")
    decide.add_argument("release_id")
    decide.add_argument("--issue", required=True)
    decide.add_argument("--action", required=True, choices=["approve", "edit", "reject", "send_back"])
    decide.add_argument("--reviewer", required=True)
    decide.add_argument("--feedback")
    decide.add_argument("--edit", action="append", default=[], type=parse_edit, metavar="NODE_ID=TEXT")
    decide.add_argument("--link", action="append", default=[], type=parse_link, metavar="KIND:SOURCE:TARGET[:IF]")
    decide.add_argument("--exclude", action="append", default=[], metavar="NODE_ID")
    return parser


async def run(args: argparse.Namespace, client=None) -> str:
    client = client or await connect()
    if args.command == "start":
        req = ReviewRequest(draft_path=args.draft, release_id=args.release_id, supersedes=args.supersedes)
        await client.start_workflow(ReviewWorkflow.run, req, id=workflow_id(args.release_id), task_queue=TASK_QUEUE)
        return f"Review started: {workflow_id(args.release_id)}"
    handle = client.get_workflow_handle(workflow_id(args.release_id))
    if args.command == "status":
        return render(await handle.query(ReviewWorkflow.status))
    if args.command == "finish":
        return render(await handle.execute_update(ReviewWorkflow.finish))
    decision = Decision(issue=args.issue, action=args.action, reviewer=args.reviewer, feedback=args.feedback,
                        edits=args.edit, links=args.link, exclude=args.exclude)
    return render(await handle.execute_update(ReviewWorkflow.decide, decision))


def main(argv: list[str] | None = None, client=None) -> int:
    try:
        args = build_parser().parse_args(argv)
        print(asyncio.run(run(args, client)))
    except ValidationError as error:
        print(f"Not a valid decision: {error.errors()[0]['msg']}", file=sys.stderr)
        return 2
    except WorkflowUpdateFailedError as error:
        print(f"Refused: {getattr(error.cause, 'message', error)}", file=sys.stderr)
        return 1
    return 0


if __name__ == "__main__":
    sys.exit(main())
