# Scoped automation credentials (engineering preview)

Automation can use bounded bearer credentials on the **web administration
listener in required mode**. This is deliberately a positive route contract,
not a generic exemption for `/api`, GET requests or an `admin` role. Legacy mode
remains unauthenticated: it must not be presented as a token-protected service.
This local software implementation has not been deployed or hardware accepted.

## Authority and supported operations

Scopes are independent; none implies another, including `admin`. A token may
contain multiple explicitly selected scopes. Only these reviewed handlers are
currently accessible:

| Scope | Exact endpoints | Effect |
|---|---|---|
| `read` | GET `/api/version`, `/api/health`, `/api/devices/status-badges`, `/api/devices/ui-capabilities` | Local version/health and cached device projections, no discovery |
| `presets` | GET `/api/presets/{device_id}` | Stored presets and revision/divergence information; not a live playability claim |
| `multiroom` | GET `/api/multiroom/scenarios`, `/api/multiroom/methods` | Saved scenarios and method descriptions, not current radio topology |
| `diagnostics` | GET `/api/diagnostics/telemetry/summary` | Local aggregate diagnostic observations |
| `playback` | POST `/api/devices/{device_id}/settings/volume` | Existing identity-bound volume transaction; not unrestricted remote keys |
| `admin` | POST `/api/devices/{device_id}/rename`, `/api/devices/{device_id}/settings-apply` | Existing backed-up and readback-verified settings transactions |

Preset writes, group changes, setup, reboots, raw transport, support bundles,
updates, token management, HTML/OpenAPI pages and unlisted endpoints remain
**denied to every bearer**, even one with all six scopes. Preset/multiroom scopes
are currently read-only; their write contracts need separate transitive safety
review. This is a usable limited automation surface, not full automation parity.

The authoritative allowlist is `core/token_policy.py`. Authorization verifies
HTTP method, effective route template, original handler module/function and,
for the parameterized settings route, `setting == volume`. First matching
routes, aliases and included-router prefixes matter. A newly added route never
inherits token access through its name, HTTP method, prefix or listener alone.
Tests use the actual locked FastAPI router context iterator.

All granted writes still execute the existing service: target identity,
protection, exact supported profile, previous state, backup, durable intent,
single write, real readback and divergence handling. Authorization cannot turn a
transport acknowledgement into verified device success. An allowed operation
can still be rejected by any device-safety gate. No automatic write retry is
introduced; an in-flight authorized radio action is not cancelled by later
token revocation.

## Creating and managing grants

Only a logged-in administrator session may manage tokens. Its existing Origin
and CSRF checks apply. Management cannot be performed by bearer credentials,
including an `admin` bearer. There is no token stored in browser localStorage.
Open **Settings → Automation access** after signing in, or `/auth/automation`.
The engineering-preview panel creates, rotates and revokes bounded grants. It
shows scopes, expiry, revocation and last authorized use. German and English
are available; other browser languages use English. Desktop and mobile layouts
use the same server-side authorization, never browser state as authority.

Only `read` is selected initially. Permissions with radio-write authority are
explicitly described, not bundled into an administrator wildcard. Rotation and
revocation ask for confirmation tied to the specific grant. The secret appears
only in the successful creation/rotation response and a temporary text area:
no localStorage/sessionStorage, no URL, no automatic clipboard operation. Save
it privately, then clear the display. Leaving or hiding the page clears it.
Backgrounding also invalidates pending display responses: returning to the tab
cannot redisplay a late credential. If a write was in flight, explicitly refresh
the grant list instead of retrying. Rotation binds the response and credential
envelope to the selected grant before displaying the new secret.

Credential mutations are single-flight. A failed or missing reply disables
further mutations until an explicit refresh. A lost creation response may mean
a real grant exists: inspect the list and revoke or rotate it deliberately;
never recreate automatically. Refresh cannot recover a previous secret. A
disabled button alone is not proof that revocation finished; the list is read
back before controls become ready again.

1. Sign in through `/auth/login` using the configured trusted HTTPS origin.
2. Obtain the session's CSRF value from `/auth/session`; keep it in memory.
3. POST `/auth/tokens` with the same-origin `Origin` and
   `X-BASSWIESN-CSRF` headers and a body such as:

```json
{"name":"Local dashboard","scopes":["read"],"lifetime_seconds":86400}
```

The `201` response includes `id`, `name`, `scopes`, `created_at`, `expires_at`,
`last_used`, `revoked` and **one newly generated `token` value**. Save the token
in the automation client's private credential facility. Do not place it in a
URL, shell command history, log, screenshot, issue or browser storage.

GET `/auth/tokens` lists metadata only, including revocation and last authorized
use. POST `/auth/tokens/{id}/rotate` atomically replaces the secret without
extending expiry or changing scopes; only its response contains the new value.
DELETE `/auth/tokens/{id}` revokes the grant; repeating revocation reports
`revoked: false`, never reactivates it. To change scopes or extend expiry, revoke
and explicitly create another grant. A lost creation/rotation response cannot
be retrieved: inspect the list and explicitly revoke/rotate as appropriate;
do not blindly replay credential mutations.

Names are bounded 1–64 character ASCII labels. A lifetime of 60 seconds through
90 days is required; no unlimited grant exists. The store retains up to 256
records, including revoked ones. At that bound, creation refuses rather than
silently deleting security state; automatic pruning is not implemented.

## Using a bearer

An automation request sets `Authorization: Bearer <opaque value>` on an
explicitly permitted route. It uses the trusted configured HTTPS origin/Host;
forwarded headers cannot substitute for actual TLS. The documented HTTP opt-in
for loopback tests remains insecure for live credentials.

Do not send cookies, `Origin` or `Sec-Fetch-Site` with automation requests.
Browser contexts are deliberately rejected instead of bypassing CSRF. Duplicate
Authorization headers and mixed cookie/bearer requests are rejected. A bad,
expired, revoked or insufficient bearer never falls back to an administrator
cookie. Cloud/debug and device-compatibility endpoints do not accept bearer
authority. Minimal public health is not an administrative capability.

Responses are `no-store`. Errors distinguish invalid credentials (`401`),
context/scope denial (`403`) and unavailable/unsafe credential storage (`503`),
without returning submitted credentials. Request handling does not fetch an
external identity service or contact a radio to authenticate a client.

## Storage, races and rollback

Credentials contain a random identifier plus a separate 256-bit random secret.
Only a SHA-256 digest of the complete opaque value is stored in the private
administrator SQLite database. The database retains permission/owner/link
checks and transactional serialization. Argon2id remains the password mechanism;
high-entropy bearer credentials do not require password stretching.

Issuance, rotation and revocation emit bounded constant-code audit events.
Raw token values are excluded from object representations and metadata lists;
shared support/log redaction also recognizes their envelope outside headers.
Redaction is not permission to collect credentials. Last use records an
authorized request, **not proof that its downstream action succeeded**.

Password recovery revokes every token and browser session. Normal launcher
restart revokes sessions but preserves unexpired automation grants. Time moving
behind creation or the last observed use fails closed. Management mutations
recheck the administrator session within the same SQLite write transaction:
a logout/password reset that wins before that transaction cannot be followed
by an already-queued request minting a fresh durable grant.

New bootstraps use auth schema version 1. Older four-table preview stores remain
usable for session login, but token operations fail closed until an explicit
local upgrade. Stop the service, retain installation backups and run as the
application UID with its ordinary data configuration:

```sh
python -m basswiesn.admin enable-api-tokens
```

The command takes a private SQLite backup including committed WAL state,
checks it, records a SHA-256 manifest and only then performs the additive schema
transaction. It reports the retained private backup path. Repeating the command
is idempotent. Requests never auto-migrate. Unknown/corrupt schemas or failed
backup prevent the upgrade. Keep these credential backups private; do not copy
them into support bundles, source archives or Git.

The stopped-generation updater compares token digests, scopes, expiry,
revocation and schema as well as administrator credentials. It ignores only
token use timestamps and session/audit activity. Any grant addition, rotation
or revocation after the snapshot prevents automatic rollback to stale authority.
Both generations remain retained with `MANUAL_ACTION_REQUIRED`; availability
does not override revocation. See [update boundaries](UPDATER_AUTH.md).

## Offline verification and remaining limits

```sh
python -m pytest -q tests/test_api_tokens.py tests/test_token_boundary.py
python -m pytest -q tests/test_auth_credential_continuity.py tests/test_update_auth_boundary.py
```

Tests cover exact route grants, aliases/prefixes, scope separation, token/cookie
ambiguity, revoked credentials, clock rollback, private backups, schema errors,
management races and unchanged radio-safety behavior using mocks. They neither
contact household devices nor certify hardware. Token-specific device selection,
rate quotas and the remaining write contracts are not yet
implemented. Local runtime UID/host compromise is outside this credential
store's protection; device listener policy and full authenticated workflow
acceptance remain prerequisites for a production hardened-LAN claim.
