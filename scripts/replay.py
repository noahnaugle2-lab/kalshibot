"""Replay a strategy against the recorded tape.

Usage:
    python scripts/replay.py --asset BTC                       # naive baseline
    python scripts/replay.py --asset ETH --params '{"edge_threshold": 0.02}'
    python scripts/replay.py --all
"""

from __future__ import annotations

import argparse
import json
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "src"))

from kalshibot.config import PROJECT_ROOT, TARGET_ASSETS
from kalshibot.persistence.db import Database
from kalshibot.sim.replay import ReplayEngine
from kalshibot.strategies.base import NaiveEdgeTaker

STRATEGIES = {"naive_edge_taker": NaiveEdgeTaker}


def main() -> None:
    parser = argparse.ArgumentParser()
    parser.add_argument("--asset")
    parser.add_argument("--all", action="store_true")
    parser.add_argument("--strategy", default="naive_edge_taker", choices=STRATEGIES)
    parser.add_argument("--params", default="{}", help="JSON strategy params")
    parser.add_argument("--latency-ms", type=float, default=300.0)
    parser.add_argument("--seed", type=int, default=0)
    parser.add_argument("--db", type=Path, default=PROJECT_ROOT / "data" / "kalshibot.db")
    args = parser.parse_args()

    assets = TARGET_ASSETS if args.all else [args.asset]
    if not assets or assets == [None]:
        parser.error("pass --asset SYMBOL or --all")

    db = Database(args.db)
    engine = ReplayEngine(db, latency_ms=args.latency_ms, seed=args.seed)
    params = json.loads(args.params)

    print(f"{'ASSET':<6} {'WINDOWS':>7} {'TRADES':>6} {'W-L':>7} "
          f"{'PNL GROSS':>10} {'PNL NET':>9} {'FEES':>7}  RUN")
    for asset in assets:
        strategy = STRATEGIES[args.strategy](params)
        r = engine.run(asset, strategy)
        print(f"{asset:<6} {r.windows:>7} {r.trades:>6} {f'{r.wins}-{r.losses}':>7} "
              f"{r.pnl_gross:>10.2f} {r.pnl_net:>9.2f} {r.fees:>7.2f}  {r.run_id}")
    db.close()


if __name__ == "__main__":
    main()
