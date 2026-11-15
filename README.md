# 🚀 Korea Market v3: Modular Systematic Trading Platform

> **한국 주식시장 퀀트 트레이딩을 위한 고신뢰도 레고 블록 아키텍처**  
> 스캔(종목 발굴), 진입(신호 발생), 청산(손익절/리스크) 3대 모듈의 완전한 분리와 백테스트-실전 1:1 대사 보장.

---

## 🌟 핵심 특징 (v3 Architecture Highlights)

1. **3대 모듈 레고 블록 (Lego-Block Architecture)**:
   - `IScanner` (종목 발굴) + `IEntryRule` (진입 판단) + `IExitPolicy` (청산 관리) 분리.
   - 1,500줄짜리 거대 단일 전략을 폐기하고, 각 컴포넌트 150줄 이내의 클린 코드로 조립 (`CompositeStrategy`).
2. **반복 실수 방지 & 장애 회고록 (`docs/00_FAILURES_AND_LESSONS.md`)**:
   - 모호봉, 미래 참조(Lookahead), KRX 호가단위 반올림 등 과거에 겪었던 이슈를 전수 아카이빙.
   - AI 및 개발자가 코드 수정 전 필수 확인하는 5대 체크리스트 탑재.
3. **체결 모호성 듀얼 바운드 감사 (Dual-Bounds Ambiguity Audit)**:
   - 가짜 균등 틱($V/n$)으로 인한 과적합을 차단하고, 모호봉 발생 시 비관적(SL 우선, 하한)과 낙관적(TP 우선, 상한) 2가지 케이스의 스프레드를 함께 분석하여 봉 내부 노이즈 과적합 검증.
4. **의존성 주입(DIP) 단일 코드베이스**:
   - 전략 코드는 단 1줄도 바꾸지 않고 Broker와 Clock만 교체하여 백테스트와 실전을 넘나드는 아키텍처.
5. **범용 계약 기반 전략 무결성 감사 (Universal Strategy Audit)**:
   - 신규 전략 개발 시 0줄의 테스트 코드로 Lookahead 0%, KRX 7단계 법정 호가, 래칫 SL 단조성, 모호봉 비관적 방어 등 **14대 계약 불변량(Invariants)**을 자동 전수 검증 (`audit/`, [docs/06_UNIVERSAL_STRATEGY_AUDIT.md](docs/06_UNIVERSAL_STRATEGY_AUDIT.md)).

---

## 📁 프로젝트 구조

```
korea-market_v3/
├── audit/                   # 🛡️ 범용 전략 계약 검증 하네스 (14대 불변량 자동 감사)
│   ├── scanner_verifier.py  # Lookahead 0% 및 심볼 셔플 불변성
│   ├── entry_verifier.py    # Bar 0 고스트 변이 차단 및 KRX 7대 호가 규격
│   ├── exit_verifier.py     # 래칫 SL 단조성 및 모호봉 비관적 방어
│   └── universal_auditor.py # 조립체 수명주기 감사 & Rich 리포터
├── config/                  # 설정 관리 (Pydantic / .env)
├── core/                    # KRX 법정 호가단위, 공통 유틸, 로깅
├── docs/
│   ├── 00_FAILURES_AND_LESSONS.md  # ⚠️ 장애 회고 및 AI 필수 체크리스트
│   ├── 01_ARCHITECTURE.md          # 아키텍처 및 시퀀스 다이어그램
│   ├── 02_DECISION_LOG.md          # 아키텍처 의사결정 기록 (ADR)
│   └── 06_UNIVERSAL_STRATEGY_AUDIT.md # 🛡️ 범용 전략 계약 감사 하네스 명세서
├── engine/
│   ├── broker/              # BacktestBroker(비관적 체결) & KisLiveBroker
│   ├── datafeed/            # HistoricalDataFeed & KisRealtimeFeed
│   ├── risk/                # 일일 손실 한도 & 연속 손실 서킷브레이커
│   ├── clock.py             # 가상 시계(Simulated) & OS 시계(Realtime)
│   ├── context.py           # 전략 실행 컨텍스트 (DIP 브리지)
│   ├── engine.py            # 이벤트 오케스트레이터
│   ├── metrics.py           # 성과 분석 (승률, 손익비, MDD)
│   └── models.py            # typed & slotted 불변 데이터 모델
├── strategy/
│   ├── scanners/            # IScanner: top_turnover 등 종목 발굴기
│   ├── entries/             # IEntryRule: breakout_cap 등 진입기
│   ├── exits/               # IExitPolicy: two_phase_atr, fixed_risk_reward 등 청산기
│   ├── composite.py         # 3대 모듈 조립 오케스트레이터
│   └── registry.py          # 조립 팩토리 및 레지스트리
├── tests/                   # 유닛 테스트 모음
├── main.py                  # CLI 엔트리포인트
└── requirements.txt
```

---

## 🛠️ CLI 사용법

```bash
# 1. 등록된 전략 레고 블록 목록 확인
py -3.12 main.py strategies

# 2. 장애 회고 및 필수 체크리스트 조회
py -3.12 main.py review-failures

# 3. 범용 계약 기반 전략 무결성 감사 (14대 안전 불변량 자동 검증)
py -3.12 main.py audit

# 4. 특정 전략 레고 블록 조합 무결성 감사 (예: fixed_risk_reward 청산기)
py -3.12 main.py audit --scanner top_turnover --entry breakout_cap --exit fixed_risk_reward

# 5. 조립형 순수 분봉 듀얼 바운드 백테스트 실행 (기본값: top_turnover + breakout_cap + two_phase_atr)
py -3.12 main.py backtest --mode dual --start 20241021 --end 20241025

# 6. 청산 모듈만 고정 손익비(fixed_risk_reward)로 교체하여 백테스트
py -3.12 main.py backtest --exit fixed_risk_reward --mode dual --start 20241021 --end 20241025

# 7. 유닛 테스트 및 무결성 하네스 실행 (19개 테스트)
py -3.12 -m pytest
```

