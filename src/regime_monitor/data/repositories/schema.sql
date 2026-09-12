-- TASK-010 — SQLite schema for data/regime_monitor.db
--
-- Conventions
--   * dates are stored as ISO 'YYYY-MM-DD' TEXT
--   * instants are stored as ISO-8601 UTC TEXT ending in '+00:00'
--   * JSON payloads are TEXT
--   * every table that the daily worker writes carries a UNIQUE key that makes
--     re-running the same day a no-op (ARCHITECTURE.md §11)

PRAGMA foreign_keys = ON;

-- ---------------------------------------------------------------- migrations
CREATE TABLE IF NOT EXISTS schema_migrations (
    version     INTEGER PRIMARY KEY,
    applied_at  TEXT NOT NULL,
    description TEXT NOT NULL
);

-- --------------------------------------------------------- market_observations
-- One row per (symbol, trading day). Price series fill open/high/low/close/
-- adj_close/volume; scalar series (VIX, CNN F&G, AAII) fill `value`.
CREATE TABLE IF NOT EXISTS market_observations (
    id                       INTEGER PRIMARY KEY AUTOINCREMENT,
    symbol                   TEXT NOT NULL,
    observation_date         TEXT NOT NULL,
    -- BACKTEST_SPEC.md §4: when this value could first be known.
    availability_datetime    TEXT NOT NULL,
    open                     REAL,
    high                     REAL,
    low                      REAL,
    close                    REAL,
    adj_close                REAL,
    volume                   REAL,
    value                    REAL,
    quality_status           TEXT NOT NULL DEFAULT 'OK',
    note                     TEXT,
    -- PRD.md §6.5: the collection library is not the source; record both.
    provider_library         TEXT NOT NULL,
    provider_library_version TEXT NOT NULL,
    underlying_source        TEXT NOT NULL,
    source_ref               TEXT,
    retrieved_at             TEXT NOT NULL,
    created_at               TEXT NOT NULL,
    updated_at               TEXT NOT NULL,
    UNIQUE (symbol, observation_date),
    CHECK (close IS NOT NULL OR value IS NOT NULL),
    CHECK (quality_status IN ('OK', 'REVIEW', 'STALE', 'MISSING', 'CORRUPT'))
);

CREATE INDEX IF NOT EXISTS idx_observations_date
    ON market_observations (observation_date);
CREATE INDEX IF NOT EXISTS idx_observations_symbol_date
    ON market_observations (symbol, observation_date DESC);
CREATE INDEX IF NOT EXISTS idx_observations_quality
    ON market_observations (quality_status)
    WHERE quality_status <> 'OK';

-- ------------------------------------------------------- data_quality_findings
-- TASK-028. A cross-validation mismatch is recorded, never auto-corrected
-- (BACKTEST_SPEC.md §5.5).
CREATE TABLE IF NOT EXISTS data_quality_findings (
    id               INTEGER PRIMARY KEY AUTOINCREMENT,
    symbol           TEXT NOT NULL,
    observation_date TEXT NOT NULL,
    check_name       TEXT NOT NULL,
    status           TEXT NOT NULL,
    detail           TEXT NOT NULL,
    primary_value    REAL,
    reference_value  REAL,
    reference_source TEXT,
    resolved_at      TEXT,
    resolution_note  TEXT,
    created_at       TEXT NOT NULL,
    UNIQUE (symbol, observation_date, check_name),
    CHECK (status IN ('REVIEW', 'STALE', 'MISSING', 'CORRUPT'))
);

CREATE INDEX IF NOT EXISTS idx_findings_open
    ON data_quality_findings (symbol, observation_date)
    WHERE resolved_at IS NULL;

-- ------------------------------------------------------------ indicator_values
-- Raw indicator readings. `params_key` is the canonical JSON of the indicator
-- parameters, so RSI(14) and RSI(30) never collide.
CREATE TABLE IF NOT EXISTS indicator_values (
    id                    INTEGER PRIMARY KEY AUTOINCREMENT,
    indicator_name        TEXT NOT NULL,
    observation_date      TEXT NOT NULL,
    params_key            TEXT NOT NULL,
    value                 REAL,
    source_symbol         TEXT NOT NULL,
    availability_datetime TEXT,
    quality_status        TEXT NOT NULL DEFAULT 'OK',
    created_at            TEXT NOT NULL,
    updated_at            TEXT NOT NULL,
    UNIQUE (indicator_name, observation_date, params_key),
    CHECK (quality_status IN ('OK', 'REVIEW', 'STALE', 'MISSING', 'CORRUPT'))
);

CREATE INDEX IF NOT EXISTS idx_indicator_values_date
    ON indicator_values (observation_date);

-- ------------------------------------------------------------ indicator_scores
-- Normalized 0..100 scores. Scoped by strategy_version because the
-- normalization window is a strategy parameter.
CREATE TABLE IF NOT EXISTS indicator_scores (
    id                   INTEGER PRIMARY KEY AUTOINCREMENT,
    indicator_name       TEXT NOT NULL,
    observation_date     TEXT NOT NULL,
    strategy_version     TEXT NOT NULL,
    score                REAL,
    raw_value            REAL,
    normalization_method TEXT NOT NULL,
    normalization_window INTEGER,
    quality_status       TEXT NOT NULL DEFAULT 'OK',
    created_at           TEXT NOT NULL,
    updated_at           TEXT NOT NULL,
    UNIQUE (indicator_name, observation_date, strategy_version),
    CHECK (score IS NULL OR (score >= 0.0 AND score <= 100.0))
);

CREATE INDEX IF NOT EXISTS idx_indicator_scores_date
    ON indicator_scores (observation_date, strategy_version);

-- ---------------------------------------------------------------- market_states
CREATE TABLE IF NOT EXISTS market_states (
    id                  INTEGER PRIMARY KEY AUTOINCREMENT,
    observation_date    TEXT NOT NULL,
    strategy_version    TEXT NOT NULL,
    composite_score     REAL,
    regime              TEXT NOT NULL,
    raw_regime          TEXT,
    previous_regime     TEXT,
    previous_score      REAL,
    target_leverage     REAL,
    data_quality_status TEXT NOT NULL DEFAULT 'OK',
    reason_codes        TEXT NOT NULL DEFAULT '[]',
    score_breakdown     TEXT NOT NULL DEFAULT '{}',
    data_version        TEXT,
    parameter_version   TEXT,
    code_commit         TEXT,
    created_at          TEXT NOT NULL,
    updated_at          TEXT NOT NULL,
    UNIQUE (observation_date, strategy_version),
    CHECK (composite_score IS NULL OR (composite_score >= 0.0 AND composite_score <= 100.0)),
    -- ARCHITECTURE.md §15: an UNKNOWN regime never carries investment advice.
    CHECK (regime <> 'UNKNOWN' OR target_leverage IS NULL)
);

CREATE INDEX IF NOT EXISTS idx_market_states_date
    ON market_states (observation_date DESC);

-- ----------------------------------------------------------- target_allocations
CREATE TABLE IF NOT EXISTS target_allocations (
    id               INTEGER PRIMARY KEY AUTOINCREMENT,
    market_state_id  INTEGER NOT NULL,
    observation_date TEXT NOT NULL,
    strategy_version TEXT NOT NULL,
    asset            TEXT NOT NULL,
    weight           REAL NOT NULL,
    created_at       TEXT NOT NULL,
    updated_at       TEXT NOT NULL,
    UNIQUE (observation_date, strategy_version, asset),
    FOREIGN KEY (market_state_id) REFERENCES market_states (id) ON DELETE CASCADE,
    CHECK (asset IN ('QQQ', 'QLD', 'TQQQ', 'CASH')),
    CHECK (weight >= 0.0 AND weight <= 1.0)
);

CREATE INDEX IF NOT EXISTS idx_allocations_date
    ON target_allocations (observation_date DESC);

-- ---------------------------------------------------------------- regime_events
CREATE TABLE IF NOT EXISTS regime_events (
    id               INTEGER PRIMARY KEY AUTOINCREMENT,
    event_date       TEXT NOT NULL,
    strategy_version TEXT NOT NULL,
    previous_regime  TEXT,
    new_regime       TEXT NOT NULL,
    previous_score   REAL,
    new_score        REAL,
    reason_codes     TEXT NOT NULL DEFAULT '[]',
    created_at       TEXT NOT NULL,
    UNIQUE (event_date, strategy_version),
    CHECK (previous_regime IS NULL OR previous_regime <> new_regime)
);

CREATE INDEX IF NOT EXISTS idx_regime_events_date
    ON regime_events (event_date DESC);

-- ----------------------------------------------------------------- alert_events
-- `dedupe_key` = event_date | event_type | strategy_version. The UNIQUE
-- constraint is what makes a re-run physically unable to re-notify.
CREATE TABLE IF NOT EXISTS alert_events (
    id               INTEGER PRIMARY KEY AUTOINCREMENT,
    dedupe_key       TEXT NOT NULL UNIQUE,
    event_date       TEXT NOT NULL,
    event_type       TEXT NOT NULL,
    strategy_version TEXT NOT NULL,
    severity         TEXT NOT NULL DEFAULT 'INFO',
    title            TEXT NOT NULL,
    body             TEXT NOT NULL,
    payload          TEXT NOT NULL DEFAULT '{}',
    delivery_status  TEXT NOT NULL DEFAULT 'PENDING',
    provider         TEXT,
    error            TEXT,
    sent_at          TEXT,
    created_at       TEXT NOT NULL,
    updated_at       TEXT NOT NULL,
    CHECK (delivery_status IN ('PENDING', 'SENT', 'FAILED', 'SUPPRESSED')),
    CHECK (severity IN ('INFO', 'WARNING', 'CRITICAL'))
);

CREATE INDEX IF NOT EXISTS idx_alert_events_date
    ON alert_events (event_date DESC, event_type);
CREATE INDEX IF NOT EXISTS idx_alert_events_pending
    ON alert_events (created_at)
    WHERE delivery_status = 'PENDING';

-- --------------------------------------------------------------- pipeline_runs
CREATE TABLE IF NOT EXISTS pipeline_runs (
    id                INTEGER PRIMARY KEY AUTOINCREMENT,
    run_id            TEXT NOT NULL UNIQUE,
    run_date          TEXT NOT NULL,
    status            TEXT NOT NULL,
    stage             TEXT,
    started_at        TEXT NOT NULL,
    finished_at       TEXT,
    error_message     TEXT,
    strategy_version  TEXT,
    data_version      TEXT,
    parameter_version TEXT,
    code_commit       TEXT,
    created_at        TEXT NOT NULL,
    updated_at        TEXT NOT NULL,
    CHECK (status IN ('SUCCESS', 'FAILED', 'SKIPPED', 'DATA_FAILURE'))
);

CREATE INDEX IF NOT EXISTS idx_pipeline_runs_date
    ON pipeline_runs (run_date DESC, started_at DESC);

-- ----------------------------------------------------------- strategy_versions
CREATE TABLE IF NOT EXISTS strategy_versions (
    id                INTEGER PRIMARY KEY AUTOINCREMENT,
    strategy_version  TEXT NOT NULL UNIQUE,
    parameter_status  TEXT NOT NULL,
    parameter_version TEXT,
    data_version      TEXT,
    frozen_at         TEXT,
    code_commit       TEXT,
    manifest          TEXT NOT NULL DEFAULT '{}',
    is_active         INTEGER NOT NULL DEFAULT 0,
    created_at        TEXT NOT NULL,
    updated_at        TEXT NOT NULL,
    CHECK (parameter_status IN
        ('RESEARCH', 'RESEARCH_PLACEHOLDER', 'VALIDATION', 'FROZEN')),
    CHECK (is_active IN (0, 1)),
    -- A FROZEN version must say when it was frozen (TASK-101).
    CHECK (parameter_status <> 'FROZEN' OR frozen_at IS NOT NULL)
);

-- At most one active strategy at a time.
CREATE UNIQUE INDEX IF NOT EXISTS idx_strategy_versions_single_active
    ON strategy_versions (is_active)
    WHERE is_active = 1;
