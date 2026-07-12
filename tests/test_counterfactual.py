from kalshibot.features.engine import FeatureSnapshot, Regime
from kalshibot.kalshi.models import Orderbook, OrderIntent
from kalshibot.orders.risk import Verdict
from kalshibot.persistence.db import Database
from kalshibot.smartmoney.signal import SmartMoneySignal
from kalshibot.strategies.base import StrategySignal
from kalshibot.trader import OpenPosition, ShadowTrader


def make_trader(tmp_path):
    trader = ShadowTrader.__new__(ShadowTrader)
    trader.db = Database(tmp_path / "cf.db")
    trader.run_ids = {"XRP": "shadow-XRP-test"}
    trader.recorders = {"XRP": type("Recorder", (), {"latest_book": None})()}
    return trader


def test_counterfactual_proposal_and_settlement_lifecycle(tmp_path):
    trader = make_trader(tmp_path)
    snap = FeatureSnapshot(
        ts=1000.0, asset="XRP", market_ticker="XRP-M1",
        regime=Regime.MID, seconds_remaining=450,
    )
    baseline = StrategySignal(
        intent=OrderIntent.BUY_YES, contracts=10, limit_price=0.5,
    )
    trader._record_counterfactual(
        "ledger-1", "XRP", snap, "lead-lag:v1", baseline,
        SmartMoneySignal(lean="DOWN", strength=0.8), 0.25, 8.0,
        False, "smart_money disagrees -0.20",
        Verdict(True, contracts=10, reason="ok"),
        Verdict(True, contracts=8, reason="ok"), "approved",
    )
    trader._update_counterfactual(
        "ledger-1", execution_status="filled", actual_filled=8,
        actual_avg_price=0.5, actual_fees=0.8, fill_ratio=1.0,
        baseline_cf_filled=10, baseline_cf_avg_price=0.5,
        baseline_cf_fees=1.0, smart_cf_filled=8,
        smart_cf_avg_price=0.5, smart_cf_fees=0.8,
    )
    pos = OpenPosition(
        asset="XRP", market_ticker="XRP-M1", strategy_key="lead-lag:v1",
        side="yes", contracts=8, avg_price=0.5, fees=0.8,
        entry_ts=1000, entry_regime="MID", reason="test",
        run_id="shadow-XRP-test", counterfactual_id="ledger-1",
    )

    trader._settle_counterfactual(pos, "yes", actual_net=3.2)

    [row] = trader.db.query(
        "SELECT * FROM smart_counterfactuals WHERE ledger_id='ledger-1'"
    )
    assert row["baseline_risk_contracts"] == 10
    assert row["smart_risk_contracts"] == 8
    assert row["baseline_cf_pnl_net"] == 4.0
    assert row["smart_cf_pnl_net"] == 3.2
    assert row["actual_pnl_net"] == 3.2
    trader.db.close()


def test_counterfactual_records_smart_veto(tmp_path):
    trader = make_trader(tmp_path)
    trader.recorders["XRP"].latest_book = Orderbook.from_api({
        "orderbook_fp": {"yes_dollars": [], "no_dollars": [["0.50", "20"]]}
    })
    snap = FeatureSnapshot(
        ts=1000.0, asset="XRP", market_ticker="XRP-M2",
        regime=Regime.MID, seconds_remaining=450,
    )
    baseline = StrategySignal(
        intent=OrderIntent.BUY_YES, contracts=10, limit_price=0.5,
    )
    trader._record_counterfactual(
        "ledger-2", "XRP", snap, "lead-lag:v1", baseline,
        SmartMoneySignal(lean="DOWN", strength=1.0), 0.8, 0.0,
        True, "smart_money veto", Verdict(True, contracts=10, reason="ok"),
        None, "smart_veto",
    )
    [row] = trader.db.query(
        "SELECT * FROM smart_counterfactuals WHERE ledger_id='ledger-2'"
    )
    assert row["smart_veto"] == 1
    assert row["baseline_risk_contracts"] == 10
    assert row["smart_risk_contracts"] == 0
    assert row["execution_status"] == "smart_veto"
    assert row["baseline_cf_filled"] == 10

    trader.db.write_now("settlements", {
        "market_ticker": "XRP-M2", "asset": "XRP", "result": "yes",
        "floor_strike": 1, "expiration_value": 2, "open_ts": 0,
        "close_ts": 900, "recorded_ts": 1001, "consistent": 1,
    })
    trader._settle_counterfactual_sweep()
    [settled] = trader.db.query(
        "SELECT * FROM smart_counterfactuals WHERE ledger_id='ledger-2'"
    )
    assert settled["baseline_cf_pnl_net"] > 0
    assert settled["smart_cf_pnl_net"] == 0
    assert settled["actual_pnl_net"] == 0
    assert settled["settlement_result"] == "yes"
    trader.db.close()
