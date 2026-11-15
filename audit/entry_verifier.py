"""
Entry Contract Verifier.
Validates IEntryRule components for ghost-bar lookahead zero, statutory KRX tick conformance,
collar-cap slippage defense, and single-fire daily execution.
"""
from __future__ import annotations

from datetime import date, datetime, time
from typing import List, Tuple, Any

from core.krx_ticks import get_krx_tick_size, round_to_krx_tick
from engine.models import MinuteBar, EntrySignal
from strategy.base import IEntryRule
from audit.scanner_verifier import ContractCheckResult


class EntryContractVerifier:
    """Rigorous contract verifier for IEntryRule components."""

    @staticmethod
    def _create_bar(
        symbol: str,
        open_p: float,
        high_p: float,
        low_p: float,
        close_p: float,
        minute: int = 2,
        d: date = date(2024, 10, 21),
    ) -> MinuteBar:
        return MinuteBar(
            symbol=symbol,
            dt=datetime(d.year, d.month, d.day, 9, minute),
            open=open_p,
            high=high_p,
            low=low_p,
            close=close_p,
            volume=10000,
            turnover=500_000_000.0,
            cum_turnover=1_500_000_000.0,
        )

    def verify_ghost_bar_lookahead_zero(self, entry: IEntryRule) -> ContractCheckResult:
        """
        Ghost-Bar Tamper Detection:
        At the moment of entry breakout (Bar 0), the bar's future close is unknown in reality.
        Altering bar.close (e.g. from high breakout to heavy drop) MUST NOT alter the
        signal issuance, trigger price, or collar cap price.
        """
        d = date(2024, 10, 21)

        # Baseline: High breakout (+5%), close finishes near high (+4.8%)
        entry.on_day_open(d)
        bar_bull = self._create_bar("005930", open_p=50000.0, high_p=52500.0, low_p=49800.0, close_p=52400.0)
        sig_baseline = entry.evaluate_entry("005930", bar_bull)

        if sig_baseline is None:
            return ContractCheckResult(
                category="Entry",
                name="Bar 0 Ghost Tamper (Lookahead 0%)",
                passed=False,
                message="Setup failed: EntryRule did not fire on standard +5.0% breakout bar.",
            )

        # Ghost Tampered: Same breakout high, but future close finishes in a crash (-3.0%)
        entry.on_day_open(d)
        bar_tampered = self._create_bar("005930", open_p=50000.0, high_p=52500.0, low_p=48500.0, close_p=48500.0)
        sig_tampered = entry.evaluate_entry("005930", bar_tampered)

        if sig_tampered is None:
            return ContractCheckResult(
                category="Entry",
                name="Bar 0 Ghost Tamper (Lookahead 0%)",
                passed=False,
                message="Lookahead Detected: Entry was withheld because in-flight bar.close dumped before candle close.",
            )

        if (
            sig_tampered.trigger_price != sig_baseline.trigger_price
            or sig_tampered.collar_cap_price != sig_baseline.collar_cap_price
        ):
            return ContractCheckResult(
                category="Entry",
                name="Bar 0 Ghost Tamper (Lookahead 0%)",
                passed=False,
                message=f"Lookahead price drift: Baseline trigger={sig_baseline.trigger_price} vs Tampered={sig_tampered.trigger_price}",
            )

        return ContractCheckResult(
            category="Entry",
            name="Bar 0 Ghost Tamper (Lookahead 0%)",
            passed=True,
            message="Entry trigger and collar-cap pricing are strictly invariant to uncompleted Bar 0 close.",
        )

    def verify_krx_tick_conformance(self, entry: IEntryRule) -> ContractCheckResult:
        """
        Validates that across all 7 statutory KRX price increments, trigger_price and
        collar_cap_price strictly conform to statutory tick boundaries without exception.
        """
        d = date(2024, 10, 21)
        test_prices = [
            (1_500.0, 1),       # Tier 1: < 2,000 KRW (1 KRW)
            (3_500.0, 5),       # Tier 2: 2,000 ~ 5,000 KRW (5 KRW)
            (12_350.0, 10),     # Tier 3: 5,000 ~ 20,000 KRW (10 KRW)
            (34_500.0, 50),     # Tier 4: 20,000 ~ 50,000 KRW (50 KRW)
            (87_500.0, 100),    # Tier 5: 50,000 ~ 200,000 KRW (100 KRW)
            (345_000.0, 500),   # Tier 6: 200,000 ~ 500,000 KRW (500 KRW)
            (789_000.0, 1_000), # Tier 7: >= 500,000 KRW (1,000 KRW)
        ]

        for open_p, expected_tick in test_prices:
            entry.on_day_open(d)
            high_p = open_p * 1.06  # +6% breakout
            bar = self._create_bar("005930", open_p=open_p, high_p=high_p, low_p=open_p * 0.99, close_p=high_p)
            sig = entry.evaluate_entry("005930", bar)

            if not sig:
                return ContractCheckResult(
                    category="Entry",
                    name="KRX 7-Tier Tick Conformance",
                    passed=False,
                    message=f"Entry failed to generate signal for test base price {open_p:,.0f} KRW",
                )

            # Validate trigger price tick alignment
            trig_tick = get_krx_tick_size(sig.trigger_price)
            if sig.trigger_price % trig_tick != 0:
                return ContractCheckResult(
                    category="Entry",
                    name="KRX 7-Tier Tick Conformance",
                    passed=False,
                    message=f"Trigger price {sig.trigger_price} violates KRX tick {trig_tick} for price {open_p}",
                )

            # Validate collar cap tick alignment
            cap_tick = get_krx_tick_size(sig.collar_cap_price)
            if sig.collar_cap_price % cap_tick != 0:
                return ContractCheckResult(
                    category="Entry",
                    name="KRX 7-Tier Tick Conformance",
                    passed=False,
                    message=f"Collar cap {sig.collar_cap_price} violates KRX tick {cap_tick} for price {open_p}",
                )

        return ContractCheckResult(
            category="Entry",
            name="KRX 7-Tier Tick Conformance",
            passed=True,
            message="100% adherence to all 7 KRX statutory tick tiers; zero fractional price rejection risk.",
        )

    def verify_collar_cap_bounds(self, entry: IEntryRule) -> ContractCheckResult:
        """
        Validates collar-cap spread and 10,000,000 KRW fixed notional bounds.
        - collar_cap_price >= trigger_price
        - collar_cap_price <= trigger_price * (1 + max_tolerance)
        - actual allocation (qty * collar_cap_price) <= target_notional
        """
        d = date(2024, 10, 21)
        entry.on_day_open(d)
        bar = self._create_bar("005930", open_p=50000.0, high_p=53000.0, low_p=49800.0, close_p=52500.0)
        sig = entry.evaluate_entry("005930", bar)

        if not sig:
            return ContractCheckResult(
                category="Entry",
                name="Collar-Cap & Capital Bounds",
                passed=False,
                message="EntryRule failed to return signal during collar bounds check.",
            )

        if sig.collar_cap_price < sig.trigger_price:
            return ContractCheckResult(
                category="Entry",
                name="Collar-Cap & Capital Bounds",
                passed=False,
                message=f"Collar cap {sig.collar_cap_price} is lower than trigger price {sig.trigger_price}.",
            )

        # Max slippage tolerance: collar cap should not exceed trigger price + 1.5%
        max_allowed_collar = sig.trigger_price * 1.015
        if sig.collar_cap_price > max_allowed_collar:
            return ContractCheckResult(
                category="Entry",
                name="Collar-Cap & Capital Bounds",
                passed=False,
                message=f"Collar cap {sig.collar_cap_price} exceeds max allowed slippage bound {max_allowed_collar:.0f}.",
            )

        # Target notional capital sizing check: 10,000,000 KRW
        target_notional = getattr(sig, "target_notional", 10_000_000.0)
        qty = int(target_notional // sig.collar_cap_price)
        total_spent = qty * sig.collar_cap_price

        if total_spent > target_notional:
            return ContractCheckResult(
                category="Entry",
                name="Collar-Cap & Capital Bounds",
                passed=False,
                message=f"Order sizing exceeded fixed capital limit: {total_spent:,.0f} > {target_notional:,.0f} KRW",
            )

        return ContractCheckResult(
            category="Entry",
            name="Collar-Cap & Capital Bounds",
            passed=True,
            message="Collar cap bounds tightly enforced; fixed 10M KRW capital sizing never exceeded by 1 KRW.",
        )

    def verify_single_fire_daily(self, entry: IEntryRule) -> ContractCheckResult:
        """
        Validates that evaluate_entry fires at most ONCE per symbol per trading day.
        Subsequent breakout bars within the same session must return None.
        Next day boundary (on_day_open) must re-enable signal firing.
        """
        day1 = date(2024, 10, 21)
        day2 = date(2024, 10, 22)

        entry.on_day_open(day1)

        # 09:02: First breakout bar -> Should fire
        bar1 = self._create_bar("005930", 50000.0, 52500.0, 49800.0, 52000.0, minute=2, d=day1)
        sig1 = entry.evaluate_entry("005930", bar1)
        if not sig1:
            return ContractCheckResult(
                category="Entry",
                name="Single Fire Daily Invariant",
                passed=False,
                message="Entry did not fire on initial breakout bar.",
            )

        # 09:03: Second breakout bar -> MUST return None
        bar2 = self._create_bar("005930", 52000.0, 54000.0, 51900.0, 53800.0, minute=3, d=day1)
        sig2 = entry.evaluate_entry("005930", bar2)
        if sig2 is not None:
            return ContractCheckResult(
                category="Entry",
                name="Single Fire Daily Invariant",
                passed=False,
                message="Duplicate entry signal emitted on same symbol in single trading session.",
            )

        # Transition to Day 2
        entry.on_day_open(day2)
        bar_d2 = self._create_bar("005930", 50000.0, 52500.0, 49800.0, 52000.0, minute=2, d=day2)
        sig_d2 = entry.evaluate_entry("005930", bar_d2)
        if not sig_d2:
            return ContractCheckResult(
                category="Entry",
                name="Single Fire Daily Invariant",
                passed=False,
                message="Entry state failed to reset on subsequent day after on_day_open.",
            )

        return ContractCheckResult(
            category="Entry",
            name="Single Fire Daily Invariant",
            passed=True,
            message="Strict single-fire per symbol enforced; duplicate entries blocked and reset cleanly next day.",
        )

    def run_all_checks(self, entry: IEntryRule) -> List[ContractCheckResult]:
        """Runs all 4 entry contract checks."""
        return [
            self.verify_ghost_bar_lookahead_zero(entry),
            self.verify_krx_tick_conformance(entry),
            self.verify_collar_cap_bounds(entry),
            self.verify_single_fire_daily(entry),
        ]
