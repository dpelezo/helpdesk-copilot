"""Publish approved releases to Neo4j (issue #29, ADR 0002).

**Every release is its own subgraph.** Nodes carry their ``release_id`` and a ``uid`` of
``<release_id>/<id>``, so publishing a new release never touches an older one. One ``:Release``
node per publish is the manifest: content hash, counts, what it supersedes, when it was
published, and whether it is the current release. Exactly one release is current.

- **Idempotent:** publishing a release that is already in the graph with the same content
  changes nothing. The same id with *different* content is refused: a release never changes
  once published, so a correction is a new release.
- **Supersede, never overwrite:** a new release must name the current one in ``supersedes``.
  Publishing it makes it current and marks the old one superseded; the old subgraph stays.
- **Rollback:** the current release is marked rolled back and the one it superseded becomes
  current again. Nothing is deleted.
- **Read back:** ``read`` rebuilds the ``KnowledgeRelease`` from the graph. Each node and edge
  stores its full JSON, so the round trip is exact; typed labels, relationship types and the
  ``text`` property are there for Cypher queries and retrieval (#30, #31).

Only approved facts are published: a release with anything ``unpublishable()`` is refused.
"""

from __future__ import annotations

import argparse
import hashlib
import os
import sys
from dataclasses import dataclass
from datetime import UTC, datetime
from pathlib import Path
from typing import Any

from pydantic import TypeAdapter

from app.knowledge.model import ENDPOINTS, Edge, KnowledgeRelease, Node

LABELS = {"issue": "Issue", "symptom": "Symptom", "cause": "Cause", "diagnostic": "Diagnostic",
          "solution": "Solution", "runbook": "Runbook", "incident": "Incident"}
_NODE = TypeAdapter(Node)
_EDGE = TypeAdapter(Edge)


class PublishError(Exception):
    """A publish, rollback or read the graph's current state does not allow."""


# ── Pure parts: content hash, properties, the decision to publish ─────


def content_hash(release: KnowledgeRelease) -> str:
    return hashlib.sha256(release.model_dump_json().encode()).hexdigest()


def node_props(node: Any, release_id: str, pos: int) -> dict[str, Any]:
    return {
        "uid": f"{release_id}/{node.id}", "id": node.id, "release_id": release_id, "kind": node.kind, "pos": pos,
        "text": getattr(node, "description", None) or getattr(node, "title", None) or "",
        "data": node.model_dump_json(),
    }


def edge_props(edge: Any, release_id: str, pos: int) -> dict[str, Any]:
    return {"source": f"{release_id}/{edge.source}", "target": f"{release_id}/{edge.target}",
            "release_id": release_id, "pos": pos, "data": edge.model_dump_json()}


@dataclass(frozen=True)
class Manifest:
    release_id: str
    sha256: str
    supersedes: str | None
    nodes: int
    edges: int
    published_at: str
    status: str  # "current", "superseded" or "rolled_back"

    @classmethod
    def of(cls, record: dict[str, Any]) -> Manifest:
        return cls(**{k: record[k] for k in cls.__dataclass_fields__})


def decide(release: KnowledgeRelease, existing: Manifest | None, current: Manifest | None) -> str:
    """'noop' if this exact release is already published, 'publish' if it may be. Raises otherwise."""
    left = release.unpublishable()
    if left:
        raise PublishError(f"{len(left)} facts are not approved for {release.release_id}, e.g. {left[0]}")
    duplicates = _duplicate_edges(release)
    if duplicates:
        raise PublishError(f"duplicate relationships: {', '.join(duplicates[:3])}")
    if existing is not None:
        if existing.sha256 == content_hash(release):
            return "noop"
        raise PublishError(f"{release.release_id} is already published with different content; "
                           "a published release never changes, so publish the correction as a new release")
    if current is None:
        if release.supersedes is not None:
            raise PublishError(f"{release.release_id} supersedes {release.supersedes}, but nothing is published yet")
        return "publish"
    if release.supersedes != current.release_id:
        named = release.supersedes or "nothing"
        raise PublishError(f"{release.release_id} supersedes {named}, but the current release is "
                           f"{current.release_id}; name it in supersedes to replace it")
    return "publish"


def _duplicate_edges(release: KnowledgeRelease) -> list[str]:
    seen, dupes = set(), []
    for e in release.edges:
        k = (e.kind, e.source, e.target)
        if k in seen:
            dupes.append(f"{e.kind} {e.source}->{e.target}")
        seen.add(k)
    return dupes


# ── The graph ──────────────────────────────────────────────────────────


SCHEMA = [
    "CREATE CONSTRAINT knowledge_uid IF NOT EXISTS FOR (n:Knowledge) REQUIRE n.uid IS UNIQUE",
    "CREATE CONSTRAINT release_id IF NOT EXISTS FOR (r:Release) REQUIRE r.release_id IS UNIQUE",
    "CREATE INDEX knowledge_release IF NOT EXISTS FOR (n:Knowledge) ON (n.release_id)",
]

_MANIFEST = "r {.release_id, .sha256, .supersedes, .nodes, .edges, .published_at, .status}"


class GraphStore:
    """Versioned knowledge releases in Neo4j. Takes a driver, so tests can point it anywhere."""

    def __init__(self, driver, database: str | None = None):
        self.driver = driver
        self.database = database

    def _run(self, work):
        with self.driver.session(database=self.database) as session:
            return session.execute_write(work)

    def _read(self, work):
        with self.driver.session(database=self.database) as session:
            return session.execute_read(work)

    def setup(self) -> None:
        with self.driver.session(database=self.database) as session:
            for statement in SCHEMA:
                session.run(statement).consume()

    # Manifests

    def manifest(self, release_id: str) -> Manifest | None:
        rows = self._read(lambda tx: tx.run(
            f"MATCH (r:Release {{release_id: $id}}) RETURN {_MANIFEST} AS m", id=release_id).data())
        return Manifest.of(rows[0]["m"]) if rows else None

    def current(self) -> Manifest | None:
        rows = self._read(lambda tx: tx.run(f"MATCH (r:Release {{status: 'current'}}) RETURN {_MANIFEST} AS m").data())
        if len(rows) > 1:
            raise PublishError(f"{len(rows)} releases are marked current; the graph needs repair")
        return Manifest.of(rows[0]["m"]) if rows else None

    def history(self) -> list[Manifest]:
        rows = self._read(lambda tx: tx.run(
            f"MATCH (r:Release) RETURN {_MANIFEST} AS m ORDER BY r.published_at, r.release_id").data())
        return [Manifest.of(r["m"]) for r in rows]

    # Publish and rollback

    def publish(self, release: KnowledgeRelease, now: datetime | None = None) -> tuple[str, Manifest]:
        """Returns ('noop' or 'published', the release's manifest)."""
        self.setup()
        action = decide(release, self.manifest(release.release_id), self.current())
        if action == "noop":
            return "noop", self.manifest(release.release_id)
        published_at = (now or datetime.now(UTC)).isoformat(timespec="seconds")
        nodes = [node_props(n, release.release_id, i) for i, n in enumerate(release.nodes)]
        by_kind: dict[str, list[dict[str, Any]]] = {}
        for i, e in enumerate(release.edges):
            by_kind.setdefault(e.kind, []).append(edge_props(e, release.release_id, i))

        def work(tx):
            # Checked again inside the transaction: another publisher may have won the race.
            again = tx.run("MATCH (r:Release {status: 'current'}) RETURN r.release_id AS id").data()
            now_current = again[0]["id"] if again else None
            if now_current != release.supersedes:
                raise PublishError(f"the current release changed to {now_current} while publishing")
            for kind, label in LABELS.items():
                batch = [n for n in nodes if n["kind"] == kind]
                if batch:
                    tx.run(f"UNWIND $rows AS row CREATE (n:Knowledge:{label}) SET n = row", rows=batch).consume()
            for kind, rows in by_kind.items():
                assert kind in ENDPOINTS  # relationship types come from the model, never from input
                tx.run(
                    "UNWIND $rows AS row "
                    "MATCH (a:Knowledge {uid: row.source}), (b:Knowledge {uid: row.target}) "
                    f"CREATE (a)-[r:{kind}]->(b) SET r.release_id = row.release_id, r.pos = row.pos, r.data = row.data",
                    rows=rows,
                ).consume()
            tx.run("MATCH (r:Release {status: 'current'}) SET r.status = 'superseded'").consume()
            tx.run(
                "CREATE (r:Release {release_id: $id, sha256: $sha, supersedes: $supersedes, nodes: $n, "
                "edges: $e, published_at: $at, status: 'current'})",
                id=release.release_id, sha=content_hash(release), supersedes=release.supersedes,
                n=len(nodes), e=len(release.edges), at=published_at,
            ).consume()
            if release.supersedes:
                tx.run("MATCH (new:Release {release_id: $id}), (old:Release {release_id: $old}) "
                       "CREATE (new)-[:SUPERSEDES]->(old)", id=release.release_id, old=release.supersedes).consume()

        self._run(work)
        return "published", self.manifest(release.release_id)

    def rollback(self) -> tuple[Manifest, Manifest]:
        """Make the release the current one superseded current again. Returns (rolled back, now current)."""
        current = self.current()
        if current is None:
            raise PublishError("nothing is published, so there is nothing to roll back")
        if current.supersedes is None:
            raise PublishError(f"{current.release_id} is the first release; there is no previous one to go back to")

        def work(tx):
            tx.run("MATCH (r:Release {release_id: $id}) SET r.status = 'rolled_back'", id=current.release_id).consume()
            tx.run("MATCH (r:Release {release_id: $id}) SET r.status = 'current'", id=current.supersedes).consume()

        self._run(work)
        return self.manifest(current.release_id), self.manifest(current.supersedes)

    # Read back

    def read(self, release_id: str | None = None) -> KnowledgeRelease:
        manifest = self.manifest(release_id) if release_id else self.current()
        if manifest is None:
            raise PublishError(f"no release {release_id}" if release_id else "nothing is published yet")

        def work(tx):
            nodes = tx.run("MATCH (n:Knowledge {release_id: $id}) RETURN n.data AS data ORDER BY n.pos",
                           id=manifest.release_id).data()
            edges = tx.run("MATCH (:Knowledge {release_id: $id})-[r]->(:Knowledge {release_id: $id}) "
                           "RETURN r.data AS data ORDER BY r.pos", id=manifest.release_id).data()
            return nodes, edges

        nodes, edges = self._read(work)
        if (len(nodes), len(edges)) != (manifest.nodes, manifest.edges):
            raise PublishError(f"{manifest.release_id}: the graph holds {len(nodes)} nodes and {len(edges)} "
                               f"relationships, its manifest says {manifest.nodes} and {manifest.edges}")
        release = KnowledgeRelease(
            release_id=manifest.release_id, supersedes=manifest.supersedes,
            nodes=[_NODE.validate_json(r["data"]) for r in nodes],
            edges=[_EDGE.validate_json(r["data"]) for r in edges],
        )
        if content_hash(release) != manifest.sha256:
            raise PublishError(f"{manifest.release_id}: what the graph holds does not match its manifest")
        return release


def connect():
    """A driver from NEO4J_URI, NEO4J_USER and NEO4J_PASSWORD (AuraDB or the local container)."""
    from neo4j import GraphDatabase

    uri = os.environ.get("NEO4J_URI")
    if not uri:
        raise PublishError("NEO4J_URI is not set")
    return GraphDatabase.driver(uri, auth=(os.environ.get("NEO4J_USER", "neo4j"), os.environ.get("NEO4J_PASSWORD", "")))


def _show(m: Manifest) -> str:
    since = f", supersedes {m.supersedes}" if m.supersedes else ""
    return f"{m.release_id} [{m.status}] {m.nodes} nodes, {m.edges} relationships, published {m.published_at}{since}"


def main(argv: list[str] | None = None, driver=None) -> int:
    parser = argparse.ArgumentParser(description="Publish approved knowledge releases to Neo4j.")
    sub = parser.add_subparsers(dest="command", required=True)
    sub.add_parser("publish", help="Publish an approved release file.").add_argument("release", type=Path)
    sub.add_parser("rollback", help="Make the previous release current again.")
    sub.add_parser("history", help="List every published release.")
    args = parser.parse_args(argv)
    owned = driver is None
    driver = driver or connect()
    try:
        store = GraphStore(driver, os.environ.get("NEO4J_DATABASE"))
        if args.command == "publish":
            release = KnowledgeRelease.model_validate_json(args.release.read_text())
            action, manifest = store.publish(release)
            print(("Already published, nothing changed: " if action == "noop" else "Published: ") + _show(manifest))
        elif args.command == "rollback":
            old, now = store.rollback()
            print(f"Rolled back: {_show(old)}\nCurrent: {_show(now)}")
        else:
            for m in store.history():
                print(_show(m))
    except PublishError as error:
        print(f"Refused: {error}", file=sys.stderr)
        return 1
    finally:
        if owned:
            driver.close()
    return 0


if __name__ == "__main__":
    sys.exit(main())

