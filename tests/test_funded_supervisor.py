"""Funded supervisor lifecycle tests. No test can reach an exchange."""

import time
from pathlib import Path

import pytest

from kalshibot.config import Mode, Settings
from kalshibot.features.engine import FeatureSnapshot, Regime
from kalshibot.funded_supervisor import FundedSupervisor
from kalshibot.kalshi.models import OrderResponse
from kalshibot.live_trader import LIVE_CONFIRMATION, LiveTradingRefused


class FakeClient:
    def __init__(self) -> None:
        self.placed = []
        self.positions = {"market_positions": []}
        self.orders = []
        self.closed = False

    async def get_api_keys(self):
        return {"api_keys": [{"api_key_id": "write-key", "scopes": ["write"]}]}

    async def get_positions(self):
        return self.positions

    async def get_orders(self, **params):
        return self.orders

    async def place_order(self, order):
        self.placed.append(order)
        return OrderResponse.model_validate({
            "order_id": "exchange-1",
            "client_order_id": order.client_order_id,
            "fill_count": "1.00",
            "remaining_count": "0.00",
            "average_fill_price": "0.4000",
            "average_fee_paid": "0.0100",
        })

    async def close(self):
        self.closed = True


def live_settings() -> Settings:
    return Settings(
        _env_file=None,
        MODE=Mode.LIVE.value,
        KALSHI_KEY_ID="write-key",
        KALSHI_PRIVATE_KEY_PATH=Path("/not/read/by/fake.pem"),
        LIVE_TRADING_ENABLED=True,
        LIVE_TRADING_CONFIRMATION=LIVE_CONFIRMATION,
        LIVE_ALLOWED_ASSETS="SOL",
        LIVE_MAX_CONTRACTS_PER_ORDER=1,
        LIVE_MAX_DAILY_LOSS_USD=5,
    )


def snapshot(*, age: float = 0.0) -> FeatureSnapshot:
    return FeatureSnapshot(
        ts=time.time() - age,
        asset="SOL",
        market_ticker="KXSOL15M-TEST",
        regime=Regime.MID,
        seconds_remaining=450,
        btc_ret_30s=0.001,
        yes_ask=0.4,
        edge_yes_net=0.03,
        depth_no_within_2c=100,
    )


def supervisor(tmp_path, client=None) -> FundedSupervisor:
    return FundedSupervisor(
        tmp_path / "funded.db",
        settings=live_settings(),
        client=client or FakeClient(),
    )


async def test_startup_requires_persisted_engaged_kill_switch(tmp_path):
    client = FakeClient()
    funded = supervisor(tmp_path, client)

    with pytest.raises(LiveTradingRefused, match="kill switch must be engaged"):
        await funded.start()

    assert client.placed == [] and client.closed is True


async def test_cursor_blocks_the_market_already_open_at_startup(tmp_path):
    client = FakeClient()
    funded = supervisor(tmp_path, client)
    await funded.executor.prepare()
    funded.reconciliation_ok = True
    current = snapshot()
    funded.db.write_now("signals", current.to_row())
    funded._prime_signal_cursor()

    await funded._process_snapshot(current)

    assert client.placed == []
    assert current.market_ticker in funded._startup_market_tickers
    funded.db.close()


async def test_fresh_approved_signal_submits_one_capped_ioc_and_audits(tmp_path):
    client = FakeClient()
    funded = supervisor(tmp_path, client)
    await funded.executor.prepare()
    funded.reconciliation_ok = True

    await funded._process_snapshot(snapshot())

    assert len(client.placed) == 1
    assert client.placed[0].count == "1.00"
    assert client.placed[0].time_in_force == "immediate_or_cancel"
    [decision] = funded.db.query("SELECT * FROM live_decisions")
    assert decision["status"] == "filled"
    [position] = funded.db.query("SELECT * FROM live_positions")
    assert position["contracts"] == 1 and position["status"] == "open"
    funded.db.close()


async def test_kill_switch_and_stale_signal_block_before_exchange(tmp_path):
    client = FakeClient()
    funded = supervisor(tmp_path, client)
    await funded.executor.prepare()
    funded.reconciliation_ok = True
    funded.db.write_now("config_overrides", {
        "ts": time.time(),
        "scope": "control:kill_switch",
        "changes": '{"engaged": true}',
    })
    funded._refresh_kill_switch()

    await funded._process_snapshot(snapshot())
    funded.risk.kill_switch = False
    await funded._process_snapshot(snapshot(age=6))

    assert client.placed == []
    statuses = {row["status"] for row in funded.db.query(
        "SELECT status FROM live_decisions"
    )}
    assert statuses == {"risk_veto", "stale"}
    funded.db.close()


async def test_reconciliation_compares_signed_quantity_not_only_ticker(tmp_path):
    client = FakeClient()
    funded = supervisor(tmp_path, client)
    funded.db.write_now("live_positions", {
        "market_ticker": "KXSOL15M-TEST",
        "asset": "SOL",
        "intent": "BUY_YES",
        "contracts": 1,
        "avg_price": 0.4,
        "fees": 0.01,
        "entry_ts": time.time(),
        "source_client_order_id": "local-1",
        "status": "open",
        "updated_ts": time.time(),
    })
    client.positions = {
        "market_positions": [{"ticker": "KXSOL15M-TEST", "position_fp": "-1.00"}]
    }

    assert await funded.reconcile_once() is False
    [row] = funded.db.query(
        "SELECT status, detail FROM live_reconciliations ORDER BY id DESC LIMIT 1"
    )
    assert row["status"] == "mismatch"
    assert '"exchange_positions":{"KXSOL15M-TEST":-1.0}' in row["detail"]
    funded.db.close()


async def test_settlement_is_idempotent_and_recovers_daily_pnl(tmp_path):
    client = FakeClient()
    funded = supervisor(tmp_path, client)
    await funded.executor.prepare()
    funded.reconciliation_ok = True
    await funded._process_snapshot(snapshot())
    funded.db.write_now("settlements", {
        "market_ticker": "KXSOL15M-TEST",
        "asset": "SOL",
        "result": "yes",
        "floor_strike": None,
        "expiration_value": None,
        "open_ts": None,
        "close_ts": time.time(),
        "recorded_ts": time.time(),
        "consistent": None,
    })

    funded._settle_live_positions()
    funded._settle_live_positions()

    [outcome] = funded.db.query("SELECT * FROM live_position_outcomes")
    assert outcome["pnl_net"] > 0
    [position] = funded.db.query("SELECT status FROM live_positions")
    assert position["status"] == "settled"
    assert funded.risk.daily_pnl["SOL"] == outcome["pnl_net"]
    funded.db.close()
