# RegimePilot

**NASDAQ Leverage Regime Monitor**

Nasdaq-100 시장의 가격 / 추세 / 변동성 / 투자심리 / breadth 지표를 매일 수집·종합하여
현재 **시장 레짐(Market Regime)** 을 판단하고, QQQ · QLD · TQQQ · Cash 의
**목표 비중과 Target Leverage** 를 계산한다.
레짐 또는 중요 상태가 변경되면 Telegram 으로 알림을 보낸다.

> ⚠️ 본 시스템은 **자동매매 시스템이 아니다.**
> 분석 결과는 참고용이며, 실제 매매 여부와 주문은 사용자가 직접 결정한다.

---

## Pipeline

```text
Daily Market Data
    ↓  Indicator Engine
    ↓  Composite Score
    ↓  Market Regime
    ↓  Target Leverage / Allocation
    ↓  Regime Change Detection
    ↓  SQLite Persistence
    ↓  Telegram Alert
```

Streamlit 은 저장된 결과를 시각화하는 Dashboard 역할만 담당한다.

## Deployment

- **GitHub Actions** — `daily_runner.py` 일일 실행 (운영 경로)
- **SQLite** — 저장소에 커밋되는 상태 저장소 (`data/regime_monitor.db`)
- **Streamlit Cloud** — 대시보드
- 로컬 PC 는 운영 경로에 포함되지 않는다.

## Documents

| 문서 | 내용 |
| --- | --- |
| [PRD.md](PRD.md) | 제품 요구사항, 투자 철학, 레짐 정의 |
| [ARCHITECTURE.md](ARCHITECTURE.md) | 시스템 구조, 데이터 흐름, 배포 아키텍처 |
| [BACKTEST_SPEC.md](BACKTEST_SPEC.md) | 백테스트 사양 및 검증 기준 |
| [TASKS.md](TASKS.md) | 구현 작업 분해 |
| [CLAUDE_CODE_INITIAL_PROMPT.md](CLAUDE_CODE_INITIAL_PROMPT.md) | 개발 에이전트 초기 지침 |

## Secrets

Telegram 토큰 등 민감정보는 **GitHub Secrets / 환경 변수** 로만 관리한다.
config YAML 이나 SQLite 에 plaintext secret 을 저장하지 않는다.

## Status

초기 설계 단계 — 구현은 [TASKS.md](TASKS.md) 순서를 따른다.
