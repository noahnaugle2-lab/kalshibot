"""Shadow trading loop: everything the observer does, plus simulated trading.

Pipeline per scan, per asset:

    FeatureSnapshot -> assigned Strategy -> smart-money modifier -> RiskManager
        -> fill simulator against the LIVE recorded book -> position

- SHADOW mode only: this class refuses to start in any other mode. There is
  no code path from here to the Kalshi order API.
- Taker signals cross the latest recorded book (IOC). Maker signals rest and
  fill from actual subsequent trade prints via the conservative queue model;
  unfilled maker orders cancel at the settlement blackout.
- Positions are held to settlement, then scored against the recorded ground
  truth and written to sim_positions / daily_pnl. The RiskManager sees every
  settled PnL, so daily loss halts work exactly as they will live.
- Smart money refreshes per asset once per minute (Layer B Polymarket poll +
  Layer A active patterns) and rides along on every persisted snapshot, so
  the with/without-smart-money comparison is reconstructable from raw rows.
"""

from __future__ import annotations

import asyncio
import logging
import time
import uuid
from dataclasses import dataclass

from kalshibot.config import Mode
from kalshibot.decision.claude_cli import ClaudeCLIRunner
from kalshibot.decision.engine import DecisionEngine, TradeDecision
from kalshibot.features.engine import FeatureSnapshot, Regime
from kalshibot.kalshi.models import OrderIntent
from kalshibot.observer import Observer
from kalshibot.orders.risk import RiskManager, Verdict
from kalshibot.persistence.db import dump_json
from kalshibot.sim.fills import TapeTrade, settle_position, simulate_maker, simulate_taker
from kalshibot.smartmoney import signal as sm_signal
from kalshibot.smartmoney.polymarket import PolymarketClient, live_lean
from kalshibot.strategies.base import PositionState, Strategy, StrategySignal
from kalshibot.strategies.library import build_strategy

logger = logging.getLogger(__name__)

SMART_MONEY_REFRESH_S = 60.0
SETTLE_CHECK_S = 60.0
DEFAULT_PAYOUT = 0.99


@dataclass
class OpenPosition:
    asset: str
    market_ticker: str
    strategy_key: str
    side: str
    contracts: float
    avg_price: float
    fees: float
    entry_ts: float
    entry_regime: str
    reason: str
    run_id: str  # settlement writes to the run that opened the position


@dataclass
class RestingMaker:
    asset: str
    market_ticker: str
    strategy_key: str
    intent: OrderIntent
    price: float
    contracts: float
    placed_ts: float
    last_checked_ts: float


class ShadowTrader(Observer):
    def __init__(self, db_path: str | None = None) -> None:
        super().__init__(db_path)
        if self.settings.mode is not Mode.SHADOW:
            raise RuntimeError(
                f"ShadowTrader only runs in SHADOW mode (MODE={self.settings.mode.value}); "
                "LIVE/DEMO order paths are not implemented in this phase"
            )
        from kalshibot.config import load_risk  # local import to avoid cycles

        self.risk: RiskManager = load_risk()
        self.strategies: dict[str, Strategy] = {}
        self.positions: dict[str, OpenPosition] = {}      # market_ticker -> pos
        self.resting: dict[str, RestingMaker] = {}        # market_ticker -> order
        self.run_ids: dict[str, str] = {}                 # asset -> sim_run id
        from kalshibot.dashboard.api import WSHub
        from kalshibot.monitoring.healthcheck import DeadMansSwitch
        from kalshibot.monitoring.webhooks import WebhookNotifier

        self.smart: dict[str, sm_signal.SmartMoneySignal] = {}
        self.latest_snapshots: dict[str, FeatureSnapshot] = {}
        self.hub = WSHub()
        self.notifier = WebhookNotifier(self.settings.n8n_webhook_base_url)
        self.deadman = DeadMansSwitch(self.settings.healthchecks_ping_url)
        self._loss_limit_notified: set[str] = set()
        self._pm_client = PolymarketClient()
        self._session_start = time.time()

        self.ai_assets: set[str] = set()
        self.decision_engine: DecisionEngine | None = None
        self._ai_pending: set[str] = set()  # markets with a decision in flight
        self._ai_tasks: set[asyncio.Task] = set()

        for asset, cfg in self.asset_configs.items():
            self.risk.paused_assets.discard(asset)
            if cfg.paused:
                self.risk.paused_assets.add(asset)
            if cfg.strategy:
                self.strategies[asset] = build_strategy(cfg.strategy, cfg.strategy_params)
            if getattr(cfg, "ai_enabled", False):
                self.ai_assets.add(asset)

    # ------------------------------------------------------------ lifecycle

    async def start(self) -> None:
        if self.ai_assets:
            provider = self.settings.decision_provider.lower()
            if provider == "api":
                from kalshibot.decision.anthropic_api import AnthropicRunner
                if not self.settings.anthropic_api_key:
                    logger.error("AI mode REFUSED: DECISION_PROVIDER=api but "
                                 "ANTHROPIC_API_KEY unset; falling back to baseline")
                    self.ai_assets = set()
                    runner = None
                else:
                    runner = AnthropicRunner(model=self.settings.decision_model,
                                             api_key=self.settings.anthropic_api_key)
            else:
                runner = ClaudeCLIRunner(model=self.settings.decision_model)
            if runner is not None:
                ok, detail = await runner.health_check()
                if ok:
                    self.decision_engine = DecisionEngine(runner, self.db)
                    logger.info("AI mode on for %s (%s: %s)",
                                sorted(self.ai_assets), provider, detail)
                else:
                    logger.error("AI mode REFUSED, falling back to baseline: %s", detail)
                    self.ai_assets = set()

        for asset, strategy in self.strategies.items():
            run_id = f"shadow-{asset}-{uuid.uuid4().hex[:8]}"
            self.run_ids[asset] = run_id
            strategy_label = strategy.params_key() + (
                "+ai" if asset in self.ai_assets else ""
            )
            self.db.write_now("sim_runs", {
                "run_id": run_id, "created_ts": self._session_start,
                "kind": "shadow", "asset": asset,
                "strategy": strategy_label,
                "params": dump_json(strategy.params),
                "latency_ms": None, "seed": None,
                "tape_start": self._session_start, "tape_end": None,
                "windows": None, "summary": None,
            })
        self._recover_positions()
        self._recover_daily_pnl()
        # safety: lead-lag strategies are inert without BTC's spot feed
        needs_btc = any(
            cfg.enabled and not cfg.paused and cfg.strategy == "cross_asset_lead_lag"
            for cfg in self.asset_configs.values()
        )
        btc = self.asset_configs.get("BTC")
        if needs_btc and (btc is None or not btc.enabled):
            logger.error(
                "cross_asset_lead_lag is active but BTC is disabled — its "
                "trigger (btc_ret_30s) will always be null and no lead-lag "
                "entries can fire. Enable BTC (paused is fine)."
            )
        logger.info(
            "SHADOW trading: %s",
            {a: s.name for a, s in self.strategies.items()} or "no strategies assigned",
        )
        await super().start()

    def _recover_daily_pnl(self) -> None:
        """Seed the risk layer's daily-loss tracker from the DB.

        The tracker is in-memory; without this, every restart re-arms assets
        that already hit their daily loss halt (observed: HYPE blew through
        its $20/day cap across restarts on 2026-07-05).
        """
        day = time.strftime("%Y-%m-%d", time.gmtime())
        self.risk._roll_day()
        for row in self.db.query(
            "SELECT asset, SUM(pnl_net) AS net FROM daily_pnl WHERE date = ? "
            "GROUP BY asset", (day,),
        ):
            self.risk.daily_pnl[row["asset"]] = row["net"] or 0.0
            if (row["net"] or 0) <= -self.risk.config.max_daily_loss_per_asset_usd:
                logger.warning("%s: daily loss limit already hit today (%.2f) — "
                               "halted for the day", row["asset"], row["net"])

    def _recover_positions(self) -> None:
        """Rebuild open positions from fills that never reached settlement.

        Idempotent restart: any shadow fill without a matching sim_positions
        row is still open. It keeps its ORIGINAL run_id so its eventual PnL
        lands with the strategy version that opened it.
        """
        rows = self.db.query(
            "SELECT f.*, r.strategy AS strategy_key FROM sim_fills f "
            "JOIN sim_runs r ON r.run_id = f.run_id "
            "WHERE r.kind = 'shadow' AND NOT EXISTS "
            "(SELECT 1 FROM sim_positions p WHERE p.run_id = f.run_id "
            " AND p.market_ticker = f.market_ticker)",
        )
        from kalshibot.features.engine import regime_for

        for f in rows:
            intent = OrderIntent(f["intent"])
            side = "yes" if intent is OrderIntent.BUY_YES else "no"
            # regime is recoverable: entry ts vs the market's recorded close
            market_row = self.db.query(
                "SELECT close_ts FROM markets WHERE ticker = ?",
                (f["market_ticker"],),
            )
            if market_row and market_row[0]["close_ts"]:
                remaining = market_row[0]["close_ts"] - f["ts"]
                entry_regime = regime_for(max(0.0, remaining)).value
            else:
                entry_regime = "UNKNOWN"
            self.positions[f["market_ticker"]] = OpenPosition(
                asset=f["asset"], market_ticker=f["market_ticker"],
                strategy_key=f["strategy_key"], side=side,
                contracts=f["contracts"], avg_price=f["price"],
                fees=f["fee"], entry_ts=f["ts"],
                entry_regime=entry_regime, reason=f["reason"] or "recovered",
                run_id=f["run_id"],
            )
            logger.info("recovered open position: %s %s %.0f @ %.3f (%s)",
                        f["asset"], side.upper(), f["contracts"], f["price"],
                        f["market_ticker"])

    def extra_tasks(self) -> list:
        from kalshibot.dashboard.api import serve

        return [
            self._settle_loop(),
            self._smart_money_loop(),
            serve(self, port=self.settings.dashboard_port),
            self._ws_push_loop(),
            self.notifier.run(),
            self.deadman.run(self),
            self._nightly_loop(),
        ]

    async def _nightly_loop(self) -> None:
        """Run the nightly evaluation job daily at ~08:15 UTC.

        Runs as a subprocess so a failure (or a slow Claude analysis) can
        never touch the trading loops. Scheduled in-process because launchd/
        cron are TCC-blocked from this repo's location (see README).
        """
        from datetime import datetime, timedelta, timezone

        while True:
            now = datetime.now(timezone.utc)
            target = now.replace(hour=8, minute=15, second=0, microsecond=0)
            if target <= now:
                target += timedelta(days=1)
            await asyncio.sleep((target - now).total_seconds())
            logger.info("nightly job starting")
            try:
                proc = await asyncio.create_subprocess_exec(
                    ".venv/bin/python", "scripts/nightly.py",
                    stdout=asyncio.subprocess.DEVNULL,
                    stderr=asyncio.subprocess.PIPE,
                )
                _, stderr = await asyncio.wait_for(proc.communicate(), timeout=1800)
                if proc.returncode == 0:
                    logger.info("nightly job finished OK")
                else:
                    logger.error("nightly job FAILED (exit %s): %s",
                                 proc.returncode, (stderr or b'').decode()[-800:])
                self.notifier.emit("nightly_report", {"exit_code": proc.returncode})
            except Exception as exc:
                logger.error("nightly job failed: %s", exc)

    async def _ws_push_loop(self) -> None:
        """Push live payloads + status to dashboard sockets."""
        from kalshibot.dashboard.api import _live_payload, _status_payload

        counter = 0
        while True:
            await asyncio.sleep(2.0)
            try:
                for asset in self.recorders:
                    self.hub.publish({
                        "type": "snapshot", "asset": asset,
                        "data": await asyncio.to_thread(_live_payload, self, asset),
                    })
                counter += 1
                if counter % 5 == 0:  # status every ~10s
                    self.hub.publish({
                        "type": "status",
                        "data": await asyncio.to_thread(_status_payload, self),
                    })
            except Exception as exc:
                logger.debug("ws push error: %s", exc)

    # --------------------------------------------------------------- hooks

    def enrich_snapshot(self, asset: str, snap: FeatureSnapshot) -> FeatureSnapshot:
        sm = self.smart.get(asset)
        if sm is not None and sm.lean != "NEUTRAL":
            snap.smart_lean = sm.lean
            snap.smart_strength = sm.strength
        self.latest_snapshots[asset] = snap
        return snap

    def on_snapshot(self, asset: str, snap: FeatureSnapshot) -> None:
        strategy = self.strategies.get(asset)
        if strategy is None:
            return
        self._check_maker_fills(asset, snap)
        position = self._position_state(snap.market_ticker)
        if snap.market_ticker in self.resting:
            return  # one working order per market
        signal = strategy.evaluate(snap, position)
        if signal is None:
            return

        # smart-money confidence modifier (weight 0 during early evaluation)
        weight = self.asset_configs[asset].smart_money_weight
        sm = self.smart.get(asset, sm_signal.SmartMoneySignal.neutral())
        contracts, vetoed, note = sm_signal.apply_modifier(
            signal.intent, signal.contracts, sm, weight
        )
        if vetoed:
            self._record_order(asset, snap, signal, "taker", "smart_veto", 0.0)
            logger.info("%s: %s", asset, note)
            return
        if contracts != signal.contracts:
            signal.reason += f" | {note}"
        signal.contracts = contracts

        # deterministic risk layer — final authority
        recorder = self.recorders[asset]
        depth = self._opposing_depth(snap, signal)
        verdict: Verdict = self.risk.check(
            asset, signal, snap, position, resting_depth_at_price=depth, now=snap.ts
        )
        if not verdict.approved:
            logger.debug("%s: risk veto: %s", asset, verdict.reason)
            return
        signal.contracts = verdict.contracts

        if asset in self.ai_assets and self.decision_engine is not None:
            if snap.market_ticker in self._ai_pending:
                return
            self._ai_pending.add(snap.market_ticker)
            task = asyncio.create_task(self._ai_flow(asset, snap, signal, strategy))
            self._ai_tasks.add(task)
            task.add_done_callback(lambda t: (
                self._ai_tasks.discard(t),
                self._ai_pending.discard(snap.market_ticker),
            ))
            return

        self._execute(asset, snap, signal, strategy.params_key())

    def _execute(self, asset: str, snap: FeatureSnapshot,
                 signal: StrategySignal, strategy_key: str) -> None:
        if signal.execution == "maker":
            self.resting[snap.market_ticker] = RestingMaker(
                asset=asset, market_ticker=snap.market_ticker,
                strategy_key=strategy_key, intent=signal.intent,
                price=signal.limit_price, contracts=signal.contracts,
                placed_ts=snap.ts, last_checked_ts=snap.ts,
            )
            self._record_order(asset, snap, signal, "maker", "resting", 0.0)
            logger.info("%s: resting maker %s %.0f @ %.2f (%s)", asset,
                        signal.intent.value, signal.contracts, signal.limit_price,
                        signal.reason)
            return

        book = self.recorders[asset].latest_book
        if book is None:
            return
        fill = simulate_taker(signal.intent, signal.limit_price, signal.contracts, book)
        if fill.filled < 1:
            self._record_order(asset, snap, signal, "taker", "missed", 0.0)
            logger.info("%s: taker signal missed (book moved): %s", asset, signal.reason)
            return
        status = "filled" if fill.filled >= signal.contracts else "partial"
        self._record_order(asset, snap, signal, "taker", status, fill.filled)
        self._open_position(asset, snap, strategy_key, signal.intent,
                            fill.filled, fill.avg_price or 0.0, fill.total_fee,
                            "taker", signal.reason)

    async def _ai_flow(self, asset: str, snap: FeatureSnapshot,
                       signal: StrategySignal, strategy: Strategy) -> None:
        """Async decision path: never blocks the scan loop.

        The risk layer already approved `signal` (that's what made it a
        qualifying dispatch), but time passes while Claude thinks, so the
        final order re-checks risk against fresh clock/positions and
        re-quotes against the live book (limit semantics: if the book moved
        past the limit, the fill comes back empty and is recorded as missed).
        """
        assert self.decision_engine is not None
        strategy_key = strategy.params_key() + "+ai"
        sm = self.smart.get(asset, sm_signal.SmartMoneySignal.neutral())
        decision, disposition = await self.decision_engine.decide(
            asset, snap, signal, sm, self._position_state(snap.market_ticker),
            self.risk.daily_pnl.get(asset, 0.0),
            {
                "daily_pnl_asset": round(self.risk.daily_pnl.get(asset, 0.0), 2),
                "daily_pnl_global": round(self.risk.global_daily_pnl, 2),
                "max_daily_loss_per_asset": self.risk.config.max_daily_loss_per_asset_usd,
            },
        )
        if disposition == "hold":
            return
        final_signal = signal
        if disposition == "claude" and decision is not None:
            if decision.action == "HOLD":
                logger.info("%s: claude HOLD (%s)", asset, decision.reasoning[:80])
                return
            if decision.action == "SELL":
                logger.info("%s: claude SELL unsupported in v1, holding", asset)
                return
            final_signal = StrategySignal(
                intent=OrderIntent(decision.action),
                contracts=min(decision.size_contracts or signal.contracts,
                              signal.contracts),  # advisory: can downsize only
                limit_price=(decision.limit_price_cents / 100
                             if decision.limit_price_cents else signal.limit_price),
                execution="taker",
                reason=f"claude({decision.confidence:.2f}): {decision.reasoning[:120]}",
            )

        # fresh risk re-check: recompute time state, positions may have changed
        market = self.recorders[asset].current_market
        if market is None or market.ticker != snap.market_ticker:
            return  # window rolled while deciding
        seconds_remaining = (
            max(0.0, market.close_time.timestamp() - time.time())
            if market.close_time else 0.0
        )
        from kalshibot.features.engine import regime_for
        fresh = snap.model_copy(update={
            "seconds_remaining": seconds_remaining,
            "regime": regime_for(seconds_remaining),
        })
        position = self._position_state(snap.market_ticker)
        verdict = self.risk.check(
            asset, final_signal, fresh, position,
            resting_depth_at_price=self._opposing_depth(fresh, final_signal),
        )
        if not verdict.approved:
            logger.info("%s: post-decision risk veto: %s", asset, verdict.reason)
            return
        final_signal.contracts = verdict.contracts
        self._execute(asset, fresh, final_signal, strategy_key)

    def _record_order(self, asset, snap, signal, execution, status, filled) -> None:
        self.db.write_now("sim_orders", {
            "run_id": self.run_ids[asset], "ts": snap.ts, "asset": asset,
            "market_ticker": snap.market_ticker, "intent": signal.intent.value,
            "execution": execution, "limit_price": signal.limit_price,
            "contracts": signal.contracts, "status": status,
            "filled_contracts": filled, "reason": signal.reason,
        })

    def _update_order_status(self, market_ticker: str, status: str, filled: float) -> None:
        rows = self.db.query(
            "SELECT id FROM sim_orders WHERE market_ticker = ? AND execution = 'maker' "
            "AND status = 'resting' ORDER BY ts DESC LIMIT 1", (market_ticker,),
        )
        if rows:
            self.db.write_now_sql(
                "UPDATE sim_orders SET status = ?, filled_contracts = ? WHERE id = ?",
                (status, filled, rows[0]["id"]),
            )

    # -------------------------------------------------------------- fills

    def _open_position(self, asset, snap, strategy_key, intent, contracts,
                       avg_price, fees, liquidity, reason) -> None:
        side = "yes" if intent is OrderIntent.BUY_YES else "no"
        self.positions[snap.market_ticker] = OpenPosition(
            asset=asset, market_ticker=snap.market_ticker,
            strategy_key=strategy_key, side=side, contracts=contracts,
            avg_price=avg_price, fees=fees, entry_ts=snap.ts,
            entry_regime=snap.regime.value, reason=reason,
            run_id=self.run_ids[asset],
        )
        self.db.write_now("sim_fills", {
            "run_id": self.run_ids[asset], "ts": snap.ts, "asset": asset,
            "market_ticker": snap.market_ticker, "intent": intent.value,
            "price": avg_price, "contracts": contracts, "fee": fees,
            "liquidity": liquidity, "reason": reason,
        })
        self.notifier.emit("trade_executed", {
            "asset": asset, "market_ticker": snap.market_ticker,
            "side": side, "contracts": contracts, "avg_price": avg_price,
            "fees": fees, "liquidity": liquidity, "reason": reason,
            "mode": self.settings.mode.value,
        })
        logger.info("%s: OPEN %s %.0f @ %.3f fee %.2f (%s) [%s]",
                    asset, side.upper(), contracts, avg_price, fees, reason, liquidity)

    def _check_maker_fills(self, asset: str, snap: FeatureSnapshot) -> None:
        order = self.resting.get(snap.market_ticker)
        if order is None:
            return
        if snap.regime == Regime.SETTLEMENT:
            logger.info("%s: cancel resting maker at blackout", asset)
            self._update_order_status(snap.market_ticker, "expired", 0.0)
            del self.resting[snap.market_ticker]
            return
        rows = self.db.query(
            "SELECT ts, yes_price, count, taker_side FROM trade_tape "
            "WHERE market_ticker = ? AND ts > ? ORDER BY ts",
            (order.market_ticker, order.last_checked_ts),
        )
        order.last_checked_ts = snap.ts
        if not rows:
            return
        trades = [TapeTrade(r["ts"], r["yes_price"] or 0, r["count"] or 0,
                            r["taker_side"] or "") for r in rows]
        fill = simulate_maker(order.intent, order.price, order.contracts, trades)
        if fill.filled < 1:
            return
        self._update_order_status(snap.market_ticker, "filled", fill.filled)
        del self.resting[snap.market_ticker]
        self._open_position(asset, snap, order.strategy_key, order.intent,
                            fill.filled, order.price, 0.0, "maker",
                            f"maker filled from prints @ {order.price:.2f}")

    def _position_state(self, market_ticker: str) -> PositionState:
        pos = self.positions.get(market_ticker)
        if pos is None:
            return PositionState(market_ticker=market_ticker)
        return PositionState(
            market_ticker=market_ticker, side=pos.side,
            contracts=pos.contracts, avg_price=pos.avg_price, entry_ts=pos.entry_ts,
        )

    @staticmethod
    def _opposing_depth(snap: FeatureSnapshot, signal) -> float | None:
        # depth available to a taker on the side being crossed (near touch)
        if signal.intent is OrderIntent.BUY_YES:
            return snap.depth_no_within_2c
        if signal.intent is OrderIntent.BUY_NO:
            return snap.depth_yes_within_2c
        return None

    # ---------------------------------------------------------- settlement

    async def _settle_loop(self) -> None:
        while True:
            await asyncio.sleep(SETTLE_CHECK_S)
            for ticker, pos in list(self.positions.items()):
                rows = self.db.query(
                    "SELECT result FROM settlements WHERE market_ticker = ? "
                    "AND result IN ('yes','no')", (ticker,),
                )
                if not rows:
                    continue
                result = rows[0]["result"]
                gross, net = settle_position(
                    pos.side, pos.contracts, pos.avg_price, pos.fees,
                    result, DEFAULT_PAYOUT,
                )
                self.db.write_now("sim_positions", {
                    "run_id": pos.run_id, "asset": pos.asset,
                    "market_ticker": ticker, "side": pos.side,
                    "contracts": pos.contracts, "avg_price": pos.avg_price,
                    "fees": pos.fees, "entry_ts": pos.entry_ts,
                    "entry_regime": pos.entry_regime, "result": result,
                    "payout_per_contract": DEFAULT_PAYOUT,
                    "pnl_gross": round(gross, 4), "pnl_net": round(net, 4),
                })
                self._bump_daily_pnl(pos, gross, net)
                self.risk.record_pnl(pos.asset, net)
                del self.positions[ticker]
                self.hub.publish({"type": "settlement", "data": {
                    "asset": pos.asset, "market_ticker": ticker,
                    "result": result, "pnl_net": round(net, 2),
                }})
                self.notifier.emit("settlement", {
                    "asset": pos.asset, "market_ticker": ticker,
                    "result": result, "side": pos.side,
                    "pnl_net": round(net, 2),
                    "day_pnl": round(self.risk.daily_pnl.get(pos.asset, 0.0), 2),
                })
                day_pnl = self.risk.daily_pnl.get(pos.asset, 0.0)
                if (day_pnl <= -self.risk.config.max_daily_loss_per_asset_usd
                        and pos.asset not in self._loss_limit_notified):
                    self._loss_limit_notified.add(pos.asset)
                    self.notifier.emit("daily_loss_limit", {
                        "scope": pos.asset, "day_pnl": round(day_pnl, 2),
                        "limit": self.risk.config.max_daily_loss_per_asset_usd,
                    })
                if self.risk.global_daily_pnl <= -self.risk.config.max_daily_loss_global_usd \
                        and "GLOBAL" not in self._loss_limit_notified:
                    self._loss_limit_notified.add("GLOBAL")
                    self.notifier.emit("daily_loss_limit", {
                        "scope": "GLOBAL",
                        "day_pnl": round(self.risk.global_daily_pnl, 2),
                        "limit": self.risk.config.max_daily_loss_global_usd,
                    })
                logger.info("%s: SETTLED %s %s -> %s, pnl_net %+.2f (day %+.2f)",
                            pos.asset, pos.side.upper(), ticker, result, net,
                            self.risk.daily_pnl.get(pos.asset, 0.0))

    def _bump_daily_pnl(self, pos: OpenPosition, gross: float, net: float) -> None:
        day = time.strftime("%Y-%m-%d", time.gmtime())
        existing = self.db.query(
            "SELECT * FROM daily_pnl WHERE date=? AND asset=? AND strategy=?",
            (day, pos.asset, pos.strategy_key),
        )
        row = dict(existing[0]) if existing else {
            "date": day, "asset": pos.asset, "strategy": pos.strategy_key,
            "trades": 0, "wins": 0, "pnl_gross": 0.0, "pnl_net": 0.0, "fees": 0.0,
        }
        row["trades"] += 1
        row["wins"] += int(gross > 0)
        row["pnl_gross"] = round(row["pnl_gross"] + gross, 4)
        row["pnl_net"] = round(row["pnl_net"] + net, 4)
        row["fees"] = round(row["fees"] + pos.fees, 4)
        row["updated_ts"] = time.time()
        self.db.write_now("daily_pnl", row)

    # --------------------------------------------------------- smart money

    async def _smart_money_loop(self) -> None:
        while True:
            await asyncio.sleep(SMART_MONEY_REFRESH_S)
            for asset, recorder in self.recorders.items():
                market = recorder.current_market
                if market is None or market.close_time is None:
                    self.smart[asset] = sm_signal.SmartMoneySignal.neutral()
                    continue
                close_ts = int(market.close_time.timestamp())
                open_ts = close_ts - 900
                layer_a = None
                try:
                    layer_a = await asyncio.to_thread(
                        sm_signal.layer_a_lean, self.db, asset,
                        market.ticker, open_ts, close_ts,
                    )
                except Exception as exc:
                    logger.warning("%s: layer A lean failed: %s", asset, exc)
                layer_b = None
                try:
                    layer_b = await live_lean(self._pm_client, self.db, asset, close_ts)
                except Exception as exc:
                    logger.debug("%s: layer B lean failed: %s", asset, exc)
                self.smart[asset] = sm_signal.merge(layer_a, layer_b)
