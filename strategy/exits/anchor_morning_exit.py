"""
Anchor Morning Exit Policy with True Smart Step-Up.
Designed for Anchor Close Betting (Overnight Continuation):
  1. Overnight Gap & Morning Surge Capture:
     - On D+1 morning (09:00~09:30), stock surges from overnight FOMO.
  2. Instant +3.0% Floor Lock:
     - The instant +3.0% is touched (or gapped up), SL is elevated to at least +3.0%.
  3. Continuous Causal Trailing:
     - Ratchets upwards at (Peak - 1.0%), letting winners run to +10%, +15%, +20%.
  4. Hard Timeout:
     - 15:20 EOD liquidation invariant.
  5. Pessimistic Ambiguous Bar Defense:
     - Strict SL priority on extreme dual-bound bars.
"""
from __future__ import annotations

from datetime import date, datetime, time
from typing import Optional, Any

from core.krx_ticks import round_to_krx_tick
from engine.models import MinuteBar, TickData, Position, ExitSignal, OrderType
from strategy.base import IExitPolicy


class AnchorMorningExit(IExitPolicy):
    """
    Morning Smart Step-Up Exit Policy for Anchor Close Betting.
    """

    name: str = "anchor_morning_exit"

    def __init__(
        self,
        tp_target_pct: float = 0.030,          # +3.0% Take-Profit Target Lock
        trailing_buffer_pct: float = 0.010,    # -1.0% buffer from peak (HWM)
        emergency_sl_pct: float = -0.025,      # -2.5% Pre-TP stop-loss floor
        morning_cutoff: Optional[time] = time(9, 30), # Optional 09:30 morning profit harvest
        hard_timeout: time = time(15, 20),     # 15:20 EOD hard timeout invariant
        ambiguity_mode: str = "pessimistic",   # "pessimistic" or "optimistic"
        **kwargs: Any,
    ):
        self.tp_target_pct = tp_target_pct
        self.trailing_buffer_pct = trailing_buffer_pct
        self.emergency_sl_pct = emergency_sl_pct
        self.morning_cutoff = morning_cutoff
        self.hard_timeout = hard_timeout
        self.ambiguity_mode = ambiguity_mode

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
        position.phase = 1
        position.high_water_mark = position.avg_price

        # 1. Pre-TP Stop-Loss (-2.5%)
        raw_sl = position.avg_price * (1.0 + self.emergency_sl_pct)
        position.stop_loss_price = round_to_krx_tick(raw_sl, side="SELL", round_mode="floor")

        # 2. Take-Profit Target Lock (+3.0%)
        raw_tp = position.avg_price * (1.0 + self.tp_target_pct)
        position.take_profit_price = round_to_krx_tick(raw_tp, side="SELL", round_mode="ceil")

        position.metadata["locked_at_tp"] = False
        position.metadata["bars_held"] = 0

    def on_day_open(self, current_date: date) -> None:
        pass

    def evaluate_exit(
        self,
        position: Position,
        bar: MinuteBar,
        tick: Optional[TickData] = None,
    ) -> Optional[ExitSignal]:
        eval_dt = tick.dt if tick else bar.dt
        eval_time = eval_dt.time()

        # 1. Hard Timeout Liquidation (15:20:00)
        if eval_time >= self.hard_timeout:
            return self._make_signal(position, eval_dt, bar.close, "TIMEOUT_1520")

        # 2. Morning Cutoff (09:30:00 on D+1, if position is carried overnight)
        if self.morning_cutoff is not None and position.entry_dt is not None:
            # Check if this bar is on a day after entry
            if bar.dt.date() > position.entry_dt.date() and eval_time >= self.morning_cutoff:
                return self._make_signal(position, eval_dt, bar.close, "MORNING_CUTOFF_0930")

        # 3. Phase 1 -> Phase 2 Transition (Subsequent Bar 1+)
        is_subsequent = bar is not None and position.entry_dt is not None and bar.dt > position.entry_dt
        if is_subsequent and position.phase == 1:
            position.phase = 2
            ref_bar = position.metadata.get("ref_bar") or {}
            eff_floor = abs(self.emergency_sl_pct)
            floor_sl = round_to_krx_tick(position.avg_price * (1.0 - eff_floor), side="SELL", round_mode="ceil")
            if ref_bar.get("open") is not None and ref_bar.get("close") is not None:
                ref_mid = (ref_bar["open"] + ref_bar["close"]) / 2.0
                center_sl = round_to_krx_tick(ref_mid, side="SELL", round_mode="floor")
                target_sl = max(center_sl, floor_sl)
            else:
                target_sl = floor_sl
            position.stop_loss_price = max(position.stop_loss_price, target_sl)

        prior_sl = position.stop_loss_price
        was_locked = position.metadata.get("locked_at_tp", False)

        # 4. If ALREADY Locked at TP: Causal Trailing Stop Regime
        if was_locked:
            if bar.low <= prior_sl:
                reason = "SMART_STEP_UP_TRAILING_SL" if prior_sl > position.take_profit_price else "SMART_STEP_UP_FLOOR_SL"
                return self._make_signal(position, eval_dt, prior_sl, reason)

            if bar.high > position.high_water_mark:
                position.high_water_mark = bar.high
                raw_trailing = position.high_water_mark * (1.0 - self.trailing_buffer_pct)
                trailing_sl = round_to_krx_tick(raw_trailing, side="SELL", round_mode="floor")
                position.stop_loss_price = max(position.stop_loss_price, position.take_profit_price, trailing_sl)

                if bar.close <= position.stop_loss_price:
                    return self._make_signal(position, eval_dt, bar.close, "SMART_STEP_UP_FLOOR_REVERSAL")
                return None

            if bar.low <= position.stop_loss_price:
                reason = "SMART_STEP_UP_TRAILING_SL" if position.stop_loss_price > position.take_profit_price else "SMART_STEP_UP_FLOOR_SL"
                return self._make_signal(position, eval_dt, position.stop_loss_price, reason)

            return None

        # 5. If NOT yet locked at TP: Evaluate SL, TP, or Ambiguous Dual Breach
        hit_tp = bar.high >= position.take_profit_price
        hit_sl = bar.low <= position.stop_loss_price

        # Dual breach on the same bar
        if hit_tp and hit_sl:
            if self.ambiguity_mode == "pessimistic":
                return self._make_signal(position, eval_dt, position.stop_loss_price, "STOP_LOSS_AMBIGUOUS")
            else:
                position.metadata["locked_at_tp"] = True
                position.high_water_mark = max(position.high_water_mark, bar.high)
                raw_trailing = position.high_water_mark * (1.0 - self.trailing_buffer_pct)
                trailing_sl = round_to_krx_tick(raw_trailing, side="SELL", round_mode="floor")
                position.stop_loss_price = max(position.stop_loss_price, position.take_profit_price, trailing_sl)
                return None

        # Stop loss triggered before TP
        if hit_sl:
            return self._make_signal(position, eval_dt, position.stop_loss_price, "STOP_LOSS")

        # Take profit triggered for the first time
        if hit_tp:
            position.metadata["locked_at_tp"] = True
            position.high_water_mark = max(position.high_water_mark, bar.high)
            raw_trailing = position.high_water_mark * (1.0 - self.trailing_buffer_pct)
            trailing_sl = round_to_krx_tick(raw_trailing, side="SELL", round_mode="floor")
            position.stop_loss_price = max(position.stop_loss_price, position.take_profit_price, trailing_sl)

            if bar.close <= position.stop_loss_price:
                return self._make_signal(position, eval_dt, bar.close, "SMART_STEP_UP_FLOOR_REVERSAL")
            return None

        return None
