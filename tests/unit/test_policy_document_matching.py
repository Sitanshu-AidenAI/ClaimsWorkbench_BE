"""The policy-document matching engine.

Pure functions over two dataclasses, so every case here is a promise made to a claims
officer rather than a fact about arithmetic. The assertions are deliberately on the
**reason list and the band**, not only on the score: a test that pins "the insured on
the notification matches an additional named insured" is a test of something an officer
reads, where one that pins 0.61 is a test of a weighted mean.

The three properties that matter most, in the order they were got wrong:

1. **Retrieval alone cannot recommend a policy.** Top-k over a library of twelve always
   returns twelve scores, and normalising them hands the first one a 1.0.
2. **Descriptive agreement is not identification.** Every in-force property policy
   agrees with a property loss on the date and the line of business. Two agreements, an
   axis each, and a normalised retrieval score computed to a confident-looking 0.56 for
   four notices whose right answer is "no policy in this library".
3. **Ambiguity is an outcome, not something to tie-break.** Two sister companies
   matching on name and broker is exactly when the officer has a choice.
"""

from __future__ import annotations

import uuid
from datetime import date
from typing import Any

import pytest

from app.core.config import PolicyLibrarySettings
from app.domain.enums import PolicyConfidence, PolicyPeriodOutcome, SignalOutcome
from app.domain.policy_matching import (
    DESCRIPTIVE_SIGNALS,
    IDENTIFYING_SIGNALS,
    Excerpt,
    MatchFacet,
    NoticeQuery,
    PolicyDocumentFacts,
    RetrievedPolicy,
    compare,
    line_is_compatible,
    match,
)

LIMITS = PolicyLibrarySettings()

ALL_FACETS = frozenset(
    {MatchFacet.IDENTITY, MatchFacet.RISK_LOCATION, MatchFacet.PERIL, MatchFacet.COVERAGE}
)


def facts(**overrides: object) -> PolicyDocumentFacts:
    """A property wording for Harborline, with everything read off its declarations."""
    base: dict[str, object] = {
        "document_id": uuid.uuid4(),
        "filename": "POL-CP-4471-88210_Harborline.pdf",
        "policy_id": uuid.uuid4(),
        "policy_number": "CP-4471-88210",
        "insured_name": "Harborline Cold Storage & Logistics, LLC",
        "insurer_name": "Meridian Atlantic Insurance Company",
        "broker_name": "Talbot & Rennick Insurance Brokers, Inc.",
        "policy_type": "Commercial Property Coverage Part",
        "line_of_business": "property",
        "effective_date": date(2025, 3, 1),
        "expiry_date": date(2026, 3, 1),
        "additional_insureds": (),
        "postcodes": ("21226", "19720"),
        "locations": ("2870 Patapsco Industrial Parkway, Baltimore, MD 21226",),
    }
    base.update(overrides)
    return PolicyDocumentFacts(**base)  # type: ignore[arg-type]


def notice(**overrides: object) -> NoticeQuery:
    base: dict[str, object] = {
        "policy_number": "CP-4471-88210",
        "insured_name": "Harborline Cold Storage & Logistics, LLC",
        "loss_location": "2870 Patapsco Industrial Parkway, Baltimore, MD 21226",
        "loss_postcode": "21226",
        "cause_of_loss": "Ammonia release from the refrigeration plant",
        "line_of_business": "property",
        "date_of_loss": date(2025, 9, 12),
        "reference": "FNOL-2026-0001",
    }
    base.update(overrides)
    return NoticeQuery(**base)  # type: ignore[arg-type]


def retrieved(
    document: PolicyDocumentFacts,
    *,
    score: float = 1.0,
    facets: frozenset[MatchFacet] = ALL_FACETS,
    excerpts: int = 2,
) -> RetrievedPolicy:
    return RetrievedPolicy(
        facts=document,
        retrieval_score=score,
        excerpts=tuple(
            Excerpt(
                chunk_ref=f"{document.document_id}:{index:05d}",
                content=f"Clause {index} of {document.policy_number}.",
                score=score,
                page_number=index + 1,
                facet=MatchFacet.IDENTITY,
            )
            for index in range(excerpts)
        ),
        facets_hit=facets,
        chunks_matched=excerpts,
    )


def signal(results: list[Any], name: str) -> Any:
    """One named signal out of a `compare()` result."""
    return next(item for item in results if item.signal == name)


class TestSignalTable:
    def test_identifying_and_descriptive_signals_partition_the_table(self) -> None:
        """The distinction the whole rejection gate rests on."""
        assert not (IDENTIFYING_SIGNALS & DESCRIPTIVE_SIGNALS)
        assert set(DESCRIPTIVE_SIGNALS) == {"policy_period", "line_of_business"}
        assert "policy_number" in IDENTIFYING_SIGNALS
        assert "insured_name" in IDENTIFYING_SIGNALS


class TestPolicyNumber:
    def test_an_exact_number_is_a_match_and_names_the_policy(self) -> None:
        results, mean = compare(notice(), facts())
        found = signal(results, "policy_number")

        assert found.outcome is SignalOutcome.MATCH
        assert found.score == 1.0
        assert "CP-4471-88210" in found.explanation
        assert mean is not None

    def test_a_contained_reference_is_still_a_match(self) -> None:
        """A reporter quoting `4471-88210` is quoting this policy."""
        found = signal(compare(notice(policy_number="4471-88210"), facts())[0], "policy_number")

        assert found.outcome is SignalOutcome.MATCH

    def test_punctuation_differences_are_the_same_reference(self) -> None:
        found = signal(compare(notice(policy_number="cp 4471 88210"), facts())[0], "policy_number")

        assert found.outcome is SignalOutcome.MATCH

    def test_a_transposed_digit_is_partial_and_says_so(self) -> None:
        """`CP-9038-64115` for `CP-9083-64115` — the commonest defect there is.

        An exact-string lookup answers "no such policy" and the officer is sent to the
        policy book for a policy that was in front of them.
        """
        found = signal(
            compare(notice(policy_number="CP-9038-64115"), facts(policy_number="CP-9083-64115"))[0],
            "policy_number",
        )

        assert found.outcome is SignalOutcome.PARTIAL
        assert "transposed" in found.explanation

    def test_an_unrelated_number_is_a_mismatch(self) -> None:
        found = signal(compare(notice(policy_number="ZZ-1111-22222"), facts())[0], "policy_number")

        assert found.outcome is SignalOutcome.MISMATCH
        assert found.score == 0.0

    def test_a_notice_with_no_number_reports_missing_not_mismatch(self) -> None:
        """A gap an officer can go and fill, which is not a fact against the policy."""
        found = signal(
            compare(notice(policy_number=None, broker_reference=None), facts())[0],
            "policy_number",
        )

        assert found.outcome is SignalOutcome.MISSING
        assert found.score is None
        assert "did not state" in found.explanation

    def test_a_wording_with_no_number_read_is_not_compared(self) -> None:
        """Different from missing: this is a signal that could not be evaluated here."""
        found = signal(compare(notice(), facts(policy_number=None))[0], "policy_number")

        assert found.outcome is SignalOutcome.NOT_COMPARED
        assert found.score is None

    def test_a_broker_reference_stands_in_for_an_absent_policy_number(self) -> None:
        """Brokers put their own scheme reference in the field a form labels "policy number"."""
        found = signal(
            compare(notice(policy_number=None, broker_reference="CP-4471-88210"), facts())[0],
            "policy_number",
        )

        assert found.outcome is SignalOutcome.MATCH


class TestInsured:
    def test_the_named_insured_matches(self) -> None:
        found = signal(compare(notice(), facts())[0], "insured_name")

        assert found.outcome is SignalOutcome.MATCH
        assert "the named insured" in found.explanation

    def test_an_additional_named_insured_matches_and_is_named(self) -> None:
        """The party reporting a commercial loss is often not the first named insured."""
        found = signal(
            compare(
                notice(insured_name="Ironbark Equipment Leasing, LLC"),
                facts(
                    insured_name="Ironbark Constructors, Inc.",
                    additional_insureds=(
                        "Ironbark Industrial Services, LLC",
                        "Ironbark Equipment Leasing, LLC",
                    ),
                ),
            )[0],
            "insured_name",
        )

        assert found.outcome is SignalOutcome.MATCH
        assert "additional named insured" in found.explanation
        assert "Ironbark Equipment Leasing, LLC" in found.explanation

    def test_a_group_company_is_partial_rather_than_a_match(self) -> None:
        """Similar enough to rank, not similar enough to bind.

        Two real entities, two real policies, and a notice that names neither precisely
        is the classic way commercial matching goes wrong — so the choice goes in front
        of the officer.
        """
        found = signal(
            compare(
                notice(insured_name="Sundale Property Group"),
                facts(insured_name="Sundale Property Group (Wisconsin), LLC"),
            )[0],
            "insured_name",
        )

        assert found.outcome is SignalOutcome.PARTIAL
        assert "one group" in found.explanation

    def test_a_different_company_is_a_mismatch_naming_the_real_insured(self) -> None:
        found = signal(
            compare(notice(insured_name="Larkspur Landscaping, LLC"), facts())[0], "insured_name"
        )

        assert found.outcome is SignalOutcome.MISMATCH
        assert "Harborline" in (found.policy_value or "")


class TestLocation:
    def test_an_exact_postcode_against_the_schedule_is_a_match(self) -> None:
        """The highest-value token an address carries."""
        found = signal(compare(notice(), facts())[0], "risk_location")

        assert found.outcome is SignalOutcome.MATCH
        assert found.score == 1.0
        assert "scheduled on this wording" in found.explanation

    def test_a_second_scheduled_premises_matches_too(self) -> None:
        """A loss at warehouse seven of twelve scores nothing against a mailing address."""
        found = signal(compare(notice(loss_postcode="19720"), facts())[0], "risk_location")

        assert found.outcome is SignalOutcome.MATCH
        assert found.policy_value == "19720"

    def test_the_risk_address_is_preferred_over_the_loss_address(self) -> None:
        """On a liability or construction notice the two differ.

        The schedule was written from the risk address.
        """
        query = notice(
            loss_postcode=None,
            risk_location="2870 Patapsco Industrial Parkway, Baltimore, MD 21226",
            loss_location="A layby on the A19",
        )

        assert query.any_location is not None
        assert "Patapsco" in query.any_location
        assert signal(compare(query, facts())[0], "risk_location").outcome is SignalOutcome.MATCH

    def test_an_unrelated_address_is_a_mismatch(self) -> None:
        found = signal(
            compare(
                notice(loss_postcode="98101", loss_location="14 Pike Street, Seattle, WA 98101"),
                facts(),
            )[0],
            "risk_location",
        )

        assert found.outcome is SignalOutcome.MISMATCH

    def test_a_wording_with_no_schedule_read_is_not_compared(self) -> None:
        found = signal(
            compare(notice(loss_postcode=None), facts(postcodes=(), locations=()))[0],
            "risk_location",
        )

        assert found.outcome is SignalOutcome.NOT_COMPARED


class TestPeriod:
    def test_a_loss_inside_the_term_matches_and_quotes_the_term(self) -> None:
        found = signal(compare(notice(), facts())[0], "policy_period")

        assert found.outcome is SignalOutcome.MATCH
        assert found.binary is True
        assert "01 Mar 2025 to 01 Mar 2026" in found.explanation

    def test_a_loss_before_inception_is_partial_not_a_mismatch(self) -> None:
        """Losses are discovered late.

        The useful answer is "you matched this year's wording, the loss is in last
        year's" rather than "no match".
        """
        found = signal(compare(notice(date_of_loss=date(2024, 11, 1)), facts())[0], "policy_period")

        assert found.outcome is SignalOutcome.PARTIAL
        assert "before this wording's inception" in found.explanation

    def test_a_loss_after_expiry_is_a_mismatch(self) -> None:
        found = signal(compare(notice(date_of_loss=date(2027, 1, 1)), facts())[0], "policy_period")

        assert found.outcome is SignalOutcome.MISMATCH

    def test_no_date_of_loss_is_missing(self) -> None:
        found = signal(compare(notice(date_of_loss=None), facts())[0], "policy_period")

        assert found.outcome is SignalOutcome.MISSING


class TestLineOfBusiness:
    def test_the_same_line_matches(self) -> None:
        assert signal(compare(notice(), facts())[0], "line_of_business").outcome is (
            SignalOutcome.MATCH
        )

    @pytest.mark.parametrize(
        ("notice_line", "policy_line"),
        [
            ("construction", "engineering"),
            ("liability", "casualty"),
            ("marine", "construction"),
        ],
    )
    def test_equivalent_lines_are_partial(self, notice_line: str, policy_line: str) -> None:
        """A construction all-risks wording and an engineering one answer each other's notices."""
        found = signal(
            compare(notice(line_of_business=notice_line), facts(line_of_business=policy_line))[0],
            "line_of_business",
        )

        assert found.outcome is SignalOutcome.PARTIAL

    def test_an_unrelated_line_is_a_mismatch(self) -> None:
        found = signal(compare(notice(line_of_business="motor"), facts())[0], "line_of_business")

        assert found.outcome is SignalOutcome.MISMATCH

    def test_an_unclassified_notice_is_missing_not_a_mismatch(self) -> None:
        found = signal(compare(notice(line_of_business="unknown"), facts())[0], "line_of_business")

        assert found.outcome is SignalOutcome.MISSING

    def test_the_filter_is_generous_where_the_score_is_not(self) -> None:
        """A wrong score is visible on the card; a wrong filter hides the policy.

        So `line_is_compatible` passes anything unknown and every equivalence.
        """
        assert line_is_compatible(None, "property") is True
        assert line_is_compatible("unknown", "property") is True
        assert line_is_compatible("property", None) is True
        assert line_is_compatible("construction", "engineering") is True
        assert line_is_compatible("motor", "property") is False


class TestSparseNotices:
    def test_the_mean_is_over_what_could_be_compared(self) -> None:
        """A notice with a policy number and nothing else is not punished for being sparse."""
        bare = NoticeQuery(policy_number="CP-4471-88210")
        results, mean = compare(bare, facts())

        assert mean == 1.0
        compared = [item for item in results if item.compared]
        assert [item.signal for item in compared] == ["policy_number"]

    def test_a_notice_with_nothing_to_compare_has_no_mean(self) -> None:
        results, mean = compare(NoticeQuery(), facts())

        assert mean is None
        assert all(not item.compared for item in results)


class TestRankingAndBands:
    def test_an_exact_number_inside_the_term_is_exact(self) -> None:
        result = match(notice(), [retrieved(facts())], config=LIMITS)

        assert len(result.matches) == 1
        best = result.matches[0]
        assert best.confidence is PolicyConfidence.EXACT
        assert best.recommended is True
        assert best.period_outcome is PolicyPeriodOutcome.IN_FORCE
        assert result.recommended is best

    def test_reasons_lead_with_the_agreements_and_name_the_facets(self) -> None:
        result = match(notice(), [retrieved(facts())], config=LIMITS)
        reasons = result.matches[0].reasons

        assert any("CP-4471-88210" in reason for reason in reasons)
        assert any("named insured" in reason for reason in reasons)
        assert any("answered the notification on" in reason for reason in reasons)

    def test_every_signal_is_published_including_the_ones_that_did_not_agree(self) -> None:
        """A candidate list printing only agreements is a sales pitch, not an audit trail."""
        result = match(
            notice(insured_name="Larkspur Landscaping, LLC"), [retrieved(facts())], config=LIMITS
        )
        outcomes = {item.signal: item.outcome for item in result.matches[0].signals}

        assert outcomes["insured_name"] is SignalOutcome.MISMATCH
        assert len(outcomes) == 7

    def test_retrieval_alone_cannot_reach_the_possible_band(self) -> None:
        """The rule that keeps a retrieval score from becoming a recommendation.

        A notice carrying nothing but a loss description has nothing to corroborate
        with, so the nearest-reading wordings are shown at `weak` — below the line,
        never acted on, and never recommended — however high the similarity. `weak`
        rather than nothing on purpose: an officer with no policy number and no insured
        name has only the prose to go on, and the honest answer is "these read closest,
        and that is all we know".
        """
        result = match(
            NoticeQuery(loss_description="a fire"), [retrieved(facts(), score=1.0)], config=LIMITS
        )

        assert len(result.matches) == 1
        weak = result.matches[0]
        assert weak.confidence is PolicyConfidence.WEAK
        assert weak.recommended is False
        assert weak.corroboration_score is None
        # The cap is what makes the band unreachable, not the retrieval score.
        assert weak.score <= LIMITS.match_possible_threshold
        assert result.recommended is None

    def test_period_and_line_agreement_alone_is_rejected(self) -> None:
        """The defect the four `NO_MATCH` notices exposed.

        Every in-force property policy agrees with a property loss on the date and the
        line of business. Two agreements, an axis each, and a retrieval score
        normalised to 1.0 computed to a confident-looking 0.56.
        """
        stranger = notice(
            policy_number=None,
            broker_reference=None,
            insured_name="Larkspur Landscaping, LLC",
            loss_postcode="20904",
            loss_location="8890 Cherry Hill Road, Silver Spring, MD 20904",
        )

        result = match(stranger, [retrieved(facts())], config=LIMITS)

        assert result.matches == []
        rejected = result.rejected[0]
        agreed = {item.signal for item in rejected.signals if item.agreed}
        assert agreed <= DESCRIPTIVE_SIGNALS
        assert agreed  # something *did* agree — which is exactly why the gate is needed

    def test_a_quoted_number_that_disagrees_with_everything_is_rejected(self) -> None:
        result = match(
            notice(
                policy_number="CP-6650-11223",
                insured_name="Meadowcrest Assisted Living",
                loss_postcode="46032",
                loss_location="1400 Meadowcrest Drive, Carmel, IN 46032",
            ),
            [retrieved(facts())],
            config=LIMITS,
        )

        assert result.matches == []

    def test_an_identity_conflict_caps_the_band_and_warns(self) -> None:
        """A wording naming a different company is at best a lead.

        Capped by rule rather than by arithmetic, and the officer is told why.
        """
        conflicted = notice(insured_name="Larkspur Landscaping, LLC")
        result = match(conflicted, [retrieved(facts())], config=LIMITS)

        assert result.matches, "the policy number still agrees, so this is a candidate"
        best = result.matches[0]
        assert best.confidence is not PolicyConfidence.EXACT
        assert best.recommended is False
        assert any(warning.code == "identity_conflict" for warning in best.warnings)

    def test_two_close_candidates_recommend_neither(self) -> None:
        """Ambiguity is an outcome to surface, not one to tie-break.

        Two companion policies of one insured is precisely when the officer has a
        choice, and pre-selecting one hides that it existed.
        """
        cgl = facts(
            policy_number="GL-8804-27153",
            policy_type="Commercial General Liability",
            line_of_business="liability",
            insured_name="Beacon Mechanical Services, Inc.",
        )
        floater = facts(
            policy_number="IM-7741-15530",
            policy_type="Contractors Equipment and Installation Floater",
            line_of_business="marine",
            insured_name="Beacon Mechanical Services, Inc.",
        )
        query = notice(
            policy_number=None,
            broker_reference=None,
            insured_name="Beacon Mechanical Services, Inc.",
            line_of_business=None,
            loss_postcode="21226",
        )

        result = match(query, [retrieved(cgl), retrieved(floater)], config=LIMITS)

        assert len(result.matches) == 2
        assert all(item.recommended is False for item in result.matches)
        assert result.recommended is None
        assert result.ambiguous is True

    def test_a_clear_leader_is_recommended_over_a_distant_second(self) -> None:
        other = facts(
            policy_number="ZZ-0000-00000",
            insured_name="Somebody Else Ltd",
            postcodes=(),
            locations=(),
            line_of_business="motor",
        )

        result = match(notice(), [retrieved(facts()), retrieved(other, score=0.4)], config=LIMITS)

        assert result.matches[0].recommended is True
        assert result.ambiguous is False

    def test_rejected_candidates_never_appear_in_matches(self) -> None:
        """A candidate list must not contradict itself on screen.

        The score gets a wording over the weak threshold; the ladder decides whether it
        is a candidate at all, so partitioning on the score alone put rows carrying the
        word "rejected" into the candidate list.
        """
        result = match(
            NoticeQuery(loss_description="a fire somewhere"),
            [retrieved(facts()), retrieved(facts(policy_number="ZZ-1-1"), score=0.9)],
            config=LIMITS,
        )

        assert all(item.confidence is not PolicyConfidence.REJECTED for item in result.matches)
        assert all(item.confidence is PolicyConfidence.REJECTED for item in result.rejected)

    def test_results_are_capped_and_ranked_from_one(self) -> None:
        many = [retrieved(facts(policy_number=f"CP-0000-0000{index}")) for index in range(9)]
        result = match(notice(), many, config=PolicyLibrarySettings(match_max_results=3))

        assert len(result.matches) <= 3
        assert [item.rank for item in result.matches] == list(range(1, len(result.matches) + 1))


class TestReporting:
    def test_the_two_scores_are_reported_separately(self) -> None:
        """One is "this wording reads like this loss"; the other is "this is the contract".

        An officer who can see both reads a 0.7 built from prose very differently from a
        0.7 built from a policy number.
        """
        best = match(notice(), [retrieved(facts(), score=0.55)], config=LIMITS).matches[0]

        assert best.retrieval_score == pytest.approx(0.55, abs=0.01)
        assert best.corroboration_score is not None
        assert best.corroboration_score != best.retrieval_score

    def test_the_notice_reports_how_many_identifying_values_it_gave(self) -> None:
        """A candidate list is only as good as what was fed to it."""
        assert notice().identifying_values == 3  # number, insured, location
        assert NoticeQuery().identifying_values == 0

        result = match(notice(), [retrieved(facts())], config=LIMITS)
        assert result.signals_available == 3

    def test_excerpts_travel_with_the_match(self) -> None:
        best = match(notice(), [retrieved(facts(), excerpts=3)], config=LIMITS).matches[0]

        assert len(best.excerpts) == 3
        assert all(excerpt.page_number is not None for excerpt in best.excerpts)
        assert best.chunks_matched == 3

    def test_an_unlinked_wording_warns_that_no_limit_check_ran(self) -> None:
        best = match(notice(), [retrieved(facts(policy_id=None))], config=LIMITS).matches[0]

        assert any(warning.code == "not_linked_to_book" for warning in best.warnings)

    def test_an_out_of_period_wording_is_shown_with_a_warning_never_hidden(self) -> None:
        """ "This is your policy and the loss is outside it" is a coverage conversation."""
        best = match(
            notice(date_of_loss=date(2027, 6, 1)), [retrieved(facts())], config=LIMITS
        ).matches[0]

        assert best.period_outcome is PolicyPeriodOutcome.OUTSIDE_PERIOD
        assert any(warning.code == "outside_policy_period" for warning in best.warnings)
        assert "may still be the right contract" in next(
            warning.detail for warning in best.warnings if warning.code == "outside_policy_period"
        )

    def test_a_line_mismatch_warns_without_stating_a_coverage_decision(self) -> None:
        """Warnings say "may not", never "not covered"."""
        best = match(notice(line_of_business="motor"), [retrieved(facts())], config=LIMITS).matches[
            0
        ]
        warning = next(item for item in best.warnings if item.code == "line_of_business_mismatch")

        assert "may not be" in warning.detail
        assert "not covered" not in warning.detail

    def test_an_empty_library_returns_an_empty_answer_rather_than_a_guess(self) -> None:
        result = match(notice(), [], config=LIMITS)

        assert result.matches == []
        assert result.rejected == []
        assert result.documents_compared == 0
        assert result.recommended is None
        assert result.ambiguous is False

    def test_the_engine_version_is_stamped_on_the_result(self) -> None:
        """So a score can be read back against the engine that produced it."""
        assert match(notice(), [retrieved(facts())], config=LIMITS).engine_version
