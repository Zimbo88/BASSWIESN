#!/usr/bin/env bash
set -Eeuo pipefail

trap 'echo "Installation failed at line $LINENO. Check the message above and run: docker compose logs --tail=100" >&2' ERR

ROOT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
cd "$ROOT_DIR"

UPDATE_HELPER=0
if (( $# > 1 )); then
  echo "Usage: ./install.sh [--enable-updater | --no-updater]" >&2
  exit 2
fi
case "${1:-}" in
  --enable-updater) UPDATE_HELPER=1 ;;
  --no-updater|"") ;;
  *) echo "Usage: ./install.sh [--enable-updater | --no-updater]" >&2; exit 2 ;;
esac
if [[ $# == 0 && -t 0 ]]; then
  echo "Experimental LAB in-app updates need a restricted root host service, HTTPS and a private administrator code."
  echo "The service may stop this BASSWIESN container, back up its data and replace or restore this installation."
  read -r -p "Enable the update service? [y/N] " UPDATE_ANSWER
  [[ "$UPDATE_ANSWER" =~ ^[Yy]$ ]] && UPDATE_HELPER=1
fi
UPDATE_ADMIN=()

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

# Read-only preflight before any host enrollment, directory creation or build.
case "$(uname -m)" in
  x86_64|aarch64|arm64) ;;
  *) echo "This release requires a 64-bit x86 or ARM host and matching Docker platform." >&2; exit 1 ;;
esac
[[ -w "$ROOT_DIR" ]] || { echo "Installation directory is not writable." >&2; exit 1; }
[[ ! -L data && ! -L .env ]] || { echo "Refusing a symlinked data directory or .env." >&2; exit 1; }
[[ ! -e data || -d data ]] || { echo "data exists but is not a directory." >&2; exit 1; }
AVAILABLE_KIB="$(df -Pk "$ROOT_DIR" | awk 'NR==2 {print $4}')"
[[ "$AVAILABLE_KIB" =~ ^[0-9]+$ ]] && (( AVAILABLE_KIB >= 2097152 )) || {
  echo "At least 2 GiB free space is required before installation (large databases need more)." >&2
  exit 1
}

# Complete non-mutating prerequisites before creating a privileged host group,
# runtime directory or any application data. Missing Docker is not partial
# updater enrollment.
if (( UPDATE_HELPER == 1 )); then
  case "$ROOT_DIR/" in
    /tmp/*|/var/tmp/*|/run/*) echo "Install the updater from a persistent directory, not a temporary folder." >&2; exit 1 ;;
  esac
  /usr/bin/python3 -c 'import sys; assert sys.version_info >= (3,11)' || {
    echo "Host Python 3.11+ is required." >&2; exit 1;
  }
  # The trusted actor may own a home ancestor even when sudo created the final
  # directory as root. Never infer trust from arbitrary filesystem owner IDs.
  # Check the entire no-symlink path before any privileged bootstrap or write.
  UPDATE_OWNER="$(/usr/bin/python3 -c 'from pathlib import Path; from tools.install_update_helper import installation_owner; print(installation_owner(Path.cwd()))')" || {
    echo "Installation path is not owned by root and the invoking administrator. Use that administrator (with sudo when needed), or a root-owned persistent path." >&2
    exit 1
  }
  if (( EUID != 0 )); then UPDATE_ADMIN=(sudo); fi
  "${UPDATE_ADMIN[@]}" /usr/bin/python3 tools/install_update_helper.py bootstrap --approve-root-helper
fi

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
  data/update-disabled \
  data/secrets/setup-rebuild

CREATED_ENV=0
if [[ ! -f .env ]]; then
  cp .env.example .env
  CREATED_ENV=1
  echo "Created .env from .env.example"
fi
chmod 600 .env

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

if (( UPDATE_HELPER == 1 )); then
  /usr/bin/python3 tools/configure_update_env.py --enable-updater
fi

docker compose pull --ignore-buildable || echo "No prebuilt image available; BASSWIESN will be built locally."
DOCKER_BUILDKIT=0 docker compose build --pull --no-cache

# The application image runs as UID/GID 10001. Only prepare host-owned runtime
# directories. Never recursively widen private files or runtime-owned 0700/0600
# auth stores, migration snapshots and locks on an upgrade.
# The production service drops every capability. Grant only the three
# filesystem capabilities needed by this short-lived repair container; they
# are not added to the long-running BASSWIESN container.
if ! docker compose run --rm --no-deps --user 0 \
  --cap-add CHOWN --cap-add FOWNER --cap-add DAC_OVERRIDE \
  --entrypoint sh basswiesn -c '
  find /app/data -xdev -path /app/data/secrets -prune -o \
    -type d ! -user 10001 -exec chgrp 10001 {} +
  find /app/data -xdev -path /app/data/secrets -prune -o \
    -type d ! -user 10001 -exec chmod g+rwX,g-s {} +
  find /app/data -xdev -path /app/data/secrets -prune -o \
    -type d -perm /2000 -exec chmod g-s {} +
  # Allow creation of a private admin directory, without changing secret files.
  if test "$(stat -c %u /app/data/secrets)" != 10001; then
    chgrp 10001 /app/data/secrets
    chmod g+wx,g-s /app/data/secrets
  fi
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
  if test -e /app/data/basswiesn.db && ! test -w /app/data/basswiesn.db; then
    echo "Existing database is not writable by UID 10001. Stop the service, retain a backup and explicitly repair ownership; no private file permissions were widened." >&2
    exit 1
  fi
'
docker compose up -d --force-recreate

# Compose accepting the start request is not evidence that the application is
# usable. Verify the exact single service even when no privileged helper was
# requested. On uncertainty retain data/logs; never blindly rebuild or rollback.
INSTALL_CONTAINER="$(docker compose ps --all --quiet basswiesn)"
[[ "$INSTALL_CONTAINER" =~ ^[0-9a-f]{64}$ ]] || {
  echo "Expected exactly one BASSWIESN container after startup. No readiness confirmed." >&2
  exit 1
}
INSTALL_HEALTH="unknown"
for (( INSTALL_WAIT=0; INSTALL_WAIT<120; INSTALL_WAIT++ )); do
  if ! INSTALL_HEALTH="$(docker inspect --format '{{.State.Health.Status}}' "$INSTALL_CONTAINER")"; then
    INSTALL_HEALTH="unverified"
    break
  fi
  [[ "$INSTALL_HEALTH" == healthy ]] && break
  [[ "$INSTALL_HEALTH" == starting ]] || break
  sleep 1
done
[[ "$INSTALL_HEALTH" == healthy ]] || {
  echo "BASSWIESN startup was not confirmed healthy. Data and logs were retained; no automatic retry or rollback was performed." >&2
  echo "Inspect: docker compose logs --tail=100" >&2
  exit 1
}

if (( UPDATE_HELPER == 1 )); then
  # Exact single service identity; never choose from unrelated containers.
  UPDATE_CONTAINER="$INSTALL_CONTAINER"
  UPDATE_IMAGE="$(docker inspect --format '{{.Image}}' "$UPDATE_CONTAINER")"
  UPDATE_PROJECT="$(docker inspect --format '{{index .Config.Labels "com.docker.compose.project"}}' "$UPDATE_CONTAINER")"
  UPDATE_VERSION="$(docker image inspect --format '{{index .Config.Labels "org.opencontainers.image.version"}}' "$UPDATE_IMAGE")"
  UPDATE_BUNDLE_HASH="$(/usr/bin/python3 -c 'from pathlib import Path; from basswiesn.update_helper.deployment import build_bundle,sha; print(sha(build_bundle(Path.cwd())))')"
  "${UPDATE_ADMIN[@]}" /usr/bin/python3 tools/install_update_helper.py activate --approve-root-helper \
    --installation-root "$ROOT_DIR" --installation-owner-uid "$UPDATE_OWNER" \
    --compose-project "$UPDATE_PROJECT" --container-id "$UPDATE_CONTAINER" \
    --image-id "$UPDATE_IMAGE" --version "$UPDATE_VERSION" --approve-sha256 "$UPDATE_BUNDLE_HASH"
  echo "Update service ready. Switch to LAB; use HTTPS port 1329 and Settings > Updates."
  echo "Retrieve the private administrator code on this host with:"
  echo "  sudo /usr/bin/python3 tools/install_update_helper.py show-code --approve-root-helper"
  if [[ -t 0 && -t 1 ]]; then
    "${UPDATE_ADMIN[@]}" /usr/bin/python3 tools/install_update_helper.py show-code --approve-root-helper
  fi
  if [[ -f data/tls/basswiesn.crt ]]; then
    echo "Compare this certificate fingerprint before entering the code in your browser:"
    openssl x509 -in data/tls/basswiesn.crt -noout -fingerprint -sha256
  fi
fi

HOST_IP="$(awk -F= '/^BASSWIESN_LAN_HOST=/{print $2; exit}' .env | tr -d '[:space:]')"
if [[ -z "$HOST_IP" ]]; then
  HOST_IP="$(ip route get 192.0.2.1 2>/dev/null | awk '{for (i=1; i<=NF; i++) if ($i == "src") {print $(i+1); exit}}')"
fi
if [[ -z "$HOST_IP" ]]; then
  HOST_IP="127.0.0.1"
fi

echo
echo "BASSWIESN is ready:"
echo "  http://${HOST_IP}:1328"
echo
echo "Logs:"
echo "  docker compose logs -f"
echo
echo "Masterlog:"
echo "  tail -f data/logs/master.log"
