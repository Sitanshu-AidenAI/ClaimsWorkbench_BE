"""Reading a date of loss out of a notice.

`app.domain.temporal` is the only thing standing between the words a broker wrote
and the timestamp a claim is created from, so the properties tested here are the
ones the rest of the pipeline assumes without re-checking:

* a date stated in full is read exactly, however much prose surrounds it;
* a *discovery* date is never read as the date of loss;
* words that only mean something next to the notice's own date are read against
  the notice's own date;
* anything that cannot be justified comes back as nothing, with a reason.

The last class in the file is the one that would have caught the bug this module
was written for: every notice in `case_data` in one table, read as the extracting
model returns it, against the arrival date of the email that carried it.
"""

from __future__ import annotations

from datetime import UTC, datetime

import pytest

from app.domain import normalisation, temporal
from app.domain.temporal import Basis

#: A Tuesday, 10:04 UTC. Chosen so that "Friday" is unambiguously four days back
#: and "yesterday" does not cross a month or a year boundary.
NOTICE = datetime(2026, 5, 5, 10, 4, tzinfo=UTC)


def read(text: str, *, notice: datetime = NOTICE, **kwargs: object) -> temporal.Reading:
    return temporal.resolve(text, reference=notice, **kwargs)  # type: ignore[arg-type]


class TestDatesStatedInFull:
    """A date the document prints, in the forms documents print it in."""

    @pytest.mark.parametrize(
        ("text", "expected"),
        [
            ("2 May 2026", "2026-05-02T00:00:00+00:00"),
            ("02 May 2026", "2026-05-02T00:00:00+00:00"),
            ("2nd May 2026", "2026-05-02T00:00:00+00:00"),
            ("2 Sept 2025", "2025-09-02T00:00:00+00:00"),
            ("May 2, 2026", "2026-05-02T00:00:00+00:00"),
            ("2026-05-02", "2026-05-02T00:00:00+00:00"),
            ("2026-05-02T02:00:00", "2026-05-02T02:00:00+00:00"),
            ("02/05/2026", "2026-05-02T00:00:00+00:00"),
            ("2.5.2026", "2026-05-02T00:00:00+00:00"),
            ("02/05/26", "2026-05-02T00:00:00+00:00"),
        ],
    )
    def test_the_forms_a_notice_prints_a_date_in(self, text: str, expected: str) -> None:
        reading = read(text)
        assert reading.value == datetime.fromisoformat(expected)
        assert reading.basis is Basis.STATED

    @pytest.mark.parametrize(
        "text",
        [
            "Date of loss: 2 May 2026",
            "Friday 2 May 2026",
            "the loss occurred on 2 May 2026 at the insured's depot",
            "2 May 2026 (per the attached incident report)",
        ],
    )
    def test_prose_around_a_date_does_not_hide_it(self, text: str) -> None:
        """The bug this module replaced.

        The reader before it matched a format list against the *whole* string, so
        a weekday in front of the date or a time behind it was enough to make a
        notice read as stating no date at all — and `fnol_cases.date_of_loss`
        stayed null on a notice that plainly said when the loss happened.
        """
        assert read(text).value == datetime(2026, 5, 2, tzinfo=UTC)

    def test_a_stated_date_explains_nothing_because_there_is_nothing_to_explain(self) -> None:
        assert read("2 May 2026").note is None


class TestTimes:
    """The clock, and the words used instead of one."""

    @pytest.mark.parametrize(
        ("text", "hour", "minute"),
        [
            ("2 May 2026 21:04", 21, 4),
            ("2 May 2026, 09:34 CDT", 9, 34),
            ("2 May 2026 at 3pm", 15, 0),
            ("2 May 2026 at 3.30pm", 15, 30),
            ("2 May 2026, 12:15am", 0, 15),
            ("2 May 2026, from about 19:30", 19, 30),
            ("2 May 2026, between 02:00 and 05:30", 2, 0),
        ],
    )
    def test_a_stated_time_is_kept_as_stated(self, text: str, hour: int, minute: int) -> None:
        """Including the timezone the document names, which is deliberately ignored.

        `21:04 EDT` is stored as 21:04. Converting to true UTC would move an
        evening loss onto the following calendar day, and the calendar day is what
        the policy-period check, the catastrophe window and the claim all read.
        """
        reading = read(text)
        assert reading.value == datetime(2026, 5, 2, hour, minute, tzinfo=UTC)
        assert reading.time_stated is True

    @pytest.mark.parametrize(
        ("text", "hour"),
        [
            ("2 May 2026, overnight", 22),
            ("2 May 2026, in the early hours", 3),
            ("2 May 2026, morning", 9),
            ("2 May 2026, the afternoon", 14),
            ("2 May 2026, in the evening", 19),
        ],
    )
    def test_a_part_of_the_day_is_read_as_the_hour_it_starts(self, text: str, hour: int) -> None:
        """A loss "in the evening" began in the evening.

        Every one of these is the start of its window rather than the middle or
        the end: a file that dates an evening fire at 19:00 is defensible in a way
        that one dating it 23:59 is not.
        """
        reading = read(text)
        assert reading.value == datetime(2026, 5, 2, hour, tzinfo=UTC)
        assert reading.note is not None and "is read as" in reading.note

    def test_a_date_with_no_time_at_all_sits_at_midnight_and_says_so(self) -> None:
        reading = read("2 May 2026")
        assert reading.value == datetime(2026, 5, 2, 0, 0, tzinfo=UTC)
        assert reading.time_stated is False

    def test_a_dotted_date_is_not_read_as_a_time(self) -> None:
        """`13.09.2025` and `02.00` are the same shape, and one of them is a clock."""
        assert read("13.09.2025").value == datetime(2025, 9, 13, tzinfo=UTC)

    def test_a_separate_time_field_is_used_when_the_date_field_has_none(self) -> None:
        reading = temporal.resolve("2 May 2026", time_text="21:04", reference=NOTICE)
        assert reading.value == datetime(2026, 5, 2, 21, 4, tzinfo=UTC)


class TestDiscovery:
    """A loss happens before it is found, and notices state both."""

    def test_the_date_a_loss_was_discovered_is_not_the_date_of_loss(self) -> None:
        """The most damaging mistake available here.

        It is plausible, it is close, and being one or two days out is exactly
        enough to move a loss outside a policy period that in fact covered it.
        """
        reading = read("13 September 2025, overnight, discovered 14 September 06:20", notice=NOTICE)
        assert reading.value == datetime(2025, 9, 13, 22, 0, tzinfo=UTC)
        assert reading.note is not None
        assert "discovered 14 September 06:20" in reading.note

    def test_a_discovery_time_is_not_read_as_the_time_of_loss(self) -> None:
        reading = read("1 February 2026, discovered 05:50 EST", notice=NOTICE)
        assert reading.value == datetime(2026, 2, 1, 0, 0, tzinfo=UTC)
        assert reading.time_stated is False

    def test_a_notice_that_states_only_a_discovery_date_uses_it_and_says_so(self) -> None:
        """Better than nothing, and honest about which of the two it is.

        A claim cannot be created without a date of loss, so refusing the only
        date on the notice would block the file over a distinction the broker did
        not make.
        """
        reading = read("discovered on 2 May 2026")
        assert reading.value == datetime(2026, 5, 2, tzinfo=UTC)
        assert reading.note is not None
        assert "discovered" in reading.note


class TestABareRelativeBeforeADiscovery:
    """ "Overnight, discovered Monday 06:40" — no explicit date anywhere in it.

    Every current fixture states a date before the discovery word, so this shape has
    never fired. On a late-reported unattended loss it misdates silently: "overnight"
    resolves against the notification's arrival, so the loss lands the night before
    the *email* rather than the night before it was found — out by however long the
    broker sat on it, with no conflict and no error to say so.
    """

    #: A Thursday. The Monday before it is three days back, which is the size of the
    #: error this class exists to close.
    LATE = datetime(2026, 2, 26, 9, 15, tzinfo=UTC)

    def test_it_is_read_against_the_discovery_and_not_the_arrival(self) -> None:
        reading = read("Overnight, discovered Monday 06:40", notice=self.LATE)
        # Monday was 23 February; the night before it was Sunday the 22nd.
        assert reading.value == datetime(2026, 2, 22, 22, 0, tzinfo=UTC)
        assert reading.basis is Basis.RELATIVE
        assert reading.note is not None
        assert "read against the discovery" in reading.note
        # And the derivation note must agree with the reading rather than still
        # claiming the notification as the anchor.
        assert "relative to the loss being discovered on 23 February 2026" in reading.note

    def test_a_clause_that_names_its_own_day_is_left_alone(self) -> None:
        """ "Overnight on Friday" already answered the question."""
        reading = read("Overnight on Friday, discovered Monday 06:40", notice=self.LATE)
        assert reading.value == datetime(2026, 2, 20, 22, 0, tzinfo=UTC)
        assert reading.note is not None
        assert "reads as when the loss was found" in reading.note

    def test_a_clause_that_says_more_than_the_word_is_left_alone(self) -> None:
        """A clause with content may well have been dating itself.

        The rule is deliberately narrow: it fires only where the clause before the
        discovery word is nothing but the relative word, because that is the only
        case where the word can have no other referent.
        """
        reading = read(
            "The unit was left secure overnight and the damage was discovered Monday 06:40",
            notice=self.LATE,
        )
        assert reading.value == datetime(2026, 2, 25, 22, 0, tzinfo=UTC)

    def test_a_bare_relative_with_no_discovery_clause_is_unchanged(self) -> None:
        reading = read("Overnight", notice=self.LATE)
        assert reading.value == datetime(2026, 2, 25, 22, 0, tzinfo=UTC)
        assert reading.note is not None
        assert "relative to the notification of" in reading.note

    def test_an_explicit_date_before_the_discovery_still_wins(self) -> None:
        """The existing fixtures' shape, asserted here so the new rule cannot reach it."""
        reading = read("23 February 2026, discovered Monday 06:40", notice=self.LATE)
        assert reading.value == datetime(2026, 2, 23, tzinfo=UTC)
        assert reading.basis is Basis.STATED


class TestTheNoticeDatesItself:
    """Words that mean nothing without the date of the notification."""

    @pytest.mark.parametrize(
        ("text", "expected"),
        [
            ("today", "2026-05-05"),
            ("this morning", "2026-05-05"),
            ("yesterday", "2026-05-04"),
            ("last night", "2026-05-04"),
            ("the day before yesterday", "2026-05-03"),
            ("overnight", "2026-05-04"),
            ("over the weekend", "2026-05-02"),
            ("last week", "2026-04-28"),
            ("three weeks ago", "2026-04-14"),
            ("10 days ago", "2026-04-25"),
            # A Tuesday notice: "Friday" is the Friday just gone, and "last
            # Tuesday" is the week before rather than this morning.
            ("on Friday", "2026-05-01"),
            ("Friday evening", "2026-05-01"),
            ("overnight on Saturday", "2026-05-02"),
            ("last Tuesday", "2026-04-28"),
            ("Tuesday", "2026-05-05"),
        ],
    )
    def test_relative_wording_is_resolved_against_the_notification(
        self, text: str, expected: str
    ) -> None:
        reading = read(text)
        assert reading.value is not None
        assert reading.value.date().isoformat() == expected
        assert reading.basis is Basis.RELATIVE

    def test_a_relative_reading_says_what_it_was_read_against(self) -> None:
        """An officer should never have to guess how a date got onto the file."""
        note = read("overnight on Friday").note
        assert note is not None
        assert "Friday" in note
        assert "1 May 2026" in note
        assert "5 May 2026" in note

    def test_a_day_and_month_with_no_year_take_the_notice_s_year(self) -> None:
        reading = read("2 May")
        assert reading.value == datetime(2026, 5, 2, tzinfo=UTC)
        assert reading.basis is Basis.YEAR_INFERRED

    def test_a_year_is_never_inferred_into_the_future(self) -> None:
        """ "20 December" on a January notice is last December, not next December."""
        reading = read("20 December", notice=datetime(2026, 1, 8, 9, 0, tzinfo=UTC))
        assert reading.value == datetime(2025, 12, 20, tzinfo=UTC)

    def test_a_day_part_beside_a_relative_word_still_sets_the_time(self) -> None:
        assert read("yesterday afternoon").value == datetime(2026, 5, 4, 14, 0, tzinfo=UTC)


class TestSpansAndAmbiguity:
    def test_a_loss_spanning_two_days_is_dated_from_the_day_it_began(self) -> None:
        for text in ("the night of 1/2 May 2026", "between 1 and 2 May 2026", "1-2 May 2026"):
            reading = read(text)
            assert reading.value is not None
            assert reading.value.date() == datetime(2026, 5, 1).date(), text

    def test_a_numeric_date_is_read_day_first_and_says_when_that_was_a_choice(self) -> None:
        """Day-first is the convention this codebase reads and writes.

        The reading is stated in the note rather than assumed silently, because
        `03/04/2026` is a real date under both conventions and the officer is the
        only one who can tell which the writer meant.
        """
        reading = read("03/04/2026")
        assert reading.value == datetime(2026, 4, 3, tzinfo=UTC)
        assert reading.note is not None
        assert "4 March 2026" in reading.note

    def test_a_date_that_is_only_a_date_month_first_is_read_month_first(self) -> None:
        """`03/14/2026` has no day-first reading, so taking one would be a null."""
        assert read("03/14/2026").value == datetime(2026, 3, 14, tzinfo=UTC)

    def test_day_first_loses_to_month_first_when_it_lands_after_the_notice(self) -> None:
        """A notice cannot report a loss that has not happened yet.

        `01/12/2026` on a notice sent in May is 12 January, not 1 December: only
        one of the two readings is a loss that has already happened, so only one
        of them is what the writer meant.
        """
        reading = read("01/12/2026")
        assert reading.value == datetime(2026, 1, 12, tzinfo=UTC)
        assert reading.note is not None
        assert "month-first" in reading.note

    def test_a_reading_the_notice_rules_out_is_not_offered_as_an_alternative(self) -> None:
        """`12/08/2026` on an August notice is a choice between two dates.

        That it *could* have meant next December is not a choice, so saying so
        would be noise on a screen whose whole job is to be read carefully.
        """
        assert read("12/08/2026", notice=datetime(2026, 8, 13, 12, 0, tzinfo=UTC)).note is None


class TestWhatItRefuses:
    """The two readings this module will not vouch for, and the one it cannot make."""

    @pytest.mark.parametrize(
        "text", ["sometime in the spring", "not a date", "2026-13-45", "when the works finish", ""]
    )
    def test_words_that_state_no_date_produce_no_date(self, text: str) -> None:
        reading = read(text)
        assert reading.value is None
        assert reading.basis is Basis.UNREADABLE

    def test_an_unreadable_phrase_says_so_in_a_sentence(self) -> None:
        error = read("sometime in the spring").error
        assert error is not None
        assert "does not state a date" in error

    def test_a_loss_dated_after_the_notice_that_reports_it_is_refused(self) -> None:
        """Refused rather than stored: the exception engine raises the flag from the
        *unparsed* text, so refusing here hides nothing and stores nothing wrong."""
        reading = read("2 May 2027")
        assert reading.value is None
        assert reading.error is not None
        assert "after the notification arrived" in reading.error

    def test_a_loss_a_day_either_side_of_the_notice_is_tolerated(self) -> None:
        """A notice and a loss can sit either side of a timezone boundary."""
        assert read("6 May 2026").value == datetime(2026, 5, 6, tzinfo=UTC)

    def test_a_loss_more_than_ten_years_before_the_notice_is_refused(self) -> None:
        reading = read("2 May 2010")
        assert reading.value is None
        assert reading.error is not None
        assert "ten years" in reading.error

    def test_the_window_is_measured_from_the_notice_and_not_from_today(self) -> None:
        """Which is what lets an archived notice be re-read years later.

        Every date in `case_data` is historic. Measured against the clock on the
        wall, half of them are fine and the rest are a decade stale for no reason
        anybody reading the file would recognise.
        """
        reading = read("13 September 2025", notice=datetime(2025, 9, 15, 16, 14, tzinfo=UTC))
        assert reading.value == datetime(2025, 9, 13, tzinfo=UTC)


class TestTheModelsOwnReading:
    """The `hint`: what the extracting model made of the same words."""

    def test_it_is_used_where_nothing_here_can_read_the_words(self) -> None:
        reading = read("the day the works were handed over", hint="2026-04-28")
        assert reading.value == datetime(2026, 4, 28, tzinfo=UTC)
        assert reading.basis is Basis.HINTED
        assert reading.note is not None

    def test_it_is_checked_against_the_same_window_as_everything_else(self) -> None:
        reading = read("the day the works were handed over", hint="2027-04-28")
        assert reading.value is None

    def test_it_does_not_override_a_reading_this_module_can_justify(self) -> None:
        reading = read("2 May 2026", hint="2026-04-28")
        assert reading.value == datetime(2026, 5, 2, tzinfo=UTC)
        assert reading.basis is Basis.STATED

    def test_a_disagreement_about_the_day_is_flagged_rather_than_silently_resolved(self) -> None:
        """Two defensible readings of one notice is what a human reviewer is for."""
        reading = read("2 May 2026", hint="2026-04-28")
        assert reading.conflict is not None
        assert "28 April 2026" in reading.conflict

    def test_agreement_about_the_day_is_not_worth_anybody_s_attention(self) -> None:
        assert read("2 May 2026, 21:04", hint="2026-05-02").conflict is None


class TestTheNormalisationFacade:
    """`app.domain.normalisation` is what the rest of the codebase calls."""

    def test_parse_datetime_returns_the_reading_s_value(self) -> None:
        assert normalisation.parse_datetime(
            "13 September 2025, overnight, discovered 14 September 06:20",
            reference=datetime(2025, 9, 15, 16, 14, tzinfo=UTC),
        ) == datetime(2025, 9, 13, 22, 0, tzinfo=UTC)

    def test_a_future_loss_date_is_reported_even_though_it_is_not_stored(self) -> None:
        """Both halves matter: the column stays empty and the exception still fires."""
        notice = datetime(2026, 5, 5, 10, 4, tzinfo=UTC)
        assert normalisation.parse_datetime("2 May 2027 21:04", reference=notice) is None
        assert normalisation.is_future_date("2 May 2027 21:04", reference=notice) is True

    def test_a_date_before_the_notice_is_not_a_future_date(self) -> None:
        assert normalisation.is_future_date("2 May 2026", reference=NOTICE) is False


#: Every notice in `case_data`, as `(pack, notice arrival UTC, the extracted value,
#: the expected day)`. Built from the loss-notice PDFs, the `Date:` header of the
#: covering email, and `_ground_truth/MATCHING_GROUND_TRUTH.json`.
#:
#: The extracted value is the two lines a notice prints — "Date of loss" and "Time
#: of loss" — joined the way the dataset asks for them: one `datetime` field,
#: "including the time if one is stated". Discovery clauses and timezone
#: abbreviations included, because that is what arrives.
CASE_DATA_CORPUS = [
    (
        "aurora-jobsite-theft",
        "2025-09-15T16:14",
        "13 September 2025, overnight, discovered 14 September 06:20",
        "2025-09-13",
    ),
    (
        "bayview-terrace-riser",
        "2026-05-05T17:04",
        "2 May 2026, between 02:00 and 05:30, discovered 05:35",
        "2026-05-02",
    ),
    (
        "cherry-creek-water-damage",
        "2026-02-09T17:22",
        "7 February 2026, from about 19:30, discovered 07:10 on 8 February",
        "2026-02-07",
    ),
    (
        "cobalt-ridge-trench-collapse",
        "2026-05-21T21:11",
        "19 May 2026, 09:34 CDT",
        "2026-05-19",
    ),
    (
        "cypress-landing-copper-theft",
        "2026-04-13T16:04",
        "11 April 2026, between 01:20 and 03:05, discovered 06:15",
        "2026-04-11",
    ),
    (
        "cypress-landing-haboob",
        "2026-07-10T21:22",
        "7 July 2026, 18:34 MST",
        "2026-07-07",
    ),
    (
        "driver-middle-school-open-roof",
        "2025-06-19T13:05",
        "18 June 2025, 16:40 EDT",
        "2025-06-18",
    ),
    (
        "fairmount-dye-house-fire",
        "2025-08-15T12:40",
        "13 August 2025, 21:04 EDT",
        "2025-08-13",
    ),
    (
        "fairmount-mezzanine-collapse",
        "2025-05-15T15:26",
        "14 May 2025, 15:40 EDT",
        "2025-05-14",
    ),
    (
        "harborline-ammonia-release",
        "2026-01-12T12:41",
        "10 January 2026, 04:20 EST",
        "2026-01-10",
    ),
    (
        "harborline-delaware-freeze",
        "2026-02-09T13:55",
        "1 February 2026, discovered 05:50 EST",
        "2026-02-01",
    ),
    (
        "ironbark-excavator-fire",
        "2025-11-18T18:26",
        "14 November 2025, 13:05 MST",
        "2025-11-14",
    ),
    (
        "kestrel-ridge-copper-theft",
        "2026-03-03T16:12",
        "2 March 2026, between 23:00 and 03:00, discovered 06:35",
        "2026-03-02",
    ),
    (
        "kestrel-ridge-vehicle-impact",
        "2025-11-07T22:47",
        "6 November 2025, 07:52 MST",
        "2025-11-06",
    ),
    (
        "larkspur-fleet-collision",
        "2026-03-16T17:22",
        "14 March 2026, 07:48 EDT",
        "2026-03-14",
    ),
    (
        "meadowcrest-frozen-sprinkler",
        "2026-01-29T14:36",
        "26 January 2026, 03:50 EST, discovered 04:15",
        "2026-01-26",
    ),
    (
        "northfield-site-vandalism",
        "2026-01-13T16:08",
        "3 January 2026, between 22:00 and 05:30",
        "2026-01-03",
    ),
    (
        "northfield-water-intrusion",
        "2025-10-22T21:38",
        "18 October 2025, overnight, discovered 20 October 06:40",
        "2025-10-18",
    ),
    (
        "rivergate-copper-theft",
        "2026-01-19T14:31",
        "17 January 2026, overnight, discovered 18 January 07:40",
        "2026-01-17",
    ),
    (
        "rivergate-dropped-load",
        "2026-02-26T22:40",
        "26 February 2026, 14:52 EST",
        "2026-02-26",
    ),
    (
        "sable-creek-falling-brick",
        "2026-03-18T15:02",
        "17 March 2026, 10:20 EDT",
        "2026-03-17",
    ),
    (
        "tidewater-rooftop-equipment",
        "2025-08-27T12:40",
        "26 August 2025, 11:15 EDT",
        "2025-08-26",
    ),
    (
        "windrow-grove-building-k-fire",
        "2025-12-05T17:41",
        "21 November 2025, 02:14 CST",
        "2025-11-21",
    ),
    (
        "windrow-grove-hail",
        "2026-04-22T14:12",
        "19 April 2026, 19:45 CDT",
        "2026-04-19",
    ),
]


class TestTheCaseDataCorpus:
    """Every notice in `case_data`, read the way the extraction returns it.

    The date and time of loss are printed as separate lines on these notices, and
    the dataset asks for one `datetime` field "including the time if one is
    stated" — so what arrives here is the two lines joined, discovery clause and
    timezone abbreviation and all. Each row is checked against the notice's own
    arrival date and the pack's ground truth.

    This table is the regression test for the reported bug: before the resolver,
    exactly one of these twenty-four read as a date at all.
    """

    @pytest.mark.parametrize(("pack", "received", "extracted", "expected"), CASE_DATA_CORPUS)
    def test_every_notice_in_the_corpus_yields_the_day_the_loss_happened(
        self, pack: str, received: str, extracted: str, expected: str
    ) -> None:
        reading = temporal.resolve(
            extracted, reference=datetime.fromisoformat(received).replace(tzinfo=UTC)
        )
        assert reading.value is not None, f"{pack}: {extracted!r} read as no date"
        assert reading.value.date().isoformat() == expected, pack

    def test_no_notice_in_the_corpus_is_dated_after_it_arrived(self) -> None:
        """Which is what the future-loss-date exception is raised on.

        A resolver that guessed a year or a convention wrongly would show up here
        as a critical exception on a clean notice, which is the failure mode that
        costs a desk its trust in the flag.
        """
        for pack, received, extracted, _ in CASE_DATA_CORPUS:
            notice = datetime.fromisoformat(received).replace(tzinfo=UTC)
            assert normalisation.is_future_date(extracted, reference=notice) is False, pack
