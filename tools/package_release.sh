#!/usr/bin/env bash
set -Eeuo pipefail

ROOT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)"
cd "$ROOT_DIR"

PYTHON="${BASSWIESN_PYTHON:-python3}"
[[ -x .venv/bin/python ]] && PYTHON=.venv/bin/python
SOURCE_DATE_EPOCH="${SOURCE_DATE_EPOCH:-0}"
DIST="$ROOT_DIR/dist"
VERSION="$("$PYTHON" - <<'PY'
from basswiesn import __version__
print(__version__)
PY
)"
ARCHIVE="$DIST/basswiesn-docker-release-${VERSION}.tar.gz"
STAGE_ROOT="$(mktemp -d)"
STAGE="$STAGE_ROOT/basswiesn-release"
BASE_ITEMS=(basswiesn Dockerfile docker-compose.yml requirements.txt README.md FEATURES.md SETUP_READ_HERE.md RELEASE_CHECKLIST.md LICENSE .env.example install.sh .dockerignore CHANGELOG.md)
PUBLIC_TOOLS=(tools/run_dev.py)
PUBLIC_DOCS=(docs/releases/2.5.0/RELEASE_NOTES_2.5.0.md)
trap 'rm -rf "$STAGE_ROOT"' EXIT

"$PYTHON" -m compileall -q basswiesn tests tools
TEST_LOG="$STAGE_ROOT/pytest-release.log"
TEST_SUMMARY="$STAGE_ROOT/release-test-summary.json"
if ! "$PYTHON" -m pytest -m "not hardware" -q >"$TEST_LOG" 2>&1; then
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
summary["command"] = f"{Path(os.environ['BASSWIESN_EFFECTIVE_PYTHON']).as_posix()} -m pytest -m 'not hardware' -q"
summary["software_only"] = True
Path(os.environ["TEST_SUMMARY"]).write_text(json.dumps(summary, indent=2, sort_keys=True) + "\n", encoding="utf-8")
PY
if command -v node >/dev/null 2>&1; then
  node --check basswiesn/app/static/app.js
  node --check basswiesn/app/static/js/translations.js
else
  echo "node not found; skipping JavaScript syntax checks" >&2
fi

mkdir -p "$DIST" "$STAGE"
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

rm -f "$ARCHIVE" "$DIST/basswiesn-docker-release.tar.gz" "$DIST/manifest.json" "$DIST/release-test-summary.json" "$DIST/SHA256SUMS.txt" "$DIST/SHA256SUMS"
tar --sort=name --mtime="@$SOURCE_DATE_EPOCH" --owner=0 --group=0 --numeric-owner -cf - -C "$STAGE_ROOT" basswiesn-release | gzip -n > "$ARCHIVE"
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
echo "$ARCHIVE"
