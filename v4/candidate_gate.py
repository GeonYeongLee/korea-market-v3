"""
Candidate Qualification & Realtime Subscription Gate for Korea Market v4.
Enforces `seq="0"` vs `seq="1"` Fast-Pass safety, REST bar splice verification,
NXT WS TR routing resolution, and immediate disqualification unsubscription.
"""
from __future__ import annotations

from datetime import datetime
from typing import Dict, List, Optional, Set

from v4.interfaces import IConditionSearchPort, IRealtimeFeedPort
from v4.models import ConditionCandidate, IntradaySnapshot, MinuteBar, SymbolFeatures, TickEvent
from v4.strategy.composite import CompositeStrategyV4


def resolve_is_nxt(
    symbol: str,
    cand: Optional[ConditionCandidate],
    snapshot: Optional[IntradaySnapshot],
    features: Dict[str, SymbolFeatures],
) -> Optional[bool]:
    """Resolves NXT eligibility before WS subscription without blind H0UNCNT0 routing."""
    if cand is not None and cand.is_nxt_eligible is not None:
        return cand.is_nxt_eligible
    feat = features.get(symbol)
    if feat is not None and feat.is_nxt_eligible is not None:
        return feat.is_nxt_eligible
    if snapshot is not None:
        return snapshot.is_nxt_eligible
    return None


def update_forming_bar(
    forming_bars: Dict[str, MinuteBar],
    tick: TickEvent,
    mid_promoted: Optional[Set[str]] = None,
) -> MinuteBar:
    """Maintains an intra-minute forming bar from live TickEvent updates."""
    sym = tick.symbol
    floor_dt = tick.dt.replace(second=0, microsecond=0)
    prev = forming_bars.get(sym)
    if prev is None or prev.dt != floor_dt:
        if prev is not None and prev.dt != floor_dt and mid_promoted is not None:
            mid_promoted.discard(sym)
        cum_to = tick.cum_turnover if tick.cum_turnover > 0.0 else (prev.cum_turnover if prev is not None else 0.0)
        bar = MinuteBar(
            symbol=sym,
            dt=floor_dt,
            open=tick.price,
            high=tick.price,
            low=tick.price,
            close=tick.price,
            volume=max(0, int(tick.volume)),
            turnover=max(0.0, float(tick.turnover)),
            cum_turnover=max(0.0, float(cum_to)),
        )
        forming_bars[sym] = bar
        return bar

    prev.high = max(prev.high, tick.price)
    prev.low = min(prev.low, tick.price)
    prev.close = tick.price
    prev.volume += max(0, int(tick.volume))
    prev.turnover += max(0.0, float(tick.turnover))
    if tick.cum_turnover > prev.cum_turnover:
        prev.cum_turnover = tick.cum_turnover
    return prev


def _seed_forming_or_mid_promoted(
    strategy: CompositeStrategyV4,
    symbol: str,
    eval_min: datetime,
    cand: ConditionCandidate,
    snapshot: Optional[IntradaySnapshot],
    recent_bars: Optional[List[MinuteBar]],
) -> None:
    """Seeds forming bar from current-minute REST bar or flags mid-minute promotion."""
    snap_px = snapshot.current_price if snapshot else 0.0
    snap_to = snapshot.cum_turnover if snapshot else 0.0
    if recent_bars and recent_bars[-1].open > 0.0 and recent_bars[-1].dt.replace(second=0, microsecond=0) == eval_min:
        b = recent_bars[-1]
        strategy._forming_bars[symbol] = MinuteBar(
            symbol=symbol, dt=eval_min, open=b.open,
            high=max(b.high, snap_px) if snap_px > 0.0 else b.high,
            low=min(b.low, snap_px) if (snap_px > 0.0 and b.low > 0.0) else b.low,
            close=snap_px if snap_px > 0.0 else b.close,
            volume=b.volume, turnover=b.turnover, cum_turnover=max(b.cum_turnover, snap_to, cand.krx_cum_turnover),
        )
        strategy._mid_minute_promoted.discard(symbol)
        return
    if cand.bar_open > 0.0 and (cand.bar_dt is None or cand.bar_dt.replace(second=0, microsecond=0) == eval_min):
        high_p = max(cand.bar_high, cand.bar_open, snap_px)
        low_p = min(x for x in (cand.bar_low, cand.bar_open, snap_px) if x > 0.0)
        close_p = snap_px if snap_px > 0.0 else (cand.bar_close if cand.bar_close > 0.0 else high_p)
        strategy._forming_bars[symbol] = MinuteBar(
            symbol=symbol, dt=eval_min, open=cand.bar_open, high=high_p, low=low_p, close=close_p,
            volume=max(0, int(cand.bar_volume)), turnover=max(0.0, float(cand.bar_turnover)),
            cum_turnover=max(cand.krx_cum_turnover, snap_to),
        )
        strategy._mid_minute_promoted.discard(symbol)
        return
    strategy._mid_minute_promoted.add(symbol)


async def process_condition_candidates(
    strategy: CompositeStrategyV4,
    condition_search: IConditionSearchPort,
    feed: IRealtimeFeedPort,
    features: Dict[str, SymbolFeatures],
    prev_closes: Dict[str, float],
    as_of: datetime,
    seq: str,
) -> None:
    """
    Polls HTS condition search (`HHKST03900400`) and enforces:
      - Fast-Pass ONLY when `seq == "0"` or `curr_bar_surge_ok is True`
      - Mandatory `get_recent_minute_bars()` same-bar verification on ambiguous `seq == "1"`
      - Immediate `subscribe(sym, is_nxt=...)` / `unsubscribe(sym)` management
    """
    candidates = await condition_search.poll_candidates(as_of, seq=seq)
    if not candidates:
        return

    active_syms = set(feed.active_symbols())
    eval_min = as_of.replace(second=0, microsecond=0)

    for cand in candidates:
        sym = cand.symbol
        if (
            sym in strategy._active_candidates
            or sym in strategy._active_positions
            or sym in strategy._pending_buy_symbols
            or strategy.is_locked_out(sym)
            or strategy.scanner.is_disqualified(sym)
        ):
            continue

        if cand.prev_close <= 0.0 and sym in prev_closes:
            strategy.scanner.register_day_open(sym, 0.0, prev_close=prev_closes[sym])

        eff_seq = cand.seq or seq
        fast_pass = (eff_seq == "0") or (cand.curr_bar_surge_ok is True)
        eff_min_to = strategy.scanner.effective_min_turnover(sym)

        snapshot: Optional[IntradaySnapshot] = None
        recent_bars: Optional[List[MinuteBar]] = None

        if cand.day_open <= 0.0 or cand.krx_day_low <= 0.0 or cand.krx_cum_turnover <= 0.0 or cand.krx_cum_turnover < eff_min_to:
            snapshot = await condition_search.get_intraday_snapshot(sym, as_of)

        if not fast_pass:
            recent_bars = await condition_search.get_recent_minute_bars(sym, as_of, limit=30)

        if strategy.scanner.evaluate_candidate(cand, snapshot=snapshot, recent_bars=recent_bars):
            is_nxt = resolve_is_nxt(sym, cand, snapshot, features)
            if not await feed.subscribe(sym, is_nxt=is_nxt):
                continue
            active_syms.add(sym)
            day_open = (
                strategy.scanner.get_day_open(sym)
                or cand.day_open
                or (snapshot.day_open if snapshot is not None else 0.0)
            )
            if day_open > 0.0:
                strategy.entry.register_day_open(sym, day_open)
            strategy._active_candidates.add(sym)
            strategy._candidate_bar_min[sym] = eval_min
            _seed_forming_or_mid_promoted(strategy, sym, eval_min, cand, snapshot, recent_bars)
        elif strategy.scanner.is_disqualified(sym) and sym in active_syms:
            await feed.unsubscribe(sym)
            active_syms.discard(sym)
