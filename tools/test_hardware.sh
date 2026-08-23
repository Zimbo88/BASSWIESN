#!/usr/bin/env bash
set -Eeuo pipefail

ROOT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)"
cd "$ROOT_DIR"

read_env_value() {
  local key="$1"
  [[ -f .env ]] || return 0
  awk -F= -v wanted="$key" '$1 == wanted {sub(/^[^=]*=/, ""); print; exit}' .env | tr -d '\r'
}

# Do not source .env: it is configuration data, not executable shell code.
# Hardware tests nevertheless require the same central protection policy as
# the running installation, so copy only the two literal list values when the
# caller has not already exported them.
export PROTECTED_DEVICE_IPS="${PROTECTED_DEVICE_IPS:-$(read_env_value PROTECTED_DEVICE_IPS)}"
export PROTECTED_DEVICE_IDS="${PROTECTED_DEVICE_IDS:-$(read_env_value PROTECTED_DEVICE_IDS)}"

if [[ -z "$PROTECTED_DEVICE_IPS" || -z "$PROTECTED_DEVICE_IDS" ]]; then
  echo "Hardware tests blocked: configure protected device IPs and IDs first." >&2
  exit 2
fi

if [[ "${BASSWIESN_HARDWARE_CONFIRM:-}" != "BASSWIESN HARDWARE TEST" ]]; then
  cat >&2 <<'EOF'
Hardware tests were safely blocked; no network access occurred.
Run them only on the explicitly authorized home network with:
  BASSWIESN_HARDWARE_CONFIRM='BASSWIESN HARDWARE TEST' make test-hardware
Configured protected IP and device-ID guards remain active.
EOF
  exit 2
fi

PYTHON="${BASSWIESN_PYTHON:-python3}"
[[ -x .venv/bin/python ]] && PYTHON=.venv/bin/python
exec "$PYTHON" -m pytest -m hardware -q --tb=short
