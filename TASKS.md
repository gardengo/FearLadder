# TASKS.md

# NASDAQ Leverage Regime Monitor — Development Tasks

## 개발 규칙

모든 작업:

```text
Implement
→ Unit Test
→ Integration Test
→ Review
→ Commit
```

완료 조건을 충족하지 못한 task는 다음 단계로 넘어가지 않는다.

자동주문 코드는 어느 단계에서도 구현하지 않는다.

---

# Phase 0 — Project Bootstrap

## TASK-001 Repository Initialization

- Python project 생성
- src layout
- app
- scripts
- tests
- config
- data
- reports
- GitHub workflow 구조

완료 조건:

```text
pytest 실행 성공
```

---

## TASK-002 Configuration System

전략 설정과 연구 설정을 분리한다.

파일:

```text
config/indicators.yaml
config/strategy.yaml
config/alerts.yaml
config/data_sources.yaml
```

validation 구현.

---

# Phase 1 — SQLite Foundation

## TASK-010 SQLite Schema

테이블:

```text
market_observations
indicator_values
indicator_scores
market_states
target_allocations
regime_events
alert_events
pipeline_runs
strategy_versions
```

DB:

```text
data/regime_monitor.db
```

---

## TASK-011 Repository Interface

SQLite 구현체와 domain logic을 분리한다.

최소:

```text
MarketObservationRepository
IndicatorRepository
MarketStateRepository
EventRepository
StrategyRepository
```

---

## TASK-012 Persistence Tests

- insert
- update
- unique constraints
- duplicate prevention
- transaction rollback

테스트.

---

# Phase 2.5 — Data Source Validation

## TASK-027 FinanceDataReader Adapter

### 목표

미국 가격 데이터의 표준 수집 경로를 FinanceDataReader 기반으로 구현한다.

대상:

```text
QQQ
QLD
TQQQ
```

### 요구사항

- start/end 지원
- 일별 OHLCV 표준화
- source metadata 기록
- library version 기록
- retry/error handling

---

## TASK-028 ProShares Cross-Validation

### 목표

QLD/TQQQ 가격 시계열을 ProShares 공식 자료와 교차검증한다.

검증 항목:

- trading dates
- split events
- price continuity
- major price discrepancies

불일치 시 자동으로 수정하지 말고 REVIEW 상태로 기록한다.

---

# Phase 2 — Data Layer

## TASK-020 Data Models

구현:

```text
MarketObservation
IndicatorValue
MarketState
TargetAllocation
RegimeEvent
AlertEvent
PipelineRun
```

---

## TASK-021 Price Adapter

QQQ / QLD / TQQQ daily data를 표준화한다.

---

## TASK-022 CNN Fear & Greed Adapter

역사 데이터를 표준화한다.

---

## TASK-023 VIX Adapter

VIX daily data를 표준화한다.

---

## TASK-024 AAII Adapter

주간 sentiment를 표준화한다.

---

## TASK-025 Breadth Adapter

가능한 경우 breadth를 추가한다.

데이터 품질이 불충분하면 명시적으로 제외한다.

---

# Phase 3 — Indicator Engine

## TASK-030 RSI

```text
RSI(14)
RSI(30)
```

---

## TASK-031 Trend

```text
20DMA
50DMA
200DMA
50DMA/200DMA
200DMA slope
```

---

## TASK-032 Momentum

```text
1M
3M
6M
12M
```

---

## TASK-033 Drawdown

```text
current
30D
90D
52W
```

---

## TASK-034 VIX Features

```text
level
change
percentile
```

---

## TASK-035 Sentiment Features

CNN / AAII.

---

## TASK-036 Breadth Features

지원 가능한 breadth.

---

# Phase 4 — Score Engine

## TASK-040 Normalization Framework

0~100 normalization abstraction.

---

## TASK-041 Past-Only Normalization

rolling percentile / z-score 등이 미래를 사용하지 않는지 테스트.

---

## TASK-042 Composite Score

가중합 구현.

Score breakdown 제공.

---

# Phase 5 — Regime Engine

## TASK-050 Regime Classifier

3/5/7/9 stage configurable.

---

## TASK-051 Transition Logic

구현:

- confirmation
- hysteresis
- minimum duration

---

## TASK-052 Regime Event

이벤트 생성:

```text
previous_regime
new_regime
previous_score
new_score
reason_codes
event_date
```

---

# Phase 6 — Allocation Engine

## TASK-060 Allocation Model

QQQ / QLD / TQQQ / Cash.

---

## TASK-061 Target Leverage

```text
L = w_QQQ + 2*w_QLD + 3*w_TQQQ
```

---

## TASK-062 Regime Mapping

config 기반 allocation.

초기값은 placeholder.

---

## TASK-063 TQQQ Gate

Extreme Fear와 Bottom Candidate를 분리할 수 있는 규칙 인터페이스를 만든다.

---

# Phase 7 — Backtest Engine

## TASK-070 Simulator

daily NAV 계산.

---

## TASK-071 Execution Timing

당일 signal → 다음 거래일 실행.

---

## TASK-072 Transaction Cost

commission / spread / slippage abstraction.

---

## TASK-073 Rebalance

Regime Change 기반 rebalance.

---

## TASK-074 Benchmark

QQQ / QLD / QLD 70-30 benchmark.

---

# Phase 8 — Optimization

## TASK-080 Indicator Selection

후보 지표 조합 비교.

---

## TASK-081 Weight Search

가중치 후보 탐색.

OOS 금지.

---

## TASK-082 Regime Count

3/5/7/9 비교.

---

## TASK-083 Threshold Search

RSI/VIX/Fear/Drawdown threshold 탐색.

---

## TASK-084 Allocation Search

Regime별 목표 비중 탐색.

---

## TASK-085 Transition Search

confirmation / hysteresis / minimum duration 탐색.

---

# Phase 9 — Validation

## TASK-090 Dataset Split

Research / Validation / OOS.

---

## TASK-091 Walk-Forward

Train → Validate → Freeze → Test 반복.

---

## TASK-092 Leakage Test Suite

모든 future-data / availability / execution leakage 검사.

---

## TASK-093 Sensitivity Analysis

주변 parameter 영역 안정성 검사.

---

# Phase 10 — Strategy Freeze

## TASK-100 Final Parameter Selection

모든 최종 파라미터를 결정.

---

## TASK-101 Strategy Manifest

예:

```yaml
strategy_version: v1.0-frozen
data_version: ...
parameter_version: ...
frozen_at: ...
```

---

## TASK-102 Frozen Regression

동일 입력에서 동일 결과 보장.

---

# Phase 11 — Daily Worker

## TASK-110 Daily Runner

구현:

```text
collect
→ validate
→ indicators
→ score
→ regime
→ allocation
→ persistence
→ event detection
→ alert
```

---

## TASK-111 Idempotency

동일 날짜 재실행 안전성 확보.

---

## TASK-112 Data Freshness

stale data를 감지하고 정상 signal 생성을 막는다.

---

# Phase 12 — Telegram Alert

## TASK-120 Telegram Provider

환경 변수:

```text
TELEGRAM_BOT_TOKEN
TELEGRAM_CHAT_ID
```

---

## TASK-121 Alert Templates

최소:

```text
REGIME_CHANGED
EXTREME_FEAR
EXTREME_BUBBLE
TQQQ_CANDIDATE
DATA_FAILURE
```

---

## TASK-122 Alert Deduplication

동일 event 중복 발송 방지.

---

# Phase 13 — GitHub Actions

## TASK-130 Test Workflow

push / pull request 시:

```text
lint
test
```

---

## TASK-131 Daily Monitor Workflow

미국 시장 종료 후 적절한 시간대에 daily runner를 실행한다.

요구사항:

- schedule
- workflow_dispatch
- secrets
- logs
- failure visibility

---

## TASK-132 SQLite Persistence

daily workflow에서:

```text
checkout
→ update SQLite
→ test
→ commit changed DB
→ push
```

가 안정적으로 동작하도록 구현한다.

---

## TASK-133 Daily Job Failure Handling

실패 시:

- pipeline failure 기록
- 잘못된 state 저장 금지
- 필요하면 DATA_FAILURE alert

---

# Phase 14 — Streamlit

## TASK-140 Current Dashboard

- Regime
- Score
- Target Leverage
- Target Allocation
- Last Update

---

## TASK-141 Indicators

핵심 지표 panel.

---

## TASK-142 Historical Charts

- QQQ + Regime
- Score
- RSI
- VIX
- Drawdown
- Target Leverage

---

## TASK-143 Event History

Regime change / alert history.

---

## TASK-144 Backtest Report

benchmark 비교.

---

# Phase 15 — Deployment

## TASK-150 Dockerfile — 제외됨 (2026-09-12)

운영 경로(GitHub Actions + Streamlit Cloud + Telegram)에 컨테이너가 쓰이지
않아 운영자 결정으로 제거했다.

---

## TASK-151 Local Development

개발자가:

```text
python daily_runner.py
streamlit run app/streamlit_app.py
```

로 로컬 테스트할 수 있어야 한다.

---

## TASK-152 Streamlit Deployment

Streamlit Cloud 배포.

Streamlit은 DB에서 최신 상태를 조회한다.

---

# Phase 16 — End-to-End

## TASK-160 E2E Daily Pipeline

```text
Data
→ Indicator
→ Score
→ Regime
→ Allocation
→ SQLite
→ Event
→ Telegram
```

전체 흐름 테스트.

---

## TASK-161 Re-run Test

동일 daily pipeline을 두 번 실행해도 결과가 중복되지 않는지 검증.

---

## TASK-162 Data Failure E2E

필수 데이터 누락 시 잘못된 투자 신호와 알림이 생성되지 않는지 확인.

---

# Phase 17 — Documentation

## TASK-170 README

설치 / 실행 / 설정 / backtest / dashboard / GitHub Actions.

---

## TASK-171 Strategy Documentation

frozen strategy 명세.

---

## TASK-172 Operations Guide

다음 항목을 기록:

- GitHub Actions 확인
- Telegram token 설정
- workflow 수동 실행
- SQLite 문제 복구
- Streamlit 재배포
- data failure 대응

---

# Definition of Done

```text
[x] PRD 준수
[x] Architecture 준수
[x] Backtest Spec 준수
[x] 모든 unit test 통과
[x] integration test 통과
[x] leakage test 통과            BACKTEST_SPEC §24 의 7개 항목 전부
[ ] OOS 검증 완료                 ← 도구는 완성. 실행은 TASK-100 과 함께
[ ] strategy freeze 완료          ← 도구는 완성. 파라미터 결정이 선행되어야 함
[x] SQLite 정상 저장
[x] GitHub Actions daily 실행     ← 워크플로 완성. frozen 이후 자동 시작
[x] Telegram alert 정상
[x] Streamlit dashboard 정상
[-] Docker 실행 정상              ← 운영자 결정으로 범위에서 제외
[x] 자동주문 기능 없음
```

## 남은 항목에 대하여

**OOS 검증과 strategy freeze 는 코드가 아니라 판단이 필요한 항목이다.**

Phase 8~10 은 탐색·walk-forward·민감도 분석·freeze·회귀 검증 도구를 모두
제공하지만, 어떤 가중치·경계·배분을 고를지는 연구 결과를 보고 사람이 결정한다.
`CLAUDE_CODE_INITIAL_PROMPT.md` §7/§10/§15 가 개발자의 임의 확정을 금지한다.

절차는 `docs/strategy.md` §4 에 있다. 완료되면:

- `config/strategy.yaml` 이 `parameter_status: FROZEN` 이 되고
- `config/frozen/` 에 manifest 와 회귀 기준이 생기며
- daily-monitor 워크플로가 자동으로 실행을 시작한다

**Docker 는 범위에서 제외했다.** 운영 경로는 GitHub Actions + Streamlit Cloud +
Telegram 이고 그 어디에도 컨테이너가 쓰이지 않는다. 유지할 이유가 없어
2026-09-12 에 Dockerfile / docker-compose.yml / docker-entrypoint.sh /
.dockerignore 를 제거했다. 나중에 다른 호스트로 옮기게 되면 그때 다시 만든다.
