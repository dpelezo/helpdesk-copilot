"""The knowledge model: typed nodes, typed relationships, and a release that checks them.

Workflow A builds a ``KnowledgeRelease`` and publishes it; Workflow B only reads
published releases (ADR 0001). Every node and every relationship carries
provenance, so any fact the copilot shows can be traced to its source.

Possible causes and confirmed causes are different types on purpose:
``HasPossibleCause`` links an issue to a hypothesis taken from documentation,
and ``ConfirmedCause`` links a closed incident to the cause that was verified.
One never turns into the other.
"""

from __future__ import annotations

from collections import Counter
from datetime import date
from enum import StrEnum
from typing import Annotated, Literal

from pydantic import (
    AnyHttpUrl,
    BaseModel,
    ConfigDict,
    Field,
    StringConstraints,
    model_validator,
)

Id = Annotated[str, StringConstraints(pattern=r"^[a-z0-9]+(?:-{1,2}[a-z0-9]+)*$", max_length=120)]
Text = Annotated[str, StringConstraints(strip_whitespace=True, min_length=1)]
ToolName = Annotated[str, StringConstraints(pattern=r"^[a-z][a-z0-9_]*$", max_length=64)]


class _Frozen(BaseModel):
    model_config = ConfigDict(frozen=True, extra="forbid")


class ReviewStatus(StrEnum):
    draft = "draft"
    approved = "approved"
    rejected = "rejected"
    superseded = "superseded"


class Provenance(_Frozen):
    """Where a fact came from, and who approved it for which release."""

    source_id: Id
    source_url: AnyHttpUrl
    # The exact words in the source that support the fact. Never empty: a fact
    # with nothing to point at is not published.
    evidence_span: Text
    source_version: str | None = None
    status: ReviewStatus = ReviewStatus.draft
    reviewer: str | None = None
    reviewed_on: date | None = None
    release_id: Id | None = None

    @model_validator(mode="after")
    def _approval_is_complete(self) -> Provenance:
        if self.status is ReviewStatus.approved:
            missing = [
                name
                for name in ("reviewer", "reviewed_on", "release_id")
                if getattr(self, name) is None
            ]
            if missing:
                raise ValueError(f"an approved fact needs {', '.join(missing)}")
        return self


Sources = Annotated[list[Provenance], Field(min_length=1)]


# ── Nodes ──────────────────────────────────────────────────────────────


class _Node(_Frozen):
    id: Id
    provenance: Sources


class Issue(_Node):
    kind: Literal["issue"] = "issue"
    title: Text
    category: Text
    # The canonical issue id from reports/seed_profile.md, when the issue came from the seed.
    seed_id: Id | None = None


class Symptom(_Node):
    kind: Literal["symptom"] = "symptom"
    description: Text


class Cause(_Node):
    """A cause as a thing in the world. Whether it is possible or confirmed is a relationship."""

    kind: Literal["cause"] = "cause"
    description: Text


class Diagnostic(_Node):
    """A check the copilot can run, through one registered read-only tool."""

    kind: Literal["diagnostic"] = "diagnostic"
    description: Text
    tool: ToolName
    # Fixed arguments; per-ticket values (employee, device) are filled at run time.
    arguments: dict[str, str | int | bool] = Field(default_factory=dict)
    cost: Annotated[int, Field(ge=1, le=10)] = 1
    risk: Literal["none", "low"] = "none"


class Solution(_Node):
    kind: Literal["solution"] = "solution"
    description: Text
    prerequisites: list[Text] = Field(default_factory=list)


class RunbookStep(_Frozen):
    order: Annotated[int, Field(ge=1)]
    instruction: Text
    tool: ToolName | None = None
    changes_account: bool = False

    @model_validator(mode="after")
    def _account_change_needs_a_tool(self) -> RunbookStep:
        if self.changes_account and self.tool is None:
            raise ValueError("a step that changes an account must name the registered tool")
        return self


class Runbook(_Node):
    kind: Literal["runbook"] = "runbook"
    title: Text
    steps: Annotated[list[RunbookStep], Field(min_length=1)]

    @property
    def requires_approval(self) -> bool:
        """True when any step changes an account (ADR 0001: the agent must approve)."""
        return any(step.changes_account for step in self.steps)

    @model_validator(mode="after")
    def _steps_in_order(self) -> Runbook:
        if [s.order for s in self.steps] != list(range(1, len(self.steps) + 1)):
            raise ValueError("runbook steps must be numbered 1..n in order")
        return self


class Incident(_Node):
    """A closed ticket whose cause was verified. Its provenance points at the ticket."""

    kind: Literal["incident"] = "incident"
    ticket_id: Text
    closed_on: date


Node = Annotated[
    Issue | Symptom | Cause | Diagnostic | Solution | Runbook | Incident,
    Field(discriminator="kind"),
]


# ── Relationships ──────────────────────────────────────────────────────


class _Edge(_Frozen):
    source: Id
    target: Id
    provenance: Sources


class HasSymptom(_Edge):
    """Issue → Symptom."""

    kind: Literal["HAS_SYMPTOM"] = "HAS_SYMPTOM"


class HasPossibleCause(_Edge):
    """Issue → Cause. A hypothesis taken from documentation, never a verified fact."""

    kind: Literal["HAS_POSSIBLE_CAUSE"] = "HAS_POSSIBLE_CAUSE"


class TestsFor(_Edge):
    """Diagnostic → Cause, with the results that support or rule out the cause."""

    kind: Literal["TESTS_FOR"] = "TESTS_FOR"
    supports_if: Text
    rules_out_if: Text | None = None


class MitigatedBy(_Edge):
    """Cause → Solution."""

    kind: Literal["MITIGATED_BY"] = "MITIGATED_BY"


class ImplementedBy(_Edge):
    """Solution → Runbook."""

    kind: Literal["IMPLEMENTED_BY"] = "IMPLEMENTED_BY"


class ConfirmedCause(_Edge):
    """Incident → Cause. A verified outcome, kept apart from the hypotheses."""

    kind: Literal["CONFIRMED_CAUSE"] = "CONFIRMED_CAUSE"
    verified_by: Text
    verified_on: date


Edge = Annotated[
    HasSymptom | HasPossibleCause | TestsFor | MitigatedBy | ImplementedBy | ConfirmedCause,
    Field(discriminator="kind"),
]

# Which node kinds each relationship may connect: (source kind, target kind).
ENDPOINTS: dict[str, tuple[str, str]] = {
    "HAS_SYMPTOM": ("issue", "symptom"),
    "HAS_POSSIBLE_CAUSE": ("issue", "cause"),
    "TESTS_FOR": ("diagnostic", "cause"),
    "MITIGATED_BY": ("cause", "solution"),
    "IMPLEMENTED_BY": ("solution", "runbook"),
    "CONFIRMED_CAUSE": ("incident", "cause"),
}


# ── Release ────────────────────────────────────────────────────────────


class KnowledgeRelease(_Frozen):
    """A versioned set of nodes and relationships. Published releases hold only approved facts."""

    release_id: Id
    nodes: list[Node]
    edges: list[Edge]
    supersedes: Id | None = None

    @model_validator(mode="after")
    def _consistent(self) -> KnowledgeRelease:
        problems: list[str] = []
        duplicates = sorted(i for i, n in Counter(n.id for n in self.nodes).items() if n > 1)
        if duplicates:
            problems.append(f"duplicate node ids: {', '.join(duplicates)}")
        kinds = {n.id: n.kind for n in self.nodes}
        for edge in self.edges:
            want = ENDPOINTS[edge.kind]
            got = (kinds.get(edge.source), kinds.get(edge.target))
            if got != want:
                problems.append(
                    f"{edge.kind} {edge.source} -> {edge.target}: expected {want[0]} -> "
                    f"{want[1]}, found {got[0] or 'missing'} -> {got[1] or 'missing'}"
                )
        if problems:
            raise ValueError("; ".join(problems))
        return self

    def unpublishable(self) -> list[str]:
        """Facts that block publishing: anything not approved for this release."""
        found = []
        for item in [*self.nodes, *self.edges]:
            label = item.id if isinstance(item, _Node) else f"{item.kind} {item.source}->{item.target}"
            for p in item.provenance:
                if p.status is not ReviewStatus.approved or p.release_id != self.release_id:
                    found.append(f"{label} ({p.source_id}: {p.status.value})")
        return found
