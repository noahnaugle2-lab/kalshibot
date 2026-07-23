"""Scan resolved Polymarket updown-15m windows and build wallet records.

Usage:
    python scripts/polymarket_scan.py --asset BTC --hours 12
    python scripts/polymarket_scan.py --all --hours 24     # nightly backfill
"""

from __future__ import annotations

import argparse
import asyncio
import sys
import time
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "src"))

from kalshibot.config import PROJECT_ROOT, TARGET_ASSETS
from kalshibot.persistence.db import Database
from kalshibot.smartmoney.polymarket import (
    MIN_QUALIFYING_WINDOWS,
    PolymarketClient,
    refresh_wallet_records,
    scan_asset,
)

WINDOW = 900


async def main() -> None:
    parser = argparse.ArgumentParser()
    parser.add_argument("--asset")
    parser.add_argument("--all", action="store_true")
    parser.add_argument("--hours", type=float, default=12.0)
    parser.add_argument("--db", type=Path, default=PROJECT_ROOT / "data" / "kalshibot.db")
    args = parser.parse_args()
    assets = TARGET_ASSETS if args.all else [args.asset]
    if not assets or assets == [None]:
        parser.error("pass --asset SYMBOL or --all")

    now = int(time.time())
    end = (now // WINDOW) * WINDOW - WINDOW      # last fully closed window
    start = end - int(args.hours * 3600)

    db = Database(args.db)
    client = PolymarketClient()
    try:
        print(f"{'ASSET':<6} {'CHECKED':>7} {'RESOLVED':>8} {'MISSING':>7} {'WALLET ROWS':>11}")
        for asset in assets:
            s = await scan_asset(client, db, asset, start, end)
            print(f"{asset:<6} {s['windows']:>7} {s['resolved']:>8} "
                  f"{s['missing']:>7} {s['wallet_rows']:>11}")
        refresh_wallet_records(db)
    finally:
        await client.close()

    top = db.query(
        "SELECT wallet, n, wins, win_rate, ci_low, pnl, qualified FROM smart_wallets "
        "ORDER BY qualified DESC, ci_low DESC LIMIT 10"
    )
    total = db.query("SELECT COUNT(*) AS c, SUM(qualified) AS q FROM smart_wallets")[0]
    print(f"\nwallets tracked: {total['c']}, qualified repeat winners "
          f"(n>={MIN_QUALIFYING_WINDOWS}, ci_low>0.5): {total['q'] or 0}")
    print(f"{'WALLET':<44} {'N':>4} {'WIN%':>6} {'CI-LOW':>6} {'PNL':>9} Q")
    for w in top:
        print(f"{w['wallet']:<44} {w['n']:>4} {w['win_rate']*100:>5.1f} "
              f"{w['ci_low']:>6.3f} {w['pnl']:>9.2f} {'*' if w['qualified'] else ''}")
    db.close()


if __name__ == "__main__":
    asyncio.run(main())
