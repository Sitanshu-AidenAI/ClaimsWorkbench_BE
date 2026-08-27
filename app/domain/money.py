"""Money that knows which currency it is in, and comparisons that refuse to guess.

The thresholds in `FNOLSettings` are amounts in the minor unit of *one* currency —
`base_currency` — and the amounts on a notification are in whatever currency the
notification stated. Comparing the two as bare integers reads a $30,500,000 fire
as though it were £30,500,000: the same digits, a different amount, and a
severity band decided on that coincidence. A book priced in dollars assessed
against pounds bands every claim wrong, and does it silently.

So nothing in this package compares two amounts until they are in the same
currency, and a comparison that *cannot* be made says so rather than falling back
to the raw integers. "Cannot" is a real outcome here: a carrier's rates come from
its treasury feed, and a notice in a currency the feed does not carry is a notice
for a person to price, not one for a threshold to guess at.

Minor units are not universally hundredths, either — a JPY amount is whole yen —
so the exponent is part of every conversion and every formatted figure.
"""

from __future__ import annotations

from dataclasses import dataclass

from app.core.config import FNOLSettings

#: Currencies whose minor unit is not a hundredth. ISO 4217 exponents, for the
#: codes a claims book plausibly carries; everything else is the 2 default.
MINOR_UNIT_EXPONENTS: dict[str, int] = {
    "BHD": 3,
    "BIF": 0,
    "CLP": 0,
    "DJF": 0,
    "GNF": 0,
    "ISK": 0,
    "JOD": 3,
    "JPY": 0,
    "KMF": 0,
    "KRW": 0,
    "KWD": 3,
    "LYD": 3,
    "OMR": 3,
    "PYG": 0,
    "RWF": 0,
    "TND": 3,
    "UGX": 0,
    "UYI": 0,
    "VND": 0,
    "VUV": 0,
    "XAF": 0,
    "XOF": 0,
    "XPF": 0,
}

_DEFAULT_EXPONENT = 2


def normalise_currency(currency: str | None) -> str | None:
    """A three-letter code, upper-cased, or `None` if it is not one.

    Deliberately strict. A currency this function cannot recognise is one no
    conversion should be attempted for, and returning `None` is what makes the
    caller handle that rather than compare pounds with an empty string.
    """
    if not currency:
        return None
    code = str(currency).strip().upper()
    if len(code) != 3 or not code.isalpha():
        return None
    return code


def minor_unit_exponent(currency: str | None) -> int:
    code = normalise_currency(currency)
    if code is None:
        return _DEFAULT_EXPONENT
    return MINOR_UNIT_EXPONENTS.get(code, _DEFAULT_EXPONENT)


def to_major(amount_minor: int, currency: str | None) -> float:
    return amount_minor / (10 ** minor_unit_exponent(currency))


def format_amount(amount_minor: int | None, currency: str | None) -> str:
    """An amount as a person reads it, in its own currency and its own exponent."""
    if amount_minor is None:
        return "an unstated amount"
    code = normalise_currency(currency)
    figure = f"{to_major(amount_minor, code):,.0f}"
    return f"{figure} {code}" if code else figure


@dataclass(frozen=True, slots=True)
class Conversion:
    """One amount restated in another currency, with the rate that did it.

    The rate travels with the result because a converted comparison has to be
    readable back: an officer told a claim is critical is entitled to see that the
    band was decided on a figure the pipeline produced, and at what rate. Every
    caller here puts `disclosure` into the sentence it shows.
    """

    amount_minor: int
    currency: str
    source_amount_minor: int
    source_currency: str
    rate: float
    rate_as_of: str | None = None

    @property
    def converted(self) -> bool:
        return self.currency != self.source_currency

    @property
    def disclosure(self) -> str:
        """How this figure was arrived at, as a phrase to drop into a sentence.

        Empty when nothing was converted, so a same-currency comparison does not
        explain itself at the officer.
        """
        if not self.converted:
            return ""
        as_of = f", rate of {self.rate_as_of}" if self.rate_as_of else ""
        return (
            f"{format_amount(self.amount_minor, self.currency)} "
            f"at {self.rate:g} {self.currency}/{self.source_currency}{as_of}"
        )

    def describe(self) -> str:
        """The amount as stated, plus what it converted to when it converted."""
        stated = format_amount(self.source_amount_minor, self.source_currency)
        return f"{stated} ({self.disclosure})" if self.converted else stated


def rate_into(
    currency: str | None,
    *,
    base_currency: str,
    rates: dict[str, float],
) -> float | None:
    """How many units of the base one unit of `currency` is worth.

    `None` means "no rate is configured", which is the answer that stops a
    comparison rather than the one that fudges it. The base currency is 1.0 by
    definition and needs no row in the table.
    """
    code = normalise_currency(currency)
    base = normalise_currency(base_currency)
    if code is None or base is None:
        return None
    if code == base:
        return 1.0
    lookup = {
        normalised: value
        for normalised, value in (
            (normalise_currency(key), value) for key, value in (rates or {}).items()
        )
        if normalised is not None
    }
    rate = lookup.get(code)
    if rate is None or rate <= 0:
        return None
    return float(rate)


def convert(
    amount_minor: int | None,
    from_currency: str | None,
    to_currency: str | None,
    *,
    base_currency: str,
    rates: dict[str, float],
    rate_as_of: str | None = None,
) -> Conversion | None:
    """`amount_minor` restated in `to_currency`, or `None` if it cannot be.

    Cross-rates go through the base currency, because that is the only leg the
    configured table holds. `None` comes back whenever either side has no rate —
    the caller's job is then to say so, never to compare the two integers anyway.
    """
    if amount_minor is None:
        return None

    source = normalise_currency(from_currency)
    target = normalise_currency(to_currency)
    if source is None or target is None:
        return None

    if source == target:
        return Conversion(
            amount_minor=amount_minor,
            currency=target,
            source_amount_minor=amount_minor,
            source_currency=source,
            rate=1.0,
            rate_as_of=rate_as_of,
        )

    from_rate = rate_into(source, base_currency=base_currency, rates=rates)
    to_rate = rate_into(target, base_currency=base_currency, rates=rates)
    if from_rate is None or to_rate is None:
        return None

    rate = from_rate / to_rate
    converted_major = to_major(amount_minor, source) * rate
    return Conversion(
        amount_minor=round(converted_major * (10 ** minor_unit_exponent(target))),
        currency=target,
        source_amount_minor=amount_minor,
        source_currency=source,
        rate=rate,
        rate_as_of=rate_as_of,
    )


def to_base(
    amount_minor: int | None, currency: str | None, *, config: FNOLSettings
) -> Conversion | None:
    """`amount_minor` in the currency the configured thresholds are denominated in.

    The one call every threshold comparison in this package goes through. `None`
    means the comparison is not available, and the caller has to say so.
    """
    return convert(
        amount_minor,
        currency,
        config.base_currency,
        base_currency=config.base_currency,
        rates=config.fx_rates,
        rate_as_of=config.fx_rates_as_of,
    )


def to_currency(
    amount_minor: int | None,
    currency: str | None,
    target: str | None,
    *,
    config: FNOLSettings,
) -> Conversion | None:
    """`amount_minor` in `target`, for comparing a claim figure with a policy's."""
    return convert(
        amount_minor,
        currency,
        target,
        base_currency=config.base_currency,
        rates=config.fx_rates,
        rate_as_of=config.fx_rates_as_of,
    )


__all__ = [
    "MINOR_UNIT_EXPONENTS",
    "Conversion",
    "convert",
    "format_amount",
    "minor_unit_exponent",
    "normalise_currency",
    "rate_into",
    "to_base",
    "to_currency",
    "to_major",
]
