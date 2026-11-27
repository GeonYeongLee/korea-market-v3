"""
Live HTS Condition Search (`HHKST03900400`) & Causal Snapshot Adapter for Korea Market v4.
Implements `IConditionSearchPort` with strict <= 18 TPS rate limiting and KRX/NXT
regular-session `day_open`/`day_low`/`day_high` isolation from 08:00~08:50 premarket.
"""
from __future__ import annotations

import asyncio
import logging
from datetime import datetime, time
from typing import Any, Dict, List, Optional, Set

from core.rate_limiter import RateLimiter, get_rate_limiter
from core.rest_client import KISRestClient
from v4.adapters.kis._parsers import safe_float as _f
from v4.adapters.kis._parsers import safe_int as _i
from v4.adapters.kis.regime_data import load_nxt_symbol_set
from v4.adapters.kis.venue import KisVenueResolver
from v4.interfaces import IConditionSearchPort
from v4.models import ConditionCandidate, IntradaySnapshot, MinuteBar

logger = logging.getLogger("korea_market.v4.adapters.kis.condition_search")


class KisConditionSearchAdapter(IConditionSearchPort):
    """Live KIS REST implementation of `IConditionSearchPort`."""

    def __init__(
        self,
        client: Optional[KISRestClient] = None,
        nxt_symbols: Optional[Set[str]] = None,
        nxt_file: Optional[str] = "data/metadata/nxt_symbols.json",
        market_open_time: time = time(9, 0),
        rate_limiter: Optional[RateLimiter] = None,
        venue: Optional[KisVenueResolver] = None,
    ) -> None:
        self.client = client or KISRestClient()
        if venue is not None:
            self.venue = venue
            self.nxt_symbols: Optional[Set[str]] = (
                set(venue.nxt_symbols) if venue.has_nxt_metadata else None
            )
        else:
            self.nxt_symbols = (
                nxt_symbols if nxt_symbols is not None else load_nxt_symbol_set(nxt_file)
            )
            self.venue = KisVenueResolver(self.nxt_symbols)
        self.market_open_time = market_open_time
        self.limiter = rate_limiter or get_rate_limiter(getattr(self.client.cfg, "env", "real"))
        if self.limiter.rate > 18.0:
            self.limiter = RateLimiter(rate=18.0, capacity=18.0)

    def set_market_open_time(self, market_open_time: time) -> None:
        """Updates regular-session open time (e.g., 10:00 on CSAT/New Year days)."""
        self.market_open_time = market_open_time

    def _is_nxt(self, symbol: str) -> Optional[bool]:
        return self.venue.is_nxt_or_none(symbol)

    async def poll_candidates(self, as_of: datetime, seq: str = "0") -> List[ConditionCandidate]:
        """Polls `HHKST03900400` (`seq="0"` or `"1"`) rate-limited <= 18 TPS."""
        await self.limiter.acquire_async()
        rows = await asyncio.to_thread(self.client.get_condition_search_result, seq)
        candidates: List[ConditionCandidate] = []
        for row in rows or []:
            sym = str(row.get("code") or row.get("stck_shrn_iscd") or "").strip()
            if len(sym) != 6 or not sym.isdigit():
                continue
            candidates.append(
                ConditionCandidate(
                    symbol=sym,
                    name=str(row.get("name") or row.get("hts_kor_isnm") or "").strip(),
                    day_open=_f(row.get("open")),
                    krx_day_low=_f(row.get("low")),
                    krx_cum_turnover=_f(row.get("trade_amt")) * 1000.0,
                    current_price=_f(row.get("price")),
                    is_nxt_eligible=self._is_nxt(sym),
                    seq=str(seq),
                    as_of=as_of,
                    chgrate=_f(row.get("chgrate")),
                    cttr=_f(row.get("cttr")),
                )
            )
        return candidates

    async def get_intraday_snapshot(self, symbol: str, as_of: datetime) -> Optional[IntradaySnapshot]:
        """
        Fetches causal snapshot as of `as_of`:
        - For NXT-eligible: `cum_turnover`/`cum_volume` from `"UN"`,
          `day_open`/`day_low`/`day_high` strictly from `"J"` (08:00~08:50 isolation).
        - For non-NXT: `"J"` only.
        """
        is_nxt = self._is_nxt(symbol)
        un_out: Dict[str, Any] = {}
        j_out: Dict[str, Any] = {}

        if is_nxt is not False:
            try:
                await self.limiter.acquire_async()
                un_out = await asyncio.to_thread(self.client.get_current_price, symbol, "UN") or {}
            except Exception as exc:
                logger.warning("get_current_price(%s, UN) failed: %s", symbol, exc)

        try:
            await self.limiter.acquire_async()
            j_out = await asyncio.to_thread(self.client.get_current_price, symbol, "J") or {}
        except Exception as exc:
            logger.warning("get_current_price(%s, J) failed: %s", symbol, exc)

        if not un_out and not j_out:
            return None

        un_vol = _i(un_out.get("acml_vol"))
        j_vol = _i(j_out.get("acml_vol"))
        resolved_nxt = is_nxt if is_nxt is not None else (un_vol > j_vol and un_vol > 0)

        turn_src = un_out if (resolved_nxt and _f(un_out.get("acml_tr_pbmn")) > 0.0) else j_out
        if not turn_src:
            turn_src = un_out or j_out

        # Strictly isolate day_open / day_low / day_high from KRX ("J") or regular bars
        day_open = _f(j_out.get("stck_oprc"))
        day_low = _f(j_out.get("stck_lwpr"))
        day_high = _f(j_out.get("stck_hgpr"))
        cur_price = _f(turn_src.get("stck_prpr")) or _f(j_out.get("stck_prpr"))

        if day_open <= 0.0 or day_low <= 0.0:
            reg_bars = await self.get_recent_minute_bars(symbol, as_of, limit=60)
            if reg_bars:
                if day_open <= 0.0:
                    day_open = reg_bars[0].open
                if day_low <= 0.0:
                    lows = [b.low for b in reg_bars if b.low > 0.0]
                    day_low = min(lows) if lows else day_low
                if day_high <= 0.0:
                    highs = [b.high for b in reg_bars if b.high > 0.0]
                    day_high = max(highs) if highs else day_high

        return IntradaySnapshot(
            symbol=symbol,
            as_of=as_of,
            day_open=day_open,
            day_low=day_low,
            day_high=day_high,
            current_price=cur_price,
            cum_turnover=_f(turn_src.get("acml_tr_pbmn")),
            cum_volume=_i(turn_src.get("acml_vol")),
            is_nxt_eligible=bool(resolved_nxt),
            source="UN+J" if resolved_nxt else "J",
            prev_close=_f(j_out.get("stck_prdy_clpr")) or _f(un_out.get("stck_prdy_clpr")),
        )

    async def get_recent_minute_bars(self, symbol: str, as_of: datetime, limit: int = 30) -> List[MinuteBar]:
        """Fetches regular-session (>= market_open_time, volume > 0) 1m bars up to `as_of`."""
        to_hour = as_of.strftime("%H%M%S")
        raw_bars: List[Dict[str, Any]] = []
        for m_div in self.venue.minute_bar_divs():
            try:
                await self.limiter.acquire_async()
                raw_bars = await asyncio.to_thread(
                    self.client.get_today_minute_bars, symbol, to_hour, m_div
                )
                if raw_bars:
                    break
            except Exception as exc:
                logger.warning("get_today_minute_bars(%s, %s) failed: %s", symbol, m_div, exc)
        if not raw_bars:
            return []

        as_of_time = as_of.time()
        parsed: List[MinuteBar] = []
        for row in raw_bars:
            hhmmss = str(row.get("stck_cntg_hour") or "").strip().zfill(6)
            if len(hhmmss) < 6 or not hhmmss.isdigit():
                continue
            try:
                b_time = time(int(hhmmss[:2]), int(hhmmss[2:4]), 0)
            except ValueError:
                continue
            if b_time < self.market_open_time or b_time > as_of_time:
                continue

            vol = _i(row.get("cntg_vol"))
            if vol <= 0:
                continue

            close_p = _f(row.get("stck_prpr"))
            open_p = _f(row.get("stck_oprc")) or close_p
            high_p = _f(row.get("stck_hgpr")) or max(open_p, close_p)
            low_p = _f(row.get("stck_lwpr")) or min(open_p, close_p)
            cum_to = _f(row.get("acml_tr_pbmn"))
            b_dt = as_of.replace(hour=b_time.hour, minute=b_time.minute, second=0, microsecond=0)

            parsed.append(
                MinuteBar(
                    symbol=symbol,
                    dt=b_dt,
                    open=open_p,
                    high=high_p,
                    low=low_p,
                    close=close_p,
                    volume=vol,
                    turnover=float(vol) * close_p,
                    vwap=close_p,
                    cum_turnover=cum_to,
                    is_premarket=False,
                    source="kis_rest",
                )
            )

        parsed.sort(key=lambda b: b.dt)
        if len(parsed) > 1:
            for idx in range(1, len(parsed)):
                diff_to = parsed[idx].cum_turnover - parsed[idx - 1].cum_turnover
                if diff_to > 0.0:
                    parsed[idx].turnover = diff_to

        return parsed[-limit:] if limit > 0 else parsed
