"""Kalshi tape recorder: order books, trade prints, and settlements to SQLite.

One AssetTapeRecorder per asset:
- tracks the currently-open 15-minute market and rolls to the next window
- polls the order book every `book_interval` seconds (staggered start so nine
  assets don't burst the shared token bucket together)
- polls the trade tape and dedupes by trade_id (UNIQUE constraint)
- after each window closes, fetches the settled market record and stores
  floor_strike / expiration_value / result — the settlement ground truth

Transport is REST polling through the shared rate limiter. Kalshi's WS feed
requires (prod) API credentials; when a prod key exists this module can grow
a WS path, but polling at these intervals stays well inside basic-tier
limits: 9 assets x (book/2s + trades/6s) ~= 6 req/s vs the 8/s bucket.
"""

from __future__ import annotations

import asyncio
import logging
import time
from datetime import datetime, timezone

from kalshibot.kalshi.client import KalshiAPIError, KalshiClient
from kalshibot.kalshi.models import Market, Orderbook
from kalshibot.persistence.db import Database, dump_json

logger = logging.getLogger(__name__)

BOOK_INTERVAL = 2.0
TRADES_INTERVAL = 6.0
SETTLEMENT_CHECK_DELAY = 120.0  # settlement value appears a few min after close
MARKET_ROLL_GRACE = 5.0
MAX_TRADE_PAGES = 12  # 12 x 100 = 1200 prints per poll ceiling (burst safety)


class AssetTapeRecorder:
    def __init__(
        self,
        asset: str,
        series_ticker: str,
        client: KalshiClient,
        db: Database,
        *,
        book_interval: float = BOOK_INTERVAL,
        trades_interval: float = TRADES_INTERVAL,
        stagger: float = 0.0,
    ) -> None:
        self.asset = asset
        self.series_ticker = series_ticker
        self.client = client
        self.db = db
        self.book_interval = book_interval
        self.trades_interval = trades_interval
        self.stagger = stagger

        self.current_market: Market | None = None
        self.latest_book: Orderbook | None = None
        self.latest_book_ts: float | None = None
        self._trade_watermark: dict[str, float] = {}  # ticker -> newest print ts seen
        self._roll_from: str | None = None  # outgoing ticker awaiting a final trades poll

    # ------------------------------------------------------------ market roll

    async def _refresh_market(self) -> None:
        now = datetime.now(timezone.utc)
        try:
            markets = await self.client.get_markets(
                series_ticker=self.series_ticker, status="open", max_results=5
            )
        except KalshiAPIError as exc:
            logger.warning("%s: market refresh failed: %s", self.asset, exc)
            return
        except Exception as exc:  # noqa: BLE001 — httpx.TransportError etc. must not
            # kill the recorder task on a transient network blip; retry next roll
            logger.warning("%s: market refresh transport error: %s", self.asset, exc)
            return
        live = [
            m for m in markets
            if m.close_time is not None and m.close_time > now
            and m.status in ("active", "open")
        ]
        if not live:
            if self.current_market is not None:
                logger.info("%s: no open market right now", self.asset)
            self.current_market = None
            return
        market = min(live, key=lambda m: m.close_time)  # type: ignore[arg-type,return-value]
        if self.current_market is None or market.ticker != self.current_market.ticker:
            if self.current_market is not None:
                self._roll_from = self.current_market.ticker  # final trades sweep
            self.current_market = market
            self.latest_book = None
            self.latest_book_ts = None
            logger.info(
                "%s: window rolled to %s (closes %s)",
                self.asset, market.ticker, market.close_time,
            )
        self.db.add("markets", self._market_row(market))

    def _market_row(self, m: Market) -> dict:
        return {
            "ticker": m.ticker,
            "asset": self.asset,
            "series_ticker": self.series_ticker,
            "open_ts": m.open_time.timestamp() if m.open_time else None,
            "close_ts": m.close_time.timestamp() if m.close_time else None,
            "floor_strike": float(m.floor_strike) if m.floor_strike is not None else None,
            "expiration_value": (
                float(m.expiration_value) if m.expiration_value is not None else None
            ),
            "result": m.result,
            "status": m.status,
            "raw": dump_json(m.model_dump(mode="json")),
            "updated_at": time.time(),
        }

    # ------------------------------------------------------------------ loops

    async def run(self) -> None:
        await asyncio.sleep(self.stagger)
        await self._refresh_market()
        await asyncio.gather(
            self._book_loop(),
            self._trades_loop(),
            self._market_roll_loop(),
            self._settlement_loop(),
        )

    async def _market_roll_loop(self) -> None:
        while True:
            try:
                close = (
                    self.current_market.close_time.timestamp()
                    if self.current_market and self.current_market.close_time
                    else None
                )
                if close is None:
                    # between windows (next market not yet listed): retry fast so
                    # strategies that act early in the window don't lose time
                    await asyncio.sleep(5)
                else:
                    await asyncio.sleep(max(1.0, close - time.time() + MARKET_ROLL_GRACE))
                await self._refresh_market()
                # one final trades sweep of the window we just left so the last
                # prints (which land after the loop's last in-window poll) aren't lost
                if self._roll_from is not None:
                    rolled = self._roll_from
                    self._roll_from = None
                    await self._poll_trades(rolled)
                    self._trade_watermark.pop(rolled, None)
            except asyncio.CancelledError:
                raise
            except Exception as exc:  # noqa: BLE001 — self-heal; never kill the loop
                logger.error("%s: market roll loop error: %s", self.asset, exc)
                await asyncio.sleep(5)

    async def _book_loop(self) -> None:
        while True:
            market = self.current_market
            if market is None:
                await asyncio.sleep(self.book_interval)
                continue
            try:
                book = await self.client.get_orderbook(market.ticker)
                now = time.time()
                self.latest_book, self.latest_book_ts = book, now
                self.db.add("book_snapshots", {
                    "ts": now,
                    "market_ticker": market.ticker,
                    "asset": self.asset,
                    "yes_bids": dump_json(
                        [[str(l.price), str(l.quantity)] for l in book.yes_bids]
                    ),
                    "no_bids": dump_json(
                        [[str(l.price), str(l.quantity)] for l in book.no_bids]
                    ),
                    "best_yes_bid": float(book.best_yes_bid) if book.best_yes_bid is not None else None,
                    "best_yes_ask": float(book.best_yes_ask) if book.best_yes_ask is not None else None,
                    "mid": float(book.mid) if book.mid is not None else None,
                    "spread": float(book.spread) if book.spread is not None else None,
                })
            except KalshiAPIError as exc:
                logger.warning("%s: book poll failed: %s", self.asset, exc)
            except Exception as exc:
                logger.error("%s: book loop error: %s", self.asset, exc)
            await asyncio.sleep(self.book_interval)

    async def _trades_loop(self) -> None:
        while True:
            market = self.current_market
            if market is None:
                await asyncio.sleep(self.trades_interval)
                continue
            try:
                await self._poll_trades(market.ticker)
            except KalshiAPIError as exc:
                logger.warning("%s: trades poll failed: %s", self.asset, exc)
            except Exception as exc:  # noqa: BLE001 — self-heal; retry next tick
                logger.error("%s: trades loop error: %s", self.asset, exc)
            await asyncio.sleep(self.trades_interval)

    @staticmethod
    def _trade_ts(t: dict) -> float:
        try:
            return datetime.fromisoformat(
                t.get("created_time", "").replace("Z", "+00:00")
            ).timestamp()
        except ValueError:
            return time.time()

    def _trade_row(self, ticker: str, t: dict, ts: float) -> dict:
        return {
            "trade_id": t.get("trade_id", ""),
            "market_ticker": ticker,
            "asset": self.asset,
            "ts": ts,
            "yes_price": float(t["yes_price_dollars"]) if t.get("yes_price_dollars") else None,
            "count": float(t["count_fp"]) if t.get("count_fp") else None,
            "taker_side": t.get("taker_side", ""),
            "raw": dump_json(t),
        }

    async def _poll_trades(self, ticker: str) -> None:
        """Fetch new prints for `ticker`, following the cursor past the 100-row
        page so end-of-window bursts (>100 prints in one interval) aren't
        dropped. Kalshi returns newest-first, so we page back until we reach
        prints older than last poll's watermark; the UNIQUE trade_id constraint
        dedupes the small boundary overlap."""
        watermark = self._trade_watermark.get(ticker, 0.0)
        newest = watermark
        cursor: str | None = None
        for _page in range(MAX_TRADE_PAGES):
            data = await self.client.get_trades(ticker, limit=100, cursor=cursor)
            trades = data.get("trades") or []
            if not trades:
                break
            reached_old = False
            for t in trades:
                ts = self._trade_ts(t)
                newest = max(newest, ts)
                if ts < watermark:
                    reached_old = True
                self.db.add("trade_tape", self._trade_row(ticker, t, ts))
            cursor = data.get("cursor") or None
            if reached_old or not cursor:
                break
        else:
            logger.warning("%s: trade pagination hit %d-page cap for %s — some "
                           "prints may be unfetched", self.asset, MAX_TRADE_PAGES, ticker)
        self._trade_watermark[ticker] = newest

    async def _settlement_loop(self) -> None:
        """Sweep: any recorded market that closed >60s ago and has no
        settlements row gets fetched until it settles. DB-driven, so it
        survives restarts and backfills windows missed at rollover."""
        while True:
            await asyncio.sleep(SETTLEMENT_CHECK_DELAY)
            try:
                tickers = await asyncio.to_thread(
                    self.db.unsettled_markets, self.asset, time.time() - 60
                )
            except Exception as exc:
                logger.error("%s: unsettled-markets query failed: %s", self.asset, exc)
                continue
            for ticker in tickers:
                try:
                    market = await self.client.get_market(ticker)
                    if not market.is_settled:
                        continue  # not finalized yet; next sweep retries
                    self._record_settlement(market)
                except Exception as exc:
                    logger.warning(
                        "%s: settlement fetch %s failed: %s", self.asset, ticker, exc
                    )

    def _record_settlement(self, market: Market) -> None:
        consistent = market.settlement_consistent()
        self.db.write_now("settlements", {
            "market_ticker": market.ticker,
            "asset": self.asset,
            "result": market.result,
            "floor_strike": float(market.floor_strike)
            if market.floor_strike is not None else None,
            "expiration_value": float(market.expiration_value)
            if market.expiration_value is not None else None,
            "open_ts": market.open_time.timestamp() if market.open_time else None,
            "close_ts": market.close_time.timestamp() if market.close_time else None,
            "recorded_ts": time.time(),
            "consistent": None if consistent is None else int(consistent),
        })
        self.db.add("markets", self._market_row(market))
        if consistent is False:
            logger.error(
                "%s: settlement INCONSISTENT for %s: result=%s strike=%s exp=%s",
                self.asset, market.ticker, market.result,
                market.floor_strike, market.expiration_value,
            )
