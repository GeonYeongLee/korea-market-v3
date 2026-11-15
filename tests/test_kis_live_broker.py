"""
Unit tests for KisLiveBroker adapter.
"""
from datetime import datetime
from unittest.mock import MagicMock
import pytest

from engine.models import (
    Order, OrderEvent, OrderSide, OrderType, OrderStatus,
    Position, AccountBalance, MinuteBar, TickData
)
from engine.broker.kis_live import KisLiveBroker


@pytest.fixture
def mock_client():
    client = MagicMock()
    # Mock successful buy
    client.order_cash_buy.return_value = {
        "rt_cd": "0",
        "msg1": "주문 전송 완료",
        "output": {"ODNO": "0000012345"},
    }
    # Mock successful sell
    client.order_cash_sell.return_value = {
        "rt_cd": "0",
        "msg1": "주문 전송 완료",
        "output": {"ODNO": "0000012346"},
    }
    # Mock cancel
    client.order_cancel.return_value = {
        "rt_cd": "0",
        "msg1": "취소 완료",
    }
    # Mock balance
    client.get_balance.return_value = {
        "holdings": [
            {
                "pdno": "005930",
                "hldg_qty": "100",
                "pchs_avg_pric": "70000.0",
                "prpr": "71000.0",
            }
        ],
        "summary": {
            "dnca_tot_amt": "5000000.0",
            "tot_evlu_amt": "12100000.0",
        },
    }
    return client


def test_submit_order_krx_tick_conformance(mock_client):
    broker = KisLiveBroker(mode="vts", client=mock_client)
    events = []
    broker.register_event_listener(lambda e: events.append(e))

    # Buy order with price needing KRX floor rounding
    # 70,001 KRW -> tick is 100 for stocks >= 50,000 and < 100,000 -> 70,000
    order = Order(
        order_id="test_ord_1",
        symbol="005930",
        side=OrderSide.BUY,
        order_type=OrderType.LIMIT,
        quantity=10,
        price=70050.0,  # Below 100-won tick boundary
    )

    odno = broker.submit_order(order)
    assert odno == "0000012345"
    assert order.status == OrderStatus.PENDING
    assert len(events) == 1
    assert events[0].status == OrderStatus.PENDING
    assert events[0].order_id == "0000012345"

    mock_client.order_cash_buy.assert_called_once_with(
        symbol="005930",
        qty=10,
        price=70000,
        order_type="00",
    )


def test_submit_order_sl_eod_routes_market_order(mock_client):
    broker = KisLiveBroker(mode="vts", client=mock_client)

    # Sell order with emergency SL tag
    order = Order(
        order_id="test_ord_2",
        symbol="005930",
        side=OrderSide.SELL,
        order_type=OrderType.LIMIT,
        quantity=10,
        price=68000.0,
        tag="STOP_LOSS_1520",
    )

    odno = broker.submit_order(order)
    assert odno == "0000012346"

    # Must be routed as market order ("01") with price 0
    mock_client.order_cash_sell.assert_called_once_with(
        symbol="005930",
        qty=10,
        price=0,
        order_type="01",
    )


def test_submit_order_rejection(mock_client):
    mock_client.order_cash_buy.return_value = {
        "rt_cd": "1",
        "msg1": "모의투자 예수금 부족",
    }
    broker = KisLiveBroker(mode="vts", client=mock_client)
    events = []
    broker.register_event_listener(lambda e: events.append(e))

    order = Order(
        order_id="test_ord_rej",
        symbol="005930",
        side=OrderSide.BUY,
        order_type=OrderType.LIMIT,
        quantity=1000,
        price=70000.0,
    )

    odno = broker.submit_order(order)
    assert odno == ""
    assert order.status == OrderStatus.REJECTED
    assert len(events) == 1
    assert events[0].status == OrderStatus.REJECTED
    assert "예수금 부족" in events[0].message


def test_cancel_order(mock_client):
    broker = KisLiveBroker(mode="vts", client=mock_client)
    order = Order(
        order_id="test_ord_c",
        symbol="005930",
        side=OrderSide.BUY,
        order_type=OrderType.LIMIT,
        quantity=10,
        price=70000.0,
    )
    odno = broker.submit_order(order)
    assert odno in broker._pending_orders

    success = broker.cancel_order(odno)
    assert success is True
    assert odno not in broker._pending_orders
    mock_client.order_cancel.assert_called_once_with(order_no=odno, qty=0, cancel_all=True)


def test_execution_notice_handling(mock_client):
    broker = KisLiveBroker(mode="vts", client=mock_client)
    events = []
    broker.register_event_listener(lambda e: events.append(e))

    # 1. Buy 50 shares @ 70,000
    broker.handle_execution_notice({
        "order_no": "0001",
        "symbol": "005930",
        "side": "BUY",
        "qty": 50,
        "price": 70000.0,
    })

    pos = broker.get_position("005930")
    assert pos is not None
    assert pos.quantity == 50
    assert pos.shares == 50
    assert pos.avg_price == 70000.0
    assert pos.avg_entry_price == 70000.0
    assert pos.current_price == 70000.0
    assert pos.high_water_mark == 70000.0

    assert len(events) == 1
    assert events[0].status == OrderStatus.FILLED
    assert events[0].filled_quantity == 50

    # 2. Add 50 shares @ 72,000 -> Avg price should be 71,000
    broker.handle_execution_notice({
        "order_no": "0002",
        "symbol": "005930",
        "side": "BUY",
        "qty": 50,
        "price": 72000.0,
    })
    pos = broker.get_position("005930")
    assert pos.quantity == 100
    assert pos.avg_price == 71000.0
    assert pos.high_water_mark == 72000.0

    # 3. Partial sell 40 shares @ 73,000 -> 60 shares remain
    broker.handle_execution_notice({
        "order_no": "0003",
        "symbol": "005930",
        "side": "SELL",
        "qty": 40,
        "price": 73000.0,
    })
    pos = broker.get_position("005930")
    assert pos.quantity == 60

    # 4. Sell remaining 60 shares -> Position closed
    broker.handle_execution_notice({
        "order_no": "0004",
        "symbol": "005930",
        "side": "SELL",
        "qty": 60,
        "price": 73000.0,
    })
    assert broker.get_position("005930") is None


def test_on_bar_and_on_tick_price_updates(mock_client):
    broker = KisLiveBroker(mode="vts", client=mock_client)
    broker.handle_execution_notice({
        "order_no": "0001",
        "symbol": "005930",
        "side": "BUY",
        "qty": 10,
        "price": 70000.0,
    })

    pos = broker.get_position("005930")
    assert pos.current_price == 70000.0
    assert pos.high_water_mark == 70000.0

    # Tick update
    tick = TickData(
        symbol="005930",
        dt=datetime.now(),
        price=71500.0,
        volume=10,
    )
    broker.on_tick(tick)
    assert pos.current_price == 71500.0
    assert pos.high_water_mark == 71500.0

    # MinuteBar update with higher high
    bar = MinuteBar(
        symbol="005930",
        dt=datetime.now(),
        open=71500.0,
        high=72500.0,
        low=71200.0,
        close=72000.0,
        volume=500,
    )
    broker.on_bar(bar)
    assert pos.current_price == 72000.0
    assert pos.high_water_mark == 72500.0


def test_sync_account_state_and_get_balance(mock_client):
    broker = KisLiveBroker(mode="vts", client=mock_client)
    broker.sync_account_state()

    pos = broker.get_position("005930")
    assert pos is not None
    assert pos.quantity == 100
    assert pos.avg_price == 70000.0
    assert pos.current_price == 71000.0

    bal = broker.get_balance()
    assert isinstance(bal, AccountBalance)
    assert bal.cash == 5_000_000.0
    assert bal.total_equity == 12_100_000.0
    assert "005930" in bal.positions
