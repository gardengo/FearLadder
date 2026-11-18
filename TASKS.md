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

---

## TASK-187 모의 성과 기록과 대시보드의 갱신/고정 구분 — 완료 (2026-11-11)

`ARCHITECTURE.md` §4.1.3 · §17, `src/fear_ladder/pipeline/track_record.py`,
`tests/integration/test_track_record.py`.

대시보드를 쓰다 나온 세 가지 불편에서 출발했다.

**1. 200일선과의 거리를 볼 수 없었다.** 레버리지를 실제로 빼앗는 규칙은 추세
필터인데, 화면에 남는 흔적은 판단 근거의 한 문장뿐이었다. '오늘' 탭 머리 숫자에
200일선 이격을 넣고, 난간 패널을 새로 만들었다 — 작동선(−2%)·해제선(+2%)까지의
거리를 %p 와 달러로, 52주 낙폭 조건(20%)의 진행도, 두 조건이 함께 맞는 종가,
그리고 지난 1년의 이격 추이. 2026-11-05 처럼 **선 아래(−4.9%)인데 낙폭이 얕아
(14.8%) 난간이 작동하지 않는 상태**에 이름을 붙여 따로 보여준다. 작동 여부는
워커가 남긴 `reason_codes` 에서 읽고 대시보드는 판정하지 않는다 (필터에 기억이
있어 오늘 값만으로는 알 수 없다). 페이지의 조건 산술이 `TrendFilter` 와 어긋나지
않는다는 것은 테스트가 붙잡는다.

**2. 전략을 따랐을 때의 성과가 어디에도 없었다.** '기록' 탭은 점수를 QQQ 와
비교했지만 그 점수가 만든 포트폴리오는 그리지 않았다. 목표 비중과 세 슬리브의
가격은 이미 매일 저장되고 있었으므로 빠진 것은 둘을 곱한 값 하나였다.
`track_record` 테이블을 추가하고 워커가 매일 한 줄 이어 붙인다.

- 백테스트와 **같은 시뮬레이터**를 지난다 (다음날 시가 체결, 드리프트, 비용).
  벤치마크는 같은 시뮬레이터의 QQQ 100%. 배당은 양쪽 모두 넣지 않는다
  (백테스트가 종가 기준이므로).
- **재생하지 않고 이어 붙인다** — §4.1.1 의 의도된 예외. 원시 가격이 5년 창으로
  잘려도 기록이 남아야 하고, 나중의 정정이 과거 성과를 고칠 수 있으면 기록이
  아니기 때문이다. 이를 위해 `PortfolioSimulator.run` 에 `initial_book` /
  `initial_target` 을 넣어 **중단 후 재개가 무중단 실행과 같은 NAV** 를 내게 했다.
- 이 단계의 실패는 그날의 신호를 막지 않는다. 다음 실행이 마지막 행부터 잇는다.
- 첫 배치에서 2024-01-02(저장된 첫 목표)부터 한 번에 채워진다. 고정일
  (2026-09-13) 이전은 고정된 규칙을 과거에 재생한 구간이고 이후가 실시간이다 —
  차트에 경계선을 긋고 실시간 구간 수익률을 따로 적는다.

**3. 매일 바뀌는 탭과 바뀌지 않는 탭이 구분되지 않았다.** 탭 순서를
오늘 · 기록 · 이벤트 · 운영 | 전략 설명 · 백테스트 로 바꾸고, 각 탭 첫 줄에
"매일 갱신 · 기준일 · 마지막 배치 시각(KST)" 또는 "고정 문서 · 고정일" 을 적는다.
'성과' 탭은 '백테스트' 로 이름을 바꿨다 — 실시간 성과가 '기록' 탭에 생겼으므로
'성과' 라는 이름은 고정된 30년 리포트를 살아 있는 숫자로 읽히게 했다.

**설계 중 발견:**

- 재개 지점을 그대로 넣으면 `NEXT_OPEN` 분기가 **첫날의 시가→종가 수익을 한 번
  더** 줬다. 무중단 실행에서는 첫날에 보유가 없어 드러나지 않던 경로다. 실제
  DB 로 "7일씩 끊어 실행 vs 한 번에 실행" 을 비교하다 최대 0.38 의 NAV 차이로
  잡혔고, 고친 뒤에는 717일 전부 비트 단위로 같다.
- 현금 금리는 가격보다 하루 이틀 늦게 온다. 백테스트처럼 날짜를 정확히 맞춰
  찾으면 이어 붙이는 기록에서는 **방금 적는 날의 현금이 늘 0% 를 번다.** 마지막
  으로 발표된 금리를 이월해 쓴다 (기간이 시작될 때 알 수 있는 값이라 누출이
  아니다).
- '기록' 차트의 단계 색이 **창 안에 있는 단계들 사이에서만** 펴져 있었다. 최근
  1년처럼 Neutral · Optimism · Greed 만 있는 창에서는 Neutral 이 Capitulation 의
  진한 빨강으로 칠해졌다. 사다리 전체 기준으로 고쳤다.

---

## TASK-188 레짐 판정이 창 시작일에 좌우되던 문제 — 완료 (2026-11-11)

`ARCHITECTURE.md` §4.1.1, `src/fear_ladder/regime/anchor.py`,
`src/fear_ladder/pipeline/daily.py`, `scripts/make_regime_anchor.py`,
`tests/integration/test_regime_memory.py`.

**증상.** '오늘' 탭에서 Regime 은 "54일째" 인데 잠금 막대는 "60 / 75" 였다. 표시
문제가 아니었다. 저장된 잠금 카운터를 따라가 보니 9/25 에 `58/75` 였던 것이 10/2 에
`33/75` 로 뛰어 있었다.

**원인.** 워커는 매 실행마다 `오늘 − 2000일`(정리 때문에 사실상 5년 전)부터 전이
규칙을 처음부터 재생했다. 창의 시작은 매일 하루씩 밀린다. 75일 유지 규칙은 유지
기간이 끝나는 날 단계를 바꾸고 다음 유지 기간을 그날부터 세므로, **시작한 날이
만든 주기가 영영 남는다.** 같은 점수열에서 재생 시작일만 1~77일 옮겼더니
2026-11-10 의 답이 Neutral · Optimism · Fear 로 갈렸고(서로 다른 결과 13가지),
저장된 715일 가운데 1996년부터 재생한 백테스트와 레짐이 같은 날은 237일이었다.
`backtest_runner.py` 의 주석은 "같은 날짜는 어느 창에서든 같은 레짐이어야 한다" 고
적고 있었지만 워커는 그 조건 밖에 있었다.

점수는 무죄였다. 최근 날짜의 종합점수는 5년 창과 30년 창에서 소수점 아래까지 같다.

**고친 것.**

- 전이 엔진이 상태를 받아 이어서 돌 수 있다 (`TransitionEngine.run(state=...)`).
  어디서 끊어도 이어 붙인 결과가 무중단 재생과 같다는 것을 테스트가 강제한다.
- 워커는 날마다 엔진이 들고 나온 상태를 `regime_memory` 에 저장하고, 다음 날은
  거기서 한 걸음만 나아간다. 창이 어디서 시작하는지는 더 이상 입력이 아니다.
- 저장된 것이 없는 첫 실행은 **고정 백테스트가 끝난 자리**에서 시작한다 —
  `config/frozen/v1.0-frozen.regime_anchor.json` (2026-09-11: Neutral, 2026-06-11
  진입, 65일째). 전체 이력을 다시 받아 만들었고, 그 이력은 CAGR 18.88% · 최대낙폭
  62.4% · 202배 · 레짐 변경 93회 · 고정 당시의 회귀 fingerprint 를 그대로 재현한다.
- 워커가 놓친 날에 단계가 바뀌었으면(또는 저장된 행이 다른 경로에서 온 것이면)
  저장된 상태가 바뀌는 날에 이벤트를 남기고 엔진이 실제로 옮긴 날을 적는다. 전에는
  이런 변경이 이벤트 없이 지나갔다.
- '오늘' 탭의 "N일째" 와 진입일은 저장된 행을 세는 대신 엔진의 숫자를 읽는다.
  잠금 막대와 같은 숫자이므로 둘이 어긋날 수 없다.

**확인.** 운영 DB 복사본에서 앵커로부터 이어받은 2026-11-10 의 상태가 30년 전체를
한 번에 재생한 것과 같다 (Optimism, 10/2 진입, 28일째, 대기 Panic 2일). 관측치
앞쪽을 16 · 43 · 110 · 200일 잘라낸 복사본에서도 같다 — 전에는 13가지로 갈리던 것이
한 가지다.

**배포하면 신호가 바뀐다.** 지금 사이트는 Neutral 1.75x (QLD 75 / QQQ 25) 이고
백테스트 경로는 Optimism 1.33x (QLD 33 / QQQ 67) 이다. 첫 배치에서 레짐 변경과
레버리지 변경 알림이 나가고, 이벤트 탭에 "Neutral → Optimism · 실제 전환
2026-10-02" 가 남는다.

**하지 않은 것.**

- **과거 기록은 다시 쓰지 않았다.** 2024-01-02 부터 전환일까지의 저장된 상태와
  모의 성과는 예전 경로 그대로다 (고정 이후 실시간 39일 중 백테스트와 같은 날은
  11일). 실제로 나갔던 신호의 기록이기도 해서 덮어쓰지 않았다. 백테스트 경로로
  다시 쓰려면 전체 이력이 든 DB 에서 재생해 운영 DB 를 교체해야 한다.
- 남은 한계 셋은 `ARCHITECTURE.md` §4.1.1 끝에 적었다 — 발표 시점의 점수와 정정된
  점수의 차이, 뒤늦게 들어오는 휴장일 값, 역대 고점이 5년 창을 벗어나는 경우의
  `drawdown_current`.

**배포 뒤 확인 (2026-11-16).** 11/11 13:19 UTC 의 첫 예약 실행이 앵커에서
이어받았고 (`regime replay resumes from the frozen anchor: Neutral since
2026-06-11, as of 2026-09-11`), 11/10 을 Optimism 1.33x 로 다시 기록했다. 알림
2건(레짐 변경, 목표 레버리지 변경)이 Telegram 으로 나갔고, 모의 성과는 11/11
시가에 QLD 75 → 33% 로 옮겼다 (회전율 0.81, 비용 0.08%). `regime_memory` 는
9/14 부터 하루도 빠짐없이 차 있고 이후 사흘 동안 28 → 31일째로 하루씩 늘었다.

---

## TASK-189 일일 배치가 미국 개장 직전에야 끝나던 문제 — 완료 (2026-11-16)

`.github/workflows/daily_monitor.yml`, `src/fear_ladder/pipeline/gate.py`,
`scripts/batch_gate.py`, `tests/unit/test_batch_gate.py`, `ARCHITECTURE.md` §5.1,
`docs/operations.md` §1.

**증상.** 예약은 06:00 UTC (한국 15:00) 인데 실제 시작은 09:56 ~ 14:23 UTC 였다.
2026-09-15 부터 11-14 까지 예약 실행 45번 전부가 3.9 ~ 8.4시간 늦었고 (평균
5.7시간), 11/12 실행은 14:24 UTC — 미국 개장 5분 전에 끝났다. 신호는 다음 거래일
시가에 실행하는 것이 전제인데, 그 시가 전에 신호를 볼 시간이 없었다.

**원인.** GitHub 의 예약 실행 지연이다. 설정할 수 있는 것이 아니다.

**고친 것.** 예약을 01:17 · 03:17 · 05:17 · 08:17 UTC 네 번으로 늘리고, 각 실행이
먼저 `scripts/batch_gate.py` 에 묻게 했다. 규칙은 `ARCHITECTURE.md` §5.1 에 있다.
요지는 셋이다.

- **하루는 한 번만 계산한다.** 직전 거래일이 기록되어 있으면 어느 예약이든
  물러난다. 조사하다 보니 CNN 이 하루의 값을 뒤에 고친다는 것이 드러났다 —
  2026-11-10 의 값이 04:46 UTC 에 33.5, 13:19 UTC 에 37.1, 다음 날 37.4 였고,
  실시간 42일 중 40일은 워커가 채점한 값과 지금 저장된 값이 다르다 (평균 1.1,
  최대 7.0 포인트. CNN 의 가중치가 5.9% 라 종합 점수로는 평균 0.07, 최대 0.4).
  같은 날을 다시 계산하면 알린 신호가 알림 없이 바뀔 수 있다.
- **입력이 다 모이기 전에는 계산하지 않는다.** 앞의 세 예약은 가격 세 종목 · VIX ·
  CNN 이 모두 새 날짜를 가졌을 때만 실행한다. QQQ 만으로도 커버리지 하한을 넘기
  때문에 이 확인이 없으면 VIX 없는 점수가 그날의 기록이 된다.
- **감시는 멈추지 않는다.** 마지막 예약은 피드 상태와 무관하게 실행하고, 게이트가
  고장 나면 마지막 예약과 수동 실행만 진행한다.

건너뛴 실행은 DB 를 읽기 전용으로만 열어 커밋을 만들지 않는다.

**알아 둘 것.**

- 가격 피드(FinanceDataReader → Yahoo)는 요청한 종료일의 00:00 (실행 환경의 현지
  시각, 러너에서는 UTC) 이전에 시작한 봉만 돌려준다. 00:00 UTC 이전의 예약은 그날
  봉을 볼 수 없으므로 01:17 보다 앞당길 여지는 거의 없다.
- 피드가 언제 그날 봉을 게시하는지는 관측이 넷뿐이다: 00:45 UTC 에는 없었고
  (9/15), 04:45 UTC (11/11) 와 05:15 UTC (9/15) 에는 있었다. 그리고 9/23 에는
  **10:34 UTC 에도 9/22 의 QQQ 봉이 없었다** — 그 행은 다음 날 10:52 UTC 에야
  저장됐고, 9/22 는 끝내 상태가 기록되지 않았다. 게이트가 매 시도마다 피드별 최신
  날짜를 로그에 남기므로, 몇 주 뒤에는 예약을 줄이거나 옮길 근거가 생긴다.
- **놓친 날은 저절로 채워지지 않았다.** 파이프라인은 가장 최근에 채점된 날 하나만
  기록한다. 피드가 하루 늦으면 다음 실행은 그다음 날을 기록하고 늦은 날은
  건너뛴다. 레짐 규칙은 건너뛴 날도 밟고 지나가므로 판정은 맞지만, 그날의 상태
  행은 비어 있었다 (9/15 · 9/16 · 9/22). 지난 빈 날은 TASK-191 의 스크립트가,
  앞으로의 빈 날은 TASK-193 이 채운다.
- 휴장일 다음 날에는 마지막 예약 하나가 새 데이터 없이 돌고 커밋 하나를 남긴다.
  예전의 하루 한 번 실행과 같은 동작이다.
- 예약이 실제로 몇 시에 시작하는지는 여전히 GitHub 에 달려 있다. 네 번 모두 예전처럼
  늦으면 첫 실행은 05:10 ~ 09:40 UTC (한국 14:10 ~ 18:40) 에 돈다. 그보다 확실한
  시각이 필요하면 외부 스케줄러가 `workflow_dispatch` 를 부르게 해야 한다.

**하지 않은 것.** 예약 이벤트는 손으로 일으킬 수 없어, `github.event.schedule` 로
마지막 예약을 가려내는 부분은 첫 예약 실행들의 로그로 확인해야 한다 (08:17 실행의
명령줄에 `--last-attempt` 가 있어야 한다).

---

## TASK-190 대시보드 점검에서 나온 것들 — 완료 (2026-11-16)

`app/views/` 전체, `src/fear_ladder/pipeline/queries.py`,
`scripts/make_equity_curve.py`, `reports/equity_curve.json`,
`tests/unit/test_dashboard_presentation.py`, `tests/unit/test_equity_curve.py`.

운영 중인 페이지를 탭마다 읽어 보고 고친 것들이다. 전략도 저장된 숫자도 바뀌지
않았다 — 같은 숫자를 읽기 쉽게, 그리고 틀리게 읽히지 않게 했다.

**오늘.**

- 맨 위에 **할 일** 한 줄. 페이지 전체가 신호를 설명하는데 그 신호가 매매를
  요구하는지는 어디에도 없었다. 직전 거래일과 목표 비중이 같으면 "할 일 없음",
  다르면 어느 슬리브가 몇 % 에서 몇 % 로 가는지.
- 머리 숫자의 `Regime` · `Market Score` · `Target Leverage` 를 한글로. `Last Update`
  는 제목 아래 줄과 같은 날짜를 한 번 더 적고 있어서, 다른 데서 읽을 수 없는
  **잠금 해제까지 N거래일 · 예상일**로 바꿨다. 예상일은 미국 정규 휴장일을 규칙으로
  넣어 계산한다 (오늘 기준 44거래일 뒤 = 2027-01-20 무렵).

**기록.**

- 레버리지 축 눈금이 `0.87 · 1.056 · 1.241` 이었다. 0 ~ 3 고정, 0.5 단위로.
- 슬리브 색이 탭마다 달랐다 (오늘: QLD 청록 / QQQ 주황, 기록: QLD 연보라 / QQQ
  연두). 하나로 합치면서 검증기를 돌려 보니 기록 탭의 QQQ 연두와 현금 회색은
  ΔE 9.9 로, 서로 다른 색으로 읽히는 하한(15)에 못 미쳤다. 레버리지 순서의 보라
  단계색 셋과 중립 회색으로 바꿨다 (가장 옅은 보라와 회색 사이 ΔE 19).
- "248거래일" 과 "250거래일" 이 한 화면에 있었다. 앞의 것은 저장된 상태의 수였다.
  이제 거래일 수를 적고, 상태가 없는 날이 있으면 며칠인지와 날짜를 적는다.
- 성과 칸의 단계 배경이 선을 가렸다 (Neutral 띠의 회색과 QQQ 선의 회색이 몇 단계
  차이). 배경을 절반 아래로 낮추고, 색은 선이 지나지 않는 위쪽 가장자리의 띠가
  온전한 농도로 맡는다.
- 기간에 **고정 이후**. 고정 직전 종가를 100 으로 둔 실시간 기록만 본다.
- 경로를 정정한 날(2026-11-10)을 세로선으로 표시하고, 10/2 ~ 11/10 의 전략 선이
  백테스트 경로가 아니라 실제로 나갔던 신호의 성과임을 적는다.

**이벤트.** 사유가 모든 행에 "한 칸" 이었다. 유지 기간이 풀리면서 몇 칸을 한
번에 건너는 일이 흔한데도 (2025-04-07 Optimism → Capitulation 은 네 칸). 두
단계 사이의 칸 수를 세어 적고, 변경 전후의 목표 레버리지를 한 칸에 둔다.

**운영.**

- 실행 기록: 영문 컬럼명 · UTC 마이크로초 · `None` 대신 시작 시각(KST) · 걸린 시간 ·
  결과 · 멈춘 단계 · 오류.
- **기록이 어디까지 왔나**: 신호 · 모의 성과 · 단계 기억의 최신 날짜를 나란히
  두고, 하나가 뒤처지면 경고한다. 뒤의 둘은 운영 중에 추가된 기록이라 신호만
  계속 가고 나머지가 멈추는 경우를 볼 방법이 없었다.
- `CNN_FEAR_GREED` 의 `flagged` 1254 / 1254 는 오류가 아니다. 과거 이력(2차
  데이터셋)에 실시간 값을 이어 붙인 시리즈라 전체가 REVIEW 등급으로 저장된다.
  컬럼 이름을 "재구성 등급 행" 으로 바꾸고 그 뜻을 적었다.

**백테스트.**

- 머리 숫자 네 개 아래에 같은 숫자의 QQQ 보유분 (18.88% 아래 13.47%, −62.4% 아래
  −83.0%).
- 누적 수익(로그 축)과 낙폭 곡선. 리포트에는 총계만 있고 경로가 없어서 "202배" 와
  "−62.4%" 가 언제의 일인지 보여 줄 수 없었다. `scripts/make_equity_curve.py` 가
  전체 이력에서 주 단위 곡선을 만들고, 쓰기 전에 최종 배수가 리포트와 같은지
  확인한다. `performance.json` 은 건드리지 않는다.

**모바일.** 390px 폭에서 사이드바가 펼쳐진 채 열려 본문을 덮었다.
`initial_sidebar_state` 를 `auto` 로. 사다리 표의 첫 칸과 마지막 칸이 잘리는 것은
그대로다 (가로로 밀면 보인다).

---

## TASK-191 고정 이전 기록을 백테스트 경로로 — 완료, 운영 DB 적용 (2026-11-16)

`scripts/rewrite_replayed_history.py`, `tests/integration/test_rewrite_history.py`,
`docs/operations.md` §6.

TASK-188 은 그날부터의 판정을 고쳤고 과거 기록은 그대로 두었다. 그 기록 중 고정
이전(2024-01-02 ~ 2026-09-11) 676일은 실제로 알린 적이 없는 재생이고, 백테스트와
단계가 같은 날은 226일이었다. 대시보드의 '1년' 성과와 단계별 일지가 그 경로를
보여 주고 있었다.

스크립트는 그 구간을 백테스트 경로의 재생으로 바꾸고, 고정 이후는 손대지 않는다.
절차와 스스로 하는 확인은 `docs/operations.md` 의 «고정 이전 기록을 백테스트
경로로 되돌리기» 에 있다.

**운영 DB 복사본에서 리허설한 결과.**

- 재생한 676일 전부가 백테스트와 단계 · 목표 레버리지가 같고, 단계 변경 9회가
  이벤트의 전부이며, 재생이 커밋된 앵커에 도착한다.
- 고정 이후 42일의 상태와 `regime_memory` 45행은 한 행도 바뀌지 않았다. 그 DB 에서
  워커를 다시 돌리면 2026-11-13 의 상태가 그대로 나온다.
- 상태가 없던 사흘(9/15 · 9/16 · 9/22)이 채워졌다 (모두 Neutral 1.75x).
- 모의 성과: 2024-01-02 부터 전략 +54.3% → **+91.7%** (QQQ +69.8%), 최대낙폭
  −40.2% → −30.7%. 최근 1년 +16.2% → +19.4% (QQQ +12.6%). 고정 이후 실시간은
  −9.5% → −9.4% 로 사실상 그대로다 — 실제로 나갔던 신호의 성과이기 때문이다.

리허설 첫 시도에서 버그 하나를 잡았다. 전체 이력 DB 는 운영 DB 의 복사본에서
출발하므로 예전 경로의 이벤트를 들고 있었고, 재생은 날짜별로 덮어쓰기만 해서
새 경로에 변경이 없는 날의 옛 이벤트가 살아남아 함께 옮겨졌다 (이벤트 9개가
아니라 18개). 재생 전에 작업본의 계산 결과를 비우고, 이벤트도 백테스트와 대조하게
했다.

**운영 DB 적용 (2026-11-16 14:56 KST).** 예약 실행이 없는 월요일에 적용했고
결과는 리허설과 같다. 적용 전 파일과 행 단위로 따로 대조했다: 고정 이후 42일의
상태 · 배분, `regime_memory` 45행, 관측치 7,841행, 실행 기록 727행, 전송된 알림
2건이 그대로이고, 추가된 것은 비어 있던 사흘의 상태뿐이다. 무결성 검사 통과,
적용한 DB 에서 워커를 다시 돌리면 2026-11-13 Optimism 1.33x 가 그대로 나온다.

---

## TASK-192 보관 기간 하한이 번인을 빼고 계산되던 문제 — 완료 (2026-11-16)

`src/fear_ladder/data/retention.py`, `tests/unit/test_retention.py`.

`required_trading_days` 는 지표마다 자기 조회 기간과 정규화 창 중 **큰 쪽**을
취했다. 둘은 겹치지 않고 쌓인다. 12개월 모멘텀은 값 하나에 252일이 필요하고, 그
점수는 최근 504개 값 가운데의 순위이며, 그 504개 각각이 또 252일을 필요로 한다.
이력의 시작점에 좌우되지 않는 첫 점수는 504일째가 아니라 756일째다. 고정된 전략의
답이 520(AAII 104주)으로 나왔는데 맞는 답은 756 이다.

운영에는 영향이 없었다 — 5년(약 1,260거래일)을 보관하므로. 영향을 받은 것은 이
숫자를 워밍업으로 쓰는 `scripts/backfill_history.py` 다. 2026-09-13 의 2024년
재생은 2021-11 부터의 관측치로 2024-01 을 채점했고(약 535거래일), 처음 몇 달의
점수는 채워지는 중인 창 안의 순위였다. TASK-191 이 그 구간을 전체 이력에서 다시
쓴다.

조회 기간과 창을 더하도록 고쳤다. 정수 파라미터는 모두 더한다 (200일 평균의 21일
기울기는 221일을 본다). `SAFETY_FACTOR` 는 2.0 에서 1.25 로 내렸다 — 2.0 은 빠진
번인을 대신하고 있었고, 이제 번인을 직접 세므로 여유는 휴장일과 빠진 날만
덮으면 된다. 하한은 4.13년에서 3.75년이 됐고 워크플로의 5년은 그대로 통과한다.

**손대지 않은 한계** (`ARCHITECTURE.md` §4.1.1 끝).

- `drawdown_current` 는 불러온 창 안의 최고가를 역대 고점으로 쓴다. 고점이 5년
  창 밖으로 나가는 장기 하락장(2000 ~ 2015 같은)에서만 백테스트와 달라지고, 가중치는
  2.8% 다. 고치려면 고점을 저장해야 한다.
- 난간(추세 필터)의 작동 / 해제 상태도 창 처음부터 재생한다. TASK-191 의 재생에서
  676일의 목표 레버리지가 전부 백테스트와 같았으므로 실제로 어긋난 날은 없다.
- 휴장일 날짜로 뒤늦게 들어오는 지표 값이 유지 일수를 하루 어긋나게 할 수 있다.
  일수를 세는 방식이 고정된 규칙의 일부라 손대지 않았다.

---

## TASK-193 건너뛴 날을 워커가 스스로 채운다 — 완료 (2026-11-16)

`src/fear_ladder/pipeline/daily.py` (`_record_skipped_days`),
`tests/integration/test_regime_memory.py`, `ARCHITECTURE.md` §4.1.1,
`docs/operations.md` §1.

**증상.** 고정 이후 두 달 동안 상태가 없는 거래일이 사흘 생겼다. 둘(9/15 · 9/16)은
워크플로가 실패한 날이고, 하나(9/22)는 배치가 정상으로 돌았는데도 생겼다. 9/23
10:34 UTC 에 야후가 9/22 의 QQQ 봉을 아직 주지 않았고, 그 실행은 9/21 을 다시
기록했으며, 다음 날의 실행은 9/23 을 기록했다. 9/22 는 채점도 됐고 레짐 규칙도
밟고 지나갔지만 행은 끝내 쓰이지 않았다.

**고친 것.** 실행이 오늘의 행을 쓰기 전에, 마지막으로 저장된 상태와 오늘 사이에서
이번 실행이 밟고 온 날들을 본다. **단계와 목표 레버리지가 마지막 저장 상태와 같은
날**에는 행을 쓴다. 사유에 `RECORDED_LATE:<기록한 날>` 이 붙는다 — 그 행의 입력은
제날짜가 아니라 기록한 날의 것이고, 그사이 CNN 이 값을 고쳤을 수 있기 때문이다.

**바뀐 날은 채우지 않는다.** 처음으로 달라지는 날에서 멈춘다. 이유는 둘이다.

- 모의 계좌는 날마다 저장된 목표를 다음 거래일 시가에 체결한다. 건너뛴 날을 새
  목표와 함께 저장하면, 그 신호를 계산한 실행이 돌기도 전의 시가에 거래한 것이
  된다. 실제로는 아무도 할 수 없었던 거래다.
- 변경을 알리는 것은 오늘의 상태와 마지막 저장 상태의 비교다. 사이를 채우면
  오늘은 아무 일 없는 날이 되고 알림이 나가지 않는다.

그 경우는 TASK-188 의 경로가 그대로 맡는다 — 오늘의 행이 마지막 저장 상태와
비교되고, 이벤트가 엔진이 실제로 옮긴 날을 적으며(`REGIME_CHANGED_ON`), 알림이
오늘 나간다. 건너뛴 날은 비어 있고 '기록' 탭이 그 날짜를 적는다.

**확인.** 운영 DB 복사본에서 11/12 와 11/13 의 기록을 지우고 11/13 을 실행했다.
11/12 가 뒤늦게 채워졌고(Optimism 1.33x, 점수 23.05 — 제날짜에는 22.98 이었다.
CNN 정정분이다), 11/13 의 상태 · `regime_memory` · 모의 성과는 실제 기록과 한
자리도 다르지 않았으며, 이벤트와 알림은 생기지 않았다.

**하지 않은 것.** 이미 `regime_memory` 가 지나간 날의 빈 행은 채우지 않는다 — 이번
실행이 밟은 날만 본다. 그런 날은 지금 없고(TASK-191 이 채웠다), 다시 생기려면 상태와
기억을 한 트랜잭션에 쓰는 지금의 구조가 깨져야 한다.

---

## TASK-194 고정 이후의 틀렸던 구간을 엔진 경로로 맞춘다 — 완료 (2026-11-18)

`scripts/realign_live_window.py`, `tests/integration/test_realign_live_window.py`,
`docs/operations.md`, 운영 DB.

**배경.** TASK-188 이전의 워커는 고정 시점의 상태에서 레짐 규칙을 이어받지 않았다.
엔진은 2026-10-02 에 Neutral → Optimism 으로 옮겼는데 저장된 신호는 11-09 까지
27거래일 동안 Neutral 1.75x (QLD 75% · QQQ 25%) 였고, 수정 후 첫 실행이 11-10 에
따라잡기 이벤트(`REGIME_CHANGED_ON:2026-10-02`)로 경로를 되돌렸다. TASK-191 은 이
구간을 "실제로 나간 신호의 기록" 으로 남겼고, '기록' 탭은 그 자리에 "경로 정정"
선을 그었다.

**결정.** 그 신호로 실제 운용한 적이 없으므로, 소유자가 버그의 신호 대신 전략의
신호를 장부에 남기기로 했다.

**한 것.** 그 27일을 파이프라인으로 다시 기록했다. 레짐 규칙은 전날의
`regime_memory` (엔진의 경로)에서 이어받는다. 변경은 10-02 의 평범한 이벤트가 되고
(알림은 `SUPPRESSED`), 11-10 의 따라잡기 이벤트는 지웠으며, 11-10 의 상태는 전날
(Optimism)에 다시 이었다. 11-10 에 실제로 전송된 알림 두 건은 스크립트가 남겼고,
소유자의 요청으로 따로 지웠다 (장부와 날짜가 맞지 않는 기록이라서).
`regime_memory` 는 한 행도 바뀌지 않았고, 다른 날의 상태도 마찬가지다. 다시 쓴 날의
점수는 지금의 입력으로 계산돼 CNN 정정분만큼 다르다 (평균 0.07, 최대 0.41).

**결과.** 이벤트는 고정 이후 2026-06-11 · 2026-10-02 두 개. "경로 정정" 선은 따라잡기
이벤트가 없으니 그려지지 않는다 (코드는 앞으로의 따라잡기를 위해 남겼다). 모의 성과:

| 구간 | 전 | 후 | QQQ |
|---|---|---|---|
| 2026-09-11 종가 ~ 11-16 | −12.6% | −8.7% | −7.4% |
| 2024-01-02 ~ 2026-11-16 | +84.9% | +93.3% | |

리허설과 운영 적용 결과가 모든 표에서 같았고, 적용한 DB 복사본에서 11-16 을 다시
돌리면 같은 상태(Optimism 13.9, 1.33x)가 나온다.

---

## TASK-195 외부 스케줄러가 워크플로를 시작시킨다 — 완료 (2026-11-18)

`.github/workflows/daily_monitor.yml`, `tests/unit/test_workflow_triggers.py`,
`ARCHITECTURE.md` §5.1, `docs/operations.md` §1, `README.md`.

**증상.** TASK-189 로 예약을 네 번으로 나눈 첫날(2026-11-17)에도 GitHub 는 네 번
모두 7.9 ~ 9.3시간 늦게 시작했다 (예약 10:17 KST → 실제 18:13). 하루의 일은
18:14 KST 에 끝났지만, 지연이 한 시간만 더 늘어도 미국 개장(23:30 KST)을 넘긴다.

**고친 것.** `workflow_dispatch` 는 예약 실행과 달리 요청한 때 바로 시작한다. 그래서
cron-job.org 가 REST API 로 워크플로를 시작시킨다. 서버는 필요 없다. 수동 실행에
입력 `attempt` 를 더했다.

- `manual` (기본값): 지금까지의 수동 실행 — 게이트를 거치지 않고 항상 실행한다.
  사람이 "Run workflow" 를 누르면 이것이다.
- `scheduled`: 외부 스케줄러의 시도. GitHub 예약과 같은 게이트를 거친다.
- `last`: 외부 스케줄러의 마지막 시도. `--last-attempt` 가 붙는다.

이것이 없으면 외부에서 하루 여러 번 부를 때마다 같은 날을 다시 계산하고
커밋한다. GitHub 의 예약 네 번은 예비로 그대로 둔다. 먼저 일한 쪽이 기록하면
나머지는 모두 물러난다.

**설정.** 토큰과 cron-job.org 작업은 저장소 밖의 일이라 `docs/operations.md` 의
"외부 스케줄러 (cron-job.org)" 절에 절차를 적었다 (fine-grained 토큰, 이 저장소의
Actions 읽기·쓰기만; 11:07 · 13:07 · 15:07 `scheduled`, 18:07 `last`, 화~토 KST).

**확인.** GitHub 표현식은 로컬에서 돌릴 수 없으므로, 게이트 스텝의 표현식과 입력
정의, `LAST_ATTEMPT_CRON` 이 예약 중 가장 늦은 것인지를 테스트로 고정했다.
실제 동작은 배포 뒤 `gh workflow run daily_monitor.yml -f attempt=scheduled` 로 본다.

---

## TASK-196 자동 시도는 이미 기록된 날을 다시 쓰지 않는다 — 완료 (2026-11-18)

`src/fear_ladder/pipeline/daily.py` (`keep_recorded`, `DailyResult.kept`),
`scripts/daily_runner.py` (`--keep-recorded`), `.github/workflows/daily_monitor.yml`,
`tests/integration/test_keep_recorded.py`, `tests/unit/test_workflow_triggers.py`,
`ARCHITECTURE.md` §5.1, `docs/operations.md` §1.

**증상.** 마지막 시도는 피드에 새 날짜가 없어도 돈다 (휴장일과 멈춘 피드를 드러내는
것이 그 역할이다). 그러면 파이프라인은 채점할 수 있는 가장 최근 날 — 이미 기록되고
알린 날 — 을 그사이 고쳐진 입력으로 다시 계산해 덮어썼다. 2026-11-18 오전에
cron-job.org 의 마지막 작업을 시험 실행하자 11/16 의 점수가 13.877 → 13.941 로
바뀌었다 (CNN 정정분, 단계 · 레버리지는 그대로). 점수가 단계 경계에 있었다면 이미
알린 단계나 목표 레버리지가 바뀌었을 것이다. 알림은 날짜로 중복 제거되므로 알림
없이, 그리고 모의 계좌는 바뀐 목표로 다음 시가에 거래한다. 휴장일 다음 날마다
(외부와 GitHub 의 마지막 시도 둘 다), 피드가 하루 늦은 날마다 일어날 일이었다.

**고친 것.** 사람이 시키지 않은 시도(GitHub 예약, 외부 `scheduled` / `last`)는
`--keep-recorded` 로 돈다. 채점할 수 있는 가장 최근 날에 이미 상태가 있으면 신선도
검사와 실행 기록만 남기고 저장된 상태를 그대로 둔다. 요약 줄에
`[already recorded; kept]` 가 붙는다. 새 날이 있으면 평소대로 기록한다. 사람의 수동
실행(`attempt: manual`)과 `--date` 재실행은 지금처럼 다시 계산한다.

**확인.** 하루를 기록한 뒤 그날의 입력을 고치고 다시 돌리면: `keep_recorded` 로는
상태 · 이벤트 · 알림이 그대로이고 실행 기록만 하나 는다. 없이 돌리면 점수가 바뀐다
(시험의 전제). 운영 DB 복사본에서 `--keep-recorded` 로 돌리면 11/16 행이 바이트까지
같다. 테스트 912 -> 916.
