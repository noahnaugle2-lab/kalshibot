"""Dashboard API tests: auth gate, contract shapes, control endpoints."""

import time
from types import SimpleNamespace

import pytest
from fastapi.testclient import TestClient

from kalshibot.config import AssetConfig, Mode
from kalshibot.dashboard.api import WSHub, create_app
from kalshibot.orders.risk import RiskConfig, RiskManager
from kalshibot.persistence.db import Database

TOKEN = "test-token-123"


class StubTrader(SimpleNamespace):
    pass


@pytest.fixture()
def trader(tmp_path):
    db = Database(tmp_path / "api.db")
    db.write_now("sim_runs", {
        "run_id": "shadow-BTC-a", "created_ts": 1.0, "kind": "shadow",
        "asset": "BTC", "strategy": "latency_momentum:v1:", "params": "{}",
        "latency_ms": None, "seed": None, "tape_start": None,
        "tape_end": None, "windows": None, "summary": None,
    })
    db.write_now("sim_positions", {
        "run_id": "shadow-BTC-a", "asset": "BTC", "market_ticker": "M1",
        "side": "yes", "contracts": 10, "avg_price": 0.5, "fees": 0.1,
        "entry_ts": time.time() - 100, "entry_regime": "MID", "result": "yes",
        "payout_per_contract": 0.99, "pnl_gross": 4.9, "pnl_net": 4.8,
    })
    stub = StubTrader(
        settings=SimpleNamespace(mode=Mode.SHADOW, dashboard_token=TOKEN,
                                 n8n_api_bearer_token=None),
        db=db,
        risk=RiskManager(config=RiskConfig()),
        recorders={"BTC": SimpleNamespace(
            current_market=None, latest_book=None, latest_book_ts=None)},
        asset_configs={"BTC": AssetConfig(symbol="BTC", strategy="latency_momentum")},
        strategies={},
        positions={},
        resting={},
        smart={},
        latest_snapshots={},
        router=None,
        hub=WSHub(),
        notifier=__import__('kalshibot.monitoring.webhooks', fromlist=['WebhookNotifier']).WebhookNotifier(None),
        _session_start=time.time() - 60,
        clock_offset_ms=-12.0,
    )
    yield stub
    db.close()


@pytest.fixture()
def client(trader):
    return TestClient(create_app(trader))


def auth() -> dict:
    return {"Authorization": f"Bearer {TOKEN}"}


def test_all_api_routes_require_auth(client):
    for path in ("/api/status", "/api/live", "/api/leaderboard", "/api/trades",
                 "/api/equity", "/api/smartmoney", "/api/config"):
        assert client.get(path).status_code == 401, path
    assert client.post("/api/control/kill").status_code == 401
    assert client.get("/api/status", headers={"Authorization": "Bearer wrong"}).status_code == 401


def test_status_shape(client):
    body = client.get("/api/status", headers=auth()).json()
    assert body["mode"] == "SHADOW"
    assert body["kill_switch_engaged"] is False
    assert set(body["feeds"]) == {"coinbase", "binance_us", "kalshi"}
    assert body["clock_offset_ms"] == -12.0


def test_live_handles_no_market(client):
    [row] = client.get("/api/live", headers=auth()).json()
    assert row["asset"] == "BTC" and row["market"] is None
    assert row["smart_money"]["lean"] == "NEUTRAL"


def test_leaderboard_rows(client):
    body = client.get("/api/leaderboard", headers=auth()).json()
    [row] = body["rows"]
    assert row["asset"] == "BTC" and row["n_trades"] == 1
    assert row["recommendation"] == "collect"


def test_trades_pagination_shape(client):
    body = client.get("/api/trades", headers=auth()).json()
    assert body["cursor"] is None
    [trade] = body["trades"]
    assert trade["decision_source"] == "baseline"
    assert trade["pnl_net"] == 4.8


def test_kill_switch_engage_disengage(client, trader):
    body = client.post("/api/control/kill", headers=auth()).json()
    assert body["engaged"] is True and trader.risk.kill_switch is True
    body = client.post("/api/control/kill", headers=auth(),
                       json={"engage": False}).json()
    assert body["engaged"] is False and trader.risk.kill_switch is False


def test_pause_resume(client, trader):
    client.post("/api/control/pause/BTC", headers=auth())
    assert "BTC" in trader.risk.paused_assets
    client.post("/api/control/resume/BTC", headers=auth())
    assert "BTC" not in trader.risk.paused_assets


def test_config_put_applies_and_audits(client, trader):
    response = client.put("/api/config/assets/BTC", headers=auth(),
                          json={"smart_money_weight": 0.3, "paused": True,
                                "bogus_field": 1})
    assert response.status_code == 200
    assert trader.asset_configs["BTC"].smart_money_weight == 0.3
    assert "BTC" in trader.risk.paused_assets
    assert "bogus_field" not in response.json()["applied"]
    audit = trader.db.query("SELECT * FROM config_overrides")
    assert len(audit) == 1 and "asset:BTC" == audit[0]["scope"]


def test_docs_disabled(client):
    assert client.get("/docs").status_code == 404
    assert client.get("/openapi.json").status_code == 404


def test_ws_rejects_bad_token(client):
    with pytest.raises(Exception):
        with client.websocket_connect("/ws/live?token=wrong"):
            pass
