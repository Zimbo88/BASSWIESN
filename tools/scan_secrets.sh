#!/usr/bin/env bash
set -Eeuo pipefail
cd "$(dirname "${BASH_SOURCE[0]}")/.."
# The scanner is immutable and cannot reach the network while reading source.
# Pulling the public image happens in the Docker client, before container start.
scanner_image=zricethezav/gitleaks@sha256:691af3c7c5a48b16f187ce3446d5f194838f91238f27270ed36eef6359a574d9
git rev-parse --verify HEAD >/dev/null
scan_log="$(mktemp)"
trap 'rm -f "$scan_log"' EXIT
if ! docker run --rm --network none --read-only --cap-drop ALL \
  --security-opt no-new-privileges:true --user "$(id -u):$(id -g)" \
  --workdir /repo --mount "type=bind,source=$PWD,target=/repo,readonly" \
  "$scanner_image" git /repo --redact --no-banner --log-opts=--all --config /repo/.gitleaks.toml >"$scan_log" 2>&1; then
  cat "$scan_log"
  exit 1
fi
cat "$scan_log"
# Gitleaks can report zero leaks after git refused to read a repository. That
# is not a clean scan: require evidence that nonempty history was traversed.
grep -Eq '[1-9][0-9]* commits scanned\.' "$scan_log" || {
  echo 'FAIL: scanner did not prove that it read Git history' >&2; exit 1;
}
