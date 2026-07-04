"""Layer A smart-money: mine the recorded Kalshi tape for flow patterns that
preceded correct outcomes.

Kalshi is anonymous — no wallets, no trader IDs — so patterns in aggregate
flow are the proxy for repeat winners. Each pattern is a simple, interpretable
rule that, given one window's tape, emits a lean (UP / DOWN / None). Patterns
are scored against settlements window by window; per-(pattern, asset) hit
rates persist to `flow_patterns` with full per-window outcomes in
`flow_pattern_outcomes`, so rolling re-validation can bench any pattern whose
recent hit rate reverts to coin-flip. A pattern only ever influences live
decisions after clearing validation on held-out windows (phase 6 wiring).

IMPORTANT (future): once the bot trades, its own fills must be excluded from
all inputs here. Kalshi prints are anonymous, so exclusion works by matching
our recorded fills (ts/price/size from the fills table) against tape prints.
The `own_fill_filter` hook exists now so the wiring is in place; it is a
no-op until there are own fills to exclude.
"""

from __future__ import annotations

import json
import time
from dataclasses import dataclass
from typing import Callable, Iterable

from kalshibot.persistence.db import Database

# window phases in seconds since open (15-min window)
PHASE_MID = (300.0, 600.0)
PHASE_LATE = (600.0, 810.0)  # ends at the 90s settlement blackout

IMBALANCE_THRESHOLD = 0.25
LARGE_PRINT_PERCENTILE = 0.90
MIN_PRINTS_FOR_LARGE = 20
DEPTH_IMBALANCE_THRESHOLD = 0.20
MIN_VOLUME = 1.0

OwnFillFilter = Callable[[dict], bool]  # True -> drop this print (it's ours)


@dataclass
class Print:
    ts: float
    yes_price: float
    count: float
    taker_side: str  # "yes" | "no"


@dataclass
class WindowTape:
    market_ticker: str
    asset: str
    open_ts: float
    close_ts: float
    result: str  # "yes" | "no"
    prints: list[Print]
    depth_imbalances: list[tuple[float, float]]  # (ts, (yes-no)/(yes+no) near touch)


def _phase_prints(tape: WindowTape, phase: tuple[float, float]) -> list[Print]:
    lo, hi = tape.open_ts + phase[0], tape.open_ts + phase[1]
    return [p for p in tape.prints if lo <= p.ts < hi]


def _imbalance_lean(prints: Iterable[Print], threshold: float) -> str | None:
    yes_vol = sum(p.count for p in prints if p.taker_side == "yes")
    no_vol = sum(p.count for p in prints if p.taker_side == "no")
    total = yes_vol + no_vol
    if total < MIN_VOLUME:
        return None
    imbalance = (yes_vol - no_vol) / total
    if imbalance >= threshold:
        return "UP"
    if imbalance <= -threshold:
        return "DOWN"
    return None


def pattern_taker_imbalance_mid(tape: WindowTape) -> str | None:
    """Aggressive taker flow one-sided during the mid window (tight book)."""
    return _imbalance_lean(_phase_prints(tape, PHASE_MID), IMBALANCE_THRESHOLD)


def pattern_late_aggression(tape: WindowTape) -> str | None:
    """One-sided taker flow in the late window, before the blackout."""
    return _imbalance_lean(_phase_prints(tape, PHASE_LATE), IMBALANCE_THRESHOLD)


def pattern_large_prints(tape: WindowTape) -> str | None:
    """Direction of unusually large prints (>= p90 size for this window)."""
    if len(tape.prints) < MIN_PRINTS_FOR_LARGE:
        return None
    sizes = sorted(p.count for p in tape.prints)
    cutoff = sizes[int(LARGE_PRINT_PERCENTILE * (len(sizes) - 1))]
    large = [p for p in tape.prints if p.count >= cutoff]
    return _imbalance_lean(large, IMBALANCE_THRESHOLD)


def pattern_depth_imbalance_mid(tape: WindowTape) -> str | None:
    """Persistent one-sided resting depth near the touch during mid window."""
    lo, hi = tape.open_ts + PHASE_MID[0], tape.open_ts + PHASE_MID[1]
    values = [v for ts, v in tape.depth_imbalances if lo <= ts < hi]
    if len(values) < 30:  # ~1 minute of 2s snapshots
        return None
    avg = sum(values) / len(values)
    if avg >= DEPTH_IMBALANCE_THRESHOLD:
        return "UP"
    if avg <= -DEPTH_IMBALANCE_THRESHOLD:
        return "DOWN"
    return None


PATTERNS: dict[str, Callable[[WindowTape], str | None]] = {
    "taker_imbalance_mid": pattern_taker_imbalance_mid,
    "late_aggression": pattern_late_aggression,
    "large_prints": pattern_large_prints,
    "depth_imbalance_mid": pattern_depth_imbalance_mid,
}


def _depth_imbalance_from_book(yes_bids_json: str, no_bids_json: str) -> float | None:
    def depth_near_touch(levels: list, cents: float = 0.02) -> float:
        if not levels:
            return 0.0
        best = float(levels[0][0])
        return sum(float(q) for p, q in levels if float(p) >= best - cents)

    yes_levels = json.loads(yes_bids_json)
    no_levels = json.loads(no_bids_json)
    yes_d, no_d = depth_near_touch(yes_levels), depth_near_touch(no_levels)
    total = yes_d + no_d
    return (yes_d - no_d) / total if total > 0 else None


def load_window_tape(
    db: Database,
    market_ticker: str,
    asset: str,
    open_ts: float,
    close_ts: float,
    result: str,
    own_fill_filter: OwnFillFilter | None = None,
) -> WindowTape:
    print_rows = db.query(
        "SELECT ts, yes_price, count, taker_side, raw FROM trade_tape "
        "WHERE market_ticker = ? ORDER BY ts, id",
        (market_ticker,),
    )
    prints = [
        Print(r["ts"], r["yes_price"] or 0.0, r["count"] or 0.0, r["taker_side"] or "")
        for r in print_rows
        if own_fill_filter is None or not own_fill_filter(dict(r))
    ]
    book_rows = db.query(
        "SELECT ts, yes_bids, no_bids FROM book_snapshots "
        "WHERE market_ticker = ? ORDER BY ts, id",
        (market_ticker,),
    )
    depth = []
    for r in book_rows:
        v = _depth_imbalance_from_book(r["yes_bids"], r["no_bids"])
        if v is not None:
            depth.append((r["ts"], v))
    return WindowTape(market_ticker, asset, open_ts, close_ts, result, prints, depth)


def mine_new_windows(
    db: Database, own_fill_filter: OwnFillFilter | None = None
) -> int:
    """Score every settled window not yet scored, then refresh aggregates.

    Returns the number of newly scored (pattern, window) outcomes.
    """
    windows = db.query(
        "SELECT s.market_ticker, s.asset, s.open_ts, s.close_ts, s.result "
        "FROM settlements s WHERE s.result IN ('yes','no') AND s.open_ts IS NOT NULL "
        "AND s.market_ticker NOT IN "
        "(SELECT DISTINCT market_ticker FROM flow_pattern_outcomes) "
        "ORDER BY s.close_ts",
    )
    new_outcomes = 0
    now = time.time()
    for w in windows:
        tape = load_window_tape(
            db, w["market_ticker"], w["asset"], w["open_ts"], w["close_ts"],
            w["result"], own_fill_filter,
        )
        for name, detector in PATTERNS.items():
            lean = detector(tape)
            if lean is None:
                continue
            correct = (lean == "UP") == (w["result"] == "yes")
            db.write_now("flow_pattern_outcomes", {
                "pattern": name, "asset": w["asset"],
                "market_ticker": w["market_ticker"], "lean": lean,
                "result": w["result"], "correct": int(correct),
                "close_ts": w["close_ts"], "computed_ts": now,
            })
            new_outcomes += 1
    _refresh_aggregates(db, now)
    return new_outcomes


def _refresh_aggregates(db: Database, now: float) -> None:
    thirty_days_ago = now - 30 * 86400
    rows = db.query(
        "SELECT pattern, asset, COUNT(*) AS n, SUM(correct) AS hits, "
        "SUM(CASE WHEN close_ts >= ? THEN 1 ELSE 0 END) AS n_30d, "
        "SUM(CASE WHEN close_ts >= ? THEN correct ELSE 0 END) AS hits_30d "
        "FROM flow_pattern_outcomes GROUP BY pattern, asset",
        (thirty_days_ago, thirty_days_ago),
    )
    for r in rows:
        db.write_now("flow_patterns", {
            "pattern": r["pattern"], "asset": r["asset"],
            "n": r["n"], "hits": r["hits"],
            "hit_rate": r["hits"] / r["n"] if r["n"] else None,
            "n_30d": r["n_30d"], "hits_30d": r["hits_30d"],
            "hit_rate_30d": r["hits_30d"] / r["n_30d"] if r["n_30d"] else None,
            "status": "candidate",  # promotion to active happens in phase 6
            "updated_ts": now,
        })
