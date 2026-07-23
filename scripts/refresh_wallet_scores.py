"""Rebuild per-asset wallet copyability rankings from resolved public trades."""

from __future__ import annotations

import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "src"))

from kalshibot.config import PROJECT_ROOT, load_wallet_consensus
from kalshibot.persistence.db import Database
from kalshibot.smartmoney.polymarket import refresh_wallet_asset_scores


def main() -> None:
    config = load_wallet_consensus()
    db = Database(PROJECT_ROOT / "data" / "kalshibot.db")
    try:
        qualified = refresh_wallet_asset_scores(
            db,
            minimum_history=config.minimum_history,
            maximum_entry_seconds=config.maximum_entry_seconds,
            top_n=config.top_n,
        )
        print(f"wallet copyability rankings refreshed: {qualified} qualified")
        for row in db.query(
            "SELECT asset, COUNT(*) AS n FROM wallet_asset_scores "
            "WHERE qualified=1 GROUP BY asset ORDER BY asset"
        ):
            print(f"{row['asset']}: {row['n']}")
    finally:
        db.close()


if __name__ == "__main__":
    main()
