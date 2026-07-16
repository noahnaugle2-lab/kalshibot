"""Retention: archive aged rows to parquet, prune SQLite, restore round-trip."""

import time

import pytest

import kalshibot.persistence.retention as retention
from kalshibot.persistence.db import Database
from kalshibot.persistence.retention import (
    read_archive,
    restore_day,
    run_retention,
)

DAY = 86400


@pytest.fixture()
def db(tmp_path):
    d = Database(tmp_path / "r.db")
    yield d
    d.close()


def _seed(db, table, ts, **extra):
    base = {"asset": "BTC", "ts": ts}
    if table == "trade_tape":
        base.update({"trade_id": f"t{ts}", "market_ticker": "M", "yes_price": 0.5,
                     "count": 10.0, "taker_side": "yes", "raw": "{}"})
    elif table == "spot_ticks":
        base.update({"source": "coinbase", "price": 50000.0})
    base.update(extra)
    db.write_now(table, base)


def test_archives_old_prunes_hot_keeps_recent(db, tmp_path):
    now = time.time()
    archive = tmp_path / "archive"
    # 3 old trades (40 days) across two days, 2 recent (1 day)
    _seed(db, "trade_tape", now - 40 * DAY)
    _seed(db, "trade_tape", now - 40 * DAY + 5)
    _seed(db, "trade_tape", now - 39 * DAY)
    _seed(db, "trade_tape", now - 1 * DAY)
    _seed(db, "spot_ticks", now - 40 * DAY)

    result = run_retention(db, retention_days=30, archive_dir=archive, now=now)

    assert result["total_archived"] == 4  # 3 trades + 1 tick
    # hot DB keeps only the recent trade
    remaining = db.query("SELECT COUNT(*) AS n FROM trade_tape")[0]["n"]
    assert remaining == 1
    assert db.query("SELECT COUNT(*) AS n FROM spot_ticks")[0]["n"] == 0
    # parquet files exist, partitioned by UTC day
    files = list((archive / "trade_tape").glob("*.parquet"))
    assert len(files) == 2  # two distinct old days
    assert result["vacuumed"] is True


def test_noop_when_nothing_aged(db, tmp_path):
    now = time.time()
    _seed(db, "trade_tape", now - 2 * DAY)  # newer than 30d
    result = run_retention(db, retention_days=30, archive_dir=tmp_path / "a", now=now)
    assert result["total_archived"] == 0
    assert db.query("SELECT COUNT(*) AS n FROM trade_tape")[0]["n"] == 1


def test_restore_round_trips(db, tmp_path):
    now = time.time()
    archive = tmp_path / "archive"
    old_ts = now - 40 * DAY
    _seed(db, "trade_tape", old_ts, trade_id="unique-1")
    day = time.strftime("%Y-%m-%d", time.gmtime(old_ts))

    run_retention(db, retention_days=30, archive_dir=archive, now=now)
    assert db.query("SELECT COUNT(*) AS n FROM trade_tape")[0]["n"] == 0

    # archived data is readable and restorable
    assert read_archive(archive, "trade_tape", day).num_rows == 1
    restored = restore_day(db, archive, "trade_tape", day)
    assert restored == 1
    row = db.query("SELECT trade_id, ts FROM trade_tape")[0]
    assert row["trade_id"] == "unique-1"
    assert abs(row["ts"] - old_ts) < 1


def test_rerun_is_idempotent(db, tmp_path):
    now = time.time()
    archive = tmp_path / "archive"
    _seed(db, "trade_tape", now - 40 * DAY)
    run_retention(db, retention_days=30, archive_dir=archive, now=now)
    # second run finds nothing to archive (rows already pruned)
    result = run_retention(db, retention_days=30, archive_dir=archive, now=now)
    assert result["total_archived"] == 0


def test_archive_streams_multiple_batches(db, tmp_path, monkeypatch):
    now = time.time()
    archive = tmp_path / "archive"
    monkeypatch.setattr(retention, "ARCHIVE_BATCH_ROWS", 2)
    releases = []
    monkeypatch.setattr(retention, "_release_archive_memory", lambda: releases.append(True))
    for offset in range(5):
        _seed(db, "trade_tape", now - 40 * DAY + offset)

    result = run_retention(
        db, retention_days=30, archive_dir=archive, now=now, vacuum=False,
    )

    assert result["total_archived"] == 5
    day = time.strftime("%Y-%m-%d", time.gmtime(now - 40 * DAY))
    assert read_archive(archive, "trade_tape", day).num_rows == 5
    assert db.query("SELECT COUNT(*) AS n FROM trade_tape")[0]["n"] == 0
    assert releases == [True]


def test_existing_archive_finishes_interrupted_delete_without_duplicates(db, tmp_path):
    now = time.time()
    archive = tmp_path / "archive"
    old_ts = now - 40 * DAY
    _seed(db, "trade_tape", old_ts)
    day = time.strftime("%Y-%m-%d", time.gmtime(old_ts))

    # Simulate the safe crash point: final archive exists, source row remains.
    rows = db.query("SELECT asset, ts, trade_id, market_ticker, yes_price, count, "
                    "taker_side, raw FROM trade_tape")
    import pyarrow as pa
    import pyarrow.parquet as pq
    out = archive / "trade_tape"
    out.mkdir(parents=True)
    pq.write_table(pa.table({k: [rows[0][k]] for k in rows[0].keys()}), out / f"{day}.parquet")

    result = run_retention(
        db, retention_days=30, archive_dir=archive, now=now, vacuum=False,
    )

    assert result["total_archived"] == 1
    assert read_archive(archive, "trade_tape", day).num_rows == 1
    assert db.query("SELECT COUNT(*) AS n FROM trade_tape")[0]["n"] == 0
