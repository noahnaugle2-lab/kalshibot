"""Run the separately armed, fail-closed production funded supervisor."""

from __future__ import annotations

import asyncio
import logging
import signal
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "src"))

from kalshibot.funded_supervisor import FundedSupervisor
from kalshibot.live_trader import LiveTradingRefused


async def amain() -> None:
    supervisor = FundedSupervisor()
    loop = asyncio.get_running_loop()
    for sig in (signal.SIGINT, signal.SIGTERM):
        loop.add_signal_handler(sig, supervisor.request_stop)
    await supervisor.start()


def main() -> None:
    logging.basicConfig(
        level=logging.INFO,
        format="%(asctime)s %(levelname)s %(name)s: %(message)s",
    )
    try:
        asyncio.run(amain())
    except LiveTradingRefused as exc:
        logging.critical("FUNDED STARTUP REFUSED: %s", exc)
        raise SystemExit(2) from exc


if __name__ == "__main__":
    main()
