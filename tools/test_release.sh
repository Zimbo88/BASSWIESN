#!/usr/bin/env bash
set -Eeuo pipefail

ROOT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)"
cd "$ROOT_DIR"
PYTHON="${BASSWIESN_PYTHON:-python3}"
if [[ -z "${BASSWIESN_PYTHON:-}" && -x .venv/bin/python ]]; then
  PYTHON=.venv/bin/python
fi
# The release suite is the complete software suite. The hardware marker is
# excluded explicitly so this command cannot address real radios.
"$PYTHON" tools/generate_phase4a_contracts.py --check
exec "$PYTHON" -m pytest -m "not hardware" -q --tb=short --maxfail=1
