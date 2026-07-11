"""Replay engine tests on a synthetic fixture tape: correctness + determinism."""

import json

import pytest

from kalshibot.features.engine import FeatureSnapshot, Regime
from kalshibot.kalshi.models import OrderIntent
from kalshibot.persistence.db import Database
from kalshibot.sim.replay import ReplayEngine
from kalshibot.strategies.base import PositionState, Strategy, StrategySignal

T_OPEN = 1_800_000_000.0
T_CLOSE = T_OPEN + 900


class BuyFirstMidSnapshot(Strategy):
    """Test double: buy 10 YES at the ask on the first MID-regime snapshot."""

    name = "test_buy_first_mid"
    active_regimes = frozenset({Regime.MID})

    def on_snapshot(self, snapshot: FeatureSnapshot, position: PositionState):
        if position.side is not None or snapshot.yes_ask is None:
            return None
        return StrategySignal(
            intent=OrderIntent.BUY_YES, contracts=10,
            limit_price=snapshot.yes_ask, reason="test",
        )


@pytest.fixture()
def tape_db(tmp_path) -> Database:
    db = Database(tmp_path / "tape.db")
    db.write_now("markets", {
        "ticker": "KXTEST15M-A-00", "asset": "TEST", "series_ticker": "KXTEST15M",
        "open_ts": T_OPEN, "close_ts": T_CLOSE, "floor_strike": 100.0,
        "expiration_value": 100.5, "result": "yes", "status": "finalized",
        "raw": "{}", "updated_at": T_CLOSE,
    })
    db.write_now("settlements", {
        "market_ticker": "KXTEST15M-A-00", "asset": "TEST", "result": "yes",
        "floor_strike": 100.0, "expiration_value": 100.5,
        "open_ts": T_OPEN, "close_ts": T_CLOSE, "recorded_ts": T_CLOSE + 60,
        "consistent": 1,
    })
    # spot ticks every second from 10 min before open through close
    for i in range(-600, 901):
        db.write_now("spot_ticks", {
            "ts": T_OPEN + i, "asset": "TEST", "source": "coinbase",
            "price": 100.0 + 0.001 * (i % 3),
        })
    # book snapshots every 2s across the window: NO bids 0.60/0.55 -> YES asks
    # 0.40 (5) / 0.45 (10); YES bid 0.38
    for i in range(0, 900, 2):
        db.write_now("book_snapshots", {
            "ts": T_OPEN + i, "market_ticker": "KXTEST15M-A-00", "asset": "TEST",
            "yes_bids": json.dumps([["0.38", "50"]]),
            "no_bids": json.dumps([["0.60", "5"], ["0.55", "10"]]),
            "best_yes_bid": 0.38, "best_yes_ask": 0.40, "mid": 0.39, "spread": 0.02,
        })
    return db


def test_replay_executes_and_settles(tape_db):
    # apply_risk=False isolates the fill simulator (this test asserts IOC
    # partial-fill mechanics, not the risk layer's depth cap)
    engine = ReplayEngine(tape_db, latency_ms=300, payout_per_contract=0.99,
                          seed=7, apply_risk=False)
    result = engine.run("TEST", BuyFirstMidSnapshot())

    assert result.windows == 1 and result.trades == 1 and result.wins == 1
    fills = tape_db.query("SELECT * FROM sim_fills")
    assert len(fills) == 1
    fill = fills[0]
    # limit at displayed ask 0.40 -> only the 0.40x5 level crosses (IOC partial)
    assert fill["contracts"] == 5 and fill["price"] == pytest.approx(0.40)
    # entry in MID: elapsed in [300, 600)
    assert T_OPEN + 300 <= fill["ts"] < T_OPEN + 600
    # latency: executed on a book snapshot strictly after the signal snapshot
    pos = tape_db.query("SELECT * FROM sim_positions")[0]
    assert pos["result"] == "yes"
    # pnl: (0.99 - 0.40) * 5 = 2.95 gross; fee ceil(.07*5*.4*.6)=0.09
    assert pos["pnl_gross"] == pytest.approx(2.95)
    assert pos["pnl_net"] == pytest.approx(2.86)
    assert result.pnl_net == pytest.approx(2.86)


def test_replay_is_deterministic(tape_db):
    engine = ReplayEngine(tape_db, latency_ms=300, seed=42)
    a = engine.run("TEST", BuyFirstMidSnapshot(), persist=False)
    b = engine.run("TEST", BuyFirstMidSnapshot(), persist=False)
    assert a.model_dump(exclude={"run_id"}) == b.model_dump(exclude={"run_id"})


def test_replay_no_settlement_no_window(tape_db):
    # an asset with no settled windows replays to zero windows, not an error
    engine = ReplayEngine(tape_db)
    result = engine.run("MISSING", BuyFirstMidSnapshot(), persist=False)
    assert result.windows == 0 and result.trades == 0


def test_replay_applies_risk_gate_by_default(tape_db):
    # faithful-to-live: the RiskManager's depth cap (0.25 x opposing depth of 5)
    # downsizes the 10-contract signal to 1 — replay must gate like production,
    # not fill the raw strategy size (the bug this fixes).
    engine = ReplayEngine(tape_db, latency_ms=300, payout_per_contract=0.99, seed=7)
    result = engine.run("TEST", BuyFirstMidSnapshot(), persist=False)
    assert result.trades == 1
    fills = ReplayEngine(tape_db, latency_ms=300, payout_per_contract=0.99, seed=7)
    fills.run("TEST", BuyFirstMidSnapshot())
    row = tape_db.query("SELECT * FROM sim_fills ORDER BY id DESC LIMIT 1")[0]
    assert row["contracts"] == 1  # depth-capped, not the 5 an ungated fill would take
