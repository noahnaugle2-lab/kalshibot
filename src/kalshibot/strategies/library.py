"""Baseline strategy archetypes, assignable per asset via config.

Each is deliberately simple and parameterized — the point of the evaluation
campaign is to measure which archetype fits which market, then retune via
replay. None of these is presumed good; NaiveEdgeTaker (base.py) is the
floor they must all beat.

Registry keys are what `strategy:` in config/assets.yaml refers to.
"""

from __future__ import annotations

from kalshibot.features.engine import FeatureSnapshot, Regime
from kalshibot.kalshi.models import OrderIntent
from kalshibot.strategies.base import (
    NaiveEdgeTaker,
    PositionState,
    Strategy,
    StrategySignal,
)


class LatencyMomentum(Strategy):
    """Trade a fast spot move before the Kalshi book reprices.

    Fires when the 30s spot return is a large multiple of realized vol AND
    the book still disagrees with the model (edge in the move's direction).
    Natural fit for BTC/ETH where the 3-7s repricing lag is documented.

    params: move_vol_mult (default 3.0), edge_threshold (0.02),
            contracts (10), max_book_age_s (6.0)
    """

    name = "latency_momentum"
    active_regimes = frozenset({Regime.EARLY, Regime.MID, Regime.LATE})

    def on_snapshot(
        self, snapshot: FeatureSnapshot, position: PositionState
    ) -> StrategySignal | None:
        if position.side is not None:
            return None
        p = self.params
        move_mult = float(p.get("move_vol_mult", 3.0))
        edge_min = float(p.get("edge_threshold", 0.02))
        contracts = float(p.get("contracts", 10))

        if snapshot.ret_30s is None or snapshot.realized_vol_5m is None:
            return None
        vol_30s = snapshot.realized_vol_5m * (30 ** 0.5)
        if vol_30s <= 0:
            return None
        move_z = snapshot.ret_30s / vol_30s

        if move_z >= move_mult and (snapshot.edge_yes_net or 0) >= edge_min:
            if snapshot.yes_ask is not None:
                return StrategySignal(
                    intent=OrderIntent.BUY_YES, contracts=contracts,
                    limit_price=snapshot.yes_ask,
                    reason=f"spot +{move_z:.1f}σ/30s, edge_yes {snapshot.edge_yes_net:.3f}",
                )
        if move_z <= -move_mult and (snapshot.edge_no_net or 0) >= edge_min:
            if snapshot.yes_bid is not None:
                return StrategySignal(
                    intent=OrderIntent.BUY_NO, contracts=contracts,
                    limit_price=round(1 - snapshot.yes_bid, 4),
                    reason=f"spot {move_z:.1f}σ/30s, edge_no {snapshot.edge_no_net:.3f}",
                )
        return None


class CrossAssetLeadLag(Strategy):
    """Use a BTC move as the entry trigger for an alt whose book lags.

    Fires when BTC's short-term return is decisive and the alt's own model
    edge points the same way (spot correlation means the alt's spot has
    typically started moving; its Kalshi book is the slow leg).

    params: btc_move_threshold (default 0.0008 = 8bp/30s), edge_threshold
            (0.02), contracts (10)
    """

    name = "cross_asset_lead_lag"
    active_regimes = frozenset({Regime.EARLY, Regime.MID, Regime.LATE})

    def on_snapshot(
        self, snapshot: FeatureSnapshot, position: PositionState
    ) -> StrategySignal | None:
        if position.side is not None:
            return None
        p = self.params
        btc_min = float(p.get("btc_move_threshold", 0.0008))
        edge_min = float(p.get("edge_threshold", 0.02))
        contracts = float(p.get("contracts", 10))

        btc_ret = snapshot.btc_ret_30s
        if btc_ret is None:
            return None
        if btc_ret >= btc_min and (snapshot.edge_yes_net or 0) >= edge_min:
            if snapshot.yes_ask is not None:
                return StrategySignal(
                    intent=OrderIntent.BUY_YES, contracts=contracts,
                    limit_price=snapshot.yes_ask,
                    reason=f"btc_30s {btc_ret:+.4f}, edge_yes {snapshot.edge_yes_net:.3f}",
                )
        if btc_ret <= -btc_min and (snapshot.edge_no_net or 0) >= edge_min:
            if snapshot.yes_bid is not None:
                return StrategySignal(
                    intent=OrderIntent.BUY_NO, contracts=contracts,
                    limit_price=round(1 - snapshot.yes_bid, 4),
                    reason=f"btc_30s {btc_ret:+.4f}, edge_no {snapshot.edge_no_net:.3f}",
                )
        return None


class MeanReversionExtremes(Strategy):
    """Fade contracts that overshoot toward the extremes early in the window
    while spot is still close to the strike.

    params: extreme (default 0.85), max_distance_z (0.6), edge_threshold
            (0.02), contracts (10)
    """

    name = "mean_reversion_extremes"
    active_regimes = frozenset({Regime.EARLY, Regime.MID})

    def on_snapshot(
        self, snapshot: FeatureSnapshot, position: PositionState
    ) -> StrategySignal | None:
        if position.side is not None:
            return None
        p = self.params
        extreme = float(p.get("extreme", 0.85))
        max_z = float(p.get("max_distance_z", 0.6))
        edge_min = float(p.get("edge_threshold", 0.02))
        contracts = float(p.get("contracts", 10))

        if snapshot.implied_prob is None or snapshot.distance_z is None:
            return None
        if abs(snapshot.distance_z) > max_z:
            return None  # spot genuinely far from strike; not an overshoot

        # market says near-certain UP while spot hugs the strike -> buy NO
        if snapshot.implied_prob >= extreme and (snapshot.edge_no_net or 0) >= edge_min:
            if snapshot.yes_bid is not None:
                return StrategySignal(
                    intent=OrderIntent.BUY_NO, contracts=contracts,
                    limit_price=round(1 - snapshot.yes_bid, 4),
                    reason=f"fade implied {snapshot.implied_prob:.2f} at z={snapshot.distance_z:.2f}",
                )
        if snapshot.implied_prob <= 1 - extreme and (snapshot.edge_yes_net or 0) >= edge_min:
            if snapshot.yes_ask is not None:
                return StrategySignal(
                    intent=OrderIntent.BUY_YES, contracts=contracts,
                    limit_price=snapshot.yes_ask,
                    reason=f"fade implied {snapshot.implied_prob:.2f} at z={snapshot.distance_z:.2f}",
                )
        return None


class ThinBookMaker(Strategy):
    """Post inside a wide spread on illiquid markets and capture it.

    Rests a maker order one tick inside the touch on the side the model
    favors, only when the spread is wide enough to pay for being wrong
    sometimes. Strict inventory: one position per window. Fills simulate
    from actual subsequent trade prints (conservative queue model).

    params: min_spread_cents (default 3.0), model_margin (0.03),
            contracts (10), tick (0.01)
    """

    name = "thin_book_maker"
    active_regimes = frozenset({Regime.MID, Regime.LATE})

    def on_snapshot(
        self, snapshot: FeatureSnapshot, position: PositionState
    ) -> StrategySignal | None:
        if position.side is not None:
            return None
        p = self.params
        min_spread = float(p.get("min_spread_cents", 3.0))
        margin = float(p.get("model_margin", 0.03))
        contracts = float(p.get("contracts", 10))
        tick = float(p.get("tick", 0.01))

        if (
            snapshot.spread_cents is None or snapshot.spread_cents < min_spread
            or snapshot.model_prob is None or snapshot.implied_prob is None
            or snapshot.yes_bid is None or snapshot.yes_ask is None
        ):
            return None

        if snapshot.model_prob >= snapshot.implied_prob + margin:
            price = round(snapshot.yes_bid + tick, 4)  # improve the YES bid
            if price < snapshot.yes_ask:
                return StrategySignal(
                    intent=OrderIntent.BUY_YES, contracts=contracts,
                    limit_price=price, execution="maker",
                    reason=f"make YES {price:.2f} inside {snapshot.spread_cents:.0f}c spread",
                )
        if snapshot.model_prob <= snapshot.implied_prob - margin:
            no_bid = round(1 - snapshot.yes_ask, 4)
            price = round(no_bid + tick, 4)  # improve the NO bid
            if price < 1 - snapshot.yes_bid:
                return StrategySignal(
                    intent=OrderIntent.BUY_NO, contracts=contracts,
                    limit_price=price, execution="maker",
                    reason=f"make NO {price:.2f} inside {snapshot.spread_cents:.0f}c spread",
                )
        return None


REGISTRY: dict[str, type[Strategy]] = {
    NaiveEdgeTaker.name: NaiveEdgeTaker,
    LatencyMomentum.name: LatencyMomentum,
    CrossAssetLeadLag.name: CrossAssetLeadLag,
    MeanReversionExtremes.name: MeanReversionExtremes,
    ThinBookMaker.name: ThinBookMaker,
}


def build_strategy(name: str, params: dict | None = None) -> Strategy:
    if name not in REGISTRY:
        raise KeyError(f"unknown strategy {name!r}; known: {sorted(REGISTRY)}")
    return REGISTRY[name](params or {})
