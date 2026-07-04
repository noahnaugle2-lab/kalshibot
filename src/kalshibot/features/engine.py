"""Feature engine: one FeatureSnapshot per asset per scan cycle.

Everything a strategy (or Claude) consumes lives in this snapshot, and every
snapshot is persisted to the `signals` table — so the replay engine and the
scorecard recompute from exactly what the live loop saw, never a
reconstruction.
"""

from __future__ import annotations

import math
from enum import Enum
from typing import Any

from pydantic import BaseModel

from kalshibot.features.fees import fee_per_contract
from kalshibot.features.window import TickWindow
from kalshibot.kalshi.models import Market, Orderbook

FEATURE_SCHEMA_VERSION = 2  # v2: added smart_lean / smart_strength

WINDOW_SECONDS = 15 * 60
SETTLEMENT_BLACKOUT_SECONDS = 90


class Regime(str, Enum):
    EARLY = "EARLY"            # first 5 minutes: exploratory book
    MID = "MID"                # minutes 5-10: tightest and deepest
    LATE = "LATE"              # minutes 10-13.5
    SETTLEMENT = "SETTLEMENT"  # final 90s: convergence auction, no entries


def regime_for(seconds_remaining: float) -> Regime:
    if seconds_remaining <= SETTLEMENT_BLACKOUT_SECONDS:
        return Regime.SETTLEMENT
    elapsed = WINDOW_SECONDS - seconds_remaining
    if elapsed < 300:
        return Regime.EARLY
    if elapsed < 600:
        return Regime.MID
    return Regime.LATE


def normal_cdf(x: float) -> float:
    return 0.5 * (1.0 + math.erf(x / math.sqrt(2.0)))


class FeatureSnapshot(BaseModel):
    schema_version: int = FEATURE_SCHEMA_VERSION
    ts: float
    asset: str
    market_ticker: str

    # spot vs strike
    spot: float | None = None
    spot_source: str | None = None
    spot_age_seconds: float | None = None
    floor_strike: float | None = None
    distance_dollars: float | None = None
    distance_z: float | None = None  # ln(S/K) / (sigma * sqrt(tau))

    # window timing
    seconds_remaining: float = 0.0
    regime: Regime = Regime.EARLY

    # momentum / vol
    ret_30s: float | None = None
    ret_1m: float | None = None
    ret_5m: float | None = None
    realized_vol_5m: float | None = None  # per sqrt(second), log returns

    # cross-asset (BTC leads the alts)
    btc_ret_30s: float | None = None
    btc_ret_1m: float | None = None
    btc_implied_prob: float | None = None

    # Kalshi book
    yes_bid: float | None = None
    yes_ask: float | None = None
    implied_prob: float | None = None  # book mid
    spread_cents: float | None = None
    depth_yes_within_2c: float | None = None
    depth_no_within_2c: float | None = None
    flow_imbalance: float | None = None  # (yes-no)/(yes+no) depth near touch
    book_age_seconds: float | None = None  # repricing-lag proxy

    # smart money (set by the trader's enrichment hook; None in pure observation)
    smart_lean: str | None = None
    smart_strength: float | None = None

    # model vs market
    model_prob: float | None = None  # P(settle YES) from spot dist + vol + time
    edge_yes_gross: float | None = None
    edge_yes_net: float | None = None  # net of half-spread crossing + taker fee
    edge_no_gross: float | None = None
    edge_no_net: float | None = None

    def to_row(self) -> dict[str, Any]:
        return {
            "ts": self.ts,
            "asset": self.asset,
            "market_ticker": self.market_ticker,
            "schema_version": self.schema_version,
            "features": self.model_dump_json(),
        }


def depth_within(book_side: list, best: float | None, cents: float = 0.02) -> float:
    if best is None:
        return 0.0
    return float(
        sum(float(lv.quantity) for lv in book_side if float(lv.price) >= best - cents)
    )


def compute_snapshot(
    *,
    now: float,
    asset: str,
    market: Market,
    book: Orderbook | None,
    book_ts: float | None,
    spot_window: TickWindow,
    spot_source: str | None,
    btc_window: TickWindow | None,
    btc_implied_prob: float | None,
) -> FeatureSnapshot:
    snap = FeatureSnapshot(ts=now, asset=asset, market_ticker=market.ticker)

    # timing
    if market.close_time is not None:
        snap.seconds_remaining = max(0.0, market.close_time.timestamp() - now)
    snap.regime = regime_for(snap.seconds_remaining)

    # spot
    spot = spot_window.last_price
    snap.spot = spot
    snap.spot_source = spot_source
    snap.spot_age_seconds = spot_window.age_seconds(now)
    if market.floor_strike is not None:
        snap.floor_strike = float(market.floor_strike)

    # momentum / vol
    snap.ret_30s = spot_window.return_over(30, now)
    snap.ret_1m = spot_window.return_over(60, now)
    snap.ret_5m = spot_window.return_over(300, now)
    sigma = spot_window.realized_vol_per_sqrt_second(300, now)
    snap.realized_vol_5m = sigma

    # cross-asset
    if btc_window is not None:
        snap.btc_ret_30s = btc_window.return_over(30, now)
        snap.btc_ret_1m = btc_window.return_over(60, now)
    snap.btc_implied_prob = btc_implied_prob

    # book
    if book is not None:
        yes_bid = float(book.best_yes_bid) if book.best_yes_bid is not None else None
        yes_ask = float(book.best_yes_ask) if book.best_yes_ask is not None else None
        snap.yes_bid, snap.yes_ask = yes_bid, yes_ask
        if yes_bid is not None and yes_ask is not None:
            snap.implied_prob = (yes_bid + yes_ask) / 2
            snap.spread_cents = round((yes_ask - yes_bid) * 100, 2)
        best_no_bid = float(book.no_bids[0].price) if book.no_bids else None
        snap.depth_yes_within_2c = depth_within(book.yes_bids, yes_bid)
        snap.depth_no_within_2c = depth_within(book.no_bids, best_no_bid)
        total = snap.depth_yes_within_2c + snap.depth_no_within_2c
        if total > 0:
            snap.flow_imbalance = (
                snap.depth_yes_within_2c - snap.depth_no_within_2c
            ) / total
    if book_ts is not None:
        snap.book_age_seconds = max(0.0, now - book_ts)

    # model probability: driftless Brownian bridge on log price to expiry
    if (
        spot is not None
        and spot > 0
        and snap.floor_strike is not None
        and snap.floor_strike > 0
        and sigma is not None
        and sigma > 0
        and snap.seconds_remaining > 0
    ):
        tau = snap.seconds_remaining
        d = math.log(spot / snap.floor_strike) / (sigma * math.sqrt(tau))
        snap.model_prob = normal_cdf(d)
        snap.distance_dollars = spot - snap.floor_strike
        snap.distance_z = d

    # edge, gross and net of taker fee at the crossing price
    if snap.model_prob is not None and snap.yes_ask is not None and 0 < snap.yes_ask < 1:
        snap.edge_yes_gross = snap.model_prob - snap.yes_ask
        snap.edge_yes_net = snap.edge_yes_gross - float(fee_per_contract(snap.yes_ask))
    if snap.model_prob is not None and snap.yes_bid is not None and 0 < snap.yes_bid < 1:
        no_ask = 1 - snap.yes_bid  # cost of buying NO
        snap.edge_no_gross = (1 - snap.model_prob) - no_ask
        snap.edge_no_net = snap.edge_no_gross - float(fee_per_contract(no_ask))

    return snap
