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
    refresh_wallet_records(db)
    return stats


def refresh_wallet_records(db: Database) -> int:
    """Rebuild smart_wallets aggregates from wallet_window_results."""
    rows = db.query(
        "SELECT wallet, COUNT(*) AS n, SUM(won) AS wins, SUM(pnl) AS pnl, "
        "AVG(stake) AS avg_stake, AVG(entry_offset_s) AS avg_entry "
        "FROM wallet_window_results GROUP BY wallet",
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
    return qualified


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
