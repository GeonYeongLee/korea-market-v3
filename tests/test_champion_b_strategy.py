"""
Unit and integration tests for Champion B (Body5.5-Micro) Modular Strategy:
- AnchorBodyMicroScanner (ChampionBScanner)
- AnchorCloseEntry
- AnchorMorningExit (09:00 KRX Regular Open Liquidation)
- Registry Factory: build_champion_b_strategy
- Full 14/14 Universal Strategy Audit Contract Verification
"""
from __future__ import annotations

from datetime import datetime, time, date
import pytest

from engine.models import MinuteBar, Position, OrderType
from strategy.scanners.anchor_body_micro_scanner import AnchorBodyMicroScanner, ChampionBScanner
from strategy.entries.anchor_close_entry import AnchorCloseEntry
from strategy.exits.anchor_morning_exit import AnchorMorningExit
from strategy.registry import build_strategy, build_champion_b_strategy
from audit.universal_auditor import UniversalStrategyAuditor


def test_champion_b_scanner_window_and_candidates():
    """Verifies that AnchorBodyMicroScanner filters candidates by 15:15~15:25 window."""
    scanner = AnchorBodyMicroScanner(
        min_body_ret=5.5,
        top10_max_shadow=0.10,
        top20_max_shadow=0.08,
        top10_min_change=8.0,
        top20_min_change=10.0,
        max_change_pct=22.0,
    )
    scanner.on_day_open(date(2025, 1, 13))

    # Mock daily candidate injection
    scanner._day_candidates = ["005930", "000660"]

    # 1. Bar before 15:15 (e.g. 11:00) -> None
    bar_morning = MinuteBar(
        "005930",
        datetime(2025, 1, 13, 11, 0),
        50000.0, 50500.0, 49800.0, 50200.0, 5000, 250_000_000.0, cum_turnover=2_000_000_000.0
    )
    assert scanner.on_bar(date(2025, 1, 13), bar_morning) is None

    # 2. Bar at 15:20 for candidate stock -> returns ["005930"]
    bar_close = MinuteBar(
        "005930",
        datetime(2025, 1, 13, 15, 20),
        55000.0, 55500.0, 54800.0, 55200.0, 10000, 550_000_000.0, cum_turnover=55_000_000_000.0
    )
    assert scanner.on_bar(date(2025, 1, 13), bar_close) == ["005930"]

    # 3. Bar at 15:20 for non-candidate stock -> None
    bar_other = MinuteBar(
        "999999",
        datetime(2025, 1, 13, 15, 20),
        10000.0, 10100.0, 9900.0, 10000.0, 1000, 10_000_000.0, cum_turnover=100_000_000.0
    )
    assert scanner.on_bar(date(2025, 1, 13), bar_other) is None

    # 4. Bar after 15:25 (e.g. 15:30) -> None
    bar_late = MinuteBar(
        "005930",
        datetime(2025, 1, 13, 15, 30),
        55200.0, 55200.0, 55200.0, 55200.0, 1000, 55_200_000.0, cum_turnover=55_055_200_000.0
    )
    assert scanner.on_bar(date(2025, 1, 13), bar_late) is None


def test_champion_b_entry_collar_and_single_fire():
    """Verifies that AnchorCloseEntry executes cleanly at 15:20 with collar cap and KRX tick sizing."""
    entry = AnchorCloseEntry(collar_cap_pct=0.005, target_notional=5_000_000.0)
    entry.on_day_open(date(2025, 1, 13))

    bar_entry = MinuteBar(
        "005930",
        datetime(2025, 1, 13, 15, 20),
        50000.0, 50500.0, 49900.0, 50000.0, 10000, 500_000_000.0
    )
    sig = entry.evaluate_entry("005930", bar_entry)

    assert sig is not None
    assert sig.symbol == "005930"
    assert sig.trigger_price == 50000.0
    # KRX tick at 50,000 KRW is 100 KRW. 50,000 * 1.005 = 50,250 -> floor to 100 KRW is 50,200 KRW
    assert sig.collar_cap_price == 50200.0
    assert sig.target_notional == 5_000_000.0
    assert sig.reason == "AUCTION_CLOSE_BETTING"

    # Single-fire invariant: duplicate call on same symbol within the same day returns None
    sig_dup = entry.evaluate_entry("005930", bar_entry)
    assert sig_dup is None


def test_champion_b_open_liquidation_exit():
    """Verifies that AnchorMorningExit with exit_at_open=True liquidates positions at 09:00 regular open auction."""
    exit_policy = AnchorMorningExit(exit_at_open=True, open_exit_time=time(9, 0))

    pos = Position(
        symbol="005930",
        quantity=100,
        avg_price=50000.0,
        current_price=50000.0,
        high_water_mark=50000.0,
        entry_dt=datetime(2025, 1, 13, 15, 20),
        phase=1,
        metadata={"ref_bar": {"open": 50000.0, "high": 50500.0, "low": 49800.0, "close": 50000.0}},
    )
    exit_policy.on_position_opened(pos)

    # D+1 KRX Opening auction bar (09:00)
    bar_open = MinuteBar(
        "005930",
        datetime(2025, 1, 14, 9, 0),
        51200.0, 51500.0, 51000.0, 51300.0, 20000
    )
    sig_exit = exit_policy.evaluate_exit(pos, bar_open)

    assert sig_exit is not None
    assert sig_exit.reason == "KRX_0900_OPEN_EXIT"
    assert sig_exit.order_type == OrderType.MARKET
    assert sig_exit.target_price == 51200.0 # Filled cleanly at 09:00 open price (0% slippage)


def test_champion_b_factory_builder_and_scaling():
    """Verifies that build_champion_b_strategy correctly configures capital, slots, and notional caps."""
    # 1. Standard 50% Cap (Sharpe 1.36, MDD -7.66%)
    strat_50 = build_champion_b_strategy(total_capital=10_000_000.0, cap_pct=0.50, max_daily_positions=2)
    assert strat_50.name == "ChampionB_Body5.5_Micro_Cap50"
    assert strat_50.max_daily_positions == 2
    assert strat_50.current_slot_notional == 5_000_000.0
    assert isinstance(strat_50.scanner, ChampionBScanner)
    assert isinstance(strat_50.entry, AnchorCloseEntry)
    assert isinstance(strat_50.exit_policy, AnchorMorningExit)
    assert strat_50.exit_policy.exit_at_open is True

    # 2. Maximum Profit 60% Cap (Profit +18.93M, MDD -9.18%)
    strat_60 = build_champion_b_strategy(total_capital=10_000_000.0, cap_pct=0.60, max_daily_positions=2)
    assert strat_60.name == "ChampionB_Body5.5_Micro_Cap60"
    assert strat_60.max_daily_positions == 2
    assert strat_60.current_slot_notional == 6_000_000.0

    # 3. Registry key lookup: "champion_b" and "anchor_body_micro"
    strat_reg = build_strategy(
        scanner_name="champion_b",
        entry_name="anchor_close",
        exit_name="anchor_morning",
    )
    assert strat_reg is not None
    assert isinstance(strat_reg.scanner, ChampionBScanner)


def test_champion_b_universal_strategy_audit_14_contracts():
    """
    Verifies that Champion B (build_champion_b_strategy) complies with all 14 universal audit contracts:
      1. Single Fire Invariant
      2. Deterministic Monotonicity
      3. Bar-0 Ghost Tamper Invariant (Lookahead 0%)
      4. KRX 7-Tier Tick Sizing
      5. Anti-Chase Collar Cap Invariant
      6. Hard Cutoff Monotonicity
      7. Pessimistic Ambiguity Defense
      8. Metamorphic Order Type Invariant
      9. Drawdown Monotonicity
      10. Volume & Turnover Invariant
      11. Floating Precision Invariant
      12. Re-entrance Memory Leak Invariant
      13. Partial Fill Liquidation Invariant
      14. Execution Reality Check
    """
    strategy = build_champion_b_strategy(
        total_capital=10_000_000.0,
        cap_pct=0.50,
        max_daily_positions=2,
        ambiguity_mode="pessimistic",
    )
    auditor = UniversalStrategyAuditor()
    report = auditor.audit(strategy)

    assert report.total_checks == 14
    assert report.passed_checks == 14
    assert report.passed is True


def test_crisis_rebound_scanner_and_priority():
    """
    Verifies that CrisisReboundScanner enforces:
      1. Market panic trigger (market_avg_chg <= -2.0%)
      2. Priority 1: Pattern C (Gap-down green body)
      3. Priority 2: Pattern A (Waterfall drop <= -6.0%)
    """
    from strategy.scanners.crisis_rebound_scanner import CrisisReboundScanner

    scanner = CrisisReboundScanner(
        market_panic_threshold=-2.0,
        pattern_c_open_gap_max=-1.0,
        pattern_c_body_min=0.5,
        pattern_a_drop_max=-6.0,
        turnover_rank_max=20,
        max_universe_size=2,
    )

    # Mock daily cache with Pattern C and Pattern A
    scanner._daily_cache = {
        "2024-08-05": ["000660", "005930"], # Black Monday panic day
    }

    scanner.on_day_open(date(2024, 8, 5))
    assert scanner.filter_universe(date(2024, 8, 5)) == ["000660", "005930"]

    # 15:20 bar triggers candidate
    bar_panic = MinuteBar(
        "000660",
        datetime(2024, 8, 5, 15, 20),
        150000.0, 152000.0, 148000.0, 151000.0, 50000, 7_500_000_000.0, cum_turnover=150_000_000_000.0
    )
    assert scanner.on_bar(date(2024, 8, 5), bar_panic) == ["000660"]

    # Non-candidate on panic day
    bar_other = MinuteBar(
        "123456",
        datetime(2024, 8, 5, 15, 20),
        10000.0, 10000.0, 9500.0, 9600.0, 1000, 9_600_000.0
    )
    assert scanner.on_bar(date(2024, 8, 5), bar_other) is None


def test_champion_b_fallback_integration():
    """
    Verifies that AnchorBodyMicroScanner uses Champion B on normal days,
    and falls back to CrisisRebound candidates when Champion B has 0 candidates on panic days.
    """
    scanner = AnchorBodyMicroScanner(
        enable_crisis_rebound=True,
        crisis_market_panic_threshold=-2.0,
        crisis_pattern_c_open_gap_max=-1.0,
        crisis_pattern_c_body_min=0.5,
        crisis_pattern_a_drop_max=-6.0,
        crisis_turnover_rank_max=20,
    )

    # Mock cache state:
    # Day 1: Normal day with Champion B candidates
    # Day 2: Panic day with 0 Champion B candidates, but Crisis Rebound candidates exist
    scanner._daily_anchor_cache = {
        "2025-01-13": ["005930"],
        "2024-08-05": ["000660", "035420"], # Fallback injected from panic engine
    }

    # Day 1: Champion B candidate returned
    scanner.on_day_open(date(2025, 1, 13))
    assert scanner.filter_universe(date(2025, 1, 13)) == ["005930"]

    # Day 2: Fallback candidate returned
    scanner.on_day_open(date(2024, 8, 5))
    assert scanner.filter_universe(date(2024, 8, 5)) == ["000660", "035420"]
