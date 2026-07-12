"""Report smart-money sizing contribution from the persistent counterfactual ledger."""

from __future__ import annotations

import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "src"))

from kalshibot.config import PROJECT_ROOT
from kalshibot.persistence.db import Database


def main() -> None:
    db = Database(PROJECT_ROOT / "data" / "kalshibot.db")
    rows = db.query(
        "SELECT asset, COUNT(*) AS trades, "
        "SUM(baseline_cf_pnl_net) AS baseline_net, "
        "SUM(smart_cf_pnl_net) AS smart_net, "
        "SUM(actual_pnl_net) AS actual_net, "
        "SUM(CASE WHEN smart_cf_pnl_net > baseline_cf_pnl_net THEN 1 ELSE 0 END) AS helped "
        "FROM smart_counterfactuals WHERE settled_ts IS NOT NULL "
        "GROUP BY asset ORDER BY asset"
    )
    print("ASSET  TRADES  BASELINE    SMART    DELTA   ACTUAL  HELPED")
    for row in rows:
        baseline = row["baseline_net"] or 0.0
        smart = row["smart_net"] or 0.0
        print(f"{row['asset']:<6} {row['trades']:>6} {baseline:>9.2f} "
              f"{smart:>8.2f} {smart-baseline:>8.2f} "
              f"{(row['actual_net'] or 0):>8.2f} {row['helped']:>7}")
    db.close()


if __name__ == "__main__":
    main()
