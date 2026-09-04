# PRD.md

# NASDAQ Leverage Regime Monitor

## 1. 제품 개요

### 1.1 프로젝트명

NASDAQ Leverage Regime Monitor

### 1.2 목적

Nasdaq-100 시장의 가격, 추세, 변동성, 투자심리, 시장 breadth 등을 매일 수집하고 종합하여 현재 시장 레짐을 판단한다.

현재 레짐에 따라 QQQ / QLD / TQQQ / Cash의 목표 비중과 Target Leverage를 계산하고, **레짐 또는 중요 상태가 변경될 때 사용자에게 알림**을 제공한다.

본 시스템은 **자동매매 시스템이 아니다.**

사용자는 시스템의 분석 결과를 참고하여 실제 매매 여부와 주문을 직접 결정한다.

---

# 2. 투자 철학

## 2.1 금융자산 10억 원 이전

공격적인 자산 증식을 목표로 한다.

기본 포지션은:

```text
QLD 70%
Cash 30%
```

시장 상황이 하락/공포 방향으로 이동하면 레버리지를 단계적으로 증가시킨다.

예:

```text
QLD 70 / Cash 30
→ QLD 80 / Cash 20
→ QLD 90 / Cash 10
→ QLD 100
→ TQQQ 편입
→ TQQQ 비중 확대
```

극단적인 공포와 바닥 형성 가능성이 확인되는 구간에서 TQQQ 비중을 가장 높인다.

시장 과열 시에는 레버리지를 축소한다.

예:

```text
QLD 감소
QQQ 증가
Cash 증가
```

아무리 과열되었다고 판단하더라도 시장을 완전히 이탈하지 않는다.

---

## 2.2 금융자산 10억 원 이후

보다 보수적인 자산보존 단계로 전환한다.

주요 자산:

```text
QQQ
QLD
SCHD
```

10억 원 이후의 최종 포트폴리오와 전환 규칙은 별도 프로젝트 범위로 둔다.

---

# 3. 제품 목표

시스템은 매일 자동으로 다음 질문에 답해야 한다.

1. 현재 Nasdaq 시장은 어떤 상태인가?
2. 현재 시장의 공포/탐욕 및 위험 수준은 어느 정도인가?
3. 현재 목표 레버리지는 어느 정도인가?
4. QQQ / QLD / TQQQ / Cash를 어떻게 배분하는 것이 현재 전략에 맞는가?
5. 어제 대비 중요한 레짐 변화가 있었는가?

---

# 4. 최종 사용자 경험

평상시:

```text
매일 자동 실행
→ 최신 데이터 수집
→ 지표 계산
→ Market Score
→ Regime
→ Target Allocation
→ DB 저장
```

레짐 변화가 발생한 날:

```text
Regime Change 감지
→ Alert 생성
→ Telegram 알림
→ 사용자가 Dashboard 확인
→ 사용자가 최종 매매 결정
```

사용자는 PC를 켜두거나 Streamlit을 열어둘 필요가 없어야 한다.

---

# 5. 시스템 범위

## 5.1 포함

- 일일 시장 데이터 수집
- 가격 데이터
- CNN Fear & Greed
- RSI
- VIX
- Drawdown
- 이동평균
- Momentum
- AAII Sentiment
- Breadth 후보
- 종합 Market Score
- Market Regime
- Target Allocation
- Target Leverage
- Regime Change Detection
- SQLite 저장
- GitHub Actions daily execution
- Streamlit Dashboard
- Telegram Alert
- Historical Backtest
- OOS / Walk-Forward validation
- Strategy Freeze
- Docker 지원

## 5.2 제외

- 자동매매
- 증권사 주문 API
- 실시간 주문
- 실시간 초단기 트레이딩
- 자동 리밸런싱
- 투자 판단을 대신하는 LLM
- 10억 이후 최종 자산배분 최적화
- 옵션/선물 전략

---

# 6. 핵심 지표 후보

### Sentiment

- CNN Fear & Greed
- AAII Sentiment

### Volatility

- VIX
- VIX percentile
- VIX change
- VIX term structure

### Price / Momentum

- RSI(14)
- RSI(30)
- 1M / 3M / 6M / 12M return

### Trend

- 20DMA
- 50DMA
- 200DMA
- 50DMA/200DMA
- 200DMA slope

### Drawdown

- current drawdown
- 30D
- 90D
- 52W

### Breadth

- % above 50DMA
- % above 200DMA
- Advance/Decline
- New High/New Low

최종 지표 구성은 백테스트에서 결정한다.

---

# 6.5 데이터 소스 및 수집 정책

### 가격 데이터 기본 수집 라이브러리

미국 상장 ETF와 지수 가격의 기본 수집 인터페이스는 **FinanceDataReader(FDR)** 를 사용한다.

FDR은 미국 개별주/ETF와 주요 지수의 가격 데이터를 하나의 Python 인터페이스로 수집할 수 있고, 현재 README에서도 미국 시장 종목 가격, NASDAQ/NYSE/AMEX listing, 주요 지수 및 FRED 데이터 경로를 지원한다. 프로젝트에서는 특정 외부 공급자에 직접 결합하지 않고 FDR adapter를 통해 접근한다. citeturn745714view0turn548244view0

대상:

```text
QQQ
QLD
TQQQ
```

### 데이터 품질 검증

FinanceDataReader는 통합 수집 인터페이스이지 원자료를 직접 생산하는 시장 데이터 공급자 자체가 아니다.

따라서 핵심 레버리지 ETF 가격은:

```text
FinanceDataReader
+
ProShares 공식 자료
```

를 이용해 교차검증한다.

ProShares의 공식 데이터/상품 정보는 QLD와 TQQQ의 상품 구조 및 과거 자료 검증에 사용한다.

### NASDAQ 지수 / FRED

FDR에서 FRED prefix를 이용한 시계열 접근이 가능하므로 FRED 데이터는 FDR 또는 FRED 직접 adapter 중 하나를 선택할 수 있도록 추상화한다.

운영 source는 실제 데이터 품질과 안정성을 검증한 후 고정한다.

### VIX

VIX는:

```text
Cboe official historical data
+
FRED VIXCLS
```

를 검증 기준으로 사용한다.

FDR에서 VIX 시계열을 읽는 것도 가능하지만, 최종 백테스트 기준 시계열은 source provenance를 명확히 기록해야 한다.

### CNN Fear & Greed

CNN Fear & Greed는 역사적 공식 다운로드 경로의 안정성이 제한적이므로:

```text
Live:
CNN official source

Historical:
reconstructed secondary dataset
```

로 구분한다.

Historical dataset에는 `source`, `quality_status`, `availability_datetime`을 반드시 저장한다.

### AAII

AAII 공식 weekly sentiment를 사용한다.

주간 데이터이므로 발표/공개된 이후에만 백테스트에서 사용한다.

### Breadth

Historical constituent survivorship 문제를 고려한다.

현재 구성종목으로 과거 breadth를 재구성하여 장기 백테스트에 사용하는 것은 금지한다.

역사적 구성종목 자료가 확보되기 전까지 Breadth는 보조 지표 또는 연구 후보로 유지한다.

### 핵심 원칙

**수집 라이브러리와 데이터 원천을 동일한 개념으로 취급하지 않는다.**

예:

```text
Collection Interface
        ↓
FinanceDataReader
        ↓
Underlying Source
        ↓
Raw Snapshot
        ↓
Validation / Provenance
```

FDR의 버전과 사용한 underlying source를 모두 기록한다.

# 7. Market Score

모든 지표를 공통된 0~100 score로 정규화한다.

개념:

```text
0 = 극단적 공포 / Risk-Off / 공격적 레버리지 후보
100 = 극단적 탐욕 / Risk-On / 레버리지 축소 후보
```

종합:

```text
Market Score
= Σ(Indicator Score × Weight)
```

가중치와 threshold는 백테스트로 결정한다.

---

# 8. Market Regime

후보:

```text
3단계
5단계
7단계
9단계
```

예시:

```text
Capitulation
Extreme Fear
Fear
Neutral
Bull
Overheated
Extreme Bubble
```

실제 단계 수와 구간은 백테스트에서 선택한다.

---

# 9. Allocation

지원 자산:

```text
QQQ = 1x
QLD = 2x
TQQQ = 3x
Cash = 0x
```

예:

```text
QQQ 20%
QLD 60%
Cash 20%
```

Target Leverage:

```text
0.20 × 1
+ 0.60 × 2
= 1.40x
```

Regime별 비중은 백테스트를 통해 결정한다.

---

# 10. TQQQ 정책

TQQQ는 가장 공격적인 구간에서 사용한다.

단순한 Extreme Fear만으로 TQQQ 100%를 결정하지 않는다.

다음 두 개념을 분리한다.

```text
Fear Intensity
vs
Bottom Confirmation
```

Bottom Confirmation 후보:

- Deep Drawdown
- Extreme Fear
- High VIX
- Oversold RSI
- Breadth Capitulation
- Volatility Spike
- Price Stabilization
- Reversal Evidence

---

# 11. 자동화 운영

## Daily Worker

GitHub Actions scheduled workflow가 매일 실행한다.

```text
GitHub Actions
→ daily_runner.py
→ Data Collection
→ Validation
→ Indicators
→ Score
→ Regime
→ Allocation
→ DB Update
→ Event Detection
→ Telegram Alert
```

사용자의 로컬 PC는 운영에 필요하지 않다.

---

# 12. 데이터 저장

MVP의 기본 DB는 SQLite다.

```text
data/regime_monitor.db
```

SQLite를 선택한 이유:

- 별도 DB 서버 불필요
- 프로젝트 폴더 내 관리 가능
- GitHub Actions에서 파일로 처리 가능
- 1인 사용자 + 일일 시계열 데이터에 충분
- 로컬 개발과 배포의 차이가 작음

DB 접근은 Repository 인터페이스 뒤에 추상화한다.

향후 PostgreSQL로 교체할 수 있어야 한다.

---

# 13. 데이터 파일 관리

GitHub는 소스코드와 소규모 SQLite 상태 저장소를 함께 관리할 수 있도록 구성한다.

단, SQLite 파일이 장기적으로 커져 Git 관리에 부적합해질 경우:

```text
SQLite
→ snapshots / Parquet
→ object storage 또는 managed DB
```

로 이전할 수 있도록 Repository 계층을 분리한다.

---

# 14. Dashboard

Streamlit Cloud에서 제공한다.

페이지:

- Current Regime
- Market Score
- Target Leverage
- Target Allocation
- Indicator Dashboard
- Historical Regime
- Backtest Results
- Event History

Streamlit은 계산의 source of truth가 아니라 저장된 상태를 조회하고 시각화하는 역할을 주로 담당한다.

---

# 15. Alert

MVP 알림 채널:

**Telegram**

이유:

- Bot API가 단순함
- GitHub Actions에서 호출 가능
- 별도 서버 필요 없음
- 모바일 즉시 확인 가능

Alert Engine은 provider interface로 추상화한다.

```text
NotificationProvider
├── Telegram
└── Future providers
```

---

# 16. 알림 이벤트

- REGIME_CHANGED
- TARGET_LEVERAGE_CHANGED
- EXTREME_FEAR
- EXTREME_BUBBLE
- TQQQ_CANDIDATE
- DATA_FAILURE

알림은 중복 전송되지 않아야 한다.

---

# 17. 성공 기준

## 기능

- 매일 자동 데이터 업데이트
- 지표 계산 성공
- Score 계산 성공
- Regime 계산 성공
- Allocation 계산 성공
- DB 저장 성공
- 변화 감지 성공
- Telegram 알림 성공
- Dashboard 표시 성공

## 검증

- 미래 데이터 누수 없음
- OOS 오염 없음
- 반복 실행 재현성 확보
- 파라미터 민감도 검증
- 다양한 시장 환경에서 robustness 확인

## 운영

- 로컬 PC 상시 실행 불필요
- GitHub Actions로 daily job 자동 실행
- Streamlit Cloud에서 dashboard 접근 가능

---

# 18. MVP 개발 순서

```text
Repository Bootstrap
→ Data Layer
→ Historical Data
→ Indicator Engine
→ Score Engine
→ Regime Engine
→ Allocation Engine
→ Backtest
→ Optimization
→ OOS / Walk-Forward
→ Strategy Freeze
→ Daily Worker
→ SQLite State Management
→ Telegram Alert
→ Streamlit Dashboard
→ Docker / Deployment
```
