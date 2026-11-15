"""
Morning Leader Quad-Layer Exit Policy.
Enforces:
  Phase 1: Emergency SL (GRACE_EMERGENCY_SL).
  Phase 2: Ref candle body center SL with dynamic floor (Ratchet monotonic).
  Phase 3: Bar 1 Volume Cliff defense.
  Phase 4: Smart Step-Up TP & Super Leader Promotion.
  Phase 5: Hard Timeout 15:21:00 liquidation. Zero overnight.
"""
from __future__ import annotations

from datetime import date, datetime, time
from typing import Optional
import numpy as np

from core.krx_ticks import round_to_krx_tick
from core.telemetry import telemetry
from engine.models import MinuteBar, TickData, Position, ExitSignal, OrderType
from strategy.base import IExitPolicy


class MorningLeaderExit(IExitPolicy):
    """
    Quad-Layer exit management policy for Morning Rank Leader Strategy.
    """

    name: str = "morning_leader_exit"

    def __init__(
        self,
        emergency_sl_pct: float = -0.020,        # Phase 1: -2.0% 비상 손절
        tp_target_pct: float = 0.030,            # 1차 익절 감시선 +3.0%
        step_up_pct: float = 0.035,              # 슈퍼 리더 승격 기준 +3.5%
        step_up_holding_bars: int = 2,           # 슈퍼 리더 승격 후 홀딩 분봉 수 (기본: 2분봉)
        step_up_enabled: bool = True,            # 스마트 스텝업 활성화 (기본: True)
        volume_cliff_threshold: float = 0.40,    # 거래대금 유지율 40% 미만
        volume_cliff_drop_pct: float = 0.010,    # 진입가 대비 -1.0% 이하 하락
        volume_cliff_enabled: bool = False,      # Bar 0 Body Midpoint SL이 핵심 지지선 역할 (기본 비활성화)
        hard_timeout: time = time(15, 21),       # 15:21 당일 전량 청산
        ambiguity_mode: str = "pessimistic",     # "pessimistic" (SL 우선) or "optimistic"
        use_dynamic_targets: bool = True,        # ATR 기반 동적 TP/SL 변수화
    ):
        self.emergency_sl_pct = emergency_sl_pct
        self.tp_target_pct = tp_target_pct
        self.step_up_pct = step_up_pct
        self.step_up_holding_bars = step_up_holding_bars
        self.step_up_enabled = step_up_enabled and (step_up_holding_bars > 0)
        self.volume_cliff_threshold = volume_cliff_threshold
        self.volume_cliff_drop_pct = volume_cliff_drop_pct
        self.volume_cliff_enabled = volume_cliff_enabled
        self.hard_timeout = hard_timeout
        self.ambiguity_mode = ambiguity_mode
        self.use_dynamic_targets = use_dynamic_targets

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

        # Dynamic parameterization of TP and Emergency SL
        prior_atr = position.metadata.get("prior_atr20_pct")
        if self.use_dynamic_targets and prior_atr is not None and prior_atr > 0:
            eff_tp = float(np.clip(0.35 * prior_atr, 0.020, 0.045))
            eff_step_up = float(eff_tp + 0.005)
            eff_sl = -float(np.clip(0.22 * prior_atr, 0.015, 0.025))
        else:
            eff_tp = self.tp_target_pct
            eff_step_up = self.step_up_pct
            eff_sl = self.emergency_sl_pct

        position.metadata["tp_target_pct"] = eff_tp
        position.metadata["step_up_pct"] = eff_step_up
        position.metadata["emergency_sl_pct"] = eff_sl

        raw_sl = position.avg_price * (1.0 + eff_sl)
        position.stop_loss_price = round_to_krx_tick(raw_sl, side="SELL", round_mode="floor")

        raw_tp = position.avg_price * (1.0 + eff_tp)
        position.take_profit_price = round_to_krx_tick(raw_tp, side="SELL", round_mode="ceil")

        position.high_water_mark = position.avg_price
        position.metadata["bars_held"] = 0
        position.metadata["is_super_leader"] = False
        position.metadata["step_up_bars_held"] = 0
        position.metadata["tp_triggered"] = False
        position.metadata["step_up_promoted_dt"] = None
        position.metadata["last_bar_dt"] = None

    def on_day_open(self, current_date: date) -> None:
        pass

    def _evaluate_stop_loss(
        self,
        position: Position,
        current_low: float,
        current_dt: datetime,
    ) -> Optional[ExitSignal]:
        """Evaluates stop-loss conditions under current phase."""
        if position.stop_loss_price <= 0.0 or current_low > position.stop_loss_price:
            return None

        # Phase 1: Bar 0 Emergency SL
        if position.phase == 1:
            return self._make_signal(position, current_dt, position.stop_loss_price, "GRACE_EMERGENCY_SL")

        # Phase 2: Super Leader Trailing SL
        if position.metadata.get("is_super_leader", False):
            promoted_dt = position.metadata.get("step_up_promoted_dt")
            if promoted_dt is not None and current_dt <= promoted_dt:
                return None  # Do not stop out inside the promotion candle itself
            return self._make_signal(position, current_dt, position.stop_loss_price, "SMART_STEP_UP_TRAILING_SL")

        # Phase 2: Reference Body Center SL
        return self._make_signal(position, current_dt, position.stop_loss_price, "LOCKED_SL")

    def _evaluate_take_profit(
        self,
        position: Position,
        bar: Optional[MinuteBar],
        tick: Optional[TickData],
        current_high: float,
        current_dt: datetime,
    ) -> Optional[ExitSignal]:
        """Evaluates smart step-up take profit conditions."""
        tp_price = position.take_profit_price
        if tp_price <= 0.0:
            tp_price = round_to_krx_tick(position.avg_price * (1.0 + self.tp_target_pct), side="SELL", round_mode="ceil")
            position.take_profit_price = tp_price

        # Check if +3.0% TP threshold is touched
        if current_high >= tp_price:
            position.metadata["tp_triggered"] = True

        # Super Leader holding mode
        if position.metadata.get("is_super_leader", False):
            if bar is not None and position.entry_dt is not None and bar.dt > position.entry_dt:
                step_up_bars = position.metadata.get("step_up_bars_held", 0)
                if step_up_bars >= self.step_up_holding_bars:
                    return self._make_signal(position, current_dt, bar.close, "SMART_STEP_UP_MATURED_EXIT")
            return None

        # 1-Minute Candle Close TP / Super Leader Promotion
        if position.metadata.get("tp_triggered", False):
            exit_exec_price = tick.price if tick else (bar.close if bar else position.current_price)

            # Intra-bar fallback stop if price dropped back below +3.0% during tick feed
            if tick is not None and tick.price < tp_price:
                return self._make_signal(position, current_dt, exit_exec_price, "REALTIME_TP_FALLBACK_STOP")

            # Candle close evaluation in bar mode
            if bar is not None:
                eff_step_up_pct = position.metadata.get("step_up_pct", self.step_up_pct)
                step2_price = round_to_krx_tick(
                    position.avg_price * (1.0 + eff_step_up_pct),
                    side="SELL",
                    round_mode="ceil",
                )

                # Fallback stop if price dropped back below +3.0%
                if bar.close < tp_price:
                    return self._make_signal(position, current_dt, exit_exec_price, "REALTIME_TP_FALLBACK_STOP")

                # Normal +3.0% close exit if close did not reach +3.5% (or if step-up holding is disabled)
                if not self.step_up_enabled or bar.close < step2_price:
                    return self._make_signal(position, current_dt, bar.close, "SMART_TP_CLOSE_1M_NORMAL")

                # Promoted to Super Leader!
                position.metadata["is_super_leader"] = True
                position.stop_loss_price = max(position.stop_loss_price, step2_price)
                position.metadata["step_up_bars_held"] = 0
                position.metadata["step_up_promoted_dt"] = bar.dt

        return None

    def evaluate_exit(
        self,
        position: Position,
        bar: Optional[MinuteBar] = None,
        tick: Optional[TickData] = None,
    ) -> Optional[ExitSignal]:
        current_price = tick.price if tick else (bar.close if bar else position.current_price)
        current_low = tick.price if tick else (bar.low if bar else position.current_price)
        current_high = tick.price if tick else (bar.high if bar else position.current_price)
        current_dt = tick.dt if tick else (bar.dt if bar else position.entry_dt)
        if not current_dt:
            return None

        if current_high > position.high_water_mark:
            position.high_water_mark = current_high

        # Helper to record exit telemetry before returning
        def _record_exit_eval(decision: str, reason: str, target_p: float) -> None:
            if not telemetry.audit_enabled:
                return
            pnl_pct = round((current_price - position.avg_price) / position.avg_price * 100.0, 2) if position.avg_price > 0 else 0.0
            metrics = {
                "current_price": current_price,
                "avg_price": position.avg_price,
                "hwm": position.high_water_mark,
                "pnl_pct": pnl_pct,
                "target_price": target_p,
                "stop_loss_price": position.stop_loss_price,
                "take_profit_price": position.take_profit_price,
                "bars_held": position.metadata.get("bars_held", 0),
                "is_super_leader": bool(position.metadata.get("is_super_leader", False)),
            }
            telemetry.record_strategy_eval("EXIT", position.symbol, decision, reason, metrics, dt=current_dt)

        # 1. Hard Timeout 15:21 Check
        if current_dt.time() >= self.hard_timeout:
            sig = self._make_signal(position, current_dt, current_price, "HARD_TIMEOUT_1521")
            _record_exit_eval("TRIGGER", "HARD_TIMEOUT_1521", current_price)
            return sig

        # 2. Phase 1 -> Phase 2 Transition (Bar 1+) & Holding Bar Tracker
        is_subsequent = bar is not None and position.entry_dt is not None and bar.dt > position.entry_dt
        if is_subsequent:
            last_dt = position.metadata.get("last_bar_dt")
            if bar.dt != last_dt:
                position.metadata["bars_held"] = position.metadata.get("bars_held", 0) + 1
                if position.metadata.get("is_super_leader", False):
                    position.metadata["step_up_bars_held"] = position.metadata.get("step_up_bars_held", 0) + 1
                position.metadata["last_bar_dt"] = bar.dt

            if position.phase == 1:
                position.phase = 2
                eff_emergency_sl = abs(position.metadata.get("emergency_sl_pct", self.emergency_sl_pct))
                floor_sl = round_to_krx_tick(position.avg_price * (1.0 - eff_emergency_sl), side="SELL", round_mode="ceil")
                ref_bar = position.metadata.get("ref_bar") or {}
                if ref_bar.get("open") is not None and ref_bar.get("close") is not None:
                    ref_mid = (ref_bar["open"] + ref_bar["close"]) / 2.0
                    center_sl = round_to_krx_tick(ref_mid, side="SELL", round_mode="floor")
                    target_sl = max(center_sl, floor_sl)
                else:
                    target_sl = floor_sl
                position.stop_loss_price = max(position.stop_loss_price, target_sl)

        # 3. Phase 3: Volume Cliff Early Exit (Bar 1 only, optional)
        if self.volume_cliff_enabled and bar is not None and position.phase == 2 and position.metadata.get("bars_held") == 1:
            ref_bar = position.metadata.get("ref_bar", {})
            ref_to = ref_bar.get("turnover", 0.0)
            if ref_to > 0.0:
                to_retention = bar.turnover / ref_to
                drop_price = round_to_krx_tick(position.avg_price * (1.0 - self.volume_cliff_drop_pct), side="SELL", round_mode="floor")
                if to_retention < self.volume_cliff_threshold and bar.close <= drop_price:
                    sig = self._make_signal(position, current_dt, bar.close, "VOLUME_CLIFF_EARLY_EXIT")
                    _record_exit_eval("TRIGGER", "VOLUME_CLIFF_EARLY_EXIT", bar.close)
                    return sig

        # 4. Ambiguity Resolution: SL vs TP ordering
        if self.ambiguity_mode == "optimistic":
            sig = (
                self._evaluate_take_profit(position, bar, tick, current_high, current_dt)
                or self._evaluate_stop_loss(position, current_low, current_dt)
            )
        else:
            sig = (
                self._evaluate_stop_loss(position, current_low, current_dt)
                or self._evaluate_take_profit(position, bar, tick, current_high, current_dt)
            )

        if sig:
            _record_exit_eval("TRIGGER", sig.reason, sig.target_price or current_price)
        else:
            _record_exit_eval("HOLDING", "POSITION_HOLDING", current_price)
        return sig
