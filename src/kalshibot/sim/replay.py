"""Replay engine: run any Strategy against recorded tape, deterministically.

For each settled window of an asset, the engine walks the recorded book
snapshots in time order. At every snapshot it rebuilds the same
FeatureSnapshot the live loop would have computed (same TickWindow, same
compute_snapshot — shared code, no divergence), hands it to the strategy,
and executes signals with the shared fill simulator against the book as it
stood one latency step later. Positions are held to settlement and scored
against the recorded ground truth.

Determinism: inputs are DB rows ordered by (ts, id); the engine holds no
wall-clock reads; `seed` feeds an RNG reserved for strategies that request
randomness. Same tape + same params + same seed => identical results.

Fidelity caveats (documented, deliberate):
- spot ticks are recorded at 1s downsample, so momentum/vol values can
  differ marginally from what the live loop computed from full-rate ticks;
- signal latency quantizes to the next recorded book snapshot (~2s grid),
  which is CONSERVATIVE (never fills on a better book than the one that
  actually existed after the delay).
"""

from __future__ import annotations

import json
import logging
import random
import time
import uuid
from bisect import bisect_right
from dataclasses import dataclass, field
from datetime import datetime, timezone
from typing import Sequence

from pydantic import BaseModel

from kalshibot.features.engine import compute_snapshot
from kalshibot.features.window import TickWindow
from kalshibot.kalshi.models import Market, Orderbook, OrderbookLevel, OrderIntent
from kalshibot.persistence.db import Database, dump_json
from kalshibot.sim.fills import settle_position, simulate_taker
from kalshibot.strategies.base import PositionState, Strategy

logger = logging.getLogger(__name__)

DEFAULT_LATENCY_MS = 300.0
DEFAULT_PAYOUT = 0.99  # per project spec ("paid as 99 cents"); verify empirically


@dataclass
class BookAt:
    ts: float
    book: Orderbook


@dataclass
class WindowTape:
    market: Market
    books: list[BookAt]
    result: str
    settlement_close_ts: float


class ReplayResult(BaseModel):
    run_id: str
    asset: str
    strategy: str
    windows: int
    trades: int
    wins: int
    losses: int
    pnl_gross: float
    pnl_net: float
    fees: float


def _book_from_row(yes_bids_json: str, no_bids_json: str) -> Orderbook:
    def levels(raw: str) -> list[OrderbookLevel]:
        return [OrderbookLevel(price=p, quantity=q) for p, q in json.loads(raw)]

    return Orderbook(yes_bids=levels(yes_bids_json), no_bids=levels(no_bids_json))


class ReplayEngine:
    def __init__(
        self,
        db: Database,
        *,
        latency_ms: float = DEFAULT_LATENCY_MS,
        payout_per_contract: float = DEFAULT_PAYOUT,
        seed: int = 0,
    ) -> None:
        self.db = db
        self.latency_ms = latency_ms
        self.payout = payout_per_contract
        self.seed = seed

    # ------------------------------------------------------------- tape load

    def load_windows(
        self, asset: str, start_ts: float | None = None, end_ts: float | None = None
    ) -> list[WindowTape]:
        """Settled windows for `asset` that have book snapshots recorded."""
        rows = self.db.query(
            "SELECT s.market_ticker, s.result, s.close_ts, m.open_ts, m.floor_strike "
            "FROM settlements s JOIN markets m ON m.ticker = s.market_ticker "
            "WHERE s.asset = ? AND s.result IN ('yes','no') "
            "AND (? IS NULL OR s.close_ts >= ?) AND (? IS NULL OR s.close_ts <= ?) "
            "ORDER BY s.close_ts",
            (asset, start_ts, start_ts, end_ts, end_ts),
        )
        windows: list[WindowTape] = []
        for r in rows:
            books = self.db.query(
                "SELECT ts, yes_bids, no_bids FROM book_snapshots "
                "WHERE market_ticker = ? ORDER BY ts, id",
                (r["market_ticker"],),
            )
            if not books:
                continue
            market = Market(
                ticker=r["market_ticker"],
                floor_strike=r["floor_strike"],
                open_time=datetime.fromtimestamp(r["open_ts"], tz=timezone.utc)
                if r["open_ts"] else None,
                close_time=datetime.fromtimestamp(r["close_ts"], tz=timezone.utc),
            )
            windows.append(WindowTape(
                market=market,
                books=[BookAt(b["ts"], _book_from_row(b["yes_bids"], b["no_bids"]))
                       for b in books],
                result=r["result"],
                settlement_close_ts=r["close_ts"],
            ))
        return windows

    def _spot_ticks(self, asset: str, start_ts: float, end_ts: float) -> list[tuple[float, float, str]]:
        rows = self.db.query(
            "SELECT ts, price, source FROM spot_ticks "
            "WHERE asset = ? AND ts BETWEEN ? AND ? ORDER BY ts, id",
            (asset, start_ts, end_ts),
        )
        return [(r["ts"], r["price"], r["source"]) for r in rows]

    # ----------------------------------------------------------------- replay

    def run(
        self,
        asset: str,
        strategy: Strategy,
        *,
        start_ts: float | None = None,
        end_ts: float | None = None,
        persist: bool = True,
    ) -> ReplayResult:
        rng = random.Random(self.seed)  # reserved for strategies that need it
        strategy.rng = rng  # type: ignore[attr-defined]

        windows = self.load_windows(asset, start_ts, end_ts)
        run_id = f"replay-{uuid.uuid4().hex[:12]}"
        btc_books = self._btc_mid_timeline() if asset != "BTC" else ([], [])

        fills_rows: list[dict] = []
        positions_rows: list[dict] = []
        wins = losses = trades = 0
        pnl_gross_total = pnl_net_total = fees_total = 0.0

        for tape in windows:
            outcome = self._replay_window(asset, strategy, tape, btc_books, run_id, fills_rows)
            if outcome is None:
                continue
            positions_rows.append(outcome)
            trades += 1
            pnl_gross_total += outcome["pnl_gross"]
            pnl_net_total += outcome["pnl_net"]
            fees_total += outcome["fees"]
            if outcome["pnl_gross"] > 0:
                wins += 1
            else:
                losses += 1

        result = ReplayResult(
            run_id=run_id, asset=asset, strategy=strategy.params_key(),
            windows=len(windows), trades=trades, wins=wins, losses=losses,
            pnl_gross=round(pnl_gross_total, 4), pnl_net=round(pnl_net_total, 4),
            fees=round(fees_total, 4),
        )
        if persist:
            self.db.write_now("sim_runs", {
                "run_id": run_id, "created_ts": time.time(), "kind": "replay",
                "asset": asset, "strategy": strategy.params_key(),
                "params": dump_json(strategy.params),
                "latency_ms": self.latency_ms, "seed": self.seed,
                "tape_start": windows[0].books[0].ts if windows else None,
                "tape_end": windows[-1].settlement_close_ts if windows else None,
                "windows": len(windows),
                "summary": result.model_dump_json(),
            })
            for row in fills_rows:
                self.db.write_now("sim_fills", row)
            for row in positions_rows:
                self.db.write_now("sim_positions", row)
        return result

    def _replay_window(
        self,
        asset: str,
        strategy: Strategy,
        tape: WindowTape,
        btc_books: tuple[list[float], list[float]],
        run_id: str,
        fills_rows: list[dict],
    ) -> dict | None:
        window_start = tape.books[0].ts
        spot_rows = self._spot_ticks(asset, window_start - 3600, tape.settlement_close_ts)
        if not spot_rows:
            return None
        # replay uses the venue that dominated the recording for this window
        primary = max({s for _, _, s in spot_rows},
                      key=lambda s: sum(1 for r in spot_rows if r[2] == s))
        spot_window = TickWindow()
        spot_idx = 0

        btc_ts, btc_mids = btc_books
        position = PositionState(market_ticker=tape.market.ticker)
        entry_fees = 0.0
        entry_regime: str | None = None
        book_ts_list = [b.ts for b in tape.books]

        for i, book_at in enumerate(tape.books):
            now = book_at.ts
            while spot_idx < len(spot_rows) and spot_rows[spot_idx][0] <= now:
                ts, price, source = spot_rows[spot_idx]
                if source == primary:
                    spot_window.add(ts, price)
                spot_idx += 1
            if spot_window.last_price is None:
                continue

            btc_implied = None
            if btc_ts:
                j = bisect_right(btc_ts, now) - 1
                if j >= 0 and now - btc_ts[j] <= 10:
                    btc_implied = btc_mids[j]

            snapshot = compute_snapshot(
                now=now, asset=asset, market=tape.market,
                book=book_at.book, book_ts=now,
                spot_window=spot_window, spot_source=primary,
                btc_window=None, btc_implied_prob=btc_implied,
            )
            signal = strategy.evaluate(snapshot, position)
            if signal is None or position.side is not None:
                continue

            # execute against the first book at/after signal time + latency
            exec_idx = bisect_right(book_ts_list, now + self.latency_ms / 1000.0)
            if exec_idx >= len(tape.books):
                continue  # no book left in the window to execute on
            exec_book = tape.books[exec_idx]
            fill = simulate_taker(
                signal.intent, signal.limit_price, signal.contracts, exec_book.book
            )
            if fill.filled <= 0:
                continue
            side = "yes" if signal.intent is OrderIntent.BUY_YES else "no"
            position = PositionState(
                market_ticker=tape.market.ticker, side=side,
                contracts=fill.filled, avg_price=fill.avg_price or 0.0,
                entry_ts=exec_book.ts,
            )
            entry_fees = fill.total_fee
            entry_regime = snapshot.regime.value
            fills_rows.append({
                "run_id": run_id, "ts": exec_book.ts, "asset": asset,
                "market_ticker": tape.market.ticker,
                "intent": signal.intent.value, "price": position.avg_price,
                "contracts": fill.filled, "fee": entry_fees,
                "liquidity": "taker", "reason": signal.reason,
            })

        if position.side is None:
            return None
        pnl_gross, pnl_net = settle_position(
            position.side, position.contracts, position.avg_price,
            entry_fees, tape.result, self.payout,
        )
        return {
            "run_id": run_id, "asset": asset, "market_ticker": tape.market.ticker,
            "side": position.side, "contracts": position.contracts,
            "avg_price": position.avg_price, "fees": entry_fees,
            "entry_ts": position.entry_ts, "entry_regime": entry_regime,
            "result": tape.result, "payout_per_contract": self.payout,
            "pnl_gross": round(pnl_gross, 4), "pnl_net": round(pnl_net, 4),
        }

    def _btc_mid_timeline(self) -> tuple[list[float], list[float]]:
        rows = self.db.query(
            "SELECT ts, mid FROM book_snapshots WHERE asset = 'BTC' "
            "AND mid IS NOT NULL ORDER BY ts, id"
        )
        return [r["ts"] for r in rows], [r["mid"] for r in rows]
