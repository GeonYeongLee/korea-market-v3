"""
Unit Tests for Live System & CLI Extensions.
Verifies CLI argument parsing, safety gates, RealtimeLeaderScanner callbacks,
and pre-flight helpers.
"""
from __future__ import annotations

import argparse
import sys
from datetime import datetime, date, time
from unittest.mock import MagicMock, patch

import pytest

from data_pipeline.realtime_scanner import RealtimeLeaderScanner, parse_kis_minute_bars
from check_live_readiness import mask_string
from main import main


def test_mask_string():
    assert mask_string("") == "N/A"
    assert mask_string("12345", 4, 2) == "*****"
    masked = mask_string("1234567890", 2, 2)
    assert masked.startswith("12")
    assert masked.endswith("90")
    assert "*" in masked





def test_parse_kis_minute_bars():
    raw = [
        {
            "stck_bsop_date": "20261110",
            "stck_cntg_hour": "090100",
            "stck_oprc": "50000",
            "stck_hgpr": "51000",
            "stck_lwpr": "49900",
            "stck_prpr": "50800",
            "cntg_vol": "1000",
            "acml_tr_pbmn": "50000000",
        },
        {
            "stck_bsop_date": "20261110",
            "stck_cntg_hour": "090000",
            "stck_oprc": "49500",
            "stck_hgpr": "50000",
            "stck_lwpr": "49500",
            "stck_prpr": "50000",
            "cntg_vol": "2000",
            "acml_tr_pbmn": "100000000",
        },
    ]
    bars = parse_kis_minute_bars("005930", raw)
    assert len(bars) == 2
    # Verify sorted ascending
    assert bars[0].dt < bars[1].dt
    assert bars[0].open == 49500
    assert bars[1].close == 50800


def test_realtime_leader_scanner_callbacks_and_feed():
    mock_client = MagicMock()
    mock_client.get_today_minute_bars.return_value = [
        {
            "stck_bsop_date": "20261110",
            "stck_cntg_hour": "090000",
            "stck_oprc": "10000",
            "stck_hgpr": "10500",
            "stck_lwpr": "9950",
            "stck_prpr": "10400",
            "cntg_vol": "5000",
            "acml_tr_pbmn": "1200000000",
        }
    ]
    mock_feed = MagicMock()
    mock_feed.active_symbols = []

    mock_scanner_comp = MagicMock()
    mock_strategy = MagicMock()
    mock_strategy.scanner = mock_scanner_comp
    mock_strategy.on_bar.side_effect = lambda ctx, bar: mock_scanner_comp.on_bar(bar.date, bar)

    scanner = RealtimeLeaderScanner(
        client=mock_client,
        datafeed=mock_feed,
        strategy=mock_strategy,
    )

    cb_events = []
    scanner.register_callback(lambda sym, meta: cb_events.append((sym, meta)))

    # Feed condition search symbol
    scanner.feed_condition_search_symbol("005930", {"open": 10000, "price": 10400})

    # Assertions
    assert "005930" in scanner._promoted_symbols
    assert len(cb_events) == 1
    assert cb_events[0][0] == "005930"
    mock_scanner_comp.feed_condition_search_symbol.assert_called_with("005930")
    mock_scanner_comp.on_bar.assert_called()
    mock_feed.subscribe_symbol.assert_called_with("005930")


def test_realtime_leader_scanner_scan_cycle_filtering():
    mock_client = MagicMock()
    mock_client.get_condition_search_result.return_value = [
        # Candidate 1: Meets all conditions (+4.0% gain, 15억 TO, low = open)
        {
            "code": "005930",
            "name": "삼성전자",
            "open": "10000",
            "price": "10400",
            "low": "10000",
            "high": "10450",
            "trade_amt": "1500000",  # 15억
            "acml_vol": "150000",
            "chgrate": "4.0",
        },
        # Candidate 2: Rejected by low gain (+1.0%)
        {
            "code": "000660",
            "name": "SK하이닉스",
            "open": "10000",
            "price": "10100",
            "low": "9950",
            "high": "10150",
            "trade_amt": "2000000",
            "acml_vol": "200000",
            "chgrate": "1.0",
        },
        # Candidate 3: Rejected by turnover (2억)
        {
            "code": "035420",
            "name": "NAVER",
            "open": "10000",
            "price": "10500",
            "low": "10000",
            "high": "10550",
            "trade_amt": "200000",  # 2억
            "acml_vol": "20000",
            "chgrate": "5.0",
        },
        # Candidate 4: Rejected by MAE breach (low dropped -3%)
        {
            "code": "035720",
            "name": "카카오",
            "open": "10000",
            "price": "10400",
            "low": "9700",  # -3%
            "high": "10450",
            "trade_amt": "1500000",
            "acml_vol": "150000",
            "chgrate": "4.0",
        },
    ]

    mock_feed = MagicMock()
    mock_feed.active_symbols = []

    scanner = RealtimeLeaderScanner(
        client=mock_client,
        datafeed=mock_feed,
        min_gain_pct=3.0,
        min_turnover_krw=800_000_000.0,
        max_mae_pct=-2.0,
    )

    promoted = scanner.scan_cycle()

    assert promoted == ["005930"]
    assert "005930" in scanner._promoted_symbols
    assert "035720" in scanner._mae_blacklisted


def test_cli_live_safety_gate():
    """Verify `python main.py live` without `--yes` exits with code 1."""
    test_args = ["main.py", "live"]
    with patch.object(sys, "argv", test_args):
        with pytest.raises(SystemExit) as exc_info:
            main()
        assert exc_info.value.code == 1


def test_cli_routine_live_safety_gate():
    """Verify `python main.py routine --mode live` without `--yes` exits with code 1."""
    test_args = ["main.py", "routine", "--mode", "live"]
    with patch.object(sys, "argv", test_args):
        with pytest.raises(SystemExit) as exc_info:
            main()
        assert exc_info.value.code == 1


def test_cli_mock_invocation():
    """Verify `python main.py mock` initializes orchestrator with mock mode."""
    test_args = ["main.py", "mock", "--nowait"]
    with patch.object(sys, "argv", test_args):
        with patch("engine.daily_routine.DailyRoutineOrchestrator") as mock_orch_cls:
            mock_orch = MagicMock()
            mock_orch_cls.return_value = mock_orch
            main()
            mock_orch_cls.assert_called_once_with(
                mode="mock",
                scanner_name="all_weather",
                entry_name="all_weather",
                exit_name="all_weather",
                capital=10_000_000.0,
            )
            mock_orch.run.assert_called_once_with(wait_for_market_open=False)


def test_cli_check_live_invocation():
    """Verify `python main.py check-live --skip-ws` calls run_preflight_checks."""
    test_args = ["main.py", "check-live", "--env", "vts", "--skip-ws"]
    with patch.object(sys, "argv", test_args):
        with patch("check_live_readiness.run_preflight_checks", return_value=True) as mock_check:
            with pytest.raises(SystemExit) as exc:
                main()
            assert exc.value.code == 0
            mock_check.assert_called_once_with(env_override="vts", skip_ws=True)



