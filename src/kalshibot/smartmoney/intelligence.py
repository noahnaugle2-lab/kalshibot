"""Public-wallet strategy intelligence and delayed Kalshi copy replay.

This module is deliberately research-only. It discovers profitable wallets
from public Polymarket leaderboards, preserves their full trade sequence in
matching short-window markets, classifies how they trade, and replays the
first externally observable directional action against historical Kalshi
books. Nothing here can create a live order.
"""

from __future__ import annotations

import hashlib
import json
import sqlite3
import time
from collections import defaultdict
from dataclasses import dataclass
from typing import Iterable

from kalshibot.features.fees import taker_fee
from kalshibot.persistence.db import Database
from kalshibot.smartmoney.polymarket import (
    REQUEST_DELAY_S,
    WINDOW_SECONDS,
    PolymarketClient,
    _trade_event_id,
)


LEADERBOARD_PERIODS = ("DAY", "WEEK", "MONTH", "ALL")
COPY_DELAYS = (2, 5, 10, 30)


@dataclass(frozen=True)
class IntelligenceRefresh:
    leaderboard_rows: int
    leaderboard_wallets: int
    fingerprints: int
    replays: int
    copyability_scores: int


async def refresh_leaderboard_snapshots(
    client: PolymarketClient,
    db: Database,
    *,
    category: str = "CRYPTO",
    periods: Iterable[str] = LEADERBOARD_PERIODS,
    top_n: int = 100,
    snapshot_ts: float | None = None,
) -> tuple[int, set[str]]:
    """Snapshot several horizons so a temporary monthly winner is not truth."""
    snapshot_ts = time.time() if snapshot_ts is None else snapshot_ts
    rows: list[dict] = []
    wallets: set[str] = set()
    for period in periods:
        leaders = await client.get_leaderboard(
            category=category, period=period, limit=top_n,
        )
        for leader in leaders:
            wallet = str(leader.get("proxyWallet") or "").lower()
            if not wallet:
                continue
            wallets.add(wallet)
            rows.append({
                "snapshot_ts": snapshot_ts,
                "category": category,
                "period": period,
                "rank": int(leader.get("rank") or 0),
                "wallet": wallet,
                "username": leader.get("userName"),
                "pnl": float(leader.get("pnl") or 0.0),
                "volume": float(leader.get("vol") or 0.0),
                "raw": json.dumps(leader, separators=(",", ":"), sort_keys=True),
            })
    db.write_many("polymarket_leaderboard_snapshots", rows)
    return len(rows), wallets


def current_leaderboard_wallets(
    db: Database,
    *,
    category: str = "CRYPTO",
    top_n: int = 100,
) -> set[str]:
    """Union the latest day/week/month/all-time top cohorts."""
    rows = db.query(
        "SELECT DISTINCT wallet FROM polymarket_leaderboard_snapshots "
        "WHERE category=? AND rank<=? AND snapshot_ts=("
        " SELECT MAX(snapshot_ts) FROM polymarket_leaderboard_snapshots "
        " WHERE category=?)",
        (category, top_n, category),
    )
    return {row["wallet"] for row in rows}


async def capture_leader_trade_sequences(
    client: PolymarketClient,
    db: Database,
    assets: Iterable[str],
    start_close_ts: int,
    end_close_ts: int,
    tracked_wallets: set[str],
) -> int:
    """Backfill tracked-wallet trades without reprocessing every wallet."""
    captured = 0
    for asset in assets:
        for close_ts in range(start_close_ts, end_close_ts + 1, WINDOW_SECONDS):
            market = await client.get_updown_market(asset, close_ts)
            if market is None or not market.closed or market.winner is None:
                continue
            trades = await client.get_trades(market.condition_id)
            observed_ts = time.time()
            rows = []
            for trade in trades:
                if trade.wallet not in tracked_wallets:
                    continue
                rows.append({
                    "event_id": _trade_event_id(market.condition_id, trade),
                    "condition_id": market.condition_id, "asset": asset,
                    "wallet": trade.wallet, "side": trade.side,
                    "outcome": trade.outcome, "price": trade.price,
                    "size": trade.size, "ts": trade.ts,
                    "observed_ts": observed_ts,
                })
            captured += db.write_many_ignore(
                "polymarket_wallet_trade_events", rows,
            )
            # This is an upsert of public market metadata only. It does not
            # alter the scanner's resolved wallet-result rows.
            existing = db.query(
                "SELECT scanned FROM polymarket_markets "
                "WHERE condition_id=?",
                (market.condition_id,),
            )
            db.write_now("polymarket_markets", {
                "condition_id": market.condition_id, "asset": asset,
                "slug": market.slug, "close_ts": close_ts,
                "winner": market.winner, "liquidity": market.liquidity,
                "volume": market.volume,
                "scanned": int(existing[0]["scanned"] or 0) if existing else 0,
                "updated_ts": observed_ts,
            })
            if REQUEST_DELAY_S:
                import asyncio
                await asyncio.sleep(REQUEST_DELAY_S)
    return captured


def _strategy_type(
    *,
    both_outcomes_rate: float,
    round_trip_rate: float,
    avg_trades: float,
    avg_first_entry: float | None,
    dominant_outcome_share: float,
) -> str:
    if both_outcomes_rate >= 0.50 and round_trip_rate >= 0.35:
        return "two_sided_market_maker"
    if avg_trades >= 8:
        return "high_frequency_scalper"
    if avg_first_entry is not None and avg_first_entry >= 600:
        return "late_window_sweeper"
    if dominant_outcome_share >= 0.80 and avg_trades <= 2.5:
        return "directional_single_shot"
    if dominant_outcome_share >= 0.75:
        return "directional_accumulator"
    return "mixed_or_arbitrage"


def refresh_strategy_fingerprints(
    db: Database,
    *,
    now: float | None = None,
) -> int:
    """Classify latest tracked behavior per wallet and asset."""
    now = time.time() if now is None else now
    events = db.query(
        "SELECT e.*, m.close_ts FROM polymarket_wallet_trade_events e "
        "JOIN polymarket_markets m ON m.condition_id=e.condition_id "
        "ORDER BY e.wallet, e.asset, e.condition_id, e.ts, e.id"
    )
    grouped: dict[tuple[str, str, str], list] = defaultdict(list)
    close_by_group: dict[tuple[str, str, str], float] = {}
    for event in events:
        key = (event["wallet"], event["asset"], event["condition_id"])
        grouped[key].append(event)
        close_by_group[key] = float(event["close_ts"])

    window_pnl = {
        (row["wallet"], row["asset"], row["condition_id"]): row
        for row in db.query(
            "SELECT w.wallet,w.asset,w.condition_id,w.pnl,w.stake "
            "FROM wallet_window_results w JOIN ("
            " SELECT DISTINCT wallet,asset,condition_id "
            " FROM polymarket_wallet_trade_events"
            ") tracked ON tracked.wallet=w.wallet "
            "AND tracked.asset=w.asset "
            "AND tracked.condition_id=w.condition_id"
        )
    }
    latest_snapshot = db.query(
        "SELECT MAX(snapshot_ts) AS ts FROM polymarket_leaderboard_snapshots"
    )
    leaderboard: dict[str, dict] = {}
    if latest_snapshot and latest_snapshot[0]["ts"] is not None:
        for row in db.query(
            "SELECT wallet,period,rank,pnl,volume "
            "FROM polymarket_leaderboard_snapshots WHERE snapshot_ts=?",
            (latest_snapshot[0]["ts"],),
        ):
            item = leaderboard.setdefault(row["wallet"], {
                "best_rank": None, "month_pnl": None, "month_volume": None,
            })
            rank = int(row["rank"])
            item["best_rank"] = (
                rank if item["best_rank"] is None
                else min(rank, item["best_rank"])
            )
            if row["period"] == "MONTH":
                item["month_pnl"] = row["pnl"]
                item["month_volume"] = row["volume"]

    by_wallet_asset: dict[tuple[str, str], list[dict]] = defaultdict(list)
    for key, trades in grouped.items():
        wallet, asset, condition_id = key
        open_ts = close_by_group[key] - WINDOW_SECONDS
        notionals = defaultdict(float)
        sides = set()
        for trade in trades:
            notionals[str(trade["outcome"]).upper()] += (
                float(trade["price"]) * float(trade["size"])
            )
            sides.add(trade["side"])
        total_notional = sum(notionals.values())
        dominant = (
            max(notionals.values()) / total_notional if total_notional else 0.0
        )
        source = window_pnl.get(key)
        by_wallet_asset[(wallet, asset)].append({
            "trades": len(trades),
            "buys": sum(t["side"] == "BUY" for t in trades),
            "sells": sum(t["side"] == "SELL" for t in trades),
            "both_outcomes": len(notionals) > 1,
            "round_trip": "BUY" in sides and "SELL" in sides,
            "first_entry": max(0.0, float(trades[0]["ts"]) - open_ts),
            "last_trade": max(0.0, float(trades[-1]["ts"]) - open_ts),
            "dominant": dominant,
            "pnl": float(source["pnl"] or 0.0) if source else 0.0,
            "stake": float(source["stake"] or 0.0) if source else 0.0,
        })

    prepared = []
    for (wallet, asset), markets in by_wallet_asset.items():
        market_n = len(markets)
        pnls = [m["pnl"] for m in markets]
        source_pnl = sum(pnls)
        source_stake = sum(m["stake"] for m in markets)
        gross_profit = sum(p for p in pnls if p > 0)
        gross_loss = -sum(p for p in pnls if p < 0)
        both_rate = sum(m["both_outcomes"] for m in markets) / market_n
        round_trip_rate = sum(m["round_trip"] for m in markets) / market_n
        avg_trades = sum(m["trades"] for m in markets) / market_n
        avg_first = sum(m["first_entry"] for m in markets) / market_n
        avg_last = sum(m["last_trade"] for m in markets) / market_n
        early_rate = sum(m["first_entry"] <= 300 for m in markets) / market_n
        dominant = sum(m["dominant"] for m in markets) / market_n
        leader = leaderboard.get(wallet, {})
        prepared.append({
            "wallet": wallet, "asset": asset,
            "strategy_type": _strategy_type(
                both_outcomes_rate=both_rate,
                round_trip_rate=round_trip_rate,
                avg_trades=avg_trades,
                avg_first_entry=avg_first,
                dominant_outcome_share=dominant,
            ),
            "markets": market_n,
            "trades": sum(m["trades"] for m in markets),
            "buys": sum(m["buys"] for m in markets),
            "sells": sum(m["sells"] for m in markets),
            "both_outcomes_rate": both_rate,
            "round_trip_rate": round_trip_rate,
            "avg_trades_per_market": avg_trades,
            "avg_first_entry_s": avg_first,
            "avg_last_trade_s": avg_last,
            "early_entry_rate": early_rate,
            "dominant_outcome_share": dominant,
            "source_pnl": source_pnl, "source_stake": source_stake,
            "source_roi": (
                source_pnl / source_stake if source_stake > 0 else None
            ),
            "source_profit_factor": (
                gross_profit / gross_loss if gross_loss > 0
                else (99.0 if gross_profit > 0 else None)
            ),
            "leaderboard_best_rank": leader.get("best_rank"),
            "leaderboard_month_pnl": leader.get("month_pnl"),
            "leaderboard_month_volume": leader.get("month_volume"),
            "updated_ts": now,
        })
    db.write_many("wallet_strategy_fingerprints", prepared)
    return len(prepared)


def _replay_id(wallet: str, condition_id: str, delay: int) -> str:
    return hashlib.sha256(
        f"{wallet}|{condition_id}|{delay}".encode()
    ).hexdigest()


def refresh_delayed_copy_replays(
    db: Database,
    *,
    delays: Iterable[int] = COPY_DELAYS,
    maximum_price: float = 0.90,
    maximum_book_lag: float = 5.0,
    now: float | None = None,
) -> int:
    """Replay one copyable first BUY per wallet/window on Kalshi books."""
    now = time.time() if now is None else now
    first_buys = db.query(
        "SELECT * FROM (SELECT e.*,ROW_NUMBER() OVER("
        " PARTITION BY wallet,condition_id ORDER BY ts,id) AS sequence "
        " FROM polymarket_wallet_trade_events e WHERE side='BUY') "
        "WHERE sequence=1 ORDER BY ts"
    )
    rows = []
    conn = sqlite3.connect(db.path)
    conn.row_factory = sqlite3.Row
    try:
        for trade in first_buys:
            pm_market = conn.execute(
                "SELECT asset,close_ts FROM polymarket_markets "
                "WHERE condition_id=?",
                (trade["condition_id"],),
            ).fetchall()
            if not pm_market:
                continue
            asset = pm_market[0]["asset"]
            close_ts = float(pm_market[0]["close_ts"])
            kalshi = conn.execute(
                "SELECT ticker FROM markets WHERE asset=? "
                "AND close_ts BETWEEN ? AND ? "
                "ORDER BY ABS(close_ts-?) LIMIT 1",
                (asset, close_ts - 2, close_ts + 2, close_ts),
            ).fetchall()
            ticker = kalshi[0]["ticker"] if kalshi else None
            outcome = str(trade["outcome"]).upper()
            intent = "BUY_YES" if outcome == "UP" else "BUY_NO"
            for delay in delays:
                target_ts = float(trade["ts"]) + int(delay)
                status = "missing_market"
                reason = "no matching Kalshi market"
                book_ts = None
                price = None
                settlement_result = None
                fee = 0.0
                pnl = None
                if ticker:
                    books = conn.execute(
                        "SELECT ts,best_yes_bid,best_yes_ask "
                        "FROM book_snapshots WHERE market_ticker=? "
                        "AND ts>=? ORDER BY ts LIMIT 1",
                        (ticker, target_ts),
                    ).fetchall()
                    if not books:
                        status, reason = "missing_book", "no Kalshi book after copy delay"
                    else:
                        book = books[0]
                        book_ts = float(book["ts"])
                        if book_ts - target_ts > maximum_book_lag:
                            status, reason = (
                                "stale_book",
                                f"next Kalshi book lag {book_ts-target_ts:.2f}s",
                            )
                        else:
                            price = (
                                book["best_yes_ask"] if intent == "BUY_YES"
                                else (
                                    1.0 - float(book["best_yes_bid"])
                                    if book["best_yes_bid"] is not None else None
                                )
                            )
                            if price is None:
                                status, reason = "no_quote", "copy side has no ask"
                            elif price > maximum_price:
                                status, reason = (
                                    "price_veto",
                                    f"copy price {price:.2f} > {maximum_price:.2f}",
                                )
                            else:
                                settled = conn.execute(
                                    "SELECT result FROM settlements "
                                    "WHERE market_ticker=?",
                                    (ticker,),
                                ).fetchall()
                                if not settled:
                                    status, reason = "pending", "awaiting settlement"
                                else:
                                    settlement_result = settled[0]["result"]
                                    won = (
                                        settlement_result == "yes"
                                        if intent == "BUY_YES"
                                        else settlement_result == "no"
                                    )
                                    fee = float(taker_fee(price, 1))
                                    pnl = (1.0 - price if won else -price) - fee
                                    status, reason = "settled", (
                                        "copied direction won" if won
                                        else "copied direction lost"
                                    )
                rows.append({
                    "replay_id": _replay_id(
                        trade["wallet"], trade["condition_id"], int(delay),
                    ),
                    "computed_ts": now, "wallet": trade["wallet"], "asset": asset,
                    "condition_id": trade["condition_id"],
                    "market_ticker": ticker,
                    "source_trade_ts": trade["ts"],
                    "source_observed_ts": trade["observed_ts"],
                    "source_outcome": outcome,
                    "source_price": trade["price"], "source_size": trade["size"],
                    "delay_seconds": int(delay), "target_ts": target_ts,
                    "book_ts": book_ts, "intent": intent,
                    "kalshi_price": price, "status": status, "reason": reason,
                    "settlement_result": settlement_result, "fees": fee,
                    "pnl_net": pnl,
                })
    finally:
        conn.close()
    return db.write_many_ignore("wallet_copy_replays", rows)


def refresh_copyability_scores(
    db: Database,
    *,
    now: float | None = None,
) -> int:
    now = time.time() if now is None else now
    fingerprints = {
        (row["wallet"], row["asset"]): row["strategy_type"]
        for row in db.query(
            "SELECT wallet,asset,strategy_type "
            "FROM wallet_strategy_fingerprints"
        )
    }
    grouped: dict[tuple[str, str, int], list] = defaultdict(list)
    for row in db.query(
        "SELECT wallet,asset,delay_seconds,status,kalshi_price,pnl_net "
        "FROM wallet_copy_replays"
    ):
        grouped[(row["wallet"], row["asset"], row["delay_seconds"])].append(row)

    prepared = []
    by_asset_delay: dict[tuple[str, int], list[dict]] = defaultdict(list)
    for (wallet, asset, delay), rows in grouped.items():
        settled = [row for row in rows if row["pnl_net"] is not None]
        pnls = [float(row["pnl_net"]) for row in settled]
        gains = sum(p for p in pnls if p > 0)
        losses = -sum(p for p in pnls if p < 0)
        equity = peak = drawdown = 0.0
        for pnl in pnls:
            equity += pnl
            peak = max(peak, equity)
            drawdown = max(drawdown, peak - equity)
        fill_rate = len(settled) / len(rows) if rows else 0.0
        net = sum(pnls)
        # Sample-aware and drawdown-aware. Negative copy PnL stays negative
        # regardless of the source wallet's public leaderboard rank.
        evidence_weight = len(settled) / (len(settled) + 20.0)
        copy_score = (
            net * evidence_weight * fill_rate / (1.0 + drawdown)
        )
        item = {
            "wallet": wallet, "asset": asset, "delay_seconds": delay,
            "strategy_type": fingerprints.get(
                (wallet, asset), "unclassified",
            ),
            "signals": len(rows), "filled": len(settled),
            "wins": sum(p > 0 for p in pnls), "net": net,
            "profit_factor": (
                gains / losses if losses > 0
                else (99.0 if gains > 0 else None)
            ),
            "max_drawdown": drawdown,
            "avg_entry_price": (
                sum(float(row["kalshi_price"]) for row in settled)
                / len(settled) if settled else None
            ),
            "fill_rate": fill_rate, "copy_score": copy_score,
            "rank": None, "updated_ts": now,
        }
        prepared.append(item)
        by_asset_delay[(asset, delay)].append(item)

    for items in by_asset_delay.values():
        items.sort(key=lambda item: (
            -item["copy_score"], -item["filled"], item["wallet"],
        ))
        for rank, item in enumerate(items, start=1):
            item["rank"] = rank
    db.execute_write("DELETE FROM wallet_copyability_scores")
    db.write_many("wallet_copyability_scores", prepared)
    return len(prepared)


def refresh_wallet_intelligence_analytics(
    db: Database,
    *,
    maximum_price: float = 0.90,
    now: float | None = None,
) -> IntelligenceRefresh:
    """Recompute fingerprints and cross-venue delayed-copy evidence."""
    now = time.time() if now is None else now
    fingerprints = refresh_strategy_fingerprints(db, now=now)
    replays = refresh_delayed_copy_replays(
        db, maximum_price=maximum_price, now=now,
    )
    scores = refresh_copyability_scores(db, now=now)
    latest = db.query(
        "SELECT COUNT(*) AS rows,COUNT(DISTINCT wallet) AS wallets "
        "FROM polymarket_leaderboard_snapshots WHERE snapshot_ts=("
        " SELECT MAX(snapshot_ts) FROM polymarket_leaderboard_snapshots)"
    )[0]
    return IntelligenceRefresh(
        leaderboard_rows=int(latest["rows"] or 0),
        leaderboard_wallets=int(latest["wallets"] or 0),
        fingerprints=fingerprints,
        replays=replays,
        copyability_scores=scores,
    )
