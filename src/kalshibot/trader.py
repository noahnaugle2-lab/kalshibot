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
from kalshibot.features.engine import FeatureSnapshot, Regime
from kalshibot.kalshi.models import OrderIntent
from kalshibot.observer import Observer
from kalshibot.orders.risk import RiskManager, Verdict
from kalshibot.persistence.db import dump_json
from kalshibot.sim.fills import TapeTrade, settle_position, simulate_maker, simulate_taker
from kalshibot.smartmoney import signal as sm_signal
from kalshibot.smartmoney.polymarket import PolymarketClient, live_lean
from kalshibot.strategies.base import PositionState, Strategy
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
        self.smart: dict[str, sm_signal.SmartMoneySignal] = {}
        self._pm_client = PolymarketClient()
        self._session_start = time.time()

        for asset, cfg in self.asset_configs.items():
            self.risk.paused_assets.discard(asset)
            if cfg.paused:
                self.risk.paused_assets.add(asset)
            if cfg.strategy:
                self.strategies[asset] = build_strategy(cfg.strategy, cfg.strategy_params)

    # ------------------------------------------------------------ lifecycle

    async def start(self) -> None:
        for asset, strategy in self.strategies.items():
            run_id = f"shadow-{asset}-{uuid.uuid4().hex[:8]}"
            self.run_ids[asset] = run_id
            self.db.write_now("sim_runs", {
                "run_id": run_id, "created_ts": self._session_start,
                "kind": "shadow", "asset": asset,
                "strategy": strategy.params_key(),
                "params": dump_json(strategy.params),
                "latency_ms": None, "seed": None,
                "tape_start": self._session_start, "tape_end": None,
                "windows": None, "summary": None,
            })
        self._recover_positions()
        logger.info(
            "SHADOW trading: %s",
            {a: s.name for a, s in self.strategies.items()} or "no strategies assigned",
        )
        await super().start()

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
        for f in rows:
            intent = OrderIntent(f["intent"])
            side = "yes" if intent is OrderIntent.BUY_YES else "no"
            self.positions[f["market_ticker"]] = OpenPosition(
                asset=f["asset"], market_ticker=f["market_ticker"],
                strategy_key=f["strategy_key"], side=side,
                contracts=f["contracts"], avg_price=f["price"],
                fees=f["fee"], entry_ts=f["ts"],
                entry_regime="UNKNOWN", reason=f["reason"] or "recovered",
                run_id=f["run_id"],
            )
            logger.info("recovered open position: %s %s %.0f @ %.3f (%s)",
                        f["asset"], side.upper(), f["contracts"], f["price"],
                        f["market_ticker"])

    def extra_tasks(self) -> list:
        return [self._settle_loop(), self._smart_money_loop()]

    # --------------------------------------------------------------- hooks

    def enrich_snapshot(self, asset: str, snap: FeatureSnapshot) -> FeatureSnapshot:
        sm = self.smart.get(asset)
        if sm is not None and sm.lean != "NEUTRAL":
            snap.smart_lean = sm.lean
            snap.smart_strength = sm.strength
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
            logger.info("%s: %s", asset, note)
            return
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

        if signal.execution == "maker":
            self.resting[snap.market_ticker] = RestingMaker(
                asset=asset, market_ticker=snap.market_ticker,
                strategy_key=strategy.params_key(), intent=signal.intent,
                price=signal.limit_price, contracts=signal.contracts,
                placed_ts=snap.ts, last_checked_ts=snap.ts,
            )
            self._record_order(asset, snap, signal, "maker", "resting", 0.0)
            logger.info("%s: resting maker %s %.0f @ %.2f (%s)", asset,
                        signal.intent.value, signal.contracts, signal.limit_price,
                        signal.reason)
            return

        book = recorder.latest_book
        if book is None:
            return
        fill = simulate_taker(signal.intent, signal.limit_price, signal.contracts, book)
        if fill.filled < 1:
            self._record_order(asset, snap, signal, "taker", "missed", 0.0)
            logger.info("%s: taker signal missed (book moved): %s", asset, signal.reason)
            return
        status = "filled" if fill.filled >= signal.contracts else "partial"
        self._record_order(asset, snap, signal, "taker", status, fill.filled)
        self._open_position(asset, snap, strategy.params_key(), signal.intent,
                            fill.filled, fill.avg_price or 0.0, fill.total_fee,
                            "taker", signal.reason)

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
