"""Fill simulator tests: book walking, maker prints, settlement PnL."""

import pytest

from kalshibot.kalshi.models import Orderbook, OrderIntent
from kalshibot.sim.fills import TapeTrade, settle_position, simulate_maker, simulate_taker


def book(yes_bids=None, no_bids=None) -> Orderbook:
    return Orderbook.from_api({
        "orderbook_fp": {
            "yes_dollars": [[str(p), str(q)] for p, q in (yes_bids or [])],
            "no_dollars": [[str(p), str(q)] for p, q in (no_bids or [])],
        }
    })


# ------------------------------------------------------------------ taker

def test_taker_buy_yes_walks_no_bids():
    # NO bids 0.60/0.55 -> YES asks 0.40 (5 qty) then 0.45 (10 qty)
    b = book(no_bids=[(0.60, 5), (0.55, 10)])
    r = simulate_taker(OrderIntent.BUY_YES, limit_price=0.45, contracts=10, book=b)
    assert r.filled == 10
    assert [(f.price, f.contracts) for f in r.fills] == [(0.40, 5), (0.45, 5)]
    assert r.avg_price == pytest.approx(0.425)
    # fees: ceil(.07*5*.40*.60)=0.09 + ceil(.07*5*.45*.55)=0.09
    assert r.total_fee == pytest.approx(0.18)


def test_taker_respects_limit():
    b = book(no_bids=[(0.60, 5), (0.55, 10)])  # asks 0.40, 0.45
    r = simulate_taker(OrderIntent.BUY_YES, limit_price=0.40, contracts=10, book=b)
    assert r.filled == 5  # only the 0.40 level crosses; rest cancels (IOC)
    assert r.avg_price == pytest.approx(0.40)


def test_taker_buy_no_uses_yes_bids():
    b = book(yes_bids=[(0.30, 8)])  # NO ask = 0.70
    r = simulate_taker(OrderIntent.BUY_NO, limit_price=0.70, contracts=5, book=b)
    assert r.filled == 5 and r.avg_price == pytest.approx(0.70)


def test_taker_sell_yes_hits_bids():
    b = book(yes_bids=[(0.30, 8), (0.28, 10)])
    r = simulate_taker(OrderIntent.SELL_YES, limit_price=0.28, contracts=12, book=b)
    assert [(f.price, f.contracts) for f in r.fills] == [(0.30, 8), (0.28, 4)]


def test_taker_empty_book_no_fill():
    r = simulate_taker(OrderIntent.BUY_YES, 0.99, 10, book())
    assert r.filled == 0 and r.avg_price is None


# ------------------------------------------------------------------ maker

def test_maker_trade_through_fills_fully():
    trades = [TapeTrade(ts=1, yes_price=0.38, count=20, taker_side="no")]
    r = simulate_maker(OrderIntent.BUY_YES, rest_price=0.40, contracts=10, trades=trades)
    assert r.filled == 10 and r.fills[0].price == 0.40 and r.total_fee == 0.0


def test_maker_at_price_fills_queue_fraction():
    trades = [TapeTrade(ts=1, yes_price=0.40, count=20, taker_side="no")]
    r = simulate_maker(OrderIntent.BUY_YES, 0.40, 10, trades, queue_fraction=0.25)
    assert r.filled == pytest.approx(5.0)  # 20 * 0.25


def test_maker_ignores_wrong_side_prints():
    trades = [TapeTrade(ts=1, yes_price=0.38, count=20, taker_side="yes")]
    r = simulate_maker(OrderIntent.BUY_YES, 0.40, 10, trades)
    assert r.filled == 0


def test_maker_buy_no_fills_on_yes_buying_through():
    # resting BUY_NO at 0.55 == YES ask at 0.45; taker buys YES at 0.47
    trades = [TapeTrade(ts=1, yes_price=0.47, count=6, taker_side="yes")]
    r = simulate_maker(OrderIntent.BUY_NO, rest_price=0.55, contracts=10, trades=trades)
    assert r.filled == 6 and r.fills[0].price == 0.55


def test_maker_rejects_sell_intents():
    with pytest.raises(ValueError):
        simulate_maker(OrderIntent.SELL_YES, 0.40, 10, [])


# -------------------------------------------------------------- settlement

def test_settle_yes_win():
    gross, net = settle_position("yes", 10, 0.425, 0.18, "yes", 0.99)
    assert gross == pytest.approx(5.65)
    assert net == pytest.approx(5.47)


def test_settle_yes_loss():
    gross, net = settle_position("yes", 10, 0.425, 0.18, "no", 0.99)
    assert gross == pytest.approx(-4.25)
    assert net == pytest.approx(-4.43)


def test_settle_no_win():
    gross, _ = settle_position("no", 10, 0.60, 0.0, "no", 0.99)
    assert gross == pytest.approx(3.90)
