"""Run 15-minute series discovery against Kalshi production (read-only, public).

Prints a table for the nine target assets and writes the machine-readable map
to data/discovery/series_map.json (consumed by the trading loops; refreshed
daily so newly-listed series are picked up automatically).

Usage: python scripts/discover_series.py [--json-out PATH]
"""

from __future__ import annotations

import argparse
import asyncio
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "src"))

from kalshibot.kalshi.client import KalshiClient, KalshiEnvironment
from kalshibot.kalshi.discovery import DiscoveryReport, discover_15m_series


def print_report(report: DiscoveryReport) -> None:
    print(f"\nKalshi 15-minute series discovery — {report.environment}, {report.checked_at:%Y-%m-%d %H:%M UTC}")
    print("=" * 96)
    header = f"{'ASSET':<6} {'SERIES':<12} {'15M?':<5} {'LIVE MARKET':<28} {'YES BID/ASK':<12} NOTE"
    print(header)
    print("-" * 96)
    for asset, d in report.found.items():
        window = "yes" if d.verified_15m_window else "NO"
        live = d.live_market_ticker or "-"
        quote = (
            f"{d.yes_bid:.2f}/{d.yes_ask:.2f}"
            if d.yes_bid is not None and d.yes_ask is not None
            else "-"
        )
        print(f"{asset:<6} {d.series_ticker:<12} {window:<5} {live:<28} {quote:<12} {d.note}")
    for asset in report.missing:
        print(f"{asset:<6} {'—':<12} {'—':<5} {'—':<28} {'—':<12} no 15-min series; re-check daily")
    print("-" * 96)
    print(f"Found {len(report.found)}/{len(report.found) + len(report.missing)} target assets.")
    if report.extras:
        print(f"Other 15-min crypto series on Kalshi (not in target set): {', '.join(report.extras)}")


async def main() -> None:
    parser = argparse.ArgumentParser()
    parser.add_argument(
        "--json-out",
        type=Path,
        default=Path(__file__).resolve().parents[1] / "data/discovery/series_map.json",
    )
    args = parser.parse_args()

    async with KalshiClient(environment=KalshiEnvironment.PROD) as client:
        report = await discover_15m_series(client)

    print_report(report)
    args.json_out.parent.mkdir(parents=True, exist_ok=True)
    args.json_out.write_text(report.model_dump_json(indent=2))
    print(f"\nWrote {args.json_out}")


if __name__ == "__main__":
    asyncio.run(main())
