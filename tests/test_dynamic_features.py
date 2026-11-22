"""
Unit tests for DynamicFeatureProvider and Lookahead 0% Causal Audit.
Verifies that all dynamic features for day t strictly originate from day t-1 or earlier.
"""
import pytest
from datetime import date, datetime, timedelta
from unittest.mock import MagicMock, patch
import pandas as pd
import numpy as np

from core.dynamic_features import (
    DEFAULT_MARKET_GAP_STD,
    DynamicFeatureProvider,
    IndexDynamicFeatures,
    SymbolDynamicFeatures,
    get_default_dynamic_feature_provider,
)
from core.market_regime import MarketRegimeProvider, _normalize_date
from strategy.registry import build_strategy


def test_dynamic_feature_provider_singleton():
    p1 = get_default_dynamic_feature_provider()
    p2 = get_default_dynamic_feature_provider()
    assert p1 is p2


def test_synthetic_lookahead_zero_audit(tmp_path):
    """
    Causal Audit Test:
    Altering day t's High/Low/Close/Volume/Turnover MUST NOT alter
    the day t dynamic indicators (prior_atr20_pct, prior_adto20).
    """
    dates = pd.date_range("2024-10-01", periods=30, freq="B")
    data = []
    np.random.seed(42)
    base_price = 10000.0

    for i, d in enumerate(dates):
        o = base_price * (1.0 + np.random.uniform(-0.01, 0.01))
        h = o * 1.02
        l = o * 0.98
        c = o * 1.01
        to = 1_000_000_000.0
        data.append({
            "date": d.date(),
            "symbol": "005930",
            "open": o,
            "high": h,
            "low": l,
            "close": c,
            "prev_close": base_price if i > 0 else o,
            "turnover": to,
            "adto20": to,
        })
        base_price = c

    df_base = pd.DataFrame(data)
    file_base = str(tmp_path / "daily_base.parquet")
    df_base.to_parquet(file_base)

    provider_base = DynamicFeatureProvider(daily_file=file_base, index_file=str(tmp_path / "nonexistent.parquet"))
    provider_base.load()

    target_date = dates[25].date()
    feat_base = provider_base.get_stock_features(target_date, "005930")

    # Now tamper day t (target_date) with an extreme 100x spike
    df_tampered = df_base.copy()
    idx = df_tampered[df_tampered["date"] == target_date].index[0]
    df_tampered.loc[idx, "high"] = 999999.0
    df_tampered.loc[idx, "low"] = 1.0
    df_tampered.loc[idx, "close"] = 888888.0
    df_tampered.loc[idx, "turnover"] = 999_999_999_999.0
    df_tampered.loc[idx, "adto20"] = 999_999_999_999.0

    file_tampered = str(tmp_path / "daily_tampered.parquet")
    df_tampered.to_parquet(file_tampered)

    provider_tampered = DynamicFeatureProvider(daily_file=file_tampered, index_file=str(tmp_path / "nonexistent.parquet"))
    provider_tampered.load()

    feat_tampered = provider_tampered.get_stock_features(target_date, "005930")

    # Assert 100% invariance to day t tampering
    assert np.isclose(feat_base.prior_atr20_pct, feat_tampered.prior_atr20_pct, atol=1e-12), (
        "Lookahead bug detected! Tampering day t affected day t prior_atr20_pct!"
    )
    assert np.isclose(feat_base.prior_adto20, feat_tampered.prior_adto20, atol=1e-6), (
        "Lookahead bug detected! Tampering day t affected day t prior_adto20!"
    )


def test_index_lookahead_zero_audit(tmp_path):
    """
    Causal Audit Test for Market Index:
    Altering day t's index values MUST NOT alter day t's trend or gap std.
    """
    dates = pd.date_range("2024-01-01", periods=30, freq="B")
    data = []
    for i, d in enumerate(dates):
        data.append({
            "date": d.strftime("%Y-%m-%d"),
            "ks_close": 2500.0 + i * 5,
            "ks_ma20": 2500.0,
            "kq_close": 800.0 + i * 2,
            "kq_ma20": 800.0,
            "ks_gap_pct": 0.5,
            "kq_gap_pct": 0.3,
        })
    df_base = pd.DataFrame(data)
    idx_file_base = str(tmp_path / "index_base.parquet")
    df_base.to_parquet(idx_file_base)

    p_base = DynamicFeatureProvider(daily_file=str(tmp_path / "nonexistent.parquet"), index_file=idx_file_base)
    p_base.load()

    target_date = dates[25].date()
    feat_base = p_base.get_index_features(target_date)

    # Tamper day t
    df_tampered = df_base.copy()
    df_tampered.loc[25, "ks_close"] = 99999.0
    df_tampered.loc[25, "ks_gap_pct"] = 15.0

    idx_file_tampered = str(tmp_path / "index_tampered.parquet")
    df_tampered.to_parquet(idx_file_tampered)

    p_tampered = DynamicFeatureProvider(daily_file=str(tmp_path / "nonexistent.parquet"), index_file=idx_file_tampered)
    p_tampered.load()

    feat_tampered = p_tampered.get_index_features(target_date)

    assert np.isclose(feat_base.ks_trend, feat_tampered.ks_trend, atol=1e-12)
    assert np.isclose(feat_base.market_gap_std20, feat_tampered.market_gap_std20, atol=1e-12)
    assert np.isclose(feat_base.dynamic_max_gap, feat_tampered.dynamic_max_gap, atol=1e-12)


def test_dynamic_max_gap_bounds():
    p = get_default_dynamic_feature_provider()
    # Check bounds on actual 2026 dates
    feat = p.get_index_features(date(2026, 1, 5))
    assert 0.50 <= feat.dynamic_max_gap <= 1.75


def test_slots_memory_optimization():
    s_feat = SymbolDynamicFeatures(prior_atr20_pct=0.05, prior_adto20=1e9)
    assert hasattr(s_feat, "__slots__")
    assert not hasattr(s_feat, "__dict__")

    i_feat = IndexDynamicFeatures(
        ks_trend=0.01, kq_trend=-0.02, market_gap_std20=0.85, is_bear_market=False, dynamic_max_gap=0.85
    )
    assert hasattr(i_feat, "__slots__")
    assert not hasattr(i_feat, "__dict__")


def test_date_normalization(tmp_path):
    assert DynamicFeatureProvider._to_date_str(date(2024, 10, 15)) == "2024-10-15"
    assert DynamicFeatureProvider._to_date_str(pd.Timestamp("2024-10-15 09:30:00")) == "2024-10-15"
    assert DynamicFeatureProvider._to_date_str("2024-10-15") == "2024-10-15"
    assert DynamicFeatureProvider._to_date_str("2024-10-15 09:00:00") == "2024-10-15"
    assert DynamicFeatureProvider._to_date_str("20241015") == "2024-10-15"


def test_market_regime_date_normalization():
    assert _normalize_date(date(2026, 11, 16)) == "2026-11-16"
    assert _normalize_date("20261116") == "2026-11-16"
    assert _normalize_date("2026-11-16") == "2026-11-16"
    assert _normalize_date("2026-11-16 09:00:00") == "2026-11-16"
    assert _normalize_date(pd.Timestamp("2026-11-16")) == "2026-11-16"

    mrp = MarketRegimeProvider()
    mrp.register_live_regime("20261116", ks_gap=-0.5, kq_gap=0.8)
    assert mrp.get_kospi_gap("2026-11-16") == -0.5
    assert mrp.get_kosdaq_gap(date(2026, 11, 16)) == 0.8
    assert mrp.get_max_gap("20261116") == 0.8
    assert mrp.get_min_gap("2026-11-16") == -0.5
    assert mrp.get_dual_regime(date(2026, 11, 16)) == (-0.5, 0.8)


def test_index_missing_columns_protection(tmp_path):
    """Index file with missing required columns logs warning and falls back safely."""
    df_broken = pd.DataFrame({
        "date": ["2024-01-02", "2024-01-03"],
        "ks_close": [2500.0, 2510.0],
    })
    idx_file = str(tmp_path / "broken_index.parquet")
    df_broken.to_parquet(idx_file)

    p = DynamicFeatureProvider(daily_file=str(tmp_path / "none.parquet"), index_file=idx_file)
    p.load()

    feat = p.get_index_features(date(2024, 1, 3))
    assert feat.market_gap_std20 == DEFAULT_MARKET_GAP_STD
    assert feat.dynamic_max_gap == DEFAULT_MARKET_GAP_STD


def test_inf_zero_division_defense(tmp_path):
    """Stock features handle division by zero / inf / negative gracefully without crash."""
    dates = pd.date_range("2024-01-01", periods=10, freq="B")
    data = []
    for d in dates:
        data.append({
            "date": d.date(),
            "symbol": "999999",
            "high": 1000.0,
            "low": 1000.0,
            "close": 0.0,  # Zero close leads to div by zero in prior_close
            "prev_close": 0.0,
            "turnover": 0.0,
            "adto20": -50.0,  # Negative adto
        })
    df = pd.DataFrame(data)
    f = str(tmp_path / "zero_div.parquet")
    df.to_parquet(f)

    p = DynamicFeatureProvider(daily_file=f, index_file=str(tmp_path / "none.parquet"))
    p.load()

    # Should safely fallback because corrupted values are excluded
    feat = p.get_stock_features(dates[5].date(), "999999")
    assert feat.prior_atr20_pct == p.default_atr20_pct
    assert feat.prior_adto20 == p.default_adto20


def test_register_live_regime_and_index_features():
    """Verify live real-time regime injection and retrieval."""
    mrp = MarketRegimeProvider()
    mrp.register_live_regime("2026-11-16", ks_gap=-0.45, kq_gap=0.32)
    assert mrp.get_kospi_gap(date(2026, 11, 16)) == -0.45
    assert mrp.get_kosdaq_gap("2026-11-16") == 0.32
    assert mrp.get_max_gap(date(2026, 11, 16)) == 0.32
    assert mrp.get_min_gap(date(2026, 11, 16)) == -0.45

    dfp = DynamicFeatureProvider()
    dfp.register_live_index_features(
        current_date="2026-11-16",
        ks_trend=-0.015,
        kq_trend=-0.022,
        is_bear_market=True,
        dynamic_max_gap=0.72,
        market_gap_std20=0.68,
    )
    feat = dfp.get_index_features(date(2026, 11, 16))
    assert feat.ks_trend == -0.015
    assert feat.kq_trend == -0.022
    assert feat.is_bear_market is True
    assert feat.dynamic_max_gap == 0.72
    assert feat.market_gap_std20 == 0.68


def test_build_strategy_dynamic_severe_gap_filter():
    """Verify build_strategy defaults severe_gap_down_filter to -2.0 when dynamic=True."""
    s_dyn = build_strategy(dynamic=True)
    assert s_dyn.severe_gap_down_filter == -2.0

    s_custom = build_strategy(dynamic=True, severe_gap_down_filter=-1.5)
    assert s_custom.severe_gap_down_filter == -1.5

    s_static = build_strategy(dynamic=False)
    assert s_static.severe_gap_down_filter is None


def test_rest_client_live_market_regime_calculation():
    """Verify KISRestClient.get_kospi_gap_and_market_regime computation & error defense."""
    from core.rest_client import KISRestClient
    client = KISRestClient()

    def mock_send(method, url, headers=None, params=None, json=None, timeout=10):
        today_str = datetime.now().strftime("%Y%m%d")
        out2 = [{
            "stck_bsop_date": today_str,
            "bstp_nmix_prpr": "2500.0",
            "bstp_nmix_oprc": "2480.0",
        }]
        base_price = 2500.0
        for i in range(1, 26):
            d = (datetime.now() - timedelta(days=i)).strftime("%Y%m%d")
            out2.append({
                "stck_bsop_date": d,
                "bstp_nmix_prpr": str(base_price + i * 2.0),
                "bstp_nmix_oprc": str(base_price + i * 2.0 - 5.0),
            })
        mock_resp = MagicMock()
        mock_resp.status_code = 200
        mock_resp.json.return_value = {
            "rt_cd": "0",
            "output1": {
                "bstp_nmix_prpr": "2500.0",
                "bstp_nmix_oprc": "2480.0",
                "prdy_nmix": "2502.0",
                "ascn_issu_cnt": "500",
                "down_issu_cnt": "300",
            },
            "output2": out2,
        }
        return mock_resp

    with patch.object(client, "_send_request", side_effect=mock_send):
        regime = client.get_kospi_gap_and_market_regime()
        assert "ks_gap_pct" in regime
        assert "kq_gap_pct" in regime
        assert "ks_trend" in regime
        assert "kq_trend" in regime
        assert "is_bear_market" in regime
        assert "dynamic_max_gap" in regime
        assert "gap_std" in regime
        assert "slots" in regime
        assert "regime" in regime
        assert 0.50 <= regime["dynamic_max_gap"] <= 1.75

    with patch.object(client, "_send_request", side_effect=RuntimeError("Network Timeout")):
        fallback = client.get_kospi_gap_and_market_regime()
        assert fallback["is_bear_market"] is True
        assert fallback["slots"] == 0
        assert fallback["max_slots"] == 0
        assert fallback["regime"] == "BEAR"


def test_daily_routine_regime_lifecycle_sync():
    """Verify DailyRoutineOrchestrator syncs live regime at 09:00:01 and executes on_day_open."""
    from engine.daily_routine import DailyRoutineOrchestrator
    orch = DailyRoutineOrchestrator(mode="mock", target_date="20261116")

    mock_regime = {
        "ks_gap_pct": -0.85,
        "kq_gap_pct": -0.92,
        "ks_trend": -0.035,
        "kq_trend": -0.041,
        "is_bear_market": True,
        "dynamic_max_gap": 0.65,
        "gap_std": 0.65,
        "slots": 0,
    }
    with patch("engine.daily_routine.KisLiveBroker") as mock_broker_cls, \
         patch("engine.daily_routine.TradingEngine") as mock_engine_cls:
        mock_broker = MagicMock()
        mock_broker.client.get_kospi_gap_and_market_regime.return_value = mock_regime
        mock_broker.get_positions.return_value = {}
        mock_broker.get_balance.return_value = MagicMock(cash=10_000_000.0)
        mock_broker_cls.return_value = mock_broker

        mock_engine = MagicMock()
        mock_engine.context.get_balance.return_value = MagicMock(cash=10_000_000.0)
        mock_engine_cls.return_value = mock_engine

        orch.run(wait_for_market_open=False)
        assert orch.strategy.max_daily_positions == 0


def test_rest_client_fail_safe_conservative_defense():
    """Verify Fail-Safe triggers BEAR (0 slots) when index data is abnormal/insufficient."""
    from core.rest_client import KISRestClient
    client = KISRestClient()

    # Case A: fewer than 5 past bars (only 2 bars)
    def mock_send_few_bars(method, url, headers=None, params=None, json=None, timeout=10):
        mock_resp = MagicMock()
        mock_resp.status_code = 200
        mock_resp.json.return_value = {
            "rt_cd": "0",
            "output1": {"bstp_nmix_prpr": "2500.0", "bstp_nmix_oprc": "2505.0"},
            "output2": [
                {"stck_bsop_date": datetime.now().strftime("%Y%m%d"), "bstp_nmix_prpr": "2500.0", "bstp_nmix_oprc": "2505.0"},
                {"stck_bsop_date": "20261115", "bstp_nmix_prpr": "2490.0", "bstp_nmix_oprc": "2480.0"},
            ],
        }
        return mock_resp

    with patch.object(client, "_send_request", side_effect=mock_send_few_bars):
        regime = client.get_kospi_gap_and_market_regime()
        assert regime["is_bear_market"] is True
        assert regime["regime"] == "BEAR"
        assert regime["max_slots"] == 0
        assert regime["slots"] == 0

    # Case B: ks_prev <= 0
    def mock_send_zero_prev(method, url, headers=None, params=None, json=None, timeout=10):
        mock_resp = MagicMock()
        mock_resp.status_code = 200
        mock_resp.json.return_value = {
            "rt_cd": "0",
            "output1": {"bstp_nmix_prpr": "0.0", "bstp_nmix_oprc": "0.0"},
            "output2": [],
        }
        return mock_resp

    with patch.object(client, "_send_request", side_effect=mock_send_zero_prev):
        regime = client.get_kospi_gap_and_market_regime()
        assert regime["is_bear_market"] is True
        assert regime["regime"] == "BEAR"
        assert regime["max_slots"] == 0
        assert regime["slots"] == 0


def test_rest_client_none_values_defense():
    """Verify None values in API response do not cause TypeError and parse safely."""
    from core.rest_client import KISRestClient
    client = KISRestClient()

    def mock_send_none_values(method, url, headers=None, params=None, json=None, timeout=10):
        mock_resp = MagicMock()
        mock_resp.status_code = 200
        today_str = datetime.now().strftime("%Y%m%d")
        out2 = [{
            "stck_bsop_date": today_str,
            "bstp_nmix_prpr": None,
            "bstp_nmix_oprc": None,
        }]
        for i in range(1, 10):
            out2.append({
                "stck_bsop_date": f"202611{10-i:02d}",
                "bstp_nmix_prpr": None,
                "bstp_nmix_oprc": None,
            })
        mock_resp.json.return_value = {
            "rt_cd": "0",
            "output1": {
                "bstp_nmix_prpr": None,
                "bstp_nmix_oprc": None,
                "prdy_nmix": None,
                "ascn_issu_cnt": None,
                "down_issu_cnt": None,
            },
            "output2": out2,
        }
        return mock_resp

    with patch.object(client, "_send_request", side_effect=mock_send_none_values):
        regime = client.get_kospi_gap_and_market_regime()
        assert regime["is_bear_market"] is True
        assert regime["regime"] == "BEAR"
        assert regime["max_slots"] == 0
        assert regime["slots"] == 0


def test_rest_client_open_price_retry():
    """Verify open price retry up to 2 times when initial open price is 0.0."""
    from core.rest_client import KISRestClient
    client = KISRestClient()

    call_count = {"0001": 0, "1001": 0}

    def mock_send_retry(method, url, headers=None, params=None, json=None, timeout=10):
        iscd = params.get("FID_INPUT_ISCD", "0001")
        call_count[iscd] += 1
        today_str = datetime.now().strftime("%Y%m%d")

        # On first call, open is 0.0; on second call (attempt 1), open is 2500.0
        oprc = "0.0" if call_count[iscd] == 1 else "2500.0"

        out2 = [{
            "stck_bsop_date": today_str,
            "bstp_nmix_prpr": "2500.0",
            "bstp_nmix_oprc": oprc,
        }]
        for i in range(1, 25):
            out2.append({
                "stck_bsop_date": f"202610{i:02d}",
                "bstp_nmix_prpr": "2490.0",
                "bstp_nmix_oprc": "2485.0",
            })
        mock_resp = MagicMock()
        mock_resp.status_code = 200
        mock_resp.json.return_value = {
            "rt_cd": "0",
            "output1": {
                "bstp_nmix_prpr": "2500.0",
                "bstp_nmix_oprc": oprc,
                "prdy_nmix": "2490.0",
            },
            "output2": out2,
        }
        return mock_resp

    with patch("time.sleep", return_value=None):  # Avoid actual delay in test
        with patch.object(client, "_send_request", side_effect=mock_send_retry):
            regime = client.get_kospi_gap_and_market_regime()
            assert call_count["0001"] == 2
            assert regime["ks_open"] == 2500.0

