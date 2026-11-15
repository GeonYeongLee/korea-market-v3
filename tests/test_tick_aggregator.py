"""
Unit tests for Real-time TickToBarAggregator and SymbolBarBuilder.
"""
from datetime import datetime
import pytest

from engine.models import MinuteBar, TickData
from engine.datafeed.tick_aggregator import SymbolBarBuilder, TickToBarAggregator


def test_symbol_bar_builder_tick_aggregation():
    builder = SymbolBarBuilder("005930")
    t1 = datetime(2026, 1, 2, 9, 1, 5)
    t2 = datetime(2026, 1, 2, 9, 1, 25)
    t3 = datetime(2026, 1, 2, 9, 1, 55)

    # Tick 1: Open=70,000
    res1 = builder.on_tick({
        "symbol": "005930",
        "price": 70000.0,
        "volume": 100,
        "time": "090105",
        "dt": t1,
        "turnover": 7_000_000.0,
        "cum_turnover": 7_000_000.0,
        "cum_volume": 100,
        "vwap": 70000.0,
    })
    assert res1 is None
    assert builder.open == 70000.0
    assert builder.high == 70000.0
    assert builder.low == 70000.0
    assert builder.close == 70000.0
    assert builder.volume == 100
    assert builder.tick_count == 1

    # Tick 2: High=70,500
    res2 = builder.on_tick({
        "symbol": "005930",
        "price": 70500.0,
        "volume": 50,
        "time": "090125",
        "dt": t2,
        "turnover": 3_525_000.0,
        "cum_turnover": 10_525_000.0,
        "cum_volume": 150,
        "vwap": 70166.0,
    })
    assert res2 is None
    assert builder.high == 70500.0
    assert builder.low == 70000.0
    assert builder.close == 70500.0
    assert builder.volume == 150
    assert builder.tick_count == 2

    # Tick 3: Low=69,800
    res3 = builder.on_tick({
        "symbol": "005930",
        "price": 69800.0,
        "volume": 80,
        "time": "090155",
        "dt": t3,
        "turnover": 5_584_000.0,
        "cum_turnover": 16_109_000.0,
        "cum_volume": 230,
        "vwap": 70039.0,
    })
    assert res3 is None
    assert builder.high == 70500.0
    assert builder.low == 69800.0
    assert builder.close == 69800.0
    assert builder.volume == 230
    assert builder.tick_count == 3


def test_symbol_bar_builder_minute_rollover():
    builder = SymbolBarBuilder("005930")
    t1 = datetime(2026, 1, 2, 9, 1, 10)
    t2 = datetime(2026, 1, 2, 9, 2, 5)

    builder.on_tick({
        "symbol": "005930",
        "price": 70000.0,
        "volume": 100,
        "time": "090110",
        "dt": t1,
        "turnover": 7_000_000.0,
        "cum_turnover": 7_000_000.0,
        "vwap": 70000.0,
    })

    # Rollover to 09:02
    completed_bar = builder.on_tick({
        "symbol": "005930",
        "price": 70200.0,
        "volume": 50,
        "time": "090205",
        "dt": t2,
        "turnover": 3_510_000.0,
        "cum_turnover": 10_510_000.0,
        "vwap": 70066.0,
    })

    assert completed_bar is not None
    assert isinstance(completed_bar, MinuteBar)
    assert completed_bar.symbol == "005930"
    assert completed_bar.open == 70000.0
    assert completed_bar.high == 70000.0
    assert completed_bar.low == 70000.0
    assert completed_bar.close == 70000.0
    assert completed_bar.volume == 100
    assert completed_bar.dt.hour == 9
    assert completed_bar.dt.minute == 1

    # Current builder state is for 09:02
    assert builder.current_minute_str == "0902"
    assert builder.open == 70200.0
    assert builder.volume == 50
    assert builder.tick_count == 1


def test_tick_to_bar_aggregator_callbacks_sync():
    aggregator = TickToBarAggregator()
    bars_received = []
    ticks_received = []

    aggregator.register_bar_callback(lambda sym, bar: bars_received.append((sym, bar)))
    aggregator.register_tick_callback(lambda sym, tick, dt: ticks_received.append((sym, tick, dt)))

    t1 = datetime(2026, 1, 2, 9, 1, 10)
    t2 = datetime(2026, 1, 2, 9, 2, 0)

    aggregator.handle_tick_sync("TICK", {
        "symbol": "005930",
        "price": 50000.0,
        "volume": 10,
        "time": "090110",
        "dt": t1,
    })
    assert len(ticks_received) == 1
    assert len(bars_received) == 0

    aggregator.handle_tick_sync("TICK", {
        "symbol": "005930",
        "price": 50100.0,
        "volume": 20,
        "time": "090200",
        "dt": t2,
    })
    assert len(ticks_received) == 2
    assert len(bars_received) == 1

    sym, bar = bars_received[0]
    assert sym == "005930"
    assert bar.open == 50000.0
    assert bar.volume == 10


def test_tick_to_bar_aggregator_flush():
    aggregator = TickToBarAggregator()
    t1 = datetime(2026, 1, 2, 15, 20, 10)

    aggregator.handle_tick_sync("TICK", {
        "symbol": "000660",
        "price": 180000.0,
        "volume": 50,
        "time": "152010",
        "dt": t1,
    })

    flushed = aggregator.flush()
    assert len(flushed) == 1
    assert flushed[0].symbol == "000660"
    assert flushed[0].close == 180000.0
    assert flushed[0].volume == 50


def test_tick_aggregator_with_tick_data_model():
    aggregator = TickToBarAggregator()
    bars = []
    aggregator.register_bar_callback(lambda bar: bars.append(bar))

    tick1 = TickData(
        symbol="005930",
        dt=datetime(2026, 1, 2, 9, 5, 10),
        price=70000.0,
        volume=10,
        turnover=700000.0,
        cum_turnover=700000.0,
    )
    tick2 = TickData(
        symbol="005930",
        dt=datetime(2026, 1, 2, 9, 6, 2),
        price=70200.0,
        volume=20,
        turnover=1404000.0,
        cum_turnover=2104000.0,
    )

    aggregator.handle_tick_sync("TICK", tick1)
    assert len(bars) == 0

    aggregator.handle_tick_sync("TICK", tick2)
    assert len(bars) == 1
    assert bars[0].symbol == "005930"
    assert bars[0].open == 70000.0
    assert bars[0].volume == 10
