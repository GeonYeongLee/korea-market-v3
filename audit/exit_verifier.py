"""
Exit Policy Contract Verifier.
Validates IExitPolicy components for ratchet SL monotonicity, ambiguous bar defense,
and 15:15 hard timeout liquidation.
"""
from __future__ import annotations

from datetime import date, datetime, time
from typing import List, Optional, Any

from engine.models import MinuteBar, Position, ExitSignal, OrderType
from strategy.base import IExitPolicy
from audit.scanner_verifier import ContractCheckResult


class ExitContractVerifier:
    """Rigorous contract verifier for IExitPolicy components."""

    @staticmethod
    def _create_position(
        symbol: str = "005930",
        avg_price: float = 50000.0,
        qty: int = 200,
        entry_dt: Optional[datetime] = None,
    ) -> Position:
        dt = entry_dt or datetime(2024, 10, 21, 9, 2)
        return Position(
            symbol=symbol,
            quantity=qty,
            avg_price=avg_price,
            current_price=avg_price,
            high_water_mark=avg_price,
            entry_dt=dt,
            phase=1,
            metadata={"ref_bar": {"open": 50000.0, "high": 52500.0, "low": 49800.0, "close": 52000.0, "dt": dt}},
        )

    def verify_ratchet_sl_monotonicity(self, exit_policy: IExitPolicy) -> ContractCheckResult:
        """
        Ratchet SL Monotonicity:
        Once a Stop-Loss level is elevated (via phase transition, ATR midpoint lock, or trailing step),
        it CANNOT regress downwards under any subsequent price drops or oscillating volatility.
        """
        pos = self._create_position()
        exit_policy.on_position_opened(pos)

        initial_sl = pos.stop_loss_price
        if initial_sl <= 0:
            return ContractCheckResult(
                category="Exit",
                name="Stop-Loss Ratchet Monotonicity",
                passed=False,
                message="ExitPolicy failed to initialize a positive stop loss upon position creation.",
            )

        recorded_sls = [initial_sl]

        # Multi-stage price path:
        # Step 1: Promotion to Phase 2 (Bar 1 at 09:03)
        bar_p2 = MinuteBar("005930", datetime(2024, 10, 21, 9, 3), 52000.0, 52800.0, 51800.0, 52500.0, 5000)
        exit_policy.evaluate_exit(pos, bar_p2)
        recorded_sls.append(pos.stop_loss_price)

        # Step 2: High surge (+4.0%) -> Triggers Step-1 trailing lock
        bar_surge = MinuteBar("005930", datetime(2024, 10, 21, 9, 4), 52500.0, 53500.0, 52400.0, 53200.0, 8000)
        exit_policy.evaluate_exit(pos, bar_surge)
        recorded_sls.append(pos.stop_loss_price)

        # Step 3: Sharp intraday pullback (price drops but remains above locked SL)
        bar_drop = MinuteBar("005930", datetime(2024, 10, 21, 9, 5), 53200.0, 53200.0, 50800.0, 51000.0, 7000)
        exit_policy.evaluate_exit(pos, bar_drop)
        recorded_sls.append(pos.stop_loss_price)

        # Step 4: High volatility sawtooth
        bar_sawtooth = MinuteBar("005930", datetime(2024, 10, 21, 9, 6), 51000.0, 52000.0, 50600.0, 51500.0, 6000)
        exit_policy.evaluate_exit(pos, bar_sawtooth)
        recorded_sls.append(pos.stop_loss_price)

        # Verify monotonicity: SL(t+1) >= SL(t)
        for i in range(1, len(recorded_sls)):
            prev_sl = recorded_sls[i - 1]
            curr_sl = recorded_sls[i]
            if curr_sl < prev_sl:
                return ContractCheckResult(
                    category="Exit",
                    name="Stop-Loss Ratchet Monotonicity",
                    passed=False,
                    message=f"Ratchet broken at step {i}: SL regressed from {prev_sl:,.0f} to {curr_sl:,.0f} KRW",
                )

        return ContractCheckResult(
            category="Exit",
            name="Stop-Loss Ratchet Monotonicity",
            passed=True,
            message="Stop-loss price adheres strictly to monotonic ratchet rule (never regressed across volatility).",
        )

    def verify_ambiguous_bar_defense(self, exit_policy: IExitPolicy) -> ContractCheckResult:
        """
        Ambiguous Bar Defense:
        When a single 1-minute bar touches BOTH the profit target (High) and the stop-loss (Low),
        a robust production system under pessimistic mode MUST prioritize STOP_LOSS to prevent
        backtest curve fitting and overestimation.
        """
        # Test under pessimistic configuration (if supported by exit policy)
        mode = getattr(exit_policy, "ambiguity_mode", "pessimistic")
        if mode != "pessimistic":
            return ContractCheckResult(
                category="Exit",
                name="Ambiguous Bar Pessimistic Defense",
                passed=True,
                message=f"ExitPolicy configured in '{mode}' mode; pessimistic test skipped.",
            )

        pos = self._create_position()
        exit_policy.on_position_opened(pos)

        # Case 1: In Phase 1, create an extreme ambiguous bar
        # Price 50,000 -> SL is -2.0% (49,000). Ambiguous bar has High=53,000 (+6%), Low=48,000 (-4%)
        ambiguous_bar = MinuteBar(
            symbol="005930",
            dt=datetime(2024, 10, 21, 9, 2), # Bar 0 in-flight
            open=50000.0,
            high=53500.0, # +7.0% (Would trigger any TP)
            low=48000.0,  # -4.0% (Touches Emergency SL 49,000)
            close=52000.0,
            volume=20000,
        )

        sig = exit_policy.evaluate_exit(pos, ambiguous_bar)

        if not sig:
            return ContractCheckResult(
                category="Exit",
                name="Ambiguous Bar Pessimistic Defense",
                passed=False,
                message="No exit signal generated during extreme ambiguous bar breaching all bounds.",
            )

        # Must exit on STOP LOSS, not TAKE PROFIT
        reason_upper = sig.reason.upper()
        if "TAKE_PROFIT" in reason_upper or "TP" in reason_upper:
            return ContractCheckResult(
                category="Exit",
                name="Ambiguous Bar Pessimistic Defense",
                passed=False,
                message=f"Pessimistic failure: Take-profit was awarded on ambiguous bar ({sig.reason}).",
            )

        if "SL" not in reason_upper and "STOP" not in reason_upper and "EMERGENCY" not in reason_upper:
            return ContractCheckResult(
                category="Exit",
                name="Ambiguous Bar Pessimistic Defense",
                passed=False,
                message=f"Exit reason '{sig.reason}' is not a defensive stop-loss on ambiguous bar.",
            )

        return ContractCheckResult(
            category="Exit",
            name="Ambiguous Bar Pessimistic Defense",
            passed=True,
            message="Ambiguous bar successfully settled defensively with Stop Loss under pessimistic assumptions.",
        )

    def verify_hard_timeout_1515(self, exit_policy: IExitPolicy) -> ContractCheckResult:
        """
        Hard Timeout EOD Invariant:
        Any position open at hard_timeout cutoff (15:15 or 15:21) or later MUST be liquidated
        unconditionally via Market order, strictly preventing overnight carrying risk.
        """
        pos = self._create_position()
        exit_policy.on_position_opened(pos)

        timeout = getattr(exit_policy, "hard_timeout", time(15, 21))
        cutoff_name = f"Hard Timeout {timeout.strftime('%H:%M')} Invariant"

        # 1 minute before cutoff -> Position is healthy, no timeout expected
        pre_minute = timeout.minute - 1 if timeout.minute > 0 else 59
        pre_hour = timeout.hour if timeout.minute > 0 else timeout.hour - 1
        bar_pre = MinuteBar("005930", datetime(2024, 10, 21, pre_hour, pre_minute), 51000.0, 51200.0, 50900.0, 51100.0, 1000)
        sig_pre = exit_policy.evaluate_exit(pos, bar_pre)
        if sig_pre and "TIMEOUT" in sig_pre.reason.upper():
            return ContractCheckResult(
                category="Exit",
                name=cutoff_name,
                passed=False,
                message=f"Premature timeout triggered before {timeout.strftime('%H:%M')}:00 cutoff.",
            )

        # At cutoff -> Hard timeout MUST trigger
        bar_timeout = MinuteBar("005930", datetime(2024, 10, 21, timeout.hour, timeout.minute), 51000.0, 51200.0, 50900.0, 51100.0, 1000)
        sig_timeout = exit_policy.evaluate_exit(pos, bar_timeout)

        if not sig_timeout:
            return ContractCheckResult(
                category="Exit",
                name=cutoff_name,
                passed=False,
                message=f"ExitPolicy failed to emit exit signal at {timeout.strftime('%H:%M')}:00 cutoff time.",
            )

        if sig_timeout.order_type != OrderType.MARKET:
            return ContractCheckResult(
                category="Exit",
                name=cutoff_name,
                passed=False,
                message=f"Timeout order must be MARKET order, got {sig_timeout.order_type}",
            )

        if "TIMEOUT" not in sig_timeout.reason.upper():
            return ContractCheckResult(
                category="Exit",
                name=cutoff_name,
                passed=False,
                message=f"Timeout exit signal reason unexpected: '{sig_timeout.reason}'",
            )

        return ContractCheckResult(
            category="Exit",
            name=cutoff_name,
            passed=True,
            message=f"Hard timeout at {timeout.strftime('%H:%M')}:00 strictly enforced with Market order; overnight risk eliminated.",
        )

    def run_all_checks(self, exit_policy: IExitPolicy) -> List[ContractCheckResult]:
        """Runs all 3 exit contract checks."""
        return [
            self.verify_ratchet_sl_monotonicity(exit_policy),
            self.verify_ambiguous_bar_defense(exit_policy),
            self.verify_hard_timeout_1515(exit_policy),
        ]
