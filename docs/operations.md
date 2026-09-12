# Operations Guide

TASK-172. 무언가 잘못됐을 때 읽는 문서.

---

## 0. 30초 상황 파악

| 질문 | 확인 방법 |
| --- | --- |
| 오늘 워커가 돌았나? | Actions → `daily-monitor` 최근 실행 |
| 상태가 저장됐나? | 대시보드 → Current 탭 → Last Update |
| 알림이 나갔나? | 대시보드 → Events 탭 → Alerts 의 `delivery_status` |
| 데이터가 멀쩡한가? | 대시보드 → Operations 탭 → Data Coverage / Findings |

```bash
# 로컬에서 같은 것을 보기
python - <<'PY'
from regime_monitor.pipeline.queries import DashboardQueries
q = DashboardQueries()
print("strategy:", q.active_strategy_version())
print("latest  :", q.latest_state())
print(q.recent_runs().head().to_string())
PY
```

---

## 1. GitHub Actions 확인

### 정상 동작

`daily-monitor` 는 평일 22:30 UTC 에 돈다. 정상이면:

1. 테스트 통과
2. `Daily run` 스텝이 `2024-03-16: Fear (score 31.2, leverage 2.00x)` 같은 한 줄 출력
3. `Verify the database is still readable` 통과
4. DB 가 바뀌었으면 `chore(data): daily state ...` 커밋

### "Strategy not frozen" notice 가 뜬다

정상이다. 전략이 아직 `RESEARCH` 상태라 운영할 전략이 없다.
`docs/strategy.md` §4 의 freeze 절차를 마치면 자동으로 진행된다.

### 비거래일

"database unchanged (likely a non-trading day); nothing to commit" — 정상이다.
파이프라인은 마지막 거래일 상태를 다시 확인하고 아무것도 바꾸지 않는다.

### 워크플로 수동 실행

Actions → `daily-monitor` → Run workflow. 입력:

| 입력 | 용도 |
| --- | --- |
| `date` | 특정 날짜로 재실행 (멱등) |
| `dry_run` | 계산·저장은 하되 알림은 보내지 않음 |
| `profile` | `placeholder` 로 두면 미확정 파라미터로 시험 실행 |

---

## 2. Telegram 설정

### 최초 설정

1. Telegram 에서 [@BotFather](https://t.me/botfather) → `/newbot` → 토큰 확보
2. 만든 봇과 대화를 시작한다 (봇은 먼저 말을 걸 수 없다)
3. chat id 확인:

```bash
curl -s "https://api.telegram.org/bot<TOKEN>/getUpdates" \
  | python -c "import json,sys; print(json.load(sys.stdin)['result'][-1]['message']['chat']['id'])"
```

4. GitHub → Settings → Secrets and variables → Actions → New repository secret
   - `TELEGRAM_BOT_TOKEN`
   - `TELEGRAM_CHAT_ID`

토큰은 코드·config·DB 어디에도 넣지 않는다 (`ARCHITECTURE.md` §9).
`config/alerts.yaml` 은 변수 '이름'만 갖고 있고, 스키마가 값처럼 보이는 것을 거부한다.

### 알림이 오지 않는다

```bash
# 1) 자격증명이 살아 있는지
curl -s "https://api.telegram.org/bot$TELEGRAM_BOT_TOKEN/getMe"

# 2) 보낼 것이 있었는지 (없으면 정상이다 — 레짐이 안 바뀌면 알림도 없다)
python - <<'PY'
from regime_monitor.pipeline.queries import DashboardQueries
print(DashboardQueries().alert_events(limit=20).to_string())
PY
```

`delivery_status` 별 대응:

| 상태 | 의미 | 대응 |
| --- | --- | --- |
| `SENT` | 전송 완료 | 없음 |
| `PENDING` | 저장됐지만 미전송 | 자격증명 확인 후 §2.1 로 재전송 |
| `FAILED` | 전송 시도 실패 | `error` 컬럼 확인 |

`FAILED` 의 흔한 원인:
- `chat not found` — chat id 가 틀렸거나 봇과 대화를 시작하지 않았다
- `Forbidden: bot was blocked by the user` — 봇 차단을 풀어야 한다
- 429 — rate limit. 자동으로 `retry_after` 를 지켜 재시도한다

### 2.1 미전송 알림 재전송

```bash
python - <<'PY'
from regime_monitor.alerts.engine import AlertEngine
from regime_monitor.alerts.telegram import TelegramNotifier
from regime_monitor.config.loader import load_config
from regime_monitor.data.repositories.sqlite import SQLiteUnitOfWork

config = load_config()
engine = AlertEngine(config.alerts, TelegramNotifier.from_spec(config.alerts.telegram))
with SQLiteUnitOfWork() as uow:
    print(engine.send_pending(uow.events).summary())
PY
```

---

## 3. 데이터 장애 대응

### DATA_FAILURE 알림을 받았다

**시스템은 이미 안전하게 동작했다.** 그 날의 레짐은 UNKNOWN 이고, 목표 배분과
레버리지는 계산되지 않았으며, 기존 배분도 지워졌다. 잘못된 신호가 나가지 않았다.

원인 확인:

```bash
python - <<'PY'
from datetime import date
from regime_monitor.config.loader import load_config
from regime_monitor.data.repositories.sqlite import SQLiteUnitOfWork
from regime_monitor.data.validators.freshness import FreshnessValidator

config = load_config()
with SQLiteUnitOfWork() as uow:
    report = FreshnessValidator(config.data_sources).validate(uow.observations, as_of=date.today())
print(report.summary())
for source in report.failures:
    print(" ", source.describe())
PY
```

| 진단 | 의미 | 대응 |
| --- | --- | --- |
| `MISSING` | 데이터가 아예 없음 | 수집 재시도 (§3.1) |
| `STALE` | 허용치보다 오래됨 | 소스 장애 여부 확인 후 재수집 |
| `CORRUPT` | 관측일이 실행일보다 미래 | 시계/날짜 인자 확인 |

### 3.1 수집만 다시 하기

```bash
python - <<'PY'
from datetime import date, timedelta
from regime_monitor.config.loader import load_config
from regime_monitor.data.collection import CollectionService
from regime_monitor.data.repositories.sqlite import SQLiteUnitOfWork

config = load_config()
with SQLiteUnitOfWork() as uow:
    report = CollectionService.from_config(config.data_sources).collect(
        uow.observations, start=date.today() - timedelta(days=30), end=date.today()
    )
print(report.summary())
PY
```

그 다음 워커를 다시 돌린다. 멱등하므로 안전하다.

```bash
python scripts/daily_runner.py --date 2024-03-16
```

### 선택적 소스가 빠졌다

CNN / AAII 는 `mandatory: false` 다. 빠지면 해당 지표만 제외되고 가중치는
나머지로 재분배된다. 파이프라인은 계속 돈다. 상태는 `REVIEW` 로 기록된다.

커버리지가 임계 미만으로 떨어지면 그 날은 점수를 만들지 않는다.

### 교차검증 REVIEW 항목이 쌓였다

```bash
python -c "
from regime_monitor.pipeline.queries import DashboardQueries
print(DashboardQueries().open_findings().to_string())"
```

**자동으로 수정하지 않는다** (`BACKTEST_SPEC.md` §5.5). 사람이 판단한다.
확인 후 해결 처리:

```bash
python - <<'PY'
from datetime import UTC, datetime
from regime_monitor.data.repositories.sqlite import SQLiteUnitOfWork

with SQLiteUnitOfWork() as uow:
    uow.connection.execute(
        "UPDATE data_quality_findings SET resolved_at = ?, resolution_note = ? "
        "WHERE symbol = ? AND observation_date = ? AND check_name = ?",
        (datetime.now(tz=UTC).isoformat(), "확인함: ProShares 공시 분할", "TQQQ", "2024-03-16", "split_events"),
    )
PY
```

---

## 4. SQLite 문제 복구

### 무결성 확인

```bash
python - <<'PY'
from regime_monitor import paths
from regime_monitor.data.repositories.connection import connect

connection = connect(read_only=True)
print(connection.execute("PRAGMA integrity_check").fetchone()[0])
print(connection.execute("SELECT COUNT(*) FROM market_states").fetchone()[0], "states")
PY
```

### DB 가 손상됐다

DB 는 git 으로 관리되므로 이전 커밋으로 되돌릴 수 있다.

```bash
git log --oneline -- data/regime_monitor.db | head -20
git checkout <commit> -- data/regime_monitor.db

# 되돌린 지점 이후를 다시 계산 (멱등)
python scripts/daily_runner.py --date 2024-03-14
python scripts/daily_runner.py --date 2024-03-15
python scripts/daily_runner.py --date 2024-03-16
```

### `-wal` / `-shm` 파일이 남아 있다

정상 종료 시 자동으로 체크포인트된다. 남아 있다면:

```bash
python -c "
from regime_monitor.data.repositories.connection import connect, checkpoint
c = connect(); checkpoint(c); c.close(); print('checkpointed')"
```

이 파일들은 gitignore 되어 있다. 커밋되는 것은 `.db` 하나뿐이며, 그 자체로
완결이어야 한다 (`ARCHITECTURE.md` §8).

### DB 를 처음부터 다시 만든다

```bash
python -c "
from regime_monitor.data.repositories.sqlite import create_database
from regime_monitor import paths
print(create_database(paths.DATA_DIR / 'regime_monitor.db'))"

python scripts/backtest.py --collect --start 2010-02-11   # 이력 재수집
```

### DB 가 너무 커졌다

`PRD.md` §13 의 이전 경로: SQLite → snapshots/Parquet → object storage 또는
managed DB. 리포지토리 계층이 분리되어 있으므로 `data/interfaces.py` 의 Protocol
을 구현한 새 어댑터만 추가하면 된다.

---

## 5. Streamlit 재배포

### Streamlit Cloud

1. https://share.streamlit.io → New app
2. Repository: `gardengo/RegimePilot`, Branch: `main`
3. Main file path: `app/streamlit_app.py`
4. 의존성은 `requirements.txt` 에서 자동 설치된다

대시보드는 **커밋된 DB 파일을 읽는다.** daily 워커가 DB 를 커밋할 때마다
Streamlit Cloud 가 자동 재배포된다.

### 대시보드가 옛날 데이터를 보여준다

1. 사이드바 → 새로고침 (앱 캐시 TTL 은 5분)
2. 그래도 안 바뀌면 daily 워커가 최근에 커밋했는지 확인:
   `git log --oneline -5 -- data/regime_monitor.db`
3. Streamlit Cloud → Manage app → Reboot

### "no database at ..." 오류

배포 브랜치에 `data/regime_monitor.db` 가 없다. daily 워커가 한 번도 커밋하지
않았거나 gitignore 되었는지 확인한다.

---

## 6. 전략 버전 관리

### 현재 운영 전략 확인

```bash
python -c "
from regime_monitor.config.loader import load_config
c = load_config()
print(c.strategy.strategy_version, c.strategy.parameter_status.value)
print('production ready:', c.is_production_ready)
print('unresolved:', c.unresolved_parameters())"
```

### frozen regression 확인 (TASK-102)

```bash
python - <<'PY'
import json
from pathlib import Path
from regime_monitor import paths
from regime_monitor.config.loader import load_config
from regime_monitor.data.repositories.sqlite import SQLiteUnitOfWork
from regime_monitor.research.backtest_runner import StrategyBacktest
from regime_monitor.research.data_loader import load_market_data
from regime_monitor.research.freeze import Fingerprint, RegressionRecord

config = load_config()
record_path = paths.CONFIG_DIR / "frozen" / f"{config.strategy.strategy_version}.regression.json"
record = RegressionRecord.read(record_path)

with SQLiteUnitOfWork() as uow:
    data = load_market_data(uow.observations, config)
record.check(Fingerprint.of_run(StrategyBacktest(config).run(data, include_benchmarks=False)))
print("frozen regression OK")
PY
```

실패하면 코드나 데이터가 바뀐 것이다. **먼저 원인을 찾는다.** 회귀 기준을
새로 쓰는 것은 원인을 이해한 뒤의 마지막 수단이다.

---

## 7. 하지 말아야 할 것

| 하지 말 것 | 이유 |
| --- | --- |
| `config/strategy.yaml` 을 손으로 수정 | manifest 가 조용히 무효가 된다 |
| 운영에서 `REGIME_MONITOR_ALLOW_RESEARCH_PARAMS=1` | 미확정 파라미터로 실제 신호가 나간다 |
| OOS 결과를 보고 파라미터 조정 | 그 구간이 더 이상 OOS 가 아니게 된다 |
| 교차검증 불일치를 임의 보정 | 미공시 정보를 소급 적용하는 누수다 |
| DATA_FAILURE 를 무시하고 이전 신호 사용 | 시스템이 판단을 포기한 날이다. 사람이 판단해야 한다 |
| DB 를 두 프로세스에서 동시에 쓰기 | 워크플로 concurrency 로 막혀 있다. 수동 실행 시 주의 |

---

## 8. 참고 파일 배치

일부 소스는 공개 API 가 없어 운영자가 파일로 넣는다.
전체 규격은 `data/reference/README.md`.

```text
data/reference/
├── proshares/{qld,tqqq}.csv      date, close|nav|price
├── cnn/fear_greed_history.csv    date, value  (0-100)
└── aaii/sentiment.csv            date, bullish, bearish
```

이 파일들은 gitignore 되어 있다. 없어도 파이프라인은 동작하며,
해당 지표가 선택적으로 제외될 뿐이다.
