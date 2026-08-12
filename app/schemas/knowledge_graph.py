"""Response shapes for the claim knowledge graph.

A thin projection of `app.domain.knowledge_graph`, kept separate for the reason
every schema module here is separate: the domain dataclasses are free to change
shape as the builder learns to read more records, and the wire format a screen is
built against is not.

The one editorial decision in this file is that `properties` is an *ordered list*
rather than a mapping. The detail panel reads top to bottom and the order is
meaningful — a coverage node states its outcome before its finding, an asset its
name before the estimate — and a JSON object would leave that ordering to
whichever client last touched it.
"""

from __future__ import annotations

from datetime import datetime

from app.domain.knowledge_graph import ClaimGraph
from app.schemas.common import SchemaBase


class GraphSourceOut(SchemaBase):
    """Where a fact came from. The traceability payload."""

    kind: str
    label: str
    reference: str | None = None
    #: The sentence the value was read out of, where one is held.
    excerpt: str | None = None
    confidence: float | None = None


class GraphPropertyOut(SchemaBase):
    label: str
    value: str
    #: `neutral` | `action` | `accent` | `ok` | `bad`, or absent for plain ink.
    tone: str | None = None


class GraphNodeOut(SchemaBase):
    id: str
    type: str
    category: str
    label: str
    subtitle: str | None = None
    properties: list[GraphPropertyOut]
    confidence: float | None = None
    relevance: str | None = None
    #: `record` | `derived` | `seeded` — see the domain module's docstring.
    provenance: str
    sources: list[GraphSourceOut]
    #: `issue` | `watch`, or absent.
    flag: str | None = None


class GraphEdgeOut(SchemaBase):
    id: str
    source: str
    target: str
    #: The machine verb — `DAMAGED`.
    relationship: str
    #: The same verb as a reading edge label — `damaged`.
    label: str
    confidence: float | None = None
    provenance: str
    sources: list[GraphSourceOut]


class GraphSignalOut(SchemaBase):
    id: str
    label: str
    value: str
    detail: str
    tone: str


class GraphSummaryOut(SchemaBase):
    entity_count: int
    relationship_count: int
    evidence_count: int
    coverage_issues: int
    risk_indicators: int
    related_claims: int
    seeded_entities: int
    signals: list[GraphSignalOut]


class ClaimKnowledgeGraph(SchemaBase):
    claim_reference: str
    generated_at: datetime
    summary: GraphSummaryOut
    nodes: list[GraphNodeOut]
    edges: list[GraphEdgeOut]


def to_knowledge_graph(graph: ClaimGraph) -> ClaimKnowledgeGraph:
    """Project the domain graph onto the wire format.

    Written out rather than relying on `from_attributes` alone: the domain uses
    `StrEnum` members and the wire uses plain strings, and being explicit here is
    what keeps a renamed enum from silently changing the API.
    """
    return ClaimKnowledgeGraph(
        claim_reference=graph.claim_reference,
        generated_at=graph.generated_at,
        summary=GraphSummaryOut(
            entity_count=graph.summary.entity_count,
            relationship_count=graph.summary.relationship_count,
            evidence_count=graph.summary.evidence_count,
            coverage_issues=graph.summary.coverage_issues,
            risk_indicators=graph.summary.risk_indicators,
            related_claims=graph.summary.related_claims,
            seeded_entities=graph.summary.seeded_entities,
            signals=[
                GraphSignalOut(
                    id=signal.id,
                    label=signal.label,
                    value=signal.value,
                    detail=signal.detail,
                    tone=signal.tone,
                )
                for signal in graph.summary.signals
            ],
        ),
        nodes=[
            GraphNodeOut(
                id=node.id,
                type=str(node.type),
                category=str(node.category),
                label=node.label,
                subtitle=node.subtitle,
                properties=[
                    GraphPropertyOut(label=prop.label, value=prop.value, tone=prop.tone)
                    for prop in node.properties
                ],
                confidence=node.confidence,
                relevance=node.relevance,
                provenance=str(node.provenance),
                sources=[_source(source) for source in node.sources],
                flag=str(node.flag) if node.flag is not None else None,
            )
            for node in graph.nodes
        ],
        edges=[
            GraphEdgeOut(
                id=edge.id,
                source=edge.source,
                target=edge.target,
                relationship=edge.relationship,
                label=edge.label,
                confidence=edge.confidence,
                provenance=str(edge.provenance),
                sources=[_source(source) for source in edge.sources],
            )
            for edge in graph.edges
        ],
    )


def _source(source: object) -> GraphSourceOut:
    return GraphSourceOut.model_validate(source)
