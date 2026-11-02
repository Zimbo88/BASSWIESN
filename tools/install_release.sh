#!/usr/bin/env bash
set -Eeuo pipefail

# Compatibility entry point only. A second installer had diverged from the
# canonical preflight, private-file permissions and explicit updater enrollment.
# Never fall back to that older, less restrictive installation path.
ROOT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)"
exec /bin/bash "$ROOT_DIR/install.sh" "$@"
