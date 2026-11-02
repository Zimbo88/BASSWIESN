# Historical engineering baseline and safety review

Status: **private software acceptance**, inspected through 2026-10-07 against the working tree based on
`0cf83d1`. No hardware validation, deployment, release or remote mutation belongs
to this engineering pass. Existing uncommitted AirPlay work is preserved.

This records the earlier engineering inspection, not the current candidate's
version or acceptance receipt. Current preparation targets 5.0.0; see the
[candidate notes](releases/5.0.0/RELEASE_NOTES_5.0.0.md) for present boundaries.

## What the code actually contains

At initial inspection the package identified itself as 3.0.2; the branch name is not a release version.
The original Docker label was 3.0.0. Runtime/UI version sourcing already uses
`basswiesn.__version__`; the missing contract was at the image/release boundary.
The development interpreter is Python 3.14; the container used 3.12-slim.
Unbounded runtime and test dependencies were mixed in `requirements.txt`.
There were no tracked GitHub workflows or coverage gates.

Existing abstractions include protected-target gates, DNS-pinned HTTP,
revisioned preset transactions, a setup coordinator, a provider registry,
independent health projections, redacted support exports, a host update-helper
protocol and a DLNA library. They must be reused, not replaced by parallel safety
implementations. General admin authentication was absent at initial inspection.
The incremental [administrator-auth preview](ADMIN_AUTH.md) now adds opt-in
sessions and CSRF/listener enforcement. [Scoped automation credentials](API_TOKENS.md)
now have a positive route allowlist and session-only management UI; comprehensive
authenticated workflow and deployment acceptance remain open. Update-helper authorization is still a
separate boundary, not a substitute for web authentication.

## Write-path matrix

This is a review map, not certification that every branch meets the desired
contract. `python tools/generate_phase4a_contracts.py` enumerates routes and transport
call candidates without importing the application. Its existing generated API,
device-access and configuration matrices are reused. It includes subprocess/CLI
entry points, not only HTTP POST. Dynamic dispatch still requires manual review.

| Feature / entry | Service and transport | Current safety/readback | Next review / recovery boundary |
|---|---|---|---|
| Device settings, `routers/api.py` | `settings_contract` → `device_settings_writer` → `SoundTouchClient` | All-field validation, protection/IP guard before construction, exact known identity/profile, durable START backup/hash, intent, actual readback, partial-result ledger, shared local worker ownership | Software-tested only; unadopted legacy writers, independent data mounts and front-panel changes remain external races |
| Rename, `api.rename_device`, settings-apply | Same settings transaction, HTTP `/name` only | Local name commits only after same-identity `/info` reports the requested name | Hidden persistent SSH fallback removed; persistence after reboot remains NOT ESTABLISHED |
| Playback/select/key, `api.py`, `stations_presets.py` | `direct_selection`, `key_actions`, `station_playback` → SoundTouch XML transport | Shared identity/profile/ownership; complete START/intent, content/account readback, observed volume/mute; buffering remains pending; no lost-command replay | Software-tested; actual firmware latency, audio and external-controller races need manual acceptance |
| Preset store/sync | Compatible router adapter → `preset_writer`, `preset_contract`, `preset_transactions`, `preset_safety` | Persisted before-XML; revision; shared ownership; store; repeated full readback; compare; local commit | Lost reply now stops batch and triggers stable readback without replay; broader DB/crash reconciliation remains |
| Preset delete | `stations_presets.delete_preset` | Before snapshot, mutation record, removal/readback, ledger | Preserve rollback reference and divergence evidence |
| Native station search | `api.native_station_search` → `native_station_search` → XML POST | Stable target ownership, protection before transport, exact live profile before/after one bounded XML query; no automatic retry | Search results are not preset persistence or playback evidence; no hardware validation inferred |
| Cloud setup legacy | Compatibility routes in `routers/setup.py` | Real execution is retired: HTTP410 before transport or job construction; read-only previews remain | Historical helper code is not an approved execution path. Use the separately guarded rebuild coordinator; `force` cannot revive retired writes |
| Setup/rebuild | `setup_rebuild.coordinator` → radio adapter → CLI17000/SSH/XML | Per-device worker ownership, identity/profile, backup, routing, reconnect, presets/final readback; rollback shares ownership | Interrupted jobs require review, not automatic write replay; exact profile matching needs exhaustive audit |
| Multiroom create/join/leave/remove | `routers/multiroom.py`, `zone_creation`, `zone_changes`, `multiroom_safety` | All-participant ownership/protection, exact-profile START snapshots, single attempts and distributed readback; creation persists and hash-checks identity/topology/volume/latency before writes | Lost replies reconcile by reads only; explicit rejection never becomes success; external-controller races and older reconciliation paths still need review |
| Reboots | `radio_reboots`, `maintenance_reboot`, setup adapter | Explicit job/schedule, target verification and reconnect | Same IP is not same identity; distinguish legacy LAB route |
| Scheduled playback/stop | `alarm_engine` delegates verified station/key/power/multiroom services | Saved/enabled schedules; all-target ownership/protection; pending playback is not successful history; uncertain STOP prevents standby | Existing-zone standby is blocked, not implicitly dissolved; group playback needs per-member acceptance |
| Recovery | `playback_keepalive`, `live_radio_reconnect` | Opt-in, generation/CAS, backoff, exact live profile, unchanged complete START, hash-backed backup, full selection/account/side-effect comparison | Keepalive itself does not POST radio commands; reconnect snapshots retained for review; real end-of-stream acceptance still required |
| DLNA playback | DLNA router/library → established playback path | LAB gate and outbound URL policy | Simulated server tests are not production hardware approval |
| AirPlay relay | bridge services + external module worker | LAB status, device protection/audio safety | Prior local changes uncommitted; no inferred hardware PASS |
| SSH/Telnet/CLI diagnostics | `api.py`, `telemetry.py`, setup runners | Protected target checks before connect | Command capability matters, not helper name; audit shell writes separately |
| Backup/restore | backup/restore services, setup snapshots, preset paths | Archive integrity, atomic no-overwrite restore preparation; WAL-safe migration snapshots and durable attempt records | Whole-installation compatibility/cutover and partial state still require review; no replacement of a live database |
| Application updates (host, not radio) | update-admin → UNIX socket → `update_helper` | Admin capability, peer authorization, artifact/manifest, staging, journal, candidate and rollback | Separate trust boundary; do not grant container root or mount Docker socket |
| Optional provider catalog | `routers/providers.py` → typed provider manager → pinned transport | Disabled by default; session/CSRF-only explicit probes; approved exact origin/peer set, device exclusions, bounded responses | Catalog search/browse only for the optional adapter; no remote player writes or audio verification |
| Offline CLI | `basswiesn.cli` and shared archive validators | Stopped/checkpointed snapshots only; no application startup, transports, migrations or device actions | Private backup creation is explicit; structural compatibility does not prove restore or runtime health |

`SoundTouchClient` writes ledger records with `verified=False`; HTTP success is
transport evidence only. Several higher-level paths compare real state, but the
remaining legacy write paths show that the desired invariant is not universal.
The mission must close reachable gaps rather than declare all historical helper
code safe. Retired compatibility writes deliberately remain unavailable instead
of silently falling back to an older, less strict implementation.

## Listener / trust-boundary matrix

| Boundary | Current entry points | Required direction |
|---|---|---|
| Human HTTP administration | web app, default 1328, `/api/*`, HTML | Local admin sessions, CSRF, scoped automation tokens; no default password |
| Human HTTPS administration | HTTPS web app, default 1329 | Same authorization; secure cookie only when actually HTTPS |
| Radio cloud compatibility on web port | `_mount_web_cloud_compat`: streaming, BMX, Marge/account/preset routes | Must not be accidentally hidden behind interactive browser login |
| Radio cloud service | cloud app, default 1516; registry, provider, reporting, DLNA relay | Request/identity/origin policy; no general admin privilege |
| Diagnostics listener | debug app, default 1860 | Required mode applies the same human session boundary; public minimal health and exact device contracts are separate registrations |
| Host update service | UNIX socket, separate process | Peer UID + narrowly scoped capability; never substitute browser cookie for host authorization |
| Outbound radio traffic | XML HTTP, CLI17000, SSH/Telnet, explicit discovery | Protected IP AND stable identity; policy before transport, pinned destinations |
| Outbound provider traffic | media URL, artwork, catalog, reporting | SSRF policy, bounded payload/timeouts, redirects and actual peer constraints |
| Database/filesystem | SQLAlchemy repositories/migrations, private snapshots | Transaction/revision checks; secrets excluded from diagnostics/releases |

The launcher prepares the database once, then starts separate Uvicorn workers.
Web lifecycle starts configured schedulers/keepalive; cloud lifecycle owns
event-driven tasks. Update validation mode suppresses background tasks. A
normal page load must remain passive; a blanket authentication middleware would
break device-compatible paths and is not an acceptable shortcut.

## Order of work

The latest backend extraction separates local health/system routes into
`routers/system_api.py` and passive health inspection into `runtime_health.py`.
Status/support reads no longer probe radio TCP ports, generate TLS files or
overwrite a fixed temporary filename. Unprobed reachability remains explicitly
unknown; directory access is a permissions inspection, not a successful write.

Preset XML/integrity logic and store/readback orchestration now have typed
service modules with compatible router adapters. Persisted provider-health
observation is separate from keepalive polling/recovery. These are incremental
extractions, not completion of every large router or recovery function.
The [ownership contract](DEVICE_TRANSACTIONS.md) records cross-process adoption
and remaining paths. Authenticated settings workflows are tested with real
session/token boundaries and simulated devices; broader deployment remains
unapproved.

1. Checked version contract; runtime/dev hash locks; tooling and CI foundations.
2. Measure real line/branch coverage and failures; ratchet from measured data.
3. Characterize unsafe/inconsistent legacy writes before changing them.
4. Extract domains one at a time; preserve URLs and response compatibility.
5. Implement auth from the listener matrix; provider/CLI/security improvements.
6. Full software regression and reproducible packaging; hardware approval stays
   separate and manual. No feature graduates from LAB based on CI alone.

This document intentionally lists remaining work. It is not a completion report.

## Implemented corrections from this review

The version, dependency and CI foundations were implemented locally. The Python
version remained 3.0.2 during that inspection; Docker derives its label through a checked build
argument. Runtime and development dependencies have separate exact hash locks.
The workflows do not publish, deploy, or run hardware tests.

The review also produced specific behavioral fixes, each first demonstrated by
a failing regression test:

- IPv4-mapped IPv6 spelling no longer bypasses same-address protection.
- Unreadable persisted protection policy raises a fail-closed domain error,
  rather than silently returning an empty protection set.
- The central interaction coordinator never retries a POST merely because the
  response was lost. Caller-owned readback/reconciliation is still required.
- System backup validation rejects empty/incomplete/unsupported manifests,
  corrupt hashes and unusable database payloads. Same-second backups no longer
  overwrite one another, and restore staging revalidates copied bytes.
- Diagnostic log/error/support redaction now shares one core policy. UTF-8 byte
  payloads cannot bypass support redaction; opaque binary payloads are rejected.

The API inventory now follows actual router mounts from the application source
instead of a manually maintained module list. A runtime-schema comparison test
checks that all mounted management routes are represented. Static evidence
references remain heuristic candidate references, not proof of a route's
complete transitive write/readback behavior.

The rename and settings transaction now addresses the original gaps without
changing URLs. The exact flow and remaining limitations are documented in
[settings transactions](SETTINGS_TRANSACTIONS.md). The coverage ratchet records
measured line and branch coverage; it is not a blanket claim of 90% coverage or
completed hardening. Other legacy write paths still require review.

The next scoped additions reuse those boundaries: [provider contracts](PROVIDER_ARCHITECTURE.md)
separate catalog capabilities from playback evidence, [CLI/doctor](CLI.md) offers
bounded offline administration, and [observability/privacy](OBSERVABILITY.md)
adds request/job correlation and safer local exports/retention. Their software
tests do not complete hardware approval, full data erasure, cross-process write
serialization or the outstanding container vulnerability gate.

The preset page now uses a target-bound read coordinator: switching radios clears
the old projection immediately, cancels only obsolete GET requests, and rejects
late or mismatched results. It never converts a DOM event into probe approval or
replays writes. Chromium and WebKit tests deliberately return the old target last.
This is one extracted domain, not a claim that the complete browser application
has been modularized.

[Database startup](DATABASE_SAFETY.md) retains the existing additive migrations
and adds private pre-change snapshots and explicit interrupted-attempt evidence.
[DLNA](dlna-library.md) separates explicit search from connection and validates
literal IPv6 and actual socket peers. Discovery is still LAB, not verified audio.
[Acceptance records](ACCEPTANCE.md) can check local evidence integrity without
granting hardware capabilities. The [performance harness](PERFORMANCE.md) uses
synthetic data and excludes network traffic; its timings cannot certify a Pi.
