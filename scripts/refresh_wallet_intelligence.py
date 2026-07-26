"""Refresh public wallet leaderboards, sequences, fingerprints, and replays."""

from __future__ import annotations

import argparse
import asyncio
import sys
import time
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "src"))

from kalshibot.config import PROJECT_ROOT, load_asset_configs, load_wallet_consensus
from kalshibot.persistence.db import Database
from kalshibot.smartmoney.intelligence import (
    capture_leader_trade_sequences,
    refresh_leaderboard_snapshots,
    refresh_wallet_intelligence_analytics,
)
from kalshibot.smartmoney.polymarket import PolymarketClient


async def main() -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument(
        "--backfill-hours", type=int, default=6,
        help="resolved matching 15-minute windows to fetch (default: 6)",
    )
    parser.add_argument(
        "--all-assets", action="store_true",
        help="capture every configured Polymarket/Kalshi asset, not only consensus assets",
    )
    args = parser.parse_args()

    db = Database(PROJECT_ROOT / "data" / "kalshibot.db")
    client = PolymarketClient()
    try:
        leaderboard_rows, wallets = await refresh_leaderboard_snapshots(client, db)
        configs = load_asset_configs()
        if args.all_assets:
            assets = [asset for asset, cfg in configs.items() if cfg.enabled]
        else:
            assets = list(load_wallet_consensus().assets)
        end = (int(time.time()) // 900) * 900 - 900
        start = end - max(1, args.backfill_hours) * 3600
        captured = await capture_leader_trade_sequences(
            client, db, assets, start, end, wallets,
        )
        result = refresh_wallet_intelligence_analytics(
            db, maximum_price=load_wallet_consensus().maximum_price,
        )
        print(
            f"leaderboard_rows={leaderboard_rows} wallets={len(wallets)} "
            f"captured_events={captured} fingerprints={result.fingerprints} "
            f"replays={result.replays} scores={result.copyability_scores}"
        )
        return 0
    finally:
        await client.close()
        db.close()


if __name__ == "__main__":
    raise SystemExit(asyncio.run(main()))
