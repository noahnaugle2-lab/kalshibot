"""Walk-forward (out-of-sample) validation of the Layer B wallet signal.

For each recorded Polymarket window, in strict time order:
  1. compute the aggregate lean using ONLY wallets whose qualification
     (n >= threshold, Wilson ci_low > 0.5, pnl > 0) holds on history strictly
     BEFORE this window — the wallet's own stance in this window contributes
     nothing to its qualification;
  2. weight each qualified wallet's stance by (ci_low_prior - 0.5) *
     log10(1 + stake) — the same formula live_lean uses;
  3. score the resulting UP/DOWN call against the window's actual outcome.

Honest limitation, stated up front: stored stances are NET-OF-WINDOW. Live,
we poll mid-window and cannot see a wallet's final stance. The `early`
variant (wallets whose first entry was <= 600s) approximates what would
have been observable in time to trade, but a wallet that entered early can
still flip late; treat `early` as indicative, full-window as upper bound.

Usage: python scripts/validate_layer_b.py [--min-n 100]
"""

from __future__ import annotations

import argparse
import math
import sys
import time
from collections import defaultdict
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "src"))

from kalshibot.config import PROJECT_ROOT
from kalshibot.persistence.db import Database
from kalshibot.smartmoney.polymarket import wilson_interval


def run(db: Database, min_n: int, entry_cutoff_s: float | None,
        causal: bool = False) -> dict:
    rows = db.query(
        "SELECT r.wallet, r.asset, r.condition_id, r.close_ts, r.lean, r.won, "
        "r.pnl, r.stake, r.entry_offset_s, r.lean_600, r.won_600, m.winner "
        "FROM wallet_window_results r "
        "JOIN polymarket_markets m ON m.condition_id = r.condition_id "
        "WHERE m.winner IN ('UP','DOWN') ORDER BY r.close_ts, r.condition_id",
    )
    # group rows into windows, preserving chronological order
    windows: list[tuple[float, str, str, list]] = []
    current_key = None
    for row in rows:
        key = row["condition_id"]
        if key != current_key:
            windows.append((row["close_ts"], key, row["winner"], []))
            current_key = key
        windows[-1][3].append(row)

    stats: dict[str, dict] = defaultdict(lambda: {"n": 0, "wins": 0, "pnl": 0.0})
    calls = hits = neutral = 0
    per_asset: dict[str, list[int]] = defaultdict(lambda: [0, 0])  # asset -> [calls, hits]
    outcome_up = 0
    scored_windows = 0

    for _close_ts, _cid, winner, wrows in windows:
        # 1) lean from wallets qualified on PRIOR history only
        score = 0.0
        for r in wrows:
            lean = r["lean_600"] if causal else r["lean"]
            if lean not in ("UP", "DOWN"):
                continue
            if entry_cutoff_s is not None and (r["entry_offset_s"] or 1e9) > entry_cutoff_s:
                continue
            s = stats[r["wallet"]]
            if s["n"] < min_n or s["pnl"] <= 0:
                continue
            ci_low, _ = wilson_interval(s["wins"], s["n"])
            if ci_low <= 0.5:
                continue
            weight = (ci_low - 0.5) * math.log10(1 + max(0.0, r["stake"] or 0))
            score += weight if lean == "UP" else -weight
        scored_windows += 1
        outcome_up += int(winner == "UP")
        if abs(score) < 1e-9:
            neutral += 1
        else:
            call = "UP" if score > 0 else "DOWN"
            calls += 1
            hit = call == winner
            hits += int(hit)
            asset = wrows[0]["asset"]
            per_asset[asset][0] += 1
            per_asset[asset][1] += int(hit)
        # 2) AFTER scoring, fold this window into every wallet's history.
        # causal mode: qualification tracks the MID-WINDOW record (won_600),
        # so both selection and signal are observable at decision time
        for r in wrows:
            s = stats[r["wallet"]]
            if causal:
                if r["lean_600"] in ("UP", "DOWN"):
                    s["n"] += 1
                    s["wins"] += int(r["won_600"] or 0)
                    s["pnl"] += r["pnl"] or 0.0
            else:
                s["n"] += 1
                s["wins"] += int(r["won"])
                s["pnl"] += r["pnl"] or 0.0

    hit_rate = hits / calls if calls else None
    ci = wilson_interval(hits, calls) if calls else (0, 1)
    base = max(outcome_up, scored_windows - outcome_up) / scored_windows if scored_windows else 0
    return {
        "windows": scored_windows, "neutral": neutral, "calls": calls,
        "hits": hits, "hit_rate": hit_rate, "ci_low": ci[0], "ci_high": ci[1],
        "majority_outcome_base_rate": base,
        "per_asset": {a: {"calls": c, "hits": h, "rate": h / c if c else None}
                      for a, (c, h) in sorted(per_asset.items())},
    }


def fmt(result: dict, label: str) -> str:
    lines = [f"\n=== {label} ==="]
    hr = result["hit_rate"]
    lines.append(
        f"windows {result['windows']}  calls {result['calls']} "
        f"(neutral {result['neutral']})  hit rate "
        f"{hr:.3f}" if hr is not None else "no calls")
    if hr is not None:
        lines.append(
            f"95% CI [{result['ci_low']:.3f}, {result['ci_high']:.3f}]  "
            f"vs majority-outcome base {result['majority_outcome_base_rate']:.3f}")
        lines.append(f"{'ASSET':<6} {'CALLS':>6} {'HIT':>7}")
        for asset, s in result["per_asset"].items():
            rate = f"{s['rate']:.3f}" if s["rate"] is not None else "-"
            lines.append(f"{asset:<6} {s['calls']:>6} {rate:>7}")
    return "\n".join(lines)


def main() -> None:
    parser = argparse.ArgumentParser()
    parser.add_argument("--min-n", type=int, default=100)
    args = parser.parse_args()
    db = Database(PROJECT_ROOT / "data" / "kalshibot.db")
    out = [f"Layer B walk-forward validation — min prior windows {args.min_n}"]
    out.append(fmt(run(db, args.min_n, None),
                   "FULL-WINDOW stance (upper bound; not fully observable live)"))
    out.append(fmt(run(db, args.min_n, 600.0),
                   "EARLY-ENTRY wallets only (first entry <= 600s; tradeable proxy)"))
    out.append(fmt(run(db, args.min_n, None, causal=True),
                   "CAUSAL — minute-10 stance, minute-10 qualification record "
                   "(fully observable live)"))
    text = "\n".join(out)
    print(text)
    path = PROJECT_ROOT / "data" / "reports" / f"layer_b_validation_{time.strftime('%Y%m%d_%H%M%S')}.txt"
    path.write_text(text)
    print(f"\nwrote {path}")
    db.close()


if __name__ == "__main__":
    asyncio = None  # noqa: F841 - no async needed; keep import surface minimal
    main()
