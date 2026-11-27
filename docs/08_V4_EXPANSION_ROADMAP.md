# Korea Market v4 — 향후 확장 아키텍처 로드맵 (Multi-Strategy / Multi-Broker / Multi-Account)

> **목적**: 현재 단일 전략(`all_weather`) + 단일 증권사(`KIS`) + 단일 계좌 기반의 `v4` 통합 아키텍처를, 향후 **① 다중 전략**, **② 타 증권사 API**, **③ 다중 계좌 동시 주문**으로 무리 없이 확장하기 위한 구조적 가이드라인을 기록합니다.

---

## 1. 현재 `v4` 구조 진단 (기준선)

`v4`는 이미 6대 포트(`v4/interfaces.py`)를 통해 전략/실행 루프(`v4/runner.py`, `v4/strategy/`)와 외부 I/O 어댑터(`v4/adapters/`)를 분리해 두었습니다.

| 확장 축 | 현재 강점 | 추후 해소할 결합점 |
|---|---|---|
| **다중 전략** | `IScannerComponent + IEntryComponent + IExitComponent` 레고 블록 조립 | `v4/cli.py`가 `all_weather`만 허용, `IConditionSearchPort`가 KIS HTS 조건검색(`seq="0"/"1"`) 중심 |
| **타 증권사 API** | KIS REST/WS 구현이 `v4/adapters/kis/`에 완전 격리됨 | `market_models.py`의 KIS 특화 필드(`is_nxt_eligible`, `seq`), 전략 내부의 `krx_ticks` 직접 호출 |
| **다중 계좌 주문** | 주문/포지션 소유권이 `IBrokerPort`에 단일화됨 | `UnifiedTradingRunner`가 `1 Runner = 1 Broker`로 1:1 결합됨 |

---

## 2. 3대 확장 축별 아키텍처 방향

### 축 1. 다중 전략 (Multi-Strategy)
1. **전략 레지스트리 복원 (`v4/strategy/registry.py`)**
   - `(scanner, entry, exit)` 조합을 이름으로 등록·조립하고, `v4/cli.py`의 `all_weather` 고정 검사를 제거합니다.
2. **스캔/유니버스 포트 일반화**
   - HTS 조건검색(`IConditionSearchPort`) 외에도 전종목 스캔, 거래대금 상위, 일봉 스윙 전략을 수용할 수 있도록 `IUniversePort` / `IScreenerPort`를 도입하고 전략이 필요한 포트만 선택적으로 주입받습니다.
3. **공유 피드 + 멀티 러너 (`1 Shared Feed -> N Runners`)**
   - 여러 전략이 하나의 `IRealtimeFeedPort`를 공유하고, 각 전략 인스턴스는 독립된 포지션/예산 슬롯(`strategy_id` 태깅)으로 병행 실행됩니다.

### 축 2. 타 증권사 API (Multi-Broker: 키움 / LS / 대신 등)
1. **어댑터 패키지 격리**
   - `v4/adapters/kis/`, `v4/adapters/kiwoom/`, `v4/adapters/ls/` 형태로 증권사별 `IBrokerPort`, `IRealtimeFeedPort`, `IRegimeDataPort` 구현체만 추가합니다. `v4/runner.py` 및 `v4/strategy/`는 수정하지 않습니다.
2. **증권사 특화 디테일 은닉**
   - `NXT(H0UNCNT0)/KRX(H0STCNT0)` 분기, `seq="0"/"1"`, TR 코드 등 증권사 고유 메커니즘은 어댑터 내부로 완전히 숨기고, 코어 모델은 보편적인 `Symbol`, `Quote`, `OrderRequest`만 다룹니다.
3. **호가/수수료 규칙의 포트화**
   - 향후 해외주식·선물·타 시장 확장 시 `core.krx_ticks.round_to_krx_tick()` 직접 호출 대신 `IMarketRules` 또는 `IBrokerPort.normalize_price()`로 위임합니다.

### 축 3. 다중 계좌 동시 주문 (Multi-Account Fan-out)
운영 목적에 따라 두 가지 패턴을 선택 또는 조합합니다.

- **패턴 A — 동일 전략 복제 주문 (`CompositeBroker(IBrokerPort)`, 우선 추천)**
  - 전략과 시세 판단은 **1번만** 수행하고, 브로커 레이어의 `CompositeBroker`가 N개의 하위 `IBrokerPort`(예: KIS 계좌 A + KIS 계좌 B + 키움 계좌 C)로 주문을 Fan-out 합니다.
  - 계좌별 자본 비중/고정금액 사이징(`AccountAllocator`)과 계좌별 `IRiskGuard`를 분리해, 특정 계좌의 주문 거부/예수금 부족이 다른 계좌 주문을 중단시키지 않도록 격리합니다.
  - **`UnifiedTradingRunner`나 전략 코드 수정 없이 브로커 교체만으로 즉시 적용 가능**합니다.

- **패턴 B — 계좌별 독립 전략 (`1 Shared Feed -> N Account Sessions`)**
  - 계좌마다 다른 전략이나 리스크 한도를 적용해야 할 때, 시세 피드 1개(WS 연결 한도 통합 관리)가 이벤트를 각 `(Strategy, RiskGuard, Broker)` 세션으로 브로드캐스트합니다.

---

## 3. 데이터 공유 원형 (`CurrentDayStore` -> `MarketDataBus`)
- 1-Pass Day-by-Day 백테스트의 `CurrentDayStore`(하루치 `DayBarIndex` 1개만 로드해 공유)는 위 **다중 전략 / 다중 계좌 동시 백테스트**의 최소 원형입니다.
- 향후 전략 N개와 계좌 M개를 동시에 시뮬레이션하더라도, 하루치 데이터를 디스크에서 1번만 읽어 `CurrentDayStore` / `MarketDataBus`로 공유함으로써 I/O와 메모리 증가를 억제할 수 있습니다.

---

## 4. 단계별 추진 로드맵
1. **Phase 0 (완료/기반 정비)**: `DayIndexCache(max_days=...)` 제거 → 단일 날짜 홀더 `CurrentDayStore` 정착, `v4` 단일 패스 백테스트 및 KIS 실전 어댑터 안정화.
2. **Phase 1 (다중 전략)**: `v4/strategy/registry.py` 신설 및 2번째 전략 파일럿 추가 + 14대 불변량 감사(`audit`) 통과.
3. **Phase 2 (다중 계좌)**: `CompositeBroker(IBrokerPort)` 및 계좌별 사이징/장애 격리 어댑터 구현.
4. **Phase 3 (다중 증권사/피드 버스)**: `1 Shared Feed -> N Runners` 이벤트 브로드캐스터 도입 및 타 증권사 어댑터 추가.
