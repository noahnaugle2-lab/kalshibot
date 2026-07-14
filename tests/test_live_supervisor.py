"""Dry-run supervisor tests: reconciliation gates proposals, never orders."""

from types import SimpleNamespace

import pytest

from kalshibot.features.engine import FeatureSnapshot, Regime
from kalshibot.kalshi.models import OrderIntent
from kalshibot.live_supervisor import LiveDryRunSupervisor
from kalshibot.orders.risk import RiskConfig, RiskManager
from kalshibot.persistence.db import Database
from kalshibot.strategies.base import StrategySignal


class FakeClient:
    async def get_positions(self):
        return {"market_positions": []}

    async def get_orders(self):
        return []


class Strategy:
    def evaluate(self, snap, position):
        return StrategySignal(
            intent=OrderIntent.BUY_YES, contracts=5, limit_price=0.4,
            execution="taker", reason="test proposal",
        )

    def params_key(self):
        return "test:v1"


@pytest.fixture()
def db(tmp_path):
    database = Database(tmp_path / "supervisor.db")
    yield database
    database.close()


def snapshot():
    return FeatureSnapshot(
        ts=1_800_000_000.0, asset="SOL", market_ticker="KXSOL15M-TEST",
        regime=Regime.MID, seconds_remaining=450.0, depth_no_within_2c=100,
    )


def planner(db, *, reconciliation_ok=True):
    supervisor = LiveDryRunSupervisor.__new__(LiveDryRunSupervisor)
    supervisor.db = db
    supervisor.risk = RiskManager(RiskConfig())
    supervisor.strategies = {"SOL": Strategy()}
    supervisor.asset_configs = {"SOL": SimpleNamespace(smart_money_weight=0.0)}
    supervisor.smart = {}
    supervisor.reconciliation_ok = reconciliation_ok
    return supervisor


def test_dry_run_writes_proposal_without_order_client_call(db):
    supervisor = planner(db)
    supervisor.on_snapshot("SOL", snapshot())
    [proposal] = db.query("SELECT * FROM live_proposals")
    assert proposal["status"] == "dry_run"
    assert proposal["risk_contracts"] == 5.0
    assert proposal["execution"] == "taker"
    assert db.query("SELECT * FROM live_orders") == []


def test_reconciliation_failure_blocks_proposal(db):
    supervisor = planner(db, reconciliation_ok=False)
    supervisor.on_snapshot("SOL", snapshot())
    [proposal] = db.query("SELECT * FROM live_proposals")
    assert proposal["status"] == "blocked_reconciliation"
    assert proposal["risk_contracts"] == 0.0


async def test_startup_reconciliation_audits_ok_and_mismatch(db):
    supervisor = LiveDryRunSupervisor.__new__(LiveDryRunSupervisor)
    supervisor.db = db
    supervisor.client = FakeClient()
    await supervisor.reconcile_startup()
    assert supervisor.reconciliation_ok is True
    [row] = db.query("SELECT status FROM live_reconciliations")
    assert row["status"] == "ok"

    db.write_now("live_positions", {
        "market_ticker": "UNMATCHED", "asset": "SOL", "intent": "BUY_YES",
        "contracts": 1, "avg_price": 0.5, "fees": 0, "entry_ts": 1,
        "source_client_order_id": "local-1", "status": "open", "updated_ts": 1,
    })
    await supervisor.reconcile_startup()
    assert supervisor.reconciliation_ok is False
    [row] = db.query("SELECT status FROM live_reconciliations ORDER BY id DESC LIMIT 1")
    assert row["status"] == "mismatch"
