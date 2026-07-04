"""Kalshi trading fee formula.

Published schedule: taker fee = ceil_to_cent( rate x C x P x (1 - P) ),
with rate 0.07 for general markets, scaled by the series' fee_multiplier
(the 15-min crypto series report fee_type="quadratic", fee_multiplier=1).
Maker fills are fee-free on these series.

The P(1-P) term peaks at P=0.50 — exactly where 15-minute contracts live for
most of their life — so a flat fee estimate would misprice edge where it
matters most. This is the exact formula, unit-tested against hand-computed
values, and it is the ONLY fee implementation in the codebase: the fill
simulator, feature engine, and scorecards all import from here.
"""

from __future__ import annotations

from decimal import ROUND_CEILING, Decimal

GENERAL_RATE = Decimal("0.07")
CENT = Decimal("0.01")


def taker_fee(
    price: Decimal | str | float,
    contracts: Decimal | int | float = 1,
    *,
    rate: Decimal = GENERAL_RATE,
    fee_multiplier: Decimal | float = 1,
) -> Decimal:
    """Fee in dollars for a taker fill of `contracts` at YES price `price`.

    Rounded UP to the next cent on the total (per Kalshi's schedule), so
    per-contract fee estimates must divide the total, not round separately.
    """
    p = Decimal(str(price))
    if not Decimal(0) <= p <= Decimal(1):
        raise ValueError(f"price must be within [0, 1] dollars, got {p}")
    c = Decimal(str(contracts))
    raw = rate * Decimal(str(fee_multiplier)) * c * p * (Decimal(1) - p)
    return raw.quantize(CENT, rounding=ROUND_CEILING)


def maker_fee(
    price: Decimal | str | float,
    contracts: Decimal | int | float = 1,
) -> Decimal:
    """Maker fills are free on the 15-min crypto series."""
    return Decimal("0.00")


def fee_per_contract(price: Decimal | str | float, contracts: int = 1) -> Decimal:
    """Average taker fee per contract at this price (for edge math)."""
    c = max(1, int(contracts))
    return taker_fee(price, c) / c
