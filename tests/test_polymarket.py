"""Layer B tests: Wilson CI, wallet stance reconstruction, qualification."""

import pytest

from kalshibot.persistence.db import Database
from kalshibot.smartmoney.polymarket import (
    PMTrade,
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
