"""Nightly walk-forward portfolio analysis.

Answers, on a schedule, the question we worked out by hand: is the active
roster's edge *structure* stable out-of-sample, or drifting? Specifically —
- per-asset quality across chronological thirds (is the anchor consistent?),
- pairwise correlation (which assets are redundant vs diversifying?),
- an in-sample→out-of-sample ranking test (does picking on the past pick the
  future?),
- an expanding-window selection sim (does holding all vs concentrating vs
  dynamically selecting win?).

It emits `flags` when the structure breaks (an asset's edge goes negative in
the latest window, the top asset changes vs the prior run, or a sub-portfolio
starts clearly beating the full book) so the roster gets revisited on evidence.

Active roster = enabled and not paused (same definition the dashboard uses),
so this auto-tracks whatever the bot is actually trading.
"""

from __future__ import annotations

import json
import statistics as st
import time
from typing import Any

from kalshibot.config import load_asset_configs
from kalshibot.persistence.db import Database

# Below this the walk-forward is too noisy to act on; we still report, but
# suppress break-flags so a thin early sample doesn't cry wolf.
MIN_POSITIONS = 150
WINDOW_S = 900  # 15-min market cadence


def active_assets() -> list[str]:
    return sorted(
        s for s, c in load_asset_configs().items() if c.enabled and not c.paused
    )


def _sharpe(seq: list[float]) -> float:
    return st.mean(seq) / st.pstdev(seq) if len(seq) > 1 and st.pstdev(seq) > 0 else 0.0


def _max_dd(seq: list[float]) -> float:
    cum = peak = worst = 0.0
    for v in seq:
        cum += v
        peak = max(peak, cum)
        worst = max(worst, peak - cum)
    return worst


def _windows(rows: list[dict]) -> dict[int, dict[str, float]]:
    """bucket per 15-min window -> {asset: summed pnl}."""
    w: dict[int, dict[str, float]] = {}
    for r in rows:
        b = int(r["t"] // WINDOW_S)
        w.setdefault(b, {}).setdefault(r["a"], 0.0)
        w[b][r["a"]] += r["v"]
    return w


def _agree(w: dict[int, dict[str, float]], x: str, y: str) -> tuple[int, int]:
    same = tot = 0
    for d in w.values():
        if x in d and y in d and abs(d[x]) > 0.01 and abs(d[y]) > 0.01:
            tot += 1
            same += (d[x] > 0) == (d[y] > 0)
    return same, tot


def _block_asset_stats(rows: list[dict], assets: list[str]) -> dict[str, dict]:
    out = {}
    for a in assets:
        s = [r["v"] for r in rows if r["a"] == a]
        out[a] = {
            "n": len(s),
            "win": round(100 * sum(1 for x in s if x > 0) / len(s), 1) if s else 0.0,
            "net": round(sum(s), 2),
            "sharpe": round(_sharpe(s), 3),
        }
    return out


def _selection_sim(w: dict[int, dict[str, float]], assets: list[str]) -> dict:
    """Expanding-window walk-forward: after a warmup, compare holding all,
    XRP-anchor-only, and dynamic top-k-by-trailing-Sharpe."""
    keys = sorted(w)
    warm = int(len(keys) * 0.4)
    anchor = "XRP" if "XRP" in assets else (assets[0] if assets else None)

    def run(strategy: str) -> dict:
        hist: dict[str, list[float]] = {a: [] for a in assets}
        seq: list[float] = []
        for i, k in enumerate(keys):
            d = w[k]
            if i >= warm:
                elig = [a for a in assets if a in d]
                if elig:
                    if strategy == "all":
                        pick = elig
                    elif strategy == "anchor":
                        pick = [a for a in elig if a == anchor]
                    else:  # top1 / top2 by trailing Sharpe
                        ranked = sorted(
                            [a for a in assets if len(hist[a]) >= 8],
                            key=lambda a: -_sharpe(hist[a]),
                        )
                        topn = 1 if strategy == "top1" else 2
                        pick = [a for a in ranked[:topn] if a in d] or elig
                    if pick:
                        seq.append(sum(d[a] for a in pick) / len(pick))
            for a in assets:
                if a in d:
                    hist[a].append(d[a])
        return {"net": round(sum(seq), 2), "sharpe": round(_sharpe(seq), 3),
                "windows": len(seq)}

    return {s: run(s) for s in ("all", "anchor", "top1", "top2")}


def walk_forward_analysis(db: Database, assets: list[str] | None = None) -> dict:
    assets = assets or active_assets()
    placeholders = ",".join("?" * len(assets)) or "''"
    rows = [
        {"a": r["asset"], "t": r["entry_ts"], "v": r["pnl_net"] or 0.0}
        for r in db.query(
            f"SELECT p.asset, p.entry_ts, p.pnl_net "
            f"FROM sim_positions p JOIN sim_runs r ON r.run_id = p.run_id "
            f"WHERE r.kind = 'shadow' AND p.result IS NOT NULL "
            f"AND p.asset IN ({placeholders}) ORDER BY p.entry_ts",
            tuple(assets),
        )
    ]
    n = len(rows)
    result: dict[str, Any] = {"run_ts": None, "assets": assets, "n_positions": n,
                              "flags": []}
    if n < 30:
        result["note"] = f"insufficient data (n={n})"
        return result

    # 1) stability across chronological thirds
    thirds = []
    for i in range(3):
        blk = rows[i * n // 3:(i + 1) * n // 3]
        s = _block_asset_stats(blk, assets)
        ranked = sorted(assets, key=lambda a: -s[a]["sharpe"])
        thirds.append({"range": [blk[0]["t"], blk[-1]["t"]], "assets": s,
                       "sharpe_rank": ranked})
    result["thirds"] = thirds

    # 2) correlation stability (halves)
    h1, h2 = rows[:n // 2], rows[n // 2:]
    w1, w2 = _windows(h1), _windows(h2)
    corr = {}
    for x, y in [(a, b) for i, a in enumerate(assets) for b in assets[i + 1:]]:
        s1, t1 = _agree(w1, x, y)
        s2, t2 = _agree(w2, x, y)
        corr[f"{x}-{y}"] = {
            "h1": round(100 * s1 / t1, 0) if t1 else None,
            "h2": round(100 * s2 / t2, 0) if t2 else None,
        }
    result["correlation"] = corr

    # 3) in-sample -> out-of-sample portfolio ranking
    IS, OOS = rows[:n // 2], rows[n // 2:]

    def pstats(rs, names):
        s = [r["v"] for r in rs if r["a"] in names]
        return {"net": round(sum(s), 2), "sharpe": round(_sharpe(s), 3),
                "maxdd": round(-_max_dd(s), 2), "n": len(s)}

    combos = [(a,) for a in assets] + [
        tuple(c) for k in range(2, len(assets) + 1)
        for c in __import__("itertools").combinations(assets, k)
    ]
    isoos = []
    for cb in combos:
        isoos.append({"portfolio": "+".join(cb),
                      "is": pstats(IS, set(cb)), "oos": pstats(OOS, set(cb))})
    is_best = max(isoos, key=lambda r: r["is"]["sharpe"])
    oos_best = max(isoos, key=lambda r: r["oos"]["sharpe"])
    result["is_oos"] = {"table": isoos, "is_best": is_best["portfolio"],
                        "oos_best": oos_best["portfolio"],
                        "is_best_generalized": is_best["portfolio"] == oos_best["portfolio"]}

    # 4) expanding-window selection sim
    result["selection"] = _selection_sim(_windows(rows), assets)

    # per-asset overall (for top-asset + flags)
    overall = _block_asset_stats(rows, assets)
    result["overall"] = overall
    top_asset = max(assets, key=lambda a: overall[a]["sharpe"])
    result["top_asset"] = top_asset
    all_net = result["selection"]["all"]["net"]
    best_sel = max(("all", "anchor", "top1", "top2"),
                   key=lambda s: result["selection"][s]["sharpe"])
    result["best_selection"] = best_sel

    # ---- break-flags (suppressed on thin samples) ----
    flags: list[str] = []
    if n >= MIN_POSITIONS:
        # a) an active asset's edge went negative in the LATEST third
        latest = thirds[-1]["assets"]
        for a in assets:
            if latest[a]["n"] >= 5 and (latest[a]["net"] < 0 or latest[a]["sharpe"] < 0):
                flags.append(
                    f"{a} edge negative in latest window "
                    f"(net ${latest[a]['net']:+.2f}, Sharpe {latest[a]['sharpe']:+.3f})"
                )
        # b) top asset changed vs the previous nightly run
        prev = db.query(
            "SELECT top_asset FROM portfolio_reports ORDER BY run_ts DESC LIMIT 1"
        )
        if prev and prev[0]["top_asset"] and prev[0]["top_asset"] != top_asset:
            flags.append(
                f"anchor changed: top asset was {prev[0]['top_asset']}, now {top_asset}"
            )
        # c) a subset now clearly beats holding all (selection sim)
        if best_sel != "all":
            b = result["selection"][best_sel]
            if b["sharpe"] > result["selection"]["all"]["sharpe"] * 1.10:
                flags.append(
                    f"'{best_sel}' now beats hold-all on the walk-forward "
                    f"(Sharpe {b['sharpe']:+.3f} vs {result['selection']['all']['sharpe']:+.3f}) "
                    f"— reconsider roster"
                )
    result["flags"] = flags
    return result


def render_portfolio_report(res: dict) -> str:
    if res.get("note"):
        return f"[portfolio] {res['note']}"
    a = res["assets"]
    L = [f"[portfolio walk-forward] {res['n_positions']} settled positions · roster {a}"]
    if res["flags"]:
        L.append("  ⚠ FLAGS: " + " | ".join(res["flags"]))
    else:
        L.append("  ✓ structure stable — no break flags")
    L.append("  thirds (Sharpe rank): " +
             "  ".join(f"T{i+1}[{'>'.join(t['sharpe_rank'])}]"
                      for i, t in enumerate(res["thirds"])))
    L.append("  correlation same-dir%: " +
             "  ".join(f"{k} {v['h1']}->{v['h2']}" for k, v in res["correlation"].items()))
    io = res["is_oos"]
    L.append(f"  IS-best {io['is_best']} -> OOS-best {io['oos_best']} "
             f"(generalized: {'YES' if io['is_best_generalized'] else 'NO'})")
    sel = res["selection"]
    L.append("  selection sim (OOS net / Sharpe): " +
             "  ".join(f"{s}=${sel[s]['net']:+.0f}/{sel[s]['sharpe']:+.2f}"
                      for s in ("all", "anchor", "top1", "top2")))
    L.append(f"  top asset: {res['top_asset']}   best config: {res['best_selection']}")
    return "\n".join(L)


def persist_portfolio_report(db: Database, res: dict) -> None:
    db.write_now("portfolio_reports", {
        "run_ts": time.time(),
        "n_positions": res.get("n_positions", 0),
        "top_asset": res.get("top_asset"),
        "best_portfolio": res.get("best_selection"),
        "all3_net": res.get("selection", {}).get("all", {}).get("net"),
        "all3_sharpe": res.get("selection", {}).get("all", {}).get("sharpe"),
        "flags": json.dumps(res.get("flags", [])),
        "detail": json.dumps(res, default=str),
    })
