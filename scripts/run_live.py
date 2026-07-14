"""Preflight the disabled production executor without starting a strategy loop.

This intentionally proves the live interlocks only.  It does not submit an
order and is not referenced by the deployed systemd unit.
"""

import asyncio
import logging
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "src"))

from kalshibot.config import Settings
from kalshibot.kalshi.auth import KalshiSigner
from kalshibot.kalshi.client import KalshiClient, KalshiEnvironment
from kalshibot.live_trader import LiveTrader, LiveTradingRefused
from kalshibot.persistence.db import Database


async def amain() -> None:
    settings = Settings()
    db = Database("data/kalshibot.db")
    try:
        signer = (KalshiSigner(settings.kalshi_key_id, settings.kalshi_private_key_path)
                  if settings.kalshi_key_id and settings.kalshi_private_key_path else None)
        async with KalshiClient(KalshiEnvironment.PROD, signer=signer) as client:
            await LiveTrader(settings, db, client).prepare()
    finally:
        db.close()


def main() -> None:
    logging.basicConfig(level=logging.INFO, format="%(levelname)s: %(message)s")
    try:
        asyncio.run(amain())
    except LiveTradingRefused as exc:
        logging.error("LIVE PREFLIGHT REFUSED: %s", exc)
        raise SystemExit(2) from exc


if __name__ == "__main__":
    main()
