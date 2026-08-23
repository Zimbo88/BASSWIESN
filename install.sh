#!/usr/bin/env bash
set -Eeuo pipefail

trap 'echo "Installation failed at line $LINENO. Check the message above and run: docker compose logs --tail=100" >&2' ERR

ROOT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
cd "$ROOT_DIR"

command -v docker >/dev/null 2>&1 || {
  echo "Docker is missing. Install Docker Engine and run this script again." >&2
  exit 1
}

docker compose version >/dev/null 2>&1 || {
  echo "Docker Compose v2 is missing. Expected command: docker compose" >&2
  exit 1
}

docker info >/dev/null 2>&1 || {
  echo "Docker daemon is not reachable. Start Docker or check user permissions." >&2
  exit 1
}

is_usable_lan_ipv4() {
  local candidate="${1:-}" second_octet=""
  [[ "$candidate" =~ ^[0-9]+\.[0-9]+\.[0-9]+\.[0-9]+$ ]] || return 1
  case "$candidate" in
    10.*|192.168.*) return 0 ;;
    172.*)
      second_octet="${candidate#172.}"
      second_octet="${second_octet%%.*}"
      [[ "$second_octet" =~ ^[0-9]+$ ]] \
        && (( second_octet >= 16 && second_octet <= 31 ))
      return
      ;;
  esac
  return 1
}

is_physical_lan_interface() {
  local interface="${1:-}"
  case "$interface" in
    ""|lo|br-*|cni*|docker*|podman*|veth*|virbr*) return 1 ;;
  esac
  return 0
}

detect_lan_candidates() {
  local route_line="" route_interface="" route_host="" line="" interface="" host=""
  local -a candidates=()
  route_line="$(ip -o route get 192.0.2.1 2>/dev/null || true)"
  route_interface="$(awk '{for (i=1; i<=NF; i++) if ($i == "dev") {print $(i+1); exit}}' <<<"$route_line")"
  route_host="$(awk '{for (i=1; i<=NF; i++) if ($i == "src") {print $(i+1); exit}}' <<<"$route_line")"
  if is_physical_lan_interface "$route_interface" && is_usable_lan_ipv4 "$route_host"; then
    candidates+=("$route_host")
  fi
  while read -r line; do
    interface="$(awk '{print $2}' <<<"$line")"
    interface="${interface%%@*}"
    host="$(awk '{print $4}' <<<"$line")"
    host="${host%%/*}"
    is_physical_lan_interface "$interface" || continue
    is_usable_lan_ipv4 "$host" || continue
    [[ " ${candidates[*]} " == *" $host "* ]] || candidates+=("$host")
  done < <(ip -o -4 addr show scope global 2>/dev/null || true)
  if (( ${#candidates[@]} > 0 )); then
    printf '%s\n' "${candidates[@]}"
  fi
}

mkdir -p \
  data/logs \
  data/backups/system \
  data/media \
  data/uploads \
  data/support \
  data/tmp \
  data/diagnostics \
  data/setup-rebuild \
  data/secrets/setup-rebuild

CREATED_ENV=0
if [[ ! -f .env ]]; then
  cp .env.example .env
  CREATED_ENV=1
  echo "Created .env from .env.example"
fi

# A bridge-mode container cannot see the host's physical LAN interfaces. On a
# brand-new installation, persist the host-side candidates so Setup can offer
# the real radio-reachable address without requiring a hidden .env edit.
mapfile -t LAN_CANDIDATES < <(detect_lan_candidates)
if (( CREATED_ENV == 1 )) && (( ${#LAN_CANDIDATES[@]} > 0 )); then
  LAN_CANDIDATE_LIST="$(IFS=,; echo "${LAN_CANDIDATES[*]}")"
  {
    printf '\nBASSWIESN_LAN_HOST=%s\n' "${LAN_CANDIDATES[0]}"
    printf 'BASSWIESN_LAN_HOST_CANDIDATES=%s\n' "$LAN_CANDIDATE_LIST"
  } >> .env
  echo "Detected LAN server address: ${LAN_CANDIDATES[0]}"
fi

docker compose pull --ignore-buildable || echo "No prebuilt image available; BASSWIESN will be built locally."
DOCKER_BUILDKIT=0 docker compose build --pull --no-cache

# The application image runs as UID/GID 10001. Preserve existing host
# owners where possible, but make runtime paths writable through the image
# group. This also repairs root-owned directories from RC1 installations.
# The production service drops every capability. Grant only the three
# filesystem capabilities needed by this short-lived repair container; they
# are not added to the long-running BASSWIESN container.
if ! docker compose run --rm --no-deps --user 0 \
  --cap-add CHOWN --cap-add FOWNER --cap-add DAC_OVERRIDE \
  --entrypoint sh basswiesn -c '
  chgrp -R 10001 /app/data
  chmod -R g+rwX /app/data
  find /app/data -type d -exec chmod g+s {} +
'; then
  # Rootless Docker cannot always change ownership of a bind mount from the
  # image namespace. Use an explicit host ACL fallback for runtime data; the
  # secret directory remains read-only and is intentionally excluded.
  if command -v setfacl >/dev/null 2>&1; then
    RUNTIME_PATHS=(data/logs data/backups/system data/media data/uploads data/support data/tmp data/diagnostics data/setup-rebuild)
    for runtime_path in "${RUNTIME_PATHS[@]}"; do
      # Existing setup baselines may already belong to UID/GID 10001 and are
      # therefore not ACL-editable by the invoking host user. They already
      # have the required access; only repair the explicit host-owned roots
      # and their default ACLs for future files.
      setfacl -m u:10001:rwX "$runtime_path" 2>/dev/null || true
      find "$runtime_path" -type d -user "$(id -u)" -exec setfacl -m d:u:10001:rwX {} + 2>/dev/null || true
    done
  elif ! docker compose run --rm --no-deps --user 10001 --entrypoint sh basswiesn -c 'test -w /app/data && test -w /app/data/setup-rebuild'; then
    echo "Cannot repair UID/GID 10001 bind-mount permissions: chgrp failed, setfacl is unavailable, and runtime paths are not writable." >&2
    exit 1
  fi
fi
docker compose run --rm --no-deps --user 10001 --entrypoint sh basswiesn -c '
  for path in /app/data /app/data/logs /app/data/backups/system /app/data/media /app/data/uploads /app/data/support /app/data/tmp /app/data/diagnostics /app/data/setup-rebuild; do
    test -w "$path" || { echo "runtime path is not writable: $path" >&2; exit 1; }
  done
'
docker compose up -d --force-recreate

HOST_IP="$(awk -F= '/^BASSWIESN_LAN_HOST=/{print $2; exit}' .env | tr -d '[:space:]')"
if [[ -z "$HOST_IP" ]]; then
  HOST_IP="$(ip route get 192.0.2.1 2>/dev/null | awk '{for (i=1; i<=NF; i++) if ($i == "src") {print $(i+1); exit}}')"
fi
if [[ -z "$HOST_IP" ]]; then
  HOST_IP="127.0.0.1"
fi

echo
echo "BASSWIESN is starting:"
echo "  http://${HOST_IP}:1328"
echo
echo "Logs:"
echo "  docker compose logs -f"
echo
echo "Masterlog:"
echo "  tail -f data/logs/master.log"
