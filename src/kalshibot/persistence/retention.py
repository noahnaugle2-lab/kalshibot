"""Nightly retention: archive aged tape rows to compressed parquet, prune SQLite.

The tape recorder writes ~0.8 GB/day across the high-volume tables
(trade_tape, signals, book_snapshots, spot_ticks). Left unchecked the hot DB
grows without bound; this job keeps it lean so the box stays on cheap
included storage.

Rows older than `retention_days` are copied to
`data/archive/{table}/{YYYY-MM-DD}.parquet` (zstd-compressed, partitioned by
UTC day), then deleted from SQLite. Parquet is the "cold store" the spec
calls for — read it back with `read_archive()`; nothing is silently lost, and
every archived row count is logged.

Safety: a day is written to a temp file and atomically renamed BEFORE its
rows are deleted, so a crash mid-archive never loses data (worst case: a day
re-archives on the next run, which is idempotent for >retention_days-old
rows since no new rows arrive for a past day). Archive writes are streamed in
bounded batches so a busy tape day cannot exhaust the VPS memory. VACUUM runs
once at the end to return freed pages to the filesystem.
"""

from __future__ import annotations

import logging
import time
from datetime import datetime, timezone
from pathlib import Path

import pyarrow as pa
import pyarrow.parquet as pq

from kalshibot.persistence.db import Database

logger = logging.getLogger(__name__)

# High-volume, append-only tables keyed by a `ts` epoch-seconds column.
ARCHIVABLE_TABLES = ("trade_tape", "signals", "book_snapshots", "spot_ticks")
DEFAULT_RETENTION_DAYS = 30


DELETE_BATCH = 5000  # rows per delete statement — keeps each write-lock hold short
ARCHIVE_BATCH_ROWS = 25_000  # bounded Arrow materialization on the 4 GB VPS


def _utc_day(ts: float) -> str:
    return datetime.fromtimestamp(ts, tz=timezone.utc).strftime("%Y-%m-%d")


def _utc_midnight(ts: float) -> float:
    dt = datetime.fromtimestamp(ts, tz=timezone.utc).replace(
        hour=0, minute=0, second=0, microsecond=0
    )
    return dt.timestamp()


def _delete_range_batched(db: Database, table: str, lo: float, hi: float) -> int:
    """Delete rows in [lo, hi) in small batches so the live writer isn't
    starved. Uses a rowid subquery (portable — no SQLITE_ENABLE_..._LIMIT
    needed) over the ts index, so each statement is a short indexed delete."""
    total = 0
    while True:
        n = db.execute_write(
            f"DELETE FROM {table} WHERE rowid IN "
            f"(SELECT rowid FROM {table} WHERE ts >= ? AND ts < ? ORDER BY ts LIMIT ?)",
            (lo, hi, DELETE_BATCH),
        )
        total += n
        if n < DELETE_BATCH:
            return total


def _columns(db: Database, table: str) -> list[str]:
    rows = db.query(f"PRAGMA table_info({table})")
    return [r["name"] for r in rows]


def archive_table(
    db: Database, table: str, cutoff_ts: float, archive_dir: Path
) -> dict:
    """Archive+prune one table's rows with ts < cutoff_ts. Returns a summary."""
    cols = _columns(db, table)
    if "ts" not in cols:
        return {"table": table, "archived": 0, "days": 0, "skipped": "no ts column"}

    # earliest row below the cutoff — an indexed MIN(ts) (no full scan)
    lo_row = db.query(f"SELECT MIN(ts) AS lo FROM {table} WHERE ts < ?", (cutoff_ts,))
    lo = lo_row[0]["lo"] if lo_row else None
    if lo is None:
        return {"table": table, "archived": 0, "days": 0}

    out_dir = archive_dir / table
    out_dir.mkdir(parents=True, exist_ok=True)
    total = 0
    n_days = 0
    col_list = ", ".join(cols)
    # walk UTC-day buckets from the earliest row up to the cutoff, selecting and
    # deleting by ts RANGE so the ts index is used and strftime never touches a
    # row in the WHERE clause (the old query full-scanned every table per day)
    day_start = _utc_midnight(lo)
    while day_start < cutoff_ts:
        day_end = day_start + 86400
        hi = min(day_end, cutoff_ts)
        day = _utc_day(day_start)
        count_rows = db.query(
            f"SELECT COUNT(*) AS n FROM {table} WHERE ts >= ? AND ts < ?",
            (day_start, hi),
        )
        row_count = count_rows[0]["n"] if count_rows else 0
        if row_count:
            final = out_dir / f"{day}.parquet"
            tmp = out_dir / f".{day}.parquet.tmp"
            if final.exists():
                # A final archive is published before deletes begin. If a prior
                # run died during deletion, the remaining DB rows are already
                # safe in that archive and must not be appended a second time.
                archived = pq.ParquetFile(final).metadata.num_rows
                if archived < row_count:
                    raise RuntimeError(
                        f"existing archive {final} has {archived} rows but "
                        f"database still has {row_count}; refusing to prune"
                    )
            else:
                tmp.unlink(missing_ok=True)
                writer: pq.ParquetWriter | None = None
                cursor = 0
                written = 0
                try:
                    while True:
                        rows = db.query(
                            f"SELECT rowid AS __rowid__, {col_list} FROM {table} "
                            "WHERE ts >= ? AND ts < ? AND rowid > ? "
                            "ORDER BY rowid LIMIT ?",
                            (day_start, hi, cursor, ARCHIVE_BATCH_ROWS),
                        )
                        if not rows:
                            break
                        cursor = rows[-1]["__rowid__"]
                        arrow = pa.table({c: [r[c] for r in rows] for c in cols})
                        if writer is None:
                            writer = pq.ParquetWriter(tmp, arrow.schema, compression="zstd")
                        writer.write_table(arrow)
                        written += len(rows)
                finally:
                    if writer is not None:
                        writer.close()
                if written != row_count:
                    tmp.unlink(missing_ok=True)
                    raise RuntimeError(
                        f"archive row count changed for {table} {day}: "
                        f"expected {row_count}, wrote {written}"
                    )
                tmp.replace(final)  # atomic: rows are safe before deletion
            # Batched indexed deletes keep each write-lock hold short.
            deleted = _delete_range_batched(db, table, day_start, hi)
            total += deleted
            n_days += 1
        day_start = day_end

    logger.info("retention: archived %d %s rows across %d day(s) -> %s",
                total, table, n_days, out_dir)
    return {"table": table, "archived": total, "days": n_days}


def run_retention(
    db: Database,
    retention_days: int = DEFAULT_RETENTION_DAYS,
    archive_dir: Path | None = None,
    now: float | None = None,
    vacuum: bool = True,
) -> dict:
    """Archive+prune all tape tables older than retention_days, then VACUUM."""
    from kalshibot.config import PROJECT_ROOT

    archive_dir = archive_dir or PROJECT_ROOT / "data" / "archive"
    cutoff = (now if now is not None else time.time()) - retention_days * 86400
    summaries = [archive_table(db, t, cutoff, archive_dir) for t in ARCHIVABLE_TABLES]
    total = sum(s["archived"] for s in summaries)
    vacuumed = False
    if total and vacuum:
        # best-effort: VACUUM needs an exclusive lock the live trader may hold.
        # Skipping it only defers returning freed pages to the OS — the DELETEs
        # already freed them for reuse inside the file, so growth stays bounded.
        try:
            db.write_now_sql("VACUUM")
            vacuumed = True
        except Exception as exc:
            logger.warning("retention: VACUUM skipped (db busy): %s", exc)
    return {
        "cutoff_utc": _utc_day(cutoff),
        "retention_days": retention_days,
        "total_archived": total,
        "by_table": summaries,
        "vacuumed": vacuumed,
    }


def read_archive(archive_dir: Path, table: str, day: str) -> pa.Table | None:
    """Read one archived day back (for restore / cold replay). None if absent."""
    path = archive_dir / table / f"{day}.parquet"
    return pq.read_table(path) if path.exists() else None


def restore_day(db: Database, archive_dir: Path, table: str, day: str) -> int:
    """Re-insert an archived day back into SQLite (e.g. to replay old tape)."""
    arrow = read_archive(archive_dir, table, day)
    if arrow is None:
        return 0
    rows = arrow.to_pylist()
    for row in rows:
        db.write_now(table, row)
    logger.info("restored %d %s rows for %s from archive", len(rows), table, day)
    return len(rows)
