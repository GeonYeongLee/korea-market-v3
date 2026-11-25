"""
Dynamic Leader Continuous High-Water-Mark Trailing Stop Exit Policy.

Enhances MorningLeaderExit by capturing post-exit MFE alpha on Super Leaders while strictly enforcing:
  1. Continuous High-Water-Mark Trailing Stop:
     - Promoted to Super Leader: initial SL locked at step2_price (+3.5%).
     - As price advances to new peak (High-Water-Mark):
       - trail_sl = round_to_krx_tick(peak * (1.0 - trailing_buffer_pct), side="SELL", round_mode="floor")
       - default trailing_buffer_pct = 0.015 (-1.5% buffer from peak).
       - Minimum SL floor: at least avg_price * (1.0 + step_up_pct) (+3.5%).
     - Monotonicity guarantee: position.stop_loss_price = max(position.stop_loss_price, min_floor, trail_sl).
  2. Fallback Stop Execution Realism:
     - When bar.close < tp_price, target_price is bar.close (the candle's actual closing price).
  3. Adaptive Holding Bars for Super Leader:
     - Default step_up_holding_bars = 3 (expanded from 2 to 3 bars to allow 2nd wave expansion).
     - Optional trend_extension: if on bar 3 the candle closes green (close > open),
       grant 1 additional holding bar (up to max 4 bars) to ride momentum.
  4. All existing invariants preserved:
     - Phase 1: -2.0% Emergency SL.
     - Phase 2: Reference Bar Body Center SL.
     - Hard Timeout: 15:21 liquidation.
     - Ambiguity Mode: pessimistic / optimistic supported.
"""
from __future__ import annotations

from datetime import time
from typing import Optional, List, Tuple

from core.krx_ticks import round_to_krx_tick
from engine.models import MinuteBar, TickData, Position, ExitSignal, OrderType
from strategy.exits.morning_leader_exit import MorningLeaderExit


class DynamicLeaderExit(MorningLeaderExit):
    """
    Dynamic Leader Continuous High-Water-Mark Trailing Stop Exit Policy.
    Lego-block modular exit for capturing Super Leader momentum alpha.
    """

    name: str = "dynamic_leader_exit"

    def __init__(
        self,
        emergency_sl_pct: float = -0.020,        # Phase 1: -2.0% Emergency SL
        tp_target_pct: float = 0.030,            # 1st TP trigger threshold +3.0%
        step_up_pct: float = 0.035,              # Super Leader promotion threshold +3.5%
        step_up_holding_bars: int = 3,           # Super Leader holding bars (default: 3 bars)
        trailing_buffer_pct: float = 0.015,      # Continuous HWM trailing buffer (default: -1.5%)
        trend_extension: bool = True,            # Adaptive holding extension on green bar 3
        max_extended_bars: int = 4,              # Maximum holding bars when extended (default: 4 bars)
        step_up_enabled: bool = True,            # Enable Super Leader promotion & holding
        volume_cliff_threshold: float = 0.40,    # Volume retention threshold (Bar 1)
        volume_cliff_drop_pct: float = 0.010,    # Drop threshold from entry
        volume_cliff_enabled: bool = False,      # Volume cliff early exit toggle
        hard_timeout: time = time(15, 21),       # 15:21 Hard timeout liquidation
        ambiguity_mode: str = "pessimistic",     # "pessimistic" (SL first) or "optimistic"
        use_dynamic_targets: bool = True,        # ATR-based dynamic TP/SL parameterization
    ):
        super().__init__(
            emergency_sl_pct=emergency_sl_pct,
            tp_target_pct=tp_target_pct,
            step_up_pct=step_up_pct,
            step_up_holding_bars=step_up_holding_bars,
            step_up_enabled=step_up_enabled,
            volume_cliff_threshold=volume_cliff_threshold,
            volume_cliff_drop_pct=volume_cliff_drop_pct,
            volume_cliff_enabled=volume_cliff_enabled,
            hard_timeout=hard_timeout,
            ambiguity_mode=ambiguity_mode,
            use_dynamic_targets=use_dynamic_targets,
        )
        self.trailing_buffer_pct = trailing_buffer_pct
        self.trend_extension = trend_extension
        self.max_extended_bars = max_extended_bars

    def on_position_opened(self, position: Position) -> None:
        super().on_position_opened(position)
        position.metadata["trend_extended"] = False

    def _update_trailing_ratchet(self, position: Position, current_high: float) -> None:
        """
        Monotonically elevates Super Leader stop loss via continuous High-Water-Mark trailing stop.
        Formula:
          peak = max(high_water_mark, current_high)
          trail_sl = round_to_krx_tick(peak * (1.0 - trailing_buffer_pct), side="SELL", round_mode="floor")
          min_floor = round_to_krx_tick(position.avg_price * (1.0 + step_up_pct), side="SELL", round_mode="floor")
          position.stop_loss_price = max(position.stop_loss_price, min_floor, trail_sl)
        Ensures position.stop_loss_price is only ever increased, never regressed.
        """
        if not position.metadata.get("is_super_leader", False):
            return

        peak = max(getattr(position, "high_water_mark", position.avg_price), current_high)
        trail_sl = round_to_krx_tick(
            peak * (1.0 - self.trailing_buffer_pct),
            side="SELL",
            round_mode="floor",
        )
        eff_step_up_pct = position.metadata.get("step_up_pct", self.step_up_pct)
        min_floor = round_to_krx_tick(
            position.avg_price * (1.0 + eff_step_up_pct),
            side="SELL",
            round_mode="floor",
        )
        position.stop_loss_price = max(position.stop_loss_price, min_floor, trail_sl)

    def _evaluate_take_profit(
        self,
        position: Position,
        bar: Optional[MinuteBar],
        tick: Optional[TickData],
        current_high: float,
        current_dt: any,
    ) -> Optional[ExitSignal]:
        """Evaluates smart step-up take profit and dynamic leader holding / ratchet conditions."""
        tp_price = position.take_profit_price
        if tp_price <= 0.0:
            eff_tp_pct = position.metadata.get("tp_target_pct", self.tp_target_pct)
            tp_price = round_to_krx_tick(
                position.avg_price * (1.0 + eff_tp_pct),
                side="SELL",
                round_mode="ceil",
            )
            position.take_profit_price = tp_price

        # Check if +3.0% TP threshold is touched
        if current_high >= tp_price:
            position.metadata["tp_triggered"] = True

        # Super Leader holding mode
        if position.metadata.get("is_super_leader", False):
            # Update multi-tier dynamic trailing ratchet
            self._update_trailing_ratchet(position, current_high)

            if bar is not None and position.entry_dt is not None and bar.dt > position.entry_dt:
                step_up_bars = position.metadata.get("step_up_bars_held", 0)

                # Adaptive Trend Extension on holding bar 3
                if self.trend_extension and step_up_bars == self.step_up_holding_bars:
                    if not position.metadata.get("trend_extended", False):
                        if bar.close > bar.open:
                            position.metadata["trend_extended"] = True
                            return None  # Momentum ride: grant 1 additional holding bar

                effective_max_bars = (
                    self.max_extended_bars
                    if (self.trend_extension and position.metadata.get("trend_extended", False))
                    else self.step_up_holding_bars
                )

                if step_up_bars >= effective_max_bars:
                    return ExitSignal(
                        symbol=position.symbol,
                        dt=current_dt,
                        order_type=OrderType.MARKET,
                        target_price=bar.close,
                        reason="SMART_STEP_UP_MATURED_EXIT",
                    )

            # Intra-bar or close drop below updated trailing SL
            if bar is not None and bar.close <= position.stop_loss_price:
                return ExitSignal(
                    symbol=position.symbol,
                    dt=current_dt,
                    order_type=OrderType.MARKET,
                    target_price=position.stop_loss_price,
                    reason="SMART_STEP_UP_TRAILING_SL",
                )

            return None

        # 1-Minute Candle Close TP / Super Leader Promotion
        if position.metadata.get("tp_triggered", False):
            # Intra-bar fallback stop if price dropped back below +3.0% during tick feed
            if tick is not None and tick.price < tp_price:
                return ExitSignal(
                    symbol=position.symbol,
                    dt=current_dt,
                    order_type=OrderType.MARKET,
                    target_price=tick.price,
                    reason="REALTIME_TP_FALLBACK_STOP",
                )

            # Candle close evaluation in bar mode
            if bar is not None:
                eff_step_up_pct = position.metadata.get("step_up_pct", self.step_up_pct)
                step2_price = round_to_krx_tick(
                    position.avg_price * (1.0 + eff_step_up_pct),
                    side="SELL",
                    round_mode="ceil",
                )

                # Fallback stop if price dropped back below +3.0% (Realistic: bar.close execution)
                if bar.close < tp_price:
                    return ExitSignal(
                        symbol=position.symbol,
                        dt=current_dt,
                        order_type=OrderType.MARKET,
                        target_price=bar.close,
                        reason="REALTIME_TP_FALLBACK_STOP",
                    )

                # Normal +3.0% close exit if close did not reach +3.5% (or if step-up holding is disabled)
                if not self.step_up_enabled or bar.close < step2_price:
                    return ExitSignal(
                        symbol=position.symbol,
                        dt=current_dt,
                        order_type=OrderType.MARKET,
                        target_price=bar.close,
                        reason="SMART_TP_CLOSE_1M_NORMAL",
                    )

                # Promoted to Super Leader!
                position.metadata["is_super_leader"] = True
                position.stop_loss_price = max(position.stop_loss_price, step2_price)
                position.metadata["step_up_bars_held"] = 0
                position.metadata["step_up_promoted_dt"] = bar.dt
                position.metadata["trend_extended"] = False
                self._update_trailing_ratchet(position, current_high)

        return None
