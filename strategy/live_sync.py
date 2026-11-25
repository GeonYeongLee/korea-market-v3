"""Real-time WebSocket and REST Promotion Synchronization Helpers for CompositeStrategy."""
from __future__ import annotations

import logging
import math
from datetime import datetime, time
from typing import Any, Dict, List, Optional

import numpy as np

from engine.models import MinuteBar, TickData

logger = logging.getLogger("korea_market.strategy.live_sync")


def register_symbol_day_open(
    strategy: Any,
    symbol: str,
    day_open: float,
    day_low: float = 0.0,
    prev_close: float = 0.0,
) -> None:
    """Propagates live session prev_close / day_open / day_low to scanner and entry."""
    scanner = getattr(strategy, "scanner", None)
    if prev_close and prev_close > 0.0 and scanner is not None:
        if hasattr(scanner, "_day_prev_closes") and isinstance(scanner._day_prev_closes, dict):
            scanner._day_prev_closes[symbol] = float(prev_close)
        for sub_attr in ("bull_scanner", "bear_scanner"):
            sub = getattr(scanner, sub_attr, None)
            if sub is not None and hasattr(sub, "_day_prev_closes") and isinstance(sub._day_prev_closes, dict):
                sub._day_prev_closes[symbol] = float(prev_close)

    if day_open and day_open > 0.0:
        if scanner is not None:
            sc_target = getattr(scanner, "active_scanner", scanner)
            if hasattr(sc_target, "_day_open_prices"):
                sc_target._day_open_prices.setdefault(symbol, float(day_open))
            if hasattr(sc_target, "_day_low_prices"):
                eff_low = float(day_low) if day_low and day_low > 0.0 else float(day_open)
                if symbol in sc_target._day_low_prices:
                    sc_target._day_low_prices[symbol] = min(sc_target._day_low_prices[symbol], eff_low)
                else:
                    sc_target._day_low_prices[symbol] = eff_low
        if hasattr(strategy.entry, "register_day_open"):
            strategy.entry.register_day_open(symbol, float(day_open))


def register_symbol_minute_bars(
    strategy: Any,
    symbol: str,
    bars: List[MinuteBar],
    day_open: float = 0.0,
    day_low: float = 0.0,
    cum_turnover: float = 0.0,
    prev_close: float = 0.0,
    is_promoted: bool = True,
) -> List[str]:
    """Replays REST 1-minute bars into scanner and syncs active candidates."""
    if not bars and day_open <= 0.0:
        return []

    first_open = bars[0].open if bars and bars[0].open > 0.0 else day_open
    eff_low = min((b.low for b in bars if b.low > 0.0), default=day_low or first_open)
    if day_low and day_low > 0.0:
        eff_low = min(eff_low, day_low)
    register_symbol_day_open(strategy, symbol, first_open, day_low=eff_low, prev_close=prev_close)

    newly_qualified: List[str] = []
    scanner = strategy.scanner
    for bar in bars:
        cands = scanner.on_bar(bar.date, bar)
        if cands:
            strategy._active_candidates.update(cands)
            newly_qualified.extend(cands)

    # Guard against mid-minute WS truncation: if promoted snapshot already satisfies scanner, register candidate
    if is_promoted and symbol not in strategy._active_candidates and hasattr(scanner, "is_disqualified"):
        if not scanner.is_disqualified(symbol):
            sc_target = getattr(scanner, "active_scanner", scanner)
            min_body = getattr(sc_target, "min_body_pct", 0.035)
            max_mae = getattr(sc_target, "max_mae_pct", -0.012)
            eff_to = getattr(sc_target, "min_cum_turnover", 1_000_000_000.0)
            feats = getattr(sc_target, "_day_features", {})
            if getattr(sc_target, "use_dynamic_turnover", False) and symbol in feats:
                adto = feats[symbol].prior_adto20
                if adto > 0.0:
                    eff_to = float(np.clip(0.08 * adto, 500_000_000.0, 2_500_000_000.0))

            max_bar_surge = max(((b.high - b.open) / b.open for b in bars if b.open > 0.0), default=0.0)
            latest_to = max([b.cum_turnover for b in bars] + [cum_turnover])
            mae = ((eff_low - first_open) / first_open) if first_open > 0.0 else 0.0
            mae_ok = (max_mae is None) or (math.isfinite(mae) and mae >= max_mae)
            if mae_ok and max_bar_surge >= min_body and latest_to >= eff_to:
                if hasattr(sc_target, "_qualified_symbols"):
                    sc_target._qualified_symbols.add(symbol)
                strategy._active_candidates.add(symbol)
                newly_qualified.append(symbol)

    return newly_qualified


def update_forming_bar(forming_bars: Dict[str, MinuteBar], tick: TickData) -> MinuteBar:
    """Maintains an up-to-date intra-minute forming bar from live ticks."""
    sym = tick.symbol
    floor_dt = tick.dt.replace(second=0, microsecond=0)
    prev = forming_bars.get(sym)
    if prev is None or prev.dt != floor_dt:
        bar = MinuteBar(
            symbol=sym,
            dt=floor_dt,
            open=tick.price,
            high=tick.price,
            low=tick.price,
            close=tick.price,
            volume=max(0, int(tick.volume)),
            turnover=max(0.0, float(tick.turnover)),
            cum_turnover=max(0.0, float(tick.cum_turnover)),
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


def try_qualify_tick(strategy: Any, tick: TickData, forming_bar: MinuteBar) -> None:
    """Evaluates forming live tick against scanner criteria within entry window."""
    if tick.symbol in strategy._active_candidates:
        return
    scanner = strategy.scanner
    if hasattr(scanner, "is_disqualified") and scanner.is_disqualified(tick.symbol):
        return
    sc_target = getattr(scanner, "active_scanner", scanner)
    if not hasattr(sc_target, "start_time"):
        return
    t = tick.dt.time()
    if t < sc_target.start_time or t > sc_target.end_time:
        return

    cond = getattr(sc_target, "_condition_symbols", None)
    if cond is not None and tick.symbol not in cond:
        return

    day_open = getattr(scanner, "get_day_open", lambda s: None)(tick.symbol) or tick.day_open
    if not day_open or day_open <= 0.0:
        return
    register_symbol_day_open(strategy, tick.symbol, day_open, day_low=min(tick.day_low or tick.price, forming_bar.low))

    # Re-use scanner's single-bar evaluation on the forming bar without lookahead
    if forming_bar.open > 0.0 and forming_bar.cum_turnover > 0.0:
        cands = scanner.on_bar(forming_bar.date, forming_bar)
        if cands:
            strategy._active_candidates.update(cands)


def build_entry_ctx(strategy: Any, bar: MinuteBar) -> Dict[str, Any]:
    """Constructs entry context dictionary from scanner state."""
    sc = strategy.scanner
    sc_meta = getattr(sc, "get_symbol_metadata", lambda s: {})(bar.symbol) or {}
    return {
        "day_open": getattr(sc, "get_day_open", lambda s: None)(bar.symbol),
        "prev_close": getattr(sc, "get_prev_close", lambda s: None)(bar.symbol) or getattr(sc, "_day_prev_closes", {}).get(bar.symbol),
        "is_vi": bool(getattr(sc, "is_vi", lambda s: False)(bar.symbol) or sc_meta.get("is_vi") or getattr(bar, "is_vi", False)),
        "vi_cls_code": getattr(sc, "get_vi_cls_code", lambda s: None)(bar.symbol) or sc_meta.get("vi_cls_code") or getattr(bar, "vi_cls_code", None),
    }


def submit_entry_order(strategy: Any, context: Any, symbol: str, ref_bar: MinuteBar, signal: Any) -> None:
    """Sizes and dispatches a buy order with reference bar state and async pending tracking."""
    if not (strategy.current_slot_notional > 0 and signal.collar_cap_price > 0):
        return
    target_qty = int(strategy.current_slot_notional // (signal.collar_cap_price * 1.002))
    bal = context.get_balance() if hasattr(context, "get_balance") else None
    if bal and bal.cash > 0:
        qty = min(target_qty, int((bal.cash * 0.995) // signal.collar_cap_price))
    else:
        qty = target_qty
    if qty <= 0:
        return
    logger.info(f"🎯 [ENTRY SIGNAL] {symbol} Trigger: {signal.trigger_price:,.0f} | Collar Cap: {signal.collar_cap_price:,.0f} | Qty: {qty} shares")
    prior_atr = signal.metadata.get("prior_atr20_pct")
    ref_dict = {
        "open": ref_bar.open, "high": ref_bar.high, "low": ref_bar.low, "close": ref_bar.close,
        "dt": ref_bar.dt, "turnover": ref_bar.turnover, "volume": ref_bar.volume, "prior_atr20_pct": prior_atr,
    }
    strategy._pending_ref_bars[symbol] = ref_dict
    order_id = context.buy(symbol=symbol, quantity=qty, price=signal.collar_cap_price, tag=signal.reason)
    if not order_id:
        strategy._pending_ref_bars.pop(symbol, None)
        if hasattr(strategy.entry, "rollback_entered_symbol"):
            strategy.entry.rollback_entered_symbol(symbol)
        return
    strategy._daily_trade_count += 1
    strategy._active_positions.add(symbol)
    strategy._active_candidates.discard(symbol)
    pos = context.get_position(symbol)
    if pos:
        pos.metadata.setdefault("ref_bar", ref_dict)
        if prior_atr is not None:
            pos.metadata.setdefault("prior_atr20_pct", prior_atr)
    else:
        strategy._pending_buy_symbols.add(symbol)

