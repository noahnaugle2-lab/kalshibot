"""FastAPI backend implementing the dashboard data contract (brief §4).

Runs inside the shadow trader process — live snapshots, positions, smart
leans, and the kill switch are in-process state, everything else reads the
SQLite DB. The React app talks to this exact surface; the contract types
live at dashboard/src/api/types.ts.

Auth: every /api route and the WS require `Authorization: Bearer
<DASHBOARD_TOKEN>` (WS: `?token=`). If DASHBOARD_TOKEN is unset the server
still requires it and generates a random one at startup (logged once) —
there is no unauthenticated mode, because this surface carries a kill
switch. Binds 127.0.0.1 only; public exposure goes through the tunnel +
Cloudflare Access in the deployment phase.
"""

from __future__ import annotations

import asyncio
import json
import logging
import secrets
import time
from datetime import datetime, timezone
from typing import Any

from fastapi import (
    Depends, FastAPI, HTTPException, Query, Request, WebSocket, WebSocketDisconnect,
)
from fastapi.security import HTTPAuthorizationCredentials, HTTPBearer

logger = logging.getLogger(__name__)

WS_QUEUE_SIZE = 512


class WSHub:
    """Fan-out of trader events to connected dashboard sockets."""

    def __init__(self) -> None:
        self._queues: set[asyncio.Queue] = set()

    def publish(self, message: dict) -> None:
        if not self._queues:
            return
        for queue in list(self._queues):
            try:
                queue.put_nowait(message)
            except asyncio.QueueFull:
                pass  # slow client: drop rather than block trading

    def subscribe(self) -> asyncio.Queue:
        queue: asyncio.Queue = asyncio.Queue(maxsize=WS_QUEUE_SIZE)
        self._queues.add(queue)
        return queue

    def unsubscribe(self, queue: asyncio.Queue) -> None:
        self._queues.discard(queue)


def create_app(trader) -> FastAPI:  # trader: kalshibot.trader.ShadowTrader
    app = FastAPI(title="KalshiBot", docs_url=None, redoc_url=None, openapi_url=None)
    token = trader.settings.dashboard_token or secrets.token_urlsafe(24)
    if not trader.settings.dashboard_token:
        logger.warning("DASHBOARD_TOKEN unset — generated for this session: %s", token)
    bearer = HTTPBearer(auto_error=False)
    failed_auth: dict[str, list[float]] = {}  # client ip -> failure timestamps

    def _client_ip(request: Request) -> str:
        # behind the Cloudflare tunnel the real client is in this header
        return request.headers.get("CF-Connecting-IP") or (
            request.client.host if request.client else "unknown"
        )

    def require_auth(
        request: Request,
        credentials: HTTPAuthorizationCredentials | None = Depends(bearer),
    ) -> None:
        ip = _client_ip(request)
        now = time.time()
        recent = [t for t in failed_auth.get(ip, []) if now - t < 60]
        if len(recent) >= 10:
            failed_auth[ip] = recent
            raise HTTPException(status_code=429, detail="too many auth failures")
        if credentials is None or not secrets.compare_digest(
            credentials.credentials, token
        ):
            recent.append(now)
            failed_auth[ip] = recent
            raise HTTPException(status_code=401, detail="invalid token")
        failed_auth.pop(ip, None)

    db = trader.db

    # ------------------------------------------------------------- status

    @app.get("/api/status", dependencies=[Depends(require_auth)])
    def status() -> dict:
        return _status_payload(trader)

    # --------------------------------------------------------------- live

    @app.get("/api/live", dependencies=[Depends(require_auth)])
    def live() -> list[dict]:
        return [_live_payload(trader, asset) for asset in trader.recorders]

    # -------------------------------------------------------- leaderboard

    @app.get("/api/leaderboard", dependencies=[Depends(require_auth)])
    def leaderboard(
        basis: str = Query("net"), regime: str = Query("all"),
        smart_money: str = Query("with"),
    ) -> dict:
        from kalshibot.evaluation.scorecard import build_scorecards

        cards = build_scorecards(db, kind="shadow")
        rows = []
        for rank, card in enumerate(cards, start=1):
            stats = card.overall
            if regime != "all":
                stats = card.by_regime.get(regime.upper()) or stats
            pf = stats.profit_factor
            rows.append({
                "rank": rank, "asset": card.asset,
                "strategy": card.strategy.split(":")[0] + (
                    "+ai" if card.strategy.endswith("+ai") else ""),
                "profit_factor": None if pf is None or pf != pf or pf == float("inf") else round(pf, 3),
                "pl_ratio_pct": stats.pl_ratio_pct,
                "net_pnl_per_contract": stats.pnl_per_contract,
                "return_on_capital": stats.return_on_capital,
                "hit_rate": stats.hit_rate,
                "brier_model": card.brier_model, "brier_market": card.brier_market,
                "signals_per_day": card.signals_per_day,
                "fill_rate": card.fill_rate_maker or card.fill_rate_taker,
                "avg_spread_cents": None,
                "avg_slippage_cents": None,
                "max_drawdown": stats.max_drawdown,
                "longest_losing_streak": stats.longest_losing_streak,
                "pnl_volatility": stats.pnl_volatility,
                "pnl_net": round(stats.pnl_net, 2),
                "pnl_gross": round(stats.pnl_gross, 2),
                "n_trades": stats.trades,
                "n_settled_windows": stats.trades,
                "confidence": {"ok": "high", "low": "medium"}.get(card.confidence, "low"),
                "recommendation": card.recommendation,
            })
        return {"computed_at": time.time(), "rows": rows}

    # ------------------------------------------------------------- trades

    @app.get("/api/trades", dependencies=[Depends(require_auth)])
    def trades(
        asset: str | None = None, limit: int = Query(50, le=200),
        cursor: str | None = None,
    ) -> dict:
        where = ["r.kind = 'shadow'"]
        params: list[Any] = []
        if asset:
            where.append("p.asset = ?")
            params.append(asset)
        if cursor:
            where.append("p.id < ?")
            params.append(int(cursor))
        rows = db.query(
            "SELECT p.*, r.strategy AS strategy_key FROM sim_positions p "
            f"JOIN sim_runs r ON r.run_id = p.run_id WHERE {' AND '.join(where)} "
            "ORDER BY p.id DESC LIMIT ?", (*params, limit),
        )
        out = []
        for p in rows:
            fill = db.query(
                "SELECT reason, intent FROM sim_fills WHERE run_id = ? AND "
                "market_ticker = ? ORDER BY id LIMIT 1",
                (p["run_id"], p["market_ticker"]),
            )
            reason = fill[0]["reason"] if fill else ""
            is_claude = bool(reason) and reason.startswith("claude(")
            out.append({
                "id": p["id"], "ts": p["entry_ts"], "asset": p["asset"],
                "market_ticker": p["market_ticker"],
                "intent": fill[0]["intent"] if fill else
                ("BUY_YES" if p["side"] == "yes" else "BUY_NO"),
                "contracts": p["contracts"], "limit_price": p["avg_price"],
                "avg_fill_price": p["avg_price"], "fees": p["fees"],
                "result": p["result"], "pnl_net": p["pnl_net"],
                "strategy": p["strategy_key"].split(":")[0],
                "regime_at_entry": p["entry_regime"],
                "smart_money_lean": None,
                "decision_source": "claude" if is_claude else "baseline",
                "claude": {"reasoning": reason} if is_claude else None,
            })
        next_cursor = str(rows[-1]["id"]) if len(rows) == limit else None
        return {"cursor": next_cursor, "trades": out}

    # ------------------------------------------------------------- equity

    @app.get("/api/equity", dependencies=[Depends(require_auth)])
    def equity(assets: str | None = None, basis: str = "net") -> dict:
        wanted = assets.split(",") if assets else list(trader.recorders)
        col = "pnl_net" if basis == "net" else "pnl_gross"
        series = []
        for symbol in wanted:
            rows = db.query(
                f"SELECT p.entry_ts, p.{col} AS pnl FROM sim_positions p "
                "JOIN sim_runs r ON r.run_id = p.run_id "
                "WHERE p.asset = ? AND r.kind = 'shadow' ORDER BY p.entry_ts",
                (symbol,),
            )
            cumulative = 0.0
            points = []
            for row in rows:
                cumulative += row["pnl"] or 0
                points.append([row["entry_ts"], round(cumulative, 2)])
            series.append({"asset": symbol, "points": points})
        return {"series": series}

    # --------------------------------------------------------- smartmoney

    PATTERN_DESCRIPTIONS = {
        "taker_imbalance_mid": "one-sided aggressive taker flow during the mid window",
        "late_aggression": "one-sided taker flow in the late window, pre-blackout",
        "large_prints": "direction of unusually large prints (>= p90 size)",
        "depth_imbalance_mid": "persistent one-sided resting depth near the touch (mid)",
    }

    @app.get("/api/smartmoney", dependencies=[Depends(require_auth)])
    def smartmoney() -> dict:
        # contract shape (brief §4 / dashboard types.ts) — DB rows are
        # per (pattern, asset); the contract wants one row per pattern
        by_pattern: dict[str, dict] = {}
        for r in db.query("SELECT * FROM flow_patterns ORDER BY pattern, asset"):
            agg = by_pattern.setdefault(r["pattern"], {
                "id": r["pattern"],
                "description": PATTERN_DESCRIPTIONS.get(r["pattern"], r["pattern"]),
                "n_30d": 0, "hits_30d": 0,
                "status": r["status"], "current_leans": {},
            })
            agg["n_30d"] += r["n_30d"] or 0
            agg["hits_30d"] += r["hits_30d"] or 0
            if r["status"] == "active":
                agg["status"] = "active"  # active anywhere shows active
        patterns = []
        for agg in by_pattern.values():
            hits = agg.pop("hits_30d")
            agg["hit_rate_30d"] = hits / agg["n_30d"] if agg["n_30d"] else None
            patterns.append(agg)

        wallets = [{
            "address": r["wallet"],
            "win_rate": r["win_rate"], "ci_low": r["ci_low"], "ci_high": r["ci_high"],
            "n_resolved": r["n"], "profit_usd": r["pnl"],
            "avg_entry_seconds_after_open": r["avg_entry_offset_s"],
            "qualified": bool(r["qualified"]),
            "current_positions": [],  # populated when qualified wallets exist
        } for r in db.query(
            "SELECT * FROM smart_wallets ORDER BY qualified DESC, ci_low DESC LIMIT 50")]

        merged = {
            asset: {"lean": sm.lean, "strength": sm.strength}
            for asset, sm in trader.smart.items()
            if sm.lean in ("UP", "DOWN")  # contract: NEUTRAL = absent
        }
        return {"patterns": patterns, "wallets": wallets, "merged_leans": merged}

    # ------------------------------------------------------------- config

    @app.get("/api/config", dependencies=[Depends(require_auth)])
    def get_config() -> dict:
        return {
            "assets": {a: c.model_dump() for a, c in trader.asset_configs.items()},
            "risk": trader.risk.config.model_dump(),
            "blackouts": [b.model_dump() for b in trader.risk.blackouts],
        }

    @app.put("/api/config/assets/{symbol}", dependencies=[Depends(require_auth)])
    def put_asset_config(symbol: str, body: dict) -> dict:
        cfg = trader.asset_configs.get(symbol)
        if cfg is None:
            raise HTTPException(404, f"unknown asset {symbol}")
        allowed = {"strategy", "strategy_params", "edge_threshold_cents",
                   "max_position_contracts", "smart_money_weight", "paused"}
        updates = {k: v for k, v in body.items() if k in allowed}
        updated = cfg.model_copy(update=updates)
        trader.asset_configs[symbol] = updated
        if "paused" in updates:
            (trader.risk.paused_assets.add if updates["paused"]
             else trader.risk.paused_assets.discard)(symbol)
        if "strategy" in updates or "strategy_params" in updates:
            from kalshibot.strategies.library import build_strategy
            if updated.strategy:
                trader.strategies[symbol] = build_strategy(
                    updated.strategy, updated.strategy_params)
        db.write_now("config_overrides", {
            "ts": time.time(), "scope": f"asset:{symbol}",
            "changes": json.dumps(updates),
        })
        logger.info("config updated for %s: %s (in-memory; YAML is the "
                    "restart source of truth)", symbol, updates)
        return {"ok": True, "asset": symbol, "applied": updates,
                "note": "applied in-memory; persist to config/assets.yaml to survive restart"}

    # ------------------------------------------------------------ control

    @app.post("/api/control/kill", dependencies=[Depends(require_auth)])
    def kill(body: dict | None = None) -> dict:
        engage = True if body is None else bool(body.get("engage", True))
        cancelled = 0
        if engage:
            trader.risk.kill_switch = True
            cancelled = len(trader.resting)
            trader.resting.clear()
            logger.warning("KILL SWITCH ENGAGED via API (%d resting cancelled)", cancelled)
        else:
            trader.risk.kill_switch = False
            logger.warning("kill switch disengaged via API")
        trader.hub.publish({"type": "status", "data": _status_payload(trader)})
        trader.notifier.emit("kill_switch", {
            "engaged": trader.risk.kill_switch, "cancelled_orders": cancelled,
        })
        return {"engaged": trader.risk.kill_switch, "cancelled_orders": cancelled}

    # ------------------------------------------------- n8n REST surface
    # Separate bearer token (N8N_API_BEARER_TOKEN) per spec: n8n workflows
    # query status/PnL and pause/resume without holding the dashboard token.

    n8n_token = trader.settings.n8n_api_bearer_token

    def require_n8n_auth(
        credentials: HTTPAuthorizationCredentials | None = Depends(bearer),
    ) -> None:
        if not n8n_token:
            raise HTTPException(status_code=404)  # surface off when unconfigured
        if credentials is None or not secrets.compare_digest(
            credentials.credentials, n8n_token
        ):
            raise HTTPException(status_code=401, detail="invalid token")

    @app.get("/n8n/status", dependencies=[Depends(require_n8n_auth)])
    def n8n_status() -> dict:
        payload = _status_payload(trader)
        return {
            "mode": payload["mode"],
            "kill_switch_engaged": payload["kill_switch_engaged"],
            "feeds_connected": all(
                f.get("connected") for f in payload["feeds"].values()),
            "paused_assets": sorted(trader.risk.paused_assets),
            "open_positions": len(trader.positions),
        }

    @app.get("/n8n/pnl", dependencies=[Depends(require_n8n_auth)])
    def n8n_pnl() -> dict:
        day = time.strftime("%Y-%m-%d", time.gmtime())
        rows = db.query(
            "SELECT asset, SUM(pnl_net) net, SUM(trades) trades FROM daily_pnl "
            "WHERE date = ? GROUP BY asset", (day,),
        )
        total = db.query(
            "SELECT SUM(p.pnl_net) net FROM sim_positions p "
            "JOIN sim_runs r ON r.run_id = p.run_id WHERE r.kind = 'shadow'",
        )
        return {
            "date": day,
            "today_by_asset": {r["asset"]: {"net": round(r["net"] or 0, 2),
                                            "trades": r["trades"]} for r in rows},
            "today_net": round(sum(r["net"] or 0 for r in rows), 2),
            "campaign_net": round(total[0]["net"] or 0, 2) if total else 0.0,
        }

    @app.post("/n8n/pause/{symbol}", dependencies=[Depends(require_n8n_auth)])
    def n8n_pause(symbol: str) -> dict:
        trader.risk.paused_assets.add(symbol)
        return {"asset": symbol, "paused": True}

    @app.post("/n8n/resume/{symbol}", dependencies=[Depends(require_n8n_auth)])
    def n8n_resume(symbol: str) -> dict:
        trader.risk.paused_assets.discard(symbol)
        return {"asset": symbol, "paused": False}

    @app.post("/api/control/pause/{symbol}", dependencies=[Depends(require_auth)])
    def pause(symbol: str) -> dict:
        trader.risk.paused_assets.add(symbol)
        return {"asset": symbol, "paused": True}

    @app.post("/api/control/resume/{symbol}", dependencies=[Depends(require_auth)])
    def resume(symbol: str) -> dict:
        trader.risk.paused_assets.discard(symbol)
        return {"asset": symbol, "paused": False}

    # ----------------------------------------------------------------- ws

    # ------------------------------------------------------------- static
    # Serve the built React app same-origin (dashboard/dist). The shell is
    # public (it holds no data); everything it fetches requires the bearer.

    from kalshibot.config import PROJECT_ROOT

    dist = PROJECT_ROOT / "dashboard" / "dist"
    if dist.is_dir():
        from fastapi.responses import FileResponse
        from fastapi.staticfiles import StaticFiles

        app.mount("/assets", StaticFiles(directory=dist / "assets"), name="assets")

        @app.get("/", include_in_schema=False)
        def spa_root() -> FileResponse:
            return FileResponse(dist / "index.html")

        @app.get("/{path:path}", include_in_schema=False)
        def spa_fallback(path: str) -> FileResponse:
            if path.startswith(("api/", "ws/", "n8n/", "assets/")):
                raise HTTPException(404)
            file = dist / path
            if file.is_file():
                return FileResponse(file)
            return FileResponse(dist / "index.html")  # client-side routes

    @app.websocket("/ws/live")
    async def ws_live(ws: WebSocket, token_param: str = Query("", alias="token")):
        if not secrets.compare_digest(token_param, token):
            await ws.close(code=4401)
            return
        await ws.accept()
        queue = trader.hub.subscribe()
        try:
            while True:
                message = await queue.get()
                await ws.send_json(message)
        except WebSocketDisconnect:
            pass
        finally:
            trader.hub.unsubscribe(queue)

    return app


# ------------------------------------------------------------ payload glue

def _status_payload(trader) -> dict:
    ticks = trader.router.tick_counts if trader.router else {}
    uptime = max(1.0, time.time() - trader._session_start)
    db_path = trader.db.path
    size_mb = db_path.stat().st_size / 1e6 if db_path.exists() else 0
    return {
        "mode": trader.settings.mode.value,
        "started_at": trader._session_start,
        "clock_offset_ms": getattr(trader, "clock_offset_ms", None),
        "kill_switch_engaged": trader.risk.kill_switch,
        "feeds": {
            "coinbase": {"connected": ticks.get("coinbase", 0) > 0,
                         "ticks_per_min": round(ticks.get("coinbase", 0) / (uptime / 60), 1)},
            "binance_us": {"connected": ticks.get("binance_us", 0) > 0,
                           "ticks_per_min": round(ticks.get("binance_us", 0) / (uptime / 60), 1)},
            "kalshi": {"connected": any(
                r.latest_book_ts and time.time() - r.latest_book_ts < 30
                for r in trader.recorders.values())},
        },
        "db": {"size_mb": round(size_mb, 1),
               "signals_rows": trader.db.counts().get("signals", 0)},
    }


def _live_payload(trader, asset: str) -> dict:
    recorder = trader.recorders[asset]
    market = recorder.current_market
    snap = trader.latest_snapshots.get(asset)
    sm = trader.smart.get(asset)
    position = None
    if market is not None:
        pos = trader.positions.get(market.ticker)
        if pos is not None:
            unrealized = None
            if snap is not None and snap.implied_prob is not None:
                mark = snap.implied_prob if pos.side == "yes" else 1 - snap.implied_prob
                unrealized = round((mark - pos.avg_price) * pos.contracts, 2)
            position = {"side": pos.side, "contracts": pos.contracts,
                        "avg_price": pos.avg_price, "unrealized_pnl": unrealized}
    day = time.strftime("%Y-%m-%d", time.gmtime())
    pnl_rows = trader.db.query(
        "SELECT SUM(pnl_gross) g, SUM(pnl_net) n, SUM(trades) t FROM daily_pnl "
        "WHERE date = ? AND asset = ?", (day, asset),
    )
    session = pnl_rows[0] if pnl_rows else None
    return {
        "asset": asset,
        "paused": asset in trader.risk.paused_assets,
        "market": {
            "ticker": market.ticker,
            "open_ts": market.open_time.timestamp() if market.open_time else None,
            "close_ts": market.close_time.timestamp() if market.close_time else None,
            "floor_strike": float(market.floor_strike)
            if market.floor_strike is not None else None,
            "status": market.status,
        } if market else None,
        "snapshot": json.loads(snap.model_dump_json()) if snap else None,
        "smart_money": {"lean": sm.lean, "strength": sm.strength,
                        "source_breakdown": sm.breakdown} if sm else
        {"lean": "NEUTRAL", "strength": 0.0, "source_breakdown": {}},
        "position": position,
        "session_pnl": {
            "gross": round(session["g"] or 0, 2) if session else 0.0,
            "net": round(session["n"] or 0, 2) if session else 0.0,
            "trades": session["t"] or 0 if session else 0,
        },
    }


async def serve(trader, host: str = "127.0.0.1", port: int = 8777) -> None:
    """Serve the API; retry binding while a predecessor finishes shutting down.

    A graceful trader restart overlaps the old process (which holds the port
    for up to ~10s) with the new one. A failed bind must not kill trading —
    retry with backoff, and give up on the API (not the trader) after that.
    """
    import socket

    import uvicorn

    async def wait_for_port() -> None:
        attempt = 0
        while True:
            probe = socket.socket(socket.AF_INET, socket.SOCK_STREAM)
            try:
                probe.bind((host, port))
                probe.close()
                return
            except OSError:
                probe.close()
                if attempt % 12 == 0:
                    logger.warning("port %d busy — dashboard API waiting (retrying "
                                   "forever; trading unaffected)", port)
                attempt += 1
                await asyncio.sleep(2.5 if attempt < 12 else 30.0)

    # Never give up: a predecessor may hold the port for minutes on a slow
    # shutdown. The API silently staying down is an outage; trading loops are
    # independent of this task either way.
    while True:
        await wait_for_port()
        config = uvicorn.Config(
            create_app(trader), host=host, port=port, log_level="warning",
            loop="asyncio",
        )
        server = uvicorn.Server(config)
        logger.info("dashboard API listening on http://%s:%d", host, port)
        try:
            await server.serve()
            logger.error("dashboard API server exited; restarting in 10s")
        except asyncio.CancelledError:
            raise
        except SystemExit as exc:
            logger.error("dashboard API start failed (exit %s); retrying in 10s", exc.code)
        except Exception as exc:
            logger.error("dashboard API crashed: %s; restarting in 10s", exc)
        await asyncio.sleep(10.0)


def utcnow_iso() -> str:
    return datetime.now(timezone.utc).isoformat()
