"""Calibration scoring and flow-pattern detector tests (synthetic fixtures)."""

import json

import pytest

from kalshibot.evaluation.calibration import score_asset
from kalshibot.persistence.db import Database
from kalshibot.smartmoney.flow import (
    Print,
    WindowTape,
    mine_new_windows,
    pattern_depth_imbalance_mid,
    pattern_large_prints,
    pattern_taker_imbalance_mid,
)

T0 = 1_800_000_000.0


# -------------------------------------------------------------- calibration

@pytest.fixture()
def cal_db(tmp_path) -> Database:
    db = Database(tmp_path / "cal.db")
    db.write_now("settlements", {
        "market_ticker": "M-YES", "asset": "BTC", "result": "yes",
        "floor_strike": 1, "expiration_value": 2, "open_ts": T0,
        "close_ts": T0 + 900, "recorded_ts": T0 + 960, "consistent": 1,
    })

    def signal(ts, model_p, implied_p, regime="MID"):
        db.write_now("signals", {
            "ts": ts, "asset": "BTC", "market_ticker": "M-YES",
            "schema_version": 1,
            "features": json.dumps({
                "market_ticker": "M-YES", "model_prob": model_p,
                "implied_prob": implied_p, "regime": regime,
            }),
        })

    # outcome is YES; model says 0.8 (brier .04), market says 0.6 (brier .16)
    signal(T0 + 400, 0.8, 0.6)
    signal(T0 + 402, 0.8, 0.6)
    # settlement-regime snapshot must not touch headline stats
    signal(T0 + 880, 0.99, 0.99, regime="SETTLEMENT")
    return db


def test_calibration_brier_and_hit_rate(cal_db):
    r = score_asset(cal_db, "BTC")
    assert r is not None
    assert r.n_windows == 1
    assert r.overall.n == 2  # SETTLEMENT excluded from headline
    assert r.overall.brier_model == pytest.approx(0.04)
    assert r.overall.brier_market == pytest.approx(0.16)
    assert r.overall.hit_rate == 1.0
    # model disagreed with market by 0.2 >= threshold, and was right
    assert r.edge_calls == 2 and r.edge_hit_rate == 1.0
    assert "SETTLEMENT" in r.per_regime and r.per_regime["SETTLEMENT"].n == 1


def test_calibration_none_for_unknown_asset(cal_db):
    assert score_asset(cal_db, "ETH") is None


# ------------------------------------------------------------ flow patterns

def tape(prints, depth=(), result="yes") -> WindowTape:
    return WindowTape(
        market_ticker="M", asset="BTC", open_ts=T0, close_ts=T0 + 900,
        result=result, prints=prints, depth_imbalances=list(depth),
    )


def test_taker_imbalance_mid_up():
    prints = [Print(T0 + 400, 0.5, 30, "yes"), Print(T0 + 450, 0.5, 10, "no")]
    assert pattern_taker_imbalance_mid(tape(prints)) == "UP"


def test_taker_imbalance_ignores_other_phases():
    prints = [Print(T0 + 100, 0.5, 100, "yes")]  # EARLY, not MID
    assert pattern_taker_imbalance_mid(tape(prints)) is None


def test_taker_imbalance_balanced_is_neutral():
    prints = [Print(T0 + 400, 0.5, 20, "yes"), Print(T0 + 401, 0.5, 20, "no")]
    assert pattern_taker_imbalance_mid(tape(prints)) is None


def test_large_prints_detects_whale_direction():
    prints = [Print(T0 + 10 * i, 0.5, 1, "no") for i in range(30)]
    prints += [Print(T0 + 500, 0.5, 500, "yes"), Print(T0 + 520, 0.5, 400, "yes")]
    assert pattern_large_prints(tape(prints)) == "UP"


def test_large_prints_needs_minimum_sample():
    prints = [Print(T0 + 500, 0.5, 500, "yes")] * 5
    assert pattern_large_prints(tape(prints)) is None


def test_depth_imbalance_mid():
    depth = [(T0 + 300 + 2 * i, 0.4) for i in range(40)]
    assert pattern_depth_imbalance_mid(tape([], depth)) == "UP"
    depth = [(T0 + 300 + 2 * i, -0.4) for i in range(40)]
    assert pattern_depth_imbalance_mid(tape([], depth)) == "DOWN"


def test_mine_new_windows_persists_and_dedupes(tmp_path):
    db = Database(tmp_path / "flow.db")
    db.write_now("settlements", {
        "market_ticker": "M1", "asset": "BTC", "result": "yes",
        "floor_strike": 1, "expiration_value": 2, "open_ts": T0,
        "close_ts": T0 + 900, "recorded_ts": T0 + 960, "consistent": 1,
    })
    for i in range(10):
        db.write_now("trade_tape", {
            "trade_id": f"t{i}", "market_ticker": "M1", "asset": "BTC",
            "ts": T0 + 400 + i, "yes_price": 0.5, "count": 50.0,
            "taker_side": "yes", "raw": "{}",
        })
    n_first = mine_new_windows(db)
    assert n_first >= 1  # taker_imbalance_mid fired at minimum
    outcomes = db.query("SELECT * FROM flow_pattern_outcomes")
    assert all(o["correct"] == 1 for o in outcomes)  # lean UP, result yes
    aggregates = db.query("SELECT * FROM flow_patterns WHERE pattern='taker_imbalance_mid'")
    assert aggregates[0]["hit_rate"] == 1.0 and aggregates[0]["status"] == "candidate"
    # second run scores nothing new (window already mined)
    assert mine_new_windows(db) == 0
    db.close()
