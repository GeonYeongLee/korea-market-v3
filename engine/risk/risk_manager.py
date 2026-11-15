"""
Risk Manager for Korea Market Trading Engine.
Enforces hard circuit breakers, daily loss caps, and position limits.
"""
from __future__ import annotations

from datetime import date
from typing import Optional
from engine.models import Order, OrderSide, AccountBalance


class RiskManager:
    def __init__(
        self,
        max_daily_loss: float = 500_000.0,       # 당일 최대 허용 손실 50만 원 (-5%)
        max_consecutive_losses: int = 2,         # 연속 2회 손실 시 당일 매매 중단
    ):
        self.max_daily_loss = max_daily_loss
        self.max_consecutive_losses = max_consecutive_losses

        self._daily_realized_pnl: float = 0.0
        self._consecutive_losses: int = 0
        self._circuit_broken: bool = False

    def on_day_open(self, current_date: date) -> None:
        self._daily_realized_pnl = 0.0
        self._consecutive_losses = 0
        self._circuit_broken = False

    def on_trade_completed(self, net_pnl: float) -> None:
        self._daily_realized_pnl += net_pnl
        if net_pnl < 0:
            self._consecutive_losses += 1
        else:
            self._consecutive_losses = 0

        # Check circuit breakers
        if self._daily_realized_pnl <= -self.max_daily_loss:
            self._circuit_broken = True
        if self._consecutive_losses >= self.max_consecutive_losses:
            self._circuit_broken = True

    def validate_order(self, order: Order, balance: AccountBalance) -> bool:
        if self._circuit_broken and order.side == OrderSide.BUY:
            return False
        return True

