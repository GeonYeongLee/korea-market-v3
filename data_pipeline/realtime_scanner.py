"""
Real-Time Market Leader Scanner (Automated Fallback & Dynamic Discovery).
========================================================================
Discovers top momentum leaders in real-time during 09:00 ~ 09:25:
1. Primary Pipeline (KIS HTS Condition Search API):
   - Fast polling of KIS HTS Condition Search API (TR: HHKST03900400, seq="0": 모닝_돌파주).
   - Server-side pre-filtering for volume/momentum candidates.
   - Filters: Normal 6-digit stocks (not SPAC/ETF/ETN/preferred), cumulative turnover >= 8~10억,
     gain >= +3.2% (striking distance before +4.5% breakout), MAE > -2.0% (or -1.5%).
   - When candidate symbols appear:
     a) Inject into CompositeStrategy.scanner via strategy.scanner.feed_condition_search_symbol(symbol).
     b) Fetch today's official 1-minute bars using client.get_today_minute_bars(symbol) and evaluate on strategy/scanner.
     c) Promote to KisRealtimeFeed via datafeed.subscribe_symbol(symbol).
2. Secondary Pipeline (External Condition Alerts):
   - Modular hook feed_condition_search_symbol(symbol):
     Allows external alerts to feed symbols for instant promotion.
"""
from __future__ import annotations

import time
import logging
import threading
from datetime import datetime, time as dtime
from typing import Dict, List, Set, Optional, Callable, Any

from config.settings import settings
from core.rest_client import KISRestClient
from core.telemetry import telemetry
from engine.models import MinuteBar
from engine.datafeed.kis_live import KisRealtimeFeed

logger = logging.getLogger("kis.scanner.live")


def parse_kis_minute_bars(symbol: str, raw_bars: List[Dict[str, Any]]) -> List[MinuteBar]:
    """Parses raw KIS REST 1-minute bar responses into standardized MinuteBar instances."""
    if not raw_bars:
        return []

    dates = [str(b.get("stck_bsop_date") or "") for b in raw_bars if b.get("stck_bsop_date")]
    latest_date_str = max(dates) if dates else datetime.now().strftime("%Y%m%d")

    parsed: List[MinuteBar] = []
    for b in raw_bars:
        b_date = str(b.get("stck_bsop_date") or "")
        if latest_date_str and b_date and b_date != latest_date_str:
            continue

        t_str = str(b.get("stck_cntg_hour") or b.get("time") or "")
        op = float(b.get("stck_oprc") or b.get("open") or 0.0)
        hp = float(b.get("stck_hgpr") or b.get("high") or 0.0)
        lp = float(b.get("stck_lwpr") or b.get("low") or 0.0)
        cp = float(b.get("stck_prpr") or b.get("close") or 0.0)
        vol = int(float(b.get("cntg_vol") or b.get("volume") or 0.0))

        to_val = float(b.get("turnover") or 0.0)
        if to_val <= 0 and vol > 0 and cp > 0:
            to_val = vol * cp

        cum_to = float(b.get("acml_tr_pbmn") or b.get("cum_turnover") or 0.0)

        digits = "".join(c for c in t_str if c.isdigit())
        if len(digits) >= 4:
            hour = int(digits[:2])
            minute = int(digits[2:4])
            second = int(digits[4:6]) if len(digits) >= 6 else 0
            if (9 <= hour < 15) or (hour == 15 and minute <= 30):
                try:
                    dt = datetime(
                        int(latest_date_str[:4]),
                        int(latest_date_str[4:6]),
                        int(latest_date_str[6:8]),
                        hour, minute, second
                    )
                except Exception:
                    dt = datetime.now()
                parsed.append(MinuteBar(
                    symbol=symbol,
                    dt=dt,
                    open=op,
                    high=hp,
                    low=lp,
                    close=cp,
                    volume=vol,
                    turnover=to_val,
                    cum_turnover=cum_to,
                ))
    parsed.sort(key=lambda b: b.dt)
    return parsed


class RealtimeLeaderScanner:
    """
    Scans market leaders in real-time via KIS HTS Condition Search API
    and dynamically registers them for ultra-low latency WebSocket tick execution.
    """

    def __init__(
        self,
        client: Optional[KISRestClient] = None,
        datafeed: Optional[KisRealtimeFeed] = None,
        min_turnover_krw: Optional[float] = None,
        min_gain_pct: Optional[float] = None,
        max_mae_pct: Optional[float] = None,
        scan_end_time: Optional[dtime] = None,
        poll_interval_sec: float = 0.5,
        condition_seq: str = "0",
        condition_name: str = "모닝_돌파주도주",
        enable_volume_rank_feeder: bool = True,
        volume_rank_interval_sec: float = 3.0,
        strategy: Any = None,
        context: Any = None,
    ):
        self.client = client or KISRestClient(settings)
        self.datafeed = datafeed
        self.strategy = strategy
        self.context = context
        self.condition_seq = str(condition_seq)
        self.condition_name = condition_name
        self.enable_volume_rank_feeder = enable_volume_rank_feeder
        self.volume_rank_interval_sec = volume_rank_interval_sec
        self._last_volume_rank_poll: float = 0.0

        # Dynamically inherit defaults from strategy if attached
        strat_bo = 4.5
        strat_to = 1_000_000_000.0
        strat_mae = -2.0
        strat_end = dtime(9, 25, 0)

        if strategy:
            scanner_mod = getattr(strategy, "scanner", None)
            if scanner_mod:
                val_to = getattr(scanner_mod, "min_cum_turnover", None)
                if isinstance(val_to, (int, float)):
                    strat_to = float(val_to)

                val_mae = getattr(scanner_mod, "max_mae_pct", None)
                if isinstance(val_mae, (int, float)):
                    strat_mae = float(val_mae)
                    if abs(strat_mae) < 0.2:
                        strat_mae = strat_mae * 100.0  # Normalize ratio to percentage

                val_end = getattr(scanner_mod, "end_time", None)
                if isinstance(val_end, dtime):
                    strat_end = val_end

            entry_mod = getattr(strategy, "entry", None)
            if entry_mod:
                val_bo = getattr(entry_mod, "breakout_threshold_pct", None)
                if isinstance(val_bo, (int, float)):
                    strat_bo = float(val_bo)

        self.min_turnover_krw = min_turnover_krw if min_turnover_krw is not None else (strat_to * 0.8)
        self.min_gain_pct = min_gain_pct if min_gain_pct is not None else max(2.5, strat_bo - 1.0)
        self.max_mae_pct = max_mae_pct if max_mae_pct is not None else strat_mae
        self.scan_end_time = scan_end_time if scan_end_time is not None else strat_end
        self.poll_interval_sec = poll_interval_sec

        self._running = False
        self._thread: Optional[threading.Thread] = None
        self._discovered_symbols: Set[str] = set()
        self._promoted_symbols: Set[str] = set()
        self._mae_blacklisted: Set[str] = set()
        self._price_cache: Dict[str, Dict[str, float]] = {}
        self._last_snapshot_minute: Optional[str] = None
        self._last_gain_pool: Dict[str, tuple[float, Dict[str, Any]]] = {}
        self._callbacks: List[Callable[[str, Dict[str, Any]], None]] = []

    def register_callback(self, cb: Callable[[str, Dict[str, Any]], None]) -> None:
        """Register callback invoked whenever a new candidate enters striking distance."""
        self._callbacks.append(cb)

    def start(self) -> None:
        """Starts background scanner thread."""
        if self._running:
            return
        self._running = True
        self._thread = threading.Thread(target=self._scan_loop, daemon=True, name="RealtimeScanner")
        self._thread.start()
        logger.info(f"RealtimeLeaderScanner background worker started (HTS seq='{self.condition_seq}', name='{self.condition_name}').")

    def stop(self) -> None:
        """Stops background scanner thread."""
        self._running = False
        if self._thread and self._thread.is_alive():
            self._thread.join(timeout=2.0)
        logger.info("RealtimeLeaderScanner stopped.")

    def reset_day(self) -> None:
        """Resets all scanner intraday tracking caches for a new trading day."""
        self._discovered_symbols.clear()
        self._promoted_symbols.clear()
        self._mae_blacklisted.clear()
        self._price_cache.clear()
        self._last_snapshot_minute = None
        self._last_gain_pool.clear()
        logger.info("[RealtimeLeaderScanner] Daily state reset complete for new trading session.")

    def feed_condition_search_symbol(self, symbol: str, metadata: Optional[Dict[str, Any]] = None) -> None:
        """
        External hook for HTS Condition Search alerts.
        When HTS condition search triggers, pass the symbol here for instant WebSocket promotion.
        """
        if symbol not in self._discovered_symbols:
            self._discovered_symbols.add(symbol)
            logger.info(f"⚡ [HTS Condition Search] Symbol received: {symbol}. Promoting to WebSocket.")
            self._promote_symbol(symbol, metadata or {})

    def _promote_symbol(self, symbol: str, item: Dict[str, Any]) -> None:
        """
        Subscribes symbol to live WebSocket, feeds into strategy/scanner, and notifies listeners.
        """
        self._promoted_symbols.add(symbol)
        self._discovered_symbols.add(symbol)

        # a) Inject into CompositeStrategy.scanner
        if self.strategy:
            if hasattr(self.strategy, "scanner") and hasattr(self.strategy.scanner, "feed_condition_search_symbol"):
                self.strategy.scanner.feed_condition_search_symbol(symbol)
            elif hasattr(self.strategy, "feed_condition_search_symbol"):
                self.strategy.feed_condition_search_symbol(symbol)

        # Day Open & Day Low injection if supported (legacy / fallback)
        day_o = float(item.get("day_open", 0.0) or item.get("open", 0.0) or item.get("stck_oprc", 0.0))
        day_l = float(item.get("day_low", 0.0) or item.get("low", 0.0) or item.get("stck_lwpr", 0.0))
        to_krw = float(
            item.get("turnover_krw", 0.0)
            or (float(item.get("trade_amt", 0.0) or 0.0) * 1000.0)
            or item.get("acml_tr_pbmn", 0.0)
        )
        if self.strategy and hasattr(self.strategy, "register_symbol_day_open") and day_o > 0:
            self.strategy.register_symbol_day_open(
                symbol, day_o, day_l, cum_turnover=to_krw, is_promoted=True
            )

        # b) Fetch today's official 1-minute bars using client.get_today_minute_bars(symbol)
        if self.client and hasattr(self.client, "get_today_minute_bars"):
            try:
                raw_bars = self.client.get_today_minute_bars(symbol)
                if raw_bars:
                    if self.strategy and hasattr(self.strategy, "register_symbol_minute_bars"):
                        self.strategy.register_symbol_minute_bars(symbol, raw_bars, is_promoted=True)

                    parsed_bars = parse_kis_minute_bars(symbol, raw_bars)
                    for bar in parsed_bars:
                        if self.strategy:
                            if hasattr(self.strategy, "on_bar"):
                                try:
                                    self.strategy.on_bar(self.context, bar)
                                except TypeError:
                                    self.strategy.on_bar(bar.date, bar)
                            elif hasattr(self.strategy, "scanner") and hasattr(self.strategy.scanner, "on_bar"):
                                self.strategy.scanner.on_bar(bar.date, bar)
                    logger.info(f"📊 [{symbol}] Synced & registered {len(parsed_bars)} 1-minute bars from KIS REST API.")
            except Exception as e:
                logger.warning(f"⚠️ [{symbol}] Could not sync 1-minute bars on promotion: {e}")

        # c) Promote to KisRealtimeFeed via datafeed.subscribe_symbol(symbol)
        if self.datafeed:
            if hasattr(self.datafeed, "subscribe_symbol"):
                self.datafeed.subscribe_symbol(symbol)
                logger.info(f"🎯 [Scanner] Promoted {symbol} to WebSocket feed! Total active: {len(self.datafeed.active_symbols)}")
            elif hasattr(self.datafeed, "set_symbols"):
                current = list(self.datafeed.active_symbols)
                if symbol not in current:
                    current.append(symbol)
                    self.datafeed.set_symbols(current)
                    logger.info(f"🎯 [Scanner] Promoted {symbol} to WebSocket feed! Total active: {len(current)}")

        for cb in self._callbacks:
            try:
                cb(symbol, item)
            except Exception as e:
                logger.error(f"Error in scanner callback for {symbol}: {e}")

    @staticmethod
    def _is_target_stock(code: str, name: str = "", prev_close: Optional[float] = None) -> bool:
        """
        Validates common stock code and filters out preferred shares, SPACs, REITs, ETFs, ETNs, and IPO Day-1.
        """
        code_str = str(code).strip()
        # 1. 6-digit ending in '0' (Common share)
        if len(code_str) != 6 or not code_str.isdigit() or not code_str.endswith("0"):
            return False

        # 2. Exclude SPAC, REIT, ETF, ETN, Infrastructure funds, Inverses, Leverages
        name_upper = name.upper()
        keywords = [
            "스팩", "SPAC", "리츠", "REIT", "인프라", "펀드",
            "인버스", "레버리지", "선물", "ETN", "ETF",
            "KODEX", "TIGER", "ACE", "SOL", "PLUS", "RISE", "KOSEF", "HANARO", "TIMEFOLIO", "히어로즈",
        ]
        if any(kw in name_upper for kw in keywords):
            return False

        # 3. Exclude IPO Day-1 (missing or non-positive prev_close)
        if prev_close is not None and prev_close <= 0.0:
            return False

        return True

    def take_1min_snapshot(
        self,
        minute_str: Optional[str] = None,
        candidate_pool: Optional[Dict[str, tuple[float, Dict[str, Any]]]] = None,
        **kwargs: Any,
    ) -> List[Dict[str, Any]]:
        """
        Takes a full universe snapshot for the specified minute (e.g. '09:02')
        evaluating all candidates in candidate_pool and active watching pool.
        Logs and persists to disk for apples-to-apples 1-minute backtest comparison.
        """
        minute_str = minute_str or datetime.now().strftime("%H:%M")
        pool = (
            candidate_pool
            if candidate_pool is not None
            else kwargs.get("gain_pool", getattr(self, "_last_gain_pool", {}))
        )

        active_symbols: List[str] = []
        if self.datafeed and hasattr(self.datafeed, "active_symbols"):
            active_symbols = list(self.datafeed.active_symbols)
        else:
            active_symbols = list(self._promoted_symbols)

        candidate_codes = set(pool.keys()) | set(active_symbols)
        evaluated_candidates: List[Dict[str, Any]] = []

        for code in candidate_codes:
            meta = pool.get(code, (0.0, {}))[1]
            name = meta.get("hts_kor_isnm") or meta.get("name") or ""

            cached = self._price_cache.get(code, {})
            day_o = cached.get("day_open", float(meta.get("open", 0.0) or meta.get("stck_oprc", 0.0)))
            day_l = cached.get("day_low", float(meta.get("low", 0.0) or meta.get("stck_lwpr", 0.0)))
            curr_p = cached.get("current_price", float(meta.get("price", 0.0) or meta.get("stck_prpr", 0.0)))

            if code in pool:
                open_gain = pool[code][0]
                pool_origin = "CONDITION_SEARCH"
            elif code in active_symbols:
                pool_origin = "ACTIVE_WATCHING"
                open_gain = round(((curr_p - day_o) / day_o) * 100.0, 2) if day_o > 0 else 0.0
            else:
                raw_rate = meta.get("chgrate") or meta.get("prdy_ctrt") or "0.0"
                try:
                    open_gain = float(raw_rate)
                except Exception:
                    open_gain = 0.0
                pool_origin = "CONDITION_SEARCH"

            to_val = cached.get("turnover_krw", 0.0)
            if to_val <= 0:
                if "trade_amt" in meta:
                    to_val = float(meta["trade_amt"] or 0.0) * 1000.0
                else:
                    vol = int(float(meta.get("acml_vol", 0.0) or 0.0))
                    px = float(meta.get("price", 0.0) or meta.get("stck_prpr", 0.0))
                    to_val = vol * px

            mae_pct = None
            if day_o > 0 and day_l > 0:
                mae_pct = round(((day_l - day_o) / day_o) * 100.0, 2)

            if code in active_symbols:
                status = "ACTIVE_WATCHING"
                reason = "웹소켓 실시간 돌파 감시 중"
            elif code in self._mae_blacklisted:
                status = "REJECTED_MAE_BREACH"
                mae_desc = f"{mae_pct:+.2f}%" if mae_pct is not None else "초과"
                reason = f"저가 {mae_desc} <= {self.max_mae_pct}% (영구 탈락)"
            elif to_val < self.min_turnover_krw:
                status = "REJECTED_TURNOVER"
                reason = f"거래대금 {to_val/1e8:.1f}억 < {self.min_turnover_krw/1e8:.1f}억"
            elif open_gain < self.min_gain_pct:
                status = "REJECTED_GAIN_LOW"
                reason = f"상승률 {open_gain:+.2f}% < {self.min_gain_pct:+.2f}%"
            elif mae_pct is not None and mae_pct <= self.max_mae_pct:
                status = "REJECTED_MAE_BREACH"
                reason = f"저가 {mae_pct:+.2f}% <= {self.max_mae_pct}%"
            else:
                status = "PROMOTED"
                reason = "3대 조건 충족 (돌파감시대상)"

            cand_info = {
                "symbol": code,
                "name": name,
                "pool_origin": pool_origin,
                "open_gain_pct": open_gain,
                "turnover_krw": to_val,
                "turnover_eok": to_val / 1e8,
                "day_open": day_o,
                "current_price": curr_p,
                "day_low": day_l,
                "mae_pct": mae_pct,
                "status": status,
                "reason": reason,
            }
            evaluated_candidates.append(cand_info)

        def _sort_key(c: Dict[str, Any]):
            is_active = 1 if c["status"] in ("ACTIVE_WATCHING", "PROMOTED") else 0
            return (is_active, c["open_gain_pct"])

        evaluated_candidates.sort(key=_sort_key, reverse=True)
        for idx, c in enumerate(evaluated_candidates, start=1):
            c["rank"] = idx

        telemetry.record_minutely_scanner_snapshot(
            minute_str=minute_str,
            candidates=evaluated_candidates,
            active_symbols=active_symbols,
        )

        return evaluated_candidates

    def scan_cycle(self) -> List[str]:
        """
        Executes a single ultra-fast scanning cycle:
        1. Queries KIS HTS Condition Search API (TR: HHKST03900400, seq=self.condition_seq).
        2. Direct evaluation of candidate pool:
           - Condition 1: Day Open gain >= min_gain_pct
           - Condition 2: Cumulative turnover >= min_turnover_krw
           - Condition 3: Day Low vs Day Open MAE > max_mae_pct
        3. Promotes passing stocks directly to WebSocket and injects Day Open & 1m bars to strategy.
        4. Takes minutely universe snapshot if minute rolled over.
        Returns list of newly promoted symbols in this cycle.
        """
        newly_promoted: List[str] = []
        try:
            # 1. Fetch HTS Condition Search candidates (TR: HHKST03900400)
            items = self.client.get_condition_search_result(seq=self.condition_seq) or []

            # 1b. Dual Feeder: Supplementary UN (KRX+NXT) Volume Rank top candidates (TR: FHPST01710000)
            # Guarantees zero missed opportunities even if a theme leader's turnover is heavily concentrated on Nextrade.
            if self.enable_volume_rank_feeder:
                now_sec = time.time()
                if now_sec - self._last_volume_rank_poll >= self.volume_rank_interval_sec:
                    self._last_volume_rank_poll = now_sec
                    try:
                        rank_items = self.client.get_volume_rank(market_div="UN")
                        if rank_items:
                            existing_codes = {str(it.get("code", "")).strip() for it in items}
                            for r in rank_items[:30]:  # Top 30 UN turnover leaders
                                r_code = str(r.get("mksc_shrn_iscd") or r.get("stck_shrn_iscd") or "").strip()
                                if r_code and r_code not in existing_codes and self._is_target_stock(r_code):
                                    # Fetch official KRX current quote for consistent 09:00:00 regular open price & range
                                    try:
                                        p_j = self.client.get_current_price(r_code, market_div="J")
                                        if p_j and float(p_j.get("stck_prpr", 0.0) or 0.0) > 0:
                                            tot_to = float(r.get("_tot_to", 0.0) or r.get("acml_tr_pbmn", 0.0) or 0.0)
                                            norm_item = {
                                                "code": r_code,
                                                "name": r.get("hts_kor_isnm") or p_j.get("rprs_mrkt_kor_name", ""),
                                                "price": p_j.get("stck_prpr", "0"),
                                                "open": p_j.get("stck_oprc", "0"),
                                                "high": p_j.get("stck_hgpr", "0"),
                                                "low": p_j.get("stck_lwpr", "0"),
                                                "trade_amt": str(tot_to / 1000.0),
                                                "acml_vol": r.get("acml_vol", "0"),
                                                "chgrate": p_j.get("prdy_ctrt", "0"),
                                                "_source": "VOLUME_RANK_UN",
                                            }
                                            items.append(norm_item)
                                            existing_codes.add(r_code)
                                    except Exception as q_err:
                                        logger.debug(f"VolumeRank candidate quote fetch failed for {r_code}: {q_err}")
                    except Exception as vr_err:
                        logger.debug(f"Dual volume rank poll failed: {vr_err}")

            if not items:
                logger.debug(f"[Scanner] Condition search & UN volume rank returned 0 items.")
                return newly_promoted

            candidate_pool: Dict[str, tuple[float, Dict[str, Any]]] = {}

            for it in items:
                code = str(it.get("code", "")).strip()
                name = str(it.get("name", "")).strip()
                if not self._is_target_stock(code, name):
                    continue

                px = float(it.get("price", 0.0) or 0.0)
                day_o = float(it.get("open", 0.0) or 0.0)
                day_l = float(it.get("low", 0.0) or 0.0)
                day_h = float(it.get("high", 0.0) or 0.0)
                to_krw = float(it.get("trade_amt", 0.0) or 0.0) * 1000.0
                vol = int(float(it.get("acml_vol", 0.0) or 0.0))
                chgrate = float(it.get("chgrate", 0.0) or 0.0)

                open_gain = round(((px - day_o) / day_o) * 100.0, 2) if day_o > 0 else 0.0
                mae_pct = round(((day_l - day_o) / day_o) * 100.0, 2) if day_o > 0 else 0.0

                self._price_cache[code] = {
                    "day_open": day_o,
                    "day_low": day_l,
                    "day_high": day_h,
                    "current_price": px,
                    "turnover_krw": to_krw,
                }
                candidate_pool[code] = (open_gain, it)

                if code in self._discovered_symbols:
                    continue

                # Condition 1: Gain check
                if open_gain < self.min_gain_pct:
                    if open_gain >= 2.0:
                        telemetry.log_scanner_audit(
                            symbol=code, name=name, open_gain_pct=open_gain,
                            turnover_krw=to_krw, day_open=day_o, current_price=px,
                            day_low=day_l, mae_pct=mae_pct, decision="NEAR_STRIKE_WATCH",
                            reason=f"Open gain (+{open_gain:.2f}%) < {self.min_gain_pct}%"
                        )
                    continue

                # Condition 2: Cumulative Turnover >= min_turnover_krw
                if to_krw < self.min_turnover_krw:
                    logger.info(
                        f"🔍 [Scanner Watch: Low Turnover] {code} ({name}) | "
                        f"DayOpen: +{open_gain:.2f}% (>= {self.min_gain_pct}%), but "
                        f"Turnover: {to_krw/1e8:,.1f}억 KRW (< {self.min_turnover_krw/1e8:,.1f}억)"
                    )
                    telemetry.log_scanner_audit(
                        symbol=code, name=name, open_gain_pct=open_gain,
                        turnover_krw=to_krw, day_open=day_o, current_price=px,
                        day_low=day_l, mae_pct=mae_pct, decision="REJECTED_TURNOVER",
                        reason=f"Turnover {to_krw/1e8:,.1f}억 < {self.min_turnover_krw/1e8:,.1f}억"
                    )
                    continue

                # Condition 3: Day Low vs Day Open (MAE > max_mae_pct)
                if mae_pct <= self.max_mae_pct:
                    self._mae_blacklisted.add(code)
                    self._discovered_symbols.add(code)
                    logger.warning(
                        f"🚫 [Pre-Scan Filtered: MAE Breach] {code} ({name}) "
                        f"Day Low dropped to {mae_pct:.2f}% (<= {self.max_mae_pct}%). "
                        f"Skipping WebSocket subscription!"
                    )
                    telemetry.log_scanner_audit(
                        symbol=code, name=name, open_gain_pct=open_gain,
                        turnover_krw=to_krw, day_open=day_o, current_price=px,
                        day_low=day_l, mae_pct=mae_pct, decision="REJECTED_MAE_BREACH",
                        reason=f"Day Low {mae_pct:.2f}% <= {self.max_mae_pct}%"
                    )
                    continue

                # ===== ALL 3 CONDITIONS SATISFIED =====
                self._promoted_symbols.add(code)
                self._discovered_symbols.add(code)
                newly_promoted.append(code)

                promo_meta = dict(it)
                promo_meta.update({
                    "symbol": code,
                    "code": code,
                    "name": name,
                    "hts_kor_isnm": name,
                    "day_open": day_o,
                    "day_low": day_l,
                    "day_high": day_h,
                    "current_price": px,
                    "turnover_krw": to_krw,
                    "open_gain_pct": open_gain,
                    "chgrate": chgrate,
                    "stck_oprc": day_o,
                    "stck_lwpr": day_l,
                    "acml_tr_pbmn": to_krw,
                    "acml_vol": vol,
                })

                mae_str = f"{mae_pct:+.2f}%" if day_o > 0 else "N/A"
                logger.info(
                    f"🎯 [Condition Search Promoted: ALL 3 Met] {code} ({name}) | "
                    f"DayOpen Chg: +{open_gain:.2f}% (>= {self.min_gain_pct}%) AND "
                    f"Turnover: {to_krw/1e8:,.1f}억 KRW (>= {self.min_turnover_krw/1e8:,.1f}억) AND "
                    f"MAE: {mae_str} (> {self.max_mae_pct}%) -> Subscribing WebSocket!"
                )
                telemetry.log_scanner_audit(
                    symbol=code, name=name, open_gain_pct=open_gain,
                    turnover_krw=to_krw, day_open=day_o, current_price=px,
                    day_low=day_l, mae_pct=mae_pct,
                    decision="PROMOTED", reason="ALL_3_CONDITIONS_MET"
                )
                self._promote_symbol(code, promo_meta)

            self._last_gain_pool = candidate_pool

            # Snapshot on minute rollover
            now_dt = datetime.now()
            minute_str = now_dt.strftime("%H:%M")
            if self._last_snapshot_minute != minute_str:
                self._last_snapshot_minute = minute_str
                self.take_1min_snapshot(
                    minute_str=minute_str,
                    candidate_pool=candidate_pool,
                )

        except Exception as e:
            logger.error(f"Scanner cycle error: {e}", exc_info=True)

        return newly_promoted

    def _scan_loop(self) -> None:
        """
        HTS Condition Search real-time scanning loop:
        Polls KIS HTS Condition Search API (seq=self.condition_seq) every poll_interval_sec.
        Active window: 09:00:00 ~ scan_end_time (e.g. 09:25:00).
        """
        logger.info(
            f"Starting live condition search leader scanner loop "
            f"(seq='{self.condition_seq}', name='{self.condition_name}', "
            f"Turnover >= {self.min_turnover_krw/1e8:,.1f}억 AND Open Gain >= {self.min_gain_pct}%)..."
        )

        while self._running:
            now_t = datetime.now().time()
            if dtime(9, 0, 0) <= now_t <= self.scan_end_time:
                self.scan_cycle()
                time.sleep(self.poll_interval_sec)
            else:
                time.sleep(1.0)
