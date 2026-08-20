"""Ranking the policy library against one notice.

Pure functions over plain objects. No database, no vector store, no configuration
lookups — everything arrives as an argument, so the whole engine is testable with
two dataclasses and an auditor recomputing a 0.87 in two years' time gets 0.87.

## What this is, and what it is not

`app/domain/policy_identification.py` matches a notice against the **policy book**:
structured rows with a policy number, an insured name and a schedule of locations,
compared field by field. It is the authority on which policy a claim is *under*, and
this module does not replace it.

This one matches a notice against the **policy wordings** — the PDFs an
administrator uploaded — using what those documents actually say. It exists because
the two answer different questions and fail in different places:

* The book answers "which contract", and cannot answer "does this wording mention
  the peril", because a row has a `perils_covered` array and a policy has fourteen
  pages of definitions, exceptions and endorsements.
* Retrieval answers "which wording talks about this loss", and on its own cannot
  answer "which contract", because a well-drafted property policy talks about a
  frozen sprinkler exactly as well as every other property policy does.

So the score here is **two things multiplied out and reported separately**:
semantic relevance from retrieval, and lexical corroboration computed here from the
facts the document states about itself. Reported separately because they mean
different things to the officer reading them, and because collapsing them is how a
system produces a confident-looking 0.9 that rests entirely on shared vocabulary.

## The rule that keeps weak matches off the screen

> **Retrieval alone cannot recommend a policy.** A document that matched on nothing
> but its prose is capped below the possible band whatever its similarity score.

Top-k retrieval over a library of twelve policies always returns twelve scores, and
normalising them produces a 1.0 for whichever came first. Without this cap, a
notice about a Maryland landscaping loss with no policy in the library returns the
closest-sounding contractors' policy at high confidence — which is precisely the
failure the four `NO_MATCH` cases in `case_data/` exist to catch. Corroboration is
what makes a number mean something: the policy number, the insured, the site, the
term. `_classify` reads *which* of those agreed, not only the total.

## Deliberately not built

**A cross-encoder reranker** is the right next step for accuracy and changes no
interface here: `match()` takes hits that are already scored, so a reranker slots in
above it.

**A model asked "does this policy cover this loss"** is a coverage opinion, not a
match. It belongs beside the coverage assessment, and it must never move an identity
rank.

**Scoring the absence of a peril as a mismatch** conflates two things. An exclusion
an officer has to read is not the same as a policy that does not answer the loss, and
one number cannot say both — so it is a warning.
"""

from __future__ import annotations

import uuid
from dataclasses import dataclass, field
from datetime import date
from enum import StrEnum
from typing import Any

from app.core.config import PolicyLibrarySettings, settings
from app.domain.enums import (
    LineOfBusiness,
    PolicyConfidence,
    PolicyPeriodOutcome,
    SignalOutcome,
)
from app.domain.matching import (
    location_similarity,
    normalise,
    normalise_reference,
    sequence_ratio,
    tokens,
)
from app.domain.policy_identification import SignalAxis, entity_similarity

#: Bumped whenever a weight, a comparator or the ladder changes. Stored on every
#: result, so a score can be read back against the engine that produced it.
ENGINE_VERSION = "policy-document-match-1"

#: How much of the final score retrieval contributes when corroboration was
#: possible at all. Under half, deliberately: semantic similarity is the evidence
#: that this wording *speaks to* the loss, and the corroborating facts are the
#: evidence that it is the right contract. The second is what an officer binds on.
RETRIEVAL_WEIGHT = 0.42

#: The ceiling on a match with no corroborating signal at all. Below the possible
#: threshold on purpose — see "The rule that keeps weak matches off the screen".
RETRIEVAL_ONLY_CEILING = 0.42

#: Lines that are the same risk under two names. A construction all-risks wording
#: and an engineering one answer each other's notices.
_EQUIVALENT_LINES: tuple[frozenset[str], ...] = (
    frozenset({LineOfBusiness.CONSTRUCTION.value, LineOfBusiness.ENGINEERING.value}),
    frozenset({LineOfBusiness.LIABILITY.value, LineOfBusiness.CASUALTY.value}),
    # An inland-marine contractors' equipment floater and a construction policy
    # cover the same site from two directions, and a notice from that site names
    # neither line reliably. Equivalent for *filtering*, still compared as a signal.
    frozenset({LineOfBusiness.MARINE.value, LineOfBusiness.CONSTRUCTION.value}),
)

#: Reference agreement tiers. A transposed digit is the single most common defect in
#: a policy number quoted from memory — `CP-9038-64115` for `CP-9083-64115` — and it
#: has to be recognised as *nearly* the number rather than as a different one.
_REFERENCE_NEAR = 0.88
#: Two references sharing this much are the same reference rendered differently.
_REFERENCE_CONTAINED = 0.98

#: Entity similarity at or above this is one company; below `_NAME_RELATED` it is
#: two. The gap between them is a corporate group, which is a choice for a person.
_NAME_MATCH = 0.90
_NAME_RELATED = 0.62

#: Address token overlap tiers.
_LOCATION_MATCH = 0.60
_LOCATION_RELATED = 0.34


class MatchFacet(StrEnum):
    """Which question a retrieval query asked.

    Matching issues one query per facet rather than one concatenated query, and
    this is the vocabulary the result reports them in. The reason is mechanical: an
    embedding of "policy number CP-4471-88210, Harborline Cold Storage, ammonia
    release at the Baltimore cold store, business interruption" is the *average* of
    four questions, and the nearest passage to an average is often the nearest
    passage to none of them. Four queries and a fusion beats one query and a hope.

    `facets_hit` is then a signal in its own right, and a strong one: a policy whose
    wording answered the identity query *and* the peril query is a better answer
    than one that answered either twice as well.
    """

    IDENTITY = "identity"
    RISK_LOCATION = "risk_location"
    PERIL = "peril"
    COVERAGE = "coverage"


FACET_LABEL: dict[MatchFacet, str] = {
    MatchFacet.IDENTITY: "Policy and insured identity",
    MatchFacet.RISK_LOCATION: "Site and premises",
    MatchFacet.PERIL: "Cause of loss",
    MatchFacet.COVERAGE: "Cover and coverage part",
}


@dataclass(frozen=True, slots=True)
class SignalDefinition:
    """One corroborating comparison, declared rather than coded into the scorer."""

    signal: str
    label: str
    axis: SignalAxis
    weight: float
    #: A yes-or-no answer. A percentage printed beside "the loss date falls inside
    #: the policy period" is a number pretending to be a measurement.
    binary: bool = False


#: Heaviest first, which is also the order the panel lists them in.
#:
#: The policy number dominates for the reason it does everywhere in this product: it
#: is the only identifier that is supposed to be unique. It is weighted lower here
#: than in `policy_identification` (4.0 against 5.0) because there it is compared
#: against a *book* whose numbers are authoritative, and here against a number read
#: out of a PDF — one more reading step, one more place to be wrong.
SIGNALS: tuple[SignalDefinition, ...] = (
    SignalDefinition("policy_number", "Policy number", SignalAxis.POLICY, 4.0),
    SignalDefinition("insured_name", "Insured", SignalAxis.INSURED, 2.2),
    SignalDefinition("risk_location", "Risk location", SignalAxis.RISK, 1.6),
    SignalDefinition("project_reference", "Project or contract", SignalAxis.PROJECT, 1.3),
    SignalDefinition("policy_period", "Policy period", SignalAxis.COVER, 1.2, binary=True),
    SignalDefinition("line_of_business", "Line of business", SignalAxis.COVER, 0.8, binary=True),
    SignalDefinition("broker_name", "Broker", SignalAxis.BROKER, 0.7),
)

SIGNAL_BY_KEY: dict[str, SignalDefinition] = {item.signal: item for item in SIGNALS}

#: Signals that identify the *contract* rather than describe it. One of these
#: agreeing is what lifts a match above the retrieval-only ceiling.
PRIMARY_IDENTIFIERS = frozenset({"policy_number", "project_reference"})

#: Signals that identify the *client*. A conflict on one of these caps the band
#: whatever the arithmetic says: a wording that names a different company is not
#: this loss's policy however closely its prose matches.
IDENTITY_SIGNALS = frozenset({"insured_name"})

#: Signals that *describe* a policy rather than identify one.
#:
#: This distinction is the second half of "retrieval alone cannot recommend a policy",
#: and leaving it out was a real defect: a notice for a loss the library holds no
#: policy for still agrees with half the book on the date of loss and the line of
#: business, because every in-force property policy is in force on the date and is a
#: property policy. Two agreements, an axis each, and a retrieval score normalised to
#: 1.0 against a library that had to return *something* — which computed to a
#: confident-looking 0.56 for four notices whose answer is "no policy in this library".
#:
#: So agreement here is corroboration and never identification. At least one
#: *identifying* signal has to agree before a wording can be a candidate at all.
DESCRIPTIVE_SIGNALS = frozenset({"policy_period", "line_of_business"})

#: Everything that actually names a party, a place, a contract or a reference.
IDENTIFYING_SIGNALS = frozenset(
    item.signal for item in SIGNALS if item.signal not in DESCRIPTIVE_SIGNALS
)

#: Sentences used when a signal could not be compared because the notice was silent.
_NOT_STATED: dict[str, str] = {
    "policy_number": "The notification did not state a policy number.",
    "insured_name": "The notification did not name an insured.",
    "risk_location": "The notification did not give a location for the loss.",
    "project_reference": "The notification did not quote a project or contract reference.",
    "policy_period": "The notification did not give a date of loss.",
    "line_of_business": "The notification has not been classified to a line of business.",
    "broker_name": "The notification did not name a broker.",
}


@dataclass(frozen=True, slots=True)
class Excerpt:
    """One passage of a policy wording, and why it is being shown.

    The excerpt *is* the explanation. A card claiming 87% and quoting nothing is an
    assertion; the same card quoting the two clauses the notice's own words
    retrieved, on their pages, is evidence an officer can check in the document.
    """

    chunk_ref: str
    content: str
    score: float
    page_number: int | None = None
    section_label: str | None = None
    #: Which query found it. Shown as a caption, so "this is the clause that
    #: answered your cause of loss" is legible rather than implied.
    facet: MatchFacet | None = None

    def as_dict(self) -> dict[str, Any]:
        return {
            "chunk_ref": self.chunk_ref,
            "content": self.content,
            "score": round(self.score, 4),
            "page_number": self.page_number,
            "section_label": self.section_label,
            "facet": self.facet.value if self.facet else None,
        }


@dataclass(frozen=True, slots=True)
class NoticeQuery:
    """What the notice says, flattened for comparison.

    Plain strings rather than the citation-carrying `SignalValue` the identification
    engine uses. That engine's reasons open the page a value was *read from*, which
    needs provenance; here the reasons open the page of the **policy** a value was
    matched *against*, which is the excerpt. Carrying provenance as well would be a
    second citation on the same row with nowhere to put it.
    """

    policy_number: str | None = None
    insured_name: str | None = None
    insured_organisation: str | None = None
    broker_name: str | None = None
    broker_reference: str | None = None
    project_name: str | None = None
    contract_number: str | None = None
    loss_location: str | None = None
    risk_location: str | None = None
    loss_postcode: str | None = None
    cause_of_loss: str | None = None
    loss_description: str | None = None
    claim_type: str | None = None
    line_of_business: str | None = None
    date_of_loss: date | None = None
    reference: str | None = None

    @property
    def any_location(self) -> str | None:
        """The address to compare against the schedule.

        The *risk* address first and the loss address second. On a liability or
        construction notice the two differ — a subcontractor's loss at a site is
        reported with the site's address and the policy schedules the works — and
        the risk address is the one the schedule was written from.
        """
        return _clean(self.risk_location) or _clean(self.loss_location)

    @property
    def identifying_values(self) -> int:
        """How many things the notice gave us to identify a policy with.

        Reported on the result, because a candidate list is only as good as what was
        fed to it: an officer who can see the search ran on one signal reads a 0.6
        very differently from one who cannot.
        """
        return sum(
            1
            for value in (
                _clean(self.policy_number),
                _clean(self.insured_name) or _clean(self.insured_organisation),
                self.any_location,
                _clean(self.contract_number) or _clean(self.project_name),
                _clean(self.broker_name),
            )
            if value
        )


@dataclass(frozen=True, slots=True)
class PolicyDocumentFacts:
    """One uploaded wording as the engine compares it. A projection, not the row."""

    document_id: uuid.UUID
    filename: str
    policy_id: uuid.UUID | None = None
    policy_number: str | None = None
    insured_name: str | None = None
    insurer_name: str | None = None
    broker_name: str | None = None
    policy_type: str | None = None
    line_of_business: str | None = None
    effective_date: date | None = None
    expiry_date: date | None = None
    page_count: int | None = None
    additional_insureds: tuple[str, ...] = ()
    postcodes: tuple[str, ...] = ()
    locations: tuple[str, ...] = ()
    limits: tuple[str, ...] = ()

    def insured_parties(self) -> tuple[str, ...]:
        """Every party this wording insures, first named first.

        A commercial loss is very often reported by a party who is not the *first*
        named insured — a joint venture partner, a leasing entity, a subcontractor
        insured for their interest. Comparing only against the first name scores a
        mismatch against the right policy, which is the single most damaging error
        this function prevents.
        """
        names = [self.insured_name, *self.additional_insureds]
        return tuple(dict.fromkeys(name for name in names if name and name.strip()))


@dataclass(frozen=True, slots=True)
class RetrievedPolicy:
    """One wording's retrieval result, before any corroboration.

    `retrieval_score` is already normalised to 0..1 against the best-scoring
    document in this run, which is what makes the configured thresholds mean the
    same thing whether the vector store answered, Postgres answered, or both did.
    """

    facts: PolicyDocumentFacts
    retrieval_score: float
    excerpts: tuple[Excerpt, ...] = ()
    facets_hit: frozenset[MatchFacet] = frozenset()
    chunks_matched: int = 0


@dataclass(frozen=True, slots=True)
class SignalResult:
    """One corroborating comparison, with its working shown."""

    signal: str
    label: str
    axis: SignalAxis
    outcome: SignalOutcome
    weight: float
    explanation: str
    score: float | None = None
    binary: bool = False
    notice_value: str | None = None
    policy_value: str | None = None

    @property
    def compared(self) -> bool:
        return self.outcome in (
            SignalOutcome.MATCH,
            SignalOutcome.PARTIAL,
            SignalOutcome.MISMATCH,
        )

    @property
    def agreed(self) -> bool:
        return self.outcome in (SignalOutcome.MATCH, SignalOutcome.PARTIAL)

    def as_dict(self) -> dict[str, Any]:
        return {
            "signal": self.signal,
            "label": self.label,
            "axis": self.axis.value,
            "outcome": self.outcome.value,
            "weight": round(self.weight, 3),
            "score": None if self.score is None else round(self.score, 4),
            "binary": self.binary,
            "explanation": self.explanation,
            "notice_value": self.notice_value,
            "policy_value": self.policy_value,
        }


@dataclass(frozen=True, slots=True)
class MatchWarning:
    """Something true about this policy that changes no rank.

    Kept apart from the signals for the reason `policy_identification` keeps them
    apart: a wording can be certainly the right policy and a poor answer to this
    loss. Collapsing coverage plausibility into identity confidence hides the thing
    the officer most needs to see, and stating a coverage decision is not this
    module's job — every sentence below says "may" where it is about cover.
    """

    code: str
    detail: str

    def as_dict(self) -> dict[str, str]:
        return {"code": self.code, "detail": self.detail}


@dataclass(frozen=True, slots=True)
class PolicyMatch:
    """One ranked policy wording, and everything the card draws."""

    facts: PolicyDocumentFacts
    score: float
    retrieval_score: float
    corroboration_score: float | None
    confidence: PolicyConfidence
    period_outcome: PolicyPeriodOutcome
    rank: int
    recommended: bool
    signals: tuple[SignalResult, ...]
    reasons: tuple[str, ...]
    warnings: tuple[MatchWarning, ...]
    excerpts: tuple[Excerpt, ...]
    facets_hit: frozenset[MatchFacet]
    chunks_matched: int

    @property
    def document_id(self) -> uuid.UUID:
        return self.facts.document_id

    @property
    def actionable(self) -> bool:
        """Whether this is a candidate rather than an explanation of a near miss."""
        return self.confidence is not PolicyConfidence.REJECTED


@dataclass(slots=True)
class PolicyMatchResult:
    """The whole answer for one notice."""

    matches: list[PolicyMatch] = field(default_factory=list)
    #: Compared and rejected. An *explanation*, not an option — which is why they
    #: are a separate list rather than the tail of `matches`. "Why is my policy not
    #: listed" is the question this list exists to answer.
    rejected: list[PolicyMatch] = field(default_factory=list)
    documents_compared: int = 0
    chunks_considered: int = 0
    signals_available: int = 0
    facets_queried: tuple[MatchFacet, ...] = ()
    strategy: str = "none"
    degraded: bool = False
    engine_version: str = ENGINE_VERSION

    @property
    def best(self) -> PolicyMatch | None:
        return self.matches[0] if self.matches else None

    @property
    def recommended(self) -> PolicyMatch | None:
        return next((item for item in self.matches if item.recommended), None)

    @property
    def ambiguous(self) -> bool:
        """Two candidates too close to choose between.

        Reported rather than tie-broken. Two sister companies matching on name,
        broker and line of business is precisely when the officer has a choice, and
        pre-selecting one hides that it existed.
        """
        return len(self.matches) >= 2 and not any(item.recommended for item in self.matches)


# ---------------------------------------------------------------------------
# The engine
# ---------------------------------------------------------------------------


def match(
    notice: NoticeQuery,
    retrieved: list[RetrievedPolicy],
    *,
    config: PolicyLibrarySettings | None = None,
    strategy: str = "none",
    degraded: bool = False,
    chunks_considered: int = 0,
    facets_queried: tuple[MatchFacet, ...] = (),
) -> PolicyMatchResult:
    """Rank the retrieved wordings against the notice, and say why."""
    limits = config or settings.policy_library

    scored = [_score_one(notice, candidate, limits) for candidate in retrieved]
    scored.sort(key=lambda item: (-item[0], item[1].facts.filename))

    result = PolicyMatchResult(
        documents_compared=len(retrieved),
        chunks_considered=chunks_considered,
        signals_available=notice.identifying_values,
        facets_queried=facets_queried,
        strategy=strategy,
        degraded=degraded,
    )

    # Classify before partitioning, not after. The score gets a wording over the weak
    # threshold; the *ladder* decides whether it is a candidate at all — and a wording
    # that scored 0.56 on prose and a shared date of loss is classified `REJECTED` by
    # rule. Partitioning on the score alone would put it in `matches` carrying the word
    # "rejected", which is a candidate list contradicting itself on screen.
    classified: list[
        tuple[float, RetrievedPolicy, list[SignalResult], float | None, PolicyConfidence]
    ] = []
    for score, candidate, signals, corroboration in scored:
        confidence = (
            _classify(score, signals, corroboration, limits)
            if score >= limits.match_weak_threshold
            else PolicyConfidence.REJECTED
        )
        classified.append((score, candidate, signals, corroboration, confidence))

    actionable = [item for item in classified if item[4] is not PolicyConfidence.REJECTED]
    rejected = [item for item in classified if item[4] is PolicyConfidence.REJECTED]

    #: The recommendation is withheld when the second candidate is within the
    #: ambiguity margin of the first. Both are then shown as candidates.
    lead = actionable[0][0] if actionable else 0.0
    runner_up = actionable[1][0] if len(actionable) > 1 else 0.0
    close = len(actionable) > 1 and (lead - runner_up) < limits.match_ambiguity_margin

    for rank, (score, candidate, signals, corroboration, confidence) in enumerate(
        actionable[: limits.match_max_results], start=1
    ):
        recommended = (
            rank == 1
            and not close
            and confidence in (PolicyConfidence.EXACT, PolicyConfidence.STRONG)
        )
        result.matches.append(
            _build(
                notice,
                candidate,
                signals,
                score=score,
                corroboration=corroboration,
                confidence=confidence,
                rank=rank,
                recommended=recommended,
            )
        )

    for rank, (score, candidate, signals, corroboration, _) in enumerate(
        rejected[: limits.match_max_results], start=len(result.matches) + 1
    ):
        result.rejected.append(
            _build(
                notice,
                candidate,
                signals,
                score=score,
                corroboration=corroboration,
                confidence=PolicyConfidence.REJECTED,
                rank=rank,
                recommended=False,
            )
        )

    return result


def compare(
    notice: NoticeQuery, facts: PolicyDocumentFacts
) -> tuple[list[SignalResult], float | None]:
    """Every corroborating signal against one wording, and their weighted mean.

    Exposed because it is the half of this engine that is worth calling on its own:
    an officer who found a wording by searching the library needs it scored against
    the notice the same way a retrieved one was, and this is that scoring without
    the retrieval half.

    The mean is over what could be **compared**, never over the whole signal table.
    A sparse notice is not punished for being sparse — a notice with a policy number
    and nothing else scores 1.0 on the one thing it said, and the result reports that
    one signal was compared so the number is read for what it is.
    """
    results = [_compare_one(definition, notice, facts) for definition in SIGNALS]
    parts = [
        (item.score if item.score is not None else 0.0, item.weight)
        for item in results
        if item.compared
    ]
    if not parts:
        return results, None
    total = sum(weight for _, weight in parts)
    return results, sum(score * weight for score, weight in parts) / total if total else None


# --- scoring ------------------------------------------------------------------


def _score_one(
    notice: NoticeQuery, candidate: RetrievedPolicy, limits: PolicyLibrarySettings
) -> tuple[float, RetrievedPolicy, list[SignalResult], float | None]:
    """The combined score for one wording."""
    signals, corroboration = compare(notice, candidate.facts)
    retrieval = _clamp(candidate.retrieval_score)

    # A second facet agreeing is worth more than the first one agreeing harder. A
    # wording whose identity clause *and* whose peril clause both answered the
    # notice is a different quality of answer from one that answered either twice
    # over, and a plain mean over passage scores cannot see the difference.
    breadth = min(len(candidate.facets_hit), 3) / 3.0
    relevance = _clamp(0.78 * retrieval + 0.22 * breadth)

    if corroboration is None:
        # Nothing about identity could be compared. The prose is all there is, and
        # prose alone is not identity — capped below the possible band.
        return (
            round(min(relevance, RETRIEVAL_ONLY_CEILING), 6),
            candidate,
            signals,
            None,
        )

    combined = RETRIEVAL_WEIGHT * relevance + (1.0 - RETRIEVAL_WEIGHT) * corroboration

    # A wording the notice's own policy number names exactly is that policy. The
    # floor stops a policy identified beyond doubt from being ranked below one whose
    # boilerplate happened to retrieve better — which arithmetic alone permits,
    # because a 4.0-weighted match still shares the mean with everything that was
    # silent.
    if _tier(signals, "policy_number") is SignalOutcome.MATCH:
        combined = max(combined, limits.match_strong_threshold + 0.02)

    return round(_clamp(combined), 6), candidate, signals, corroboration


def _classify(
    score: float,
    signals: list[SignalResult],
    corroboration: float | None,
    limits: PolicyLibrarySettings,
) -> PolicyConfidence:
    """The band, from *which* signals agreed and not only from the total.

    A ladder rather than three thresholds, for the reason the identification engine
    gives: the mean is a summary and the band is a judgement, and the two disagree in
    exactly the case that matters. An exact policy number beside a mismatched
    insured name computes to something middling and would be promoted by arithmetic;
    here it is capped by rule, because two documents cannot both be the contract for
    one insured.
    """
    by_signal = {item.signal: item for item in signals}
    agreed = {item.signal for item in signals if item.agreed}
    conflicted = {item.signal for item in signals if item.outcome is SignalOutcome.MISMATCH}
    axes = {by_signal[name].axis for name in agreed}

    identity_conflict = bool(conflicted & IDENTITY_SIGNALS)
    primary_agreed = bool(agreed & PRIMARY_IDENTIFIERS)
    period = by_signal.get("policy_period")
    outside_period = period is not None and period.outcome is SignalOutcome.MISMATCH

    if corroboration is None:
        # Retrieval only. Never above weak: the wording reads like the loss and
        # nothing says it is this loss's contract.
        return (
            PolicyConfidence.WEAK
            if score >= limits.match_weak_threshold
            else PolicyConfidence.REJECTED
        )

    # The gate that keeps the `NO_MATCH` notices off the screen. A library full of
    # contractors' policies will always retrieve *something* for a contractor's loss,
    # and will always agree with it on the date and the line of business — so neither
    # of those counts here. Something has to name the party, the place, the contract or
    # the reference, or this is not a candidate whatever the prose scored.
    if not (agreed & IDENTIFYING_SIGNALS):
        return PolicyConfidence.REJECTED

    # A unique reference actively disagreed and nothing about the client agreed. The
    # notice quoted a policy number and it is not this one; a shared broker or a
    # neighbouring postcode is not enough to overrule that.
    if "policy_number" in conflicted and not (agreed & {"insured_name", "risk_location"}):
        return PolicyConfidence.REJECTED

    if (
        _tier(signals, "policy_number") is SignalOutcome.MATCH
        and not outside_period
        and not identity_conflict
    ):
        return PolicyConfidence.EXACT

    if identity_conflict:
        # Capped by rule, and the officer is told why in as many words — see
        # `_warnings`. A wording naming a different company is at best a lead.
        return (
            PolicyConfidence.POSSIBLE
            if score >= limits.match_possible_threshold
            else PolicyConfidence.WEAK
        )

    if (primary_agreed or len(axes) >= 2) and score >= limits.match_strong_threshold:
        return PolicyConfidence.STRONG

    if score >= limits.match_possible_threshold:
        return PolicyConfidence.POSSIBLE

    return (
        PolicyConfidence.WEAK if score >= limits.match_weak_threshold else PolicyConfidence.REJECTED
    )


def _build(
    notice: NoticeQuery,
    candidate: RetrievedPolicy,
    signals: list[SignalResult],
    *,
    score: float,
    corroboration: float | None,
    confidence: PolicyConfidence,
    rank: int,
    recommended: bool,
) -> PolicyMatch:
    return PolicyMatch(
        facts=candidate.facts,
        score=score,
        retrieval_score=round(_clamp(candidate.retrieval_score), 6),
        corroboration_score=None if corroboration is None else round(corroboration, 6),
        confidence=confidence,
        period_outcome=_period_outcome(notice.date_of_loss, candidate.facts),
        rank=rank,
        recommended=recommended,
        signals=tuple(signals),
        reasons=_reasons(signals, candidate),
        warnings=_warnings(notice, candidate.facts, signals),
        excerpts=candidate.excerpts,
        facets_hit=candidate.facets_hit,
        chunks_matched=candidate.chunks_matched,
    )


def _reasons(signals: list[SignalResult], candidate: RetrievedPolicy) -> tuple[str, ...]:
    """Why this policy matched, in the officer's words and heaviest first.

    Only the signals that agreed. The ones that did not are still on the card — they
    are in `signals`, with their own sentences — because a candidate list printing
    only agreements is a sales pitch rather than an audit trail. This tuple is the
    summary line, and a summary that led with a mismatch would be describing the
    wrong thing.
    """
    said = [item.explanation for item in signals if item.agreed]
    if candidate.facets_hit:
        found = ", ".join(
            FACET_LABEL[facet].lower() for facet in sorted(candidate.facets_hit, key=str)
        )
        said.append(f"The wording answered the notification on {found}.")
    return tuple(said)


def _warnings(
    notice: NoticeQuery, facts: PolicyDocumentFacts, signals: list[SignalResult]
) -> tuple[MatchWarning, ...]:
    """What is true about this wording and changes no rank."""
    found: list[MatchWarning] = []
    outcome = _period_outcome(notice.date_of_loss, facts)

    if outcome is PolicyPeriodOutcome.OUTSIDE_PERIOD:
        found.append(
            MatchWarning(
                "outside_policy_period",
                "The date of loss falls outside this wording's policy period. "
                "The policy may still be the right contract with a coverage question over it.",
            )
        )
    elif outcome is PolicyPeriodOutcome.PRIOR_TERM:
        found.append(
            MatchWarning(
                "prior_term",
                "The date of loss falls before this wording's inception. "
                "A prior term of the same policy may be the one that answers it.",
            )
        )

    line = next((item for item in signals if item.signal == "line_of_business"), None)
    if line is not None and line.outcome is SignalOutcome.MISMATCH:
        found.append(
            MatchWarning(
                "line_of_business_mismatch",
                f"This wording is a {facts.line_of_business} policy and the notification "
                f"was classified as {notice.line_of_business}. "
                "It may not be the coverage part this loss belongs on.",
            )
        )

    insured = next((item for item in signals if item.signal == "insured_name"), None)
    if insured is not None and insured.outcome is SignalOutcome.MISMATCH:
        found.append(
            MatchWarning(
                "identity_conflict",
                "The insured named on the notification does not match any party this "
                "wording insures. The confidence shown is capped because of it.",
            )
        )

    if facts.policy_id is None:
        found.append(
            MatchWarning(
                "not_linked_to_book",
                "This wording is not yet linked to a policy record, so no limit or "
                "excess check has been run against it.",
            )
        )

    return tuple(found)


# --- comparators --------------------------------------------------------------


def _compare_one(
    definition: SignalDefinition, notice: NoticeQuery, facts: PolicyDocumentFacts
) -> SignalResult:
    return _COMPARATORS[definition.signal](definition, notice, facts)


def _compare_policy_number(
    definition: SignalDefinition, notice: NoticeQuery, facts: PolicyDocumentFacts
) -> SignalResult:
    """The one identifier that is supposed to be unique, compared in three tiers.

    Exact after punctuation is dropped; then containment, because a reporter quoting
    `4471-88210` of `CP-4471-88210` is quoting this policy; then near-agreement,
    which is what catches a transposed digit — the most common defect in a number
    quoted from memory, and the one an exact-string lookup silently answers "no
    such policy" to.

    A number the notice states which is *also* a plausible rendering of the
    broker's own reference is not scored here as a mismatch. Brokers put their
    scheme reference in the field a form labels "policy number", and punishing the
    right policy for the wrong label is how a correct match gets demoted.
    """
    stated = _clean(notice.policy_number) or _clean(notice.broker_reference)
    if stated is None:
        return _missing(definition, policy_value=facts.policy_number)
    if not _clean(facts.policy_number):
        return _not_compared(
            definition,
            "No policy number could be read from this wording, so the number on the "
            "notification could not be checked against it.",
            notice_value=stated,
        )

    left = normalise_reference(stated)
    right = normalise_reference(facts.policy_number)
    if not left or not right:
        return _not_compared(
            definition,
            "Neither reference survived normalisation.",
            notice_value=stated,
            policy_value=facts.policy_number,
        )

    if left == right:
        return _result(
            definition,
            SignalOutcome.MATCH,
            1.0,
            f"The policy number on the notification is this wording's own number, "
            f"{facts.policy_number}.",
            notice_value=stated,
            policy_value=facts.policy_number,
        )

    if left in right or right in left:
        return _result(
            definition,
            SignalOutcome.MATCH,
            _REFERENCE_CONTAINED,
            f"The reference quoted on the notification is contained in this wording's "
            f"number, {facts.policy_number}.",
            notice_value=stated,
            policy_value=facts.policy_number,
        )

    ratio = sequence_ratio(left, right)
    if ratio >= _REFERENCE_NEAR:
        return _result(
            definition,
            SignalOutcome.PARTIAL,
            0.72,
            f"The number on the notification is one or two characters from this "
            f"wording's number, {facts.policy_number} — consistent with a transposed "
            f"or mistyped digit.",
            notice_value=stated,
            policy_value=facts.policy_number,
        )

    return _result(
        definition,
        SignalOutcome.MISMATCH,
        0.0,
        f"The policy number on the notification is not this wording's number, "
        f"{facts.policy_number}.",
        notice_value=stated,
        policy_value=facts.policy_number,
    )


def _compare_insured(
    definition: SignalDefinition, notice: NoticeQuery, facts: PolicyDocumentFacts
) -> SignalResult:
    """The client, against every party the wording insures.

    Against *every* party, first named and additional alike, because the party
    reporting a commercial loss is frequently not the first name on the
    declarations. The reason names which one matched, so an officer reading "matches
    the additional named insured Ironbark Equipment Leasing, LLC" can see the
    inference rather than being asked to trust it.

    `entity_similarity` rather than `name_similarity`, and the difference is the
    point: a strict subset is capped below the match threshold, so "Sundale Property
    Group" against "Sundale Property Group (Wisconsin), LLC" ranks without binding.
    Two companies in a group are the classic way commercial matching goes wrong.
    """
    stated = _clean(notice.insured_name) or _clean(notice.insured_organisation)
    parties = facts.insured_parties()
    if stated is None:
        return _missing(definition, policy_value=parties[0] if parties else None)
    if not parties:
        return _not_compared(
            definition,
            "No insured could be read from this wording.",
            notice_value=stated,
        )

    best_name, best_score = "", 0.0
    for party in parties:
        scored = entity_similarity(stated, party)
        if scored > best_score:
            best_name, best_score = party, scored

    if best_score >= _NAME_MATCH:
        first = parties[0]
        where = (
            "the named insured"
            if best_name == first
            else f"an additional named insured, {best_name}"
        )
        return _result(
            definition,
            SignalOutcome.MATCH,
            best_score,
            f"The insured on the notification matches {where}.",
            notice_value=stated,
            policy_value=best_name,
        )

    if best_score >= _NAME_RELATED:
        return _result(
            definition,
            SignalOutcome.PARTIAL,
            best_score,
            f"The insured on the notification is close to {best_name} but not the same "
            f"entity — the two may be companies in one group.",
            notice_value=stated,
            policy_value=best_name,
        )

    return _result(
        definition,
        SignalOutcome.MISMATCH,
        0.0,
        f"The insured on the notification does not match any party this wording "
        f"insures — it names {parties[0]}.",
        notice_value=stated,
        policy_value=parties[0],
    )


def _compare_location(
    definition: SignalDefinition, notice: NoticeQuery, facts: PolicyDocumentFacts
) -> SignalResult:
    """The site, against the wording's schedule of premises.

    The postcode first and on its own, because it is the highest-value token an
    address carries: an exact postcode agreement against a scheduled premises is
    worth far more than a fuzzy street-name overlap, and a loss at warehouse seven
    of twelve scores nothing against a mailing address.
    """
    postcode = _clean(notice.loss_postcode)
    if postcode and facts.postcodes:
        wanted = normalise(postcode).replace(" ", "")
        for known in facts.postcodes:
            if normalise(known).replace(" ", "") == wanted:
                return _result(
                    definition,
                    SignalOutcome.MATCH,
                    1.0,
                    f"The loss postcode {postcode} is a premises scheduled on this wording.",
                    notice_value=postcode,
                    policy_value=known,
                )

    address = notice.any_location
    if address is None:
        return _missing(definition)
    if not facts.locations and not facts.postcodes:
        return _not_compared(
            definition,
            "No premises schedule could be read from this wording.",
            notice_value=address,
        )

    best_line, best_score = "", 0.0
    for line in facts.locations:
        scored = location_similarity(address, line)
        if scored > best_score:
            best_line, best_score = line, scored

    if best_score >= _LOCATION_MATCH:
        return _result(
            definition,
            SignalOutcome.MATCH,
            best_score,
            f"The location on the notification matches a premises on this wording: {best_line}.",
            notice_value=address,
            policy_value=best_line,
        )
    if best_score >= _LOCATION_RELATED:
        return _result(
            definition,
            SignalOutcome.PARTIAL,
            best_score,
            f"The location on the notification partly matches {best_line} — the same "
            f"town or street, not the same address.",
            notice_value=address,
            policy_value=best_line,
        )
    return _result(
        definition,
        SignalOutcome.MISMATCH,
        0.0,
        "The location on the notification does not match any premises scheduled on this wording.",
        notice_value=address,
        policy_value=facts.locations[0] if facts.locations else None,
    )


def _compare_project(
    definition: SignalDefinition, notice: NoticeQuery, facts: PolicyDocumentFacts
) -> SignalResult:
    """A contract number or project name, against the wording's text-derived facts.

    Compared against the policy *type* and the premises schedule rather than a
    dedicated column, because a wording states its project in its heading and its
    site schedule — `POL-BR-3358` is "Rivergate Commons Phase II" in both. A
    construction notice reported by the employer rather than the named insured is
    identified on exactly this signal and on nothing else, which is why it carries a
    primary identifier's weight.
    """
    reference = _clean(notice.contract_number)
    project = _clean(notice.project_name)
    if not reference and not project:
        return _missing(definition)

    haystack = " ".join(
        part for part in (facts.policy_type, facts.filename, *facts.locations) if part
    )
    if not haystack.strip():
        return _not_compared(
            definition,
            "This wording states no project or contract.",
            notice_value=reference or project,
        )

    if reference:
        needle = normalise_reference(reference)
        if len(needle) >= 4 and needle in normalise_reference(haystack):
            return _result(
                definition,
                SignalOutcome.MATCH,
                1.0,
                f"The contract reference {reference} appears on this wording.",
                notice_value=reference,
                policy_value=facts.policy_type,
            )

    if project:
        overlap = _token_overlap(project, haystack)
        if overlap >= 0.75:
            return _result(
                definition,
                SignalOutcome.MATCH,
                overlap,
                "The project named on the notification is the project this wording covers.",
                notice_value=project,
                policy_value=facts.policy_type,
            )
        if overlap >= 0.45:
            return _result(
                definition,
                SignalOutcome.PARTIAL,
                overlap,
                "The project named on the notification partly matches this wording's project.",
                notice_value=project,
                policy_value=facts.policy_type,
            )

    return _not_compared(
        definition,
        "Neither the contract reference nor the project name could be found on this wording.",
        notice_value=reference or project,
    )


def _compare_period(
    definition: SignalDefinition, notice: NoticeQuery, facts: PolicyDocumentFacts
) -> SignalResult:
    """Where the date of loss falls relative to the wording's own term.

    Four outcomes rather than two, and `PRIOR_TERM` is the one that earns its
    complexity: losses are discovered late, so a notice dated before inception is
    routine and the useful answer is "you have matched this year's wording, the loss
    is in last year's" rather than "no match".
    """
    if notice.date_of_loss is None:
        return _missing(definition)
    if facts.effective_date is None or facts.expiry_date is None:
        return _not_compared(
            definition,
            "No policy period could be read from this wording.",
            notice_value=f"{notice.date_of_loss:%d %b %Y}",
        )

    stated = f"{notice.date_of_loss:%d %b %Y}"
    term = f"{facts.effective_date:%d %b %Y} to {facts.expiry_date:%d %b %Y}"

    if facts.effective_date <= notice.date_of_loss <= facts.expiry_date:
        return _result(
            definition,
            SignalOutcome.MATCH,
            1.0,
            f"The date of loss falls inside this wording's policy period, {term}.",
            notice_value=stated,
            policy_value=term,
        )
    if notice.date_of_loss < facts.effective_date:
        return _result(
            definition,
            SignalOutcome.PARTIAL,
            0.3,
            f"The date of loss falls before this wording's inception. Its period is {term}.",
            notice_value=stated,
            policy_value=term,
        )
    return _result(
        definition,
        SignalOutcome.MISMATCH,
        0.0,
        f"The date of loss falls after this wording's expiry. Its period is {term}.",
        notice_value=stated,
        policy_value=term,
    )


def _compare_line(
    definition: SignalDefinition, notice: NoticeQuery, facts: PolicyDocumentFacts
) -> SignalResult:
    """The line of business, with the equivalences a real book needs."""
    stated = _clean(notice.line_of_business)
    if not stated or stated == LineOfBusiness.UNKNOWN.value:
        return _missing(definition)
    if not facts.line_of_business:
        return _not_compared(
            definition,
            "The line of business could not be read from this wording.",
            notice_value=stated,
        )

    if stated == facts.line_of_business:
        return _result(
            definition,
            SignalOutcome.MATCH,
            1.0,
            f"This is a {facts.line_of_business} wording and the notification is a {stated} loss.",
            notice_value=stated,
            policy_value=facts.line_of_business,
        )

    pair = {stated, facts.line_of_business}
    if any(pair <= group for group in _EQUIVALENT_LINES):
        return _result(
            definition,
            SignalOutcome.PARTIAL,
            0.8,
            f"This is a {facts.line_of_business} wording, which answers a {stated} "
            f"notification on the same risk.",
            notice_value=stated,
            policy_value=facts.line_of_business,
        )

    return _result(
        definition,
        SignalOutcome.MISMATCH,
        0.0,
        f"This is a {facts.line_of_business} wording and the notification is a {stated} loss.",
        notice_value=stated,
        policy_value=facts.line_of_business,
    )


def _compare_broker(
    definition: SignalDefinition, notice: NoticeQuery, facts: PolicyDocumentFacts
) -> SignalResult:
    """The broker, which on a commercial desk is the second-best identifier there is.

    Worth its weight because most commercial notices arrive through a broking
    house, and a broker places a client's whole programme — so "the notification's
    broker is the producer on this wording" narrows the book hard even when nothing
    about the policy itself was quoted.
    """
    stated = _clean(notice.broker_name)
    if stated is None:
        return _missing(definition, policy_value=facts.broker_name)
    if not _clean(facts.broker_name):
        return _not_compared(
            definition, "No producer could be read from this wording.", notice_value=stated
        )

    scored = entity_similarity(stated, facts.broker_name)
    if scored >= _NAME_MATCH:
        return _result(
            definition,
            SignalOutcome.MATCH,
            scored,
            f"The broker on the notification is the producer on this wording, {facts.broker_name}.",
            notice_value=stated,
            policy_value=facts.broker_name,
        )
    if scored >= _NAME_RELATED:
        return _result(
            definition,
            SignalOutcome.PARTIAL,
            scored,
            f"The broker on the notification resembles this wording's producer, "
            f"{facts.broker_name}.",
            notice_value=stated,
            policy_value=facts.broker_name,
        )
    return _result(
        definition,
        SignalOutcome.MISMATCH,
        0.0,
        f"The broker on the notification is not this wording's producer, {facts.broker_name}.",
        notice_value=stated,
        policy_value=facts.broker_name,
    )


_COMPARATORS: dict[str, Any] = {
    "policy_number": _compare_policy_number,
    "insured_name": _compare_insured,
    "risk_location": _compare_location,
    "project_reference": _compare_project,
    "policy_period": _compare_period,
    "line_of_business": _compare_line,
    "broker_name": _compare_broker,
}


# --- helpers ------------------------------------------------------------------


def _result(
    definition: SignalDefinition,
    outcome: SignalOutcome,
    score: float,
    explanation: str,
    *,
    notice_value: str | None = None,
    policy_value: str | None = None,
) -> SignalResult:
    return SignalResult(
        signal=definition.signal,
        label=definition.label,
        axis=definition.axis,
        outcome=outcome,
        weight=definition.weight,
        explanation=explanation,
        score=round(_clamp(score), 4),
        binary=definition.binary,
        notice_value=notice_value,
        policy_value=policy_value,
    )


def _missing(definition: SignalDefinition, *, policy_value: str | None = None) -> SignalResult:
    """The notice was silent. A gap an officer can go and fill, and said so."""
    return SignalResult(
        signal=definition.signal,
        label=definition.label,
        axis=definition.axis,
        outcome=SignalOutcome.MISSING,
        weight=definition.weight,
        explanation=_NOT_STATED[definition.signal],
        binary=definition.binary,
        policy_value=policy_value,
    )


def _not_compared(
    definition: SignalDefinition,
    explanation: str,
    *,
    notice_value: str | None = None,
    policy_value: str | None = None,
) -> SignalResult:
    """The signal does not apply here — a different statement from a gap.

    Both drop out of the mean and both are listed, because silence tells the officer
    nothing and the two silences send them to two different places.
    """
    return SignalResult(
        signal=definition.signal,
        label=definition.label,
        axis=definition.axis,
        outcome=SignalOutcome.NOT_COMPARED,
        weight=definition.weight,
        explanation=explanation,
        binary=definition.binary,
        notice_value=notice_value,
        policy_value=policy_value,
    )


def _tier(signals: list[SignalResult], name: str) -> SignalOutcome | None:
    found = next((item for item in signals if item.signal == name), None)
    return found.outcome if found else None


def _period_outcome(loss_date: date | None, facts: PolicyDocumentFacts) -> PolicyPeriodOutcome:
    if loss_date is None or facts.effective_date is None or facts.expiry_date is None:
        return PolicyPeriodOutcome.UNKNOWN
    if facts.effective_date <= loss_date <= facts.expiry_date:
        return PolicyPeriodOutcome.IN_FORCE
    if loss_date < facts.effective_date:
        return PolicyPeriodOutcome.PRIOR_TERM
    return PolicyPeriodOutcome.OUTSIDE_PERIOD


def _token_overlap(needle: str, haystack: str) -> float:
    """How much of `needle` appears in `haystack`, by distinctive token.

    Containment rather than Jaccard: the haystack is a heading plus a schedule and
    the needle is a project name, so penalising the needle for the words it does not
    share would score every real match near zero.
    """
    left, right = tokens(needle), tokens(haystack)
    if not left or not right:
        return 0.0
    return len(left & right) / len(left)


def _clean(value: str | None) -> str | None:
    if value is None:
        return None
    stripped = value.strip()
    return stripped or None


def _clamp(value: float) -> float:
    return max(0.0, min(1.0, value))


def line_is_compatible(notice_line: str | None, policy_line: str | None) -> bool:
    """Whether a wording's line can answer a notice's, for *filtering*.

    Kept public and kept apart from `_compare_line`, because filtering and scoring
    are different decisions with different failure costs. A wrong score is visible
    on the card and an officer can overrule it; a wrong filter removes the policy
    from the screen and nobody ever learns it was there. So this is deliberately
    generous: unknown on either side passes, and every equivalence passes.
    """
    if not notice_line or not policy_line:
        return True
    if notice_line == LineOfBusiness.UNKNOWN.value:
        return True
    if notice_line == policy_line:
        return True
    pair = {notice_line, policy_line}
    return any(pair <= group for group in _EQUIVALENT_LINES)


__all__ = [
    "DESCRIPTIVE_SIGNALS",
    "ENGINE_VERSION",
    "FACET_LABEL",
    "IDENTIFYING_SIGNALS",
    "IDENTITY_SIGNALS",
    "PRIMARY_IDENTIFIERS",
    "RETRIEVAL_ONLY_CEILING",
    "RETRIEVAL_WEIGHT",
    "SIGNALS",
    "SIGNAL_BY_KEY",
    "Excerpt",
    "MatchFacet",
    "MatchWarning",
    "NoticeQuery",
    "PolicyDocumentFacts",
    "PolicyMatch",
    "PolicyMatchResult",
    "RetrievedPolicy",
    "SignalDefinition",
    "SignalResult",
    "compare",
    "line_is_compatible",
    "match",
]
