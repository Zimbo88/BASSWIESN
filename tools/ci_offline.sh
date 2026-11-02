#!/usr/bin/env bash
set -Eeuo pipefail
# A second boundary around Python mocks: browser/subprocess traffic cannot leave
# a fresh network namespace. Only GitHub-hosted Linux runners use this wrapper.
# Local tests retain their existing socket guard and never require sudo.
[[ "${GITHUB_ACTIONS:-}" == true && $# -gt 0 ]] || {
  echo 'ci_offline.sh is restricted to explicitly running CI jobs.' >&2; exit 2;
}
ci_test_user="$(id -un)"
ci_python="$(command -v python)"
sudo unshare --net -- bash -c '
  ip link set lo up
  ci_user="$1"
  shift
  exec sudo --user "$ci_user" --set-home -- env "$@"
' -- "$ci_test_user" "PATH=$PATH" "BASSWIESN_PYTHON=$ci_python" "$@"
