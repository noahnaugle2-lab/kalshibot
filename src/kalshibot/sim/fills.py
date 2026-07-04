"""Fill simulator: execute simulated orders against recorded order books.

Two execution styles, matching how the live order manager will work:

- taker: cross the book immediately (IOC). Fills walk the actual recorded
  levels, so price impact IS the slippage model — a 500-contract order on a
  thin ZEC book fills progressively worse, exactly as it would live. Taker
  fees use the exact Kalshi formula per level price.
- maker: rest at a price and fill from subsequent trade prints. Strict
  trade-through (a print at a better price than ours) fills fully; prints AT
  our price fill only `queue_fraction` of printed size (we can't know our
  queue position, so we assume we're behind most of it — conservative by
  default). Maker fills are fee-free on these series.

All prices in a fill are in the *traded side's own terms* (a BUY_NO fill at
0.97 cost 97 cents). The YES/NO fee symmetry means P(1-P) is identical either
way, so fees are side-agnostic.

Shared by shadow mode (live book) and the replay engine (recorded book) —
one implementation, no divergence.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from decimal import Decimal

from kalshibot.features.fees import taker_fee
from kalshibot.kalshi.models import Orderbook, OrderbookLevel, OrderIntent

DEFAULT_QUEUE_FRACTION = 0.25


@dataclass
class SimFill:
    price: float          # in the intent's own terms
    contracts: float
    fee: float
    liquidity: str        # "taker" | "maker"


@dataclass
class FillResult:
    intent: OrderIntent
    fills: list[SimFill] = field(default_factory=list)

    @property
    def filled(self) -> float:
        return sum(f.contracts for f in self.fills)

    @property
    def avg_price(self) -> float | None:
        if not self.fills:
            return None
        return sum(f.price * f.contracts for f in self.fills) / self.filled

    @property
    def total_fee(self) -> float:
        return sum(f.fee for f in self.fills)


def _levels_for(intent: OrderIntent, book: Orderbook) -> list[tuple[float, float]]:
    """(price_in_intent_terms, qty) levels this intent crosses, best first.

    Kalshi books hold bids on YES and bids on NO; asks are the complement of
    the opposite side's bids (ask YES at p == bid NO at 1-p).
    """
    if intent is OrderIntent.BUY_YES:      # cross YES asks = 1 - NO bids
        return [(round(1 - float(l.price), 4), float(l.quantity)) for l in book.no_bids]
    if intent is OrderIntent.BUY_NO:       # cross NO asks = 1 - YES bids
        return [(round(1 - float(l.price), 4), float(l.quantity)) for l in book.yes_bids]
    if intent is OrderIntent.SELL_YES:     # hit YES bids
        return [(float(l.price), float(l.quantity)) for l in book.yes_bids]
    if intent is OrderIntent.SELL_NO:      # hit NO bids
        return [(float(l.price), float(l.quantity)) for l in book.no_bids]
    raise ValueError(intent)


def _crosses(intent: OrderIntent, level_price: float, limit_price: float) -> bool:
    if intent in (OrderIntent.BUY_YES, OrderIntent.BUY_NO):
        return level_price <= limit_price
    return level_price >= limit_price


def simulate_taker(
    intent: OrderIntent,
    limit_price: float,
    contracts: float,
    book: Orderbook,
) -> FillResult:
    """IOC crossing order: walk recorded levels inside the limit; rest cancels."""
    result = FillResult(intent=intent)
    remaining = contracts
    for level_price, level_qty in _levels_for(intent, book):
        if remaining <= 0 or not _crosses(intent, level_price, limit_price):
            break
        take = min(remaining, level_qty)
        fee = float(taker_fee(Decimal(str(level_price)), Decimal(str(take))))
        result.fills.append(SimFill(level_price, take, fee, "taker"))
        remaining -= take
    return result


@dataclass
class TapeTrade:
    """A recorded Kalshi print (subset of trade_tape columns)."""

    ts: float
    yes_price: float
    count: float
    taker_side: str  # "yes" = taker bought YES; "no" = taker bought NO (sold YES)


def simulate_maker(
    intent: OrderIntent,
    rest_price: float,
    contracts: float,
    trades: list[TapeTrade],
    queue_fraction: float = DEFAULT_QUEUE_FRACTION,
) -> FillResult:
    """Fill a resting order from trade prints occurring while it rests.

    Only BUY intents rest in v1 (strategies quote to open positions).
    A resting BUY_YES at p is a YES bid: it fills when takers SELL yes
    (taker_side == "no") at yes_price < p (full print) or == p (queued).
    A resting BUY_NO at q is a YES ask at 1-q: fills when takers BUY yes
    (taker_side == "yes") at yes_price > 1-q (full) or == 1-q (queued).
    """
    if intent not in (OrderIntent.BUY_YES, OrderIntent.BUY_NO):
        raise ValueError(f"maker simulation only supports BUY intents, got {intent}")
    result = FillResult(intent=intent)
    remaining = contracts
    for trade in sorted(trades, key=lambda t: t.ts):
        if remaining <= 0:
            break
        if intent is OrderIntent.BUY_YES:
            if trade.taker_side != "no":
                continue
            if trade.yes_price < rest_price:
                available = trade.count
            elif trade.yes_price == rest_price:
                available = trade.count * queue_fraction
            else:
                continue
            fill_price = rest_price
        else:  # BUY_NO resting at rest_price == YES ask at 1 - rest_price
            yes_ask = round(1 - rest_price, 4)
            if trade.taker_side != "yes":
                continue
            if trade.yes_price > yes_ask:
                available = trade.count
            elif trade.yes_price == yes_ask:
                available = trade.count * queue_fraction
            else:
                continue
            fill_price = rest_price
        take = min(remaining, available)
        if take > 0:
            result.fills.append(SimFill(fill_price, take, 0.0, "maker"))
            remaining -= take
    return result


def settle_position(
    side: str,
    contracts: float,
    avg_price: float,
    entry_fees: float,
    result: str,
    payout_per_contract: float,
) -> tuple[float, float]:
    """(pnl_gross, pnl_net) for a position held to settlement.

    `payout_per_contract` is what a winning contract actually pays (project
    spec: $0.99; configurable pending empirical verification from real fills).
    """
    won = (side == "yes") == (result == "yes")
    if won:
        gross = (payout_per_contract - avg_price) * contracts
    else:
        gross = -avg_price * contracts
    return gross, gross - entry_fees
