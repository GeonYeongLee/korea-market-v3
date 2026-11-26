"""Regression tests for live trading bugfixes across REST client, broker, strategy, and dynamic features."""
from datetime import date, datetime
from unittest.mock import MagicMock, patch

import numpy as np
import pandas as pd
import pytest
import requests

from core.dynamic_features import DynamicFeatureProvider
from engine.models import Order, OrderEvent, OrderSide, OrderStatus, OrderType
from core.rest_client import KISRestClient
from engine.broker.kis_live import KisLiveBroker
from strategy.composite import CompositeStrategy


def _make_real_rest_client() -> KISRestClient:
    cfg = MagicMock()
    cfg.env = "real"
    cfg.base_url = "https://openapi.koreainvestment.com:9443"
    cfg.app_key = "test_key"
    cfg.app_secret = "test_secret"
    cfg.account_no = "12345678"
    cfg.account_prdt_cd = "01"
    cfg.access_token = "test_token"
    cfg.endpoints.rest_url = "https://openapi.koreainvestment.com:9443"
    cfg.endpoints.tr_buy = "TTTC0012U"
    cfg.endpoints.tr_sell = "TTTC0011U"
    cfg.endpoints.tr_cancel = "TTTC0013U"
    client = KISRestClient(cfg=cfg)
    client.limiter = MagicMock()
    client.token_mgr = MagicMock()
    client.token_mgr.get_access_token.return_value = "test_token"
    return client


def test_real_orders_do_not_call_deprecated_hashkey():
    """Verify real-env buy, sell, and cancel orders omit deprecated /uapi/hashkey."""
    client = _make_real_rest_client()
    captured_headers = []

    def fake_send(method, url, headers=None, params=None, json=None, timeout=10):
        captured_headers.append(dict(headers or {}))
        resp = MagicMock()
        resp.status_code = 200
        resp.json.return_value = {"rt_cd": "0", "output": {"ODNO": "0001234"}}
        return resp

    with patch.object(client, "generate_hashkey", side_effect=AssertionError("generate_hashkey must not be called")), \
         patch.object(client, "_send_request", side_effect=fake_send):
        r1 = client.order_cash_buy("005930", qty=1, price=50000, order_type="00")
        r2 = client.order_cash_sell("005930", qty=1, price=50000, order_type="00")
        r3 = client.order_cancel("0001234", qty=1)

    assert r1["rt_cd"] == "0" and r2["rt_cd"] == "0" and r3["rt_cd"] == "0"
    assert len(captured_headers) == 3
    for hdrs in captured_headers:
        assert "hashkey" not in {k.lower() for k in hdrs}


def test_send_request_skips_retry_on_business_http_500():
    """Deterministic KIS HTTP 500 with rt_cd='1' & msg1 must NOT trigger transient retry sleeps."""
    client = _make_real_rest_client()
    biz_resp = MagicMock()
    biz_resp.status_code = 500
    biz_resp.json.return_value = {"rt_cd": "1", "msg_cd": "EGW0000", "msg1": "Business error"}

    with patch.object(client.session, "post", return_value=biz_resp) as mock_post, \
         patch("time.sleep") as mock_sleep:
        resp = client._send_request("POST", "https://example.com/order", json={"a": 1})

    assert resp is biz_resp
    assert mock_post.call_count == 1
    mock_sleep.assert_not_called()


def test_kis_live_broker_http_500_skips_phase2_ccld():
    """Explicit HTTP 500 rejection on buy/sell must mark REJECTED immediately without 1s sleep or inquire_daily_ccld."""
    mock_client = _make_real_rest_client()
    mock_client.get_balance = MagicMock(return_value={"cash": 1_000_000.0, "positions": []})

    err_resp = MagicMock()
    err_resp.status_code = 500
    err_resp.json.return_value = {"rt_cd": "1", "msg_cd": "APBK0001", "msg1": "장운영시간이 아닙니다"}
    http_err = requests.exceptions.HTTPError("500 Server Error", response=err_resp)

    mock_client.order_cash_buy = MagicMock(side_effect=http_err)
    mock_client.order_cash_sell = MagicMock(side_effect=http_err)
    mock_client.inquire_daily_ccld = MagicMock()

    broker = KisLiveBroker(client=mock_client, mode="mock")

    buy_order = Order(order_id="B1", symbol="018260", side=OrderSide.BUY, quantity=1, order_type=OrderType.LIMIT, price=10000.0)
    with patch("time.sleep") as mock_sleep:
        res_buy = broker.order_cash_buy("018260", qty=1, price=10000, order_type="00", order=buy_order)
        res_sell = broker.order_cash_sell("018260", qty=1, price=10000, order_type="00", order=buy_order)

    assert res_buy["rt_cd"] == "HTTP_ERROR"
    assert res_sell["rt_cd"] == "HTTP_ERROR"
    assert "장운영시간이 아닙니다" in res_buy["msg1"]
    assert buy_order.status == OrderStatus.REJECTED
    mock_sleep.assert_not_called()
    mock_client.inquire_daily_ccld.assert_not_called()


def test_composite_strategy_rejection_lockout():
    """CompositeStrategy must stop rolling back entered_symbols after 3 REJECTED events on the same symbol."""
    scanner = MagicMock()
    entry = MagicMock()
    entry.entered_symbols = {"018260"}

    def rollback_side_effect(sym: str) -> None:
        entry.entered_symbols.discard(sym)

    entry.rollback_entered_symbol.side_effect = rollback_side_effect
    exit_policy = MagicMock()

    strat = CompositeStrategy(
        scanner=scanner,
        entry=entry,
        exit_policy=exit_policy,
        total_capital=1_000_000.0,
        max_daily_positions=1,
    )
    ctx = MagicMock()
    ctx.get_position.return_value = None
    ctx.get_balance.return_value = MagicMock(cash=1_000_000.0)
    strat.on_day_open(ctx, date(2026, 11, 26))

    for i in range(4):
        strat.on_order_event(
            ctx,
            OrderEvent(
                order_id=f"REJ_{i}",
                symbol="018260",
                side=OrderSide.BUY,
                status=OrderStatus.REJECTED,
                filled_price=0.0,
                filled_quantity=0,
                dt=datetime(2026, 11, 26, 9, 1, i),
                message="Order rejected",
            ),
        )

    assert entry.rollback_entered_symbol.call_count == 2
    assert strat._rejection_counts["018260"] == 4

    # Resets cleanly on next trading day
    strat.on_day_open(ctx, date(2026, 11, 27))
    assert strat._rejection_counts == {}


def test_dynamic_feature_provider_live_only_and_precomputed_index(tmp_path):
    """live_only=True matches full settled EOD features rapidly and precomputed index schema loads without error."""
    dates = pd.date_range(end=date.today(), periods=25, freq="B")
    rows = []
    for i, d in enumerate(dates):
        c = 50000.0 + i * 150.0
        rows.append({
            "date": d.date(),
            "symbol": "005930",
            "high": c + 200.0,
            "low": c - 200.0,
            "close": c,
            "prev_close": c - 150.0 if i > 0 else c,
            "turnover": 50_000_000_000.0,
            "adto20": 45_000_000_000.0 + i * 100_000_000.0,
        })
    daily_file = str(tmp_path / "daily.parquet")
    pd.DataFrame(rows).to_parquet(daily_file)

    idx_df = pd.DataFrame({
        "date": [dates[-1].date()],
        "ks_gap_pct": [1.2],
        "kq_gap_pct": [0.8],
        "ks_trend": [-0.015],
        "kq_trend": [-0.025],
        "market_gap_std20": [1.10],
        "is_bear_market": [True],
    })
    idx_file = str(tmp_path / "index.parquet")
    idx_df.to_parquet(idx_file)

    p_live = DynamicFeatureProvider(daily_file=daily_file, index_file=idx_file)
    p_live.load(live_only=True)
    assert len(p_live._stock_features_by_date) <= 2
    assert p_live._index_features_by_date == {}

    p_full = DynamicFeatureProvider(daily_file=daily_file, index_file=idx_file)
    p_full.load(live_only=False)

    # Verify latest settled features match full load
    assert np.isclose(
        p_live._latest_settled_features["005930"].prior_atr20_pct,
        p_full._latest_settled_features["005930"].prior_atr20_pct,
    )
    assert np.isclose(
        p_live._latest_settled_features["005930"].prior_adto20,
        p_full._latest_settled_features["005930"].prior_adto20,
    )

    idx_feat = p_full.get_index_features(dates[-1].date())
    assert idx_feat.ks_trend == -0.015
    assert idx_feat.kq_trend == -0.025
    assert idx_feat.market_gap_std20 == 1.10
    assert idx_feat.is_bear_market is True
    assert idx_feat.dynamic_max_gap == 1.10
