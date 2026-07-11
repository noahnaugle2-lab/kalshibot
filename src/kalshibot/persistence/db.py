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

CREATE TABLE IF NOT EXISTS sim_runs (
    run_id TEXT PRIMARY KEY,
    created_ts REAL NOT NULL,
    kind TEXT NOT NULL,           -- 'replay' | 'shadow'
    asset TEXT NOT NULL,
    strategy TEXT NOT NULL,       -- Strategy.params_key(): name:version:params
    params TEXT NOT NULL,         -- JSON
    latency_ms REAL,
    seed INTEGER,
    tape_start REAL,
    tape_end REAL,
    windows INTEGER,
    summary TEXT                  -- JSON aggregates
);

CREATE TABLE IF NOT EXISTS sim_fills (
    id INTEGER PRIMARY KEY,
    run_id TEXT NOT NULL,
    ts REAL NOT NULL,
    asset TEXT NOT NULL,
    market_ticker TEXT NOT NULL,
    intent TEXT NOT NULL,
    price REAL NOT NULL,          -- in the intent's own terms
    contracts REAL NOT NULL,
    fee REAL NOT NULL,
    liquidity TEXT NOT NULL,
    reason TEXT
);
CREATE INDEX IF NOT EXISTS idx_sim_fills_run ON sim_fills(run_id);

CREATE TABLE IF NOT EXISTS sim_orders (
    id INTEGER PRIMARY KEY,
    run_id TEXT NOT NULL,
    ts REAL NOT NULL,
    asset TEXT NOT NULL,
    market_ticker TEXT NOT NULL,
    intent TEXT NOT NULL,
    execution TEXT NOT NULL,      -- taker | maker
    limit_price REAL NOT NULL,
    contracts REAL NOT NULL,
    status TEXT NOT NULL,         -- filled | partial | missed | expired
    filled_contracts REAL DEFAULT 0,
    reason TEXT
);
CREATE INDEX IF NOT EXISTS idx_sim_orders_run ON sim_orders(run_id);

CREATE TABLE IF NOT EXISTS sim_positions (
    id INTEGER PRIMARY KEY,
    run_id TEXT NOT NULL,
    asset TEXT NOT NULL,
    market_ticker TEXT NOT NULL,
    side TEXT NOT NULL,
    contracts REAL NOT NULL,
    avg_price REAL NOT NULL,
    fees REAL NOT NULL,
    entry_ts REAL,
    entry_regime TEXT,
    result TEXT,
    payout_per_contract REAL,
    pnl_gross REAL,
    pnl_net REAL
);
CREATE INDEX IF NOT EXISTS idx_sim_positions_run ON sim_positions(run_id);

CREATE TABLE IF NOT EXISTS calibration_reports (
    id INTEGER PRIMARY KEY,
    run_ts REAL NOT NULL,
    asset TEXT NOT NULL,
    n_windows INTEGER,
    n_snapshots INTEGER,
    brier_model REAL,
    brier_market REAL,
    hit_rate REAL,
    edge_hit_rate REAL,
    edge_calls INTEGER,
    report TEXT NOT NULL          -- full JSON incl. per-regime + bins
);
CREATE INDEX IF NOT EXISTS idx_calibration_asset ON calibration_reports(asset, run_ts);

CREATE TABLE IF NOT EXISTS flow_pattern_outcomes (
    id INTEGER PRIMARY KEY,
    pattern TEXT NOT NULL,
    asset TEXT NOT NULL,
    market_ticker TEXT NOT NULL,
    lean TEXT NOT NULL,           -- UP | DOWN
    result TEXT NOT NULL,
    correct INTEGER NOT NULL,
    close_ts REAL,
    computed_ts REAL NOT NULL,
    UNIQUE(pattern, market_ticker)
);
CREATE INDEX IF NOT EXISTS idx_fpo_pattern_asset ON flow_pattern_outcomes(pattern, asset, close_ts);

CREATE TABLE IF NOT EXISTS flow_patterns (
    pattern TEXT NOT NULL,
    asset TEXT NOT NULL,
    n INTEGER, hits INTEGER, hit_rate REAL,
    n_30d INTEGER, hits_30d INTEGER, hit_rate_30d REAL,
    status TEXT DEFAULT 'candidate',  -- candidate | active | benched
    updated_ts REAL NOT NULL,
    PRIMARY KEY (pattern, asset)
);

CREATE TABLE IF NOT EXISTS polymarket_markets (
    condition_id TEXT PRIMARY KEY,
    asset TEXT NOT NULL,
    slug TEXT NOT NULL,
    close_ts INTEGER NOT NULL,
    winner TEXT,                  -- UP | DOWN | NULL
    liquidity REAL,
    volume REAL,
    scanned INTEGER DEFAULT 0,
    updated_ts REAL NOT NULL
);
CREATE INDEX IF NOT EXISTS idx_pm_asset_close ON polymarket_markets(asset, close_ts);

CREATE TABLE IF NOT EXISTS wallet_window_results (
    id INTEGER PRIMARY KEY,
    wallet TEXT NOT NULL,
    asset TEXT NOT NULL,
    condition_id TEXT NOT NULL,
    close_ts INTEGER NOT NULL,
    lean TEXT NOT NULL,
    won INTEGER NOT NULL,
    pnl REAL,
    stake REAL,
    entry_offset_s REAL,
    lean_600 TEXT,                -- stance observable at minute 10 (causal)
    won_600 INTEGER,
    computed_ts REAL NOT NULL,
    UNIQUE(wallet, condition_id)
);
CREATE INDEX IF NOT EXISTS idx_wwr_wallet ON wallet_window_results(wallet);

CREATE TABLE IF NOT EXISTS config_overrides (
    id INTEGER PRIMARY KEY,
    ts REAL NOT NULL,
    scope TEXT NOT NULL,          -- e.g. asset:BTC
    changes TEXT NOT NULL         -- JSON of applied updates
);

CREATE TABLE IF NOT EXISTS decisions (
    id INTEGER PRIMARY KEY,
    ts REAL NOT NULL,
    asset TEXT NOT NULL,
    market_ticker TEXT NOT NULL,
    prompt_version INTEGER NOT NULL,
    prompt TEXT NOT NULL,
    raw_stdout TEXT,
    parsed TEXT,                  -- JSON TradeDecision or NULL
    latency_ms REAL,
    snapshot_age_s REAL,
    disposition TEXT NOT NULL,    -- claude | fallback | fallback_hold | stale_discard
    error TEXT,
    cost_usd REAL
);
CREATE INDEX IF NOT EXISTS idx_decisions_asset_ts ON decisions(asset, ts);

CREATE TABLE IF NOT EXISTS proposals (
    id INTEGER PRIMARY KEY,
    created_ts REAL NOT NULL,
    asset TEXT NOT NULL,
    strategy TEXT NOT NULL,
    current_params TEXT NOT NULL,   -- JSON
    proposed_params TEXT NOT NULL,  -- JSON
    rationale TEXT,
    replay_run_id TEXT,             -- replay validation is mandatory
    replay_summary TEXT,            -- JSON ReplayResult
    baseline_replay_run_id TEXT,
    baseline_replay_summary TEXT,
    status TEXT DEFAULT 'pending'   -- pending | approved | rejected (human only)
);

CREATE TABLE IF NOT EXISTS scorecards (
    id INTEGER PRIMARY KEY,
    run_ts REAL NOT NULL,
    rank INTEGER,
    kind TEXT NOT NULL,           -- shadow | replay
    asset TEXT NOT NULL,
    strategy TEXT NOT NULL,
    trades INTEGER,
    profit_factor REAL,
    pl_ratio_pct REAL,
    pnl_gross REAL,
    pnl_net REAL,
    hit_rate REAL,
    brier_model REAL,
    brier_market REAL,
    signals_per_day REAL,
    fill_rate_maker REAL,
    fill_rate_taker REAL,
    max_drawdown REAL,
    confidence TEXT,
    recommendation TEXT,
    detail TEXT                   -- JSON: overall + by_regime + by_hour
);
CREATE INDEX IF NOT EXISTS idx_scorecards_run ON scorecards(run_ts);

CREATE TABLE IF NOT EXISTS daily_pnl (
    date TEXT NOT NULL,           -- UTC YYYY-MM-DD
    asset TEXT NOT NULL,
    strategy TEXT NOT NULL,
    trades INTEGER DEFAULT 0,
    wins INTEGER DEFAULT 0,
    pnl_gross REAL DEFAULT 0,
    pnl_net REAL DEFAULT 0,
    fees REAL DEFAULT 0,
    updated_ts REAL NOT NULL,
    PRIMARY KEY (date, asset, strategy)
);

CREATE TABLE IF NOT EXISTS smart_wallets (
    wallet TEXT PRIMARY KEY,
    n INTEGER NOT NULL,
    wins INTEGER NOT NULL,
    win_rate REAL,
    ci_low REAL,
    ci_high REAL,
    pnl REAL,
    avg_stake REAL,
    avg_entry_offset_s REAL,
    qualified INTEGER DEFAULT 0,  -- n >= 100 AND ci_low > 0.5 AND pnl > 0
    updated_ts REAL NOT NULL
);

CREATE TABLE IF NOT EXISTS webauthn_credentials (
    credential_id TEXT PRIMARY KEY,   -- base64url of the raw credential id
    public_key TEXT NOT NULL,         -- base64url COSE public key
    sign_count INTEGER NOT NULL DEFAULT 0,
    transports TEXT DEFAULT '',       -- JSON list, informational
    label TEXT DEFAULT '',            -- human label ("Noah's iPhone")
    created_ts REAL NOT NULL
);

CREATE TABLE IF NOT EXISTS portfolio_reports (
    id INTEGER PRIMARY KEY,
    run_ts REAL NOT NULL,
    n_positions INTEGER,
    top_asset TEXT,               -- best-Sharpe active asset (the anchor)
    best_portfolio TEXT,          -- winning config in the selection sim
    all3_net REAL,
    all3_sharpe REAL,
    flags TEXT,                   -- JSON list of structure-break flags
    detail TEXT                   -- JSON full walk-forward result
);
CREATE INDEX IF NOT EXISTS idx_portfolio_reports_ts ON portfolio_reports(run_ts);
-- ts-leading indexes for the retention job: without these, its DELETEs
-- full-scan multi-GB tables while holding the write lock, starving the live
-- writer past its busy_timeout and (previously) killing the flusher.
CREATE INDEX IF NOT EXISTS idx_trade_tape_ts ON trade_tape(ts);
CREATE INDEX IF NOT EXISTS idx_signals_ts ON signals(ts);
CREATE INDEX IF NOT EXISTS idx_book_snapshots_ts ON book_snapshots(ts);
CREATE INDEX IF NOT EXISTS idx_spot_ticks_ts ON spot_ticks(ts);
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
        self._flush_failures = 0
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

    def write_now_sql(self, sql: str, params: tuple = ()) -> None:
        """Synchronous arbitrary write (e.g. UPDATE) with immediate commit."""
        with self._write_lock:
            self._conn.execute(sql, params)
            self._conn.commit()

    def execute_write(self, sql: str, params: tuple = ()) -> int:
        """Synchronous write that returns rows affected (for batched deletes)."""
        with self._write_lock:
            cur = self._conn.execute(sql, params)
            self._conn.commit()
            return cur.rowcount

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
                except (sqlite3.InterfaceError, sqlite3.ProgrammingError,
                        ValueError, OverflowError) as exc:
                    # one unbindable row must not poison the whole batch (or,
                    # via a killed flusher, halt all recording)
                    logger.warning("dropping unbindable %s row: %s", table, exc)
            self._conn.commit()  # may raise OperationalError (locked/disk) — caller retries

    # cap held rows during a sustained write outage so memory can't grow
    # without bound; if we ever hit this the dead-man's switch is already firing
    MAX_BACKLOG = 200_000

    async def run_flusher(self, interval: float = 1.0) -> None:
        """Background task: drain the queue and commit once per interval.

        A transient write failure (SQLite 'database is locked' during the
        nightly retention/VACUUM, or a full disk) must NOT kill this task —
        that would silently stop all recording while the process stays alive.
        On failure the batch is held and retried with backoff; the loop lives.
        """
        backlog: list[tuple[str, dict[str, Any]]] = []
        try:
            while not self._closed or not self._queue.empty() or backlog:
                await asyncio.sleep(interval)
                batch = backlog
                backlog = []
                while not self._queue.empty():
                    batch.append(self._queue.get_nowait())
                if not batch:
                    continue
                try:
                    await asyncio.to_thread(self._flush_batch, batch)
                except sqlite3.OperationalError as exc:
                    backlog = batch[-self.MAX_BACKLOG:]
                    dropped = len(batch) - len(backlog)
                    if self._flush_failures % 10 == 0:
                        logger.error("db flush failed (holding %d rows%s): %s",
                                     len(backlog),
                                     f", DROPPED {dropped} oldest" if dropped else "",
                                     exc)
                    self._flush_failures += 1
                    await asyncio.sleep(min(30.0, interval * 2 ** min(self._flush_failures, 5)))
                else:
                    if self._flush_failures:
                        logger.info("db flush recovered after %d failed attempts",
                                    self._flush_failures)
                    self._flush_failures = 0
        except asyncio.CancelledError:
            batch = backlog
            while not self._queue.empty():
                batch.append(self._queue.get_nowait())
            if batch:
                try:
                    self._flush_batch(batch)
                except Exception as exc:  # noqa: BLE001 — best-effort final drain
                    logger.error("final flush lost %d rows: %s", len(batch), exc)
            raise

    # -------------------------------------------------------------- reads

    def query(self, sql: str, params: tuple = ()) -> list[sqlite3.Row]:
        conn = sqlite3.connect(self.path)
        conn.row_factory = sqlite3.Row
        try:
            return conn.execute(sql, params).fetchall()
        finally:
            conn.close()

    def unsettled_markets(self, asset: str, closed_before_ts: float, limit: int = 20) -> list[str]:
        """Tickers recorded for this asset that closed but have no settlement row.

        Drives the settlement sweep: derived from the DB rather than in-memory
        state, so it survives restarts and heals any window that was missed
        live (e.g. when the next market wasn't yet listed at rollover).
        """
        rows = self.query(
            "SELECT ticker FROM markets WHERE asset = ? AND close_ts IS NOT NULL "
            "AND close_ts < ? AND ticker NOT IN (SELECT market_ticker FROM settlements) "
            "ORDER BY close_ts LIMIT ?",
            (asset, closed_before_ts, limit),
        )
        return [r["ticker"] for r in rows]

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
