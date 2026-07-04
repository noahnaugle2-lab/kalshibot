"""Observation mode: record everything, trade nothing.

Development-order step 2. Runs the spot feeds, the Kalshi tape recorders, and
the feature engine across all discovered assets, persisting to SQLite:

    spot_ticks      1s-downsampled spot per asset per venue
    book_snapshots  Kalshi order book every ~2s per asset
    trade_tape      every Kalshi trade print (deduped by trade_id)
    markets         every market seen, raw payload included
    settlements     ground truth after each window (strike vs expiration value)
    signals         full FeatureSnapshot per asset every scan cycle

There is deliberately no order code path anywhere in this module.
"""

from __future__ import annotations

import asyncio
import logging
import signal
import time

from kalshibot.config import PROJECT_ROOT, Settings, load_asset_configs
from kalshibot.feeds.kalshi_tape import AssetTapeRecorder
from kalshibot.feeds.spot import SpotRouter, SpotTick
from kalshibot.features.engine import compute_snapshot
from kalshibot.kalshi.client import KalshiClient, KalshiEnvironment
from kalshibot.kalshi.discovery import discover_15m_series
from kalshibot.persistence.db import Database
from kalshibot.utils.clock import check_clock

logger = logging.getLogger(__name__)

SCAN_INTERVAL = 2.0
STATUS_INTERVAL = 60.0
DISCOVERY_REFRESH = 24 * 3600.0
SPOT_WRITE_DOWNSAMPLE = 1.0  # max one spot row per asset+venue per second
CLOCK_CHECK_INTERVAL = 3600.0


class Observer:
    def __init__(self, db_path: str | None = None) -> None:
        self.settings = Settings()
        self.asset_configs = load_asset_configs()
        self.db = Database(db_path or PROJECT_ROOT / "data" / "kalshibot.db")
        self.client = KalshiClient(environment=KalshiEnvironment.PROD)
        self.router: SpotRouter | None = None
        self.recorders: dict[str, AssetTapeRecorder] = {}
        self._last_spot_write: dict[tuple[str, str], float] = {}
        self._stop = asyncio.Event()

    # ------------------------------------------------------------- lifecycle

    async def start(self) -> None:
        ok, offset = await check_clock()
        if not ok:
            logger.warning(
                "clock drift check failed (offset=%s) — observation continues, "
                "but fix NTP sync before any trading phase", offset,
            )

        report = await discover_15m_series(self.client)
        for asset in report.missing:
            logger.warning("%s: no 15-min series today; daily re-check active", asset)

        assets = [a for a in report.found if self.asset_configs[a].enabled]
        logger.info("observing %d assets: %s", len(assets), ", ".join(assets))

        self.router = SpotRouter(assets, on_tick=self._record_spot_tick)
        coinbase = {
            self.asset_configs[a].spot_symbol_coinbase: a
            for a in assets if self.asset_configs[a].spot_symbol_coinbase
        }
        binance = {
            self.asset_configs[a].spot_symbol_binance: a
            for a in assets if self.asset_configs[a].spot_symbol_binance
        }

        for i, asset in enumerate(assets):
            found = report.found[asset]
            self.db.write_now("assets", {
                "symbol": asset,
                "series_ticker": found.series_ticker,
                "spot_symbol_primary": self.asset_configs[asset].spot_symbol_coinbase,
                "spot_symbol_backup": self.asset_configs[asset].spot_symbol_binance,
                "updated_at": time.time(),
            })
            self.recorders[asset] = AssetTapeRecorder(
                asset, found.series_ticker, self.client, self.db,
                stagger=i * 0.25,
            )

        tasks = [
            asyncio.create_task(coro, name=name)
            for name, coro in [
                ("db_flusher", self.db.run_flusher()),
                *[(f"spot_{i}", t) for i, t in enumerate(
                    self.router.feed_tasks(coinbase, binance))],
                *[(f"tape_{a}", r.run()) for a, r in self.recorders.items()],
                ("scan", self._scan_loop(assets)),
                ("status", self._status_loop()),
                ("discovery_refresh", self._discovery_refresh_loop()),
                ("clock", self._clock_loop()),
                *[(f"extra_{i}", t) for i, t in enumerate(self.extra_tasks())],
            ]
        ]
        await self._stop.wait()
        logger.info("shutting down...")
        for task in tasks:
            task.cancel()
        await asyncio.gather(*tasks, return_exceptions=True)
        self.db.close()
        await self.client.close()
        logger.info("shutdown complete")

    def request_stop(self) -> None:
        self._stop.set()

    # ------------------------------------------------- subclass hooks (trader)

    def enrich_snapshot(self, asset: str, snap):  # noqa: ANN001 - FeatureSnapshot
        """Called before the snapshot is persisted; trader attaches smart money."""
        return snap

    def on_snapshot(self, asset: str, snap) -> None:  # noqa: ANN001
        """Called after persistence; the shadow trader trades here. No-op."""

    def extra_tasks(self) -> list:
        """Additional background coroutines for subclasses."""
        return []

    # ------------------------------------------------------------- recording

    def _record_spot_tick(self, tick: SpotTick) -> None:
        key = (tick.asset, tick.source)
        last = self._last_spot_write.get(key, 0.0)
        if tick.ts - last >= SPOT_WRITE_DOWNSAMPLE:
            self._last_spot_write[key] = tick.ts
            self.db.add("spot_ticks", {
                "ts": tick.ts,
                "asset": tick.asset,
                "source": tick.source,
                "price": tick.price,
            })

    async def _scan_loop(self, assets: list[str]) -> None:
        assert self.router is not None
        while True:
            await asyncio.sleep(SCAN_INTERVAL)
            now = time.time()
            btc_implied: float | None = None
            btc_recorder = self.recorders.get("BTC")
            if btc_recorder and btc_recorder.latest_book and btc_recorder.latest_book.mid:
                btc_implied = float(btc_recorder.latest_book.mid)
            btc_window = self.router.window("BTC", now)[0] if "BTC" in assets else None

            for asset in assets:
                recorder = self.recorders[asset]
                market = recorder.current_market
                if market is None:
                    continue
                window, source = self.router.window(asset, now)
                try:
                    snap = compute_snapshot(
                        now=now,
                        asset=asset,
                        market=market,
                        book=recorder.latest_book,
                        book_ts=recorder.latest_book_ts,
                        spot_window=window,
                        spot_source=source,
                        btc_window=btc_window if asset != "BTC" else None,
                        btc_implied_prob=btc_implied if asset != "BTC" else None,
                    )
                    snap = self.enrich_snapshot(asset, snap)
                    self.db.add("signals", snap.to_row())
                    self.on_snapshot(asset, snap)
                except Exception as exc:
                    logger.error("%s: feature computation failed: %s", asset, exc)

    # ------------------------------------------------------------ background

    async def _status_loop(self) -> None:
        while True:
            await asyncio.sleep(STATUS_INTERVAL)
            counts = await asyncio.to_thread(self.db.counts)
            ticks = self.router.tick_counts if self.router else {}
            logger.info(
                "status: rows=%s spot_ticks_received=%s",
                {k: v for k, v in counts.items() if v}, ticks,
            )

    async def _discovery_refresh_loop(self) -> None:
        while True:
            await asyncio.sleep(DISCOVERY_REFRESH)
            try:
                report = await discover_15m_series(self.client)
                newly = [a for a in report.found if a not in self.recorders]
                if newly:
                    logger.info(
                        "discovery: new 15-min series available for %s — "
                        "restart observer to pick them up", newly,
                    )
            except Exception as exc:
                logger.warning("daily discovery refresh failed: %s", exc)

    async def _clock_loop(self) -> None:
        while True:
            await asyncio.sleep(CLOCK_CHECK_INTERVAL)
            await check_clock()


async def amain(db_path: str | None = None) -> None:
    observer = Observer(db_path)
    loop = asyncio.get_running_loop()
    for sig in (signal.SIGINT, signal.SIGTERM):
        loop.add_signal_handler(sig, observer.request_stop)
    await observer.start()


def main() -> None:
    logging.basicConfig(
        level=logging.INFO,
        format="%(asctime)s %(levelname)-7s %(name)s: %(message)s",
    )
    logging.getLogger("httpx").setLevel(logging.WARNING)  # one line per request otherwise
    asyncio.run(amain())


if __name__ == "__main__":
    main()
