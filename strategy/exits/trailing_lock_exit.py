"""
Trailing Lock Exit Policy.
Implements custom TP1 Lock + Trailing Run + Hard Time-Cut:
  1. Hard Stop-Loss (-1.5% to -2.0% from entry price)
  2. TP1 Profit Lock (+2.0% to +2.5% target; ratchets SL to entry + 0.5% ~ +1.0% locked profit)
  3. Trailing Run (after TP1 is reached, continuously trails from highest high by trailing_offset_pct)
  4. Hard Timeout (liquidates unconditionally at 15:00:00 or 15:15:00; zero overnight risk)
  5. Pessimistic Ambiguity Defense (if High >= TP and Low <= SL in same bar, SL triggers first)
  6. Ratchet Monotonicity Guarantee (SL price NEVER decreases once elevated)
  7. KRX Tick Conformance (all sell/stop prices rounded via side='sell' [Ceil])
"""
from __future__ import annotations

import math
from datetime import date, datetime, time
from typing import Optional, Any

from core.krx_ticks import round_to_krx_tick
from core.telemetry import telemetry
from engine.models import MinuteBar, TickData, Position, ExitSignal, OrderType
from strategy.base import IExitPolicy


class TrailingLockExit(IExitPolicy):
    """
    TP1 Lock + Continuous Trailing Stop + Hard Timeout Exit Policy.
    """

    name: str = "trailing_lock_exit"

    def __init__(
        self,
        hard_sl_pct: float = -0.015,            # Hard Stop-Loss: -1.5% from entry price
        tp1_pct: float = 0.025,                 # TP1 Profit Target: +2.5% from entry price
        lock_profit_pct: float = 0.010,         # Locked Profit floor upon TP1: +1.0% from entry price
        trailing_offset_pct: float = 0.015,     # Trailing offset: 1.5% below peak High-Water-Mark
        hard_timeout: time = time(15, 0),       # 15:00:00 Hard timeout liquidation
        ambiguity_mode: str = "pessimistic",    # 'pessimistic' (SL priority) or 'optimistic'
        hard_stop_pct: Optional[float] = None,
        tp1_target_pct: Optional[float] = None,
        trailing_buffer_pct: Optional[float] = None,
        **kwargs: Any,
    ):
        if hard_stop_pct is not None:
            hard_sl_pct = hard_stop_pct
        if tp1_target_pct is not None:
            tp1_pct = tp1_target_pct
        if trailing_buffer_pct is not None:
            trailing_offset_pct = trailing_buffer_pct

        self.hard_sl_pct = -abs(hard_sl_pct)
        self.tp1_pct = abs(tp1_pct)
        self.lock_profit_pct = abs(lock_profit_pct)
        self.trailing_offset_pct = abs(trailing_offset_pct)
        self.hard_timeout = hard_timeout
        self.ambiguity_mode = ambiguity_mode.lower()

    @staticmethod
    def _make_signal(position: Position, dt: datetime, price: float, reason: str) -> ExitSignal:
        return ExitSignal(
            symbol=position.symbol,
            dt=dt,
            order_type=OrderType.MARKET,
            target_price=price,
            reason=reason,
        )

    def on_position_opened(self, position: Position) -> None:
        """Called immediately upon order fill. Initializes stop-loss and take-profit targets."""
        position.phase = 1

        # Hard stop-loss rounded Ceil (adverse KRX statutory tick for sell)
        raw_sl = position.avg_price * (1.0 + self.hard_sl_pct)
        position.stop_loss_price = round_to_krx_tick(raw_sl, side="SELL", round_mode="ceil")

        # TP1 target price rounded Ceil
        raw_tp = position.avg_price * (1.0 + self.tp1_pct)
        position.take_profit_price = round_to_krx_tick(raw_tp, side="SELL", round_mode="ceil")

        position.high_water_mark = position.avg_price
        position.metadata["tp1_reached"] = False
        position.metadata["hard_sl_pct"] = self.hard_sl_pct
        position.metadata["tp1_pct"] = self.tp1_pct
        position.metadata["lock_profit_pct"] = self.lock_profit_pct
        position.metadata["trailing_offset_pct"] = self.trailing_offset_pct

    def on_day_open(self, current_date: date) -> None:
        """Called at 09:00:00 to reset daily states."""
        pass

    def evaluate_exit(
        self,
        position: Position,
        bar: Optional[MinuteBar] = None,
        tick: Optional[TickData] = None,
    ) -> Optional[ExitSignal]:
        """
        Evaluates position liquidation conditions.
        Enforces Pessimistic Ambiguity Defense and Ratchet Monotonicity.
        """
        current_price = tick.price if tick else (bar.close if bar else position.current_price)
        current_low = tick.price if tick else (bar.low if bar else position.current_price)
        current_high = tick.price if tick else (bar.high if bar else position.current_price)
        current_dt = tick.dt if tick else (bar.dt if bar else position.entry_dt)

        if not current_dt or not math.isfinite(current_price) or current_price <= 0.0:
            return None

        # 1. Hard Timeout Cutoff Check (15:00:00 / 15:15:00)
        if current_dt.time() >= self.hard_timeout:
            sig = self._make_signal(position, current_dt, current_price, "HARD_TIMEOUT")
            telemetry.record_strategy_eval(
                "EXIT", position.symbol, "TRIGGER", "HARD_TIMEOUT",
                {"current_price": current_price, "timeout": str(self.hard_timeout)},
                dt=current_dt,
            )
            return sig

        # Helper: Ratchet Trailing SL calculation
        def _apply_trailing_ratchet() -> None:
            if current_high > position.high_water_mark:
                position.high_water_mark = current_high

            # Check if TP1 target has been reached
            if current_high >= position.take_profit_price:
                position.metadata["tp1_reached"] = True

            # If TP1 hit: elevate SL to locked profit and trail from peak HWM
            if position.metadata.get("tp1_reached", False):
                raw_locked = position.avg_price * (1.0 + self.lock_profit_pct)
                locked_sl = round_to_krx_tick(raw_locked, side="SELL", round_mode="ceil")

                raw_trail = position.high_water_mark * (1.0 - self.trailing_offset_pct)
                trail_sl = round_to_krx_tick(raw_trail, side="SELL", round_mode="ceil")

                # Ratchet monotonicity: SL price NEVER decreases once elevated
                position.stop_loss_price = max(position.stop_loss_price, locked_sl, trail_sl)

        # Helper: Stop Loss Check
        def _check_stop_loss() -> Optional[ExitSignal]:
            if position.stop_loss_price > 0.0 and current_low <= position.stop_loss_price:
                is_trailing = bool(position.metadata.get("tp1_reached", False))
                reason = "TRAILING_STOP" if is_trailing else "HARD_STOP_LOSS"
                has_target = position.stop_loss_price > 0
                exec_price = min(bar.open, position.stop_loss_price) if (bar and has_target) else position.stop_loss_price
                return self._make_signal(position, current_dt, exec_price, reason)
            return None

        # 2. Ambiguity Defense Evaluation
        if self.ambiguity_mode == "pessimistic":
            # Pessimistic Ambiguity Defense:
            # Check Stop-Loss FIRST based on the pre-existing SL price before updating TP / trailing run
            stop_sig = _check_stop_loss()
            if stop_sig:
                telemetry.record_strategy_eval(
                    "EXIT", position.symbol, "TRIGGER", stop_sig.reason,
                    {"stop_loss_price": position.stop_loss_price, "current_low": current_low},
                    dt=current_dt,
                )
                return stop_sig

            # SL did not trigger: now update HWM, check TP1, and ratchet trailing stop
            _apply_trailing_ratchet()

            # Check if candle closed or tick executed below newly ratcheted trailing SL
            if (bar and bar.close <= position.stop_loss_price) or (tick and tick.price <= position.stop_loss_price):
                sig = self._make_signal(position, current_dt, position.stop_loss_price, "TRAILING_STOP")
                telemetry.record_strategy_eval(
                    "EXIT", position.symbol, "TRIGGER", "TRAILING_STOP",
                    {"stop_loss_price": position.stop_loss_price, "current_price": current_price},
                    dt=current_dt,
                )
                return sig

        else:
            # Optimistic Mode:
            # Check pre-existing SL with current_low first (prevents intra-bar low self-stop on newly ratcheted SL)
            orig_sl = position.stop_loss_price
            if orig_sl > 0.0 and current_low <= orig_sl:
                is_trailing = bool(position.metadata.get("tp1_reached", False))
                reason = "TRAILING_STOP" if is_trailing else "HARD_STOP_LOSS"
                has_target = orig_sl > 0
                exec_price = min(bar.open, orig_sl) if (bar and has_target) else orig_sl
                sig = self._make_signal(position, current_dt, exec_price, reason)
                telemetry.record_strategy_eval(
                    "EXIT", position.symbol, "TRIGGER", sig.reason,
                    {"stop_loss_price": orig_sl, "current_low": current_low},
                    dt=current_dt,
                )
                return sig

            # SL did not trigger: now update HWM, check TP1, and ratchet trailing stop
            _apply_trailing_ratchet()

            # Newly elevated trailing SL is only breached if candle closes below it (or live tick trades below it)
            if (bar and bar.close <= position.stop_loss_price) or (tick and tick.price <= position.stop_loss_price):
                sig = self._make_signal(position, current_dt, position.stop_loss_price, "TRAILING_STOP")
                telemetry.record_strategy_eval(
                    "EXIT", position.symbol, "TRIGGER", "TRAILING_STOP",
                    {"stop_loss_price": position.stop_loss_price, "current_price": current_price},
                    dt=current_dt,
                )
                return sig

        telemetry.record_strategy_eval(
            "EXIT", position.symbol, "HOLDING", "POSITION_HOLDING",
            {"current_price": current_price, "stop_loss_price": position.stop_loss_price, "hwm": position.high_water_mark},
            dt=current_dt,
        )
        return None
