# RegimePilot

**NASDAQ Leverage Regime Monitor**

Nasdaq-100 시장의 가격 / 추세 / 변동성 / 투자심리 / breadth 지표를 매일 수집·종합하여
현재 **시장 레짐(Market Regime)** 을 판단하고, QQQ · QLD · TQQQ · Cash 의
**목표 비중과 Target Leverage** 를 계산한다.
레짐 또는 중요 상태가 변경되면 Telegram 으로 알림을 보낸다.

> ⚠️ 본 시스템은 **자동매매 시스템이 아니다.**
> 분석 결과는 참고용이며, 실제 매매 여부와 주문은 사용자가 직접 결정한다.

---

## 현재 상태

| 항목 | 상태 |
| --- | --- |
| 파이프라인 (수집 → 지표 → 점수 → 레짐 → 배분 → 저장 → 알림) | 구현·검증 완료 |
| 백테스트 / 최적화 / walk-forward / 누수 테스트 | 구현·검증 완료 |
| **전략 파라미터** | **미확정 (`parameter_status: RESEARCH`)** |
| 일일 운영 (GitHub Actions) | 전략 freeze 이후 자동 시작 |

`config/strategy.yaml` 의 전략 수치는 전부 `null` 이다. 가중치·경계·배분·임계값은
백테스트와 검증을 거쳐 결정할 대상이며, 임의로 채워 넣지 않는다
(`CLAUDE_CODE_INITIAL_PROMPT.md` §7, §10).

**따라서 daily runner 는 지금 실행하면 종료 코드 2 로 거부한다.** 이는 버그가 아니라
설계된 안전장치다. 개발·검증용으로는 명시적으로 표시된 RESEARCH_PLACEHOLDER
프로파일을 쓴다.

---

## Pipeline

```text
Daily Market Data
    ↓  Indicator Engine
    ↓  Composite Score        0 = 극단적 공포 … 100 = 극단적 탐욕
    ↓  Market Regime          confirmation / hysteresis / minimum duration
    ↓  Target Leverage / Allocation
    ↓  Regime Change Detection
    ↓  SQLite Persistence
    ↓  Telegram Alert
```

Streamlit 은 저장된 결과를 시각화하는 Dashboard 역할만 담당한다.

## Deployment

- **GitHub Actions** — `scripts/daily_runner.py` 일일 실행 (운영 경로)
- **SQLite** — 저장소에 커밋되는 상태 저장소 (`data/regime_monitor.db`)
- **Streamlit Cloud** — 대시보드
- 로컬 PC 는 운영 경로에 포함되지 않는다.

---

## 설치

Python 3.11 이상이 필요하다.

```bash
python -m venv .venv
source .venv/Scripts/activate       # Windows(Git Bash) / macOS·Linux 는 .venv/bin/activate
pip install -e ".[dev,dashboard]"
pytest -m "not network"             # 네트워크 테스트 제외
```

### 실제 소스 접근 확인 (선택)

```bash
pytest -m network                   # FDR / CNN 엔드포인트에 실제로 붙는다
```

---

## 실행

### 일일 워커

```bash
# 운영 (전략이 frozen 된 이후에만 동작)
python scripts/daily_runner.py

# 개발·검증: RESEARCH_PLACEHOLDER 프로파일
REGIME_MONITOR_ALLOW_RESEARCH_PARAMS=1 \
  python scripts/daily_runner.py --profile placeholder --dry-run

# 특정 날짜 재실행 (멱등하므로 몇 번을 돌려도 안전하다)
python scripts/daily_runner.py --date 2024-03-16
```

종료 코드:

| 코드 | 의미 |
| --- | --- |
| 0 | 처리 완료 (DATA_FAILURE 를 기록한 경우 포함) |
| 1 | 실행 중 예외. 정상 상태가 저장되지 않았다 |
| 2 | 전략이 frozen 이 아니고 research 게이트도 열려 있지 않다 |

### 백테스트

```bash
# 데이터 수집 후 전 구간 백테스트 + 리포트 생성
python scripts/backtest.py --profile placeholder --collect --report

# 이미 수집된 데이터로 기간 지정
python scripts/backtest.py --profile placeholder --start 2015-01-01 --end 2024-12-31
```

산출물은 `reports/backtest/` 에 쌓인다 (`BACKTEST_SPEC.md` §26).

### 대시보드

```bash
streamlit run app/streamlit_app.py     # http://localhost:8501
```

### Docker

```bash
docker compose up dashboard                                   # 대시보드
docker compose run --rm daily --profile placeholder --dry-run # 워커
docker compose run --rm backtest                              # 백테스트
```

---

## 설정

| 파일 | 내용 |
| --- | --- |
| `config/indicators.yaml` | 지표 정의. 길이는 문서에 고정된 값, 정규화 window 는 연구 파라미터 |
| `config/strategy.yaml` | **운영 전략.** 현재 전부 `null` |
| `config/alerts.yaml` | 알림 규칙. secret 이 아니라 환경변수 '이름'만 보관 |
| `config/data_sources.yaml` | 데이터 소스와 provenance |
| `config/research/placeholder.*.yaml` | RESEARCH_PLACEHOLDER 프로파일. 운영 경로에서 거부된다 |

### 환경변수

| 변수 | 용도 |
| --- | --- |
| `TELEGRAM_BOT_TOKEN` / `TELEGRAM_CHAT_ID` | 알림 채널 |
| `REGIME_MONITOR_DB` | DB 경로 오버라이드 (테스트·연구용) |
| `REGIME_MONITOR_ALLOW_RESEARCH_PARAMS` | 미확정 파라미터로 실행 허용. **운영에서는 절대 설정하지 않는다** |
| `REGIME_MONITOR_LOG_LEVEL` | 로그 레벨 |

`.env.example` 참고. secret 은 코드·config·DB 어디에도 저장하지 않는다.

### 운영자가 직접 배치해야 하는 파일

일부 소스는 공개 API 가 없어 파일로 받는다. `data/reference/README.md` 참고.

- `data/reference/proshares/{qld,tqqq}.csv` — 교차검증 기준 (TASK-028)
- `data/reference/cnn/fear_greed_history.csv` — CNN 재구성 과거 데이터
- `data/reference/aaii/sentiment.csv` — AAII 주간 설문

없어도 파이프라인은 동작한다. 해당 지표가 선택적으로 제외될 뿐이다.

---

## GitHub Actions

| 워크플로 | 트리거 | 역할 |
| --- | --- | --- |
| `.github/workflows/test.yml` | push / PR | lint + test |
| `.github/workflows/daily_monitor.yml` | 평일 22:30 UTC + 수동 | 일일 워커, DB 커밋 |

`22:30 UTC` 는 EST 17:30 / EDT 18:30 으로, 서머타임 양쪽 모두에서 미국 장 마감
이후다. cron 은 서머타임을 따르지 않으므로 늦은 쪽으로 고정했다.

**필요한 Secrets:** `TELEGRAM_BOT_TOKEN`, `TELEGRAM_CHAT_ID`
(Settings → Secrets and variables → Actions)

전략이 frozen 이 아니면 daily 워크플로는 명시적 notice 를 남기고 중단한다.

---

## 프로젝트 구조

```text
src/regime_monitor/
├── config/        설정 스키마와 로더, production/research 게이트
├── data/          모델 · 리포지토리 · 수집기 · 검증기
├── indicators/    지표 계산 (전부 trailing-only)
├── scoring/       0~100 정규화와 종합 점수
├── regime/        레짐 분류와 전이 로직
├── allocation/    목표 배분과 TQQQ 게이트
├── backtest/      시뮬레이터 · 비용 · 벤치마크 · 성과지표
├── research/      ← 연구 경로. 운영 코드는 여기를 import 하지 않는다
├── alerts/        템플릿 · 중복방지 · Telegram
└── pipeline/      일일 워커와 대시보드 조회
```

`research/` 와 `pipeline/` 의 분리는 규약이 아니라 테스트로 강제된다
(`BACKTEST_SPEC.md` §28).

---

## 문서

| 문서 | 내용 |
| --- | --- |
| [PRD.md](PRD.md) | 무엇을 만드는가 |
| [ARCHITECTURE.md](ARCHITECTURE.md) | 어떻게 구성하는가 |
| [BACKTEST_SPEC.md](BACKTEST_SPEC.md) | 어떻게 검증하는가 |
| [TASKS.md](TASKS.md) | 어떤 순서로 구현하는가 |
| [docs/strategy.md](docs/strategy.md) | 전략 명세와 현재 확정 상태 |
| [docs/operations.md](docs/operations.md) | 운영 가이드 |

문서 충돌 시 우선순위: BACKTEST_SPEC → PRD → ARCHITECTURE → TASKS.
단, 안전성·데이터 누수 규칙이 최우선이다.

---

## 설계상 지켜지는 것들

이 시스템에서 다음은 의도가 아니라 구조로 보장된다.

- **미래 데이터를 볼 수 없다.** 모든 지표·정규화가 trailing-only 이고,
  전체 이력으로 계산한 값과 특정 시점까지만으로 계산한 값이 겹치는 구간에서
  완전히 일치하는지 테스트한다.
- **당일 체결이 표현 불가능하다.** 시뮬레이터는 '오늘 이전에 결정된' 목표만
  조회하므로 same-day execution 을 코드로 쓸 수 없다.
- **OOS 구간을 최적화가 읽을 수 없다.** `SplitGuard` 가 예외를 던진다.
- **실패는 추측을 만들지 않는다.** 필수 데이터가 없으면 레짐 UNKNOWN,
  점수·배분·레버리지 모두 없음, DATA_FAILURE 알림.
- **재실행이 중복을 만들지 않는다.** 상태·이벤트·알림 모두 자연키 UNIQUE.
- **자동매매 코드가 없다.** 증권사 API 의존성도, 주문 경로도 존재하지 않는다.

---

## 라이선스

Proprietary. 개인 투자 의사결정 보조용.
