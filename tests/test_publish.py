"""Publishing to Neo4j (#29).

The decision rules run everywhere. The graph tests need a Neo4j: CI starts one as a service
(``NEO4J_URI``) and sets ``REQUIRE_NEO4J_TESTS=1``, so there a skip is a failure. Locally,
``docker compose up neo4j`` and export ``NEO4J_URI=bolt://localhost:7687``.
"""

import os
from datetime import UTC, date, datetime

import pytest

from app.knowledge import publish as pub
from app.knowledge import resolve
from app.knowledge import review as rv
from app.knowledge.model import (
    ConfirmedCause,
    ImplementedBy,
    Incident,
    KnowledgeRelease,
    Provenance,
    ReviewStatus,
    Runbook,
    RunbookStep,
)
from app.knowledge.sources import Source
from app.mock.faults import ISSUES

LOCKED = "identity--account-locked"
DAY = date(2026, 11, 30)


def approved(release_id, **kw):
    return Provenance(source_id="handbook-it-policy", source_url="https://github.com/dpelezo/helpdesk-copilot",
                      evidence_span="After 5 failed sign-in attempts the account locks.", status=ReviewStatus.approved,
                      reviewer="dpelezo", reviewed_on=DAY, release_id=release_id, **kw)


def build(release_id="release-1", supersedes=None, extra_cause=None):
    """An approved release using every node kind and all six relationships."""
    items = [{"issue": LOCKED, "source_id": "ms-a", "kind": k, "text": t, "evidence": "words", "evidence_ok": True}
             for k, t in [("symptom", "Sign-in is refused."), ("cause", "Too many failed sign-ins."),
                          ("diagnostic", "Check the failed sign-in count in the account status."),
                          ("solution", "Unlock the account.")]]
    if extra_cause:
        items.append({"issue": LOCKED, "source_id": "ms-a", "kind": "cause", "text": extra_cause,
                      "evidence": "words", "evidence_ok": True})
    source = Source(id="ms-a", url="https://learn.microsoft.com/a", issues=[], fetched="2026-11-28", status="ok")
    draft, report = resolve.resolve({"date": "2026-11-28", "items": items}, [source], resolve.registered_tools())
    draft = draft.model_copy(update={"nodes": [n for n in draft.nodes if n.id.startswith(LOCKED)],
                                     "edges": [e for e in draft.edges if e.source == LOCKED]})
    kind = {n.kind: n.id for n in draft.nodes}
    decision = rv.Decision(issue=LOCKED, action="edit", reviewer="dpelezo", links=[
        rv.Link(kind="TESTS_FOR", source=kind["diagnostic"], target=kind["cause"], supports_if="count at the threshold"),
        rv.Link(kind="MITIGATED_BY", source=kind["cause"], target=kind["solution"]),
    ])
    release, _ = rv.apply(draft, "sha", {LOCKED: rv.Recorded(decision=decision, decided_on=DAY)}, release_id,
                          supersedes=supersedes)
    prov = [approved(release_id)]
    runbook = Runbook(id=f"{LOCKED}--runbook-unlock", title="Unlock a locked account", provenance=prov, steps=[
        RunbookStep(order=1, instruction="Confirm the lock in the account status.", tool="account_status"),
        RunbookStep(order=2, instruction="Unlock after approval.", tool="unlock_account", changes_account=True)])
    incident = Incident(id=f"{LOCKED}--incident-t001", ticket_id="T-001", closed_on=DAY, provenance=prov)
    return release.model_copy(update={
        "nodes": [*release.nodes, runbook, incident],
        "edges": [*release.edges,
                  ImplementedBy(source=kind["solution"], target=runbook.id, provenance=prov),
                  ConfirmedCause(source=incident.id, target=kind["cause"], provenance=prov,
                                 verified_by="account_status", verified_on=DAY)],
    })


def manifest(release, status="current"):
    return pub.Manifest(release.release_id, pub.content_hash(release), release.supersedes, len(release.nodes),
                        len(release.edges), "2026-11-30T00:00:00+00:00", status)


# ── Decision rules (no database) ───────────────────────────────────────


def test_the_test_release_uses_every_relationship():
    assert {e.kind for e in build().edges} == set(pub.ENDPOINTS)
    assert build().unpublishable() == []


def test_a_first_release_publishes_and_the_same_one_again_is_a_noop():
    r = build()
    assert pub.decide(r, None, None) == "publish"
    assert pub.decide(r, manifest(r), manifest(r)) == "noop"


def test_the_same_id_with_different_content_is_refused():
    r = build()
    changed = build(extra_cause="The password expired.")
    with pytest.raises(pub.PublishError, match="already published with different content"):
        pub.decide(changed, manifest(r), manifest(r))


def test_a_new_release_must_name_the_current_one():
    first = build()
    with pytest.raises(pub.PublishError, match="supersedes nothing, but the current release is release-1"):
        pub.decide(build("release-2"), None, manifest(first))
    with pytest.raises(pub.PublishError, match="supersedes release-0, but the current release is release-1"):
        pub.decide(build("release-2", supersedes="release-0"), None, manifest(first))
    assert pub.decide(build("release-2", supersedes="release-1"), None, manifest(first)) == "publish"
    with pytest.raises(pub.PublishError, match="nothing is published yet"):
        pub.decide(build("release-2", supersedes="release-1"), None, None)


def test_unapproved_facts_and_duplicate_relationships_are_refused():
    r = build()
    draftish = r.model_copy(update={"nodes": [r.nodes[0].model_copy(update={"provenance": [
        r.nodes[0].provenance[0].model_copy(update={"status": ReviewStatus.draft})]}), *r.nodes[1:]]})
    with pytest.raises(pub.PublishError, match="not approved"):
        pub.decide(draftish, None, None)
    doubled = r.model_copy(update={"edges": [*r.edges, r.edges[0]]})
    with pytest.raises(pub.PublishError, match="duplicate relationships"):
        pub.decide(doubled, None, None)


def test_node_properties_keep_the_full_node_and_a_searchable_text():
    r = build()
    props = pub.node_props(r.nodes[0], r.release_id, 0)
    assert props["uid"] == f"release-1/{LOCKED}" and props["kind"] == "issue" and props["text"] == "Account locked"
    assert type(r.nodes[0]).model_validate_json(props["data"]) == r.nodes[0]


def test_the_content_hash_is_stable_and_changes_with_content():
    assert pub.content_hash(build()) == pub.content_hash(build())
    assert pub.content_hash(build()) != pub.content_hash(build(extra_cause="x happened."))


# ── Against a real Neo4j ───────────────────────────────────────────────


@pytest.fixture
def store():
    if not os.getenv("NEO4J_URI"):
        if os.getenv("REQUIRE_NEO4J_TESTS") == "1":
            pytest.fail("REQUIRE_NEO4J_TESTS=1 but NEO4J_URI is not set")
        pytest.skip("no Neo4j here: set NEO4J_URI to run the graph tests")
    driver = pub.connect()
    with driver.session() as session:
        session.run("MATCH (n) DETACH DELETE n").consume()
    yield pub.GraphStore(driver)
    driver.close()


def count(store):
    with store.driver.session() as s:
        row = s.run("MATCH (n) OPTIONAL MATCH (n)-[r]->() RETURN count(DISTINCT n) AS n, count(r) AS r").single()
        return row["n"], row["r"]


T1, T2, T3 = (datetime(2026, 11, 30, h, tzinfo=UTC) for h in (9, 10, 11))


def test_a_release_reads_back_equal(store):
    r = build()
    assert store.publish(r, T1)[0] == "published"
    assert store.read() == r
    assert store.read("release-1") == r


def test_publishing_the_same_release_twice_changes_nothing(store):
    r = build()
    store.publish(r, T1)
    before = count(store)
    action, m = store.publish(r, T2)
    assert action == "noop" and m.published_at == T1.isoformat(timespec="seconds")
    assert count(store) == before


def test_the_graph_has_typed_labels_and_relationships(store):
    store.publish(build(), T1)
    with store.driver.session() as s:
        labels = {r["l"] for r in s.run("MATCH (n:Knowledge) UNWIND labels(n) AS l RETURN DISTINCT l AS l")}
        types = {r["t"] for r in s.run("MATCH (:Knowledge)-[r]->(:Knowledge) RETURN DISTINCT type(r) AS t")}
    assert labels == {"Knowledge", *pub.LABELS.values()}
    assert types == set(pub.ENDPOINTS)


def test_a_newer_release_supersedes_and_the_old_one_stays_readable(store):
    first, second = build(), build("release-2", supersedes="release-1", extra_cause="The password expired.")
    store.publish(first, T1)
    store.publish(second, T2)
    assert store.current().release_id == "release-2"
    assert store.read() == second and store.read("release-1") == first
    assert [(m.release_id, m.status) for m in store.history()] == [("release-1", "superseded"), ("release-2", "current")]
    with store.driver.session() as s:
        assert s.run("MATCH (:Release {release_id: 'release-2'})-[:SUPERSEDES]->(o:Release) RETURN o.release_id AS id"
                     ).single()["id"] == "release-1"


def test_overwriting_or_skipping_the_current_release_is_refused(store):
    store.publish(build(), T1)
    with pytest.raises(pub.PublishError, match="different content"):
        store.publish(build(extra_cause="The password expired."), T2)
    with pytest.raises(pub.PublishError, match="supersedes nothing"):
        store.publish(build("release-2"), T2)
    assert [m.release_id for m in store.history()] == ["release-1"]


def test_rollback_restores_the_previous_release_and_deletes_nothing(store):
    store.publish(build(), T1)
    store.publish(build("release-2", supersedes="release-1", extra_cause="The password expired."), T2)
    before = count(store)
    old, now = store.rollback()
    assert (old.release_id, old.status, now.release_id, now.status) == ("release-2", "rolled_back", "release-1", "current")
    assert store.read() == build() and count(store) == before
    with pytest.raises(pub.PublishError, match="first release"):
        store.rollback()
    # After a rollback, the next release supersedes the one that is current again.
    store.publish(build("release-3", supersedes="release-1"), T3)
    assert store.current().release_id == "release-3"


def test_rollback_with_nothing_published_is_refused(store):
    with pytest.raises(pub.PublishError, match="nothing is published"):
        store.rollback()
    with pytest.raises(pub.PublishError, match="nothing is published yet"):
        store.read()


def test_the_cli_publishes_lists_and_rolls_back(store, tmp_path, capsys):
    first, second = tmp_path / "r1.json", tmp_path / "r2.json"
    first.write_text(build().model_dump_json())
    second.write_text(build("release-2", supersedes="release-1", extra_cause="The password expired.").model_dump_json())
    assert pub.main(["publish", str(first)], driver=store.driver) == 0
    assert pub.main(["publish", str(first)], driver=store.driver) == 0
    assert pub.main(["publish", str(second)], driver=store.driver) == 0
    assert pub.main(["history"], driver=store.driver) == 0
    assert pub.main(["rollback"], driver=store.driver) == 0
    out = capsys.readouterr().out
    assert "Published: release-1 [current]" in out and "Already published, nothing changed: release-1" in out
    assert "release-2 [current]" in out and "Rolled back: release-2 [rolled_back]" in out
    assert pub.main(["rollback"], driver=store.driver) == 1
    assert "Refused: release-1 is the first release" in capsys.readouterr().err


def test_a_release_with_unapproved_facts_never_reaches_the_graph(store):
    draft = KnowledgeRelease.model_validate_json(resolve.DRAFT.read_text())
    with pytest.raises(pub.PublishError, match="not approved"):
        store.publish(draft, T1)
    assert count(store) == (0, 0)


def test_every_canonical_issue_can_be_published(store):
    """The committed draft, approved as a whole, publishes and reads back equal."""
    draft = KnowledgeRelease.model_validate_json(resolve.DRAFT.read_text())
    decisions = {i: rv.Recorded(decision=rv.Decision(issue=i, action="approve", reviewer="dpelezo"), decided_on=DAY)
                 for i in ISSUES}
    release, _ = rv.apply(draft, "sha", decisions, "release-all")
    store.publish(release, T1)
    assert store.read() == release
