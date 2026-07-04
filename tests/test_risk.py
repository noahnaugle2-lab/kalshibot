"""Risk layer enforcement tests — spec-required, every limit pinned."""

import pytest

from kalshibot.features.engine import FeatureSnapshot, Regime
from kalshibot.kalshi.models import OrderIntent
from kalshibot.orders.risk import BlackoutEvent, RiskConfig, RiskManager
from kalshibot.strategies.base import PositionState, StrategySignal

NOW = 1_800_000_000.0


def snap(regime=Regime.MID, seconds_remaining=450.0) -> FeatureSnapshot:
    return FeatureSnapshot(
        ts=NOW, asset="BTC", market_ticker="M",
        regime=regime, seconds_remaining=seconds_remaining,
    )


def sig(contracts=10, price=0.50, intent=OrderIntent.BUY_YES) -> StrategySignal:
    return StrategySignal(intent=intent, contracts=contracts, limit_price=price)


def manager(**overrides) -> RiskManager:
    return RiskManager(config=RiskConfig(**overrides))


def flat() -> PositionState:
    return PositionState(market_ticker="M")


def test_approves_normal_entry():
    v = manager().check("BTC", sig(), snap(), flat(), now=NOW)
    assert v.approved and v.contracts == 10


def test_settlement_blackout_by_regime_and_by_clock():
    m = manager()
    assert not m.check("BTC", sig(), snap(regime=Regime.SETTLEMENT), flat(), now=NOW).approved
    assert not m.check("BTC", sig(), snap(seconds_remaining=89), flat(), now=NOW).approved
    assert m.check("BTC", sig(), snap(seconds_remaining=91), flat(), now=NOW).approved


def test_kill_switch_and_pause():
    m = manager()
    m.kill_switch = True
    assert not m.check("BTC", sig(), snap(), flat(), now=NOW).approved
    m.kill_switch = False
    m.paused_assets.add("BTC")
    assert not m.check("BTC", sig(), snap(), flat(), now=NOW).approved
    assert m.check("ETH", sig(), snap(), flat(), now=NOW).approved


def test_daily_loss_limits_halt():
    m = manager(max_daily_loss_per_asset_usd=20, max_daily_loss_global_usd=100)
    m.record_pnl("BTC", -25, now=NOW)
    assert not m.check("BTC", sig(), snap(), flat(), now=NOW).approved
    assert m.check("ETH", sig(), snap(), flat(), now=NOW).approved  # only BTC halted
    for a in ("ETH", "SOL", "XRP", "DOGE"):
        m.record_pnl(a, -20, now=NOW)
    v = m.check("ZEC", sig(), snap(), flat(), now=NOW)  # -105 total: global halt
    assert not v.approved and "global" in v.reason


def test_daily_loss_resets_next_day():
    m = manager(max_daily_loss_per_asset_usd=20)
    m.record_pnl("BTC", -25, now=NOW)
    next_day = NOW + 86400
    assert m.check("BTC", sig(), snap(), flat(), now=next_day).approved


def test_notional_caps_downsize():
    # bankroll 1000, 9 assets -> allocation 111.11; 5% per trade -> $50
    m = manager(bankroll_usd=1000, n_assets=9, max_bankroll_fraction_per_trade=0.05)
    v = m.check("BTC", sig(contracts=500, price=0.50), snap(), flat(), now=NOW)
    assert v.approved and v.contracts == 100  # $50 / 0.50


def test_depth_cap_fraction():
    m = manager(depth_cap_fraction=0.25)
    v = m.check("ZEC", sig(contracts=100, price=0.50), snap(), flat(),
                resting_depth_at_price=40, now=NOW)
    assert v.approved and v.contracts == 10  # 40 * 0.25


def test_size_rounding_to_zero_vetoes():
    m = manager(depth_cap_fraction=0.25)
    v = m.check("ZEC", sig(), snap(), flat(), resting_depth_at_price=2, now=NOW)
    assert not v.approved


def test_no_pyramiding():
    m = manager()
    held = PositionState(market_ticker="M", side="yes", contracts=10, avg_price=0.5)
    assert not m.check("BTC", sig(), snap(), held, now=NOW).approved


def test_event_blackout_calendar():
    m = manager()
    m.blackouts = [BlackoutEvent(label="CPI", start_ts=NOW - 60, end_ts=NOW + 60, assets=[])]
    v = m.check("BTC", sig(), snap(), flat(), now=NOW)
    assert not v.approved and "CPI" in v.reason
    m.blackouts = [BlackoutEvent(label="unlock", start_ts=NOW - 60, end_ts=NOW + 60,
                                 assets=["NEAR"])]
    assert m.check("BTC", sig(), snap(), flat(), now=NOW).approved
    assert not m.check("NEAR", sig(), snap(), flat(), now=NOW).approved


def test_sell_intents_rejected_v1():
    v = manager().check("BTC", sig(intent=OrderIntent.SELL_YES), snap(), flat(), now=NOW)
    assert not v.approved
