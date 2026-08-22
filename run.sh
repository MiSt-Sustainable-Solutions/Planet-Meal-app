#!/usr/bin/env bash
# Start both halves for local development.
#   ./run.sh          both
#   ./run.sh app      just this app (assumes the catalogue is already running)
set -euo pipefail
HERE="$(cd "$(dirname "$0")" && pwd)"
CATALOGUE="$HERE/../MiSt Tool Mrigank/catalogue/src"

start_catalogue() {
  if [ ! -d "$CATALOGUE" ]; then
    echo "catalogue repo not found at $CATALOGUE — start it yourself and run: ./run.sh app"
    exit 1
  fi
  echo "starting the catalogue API on :8077"
  ( cd "$CATALOGUE" && python -m uvicorn api:app --port 8077 ) &
  for _ in $(seq 1 60); do
    curl -sf http://127.0.0.1:8077/health >/dev/null && break
    sleep 1
  done
}

[ "${1:-both}" = "both" ] && start_catalogue
echo "starting the app on :8080  ->  http://127.0.0.1:8080"
cd "$HERE/app" && python -m uvicorn main:app --port 8080 --reload
