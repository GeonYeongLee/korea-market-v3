"""
Event Handlers for UnifiedTradingRunner (MinutePulse, Tick, BarClose).
Separates event-level execution flow from lifecycle orchestration in `v4/runner.py`.
"""
from __future__ import annotations

from typing import TYPE_CHECKING, Optional

from v4.candidate_gate import process_condition_candidates, update_forming_bar
from v4.models import BarCloseEvent, MinutePulseEvent, Position, TickEvent

if TYPE_CHECKING:
    from v4.runner import UnifiedTradingRunner


def entry_blocked(runner: UnifiedTradingRunner, symbol: str) -> bool:
    """Returns True if new entries for `symbol` are currently blocked."""
    return (
        runner.strategy.is_daily_trading_completed
        or runner.strategy.current_slot_notional <= 0.0
        or symbol in runner.strategy._pending_buy_symbols
        or runner.strategy.is_locked_out(symbol)
        or runner.risk_guard.is_circuit_broken
    )


async def unsubscribe_if_idle(runner: UnifiedTradingRunner, symbol: str) -> None:
    """Unsubscribes `symbol` from feed if it has no open position and no pending buy."""
    pos = runner.broker.get_position(symbol)
    if (pos is None or pos.quantity <= 0) and symbol not in runner.strategy._pending_buy_symbols:
        if symbol in runner.feed.active_symbols():
            await runner.feed.unsubscribe(symbol)


async def prepare_exit_position(runner: UnifiedTradingRunner, sym: str, pos: Position) -> Optional[Position]:
    """Cancels any pending buy on `sym` before submitting an exit order."""
    if sym in runner.strategy._pending_buy_symbols:
        await runner.broker.cancel_pending(sym)
        await runner.executor.drain_order_events()
        return runner.broker.get_position(sym)
    return pos


async def handle_minute_pulse(runner: UnifiedTradingRunner, ev: MinutePulseEvent) -> None:
    """Processes a 1-minute pulse: stale buy cleanup, hard timeout, and HTS polling."""
    runner.clock.sync_time(ev.dt)
    await runner.executor.cancel_stale_buy_orders(ev.dt)

    if ev.time >= runner.strategy.exit_policy.hard_timeout:
        for sym, pos in list(runner.broker.get_positions().items()):
            if pos and pos.quantity > 0 and sym not in runner.strategy._pending_sell_symbols:
                await runner.broker.cancel_pending(sym)
                await runner.executor.drain_order_events()
                cur = runner.broker.get_position(sym)
                if cur and cur.quantity > 0:
                    exec_p = cur.current_price if cur.current_price > 0.0 else cur.avg_price
                    await runner.executor.submit_exit_order(sym, cur.quantity, exec_p, "HARD_TIMEOUT_1521", ev.dt)

    if ev.time > runner.strategy.cutoff_time or runner.strategy.is_daily_trading_completed:
        for sym in list(runner.feed.active_symbols()):
            await unsubscribe_if_idle(runner, sym)
        return

    if (
        runner.strategy.max_daily_positions > 0
        and not runner.risk_guard.is_circuit_broken
        and runner.strategy.scanner.start_time <= ev.time <= runner.strategy.scanner.end_time
    ):
        open_min = runner._market_open_time.hour * 60 + runner._market_open_time.minute
        ev_min = ev.time.hour * 60 + ev.time.minute
        seq = "0" if ev_min <= open_min + 1 else "1"
        await process_condition_candidates(
            runner.strategy, runner.condition_search, runner.feed,
            runner._day_features, runner._day_prev_closes, ev.dt, seq,
            audit_sink=runner.audit_sink,
        )


async def handle_tick(runner: UnifiedTradingRunner, tick: TickEvent) -> None:
    """Processes a real-time execution tick for exit evaluation or forming-bar entry."""
    if tick.time < runner._market_open_time:
        return
    runner.clock.sync_time(tick.dt)
    runner.broker.on_tick(tick)
    await runner.executor.drain_order_events()

    sym = tick.symbol
    pos = runner.broker.get_position(sym)
    if pos and pos.quantity > 0:
        if sym not in runner.strategy._pending_sell_symbols:
            sig = runner.strategy.exit_policy.evaluate_exit(pos, tick=tick)
            if sig:
                cur = await prepare_exit_position(runner, sym, pos)
                if cur and cur.quantity > 0:
                    await runner.executor.submit_exit_order(sym, cur.quantity, tick.price, sig.reason, tick.dt)
        return

    if entry_blocked(runner, sym):
        return

    forming_bar = update_forming_bar(runner.strategy._forming_bars, tick, runner.strategy._mid_minute_promoted)
    if tick.day_open > 0.0:
        runner.strategy.scanner.register_day_open(sym, tick.day_open, krx_day_low=tick.day_low)
    day_open = runner.strategy.scanner.get_day_open(sym) or tick.day_open
    eff_low = min(tick.day_low, tick.price) if tick.day_low > 0.0 else tick.price
    if day_open > 0.0 and eff_low > 0.0 and runner.strategy.scanner.check_live_tick_mae(sym, day_open, eff_low):
        runner.strategy._active_candidates.discard(sym)
        await unsubscribe_if_idle(runner, sym)
        return

    if sym not in runner.strategy._active_candidates and runner.strategy.scanner.start_time <= tick.time <= runner.strategy.scanner.end_time:
        if forming_bar.open > 0.0 and forming_bar.cum_turnover > 0.0:
            cands = runner.strategy.scanner.on_bar(forming_bar.date, forming_bar)
            if cands and sym in cands:
                runner.strategy._active_candidates.add(sym)

    if sym in runner.strategy._active_candidates:
        ctx = runner.strategy.build_entry_context(sym)
        eval_bar = None if sym in runner.strategy._mid_minute_promoted else forming_bar
        entry_sig = runner.strategy.entry.evaluate_entry(sym, bar=eval_bar, tick=tick, context_state=ctx)
        if entry_sig:
            await runner.executor.submit_entry_order(sym, forming_bar, entry_sig)


async def handle_bar_close(runner: UnifiedTradingRunner, ev: BarCloseEvent) -> None:
    """Processes a completed 1-minute bar: Phase 2 elevation, exits, and backtest entries."""
    bar = ev.bar
    if getattr(bar, "is_premarket", False) or bar.time < runner._market_open_time:
        return
    runner.clock.sync_time(ev.dt)
    runner.broker.on_bar(bar)
    await runner.executor.drain_order_events()

    sym = bar.symbol
    bar_min = bar.dt.replace(second=0, microsecond=0)
    fb = runner.strategy._forming_bars.get(sym)
    if fb is None or fb.dt <= bar_min:
        runner.strategy._mid_minute_promoted.discard(sym)
    pos = runner.broker.get_position(sym)
    if pos and pos.quantity > 0:
        entry_min = pos.entry_dt.replace(second=0, microsecond=0) if pos.entry_dt else None
        if entry_min is not None and bar_min == entry_min:
            runner.strategy.exit_policy.elevate_to_phase2(pos, ref_bar=bar)
            return
        if entry_min is not None and bar_min < entry_min:
            return
        if sym not in runner.strategy._pending_sell_symbols:
            sig = runner.strategy.exit_policy.evaluate_exit(pos, bar=bar)
            if sig:
                cur = await prepare_exit_position(runner, sym, pos)
                if cur and cur.quantity > 0:
                    is_sl = "SL" in sig.reason or "STOP" in sig.reason
                    exec_p = (
                        min(bar.open, sig.target_price)
                        if (not runner.is_live_tick_driven and is_sl and sig.target_price and sig.target_price > 0.0)
                        else (sig.target_price if sig.target_price and sig.target_price > 0.0 else bar.close)
                    )
                    await runner.executor.submit_exit_order(sym, cur.quantity, exec_p, sig.reason, bar.dt)
        return

    if runner.is_live_tick_driven:
        runner.strategy.scanner.on_bar(bar.date, bar)
        if runner.strategy.scanner.is_disqualified(sym):
            runner.strategy._active_candidates.discard(sym)
            await unsubscribe_if_idle(runner, sym)
        return

    if bar.time > runner.strategy.cutoff_time or entry_blocked(runner, sym):
        return

    was_active = sym in runner.strategy._active_candidates
    was_disqualified = runner.strategy.scanner.is_disqualified(sym)
    qualified_now = was_active and (runner.strategy._candidate_bar_min.get(sym) == bar_min)
    cands = runner.strategy.scanner.on_bar(bar.date, bar)
    if cands and sym in cands:
        runner.strategy._active_candidates.add(sym)
        qualified_now = True

    use_opt = runner.execution_mode == "optimistic" and getattr(runner.strategy.scanner, "optimistic_prior_mae", True)
    allow_opt = use_opt and was_active and not was_disqualified
    if not qualified_now and not allow_opt and runner.strategy.scanner.is_disqualified(sym):
        runner.strategy._active_candidates.discard(sym)
        await unsubscribe_if_idle(runner, sym)
        return

    if sym in runner.strategy._active_candidates:
        ctx = runner.strategy.build_entry_context(sym)
        entry_sig = runner.strategy.entry.evaluate_entry(sym, bar=bar, context_state=ctx)
        if entry_sig:
            filled_pos = await runner.executor.submit_entry_order(sym, bar, entry_sig)
            if filled_pos is not None and filled_pos.quantity > 0:
                p1_sig = runner.strategy.exit_policy.evaluate_exit(filled_pos, bar=bar)
                if p1_sig:
                    cur = await prepare_exit_position(runner, sym, filled_pos)
                    if cur and cur.quantity > 0:
                        exec_p = p1_sig.target_price if (p1_sig.target_price and p1_sig.target_price > 0.0) else bar.close
                        await runner.executor.submit_exit_order(sym, cur.quantity, exec_p, p1_sig.reason, bar.dt)
                rem = runner.broker.get_position(sym)
                if rem is not None and rem.quantity > 0:
                    runner.strategy.exit_policy.elevate_to_phase2(rem, ref_bar=bar)

    if runner.strategy.scanner.is_disqualified(sym):
        runner.strategy._active_candidates.discard(sym)
        await unsubscribe_if_idle(runner, sym)
