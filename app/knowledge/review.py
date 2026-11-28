"""Human review of a draft release (issue #28): decisions, and the approved release they produce.

Pure functions with no Temporal and no I/O, so every rule is unit-tested directly. The Temporal
workflow (``app.workflows.review``) only holds the decisions while the reviewer works and
calls these functions; ADR 0001 and ADR 0004 say why it waits in Temporal.

A reviewer decides **per issue**, with the POC's four actions:

- ``approve``: every fact of the issue goes into the release, except any listed in ``exclude``.
- ``edit``: approve with changes: reworded text, and links the extraction could not make
  (a diagnostic ``TESTS_FOR`` a cause, a cause ``MITIGATED_BY`` a solution).
- ``reject``: nothing of the issue goes in.
- ``send_back``: nothing goes in, and the feedback is recorded for the next extraction.

Only approved facts reach the release. Each approved fact's provenance records the reviewer,
the date and the release id, so the published graph can always say who approved what.
"""

from __future__ import annotations

from datetime import date
from enum import StrEnum
from typing import Literal

from pydantic import BaseModel, ConfigDict, Field, model_validator

from app.knowledge.model import (
    ENDPOINTS,
    Id,
    KnowledgeRelease,
    MitigatedBy,
    Provenance,
    ReviewStatus,
    TestsFor,
    Text,
)


class _Frozen(BaseModel):
    model_config = ConfigDict(frozen=True, extra="forbid")


class Action(StrEnum):
    approve = "approve"
    edit = "edit"
    reject = "reject"
    send_back = "send_back"


class TextEdit(_Frozen):
    """New wording for one node. The evidence stays as it was: the source still says what it says."""

    node_id: Id
    text: Text


class Link(_Frozen):
    """A relationship the reviewer adds: diagnostic → cause, or cause → solution."""

    kind: Literal["TESTS_FOR", "MITIGATED_BY"]
    source: Id
    target: Id
    supports_if: Text | None = None
    rules_out_if: Text | None = None

    @model_validator(mode="after")
    def _tests_for_says_what_supports(self) -> Link:
        if self.kind == "TESTS_FOR" and self.supports_if is None:
            raise ValueError("a TESTS_FOR link needs supports_if: the result that supports the cause")
        if self.kind == "MITIGATED_BY" and (self.supports_if or self.rules_out_if):
            raise ValueError("supports_if and rules_out_if belong to TESTS_FOR links only")
        return self


class Decision(_Frozen):
    issue: Id
    action: Action
    reviewer: Text
    feedback: Text | None = None
    edits: list[TextEdit] = Field(default_factory=list)
    links: list[Link] = Field(default_factory=list)
    exclude: list[Id] = Field(default_factory=list)

    @model_validator(mode="after")
    def _fits_the_action(self) -> Decision:
        if self.action is Action.send_back and self.feedback is None:
            raise ValueError("send_back needs feedback: what the next extraction should do differently")
        if self.action is Action.edit and not (self.edits or self.links):
            raise ValueError("edit needs at least one text edit or link; use approve for no changes")
        if self.action is not Action.edit and (self.edits or self.links):
            raise ValueError("text edits and links come with the edit action")
        if self.action in (Action.reject, Action.send_back) and self.exclude:
            raise ValueError("exclude applies to approve and edit; reject and send_back keep nothing")
        return self

    @property
    def approves(self) -> bool:
        return self.action in (Action.approve, Action.edit)


class Recorded(_Frozen):
    """A decision with the date the workflow received it."""

    decision: Decision
    decided_on: date


# ── The draft, as the reviewer sees it ─────────────────────────────────


class DraftIndex(_Frozen):
    """What the workflow needs to check a decision without holding the whole draft."""

    release_id: Id
    sha256: str
    issues: dict[str, list[str]]  # issue id → the ids of its nodes, the issue's own first
    kinds: dict[str, str]  # node id → kind


def owner(node_id: str, issues: list[str]) -> str | None:
    """The issue a node belongs to: resolve names every node ``<issue>--<kind>-<hash>``."""
    for issue in issues:
        if node_id == issue or node_id.startswith(f"{issue}--"):
            return issue
    return None


def index(draft: KnowledgeRelease, sha256: str) -> DraftIndex:
    issue_ids = [n.id for n in draft.nodes if n.kind == "issue"]
    # Longest first, so an issue whose id starts with another's is matched exactly.
    ordered = sorted(issue_ids, key=len, reverse=True)
    issues: dict[str, list[str]] = {i: [i] for i in issue_ids}
    for node in draft.nodes:
        if node.kind != "issue" and (mine := owner(node.id, ordered)):
            issues[mine].append(node.id)
    return DraftIndex(release_id=draft.release_id, sha256=sha256, issues=issues,
                      kinds={n.id: n.kind for n in draft.nodes})


def check(decision: Decision, draft: DraftIndex) -> list[str]:
    """Why a decision cannot apply to this draft; empty when it can."""
    if decision.issue not in draft.issues:
        return [f"{decision.issue} is not an issue in {draft.release_id}"]
    mine = set(draft.issues[decision.issue])
    problems = []
    for edit in decision.edits:
        if edit.node_id not in mine:
            problems.append(f"edit: {edit.node_id} is not part of {decision.issue}")
    for node_id in decision.exclude:
        if node_id == decision.issue:
            problems.append("exclude: the issue itself cannot be excluded; reject it instead")
        elif node_id not in mine:
            problems.append(f"exclude: {node_id} is not part of {decision.issue}")
    for link in decision.links:
        want = ENDPOINTS[link.kind]
        got = (draft.kinds.get(link.source), draft.kinds.get(link.target))
        if link.source not in mine or link.target not in mine:
            problems.append(f"link {link.source} -> {link.target}: both ends must be part of {decision.issue}")
        elif got != want:
            problems.append(f"link {link.kind} must go {want[0]} -> {want[1]}, not {got[0]} -> {got[1]}")
        elif link.source in decision.exclude or link.target in decision.exclude:
            problems.append(f"link {link.source} -> {link.target}: one end is excluded")
    return problems


# ── The approved release ───────────────────────────────────────────────


class IssueOutcome(_Frozen):
    issue: Id
    action: Action | None  # None: no decision before the review finished
    reviewer: str | None = None
    decided_on: date | None = None
    feedback: str | None = None
    nodes: int = 0
    edges: int = 0
    edits: int = 0
    links: int = 0
    excluded: int = 0


class ReviewRecord(_Frozen):
    """What was decided and what it produced. Kept beside the release, and fed back to extraction."""

    release_id: Id
    draft_id: Id
    draft_sha256: str
    outcomes: list[IssueOutcome]

    @property
    def approved_issues(self) -> list[str]:
        return [o.issue for o in self.outcomes if o.action in (Action.approve, Action.edit)]

    @property
    def sent_back(self) -> dict[str, str]:
        return {o.issue: o.feedback or "" for o in self.outcomes if o.action is Action.send_back}


def _approved(provenance: list[Provenance], recorded: Recorded, release_id: str) -> list[Provenance]:
    return [p.model_copy(update={"status": ReviewStatus.approved, "reviewer": recorded.decision.reviewer,
                                 "reviewed_on": recorded.decided_on, "release_id": release_id})
            for p in provenance]


def apply(draft: KnowledgeRelease, draft_sha256: str, decisions: dict[str, Recorded], release_id: str,
          supersedes: str | None = None) -> tuple[KnowledgeRelease, ReviewRecord]:
    """The release holding only what was approved, and the record of every decision."""
    idx = index(draft, draft_sha256)
    problems = [f"{issue}: {p}" for issue, r in decisions.items() for p in check(r.decision, idx)]
    if problems:
        raise ValueError("; ".join(problems))

    issue_ids = list(idx.issues)
    ordered = sorted(issue_ids, key=len, reverse=True)
    kept: dict[str, Recorded] = {}  # node id → the decision that approved it
    nodes = []
    for node in draft.nodes:
        issue = owner(node.id, ordered)
        recorded = decisions.get(issue) if issue else None
        if recorded is None or not recorded.decision.approves or node.id in recorded.decision.exclude:
            continue
        update: dict = {"provenance": _approved(node.provenance, recorded, release_id)}
        for edit in recorded.decision.edits:
            if edit.node_id == node.id:
                update["title" if node.kind == "issue" else "description"] = edit.text
        nodes.append(node.model_copy(update=update))
        kept[node.id] = recorded

    edges = []
    for edge in draft.edges:
        if edge.source in kept and edge.target in kept:
            edges.append(edge.model_copy(update={
                "provenance": _approved(edge.provenance, kept[edge.source], release_id)}))
    by_id = {n.id: n for n in draft.nodes}
    for recorded in decisions.values():
        if recorded.decision.action is not Action.edit:
            continue
        for link in recorded.decision.links:
            # The reviewer asserts the link on the strength of the source node's documentation,
            # so the link cites that documentation and records the reviewer's approval.
            prov = _approved(by_id[link.source].provenance, recorded, release_id)
            if link.kind == "TESTS_FOR":
                edges.append(TestsFor(source=link.source, target=link.target, provenance=prov,
                                      supports_if=link.supports_if, rules_out_if=link.rules_out_if))
            else:
                edges.append(MitigatedBy(source=link.source, target=link.target, provenance=prov))

    release = KnowledgeRelease(release_id=release_id, nodes=nodes, edges=edges, supersedes=supersedes)
    left = release.unpublishable()
    if left:  # cannot happen if the code above is right; checked so it never ships silently
        raise ValueError(f"unapproved facts in the approved release: {', '.join(left[:5])}")

    outcomes = []
    for issue in issue_ids:
        recorded = decisions.get(issue)
        if recorded is None:
            outcomes.append(IssueOutcome(issue=issue, action=None))
            continue
        d = recorded.decision
        mine = set(idx.issues[issue])
        outcomes.append(IssueOutcome(
            issue=issue, action=d.action, reviewer=d.reviewer, decided_on=recorded.decided_on, feedback=d.feedback,
            nodes=sum(n.id in mine for n in nodes), edges=sum(e.source in mine for e in edges),
            edits=len(d.edits), links=len(d.links), excluded=len(d.exclude),
        ))
    record = ReviewRecord(release_id=release_id, draft_id=draft.release_id, draft_sha256=draft_sha256,
                          outcomes=outcomes)
    return release, record
