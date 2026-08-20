"""Turning extracted strings into values the database can hold.

This is the layer that refuses. A model that reports a loss on 2026-13-45, an
estimate of "-£40,000", or 300 fatalities in a windscreen claim is not producing
data — it is producing a field that should read as missing, and be reported as
missing by the completeness engine, rather than a wrong value that looks
authoritative in a column.

Every function here returns `None` on anything it cannot vouch for, and none of
them raise: an unparseable field is a normal outcome of reading a broker's email,
not an exception.
"""

from __future__ import annotations

import re
from datetime import UTC, date, datetime
from decimal import Decimal, InvalidOperation

from app.domain import temporal
from app.domain.enums import LineOfBusiness

#: The plausibility window a date of loss has to fall inside. Defined in
#: `app.domain.temporal`, which is where dates are read, and re-exported here
#: because this is the module the rest of the codebase asks about normalisation.
FUTURE_TOLERANCE = temporal.FUTURE_TOLERANCE
MAX_BACKDATE = temporal.MAX_BACKDATE

#: The largest single loss this schema will accept, in minor units. Above it, the
#: value is far more likely to be a misplaced decimal than a real exposure, and a
#: number this size silently entering the reserve is worse than a missing one.
MAX_LOSS_MINOR = 10_000_000_000_00

_CURRENCY_SYMBOLS = {
    "£": "GBP",
    "$": "USD",
    "€": "EUR",
    "s$": "SGD",
    "sgd": "SGD",
    "gbp": "GBP",
    "usd": "USD",
    "eur": "EUR",
}

SUPPORTED_CURRENCIES = frozenset({"GBP", "USD", "EUR", "SGD"})

_AMOUNT_RE = re.compile(r"(-?[\d][\d,\s]*(?:\.\d{1,2})?)")
_EMAIL_RE = re.compile(r"[\w.+-]+@[\w-]+\.[\w.-]+")
_PHONE_RE = re.compile(r"[+()\d][\d\s()+.-]{6,}\d")

_TRUE_WORDS = frozenset({"yes", "true", "y", "confirmed", "present", "1"})
_FALSE_WORDS = frozenset({"no", "false", "n", "none", "absent", "0", "nil"})


def parse_date(value: str | None, *, reference: datetime | None = None) -> date | None:
    """The calendar date a phrase states, whether or not it is a plausible one.

    Reading is `app.domain.temporal`'s job — it knows about discovery clauses,
    day-and-month with no year, and "overnight on Friday", none of which a format
    list can express. `reference` is the notice's own date, which is what anything
    relative is resolved against.
    """
    return temporal.stated_date(value, reference=reference)


def parse_datetime(
    date_value: str | None,
    time_value: str | None = None,
    *,
    reference: datetime | None = None,
) -> datetime | None:
    """A loss date, and its time when one was stated.

    Returns `None` for a date outside the plausible window rather than storing it
    — the future-loss-date exception is raised from the *unparsed* string by the
    exception engine, so refusing here does not hide the problem. Callers that
    want to know *why*, or to show the officer what was inferred, should ask
    `app.domain.temporal.resolve` for the whole reading instead of just its value.
    """
    return temporal.resolve(date_value, time_text=time_value, reference=reference).value


def is_future_date(value: str | None, *, reference: datetime | None = None) -> bool:
    """True when a stated date lies beyond the tolerance. Drives the exception.

    `reference` is the notice's arrival, so "in the future" means later than the
    notification that reports it rather than later than today — which is the
    question actually being asked, and the only one that still means anything on a
    notice loaded from an archive.
    """
    parsed = temporal.stated_date(value, reference=reference)
    if parsed is None:
        return False
    anchor = reference or datetime.now(UTC)
    return parsed > (anchor + FUTURE_TOLERANCE).date()


def parse_currency(value: str | None, *, fallback: str = "GBP") -> str:
    """A three-letter code, from a code or a symbol. Unknown input keeps the fallback."""
    if not value:
        return fallback
    text = value.strip().lower()
    for token, code in _CURRENCY_SYMBOLS.items():
        if token in text:
            return code
    upper = value.strip().upper()
    return upper if upper in SUPPORTED_CURRENCIES else fallback


def parse_money_minor(value: str | None) -> int | None:
    """An amount in minor units. Negative and implausible values are refused."""
    if not value:
        return None

    text = value.strip().lower()
    multiplier = Decimal(1)
    if re.search(r"\d\s*(m|mn|million)\b", text):
        multiplier = Decimal(1_000_000)
    elif re.search(r"\d\s*(k|thousand)\b", text):
        multiplier = Decimal(1_000)

    match = _AMOUNT_RE.search(text)
    if not match:
        return None

    try:
        amount = Decimal(match.group(1).replace(",", "").replace(" ", "")) * multiplier
    except InvalidOperation:
        return None

    if amount < 0:
        return None

    minor = int((amount * 100).to_integral_value())
    return minor if 0 <= minor <= MAX_LOSS_MINOR else None


def parse_count(value: str | None, *, maximum: int = 100_000) -> int | None:
    """A non-negative count. "None reported" reads as zero, not as missing."""
    if not value:
        return None
    text = value.strip().lower()
    if text in _FALSE_WORDS or "no " in f" {text} "[:4]:
        return 0
    match = re.search(r"-?\d+", text.replace(",", ""))
    if not match:
        return None
    count = int(match.group())
    return count if 0 <= count <= maximum else None


def parse_bool(value: str | None) -> bool | None:
    if not value:
        return None
    text = value.strip().lower()
    if text in _TRUE_WORDS:
        return True
    if text in _FALSE_WORDS:
        return False
    return None


def parse_email(value: str | None) -> str | None:
    if not value:
        return None
    match = _EMAIL_RE.search(value)
    return match.group().lower() if match else None


def parse_phone(value: str | None) -> str | None:
    if not value:
        return None
    match = _PHONE_RE.search(value)
    if not match:
        return None
    cleaned = re.sub(r"[^\d+]", "", match.group())
    return cleaned if 7 <= len(cleaned.lstrip("+")) <= 15 else None


#: A UK postcode. Written strictly because the value of this field is that it is
#: *exact*: a partial match on a loose pattern would put two unrelated businesses
#: in the same candidate list and cost the signal its whole discriminating power.
_POSTCODE_RE = re.compile(r"\b([A-Z]{1,2}\d[A-Z\d]?)\s*(\d[A-Z]{2})\b", re.IGNORECASE)

#: A US ZIP, with the optional +4 add-on. Anchored to the whole value rather than
#: searched for, because a bare five-digit number is only a postcode when the
#: field it arrived in says so — which is the case here and nowhere else. Reading
#: a ZIP out of free text needs the state-code guard in
#: `app.domain.policy_extraction`; this function is answering for a value a model
#: was asked to supply *as* a postcode.
_ZIP_RE = re.compile(r"^(\d{5})(?:-(\d{4}))?$")

#: The `MD 21226` tail of a written US address, which is what a model returns when
#: it copies the postcode line rather than the postcode.
_STATE_ZIP_RE = re.compile(r"\b[A-Z]{2}\s+(\d{5})(?:-\d{4})?\b")


def parse_postcode(value: str | None) -> str | None:
    """A postcode in its canonical printed form, or `None`.

    Handles both books this carrier writes on. A UK postcode normalises to
    `LS11 8AX` — outward and inward halves, one space, uppercase. A US ZIP
    normalises to its five digits, discarding the +4: the add-on identifies a
    delivery segment within one postcode area rather than a different place, so
    keeping it would make `21226-1234` and `21226` compare unequal for two
    addresses across the street from each other.

    Anything neither pattern recognises reads as missing rather than being stored
    as a guess: this field exists to be compared exactly, and an approximate
    postcode is worse than none.
    """
    if not value:
        return None
    text = value.strip()

    found = _POSTCODE_RE.search(text)
    if found:
        return f"{found.group(1).upper()} {found.group(2).upper()}"

    zip_only = _ZIP_RE.match(re.sub(r"\s+", "", text))
    if zip_only:
        return zip_only.group(1)

    # A whole address line, or the `Baltimore, MD 21226` tail of one. Guarded by
    # the state code for the reason `policy_extraction` documents at length: a
    # bare five-digit run in free text is far more often a producer code or half a
    # policy number than a postcode.
    in_address = _STATE_ZIP_RE.search(text.upper())
    if in_address:
        return in_address.group(1)

    return None


def parse_line_of_business(value: str | None) -> LineOfBusiness | None:
    """A configured line, or `None`. Never a new one.

    The single most important refusal in this module: a model naming a line the
    carrier does not write must not create it by saying it.
    """
    if not value:
        return None
    key = re.sub(r"[^a-z]+", "_", value.strip().lower()).strip("_")
    aliases = {
        "workers_comp": LineOfBusiness.WORKERS_COMPENSATION,
        "workers_compensation": LineOfBusiness.WORKERS_COMPENSATION,
        "employers_liability": LineOfBusiness.LIABILITY,
        "general_liability": LineOfBusiness.LIABILITY,
        "commercial_property": LineOfBusiness.PROPERTY,
        "motor_fleet": LineOfBusiness.MOTOR,
        "auto": LineOfBusiness.MOTOR,
        "cargo": LineOfBusiness.MARINE,
        "technology": LineOfBusiness.CYBER,
    }
    if key in aliases:
        return aliases[key]
    try:
        return LineOfBusiness(key)
    except ValueError:
        return None


#: Party roles the FNOL model recognises. Anything else becomes `other`.
PARTY_ROLES = frozenset(
    {"claimant", "insured", "broker", "third_party", "witness", "authority", "other"}
)


def parse_party_role(value: str | None) -> str:
    if not value:
        return "other"
    key = re.sub(r"[^a-z]+", "_", value.strip().lower()).strip("_")
    aliases = {
        "policyholder": "insured",
        "insured_party": "insured",
        "agent": "broker",
        "intermediary": "broker",
        "third_party_claimant": "third_party",
        "counterparty": "third_party",
        "police": "authority",
        "fire_service": "authority",
        "regulator": "authority",
        "loss_adjuster": "other",
    }
    key = aliases.get(key, key)
    return key if key in PARTY_ROLES else "other"


def clip(value: str | None, limit: int) -> str | None:
    """Trim a value to a column's width without failing the whole extraction."""
    if value is None:
        return None
    trimmed = value.strip()
    return trimmed[:limit] if trimmed else None
