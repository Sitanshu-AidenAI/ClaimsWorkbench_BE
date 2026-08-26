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
from app.models.reference_data import (
    CatEvent,
    Handler,
    Policy,
    PolicyLocation,
    ReferenceSequence,
)
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
        "insured_domain": "northline-logistics.co.uk",
        "broker_name": "Harding Vale Brokers",
        "broker_reference": "HVB/NL/0041",
        "broker_domain": "hardingvale.co.uk",
        "insurer_name": "Ardenmoor Insurance",
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
        # The policy that proves location matching works. Nine scheduled premises,
        # each with its own sum insured and its own excess, so a loss at number
        # seven is matched to number seven — and the candidate card shows *that*
        # location's excess rather than the policy's headline figure.
        "locations_scheduled": [
            (
                "Location 001",
                "Head office and national distribution centre",
                "Unit 7, Wakefield Road, Leeds LS9 8AA",
                "LS9 8AA",
                1_800_000_00,
                25_000_00,
                True,
            ),
            (
                "Location 002",
                "Cold store",
                "Unit 4, Gelderd Road, Leeds LS11 8AX",
                "LS11 8AX",
                900_000_00,
                50_000_00,
                False,
            ),
            (
                "Location 003",
                "Transhipment depot",
                "Airedale Depot, Keighley BD21 4LP",
                "BD21 4LP",
                450_000_00,
                10_000_00,
                False,
            ),
            (
                "Location 004",
                "Bonded warehouse",
                "Dock Street, Hull HU1 3DZ",
                "HU1 3DZ",
                620_000_00,
                10_000_00,
                False,
            ),
            (
                "Location 005",
                "Vehicle workshop",
                "Brookfoot Lane, Brighouse HD6 2RW",
                "HD6 2RW",
                310_000_00,
                5_000_00,
                False,
            ),
            (
                "Location 006",
                "Regional hub",
                "Parkway Industrial Estate, Sheffield S9 4WN",
                "S9 4WN",
                540_000_00,
                10_000_00,
                False,
            ),
            (
                "Location 007",
                "Ambient store",
                "Whitehouse Lane, Huddersfield HD2 1YJ",
                "HD2 1YJ",
                275_000_00,
                5_000_00,
                False,
            ),
            (
                "Location 008",
                "Cross-dock",
                "Europa Way, Doncaster DN11 0BF",
                "DN11 0BF",
                380_000_00,
                10_000_00,
                False,
            ),
            (
                "Location 009",
                "Overflow yard",
                "Kirkstall Road, Leeds LS4 2AZ",
                "LS4 2AZ",
                120_000_00,
                5_000_00,
                False,
            ),
        ],
    },
    {
        "policy_number": "POL-2026-0198",
        "insured_name": "Northline Logistics (Scotland) Limited",
        "insured_organisation": "Northline Logistics (Scotland) Limited",
        "insured_email": "scotland@northline-logistics.co.uk",
        "insured_domain": "northline-logistics.co.uk",
        "broker_name": "Harding Vale Brokers",
        "broker_reference": "HVB/NLS/0198",
        "broker_domain": "hardingvale.co.uk",
        "insurer_name": "Ardenmoor Insurance",
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
        "insured_domain": "ashfordfreight.co.uk",
        "broker_name": "Merridge & Co",
        "broker_reference": "MER/AF/7781",
        "broker_domain": "merridge.co.uk",
        "insurer_name": "Ardenmoor Insurance",
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
        "insured_domain": "solentmarine.com",
        "broker_name": "Ridgeway Marine",
        "broker_reference": "RM/SMC/3320",
        "broker_domain": "ridgewaymarine.com",
        "insurer_name": "Ardenmoor Insurance",
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
        "insured_domain": "calderwood-consulting.com",
        "broker_name": "Merridge & Co",
        "broker_reference": "MER/CC/5504",
        "broker_domain": "merridge.co.uk",
        "insurer_name": "Ardenmoor Insurance",
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
        "insured_domain": "verityhealth.io",
        "broker_name": "Harding Vale Brokers",
        "broker_reference": "HVB/VH/9120",
        "broker_domain": "hardingvale.co.uk",
        "insurer_name": "Ardenmoor Insurance",
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
    # -- The corporate group, and why name matching alone is not enough -------
    # A third Northline entity, on a different line of business, at a Leeds
    # address. Its whole job is to be wrong in a way that only the location and
    # the line of business can separate it from POL-2026-0041.
    {
        "policy_number": "POL-2026-0263",
        "insured_name": "Northline Property Holdings Ltd",
        "insured_organisation": "Northline Property Holdings Ltd",
        "insured_email": "estates@northline-holdings.co.uk",
        "insured_domain": "northline-holdings.co.uk",
        "broker_name": "Harding Vale Brokers",
        "broker_reference": "HVB/NPH/0263",
        "broker_domain": "hardingvale.co.uk",
        "insurer_name": "Ardenmoor Insurance",
        "policy_type": "Property Owners",
        "line_of_business": "liability",
        "status": "active",
        "effective_date": date(TODAY.year, 1, 1),
        "expiry_date": date(TODAY.year, 12, 31),
        "country": "United Kingdom",
        "region": "Yorkshire",
        "primary_location": "Carlton House, Wellington Street, Leeds LS1 4LT",
        "currency": "GBP",
        "limit_amount_minor": 10_000_000_00,
        "deductible_amount_minor": 5_000_00,
        "perils_covered": ["public liability", "property owners liability"],
        "exclusions": ["contractual liability", "terrorism"],
        "locations_scheduled": [
            (
                "Location 001",
                "Registered office",
                "Carlton House, Wellington Street, Leeds LS1 4LT",
                "LS1 4LT",
                2_400_000_00,
                5_000_00,
                True,
            ),
        ],
    },
    # -- The renewal chain ----------------------------------------------------
    # Last year's term for the same insured, linked from this year's by
    # `prior_policy_number` below. A loss discovered late falls in this policy and
    # the engine says so instead of calling it uninsured.
    {
        "policy_number": "CP-2025-30582",
        "insured_name": "Kelbrook Foods Limited",
        "insured_organisation": "Kelbrook Foods Limited",
        "insured_email": "accounts@kelbrookfoods.co.uk",
        "insured_domain": "kelbrookfoods.co.uk",
        "broker_name": "Thurlow Beckett",
        "broker_reference": "TB/KF/2025",
        "broker_domain": "thurlowbeckett.co.uk",
        "insurer_name": "Ardenmoor Insurance",
        "policy_type": "Commercial Combined",
        "line_of_business": "property",
        "status": "lapsed",
        "effective_date": date(TODAY.year - 1, 4, 1),
        "expiry_date": date(TODAY.year, 3, 31),
        "country": "United Kingdom",
        "region": "Lancashire",
        "primary_location": "Sough Lane, Barnoldswick BB18 5NX",
        "currency": "GBP",
        "limit_amount_minor": 4_000_000_00,
        "deductible_amount_minor": 15_000_00,
        "perils_covered": ["fire", "flood", "escape of water", "impact"],
        "exclusions": ["wear and tear", "terrorism"],
        "locations_scheduled": [
            (
                "Location 001",
                "Bakery and cold store",
                "Sough Lane, Barnoldswick BB18 5NX",
                "BB18 5NX",
                3_200_000_00,
                15_000_00,
                True,
            ),
        ],
    },
    {
        "policy_number": "CP-2026-30582",
        "insured_name": "Kelbrook Foods Limited",
        "insured_organisation": "Kelbrook Foods Limited",
        "insured_email": "accounts@kelbrookfoods.co.uk",
        "insured_domain": "kelbrookfoods.co.uk",
        "broker_name": "Thurlow Beckett",
        "broker_reference": "TB/KF/2026",
        "broker_domain": "thurlowbeckett.co.uk",
        "insurer_name": "Ardenmoor Insurance",
        "policy_type": "Commercial Combined",
        "line_of_business": "property",
        "status": "active",
        "effective_date": date(TODAY.year, 4, 1),
        "expiry_date": date(TODAY.year + 1, 3, 31),
        "country": "United Kingdom",
        "region": "Lancashire",
        "primary_location": "Sough Lane, Barnoldswick BB18 5NX",
        "currency": "GBP",
        "limit_amount_minor": 4_500_000_00,
        "deductible_amount_minor": 20_000_00,
        "perils_covered": ["fire", "flood", "escape of water", "impact", "theft"],
        "exclusions": ["wear and tear", "terrorism"],
        "prior_policy_number": "CP-2025-30582",
        "locations_scheduled": [
            (
                "Location 001",
                "Bakery and cold store",
                "Sough Lane, Barnoldswick BB18 5NX",
                "BB18 5NX",
                3_600_000_00,
                20_000_00,
                True,
            ),
            (
                "Location 002",
                "Chilled distribution",
                "Skipton Road, Colne BB8 7DR",
                "BB8 7DR",
                900_000_00,
                10_000_00,
                False,
            ),
        ],
    },
    # -- The number decoy ----------------------------------------------------
    # One digit from CP-2026-30582, a different insured, a different broker. It is
    # what makes an exact-versus-near-miss distinction worth drawing at all.
    {
        "policy_number": "CP-2026-30583",
        "insured_name": "Kelbridge Plant Hire Ltd",
        "insured_organisation": "Kelbridge Plant Hire Ltd",
        "insured_email": "office@kelbridgeplant.co.uk",
        "insured_domain": "kelbridgeplant.co.uk",
        "broker_name": "Merridge & Co",
        "broker_reference": "MER/KPH/0583",
        "broker_domain": "merridge.co.uk",
        "insurer_name": "Ardenmoor Insurance",
        "policy_type": "Commercial Combined",
        "line_of_business": "property",
        "status": "active",
        "effective_date": date(TODAY.year, 2, 1),
        "expiry_date": date(TODAY.year + 1, 1, 31),
        "country": "United Kingdom",
        "region": "Lancashire",
        "primary_location": "Fence Gate Works, Nelson BB9 0PT",
        "currency": "GBP",
        "limit_amount_minor": 2_000_000_00,
        "deductible_amount_minor": 10_000_00,
        "perils_covered": ["fire", "storm", "theft"],
        "exclusions": ["wear and tear"],
    },
    # -- Construction: the project is the risk -------------------------------
    # Joint names, a contract number, a works period and a twelve-month defects
    # liability period after practical completion. Every one of those is a signal
    # a property-shaped matcher has no way to read.
    {
        "policy_number": "CAR-2026-4417",
        "insured_name": "Bellhaven Construction Limited",
        "insured_organisation": "Bellhaven Construction Limited",
        "insured_email": "insurance@bellhaven.build",
        "insured_domain": "bellhaven.build",
        "broker_name": "Thurlow Beckett",
        "broker_reference": "TB/BC/4417",
        "broker_domain": "thurlowbeckett.co.uk",
        "insurer_name": "Ardenmoor Insurance",
        "policy_type": "Contractors All Risks",
        "line_of_business": "construction",
        "status": "active",
        "effective_date": date(TODAY.year - 1, 9, 1),
        "expiry_date": date(TODAY.year, 6, 30),
        "country": "United Kingdom",
        "region": "Greater Manchester",
        "primary_location": "Bellhaven House, Talbot Road, Manchester M16 0PG",
        "site_address": "Riverside Quarter Phase 2, Water Street, Manchester M3 4JU",
        "project_name": "Riverside Quarter Phase 2",
        "project_reference": "ARD/PROJ/2026/017",
        "contract_number": "RQ2-JCT-2025-0884",
        "principal_name": "Waterline Regeneration LLP",
        "contractor_name": "Bellhaven Construction Limited",
        "practical_completion_date": date(TODAY.year, 6, 30),
        "maintenance_period_months": 12,
        "currency": "GBP",
        "limit_amount_minor": 18_500_000_00,
        "deductible_amount_minor": 50_000_00,
        "perils_covered": [
            "damage to the works",
            "storm",
            "flood",
            "fire",
            "theft of materials",
            "third party property damage",
        ],
        "exclusions": ["defective design", "consequential loss", "war"],
        "locations_scheduled": [
            (
                "Site",
                "The works — Riverside Quarter Phase 2",
                "Riverside Quarter Phase 2, Water Street, Manchester M3 4JU",
                "M3 4JU",
                18_500_000_00,
                50_000_00,
                True,
            ),
            (
                "Store",
                "Off-site materials store",
                "Pomona Strand, Manchester M15 4LX",
                "M15 4LX",
                750_000_00,
                25_000_00,
                False,
            ),
        ],
    },
    {
        "policy_number": "CAR-2026-4482",
        "insured_name": "Marchmont Civils Ltd",
        "insured_organisation": "Marchmont Civils Ltd",
        "insured_email": "claims@marchmontcivils.co.uk",
        "insured_domain": "marchmontcivils.co.uk",
        "broker_name": "Thurlow Beckett",
        "broker_reference": "TB/MC/4482",
        "broker_domain": "thurlowbeckett.co.uk",
        "insurer_name": "Ardenmoor Insurance",
        "policy_type": "Erection All Risks",
        "line_of_business": "engineering",
        "status": "active",
        "effective_date": date(TODAY.year, 2, 1),
        "expiry_date": date(TODAY.year + 1, 5, 31),
        "country": "United Kingdom",
        "region": "West Midlands",
        "primary_location": "Marchmont Yard, Tyburn Road, Birmingham B24 8HJ",
        "site_address": "Gasworks Lane Regeneration, Gasworks Lane, Wolverhampton WV1 3PT",
        "project_name": "Gasworks Lane Regeneration",
        "project_reference": "ARD/PROJ/2026/031",
        "contract_number": "GLR/NEC4/0221",
        "principal_name": "Wolverhampton Development Company",
        "contractor_name": "Marchmont Civils Ltd",
        "maintenance_period_months": 24,
        "currency": "GBP",
        "limit_amount_minor": 9_250_000_00,
        "deductible_amount_minor": 35_000_00,
        "perils_covered": [
            "damage to the works",
            "storm",
            "collapse",
            "third party property damage",
        ],
        "exclusions": ["defective workmanship", "penalties"],
        "locations_scheduled": [
            (
                "Site",
                "The works — Gasworks Lane",
                "Gasworks Lane Regeneration, Gasworks Lane, Wolverhampton WV1 3PT",
                "WV1 3PT",
                9_250_000_00,
                35_000_00,
                True,
            ),
        ],
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
    # --- United States -----------------------------------------------------
    #
    # Absolute dates, unlike the two above. Deliberately: these three exist to be
    # matched by the `case_data` packs, and those packs state their dates in full in
    # `scripts/fnol_scenarios.py`. A `TODAY - timedelta(...)` window would drift past
    # them the day after it was written, which is how the seed came to hold two UK
    # events and three US fixtures built specifically to exercise catastrophe
    # attribution that could never match either of them.
    {
        "reference": "CAT-2026-011",
        "name": "Arctic outbreak — Delaware Valley hard freeze",
        "event_type": "freeze",
        "perils": ["freeze", "winter_storm"],
        "severity": "major",
        "status": "closed",
        # `harborline-delaware-freeze`: 419 Delaware Terminal Road, New Castle, DE,
        # 1 February 2026, frozen sprinkler branch line.
        "start_date": date(2026, 1, 30),
        "end_date": date(2026, 2, 3),
        "country": "United States",
        "region": "Delaware",
        "affected_areas": [
            "New Castle County",
            "New Castle",
            "Wilmington",
            "Newark",
            "Delaware City",
            "Philadelphia",
        ],
        "latitude": 39.6620,
        "longitude": -75.5660,
        "radius_km": 120,
    },
    {
        "reference": "CAT-2026-012",
        "name": "Hail and straight-line wind — northeastern Oklahoma",
        "event_type": "hail",
        "perils": ["hail", "storm", "derecho"],
        "severity": "major",
        "status": "closed",
        # `windrow-grove-hail`: 6120 East 91st Street, Tulsa, OK, 19 April 2026.
        "start_date": date(2026, 4, 18),
        "end_date": date(2026, 4, 20),
        "country": "United States",
        "region": "Oklahoma",
        "affected_areas": [
            "Tulsa County",
            "Tulsa",
            "Broken Arrow",
            "Bixby",
            "Jenks",
            "Owasso",
            "Wagoner County",
        ],
        "latitude": 36.0760,
        "longitude": -95.8800,
        "radius_km": 110,
    },
    {
        "reference": "CAT-2026-013",
        "name": "Haboob and wet microburst — Phoenix metropolitan area",
        "event_type": "haboob",
        "perils": ["haboob", "microburst", "storm"],
        "severity": "moderate",
        "status": "open",
        # `cypress-landing-haboob`: 14900 South Cypress Landing Way, Mesa, AZ,
        # 7 July 2026, windstorm — haboob followed by a wet microburst.
        "start_date": date(2026, 7, 6),
        "end_date": date(2026, 7, 8),
        "country": "United States",
        "region": "Arizona",
        "affected_areas": [
            "Maricopa County",
            "Mesa",
            "Phoenix",
            "Gilbert",
            "Chandler",
            "Tempe",
            "Queen Creek",
            "Pinal County",
        ],
        "latitude": 33.3060,
        "longitude": -111.6410,
        "radius_km": 90,
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
    """The demonstration notifications, as their sources.

    Written as raw broker emails and call notes and run through the real pipeline,
    never as derived values: a fixture that writes `policy_id` directly decorates
    the screen instead of testing the system behind it.

    A to G exercise reading a notice, citing it and assessing it. H to L exercise
    *identifying the policy*, and each is built around one signal the engine has to
    get right — a broker's own reference in place of a policy number, a scheduled
    location, a corporate group, a construction contract, and a loss discovered
    after the policy it belongs to has renewed.
    """
    loss_a = TODAY - timedelta(days=6)
    loss_c = TODAY - timedelta(days=3)
    loss_d = TODAY - timedelta(days=7)
    loss_e = TODAY - timedelta(days=12)
    loss_f = TODAY - timedelta(days=8)
    loss_h = TODAY - timedelta(days=4)
    loss_i = TODAY - timedelta(days=9)
    loss_j = TODAY - timedelta(days=2)
    loss_k = TODAY - timedelta(days=11)
    # Three weeks before this year's term began, so it falls in the prior policy.
    loss_l = date(TODAY.year, 3, 10)

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
        # -- H. The broker quotes their own reference, not ours -------------
        # The commonest real shape of a notice that a policy-number-only matcher
        # cannot resolve: brokers quote the reference their own system prints.
        {
            "key": "H",
            "kind": "email",
            "email": IncomingEmail(
                sender="claims@thurlowbeckett.co.uk",
                recipient="fnol@carrier.com",
                subject="New claim - our ref TB/KF/2026 - escape of water, Barnoldswick",
                message_id="<tb-kf-2026-h@thurlowbeckett.co.uk>",
                received_at=_days_ago(3),
                from_broker=True,
                body=f"""Dear Claims Team

Please open a claim for our client under the above scheme.

Our reference: TB/KF/2026
Insured: Kelbrook Foods Limited
Broker: Thurlow Beckett
Reported by: Marcus Ryle
Role: Claims Broker
Contact email: m.ryle@thurlowbeckett.co.uk
Phone: +44 1282 447 118

I am afraid I do not have your policy number to hand — our client's schedule is
with their finance team — but the risk is the bakery and cold store at Sough Lane.

Date of loss: {loss_h.strftime("%d/%m/%Y")}
Location: Sough Lane, Barnoldswick BB18 5NX
Postcode: BB18 5NX
Country: United Kingdom
Cause: escape of water
Description: A chilled water pipe above the packing hall failed overnight and
discharged for several hours before it was found. Water has reached the packing
line, the ambient store and the ground floor offices. Production has stopped.

Affected property: packing hall floor and ceiling, packing line, ambient stock
Injuries: none
Estimated loss: GBP 210,000

Kind regards
Marcus Ryle
Thurlow Beckett
""",
            ),
        },
        # -- I. A policy number damaged by OCR, resolved by the schedule -----
        # The number was read off a scanned claim form, so `0` became `O`. What
        # confirms it is the location: a loss at the second of nine scheduled
        # premises, which a matcher comparing against a head office would miss.
        {
            "key": "I",
            "kind": "email",
            "email": IncomingEmail(
                sender="claims@hardingvale.co.uk",
                recipient="fnol@carrier.com",
                subject="FNOL - Northline Logistics - refrigeration failure, Gelderd Road",
                message_id="<hvb-fnol-ocr-i@hardingvale.co.uk>",
                received_at=_days_ago(8),
                from_broker=True,
                body=f"""Dear Claims Team

First notification of loss, details taken from the insured's completed claim
form which is scanned below.

Policy number: P0L-2O26-OO41
Our reference: HVB/NL/0041
Insured: Northline Logistics Ltd
Broker: Harding Vale Brokers
Reported by: Elaine Prosser
Role: Account Handler
Contact email: e.prosser@hardingvale.co.uk

Date of loss: {loss_i.strftime("%d/%m/%Y")}
Location: Unit 4, Gelderd Road, Leeds LS11 8AX
Postcode: LS11 8AX
Country: United Kingdom
Cause: refrigeration breakdown
Description: The compressor on the cold store at Unit 4 failed at some point over
the weekend and was found on Monday morning. The store was holding chilled
product which has had to be condemned. The plant itself is also damaged.

Affected property: cold store compressor, chilled stock
Injuries: none
Estimated loss: GBP 96,500

Kind regards
Elaine Prosser
Harding Vale Brokers
""",
            ),
        },
        # -- J. The corporate group ----------------------------------------
        # Names the client loosely and quotes no reference at all. Two real
        # entities on two real policies fit, and the officer has to choose.
        {
            "key": "J",
            "kind": "email",
            "email": IncomingEmail(
                sender="claims@hardingvale.co.uk",
                recipient="fnol@carrier.com",
                subject="Northline - impact damage, please log",
                message_id="<hvb-fnol-group-j@hardingvale.co.uk>",
                received_at=_days_ago(1),
                from_broker=True,
                body=f"""Hello

Please log the following. I am checking with the client which of their companies
this site sits under and will confirm.

Insured: Northline Logistics
Broker: Harding Vale Brokers
Reported by: Elaine Prosser
Contact email: e.prosser@hardingvale.co.uk

Date of loss: {loss_j.strftime("%d/%m/%Y")}
Location: the yard, Leeds
Country: United Kingdom
Cause: impact
Description: A visiting curtain-sider reversed into the loading dock canopy and
brought down two of the support posts. Nobody was hurt. The dock is out of use
until it has been propped.

Affected property: loading dock canopy and support posts
Injuries: none
Estimated loss: GBP 34,000

Kind regards
Elaine Prosser
""",
            ),
        },
        # -- K. Construction, and reported by a party who is not the insured -
        # The employer notifies on a policy written in joint names. Name matching
        # against the named insured fails; the project and the contract resolve it.
        {
            "key": "K",
            "kind": "email",
            "email": IncomingEmail(
                sender="insurance@waterline-regeneration.co.uk",
                recipient="fnol@carrier.com",
                subject="Riverside Quarter Phase 2 - storm damage to the works",
                message_id="<wr-rq2-k@waterline-regeneration.co.uk>",
                received_at=_days_ago(10),
                from_broker=False,
                body=f"""Dear Claims Team

We are the employer under the contract below and are notifying damage to the
works following Saturday night's storm. Our contractor is aware and has made the
area safe.

Project: Riverside Quarter Phase 2
Contract number: RQ2-JCT-2025-0884
Insured: Waterline Regeneration LLP
Main contractor: Bellhaven Construction Limited
Broker: Thurlow Beckett
Reported by: Dilys Amankwah
Role: Development Director
Contact email: d.amankwah@waterline-regeneration.co.uk
Phone: +44 161 445 9022

Date of loss: {loss_k.strftime("%d/%m/%Y")}
Site address: Riverside Quarter Phase 2, Water Street, Manchester M3 4JU
Postcode: M3 4JU
Country: United Kingdom
Cause: storm
Description: Overnight winds lifted a section of the temporary roof over blocks C
and D, and rain then entered the completed floors below. Plasterboard, joinery
and the newly laid screed on levels three and four are affected, and the tower
crane has been stood down pending inspection. Practical completion is likely to
slip.

Affected property: temporary roof, screed and joinery to levels three and four
Injuries: none
Estimated loss: GBP 480,000

Kind regards
Dilys Amankwah
Waterline Regeneration LLP
""",
            ),
        },
        # -- L. Discovered late, and it belongs to last year's policy --------
        # The broker quotes the current number in good faith. The loss predates
        # this term, and the useful answer is the prior policy rather than
        # "outside the period".
        {
            "key": "L",
            "kind": "email",
            "email": IncomingEmail(
                sender="claims@thurlowbeckett.co.uk",
                recipient="fnol@carrier.com",
                subject="Late notification - Kelbrook Foods - CP-2026-30582",
                message_id="<tb-kf-late-l@thurlowbeckett.co.uk>",
                received_at=_days_ago(2),
                from_broker=True,
                body=f"""Dear Claims Team

Our client has only just established the extent of this and has asked us to
notify it now. I appreciate the delay.

Policy number: CP-2026-30582
Our reference: TB/KF/2026
Insured: Kelbrook Foods Limited
Broker: Thurlow Beckett
Reported by: Marcus Ryle
Contact email: m.ryle@thurlowbeckett.co.uk

Date of loss: {loss_l.strftime("%d/%m/%Y")}
Location: Sough Lane, Barnoldswick BB18 5NX
Postcode: BB18 5NX
Country: United Kingdom
Cause: impact
Description: A delivery vehicle struck the loading bay wall in March. The damage
looked cosmetic at the time and was not reported. A structural engineer has now
advised that the panel has moved and the bay has had to be closed.

Affected property: loading bay wall panel and door track
Injuries: none
Estimated loss: GBP 62,000

Kind regards
Marcus Ryle
Thurlow Beckett
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
                # Reference data too, and policies in particular. `_seed_reference_data`
                # is idempotent by natural key, which means a policy row that already
                # exists is left exactly as it was — so a book that gained a schedule of
                # locations or a renewal link since the last run would never acquire one
                # on a developer's machine. `--reset` means rebuild, so it rebuilds.
                # `policy_locations` goes with them by cascade.
                await session.execute(delete(Policy))
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
    """Insert reference rows that are not already there, keyed by their natural id.

    Policies come in two passes because two of their columns point at other rows.
    The schedule of locations is written as child rows in the first pass, and the
    renewal chain is linked in the second — `prior_policy_id` cannot be set until
    the policy it names exists, and expressing that in the literal above would mean
    putting ids in a fixture file.
    """
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
        if existing is not None:
            continue

        fields = dict(payload)
        schedule = fields.pop("locations_scheduled", [])
        fields.pop("prior_policy_number", None)
        policy = Policy(**fields)
        # `locations` keeps the raw JSON form a synchronising integration would
        # land in, derived from the schedule rather than written twice.
        policy.locations = [
            {
                "location_ref": ref,
                "description": description,
                "address": address,
                "postcode": postcode,
                "sum_insured_minor": sum_insured,
                "deductible_minor": deductible,
                "is_primary": is_primary,
            }
            for ref, description, address, postcode, sum_insured, deductible, is_primary in schedule
        ]
        policy.locations_scheduled = [
            PolicyLocation(
                location_ref=ref,
                description=description,
                address=address,
                postcode=postcode,
                country=payload.get("country"),
                sum_insured_minor=sum_insured,
                deductible_minor=deductible,
                is_primary=is_primary,
            )
            for ref, description, address, postcode, sum_insured, deductible, is_primary in schedule
        ]
        session.add(policy)

    await session.flush()

    for payload in POLICIES:
        prior_number = payload.get("prior_policy_number")
        if not prior_number:
            continue
        rows = (
            (
                await session.execute(
                    select(Policy).where(
                        Policy.policy_number.in_([payload["policy_number"], prior_number])
                    )
                )
            )
            .scalars()
            .all()
        )
        by_number = {row.policy_number: row for row in rows}
        current = by_number.get(payload["policy_number"])
        prior = by_number.get(prior_number)
        if current is not None and prior is not None and current.prior_policy_id is None:
            current.prior_policy_id = prior.id

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
