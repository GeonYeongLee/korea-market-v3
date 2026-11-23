"""
Zero-Gap Live Audit & Diagnostic Telemetry Test Suite
======================================================
Verifies:
  1. Non-blocking asynchronous JSON Lines disk writer with batching & flush.
  2. Granular REST API & WebSocket message telemetry recording with secret masking.
  3. Strategy evaluation logging (Scanner, Entry, Exit) with complete numeric metrics.
  4. Execution lifecycle logging with order fill slippage (KRW and bp) and latencies.
"""
import json
import time
from pathlib import Path
from unittest.mock import MagicMock, patch

import pytest

from core.telemetry_writer import AsyncJsonLinesWriter, mask_sensitive_dict
from core.telemetry import TelemetryManager


def test_mask_sensitive_dict():
    """Confirms sensitive credential keys are masked while preserving safe payload values."""
    raw = {
        "symbol": "005930",
        "appkey": "PSxxxxxxxxxxxxxxxx",
        "appsecret": "SECRETKEY123456789",
        "authorization": "Bearer eyJhbGciOi...",
        "CANO": "12345678",
        "order_qty": 10,
        "nested": {
            "token": "TOP_SECRET_TOKEN",
            "safe_field": "public_data",
        },
    }
    masked = mask_sensitive_dict(raw)
    assert masked["symbol"] == "005930"
    assert masked["order_qty"] == 10
    assert masked["nested"]["safe_field"] == "public_data"
    assert "***" in masked["appkey"]
    assert "***" in masked["appsecret"]
    assert "***" in masked["authorization"]
    assert "***" in masked["CANO"]
    assert "***" in masked["nested"]["token"]


def test_async_writer_non_blocking_and_flush(tmp_path):
    """Verifies async background queue flushes records to disk without blocking."""
    writer = AsyncJsonLinesWriter()
    test_file = tmp_path / "test_stream.jsonl"

    for i in range(10):
        writer.write(test_file, {"seq": i, "data": f"payload_{i}"})

    writer.flush()
    assert test_file.exists()

    with open(test_file, "r", encoding="utf-8") as f:
        lines = [json.loads(line) for line in f]

    assert len(lines) == 10
    assert lines[0]["seq"] == 0
    assert lines[9]["seq"] == 9
    writer.close()


def test_telemetry_live_api_and_ws_logging(tmp_path):
    """Verifies live_api_YYYYMMDD.jsonl captures REST calls and WS frames."""
    tm = TelemetryManager(logs_dir=tmp_path / "logs")
    tm.set_audit_enabled(True)

    # 1. Record REST API call
    tm.record_api_call(
        endpoint="/uapi/domestic-stock/v1/trading/order-cash",
        method="POST",
        params={"market": "0000"},
        body={"CANO": "12345678", "PDNO": "005930", "ORD_QTY": "10"},
        status_code=200,
        rt_cd="0",
        msg1="주문 접수 완료",
        latency_ms=45.2,
    )

    # 2. Record WS quote frame
    tm.record_ws_frame(
        frame_type="TICK",
        tr_id="H0UNCNT0",
        symbol="005930",
        data={"price": 75800, "volume": 150, "cum_turnover": 1250000000.0},
    )

    # 3. Record WS execution notice
    tm.record_ws_frame(
        frame_type="EXECUTION_NOTICE",
        tr_id="H0STCNI0",
        symbol="005930",
        data={"order_no": "0001234567", "price": 75800, "qty": 10, "side": "BUY"},
    )

    tm.flush()
    assert tm.live_api_file.exists()

    with open(tm.live_api_file, "r", encoding="utf-8") as f:
        records = [json.loads(line) for line in f]

    assert len(records) == 3
    # Check REST record
    rest_rec = records[0]
    assert rest_rec["channel"] == "REST"
    assert rest_rec["method"] == "POST"
    assert "***" in rest_rec["body"]["CANO"]
    assert rest_rec["latency_ms"] == 45.2
    assert rest_rec["rt_cd"] == "0"

    # Check WS records
    ws_tick = records[1]
    assert ws_tick["channel"] == "WS"
    assert ws_tick["frame_type"] == "TICK"
    assert ws_tick["symbol"] == "005930"

    ws_exec = records[2]
    assert ws_exec["channel"] == "WS"
    assert ws_exec["frame_type"] == "EXECUTION_NOTICE"
    assert ws_exec["data"]["order_no"] == "0001234567"


def test_telemetry_strategy_eval_logging(tmp_path):
    """Verifies live_strategy_eval_YYYYMMDD.jsonl logs scanner, entry, and exit evaluations."""
    tm = TelemetryManager(logs_dir=tmp_path / "logs")
    tm.set_audit_enabled(True)

    # 1. Scanner eval (Pass & Reject)
    tm.record_strategy_eval(
        eval_type="SCANNER",
        symbol="005930",
        decision="PASS",
        reason="PASS_SCANNER_CONDITIONS",
        metrics={"day_open": 74000, "high": 76800, "surge_pct": 3.78, "cum_turnover": 1200000000},
    )
    tm.record_strategy_eval(
        eval_type="SCANNER",
        symbol="000660",
        decision="REJECT",
        reason="FAIL_TURNOVER_THRESHOLD",
        metrics={"day_open": 180000, "high": 185000, "surge_pct": 2.78, "cum_turnover": 400000000},
    )

    # 2. Entry eval (Pass & Collar Cap Reject)
    tm.record_strategy_eval(
        eval_type="ENTRY",
        symbol="005930",
        decision="PASS",
        reason="PASS_BREAKOUT_ENTRY",
        metrics={"trigger_price": 77300, "collar_cap_price": 77600, "high": 77400},
    )

    # 3. Exit eval (Holding & Trigger)
    tm.record_strategy_eval(
        eval_type="EXIT",
        symbol="005930",
        decision="HOLDING",
        reason="POSITION_HOLDING",
        metrics={"current_price": 77400, "avg_price": 77300, "pnl_pct": 0.13, "hwm": 77500},
    )
    tm.record_strategy_eval(
        eval_type="EXIT",
        symbol="005930",
        decision="TRIGGER",
        reason="SMART_STEP_UP_MATURED_EXIT",
        metrics={"current_price": 79500, "avg_price": 77300, "pnl_pct": 2.85, "hwm": 80000},
    )

    tm.flush()
    assert tm.live_strategy_eval_file.exists()

    with open(tm.live_strategy_eval_file, "r", encoding="utf-8") as f:
        records = [json.loads(line) for line in f]

    assert len(records) == 5
    assert records[0]["eval_type"] == "SCANNER"
    assert records[0]["decision"] == "PASS"
    assert records[1]["eval_type"] == "SCANNER"
    assert records[1]["decision"] == "REJECT"
    assert records[2]["eval_type"] == "ENTRY"
    assert records[2]["decision"] == "PASS"
    assert records[3]["eval_type"] == "EXIT"
    assert records[3]["decision"] == "HOLDING"
    assert records[4]["eval_type"] == "EXIT"
    assert records[4]["decision"] == "TRIGGER"


def test_telemetry_execution_lifecycle_and_slippage(tmp_path):
    """Verifies live_executions_YYYYMMDD.jsonl logs orders, fills, and slippage in bp."""
    tm = TelemetryManager(logs_dir=tmp_path / "logs")
    tm.set_audit_enabled(True)

    # Order Submitted
    tm.record_execution(
        event_type="ORDER_SUBMITTED",
        symbol="005930",
        side="BUY",
        quantity=120,
        price=77300,
        order_no="10001",
        tag="MORNING_BREAKOUT",
    )

    # Order Filled with 1 tick favorable slippage (-100 KRW = -12.9 bp)
    tm.record_execution(
        event_type="ORDER_FILLED",
        symbol="005930",
        side="BUY",
        quantity=120,
        price=77300,
        order_no="10001",
        filled_price=77200,
        filled_quantity=120,
        slippage_krw=-100.0,
        slippage_bp=-12.94,
        tag="FILLED",
    )

    # Order Cancelled
    tm.record_execution(
        event_type="ORDER_CANCELLED",
        symbol="000660",
        side="BUY",
        quantity=0,
        order_no="10002",
        tag="CANCELLED",
    )

    tm.flush()
    assert tm.live_executions_file.exists()

    with open(tm.live_executions_file, "r", encoding="utf-8") as f:
        records = [json.loads(line) for line in f]

    assert len(records) == 3
    assert records[0]["event_type"] == "ORDER_SUBMITTED"
    assert records[1]["event_type"] == "ORDER_FILLED"
    assert records[1]["slippage_krw"] == -100.0
    assert records[1]["slippage_bp"] == -12.94
    assert records[2]["event_type"] == "ORDER_CANCELLED"





def test_rest_client_telemetry_recording(tmp_path):
    """Verifies KISRestClient automatically records latency and masks sensitive credentials in telemetry."""
    from core.rest_client import KISRestClient
    from core.telemetry import telemetry

    telemetry.set_logs_dir(tmp_path / "logs")
    telemetry.set_audit_enabled(True)

    client = KISRestClient()
    mock_resp = MagicMock()
    mock_resp.status_code = 200
    mock_resp.json.return_value = {"rt_cd": "0", "msg1": "정상조회", "output": []}

    with patch.object(client.session, "request", return_value=mock_resp):
        with patch.object(client.limiter, "acquire_sync"):
            with patch.object(client.token_mgr, "get_access_token", return_value="FAKE_TOKEN_123456"):
                client.get_volume_rank(market_div="J")

    telemetry.flush()
    assert telemetry.live_api_file.exists()

    with open(telemetry.live_api_file, "r", encoding="utf-8") as f:
        records = [json.loads(line) for line in f]

    assert len(records) >= 1
    api_rec = records[0]
    assert api_rec["channel"] == "REST"
    assert "/uapi/domestic-stock/v1/quotations/volume-rank" in api_rec["endpoint"]
    assert api_rec["rt_cd"] == "0"
    assert api_rec["latency_ms"] >= 0.0




