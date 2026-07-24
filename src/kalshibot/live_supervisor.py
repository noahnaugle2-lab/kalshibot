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

from kalshibot.config import Mode, load_risk, load_wallet_consensus
from kalshibot.features.engine import FeatureSnapshot
from kalshibot.kalshi.auth import KalshiSigner
from kalshibot.kalshi.client import KalshiClient, KalshiEnvironment
from kalshibot.kalshi.models import OrderIntent
from kalshibot.observer import Observer
from kalshibot.orders.risk import RiskManager
from kalshibot.persistence.db import dump_json
from kalshibot.sim.fills import DEFAULT_PAYOUT, settle_position, simulate_taker
from kalshibot.smartmoney import signal as sm_signal
from kalshibot.smartmoney.polymarket import (
    PolymarketClient,
    copyable_consensus_lean,
    live_lean,
)
from kalshibot.strategies.base import PositionState, Strategy, StrategySignal
from kalshibot.strategies.library import build_strategy

logger = logging.getLogger(__name__)
SMART_MONEY_REFRESH_S = 60.0
SETTLEMENT_CHECK_S = 60.0


class LiveDryRunSupervisor(Observer):
    """Observe, reconcile, and propose. It cannot submit or cancel an order."""

    def __init__(self, db_path: str | None = None) -> None:
        # The primary shadow trader is the sole tape writer. This companion
        # keeps independent live books for proposal fidelity but persists only
        # reconciliation, proposals, and outcomes.
        super().__init__(db_path, persist_observations=False)
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
        self.wallet_consensus_config = load_wallet_consensus()
        self.latest_snapshots: dict[str, FeatureSnapshot] = {}
        self._wallet_consensus_confirmation: dict[str, tuple[str, int]] = {}
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
        self.latest_snapshots[asset] = snap
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

    def _has_pending_proposal(self, ticker: str) -> bool:
        """One hypothetical entry per market, matching the live entry policy."""
        rows = self.db.query(
            "SELECT 1 FROM live_proposals p JOIN live_proposal_outcomes o "
            "ON o.proposal_id=p.proposal_id WHERE p.market_ticker=? "
            "AND o.outcome_status='pending' LIMIT 1", (ticker,)
        )
        return bool(rows)

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
        if self._has_pending_proposal(snap.market_ticker):
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
        proposal_id = uuid.uuid4().hex
        self.db.write_now("live_proposals", {
            "proposal_id": proposal_id, "created_ts": snap.ts, "asset": asset,
            "market_ticker": snap.market_ticker, "strategy": strategy.params_key(),
            "intent": signal.intent.value, "execution": signal.execution,
            "limit_price": signal.limit_price, "requested_contracts": signal.contracts,
            "risk_contracts": risk_contracts, "status": status, "reason": reason,
            "snapshot": dump_json(snap.model_dump(mode="json")),
        })
        self._record_fill_assumption(proposal_id, asset, snap, signal, status, risk_contracts)

    def _record_fill_assumption(
        self, proposal_id: str, asset: str, snap: FeatureSnapshot, signal,
        proposal_status: str, risk_contracts: float,
    ) -> None:
        """Persist the exact taker-fill model before the market can move."""
        outcome_status = "not_approved"
        filled = fees = 0.0
        avg_price = None
        assumption = "proposal was not risk-approved"
        if proposal_status == "dry_run":
            if signal.execution != "taker":
                outcome_status = "unsupported"
                assumption = "maker execution is not supported by the live executor"
            else:
                book = self.recorders[asset].latest_book
                if book is None:
                    outcome_status = "unfilled"
                    assumption = "no captured order book at proposal time"
                else:
                    fill = simulate_taker(signal.intent, signal.limit_price, risk_contracts, book)
                    filled = fill.filled
                    avg_price = fill.avg_price
                    fees = fill.total_fee
                    outcome_status = "pending" if filled >= 1 else "unfilled"
                    assumption = "IOC taker simulation against captured order book"
        self.db.write_now("live_proposal_outcomes", {
            "proposal_id": proposal_id, "recorded_ts": snap.ts,
            "outcome_status": outcome_status, "expected_filled": filled,
            "expected_avg_price": avg_price, "expected_fees": fees,
            "fill_assumption": assumption,
        })

    def extra_tasks(self) -> list:
        tasks = [self._smart_money_loop(), self._proposal_settle_loop()]
        if self.wallet_consensus_config.enabled:
            tasks.append(self._wallet_consensus_loop())
        return tasks

    async def _proposal_settle_loop(self) -> None:
        while True:
            await asyncio.sleep(SETTLEMENT_CHECK_S)
            self._settle_pending_proposals()

    def _settle_pending_proposals(self) -> None:
        rows = self.db.query(
            "SELECT p.proposal_id, p.intent, o.expected_filled, o.expected_avg_price, "
            "o.expected_fees, s.result FROM live_proposals p "
            "JOIN live_proposal_outcomes o ON o.proposal_id=p.proposal_id "
            "JOIN settlements s ON s.market_ticker=p.market_ticker "
            "WHERE o.outcome_status='pending'"
        )
        for row in rows:
            try:
                intent = OrderIntent(row["intent"])
                side = "yes" if intent is OrderIntent.BUY_YES else "no"
                gross, net = settle_position(
                    side, row["expected_filled"], row["expected_avg_price"],
                    row["expected_fees"], row["result"], DEFAULT_PAYOUT,
                )
                self.db.write_now_sql(
                    "UPDATE live_proposal_outcomes SET outcome_status='settled', "
                    "settlement_result=?, payout_per_contract=?, hypothetical_pnl_gross=?, "
                    "hypothetical_pnl_net=?, settled_ts=? WHERE proposal_id=?",
                    (row["result"], DEFAULT_PAYOUT, gross, net, time.time(), row["proposal_id"]),
                )
            except Exception as exc:
                logger.exception("failed to settle dry-run proposal %s: %s", row["proposal_id"], exc)

        wallet_rows = self.db.query(
            "SELECT p.proposal_id, p.intent, o.expected_filled, o.expected_avg_price, "
            "o.expected_fees, s.result FROM wallet_consensus_proposals p "
            "JOIN wallet_consensus_outcomes o ON o.proposal_id=p.proposal_id "
            "JOIN settlements s ON s.market_ticker=p.market_ticker "
            "WHERE o.outcome_status='pending'"
        )
        for row in wallet_rows:
            try:
                intent = OrderIntent(row["intent"])
                side = "yes" if intent is OrderIntent.BUY_YES else "no"
                gross, net = settle_position(
                    side, row["expected_filled"], row["expected_avg_price"],
                    row["expected_fees"], row["result"], DEFAULT_PAYOUT,
                )
                self.db.write_now_sql(
                    "UPDATE wallet_consensus_outcomes SET outcome_status='settled', "
                    "settlement_result=?, payout_per_contract=?, hypothetical_pnl_gross=?, "
                    "hypothetical_pnl_net=?, settled_ts=? WHERE proposal_id=?",
                    (row["result"], DEFAULT_PAYOUT, gross, net, time.time(), row["proposal_id"]),
                )
            except Exception as exc:
                logger.exception(
                    "failed to settle wallet-consensus proposal %s: %s",
                    row["proposal_id"], exc,
                )

        counterfactual_rows = self.db.query(
            "SELECT p.proposal_id, p.intent, o.expected_filled, o.expected_avg_price, "
            "o.expected_fees, s.result FROM wallet_consensus_counterfactuals p "
            "JOIN wallet_consensus_counterfactual_outcomes o "
            "ON o.proposal_id=p.proposal_id "
            "JOIN settlements s ON s.market_ticker=p.market_ticker "
            "WHERE o.outcome_status='pending'"
        )
        for row in counterfactual_rows:
            try:
                intent = OrderIntent(row["intent"])
                side = "yes" if intent is OrderIntent.BUY_YES else "no"
                gross, net = settle_position(
                    side, row["expected_filled"], row["expected_avg_price"],
                    row["expected_fees"], row["result"], DEFAULT_PAYOUT,
                )
                self.db.write_now_sql(
                    "UPDATE wallet_consensus_counterfactual_outcomes "
                    "SET outcome_status='settled', settlement_result=?, "
                    "payout_per_contract=?, hypothetical_pnl_gross=?, "
                    "hypothetical_pnl_net=?, settled_ts=? WHERE proposal_id=?",
                    (row["result"], DEFAULT_PAYOUT, gross, net, time.time(), row["proposal_id"]),
                )
            except Exception as exc:
                logger.exception(
                    "failed to settle wallet-only counterfactual %s: %s",
                    row["proposal_id"], exc,
                )

    def _wallet_consensus_proposed(self, asset: str, ticker: str) -> bool:
        return bool(self.db.query(
            "SELECT 1 FROM wallet_consensus_proposals "
            "WHERE asset=? AND market_ticker=? LIMIT 1", (asset, ticker),
        ))

    def _wallet_counterfactual_proposed(self, asset: str, ticker: str) -> bool:
        return bool(self.db.query(
            "SELECT 1 FROM wallet_consensus_counterfactuals "
            "WHERE asset=? AND market_ticker=? LIMIT 1", (asset, ticker),
        ))

    def _record_wallet_consensus_decision(
        self, asset: str, ticker: str, result: dict, *, arm: str,
        status: str, reason: str, edge: float | None, price: float | None,
        evaluated_ts: float,
    ) -> None:
        self.db.write_now("wallet_consensus_decisions", {
            "decision_id": f"{asset}:{ticker}:{arm}",
            "evaluated_ts": evaluated_ts, "asset": asset,
            "market_ticker": ticker, "condition_id": result.get("condition_id"),
            "arm": arm, "status": status, "reason": reason,
            "lean": result["candidate_lean"],
            "active_wallets": result["wallets"],
            "effective_wallets": result["effective_wallets"],
            "dominant_share": result["dominant_share"],
            "edge_net": edge, "limit_price": price,
        })

    @staticmethod
    def _wallet_consensus_quote(
        result: dict, snap: FeatureSnapshot,
    ) -> tuple[OrderIntent, float | None, float | None]:
        if result["candidate_lean"] == "UP":
            return OrderIntent.BUY_YES, snap.edge_yes_net, snap.yes_ask
        price = round(1 - snap.yes_bid, 4) if snap.yes_bid is not None else None
        return OrderIntent.BUY_NO, snap.edge_no_net, price

    def _record_wallet_consensus_observation(
        self, asset: str, ticker: str, close_ts: int, result: dict, now: float,
    ) -> None:
        cfg = self.wallet_consensus_config
        self.db.write_now("wallet_consensus_observations", {
            "ts": now, "asset": asset, "market_ticker": ticker,
            "condition_id": result.get("condition_id"),
            "elapsed_s": max(0.0, now - (close_ts - 900)),
            "top_n": cfg.top_n, "active_wallets": result.get("wallets", 0),
            "effective_wallets": result.get("effective_wallets", 0.0),
            "weighted_up": result.get("weighted_up", 0.0),
            "weighted_down": result.get("weighted_down", 0.0),
            "dominant_share": result.get("dominant_share"),
            "lean": result.get("candidate_lean", result.get("lean", "NEUTRAL")),
            "eligible": int(bool(result.get("eligible"))),
            "reason": result.get("reason", "unknown"),
        })

    def _record_wallet_consensus_proposal(
        self, asset: str, ticker: str, result: dict, snap: FeatureSnapshot,
    ) -> None:
        cfg = self.wallet_consensus_config
        lean = result["candidate_lean"]
        intent, edge, price = self._wallet_consensus_quote(result, snap)
        if edge is None or price is None or edge < cfg.edge_threshold:
            self._record_wallet_consensus_decision(
                asset, ticker, result, arm="consensus_plus_edge",
                status="edge_veto",
                reason=(
                    "missing quote or model edge"
                    if edge is None or price is None
                    else f"edge {edge:.4f} < {cfg.edge_threshold:.4f}"
                ),
                edge=edge, price=price, evaluated_ts=snap.ts,
            )
            return
        signal = StrategySignal(
            intent=intent, contracts=cfg.contracts, limit_price=price,
            execution="taker",
            reason=(
                f"wallet consensus {lean} share={result['dominant_share']:.3f} "
                f"active={result['wallets']} edge={edge:.3f}"
            ),
        )
        verdict = self.risk.check(
            asset, signal, snap, self._position_state(ticker),
            resting_depth_at_price=self._opposing_depth(snap, intent), now=snap.ts,
        )
        if not verdict.approved:
            self._record_wallet_consensus_decision(
                asset, ticker, result, arm="consensus_plus_edge",
                status="risk_veto", reason=verdict.reason,
                edge=edge, price=price, evaluated_ts=snap.ts,
            )
            return
        book = self.recorders[asset].latest_book
        if book is None:
            self._record_wallet_consensus_decision(
                asset, ticker, result, arm="consensus_plus_edge",
                status="no_book", reason="no captured Kalshi order book",
                edge=edge, price=price, evaluated_ts=snap.ts,
            )
            return
        fill = simulate_taker(intent, price, verdict.contracts, book)
        proposal_id = uuid.uuid4().hex
        status = "dry_run" if fill.filled >= 1 else "unfilled"
        self.db.write_now("wallet_consensus_proposals", {
            "proposal_id": proposal_id, "created_ts": snap.ts, "asset": asset,
            "market_ticker": ticker, "condition_id": result.get("condition_id"),
            "lean": lean, "intent": intent.value,
            "active_wallets": result["wallets"],
            "effective_wallets": result["effective_wallets"],
            "dominant_share": result["dominant_share"],
            "weighted_up": result["weighted_up"],
            "weighted_down": result["weighted_down"],
            "edge_net": edge, "limit_price": price,
            "requested_contracts": verdict.contracts, "status": status,
            "reason": signal.reason,
            "snapshot": dump_json(snap.model_dump(mode="json")),
        })
        self.db.write_now("wallet_consensus_outcomes", {
            "proposal_id": proposal_id, "recorded_ts": snap.ts,
            "outcome_status": "pending" if fill.filled >= 1 else "unfilled",
            "expected_filled": fill.filled, "expected_avg_price": fill.avg_price,
            "expected_fees": fill.total_fee,
            "fill_assumption": "one-contract IOC simulation against captured Kalshi book",
        })
        self._record_wallet_consensus_decision(
            asset, ticker, result, arm="consensus_plus_edge",
            status="proposed" if fill.filled >= 1 else "unfilled",
            reason=signal.reason if fill.filled >= 1 else "captured book did not fill IOC",
            edge=edge, price=price, evaluated_ts=snap.ts,
        )

    def _record_wallet_only_counterfactual(
        self, asset: str, ticker: str, result: dict, snap: FeatureSnapshot,
    ) -> None:
        """Score wallet consensus without requiring agreement from our model.

        This path writes only dedicated counterfactual tables and never calls
        RiskManager or an order-capable client, so paused assets remain safe to
        study.
        """
        cfg = self.wallet_consensus_config
        lean = result["candidate_lean"]
        intent, edge, price = self._wallet_consensus_quote(result, snap)
        if price is None:
            self._record_wallet_consensus_decision(
                asset, ticker, result, arm="wallet_only", status="no_quote",
                reason="missing Kalshi quote", edge=edge, price=price,
                evaluated_ts=snap.ts,
            )
            return
        if price > cfg.maximum_price:
            self._record_wallet_consensus_decision(
                asset, ticker, result, arm="wallet_only", status="price_veto",
                reason=f"price {price:.4f} > {cfg.maximum_price:.4f}",
                edge=edge, price=price, evaluated_ts=snap.ts,
            )
            return
        book = self.recorders[asset].latest_book
        if book is None:
            self._record_wallet_consensus_decision(
                asset, ticker, result, arm="wallet_only", status="no_book",
                reason="no captured Kalshi order book", edge=edge, price=price,
                evaluated_ts=snap.ts,
            )
            return
        fill = simulate_taker(intent, price, cfg.contracts, book)
        proposal_id = uuid.uuid4().hex
        status = "dry_run" if fill.filled >= 1 else "unfilled"
        reason = (
            f"wallet-only {lean} share={result['dominant_share']:.3f} "
            f"active={result['wallets']} model_edge={edge}"
        )
        self.db.write_now("wallet_consensus_counterfactuals", {
            "proposal_id": proposal_id, "created_ts": snap.ts, "asset": asset,
            "market_ticker": ticker, "condition_id": result.get("condition_id"),
            "lean": lean, "intent": intent.value,
            "active_wallets": result["wallets"],
            "effective_wallets": result["effective_wallets"],
            "dominant_share": result["dominant_share"],
            "weighted_up": result["weighted_up"],
            "weighted_down": result["weighted_down"],
            "model_edge_net": edge, "limit_price": price,
            "requested_contracts": cfg.contracts, "status": status,
            "reason": reason, "snapshot": dump_json(snap.model_dump(mode="json")),
        })
        self.db.write_now("wallet_consensus_counterfactual_outcomes", {
            "proposal_id": proposal_id, "recorded_ts": snap.ts,
            "outcome_status": "pending" if fill.filled >= 1 else "unfilled",
            "expected_filled": fill.filled, "expected_avg_price": fill.avg_price,
            "expected_fees": fill.total_fee,
            "fill_assumption": "wallet-only one-contract IOC against captured Kalshi book",
        })
        self._record_wallet_consensus_decision(
            asset, ticker, result, arm="wallet_only",
            status="proposed" if fill.filled >= 1 else "unfilled",
            reason=reason if fill.filled >= 1 else "captured book did not fill IOC",
            edge=edge, price=price, evaluated_ts=snap.ts,
        )

    async def _wallet_consensus_loop(self) -> None:
        cfg = self.wallet_consensus_config
        while True:
            await asyncio.sleep(cfg.refresh_seconds)
            now = time.time()
            for asset in cfg.assets:
                recorder = self.recorders.get(asset)
                if recorder is None or recorder.current_market is None:
                    continue
                market = recorder.current_market
                if market.close_time is None:
                    continue
                close_ts = int(market.close_time.timestamp())
                elapsed = now - (close_ts - 900)
                if elapsed < 0 or elapsed > cfg.maximum_entry_seconds:
                    continue
                try:
                    result = await copyable_consensus_lean(
                        self._pm_client, self.db, asset, close_ts,
                        top_n=cfg.top_n,
                        maximum_entry_seconds=cfg.maximum_entry_seconds,
                        minimum_active_wallets=cfg.minimum_active_wallets,
                        minimum_effective_wallets=cfg.minimum_effective_wallets,
                        minimum_dominant_share=cfg.minimum_dominant_share,
                        observed_ts=now,
                    )
                except Exception as exc:
                    logger.warning("%s: wallet consensus failed: %s", asset, exc)
                    continue
                self._record_wallet_consensus_observation(
                    asset, market.ticker, close_ts, result, now,
                )
                key = f"{asset}:{market.ticker}"
                if not result.get("eligible"):
                    self._wallet_consensus_confirmation.pop(key, None)
                    continue
                lean = result["candidate_lean"]
                previous_lean, previous_n = self._wallet_consensus_confirmation.get(
                    key, (lean, 0)
                )
                count = previous_n + 1 if previous_lean == lean else 1
                self._wallet_consensus_confirmation[key] = (lean, count)
                if count < cfg.confirmation_observations:
                    continue
                snap = self.latest_snapshots.get(asset)
                if snap is None or snap.market_ticker != market.ticker:
                    continue
                if not self._wallet_counterfactual_proposed(asset, market.ticker):
                    self._record_wallet_only_counterfactual(
                        asset, market.ticker, result, snap,
                    )
                if not self._wallet_consensus_proposed(asset, market.ticker):
                    self._record_wallet_consensus_proposal(
                        asset, market.ticker, result, snap,
                    )

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
