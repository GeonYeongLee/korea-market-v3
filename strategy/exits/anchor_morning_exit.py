"""
Anchor Morning Exit Policy.
===========================
Anchor Morning Exit Policy with Conditional 08:00 NXT Early Exit & 09:00 Open Liquidation.
Designed for Anchor Close Betting (Overnight Continuation):
  1. Conditional 08:00 NXT Early Take-Profit (+3.0%):
     - In the 08:00~08:50 NXT Pre-market window, if price surges >= +3.0%, captures profit immediately.
     - Never cuts loss at 08:00 (prevents undershooting shakeout).
  2. 09:00 KRX Regular Market Open Liquidation:
     - Liquidates at 09:00 open auction with 0% slippage.
  3. Fallback Smart Step-Up & 15:20 EOD Hard Timeout:
     - Guaranteed 15:20 hard timeout liquidation invariant.
     - Strict ratchet SL monotonicity and pessimistic ambiguous bar defense.
"""
from __future__ import annotations

import logging
from datetime import date, datetime, time
from typing import Any, Optional

from core.krx_ticks import round_to_krx_tick
from engine.models import ExitSignal, MinuteBar, OrderType, Position, TickData
from strategy.base import IExitPolicy

logger = logging.getLogger("korea_market.strategy.exit.anchor_morning")


class AnchorMorningExit(IExitPolicy):
    """
    Morning Smart Step-Up & Open Liquidation Exit Policy for Anchor Close Betting.

    Handles overnight position lifecycle including:
      - 08:00~08:50 NXT pre-market conditional early profit taking (+3.0%).
      - 09:00:00 KRX regular market opening auction liquidation (0% slippage).
      - Step-up trailing ratchet stop-loss protection and pessimistic ambiguous bar resolution.
      - 15:20:00 EOD hard timeout guarantee.
    """

    name: str = "anchor_morning_exit"

    def __init__(
        self,
        exit_at_open: bool = False,
        enable_nxt_exit: bool = False,
        nxt_tp_pct: float = 0.030,
        nxt_start_time: time = time(8, 0),
        nxt_end_time: time = time(8, 50),
        open_exit_time: time = time(9, 0),
        tp_target_pct: float = 0.030,
        trailing_buffer_pct: float = 0.010,
        emergency_sl_pct: float = -0.025,
        morning_cutoff: Optional[time] = time(9, 30),
        hard_timeout: time = time(15, 20),
        ambiguity_mode: str = "pessimistic",
        **kwargs: Any,
    ) -> None:
        """
        Initializes Anchor Morning Exit policy.

        Args:
            exit_at_open: If True, liquidates overnight position at 09:00 open auction.
            enable_nxt_exit: If True, enables conditional 08:00 NXT early profit taking.
            nxt_tp_pct: Profit lock threshold for NXT early exit (+3.0%).
            nxt_start_time: NXT pre-market evaluation start time (08:00:00).
            nxt_end_time: NXT pre-market evaluation end time (08:50:00).
            open_exit_time: KRX regular market open liquidation time (09:00:00).
            tp_target_pct: Take-profit target percentage for step-up trailing (+3.0%).
            trailing_buffer_pct: Distance buffer below peak price for trailing stop (-1.0%).
            emergency_sl_pct: Pre-TP emergency stop-loss floor percentage (-2.5%).
            morning_cutoff: Optional profit harvest time cutoff on D+1 (default: 09:30:00).
            hard_timeout: EOD hard timeout liquidation time (15:20:00).
            ambiguity_mode: Dual SL/TP breach resolution ('pessimistic' or 'optimistic').
        """
        self.exit_at_open = exit_at_open
        self.enable_nxt_exit = enable_nxt_exit
        self.nxt_tp_pct = nxt_tp_pct
        self.nxt_start_time = nxt_start_time
        self.nxt_end_time = nxt_end_time
        self.open_exit_time = open_exit_time
        self.tp_target_pct = tp_target_pct
        self.trailing_buffer_pct = trailing_buffer_pct
        self.emergency_sl_pct = emergency_sl_pct
        self.morning_cutoff = morning_cutoff
        self.hard_timeout = hard_timeout
        self.ambiguity_mode = ambiguity_mode

    @staticmethod
    def _make_signal(position: Position, dt: datetime, price: float, reason: str) -> ExitSignal:
        """Constructs a standardized MARKET exit signal."""
        return ExitSignal(
            symbol=position.symbol,
            dt=dt,
            order_type=OrderType.MARKET,
            target_price=price,
            reason=reason,
        )

    def on_position_opened(self, position: Position) -> None:
        """
        Initializes initial stop-loss and take-profit thresholds upon position entry.

        Sets phase 1, initializes high water mark to average fill price, and calculates
        the initial emergency stop-loss floor (-2.5%) and first take-profit target (+3.0%).
        """
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
        """Session open lifecycle hook."""
        pass

    def evaluate_exit(
        self,
        position: Position,
        bar: Optional[MinuteBar],
        tick: Optional[TickData] = None,
    ) -> Optional[ExitSignal]:
        """
        Evaluates exit conditions against incoming bar or tick data.

        Returns:
            ExitSignal if an exit condition is met, otherwise None.
        """
        eval_dt = tick.dt if tick else (bar.dt if bar else datetime.now())
        eval_date = eval_dt.date()
        eval_time = eval_dt.time()

        b_open = tick.price if tick else (bar.open if bar else position.current_price)
        b_high = tick.price if tick else (bar.high if bar else position.current_price)
        b_low = tick.price if tick else (bar.low if bar else position.current_price)
        b_close = tick.price if tick else (bar.close if bar else position.current_price)

        # 1. Hard Timeout Liquidation (15:20:00)
        # For positions bought at 15:20 close betting, do not timeout on the entry day itself
        is_close_entry_day = (
            position.entry_dt is not None
            and eval_date == position.entry_dt.date()
            and position.entry_dt.time() >= time(15, 0)
        )
        if not is_close_entry_day and eval_time >= self.hard_timeout:
            return self._make_signal(position, eval_dt, b_close, "TIMEOUT_1520")

        is_overnight = (
            position.entry_dt is not None
            and eval_date > position.entry_dt.date()
        )

        # 2. Conditional 08:00 NXT Pre-Market Window (08:00 <= eval_time < 08:50)
        if is_overnight and (self.nxt_start_time <= eval_time < self.nxt_end_time):
            if self.enable_nxt_exit:
                target_nxt_tp = position.avg_price * (1.0 + self.nxt_tp_pct)
                if b_high >= target_nxt_tp:
                    exec_price = round_to_krx_tick(target_nxt_tp, side="SELL", round_mode="floor")
                    return self._make_signal(position, eval_dt, exec_price, "NXT_0800_EARLY_TAKE_PROFIT")
            # In pre-market, NEVER trigger stop loss (strictly eliminates illiquid undershooting shakeout)
            return None

        # 3. 09:00 KRX Open Liquidation
        if is_overnight and self.exit_at_open and eval_time >= self.open_exit_time:
            return self._make_signal(position, eval_dt, b_open, "KRX_0900_OPEN_EXIT")

        # 4. Morning Cutoff (09:30:00 on D+1, if position is carried overnight and step-up mode is active)
        if self.morning_cutoff is not None and is_overnight and eval_time >= self.morning_cutoff:
            return self._make_signal(position, eval_dt, b_close, "MORNING_CUTOFF_0930")

        # 5. Phase 1 -> Phase 2 Transition (Subsequent Bar 1+)
        is_subsequent = position.entry_dt is not None and eval_dt > position.entry_dt
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

        # 6. If ALREADY Locked at TP: Causal Trailing Stop Regime
        if was_locked:
            if b_low <= prior_sl:
                reason = "SMART_STEP_UP_TRAILING_SL" if prior_sl > position.take_profit_price else "SMART_STEP_UP_FLOOR_SL"
                return self._make_signal(position, eval_dt, prior_sl, reason)

            if b_high > position.high_water_mark:
                position.high_water_mark = b_high
                raw_trailing = position.high_water_mark * (1.0 - self.trailing_buffer_pct)
                trailing_sl = round_to_krx_tick(raw_trailing, side="SELL", round_mode="floor")
                position.stop_loss_price = max(position.stop_loss_price, position.take_profit_price, trailing_sl)

                if b_close <= position.stop_loss_price:
                    return self._make_signal(position, eval_dt, b_close, "SMART_STEP_UP_FLOOR_REVERSAL")
                return None

            if b_low <= position.stop_loss_price:
                reason = "SMART_STEP_UP_TRAILING_SL" if position.stop_loss_price > position.take_profit_price else "SMART_STEP_UP_FLOOR_SL"
                return self._make_signal(position, eval_dt, position.stop_loss_price, reason)

            return None

        # 7. If NOT yet locked at TP: Evaluate SL, TP, or Ambiguous Dual Breach
        hit_tp = b_high >= position.take_profit_price
        hit_sl = b_low <= position.stop_loss_price

        # Dual breach on the same bar
        if hit_tp and hit_sl:
            if self.ambiguity_mode == "pessimistic":
                return self._make_signal(position, eval_dt, position.stop_loss_price, "STOP_LOSS_AMBIGUOUS")
            else:
                position.metadata["locked_at_tp"] = True
                position.high_water_mark = max(position.high_water_mark, b_high)
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
            position.high_water_mark = max(position.high_water_mark, b_high)
            raw_trailing = position.high_water_mark * (1.0 - self.trailing_buffer_pct)
            trailing_sl = round_to_krx_tick(raw_trailing, side="SELL", round_mode="floor")
            position.stop_loss_price = max(position.stop_loss_price, position.take_profit_price, trailing_sl)

            if b_close <= position.stop_loss_price:
                return self._make_signal(position, eval_dt, b_close, "SMART_STEP_UP_FLOOR_REVERSAL")
            return None

        return None


class AnchorConditionalMorningExit(AnchorMorningExit):
    """
    Production Champion Exit Policy.

    Features:
      - 08:00~08:50 NXT Pre-Market Conditional Early Take-Profit (>= +3.0%)
      - 09:00 KRX Regular Market Open Liquidation
      - Strict 15:20 EOD Hard Timeout Liquidation
    """

    name: str = "anchor_conditional_morning_exit"

    def __init__(
        self,
        exit_at_open: bool = True,
        enable_nxt_exit: bool = True,
        nxt_tp_pct: float = 0.030,
        nxt_start_time: time = time(8, 0),
        nxt_end_time: time = time(8, 50),
        open_exit_time: time = time(9, 0),
        hard_timeout: time = time(15, 20),
        **kwargs: Any,
    ) -> None:
        """
        Initializes Champion B Anchor Conditional Morning Exit policy.

        Args:
            exit_at_open: Defaults to True for 09:00 open liquidation.
            enable_nxt_exit: Defaults to True for 08:00 NXT early exit.
            nxt_tp_pct: Profit target for early NXT exit (+3.0%).
            nxt_start_time: NXT pre-market start time (08:00:00).
            nxt_end_time: NXT pre-market end time (08:50:00).
            open_exit_time: Regular session opening auction liquidation (09:00:00).
            hard_timeout: End-of-day hard timeout liquidation (15:20:00).
        """
        super().__init__(
            exit_at_open=exit_at_open,
            enable_nxt_exit=enable_nxt_exit,
            nxt_tp_pct=nxt_tp_pct,
            nxt_start_time=nxt_start_time,
            nxt_end_time=nxt_end_time,
            open_exit_time=open_exit_time,
            hard_timeout=hard_timeout,
            **kwargs,
        )
