"""Review API and CLI (#28) against a fake Temporal client: no server, no database."""

from datetime import date
from types import SimpleNamespace

import pytest
from fastapi import FastAPI
from fastapi.testclient import TestClient
from temporalio.client import WorkflowUpdateFailedError
from temporalio.exceptions import ApplicationError
from temporalio.service import RPCError, RPCStatusCode

from app.api import reviews
from app.knowledge.review import Decision, Recorded
from app.workflows import review as wf
from app.workflows import review_cli

AUTH = {"Authorization": "Bearer test-key-1"}
VPN = "network--vpn-not-connecting"


def state(**kw):
    base = {"stage": "in_review", "draft_id": "draft-2026-11-28", "release_id": "release-x", "decided": {},
            "open_issues": [VPN]}
    return wf.ReviewState(**{**base, **kw})


class Handle:
    def __init__(self, fake):
        self.fake = fake

    async def query(self, query):
        self.fake.check()
        return self.fake.state

    async def describe(self):
        return SimpleNamespace(status=SimpleNamespace(name=self.fake.temporal_status))

    async def result(self):
        return self.fake.result

    async def execute_update(self, update, *args):
        self.fake.check()
        self.fake.updates.append((update.__name__, args))
        if self.fake.refuse:
            raise WorkflowUpdateFailedError(ApplicationError(self.fake.refuse))
        if update.__name__ == "decide":
            d = args[0]
            return state(decided={d.issue: Recorded(decision=d, decided_on=date(2026, 11, 30))}, open_issues=[])
        return state(stage="building", open_issues=[])


class Fake:
    def __init__(self):
        self.started, self.updates = [], []
        self.state, self.result, self.temporal_status = state(), None, "RUNNING"
        self.refuse = self.missing = None
        self.exists = False

    def check(self):
        if self.missing:
            raise RPCError("not found", RPCStatusCode.NOT_FOUND, b"")

    async def start_workflow(self, run, req, *, id, task_queue):
        if self.exists:
            raise RPCError("exists", RPCStatusCode.ALREADY_EXISTS, b"")
        self.started.append((req, id, task_queue))

    def get_workflow_handle(self, workflow_id):
        self.last_id = workflow_id
        return Handle(self)


@pytest.fixture
def fake():
    return Fake()


@pytest.fixture
def api(fake):
    app = FastAPI()
    app.include_router(reviews.router, prefix="/api/v1")
    app.dependency_overrides[reviews.review_client] = lambda: fake
    return TestClient(app)


DECISION = {"issue": VPN, "action": "approve", "reviewer": "dpelezo"}


def test_every_route_needs_an_api_key(api):
    assert api.post("/api/v1/reviews", json={"draft_path": "d.json", "release_id": "release-x"}).status_code in (401, 403)
    assert api.get("/api/v1/reviews/release-x", headers={"Authorization": "Bearer wrong"}).status_code == 401
    assert api.post("/api/v1/reviews/release-x/finish").status_code in (401, 403)


def test_start_runs_the_workflow_on_the_review_queue(api, fake):
    r = api.post("/api/v1/reviews", json={"draft_path": "d.json", "release_id": "release-x"}, headers=AUTH)
    assert r.status_code == 201 and r.json() == {"workflow_id": "review-release-x", "release_id": "release-x"}
    [(req, wid, queue)] = fake.started
    assert (req.draft_path, wid, queue) == ("d.json", "review-release-x", wf.TASK_QUEUE)


def test_starting_the_same_release_twice_is_a_conflict(api, fake):
    fake.exists = True
    r = api.post("/api/v1/reviews", json={"draft_path": "d.json", "release_id": "release-x"}, headers=AUTH)
    assert r.status_code == 409


def test_an_invalid_release_id_is_refused_before_temporal(api, fake):
    r = api.post("/api/v1/reviews", json={"draft_path": "d.json", "release_id": "Release X"}, headers=AUTH)
    assert r.status_code == 422 and not fake.started


def test_status_shows_the_state_and_the_result_once_done(api, fake):
    r = api.get("/api/v1/reviews/release-x", headers=AUTH)
    assert r.json()["state"]["open_issues"] == [VPN] and r.json()["result"] is None
    fake.temporal_status = "COMPLETED"
    fake.result = wf.ReviewResult(status="approved", release_id="release-x", release_path="r.json", nodes=2)
    assert api.get("/api/v1/reviews/release-x", headers=AUTH).json()["result"]["status"] == "approved"
    assert fake.last_id == "review-release-x"


def test_an_unknown_review_is_404(api, fake):
    fake.missing = True
    assert api.get("/api/v1/reviews/release-x", headers=AUTH).status_code == 404
    assert api.post("/api/v1/reviews/release-x/decisions", json=DECISION, headers=AUTH).status_code == 404


def test_a_decision_is_sent_as_a_checked_update(api, fake):
    r = api.post("/api/v1/reviews/release-x/decisions", json=DECISION, headers=AUTH)
    assert r.status_code == 200 and VPN in r.json()["decided"]
    [(name, (d,))] = fake.updates
    assert name == "decide" and d == Decision(**DECISION)


def test_a_malformed_decision_never_reaches_the_workflow(api, fake):
    r = api.post("/api/v1/reviews/release-x/decisions", json={**DECISION, "action": "send_back"}, headers=AUTH)
    assert r.status_code == 422 and "send_back needs feedback" in r.text and not fake.updates


def test_a_refused_decision_returns_the_workflows_reason(api, fake):
    fake.refuse = "edit: x is not part of network--vpn-not-connecting"
    r = api.post("/api/v1/reviews/release-x/decisions", json=DECISION, headers=AUTH)
    assert r.status_code == 422 and r.json()["detail"] == fake.refuse


def test_finish_closes_the_review(api, fake):
    r = api.post("/api/v1/reviews/release-x/finish", headers=AUTH)
    assert r.status_code == 200 and r.json()["stage"] == "building" and fake.updates[0][0] == "finish"


def test_the_main_app_serves_the_review_routes():
    from app.main import app

    assert any(getattr(route, "path", "") == "/api/v1/reviews/{release_id}/decisions" for route in app.routes)


# ── CLI ────────────────────────────────────────────────────────────────


def test_cli_starts_a_review_of_the_committed_draft_by_default(fake, capsys):
    assert review_cli.main(["start", "--release-id", "release-x"], client=fake) == 0
    [(req, wid, _)] = fake.started
    assert req.draft_path == "knowledge/draft/release.json" and wid == "review-release-x"
    assert "Review started: review-release-x" in capsys.readouterr().out


def test_cli_decides_with_edits_links_and_excludes(fake, capsys):
    argv = ["decide", "release-x", "--issue", VPN, "--action", "edit", "--reviewer", "dpelezo",
            "--edit", f"{VPN}--cause-1=New: wording", "--link", "TESTS_FOR:d1:c1:certificate expired",
            "--exclude", f"{VPN}--solution-2"]
    assert review_cli.main(argv, client=fake) == 0
    [(_, (d,))] = fake.updates
    assert d.edits[0].text == "New: wording" and d.links[0].supports_if == "certificate expired"
    assert d.exclude == [f"{VPN}--solution-2"]
    assert f"{VPN}: edit by dpelezo on 2026-11-30" in capsys.readouterr().out


def test_cli_reports_refusals_and_bad_decisions(fake, capsys):
    fake.refuse = "the review is finished; decisions can no longer change"
    assert review_cli.main(["decide", "release-x", "--issue", VPN, "--action", "approve", "--reviewer", "r"],
                           client=fake) == 1
    assert "Refused: the review is finished" in capsys.readouterr().err
    assert review_cli.main(["decide", "release-x", "--issue", VPN, "--action", "send_back", "--reviewer", "r"],
                           client=fake) == 2
    assert "send_back needs feedback" in capsys.readouterr().err


def test_cli_status_lists_decided_and_open_issues(fake, capsys):
    d = Decision(issue="identity--mfa-lockout", action="send_back", reviewer="r", feedback="Add the SSO check.")
    fake.state = state(decided={d.issue: Recorded(decision=d, decided_on=date(2026, 11, 30))})
    assert review_cli.main(["status", "release-x"], client=fake) == 0
    out = capsys.readouterr().out
    assert "identity--mfa-lockout: send_back by r on 2026-11-30: Add the SSO check." in out
    assert f"{VPN}: open" in out
