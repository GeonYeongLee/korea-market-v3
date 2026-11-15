# 🤖 AGENTS.md — Korea Market v3

> **AI Agent Operational Router**: High signal, zero clutter. Read referenced docs on demand.

---

### 🧭 North Star Mission (최상위 사명)
> **"이 시스템의 존재 이유는 사용자가 꾸준히 실질적인 돈을 벌어 금전적 여유를 찾도록 만드는 것이다.**  
> **백테스트와 실전 매매 사이의 괴리는 0%여야 하며, 화면상의 가짜 수익률에 결코 타협하지 않는다. 모든 코드는 실전 계좌에서 1원의 누수도 없이 완벽히 재현되어야 한다."**

---

## 1. Absolute Invariants (절대 준수 원칙)
- **Zero-Gap Realized Profit (실전 수익 무결성)**: 모든 연구와 개발의 종착점은 실전 계좌의 지속 가능한 순익이다. 백테스트와 실전 매매 간의 괴리를 0%로 수렴시키며, 화면 속 뻥튀기 수익률(과적합, 미래 참조, 모호봉 왜곡)을 원천 차단한다.
- **AI Supervisor & Subagent Delegation**: 메인 어시스턴트는 항상 시스템 총괄 **감독자(Supervisor)** 역할을 수행하며, 심층 분석, 코드베이스 감사, 리팩토링, 구현 등 모든 실무 작업은 전문 서브에이전트(`invoke_subagent`)를 띄워 위임하고 결과를 취합·통제한다.
- **Lego-Block Modularity**: 모든 전략은 `CompositeStrategy(scanner=..., entry=..., exit=...)` 조립형으로만 구현. 단일 모놀리스 전략 클래스 생성 절대 금지, 개별 모듈 300줄 이내(SRP) 강제.
- **Bar 0 Causal Integrity (Lookahead 0%)**: [docs/00_FAILURES_AND_LESSONS.md](docs/00_FAILURES_AND_LESSONS.md) Rule 0 준수. 어떤 진입/청산 모듈이든 진입 분봉(Bar 0) 미완성 상태에서 미래의 High/Close를 당겨와 목표가를 계산하는 행위 원천 차단.
- **Dual-Bounds Ambiguity Audit**: 모호봉(동일 분봉 내 TP/SL 동시 도달) 발생 시 전략 평가는 반드시 **비관적(SL 우선, 하한)**과 **낙관적(TP 우선, 상한)** 2가지 케이스의 스프레드를 함께 분석하여 봉 내부 노이즈 과적합 검증.
- **KRX Tick Conformance**: 모든 주문/청산 가격은 반드시 `core.krx_ticks.round_to_krx_tick()`을 거쳐 법정 호가로 반올림 (매수: Floor, 매도: Ceil).
- **DIP (Dependency Inversion Principle)**: 전략 코드는 백테스트/실전 100% 동일 유지. 오직 `IBroker`, `IClock`, `IDataFeed` 주입 부품만 교체.
- **Single CLI & Clean Workspace**: 모든 실행은 `python main.py <subcommand>`로만 수행. 루트 디렉토리에 임시 스크립트(`test_*.py`, `audit_*.py`, `scratch_*.py`) 생성 절대 금지.
- **Fixed Notional & Risk Guard**: 단리 1,000만 원 고정 운용 (복리 베팅 절대 금지, MDD < 12.0% 사수).
- **Deterministic Reproducibility**: 동일 데이터셋 및 설정에 대해 항상 100% 일치하는 백테스트 결과 재현.

## 2. Topic References (상세 내용 필요 시 상대 경로 파일 참조)
- **장애 회고 및 필수 체크리스트**: [docs/00_FAILURES_AND_LESSONS.md](docs/00_FAILURES_AND_LESSONS.md) — 과거 장애 아카이브(Issue 1~5), AI 5대 체크리스트
- **시스템 아키텍처 및 DIP 구조**: [docs/01_ARCHITECTURE.md](docs/01_ARCHITECTURE.md) — 3대 모듈 책임 및 브로커/피드 주입 구조
- **아키텍처 의사결정 기록 (ADR)**: [docs/02_DECISION_LOG.md](docs/02_DECISION_LOG.md) — 모듈화 및 체결 모호성 정책
- **전략 조립 및 컴포넌트 레지스트리**: [strategy/registry.py](strategy/registry.py) & [strategy/composite.py](strategy/composite.py)
- **범용 전략 계약 감사 하네스**: [docs/06_UNIVERSAL_STRATEGY_AUDIT.md](docs/06_UNIVERSAL_STRATEGY_AUDIT.md) — 14대 불변량 자동 검증 (0줄 테스트 체계)
