"""Pydantic models for Kalshi Trade API v2 objects.

The API (as of mid-2026) reports prices as dollar strings with deci-cent
precision (`yes_bid_dollars: "0.2600"`, `price_level_structure:
"tapered_deci_cent"`). We parse money into Decimal dollars and expose float
cents where convenient. Models allow extra fields so API additions never
break parsing; raw payloads are also persisted verbatim by the tape recorder.
"""

from __future__ import annotations

from datetime import datetime, timedelta
from decimal import Decimal, InvalidOperation
from typing import Any, Literal

from pydantic import BaseModel, ConfigDict, Field, field_validator


def _to_decimal(v: Any) -> Decimal | None:
    """Coerce API money values ('' | '0.2600' | 0.26 | None) to Decimal."""
    if v is None or v == "":
        return None
    try:
        return Decimal(str(v))
    except InvalidOperation as exc:
        raise ValueError(f"unparseable money value: {v!r}") from exc


class KalshiModel(BaseModel):
    model_config = ConfigDict(extra="allow", populate_by_name=True)


class Series(KalshiModel):
    ticker: str
    title: str = ""
    category: str = ""
    frequency: str = ""
    fee_type: str = ""
    fee_multiplier: float | None = None


class Market(KalshiModel):
    ticker: str
    event_ticker: str = ""
    market_type: str = ""
    title: str = ""
    status: str = ""
    result: str = ""  # "yes" | "no" | "" (unsettled)
    strike_type: str = ""

    open_time: datetime | None = None
    close_time: datetime | None = None
    expected_expiration_time: datetime | None = None

    floor_strike: Decimal | None = None
    expiration_value: Decimal | None = None

    yes_bid: Decimal | None = Field(default=None, alias="yes_bid_dollars")
    yes_ask: Decimal | None = Field(default=None, alias="yes_ask_dollars")
    no_bid: Decimal | None = Field(default=None, alias="no_bid_dollars")
    no_ask: Decimal | None = Field(default=None, alias="no_ask_dollars")
    last_price: Decimal | None = Field(default=None, alias="last_price_dollars")
    notional_value: Decimal | None = Field(default=None, alias="notional_value_dollars")
    liquidity: Decimal | None = Field(default=None, alias="liquidity_dollars")

    volume_fp: Decimal | None = None
    open_interest_fp: Decimal | None = None

    _money = field_validator(
        "floor_strike",
        "expiration_value",
        "yes_bid",
        "yes_ask",
        "no_bid",
        "no_ask",
        "last_price",
        "notional_value",
        "liquidity",
        "volume_fp",
        "open_interest_fp",
        mode="before",
    )(_to_decimal)

    @property
    def window_minutes(self) -> float | None:
        if self.open_time and self.close_time:
            return (self.close_time - self.open_time) / timedelta(minutes=1)
        return None

    @property
    def is_settled(self) -> bool:
        return self.result in ("yes", "no")

    def settlement_consistent(self) -> bool | None:
        """Cross-check `result` against floor_strike vs expiration_value.

        The market record itself is the source of truth for settlement; this
        validates our reading of it. Returns None when not yet settled.
        Assumes strike_type greater_or_equal (the 15m up/down convention).
        """
        if not self.is_settled or self.floor_strike is None or self.expiration_value is None:
            return None
        went_up = self.expiration_value >= self.floor_strike
        return went_up == (self.result == "yes")


class OrderbookLevel(KalshiModel):
    price: Decimal  # dollars
    quantity: Decimal  # contracts (fractional trading allowed)


class Orderbook(KalshiModel):
    """Kalshi books quote bids only: bids on YES and bids on NO.

    Asks are implied: best YES ask = 1.00 - best NO bid, and vice versa.
    """

    yes_bids: list[OrderbookLevel] = []
    no_bids: list[OrderbookLevel] = []

    @classmethod
    def from_api(cls, payload: dict[str, Any]) -> "Orderbook":
        book = payload.get("orderbook_fp") or payload.get("orderbook") or {}
        divisor = Decimal(1) if "orderbook_fp" in payload else Decimal(100)

        def levels(raw: list | None) -> list[OrderbookLevel]:
            out = [
                OrderbookLevel(price=Decimal(str(p)) / divisor, quantity=Decimal(str(q)))
                for p, q in (raw or [])
            ]
            out.sort(key=lambda lv: lv.price, reverse=True)  # best bid first
            return out

        return cls(
            yes_bids=levels(book.get("yes_dollars") or book.get("yes")),
            no_bids=levels(book.get("no_dollars") or book.get("no")),
        )

    @property
    def best_yes_bid(self) -> Decimal | None:
        return self.yes_bids[0].price if self.yes_bids else None

    @property
    def best_yes_ask(self) -> Decimal | None:
        return Decimal(1) - self.no_bids[0].price if self.no_bids else None

    @property
    def spread(self) -> Decimal | None:
        if self.best_yes_bid is None or self.best_yes_ask is None:
            return None
        return self.best_yes_ask - self.best_yes_bid

    @property
    def mid(self) -> Decimal | None:
        if self.best_yes_bid is None or self.best_yes_ask is None:
            return None
        return (self.best_yes_bid + self.best_yes_ask) / 2


class Balance(KalshiModel):
    balance: Decimal | None = None  # cents in legacy field
    balance_dollars: Decimal | None = None

    _money = field_validator("balance", "balance_dollars", mode="before")(_to_decimal)

    @property
    def dollars(self) -> Decimal | None:
        if self.balance_dollars is not None:
            return self.balance_dollars
        if self.balance is not None:
            return self.balance / 100
        return None


class OrderRequest(KalshiModel):
    """Body for POST /portfolio/orders. Prices are integer cents."""

    ticker: str
    client_order_id: str
    side: Literal["yes", "no"]
    action: Literal["buy", "sell"]
    count: int
    type: Literal["limit", "market"] = "limit"
    yes_price: int | None = None
    no_price: int | None = None
    expiration_ts: int | None = None
    post_only: bool | None = None

    def body(self) -> dict[str, Any]:
        return self.model_dump(exclude_none=True)


class Order(KalshiModel):
    order_id: str = ""
    client_order_id: str = ""
    ticker: str = ""
    side: str = ""
    action: str = ""
    status: str = ""
    type: str = ""


class Fill(KalshiModel):
    trade_id: str = ""
    order_id: str = ""
    ticker: str = ""
    side: str = ""
    action: str = ""
    is_taker: bool | None = None
    created_time: datetime | None = None


class Settlement(KalshiModel):
    ticker: str = ""
    market_result: str = ""
    settled_time: datetime | None = None
