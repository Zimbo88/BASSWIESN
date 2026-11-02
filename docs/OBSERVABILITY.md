# Local observability and diagnostic privacy

Observability writes local structured JSONL/SQLite data. It does not install an
external telemetry SDK, exporter, upload service, or new background network
operation. A local diagnostic export is not an upload or a hardware acceptance
result. Existing explicit provider/device operations retain their own policies.

## Request, job, interaction and error correlation

Every HTTP request on the web, cloud and debug applications receives a fresh,
server-generated `X-Request-ID`. Client-supplied IDs are ignored. IDs are opaque
random identifiers, not authentication capabilities. Existing response bodies
and error codes are unchanged; IDs are response headers, including errors.

The local `http_request_completed` event contains the listener, method, matched
route template, response status, elapsed milliseconds and completion outcome.
It never collects raw request paths, query strings, bodies, arbitrary headers
or client addresses. Unmatched/authorization-short-circuited routes use a fixed
placeholder. Successful `/live`, `/ready`, `/api/health`, static and media reads
do not append completion events; their errors still do. This limits routine
polling/streaming log traffic without changing response correlation.

Context-local IDs follow asynchronous work without mixing concurrent requests.
Owned background tasks receive a distinct `job_id`, preserve their originating
`request_id`, and record start, completion/failure/cancellation and stop events.
Their failure logs retain exception type, not raw exception text or tracebacks.
The device-interaction coordinator inherits the job ID, then request ID, unless
its caller supplies an existing domain correlation ID. Existing persisted setup,
recovery and write-ledger job IDs are not replaced or migrated by this change.

Logs remain best effort: configuration/storage/sink failures must not replace a
response, transport error or cancellation. Ordinary completion events still use
the existing synchronous, fsynced masterlog; this is not a zero-overhead tracing
system. Rotation bounds storage but is not a queue or back-pressure mechanism.

A 250-call local ASGI microcheck on the engineering host measured median/p95
microseconds of 0.28/0.36 without middleware, 3.88/5.24 for a correlated quiet
health probe, and 53.07/63.23 with completion-log fsync to temporary storage.
These are a local overhead observation, not a Raspberry Pi, slow-storage or
end-to-end workload acceptance result.

## Health endpoints are different contracts

| Endpoint | Meaning and permitted work |
|---|---|
| `/live` on each listener | Process HTTP response only; no radio/provider checks. |
| `/ready` on each listener | Local schema availability; failure is HTTP 503. Public output is minimal. Extended updater attestation requires its separate loopback capability. |
| `/api/health` | Legacy application/version health contract; not a provider or radio readiness claim. |
| `/api/readiness` | Administrative local DB/filesystem/background-task diagnostics. |
| `/api/system/healthcheck` | Passive local filesystem, manifest and database inspection. Cached device flags are explicitly NOT_PROBED; no fresh radio probes. |
| Support-bundle workflows | Explicit diagnostic collection with its own access/redaction policy; not a liveness probe. |

Historical database counts can be slow on a Pi with a large diagnostic history.
The system healthcheck, storage summary and telemetry-list handlers run in
FastAPI's worker pool with request-local database sessions. They must not block
the event loop that processes radio responses. A slow diagnostic query is not
grounds to increase radio-write retries or certify an unobserved readback.
Concurrency regression tests hold a diagnostic read open while checking that
the lightweight health endpoint remains responsive.

The administrative service-health view checks the cloud and debug listeners'
loopback `/live` routes, not their authenticated diagnostic pages. It requires
HTTP 200 and the exact minimal `{"live": true}` contract. Login/error pages,
401, 404, redirects and malformed/oversized responses are not healthy listeners.
Checks are bounded in time and bytes, do not use environment proxies, and never
follow a redirect off loopback. This proves process liveness, not provider,
database readiness, radio reachability or audible output.

The authentication/listener boundary still applies. Correlation headers do not
grant access or broaden a scoped token's permissions.

## Redacted exports are not anonymous exports

Support ZIPs include deterministic payload ordering, a redacted manifest and
SHA-256 payload checksums. They accept inspectable UTF-8 text only. Secret
redaction applies to bytes, text, parsed JSON and metadata. Exported IPv4/IPv6
addresses are anonymized, including manifest metadata. Private operational logs
may retain addresses for local diagnosis.

Source input is bounded before JSON parsing, redaction and compression. The
entry count is capped at 256; payload, manifest/checksum and final ZIP sizes are
checked. Traversal, control characters, backslashes, drive-like names, duplicate
case-folded names and collisions with the manifest/checksum names are rejected.
Log tails are bounded and opened through no-follow directory/file descriptors;
symlinks and special files cannot redirect or block the reader.
The first partial byte-window line is discarded; multiline secret envelopes
are redacted before line-count truncation, including orphan private-key suffixes.

Legacy protocol request diagnostics now redact before their 4096-character
storage limit. Values over 64 KiB are omitted completely, not truncated before
redaction. Parsed JSON also redacts non-string secret values. Truncated quoted
secret assignments are handled defensively. The request-log API does not accept
an arbitrary headers collection.

Names, device IDs, station titles and unrecognized free-form personal text can
remain. Arbitrary opaque secrets without a recognizable field/envelope cannot
be reliably detected. Review every archive before sharing. Existing historical
rows/files are not retroactively rewritten. Full database backups are private
recovery artifacts, not redacted support bundles.

## Retention, export and deletion

| Data | Existing lifecycle |
|---|---|
| `master.log` | Local size rotation: default 50 MiB and five backups. This is not age-based erasure. |
| Request logs / protocol telemetry | Maintenance preview and execution use configured defaults of 14 / 30 days. |
| Configuration backups | Maintenance keeps the configured default of 100 rows per device. |
| Research diagnostic/recovery history | Bounded retention, default 30 days; only terminal recovery operations qualify. |
| Device interactions | Configured default 14 days, only completed GET/HEAD observations of explicitly listed read-only endpoints with known terminal outcomes. Unknown endpoints, unfinished reads and all mutations remain. |
| Terminal reporting queue | Seven-day retention; active/retry operations are preserved. |
| Artwork | Only expired entries beyond a seven-day grace period qualify. |
| Stored diagnostic/support ZIPs and rotated logs | Known-name filesystem quota cleanup; active logs and unknown/symlink files are excluded. |

Research retention shares one total batch budget across its eligible tables.
Current provider/playback/metadata snapshots and active operations are not
age-purged. Preview is non-mutating. Artwork cleanup uses a stable, private,
process-shared nonblocking lock also used by the cache producer. The lock covers
local file/row publication and deletion, never an await or provider fetch. Busy,
unsafe or unavailable ownership prevents file deletion and is reported for retry.
This closes retention versus publication races. Both successful and failed
fetch publication refresh the SQLite row under the same lock: a delayed request
cannot replace an observation whose request timestamp is newer or equal. Equal
timestamps preserve the first committed result. This ordering uses UTC request
timestamps, not a distributed logical clock; it does not promise correctness
across arbitrary host-clock changes. A busy lock returns `CACHE_BUSY` without
persisting a synthetic provider failure or overwriting the other writer's state.

Deletion intent (`RETENTION_PENDING`) is committed before an artwork file is
unlinked. Initial commit failure leaves files untouched. Failed unlinks retain
rows for retry; a crash or final DB failure after unlink leaves a durable expired
tombstone which the next pass reconciles as a missing file. SQLite and filesystem
updates are **not** a single atomic transaction. Only regular cache leaves below
the approved root are removed, using pinned no-follow directory descriptors.
Do not remove the lock file while any worker may be using the cache.

Practical administration surfaces already include `/api/maintenance/storage`,
`POST /api/maintenance/cleanup/dry-run`, `POST /api/maintenance/cleanup/run`,
telemetry export and diagnostic/support export. Authentication, CSRF and token
route policy apply unchanged; no new anonymous deletion endpoint is introduced.
Use the preview and review its exact scope before requesting execution.

These are retention/quota tools, not full personal-data erasure. Operator copies,
downloaded archives, external backups, SQLite free pages and filesystem snapshots
are outside their deletion guarantee. In particular, the existing interaction
retention setting alone is not evidence that all historical interaction rows
have been pruned: the runtime applies it only to the conservative read-only scope
above. Write/audit ledgers are not touched. No secure-wipe or whole-host deletion
claim is made.

## Offline verification

Focused tests cover concurrent requests, context restoration, streaming,
cancellation, domain/unhandled errors, task failure secrecy, device-interaction
linking, observer failure isolation, archive ambiguity/bounds, redaction before
truncation, unsafe file descriptors, commit failures, retry tombstones, independent
flock ownership and refresh attempted precisely between retention recheck/unlink.

```sh
python -m pytest -q tests/test_request_observability.py \
  tests/test_request_log_privacy.py tests/test_support_export_privacy.py \
  tests/test_retention_privacy_failures.py tests/test_secret_redaction_boundaries.py
```

These are mocked/offline software tests. No real radio, private LAN, deployment
or hardware state is needed or authorized by them.

## Client reports and observed listening

The compatibility `/api/play-history/start` endpoint returns an already fresh,
backend-observed session, or `opened: false` while waiting. It does not trust a
browser's `PLAY_STATE`, timestamp, source label or `is_confirmed` flag and does
not contact a radio. Actual playback services/readback remain responsible for
opening and refreshing confirmed listening history.

`/api/play-history/event` records an unverified, zero-duration internal report.
A client-reported stop/pause cannot close a radio-observed session. The old
`/api/play-history/{id}/stop` URL only acknowledges an already closed row; an
open row returns `HISTORY_OBSERVATION_REQUIRED`. Control the radio through the
verified playback/key API instead. This distinction prevents a successful HTTP
request or a stale browser state from becoming evidence of speaker activity.

The browser's cached health reader also binds its nine independent projections
to the selected stable target and request generation. Switching targets clears
the previous display, cancels stale GETs and rejects late or mismatched bodies,
even when cancellation is ignored. One failed health plane does not erase the
other planes. Local metadata/clock actions keep their original target; late
results cannot be applied to a different selected radio. This UI protection
does not replace server-side identity or write verification.
