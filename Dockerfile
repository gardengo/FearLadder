# TASK-150 — application container.
#
# One image serves both entry points, because they are the same application
# reading the same database:
#
#   docker run --rm regimepilot daily          # the worker
#   docker run --rm -p 8501:8501 regimepilot dashboard
#
# The image is *not* how production runs. GitHub Actions runs the daily worker
# and Streamlit Cloud serves the dashboard (ARCHITECTURE.md §2); this exists for
# local development and for anyone who would rather not install Python 3.12.

FROM python:3.12-slim AS base

ENV PYTHONUNBUFFERED=1 \
    PYTHONDONTWRITEBYTECODE=1 \
    PYTHONUTF8=1 \
    PIP_NO_CACHE_DIR=1 \
    PIP_DISABLE_PIP_VERSION_CHECK=1

# tzdata: availability timestamps are computed against America/New_York, and a
# slim image ships no timezone database. Without it every availability check
# would silently shift by an hour across DST.
RUN apt-get update \
    && apt-get install -y --no-install-recommends tzdata \
    && rm -rf /var/lib/apt/lists/*

WORKDIR /app

# ---------------------------------------------------------------- dependencies
# Copied first so a source-only change does not reinstall the world.
FROM base AS deps
COPY pyproject.toml README.md ./
COPY src/regime_monitor/__init__.py src/regime_monitor/__init__.py
RUN pip install --upgrade pip && pip install ".[dashboard]"

# --------------------------------------------------------------------- runtime
FROM base AS runtime

COPY --from=deps /usr/local/lib/python3.12/site-packages /usr/local/lib/python3.12/site-packages
COPY --from=deps /usr/local/bin /usr/local/bin

COPY pyproject.toml README.md ./
COPY src/ src/
COPY config/ config/
COPY scripts/ scripts/
COPY app/ app/

RUN pip install --no-deps -e .

# The database is a mounted volume, not baked in: it is state, and it is
# committed to git by the workflow rather than shipped in an image.
VOLUME ["/app/data"]
RUN mkdir -p /app/data /app/reports/backtest

# Never run as root. The container only ever needs to read config and
# read/write one SQLite file.
RUN useradd --create-home --uid 10001 regime \
    && chown -R regime:regime /app
USER regime

ENV REGIME_MONITOR_DB=/app/data/regime_monitor.db \
    PYTHONPATH=/app/src

EXPOSE 8501

HEALTHCHECK --interval=60s --timeout=10s --start-period=20s --retries=3 \
    CMD python -c "from regime_monitor.config.loader import load_config; load_config()" || exit 1

COPY --chown=regime:regime docker-entrypoint.sh /usr/local/bin/docker-entrypoint.sh
ENTRYPOINT ["/usr/local/bin/docker-entrypoint.sh"]
CMD ["dashboard"]
