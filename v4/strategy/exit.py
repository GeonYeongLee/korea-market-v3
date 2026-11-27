"""
All-Weather Quad-Layer Exit Policy for Korea Market v4.
Enforces Bar 0 Phase 1 Emergency SL isolation, monotonic ratchet SL,
Smart Step-Up Super Leader holding, and 15:21 Hard Timeout liquidation.
"""
from __future__ import annotations

from datetime import date, datetime, time
from typing import Any, Dict, Optional

from core.krx_ticks import round_to_krx_tick
from v4.interfaces import IExitComponent
from v4.models import ExecutionMode, ExitSignal, MinuteBar, OrderType, Position, TickEvent


def make_ref_bar_dict(bar: MinuteBar, prior_atr20_pct: Optional[float] = None) -> Dict[str, Any]:
    """Builds standardized Bar 0 reference metadata dict for Phase 2 center-SL & volume-cliff."""
    return {
        "open": bar.open,
        "high": bar.high,
        "low": bar.low,
        "close": bar.close,
        "dt": bar.dt,
        "turnover": bar.turnover,
        "volume": bar.volume,
        "prior_atr20_pct": prior_atr20_pct,
    }


class AllWeatherExitV4(IExitComponent):
    """
    Quad-layer exit policy with strict Bar 0 Phase 1 isolation and Dual-Bounds support.
    """

    name: str = "all_weather_exit"

    def __init__(
        self,
        emergency_sl_pct: float = -0.020,
        tp_target_pct: float = 0.030,
        step_up_pct: float = 0.035,
        step_up_holding_bars: int = 2,
        step_up_enabled: bool = True,
        volume_cliff_threshold: float = 0.40,
        volume_cliff_drop_pct: float = 0.010,
        volume_cliff_enabled: bool = False,
        hard_timeout: time = time(15, 21),
        execution_mode: ExecutionMode = "pessimistic",
        ambiguity_mode: Optional[ExecutionMode] = None,
        use_dynamic_targets: bool = True,
        enable_bar0_bar_sl: bool = True,
    ) -> None:
        self.emergency_sl_pct = emergency_sl_pct
        self.tp_target_pct = tp_target_pct
        self.step_up_pct = step_up_pct
        self.step_up_holding_bars = step_up_holding_bars
        self.step_up_enabled = step_up_enabled and (step_up_holding_bars > 0)
        self.volume_cliff_threshold = volume_cliff_threshold
        self.volume_cliff_drop_pct = volume_cliff_drop_pct
        self.volume_cliff_enabled = volume_cliff_enabled
        self.base_hard_timeout = hard_timeout
        self.hard_timeout = hard_timeout
        self.execution_mode: ExecutionMode = ambiguity_mode if ambiguity_mode is not None else execution_mode
        self.use_dynamic_targets = use_dynamic_targets
        self.enable_bar0_bar_sl = enable_bar0_bar_sl

    @staticmethod
    def _make_signal(position: Position, dt: datetime, price: float, reason: str) -> ExitSignal:
        return ExitSignal(
            symbol=position.symbol,
            dt=dt,
            order_type=OrderType.MARKET,
            target_price=price,
            reason=reason,
        )

    def on_day_open(
        self,
        current_date: date,
        market_open_time: time = time(9, 0),
        market_close_time: time = time(15, 30),
        execution_mode: Optional[ExecutionMode] = None,
    ) -> None:
        if execution_mode is not None:
            self.execution_mode = execution_mode
        self.hard_timeout = min(self.base_hard_timeout, market_close_time)

    def on_position_opened(self, position: Position) -> None:
        position.phase = 1
        prior_atr = position.metadata.get("prior_atr20_pct")
        if self.use_dynamic_targets and prior_atr is not None and prior_atr > 0.0:
            eff_tp = min(0.045, max(0.020, 0.35 * prior_atr))
            eff_step_up = eff_tp + 0.005
            eff_sl = -min(0.025, max(0.015, 0.22 * prior_atr))
        else:
            eff_tp = self.tp_target_pct
            eff_step_up = self.step_up_pct
            eff_sl = self.emergency_sl_pct

        position.metadata["tp_target_pct"] = eff_tp
        position.metadata["step_up_pct"] = eff_step_up
        position.metadata["emergency_sl_pct"] = eff_sl

        position.stop_loss_price = round_to_krx_tick(position.avg_price * (1.0 + eff_sl), side="SELL", round_mode="ceil")
        position.take_profit_price = round_to_krx_tick(position.avg_price * (1.0 + eff_tp), side="SELL", round_mode="ceil")
        position.high_water_mark = position.avg_price
        position.metadata["bars_held"] = 0
        position.metadata["is_super_leader"] = False
        position.metadata["step_up_bars_held"] = 0
        position.metadata["tp_triggered"] = False
        position.metadata["step_up_promoted_dt"] = None
        position.metadata["last_bar_dt"] = None

    def apply_fill_targets(self, position: Position) -> None:
        """Initializes or ratchets SL/TP targets after initial or partial fill avg_price update."""
        if position.stop_loss_price <= 0.0:
            self.on_position_opened(position)
            return
        eff_sl = position.metadata.get("emergency_sl_pct", self.emergency_sl_pct)
        eff_tp = position.metadata.get("tp_target_pct", self.tp_target_pct)
        new_sl = round_to_krx_tick(position.avg_price * (1.0 + eff_sl), side="SELL", round_mode="ceil")
        position.stop_loss_price = max(position.stop_loss_price, new_sl)
        position.take_profit_price = round_to_krx_tick(position.avg_price * (1.0 + eff_tp), side="SELL", round_mode="ceil")

    def elevate_to_phase2(self, position: Position, ref_bar: Optional[MinuteBar] = None) -> None:
        """Syncs completed Bar 0 ref_bar metadata and elevates Phase 1 -> Phase 2 monotonically."""
        if ref_bar is not None:
            position.metadata["ref_bar"] = make_ref_bar_dict(ref_bar, position.metadata.get("prior_atr20_pct"))
        if position.phase != 1:
            return
        position.phase = 2
        eff_emergency_sl = abs(position.metadata.get("emergency_sl_pct", self.emergency_sl_pct))
        floor_sl = round_to_krx_tick(position.avg_price * (1.0 - eff_emergency_sl), side="SELL", round_mode="ceil")
        rb = position.metadata.get("ref_bar") or {}
        if rb.get("open") is not None and rb.get("close") is not None:
            center_sl = round_to_krx_tick((rb["open"] + rb["close"]) / 2.0, side="SELL", round_mode="ceil")
            target_sl = max(center_sl, floor_sl)
        else:
            target_sl = floor_sl
        position.stop_loss_price = max(position.stop_loss_price, target_sl)

    def _evaluate_stop_loss(self, position: Position, current_low: float, current_dt: datetime) -> Optional[ExitSignal]:
        if position.stop_loss_price <= 0.0 or current_low > position.stop_loss_price:
            return None
        if position.phase == 1:
            return self._make_signal(position, current_dt, position.stop_loss_price, "GRACE_EMERGENCY_SL")
        if position.metadata.get("is_super_leader", False):
            promoted_dt = position.metadata.get("step_up_promoted_dt")
            if promoted_dt is not None and current_dt <= promoted_dt:
                return None
            return self._make_signal(position, current_dt, position.stop_loss_price, "SMART_STEP_UP_TRAILING_SL")
        return self._make_signal(position, current_dt, position.stop_loss_price, "LOCKED_SL")

    def _evaluate_take_profit(
        self,
        position: Position,
        bar: Optional[MinuteBar],
        current_high: float,
        current_dt: datetime,
    ) -> Optional[ExitSignal]:
        tp_price = position.take_profit_price
        if tp_price <= 0.0:
            tp_price = round_to_krx_tick(position.avg_price * (1.0 + self.tp_target_pct), side="SELL", round_mode="ceil")
            position.take_profit_price = tp_price

        if current_high >= tp_price:
            position.metadata["tp_triggered"] = True

        if position.metadata.get("is_super_leader", False):
            if bar is not None and position.entry_dt is not None and bar.dt > position.entry_dt:
                if position.metadata.get("step_up_bars_held", 0) >= self.step_up_holding_bars:
                    return self._make_signal(position, current_dt, bar.close, "SMART_STEP_UP_MATURED_EXIT")
            return None

        if position.metadata.get("tp_triggered", False) and bar is not None:
            eff_step_up_pct = position.metadata.get("step_up_pct", self.step_up_pct)
            step2_price = round_to_krx_tick(position.avg_price * (1.0 + eff_step_up_pct), side="SELL", round_mode="ceil")
            if bar.close < tp_price:
                return self._make_signal(position, current_dt, bar.close, "REALTIME_TP_FALLBACK_STOP")
            if not self.step_up_enabled or bar.close < step2_price:
                return self._make_signal(position, current_dt, bar.close, "SMART_TP_CLOSE_1M_NORMAL")
            position.metadata["is_super_leader"] = True
            position.stop_loss_price = max(position.stop_loss_price, step2_price)
            position.metadata["step_up_bars_held"] = 0
            position.metadata["step_up_promoted_dt"] = bar.dt
        return None

    def evaluate_exit(
        self,
        position: Position,
        bar: Optional[MinuteBar] = None,
        tick: Optional[TickEvent] = None,
    ) -> Optional[ExitSignal]:
        current_price = tick.price if tick is not None else (bar.close if bar is not None else position.current_price)
        current_low = tick.price if tick is not None else (bar.low if bar is not None else position.current_price)
        current_high = tick.price if tick is not None else (bar.high if bar is not None else position.current_price)
        current_dt = tick.dt if tick is not None else (bar.dt if bar is not None else position.entry_dt)
        if not current_dt:
            return None

        # 1. Hard Timeout Check (always active across Phase 1 and Phase 2+)
        if current_dt.time() >= self.hard_timeout:
            return self._make_signal(position, current_dt, current_price, "HARD_TIMEOUT_1521")

        # 2. Subsequent bar counter & standalone Phase 1 -> Phase 2 elevation (for audit/ callers)
        is_subsequent = (
            bar is not None
            and position.entry_dt is not None
            and bar.dt.replace(second=0, microsecond=0) > position.entry_dt.replace(second=0, microsecond=0)
        )
        if is_subsequent:
            if bar.dt != position.metadata.get("last_bar_dt"):
                position.metadata["bars_held"] = position.metadata.get("bars_held", 0) + 1
                if position.metadata.get("is_super_leader", False):
                    position.metadata["step_up_bars_held"] = position.metadata.get("step_up_bars_held", 0) + 1
                position.metadata["last_bar_dt"] = bar.dt
            if position.phase == 1:
                self.elevate_to_phase2(position)

        # 3. CRITICAL BAR 0 INVARIANT: Phase 1 evaluates GRACE_EMERGENCY_SL ONLY (never TP / center SL)
        if position.phase == 1:
            if tick is not None and current_high > position.high_water_mark:
                position.high_water_mark = current_high
            if self.execution_mode == "optimistic":
                return None
            if bar is not None and tick is None:
                if not self.enable_bar0_bar_sl:
                    return None
                check_price = bar.low if bar.open > position.stop_loss_price else bar.close
                return self._evaluate_stop_loss(position, check_price, current_dt)
            return self._evaluate_stop_loss(position, current_low, current_dt)

        if current_high > position.high_water_mark:
            position.high_water_mark = current_high

        # 4. Optional Phase 3 Volume Cliff (Bar 1 only)
        if self.volume_cliff_enabled and bar is not None and position.phase == 2 and position.metadata.get("bars_held") == 1:
            ref_to = (position.metadata.get("ref_bar") or {}).get("turnover", 0.0)
            if ref_to > 0.0:
                drop_price = round_to_krx_tick(position.avg_price * (1.0 - self.volume_cliff_drop_pct), side="SELL", round_mode="floor")
                if (bar.turnover / ref_to) < self.volume_cliff_threshold and bar.close <= drop_price:
                    return self._make_signal(position, current_dt, bar.close, "VOLUME_CLIFF_EARLY_EXIT")

        # 5. Phase 2+ Dual-Bounds Ambiguity Resolution
        if self.execution_mode == "optimistic":
            return self._evaluate_take_profit(position, bar, current_high, current_dt) or self._evaluate_stop_loss(
                position, current_low, current_dt
            )
        return self._evaluate_stop_loss(position, current_low, current_dt) or self._evaluate_take_profit(
            position, bar, current_high, current_dt
        )
