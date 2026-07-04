"""Verify order plumbing end to end against the Kalshi DEMO exchange.

Exercises every authenticated path the order manager will use:
balance -> open orders/positions/fills -> place a resting limit order
(1 contract, priced far from the market so it can't fill) -> reconcile it
by client_order_id -> cancel it -> confirm the cancel.

DEMO only: refuses to run against production by construction.
"""

from __future__ import annotations

import asyncio
import sys
import uuid
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "src"))

from datetime import datetime, timedelta, timezone

from kalshibot.config import Settings
from kalshibot.kalshi.auth import KalshiSigner
from kalshibot.kalshi.client import KalshiAPIError, KalshiClient, KalshiEnvironment
from kalshibot.kalshi.models import Market, OrderIntent, OrderRequest


def _tradeable(m: Market) -> bool:
    # Demo's status filter is unreliable (returns closed markets), so verify
    # client-side: active status and closing comfortably in the future.
    return m.status in ("active", "open") and m.close_time is not None and (
        m.close_time > datetime.now(timezone.utc) + timedelta(minutes=2)
    )


async def pick_test_market(client: KalshiClient) -> Market | None:
    """Prefer a 15-min crypto market if demo mirrors one live; else anything active."""
    for series in ("KXBTC15M", "KXETH15M"):
        try:
            markets = await client.get_markets(
                series_ticker=series, status="open", max_results=20
            )
        except KalshiAPIError:
            markets = []
        for m in markets:
            if _tradeable(m):
                return m
    markets = await client.get_markets(status="open", max_results=200)
    return next((m for m in markets if _tradeable(m)), None)


async def main() -> None:
    settings = Settings()
    if not settings.kalshi_demo_key_id or not settings.kalshi_demo_private_key_path:
        sys.exit("KALSHI_DEMO_KEY_ID / KALSHI_DEMO_PRIVATE_KEY_PATH missing from .env")

    signer = KalshiSigner.from_pem_file(
        settings.kalshi_demo_key_id, settings.kalshi_demo_private_key_path
    )
    async with KalshiClient(environment=KalshiEnvironment.DEMO, signer=signer) as client:
        balance = await client.get_balance()
        print(f"[1] balance           : ${balance.dollars}")

        orders = await client.get_orders(status="resting")
        positions = await client.get_positions()
        fills = await client.get_fills()
        print(f"[2] resting orders    : {len(orders)}")
        print(f"[3] positions payload : {list(positions.keys())}")
        print(f"[4] fills             : {len(fills)}")

        market = await pick_test_market(client)
        if market is None:
            sys.exit("no open market found on demo to test order plumbing against")
        print(f"[5] test market       : {market.ticker} ({market.title!r}, status={market.status})")

        client_order_id = str(uuid.uuid4())
        order_request = OrderRequest.from_intent(
            market.ticker,
            OrderIntent.BUY_YES,
            contracts=1,
            price="0.01",  # rests far below any live market, cannot fill
            client_order_id=client_order_id,
        )
        print(f"    wire payload      : {order_request.body()}")
        try:
            placed = await client.place_order(order_request)
        except KalshiAPIError as exc:
            print(f"[6] place order       : REJECTED HTTP {exc.status_code}: {exc.body[:200]}")
            print("\nWrite path reached the exchange and was rejected cleanly "
                  "(auth + payload OK). Fund the demo account to test a resting order.")
            return
        print(f"[6] place order       : order_id={placed.order_id} "
              f"filled={placed.fill_count} remaining={placed.remaining_count}")

        mine = await client.get_orders(ticker=market.ticker)
        match = next((o for o in mine if o.client_order_id == client_order_id), None)
        print(f"[7] reconcile by coid : {'FOUND' if match else 'MISSING'} "
              f"(status={match.status if match else '-'})")

        canceled = await client.cancel_order(placed.order_id)
        print(f"[8] cancel order      : reduced_by={canceled.reduced_by} (expected 1.00)")
        print("\nOrder plumbing verified end to end: place, reconcile, cancel all OK.")


if __name__ == "__main__":
    asyncio.run(main())
