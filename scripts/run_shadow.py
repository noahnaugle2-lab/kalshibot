"""Run the SHADOW trading loop: real market data, simulated fills, no orders.

Replaces run_observer.py (the trader does everything the observer does,
plus simulated trading). Requires MODE=SHADOW in .env — refuses otherwise.

Usage:
    python scripts/run_shadow.py                                    # foreground
    nohup python scripts/run_shadow.py >> logs/shadow.log 2>&1 &    # detached
"""

import asyncio
import logging
import signal
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "src"))

from kalshibot.trader import ShadowTrader


async def amain() -> None:
    trader = ShadowTrader()
    loop = asyncio.get_running_loop()
    for sig in (signal.SIGINT, signal.SIGTERM):
        loop.add_signal_handler(sig, trader.request_stop)
    await trader.start()


def main() -> None:
    logging.basicConfig(
        level=logging.INFO,
        format="%(asctime)s %(levelname)-7s %(name)s: %(message)s",
    )
    logging.getLogger("httpx").setLevel(logging.WARNING)
    asyncio.run(amain())


if __name__ == "__main__":
    main()
