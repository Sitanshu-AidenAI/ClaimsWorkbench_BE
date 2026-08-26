"""Reading the date of loss out of the words a notice actually uses.

A notice states when the loss happened however the person writing it thought
clearest: `13 September 2025`, `2 May 2026, between 02:00 and 05:30, discovered
05:35`, `overnight on Friday`, `26 February 2026 14:52 EST`. The claim record has
one `date_of_loss` timestamp, and a claim cannot be created without it, so
somewhere between the prose and the column somebody has to turn one into the
other.

That step used to be `strptime` against nine formats over the *whole* string,
which meant every example above except the first read as no date at all: the
column stayed null, the completeness engine reported the date as missing, and the
notice could not become a claim even though it plainly said when the loss
happened. This module is that step done properly, and three ideas hold it
together.

**The notice dates itself.** Everything relative — `yesterday`, `overnight on
Friday`, a day and month with no year — is resolved against a *reference*
instant, which is when the notification arrived. The case already carries it in
`received_at`, and it is exactly what the person writing "Friday" meant by it.

**A loss happens before it is discovered.** Notices state both in one breath:
`overnight, discovered 14 September 06:20`. The clause after a discovery word is
never the date of loss, so it is set aside before anything is read, and consulted
only when there is nothing else to read.

**Nothing is invented silently.** Every inference comes back as a sentence for
the officer reviewing the notice, and a reading that cannot be justified comes
back as `None` with the reason. A wrong date that looks certain is worse than a
missing one: the policy-period check, the catastrophe window, the fraud signals
and the claim itself are all decided against this one value.

One deliberate imprecision. The wall clock a document states is kept as stated —
`21:04 EDT` is stored as 21:04, not shifted to 01:04 the following day. The
calendar day is what every downstream reader uses, and a true-UTC conversion
moves roughly a third of evening losses onto the wrong day. An offset nobody
reads is a smaller lie than a date that disagrees with the document in front of
the officer.
"""

from __future__ import annotations

import re
from dataclasses import dataclass, field
from datetime import UTC, date, datetime, time, timedelta
from enum import StrEnum

#: Nobody notifies a loss more than a working day after the notice's own date; a
#: later one is a typo or a misread year, and either way it is not a date of loss.
#: A day rather than nothing because a notice and a loss can sit either side of a
#: timezone boundary.
FUTURE_TOLERANCE = timedelta(days=1)

#: A claim older than this is not being notified for the first time. Measured from
#: the notice rather than from today, so a notice loaded from an archive is read
#: against its own arrival and not against the clock on the wall now.
MAX_BACKDATE = timedelta(days=365 * 10)


class Basis(StrEnum):
    """How a reading was arrived at. Carried so the note can say so, and so a
    caller can tell a date the document printed from one this module worked out."""

    #: A complete date, printed in the source. Nothing was inferred.
    STATED = "stated"
    #: A day and month were printed; the year came from the notice's own date.
    YEAR_INFERRED = "year_inferred"
    #: Words rather than a date — "yesterday", "overnight on Friday".
    RELATIVE = "relative"
    #: The extracting model's own normalisation, used because nothing here could
    #: read the phrase. Last resort, and always noted as one.
    HINTED = "hinted"
    #: No date could be justified from the text.
    UNREADABLE = "unreadable"


@dataclass(frozen=True, slots=True)
class Reading:
    """What a date phrase was read as, and everything a reviewer should know."""

    value: datetime | None = None
    basis: Basis = Basis.UNREADABLE
    #: Sentences describing each inference made, in the order they were made.
    #: Empty for a date printed in full, which needs no explanation.
    notes: tuple[str, ...] = field(default_factory=tuple)
    #: Why nothing could be read, when nothing could be. Never set alongside a
    #: value: this module either vouches for a reading or explains itself.
    error: str | None = None
    #: Whether a time of day was stated rather than assumed. A date with no time
    #: is stored at midnight, which is a convention and not a fact about the loss.
    time_stated: bool = False
    #: Set when the extracting model normalised the same phrase to a different
    #: *day*. Not an error — a notice ambiguous enough to split the two readings
    #: is one a human should look at, which is what the caller does with this.
    conflict: str | None = None

    @property
    def note(self) -> str | None:
        """Every inference as one paragraph, for the review screen."""
        joined = " ".join(part for part in (*self.notes, self.conflict) if part)
        return joined or None


# ---------------------------------------------------------------------------
# Vocabulary
# ---------------------------------------------------------------------------

_MONTHS: dict[str, int] = {
    name: number
    for number, names in enumerate(
        (
            ("jan", "january"),
            ("feb", "february"),
            ("mar", "march"),
            ("apr", "april"),
            ("may",),
            ("jun", "june"),
            ("jul", "july"),
            ("aug", "august"),
            ("sep", "sept", "september"),
            ("oct", "october"),
            ("nov", "november"),
            ("dec", "december"),
        ),
        start=1,
    )
    for name in names
}

#: Longest name first within each day, so "tuesday" is matched before "tue" and a
#: note quotes the word the notice actually used.
_WEEKDAYS: dict[str, int] = {
    name: number
    for number, names in enumerate(
        (
            ("monday", "mon"),
            ("tuesday", "tues", "tue"),
            ("wednesday", "wed"),
            ("thursday", "thurs", "thur", "thu"),
            ("friday", "fri"),
            ("saturday", "sat"),
            ("sunday", "sun"),
        )
    )
    for name in names
}

#: The hour a part of the day is read as. Deliberately the *start* of the window
#: in every case: a loss "in the evening" began in the evening, and a claims file
#: that says 19:00 for a fire reported as an evening fire is defensible in a way
#: that 23:59 is not. Longest key wins, so "early hours" beats "hours" and
#: "overnight" is not read as "night".
_DAY_PARTS: dict[str, time] = {
    "early hours": time(3, 0),
    "small hours": time(3, 0),
    "first thing": time(8, 0),
    "mid-morning": time(10, 0),
    "morning": time(9, 0),
    "lunchtime": time(12, 30),
    "midday": time(12, 0),
    "noon": time(12, 0),
    "afternoon": time(14, 0),
    "teatime": time(17, 0),
    "evening": time(19, 0),
    "overnight": time(22, 0),
    "tonight": time(22, 0),
    "night": time(22, 0),
    "midnight": time(0, 0),
}

#: Words after which a date belongs to the *finding* of the loss, not to the loss.
#: "overnight, discovered 14 September 06:20" is one loss with two dates in it,
#: and reading the second one is the single most damaging mistake available here:
#: it is plausible, it is close, and it silently moves the loss outside the policy
#: period often enough to matter.
_DISCOVERY_RE = re.compile(
    r"\b(?:discovered|discovery|detected|found|noticed|identified|reported|notified|"
    r"advised|attended|raised|became aware|first became aware|came to light)\b",
    re.IGNORECASE,
)

_ISO_RE = re.compile(r"\b(?P<y>\d{4})-(?P<m>\d{1,2})-(?P<d>\d{1,2})(?!\d)")

#: "the night of 3/4 March", "12-14 March 2026". A loss spanning days is dated
#: from the day it began, which is the first number in every one of these forms.
_SPAN_RE = re.compile(
    r"\b(?P<d>\d{1,2})\s?(?:/|-|to|and)\s?\d{1,2}\s+(?P<mon>[a-z]{3,9})\.?"
    r"(?:\s+(?P<y>\d{4}|\d{2}))?(?!\d)",
    re.IGNORECASE,
)

_DMY_RE = re.compile(
    r"\b(?P<d>\d{1,2})(?!\d)\s+(?:of\s+)?(?P<mon>[a-z]{3,9})\.?(?:\s+(?P<y>\d{4}|\d{2}))?(?!\d)",
    re.IGNORECASE,
)
_MDY_RE = re.compile(
    r"\b(?P<mon>[a-z]{3,9})\.?\s+(?P<d>\d{1,2})(?!\d)(?:\s+(?P<y>\d{4}|\d{2}))?", re.IGNORECASE
)
_NUMERIC_RE = re.compile(
    r"\b(?P<a>\d{1,2})\s?[/.-]\s?(?P<b>\d{1,2})(?:\s?[/.-]\s?(?P<y>\d{4}|\d{2}))?(?!\d)"
)

#: `21:04`, `3.30pm`, `12:15am`. The trailing guard is a lookahead rather than a
#: word boundary, because there is no `\b` between "30" and "pm" — and "3.30pm" is
#: one of the commonest ways a loss time is written.
_HHMM_RE = re.compile(
    r"\b(?P<h>[01]?\d|2[0-3])[:.](?P<m>[0-5]\d)(?!\d)\s?(?P<ap>am|pm)?", re.IGNORECASE
)
_CLOCK_RE = re.compile(r"\b(?P<h>1[0-2]|\d)\s?(?P<ap>am|pm)\b", re.IGNORECASE)

_DASHES = str.maketrans({"–": "-", "—": "-", "‑": "-", "−": "-"})

_WORD_COUNTS = {
    "a": 1,
    "one": 1,
    "two": 2,
    "a couple of": 2,
    "couple of": 2,
    "three": 3,
    "four": 4,
    "five": 5,
    "six": 6,
    "seven": 7,
    "eight": 8,
    "nine": 9,
    "ten": 10,
}
_AGO_RE = re.compile(
    r"\b(?P<count>\d{1,3}|"
    + "|".join(_WORD_COUNTS)
    + r")\s+(?P<unit>day|night|week|month)s?\s+ago",
    re.IGNORECASE,
)


# ---------------------------------------------------------------------------
# The public surface
# ---------------------------------------------------------------------------


def resolve(
    text: str | None,
    *,
    reference: datetime | None = None,
    time_text: str | None = None,
    hint: str | None = None,
) -> Reading:
    """Read `text` as the instant a loss happened.

    `reference` is when the notification arrived, and everything relative is
    resolved against it; it defaults to now, which is the right answer for a
    notice arriving live and a harmless one for a date stated in full.

    `time_text` is a separate time-of-loss field, for the callers that have one.
    `hint` is the extracting model's own ISO normalisation of the same phrase,
    used only when nothing here can read the words at all.
    """
    anchor = _anchor(reference)
    raw = (text or "").strip()
    if not raw:
        return _from_hint(hint, anchor, notes=()) if hint else Reading()

    before, after = _split_discovery(_clean(raw))
    notes: list[str] = []

    hit = _find_date(before, anchor)
    searched = before
    found_when = _find_date(after, anchor) if after else None

    #: What a relative phrase was read against, and how a note should name it.
    relative_anchor, relative_source = anchor, "the notification of"

    if hit is not None and found_when is not None:
        rebased = _rebase_on_discovery(before, hit, found_when, anchor)
        if rebased is not None:
            notes.append(
                f"“{hit.phrase.strip()}” states no day of its own, so it is read against "
                "the discovery rather than against the notification's arrival — the notice "
                "says the loss happened the night before it was found, not the night before "
                "it was reported."
            )
            hit = rebased
            relative_anchor = datetime.combine(found_when.day, anchor.timetz())
            relative_source = "the loss being discovered on"
        else:
            notes.append(f"“{after}” reads as when the loss was found, so it was not used.")
    elif hit is None and found_when is not None:
        # Nothing before the discovery word carried a date, so the notice states
        # only when the loss was found. That is the best answer available, and it
        # is said to be the one that was taken.
        hit, searched = found_when, after
        notes.append(
            "The only date stated is when the loss was discovered, so it is read as "
            "the date of loss."
        )

    if hit is None:
        if hint:
            return _from_hint(hint, anchor, notes=tuple(notes))
        return Reading(
            error=f"{raw!r} does not state a date this reads as the date of loss.",
            notes=tuple(notes),
        )

    notes.extend(hit.notes)
    clock = _find_time(_without_date(searched, hit)) or (
        _find_time(_clean(time_text)) if time_text else None
    )

    stamp = datetime.combine(hit.day, clock.at if clock else time(0, 0), tzinfo=UTC)
    refused = _refuse(stamp, anchor, raw)
    if refused is not None:
        return refused

    if hit.basis is not Basis.STATED:
        notes.append(
            _derivation_note(
                hit, stamp, relative_anchor, clock is not None, against=relative_source
            )
        )
    if clock is not None:
        # "tonight" carries its own hour, and the derivation note has already
        # quoted it. Saying it twice reads as two separate inferences.
        notes.extend(note for note in clock.notes if hit.phrase.lower() not in note.lower())

    return Reading(
        value=stamp,
        basis=hit.basis,
        notes=tuple(notes),
        time_stated=clock is not None,
        conflict=_conflict(hint, stamp, anchor),
    )


def stated_date(text: str | None, *, reference: datetime | None = None) -> date | None:
    """The calendar date a phrase states, plausible or not.

    The plausibility window is deliberately *not* applied: this is what the
    exception engine asks when it wants to know whether a notice reports a loss in
    the future, and refusing to read the date would hide the very thing it is
    looking for.
    """
    if not text:
        return None
    cleaned = _clean(text)
    before, after = _split_discovery(cleaned)
    anchor = _anchor(reference)
    hit = _find_date(before, anchor) or (_find_date(after, anchor) if after else None)
    return hit.day if hit is not None else None


# ---------------------------------------------------------------------------
# Finding a date
# ---------------------------------------------------------------------------


@dataclass(frozen=True, slots=True)
class _DateHit:
    day: date
    basis: Basis
    span: tuple[int, int]
    phrase: str
    notes: tuple[str, ...] = field(default_factory=tuple)


@dataclass(frozen=True, slots=True)
class _TimeHit:
    at: time
    notes: tuple[str, ...] = field(default_factory=tuple)


def _find_date(text: str, anchor: datetime) -> _DateHit | None:
    """The first reading any of the forms below can justify, in order of certainty."""
    for finder in (_iso, _span, _dmy, _mdy, _numeric, _relative):
        hit = finder(text, anchor)
        if hit is not None:
            return hit
    return None


def _iso(text: str, anchor: datetime) -> _DateHit | None:
    del anchor
    for match in _ISO_RE.finditer(text):
        day = _safe(int(match["y"]), int(match["m"]), int(match["d"]))
        if day is not None:
            return _DateHit(day, Basis.STATED, match.span(), match.group())
    return None


def _span(text: str, anchor: datetime) -> _DateHit | None:
    for match in _SPAN_RE.finditer(text):
        month = _MONTHS.get(match["mon"].lower())
        if month is None:
            continue
        year, inferred = _year(match["y"], month, int(match["d"]), anchor)
        day = _safe(year, month, int(match["d"]))
        if day is None:
            continue
        return _DateHit(
            day,
            Basis.YEAR_INFERRED if inferred else Basis.STATED,
            match.span(),
            match.group(),
            notes=(
                f"“{match.group().strip()}” spans more than one day, so the loss is dated "
                "from the day it began.",
            ),
        )
    return None


def _dmy(text: str, anchor: datetime) -> _DateHit | None:
    return _textual(_DMY_RE, text, anchor)


def _mdy(text: str, anchor: datetime) -> _DateHit | None:
    return _textual(_MDY_RE, text, anchor)


def _textual(pattern: re.Pattern[str], text: str, anchor: datetime) -> _DateHit | None:
    for match in pattern.finditer(text):
        month = _MONTHS.get(match["mon"].lower())
        if month is None:
            continue
        year, inferred = _year(match["y"], month, int(match["d"]), anchor)
        day = _safe(year, month, int(match["d"]))
        if day is None:
            continue
        basis = Basis.YEAR_INFERRED if inferred else Basis.STATED
        return _DateHit(day, basis, match.span(), match.group())
    return None


def _numeric(text: str, anchor: datetime) -> _DateHit | None:
    """`13/09/2025`, `2.5.26`, `13/09`.

    Day-first, because that is the convention the rest of this codebase reads and
    writes. Two exceptions, both of which make a wrong answer into a right one
    rather than a matter of taste: a day-first reading that is not a real date
    falls back to month-first, and a day-first reading that lands *after* the
    notice arrived loses to a month-first one that does not — a notice cannot
    report a loss that has not happened, so the other reading is the intended one.
    A pair that reads validly both ways and plausibly both ways stays day-first
    and says so.
    """
    for match in _NUMERIC_RE.finditer(text):
        first, second = int(match["a"]), int(match["b"])
        printed = match.group().strip()

        for year, inferred in _numeric_years(match["y"], anchor):
            day_first = _safe(year, second, first)
            month_first = _safe(year, first, second)
            basis = Basis.YEAR_INFERRED if inferred else Basis.STATED

            if day_first is None:
                if month_first is None:
                    continue  # Neither reading is a real date — a time, or a fraction.
                return _DateHit(
                    month_first,
                    basis,
                    match.span(),
                    printed,
                    notes=(f"“{printed}” only reads as a date month-first.",),
                )

            if (
                month_first is not None
                and month_first != day_first
                and _beyond(day_first, anchor)
                and not _beyond(month_first, anchor)
            ):
                return _DateHit(
                    month_first,
                    basis,
                    match.span(),
                    printed,
                    notes=(
                        f"“{printed}” is read month-first as {_pretty(month_first)}; "
                        "day-first it would fall after the notification arrived.",
                    ),
                )

            # The ambiguity is only worth raising when the other reading is a date
            # this notice could actually be reporting. `12/08/2026` on an August
            # notice is a choice; that it *could* have meant next December is not.
            notes: tuple[str, ...] = ()
            if (
                month_first is not None
                and month_first != day_first
                and _plausible(month_first, anchor)
            ):
                notes = (
                    f"“{printed}” is read day-first as {_pretty(day_first)}; "
                    f"month-first it would be {_pretty(month_first)}.",
                )
            return _DateHit(day_first, basis, match.span(), printed, notes=notes)
    return None


def _relative(text: str, anchor: datetime) -> _DateHit | None:
    """Words that only mean something next to the notice's own date."""
    today = anchor.date()
    lowered = text.lower()

    for phrase, offset in (
        ("day before yesterday", 2),
        ("yesterday", 1),
        ("last night", 1),
        ("the week before last", 14),
        ("last week", 7),
        ("earlier this week", (today.weekday() or 0)),
        ("this week", (today.weekday() or 0)),
    ):
        if phrase in lowered:
            return _relative_hit(text, phrase, today - timedelta(days=offset))

    ago = _AGO_RE.search(text)
    if ago is not None:
        spelled = ago["count"]
        count = int(spelled) if spelled.isdigit() else _WORD_COUNTS.get(spelled, 1)
        days = count * {"day": 1, "night": 1, "week": 7, "month": 30}[ago["unit"]]
        return _DateHit(today - timedelta(days=days), Basis.RELATIVE, ago.span(), ago.group())

    if "weekend" in lowered:
        # The Saturday just gone: two thirds of a weekend, and the day a Monday
        # notice means when it says "over the weekend".
        return _relative_hit(text, "weekend", _last_weekday(today, 5, inclusive=True))

    for name, weekday in _WEEKDAYS.items():
        match = re.search(rf"\b(?P<last>last\s+)?{name}\b", text, re.IGNORECASE)
        if match is None:
            continue
        day = _last_weekday(today, weekday, inclusive=match["last"] is None)
        return _DateHit(day, Basis.RELATIVE, match.span(), match.group().strip())

    for phrase in ("today", "this morning", "this afternoon", "this evening", "tonight"):
        if phrase in lowered:
            return _relative_hit(text, phrase, today)

    if "overnight" in lowered:
        # No date and no weekday beside it: the night that ended on the morning
        # the notice was written, which is what a notice sent at 07:41 means.
        return _relative_hit(text, "overnight", today - timedelta(days=1))

    return None


# ---------------------------------------------------------------------------
# Finding a time
# ---------------------------------------------------------------------------


def _find_time(text: str | None) -> _TimeHit | None:
    """A stated clock time, or the hour a part of the day is read as.

    The *first* time in the text, which is the one that matters: "between 02:00
    and 05:30" is a loss that began at 02:00, and a range's second half is when it
    was over.
    """
    if not text:
        return None

    hhmm = _HHMM_RE.search(text)
    clock = _CLOCK_RE.search(text)
    if hhmm is not None and (clock is None or hhmm.start() <= clock.start()):
        hour = _hour(int(hhmm["h"]), hhmm["ap"])
        return _TimeHit(time(hour, int(hhmm["m"])))
    if clock is not None:
        return _TimeHit(time(_hour(int(clock["h"]), clock["ap"]), 0))

    lowered = text.lower()
    for phrase, at in sorted(_DAY_PARTS.items(), key=lambda item: -len(item[0])):
        start = lowered.find(phrase)
        if start >= 0:
            written = text[start : start + len(phrase)]
            return _TimeHit(at, notes=(f"“{written}” is read as {at:%H:%M}.",))
    return None


def _hour(hour: int, meridiem: str | None) -> int:
    meridiem = meridiem.lower() if meridiem else None
    if meridiem == "pm" and hour < 12:
        return hour + 12
    if meridiem == "am" and hour == 12:
        return 0
    return hour % 24


# ---------------------------------------------------------------------------
# Plumbing
# ---------------------------------------------------------------------------


def _anchor(reference: datetime | None) -> datetime:
    if reference is None:
        return datetime.now(UTC)
    return reference if reference.tzinfo is not None else reference.replace(tzinfo=UTC)


def _clean(text: str | None) -> str:
    """De-ordinalled, de-comma'd, one-spaced, with an ISO `T` opened out.

    Case is left alone. Every pattern here is case-insensitive, so nothing is
    gained by lowering it and something is lost: the notes quote this text, and an
    officer reading “Friday” back as “friday” is being shown a paraphrase of their
    own notice.
    """
    if not text:
        return ""
    cleaned = text.translate(_DASHES)
    cleaned = re.sub(r"(\d)(st|nd|rd|th)\b", r"\1", cleaned, flags=re.IGNORECASE)
    cleaned = re.sub(r"(\d)[Tt](\d)", r"\1 \2", cleaned)
    cleaned = cleaned.replace(",", " ")
    return re.sub(r"\s+", " ", cleaned).strip()


def _split_discovery(text: str) -> tuple[str, str]:
    match = _DISCOVERY_RE.search(text)
    if match is None:
        return text, ""
    return text[: match.start()].strip(" -–—;:"), text[match.start() :].strip()


def _is_bare_relative(before: str, hit: _DateHit) -> bool:
    """Whether the clause before "discovered" is nothing but a relative word.

    "Overnight," is bare. "Overnight on Friday" is not — it names a day, and that day
    is the answer. "The unit was left secure overnight" is not either: it says
    something, and a clause that says something may well be dating itself.

    Tested by removing the matched phrase and looking for anything alphanumeric left,
    which is exactly the question and needs no vocabulary of its own.
    """
    if hit.basis is not Basis.RELATIVE:
        return False
    start, end = hit.span
    if end <= start:
        return False
    remainder = before[:start] + before[end:]
    return not any(char.isalnum() for char in remainder)


def _rebase_on_discovery(
    before: str, hit: _DateHit, found_when: _DateHit, anchor: datetime
) -> _DateHit | None:
    """A bare relative word re-read against the discovery date, or `None`.

    The case: "Overnight, discovered Monday 06:40", on a notice that arrived several
    days late. "Overnight" resolves against the notification's arrival, so the loss
    was dated the night before the *email* rather than the night before the discovery
    — silently, and by however long the broker sat on it. No conflict, no error, and
    on a late-reported unattended loss that is the difference between a date inside
    the policy period and one outside it.

    What the notice actually asserts is that the loss happened overnight *before it
    was found*, so the discovery date is the anchor that sentence was written
    against. Only for a bare relative clause: anything that names a day of its own
    already answered the question, and anything that says more than the word might
    not have been dating itself at all.

    Returns `None` when the rule does not apply or changes nothing, so the caller's
    existing note stands.
    """
    if not _is_bare_relative(before, hit):
        return None
    rebased = _find_date(before, datetime.combine(found_when.day, anchor.timetz()))
    if rebased is None or rebased.day == hit.day:
        return None
    return rebased


def _without_date(text: str, hit: _DateHit) -> str:
    """The text a time is looked for in.

    A printed date is cut out first, because `13.09.2025` and `02.00` are the same
    shape and one of them is a time. A relative phrase is left in: no arrangement
    of the word "yesterday" reads as a clock, and cutting it would take the
    "afternoon" standing next to it with it.
    """
    if hit.basis is Basis.RELATIVE:
        return text
    start, end = hit.span
    return f"{text[:start]} {text[end:]}"


def _relative_hit(text: str, phrase: str, day: date) -> _DateHit:
    """A hit for a phrase found by `in`, quoted as the notice wrote it."""
    start = text.lower().find(phrase)
    span = (start, start + len(phrase)) if start >= 0 else (0, 0)
    return _DateHit(day, Basis.RELATIVE, span, text[span[0] : span[1]] or phrase)


def _safe(year: int, month: int, day: int) -> date | None:
    try:
        return date(year, month, day)
    except ValueError:
        return None


def _year(printed: str | None, month: int, day: int, anchor: datetime) -> tuple[int, bool]:
    """`(year, inferred)` — the printed year, or the one the notice implies."""
    if printed:
        value = int(printed)
        return (value if value > 99 else 2000 + value), False
    return _infer_year(month, day, anchor), True


def _infer_year(month: int, day: int, anchor: datetime) -> int:
    """The latest year that puts this day and month at or before the notice.

    A broker writing "14 September" in a notice sent on 15 September 2025 means
    2025; one writing "20 December" in a notice sent that January means the year
    before. Taking the most recent occurrence is both readings at once.
    """
    limit = anchor.date() + FUTURE_TOLERANCE
    for year in (anchor.year, anchor.year - 1, anchor.year - 2, anchor.year - 3):
        candidate = _safe(year, month, day)
        if candidate is not None and candidate <= limit:
            return year
    return anchor.year


def _numeric_years(printed: str | None, anchor: datetime) -> list[tuple[int, bool]]:
    """The years a numeric date could carry, most likely first.

    A printed year is the only candidate. Without one, both readings of the pair
    need a year and neither has told us the month yet, so the notice's year and
    the one before it are tried in turn — a January notice about a December loss
    is the common case this covers.
    """
    if printed:
        value = int(printed)
        return [(value if value > 99 else 2000 + value, False)]
    return [(anchor.year, True), (anchor.year - 1, True)]


def _last_weekday(today: date, weekday: int, *, inclusive: bool) -> date:
    """The most recent named weekday. `inclusive` allows today itself.

    "on Friday" in a notice sent on a Friday means that morning; "last Friday" in
    the same notice means the week before, which is the only thing the word "last"
    can be adding.
    """
    delta = (today.weekday() - weekday) % 7
    if delta == 0 and not inclusive:
        delta = 7
    return today - timedelta(days=delta)


def _beyond(day: date | None, anchor: datetime) -> bool:
    return day is not None and day > anchor.date() + FUTURE_TOLERANCE


def _plausible(day: date, anchor: datetime) -> bool:
    """Whether a day is one this notice could be reporting at all."""
    return not _beyond(day, anchor) and day >= (anchor - MAX_BACKDATE).date()


def _refuse(stamp: datetime, anchor: datetime, raw: str) -> Reading | None:
    """The two readings this module will not vouch for, each with its reason."""
    if stamp.date() > anchor.date() + FUTURE_TOLERANCE:
        return Reading(
            basis=Basis.UNREADABLE,
            error=(
                f"{raw!r} reads as {_pretty(stamp.date())}, which is after the notification "
                f"arrived on {_pretty(anchor.date())}."
            ),
        )
    if stamp < anchor - MAX_BACKDATE:
        return Reading(
            basis=Basis.UNREADABLE,
            error=(
                f"{raw!r} reads as {_pretty(stamp.date())}, more than ten years before the "
                "notification arrived."
            ),
        )
    return None


def _derivation_note(
    hit: _DateHit,
    stamp: datetime,
    anchor: datetime,
    timed: bool,
    *,
    against: str = "the notification of",
) -> str:
    """One sentence saying what was inferred and what it was inferred from.

    `against` names the thing the phrase was read relative to. It is a parameter
    because that is not always the notification: a bare relative word before a
    discovery clause is read against the discovery, and a note that said
    "notification" there would contradict the reading it is explaining.
    """
    shown = f"{_pretty(stamp.date())} {stamp:%H:%M}" if timed else _pretty(stamp.date())
    return (
        f"“{hit.phrase.strip()}” is read as {shown}, relative to {against} "
        f"{_pretty(anchor.date())}."
    )


def _from_hint(hint: str, anchor: datetime, *, notes: tuple[str, ...]) -> Reading:
    """Fall back to the extracting model's own normalisation of the phrase.

    Reached only when none of the forms above could read the words — "the Tuesday
    after the storm", a date written in another language. The model read the same
    passage, so its answer is worth more than nothing, and it is checked against
    the same window as everything else before being believed.
    """
    parsed = _parse_iso(hint)
    if parsed is None:
        return Reading(error=f"{hint!r} is not an ISO date.", notes=notes)
    refused = _refuse(parsed, anchor, hint)
    if refused is not None:
        return refused
    return Reading(
        value=parsed,
        basis=Basis.HINTED,
        notes=(
            *notes,
            f"The wording could not be read here, so the date is the extracting model's "
            f"own reading of it: {_pretty(parsed.date())}.",
        ),
        time_stated=parsed.time() != time(0, 0),
    )


def _conflict(hint: str | None, stamp: datetime, anchor: datetime) -> str | None:
    """Whether the model read the same phrase as a different day."""
    if not hint:
        return None
    parsed = _parse_iso(hint)
    if parsed is None or parsed.date() == stamp.date():
        return None
    if _beyond(parsed.date(), anchor):
        return None
    return (
        f"The extracting model read the same wording as {_pretty(parsed.date())}. "
        "Worth confirming which day is meant."
    )


def _parse_iso(value: str | None) -> datetime | None:
    if not value:
        return None
    text = value.strip().replace("z", "+00:00").replace("Z", "+00:00")
    try:
        parsed = datetime.fromisoformat(text)
    except ValueError:
        try:
            parsed = datetime.combine(date.fromisoformat(text[:10]), time(0, 0))
        except ValueError:
            return None
    return parsed if parsed.tzinfo is not None else parsed.replace(tzinfo=UTC)


def _pretty(day: date) -> str:
    return f"{day:%d %B %Y}".lstrip("0")


__all__ = [
    "FUTURE_TOLERANCE",
    "MAX_BACKDATE",
    "Basis",
    "Reading",
    "resolve",
    "stated_date",
]
