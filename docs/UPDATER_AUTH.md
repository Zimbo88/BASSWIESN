# Authentication across update and rollback

Status: local software implementation; not a deployment or hardware acceptance.
The authenticated hardened-LAN profile remains a preview. The limited
[automation-token contract](API_TOKENS.md) is software implemented; wider grants,
device-source/resource policies and broader authenticated workflows remain
separate work. No radio communication is involved in these checks.

## Three distinct authorities

| Authority | Where it is checked | What it permits |
|---|---|---|
| Human administrator session + CSRF/Origin | Web/cloud/debug administration boundary | Allowed human operations, still subject to each device safety gate |
| Host updater peer/capability authorization | Existing narrow UNIX-socket helper | One explicitly authorized, journalled installation transaction |
| Runtime probe capability | Loopback `GET /ready` only | Read a small attestation of the running worker; no administrative action |

None is a replacement for another. In particular, possession of the probe value
does not log a user in, start an update, authorize a radio write or bypass the
protected-device policy. The application container remains non-root, read-only,
without Linux capabilities or a Docker-socket mount.

The previous probes used detailed human readiness endpoints. With required-mode
authentication that path correctly failed closed. The solution is **not** to
make those endpoints public again or manufacture an administrator session.

## Verifying a real worker

The supported launcher creates a fresh random 256-bit capability under
`/tmp/basswiesn-runtime/` before workers start. The directory is runtime-UID-owned
0700; the single-link regular file is 0600. Readers reject symlinks, unexpected
owners, permissions and malformed contents. Requests never create or repair
the capability. It is outside the persistent data volume and backup generations.

An ordinary `/ready` request still returns only the public readiness boolean.
Its extended response requires the exact capability header **and** an actual
loopback peer. Browser Origin/Fetch-Metadata requests are refused, forwarded
headers do not establish loopback trust, and the response is `no-store`. The
capability is never returned to callers, placed in a URL, or used as a browser
session. The header is named as a token so shared redaction recognizes it if a
future diagnostic path accidentally collects it; intentional collection remains
forbidden.

The host runs a fixed Python probe inside the exact inspected container as its
non-root runtime user. That probe reads the local capability and queries all
three actual listeners using literal loopback destinations. Proxies and HTTP
redirects are disabled, responses are bounded, and no retry can replay a write.
Each response must match the expected listener, running version, auth mode and
validation mode. Storage/database readiness must be true. An isolated candidate
must report zero owned background tasks on **every** listener, not merely an
empty task registry in a separately imported Python interpreter.

The probe then checks SQLite integrity and the presence of all modeled tables
in the actual local database. Candidate results contain a schema digest and
table count; the host still validates the receipt, exact image/container
identity, process signature and absence of restarts/OOM. A boolean HTTP success
alone cannot pass this contract.

For older releases without the new module, the fixed legacy probe is retained
only when configuration is explicitly legacy (or predates the auth setting).
A present but broken new module, bad token, false attestation or required-mode
configuration must **never** trigger legacy fallback. Compatibility is not
permission to bypass an authentication failure.

These authorization and worker-readiness decisions use explicit runtime checks,
not Python assertions that disappear under `-O` or `-OO`. The supported host
invokes `python -I -B`, without optimization, but the generated compatibility
probe must remain fail-closed independently of that invocation detail. Tests
compile both the dispatch wrapper and its legacy body at all three optimization
levels and inject failed listeners and wrong modes. Passing this contract is not
permission to accept a missing modern probe on a required-auth installation.

Direct ad-hoc Uvicorn launches do not initialize this launcher capability and
are not supported updater runtimes. The existing image contract requires
`python tools/run_dev.py`; no deployment shell or arbitrary request command was
added.

## Credential continuity before rollback

The installation updater intentionally snapshots complete stopped data, unlike
ordinary application-database backups. That includes administrator credentials
and session rows. A blind restore could therefore revive an old password or a
revoked cookie.

Rollback now has two independent controls:

1. Before any reverse filesystem exchange, compare current credentials with the
   preserved original. The check recognizes only the current explicit auth
   schema and reads both databases through bounded private copies. Main files
   and WAL state are considered together. SQLite reader bookkeeping cannot
   modify either retained generation. File identity/metadata are checked around
   copying. Unknown schema, changed account/password hash, corruption, unsafe
   links or missing credentials on one side stop automatic rollback. API-token
   digests, scopes, expiry, revocation and additions also belong to this check;
   token usage timestamps alone do not change authority.
2. Before launching any HTTP worker, revoke all stored sessions and record an
   `ADMIN_RUNTIME_RESTART` event. This occurs on normal restarts too. A restored
   generation therefore cannot silently restore a browser cookie's authority.
   Passwords and login rate budgets are not reset.

Authentication activity such as logout, session touching and constant-code
audit entries does not itself change credential continuity. A password rehash
or reset does: even a benign hash change is conservatively treated as requiring
manual recovery rather than guessing equivalence without the password.

If continuity cannot be proved, both application runtimes remain stopped and
the transaction reports `MANUAL_ACTION_REQUIRED`. Current and original data
remain retained. Availability is deliberately secondary to avoiding credential
rollback. Operators must not run independent local credential writers during
the stopped-writer update window. The host administrator/runtime UID remain
trusted; this is not protection against a compromised host administrator.

The continuity reader supports the original four-table schema version 0 and
the five-table token schema version 1. A schema transition, even an additive
one, is conservatively different authority and requires manual recovery after
a failed update. Future credential schemas must extend this contract and its
tests before automatic rollback can accept them. Ordinary radio/configuration
snapshots still do not contain the auth DB.

## Reproduction without devices

Use the project's locked development environment:

```sh
python -m pytest -q tests/test_runtime_probe.py tests/test_update_auth_boundary.py tests/test_auth_credential_continuity.py
python -m pytest -q tests/test_update_candidate_runtime_300.py tests/test_update_host_executor_300.py tests/test_update_installation_cutover_300.py
docker build -t basswiesn:local-verification .
bash tools/test_container_offline.sh basswiesn:local-verification legacy live
bash tools/test_container_offline.sh basswiesn:local-verification required live
bash tools/test_container_offline.sh basswiesn:local-verification required validation
```

Container tests have no external network, published ports, installation-data
mounts or host sockets. HTTP permission is confined to their loopback fixtures.
Credentials are generated inside the disposable container and never printed.
The probes executed there are the actual host-generated candidate/health
commands. Fixtures also exercise lost replies, partial cutovers, failed health,
changed passwords, unknown auth schemas, WAL-only changes and startup ordering.

These tests cannot certify a real installation upgrade, rollback, Pi resource
budget, TLS deployment or hardware behavior. Existing supply-chain and container
vulnerability gates still apply independently.
