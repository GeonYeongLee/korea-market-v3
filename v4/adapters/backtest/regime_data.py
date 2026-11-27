"""
Parquet Regime, Dynamic Features & Session Schedule Adapter for Korea Market v4.
Implements `IRegimeDataPort` by reusing `DynamicFeatureProvider`,
`MarketRegimeProvider`, `daily_bars_2y.parquet` `prev_close`, optional
`nxt_symbols.json`, and `10:00` late-open day detection.
"""
from __future__ import annotations

import os
from datetime import date, time, timedelta
from typing import Dict, Optional, Set, Tuple

from core.dynamic_features import DynamicFeatureProvider
from core.krx_symbols import load_nxt_symbol_set as load_nxt_symbols
from core.market_regime import MarketRegimeProvider
from v4.adapters.backtest.feature_loader import (
    choose_cutoff,
    load_index_gaps,
    populate_features_and_prev_closes,
    populate_prev_closes,
    read_filtered_daily,
)
from v4.adapters.backtest.parquet_loader import CurrentDayStore, detect_session_times, parse_date_arg, resolve_data_dir
from v4.interfaces import IRegimeDataPort
from v4.models import MarketIndexRegime, SymbolFeatures


class ParquetRegimeDataAdapter(IRegimeDataPort):
    """Point-in-time historical regime, t-1 features, prev_close, and session open/close port."""

    def __init__(
        self,
        data_dir: Optional[str] = None,
        daily_file: str = "data/dataset_2y/daily_bars_2y.parquet",
        index_file: str = "data/dataset_2y/index_daily.parquet",
        market_map_file: str = "data/metadata/market_map.json",
        nxt_file: Optional[str] = "data/metadata/nxt_symbols.json",
        start_date: Optional[date | str] = None,
        end_date: Optional[date | str] = None,
        feature_provider: Optional[DynamicFeatureProvider] = None,
        regime_provider: Optional[MarketRegimeProvider] = None,
        day_store: Optional[CurrentDayStore] = None,
    ) -> None:
        self.data_dir = resolve_data_dir(data_dir)
        self.daily_file = daily_file
        self.index_file = index_file
        self.market_map_file = market_map_file
        self.start_date = parse_date_arg(start_date)
        self.end_date = parse_date_arg(end_date)
        self.day_store = day_store

        self._external_feature_provider = feature_provider is not None
        self.feature_provider = feature_provider or DynamicFeatureProvider(
            daily_file=daily_file, index_file=index_file
        )
        self._external_regime_provider = regime_provider is not None
        self.regime_provider = regime_provider or MarketRegimeProvider(
            index_file=index_file, daily_file=daily_file, market_map_file=market_map_file
        )
        self.nxt_symbols: Optional[Set[str]] = load_nxt_symbols(nxt_file)

        self._prev_closes_by_date: Dict[str, Dict[str, float]] = {}
        self._session_bounds: Dict[date, Tuple[time, time]] = {}
        self._loaded_cutoff: Optional[date] = None
        self._features_loaded: bool = False

    def _ensure_loaded(self, target_date: Optional[date] = None) -> None:
        need_reload = not self._features_loaded
        if (
            not need_reload
            and not self._external_feature_provider
            and target_date is not None
            and self._loaded_cutoff is not None
            and target_date < self._loaded_cutoff + timedelta(days=30)
        ):
            need_reload = True
        if not need_reload:
            return

        load_index_gaps(self.regime_provider, self.index_file, self._external_regime_provider)
        if not self.feature_provider._index_features_by_date:
            self.feature_provider._load_index_features()

        if self._external_feature_provider:
            if not self.feature_provider._is_loaded:
                self.feature_provider.load()
            cutoff = (self.start_date - timedelta(days=5)) if self.start_date else None
            df_pc = read_filtered_daily(self.daily_file, ["date", "symbol", "prev_close"], cutoff, self.end_date)
            populate_prev_closes(df_pc, self._prev_closes_by_date)
            self._loaded_cutoff = cutoff
            self._features_loaded = True
            return

        cutoff = choose_cutoff(self.daily_file, self.start_date, target_date)
        cols = ["date", "symbol", "high", "low", "close", "prev_close", "turnover", "adto20"]
        df = read_filtered_daily(self.daily_file, cols, cutoff, self.end_date)
        populate_features_and_prev_closes(df, self.feature_provider, self._prev_closes_by_date)
        self._loaded_cutoff = cutoff
        self._features_loaded = True

    def _get_session_bounds(self, d: date) -> Tuple[time, time]:
        cached = self._session_bounds.get(d)
        if cached is not None:
            return cached
        if self.day_store is not None:
            day_idx = self.day_store.get(d)
            if day_idx is not None:
                bounds = (day_idx.market_open_time, day_idx.market_close_time)
                self._session_bounds[d] = bounds
                return bounds
        day_dir = os.path.join(self.data_dir, d.strftime("%Y%m%d"))
        if os.path.isdir(day_dir):
            if self.day_store is not None:
                day_idx = self.day_store.get_or_load(day_dir, d)
                bounds = (day_idx.market_open_time, day_idx.market_close_time)
            else:
                bounds = detect_session_times(day_dir, d)
        else:
            bounds = (time(9, 0), time(15, 30))
        self._session_bounds[d] = bounds
        return bounds

    async def get_index_regime(self, d: date) -> MarketIndexRegime:
        self._ensure_loaded(d)
        ks_gap = self.regime_provider.get_kospi_gap(d)
        kq_gap = self.regime_provider.get_kosdaq_gap(d)
        idx_feat = self.feature_provider.get_index_features(d)
        m_open, m_close = self._get_session_bounds(d)
        return MarketIndexRegime(
            trade_date=d,
            ks_gap_pct=float(ks_gap) if ks_gap is not None else 0.0,
            kq_gap_pct=float(kq_gap) if kq_gap is not None else 0.0,
            ks_trend=idx_feat.ks_trend, kq_trend=idx_feat.kq_trend,
            market_gap_std20=idx_feat.market_gap_std20, is_bear_market=idx_feat.is_bear_market,
            dynamic_max_gap=idx_feat.dynamic_max_gap, market_open_time=m_open, market_close_time=m_close,
            source="parquet",
        )

    async def get_prev_closes(self, d: date) -> Dict[str, float]:
        self._ensure_loaded(d)
        return dict(self._prev_closes_by_date.get(d.strftime("%Y-%m-%d"), {}))

    async def get_symbol_features(self, d: date) -> Dict[str, SymbolFeatures]:
        self._ensure_loaded(d)
        dyn_feats = self.feature_provider.get_day_stock_features(d)
        prev_closes = self._prev_closes_by_date.get(d.strftime("%Y-%m-%d"), {})
        nxt = self.nxt_symbols

        out: Dict[str, SymbolFeatures] = {}
        for sym, f in dyn_feats.items():
            nxt_flag = (sym in nxt) if nxt is not None else None
            out[sym] = SymbolFeatures(
                symbol=sym, prior_atr20_pct=f.prior_atr20_pct, prior_adto20=f.prior_adto20,
                prev_close=prev_closes.get(sym, 0.0), is_nxt_eligible=nxt_flag,
            )
        for sym, pc in prev_closes.items():
            if sym not in out:
                nxt_flag = (sym in nxt) if nxt is not None else None
                out[sym] = SymbolFeatures(
                    symbol=sym, prior_atr20_pct=0.0, prior_adto20=0.0, prev_close=pc, is_nxt_eligible=nxt_flag,
                )
        return out
