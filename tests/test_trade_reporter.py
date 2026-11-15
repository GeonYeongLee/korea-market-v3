"""
Unit tests for trade reporter: symbol mapping, CSV export, and Rich table rendering.
"""
from __future__ import annotations

import os
from datetime import datetime
from engine.models import TradeRecord
from engine.trade_reporter import (
    load_symbol_map,
    export_trades_to_csv,
    render_trades_table,
)


def test_load_symbol_map():
    symbol_map = load_symbol_map()
    assert isinstance(symbol_map, dict)
    assert len(symbol_map) > 0
    # Samsung Electronics test
    if "005930" in symbol_map:
        assert symbol_map["005930"] == "삼성전자"


def test_export_trades_to_csv(tmp_path):
    output_path = str(tmp_path / "reports" / "trades_test.csv")
    trades = [
        TradeRecord(
            trade_id=1,
            symbol="005930",
            entry_dt=datetime(2026, 10, 15, 9, 3),
            exit_dt=datetime(2026, 10, 15, 9, 5),
            entry_price=50000.0,
            exit_price=51000.0,
            quantity=100,
            side="BUY",
            gross_pnl=100000.0,
            net_pnl=95000.0,
            return_pct=0.02,
            holding_seconds=120.0,
            exit_reason="SMART_STEP_UP_TRAILING_SL",
            mae_pct=-0.005,
            mfe_pct=0.025,
        ),
        TradeRecord(
            trade_id=2,
            symbol="000660",
            entry_dt=datetime(2026, 10, 15, 9, 10),
            exit_dt=datetime(2026, 10, 15, 9, 12),
            entry_price=180000.0,
            exit_price=178000.0,
            quantity=50,
            side="BUY",
            gross_pnl=-100000.0,
            net_pnl=-104000.0,
            return_pct=-0.0111,
            holding_seconds=120.0,
            exit_reason="LOCKED_SL",
            mae_pct=-0.015,
            mfe_pct=0.002,
        ),
    ]

    res_path = export_trades_to_csv(trades, output_path)
    assert os.path.exists(res_path)

    # Verify BOM header for UTF-8-sig (0xEF, 0xBB, 0xBF)
    with open(res_path, "rb") as f:
        content_bytes = f.read()
    assert content_bytes.startswith(b"\xef\xbb\xbf")

    # Verify content read with utf-8-sig
    with open(res_path, "r", encoding="utf-8-sig") as f:
        lines = [line.strip() for line in f if line.strip()]

    header = lines[0].split(",")
    expected_headers = [
        "trade_id", "symbol", "name", "entry_dt", "entry_price",
        "exit_dt", "exit_price", "quantity", "holding_min",
        "return_pct", "gross_pnl", "net_pnl", "exit_reason",
        "mae_pct", "mfe_pct",
    ]
    assert header == expected_headers
    assert len(lines) == 3  # Header + 2 trades
    assert "삼성전자" in lines[1]


def test_render_trades_table():
    trades = [
        TradeRecord(
            trade_id=i,
            symbol=f"0000{i:02d}",
            entry_dt=datetime(2026, 10, 15, 9, 3),
            exit_dt=datetime(2026, 10, 15, 9, 10),
            entry_price=10000.0,
            exit_price=10200.0 if i % 2 == 0 else 9800.0,
            quantity=100,
            side="BUY",
            gross_pnl=20000.0 if i % 2 == 0 else -20000.0,
            net_pnl=18000.0 if i % 2 == 0 else -22000.0,
            return_pct=0.02 if i % 2 == 0 else -0.02,
            holding_seconds=420.0,
            exit_reason="LOCKED_SL" if i % 2 != 0 else "TAKE_PROFIT",
        )
        for i in range(1, 60)
    ]

    # Test empty trades table
    empty_table = render_trades_table([])
    assert empty_table is not None

    # Test truncated table when > max_display
    table = render_trades_table(trades, max_display=50)
    assert table is not None
    assert len(table.rows) == 50
    assert "59" in table.caption
    assert "50" in table.caption
    assert "전체 상세 내역은 CSV 보고서를 확인하세요" in table.caption
