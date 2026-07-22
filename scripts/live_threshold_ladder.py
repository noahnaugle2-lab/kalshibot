"""Rank stricter thresholds from settled production-path dry-run proposals.

This is a counterfactual subset analysis: it can test stricter thresholds than
the deployed baseline because every included proposal was actually observed,
filled by the production-path simulator, and settled. It cannot estimate looser
thresholds that would have created proposals absent from the ledger.
"""

from __future__ import annotations

import argparse
import json
import sqlite3
import time
from collections.abc import Iterable
from pathlib import Path


BTC_THRESHOLDS = (0.0008, 0.0010, 0.0012)
EDGE_THRESHOLDS = (0.02, 0.03, 0.04)


def metric(rows: Iterable[dict]) -> dict:
    values = [float(row["pnl"] or 0) for row in rows]
    gains = sum(value for value in values if value > 0)
    losses = -sum(value for value in values if value < 0)
    return {
        "n": len(values),
        "wins": sum(value > 0 for value in values),
        "net": round(sum(values), 2),
        "profit_factor": round(gains / losses, 3) if losses else None,
    }


def load_rows(db_path: Path, since_ts: float) -> list[dict]:
    with sqlite3.connect(f"file:{db_path}?mode=ro", uri=True) as db:
        db.row_factory = sqlite3.Row
        source = db.execute(
            "SELECT p.created_ts,p.asset,p.intent,p.snapshot,"
            "o.hypothetical_pnl_net AS pnl "
            "FROM live_proposals p JOIN live_proposal_outcomes o "
            "ON o.proposal_id=p.proposal_id "
            "WHERE p.created_ts>=? AND p.status='dry_run' "
            "AND o.outcome_status='settled' AND p.asset IN ('SOL','XRP') "
            "ORDER BY p.created_ts",
            (since_ts,),
        )
        rows = []
        for raw in source:
            row = dict(raw)
            snapshot = json.loads(row.pop("snapshot"))
            edge_key = "edge_yes_net" if row["intent"] == "BUY_YES" else "edge_no_net"
            row.update(
                btc_move=abs(float(snapshot.get("btc_ret_30s") or 0)),
                edge=float(snapshot.get(edge_key) or 0),
                regime=str(snapshot.get("regime") or "UNKNOWN"),
            )
            rows.append(row)
        return rows


def rank(rows: list[dict], validation_fraction: float) -> dict:
    if not rows:
        return {"source_rows": 0, "candidates": []}
    split_index = max(1, int(len(rows) * (1 - validation_fraction)))
    split_ts = rows[split_index]["created_ts"] if split_index < len(rows) else rows[-1]["created_ts"]
    regime_sets = {
        "SOL": (("MID",), ("LATE",), ("MID", "LATE")),
        "XRP": (("EARLY",), ("MID",), ("EARLY", "MID")),
    }
    candidates = []
    for asset, regimes_options in regime_sets.items():
        asset_rows = [row for row in rows if row["asset"] == asset]
        for regimes in regimes_options:
            for btc_threshold in BTC_THRESHOLDS:
                for edge_threshold in EDGE_THRESHOLDS:
                    selected = [
                        row for row in asset_rows
                        if row["regime"] in regimes
                        and row["btc_move"] >= btc_threshold
                        and row["edge"] >= edge_threshold
                    ]
                    train = metric(row for row in selected if row["created_ts"] < split_ts)
                    validation = metric(
                        row for row in selected if row["created_ts"] >= split_ts
                    )
                    candidates.append({
                        "asset": asset,
                        "regimes": list(regimes),
                        "btc_threshold": btc_threshold,
                        "edge_threshold": edge_threshold,
                        "all": metric(selected),
                        "train": train,
                        "validation": validation,
                    })
    candidates.sort(
        key=lambda row: (
            min(
                row["train"]["profit_factor"] or 0,
                row["validation"]["profit_factor"] or 0,
            ),
            row["validation"]["net"],
            row["all"]["n"],
        ),
        reverse=True,
    )
    return {"source_rows": len(rows), "split_ts": split_ts, "candidates": candidates}


def main() -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument("--db", type=Path, default=Path("data/kalshibot.db"))
    parser.add_argument("--days", type=int, default=7)
    parser.add_argument("--validation-fraction", type=float, default=0.33)
    parser.add_argument("--limit", type=int, default=20)
    args = parser.parse_args()
    if args.days < 1:
        parser.error("--days must be positive")
    if not 0.1 <= args.validation_fraction <= 0.5:
        parser.error("--validation-fraction must be between 0.1 and 0.5")
    result = rank(
        load_rows(args.db, time.time() - args.days * 86400),
        args.validation_fraction,
    )
    result["candidates"] = result["candidates"][: max(1, args.limit)]
    print(json.dumps(result, indent=2))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
