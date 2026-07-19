"""Report the mechanical funded-canary gates from the durable SQLite ledger."""

from __future__ import annotations

import argparse
import json
import sqlite3
from collections import defaultdict
from pathlib import Path


def metrics(pnls: list[float]) -> dict[str, float | int | None]:
    wins = sum(1 for pnl in pnls if pnl > 0)
    gross_profit = sum(pnl for pnl in pnls if pnl > 0)
    gross_loss = -sum(pnl for pnl in pnls if pnl < 0)
    profit_factor = gross_profit / gross_loss if gross_loss else None
    equity = peak = max_drawdown = 0.0
    for pnl in pnls:
        equity += pnl
        peak = max(peak, equity)
        max_drawdown = max(max_drawdown, peak - equity)
    return {
        "settled": len(pnls),
        "wins": wins,
        "hit_rate": wins / len(pnls) if pnls else None,
        "net": round(sum(pnls), 4),
        "profit_factor": None if profit_factor is None else round(profit_factor, 4),
        "gross_profit": round(gross_profit, 4),
        "gross_loss": round(gross_loss, 4),
        "max_drawdown": round(max_drawdown, 4),
    }


def one_contract_pnls(rows) -> tuple[list[float], int]:
    """Scale each settled dry-run result to exactly one filled contract."""
    pnls: list[float] = []
    invalid = 0
    for row in rows:
        filled = float(row["expected_filled"] or 0)
        if filled <= 0 or row["hypothetical_pnl_net"] is None:
            invalid += 1
            continue
        pnls.append(float(row["hypothetical_pnl_net"]) / filled)
    return pnls, invalid


def build_report(db_path: Path, since: float, target: int) -> dict:
    conn = sqlite3.connect(db_path)
    conn.row_factory = sqlite3.Row
    try:
        dry = conn.execute(
            "SELECT p.asset,p.market_ticker,p.intent,p.risk_contracts,"
            "o.expected_filled,o.hypothetical_pnl_net "
            "FROM live_proposals p JOIN live_proposal_outcomes o "
            "ON o.proposal_id=p.proposal_id "
            "WHERE p.created_ts>=? AND p.status='dry_run' "
            "AND o.outcome_status='settled' ORDER BY p.created_ts",
            (since,),
        ).fetchall()
        by_asset: dict[str, list[float]] = defaultdict(list)
        for row in dry:
            by_asset[row["asset"]].append(float(row["hypothetical_pnl_net"]))

        shadow = conn.execute(
            "SELECT f.asset,f.market_ticker,f.intent,f.contracts "
            "FROM sim_fills f JOIN sim_runs r ON r.run_id=f.run_id "
            "WHERE r.kind='shadow' AND f.ts>=?",
            (since,),
        ).fetchall()
        shadow_by_market = {
            (row["asset"], row["market_ticker"]): row for row in shadow
        }
        comparable = direction_matches = size_matches = 0
        for row in dry:
            other = shadow_by_market.get((row["asset"], row["market_ticker"]))
            if other is None:
                continue
            comparable += 1
            direction_matches += int(row["intent"] == other["intent"])
            size_matches += int(
                abs(float(row["expected_filled"]) - float(other["contracts"])) < 1e-9
            )

        recon_failures = conn.execute(
            "SELECT COUNT(*) FROM live_reconciliations "
            "WHERE run_ts>=? AND status!='ok'",
            (since,),
        ).fetchone()[0]
        unresolved_orders = conn.execute(
            "SELECT COUNT(*) FROM live_orders WHERE status='submit_unknown'"
        ).fetchone()[0]
        open_positions = conn.execute(
            "SELECT COUNT(*) FROM live_positions WHERE status='open'"
        ).fetchone()[0]
    finally:
        conn.close()

    overall = metrics([float(row["hypothetical_pnl_net"]) for row in dry])
    assets = {asset: metrics(pnls) for asset, pnls in sorted(by_asset.items())}
    normalized_pnls, invalid_normalized_rows = one_contract_pnls(dry)
    one_contract = metrics(normalized_pnls)
    one_contract_assets = {}
    for asset in sorted(by_asset):
        asset_rows = [row for row in dry if row["asset"] == asset]
        asset_pnls, _ = one_contract_pnls(asset_rows)
        one_contract_assets[asset] = metrics(asset_pnls)
    pf = one_contract["profit_factor"]
    comparable_rate = comparable / len(dry) if dry else 0.0
    direction_rate = direction_matches / comparable if comparable else 0.0
    mechanical_gates = {
        "settled_target": overall["settled"] >= target,
        "one_contract_profit_factor_at_least_1_5": (
            (pf is not None and pf >= 1.5)
            or (
                one_contract["gross_profit"] > 0
                and one_contract["gross_loss"] == 0
            )
        ),
        "every_candidate_asset_positive_at_one_contract": bool(
            one_contract_assets
        ) and all(
            item["net"] > 0 for item in one_contract_assets.values()
        ),
        "zero_invalid_one_contract_rows": invalid_normalized_rows == 0,
        "shadow_comparison_coverage_at_least_80pct": comparable_rate >= 0.8,
        "direction_agreement_at_least_90pct": direction_rate >= 0.9,
        "zero_reconciliation_failures": recon_failures == 0,
        "zero_unresolved_live_orders": unresolved_orders == 0,
        "zero_open_live_positions": open_positions == 0,
        "one_contract_drawdown_within_5_dollars": (
            one_contract["max_drawdown"] <= 5
        ),
    }
    return {
        "ready_mechanical": all(mechanical_gates.values()),
        "since": since,
        "target": target,
        "remaining": max(0, target - int(overall["settled"])),
        "dry_run": overall,
        "by_asset": assets,
        "one_contract_projection": one_contract,
        "one_contract_by_asset": one_contract_assets,
        "normalization": {
            "method": "hypothetical_pnl_net divided by expected_filled",
            "invalid_settled_rows": invalid_normalized_rows,
        },
        "shadow_agreement": {
            "comparable": comparable,
            "coverage": round(comparable_rate, 4),
            "direction_matches": direction_matches,
            "direction_rate": round(direction_rate, 4),
            "size_matches": size_matches,
            "size_rate_diagnostic": round(size_matches / comparable, 4) if comparable else 0,
        },
        "safety": {
            "reconciliation_failures": recon_failures,
            "unresolved_live_orders": unresolved_orders,
            "open_live_positions": open_positions,
        },
        "gates": mechanical_gates,
        "manual_gates": [
            "held-out replay profitable",
            "operator selects exactly one asset",
            "demo place/reconcile/cancel passes",
            "operator signs off before creating the live environment file",
        ],
    }


def main() -> None:
    parser = argparse.ArgumentParser()
    parser.add_argument("--db", type=Path, default=Path("data/kalshibot.db"))
    parser.add_argument("--since", type=float, required=True, help="UTC epoch baseline")
    parser.add_argument("--target", type=int, default=50)
    args = parser.parse_args()
    report = build_report(args.db, args.since, args.target)
    print(json.dumps(report, indent=2, sort_keys=True, allow_nan=False))
    raise SystemExit(0 if report["ready_mechanical"] else 1)


if __name__ == "__main__":
    main()
