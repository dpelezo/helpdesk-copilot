"""The review workflow (issue #28, ADR 0004): a draft release waits in Temporal for a reviewer.

Temporal holds the wait, which can last days, and keeps the history of every decision. The
rules themselves live in ``app.knowledge.review`` and run inside activities, so the workflow
code stays small and deterministic.

1. ``load_draft`` reads the draft, checks it is valid and returns an index of its issues.
2. The reviewer sends decisions (an update, checked before it is accepted) and can change a
   decision until the review is finished. ``status`` (a query) shows what is still open.
3. ``finish`` (an update) closes the review. Issues with no decision are left out.
4. ``build_release`` re-reads the draft, refuses it if it changed during the review, applies
   the decisions, validates the result and writes the approved release and its review record.
   That file is what publishing loads (#29); nothing reaches Neo4j without approval.

If nobody finishes the review within its timeout, it expires and nothing is written.
"""

from __future__ import annotations

import asyncio
import hashlib
import os
from datetime import timedelta
from pathlib import Path

from temporalio import activity, workflow
from temporalio.common import RetryPolicy
from temporalio.exceptions import ApplicationError

with workflow.unsafe.imports_passed_through():
    from pydantic import BaseModel, ConfigDict, Field

    from app.knowledge.model import Id, KnowledgeRelease
    from app.knowledge.review import Decision, DraftIndex, IssueOutcome, Recorded, apply, check, index

TASK_QUEUE = "knowledge-review"
RELEASES = Path("knowledge/releases")
DEFAULT_TIMEOUT_DAYS = 14
_RETRY = RetryPolicy(maximum_attempts=3, initial_interval=timedelta(seconds=2))


class _Frozen(BaseModel):
    model_config = ConfigDict(frozen=True, extra="forbid")


class ReviewRequest(_Frozen):
    draft_path: str
    release_id: Id
    supersedes: Id | None = None
    timeout_days: int = Field(default=DEFAULT_TIMEOUT_DAYS, ge=1, le=90)
    out_dir: str = RELEASES.as_posix()


class BuildRequest(_Frozen):
    draft_path: str
    draft_sha256: str
    decisions: dict[str, Recorded]
    release_id: Id
    supersedes: Id | None = None
    out_dir: str


class Built(_Frozen):
    release_path: str
    record_path: str
    nodes: int
    edges: int
    outcomes: list[IssueOutcome]


class ReviewState(_Frozen):
    stage: str  # "loading", "in_review", "building", "approved", "nothing_approved", "expired"
    draft_id: str | None
    release_id: str
    decided: dict[str, Recorded]
    open_issues: list[str]


class ReviewResult(_Frozen):
    status: str  # "approved", "nothing_approved" or "expired"
    release_id: str
    release_path: str | None = None
    record_path: str | None = None
    nodes: int = 0
    edges: int = 0
    outcomes: list[IssueOutcome] = Field(default_factory=list)


# ── Activities ─────────────────────────────────────────────────────────


def _read(path: str) -> tuple[KnowledgeRelease, str]:
    raw = Path(path).read_bytes()
    return KnowledgeRelease.model_validate_json(raw), hashlib.sha256(raw).hexdigest()


@activity.defn
async def load_draft(path: str) -> DraftIndex:
    try:
        draft, digest = _read(path)
    except FileNotFoundError as error:
        raise ApplicationError(f"no draft at {path}", non_retryable=True) from error
    except ValueError as error:  # an invalid draft will not become valid on a retry
        raise ApplicationError(f"the draft at {path} is not a valid release: {error}", non_retryable=True) from error
    return index(draft, digest)


@activity.defn
async def build_release(req: BuildRequest) -> Built:
    from app.knowledge.resolve import registered_tools, validate

    draft, digest = _read(req.draft_path)
    if digest != req.draft_sha256:
        raise ApplicationError(
            f"{req.draft_path} changed during the review; start a new review of the new draft",
            non_retryable=True,
        )
    try:
        release, record = apply(draft, digest, req.decisions, req.release_id, req.supersedes)
    except ValueError as error:
        raise ApplicationError(str(error), non_retryable=True) from error
    problems = await asyncio.to_thread(lambda: validate(release, registered_tools()))
    if problems:
        raise ApplicationError(f"the approved release does not validate: {'; '.join(problems)}", non_retryable=True)

    out = Path(req.out_dir)
    out.mkdir(parents=True, exist_ok=True)
    release_path = out / f"{req.release_id}.json"
    record_path = out / f"{req.release_id}.review.json"
    release_path.write_text(release.model_dump_json(indent=2) + "\n")
    record_path.write_text(record.model_dump_json(indent=2) + "\n")
    return Built(release_path=release_path.as_posix(), record_path=record_path.as_posix(),
                 nodes=len(release.nodes), edges=len(release.edges), outcomes=record.outcomes)


ACTIVITIES = [load_draft, build_release]


# ── Workflow ───────────────────────────────────────────────────────────


@workflow.defn
class ReviewWorkflow:
    def __init__(self) -> None:
        self._draft: DraftIndex | None = None
        self._decided: dict[str, Recorded] = {}
        self._finished = False
        self._stage = "loading"
        self._release_id = ""

    @workflow.run
    async def run(self, req: ReviewRequest) -> ReviewResult:
        self._release_id = req.release_id
        self._draft = await workflow.execute_activity(
            load_draft, req.draft_path, start_to_close_timeout=timedelta(minutes=1), retry_policy=_RETRY)
        self._stage = "in_review"
        try:
            await workflow.wait_condition(lambda: self._finished, timeout=timedelta(days=req.timeout_days))
        except asyncio.TimeoutError:
            self._stage = "expired"
            return ReviewResult(status="expired", release_id=req.release_id)

        self._stage = "building"
        built = await workflow.execute_activity(
            build_release,
            BuildRequest(draft_path=req.draft_path, draft_sha256=self._draft.sha256, decisions=self._decided,
                         release_id=req.release_id, supersedes=req.supersedes, out_dir=req.out_dir),
            start_to_close_timeout=timedelta(minutes=2), retry_policy=_RETRY,
        )
        self._stage = "approved" if built.nodes else "nothing_approved"
        return ReviewResult(status=self._stage, release_id=req.release_id, release_path=built.release_path,
                            record_path=built.record_path, nodes=built.nodes, edges=built.edges,
                            outcomes=built.outcomes)

    @workflow.update
    async def decide(self, decision: Decision) -> ReviewState:
        self._decided[decision.issue] = Recorded(decision=decision, decided_on=workflow.now().date())
        return self.status()

    @decide.validator
    def _check_decision(self, decision: Decision) -> None:
        if self._draft is None:
            raise ValueError("the draft is still loading; try again in a moment")
        if self._finished:
            raise ValueError("the review is finished; decisions can no longer change")
        problems = check(decision, self._draft)
        if problems:
            raise ValueError("; ".join(problems))

    @workflow.update
    async def finish(self) -> ReviewState:
        self._finished = True
        return self.status()

    @finish.validator
    def _check_finish(self) -> None:
        if self._draft is None:
            raise ValueError("the draft is still loading; try again in a moment")
        if self._finished:
            raise ValueError("the review is already finished")
        if not self._decided:
            raise ValueError("no decisions yet: decide at least one issue before finishing")

    @workflow.query
    def status(self) -> ReviewState:
        issues = list(self._draft.issues) if self._draft else []
        return ReviewState(stage=self._stage, draft_id=self._draft.release_id if self._draft else None,
                           release_id=self._release_id, decided=dict(self._decided),
                           open_issues=[i for i in issues if i not in self._decided])


def workflow_id(release_id: str) -> str:
    return f"review-{release_id}"


async def connect():
    """A Temporal client with the pydantic converter the workflow's models need."""
    from temporalio.client import Client
    from temporalio.contrib.pydantic import pydantic_data_converter

    host = os.getenv("TEMPORAL_HOST", "localhost:7233")
    namespace = os.getenv("TEMPORAL_NAMESPACE", "default")
    api_key = os.getenv("TEMPORAL_API_KEY") or None
    return await Client.connect(host, namespace=namespace, api_key=api_key, tls=bool(api_key),
                                data_converter=pydantic_data_converter)
