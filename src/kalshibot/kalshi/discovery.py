"""Discover Kalshi 15-minute crypto series for the target assets.

Series tickers are never hardcoded: we pull every Crypto-category series,
filter to frequency == "fifteen_min", and map to target assets by ticker
pattern (KX{ASSET}15M) with a title-based fallback. Assets with no live
series are reported as missing and re-checked daily by the scheduler.

Each match is verified by fetching a currently-open market and checking the
window is exactly 15 minutes — a title saying "15 min" is not trusted.
"""

from __future__ import annotations

import logging
import re
from datetime import datetime, timezone

from pydantic import BaseModel

from kalshibot.config import TARGET_ASSETS
from kalshibot.kalshi.client import KalshiClient
from kalshibot.kalshi.models import Market, Series

logger = logging.getLogger(__name__)

FIFTEEN_MIN = "fifteen_min"


class DiscoveredSeries(BaseModel):
    asset: str
    series_ticker: str
    title: str
    verified_15m_window: bool
    live_market_ticker: str | None = None
    live_close_time: datetime | None = None
    yes_bid: float | None = None
    yes_ask: float | None = None
    note: str = ""


class DiscoveryReport(BaseModel):
    checked_at: datetime
    environment: str
    found: dict[str, DiscoveredSeries]  # asset -> series
    missing: list[str]  # assets with no 15-min series (re-check daily)
    extras: list[str]  # 15-min crypto series outside the target set


def match_asset(asset: str, fifteen_min_series: list[Series]) -> Series | None:
    """Map one asset symbol to its 15-minute series.

    Primary: exact ticker pattern KX{ASSET}15M. Fallback: asset symbol as a
    word in the ticker or title (handles hypothetical renames like KXBTC-15MIN).
    """
    exact = f"KX{asset.upper()}15M"
    for series in fifteen_min_series:
        if series.ticker.upper() == exact:
            return series
    pattern = re.compile(rf"(?<![A-Z]){re.escape(asset.upper())}(?![A-Z])")
    for series in fifteen_min_series:
        if pattern.search(series.ticker.upper()) or pattern.search(series.title.upper()):
            return series
    return None


async def discover_15m_series(
    client: KalshiClient, assets: list[str] | None = None
) -> DiscoveryReport:
    assets = assets or TARGET_ASSETS
    all_crypto = await client.get_series_list(category="Crypto")
    fifteen = [s for s in all_crypto if s.frequency == FIFTEEN_MIN]
    logger.info("crypto series: %d total, %d fifteen_min", len(all_crypto), len(fifteen))

    found: dict[str, DiscoveredSeries] = {}
    missing: list[str] = []
    matched_tickers: set[str] = set()

    for asset in assets:
        series = match_asset(asset, fifteen)
        if series is None:
            missing.append(asset)
            logger.warning("no 15-minute series for %s; will re-check daily", asset)
            continue
        matched_tickers.add(series.ticker)
        found[asset] = await _verify_series(client, asset, series)

    extras = sorted(s.ticker for s in fifteen if s.ticker not in matched_tickers)
    return DiscoveryReport(
        checked_at=datetime.now(timezone.utc),
        environment=client.environment.name,
        found=found,
        missing=missing,
        extras=extras,
    )


async def _verify_series(
    client: KalshiClient, asset: str, series: Series
) -> DiscoveredSeries:
    """Confirm the series has a live market with an exact 15-minute window."""
    live: Market | None = None
    note = ""
    try:
        open_markets = await client.get_markets(
            series_ticker=series.ticker, status="open", max_results=5
        )
        live = min(
            (m for m in open_markets if m.close_time),
            key=lambda m: m.close_time,  # type: ignore[arg-type,return-value]
            default=None,
        )
    except Exception as exc:  # discovery must never crash the caller
        note = f"market fetch failed: {exc}"
        logger.warning("verify failed for %s: %s", series.ticker, exc)

    verified = live is not None and live.window_minutes == 15.0
    if live is not None and not verified:
        note = f"open market window is {live.window_minutes} minutes, expected 15"
    elif live is None and not note:
        note = "no open market right now (series exists; verify later)"

    return DiscoveredSeries(
        asset=asset,
        series_ticker=series.ticker,
        title=series.title,
        verified_15m_window=verified,
        live_market_ticker=live.ticker if live else None,
        live_close_time=live.close_time if live else None,
        yes_bid=float(live.yes_bid) if live and live.yes_bid is not None else None,
        yes_ask=float(live.yes_ask) if live and live.yes_ask is not None else None,
        note=note,
    )
