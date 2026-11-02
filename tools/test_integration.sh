#!/usr/bin/env bash
set -Eeuo pipefail

ROOT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)"
cd "$ROOT_DIR"
PYTHON="${BASSWIESN_PYTHON:-python3}"
if [[ -z "${BASSWIESN_PYTHON:-}" && -x .venv/bin/python ]]; then
  PYTHON=.venv/bin/python
fi
exec "$PYTHON" -m pytest -m "integration and not browser and not slow and not hardware" -q --tb=short
