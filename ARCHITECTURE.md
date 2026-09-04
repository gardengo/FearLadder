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
nasdaq-leverage-regime-monitor/
├── README.md
├── PRD.md
├── ARCHITECTURE.md
├── BACKTEST_SPEC.md
├── TASKS.md
├── CLAUDE_CODE_INITIAL_PROMPT.md
│
├── pyproject.toml
├── Dockerfile
├── .env.example
├── .gitignore
│
├── config/
│   ├── indicators.yaml
│   ├── strategy.yaml
│   ├── alerts.yaml
│   └── data_sources.yaml
│
├── src/
│   └── regime_monitor/
│       ├── config/
│       ├── data/
│       │   ├── models.py
│       │   ├── interfaces.py
│       │   ├── collectors/
│       │   ├── repositories/
│       │   └── validators/
│       ├── indicators/
│       ├── scoring/
│       ├── regime/
│       ├── allocation/
│       ├── backtest/
│       ├── alerts/
│       └── monitoring/
│
├── scripts/
│   ├── daily_runner.py
│   └── backtest.py
│
├── app/
│   └── streamlit_app.py
│
├── data/
│   ├── regime_monitor.db
│   ├── raw/
│   ├── processed/
│   └── snapshots/
│
├── tests/
│   ├── unit/
│   ├── integration/
│   ├── backtest/
│   └── fixtures/
│
├── .github/
│   └── workflows/
│       ├── daily_monitor.yml
│       └── test.yml
│
└── reports/
    └── backtest/
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

Daily Worker는 주문을 실행하지 않는다.

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
data/regime_monitor.db
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

`null`은 아직 백테스트로 확정되지 않은 연구 파라미터다.

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

## Current

- Current Regime
- Market Score
- Target Leverage
- Target Allocation
- Key Indicators
- Last Update

## History

- QQQ Price + Regime
- Market Score
- Target Leverage
- Regime Transition
- Event History

## Backtest

- Equity Curve
- Drawdown
- CAGR
- Sharpe
- Sortino
- Calmar
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
