"""Feature engine tests: regimes, tick window math, model probability."""

import math

import pytest

from kalshibot.features.engine import (
    Regime,
    compute_snapshot,
    normal_cdf,
    regime_for,
)
from kalshibot.features.window import TickWindow
from kalshibot.kalshi.models import Market, Orderbook

T0 = 1_800_000_000.0  # fixed epoch base so tests are deterministic


# ------------------------------------------------------------------- regimes

@pytest.mark.parametrize(
    ("seconds_remaining", "expected"),
    [
        (900, Regime.EARLY),   # window open
        (601, Regime.EARLY),   # 4:59 elapsed
        (600, Regime.MID),     # 5:00 elapsed
        (301, Regime.MID),     # 9:59 elapsed
        (300, Regime.LATE),    # 10:00 elapsed
        (91, Regime.LATE),
        (90, Regime.SETTLEMENT),  # final 90s: trading blackout
        (0, Regime.SETTLEMENT),
    ],
)
def test_regime_boundaries(seconds_remaining, expected):
    assert regime_for(seconds_remaining) == expected


# --------------------------------------------------------------- tick window

def make_window(prices: list[float], spacing: float = 1.0) -> TickWindow:
    w = TickWindow()
    for i, p in enumerate(prices):
        w.add(T0 + i * spacing, p)
    return w


def test_return_over():
    w = make_window([100.0] * 31 + [101.0])  # jump at the last tick
    now = T0 + 31
    assert w.return_over(30, now) == pytest.approx(0.01)


def test_out_of_order_ticks_dropped():
    w = TickWindow()
    w.add(T0 + 10, 100.0)
    w.add(T0 + 5, 999.0)
    assert w.last_price == 100.0 and len(w) == 1


def test_window_eviction():
    w = TickWindow(horizon_seconds=60)
    for i in range(120):
        w.add(T0 + i, 100.0)
    assert w.age_seconds(T0 + 119) == 0
    assert len(w) == 61  # only the last minute retained


def test_realized_vol_constant_price_is_zero():
    w = make_window([100.0] * 600)
    vol = w.realized_vol_per_sqrt_second(300, T0 + 599)
    assert vol == pytest.approx(0.0)


def test_realized_vol_scales_with_noise():
    small = make_window([100.0 + 0.01 * (-1) ** i for i in range(600)])
    large = make_window([100.0 + 0.10 * (-1) ** i for i in range(600)])
    now = T0 + 599
    assert large.realized_vol_per_sqrt_second(300, now) > (
        small.realized_vol_per_sqrt_second(300, now)
    )


# ------------------------------------------------------------ model prob

def snapshot_for(spot: float, strike: float, seconds_remaining: float = 450):
    market = Market.model_validate({
        "ticker": "KXTEST15M-X-00",
        "floor_strike": strike,
        "open_time": "2027-01-15T10:00:00Z",
        "close_time": "2027-01-15T10:15:00Z",
    })
    close_ts = market.close_time.timestamp()
    now = close_ts - seconds_remaining
    # noisy window around `spot` so realized vol is well-defined and positive
    w = TickWindow()
    for i in range(600):
        w.add(now - 600 + i, spot * (1 + 0.0001 * (-1) ** i))
    w.add(now, spot)
    book = Orderbook.from_api(
        {"orderbook_fp": {"yes_dollars": [["0.48", "100"]], "no_dollars": [["0.50", "100"]]}}
    )
    return compute_snapshot(
        now=now, asset="TEST", market=market, book=book, book_ts=now - 1,
        spot_window=w, spot_source="coinbase", btc_window=None, btc_implied_prob=None,
    )


def test_model_prob_at_strike_is_half():
    snap = snapshot_for(spot=50000.0, strike=50000.0)
    assert snap.model_prob == pytest.approx(0.5, abs=0.02)


def test_model_prob_far_above_strike_near_one():
    snap = snapshot_for(spot=50500.0, strike=50000.0)
    assert snap.model_prob > 0.99


def test_model_prob_far_below_strike_near_zero():
    snap = snapshot_for(spot=49500.0, strike=50000.0)
    assert snap.model_prob < 0.01


def test_edges_net_of_fees_are_lower_than_gross():
    snap = snapshot_for(spot=50000.0, strike=50000.0)
    assert snap.edge_yes_net < snap.edge_yes_gross
    assert snap.edge_no_net < snap.edge_no_gross
    # book: yes_ask=0.50, yes_bid=0.48, mid 0.49
    assert snap.implied_prob == pytest.approx(0.49)
    assert snap.spread_cents == pytest.approx(2.0)


def test_normal_cdf_sanity():
    assert normal_cdf(0) == pytest.approx(0.5)
    assert normal_cdf(3) == pytest.approx(0.99865, abs=1e-4)
    assert normal_cdf(-3) == pytest.approx(0.00135, abs=1e-4)
