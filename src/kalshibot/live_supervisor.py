"""Production-path dry-run supervisor, with no exchange order path.

The process intentionally keeps ``MODE=SHADOW``. It observes the same market
data and uses the same strategy, smart-signal modifier, persisted kill switch,
and deterministic risk layer as the shadow trader, but records proposals in
``live_proposals`` only. It never imports or calls ``LiveTrader``.
"""

from __future__ import annotations

import asyncio
import json
import logging
import time
import uuid
from typing import Any

from kalshibot.config import Mode, load_risk
from kalshibot.features.engine import FeatureSnapshot
from kalshibot.kalshi.auth import KalshiSigner
from kalshibot.kalshi.client import KalshiClient, KalshiEnvironment
from kalshibot.kalshi.models import OrderIntent
from kalshibot.observer import Observer
from kalshibot.orders.risk import RiskManager
from kalshibot.persistence.db import dump_json
from kalshibot.smartmoney import signal as sm_signal
from kalshibot.smartmoney.polymarket import PolymarketClient, live_lean
from kalshibot.strategies.base import PositionState, Strategy
from kalshibot.strategies.library import build_strategy

logger = logging.getLogger(__name__)
SMART_MONEY_REFRESH_S = 60.0


class LiveDryRunSupervisor(Observer):
    """Observe, reconcile, and propose. It cannot submit or cancel an order."""

    def __init__(self, db_path: str | None = None) -> None:
        super().__init__(db_path)
        if self.settings.mode is not Mode.SHADOW:
            raise RuntimeError("live dry-run supervisor requires MODE=SHADOW")
        if not self.settings.live_dry_run_enabled:
            raise RuntimeError("LIVE_DRY_RUN_ENABLED is not true")
        if not self.settings.kalshi_key_id or not self.settings.kalshi_private_key_path:
            raise RuntimeError("production read credentials are required for reconciliation")

        # Replace Observer's public client before any task has started. This
        # client is authenticated for GET reconciliation, but this class has
        # no reference to an order-submission method.
        signer = KalshiSigner.from_pem_file(
            self.settings.kalshi_key_id, self.settings.kalshi_private_key_path
        )
        self._unauthenticated_client = self.client
        self.client = KalshiClient(KalshiEnvironment.PROD, signer=signer)
        self.risk: RiskManager = load_risk()
        self.strategies: dict[str, Strategy] = {}
        self.smart: dict[str, sm_signal.SmartMoneySignal] = {}
        self._pm_client = PolymarketClient()
        self.reconciliation_ok = False
        for asset, cfg in self.asset_configs.items():
            if cfg.paused:
                self.risk.paused_assets.add(asset)
            if cfg.strategy:
                self.strategies[asset] = build_strategy(cfg.strategy, cfg.strategy_params)
        self._recover_kill_switch()

    async def start(self) -> None:
        # Observer constructs a public client. It was never used, but close it
        # before starting so this companion process owns exactly one session.
        await self._unauthenticated_client.close()
        await self.reconcile_startup()
        if not self.reconciliation_ok:
            raise RuntimeError("live dry-run reconciliation mismatch; refusing proposals")
        logger.info("LIVE DRY RUN: proposals only, no order-capable code path")
        await super().start()

    def _recover_kill_switch(self) -> None:
        rows = self.db.query(
            "SELECT changes FROM config_overrides WHERE scope='control:kill_switch' "
            "ORDER BY id DESC LIMIT 1"
        )
        if not rows:
            return
        try:
            self.risk.kill_switch = bool(json.loads(rows[0]["changes"])["engaged"])
        except (KeyError, TypeError, ValueError, json.JSONDecodeError):
            logger.exception("invalid persisted kill-switch state; defaulting to engaged")
            self.risk.kill_switch = True

    @staticmethod
    def _position_tickers(payload: dict[str, Any]) -> set[str]:
        rows = payload.get("market_positions") or payload.get("positions") or []
        if not isinstance(rows, list):
            return set()
        return {
            str(row.get("ticker")) for row in rows
            if isinstance(row, dict) and row.get("ticker")
            and float(row.get("position", row.get("position_count", 1)) or 0) != 0
        }

    async def reconcile_startup(self) -> None:
        """Reconcile exchange state, block on any discrepancy, and audit it."""
        now = time.time()
        try:
            positions_payload = await self.client.get_positions()
            orders = await self.client.get_orders()
            exchange_positions = self._position_tickers(positions_payload)
            exchange_orders = {o.ticker for o in orders if getattr(o, "ticker", "")}
            local_positions = {
                r["market_ticker"] for r in self.db.query(
                    "SELECT market_ticker FROM live_positions WHERE status='open'"
                )
            }
            local_orders = {
                r["market_ticker"] for r in self.db.query(
                    "SELECT market_ticker FROM live_orders WHERE status='submit_unknown'"
                )
            }
            ok = exchange_positions == local_positions and exchange_orders == local_orders
            detail = {
                "exchange_position_tickers": sorted(exchange_positions),
                "local_position_tickers": sorted(local_positions),
                "exchange_order_tickers": sorted(exchange_orders),
                "local_unresolved_order_tickers": sorted(local_orders),
            }
            self.reconciliation_ok = ok
            self.db.write_now("live_reconciliations", {
                "run_ts": now, "status": "ok" if ok else "mismatch",
                "external_positions": dump_json(positions_payload),
                "external_orders": dump_json([o.model_dump(mode="json") for o in orders]),
                "detail": dump_json(detail),
            })
            if not ok:
                logger.error("live dry-run reconciliation mismatch: %s", detail)
        except Exception as exc:
            self.reconciliation_ok = False
            self.db.write_now("live_reconciliations", {
                "run_ts": now, "status": "error", "external_positions": None,
                "external_orders": None, "detail": dump_json({"error": str(exc)}),
            })
            raise

    def enrich_snapshot(self, asset: str, snap: FeatureSnapshot) -> FeatureSnapshot:
        sm = self.smart.get(asset)
        if sm is not None and sm.lean != "NEUTRAL":
            snap.smart_lean = sm.lean
            snap.smart_strength = sm.strength
        return snap

    def _position_state(self, ticker: str) -> PositionState:
        rows = self.db.query(
            "SELECT * FROM live_positions WHERE market_ticker=? AND status='open'", (ticker,)
        )
        if not rows:
            return PositionState(market_ticker=ticker)
        row = rows[0]
        intent = OrderIntent(row["intent"])
        return PositionState(
            market_ticker=ticker, side="yes" if intent is OrderIntent.BUY_YES else "no",
            contracts=row["contracts"], avg_price=row["avg_price"], entry_ts=row["entry_ts"],
        )

    @staticmethod
    def _opposing_depth(snap: FeatureSnapshot, intent: OrderIntent) -> float | None:
        if intent is OrderIntent.BUY_YES:
            return snap.depth_no_within_2c
        if intent is OrderIntent.BUY_NO:
            return snap.depth_yes_within_2c
        return None

    def on_snapshot(self, asset: str, snap: FeatureSnapshot) -> None:
        strategy = self.strategies.get(asset)
        if strategy is None:
            return
        signal = strategy.evaluate(snap, self._position_state(snap.market_ticker))
        if signal is None:
            return
        status = "blocked_reconciliation"
        reason = "startup reconciliation has not passed"
        risk_contracts = 0.0
        if self.reconciliation_ok:
            sm = self.smart.get(asset, sm_signal.SmartMoneySignal.neutral())
            contracts, vetoed, note = sm_signal.apply_modifier(
                signal.intent, signal.contracts, sm, self.asset_configs[asset].smart_money_weight
            )
            signal.contracts = contracts
            if vetoed:
                reason = note
                status = "risk_veto"
            else:
                verdict = self.risk.check(
                    asset, signal, snap, self._position_state(snap.market_ticker),
                    resting_depth_at_price=self._opposing_depth(snap, signal.intent), now=snap.ts,
                )
                risk_contracts = verdict.contracts
                status = "dry_run" if verdict.approved else "risk_veto"
                reason = note if verdict.approved and note else verdict.reason
        self.db.write_now("live_proposals", {
            "proposal_id": uuid.uuid4().hex, "created_ts": snap.ts, "asset": asset,
            "market_ticker": snap.market_ticker, "strategy": strategy.params_key(),
            "intent": signal.intent.value, "execution": signal.execution,
            "limit_price": signal.limit_price, "requested_contracts": signal.contracts,
            "risk_contracts": risk_contracts, "status": status, "reason": reason,
            "snapshot": dump_json(snap.model_dump(mode="json")),
        })

    def extra_tasks(self) -> list:
        return [self._smart_money_loop()]

    async def _smart_money_loop(self) -> None:
        while True:
            await asyncio.sleep(SMART_MONEY_REFRESH_S)
            for asset, recorder in self.recorders.items():
                market = recorder.current_market
                if market is None or market.close_time is None:
                    self.smart[asset] = sm_signal.SmartMoneySignal.neutral()
                    continue
                close_ts = int(market.close_time.timestamp())
                try:
                    layer_a = await asyncio.to_thread(
                        sm_signal.layer_a_lean, self.db, asset, market.ticker, close_ts - 900, close_ts
                    )
                except Exception as exc:
                    logger.warning("%s: layer A lean failed: %s", asset, exc)
                    layer_a = None
                try:
                    layer_b = await live_lean(self._pm_client, self.db, asset, close_ts)
                except Exception as exc:
                    logger.debug("%s: layer B lean failed: %s", asset, exc)
                    layer_b = None
                self.smart[asset] = sm_signal.merge(layer_a, layer_b)
