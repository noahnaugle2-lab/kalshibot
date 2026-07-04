"""Kalshi fee formula tests against hand-computed values.

fee = ceil_to_cent(0.07 x C x P x (1-P)); the P(1-P) term peaks at P=0.50,
exactly where 15-minute contracts trade, so these values are load-bearing.
"""

from decimal import Decimal

import pytest

from kalshibot.features.fees import fee_per_contract, maker_fee, taker_fee


@pytest.mark.parametrize(
    ("price", "contracts", "expected"),
    [
        # 0.07 * 100 * 0.50 * 0.50 = 1.75 exactly
        ("0.50", 100, "1.75"),
        # 0.07 * 1 * 0.25 = 0.0175 -> ceil to 0.02
        ("0.50", 1, "0.02"),
        # 0.07 * 100 * 0.99 * 0.01 = 0.0693 -> ceil to 0.07
        ("0.99", 100, "0.07"),
        # 0.07 * 100 * 0.01 * 0.99 = symmetric
        ("0.01", 100, "0.07"),
        # 0.07 * 50 * 0.30 * 0.70 = 0.735 -> ceil to 0.74
        ("0.30", 50, "0.74"),
        # deci-cent price: 0.07 * 1000 * 0.015 * 0.985 = 1.03425 -> 1.04
        ("0.015", 1000, "1.04"),
        # boundary prices are fee-free
        ("0", 100, "0.00"),
        ("1", 100, "0.00"),
    ],
)
def test_taker_fee_known_values(price, contracts, expected):
    assert taker_fee(price, contracts) == Decimal(expected)


def test_fee_peaks_at_fifty_cents():
    # ceiling-to-cent creates ties near the peak (e.g. 47c also rounds to
    # 1.75), so assert 50c attains the maximum, not that it's unique
    fees = {p: taker_fee(Decimal(p) / 100, 100) for p in range(1, 100)}
    assert fees[50] == max(fees.values())
    assert fees[50] > fees[10] > fees[1]


def test_rounding_is_on_total_not_per_contract():
    # 100 contracts at 0.50: total 1.75; per-contract 0.0175 (not 0.02*100)
    assert fee_per_contract("0.50", 100) == Decimal("0.0175")
    assert taker_fee("0.50", 100) < 100 * taker_fee("0.50", 1)


def test_fee_multiplier_scales():
    assert taker_fee("0.50", 100, fee_multiplier=2) == Decimal("3.50")


def test_maker_fee_is_zero():
    assert maker_fee("0.50", 100) == Decimal("0.00")


def test_out_of_range_price_rejected():
    with pytest.raises(ValueError):
        taker_fee("1.01", 1)
