"""The policy identification engine, tested without a database or a model.

Everything in `app.domain.policy_identification` is a pure function over two plain
objects, which is the whole reason it lives there. These tests assert on the
*reason list* as much as on the score, because a reason is the product: a test
that pins "the loss location does not match any location insured under this
policy" is a test of a promise made to an officer, and a test that only pins 0.61
is a test of arithmetic.
"""

from __future__ import annotations

from datetime import date

import pytest

from app.core.config import FNOLSettings
from app.domain import policy_identification as engine
from app.domain.enums import (
    PolicyConfidence,
    PolicyIdentificationStatus,
    PolicyMatchStrength,
    PolicyPeriodOutcome,
    SignalOutcome,
)

CONFIG = FNOLSettings()


def notice(**overrides: object) -> engine.NoticeSignals:
    """A notice stating whatever the test needs and nothing else.

    Values are wrapped in `SignalValue` here rather than in every test, so a test
    reads as "the notice said this" instead of as construction.
    """
    loss_day = overrides.pop("loss_day", None)
    estimated = overrides.pop("estimated_loss_minor", None)
    wrapped = {
        key: engine.SignalValue(value=str(value), field_key=f"test.{key}")
        for key, value in overrides.items()
        if value is not None
    }
    if loss_day is not None:
        assert isinstance(loss_day, date)
        wrapped["date_of_loss"] = engine.SignalValue(
            value=f"{loss_day:%d %b %Y}", field_key="loss.date_of_loss"
        )
    return engine.NoticeSignals(
        **wrapped,
        loss_day=loss_day if isinstance(loss_day, date) else None,
        estimated_loss_minor=estimated if isinstance(estimated, int) else None,
    )


def policy(**overrides: object) -> engine.PolicyFacts:
    base: dict[str, object] = {
        "policy_id": "policy-1",
        "policy_number": "POL-2026-0041",
        "insured_name": "Northline Logistics Limited",
        "line_of_business": "property",
        "status": "active",
        "effective_date": date(2026, 1, 1),
        "expiry_date": date(2026, 12, 31),
    }
    base.update(overrides)
    return engine.PolicyFacts(**base)  # type: ignore[arg-type]


def location(address: str, postcode: str, ref: str = "Location 001") -> engine.PolicyLocationFacts:
    return engine.PolicyLocationFacts(
        location_ref=ref, description=None, address=address, postcode=postcode
    )


def outcome(match: engine.CandidateMatch, signal: str) -> SignalOutcome:
    return match.outcome_of(signal)


def explanation(match: engine.CandidateMatch, signal: str) -> str:
    found = match.result(signal)
    assert found is not None, f"no result for {signal}"
    return found.explanation


class TestPolicyNumber:
    def test_exact_number_and_agreeing_name_is_exact(self) -> None:
        match = engine.compare(
            notice(
                policy_number="POL-2026-0041",
                insured_name="Northline Logistics Ltd",
                loss_day=date(2026, 3, 14),
            ),
            policy(),
            config=CONFIG,
        )
        assert match.confidence is PolicyConfidence.EXACT
        assert outcome(match, "policy_number") is SignalOutcome.MATCH
        assert "matches exactly" in explanation(match, "policy_number")

    def test_punctuation_is_formatting_not_identity(self) -> None:
        match = engine.compare(
            notice(policy_number="pol 2026 0041", insured_name="Northline Logistics Limited"),
            policy(),
            config=CONFIG,
        )
        assert outcome(match, "policy_number") is SignalOutcome.MATCH
        assert match.score_of("policy_number") == 1.0

    def test_ocr_damage_matches_at_a_lower_tier_and_says_so(self) -> None:
        """`P0L-2O26-OO41` is this policy read by a scanner with an opinion."""
        match = engine.compare(
            notice(policy_number="P0L-2O26-OO41", insured_name="Northline Logistics Limited"),
            policy(),
            config=CONFIG,
        )
        assert outcome(match, "policy_number") is SignalOutcome.MATCH
        assert match.score_of("policy_number") == pytest.approx(0.9)
        assert "OCR" in explanation(match, "policy_number")

    def test_one_transposed_digit_is_partial_not_a_match(self) -> None:
        match = engine.compare(
            notice(policy_number="POL-2026-0014", insured_name="Northline Logistics Limited"),
            policy(),
            config=CONFIG,
        )
        assert outcome(match, "policy_number") is SignalOutcome.PARTIAL
        assert "transcription error" in explanation(match, "policy_number")

    def test_a_different_number_is_a_stated_mismatch(self) -> None:
        match = engine.compare(
            notice(policy_number="XYZ-1999-9999", insured_name="Northline Logistics Limited"),
            policy(),
            config=CONFIG,
        )
        assert outcome(match, "policy_number") is SignalOutcome.MISMATCH
        assert "different policy number" in explanation(match, "policy_number")

    def test_exact_number_with_a_wrong_insured_is_not_confirmed_blindly(self) -> None:
        """The case the whole confidence ladder exists for.

        A policy number that matches while the insured named does not is the shape
        of a broker quoting a reference off the wrong covering schedule. Arithmetic
        over a weighted average happens to demote it; this asserts that the demotion
        is *decided* — and that the officer is told why in as many words.
        """
        match = engine.compare(
            notice(
                policy_number="POL-2026-0041",
                insured_name="Ashford Freight Services Ltd",
                loss_day=date(2026, 3, 14),
            ),
            policy(),
            config=CONFIG,
        )
        assert match.confidence is not PolicyConfidence.EXACT
        assert match.confidence is not PolicyConfidence.STRONG
        codes = {warning.code for warning in match.warnings}
        assert "identity_conflict" in codes


class TestBrokerReference:
    def test_the_brokers_own_reference_identifies_the_policy(self) -> None:
        match = engine.compare(
            notice(broker_reference="HVB/NL/0041", insured_name="Northline Logistics Limited"),
            policy(broker_reference="HVB/NL/0041"),
            config=CONFIG,
        )
        assert outcome(match, "broker_reference") is SignalOutcome.MATCH
        assert match.confidence in (PolicyConfidence.STRONG, PolicyConfidence.EXACT)

    def test_a_broker_reference_quoted_as_the_policy_number_is_recognised(self) -> None:
        """Brokers put their own reference in the field labelled "policy number".

        It is what their system prints, and reading it for what it is turns a
        rejected candidate into the right one. See `TestReferenceSubstitution` for
        the other half of the rule: the policy-number row must not also report a
        failure, or one fact is counted twice against the policy it identified.
        """
        match = engine.compare(
            notice(policy_number="HVB/NL/0041", insured_name="Northline Logistics Limited"),
            policy(broker_reference="HVB/NL/0041"),
            config=CONFIG,
        )
        assert outcome(match, "broker_reference") is SignalOutcome.MATCH
        assert "broker" in explanation(match, "broker_reference").lower()


class TestInsuredIdentity:
    def test_legal_suffixes_are_formatting(self) -> None:
        match = engine.compare(
            notice(insured_name="Northline Logistics Ltd"), policy(), config=CONFIG
        )
        assert outcome(match, "insured_name") is SignalOutcome.MATCH

    def test_a_sister_company_is_partial_and_says_why(self) -> None:
        match = engine.compare(
            notice(insured_name="Northline Logistics"),
            policy(insured_name="Northline Logistics (Scotland) Limited"),
            config=CONFIG,
        )
        assert outcome(match, "insured_name") is SignalOutcome.PARTIAL
        assert "sister company" in explanation(match, "insured_name")

    def test_a_subcontractor_matches_through_the_joint_names(self) -> None:
        """A construction claim reported by a party who is not the named insured.

        On a CAR policy the principal and the main contractor are insureds for
        their interest. Scoring the notice zero because the first name on the
        declarations page is somebody else fails a legitimate claim.
        """
        match = engine.compare(
            notice(
                insured_name="Waterline Regeneration LLP",
                line_of_business="construction",
            ),
            policy(
                insured_name="Bellhaven Construction Limited",
                line_of_business="construction",
                principal_name="Waterline Regeneration LLP",
            ),
            config=CONFIG,
        )
        assert outcome(match, "insured_name") is SignalOutcome.MATCH
        assert "principal" in explanation(match, "insured_name")


class TestBrokerAndInsuredDomains:
    def test_the_senders_domain_is_evidence_about_the_broker(self) -> None:
        match = engine.compare(
            notice(
                broker_domain="hardingvale.co.uk",
                insured_name="Northline Logistics Limited",
            ),
            policy(broker_domain="hardingvale.co.uk"),
            config=CONFIG,
        )
        assert outcome(match, "broker_domain") is SignalOutcome.MATCH

    def test_the_senders_domain_is_never_compared_to_the_insureds(self) -> None:
        """The defect this pair of signals exists to fix.

        Most commercial notices arrive from a broking house. A single email signal
        compared against the insured's address will almost never match, and scoring
        that as a fact about the client is a category error.
        """
        match = engine.compare(
            notice(
                broker_domain="hardingvale.co.uk",
                insured_name="Northline Logistics Limited",
            ),
            policy(insured_email="operations@northline-logistics.co.uk"),
            config=CONFIG,
        )
        # The insured's own domain is missing *from the notice* — the policy holds
        # one and nothing on the notice answered it, which is a gap an officer can go
        # and fill.
        assert outcome(match, "insured_domain") is SignalOutcome.MISSING
        assert "No email address for the insured" in explanation(match, "insured_domain")
        # The sender's domain, by contrast, is on the notice and has nothing on this
        # policy to compare against. The record is what is short, not the notice, and
        # the two are not reported as the same failure.
        assert outcome(match, "broker_domain") is SignalOutcome.NOT_COMPARED
        assert "held against this policy" in explanation(match, "broker_domain")

    def test_a_mail_provider_identifies_nobody(self) -> None:
        match = engine.compare(
            notice(broker_domain="gmail.com", insured_name="Northline Logistics Limited"),
            policy(broker_domain="hardingvale.co.uk"),
            config=CONFIG,
        )
        assert outcome(match, "broker_domain") is SignalOutcome.NOT_COMPARED
        assert "mail provider" in explanation(match, "broker_domain")


class TestRiskLocation:
    def test_a_loss_at_the_seventh_warehouse_matches_the_seventh_warehouse(self) -> None:
        """The central defect in matching against a primary location alone."""
        match = engine.compare(
            notice(
                loss_location="Unit 4, Gelderd Road, Leeds LS11 8AX",
                loss_postcode="LS11 8AX",
                insured_name="Northline Logistics Limited",
            ),
            policy(
                primary_location="Unit 7, Wakefield Road, Leeds LS9 8AA",
                locations=(
                    location("Unit 7, Wakefield Road, Leeds LS9 8AA", "LS9 8AA", "Location 001"),
                    location("Unit 4, Gelderd Road, Leeds LS11 8AX", "LS11 8AX", "Location 007"),
                ),
            ),
            config=CONFIG,
        )
        assert outcome(match, "risk_location") is SignalOutcome.MATCH
        assert "Location 007" in explanation(match, "risk_location")

    def test_the_matched_locations_own_excess_is_what_the_card_shows(self) -> None:
        """The deductible attaches to the location, not to the policy.

        A card printing the policy's headline excess beside a loss at a location
        with its own is printing the wrong number, and the officer is the one who
        has to notice.
        """
        entry = engine.PolicyLocationFacts(
            location_ref="Location 007",
            description="Cold store",
            address="Unit 4, Gelderd Road, Leeds LS11 8AX",
            postcode="LS11 8AX",
            sum_insured_minor=900_000_00,
            deductible_minor=50_000_00,
        )
        match = engine.compare(
            notice(
                loss_postcode="LS11 8AX",
                loss_location="Unit 4, Gelderd Road, Leeds LS11 8AX",
                insured_name="Northline Logistics Limited",
            ),
            policy(
                limit_amount_minor=5_000_000_00,
                deductible_amount_minor=25_000_00,
                locations=(entry,),
            ),
            config=CONFIG,
        )
        assert match.display.excess_minor == 50_000_00
        assert match.display.limit_minor == 900_000_00
        assert match.display.location_label == "Location 007"

    def test_a_loss_nowhere_on_the_schedule_says_so_in_those_words(self) -> None:
        match = engine.compare(
            notice(
                loss_location="Berth 42, Port of Southampton SO14 3QN",
                insured_name="Northline Logistics Limited",
            ),
            policy(
                locations=(
                    location("Unit 7, Wakefield Road, Leeds LS9 8AA", "LS9 8AA"),
                    location("Airedale Depot, Keighley BD21 4LP", "BD21 4LP", "Location 002"),
                )
            ),
            config=CONFIG,
        )
        assert outcome(match, "risk_location") is SignalOutcome.MISMATCH
        assert "does not match any location insured" in explanation(match, "risk_location")

    def test_a_postcode_district_agreement_is_worth_saying_separately(self) -> None:
        match = engine.compare(
            notice(loss_postcode="LS9 7AB", insured_name="Northline Logistics Limited"),
            policy(locations=(location("Unit 7, Wakefield Road, Leeds LS9 8AA", "LS9 8AA"),)),
            config=CONFIG,
        )
        assert outcome(match, "risk_location") is SignalOutcome.MATCH
        assert "district" in explanation(match, "risk_location")

    def test_a_us_zip_matches_the_scheduled_location_exactly(self) -> None:
        """The same signal on the American half of the book.

        The policy schedule has held US ZIPs all along; only the notice side could
        not read one, so this comparison never happened and the strongest location
        signal scored nothing on every US notice.
        """
        match = engine.compare(
            notice(
                loss_location="2870 Patapsco Industrial Parkway, Baltimore, MD 21226",
                loss_postcode="21226",
                insured_name="Harborline Cold Storage & Logistics, LLC",
            ),
            policy(
                locations=(
                    location("1200 W Overland Rd, Meridian, ID 83713", "83713", "Location 001"),
                    location(
                        "2870 Patapsco Industrial Parkway, Baltimore, MD 21226",
                        "21226",
                        "Location 004",
                    ),
                )
            ),
            config=CONFIG,
        )
        assert outcome(match, "risk_location") is SignalOutcome.MATCH
        assert "Location 004" in explanation(match, "risk_location")

    def test_two_zips_sharing_a_leading_digit_are_not_a_district(self) -> None:
        """A ZIP has no outward code, and pretending it has one invents a match.

        Cutting three digits off `21226` the way a UK postcode splits would leave
        `21` — most of Maryland, Delaware and Pennsylvania — and score a 0.85
        location agreement between two risks four hours apart.
        """
        match = engine.compare(
            notice(loss_postcode="21403", insured_name="Harborline Cold Storage & Logistics, LLC"),
            policy(
                locations=(
                    location(
                        "2870 Patapsco Industrial Parkway, Baltimore, MD 21226",
                        "21226",
                        "Location 004",
                    ),
                )
            ),
            config=CONFIG,
        )
        assert outcome(match, "risk_location") is SignalOutcome.MISMATCH

    def test_a_plant_serial_is_not_read_as_a_postcode(self) -> None:
        """`PC290LC` is an excavator, and a construction notice is full of them.

        Read as a postcode it becomes a location token that some later notice
        collides with — the trap `app.domain.policy_extraction` already documents.
        """
        assert engine._postcode_of("Serial PC290LC-11 excavator") is None
        assert engine._postcode_of("Unit 7, Wakefield Road, Leeds LS9 8AA") == "LS9 8AA"


class TestPolicyPeriod:
    def test_in_force(self) -> None:
        match = engine.compare(
            notice(insured_name="Northline Logistics Limited", loss_day=date(2026, 3, 14)),
            policy(),
            config=CONFIG,
        )
        assert match.period_outcome is PolicyPeriodOutcome.IN_FORCE
        assert outcome(match, "policy_period") is SignalOutcome.MATCH

    def test_outside_the_period_is_shown_not_hidden(self) -> None:
        """A candidate that fails the date check is a candidate to *show*.

        "This is your policy, but the loss is outside the period" is a coverage
        conversation. Dropping it would make the officer search for a policy the
        engine had already found.
        """
        match = engine.compare(
            notice(
                policy_number="POL-2026-0041",
                insured_name="Northline Logistics Limited",
                loss_day=date(2027, 5, 1),
            ),
            policy(),
            config=CONFIG,
        )
        assert match.period_outcome is PolicyPeriodOutcome.OUTSIDE_PERIOD
        assert match.confidence is not PolicyConfidence.REJECTED
        assert "outside_policy_period" in {warning.code for warning in match.warnings}

    def test_a_defect_found_after_completion_is_in_the_maintenance_period(self) -> None:
        match = engine.compare(
            notice(
                insured_name="Bellhaven Construction Limited",
                line_of_business="construction",
                loss_day=date(2027, 2, 14),
            ),
            policy(
                insured_name="Bellhaven Construction Limited",
                line_of_business="construction",
                effective_date=date(2025, 9, 1),
                expiry_date=date(2026, 6, 30),
                practical_completion_date=date(2026, 6, 30),
                maintenance_period_months=12,
            ),
            config=CONFIG,
        )
        assert match.period_outcome is PolicyPeriodOutcome.IN_MAINTENANCE_PERIOD
        assert "defects liability period" in explanation(match, "policy_period")

    def test_a_late_notified_loss_points_at_the_prior_term(self) -> None:
        match = engine.compare(
            notice(
                policy_number="CP-2026-30582",
                insured_name="Kelbrook Foods Limited",
                loss_day=date(2026, 2, 3),
            ),
            policy(
                policy_number="CP-2026-30582",
                insured_name="Kelbrook Foods Limited",
                effective_date=date(2026, 4, 1),
                expiry_date=date(2027, 3, 31),
                prior_term=(date(2025, 4, 1), date(2026, 3, 31)),
                prior_policy_number="CP-2025-30582",
            ),
            config=CONFIG,
        )
        assert match.period_outcome is PolicyPeriodOutcome.PRIOR_TERM
        assert match.prior_policy_number == "CP-2025-30582"
        assert "CP-2025-30582" in explanation(match, "policy_period")


class TestConstruction:
    def test_the_contract_number_identifies_a_project_risk(self) -> None:
        match = engine.compare(
            notice(
                contract_number="RQ2-JCT-2025-0884",
                project_name="Riverside Quarter Phase 2",
                line_of_business="construction",
                loss_day=date(2026, 3, 2),
            ),
            policy(
                insured_name="Bellhaven Construction Limited",
                line_of_business="construction",
                effective_date=date(2025, 9, 1),
                expiry_date=date(2026, 6, 30),
                project_name="Riverside Quarter Phase 2",
                contract_number="RQ2-JCT-2025-0884",
            ),
            config=CONFIG,
        )
        assert outcome(match, "contract_number") is SignalOutcome.MATCH
        assert outcome(match, "project_name") is SignalOutcome.MATCH
        assert match.confidence is PolicyConfidence.STRONG

    def test_project_signals_are_not_compared_on_a_property_risk(self) -> None:
        """A property notice must not carry two "not stated" project rows.

        `NOT_COMPARED` on a signal that does not apply to the risk is noise, and
        noise on this panel costs the officer the one line that mattered.
        """
        match = engine.compare(
            notice(insured_name="Northline Logistics Limited", line_of_business="property"),
            policy(),
            config=CONFIG,
        )
        assert match.result("contract_number") is None
        assert match.result("project_name") is None


class TestMissingAndNotCompared:
    def test_a_gap_in_the_notice_reads_differently_from_a_signal_that_does_not_apply(
        self,
    ) -> None:
        """`MISSING` sends the officer to find something; `NOT_COMPARED` does not.

        The distinction is the reason the outcome is an enum rather than a float,
        and collapsing the two into one silence tells an officer to chase a fact
        that was never there.
        """
        match = engine.compare(
            notice(policy_number="POL-2026-0041"),
            policy(broker_name="Harding Vale Brokers"),
            config=CONFIG,
        )
        assert outcome(match, "broker_name") is SignalOutcome.MISSING
        assert explanation(match, "broker_name") == "No broker was named on the notice."
        assert outcome(match, "insured_domain") is SignalOutcome.NOT_COMPARED

    def test_neither_missing_nor_uncompared_signals_drag_the_score_down(self) -> None:
        """A sparse notice is not punished for being sparse.

        The alternative — scoring an absent signal zero — makes a notice quoting
        only a policy number rank below one quoting nothing, which is backwards.
        """
        sparse = engine.compare(
            notice(policy_number="POL-2026-0041", insured_name="Northline Logistics Limited"),
            policy(),
            config=CONFIG,
        )
        assert sparse.score > 0.9
        assert len(sparse.compared_signals) < len(sparse.signal_results)

    def test_the_evidence_count_is_reported_alongside_the_score(self) -> None:
        """A 96% on two signals and a 96% on seven are not the same claim.

        The score alone cannot tell them apart, so the count travels with it.
        """
        match = engine.compare(
            notice(policy_number="POL-2026-0041", insured_name="Northline Logistics Limited"),
            policy(),
            config=CONFIG,
        )
        payload = match.as_dict()
        assert payload["compared_signal_count"] == len(match.compared_signals)
        assert payload["signal_count"] == len(match.signal_results)


class TestWarnings:
    def test_a_lapsed_policy_is_a_warning_and_not_a_lower_rank(self) -> None:
        """Identity confidence and coverage plausibility are different axes.

        A lapsed policy may be exactly the right policy. Demoting it would send the
        officer looking for one that does not exist.
        """
        match = engine.compare(
            notice(policy_number="POL-2026-0041", insured_name="Northline Logistics Limited"),
            policy(status="lapsed"),
            config=CONFIG,
        )
        assert match.confidence is PolicyConfidence.EXACT
        assert "policy_not_active" in {warning.code for warning in match.warnings}

    def test_a_peril_the_policy_does_not_list_is_flagged_without_being_scored(self) -> None:
        match = engine.compare(
            notice(
                policy_number="POL-2026-0041",
                insured_name="Northline Logistics Limited",
                cause_of_loss="ransomware",
            ),
            policy(perils_covered=("fire", "flood", "storm")),
            config=CONFIG,
        )
        assert "peril_not_listed" in {warning.code for warning in match.warnings}
        assert match.confidence is PolicyConfidence.EXACT

    def test_a_line_of_business_mismatch_narrows_rather_than_deciding(self) -> None:
        match = engine.compare(
            notice(
                policy_number="POL-2026-0041",
                insured_name="Northline Logistics Limited",
                line_of_business="cyber",
            ),
            policy(line_of_business="property"),
            config=CONFIG,
        )
        assert outcome(match, "line_of_business") is SignalOutcome.MISMATCH
        assert "narrows rather than decides" in explanation(match, "line_of_business")
        assert "line_of_business_mismatch" in {warning.code for warning in match.warnings}

    def test_construction_and_engineering_are_the_same_risk(self) -> None:
        match = engine.compare(
            notice(insured_name="Marchmont Civils Ltd", line_of_business="construction"),
            policy(insured_name="Marchmont Civils Ltd", line_of_business="engineering"),
            config=CONFIG,
        )
        assert outcome(match, "line_of_business") is SignalOutcome.MATCH


class TestIdentificationGate:
    def test_dates_and_a_line_of_business_alone_are_not_a_candidate(self) -> None:
        """Without this gate every property policy on the book qualifies."""
        result = engine.identify(
            notice(line_of_business="property", loss_day=date(2026, 3, 14)),
            [policy(), policy(policy_id="policy-2", policy_number="CP-2026-30583")],
            config=CONFIG,
        )
        assert result.candidates == []
        assert result.status is PolicyIdentificationStatus.NO_MATCH

    def test_a_unique_reference_that_disagrees_is_rejected_and_still_explained(self) -> None:
        result = engine.identify(
            notice(policy_number="ZZZ-0000-0000", insured_name="Somebody Else Ltd"),
            [policy()],
            config=CONFIG,
        )
        assert result.candidates == []
        # Rejected, not invisible: an officer asking "why is my policy not listed"
        # gets the comparison rather than an empty panel.
        assert len(result.near_misses) == 1
        assert result.near_misses[0].confidence is PolicyConfidence.REJECTED


class TestRankingAndRecommendation:
    def test_the_best_candidate_is_recommended_when_it_stands_alone(self) -> None:
        result = engine.identify(
            notice(
                policy_number="POL-2026-0041",
                insured_name="Northline Logistics Limited",
                loss_day=date(2026, 3, 14),
            ),
            [policy(), policy(policy_id="policy-2", policy_number="CP-2026-30583")],
            config=CONFIG,
        )
        assert result.status is PolicyIdentificationStatus.CONFIDENT_MATCH
        assert result.recommended_policy_id == "policy-1"
        assert result.candidates[0].recommendation_reason.startswith("Put forward")

    def test_two_strong_candidates_recommend_neither(self) -> None:
        """Ambiguity is an outcome to surface, not one to tie-break.

        Two sister companies that both match on name, broker and line of business is
        precisely when the officer has a choice to make, and pre-selecting one of
        them hides that the choice existed.
        """
        result = engine.identify(
            notice(
                insured_name="Northline Logistics",
                broker_name="Harding Vale Brokers",
                loss_day=date(2026, 3, 14),
            ),
            [
                policy(broker_name="Harding Vale Brokers"),
                policy(
                    policy_id="policy-2",
                    policy_number="POL-2026-0198",
                    insured_name="Northline Logistics (Scotland) Limited",
                    broker_name="Harding Vale Brokers",
                ),
            ],
            config=CONFIG,
        )
        assert len(result.candidates) == 2
        assert result.recommended_policy_id is None
        assert result.status is PolicyIdentificationStatus.NEEDS_REVIEW
        assert result.strength is PolicyMatchStrength.POSSIBLE

    def test_ties_are_broken_by_how_much_evidence_the_score_rests_on(self) -> None:
        thin = policy(policy_id="thin", policy_number="POL-2026-0041")
        thick = policy(
            policy_id="thick",
            policy_number="POL-2026-0041",
            broker_name="Harding Vale Brokers",
            broker_domain="hardingvale.co.uk",
        )
        result = engine.identify(
            notice(
                policy_number="POL-2026-0041",
                insured_name="Northline Logistics Limited",
                broker_name="Harding Vale Brokers",
                broker_domain="hardingvale.co.uk",
                loss_day=date(2026, 3, 14),
            ),
            [thin, thick],
            config=CONFIG,
        )
        assert result.candidates[0].policy_id == "thick"


#: Beacon Mechanical Services, as it appears in `policy/policy-book.json`: two
#: policies, same insured, same address, same broker, different products. Nothing
#: but the broker reference and the product distinguishes them.
BEACON_GL: dict[str, object] = {
    "policy_id": "GL-8804-27153",
    "policy_number": "GL-8804-27153",
    "insured_name": "Beacon Mechanical Services, Inc.",
    "insured_organisation": "Beacon Mechanical Services, Inc.",
    "line_of_business": "liability",
    "policy_type": "Commercial General Liability",
    "broker_name": "Front Range Commercial Insurance Group, Inc.",
    "broker_reference": "FRC/GL/2025/8804",
    "broker_domain": "frontrangecommercial.example",
}
BEACON_FLOATER: dict[str, object] = {
    "policy_id": "IM-7741-15530",
    "policy_number": "IM-7741-15530",
    "insured_name": "Beacon Mechanical Services, Inc.",
    "insured_organisation": "Beacon Mechanical Services, Inc.",
    "line_of_business": "engineering",
    "policy_type": "Contractors Equipment and Installation Floater",
    "broker_name": "Front Range Commercial Insurance Group, Inc.",
    "broker_reference": "FRC/IM/2025/7741",
    "broker_domain": "frontrangecommercial.example",
}


class TestPolicyType:
    """The product, as distinct from the line of business.

    `PolicyFacts.policy_type` was carried for display and compared by nothing, so an
    insured holding two policies in one line was separated almost entirely by
    `line_of_business` — the lightest signal in the set, at 0.6, and one that says
    the same thing about both when the two products sit in the same line.
    """

    def book(self) -> list[engine.PolicyFacts]:
        return [policy(**BEACON_GL), policy(**BEACON_FLOATER)]  # type: ignore[arg-type]

    def test_without_a_policy_number_the_product_is_what_separates_them(self) -> None:
        without = engine.identify(
            notice(
                insured_name="Beacon Mechanical Services, Inc.",
                broker_domain="frontrangecommercial.example",
                loss_day=date(2026, 3, 14),
            ),
            self.book(),
            config=CONFIG,
        )
        scores = {candidate.policy_id: candidate.score for candidate in without.candidates}
        # The premise: with no product stated the two are indistinguishable.
        assert scores["GL-8804-27153"] == pytest.approx(scores["IM-7741-15530"])

        stated = engine.identify(
            notice(
                insured_name="Beacon Mechanical Services, Inc.",
                broker_domain="frontrangecommercial.example",
                policy_type="Contractors equipment and installation floater",
                loss_day=date(2026, 3, 14),
            ),
            self.book(),
            config=CONFIG,
        )
        assert stated.candidates[0].policy_id == "IM-7741-15530"
        assert stated.candidates[0].score - stated.candidates[1].score > (
            CONFIG.policy_identification_ambiguity_margin
        )

    def test_the_other_product_picks_the_other_policy(self) -> None:
        result = engine.identify(
            notice(
                insured_name="Beacon Mechanical Services, Inc.",
                broker_domain="frontrangecommercial.example",
                policy_type="Commercial general liability",
                loss_day=date(2026, 3, 14),
            ),
            self.book(),
            config=CONFIG,
        )
        assert result.candidates[0].policy_id == "GL-8804-27153"
        assert outcome(result.candidates[0], "policy_type") is SignalOutcome.MATCH
        assert outcome(result.candidates[1], "policy_type") is SignalOutcome.MISMATCH

    def test_a_product_the_family_table_does_not_recognise_reads_as_partial(self) -> None:
        """An unrecognised product name is where this is least reliable, and says so."""
        result = engine.identify(
            notice(
                policy_number="POL-2026-0041",
                insured_name="Northline Logistics Limited",
                policy_type="Bloodstock all risks",
                loss_day=date(2026, 3, 14),
            ),
            [policy(policy_type="Bloodstock All Risks")],
            config=CONFIG,
        )
        assert outcome(result.candidates[0], "policy_type") is SignalOutcome.PARTIAL

    def test_a_product_mismatch_warns_and_does_not_hide_the_policy(self) -> None:
        """A broker writing the wrong product is a thing to say, not to act on.

        `policy_type` is not a primary identifier: an insured commonly holds the
        product the notice did not name, so a mismatch must not reject a candidate or
        the officer loses the right policy to a typo.
        """
        result = engine.identify(
            notice(
                policy_number="GL-8804-27153",
                insured_name="Beacon Mechanical Services, Inc.",
                policy_type="Contractors equipment",
                loss_day=date(2026, 3, 14),
            ),
            [policy(**BEACON_GL)],  # type: ignore[arg-type]
            config=CONFIG,
        )
        best = result.candidates[0]
        assert best.policy_id == "GL-8804-27153"
        assert best.confidence is PolicyConfidence.EXACT
        assert "policy_type_mismatch" in {warning.code for warning in best.warnings}

    def test_the_longer_product_name_wins_the_classification(self) -> None:
        """ "Commercial general liability" must not classify as marine on "cargo"-style
        substring luck, and must beat the shorter "general liability" it contains."""
        assert engine.policy_type_family("Commercial General Liability") == "general_liability"
        assert engine.policy_type_family("Contractors Equipment") == "equipment_floater"
        assert engine.policy_type_family("Commercial Property") == "commercial_property"
        assert engine.policy_type_family("Bloodstock All Risks") is None


class TestAmbiguityIsNotWrittenToTheCase:
    def test_an_ambiguous_result_says_so_on_the_result(self) -> None:
        """The flag the service reads instead of falling back to `best`.

        `app.services.fnol.identification` used to write `result.best.policy_id`
        whenever nothing was recommended — which is exactly the ambiguous case, so a
        coin toss between two candidates reached the column that coverage reads
        against.
        """
        result = engine.identify(
            notice(
                insured_name="Northline Logistics",
                broker_name="Harding Vale Brokers",
                loss_day=date(2026, 3, 14),
            ),
            [
                policy(broker_name="Harding Vale Brokers"),
                policy(
                    policy_id="policy-2",
                    policy_number="POL-2026-0198",
                    insured_name="Northline Logistics (Scotland) Limited",
                    broker_name="Harding Vale Brokers",
                ),
            ],
            config=CONFIG,
        )
        assert result.recommended_policy_id is None
        assert result.ambiguous is True
        assert result.best is not None  # still ranked and still shown

    def test_a_clear_winner_is_not_ambiguous(self) -> None:
        result = engine.identify(
            notice(
                policy_number="POL-2026-0041",
                insured_name="Northline Logistics Limited",
                loss_day=date(2026, 3, 14),
            ),
            [policy(), policy(policy_id="policy-2", policy_number="CP-2026-30583")],
            config=CONFIG,
        )
        assert result.ambiguous is False


class TestSearchedOn:
    def test_the_panel_reports_what_was_searched_on_and_what_was_not(self) -> None:
        """Shown even when nothing matched.

        An officer who can see the search ran on a policy number and an insured name
        knows the book is the problem rather than the reading, and that is a
        different next action.
        """
        rows = engine.searched_on(
            notice(policy_number="POL-2026-0041", insured_name="Northline Logistics Limited")
        )
        by_signal = {row.signal: row for row in rows}
        assert by_signal["policy_number"].outcome is SignalOutcome.MATCH
        assert by_signal["policy_number"].evidence_field_key is not None
        assert by_signal["policy_period"].outcome is SignalOutcome.MISSING
        assert (
            by_signal["policy_period"].explanation
            == "No date of loss was read, so the policy period could not be checked."
        )

    def test_every_signal_that_can_be_traced_carries_its_field_key(self) -> None:
        """What makes a reason clickable rather than merely readable."""
        rows = engine.searched_on(notice(policy_number="POL-2026-0041"))
        traceable = [row for row in rows if row.evidence_field_key]
        assert len(traceable) >= 8


class TestSerialisation:
    def test_a_candidate_round_trips_everything_the_card_draws(self) -> None:
        match = engine.compare(
            notice(
                policy_number="POL-2026-0041",
                insured_name="Northline Logistics Limited",
                loss_day=date(2026, 3, 14),
            ),
            policy(limit_amount_minor=5_000_000_00, deductible_amount_minor=25_000_00),
            config=CONFIG,
        )
        payload = match.as_dict()
        assert payload["confidence"] == "exact"
        assert payload["match_strength"] == "exact"
        assert payload["display"]["limit_minor"] == 5_000_000_00
        signal = next(entry for entry in payload["signals"] if entry["signal"] == "policy_number")
        assert signal["outcome"] == "match"
        assert signal["explanation"]
        assert signal["evidence_field_key"]

    def test_the_prose_summary_is_derived_from_the_reasons(self) -> None:
        """So the sentence and the list can never disagree.

        A summary reading "matched on the policy number" beside a list saying the
        policy number mismatched is worse than no summary at all.
        """
        from app.services.fnol.identification import _prose

        match = engine.compare(
            notice(
                policy_number="XYZ-0000-0000",
                insured_name="Northline Logistics Limited",
                loss_day=date(2026, 3, 14),
            ),
            policy(),
            config=CONFIG,
        )
        summary = _prose(match)
        assert "did not match" in summary
        assert "policy number" in summary


class TestJointNamesDoNotReadAsConflict:
    def test_a_notice_from_the_employer_is_not_an_identity_conflict(self) -> None:
        """The construction case that must not be capped.

        A CAR policy insures the employer and the main contractor jointly. A notice
        from the employer names a party the policy insures, so it is not a
        disagreement about who the client is — and reading it as one would demote a
        candidate whose contract number matched exactly, on the risk where the
        contract number is the best identifier there is.
        """
        match = engine.compare(
            notice(
                insured_name="Waterline Regeneration LLP",
                insured_organisation="Waterline Regeneration LLP",
                contract_number="RQ2-JCT-2025-0884",
                project_name="Riverside Quarter Phase 2",
                line_of_business="construction",
                loss_day=date(2026, 3, 2),
            ),
            policy(
                insured_name="Bellhaven Construction Limited",
                insured_organisation="Bellhaven Construction Limited",
                line_of_business="construction",
                effective_date=date(2025, 9, 1),
                expiry_date=date(2026, 6, 30),
                project_name="Riverside Quarter Phase 2",
                contract_number="RQ2-JCT-2025-0884",
                principal_name="Waterline Regeneration LLP",
                contractor_name="Bellhaven Construction Limited",
            ),
            config=CONFIG,
        )
        assert outcome(match, "insured_organisation") is SignalOutcome.MATCH
        assert "principal" in explanation(match, "insured_organisation")
        assert "identity_conflict" not in {warning.code for warning in match.warnings}
        assert match.confidence is PolicyConfidence.STRONG
