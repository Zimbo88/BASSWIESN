#!/usr/bin/env bash
set -Eeuo pipefail
cd "$(dirname "${BASH_SOURCE[0]}")/.."
command -v node >/dev/null || { echo 'Node.js is required for the JavaScript gate.' >&2; exit 1; }
while IFS= read -r -d '' script; do
  node --check "$script"
done < <(find basswiesn/app/static tools -type f \( -name '*.js' -o -name '*.mjs' \) -print0)
