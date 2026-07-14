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
from fastapi.responses import JSONResponse
from fastapi.security import HTTPAuthorizationCredentials, HTTPBearer
from pydantic import ValidationError

from kalshibot.dashboard import webauthn_auth as wa

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

    @app.middleware("http")
    async def security_headers(request: Request, call_next):
        response = await call_next(request)
        response.headers["Content-Security-Policy"] = (
            "default-src 'self'; script-src 'self'; style-src 'self' 'unsafe-inline'; "
            "img-src 'self' data:; connect-src 'self' wss:; object-src 'none'; "
            "base-uri 'self'; frame-ancestors 'none'; form-action 'self'"
        )
        response.headers["Strict-Transport-Security"] = "max-age=31536000; includeSubDomains"
        response.headers["X-Content-Type-Options"] = "nosniff"
        response.headers["Referrer-Policy"] = "no-referrer"
        response.headers["Permissions-Policy"] = "camera=(), microphone=(), geolocation=()"
        if request.url.path.startswith(("/api/", "/n8n/", "/auth/")):
            response.headers["Cache-Control"] = "no-store"
        return response
    token = trader.settings.dashboard_token or secrets.token_urlsafe(24)
    if not trader.settings.dashboard_token:
        logger.warning("DASHBOARD_TOKEN unset — generated for this session: %s", token)
    session_secret = trader.settings.dashboard_session_secret or token
    rp_id = trader.settings.dashboard_rp_id
    origin = trader.settings.dashboard_origin
    bearer = HTTPBearer(auto_error=False)
    failed_auth: dict[str, list[float]] = {}  # client ip -> failure timestamps

    def _client_ip(request: Request) -> str:
        # behind the Cloudflare tunnel the real client is in this header
        return request.headers.get("CF-Connecting-IP") or (
            request.client.host if request.client else "unknown"
        )

    def _session_ok(cookies) -> bool:
        return wa.verify(session_secret, cookies.get("wa_session")) is not None

    def _auth_cookie(resp, name: str, value: str, max_age: int, path: str) -> None:
        resp.set_cookie(name, value, max_age=max_age, httponly=True,
                        secure=True, samesite="lax", path=path)

    def require_auth(
        request: Request,
        credentials: HTTPAuthorizationCredentials | None = Depends(bearer),
    ) -> None:
        # a valid passkey session cookie authorizes without the bearer token
        if _session_ok(request.cookies):
            return
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

    def _active_assets() -> list[str]:
        """Assets currently TRADING (enabled and not paused) — the live book.

        Data-source/benched/disabled assets (BTC paused as a feed, ETH/HYPE/…
        disabled) are excluded so campaign/settled figures reflect only what
        the bot actually trades. Driven by config, so it auto-tracks the roster.
        """
        from kalshibot.config import load_asset_configs

        return sorted(
            s for s, c in load_asset_configs().items() if c.enabled and not c.paused
        )

    # ------------------------------------------------------- passkey auth (WebAuthn)

    @app.get("/auth/status")
    def auth_status(request: Request) -> dict:
        return {
            "registered": wa.has_credentials(db),
            "authed": _session_ok(request.cookies),
        }

    @app.post("/auth/register/options", dependencies=[Depends(require_auth)])
    def auth_register_options() -> JSONResponse:
        options_json, challenge = wa.registration_options(db, rp_id)
        resp = JSONResponse(content=json.loads(options_json))
        _auth_cookie(resp, "wa_chal",
                     wa.sign(session_secret, {"c": challenge, "t": "reg"}, wa.CHALLENGE_TTL_S),
                     wa.CHALLENGE_TTL_S, "/auth")
        return resp

    @app.post("/auth/register/verify", dependencies=[Depends(require_auth)])
    async def auth_register_verify(request: Request) -> JSONResponse:
        body = await request.json()
        data = wa.verify(session_secret, request.cookies.get("wa_chal"))
        if not data or data.get("t") != "reg":
            raise HTTPException(status_code=400, detail="challenge missing or expired")
        try:
            wa.verify_registration(db, body, data["c"], rp_id, origin,
                                   body.get("label", "passkey"))
        except Exception as exc:  # noqa: BLE001 — surface the reason to the client
            raise HTTPException(status_code=400, detail=f"registration failed: {exc}")
        resp = JSONResponse({"ok": True})  # enrolling also logs you in
        resp.delete_cookie("wa_chal", path="/auth")
        _auth_cookie(resp, "wa_session",
                     wa.sign(session_secret, {"sub": "dashboard"}, wa.SESSION_TTL_S),
                     wa.SESSION_TTL_S, "/")
        return resp

    @app.post("/auth/login/options")
    def auth_login_options() -> JSONResponse:
        try:
            options_json, challenge = wa.authentication_options(db, rp_id)
        except ValueError:
            raise HTTPException(status_code=400, detail="no passkeys registered")
        resp = JSONResponse(content=json.loads(options_json))
        _auth_cookie(resp, "wa_chal",
                     wa.sign(session_secret, {"c": challenge, "t": "auth"}, wa.CHALLENGE_TTL_S),
                     wa.CHALLENGE_TTL_S, "/auth")
        return resp

    @app.post("/auth/login/verify")
    async def auth_login_verify(request: Request) -> JSONResponse:
        body = await request.json()
        data = wa.verify(session_secret, request.cookies.get("wa_chal"))
        if not data or data.get("t") != "auth":
            raise HTTPException(status_code=400, detail="challenge missing or expired")
        try:
            wa.verify_authentication(db, body, data["c"], rp_id, origin)
        except Exception as exc:  # noqa: BLE001
            raise HTTPException(status_code=401, detail=f"authentication failed: {exc}")
        resp = JSONResponse({"ok": True})
        resp.delete_cookie("wa_chal", path="/auth")
        _auth_cookie(resp, "wa_session",
                     wa.sign(session_secret, {"sub": "dashboard"}, wa.SESSION_TTL_S),
                     wa.SESSION_TTL_S, "/")
        return resp

    @app.post("/auth/logout")
    def auth_logout() -> JSONResponse:
        resp = JSONResponse({"ok": True})
        resp.delete_cookie("wa_session", path="/")
        return resp

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
        # default to the active trading book so the campaign curve/total isn't
        # dragged by benched/data-source assets; explicit ?assets= overrides.
        wanted = assets.split(",") if assets else _active_assets()
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
            # union: statistically strongest AND biggest earners, so the
            # top-by-profit widget sees the profit leaders too
            "SELECT * FROM smart_wallets WHERE wallet IN ("
            " SELECT wallet FROM (SELECT wallet FROM smart_wallets"
            "  ORDER BY qualified DESC, ci_low DESC LIMIT 50)"
            " UNION SELECT wallet FROM (SELECT wallet FROM smart_wallets"
            "  ORDER BY pnl DESC LIMIT 10)"
            ") ORDER BY qualified DESC, ci_low DESC")]

        merged = {
            asset: {"lean": sm.lean, "strength": sm.strength}
            for asset, sm in trader.smart.items()
            if sm.lean in ("UP", "DOWN")  # contract: NEUTRAL = absent
        }
        return {"patterns": patterns, "wallets": wallets, "merged_leans": merged}

    # --------------------------------------------------------- live dry run

    @app.get("/api/live-dry-run", dependencies=[Depends(require_auth)])
    def live_dry_run(limit: int = Query(100, ge=1, le=250)) -> dict:
        """Read-only production-path rehearsal telemetry.

        This intentionally excludes raw exchange API payloads and all
        credential material. The supervisor is the only writer for proposals;
        the dashboard only reads the durable audit rows.
        """
        latest = db.query(
            "SELECT run_ts, status, detail FROM live_reconciliations "
            "ORDER BY id DESC LIMIT 1"
        )
        reconciliation = None
        if latest:
            row = latest[0]
            try:
                detail = json.loads(row["detail"])
            except (TypeError, ValueError, json.JSONDecodeError):
                detail = {"parse_error": True}
            reconciliation = {
                "run_ts": row["run_ts"], "status": row["status"], "detail": detail,
            }
        counts = {
            row["status"]: row["n"] for row in db.query(
                "SELECT status, COUNT(*) AS n FROM live_proposals GROUP BY status"
            )
        }
        by_asset = [dict(row) for row in db.query(
            "SELECT asset, status, COUNT(*) AS n FROM live_proposals "
            "GROUP BY asset, status ORDER BY asset, status"
        )]
        proposals = [dict(row) for row in db.query(
            "SELECT p.proposal_id, p.created_ts, p.asset, p.market_ticker, p.strategy, p.intent, "
            "p.execution, p.limit_price, p.requested_contracts, p.risk_contracts, p.status, p.reason, "
            "COALESCE(o.outcome_status, 'untracked') AS outcome_status, "
            "o.expected_filled, o.hypothetical_pnl_net "
            "FROM live_proposals p LEFT JOIN live_proposal_outcomes o ON o.proposal_id=p.proposal_id "
            "ORDER BY p.created_ts DESC LIMIT ?", (limit,)
        )]
        outcomes = db.query(
            "SELECT COUNT(*) AS settled, COALESCE(SUM(hypothetical_pnl_net), 0) AS net "
            "FROM live_proposal_outcomes WHERE outcome_status='settled'"
        )[0]
        return {
            "reconciliation": reconciliation,
            "summary": {
                "proposal_counts": counts,
                "by_asset": by_asset,
                "live_orders": db.query("SELECT COUNT(*) AS n FROM live_orders")[0]["n"],
                "open_live_positions": db.query(
                    "SELECT COUNT(*) AS n FROM live_positions WHERE status='open'"
                )[0]["n"],
                "settled_proposals": outcomes["settled"],
                "hypothetical_net": round(outcomes["net"], 2),
            },
            "proposals": proposals,
        }

    # ------------------------------------------------------------- config

    @app.get("/api/config", dependencies=[Depends(require_auth)])
    def get_config() -> dict:
        from kalshibot.strategies.library import REGISTRY
        return {
            "assets": {a: c.model_dump() for a, c in trader.asset_configs.items()},
            "risk": trader.risk.config.model_dump(),
            "blackouts": [b.model_dump() for b in trader.risk.blackouts],
            # valid strategy keys straight from the backend registry so the
            # config dropdown can never drift out of sync with what build_strategy
            # accepts (a mismatched key 500s the PUT)
            "strategies": sorted(REGISTRY),
        }

    @app.put("/api/config/assets/{symbol}", dependencies=[Depends(require_auth)])
    def put_asset_config(symbol: str, body: dict) -> dict:
        cfg = trader.asset_configs.get(symbol)
        if cfg is None:
            raise HTTPException(404, f"unknown asset {symbol}")
        allowed = {"strategy", "strategy_params", "edge_threshold_cents",
                   "max_position_contracts", "smart_money_weight", "paused"}
        updates = {k: v for k, v in body.items() if k in allowed}
        from kalshibot.config import AssetConfig
        try:
            updated = AssetConfig.model_validate({**cfg.model_dump(), **updates})
        except ValidationError as exc:
            raise HTTPException(422, detail=exc.errors(include_url=False)) from exc
        # Validate-then-commit: build the strategy FIRST so an unknown key
        # returns a clean 400 instead of a 500 that has already corrupted the
        # in-memory config (asset_configs was mutated before build in the old
        # order). Only assign state once construction succeeds.
        new_strategy = None
        if ("strategy" in updates or "strategy_params" in updates) and updated.strategy:
            from kalshibot.strategies.library import build_strategy
            try:
                new_strategy = build_strategy(updated.strategy, updated.strategy_params)
            except KeyError as exc:
                raise HTTPException(400, f"invalid strategy: {exc}") from exc
        trader.asset_configs[symbol] = updated
        if updated.strategy is None:
            trader.strategies.pop(symbol, None)
        elif new_strategy is not None:
            trader.strategies[symbol] = new_strategy
        if "paused" in updates:
            (trader.risk.paused_assets.add if updates["paused"]
             else trader.risk.paused_assets.discard)(symbol)
        db.write_now("config_overrides", {
            "ts": time.time(), "scope": f"asset:{symbol}",
            "changes": json.dumps(updates),
        })
        logger.info("config updated for %s: %s (in-memory; YAML is the "
                    "restart source of truth)", symbol, updates)
        return updated.model_dump()

    # ------------------------------------------------------------ control

    @app.post("/api/control/kill", dependencies=[Depends(require_auth)])
    def kill(body: dict | None = None) -> dict:
        engage = True if body is None else bool(body.get("engage", True))
        cancelled = 0
        if engage:
            trader.risk.kill_switch = True
            cancelled = len(trader.resting)
            trader.resting.clear()
            db.write_now_sql(
                "UPDATE sim_orders SET status = 'cancelled' "
                "WHERE execution = 'maker' AND status = 'resting'"
            )
            logger.warning("KILL SWITCH ENGAGED via API (%d resting cancelled)", cancelled)
        else:
            trader.risk.kill_switch = False
            logger.warning("kill switch disengaged via API")
        db.write_now("config_overrides", {
            "ts": time.time(), "scope": "control:kill_switch",
            "changes": json.dumps({"engaged": trader.risk.kill_switch}),
        })
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
        # all figures cover only the ACTIVE trading book (SOL/XRP/DOGE) — benched
        # and data-source assets' settled history is excluded from the numbers.
        active = _active_assets()
        ph = ",".join("?" * len(active)) or "''"
        rows = db.query(
            f"SELECT asset, SUM(pnl_net) net, SUM(trades) trades FROM daily_pnl "
            f"WHERE date = ? AND asset IN ({ph}) GROUP BY asset", (day, *active),
        )
        total = db.query(
            f"SELECT SUM(p.pnl_net) net FROM sim_positions p "
            f"JOIN sim_runs r ON r.run_id = p.run_id "
            f"WHERE r.kind = 'shadow' AND p.asset IN ({ph})", tuple(active),
        )
        # rolling last-24h: settled shadow positions entered within 24h (a
        # 15-min market settles ~15min after entry, so entry_ts is a fine proxy)
        cutoff = time.time() - 86400
        last24 = db.query(
            f"SELECT p.asset asset, SUM(p.pnl_net) net, COUNT(*) n "
            f"FROM sim_positions p JOIN sim_runs r ON r.run_id = p.run_id "
            f"WHERE r.kind = 'shadow' AND p.result IS NOT NULL AND p.entry_ts >= ? "
            f"AND p.asset IN ({ph}) GROUP BY p.asset", (cutoff, *active),
        )
        return {
            "date": day,
            "active_assets": active,
            "today_by_asset": {r["asset"]: {"net": round(r["net"] or 0, 2),
                                            "trades": r["trades"]} for r in rows},
            "today_net": round(sum(r["net"] or 0 for r in rows), 2),
            "last_24h_by_asset": {r["asset"]: {"net": round(r["net"] or 0, 2),
                                               "positions": r["n"]} for r in last24},
            "last_24h_net": round(sum(r["net"] or 0 for r in last24), 2),
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

        dist_root = dist.resolve()

        @app.get("/{path:path}", include_in_schema=False)
        def spa_fallback(path: str) -> FileResponse:
            if path.startswith(("api/", "ws/", "n8n/", "assets/")):
                raise HTTPException(404)
            # Containment check: never serve a file outside dist. `path` can
            # carry `..` (incl. url-encoded %2f./%2e%2e) that resolves above
            # the SPA root — without this guard `/..%2f..%2f.env` reads secrets.
            try:
                target = (dist / path).resolve()
                inside = target == dist_root or target.is_relative_to(dist_root)
            except (OSError, RuntimeError, ValueError):
                inside = False
            if inside and target.is_file():
                return FileResponse(target)
            return FileResponse(dist / "index.html")  # client-side routes

    @app.websocket("/ws/live")
    async def ws_live(ws: WebSocket, token_param: str = Query("", alias="token")):
        # passkey session cookie OR the bearer token (?token=) authorizes the WS
        cookie_ok = wa.verify(session_secret, ws.cookies.get("wa_session")) is not None
        if not cookie_ok and not secrets.compare_digest(token_param, token):
            await ws.close(code=4401)
            return
        await ws.accept()
        queue = trader.hub.subscribe()
        # a reader task detects dead clients: browsers that vanish without a
        # close handshake never raise from send() (the transport swallows it,
        # flooding 'socket.send() raised exception' warnings forever) — but
        # receive() does raise, so racing it against the queue exits cleanly
        reader = asyncio.ensure_future(ws.receive_text())
        try:
            while True:
                getter = asyncio.ensure_future(queue.get())
                done, _ = await asyncio.wait(
                    {getter, reader}, return_when=asyncio.FIRST_COMPLETED
                )
                if reader in done:
                    getter.cancel()
                    break  # disconnected (or client spoke; either way, bail)
                await ws.send_json(getter.result())
        except (WebSocketDisconnect, asyncio.CancelledError):
            pass  # client gone or server cancelling us — either way, done
        finally:
            reader.cancel()
            if reader.done() and not reader.cancelled():
                reader.exception()  # retrieve, silencing 'never retrieved'
            trader.hub.unsubscribe(queue)

    return app


# ------------------------------------------------------------ payload glue

def _status_payload(trader) -> dict:
    from kalshibot.config import load_asset_configs

    ticks = trader.router.tick_counts if trader.router else {}
    uptime = max(1.0, time.time() - trader._session_start)
    db_path = trader.db.path
    size_mb = db_path.stat().st_size / 1e6 if db_path.exists() else 0
    active_assets = sorted(
        s for s, c in load_asset_configs().items() if c.enabled and not c.paused
    )
    return {
        "mode": trader.settings.mode.value,
        "active_assets": active_assets,
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
        "SELECT SUM(pnl_gross) g, SUM(pnl_net) n, SUM(trades) t, SUM(wins) w "
        "FROM daily_pnl WHERE date = ? AND asset = ?", (day, asset),
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
            "trades": (session["t"] or 0) if session else 0,
            "wins": (session["w"] or 0) if session else 0,
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
            # match uvicorn's real listener (asyncio sets SO_REUSEADDR): bind
            # succeeds as soon as no live listener holds the port, ignoring
            # TIME_WAIT connections from the predecessor — so a restart rebinds
            # in seconds instead of waiting out the ~60s TIME_WAIT.
            probe.setsockopt(socket.SOL_SOCKET, socket.SO_REUSEADDR, 1)
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
