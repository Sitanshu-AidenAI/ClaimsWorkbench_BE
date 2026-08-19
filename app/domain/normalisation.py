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
from datetime import UTC, date, datetime, timedelta
from decimal import Decimal, InvalidOperation

from app.domain.enums import LineOfBusiness

#: Nobody notifies a loss more than a working day into the future; a later date is
#: a typo or a misread year, and either way it is not a date of loss.
FUTURE_TOLERANCE = timedelta(days=1)

#: A claim older than this is not being notified for the first time.
MAX_BACKDATE = timedelta(days=365 * 10)

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

_DATE_FORMATS = (
    "%Y-%m-%d",
    "%d/%m/%Y",
    "%d-%m-%Y",
    "%d.%m.%Y",
    "%d %B %Y",
    "%d %b %Y",
    "%B %d %Y",
    "%b %d %Y",
    "%Y/%m/%d",
)

_TIME_RE = re.compile(r"\b([01]?\d|2[0-3])[:.]([0-5]\d)\b")
_AMOUNT_RE = re.compile(r"(-?[\d][\d,\s]*(?:\.\d{1,2})?)")
_EMAIL_RE = re.compile(r"[\w.+-]+@[\w-]+\.[\w.-]+")
_PHONE_RE = re.compile(r"[+()\d][\d\s()+.-]{6,}\d")

_TRUE_WORDS = frozenset({"yes", "true", "y", "confirmed", "present", "1"})
_FALSE_WORDS = frozenset({"no", "false", "n", "none", "absent", "0", "nil"})


def parse_date(value: str | None) -> date | None:
    """A calendar date from whatever a human or a model wrote."""
    if not value:
        return None
    text = value.strip().replace(",", " ")
    text = re.sub(r"\s+", " ", text)
    text = re.sub(r"(\d)(st|nd|rd|th)\b", r"\1", text, flags=re.IGNORECASE)

    for fmt in _DATE_FORMATS:
        try:
            return datetime.strptime(text, fmt).date()
        except ValueError:
            continue

    # An ISO timestamp with a time part, which is what an API channel supplies.
    try:
        return datetime.fromisoformat(text.replace("Z", "+00:00")).date()
    except ValueError:
        return None


def parse_datetime(date_value: str | None, time_value: str | None = None) -> datetime | None:
    """A loss date, and its time when one was stated.

    Returns `None` for a date outside the plausible window rather than storing it
    — the future-loss-date exception is raised from the *unparsed* string by the
    exception engine, so refusing here does not hide the problem.
    """
    parsed = parse_date(date_value)
    if parsed is None:
        return None

    hour, minute = 0, 0
    if time_value:
        match = _TIME_RE.search(time_value)
        if match:
            hour, minute = int(match.group(1)), int(match.group(2))

    stamp = datetime(parsed.year, parsed.month, parsed.day, hour, minute, tzinfo=UTC)
    now = datetime.now(UTC)
    if stamp > now + FUTURE_TOLERANCE or stamp < now - MAX_BACKDATE:
        return None
    return stamp


def is_future_date(value: str | None) -> bool:
    """True when a parseable date lies beyond the tolerance. Drives the exception."""
    parsed = parse_date(value)
    if parsed is None:
        return False
    return parsed > (datetime.now(UTC) + FUTURE_TOLERANCE).date()


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


#: A UK postcode, and only a UK postcode. Written strictly because the value of
#: this field is that it is *exact*: a partial match on a loose pattern would put
#: two unrelated businesses in the same candidate list and cost the signal its
#: whole discriminating power.
_POSTCODE_RE = re.compile(r"\b([A-Z]{1,2}\d[A-Z\d]?)\s*(\d[A-Z]{2})\b", re.IGNORECASE)


def parse_postcode(value: str | None) -> str | None:
    """A postcode in its canonical printed form, or `None`.

    Normalised to `LS11 8AX` — outward and inward halves, one space, uppercase —
    so that a postcode read from a claim form and one read from an email compare
    equal. Anything the pattern does not recognise reads as missing rather than
    being stored as a guess: this field exists to be compared exactly, and an
    approximate postcode is worse than none.
    """
    if not value:
        return None
    found = _POSTCODE_RE.search(value)
    if not found:
        return None
    return f"{found.group(1).upper()} {found.group(2).upper()}"


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
