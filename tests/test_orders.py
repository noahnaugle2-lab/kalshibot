"""V2 order construction tests — especially the NO-side price inversion.

The V2 API quotes the YES leg only (bid = buy YES, ask = sell YES). A wrong
inversion here places real orders at wrong prices, so every intent is pinned.
"""

from decimal import Decimal

import pytest

from kalshibot.kalshi.models import CancelResponse, OrderIntent, OrderRequest, OrderResponse


def build(intent: OrderIntent, price: str) -> OrderRequest:
    return OrderRequest.from_intent("KXBTC15M-TEST-00", intent, 5, price)


def test_buy_yes_is_bid_at_price():
    o = build(OrderIntent.BUY_YES, "0.26")
    assert (o.side, o.price) == ("bid", "0.2600")


def test_sell_yes_is_ask_at_price():
    o = build(OrderIntent.SELL_YES, "0.26")
    assert (o.side, o.price) == ("ask", "0.2600")


def test_buy_no_is_ask_at_one_minus_price():
    # Buying NO at 0.97 == offering YES at 0.03
    o = build(OrderIntent.BUY_NO, "0.97")
    assert (o.side, o.price) == ("ask", "0.0300")


def test_sell_no_is_bid_at_one_minus_price():
    o = build(OrderIntent.SELL_NO, "0.97")
    assert (o.side, o.price) == ("bid", "0.0300")


def test_deci_cent_price_preserved():
    o = build(OrderIntent.BUY_YES, "0.015")
    assert o.price == "0.0150"
    o = build(OrderIntent.BUY_NO, "0.985")
    assert o.price == "0.0150"


def test_count_is_two_decimal_string():
    o = OrderRequest.from_intent("T", OrderIntent.BUY_YES, Decimal("2.5"), "0.50")
    assert o.count == "2.50"


@pytest.mark.parametrize("bad", ["0", "1", "1.01", "-0.05"])
def test_price_out_of_range_rejected(bad):
    with pytest.raises(ValueError):
        build(OrderIntent.BUY_YES, bad)


def test_wire_body_defaults():
    o = OrderRequest.from_intent(
        "T", OrderIntent.BUY_YES, 1, "0.01", client_order_id="abc"
    )
    body = o.body()
    assert body == {
        "ticker": "T",
        "side": "bid",
        "count": "1.00",
        "price": "0.0100",
        "time_in_force": "good_till_canceled",
        "self_trade_prevention_type": "taker_at_cross",
        "client_order_id": "abc",
    }


def test_response_models_parse_fixed_point_strings():
    r = OrderResponse.model_validate(
        {"order_id": "o1", "fill_count": "0.00", "remaining_count": "1.00", "ts_ms": 5}
    )
    assert r.remaining_count == Decimal("1.00")
    c = CancelResponse.model_validate({"order_id": "o1", "reduced_by": "1.00"})
    assert c.reduced_by == Decimal("1.00")
