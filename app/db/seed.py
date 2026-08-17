"""Development seed data.

Reference data first — policies, catastrophe events, handlers — then seven
notifications chosen so that every state the intake module can be in is reachable
without anyone having to construct it by hand:

    A  straight-through commercial property claim
    B  a possible duplicate of A
    C  a major loss with business interruption
    D  a flood attributed to a catastrophe event
    E  a loss outside the policy period
    F  several fraud indicators at once
    G  an incomplete notice with no policy to match

The notifications are written as the *sources they would actually arrive as* —
broker emails, portal payloads, call notes — and then run through the real
pipeline. Nothing here writes a severity, a duplicate score or an exception
directly: the demo data is a set of inputs, and everything the screens show is
computed by the same code that will run in production.

Every notice that then has nothing blocking it is converted into a claim, which
is what gives the claims queue, the triage and assignment rows and the knowledge
graph something to read. That conversion goes through `ClaimCreationService` for
the same reason: the demo claims are the ones an officer could have created, and
the blocked scenarios stay in the intake queue.

    uv run python -m app.db.seed          # add anything missing
    uv run python -m app.db.seed --reset  # clear the FNOL tables and rebuild
"""

from __future__ import annotations

import argparse
import asyncio
from datetime import UTC, date, datetime, timedelta
from typing import Any

from sqlalchemy import delete, select

from app.api.deps.services import build_pipeline
from app.core.config import settings
from app.core.logging import configure_logging, get_logger
from app.db.session import dispose_engine, init_engine, session_scope
from app.domain.enums import FNOLChannel
from app.models.audit import AuditEvent
from app.models.claim import Claim, ClaimAssignment, ClaimTriage
from app.models.fnol import (
    FNOLAIAnalysis,
    FNOLCase,
    FNOLDocument,
    FNOLDuplicateCandidate,
    FNOLException,
    FNOLExtractedField,
    FNOLNote,
    FNOLParty,
    FNOLPolicyMatch,
)
from app.models.reference_data import CatEvent, Handler, Policy, ReferenceSequence
from app.repositories.audit import AuditRepository
from app.repositories.catastrophe import CatEventRepository
from app.repositories.claim import ClaimRepository
from app.repositories.extraction import ExtractionSchemaRepository
from app.repositories.fnol import FNOLRepository
from app.repositories.handler import HandlerRepository
from app.repositories.reference import ReferenceRepository
from app.services.ai.factory import get_ai_provider
from app.services.documents.service import DocumentProcessingService
from app.services.extraction.registry import seed_builtin_schemas
from app.services.fnol.audit import AuditService
from app.services.fnol.claims import ClaimCreationBlocked, ClaimCreationService
from app.services.fnol.ingestion import (
    FNOLIngestionService,
    IncomingAttachment,
    IncomingEmail,
    IncomingNotification,
)
from app.services.fnol.service import FNOLService
from app.services.fnol.triage import AssignmentService, TriageService
from app.services.intelligence.embedding import get_embedding_provider
from app.services.intelligence.vectors import get_vector_store

logger = get_logger(__name__)

TODAY = date.today()


def _days_ago(days: int) -> datetime:
    return datetime.now(UTC) - timedelta(days=days)


# ---------------------------------------------------------------------------
# Reference data
# ---------------------------------------------------------------------------

POLICIES: list[dict[str, Any]] = [
    {
        "policy_number": "POL-2026-0041",
        "insured_name": "Northline Logistics Limited",
        "insured_organisation": "Northline Logistics Limited",
        "insured_email": "operations@northline-logistics.co.uk",
        "broker_name": "Harding Vale Brokers",
        "broker_reference": "HVB/NL/0041",
        "policy_type": "Commercial Combined",
        "line_of_business": "property",
        "status": "active",
        "effective_date": date(TODAY.year, 1, 1),
        "expiry_date": date(TODAY.year, 12, 31),
        "country": "United Kingdom",
        "region": "Yorkshire",
        "primary_location": "Unit 7, Wakefield Road, Leeds LS9 8AA",
        "latitude": 53.7965,
        "longitude": -1.5210,
        "currency": "GBP",
        "limit_amount_minor": 5_000_000_00,
        "deductible_amount_minor": 25_000_00,
        "perils_covered": ["fire", "flood", "storm", "escape of water", "impact", "theft"],
        "exclusions": ["wear and tear", "gradual deterioration", "terrorism"],
        "locations": [
            {"address": "Unit 7, Wakefield Road, Leeds LS9 8AA", "use": "warehouse"},
            {"address": "Airedale Depot, Keighley BD21 4LP", "use": "depot"},
        ],
    },
    {
        "policy_number": "POL-2026-0198",
        "insured_name": "Northline Logistics (Scotland) Limited",
        "insured_organisation": "Northline Logistics (Scotland) Limited",
        "insured_email": "scotland@northline-logistics.co.uk",
        "broker_name": "Harding Vale Brokers",
        "broker_reference": "HVB/NLS/0198",
        "policy_type": "Commercial Combined",
        "line_of_business": "property",
        "status": "active",
        "effective_date": date(TODAY.year, 3, 1),
        "expiry_date": date(TODAY.year + 1, 2, 28),
        "country": "United Kingdom",
        "region": "Lanarkshire",
        "primary_location": "Clyde Gateway Estate, Glasgow G32 8RH",
        "latitude": 55.8412,
        "longitude": -4.1712,
        "currency": "GBP",
        "limit_amount_minor": 3_000_000_00,
        "deductible_amount_minor": 20_000_00,
        "perils_covered": ["fire", "flood", "storm", "theft"],
        "exclusions": ["subsidence", "terrorism"],
    },
    {
        "policy_number": "CP-2026-7781",
        "insured_name": "Ashford Freight Services Ltd",
        "insured_organisation": "Ashford Freight Services Ltd",
        "insured_email": "claims@ashfordfreight.co.uk",
        "broker_name": "Merridge & Co",
        "broker_reference": "MER/AF/7781",
        "policy_type": "Motor Fleet",
        "line_of_business": "motor",
        "status": "active",
        "effective_date": date(TODAY.year, 2, 1),
        "expiry_date": date(TODAY.year + 1, 1, 31),
        "country": "United Kingdom",
        "region": "Kent",
        "primary_location": "Orbital Park, Ashford TN24 0GA",
        "latitude": 51.1279,
        "longitude": 0.8630,
        "currency": "GBP",
        "limit_amount_minor": 1_500_000_00,
        "deductible_amount_minor": 2_500_00,
        "perils_covered": ["collision", "fire", "theft", "third party injury"],
        "exclusions": ["racing", "unlicensed driver"],
    },
    {
        "policy_number": "MF-2026-3320",
        "insured_name": "Solent Marine Cargo plc",
        "insured_organisation": "Solent Marine Cargo plc",
        "insured_email": "cargo@solentmarine.com",
        "broker_name": "Ridgeway Marine",
        "broker_reference": "RM/SMC/3320",
        "policy_type": "Marine Cargo",
        "line_of_business": "marine",
        "status": "active",
        "effective_date": date(TODAY.year, 1, 15),
        "expiry_date": date(TODAY.year + 1, 1, 14),
        "country": "United Kingdom",
        "region": "Hampshire",
        "primary_location": "Berth 42, Port of Southampton SO14 3QN",
        "latitude": 50.8998,
        "longitude": -1.4180,
        "currency": "USD",
        "limit_amount_minor": 8_000_000_00,
        "deductible_amount_minor": 50_000_00,
        "perils_covered": ["cargo damage", "cargo loss", "general average"],
        "exclusions": ["inherent vice", "insufficient packing"],
    },
    {
        "policy_number": "PI-2025-5504",
        "insured_name": "Calderwood Consulting LLP",
        "insured_organisation": "Calderwood Consulting LLP",
        "insured_email": "risk@calderwood-consulting.com",
        "broker_name": "Merridge & Co",
        "broker_reference": "MER/CC/5504",
        "policy_type": "Professional Indemnity",
        "line_of_business": "liability",
        "status": "lapsed",
        # Deliberately expired: scenario E's loss falls after this date.
        "effective_date": date(TODAY.year - 1, 6, 1),
        "expiry_date": date(TODAY.year, 5, 31),
        "country": "United Kingdom",
        "region": "Greater Manchester",
        "primary_location": "3 Peter Street, Manchester M2 5QR",
        "latitude": 53.4783,
        "longitude": -2.2480,
        "currency": "GBP",
        "limit_amount_minor": 2_000_000_00,
        "deductible_amount_minor": 10_000_00,
        "perils_covered": ["professional indemnity", "negligence"],
        "exclusions": ["fraud", "known circumstances"],
    },
    {
        "policy_number": "CY-2026-9120",
        "insured_name": "Verity Health Systems Ltd",
        "insured_organisation": "Verity Health Systems Ltd",
        "insured_email": "security@verityhealth.io",
        "broker_name": "Harding Vale Brokers",
        "broker_reference": "HVB/VH/9120",
        "policy_type": "Cyber & Data",
        "line_of_business": "cyber",
        "status": "active",
        "effective_date": TODAY - timedelta(days=18),
        "expiry_date": TODAY + timedelta(days=347),
        "country": "United Kingdom",
        "region": "Berkshire",
        "primary_location": "Thames Valley Park, Reading RG6 1PT",
        "latitude": 51.4560,
        "longitude": -0.9330,
        "currency": "GBP",
        "limit_amount_minor": 1_000_000_00,
        "deductible_amount_minor": 25_000_00,
        "perils_covered": ["ransomware", "data breach", "business interruption"],
        "exclusions": ["unpatched known vulnerability", "war"],
    },
]


CAT_EVENTS: list[dict[str, Any]] = [
    {
        "reference": f"CAT-{TODAY.year}-004",
        "name": "Storm Isolde — severe flooding, northern England",
        "event_type": "flood",
        "perils": ["flood", "storm"],
        "severity": "major",
        "status": "open",
        "start_date": TODAY - timedelta(days=9),
        "end_date": TODAY - timedelta(days=5),
        "country": "United Kingdom",
        "region": "Yorkshire and the Humber",
        "affected_areas": ["Leeds", "Wakefield", "Bradford", "Calderdale", "York"],
        "latitude": 53.8008,
        "longitude": -1.5491,
        "radius_km": 90,
    },
    {
        "reference": f"CAT-{TODAY.year}-002",
        "name": "Storm Halvard — wind damage, southern England",
        "event_type": "storm",
        "perils": ["storm", "hail"],
        "severity": "moderate",
        "status": "closed",
        "start_date": TODAY - timedelta(days=64),
        "end_date": TODAY - timedelta(days=61),
        "country": "United Kingdom",
        "region": "South East",
        "affected_areas": ["Kent", "Sussex", "Hampshire"],
        "latitude": 51.1000,
        "longitude": 0.5000,
        "radius_km": 120,
    },
]


HANDLERS: list[dict[str, Any]] = [
    {
        "full_name": "Rebecca Marsh",
        "email": "r.marsh@carrier.com",
        "subject": "8f2c41a9-6b2e-4c77-9a1d-1f0e5c3b7a20",
        "team": "Property",
        "job_title": "Senior Claims Handler",
        "skills": ["property", "business_interruption"],
        "lines_of_business": ["property", "construction"],
        "countries": ["United Kingdom"],
        "max_severity": "high",
        "authority_limit_minor": 250_000_00,
        "open_claims": 12,
        "capacity": 25,
    },
    {
        "full_name": "Daniel Okafor",
        "email": "d.okafor@carrier.com",
        "team": "Major Loss",
        "job_title": "Major Loss Adjuster",
        "skills": ["major_loss", "business_interruption", "property"],
        "lines_of_business": ["property", "engineering", "construction"],
        "countries": ["United Kingdom", "Ireland"],
        "max_severity": "critical",
        "authority_limit_minor": 2_000_000_00,
        "open_claims": 6,
        "capacity": 12,
    },
    {
        "full_name": "Priya Raghunathan",
        "email": "p.raghunathan@carrier.com",
        "team": "SIU",
        "job_title": "Fraud Investigator",
        "skills": ["fraud_investigation"],
        "lines_of_business": ["property", "motor", "liability"],
        "countries": ["United Kingdom"],
        "max_severity": "high",
        "authority_limit_minor": 100_000_00,
        "open_claims": 9,
        "capacity": 18,
    },
    {
        "full_name": "Tomas Lindqvist",
        "email": "t.lindqvist@carrier.com",
        "team": "CAT Response",
        "job_title": "Catastrophe Claims Lead",
        "skills": ["catastrophe", "property"],
        "lines_of_business": ["property"],
        "countries": ["United Kingdom", "Norway", "Sweden"],
        "max_severity": "critical",
        "authority_limit_minor": 500_000_00,
        "open_claims": 21,
        "capacity": 30,
    },
    {
        "full_name": "Amara Bello",
        "email": "a.bello@carrier.com",
        "team": "Litigation",
        "job_title": "Litigation Claims Specialist",
        "skills": ["litigation", "liability"],
        "lines_of_business": ["liability", "casualty"],
        "countries": ["United Kingdom"],
        "max_severity": "critical",
        "authority_limit_minor": 750_000_00,
        "open_claims": 8,
        "capacity": 15,
    },
    {
        "full_name": "Marcus Feldt",
        "email": "m.feldt@carrier.com",
        "team": "Complex",
        "job_title": "Complex Claims Handler",
        "skills": ["property", "motor", "business_interruption"],
        "lines_of_business": ["property", "motor", "liability", "casualty"],
        "countries": ["United Kingdom"],
        "max_severity": "high",
        "authority_limit_minor": 150_000_00,
        "open_claims": 14,
        "capacity": 24,
    },
    {
        "full_name": "Joel Whitcombe",
        "email": "j.whitcombe@carrier.com",
        "team": "Fast Track",
        "job_title": "Claims Handler",
        "skills": ["motor", "property"],
        "lines_of_business": ["motor", "property"],
        "countries": ["United Kingdom"],
        "max_severity": "medium",
        "authority_limit_minor": 25_000_00,
        "open_claims": 31,
        "capacity": 40,
    },
    {
        "full_name": "Sofia Marchetti",
        "email": "s.marchetti@carrier.com",
        "team": "Specialist",
        "job_title": "Marine & Cyber Specialist",
        "skills": ["marine", "cyber", "specialist"],
        "lines_of_business": ["marine", "cyber", "specialty"],
        "countries": ["United Kingdom", "Italy", "Singapore"],
        "max_severity": "critical",
        "authority_limit_minor": 1_000_000_00,
        "open_claims": 4,
        "capacity": 14,
    },
]


# ---------------------------------------------------------------------------
# Scenarios
# ---------------------------------------------------------------------------

_REPAIR_ESTIMATE_CSV = b"""Item,Description,Quantity,Unit cost,Line total
1,Make safe and strip out fire-damaged racking,1,8400.00,8400.00
2,Replace warehouse roof panels (bay 3),220,64.50,14190.00
3,Electrical first fix and testing,1,11250.00,11250.00
4,Stock write-off - palletised goods,1,38600.00,38600.00
5,Cleaning and decontamination,1,4750.00,4750.00
Total,,,,77190.00
"""

_LOSS_ADJUSTER_NOTE = b"""PRELIMINARY SITE NOTE - NORTHLINE LOGISTICS, LEEDS

Attending adjuster: G. Hasnain
Date of visit: site attended the morning after the incident

Fire originated in the north-east corner of bay 3, adjacent to the battery
charging station for the forklift fleet. The sprinkler system operated and
contained the fire to bay 3, though smoke damage extends across bays 2 and 3.

Structural damage: roof panels above bay 3 have failed and require replacement.
The steel frame appears sound pending an engineer's inspection.

Business interruption: the insured has confirmed that bays 2 and 3 are out of
use. Bay 1 continues to operate. The insured estimates two to three weeks of
reduced throughput.

No injuries. The fire service attended and has issued a report reference.
"""


def scenarios() -> list[dict[str, Any]]:
    """The seven demonstration notifications, as their sources."""
    loss_a = TODAY - timedelta(days=6)
    loss_c = TODAY - timedelta(days=3)
    loss_d = TODAY - timedelta(days=7)
    loss_e = TODAY - timedelta(days=12)
    loss_f = TODAY - timedelta(days=8)

    return [
        # -- A. Straight-through -------------------------------------------
        {
            "key": "A",
            "kind": "email",
            "email": IncomingEmail(
                sender="claims@hardingvale.co.uk",
                recipient="fnol@carrier.com",
                subject="FNOL - Northline Logistics - fire at Leeds warehouse - POL-2026-0041",
                message_id="<hvb-fnol-0041-a@hardingvale.co.uk>",
                received_at=_days_ago(5),
                from_broker=True,
                body=f"""Dear Claims Team

Please treat this email as first notification of loss on behalf of our client.

Policy number: POL-2026-0041
Insured: Northline Logistics Limited
Broker: Harding Vale Brokers
Reported by: Elaine Prosser
Role: Account Handler
Contact email: e.prosser@hardingvale.co.uk
Phone: +44 113 496 2210

Date of loss: {loss_a.strftime("%d/%m/%Y")}
Time of loss: 04:20
Location: Unit 7, Wakefield Road, Leeds LS9 8AA
Country: United Kingdom
Cause: fire
Description: A fire broke out overnight in bay 3 of the insured's distribution
warehouse, starting at the forklift battery charging station. The sprinkler
system operated and contained the fire to that bay, but there is smoke damage
across bays 2 and 3 and the roof panels above bay 3 have failed. The insured
has been unable to use bays 2 and 3 since the incident.

Affected property: warehouse bay 3, roof panels, racking and palletised stock
Injuries: none
Estimated loss: GBP 128,000
Repair estimate: GBP 77,190
Police reference: not applicable
Incident reference: WYFRS/2026/44821
Attachments: repair estimate schedule and the adjuster's preliminary site note

Kind regards
Elaine Prosser
Harding Vale Brokers
""",
                attachments=[
                    IncomingAttachment(
                        "northline-repair-estimate.csv", _REPAIR_ESTIMATE_CSV, "text/csv"
                    ),
                    IncomingAttachment(
                        "northline-site-note.txt", _LOSS_ADJUSTER_NOTE, "text/plain"
                    ),
                ],
            ),
        },
        # -- B. Possible duplicate -----------------------------------------
        {
            "key": "B",
            "kind": "email",
            "email": IncomingEmail(
                sender="operations@northline-logistics.co.uk",
                recipient="fnol@carrier.com",
                subject="Fire damage at our Leeds site - claim",
                message_id="<northline-direct-b@northline-logistics.co.uk>",
                received_at=_days_ago(4),
                from_broker=False,
                body=f"""Hello

I want to make sure a claim has been logged for the fire at our Leeds warehouse.
Our broker may have already sent this but I would rather it were logged twice
than not at all.

Policy: POL-2026-0041
Insured: Northline Logistics Ltd
Reported by: Martin Ovens
Role: Operations Director
Contact email: m.ovens@northline-logistics.co.uk
Phone: 0113 496 2200

Date of loss: {loss_a.strftime("%d %B %Y")}
Location: Wakefield Road, Leeds LS9
Cause: fire
Description: Overnight fire in bay 3 of the warehouse which started at the
forklift charging point. Sprinklers contained it but the roof over bay 3 is
damaged and we have lost a quantity of palletised stock to smoke. Bays 2 and 3
are out of use.
Estimated loss: around GBP 130,000
Injuries: none

Regards
Martin Ovens
""",
            ),
        },
        # -- C. Major loss --------------------------------------------------
        {
            "key": "C",
            "kind": "email",
            "email": IncomingEmail(
                sender="claims@hardingvale.co.uk",
                recipient="fnol@carrier.com",
                subject="URGENT FNOL - Northline Logistics Scotland - major fire - POL-2026-0198",
                message_id="<hvb-fnol-0198-c@hardingvale.co.uk>",
                received_at=_days_ago(2),
                from_broker=True,
                body=f"""Claims Team

Urgent first notification. Our client has suffered a substantial loss.

Policy number: POL-2026-0198
Insured: Northline Logistics (Scotland) Limited
Broker: Harding Vale Brokers
Reported by: Elaine Prosser
Role: Account Handler
Contact email: e.prosser@hardingvale.co.uk
Phone: +44 113 496 2210

Date of loss: {loss_c.strftime("%d/%m/%Y")}
Time of loss: 23:45
Location: Clyde Gateway Estate, Glasgow G32 8RH
Country: United Kingdom
Cause: fire
Description: A major fire has destroyed the main distribution building at the
insured's Glasgow site. The structure has partially collapsed and the building
is a total loss. Operations at the site have been suspended entirely and the
insured has been unable to trade from this location since the incident.
Production stopped and they are diverting volume to third-party warehousing at
significant cost. Two members of the night shift were treated for smoke
inhalation and taken to hospital. Fuel from a damaged tank has entered the
site drainage and SEPA has been notified of the contamination.

Affected property: main distribution building, mezzanine offices, conveyor plant
Injuries: 2
Fatalities: none
Estimated loss: GBP 2,400,000
Incident reference: SFRS/2026/11207
Authorities: Scottish Fire and Rescue Service, SEPA

This will need a major loss adjuster appointed today.

Elaine Prosser
Harding Vale Brokers
""",
            ),
        },
        # -- D. Catastrophe --------------------------------------------------
        {
            "key": "D",
            "kind": "portal",
            "notification": IncomingNotification(
                channel=FNOLChannel.PORTAL,
                received_at=_days_ago(4),
                external_reference="PORTAL-2026-88417",
                reporter_name="Martin Ovens",
                reporter_organisation="Northline Logistics Limited",
                reporter_role="Operations Director",
                reporter_email="m.ovens@northline-logistics.co.uk",
                reporter_phone="+44 113 496 2200",
                source_metadata={
                    "portal": "insured-portal",
                    "form": "property-damage-v3",
                    "submitted_from": "10.22.4.19",
                },
                supplied_fields={
                    "policy_number": "POL-2026-0041",
                    "insured_name": "Northline Logistics Limited",
                    "loss_location": "Airedale Depot, Keighley BD21 4LP",
                    "loss_country": "United Kingdom",
                    "cause_of_loss": "flood",
                },
                body=f"""Policy number: POL-2026-0041
Insured: Northline Logistics Limited
Reported by: Martin Ovens
Contact email: m.ovens@northline-logistics.co.uk
Phone: +44 113 496 2200

Date of loss: {loss_d.strftime("%d/%m/%Y")}
Location: Airedale Depot, Keighley BD21 4LP
Country: United Kingdom
Cause: flood
Description: The river burst its banks during the storm and floodwater entered
the ground floor of our Keighley depot to a depth of about 600mm. Flooding has
damaged the loading bay, the ground floor office and a quantity of stock held at
floor level. Several other units on the estate were flooded at the same time.
Affected property: loading bay, ground floor offices, palletised stock
Injuries: none
Estimated loss: GBP 185,000
""",
            ),
        },
        # -- E. Coverage exception -------------------------------------------
        {
            "key": "E",
            "kind": "email",
            "email": IncomingEmail(
                sender="risk@calderwood-consulting.com",
                recipient="fnol@carrier.com",
                subject="Professional indemnity notification - PI-2025-5504",
                message_id="<calderwood-pi-e@calderwood-consulting.com>",
                received_at=_days_ago(3),
                from_broker=False,
                body=f"""Dear Sirs

We are notifying a circumstance which may give rise to a claim.

Policy: PI-2025-5504
Insured: Calderwood Consulting LLP
Reported by: Helena Vance
Role: Risk Partner
Contact email: h.vance@calderwood-consulting.com
Phone: 0161 402 8890

Date of loss: {loss_e.strftime("%d/%m/%Y")}
Location: 3 Peter Street, Manchester M2 5QR
Country: United Kingdom
Cause: professional indemnity
Description: A former client has instructed solicitors and served a letter of
claim alleging negligent advice given in relation to a property development
appraisal. The letter of claim indicates they will issue proceedings if the
matter is not resolved. We deny the allegations but are notifying in accordance
with the policy condition.
Estimated loss: GBP 450,000
Injuries: none

Yours faithfully
Helena Vance
Calderwood Consulting LLP
""",
            ),
        },
        # -- F. Fraud indicators ---------------------------------------------
        {
            "key": "F",
            "kind": "email",
            "email": IncomingEmail(
                sender="security@verityhealth.io",
                recipient="fnol@carrier.com",
                subject="Cyber incident notification - CY-2026-9120",
                message_id="<verity-cyber-f@verityhealth.io>",
                received_at=_days_ago(1),
                from_broker=False,
                body=f"""Claims

Notifying a cyber incident under our policy.

Policy: CY-2026-9120
Insured: Verity Health Systems Ltd
Reported by: Douglas Rennie
Role: Head of IT
Contact email: d.rennie@verityhealth.io
Phone: 0118 902 4417

Date of loss: {loss_f.strftime("%d/%m/%Y")}
Location: Thames Valley Park, Reading RG6 1PT
Country: United Kingdom
Cause: ransomware
Description: Systems offline. Encrypted. We think it is ransomware.
Estimated loss: GBP 940,000
Injuries: none

Regards
D Rennie
""",
            ),
        },
        # -- G. Incomplete ---------------------------------------------------
        {
            "key": "G",
            "kind": "phone",
            "notification": IncomingNotification(
                channel=FNOLChannel.PHONE,
                received_at=_days_ago(0),
                reporter_name="Unidentified caller",
                reporter_role="Site manager",
                reporter_phone="+44 7700 900311",
                source_metadata={
                    "call_reference": "CC-2026-441902",
                    "call_centre_agent": "K. Abiola",
                    "call_duration_seconds": 214,
                    "line": "FNOL general enquiries",
                },
                body="""CALL NOTE - FNOL GENERAL ENQUIRIES

Caller rang to report damage at a site but could not give a policy number and
was calling from the roadside. Line was poor and the call dropped before the
details could be completed.

What the caller gave:
- Something came off a lorry and struck a unit on the estate
- There is damage to a roller shutter and to the front of the unit
- He is the site manager but did not give the company name
- He will call back with the policy documents

Not captured: policy number, insured name, date of loss, full address, estimate.
""",
            ),
        },
    ]


# ---------------------------------------------------------------------------
# Seeding
# ---------------------------------------------------------------------------

#: The tables a `--reset` clears, children first.
_FNOL_TABLES = (
    AuditEvent,
    ClaimAssignment,
    ClaimTriage,
    FNOLAIAnalysis,
    FNOLDuplicateCandidate,
    FNOLException,
    FNOLExtractedField,
    FNOLNote,
    FNOLParty,
    FNOLPolicyMatch,
    FNOLDocument,
)


async def seed(*, reset: bool = False) -> None:
    configure_logging(settings)
    await init_engine(settings)

    try:
        async with session_scope() as session:
            if reset:
                logger.info("seed_reset_started")
                for model in _FNOL_TABLES:
                    await session.execute(delete(model))
                # `claims` before `fnol_cases`: the notice holds a foreign key
                # to the claim, so that reference has to go first.
                await session.execute(delete(Claim))
                await session.execute(delete(FNOLCase))
                await session.execute(delete(ReferenceSequence))
                await session.flush()

            await _seed_reference_data(session)

        async with session_scope() as session:
            created = await _seed_scenarios(session)

        async with session_scope() as session:
            converted = await _convert_ready_cases(session)

        logger.info("seed_completed", scenarios=created, claims=converted)
    finally:
        await dispose_engine()


async def _seed_reference_data(session: Any) -> None:
    """Insert reference rows that are not already there, keyed by their natural id."""
    for payload in POLICIES:
        existing = (
            (
                await session.execute(
                    select(Policy).where(Policy.policy_number == payload["policy_number"])
                )
            )
            .scalars()
            .first()
        )
        if existing is None:
            session.add(Policy(**payload))

    for payload in CAT_EVENTS:
        existing = (
            (
                await session.execute(
                    select(CatEvent).where(CatEvent.reference == payload["reference"])
                )
            )
            .scalars()
            .first()
        )
        if existing is None:
            session.add(CatEvent(**payload))

    for payload in HANDLERS:
        existing = (
            (await session.execute(select(Handler).where(Handler.email == payload["email"])))
            .scalars()
            .first()
        )
        if existing is None:
            session.add(Handler(**payload))


async def _seed_scenarios(session: Any) -> int:
    """Ingest each notification and run it through the real pipeline."""
    # Ordinarily created on boot by the API. Seeding may run against a database
    # the API has never started against, and without a dataset the pipeline falls
    # back to the fixed schema and writes no citations.
    await seed_builtin_schemas(ExtractionSchemaRepository(session))

    cases = FNOLRepository(session)
    references = ReferenceRepository(session)
    audit = AuditService(AuditRepository(session))
    documents = DocumentProcessingService()
    cat_events = CatEventRepository(session)
    provider = get_ai_provider()

    ingestion = FNOLIngestionService(cases, references, audit)
    fnol_service = FNOLService(cases, audit, documents=documents, cat_events=cat_events)

    # The same assembly the API and the worker use.
    #
    # This used to build its own `FNOLPipeline` with ten collaborators listed by
    # hand, which meant the seeded cases silently skipped the four stages that
    # arrived with document intelligence: the notification body was never written
    # out as a document, nothing was chunked or indexed, the configured dataset
    # was never run, and no value carried the passage it was read from. The demo
    # data therefore could not exercise the screen the demo data exists for.
    # `build_pipeline` exists precisely so a scheduled run and an officer pressing
    # "reprocess" cannot diverge; seeding is a third caller and had diverged.
    pipeline = build_pipeline(
        session,
        provider=provider,
        embeddings=get_embedding_provider(),
        vectors=get_vector_store(),
    )

    created = 0
    for scenario in scenarios():
        if scenario["kind"] == "email":
            email: IncomingEmail = scenario["email"]
            notification = ingestion.from_email(email)
            attachments = email.attachments
        else:
            notification = scenario["notification"]
            attachments = notification.attachments

        case, is_new = await ingestion.ingest(notification, actor="Seed data")
        if not is_new:
            continue

        await ingestion.record_supplied_provenance(case, notification)
        for attachment in attachments:
            await fnol_service.attach_document(
                case,
                filename=attachment.filename,
                content=attachment.content,
                content_type=attachment.content_type,
                source="email_attachment",
                actor="Seed data",
            )

        await pipeline.run(case)
        # Committed per scenario so the duplicate scan in scenario B can actually
        # see scenario A — which is the whole point of scenario B.
        await session.commit()
        created += 1
        logger.info(
            "seed_scenario",
            scenario=scenario["key"],
            reference=case.reference,
            status=case.status,
        )

    return created


async def _convert_ready_cases(session: Any) -> int:
    """Convert every notice with nothing blocking it into a claim.

    A separate pass, run after all seven scenarios are ingested, for the reason
    the scenarios are committed one at a time: converting A the moment it is
    ingested would change what B's duplicate scan sees, and B exists precisely to
    come out as a possible duplicate of A. Claims are layered on top of the
    finished intake states rather than interleaved with them.

    Which notices convert is *asked*, not listed. `blockers()` is the same
    checklist the create-claim endpoint enforces, so the demo claims are exactly
    the ones an officer could have made by hand — and a scenario written to be
    blocked (E outside its policy period, F on fraud indicators, G with no policy
    to match) stays in the intake queue because the rules say so, not because a
    list here left it out.
    """
    cases = FNOLRepository(session)
    claims = ClaimRepository(session)
    handlers = HandlerRepository(session)
    references = ReferenceRepository(session)
    audit = AuditService(AuditRepository(session))

    creation = ClaimCreationService(
        fnol=cases,
        claims=claims,
        references=references,
        triage=TriageService(claims),
        assignment=AssignmentService(handlers, claims),
        audit=audit,
    )

    rows, _ = await cases.list_cases(limit=100)
    created = 0
    for case in rows:
        if case.claim_id is not None:
            continue

        blockers = await creation.blockers(case)
        if blockers:
            logger.info(
                "seed_claim_skipped",
                reference=case.reference,
                blockers=[blocker.code for blocker in blockers],
            )
            continue

        try:
            result = await creation.create(case.id, actor="Seed data")
        except ClaimCreationBlocked as exc:
            # Only reachable if the checklist changed under us. Recorded rather
            # than raised: one unconvertible notice must not cost the whole seed.
            logger.warning(
                "seed_claim_refused",
                reference=case.reference,
                blockers=[blocker.code for blocker in exc.blockers],
            )
            continue

        # Committed per claim for the same reason the scenarios are: the triage
        # and assignment rows are written inside this transaction, and a later
        # failure must not take an already-good claim down with it.
        await session.commit()
        created += 1
        logger.info(
            "seed_claim",
            reference=case.reference,
            claim=result.claim.reference,
            severity=result.claim.severity,
        )

    return created


def main() -> None:
    parser = argparse.ArgumentParser(description="Seed FNOL development data.")
    parser.add_argument(
        "--reset",
        action="store_true",
        help="Delete existing FNOL, claim and audit rows before seeding.",
    )
    args = parser.parse_args()
    asyncio.run(seed(reset=args.reset))


if __name__ == "__main__":
    main()
