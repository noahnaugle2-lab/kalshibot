"""Layer B tests: Wilson CI, wallet stance reconstruction, qualification."""

import pytest

from kalshibot.persistence.db import Database
from kalshibot.smartmoney.polymarket import (
    PMTrade,
    PolymarketMarket,
    copyable_consensus_lean,
    refresh_wallet_asset_scores,
    refresh_wallet_records,
    updown_slug,
    wallet_windows,
    wilson_interval,
)

T_OPEN = 1_783_000_000.0


def test_updown_slug_uses_window_open():
    # slug ts = window OPEN (verified from live trades); arg is the CLOSE
    assert updown_slug("BTC", 1783201500) == "btc-updown-15m-1783200600"


# ----------------------------------------------------------------- wilson

def test_wilson_interval_known_value():
    # 60/100: Wilson 95% CI ~ (0.502, 0.691)
    lo, hi = wilson_interval(60, 100)
    assert lo == pytest.approx(0.502, abs=0.002)
    assert hi == pytest.approx(0.691, abs=0.002)


def test_wilson_small_sample_wide():
    lo, hi = wilson_interval(5, 5)  # 100% on n=5 must NOT qualify
    assert lo < 0.6  # lower bound well below a qualifying threshold
    assert hi == 1.0


def test_wilson_zero_n():
    assert wilson_interval(0, 0) == (0.0, 1.0)


# ------------------------------------------------------- stance rebuilding

def trade(wallet, side, outcome, price, size, ts_offset):
    return PMTrade(wallet, side, outcome, price, size, T_OPEN + ts_offset)


def test_wallet_stance_and_pnl_win():
    trades = [
        trade("0xa", "BUY", "Up", 0.50, 100, 60),
        trade("0xa", "SELL", "Up", 0.60, 40, 300),  # partial exit
    ]
    [w] = wallet_windows(trades, winner="UP", window_open_ts=T_OPEN)
    assert w.lean == "UP" and w.won
    # cash: -50 + 24 = -26; 60 winning shares redeem = +60 -> pnl 34
    assert w.pnl == pytest.approx(34.0)
    assert w.stake == pytest.approx(50.0)
    assert w.entry_offset_s == pytest.approx(60.0)


def test_wallet_stance_down_loss():
    trades = [trade("0xb", "BUY", "Down", 0.45, 50, 120)]
    [w] = wallet_windows(trades, winner="UP", window_open_ts=T_OPEN)
    assert w.lean == "DOWN" and not w.won
    assert w.pnl == pytest.approx(-22.5)  # stake lost, shares worthless


def test_wallet_hedged_or_dust_excluded():
    trades = [
        trade("0xc", "BUY", "Up", 0.5, 30, 10),
        trade("0xc", "BUY", "Down", 0.5, 30, 20),  # fully hedged -> no stance
        trade("0xd", "BUY", "Up", 0.5, 0.5, 10),   # dust position
    ]
    assert wallet_windows(trades, "UP", T_OPEN) == []


def test_net_seller_counts_as_opposite_side():
    # sells more Up than bought elsewhere -> net short Up == DOWN stance
    trades = [
        trade("0xe", "BUY", "Up", 0.5, 10, 10),
        trade("0xe", "SELL", "Up", 0.55, 40, 200),
    ]
    [w] = wallet_windows(trades, winner="DOWN", window_open_ts=T_OPEN)
    assert w.lean == "DOWN" and w.won


# ------------------------------------------------------------ aggregation

def test_refresh_wallet_records_qualification(tmp_path):
    db = Database(tmp_path / "pm.db")

    def results(wallet, n, wins, pnl_each):
        for i in range(n):
            db.write_now("wallet_window_results", {
                "wallet": wallet, "asset": "BTC",
                "condition_id": f"{wallet}-{i}",
                "close_ts": 1000 + i, "lean": "UP", "won": int(i < wins),
                "pnl": pnl_each, "stake": 10.0, "entry_offset_s": 100.0,
                "lean_600": "UP", "won_600": int(i < wins),
                "computed_ts": 1.0,
            })

    results("0xaaa", 120, 80, 1.0)    # solid n, ci_low>0.5, profitable -> qualified
    results("0xbbb", 10, 9, 1.0)      # 90% raw win rate but n=10 -> no
    results("0xccc", 150, 150, -0.01) # 100% "wins" at 99c, LOSES money -> no
    qualified = refresh_wallet_records(db)
    assert qualified == 1
    rows = {r["wallet"]: r for r in db.query("SELECT * FROM smart_wallets")}
    assert rows["0xaaa"]["qualified"] == 1
    assert rows["0xaaa"]["ci_low"] > 0.5
    assert rows["0xbbb"]["qualified"] == 0
    assert rows["0xccc"]["qualified"] == 0  # the late-window sweeper trap
    db.close()


def test_copyability_ranking_rewards_profitable_payoff_not_hit_rate(tmp_path):
    db = Database(tmp_path / "copyable.db")
    now = 10_000.0
    for i in range(120):
        # Exactly 50% causal accuracy, but winners make twice what losers lose.
        db.write_now("wallet_window_results", {
            "wallet": "0xpayoff", "asset": "SOL", "condition_id": f"p-{i}",
            "close_ts": int(now - 120 + i), "lean": "UP", "won": int(i % 2 == 0),
            "pnl": 2.0 if i % 2 == 0 else -1.0, "stake": 10.0,
            "entry_offset_s": 90.0, "lean_600": "UP", "won_600": int(i % 2 == 0),
            "computed_ts": now,
        })
        # High hit rate and positive PnL, but too late to copy in a 15m market.
        db.write_now("wallet_window_results", {
            "wallet": "0xlate", "asset": "SOL", "condition_id": f"l-{i}",
            "close_ts": int(now - 120 + i), "lean": "UP", "won": 1,
            "pnl": 0.01, "stake": 10.0, "entry_offset_s": 700.0,
            "lean_600": "UP", "won_600": 1, "computed_ts": now,
        })
    qualified = refresh_wallet_asset_scores(db, now=now)
    assert qualified == 1
    payoff = db.query(
        "SELECT * FROM wallet_asset_scores WHERE wallet='0xpayoff' AND asset='SOL'"
    )[0]
    late = db.query(
        "SELECT * FROM wallet_asset_scores WHERE wallet='0xlate' AND asset='SOL'"
    )[0]
    assert payoff["qualified"] == 1 and payoff["rank"] == 1
    assert payoff["win_rate"] == pytest.approx(0.5)
    assert payoff["profit_factor"] == pytest.approx(2.0)
    assert late["qualified"] == 0
    db.close()


async def test_copyable_consensus_requires_diversified_early_majority(tmp_path):
    db = Database(tmp_path / "consensus.db")
    for rank in range(1, 11):
        db.write_now("wallet_asset_scores", {
            "wallet": f"0x{rank}", "asset": "SOL", "rank": rank,
            "n": 200, "wins": 120, "win_rate": 0.6, "pnl": 1000 - rank,
            "stake": 10_000, "roi": 0.1, "profit_factor": 1.5,
            "max_drawdown": None, "avg_entry_offset_s": 60,
            "copy_score": 10 - rank / 10, "qualified": 1, "updated_ts": T_OPEN,
        })

    trades = [
        trade(f"0x{rank}", "BUY", "Up" if rank <= 7 else "Down", 0.5, 10, 60)
        for rank in range(1, 11)
    ]

    class Client:
        async def get_updown_market(self, asset, close_ts):
            return PolymarketMarket(
                condition_id="condition", slug="sol-updown", asset=asset,
                close_ts=close_ts, closed=False, winner=None,
            )

        async def get_trades(self, condition_id):
            return trades

    result = await copyable_consensus_lean(
        Client(), db, "SOL", int(T_OPEN + 900), minimum_active_wallets=8,
        minimum_effective_wallets=8, minimum_dominant_share=0.65,
        observed_ts=T_OPEN + 90,
    )
    assert result["eligible"] is True
    assert result["lean"] == "UP"
    assert result["wallets"] == 10
    assert result["effective_wallets"] >= 8
    assert result["dominant_share"] >= 0.65
    assert db.query("SELECT COUNT(*) AS n FROM polymarket_wallet_trade_events")[0]["n"] == 10
    db.close()
