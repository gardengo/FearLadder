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
data/fear_ladder.db
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
[x] OOS 검증 완료                 2026-09-13, 고정 이후 한 번. docs/strategy.md 2.8
[x] strategy freeze 완료          v1.0-frozen, 지문 b425a9b104b62daa
[x] SQLite 정상 저장
[x] GitHub Actions daily 실행     거래일 다음 날 06:00 UTC (2026-09-14 에 옮겼다 —
                                  22:30 UTC 는 QQQ 봉이 올라오기 전이었다)
[x] Telegram alert 정상
[x] Streamlit dashboard 정상
[-] Docker 실행 정상              ← 운영자 결정으로 범위에서 제외
[x] 자동주문 기능 없음
```

## 완료 (2026-09-13)

전략은 `v1.0-frozen` 으로 고정됐고 OOS 구간까지 소비했다. 순서를 지켰다 —
탐색(1999–2015) → 검증(2015–2021) → **고정** → 최종(2021–2026). 고정을 먼저
한 것이 중요하다. 결과를 보고 숫자를 고칠 수 있으면 그 구간은 시험이 아니다.

| 구간 | CAGR | MDD | Sharpe |
| --- | --- | --- | --- |
| 탐색 1999–2015 | +12.78% | 62.4% | 0.56 |
| 검증 2015–2021 | +28.29% | 27.9% | 1.01 |
| **최종 2021–2026** | **+19.21%** | **46.6%** | **0.74** |

증거는 `config/frozen/v1.0-frozen.{manifest,regression,indicators,oos}` 에 있다.
경위와 기각한 시도는 `docs/strategy.md` 2.5~2.8.

**남은 구간이 없다.** 다음에 파라미터를 바꾸려면 새 데이터가 쌓이기를 기다려야
한다. 지금 고치면 검증할 창이 없다.

**Docker 는 범위에서 제외했다.** 운영 경로는 GitHub Actions + Streamlit Cloud +
Telegram 이고 그 어디에도 컨테이너가 쓰이지 않는다. 유지할 이유가 없어
2026-09-12 에 Dockerfile / docker-compose.yml / docker-entrypoint.sh /
.dockerignore 를 제거했다. 나중에 다른 호스트로 옮기게 되면 그때 다시 만든다.

---

# Phase 18 — 사후 검증 (research backlog) — **전부 완료 (2026-09-18)**

전부 **사후 측정**이다. `config/` 는 v1.0-frozen 이고 아래 어떤 항목도 설정을
바꾸지 않는다. 바꿀 근거가 나오면 새 freeze 절차(TASK-100)를 밟는다.

상세한 배경·실행 방법·판단 기준·함정은 **`docs/research-backlog.md`** 에 있다.
새 세션은 그 문서부터 읽는다. 아래는 각 항목의 **완료 기록**이고, 결론의 근거는
`docs/strategy.md` §2.12–§2.18 에 있다.

---

## TASK-180 전체 이력 DB 재구축 (다른 모든 항목의 선행 조건) — 완료 (2026-09-18)

`data/fear_ladder.db` 는 5년치만 들어 있다. 30년 이력을 저장소 밖에 만든다.

```bash
python scripts/collect_full_history.py --out /path/outside/repo/full.db
```

완료 조건:

```text
QQQ/QLD/TQQQ 각 7700행 이상, 1996-01-02 부터
```

각 7755행, 1996-01-02 … 2026-09-17. 기준 수치 재현 확인:
MDD −62.38% / Sharpe 0.648 / 단계 변경 93회.

**`data/reference/` 를 먼저 채워라.** gitignore 대상이라 새 워크트리에는 비어
있고, 그대로 수집하면 AAII/CNN 이 5년치만 들어와 낙폭이 −71.5% 로 어긋난다.
`docs/research-backlog.md` §0.1 에 확인 방법이 있다.

---

## TASK-181 재구성 모델을 2008년 실물 QLD 로 검증 — 완료 (2026-09-18)

QLD 는 2006-06-21 상장이라 2008년 폭락(QQQ −53.6%, QLD −84.0%)을 실물로 겪었다.
재구성 모델을 같은 구간에 돌려 **폭락기에 한정한** 추적오차를 쟀다.

```bash
python scripts/reconstruction_accuracy.py --db /path/outside/repo/full.db
```

결과 (`docs/strategy.md` §2.12, `reports/reconstruction_accuracy.json`):

- 모델은 폭락기에 **낙관적이다** — 금융위기 R² 0.968(전체 수명 0.990),
  연율 표류 +6.21%(전체 수명 −0.08%). 그 창이 함의하는 연간 마찰은 6.68% 인데
  모델은 0.68% 만 물린다.
- **그래도 −62.4% 는 그 오차에 둔감하다** — 재구성 구간 전체에 위기 수준
  조달비용을 물려도 −62.6%. 닷컴 낙폭 구간의 레버리지 슬리브 보유일이 10.4%
  뿐이고 나머지는 현금과 실물 QQQ 이기 때문이다.
- §2.11 은 철회하지 않는다. 미검증인 것은 모델 가격이 아니라
  `max_leverage_below = 0.5` 라는 **선택 자체**다.

---

## TASK-182 S&P 500 교차시장 재현 — 완료 (2026-09-18)

```bash
python scripts/cross_market.py --db /path/outside/repo/full.db
```

`docs/strategy.md` §2.13, `reports/cross_market.json`.

**`max_leverage_below` 는 재현되고 `d`·`h` 는 재현되지 않는다.** cap 은 두 시장
네 창 모두 단조이고 8개 조합 중 7개가 순위상관 +1.00 이며, 잔잔한 구간에서
부호가 뒤집히는 함정까지 같은 모양으로 나타난다. `d`·`h` 는 양쪽 다 톱니이고
시장 간 순위 상관이 −0.60…+0.70 을 떠돈다.

---

## TASK-183 `score.weights` 지표 제거 실험 — 완료 (2026-09-18)

```bash
python scripts/indicator_ablation.py --db /path/outside/repo/full.db
```

`docs/strategy.md` §2.14, `reports/indicator_ablation.json`.

**집합은 줄지 않는다.** 하나씩 빼면 아홉 개가 빼는 쪽이 나아 보이지만 아홉 개
전부 창을 바꾸면 부호가 뒤집히고 전부 검증창에서 음수다. 계열째 빼면 여섯 계열
전부 손해 — 상관된 지표에서 하나씩 빼기는 중요도를 체계적으로 과소평가한다.
덤: 평균 0.45점의 점수 이동이 68% 의 날에 단계를 바꾼다.

---

## TASK-184 이벤트 단위 분석 — 완료 (2026-09-18)

```bash
python scripts/event_analysis.py --db /path/outside/repo/full.db
```

`docs/strategy.md` §2.15, `reports/event_analysis.json`.

**사다리의 edge 는 공포 쪽에 있다** — 93개 이벤트 평균 +2.33%, 공포로 내려가는
44번은 +3.52%(68% 양수), 탐욕 쪽 49번은 +1.26%(55%). 짝지은 비교 16개 중
|t|>1.5 가 없고 `d`=90·120 은 75 와 구별되지 않는다. `cap` 만 총합이 단조인데
그 효과는 평균이 아니라 한 구간에 몰려 있다.

---

## TASK-185 비용 모델 측정 — 완료 (2026-09-18)

```bash
python scripts/cost_model.py --db /path/outside/repo/full.db
```

`docs/strategy.md` §2.16, `reports/cost_model.json`.

**마찰 = 연간회전율 × bps × (1 + CAGR)** — 25칸 전부 0.01%p 안쪽으로 맞으므로
새 비용 가정에 표를 다시 만들 필요가 없다. 회전율 3.26회/년이라 현실적인
+10bps 가 연 0.39%p 다. 무시할 값은 아니지만 어떤 결론도 뒤집지 않는다
(낙폭은 +50bps 에서도 −62.4% → −63.8%).

## TASK-186 0.0x 와 0.5x 의 실제 차이 — 완료 (2026-09-18)

```bash
python scripts/cash_exposure.py --db /path/outside/repo/full.db
```

`docs/strategy.md` §2.17, `reports/cash_exposure.json`.

§2.5 가 `max_leverage_below = 0.5` 를 원칙으로 정하며 남긴 근거는 research
구간의 "현금 100% 인 날 29.9%" 하나였다. 전 구간에서, 그리고 **사람이 겪는
형태**(연속 이탈 구간 길이)로 다시 쟀다.

추세 필터가 무는 날은 7414일 중 1301일(17.5%)이고 나머지는 두 설정이 같은
포트폴리오다. cap=0.0 이면 그 1301일이 **42개 구간**으로 뭉치는데 6개월 이상이
넷, **최장 310일(약 15개월, 2000-09 → 2001-12)**, 그다음이 249일과
**224일(2022-03 → 2023-01)**이다. cap=0.5 에서는 현금 100% 인 날이 **0일**이고
필터가 물 때 주식 비중이 정확히 50%다.

총합은 0.0 이 앞서지만(+0.133 로그, 연 +0.44%p) **그 우위가 다섯 해에서
나온다** — 위기 5년 +0.660, 나머지 26년 −0.527, 앞선 해는 31년 중 15년.
§2.15 의 구간 단위 비교에서도 t +0.31 / p 0.320 으로 **구별되지 않고**,
2010년 이후로는 0.5 가 연 1.36%p 앞선다. 유지 근거가 전 구간에서 재현됐다.

**설계 중 발견:** `era()` 가 일간 차분을 먼저 하고 경계로 자르면 경계일의
차분(경계 *이전*에 번 것)이 구간에 들어간다. 자른 뒤 차분하도록 고쳤다 —
`(start, end]` 규칙을 쓰는 §2.15 이벤트 원장과 같은 이유다. 이번 경계에서는
하루치라 숫자가 바뀌지 않았지만 2008년 경계였다면 폭락 대부분이 들어갔다.
