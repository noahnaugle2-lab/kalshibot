"""Strategy library + smart-money modifier tests."""

import pytest

from kalshibot.features.engine import FeatureSnapshot, Regime
from kalshibot.kalshi.models import OrderIntent
from kalshibot.smartmoney.signal import SmartMoneySignal, apply_modifier, merge
from kalshibot.strategies.base import PositionState
from kalshibot.strategies.library import REGISTRY, build_strategy

NOW = 1_800_000_000.0


def snap(**kw) -> FeatureSnapshot:
    base = dict(
        ts=NOW, asset="SOL", market_ticker="M", regime=Regime.MID,
        seconds_remaining=450.0, yes_bid=0.48, yes_ask=0.50, implied_prob=0.49,
        spread_cents=2.0, realized_vol_5m=5e-5, distance_z=0.1,
    )
    base.update(kw)
    return FeatureSnapshot(**base)


def flat() -> PositionState:
    return PositionState(market_ticker="M")


def held() -> PositionState:
    return PositionState(market_ticker="M", side="yes", contracts=10, avg_price=0.5)


# ------------------------------------------------------------- strategies

def test_registry_builds_all():
    for name in REGISTRY:
        assert build_strategy(name, {}).name == name
    with pytest.raises(KeyError):
        build_strategy("nope")


def test_latency_momentum_fires_on_fast_move_with_edge():
    s = build_strategy("latency_momentum", {"move_vol_mult": 3.0, "edge_threshold": 0.02})
    # vol_30s = 5e-5 * sqrt(30) ~ 2.74e-4; ret_30s 0.001 -> z ~ 3.65
    signal = s.evaluate(snap(ret_30s=0.001, edge_yes_net=0.03), flat())
    assert signal is not None and signal.intent is OrderIntent.BUY_YES
    # same move without edge -> nothing
    assert s.evaluate(snap(ret_30s=0.001, edge_yes_net=0.0), flat()) is None
    # down move with NO edge -> BUY_NO at 1 - yes_bid
    signal = s.evaluate(snap(ret_30s=-0.001, edge_no_net=0.03), flat())
    assert signal.intent is OrderIntent.BUY_NO
    assert signal.limit_price == pytest.approx(0.52)


def test_latency_momentum_holds_when_positioned():
    s = build_strategy("latency_momentum", {})
    assert s.evaluate(snap(ret_30s=0.001, edge_yes_net=0.05), held()) is None


def test_cross_asset_lead_lag_uses_btc_return():
    s = build_strategy("cross_asset_lead_lag", {"btc_move_threshold": 0.0008})
    signal = s.evaluate(snap(btc_ret_30s=0.001, edge_yes_net=0.03), flat())
    assert signal is not None and signal.intent is OrderIntent.BUY_YES
    assert s.evaluate(snap(btc_ret_30s=0.0001, edge_yes_net=0.03), flat()) is None
    assert s.evaluate(snap(btc_ret_30s=None, edge_yes_net=0.03), flat()) is None


def test_mean_reversion_fades_extreme_near_strike():
    s = build_strategy("mean_reversion_extremes", {"extreme": 0.85})
    signal = s.evaluate(
        snap(regime=Regime.EARLY, implied_prob=0.90, distance_z=0.2,
             edge_no_net=0.04, yes_bid=0.89, yes_ask=0.91), flat(),
    )
    assert signal is not None and signal.intent is OrderIntent.BUY_NO
    # spot genuinely far above strike -> not an overshoot, no fade
    assert s.evaluate(
        snap(regime=Regime.EARLY, implied_prob=0.90, distance_z=2.0,
             edge_no_net=0.04, yes_bid=0.89, yes_ask=0.91), flat(),
    ) is None


def test_thin_book_maker_quotes_inside_wide_spread():
    s = build_strategy("thin_book_maker", {"min_spread_cents": 3.0, "model_margin": 0.03})
    signal = s.evaluate(
        snap(yes_bid=0.40, yes_ask=0.46, spread_cents=6.0,
             implied_prob=0.43, model_prob=0.50), flat(),
    )
    assert signal is not None
    assert signal.execution == "maker"
    assert signal.intent is OrderIntent.BUY_YES
    assert signal.limit_price == pytest.approx(0.41)  # one tick inside
    # tight spread -> no quote
    assert s.evaluate(
        snap(yes_bid=0.44, yes_ask=0.46, spread_cents=2.0,
             implied_prob=0.45, model_prob=0.50), flat(),
    ) is None


# ---------------------------------------------------------- smart money

def test_merge_neutral_when_no_layers():
    assert merge(None, None).lean == "NEUTRAL"
    assert merge(None, {"lean": "NEUTRAL", "strength": 0}).lean == "NEUTRAL"


def test_merge_layers_agree_and_conflict():
    both_up = merge(("UP", 0.6), {"lean": "UP", "strength": 0.4})
    assert both_up.lean == "UP" and both_up.strength == pytest.approx(0.5)
    conflict = merge(("UP", 0.5), {"lean": "DOWN", "strength": 0.5})
    assert conflict.lean == "NEUTRAL" or conflict.strength == pytest.approx(0.0)


def test_modifier_agree_upsizes_disagree_downsizes():
    sm = SmartMoneySignal(lean="UP", strength=0.5)
    up, veto, _ = apply_modifier(OrderIntent.BUY_YES, 10, sm, weight=0.4)
    assert not veto and up == pytest.approx(12.0)
    down, veto, _ = apply_modifier(OrderIntent.BUY_NO, 10, sm, weight=0.4)
    assert not veto and down == pytest.approx(8.0)


def test_modifier_vetoes_strong_disagreement():
    sm = SmartMoneySignal(lean="DOWN", strength=0.8)
    _, veto, note = apply_modifier(OrderIntent.BUY_YES, 10, sm, weight=0.8)
    assert veto and "veto" in note


def test_modifier_inert_at_zero_weight():
    sm = SmartMoneySignal(lean="DOWN", strength=1.0)
    contracts, veto, _ = apply_modifier(OrderIntent.BUY_YES, 10, sm, weight=0.0)
    assert not veto and contracts == 10
