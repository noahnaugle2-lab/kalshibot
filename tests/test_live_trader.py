"""Live executor safety tests.  No test can reach an exchange."""

from decimal import Decimal
from pathlib import Path

import pytest

from kalshibot.config import Mode, Settings
from kalshibot.features.engine import FeatureSnapshot, Regime
from kalshibot.kalshi.models import OrderIntent, OrderResponse
from kalshibot.live_trader import LIVE_CONFIRMATION, LiveTrader, LiveTradingRefused
from kalshibot.persistence.db import Database
from kalshibot.strategies.base import StrategySignal


class FakeClient:
    def __init__(self) -> None:
        self.orders = []

    async def place_order(self, order):
        self.orders.append(order)
        return OrderResponse.model_validate({
            "order_id": "exchange-1", "client_order_id": order.client_order_id,
            "fill_count": "1.00", "remaining_count": "0.00",
            "average_fill_price": "0.3200", "average_fee_paid": "0.0100",
        })

    async def get_orders(self, **params):
        return []

    async def get_api_keys(self):
        return {"api_keys": [{"api_key_id": "production-key", "scope": "write"}]}


def settings(**overrides) -> Settings:
    base = {
        "MODE": Mode.LIVE.value,
        "KALSHI_KEY_ID": "production-key",
        "KALSHI_PRIVATE_KEY_PATH": Path("/tmp/key.pem"),
        "LIVE_TRADING_ENABLED": True,
        "LIVE_TRADING_CONFIRMATION": LIVE_CONFIRMATION,
        "LIVE_ALLOWED_ASSETS": "SOL",
        "LIVE_MAX_CONTRACTS_PER_ORDER": 1,
    }
    base.update(overrides)
    return Settings(_env_file=None, **base)


def snapshot() -> FeatureSnapshot:
    return FeatureSnapshot(
        ts=100.0, asset="SOL", market_ticker="KXSOL15M-TEST",
        regime=Regime.MID, seconds_remaining=450.0,
    )


def signal(**overrides) -> StrategySignal:
    base = {
        "intent": OrderIntent.BUY_NO, "contracts": 4,
        "limit_price": 0.32, "execution": "taker", "reason": "test",
    }
    base.update(overrides)
    return StrategySignal(**base)


@pytest.fixture()
def db(tmp_path):
    database = Database(tmp_path / "live.db")
    yield database
    database.close()


def test_live_interlocks_fail_closed(db):
    client = FakeClient()
    with pytest.raises(LiveTradingRefused, match="MODE"):
        LiveTrader(settings(MODE="SHADOW"), db, client).assert_armed("SOL")
    with pytest.raises(LiveTradingRefused, match="ENABLED"):
        LiveTrader(settings(LIVE_TRADING_ENABLED=False), db, client).assert_armed("SOL")
    with pytest.raises(LiveTradingRefused, match="exact"):
        LiveTrader(settings(LIVE_TRADING_CONFIRMATION="yes"), db, client).assert_armed("SOL")
    with pytest.raises(LiveTradingRefused, match="allowlisted"):
        LiveTrader(settings(), db, client).assert_armed("XRP")


async def test_ioc_order_is_capped_persisted_and_not_duplicated(db):
    client = FakeClient()
    trader = LiveTrader(settings(), db, client)
    await trader.prepare()
    result = await trader.submit_approved("SOL", snapshot(), signal())

    assert result.order_id == "exchange-1"
    assert len(client.orders) == 1
    request = client.orders[0]
    assert request.time_in_force == "immediate_or_cancel"
    assert request.count == "1.00"  # explicit live cap, rather than signal's four
    assert request.side == "ask" and request.price == "0.6800"  # BUY_NO inversion
    [order] = db.query("SELECT * FROM live_orders")
    assert order["status"] == "filled" and order["filled_contracts"] == 1.0
    [position] = db.query("SELECT * FROM live_positions")
    assert position["contracts"] == 1.0 and position["intent"] == "BUY_NO"

    with pytest.raises(LiveTradingRefused, match="duplicate"):
        await trader.submit_approved("SOL", snapshot(), signal())
    assert len(client.orders) == 1


async def test_live_executor_refuses_maker_and_sell_before_writes(db):
    trader = LiveTrader(settings(), db, FakeClient())
    with pytest.raises(LiveTradingRefused, match="maker"):
        await trader.submit_approved("SOL", snapshot(), signal(execution="maker"))
    with pytest.raises(LiveTradingRefused, match="only BUY"):
        await trader.submit_approved(
            "SOL", snapshot(), signal(intent=OrderIntent.SELL_YES)
        )
    assert db.query("SELECT * FROM live_orders") == []


async def test_live_executor_requires_verified_write_scope(db):
    trader = LiveTrader(settings(), db, FakeClient())
    with pytest.raises(LiveTradingRefused, match="scope has not been verified"):
        await trader.submit_approved("SOL", snapshot(), signal())


def test_client_order_id_is_stable_and_bounded():
    order_id = LiveTrader.client_order_id("SOL", signal(), "KXSOL15M-TEST")
    assert order_id == LiveTrader.client_order_id("SOL", signal(), "KXSOL15M-TEST")
    assert order_id.startswith("kb-live-") and len(order_id) == 27
