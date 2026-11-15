"""Unit tests for statutory KRX tick size rules."""
import pytest
from core.krx_ticks import get_krx_tick_size, round_to_krx_tick, add_krx_ticks


def test_krx_tick_sizes():
    assert get_krx_tick_size(1500) == 1
    assert get_krx_tick_size(3500) == 5
    assert get_krx_tick_size(12000) == 10
    assert get_krx_tick_size(35000) == 50
    assert get_krx_tick_size(75000) == 100
    assert get_krx_tick_size(350000) == 500
    assert get_krx_tick_size(650000) == 1000


def test_round_to_krx_tick_buy_floor():
    # BUY defaults to floor
    assert round_to_krx_tick(52367.0, side="BUY") == 52300
    assert round_to_krx_tick(1234.5, side="BUY") == 1234
    assert round_to_krx_tick(3542.0, side="BUY") == 3540


def test_round_to_krx_tick_sell_ceil():
    # SELL defaults to ceil
    assert round_to_krx_tick(52301.0, side="SELL") == 52400
    assert round_to_krx_tick(1234.1, side="SELL") == 1235
    assert round_to_krx_tick(3542.0, side="SELL") == 3545

