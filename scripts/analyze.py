"""Historical analysis pass (development-order step 4).

Scores model vs market calibration against settlements and mines the tape
for flow patterns. Run ad hoc or nightly; results persist to
calibration_reports / flow_patterns and a text report lands in data/reports/.

Usage: python scripts/analyze.py
"""

from __future__ import annotations

import sys
import time
from datetime import datetime, timezone
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "src"))

from kalshibot.config import PROJECT_ROOT, TARGET_ASSETS
from kalshibot.evaluation.calibration import run_calibration
from kalshibot.persistence.db import Database
from kalshibot.smartmoney.flow import mine_new_windows


def fmt(x: float | None, places: int = 3) -> str:
    return "-" if x is None else f"{x:.{places}f}"


def main() -> None:
    db = Database(PROJECT_ROOT / "data" / "kalshibot.db")
    lines: list[str] = []
    out = lines.append

    out(f"KalshiBot analysis — {datetime.now(timezone.utc):%Y-%m-%d %H:%M UTC}")
    out("")
    out("CALIBRATION (tradeable regimes only; lower Brier = better calibrated)")
    out(f"{'ASSET':<6} {'WINDOWS':>7} {'SNAPS':>6} {'BRIER(model)':>12} "
        f"{'BRIER(market)':>13} {'HIT':>6} {'EDGE-HIT':>8} {'EDGE-N':>6}")
    reports = run_calibration(db, TARGET_ASSETS)
    for r in sorted(reports, key=lambda r: (r.overall.brier_model or 1)):
        marker = " *" if (
            r.overall.brier_model is not None
            and r.overall.brier_market is not None
            and r.overall.brier_model < r.overall.brier_market
        ) else ""
        out(f"{r.asset:<6} {r.n_windows:>7} {r.n_snapshots:>6} "
            f"{fmt(r.overall.brier_model):>12} {fmt(r.overall.brier_market):>13} "
            f"{fmt(r.overall.hit_rate):>6} {fmt(r.edge_hit_rate):>8} "
            f"{r.edge_calls:>6}{marker}")
    out("  * model better calibrated than market (exploitability signal)")
    out("")

    new = mine_new_windows(db)
    out(f"FLOW PATTERNS ({new} new window outcomes scored)")
    out(f"{'PATTERN':<22} {'ASSET':<6} {'N':>4} {'HIT RATE':>8}  STATUS")
    for row in db.query(
        "SELECT * FROM flow_patterns ORDER BY pattern, asset"
    ):
        out(f"{row['pattern']:<22} {row['asset']:<6} {row['n']:>4} "
            f"{fmt(row['hit_rate']):>8}  {row['status']}")
    out("")
    out("NOTE: hit rates below ~100 samples are noise; patterns stay "
        "'candidate' until validated on held-out windows (phase 6).")

    text = "\n".join(lines)
    print(text)
    reports_dir = PROJECT_ROOT / "data" / "reports"
    reports_dir.mkdir(parents=True, exist_ok=True)
    path = reports_dir / f"analysis_{time.strftime('%Y%m%d_%H%M%S')}.txt"
    path.write_text(text)
    print(f"\nwrote {path}")
    db.close()


if __name__ == "__main__":
    main()
