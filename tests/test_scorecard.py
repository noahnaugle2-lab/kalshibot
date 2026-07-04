"""Scorecard metric tests: profit factor, P/L ratio, drawdown, streaks, ranking."""

import pytest

from kalshibot.evaluation.scorecard import (
    ProfitStats,
    Scorecard,
    build_scorecards,
    render_ranking,
)
from kalshibot.persistence.db import Database


def stats_from(pnls: list[float], fees_each: float = 0.1) -> ProfitStats:
    s = ProfitStats()
    for pnl in pnls:
        s.add(pnl, pnl - fees_each, fees_each, contracts=10, capital=5.0)
    return s


def test_profit_factor_and_pl_ratio():
    s = stats_from([10, 10, -5, -5])
    assert s.profit_factor == pytest.approx(2.0)   # 20 / 10
    assert s.pl_ratio_pct == pytest.approx(200.0)  # avg win 10 / avg loss 5
    assert s.hit_rate == pytest.approx(0.5)


def test_profit_factor_no_losses_is_inf():
    s = stats_from([5, 5])
    assert s.profit_factor == float("inf")
    assert s.pl_ratio_pct is None  # undefined without losses


def test_max_drawdown_and_streaks():
    # net pnls: +9.9, -5.1, -5.1, -5.1, +9.9 -> trough at -15.3+9.9... compute:
    s = stats_from([10, -5, -5, -5, 10])
    # equity (net): 9.9, 4.8, -0.3, -5.4, 4.5 ; peak 9.9 -> trough -5.4 => dd -15.3
    assert s.max_drawdown == pytest.approx(-15.3)
    assert s.longest_losing_streak == 3


def test_return_on_capital_and_per_contract():
    s = stats_from([10.1])  # net 10.0, contracts 10, capital 5
    assert s.pnl_per_contract == pytest.approx(1.0)
    assert s.return_on_capital == pytest.approx(2.0)


def test_pnl_volatility_needs_two():
    assert stats_from([5]).pnl_volatility is None
    assert stats_from([5, 5]).pnl_volatility == pytest.approx(0.0)


# ------------------------------------------------------------ integration

@pytest.fixture()
def score_db(tmp_path) -> Database:
    db = Database(tmp_path / "score.db")
    db.write_now("sim_runs", {
        "run_id": "shadow-BTC-x", "created_ts": 1.0, "kind": "shadow",
        "asset": "BTC", "strategy": "latency_momentum:v1:", "params": "{}",
        "latency_ms": None, "seed": None, "tape_start": None, "tape_end": None,
        "windows": None, "summary": None,
    })
    for i, (gross, regime) in enumerate([(5.0, "MID"), (-3.0, "MID"), (4.0, "LATE")]):
        db.write_now("sim_positions", {
            "run_id": "shadow-BTC-x", "asset": "BTC", "market_ticker": f"M{i}",
            "side": "yes", "contracts": 10, "avg_price": 0.5, "fees": 0.1,
            "entry_ts": 1_800_000_000 + i * 900, "entry_regime": regime,
            "result": "yes" if gross > 0 else "no",
            "payout_per_contract": 0.99,
            "pnl_gross": gross, "pnl_net": gross - 0.1,
        })
    for status in ("filled", "filled", "missed"):
        db.write_now("sim_orders", {
            "run_id": "shadow-BTC-x", "ts": 1_800_000_000, "asset": "BTC",
            "market_ticker": "M0", "intent": "BUY_YES", "execution": "taker",
            "limit_price": 0.5, "contracts": 10, "status": status,
            "filled_contracts": 10 if status == "filled" else 0, "reason": "t",
        })
    return db


def test_build_scorecards_aggregates(score_db):
    [card] = build_scorecards(score_db, kind="shadow")
    assert card.asset == "BTC"
    assert card.overall.trades == 3
    assert card.overall.profit_factor == pytest.approx(3.0)  # 9 / 3
    assert card.by_regime["MID"].trades == 2
    assert card.by_regime["LATE"].trades == 1
    assert card.fill_rate_taker == pytest.approx(2 / 3)
    assert card.fill_rate_maker is None
    assert card.confidence == "insufficient"
    assert card.recommendation == "collect"  # honest below 30 trades
    assert "BTC" in render_ranking([card])


def test_replay_runs_excluded_from_shadow_cards(score_db):
    score_db.write_now("sim_runs", {
        "run_id": "replay-1", "created_ts": 1.0, "kind": "replay",
        "asset": "ETH", "strategy": "s", "params": "{}", "latency_ms": 300,
        "seed": 0, "tape_start": None, "tape_end": None, "windows": 1,
        "summary": None,
    })
    score_db.write_now("sim_positions", {
        "run_id": "replay-1", "asset": "ETH", "market_ticker": "R0",
        "side": "yes", "contracts": 10, "avg_price": 0.5, "fees": 0.1,
        "entry_ts": 1, "entry_regime": "MID", "result": "yes",
        "payout_per_contract": 0.99, "pnl_gross": 5, "pnl_net": 4.9,
    })
    cards = build_scorecards(score_db, kind="shadow")
    assert [c.asset for c in cards] == ["BTC"]
    replay_cards = build_scorecards(score_db, kind="replay")
    assert [c.asset for c in replay_cards] == ["ETH"]
