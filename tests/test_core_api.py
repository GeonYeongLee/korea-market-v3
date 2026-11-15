"""
Unit tests for Core KIS Live Trading API & Configuration infrastructure.
"""
from __future__ import annotations

import asyncio
import os
import json
from pathlib import Path
from unittest.mock import MagicMock, patch
import pytest

from config.settings import Settings, KISEndpoints, settings, switch_env
from core.auth import TokenManager
from core.rate_limiter import RateLimiter, get_rate_limiter
from core.rest_client import KISRestClient
from core.ws_client import KISWebSocketClient
from core.telemetry import TelemetryManager
from core.krx_symbols import (
    get_krx_name_map,
    get_krx_code_map,
    get_name_by_symbol,
    get_symbol_by_name,
)


def test_settings_dual_environment():
    # Test VTS endpoints
    vts_ep = KISEndpoints.create("vts")
    assert "openapivts.koreainvestment.com" in vts_ep.rest_url
    assert vts_ep.tr_buy == "VTTC0012U"
    assert vts_ep.tr_sell == "VTTC0011U"
    assert vts_ep.tr_cancel == "VTTC0013U"
    assert vts_ep.tr_balance == "VTTC8434R"

    # Test REAL endpoints
    real_ep = KISEndpoints.create("real")
    assert "openapi.koreainvestment.com" in real_ep.rest_url
    assert real_ep.tr_buy == "TTTC0012U"
    assert real_ep.tr_sell == "TTTC0011U"
    assert real_ep.tr_cancel == "TTTC0013U"
    assert real_ep.tr_balance == "TTTC8434R"

    # Test switch_env
    s = Settings()
    s.switch_env("real")
    assert s.env == "real"
    assert s.kis_env == "real"
    assert s.kis_is_virtual is False

    s.switch_env("vts")
    assert s.env == "vts"
    assert s.kis_env == "vts"
    assert s.kis_is_virtual is True

    # Module-level switch_env
    switched = switch_env("vts")
    assert switched.env == "vts"


def test_rate_limiter_token_bucket():
    # VTS rate limiter: 1.8 TPS, capacity 2.0
    limiter_vts = get_rate_limiter("vts")
    assert limiter_vts.rate == pytest.approx(1.8)
    assert limiter_vts.capacity == pytest.approx(2.0)

    # REAL rate limiter: 18.0 TPS, capacity 20.0
    limiter_real = get_rate_limiter("real")
    assert limiter_real.rate == pytest.approx(18.0)
    assert limiter_real.capacity == pytest.approx(20.0)

    # Sync acquire
    fast_limiter = RateLimiter(rate=100.0, capacity=10.0)
    fast_limiter.acquire_sync(1.0)
    assert fast_limiter.tokens <= 10.0

    # Async acquire
    async def _async_test():
        await fast_limiter.acquire_async(1.0)

    asyncio.run(_async_test())


def test_auth_token_manager(tmp_path):
    cfg = Settings()
    cfg.token_cache_dir = tmp_path / ".tokens"
    tm = TokenManager(cfg)

    # Test key hash
    key_hash = tm._get_key_hash()
    assert isinstance(key_hash, str)
    assert len(key_hash) == 12

    # Test atomic write helper
    test_file = tmp_path / ".tokens" / "test_atomic.json"
    tm._atomic_write_json(test_file, {"foo": "bar"})
    assert test_file.exists()
    with open(test_file, "r", encoding="utf-8") as f:
        data = json.load(f)
    assert data == {"foo": "bar"}


def test_rest_client_krx_tick_conformance():
    cfg = Settings()
    limiter = RateLimiter(rate=100.0, capacity=10.0)
    client = KISRestClient(cfg=cfg, limiter=limiter)

    with patch.object(client.session, "post") as mock_post:
        mock_resp = MagicMock()
        mock_resp.json.return_value = {"rt_cd": "0", "msg1": "OK"}
        mock_post.return_value = mock_resp

        with patch.object(client.token_mgr, "get_access_token", return_value="dummy_token"):
            # Test BUY order: price 15234 -> should round floor to 15230 (tick=10 for 5,000~20,000)
            client.order_cash_buy(symbol="005930", qty=10, price=15234, order_type="00")
            called_body = mock_post.call_args[1]["json"]
            assert called_body["ORD_UNPR"] == "15230"
            assert called_body["ORD_QTY"] == "10"
            assert called_body["PDNO"] == "005930"

            # Test SELL order: price 15234 -> should round ceil to 15240
            client.order_cash_sell(symbol="005930", qty=10, price=15234, order_type="00")
            called_body = mock_post.call_args[1]["json"]
            assert called_body["ORD_UNPR"] == "15240"


def test_ws_client_parsing():
    client = KISWebSocketClient()
    assert "/tryitout/H0UNCNT0" in client.ws_url

    # Test raw tick message processing
    received_ticks = []

    async def callback(msg_type, data):
        if msg_type == "TICK":
            received_ticks.append(data)

    client.register_callback(callback)

    # Construct standard pipe tick message:
    # 0: enc_flag | 1: tr_id | 2: cnt | 3: data_str
    # fields: 0=symbol, 1=time, 2=price, 3=sign, 4=change, 5=change_rate, 6=vwap,
    #         7=open, 8=high, 9=low, 10=ask, 11=bid, 12=cntg_vol, 13=acml_vol, 14=acml_tr_pbmn,
    #         15=..., 16=..., 17=..., 18=total_sel_qty, 19=total_buy_qty
    fields = [
        "005930", "090500", "75000", "2", "1000", "1.35", "74800",
        "74000", "75500", "73900", "75100", "75000", "100", "50000", "3750000000",
        "0", "0", "0", "10000", "15000"
    ]
    raw_msg = f"0|H0UNCNT0|001|{'^'.join(fields)}"

    async def run_ws_msg():
        await client._handle_message(raw_msg)

    asyncio.run(run_ws_msg())

    assert len(received_ticks) == 1
    tick = received_ticks[0]
    assert tick["symbol"] == "005930"
    assert tick["price"] == 75000.0
    assert tick["volume"] == 100
    assert tick["volume_power"] == 150.0  # 15000 / 10000 * 100


def test_telemetry_manager(tmp_path):
    tm = TelemetryManager(logs_dir=tmp_path / "logs")
    tm.set_audit_enabled(True)

    # 1. Record scanner evaluation
    tm.record_scanner_evaluation(
        symbol="005930",
        name="삼성전자",
        open_gain_pct=2.5,
        turnover_krw=10000000000,
        day_open=74000,
        current_price=75850,
        day_low=73500,
        mae_pct=-0.68,
        decision="PROMOTED",
        reason="Morning surge",
    )
    assert tm.scanner_audit_file.exists()
    with open(tm.scanner_audit_file, "r", encoding="utf-8") as f:
        line = f.readline()
        record = json.loads(line)
        assert record["symbol"] == "005930"
        assert record["decision"] == "PROMOTED"

    # 2. Record minutely snapshot
    candidates = [
        {
            "rank": 1,
            "symbol": "005930",
            "name": "삼성전자",
            "open_gain_pct": 2.5,
            "turnover_krw": 10000000000,
            "turnover_eok": 100.0,
            "day_open": 74000,
            "current_price": 75850,
            "day_low": 73500,
            "mae_pct": -0.68,
            "status": "PROMOTED",
            "reason": "Top Turnover",
        }
    ]
    tm.record_minutely_scanner_snapshot("090500", candidates, ["005930"])
    assert tm.scanner_1min_file.exists()

    # 3. Record trade event
    tm.record_trade_event("BREAKOUT_TRIGGERED", "005930", {"price": 75850, "qty": 10})
    assert tm.trades_audit_file.exists()
    with open(tm.trades_audit_file, "r", encoding="utf-8") as f:
        line = f.readline()
        record = json.loads(line)
        assert record["event_type"] == "BREAKOUT_TRIGGERED"
        assert record["price"] == 75850


def test_krx_symbols():
    name_map = get_krx_name_map()
    assert isinstance(name_map, dict)
    assert len(name_map) > 0
    assert "005930" in name_map
    assert name_map["005930"] == "삼성전자"

    code_map = get_krx_code_map()
    assert isinstance(code_map, dict)
    assert code_map.get("삼성전자") == "005930"

    assert get_name_by_symbol("005930") == "삼성전자"
    assert get_symbol_by_name("삼성전자") == "005930"
