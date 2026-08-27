"""Deterministic readers for notifications and documents.

These run when no model provider is configured, and they are also what the
classifier cross-checks a model's answer against. They are not a stand-in for
comprehension: they read the labelled fields that broker notifications, portal
exports and TPA bordereaux actually use, and they say `confidence` accordingly —
0.9 for a value read from an explicit `Policy number:` label, 0.4 for one
recovered by pattern from prose, 0 for one not found.

Everything is pure: text in, `FNOLExtraction` out. That makes the fallback path as
testable as the model path, which matters because in a provider outage it becomes
the only path.
"""

from __future__ import annotations

import re
from collections.abc import Iterable
from functools import lru_cache

from app.domain.enums import LineOfBusiness
from app.domain.extraction import (
    AdditionalExtraction,
    ClassificationResult,
    ExtractedField,
    FNOLExtraction,
    LossExtraction,
    NotificationExtraction,
    PartyExtraction,
    PolicyExtraction,
)
from app.domain.normalisation import parse_email, parse_phone
from app.domain.rules import (
    INJURY_SIGNALS,
    LOB_KEYWORDS,
    LOSS_TYPES,
    matches_any,
    signal_pattern,
)

#: Confidence attached to a value read from an explicit label.
LABELLED_CONFIDENCE = 0.9
#: Confidence attached to a value recovered by pattern from running prose.
INFERRED_CONFIDENCE = 0.45

_EMPTY = ExtractedField(value=None, confidence=0.0, evidence=None)

#: `Label: value` on one line, or `Label` followed by the value on the next.
_LABEL_TEMPLATE = r"^[ \t>*\-•]*{labels}[ \t]*[:\-–][ \t]*(?P<value>.+?)[ \t]*$"

_POLICY_NUMBER_RE = re.compile(
    r"\b(?:POL|PL|POLICY|CP|MF|MC|CY|EL|PI)[-/ ]?\d{2,4}[-/ ]?[A-Z0-9]{3,8}\b", re.IGNORECASE
)
_DATE_IN_TEXT_RE = re.compile(
    r"\b(\d{1,2}[/-]\d{1,2}[/-]\d{2,4}"
    r"|\d{4}-\d{2}-\d{2}"
    r"|\d{1,2}\s+(?:Jan|Feb|Mar|Apr|May|Jun|Jul|Aug|Sep|Oct|Nov|Dec)[a-z]*\.?\s+\d{4})\b",
    re.IGNORECASE,
)
_AMOUNT_IN_TEXT_RE = re.compile(
    r"(?:[£$€]|GBP|USD|EUR|SGD)\s?\d[\d,\s]*(?:\.\d{1,2})?(?:\s?(?:k|m|mn|million|thousand))?",
    re.IGNORECASE,
)


#: A line that starts a new labelled field, used to know where the previous
#: one's value ended.
_NEXT_LABEL_RE = re.compile(r"^[ \t>*\-•]*[A-Za-z][A-Za-z /&'()-]{2,40}[ \t]*[:][ \t]")


def _find_labelled(
    text: str, labels: Iterable[str], *, multiline: bool = False
) -> tuple[str, str] | None:
    """The first `Label: value` match, with the line it was read from as evidence.

    `multiline` continues the value onto following lines until a blank line or
    the next labelled field. Broker notifications write the description of the
    loss as a paragraph under `Description:`, and stopping at the first newline
    would file three words of a three-sentence account.
    """
    alternatives = "|".join(re.escape(label) for label in labels)
    pattern = re.compile(
        _LABEL_TEMPLATE.format(labels=f"(?:{alternatives})"),
        re.IGNORECASE | re.MULTILINE,
    )
    match = pattern.search(text)
    if not match:
        return None

    value = match.group("value").strip()
    if not value or value.lower() in {"n/a", "tbc", "unknown", "-"}:
        return None

    if multiline:
        for line in text[match.end() :].splitlines()[1:]:
            stripped = line.strip()
            if not stripped or _NEXT_LABEL_RE.match(line):
                break
            value = f"{value} {stripped}"

    return value, match.group(0).strip()


def _labelled(text: str, labels: Iterable[str], *, multiline: bool = False) -> ExtractedField:
    found = _find_labelled(text, labels, multiline=multiline)
    if found is None:
        return _EMPTY
    value, evidence = found
    return ExtractedField(value=value, confidence=LABELLED_CONFIDENCE, evidence=evidence)


def _inferred(value: str | None, evidence: str | None) -> ExtractedField:
    if not value:
        return _EMPTY
    return ExtractedField(value=value, confidence=INFERRED_CONFIDENCE, evidence=evidence)


def _first(*fields: ExtractedField) -> ExtractedField:
    for candidate in fields:
        if candidate.present:
            return candidate
    return _EMPTY


def _sentence_around(text: str, needle: str, width: int = 160) -> str:
    index = text.find(needle)
    if index < 0:
        return needle
    start = max(0, index - width // 2)
    return text[start : index + len(needle) + width // 2].strip()


def _flag(text: str, words: Iterable[str]) -> ExtractedField:
    """Yes, no, or nothing — three answers, because the question has three.

    The absence of the word is not a statement that the thing did not happen. It
    used to be read as one: this returned `"no"` at 0.3 confidence whenever none of
    `words` appeared, which on a notice that recovers 24 of 30 fields is most
    notices. `parse_bool("no")` then wrote `false` to the column, severity read the
    column with `bool()`, and an environmental exposure nobody had looked for
    scored exactly like one that had been ruled out.

    So: `"yes"` when the notice says so, `"no"` when it says so — a negation beside
    the word is a fact, and "no business interruption" is a real sentence a broker
    writes — and nothing at all when the notice is silent. Nothing at all leaves
    the column `NULL`, which the completeness engine reports as an unanswered
    required field instead of a settled one.
    """
    lowered = text.lower()
    for word in words:
        index = lowered.find(word)
        if index < 0:
            continue
        before = lowered[max(0, index - 30) : index]
        after = lowered[index + len(word) : index + len(word) + 24]
        negated = bool(_NEGATION_RE.search(before) or _NEGATION_RE.search(after))
        return ExtractedField(
            value="no" if negated else "yes",
            confidence=INFERRED_CONFIDENCE,
            evidence=_sentence_around(text, word),
        )
    return _EMPTY


def extract_from_text(text: str, *, sender_email: str | None = None) -> FNOLExtraction:
    """Read a notification the way a person scanning it for fields would."""
    body = text or ""

    policy_number = _first(
        _labelled(body, ("policy number", "policy no", "policy ref", "policy", "certificate")),
        _pattern_field(body, _POLICY_NUMBER_RE),
    )
    date_of_loss = _first(
        _labelled(
            body,
            (
                "date of loss",
                "loss date",
                "date of incident",
                "incident date",
                "date of accident",
                "occurred on",
            ),
        ),
        _pattern_field(body, _DATE_IN_TEXT_RE),
    )
    estimate = _first(
        _labelled(
            body,
            (
                "estimated loss",
                "estimate",
                "estimated value",
                "reserve",
                "estimated cost",
                "loss estimate",
                "quantum",
            ),
        ),
        _pattern_field(body, _AMOUNT_IN_TEXT_RE),
    )

    reporter_email = _first(
        _labelled(body, ("reporter email", "contact email", "email", "e-mail", "from")),
        _inferred(parse_email(body), None),
        _inferred(sender_email, "message envelope") if sender_email else _EMPTY,
    )

    notification = NotificationExtraction(
        reporter_name=_labelled(
            body, ("reported by", "reporter", "contact name", "notified by", "contact")
        ),
        reporter_organisation=_labelled(
            body, ("broker", "brokerage", "reporter organisation", "company", "firm", "tpa")
        ),
        reporter_role=_labelled(body, ("role", "position", "job title", "capacity")),
        reporter_email=reporter_email,
        reporter_phone=_first(
            _labelled(body, ("phone", "telephone", "contact number", "mobile", "tel")),
            _inferred(parse_phone(body), None),
        ),
    )

    policy = PolicyExtraction(
        policy_number=policy_number,
        insured_name=_labelled(
            body, ("insured", "insured name", "policyholder", "assured", "client")
        ),
        insured_organisation=_labelled(
            body, ("insured organisation", "insured company", "organisation")
        ),
        policy_type=_labelled(body, ("policy type", "product", "cover type", "class of business")),
        effective_date=_labelled(body, ("effective date", "inception", "inception date")),
        expiry_date=_labelled(body, ("expiry date", "expiry", "renewal date")),
    )

    loss = LossExtraction(
        date_of_loss=date_of_loss,
        time_of_loss=_labelled(body, ("time of loss", "loss time", "time of incident", "time")),
        loss_location=_labelled(
            body, ("location", "loss location", "site", "address", "place of loss", "risk address")
        ),
        loss_country=_labelled(body, ("country", "territory")),
        loss_description=_first(
            _labelled(
                body,
                (
                    "description",
                    "description of loss",
                    "incident description",
                    "details",
                    "circumstances",
                    "what happened",
                    "summary",
                ),
                multiline=True,
            ),
            _longest_paragraph(body),
        ),
        cause_of_loss=_labelled(body, ("cause", "cause of loss", "peril", "proximate cause")),
        affected_assets=_labelled(
            body,
            (
                "affected property",
                "affected assets",
                "damaged property",
                "vehicle",
                "asset",
                "property affected",
                "vessel",
                "plant",
            ),
        ),
        injuries=_first(
            _labelled(body, ("injuries", "injured", "casualties")),
            _flag_count(body, _INJURY_STEMS, indicators=_INJURY_INDICATORS),
        ),
        fatalities=_first(
            _labelled(body, ("fatalities", "deaths", "fatal")),
            _flag_count(body, _FATALITY_STEMS, indicators=_FATALITY_INDICATORS),
        ),
        estimated_loss_amount=estimate,
        currency=_labelled(body, ("currency", "ccy")),
        business_interruption=_flag(
            body,
            (
                "business interruption",
                "trading halted",
                "unable to trade",
                "downtime",
                "operations suspended",
                "production stopped",
            ),
        ),
        structural_damage=_flag(
            body, ("structural", "roof collapse", "collapse", "building damage", "wall failure")
        ),
        environmental_exposure=_flag(
            body, ("contamination", "pollution", "spill", "environmental", "leak into")
        ),
    )

    additional = AdditionalExtraction(
        police_reference=_labelled(
            body, ("police reference", "police ref", "crime reference", "crime ref")
        ),
        incident_reference=_labelled(
            body, ("incident reference", "incident ref", "your ref", "our ref", "reference")
        ),
        authorities_involved=_labelled(
            body, ("authorities", "emergency services", "attended by", "regulator")
        ),
        repair_estimate_amount=_labelled(
            body, ("repair estimate", "repair cost", "reinstatement cost", "quotation")
        ),
        potential_litigation=_flag(
            body, ("solicitor", "letter of claim", "litigation", "legal action", "court")
        ),
        supporting_documents=_labelled(
            body, ("attachments", "enclosed", "supporting documents", "documents attached")
        ),
    )

    parties = _extract_parties(policy, notification)

    present = sum(
        1
        for field in (
            *_fields_of(notification),
            *_fields_of(policy),
            *_fields_of(loss),
            *_fields_of(additional),
        )
        if field.present
    )
    total = len(
        [*_fields_of(notification), *_fields_of(policy), *_fields_of(loss), *_fields_of(additional)]
    )

    return FNOLExtraction(
        notification=notification,
        policy=policy,
        loss=loss,
        additional=additional,
        parties=parties,
        overall_confidence=round(present / total, 2) if total else 0.0,
    )


def _fields_of(model: object) -> list[ExtractedField]:
    return [value for value in vars(model).values() if isinstance(value, ExtractedField)]


def _pattern_field(text: str, pattern: re.Pattern[str]) -> ExtractedField:
    match = pattern.search(text)
    if not match:
        return _EMPTY
    return ExtractedField(
        value=match.group(0).strip(),
        confidence=INFERRED_CONFIDENCE,
        evidence=_sentence_around(text, match.group(0)),
    )


_NEGATION_RE = re.compile(r"\b(no|none|nil|zero|without|not)\b")
_COUNT_BEFORE_RE = re.compile(r"\b(\d{1,3})\b\s*\w{0,12}$")
_COUNT_AFTER_RE = re.compile(r"^\W{0,3}(\d{1,3})\b")

#: Numbers a notice spells out. `both` is here because "both were hospitalised" is
#: how a broker says two without saying two.
_SPELLED_COUNTS: dict[str, int] = {
    "a": 1,
    "an": 1,
    "one": 1,
    "both": 2,
    "two": 2,
    "three": 3,
    "four": 4,
    "five": 5,
    "six": 6,
    "seven": 7,
    "eight": 8,
    "nine": 9,
    "ten": 10,
    "eleven": 11,
    "twelve": 12,
}

#: Ordinals, which state a floor rather than a count: "a third was cut" says three
#: people are in this incident without saying how many in total.
_ORDINAL_COUNTS: dict[str, int] = {
    "second": 2,
    "third": 3,
    "fourth": 4,
    "fifth": 5,
    "sixth": 6,
    "seventh": 7,
    "eighth": 8,
    "ninth": 9,
    "tenth": 10,
}

#: The nouns a notice counts people with.
_PERSON_NOUNS: tuple[str, ...] = (
    "employees",
    "employee",
    "workers",
    "worker",
    "operatives",
    "operative",
    "labourers",
    "labourer",
    "persons",
    "person",
    "people",
    "individuals",
    "individual",
    "occupants",
    "occupant",
    "residents",
    "resident",
    "staff",
    "crew",
    "contractors",
    "contractor",
    "subcontractors",
    "subcontractor",
    "pedestrians",
    "pedestrian",
    "passengers",
    "passenger",
    "drivers",
    "driver",
    "casualties",
    "casualty",
    "others",
    "other",
    "men",
    "women",
)

_GROUP_COUNT_RE = re.compile(
    r"\b(?P<count>\d{1,3}|"
    + "|".join(sorted(_SPELLED_COUNTS, key=len, reverse=True))
    + r")\s+(?:further\s+|other\s+|more\s+|of\s+the\s+)?(?P<noun>"
    + "|".join(_PERSON_NOUNS)
    + r")\b"
)
_ORDINAL_RE = re.compile(
    r"\b(?:a|the)\s+(?P<ordinal>" + "|".join(_ORDINAL_COUNTS) + r")\b(?!\s+(?:party|parties))"
)
#: What the labelled reader looks for, and the wider vocabulary each count is
#: allowed to read a sentence with. Kept apart because "hospitalised" says somebody
#: was injured and says nothing whatever about anybody having died.
_INJURY_STEMS: tuple[str, ...] = ("injur", "casualt")
#: The same two, marked as stems for `matches_any` — see `rules.signal_pattern`.
_INJURY_STEM_TERMS: tuple[str, ...] = ("injur*", "casualt*")
_INJURY_INDICATORS: tuple[str, ...] = _INJURY_STEM_TERMS + INJURY_SIGNALS
_FATALITY_STEMS: tuple[str, ...] = ("fatalit", "died", "death")
_FATALITY_INDICATORS: tuple[str, ...] = (
    "fatalit*",
    "fatal",
    "died",
    "death",
    "deceased",
    "killed",
    "pronounced dead",
    "loss of life",
)

#: A clause, for the purpose of asking "does this one talk about someone hurt".
#:
#: Terminators and semicolons, not commas. The semicolon earns its place: "one
#: operative died at the scene; two others were hospitalised" states a fatality and
#: two injuries, and read as one clause the injury count and the fatality count both
#: come back as two. Commas do not, because "two employees were buried, one has a
#: crush injury, a third was cut" is one account of one incident and splitting it
#: strands the third casualty in a clause with no injury word of its own.
_CASUALTY_CLAUSE_RE = re.compile(r"(?<=[.!?;])\s+")


def _casualty_floor(text: str, indicators: Iterable[str]) -> tuple[int, str] | None:
    """The smallest number of people the prose can be read as accounting for.

    A floor, not a count, and stated as one. Two independent readings contribute
    and the larger wins: a counted group ("two employees were buried") and an
    ordinal ("a third was cut"), which names no group but says a third person is in
    this incident. On the notice that prompted this — "Two employees were buried to
    chest height; one has a crush injury and a third was cut" — the group says two,
    the ordinal says three, and three is the answer. The old reader said one,
    because no digit sat next to the word "injury".

    Only sentences that mention this kind of casualty are counted, so "two employees
    discovered the smoke" is not two casualties — and `indicators` is a parameter
    rather than a constant so that the fatality reader cannot count a sentence about
    two people in hospital. Returns `None` when the prose supports no number at all.
    """
    marks = tuple(indicators)
    best = 0
    evidence = ""
    counted_a_group = False

    for sentence in _CASUALTY_CLAUSE_RE.split(text):
        lowered = sentence.lower()
        if not matches_any(lowered, marks):
            continue
        if _NEGATION_RE.search(lowered):
            # "No employees were injured" counts nobody, and a sentence mixing a
            # negation with a count is not one to guess at.
            continue

        groups = [
            int(match["count"])
            if match["count"].isdigit()
            else _SPELLED_COUNTS.get(match["count"], 0)
            for match in _GROUP_COUNT_RE.finditer(lowered)
        ]
        counted_a_group = counted_a_group or bool(groups)
        readings = groups + [
            _ORDINAL_COUNTS[match["ordinal"]] for match in _ORDINAL_RE.finditer(lowered)
        ]
        found = max(readings, default=0)
        if found > best:
            best, evidence = found, sentence.strip()

    if counted_a_group:
        # An ordinal extends a count that is already established; it never creates
        # one. "A third was cut during the rescue" is a third casualty in a notice
        # that has already said two people were buried, and the clause it sits in
        # need not carry an injury word of its own — real notices put the mechanism
        # in one clause and the people in the next.
        #
        # The guard is what keeps this from reading "No fatalities." plus "A third
        # was cut" as three deaths: nothing counted a group of fatalities, so
        # nothing here can extend one.
        for match in _ORDINAL_RE.finditer(text.lower()):
            value = _ORDINAL_COUNTS[match["ordinal"]]
            if value > best:
                best, evidence = value, _sentence_around(text, match.group(0))

    return (best, evidence) if best else None


def _flag_count(
    text: str, stems: Iterable[str], *, indicators: Iterable[str] | None = None
) -> ExtractedField:
    """How many people, when the notice says so in prose rather than in a field.

    "No injuries reported" is a fact, and so is "Fatalities: none" — the negation
    can fall either side of the word, which is why both are looked at. The absence
    of the word entirely is *not* a fact, and returns nothing rather than zero.

    Where the word is present and un-negated, the prose is counted before a number
    is assumed. Assuming one was the defect: a three-casualty trench collapse read
    as a single injury because its counts were spelled out and its third casualty
    was named by an ordinal, and a single injury does not clear the multiple-injury
    severity floor. One survives as the *last* answer rather than the first, and at
    a confidence low enough that the completeness engine marks it worth checking —
    which is the honest reading of "somebody was hurt and the notice does not say
    how many".

    `indicators` is the wider vocabulary that marks a sentence as being about this
    kind of casualty — every way a notice says somebody was hurt, not just the two
    stems the labelled reader looks for. "Two operatives suffered burns" reports two
    injuries and contains neither "injur" nor "casualt"; without the wider list the
    reader saw nothing at all. It stays a parameter because the two callers must not
    share it: `hospitalised` is an injury indicator and emphatically not a fatality
    one.
    """
    stem_list = tuple(stems)
    marks = tuple(indicators) if indicators is not None else stem_list
    lowered = text.lower()
    for stem in stem_list:
        index = lowered.find(stem)
        if index < 0:
            continue

        before = lowered[max(0, index - 30) : index]
        after = lowered[index + len(stem) : index + len(stem) + 24]

        if _NEGATION_RE.search(before) or _NEGATION_RE.search(after):
            return ExtractedField(
                value="0",
                confidence=INFERRED_CONFIDENCE,
                evidence=_sentence_around(text, stem),
            )

        digits = _COUNT_BEFORE_RE.search(before) or _COUNT_AFTER_RE.search(after)
        if digits:
            return ExtractedField(
                value=digits.group(1),
                confidence=INFERRED_CONFIDENCE,
                evidence=_sentence_around(text, stem),
            )

        counted = _casualty_floor(text, marks)
        if counted is not None:
            return ExtractedField(
                value=str(counted[0]),
                confidence=INFERRED_CONFIDENCE,
                evidence=counted[1] or _sentence_around(text, stem),
            )

        return ExtractedField(value="1", confidence=0.3, evidence=_sentence_around(text, stem))

    # No stem anywhere, which is not the same as nothing having happened: the
    # notice may report the casualty in words the labelled reader does not look
    # for. A floor read out of the prose is worth having; a bare indicator with no
    # number is worth one person, said quietly.
    counted = _casualty_floor(text, marks)
    if counted is not None:
        return ExtractedField(
            value=str(counted[0]), confidence=INFERRED_CONFIDENCE, evidence=counted[1]
        )
    found = signal_pattern(marks).search(lowered)
    if found is not None:
        # Sliced out of the original rather than looked up by the lowered match, so
        # the evidence quotes the notice as the broker wrote it.
        start, end = found.span()
        return ExtractedField(
            value="1",
            confidence=0.3,
            evidence=_sentence_around(text, text[start:end]),
        )
    return _EMPTY


def _longest_paragraph(text: str) -> ExtractedField:
    """The body of the notice, when nothing was explicitly labelled a description.

    A broker who writes three paragraphs and labels none of them has still
    described the loss; the longest one is that description far more often than
    it is not.
    """
    paragraphs = [
        paragraph.strip()
        for paragraph in re.split(r"\n\s*\n", text)
        if len(paragraph.strip()) > 80 and ":" not in paragraph[:24]
    ]
    if not paragraphs:
        return _EMPTY
    longest = max(paragraphs, key=len)
    return ExtractedField(value=longest[:2000], confidence=0.35, evidence=longest[:200])


def _extract_parties(
    policy: PolicyExtraction, notification: NotificationExtraction
) -> list[PartyExtraction]:
    """The parties a labelled notice states outright.

    Deliberately conservative — the deterministic reader claims the insured and
    the reporter, and leaves inferring witnesses and third parties from prose to a
    model that can actually read it.
    """
    parties: list[PartyExtraction] = []
    if policy.insured_name.present:
        parties.append(
            PartyExtraction(
                role="insured",
                name=policy.insured_name.value or "",
                organisation=policy.insured_organisation.value,
                email=None,
                phone=None,
                confidence=policy.insured_name.confidence,
            )
        )
    if notification.reporter_name.present:
        parties.append(
            PartyExtraction(
                role="broker" if notification.reporter_organisation.present else "other",
                name=notification.reporter_name.value or "",
                organisation=notification.reporter_organisation.value,
                email=notification.reporter_email.value,
                phone=notification.reporter_phone.value,
                confidence=notification.reporter_name.confidence,
            )
        )
    return parties


@lru_cache(maxsize=512)
def _boundaried(keyword: str) -> re.Pattern[str]:
    """One keyword as a pattern that only matches whole words.

    `car` is in the motor keyword list and `Carbondale Avenue` is a street. Plain
    `in` scored that street as a motor claim, which is the same defect as reading a
    broker's programme list as a description of the loss: a hit counter that cannot
    tell what it is counting.

    A leading boundary always. A trailing one — with an optional plural — when the
    keyword ends in a word character, so `d&o` still works and `sprinkler` still
    reaches "the sprinklers operated", which is how a notice writes it. The plural is
    not optional politeness: without it the boundary fix traded one silent miss for
    another, and a fire in a sprinklered building read as no line of business at all.
    """
    if not keyword[-1:].isalnum():
        return re.compile(r"\b" + re.escape(keyword), re.IGNORECASE)
    return re.compile(r"\b" + re.escape(keyword) + r"(?:e?s)?\b", re.IGNORECASE)


def _keyword_hits(lowered: str, keywords: Iterable[str]) -> list[str]:
    """Which of `keywords` the text actually uses as words."""
    return [keyword for keyword in keywords if _boundaried(keyword).search(lowered)]


#: Words that mark a clause as being about the insurance *programme* rather than
#: about the loss. On their own they decide nothing — plenty of loss descriptions
#: mention a policy — which is why `_is_cover_listing` also demands that the clause
#: enumerate cover types before its words are set aside.
_PROGRAMME_CONTEXT: tuple[str, ...] = (
    "placement",
    "placements",
    "placed",
    "programme",
    "program",
    "policies",
    "policy types",
    "lines of cover",
    "lines placed",
    "cover in place",
    "covers in place",
    "insurance in place",
    "schedule of insurance",
    "own policies",
    "are the ones engaged",
    "coverages",
    "excess layer",
    "in force",
    "towers",
)

#: Words that name a cover without appearing in `LOB_KEYWORDS`. A broker listing a
#: programme uses these next to the words that *are* in the table, and counting
#: them is what lets a five-item list be recognised as five items.
_COVER_NAMES: tuple[str, ...] = (
    "excess",
    "umbrella",
    "workers compensation",
    "workers' compensation",
    "workers comp",
    "employers liability",
    "general liability",
    "auto",
    "inland marine",
    "builders risk",
    "installation floater",
    "equipment",
    "pollution",
    "professional",
    "crime",
    "surety",
    "d&o",
    "e&o",
    "epli",
)

#: How a clause is broken into the things it lists.
_LIST_SPLIT_RE = re.compile(r"[,;:]|\band\b|\bor\b")
#: How the notice is broken into clauses. Terminators only — *not* newlines. A
#: broker's covering email is hard-wrapped, so a newline falls in the middle of the
#: sentence that matters here: `fnol_scenarios` wraps "…their own placements are the
#: ones / engaged: liability, excess, workers compensation…" across two lines, and
#: splitting there put the programme word in one clause and its list in another so
#: neither half looked like a cover listing. Whitespace is collapsed first.
_CLAUSE_SPLIT_RE = re.compile(r"[.!?]+")
_WHITESPACE_RE = re.compile(r"\s+")


def _names_a_cover(item: str) -> bool:
    """Whether one item of a list reads as the name of a cover rather than a fact.

    Short on purpose: "liability" names a cover, "the excavator's boom struck the
    live 11kV cable" does not, and length is most of the difference between them.
    """
    words = item.split()
    if not words or len(words) > 3:
        return False
    if _keyword_hits(item, _COVER_NAMES):
        return True
    return any(_keyword_hits(item, keywords) for keywords in LOB_KEYWORDS.values())


def _is_cover_listing(clause: str) -> bool:
    """Whether a clause lists the covers a risk carries instead of describing a loss.

    The case this exists for is a real one, and it cost a classification: a broker
    wrote "their own placements are the ones engaged: liability, excess, workers
    compensation, contractors equipment, motor and possibly contractors pollution",
    and the tally read `liability`, `motor` and `pollution` out of it as evidence
    about a trench collapse. They are evidence about the insurance programme. The
    sentence names five products; it does not say a single thing about the loss.

    Both halves are required, and each makes the other safe. A programme word alone
    would set aside "the policy covers the sprinkler system"; a list alone would set
    aside "a van, a car and a lorry were damaged", which is a motor loss and says so.
    """
    lowered = clause.lower()
    if not any(term in lowered for term in _PROGRAMME_CONTEXT):
        return False
    items = [item.strip() for item in _LIST_SPLIT_RE.split(lowered) if item.strip()]
    return sum(1 for item in items if _names_a_cover(item)) >= 3


def _split_loss_clauses(text: str) -> tuple[str, int]:
    """`(the clauses that describe the loss, how many were set aside)`.

    The count is returned rather than inferred from the lengths, because the split
    drops the terminators too: comparing the joined result against the input says
    "something was removed" about every notice that contains a full stop.
    """
    kept: list[str] = []
    dropped = 0
    for clause in _CLAUSE_SPLIT_RE.split(_WHITESPACE_RE.sub(" ", text or "")):
        if _is_cover_listing(clause):
            dropped += 1
        else:
            kept.append(clause)
    return " ".join(kept), dropped


def loss_clauses(text: str) -> str:
    """`text` with the clauses that enumerate cover types removed."""
    return _split_loss_clauses(text)[0]


#: The order an exact tie resolves in, and the reason it is written down.
#:
#: `max()` over a dict resolved ties by insertion order, which meant the answer to
#: a genuinely three-way notice was a fact about how `LOB_KEYWORDS` happened to be
#: typed. Stated here instead, strictest line first: the required-field set now
#: composes over the signals the notice actually carries
#: (`app.domain.assessment.claim_signals`), so a tie costs far less than it did —
#: and where it still costs something, over-asking for detail is the cheaper error.
_TIE_BREAK_ORDER: tuple[LineOfBusiness, ...] = (
    LineOfBusiness.WORKERS_COMPENSATION,
    LineOfBusiness.CASUALTY,
    LineOfBusiness.LIABILITY,
    LineOfBusiness.CYBER,
    LineOfBusiness.MARINE,
    LineOfBusiness.MOTOR,
    LineOfBusiness.CONSTRUCTION,
    LineOfBusiness.ENGINEERING,
    LineOfBusiness.SPECIALTY,
    LineOfBusiness.PROPERTY,
)


def classify_from_text(
    text: str, *, policy_line: LineOfBusiness | None = None
) -> ClassificationResult:
    """Score the configured keyword sets against the notice.

    When a policy has already been matched, its line is weighted heavily but not
    treated as final: a motor policy can carry a liability claim, and the words in
    the notice are evidence about which one this is.

    Only the clauses that describe the loss are scored. A broker who lists the
    programme in the covering email is telling you what is in force, not what
    happened, and reading those words as evidence about the loss is how a trench
    collapse became a three-way tie between liability, casualty and construction.
    """
    lowered, set_aside = _split_loss_clauses((text or "").lower())
    scores: dict[LineOfBusiness, float] = {}

    for line, keywords in LOB_KEYWORDS.items():
        hits = _keyword_hits(lowered, keywords)
        if hits:
            scores[line] = len(hits) + 0.5 * sum(len(hit.split()) - 1 for hit in hits)

    if policy_line is not None:
        scores[policy_line] = scores.get(policy_line, 0.0) + 3.0

    if not scores:
        return ClassificationResult(
            line_of_business=LineOfBusiness.UNKNOWN.value,
            claim_type=None,
            loss_type=None,
            complexity="standard",
            confidence=0.0,
            reasoning=(
                "The notification names the covers in force but does not describe the "
                "loss in terms of any of them."
                if set_aside
                else "No line-of-business terms were found in the notification."
            ),
        )

    top = max(scores.values())
    tied = [line for line, score in scores.items() if score == top]
    # Stated precedence, then the table's order as the last resort — never the
    # dict's, which is what "whichever line was typed first" reduces to.
    line = min(
        tied,
        key=lambda candidate: (
            _TIE_BREAK_ORDER.index(candidate)
            if candidate in _TIE_BREAK_ORDER
            else len(_TIE_BREAK_ORDER),
            candidate.value,
        ),
    )
    runner_up = max((score for other, score in scores.items() if other is not line), default=0.0)
    # Confidence is the margin over the next-best line, not the raw hit count: two
    # lines scoring 4 each is a genuinely ambiguous notice.
    confidence = min(0.85, 0.35 + 0.1 * (top - runner_up))

    matched = _keyword_hits(lowered, LOB_KEYWORDS.get(line, ()))
    reasoning = (
        f"Matched {len(matched)} {line.value.replace('_', ' ')} terms"
        + (f" ({', '.join(matched[:4])})" if matched else "")
        + (" and the matched policy's line of business" if policy_line == line else "")
        + "."
    )
    if len(tied) > 1:
        # Said out loud rather than hidden in a low confidence. The officer is the
        # one who can tell which of three lines a notice belongs to, and cannot do
        # it without being told there was a question.
        others = ", ".join(other.value.replace("_", " ") for other in tied if other is not line)
        reasoning += (
            f" The notice scores equally as {others}, so this reading is a tie-break "
            "rather than a conclusion."
        )

    return ClassificationResult(
        line_of_business=line.value,
        claim_type=None,
        loss_type=_loss_type_from_text(lowered, line),
        complexity="standard",
        confidence=round(confidence, 2),
        reasoning=reasoning,
    )


#: Phrases that contain a loss type's name without describing that loss.
#:
#: `fire` is the case that matters and the reason this table exists: it is the
#: first loss type configured on the property line, and a claim notice mentions
#: the fire service, a fire alarm or a fire door far more often than it describes
#: a fire. Every entry here is a noun phrase naming an organisation, a piece of
#: equipment or a document — never an event.
_NOT_THE_LOSS = (
    "fire department",
    "fire brigade",
    "fire service",
    "fire and rescue",
    "fire authority",
    "fire marshal",
    "fire officer",
    "fire crew",
    "firefighter",
    "fire alarm",
    "fire suppression",
    "fire sprinkler",
    "fire hydrant",
    "fire door",
    "fire escape",
    "fire extinguisher",
    "fire risk assessment",
    "fire certificate",
    "flood defence",
    "flood plain",
    "flood zone",
    "theft alarm",
)


def _loss_type_from_text(lowered: str, line: LineOfBusiness) -> str | None:
    """The configured loss type the notice talks about most, or `None`.

    Counted rather than taken in list order. Order was the whole defect: `fire`
    leads the property line, so one mention of the attending fire brigade outranked
    six of the escape of water that actually happened. Counting makes the answer
    depend on the notice instead of on the table, and ties still fall back to table
    order so a genuinely balanced notice reads as it always did.

    Still deliberately crude — it is a cross-check on a model and the reader of
    last resort when no model is configured, not a classifier. It says nothing
    about negation, so "there was no fire" counts as a mention; that costs a
    confidence point on a rare notice, where mis-weighting the common ones cost the
    answer.
    """
    scores: list[tuple[int, int, str]] = []
    for order, loss_type in enumerate(LOSS_TYPES.get(line, ())):
        if loss_type == "other":
            continue
        phrase = loss_type.replace("_", " ")
        hits = len(re.findall(rf"\b{re.escape(phrase)}\b", lowered))
        if not hits:
            continue
        # A mention inside one of the phrases above is not a mention of the loss.
        for decoy in _NOT_THE_LOSS:
            if phrase in decoy:
                hits -= lowered.count(decoy)
        if hits > 0:
            scores.append((hits, -order, loss_type))

    if not scores:
        return None
    return max(scores)[2]
