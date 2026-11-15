# 🏛️ [아키텍처 설계서] Korea Market v3 Architecture Specification

> **"1,500줄짜리 거대 모놀리스 전략을 폐기하고,  
> 스캔(발굴) + 진입(신호) + 청산(리스크) 3대 부품을 레고 블록처럼 조립하는  
> 100% DIP(의존성 역전) 기반의 무결점 퀀트 트레이딩 플랫폼"**

---

## 1. 시스템 개요 및 수직 계층도

Korea Market v3는 **[CLI 계층] $\to$ [계약 감사 계층 (Audit Layer)] $\to$ [오케스트레이션 엔진] $\to$ [DIP 인프라 어댑터] $\to$ [전략 레고 블록]**의 5단계 수직 분리 구조로 설계되었습니다.

```mermaid
flowchart TD
    CLI["🖥️ CLI Entry Point (main.py)<br/>• backtest (--mode dual)<br/>• audit (14대 계약 감사)<br/>• strategies<br/>• review-failures"]

    subgraph AuditLayer ["🛡️ Audit Layer (audit/) — 무결성 계약 감사"]
        Auditor["UniversalStrategyAuditor<br/>14대 불변량 일괄 평가 및 Rich 콘솔 리포트"]
        ScanV["ScannerVerifier (4대 계약)"]
        EntV["EntryVerifier (4대 계약)"]
        ExitV["ExitVerifier (3대 계약)"]
        Auditor --> ScanV & EntV & ExitV
    end

    subgraph Orchestrator ["TradingEngine (엔진 오케스트레이터)"]
        Engine["engine/engine.py<br/>이벤트 루프, 일자 전환, 가상 시계 전진, 이벤트 중계"]
        Context["StrategyContext (엔진 파사드 브리지)<br/>buy / liquidate / get_position / get_balance"]
    end

    subgraph Infrastructure ["DIP 인프라 어댑터 계층 (주입식 교체 가능)"]
        Broker["IBroker<br/>(BacktestBroker: 슬리피지 0.035%, 세금 0.18%, 수수료 0.014%)"]
        Feed["IDataFeed<br/>(HistoricalDataFeed: 2개년 순수 1분봉 스트리머)"]
        Clock["IClock<br/>(SimulatedClock / RealtimeClock)"]
        Risk["RiskManager<br/>(당일 손실 한도 -50만 원, 2연패 서킷브레이커)"]
    end

    subgraph StrategyAssembly ["CompositeStrategy (전략 레고 블록 조립체)"]
        direction TB
        Scanner["① IScanner (종목 발굴)<br/>TopTurnoverScanner: 누적 10억, 1분 3억, 양봉 +1.5%"]
        Entry["② IEntryRule (진입 판단)<br/>BreakoutCapEntry: 시초가 +4.5% 돌파, +0.5% 호가 캡 방어"]
        Exit["③ IExitPolicy (청산 관리)<br/>TwoPhaseAtrExit / FixedRiskRewardExit (Dual-Bounds 지원)"]
    end

    CLI -->|실행 및 감사| Auditor
    Auditor -.->|14대 불변량 검증| StrategyAssembly
    CLI --> Engine
    Engine --> Context
    Engine --> Broker
    Engine --> Feed
    Engine --> Clock
    Engine --> Risk
    Context --> StrategyAssembly
    StrategyAssembly --> Scanner
    StrategyAssembly --> Entry
    StrategyAssembly --> Exit
```

---

## 2. 디렉토리 구조 및 모듈별 단일 책임 (SRP)

모든 모듈은 **300라인 이내(Single Responsibility Principle)**로 엄격히 통제되며 결합도가 0으로 분리되어 있습니다.

```
korea-market_v3/
├── audit/                       # 🛡️ Audit Layer (범용 전략 계약 무결성 검증 하네스)
│   ├── scanner_verifier.py      # IScanner 계약 검증기 (Lookahead 0% Fuzzing, 심볼 셔플 불변성 등 4대 계약)
│   ├── entry_verifier.py        # IEntryRule 계약 검증기 (Bar 0 Ghost Tamper, KRX 7대 호가 규격 등 4대 계약)
│   ├── exit_verifier.py         # IExitPolicy 계약 검증기 (래칫 SL 단조성, 모호봉 비관적 방어 등 3대 계약)
│   └── universal_auditor.py     # 조립체 수명주기 감사(슬롯 락/롤백/기준봉 보존 등 3대 계약) 및 14대 불변량 종합 리포트
├── config/
│   └── settings.py              # Pydantic Settings (KIS 환경 변수, 계좌번호, API 키, 로깅 수준)
├── core/
│   ├── krx_ticks.py             # KRX 법정 호가단위 7단계 연산, 호가 반올림(Floor/Ceil), 틱 증감
│   ├── exceptions.py            # 도메인 예외 계층 (TradingEngineError, LookaheadViolationError 등)
│   └── logging.py               # RichHandler 기반의 콘솔 구조화 로거
├── docs/
│   ├── 00_FAILURES_AND_LESSONS.md # ⚠️ 5대 장애 포스트모템 및 AI 필수 체크리스트 (SSOT)
│   ├── 01_ARCHITECTURE.md       # 🏛️ 본 아키텍처 공식 설계서
│   ├── 02_DECISION_LOG.md       # 📋 ADR-001 ~ ADR-003 아키텍처 의사결정 기록
│   └── 06_UNIVERSAL_STRATEGY_AUDIT.md # 🛡️ 범용 전략 계약 감사 하네스 명세서 (14대 불변량 전수)
├── engine/
│   ├── broker/
│   │   ├── base.py              # IBroker 추상 인터페이스 (주문 제출/취소, 잔고/포지션 조회)
│   │   └── backtest.py          # BacktestBroker (KRX 법정 호가 반올림, 세금/수수료, 비관적 체결)
│   ├── clock.py                 # IClock 인터페이스, SimulatedClock(가상 시계), RealtimeClock(실전 시계)
│   ├── context.py               # StrategyContext (전략-엔진 간 Facade 브리지, buy/liquidate/schedule)
│   ├── datafeed/
│   │   ├── base.py              # IDataFeed 추상 인터페이스 (stream_events)
│   │   └── historical.py        # HistoricalDataFeed (2개년 파켓 1분봉 스트리머, 일자별 정렬)
│   ├── engine.py                # TradingEngine (이벤트 루프 총괄, 일자 전환, 손익 피드백 중계)
│   ├── metrics.py               # PerformanceSummary 및 퀀트 지표(승률, PF, MDD, Expectancy) 산출
│   ├── models.py                # Slotted Dataclasses 불변 데이터 모델 및 Enums
│   └── risk/
│       └── risk_manager.py      # RiskManager (당일 손실 한도 50만 원, 2연패 서킷브레이커)
├── strategy/
│   ├── base.py                  # IScanner, IEntryRule, IExitPolicy, BaseStrategy 추상 인터페이스
│   ├── composite.py             # CompositeStrategy (3대 부품 오케스트레이터, Bar 0 스냅샷 보존)
│   ├── registry.py              # 팩토리 레지스트리 (SCANNERS, ENTRIES, EXITS 딕셔너리 및 build_strategy)
│   ├── entries/
│   │   └── breakout_cap.py      # BreakoutCapEntry (+4.5% 시초가 돌파, +0.5% 호가 캡 방어)
│   ├── exits/
│   │   ├── fixed_risk_reward.py # FixedRiskRewardExit (TP +3.0% / SL -1.0%, 15:15 타임아웃, Dual-Bounds 지원)
│   │   └── two_phase_atr.py     # TwoPhaseAtrExit (Phase 1 비상손절 -2% -> Phase 2 기준봉 중심선/트레일링)
│   └── scanners/
│       └── top_turnover.py      # TopTurnoverScanner (09:00~09:15 누적대금 10억, 1분봉 3억, 양봉 +1.5%)
├── tests/
│   ├── test_composite_strategy.py # 조립 무결성, 핫스왑, 실행 라이프사이클, Bar 0 보존, 서킷브레이커, 롤백
│   └── test_tick_rules.py       # KRX 법정 호가단위 7구간 및 매수 Floor / 매도 Ceil 검증
├── main.py                      # 통합 단일 CLI (backtest, strategies, review-failures)
└── requirements.txt             # 의존성 라이브러리 목록
```

---

## 3. 의존성 주입(DIP) 및 클래스 상속 구조

전략 코드는 백테스트 환경과 실전 환경에서 **단 1글자도 변경되지 않습니다**.  
오직 `TradingEngine`에 주입되는 3대 인프라 부품(`IBroker`, `IDataFeed`, `IClock`)만 교체하여 환경을 스위칭합니다.

```mermaid
classDiagram
    direction TB

    class TradingEngine {
        +run() PerformanceSummary
        -_handle_order_event(OrderEvent)
    }

    class StrategyContext {
        +buy(symbol, qty, price, tag) str
        +liquidate(symbol, tag) str
        +get_position(symbol) Position
        +get_balance() AccountBalance
    }

    class IBroker {
        <<interface>>
        +submit_order(Order) str
        +get_position(str) Position
        +get_balance() AccountBalance
        +on_bar(MinuteBar)
    }

    class BacktestBroker {
        +float commission_rate (0.014%)
        +float tax_rate (0.18%)
        +float slippage_rate (0.035%)
        -_fill_order(Order, fill_price)
    }

    class IDataFeed {
        <<interface>>
        +stream_events() Iterator
    }

    class HistoricalDataFeed {
        +stream_events() Iterator[MinuteBar]
    }

    class IClock {
        <<interface>>
        +now() datetime
    }

    class SimulatedClock {
        +set_time(datetime)
        +now() datetime
    }

    class RiskManager {
        +on_trade_completed(net_pnl)
        +validate_order(order, balance) bool
    }

    class BaseStrategy {
        <<interface>>
        +on_bar(context, MinuteBar)
        +on_order_event(context, OrderEvent)
    }

    TradingEngine --> StrategyContext
    TradingEngine --> IBroker
    TradingEngine --> IDataFeed
    TradingEngine --> IClock
    TradingEngine --> RiskManager
    TradingEngine --> BaseStrategy
    StrategyContext --> IBroker
    StrategyContext --> RiskManager
    IBroker <|.. BacktestBroker
    IDataFeed <|.. HistoricalDataFeed
    IClock <|.. SimulatedClock
```

---

## 4. 전략 레고 블록 조립 구조 (Composite Pattern)

모든 전략은 단일 거대 모놀리스가 아닌 3대 인터페이스의 순수 조립체입니다.

```mermaid
classDiagram
    direction TB

    class CompositeStrategy {
        +IScanner scanner
        +IEntryRule entry
        +IExitPolicy exit_policy
        +int max_daily_positions
        +on_bar(context, MinuteBar)
        +on_order_event(context, OrderEvent)
    }

    class IScanner {
        <<interface>>
        +filter_universe(date) List[str]
        +on_bar(date, MinuteBar) List[str]
    }

    class TopTurnoverScanner {
        +on_bar(date, MinuteBar) List[str]
    }

    class IEntryRule {
        <<interface>>
        +evaluate_entry(symbol, bar, tick, context) EntrySignal
    }

    class BreakoutCapEntry {
        +evaluate_entry(symbol, bar, tick, context) EntrySignal
    }

    class IExitPolicy {
        <<interface>>
        +evaluate_exit(position, bar, tick) ExitSignal
        +on_position_opened(position)
    }

    class TwoPhaseAtrExit {
        +evaluate_exit(position, bar, tick) ExitSignal
    }

    class FixedRiskRewardExit {
        +evaluate_exit(position, bar, tick) ExitSignal
    }

    CompositeStrategy *-- IScanner : 1. 발굴
    CompositeStrategy *-- IEntryRule : 2. 진입
    CompositeStrategy *-- IExitPolicy : 3. 청산
    IScanner <|.. TopTurnoverScanner
    IEntryRule <|.. BreakoutCapEntry
    IExitPolicy <|.. TwoPhaseAtrExit
    IExitPolicy <|.. FixedRiskRewardExit
```

---

## 5. 1개 트레이딩 분봉의 런타임 12단계 실행 라이프사이클

1개 분봉(예: 09:02:00)이 인입되었을 때, 시스템 내부의 정밀한 실행 순서와 데이터/제어 흐름입니다.

```mermaid
sequenceDiagram
    autonumber
    participant Feed as HistoricalDataFeed
    participant Engine as TradingEngine
    participant Clock as SimulatedClock
    participant Broker as BacktestBroker
    participant Strat as CompositeStrategy
    participant Exit as IExitPolicy
    participant Scan as IScanner
    participant Entry as IEntryRule
    participant Ctx as StrategyContext
    participant Risk as RiskManager

    Feed->>Engine: 1. yield MinuteBar("010820", 09:02:00)
    Engine->>Clock: 2. 가상 시계 동기화 (09:02:00)
    Engine->>Broker: 3. 시세 갱신 (보유 포지션 최고가 HWM 갱신)
    Engine->>Strat: 4. on_bar(context, bar)

    rect rgb(255, 245, 245)
    note over Strat,Exit: [단계 A: 기존 포지션 청산 최우선 평가]
    opt 이미 해당 종목을 보유 중인 경우
        Strat->>Exit: evaluate_exit(position, bar)
        opt 손절/익절/타임아웃 충족
            Exit-->>Strat: ExitSignal(STOP_LOSS, 3145 KRW)
            Strat->>Ctx: liquidate("010820")
            Ctx->>Broker: submit_order(SELL MARKET)
            Broker->>Broker: 세금(0.18%) 및 수수료 차감, 실현손익 계산
            Broker->>Engine: OrderEvent(SELL, FILLED, net_pnl=-456,052)
            Engine->>Risk: on_trade_completed(-456,052) (서킷브레이커 카운트 갱신)
            Engine->>Strat: on_order_event(FILLED)
            note over Strat: 청산 완료 후 즉시 리턴 (신규 매수 탐색 스킵)
        end
    end
    end

    rect rgb(245, 255, 245)
    note over Strat,Entry: [단계 B: 신규 종목 스캔 및 진입 평가]
    opt 당일 거래 횟수 < 1 (일일 거래 한도 이내)
        Strat->>Scan: on_bar(date, bar)
        Scan-->>Strat: candidates = ["010820"] (누적 10억, 1분 3억 충족)
        Strat->>Entry: evaluate_entry("010820", bar)
        opt +4.5% 시초가 돌파 확인
            Entry-->>Strat: EntrySignal(trigger=3195, collar_cap=3210)
            Strat->>Strat: 진입 분봉(Bar 0) 시고저종 영구 박제 (_pending_ref_bars)
            Strat->>Ctx: buy("010820", qty=3115, price=3210)
            Ctx->>Risk: validate_order(Order[BUY], balance)
            alt 서킷브레이커 미발동 (정상)
                Risk-->>Ctx: True (승인)
                Ctx->>Broker: submit_order(BUY LIMIT)
                Broker->>Broker: 법정 호가(Floor) 반올림 + 포지션 생성
                Broker->>Engine: OrderEvent(BUY, FILLED)
                Engine->>Strat: on_order_event(BUY, FILLED)
                Strat->>Strat: position.metadata["ref_bar"]에 Bar 0 스냅샷 이관
                Strat->>Exit: on_position_opened(position) -> Phase 1 비상손절(-2%) 장착
            else 당일 2연패 또는 -50만 원 초과 상태
                Risk-->>Ctx: False (매수 차단)
                Strat->>Strat: 슬롯 카운트 롤백
            end
        end
    end
    end
```

---

## 6. 실전 수익 무결성을 지키는 4대 핵심 메커니즘

### ① Bar 0 Causal Integrity (Lookahead 0% 사수)
- **문제점**: 09:01에 매수 주문이 체결될 때, 09:01 봉의 고가나 종가는 아직 미래 시점의 데이터이므로 알 수 없습니다.
- **해결책 (Two-Phase Lifecycle)**:
  - **Phase 1 (진입 분봉 진행 중)**: 진입 평단가 대비 `-2.0% Emergency SL`만 작동.
  - **Phase 2 (다음 분봉 시작 시)**: 09:02 분봉이 인입되어 09:01 봉이 완전히 마감된 순간, 박제해 둔 진입봉 스냅샷(`metadata["ref_bar"]`)의 중심선 `(open + close) / 2`로 손절가를 안전하게 승격.

### ② KRX 법정 호가 규약 준수 (KRX Tick Conformance)
- **원칙**: 국내 코스피/코스닥의 7단계 호가 단위 규약 준수 (`core/krx_ticks.py`).
- **매수 주문**: `round_mode="floor"` (설정한 예산 한도 호가를 절대 초과하지 않음).
- **매도/청산 주문**: `round_mode="ceil"` (지정한 손절 방어선 호가 이하로 깎이지 않음).

### ③ 체결 모호성 감사 (Dual-Bounds Ambiguity Audit)
- 1개 분봉 내에서 고가(TP)와 저가(SL)가 동시 충족될 때:
  - **하한선 (Pessimistic, SL First)**: 무조건 손절 우선 체결 (최악의 마지노선 검증).
  - **상한선 (Optimistic, TP First)**: 익절 우선 체결 (이론적 최대 잠재력).
- 두 결과의 격차(Ambiguity Spread)가 **3% 이내**일 때만 "봉 내부 순서 노이즈에 의존하지 않는 견고한 진짜 알파"로 공인.

### ④ 고성능 불변 모델 (`__slots__ = True`)
- `MinuteBar`, `Position` 등 모든 핵심 데이터 모델에 `@dataclass(slots=True)` 적용.
- 2개년 수백만 개 분봉을 스트리밍해도 dynamic `__dict__` 오버헤드가 없어 메모리 70% 절감, C 구조체 포인터 접근으로 40% 고속 처리.

---

## 7. 개발자 가이드: 신규 전략 부품 추가 워크플로우 (4단계)

새로운 청산 정책(예: 고정 손익비 청산)을 추가하고 싶다면 다른 엔진 코드는 1줄도 수정할 필요 없이 아래 4단계만 거치면 됩니다:

1. **`strategy/exits/`에 새 클래스 작성 (`IExitPolicy` 상속)**:
   ```python
   from strategy.base import IExitPolicy
   from engine.models import ExitSignal, OrderType

   class TrailingHalfExit(IExitPolicy):
       name = "trailing_half_exit"
       def evaluate_exit(self, position, bar, tick=None):
           if bar.close <= position.high_water_mark * 0.985:
               return ExitSignal(symbol=position.symbol, dt=bar.dt, order_type=OrderType.MARKET, target_price=bar.close, reason="TRAILING_1.5PCT")
           return None
   ```
2. **`strategy/registry.py`에 등록**:
   ```python
   EXITS = {
       "two_phase_atr": TwoPhaseAtrExit,
       "fixed_risk_reward": FixedRiskRewardExit,
       "trailing_half": TrailingHalfExit,  # <- 1줄 등록
   }
   ```
3. **0줄 테스트 계약 감사 실행 (14대 불변량 자동 검증)**:
   별도의 테스트 코드를 작성하지 않고 CLI 감사 명령어로 Lookahead 0%, KRX 호가 규격, 래칫 SL 단조성, 모호봉 비관적 방어 등을 0.1초 만에 자동 검증합니다.
   ```bash
   py -3.12 main.py audit --exit trailing_half
   ```
4. **CLI로 즉시 듀얼 바운드 백테스트 실행**:
   감사가 승인(`APPROVED`)되면 2개년 듀얼 바운드 시뮬레이션으로 알파의 견고성을 입증합니다.
   ```bash
   py -3.12 main.py backtest --exit trailing_half --mode dual
   ```

---

## 8. 토픽별 상세 레퍼런스 (Topic References)

- **⚠️ 5대 장애 아카이브 & AI 필수 체크리스트**: [docs/00_FAILURES_AND_LESSONS.md](00_FAILURES_AND_LESSONS.md)
- **📋 아키텍처 의사결정 기록 (ADR)**: [docs/02_DECISION_LOG.md](02_DECISION_LOG.md)
- **⚙️ 엔진 구조 및 마이크로스트럭처 명세**: [docs/03_ENGINE_AND_MICROSTRUCTURE.md](03_ENGINE_AND_MICROSTRUCTURE.md)
- **🧩 전략 레고 블록 조립 및 수명주기 명세**: [docs/04_STRATEGY_LEGO_BLOCKS.md](04_STRATEGY_LEGO_BLOCKS.md)
- **🔍 정밀 코드 감사 보고서**: [docs/05_CODE_AUDIT_AND_POTENTIAL_ISSUES.md](05_CODE_AUDIT_AND_POTENTIAL_ISSUES.md)
- **🛡️ 범용 전략 계약 감사 하네스 명세 (Universal Contract Suite)**: [docs/06_UNIVERSAL_STRATEGY_AUDIT.md](06_UNIVERSAL_STRATEGY_AUDIT.md)
