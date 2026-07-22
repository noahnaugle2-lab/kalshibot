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
import shutil
import time
from pathlib import Path

import httpx

logger = logging.getLogger(__name__)

PING_INTERVAL_S = 60.0
SPOT_STALE_S = 90.0
BOOK_STALE_S = 90.0
SNAPSHOT_STALE_S = 30.0
DEFAULT_DISK_CRITICAL_PERCENT = 85.0
DEFAULT_DISK_WARNING_PERCENT = 70.0
DEFAULT_BACKUP_MAX_AGE_HOURS = 26.0


def health_report(trader, now: float | None = None) -> tuple[bool, list[str]]:
    """(healthy, problems). Pure function over trader state for testability."""
    now = now if now is not None else time.time()
    problems: list[str] = []

    # Watch the ACTIVELY-TRADED book, not a quorum of the full recorder fleet:
    # paused record-only assets (BTC/DOGE) must not keep the quorum "healthy"
    # while both traded assets (SOL/XRP) are dark. Require every traded asset
    # fresh; if the whole book is paused, fall back to watching the full fleet.
    paused = getattr(getattr(trader, "risk", None), "paused_assets", None) or set()
    active = [a for a in trader.recorders if a not in paused]
    watch = active or list(trader.recorders)

    if trader.router is not None:
        stale_spot = [
            a for a in watch
            if (age := trader.router.window(a, now)[0].age_seconds(now)) is None
            or age >= SPOT_STALE_S
        ]
        if stale_spot:
            problems.append(f"spot feed stale for traded {sorted(stale_spot)}")
    else:
        problems.append("spot router not started")

    stale_books = [
        a for a in watch
        if trader.recorders[a].latest_book_ts is None
        or now - trader.recorders[a].latest_book_ts >= BOOK_STALE_S
    ]
    if watch and stale_books:
        problems.append(f"kalshi book stale for traded {sorted(stale_books)}")

    snapshot_ages = [
        now - snap.ts for snap in trader.latest_snapshots.values()
    ]
    if not snapshot_ages or min(snapshot_ages) > SNAPSHOT_STALE_S:
        problems.append("scan loop not producing snapshots")

    settings = getattr(trader, "settings", None)
    data_path = Path(getattr(getattr(trader, "db", None), "path", ".")).parent
    try:
        usage = shutil.disk_usage(data_path)
        used_pct = 100 * usage.used / usage.total
        critical = getattr(settings, "disk_critical_percent",
                           DEFAULT_DISK_CRITICAL_PERCENT)
        if used_pct >= critical:
            problems.append(f"disk critical: {used_pct:.1f}% used")
            risk = getattr(trader, "risk", None)
            if risk is not None:
                risk.kill_switch = True
        else:
            warning = getattr(
                settings, "disk_warning_percent", DEFAULT_DISK_WARNING_PERCENT,
            )
            if used_pct >= warning:
                problems.append(f"disk warning: {used_pct:.1f}% used")
    except OSError as exc:
        problems.append(f"disk check failed: {exc}")

    queue = getattr(getattr(trader, "db", None), "_queue", None)
    if queue is not None and queue.qsize() >= 10_000:
        problems.append(f"db write queue critical: {queue.qsize()} rows")

    # Once backups have been configured, a stale success marker is unhealthy.
    backup_rows = trader.db.query("SELECT value FROM meta WHERE key='last_backup_ts'")
    if backup_rows:
        max_age = getattr(settings, "backup_max_age_hours",
                          DEFAULT_BACKUP_MAX_AGE_HOURS) * 3600
        age = now - float(backup_rows[0]["value"])
        if age > max_age:
            problems.append(f"backup stale: {age / 3600:.1f}h old")

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
