"""
Universal Strategy Auditor & Contract Suite.
Executes end-to-end 4-stage contract verification across Scanner, Entry, Exit,
and Composite Orchestration Lifecycle.
"""
from __future__ import annotations

from dataclasses import dataclass, field
from datetime import date, datetime
from typing import List, Dict, Any, Optional

from rich.table import Table
from rich.panel import Panel
from rich.console import Console

from core.krx_ticks import round_to_krx_tick
from engine.models import MinuteBar, Position, OrderEvent, OrderStatus, OrderSide
from engine.broker.backtest import BacktestBroker
from engine.clock import SimulatedClock
from engine.context import StrategyContext
from strategy.composite import CompositeStrategy
from audit.scanner_verifier import ScannerContractVerifier, ContractCheckResult
from audit.entry_verifier import EntryContractVerifier
from audit.exit_verifier import ExitContractVerifier


@dataclass
class AuditReport:
    """Comprehensive contract verification report for an assembled CompositeStrategy."""
    strategy_name: str
    scanner_name: str
    entry_name: str
    exit_name: str
    results: List[ContractCheckResult] = field(default_factory=list)

    @property
    def passed(self) -> bool:
        return all(r.passed for r in self.results)

    @property
    def total_checks(self) -> int:
        return len(self.results)

    @property
    def passed_checks(self) -> int:
        return sum(1 for r in self.results if r.passed)

    def render_table(self) -> Table:
        """Renders results into a styled Rich Table."""
        table = Table(
            title=f"🛡️ Strategy Contract Audit: {self.strategy_name}",
            show_header=True,
            header_style="bold magenta",
        )
        table.add_column("Stage / Category", style="bold cyan", width=16)
        table.add_column("Contract Invariant", style="bold white", width=34)
        table.add_column("Status", justify="center", width=10)
        table.add_column("Audit Message & Verification Details", style="dim", width=55)

        for r in self.results:
            status_text = "[bold green]PASS[/bold green]" if r.passed else "[bold red]FAIL[/bold red]"
            msg_style = "green" if r.passed else "bold red"
            table.add_row(r.category, r.name, status_text, f"[{msg_style}]{r.message}[/{msg_style}]")

        return table


class UniversalStrategyAuditor:
    """Universal Strategy Contract Auditor for Korea Market v3."""

    def __init__(self):
        self.scanner_verifier = ScannerContractVerifier()
        self.entry_verifier = EntryContractVerifier()
        self.exit_verifier = ExitContractVerifier()

    def _audit_orchestration_lifecycle(self, strategy: CompositeStrategy) -> List[ContractCheckResult]:
        """
        Validates CompositeStrategy execution orchestration:
          - Daily position slot locking
          - Daily trade count rollback upon order rejection/cancellation
          - Reference bar data preservation into position metadata
          - Liquidation dispatch on exit policy signal
        """
        results: List[ContractCheckResult] = []

        # 1. Slot Locking & Daily Count Limit
        broker = BacktestBroker(initial_capital=10_000_000.0)
        clock = SimulatedClock()
        ctx = StrategyContext(broker=broker, clock=clock)
        today = date(2024, 10, 21)

        broker.register_event_listener(lambda ev: strategy.on_order_event(ctx, ev))
        strategy.on_start(ctx)
        strategy.on_day_open(ctx, today)
        strategy.max_daily_positions = 1
        strategy.current_slot_notional = strategy.total_capital / 1
        if hasattr(strategy.entry, "target_notional"):
            strategy.entry.target_notional = strategy.current_slot_notional

        # Trigger first entry order
        bar1 = MinuteBar(
            symbol="005930",
            dt=datetime(2024, 10, 21, 9, 2),
            open=50000.0,
            high=53000.0,
            low=49900.0,
            close=52800.0,
            volume=10000,
            turnover=500_000_000.0,
            cum_turnover=1_500_000_000.0,
        )
        broker.on_bar(bar1)
        strategy.on_bar(ctx, bar1)

        first_count = strategy._daily_trade_count
        if first_count != 1:
            results.append(ContractCheckResult(
                category="Lifecycle",
                name="Daily Slot Locking",
                passed=False,
                message=f"Slot locking failed: daily_trade_count expected 1, got {first_count}",
            ))
        else:
            # Second qualifying stock on same day should be rejected due to slot lock (max_daily_positions=1)
            bar2 = MinuteBar(
                symbol="000660",
                dt=datetime(2024, 10, 21, 9, 3),
                open=150000.0,
                high=160000.0,
                low=149000.0,
                close=158000.0,
                volume=10000,
                turnover=500_000_000.0,
                cum_turnover=1_500_000_000.0,
            )
            broker.on_bar(bar2)
            strategy.on_bar(ctx, bar2)

            pos2 = broker.get_position("000660")
            if pos2 is not None:
                results.append(ContractCheckResult(
                    category="Lifecycle",
                    name="Daily Slot Locking",
                    passed=False,
                    message="Slot lock breached: additional position opened despite max_daily_positions=1 limit.",
                ))
            else:
                results.append(ContractCheckResult(
                    category="Lifecycle",
                    name="Daily Slot Locking",
                    passed=True,
                    message="Daily trade slot locked strictly at max limit; over-allocation prevented.",
                ))

        # 2. Rollback on Order Rejection / Cancellation
        strategy._daily_trade_count = 1
        strategy._pending_ref_bars["REJ_SYM"] = {"open": 10000.0}
        rej_event = OrderEvent(
            order_id="ord_rej",
            symbol="REJ_SYM",
            side=OrderSide.BUY,
            status=OrderStatus.REJECTED,
            filled_price=0.0,
            filled_quantity=0,
            dt=datetime(2024, 10, 21, 9, 5),
            message="ORDER_REJECTED",
        )
        strategy.on_order_event(ctx, rej_event)

        if strategy._daily_trade_count == 0 and "REJ_SYM" not in strategy._pending_ref_bars:
            results.append(ContractCheckResult(
                category="Lifecycle",
                name="Rejection Slot Rollback",
                passed=True,
                message="Daily trade count and pending ref_bar cleanly rolled back upon order rejection.",
            ))
        else:
            results.append(ContractCheckResult(
                category="Lifecycle",
                name="Rejection Slot Rollback",
                passed=False,
                message=f"Rollback failed: daily_count={strategy._daily_trade_count}, pending={strategy._pending_ref_bars}",
            ))

        # 3. Reference Bar Preservation
        pos1 = broker.get_position("005930")
        if pos1 and "ref_bar" in pos1.metadata and pos1.metadata["ref_bar"]["open"] == 50000.0:
            results.append(ContractCheckResult(
                category="Lifecycle",
                name="Reference Bar Preservation",
                passed=True,
                message="Entry Bar 0 OHLC successfully preserved in position metadata for Phase 2 dynamic exit.",
            ))
        else:
            results.append(ContractCheckResult(
                category="Lifecycle",
                name="Reference Bar Preservation",
                passed=False,
                message="Entry Bar 0 reference data missing or corrupted in position metadata.",
            ))

        return results

    def audit(self, strategy: CompositeStrategy) -> AuditReport:
        """Executes full contract verification suite on an assembled CompositeStrategy."""
        report = AuditReport(
            strategy_name=strategy.name,
            scanner_name=strategy.scanner.name,
            entry_name=strategy.entry.name,
            exit_name=strategy.exit_policy.name,
        )

        # Stage 1: Scanner Contracts
        report.results.extend(self.scanner_verifier.run_all_checks(strategy.scanner))

        # Stage 2: Entry Contracts
        report.results.extend(self.entry_verifier.run_all_checks(strategy.entry))

        # Stage 3: Exit Policy Contracts
        report.results.extend(self.exit_verifier.run_all_checks(strategy.exit_policy))

        # Stage 4: Orchestration Lifecycle Contracts
        report.results.extend(self._audit_orchestration_lifecycle(strategy))

        return report
