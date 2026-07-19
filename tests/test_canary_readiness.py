"""One-contract normalization tests for the funded-canary report."""

import importlib.util
from pathlib import Path

from kalshibot.persistence.db import Database


SCRIPT = Path(__file__).resolve().parents[1] / "scripts" / "canary_readiness.py"
SPEC = importlib.util.spec_from_file_location("canary_readiness", SCRIPT)
assert SPEC and SPEC.loader
canary_readiness = importlib.util.module_from_spec(SPEC)
SPEC.loader.exec_module(canary_readiness)


def add_settled(db, proposal_id, ticker, filled, pnl, ts):
    db.write_now("live_proposals", {
        "proposal_id": proposal_id,
        "created_ts": ts,
        "asset": "SOL",
        "market_ticker": ticker,
        "strategy": "test:v1",
        "intent": "BUY_YES",
        "execution": "taker",
        "limit_price": 0.4,
        "requested_contracts": filled,
        "risk_contracts": filled,
        "status": "dry_run",
        "reason": "test",
        "snapshot": "{}",
    })
    db.write_now("live_proposal_outcomes", {
        "proposal_id": proposal_id,
        "recorded_ts": ts,
        "outcome_status": "settled",
        "expected_filled": filled,
        "expected_avg_price": 0.4,
        "expected_fees": 0,
        "fill_assumption": "test",
        "settlement_result": "yes" if pnl > 0 else "no",
        "payout_per_contract": 1,
        "hypothetical_pnl_gross": pnl,
        "hypothetical_pnl_net": pnl,
        "settled_ts": ts + 1,
    })


def test_report_uses_one_contract_metrics_for_canary_gates(tmp_path):
    path = tmp_path / "readiness.db"
    db = Database(path)
    add_settled(db, "p1", "M1", 10, 5.0, 100)
    add_settled(db, "p2", "M2", 2, -1.0, 200)
    db.close()

    report = canary_readiness.build_report(path, since=0, target=2)

    assert report["dry_run"]["net"] == 4.0
    assert report["dry_run"]["profit_factor"] == 5.0
    assert report["one_contract_projection"]["net"] == 0.0
    assert report["one_contract_projection"]["profit_factor"] == 1.0
    assert report["one_contract_projection"]["max_drawdown"] == 0.5
    assert report["one_contract_by_asset"]["SOL"]["net"] == 0.0
    assert report["gates"]["one_contract_profit_factor_at_least_1_5"] is False
    assert report["gates"]["every_candidate_asset_positive_at_one_contract"] is False
