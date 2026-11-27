# Korea Market v4 — 통합 아키텍처 재설계 Task

## 1. 배경 및 목표
- **문제**: 기존 `v3`는 `TradingEngine`(백테스트)과 `RealtimeLeaderScanner` + `live_sync.py`(실전)가 이원화되어 구조적 괴리 및 멀티스레드 경쟁/실전 전용 버그가 반복 발생함.
- **목표**:
  1. 외부 API와 WebSocket 스트림을 **단 하나의 추상 인터페이스(`v4/interfaces.py`)**로 정의.
  2. **전략 및 매매 루프 코드는 한 벌(`v4/runner.py` + `v4/strategy/`)만 작성**하고, 실행 모드(`live` vs `backtest`)에 따라 어댑터(`v4/adapters/`)만 갈아끼우는 단일 이벤트 루프 구조로 단순화.
  3. 기준 전략은 `feat/regime-adaptive-strategy` (`fab15f1`)의 **`all_weather`** 를 참고하되, 기존 레거시 엔진(`historical.py`, `live_sync.py`, `realtime_scanner.py`)을 억지로 재활용하지 않고 `v4/` 패키지로 깔끔하게 신규 작성.

---

## 2. 현재 Worktree 환경 (세팅 완료)
- **경로**: `d:\repo\korea-market_v4`
- **브랜치**: `feat/unified-v4` (`feat/regime-adaptive-strategy` @ `fab15f1` 기준)
- **데이터**: `data/` → Windows Junction (`D:\repo\krx-market-data\data`)
- **환경설정**: `.env` (`korea-market_v3\.env` 복사 완료)
- **재사용 코어 유틸**: `core/auth.py`, `core/krx_ticks.py`, `core/krx_symbols.py`, `core/rate_limiter.py`, `core/dynamic_features.py`, `core/market_regime.py`, `core/telemetry.py`, `config/settings.py`

---

## 3. 실전(2026-11-26) & 데이터셋 실증에서 확인된 핵심 사실
1. **`get_volume_rank` 불필요**: 조건검색(`HHKST03900400`)만 사용. `seq="0"` 또는 `curr_bar_surge_ok=True`일 때만 `krx_cum_turnover` Fast-Pass 허용; `seq="1"`에서 `curr_bar_surge_ok is None`이면 반드시 `get_recent_minute_bars`로 동일 봉 급등+거래대금 확인.
2. **NXT vs KRX 데이터 분리 & 정준 `cum_turnover`**:
   - 파켓(`20261126/005930.parquet`, `006400.parquet`, `intraday_2026.parquet`) 및 `v3` `HistoricalDataFeed` 실측 결과, 통합 `cum_turnover`는 `08:00~08:50` 프리마켓 거래대금을 포함(`"UN"` `acml_tr_pbmn` 정합)하나, `day_open`/`krx_day_low`/`prior_krx_day_low`는 반드시 `>= market_open_time` 정규장 데이터만 사용(`005930` `08:00` 저가 `170,600` vs `09:00` 시가 `172,900` = `-1.33%` 허위 MAE 이탈 차단).
   - `TickEvent.day_low`는 `H0UNCNT0`에서 `raw_day_low` 폴백 금지(`H0STCNT0`만 허용), `get_intraday_snapshot`의 `day_open/low/high`는 `"J"` 또는 정규장 분봉만 사용.
3. **WS TR 분기, 멀티 프레임 파싱, 40종목 공유 한도 & 무틱 15:21 청산**:
   - **NXT 대상은 `H0UNCNT0`("UN")**, **비-NXT는 `H0STCNT0`("J")** 분기 구독 (`is_nxt=None` 시 마스터/피처로 사전 판별).
   - 40종목 한도는 두 TR 합산으로 관리하며 보유 포지션과 미체결 주문(`pending_orders`) 종목은 퇴출 금지.
   - `stream_events()`는 `market_close_time`(`15:30`)까지 `MinutePulseEvent`를 지속 발행해 틱이 없는 비-NXT 포지션도 `15:20~15:30` 동시호가에 즉시 청산.
4. **10:00 지연 개장일 & Bar 0 수명주기 정합**:
   - `MarketIndexRegime`의 `market_open_time`/`market_close_time` (`2026-01-02` `10:00~15:19`, `2026-11-19`/`2025-11-13` `10:00~15:30`)을 전파 (`v4` 기본 `shift_to_market_open=True`, `v3` 교차검증용 `False` 지원).
   - `IExitComponent`는 `phase == 1`에서 오직 `GRACE_EMERGENCY_SL`만 평가하며(`optimistic` 시 스킵), Bar 0 종료 직후 `ref_bar` 동기화 및 `phase = 2` 승격으로 Bar 1 중복 평가를 원천 차단.

---

## 4. 진행 로드맵
- [x] **Step 1. v4 아키텍처 명세 확정 & 모델/인터페이스(`v4/market_models.py`, `v4/order_models.py`, `v4/models.py`, `v4/interfaces.py`, `docs/07_V4_ARCHITECTURE_SPEC.md`) 작성** *(Approved)*
- [x] **Step 2. 단일 전략(`v4/strategy/`), 리스크 가드(`v4/risk.py`) 및 통합 실행 루프(`v4/runner.py`) 구현 + `audit/` 14대 계약 검증**
- [x] **Step 3. 백테스트 어댑터(`v4/adapters/backtest/`) 구현 & `v3-regime` 기준일 교차 검증**
- [x] **Step 4. 실전 KIS 어댑터(`v4/adapters/kis/`) 구현 & VTS/REAL API 연동 및 40종목 캡/CLI 배선 완료**
