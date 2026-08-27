"""Converting money, and refusing to.

The tests worth having here are the refusals. A conversion that works is easy to
believe; the behaviour that matters is what happens when no rate connects two
currencies, because the bug this module exists to fix was a comparison that
carried on regardless.
"""

from __future__ import annotations

import pytest

from app.core.config import FNOLSettings
from app.domain import money

CONFIG = FNOLSettings(base_currency="GBP", fx_rates={"USD": 0.80, "JPY": 0.005})


class TestCurrencyCodes:
    @pytest.mark.parametrize(
        ("value", "expected"),
        [
            ("usd", "USD"),
            (" GBP ", "GBP"),
            ("", None),
            (None, None),
            ("POUNDS", None),
            ("US$", None),
            ("12", None),
        ],
    )
    def test_only_a_three_letter_code_is_a_currency(
        self, value: str | None, expected: str | None
    ) -> None:
        assert money.normalise_currency(value) == expected


class TestMinorUnits:
    def test_most_currencies_are_hundredths(self) -> None:
        assert money.minor_unit_exponent("GBP") == 2
        assert money.minor_unit_exponent("USD") == 2

    def test_yen_has_no_minor_unit(self) -> None:
        # 1,000,000 minor units of JPY is a million yen, not ten thousand.
        assert money.minor_unit_exponent("JPY") == 0
        assert money.format_amount(1_000_000, "JPY") == "1,000,000 JPY"
        assert money.format_amount(1_000_000, "GBP") == "10,000 GBP"

    def test_an_unknown_currency_still_formats_as_a_figure(self) -> None:
        assert money.format_amount(1_150_000_00, None) == "1,150,000"


class TestConversion:
    def test_the_base_currency_needs_no_rate(self) -> None:
        result = money.to_base(1_000_00, "GBP", config=CONFIG)
        assert result is not None
        assert result.amount_minor == 1_000_00
        assert result.converted is False
        assert result.disclosure == ""

    def test_a_dollar_estimate_becomes_a_pound_one(self) -> None:
        result = money.to_base(1_150_000_00, "USD", config=CONFIG)
        assert result is not None
        assert result.amount_minor == 920_000_00
        assert result.converted is True
        assert "920,000 GBP" in result.disclosure
        assert result.describe().startswith("1,150,000 USD (")

    def test_conversion_respects_the_minor_unit_of_both_sides(self) -> None:
        # 10,000,000 yen at 0.005 is £50,000 — 5,000,000 pence, not 50,000.
        result = money.to_base(10_000_000, "JPY", config=CONFIG)
        assert result is not None
        assert result.amount_minor == 50_000_00

    def test_a_cross_rate_goes_through_the_base(self) -> None:
        result = money.to_currency(1_000_00, "USD", "JPY", config=CONFIG)
        assert result is not None
        # $1,000 is £800, and £800 buys 160,000 yen.
        assert result.amount_minor == 160_000

    def test_an_unrated_currency_returns_nothing_rather_than_a_guess(self) -> None:
        assert money.to_base(1_000_00, "BRL", config=CONFIG) is None
        assert money.to_currency(1_000_00, "USD", "BRL", config=CONFIG) is None
        assert money.to_currency(1_000_00, "BRL", "USD", config=CONFIG) is None

    def test_a_nonsense_currency_returns_nothing(self) -> None:
        assert money.to_base(1_000_00, "pounds sterling", config=CONFIG) is None
        assert money.to_base(1_000_00, None, config=CONFIG) is None

    def test_a_zero_or_negative_rate_is_no_rate(self) -> None:
        broken = FNOLSettings(base_currency="GBP", fx_rates={"USD": 0.0, "EUR": -1.0})
        assert money.to_base(1_000_00, "USD", config=broken) is None
        assert money.to_base(1_000_00, "EUR", config=broken) is None

    def test_rate_keys_are_read_case_insensitively(self) -> None:
        config = FNOLSettings(base_currency="GBP", fx_rates={"usd": 0.80})
        result = money.to_base(1_000_00, "USD", config=config)
        assert result is not None
        assert result.amount_minor == 800_00

    def test_no_amount_converts_to_no_conversion(self) -> None:
        assert money.to_base(None, "USD", config=CONFIG) is None

    def test_the_disclosure_names_the_rate_and_when_it_was_taken(self) -> None:
        config = FNOLSettings(
            base_currency="GBP", fx_rates={"USD": 0.80}, fx_rates_as_of="2026-08-01"
        )
        result = money.to_base(1_000_00, "USD", config=config)
        assert result is not None
        assert result.disclosure == "800 GBP at 0.8 GBP/USD, rate of 2026-08-01"
