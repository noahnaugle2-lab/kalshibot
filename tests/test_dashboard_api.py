"""Dashboard API tests: auth gate, contract shapes, control endpoints."""

import time
from types import SimpleNamespace

import pytest
from fastapi.testclient import TestClient

from kalshibot.config import AssetConfig, Mode
from kalshibot.dashboard.api import WSHub, create_app
from kalshibot.orders.risk import RiskConfig, RiskManager
from kalshibot.persistence.db import Database
from kalshibot.strategies.library import build_strategy
from kalshibot.trader import ShadowTrader

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
        settings=SimpleNamespace(
            mode=Mode.SHADOW, dashboard_token=TOKEN, n8n_api_bearer_token=None,
            dashboard_session_secret="test-session-secret",
            dashboard_rp_id="localhost", dashboard_origin="https://localhost",
        ),
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
                 "/api/equity", "/api/smartmoney", "/api/live-dry-run", "/api/config"):
        assert client.get(path).status_code == 401, path
    assert client.post("/api/control/kill").status_code == 401
    assert client.get("/api/status", headers={"Authorization": "Bearer wrong"}).status_code == 401


def test_status_shape(client):
    response = client.get("/api/status", headers=auth())
    body = response.json()
    assert body["mode"] == "SHADOW"
    assert body["kill_switch_engaged"] is False
    assert set(body["feeds"]) == {"coinbase", "binance_us", "kalshi"}
    assert body["clock_offset_ms"] == -12.0
    assert response.headers["cache-control"] == "no-store"
    assert "frame-ancestors 'none'" in response.headers["content-security-policy"]
    assert response.headers["x-content-type-options"] == "nosniff"


def test_live_handles_no_market(client):
    [row] = client.get("/api/live", headers=auth()).json()
    assert row["asset"] == "BTC" and row["market"] is None
    assert row["smart_money"]["lean"] == "NEUTRAL"


def test_smartmoney_exposes_read_only_strategy_intelligence(client):
    body = client.get("/api/smartmoney", headers=auth()).json()
    intelligence = body["strategy_intelligence"]
    assert intelligence["counts"] == {
        "monthly_leaders": 0, "fingerprints": 0, "replays": 0,
    }
    assert intelligence["monthly_leaderboard"] == []
    assert intelligence["copyability"] == []


def test_live_dry_run_is_read_only_and_omits_exchange_raw_payloads(client, trader):
    trader.db.write_now("live_reconciliations", {
        "run_ts": 100.0, "status": "ok", "external_positions": '{"secret":"never"}',
        "external_orders": '[]', "detail": '{"local_position_tickers":[]}',
    })
    trader.db.write_now("live_proposals", {
        "proposal_id": "proposal-1", "created_ts": 101.0, "asset": "BTC",
        "market_ticker": "M1", "strategy": "latency_momentum:v1", "intent": "BUY_YES",
        "execution": "taker", "limit_price": 0.42, "requested_contracts": 5,
        "risk_contracts": 2, "status": "dry_run", "reason": "ok", "snapshot": '{}',
    })
    body = client.get("/api/live-dry-run", headers=auth()).json()
    assert body["reconciliation"]["status"] == "ok"
    assert "external_positions" not in body["reconciliation"]
    assert body["summary"]["proposal_counts"] == {"dry_run": 1}
    assert body["proposals"][0]["proposal_id"] == "proposal-1"
    assert body["proposals"][0]["outcome_status"] == "untracked"
    assert body["summary"]["live_orders"] == 0


def test_live_dry_run_reports_one_contract_normalized_pnl(client, trader):
    trader.db.write_now("live_proposals", {
        "proposal_id": "normalized-1", "created_ts": 101.0, "asset": "BTC",
        "market_ticker": "M2", "strategy": "latency_momentum:v1",
        "intent": "BUY_YES", "execution": "taker", "limit_price": 0.42,
        "requested_contracts": 10, "risk_contracts": 10, "status": "dry_run",
        "reason": "ok", "snapshot": '{}',
    })
    trader.db.write_now("live_proposal_outcomes", {
        "proposal_id": "normalized-1", "recorded_ts": 101.0,
        "outcome_status": "settled", "expected_filled": 10,
        "expected_avg_price": 0.42, "expected_fees": 0.2,
        "fill_assumption": "test", "settlement_result": "yes",
        "payout_per_contract": 1.0, "hypothetical_pnl_gross": 5.8,
        "hypothetical_pnl_net": 5.0, "settled_ts": 200.0,
    })

    body = client.get("/api/live-dry-run", headers=auth()).json()

    assert body["summary"]["hypothetical_net"] == 5.0
    assert body["summary"]["one_contract_net"] == 0.5
    assert body["proposals"][0]["one_contract_pnl_net"] == 0.5


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
    rows = trader.db.query(
        "SELECT changes FROM config_overrides WHERE scope = 'control:kill_switch' "
        "ORDER BY id"
    )
    assert [r["changes"] for r in rows] == ['{"engaged": true}', '{"engaged": false}']


def test_kill_switch_durably_cancels_resting_orders(client, trader):
    trader.db.write_now("sim_orders", {
        "run_id": "shadow-BTC-a", "ts": time.time(), "asset": "BTC",
        "market_ticker": "M2", "intent": "BUY_YES", "execution": "maker",
        "limit_price": 0.45, "contracts": 10, "status": "resting",
        "filled_contracts": 0, "reason": "test",
    })
    trader.resting["M2"] = object()

    body = client.post("/api/control/kill", headers=auth()).json()

    assert body["cancelled_orders"] == 1
    assert trader.resting == {}
    [row] = trader.db.query("SELECT status FROM sim_orders WHERE market_ticker = 'M2'")
    assert row["status"] == "cancelled"


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
    assert response.json()["symbol"] == "BTC"
    assert response.json()["smart_money_weight"] == 0.3
    assert trader.asset_configs["BTC"].smart_money_weight == 0.3
    assert "BTC" in trader.risk.paused_assets
    assert "bogus_field" not in response.json()
    audit = trader.db.query("SELECT * FROM config_overrides")
    assert len(audit) == 1 and "asset:BTC" == audit[0]["scope"]


@pytest.mark.parametrize("payload", [
    {"max_position_contracts": -1},
    {"smart_money_weight": 1.5},
    {"edge_threshold_cents": -0.1},
    {"paused": "not-a-boolean"},
])
def test_config_put_rejects_invalid_values(client, trader, payload):
    before = trader.asset_configs["BTC"]
    response = client.put("/api/config/assets/BTC", headers=auth(), json=payload)
    assert response.status_code == 422
    assert trader.asset_configs["BTC"] == before


def test_config_put_removes_disabled_strategy(client, trader):
    trader.strategies["BTC"] = build_strategy("latency_momentum")
    response = client.put(
        "/api/config/assets/BTC", headers=auth(), json={"strategy": None}
    )
    assert response.status_code == 200
    assert response.json()["strategy"] is None
    assert "BTC" not in trader.strategies


def test_kill_switch_recovery_fails_safe_and_skips_resting_orders(tmp_path):
    db = Database(tmp_path / "recovery.db")
    db.write_now("config_overrides", {
        "ts": time.time(), "scope": "control:kill_switch",
        "changes": '{"engaged": true}',
    })
    trader = ShadowTrader.__new__(ShadowTrader)
    trader.db = db
    trader.risk = RiskManager(config=RiskConfig())
    trader.resting = {}

    trader._recover_kill_switch()
    trader._recover_resting()

    assert trader.risk.kill_switch is True
    assert trader.resting == {}
    db.close()


def test_docs_disabled(client):
    # with the SPA catch-all, /docs and /openapi.json serve the app shell —
    # what matters is that no OpenAPI schema or Swagger UI leaks
    for path in ("/docs", "/openapi.json"):
        response = client.get(path)
        assert response.status_code in (200, 404)
        assert "openapi" not in response.text.lower()
        assert "swagger" not in response.text.lower()


def test_ws_rejects_bad_token(client):
    with pytest.raises(Exception):
        with client.websocket_connect("/ws/live?token=wrong"):
            pass
