"""The deterministic rules, run against the fixtures built to stress them.

`case_data/` holds 24 packs and each one's README says what it is for — "should not
be confused with", "built to exercise catastrophe attribution", "a three-way tie on a
broker's programme list". Those sentences were the specification and nothing checked
them, so several of the packs could not have done the job they were written for: the
catastrophe fixtures had no event in the seed that could match, and the duplicate pair
scored 0.6292 against a 0.62 threshold and landed as a review candidate.

No database and no model. The scenario definitions in `scripts/fnol_scenarios.py` are
the same source `case_data/` and its answer key are generated from, so a test that
reads them is testing the real fixture text at the point the rules see it — which is
the level the rules can actually be pinned at. `tests/integration/test_fnol_flow.py`
covers the pipeline that carries the text there.
"""

from __future__ import annotations

import sys
from datetime import UTC, datetime
from pathlib import Path
from types import SimpleNamespace
from typing import Any

import pytest

from app.core.config import FNOLSettings
from app.db.seed import CAT_EVENTS
from app.domain import catastrophe, duplicates, matching
from app.domain.assessment import claim_signals
from app.domain.enums import LineOfBusiness
from app.domain.heuristics import classify_from_text, extract_from_text, loss_clauses
from app.domain.rules import ClaimSignal, required_fields

sys.path.insert(0, str(Path(__file__).resolve().parents[2] / "scripts"))

from fnol_scenarios import SPECS

CONFIG = FNOLSettings()

#: Every pack, by slug. The fixtures are the input to this whole file.
SCENARIOS: dict[str, Any] = {spec["scenario"].slug: spec["scenario"] for spec in SPECS}

#: The seeded events, shaped like the rows the repository would return.
SEEDED_EVENTS = [SimpleNamespace(id=payload["reference"], **payload) for payload in CAT_EVENTS]


def parse_day(value: str) -> datetime:
    return datetime.strptime(value, "%d %B %Y").replace(tzinfo=UTC)


def case_from(slug: str, **overrides: Any) -> SimpleNamespace:
    """One pack as the columns the rules read.

    Only the fields the scenario states. A test that filled in the gaps would be
    measuring its own generosity rather than the fixture.
    """
    scenario = SCENARIOS[slug]
    base: dict[str, Any] = {
        "id": slug,
        "reference": f"FNOL-{slug}",
        "policy_number": scenario.policy_number,
        "policy_id": None,
        "insured_name": scenario.insured,
        "claimant_name": scenario.claimant,
        "date_of_loss": parse_day(scenario.date_of_loss),
        "loss_location": scenario.loss_location,
        "loss_country": scenario.loss_country,
        "loss_latitude": None,
        "loss_longitude": None,
        "loss_description": scenario.description,
        "cause_of_loss": scenario.cause,
        "loss_type": None,
        "affected_assets": None,
        "authorities_involved": scenario.authorities,
        # The broker's covering narrative, which is where a real notice states the
        # things no labelled field on the form has a box for.
        "source_body": scenario.email_narrative,
        "injuries": int(scenario.injuries) if scenario.injuries.isdigit() else None,
        "fatalities": int(scenario.fatalities) if scenario.fatalities.isdigit() else None,
        "business_interruption": None,
        "structural_damage": None,
        "environmental_exposure": None,
        "potential_litigation": None,
        "external_reference": None,
        "line_of_business": None,
    }
    base.update(overrides)
    return SimpleNamespace(**base)


# ---------------------------------------------------------------------------
# 07 — the duplicate pair
# ---------------------------------------------------------------------------


class TestTheWindrowGrovePair:
    """Two claims, one policy, one street address, seven months and one peril apart.

    `case_data/windrow-grove-hail/README.md` and its Building K counterpart say in as
    many words that these should not be confused. The only test of the rule compared
    two claims in *different cities*, which the comparator would separate on tokens
    alone — so the shape the fixtures were written to stress was never exercised.
    """

    def test_they_are_not_a_duplicate_candidate(self) -> None:
        hail = case_from("windrow-grove-hail")
        fire = case_from("windrow-grove-building-k-fire")

        # The premise: everything that identifies the client agrees. If this stops
        # being true the fixtures have changed and the test below proves nothing.
        assert hail.insured_name == fire.insured_name
        assert hail.claimant_name == fire.claimant_name

        result = duplicates.compare(hail, fire, kind="fnol", reference=fire.reference)
        assert not duplicates.is_duplicate_candidate(result, config=CONFIG)
        assert result.score < CONFIG.duplicate_similarity_threshold

    def test_the_building_suffix_is_what_separates_them(self) -> None:
        """Named directly, because it is the one signal that was reading 1.0.

        Every token of the hail notice's address is in the fire notice's address, so
        containment said "Same location" about a claim in Building K and a claim on
        the roofs of six other buildings.
        """
        hail = case_from("windrow-grove-hail")
        fire = case_from("windrow-grove-building-k-fire")
        assert "Building K" in fire.loss_location
        assert "Building" not in hail.loss_location

        overlap = matching.token_containment(
            matching.tokens(hail.loss_location), matching.tokens(fire.loss_location)
        )
        assert overlap == pytest.approx(1.0)
        assert matching.location_similarity(hail.loss_location, fire.loss_location) < 1.0

    def test_the_same_building_named_twice_is_still_the_same_place(self) -> None:
        """The correction must not fire on two renderings of one address."""
        assert matching.location_similarity(
            "6120 East 91st Street, Tulsa, OK 74137 — Building K",
            "Building K, 6120 East 91st Street, Tulsa OK",
        ) == pytest.approx(1.0)


# ---------------------------------------------------------------------------
# 06 — catastrophe attribution
# ---------------------------------------------------------------------------


class TestTheCatastropheFixtures:
    """Three US packs built to exercise attribution, against the seeded events.

    Before the events below existed the seed held exactly two, both in the United
    Kingdom, and all three of these packs matched nothing — while `cypress-landing-
    haboob` avoided a *peril* mismatch only because "storm" is a substring of the
    "windstorm" in its cause text. A vocabulary gap covered for by a substring is not
    coverage, and it would have gone the other way on "haboob" alone.
    """

    @pytest.mark.parametrize(
        ("slug", "reference"),
        [
            ("harborline-delaware-freeze", "CAT-2026-011"),
            ("windrow-grove-hail", "CAT-2026-012"),
            ("cypress-landing-haboob", "CAT-2026-013"),
        ],
    )
    def test_each_one_is_attributed_to_its_event(self, slug: str, reference: str) -> None:
        match = catastrophe.best_match(case_from(slug), SEEDED_EVENTS, config=CONFIG)
        assert match is not None, f"{slug} matched no seeded catastrophe event"
        assert match.reference == reference
        assert match.confidence >= CONFIG.cat_match_threshold

    @pytest.mark.parametrize(
        "slug",
        [
            # Same address as the hail pack and inside no event window.
            "windrow-grove-building-k-fire",
            # A theft, which is not weather anywhere.
            "kestrel-ridge-copper-theft",
            # A trench collapse in Kansas, nowhere near any seeded event.
            "cobalt-ridge-trench-collapse",
        ],
    )
    def test_a_loss_that_is_not_the_event_is_not_attributed(self, slug: str) -> None:
        assert catastrophe.best_match(case_from(slug), SEEDED_EVENTS, config=CONFIG) is None

    def test_the_peril_vocabulary_covers_the_words_the_fixtures_use(self) -> None:
        """The terms themselves, so a fixture reworded to "derecho" still matches."""
        terms = {term for terms in catastrophe.EVENT_PERIL_TERMS.values() for term in terms}
        assert {"haboob", "microburst", "derecho", "straight-line wind"} <= terms


# ---------------------------------------------------------------------------
# 02 and 04 — the trench collapse
# ---------------------------------------------------------------------------


class TestTheCobaltRidgeTrenchCollapse:
    """The pack that produced a three-way classification tie on a broker's aside.

    Its notice contains "their own placements are the ones engaged: liability, excess,
    workers compensation, contractors equipment, motor and possibly contractors
    pollution" — five products named, nothing said about the loss — and the tally read
    `liability`, `casualty` and `construction` out of it at one hit each. Whichever
    won the tie decided whether the claim was asked for third-party details and an
    injury count, on a loss with two hospitalised workers and $1.15m of third-party
    exposure.
    """

    def test_the_required_fields_do_not_depend_on_the_tie(self) -> None:
        case = case_from("cobalt-ridge-trench-collapse")
        signals = claim_signals(case, party_roles=set())
        assert ClaimSignal.INJURIES in signals
        assert ClaimSignal.THIRD_PARTY in signals

        # `None` for the line: whatever the tie resolved to, and deliberately not
        # any of the three it was tied between. Both fields below were previously
        # reachable only through whichever one won.
        paths = {requirement.path: requirement for requirement in required_fields(None, signals)}
        assert "parties.third_parties" in paths
        assert paths["loss.injuries"].critical

        # And they are still there under each of the three lines that tied, so the
        # tie-break can no longer change what the claim is asked for.
        for line in (
            LineOfBusiness.LIABILITY,
            LineOfBusiness.CASUALTY,
            LineOfBusiness.CONSTRUCTION,
        ):
            under = {requirement.path for requirement in required_fields(line, signals)}
            assert {"loss.injuries", "parties.third_parties"} <= under, line

    def test_the_broker_programme_list_is_not_read_as_the_loss(self) -> None:
        """Five products named in an aside, and none of them a fact about the loss."""
        narrative = SCENARIOS["cobalt-ridge-trench-collapse"].email_narrative
        assert "workers compensation, contractors equipment, motor" in narrative

        kept = loss_clauses(narrative.lower())
        assert "placements are the ones engaged" not in kept
        # The clause the words came from is gone; the account of the collapse is not.
        assert "buried to chest height" in kept

        result = classify_from_text(narrative)
        assert result.line_of_business != LineOfBusiness.MOTOR.value

    def test_the_prose_counts_three_casualties_and_not_one(self) -> None:
        """The scenario states 3, and the text-only reader has to agree.

        The reader used to answer 1 for this notice: the counts are spelled out and
        the third casualty is named by an ordinal, so no digit sat beside the word
        "injury". One injury does not clear the multiple-injury severity floor and
        three does, which is the whole cost of the difference.
        """
        scenario = SCENARIOS["cobalt-ridge-trench-collapse"]
        assert scenario.injuries == "3"

        read = extract_from_text(scenario.email_narrative)
        assert read.loss.injuries.value == "3"
        # "No fatalities" is a stated fact, not an absence — nobody died, and the
        # notice says so, which is a different reading from silence.
        assert read.loss.fatalities.value == "0"
