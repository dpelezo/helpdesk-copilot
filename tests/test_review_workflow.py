"""The review workflow end to end, on Temporal's time-skipping test server (#28).

The test server is a binary the SDK downloads on first use. Where it cannot be downloaded
these tests skip, except in CI, where ``REQUIRE_TEMPORAL_TESTS=1`` makes a skip a failure.
"""

import asyncio
import json
import os
import uuid
from datetime import timedelta
from pathlib import Path

import pytest
from temporalio.client import WorkflowFailureError, WorkflowUpdateFailedError
from temporalio.contrib.pydantic import pydantic_data_converter
from temporalio.testing import WorkflowEnvironment
from temporalio.worker import Worker

from app.knowledge import resolve
from app.knowledge.model import KnowledgeRelease, ReviewStatus
from app.knowledge.review import Decision
from app.knowledge.sources import Source
from app.mock.faults import ISSUES
from app.workflows import review as wf

VPN = "network--vpn-not-connecting"
MFA = "identity--mfa-lockout"


@pytest.fixture
async def env():
    try:
        environment = await WorkflowEnvironment.start_time_skipping(data_converter=pydantic_data_converter)
    except RuntimeError as error:
        if os.getenv("REQUIRE_TEMPORAL_TESTS") == "1":
            raise
        pytest.skip(f"Temporal test server unavailable here: {error}")
    yield environment
    await environment.shutdown()


@pytest.fixture
def draft_path(tmp_path) -> Path:
    items = [{"issue": i, "source_id": "ms-a", "kind": "cause", "text": f"Cause of {i}.", "evidence": "words",
              "evidence_ok": True} for i in ISSUES]
    source = Source(id="ms-a", url="https://learn.microsoft.com/a", issues=[], fetched="2026-11-28", status="ok")
    release, report = resolve.resolve({"date": "2026-11-28", "items": items}, [source], resolve.registered_tools())
    assert report.valid
    path = tmp_path / "draft.json"
    path.write_text(release.model_dump_json(indent=2))
    return path


@pytest.fixture
async def started(env, draft_path, tmp_path):
    """A running worker and a started review; yields (handle, out_dir)."""
    queue = f"review-{uuid.uuid4()}"
    out = tmp_path / "releases"
    req = wf.ReviewRequest(draft_path=draft_path.as_posix(), release_id="release-2026-11-30", out_dir=out.as_posix())
    async with Worker(env.client, task_queue=queue, workflows=[wf.ReviewWorkflow], activities=wf.ACTIVITIES):
        handle = await env.client.start_workflow(wf.ReviewWorkflow.run, req, id=f"review-{uuid.uuid4()}",
                                                 task_queue=queue)
        await loaded(handle)
        yield handle, out


async def loaded(handle):
    """Decisions are refused while the draft loads, so tests wait for the review to open."""
    for _ in range(200):
        if (await handle.query(wf.ReviewWorkflow.status)).stage == "in_review":
            return
        await asyncio.sleep(0.05)
    raise AssertionError("the draft never loaded")


def decision(issue, action="approve", **kw):
    return Decision(issue=issue, action=action, reviewer="dpelezo", **kw)


async def decide(handle, d):
    return await handle.execute_update(wf.ReviewWorkflow.decide, d)


async def test_a_draft_is_reviewed_end_to_end_and_the_approved_part_is_written(started):
    handle, out = started
    state = await decide(handle, decision(VPN))
    assert state.stage == "in_review" and VPN in state.decided and len(state.open_issues) == len(ISSUES) - 1
    await decide(handle, decision(MFA, "send_back", feedback="Add the SSO outage check."))
    await handle.execute_update(wf.ReviewWorkflow.finish)
    result = await handle.result()

    assert result.status == "approved" and result.nodes == 2 and result.edges == 1
    release = KnowledgeRelease.model_validate_json(Path(result.release_path).read_text())
    assert release.release_id == "release-2026-11-30" and release.unpublishable() == []
    assert [n.kind for n in release.nodes] == ["issue", "cause"] and release.nodes[0].id == VPN
    assert all(p.status is ReviewStatus.approved and p.reviewer == "dpelezo" for p in release.nodes[0].provenance)
    record = json.loads(Path(result.record_path).read_text())
    mfa = next(o for o in record["outcomes"] if o["issue"] == MFA)
    assert (mfa["action"], mfa["feedback"]) == ("send_back", "Add the SSO outage check.")
    assert result.release_path.startswith(out.as_posix())


async def test_a_decision_can_change_until_the_review_is_finished(started):
    handle, _ = started
    await decide(handle, decision(VPN))
    state = await decide(handle, decision(VPN, "reject"))
    assert state.decided[VPN].decision.action == "reject"
    await handle.execute_update(wf.ReviewWorkflow.finish)
    result = await handle.result()
    assert result.status == "nothing_approved" and result.nodes == 0
    with pytest.raises(WorkflowUpdateFailedError):
        await decide(handle, decision(VPN))


async def test_a_decision_that_does_not_fit_the_draft_is_refused_and_not_recorded(started):
    handle, _ = started
    with pytest.raises(WorkflowUpdateFailedError) as error:
        await decide(handle, decision(VPN, exclude=[f"{MFA}--cause-00000000"]))
    assert "is not part of" in str(error.value.cause)
    assert (await handle.query(wf.ReviewWorkflow.status)).decided == {}


async def test_finishing_with_no_decisions_is_refused(started):
    handle, _ = started
    with pytest.raises(WorkflowUpdateFailedError):
        await handle.execute_update(wf.ReviewWorkflow.finish)


async def test_an_unfinished_review_expires_and_writes_nothing(env, started):
    handle, out = started
    await decide(handle, decision(VPN))
    await env.sleep(timedelta(days=wf.DEFAULT_TIMEOUT_DAYS + 1))
    result = await handle.result()
    assert result.status == "expired" and result.release_path is None
    assert not out.exists()


async def test_a_draft_changed_during_the_review_is_refused(started, draft_path):
    handle, out = started
    await decide(handle, decision(VPN))
    draft_path.write_text(draft_path.read_text() + "\n")
    await handle.execute_update(wf.ReviewWorkflow.finish)
    with pytest.raises(WorkflowFailureError) as error:
        await handle.result()
    assert "changed during the review" in str(error.value.cause.cause or error.value.cause)
    assert not out.exists()


async def test_a_missing_draft_fails_the_review_at_once(env, tmp_path):
    queue = f"review-{uuid.uuid4()}"
    req = wf.ReviewRequest(draft_path=(tmp_path / "none.json").as_posix(), release_id="release-x")
    async with Worker(env.client, task_queue=queue, workflows=[wf.ReviewWorkflow], activities=wf.ACTIVITIES):
        handle = await env.client.start_workflow(wf.ReviewWorkflow.run, req, id=f"review-{uuid.uuid4()}",
                                                 task_queue=queue)
        with pytest.raises(WorkflowFailureError) as error:
            await handle.result()
    assert "no draft at" in str(error.value.cause.cause or error.value.cause)
