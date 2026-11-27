"""
Account-Level Circuit Breaker for Korea Market v4.
Implements IRiskGuard with exact parity to v3 RiskManager.
"""
from __future__ import annotations

from datetime import date

from v4.interfaces import IRiskGuard
from v4.models import AccountBalance, OrderRequest, OrderSide


class DailyRiskGuard(IRiskGuard):
    """Enforces daily max realized loss and consecutive loss circuit breakers."""

    def __init__(
        self,
        max_daily_loss: float = 500_000.0,
        max_consecutive_losses: int = 2,
    ) -> None:
        self.max_daily_loss = float(max_daily_loss)
        self.max_consecutive_losses = int(max_consecutive_losses)
        self._daily_realized_pnl: float = 0.0
        self._consecutive_losses: int = 0
        self._circuit_broken: bool = False

    def on_day_open(self, current_date: date) -> None:
        self._daily_realized_pnl = 0.0
        self._consecutive_losses = 0
        self._circuit_broken = False

    def on_trade_completed(self, net_pnl: float) -> None:
        self._daily_realized_pnl += float(net_pnl)
        if net_pnl < 0.0:
            self._consecutive_losses += 1
        else:
            self._consecutive_losses = 0

        if self._daily_realized_pnl <= -self.max_daily_loss:
            self._circuit_broken = True
        if self._consecutive_losses >= self.max_consecutive_losses:
            self._circuit_broken = True

    def validate_order(self, req: OrderRequest, balance: AccountBalance) -> bool:
        if self._circuit_broken and req.side == OrderSide.BUY:
            return False
        return True

    @property
    def is_circuit_broken(self) -> bool:
        return self._circuit_broken
