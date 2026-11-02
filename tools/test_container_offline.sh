#!/usr/bin/env bash
set -Eeuo pipefail
cd "$(dirname "${BASH_SOURCE[0]}")/.."
[[ $# -ge 1 && $# -le 3 ]] || { echo 'Usage: test_container_offline.sh LOCAL_IMAGE [legacy|required] [live|validation]' >&2; exit 2; }
test_image="$1"
emulation="${BASSWIESN_TEST_EMULATED:-0}"
[[ "$emulation" == 0 || "$emulation" == 1 ]] || exit 2
health_options=(--health-interval=1s --health-start-period=5s --health-retries=10)
attempt_limit=45
if [[ "$emulation" == 1 ]]; then
  # QEMU can spend >8s importing Python before the real loopback probe runs.
  # This explicit test-only allowance never changes the production image or
  # native CI deadline and cannot certify physical-Pi performance.
  [[ "$(docker image inspect --format '{{.Architecture}}' "$test_image")" == arm64 ]] || exit 2
  [[ "$(docker info --format '{{.Architecture}}')" =~ ^(x86_64|amd64)$ ]] || exit 2
  health_options+=(--health-timeout=30s)
  attempt_limit=120
  echo 'EMULATED ARM64: test-only health timeout 30s; native startup deadline NOT VERIFIED'
fi
auth_mode="${2:-legacy}"
[[ "$auth_mode" == legacy || "$auth_mode" == required ]] || exit 2
runtime_mode="${3:-live}"
[[ "$runtime_mode" == live || "$runtime_mode" == validation ]] || exit 2
validation_mode=0
[[ "$runtime_mode" != validation ]] || validation_mode=1
[[ "$(docker image inspect --format '{{.Config.User}}' "$test_image")" == basswiesn ]] || exit 1
expected_version="$(python3 -c 'from basswiesn import __version__; print(__version__)')"
[[ "$(docker image inspect --format '{{index .Config.Labels "org.opencontainers.image.version"}}' "$test_image")" == "$expected_version" ]] || exit 1
# No published ports, host binds, host sockets or external networking. All data
# belongs to this disposable test container, never an installation or radio.
test_container="$(docker create --network none --read-only --cap-drop ALL \
  --security-opt no-new-privileges:true \
  --tmpfs /tmp:rw,noexec,nosuid,size=64m \
  --tmpfs /app/data:rw,noexec,nosuid,size=128m,uid=10001,gid=10001 \
  --env "BASSWIESN_ADMIN_AUTH_MODE=$auth_mode" \
  --env BASSWIESN_ADMIN_ORIGINS=http://127.0.0.1:1328 \
  --env BASSWIESN_ADMIN_ALLOW_HTTP=true \
  --env BASSWIESN_DEVICE_ALLOWED_PEERS=127.0.0.1 \
  --env "BASSWIESN_UPDATE_VALIDATION_MODE=$validation_mode" \
  "${health_options[@]}" "$test_image")"
[[ "$test_container" =~ ^[a-f0-9]{64}$ ]] || exit 1
trap 'docker rm -f "$test_container" >/dev/null' EXIT
docker start "$test_container" >/dev/null
for ((attempt=1; attempt<=attempt_limit; attempt++)); do
  health="$(docker inspect --format '{{.State.Health.Status}}' "$test_container")"
  if [[ "$health" == healthy ]]; then
    docker exec "$test_container" python -c 'import importlib.util; from basswiesn import __version__; assert importlib.util.find_spec("pytest") is None; assert importlib.util.find_spec("pip") is None; print("PASS: isolated non-root runtime without test tools or package installer", __version__)'
    docker exec -i "$test_container" python - <<'PY'
import subprocess
from datetime import datetime, timedelta
from pathlib import Path
from zoneinfo import ZoneInfo
release = dict(line.split('=', 1) for line in Path('/etc/os-release').read_text().splitlines() if '=' in line)
distribution = release.get('ID', '').strip('"')
if distribution == 'debian':
    version = subprocess.check_output(
        ['dpkg-query', '-W', '-f=${Version}', 'libpcre2-8-0'], text=True, timeout=5,
    ).strip()
    subprocess.run(['dpkg', '--compare-versions', version, 'ge', '10.46-1~deb13u3'],
                   check=True, timeout=5)
    print('PASS: PCRE2 stable security update present', version)
elif distribution == 'alpine':
    assert release.get('VERSION_ID', '').strip('"').startswith('3.24.'), 'Unreviewed Alpine family'
    # This runtime does not need PCRE2. Its absence is not an exception for an
    # installed vulnerable library; the independent raw image scan still gates.
    assert not list(Path('/usr/lib').glob('libpcre2-*.so*')), 'Reassess added PCRE2 dependency'
    assert not list(Path('/lib').glob('libpcre2-*.so*')), 'Reassess added PCRE2 dependency'
    print('PASS: Alpine 3.24 runtime; PCRE2 not installed')
else:
    raise AssertionError('Unreviewed runtime distribution')
# The image serves clock/alarm features. A smaller base must not silently lose
# the IANA database or change daylight-saving behavior.
berlin = ZoneInfo('Europe/Berlin')
assert datetime(2026, 1, 1, tzinfo=berlin).utcoffset() == timedelta(hours=1)
assert datetime(2026, 7, 1, tzinfo=berlin).utcoffset() == timedelta(hours=2)
print('PASS: runtime timezone data and seasonal offsets')
PY
    # -G prints effective configuration without connecting. -F /dev/null avoids
    # host configuration hooks; the container has no external network anyway.
    # A newer base must still accept our existing firmware compatibility options.
    docker exec -i "$test_container" python - <<'PY'
import subprocess
import hashlib
import json
from pathlib import Path
lock = json.loads(Path('/app/tools/openssh-source.json').read_text())
manifest = json.loads(Path('/usr/local/share/doc/openssh/build-manifest.json').read_text())
assert manifest['source'] == lock and manifest['signature_verified'] is True
assert manifest['version'] == lock['version']
for name, expected in manifest['files'].items():
    path = Path('/usr/local') / name
    assert hashlib.sha256(path.read_bytes()).hexdigest() == expected
version = subprocess.run(['ssh', '-V'], capture_output=True, text=True, timeout=5, check=True)
assert 'OpenSSH_' + lock['version'] + ',' in version.stderr
assert not Path('/usr/sbin/sshd').exists()
print('PASS: pinned authentic OpenSSH client and installed binary hashes', lock['version'])
PY
    docker exec -i "$test_container" python - <<'PY'
import subprocess
from basswiesn.app.adapters.ssh import build_legacy_ssh_command
command = build_legacy_ssh_command("192.0.2.42", "root", "true")
result = subprocess.run(
    [command[0], "-G", "-F", "/dev/null", "-o", "CanonicalizeHostname=no", *command[1:]],
    capture_output=True, text=True, timeout=5, check=False,
)
assert result.returncode == 0, "Runtime SSH rejects legacy compatibility options"
for algorithm in ("ssh-rsa", "diffie-hellman-group1-sha1", "aes128-cbc"):
    assert algorithm in result.stdout, "Required legacy SSH algorithm is unavailable"
print("PASS: legacy SSH configuration parses offline; hardware acceptance NOT RUN")
PY
    if [[ "$auth_mode" == required ]]; then
      # HTTP is deliberately confined to this network-none container's loopback.
      # Generated credentials are never output, supplied as argv or persisted on
      # the host. Exercise the real locked runtime, not a test dependency stack.
      docker exec -i "$test_container" python - <<'PY'
import http.cookiejar
import json
from pathlib import Path
import secrets
import urllib.error
import urllib.request
from basswiesn.app.core.admin_store import AdminStore

base = "http://127.0.0.1:1328"
opener = urllib.request.build_opener(urllib.request.ProxyHandler({}), urllib.request.HTTPCookieProcessor(http.cookiejar.CookieJar()))
def request(path, body=None, csrf=None):
    headers = {"Origin": base, "Content-Type": "application/json"}
    if csrf:
        headers["X-BASSWIESN-CSRF"] = csrf
    req = urllib.request.Request(base + path, data=None if body is None else json.dumps(body).encode(), headers=headers)
    try:
        with opener.open(req, timeout=5) as response:
            return response.status, json.load(response)
    except urllib.error.HTTPError as error:
        return error.code, None

assert request("/api/devices")[0] == 401
password = secrets.token_urlsafe(32)
AdminStore(Path("data/secrets/admin/auth.sqlite3")).bootstrap("offline-smoke", password)
status, login = request("/auth/login", {"username": "offline-smoke", "password": password})
assert status == 200
assert request("/api/devices")[0] == 200
assert request("/auth/logout", {})[0] == 403
assert request("/auth/logout", {}, login["csrf_token"])[0] == 200
assert request("/api/devices")[0] == 401
print("PASS: isolated required-mode bootstrap/login/CSRF/logout; no radio contacted")
PY
    fi
    docker exec -i "$test_container" python - <<'PY'
from basswiesn.app.config import get_settings
from basswiesn.update_helper.candidate_runtime import PROBE
from basswiesn.update_helper.host_executor import HEALTH_PROBE
# Execute the actual host-generated probe, not a substitute test implementation.
# Output is only the bounded version/schema receipt, never credentials.
exec(PROBE if get_settings().update_validation_mode else HEALTH_PROBE)
print("PASS: actual updater runtime attestation; no admin route exemption")
PY
    exit 0
  fi
  if [[ "$health" == unhealthy ]]; then
    break
  fi
  sleep 1
done
docker inspect --format '{{json .State.Health}}' "$test_container"
docker logs --tail 60 "$test_container"
echo 'FAIL: isolated container did not become healthy' >&2
exit 1
