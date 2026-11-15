"""
Unit tests for KisRealtimeFeed.
"""
import asyncio
from datetime import datetime
from unittest.mock import MagicMock
import pytest

from engine.models import MinuteBar, TickData, Position
from engine.datafeed.kis_live import KisRealtimeFeed
from engine.datafeed.tick_aggregator import TickToBarAggregator


@pytest.fixture
def mock_ws_client():
    client = MagicMock()
    client.subscribe_symbol = MagicMock()
    client.unsubscribe_symbol = MagicMock()
    return client


def test_symbol_subscription_and_40_cap_protection(mock_ws_client):
    mock_broker = MagicMock()
    # Mock held positions: 000000 and 000001
    mock_broker.get_positions.return_value = {
        "000000": Position("000000", quantity=10, avg_price=1000.0),
        "000001": Position("000001", quantity=20, avg_price=2000.0),
    }

    feed = KisRealtimeFeed(ws_client=mock_ws_client, broker=mock_broker)

    # Subscribe 40 symbols: "000000" to "000039"
    for i in range(40):
        code = f"{i:06d}"
        feed.subscribe_symbol(code)

    assert len(feed.active_symbols) == 40
    assert "000000" in feed.active_symbols
    assert "000001" in feed.active_symbols

    # Subscribe 41st symbol: should evict an unheld symbol (e.g., "000002"), NOT "000000" or "000001"
    feed.subscribe_symbol("999999")
    assert len(feed.active_symbols) == 40
    assert "999999" in feed.active_symbols
    assert "000000" in feed.active_symbols  # Held position protected
    assert "000001" in feed.active_symbols  # Held position protected
    assert "000002" not in feed.active_symbols  # Evicted unheld symbol

    # Unsubscribe
    feed.unsubscribe_symbol("999999")
    assert "999999" not in feed.active_symbols
    assert len(feed.active_symbols) == 39


def test_feed_event_routing_and_streaming(mock_ws_client):
    aggregator = TickToBarAggregator()
    feed = KisRealtimeFeed(ws_client=mock_ws_client, aggregator=aggregator)

    # Push ticks into aggregator
    t1 = datetime(2026, 1, 2, 9, 1, 10)
    t2 = datetime(2026, 1, 2, 9, 2, 0)

    aggregator.handle_tick_sync("TICK", {
        "symbol": "005930",
        "price": 70000.0,
        "volume": 10,
        "time": "090110",
        "dt": t1,
    })

    aggregator.handle_tick_sync("TICK", {
        "symbol": "005930",
        "price": 70500.0,
        "volume": 20,
        "time": "090200",
        "dt": t2,
    })

    # The sync queue should have 2 ticks and 1 bar
    events = []
    while not feed._sync_queue.empty():
        events.append(feed._sync_queue.get_nowait())

    # We expect 2 TickData items and 1 MinuteBar item
    ticks = [e for e in events if isinstance(e, TickData)]
    bars = [e for e in events if isinstance(e, MinuteBar)]

    assert len(ticks) == 2
    assert len(bars) == 1
    assert bars[0].symbol == "005930"
    assert bars[0].open == 70000.0


def test_feed_execution_notice_routing(mock_ws_client):
    mock_broker = MagicMock()
    feed = KisRealtimeFeed(ws_client=mock_ws_client, broker=mock_broker)

    notice = {
        "order_no": "0001",
        "symbol": "005930",
        "side": "BUY",
        "qty": 10,
        "price": 70000.0,
    }

    # Run the async ws message handler
    asyncio.run(feed._on_ws_message("EXECUTION", notice))
    mock_broker.handle_execution_notice.assert_called_once_with(notice)
