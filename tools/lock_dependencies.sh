#!/usr/bin/env bash
set -Eeuo pipefail
cd "$(dirname "${BASH_SOURCE[0]}")/.."
UV="${BASSWIESN_UV:-uv}"
[[ "$("$UV" --version)" =~ ^uv[[:space:]]0\.13\.4([[:space:]].*)?$ ]] || { echo 'Expected uv 0.13.4' >&2; exit 1; }
# Existing pins are preferred. Upgrades require an explicit named package.
args=()
if (( $# )); then
  [[ $# == 2 && "$1" == --upgrade-package ]] || { echo 'Usage: lock_dependencies.sh [--upgrade-package PACKAGE]' >&2; exit 2; }
  args=(--upgrade-package "$2")
fi
"$UV" pip compile requirements.in --universal --python-version 3.11 --generate-hashes --no-header --no-annotate -o requirements.lock "${args[@]}"
"$UV" pip compile requirements-dev.in --universal --python-version 3.11 --generate-hashes --no-header --no-annotate -o requirements-dev.lock "${args[@]}"
python3 tools/check_dependency_lock.py --record
