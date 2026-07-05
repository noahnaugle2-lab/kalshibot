"""External dead man's switch: Healthchecks.io ping gated on real health.

Local n8n cannot report that its own machine is down, so the alarm lives
outside the box: Healthchecks.io expects a ping every minute and alerts
phone/email when pings stop. The ping is only sent when the system is
GENUINELY healthy — spot feed fresh, Kalshi books fresh, scan loop
producing snapshots, DB writable — so a missed ping means real signal,
not a bare cron heartbeat. When unhealthy we ping the /fail endpoint
(immediate alert with a reason) rather than going silent, and log why.
"""

from __future__ import annotations

import asyncio
import logging
import time

import httpx

logger = logging.getLogger(__name__)

PING_INTERVAL_S = 60.0
SPOT_STALE_S = 90.0
BOOK_STALE_S = 90.0
SNAPSHOT_STALE_S = 30.0


def health_report(trader, now: float | None = None) -> tuple[bool, list[str]]:
    """(healthy, problems). Pure function over trader state for testability."""
    now = now if now is not None else time.time()
    problems: list[str] = []

    if trader.router is not None:
        fresh_spot = sum(
            1 for asset in trader.recorders
            if (age := trader.router.window(asset, now)[0].age_seconds(now)) is not None
            and age < SPOT_STALE_S
        )
        if fresh_spot < max(1, len(trader.recorders) // 2):
            problems.append(f"spot feeds stale ({fresh_spot}/{len(trader.recorders)} fresh)")
    else:
        problems.append("spot router not started")

    fresh_books = sum(
        1 for r in trader.recorders.values()
        if r.latest_book_ts is not None and now - r.latest_book_ts < BOOK_STALE_S
    )
    if trader.recorders and fresh_books < max(1, len(trader.recorders) // 2):
        problems.append(f"kalshi books stale ({fresh_books}/{len(trader.recorders)} fresh)")

    snapshot_ages = [
        now - snap.ts for snap in trader.latest_snapshots.values()
    ]
    if not snapshot_ages or min(snapshot_ages) > SNAPSHOT_STALE_S:
        problems.append("scan loop not producing snapshots")

    try:
        trader.db.write_now("meta", {"key": "last_health_ts", "value": str(now)})
    except Exception as exc:
        problems.append(f"db write failed: {exc}")

    return (not problems, problems)


class DeadMansSwitch:
    def __init__(self, ping_url: str | None) -> None:
        self.ping_url = ping_url
        self.last_ping_ts: float | None = None

    async def run(self, trader) -> None:
        if not self.ping_url:
            logger.warning(
                "HEALTHCHECKS_PING_URL unset — external dead man's switch is "
                "OFF; machine-level failures will not alert"
            )
            return
        async with httpx.AsyncClient(timeout=10) as client:
            while True:
                await asyncio.sleep(PING_INTERVAL_S)
                healthy, problems = await asyncio.to_thread(health_report, trader)
                try:
                    if healthy:
                        await client.get(self.ping_url)
                        self.last_ping_ts = time.time()
                    else:
                        logger.error("health check failed, signalling: %s", problems)
                        await client.post(f"{self.ping_url}/fail",
                                          content="; ".join(problems)[:500])
                except httpx.HTTPError as exc:
                    logger.warning("healthchecks ping failed: %s", exc)
