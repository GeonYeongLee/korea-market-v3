"""
Dynamic Feature Provider for Korea Market v3.
Provides volatility (ATR20), liquidity (ADTO20), and market regime features.
Strict Causal Invariant (Lookahead 0%): All indicators for day t strictly
originate from day t-1 or earlier.
"""
from __future__ import annotations

import logging
import os
from dataclasses import dataclass
from datetime import date, datetime, timedelta
from typing import Dict, Optional

import numpy as np
import pandas as pd

logger = logging.getLogger("korea_market.core.dynamic_features")

DEFAULT_MARKET_GAP_STD: float = 0.85


@dataclass(frozen=True, slots=True)
class SymbolDynamicFeatures:
    prior_atr20_pct: float
    prior_adto20: float


@dataclass(frozen=True, slots=True)
class IndexDynamicFeatures:
    ks_trend: float
    kq_trend: float
    market_gap_std20: float = DEFAULT_MARKET_GAP_STD
    is_bear_market: bool = False
    dynamic_max_gap: float = DEFAULT_MARKET_GAP_STD


class DynamicFeatureProvider:
    """
    Caches and serves stock-level (ATR20, ADTO20) and market-level
    dynamic features with guaranteed 0% lookahead.
    """

    def __init__(
        self,
        daily_file: str = "data/dataset_2y/daily_bars_2y.parquet",
        index_file: str = "data/dataset_2y/index_daily.parquet",
        default_atr20_pct: float = 0.080,
        default_adto20: float = 10_000_000_000.0,
    ):
        self.daily_file = daily_file
        self.index_file = index_file
        self.default_atr20_pct = default_atr20_pct
        self.default_adto20 = default_adto20

        self._stock_features_by_date: Dict[str, Dict[str, SymbolDynamicFeatures]] = {}
        self._index_features_by_date: Dict[str, IndexDynamicFeatures] = {}
        self._sorted_stock_dates: list[str] = []
        self._latest_settled_stock_date: Optional[str] = None
        self._latest_settled_features: Dict[str, SymbolDynamicFeatures] = {}
        self._latest_index_date: Optional[str] = None
        self._is_loaded: bool = False
        self._loaded_live_only: bool = False

    @staticmethod
    def _to_date_str(d: object) -> str:
        if hasattr(d, "strftime"):
            return d.strftime("%Y-%m-%d")
        s = str(d).strip()
        if len(s) == 8 and s.isdigit():
            return f"{s[:4]}-{s[4:6]}-{s[6:8]}"
        return s[:10]

    def load(self, live_only: bool = False, lookback_days: int = 60) -> None:
        """Precomputes dynamic features with strict shift(1) lookahead guard.

        When live_only=True, only reads a trailing lookback window and computes
        _latest_settled_features without iterating thousands of historical dates.
        """
        if self._is_loaded:
            if live_only or not self._loaded_live_only:
                return
            # Upgrade from live_only cache to full historical cache if requested
            self._stock_features_by_date.clear()
            self._index_features_by_date.clear()
            self._sorted_stock_dates.clear()

        self._load_stock_features(live_only=live_only, lookback_days=lookback_days)
        if not live_only:
            self._load_index_features()
        self._loaded_live_only = live_only
        self._is_loaded = True

    def _read_recent_daily_parquet(self, cols: list[str], lookback_days: int) -> pd.DataFrame:
        """Reads only the trailing `lookback_days` window via pyarrow predicate pushdown."""
        try:
            import pyarrow.dataset as ds
            import pyarrow.types as patypes

            dataset = ds.dataset(self.daily_file, format="parquet")
            date_type = dataset.schema.field("date").type
            cutoff_date = (datetime.now() - timedelta(days=lookback_days)).date()

            if patypes.is_date(date_type):
                filt = ds.field("date") >= cutoff_date
            elif patypes.is_timestamp(date_type):
                filt = ds.field("date") >= pd.Timestamp(cutoff_date)
            else:
                filt = ds.field("date") >= cutoff_date.isoformat()

            df = dataset.to_table(columns=cols, filter=filt).to_pandas()
            if not df.empty:
                return df
        except Exception as e:
            logger.debug(f"Predicate pushdown fallback to full read for {self.daily_file}: {e}")
        return pd.read_parquet(self.daily_file, columns=cols)

    def _load_stock_features(self, live_only: bool = False, lookback_days: int = 60) -> None:
        if not os.path.exists(self.daily_file):
            logger.warning(f"Daily file not found: {self.daily_file}")
            return

        try:
            cols = ["date", "symbol", "high", "low", "close", "prev_close", "turnover", "adto20"]
            if live_only:
                df = self._read_recent_daily_parquet(cols=cols, lookback_days=lookback_days)
            else:
                df = pd.read_parquet(self.daily_file, columns=cols)
            df["date_str"] = df["date"].map(self._to_date_str)
            df = df.sort_values(["symbol", "date_str"]).reset_index(drop=True)

            # True Range: max(H-L, |H-PC|, |L-PC|)
            pc = df["prev_close"].fillna(df["close"])
            tr = np.maximum(df["high"] - df["low"], np.maximum(np.abs(df["high"] - pc), np.abs(df["low"] - pc)))
            df["tr"] = tr

            # Groupby rolling 20 days
            g = df.groupby("symbol", sort=False)
            df["atr20"] = g["tr"].rolling(20, min_periods=5).mean().reset_index(level=0, drop=True)

            if live_only:
                # Populate at most the latest 2 trading dates for same-day T parity, skipping 50-year history iteration
                prior_atr = g["atr20"].shift(1)
                prior_close = g["close"].shift(1)
                df["prior_atr20_pct"] = (prior_atr / prior_close).replace([np.inf, -np.inf], np.nan)
                df["prior_adto20"] = g["adto20"].shift(1)
                recent_dates = sorted(df["date_str"].unique())[-2:]
                clean = df[df["date_str"].isin(recent_dates)].dropna(subset=["prior_atr20_pct", "prior_adto20"])
            else:
                # Strict Lookahead 0%: shift(1) relative to day t
                prior_atr = g["atr20"].shift(1)
                prior_close = g["close"].shift(1)
                df["prior_atr20_pct"] = (prior_atr / prior_close).replace([np.inf, -np.inf], np.nan)
                df["prior_adto20"] = g["adto20"].shift(1)
                clean = df.dropna(subset=["prior_atr20_pct", "prior_adto20"])

            for d_str, sub in clean.groupby("date_str", sort=False):
                syms = sub["symbol"].values
                atrs = sub["prior_atr20_pct"].values
                adtos = sub["prior_adto20"].values
                day_dict: Dict[str, SymbolDynamicFeatures] = {}
                for s, a, ad in zip(syms, atrs, adtos):
                    if np.isfinite(a) and a > 0.0 and np.isfinite(ad) and ad > 0.0:
                        day_dict[s] = SymbolDynamicFeatures(prior_atr20_pct=float(a), prior_adto20=float(ad))
                self._stock_features_by_date[d_str] = day_dict

            self._sorted_stock_dates = sorted(self._stock_features_by_date.keys())

            # Point-in-Time EOD Settled snapshot per symbol:
            # The unshifted settled metrics as of the latest completed bar in dataset.
            # When trading on day T > latest_settled_stock_date (e.g. today in live trading),
            # the prior metrics for day T are mathematically the unshifted settled EOD metrics of the latest completed bar!
            df["eod_atr20_pct"] = (df["atr20"] / df["close"]).replace([np.inf, -np.inf], np.nan)
            valid_last = df.dropna(subset=["eod_atr20_pct", "adto20", "close"])
            if not valid_last.empty:
                last_per_sym = valid_last.groupby("symbol", sort=False).last()
                self._latest_settled_features = {}
                for s, row in last_per_sym.iterrows():
                    a = row["eod_atr20_pct"]
                    ad = row["adto20"]
                    if np.isfinite(a) and a > 0.0 and np.isfinite(ad) and ad > 0.0:
                        self._latest_settled_features[str(s)] = SymbolDynamicFeatures(
                            prior_atr20_pct=float(a), prior_adto20=float(ad)
                        )
                self._latest_settled_stock_date = str(last_per_sym["date_str"].max())
            else:
                self._latest_settled_features = {}
                self._latest_settled_stock_date = None

            logger.info(
                f"Loaded stock dynamic features ({'live_only' if live_only else 'full'}) for "
                f"{len(self._stock_features_by_date)} trading dates. "
                f"Latest settled EOD date: {self._latest_settled_stock_date} ({len(self._latest_settled_features)} symbols)."
            )
        except Exception as e:
            logger.warning(f"Failed to load stock features from {self.daily_file}: {e}")

    def _load_index_features(self) -> None:
        if not os.path.exists(self.index_file):
            logger.warning(f"Index file not found: {self.index_file}")
            return

        try:
            df_idx = pd.read_parquet(self.index_file)
            raw_cols = {"date", "ks_close", "ks_ma20", "kq_close", "kq_ma20", "ks_gap_pct"}
            precomputed_cols = {"date", "ks_trend", "kq_trend", "market_gap_std20", "is_bear_market"}
            has_raw = raw_cols.issubset(df_idx.columns)
            has_precomputed = precomputed_cols.issubset(df_idx.columns)
            if not has_raw and not has_precomputed:
                logger.warning(
                    f"Index file {self.index_file} missing required columns: {raw_cols - set(df_idx.columns)}"
                )
                return

            df_idx["date_str"] = df_idx["date"].map(self._to_date_str)
            df_idx = df_idx.sort_values("date_str").reset_index(drop=True)
            if df_idx.empty:
                return

            if has_raw:
                # Trend as of day t-1
                ks_trend = ((df_idx["ks_close"] - df_idx["ks_ma20"]) / df_idx["ks_ma20"]).shift(1)
                kq_trend = ((df_idx["kq_close"] - df_idx["kq_ma20"]) / df_idx["kq_ma20"]).shift(1)

                # Gap standard deviation (in % points) over 20 rolling days as of day t-1
                gap_std = (
                    df_idx["ks_gap_pct"].shift(1).rolling(20, min_periods=5).std().fillna(DEFAULT_MARKET_GAP_STD)
                )
                bear_series = None
            else:
                ks_trend = df_idx["ks_trend"]
                kq_trend = df_idx["kq_trend"]
                gap_std = df_idx["market_gap_std20"]
                bear_series = df_idx["is_bear_market"]

            for idx_pos, d_str in enumerate(df_idx["date_str"].values):
                kt = ks_trend.iloc[idx_pos]
                qt = kq_trend.iloc[idx_pos]
                gs = gap_std.iloc[idx_pos]

                eff_kt = float(kt) if pd.notna(kt) else 0.0
                eff_qt = float(qt) if pd.notna(qt) else 0.0
                eff_gs = float(gs) if pd.notna(gs) and gs > 0 else DEFAULT_MARKET_GAP_STD

                if bear_series is not None and pd.notna(bear_series.iloc[idx_pos]):
                    is_bear = bool(bear_series.iloc[idx_pos])
                else:
                    # Bear Market Defense: Both KOSPI and KOSDAQ below MA20 on t-1
                    is_bear = (eff_kt < 0.0) and (eff_qt < 0.0)

                # Dynamic Max Gap: clip(1.0 * std, 0.50%, 1.75%)
                dyn_gap = float(np.clip(1.0 * eff_gs, 0.50, 1.75))

                self._index_features_by_date[d_str] = IndexDynamicFeatures(
                    ks_trend=eff_kt,
                    kq_trend=eff_qt,
                    market_gap_std20=eff_gs,
                    is_bear_market=is_bear,
                    dynamic_max_gap=dyn_gap,
                )

            self._latest_index_date = (
                max(self._index_features_by_date.keys()) if self._index_features_by_date else None
            )
            logger.info(f"Loaded index dynamic features for {len(self._index_features_by_date)} trading dates.")
        except Exception as e:
            logger.warning(f"Failed to load index features from {self.index_file}: {e}")

    def get_stock_features(self, current_date: date | str, symbol: str) -> SymbolDynamicFeatures:
        """
        Returns SymbolDynamicFeatures for symbol on current_date, with Point-in-Time (As-Of) resolution.
        If current_date is today (live trading) and not yet in the historical dataset,
        it automatically resolves to the latest settled EOD values (T-1) without requiring today's row!
        """
        if not self._is_loaded:
            self.load()
        d_str = self._to_date_str(current_date)
        day_dict = self._stock_features_by_date.get(d_str)
        if day_dict and symbol in day_dict:
            return day_dict[symbol]

        # Point-in-Time Forward Lookup: If querying day T > latest completed date in file
        if self._latest_settled_stock_date and d_str > self._latest_settled_stock_date:
            if symbol in self._latest_settled_features:
                return self._latest_settled_features[symbol]

        # Point-in-Time Historical Lookup: If querying a weekend/holiday or date between historical dates
        if self._sorted_stock_dates:
            import bisect
            idx = bisect.bisect_left(self._sorted_stock_dates, d_str)
            if idx > 0:
                prior_d = self._sorted_stock_dates[idx - 1]
                if prior_d in self._stock_features_by_date and symbol in self._stock_features_by_date[prior_d]:
                    return self._stock_features_by_date[prior_d][symbol]

        return SymbolDynamicFeatures(prior_atr20_pct=self.default_atr20_pct, prior_adto20=self.default_adto20)

    def get_day_stock_features(self, current_date: date | str) -> Dict[str, SymbolDynamicFeatures]:
        """Returns full dictionary of symbol dynamic features for current_date with Point-in-Time support."""
        if not self._is_loaded:
            self.load()
        d_str = self._to_date_str(current_date)
        if d_str in self._stock_features_by_date:
            return self._stock_features_by_date[d_str]

        # Point-in-Time Forward Lookup for live trading day
        if self._latest_settled_stock_date and d_str > self._latest_settled_stock_date:
            return self._latest_settled_features

        if self._sorted_stock_dates:
            import bisect
            idx = bisect.bisect_left(self._sorted_stock_dates, d_str)
            if idx > 0:
                prior_d = self._sorted_stock_dates[idx - 1]
                return self._stock_features_by_date.get(prior_d, {})

        return {}

    def get_index_features(self, current_date: date | str) -> IndexDynamicFeatures:
        """Returns IndexDynamicFeatures for current_date, with safe fallback."""
        if not self._is_loaded:
            self.load()
        d_str = self._to_date_str(current_date)
        if d_str in self._index_features_by_date:
            return self._index_features_by_date[d_str]

        # Fallback for dates beyond index_daily range
        if self._latest_index_date and d_str > self._latest_index_date:
            return self._index_features_by_date[self._latest_index_date]

        return IndexDynamicFeatures(
            ks_trend=0.0,
            kq_trend=0.0,
            market_gap_std20=DEFAULT_MARKET_GAP_STD,
            is_bear_market=False,
            dynamic_max_gap=DEFAULT_MARKET_GAP_STD,
        )

    def register_live_index_features(
        self,
        current_date: date | str,
        ks_trend: float,
        kq_trend: float,
        is_bear_market: bool,
        dynamic_max_gap: float,
        market_gap_std20: float = DEFAULT_MARKET_GAP_STD,
    ) -> None:
        """Registers real-time index dynamic features computed at market open."""
        if not self._is_loaded:
            self.load()
        d_str = self._to_date_str(current_date)
        self._index_features_by_date[d_str] = IndexDynamicFeatures(
            ks_trend=float(ks_trend),
            kq_trend=float(kq_trend),
            market_gap_std20=float(market_gap_std20),
            is_bear_market=bool(is_bear_market),
            dynamic_max_gap=float(dynamic_max_gap),
        )
        if self._latest_index_date is None or d_str > self._latest_index_date:
            self._latest_index_date = d_str
        logger.info(
            f"Registered live index features for {d_str}: KS trend {ks_trend:+.2%}, KQ trend {kq_trend:+.2%}, "
            f"bear_market={is_bear_market}, dynamic_max_gap={dynamic_max_gap:.2f}%"
        )


_default_provider: Optional[DynamicFeatureProvider] = None


def get_default_dynamic_feature_provider() -> DynamicFeatureProvider:
    global _default_provider
    if _default_provider is None:
        _default_provider = DynamicFeatureProvider()
    return _default_provider
