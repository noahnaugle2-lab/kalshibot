"""Fail-closed funded supervisor driven by the canonical shadow signal stream.

The shadow service remains the sole owner of market-data feeds and the
dashboard.  This separate process tails newly persisted FeatureSnapshots,
re-runs the exact strategy and risk checks, then hands approved taker signals
to :class:`LiveTrader`.  It never backfills signals from before startup.

Safety properties:
- all LiveTrader interlocks and production write-scope verification must pass
- the persisted kill switch must be engaged at startup
- only explicitly allowlisted, enabled, unpaused assets get strategies
- a signal older than five seconds can never reach the exchange
- startup and continuous reconciliation must remain exact
- any reconciliation or ambiguous submission error terminates the process
- the systemd unit has Restart=no, so failure requires a human restart
"""

from __future__ import annotations

import asyncio
import json
import logging
import time
import uuid
from datetime import datetime, timezone
from pathlib import Path
from typing import Any

from kalshibot.config import PROJECT_ROOT, Mode, Settings, load_asset_configs, load_risk
from kalshibot.features.engine import FeatureSnapshot
from kalshibot.kalshi.auth import KalshiSigner
from kalshibot.kalshi.client import KalshiClient, KalshiEnvironment
from kalshibot.kalshi.models import Order, OrderIntent
from kalshibot.live_trader import LiveTrader, LiveTradingRefused
from kalshibot.orders.risk import RiskManager
from kalshibot.persistence.db import Database, dump_json
from kalshibot.sim.fills import DEFAULT_PAYOUT, settle_position
from kalshibot.smartmoney.signal import SmartMoneySignal, apply_modifier
from kalshibot.strategies.base import PositionState, Strategy, StrategySignal
from kalshibot.strategies.library import build_strategy

logger = logging.getLogger(__name__)

SIGNAL_POLL_S = 0.5
RECONCILIATION_S = 60.0
SETTLEMENT_S = 30.0
MAX_SIGNAL_AGE_S = 5.0


class FundedSupervisor:
    """Consume fresh snapshots and submit only fully approved live IOC orders."""

    def __init__(
        self,
        db_path: str | Path | None = None,
        *,
        settings: Settings | None = None,
        client: KalshiClient | None = None,
    ) -> None:
        self.settings = settings or Settings()
        if self.settings.mode is not Mode.LIVE:
            raise LiveTradingRefused("funded supervisor requires MODE=LIVE")
        if not self.settings.kalshi_key_id or not self.settings.kalshi_private_key_path:
            raise LiveTradingRefused("production Kalshi credentials are missing")

        self.db = Database(db_path or PROJECT_ROOT / "data" / "kalshibot.db")
        if client is None:
            signer = KalshiSigner.from_pem_file(
                self.settings.kalshi_key_id, self.settings.kalshi_private_key_path
            )
            client = KalshiClient(KalshiEnvironment.PROD, signer=signer)
        self.client = client
        self.executor = LiveTrader(self.settings, self.db, self.client)
        self.asset_configs = load_asset_configs()
        self.risk: RiskManager = load_risk()
        live_loss_cap = self.settings.live_max_daily_loss_usd
        self.risk.config.max_daily_loss_per_asset_usd = min(
            self.risk.config.max_daily_loss_per_asset_usd, live_loss_cap
        )
        self.risk.config.max_daily_loss_global_usd = min(
            self.risk.config.max_daily_loss_global_usd, live_loss_cap
        )

        allowed = self.executor.allowed_assets
        unknown = allowed - set(self.asset_configs)
        if unknown:
            raise LiveTradingRefused(f"unknown LIVE_ALLOWED_ASSETS: {sorted(unknown)}")
        self.strategies: dict[str, Strategy] = {}
        for asset in sorted(allowed):
            cfg = self.asset_configs[asset]
            if not cfg.enabled or cfg.paused or not cfg.strategy:
                raise LiveTradingRefused(
                    f"{asset} is not enabled, unpaused, and strategy-configured"
                )
            self.strategies[asset] = build_strategy(cfg.strategy, cfg.strategy_params)

        self.reconciliation_ok = False
        self._signal_cursor = 0
        self._startup_market_tickers: set[str] = set()
        self._kill_override_id = 0
        self._stop = asyncio.Event()

    def request_stop(self) -> None:
        self._stop.set()

    async def start(self) -> None:
        """Arm read/write prerequisites, reconcile, then supervise all loops."""
        tasks: list[asyncio.Task] = []
        stop_wait: asyncio.Task | None = None
        try:
            await self.executor.prepare()
            self._refresh_kill_switch()
            if not self.risk.kill_switch:
                raise LiveTradingRefused(
                    "persisted kill switch must be engaged before funded startup"
                )
            self._recover_live_daily_pnl()
            self._settle_live_positions()
            if not await self.reconcile_once():
                raise LiveTradingRefused("startup reconciliation mismatch")
            self._prime_signal_cursor()
            logger.warning(
                "FUNDED SUPERVISOR ARMED: assets=%s max_contracts=%d daily_loss=$%.2f; "
                "kill switch remains engaged",
                sorted(self.strategies), self.settings.live_max_contracts_per_order,
                self.risk.config.max_daily_loss_global_usd,
            )

            tasks = [
                asyncio.create_task(self._signal_loop(), name="funded_signal_loop"),
                asyncio.create_task(
                    self._reconciliation_loop(), name="funded_reconciliation_loop"
                ),
                asyncio.create_task(
                    self._settlement_loop(), name="funded_settlement_loop"
                ),
            ]
            stop_wait = asyncio.create_task(self._stop.wait(), name="funded_stop")
            done, _ = await asyncio.wait(
                [*tasks, stop_wait], return_when=asyncio.FIRST_COMPLETED
            )
            if stop_wait not in done:
                task = next(iter(done))
                exc = task.exception()
                if exc is not None:
                    raise exc
                raise RuntimeError(f"supervised task {task.get_name()} returned")
        finally:
            for task in tasks:
                task.cancel()
            if stop_wait is not None:
                stop_wait.cancel()
            if tasks:
                await asyncio.gather(*tasks, return_exceptions=True)
            self.db.close()
            await self.client.close()

    # --------------------------------------------------------------- controls

    def _refresh_kill_switch(self) -> None:
        rows = self.db.query(
            "SELECT id, changes FROM config_overrides "
            "WHERE scope='control:kill_switch' ORDER BY id DESC LIMIT 1"
        )
        if not rows or rows[0]["id"] == self._kill_override_id:
            return
        self._kill_override_id = rows[0]["id"]
        try:
            self.risk.kill_switch = bool(json.loads(rows[0]["changes"])["engaged"])
        except (KeyError, TypeError, ValueError, json.JSONDecodeError):
            logger.exception("invalid kill-switch override; engaging fail-closed")
            self.risk.kill_switch = True
        logger.warning(
            "funded kill switch %s via persisted control",
            "ENGAGED" if self.risk.kill_switch else "DISENGAGED",
        )

    def _recover_live_daily_pnl(self) -> None:
        day_start = datetime.now(timezone.utc).replace(
            hour=0, minute=0, second=0, microsecond=0
        ).timestamp()
        for row in self.db.query(
            "SELECT p.asset, o.pnl_net, o.settled_ts FROM live_position_outcomes o "
            "JOIN live_positions p ON p.market_ticker=o.market_ticker "
            "WHERE o.settled_ts>=? ORDER BY o.settled_ts",
            (day_start,),
        ):
            self.risk.record_pnl(row["asset"], row["pnl_net"], now=row["settled_ts"])

    # --------------------------------------------------------------- signals

    def _prime_signal_cursor(self) -> None:
        rows = self.db.query("SELECT COALESCE(MAX(id), 0) AS cursor FROM signals")
        self._signal_cursor = int(rows[0]["cursor"] if rows else 0)
        # Starting midway through a market could make the funded process act on
        # a later signal after shadow already entered earlier in that window.
        # Block every allowlisted market visible at startup and begin with the
        # next complete 15-minute window instead.
        self._startup_market_tickers = set()
        for asset in self.strategies:
            latest = self.db.query(
                "SELECT market_ticker FROM signals WHERE asset=? AND id<=? "
                "ORDER BY id DESC LIMIT 1",
                (asset, self._signal_cursor),
            )
            if latest:
                self._startup_market_tickers.add(latest[0]["market_ticker"])

    async def _signal_loop(self) -> None:
        while True:
            await asyncio.sleep(SIGNAL_POLL_S)
            self._refresh_kill_switch()
            rows = self.db.query(
                "SELECT id, asset, features FROM signals WHERE id>? "
                "ORDER BY id LIMIT 500",
                (self._signal_cursor,),
            )
            for row in rows:
                self._signal_cursor = max(self._signal_cursor, int(row["id"]))
                if row["asset"] not in self.strategies:
                    continue
                try:
                    snap = FeatureSnapshot.model_validate_json(row["features"])
                except Exception as exc:  # malformed telemetry can never become an order
                    logger.error("dropping invalid live snapshot id=%s: %s", row["id"], exc)
                    continue
                await self._process_snapshot(snap)

    def _position_state(self, ticker: str) -> PositionState:
        rows = self.db.query(
            "SELECT * FROM live_positions WHERE market_ticker=? AND status='open'",
            (ticker,),
        )
        if not rows:
            return PositionState(market_ticker=ticker)
        row = rows[0]
        intent = OrderIntent(row["intent"])
        return PositionState(
            market_ticker=ticker,
            side="yes" if intent is OrderIntent.BUY_YES else "no",
            contracts=row["contracts"], avg_price=row["avg_price"],
            entry_ts=row["entry_ts"],
        )

    def _already_tracked(self, ticker: str) -> bool:
        return bool(self.db.query(
            "SELECT 1 FROM live_orders WHERE market_ticker=? LIMIT 1", (ticker,)
        )) or bool(self.db.query(
            "SELECT 1 FROM live_positions WHERE market_ticker=? LIMIT 1", (ticker,)
        ))

    @staticmethod
    def _opposing_depth(snap: FeatureSnapshot, intent: OrderIntent) -> float | None:
        if intent is OrderIntent.BUY_YES:
            return snap.depth_no_within_2c
        if intent is OrderIntent.BUY_NO:
            return snap.depth_yes_within_2c
        return None

    async def _process_snapshot(self, snap: FeatureSnapshot) -> None:
        if snap.market_ticker in self._startup_market_tickers:
            return
        strategy = self.strategies.get(snap.asset)
        if strategy is None or self._already_tracked(snap.market_ticker):
            return
        position = self._position_state(snap.market_ticker)
        signal = strategy.evaluate(snap, position)
        if signal is None:
            return

        requested = signal.contracts
        if time.time() - snap.ts > MAX_SIGNAL_AGE_S:
            self._record_decision(
                snap, strategy, signal, requested, 0, "stale", "signal older than 5s"
            )
            return
        if not self.reconciliation_ok:
            self._record_decision(
                snap, strategy, signal, requested, 0, "blocked_reconciliation",
                "continuous reconciliation is not clean",
            )
            return

        cfg = self.asset_configs[snap.asset]
        smart = SmartMoneySignal(
            lean=snap.smart_lean or "NEUTRAL", strength=snap.smart_strength or 0.0
        )
        contracts, vetoed, modifier_note = apply_modifier(
            signal.intent, signal.contracts, smart, cfg.smart_money_weight
        )
        if vetoed:
            self._record_decision(
                snap, strategy, signal, requested, 0, "risk_veto", modifier_note
            )
            return
        signal.contracts = contracts
        verdict = self.risk.check(
            snap.asset, signal, snap, position,
            resting_depth_at_price=self._opposing_depth(snap, signal.intent),
            now=snap.ts,
        )
        if not verdict.approved:
            self._record_decision(
                snap, strategy, signal, requested, 0, "risk_veto", verdict.reason
            )
            return
        signal.contracts = verdict.contracts
        decision_id = self._record_decision(
            snap, strategy, signal, requested, verdict.contracts, "approved",
            modifier_note if modifier_note else verdict.reason,
        )

        # Refresh the operator control immediately before the exchange boundary.
        self._refresh_kill_switch()
        if self.risk.kill_switch:
            self._update_decision(decision_id, "risk_veto", "kill switch engaged")
            return
        try:
            response = await self.executor.submit_approved(snap.asset, snap, signal)
        except Exception as exc:
            self.reconciliation_ok = False
            self._update_decision(decision_id, "error", f"submission failed: {exc}")
            raise RuntimeError(
                "funded submission was ambiguous; process requires manual reconciliation"
            ) from exc

        client_order_id = self.executor.client_order_id(
            snap.asset, signal, snap.market_ticker
        )
        rows = self.db.query(
            "SELECT status FROM live_orders WHERE client_order_id=?", (client_order_id,)
        )
        status = rows[0]["status"] if rows else "error"
        self._update_decision(
            decision_id, status, f"exchange order {response.order_id}", client_order_id
        )
        logger.warning(
            "FUNDED %s %s %s contracts=%s price=%.4f status=%s",
            snap.asset, snap.market_ticker, signal.intent.value,
            signal.contracts, signal.limit_price, status,
        )

    def _record_decision(
        self, snap: FeatureSnapshot, strategy: Strategy, signal: StrategySignal,
        requested: float, risk_contracts: float, status: str, reason: str,
    ) -> str:
        decision_id = uuid.uuid4().hex
        self.db.write_now("live_decisions", {
            "decision_id": decision_id, "created_ts": snap.ts, "asset": snap.asset,
            "market_ticker": snap.market_ticker, "strategy": strategy.params_key(),
            "intent": signal.intent.value, "execution": signal.execution,
            "limit_price": signal.limit_price, "requested_contracts": requested,
            "risk_contracts": risk_contracts, "status": status, "reason": reason,
            "client_order_id": None, "snapshot": dump_json(snap.model_dump(mode="json")),
            "updated_ts": time.time(),
        })
        return decision_id

    def _update_decision(
        self, decision_id: str, status: str, reason: str,
        client_order_id: str | None = None,
    ) -> None:
        self.db.write_now_sql(
            "UPDATE live_decisions SET status=?, reason=?, client_order_id=?, "
            "updated_ts=? WHERE decision_id=?",
            (status, reason, client_order_id, time.time(), decision_id),
        )

    # --------------------------------------------------------- reconciliation

    @staticmethod
    def _exchange_positions(payload: dict[str, Any]) -> dict[str, float]:
        rows = payload.get("market_positions") or payload.get("positions") or []
        if not isinstance(rows, list):
            return {}
        positions: dict[str, float] = {}
        for row in rows:
            if not isinstance(row, dict) or not row.get("ticker"):
                continue
            raw = row.get("position_fp", row.get("position", row.get("position_count")))
            if raw is None:
                raise ValueError(f"position quantity missing for {row['ticker']}")
            quantity = float(raw)
            if quantity:
                positions[str(row["ticker"])] = quantity
        return positions

    def _local_positions(self) -> dict[str, float]:
        positions: dict[str, float] = {}
        for row in self.db.query(
            "SELECT market_ticker, intent, contracts FROM live_positions "
            "WHERE status='open'"
        ):
            intent = OrderIntent(row["intent"])
            sign = 1.0 if intent is OrderIntent.BUY_YES else -1.0
            positions[row["market_ticker"]] = sign * float(row["contracts"])
        return positions

    @staticmethod
    def _open_orders(orders: list[Order]) -> set[tuple[str, str]]:
        return {
            (order.client_order_id, order.ticker) for order in orders
            if order.client_order_id and order.ticker
            and order.status.lower() in {"resting", "pending", "open"}
        }

    async def reconcile_once(self) -> bool:
        now = time.time()
        try:
            positions_payload = await self.client.get_positions()
            orders = await self.client.get_orders(status="resting")
            settled = {
                row["market_ticker"] for row in self.db.query(
                    "SELECT market_ticker FROM live_position_outcomes"
                )
            }
            exchange_positions = {
                ticker: quantity
                for ticker, quantity in self._exchange_positions(positions_payload).items()
                if ticker not in settled
            }
            exchange_orders = self._open_orders(orders)
            local_positions = self._local_positions()
            local_unresolved = {
                (row["client_order_id"], row["market_ticker"])
                for row in self.db.query(
                    "SELECT client_order_id, market_ticker FROM live_orders "
                    "WHERE status='submit_unknown'"
                )
            }
            ok = (
                exchange_positions == local_positions
                and exchange_orders == local_unresolved
            )
            detail = {
                "exchange_positions": exchange_positions,
                "local_positions": local_positions,
                "exchange_open_orders": sorted(exchange_orders),
                "local_unresolved_orders": sorted(local_unresolved),
            }
            self.reconciliation_ok = ok
            self.db.write_now("live_reconciliations", {
                "run_ts": now, "status": "ok" if ok else "mismatch",
                "external_positions": dump_json(positions_payload),
                "external_orders": dump_json(
                    [order.model_dump(mode="json") for order in orders]
                ),
                "detail": dump_json(detail),
            })
            if not ok:
                logger.critical("funded reconciliation mismatch: %s", detail)
            return ok
        except Exception as exc:
            self.reconciliation_ok = False
            self.db.write_now("live_reconciliations", {
                "run_ts": now, "status": "error", "external_positions": None,
                "external_orders": None, "detail": dump_json({"error": str(exc)}),
            })
            raise

    async def _reconciliation_loop(self) -> None:
        while True:
            await asyncio.sleep(RECONCILIATION_S)
            self._settle_live_positions()
            if not await self.reconcile_once():
                raise RuntimeError("continuous funded reconciliation mismatch")

    # -------------------------------------------------------------- settlement

    def _settle_live_positions(self) -> None:
        rows = self.db.query(
            "SELECT p.*, s.result, s.recorded_ts FROM live_positions p "
            "JOIN settlements s ON s.market_ticker=p.market_ticker "
            "WHERE p.status='open'"
        )
        for row in rows:
            existing = self.db.query(
                "SELECT 1 FROM live_position_outcomes WHERE market_ticker=?",
                (row["market_ticker"],),
            )
            if existing:
                self.db.write_now_sql(
                    "UPDATE live_positions SET status='settled', updated_ts=? "
                    "WHERE market_ticker=?",
                    (time.time(), row["market_ticker"]),
                )
                continue
            intent = OrderIntent(row["intent"])
            side = "yes" if intent is OrderIntent.BUY_YES else "no"
            gross, net = settle_position(
                side, row["contracts"], row["avg_price"], row["fees"],
                row["result"], DEFAULT_PAYOUT,
            )
            settled_ts = time.time()
            self.db.write_now("live_position_outcomes", {
                "market_ticker": row["market_ticker"],
                "recorded_ts": row["recorded_ts"],
                "settlement_result": row["result"],
                "payout_per_contract": DEFAULT_PAYOUT,
                "pnl_gross": round(gross, 4), "pnl_net": round(net, 4),
                "settled_ts": settled_ts,
            })
            self.db.write_now_sql(
                "UPDATE live_positions SET status='settled', updated_ts=? "
                "WHERE market_ticker=?",
                (settled_ts, row["market_ticker"]),
            )
            self.risk.record_pnl(row["asset"], net, now=settled_ts)
            logger.warning(
                "FUNDED SETTLED %s %s result=%s pnl=%+.2f",
                row["asset"], row["market_ticker"], row["result"], net,
            )

    async def _settlement_loop(self) -> None:
        while True:
            await asyncio.sleep(SETTLEMENT_S)
            self._settle_live_positions()
