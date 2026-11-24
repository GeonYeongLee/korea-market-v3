"""
EOD Trend Exit Policy for Bear Market Dip-Rebound Strategy.
Enforces:
  1. Catastrophic Stop Loss at -2.5% (KRX tick ceil rounded).
  2. Profit Lock Ratchet: If MFE >= +5.0%, ratchet SL up to +2.0% (monotonic, never regresses).
  3. Hold to 15:20 EOD (Zero overnight carrying risk).
  4. Pessimistic ambiguity defense: SL strictly prioritized over TP on ambiguous bars.
"""
from __future__ import annotations

from datetime import date, datetime, time
from typing import Optional

from core.krx_ticks import round_to_krx_tick
from core.telemetry import telemetry
from engine.models import MinuteBar, TickData, Position, ExitSignal, OrderType
from strategy.base import IExitPolicy


class EodTrendExit(IExitPolicy):
    """
    End-Of-Day Trend Exit Policy.
    Liquidates at 15:20:00 cutoff or on -2.5% catastrophic SL / +2.0% ratcheted SL.
    Strictly adheres to KRX statutory tick rules and 14 contract invariants.
    """

    name: str = "eod_trend_exit"

    def __init__(
        self,
        emergency_sl_pct: float = -0.025,       # -2.5% catastrophic stop loss
        ratchet_mfe_trigger: float = 0.050,     # +5.0% MFE triggers profit lock
        ratchet_locked_sl_pct: float = 0.020,   # +2.0% profit lock ratchet
        hard_timeout: time = time(15, 19),      # 15:19:00 EOD hard cutoff (before 15:20 auction)
        ambiguity_mode: str = "pessimistic",    # "pessimistic" or "optimistic"
        **kwargs: Any,
    ):
        self.emergency_sl_pct = emergency_sl_pct
        self.ratchet_mfe_trigger = ratchet_mfe_trigger
        self.ratchet_locked_sl_pct = ratchet_locked_sl_pct
        self.hard_timeout = hard_timeout
        self.ambiguity_mode = ambiguity_mode

    @staticmethod
    def _make_signal(position: Position, dt: datetime, price: float, reason: str, order_type: OrderType = OrderType.MARKET) -> ExitSignal:
        return ExitSignal(
            symbol=position.symbol,
            dt=dt,
            order_type=order_type,
            target_price=price,
            reason=reason,
        )

    def on_position_opened(self, position: Position) -> None:
        """Configures initial catastrophic stop-loss and watermarks."""
        raw_sl = position.avg_price * (1.0 + self.emergency_sl_pct)
        position.stop_loss_price = round_to_krx_tick(raw_sl, side="SELL", round_mode="ceil")
        position.high_water_mark = position.avg_price
        position.metadata["sl_ratcheted"] = False
        position.metadata["emergency_sl_pct"] = self.emergency_sl_pct
        position.metadata["ratchet_locked_sl_pct"] = self.ratchet_locked_sl_pct

    def on_day_open(self, current_date: date) -> None:
        """Called at 09:00:00 daily boundary."""
        pass

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

        # 1. Update High Water Mark & Ratchet Stop-Loss (Monotonic invariant)
        if current_high > position.high_water_mark:
            position.high_water_mark = current_high

        mfe = (position.high_water_mark - position.avg_price) / position.avg_price if position.avg_price > 0 else 0.0
        if mfe >= self.ratchet_mfe_trigger:
            raw_locked_sl = position.avg_price * (1.0 + self.ratchet_locked_sl_pct)
            locked_sl = round_to_krx_tick(raw_locked_sl, side="SELL", round_mode="ceil")
            # Ratchet SL can ONLY increase, never regress
            position.stop_loss_price = max(position.stop_loss_price, locked_sl)
            position.metadata["sl_ratcheted"] = True

        # 2. Stop-Loss check (Pessimistic ambiguity defense)
        # Evaluated BEFORE timeout or take-profit to guarantee worst-case risk defense
        if position.stop_loss_price > 0.0 and current_low <= position.stop_loss_price:
            reason = "PROFIT_LOCK_RATCHET_SL" if position.metadata.get("sl_ratcheted") else "CATASTROPHIC_STOP_LOSS"
            telemetry.record_strategy_eval(
                "EXIT", position.symbol, "TRIGGER", reason,
                {
                    "stop_loss_price": position.stop_loss_price,
                    "avg_price": position.avg_price,
                    "low": current_low,
                    "mfe": mfe,
                },
                dt=current_dt,
            )
            return self._make_signal(position, current_dt, position.stop_loss_price, reason)

        # 3. Hard Timeout 15:20 Invariant (Zero overnight carry)
        if current_dt.time() >= self.hard_timeout:
            telemetry.record_strategy_eval(
                "EXIT", position.symbol, "TRIGGER", "HARD_TIMEOUT_1520",
                {"current_price": current_price, "cutoff": str(self.hard_timeout)},
                dt=current_dt,
            )
            return self._make_signal(position, current_dt, current_price, "HARD_TIMEOUT_1520", order_type=OrderType.MARKET)

        return None
