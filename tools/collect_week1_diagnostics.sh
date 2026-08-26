#!/usr/bin/env bash
set -Eeuo pipefail

ROOT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)"
cd "$ROOT_DIR"

STAMP="$(date +%Y%m%d-%H%M%S)"
NAME="basswiesn-week1-diagnostics-$STAMP"
TMP_ROOT="$(mktemp -d)"
STAGE="$TMP_ROOT/$NAME"
ARCHIVE="dist/$NAME.tar.gz"
cleanup() { rm -rf "$TMP_ROOT"; }
trap cleanup EXIT

mkdir -p "$STAGE/data/logs" "$STAGE/data/live-tests" "$STAGE/system" dist

[[ -f data/logs/master.log ]] && cp data/logs/master.log "$STAGE/data/logs/"
for file in data/*.db; do
  [[ -f "$file" ]] && cp "$file" "$STAGE/data/"
done
for file in data/live-tests/*.json; do
  [[ -f "$file" ]] && cp "$file" "$STAGE/data/live-tests/"
done
[[ -f .env ]] && cp .env "$STAGE/"
cp docker-compose.yml "$STAGE/"

capture() {
  local output="$1"
  shift
  "$@" > "$STAGE/system/$output" 2>&1 || true
}

capture docker-compose-logs.txt docker compose logs --no-color
capture free.txt free -h
capture meminfo.txt cat /proc/meminfo
capture df.txt df -h
capture uptime.txt uptime
if command -v vcgencmd >/dev/null 2>&1; then
  capture temperature.txt vcgencmd measure_temp
else
  printf '%s\n' 'vcgencmd is not available on this host' > "$STAGE/system/temperature.txt"
fi

tar -czf "$ARCHIVE" -C "$TMP_ROOT" "$NAME"
echo "$ARCHIVE"
