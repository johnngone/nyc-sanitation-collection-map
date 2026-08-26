#!/bin/sh
# One-container runtime: serve the map while the refresh scheduler builds the
# first audited release and performs later refreshes in the background.
set -eu

refresh_pid=""
if [ "${DATA_REFRESH_ENABLED:-true}" = "true" ]; then
  python scripts/run_scheduler.py &
  refresh_pid="$!"
fi

uvicorn backend.app.main:app --host 0.0.0.0 --port 8000 --no-access-log &
app_pid="$!"

shutdown() {
  trap - INT TERM
  kill -TERM "$app_pid" 2>/dev/null || true
  if [ -n "$refresh_pid" ]; then
    kill -TERM "$refresh_pid" 2>/dev/null || true
  fi
  wait "$app_pid" 2>/dev/null || true
  if [ -n "$refresh_pid" ]; then
    wait "$refresh_pid" 2>/dev/null || true
  fi
  exit 0
}

trap shutdown INT TERM

if wait "$app_pid"; then
  app_status=0
else
  app_status="$?"
fi
if [ -n "$refresh_pid" ]; then
  kill -TERM "$refresh_pid" 2>/dev/null || true
  wait "$refresh_pid" 2>/dev/null || true
fi
exit "$app_status"
