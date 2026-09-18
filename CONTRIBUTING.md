# CONTRIBUTING.md

# 이 저장소에서 일할 때 지키는 것

이 프로젝트는 Nasdaq-100 시장 레짐을 매일 분석해 QQQ / QLD / TQQQ / Cash의 목표
비중을 제시하고, 중요한 레짐 변화가 생기면 Telegram 알림을 보내는 **투자
의사결정 보조 시스템**이다.

자동매매 시스템이 아니다.

아래는 그 시스템이 지켜야 하는 규칙이다. 대부분은 한 번 어겼다가 손해를 보고
규칙이 된 것이고, 경위는 `docs/strategy.md`에 있다.

> **절 번호는 함부로 바꾸지 마라.** 코드 주석과 다른 문서가 이 문서의 절을 번호로
> 인용한다 (`grep -r "CONTRIBUTING.md" src/`). 절을 지우더라도 뒤 번호를 당기지
> 말고, 새 규칙은 뒤에 붙여라.

---

# 1. 반드시 먼저 읽어야 하는 문서

```text
PRD.md              무엇을 만들기로 했고 무엇을 만들지 않기로 했는가
ARCHITECTURE.md     코드가 어떤 층으로 나뉘고 어디에 무엇이 있는가
BACKTEST_SPEC.md    전략을 어떻게 측정하고 검증하는가
docs/strategy.md    왜 이 숫자인가 — 시도했다 기각한 것과 그 이유까지
docs/operations.md  매일 무엇이 돌고, 실패하면 무엇을 손보는가
TASKS.md            무엇을 어떤 순서로 만들었는가 (완료된 작업 원장)
```

전략을 건드릴 일이 있으면 **`docs/strategy.md`가 가장 중요한 문서다.** 성과보다
그 성과가 어떻게 나왔는지가 더 중요하다.

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

---

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

작업은 TASKS.md 의 TASK 단위로 진행한다. TASKS.md 의 계획된 작업은 전부 끝났고
(§15), 새 작업은 **새 TASK 번호를 받아 그 문서 끝에 추가한다** — 완료 조건과 함께.
작업 단위가 문서에 남지 않으면 나중에 무엇을 근거로 무엇을 바꿨는지 되짚을 수 없다.

각 task는:

```text
Read relevant docs
→ Implement
→ Test
→ Verify
→ Commit
```

로 진행한다.

완료 조건을 충족하지 못한 task 는 다음 단계로 넘어가지 않는다.

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

**전략은 `v1.0-frozen` 으로 고정됐다 (2026-09-13).** 순서를 지켰다 —
탐색(1999–2015) → 검증(2015–2021) → **고정** → 최종(2021–2026). 증거는
`config/frozen/v1.0-frozen.{manifest,regression,indicators,oos}` 에 있다.

고정 전의 목표는 "좋은 투자 전략을 임의로 결정하는 것"이 아니라 실험 가능한 구조 ·
재현 가능한 백테스트 · 데이터 누수 방지 · 설명 가능한 레짐 시스템을 만드는 것이었다.
고정 후에도 §7 과 §10 은 그대로다. 달라지는 것은 하나뿐이다:

```text
파라미터를 바꾸려면 새 freeze 가 필요하다.
```

`config/strategy.yaml` 과 `config/frozen/` 은 **손으로 고치지 않는다.** manifest 가
그 숫자들에 대한 증거이고, 편집하면 증거가 무효가 된다. 바꾸려면 탐색을 다시 하고
새 버전을 고정해야 한다 (TASK-100).

그리고 **깨끗한 검증 구간이 남아 있지 않다.** 탐색 · 검증 · 최종 구간을 전부 썼다
(`docs/strategy.md` §2.8). 지금 숫자를 고치면 그것을 시험할 창이 없다. 새 데이터가
쌓이기를 기다려야 한다.

고정 이후의 측정은 전부 **사후 측정**이다 — 재고 기록하되 설정을 바꾸지 않는다
(`docs/research-backlog.md`).

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

# 17. 작업 절차

1. §1 의 문서 중 손댈 영역에 해당하는 것을 읽는다.
2. 문서와 코드가 어긋나 있으면 **먼저 그것을 말한다.** 어긋난 채로 위에 쌓지 않는다.
3. 구현하고 테스트를 붙인다.
4. 전체를 돌린다 — 이것이 통과해야 끝난 것이다:

   ```bash
   ruff check src tests scripts app
   mypy
   pytest -m "not network"
   ```

5. 바뀐 것이 §18 의 목록에 해당하면 문서도 같이 고친다.
6. 커밋한다.

대시보드만 고쳤더라도 `-m "not network"` 전체를 돌린다. 일일 워커는
`-m "not network and not dashboard"` 로 돌지만(§5), 그것은 **표현 계층의 버그가
시장 감시를 멈추지 못하게** 하려는 것이지 대시보드를 덜 검사하겠다는 뜻이 아니다.

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

**문서에 없는 투자전략을 임의로 발명하지 마라.**
