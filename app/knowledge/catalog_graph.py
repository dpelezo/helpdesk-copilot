"""The cause catalog as a knowledge graph, for condition B (issue #31).

Condition B must know exactly what the baseline knew, so only retrieval differs. The
baseline searched one flat document per catalog cause (summary and fix) and per handbook
section (``app.knowledge.corpus``). This release holds the same causes and fixes, arranged
as the graph the copilot reads:

    (:Issue)-[:HAS_POSSIBLE_CAUSE]->(:Cause)-[:MITIGATED_BY]->(:Solution)
    (:Diagnostic)-[:TESTS_FOR]->(:Cause)        one Diagnostic per read-only tool

Cause node ids are the catalog ids, so the model answers in the scorer's terms. The
``TESTS_FOR`` links say which check would reveal each cause (``Cause.reveals``): structure
the flat documents did not have, which is what condition B is meant to add. Nothing says
which cause is planted in a ticket.

The knowledge model has no node for a policy section, so the handbook stays a flat list
beside the graph (see ``app.evals.graph``).

This is an eval fixture, generated from code: its provenance names the catalog and says it
was generated, and it is published only into a throwaway graph inside the eval job, never
into the product's release chain.
"""

from __future__ import annotations

from datetime import date

from app.knowledge.model import (
    Cause,
    Diagnostic,
    HasPossibleCause,
    Issue,
    KnowledgeRelease,
    MitigatedBy,
    Provenance,
    ReviewStatus,
    Solution,
    TestsFor,
)
from app.mock.faults import CAUSES, ISSUES

RELEASE_ID = "eval-catalog"
SOURCE_URL = "https://github.com/dpelezo/helpdesk-copilot/blob/main/app/mock/faults.py"
GENERATED_BY = "generated from the cause catalog (eval fixture)"
FIXED_ON = date(2026, 11, 28)  # constant, so the release and its hash never drift


def diagnostic_id(tool: str) -> str:
    return f"check-{tool.replace('_', '-')}"


def _prov(evidence: str) -> list[Provenance]:
    return [Provenance(source_id="cause-catalog", source_url=SOURCE_URL, evidence_span=evidence,
                       status=ReviewStatus.approved, reviewer=GENERATED_BY, reviewed_on=FIXED_ON,
                       release_id=RELEASE_ID)]


def build() -> KnowledgeRelease:
    nodes: list = [Issue(id=i, title=title, category=i.split("--")[0].replace("-", " ").title(), seed_id=None,
                         provenance=_prov(title)) for i, title in ISSUES.items()]
    edges: list = []
    tools = sorted({t for c in CAUSES for t in c.reveals})
    nodes += [Diagnostic(id=diagnostic_id(t), description=f"Run {t} for the employee.", tool=t,
                         provenance=_prov(t)) for t in tools]
    for c in CAUSES:
        fix_id = f"fix-{c.id}"
        nodes.append(Cause(id=c.id, description=c.summary, provenance=_prov(c.summary)))
        nodes.append(Solution(id=fix_id, description=c.fix, provenance=_prov(c.fix)))
        edges.append(HasPossibleCause(source=c.issue, target=c.id, provenance=_prov(c.summary)))
        edges.append(MitigatedBy(source=c.id, target=fix_id, provenance=_prov(c.fix)))
        edges += [TestsFor(source=diagnostic_id(t), target=c.id, provenance=_prov(t),
                           supports_if=f"{t} differs from a healthy record") for t in c.reveals]
    return KnowledgeRelease(release_id=RELEASE_ID, nodes=nodes, edges=edges)
