# Reference datasets

Third-party inputs, not results of this project, so **nothing here is
committed** (see `.gitignore`) — a public repository is not the place to
redistribute someone else's survey data.

`scripts/fetch_reference.py` downloads the CNN and AAII files where the source
allows it, and the daily workflow runs it on every job so a fresh runner has
them. The ProShares exports still have to be placed by hand. When a fetch
fails the run continues — those indicators are optional — but the workflow
raises a warning, because a silently redistributed weight means the live signal
no longer matches the backtested one.

## Expected layout

```text
data/reference/
├── proshares/
│   ├── qld.csv                 # TASK-028 cross-validation reference
│   └── tqqq.csv
├── cnn/
│   └── fear_greed_history.csv  # reconstructed Fear & Greed history
└── aaii/
    └── sentiment.csv           # AAII weekly survey export
```

## `proshares/<symbol>.csv` — TASK-028

Downloaded from ProShares' official product pages (historical NAV / market
price). Required columns, case-insensitive:

| column | meaning |
| --- | --- |
| `date` | trading day |
| `close` (or `nav`, or `price`) | closing level |

Used by `ProSharesCrossValidator` to check trading dates, split events, price
continuity and price level against what FinanceDataReader returned. A
disagreement is recorded as a `REVIEW` finding in `data_quality_findings`; it is
**never** auto-corrected (`BACKTEST_SPEC.md` §5.5).

Without this file, cross-validation cannot run and the check is skipped — which
is recorded, not silently ignored.

## `cnn/fear_greed_history.csv`

CNN's live endpoint serves roughly one year of history, so a long backtest needs
a reconstructed secondary dataset (`PRD.md` §6.5). Required columns: `date`,
`value` (the 0–100 published greed scale).

Every row loaded from this file is stored with `quality_status = REVIEW` and an
`underlying_source` that says "reconstructed", so a backtest can never present
it as if CNN had published it.

## `aaii/sentiment.csv`

AAII's weekly Investor Sentiment Survey export. Required columns: `date`,
`bullish`, `bearish`. Percentages may be written either as `38.5` or `0.385`.

`date` is the **week-ending date the survey describes**, not the publication
date. The configured `availability_lag_days` in `config/data_sources.yaml` adds
the publication delay, so a backtest cannot read the survey before it existed
(`BACKTEST_SPEC.md` §24.6).

## Breadth

There is no breadth file. Reconstructing historical Nasdaq-100 breadth from
today's constituents is forbidden (`PRD.md` §6.5) because it injects
survivorship bias precisely during the crashes breadth is meant to measure.
Breadth stays disabled until point-in-time constituent data exists.
