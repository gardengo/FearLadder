# BACKTEST_SPEC.md

# NASDAQ Leverage Regime Monitor — Backtest Specification

## 1. 목적

시장 레짐에 따라 QQQ / QLD / TQQQ / Cash의 레버리지를 조절하는 전략이 QLD Buy & Hold보다 우수한 위험조정 성과를 제공하는지 검증한다.

---

# 2. 핵심 가설

```text
Regime-based leverage adjustment
>
Static QLD exposure
```

라는 가설을 검증한다.

단, CAGR 하나만 비교하지 않는다.

---

# 3. Benchmark

동일 조건으로:

```text
QQQ 100%
QLD 100%
QLD 70% + Cash 30%
```

을 비교한다.

주요 전략:

```text
QQQ / QLD / Cash Regime
QQQ / QLD / TQQQ / Cash Regime
```

---

# 4. Data Timing

모든 feature에는 반드시:

```text
observation_date
availability_datetime
```

가 있다.

전략은 실제 의사결정 시점에서 이미 이용 가능한 데이터만 사용할 수 있다.

---

# 5. Signal Timing

기본:

```text
t일 시장 종료
→ t일 데이터 확정
→ t일 지표 계산
→ t일 Regime 결정
→ t+1 거래일 실행
```

동일 t일 종가 체결을 기본 백테스트 결과로 사용하지 않는다.

---

# 5.5 Data Provenance Policy

백테스트에 사용한 각 가격/지수/심리지표 시계열은 데이터 수집 라이브러리와 실제 데이터 원천을 구분하여 기록한다.

예:

```text
provider_library = FinanceDataReader
provider_version = x.y.z
underlying_source = Yahoo/FRED/...
```

핵심 ETF:

```text
FDR series
vs
ProShares reference
```

간의 날짜, split, 가격 레벨 등을 교차검증한다.

불일치가 발생하면 자동으로 정상 데이터로 간주하지 않고:

```text
quality_status = REVIEW
```

로 분류한다.

# 6. Historical Data

필수:

```text
QQQ
QLD
TQQQ
VIX
CNN Fear & Greed
RSI용 QQQ 가격
```

후보:

```text
AAII
Breadth
VIX Term Structure
```

각 데이터는 실제 역사적 availability를 고려해야 한다.

---

# 7. Missing Data

기본 정책:

- mandatory data missing → 해당일 정상 signal 생성 금지
- optional data missing → 명시된 fallback 또는 indicator 제외
- 데이터 품질 문제는 log에 기록

미래 데이터를 이용하여 결측치를 보간해서는 안 된다.

---

# 8. Indicator Calculation

모든 rolling indicator는 과거 window만 사용한다.

금지:

```text
centered rolling
future fill
full sample statistic
```

허용:

```text
trailing rolling window
past-only percentile
expanding statistic using data available by t
```

---

# 9. Normalization

후보 방식:

```text
fixed mapping
rolling percentile
rolling z-score
bounded transformation
```

전체 데이터의 평균/표준편차/분위수를 계산한 뒤 과거 전체에 적용하는 방식은 금지한다.

---

# 10. Market Score

```text
Score_t = Σ(weight_i × normalized_indicator_i,t)
```

제약:

```text
weight_i >= 0
Σweight = 1
Score ∈ [0,100]
```

방향:

```text
낮은 Score → Fear
높은 Score → Greed
```

---

# 11. Weight Optimization

백테스트 성능만 보고 weight를 선택하지 않는다.

후보 목적함수는:

```text
CAGR
Sharpe
Sortino
Calmar
MDD
Turnover
Recovery Time
```

을 종합한다.

가중치 최적화 결과는 반드시 별도의 parameter version으로 저장한다.

---

# 12. Regime Count

후보:

```text
3
5
7
9
```

단계가 많을수록 무조건 유리하다고 가정하지 않는다.

평가:

- performance
- MDD
- transitions
- robustness
- explainability
- parameter sensitivity

---

# 13. Allocation Optimization

각 regime에서:

```text
QQQ
QLD
TQQQ
Cash
```

의 비중을 결정한다.

제약:

```text
weight >= 0
sum(weight) = 1
```

전략 철학상:

```text
extreme overheat → market exposure remains > 0
```

라는 제약을 둘 수 있다.

---

# 14. Target Leverage

```text
L = 1*w_QQQ + 2*w_QLD + 3*w_TQQQ
```

단순 ETF 목표배수의 합성 노출을 나타내는 연구용 지표로 사용한다.

이는 실제 장기 실현수익률이 정확히 이 값의 배수라는 의미가 아니다.

---

# 15. TQQQ Research

TQQQ 사용은 별도 실험으로 검증한다.

분리해야 하는 신호:

```text
Extreme Fear
```

와

```text
Bottom Candidate
```

Bottom Candidate에는:

- drawdown
- fear
- volatility
- RSI
- breadth
- stabilization/reversal

을 함께 사용한다.

TQQQ 100%는 전체 전략에서 가장 엄격한 regime/action 후보로 취급한다.

---

# 16. Rebalance

MVP 기본:

```text
Regime Change 시 target allocation 반영
```

Regime이 바뀌지 않은 날에는 기본적으로 재매매하지 않는다.

---

# 17. Transaction Cost

모든 전략에 동일하게 적용한다.

모델:

```text
commission
spread
slippage
```

비용 0 결과와 비용 포함 결과를 모두 보관한다.

---

# 18. Dataset Split

최소:

```text
Research
Validation
Out-of-Sample
```

으로 나눈다.

운영 전략 확정 후 OOS 데이터로 규칙을 수정하면 안 된다.

---

# 19. Walk-Forward

```text
Train
→ Validate
→ Freeze
→ Test
→ Roll Forward
```

각 window에서 future data 접근을 금지한다.

---

# 20. Optimization Leakage Prevention

다음은 test/OOS 구간에서 금지:

- weight 수정
- threshold 수정
- regime count 변경
- indicator 추가/삭제
- allocation 수정
- confirmation 수정

OOS 결과를 보고 수정하면 해당 기간은 더 이상 순수 OOS가 아니다.

---

# 21. Robustness

단일 최적점보다 안정적인 parameter region을 선호한다.

예:

```text
RSI threshold
25 / 27 / 29 / 31 / 33
```

주변값에서 성능이 모두 합리적인지 확인한다.

---

# 22. Performance Metrics

필수:

```text
CAGR
Total Return
Volatility
MDD
Sharpe
Sortino
Calmar
Worst Year
Recovery Time
Time Under Water
Turnover
Regime Change Count
```

---

# 23. Stress Periods

최소:

```text
2008 Financial Crisis
2020 COVID Crash
2022 Bear Market
강세장
횡보장
급락 후 급반등
```

을 개별 분석한다.

---

# 24. Leakage Test Suite

자동 테스트:

1. t일 signal에 t+1 price 사용 여부
2. future rolling value 사용 여부
3. full-sample normalization 여부
4. OOS data에 대한 optimization 접근 여부
5. same-day execution 여부
6. availability 이전 sentiment 사용 여부
7. future corporate action 정보의 부적절한 사용 여부

---

# 25. Reproducibility

각 run에는:

```text
strategy_version
data_version
parameter_version
code_commit
start_date
end_date
execution_rule
cost_model
run_timestamp
```

을 저장한다.

---

# 26. Backtest Artifacts

```text
reports/backtest/
├── summary.json
├── metrics.json
├── daily_portfolio.csv
├── daily_regime.csv
├── daily_indicators.csv
├── trades.csv
├── parameters.json
└── charts/
```

---

# 27. Strategy Freeze

다음을 모두 확정한 경우 frozen version을 생성한다.

```text
indicator set
normalization
weights
regime count
boundaries
allocation
TQQQ rules
confirmation
hysteresis
execution
cost model
```

Freeze 이후 운영 worker는 frozen version만 사용한다.

---

# 28. Live Data와 Research Data 분리

Live daily runner가 연구용 최적화 코드를 호출해서 parameter를 변경해서는 안 된다.

운영:

```text
Frozen Parameters
+
Today's Data
→
Today's State
```

연구:

```text
Historical Data
+
Candidate Parameters
→
Backtest
```

두 경로를 코드 수준에서도 분리한다.
