"""Clock discipline: measure offset against NTP without external deps.

Fifteen-minute windows make clock drift a real bug class — a skewed clock
misjudges time-remaining, regimes, and the settlement blackout. We query an
NTP server with a minimal SNTP client; the trading layer (later phase)
refuses to trade when |offset| exceeds its threshold, the observer just logs.
"""

from __future__ import annotations

import asyncio
import logging
import socket
import struct
import time

logger = logging.getLogger(__name__)

NTP_SERVERS = ("time.apple.com", "pool.ntp.org", "time.google.com")
NTP_EPOCH_DELTA = 2208988800  # seconds between 1900-01-01 and 1970-01-01
DEFAULT_MAX_DRIFT_SECONDS = 0.5


def _query_ntp(server: str, timeout: float = 3.0) -> float:
    """Return local clock offset in seconds (positive = local clock ahead)."""
    packet = b"\x1b" + 47 * b"\0"
    with socket.socket(socket.AF_INET, socket.SOCK_DGRAM) as sock:
        sock.settimeout(timeout)
        t0 = time.time()
        sock.sendto(packet, (server, 123))
        data, _ = sock.recvfrom(512)
        t3 = time.time()
    if len(data) < 48:
        raise ValueError(f"short NTP response from {server}")
    secs, frac = struct.unpack("!II", data[40:48])
    server_time = secs - NTP_EPOCH_DELTA + frac / 2**32
    midpoint = (t0 + t3) / 2
    return midpoint - server_time


async def clock_offset_seconds() -> float | None:
    """Offset via the first NTP server that answers, or None if all fail."""
    for server in NTP_SERVERS:
        try:
            return await asyncio.to_thread(_query_ntp, server)
        except Exception as exc:
            logger.warning("NTP query to %s failed: %s", server, exc)
    return None


async def check_clock(max_drift: float = DEFAULT_MAX_DRIFT_SECONDS) -> tuple[bool, float | None]:
    """(ok, offset). ok=False when drift exceeds max_drift or NTP unreachable."""
    offset = await clock_offset_seconds()
    if offset is None:
        return False, None
    ok = abs(offset) <= max_drift
    (logger.info if ok else logger.error)(
        "clock offset vs NTP: %+.3fs (limit %.3fs)%s",
        offset, max_drift, "" if ok else " — EXCEEDS LIMIT",
    )
    return ok, offset
