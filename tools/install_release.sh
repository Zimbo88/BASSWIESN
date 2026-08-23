#!/usr/bin/env bash
set -Eeuo pipefail
trap 'echo "Installation fehlgeschlagen (Zeile $LINENO). Prüfe die Meldung oben und: docker compose logs --tail=100" >&2' ERR

ROOT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
cd "$ROOT_DIR"

command -v docker >/dev/null 2>&1 || { echo "Docker fehlt. Installiere Docker Engine und starte das Skript erneut." >&2; exit 1; }
docker compose version >/dev/null 2>&1 || { echo "Docker Compose v2 fehlt. Erwartet wird: docker compose ..." >&2; exit 1; }
docker info >/dev/null 2>&1 || { echo "Docker-Daemon ist nicht erreichbar. Starte Docker oder prüfe die Benutzerrechte." >&2; exit 1; }

mkdir -p data data/logs data/backups data/media
if [[ ! -f .env ]]; then
  cp .env.example .env
  echo "Created .env from .env.example"
fi

docker compose pull --ignore-buildable || echo "Kein fertiges Image verfügbar; BASSWIESN wird lokal gebaut."
DOCKER_BUILDKIT=0 docker compose build --pull --no-cache
docker compose up -d --force-recreate

HOST_IP="$(hostname -I 2>/dev/null | awk '{print $1}')"
HOST_IP="${HOST_IP:-127.0.0.1}"
echo "BASSWIESN is starting: http://${HOST_IP}:1328"
echo "Logs: docker compose logs -f"
echo "Masterlog: tail -f data/logs/master.log"
