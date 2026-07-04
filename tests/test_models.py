"""Model parsing tests against real API payload shapes (captured 2026-07-04)."""

from decimal import Decimal

from kalshibot.kalshi.models import Balance, Market, Orderbook

OPEN_MARKET = {
    "ticker": "KXBTC15M-26JUL041600-00",
    "event_ticker": "KXBTC15M-26JUL041600",
    "market_type": "binary",
    "title": "BTC price up in next 15 mins?",
    "status": "active",
    "result": "",
    "strike_type": "greater_or_equal",
    "open_time": "2026-07-04T19:45:00Z",
    "close_time": "2026-07-04T20:00:00Z",
    "expected_expiration_time": "2026-07-04T20:05:00Z",
    "floor_strike": 63318.53,
    "expiration_value": "",
    "yes_bid_dollars": "0.2600",
    "yes_ask_dollars": "0.2700",
    "no_bid_dollars": "0.7300",
    "no_ask_dollars": "0.7400",
    "last_price_dollars": "0.2700",
    "notional_value_dollars": "1.0000",
    "liquidity_dollars": "0.0000",
    "volume_fp": "372572.30",
    "open_interest_fp": "199851.63",
    "price_level_structure": "tapered_deci_cent",
}

SETTLED_MARKET = {
    "ticker": "KXBTC15M-26JUL041545-45",
    "status": "finalized",
    "result": "yes",
    "strike_type": "greater_or_equal",
    "floor_strike": 63313.33,
    "expiration_value": "63318.53",
    "open_time": "2026-07-04T19:30:00Z",
    "close_time": "2026-07-04T19:45:00Z",
}

ORDERBOOK_FP = {
    "orderbook_fp": {
        "yes_dollars": [["0.1900", "506.00"], ["0.2100", "2757.33"], ["0.2000", "727.00"]],
        "no_dollars": [["0.7200", "377.19"], ["0.7400", "1728.25"], ["0.7300", "450.00"]],
    }
}


def test_open_market_parsing():
    m = Market.model_validate(OPEN_MARKET)
    assert m.yes_bid == Decimal("0.26")
    assert m.yes_ask == Decimal("0.27")
    assert m.floor_strike == Decimal("63318.53")
    assert m.expiration_value is None  # empty string -> None
    assert m.window_minutes == 15.0
    assert not m.is_settled
    assert m.settlement_consistent() is None


def test_settled_market_settlement_math():
    m = Market.model_validate(SETTLED_MARKET)
    assert m.is_settled
    assert m.expiration_value == Decimal("63318.53")
    # expiration_value >= floor_strike and result == yes -> consistent
    assert m.settlement_consistent() is True

    wrong = Market.model_validate({**SETTLED_MARKET, "result": "no"})
    assert wrong.settlement_consistent() is False

    down = Market.model_validate(
        {**SETTLED_MARKET, "expiration_value": "63000.00", "result": "no"}
    )
    assert down.settlement_consistent() is True


def test_orderbook_fp_parsing_and_derived_quotes():
    book = Orderbook.from_api(ORDERBOOK_FP)
    # bids sorted best-first regardless of API order
    assert book.best_yes_bid == Decimal("0.21")
    assert [lv.price for lv in book.no_bids] == [
        Decimal("0.74"),
        Decimal("0.73"),
        Decimal("0.72"),
    ]
    # yes ask implied from best no bid: 1.00 - 0.74 = 0.26
    assert book.best_yes_ask == Decimal("0.26")
    assert book.spread == Decimal("0.05")
    assert book.mid == Decimal("0.235")


def test_orderbook_legacy_cent_format():
    book = Orderbook.from_api({"orderbook": {"yes": [[21, 500]], "no": [[74, 300]]}})
    assert book.best_yes_bid == Decimal("0.21")
    assert book.best_yes_ask == Decimal("0.26")


def test_empty_orderbook():
    book = Orderbook.from_api({"orderbook_fp": {"yes_dollars": [], "no_dollars": None}})
    assert book.best_yes_bid is None
    assert book.best_yes_ask is None
    assert book.spread is None


def test_balance_legacy_cents_and_dollars():
    assert Balance.model_validate({"balance": 12345}).dollars == Decimal("123.45")
    assert Balance.model_validate({"balance_dollars": "99.10"}).dollars == Decimal("99.10")
