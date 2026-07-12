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
import logging
import sys
import time
from datetime import datetime, timezone
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "src"))

from kalshibot.config import PROJECT_ROOT, load_asset_configs
from kalshibot.evaluation.calibration import run_calibration
from kalshibot.evaluation.scorecard import build_scorecards, persist_scorecards, render_ranking
from kalshibot.persistence.db import Database
from kalshibot.persistence.retention import run_retention
from kalshibot.smartmoney.flow import mine_new_windows
from kalshibot.smartmoney.polymarket import PolymarketClient, scan_asset

WINDOW = 900


async def main() -> int:
    db = Database(PROJECT_ROOT / "data" / "kalshibot.db")
    ASSETS = [a for a, c in load_asset_configs().items() if c.enabled]
    lines: list[str] = [f"KalshiBot nightly — {datetime.now(timezone.utc):%Y-%m-%d %H:%M UTC}", ""]
    failures: list[str] = []

    def step(label: str, fn):
        """Run one nightly step in isolation: a crash in analytics must never
        skip retention (disk-fill protection) or the report file."""
        try:
            return fn()
        except Exception as exc:  # noqa: BLE001 — isolate each step
            import traceback
            logging.getLogger("nightly").error("step %s failed: %s", label, exc)
            failures.append(f"{label}: {exc}")
            lines.append(f"[{label}] FAILED: {exc}")
            lines.append(traceback.format_exc())
            return None

    reports = step("1/5 calibration", lambda: run_calibration(db, ASSETS))
    if reports is not None:
        lines.append(f"[1/5] calibration: {len(reports)} assets scored")

    new_flow = step("2/5 flow mining", lambda: mine_new_windows(db))
    if new_flow is not None:
        lines.append(f"[2/5] flow mining: {new_flow} new pattern outcomes")

    async def _polymarket():
        client = PolymarketClient()
        try:
            now = int(time.time())
            end = (now // WINDOW) * WINDOW - WINDOW
            start = end - int(26 * 3600)
            total_rows = 0
            for asset in ASSETS:
                stats = await scan_asset(client, db, asset, start, end)
                total_rows += stats["wallet_rows"]
            qualified = db.query(
                "SELECT COUNT(*) AS c FROM smart_wallets WHERE qualified = 1"
            )[0]["c"]
            return total_rows, qualified
        finally:
            await client.close()

    try:
        total_rows, qualified = await _polymarket()
        lines.append(f"[3/5] polymarket: {total_rows} wallet rows, {qualified} qualified winners")
    except Exception as exc:  # noqa: BLE001
        logging.getLogger("nightly").error("polymarket step failed: %s", exc)
        failures.append(f"3/5 polymarket: {exc}")
        lines.append(f"[3/5] polymarket FAILED: {exc}")

    cards = step("4/5 scorecards", lambda: build_scorecards(db, kind="shadow")) or []
    if cards:
        step("4/5 persist scorecards", lambda: persist_scorecards(db, cards))
        lines.append(f"[4/5] scorecards: {len(cards)} (asset, strategy) pairs ranked")

    def _portfolio():
        from kalshibot.evaluation.portfolio import (
            persist_portfolio_report, render_portfolio_report, walk_forward_analysis,
        )
        pf = walk_forward_analysis(db)
        persist_portfolio_report(db, pf)
        if pf.get("flags"):
            lines.append("⚠ PORTFOLIO STRUCTURE FLAGS: " + " | ".join(pf["flags"]))
        lines.append(render_portfolio_report(pf))
        # once-a-day Telegram portfolio check: silent when stable, alert on break
        if pf.get("flags"):
            from kalshibot.monitoring.telegram import send_telegram

            alert = (
                "⚠️ KalshiBot — portfolio check\n"
                + "\n".join("• " + f for f in pf["flags"])
                + f"\n\nanchor {pf.get('top_asset')} · best config: "
                + f"{pf.get('best_selection')} · {pf.get('n_positions')} settled pos"
            )
            send_telegram(alert)

    step("portfolio", _portfolio)

    from kalshibot.config import Settings

    def _retention():
        ret = run_retention(db, retention_days=Settings().retention_days)
        lines.append(f"[retention] archived {ret['total_archived']} aged tape rows "
                     f"(> {ret['retention_days']}d), vacuumed={ret['vacuumed']}")

    step("retention", _retention)
    lines.append("")
    ranking = render_ranking(cards) if cards else "(scorecards unavailable)"
    lines.append(ranking)

    if "--no-ai" in sys.argv:
        lines.append("\n[5/5] Claude analysis skipped (--no-ai)")
    elif cards:
        try:
            from kalshibot.decision.proposals import run_nightly_analysis

            analysis, n_proposals = await run_nightly_analysis(
                db, ranking, load_asset_configs()
            )
            lines.append(f"\n[5/5] Claude analysis ({n_proposals} replay-validated "
                         f"proposals written for review):\n{analysis}")
        except Exception as exc:  # noqa: BLE001
            logging.getLogger("nightly").error("claude analysis failed: %s", exc)
            failures.append(f"5/5 claude: {exc}")
            lines.append(f"\n[5/5] Claude analysis FAILED: {exc}")

    if failures:
        lines.insert(1, f"⚠ {len(failures)} STEP(S) FAILED: "
                        + "; ".join(f.split(':')[0] for f in failures))
        try:
            from kalshibot.monitoring.telegram import send_telegram
            send_telegram("⚠️ KalshiBot nightly — steps failed:\n"
                          + "\n".join("• " + f for f in failures))
        except Exception:  # noqa: BLE001
            pass

    text = "\n".join(lines)
    print(text)
    reports_dir = PROJECT_ROOT / "data" / "reports"
    reports_dir.mkdir(parents=True, exist_ok=True)
    path = reports_dir / f"nightly_{time.strftime('%Y%m%d_%H%M%S')}.txt"
    path.write_text(text)
    db.write_now("meta", {"key": "last_nightly_ts", "value": str(time.time())})
    print(f"\nwrote {path}")
    db.close()
    # non-zero exit so the trader's nightly watcher logs+alerts on partial failure
    return 1 if failures else 0


if __name__ == "__main__":
    logging.basicConfig(level=logging.INFO,
                        format="%(asctime)s %(levelname)-7s %(name)s: %(message)s")
    sys.exit(asyncio.run(main()))
