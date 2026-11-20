# Korea Market v3: Research & Strategy Discovery Catalog

이 문서는 Korea Market v3 (`feat/anchor-candle-strategy`)의 앵커 캔들 전략 연구(`research/`), 7개년(2020~2026) 전수 백테스트 데이터셋, 그리고 최종 채택된 **챔피언 B (Champion B: Body5.5-Micro)** 전략의 발견 및 검증 모듈을 체계적으로 정리한 마스터 인덱스 및 카탈로그입니다.

---

## 🧭 연구 개요 및 디렉터리 구조

`research/` 디렉터리는 KOSPI/KOSDAQ 시장의 일봉 350만 개(2020~2026) 및 2개년 분봉(1-minute bars) 데이터를 기반으로 인과율(Causal Invariance)과 무참조 원칙(Zero Lookahead)을 엄격히 준수하며 알파를 발굴한 정량 연구의 집약체입니다.

```
research/
├── README.md                                 # [현재 파일] 연구 카탈로그 및 인덱스
│
├── 🏆 [Champion B & Golden Decoupled Suite]  # 챔피언 B + 위기반등(-2.5% 분리형) 실증 핵심 모듈
│   ├── round2_candidate_pool.parquet         # 1,725 거래일 2차 정밀 후보 풀 (핵심 아티팩트)
│   ├── golden_25_decoupled_trades_2020_2026.csv # 골든 2.5% 분리형 1,261건 실증 거래 내역
│   ├── generate_final_backtest_report_data.py # 2020~2026년 7개년 골든 백테스트 공식 실행기
│   ├── study_oversold_gap_rebound.py         # 패턴 A/C 낙폭과대 갭하락 반등 수급 실증
│   ├── study_pattern_a_c_frequency.py        # 패턴 A/C 출현 빈도 및 결합 시너지 분석
│   ├── analyze_cash_defense_days.py          # 722일 관망일 특성 및 진입 가설 전수 검증
│   ├── calibrate_target_candidates.py        # 챔피언 B 타겟 가중치/스코어링/슬롯 캘리브레이션
│   ├── part1_prepare_and_engine.py           # DuckDB 기반 고속 벡터화 백테스트 엔진
│   ├── part2_extract_round2_data.py          # 15:20 실시간 피처 기반 라운드 2 데이터 추출
│   ├── part2_grid_search_round2.py           # 몸통/윗꼬리/순위 다차원 그리드 탐색
│   ├── part2_factor_empirical_tests.py       # 거래대금 순위/윗꼬리 팩터 실증 분석
│   ├── part3_experiment2_high60_filter.py    # 60일 신고가 및 모멘텀 필터 실험
│   ├── part4_experiment3_rebound_rescue.py   # 반등 구출 및 분봉 청산 실험
│   ├── audit_quant_integrity.py              # 퀀트 무결성 및 인과성 감사
│   ├── audit_and_optimize_quant_risk.py      # 레짐별 사이징 및 계좌 MDD 방어 최적화
│   ├── rigorous_alpha_audit.py               # 14개 조항 유니버설 전략 감사 규약 검증
│   ├── run_2020_2026_exhaustive_backtest.py   # 7개년 1,725일 전수 단리 백테스트 실행기
│   ├── run_all_years_backtest.py             # 연도별(2020~2026) 무패 실적 감사
│   ├── run_final_champion_benchmark.py       # 최종 챔피언 수수료(0.28%) 반영 벤치마크
│   ├── generate_final_champion_matrix.py     # 챔피언 후보군 종합 성과 매트릭스 산출
│   └── synthesize_super_champion_b_c.py      # B형(정밀 760형) + C형(NXT 조기청산) 포트폴리오 합성
│
└── 📜 [Tracked Legacy Research Suite]        # 초기 가설 수립 및 Q1~Q4 검증 모듈
    ├── analyze_q1_anchor_definition.py       # Q1: 앵커봉 정의 및 급등 임계치 분석
    ├── analyze_q2_position_and_low_break.py  # Q2: 앵커봉 저가 이탈 및 눌림 반등 분석
    ├── analyze_q3_q4_intraday_and_exits.py   # Q3~Q4: 분봉 체결 및 익일 청산 프로토콜
    ├── analyze_turnover_power.py             # 거래대금 순위별 후행 표류(Drift) 효과
    ├── analyze_user_7pct_d20_proposal.py     # 7% D-20 돌파 가설 실증 검토
    ├── compare_state_a_vs_b.py               # 상태 A vs 상태 B 가속 모델 비교
    ├── discover_profitable_anchor_strategy.py# 초기 수익성 앵커 발굴 스크립트
    ├── find_optimal_anchor_exit.py           # 09:05/09:10 청산 타이밍 최적화
    ├── grid_search_all_anchor_archetypes.py  # 4대 앵커 아키타입 전수 그리드 탐색
    ├── optimize_model2_close_betting.py      # 모델 2 종가배팅 파라미터 최적화
    ├── run_full_2year_refined_backtest.py    # 2개년 정제 백테스트
    ├── simulate_7pct_d20_intraday.py         # 7% D-20 분봉 시뮬레이션
    ├── simulate_anchor_daytrade_improvements.py # 데이트레이딩 개선안 시뮬레이션
    ├── simulate_true_stepup_2y.py            # 스마트 스텝업 트레일링 래칫 시뮬레이션
    ├── test_the_real_anchor_alphas.py        # 실전 앵커 알파 검증
    ├── q1_anchor_grid.csv                    # Q1 앵커 그리드 베이스라인 데이터
    ├── q2_position_low_break.csv             # Q2 저가 이탈 분석 베이스라인 데이터
    └── q3_q4_trades.csv                      # Q3~Q4 체결 내역 베이스라인 데이터
```

---

## 🏆 1. 챔피언 B (Champion B: Body5.5-Micro) 연구 코어

### 전략 핵심 명세
- **진입**: 15:20 종가 동시호가 시장성 지정가 (+0.5% 칼라 캡)
- **청산**: 익일 08:50 시초가 동시호가 매도 접수 $\rightarrow$ 09:00 시초가 전량 청산
- **필터 (Body5.5-Micro)**:
  - 당일 양봉 몸통(`body_ret`): $\ge 5.5\%$
  - 거래대금 1~10위: 윗꼬리 $\le 10\%$, 당일 등락률 $8.0\sim 22.0\%$
  - 거래대금 11~20위: 윗꼬리 $\le 8\%$, 당일 등락률 $10.0\sim 22.0\%$
  - 밑꼬리: $\le 20\%$ (`lower_shadow <= 0.20`)
  - 거래대금 20위 밖(Rank 21~30): 음의 알파 구간으로 **완전 배제**
- **슬롯 및 자금 배분**: 단리 1,000만 원 고정, 2슬롯 (55% Cap 또는 레짐 연동 70%/40%)

### 핵심 연구 아티팩트 및 스크립트 색인

| 파일명 | 역할 및 내용 | 의존 관계 / 산출물 |
| :--- | :--- | :--- |
| `round2_candidate_pool.parquet` | 2020~2026년 1,725 거래일의 15:20 시점 앵커 후보군 및 팩터 피처 테이블 | 프로덕션 백테스트 및 스캐너 캐시로 직접 참조 |
| `calibrate_target_candidates.py` | 후보군 스코어링 공식(`score_user`, `score_shadow40` 등)과 슬롯별 PnL/MDD를 정밀 캘리브레이션 | `round2_candidate_pool.parquet` 로드 $\rightarrow$ 성과 검증 |
| `part1_prepare_and_engine.py` | 7개년 일봉 데이터를 고속 로드하고 시뮬레이션하는 DuckDB 기반 공통 백테스트 코어 | `part2`, `part3`, `part4`가 공통 엔진으로 import |
| `part2_extract_round2_data.py` | 2차 그리드 서치를 위한 후보군 데이터셋 추출 및 정제 | `round2_candidate_pool.parquet` 생성 |
| `part2_grid_search_round2.py` | 몸통 4.5~6.5%, 윗꼬리 8~12%, 순위 10~25위 다차원 파라미터 그리드 전수 탐색 | `round2_grid_results.csv` 산출 |
| `part2_factor_empirical_tests.py` | 거래대금 순위별 유동성 절벽(Rank 21 이후 알파 급락) 및 윗꼬리 페널티 정량 검증 | 유동성 절벽(Liquidity Cliff) 실증 보고서 데이터 |
| `part3_experiment2_high60_filter.py` | 60일 신고가 비율(`ratio_60d >= 0.95`) 필터의 승률 및 손익비 개선 효과 측정 | 신고가 모멘텀 팩터 채택 근거 |
| `part4_experiment3_rebound_rescue.py` | 당일 장중 눌림 반등 및 익일 시초가 방어력 측정 | 손절 완화 및 구출 알고리즘 검증 |
| `audit_and_optimize_quant_risk.py` | KOSPI 이동평균 레짐(BullStrong vs Bear)에 따른 70%/40% 동적 사이징 및 MDD 방어 최적화 | 계좌 MDD -8.79% 달성 핵심 모듈 |
| `rigorous_alpha_audit.py` | 14개 유니버설 전략 감사 규약(무참조, KRX 호가단위, 칼라캡 등) 전수 자동 감사 | 14/14 조항 전원 PASS 검증 |
| `run_2020_2026_exhaustive_backtest.py` | 7개년 1,725일 전체를 단리 1,000만 원 고정으로 구동하여 연도별 손익 및 전체 지표 산출 | 816건, +170.5%, MDD -8.79%, 샤프 1.25 검증 |
| `run_all_years_backtest.py` | 2020, 2021, 2022(하락장), 2023, 2024, 2025, 2026 전년도 흑자(7전 7승) 검증 | 7개년 연도별 손익 감사 |
| `run_final_champion_benchmark.py` | 왕복 수수료/세금 0.28% 전액 차감 상태의 최종 공인 벤치마크 실행 | 공식 성과 지표 산출 |
| `generate_final_champion_matrix.py` | 베이스라인 vs 챔피언 A vs 챔피언 B vs 챔피언 C 종합 성과 비교표 자동 생성 | `master_champion_matrix.csv` 산출 |
| `synthesize_super_champion_b_c.py` | 챔피언 B(시초가 청산)와 챔피언 C(NXT +2.5% 조기 청산)의 하이브리드 포트폴리오 합성 | 승률 48.6%, MDD -9.04% 방어 검증 |

---

## 📜 2. 레거시 및 기초 가설 연구 모듈 (Legacy Baseline Suite)

초기 질문 1~4번(Q1~Q4)에 대한 실증 분석 및 2개년 기초 앵커 전략 연구 모듈입니다.

- **`analyze_q1_anchor_definition.py` & `q1_anchor_grid.csv`**:
  - 앵커봉(기준봉)의 정의(당일 등락률, 거래대금, 몸통 크기)에 따른 후속 변동성 측정.
- **`analyze_q2_position_and_low_break.py` & `q2_position_low_break.csv`**:
  - 앵커봉 저가 이탈 시의 회복 확률 및 눌림목 지지선 거동 정량 분석.
- **`analyze_q3_q4_intraday_and_exits.py` & `q3_q4_trades.csv`**:
  - 장중 1분봉 진입 vs 종가 진입, 익일 09:00 시초가 vs 09:05 분할 청산의 기대값 비교.
- **`analyze_turnover_power.py`**:
  - 거래대금 규모(1,000억 이상 vs 300억 이상)에 따른 슬리피지 및 시초가 갭상승 파워 분석.
- **`analyze_user_7pct_d20_proposal.py`**:
  - 사용자가 제시한 7% D-20 앵커 돌파 조건의 2개년 실증 백테스트.
- **`simulate_true_stepup_2y.py`**:
  - 장중 스마트 스텝업 트레일링 래칫(+2.5% $\rightarrow$ +5.0% $\rightarrow$ +7.5%)의 조기 익절 효과 측정.
- **`test_the_real_anchor_alphas.py`**:
  - 가짜 돌파(Fake Breakout)를 필터링하기 위한 체결강도 및 호가 잔량 비율 알파 검증.

---

## 🔬 3. 탐색형 및 진단형 스크립트 군 (Exploratory Catalog)

이 스크립트들은 파라미터 스윕 및 진단 과정에서 사용된 일회성 탐색 모듈로, 디렉터리 내에 안전하게 보존되며 `.gitignore` 규칙을 통해 Git 저장소 형상을 깨끗하게 유지합니다.

| 카테고리 접두어 | 수량 | 주요 목적 및 설명 |
| :--- | :---: | :--- |
| `check_*.py` | 32개 | 날짜 범위, 거래대금 계산식, 특정 종목(한화오션, SK하이닉스 등) 체결 데이터 정합성 검증 |
| `inspect_*.py` | 24개 | 15:15~15:30 호가/분봉 데이터 심층 분석, MDD 발생 구간 거래 내역 세부 감사 |
| `test_*.py` | 56개 | 개별 아이디어(약세장 인버스 헤지, 피보나치 되돌림, NXT 스톱로스 등) 단위 시뮬레이션 |
| `diagnose_*.py` | 4개 | 유휴 현금(Idle Cash) 비효율, 연도별 편차, 760건 체결수 유지 조건 진단 |
| `compare_*.py` | 7개 | 15:19 매수 vs 15:30 매수, Top 1 vs Top 2 슬롯 배분, 고정 금액 vs 슬롯 비율 비교 |
| `find_*.py` | 13개 | 최적 MDD 조합 탐색, 고빈도 알파 스팟 탐색, 760+ 챔피언 파라미터 서치 |
| `grid_search_*.py` / `fine_grid_*.py` | 다수 | 다차원 파라미터 그리드 서치 및 미세 그리드 튜닝 |

---

## 🛡️ 4. 데이터 보존 및 재현성 가이드 (Reproducibility)

### 1) 챔피언 B 백테스트 단독 재현
```bash
# 1. 챔피언 B 타겟 캘리브레이션 및 스코어링 평가
python research/calibrate_target_candidates.py

# 2. 7개년(2020~2026) 전수 백테스트 실행
python research/run_2020_2026_exhaustive_backtest.py

# 3. 메인 CLI를 통한 프로덕션 백테스트 실행
python main.py backtest --strategy champion_b --start-date 20200101 --end-date 20261118
```

### 2) 데이터 보존 원칙
- **영구 보존 대상**:
  - `data/` 디렉터리 (일봉 및 2개년 분봉 원천 데이터)
  - `research/round2_candidate_pool.parquet` (챔피언 B 전용 핵심 피처 데이터)
  - `research/q1_anchor_grid.csv`, `q2_position_low_break.csv`, `q3_q4_trades.csv` (기초 데이터)
  - `research/*.py` (모든 연구 및 탐색 스크립트 전수 보존)
- **Git 형상 관리**:
  - 대용량 임시 파켓/CSV 및 일회성 검증 스크립트는 `.gitignore`로 제외하여 `git status` 청결도를 유지합니다.
  - 프로덕션 코드(`strategy/scanners/anchor_body_micro_scanner.py`, `engine/datafeed/historical.py`, `main.py`)는 `research/round2_candidate_pool.parquet`를 기본 캐시로 안전하게 참조합니다.
