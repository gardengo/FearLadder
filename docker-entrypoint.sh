#!/usr/bin/env bash
# TASK-150 — one image, several jobs.
set -euo pipefail

case "${1:-dashboard}" in
  daily)
    shift
    exec python scripts/daily_runner.py "$@"
    ;;
  backtest)
    shift
    exec python scripts/backtest.py "$@"
    ;;
  dashboard)
    shift
    exec streamlit run app/streamlit_app.py \
      --server.address=0.0.0.0 \
      --server.port="${PORT:-8501}" \
      --server.headless=true \
      --browser.gatherUsageStats=false "$@"
    ;;
  shell)
    shift
    exec /bin/bash "$@"
    ;;
  *)
    # Anything else is run verbatim, so `docker run ... python -c ...` works.
    exec "$@"
    ;;
esac
