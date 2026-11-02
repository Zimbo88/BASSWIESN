# ADR 0001: distinguish administration from device protocols

Status: **partially implemented**. Opt-in local bootstrap, sessions, CSRF and
default-deny human listener boundaries are implemented; see [operation and
limits](../ADMIN_AUTH.md). [Limited scoped automation](../API_TOKENS.md) is now
implemented; broader token write contracts, device source policy and complete
authenticated-workflow acceptance remain open. This does not certify the
hardened-LAN preview as production-ready.

## Context from the actual application factories

The web application is not a purely human-facing service. It mounts human
routers and selected Bose compatibility routes on the same listener. Conversely,
the cloud application is not entirely a device-only surface: it also exposes a
station inventory containing stream URLs. A rule based only on port number or
the words "cloud" and "debug" would therefore be insufficient.

| Listener / route family | Role and required boundary |
|---|---|
| Web HTTP/HTTPS: `/`, `/remote/{device_id}`, administrative `/api/*` | Human session or explicitly scoped automation; mutations require authorization and retain device safety/readback checks. |
| Web: exact routes registered by `_mount_web_cloud_compat` | Device protocol, not interactive browser login. Classify registered endpoint and HTTP method; never exempt an arbitrary prefix from authorization. |
| Cloud: Bose streaming/BMX/Marge contracts | Device-specific policy, validated request shape and limits. A caller-supplied device ID is an identity claim, not an administrator credential. |
| Cloud: `GET /api/cloud/stations` | Human inventory/diagnostic data, not a necessary blanket auth exemption. Preserve any client compatibility explicitly rather than exposing stream URLs through a forgotten listener. |
| Cloud: DLNA relay and `/media` | Device playback data plane. Apply its own source/resource policy; no route to administrator actions. User media is not automatically public documentation. |
| Debug: `/requests`, `/diagnostics.json`, root/API documentation | Human diagnostic surface. Must not become an alternate unauthenticated route to private configuration or household activity. |
| Minimal process liveness | Separately classified public response containing no devices, credentials or configuration. Do not confuse it with detailed component health. |
| Host update UNIX socket | Independent peer-UID/capability authorization. A web session does not remove helper-side artifact or rollback checks. |
| Capability-bearing loopback `GET /ready` | Runtime-local, read-only worker attestation; no human or host-update authority. Ordinary requests still receive only minimal health. |

The inventory in `docs/generated/api-contract-matrix.json` records actual route
mounts. Dynamic policy must use the resolved route/method or an equally strict
registry, not merely `path.startswith(...)`. Unknown management paths must not
default to anonymous access. OPTIONS, HEAD, trailing slashes, percent encoding,
mounts, generated API docs and method mismatches need explicit tests.

## Decision constraints

1. Keep existing device protocol URLs and payloads compatible. Do not add a
   browser login response to a radio's station or reporting request.
2. Bootstrap locally and explicitly, without a known/default password or an
   unauthenticated first-request-wins account creation route on the LAN.
3. Use a modern password hash and server-side revocable sessions. Rotate on
   login; reject revoked/expired sessions; never place tokens in localStorage.
4. Require CSRF and origin validation for browser mutations. CORS configuration
   alone is neither authentication nor CSRF protection.
5. Treat HTTPS according to the actual deployment. A forged forwarding header
   must not silently establish a trusted origin or change cookie security.
6. Cookies are not port-isolated. A separate listener alone does not keep an
   admin cookie from reaching another port on the same host. Device endpoints
   must never interpret a received session as permission for admin operations,
   and request logging must redact cookies and authorization material.
7. Scoped tokens are a separate automation credential; a read token must not
   reach writes via alternate methods, routes or debugging helpers.
8. Authentication failures never trigger radio discovery, transport creation,
   fallback writes or setup repair. Successfully authenticated users still pass
   stable-ID/protection/capability/backup/readback gates.
9. Migration from the existing unauthenticated installation must be explicit,
   recoverable and documented. Authentication cannot be advertised before every
   relevant listener is covered and the browser flows have been exercised.

## Verification before accepting this ADR as implemented

Use real application factories with mock device/provider adapters. Exercise
unauthenticated, valid-session, revoked-session and insufficient-scope requests
on web, HTTPS, cloud and debug boundaries. Include cross-origin mutations,
session fixation, bootstrap races, rate limits, invalid proxy headers, alternate
HTTP methods and protected-device attempts. Prove device-facing playback and
reporting requests are still structurally compatible without login.

No part of this acceptance requires contacting an actual radio. Hardware
interoperability remains a separate, explicitly authorized later check.

## Update and restored authentication state

Use a launcher-rotated capability outside persistent snapshots for internal
worker attestation, rather than issuing an admin session or exempting detailed
human health routes. Rollback must verify unchanged credentials before its first
reverse exchange. Unknown credential schemas fail closed. Sessions are runtime
grants: the supported launcher revokes them before serving a restored generation
or a normal restart. This deliberately trades automatic browser reconnection
for preventing snapshot-based session resurrection. See [the full contract and
software evidence](../UPDATER_AUTH.md); real deployment acceptance is separate.

Automation grants are durable credentials, unlike runtime sessions. Rotation,
revocation, scopes and expiry must participate in credential continuity. Only
usage timestamps may be ignored. Token management rechecks a browser session
inside its write transaction so logout/reset can win over queued issuance.
