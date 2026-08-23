#!/usr/bin/env bash
set -Eeuo pipefail

ROOT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)"
cd "$ROOT_DIR"
PYTHON="${BASSWIESN_PYTHON:-python3}"
[[ -x .venv/bin/python ]] && PYTHON=.venv/bin/python
exec "$PYTHON" -m pytest -m browser -q --tb=short
