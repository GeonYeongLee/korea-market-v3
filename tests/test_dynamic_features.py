"""
Unit tests for DynamicFeatureProvider and Lookahead 0% Causal Audit.
Verifies that all dynamic features for day t strictly originate from day t-1 or earlier.
"""
import pytest
from datetime import date
import pandas as pd
import numpy as np

from core.dynamic_features import (
    DEFAULT_MARKET_GAP_STD,
    DynamicFeatureProvider,
    IndexDynamicFeatures,
    SymbolDynamicFeatures,
    get_default_dynamic_feature_provider,
)


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

