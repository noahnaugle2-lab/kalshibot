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
rows since no new rows arrive for a past day). VACUUM runs once at the end to
return freed pages to the filesystem.
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


def _utc_day(ts: float) -> str:
    return datetime.fromtimestamp(ts, tz=timezone.utc).strftime("%Y-%m-%d")


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

    # distinct UTC days present below the cutoff — bounds memory to one day
    day_rows = db.query(
        f"SELECT DISTINCT strftime('%Y-%m-%d', ts, 'unixepoch') AS d "
        f"FROM {table} WHERE ts < ? ORDER BY d",
        (cutoff_ts,),
    )
    days = [r["d"] for r in day_rows if r["d"]]
    if not days:
        return {"table": table, "archived": 0, "days": 0}

    out_dir = archive_dir / table
    out_dir.mkdir(parents=True, exist_ok=True)
    total = 0
    col_list = ", ".join(cols)
    for day in days:
        rows = db.query(
            f"SELECT {col_list} FROM {table} "
            f"WHERE ts < ? AND strftime('%Y-%m-%d', ts, 'unixepoch') = ?",
            (cutoff_ts, day),
        )
        if not rows:
            continue
        columns = {c: [r[c] for r in rows] for c in cols}
        arrow = pa.table(columns)
        final = out_dir / f"{day}.parquet"
        tmp = out_dir / f".{day}.parquet.tmp"
        if final.exists():
            # merge with what's already archived for this day (re-run safety)
            existing = pq.read_table(final)
            arrow = pa.concat_tables([existing, arrow.cast(existing.schema)])
        pq.write_table(arrow, tmp, compression="zstd")
        tmp.replace(final)  # atomic: rows are safe on disk before we delete
        # delete this day's rows immediately — chunks the delete per day so the
        # live trader's writes interleave instead of blocking on one huge lock
        db.write_now_sql(
            f"DELETE FROM {table} WHERE ts < ? "
            f"AND strftime('%Y-%m-%d', ts, 'unixepoch') = ?",
            (cutoff_ts, day),
        )
        total += len(rows)

    logger.info("retention: archived %d %s rows across %d day(s) -> %s",
                total, table, len(days), out_dir)
    return {"table": table, "archived": total, "days": len(days)}


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
