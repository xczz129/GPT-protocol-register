#!/bin/bash
set -euo pipefail

ROOT="/root/full/gpt-register-fp"
HOST="${WEB_HOST:-0.0.0.0}"
PORT="${WEB_PORT:-8787}"
LOG="/tmp/gpt-reg-web.log"
PIDFILE="/tmp/gpt-reg-web.pid"
UNIT="gpt-register-fp-8787.service"
FORCE_RESTART="${FORCE_RESTART:-0}"

health_ok() {
  curl -fsS --max-time 3 "http://127.0.0.1:${PORT}/api/health" >/dev/null 2>&1
}

wait_health() {
  local deadline=$((SECONDS + ${1:-20}))
  while (( SECONDS < deadline )); do
    if health_ok; then
      return 0
    fi
    sleep 0.5
  done
  return 1
}

# Prefer systemd unit so only one process owns :8787.  Do not restart a healthy
# service by default: bouncing the upstream is the common cause of Bad Gateway.
if systemctl list-unit-files "${UNIT}" >/dev/null 2>&1; then
  if [[ "${FORCE_RESTART}" != "1" ]] && systemctl is-active --quiet "${UNIT}" && health_ok; then
    echo "healthy: ${UNIT} http://127.0.0.1:${PORT}"
    systemctl --no-pager --full status "${UNIT}" | head -15 || true
    exit 0
  fi

  if [[ "${FORCE_RESTART}" == "1" ]]; then
    echo "force restarting ${UNIT}..."
    systemctl restart "${UNIT}"
  else
    echo "starting ${UNIT}..."
    systemctl reset-failed "${UNIT}" 2>/dev/null || true
    systemctl start "${UNIT}"
  fi

  if wait_health 60; then
    echo "ready: http://127.0.0.1:${PORT}"
    systemctl --no-pager --full status "${UNIT}" | head -15 || true
    exit 0
  fi
  echo "ERROR: ${UNIT} did not become healthy" >&2
  systemctl --no-pager --full status "${UNIT}" || true
  journalctl -u "${UNIT}" -n 80 --no-pager || true
  exit 1
fi

cd "$ROOT"

PYTHON_BIN="${PYTHON_BIN:-}"
if [[ -z "${PYTHON_BIN}" ]]; then
  if [[ -x "$ROOT/.venv/bin/python" ]]; then
    PYTHON_BIN="$ROOT/.venv/bin/python"
  else
    PYTHON_BIN="python3"
  fi
fi

# Fallback non-systemd mode: only kill stale/non-responsive owner when health fails.
if health_ok; then
  echo "healthy: http://127.0.0.1:${PORT}"
  exit 0
fi

if [[ -f "$PIDFILE" ]]; then
  old_pid="$(cat "$PIDFILE" 2>/dev/null || true)"
  if [[ -n "${old_pid}" ]] && ps -p "${old_pid}" >/dev/null 2>&1; then
    echo "stopping stale pid=${old_pid}"
    kill "${old_pid}" 2>/dev/null || true
    sleep 2
    kill -9 "${old_pid}" 2>/dev/null || true
  fi
fi
fuser -k "${PORT}/tcp" >/dev/null 2>&1 || true
sleep 0.5
nohup "$PYTHON_BIN" -u cli.py web --host "$HOST" --port "$PORT" >>"$LOG" 2>&1 &
echo $! > "$PIDFILE"
if wait_health 60; then
  echo "started pid=$! http://${HOST}:${PORT}"
else
  echo "ERROR: fallback web did not become healthy; log=${LOG}" >&2
  tail -80 "$LOG" || true
  exit 1
fi
