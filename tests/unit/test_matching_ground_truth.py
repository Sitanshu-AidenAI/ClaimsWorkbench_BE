"""The answer key, wired to a check that fails when the matcher moves.

`case_data/_ground_truth/MATCHING_GROUND_TRUTH.json` has stated the expected policy
for all 24 packs since they were generated and was read by nothing — not a test, not a
script. A weight, a comparator or a rung of the confidence ladder could have changed
every one of those answers and the suite would have stayed green.

Three things are asserted, in order of how much they matter:

1. **Nothing is bound to the wrong contract.** No pack may recommend a policy the key
   does not name. This is the only failure here that would cause real harm.
2. **The right policy is top-ranked, on every pack.** Currently 24 of 24 — an exact
   figure rather than a floor, because a matcher that used to find all of them and now
   finds 23 has regressed whether or not 23 clears a threshold.
3. **The confidence bands.** Four packs sit in a band the key does not state, and the
   four are named. Named rather than tolerated by a count: the point of the list is
   that a fifth cannot appear without this test failing and somebody deciding whether
   it is a finding or a fixture change.

The eval is pure — `scripts/matching_eval.py` builds both sides from the two JSON
files — so it belongs in the unit suite, which is the only place a regression is caught
before it ships. See `scripts/eval_policy_matching.py` for the readable report.
"""

from __future__ import annotations

import sys
from pathlib import Path

import pytest

from app.core.config import FNOLSettings
from app.domain.enums import PolicyConfidence

sys.path.insert(0, str(Path(__file__).resolve().parents[2] / "scripts"))

from matching_eval import (
    CONFIDENCE_WORDS,
    NO_MATCH,
    evaluate,
    load_ground_truth,
    load_policy_book,
    notice_from_row,
)

CONFIG = FNOLSettings()

#: How many packs the key holds. Stated so a truncated or half-regenerated file is a
#: failure rather than a smaller, quietly easier eval.
EXPECTED_PACKS = 24

#: Packs whose confidence band differs from the key, and why each one does.
#:
#: Some of this is the harness rather than the matcher, and saying which is the point
#: of writing it down. This eval feeds the field *values* the key states, which is a
#: cleaner notice than the pack's own documents — no OCR, no missing fields, no broker
#: prose. A pack the key marks `possible` because its real notice is thin can honestly
#: score `strong` on inputs that good, and `rivergate-copper-theft` scoring 1.0 is that
#: case exactly. `kestrel-ridge-copper-theft` is the interesting one in the other
#: direction: the key expects `strong` on a notice with no policy number, carried by
#: the broker reference and the insured name, and the ladder currently says `possible`.
KNOWN_BAND_DIVERGENCES: dict[str, tuple[PolicyConfidence, PolicyConfidence]] = {
    # Under-confident: the key's `strong` rests on corroboration this harness does not
    # feed it (the managing agent as scheduled additional insured, the sender domain).
    "kestrel-ridge-copper-theft": (PolicyConfidence.STRONG, PolicyConfidence.POSSIBLE),
    # Over-confident, and all three for the same reason: the key rates these `possible`
    # on the strength of a thin real notice, and the harness hands over every field.
    "cherry-creek-water-damage": (PolicyConfidence.POSSIBLE, PolicyConfidence.STRONG),
    "kestrel-ridge-vehicle-impact": (PolicyConfidence.POSSIBLE, PolicyConfidence.STRONG),
    "rivergate-copper-theft": (PolicyConfidence.POSSIBLE, PolicyConfidence.STRONG),
}


@pytest.fixture(scope="module")
def report():  # type: ignore[no-untyped-def]
    return evaluate(config=CONFIG)


class TestTheAnswerKeyIsUsable:
    """The key and the book have to agree before anything measured against them means much."""

    def test_the_key_holds_every_pack_and_names_a_band_for_each(self) -> None:
        rows = load_ground_truth()
        assert len(rows) == EXPECTED_PACKS
        assert len({row["pack"] for row in rows}) == EXPECTED_PACKS
        for row in rows:
            assert row["match_confidence"] in CONFIDENCE_WORDS, row["pack"]

    def test_every_expected_policy_exists_in_the_book(self) -> None:
        """Drift between the key and the book, caught here rather than as a mystery miss."""
        numbers = {policy.policy_number for policy in load_policy_book()}
        for row in load_ground_truth():
            expected = row["expected_matched_policy_number"]
            if expected == NO_MATCH:
                continue
            assert expected in numbers, f"{row['pack']} expects {expected}, absent from the book"

    def test_the_notice_built_from_a_row_never_carries_the_answer(self) -> None:
        """The one way an eval like this lies to itself.

        A `NO_MATCH` pack must not arrive at the matcher holding a policy number, and
        no pack's signals may quote the expected number unless the notice genuinely
        states it. Cheap to check and impossible to notice going wrong.
        """
        for row in load_ground_truth():
            notice = notice_from_row(row)
            stated = notice.policy_number.value if notice.policy_number else None
            assert stated == (row["mentioned_policy_number"] or None), row["pack"]
            if row["expected_matched_policy_number"] == NO_MATCH:
                assert stated is None or stated != row["expected_matched_policy_number"]


class TestTheMatcherAgainstTheKey:
    def test_no_pack_is_bound_to_a_policy_the_key_does_not_name(self, report) -> None:  # type: ignore[no-untyped-def]
        """The failure that matters. Everything else here is accuracy; this is harm.

        Includes the four `NO_MATCH` packs, which is where it earns its place: an
        insured who is not in the book must come back as nothing, not as the nearest
        contract that happened to score.
        """
        wrong = {
            outcome.pack: outcome.recommended_policy_number
            for outcome in report.outcomes
            if outcome.recommended_policy_number is not None
            and outcome.recommended_policy_number != outcome.expected_policy_number
        }
        assert wrong == {}

    def test_the_right_policy_is_top_ranked_on_every_pack(self, report) -> None:  # type: ignore[no-untyped-def]
        misses = {
            outcome.pack: outcome.matched_policy_number
            for outcome in report.outcomes
            if not outcome.top_correct
        }
        assert misses == {}
        assert report.top_correct == EXPECTED_PACKS

    def test_the_recommendation_matches_the_key_on_all_but_one_pack(self, report) -> None:  # type: ignore[no-untyped-def]
        """23 of 24, and the one is named.

        `kestrel-ridge-copper-theft` ranks the right policy first and declines to
        recommend it, because the ladder bands it `possible` where the key says
        `strong`. That is a real finding about the ladder on a notice with no policy
        number, and it is recorded rather than smoothed into a threshold.
        """
        misses = {outcome.pack for outcome in report.failures()}
        assert misses == {"kestrel-ridge-copper-theft"}

    def test_the_confidence_bands_diverge_on_exactly_the_four_known_packs(self, report) -> None:  # type: ignore[no-untyped-def]
        divergences = {
            outcome.pack: (outcome.expected_confidence, outcome.matched_confidence)
            for outcome in report.outcomes
            if not outcome.confidence_correct
        }
        assert divergences == KNOWN_BAND_DIVERGENCES

    def test_every_pack_with_a_stated_policy_number_reaches_exact(self, report) -> None:  # type: ignore[no-untyped-def]
        """The heaviest signal, checked on its own.

        Eight packs quote the policy number on the notice. Every one of them must land
        on `exact`: if a stated, correct, unambiguous number does not, the weighting is
        broken in a way none of the aggregate figures above would isolate.
        """
        stated = {
            row["pack"]
            for row in load_ground_truth()
            if row["mentioned_policy_number"]
            and row["mentioned_policy_number"] == row["expected_matched_policy_number"]
        }
        assert stated, "the key no longer has a pack that quotes its own policy number"
        for outcome in report.outcomes:
            if outcome.pack in stated:
                assert outcome.matched_confidence is PolicyConfidence.EXACT, outcome.pack
