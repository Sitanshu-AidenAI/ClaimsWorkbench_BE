"""Configurable claims rules the FNOL services read.

What a carrier considers a required field, a specialist line or a litigation
signal changes with the book it writes. None of it belongs in a controller or a
React component, so it is stated once here, as data, and the services walk it.

This is a configuration surface, not a rule engine: there is no builder UI in the
application, so a table of tuples is the honest shape. If one is added later,
these tables become its seed rather than its competition.
"""

from __future__ import annotations

from dataclasses import dataclass, field

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


def required_fields(line_of_business: LineOfBusiness | None) -> tuple[RequiredField, ...]:
    """The full requirement set for a line, base first so the order is stable."""
    if line_of_business is None:
        return BASE_REQUIRED_FIELDS
    return BASE_REQUIRED_FIELDS + LOB_REQUIRED_FIELDS.get(line_of_business, ())


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

#: Phrases that indicate a third party is seriously hurt, which changes the route
#: regardless of the reserve.
INJURY_SIGNALS: tuple[str, ...] = (
    "fatality",
    "fatal",
    "died",
    "life-changing",
    "amputation",
    "hospitalised",
    "hospitalized",
    "intensive care",
    "serious injury",
)
