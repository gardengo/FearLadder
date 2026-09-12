# Strategy Specification

TASK-171. 이 문서는 **현재 확정된 것과 확정되지 않은 것**을 구분해서 기록한다.

> **현재 상태: 탐색 완료, freeze 전 (`v0.4-searched`)**
>
> `config/strategy.yaml` 은 여전히 전부 `null` 이고 운영은 시작되지 않았다.
> 탐색 결과는 `config/research/candidate.strategy.yaml` 에 있다.
> **OOS 구간은 아직 쓰지 않았다** — 마지막 시험이 남아 있다는 뜻이다.
> 아래 §2.5 에 탐색 결과와 각 파라미터의 지지도를 기록했다.

---

## 1. 확정된 것 (구조)

이 항목들은 문서에 정의되어 있거나 정의상 고정된 것으로, 연구 대상이 아니다.

### 1.1 자산과 레버리지

| Sleeve | 일일 목표 배수 |
| --- | --- |
| QQQ | 1x |
| QLD | 2x |
| TQQQ | 3x |
| Cash | 0x (금리 미반영) |

`Target Leverage = 1·w_QQQ + 2·w_QLD + 3·w_TQQQ`

이는 **합성 노출을 서술하는 연구용 지표**이며, 장기 실현수익률이 이 값의 배수라는
뜻이 아니다 (`BACKTEST_SPEC.md` §14).

### 1.2 점수 축

```text
0   = 극단적 공포 / Risk-Off / 레버리지 확대 후보
100 = 극단적 탐욕 / Risk-On  / 레버리지 축소 후보
```

`HIGHER_IS_FEAR` 로 선언된 지표(VIX, 낙폭 깊이)는 정규화 마지막 단계에서 반전되어,
가중합 이전에 모든 지표가 같은 언어를 쓴다.

### 1.3 지표 집합과 길이

길이는 PRD §6 에 명시된 값이다.

| 계열 | 지표 |
| --- | --- |
| RSI | RSI(14), RSI(30) — Wilder 평활 |
| Trend | 20/50/200DMA 대비 거리, 50/200 비율, 200DMA 21일 기울기 |
| Momentum | 1M(21) / 3M(63) / 6M(126) / 12M(252) 수익률 |
| Drawdown | current(expanding) / 30D / 90D / 52W — 양수 depth |
| Volatility | VIX level, VIX 5일 변화, VIX 자체 분포 내 백분위 |
| Sentiment | CNN Fear & Greed, AAII bull-bear spread |
| Breadth | **제외** (아래 §3) |

### 1.4 실행 규칙

```text
t일 종가 → t일 지표·점수·레짐 확정 → t+1 거래일 실행
```

`NEXT_OPEN` 이 기본값이다. **당일 종가 체결은 설정으로도 표현할 수 없다**
(`BACKTEST_SPEC.md` §5).

### 1.5 리밸런스

레짐이 바뀔 때만 목표 배분을 반영한다. 그 사이 비중은 시장에 따라 드리프트한다
(`BACKTEST_SPEC.md` §16).

### 1.6 제약

- 비중 ≥ 0, 합 = 1
- **과열 구간에서도 시장 노출이 0 이 되지 않는다** (`PRD.md` §2.1)
- 공포 강도만으로 TQQQ 를 편입하지 않는다 (§1.7)

### 1.7 TQQQ 게이트 구조

`PRD.md` §10 이 요구하는 분리:

```text
Fear Intensity          (레짐이 답한다)
        vs
Bottom Confirmation     (게이트가 답한다)
```

게이트는 다음을 **모두** 요구한다.

1. `required_rules` 전원 통과
2. `candidate_rules` 중 `min_confirmations` 개 이상 통과

**지표를 읽을 수 없는 규칙은 통과가 아니라 실패로 센다.** 증거가 없는 상태에서
3배 레버리지를 드는 것이 이 시스템이 낼 수 있는 최악의 결과이기 때문이다.

게이트가 막으면 TQQQ 비중은 레버리지 사다리 한 칸 아래(QLD)로 이동하고,
그 사실이 reason code 로 남는다.

후보 규칙 (임계값은 미확정):
`deep_drawdown`, `extreme_fear`, `high_vix`, `oversold_rsi`,
`volatility_spike`, `price_stabilization`, `reversal_evidence`,
`breadth_capitulation`

### 1.8 추세 필터 — 하락 중에는 레버리지를 잠근다

`PRD.md` §2.1 은 공포가 깊어질수록 레버리지를 올리라고 한다. 이 규칙만으로는
장기 하락에서 파멸한다. 1999–2015 실측:

- 2000-03 고점: 레짐 Overheated, 레버리지 **1.02x** — 정확히 축소했다
- 2000-09 (−30%): Fear, 1.92x
- 2001-09 (−70%): Capitulation, **2.80x** — 하락 도중 확대
- 결과: 닷컴 구간 −96.9% (QQQ −77.2%)

**공포는 바닥이 아니다.** 공포 지표로 만든 점수는 "떨어지는 중"과 "다 떨어졌다"를
구분할 수 없다. 둘 다 똑같이 무섭게 보이기 때문이다.

그래서 조건을 하나 더 건다: 레버리지 상한 위의 구간은 **장기 추세가 살아 있는
동안에만** 열린다.

```text
price_vs_200dma >= threshold   →  사다리 그대로
price_vs_200dma <  threshold   →  목표 레버리지를 max_leverage_below 로 제한
```

같은 구간(research) 실측 효과:

| | CAGR | MDD | 닷컴 | 금융위기 |
| --- | --- | --- | --- | --- |
| 필터 없음 | −2.74% | 98.4% | −96.9% | −68.8% |
| **필터 적용** | **+5.89%** | **88.2%** | **−83.6%** | **−48.5%** |
| QQQ 보유 | +5.30% | 83.0% | −77.2% | −41.7% |
| QLD 보유 | −1.49% | 99.0% | −97.8% | −75.4% |

구조는 확정이고 수치(지표·임계값·상한)는 연구 대상이다.

두 가지 설계 결정:

- **지표를 읽을 수 없으면 추세가 꺾인 것으로 본다.** 실패 시 열어두면 이
  필터가 존재하는 이유가 사라진다.
- **TQQQ 게이트보다 먼저 실행된다.** 레버리지를 제한하면 TQQQ 가 사라지므로,
  들고 있지도 않은 sleeve 를 게이트에 물을 이유가 없다.
- **과열 최소 노출 하한은 방어적 축소에 적용하지 않는다.** `PRD.md` §2.1 의
  하한은 '과열되었다는 이유로' 이탈하지 말라는 것이지, 하락 추세에서의 축소를
  막는 규정이 아니다.

---

### 1.9 실패 안전

필수 데이터가 결측·낡음·미래일자·손상이면:

```text
Regime = UNKNOWN
composite_score = None
target_allocation = 없음
target_leverage = None
→ DATA_FAILURE 알림
```

해당 날짜에 이미 저장돼 있던 배분은 삭제된다. 나쁜 데이터가 어제의 조언을
그대로 세워두지 않는다.

### 1.10 전이 로직 구조

세 개의 독립적인 제동 장치 (`TASK-051`):

| 장치 | 막는 것 |
| --- | --- |
| `confirmation_days` | 하루짜리 스파이크 |
| `hysteresis` | 경계 위에서 떠는 점수의 진동 |
| `minimum_duration_days` | 과도한 회전율 |

UNKNOWN 입력일은 확정 레짐을 유지하되 확인 카운트를 전진시키지도 취소하지도
않는다.

---

## 2. 확정되지 않은 것 (연구 대상)

| 파라미터 | 위치 | 결정 방법 |
| --- | --- | --- |
| 지표 가중치 | `score.weights` | TASK-080, TASK-081 |
| 정규화 window | `indicators.*.normalization.window` | TASK-041, TASK-083 |
| VIX 백분위 lookback | `indicators.vix_percentile.params.window` | TASK-083 |
| 레짐 단계 수 (3/5/7/9) | `regime.count` | TASK-082 |
| 레짐 라벨과 경계 | `regime.labels`, `regime.boundaries` | TASK-082, TASK-083 |
| confirmation / hysteresis / min duration | `transition.*` | TASK-085 |
| 레짐별 목표 배분 | `allocation.mappings` | TASK-084 |
| 최소 시장 노출, 최대 레버리지 | `allocation.constraints.*` | TASK-084 |
| TQQQ 게이트 규칙과 임계값 | `tqqq_gate.*` | TASK-063, TASK-083 |
| 추세 필터 지표·임계값·상한 | `trend_filter.*` | TASK-083 |
| 거래비용 | `cost_model.*` | TASK-072 |
| 데이터셋 분할 경계 | `dataset_split.*` | TASK-090 |

**이 값들을 손으로 채우지 않는다.** `CLAUDE_CODE_INITIAL_PROMPT.md` §7, §10, §15.

---

## 2.5 탐색 결과 (2026-09-12, `v0.4-searched`)

research 구간(1999-01-01 … 2015-08-14)만 읽어 좌표하강으로 탐색했다.
재현: `python scripts/optimize.py` · 검증: `python scripts/validate.py`

### 확정된 값과 그 지지도

| 파라미터 | 값 | 지지도 | 근거 |
| --- | --- | --- | --- |
| `trend_filter` | 200일선 −2%, 하락 시 0.0x | **강함** | 단조. 없으면 CAGR −2.74%/MDD 98.4%, 있으면 +9.5%/58.2% |
| `regime.count` | 5 | **강함** | 단계별 주변부: 3 −0.017, 5 **+0.094**, 7 +0.075, 9 −0.125 |
| `score.weights` | trend-heavy | 보통 | fear-heavy 가 최하위. 추세 필터 발견과 방향이 일치 |
| `transition.hysteresis` | 5.0 | 약함 | 내부 최적점. CAGR·MDD 동시 최고 |
| `transition` 확인/최소유지 | 1일 / 75일 | **없음** | 비단조. 민감도가 SPIKE 판정(이웃 격차 +90%) |
| `regime.boundaries` | [25, 40, 60, 75] | **없음** | 확장계수별 목적값이 톱니 |

### 왜 지지도를 함께 적었는가

한 16.6년 구간에서 후보 목적값은 −0.10 … +0.31 로 퍼지는데, 같은 후보들의
**이웃 평균은 +0.04 … +0.09** 범위에 몰린다. 봉우리 높이가 실제 신호의 약
10배다. 실제로 그리드를 넓힐 때마다 더 높은 봉우리가 나타났다.

그래서 지지도가 없는 차원은 봉우리를 쫓지 않고 구조적 근거로 정했다.
예를 들어 최소유지일은 성과 신호가 없지만 회전율이 단조 감소하므로 긴 쪽을,
경계는 균등에서 데이터가 약하게 가리키는 방향으로만 조금 이동한 둥근 값을
택했다.

### 성과 (research + validation 합산 22.2년, OOS 미사용)

| | CAGR | 변동성 | MDD | Sharpe | Calmar | 최악의 해 | 최종 배수 |
| --- | --- | --- | --- | --- | --- | --- | --- |
| **전략** | **11.10%** | **26.0%** | **58.2%** | **0.54** | **0.19** | **−28.3%** | **10.3x** |
| QQQ 보유 | 8.97% | 28.0% | 83.0% | 0.45 | 0.11 | −41.9% | 6.7x |
| QLD 보유 | 7.14% | 55.9% | 99.0% | 0.40 | 0.07 | −72.9% | 4.6x |
| QLD 70/현금 30 | 7.84% | 38.3% | 94.1% | 0.39 | 0.08 | −57.2% | 5.3x |

평균 레버리지 1.11x, 현금 100% 보유 21.8%.

### 구간별 성격

| 구간 | 전략 | QQQ | QLD |
| --- | --- | --- | --- |
| research 1999–2015 (폭락 2회) | **+9.52%** | +5.30% | −1.49% |
| validation 2015–2021 (강세장) | +15.69% | +20.62% | **+37.51%** |

**폭락이 있으면 이기고, 없으면 진다.** 강세장 열위는 폭락 방어의 대가이고,
전체 주기에서는 회수된다. 다만 강세장이 길게 이어지는 동안에는 벤치마크에
뒤처지는 구간을 감수해야 한다.

### Walk-forward (5 fold, train 6년 → test 2년)

이어붙인 out-of-sample: **CAGR 9.83%, MDD 41.0%, Sharpe 0.53, Calmar 0.24**
평균 overfit gap **−2.03%** (음수 = test 가 train 보다 나았다).

fold 별 test CAGR 은 −6.2% … +38.9% 로 분산이 매우 크다. 레버리지 레짐
전략의 정직한 모습이다.

**fold 마다 서로 다른 전이 파라미터를 골랐다** (d 는 20/45/45/60/60,
어느 fold 도 75 를 고르지 않았다). 이 차원이 안정적으로 결정되지 않는다는
독립적인 확인이며, 위에서 '지지도 없음' 으로 기록한 판단을 뒷받침한다.

### RESEARCH_PLACEHOLDER 프로파일

개발·테스트를 위해 `config/research/placeholder.strategy.yaml` 에만 임시값이 있다.
그 값들은 의도적으로 균일하고 둥근 숫자다 — 모든 지표 동일 가중치, 경계는
20/40/60/80 균등 배치. 연구 결과로 오인될 수 없게 만든 것이다.

`ensure_production_ready()` 가 이 프로파일을 운영 경로에서 거부하고,
`freeze()` 는 이 프로파일을 freeze 하는 것 자체를 거부한다.

---

## 3. 명시적으로 제외한 것

### Breadth

Nasdaq-100 breadth 에는 각 과거 시점의 구성종목(point-in-time constituents)이
필요하다. 이 데이터가 없고, **현재 구성종목으로 과거 breadth 를 재구성하는 것은
금지되어 있다** (`PRD.md` §6.5).

이유: 지수에서 퇴출된 종목이 전부 빠지므로, breadth 가 측정하려는 바로 그
폭락 구간에서 생존편향이 들어간다.

따라서 `BREADTH_NDX` 소스와 `breadth_pct_above_200dma` 지표는
**사유를 명시한 채 비활성화**되어 있고, `UnavailableBreadthProvider` 와
`pct_above_ma()` 는 조용한 오답 대신 예외를 던진다.

point-in-time 구성종목이 확보되면 `ConstituentSource.members_on()` 구현만
추가하면 된다.

### 현금 금리

Cash 수익률은 0 이다. 금리 가정은 문서에 없고, 과열 구간에서 현금을 드는
이 전략에서는 0 이 보수적인 방향이다.

### 자동매매

증권사 API, 주문 전송, 자동 리밸런싱은 구현하지 않는다 (`PRD.md` §5.2).

---

## 4. Freeze 절차 (TASK-100)

1. `scripts/backtest.py` 와 `regime_monitor.research` 의 탐색으로 후보 도출
   (research 구간에서만 — `SplitGuard` 가 강제)
2. validation 구간 검증
3. walk-forward (`WalkForward`) — train → freeze → test
4. 민감도 분석 (`sensitivity.analyse`) — 최적점이 plateau 인지 spike 인지
5. 선택한 수치를 후보 strategy yaml 로 작성
6. `python scripts/freeze.py --candidate ... --version v1.0-frozen ...`
   (`--apply` 없이는 dry run)

freeze 는 다음을 거부한다:

- RESEARCH_PLACEHOLDER 프로파일
- `null` 이 하나라도 남은 파라미터 세트
- `-frozen` 으로 끝나지 않는 버전명

성공하면:

```text
config/strategy.yaml                       ← FROZEN, 손으로 고치지 말 것
config/frozen/v1.0-frozen.manifest.json    ← 파라미터 + 근거 + fingerprint
config/frozen/v1.0-frozen.regression.json  ← TASK-102 회귀 기준
```

`config/strategy.yaml` 과 `config/frozen/` 은 반드시 같이 커밋한다.
manifest 가 바로 그 숫자들에 대한 근거이기 때문이다.

### Freeze 이후

OOS 결과를 보고 파라미터를 수정하면 그 구간은 더 이상 OOS 가 아니다
(`BACKTEST_SPEC.md` §20). 전략을 바꾸려면 연구를 다시 하고 **새 버전으로**
freeze 한다.
