"""Configurable claims rules the FNOL services read.

What a carrier considers a required field, a specialist line or a litigation
signal changes with the book it writes. None of it belongs in a controller or a
React component, so it is stated once here, as data, and the services walk it.

This is a configuration surface, not a rule engine: there is no builder UI in the
application, so a table of tuples is the honest shape. If one is added later,
these tables become its seed rather than its competition.
"""

from __future__ import annotations

import re
from collections.abc import Iterable
from dataclasses import dataclass, field
from enum import StrEnum
from functools import lru_cache

from app.domain.enums import LineOfBusiness, TriageCategory


@dataclass(frozen=True, slots=True)
class RequiredField:
    """One thing an FNOL has to state before it can become a claim.

    `path` is the dotted address the extraction and the API both use, so a
    completeness result points at a field the officer can actually go and edit.
    """

    path: str
    label: str
    #: Critical fields block claim creation. The rest lower the completeness
    #: score and are listed as outstanding, but do not hold the case.
    critical: bool = False
    #: Which section of the review form the field lives in.
    section: str = "loss"


#: Required of every FNOL, whatever the line of business.
BASE_REQUIRED_FIELDS: tuple[RequiredField, ...] = (
    RequiredField("policy.policy_number", "Policy number", critical=True, section="policy"),
    RequiredField("policy.insured_name", "Insured name", critical=True, section="policy"),
    RequiredField("loss.date_of_loss", "Date of loss", critical=True, section="loss"),
    RequiredField("loss.loss_location", "Loss location", critical=True, section="loss"),
    RequiredField("loss.loss_description", "Description of loss", critical=True, section="loss"),
    RequiredField("loss.cause_of_loss", "Cause of loss", section="loss"),
    RequiredField("loss.loss_country", "Country of loss", section="loss"),
    RequiredField("notification.reporter_name", "Reporter name", section="notification"),
    RequiredField("notification.reporter_email", "Reporter email", section="notification"),
    RequiredField("parties.claimant_name", "Claimant / contact", section="parties"),
    RequiredField("financial.estimated_loss", "Estimated loss", section="financial"),
    RequiredField("documents.supporting", "Supporting documentation", section="documents"),
    # The four exposure flags. Required *as questions*: "no" is a complete answer
    # and scores as present, and only silence counts as missing. They are here
    # because the columns became tri-state and silence had nowhere else to go —
    # it used to be written as `false`, read by severity as `bool()`, and shown to
    # nobody, so an unassessed pollution exposure was indistinguishable from a
    # ruled-out one on a claim where extraction had recovered 24 of 30 fields.
    RequiredField("loss.business_interruption", "Business interruption", section="loss"),
    RequiredField("loss.structural_damage", "Structural damage", section="loss"),
    RequiredField("loss.environmental_exposure", "Environmental exposure", section="loss"),
    RequiredField("additional.potential_litigation", "Litigation exposure", section="additional"),
)

#: Added on top of the base set, per line of business.
LOB_REQUIRED_FIELDS: dict[LineOfBusiness, tuple[RequiredField, ...]] = {
    LineOfBusiness.PROPERTY: (
        RequiredField("loss.affected_assets", "Affected property or assets", section="loss"),
        RequiredField("financial.repair_estimate", "Repair estimate", section="financial"),
    ),
    LineOfBusiness.MOTOR: (
        RequiredField("loss.affected_assets", "Vehicle details", critical=True, section="loss"),
        RequiredField("additional.police_reference", "Police reference", section="additional"),
    ),
    LineOfBusiness.MARINE: (
        RequiredField("loss.affected_assets", "Vessel or cargo", critical=True, section="loss"),
        RequiredField("additional.incident_reference", "Incident reference", section="additional"),
    ),
    LineOfBusiness.LIABILITY: (
        RequiredField(
            "parties.third_parties", "Third party details", critical=True, section="parties"
        ),
    ),
    LineOfBusiness.CASUALTY: (
        RequiredField("parties.third_parties", "Third party details", section="parties"),
        RequiredField("loss.injuries", "Injuries reported", section="loss"),
    ),
    LineOfBusiness.WORKERS_COMPENSATION: (
        RequiredField("loss.injuries", "Injuries reported", critical=True, section="loss"),
        RequiredField("parties.witnesses", "Witnesses", section="parties"),
    ),
    LineOfBusiness.CYBER: (
        RequiredField("additional.incident_reference", "Incident reference", section="additional"),
    ),
    LineOfBusiness.CONSTRUCTION: (
        RequiredField("loss.affected_assets", "Affected works", section="loss"),
    ),
    LineOfBusiness.ENGINEERING: (
        RequiredField("loss.affected_assets", "Affected plant", section="loss"),
    ),
}


#: Facts about a loss that make a field necessary whatever line the classifier
#: settled on. A `RequiredField` is a question the notice has to answer, and the
#: question "who was hurt" is asked by two people being in hospital, not by a
#: keyword tally picking `casualty` over `construction`.
class ClaimSignal(StrEnum):
    """A condition read off the notice that adds requirements of its own."""

    INJURIES = "injuries"
    FATALITIES = "fatalities"
    THIRD_PARTY = "third_party"
    ENVIRONMENTAL = "environmental"
    LITIGATION = "litigation"
    VEHICLE = "vehicle"


#: Added on top of the base and per-line sets, per detected signal.
#:
#: These compose. The defect they exist to close is a real one: a trench collapse
#: with two hospitalised workers and £481k of third-party exposure tied three ways
#: between `liability`, `casualty` and `construction`, and the winner was decided
#: by dict-insertion order. Whichever line won, only that line's requirements
#: applied — so `parties.third_parties` and `loss.injuries` were asked for or not
#: on the strength of a coin toss. A notice that says people were hurt has to
#: state who and how many regardless of which line the words leaned towards.
SIGNAL_REQUIRED_FIELDS: dict[ClaimSignal, tuple[RequiredField, ...]] = {
    ClaimSignal.INJURIES: (
        RequiredField("loss.injuries", "Injuries reported", critical=True, section="loss"),
        RequiredField("parties.witnesses", "Witnesses", section="parties"),
        RequiredField(
            "additional.authorities_involved", "Authorities involved", section="additional"
        ),
    ),
    ClaimSignal.FATALITIES: (
        RequiredField("loss.fatalities", "Fatalities", critical=True, section="loss"),
        RequiredField(
            "additional.authorities_involved",
            "Authorities involved",
            critical=True,
            section="additional",
        ),
        RequiredField("parties.witnesses", "Witnesses", section="parties"),
    ),
    ClaimSignal.THIRD_PARTY: (
        # Not critical here, and critical under `LineOfBusiness.LIABILITY` above.
        # The defect this closes is that the field was not required *at all* unless
        # the classifier's tie happened to land on liability or casualty; whether an
        # unanswered third party then blocks claim creation is the line's call, and
        # the line already states it.
        RequiredField("parties.third_parties", "Third party details", section="parties"),
    ),
    ClaimSignal.ENVIRONMENTAL: (
        RequiredField(
            "additional.authorities_involved", "Authorities involved", section="additional"
        ),
        RequiredField("loss.affected_assets", "Affected property or assets", section="loss"),
    ),
    ClaimSignal.LITIGATION: (
        RequiredField("parties.third_parties", "Third party details", section="parties"),
    ),
    ClaimSignal.VEHICLE: (
        RequiredField("loss.affected_assets", "Vehicle details", section="loss"),
        RequiredField("additional.police_reference", "Police reference", section="additional"),
    ),
}


def required_fields(
    line_of_business: LineOfBusiness | None,
    signals: Iterable[ClaimSignal] = (),
) -> tuple[RequiredField, ...]:
    """The full requirement set for a line and what the notice actually says.

    Base first, then the line's own additions, then anything the detected signals
    add, so the order is stable and a field's position does not move when a new
    signal fires. A path named more than once appears once, and the strictest
    claim on it wins: a field that is critical under any rule that reached it is
    critical, because "we can create the claim without this" and "we cannot" is
    not something to average.
    """
    ordered: dict[str, RequiredField] = {}

    def add(requirement: RequiredField) -> None:
        existing = ordered.get(requirement.path)
        if existing is None:
            ordered[requirement.path] = requirement
        elif requirement.critical and not existing.critical:
            # Keep the first label and section — the base table's wording is the
            # one the review form is built around — and take only the escalation.
            ordered[requirement.path] = RequiredField(
                existing.path, existing.label, critical=True, section=existing.section
            )

    for requirement in BASE_REQUIRED_FIELDS:
        add(requirement)
    if line_of_business is not None:
        for requirement in LOB_REQUIRED_FIELDS.get(line_of_business, ()):
            add(requirement)
    for signal in signals:
        for requirement in SIGNAL_REQUIRED_FIELDS.get(signal, ()):
            add(requirement)

    return tuple(ordered.values())


# ---------------------------------------------------------------------------
# Classification vocabulary
# ---------------------------------------------------------------------------

#: Loss types offered per line of business. The AI is constrained to these, and
#: anything it invents is rejected by the validator rather than stored.
LOSS_TYPES: dict[LineOfBusiness, tuple[str, ...]] = {
    LineOfBusiness.PROPERTY: (
        "fire",
        "flood",
        "storm",
        "escape_of_water",
        "impact",
        "theft",
        "subsidence",
        "business_interruption",
        "machinery_breakdown",
        "other",
    ),
    LineOfBusiness.MOTOR: (
        "collision",
        "theft",
        "fire",
        "windscreen",
        "third_party_injury",
        "third_party_property",
        "flood",
        "other",
    ),
    LineOfBusiness.MARINE: (
        "cargo_damage",
        "cargo_loss",
        "hull_damage",
        "general_average",
        "delay",
        "piracy",
        "other",
    ),
    LineOfBusiness.LIABILITY: (
        "public_liability",
        "product_liability",
        "professional_indemnity",
        "employers_liability",
        "directors_officers",
        "other",
    ),
    LineOfBusiness.CASUALTY: (
        "bodily_injury",
        "property_damage",
        "environmental",
        "other",
    ),
    LineOfBusiness.WORKERS_COMPENSATION: (
        "workplace_injury",
        "occupational_illness",
        "fatality",
        "other",
    ),
    LineOfBusiness.CYBER: (
        "ransomware",
        "data_breach",
        "business_interruption",
        "funds_transfer_fraud",
        "system_damage",
        "other",
    ),
    LineOfBusiness.CONSTRUCTION: (
        "contract_works",
        "plant_damage",
        "third_party_injury",
        "delay",
        "other",
    ),
    LineOfBusiness.ENGINEERING: (
        "machinery_breakdown",
        "electronic_equipment",
        "erection_all_risks",
        "other",
    ),
    LineOfBusiness.SPECIALTY: (
        "fine_art",
        "terrorism",
        "political_risk",
        "aviation",
        "other",
    ),
    LineOfBusiness.UNKNOWN: ("other",),
}


def valid_loss_type(line_of_business: LineOfBusiness, loss_type: str | None) -> str | None:
    """Return `loss_type` when the line offers it, otherwise `None`.

    The point of the round trip is that a hallucinated loss type never reaches
    the database — an unrecognised value is dropped and the field reads as
    missing, which the completeness engine then reports honestly.
    """
    if not loss_type:
        return None
    normalised = loss_type.strip().lower().replace(" ", "_").replace("-", "_")
    return normalised if normalised in LOSS_TYPES.get(line_of_business, ()) else None


#: Keyword hints used by the deterministic classifier and as a cross-check on the
#: model's answer. Weighted so a single incidental word does not decide a line.
LOB_KEYWORDS: dict[LineOfBusiness, tuple[str, ...]] = {
    LineOfBusiness.PROPERTY: (
        "warehouse",
        "premises",
        "building",
        "roof",
        "sprinkler",
        "flood",
        "fire damage",
        "escape of water",
        "storm damage",
        "factory",
        "retail unit",
        "stock damage",
    ),
    LineOfBusiness.MOTOR: (
        "vehicle",
        "van",
        "lorry",
        "hgv",
        "car",
        "registration",
        "collision",
        "windscreen",
        "driver",
        "fleet",
        "motor",
    ),
    LineOfBusiness.MARINE: (
        "cargo",
        "vessel",
        "shipment",
        "container",
        "bill of lading",
        "port",
        "hull",
        "voyage",
        "freight",
    ),
    LineOfBusiness.LIABILITY: (
        "liability",
        "negligence",
        "claimant solicitor",
        "letter of claim",
        "third party claim",
        "professional indemnity",
    ),
    LineOfBusiness.CASUALTY: ("bodily injury", "casualty", "environmental", "pollution"),
    LineOfBusiness.WORKERS_COMPENSATION: (
        "employee",
        "workplace",
        "injured at work",
        "riddor",
        "occupational",
        "employers liability",
    ),
    LineOfBusiness.CYBER: (
        "ransomware",
        "cyber",
        "data breach",
        "phishing",
        "encrypted",
        "malware",
        "exfiltration",
        "systems offline",
    ),
    LineOfBusiness.CONSTRUCTION: (
        "site",
        "contract works",
        "scaffolding",
        "excavation",
        "principal contractor",
    ),
    LineOfBusiness.ENGINEERING: (
        "machinery",
        "boiler",
        "turbine",
        "plant breakdown",
        "conveyor",
        "press",
    ),
    LineOfBusiness.SPECIALTY: ("fine art", "terrorism", "political risk", "aviation"),
}


# ---------------------------------------------------------------------------
# Triage
# ---------------------------------------------------------------------------


@dataclass(frozen=True, slots=True)
class RouteDefinition:
    """A destination triage can recommend."""

    key: str
    label: str
    team: str
    #: The skill an assignment has to match. Empty means any handler will do.
    required_skill: str | None = None
    lines_of_business: frozenset[LineOfBusiness] = field(default_factory=frozenset)


#: Ordered most specialised first: triage takes the first route whose categories
#: are present, so "major loss property" beats the general property desk.
ROUTES: tuple[tuple[frozenset[TriageCategory], RouteDefinition], ...] = (
    (
        frozenset({TriageCategory.FRAUD_REVIEW}),
        RouteDefinition("siu", "Special Investigations Unit", "SIU", "fraud_investigation"),
    ),
    (
        frozenset({TriageCategory.LITIGATION_RISK}),
        RouteDefinition("litigation", "Litigation & Large Loss", "Litigation", "litigation"),
    ),
    (
        frozenset({TriageCategory.MAJOR_LOSS}),
        RouteDefinition("major_loss", "Major Loss Team", "Major Loss", "major_loss"),
    ),
    (
        frozenset({TriageCategory.CAT_CLAIM}),
        RouteDefinition("cat", "Catastrophe Response Unit", "CAT Response", "catastrophe"),
    ),
    (
        frozenset({TriageCategory.SPECIALIST_REQUIRED}),
        RouteDefinition("specialist", "Specialist Claims", "Specialist", None),
    ),
    (
        frozenset({TriageCategory.COMPLEX}),
        RouteDefinition("complex", "Complex Claims", "Complex", None),
    ),
    (
        frozenset({TriageCategory.SIMPLE}),
        RouteDefinition("fast_track", "Fast Track", "Fast Track", None),
    ),
)

#: Lines that always need a specialist, whatever the size of the loss.
SPECIALIST_LINES: frozenset[LineOfBusiness] = frozenset(
    {
        LineOfBusiness.MARINE,
        LineOfBusiness.CYBER,
        LineOfBusiness.ENGINEERING,
        LineOfBusiness.SPECIALTY,
        LineOfBusiness.CONSTRUCTION,
    }
)

#: Phrases in a loss description or document that suggest a claim will be fought.
LITIGATION_SIGNALS: tuple[str, ...] = (
    "solicitor",
    "letter of claim",
    "litigation",
    "court",
    "writ",
    "subrogation",
    "legal proceedings",
    "claim form",
    "pre-action protocol",
    "attorney",
    "lawsuit",
)

#: Phrases that indicate somebody is seriously hurt, which changes the route
#: regardless of the reserve.
#:
#: The original list held nine clinical terms, and the notices this is a fallback
#: for do not use them. A real trench-collapse notice reads "two employees were
#: buried to chest height", "a pelvic fracture", "a crush injury", "admitted
#: overnight" — and matched none of the nine, so a two-casualty incident read as
#: having no injury signal at all whenever the structured `injuries` field was not
#: extracted.
#:
#: Every entry is matched on word boundaries by `matches_any`, which is what makes
#: broadening it safe: a route to the major-loss desk is expensive to get wrong, and
#: "icu" must not be reachable from "particular" nor "hospital" from a loss at one.
#: A trailing `*` marks a stem — `fractur*` reaches fracture, fractured and
#: fracturing — and stems are long enough that what else they could reach is not a
#: word anybody writes.
INJURY_SIGNALS: tuple[str, ...] = (
    # Fatal.
    "fatal",
    "fatalit*",
    "died",
    "deceased",
    "pronounced dead",
    "killed",
    "loss of life",
    # Named injuries that are serious by definition.
    "life-changing",
    "life changing",
    "amputat*",
    "degloving",
    "crush injur*",
    "crushed",
    "fractur*",
    "broken leg",
    "broken arm",
    "broken back",
    "broken hip",
    "broken pelvis",
    "spinal",
    "paralys*",
    "paralyz*",
    "head injur*",
    "traumatic brain",
    "internal bleeding",
    "haemorrhage",
    "hemorrhage",
    "degree burns",
    "suffered burns",
    "burns to",
    "asphyxi*",
    "electrocut*",
    "impaled",
    "impalement",
    "severed",
    # Where they ended up. "Admitted" is the word a notice uses when it will not
    # name the injury, and it means the same thing.
    "hospitalis*",
    "hospitaliz*",
    "taken to hospital",
    "admitted to hospital",
    "admitted overnight",
    "intensive care",
    "critical condition",
    "emergency surgery",
    "air ambulance",
    "medevac",
    "life flight",
    "trauma centre",
    "trauma center",
    "icu",
    # Plain language for the mechanism, which is often all a first notice states.
    "serious injur*",
    "seriously injured",
    "severely injured",
    "critically injured",
    "multiple casualt*",
    "buried",
    "trapped",
    "pinned",
    "fell from height",
    "fall from height",
    "unconscious",
    "riddor",
)

#: Phrases that say somebody who is not the insured has been affected, or is
#: coming. One of these present is what makes third-party details a required
#: field — independently of which line of business the classifier settled on,
#: which is the point: the exposure exists whether the notice reads as
#: `liability`, `casualty` or `construction`.
THIRD_PARTY_SIGNALS: tuple[str, ...] = (
    "third party",
    "third-party",
    "third parties",
    "member of the public",
    "members of the public",
    "passer-by",
    "passerby",
    "pedestrian",
    "neighbouring",
    "neighboring",
    "adjoining owner",
    "adjoining propert*",
    "adjacent propert*",
    "claimant solicitor",
    "letter of claim",
    "public liability",
    "bodily injury",
    "injured party",
    "tenant",
    "subcontractor employee",
    "employee of ",
    "damage to the highway",
)

#: Phrases that say the loss has reached the ground, the water or the air. Same
#: reasoning as the third-party list: a pollution exposure is a fact about the
#: loss, not about the line it was filed under.
ENVIRONMENTAL_SIGNALS: tuple[str, ...] = (
    "contaminat*",
    "pollut*",
    "spillage",
    "spilled",
    "spilt",
    "release to ground",
    "groundwater",
    "watercourse",
    "storm drain",
    "environment agency",
    "environmental protection",
    "hazmat",
    "chemical release",
    "ammonia release",
    "fuel leak",
    "oil leak",
    "asbestos",
    "silt runoff",
)

#: Phrases that say a vehicle is in the loss, whatever the line it was filed
#: under. `vin` is deliberately absent: matched on a word boundary it is still one
#: letter away from every "vinyl" and "vineyard" in a property notice, and the
#: registration terms below carry the same fact without the risk.
VEHICLE_SIGNALS: tuple[str, ...] = (
    "vehicle",
    "lorry",
    "hgv",
    "tipper",
    "flatbed",
    "registration number",
    "number plate",
    "licence plate",
    "license plate",
    "rear-ended",
    "collided with",
)


@lru_cache(maxsize=64)
def signal_pattern(terms: tuple[str, ...]) -> re.Pattern[str]:
    r"""One compiled alternation over `terms`, boundaried where that means anything.

    A leading `\b` always. A trailing one only when the term ends in a word
    character *and* is not marked as a stem, because `"employee of "\b` can never
    match and `"fractur"\b` never matches "fractured" — a term that silently drops
    itself out of the alternation is worse than no term at all, because the list
    still reads as covering the case.

    A trailing `*` marks a stem: `"fractur*"` matches fracture, fractured and
    fracturing. Written out rather than inferred, because "does this word carry a
    suffix" is not something a pattern builder can be trusted to guess, and a wrong
    guess either loses the inflections or reaches into the middle of an unrelated
    word.

    A stem does match any longer word beginning with it — "fracturewood" would hit
    `fractur*`. That is the trade, and it is a small one: the defect this protects
    against is a *leading*-boundary failure, `car` inside `Carbondale`, and stems are
    kept long enough that the compounds they could reach are not words. Every entry
    marked as a stem is at least six characters for that reason.
    """
    alternatives = sorted({term for term in terms if term}, key=len, reverse=True)
    parts: list[str] = []
    for term in alternatives:
        if term.endswith("*"):
            parts.append(r"\b" + re.escape(term[:-1]))
        else:
            parts.append(r"\b" + re.escape(term) + (r"\b" if term[-1:].isalnum() else ""))
    # Case-insensitive rather than relying on every caller to have normalised.
    # `has_serious_injury_signal` is handed `text_signals(case)`, which is lowered —
    # and was also handed raw notice text by anything that did not know that, so
    # "RIDDOR" read as no signal at all.
    return re.compile("|".join(parts), re.IGNORECASE)


def matches_any(text: str, terms: Iterable[str]) -> bool:
    """Whether any of `terms` appears in `text` as a word rather than a substring."""
    return bool(signal_pattern(tuple(terms)).search(text))
