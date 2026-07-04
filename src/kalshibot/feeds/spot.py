"""Spot price feeds: Coinbase Exchange WS (primary) + Binance.US WS (backup).

Venue decision (2026-07-04): the spec named binance.com as primary, but it
geo-blocks US IPs (HTTP 451). Coinbase lists all nine assets (including
HYPE-USD and BNB-USD) and is a CF Benchmarks constituent exchange — the same
index family Kalshi settles against — so Coinbase is primary and Binance.US
(all nine as USDT pairs) is the backup.

Failover is per-asset at READ time: each venue keeps its own TickWindow and
`window()` returns the freshest acceptable one. Ticks from different venues
are never merged into one series, so returns/vol stay internally consistent.
"""

from __future__ import annotations

import asyncio
import json
import logging
import time
from dataclasses import dataclass
from typing import Awaitable, Callable

import websockets

from kalshibot.features.window import TickWindow

logger = logging.getLogger(__name__)

COINBASE_WS = "wss://ws-feed.exchange.coinbase.com"
BINANCE_US_WS = "wss://stream.binance.us:9443/stream"

STALE_AFTER_SECONDS = 5.0
RECONNECT_MAX_BACKOFF = 30.0


@dataclass
class SpotTick:
    asset: str
    price: float
    ts: float          # exchange timestamp (epoch seconds)
    source: str        # "coinbase" | "binance_us"


TickCallback = Callable[[SpotTick], Awaitable[None] | None]


async def _reconnect_loop(name: str, connect_once: Callable[[], Awaitable[None]]) -> None:
    backoff = 1.0
    while True:
        try:
            await connect_once()
            backoff = 1.0  # clean exit = server closed; retry quickly
        except asyncio.CancelledError:
            raise
        except Exception as exc:
            logger.warning("%s feed error: %s — reconnecting in %.0fs", name, exc, backoff)
        await asyncio.sleep(backoff)
        backoff = min(backoff * 2, RECONNECT_MAX_BACKOFF)


class CoinbaseFeed:
    def __init__(self, products: dict[str, str], on_tick: TickCallback) -> None:
        self.products = products  # product_id -> asset symbol
        self.on_tick = on_tick

    async def run(self) -> None:
        await _reconnect_loop("coinbase", self._connect_once)

    async def _connect_once(self) -> None:
        async with websockets.connect(COINBASE_WS, ping_interval=20) as ws:
            await ws.send(json.dumps({
                "type": "subscribe",
                "product_ids": list(self.products),
                "channels": ["ticker", "heartbeat"],
            }))
            logger.info("coinbase subscribed: %s", ", ".join(self.products))
            async for raw in ws:
                msg = json.loads(raw)
                if msg.get("type") != "ticker":
                    continue
                asset = self.products.get(msg.get("product_id", ""))
                if asset is None or "price" not in msg:
                    continue
                ts = time.time()
                if t := msg.get("time"):
                    try:
                        from datetime import datetime
                        ts = datetime.fromisoformat(t.replace("Z", "+00:00")).timestamp()
                    except ValueError:
                        pass
                result = self.on_tick(SpotTick(asset, float(msg["price"]), ts, "coinbase"))
                if result is not None:
                    await result


class BinanceUSFeed:
    def __init__(self, symbols: dict[str, str], on_tick: TickCallback) -> None:
        self.symbols = {s.upper(): a for s, a in symbols.items()}  # SYMBOL -> asset
        self.on_tick = on_tick

    async def run(self) -> None:
        await _reconnect_loop("binance_us", self._connect_once)

    async def _connect_once(self) -> None:
        streams = "/".join(f"{s.lower()}@trade" for s in self.symbols)
        async with websockets.connect(
            f"{BINANCE_US_WS}?streams={streams}", ping_interval=20
        ) as ws:
            logger.info("binance_us subscribed: %s", ", ".join(self.symbols))
            async for raw in ws:
                data = json.loads(raw).get("data") or {}
                asset = self.symbols.get(data.get("s", ""))
                if asset is None or "p" not in data:
                    continue
                ts = float(data.get("T", time.time() * 1000)) / 1000.0
                result = self.on_tick(SpotTick(asset, float(data["p"]), ts, "binance_us"))
                if result is not None:
                    await result


class SpotRouter:
    """Owns per-asset, per-venue tick windows and picks the freshest at read."""

    PRIMARY = "coinbase"
    BACKUP = "binance_us"

    def __init__(
        self,
        assets: list[str],
        *,
        horizon_seconds: float = 3600.0,
        on_tick: TickCallback | None = None,
    ) -> None:
        self.assets = assets
        self._windows: dict[tuple[str, str], TickWindow] = {
            (asset, src): TickWindow(horizon_seconds)
            for asset in assets
            for src in (self.PRIMARY, self.BACKUP)
        }
        self._external_on_tick = on_tick
        self.tick_counts: dict[str, int] = {self.PRIMARY: 0, self.BACKUP: 0}

    def handle_tick(self, tick: SpotTick) -> None:
        window = self._windows.get((tick.asset, tick.source))
        if window is None:
            return
        window.add(tick.ts, tick.price)
        self.tick_counts[tick.source] = self.tick_counts.get(tick.source, 0) + 1
        if self._external_on_tick is not None:
            self._external_on_tick(tick)

    def window(self, asset: str, now: float | None = None) -> tuple[TickWindow, str]:
        """Freshest acceptable window: primary unless stale, else backup."""
        now = now if now is not None else time.time()
        primary = self._windows[(asset, self.PRIMARY)]
        backup = self._windows[(asset, self.BACKUP)]
        p_age = primary.age_seconds(now)
        b_age = backup.age_seconds(now)
        if p_age is not None and p_age <= STALE_AFTER_SECONDS:
            return primary, self.PRIMARY
        if b_age is not None and (p_age is None or b_age < p_age):
            return backup, self.BACKUP
        return primary, self.PRIMARY

    def feed_tasks(
        self,
        coinbase_products: dict[str, str],
        binance_symbols: dict[str, str],
    ) -> list[Awaitable[None]]:
        return [
            CoinbaseFeed(coinbase_products, self.handle_tick).run(),
            BinanceUSFeed(binance_symbols, self.handle_tick).run(),
        ]
