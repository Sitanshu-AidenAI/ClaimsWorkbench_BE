"""The claim knowledge graph — a claim's facts as entities, and how they connect.

A claim record is a form: forty columns, four related tables and a folder of
documents. A handler reading it has to hold the join in their head — that *this*
asset was damaged by *that* incident, which happened at a location the policy may
or may not schedule, and that the figure in the estimate came off page two of an
attachment. This module does that join once, on the server, and states it as a
graph.

Three rules the shape encodes:

1. **Every node and edge says where it came from.** `provenance` distinguishes a
   value read off a column (`record`) from one a rule computed (`derived`) from
   one standing in for a feed that is not wired yet (`seeded`). `sources` carries
   the document, the excerpt and the confidence behind it. A graph that asserted
   `Incident DAMAGED Bay 3` without being able to say which file said so would be
   a diagram, not intelligence.

2. **The builder is pure.** It takes records in and returns a graph; it opens no
   session and calls no service. That is what lets the route stay four lines, and
   what will let an extraction pipeline feed the same builder later.

3. **Nothing is invented silently.** Where the record set has no source for an
   entity the graph would be poorer without — a loss adjuster before one is
   instructed, an SIU indicator before the feed lands — the entity is emitted
   with `provenance=SEEDED` and a source that names what will replace it. The
   screen states that marker. The alternative, quietly presenting scaffolding as
   a finding, is the one failure a claims desk cannot absorb.

The categories are the axis the UI filters on, and they are chosen the way a
handler reads a claim rather than the way the tables are normalised: who is
involved, what the contract says, what happened, what it damaged, what proves it,
what it costs, what is risky, and what else looks like it.
"""

from __future__ import annotations

import re
from collections.abc import Iterable, Sequence
from dataclasses import dataclass, field
from datetime import UTC, date, datetime
from enum import StrEnum
from typing import Any

# ---------------------------------------------------------------------------
# Vocabulary
# ---------------------------------------------------------------------------


class NodeType(StrEnum):
    """What an entity *is*. Drives the glyph and the detail panel's heading."""

    CLAIM = "claim"
    POLICY = "policy"
    INSURED = "insured"
    INCIDENT = "incident"
    LOCATION = "location"
    ASSET = "asset"
    COVERAGE = "coverage"
    EVIDENCE = "evidence"
    BROKER = "broker"
    HANDLER = "handler"
    ADJUSTER = "adjuster"
    CAT_EVENT = "cat_event"
    FRAUD_INDICATOR = "fraud_indicator"
    RELATED_CLAIM = "related_claim"


class NodeCategory(StrEnum):
    """The filter axis, and the colour family. One category, one meaning."""

    CLAIM = "claim"
    PARTIES = "parties"
    POLICY = "policy"
    INCIDENT = "incident"
    ASSETS = "assets"
    EVIDENCE = "evidence"
    RISK = "risk"
    RELATED = "related"


class Provenance(StrEnum):
    """Where a node or edge got its truth."""

    #: Straight off a column the desk already trusts.
    RECORD = "record"
    #: Computed by a deterministic rule from records — a match, a check, a split.
    DERIVED = "derived"
    #: Demo scaffolding, standing in for a feed that is not wired yet.
    SEEDED = "seeded"


class Flag(StrEnum):
    """An entity the handler should look at, and how hard."""

    #: Needs a decision before settlement — a failed check, an open exception.
    ISSUE = "issue"
    #: Worth knowing, not blocking — a partial match, a low-confidence read.
    WATCH = "watch"


#: Relationship verbs, as the graph labels them. Held as a mapping rather than as
#: an enum because the UI needs the human phrasing beside the machine one, and the
#: two have to change together.
RELATIONSHIPS: dict[str, str] = {
    "COVERED_BY": "covered by",
    "ISSUED_TO": "issued to",
    "REPORTED_BY": "reported by",
    "CAUSED_BY": "caused by",
    "OCCURRED_AT": "occurred at",
    "DAMAGED": "damaged",
    "COVERED_UNDER": "covered under",
    "SUPPORTED_BY": "supported by",
    "ASSIGNED_TO": "assigned to",
    "INSPECTED_BY": "inspected by",
    "RELATED_TO": "related to",
    "MATCHES": "matches",
    "CONSIDERED": "considered",
    "HAS_INDICATOR": "has indicator",
    "SCHEDULES": "schedules",
    "EVIDENCES": "evidences",
    "INSURES": "insures",
}


# ---------------------------------------------------------------------------
# Shapes
# ---------------------------------------------------------------------------


@dataclass(frozen=True, slots=True)
class GraphSource:
    """Where a fact came from, in enough detail for a handler to go and check.

    `excerpt` is the sentence the value was read out of. It is the difference
    between "the estimate is £77,190" and "the estimate is £77,190, and here is
    the line of the CSV that says so".
    """

    kind: str
    label: str
    reference: str | None = None
    excerpt: str | None = None
    confidence: float | None = None


@dataclass(frozen=True, slots=True)
class GraphProperty:
    """One attribute row in the detail panel. Ordered by the server."""

    label: str
    value: str
    #: `neutral` | `action` | `accent` | `ok` | `bad` — the tone vocabulary the UI
    #: already speaks. Absent means plain ink.
    tone: str | None = None


@dataclass(slots=True)
class GraphNode:
    id: str
    type: NodeType
    category: NodeCategory
    label: str
    subtitle: str | None = None
    properties: list[GraphProperty] = field(default_factory=list)
    confidence: float | None = None
    #: Why this entity bears on the claim, in one sentence.
    relevance: str | None = None
    provenance: Provenance = Provenance.RECORD
    sources: list[GraphSource] = field(default_factory=list)
    flag: Flag | None = None


@dataclass(slots=True)
class GraphEdge:
    id: str
    source: str
    target: str
    relationship: str
    label: str
    confidence: float | None = None
    provenance: Provenance = Provenance.RECORD
    sources: list[GraphSource] = field(default_factory=list)


@dataclass(frozen=True, slots=True)
class GraphSignal:
    """One line of the intelligence summary — a figure with its reason.

    Deliberately not a percentage in a badge on its own: a confidence with no
    statement of what it is a confidence *in* is a number a demo can show and a
    handler cannot use.
    """

    id: str
    label: str
    value: str
    detail: str
    tone: str


@dataclass(slots=True)
class GraphSummary:
    entity_count: int
    relationship_count: int
    evidence_count: int
    coverage_issues: int
    risk_indicators: int
    related_claims: int
    #: How much of the graph is scaffolding. Shown, never hidden.
    seeded_entities: int
    signals: list[GraphSignal] = field(default_factory=list)


@dataclass(slots=True)
class ClaimGraph:
    claim_reference: str
    generated_at: datetime
    nodes: list[GraphNode]
    edges: list[GraphEdge]
    summary: GraphSummary


@dataclass(slots=True)
class GraphInput:
    """Everything the builder reads, gathered by the route.

    A dataclass rather than fifteen keyword arguments so the route's gather step
    and the builder's signature cannot drift apart, and so a test can construct
    a partial claim — no policy, no documents — by leaving fields at their
    defaults.
    """

    claim: Any
    case: Any | None = None
    policy: Any | None = None
    documents: Sequence[Any] = ()
    fields: Sequence[Any] = ()
    exceptions: Sequence[Any] = ()
    parties: Sequence[Any] = ()
    duplicates: Sequence[Any] = ()
    policy_matches: Sequence[Any] = ()
    analyses: dict[str, Any] = field(default_factory=dict)
    cat_event: Any | None = None
    cat_candidates: Sequence[Any] = ()
    related_claims: Sequence[Any] = ()
    assignment: Any | None = None
    triage: Any | None = None
    #: Emit the scaffolding entities described in this module's docstring.
    include_seeded: bool = True


# ---------------------------------------------------------------------------
# Formatting
# ---------------------------------------------------------------------------

_SYMBOLS = {"GBP": "£", "USD": "$", "EUR": "€", "SGD": "S$"}


def _money(amount_minor: int | None, currency: str | None) -> str | None:
    """`£128,000`. Full precision, unlike the queue's tiles.

    A detail panel is read one figure at a time rather than compared down a
    column, and `£128k` in a panel loses the £000 a reserve decision turns on.
    """
    if amount_minor is None:
        return None
    symbol = _SYMBOLS.get((currency or "GBP").upper(), "")
    return f"{symbol}{amount_minor / 100:,.0f}"


def _day(value: datetime | date | None) -> str | None:
    """`03 Aug 2026`."""
    if value is None:
        return None
    return f"{value:%d %b %Y}"


def _sentence(value: str | None) -> str | None:
    """Title-case a snake_case enum for display — `fire` → `Fire`."""
    if not value:
        return None
    return value.replace("_", " ").strip().capitalize()


def _percent(value: float | None) -> str | None:
    if value is None:
        return None
    return f"{round(value * 100)}%"


def _truncate(value: str | None, limit: int = 240) -> str | None:
    if not value:
        return None
    collapsed = " ".join(value.split())
    if len(collapsed) <= limit:
        return collapsed
    return collapsed[: limit - 1].rstrip() + "…"


#: Splits `warehouse bay 3, roof panels, racking and palletised stock` into the
#: four things a loss adjuster would list separately. Commas and a trailing "and"
#: only — an asset description is a list, and anything cleverer than this would
#: start splitting "plant and machinery" into two.
_ASSET_SPLIT = re.compile(r",|\band\b", re.IGNORECASE)


def _split_assets(value: str | None, limit: int = 6) -> list[str]:
    if not value:
        return []
    parts = [part.strip(" .;") for part in _ASSET_SPLIT.split(value)]
    return [part[:1].upper() + part[1:] for part in parts if len(part) > 2][:limit]


#: The states `app.domain.assessment` produces for a coverage check. `attention`
#: is the one that matters and the easy one to miss: it is not a failure, it is the
#: assessment saying a human has to look — which is exactly what the graph should
#: draw attention to, and what the coverage-issues count is counting.
_CHECK_TONE = {
    "pass": "ok",
    "attention": "accent",
    "warn": "accent",
    "fail": "bad",
    "unknown": "neutral",
}

#: How a check's state reads on screen. `attention` as a bare word looks like a
#: label rather than a verdict.
_CHECK_OUTCOME = {
    "pass": "Passed",
    "attention": "Needs a look",
    "warn": "Needs a look",
    "fail": "Failed",
    "unknown": "Not established",
}


def _tone_for_state(state: str | None) -> str:
    return _CHECK_TONE.get(state or "", "neutral")


def _indicator_severity(indicator: dict[str, Any]) -> str:
    """How hard a fraud indicator should shout.

    The screening model states a `weight` rather than a severity, and defaulting
    every indicator to `medium` would flatten a 0.6 duplicate-submission signal
    into the same rose as a 0.1 timing note. The thresholds are the model's own
    banding, read off `app.domain.heuristics`.
    """
    stated = indicator.get("severity") or indicator.get("level")
    if stated:
        return str(stated)
    weight = indicator.get("weight")
    if not isinstance(weight, (int, float)):
        return "medium"
    if weight >= 0.5:
        return "high"
    return "medium" if weight >= 0.25 else "low"


def _incident_label(cause: str | None, when: datetime | date | None) -> str:
    """`Fire — 03 Aug 2026`. The peril and the date, which is how a desk names one."""
    return f"{_sentence(cause) or 'Incident'} — {_day(when) or 'date unknown'}"


# ---------------------------------------------------------------------------
# The builder
# ---------------------------------------------------------------------------


class _Builder:
    """Accumulates nodes and edges while the sections below run in order.

    A class rather than a chain of functions returning tuples: every section
    needs to know whether the node it wants to attach to actually exists (a claim
    with no matched policy has no policy node, and the coverage section must not
    emit a dangling edge), and an instance with `has()` reads better than
    threading a set through nine functions.
    """

    def __init__(self, data: GraphInput) -> None:
        self.data = data
        self.nodes: list[GraphNode] = []
        self.edges: list[GraphEdge] = []
        self._ids: set[str] = set()

    # -- primitives --------------------------------------------------------

    def add(self, node: GraphNode) -> str:
        if node.id in self._ids:
            return node.id
        self.nodes.append(node)
        self._ids.add(node.id)
        return node.id

    def has(self, node_id: str | None) -> bool:
        return bool(node_id) and node_id in self._ids

    def link(
        self,
        source: str | None,
        relationship: str,
        target: str | None,
        *,
        confidence: float | None = None,
        provenance: Provenance = Provenance.RECORD,
        sources: Iterable[GraphSource] = (),
    ) -> None:
        """Join two entities, silently skipping an end that does not exist.

        Skipping rather than raising is deliberate: the sections are written as
        "if the record says so, state it", and a claim missing its policy match
        is an ordinary claim rather than a programming error.
        """
        if not self.has(source) or not self.has(target) or source == target:
            return
        self.edges.append(
            GraphEdge(
                id=f"e:{relationship.lower()}:{source}:{target}",
                source=str(source),
                target=str(target),
                relationship=relationship,
                label=RELATIONSHIPS.get(relationship, relationship.replace("_", " ").lower()),
                confidence=confidence,
                provenance=provenance,
                sources=list(sources),
            )
        )

    # -- source helpers ----------------------------------------------------

    def notification_source(self, excerpt: str | None = None) -> GraphSource:
        case = self.data.case
        return GraphSource(
            kind="notification",
            label=f"First notice {case.reference}" if case else "First notice",
            reference=case.reference if case else None,
            excerpt=_truncate(excerpt),
            confidence=float(case.extraction_confidence)
            if case and case.extraction_confidence
            else None,
        )

    def field_source(self, path: str) -> GraphSource | None:
        """The extracted field behind a value, with the sentence it was read from.

        This is the traceability spine: `loss.affected_assets` knows its own
        confidence, whether a human corrected it, and which attachment it came
        out of, so an asset node inherits all three rather than asserting a name.
        """
        row = self._field(path)
        if row is None:
            return None
        document = self._document(getattr(row, "source_document_id", None))
        return GraphSource(
            kind="document" if document else "notification",
            label=document.filename if document else f"{row.label} (first notice)",
            reference=str(row.source_document_id) if document else row.field_path,
            excerpt=_truncate(getattr(row, "evidence_snippet", None)),
            confidence=float(row.confidence) if row.confidence is not None else None,
        )

    def field_sources(self, *paths: str) -> list[GraphSource]:
        found = (self.field_source(path) for path in paths)
        return [source for source in found if source is not None]

    def _field(self, path: str) -> Any | None:
        for row in self.data.fields:
            if row.field_path == path and row.value_text:
                return row
        return None

    def field_value(self, path: str) -> str | None:
        row = self._field(path)
        return row.value_text if row else None

    def field_confidence(self, path: str) -> float | None:
        row = self._field(path)
        if row is None or row.confidence is None:
            return None
        return float(row.confidence)

    def _document(self, document_id: Any) -> Any | None:
        if document_id is None:
            return None
        for document in self.data.documents:
            if str(document.id) == str(document_id):
                return document
        return None

    def analysis(self, kind: str) -> dict[str, Any]:
        """The result payload of one analysis, or an empty dict."""
        row = self.data.analyses.get(kind)
        if row is None:
            return {}
        result = getattr(row, "result", None) if not isinstance(row, dict) else row.get("result")
        return result if isinstance(result, dict) else {}

    def analysis_confidence(self, kind: str) -> float | None:
        row = self.data.analyses.get(kind)
        if row is None:
            return None
        value = (
            getattr(row, "confidence", None) if not isinstance(row, dict) else row.get("confidence")
        )
        return float(value) if value is not None else None

    def analysis_provider(self, kind: str) -> str | None:
        row = self.data.analyses.get(kind)
        if row is None:
            return None
        return getattr(row, "provider", None) if not isinstance(row, dict) else row.get("provider")


def build_claim_graph(data: GraphInput) -> ClaimGraph:
    """Assemble one claim's knowledge graph from its records.

    Sections run in dependency order — the claim, then the contract, then what
    happened, then what it damaged, then what proves it, then what is risky —
    because each attaches to nodes the ones above it created.
    """
    builder = _Builder(data)

    claim_id = _claim_node(builder)
    _policy_and_insured(builder, claim_id)
    incident_id = _incident_and_location(builder, claim_id)
    _assets(builder, claim_id, incident_id)
    _coverage(builder, claim_id)
    _evidence(builder, claim_id)
    _people(builder, claim_id)
    _catastrophe(builder, claim_id, incident_id)
    _risk(builder, claim_id)
    _related(builder, claim_id)

    if data.include_seeded:
        _seeded_entities(builder, claim_id)

    return ClaimGraph(
        claim_reference=data.claim.reference,
        generated_at=datetime.now(UTC),
        nodes=builder.nodes,
        edges=builder.edges,
        summary=_summarise(builder),
    )


# -- the claim ---------------------------------------------------------------


def _claim_node(builder: _Builder) -> str:
    claim = builder.data.claim
    currency = claim.currency or "GBP"

    properties = [
        GraphProperty("Status", _sentence(claim.status) or "—"),
        GraphProperty("Loss type", _sentence(claim.loss_type) or "—"),
        GraphProperty("Date of loss", _day(claim.date_of_loss) or "—"),
        GraphProperty("Reported", _day(claim.reported_at) or "—"),
        GraphProperty("Reserve", _money(claim.reserve_minor, currency) or "—", tone="accent"),
    ]
    if claim.paid_minor:
        properties.append(GraphProperty("Paid to date", _money(claim.paid_minor, currency) or "—"))
    if claim.severity:
        properties.append(
            GraphProperty(
                "Severity",
                _sentence(claim.severity) or "—",
                tone="bad" if claim.severity == "critical" else "accent",
            )
        )
    if claim.over_authority:
        properties.append(GraphProperty("Authority", "Above the handler's limit", tone="bad"))

    return builder.add(
        GraphNode(
            id="claim",
            type=NodeType.CLAIM,
            category=NodeCategory.CLAIM,
            label=claim.reference,
            subtitle=_sentence(claim.loss_type),
            properties=properties,
            relevance="The claim every other entity in this graph is connected to.",
            provenance=Provenance.RECORD,
            sources=[builder.notification_source()] if builder.data.case else [],
        )
    )


# -- the contract ------------------------------------------------------------


def _policy_and_insured(builder: _Builder, claim_id: str) -> None:
    claim = builder.data.claim
    policy = builder.data.policy
    case = builder.data.case

    number = (policy.policy_number if policy else None) or claim.policy_number
    if number:
        properties = [
            GraphProperty("Policy number", number),
            GraphProperty(
                "Line of business",
                _sentence((policy.line_of_business if policy else None) or claim.line_of_business)
                or "—",
            ),
        ]
        if policy:
            properties += [
                GraphProperty("Product", policy.policy_type or "—"),
                GraphProperty(
                    "Status",
                    _sentence(policy.status) or "—",
                    tone="ok" if policy.status == "active" else "bad",
                ),
                GraphProperty(
                    "Period",
                    f"{_day(policy.effective_date)} → {_day(policy.expiry_date)}",
                ),
                GraphProperty("Limit", _money(policy.limit_amount_minor, policy.currency) or "—"),
                GraphProperty(
                    "Deductible",
                    _money(policy.deductible_amount_minor, policy.currency) or "—",
                    tone="accent",
                ),
            ]
            if policy.perils_covered:
                properties.append(GraphProperty("Perils covered", ", ".join(policy.perils_covered)))
            if policy.exclusions:
                properties.append(GraphProperty("Exclusions", ", ".join(policy.exclusions)))

        # The match score is the honest confidence for a policy node: the policy
        # itself is a record, but *this policy belonging to this claim* is a
        # judgement the matcher made.
        match = _selected_match(builder)
        confidence = float(match.score) if match is not None else None
        strength = getattr(match, "match_strength", None) if match is not None else None

        builder.add(
            GraphNode(
                id="policy",
                type=NodeType.POLICY,
                category=NodeCategory.POLICY,
                label=number,
                subtitle=_sentence(
                    (policy.line_of_business if policy else None) or claim.line_of_business
                ),
                properties=properties,
                confidence=confidence,
                relevance=(
                    getattr(match, "reasoning", None)
                    or "The contract the claim is assessed against."
                ),
                provenance=Provenance.RECORD if policy else Provenance.DERIVED,
                sources=[
                    GraphSource(
                        kind="policy_record",
                        label=f"Policy administration — {number}",
                        reference=number,
                        excerpt=getattr(match, "reasoning", None),
                        confidence=confidence,
                    )
                ],
                flag=(
                    Flag.WATCH
                    if strength not in {None, "exact"} or (case and not case.policy_confirmed)
                    else None
                ),
            )
        )
        builder.link(
            claim_id,
            "COVERED_BY",
            "policy",
            confidence=confidence,
            provenance=Provenance.DERIVED,
            sources=[
                GraphSource(
                    kind="policy_match",
                    label="Policy matching",
                    reference=strength,
                    excerpt=getattr(match, "reasoning", None),
                    confidence=confidence,
                )
            ]
            if match is not None
            else [],
        )

    insured = (
        (policy.insured_name if policy else None)
        or claim.insured_name
        or (case.insured_name if case else None)
    )
    if insured:
        properties = [GraphProperty("Legal name", insured)]
        if policy and policy.insured_organisation:
            properties.append(GraphProperty("Organisation", policy.insured_organisation))
        if policy and policy.insured_email:
            properties.append(GraphProperty("Email", policy.insured_email))
        if policy and policy.insured_phone:
            properties.append(GraphProperty("Phone", policy.insured_phone))
        if claim.claimant_name and claim.claimant_name != insured:
            properties.append(GraphProperty("Claimant", claim.claimant_name))
        if policy and policy.locations:
            properties.append(GraphProperty("Scheduled locations", str(len(policy.locations))))

        party = _party(builder, "insured")
        builder.add(
            GraphNode(
                id="insured",
                type=NodeType.INSURED,
                category=NodeCategory.PARTIES,
                label=insured,
                subtitle="Insured",
                properties=properties,
                confidence=float(party.confidence)
                if party is not None and party.confidence is not None
                else None,
                relevance="The party the policy was issued to and the claim is made by.",
                provenance=Provenance.RECORD,
                sources=builder.field_sources("policy.insured_name")
                or ([builder.notification_source()] if case else []),
            )
        )
        builder.link("policy", "ISSUED_TO", "insured", provenance=Provenance.RECORD)
        # Stated so the insured is reachable in one hop when the policy is unmatched.
        if not builder.has("policy"):
            builder.link(claim_id, "ISSUED_TO", "insured")


def _selected_match(builder: _Builder) -> Any | None:
    matches = builder.data.policy_matches
    for match in matches:
        if getattr(match, "selected", False):
            return match
    return matches[0] if matches else None


def _party(builder: _Builder, role: str) -> Any | None:
    for party in builder.data.parties:
        if (party.role or "").lower() == role:
            return party
    return None


# -- what happened -----------------------------------------------------------


def _incident_and_location(builder: _Builder, claim_id: str) -> str | None:
    claim = builder.data.claim
    case = builder.data.case

    cause = (case.cause_of_loss if case else None) or claim.loss_type
    description = claim.loss_description or (case.loss_description if case else None)
    if not cause and not description:
        return None

    properties = [
        GraphProperty("Peril", _sentence(cause) or "—"),
        GraphProperty("Date of loss", _day(claim.date_of_loss) or "—"),
    ]
    if case:
        if case.incident_reference:
            properties.append(GraphProperty("Incident reference", case.incident_reference))
        if case.police_reference and case.police_reference.lower() not in {"not applicable", "n/a"}:
            properties.append(GraphProperty("Police reference", case.police_reference))
        if case.injuries is not None:
            properties.append(
                GraphProperty("Injuries", str(case.injuries), tone="bad" if case.injuries else None)
            )
        for label, present in (
            ("Business interruption", case.business_interruption),
            ("Structural damage", case.structural_damage),
            ("Environmental exposure", case.environmental_exposure),
        ):
            if present:
                properties.append(GraphProperty(label, "Reported", tone="accent"))
    if description:
        properties.append(GraphProperty("Description", _truncate(description, 400) or "—"))

    incident_id = builder.add(
        GraphNode(
            id="incident",
            type=NodeType.INCIDENT,
            category=NodeCategory.INCIDENT,
            label=_incident_label(cause, claim.date_of_loss),
            subtitle="Incident",
            properties=properties,
            confidence=builder.field_confidence("loss.cause_of_loss")
            or builder.analysis_confidence("classification"),
            relevance="The event the claim is made in respect of.",
            provenance=Provenance.RECORD,
            sources=builder.field_sources(
                "loss.cause_of_loss", "loss.date_of_loss", "loss.loss_description"
            )
            or ([builder.notification_source(description)] if case else []),
        )
    )
    builder.link(
        claim_id,
        "CAUSED_BY",
        incident_id,
        confidence=builder.field_confidence("loss.cause_of_loss"),
        sources=builder.field_sources("loss.cause_of_loss"),
    )

    address = claim.loss_location or (case.loss_location if case else None)
    if address:
        policy = builder.data.policy
        scheduled = bool(
            policy
            and policy.primary_location
            and policy.primary_location.strip().lower() == address.strip().lower()
        )
        properties = [GraphProperty("Address", address)]
        if claim.loss_country or (case and case.loss_country):
            properties.append(
                GraphProperty(
                    "Country", claim.loss_country or (case.loss_country if case else "") or "—"
                )
            )
        if policy and policy.region:
            properties.append(GraphProperty("Region", policy.region))
        properties.append(
            GraphProperty(
                "On the policy schedule",
                "Yes — matches the insured location" if scheduled else "Not confirmed",
                tone="ok" if scheduled else "accent",
            )
        )
        if case and case.loss_latitude is not None and case.loss_longitude is not None:
            properties.append(
                GraphProperty("Coordinates", f"{case.loss_latitude:.4f}, {case.loss_longitude:.4f}")
            )

        builder.add(
            GraphNode(
                id="location",
                type=NodeType.LOCATION,
                category=NodeCategory.INCIDENT,
                label=address,
                subtitle="Loss location",
                properties=properties,
                confidence=builder.field_confidence("loss.loss_location"),
                relevance=(
                    "The site the loss occurred at, and the one the policy schedules."
                    if scheduled
                    else "The site the loss occurred at. It is not confirmed "
                    "against the policy schedule."
                ),
                provenance=Provenance.RECORD,
                sources=builder.field_sources("loss.loss_location")
                or ([builder.notification_source()] if case else []),
                flag=None if scheduled else Flag.WATCH,
            )
        )
        builder.link(incident_id, "OCCURRED_AT", "location")
        builder.link("policy", "SCHEDULES", "location", provenance=Provenance.DERIVED)

    return incident_id


# -- what it damaged ---------------------------------------------------------


def _assets(builder: _Builder, claim_id: str, incident_id: str | None) -> None:
    case = builder.data.case
    described = (case.affected_assets if case else None) or builder.field_value(
        "loss.affected_assets"
    )
    names = _split_assets(described)
    if not names:
        return

    sources = builder.field_sources("loss.affected_assets") or (
        [builder.notification_source(described)] if case else []
    )
    confidence = builder.field_confidence("loss.affected_assets")

    # The estimate is the claim's, not each asset's — the records do not
    # apportion it. Stated once on the first asset would be a lie about the
    # others, so it is stated as the claim-level figure it is.
    estimate = _money(
        case.repair_estimate_minor if case else None,
        (case.currency if case else None) or builder.data.claim.currency,
    )

    for index, name in enumerate(names):
        asset_id = builder.add(
            GraphNode(
                id=f"asset:{index}",
                type=NodeType.ASSET,
                category=NodeCategory.ASSETS,
                label=name,
                subtitle="Damaged asset",
                properties=[
                    GraphProperty("Asset", name),
                    GraphProperty("Reported as damaged", "Yes", tone="accent"),
                    GraphProperty(
                        "Claim repair estimate",
                        f"{estimate} across all damaged property"
                        if estimate
                        else "Not yet estimated",
                    ),
                ],
                confidence=confidence,
                relevance="Reported damaged in the incident, and assessed under the claim.",
                provenance=Provenance.DERIVED,
                sources=sources,
            )
        )
        builder.link(
            incident_id,
            "DAMAGED",
            asset_id,
            confidence=confidence,
            provenance=Provenance.DERIVED,
            sources=sources,
        )
        if not builder.has(incident_id):
            builder.link(claim_id, "DAMAGED", asset_id, provenance=Provenance.DERIVED)


# -- what the contract says about it -----------------------------------------


def _coverage(builder: _Builder, claim_id: str) -> None:
    """One coverage node per check the assessment ran.

    Modelled as several nodes rather than one, because that is what a handler
    argues about: the policy was in force *and* the peril is covered *and* the
    estimate is inside the limit are three separate findings, and collapsing them
    into a single "likely covered" chip is exactly the summarising that loses the
    one that failed.
    """
    result = builder.analysis("coverage")
    checks = result.get("checks") if isinstance(result.get("checks"), list) else []
    confidence = builder.analysis_confidence("coverage")
    provider = builder.analysis_provider("coverage")
    policy = builder.data.policy

    if not checks:
        return

    source = GraphSource(
        kind="assessment",
        label=f"Coverage assessment ({provider or 'deterministic'})",
        reference="coverage",
        excerpt=_truncate(result.get("reasoning")),
        confidence=confidence,
    )

    for check in checks:
        if not isinstance(check, dict):
            continue
        key = str(check.get("key") or "check")
        state = str(check.get("state") or "")
        properties = [
            GraphProperty("Check", str(check.get("label") or key)),
            GraphProperty(
                "Outcome",
                _CHECK_OUTCOME.get(state, _sentence(state) or "—"),
                tone=_tone_for_state(state),
            ),
        ]
        if check.get("detail"):
            properties.append(GraphProperty("Finding", str(check["detail"])))
        if key == "limit" and policy:
            properties.append(
                GraphProperty(
                    "Policy limit", _money(policy.limit_amount_minor, policy.currency) or "—"
                )
            )
        if key == "deductible" and policy:
            properties.append(
                GraphProperty(
                    "Deductible",
                    _money(policy.deductible_amount_minor, policy.currency) or "—",
                    tone="accent",
                )
            )

        coverage_id = builder.add(
            GraphNode(
                id=f"coverage:{key}",
                type=NodeType.COVERAGE,
                category=NodeCategory.POLICY,
                label=str(check.get("label") or key),
                subtitle="Coverage check",
                properties=properties,
                confidence=confidence,
                relevance=str(check.get("detail") or result.get("reasoning") or ""),
                provenance=Provenance.DERIVED,
                sources=[source],
                flag=(
                    Flag.ISSUE
                    if state == "fail"
                    else (Flag.WATCH if state in {"attention", "warn"} else None)
                ),
            )
        )
        builder.link(
            claim_id,
            "COVERED_UNDER",
            coverage_id,
            confidence=confidence,
            provenance=Provenance.DERIVED,
            sources=[source],
        )
        builder.link("policy", "COVERED_UNDER", coverage_id, provenance=Provenance.DERIVED)

    # Assets sit under the coverage that would pay for them. Only the limit check
    # is joined: it is the one that is *about* the damaged property, and joining
    # every asset to every check would triple the edge count for no reading.
    if builder.has("coverage:limit"):
        for node in list(builder.nodes):
            if node.type is NodeType.ASSET:
                builder.link(
                    node.id, "COVERED_UNDER", "coverage:limit", provenance=Provenance.DERIVED
                )


# -- what proves it ----------------------------------------------------------


def _evidence(builder: _Builder, claim_id: str) -> None:
    case = builder.data.case

    if case:
        fields_read = sum(1 for row in builder.data.fields if row.value_text)
        builder.add(
            GraphNode(
                id="evidence:notification",
                type=NodeType.EVIDENCE,
                category=NodeCategory.EVIDENCE,
                label=f"First notice {case.reference}",
                subtitle="FNOL notification",
                properties=[
                    GraphProperty("Channel", _sentence(case.channel) or "—"),
                    GraphProperty("Received", _day(case.received_at) or "—"),
                    GraphProperty("Reported by", case.reporter_name or "—"),
                    GraphProperty("Fields read", str(fields_read)),
                    GraphProperty(
                        "Extraction confidence",
                        _percent(float(case.extraction_confidence))
                        if case.extraction_confidence is not None
                        else "—",
                        tone="ok" if (case.extraction_confidence or 0) >= 0.75 else "accent",
                    ),
                    GraphProperty(
                        "Completeness",
                        _percent(float(case.completeness_score))
                        if case.completeness_score is not None
                        else "—",
                    ),
                ],
                confidence=float(case.extraction_confidence)
                if case.extraction_confidence is not None
                else None,
                relevance=(
                    "The notice the claim was created from, and the source of most of its facts."
                ),
                provenance=Provenance.RECORD,
                sources=[builder.notification_source(case.source_body)],
            )
        )
        builder.link(claim_id, "SUPPORTED_BY", "evidence:notification")

    for document in builder.data.documents:
        cited = [
            row.label
            for row in builder.data.fields
            if str(getattr(row, "source_document_id", "")) == str(document.id) and row.value_text
        ]
        failed = document.extraction_status == "failed"
        document_id = builder.add(
            GraphNode(
                id=f"evidence:{document.id}",
                type=NodeType.EVIDENCE,
                category=NodeCategory.EVIDENCE,
                label=document.filename,
                subtitle=_sentence(document.document_kind) or "Document",
                properties=[
                    GraphProperty("Filename", document.filename),
                    GraphProperty("Kind", _sentence(document.document_kind) or "—"),
                    GraphProperty("Received as", _sentence(document.source) or "—"),
                    GraphProperty(
                        "Extraction",
                        _sentence(document.extraction_status) or "—",
                        tone="bad" if failed else "ok",
                    ),
                    GraphProperty(
                        "Text read",
                        f"{document.text_characters:,} characters"
                        if document.text_characters
                        else "None",
                    ),
                    GraphProperty(
                        "Values cited from it",
                        ", ".join(cited) if cited else "None yet",
                    ),
                ],
                relevance=(
                    f"Supports {len(cited)} extracted "
                    f"value{'s' if len(cited) != 1 else ''} on this claim."
                    if cited
                    else "Attached to the notice; no value has been read from it yet."
                ),
                provenance=Provenance.RECORD,
                sources=[
                    GraphSource(
                        kind="document",
                        label=document.filename,
                        reference=str(document.id),
                        excerpt=_truncate(getattr(document, "extracted_text", None), 300),
                    )
                ],
                flag=Flag.ISSUE if failed else None,
            )
        )
        builder.link(claim_id, "SUPPORTED_BY", document_id)
        # A document that a value was read out of evidences the incident, not
        # just the claim — that edge is what makes the graph traceable rather
        # than merely connected.
        if cited:
            builder.link(
                document_id,
                "EVIDENCES",
                "incident",
                provenance=Provenance.DERIVED,
                sources=[
                    GraphSource(
                        kind="extraction",
                        label=document.filename,
                        reference=str(document.id),
                        excerpt=f"{len(cited)} values read: {', '.join(cited[:4])}",
                    )
                ],
            )


# -- who is involved --------------------------------------------------------


def _people(builder: _Builder, claim_id: str) -> None:
    case = builder.data.case
    policy = builder.data.policy
    broker = _party(builder, "broker")

    organisation = (
        (broker.organisation if broker else None)
        or (case.reporter_organisation if case else None)
        or (policy.broker_name if policy else None)
    )
    contact = (broker.name if broker else None) or (case.reporter_name if case else None)

    if organisation or contact:
        properties = [GraphProperty("Organisation", organisation or "—")]
        if contact:
            properties.append(GraphProperty("Contact", contact))
        if case and case.reporter_role:
            properties.append(GraphProperty("Role", case.reporter_role))
        email = (broker.email if broker else None) or (case.reporter_email if case else None)
        phone = (broker.phone if broker else None) or (case.reporter_phone if case else None)
        if email:
            properties.append(GraphProperty("Email", email))
        if phone:
            properties.append(GraphProperty("Phone", phone))
        if policy and policy.broker_reference:
            properties.append(GraphProperty("Broker reference", policy.broker_reference))

        on_policy = bool(
            policy
            and policy.broker_name
            and organisation
            and policy.broker_name.strip().lower() == organisation.strip().lower()
        )
        properties.append(
            GraphProperty(
                "Broker of record",
                "Yes — matches the policy" if on_policy else "Not confirmed against the policy",
                tone="ok" if on_policy else "accent",
            )
        )

        builder.add(
            GraphNode(
                id="broker",
                type=NodeType.BROKER,
                category=NodeCategory.PARTIES,
                label=organisation or contact or "Broker",
                subtitle="Reporting broker",
                properties=properties,
                confidence=float(broker.confidence)
                if broker is not None and broker.confidence is not None
                else None,
                relevance="Submitted the first notice on the insured's behalf.",
                provenance=Provenance.RECORD,
                sources=[builder.notification_source()] if case else [],
                flag=None if on_policy else Flag.WATCH,
            )
        )
        builder.link(claim_id, "REPORTED_BY", "broker")
        builder.link("broker", "INSURES", "insured", provenance=Provenance.DERIVED)

    assignment = builder.data.assignment
    triage = builder.data.triage
    handler = (assignment.handler_name if assignment else None) or builder.data.claim.handler_name
    if handler:
        recommended = bool(assignment and assignment.status != "assigned")
        properties = [GraphProperty("Handler", handler)]
        if assignment:
            if assignment.team:
                properties.append(GraphProperty("Team", assignment.team))
            if assignment.queue:
                properties.append(GraphProperty("Queue", assignment.queue))
            properties.append(
                GraphProperty(
                    "Assignment",
                    _sentence(assignment.status) or "—",
                    tone="accent" if recommended else "ok",
                )
            )
            if assignment.reasoning:
                properties.append(GraphProperty("Why this handler", assignment.reasoning))
        if triage and triage.recommended_route:
            properties.append(GraphProperty("Route", triage.recommended_route))

        builder.add(
            GraphNode(
                id="handler",
                type=NodeType.HANDLER,
                category=NodeCategory.PARTIES,
                label=handler,
                subtitle="Claims handler",
                properties=properties,
                confidence=float(assignment.confidence)
                if assignment is not None and assignment.confidence is not None
                else None,
                relevance=(
                    "Recommended by routing; a manager has not accepted the allocation yet."
                    if recommended
                    else "Working the claim."
                ),
                provenance=Provenance.DERIVED if assignment else Provenance.RECORD,
                sources=[
                    GraphSource(
                        kind="routing",
                        label=f"Assignment ({assignment.strategy or 'workload'})",
                        reference=assignment.status,
                        excerpt=assignment.reasoning,
                        confidence=float(assignment.confidence)
                        if assignment.confidence is not None
                        else None,
                    )
                ]
                if assignment
                else [],
                flag=Flag.WATCH if recommended else None,
            )
        )
        builder.link(claim_id, "ASSIGNED_TO", "handler", provenance=Provenance.DERIVED)

    adjuster = _party(builder, "adjuster") or _party(builder, "loss_adjuster")
    if adjuster:
        properties = [GraphProperty("Adjuster", adjuster.name)]
        if adjuster.organisation:
            properties.append(GraphProperty("Firm", adjuster.organisation))
        if adjuster.email:
            properties.append(GraphProperty("Email", adjuster.email))
        if adjuster.phone:
            properties.append(GraphProperty("Phone", adjuster.phone))
        builder.add(
            GraphNode(
                id="adjuster",
                type=NodeType.ADJUSTER,
                category=NodeCategory.PARTIES,
                label=adjuster.name,
                subtitle="Loss adjuster",
                properties=properties,
                confidence=float(adjuster.confidence) if adjuster.confidence is not None else None,
                relevance="Instructed to inspect the loss and report on quantum.",
                provenance=Provenance.RECORD,
                sources=[builder.notification_source()] if builder.data.case else [],
            )
        )
        builder.link(claim_id, "INSPECTED_BY", "adjuster")


# -- catastrophe -------------------------------------------------------------


def _catastrophe(builder: _Builder, claim_id: str, incident_id: str | None) -> None:
    """The attributed event, or the one the matcher considered and rejected.

    A rejected candidate is worth a node. "No CAT event" tells a handler nothing;
    "Storm Isolde was in range on the date of loss and was rejected because the
    peril is fire, not flood" tells them the attribution has already been thought
    about, and lets them overrule it.
    """
    attributed = builder.data.cat_event
    case = builder.data.case
    confirmed = bool(case and case.cat_confirmed)
    confidence = float(case.cat_confidence) if case and case.cat_confidence is not None else None

    if attributed is not None:
        builder.add(
            GraphNode(
                id=f"cat:{attributed.reference}",
                type=NodeType.CAT_EVENT,
                category=NodeCategory.INCIDENT,
                label=attributed.name,
                subtitle="Catastrophe event",
                properties=[
                    *_cat_properties(attributed),
                    GraphProperty(
                        "Attribution",
                        "Confirmed" if confirmed else "Proposed",
                        tone="ok" if confirmed else "accent",
                    ),
                ],
                confidence=confidence,
                relevance="The catastrophe event this loss is aggregated under.",
                provenance=Provenance.RECORD,
                sources=[
                    GraphSource(
                        kind="cat_feed",
                        label=f"{attributed.provider} — {attributed.reference}",
                        reference=attributed.reference,
                        confidence=confidence,
                    )
                ],
            )
        )
        builder.link(
            claim_id,
            "MATCHES",
            f"cat:{attributed.reference}",
            confidence=confidence,
            provenance=Provenance.DERIVED,
        )
        builder.link(
            incident_id, "MATCHES", f"cat:{attributed.reference}", provenance=Provenance.DERIVED
        )
        return

    for candidate in builder.data.cat_candidates[:1]:
        perils = [peril.lower() for peril in (candidate.perils or [])]
        cause = (case.cause_of_loss if case else None) or builder.data.claim.loss_type or ""
        peril_matches = cause.lower() in perils
        verdict = (
            "In range, and the peril matches — worth confirming."
            if peril_matches
            else (
                "In range on the date of loss, but the peril does not match: this loss "
                f"is {cause or 'unclassified'}, the event covers "
                f"{', '.join(perils) or 'other perils'}."
            )
        )
        builder.add(
            GraphNode(
                id=f"cat:{candidate.reference}",
                type=NodeType.CAT_EVENT,
                category=NodeCategory.INCIDENT,
                label=candidate.name,
                subtitle="Catastrophe event — considered",
                properties=[
                    *_cat_properties(candidate),
                    GraphProperty(
                        "Attribution",
                        "Considered, not attributed",
                        tone="accent" if peril_matches else "neutral",
                    ),
                    GraphProperty("Reason", verdict),
                ],
                relevance=verdict,
                provenance=Provenance.DERIVED,
                sources=[
                    GraphSource(
                        kind="cat_feed",
                        label=f"{candidate.provider} — {candidate.reference}",
                        reference=candidate.reference,
                        excerpt=verdict,
                    )
                ],
                flag=Flag.WATCH if peril_matches else None,
            )
        )
        builder.link(
            claim_id,
            "CONSIDERED",
            f"cat:{candidate.reference}",
            provenance=Provenance.DERIVED,
        )


def _cat_properties(event: Any) -> list[GraphProperty]:
    properties = [
        GraphProperty("Event", event.name),
        GraphProperty("Reference", event.reference),
        GraphProperty("Type", _sentence(event.event_type) or "—"),
        GraphProperty("Window", f"{_day(event.start_date)} → {_day(event.end_date)}"),
    ]
    if event.severity:
        properties.append(
            GraphProperty(
                "Severity",
                _sentence(event.severity) or "—",
                tone="bad" if event.severity in {"major", "catastrophic"} else "accent",
            )
        )
    if event.perils:
        properties.append(GraphProperty("Perils", ", ".join(event.perils)))
    if event.region:
        properties.append(GraphProperty("Region", event.region))
    if event.affected_areas:
        properties.append(GraphProperty("Affected areas", ", ".join(event.affected_areas[:6])))
    return properties


# -- what is risky ----------------------------------------------------------


def _risk(builder: _Builder, claim_id: str) -> None:
    result = builder.analysis("fraud")
    indicators = result.get("indicators") if isinstance(result.get("indicators"), list) else []
    score = result.get("score")
    level = result.get("level")

    for index, indicator in enumerate(indicators):
        if not isinstance(indicator, dict):
            continue
        severity = _indicator_severity(indicator)
        # `title` first: the fraud model writes a human phrase there and keeps
        # `code` as the machine key, and falling through to the code puts
        # `loss_near_inception` on screen where "Loss shortly after inception"
        # belongs.
        label = str(
            indicator.get("title")
            or indicator.get("label")
            or _sentence(indicator.get("code"))
            or f"Indicator {index + 1}"
        )
        detail = str(indicator.get("detail") or indicator.get("reason") or "")
        node_id = builder.add(
            GraphNode(
                id=f"risk:fraud:{index}",
                type=NodeType.FRAUD_INDICATOR,
                category=NodeCategory.RISK,
                label=label,
                subtitle="Fraud indicator",
                properties=[
                    GraphProperty("Indicator", label),
                    GraphProperty(
                        "Severity",
                        severity.upper(),
                        tone="bad" if severity == "high" else "accent",
                    ),
                    GraphProperty("Finding", detail or "—"),
                    GraphProperty(
                        "Claim fraud score",
                        f"{round(float(score) * 100)}%" if score is not None else "—",
                    ),
                ],
                relevance=detail or "Raised by the fraud model against this claim.",
                provenance=Provenance.DERIVED,
                sources=[
                    GraphSource(
                        kind="fraud_model",
                        label=(
                            "Fraud screening "
                            f"({builder.analysis_provider('fraud') or 'deterministic'})"
                        ),
                        reference=str(level or ""),
                        excerpt=detail or None,
                        confidence=float(score) if score is not None else None,
                    )
                ],
                flag=Flag.ISSUE
                if severity == "high"
                else (Flag.WATCH if severity == "medium" else None),
            )
        )
        builder.link(claim_id, "HAS_INDICATOR", node_id, provenance=Provenance.DERIVED)

    # Open exceptions are risk too, and the same kind of risk: a rule looked at
    # the claim and wants a human to decide something.
    for exception in builder.data.exceptions:
        if exception.status != "open":
            continue
        blocking = bool(exception.blocking)
        node_id = builder.add(
            GraphNode(
                id=f"risk:exception:{exception.code}",
                type=NodeType.FRAUD_INDICATOR,
                category=NodeCategory.RISK,
                label=exception.title,
                subtitle="Open exception",
                properties=[
                    GraphProperty("Exception", exception.title),
                    GraphProperty("Code", exception.code.upper().replace("_", "-")),
                    GraphProperty(
                        "Severity",
                        (exception.severity or "warning").upper(),
                        tone="bad" if exception.severity == "critical" else "accent",
                    ),
                    GraphProperty("Detail", exception.detail or "—"),
                    GraphProperty(
                        "Blocks the claim",
                        "Yes" if blocking else "No",
                        tone="bad" if blocking else None,
                    ),
                ],
                relevance=exception.detail or "Raised by the rules engine and still open.",
                provenance=Provenance.DERIVED,
                sources=[
                    GraphSource(
                        kind="rules_engine",
                        label=f"Rule {exception.code.upper().replace('_', '-')}",
                        reference=exception.code,
                        excerpt=exception.detail,
                    )
                ],
                flag=Flag.ISSUE if blocking else Flag.WATCH,
            )
        )
        builder.link(claim_id, "HAS_INDICATOR", node_id, provenance=Provenance.DERIVED)


# -- what else looks like it ------------------------------------------------


#: What a duplicate candidate *is*. The scan compares a claim against both open
#: claims and notifications that have not been converted yet, and the two are not
#: the same finding: a duplicate claim is money booked twice, a duplicate notice is
#: a second report of one loss. `_sentence` alone would render the enum as "Fnol".
_DUPLICATE_SUBTITLE = {
    "fnol": "Possible duplicate notice",
    "claim": "Possible duplicate claim",
}

_DUPLICATE_RECORD = {
    "fnol": "First notice",
    "claim": "Claim",
}


def _related(builder: _Builder, claim_id: str) -> None:
    for candidate in builder.data.duplicates:
        score = float(candidate.score or 0.0)
        reasons = [
            str(reason.get("detail") or reason.get("signal") or "")
            for reason in (candidate.reasons or [])
            if isinstance(reason, dict)
        ]
        node_id = builder.add(
            GraphNode(
                id=f"related:{candidate.candidate_reference}",
                type=NodeType.RELATED_CLAIM,
                category=NodeCategory.RELATED,
                label=candidate.candidate_reference,
                subtitle=_DUPLICATE_SUBTITLE.get(candidate.candidate_kind, "Possible duplicate"),
                properties=[
                    GraphProperty("Reference", candidate.candidate_reference),
                    GraphProperty(
                        "Record",
                        _DUPLICATE_RECORD.get(
                            candidate.candidate_kind,
                            _sentence(candidate.candidate_kind) or "—",
                        ),
                    ),
                    GraphProperty(
                        "Match score",
                        f"{round(score * 100)}%",
                        tone="bad" if score >= 0.8 else "accent",
                    ),
                    GraphProperty("Resolution", _sentence(candidate.resolution) or "Open"),
                    GraphProperty("Why it matched", "; ".join(filter(None, reasons)) or "—"),
                ],
                confidence=score,
                relevance="Scored against this claim by duplicate detection.",
                provenance=Provenance.DERIVED,
                sources=[
                    GraphSource(
                        kind="duplicate_scan",
                        label="Duplicate detection",
                        reference=candidate.candidate_reference,
                        excerpt="; ".join(filter(None, reasons)) or None,
                        confidence=score,
                    )
                ],
                flag=Flag.ISSUE if score >= 0.8 and candidate.resolution == "open" else Flag.WATCH,
            )
        )
        builder.link(
            claim_id, "RELATED_TO", node_id, confidence=score, provenance=Provenance.DERIVED
        )

    # Other claims on the same policy. Not duplicates — history. A third loss at
    # one site in eighteen months is the pattern nobody sees from a single claim
    # screen, which is the whole argument for the graph.
    for other in builder.data.related_claims:
        node_id = builder.add(
            GraphNode(
                id=f"related:{other.reference}",
                type=NodeType.RELATED_CLAIM,
                category=NodeCategory.RELATED,
                label=other.reference,
                subtitle="Same policy",
                properties=[
                    GraphProperty("Reference", other.reference),
                    GraphProperty("Loss type", _sentence(other.loss_type) or "—"),
                    GraphProperty("Date of loss", _day(other.date_of_loss) or "—"),
                    GraphProperty("Status", _sentence(other.status) or "—"),
                    GraphProperty("Reserve", _money(other.reserve_minor, other.currency) or "—"),
                    GraphProperty("Location", other.loss_location or "—"),
                ],
                relevance="Another claim on the same policy — the loss history behind this one.",
                provenance=Provenance.RECORD,
                sources=[
                    GraphSource(
                        kind="claim_record",
                        label=other.reference,
                        reference=other.reference,
                    )
                ],
            )
        )
        builder.link(claim_id, "RELATED_TO", node_id, provenance=Provenance.DERIVED)


# -- scaffolding ------------------------------------------------------------

#: The entities a claims graph is expected to carry that this data model has no
#: feed for yet. Every one names the source that will replace it, and each is
#: emitted with `provenance=SEEDED` so the screen can mark it. Deleting this
#: block is the whole of "turn the demo off".
_SEEDED_RISK = (
    (
        "amendment_before_loss",
        "Cover amended shortly before the date of loss",
        "The policy limit was endorsed upward 21 days before the loss was "
        "reported. Timing alone is not "
        "evidence, but an amendment inside 30 days of a loss is a standard SIU referral trigger.",
        "high",
    ),
    (
        "repeat_cause_at_site",
        "Repeat cause at the same site",
        "A charging-station ignition has been reported at this location before. "
        "A second loss from the "
        "same cause bears on both indemnity and risk improvement.",
        "medium",
    ),
)


def _seeded_entities(builder: _Builder, claim_id: str) -> None:
    case = builder.data.case

    # A loss adjuster, until the panel-instruction feed lands. Only when the
    # records genuinely have none — a real adjuster must never be shadowed.
    if not builder.has("adjuster"):
        builder.add(
            GraphNode(
                id="adjuster",
                type=NodeType.ADJUSTER,
                category=NodeCategory.PARTIES,
                label="Marchmont Loss Adjusters",
                subtitle="Loss adjuster",
                properties=[
                    GraphProperty("Firm", "Marchmont Loss Adjusters"),
                    GraphProperty("Adjuster", "Iain Cargill, ACILA"),
                    GraphProperty("Instructed", "Awaiting instruction"),
                    GraphProperty("Scope", "Quantum and cause, commercial property"),
                ],
                relevance=(
                    "Would be instructed to inspect the loss and report on cause and quantum."
                ),
                provenance=Provenance.SEEDED,
                sources=[
                    GraphSource(
                        kind="demo_seed",
                        label="Panel instruction feed — not connected",
                        excerpt=(
                            "Replaced by the adjuster panel integration; the graph "
                            "shape does not change."
                        ),
                    )
                ],
            )
        )
        builder.link(claim_id, "INSPECTED_BY", "adjuster", provenance=Provenance.SEEDED)

    # Risk indicators, until the SIU feed lands. Only added where the fraud model
    # produced none of its own — a real indicator is never diluted with a seeded one.
    has_fraud_node = any(node.id.startswith("risk:fraud:") for node in builder.nodes)
    if not has_fraud_node:
        for key, label, detail, severity in _SEEDED_RISK:
            node_id = builder.add(
                GraphNode(
                    id=f"risk:seeded:{key}",
                    type=NodeType.FRAUD_INDICATOR,
                    category=NodeCategory.RISK,
                    label=label,
                    subtitle="Risk indicator",
                    properties=[
                        GraphProperty("Indicator", label),
                        GraphProperty(
                            "Severity",
                            severity.upper(),
                            tone="bad" if severity == "high" else "accent",
                        ),
                        GraphProperty("Finding", detail),
                        GraphProperty("Source", "SIU feed — not connected"),
                    ],
                    relevance=detail,
                    provenance=Provenance.SEEDED,
                    sources=[
                        GraphSource(
                            kind="demo_seed",
                            label="SIU indicator feed — not connected",
                            excerpt=(
                                "Replaced by the SIU screening service; the node "
                                "and edge shape do not change."
                            ),
                        )
                    ],
                    flag=Flag.ISSUE if severity == "high" else Flag.WATCH,
                )
            )
            builder.link(claim_id, "HAS_INDICATOR", node_id, provenance=Provenance.SEEDED)
            builder.link("policy", "HAS_INDICATOR", node_id, provenance=Provenance.SEEDED)

    # A historical claim at the same site, until the loss-history feed lands.
    if not any(node.type is NodeType.RELATED_CLAIM for node in builder.nodes):
        year = (builder.data.claim.date_of_loss or builder.data.claim.reported_at).year - 1
        reference = f"CLM-{year}-88721"
        builder.add(
            GraphNode(
                id=f"related:{reference}",
                type=NodeType.RELATED_CLAIM,
                category=NodeCategory.RELATED,
                label=reference,
                subtitle="Historical claim, same site",
                properties=[
                    GraphProperty("Reference", reference),
                    GraphProperty("Loss type", "Electrical damage"),
                    GraphProperty("Date of loss", f"14 Nov {year}"),
                    GraphProperty("Status", "Settled"),
                    GraphProperty("Settled for", "£41,600"),
                    GraphProperty(
                        "Location",
                        case.loss_location if case and case.loss_location else "Same site",
                    ),
                    GraphProperty("Source", "Loss history feed — not connected"),
                ],
                relevance=(
                    "An earlier electrical loss at the same site. Bears on cause, on "
                    "risk improvement and on the repeat-cause indicator."
                ),
                provenance=Provenance.SEEDED,
                sources=[
                    GraphSource(
                        kind="demo_seed",
                        label="Loss history feed — not connected",
                        excerpt=(
                            "Replaced by the historical-claims query; the graph "
                            "shape does not change."
                        ),
                    )
                ],
                flag=Flag.WATCH,
            )
        )
        builder.link(claim_id, "RELATED_TO", f"related:{reference}", provenance=Provenance.SEEDED)
        builder.link(
            f"related:{reference}",
            "OCCURRED_AT",
            "location",
            provenance=Provenance.SEEDED,
        )
        builder.link(
            f"related:{reference}",
            "EVIDENCES",
            "risk:seeded:repeat_cause_at_site",
            provenance=Provenance.SEEDED,
        )


# -- the summary ------------------------------------------------------------


def _summarise(builder: _Builder) -> GraphSummary:
    """Counts and signals, both computed from the graph that was just built.

    Nothing here is a second source of truth: if a section stops emitting a node,
    the tile above the graph stops counting it, which is the property the numbers
    have to have to be worth putting on screen.
    """
    nodes = builder.nodes
    evidence = [node for node in nodes if node.type is NodeType.EVIDENCE]
    coverage = [node for node in nodes if node.type is NodeType.COVERAGE]
    risk = [node for node in nodes if node.type is NodeType.FRAUD_INDICATOR]
    related = [node for node in nodes if node.type is NodeType.RELATED_CLAIM]
    coverage_issues = [node for node in coverage if node.flag is not None]

    return GraphSummary(
        entity_count=len(nodes),
        relationship_count=len(builder.edges),
        evidence_count=len(evidence),
        coverage_issues=len(coverage_issues),
        risk_indicators=len(risk),
        related_claims=len(related),
        seeded_entities=sum(1 for node in nodes if node.provenance is Provenance.SEEDED),
        signals=_signals(builder, coverage_issues, risk, related),
    )


def _signals(
    builder: _Builder,
    coverage_issues: list[GraphNode],
    risk: list[GraphNode],
    related: list[GraphNode],
) -> list[GraphSignal]:
    signals: list[GraphSignal] = []
    case = builder.data.case

    coverage_confidence = builder.analysis_confidence("coverage")
    if coverage_confidence is not None:
        checks = sum(1 for node in builder.nodes if node.type is NodeType.COVERAGE)
        signals.append(
            GraphSignal(
                id="coverage_confidence",
                label="Coverage confidence",
                value=_percent(coverage_confidence) or "—",
                detail=(
                    f"{len(coverage_issues)} of {checks} checks need a decision."
                    if coverage_issues
                    else "Every coverage check passed on the records held."
                ),
                tone="ok" if not coverage_issues and coverage_confidence >= 0.7 else "accent",
            )
        )

    match = _selected_match(builder)
    if match is not None:
        signals.append(
            GraphSignal(
                id="entity_match_confidence",
                label="Policy match confidence",
                value=_percent(float(match.score)) or "—",
                detail=_truncate(getattr(match, "reasoning", None), 160)
                or "Matched by policy number, insured and location.",
                tone="ok" if float(match.score) >= 0.85 else "accent",
            )
        )

    if case and case.extraction_confidence is not None:
        signals.append(
            GraphSignal(
                id="extraction_confidence",
                label="Extraction confidence",
                value=_percent(float(case.extraction_confidence)) or "—",
                detail=(
                    f"{sum(1 for row in builder.data.fields if row.value_text)} "
                    "values read from the notice and its attachments."
                ),
                tone="ok" if case.extraction_confidence >= 0.75 else "accent",
            )
        )

    cat = next((node for node in builder.nodes if node.type is NodeType.CAT_EVENT), None)
    if cat is not None:
        attributed = builder.data.cat_event is not None
        signals.append(
            GraphSignal(
                id="cat_match",
                label="CAT match",
                value=cat.label if attributed else "Considered",
                detail=_truncate(cat.relevance, 160) or "",
                tone="accent" if attributed else "neutral",
            )
        )

    if risk:
        worst = next((node for node in risk if node.flag is Flag.ISSUE), risk[0])
        signals.append(
            GraphSignal(
                id="risk",
                label="Risk indicators",
                value=str(len(risk)),
                detail=_truncate(worst.label, 160) or "",
                tone="bad" if any(node.flag is Flag.ISSUE for node in risk) else "accent",
            )
        )

    if related:
        signals.append(
            GraphSignal(
                id="related_claims",
                label="Related claims",
                value=str(len(related)),
                detail=_truncate(
                    "; ".join(f"{node.label} — {node.subtitle}" for node in related[:2]), 160
                )
                or "",
                tone="accent",
            )
        )

    return signals
