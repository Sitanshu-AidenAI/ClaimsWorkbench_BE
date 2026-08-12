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
from app.domain.rules import LOB_KEYWORDS, LOSS_TYPES

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
    lowered = text.lower()
    for word in words:
        if word in lowered:
            return ExtractedField(
                value="yes", confidence=INFERRED_CONFIDENCE, evidence=_sentence_around(text, word)
            )
    return ExtractedField(value="no", confidence=0.3, evidence=None)


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
            _flag_count(body, ("injur", "casualt")),
        ),
        fatalities=_first(
            _labelled(body, ("fatalities", "deaths", "fatal")),
            _flag_count(body, ("fatalit", "died", "death")),
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


def _flag_count(text: str, stems: Iterable[str]) -> ExtractedField:
    """How many people, when the notice says so in prose rather than in a field.

    "No injuries reported" is a fact, and so is "Fatalities: none" — the negation
    can fall either side of the word, which is why both are looked at. The absence
    of the word entirely is *not* a fact, and returns nothing rather than zero.
    """
    lowered = text.lower()
    for stem in stems:
        index = lowered.find(stem)
        if index < 0:
            continue

        before = lowered[max(0, index - 30) : index]
        after = lowered[index + len(stem) : index + len(stem) + 24]

        if _NEGATION_RE.search(before) or _NEGATION_RE.search(after):
            value = "0"
        else:
            digits = _COUNT_BEFORE_RE.search(before) or _COUNT_AFTER_RE.search(after)
            value = digits.group(1) if digits else "1"

        return ExtractedField(
            value=value, confidence=INFERRED_CONFIDENCE, evidence=_sentence_around(text, stem)
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


def classify_from_text(
    text: str, *, policy_line: LineOfBusiness | None = None
) -> ClassificationResult:
    """Score the configured keyword sets against the notice.

    When a policy has already been matched, its line is weighted heavily but not
    treated as final: a motor policy can carry a liability claim, and the words in
    the notice are evidence about which one this is.
    """
    lowered = (text or "").lower()
    scores: dict[LineOfBusiness, float] = {}

    for line, keywords in LOB_KEYWORDS.items():
        hits = [keyword for keyword in keywords if keyword in lowered]
        if hits:
            scores[line] = len(hits) + 0.5 * sum(len(hit.split()) - 1 for hit in hits)

    if policy_line is not None:
        scores[policy_line] = scores.get(policy_line, 0.0) + 3.0

    if not scores:
        return ClassificationResult(
            line_of_business=LineOfBusiness.UNKNOWN,
            claim_type=None,
            loss_type=None,
            complexity="standard",
            confidence=0.0,
            reasoning="No line-of-business terms were found in the notification.",
        )

    line, score = max(scores.items(), key=lambda item: item[1])
    runner_up = sorted(scores.values(), reverse=True)[1] if len(scores) > 1 else 0.0
    # Confidence is the margin over the next-best line, not the raw hit count: two
    # lines scoring 4 each is a genuinely ambiguous notice.
    confidence = min(0.85, 0.35 + 0.1 * (score - runner_up))

    matched = [keyword for keyword in LOB_KEYWORDS.get(line, ()) if keyword in lowered]
    reasoning = (
        f"Matched {len(matched)} {line.value.replace('_', ' ')} terms"
        + (f" ({', '.join(matched[:4])})" if matched else "")
        + (" and the matched policy's line of business" if policy_line == line else "")
        + "."
    )

    return ClassificationResult(
        line_of_business=line.value,
        claim_type=None,
        loss_type=_loss_type_from_text(lowered, line),
        complexity="standard",
        confidence=round(confidence, 2),
        reasoning=reasoning,
    )


def _loss_type_from_text(lowered: str, line: LineOfBusiness) -> str | None:
    """The first configured loss type whose name appears in the notice."""
    for loss_type in LOSS_TYPES.get(line, ()):
        if loss_type == "other":
            continue
        if loss_type.replace("_", " ") in lowered:
            return loss_type
    return None
