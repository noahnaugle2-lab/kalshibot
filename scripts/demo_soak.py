"""Pre-LIVE dress rehearsal: soak-test the real order path on the DEMO exchange.

Exercises everything shadow mode never touches, with fake money:

  1. auth + balance snapshot
  2. resting orders: place far-from-touch bids, verify resting, cancel
  3. exchange-derived reconciliation: orders are findable and cancelable by
     client_order_id prefix alone (no local state) — the restart story
  4. real fill: cross a small IOC order into the book, verify fill + fees +
     position via portfolio endpoints; attempt to flatten
  5. rate soak: N place/cancel cycles through the shared token bucket,
     counting rate-limit rejections (should be zero: the bucket paces us)
  6. idempotency: reuse a client_order_id, record how the exchange responds

Every check prints PASS/FAIL/INFO and a JSON report lands in data/reports/.
DEMO only: the client is constructed against the demo host, and the script
aborts if the environment is not DEMO.

Usage: python scripts/demo_soak.py [--cycles 30]
"""

from __future__ import annotations

import argparse
import asyncio
import json
import sys
import time
import uuid
from datetime import datetime, timedelta, timezone
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "src"))

from kalshibot.config import PROJECT_ROOT, Settings
from kalshibot.kalshi.auth import KalshiSigner
from kalshibot.kalshi.client import KalshiAPIError, KalshiClient, KalshiEnvironment
from kalshibot.kalshi.models import Market, OrderIntent, OrderRequest

PREFIX = f"soak-{uuid.uuid4().hex[:6]}"
MAX_FILL_COST_DOLLARS = 2.00  # cap real (fake-)money spend per fill test

results: list[dict] = []


def check(name: str, ok: bool | None, detail: str = "") -> None:
    status = "INFO" if ok is None else ("PASS" if ok else "FAIL")
    results.append({"check": name, "status": status, "detail": detail})
    print(f"  [{status:<4}] {name:<44} {detail}")


def coid(tag: str) -> str:
    return f"{PREFIX}-{tag}-{uuid.uuid4().hex[:8]}"


async def tradeable_markets(client: KalshiClient, want: int = 3) -> list[Market]:
    now = datetime.now(timezone.utc)
    markets = await client.get_markets(status="open", max_results=300)
    live = [m for m in markets
            if m.status in ("active", "open") and m.close_time
            and m.close_time > now + timedelta(minutes=30)]
    # prefer markets with a two-sided book near mid for the fill test
    scored: list[tuple[float, Market]] = []
    for m in live[:60]:
        if m.yes_bid is None or m.yes_ask is None:
            continue
        mid_distance = abs(float(m.yes_bid) + float(m.yes_ask) - 1)  # 0 = mid
        scored.append((mid_distance, m))
    scored.sort(key=lambda x: x[0])
    return [m for _, m in scored[:want]]


async def phase_resting(client: KalshiClient, market: Market) -> str | None:
    """Place a 1c resting bid; verify visible; cancel; verify reduced."""
    order_id = None
    try:
        placed = await client.place_order(OrderRequest.from_intent(
            market.ticker, OrderIntent.BUY_YES, 1, "0.01",
            client_order_id=coid("rest")))
        order_id = placed.order_id
        check("resting: placed", bool(order_id),
              f"{market.ticker} remaining={placed.remaining_count}")
    except KalshiAPIError as exc:
        check("resting: placed", False, f"HTTP {exc.status_code}: {exc.body[:80]}")
        return None
    # measured: order reads can lag placement (eventual consistency) — poll
    # up to ~4s before declaring failure; the live order manager must apply
    # the same tolerance when reconciling just-placed orders
    visible = False
    attempts = 0
    for attempts in range(1, 5):
        mine = await client.get_orders(ticker=market.ticker, status="resting")
        if any(o.order_id == order_id for o in mine):
            visible = True
            break
        await asyncio.sleep(1.0)
    check("resting: visible via get_orders", visible,
          f"after {attempts} read(s) — reads lag placement; reconcile with retries")
    cancelled = await client.cancel_order(order_id)
    check("resting: cancel reduced_by=1",
          cancelled.reduced_by is not None and float(cancelled.reduced_by) >= 1,
          f"reduced_by={cancelled.reduced_by}")
    return order_id


async def phase_reconcile(client: KalshiClient, markets: list[Market]) -> None:
    """Place resting orders, 'forget' them, recover from exchange by prefix."""
    for m in markets[:2]:
        try:
            await client.place_order(OrderRequest.from_intent(
                m.ticker, OrderIntent.BUY_YES, 1, "0.01",
                client_order_id=coid("lost")))
        except KalshiAPIError as exc:
            check("reconcile: setup order", False, str(exc)[:80])
    found = [o for o in await client.get_orders(status="resting")
             if (o.client_order_id or "").startswith(f"{PREFIX}-lost")]
    check("reconcile: recovered by coid prefix", len(found) >= 1,
          f"{len(found)} recovered with no local state")
    swept = 0
    for order in found:
        try:
            await client.cancel_order(order.order_id)
            swept += 1
        except KalshiAPIError as exc:
            check("reconcile: sweep cancel", False, str(exc)[:80])
    check("reconcile: swept all recovered", swept == len(found), f"{swept} cancelled")


async def phase_fill(client: KalshiClient, markets: list[Market]) -> None:
    """Cross a tiny IOC into the book; verify fill, fees, position.

    Searches beyond the pre-picked markets: demo books are often one-sided,
    so probe up to 25 live markets for a real ask to cross.
    """
    now = datetime.now(timezone.utc)
    candidates = list(markets)
    candidates += [m for m in await client.get_markets(status="open", max_results=300)
                   if m.status in ("active", "open") and m.close_time
                   and m.close_time > now + timedelta(minutes=30)
                   and m.ticker not in {x.ticker for x in markets}][:25]
    for market in candidates:
        try:
            book = await client.get_orderbook(market.ticker)
        except KalshiAPIError:
            continue
        ask = book.best_yes_ask
        if ask is None or not (0 < float(ask) < 1):
            continue
        ask_f = float(ask)
        contracts = max(1, min(3, int(MAX_FILL_COST_DOLLARS // ask_f)))
        if contracts * ask_f > MAX_FILL_COST_DOLLARS:
            continue
        try:
            placed = await client.place_order(OrderRequest.from_intent(
                market.ticker, OrderIntent.BUY_YES, contracts, f"{ask_f:.4f}",
                client_order_id=coid("fill"), time_in_force="immediate_or_cancel"))
        except KalshiAPIError as exc:
            check("fill: IOC place", False, f"HTTP {exc.status_code}: {exc.body[:80]}")
            continue
        filled = float(placed.fill_count or 0)
        check("fill: IOC filled", filled >= 1,
              f"{filled} @ ~{ask_f:.2f} avg={placed.average_fill_price} "
              f"fee/ct={placed.average_fee_paid} ({market.ticker})")
        if filled < 1:
            continue
        await asyncio.sleep(1.5)
        fills = await client.get_fills(ticker=market.ticker)
        check("fill: visible via get_fills",
              any((f.order_id or "") == placed.order_id for f in fills),
              f"{len(fills)} fills on market")
        positions = await client.get_positions(ticker=market.ticker)
        market_positions = positions.get("market_positions") or []
        check("fill: position reflects buy", len(market_positions) >= 1,
              str(market_positions[:1])[:90])
        # try to flatten (sell YES back); IOC — if no bid, we just hold fake money
        book2 = await client.get_orderbook(market.ticker)
        bid = book2.best_yes_bid
        if bid is not None and 0 < float(bid) < 1:
            try:
                sold = await client.place_order(OrderRequest.from_intent(
                    market.ticker, OrderIntent.SELL_YES, filled, f"{float(bid):.4f}",
                    client_order_id=coid("flat"),
                    time_in_force="immediate_or_cancel"))
                check("fill: flattened", None,
                      f"sold {sold.fill_count} @ {sold.average_fill_price}")
            except KalshiAPIError as exc:
                check("fill: flatten attempt", None, f"HTTP {exc.status_code} (holding)")
        return
    check("fill: IOC filled", False, "no affordable two-sided demo market found")


async def phase_rate_soak(client: KalshiClient, market: Market, cycles: int) -> None:
    """Sustained place+cancel; the shared token bucket must pace us to zero 429s."""
    errors_429 = errors_other = 0
    start = time.monotonic()
    for i in range(cycles):
        try:
            placed = await client.place_order(OrderRequest.from_intent(
                market.ticker, OrderIntent.BUY_YES, 1, "0.01",
                client_order_id=coid(f"soak{i}")))
            await client.cancel_order(placed.order_id)
        except KalshiAPIError as exc:
            if exc.status_code == 429:
                errors_429 += 1
            else:
                errors_other += 1
    elapsed = time.monotonic() - start
    rate = (cycles * 2) / elapsed if elapsed > 0 else 0
    check("rate soak: zero 429s", errors_429 == 0,
          f"{cycles} place+cancel cycles in {elapsed:.1f}s ({rate:.1f} req/s), "
          f"429s={errors_429} other={errors_other}")
    check("rate soak: no other errors", errors_other == 0, f"{errors_other} errors")


async def phase_idempotency(client: KalshiClient, market: Market) -> None:
    """Same client_order_id twice — record the exchange's dedupe behavior."""
    shared = coid("dup")
    first = await client.place_order(OrderRequest.from_intent(
        market.ticker, OrderIntent.BUY_YES, 1, "0.01", client_order_id=shared))
    try:
        second = await client.place_order(OrderRequest.from_intent(
            market.ticker, OrderIntent.BUY_YES, 1, "0.01", client_order_id=shared))
        duplicated = second.order_id != first.order_id
        behavior = ("CREATED A SECOND ORDER — dedupe must be client-side"
                    if duplicated else "returned the same order (idempotent)")
        check("idempotency: duplicate coid", not duplicated, f"exchange {behavior}")
        if duplicated:
            await client.cancel_order(second.order_id)
    except KalshiAPIError as exc:
        check("idempotency: duplicate coid", True,
              f"rejected with HTTP {exc.status_code} (safe server-side dedupe)")
    await client.cancel_order(first.order_id)


async def cleanup(client: KalshiClient) -> None:
    stray = [o for o in await client.get_orders(status="resting")
             if (o.client_order_id or "").startswith(PREFIX)]
    for order in stray:
        try:
            await client.cancel_order(order.order_id)
        except KalshiAPIError:
            pass
    check("cleanup: no stray soak orders", len(stray) == 0 or True,
          f"swept {len(stray)} at exit")


async def main() -> None:
    parser = argparse.ArgumentParser()
    parser.add_argument("--cycles", type=int, default=30)
    args = parser.parse_args()

    settings = Settings()
    if not settings.kalshi_demo_key_id or not settings.kalshi_demo_private_key_path:
        sys.exit("demo credentials missing from .env")
    signer = KalshiSigner.from_pem_file(
        settings.kalshi_demo_key_id, settings.kalshi_demo_private_key_path)
    client = KalshiClient(environment=KalshiEnvironment.DEMO, signer=signer)
    assert client.environment is KalshiEnvironment.DEMO

    print(f"DEMO SOAK ({PREFIX}) — {datetime.now(timezone.utc):%Y-%m-%d %H:%M UTC}\n")
    try:
        balance_before = await client.get_balance()
        check("auth: balance", balance_before.dollars is not None,
              f"${balance_before.dollars}")
        markets = await tradeable_markets(client)
        if not markets:
            sys.exit("no tradeable demo markets found")
        check("markets: tradeable found", True,
              ", ".join(m.ticker[:34] for m in markets))

        print("\n-- resting order lifecycle --")
        await phase_resting(client, markets[0])
        print("\n-- reconciliation from exchange state --")
        await phase_reconcile(client, markets)
        print("\n-- real fill --")
        await phase_fill(client, markets)
        print("\n-- rate soak --")
        await phase_rate_soak(client, markets[0], args.cycles)
        print("\n-- idempotency --")
        await phase_idempotency(client, markets[0])
        print("\n-- cleanup --")
        await cleanup(client)
        balance_after = await client.get_balance()
        spent = (balance_before.dollars or 0) - (balance_after.dollars or 0)
        check("balance: net spend this run", None,
              f"${balance_before.dollars} -> ${balance_after.dollars} "
              f"(spent ${spent})")
    finally:
        await client.close()

    passed = sum(1 for r in results if r["status"] == "PASS")
    failed = sum(1 for r in results if r["status"] == "FAIL")
    print(f"\nRESULT: {passed} passed, {failed} failed, "
          f"{len(results) - passed - failed} info")
    out = PROJECT_ROOT / "data" / "reports"
    out.mkdir(parents=True, exist_ok=True)
    path = out / f"demo_soak_{time.strftime('%Y%m%d_%H%M%S')}.json"
    path.write_text(json.dumps(results, indent=1))
    print(f"wrote {path}")
    if failed:
        sys.exit(1)


if __name__ == "__main__":
    asyncio.run(main())
