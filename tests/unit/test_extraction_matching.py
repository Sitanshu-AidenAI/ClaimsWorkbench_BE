"""Finding a value in text that writes it differently.

Two things are being pinned here, and both are load-bearing for citations:

* an offset handed back is an offset into the **original** string, whatever the
  normaliser did to the copy it searched — every quote, page lookup and rectangle
  downstream is derived from that number;
* "distinctive enough to prove something" is a rule with edges, and the edges are
  where a corroboration list stops being evidence and becomes noise.
"""

from __future__ import annotations

from app.services.extraction.matching import (
    NormalisedText,
    collapse,
    searchable,
    variants,
)


class TestNormalisedText:
    def test_a_phrase_split_across_a_line_is_still_found(self) -> None:
        raw = "Loss location: 2870 Patapsco\nIndustrial   Park, Baltimore"
        found = NormalisedText.of(raw).find("2870 Patapsco Industrial Park")

        assert found is not None
        start, end = found
        # The span is into the original, line break and double space included.
        assert raw[start:end] == "2870 Patapsco\nIndustrial   Park"

    def test_matching_ignores_case_and_the_span_keeps_the_original(self) -> None:
        raw = "Policy Number: CP-4471-88210"
        found = NormalisedText.of(raw).find("cp-4471-88210")

        assert found is not None
        assert raw[found[0] : found[1]] == "CP-4471-88210"

    def test_leading_and_trailing_whitespace_do_not_shift_the_offsets(self) -> None:
        raw = "\n\n   Insured: Harborline Cold Storage   \n"
        found = NormalisedText.of(raw).find("Harborline Cold Storage")

        assert found is not None
        assert raw[found[0] : found[1]] == "Harborline Cold Storage"

    def test_occurrences_are_returned_in_order_and_do_not_overlap(self) -> None:
        raw = "aa aa aa"
        spans = NormalisedText.of(raw).find_all("aa", limit=10)

        assert spans == [(0, 2), (3, 5), (6, 8)]

    def test_the_limit_is_respected(self) -> None:
        spans = NormalisedText.of("x x x x x").find_all("x", limit=2)

        assert len(spans) == 2

    def test_a_needle_that_is_not_there_finds_nothing(self) -> None:
        assert NormalisedText.of("Policy CP-1").find("CP-2") is None

    def test_an_empty_needle_or_text_finds_nothing(self) -> None:
        assert NormalisedText.of("something").find("   ") is None
        assert NormalisedText.of("").find("anything") is None
        assert not NormalisedText.of("")

    def test_a_character_that_lowercases_to_two_keeps_the_map_aligned(self) -> None:
        # "İ".lower() is two code points. A naive normaliser emits both and every
        # offset after it is wrong by one — which is a rectangle drawn round the
        # neighbouring word, for every document containing one Turkish capital I.
        raw = "İstanbul depot: policy CP-9911-002"
        found = NormalisedText.of(raw).find("CP-9911-002")

        assert found is not None
        assert raw[found[0] : found[1]] == "CP-9911-002"

    def test_find_any_takes_the_first_form_that_matches(self) -> None:
        text = NormalisedText.of("Date of loss: 2026-01-10")

        found = text.find_any(["10 January 2026", "2026-01-10"])

        assert found is not None
        assert found[0] == len("Date of loss: ")

    def test_find_any_with_nothing_matching_is_none(self) -> None:
        assert NormalisedText.of("nothing here").find_any(["a-1", "b-2"]) is None

    def test_collapse_is_what_the_copy_is_normalised_to(self) -> None:
        assert collapse("  Two   Words\n") == "two words"
        assert collapse(None) == ""


class TestSearchable:
    def test_a_reference_is_searchable(self) -> None:
        assert searchable("CP-4471-88210")
        assert searchable("TR/PROP/2025/4471")

    def test_a_short_reference_mixing_letters_and_digits_is_searchable(self) -> None:
        # Under the character floor, but a letter-and-digit mix is a reference
        # rather than a word.
        assert searchable("A/42")

    def test_a_count_proves_nothing(self) -> None:
        assert not searchable("0", data_type="integer")
        assert not searchable("2", data_type="integer")

    def test_a_short_word_proves_nothing(self) -> None:
        assert not searchable("USD")
        assert not searchable("Yes")

    def test_a_number_needs_enough_digits_to_be_distinctive(self) -> None:
        assert not searchable("12", data_type="number")
        assert searchable("3,700,000", data_type="money")

    def test_a_boolean_or_a_json_blob_is_never_searchable(self) -> None:
        assert not searchable("true", data_type="boolean")
        assert not searchable('[{"role":"broker"}]', data_type="json")

    def test_a_paragraph_is_not_searched_for(self) -> None:
        # A second document paraphrases a narrative rather than repeating it, so
        # the search is a scan of every file that cannot succeed.
        assert not searchable("word " * 100)

    def test_nothing_is_not_searchable(self) -> None:
        assert not searchable(None)
        assert not searchable("   ")


class TestVariants:
    def test_the_document_s_own_wording_comes_first(self) -> None:
        forms = variants("3,700,000", data_type="money", typed=370000000)

        assert forms[0] == "3,700,000"

    def test_a_date_offers_the_forms_a_document_prints(self) -> None:
        forms = variants("10 January 2026", data_type="date", typed="2026-01-10")

        assert "10 january 2026" in forms
        assert "2026-01-10" in forms
        assert "10/01/2026" in forms
        assert "january 10, 2026" in forms
        assert "10 jan 2026" in forms

    def test_a_timestamp_resolves_to_its_date_forms(self) -> None:
        forms = variants("overnight on Friday", data_type="datetime", typed="2026-01-10T04:20:00")

        assert "2026-01-10" in forms

    def test_money_offers_separated_and_unseparated_forms(self) -> None:
        forms = variants("GBP 1,410,000", data_type="money", typed=141000000)

        assert "1,410,000" in forms
        assert "1410000" in forms

    def test_money_with_pence_offers_both(self) -> None:
        forms = variants("4,000.50", data_type="money", typed=400050)

        assert "4,000.50" in forms
        assert "4000.50" in forms

    def test_a_value_that_proves_nothing_yields_no_forms(self) -> None:
        assert variants("0", data_type="integer", typed=0) == ()
        assert variants("Yes", data_type="boolean", typed=True) == ()

    def test_a_typed_value_that_cannot_be_read_leaves_the_wording(self) -> None:
        forms = variants("sometime in March", data_type="date", typed=None)

        assert forms == ("sometime in march",)

    def test_forms_are_deduplicated(self) -> None:
        # Day 10 renders the same padded and unpadded, so the two collapse.
        forms = variants("10 January 2026", data_type="date", typed="2026-01-10")

        assert len(forms) == len(set(forms))

    def test_nothing_yields_nothing(self) -> None:
        assert variants(None) == ()
