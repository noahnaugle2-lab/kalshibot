"""Measure the actual winning-contract payout on the DEMO exchange.

Buys BOTH sides of one demo market (exactly one side must win), holds
through settlement, and derives the per-contract payout from the balance
delta. Resolves the $0.99-vs-$1.00 assumption baked into the fill
simulator (DEFAULT_PAYOUT) with fake money instead of real.

Usage:
    python scripts/demo_payout_probe.py            # place the straddle
    python scripts/demo_payout_probe.py --check    # after close: read result
State (entry fills + balance) persists to data/reports/payout_probe.json.
"""

from __future__ import annotations

import argparse
import asyncio
import json
import sys
import uuid
from datetime import datetime, timedelta, timezone
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "src"))

from kalshibot.config import PROJECT_ROOT, Settings
from kalshibot.kalshi.auth import KalshiSigner
from kalshibot.kalshi.client import KalshiAPIError, KalshiClient, KalshiEnvironment
from kalshibot.kalshi.models import OrderIntent, OrderRequest

STATE = PROJECT_ROOT / "data" / "reports" / "payout_probe.json"
CONTRACTS = 2


def client_from_env() -> KalshiClient:
    settings = Settings()
    signer = KalshiSigner.from_pem_file(
        settings.kalshi_demo_key_id, settings.kalshi_demo_private_key_path)
    return KalshiClient(environment=KalshiEnvironment.DEMO, signer=signer)


async def place() -> None:
    client = client_from_env()
    try:
        now = datetime.now(timezone.utc)
        markets = await client.get_markets(status="open", max_results=300)
        def horizon(m):
            # event markets settle at expected_expiration (game end), often
            # long before the formal close_time
            return m.expected_expiration_time or m.close_time

        candidates = sorted(
            (m for m in markets
             if m.status in ("active", "open") and horizon(m)
             and now + timedelta(minutes=20) < horizon(m) < now + timedelta(hours=36)),
            key=horizon,
        )
        for market in candidates:
            book = await client.get_orderbook(market.ticker)
            ask = book.best_yes_ask
            yes_bid = book.best_yes_bid
            if ask is None or yes_bid is None:
                continue
            no_ask = round(1 - float(yes_bid), 4)  # cost of buying NO
            if not (0 < float(ask) < 1 and 0 < no_ask < 1):
                continue
            balance_before = (await client.get_balance()).dollars
            fills = {}
            for intent, price in ((OrderIntent.BUY_YES, min(0.99, float(ask) + 0.03)),
                                  (OrderIntent.BUY_NO, min(0.99, no_ask + 0.03))):
                placed = await client.place_order(OrderRequest.from_intent(
                    market.ticker, intent, CONTRACTS, f"{price:.4f}",
                    client_order_id=f"payout-{uuid.uuid4().hex[:8]}",
                    time_in_force="immediate_or_cancel"))
                fills[intent.value] = {
                    "filled": float(placed.fill_count or 0),
                    "avg_price": str(placed.average_fill_price),
                    "fee_per_contract": str(placed.average_fee_paid),
                }
            if any(f["filled"] < CONTRACTS for f in fills.values()):
                print(f"partial straddle on {market.ticker}, trying next market")
                continue
            balance_after = (await client.get_balance()).dollars
            state = {
                "ticker": market.ticker,
                "close_time": market.close_time.isoformat(),
                "contracts": CONTRACTS,
                "fills": fills,
                "balance_before": str(balance_before),
                "balance_after_entry": str(balance_after),
            }
            STATE.parent.mkdir(parents=True, exist_ok=True)
            STATE.write_text(json.dumps(state, indent=1))
            print(f"straddle ON: {market.ticker} (closes {market.close_time:%H:%M UTC})")
            print(json.dumps(fills, indent=1))
            print(f"balance {balance_before} -> {balance_after}")
            print(f"run --check after {market.close_time:%H:%M UTC} + settlement lag")
            return
        sys.exit("no two-sided demo market found for the straddle")
    finally:
        await client.close()


async def check() -> None:
    if not STATE.exists():
        sys.exit("no probe state — run without --check first")
    state = json.loads(STATE.read_text())
    client = client_from_env()
    try:
        market = await client.get_market(state["ticker"])
        if not market.is_settled:
            print(f"{state['ticker']} not settled yet (status={market.status}); retry later")
            return
        balance_now = (await client.get_balance()).dollars
        from decimal import Decimal
        entry_after = Decimal(state["balance_after_entry"])
        credited = Decimal(str(balance_now)) - entry_after
        payout_per_contract = credited / Decimal(str(state["contracts"]))
        print(f"result: {market.result}")
        print(f"balance after entry : ${entry_after}")
        print(f"balance now         : ${balance_now}")
        print(f"settlement credit   : ${credited} for {state['contracts']} winning contracts")
        print(f"PAYOUT PER CONTRACT : ${payout_per_contract}")
        print("(fill simulator DEFAULT_PAYOUT is 0.99 — update if this differs)")
        state["result"] = market.result
        state["payout_per_contract"] = str(payout_per_contract)
        STATE.write_text(json.dumps(state, indent=1))
    finally:
        await client.close()


if __name__ == "__main__":
    parser = argparse.ArgumentParser()
    parser.add_argument("--check", action="store_true")
    args = parser.parse_args()
    asyncio.run(check() if args.check else place())
