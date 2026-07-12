"""Phase-10 monitoring tests: webhook queue, health gating, n8n surface."""

import asyncio
import time
from types import SimpleNamespace

import pytest
from fastapi.testclient import TestClient

from kalshibot.config import AssetConfig, Mode
from kalshibot.dashboard.api import WSHub, create_app
from kalshibot.monitoring.healthcheck import health_report
from kalshibot.monitoring.webhooks import WebhookNotifier
from kalshibot.orders.risk import RiskConfig, RiskManager
from kalshibot.persistence.db import Database


# ------------------------------------------------------------- webhooks

def test_notifier_disabled_without_base_url():
    n = WebhookNotifier(None)
    n.emit("trade_executed", {"x": 1})  # no-op, no queue growth
    assert n._queue.qsize() == 0


def test_notifier_enqueues_and_drops_on_overflow():
    n = WebhookNotifier("http://localhost:5678")
    for i in range(1100):
        n.emit("settlement", {"i": i})
    assert n._queue.qsize() == 1000
    assert n.dropped == 100


# ---------------------------------------------------------- health gate

def make_trader(tmp_path, *, fresh: bool):
    now = time.time()
    db = Database(tmp_path / "h.db")

    class FakeWindow:
        def age_seconds(self, _now):
            return 1.0 if fresh else 500.0

    router = SimpleNamespace(window=lambda asset, now=None: (FakeWindow(), "coinbase"))
    snap = SimpleNamespace(ts=now - (1 if fresh else 500))
    return SimpleNamespace(
        router=router,
        recorders={"BTC": SimpleNamespace(latest_book_ts=now - (1 if fresh else 500))},
        latest_snapshots={"BTC": snap},
        db=db,
    )


def test_health_report_healthy(tmp_path):
    healthy, problems = health_report(make_trader(tmp_path, fresh=True))
    assert healthy and problems == []


def test_health_report_stale_everything(tmp_path):
    healthy, problems = health_report(make_trader(tmp_path, fresh=False))
    assert not healthy
    assert any("spot" in p for p in problems)
    assert any("book" in p for p in problems)
    assert any("snapshots" in p for p in problems)


# ---------------------------------------------------------- n8n surface

def stub_trader(tmp_path, n8n_token):
    db = Database(tmp_path / "n8n.db")
    return SimpleNamespace(
        settings=SimpleNamespace(
            mode=Mode.SHADOW, dashboard_token="dash-token",
            n8n_api_bearer_token=n8n_token,
            dashboard_session_secret="test-session-secret",
            dashboard_rp_id="localhost", dashboard_origin="https://localhost",
        ),
        db=db, risk=RiskManager(config=RiskConfig()),
        recorders={}, asset_configs={"BTC": AssetConfig(symbol="BTC")},
        strategies={}, positions={}, resting={}, smart={},
        latest_snapshots={}, router=None, hub=WSHub(),
        notifier=WebhookNotifier(None),
        _session_start=time.time(), clock_offset_ms=0.0,
    )


def test_n8n_surface_hidden_when_unconfigured(tmp_path):
    client = TestClient(create_app(stub_trader(tmp_path, n8n_token=None)))
    r = client.get("/n8n/status", headers={"Authorization": "Bearer anything"})
    assert r.status_code == 404


def test_n8n_surface_with_own_token(tmp_path):
    client = TestClient(create_app(stub_trader(tmp_path, n8n_token="n8n-secret")))
    assert client.get("/n8n/status").status_code == 401
    # dashboard token must NOT work on the n8n surface
    r = client.get("/n8n/status", headers={"Authorization": "Bearer dash-token"})
    assert r.status_code == 401
    r = client.get("/n8n/status", headers={"Authorization": "Bearer n8n-secret"})
    assert r.status_code == 200
    assert r.json()["mode"] == "SHADOW"
    r = client.post("/n8n/pause/BTC", headers={"Authorization": "Bearer n8n-secret"})
    assert r.json()["paused"] is True
    r = client.get("/n8n/pnl", headers={"Authorization": "Bearer n8n-secret"})
    assert "campaign_net" in r.json()
