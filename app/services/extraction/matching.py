"""Finding a value in a document, the way that document happens to write it.

Two problems, and they are the reason this is a module rather than a `str.find`
at the call site.

**Whitespace.** A reader emits the line breaks the page had; a model returns the
words. `"2870 Patapsco\nIndustrial Park"` and `"2870 Patapsco Industrial Park"`
are the same phrase and neither `find` nor `==` says so. The fix is to search a
*normalised* copy of the text — whitespace collapsed, case folded — and map the
hit back to an offset in the original. `NormalisedText` is that copy plus the map,
built once per document and searched many times, which is the whole point: a
regex rebuilt per value per document is the same work done thirty-eight times.

**Wording.** The loss notice writes `10 January 2026`, the engineer's report
writes `2026-01-10` and the schedule writes `10/01/2026`. They state the same
date, so a citation that says "the report does not mention this" is wrong.
`variants` turns one extracted value into the handful of forms a document
plausibly writes it in, most distinctive first, using the *coerced* value as the
source of truth — the coercion has already done the hard reading.

`searchable` is the guard on both. Corroborating a value by substring search is
only honest when the value is distinctive: `0` injuries appears in every document
that prints a date, and a citation saying so is noise dressed as evidence.
"""

from __future__ import annotations

from dataclasses import dataclass
from datetime import date, datetime
from typing import Any

#: Shortest value worth searching another document for. Below this a match is a
#: coincidence: `USD`, `0`, `No` and `2` all appear on pages that state nothing
#: about the field they were read for.
MIN_SEARCHABLE_CHARACTERS = 4

#: Longest value worth searching for. A paragraph of narrative is never restated
#: verbatim in a second document — the second document paraphrases it — so the
#: search costs a scan of every file and returns nothing.
MAX_SEARCHABLE_CHARACTERS = 240

#: Significant digits a purely numeric value needs before it is distinctive.
#: `3,700,000` has seven and means something; `12` has two and matches a page
#: number.
MIN_SEARCHABLE_DIGITS = 4

#: Types whose values are never worth searching for. A boolean is one of two
#: words, a whole-number count is usually one of ten, and a JSON blob is not text
#: any document contains.
_UNSEARCHABLE_TYPES = frozenset({"boolean", "json", "integer"})

_MONTHS = (
    "January",
    "February",
    "March",
    "April",
    "May",
    "June",
    "July",
    "August",
    "September",
    "October",
    "November",
    "December",
)


@dataclass(frozen=True, slots=True)
class NormalisedText:
    """A document's text with whitespace collapsed and case folded, plus the map back.

    `text` is what is searched. `offsets[i]` is the index in the *original* string
    of the character `text[i]` came from, which is what makes a hit reportable: the
    span handed back is in the document's own coordinate system, the same one the
    passages' offsets and `page_offsets` use.

    Built once per document. Normalising a 40-page schedule is a linear pass, and
    doing it per value per field is that pass thirty-eight times over.
    """

    text: str
    offsets: tuple[int, ...]

    @classmethod
    def of(cls, raw: str | None) -> NormalisedText:
        characters: list[str] = []
        offsets: list[int] = []
        pending_space = True  # True at the start, so leading whitespace is dropped.

        for index, character in enumerate(raw or ""):
            if character.isspace():
                if not pending_space:
                    characters.append(" ")
                    offsets.append(index)
                    pending_space = True
                continue
            # `str.lower` can return two characters for a few code points — the
            # dotted capital I is the usual example. Taking the first keeps the
            # offset map aligned one-to-one, which every span here depends on; the
            # alternative is a map that silently drifts after the first such
            # character in the file.
            lowered = character.lower()
            characters.append(lowered if len(lowered) == 1 else lowered[0])
            offsets.append(index)
            pending_space = False

        return cls("".join(characters), tuple(offsets))

    def __bool__(self) -> bool:
        return bool(self.text)

    def find_all(self, needle: str, *, limit: int = 1) -> list[tuple[int, int]]:
        """Spans in the original text where `needle` appears, non-overlapping.

        Substring rather than whole-word, deliberately: `4471` inside
        `CP-4471-88210` is the match a reviewer wants when the broker's email
        quotes the short form of a policy number.
        """
        probe = collapse(needle)
        if not probe or limit <= 0:
            return []

        spans: list[tuple[int, int]] = []
        cursor = 0
        while len(spans) < limit:
            found = self.text.find(probe, cursor)
            if found < 0:
                break
            spans.append(self._span(found, found + len(probe)))
            cursor = found + len(probe)
        return spans

    def find(self, needle: str) -> tuple[int, int] | None:
        """The first span where `needle` appears, or `None`."""
        found = self.find_all(needle, limit=1)
        return found[0] if found else None

    def find_any(self, needles: tuple[str, ...] | list[str]) -> tuple[int, int] | None:
        """The first span matching any of `needles`, in the order given.

        The order is the caller's ranking, not the document's: `variants` returns
        the value as the model read it before any reformatting of it, so a document
        that writes the value exactly is cited exactly.
        """
        for needle in needles:
            found = self.find(needle)
            if found is not None:
                return found
        return None

    def _span(self, start: int, end: int) -> tuple[int, int]:
        """A normalised `[start, end)` as an original `[start, end)`."""
        first = self.offsets[start]
        # `end - 1` is the last matched character; the original span runs one past
        # it. Taking `offsets[end]` instead would include the whitespace that was
        # collapsed after the match, which is what makes a highlight look like it
        # has swallowed the following word.
        last = self.offsets[end - 1]
        return first, last + 1


def collapse(text: str | None) -> str:
    """`text` in the form `NormalisedText.text` is in: folded, single-spaced, trimmed."""
    return " ".join((text or "").lower().split())


def searchable(text: str | None, *, data_type: str = "string") -> bool:
    """Whether looking for this value in another document proves anything.

    The rule is distinctiveness, not length alone. A short value that mixes
    letters and digits — `CP-4471` — is distinctive; a short value that does not —
    `USD`, `Yes`, `0` — is not, and neither is a number with too few digits to be
    anything but a page number or a year.
    """
    if data_type in _UNSEARCHABLE_TYPES:
        return False

    probe = collapse(text)
    if not probe or len(probe) > MAX_SEARCHABLE_CHARACTERS:
        return False

    digits = sum(1 for character in probe if character.isdigit())
    letters = sum(1 for character in probe if character.isalpha())

    if digits and not letters:
        # Purely numeric: distinctiveness is the digit count, so a seven-figure
        # amount qualifies and a two-digit count does not.
        return digits >= MIN_SEARCHABLE_DIGITS

    if len(probe) >= MIN_SEARCHABLE_CHARACTERS:
        return True

    # Shorter than the floor, but a letter-and-digit mix is a reference rather
    # than a word — `A/42`, `CP-1` — and those are worth finding.
    return bool(digits and letters)


def variants(text: str | None, *, data_type: str = "string", typed: Any = None) -> tuple[str, ...]:
    """The forms a document plausibly writes this value in, most exact first.

    `typed` is the coerced value from `app.services.extraction.schema` — an ISO
    string for a date, minor units for money. Variants are generated from it
    rather than by re-parsing the text, so the reading is done once and this
    module never has to guess whether `01/10` is January or October.
    """
    ordered: list[str] = []

    def offer(candidate: str | None) -> None:
        cleaned = collapse(candidate)
        if not cleaned or not searchable(cleaned, data_type=data_type):
            return
        if cleaned not in ordered:
            ordered.append(cleaned)

    # The document's own wording, first: where a second document writes it the
    # same way, that is the citation to make.
    offer(text)

    match data_type:
        case "date" | "datetime":
            for form in _date_forms(typed):
                offer(form)
        case "money":
            for form in _money_forms(typed):
                offer(form)
        case "number":
            for form in _number_forms(typed):
                offer(form)

    return tuple(ordered)


def _date_forms(typed: Any) -> list[str]:
    """A date as the handful of things a claim document prints."""
    parsed = _as_date(typed)
    if parsed is None:
        return []

    day = parsed.day
    month = _MONTHS[parsed.month - 1]
    year = parsed.year
    return [
        f"{day} {month} {year}",
        f"{day:02d} {month} {year}",
        f"{month} {day}, {year}",
        f"{day} {month[:3]} {year}",
        parsed.isoformat(),
        f"{day:02d}/{parsed.month:02d}/{year}",
        f"{parsed.month:02d}/{day:02d}/{year}",
    ]


def _money_forms(typed: Any) -> list[str]:
    """An amount in minor units as the major-unit strings a document prints."""
    if not isinstance(typed, int) or isinstance(typed, bool):
        return []

    major, minor = divmod(abs(typed), 100)
    sign = "-" if typed < 0 else ""
    forms = [f"{sign}{major:,}", f"{sign}{major}"]
    if minor:
        forms = [f"{sign}{major:,}.{minor:02d}", f"{sign}{major}.{minor:02d}", *forms]
    else:
        forms.extend([f"{sign}{major:,}.00", f"{sign}{major}.00"])
    return forms


def _number_forms(typed: Any) -> list[str]:
    if isinstance(typed, bool) or not isinstance(typed, (int, float)):
        return []
    if float(typed).is_integer():
        whole = int(typed)
        return [f"{whole:,}", str(whole)]
    return [f"{typed:,}", str(typed)]


def _as_date(typed: Any) -> date | None:
    if isinstance(typed, str):
        try:
            return datetime.fromisoformat(typed).date()
        except ValueError:
            return None
    if isinstance(typed, datetime):
        return typed.date()
    if isinstance(typed, date):
        return typed
    return None


__all__ = [
    "MAX_SEARCHABLE_CHARACTERS",
    "MIN_SEARCHABLE_CHARACTERS",
    "MIN_SEARCHABLE_DIGITS",
    "NormalisedText",
    "collapse",
    "searchable",
    "variants",
]
