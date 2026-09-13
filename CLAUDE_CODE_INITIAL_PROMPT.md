# CLAUDE_CODE_INITIAL_PROMPT.md

# Claude Code 초기 개발 지시문

당신은 이 repository의 수석 Python 엔지니어이자 quantitative research engineer다.

이 프로젝트의 목적은 Nasdaq-100 시장 레짐을 매일 분석하여 QQQ / QLD / TQQQ / Cash의 목표 비중을 제시하고, 중요한 레짐 변화가 발생하면 사용자에게 Telegram 알림을 보내는 **투자 의사결정 보조 시스템**을 구축하는 것이다.

자동매매 시스템이 아니다.

---

# 1. 반드시 먼저 읽어야 하는 문서

개발을 시작하기 전에 repository root의 다음 문서를 모두 읽어라.

```text
PRD.md
ARCHITECTURE.md
BACKTEST_SPEC.md
TASKS.md
```

문서 간 역할:

```text
PRD.md
→ 무엇을 만들 것인가

ARCHITECTURE.md
→ 시스템을 어떻게 구성할 것인가

BACKTEST_SPEC.md
→ 전략을 어떻게 검증할 것인가

TASKS.md
→ 어떤 순서로 구현할 것인가
```

문서가 직접 충돌할 경우 우선순위:

```text
1. BACKTEST_SPEC.md
2. PRD.md
3. ARCHITECTURE.md
4. TASKS.md
```

단, 안전성/데이터 누수와 관련된 규칙은 최우선이다.

---

# 2. 절대 변경하면 안 되는 핵심 원칙

## 2.1 자동매매 금지

다음을 구현하지 마라.

```text
증권사 주문 API
자동 매수
자동 매도
자동 리밸런싱
실제 주문 전송
```

시스템은 분석하고 알림만 보낸다.

---

## 2.2 로컬 PC를 운영 서버로 사용하지 않는다

운영 환경에서 사용자의 로컬 PC를 상시 켜둘 것을 전제로 하지 마라.

기본 배포 구조:

```text
GitHub
+
GitHub Actions
+
SQLite
+
Streamlit Cloud
+
Telegram
```

GitHub Actions가 daily batch worker 역할을 한다.

---

# 3. 운영 구조

최종 구조는:

```text
GitHub Repository
│
├── Source Code
├── Configuration
├── Tests
├── SQLite
└── GitHub Actions
          │
          ▼
    Daily Worker
          │
          ├── Data Collection
          ├── Indicators
          ├── Market Score
          ├── Regime
          ├── Allocation
          ├── SQLite Update
          └── Alert
                   │
                   ▼
                Telegram


Streamlit Cloud
      │
      ▼
SQLite / Repository
      │
      ▼
Dashboard
```

Streamlit은 일일 batch 실행의 핵심 orchestrator가 아니다.

---

# 4. SQLite 정책

MVP DB는:

```text
data/fear_ladder.db
```

이다.

SQLite 선택 이유:

- 단일 사용자
- 일일 시계열
- 낮은 동시성
- 프로젝트 내부 관리 가능
- 별도 DB 서버 불필요

그러나 application code가 SQLite에 강하게 결합되지 않도록 Repository/Service 계층을 둬라.

향후 PostgreSQL로 교체 가능해야 한다.

---

# 5. GitHub Actions 정책

Daily workflow는 미국 시장 종료 후 필요한 데이터가 확정될 수 있는 시점에 실행한다.

필수:

```text
schedule
workflow_dispatch
```

실행 과정:

```text
checkout
→ install
→ run tests
→ daily_runner
→ validate
→ update SQLite
→ detect events
→ send Telegram
→ commit changed database
→ push
```

daily runner 실패 시 잘못된 정상 상태를 push하지 마라.

---

# 6. Telegram 정책

MVP 알림은 Telegram을 사용한다.

Secrets:

```text
TELEGRAM_BOT_TOKEN
TELEGRAM_CHAT_ID
```

절대 코드 또는 repository에 secret을 하드코딩하지 마라.

알림은:

```text
NotificationProvider
```

interface 뒤에 둔다.

---

# 6.5 데이터 수집 라이브러리

미국 가격 데이터의 기본 Python 수집 인터페이스는 **FinanceDataReader**로 구현한다.

중요:

```text
FinanceDataReader
≠
원자료 공급자
```

FDR이 내부적으로 사용하는 실제 source/provider와 FDR 버전을 provenance에 기록하라.

핵심 레버리지 ETF:

```text
QQQ
QLD
TQQQ
```

에 대해서는 가능한 경우 ProShares 공식 자료로 교차검증하라.

FDR 호출 실패 시 다른 provider를 코드에 임의로 추가하지 마라. 먼저 Data Provider Adapter 구조를 활용하고, fallback provider가 문서에 정의되어 있는지 확인한다.

# 7. 전략 관련 절대 규칙

초기 기본 아이디어:

```text
QLD 70%
Cash 30%
```

시장 하락/공포가 심해질수록 레버리지 증가.

시장 과열이 심해질수록 레버리지 감소.

과열 상태에서도 시장을 완전히 이탈하지 않는다.

TQQQ는 가장 극단적인 공포/바닥 후보에서 사용한다.

하지만 다음 숫자를 개발자가 임의로 확정하지 마라.

```text
indicator weights
thresholds
regime count
regime boundaries
allocation weights
confirmation days
hysteresis
TQQQ entry threshold
```

이 값들은 백테스트/검증 결과로 결정한다.

---

# 8. 백테스트 원칙

절대 위반하지 마라.

## No Look-ahead Bias

t일 signal은 t일까지 실제 이용 가능한 정보만 사용해야 한다.

## No Same-Day Execution

기본:

```text
t close
→ signal
→ t+1 execution
```

## No Full-Sample Normalization

과거 데이터 전체를 보고 계산한 percentile/mean/std를 과거 signal에 적용하지 마라.

## No OOS Contamination

OOS 결과를 보고 전략 파라미터를 수정하지 마라.

## Availability Awareness

관측일과 실제 이용 가능일을 구분하라.

---

# 9. 개발 방법

TASKS.md 순서대로 작업하라.

각 task는:

```text
Read relevant docs
→ Implement
→ Test
→ Verify
→ Commit
→ Next task
```

로 진행한다.

완료되지 않은 task를 건너뛰지 마라.

---

# 10. 파라미터 미정 상태 처리

문서에 값이 없거나 `null`이면 연구 파라미터다.

절대 임의로:

```text
RSI < 30
VIX > 30
Fear & Greed < 20
```

같은 값으로 확정하지 마라.

개발에 임시값이 반드시 필요할 경우:

```text
RESEARCH_PLACEHOLDER
```

로 명시하고 production/frozen strategy에서 사용할 수 없도록 구분한다.

---

# 11. Strategy vs Research 분리

다음 두 경로를 분리하라.

```text
Research
Historical Data
→ optimization
→ candidate parameters
→ validation
→ OOS
→ freeze

Production
Frozen Strategy
+
Today's Data
→ Current State
→ Alert
```

Production daily runner가 optimizer를 호출해서 전략을 변경하면 안 된다.

---

# 12. Explainability

최종 Dashboard와 Telegram에는 단순히:

```text
Regime = Fear
```

만 표시하지 마라.

가능하면:

```text
Current Regime
Market Score
Target Leverage
Target Allocation
Key Indicators
Previous Regime
Score Change
Reason Codes
```

를 제공한다.

---

# 13. Idempotency

daily runner를 같은 날짜에 여러 번 실행해도:

```text
duplicate state
duplicate event
duplicate alert
```

가 발생하지 않아야 한다.

---

# 14. Data Failure

다음 상황에서는 정상적인 투자 신호를 생성하지 마라.

```text
mandatory data missing
stale data
corrupt data
date mismatch
indicator calculation failure
```

필요한 경우:

```text
Regime = UNKNOWN
```

으로 처리하고 DATA_FAILURE event를 기록한다.

---

# 15. 현재 개발 단계

아직 Strategy Freeze 전이다.

따라서 지금 목표는:

```text
좋은 투자 전략을 임의로 결정하는 것
```

이 아니다.

목표는:

```text
실험 가능한 구조
+
재현 가능한 백테스트
+
데이터 누수 방지
+
설명 가능한 레짐 시스템
```

을 만드는 것이다.

---

# 16. 구현 품질 기준

코드는:

- Python type hints 사용
- 명확한 domain model
- 작은 단위 함수
- 테스트 가능한 순수 계산 함수
- 적절한 logging
- 예외 상황의 명시적 처리
- configuration과 business logic 분리

를 따른다.

---

# 17. 시작 절차

개발을 시작하면 먼저:

1. 네 개의 핵심 문서를 읽는다.
2. repository 현재 상태를 분석한다.
3. TASKS.md의 TASK-001부터 확인한다.
4. 기존 코드가 있다면 문서와의 차이를 확인한다.
5. 필요한 경우 먼저 문서 수정안을 제시한다.
6. 문서와 코드가 일치한 뒤 구현한다.

---

# 18. 중요한 변경은 문서에 반영

다음 변경이 발생하면 코드만 수정하지 말고 관련 문서도 함께 수정한다.

```text
architecture change
database schema change
backtest rule change
strategy definition change
deployment change
alert behavior change
```

문서와 구현이 불일치하는 상태를 남기지 마라.

---

# 19. 최종 목표

최종 시스템은 다음처럼 동작해야 한다.

```text
미국 시장 종료
      ↓
GitHub Actions 자동 실행
      ↓
최신 데이터 수집
      ↓
지표 업데이트
      ↓
Market Score
      ↓
Regime
      ↓
Target Leverage
      ↓
Target Allocation
      ↓
SQLite 저장
      ↓
Regime 변경?
   ┌──┴──┐
  NO     YES
   │       │
 종료    Telegram Alert
           │
           ▼
       사용자가 판단
```

사용자는 자동매매를 맡기지 않는다.

시스템은 매일 시장을 감시하고, 중요한 변화가 발생했을 때만 사용자가 의사결정을 내릴 수 있도록 정보를 제공한다.

---

# 20. 첫 응답 시 수행할 작업

이 초기 지시를 받은 즉시 코드를 대량 작성하지 마라.

먼저 다음을 수행한다.

```text
1. PRD.md 읽기
2. ARCHITECTURE.md 읽기
3. BACKTEST_SPEC.md 읽기
4. TASKS.md 읽기
5. repository 구조 확인
6. 현재 구현 상태 확인
7. 문서 간 충돌 여부 확인
8. TASK-001부터 실행할 구체적 작업과 완료 기준을 요약
```

그 다음 단계별로 구현한다.

**문서에 없는 투자전략을 임의로 발명하지 마라.**
