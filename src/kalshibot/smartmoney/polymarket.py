"""Layer B smart-money: Polymarket repeat-winner tracking (on-chain, real IDs).

Polymarket runs `{asset}-updown-15m-{close_epoch}` markets — direct
equivalents of Kalshi's 15-minute windows — and every position is tied to a
public proxy wallet. This module:

1. scans resolved updown windows (deterministic slugs, no search needed),
2. reconstructs each wallet's net stance per window from the trade log
   (BUY adds, SELL subtracts, per outcome token) plus PnL and entry timing,
3. maintains `smart_wallets`: per-wallet records with Wilson confidence
   intervals — a wallet qualifies as a tracked repeat winner only with
   >= MIN_QUALIFYING_WINDOWS resolved windows AND a CI lower bound > 0.5
   AND positive cumulative PnL. The PnL gate is load-bearing: the first
   live scan surfaced wallets with 100% win rates and NEGATIVE PnL —
   late-window sweepers buying the near-certain side at 99c. They are
   "right" every window and carry no information,
4. polls the live window's trades for tracked wallets and aggregates a lean
   weighted by each wallet's track record and stake.

APIs (public, read-only): gamma-api for market metadata/resolution,
data-api for wallet-attributed trades. Volumes on these markets are still
modest; the machinery is built now so records accumulate as they grow.
"""

from __future__ import annotations

import asyncio
import hashlib
import logging
import math
import time
from dataclasses import dataclass, field

import httpx

from kalshibot.persistence.db import Database

logger = logging.getLogger(__name__)

GAMMA_BASE = "https://gamma-api.polymarket.com"
DATA_BASE = "https://data-api.polymarket.com"

WINDOW_SECONDS = 900
MIN_QUALIFYING_WINDOWS = 100  # spec: statistical significance floor
WILSON_Z = 1.96
REQUEST_DELAY_S = 0.15  # politeness between paginated calls
TRADES_PAGE = 500

# Kalshi asset symbol -> polymarket slug prefix (same symbols, lowercased)
SLUG_PREFIX = {a: a.lower() for a in
               ("BTC", "ETH", "SOL", "ZEC", "HYPE", "XRP", "DOGE", "BNB", "NEAR")}


def updown_slug(asset: str, close_ts: int) -> str:
    """Slug for the window CLOSING at close_ts.

    Measured from live trades (2026-07-05): the slug timestamp is the window
    OPEN, not the close — a wallet's first trade on ...-15m-T arrived 7s
    after T. Getting this wrong shifts every entry-timing number by +900s
    and makes live polls hit the NEXT (empty) window.
    """
    return f"{SLUG_PREFIX[asset]}-updown-15m-{close_ts - WINDOW_SECONDS}"


def wilson_interval(wins: int, n: int, z: float = WILSON_Z) -> tuple[float, float]:
    """Wilson score interval for a binomial proportion."""
    if n == 0:
        return 0.0, 1.0
    p = wins / n
    denom = 1 + z**2 / n
    center = (p + z**2 / (2 * n)) / denom
    margin = (z / denom) * math.sqrt(p * (1 - p) / n + z**2 / (4 * n**2))
    return max(0.0, center - margin), min(1.0, center + margin)


@dataclass
class PMTrade:
    wallet: str
    side: str          # BUY | SELL
    outcome: str       # Up | Down
    price: float
    size: float
    ts: float
    trade_id: str | None = None


@dataclass
class WalletWindow:
    wallet: str
    lean: str                  # UP | DOWN (net of full window)
    won: bool
    pnl: float
    stake: float
    entry_offset_s: float      # seconds after window open of first entry
    lean_600: str | None = None  # stance as of minute 10 — what live polling sees
    won_600: bool | None = None


@dataclass
class PolymarketMarket:
    condition_id: str
    slug: str
    asset: str
    close_ts: int
    closed: bool
    winner: str | None         # UP | DOWN | None (unresolved)
    liquidity: float = 0.0
    volume: float = 0.0


class PolymarketClient:
    def __init__(self, timeout: float = 15.0) -> None:
        self._http = httpx.AsyncClient(timeout=timeout)

    async def close(self) -> None:
        await self._http.aclose()

    async def get_updown_market(self, asset: str, close_ts: int) -> PolymarketMarket | None:
        slug = updown_slug(asset, close_ts)
        r = await self._http.get(f"{GAMMA_BASE}/events", params={"slug": slug})
        r.raise_for_status()
        events = r.json()
        if not events or not events[0].get("markets"):
            return None
        m = events[0]["markets"][0]
        winner = None
        try:
            outcomes = m.get("outcomes")
            prices = m.get("outcomePrices")
            if isinstance(outcomes, str):
                import json as _json
                outcomes = _json.loads(outcomes)
                prices = _json.loads(prices)
            if m.get("closed") and outcomes and prices:
                idx = max(range(len(prices)), key=lambda i: float(prices[i]))
                if float(prices[idx]) > 0.99:
                    winner = outcomes[idx].upper()  # UP | DOWN
        except (ValueError, TypeError) as exc:
            logger.warning("winner parse failed for %s: %s", slug, exc)
        return PolymarketMarket(
            condition_id=m["conditionId"], slug=slug, asset=asset,
            close_ts=close_ts, closed=bool(m.get("closed")), winner=winner,
            liquidity=float(m.get("liquidityNum") or 0),
            volume=float(m.get("volumeNum") or 0),
        )

    async def get_trades(self, condition_id: str) -> list[PMTrade]:
        trades: list[PMTrade] = []
        offset = 0
        while True:
            r = await self._http.get(
                f"{DATA_BASE}/trades",
                params={"market": condition_id, "limit": TRADES_PAGE, "offset": offset},
            )
            r.raise_for_status()
            page = r.json() or []
            for t in page:
                if not t.get("proxyWallet"):
                    continue
                trades.append(PMTrade(
                    wallet=t["proxyWallet"].lower(),
                    side=(t.get("side") or "").upper(),
                    outcome=(t.get("outcome") or "").capitalize(),
                    price=float(t.get("price") or 0),
                    size=float(t.get("size") or 0),
                    ts=float(t.get("timestamp") or 0),
                    trade_id=(t.get("transactionHash") or t.get("id")),
                ))
            if len(page) < TRADES_PAGE:
                return trades
            offset += TRADES_PAGE
            await asyncio.sleep(REQUEST_DELAY_S)


# --------------------------------------------------------------- analytics

def wallet_windows(
    trades: list[PMTrade], winner: str, window_open_ts: float
) -> list[WalletWindow]:
    """Reconstruct each wallet's stance and outcome for one resolved window."""
    by_wallet: dict[str, list[PMTrade]] = {}
    for t in trades:
        by_wallet.setdefault(t.wallet, []).append(t)

    results: list[WalletWindow] = []
    for wallet, ts_list in by_wallet.items():
        shares = {"Up": 0.0, "Down": 0.0}
        shares_600 = {"Up": 0.0, "Down": 0.0}
        cash = 0.0
        stake = 0.0
        first_entry: float | None = None
        cutoff_600 = window_open_ts + 600
        for t in sorted(ts_list, key=lambda x: x.ts):
            if t.outcome not in shares or t.side not in ("BUY", "SELL"):
                continue
            signed = t.size if t.side == "BUY" else -t.size
            shares[t.outcome] += signed
            if t.ts <= cutoff_600:
                shares_600[t.outcome] += signed
            cash -= signed * t.price
            if t.side == "BUY":
                stake += t.size * t.price
                if first_entry is None:
                    first_entry = t.ts
        net = shares["Up"] - shares["Down"]
        if abs(net) < 1.0 or first_entry is None:  # no meaningful stance
            continue
        lean = "UP" if net > 0 else "DOWN"
        net_600 = shares_600["Up"] - shares_600["Down"]
        lean_600 = None if abs(net_600) < 1.0 else ("UP" if net_600 > 0 else "DOWN")
        payout = shares["Up"] if winner == "UP" else shares["Down"]
        pnl = cash + max(0.0, payout)  # winning shares redeem at $1
        results.append(WalletWindow(
            wallet=wallet, lean=lean, won=(lean == winner),
            pnl=round(pnl, 4), stake=round(stake, 4),
            entry_offset_s=max(0.0, first_entry - window_open_ts),
            lean_600=lean_600,
            won_600=None if lean_600 is None else (lean_600 == winner),
        ))
    return results


# ----------------------------------------------------------------- scanner

async def scan_asset(
    client: PolymarketClient,
    db: Database,
    asset: str,
    start_close_ts: int,
    end_close_ts: int,
) -> dict[str, int]:
    """Scan resolved updown windows in [start, end], skipping already-scanned."""
    stats = {"windows": 0, "resolved": 0, "wallet_rows": 0, "missing": 0}
    for close_ts in range(start_close_ts, end_close_ts + 1, WINDOW_SECONDS):
        already = db.query(
            "SELECT scanned FROM polymarket_markets WHERE asset=? AND close_ts=?",
            (asset, close_ts),
        )
        if already and already[0]["scanned"]:
            continue
        stats["windows"] += 1
        try:
            market = await client.get_updown_market(asset, close_ts)
        except httpx.HTTPError as exc:
            logger.warning("%s: gamma fetch failed @%s: %s", asset, close_ts, exc)
            continue
        if market is None:
            stats["missing"] += 1
            continue
        if not market.closed or market.winner is None:
            continue  # unresolved; a later scan picks it up
        try:
            trades = await client.get_trades(market.condition_id)
        except httpx.HTTPError as exc:
            logger.warning("%s: trades fetch failed @%s: %s", asset, close_ts, exc)
            continue
        window_open = close_ts - WINDOW_SECONDS
        rows = wallet_windows(trades, market.winner, window_open)
        for w in rows:
            db.write_now("wallet_window_results", {
                "wallet": w.wallet, "asset": asset,
                "condition_id": market.condition_id, "close_ts": close_ts,
                "lean": w.lean, "won": int(w.won), "pnl": w.pnl,
                "stake": w.stake, "entry_offset_s": w.entry_offset_s,
                "lean_600": w.lean_600,
                "won_600": None if w.won_600 is None else int(w.won_600),
                "computed_ts": time.time(),
            })
        db.write_now("polymarket_markets", {
            "condition_id": market.condition_id, "asset": asset,
            "slug": market.slug, "close_ts": close_ts, "winner": market.winner,
            "liquidity": market.liquidity, "volume": market.volume,
            "scanned": 1, "updated_ts": time.time(),
        })
        stats["resolved"] += 1
        stats["wallet_rows"] += len(rows)
        await asyncio.sleep(REQUEST_DELAY_S)
    return stats


def refresh_wallet_records(db: Database) -> int:
    """Rebuild smart_wallets aggregates from wallet_window_results."""
    # qualification uses the minute-10 (causal) record — the only record the
    # live poll can act on, and the one the walk-forward validation passed
    rows = db.query(
        "SELECT wallet, COUNT(*) AS n, SUM(won_600) AS wins, SUM(pnl) AS pnl, "
        "AVG(stake) AS avg_stake, AVG(entry_offset_s) AS avg_entry "
        "FROM wallet_window_results WHERE lean_600 IS NOT NULL GROUP BY wallet",
    )
    now = time.time()
    qualified = 0
    for r in rows:
        ci_low, ci_high = wilson_interval(r["wins"], r["n"])
        is_qualified = int(
            r["n"] >= MIN_QUALIFYING_WINDOWS
            and ci_low > 0.5
            and (r["pnl"] or 0) > 0  # being right at 99c is not information
        )
        qualified += is_qualified
        db.write_now("smart_wallets", {
            "wallet": r["wallet"], "n": r["n"], "wins": r["wins"],
            "win_rate": r["wins"] / r["n"], "ci_low": ci_low, "ci_high": ci_high,
            "pnl": r["pnl"], "avg_stake": r["avg_stake"],
            "avg_entry_offset_s": r["avg_entry"],
            "qualified": is_qualified, "updated_ts": now,
        })
    refresh_wallet_asset_scores(db, now=now)
    return qualified


def refresh_wallet_asset_scores(
    db: Database,
    *,
    minimum_history: int = 100,
    maximum_entry_seconds: float = 300.0,
    top_n: int = 100,
    now: float | None = None,
) -> int:
    """Build per-asset rankings around copyable profit instead of win rate.

    Only the trailing 30 days are used. A wallet can qualify near a 50% hit
    rate when its entry prices create positive PnL, ROI, and profit factor.
    Ranking is deterministic and is later consumed only by the read-only
    wallet-consensus experiment.
    """
    now = time.time() if now is None else now
    cutoff = now - 30 * 86400
    db.execute_write("DELETE FROM wallet_asset_scores")
    rows = db.query(
        "SELECT wallet, asset, COUNT(*) AS n, SUM(won_600) AS wins, "
        "SUM(pnl) AS pnl, SUM(stake) AS stake, AVG(entry_offset_s) AS avg_entry, "
        "SUM(CASE WHEN pnl > 0 THEN pnl ELSE 0 END) AS gross_profit, "
        "-SUM(CASE WHEN pnl < 0 THEN pnl ELSE 0 END) AS gross_loss "
        "FROM wallet_window_results WHERE lean_600 IS NOT NULL AND close_ts >= ? "
        "GROUP BY wallet, asset",
        (cutoff,),
    )
    by_asset: dict[str, list[dict]] = {}
    prepared: list[dict] = []
    for row in rows:
        n = int(row["n"] or 0)
        pnl = float(row["pnl"] or 0.0)
        stake = float(row["stake"] or 0.0)
        roi = pnl / stake if stake > 0 else 0.0
        gross_loss = float(row["gross_loss"] or 0.0)
        gross_profit = float(row["gross_profit"] or 0.0)
        profit_factor = gross_profit / gross_loss if gross_loss > 0 else (
            99.0 if gross_profit > 0 else 0.0
        )
        avg_entry = float(row["avg_entry"] or 0.0)
        qualifies = (
            n >= minimum_history
            and pnl > 0
            and roi > 0
            and profit_factor > 1.0
            and avg_entry <= maximum_entry_seconds
        )
        # Profit and ROI are the primary evidence. Sample size, payoff quality,
        # and early entry improve the score but are deliberately capped so a
        # single high-turnover wallet cannot dominate the live vote.
        copy_score = 0.0
        if qualifies:
            copy_score = (
                math.log1p(pnl)
                * min(1.0, max(0.0, roi))
                * min(2.0, math.sqrt(n / minimum_history))
                * min(1.0, profit_factor / 2.0)
                * max(0.25, 1.0 - avg_entry / 600.0)
            )
        item = {
            "wallet": row["wallet"], "asset": row["asset"], "rank": None,
            "n": n, "wins": int(row["wins"] or 0),
            "win_rate": (row["wins"] or 0) / n if n else 0.0,
            "pnl": pnl, "stake": stake, "roi": roi,
            "profit_factor": profit_factor, "max_drawdown": None,
            "avg_entry_offset_s": avg_entry, "copy_score": copy_score,
            "qualified": int(qualifies), "updated_ts": now,
        }
        prepared.append(item)
        if qualifies:
            by_asset.setdefault(row["asset"], []).append(item)

    # Stream each asset's qualifying histories in bounded wallet batches. This
    # avoids materializing the full ledger while allowing drawdown to penalize
    # the ranking, not merely appear as a display-only statistic.
    for asset, items in by_asset.items():
        by_wallet = {item["wallet"]: item for item in items}
        for offset in range(0, len(items), 100):
            batch = items[offset:offset + 100]
            placeholders = ",".join("?" for _ in batch)
            pnl_rows = db.query(
                f"SELECT wallet, pnl FROM wallet_window_results WHERE asset=? "
                f"AND close_ts >= ? AND wallet IN ({placeholders}) "
                "ORDER BY wallet, close_ts, id",
                (asset, cutoff, *(item["wallet"] for item in batch)),
            )
            state: dict[str, tuple[float, float, float]] = {}
            for row in pnl_rows:
                equity, peak, drawdown = state.get(row["wallet"], (0.0, 0.0, 0.0))
                equity += float(row["pnl"] or 0.0)
                peak = max(peak, equity)
                drawdown = max(drawdown, peak - equity)
                state[row["wallet"]] = (equity, peak, drawdown)
            for wallet, (_, _, drawdown) in state.items():
                item = by_wallet[wallet]
                item["max_drawdown"] = drawdown
                item["copy_score"] /= 1.0 + drawdown / max(item["pnl"], 1.0)
        items.sort(key=lambda x: (-x["copy_score"], -x["pnl"], x["wallet"]))
        for rank, item in enumerate(items, start=1):
            item["rank"] = rank
            if rank > top_n:
                # Preserve the score for research, but only the configured top
                # cohort is eligible for the runtime consensus.
                item["qualified"] = 0

    db.write_many("wallet_asset_scores", prepared)
    return sum(item["qualified"] for item in prepared)


def _trade_event_id(condition_id: str, trade: PMTrade) -> str:
    raw = (
        f"{trade.trade_id or ''}|{condition_id}|{trade.wallet}|{trade.side}|{trade.outcome}|"
        f"{trade.price:.10f}|{trade.size:.10f}|{trade.ts:.6f}"
    )
    return hashlib.sha256(raw.encode()).hexdigest()


async def copyable_consensus_lean(
    client: PolymarketClient,
    db: Database,
    asset: str,
    close_ts: int,
    *,
    top_n: int = 100,
    maximum_entry_seconds: float = 300.0,
    minimum_active_wallets: int = 8,
    minimum_effective_wallets: float = 8.0,
    minimum_dominant_share: float = 0.65,
    observed_ts: float | None = None,
) -> dict:
    """Return an early, diversified consensus from top copyable wallets.

    This is separate from ``live_lean`` so the existing smart sizing overlay
    remains unchanged while the new entry strategy accumulates dry-run data.
    """
    observed_ts = time.time() if observed_ts is None else observed_ts
    market = await client.get_updown_market(asset, close_ts)
    neutral = {
        "lean": "NEUTRAL", "strength": 0.0, "wallets": 0,
        "effective_wallets": 0.0, "weighted_up": 0.0,
        "weighted_down": 0.0, "dominant_share": None,
        "eligible": False, "reason": "no market", "condition_id": None,
    }
    if market is None:
        return neutral
    neutral["condition_id"] = market.condition_id
    ranked = db.query(
        "SELECT wallet, rank, copy_score FROM wallet_asset_scores "
        "WHERE asset=? AND qualified=1 AND rank <= ? ORDER BY rank",
        (asset, top_n),
    )
    if not ranked:
        return {**neutral, "reason": "no ranked wallets"}
    records = {r["wallet"]: r for r in ranked}
    trades = await client.get_trades(market.condition_id)
    open_ts = close_ts - WINDOW_SECONDS
    cutoff_ts = open_ts + maximum_entry_seconds
    tracked = [
        trade for trade in trades
        if trade.wallet in records and open_ts <= trade.ts <= cutoff_ts
    ]
    db.write_many_ignore("polymarket_wallet_trade_events", [{
        "event_id": _trade_event_id(market.condition_id, trade),
        "condition_id": market.condition_id, "asset": asset,
        "wallet": trade.wallet, "side": trade.side, "outcome": trade.outcome,
        "price": trade.price, "size": trade.size, "ts": trade.ts,
        "observed_ts": observed_ts,
    } for trade in tracked])
    stances = wallet_windows(tracked, "UP", open_ts)
    raw_votes: list[tuple[str, float]] = []
    for stance in stances:
        record = records.get(stance.wallet)
        if record is None:
            continue
        # Rank-based quality is stable and bounded. Current stake contributes
        # conviction logarithmically, so a whale cannot own the vote.
        quality = 1.0 / math.sqrt(max(1, int(record["rank"])))
        conviction = min(2.0, max(0.5, math.log10(1 + max(0.0, stance.stake))))
        raw_votes.append((stance.lean, quality * conviction))
    if not raw_votes:
        return {**neutral, "reason": "no early ranked-wallet positions"}

    uncapped_total = sum(weight for _, weight in raw_votes)
    cap = uncapped_total * 0.10
    votes = [(lean, min(weight, cap)) for lean, weight in raw_votes]
    total = sum(weight for _, weight in votes)
    weighted_up = sum(weight for lean, weight in votes if lean == "UP")
    weighted_down = total - weighted_up
    dominant = max(weighted_up, weighted_down) / total if total else 0.0
    effective = total * total / sum(weight * weight for _, weight in votes)
    lean = "UP" if weighted_up >= weighted_down else "DOWN"
    eligible = True
    reason = "eligible"
    if len(votes) < minimum_active_wallets:
        eligible = False
        reason = f"active wallets {len(votes)} < {minimum_active_wallets}"
    elif effective < minimum_effective_wallets:
        eligible = False
        reason = f"effective wallets {effective:.2f} < {minimum_effective_wallets:.2f}"
    elif dominant < minimum_dominant_share:
        eligible = False
        reason = f"dominant share {dominant:.3f} < {minimum_dominant_share:.3f}"
    return {
        "lean": lean if eligible else "NEUTRAL",
        "candidate_lean": lean,
        "strength": max(0.0, min(1.0, (dominant - 0.5) * 2)),
        "wallets": len(votes), "effective_wallets": effective,
        "weighted_up": weighted_up, "weighted_down": weighted_down,
        "dominant_share": dominant, "eligible": eligible, "reason": reason,
        "condition_id": market.condition_id,
    }


# ---------------------------------------------------------------- live lean

async def live_lean(
    client: PolymarketClient,
    db: Database,
    asset: str,
    close_ts: int,
) -> dict:
    """Current-window lean of qualified repeat winners, weighted by record.

    Weight per wallet = (ci_low - 0.5) * log10(1 + stake): conviction of the
    record times the size of the current bet. Returns NEUTRAL when no
    qualified wallet is active.
    """
    market = await client.get_updown_market(asset, close_ts)
    if market is None:
        return {"lean": "NEUTRAL", "strength": 0.0, "wallets": 0}
    tracked = {
        r["wallet"]: r for r in
        db.query("SELECT * FROM smart_wallets WHERE qualified = 1")
    }
    if not tracked:
        return {"lean": "NEUTRAL", "strength": 0.0, "wallets": 0}
    trades = await client.get_trades(market.condition_id)
    stances = wallet_windows(
        # winner unknown mid-window; pass "UP" (won flag unused here)
        [t for t in trades if t.wallet in tracked], "UP", close_ts - WINDOW_SECONDS,
    )
    score = 0.0
    active = 0
    for s in stances:
        record = tracked[s.wallet]
        weight = (record["ci_low"] - 0.5) * math.log10(1 + max(0.0, s.stake))
        score += weight if s.lean == "UP" else -weight
        active += 1
    if active == 0 or abs(score) < 1e-9:
        return {"lean": "NEUTRAL", "strength": 0.0, "wallets": active}
    return {
        "lean": "UP" if score > 0 else "DOWN",
        "strength": min(1.0, abs(score)),
        "wallets": active,
    }
