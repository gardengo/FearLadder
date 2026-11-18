# Operations Guide

TASK-172. 무언가 잘못됐을 때 읽는 문서.

---

## 0. 30초 상황 파악

| 질문 | 확인 방법 |
| --- | --- |
| 오늘 워커가 돌았나? | Actions → `daily-monitor` 최근 실행 |
| 상태가 저장됐나? | 대시보드 → '오늘' 탭 → Last Update |
| 알림이 나갔나? | 대시보드 → '이벤트' 탭 → Alerts 의 `delivery_status` |
| 데이터가 멀쩡한가? | 대시보드 → '운영' 탭 → Data Coverage / Findings |

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

`daily-monitor` 는 미국 거래일 다음 날(화~토)에 여러 번 시작된다. 하루에 실제로
일하는 것은 그중 한 번이다.

| 누가 | 언제 (한국 시간) | 입력 |
| --- | --- | --- |
| cron-job.org (외부, 주 경로) | 11:07 · 13:07 · 15:07 | `attempt: scheduled` |
| cron-job.org (외부, 주 경로) | 18:07 | `attempt: last` |
| GitHub 예약 (예비) | 10:17 · 12:17 · 14:17 · 17:17 (01:17 ~ 08:17 UTC) — 실제로는 몇 시간 늦게 | 마지막이 `--last-attempt` |

외부 cron 이 주 경로인 이유는 아래와 같다 (TASK-195). 설정 방법은
"외부 스케줄러 (cron-job.org)" 절에 있다.

여러 번인 이유는 GitHub 가 예약 실행을 제때 시작하지 않아서다. 06:00 UTC 한 번만
예약했던 2026-09-15 ~ 11-14 의 45번 모두 실제 시작은 09:56 ~ 14:23 UTC 였고
(3.9 ~ 8.4시간 지연, 평균 5.7시간), 11/12 에는 미국 개장 5분 전에 끝났다. 지연은
설정으로 줄일 수 없으므로
기회를 여러 번 주고, 그날이 준비된 것을 처음 확인한 실행이 일을 한다.

네 번으로 나눈 첫날(2026-11-17)에도 7.9 ~ 9.3시간 늦었다. 예약 실행(`schedule`)은
GitHub 가 한가할 때 돌리는 것이고, `workflow_dispatch` 는 요청한 때 바로 시작한다
(11/11 의 수동 실행은 요청 1분 안에 끝났다). 그래서 외부 cron 이 REST API 로
워크플로를 시작시키고, GitHub 의 예약은 외부 cron 이 실패했을 때의 예비로 둔다.

각 실행은 먼저 `Is there anything to do?` 스텝(`scripts/batch_gate.py`)에 묻는다:

| 상황 | 결과 |
| --- | --- |
| 사람의 수동 실행 (`attempt: manual`, 기본값) | 항상 실행 |
| 직전 거래일이 이미 기록됨 | 건너뜀 — 같은 날을 두 번 계산하지 않는다 |
| 마지막 시도(외부 `last` 또는 GitHub 08:17 예약)인데 아직 기록 안 됨 | 피드 상태와 무관하게 실행 |
| 그 앞 시도(외부 `scheduled`, GitHub 의 앞 세 예약) | QQQ · QLD · TQQQ · VIX · CNN 이 **모두** 새 날짜를 갖고 있을 때만 실행 |

건너뛴 실행은 `Nothing to do` 알림 한 줄을 남기고 성공으로 끝난다. DB 를 읽기
전용으로만 열기 때문에 커밋도 없다. 그래서 Actions 목록에 하루 여덟 줄(외부 넷,
GitHub 넷)이 보이고 그중 하나만 1분 넘게 걸리는 것이 정상이다.

마지막 시도는 둘이다 (외부 18:07, GitHub 08:17 UTC 예약). 평일에는 먼저 일한
쪽이 기록하므로 나중 것은 물러난다. 휴장일 다음 날에는 둘 다 돌지만, 기록된 날은
건드리지 않고 실행 기록만 남긴 DB 를 커밋한다 — 해마다 열흘 남짓이고 해가 없다.

**cron-job.org 의 마지막 작업(`last`)을 Test run 하지 않는다.** 해는 없지만(위와
같이 기록된 날은 그대로다) 그날의 실행 기록이 하나 늘고 커밋이 생긴다. 시험은
`scheduled` 작업으로 한다.

같은 날을 두 번 계산하지 않는 데는 이유가 있다. CNN 은 하루의 값을 그 뒤 하루
이틀 동안 고친다 (2026-11-10 의 값이 04:46 UTC 에는 33.5, 13:19 UTC 에는 37.1,
다음 날에는 37.4 였다). 다시 계산하면 이미 알린 신호가 바뀔 수 있는데, 알림은
날짜로 중복 제거되므로 두 번째 알림은 나가지 않는다.

휴장일 다음 날에는 앞의 시도들이 "새 날짜가 없다" 며 물러나고 마지막 시도만 돈다.
그 실행은 신선도 검사를 하고, 이미 기록된 직전 거래일은 **다시 계산하지 않고
그대로 둔다** (TASK-196). `Daily run` 로그와 커밋 메시지에 이렇게 남는다:

```
2026-11-16: Optimism (score 13.9, leverage 1.33x) [already recorded; kept]
```

피드가 며칠째 멈춰 있는 경우를 드러내는 것도 이 실행이다 (신선도 검사는
파이프라인이 돌아야 말한다).

다시 계산하지 않는 이유: 하루의 입력은 알린 뒤에도 움직인다 (CNN 이 하루 이틀
고친다). 다시 계산하면 이미 알린 단계나 목표 레버리지가 알림 없이 바뀔 수 있고,
모의 계좌는 바뀐 목표로 다음 시가에 거래한다. 2026-11-18 에 cron-job.org 의 마지막
작업을 오전에 시험 실행했을 때 실제로 11/16 의 점수가 13.877 → 13.941 로 바뀌었다
(단계와 레버리지는 그대로). 같은 날을 다시 계산하는 것은 사람의 수동 실행
(`attempt: manual`)만 한다.

가격 피드는 "오늘(UTC) 날짜 이전에 시작한 봉" 만 돌려준다. 그래서 00:00 UTC
이전에 예약하는 것은 의미가 없고, 예약을 더 앞당길 여지는 01:17 이 사실상
끝이다.

일을 한 실행이 정상이면:

1. 테스트 통과
2. `Daily run` 스텝이 `2024-03-16: Fear (score 31.2, leverage 2.00x)` 같은 한 줄 출력
3. `Verify the database is still readable` 통과
4. DB 가 바뀌었으면 `chore(data): daily state ...` 커밋

### 하루가 늦게 기록됐다

가격 피드가 하루 늦은 날(2026-09-22 는 다음 날 19:34 KST 에도 봉이 없었다)에는
그날 아침의 실행들이 물러나고, 다음 날의 실행이 두 날을 함께 기록한다. `Daily run`
로그에 이렇게 남는다:

```
stored 1 earlier session(s) no run had recorded, none of which changed the signal: 2026-09-22
```

뒤늦게 쓴 행은 사유에 `RECORDED_LATE:<기록한 날>` 이 붙는다. **신호가 바뀌지 않은
날만** 이렇게 채워진다. 건너뛴 날에 단계나 목표 레버리지가 바뀌었다면 그날은
비워 두고, 변경은 기록한 날의 이벤트(`실제 전환 <날짜>`)와 알림으로 나간다 —
'기록' 탭 위쪽 줄에 "신호 기록이 없습니다" 로 그 날짜가 적힌다. 둘 다 정상이다.

### 네 번 모두 건너뛰었다

`Is there anything to do?` 스텝의 로그에 이유가 한 줄로 있다.

- `... is already recorded` 가 네 번 — 그 전날 이미 기록됐다는 뜻이다. 수동 실행을
  했거나 날짜를 착각한 경우다. 문제가 아니다.
- 마지막 예약(08:17)이 `--last-attempt` 없이 돌았다 — 워크플로의 예약 목록과
  `LAST_ATTEMPT_CRON` 이 어긋난 것이다. 둘은 같은 문자열이어야 한다.
- 외부 18:07 실행이 `--last-attempt` 없이 돌았다 — cron-job.org 의 마지막 작업
  본문이 `"attempt":"last"` 가 아니다.
- 외부 실행이 아예 없다 — cron-job.org 작업이 꺼졌거나 토큰이 만료됐다. 아래
  "외부 스케줄러" 절의 확인 순서를 따른다. 그동안은 GitHub 예약이 늦게나마 돈다.
- `could not read ...` — 그 피드가 아침 내내 응답하지 않았다. 마지막 예약은 그래도
  돌았어야 하므로 그 실행의 `Daily run` 로그를 본다.

급하면 수동 실행이 가장 빠르다: `gh workflow run daily_monitor.yml --ref main`.
수동 실행은 게이트를 거치지 않고, 같은 날을 다시 계산한다. 게이트를 거치게 하려면
`-f attempt=scheduled` 를 붙인다.

### "Strategy is not frozen" 으로 실패한다

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
| `attempt` | `manual`(기본): 항상 실행. `scheduled` / `last`: 외부 스케줄러용 — 예약과 같은 게이트 |

### 외부 스케줄러 (cron-job.org)

TASK-195. GitHub REST API 의 workflow dispatch 를 정해진 시각에 호출한다. 서버는
필요 없다.

**1. 토큰 (GitHub)** — Settings → Developer settings → Personal access tokens →
Fine-grained tokens → Generate new token.

| 항목 | 값 |
| --- | --- |
| Token name | `fearladder-cron` |
| Expiration | 1년 (만료일을 달력에 적어 둔다) |
| Repository access | Only select repositories → `gardengo/FearLadder` |
| Repository permissions | **Actions: Read and write** (Metadata: Read-only 는 자동). 그 밖에는 없음 |

이 토큰으로 할 수 있는 것은 이 저장소의 워크플로 실행 · 취소 · 재실행과 실행
기록 삭제뿐이다. 코드와 DB 는 바꿀 수 없다. 새어 나갔다고 의심되면 GitHub 에서
바로 폐기하고 새로 만든다.

**2. 작업 (cron-job.org)** — 작업 두 개를 만든다. 본문만 다르다.

| 항목 | 값 |
| --- | --- |
| URL | `https://api.github.com/repos/gardengo/FearLadder/actions/workflows/daily_monitor.yml/dispatches` |
| 시간대 | `Asia/Seoul` |
| 요일 | 화 · 수 · 목 · 금 · 토 |
| 작업 A 시각 | 11:07, 13:07, 15:07 |
| 작업 B 시각 | 18:07 |
| Request method | `POST` |
| Headers | `Accept: application/vnd.github+json`<br>`Authorization: Bearer <토큰>`<br>`X-GitHub-Api-Version: 2022-11-28`<br>`Content-Type: application/json` |
| 작업 A 본문 | `{"ref":"main","inputs":{"attempt":"scheduled"}}` |
| 작업 B 본문 | `{"ref":"main","inputs":{"attempt":"last"}}` |
| 실패 알림 | 켠다 (이메일) |

성공하면 GitHub 가 **204 No Content** 를 돌려준다. 401 은 토큰, 404 는 URL 이나
토큰의 저장소 범위, 422 는 본문(입력값 이름이나 `ref`) 문제다.

시각을 이렇게 둔 이유: 미국 장 마감은 한국 시간 06:00(서머타임 05:00)이지만
가격 피드가 그 봉을 내놓는 시각은 들쑥날쑥하다 (09:45 에는 없었고 13:45 에는
있었다). 이른 시도는 물러날 뿐 비용이 거의 없으니 11:07 부터 두 시간 간격으로
묻고, 18:07 에 마지막으로 무조건 돈다. 미국 개장(23:30, 서머타임 22:30)보다
넉넉히 앞선다.

**3. 확인** — Actions 목록에서 이벤트가 `workflow_dispatch` 인 실행이 위 시각에
생기는지, `Is there anything to do?` 로그에 `RUN` / `SKIP` 판정이 남는지 본다.
cron-job.org 의 실행 기록에 응답 코드 204 가 찍혀야 한다.

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
**계산된 상태(단계·배분·점수·이벤트·모의 성과 기록)는 지우지 않는다** — 용량이
작고, 대시보드 '기록' 탭이 실제로 보여주는 것이 그쪽이다. '기록' 탭의 전략 vs QQQ
성과도 그래서 가격이 아니라 `track_record` 테이블에서 나온다: 워커가 하루씩 이어
붙이고 다시 계산하지 않으므로, 그 밑의 가격이 5년 창 밖으로 잘려 나가도 기록은
남는다 (ARCHITECTURE.md §4.1.3).

창 길이를 줄이려면 주의해야 한다. 스크립트는 설정에서 **지표가 실제로 필요로
하는 기간을 계산**해서, 그보다 짧은 창을 요구하면 거부한다:

```bash
python scripts/prune_observations.py --keep-years 1
# refusing to prune: 1 years is below the 4.13-year floor ...
```

거부하는 이유는 짧은 창이 오류를 내지 않기 때문이다. 절반만 찬 롤링 창으로
점수가 계산되고, 아무도 그 사실을 모른 채 신호가 나간다. 현재 하한은 AAII
주간 지표(104주 = 약 2년)가 정한다.

30년 전체 성과는 `reports/performance.json` 에 이미 계산돼 있으므로 '백테스트' 탭은
이 정리에 영향받지 않는다.

**대신 그 리포트를 이 체크아웃에서 다시 만들 수는 없다.** 5년으로 잘린 DB 로
`make_performance_report.py` 를 돌리면 1996–2026 리포트가 2021–2026 리포트로
바뀐다 — 파일 이름도 모양도 같고 숫자만 다르다. 그래서 스크립트가 거부한다:

```bash
python scripts/make_performance_report.py
# refusing to write: the loaded data starts 2021-09-15, after the frozen
# strategy's research window opens (1999-01-01) ...
```

진짜로 다시 만들려면 `scripts/backfill_history.py` 로 전체 이력을 DB 에 되돌린
뒤에 실행한다. 5년치 리포트가 정말 필요하면 `--allow-short-window --out` 으로
다른 경로에 쓴다. 커밋된 리포트는 건드리지 않는다.

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
managed DB. 리포지토리 계층이 분리되어 있으므로 `src/fear_ladder/data/interfaces.py` 의 Protocol
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

### 레짐 앵커 (TASK-188)

워커는 레짐을 매일 처음부터 재생하지 않고 어제 저장한 상태(`regime_memory`)에서
이어받는다. DB 에 그 기억이 없을 때 — 처음 배포한 날, DB 를 새로 만든 날 — 는
`config/frozen/<version>.regime_anchor.json` 에서 시작한다. 고정 백테스트의 재생이
끝난 자리다 (ARCHITECTURE.md §4.1.1).

지금 워커가 어디서 이어받고 있는지:

```bash
sqlite3 data/fear_ladder.db \
  "SELECT observation_date, regime, since, days_held FROM regime_memory
   ORDER BY observation_date DESC LIMIT 5"
```

앵커를 다시 만들어야 하는 경우는 둘뿐이다 — 새 전략을 고정했을 때, 그리고
`REGIME_MEMORY_OUT_OF_WINDOW` 로 워커가 멈췄을 때(DB 의 기억과 앵커가 모두
불러온 이력보다 오래됐다는 뜻이다). 전체 이력이 필요하므로 운영 DB 로는 안 된다:

```bash
python scripts/collect_full_history.py --out ../scratch/full.db
python scripts/make_regime_anchor.py --db ../scratch/full.db            # 고정 구간의 마지막 날
python scripts/make_regime_anchor.py --db ../scratch/full.db \
    --as-of 2027-03-31 --force                                          # 더 최근 날짜로
```

스크립트는 쓰기 전에 연구 구간을 다시 돌려 고정 당시의 fingerprint 와 맞는지
확인하고, 맞지 않으면 거부한다. **이미 있는 앵커를 `--force` 로 바꾸는 것은
실시간 레짐이 따르는 경로를 바꾸는 일이다.** DB 에 더 최근의 기억이 있으면 워커는
그쪽을 쓰므로 앵커만 바꿔서는 아무 일도 일어나지 않는다 — 그것이 의도다.

`backfill_history.py` 로 **앵커 날짜 이전**을 다시 재생하면 그 구간은 불러온 창의
첫 점수일에서 시작한 경로를 따른다. 백테스트의 경로가 아니다. 과거 구간을 백테스트
경로로 다시 쓰려면 아래 스크립트를 쓴다.

### 고정 이전 기록을 백테스트 경로로 되돌리기 (TASK-191)

저장된 이력은 성격이 다른 두 부분이다. **고정 이전**은 대시보드에 보여 줄 것이
있도록 고정된 규칙을 과거 날짜에 재생한 것이고, **고정 이후**는 워커가 매일 실제로
알린 신호다. 앞부분은 TASK-188 이전의 재생이라 백테스트가 간 적 없는 경로를
따르고 있었다 (676일 중 226일만 일치).

`scripts/rewrite_replayed_history.py` 는 앞부분만 다시 쓴다. 고정 이후의 행은
틀렸던 날을 포함해 그대로 둔다. 고정 이후에 틀렸던 구간은 아래의 TASK-194 가
따로 맞춘다.

```bash
python scripts/collect_full_history.py --out ../scratch/full.db

# 1) 복사본에서 리허설한다. 운영 DB 는 --operational 없이는 거부된다.
cp data/fear_ladder.db ../scratch/rehearsal.db
python scripts/rewrite_replayed_history.py --full-db ../scratch/full.db --db ../scratch/rehearsal.db

# 2) 결과가 맞으면 운영 DB 에 적용하고, 바로 커밋 · 푸시한다.
python scripts/rewrite_replayed_history.py --full-db ../scratch/full.db \
    --db data/fear_ladder.db --operational
```

스크립트가 하는 일과 스스로 확인하는 것:

1. 전체 이력으로 백테스트를 돌려 고정 당시의 fingerprint 와 커밋된 앵커에 닿는지
   확인한다. 닿지 않으면 멈춘다.
2. 전체 이력의 임시 복사본에서 저장된 첫날부터 앵커 날짜까지 파이프라인을 하루씩
   재생한다. 레짐 규칙은 백테스트가 그 전날 들고 있던 상태에서 이어받는다.
3. 재생한 **모든 날**을 백테스트와 대조한다 — 단계, 목표 레버리지, 그리고 단계
   변경 이벤트의 날짜와 방향. 하루라도 다르면 대상 DB 를 건드리지 않고 멈춘다.
4. 그 구간의 상태 · 배분 · 지표값 · 점수 · 이벤트 · 알림을 한 트랜잭션으로 바꿔
   넣는다. 알림은 `SUPPRESSED` 로 들어가므로 전송되지 않는다.
5. 고정 이후 거래일 중 상태가 없는 날(배치가 돌지 않았거나 피드가 늦은 날)을
   채운다. 채운 뒤 `regime_memory` 는 원래대로 되돌린다.
6. 모의 성과 기록을 처음부터 다시 만든다 — 따르던 목표 비중이 바뀌었으므로.
7. 고정 이후의 상태와 `regime_memory` 가 한 행도 바뀌지 않았는지 확인한다.

**운영 DB 에 적용할 때는 봇의 커밋과 겹치지 않아야 한다.** DB 는 바이너리라 병합이
안 된다. 예약 실행이 없는 시간대(한국 시간 토요일 저녁 ~ 화요일 오전 10시)에
`git pull` → 적용 → 커밋 → 푸시를 한 번에 한다.

### 고정 이후의 틀렸던 구간을 엔진 경로로 맞추기 (TASK-194)

TASK-188 이전의 워커는 고정 시점의 상태에서 레짐 규칙을 이어받지 않아, 저장된
경로가 엔진과 어긋났다. 수정 후 첫 실행이 따라잡기 이벤트(`REGIME_CHANGED_ON:<엔진이
옮긴 날>`)로 경로를 되돌렸지만, 그 사이의 날들은 어긋난 경로를 그대로 적고 있었다
(2026-10-02 ~ 11-09, 27거래일, Neutral 1.75x — 엔진은 Optimism 1.33x).

그 신호로 실제 운용한 적이 없으므로 소유자가 엔진 경로로 맞추기로 했다
(2026-11-18). `scripts/realign_live_window.py` 가 따라잡기 이벤트마다:

1. 엔진이 옮긴 날부터 따라잡은 날 전날까지의 상태를 파이프라인으로 다시 기록한다.
   레짐 규칙은 그 전날의 `regime_memory` 에서 이어받는다 — 엔진의 경로다.
2. 변경은 엔진이 옮긴 날의 평범한 이벤트가 되고, 그 알림은 `SUPPRESSED` 로 들어간다.
3. 따라잡기 이벤트를 지우고, 따라잡은 날의 상태를 전날에 다시 잇는다. 그날의 단계 ·
   점수 · 목표는 이미 엔진의 것이라 그대로다. 실제로 전송된 알림은 기록에 남긴다.
4. `regime_memory` 를 원래대로 되돌리고, 다시 쓴 모든 날의 단계가 그날의 기억과 같은지,
   다른 날의 상태가 한 행도 바뀌지 않았는지 확인한 뒤에야 대상 DB 를 바꾼다.
5. 모의 성과를 처음부터 다시 만든다.

```bash
cp data/fear_ladder.db ../scratch/realign.db
python scripts/realign_live_window.py --db ../scratch/realign.db       # 리허설
python scripts/realign_live_window.py --db data/fear_ladder.db --operational
```

따라잡기 이벤트가 없으면 아무것도 하지 않는다. 앞으로 생기는 따라잡기(놓친 날에
단계가 바뀐 경우, TASK-193)도 같은 스크립트로 맞출 수 있지만, 그것은 실제로 나간
신호를 고쳐 쓰는 일이므로 운용을 시작한 뒤에는 쓰지 않는다.

### 백테스트 탭의 누적 곡선 (TASK-190)

`reports/equity_curve.json` 은 '백테스트' 탭의 누적 수익 · 낙폭 차트가 읽는 파일이다.
`performance.json` 과 달리 고정의 증거가 아니라 그 증거의 **그림**이고, 그래서
스크립트가 둘이 어긋나지 않게 막는다:

```bash
python scripts/make_equity_curve.py --db ../scratch/full.db
```

곡선은 리포트의 마지막 날(2026-09-11)에서 끊고, 쓰기 전에 전략과 QQQ 의 최종
배수가 리포트의 숫자(202.0x, 48.3x)와 같은지 확인한다. 가격 제공자가 과거 가격을
고쳤거나 설정이 고정본이 아니면 거부한다. 파일이 없으면 탭은 숫자만 보여 준다.

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
| `regime_anchor.json` 을 손으로 수정하거나 `regime_memory` 행을 지우기 | 실시간 레짐이 백테스트와 다른 경로로 옮겨 간다. 알림 없이 목표 비중이 바뀐다 |
| 고정 이후(2026-09-14 ~)의 저장된 상태를 고쳐 쓰기 | 실제로 알린 신호의 기록이다. 틀렸던 날도 틀린 채로 남아야 기록이다 |
| 같은 날을 수동으로 여러 번 다시 실행 | CNN 이 하루의 값을 뒤에 고치므로 알린 신호가 알림 없이 바뀔 수 있다 |

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
