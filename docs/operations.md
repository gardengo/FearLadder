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
from fear_ladder.pipeline.queries import DashboardQueries
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

**더 이상 정상이 아니다.** 전략은 2026-09-13 에 `v1.0-frozen` 으로 고정됐다
(`docs/strategy.md` §2.8). 이 메시지가 보인다면 `config/strategy.yaml` 이
고정본이 아니라는 뜻이므로, 덮어써졌거나 체크아웃이 잘못된 것이다.

확인:

```bash
python -c "import sys; sys.path.insert(0,'src');   from fear_ladder.config.loader import load_config;   c = load_config(); print(c.strategy.strategy_version, c.strategy.parameter_status.value)"
# v1.0-frozen FROZEN 이 나와야 한다
```

`config/frozen/v1.0-frozen.manifest.json` 이 그 숫자들에 대한 증거다.
복구는 git 에서 되돌리는 것이며, 손으로 채워 넣는 것이 아니다.

### 비거래일

"database unchanged (likely a non-trading day); nothing to commit" — 정상이다.
파이프라인은 마지막 거래일 상태를 다시 확인하고 아무것도 바꾸지 않는다.

### 워크플로 수동 실행

Actions → `daily-monitor` → Run workflow. 입력:

| 입력 | 용도 |
| --- | --- |
| `date` | 특정 날짜로 재실행 (멱등) |
| `dry_run` | 계산·저장은 하되 알림은 보내지 않음 |
| `profile` | `placeholder` 로 두면 개발용 임시값으로 시험 실행 (실제 신호 아님) |

---

### 1.1 DB 가 매일 커밋되는데 왜 계속 작아지는가

워크플로는 매 거래일 `data/fear_ladder.db` 를 통째로 커밋한다. 실제로 재보면
git 이 이 파일을 생각보다 잘 다룬다 — 9.7MB 파일이 pack 에서 1.2MB 이고, 하루치
변경은 약 **6KB** 다 (VACUUM 이 일어난 날은 약 51KB). 연 1.5~13MB 수준이므로
저장소가 감당 못 할 크기로 불어나지는 않는다.

그래도 커밋 직전에 `scripts/prune_observations.py --keep-years 5` 가 돈다.
원시 관측치만 5년 롤링 창으로 잘라내 작업 트리와 새 클론을 작게 유지한다.
VACUUM 은 매번 하지 않는다 — 파일을 통째로 다시 쓰므로 그날 커밋이 6KB 에서
51KB 로 커진다. 빈 페이지가 10% 를 넘을 때만 압축한다.
**계산된 상태(단계·배분·점수·이벤트)는 지우지 않는다** — 용량이 작고, 대시보드
'기록' 탭이 실제로 보여주는 것이 그쪽이다.

창 길이를 줄이려면 주의해야 한다. 스크립트는 설정에서 **지표가 실제로 필요로
하는 기간을 계산**해서, 그보다 짧은 창을 요구하면 거부한다:

```bash
python scripts/prune_observations.py --keep-years 1
# refusing to prune: 1 years is below the 4.13-year floor ...
```

거부하는 이유는 짧은 창이 오류를 내지 않기 때문이다. 절반만 찬 롤링 창으로
점수가 계산되고, 아무도 그 사실을 모른 채 신호가 나간다. 현재 하한은 AAII
주간 지표(104주 = 약 2년)가 정한다.

30년 전체 성과는 `reports/performance.json` 에 이미 계산돼 있으므로 '성과' 탭은
이 정리에 영향받지 않는다.

---

## 2. Telegram 설정

### 최초 설정

```bash
# 1) 토큰과 chat id 를 환경에 둔다 (파일에 쓰지 않는다)
#    로컬: .env,  운영: GitHub Actions repository secret
gh secret set TELEGRAM_BOT_TOKEN
gh secret set TELEGRAM_CHAT_ID

# 2) 연결이 되는지 확인한다 — 실제로 한 통 보낸다
python scripts/notify.py --test

# 3) 지금 상태 확인
python scripts/notify.py --status
```

`--test` 가 없으면 설정이 맞는지는 **단계가 바뀌는 날**에야 알게 된다. chat id
오타를 확인하기에 가장 나쁜 날이다.

메시지가 어떻게 생겼는지는 네트워크 없이 볼 수 있다:

```bash
python scripts/notify.py --preview   # 6개 템플릿 전부, 저장된 최신 상태로 렌더
```

#### 채널이 없을 때 알림은 어떻게 되는가

**전송되지 않고 `PENDING` 으로 남는다.** 일일 워커는 알림을 보내기 *전에* 먼저
저장하므로, 채널이 없거나 죽어 있어도 사라지지 않는다. 나중에 토큰을 설정하면
다음 실행이 자동으로 밀린 것을 배달한다 (7일 이내 것만 — 그보다 오래된 알림은
이미 지나간 시장을 설명하므로 `SUPPRESSED` 로 내린다).

수동으로 밀어 넣으려면:

```bash
python scripts/notify.py --resend                # 최근 7일
python scripts/notify.py --resend --within-days 0  # 제한 없이 전부
```

> 2026-09-13 이전에는 이게 반대로 동작했다. 토큰이 없으면 `NullNotifier` 가
> 조용히 성공해서 알림이 **`SENT` 로 기록**됐다 — 아무도 받지 못했는데
> 기록은 보냈다고 말했고, 나중에 재전송할 방법도 없었다. 이제 배달하지 않는
> 채널은 그렇다고 선언하고, 알림은 `PENDING` 으로 남는다.

### Telegram 봇 만들기

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
from fear_ladder.pipeline.queries import DashboardQueries
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
from fear_ladder.alerts.engine import AlertEngine
from fear_ladder.alerts.telegram import TelegramNotifier
from fear_ladder.config.loader import load_config
from fear_ladder.data.repositories.sqlite import SQLiteUnitOfWork

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
from fear_ladder.config.loader import load_config
from fear_ladder.data.repositories.sqlite import SQLiteUnitOfWork
from fear_ladder.data.validators.freshness import FreshnessValidator

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
from fear_ladder.config.loader import load_config
from fear_ladder.data.collection import CollectionService
from fear_ladder.data.repositories.sqlite import SQLiteUnitOfWork

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
from fear_ladder.pipeline.queries import DashboardQueries
print(DashboardQueries().open_findings().to_string())"
```

**자동으로 수정하지 않는다** (`BACKTEST_SPEC.md` §5.5). 사람이 판단한다.
확인 후 해결 처리:

```bash
python - <<'PY'
from datetime import UTC, datetime
from fear_ladder.data.repositories.sqlite import SQLiteUnitOfWork

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
from fear_ladder import paths
from fear_ladder.data.repositories.connection import connect

connection = connect(read_only=True)
print(connection.execute("PRAGMA integrity_check").fetchone()[0])
print(connection.execute("SELECT COUNT(*) FROM market_states").fetchone()[0], "states")
PY
```

### DB 가 손상됐다

DB 는 git 으로 관리되므로 이전 커밋으로 되돌릴 수 있다.

```bash
git log --oneline -- data/fear_ladder.db | head -20
git checkout <commit> -- data/fear_ladder.db

# 되돌린 지점 이후를 다시 계산 (멱등)
python scripts/daily_runner.py --date 2024-03-14
python scripts/daily_runner.py --date 2024-03-15
python scripts/daily_runner.py --date 2024-03-16
```

### `-wal` / `-shm` 파일이 남아 있다

정상 종료 시 자동으로 체크포인트된다. 남아 있다면:

```bash
python -c "
from fear_ladder.data.repositories.connection import connect, checkpoint
c = connect(); checkpoint(c); c.close(); print('checkpointed')"
```

이 파일들은 gitignore 되어 있다. 커밋되는 것은 `.db` 하나뿐이며, 그 자체로
완결이어야 한다 (`ARCHITECTURE.md` §8).

### DB 를 처음부터 다시 만든다

```bash
python -c "
from fear_ladder.data.repositories.sqlite import create_database
from fear_ladder import paths
print(create_database(paths.DATA_DIR / 'fear_ladder.db'))"

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
2. Repository: `gardengo/FearLadder`, Branch: `main`
3. Main file path: `app/streamlit_app.py`
4. 의존성은 `requirements.txt` 에서 자동 설치된다

대시보드는 **커밋된 DB 파일을 읽는다.** daily 워커가 DB 를 커밋할 때마다
Streamlit Cloud 가 자동 재배포된다.

### 대시보드가 옛날 데이터를 보여준다

1. 사이드바 → 새로고침 (앱 캐시 TTL 은 5분)
2. 그래도 안 바뀌면 daily 워커가 최근에 커밋했는지 확인:
   `git log --oneline -5 -- data/fear_ladder.db`
3. Streamlit Cloud → Manage app → Reboot

### "no database at ..." 오류

배포 브랜치에 `data/fear_ladder.db` 가 없다. daily 워커가 한 번도 커밋하지
않았거나 gitignore 되었는지 확인한다.

---

## 6. 전략 버전 관리

### 현재 운영 전략 확인

```bash
python -c "
from fear_ladder.config.loader import load_config
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
from fear_ladder import paths
from fear_ladder.config.loader import load_config
from fear_ladder.data.repositories.sqlite import SQLiteUnitOfWork
from fear_ladder.research.backtest_runner import StrategyBacktest
from fear_ladder.research.data_loader import load_market_data
from fear_ladder.research.freeze import Fingerprint, RegressionRecord

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
| 운영에서 `FEAR_LADDER_ALLOW_RESEARCH_PARAMS=1` | 미확정 파라미터로 실제 신호가 나간다 |
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
