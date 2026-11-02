#!/usr/bin/env bash
set -Eeuo pipefail

# The archive is public material; its generated files must have canonical modes
# regardless of the caller's umask. Private output/staging roots remain 0700.
# In particular manifest/checksum/data entries are created after source copying.
umask 022

ROOT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)"
cd "$ROOT_DIR"

PYTHON="${BASSWIESN_PYTHON:-python3}"
if [[ -z "${BASSWIESN_PYTHON:-}" && -x .venv/bin/python ]]; then
  PYTHON=.venv/bin/python
fi
SOURCE_DATE_EPOCH="${SOURCE_DATE_EPOCH:-0}"
DIST="$ROOT_DIR/dist"
REPRO_DIST=""
if (( $# != 0 )); then
  [[ ( $# == 2 || $# == 4 ) && "$1" == "--output-dir" && "$2" == /* ]] || {
    echo "Usage: package_release.sh [--output-dir NEW_ABSOLUTE_DIRECTORY [--repro-output-dir SECOND_NEW_ABSOLUTE_DIRECTORY]]" >&2
    exit 2
  }
  DIST="$2"
  if (( $# == 4 )); then
    [[ "$3" == "--repro-output-dir" && "$4" == /* ]] || exit 2
    REPRO_DIST="$4"
  fi
fi
# Reserve a NEW directory before doing work. Never overwrite a previous release
# or accept a symlink/ambiguous output path. Parent must already exist.
OUTPUTS=("$DIST")
[[ -z "$REPRO_DIST" ]] || OUTPUTS+=("$REPRO_DIST")
for output in "${OUTPUTS[@]}"; do
  [[ "$output" != */../* && "$output" != */./* && "$output" != */.. && "$output" != */. ]] || exit 2
  if ! mkdir -m 700 -- "$output"; then
    echo "Release output must be a new directory; existing artifacts were not changed." >&2
    exit 1
  fi
done
VERSION="$("$PYTHON" - <<'PY'
from basswiesn import __version__
print(__version__)
PY
)"
STAGE_ROOT="$(mktemp -d)"
CLEANUP_ROOT="$STAGE_ROOT"
ARCHIVE="$STAGE_ROOT/basswiesn-docker-release-${VERSION}.tar.gz"
STAGE="$STAGE_ROOT/basswiesn-release"
BASE_ITEMS=(basswiesn Dockerfile docker-compose.yml requirements.txt requirements.in requirements.lock README.md FEATURES.md SETUP_READ_HERE.md RELEASE_CHECKLIST.md LICENSE THIRD_PARTY_NOTICES.md .env.example install.sh .dockerignore CHANGELOG.md)
PUBLIC_TOOLS=(tools/check_version.py tools/run_dev.py tools/pi_observer.py tools/install_observer.py tools/check_update_host.py tools/prepare_update_host.py tools/prepare_update_service.py tools/install_update_helper.py tools/configure_update_env.py tools/verify_release_archive.py tools/audit_languages.js)
PUBLIC_DOCS=(docs/releases/3.0.0/RELEASE_NOTES_3.0.0.md docs/releases/3.0.1/RELEASE_NOTES_3.0.1.md docs/releases/3.0.2/RELEASE_NOTES_3.0.2.md docs/PI_OBSERVER.md docs/update-helper.md docs/dlna-library.md docs/airplay-bridge.md docs/lab-workbench.md docs/languages-and-help.md docs/testing.md docs/DEPENDENCIES.md docs/SECURITY_CHECKS.md docs/SECURITY_MODEL.md docs/ENGINEERING_BASELINE.md docs/SETTINGS_TRANSACTIONS.md docs/ADMIN_AUTH.md docs/adr/0001-administration-boundaries.md)
PUBLIC_DOCS+=(docs/UPDATER_AUTH.md docs/API_TOKENS.md)
PUBLIC_TOOLS+=(tools/basswiesnctl)
PUBLIC_TOOLS+=(tools/build_ssh_client.py tools/check_ssh_provenance.py tools/openssh-source.json)
PUBLIC_TOOLS+=(tools/probe_migrations.py)
PUBLIC_DOCS+=(docs/CLI.md docs/PROVIDER_ARCHITECTURE.md docs/OBSERVABILITY.md)
PUBLIC_DOCS+=(docs/DEVICE_TRANSACTIONS.md docs/adr/0002-device-transaction-ownership.md)
PUBLIC_DOCS+=(docs/DATABASE_SAFETY.md docs/ACCEPTANCE.md docs/PERFORMANCE.md)
PUBLIC_DOCS+=(docs/RADIO_PATCHES.md)
PUBLIC_DOCS+=(docs/releases/5.0.0/RELEASE_NOTES_5.0.0.md)
PUBLIC_DOCS+=(modules/airplay-bridge/README.md)
trap 'rm -rf "$CLEANUP_ROOT"' EXIT

"$PYTHON" tools/check_version.py --artifact "$ARCHIVE"
"$PYTHON" tools/check_dependency_lock.py
"$PYTHON" tools/generate_phase4a_contracts.py --check
"$PYTHON" -m compileall -q basswiesn tests tools
TEST_LOG="$STAGE_ROOT/pytest-release.log"
TEST_SUMMARY="$STAGE_ROOT/release-test-summary.json"
# The epoch belongs to the source archive, not runtime snapshots tested below.
# Python 3.14 zipfile honors this variable, including a pre-1980 epoch which
# cannot fit ZIP headers. Exercise the runtime clock without weakening tests;
# retain the requested epoch for manifest/tar/gzip generation after this gate.
# Stop on the first failure so fixture cascades cannot hide its original trace.
# Successful runs still execute every selected software test.
if ! env -u SOURCE_DATE_EPOCH "$PYTHON" -m pytest -m "not hardware" -q --maxfail=1 --tb=short >"$TEST_LOG" 2>&1; then
  # Keep the first failure outside disposable staging, but never in the public
  # archive. This private log is essential for resuming a long release run.
  install -m 600 "$TEST_LOG" "$DIST/failed-software-gate.log"
  cat "$TEST_LOG"
  exit 1
fi
cat "$TEST_LOG"
TEST_SUMMARY="$TEST_SUMMARY" TEST_LOG="$TEST_LOG" BASSWIESN_EFFECTIVE_PYTHON="$PYTHON" "$PYTHON" - <<'PY'
import json
import os
import re
from pathlib import Path

text = Path(os.environ["TEST_LOG"]).read_text(encoding="utf-8", errors="replace")
summary = {}
for key, label in (("passed", "passed"), ("failed", "failed"), ("skipped", "skipped"), ("xfailed", "xfailed"), ("xpassed", "xpassed")):
    match = re.search(rf"(\d+) {label}", text)
    summary[key] = int(match.group(1)) if match else 0
summary["command"] = "python -m pytest -m 'not hardware' -q"
summary["software_only"] = True
Path(os.environ["TEST_SUMMARY"]).write_text(json.dumps(summary, indent=2, sort_keys=True) + "\n", encoding="utf-8")
PY
bash tools/check_js.sh
if command -v node >/dev/null 2>&1; then
  node --check basswiesn/app/static/app.js
  node --check basswiesn/app/static/remote.js
  node --check basswiesn/app/static/reboots.js
  node --check basswiesn/app/static/js/translations.js
  node --check basswiesn/app/static/js/lab-workbench.js
  node --check basswiesn/app/static/js/locale-301.js
  node --check basswiesn/app/static/js/locale-302.js
  node --check basswiesn/app/static/js/accessibility.js
  node --check basswiesn/app/static/js/lab-learning.js
  node --check basswiesn/app/static/js/help-content.js
  node --check basswiesn/app/static/js/help.js
  node tools/audit_languages.js --check
else
  echo "node not found; JavaScript release gate cannot run" >&2
  exit 1
fi

# Both independent staging passes consume the same successful, mandatory test
# gate. There is no skip-tests flag or externally supplied PASS receipt.
build_archive() {
local DIST="$1"
local STAGE_ROOT="$CLEANUP_ROOT/stage-$2"
local STAGE="$STAGE_ROOT/basswiesn-release"
local ARCHIVE="$STAGE_ROOT/basswiesn-docker-release-${VERSION}.tar.gz"
mkdir -p "$STAGE"
for item in "${BASE_ITEMS[@]}"; do
  [[ -e "$item" ]] || { echo "Missing release input: $item" >&2; exit 1; }
  cp -a "$item" "$STAGE/"
done
for item in "${PUBLIC_TOOLS[@]}" "${PUBLIC_DOCS[@]}"; do
  [[ -e "$item" ]] || { echo "Missing public release input: $item" >&2; exit 1; }
  mkdir -p "$STAGE/$(dirname "$item")"
  cp -a "$item" "$STAGE/$item"
done
chmod 755 "$STAGE/install.sh"
# Stable release metadata must not inherit a developer's group-writable umask.
find "$STAGE" -type d -exec chmod go-w {} +
find "$STAGE" -type f -exec chmod go-w {} +
mkdir -p "$STAGE/data"
cp "$TEST_SUMMARY" "$STAGE/release-test-summary.json"
find "$STAGE" -type d -name __pycache__ -prune -exec rm -rf {} +
find "$STAGE" -type f \( -name '*.pyc' -o -name '*.pyo' -o -name '*.log' \) -delete
find "$STAGE" -type f \( -name '*.db' -o -name '*.sqlite' -o -name '*.sqlite3' \) -delete
find "$STAGE" -type f \( \
  -name 'basswiesn_forensics_bundle.tar.gz' -o \
  -name 'basswiesn_dump.tar.gz' -o \
  -name 'master_tail.log' -o \
  -name 'devices.json' -o \
  -name 'health.json' -o \
  -name 'basswiesn_db_dump.sql' -o \
  -name 'monitor_alerts_tail.log' \
\) -delete
rm -f "$STAGE/docs/PRIVATE_RPI_INSTALL.md"
find "$STAGE/docs" -type f -name '*_extracted.txt' -delete
NOTE_GLOB_A="pro""mpt"
NOTE_GLOB_B="se""ssion"
NOTE_GLOB_C="re""sume"
find "$STAGE/docs" -type f \( -iname "*${NOTE_GLOB_A}*" -o -iname "*${NOTE_GLOB_B}*" -o -iname "*${NOTE_GLOB_C}*" \) -delete
rm -f "$STAGE/tools/package_private_rpi.sh"
find "$STAGE" -type f \( -name '*.odt' -o -name '*.tar' -o -name '*.tar.gz' -o -name '*.tar.xz' -o -name '*.zip' -o -name '*.7z' \) -delete
if grep -RIEq '(^|[^0-9A-Fa-f])[0-9A-Fa-f]{12}([^0-9A-Fa-f]|$)|/home/[^/[:space:]]+' "$STAGE"; then
  echo "Release staging contains installation-specific hardware or filesystem data" >&2
  exit 1
fi

"$PYTHON" tools/check_package_links.py "$STAGE"

STAGE="$STAGE" SOURCE_DATE_EPOCH="$SOURCE_DATE_EPOCH" "$PYTHON" - <<'PY'
import hashlib, json, os
from pathlib import Path
from basswiesn import __version__
root = Path(os.environ["STAGE"])
(root / ".dockerignore").write_text(
    (root / ".dockerignore").read_text(encoding="utf-8") + f"# release-context {__version__}\n",
    encoding="utf-8",
)
files = []
for path in sorted(
    p for p in root.rglob("*")
    if p.is_file() and p.name not in {"manifest.json", "SHA256SUMS"}
):
    files.append({"path": path.relative_to(root).as_posix(), "sha256": hashlib.sha256(path.read_bytes()).hexdigest(), "size": path.stat().st_size})
(root / "manifest.json").write_text(json.dumps({"format": 1, "version": __version__, "source_date_epoch": int(os.environ["SOURCE_DATE_EPOCH"]), "files": files}, indent=2) + "\n", encoding="utf-8")
manifest = json.loads((root / "manifest.json").read_text(encoding="utf-8"))
assert manifest["format"] == 1 and manifest["version"] == __version__ and manifest["files"]
assert all(len(item["sha256"]) == 64 and item["size"] >= 0 for item in manifest["files"])
checksum_lines = []
for path in sorted(p for p in root.rglob("*") if p.is_file() and p.name != "SHA256SUMS"):
    checksum_lines.append(f"{hashlib.sha256(path.read_bytes()).hexdigest()}  {path.relative_to(root).as_posix()}")
(root / "SHA256SUMS").write_text("\n".join(checksum_lines) + "\n", encoding="utf-8")
PY

tar --format=gnu --sort=name --mtime="@$SOURCE_DATE_EPOCH" --owner=0 --group=0 --numeric-owner -cf - -C "$STAGE_ROOT" basswiesn-release | gzip -n > "$ARCHIVE"
# Verify before placing any package in the reserved output directory.
(cd "$STAGE_ROOT" && sha256sum "$(basename "$ARCHIVE")" > SHA256SUMS)
"$PYTHON" tools/verify_release_archive.py "$ARCHIVE" --version "$VERSION" --checksums "$STAGE_ROOT/SHA256SUMS"
cp -n "$ARCHIVE" "$DIST/$(basename "$ARCHIVE")"
ln -s "$(basename "$ARCHIVE")" "$DIST/basswiesn-docker-release.tar.gz"
cp "$STAGE/manifest.json" "$DIST/manifest.json"
cp "$STAGE/release-test-summary.json" "$DIST/release-test-summary.json"
cp "$STAGE/install.sh" "$DIST/install.sh"
cp "$STAGE/docker-compose.yml" "$DIST/docker-compose.yml"
cp "$STAGE/.env.example" "$DIST/.env.example"
(cd "$DIST" && sha256sum "$(basename "$ARCHIVE")" basswiesn-docker-release.tar.gz manifest.json release-test-summary.json install.sh docker-compose.yml .env.example > SHA256SUMS.txt)
# The public GitHub release uploads only the versioned archive and this file;
# keep its copy-paste verification contract limited to the downloadable asset.
(cd "$DIST" && sha256sum "$(basename "$ARCHIVE")" > SHA256SUMS)
# Verify the complete artifact without extracting or executing its contents.
# This is an integrity/layout gate, not the separate publication/privacy gate.
"$PYTHON" tools/verify_release_archive.py "$ARCHIVE" --version "$VERSION" --checksums "$DIST/SHA256SUMS"
echo "$DIST/$(basename "$ARCHIVE")"
}

build_archive "$DIST" first
if [[ -n "$REPRO_DIST" ]]; then
  build_archive "$REPRO_DIST" second
  cmp "$DIST/basswiesn-docker-release.tar.gz" "$REPRO_DIST/basswiesn-docker-release.tar.gz"
  echo 'PASS: independently staged archives are byte-identical; mandatory software gate ran once'
fi
