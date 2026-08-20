"""The FNOL business rules, tested without a database or a model provider.

Everything in `app.domain` is a pure function over plain objects, which is the
whole reason it lives there: these tests are the ones that have to keep working
when the language model, the schema and the API all change around them.

Policy identification is tested in `test_policy_identification.py` rather than
here. It moved out with the engine: the tests it needs are about signal outcomes,
confidence bands and the wording of reasons rather than about a score, and they
are worth reading as a group.
"""

from __future__ import annotations

from datetime import UTC, date, datetime, timedelta
from types import SimpleNamespace

import pytest

from app.core.config import FNOLSettings
from app.domain import assessment, catastrophe, duplicates, matching, normalisation
from app.domain import triage as triage_rules
from app.domain.enums import (
    CoverageIndicator,
    ExceptionCode,
    FNOLStatus,
    LineOfBusiness,
    Priority,
    RiskLevel,
    Severity,
    TriageCategory,
)
from app.domain.heuristics import classify_from_text, extract_from_text
from app.domain.lifecycle import blocking_codes, can_transition, derive_status
from app.domain.references import format_reference, is_claim_reference, parse_reference
from app.domain.rules import required_fields, valid_loss_type

CONFIG = FNOLSettings()
NOW = datetime.now(UTC)


def make_case(**overrides: object) -> SimpleNamespace:
    """A case with every attribute the rules read, defaulted to absent."""
    base: dict[str, object] = {
        "id": "case-1",
        "reference": "FNOL-2026-000001",
        "received_at": NOW,
        "policy_number": None,
        "policy_id": None,
        "policy_confirmed": False,
        "insured_name": None,
        "insured_organisation": None,
        "reporter_name": None,
        "reporter_organisation": None,
        "reporter_email": None,
        "line_of_business": None,
        "loss_type": None,
        "date_of_loss": None,
        "loss_location": None,
        "loss_country": None,
        "loss_latitude": None,
        "loss_longitude": None,
        "loss_description": None,
        "cause_of_loss": None,
        "affected_assets": None,
        "injuries": None,
        "fatalities": None,
        "business_interruption": False,
        "structural_damage": False,
        "environmental_exposure": False,
        "potential_litigation": False,
        "estimated_loss_minor": None,
        "repair_estimate_minor": None,
        "currency": "GBP",
        "police_reference": None,
        "incident_reference": None,
        "authorities_involved": None,
        "external_reference": None,
        "source_body": None,
        "severity": None,
        "severity_overridden": False,
        "completeness_score": None,
    }
    base.update(overrides)
    return SimpleNamespace(**base)


def make_policy(**overrides: object) -> SimpleNamespace:
    base: dict[str, object] = {
        "id": "policy-1",
        "policy_number": "POL-2026-0041",
        "insured_name": "Northline Logistics Limited",
        "insured_organisation": "Northline Logistics Limited",
        "insured_email": "operations@northline-logistics.co.uk",
        "broker_name": "Harding Vale Brokers",
        "line_of_business": "property",
        "status": "active",
        "effective_date": date(2026, 1, 1),
        "expiry_date": date(2026, 12, 31),
        "primary_location": "Unit 7, Wakefield Road, Leeds LS9 8AA",
        "region": "Yorkshire",
        "country": "United Kingdom",
        "currency": "GBP",
        "limit_amount_minor": 5_000_000_00,
        "deductible_amount_minor": 25_000_00,
        "perils_covered": ["fire", "flood", "storm"],
        "exclusions": ["terrorism", "war"],
        "locations": [],
        "latitude": 53.7965,
        "longitude": -1.5210,
    }
    base.update(overrides)
    return SimpleNamespace(**base)


# ---------------------------------------------------------------------------
# Normalisation — the layer that refuses
# ---------------------------------------------------------------------------


class TestNormalisation:
    @pytest.mark.parametrize(
        ("value", "expected"),
        [
            ("07/08/2026", date(2026, 8, 7)),
            ("2026-08-07", date(2026, 8, 7)),
            ("7 August 2026", date(2026, 8, 7)),
            ("7th Aug 2026", date(2026, 8, 7)),
            ("not a date", None),
            ("2026-13-45", None),
        ],
    )
    def test_parses_the_forms_a_broker_writes(self, value: str, expected: date | None) -> None:
        assert normalisation.parse_date(value) == expected

    def test_refuses_a_loss_date_in_the_future(self) -> None:
        future = (datetime.now(UTC) + timedelta(days=30)).strftime("%d/%m/%Y")
        assert normalisation.parse_datetime(future) is None
        assert normalisation.is_future_date(future) is True

    @pytest.mark.parametrize(
        ("value", "expected"),
        [
            ("GBP 128,000", 128_000_00),
            ("£430,000", 430_000_00),
            ("2.4m", 2_400_000_00),
            ("40k", 40_000_00),
            ("-40000", None),
            ("no idea", None),
        ],
    )
    def test_money_is_minor_units_and_never_negative(
        self, value: str, expected: int | None
    ) -> None:
        assert normalisation.parse_money_minor(value) == expected

    def test_counts_read_none_as_zero_and_refuse_nonsense(self) -> None:
        assert normalisation.parse_count("none") == 0
        assert normalisation.parse_count("2 people") == 2
        assert normalisation.parse_count("lots") is None

    def test_a_line_of_business_the_carrier_does_not_write_is_refused(self) -> None:
        assert normalisation.parse_line_of_business("property") is LineOfBusiness.PROPERTY
        assert normalisation.parse_line_of_business("Workers Comp") is (
            LineOfBusiness.WORKERS_COMPENSATION
        )
        assert normalisation.parse_line_of_business("Space Tourism") is None

    def test_a_loss_type_outside_the_line_is_dropped(self) -> None:
        assert valid_loss_type(LineOfBusiness.PROPERTY, "fire") == "fire"
        assert valid_loss_type(LineOfBusiness.PROPERTY, "ransomware") is None

    @pytest.mark.parametrize(
        ("value", "expected"),
        [
            # The UK book. A missing space is tolerated here because the field
            # itself says the value is a postcode.
            ("LS11 8AX", "LS11 8AX"),
            ("ls118ax", "LS11 8AX"),
            ("BB18 5NX", "BB18 5NX"),
            # The US book. The +4 add-on segments a delivery route inside one ZIP
            # rather than naming a different place, so it is dropped — otherwise
            # two addresses across the street compare unequal.
            ("21226", "21226"),
            ("21226-1234", "21226"),
            ("MD 21226", "21226"),
            ("2870 Patapsco Industrial Parkway, Baltimore, MD 21226", "21226"),
            # Neither, and stored as nothing rather than as a guess.
            ("", None),
            ("not a postcode", None),
            ("12 High Street", None),
        ],
    )
    def test_a_postcode_is_read_on_both_books(self, value: str, expected: str | None) -> None:
        """A US ZIP is a postcode, and dropping it costs the location signal.

        `loss_postcode` is the highest-weighted location signal in policy
        identification, and it is compared exactly. Reading only UK postcodes left
        it empty on every US notice, so the whole axis scored nothing on half the
        book while the extraction had read the value correctly all along.
        """
        assert normalisation.parse_postcode(value) == expected


# ---------------------------------------------------------------------------
# Matching primitives
# ---------------------------------------------------------------------------


class TestMatching:
    def test_company_forms_do_not_make_two_names(self) -> None:
        assert (
            matching.name_similarity("Northline Logistics Ltd", "Northline Logistics Limited")
            == 1.0
        )

    def test_reference_punctuation_is_formatting_not_identity(self) -> None:
        assert matching.normalise_reference("POL-2026/0041") == matching.normalise_reference(
            "pol 2026 0041"
        )

    def test_uncomparable_signals_do_not_drag_a_score_down(self) -> None:
        # -1 means "neither side had the data"; it must be ignored rather than
        # counted as a zero.
        assert matching.weighted_score([(1.0, 2.0), (-1.0, 5.0)]) == 1.0

    def test_distance_is_none_when_either_point_is_unknown(self) -> None:
        assert matching.haversine_km(53.8, -1.5, None, None) is None
        leeds_to_york = matching.haversine_km(53.8008, -1.5491, 53.9599, -1.0873)
        assert leeds_to_york is not None
        assert 30 < leeds_to_york < 45


# ---------------------------------------------------------------------------
# Completeness
# ---------------------------------------------------------------------------


class TestCompleteness:
    def test_required_fields_depend_on_the_line_of_business(self) -> None:
        motor = {field.path for field in required_fields(LineOfBusiness.MOTOR)}
        property_ = {field.path for field in required_fields(LineOfBusiness.PROPERTY)}
        assert "additional.police_reference" in motor
        assert "additional.police_reference" not in property_

    def test_the_score_reflects_named_fields_rather_than_a_guess(self) -> None:
        empty = assessment.assess_completeness(
            make_case(),
            document_count=0,
            party_roles=set(),
            field_confidences={},
            config=CONFIG,
        )
        assert empty.score == 0.0
        assert {status.path for status in empty.missing_critical} >= {
            "policy.policy_number",
            "loss.date_of_loss",
            "loss.loss_description",
        }

    def test_a_low_confidence_value_counts_as_uncertain_not_present(self) -> None:
        case = make_case(policy_number="POL-2026-0041")
        result = assessment.assess_completeness(
            case,
            document_count=0,
            party_roles=set(),
            field_confidences={"policy.policy_number": 0.2},
            config=CONFIG,
        )
        assert [status.path for status in result.uncertain] == ["policy.policy_number"]
        assert "policy.policy_number" not in {status.path for status in result.present}

    def test_a_loss_dated_after_the_notice_is_a_conflict_not_a_gap(self) -> None:
        case = make_case(date_of_loss=NOW + timedelta(days=2), received_at=NOW)
        result = assessment.assess_completeness(
            case, document_count=0, party_roles=set(), field_confidences={}, config=CONFIG
        )
        assert [status.path for status in result.conflicting] == ["loss.date_of_loss"]


# ---------------------------------------------------------------------------
# Severity
# ---------------------------------------------------------------------------


class TestSeverity:
    def test_money_sets_the_band(self) -> None:
        assert (
            assessment.assess_severity(
                make_case(estimated_loss_minor=5_000_00), config=CONFIG
            ).severity
            is Severity.LOW
        )
        assert (
            assessment.assess_severity(
                make_case(estimated_loss_minor=2_000_000_00), config=CONFIG
            ).severity
            is Severity.CRITICAL
        )

    def test_a_fatality_outranks_a_small_estimate(self) -> None:
        result = assessment.assess_severity(
            make_case(estimated_loss_minor=5_000_00, fatalities=1), config=CONFIG
        )
        assert result.severity is Severity.CRITICAL
        assert any(factor.key == "fatalities" for factor in result.factors)

    def test_no_estimate_bands_provisionally_and_says_so(self) -> None:
        result = assessment.assess_severity(make_case(), config=CONFIG)
        assert result.severity is Severity.MEDIUM
        assert result.confidence < 0.5
        assert any(factor.key == "no_estimate" for factor in result.factors)


# ---------------------------------------------------------------------------
# Fraud indicators
# ---------------------------------------------------------------------------


class TestFraudIndicators:
    def test_no_signals_is_low_with_nothing_to_show(self) -> None:
        result = assessment.assess_fraud_indicators(
            make_case(loss_description="A detailed account of what happened on the night."),
            config=CONFIG,
            policy_effective=None,
            policy_expiry=None,
            duplicate_count=0,
            conflicting_fields=0,
            human_corrections=0,
            unreadable_documents=0,
        )
        assert result.level is RiskLevel.LOW
        assert result.indicators == []

    def test_a_loss_days_after_inception_is_an_indicator(self) -> None:
        loss = datetime.now(UTC) - timedelta(days=5)
        result = assessment.assess_fraud_indicators(
            make_case(date_of_loss=loss),
            config=CONFIG,
            policy_effective=(loss - timedelta(days=3)).date(),
            policy_expiry=(loss + timedelta(days=360)).date(),
            duplicate_count=0,
            conflicting_fields=0,
            human_corrections=0,
            unreadable_documents=0,
        )
        assert {indicator.code for indicator in result.indicators} == {"loss_near_inception"}

    def test_weak_signals_accumulate_without_reaching_certainty(self) -> None:
        result = assessment.assess_fraud_indicators(
            make_case(),
            config=CONFIG,
            policy_effective=None,
            policy_expiry=None,
            duplicate_count=2,
            conflicting_fields=2,
            human_corrections=6,
            unreadable_documents=3,
        )
        assert 0.0 < result.score < 1.0
        assert result.level in (RiskLevel.MEDIUM, RiskLevel.HIGH)


# ---------------------------------------------------------------------------
# Coverage indicators
# ---------------------------------------------------------------------------


class TestCoverage:
    def test_no_policy_is_stated_as_such(self) -> None:
        result = assessment.assess_coverage(make_case(), None, policy_confirmed=False)
        assert result.indicator is CoverageIndicator.POLICY_NOT_LOCATED

    def test_a_loss_outside_the_period_is_a_possible_exclusion(self) -> None:
        case = make_case(date_of_loss=datetime(2025, 8, 3, tzinfo=UTC), cause_of_loss="fire")
        result = assessment.assess_coverage(case, make_policy(), policy_confirmed=True)
        assert result.indicator is CoverageIndicator.POSSIBLE_EXCLUSION
        assert "outside the policy period" in result.reasoning

    def test_an_exclusion_is_matched_on_words_not_characters(self) -> None:
        # "war" inside "ransomware" must not decline a covered cyber claim.
        case = make_case(
            date_of_loss=datetime(2026, 8, 3, tzinfo=UTC),
            cause_of_loss="ransomware",
            loss_description="Systems encrypted by ransomware.",
            loss_location="Unit 7, Wakefield Road, Leeds LS9 8AA",
            estimated_loss_minor=100_000_00,
        )
        policy = make_policy(perils_covered=["ransomware"], exclusions=["war"])
        result = assessment.assess_coverage(case, policy, policy_confirmed=True)
        assert result.indicator is not CoverageIndicator.POSSIBLE_EXCLUSION

    def test_an_unconfirmed_match_can_never_read_as_likely_covered(self) -> None:
        case = make_case(
            date_of_loss=datetime(2026, 8, 3, tzinfo=UTC),
            cause_of_loss="fire",
            loss_location="Unit 7, Wakefield Road, Leeds LS9 8AA",
            estimated_loss_minor=100_000_00,
        )
        result = assessment.assess_coverage(case, make_policy(), policy_confirmed=False)
        assert result.indicator is CoverageIndicator.REVIEW_REQUIRED


# ---------------------------------------------------------------------------
# Duplicates
# ---------------------------------------------------------------------------


class TestDuplicates:
    def test_the_same_loss_reported_twice_scores_highly(self) -> None:
        first = make_case(
            policy_number="POL-2026-0041",
            insured_name="Northline Logistics Ltd",
            date_of_loss=datetime(2026, 8, 3, tzinfo=UTC),
            loss_location="Unit 7, Wakefield Road, Leeds LS9 8AA",
            loss_description="Overnight fire in bay 3 of the warehouse at the charging point.",
        )
        second = make_case(
            policy_number="POL 2026 0041",
            insured_name="Northline Logistics Limited",
            date_of_loss=datetime(2026, 8, 3, 4, 20, tzinfo=UTC),
            loss_location="Wakefield Road, Leeds LS9",
            loss_description="Fire overnight in warehouse bay 3, started at the charging point.",
        )
        result = duplicates.compare(first, second, kind="fnol", reference="FNOL-2026-000002")
        assert result.score >= CONFIG.duplicate_strong_threshold
        assert {reason.signal for reason in result.reasons} >= {"policy", "loss_date"}

    def test_a_different_loss_on_the_same_policy_is_not_a_duplicate(self) -> None:
        first = make_case(
            policy_number="POL-2026-0041",
            date_of_loss=datetime(2026, 8, 3, tzinfo=UTC),
            loss_location="Leeds",
            loss_description="Fire in the warehouse.",
        )
        second = make_case(
            policy_number="POL-2026-0041",
            date_of_loss=datetime(2026, 2, 14, tzinfo=UTC),
            loss_location="Keighley",
            loss_description="Theft of palletised stock from the depot yard.",
        )
        result = duplicates.compare(first, second, kind="fnol", reference="FNOL-2026-000009")
        assert not duplicates.is_duplicate_candidate(result, config=CONFIG)


# ---------------------------------------------------------------------------
# Catastrophe matching
# ---------------------------------------------------------------------------


def make_event(**overrides: object) -> SimpleNamespace:
    base: dict[str, object] = {
        "id": "event-1",
        "reference": "CAT-2026-004",
        "name": "Storm Isolde",
        "event_type": "flood",
        "perils": ["flood", "storm"],
        "start_date": date(2026, 8, 1),
        "end_date": date(2026, 8, 5),
        "country": "United Kingdom",
        "region": "Yorkshire and the Humber",
        "affected_areas": ["Leeds", "Wakefield", "Keighley"],
        "latitude": 53.8008,
        "longitude": -1.5491,
        "radius_km": 90,
    }
    base.update(overrides)
    return SimpleNamespace(**base)


class TestCatastropheMatching:
    def test_place_date_and_peril_all_have_to_agree(self) -> None:
        case = make_case(
            date_of_loss=datetime(2026, 8, 3, tzinfo=UTC),
            loss_location="Airedale Depot, Keighley",
            cause_of_loss="flood",
        )
        match = catastrophe.score_event(case, make_event(), config=CONFIG)
        assert match is not None
        assert match.reference == "CAT-2026-004"
        assert match.confidence > CONFIG.cat_match_threshold

    def test_the_wrong_place_is_not_the_event(self) -> None:
        case = make_case(
            date_of_loss=datetime(2026, 8, 3, tzinfo=UTC),
            loss_location="Truro, Cornwall",
            loss_country="United Kingdom",
            cause_of_loss="flood",
        )
        assert catastrophe.score_event(case, make_event(country=None), config=CONFIG) is None

    def test_a_theft_during_a_flood_is_not_a_flood_claim(self) -> None:
        case = make_case(
            date_of_loss=datetime(2026, 8, 3, tzinfo=UTC),
            loss_location="Leeds",
            cause_of_loss="theft",
            loss_description="Stock stolen from the yard overnight.",
        )
        match = catastrophe.score_event(case, make_event(), config=CONFIG)
        assert match is None or match.confidence < 0.7

    def test_no_loss_date_means_no_attribution(self) -> None:
        assert catastrophe.score_event(make_case(), make_event(), config=CONFIG) is None


# ---------------------------------------------------------------------------
# Triage and assignment
# ---------------------------------------------------------------------------


def triage_for(**overrides: object) -> triage_rules.TriageResult:
    payload: dict[str, object] = {
        "severity": Severity.LOW,
        "estimated_loss_minor": 10_000_00,
        "line_of_business": LineOfBusiness.PROPERTY,
        "fraud_risk": RiskLevel.LOW,
        "cat_matched": False,
        "injuries": 0,
        "fatalities": 0,
        "business_interruption": False,
        "potential_litigation": False,
        "text": "",
        "config": CONFIG,
    }
    payload.update(overrides)
    return triage_rules.triage(**payload)  # type: ignore[arg-type]


class TestTriage:
    def test_a_small_clean_claim_is_fast_tracked(self) -> None:
        result = triage_for()
        assert result.categories == [TriageCategory.SIMPLE]
        assert result.route.key == "fast_track"
        assert result.priority is Priority.ROUTINE

    def test_exposure_over_the_threshold_is_a_major_loss(self) -> None:
        result = triage_for(estimated_loss_minor=2_000_000_00, severity=Severity.CRITICAL)
        assert TriageCategory.MAJOR_LOSS in result.categories
        assert result.route.key == "major_loss"
        assert result.priority is Priority.URGENT

    def test_fraud_signals_route_to_the_investigations_unit_first(self) -> None:
        result = triage_for(fraud_risk=RiskLevel.HIGH, estimated_loss_minor=2_000_000_00)
        assert result.route.key == "siu"
        assert TriageCategory.MAJOR_LOSS in result.categories  # still both things

    def test_a_letter_of_claim_is_litigation_risk_however_small_the_loss(self) -> None:
        result = triage_for(text="the claimant's solicitor has served a letter of claim")
        assert TriageCategory.LITIGATION_RISK in result.categories
        assert result.route.key == "litigation"

    def test_specialist_lines_go_to_the_specialist_desk(self) -> None:
        result = triage_for(line_of_business=LineOfBusiness.CYBER)
        assert TriageCategory.SPECIALIST_REQUIRED in result.categories


def make_handler(**overrides: object) -> SimpleNamespace:
    base: dict[str, object] = {
        "id": "handler-1",
        "full_name": "Daniel Okafor",
        "team": "Major Loss",
        "skills": ["major_loss"],
        "lines_of_business": ["property"],
        "countries": ["United Kingdom"],
        "max_severity": "critical",
        "open_claims": 2,
        "capacity": 10,
        "authority_limit_minor": 1_000_000_00,
    }
    base.update(overrides)
    return SimpleNamespace(**base)


class TestAssignment:
    def test_the_qualified_handler_is_recommended_with_reasons(self) -> None:
        result = triage_for(estimated_loss_minor=2_000_000_00, severity=Severity.CRITICAL)
        recommendation = triage_rules.recommend_assignment(
            [
                make_handler(),
                make_handler(
                    id="handler-2",
                    full_name="Joel Whitcombe",
                    team="Fast Track",
                    skills=["motor"],
                    max_severity="medium",
                ),
            ],
            triage_result=result,
            severity=Severity.CRITICAL,
            line_of_business=LineOfBusiness.PROPERTY,
            country="United Kingdom",
        )
        assert recommendation.handler is not None
        assert recommendation.handler.name == "Daniel Okafor"
        assert recommendation.reasoning.startswith("Daniel Okafor")

    def test_nobody_qualified_queues_the_claim_rather_than_guessing(self) -> None:
        result = triage_for(estimated_loss_minor=2_000_000_00, severity=Severity.CRITICAL)
        recommendation = triage_rules.recommend_assignment(
            [make_handler(skills=["motor"], max_severity="low", open_claims=99)],
            triage_result=result,
            severity=Severity.CRITICAL,
            line_of_business=LineOfBusiness.PROPERTY,
            country="United Kingdom",
        )
        assert recommendation.handler is None
        assert "Major Loss queue" in recommendation.queue
        assert recommendation.alternatives[0].blockers

    def test_a_handler_at_capacity_is_not_offered(self) -> None:
        result = triage_for()
        recommendation = triage_rules.recommend_assignment(
            [make_handler(open_claims=10, capacity=10)],
            triage_result=result,
            severity=Severity.LOW,
            line_of_business=LineOfBusiness.PROPERTY,
            country="United Kingdom",
        )
        assert recommendation.handler is None


# ---------------------------------------------------------------------------
# Lifecycle
# ---------------------------------------------------------------------------


class TestLifecycle:
    def test_a_claim_can_only_be_created_from_ready(self) -> None:
        assert can_transition(FNOLStatus.READY_FOR_CLAIM, FNOLStatus.CLAIM_CREATED)
        assert not can_transition(FNOLStatus.RECEIVED, FNOLStatus.CLAIM_CREATED)

    def test_a_converted_notice_does_not_move_again(self) -> None:
        assert not can_transition(FNOLStatus.CLAIM_CREATED, FNOLStatus.NEEDS_REVIEW)
        assert (
            derive_status(FNOLStatus.CLAIM_CREATED, open_exception_codes=["no_policy_match"])
            is FNOLStatus.CLAIM_CREATED
        )

    def test_advisory_exceptions_do_not_hold_a_notice(self) -> None:
        assert (
            derive_status(
                FNOLStatus.PROCESSING,
                open_exception_codes=["high_severity", "cat_match"],
            )
            is FNOLStatus.READY_FOR_CLAIM
        )

    def test_capture_problems_are_reported_before_matching_problems(self) -> None:
        assert (
            derive_status(
                FNOLStatus.PROCESSING,
                open_exception_codes=["no_policy_match", "missing_critical_information"],
            )
            is FNOLStatus.INCOMPLETE
        )

    def test_blocking_codes_come_back_in_checklist_order(self) -> None:
        codes = blocking_codes(
            ["possible_duplicate", "no_policy_match", "missing_critical_information"]
        )
        assert codes[0] is ExceptionCode.NO_POLICY_MATCH

    def test_an_officer_referral_survives_reprocessing(self) -> None:
        assert derive_status(FNOLStatus.REFERRED, open_exception_codes=[]) is FNOLStatus.REFERRED


# ---------------------------------------------------------------------------
# References
# ---------------------------------------------------------------------------


class TestReferences:
    def test_references_are_fixed_width_and_round_trip(self) -> None:
        reference = format_reference("FNOL", 2026, 412)
        assert reference == "FNOL-2026-000412"
        assert parse_reference(reference) == ("FNOL", 2026, 412)
        assert is_claim_reference("CLM-2026-001284")
        assert not is_claim_reference("FNOL-2026-000412")


# ---------------------------------------------------------------------------
# Deterministic reading
# ---------------------------------------------------------------------------


BROKER_EMAIL = """Subject: FNOL - Northline Logistics - fire at Leeds warehouse

Policy number: POL-2026-0041
Insured: Northline Logistics Limited
Reported by: Elaine Prosser
Contact email: e.prosser@hardingvale.co.uk

Date of loss: 03/08/2026
Location: Unit 7, Wakefield Road, Leeds LS9 8AA
Cause: fire
Description: A fire broke out overnight in bay 3 of the warehouse.
The sprinkler system contained it but the roof panels have failed.
Injuries: none
Estimated loss: GBP 128,000
"""


class TestHeuristicReading:
    def test_labelled_fields_are_read_with_high_confidence(self) -> None:
        extraction = extract_from_text(BROKER_EMAIL)
        assert extraction.policy.policy_number.value == "POL-2026-0041"
        assert extraction.policy.policy_number.confidence >= 0.8
        assert extraction.loss.date_of_loss.value == "03/08/2026"
        assert extraction.loss.estimated_loss_amount.value == "GBP 128,000"

    def test_a_description_continues_past_the_first_line(self) -> None:
        extraction = extract_from_text(BROKER_EMAIL)
        description = extraction.loss.loss_description.value or ""
        assert "roof panels have failed" in description

    def test_no_injuries_reads_as_zero_not_as_one(self) -> None:
        extraction = extract_from_text(BROKER_EMAIL)
        assert normalisation.parse_count(extraction.loss.injuries.value) == 0

    def test_a_missing_field_is_absent_rather_than_invented(self) -> None:
        extraction = extract_from_text(BROKER_EMAIL)
        assert extraction.additional.police_reference.value is None
        assert extraction.additional.police_reference.confidence == 0.0

    def test_classification_reads_the_words_in_the_notice(self) -> None:
        result = classify_from_text(BROKER_EMAIL)
        assert result.line_of_business == LineOfBusiness.PROPERTY.value
        assert result.confidence > 0.0
        assert "property" in result.reasoning.lower()

    def test_a_notice_with_no_signals_classifies_as_unknown(self) -> None:
        result = classify_from_text("Please call me back about the thing.")
        assert result.line_of_business == LineOfBusiness.UNKNOWN.value
        assert result.confidence == 0.0

    def test_the_attending_fire_brigade_is_not_the_loss(self) -> None:
        """`fire` leads the property line, and every notice mentions the brigade.

        Taken in table order, one mention of the fire service outranked the escape
        of water that actually happened — and because the validator drops any loss
        type the model invents, this reading is what reached the case whenever the
        model answered outside the vocabulary. An ammonia release was filed as a
        fire on the strength of "Baltimore City Fire Department HAZMAT attended".
        """
        result = classify_from_text(
            "A frozen fire sprinkler pipe burst on the third floor and caused an "
            "escape of water through the two storeys below. The fire alarm sounded "
            "and the fire brigade attended."
        )
        assert result.loss_type == "escape_of_water"

    def test_a_loss_no_configured_type_describes_reads_as_nothing(self) -> None:
        """Better empty than wrong: `other` is the model's answer to give, not this one."""
        result = classify_from_text(
            "A welded elbow on the liquid ammonia header failed overnight. "
            "Baltimore City Fire Department HAZMAT attended the premises."
        )
        assert result.loss_type is None

    def test_the_loss_the_notice_dwells_on_is_the_one_chosen(self) -> None:
        result = classify_from_text(
            "Fire broke out in the dye house and spread through the roof void. "
            "The fire damage extends across two bays."
        )
        assert result.loss_type == "fire"
