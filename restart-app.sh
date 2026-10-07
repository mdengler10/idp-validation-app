#!/bin/bash
# Stop any running IDP Validation app and start a fresh one.
set -e
cd "$(dirname "$0")"

echo "Checking for processes on port 8000..."
PIDS=$(lsof -ti :8000 2>/dev/null || true)
if [ -n "$PIDS" ]; then
  echo "Stopping existing process(es): $PIDS"
  echo "$PIDS" | xargs kill -9 2>/dev/null || true
  sleep 2
fi

if lsof -i :8000 >/dev/null 2>&1; then
  echo "Port 8000 still in use. Manually stop the process:"
  echo "  lsof -i :8000"
  echo "  kill -9 <PID>"
  exit 1
fi

echo "Starting app..."
echo "Logs: $(dirname "$0")/data/logs/idp-validation.log"
echo "Verbose IDP traces: IDP_LOG_LEVEL=DEBUG ./restart-app.sh"
source .venv/bin/activate
export IDP_CLIENT_ID="${IDP_CLIENT_ID:-1fc1acdb959d44d98f021e0fdcdcc263}"
export IDP_CLIENT_SECRET="${IDP_CLIENT_SECRET:-668a6D2441f94Ced877fBBA9ad8e39a1}"
exec uvicorn app.main:app --reload --host 127.0.0.1 --port 8000
