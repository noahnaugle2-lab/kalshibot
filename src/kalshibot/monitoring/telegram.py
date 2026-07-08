"""Minimal Telegram sender for server-side alerts (nightly portfolio check).

Bot token + chat id come from TELEGRAM_BOT_TOKEN / TELEGRAM_CHAT_ID. If either
is unset the send is a no-op (logged), so the bot runs fine without Telegram
configured. Sync httpx is fine here — the only caller is the nightly
subprocess, which is not on the trading event loop.
"""

from __future__ import annotations

import logging

import httpx

from kalshibot.config import Settings

logger = logging.getLogger(__name__)


def send_telegram(text: str, settings: Settings | None = None) -> bool:
    s = settings or Settings()
    token, chat = s.telegram_bot_token, s.telegram_chat_id
    if not token or not chat:
        logger.info("telegram unset (TELEGRAM_BOT_TOKEN/CHAT_ID) — skipping send")
        return False
    try:
        r = httpx.post(
            f"https://api.telegram.org/bot{token}/sendMessage",
            data={"chat_id": chat, "text": text},
            timeout=10,
        )
        r.raise_for_status()
        return True
    except Exception as exc:  # noqa: BLE001 — alerting must never crash the job
        logger.warning("telegram send failed: %s", exc)
        return False
