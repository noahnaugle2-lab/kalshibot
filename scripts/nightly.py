"""Nightly evaluation job (development-order step 7).

Runs, in order:
  1. calibration scoring (model vs market vs settlements)
  2. Layer A flow-pattern mining on new settled windows
  3. Polymarket Layer B scan (last 26h, all assets) + wallet re-aggregation
  4. scorecard build + ranking, persisted to the scorecards table
  5. text ranking report to data/reports/

Schedule via cron/launchd (deployment phase), e.g.:
  15 4 * * * cd /path/to/Kalshi && .venv/bin/python scripts/nightly.py

The Claude-analysis step (proposals) is added in phase 8 and appends here.
"""

from __future__ import annotations

import asyncio
import sys
import time
from datetime import datetime, timezone
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "src"))

from kalshibot.config import PROJECT_ROOT, TARGET_ASSETS
from kalshibot.evaluation.calibration import run_calibration
from kalshibot.evaluation.scorecard import build_scorecards, persist_scorecards, render_ranking
from kalshibot.persistence.db import Database
from kalshibot.smartmoney.flow import mine_new_windows
from kalshibot.smartmoney.polymarket import PolymarketClient, scan_asset

WINDOW = 900


async def main() -> None:
    db = Database(PROJECT_ROOT / "data" / "kalshibot.db")
    lines: list[str] = [f"KalshiBot nightly — {datetime.now(timezone.utc):%Y-%m-%d %H:%M UTC}", ""]

    reports = run_calibration(db, TARGET_ASSETS)
    lines.append(f"[1/5] calibration: {len(reports)} assets scored")

    new_flow = mine_new_windows(db)
    lines.append(f"[2/5] flow mining: {new_flow} new pattern outcomes")

    client = PolymarketClient()
    try:
        now = int(time.time())
        end = (now // WINDOW) * WINDOW - WINDOW
        start = end - int(26 * 3600)
        total_rows = 0
        for asset in TARGET_ASSETS:
            stats = await scan_asset(client, db, asset, start, end)
            total_rows += stats["wallet_rows"]
        qualified = db.query(
            "SELECT COUNT(*) AS c FROM smart_wallets WHERE qualified = 1"
        )[0]["c"]
        lines.append(f"[3/5] polymarket: {total_rows} wallet rows, {qualified} qualified winners")
    finally:
        await client.close()

    cards = build_scorecards(db, kind="shadow")
    persist_scorecards(db, cards)
    lines.append(f"[4/5] scorecards: {len(cards)} (asset, strategy) pairs ranked")
    lines.append("")
    ranking = render_ranking(cards)
    lines.append(ranking)

    if "--no-ai" in sys.argv:
        lines.append("\n[5/5] Claude analysis skipped (--no-ai)")
    else:
        from kalshibot.config import load_asset_configs
        from kalshibot.decision.proposals import run_nightly_analysis

        analysis, n_proposals = await run_nightly_analysis(
            db, ranking, load_asset_configs()
        )
        lines.append(f"\n[5/5] Claude analysis ({n_proposals} replay-validated "
                     f"proposals written for review):\n{analysis}")

    text = "\n".join(lines)
    print(text)
    reports_dir = PROJECT_ROOT / "data" / "reports"
    reports_dir.mkdir(parents=True, exist_ok=True)
    path = reports_dir / f"nightly_{time.strftime('%Y%m%d_%H%M%S')}.txt"
    path.write_text(text)
    print(f"\nwrote {path}")
    db.close()


if __name__ == "__main__":
    asyncio.run(main())
