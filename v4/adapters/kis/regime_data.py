"""
Live Regime, Session Calendar & t-1 Symbol Features Adapter for Korea Market v4.
Implements `IRegimeDataPort` using `FHKUP03500100` (KOSPI/KOSDAQ), `DynamicFeatureProvider`
with `live_only=True`, and NXT eligibility metadata.
"""
from __future__ import annotations

import asyncio
import logging
import os
from datetime import date, time
from typing import Dict, Optional, Set, Tuple

from core.dynamic_features import DynamicFeatureProvider
from core.krx_symbols import get_krx_name_map, load_nxt_symbol_set, refresh_nxt_symbols
from core.market_regime import MarketRegimeProvider, get_default_market_regime_provider
from core.rest_client import KISRestClient
from v4.adapters.kis.venue import KisVenueResolver
from v4.interfaces import IRegimeDataPort
from v4.models import MarketIndexRegime, SymbolFeatures

logger = logging.getLogger("korea_market.v4.adapters.kis.regime_data")

LATE_OPEN_CALENDAR: Dict[date, Tuple[time, time]] = {
    date(2025, 11, 13): (time(10, 0), time(15, 30)),
    date(2026, 1, 2): (time(10, 0), time(15, 30)),
    date(2026, 11, 19): (time(10, 0), time(15, 30)),
}


class KisRegimeDataAdapter(IRegimeDataPort):
    """Live `IRegimeDataPort` combining `FHKUP03500100` index regime and `live_only` features."""

    def __init__(
        self,
        client: Optional[KISRestClient] = None,
        feature_provider: Optional[DynamicFeatureProvider] = None,
        regime_provider: Optional[MarketRegimeProvider] = None,
        nxt_file: Optional[str] = "data/metadata/nxt_symbols.json",
        daily_file: str = "data/dataset_2y/daily_bars_2y.parquet",
        index_file: str = "data/dataset_2y/index_daily.parquet",
    ) -> None:
        self.client = client or KISRestClient()
        self.feature_provider = feature_provider or DynamicFeatureProvider(
            daily_file=daily_file, index_file=index_file
        )
        self.regime_provider = regime_provider or get_default_market_regime_provider()
        nxt_set: Optional[Set[str]] = None
        try:
            refreshed = refresh_nxt_symbols()
            if refreshed:
                nxt_set = refreshed
        except Exception as exc:
            logger.debug(f"refresh_nxt_symbols failed; falling back to local file: {exc}")
        if not nxt_set:
            nxt_set = load_nxt_symbol_set(nxt_file)
        self.nxt_symbols: Optional[Set[str]] = nxt_set
        self.venue: KisVenueResolver = KisVenueResolver(self.nxt_symbols)
        self._prev_closes_cache: Dict[str, float] = {}
        self._features_loaded: bool = False

    def _ensure_features_loaded(self) -> None:
        if self._features_loaded:
            return
        self.feature_provider.load(live_only=True, lookback_days=60)
        try:
            get_krx_name_map()
        except Exception as exc:
            logger.debug(f"KRX symbol map warm-up skipped: {exc}")

        if os.path.exists(self.feature_provider.daily_file):
            try:
                df = self.feature_provider._read_recent_daily_parquet(
                    cols=["date", "symbol", "close"], lookback_days=14
                )
                if not df.empty:
                    df["date_str"] = df["date"].map(self.feature_provider._to_date_str)
                    df = df.sort_values(["symbol", "date_str"])
                    last_rows = df.dropna(subset=["close"]).groupby("symbol", sort=False).last()
                    self._prev_closes_cache = {
                        str(sym): float(row["close"])
                        for sym, row in last_rows.iterrows()
                        if float(row["close"]) > 0.0
                    }
            except Exception as exc:
                logger.warning(f"Failed to load prior closes from {self.feature_provider.daily_file}: {exc}")
        self._features_loaded = True

    @staticmethod
    def get_session_bounds(d: date) -> Tuple[time, time]:
        """Returns `(market_open_time, market_close_time)` for trading date `d`."""
        return LATE_OPEN_CALENDAR.get(d, (time(9, 0), time(15, 30)))

    async def get_index_regime(self, d: date) -> MarketIndexRegime:
        """Queries `FHKUP03500100` for KOSPI/KOSDAQ gap & trend and applies session calendar."""
        m_open, m_close = self.get_session_bounds(d)
        raw = await asyncio.to_thread(self.client.get_kospi_gap_and_market_regime)

        ks_gap = float(raw.get("ks_gap_pct", 0.0))
        kq_gap = float(raw.get("kq_gap_pct", 0.0))
        ks_trend = float(raw.get("ks_trend", 0.0))
        kq_trend = float(raw.get("kq_trend", 0.0))
        gap_std = float(raw.get("gap_std", 0.85))
        is_bear = bool(raw.get("is_bear_market", False))
        dyn_gap = float(raw.get("dynamic_max_gap", 0.85))

        if raw.get("error"):
            logger.warning(f"Live index regime error ({raw.get('error')}); checking local fallback.")
            fallback_ks = self.regime_provider.get_kospi_gap(d)
            fallback_kq = self.regime_provider.get_kosdaq_gap(d)
            if fallback_ks is not None:
                ks_gap = float(fallback_ks)
            if fallback_kq is not None:
                kq_gap = float(fallback_kq)
        else:
            try:
                self.regime_provider.register_live_regime(d, ks_gap, kq_gap)
                self.feature_provider.register_live_index_features(
                    d,
                    ks_trend=ks_trend,
                    kq_trend=kq_trend,
                    is_bear_market=is_bear,
                    dynamic_max_gap=dyn_gap,
                    market_gap_std20=gap_std,
                )
            except Exception as exc:
                logger.debug(f"Could not cache live regime into providers: {exc}")

        return MarketIndexRegime(
            trade_date=d,
            ks_gap_pct=ks_gap,
            kq_gap_pct=kq_gap,
            ks_trend=ks_trend,
            kq_trend=kq_trend,
            market_gap_std20=gap_std,
            is_bear_market=is_bear,
            dynamic_max_gap=dyn_gap,
            market_open_time=m_open,
            market_close_time=m_close,
            source="kis_rest",
        )

    async def get_symbol_features(self, d: date) -> Dict[str, SymbolFeatures]:
        """Returns causal t-1 ATR20/ADTO20/prev_close/NXT eligibility per symbol."""
        await asyncio.to_thread(self._ensure_features_loaded)
        dyn_feats = self.feature_provider.get_day_stock_features(d)
        nxt = self.nxt_symbols

        out: Dict[str, SymbolFeatures] = {}
        for sym, f in dyn_feats.items():
            out[sym] = SymbolFeatures(
                symbol=sym,
                prior_atr20_pct=f.prior_atr20_pct,
                prior_adto20=f.prior_adto20,
                prev_close=self._prev_closes_cache.get(sym, 0.0),
                is_nxt_eligible=(sym in nxt) if nxt is not None else None,
            )
        for sym, pc in self._prev_closes_cache.items():
            if sym not in out:
                out[sym] = SymbolFeatures(
                    symbol=sym,
                    prior_atr20_pct=self.feature_provider.default_atr20_pct,
                    prior_adto20=self.feature_provider.default_adto20,
                    prev_close=pc,
                    is_nxt_eligible=(sym in nxt) if nxt is not None else None,
                )
        return out

    async def get_prev_closes(self, d: date) -> Dict[str, float]:
        """Returns prior trading day settled close prices keyed by 6-digit symbol."""
        await asyncio.to_thread(self._ensure_features_loaded)
        return dict(self._prev_closes_cache)
