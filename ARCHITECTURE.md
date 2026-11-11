# ARCHITECTURE.md

# NASDAQ Leverage Regime Monitor — Architecture

## 1. System Goal

시스템의 핵심은 다음이다.

```text
Daily Market Data
    ↓
Indicator Engine
    ↓
Composite Score
    ↓
Market Regime
    ↓
Target Leverage
    ↓
Target Allocation
    ↓
Regime Change Detection
    ↓
SQLite Persistence
    ↓
Telegram Alert
```

Streamlit은 저장된 결과를 시각화하는 Dashboard 역할을 담당한다.

---

# 2. Deployment Architecture

```text
                         GitHub Repository
                                │
              ┌─────────────────┼─────────────────┐
              │                 │                 │
              ▼                 ▼                 ▼
          Source Code         SQLite          Workflows
              │                 │                 │
              │                 │                 ▼
              │                 │           GitHub Actions
              │                 │                 │
              │                 │                 ▼
              │                 │          daily_runner.py
              │                 │                 │
              │                 │        ┌────────┴────────┐
              │                 │        ▼                 ▼
              │                 │   Market Analysis    Alert Engine
              │                 │        │                 │
              │                 ◄────────┘                 ▼
              │                 │                      Telegram
              │                 │
              ▼                 │
        Streamlit Cloud ◄───────┘
              │
              ▼
          Dashboard
```

중요:

**로컬 PC는 운영 경로에 포함되지 않는다.**

---

# 3. Repository Structure

```text
FearLadder/
├── README.md
├── PRD.md
├── ARCHITECTURE.md
├── BACKTEST_SPEC.md
├── TASKS.md
├── CONTRIBUTING.md
│
├── pyproject.toml
├── .env.example
├── .gitignore
│
├── config/
│   ├── indicators.yaml      # 지표 정의 (손으로 관리, 연구 후보 그리드 포함)
│   ├── strategy.yaml        # FROZEN 출력. 손으로 고치지 않는다
│   ├── alerts.yaml
│   ├── data_sources.yaml
│   ├── frozen/              # v1.0-frozen.{manifest,regression,indicators,oos}
│   └── research/            # 탐색용 프로파일 (candidate / placeholder)
│
├── src/
│   └── fear_ladder/
│       ├── config/
│       ├── data/
│       │   ├── models.py
│       │   ├── interfaces.py    # 포트 (Protocol)
│       │   ├── retention.py     # 보관 기간의 하한을 설정에서 계산
│       │   ├── collectors/
│       │   ├── repositories/    # sqlite/ 아래 표 그룹별 어댑터
│       │   └── validators/
│       ├── indicators/
│       ├── scoring/
│       ├── regime/
│       ├── allocation/        # 배분 + TQQQ 게이트 + 추세 필터
│       ├── backtest/
│       ├── research/        # 연구 경로 (§20). measurement.py 는 사후 측정 스크립트가
│       │                    # 공유하는 창·변형·지표 (ARCHITECTURE §3 "얇은 CLI")
│       ├── pipeline/        # 일일 워커 + 대시보드 조회
│       ├── alerts/
│       └── monitoring/
│
├── scripts/                 # 얇은 CLI. 로직은 패키지 안에 있다
│   ├── daily_runner.py      # 운영: 하루치 실행
│   ├── notify.py            # 알림 채널 점검·복구
│   ├── prune_observations.py
│   ├── backfill_history.py
│   ├── collect_full_history.py   # 30년 이력 DB (저장소 밖에 만든다)
│   ├── fetch_reference.py
│   ├── backtest.py          # 연구
│   ├── optimize.py
│   ├── validate.py
│   ├── freeze.py
│   ├── make_performance_report.py
│   ├── make_placeholder_indicators.py
│   ├── make_favicon.py
│   │                        # 고정 이후의 사후 측정 (§21, docs/research-backlog.md).
│   │                        # 전부 config/ 를 복사해 쓰고, 한 글자도 쓰지 않는다.
│   ├── transition_sensitivity.py
│   ├── tail_risk.py
│   ├── reconstruction_accuracy.py
│   ├── cross_market.py
│   ├── indicator_ablation.py
│   ├── event_analysis.py
│   ├── cost_model.py
│   └── cash_exposure.py
│
├── app/
│   ├── streamlit_app.py     # 탭 배선만
│   ├── assets/favicon.png
│   └── views/               # 탭 하나당 모듈. 엔진을 import 하지 않는다
│                            # (reason_codes 를 문장으로 옮기는 일은 views/common.py)
│
├── data/
│   ├── fear_ladder.db
│   └── reference/           # 운영자가 배치하는 외부 자료 (ProShares/CNN/AAII).
│                            # 미추적 — 제3자 데이터를 재배포하지 않는다
│
├── docs/
│   ├── strategy.md
│   ├── operations.md
│   └── research-backlog.md  # 고정 이후 측정의 준비 절차·규칙·이미 잰 것
│
├── tests/
│   ├── unit/
│   ├── integration/
│   └── backtest/
│
├── .github/
│   └── workflows/
│       ├── daily_monitor.yml
│       └── test.yml
│
└── reports/
    ├── performance.json     # 대시보드가 읽는 성과 증거
    ├── *.json               # 사후 측정 결과. docs/strategy.md §2.10–§2.18 의 증거물
    └── backtest/            # scripts/backtest.py --report 산출물 (미추적)
```

---

# 3.5 Data Provider Architecture

가격 데이터의 애플리케이션 기본 진입점은 FinanceDataReader adapter다.

```text
PriceProvider
      │
      └── FinanceDataReaderPriceProvider
```

단, FDR을 원천 데이터 공급자와 동일시하지 않는다.

핵심 ETF는 필요할 경우:

```text
FinanceDataReader
      +
ProSharesVerifier
```

구조로 교차검증한다.

권장 interface:

```python
class PriceProvider(Protocol):
    def fetch(
        self,
        symbol: str,
        start: date,
        end: date,
    ) -> DataFrame: ...
```

Provider 내부에서 사용하는 source/provider 정보는 raw metadata에 기록한다.

필수 metadata:

```text
provider_library
provider_library_version
underlying_source
retrieved_at
```

# 4. Runtime Components

## 4.1 Daily Worker

Entry point:

```text
scripts/daily_runner.py
```

책임:

1. 오늘 필요한 데이터를 수집한다.
2. 데이터 품질을 검증한다.
3. 지표를 계산한다.
4. Score를 계산한다.
5. Regime을 계산한다.
6. Target Allocation을 계산한다.
7. 이전 상태와 비교한다.
8. Event를 생성한다.
9. SQLite에 저장한다.
10. 필요한 경우 Telegram 알림을 보낸다.
11. 모의 성과 기록(`track_record`)을 하루 이어 붙인다 (§4.1.3).

Daily Worker는 주문을 실행하지 않는다.

### 4.1.1 지표와 점수는 재생하고, 레짐은 이어받는다

Daily Worker 는 매 실행마다 **지표와 점수를 불러온 이력에서 처음부터 다시
계산한다.** 둘 다 정해진 길이만큼만 뒤를 보므로(지표 창과 백분위 창을 합쳐 3년
남짓) 최근 날짜는 창이 어디서 시작하든 같은 점수를 받고, 누적 상태가 없으니 재실행이
본질적으로 멱등하다 (§11).

**레짐 전이는 그렇게 하지 않는다 (TASK-188).** 최소 유지 기간이 있는 규칙은 시작한
날을 끝까지 기억한다 — 단계는 대개 유지 기간이 끝나는 날 바뀌고, 다음 유지 기간은
그날부터 세므로, 첫날이 하루 밀리면 그 뒤의 모든 전환일이 따라 밀린다. 워커가
읽는 창은 매일 하루씩 앞으로 밀리므로(lookback 2000일, 저장은 5년), 창 처음부터
전이를 재생하면 같은 날짜가 물어본 날에 따라 다른 레짐을 받는다. 실제로 2026-11-10
한 날짜가 시작일만 바꿔도 Neutral · Optimism · Fear 로 갈렸고, 저장된 715일 중
백테스트와 같은 레짐인 날은 237일뿐이었다.

그래서 워커는 전이 상태를 **이어받는다.**

1. `regime_memory` 에 전이 엔진이 그날 들고 나온 상태(단계 · 진입일 · 유지 일수 ·
   대기 중인 후보)를 날마다 저장한다. 다음 실행은 계산할 날 **직전** 행에서
   한 걸음만 나아간다.
2. 저장된 것이 없는 첫 실행은 `config/frozen/<version>.regime_anchor.json` 에서
   시작한다. 고정 백테스트(1996~)의 재생이 마지막 날에 있던 상태다. 그러므로
   실시간 레짐은 **백테스트의 재생을 그대로 이어간 것**이고, "백테스트가 보여주는
   과거 날짜의 레짐이 워커가 그날 산출했을 레짐" 이라는 말은 이 뜻에서만 참이다.
3. 워커가 돌지 않은 날은 다음 실행이 한꺼번에 밟고, 밟은 날마다 행을 남긴다.
   그 사이에 단계가 바뀌었으면 저장된 상태가 바뀌는 날에 이벤트를 적고 엔진이
   실제로 옮긴 날을 사유에 남긴다 (`REGIME_CHANGED_ON:<날짜>`).
4. 재실행은 여전히 멱등하다 — 같은 행에서 같은 날을 다시 밟는다.

앵커는 설정이 아니라 증거다. `scripts/make_regime_anchor.py` 가 전체 이력에서
재생해 쓰고, 쓰기 전에 연구 구간의 fingerprint 가 고정 당시 것과 같은지 확인한다.
잘린 운영 DB 에서는 만들 수 없다. 이어받을 지점이 불러온 이력보다 오래되면
워커는 추측하지 않고 그날을 UNKNOWN 으로 남긴다 (`REGIME_MEMORY_OUT_OF_WINDOW`).

**남아 있는 한계.** (a) 워커가 그날 본 점수는 그날까지 발표된 값이고 백테스트는
나중에 정정된 값을 읽는다 — 고정 이후 39일에서 차이는 대부분 0.3점 이하, 가장 큰
날이 1.5점이었고, 유지 기간이 끝난 바로 그날 경계에 걸리지 않는 한 경로를 바꾸지
않는다. (b) 엔진은 지표 값이 하나라도
있는 날을 하루로 센다. 휴장일 날짜로 뒤늦게 들어오는 값(AAII)은 백테스트에서는
하루지만 워커가 이미 지나간 뒤라면 세지 않는다. (c) `drawdown_current` 는 역대
고점 대비 낙폭이라, 고점이 5년 창 안에 있는 동안만 백테스트와 같다.

### 4.1.2 실패 처리

필수 데이터가 결측·낡음·미래일자·손상이면 `Regime = UNKNOWN` 으로 기록하고,
점수·배분·레버리지를 만들지 않으며, **해당 날짜에 이미 저장돼 있던 배분을
삭제한다.** 나쁜 데이터가 이전 조언을 그대로 세워두지 않게 하기 위해서다.

### 4.1.3 모의 성과 기록 — 이어 붙이는 기록

`pipeline/track_record.py`. 저장된 목표 비중을 그대로 따른 계좌가 얼마가 됐는지를
QQQ 보유와 나란히 하루 한 줄씩 적는다. 대시보드 '기록' 탭의 성과 비교가 이
테이블을 읽는다.

- **계산은 백테스트의 것이다.** 매일 `PortfolioSimulator` 를 그대로 지나간다 —
  다음 거래일 시가 체결, 단계가 바뀔 때만 매매, 거래마다 고정된 비용. 벤치마크는
  같은 시뮬레이터에 QQQ 100% 를 넣은 것이다. 두 번째 회계를 따로 두지 않는다.
- **레짐 기억(§4.1.1)처럼 이어 붙이고 재생하지 않는다.** 원시 가격은 5년 창으로
  잘리므로(§6, `data/retention.py`) 재생하는 기록은 가격이 잘리는 날 앞부분을
  잃는다. 그래서 각 행이 다음 행에 필요한 것 — 드리프트된 보유 비중과 마지막으로
  체결한 목표 — 을 들고 있고, 새 날은 마지막 행에서 계산한다. 하루씩 붙인 결과와
  한 번에 계산한 결과가 같다는 것은 테스트가 강제한다. 한 번 적힌 날은 다시
  쓰지 않으므로 재실행은 여전히 멱등하고, 나중의 정정이 과거 성과를 고치지 못한다.
- **신호가 없는 날은 보유다.** 워커가 돌지 않았거나 UNKNOWN 인 날은 직전 보유를
  그대로 들고 간다.
- **실패해도 그날의 신호를 막지 않는다.** 파이프라인에서 예외를 삼키는 유일한
  단계다. 상태 저장과 알림이 끝난 뒤에 돌고, 실패하면 다음 성공한 실행이 마지막
  행부터 이어 붙인다. 뒤처진 정도는 '기록' 탭에 표시된다.
- 이력을 일부러 다시 쓰는 `scripts/backfill_history.py` 만 끝에서 기록을
  재구성한다. 가격이 이미 잘린 구간까지 재구성해야 하면 거부한다.

---

## 4.2 Streamlit Dashboard

Entry point:

```text
app/streamlit_app.py
```

책임:

- SQLite의 현재 상태 조회
- 과거 상태 조회
- 지표 시각화
- Regime timeline 표시
- Backtest report 표시

Streamlit 요청 처리에서 새로운 전략 계산을 수행하는 것을 기본 동작으로 하지 않는다.

---

# 5. GitHub Actions

Scheduled workflow가 daily runner를 실행한다.

개념:

```yaml
name: daily-monitor

on:
  schedule:
    - cron: "..."
  workflow_dispatch:
```

workflow는 반드시:

```text
checkout
→ install dependencies
→ run daily_runner.py
→ persist updated SQLite
→ send/update outputs
```

의 흐름을 갖는다.

실제 운영 시 timezone과 미국 거래일/휴장일을 별도로 처리한다.

---

# 6. SQLite Strategy

DB 파일:

```text
data/fear_ladder.db
```

권장 테이블:

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

구현 시 추가된 테이블:

```text
schema_migrations       스키마 버전 기록
data_quality_findings   교차검증 불일치 (TASK-028)
track_record            목표 비중을 따른 모의 계좌와 QQQ 보유, 하루 한 줄 (§4.1.3)
regime_memory           전이 엔진이 그날 들고 나온 상태 — 다음 실행이 이어받는 곳 (§4.1.1)
```

`data_quality_findings` 가 필요한 이유는 BACKTEST_SPEC §5.5 때문이다.
불일치를 자동 수정하지 않고 REVIEW 로 '남겨야' 하므로, 관측 자체와 별개로
사람이 검토할 대상을 담을 곳이 있어야 한다.

모든 일일 기록 테이블은 자연키에 UNIQUE 제약을 갖는다. 재실행이 중복을
만들 수 없는 것은 애플리케이션 로직이 아니라 DB 제약으로 보장된다.

---

# 7. Repository Abstraction

Application layer는 SQLite 구현체를 직접 사용하지 않는다.

예:

```python
class MarketStateRepository(Protocol):
    def get_latest_state(self) -> MarketState | None: ...
    def save_state(self, state: MarketState) -> None: ...
```

구현:

```text
SQLiteMarketStateRepository
```

향후:

```text
PostgresMarketStateRepository
```

로 교체 가능해야 한다.

---

# 8. SQLite + Git Operations

Daily worker 이후 변경된 DB를 repository에 반영하는 방식을 기본 운영안으로 사용한다.

개념:

```text
checkout
→ load SQLite
→ update DB
→ validate
→ commit changed DB
→ push
```

단, 민감정보와 secret은 절대 DB나 repository에 저장하지 않는다.

---

# 9. Secrets

다음은 GitHub Secrets 또는 환경 변수로 관리한다.

```text
TELEGRAM_BOT_TOKEN
TELEGRAM_CHAT_ID
DATA_PROVIDER_API_KEY
기타 인증정보
```

금지:

```text
소스코드에 token 직접 입력
config yaml에 plaintext secret 저장
SQLite에 secret 저장
```

---

# 10. Alert Architecture

```text
Regime Change
      ↓
AlertEvent
      ↓
Alert Engine
      ↓
NotificationProvider
      ↓
TelegramNotifier
```

예시 인터페이스:

```python
class NotificationProvider(Protocol):
    def send(self, event: AlertEvent) -> None: ...
```

---

# 11. Idempotency

Daily worker는 동일 날짜에 반복 실행되어도 안전해야 한다.

같은 실행을 두 번 수행해도:

- duplicate state 없음
- duplicate event 없음
- duplicate Telegram notification 없음

Event에 unique key를 둔다.

예:

```text
event_date + event_type + regime_version
```

---

# 12. Data Flow

```text
External Sources
      ↓
Data Collectors
      ↓
Raw/Processed Data
      ↓
Indicator Engine
      ↓
Indicator Scores
      ↓
Score Engine
      ↓
Market State
      ↓
Regime Engine
      ↓
Allocation Engine
      ↓
SQLite
      ↓
┌───────────────┬───────────────┐
↓                               ↓
Streamlit                    Alert Engine
                                  ↓
                              Telegram
```

---

# 13. Configuration

모든 전략 숫자는 configuration에서 관리한다.

```yaml
score:
  weights: null

regime:
  count: null
  boundaries: null

allocation:
  mappings: null

transition:
  confirmation_days: null
  hysteresis: null
```

`null`은 백테스트로 확정되지 않은 연구 파라미터를 뜻한다. **운영본
`config/strategy.yaml` 에는 더 이상 `null` 이 없다** — 2026-09-13 에
`v1.0-frozen` 으로 고정됐고, `null` 이 남은 것은
`config/research/` 아래 탐색용 프로파일뿐이다.

---

# 14. Strategy Versioning

각 strategy는 version을 갖는다.

```text
v0.1-research
v0.2-validation
v1.0-frozen
```

SQLite에도 active strategy version을 저장한다.

Daily state에는 다음이 기록된다.

```text
strategy_version
data_version
parameter_version
```

---

# 15. Failure Safety

필수 데이터가 누락되거나 freshness 조건을 만족하지 못하면:

```text
Market Regime = UNKNOWN
```

으로 처리하거나 해당일 분석을 중단한다.

잘못된 데이터를 기반으로 정상적인 투자 알림을 보내면 안 된다.

---

# 16. Data Freshness

각 데이터의:

```text
observation_date
availability_datetime
retrieved_at
```

를 저장한다.

운영 시 예상 수집 시간보다 지나치게 오래된 데이터는 stale로 판정한다.

---

# 17. Dashboard Views

탭 하나당 `app/views/` 아래 모듈 하나. 배선은 `app/streamlit_app.py` 의 `TABS` 에만
있다. 어느 탭도 엔진을 import 하지 않는다 (§4.2).

탭은 두 종류이고 순서가 그 구분이다. **앞의 넷(오늘 · 기록 · 이벤트 · 운영)은 일일
워커가 매일 다시 쓰고, 뒤의 둘(전략 설명 · 백테스트)은 전략을 고정할 때 한 번 쓴
문서라 바뀌지 않는다.** 각 탭은 첫 줄에서 자기가 어느 쪽인지 밝힌다
(`views/common.py` 의 `updated_daily` / `fixed_at_freeze`) — 어제 움직인 숫자와
영영 움직이지 않을 숫자가 같은 얼굴을 하고 있으면 안 되기 때문이다.

## 오늘 — `views/today.py` (매일 갱신)

- Current Regime
- Market Score
- Target Leverage
- 200일선 이격과 난간(추세 필터) 상태 — 작동선·해제선까지의 거리, 52주 낙폭 조건
- Target Allocation
- Key Indicators
- Last Update

난간 패널은 저장된 `indicator_values` 와 `reason_codes` 만 읽는다. 작동 여부는
워커가 남긴 코드에서 가져오고(필터에 기억이 있어 오늘 값만으로는 알 수 없다),
대시보드가 하는 일은 각 선까지의 거리를 % 와 달러로 옮기는 것뿐이다.

## 기록 — `views/history.py` (매일 갱신)

- 전략(모의 계좌) vs QQQ — `track_record` (§4.1.3), 기간 첫날을 100 으로
- Market Score
- Regime Transition (점수가 가리킨 칸 vs 실제 칸)
- Target Allocation + Target Leverage

## 이벤트 — `views/events.py` (매일 갱신)

- Event History
- 알림 발송 상태

## 운영 — `views/operations.py` (매일 갱신)

- 최근 파이프라인 실행과 그 결과
- 데이터 신선도 · 교차검증 findings
- 활성 전략 버전

## 전략 설명 — `views/strategy.py` (고정 문서)

- 사다리 구조와 각 단계의 목표 비중
- 추세 필터 · TQQQ 게이트가 무엇을 언제 막는가

## 백테스트 — `views/performance.py` (고정 문서)

`reports/performance.json` 을 읽는다. 대시보드는 백테스트를 돌리지 않는다.
고정 시점까지의 1996–2026 을 한 번 계산한 증거이고, 고정 이후에 쌓이는 성과는
여기가 아니라 '기록' 탭에 있다 — 그래서 탭 이름이 '성과' 가 아니다.

- Equity Curve
- Drawdown
- CAGR / Sharpe / Sortino / Calmar
- Benchmark Comparison

---

# 18. Testing Architecture

```text
Unit
 ↓
Integration
 ↓
Backtest Leakage Tests
 ↓
End-to-End
 ↓
Scheduled Runner Dry Run
```

GitHub Actions의 test workflow와 daily workflow를 분리한다.

---

# 19. Design Constraints

1. 자동주문 코드 없음.
2. 증권사 API 의존 없음.
3. 로컬 PC 상시 실행 필요 없음.
4. DB는 Repository 계층 뒤에 둔다.
5. Streamlit은 Dashboard 역할 중심.
6. Daily Worker는 batch job으로 동작.
7. 모든 전략 파라미터는 versioned configuration으로 관리.

---

# 20. Research / Production Separation

`BACKTEST_SPEC.md` §28 의 요구를 패키지 경계로 구현한다.

```text
fear_ladder.research/     연구 경로
  backtest_runner.py         전체 체인 백테스트
  search.py                  파라미터 탐색 (TASK-080~085)
  walk_forward.py            TASK-091
  sensitivity.py             TASK-093
  splits.py                  데이터셋 분할 + OOS 가드
  freeze.py                  TASK-101/102
  reports.py                 아티팩트

fear_ladder.pipeline/     운영 경로
  daily.py                   일일 워커
  queries.py                 대시보드 조회 (read-only)
```

**`pipeline` 은 `research` 를 import 하지 않는다.** 이는 규약이 아니라 테스트로
강제된다 (`tests/backtest/test_leakage.py`). 운영 워커가 optimizer 를 호출해
전략을 바꾸는 경로가 물리적으로 존재하지 않는다.

마찬가지로 대시보드(`app/streamlit_app.py`)는 엔진 모듈을 import 하지 않으며,
DB 를 read-only 로 연다.

---

# 21. OOS Protection

`SplitGuard` 가 보호 구간에 대한 최적화 접근을 예외로 막는다 (`BACKTEST_SPEC.md`
§20). 긴 프로젝트에서 규율은 먼저 무너지므로, 구조로 막는다.

해제는 가능하지만 명시적이어야 하고 사유를 요구하며, 해제 순간 그 구간이 더는
깨끗한 OOS 가 아니라는 경고를 로그에 남긴다.
