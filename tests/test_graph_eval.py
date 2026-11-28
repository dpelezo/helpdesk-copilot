"""Condition B (#31): the catalog graph, entry linking, traversal and the run. No live model.

The graph tests need Neo4j (``NEO4J_URI``); CI provides it and requires them.
"""

import hashlib
import json
import os

import pytest

from app.evals import graph as gb
from app.knowledge import catalog_graph, resolve
from app.knowledge import publish as pub
from app.knowledge.corpus import documents
from app.llm import Reply
from app.mock.faults import CAUSES, ISSUES


class Words:
    """Deterministic stand-in embeddings; also usable as a CachedEmbedder (callable)."""

    model = "fake-embed"
    cached = embedded = 0

    def __call__(self, texts):
        out = []
        for text in texts:
            v = [0.0] * 64
            for w in text.lower().replace("\n", " ").split():
                v[int(hashlib.md5(w.strip(".,:?!()").encode()).hexdigest(), 16) % 64] += 1.0
            v[-1] += 0.01
            out.append(v)
        return out


# ── The catalog graph ──────────────────────────────────────────────────


def test_the_catalog_graph_holds_every_issue_cause_and_fix():
    r = catalog_graph.build()
    kinds = [n.kind for n in r.nodes]
    assert kinds.count("issue") == 13 and kinds.count("cause") == 36 and kinds.count("solution") == 36
    assert {n.id for n in r.nodes if n.kind == "cause"} == {c.id for c in CAUSES}
    assert {e.kind for e in r.edges} == {"HAS_POSSIBLE_CAUSE", "MITIGATED_BY", "TESTS_FOR"}
    assert sum(e.kind == "TESTS_FOR" for e in r.edges) == sum(len(c.reveals) for c in CAUSES)


def test_it_states_the_same_facts_as_the_baselines_documents():
    """Only retrieval may differ between conditions A and B, so the facts must match."""
    r = catalog_graph.build()
    by_id = {n.id: n for n in r.nodes}
    for c in CAUSES:
        flat = next(d.text for d in documents() if d.id == f"cause:{c.id}")
        assert c.summary in flat and c.fix in flat
        assert by_id[c.id].description == c.summary and by_id[f"fix-{c.id}"].description == c.fix
        assert by_id[c.issue].title == ISSUES[c.issue]


def test_it_is_approved_valid_and_never_changes():
    r = catalog_graph.build()
    assert r.unpublishable() == []
    assert resolve.validate(r, resolve.registered_tools()) == []
    assert pub.content_hash(r) == pub.content_hash(catalog_graph.build())


def test_nothing_in_the_graph_names_a_ticket_or_a_planted_cause():
    text = catalog_graph.build().model_dump_json().lower()
    assert "ho-" not in text and "tu-" not in text and "planted" not in text


def test_entry_nodes_map_to_their_issues_in_rank_order():
    owner = {"mfa-locked": "identity--mfa-lockout", "locked-failed-sign-ins": "identity--account-locked"}
    entry = ["locked-failed-sign-ins", "mfa-locked", "identity--account-locked", "network--vpn-not-connecting"]
    assert gb.issues_for(entry, owner) == ["identity--account-locked", "identity--mfa-lockout"]
    assert gb.issues_for(entry, owner, limit=3)[-1] == "network--vpn-not-connecting"


def test_the_settings_are_the_ones_fixed_before_the_run():
    assert (gb.ENTRY_K, gb.MAX_ISSUES, gb.POLICY_K) == (5, 2, 2)


# ── Against Neo4j ──────────────────────────────────────────────────────


@pytest.fixture
def store():
    if not os.getenv("NEO4J_URI"):
        if os.getenv("REQUIRE_NEO4J_TESTS") == "1":
            pytest.fail("REQUIRE_NEO4J_TESTS=1 but NEO4J_URI is not set")
        pytest.skip("no Neo4j here: set NEO4J_URI to run the graph tests")
    driver = pub.connect()
    with driver.session() as session:
        session.run("MATCH (n) DETACH DELETE n").consume()
    s = pub.GraphStore(driver)
    yield s
    driver.close()


@pytest.fixture
def retriever(store):
    gb.prepare(store, Words(), "fake-embed")
    return gb.GraphRetriever(store, Words(), "fake-embed")


def test_prepare_publishes_and_embeds_once(store):
    gb.prepare(store, Words(), "fake-embed")
    gb.prepare(store, Words(), "fake-embed")  # a second run changes nothing
    [m] = store.history()
    assert (m.release_id, m.embedding_model, m.nodes) == ("eval-catalog", "fake-embed", len(catalog_graph.build().nodes))


def test_prepare_refuses_a_graph_holding_product_releases(store):
    from tests.test_publish import build

    store.publish(build())
    with pytest.raises(pub.PublishError, match="never on the product's"):
        gb.prepare(store, Words(), "fake-embed")


def test_retrieval_shows_every_cause_of_the_linked_issues_with_fix_and_checks(retriever):
    got = retriever.retrieve("My VPN certificate expired and the VPN will not connect")
    assert got.issues[0] == "network--vpn-not-connecting" and len(got.issues) <= 2
    vpn_causes = {f"cause:{c.id}" for c in CAUSES if c.issue == "network--vpn-not-connecting"}
    assert vpn_causes <= set(got.shown)
    assert "Possible cause (vpn-certificate-expired)" in got.text
    assert "Revealed by: vpn_certificate_status, vpn_last_session." in got.text
    assert "Fix: Issue a new VPN client certificate." in got.text
    assert sum(s.startswith("policy:") for s in got.shown) == 2


def test_a_run_scores_like_the_baseline(retriever):
    class FirstShown:
        model = "fake-chat"

        def complete(self, messages, tools=None, *, max_tokens=None):
            first = messages[1]["content"].split("Possible cause (")[1].split(")")[0]
            return Reply({"content": json.dumps({"cause": first, "reason": "first"})},
                         {"prompt_tokens": 10, "completion_tokens": 2})

    result = gb.evaluate(FirstShown(), retriever, "tuning")
    assert result["condition"].startswith("B:") and result["split"] == "tuning"
    assert 0 <= result["score"]["root_cause_accuracy"] <= 100
    assert result["right_cause_shown"] >= 50 and result["score"]["errors"] == 0
    run = result["runs"][0]
    assert run["entry"] and run["issues"] and run["retrieved"]


def test_an_unparseable_reply_is_scored_wrong_not_dropped(retriever):
    class Garbage:
        model = "fake-chat"

        def complete(self, messages, tools=None, *, max_tokens=None):
            return Reply({"content": "I think it is the network"}, finish_reason="stop")

    result = gb.evaluate(Garbage(), retriever, "tuning")
    assert result["score"]["errors"] == len(result["runs"]) and result["score"]["root_cause_accuracy"] == 0
