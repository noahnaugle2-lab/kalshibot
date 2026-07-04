"""Persistence round-trip tests: schema, buffered writes, dedup."""

import asyncio

import pytest

from kalshibot.persistence.db import Database


@pytest.fixture()
def db(tmp_path):
    database = Database(tmp_path / "test.db")
    yield database
    database.close()


async def flush(db: Database):
    task = asyncio.create_task(db.run_flusher(interval=0.01))
    await asyncio.sleep(0.1)
    task.cancel()
    try:
        await task
    except asyncio.CancelledError:
        pass


async def test_buffered_write_roundtrip(db):
    db.add("spot_ticks", {"ts": 1.0, "asset": "BTC", "source": "coinbase", "price": 50000.0})
    db.add("spot_ticks", {"ts": 2.0, "asset": "ETH", "source": "coinbase", "price": 3000.0})
    await flush(db)
    rows = db.query("SELECT * FROM spot_ticks ORDER BY ts")
    assert len(rows) == 2
    assert rows[0]["asset"] == "BTC" and rows[0]["price"] == 50000.0


async def test_trade_tape_dedupes_by_trade_id(db):
    row = {
        "trade_id": "t-1", "market_ticker": "M", "asset": "BTC",
        "ts": 1.0, "yes_price": 0.5, "count": 10.0, "taker_side": "yes", "raw": "{}",
    }
    db.add("trade_tape", row)
    db.add("trade_tape", dict(row, ts=2.0))  # same trade_id polled twice
    await flush(db)
    rows = db.query("SELECT * FROM trade_tape")
    assert len(rows) == 1


def test_write_now_upserts(db):
    row = {
        "market_ticker": "M1", "asset": "BTC", "result": "yes",
        "floor_strike": 100.0, "expiration_value": 101.0,
        "open_ts": 0.0, "close_ts": 900.0, "recorded_ts": 1.0, "consistent": 1,
    }
    db.write_now("settlements", row)
    db.write_now("settlements", dict(row, result="no", consistent=0))
    rows = db.query("SELECT * FROM settlements")
    assert len(rows) == 1 and rows[0]["result"] == "no"


def test_counts(db):
    assert db.counts()["spot_ticks"] == 0


def test_unsettled_markets_sweep(db):
    def market(ticker, close_ts):
        return {
            "ticker": ticker, "asset": "BTC", "series_ticker": "KXBTC15M",
            "open_ts": close_ts - 900, "close_ts": close_ts,
            "floor_strike": 100.0, "expiration_value": None,
            "result": "", "status": "active", "raw": "{}", "updated_at": 1.0,
        }

    now = 10_000.0
    db.write_now("markets", market("M-CLOSED-UNSETTLED", now - 300))
    db.write_now("markets", market("M-CLOSED-SETTLED", now - 600))
    db.write_now("markets", market("M-STILL-OPEN", now + 600))
    db.write_now("settlements", {
        "market_ticker": "M-CLOSED-SETTLED", "asset": "BTC", "result": "yes",
        "floor_strike": 100.0, "expiration_value": 101.0,
        "open_ts": now - 1500, "close_ts": now - 600,
        "recorded_ts": now, "consistent": 1,
    })
    # only the closed-but-unsettled market is due for the sweep
    assert db.unsettled_markets("BTC", closed_before_ts=now - 60) == ["M-CLOSED-UNSETTLED"]
    assert db.unsettled_markets("ETH", closed_before_ts=now - 60) == []
