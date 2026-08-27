"""Identifying which policy a notice belongs to.

The first decision on a commercial claim, written as a domain module rather than
a scoring function, because the officer's question is not "what is the score" but
"why is that policy at the top and mine at number three".

The engine has five parts and they are deliberately separable:

1. **Signals.** A declared table of what can be compared, what each comparison is
   worth, and — critically — which axis of identity it speaks to. A broker's email
   domain is evidence about *the broker*; the insured's is evidence about *the
   insured*. Conflating the two is the single most common defect in a claims
   matcher, and the taxonomy here is what stops it.
2. **Normalisation.** The notice becomes a `NoticeSignals`, the policy becomes a
   `PolicyFacts`. Both are plain, both are built once, and every comparison below
   reads them rather than reaching into an ORM row. That is what makes the engine
   testable with two dictionaries and no database.
3. **Comparison.** One function per signal, each returning a typed
   `SignalResult` — an outcome, a score, a weight, a sentence, and where on the
   notice the value was read from. Never a bare float: a 0.6 on the location axis
   and a 0.6 on the email axis mean different things and the officer has to be
   told which.
4. **Scoring and classification.** A weighted mean over what could actually be
   compared, then a *deterministic ladder* to a confidence band. The ladder is not
   a set of thresholds on the mean, and that is the point: an exact policy number
   against a mismatched insured name is suspicious, and no arithmetic over a
   weighted average expresses "suspicious".
5. **Explanation.** Reasons, missing signals, and warnings, kept apart. A reason
   is about identity confidence. A warning is about coverage plausibility. A
   candidate can be certainly the right policy and a poor fit for the loss at the
   same time, and one percentage cannot say both.

Three rules hold throughout and each is a product decision, not an implementation
detail:

* **Nothing here selects a policy.** It ranks, explains, and recommends. Binding
  is a person's act, recorded with their name on it.
* **Absence is reported, never inferred.** A signal neither side stated is
  `NOT_COMPARED`; one the policy has and the notice does not is `MISSING`. Both
  are dropped from the mean — a sparse notice is not punished for being sparse —
  and both are *listed*, because "no date of loss was read" is the most actionable
  line on the panel.
* **Failures are shown.** A candidate list that prints only agreements is a sales
  pitch. The officer's job on this screen is to find the one signal the machine
  read wrong, which needs the signals it got wrong.

Every function in this module is pure. An auditor recomputing a 0.91 in two
years' time gets 0.91.
"""

from __future__ import annotations

import re
from dataclasses import dataclass, field
from datetime import date, timedelta
from enum import StrEnum
from typing import Any

from app.core.config import FNOLSettings
from app.domain.enums import (
    LineOfBusiness,
    PolicyConfidence,
    PolicyIdentificationStatus,
    PolicyMatchStrength,
    PolicyPeriodOutcome,
    SignalOutcome,
)
from app.domain.matching import (
    location_similarity,
    name_similarity,
    normalise,
    normalise_organisation,
    normalise_reference,
    sequence_ratio,
    tokens,
    weighted_score,
)

ENGINE_VERSION = "policy-identification/1"


# ---------------------------------------------------------------------------
# The signal taxonomy
# ---------------------------------------------------------------------------


class SignalAxis(StrEnum):
    """What a signal is evidence *about*.

    The grouping the panel draws, and the grouping the confidence ladder reasons
    over. `POLICY` identifies the contract, `INSURED` identifies the client,
    `BROKER` identifies their agent, `PROJECT` identifies the works, `RISK`
    locates them and `COVER` describes the shape of the cover. They are not
    interchangeable: two of them agreeing across axes is worth far more than two
    agreeing on the same one.
    """

    POLICY = "policy"
    INSURED = "insured"
    BROKER = "broker"
    PROJECT = "project"
    RISK = "risk"
    COVER = "cover"


AXIS_LABEL: dict[SignalAxis, str] = {
    SignalAxis.POLICY: "Policy identity",
    SignalAxis.INSURED: "Insured identity",
    SignalAxis.BROKER: "Broker identity",
    SignalAxis.PROJECT: "Project and contract",
    SignalAxis.RISK: "Risk and location",
    SignalAxis.COVER: "Cover and period",
}


@dataclass(frozen=True, slots=True)
class SignalDefinition:
    """One comparable thing, declared rather than coded into the scorer.

    `evidence_field_key` is the dataset field the notice's side of this signal was
    read from, which is what lets the review screen open the page a reason came
    from. A signal with none — the sender's own domain, taken from the envelope —
    says so by holding `None`, and the panel simply does not offer a link.
    """

    signal: str
    label: str
    axis: SignalAxis
    weight: float
    #: The dataset field key behind the notice's value, for the evidence viewer.
    evidence_field_key: str | None = None
    #: Signals whose score is a yes or a no; a percentage beside them is noise.
    binary: bool = False
    #: Only compared when the risk is a construction or engineering one.
    construction_only: bool = False


#: Weights, heaviest first, and the order the panel lists them in.
#:
#: The policy number dominates because it is the only identifier that is supposed
#: to be unique. The broker's own reference comes second and is the change most
#: worth having: brokers quote their own scheme reference far more reliably than
#: they quote the carrier's number, because it is what their own system prints.
SIGNALS: tuple[SignalDefinition, ...] = (
    SignalDefinition(
        "policy_number",
        "Policy number",
        SignalAxis.POLICY,
        5.0,
        evidence_field_key="policy.policy_number",
    ),
    SignalDefinition(
        "broker_reference",
        "Broker reference",
        SignalAxis.BROKER,
        2.5,
        evidence_field_key="policy.broker_reference",
    ),
    SignalDefinition(
        "contract_number",
        "Contract number",
        SignalAxis.PROJECT,
        2.2,
        evidence_field_key="project.contract_number",
        construction_only=True,
    ),
    SignalDefinition(
        "insured_name",
        "Insured name",
        SignalAxis.INSURED,
        2.0,
        evidence_field_key="policy.insured_name",
    ),
    SignalDefinition(
        "project_name",
        "Project",
        SignalAxis.PROJECT,
        1.6,
        evidence_field_key="project.project_name",
        construction_only=True,
    ),
    SignalDefinition(
        "policy_period",
        "Policy period",
        SignalAxis.COVER,
        1.5,
        evidence_field_key="loss.date_of_loss",
        binary=True,
    ),
    SignalDefinition(
        "policy_type",
        "Policy type",
        SignalAxis.COVER,
        1.4,
        evidence_field_key="policy.policy_type",
    ),
    SignalDefinition(
        "risk_location",
        "Loss location",
        SignalAxis.RISK,
        1.3,
        evidence_field_key="loss.loss_location",
    ),
    SignalDefinition(
        "insured_organisation",
        "Insured organisation",
        SignalAxis.INSURED,
        1.2,
        evidence_field_key="policy.insured_organisation",
    ),
    SignalDefinition(
        "insured_domain",
        "Insured email domain",
        SignalAxis.INSURED,
        1.0,
        binary=True,
    ),
    SignalDefinition(
        "broker_domain",
        "Broker email domain",
        SignalAxis.BROKER,
        1.0,
        binary=True,
    ),
    SignalDefinition(
        "broker_name",
        "Broker",
        SignalAxis.BROKER,
        0.9,
        evidence_field_key="policy.broker_name",
    ),
    SignalDefinition(
        "line_of_business",
        "Line of business",
        SignalAxis.COVER,
        0.6,
        binary=True,
    ),
)

SIGNAL_BY_KEY: dict[str, SignalDefinition] = {
    definition.signal: definition for definition in SIGNALS
}

#: References that are supposed to be unique to one contract. One of these
#: matching is close to decisive on its own; one of these *mismatching* while
#: nothing else identifies the insured is what makes a candidate `REJECTED`.
PRIMARY_IDENTIFIERS: tuple[str, ...] = ("policy_number", "broker_reference", "contract_number")

#: Signals that say who the client is. A candidate has to agree on at least one of
#: these, or on a primary identifier, before it is a candidate at all — otherwise
#: every property policy on the book qualifies on dates and line of business.
IDENTITY_SIGNALS: tuple[str, ...] = (
    "insured_name",
    "insured_organisation",
    "insured_domain",
    "project_name",
)

#: Mailbox domains that identify a mail provider rather than an organisation.
#: A notice from a `gmail.com` address tells you nothing about the broker, and
#: scoring it as a domain mismatch would punish a policy for a fact about
#: somebody's email habits.
GENERIC_EMAIL_DOMAINS: frozenset[str] = frozenset(
    {
        "gmail.com",
        "googlemail.com",
        "outlook.com",
        "hotmail.com",
        "live.com",
        "live.co.uk",
        "yahoo.com",
        "yahoo.co.uk",
        "icloud.com",
        "me.com",
        "aol.com",
        "protonmail.com",
        "proton.me",
        "btinternet.com",
        "sky.com",
        "msn.com",
    }
)

#: Lines of business that describe the same risk. A CAR policy classified as
#: engineering and a notice classified as construction are not a mismatch.
_COMPATIBLE_LINES: tuple[frozenset[str], ...] = (
    frozenset({LineOfBusiness.CONSTRUCTION.value, LineOfBusiness.ENGINEERING.value}),
    frozenset({LineOfBusiness.LIABILITY.value, LineOfBusiness.CASUALTY.value}),
)

CONSTRUCTION_LINES: frozenset[str] = frozenset(
    {LineOfBusiness.CONSTRUCTION.value, LineOfBusiness.ENGINEERING.value}
)

#: Characters an OCR pass confuses, folded before a second comparison of a
#: reference. `P0L-2O26-3O582` and `POL-2026-30582` are the same policy read by a
#: scanner with an opinion.
_CONFUSABLES: dict[str, str] = {
    "o": "0",
    "q": "0",
    "d": "0",
    "i": "1",
    "l": "1",
    "|": "1",
    "z": "2",
    "s": "5",
    "b": "8",
    "g": "9",
    "a": "4",
    "t": "7",
}

#: A UK postcode, the highest-value token in a British address. Captured in two
#: halves because the outward code alone ("LS11") already narrows a book to a
#: handful of locations, and an outward-only agreement is worth saying out loud.
#:
#: The space between the halves is required, not optional — the same guard
#: `app.domain.policy_extraction` carries and for the same reason. This pattern is
#: searched over free text, and without the space `PC290LC`, a plant serial on a
#: construction notice's equipment list, reads as a valid postcode and becomes a
#: location token that some later notice collides with. A postcode a *person* typed
#: without the space is a different question, answered by
#: `app.domain.normalisation.parse_postcode`, where the field itself is the context.
_POSTCODE_RE = re.compile(r"\b([A-Z]{1,2}\d[A-Z\d]?)\s(\d[A-Z]{2})\b", re.IGNORECASE)

#: The same token in a US address. Guarded by the two-letter state code that
#: precedes it, for the reason `app.domain.policy_extraction` documents: a bare
#: five-digit run in an address block is as often a producer code or half a policy
#: number as it is a ZIP, and a wrong location token is worse than no location
#: token. The +4 add-on is captured so it can be discarded — it segments a
#: delivery route inside one ZIP, not a different place.
_ZIP_RE = re.compile(r"\b[A-Z]{2}\s+(\d{5})(?:-\d{4})?\b")


# ---------------------------------------------------------------------------
# The two normalised sides
# ---------------------------------------------------------------------------


@dataclass(frozen=True, slots=True)
class SignalValue:
    """One value the notice stated, and where it was read from.

    `field_key` and `quote` are what make a reason clickable. They travel with the
    value rather than being looked up later, so a signal and its citation cannot
    drift apart.
    """

    value: str
    field_key: str | None = None
    document_id: Any | None = None
    page_number: int | None = None
    quote: str | None = None
    confidence: float | None = None

    def as_dict(self) -> dict[str, Any]:
        return {
            "value": self.value,
            "field_key": self.field_key,
            "document_id": str(self.document_id) if self.document_id is not None else None,
            "page_number": self.page_number,
            "quote": self.quote,
            "confidence": self.confidence,
        }


@dataclass(frozen=True, slots=True)
class NoticeSignals:
    """What the notice and its attachments said, ready to compare.

    Built by the service from the case's columns and its extracted values. Holds
    the *reporter* apart from the *insured* apart from the *broker*, because the
    person who sent the email is usually none of the other two.
    """

    policy_number: SignalValue | None = None
    broker_reference: SignalValue | None = None
    insured_name: SignalValue | None = None
    insured_organisation: SignalValue | None = None
    insured_domain: SignalValue | None = None
    broker_name: SignalValue | None = None
    broker_domain: SignalValue | None = None
    loss_location: SignalValue | None = None
    risk_location: SignalValue | None = None
    loss_postcode: SignalValue | None = None
    project_name: SignalValue | None = None
    contract_number: SignalValue | None = None
    date_of_loss: SignalValue | None = None
    line_of_business: SignalValue | None = None
    policy_type: SignalValue | None = None
    cause_of_loss: SignalValue | None = None
    policy_period_stated: SignalValue | None = None
    estimated_loss_minor: int | None = None

    #: The parsed date of loss, held apart from its display form.
    loss_day: date | None = None

    def is_construction(self) -> bool:
        line = self.line_of_business.value if self.line_of_business else None
        return line in CONSTRUCTION_LINES

    def get(self, name: str) -> SignalValue | None:
        return getattr(self, name, None)


@dataclass(frozen=True, slots=True)
class PolicyLocationFacts:
    """One entry on the schedule of insured locations.

    A named thing rather than an address string, because the useful reason is
    "the loss location matches Location 007, the cold store" and that needs the
    reference the schedule printed.
    """

    location_ref: str | None
    description: str | None
    address: str
    postcode: str | None
    is_primary: bool = False
    sum_insured_minor: int | None = None
    deductible_minor: int | None = None

    def label(self) -> str:
        """How a reason names this place.

        The schedule's own reference when it has one — "Location 007" is what the
        officer will find on the policy document — and the address otherwise. The
        description is deliberately not used here: "the loss postcode matches depot
        exactly" is a worse sentence than naming the address, and the description
        still travels to the candidate card, where it has room to be a caption.
        """
        return self.location_ref or self.address


@dataclass(frozen=True, slots=True)
class PolicyFacts:
    """One policy as the engine compares it. A projection, not the row."""

    policy_id: Any
    policy_number: str
    insured_name: str
    line_of_business: str
    status: str
    effective_date: date
    expiry_date: date

    insured_organisation: str | None = None
    insured_email: str | None = None
    insured_domain: str | None = None
    broker_name: str | None = None
    broker_reference: str | None = None
    broker_domain: str | None = None
    insurer_name: str | None = None
    policy_type: str | None = None

    primary_location: str | None = None
    site_address: str | None = None
    locations: tuple[PolicyLocationFacts, ...] = ()

    project_name: str | None = None
    project_reference: str | None = None
    contract_number: str | None = None
    principal_name: str | None = None
    contractor_name: str | None = None
    practical_completion_date: date | None = None
    maintenance_period_months: int | None = None

    #: The term of the policy this one renewed, when there is one. Supplied by the
    #: service, which is the only layer that can follow the chain.
    prior_term: tuple[date, date] | None = None
    prior_policy_number: str | None = None

    currency: str = "GBP"
    limit_amount_minor: int | None = None
    deductible_amount_minor: int | None = None
    perils_covered: tuple[str, ...] = ()
    exclusions: tuple[str, ...] = ()

    def is_construction(self) -> bool:
        return self.line_of_business in CONSTRUCTION_LINES

    def joint_names(self) -> tuple[tuple[str, str], ...]:
        """Every party insured under this policy, labelled by their interest.

        Construction policies are written in joint names — employer, main
        contractor, and every subcontractor for their interest — so the party who
        reports a loss is very often an insured who is not the *named* insured.
        Comparing against the named insured alone fails a legitimate claim.
        """
        parties: list[tuple[str, str]] = [("the named insured", self.insured_name)]
        if self.insured_organisation and self.insured_organisation != self.insured_name:
            parties.append(("the insured organisation", self.insured_organisation))
        if self.principal_name:
            parties.append(("the principal", self.principal_name))
        if self.contractor_name:
            parties.append(("the main contractor", self.contractor_name))
        return tuple((role, name) for role, name in parties if name)

    def maintenance_expiry(self) -> date | None:
        """The end of the defects liability period, when the policy declares one."""
        months = self.maintenance_period_months
        if not months:
            return None
        start = self.practical_completion_date or self.expiry_date
        # Calendar months without dateutil: 30-day months are close enough for a
        # period whose own boundary is a practical-completion certificate, and
        # being deterministic matters more than being exact here.
        return start + timedelta(days=round(months * 30.44))

    def period_label(self) -> str:
        return f"{self.effective_date:%d %b %Y} – {self.expiry_date:%d %b %Y}"


# ---------------------------------------------------------------------------
# Results
# ---------------------------------------------------------------------------


@dataclass(frozen=True, slots=True)
class SignalResult:
    """One signal compared against one policy, with its working shown."""

    signal: str
    label: str
    axis: SignalAxis
    outcome: SignalOutcome
    weight: float
    explanation: str
    #: Always the number the weighted mean used, for every compared signal. `None`
    #: only when nothing was compared. Presentation is `binary`'s job, not this
    #: field's — a score held back for display reasons is a score the arithmetic
    #: silently loses, which is how a matching yes/no signal ends up contributing
    #: zero to the very average it agreed with.
    score: float | None = None
    #: This signal's answer is a yes or a no. The panel prints no percentage beside
    #: it, because "100%" against "the loss date falls inside the policy period" is
    #: a number pretending to be a measurement.
    binary: bool = False
    notice_value: str | None = None
    policy_value: str | None = None
    #: The dataset field the notice's value was read from, for the viewer.
    evidence_field_key: str | None = None
    document_id: Any | None = None
    page_number: int | None = None
    quote: str | None = None

    @property
    def compared(self) -> bool:
        return self.outcome in (SignalOutcome.MATCH, SignalOutcome.PARTIAL, SignalOutcome.MISMATCH)

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
            "evidence_field_key": self.evidence_field_key,
            "document_id": str(self.document_id) if self.document_id is not None else None,
            "page_number": self.page_number,
            "quote": self.quote,
        }


@dataclass(frozen=True, slots=True)
class CandidateWarning:
    """Something about the *cover*, not about the identification.

    Kept apart from the signal results because collapsing the two hides what the
    officer most needs to see: a candidate can be certainly the right policy and
    still be a poor answer to this loss. "Policy is lapsed" is not a reason to
    rank it lower — it is a reason to have a different conversation.
    """

    code: str
    detail: str
    severity: str = "warning"

    def as_dict(self) -> dict[str, Any]:
        return {"code": self.code, "detail": self.detail, "severity": self.severity}


@dataclass(frozen=True, slots=True)
class CandidateDisplay:
    """The policy as the candidate card prints it. Formatted once, on the server."""

    insured_name: str
    line_of_business: str
    policy_type: str | None
    policy_period: str
    status: str
    limit_minor: int | None
    excess_minor: int | None
    currency: str
    location: str | None
    location_label: str | None
    broker_name: str | None
    project_name: str | None
    contract_number: str | None

    def as_dict(self) -> dict[str, Any]:
        return {
            "insured_name": self.insured_name,
            "line_of_business": self.line_of_business,
            "policy_type": self.policy_type,
            "policy_period": self.policy_period,
            "status": self.status,
            "limit_minor": self.limit_minor,
            "excess_minor": self.excess_minor,
            "currency": self.currency,
            "location": self.location,
            "location_label": self.location_label,
            "broker_name": self.broker_name,
            "project_name": self.project_name,
            "contract_number": self.contract_number,
        }


@dataclass(slots=True)
class CandidateMatch:
    """One policy, ranked, with everything the officer needs to disagree."""

    policy_id: Any
    policy_number: str
    score: float
    confidence: PolicyConfidence
    signal_results: list[SignalResult]
    display: CandidateDisplay
    warnings: list[CandidateWarning] = field(default_factory=list)
    period_outcome: PolicyPeriodOutcome = PolicyPeriodOutcome.UNKNOWN
    #: The prior-year policy this candidate points at, when the loss falls in it.
    prior_policy_number: str | None = None
    rank: int = 0
    recommendation_reason: str = ""

    @property
    def compared_signals(self) -> list[SignalResult]:
        return [result for result in self.signal_results if result.compared]

    @property
    def missing_signals(self) -> list[SignalResult]:
        return [result for result in self.signal_results if not result.compared]

    @property
    def agreements(self) -> list[SignalResult]:
        return [result for result in self.signal_results if result.agreed]

    def result(self, signal: str) -> SignalResult | None:
        for entry in self.signal_results:
            if entry.signal == signal:
                return entry
        return None

    def outcome_of(self, signal: str) -> SignalOutcome:
        found = self.result(signal)
        return found.outcome if found else SignalOutcome.NOT_COMPARED

    def score_of(self, signal: str) -> float:
        found = self.result(signal)
        return found.score if found and found.score is not None else 0.0

    def as_dict(self) -> dict[str, Any]:
        return {
            "policy_id": self.policy_id,
            "policy_number": self.policy_number,
            "rank": self.rank,
            "score": round(self.score, 4),
            "confidence": self.confidence.value,
            "match_strength": STRENGTH_FOR_CONFIDENCE[self.confidence].value,
            "period_outcome": self.period_outcome.value,
            "prior_policy_number": self.prior_policy_number,
            "recommendation_reason": self.recommendation_reason,
            "compared_signal_count": len(self.compared_signals),
            "signal_count": len(self.signal_results),
            "display": self.display.as_dict(),
            "signals": [result.as_dict() for result in self.signal_results],
            "warnings": [warning.as_dict() for warning in self.warnings],
        }


@dataclass(slots=True)
class IdentificationResult:
    """The whole answer for one notice."""

    status: PolicyIdentificationStatus
    candidates: list[CandidateMatch]
    #: Candidates compared and rejected, kept so "why is my policy not here" has
    #: an answer. Never ranked with the others.
    near_misses: list[CandidateMatch] = field(default_factory=list)
    recommended_policy_id: Any | None = None
    #: Whether a second candidate is close enough to the first that choosing between
    #: them is an officer's decision. Held on the result rather than recomputed,
    #: because the service that writes `case.policy_id` has no `config` at hand and
    #: was answering the question by not asking it.
    ambiguous: bool = False
    #: What the search was run on, so the officer can see it was reasonable.
    searched_on: list[SignalResult] = field(default_factory=list)
    policies_compared: int = 0
    engine_version: str = ENGINE_VERSION

    @property
    def best(self) -> CandidateMatch | None:
        return self.candidates[0] if self.candidates else None

    @property
    def strength(self) -> PolicyMatchStrength:
        """The coarse reading the queue and the exception engine work from."""
        if not self.candidates:
            return PolicyMatchStrength.NONE
        contenders = [
            candidate
            for candidate in self.candidates[1:]
            if candidate.confidence in (PolicyConfidence.EXACT, PolicyConfidence.STRONG)
        ]
        if contenders:
            # Two strong candidates is `POSSIBLE`, not `HIGH`. The distinction the
            # queue cares about is whether a human has a choice to make, and two
            # strong matches is precisely when they do.
            return PolicyMatchStrength.POSSIBLE
        return STRENGTH_FOR_CONFIDENCE[self.candidates[0].confidence]

    def as_dict(self) -> dict[str, Any]:
        return {
            "status": self.status.value,
            "engine_version": self.engine_version,
            "strength": self.strength.value,
            "policies_compared": self.policies_compared,
            "recommended_policy_id": (
                str(self.recommended_policy_id) if self.recommended_policy_id else None
            ),
            "searched_on": [signal.as_dict() for signal in self.searched_on],
            "candidates": [
                {**candidate.as_dict(), "policy_id": str(candidate.policy_id)}
                for candidate in self.candidates
            ],
            "near_misses": [
                {**candidate.as_dict(), "policy_id": str(candidate.policy_id)}
                for candidate in self.near_misses
            ],
        }


#: How a per-candidate band reads on the queue. `WEAK` and `REJECTED` both come
#: out as `POSSIBLE` rather than `NONE`: something was found, and telling the
#: officer "no policy matched" while showing them three rows would be a lie.
STRENGTH_FOR_CONFIDENCE: dict[PolicyConfidence, PolicyMatchStrength] = {
    PolicyConfidence.EXACT: PolicyMatchStrength.EXACT,
    PolicyConfidence.STRONG: PolicyMatchStrength.HIGH,
    PolicyConfidence.POSSIBLE: PolicyMatchStrength.POSSIBLE,
    PolicyConfidence.WEAK: PolicyMatchStrength.POSSIBLE,
    PolicyConfidence.REJECTED: PolicyMatchStrength.NONE,
}


# ---------------------------------------------------------------------------
# The engine
# ---------------------------------------------------------------------------


def identify(
    notice: NoticeSignals,
    policies: list[PolicyFacts],
    *,
    config: FNOLSettings,
    confirmed_policy_id: Any | None = None,
    referred: bool = False,
) -> IdentificationResult:
    """Rank the policy book against one notice.

    The order is retrieve-then-score by design and the retrieval has already
    happened: `policies` is whatever the repository could narrow to, and every
    judgement below is made here, in Python, where it can be tested and explained.
    """
    scored = [compare(notice, policy, config=config) for policy in policies]

    viable = [
        candidate for candidate in scored if candidate.confidence is not PolicyConfidence.REJECTED
    ]
    viable.sort(key=_ranking_key, reverse=True)
    viable = viable[: config.policy_identification_max_candidates]
    for index, candidate in enumerate(viable):
        candidate.rank = index

    rejected = [
        candidate for candidate in scored if candidate.confidence is PolicyConfidence.REJECTED
    ]
    rejected.sort(key=_ranking_key, reverse=True)
    near_misses = rejected[: config.policy_identification_max_near_misses]
    for index, candidate in enumerate(near_misses):
        candidate.rank = index

    recommended = _recommended(viable, config=config)
    for candidate in viable:
        candidate.recommendation_reason = _recommendation_reason(candidate, recommended)

    result = IdentificationResult(
        status=_status(viable, recommended, confirmed=confirmed_policy_id, referred=referred),
        candidates=viable,
        near_misses=near_misses,
        recommended_policy_id=recommended.policy_id if recommended else None,
        ambiguous=bool(contenders(viable, config=config)),
        searched_on=list(searched_on(notice)),
        policies_compared=len(policies),
    )
    return result


def compare(notice: NoticeSignals, policy: PolicyFacts, *, config: FNOLSettings) -> CandidateMatch:
    """How well one policy answers this notice, and why."""
    construction = notice.is_construction() or policy.is_construction()

    results: list[SignalResult] = []
    for definition in SIGNALS:
        if definition.construction_only and not construction:
            continue
        results.append(_COMPARATORS[definition.signal](notice, policy, definition))

    period = _period_outcome(notice, policy)
    score = weighted_score(
        [
            (result.score, result.weight)
            for result in results
            if result.compared and result.score is not None
        ]
    )
    match = CandidateMatch(
        policy_id=policy.policy_id,
        policy_number=policy.policy_number,
        score=round(score, 4),
        confidence=PolicyConfidence.REJECTED,
        signal_results=results,
        display=_display(policy, results),
        period_outcome=period,
        prior_policy_number=(
            policy.prior_policy_number if period is PolicyPeriodOutcome.PRIOR_TERM else None
        ),
    )
    match.confidence = _classify(match, config=config)
    match.warnings = _warnings(notice, policy, match)
    return match


def searched_on(notice: NoticeSignals) -> list[SignalResult]:
    """What the notice gave the engine to work with, signal by signal.

    Rendered as the "what we matched on" panel, and shown even when nothing
    matched — an officer who can see the search was run on a policy number and an
    insured name knows the book is the problem, not the reading.
    """
    rows: list[SignalResult] = []
    for definition in SIGNALS:
        value = _notice_value(notice, definition.signal)
        if value is None:
            rows.append(
                SignalResult(
                    signal=definition.signal,
                    label=definition.label,
                    axis=definition.axis,
                    outcome=SignalOutcome.MISSING,
                    weight=definition.weight,
                    explanation=_NOT_STATED[definition.signal],
                    evidence_field_key=definition.evidence_field_key,
                )
            )
            continue
        rows.append(
            SignalResult(
                signal=definition.signal,
                label=definition.label,
                axis=definition.axis,
                outcome=SignalOutcome.MATCH,
                weight=definition.weight,
                explanation=_READ_FROM.get(definition.signal, "Read from the notice."),
                score=value.confidence,
                notice_value=value.value,
                evidence_field_key=value.field_key or definition.evidence_field_key,
                document_id=value.document_id,
                page_number=value.page_number,
                quote=value.quote,
            )
        )
    return rows


# ---------------------------------------------------------------------------
# Per-signal comparison
# ---------------------------------------------------------------------------


def _uncompared(
    definition: SignalDefinition,
    *,
    policy_value: str | None,
    notice_value: str | None = None,
) -> SignalResult:
    """A signal that could not be compared, and which side was silent.

    `MISSING` when the policy holds the value and the notice does not — a gap in
    the notice, which the officer can go and fill. `NOT_COMPARED` when neither
    side holds it, which is a signal that does not apply to this risk. Neither
    scores; both are listed.
    """
    stated = bool(policy_value)
    return SignalResult(
        signal=definition.signal,
        label=definition.label,
        axis=definition.axis,
        outcome=SignalOutcome.MISSING if stated else SignalOutcome.NOT_COMPARED,
        weight=definition.weight,
        explanation=(
            _NOT_STATED[definition.signal]
            if stated
            else _NEITHER_STATED.get(
                definition.signal, "Neither the notice nor the policy stated it."
            )
        ),
        binary=definition.binary,
        notice_value=notice_value,
        policy_value=policy_value,
        evidence_field_key=definition.evidence_field_key,
    )


def _result(
    definition: SignalDefinition,
    notice_value: SignalValue | None,
    policy_value: str | None,
    *,
    outcome: SignalOutcome,
    score: float,
    explanation: str,
) -> SignalResult:
    return SignalResult(
        signal=definition.signal,
        label=definition.label,
        axis=definition.axis,
        outcome=outcome,
        weight=definition.weight,
        score=score,
        binary=definition.binary,
        explanation=explanation,
        notice_value=notice_value.value if notice_value else None,
        policy_value=policy_value,
        evidence_field_key=(
            (notice_value.field_key if notice_value else None) or definition.evidence_field_key
        ),
        document_id=notice_value.document_id if notice_value else None,
        page_number=notice_value.page_number if notice_value else None,
        quote=notice_value.quote if notice_value else None,
    )


def _compare_policy_number(
    notice: NoticeSignals, policy: PolicyFacts, definition: SignalDefinition
) -> SignalResult:
    """The one identifier that is supposed to be unique, compared four ways.

    Exact after punctuation is dropped; then OCR-folded, because a scanned claim
    form turns `POL-2026` into `P0L-2O26`; then containment, because a broker
    quoting `0041` of `POL-2026-0041` is quoting this policy; then edit distance,
    which catches a dropped or transposed digit. Each tier scores lower than the
    one above and each says which tier it is, so an officer knows whether they are
    looking at a match or at a guess.
    """
    stated = notice.policy_number
    if stated is None:
        return _uncompared(definition, policy_value=policy.policy_number)

    left = normalise_reference(stated.value)
    right = normalise_reference(policy.policy_number)
    if not left or not right:
        return _uncompared(definition, policy_value=policy.policy_number, notice_value=stated.value)

    def answer(outcome: SignalOutcome, score: float, explanation: str) -> SignalResult:
        return _result(
            definition,
            stated,
            policy.policy_number,
            outcome=outcome,
            score=score,
            explanation=explanation,
        )

    if left == right:
        return answer(SignalOutcome.MATCH, 1.0, "The number quoted on the notice matches exactly.")

    if _fold_confusables(left) == _fold_confusables(right):
        return answer(
            SignalOutcome.MATCH,
            0.9,
            "The numbers match once characters an OCR pass confuses — 0/O, 1/I, 5/S, 8/B — "
            "are folded together. Worth a second look at the source.",
        )

    if len(left) >= 5 and len(right) >= 5 and (left in right or right in left):
        return answer(
            SignalOutcome.PARTIAL,
            0.75,
            "One number contains the other. Often a broker quoting a policy without "
            "its prefix, or with a section suffix added.",
        )

    distance = _edit_distance(left, right, ceiling=3)
    if distance <= 2 and min(len(left), len(right)) >= 6:
        return answer(
            SignalOutcome.PARTIAL,
            0.55,
            f"The numbers differ by {distance} character"
            f"{'' if distance == 1 else 's'} — consistent with a transcription error, "
            "not with a different policy.",
        )

    # Before calling it a mismatch: is the quoted reference one of this policy's
    # *other* references? Brokers put their own scheme reference in the field a form
    # labels "policy number", and construction desks put the contract number there.
    # When that is what happened, the fact is scored on the signal it belongs to —
    # reporting it here as well would punish the right policy for the same fact that
    # identified it.
    for label, other in (
        ("broker reference", policy.broker_reference),
        ("contract number", policy.contract_number),
        ("project reference", policy.project_reference),
    ):
        if other and left == normalise_reference(other):
            return SignalResult(
                signal=definition.signal,
                label=definition.label,
                axis=definition.axis,
                outcome=SignalOutcome.NOT_COMPARED,
                weight=definition.weight,
                explanation=(
                    f"The reference on the notice is this policy's {label} rather than its "
                    f"policy number, and is scored as that. No policy number was quoted."
                ),
                notice_value=stated.value,
                policy_value=policy.policy_number,
                evidence_field_key=stated.field_key or definition.evidence_field_key,
                document_id=stated.document_id,
                page_number=stated.page_number,
                quote=stated.quote,
            )

    return answer(
        SignalOutcome.MISMATCH,
        0.0,
        f"The notice quotes {stated.value}, which is a different policy number.",
    )


def _compare_broker_reference(
    notice: NoticeSignals, policy: PolicyFacts, definition: SignalDefinition
) -> SignalResult:
    """The broker's own scheme reference — quoted far more reliably than ours.

    Two candidate values from the notice, and the second is the point: brokers
    routinely put their own reference in the field a form labels "policy number",
    because that is the number their system prints. Recognising it there turns a
    policy-number mismatch into a broker-reference match, which is the correct
    reading of the same fact.
    """
    if not policy.broker_reference:
        return _uncompared(
            definition,
            policy_value=None,
            notice_value=notice.broker_reference.value if notice.broker_reference else None,
        )

    right = normalise_reference(policy.broker_reference)
    stated = notice.broker_reference
    if stated is not None and right:
        left = normalise_reference(stated.value)
        if left and left == right:
            return _result(
                definition,
                stated,
                policy.broker_reference,
                outcome=SignalOutcome.MATCH,
                score=1.0,
                explanation="The broker's own reference on the notice matches this policy.",
            )
        if left and len(left) >= 4 and (left in right or right in left):
            return _result(
                definition,
                stated,
                policy.broker_reference,
                outcome=SignalOutcome.PARTIAL,
                score=0.7,
                explanation="The broker's reference partly matches this policy's.",
            )
        if left:
            return _result(
                definition,
                stated,
                policy.broker_reference,
                outcome=SignalOutcome.MISMATCH,
                score=0.0,
                explanation=(
                    f"The notice quotes broker reference {stated.value}; this policy is "
                    f"{policy.broker_reference}."
                ),
            )

    quoted = notice.policy_number
    if quoted is not None and right and normalise_reference(quoted.value) == right:
        return _result(
            definition,
            quoted,
            policy.broker_reference,
            outcome=SignalOutcome.MATCH,
            score=1.0,
            explanation=(
                "The reference quoted as the policy number is this policy's broker "
                "reference — the number the broker's own system prints."
            ),
        )

    return _uncompared(definition, policy_value=policy.broker_reference)


def _compare_contract_number(
    notice: NoticeSignals, policy: PolicyFacts, definition: SignalDefinition
) -> SignalResult:
    """On a construction risk the contract is the risk, and its number identifies it."""
    policy_value = policy.contract_number or policy.project_reference
    stated = notice.contract_number

    # The same substitution the broker reference has to allow for, and for the same
    # reason: a construction desk fills in the field a form labels "policy number"
    # with the contract reference, because the contract is what their paperwork is
    # filed under. Recognising it here is what turns a policy-number mismatch into a
    # contract match, which is the correct reading of one fact rather than two.
    if (
        stated is None
        and policy_value
        and notice.policy_number is not None
        and normalise_reference(notice.policy_number.value) == normalise_reference(policy_value)
    ):
        return _result(
            definition,
            notice.policy_number,
            policy_value,
            outcome=SignalOutcome.MATCH,
            score=1.0,
            explanation=(
                "The reference quoted as the policy number is this policy's contract "
                "number — the reference the works are filed under."
            ),
        )

    if stated is None or not policy_value:
        return _uncompared(
            definition,
            policy_value=policy_value,
            notice_value=stated.value if stated else None,
        )

    left, right = normalise_reference(stated.value), normalise_reference(policy_value)
    if left and left == right:
        return _result(
            definition,
            stated,
            policy_value,
            outcome=SignalOutcome.MATCH,
            score=1.0,
            explanation="The contract number on the notice matches this policy's.",
        )
    if left and right and len(left) >= 4 and (left in right or right in left):
        return _result(
            definition,
            stated,
            policy_value,
            outcome=SignalOutcome.PARTIAL,
            score=0.7,
            explanation=(
                "The contract references partly agree. Employer's contract reference, "
                "contractor's job number and the insurer's project reference are three "
                "different numbers, all called the contract number."
            ),
        )
    return _result(
        definition,
        stated,
        policy_value,
        outcome=SignalOutcome.MISMATCH,
        score=0.0,
        explanation=(
            f"The notice quotes contract {stated.value}; this policy covers {policy_value}."
        ),
    )


def _compare_insured_name(
    notice: NoticeSignals, policy: PolicyFacts, definition: SignalDefinition
) -> SignalResult:
    """The insured named on the notice, against every party this policy insures.

    Compared against the joint-names list rather than the named insured alone, and
    the reason says which party matched. A subcontractor reporting a loss on a CAR
    policy is an insured for their interest and is not the first name on the
    declarations page; scoring them zero fails a legitimate claim.
    """
    stated = notice.insured_name
    if stated is None:
        return _uncompared(definition, policy_value=policy.insured_name)

    best_role, best_name, best = "the named insured", policy.insured_name, 0.0
    for role, name in policy.joint_names():
        similarity = entity_similarity(stated.value, name)
        if similarity > best:
            best_role, best_name, best = role, name, similarity

    named = best_role == "the named insured"
    if best >= 0.92:
        return _result(
            definition,
            stated,
            best_name,
            outcome=SignalOutcome.MATCH,
            score=best,
            explanation=(
                f"The insured named on the notice matches {best_role}."
                if not named
                else "The insured named on the notice matches this policy."
            ),
        )
    if best >= 0.6:
        return _result(
            definition,
            stated,
            best_name,
            outcome=SignalOutcome.PARTIAL,
            score=best,
            explanation=(
                f"The name on the notice resembles {best_role} ({best:.2f}) without matching "
                "it. Legal suffixes and trading names have already been allowed for, so a "
                "gap here often means a sister company."
            ),
        )
    return _result(
        definition,
        stated,
        policy.insured_name,
        outcome=SignalOutcome.MISMATCH,
        score=best,
        explanation=(
            f"The notice names {stated.value}; this policy insures {policy.insured_name}."
        ),
    )


def _compare_insured_organisation(
    notice: NoticeSignals, policy: PolicyFacts, definition: SignalDefinition
) -> SignalResult:
    stated = notice.insured_organisation
    if stated is None or not policy.insured_organisation:
        return _uncompared(
            definition,
            policy_value=policy.insured_organisation,
            notice_value=stated.value if stated else None,
        )

    # Compared against every party the policy insures, not only the entity on the
    # declarations page — for the same reason the name signal is. A joint-names
    # policy legitimately insures the employer and the main contractor, and a
    # notice from one of them is not a conflict about who the client is. Reading it
    # as one would cap the confidence of a candidate whose contract number matched
    # exactly, which is the wrong answer on the risk where the contract number is
    # the best identifier there is.
    best_role, best_name, best = (
        "this policy",
        policy.insured_organisation,
        entity_similarity(stated.value, policy.insured_organisation),
    )
    for role, name in policy.joint_names():
        similarity = entity_similarity(stated.value, name)
        if similarity > best:
            best_role, best_name, best = role, name, similarity

    named = best_role in ("this policy", "the named insured", "the insured organisation")
    if best >= 0.92:
        outcome, explanation = (
            SignalOutcome.MATCH,
            "The legal entity named on the notice matches this policy."
            if named
            else f"The entity named on the notice is {best_role} on this policy.",
        )
    elif best >= 0.6:
        outcome, explanation = (
            SignalOutcome.PARTIAL,
            f"The entity named resembles {best_name} ({best:.2f}) without matching it.",
        )
    else:
        outcome, explanation = (
            SignalOutcome.MISMATCH,
            f"The notice names {stated.value}; this policy is written for "
            f"{policy.insured_organisation}.",
        )
    return _result(
        definition,
        stated,
        best_name,
        outcome=outcome,
        score=best,
        explanation=explanation,
    )


def _compare_insured_domain(
    notice: NoticeSignals, policy: PolicyFacts, definition: SignalDefinition
) -> SignalResult:
    """The insured's own email domain — *not* the sender's.

    The sender is usually the broker. This signal compares a domain taken from the
    insured party on the notice against the domain held on the policy, and it is a
    separate signal from `broker_domain` for exactly that reason.
    """
    stated = notice.insured_domain
    held = policy.insured_domain or _domain_of(policy.insured_email)
    if stated is None or not held:
        return _uncompared(
            definition,
            policy_value=held,
            notice_value=stated.value if stated else None,
        )

    left = _domain_of(stated.value) or normalise(stated.value)
    if left in GENERIC_EMAIL_DOMAINS:
        return SignalResult(
            signal=definition.signal,
            label=definition.label,
            axis=definition.axis,
            outcome=SignalOutcome.NOT_COMPARED,
            weight=definition.weight,
            binary=definition.binary,
            explanation=(
                f"The address on the notice is at {left}, a mail provider rather than an "
                "organisation, so it identifies nobody."
            ),
            notice_value=stated.value,
            policy_value=held,
        )

    if left == normalise(held):
        return _result(
            definition,
            stated,
            held,
            outcome=SignalOutcome.MATCH,
            score=1.0,
            explanation=f"The insured's address is at {held}, the domain held on this policy.",
        )
    return _result(
        definition,
        stated,
        held,
        outcome=SignalOutcome.MISMATCH,
        score=0.0,
        explanation=f"The insured's address is at {left}; this policy holds {held}.",
    )


def _compare_broker_domain(
    notice: NoticeSignals, policy: PolicyFacts, definition: SignalDefinition
) -> SignalResult:
    """The sender's domain against the broker on the policy.

    Free — it is in the envelope, needing no extraction at all — and it is the
    right target for it. Most commercial notices arrive from a broking house, so
    the sender's domain is evidence about the broker and comparing it to the
    insured's address is a category error.
    """
    stated = notice.broker_domain
    if stated is None or not policy.broker_domain:
        return _uncompared(
            definition,
            policy_value=policy.broker_domain,
            notice_value=stated.value if stated else None,
        )

    left = _domain_of(stated.value) or normalise(stated.value)
    if left in GENERIC_EMAIL_DOMAINS:
        return SignalResult(
            signal=definition.signal,
            label=definition.label,
            axis=definition.axis,
            outcome=SignalOutcome.NOT_COMPARED,
            weight=definition.weight,
            binary=definition.binary,
            explanation=(
                f"The notice was sent from {left}, a mail provider rather than a broking "
                "house, so the domain identifies nobody."
            ),
            notice_value=stated.value,
            policy_value=policy.broker_domain,
        )

    if left == normalise(policy.broker_domain):
        return _result(
            definition,
            stated,
            policy.broker_domain,
            outcome=SignalOutcome.MATCH,
            score=1.0,
            explanation=f"The notice was sent from {left}, the broker who placed this policy.",
        )
    return _result(
        definition,
        stated,
        policy.broker_domain,
        outcome=SignalOutcome.MISMATCH,
        score=0.0,
        explanation=(
            f"The notice was sent from {left}; this policy was placed through "
            f"{policy.broker_domain}."
        ),
    )


def _compare_broker_name(
    notice: NoticeSignals, policy: PolicyFacts, definition: SignalDefinition
) -> SignalResult:
    stated = notice.broker_name
    if stated is None or not policy.broker_name:
        return _uncompared(
            definition,
            policy_value=policy.broker_name,
            notice_value=stated.value if stated else None,
        )
    similarity = name_similarity(stated.value, policy.broker_name)
    if similarity >= 0.85:
        outcome, explanation = (
            SignalOutcome.MATCH,
            f"{policy.broker_name} placed this policy and is the broker on the notice.",
        )
    elif similarity >= 0.55:
        outcome, explanation = (
            SignalOutcome.PARTIAL,
            f"The broker named resembles {policy.broker_name} ({similarity:.2f}). Trading "
            "names and post-acquisition renames both produce this.",
        )
    else:
        outcome, explanation = (
            SignalOutcome.MISMATCH,
            f"The notice names {stated.value}; this policy was placed by {policy.broker_name}.",
        )
    return _result(
        definition,
        stated,
        policy.broker_name,
        outcome=outcome,
        score=similarity,
        explanation=explanation,
    )


def _compare_project_name(
    notice: NoticeSignals, policy: PolicyFacts, definition: SignalDefinition
) -> SignalResult:
    stated = notice.project_name
    held = policy.project_name
    if stated is None or not held:
        return _uncompared(
            definition, policy_value=held, notice_value=stated.value if stated else None
        )
    similarity = max(
        name_similarity(stated.value, held),
        name_similarity(stated.value, policy.project_reference or ""),
    )
    if similarity >= 0.85:
        outcome, explanation = (
            SignalOutcome.MATCH,
            f"The notice concerns {held}, the project this policy covers.",
        )
    elif similarity >= 0.5:
        outcome, explanation = (
            SignalOutcome.PARTIAL,
            f"The project named resembles {held} ({similarity:.2f}). Phase numbering and "
            "site street names both produce this.",
        )
    else:
        outcome, explanation = (
            SignalOutcome.MISMATCH,
            f"The notice names project {stated.value}; this policy covers {held}.",
        )
    return _result(
        definition, stated, held, outcome=outcome, score=similarity, explanation=explanation
    )


def _compare_risk_location(
    notice: NoticeSignals, policy: PolicyFacts, definition: SignalDefinition
) -> SignalResult:
    """The loss address against the *schedule* of insured locations.

    For commercial property this is the signal that matters, and comparing against
    a head-office address alone is the defect it fixes: a loss at warehouse seven
    of twelve scores nothing against the primary location and everything against
    the schedule. The postcode is compared first and separately, because in the UK
    it is the single most discriminating token in an address — and the reason names
    the location that matched, since the deductible and the sum insured attach to
    the location rather than to the policy.
    """
    stated = notice.risk_location or notice.loss_location
    postcode = notice.loss_postcode or (
        SignalValue(_postcode_of(stated.value) or "", stated.field_key) if stated else None
    )
    schedule = _schedule(policy)
    if not schedule:
        return _uncompared(
            definition,
            policy_value=None,
            notice_value=stated.value if stated else None,
        )
    if stated is None and (postcode is None or not postcode.value):
        return _uncompared(definition, policy_value=schedule[0].address)

    best: tuple[float, SignalOutcome, str, PolicyLocationFacts] | None = None
    stated_postcode = _normalise_postcode(postcode.value) if postcode and postcode.value else None

    for entry in schedule:
        entry_postcode = _normalise_postcode(entry.postcode or _postcode_of(entry.address) or "")
        candidates: list[tuple[float, SignalOutcome, str]] = []

        if stated_postcode and entry_postcode:
            if stated_postcode == entry_postcode:
                candidates.append(
                    (
                        1.0,
                        SignalOutcome.MATCH,
                        f"The loss postcode matches {entry.label()} exactly.",
                    )
                )
            elif stated_postcode.split(" ")[0] == entry_postcode.split(" ")[0]:
                candidates.append(
                    (
                        0.85,
                        SignalOutcome.MATCH,
                        f"The loss falls in the same postcode district as {entry.label()}.",
                    )
                )

        if stated is not None:
            similarity = location_similarity(stated.value, entry.address)
            if similarity >= 0.75:
                candidates.append(
                    (
                        similarity,
                        SignalOutcome.MATCH,
                        f"The loss address matches the insured location {entry.label()}.",
                    )
                )
            elif similarity >= 0.4:
                candidates.append(
                    (
                        similarity,
                        SignalOutcome.PARTIAL,
                        f"The loss address partly matches {entry.label()} ({similarity:.2f}).",
                    )
                )

        for score, outcome, explanation in candidates:
            if best is None or score > best[0]:
                best = (score, outcome, explanation, entry)

    if best is None:
        return _result(
            definition,
            stated,
            schedule[0].address if len(schedule) == 1 else f"{len(schedule)} scheduled locations",
            outcome=SignalOutcome.MISMATCH,
            score=0.0,
            explanation=(
                "The loss location does not match any location insured under this policy."
                if len(schedule) > 1
                else f"The loss location does not match {schedule[0].address}."
            ),
        )

    score, outcome, explanation, entry = best
    return _result(
        definition,
        stated,
        entry.address,
        outcome=outcome,
        score=score,
        explanation=explanation,
    )


def _compare_policy_period(
    notice: NoticeSignals, policy: PolicyFacts, definition: SignalDefinition
) -> SignalResult:
    """Whether the loss falls in cover — in five answers, not two.

    A policy that fails the date check is a candidate to *show*, not one to hide:
    "this is your policy, but the loss is outside the period" is a coverage
    conversation and not a matching failure. The maintenance tier is what makes
    construction work at all, and the prior-term tier is what makes a
    late-notified loss point at last year's policy instead of failing silently.
    """
    outcome = _period_outcome(notice, policy)
    stated = notice.date_of_loss
    period = policy.period_label()

    if outcome is PolicyPeriodOutcome.UNKNOWN:
        return _uncompared(definition, policy_value=period)

    if outcome is PolicyPeriodOutcome.IN_FORCE:
        return _result(
            definition,
            stated,
            period,
            outcome=SignalOutcome.MATCH,
            score=1.0,
            explanation=f"The date of loss falls inside the policy period, {period}.",
        )
    if outcome is PolicyPeriodOutcome.IN_MAINTENANCE_PERIOD:
        expiry = policy.maintenance_expiry()
        return _result(
            definition,
            stated,
            period,
            outcome=SignalOutcome.MATCH,
            score=1.0,
            explanation=(
                "The loss falls after practical completion but inside the defects "
                f"liability period, which runs to {expiry:%d %b %Y}."
                if expiry
                else "The loss falls inside the defects liability period."
            ),
        )
    if outcome is PolicyPeriodOutcome.PRIOR_TERM:
        return _result(
            definition,
            stated,
            period,
            outcome=SignalOutcome.PARTIAL,
            score=0.3,
            explanation=(
                f"The loss falls before this term began but inside the preceding policy "
                f"{policy.prior_policy_number or 'for the same insured'}. Losses are often "
                "discovered long after they happen — check the prior year."
            ),
        )
    return _result(
        definition,
        stated,
        period,
        outcome=SignalOutcome.MISMATCH,
        score=0.0,
        explanation=f"The policy ran {period}; the loss falls outside it.",
    )


#: Product families, and the words a notice or a schedule names each one with.
#:
#: A line of business is a *class* of business — `liability`, `engineering` — and one
#: insured routinely holds several policies inside one line. The product is what tells
#: them apart, and until now it was carried on `PolicyFacts.policy_type` for display
#: only and compared by nothing.
#:
#: The case that needs it: Beacon Mechanical Services holds `GL-8804-27153`
#: (Commercial General Liability) and `IM-7741-15530` (Contractors Equipment and
#: Installation Floater). Same insured name, same address, same broker; they differ by
#: broker reference and by product. With no policy number stated, a notice for either
#: scored almost identically on both, and the only signal that separated them at all
#: was `line_of_business` — the lightest weight in the set, at 0.6.
POLICY_TYPE_FAMILIES: dict[str, tuple[str, ...]] = {
    "commercial_property": (
        "commercial property",
        "commercial buildings",
        "property owners",
        "buildings and contents",
        "material damage",
        "property",
    ),
    "business_owners": ("businessowners", "business owners", "bop"),
    "builders_risk": (
        "builders risk",
        "builder's risk",
        "course of construction",
        "contract works",
        "contractors all risks",
        "erection all risks",
        "car policy",
    ),
    "general_liability": (
        "commercial general liability",
        "general liability",
        "contractors gl",
        "contractors general liability",
        "public liability",
        "products liability",
        "cgl",
    ),
    "equipment_floater": (
        "contractors equipment",
        "installation floater",
        "equipment floater",
        "inland marine",
        "plant and equipment",
        "tools and equipment",
    ),
    "commercial_auto": (
        "commercial auto",
        "business auto",
        "motor fleet",
        "auto liability",
        "commercial motor",
        "fleet",
    ),
    "workers_compensation": (
        "workers compensation",
        "workers' compensation",
        "workers comp",
        "employers liability",
    ),
    "umbrella": ("umbrella", "excess liability", "excess casualty"),
    "cyber": ("cyber", "data breach", "technology risks"),
    "professional": (
        "professional indemnity",
        "professional liability",
        "errors and omissions",
        "e&o",
        "directors and officers",
        "d&o",
    ),
    "marine": ("marine cargo", "marine hull", "goods in transit", "cargo", "hull"),
    "engineering": ("machinery breakdown", "electronic equipment", "boiler and machinery"),
    "environmental": ("contractors pollution", "environmental impairment", "pollution legal"),
}

#: Families whose products overlap enough that naming one and holding the other is
#: not a contradiction. A contractors-equipment policy and an inland-marine policy
#: are frequently the same paper under two names, and a notice that says "plant" has
#: not ruled out either.
_COMPATIBLE_FAMILIES: tuple[frozenset[str], ...] = (
    frozenset({"equipment_floater", "engineering"}),
    frozenset({"equipment_floater", "marine"}),
    frozenset({"general_liability", "umbrella"}),
    frozenset({"commercial_property", "business_owners"}),
    frozenset({"builders_risk", "engineering"}),
)


def policy_type_family(value: str | None) -> str | None:
    """The product family a stated policy type belongs to, or `None`.

    Longest phrase wins, which is the whole reason this is a table and not a set of
    `in` checks: "commercial general liability" has to beat "general liability", and
    "general liability" has to beat "liability" — a wording that names the broader
    term first classifies half the book as the wrong product.
    """
    text = normalise(value)
    if not text:
        return None
    best: tuple[int, str] | None = None
    for family, terms in POLICY_TYPE_FAMILIES.items():
        for term in terms:
            if term in text and (best is None or len(term) > best[0]):
                best = (len(term), family)
    return best[1] if best else None


def _families_agree(left: str, right: str) -> bool:
    if left == right:
        return True
    pair = frozenset({left, right})
    return any(pair <= group for group in _COMPATIBLE_FAMILIES)


def _compare_policy_type(
    notice: NoticeSignals, policy: PolicyFacts, definition: SignalDefinition
) -> SignalResult:
    """The product, as distinct from the line of business.

    Scored at 1.4 — heavier than `line_of_business` at 0.6 and lighter than
    `insured_name` at 2.0. That ordering is the claim being made: the product is real
    evidence about which of an insured's policies this is, and it is never evidence
    about *whose* policy it is.

    A mismatch here does not reject a candidate, and deliberately: `policy_type` is
    not a primary identifier, an insured commonly holds the product the notice did
    not name, and a broker writing "GL" on an equipment claim is a mistake to warn
    about rather than a reason to hide the right policy.
    """
    stated = notice.policy_type
    if stated is None or not policy.policy_type:
        return _uncompared(
            definition,
            policy_value=policy.policy_type or None,
            notice_value=stated.value if stated else None,
        )

    notice_family = policy_type_family(stated.value)
    policy_family = policy_type_family(policy.policy_type)

    if notice_family and policy_family:
        if _families_agree(notice_family, policy_family):
            return _result(
                definition,
                stated,
                policy.policy_type,
                outcome=SignalOutcome.MATCH,
                score=1.0,
                explanation=(
                    f"The notice names a {_humanise(notice_family)} product and this is "
                    f"the insured's {policy.policy_type} policy."
                ),
            )
        return _result(
            definition,
            stated,
            policy.policy_type,
            outcome=SignalOutcome.MISMATCH,
            score=0.0,
            explanation=(
                f"The notice names a {_humanise(notice_family)} product; this is a "
                f"{policy.policy_type} policy. One insured commonly holds both, so this "
                "separates two of their policies rather than ruling this one out."
            ),
        )

    # Neither side classified, or only one did. Fall back to comparing the words,
    # which is worth something — two schedules printing the same product name agree
    # about the product — and is reported as partial rather than as a match, because
    # an unrecognised product name is exactly where this is least reliable.
    similarity = entity_similarity(stated.value, policy.policy_type)
    if similarity >= 0.6:
        return _result(
            definition,
            stated,
            policy.policy_type,
            outcome=SignalOutcome.PARTIAL,
            score=round(similarity, 4),
            explanation=(
                f"“{stated.value}” and “{policy.policy_type}” read as the same product, "
                "though neither is a product name this recognises."
            ),
        )
    return _result(
        definition,
        stated,
        policy.policy_type,
        outcome=SignalOutcome.MISMATCH,
        score=0.0,
        explanation=(f"The notice names “{stated.value}”; this policy is “{policy.policy_type}”."),
    )


def _compare_line_of_business(
    notice: NoticeSignals, policy: PolicyFacts, definition: SignalDefinition
) -> SignalResult:
    stated = notice.line_of_business
    if stated is None or not policy.line_of_business:
        return _uncompared(
            definition,
            policy_value=policy.line_of_business or None,
            notice_value=stated.value if stated else None,
        )
    if _lines_agree(stated.value, policy.line_of_business):
        return _result(
            definition,
            stated,
            policy.line_of_business,
            outcome=SignalOutcome.MATCH,
            score=1.0,
            explanation=("This policy covers the line of business the notice was classified as."),
        )
    return _result(
        definition,
        stated,
        policy.line_of_business,
        outcome=SignalOutcome.MISMATCH,
        score=0.0,
        explanation=(
            f"The notice reads as a {_humanise(stated.value)} loss; this is a "
            f"{_humanise(policy.line_of_business)} policy. One insured commonly holds "
            "several, so this narrows rather than decides."
        ),
    )


_COMPARATORS: dict[str, Any] = {
    "policy_number": _compare_policy_number,
    "broker_reference": _compare_broker_reference,
    "contract_number": _compare_contract_number,
    "insured_name": _compare_insured_name,
    "project_name": _compare_project_name,
    "policy_period": _compare_policy_period,
    "risk_location": _compare_risk_location,
    "insured_organisation": _compare_insured_organisation,
    "insured_domain": _compare_insured_domain,
    "broker_domain": _compare_broker_domain,
    "broker_name": _compare_broker_name,
    "policy_type": _compare_policy_type,
    "line_of_business": _compare_line_of_business,
}


# ---------------------------------------------------------------------------
# Classification
# ---------------------------------------------------------------------------


def _classify(match: CandidateMatch, *, config: FNOLSettings) -> PolicyConfidence:
    """The confidence band, as a ladder of stated rules rather than a threshold.

    Deliberately not a set of cut-offs on the weighted mean. The mean is a
    *summary*; the band is a *judgement*, and the two disagree in exactly the case
    that matters. An exact policy number carried alongside a mismatched insured
    name computes to something like 0.66 and would demote to a middling band by
    arithmetic — which is arguably the right answer, but arriving at it by
    accident is not the same as deciding it. Here it is decided: an identity
    conflict caps the band, whatever the number says.
    """
    identity_conflict = _identity_conflict(match)
    primary_agreement = any(
        match.outcome_of(signal) is SignalOutcome.MATCH for signal in PRIMARY_IDENTIFIERS
    )
    primary_conflict = any(
        match.outcome_of(signal) is SignalOutcome.MISMATCH for signal in PRIMARY_IDENTIFIERS
    )
    identity_agreement = any(
        match.outcome_of(signal) in (SignalOutcome.MATCH, SignalOutcome.PARTIAL)
        for signal in IDENTITY_SIGNALS
    )
    outside_period = match.period_outcome is PolicyPeriodOutcome.OUTSIDE_PERIOD

    # A candidate that agrees on nothing identifying is not a candidate. Without
    # this every property policy on the book qualifies on dates and line of
    # business alone, and the list stops meaning anything.
    if not primary_agreement and not identity_agreement and not _location_identifies(match):
        return PolicyConfidence.REJECTED

    # A unique reference that disagrees, with nothing about the client agreeing,
    # is the strongest available statement that this is the wrong policy.
    if primary_conflict and not identity_agreement:
        return PolicyConfidence.REJECTED

    exact_number = (
        match.outcome_of("policy_number") is SignalOutcome.MATCH
        and match.score_of("policy_number") >= 1.0
    )
    if exact_number and not identity_conflict and not outside_period:
        return PolicyConfidence.EXACT

    agreeing_axes = {
        result.axis
        for result in match.signal_results
        if result.agreed and result.axis is not SignalAxis.COVER
    }
    strong_agreement = primary_agreement or len(agreeing_axes) >= 2
    if (
        strong_agreement
        and identity_agreement
        and not identity_conflict
        and not outside_period
        and match.score >= config.policy_identification_strong_threshold
    ):
        return PolicyConfidence.STRONG

    if match.score >= config.policy_identification_possible_threshold:
        return PolicyConfidence.POSSIBLE
    if match.score >= config.policy_identification_weak_threshold:
        return PolicyConfidence.WEAK
    return PolicyConfidence.REJECTED


def _identity_conflict(match: CandidateMatch) -> bool:
    """The notice and the policy disagree about *who* is insured.

    The reason an exact policy number is not automatically the answer. A number
    that matches while the name does not is the shape of a broker quoting the
    wrong reference off a covering schedule, and it is precisely the case where a
    machine binding the policy would attach a claim to the wrong client.
    """
    return any(
        match.outcome_of(signal) is SignalOutcome.MISMATCH
        for signal in ("insured_name", "insured_organisation")
    )


def _location_identifies(match: CandidateMatch) -> bool:
    """A location match strong enough to stand as identification on its own.

    Only an exact postcode agreement counts. A fuzzy street-name overlap puts two
    unrelated Leeds businesses in the same candidate list, which is how a
    location signal stops being useful.
    """
    result = match.result("risk_location")
    return (
        result is not None
        and result.outcome is SignalOutcome.MATCH
        and (result.score or 0.0) >= 1.0
    )


def _ranking_key(candidate: CandidateMatch) -> tuple[float, float, int]:
    """Best first, and ties broken by how much evidence the score rests on.

    Two candidates on 0.91 are not equally good if one was computed from six
    signals and the other from two. Sorting on the evidence count after the score
    is the cheapest honest tie-break available.
    """
    band = {
        PolicyConfidence.EXACT: 4.0,
        PolicyConfidence.STRONG: 3.0,
        PolicyConfidence.POSSIBLE: 2.0,
        PolicyConfidence.WEAK: 1.0,
        PolicyConfidence.REJECTED: 0.0,
    }[candidate.confidence]
    return (band, candidate.score, len(candidate.compared_signals))


def contenders(candidates: list[CandidateMatch], *, config: FNOLSettings) -> list[CandidateMatch]:
    """Candidates close enough to the top one that choosing between them is a person's job.

    Factored out of `_recommended` because the service needs the same answer for a
    different question. `_recommended` asks "may the engine put one forward"; the
    service asks "may anything be written to `case.policy_id`", and the two were
    being answered by different rules — the engine declined to recommend and the
    service then wrote `result.best.policy_id` anyway.
    """
    if not candidates:
        return []
    best = candidates[0]
    return [
        candidate
        for candidate in candidates[1:]
        if candidate.confidence in (PolicyConfidence.EXACT, PolicyConfidence.STRONG)
        or candidate.score >= best.score - config.policy_identification_ambiguity_margin
    ]


def _recommended(
    candidates: list[CandidateMatch], *, config: FNOLSettings
) -> CandidateMatch | None:
    """The candidate the engine would put forward, or none at all.

    Ambiguity is a first-class outcome. Two candidates that both reach a strong
    band means the engine recommends *neither*: the officer has a choice to make
    and pre-selecting one of them would hide it. Nothing about that is a failure
    of the matcher — telling a person which two policies to look at is the job.
    """
    if not candidates:
        return None
    best = candidates[0]
    if best.confidence not in (PolicyConfidence.EXACT, PolicyConfidence.STRONG):
        return None
    if contenders(candidates, config=config):
        return None
    return best


def _recommendation_reason(candidate: CandidateMatch, recommended: CandidateMatch | None) -> str:
    if recommended is not None and recommended.policy_id == candidate.policy_id:
        agreements = [result.label.lower() for result in candidate.agreements]
        joined = ", ".join(agreements[:4]) if agreements else "partial similarity"
        return (
            f"Put forward because {joined} agree, across "
            f"{len(candidate.compared_signals)} of {len(candidate.signal_results)} signals."
        )
    if candidate.confidence in (PolicyConfidence.EXACT, PolicyConfidence.STRONG):
        return (
            "A strong match, but not put forward on its own — another candidate scores "
            "close enough that the choice belongs to an officer."
        )
    if candidate.confidence is PolicyConfidence.WEAK:
        return "Below the threshold the engine would act on. Shown in case you recognise it."
    return "Listed for review: some signals agree and the identification is not settled."


def _status(
    candidates: list[CandidateMatch],
    recommended: CandidateMatch | None,
    *,
    confirmed: Any | None,
    referred: bool,
) -> PolicyIdentificationStatus:
    if confirmed is not None:
        return PolicyIdentificationStatus.CONFIRMED
    if referred:
        return PolicyIdentificationStatus.REFERRED
    if not candidates:
        return PolicyIdentificationStatus.NO_MATCH
    if recommended is not None:
        return PolicyIdentificationStatus.CONFIDENT_MATCH
    return PolicyIdentificationStatus.NEEDS_REVIEW


# ---------------------------------------------------------------------------
# Warnings
# ---------------------------------------------------------------------------


def _warnings(
    notice: NoticeSignals, policy: PolicyFacts, match: CandidateMatch
) -> list[CandidateWarning]:
    """Coverage plausibility, kept out of the score.

    None of these lowers a rank. A ransomware loss against a commercial property
    policy may well be the right *policy* and the wrong *section*, and the officer
    needs both facts stated separately rather than blended into one percentage.
    """
    warnings: list[CandidateWarning] = []

    if policy.status and policy.status != "active":
        warnings.append(
            CandidateWarning(
                "policy_not_active",
                f"This policy is recorded as {policy.status}.",
                severity="critical" if policy.status == "cancelled" else "warning",
            )
        )

    if match.period_outcome is PolicyPeriodOutcome.OUTSIDE_PERIOD:
        warnings.append(
            CandidateWarning(
                "outside_policy_period",
                f"The loss falls outside the policy period ({policy.period_label()}).",
                severity="critical",
            )
        )
    elif match.period_outcome is PolicyPeriodOutcome.PRIOR_TERM:
        warnings.append(
            CandidateWarning(
                "prior_term",
                "The loss falls in the preceding policy term"
                + (f" — {policy.prior_policy_number}." if policy.prior_policy_number else "."),
            )
        )

    if match.outcome_of("line_of_business") is SignalOutcome.MISMATCH:
        warnings.append(
            CandidateWarning(
                "line_of_business_mismatch",
                f"This is a {_humanise(policy.line_of_business)} policy and the notice reads "
                f"as a {_humanise(_line_of(notice))} loss.",
            )
        )

    if match.outcome_of("policy_type") is SignalOutcome.MISMATCH:
        warnings.append(
            CandidateWarning(
                "policy_type_mismatch",
                f"The notice names a different product from this policy's "
                f"“{policy.policy_type}”. Check which of the insured's policies the "
                "loss belongs under.",
            )
        )

    if _identity_conflict(match):
        warnings.append(
            CandidateWarning(
                "identity_conflict",
                "The reference agrees but the insured named does not. Check the notice "
                "against the schedule before binding.",
                severity="critical",
            )
        )

    cause = notice.cause_of_loss.value if notice.cause_of_loss else None
    if cause and policy.perils_covered and not _peril_listed(cause, policy):
        warnings.append(
            CandidateWarning(
                "peril_not_listed",
                f"“{cause}” is not among the perils listed on this policy. The wording and "
                "its endorsements are the authority, not this list.",
            )
        )
    if cause and _peril_excluded(cause, policy):
        warnings.append(
            CandidateWarning(
                "peril_excluded",
                f"“{cause}” resembles an exclusion recorded against this policy.",
            )
        )

    if (
        notice.estimated_loss_minor
        and policy.limit_amount_minor
        and notice.estimated_loss_minor > policy.limit_amount_minor
    ):
        warnings.append(
            CandidateWarning(
                "exceeds_limit",
                "The estimate on the notice is above this policy's limit.",
            )
        )

    return warnings


def _peril_listed(cause: str, policy: PolicyFacts) -> bool:
    stated = normalise(cause)
    return any(
        normalise(peril) in stated or stated in normalise(peril) for peril in policy.perils_covered
    )


def _peril_excluded(cause: str, policy: PolicyFacts) -> bool:
    stated = normalise(cause)
    return any(
        normalise(exclusion) in stated or stated in normalise(exclusion)
        for exclusion in policy.exclusions
    )


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------


def _display(policy: PolicyFacts, results: list[SignalResult]) -> CandidateDisplay:
    """The card's face. Names the *matched* location where there is one.

    The deductible and the sum insured attach to a location rather than to the
    policy, so a card showing the head office beside a loss at warehouse seven is
    showing the wrong excess.
    """
    matched = next(
        (result for result in results if result.signal == "risk_location" and result.agreed), None
    )
    entry = None
    if matched and matched.policy_value:
        entry = next(
            (item for item in _schedule(policy) if item.address == matched.policy_value), None
        )

    return CandidateDisplay(
        insured_name=policy.insured_name,
        line_of_business=policy.line_of_business,
        policy_type=policy.policy_type,
        policy_period=policy.period_label(),
        status=policy.status,
        limit_minor=(
            entry.sum_insured_minor
            if entry and entry.sum_insured_minor
            else policy.limit_amount_minor
        ),
        excess_minor=(
            entry.deductible_minor
            if entry and entry.deductible_minor
            else policy.deductible_amount_minor
        ),
        currency=policy.currency,
        location=(entry.address if entry else policy.primary_location or policy.site_address),
        location_label=entry.label() if entry else None,
        broker_name=policy.broker_name,
        project_name=policy.project_name,
        contract_number=policy.contract_number,
    )


def _schedule(policy: PolicyFacts) -> list[PolicyLocationFacts]:
    """Every insured place, schedule first and the denormalised fallbacks after."""
    entries = list(policy.locations)
    known = {normalise(entry.address) for entry in entries}
    for address, label in (
        (policy.primary_location, "the primary insured location"),
        (policy.site_address, "the works site"),
    ):
        if address and normalise(address) not in known:
            entries.append(
                PolicyLocationFacts(
                    location_ref=None,
                    description=label,
                    address=address,
                    postcode=_postcode_of(address),
                    is_primary=label.endswith("location"),
                )
            )
            known.add(normalise(address))
    return entries


def _period_outcome(notice: NoticeSignals, policy: PolicyFacts) -> PolicyPeriodOutcome:
    day = notice.loss_day
    if day is None:
        return PolicyPeriodOutcome.UNKNOWN
    if policy.effective_date <= day <= policy.expiry_date:
        return PolicyPeriodOutcome.IN_FORCE

    maintenance_expiry = policy.maintenance_expiry()
    if maintenance_expiry is not None and policy.expiry_date < day <= maintenance_expiry:
        return PolicyPeriodOutcome.IN_MAINTENANCE_PERIOD

    if policy.prior_term is not None:
        start, end = policy.prior_term
        if start <= day <= end:
            return PolicyPeriodOutcome.PRIOR_TERM

    return PolicyPeriodOutcome.OUTSIDE_PERIOD


def _line_of(notice: NoticeSignals) -> str:
    return notice.line_of_business.value if notice.line_of_business else "unknown"


def entity_similarity(left: str | None, right: str | None) -> float:
    """How alike two *company identities* are — stricter than `name_similarity`.

    `name_similarity` is built for finding a name inside a body of text, so it takes
    the kinder of a containment and a character view. That is right there and wrong
    here: it scores "Northline Logistics" against "Northline Logistics (Scotland)
    Limited" as a perfect match, because every token of the shorter name appears in
    the longer one. But the extra tokens are exactly what distinguishes one company
    in a group from another, and a group is the classic way commercial policy
    matching goes wrong — two real entities, two real policies, and a notice that
    names neither precisely.

    So a strict subset is capped short of a match: similar enough to rank, not
    similar enough to bind, which puts the choice in front of the officer where it
    belongs. Legal suffixes are stripped first, so `Ltd` against `Limited` is still
    one name.
    """
    left_norm, right_norm = normalise_organisation(left), normalise_organisation(right)
    if not left_norm or not right_norm:
        return 0.0
    if left_norm == right_norm:
        return 1.0

    left_tokens = tokens(left_norm, drop_stopwords=False)
    right_tokens = tokens(right_norm, drop_stopwords=False)
    if not left_tokens or not right_tokens:
        return 0.0

    shared = left_tokens & right_tokens
    extra = (left_tokens | right_tokens) - shared
    if not shared:
        return sequence_ratio(left_norm, right_norm)

    # A name wholly contained in the other: distinctive, but the difference is the
    # distinguishing part. Each unstated token costs a little, and the result can
    # never reach the match threshold on its own.
    if left_tokens <= right_tokens or right_tokens <= left_tokens:
        return max(0.55, min(_SUBSET_CEILING, _SUBSET_CEILING - 0.05 * (len(extra) - 1)))

    overlap = len(shared) / len(left_tokens | right_tokens)
    return max(overlap, sequence_ratio(left_norm, right_norm) * 0.9)


#: The most a name that is a strict subset of another may score. Below the 0.92 a
#: match needs, deliberately: it is a strong partial and never a confirmation.
_SUBSET_CEILING = 0.85


def _lines_agree(left: str, right: str) -> bool:
    if left == right:
        return True
    return any({left, right} <= group for group in _COMPATIBLE_LINES)


def _notice_value(notice: NoticeSignals, signal: str) -> SignalValue | None:
    if signal == "risk_location":
        return notice.risk_location or notice.loss_location
    if signal == "policy_period":
        return notice.date_of_loss
    return notice.get(signal)


def _fold_confusables(value: str) -> str:
    return "".join(_CONFUSABLES.get(char, char) for char in value)


def _edit_distance(left: str, right: str, *, ceiling: int) -> int:
    """Levenshtein, abandoned once it passes `ceiling`.

    Bounded because the only question asked of it is "is this within two
    characters", and computing an exact distance of 19 between two unrelated
    references is work nobody reads.
    """
    if abs(len(left) - len(right)) > ceiling:
        return ceiling + 1

    previous = list(range(len(right) + 1))
    for index, char_left in enumerate(left, start=1):
        current = [index]
        for jndex, char_right in enumerate(right, start=1):
            current.append(
                min(
                    previous[jndex] + 1,
                    current[jndex - 1] + 1,
                    previous[jndex - 1] + (0 if char_left == char_right else 1),
                )
            )
        if min(current) > ceiling:
            return ceiling + 1
        previous = current
    return previous[-1]


def _postcode_of(value: str | None) -> str | None:
    """The postcode inside an address, on either book. `None` when there is none."""
    if not value:
        return None
    found = _POSTCODE_RE.search(value)
    if found:
        return f"{found.group(1).upper()} {found.group(2).upper()}"
    zipped = _ZIP_RE.search(value.upper())
    if zipped:
        return zipped.group(1)
    return None


def _normalise_postcode(value: str) -> str:
    """One comparable form, whichever country the address is in.

    A UK postcode splits into its outward and inward halves so that the district
    comparison below has something to compare. A US ZIP does not split: it is five
    digits, and cutting three off the end would leave `21` as the "district" —
    which is most of Maryland, Delaware and Pennsylvania, and would score a
    0.85 location match between two policies four hours' drive apart. The +4
    add-on is dropped for the same reason it is dropped on the way in.
    """
    compact = re.sub(r"[^A-Z0-9]", "", value.upper())
    if compact.isdigit():
        # A US ZIP, or a ZIP+4 that reduces to one. Anything else numeric is not a
        # postcode this function was given, and returning it unchanged keeps it
        # comparable with itself and equal to nothing else.
        return compact[:5] if len(compact) in (5, 9) else compact
    if len(compact) < 5:
        return compact
    return f"{compact[:-3]} {compact[-3:]}"


def _domain_of(value: str | None) -> str | None:
    if not value:
        return None
    text = normalise(value)
    if "@" in text:
        text = text.rsplit("@", 1)[-1]
    text = text.strip().strip(">").strip(".")
    return text or None


def _humanise(value: str | None) -> str:
    if not value:
        return "unknown"
    return value.replace("_", " ")


#: What "the notice did not say" reads as, per signal. Written out rather than
#: generated, because "no date of loss was read, so the policy period could not be
#: checked" tells an officer what to go and find, and "date of loss: missing" does
#: not.
_NOT_STATED: dict[str, str] = {
    "policy_number": "No policy number was read from the notice or its attachments.",
    "broker_reference": "The broker's own reference was not stated on the notice.",
    "contract_number": "No contract number was read from the notice.",
    "insured_name": "No insured name was read from the notice.",
    "project_name": "No project was named on the notice.",
    "policy_period": "No date of loss was read, so the policy period could not be checked.",
    "risk_location": "No loss location was read from the notice.",
    "insured_organisation": "The insured's legal entity name was not stated on the notice.",
    "insured_domain": "No email address for the insured was read from the notice.",
    "broker_domain": "The notice carries no sender domain to compare.",
    "broker_name": "No broker was named on the notice.",
    "line_of_business": "The notice has not been classified to a line of business.",
    "policy_type": "The notice does not say which of the insured's policies it is under.",
}

_NEITHER_STATED: dict[str, str] = {
    "broker_reference": "Neither the notice nor this policy carries a broker reference.",
    "contract_number": "Neither the notice nor this policy carries a contract number.",
    "project_name": "Neither the notice nor this policy names a project.",
    "insured_domain": "No insured email domain is held on either side.",
    "broker_domain": "No broker domain is held against this policy.",
    "risk_location": "This policy has no location recorded to compare against.",
    "policy_type": "No product is recorded against this policy to compare.",
}

#: The "what we matched on" panel's per-row caption.
_READ_FROM: dict[str, str] = {
    "policy_number": "Used as the primary identifier for the search.",
    "broker_reference": "Used as an identifier — brokers quote their own references reliably.",
    "contract_number": "Used as the primary identifier for a construction risk.",
    "insured_name": "Used to search the book by insured.",
    "project_name": "Used to search the book by project.",
    "policy_period": "Checked against each candidate's period of cover.",
    "risk_location": "Checked against each candidate's schedule of insured locations.",
    "insured_organisation": "Used to disambiguate group companies.",
    "insured_domain": "Compared against the insured's domain, not the sender's.",
    "broker_domain": "Taken from the envelope and compared against the broker on the policy.",
    "broker_name": "Compared against the broker who placed each candidate.",
    "line_of_business": "Narrows the book; never decides on its own.",
    "policy_type": "Separates two policies the same insured holds in one line.",
}


__all__ = [
    "AXIS_LABEL",
    "CONSTRUCTION_LINES",
    "ENGINE_VERSION",
    "GENERIC_EMAIL_DOMAINS",
    "IDENTITY_SIGNALS",
    "POLICY_TYPE_FAMILIES",
    "PRIMARY_IDENTIFIERS",
    "SIGNALS",
    "SIGNAL_BY_KEY",
    "STRENGTH_FOR_CONFIDENCE",
    "CandidateDisplay",
    "CandidateMatch",
    "CandidateWarning",
    "IdentificationResult",
    "NoticeSignals",
    "PolicyFacts",
    "PolicyLocationFacts",
    "SignalAxis",
    "SignalDefinition",
    "SignalResult",
    "SignalValue",
    "compare",
    "contenders",
    "entity_similarity",
    "identify",
    "policy_type_family",
    "searched_on",
]
