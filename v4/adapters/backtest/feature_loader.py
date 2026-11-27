"""
Parquet Daily Feature, ATR20, Index Gap & Prev-Close Loader for Korea Market v4.
Provides causal t-1 feature computation and predicate-pushdown parquet reading.
"""
from __future__ import annotations

import logging
import os
from datetime import date, timedelta
from typing import Dict, Optional

import numpy as np
import pandas as pd
import pyarrow.dataset as ds
import pyarrow.types as patypes

from core.dynamic_features import DynamicFeatureProvider, SymbolDynamicFeatures
from core.market_regime import MarketRegimeProvider

logger = logging.getLogger("korea_market.v4.adapters.backtest.feature_loader")


def choose_cutoff(daily_file: str, start_date: Optional[date], target_date: Optional[date]) -> Optional[date]:
    """Selects causal lookback cutoff date for daily parquet predicate pushdown."""
    if start_date is not None:
        ref = min(start_date, target_date) if target_date else start_date
        return ref - timedelta(days=120)
    if os.path.exists(daily_file) and os.path.getsize(daily_file) <= 20_000_000:
        return None
    base = target_date if target_date is not None else date(2024, 10, 1)
    return min(base, date(2024, 10, 1)) - timedelta(days=120)


def read_filtered_daily(
    daily_file: str, cols: list[str], cutoff: Optional[date], end_date: Optional[date]
) -> pd.DataFrame:
    """Reads daily_bars parquet with optional `[cutoff, end_date]` pyarrow predicate pushdown."""
    if not os.path.exists(daily_file):
        return pd.DataFrame()
    if cutoff is None and end_date is None:
        return pd.read_parquet(daily_file, columns=cols)
    try:
        dataset = ds.dataset(daily_file, format="parquet")
        avail = [c for c in cols if c in dataset.schema.names]
        date_type = dataset.schema.field("date").type
        filt = None
        for bound, op in ((cutoff, ">="), (end_date, "<=")):
            if bound is None:
                continue
            if patypes.is_date(date_type):
                val = bound
            elif patypes.is_timestamp(date_type):
                val = pd.Timestamp(bound)
            else:
                val = bound.isoformat()
            expr = (ds.field("date") >= val) if op == ">=" else (ds.field("date") <= val)
            filt = expr if filt is None else (filt & expr)
        return dataset.to_table(columns=avail, filter=filt).to_pandas()
    except Exception as exc:
        logger.debug(f"Predicate pushdown fallback for {daily_file}: {exc}")
        return pd.read_parquet(daily_file, columns=cols)


def load_index_gaps(
    regime_provider: MarketRegimeProvider, index_file: str, is_external: bool
) -> None:
    """Populates KOSPI/KOSDAQ opening gap caches into `regime_provider`."""
    if is_external:
        regime_provider._load_cache()
        return
    if os.path.exists(index_file):
        df_idx = pd.read_parquet(index_file, columns=["date", "ks_gap_pct", "kq_gap_pct"])
        date_strs = pd.to_datetime(df_idx["date"]).dt.strftime("%Y-%m-%d")
        for d_str, ks, kq in zip(date_strs, df_idx["ks_gap_pct"], df_idx["kq_gap_pct"]):
            if pd.notna(ks):
                regime_provider._kospi_gap_cache[d_str] = float(ks)
            if pd.notna(kq):
                regime_provider._kosdaq_gap_cache[d_str] = float(kq)
    regime_provider._is_loaded = True


def populate_prev_closes(df: pd.DataFrame, prev_closes_by_date: Dict[str, Dict[str, float]]) -> None:
    """Populates `prev_closes_by_date[YYYY-MM-DD][symbol]` from a daily DataFrame."""
    prev_closes_by_date.clear()
    if df.empty or "prev_close" not in df.columns:
        return
    df = df.dropna(subset=["prev_close"])
    df = df[df["prev_close"] > 0.0]
    if df.empty:
        return
    date_strs = pd.to_datetime(df["date"]).dt.strftime("%Y-%m-%d").tolist()
    syms = df["symbol"].astype(str).tolist()
    pcs = df["prev_close"].astype(float).tolist()
    for d_str, sym, pc in zip(date_strs, syms, pcs):
        prev_closes_by_date.setdefault(d_str, {})[sym] = pc


def populate_features_and_prev_closes(
    df: pd.DataFrame,
    feature_provider: DynamicFeatureProvider,
    prev_closes_by_date: Dict[str, Dict[str, float]],
) -> None:
    """Computes strictly causal t-1 ATR20% / ADTO20 features and prev_closes from daily bars."""
    prev_closes_by_date.clear()
    feature_provider._stock_features_by_date.clear()
    if df.empty:
        feature_provider._sorted_stock_dates = []
        feature_provider._is_loaded = True
        if hasattr(feature_provider, "_latest_settled_stock_date"):
            feature_provider._latest_settled_stock_date = None
        if hasattr(feature_provider, "_latest_settled_features"):
            feature_provider._latest_settled_features = {}
        return

    df["date_str"] = pd.to_datetime(df["date"]).dt.strftime("%Y-%m-%d")
    populate_prev_closes(df[["date_str", "symbol", "prev_close"]].rename(columns={"date_str": "date"}), prev_closes_by_date)

    df = df.sort_values(["symbol", "date_str"]).reset_index(drop=True)
    pc = df["prev_close"].fillna(df["close"])
    df["tr"] = np.maximum(
        df["high"] - df["low"],
        np.maximum(np.abs(df["high"] - pc), np.abs(df["low"] - pc)),
    )
    grp = df.groupby("symbol", sort=False)
    df["atr20"] = grp["tr"].rolling(20, min_periods=5).mean().reset_index(level=0, drop=True)
    df["prior_atr20_pct"] = (grp["atr20"].shift(1) / grp["close"].shift(1)).replace([np.inf, -np.inf], np.nan)
    df["prior_adto20"] = grp["adto20"].shift(1)
    clean = df.dropna(subset=["prior_atr20_pct", "prior_adto20"])

    by_date = feature_provider._stock_features_by_date
    for d_str, sym, a, ad in zip(
        clean["date_str"].tolist(),
        clean["symbol"].astype(str).tolist(),
        clean["prior_atr20_pct"].tolist(),
        clean["prior_adto20"].tolist(),
    ):
        if np.isfinite(a) and a > 0.0 and np.isfinite(ad) and ad > 0.0:
            by_date.setdefault(d_str, {})[sym] = SymbolDynamicFeatures(
                prior_atr20_pct=float(a), prior_adto20=float(ad)
            )

    feature_provider._sorted_stock_dates = sorted(by_date.keys())
    feature_provider._is_loaded = True
    if hasattr(feature_provider, "_latest_settled_features") and hasattr(feature_provider, "_latest_settled_stock_date"):
        eod = df.assign(eod_atr=(df["atr20"] / df["close"]).replace([np.inf, -np.inf], np.nan)).dropna(
            subset=["eod_atr", "adto20", "close"]
        )
        settled: Dict[str, SymbolDynamicFeatures] = {}
        if not eod.empty:
            last_sym = eod.groupby("symbol", sort=False).last()
            for s, row in last_sym.iterrows():
                if np.isfinite(row["eod_atr"]) and row["eod_atr"] > 0.0 and np.isfinite(row["adto20"]) and row["adto20"] > 0.0:
                    settled[str(s)] = SymbolDynamicFeatures(
                        prior_atr20_pct=float(row["eod_atr"]), prior_adto20=float(row["adto20"])
                    )
            feature_provider._latest_settled_stock_date = str(last_sym["date_str"].max())
        else:
            feature_provider._latest_settled_stock_date = None
        feature_provider._latest_settled_features = settled
