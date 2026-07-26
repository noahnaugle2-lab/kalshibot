"""Strategy fingerprints and cross-venue delayed-copy replay tests."""

import pytest

from kalshibot.persistence.db import Database
from kalshibot.smartmoney.intelligence import (
    refresh_copyability_scores,
    refresh_delayed_copy_replays,
    refresh_leaderboard_snapshots,
    refresh_strategy_fingerprints,
)


OPEN = 1_783_000_000.0
CLOSE = OPEN + 900


async def test_leaderboard_snapshot_unions_horizons(tmp_path):
    db = Database(tmp_path / "leaders.db")

    class Client:
        async def get_leaderboard(self, *, category, period, limit):
            return [
                {
                    "rank": "1", "proxyWallet": "0xABC",
                    "userName": f"{period}-winner",
                    "pnl": 1000, "vol": 10_000,
                },
                {
                    "rank": "2", "proxyWallet": f"0x{period}",
                    "userName": None, "pnl": 500, "vol": 5_000,
                },
            ]

    rows, wallets = await refresh_leaderboard_snapshots(
        Client(), db, periods=("DAY", "MONTH"), snapshot_ts=123.0,
    )
    assert rows == 4
    assert wallets == {"0xabc", "0xday", "0xmonth"}
    assert db.query(
        "SELECT COUNT(*) AS n FROM polymarket_leaderboard_snapshots"
    )[0]["n"] == 4
    db.close()


def _market(db, condition, ticker, result="yes"):
    db.write_now("polymarket_markets", {
        "condition_id": condition, "asset": "SOL", "slug": condition,
        "close_ts": CLOSE, "winner": "UP" if result == "yes" else "DOWN",
        "liquidity": 1000, "volume": 5000, "scanned": 1,
        "updated_ts": CLOSE,
    })
    db.write_now("markets", {
        "ticker": ticker, "asset": "SOL", "series_ticker": "KXSOL15M",
        "open_ts": OPEN, "close_ts": CLOSE, "floor_strike": 100,
        "expiration_value": 101 if result == "yes" else 99,
        "result": result, "status": "settled", "raw": "{}",
        "updated_at": CLOSE,
    })
    db.write_now("settlements", {
        "market_ticker": ticker, "asset": "SOL", "result": result,
        "floor_strike": 100, "expiration_value": 101,
        "open_ts": OPEN, "close_ts": CLOSE, "recorded_ts": CLOSE + 30,
        "consistent": 1,
    })


def _event(db, event_id, wallet, condition, side, outcome, price, size, offset):
    db.write_now("polymarket_wallet_trade_events", {
        "event_id": event_id, "condition_id": condition, "asset": "SOL",
        "wallet": wallet, "side": side, "outcome": outcome,
        "price": price, "size": size, "ts": OPEN + offset,
        "observed_ts": OPEN + offset + 1,
    })


def _window(db, wallet, condition, pnl, stake):
    db.write_now("wallet_window_results", {
        "wallet": wallet, "asset": "SOL", "condition_id": condition,
        "close_ts": int(CLOSE), "lean": "UP", "won": int(pnl > 0),
        "pnl": pnl, "stake": stake, "entry_offset_s": 100,
        "lean_600": "UP", "won_600": int(pnl > 0), "computed_ts": CLOSE,
    })


def test_fingerprint_classifies_sequence_not_just_direction(tmp_path):
    db = Database(tmp_path / "fingerprints.db")
    for idx in range(2):
        condition = f"maker-{idx}"
        _market(db, condition, f"KXSOL-{idx}")
        _event(db, f"{idx}-1", "0xmaker", condition, "BUY", "Up", .45, 10, 50)
        _event(db, f"{idx}-2", "0xmaker", condition, "BUY", "Down", .48, 10, 60)
        _event(db, f"{idx}-3", "0xmaker", condition, "SELL", "Up", .50, 5, 100)
        _window(db, "0xmaker", condition, 2, 10)
    _market(db, "directional", "KXSOL-D")
    _event(
        db, "directional-1", "0xdirectional", "directional",
        "BUY", "Up", .40, 20, 40,
    )
    _window(db, "0xdirectional", "directional", 5, 8)

    assert refresh_strategy_fingerprints(db, now=CLOSE + 60) == 2
    rows = {
        row["wallet"]: row
        for row in db.query("SELECT * FROM wallet_strategy_fingerprints")
    }
    assert rows["0xmaker"]["strategy_type"] == "two_sided_market_maker"
    assert rows["0xmaker"]["both_outcomes_rate"] == 1
    assert rows["0xdirectional"]["strategy_type"] == "directional_single_shot"
    assert rows["0xdirectional"]["dominant_outcome_share"] == 1
    db.close()


def test_delayed_copy_replay_uses_future_kalshi_book_and_scores(tmp_path):
    db = Database(tmp_path / "copy.db")
    _market(db, "condition", "KXSOL-C", result="yes")
    _event(
        db, "trade", "0xcopy", "condition", "BUY", "Up", .35, 100, 100,
    )
    _window(db, "0xcopy", "condition", 50, 35)
    db.write_now("book_snapshots", {
        "ts": OPEN + 102.5, "market_ticker": "KXSOL-C", "asset": "SOL",
        "yes_bids": "[]", "no_bids": "[]", "best_yes_bid": .39,
        "best_yes_ask": .40, "mid": .395, "spread": .01,
    })
    refresh_strategy_fingerprints(db, now=CLOSE + 60)
    assert refresh_delayed_copy_replays(
        db, delays=(2,), now=CLOSE + 60,
    ) == 1
    [replay] = db.query("SELECT * FROM wallet_copy_replays")
    assert replay["status"] == "settled"
    assert replay["kalshi_price"] == pytest.approx(.40)
    assert replay["book_ts"] >= replay["target_ts"]
    assert replay["pnl_net"] == pytest.approx(.58)  # 60c payout gain - 2c fee

    assert refresh_copyability_scores(db, now=CLOSE + 60) == 1
    [score] = db.query("SELECT * FROM wallet_copyability_scores")
    assert score["filled"] == 1 and score["wins"] == 1
    assert score["net"] == pytest.approx(.58)
    assert score["rank"] == 1
    db.close()


def test_copy_score_shrinks_lucky_tiny_samples(tmp_path):
    db = Database(tmp_path / "shrink.db")

    def replay(wallet, index, pnl):
        db.write_now("wallet_copy_replays", {
            "replay_id": f"{wallet}-{index}", "computed_ts": CLOSE,
            "wallet": wallet, "asset": "SOL", "condition_id": f"c-{wallet}-{index}",
            "market_ticker": f"M-{index}", "source_trade_ts": OPEN,
            "source_observed_ts": OPEN + 1, "source_outcome": "UP",
            "source_price": .4, "source_size": 10, "delay_seconds": 5,
            "target_ts": OPEN + 5, "book_ts": OPEN + 5.2,
            "intent": "BUY_YES", "kalshi_price": .4, "status": "settled",
            "reason": "test", "settlement_result": "yes", "fees": .02,
            "pnl_net": pnl,
        })

    replay("0xlucky", 0, .60)
    for i in range(10):
        replay("0xrepeatable", i, .10)
    refresh_copyability_scores(db, now=CLOSE)
    rows = {
        row["wallet"]: row
        for row in db.query("SELECT * FROM wallet_copyability_scores")
    }
    assert rows["0xrepeatable"]["rank"] == 1
    assert rows["0xrepeatable"]["copy_score"] > rows["0xlucky"]["copy_score"]
    db.close()
