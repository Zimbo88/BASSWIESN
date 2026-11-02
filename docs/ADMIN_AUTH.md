# Local administrator authentication (engineering preview)

The optional `required` mode now protects human administration on **all three
application factories**, not just the dashboard. Existing installations remain
in `legacy` mode until an operator explicitly migrates. Legacy mode is **not
authenticated** and is unsuitable for an untrusted LAN or Internet exposure.

This is not yet a production hardened-LAN certificate: wider automation grants,
complete protocol-body authorization, comprehensive CSP and deployment
acceptance remain work in progress. Authentication does not validate a
radio operation, grant updater host capabilities, or move LAB features into
production.

[Scoped automation credentials](API_TOKENS.md) now provide a limited, explicit
web-API contract. They do not authorize the complete API, token administration
or the host updater. Browser sessions remain a separate authority.

The updater now uses a separate read-only runtime attestation instead of human
sessions or an exemption for detailed health endpoints. Version, listener,
authentication/validation mode, storage and database state are checked against
the actual workers. See [updater authentication boundaries](UPDATER_AUTH.md).
These are local software/container checks, not approval to deploy this preview
on a live installation. The ordinary container health check continues using the
minimal public `/ready` contract in required mode.

## Bootstrap and activation

Use the application runtime UID, the normal application directory and the
existing data mount. No account-creation endpoint is exposed on the network.
Do not pass a password as an argument, shell variable, environment value or
chat message. The command prompts twice on an interactive terminal:

```sh
docker compose exec basswiesn python -m basswiesn.admin bootstrap --username operator
```

For a source installation, activate the hash-locked Python environment and run:

```sh
python -m basswiesn.admin bootstrap --username operator
```

The account name is 1–64 ASCII letters/digits/dots/dashes/underscores, beginning
with a letter or digit. The password must be at least 15 characters and at most
1024 UTF-8 bytes. There is no default account/password. Repeating bootstrap
refuses to overwrite an existing administrator, including concurrent attempts.

Configure the administration origin explicitly in `.env`, then restart the
application through the normal installation procedure. For example, with a
locally configured hostname and trusted HTTPS certificate:

```dotenv
BASSWIESN_ADMIN_AUTH_MODE=required
BASSWIESN_ENABLE_HTTPS=true
BASSWIESN_ADMIN_ORIGINS=https://basswiesn.example.invalid:1329
BASSWIESN_ADMIN_ALLOW_HTTP=false
BASSWIESN_DEVICE_ALLOWED_PEERS=192.0.2.42,192.0.2.43
```

Replace the documentation hostname with the address actually used by the
browser and the documentation addresses with explicitly approved radio peers.
Multiple explicitly trusted origins can be comma-separated. A missing
or malformed origin configuration fails closed. Arbitrary Host or forwarded
headers do not establish a trusted origin. The supplied process launcher
disables proxy-header trust. Reverse-proxy deployments need a separate reviewed
TLS/listener configuration; do not blindly enable forwarded headers.

The `hardened-lan-preview` Compose profile forces `required` mode and enables the
existing HTTPS listener. Start only **one** profile/service using these ports.
Its account command uses `basswiesn-hardened-preview` instead of `basswiesn`.
The login page is `/auth/login`; opening the dashboard in a browser redirects
there if necessary. Invalid account/password responses do not identify which
value was wrong. The login page supports German and English, with English for
other languages. This is not a claim of 28 new native translations.

HTTP login is denied by default. `BASSWIESN_ADMIN_ALLOW_HTTP=true` is an explicit
compatibility/testing opt-in, **not encrypted transport**: anyone able to observe
that network may steal credentials or cookies. Software browser tests use it on
loopback only. Never present it as equivalent to HTTPS.

## Session, CSRF and request behavior

Authenticated application pages allow scripts only from the same origin and
block inline script, string-evaluation and inline event-handler attributes.
Login and token-management pages retain their stricter complete policy. The
optional FastAPI interactive documentation retains its earlier compatibility
policy because it uses an inline initializer and CDN assets. Image, style and
cross-listener connection restrictions remain a separate compatibility review;
this is not a claim of a complete restrictive CSP on every route. The legacy
unauthenticated mode is unchanged. Bose device-protocol responses do not inherit
the browser script policy.

- Argon2id password hashes use 64 MiB, three iterations and four lanes. Verify
  memory capacity on the deployment platform; this is not a Pi performance
  certification. Existing hashes are rehashed on a successful login when needed.
- A login issues an independent 256-bit opaque session value. Only its SHA-256
  digest is stored. An old cookie presented at successful login is revoked.
- The host-only browser-session cookie is `HttpOnly`, `SameSite=Strict`, path `/`,
  and `Secure` on actual HTTPS. It is never stored in localStorage/sessionStorage.
- Sessions have an eight-hour absolute lifetime and 30-minute inactivity limit.
  Successful authenticated requests, including UI polling, count as activity.
  Logout and local password reset revoke server-side sessions. Clock rollback
  behind the last observed use invalidates the affected session.
- The supported `tools/run_dev.py` launcher also revokes sessions **before any
  worker starts**, including after update or rollback. A normal application
  restart therefore requires login again. Passwords and rate budgets persist;
  restoring old session rows is not a way to restore browser access.
- Browser mutations require **both** an exact trusted same-origin `Origin` and
  the session-bound `X-BASSWIESN-CSRF` header. Login itself requires Origin;
  pre-login CSRF tokens are not used. The authenticated `/auth/session` response
  supplies the synchronizer token with `Cache-Control: no-store`.
- The shared browser transport keeps the CSRF token in memory, attaches it only
  to same-origin mutations, and never retries an uncertain write. Navigation
  cancels read requests, not writes already in flight. Expired credentials
  require a new login and an explicit user decision before another action.
- Login attempts are limited transactionally across workers: five per peer and
  twenty total per minute, including successful attempts. Forwarded IP headers
  are not a way to obtain another budget. Distributed LAN denial of service is
  still possible; local recovery is available.

Human errors return a bounded `error.code`/`message`, not submitted passwords or
validation traces. Authentication does not replace target identity, protection,
capability, previous-state, backup or readback checks in application services.

Credential-bearing JSON has a whole-body five-second deadline, not a timeout
reset by each incoming byte. Login accepts at most 8 KiB and token creation at
most 4 KiB. Ambiguous duplicate object keys, non-JSON numeric constants,
compressed bodies and conflicting or inaccurate lengths are rejected before
password verification or token creation. Disconnects produce generic errors;
cancellation is not swallowed. These request bounds complement the login rate
limit; they are not a claim of immunity to network-level denial of service.

## Listener and protocol boundaries

| Surface | Required-mode policy |
|---|---|
| Human web pages/API, OpenAPI/docs, unknown web paths | Administrator session; mutations also require Origin and CSRF |
| Exact reviewed Bose routes on web/cloud | No browser login; the effective first handler, path template and methods must match the registry; explicit peer allowlist and ingress quotas |
| Browser-originated mutations to Bose compatibility routes | Rejected; use the authenticated human API |
| Cloud `/api/cloud/stations`, cloud/debug docs, diagnostic requests | Protected, not anonymous exceptions merely because of their listener |
| Static assets, cloud media files and the existing tokenized DLNA relay | Device data plane retains existing resource checks; no administrator privilege |
| `/live`, `/ready` on every factory | Minimal process/database check, no household configuration or provider/radio probes |
| Capability-bearing loopback `GET /ready` | Separate runtime-local read-only attestation; cannot authorize human routes or updater actions |
| Unreviewed websockets | Rejected in required mode |
| Host updater UNIX socket | Existing independent peer/capability/artifact/rollback boundary remains |

The anonymous device contract registry is in `core/admin_protocols.py`. It is
not a prefix exemption. The first route selected by the locked framework is
checked, including included-router prefixes. A shadowing handler, administrative
alias or extra HTTP method cannot borrow a later anonymous route's permission.
The public health exceptions are likewise registered handler objects, not names.

In required mode, `BASSWIESN_DEVICE_ALLOWED_PEERS` must explicitly list literal
IPv4/IPv6 socket peers. Empty/invalid configuration fails closed; there is no
automatic discovery, DNS lookup, subnet wildcard or trust in forwarded headers.
Use reserved addresses and update the configuration deliberately when they change.
IPv4-mapped IPv6 addresses compare as the same IPv4 peer. This is network policy,
**not cryptographic device authentication**; address spoofing or a compromised
allowed radio remains a threat. A reverse proxy collapses peer identities unless
a separate reviewed isolation design preserves them; do not blindly allow its IP.

Identity-bound protocol requests require an unambiguous cached inventory match:
the claimed stable ID must select one row, and its canonical socket peer must
belong to that row alone. Two cached devices sharing an address are rejected,
including IPv4/IPv4-mapped IPv6 aliases. The ingress check performs no discovery
or live identity probe as a side effect of receiving a request.

Before a device handler runs, the boundary buffers at most 1 MiB within five
seconds. Headers are limited to 16 KiB and request target/query to 8 KiB; malformed
lengths and compressed bodies are rejected. A token bucket allows a burst of 120
requests and refills two requests per second per allowed peer. Four concurrent
requests per peer and 32 across the installation bound active work. Web/cloud
workers share rate reservations in SQLite and active-request slots in the private
`ingress-locks` directory. The slots use kernel ownership locks for the entire
request: failed/cancelled requests and exited processes release capacity without
resetting the persisted rate bucket. A backward wall-clock adjustment does not
refill tokens. A corrupt/unavailable quota store or unsafe lock directory fails
closed with `DEVICE_BUDGET_UNAVAILABLE`, before the protocol handler runs.

Workers must share the same local database and data directory, as in the supported
installation. These are host-local locks, not a distributed multi-host quota
service; separate database copies cannot enforce a common allowance. Peer keys
are hashed, but the quota rows remain private operational data, not anonymized
public evidence. These quotas do not replace listener/firewall resource limits.
These conservative defaults need real-radio compatibility acceptance before
required mode is recommended for production.

For routes with a `device_id` path parameter, the peer must also match exactly
one existing local inventory record. Missing/ambiguous identity or unavailable
inventory fails closed without probing a radio. Account-scoped Marge paths also
require exactly one inventory identity at that peer and an exact cached
`margeAccountUUID` match. An allowed radio cannot select another account merely
by changing the URL. The account preset collection is filtered to that account,
including in legacy mode; it never falls back to an unrelated recent radio.

The AddDevice callback additionally parses the exact root `deviceid` attribute
before its handler can update local state. Malformed/ambiguous XML, DTDs,
entities, unknown identities and mismatched peers/accounts are rejected. An
incoming callback cannot establish its own account authorization: factory-fresh
enrollment in required mode needs an administrator-established inventory/account
binding first. There is no permissive enrollment exception. This compatibility
boundary still requires physical setup acceptance before deployment.

The four LAB `/group` compatibility handlers have a separate reviewed contract
in required mode. The checked socket peer must map to one cached device and
account. A group leader must be that sender; members must be known,
unprotected identities in the same cached account. Account-local reads cannot
list another account's records, and only the original leader may update/delete.
Unknown or ambiguous inventory fails closed without discovery. Changing the
leader is not an implicit transfer of ownership.

Required-mode group bodies are bounded JSON objects with an explicit
`group_id` (or matching `id` alias), `leader` (or matching `master` alias),
and up to 32 unique `members`. Optional `source` and `status` are bounded
strings. Unknown fields and contradictory aliases are rejected. Update/delete
may supply the returned integer `expected_revision`; mismatches return 409.
All writes also compare the previously read stored value atomically, including
when this optional client revision is absent. Update never creates a missing
group, duplicate create never overwrites, and capacity is capped at 128 local
records by the insertion statement. A lost response is still uncertain; read
the local record before deciding on another operation.

These records use the existing local database, shared by web/cloud workers,
and survive a worker restart. Legacy mode retains its separate old in-memory
compatibility state; it gains no authentication from this work, and its state
is not imported as authorization. Required-mode responses carry
`state_only=true`, `hardware_verified=false` and `Cache-Control: no-store`.
They do **not** create a SoundTouch Zone, send radio commands, prove membership
or provide leader failover. Actual multiroom uses the explicit administration
transaction and distributed radio readback. Native group interoperability
remains LAB and requires physical acceptance.

These are **not** generic validation of every identifier inside every body.
Other provider-specific bodies still require separate review. Group members
must not be confused with the sender by a
recursive catch-all XML/JSON scanner.
Device-facing account/preset protocols retain existing application-state updates;
they never gain administrator authority. Static user media and tokenized relay
mounts retain their own data-plane checks, not these body quotas, and do not
become private through human-session authentication.

Cookies are not isolated by port. Device handlers never use an arriving admin
cookie as authority. Use trusted listeners and existing redaction; do not log
raw request cookies. Human cloud/debug access must use configured origins and
the session; there is no implicit cross-origin credential-sharing exception.

## Browser response policy

In required mode, human responses (including login failures and denied requests)
carry `no-store`, `nosniff`, `X-Frame-Options: DENY`, `Referrer-Policy: no-referrer`
and a Permissions Policy disabling camera, microphone, geolocation, payment and
USB. Login and token administration use a restrictive CSP with same-origin
external scripts/styles, no inline scripts, no eval, no frames and no base URL.
The token UI uses text nodes rather than HTML interpolation for grant metadata.

Older application views retain a transitional frame/object/base/form policy;
their inline handlers still need an incremental migration before a restrictive
script policy can be enforced. This is deliberately not called a complete CSP.
Device-protocol responses do not acquire a browser CSP or a new login gate.
HSTS is **not automatically enabled**, even on HTTPS: it applies to the whole
host across ports and can break this installation's separate HTTP listeners.
An operator may enable it only after reviewing the complete TLS deployment.

## Storage, recovery and backups

Credentials, session digests, rate budgets and the last 1,000 authentication
event codes live in `data/secrets/admin/auth.sqlite3`. The immediate directory
must be owned by the runtime UID with mode 0700; the single-link regular file
must be owned by it with mode 0600. Symlinked files/parent, permissive modes,
missing records and unreadable databases fail closed. The runtime UID and host
administrator are trusted; filesystem permissions cannot isolate a compromised
application from its own credentials.

The authentication database is deliberately **not** in ordinary application
database backups, support exports or radio snapshots. Restoring those snapshots
must not resurrect revoked sessions. Secure host-volume backups may contain
it and must remain private; restoring that credential store separately is a
host-administration operation. No request recreates a missing/corrupt store.

Full installation snapshots include this database. Before reversing the first
filesystem entry, the updater compares current and original credentials using
bounded private copies, including SQLite WAL state. Changed credentials,
missing state on one side or an unknown auth schema require manual recovery;
the updater must not revive an old password. Session/audit activity alone does
not block rollback. The launcher separately revokes restored sessions before
serving requests. The retained backup remains private and is not discarded.

Local recovery, with the known account name:

```sh
docker compose exec basswiesn python -m basswiesn.admin reset-password --username operator
```

This resets the password and invalidates all sessions. It neither resets radios
nor changes presets. Do not delete arbitrary data directories to recover login.
Application auth events contain constant codes/timestamps, not submitted
usernames, passwords, tokens or full client addresses.

The separate local Marge setup credential also uses create-once private-file
handling. It is created only during explicit setup, not startup or discovery.
Symlinked ancestors/files, hardlinks, non-regular files, wrong ownership,
non-0600 files and group/world-writable immediate directories fail closed.
Creation is serialized, flushed and published without replacing an existing
credential. Unsafe existing files are not silently repaired or rotated: an
administrator must inspect ownership/permissions and preserve the old value.
This does not extract or handle any radio/Apple private authentication material.

## Verification and references

`tests/test_admin_store.py` covers durable/session failure modes;
`tests/test_admin_boundary.py` exercises the real factories with loopback/mocks;
`tests/test_admin_browser.py` exercises login, CSRF, logout and expiry in Chromium
and WebKit. These are software checks, not a hardware acceptance result.

Design references: [argon2-cffi password hashing](https://argon2-cffi.readthedocs.io/en/stable/howto.html),
[OWASP session management](https://cheatsheetseries.owasp.org/cheatsheets/Session_Management_Cheat_Sheet.html),
[OWASP CSRF prevention](https://cheatsheetseries.owasp.org/cheatsheets/Cross-Site_Request_Forgery_Prevention_Cheat_Sheet.html).
