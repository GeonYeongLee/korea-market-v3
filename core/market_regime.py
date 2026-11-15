"""
Market Regime and Index Indicators Provider.
Provides pre-market & 09:00:00 KOSPI and KOSDAQ index gap and market breadth metrics.
"""
from __future__ import annotations

import os
import json
import logging
from datetime import date
from typing import Optional, Dict, Tuple
import pandas as pd

logger = logging.getLogger("korea_market.core.market_regime")


class MarketRegimeProvider:
    """
    Caches and serves KOSPI and KOSDAQ gap % and market breadth indicators.
    Lookahead 0%: Only pre-market and 09:00:00 open values are used.
    """

    def __init__(
        self,
        index_file: str = "data/dataset_2y/index_daily.parquet",
        daily_file: str = "data/dataset_2y/daily_bars_2y.parquet",
        market_map_file: str = "data/metadata/market_map.json",
    ):
        self.index_file = index_file
        self.daily_file = daily_file
        self.market_map_file = market_map_file
        self._kospi_gap_cache: Dict[str, float] = {}
        self._kosdaq_gap_cache: Dict[str, float] = {}
        self._is_loaded: bool = False

    def _load_cache(self) -> None:
        if self._is_loaded:
            return

        if os.path.exists(self.index_file):
            try:
                cols = ["date", "ks_gap_pct", "kq_gap_pct"]
                df_idx = pd.read_parquet(self.index_file, columns=cols)
                for _, r in df_idx.iterrows():
                    d_str = str(pd.Timestamp(r["date"]).date())
                    if pd.notna(r.get("ks_gap_pct")):
                        self._kospi_gap_cache[d_str] = float(r["ks_gap_pct"])
                    if pd.notna(r.get("kq_gap_pct")):
                        self._kosdaq_gap_cache[d_str] = float(r["kq_gap_pct"])
            except Exception as e:
                logger.warning(f"Failed to load {self.index_file}: {e}")

        # Compute fallback for any dates missing in index_daily from daily_bars_2y
        if os.path.exists(self.daily_file):
            try:
                market_map = {}
                if os.path.exists(self.market_map_file):
                    with open(self.market_map_file, "r", encoding="utf-8") as f:
                        market_map = json.load(f)

                df_daily = pd.read_parquet(self.daily_file, columns=["date", "symbol", "open", "prev_close", "adto20"])
                df_daily["d_str"] = df_daily["date"].astype(str)
                all_dates = set(df_daily["d_str"].unique())
                missing_dates = all_dates - (set(self._kospi_gap_cache.keys()) & set(self._kosdaq_gap_cache.keys()))
                if missing_dates:
                    df_sub = df_daily[df_daily["d_str"].isin(missing_dates)].copy()
                    df_sub["gap_pct"] = (df_sub["open"] - df_sub["prev_close"]) / df_sub["prev_close"] * 100.0
                    df_sub["market"] = df_sub["symbol"].map(lambda s: market_map.get(s, {}).get("market", "UNKNOWN"))

                    for d_str, g in df_sub.groupby("d_str"):
                        ks_g = g[g["market"] == "KOSPI"]
                        kq_g = g[g["market"] == "KOSDAQ"]

                        if d_str not in self._kospi_gap_cache:
                            if not ks_g.empty and ks_g["adto20"].sum() > 0:
                                self._kospi_gap_cache[d_str] = float((ks_g["gap_pct"] * ks_g["adto20"]).sum() / ks_g["adto20"].sum())
                            elif not ks_g.empty:
                                self._kospi_gap_cache[d_str] = float(ks_g["gap_pct"].mean())
                            else:
                                self._kospi_gap_cache[d_str] = float(g["gap_pct"].mean())

                        if d_str not in self._kosdaq_gap_cache:
                            if not kq_g.empty and kq_g["adto20"].sum() > 0:
                                self._kosdaq_gap_cache[d_str] = float((kq_g["gap_pct"] * kq_g["adto20"]).sum() / kq_g["adto20"].sum())
                            elif not kq_g.empty:
                                self._kosdaq_gap_cache[d_str] = float(kq_g["gap_pct"].mean())
                            else:
                                self._kosdaq_gap_cache[d_str] = float(g["gap_pct"].mean())
            except Exception as e:
                logger.warning(f"Failed to compute fallback regime from {self.daily_file}: {e}")

        self._is_loaded = True

    def get_kospi_gap(self, current_date: date) -> Optional[float]:
        self._load_cache()
        d_str = str(current_date)
        return self._kospi_gap_cache.get(d_str)

    def get_kosdaq_gap(self, current_date: date) -> Optional[float]:
        self._load_cache()
        d_str = str(current_date)
        return self._kosdaq_gap_cache.get(d_str)

    def get_max_gap(self, current_date: date) -> Optional[float]:
        ks = self.get_kospi_gap(current_date)
        kq = self.get_kosdaq_gap(current_date)
        if ks is not None and kq is not None:
            return max(ks, kq)
        return ks if ks is not None else kq

    def get_min_gap(self, current_date: date) -> Optional[float]:
        ks = self.get_kospi_gap(current_date)
        kq = self.get_kosdaq_gap(current_date)
        if ks is not None and kq is not None:
            return min(ks, kq)
        return ks if ks is not None else kq

    def get_dual_regime(self, current_date: date) -> Tuple[Optional[float], Optional[float]]:
        return self.get_kospi_gap(current_date), self.get_kosdaq_gap(current_date)


_default_provider: Optional[MarketRegimeProvider] = None


def get_default_market_regime_provider() -> MarketRegimeProvider:
    global _default_provider
    if _default_provider is None:
        _default_provider = MarketRegimeProvider()
    return _default_provider
