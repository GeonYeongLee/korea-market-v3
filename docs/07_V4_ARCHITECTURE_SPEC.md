# Korea Market v4 — Unified Ports & Adapters Architecture Specification

> **Single Strategy, Single Event Loop, Zero Live-vs-Backtest Gap.**  
> `v4` replaces `v3`'s dual execution paths (`TradingEngine` vs `RealtimeLeaderScanner` + `live_sync.py`) with one unified `asyncio` runner calling strictly abstract ports (`v4/interfaces.py`) and synchronous `<= 300`-line Lego-block strategy components.

---

## 1. Module Layout & `main.py` / `core/` Integration

```text
korea-market_v4/
├── main.py                              # Single CLI entrypoint (wires to v4.cli)
├── config/settings.py                   # Reused KIS VTS/REAL endpoints & credentials
├── core/                                # Reused utilities (auth, rate_limiter, krx_ticks, krx_symbols, dynamic_features, market_regime, telemetry)
├── audit/                               # Existing 14-invariant Contract Suite (preserved)
└── v4/                                  # Clean v4 Ports & Adapters package (every file <= 300 lines; models <= 200)
    ├── __init__.py                      # Public exports of v4 models & interfaces
    ├── market_models.py                 # MarketIndexRegime, SymbolFeatures, MinuteBar, ConditionCandidate, IntradaySnapshot, MarketEvents
    ├── order_models.py                  # Enums, EntrySignal, ExitSignal, OrderRequest, OrderEvent, Position, AccountBalance, TradeRecord
    ├── models.py                        # Re-export barrel + MarketEvent = Union[MinutePulseEvent, TickEvent, BarCloseEvent]
    ├── interfaces.py                    # 6 Ports (incl. IRiskGuard) + 3 synchronous Strategy Component ABCs
    ├── risk.py                          # Concrete IRiskGuard circuit breaker (-500K daily loss / 2 consecutive losses)
    ├── runner.py                        # Single unified async trading loop (Live & Backtest)
    ├── cli.py                           # CLI handlers (backtest, mock, live, routine, audit)
    ├── strategy/                        # All-Weather Lego blocks (sync, <= 300 lines each)
    │   ├── scanner.py                   # IScannerComponent (gap, KRX MAE, same-bar turnover)
    │   ├── entry.py                     # IEntryComponent (Bar 0 causal breakout + collar cap)
    │   ├── exit.py                      # IExitComponent (Phase 1 Bar 0 SL, Phase 2+ ratchet SL/TP & 15:21 EOD)
    │   └── composite.py                 # All-Weather assembly + audit/ compatibility facade
    └── adapters/
        ├── backtest/                    # Direct parquet simulation (historical.py unused)
        └── kis/                         # Production KIS REST + H0UNCNT0/H0STCNT0/H0STCNI0/9 adapters
```

---

## 2. Port Contracts, Concurrency & Single Canonical Execution Stream

| Interface | Sync / Async | Responsibility | Backtest Impl | Live KIS Impl |
| :--- | :--- | :--- | :--- | :--- |
| **`IClock`** | Sync + Async `sleep` | KST time & loop pacing | Virtual clock (`sync_time`) | Wall-clock (`asyncio.sleep`) |
| **`IRegimeDataPort`** | Async I/O | Index regime, `market_open/close_time`, `t-1` features | `index_daily` + `daily_bars_2y` | `FHKUP03500100` + `daily_bars_2y` |
| **`IConditionSearchPort`** | Async I/O | HTS scan + `as_of` snapshot + regular 1m bars | Causal parquet emulator (`<= as_of`) | `HHKST03900400` + `FHKST01010100`/`03010200` |
| **`IRealtimeFeedPort`** | Async stream | Subscribed pool + `MarketEvent` pump (no `OrderEvent`) | `MinutePulse` + `BarClose` | `H0UNCNT0`/`H0STCNT0` + `Tick` + `BarClose` |
| **`IBrokerPort`** | Sync + Async order | Execution owner, `Position`, `OrderEvent` queue, cost/mode | `BacktestBroker` | `KisLiveBroker` (`H0STCNI0`/`9` + CCLD) |
| **`IRiskGuard`** | Sync pure | Daily loss & consecutive-loss circuit breaker | Identical `RiskGuard` | Identical `RiskGuard` |
| **`IScanner/Entry/Exit`** | Sync pure | Gap/MAE/same-bar, breakout, Phase 1..5 exit | Identical code | Identical code |

### Concurrency & Single Canonical `OrderEvent` Delivery Path
1. **Single-Threaded `asyncio` Loop**: `v4/runner.py` mutates strategy, risk, and broker state on one event loop thread (blocking KIS REST calls in `v4/adapters/kis/` execute via `await asyncio.to_thread(...)`).
2. **No Duplicate `OrderEvent`s**: `IRealtimeFeedPort.stream_events()` yields **only** `MarketEvent` (`MinutePulseEvent | TickEvent | BarCloseEvent`). `IBrokerPort` is the sole owner of execution state: in live, `H0STCNI0`/`H0STCNI9` execution notices route directly to `broker.handle_execution_notice()` (and `sync_open_orders()` / CCLD fallback), pushing every `OrderEvent` into a single `asyncio.Queue` drained by `runner.py`.
3. **Shared Mutable `Position` Semantics**: `IBrokerPort.get_position()` / `get_positions()` return the broker-owned mutable `Position` instances so synchronous `IExitComponent` mutations (`phase`, `stop_loss_price`, `take_profit_price`, `high_water_mark`, `metadata`) persist identically across backtest and live without thread races.
4. **`IRiskGuard` Feedback**: `IBrokerPort` populates `OrderEvent.net_pnl` on every `SELL` `FILLED` event (`gross_pnl - sell_comm - sell_tax - pro_rata_buy_comm`) in both backtest and live, and `runner.py` invokes `risk_guard.on_trade_completed(event.net_pnl)`.

---

## 3. Bar 0 Causal State Machine & `execution_mode` Parity

```mermaid
flowchart TD
    Pulse["MinutePulseEvent(dt=M) -> poll_candidates + snapshot/bars + subscribe(is_nxt)"] --> CheckPos{"Position exists<br/>prior to M?"}
    CheckPos -->|No| Entry["Eval Bar 0 Entry (NEVER reads bar.close)<br/>pessimistic: bar_M.low in MAE | optimistic: prior_krx_day_low only"]
    Entry -->|BUY Filled, phase=1| Phase1{"Bar 0 Exit Check<br/>IExitComponent(phase=1)"}
    Phase1 -->|pessimistic & bar(O>SL:L, else:C)/tick <= SL| EmgSL["GRACE_EMERGENCY_SL ONLY<br/>-> SELL MARKET"]
    Phase1 -->|optimistic OR intact| SyncBar0["End of Bar M: ref_bar = bar_M<br/>phase = 2, SL = max(SL, center_sl, floor_sl)"]
    CheckPos -->|Yes, entry_min < M| Phase2["Phase 2+: LOCKED_SL / SMART_STEP_UP / 15:21 TIMEOUT"]
```

### Mandatory Execution Flows (Minute `M`, `BarCloseEvent.dt = HH:MM:00`)
- **`IExitComponent` Phase 1 Rule**: Whenever `position.phase == 1`, `evaluate_exit()` **MUST evaluate `GRACE_EMERGENCY_SL` ONLY** (never TP, Super Leader promotion, or body-center SL inside Bar 0; skipped when `execution_mode == "optimistic"`).
- **Backtest Flow for Minute `M`**:
  1. `MinutePulseEvent(dt=M, is_minute_boundary=True)` triggers `poll_candidates(as_of=M, seq=...)` + snapshot/recent-bar verification + `feed.subscribe(sym, is_nxt=...)`.
  2. `BarCloseEvent(dt=M, bar=bar_M)` (excluding premarket rows `< market_open_time` / `is_premarket=True`):
     - **No prior position**: evaluate entry without `bar_M.close` (`pessimistic` checks `bar_M.low` in MAE; `optimistic` checks `prior_krx_day_low` strictly `< M`). If filled on `bar_M`:
       1. Immediately run Phase 1 `GRACE_EMERGENCY_SL` ONLY on `bar_M` (`pessimistic`: check `bar_M.low <= stop_loss_price` when `bar_M.open > stop_loss_price`, else `bar_M.close <= stop_loss_price` ONLY to prevent false stopouts on green breakout candles; `optimistic`: skip).
       2. Immediately *after* Phase 1 check at end of `bar_M`, if still open: set `position.metadata["ref_bar"]` from `bar_M` and promote `position.phase = 2` (`stop_loss_price = max(stop_loss_price, center_sl, floor_sl)`). Bar `M+1` starts cleanly in Phase 2 with zero double-processing.
     - **Existing position (`entry_min < M`)**: run Phase 2+ `IExitComponent.evaluate_exit(pos, bar=bar_M)` (`pessimistic`: SL before TP; `optimistic`: TP before SL).
- **Live Flow for Minute `M`**:
  1. Sub-second `MinutePulseEvent(dt, is_minute_boundary=...)` polls HTS condition search.
  2. `TickEvent` updates forming bar `M`, evaluates Bar 0 entry (`bar.close` unused), and if `position.phase == 1` (`entry_min == M`), evaluates Phase 1 `GRACE_EMERGENCY_SL` ONLY (`tick.price <= stop_loss_price`).
  3. `BarCloseEvent(dt=M)` (including a wall-clock rollover flush at `M+1:00.1` so quiet symbols close on time) **ONLY** syncs `ref_bar` + promotes `phase = 2` when `entry_min == M` (never re-evaluates `bar_M.low`/`bar_M.high`), or evaluates Phase 2+ when `entry_min < M`.

---

## 4. HTS Condition Search, `seq="1"` Splice Safety & KRX/NXT Isolation

1. **Pure-Price HTS Filter (`HHKST03900400`)**: `0% <= gap <= +6.0%`, `KRX MAE >= -1.2%`, `1m high surge >= +3.5%`, **no `+1.5%` current-price floor** (preserves post-surge pullbacks).
2. **`seq` Switching & `seq="1"` Splice Guard**:
   - `M = market_open + 1m` (`09:01` / `10:01`) polls `seq="0"` (`n` surge); `M >= market_open + 2m` polls `seq="1"` (`n OR n-1` surge).
   - **Fast-Pass Rule**: Skipping REST snapshot/bars via `krx_cum_turnover >= dyn_to` is **ONLY allowed when `seq == "0"` or `curr_bar_surge_ok is True`**. When `seq == "1"` and `curr_bar_surge_ok is None` (live KIS cannot indicate whether `n` or `n-1` surged), `get_recent_minute_bars(symbol, as_of)` MUST verify same-bar `surge >= +3.5%` AND `cum_turnover >= dyn_to` before subscribing.
3. **Canonical `cum_turnover` & Premarket Isolation (Empirically Verified)**:
   - Parquet inspection (`daily/20261126/005930.parquet`, `006400.parquet`, `000020.parquet`, `intraday_2026.parquet`, and `engine/datafeed/historical.py`) confirms NXT stocks contain `08:00~08:49` rows (`is_premarket=True`; e.g. `005930` `pm_turnover=484.35B`, `pm_low=170,600` vs `09:00` `or_open=172,900` = `-1.33%`), while non-NXT stocks start at `09:00:00`.
   - **Canonical Turnover Rule**: `cum_turnover` uses integrated KRX+NXT cumulative turnover (inclusive of `08:00~08:50` premarket, matching `v3` `HistoricalDataFeed`, parquet `cum_to`, and KIS `"UN"` `acml_tr_pbmn`), while `premarket_turnover` is exposed separately on `IntradaySnapshot`.
   - **Strict Regular-Session Open/Low Isolation**: `day_open`, `krx_day_low`, and `prior_krx_day_low` MUST exclude `08:00~08:50` (`is_premarket == False` / `>= market_open_time`). In `get_intraday_snapshot` for live NXT stocks, `cum_turnover`/`cum_volume` come from `"UN"`, whereas `day_open`/`day_low`/`day_high` MUST come from `"J"` (`FHKST01010100`) or regular `>= market_open_time` bars (`FHKST03010200` `"UN"`->`"J"`, `vol > 0`). In `TickEvent`, `day_low` returns `krx_day_low` and NEVER falls back to `raw_day_low` for `H0UNCNT0` (only for `H0STCNT0`).
4. **WS Routing & 40-Symbol Cap**: `subscribe(symbol, is_nxt=None)` resolves `is_nxt` via `data/metadata/nxt_symbols.json` / `SymbolFeatures` / snapshot before selecting `H0UNCNT0` vs `H0STCNT0`, is idempotent, enforces a shared 40-symbol cap across both TRs, and NEVER evicts held positions or pending-order symbols.

---

## 5. Schedule Parity (`10:00` Opens, `15:20` Auction) & `v3` Cost/Tick/Sizing Parity

- **Late-Open (`10:00`) Parity Policy (`2026-01-02`, `2026-11-19`, `2025-11-13`)**:
  - Empirical check shows `10:00` open days have zero premarket rows (`2026-01-02`: `10:00~15:19`; `2026-11-19` & `2025-11-13`: `10:00~15:30`). In `v3`, `HistoricalDataFeed` & `TradingEngine` shifted cutoffs `+1h`, **but** `MorningLeaderScanner`/`MorningBreakoutEntry` remained pinned to `09:01~09:15` (0 trades).
  - `v4` propagates `MarketIndexRegime.market_open_time` / `market_close_time` into `on_day_open()` and defaults `shift_to_market_open=True` (`10:01~10:15` active in both live and backtest), with `shift_to_market_open=False` available for literal `v3` parity diffing.
- **Zero-Tick `15:20~15:30` Non-NXT Liquidation**: `stream_events()` continues emitting `MinutePulseEvent` until `market_close_time` (`15:30`); `runner.py` checks `hard_timeout` (`15:21`, or `< market_close_time`) on `MinutePulseEvent` so illiquid non-NXT positions liquidate during the `15:20~15:30` auction even with zero ticks.
- **Exact `v3` `BacktestBroker` Cost, Slippage, Tick & Sizing Parity**:
  - Commission: `0.00014` (both sides); Tax: `0.0018` (`SELL` only); `OrderEvent.net_pnl = (exit_p - entry_p) * qty - sell_comm - sell_tax - (entry_p * qty * 0.00014)`.
  - Adverse execution slippage `0.00035` + adverse tick rounding: `BUY` fills at `round_to_krx_tick(base * 1.00035, "BUY", "ceil")`; `SELL` fills at `round_to_krx_tick(base * 0.99965, "SELL", "floor")`.
  - `OrderRequest.price` (`MARKET`): live sends `ORD_DVSN="01"`, `ORD_UNPR="0"` (ignoring `price`); backtest uses `price > 0` (else `latest_prices[symbol]`) as `base`.
  - Strategy KRX tick rounding: `trigger_price` (`BUY ceil`), `collar_cap_price` (`BUY floor`, `>= trigger_price`), `stop_loss_price` / `take_profit_price` / `step2_price` (`SELL ceil`).
  - Sizing: `effective_cap = cash * 0.995 if cash < total_capital else total_capital` (`10M KRW` cap); `target_qty = int(slot_notional // (collar_cap_price * 1.002))`; `qty = min(target_qty, int((cash * 0.995) // collar_cap_price))`.

---

## 6. `audit/` Compatibility & Live Execution Safety Invariants

- **`audit/` 14-Invariant Compatibility**: `v4/strategy/` components use `getattr` for optional `MinuteBar`/`Position` fields, perform zero disk I/O in `on_day_open()`, share one pure evaluation helper between `on_bar()` and `evaluate_candidate()`, transition `phase 1 -> 2` cleanly when standalone `audit/` passes `bar_min > entry_min`, and expose `ambiguity_mode` + Stage-4 facade attributes (`_daily_trade_count`, `_pending_ref_bars`, `max_daily_positions`, `current_slot_notional`, `on_order_event`) on `v4/strategy/composite.py`.
- **Live KIS Safety (`v4/adapters/kis/`)**: never call deprecated `/uapi/hashkey`; abort retries on `rt_cd=="1"`; `1.0s` `inquire_daily_ccld` Phase-2 timeout recovery; AES `H0STCNI0`/`H0STCNI9` + early-execution reconciliation; rejection lockout (`>= 3`); stale buy cancel (`>= 45s`); `15:20~15:21` market liquidation (`zero overnight`).
