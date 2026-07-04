"""SQLite persistence: schema, connection management, buffered async writes.

Design constraints:
- Every trade must be reconstructable and every scorecard metric recomputable
  from raw rows, so raw JSON payloads ride along with the typed columns.
- Observation writes ~10-20 rows/sec across 9 assets; a buffered writer
  batches inserts and commits off the event loop (WAL mode, single writer).
- The replay engine later reads these same tables, so timestamps are UTC
  epoch seconds (REAL) throughout — no string parsing in hot loops.
"""

from __future__ import annotations

import asyncio
import json
import logging
import sqlite3
import threading
from pathlib import Path
from typing import Any

logger = logging.getLogger(__name__)

SCHEMA_VERSION = 1

SCHEMA = """
CREATE TABLE IF NOT EXISTS meta (
    key TEXT PRIMARY KEY,
    value TEXT NOT NULL
);

CREATE TABLE IF NOT EXISTS assets (
    symbol TEXT PRIMARY KEY,
    series_ticker TEXT,
    spot_symbol_primary TEXT,
    spot_symbol_backup TEXT,
    updated_at REAL NOT NULL
);

CREATE TABLE IF NOT EXISTS markets (
    ticker TEXT PRIMARY KEY,
    asset TEXT NOT NULL,
    series_ticker TEXT NOT NULL,
    open_ts REAL,
    close_ts REAL,
    floor_strike REAL,
    expiration_value REAL,
    result TEXT DEFAULT '',
    status TEXT DEFAULT '',
    raw TEXT,
    updated_at REAL NOT NULL
);
CREATE INDEX IF NOT EXISTS idx_markets_asset_close ON markets(asset, close_ts);

CREATE TABLE IF NOT EXISTS spot_ticks (
    id INTEGER PRIMARY KEY,
    ts REAL NOT NULL,
    asset TEXT NOT NULL,
    source TEXT NOT NULL,
    price REAL NOT NULL
);
CREATE INDEX IF NOT EXISTS idx_spot_asset_ts ON spot_ticks(asset, ts);

CREATE TABLE IF NOT EXISTS book_snapshots (
    id INTEGER PRIMARY KEY,
    ts REAL NOT NULL,
    market_ticker TEXT NOT NULL,
    asset TEXT NOT NULL,
    yes_bids TEXT NOT NULL,   -- JSON [[price, qty], ...] best-first
    no_bids TEXT NOT NULL,
    best_yes_bid REAL,
    best_yes_ask REAL,
    mid REAL,
    spread REAL
);
CREATE INDEX IF NOT EXISTS idx_book_market_ts ON book_snapshots(market_ticker, ts);
CREATE INDEX IF NOT EXISTS idx_book_asset_ts ON book_snapshots(asset, ts);

CREATE TABLE IF NOT EXISTS trade_tape (
    id INTEGER PRIMARY KEY,
    trade_id TEXT NOT NULL UNIQUE,
    market_ticker TEXT NOT NULL,
    asset TEXT NOT NULL,
    ts REAL NOT NULL,
    yes_price REAL,
    count REAL,
    taker_side TEXT,
    raw TEXT
);
CREATE INDEX IF NOT EXISTS idx_tape_market_ts ON trade_tape(market_ticker, ts);

CREATE TABLE IF NOT EXISTS settlements (
    market_ticker TEXT PRIMARY KEY,
    asset TEXT NOT NULL,
    result TEXT NOT NULL,
    floor_strike REAL,
    expiration_value REAL,
    open_ts REAL,
    close_ts REAL,
    recorded_ts REAL NOT NULL,
    consistent INTEGER            -- result agrees with strike vs expiration value
);
CREATE INDEX IF NOT EXISTS idx_settlements_asset ON settlements(asset);

CREATE TABLE IF NOT EXISTS signals (
    id INTEGER PRIMARY KEY,
    ts REAL NOT NULL,
    asset TEXT NOT NULL,
    market_ticker TEXT NOT NULL,
    schema_version INTEGER NOT NULL,
    features TEXT NOT NULL        -- JSON FeatureSnapshot
);
CREATE INDEX IF NOT EXISTS idx_signals_asset_ts ON signals(asset, ts);
CREATE INDEX IF NOT EXISTS idx_signals_market ON signals(market_ticker);
"""


class Database:
    """Buffered writer over a WAL-mode SQLite file.

    add() enqueues; a background task flushes batches roughly once per
    second via a worker thread so the event loop never blocks on fsync.
    Reads open short-lived connections (WAL allows concurrent readers).
    """

    def __init__(self, path: str | Path) -> None:
        self.path = Path(path)
        self.path.parent.mkdir(parents=True, exist_ok=True)
        self._queue: asyncio.Queue[tuple[str, dict[str, Any]]] = asyncio.Queue()
        self._flush_task: asyncio.Task | None = None
        self._closed = False
        self._write_lock = threading.Lock()
        self._conn = sqlite3.connect(self.path, check_same_thread=False)
        self._conn.execute("PRAGMA journal_mode=WAL")
        self._conn.execute("PRAGMA synchronous=NORMAL")
        self._conn.execute("PRAGMA busy_timeout=5000")
        self._conn.executescript(SCHEMA)
        self._conn.execute(
            "INSERT OR REPLACE INTO meta(key, value) VALUES('schema_version', ?)",
            (str(SCHEMA_VERSION),),
        )
        self._conn.commit()

    # ------------------------------------------------------------- writes

    def add(self, table: str, row: dict[str, Any]) -> None:
        """Enqueue a row for the next batch flush (fire and forget)."""
        if self._closed:
            raise RuntimeError("database is closed")
        self._queue.put_nowait((table, row))

    def write_now(self, table: str, row: dict[str, Any]) -> None:
        """Synchronous upsert for low-frequency, must-not-lose rows."""
        with self._write_lock:
            self._upsert(table, row)
            self._conn.commit()

    def _upsert(self, table: str, row: dict[str, Any]) -> None:
        cols = ", ".join(row)
        placeholders = ", ".join("?" for _ in row)
        self._conn.execute(
            f"INSERT OR REPLACE INTO {table} ({cols}) VALUES ({placeholders})",
            tuple(row.values()),
        )

    def _flush_batch(self, batch: list[tuple[str, dict[str, Any]]]) -> None:
        with self._write_lock:
            for table, row in batch:
                try:
                    self._upsert(table, row)
                except sqlite3.IntegrityError:
                    pass  # duplicate trade_id etc. — dedupe is the constraint's job
            self._conn.commit()

    async def run_flusher(self, interval: float = 1.0) -> None:
        """Background task: drain the queue and commit once per interval."""
        try:
            while not self._closed or not self._queue.empty():
                await asyncio.sleep(interval)
                batch: list[tuple[str, dict[str, Any]]] = []
                while not self._queue.empty():
                    batch.append(self._queue.get_nowait())
                if batch:
                    await asyncio.to_thread(self._flush_batch, batch)
        except asyncio.CancelledError:
            batch = []
            while not self._queue.empty():
                batch.append(self._queue.get_nowait())
            if batch:
                self._flush_batch(batch)
            raise

    # -------------------------------------------------------------- reads

    def query(self, sql: str, params: tuple = ()) -> list[sqlite3.Row]:
        conn = sqlite3.connect(self.path)
        conn.row_factory = sqlite3.Row
        try:
            return conn.execute(sql, params).fetchall()
        finally:
            conn.close()

    def counts(self) -> dict[str, int]:
        tables = ("spot_ticks", "book_snapshots", "trade_tape", "signals",
                  "settlements", "markets")
        with self._write_lock:
            return {
                t: self._conn.execute(f"SELECT COUNT(*) FROM {t}").fetchone()[0]
                for t in tables
            }

    def close(self) -> None:
        self._closed = True
        with self._write_lock:
            self._conn.commit()
            self._conn.close()


def dump_json(obj: Any) -> str:
    return json.dumps(obj, separators=(",", ":"), default=str)
