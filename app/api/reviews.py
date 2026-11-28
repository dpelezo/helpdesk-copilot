"""Review API (issue #28): start a review, see what is open, decide, finish. API-key auth.

Every route talks to the ``ReviewWorkflow`` in Temporal; nothing here touches the database or
the knowledge files directly, so the workflow's history is the single record of the review.
"""

from __future__ import annotations

from fastapi import APIRouter, Depends, HTTPException, Security, status
from pydantic import BaseModel
from temporalio.client import Client, WorkflowUpdateFailedError
from temporalio.service import RPCError, RPCStatusCode

from app.auth.auth import verify_api_key
from app.knowledge.review import Decision
from app.workflows.review import TASK_QUEUE, ReviewRequest, ReviewResult, ReviewState, ReviewWorkflow, connect, workflow_id

router = APIRouter(prefix="/reviews", tags=["Review"], dependencies=[Security(verify_api_key)])

_client: Client | None = None


async def review_client() -> Client:
    """The Temporal client, connected once. Tests override this dependency."""
    global _client
    if _client is None:
        _client = await connect()
    return _client


class Started(BaseModel):
    workflow_id: str
    release_id: str


class Snapshot(BaseModel):
    state: ReviewState
    result: ReviewResult | None = None


def _not_found(error: RPCError, release_id: str) -> HTTPException:
    if error.status is RPCStatusCode.NOT_FOUND:
        return HTTPException(status.HTTP_404_NOT_FOUND, f"no review for {release_id}")
    return HTTPException(status.HTTP_502_BAD_GATEWAY, "Temporal did not answer")


@router.post("", status_code=status.HTTP_201_CREATED, response_model=Started)
async def start_review(req: ReviewRequest, client: Client = Depends(review_client)) -> Started:
    wid = workflow_id(req.release_id)
    try:
        await client.start_workflow(ReviewWorkflow.run, req, id=wid, task_queue=TASK_QUEUE)
    except RPCError as error:
        if error.status is RPCStatusCode.ALREADY_EXISTS:
            raise HTTPException(status.HTTP_409_CONFLICT, f"a review for {req.release_id} already exists") from error
        raise HTTPException(status.HTTP_502_BAD_GATEWAY, "Temporal did not answer") from error
    return Started(workflow_id=wid, release_id=req.release_id)


@router.get("/{release_id}", response_model=Snapshot)
async def review_status(release_id: str, client: Client = Depends(review_client)) -> Snapshot:
    handle = client.get_workflow_handle(workflow_id(release_id))
    try:
        state = await handle.query(ReviewWorkflow.status)
        described = await handle.describe()
        result = None
        if described.status and described.status.name == "COMPLETED":
            result = await handle.result()
    except RPCError as error:
        raise _not_found(error, release_id) from error
    return Snapshot(state=state, result=result)


async def _update(release_id: str, client: Client, update, *args):
    handle = client.get_workflow_handle(workflow_id(release_id))
    try:
        return await handle.execute_update(update, *args)
    except WorkflowUpdateFailedError as error:
        # The workflow's validator refused it: say why, in its words.
        reason = getattr(error.cause, "message", None) or str(error.cause or error)
        raise HTTPException(status.HTTP_422_UNPROCESSABLE_CONTENT, reason) from error
    except RPCError as error:
        # A completed workflow takes no updates, and Temporal says "not found" for the update.
        # Tell that apart from a review that never existed.
        if error.status is RPCStatusCode.NOT_FOUND:
            try:
                described = await handle.describe()
            except RPCError:
                described = None
            if described is not None and described.status and described.status.name != "RUNNING":
                raise HTTPException(status.HTTP_409_CONFLICT,
                                    f"the review of {release_id} is finished; decisions can no longer change") from error
        raise _not_found(error, release_id) from error


@router.post("/{release_id}/decisions", response_model=ReviewState)
async def decide(release_id: str, decision: Decision, client: Client = Depends(review_client)) -> ReviewState:
    return await _update(release_id, client, ReviewWorkflow.decide, decision)


@router.post("/{release_id}/finish", response_model=ReviewState)
async def finish(release_id: str, client: Client = Depends(review_client)) -> ReviewState:
    return await _update(release_id, client, ReviewWorkflow.finish)
