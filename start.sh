#!/usr/bin/env bash
set -euo pipefail

SCRIPT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
VENV="$SCRIPT_DIR/.venv"
LOG_DIR="$SCRIPT_DIR/logs"
LOG_FILE="$LOG_DIR/agent.log"
PID_FILE="$LOG_DIR/agent.pid"

cd "$SCRIPT_DIR"

# --------------------------------------------------------------------------
# Commands
# --------------------------------------------------------------------------
case "${1:-start}" in

  start)
    if [[ -f "$PID_FILE" ]] && kill -0 "$(cat "$PID_FILE")" 2>/dev/null; then
      echo "Already running (pid $(cat "$PID_FILE")). Use: $0 stop"
      exit 0
    fi

    [[ -d "$VENV" ]] || { echo "Virtual env not found. Run: python3 -m venv .venv && .venv/bin/pip install -r requirements.txt"; exit 1; }
    mkdir -p "$LOG_DIR"

    nohup "$VENV/bin/python" main.py >> "$LOG_FILE" 2>&1 &
    echo $! > "$PID_FILE"
    echo "Started (pid $!)"
    echo "Logs : tail -f $LOG_FILE"
    echo "Stop : $0 stop"
    ;;

  stop)
    if [[ ! -f "$PID_FILE" ]]; then
      echo "Not running (no pid file)."
      exit 0
    fi
    PID=$(cat "$PID_FILE")
    if kill -0 "$PID" 2>/dev/null; then
      kill "$PID"
      rm -f "$PID_FILE"
      echo "Stopped (pid $PID)"
    else
      echo "Process $PID not found, cleaning up pid file."
      rm -f "$PID_FILE"
    fi
    ;;

  restart)
    "$0" stop
    sleep 1
    "$0" start
    ;;

  status)
    if [[ -f "$PID_FILE" ]] && kill -0 "$(cat "$PID_FILE")" 2>/dev/null; then
      echo "Running (pid $(cat "$PID_FILE"))"
    else
      echo "Not running"
    fi
    ;;

  logs)
    tail -f "$LOG_FILE"
    ;;

  *)
    echo "Usage: $0 {start|stop|restart|status|logs}"
    exit 1
    ;;
esac
