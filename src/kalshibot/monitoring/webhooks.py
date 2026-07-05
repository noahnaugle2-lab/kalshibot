"""Outbound webhooks to the local n8n instance — fire-and-forget with retry.

Events: trade_executed, settlement, daily_loss_limit, feed_disconnect,
kill_switch. Each POSTs to {N8N_WEBHOOK_BASE_URL}/webhook/kalshibot with a
typed JSON body (one n8n workflow can route on .event).

Delivery rules (spec section 11): the bot must tolerate n8n being down —
events queue in memory, retry with exponential backoff, and are DROPPED
after max attempts or on queue overflow. A webhook is never allowed to
block or crash trading.
"""

from __future__ import annotations

import asyncio
import logging
import time

import httpx

logger = logging.getLogger(__name__)

QUEUE_MAX = 1000
MAX_ATTEMPTS = 5
BASE_BACKOFF_S = 2.0


class WebhookNotifier:
    def __init__(self, base_url: str | None) -> None:
        self.base_url = base_url.rstrip("/") if base_url else None
        self._queue: asyncio.Queue[dict] = asyncio.Queue(maxsize=QUEUE_MAX)
        self.delivered = 0
        self.dropped = 0

    def emit(self, event: str, payload: dict) -> None:
        """Non-blocking enqueue; silently counts drops on overflow."""
        if self.base_url is None:
            return
        try:
            self._queue.put_nowait({
                "event": event, "ts": time.time(), "payload": payload,
            })
        except asyncio.QueueFull:
            self.dropped += 1

    async def run(self) -> None:
        if self.base_url is None:
            logger.info("webhooks disabled (N8N_WEBHOOK_BASE_URL unset)")
            return
        url = f"{self.base_url}/webhook/kalshibot"
        async with httpx.AsyncClient(timeout=10) as client:
            while True:
                message = await self._queue.get()
                delivered = False
                for attempt in range(MAX_ATTEMPTS):
                    try:
                        response = await client.post(url, json=message)
                        if response.status_code < 300:
                            delivered = True
                            self.delivered += 1
                            break
                        if response.status_code == 404:
                            # no workflow registered for this webhook path —
                            # permanent until the user builds one; don't retry
                            logger.debug("webhook %s -> 404 (no n8n workflow "
                                         "registered), dropping", message["event"])
                            break
                        logger.warning("webhook %s -> HTTP %s (attempt %d)",
                                       message["event"], response.status_code,
                                       attempt + 1)
                    except httpx.HTTPError as exc:
                        logger.warning("webhook %s failed (attempt %d): %s",
                                       message["event"], attempt + 1, exc)
                    await asyncio.sleep(BASE_BACKOFF_S * 2**attempt)
                if not delivered:
                    self.dropped += 1
                    logger.error("webhook %s dropped after %d attempts",
                                 message["event"], MAX_ATTEMPTS)
