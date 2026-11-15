# 🔍 [코드 감사 및 잠재 이슈 명세서] Code Audit & Architectural Debt Registry

> **Korea Market v3 — Critical Analysis, Design Intentions & Pre-Flight Risk Matrix**  
> 본 문서는 시스템의 소스 코드를 라인 단위로 전수 검토하여 파악한 **설계자의 본래 의도(Design Intentions)**, **코드 구현상의 트레이드오프**, 그리고 **실전 매매(Live KIS) 전환 시 반드시 주의하거나 보완해야 할 잠재적 리스크/개선점(Known Issues & Architectural Debt)**을 투명하게 기록합니다.  
> *(본 문서의 항목들은 임의로 코드를 변경하지 않고, 시스템의 본래 의도와 한계를 보존하여 향후 안전한 개발을 도모하기 위해 작성되었습니다.)*

---

## 1. 전수 감사 항목 요약 매트릭스

| 번호 | 모듈 및 위치 | 핵심 쟁점 및 현상 | 설계자의 본래 의도 (Design Intent) | 잠재적 리스크 및 권장 조치 |
| :---: | :--- | :--- | :--- | :--- |
| **01** | `broker/backtest.py`<br/>`context.py` | `submit_order`의 즉시 동기 체결(0ms) | 백테스트 엔진 단순화 및 결정론적 재현성 확보 | 실전 비동기(REST ➔ WS 통보) 지연 시 중복 발주 주의 |
| **02** | `strategy/composite.py`<br/>`exits/two_phase_atr.py` | Bar 0 종가 스냅샷 시점 차이 | 백테스트 분봉 단위 마감 종가 박제 | 실전 틱 단타 시 봉 마감 동기화 훅(`OnBarClose`) 필요 |
| **03** | `datafeed/historical.py`<br/>`composite.py` | `sort_values(by=["dt", "symbol"])` | 일자별 시계열 완전 정렬 및 재현성 | 복수 종목 동시 돌파 시 심볼 코드 순서 편향 존재 |
| **04** | `scanners/top_turnover.py` | `body_pct = (close - open) / day_open` | 시초가 대비 실질 양봉 파동 정규화 의도 | 일반 분봉 시초가(`bar.open`)로 오인 수정 금지 |
| **05** | `scanners/top_turnover.py`<br/>`entries/breakout_cap.py` | 09:00 첫 틱 미체결 종목의 `day_open` | 첫 인입 분봉의 `bar.open`을 시초가로 저장 | VI 지연 개장 종목의 시초가 왜곡 주의 (마스터 참조 권고) |
| **06** | `engine/metrics.py` | 청산 완료 거래(`net_pnl`) 기반 MDD 산출 | 폐쇄 거래(Closed-Trade) 지분 곡선 표준 산출 | 포지션 보유 중 미실현 출렁임(Intra-Trade MAE) 미반영 |
| **07** | `datafeed/historical.py` | `_stream_day`의 `pd.concat` 결합 | 일자 단위 메모리 격리 및 제너레이터 스트리밍 | 2개년 전수 백테스트 시 디스크 I/O 최적화 여지 |

---

## 2. 세부 항목별 정밀 감사 보고

---

### 📌 [Audit-01] 동기식 체결 가정과 실전 비동기 주문 수명주기 갭
* **관련 코드**:
  * [`engine/broker/backtest.py`](file:///d:/repo/korea-market_v3/engine/broker/backtest.py) L88~L109 (`submit_order` 및 `_fill_order`)
  * [`engine/context.py`](file:///d:/repo/korea-market_v3/engine/context.py) L45~L63 (`buy`)
  * [`strategy/composite.py`](file:///d:/repo/korea-market_v3/strategy/composite.py) L106~L118
* **코드 현상**:
  * 백테스트 브로커는 `submit_order()`가 호출되면 그 즉시 내부에서 `_fill_order()`를 실행하여 포지션을 생성하고 반환합니다.
  * 따라서 `CompositeStrategy.on_bar()` L115에서 `context.buy()` 직후 `context.get_position(bar.symbol)`을 호출하면 이미 포지션 객체가 존재합니다.
* **설계자의 본래 의도**:
  * 백테스트 시 불필요한 스레드 비동기 대기(sleep/event loop)를 제거하고, 밀리초 단위 초고속 시뮬레이션을 수행하기 위한 표준 설계입니다.
* **실전 전환 시 잠재적 위험**:
  * 실전 KIS API에서는 `submit_order()`가 REST 접수 번호(`ODNO`)만 반환하며 주문 상태는 `PENDING`입니다.
  * 실제 체결은 수백 밀리초 후 체결통보 WebSocket(`H0STCNI0`)을 통해 비동기로 도착하므로, `context.buy()` 직후 `get_position()`은 `None`을 반환합니다.
* **보호 장치 및 가이드**:
  * 다행히 `CompositeStrategy.on_order_event()` (L133~L139)에 체결(`OrderStatus.FILLED`) 통보 시 `_pending_ref_bars`에서 스냅샷을 꺼내 포지션에 주입하도록 2중 안전장치가 설계되어 있습니다.
  * **[가이드]**: 향후 `KisLiveBroker` 작성 시, `context.buy()` 직후의 동기식 포지션 참조에 절대 의존하지 말고, 반드시 `on_order_event(FILLED)` 이벤트 핸들러를 통해서만 포지션 상태를 전이해야 합니다.

---

### 📌 [Audit-02] 분봉 백테스트와 틱 기반 실전 간의 '진입봉 종가(Close)' 참조 차이
* **관련 코드**:
  * [`strategy/composite.py`](file:///d:/repo/korea-market_v3/strategy/composite.py) L98~L105
  * [`strategy/exits/two_phase_atr.py`](file:///d:/repo/korea-market_v3/strategy/exits/two_phase_atr.py) L81~L89
* **코드 현상**:
  ```python
  ref_bar_data = {
      "open": bar.open,
      "high": bar.high,
      "low": bar.low,
      "close": bar.close,
      "dt": bar.dt,
  }
  self._pending_ref_bars[bar.symbol] = ref_bar_data
  ```
* **분석 및 엣지 케이스**:
  * 백테스트에서는 분봉이 이미 완성된 1분봉 데이터가 들어오므로, 09:02 분봉에서 진입할 때 `bar.close`는 09:02:59의 최종 마감 종가입니다.
  * 반면 실전 틱 단타 환경에서는 09:02:15 시점에 돌파가 발생하여 진입할 때의 `bar.close`는 09:02:15 시점의 현재 체결 틱 가격입니다.
* **설계자의 본래 의도**:
  * 백테스트 환경에서 Bar 0의 미래 참조(Lookahead)를 막기 위해, Phase 1 동안에는 -2.0% 비상손절만 쓰고, 다음 분봉(09:03)이 시작될 때 마감된 Bar 0의 중심선 `(open + close) / 2`로 승격시키는 정교한 수명주기 설계입니다.
* **개선 권고안**:
  * 실전 환경 구현 시, 09:02:00~09:02:59 분봉이 마감되는 09:03:00 정각에 확정된 Bar 0의 최종 종가로 `ref_bar["close"]`를 1회 갱신해 주는 '봉 마감 동기화 이벤트(Bar Completed Hook)'를 연결하면 백테스트와 실전 간의 괴리가 0.0%로 수렴합니다.

---

### 📌 [Audit-03] 단일 스레드 분봉 스트림의 심볼 정렬 순서 편향 (Symbol Order Bias)
* **관련 코드**:
  * [`engine/datafeed/historical.py`](file:///d:/repo/korea-market_v3/engine/datafeed/historical.py) L77 (`combined.sort_values(by=["dt", "symbol"], inplace=True)`)
  * [`strategy/composite.py`](file:///d:/repo/korea-market_v3/strategy/composite.py) L77~L78 (`if self._daily_trade_count >= self.max_daily_positions: return`)
* **코드 현상**:
  * 동일한 분봉(예: 09:02:00)에 2개 이상의 종목(예: `000660 SK하이닉스`와 `005930 삼성전자`)이 동시에 돌파 조건을 만족한 경우:
  * 데이터피드가 `["dt", "symbol"]` 순으로 정렬되어 있으므로, 심볼 코드 번호가 빠른 종목(`000660`)이 루프에서 먼저 평가되어 일일 거래 한도(1종목)를 선점합니다.
* **설계자의 본래 의도**:
  * 백테스트의 결정론적 재현성(Deterministic Reproducibility)을 보장하기 위해 고정된 정렬 기준을 둔 것입니다.
* **실전 상황 및 주의점**:
  * 실전에서는 밀리초 단위로 먼저 돌파 호가를 체결시킨 종목이 먼저 들어옵니다.
  * **[개선 권고안]**: 백테스트 시 동일 분봉 내 복수 후보가 출현할 경우, 단순 심볼 순이 아닌 당일 누적 거래대금(`cum_turnover`)이 더 큰 최우선 주도주를 선점하도록 스캐너 후보군 우선순위 큐(Priority Queue)를 도입하는 방안을 고려할 수 있습니다.

---

### 📌 [Audit-04] 캔들 몸통 비율 계산식의 분모 정규화 의도
* **관련 코드**:
  * [`strategy/scanners/top_turnover.py`](file:///d:/repo/korea-market_v3/strategy/scanners/top_turnover.py) L72
  ```python
  body_pct = (bar.close - bar.open) / day_open
  ```
* **일반 공식과의 차이**:
  * 통상적인 기술적 분석 지표에서는 캔들 몸통 비율을 해당 분봉의 시초가 `(bar.close - bar.open) / bar.open`으로 계산합니다.
  * 하지만 본 코드에서는 분모로 **당일 시초가(`day_open`)**를 채택했습니다.
* **설계자의 본래 의도**:
  * 갭상승 종목이나 고가권 종목에서 분봉 시초가가 높아졌을 때 발생하는 변동성 왜곡을 배제하고, "당일 시초가 대비 실질적으로 +1.5% 이상의 순수 매수 대금이 실린 장대 양봉인가"를 일관된 잣대로 필터링하기 위한 **의도적인 정규화 설계**입니다.
  * **[주의]**: 후임 개발자나 AI가 이를 "일반적인 캔들 계산 공식과 다르다"고 판단하여 `bar.open`으로 임의 수정할 경우 주도주 필터링 성격이 크게 훼손되므로 원본 공식을 유지해야 합니다.

---

### 📌 [Audit-05] 장초반 09:00 틱 미체결 종목의 `_day_open_prices` 초기화
* **관련 코드**:
  * [`strategy/scanners/top_turnover.py`](file:///d:/repo/korea-market_v3/strategy/scanners/top_turnover.py) L56~L57
  * [`strategy/entries/breakout_cap.py`](file:///d:/repo/korea-market_v3/strategy/entries/breakout_cap.py) L58~L59
* **코드 현상**:
  ```python
  if bar.symbol not in self._day_open_prices:
      self._day_open_prices[bar.symbol] = bar.open
  ```
* **잠재적 엣지 케이스**:
  * 09:00:00 정각에 거래가 없다가 09:04:00에 첫 체결이 발생한 유동성 부족 종목의 경우, 09:04 분봉의 시가가 `day_open`으로 저장될 수 있습니다.
* **설계 의도 및 실전 가이드**:
  * 백테스트에서는 거래대금 상위(10억 이상) 종목만 다루므로 09:00 정각 체결이 확실하여 문제가 되지 않습니다.
  * 하지만 실전에서는 VI 발동(동시호가 2분 연장)으로 09:02에 시초가가 형성되는 경우가 빈번하므로, 실전 환경에서는 KIS API의 당일 공식 시초가(`stck_oprc`)를 직접 주입받는 안전 장치를 마련하는 것이 좋습니다.

---

### 📌 [Audit-06] 포지션 보유 중 미실현 출렁임(Intra-Trade MAE) 미반영
* **관련 코드**:
  * [`engine/metrics.py`](file:///d:/repo/korea-market_v3/engine/metrics.py) L70~L80 (`calculate_performance`)
* **코드 현상**:
  * 성과 분석 엔진은 청산이 완료된 개별 트레이드의 실현 순익(`trade.net_pnl`)의 누적 합산으로 지분 곡선(Equity Curve)을 그리고 MDD를 계산합니다.
* **분석**:
  * 포지션을 보유하고 있는 도중 분봉 저가가 -1.8%까지 급락했다가 반등하여 +3.0% 익절로 마감된 경우, 보유 중 겪었던 -1.8%의 심리적 손실(Intra-Trade Drawdown)은 전체 MDD 통계에 반영되지 않습니다.
* **설계 의도**:
  * 전형적인 퀀트 트레이딩의 "폐쇄 거래 기준 지분 곡선(Closed-Trade Equity Curve)" 표준 방식입니다.
* **가이드**:
  * 각 거래의 `TradeRecord`에 이미 `mae_pct`(최대 불리한 가격 변화) 필드가 마련되어 있으므로, 향후 분봉 단위 실시간 미실현 손익 지분곡선(Mark-to-Market Equity Curve) 분석기를 보강하면 투자 심리 관리 및 실전 운용 안정성을 더욱 극대화할 수 있습니다.

---

### 📌 [Audit-07] 대용량 파켓 스트리밍(`pd.concat`) 디스크 I/O 최적화 여지
* **관련 코드**:
  * [`engine/datafeed/historical.py`](file:///d:/repo/korea-market_v3/engine/datafeed/historical.py) L54~L78 (`_stream_day`)
* **코드 현상**:
  * 매 거래일마다 해당 일자 디렉토리 내 수백~수천 개의 파켓 파일을 `pd.read_parquet`로 개별 로드한 뒤 `pd.concat`으로 합칩니다.
* **설계 의도**:
  * 2개년 수백만 개 분봉 전체를 메모리에 한꺼번에 적재하면 8~16GB 이상의 RAM이 소모되므로, 하루 단위로 격리하여 메모리 누수를 원천 방지한 안정적인 설계입니다.
* **향후 최적화 권고**:
  * 500거래일 전수 백테스트를 수백 번 스윕할 때는 개별 파일 읽기보다 연도별 결합 파켓(`intraday_2024.parquet`) 파티셔닝 포맷을 활용하면 백테스트 속도를 5~10배 추가 가속할 수 있습니다.

