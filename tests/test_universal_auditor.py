"""
Unit and Contract Verification Tests for UniversalStrategyAuditor.
Verifies both production strategy compliance and fault-injection detection power.
"""
from __future__ import annotations

from datetime import date, datetime, time
from typing import Optional, List, Dict, Any
import pytest

from engine.models import MinuteBar, Position, EntrySignal, ExitSignal, OrderType
from strategy.base import IScanner, IEntryRule, IExitPolicy
from strategy.composite import CompositeStrategy
from strategy.registry import build_strategy
from audit.scanner_verifier import ScannerContractVerifier
from audit.entry_verifier import EntryContractVerifier
from audit.exit_verifier import ExitContractVerifier
from audit.universal_auditor import UniversalStrategyAuditor


# ============================================================================
# 1. Faulty Mock Components (for Verifier Defect Detection Verification)
# ============================================================================

class BuggyLookaheadScanner(IScanner):
    """Buggy Scanner that alters past candidate decision based on future bars."""
    name = "buggy_lookahead_scanner"

    def __init__(self):
        self._history: List[MinuteBar] = []

    def filter_universe(self, current_date: date) -> Optional[List[str]]:
        return None

    def on_day_open(self, current_date: date) -> None:
        self._history.clear()

    def on_bar(self, current_date: date, bar: MinuteBar) -> Optional[List[str]]:
        self._history.append(bar)
        # Violates lookahead by returning candidate only if future extreme spike seen
        if any(b.turnover > 1e10 for b in self._history):
            return [bar.symbol]
        return None


class BuggyZeroDivScanner(IScanner):
    """Buggy Scanner that unhandledly crashes on zero open price."""
    name = "buggy_zero_div_scanner"

    def filter_universe(self, current_date: date) -> Optional[List[str]]:
        return None

    def on_bar(self, current_date: date, bar: MinuteBar) -> Optional[List[str]]:
        # Unconditionally divides by open price without checking for 0.0
        pct = (bar.close - bar.open) / bar.open
        return [bar.symbol] if pct > 0.01 else None


class BuggyGhostBarEntry(IEntryRule):
    """Buggy Entry that peeks at Bar 0 future close to cancel entry."""
    name = "buggy_ghost_bar_entry"

    def on_day_open(self, current_date: date) -> None:
        pass

    def evaluate_entry(self, symbol: str, bar: MinuteBar, tick=None, context_state=None) -> Optional[EntrySignal]:
        # Breaches Lookahead: Refuses to enter if in-flight candle dumps before close
        if bar.high >= 52000.0 and bar.close >= 50000.0:
            return EntrySignal(symbol, bar.dt, trigger_price=52000, collar_cap_price=52200)
        return None


class BuggyTickEntry(IEntryRule):
    """Buggy Entry that generates invalid KRX tick increments (e.g. 10 KRW on 50,000+ KRW)."""
    name = "buggy_tick_entry"

    def on_day_open(self, current_date: date) -> None:
        pass

    def evaluate_entry(self, symbol: str, bar: MinuteBar, tick=None, context_state=None) -> Optional[EntrySignal]:
        # 52,345 KRW is invalid on KRX (must be 100 KRW tick -> 52,300 or 52,400)
        return EntrySignal(symbol, bar.dt, trigger_price=52345.0, collar_cap_price=52345.0)


class BuggyMultiFireEntry(IEntryRule):
    """Buggy Entry that repeatedly fires on every single bar."""
    name = "buggy_multi_fire_entry"

    def on_day_open(self, current_date: date) -> None:
        pass

    def evaluate_entry(self, symbol: str, bar: MinuteBar, tick=None, context_state=None) -> Optional[EntrySignal]:
        return EntrySignal(symbol, bar.dt, trigger_price=52000, collar_cap_price=52200)


class BuggyRegressingSlExit(IExitPolicy):
    """Buggy Exit whose stop-loss regresses downwards when price pulls back."""
    name = "buggy_regressing_sl_exit"

    def on_position_opened(self, position: Position) -> None:
        position.stop_loss_price = position.avg_price * 0.98

    def evaluate_exit(self, position: Position, bar=None, tick=None) -> Optional[ExitSignal]:
        # Fatal flaw: sets SL relative to current lower close, allowing SL to drop!
        if bar:
            position.stop_loss_price = bar.close * 0.98
        return None


class BuggyOptimisticAmbiguityExit(IExitPolicy):
    """Buggy Exit that awards Take-Profit on ambiguous bars under pessimistic mode."""
    name = "buggy_optimistic_ambiguity_exit"

    def __init__(self, ambiguity_mode: str = "pessimistic"):
        self.ambiguity_mode = ambiguity_mode

    def on_position_opened(self, position: Position) -> None:
        position.stop_loss_price = 49000.0
        position.take_profit_price = 53000.0

    def evaluate_exit(self, position: Position, bar=None, tick=None) -> Optional[ExitSignal]:
        # Always evaluates TP first regardless of pessimism
        if bar and bar.high >= position.take_profit_price:
            return ExitSignal(position.symbol, bar.dt, OrderType.LIMIT, position.take_profit_price, "TAKE_PROFIT")
        if bar and bar.low <= position.stop_loss_price:
            return ExitSignal(position.symbol, bar.dt, OrderType.MARKET, position.stop_loss_price, "STOP_LOSS")
        return None


class BuggyNoTimeoutExit(IExitPolicy):
    """Buggy Exit that ignores 15:15 hard timeout."""
    name = "buggy_no_timeout_exit"

    def on_position_opened(self, position: Position) -> None:
        position.stop_loss_price = position.avg_price * 0.98

    def evaluate_exit(self, position: Position, bar=None, tick=None) -> Optional[ExitSignal]:
        # Completely omits timeout check, exposing positions to overnight risk
        return None


# ============================================================================
# 2. Production Strategy Compliance Tests (100% PASS Expected)
# ============================================================================

def test_production_composite_strategies_audit_pass():
    """Validates that all standard registry strategy combinations pass 100% of audit contracts."""
    auditor = UniversalStrategyAuditor()

    # Production Combo: morning_leader + morning_breakout + morning_leader (pessimistic mode)
    strat1 = build_strategy("morning_leader", "morning_breakout", "morning_leader", ambiguity_mode="pessimistic")
    assert strat1.scanner.min_body_pct == 0.035
    assert strat1.scanner.min_cum_turnover == 1_000_000_000.0
    report1 = auditor.audit(strat1)
    assert report1.passed is True
    assert report1.total_checks == 14
    assert report1.passed_checks == 14

    # Production Combo: morning_leader + morning_breakout + morning_leader (optimistic mode)
    strat2 = build_strategy("morning_leader", "morning_breakout", "morning_leader", ambiguity_mode="optimistic")
    report2 = auditor.audit(strat2)
    assert report2.passed is True
    assert report2.total_checks == 14
    assert report2.passed_checks == 14

    # Dynamic Leader Combo: morning_leader + morning_breakout + dynamic_leader (pessimistic mode)
    strat3 = build_strategy("morning_leader", "morning_breakout", "dynamic_leader", ambiguity_mode="pessimistic")
    report3 = auditor.audit(strat3)
    assert report3.passed is True
    assert report3.total_checks == 14
    assert report3.passed_checks == 14

    # Dynamic Leader Combo: morning_leader + morning_breakout + dynamic_leader (optimistic mode)
    strat4 = build_strategy("morning_leader", "morning_breakout", "dynamic_leader", ambiguity_mode="optimistic")
    report4 = auditor.audit(strat4)
    assert report4.passed is True
    assert report4.total_checks == 14
    assert report4.passed_checks == 14

    # Bear Market Dip-Rebound Combo: bear_dip_rebound + vwap_rebound + eod_trend (pessimistic mode)
    strat5 = build_strategy("bear_dip_rebound", "vwap_rebound", "eod_trend", ambiguity_mode="pessimistic")
    report5 = auditor.audit(strat5)
    assert report5.passed is True
    assert report5.total_checks == 14
    assert report5.passed_checks == 14

    # Bear Market Dip-Rebound Combo: bear_dip_rebound + vwap_rebound + eod_trend (optimistic mode)
    strat6 = build_strategy("bear_dip_rebound", "vwap_rebound", "eod_trend", ambiguity_mode="optimistic")
    report6 = auditor.audit(strat6)
    assert report6.passed is True
    assert report6.total_checks == 14
    assert report6.passed_checks == 14


# ============================================================================
# 3. Defect Detection Power Tests (Auditor Must Catch Invariant Breaches)
# ============================================================================

def test_scanner_verifier_catches_lookahead_bug():
    verifier = ScannerContractVerifier()
    res = verifier.verify_lookahead_zero(BuggyLookaheadScanner())
    assert res.passed is False
    assert "Lookahead dependency detected" in res.message


def test_scanner_verifier_catches_zero_division():
    verifier = ScannerContractVerifier()
    res = verifier.verify_extreme_resilience(BuggyZeroDivScanner())
    assert res.passed is False
    assert "ZeroDivisionError" in res.message


def test_entry_verifier_catches_ghost_bar_lookahead():
    verifier = EntryContractVerifier()
    res = verifier.verify_ghost_bar_lookahead_zero(BuggyGhostBarEntry())
    assert res.passed is False
    assert "Lookahead Detected" in res.message


def test_entry_verifier_catches_krx_tick_violation():
    verifier = EntryContractVerifier()
    res = verifier.verify_krx_tick_conformance(BuggyTickEntry())
    assert res.passed is False
    assert "violates KRX tick" in res.message


def test_entry_verifier_catches_multi_fire_bug():
    verifier = EntryContractVerifier()
    res = verifier.verify_single_fire_daily(BuggyMultiFireEntry())
    assert res.passed is False
    assert "Duplicate entry signal" in res.message


def test_exit_verifier_catches_sl_regression():
    verifier = ExitContractVerifier()
    res = verifier.verify_ratchet_sl_monotonicity(BuggyRegressingSlExit())
    assert res.passed is False
    assert "Ratchet broken" in res.message


def test_exit_verifier_catches_ambiguous_bar_optimism():
    verifier = ExitContractVerifier()
    res = verifier.verify_ambiguous_bar_defense(BuggyOptimisticAmbiguityExit(ambiguity_mode="pessimistic"))
    assert res.passed is False
    assert "Pessimistic failure" in res.message


def test_exit_verifier_catches_missing_timeout():
    verifier = ExitContractVerifier()
    res = verifier.verify_hard_timeout_1515(BuggyNoTimeoutExit())
    assert res.passed is False
    assert "failed to emit exit signal" in res.message


def test_universal_auditor_flags_flawed_composite_strategy():
    """Ensures that a CompositeStrategy assembled with a flawed component fails the overall audit."""
    auditor = UniversalStrategyAuditor()
    flawed_strat = CompositeStrategy(
        scanner=BuggyZeroDivScanner(),
        entry=BuggyTickEntry(),
        exit_policy=BuggyNoTimeoutExit(),
    )
    report = auditor.audit(flawed_strat)
    assert report.passed is False
    failed_checks = [r for r in report.results if not r.passed]
    assert len(failed_checks) >= 3
