#!/usr/bin/env bash
set -Eeuo pipefail

ROOT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)"
cd "$ROOT_DIR"
PYTHON="${BASSWIESN_PYTHON:-python3}"
[[ -x .venv/bin/python ]] && PYTHON=.venv/bin/python
# The release suite is the complete software suite. The hardware marker is
# excluded explicitly so this command cannot address real radios.
exec "$PYTHON" -m pytest -m "not hardware" -q --tb=short
