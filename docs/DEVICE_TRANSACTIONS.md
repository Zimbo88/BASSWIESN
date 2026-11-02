# Device transaction ownership and uncertain writes

An accepted command and a verified device result are different events. This
implementation increment adds ownership across local processes; it does not
make the proprietary device API transactional or allow new hardware profiles.

## Ownership is shared, not global

`services/device_ownership.py` uses a private, UID-owned `device-locks` directory
under the configured application data directory. A canonical stable ID maps to
a SHA-256 filename. Each worker must mount the **same** directory on a filesystem
that supports local advisory `flock`; separate data mounts do not coordinate.
No address-only lock, process-local mutex or browser-selected target can replace
the stable identity check.

The lock namespace is opened without following symlinks. Directory permissions
must be 0700 and regular single-link lock files 0600, owned by the runtime UID.
Symlinks, FIFOs, hard links, unsafe ownership/permissions and a replaced namespace
fail closed. Lock files are never removed during release: unlinking a locked
inode would allow a second independent owner. Operating-system process exit
releases ownership, including abrupt termination. This is not protection from
a malicious process with the same UID or filesystem-administrator authority.

Acquisition uses nonblocking attempts with asynchronous bounded waiting, not a
blocking sleep on the event loop. An unrelated radio remains independent. A
timeout returns `DEVICE_BUSY`; an unsafe/unavailable namespace returns
`DEVICE_LOCK_UNAVAILABLE`. Neither result permits constructing a new successful
transaction from an old browser state.

`device_transactions.device_transaction()` acquires a complete known target set
in canonical order. Nested orchestration may borrow an already-owned subset in
the **same task**. A spawned task does not inherit ownership, even when it
inherits context variables. Adding another target while holding a subset is
rejected rather than risking lock-order inversion. Partial acquisition failure,
cancellation and exceptions release only locks acquired by that context.

## Adopted paths and limits

Existing users of `device_policy.device_lock()` now share cross-process ownership:
verified settings, keepalive/recovery, explicit reboot jobs and related control
services. The interaction coordinator can participate inside a larger transaction
without releasing its parent ownership.

Preset set/delete/clone/sync/playback orchestration now holds ownership around its
state-dependent work; direct preset-writer service calls also acquire ownership.
The zone creation and remote-control grouping paths acquire their requested
master/member set before snapshots and retain it through readback. Their nested
playback/interaction calls borrow the relevant target without starting a competing
transaction. Preview-only paths do not acquire write ownership.

Zone removal and clearing now delegate to `zone_changes.py`. Every member in the
coordinator's snapshot must resolve by stable ID to known inventory; a stale IP,
unknown member or protected indirect participant blocks the change. The service
locks the whole affected set, verifies exact stored/live hardware profiles,
persists the identity/topology START snapshot and intent, then rechecks every
participant before its single `/setZone` attempt. It does not write volume or
automatically restore a topology after an uncertain outcome.

Readback covers the master, the removed radio and retained members. Missing,
malformed or identity-mismatching readback is unknown, never standalone. A lost
POST reply can be reconciled only by the complete distributed readback; the POST
is not replayed. Clear-all reports per-zone results, unreachable devices and
protected skips. An unreachable inventory cannot become “already clear”.
Snapshots here cover `/info` and `/getZone`, not a complete radio backup or a
promise to restore volatile audio/volume behavior. Hardware acceptance remains
manual. Unknown profiles that older clear/remove paths accepted now fail closed.

The persisted snapshot is reread and its SHA-256 compared before a write.
Both group services use the shared `device_fingerprint` for stable identity,
endpoint and cached model/firmware/profile inputs. A database refresh must not
silently retarget a pending action, even if the new endpoint answers with the
same ID. An explicit identity claim in topology XML must match the responding
device; reported member addresses must agree with the selected inventory.

If final journal persistence fails after the POST, the response is
`ZONE_FINAL_RECORD_FAILED` with `DIVERGED`, the durable START reference and no
automatic retry. A successful topology observation can be retained separately
as `readback_verified`; it does not make the application transaction committed.
This separation also matters if a browser loses that error response.

Setup execution and routing rollback now hold the same per-device ownership
around adapter operations and persisted results, in addition to the existing
setup-coordinator lease. A busy/unsafe lock prevents the first adapter call;
simulated previews do not acquire hardware-operation ownership. Failed or
cancelled operations release ownership without replaying the next step.

Zone creation now rejects missing/malformed topology snapshots and any former
coordinator, member or contradictory member address outside the selected set,
before latency/volume/topology writes. Clear the existing group explicitly first;
the operation never expands its lock set in flight. Master readback must report
the exact selected members, not merely the expected coordinator ID.

Creation also enters `zone_creation.py` after acquiring the full target set.
It validates exact cached/live profiles and snapshots `/info`, `/getZone`,
`/volume` and `/rebroadcastlatencymode` for every participant. The aggregate
snapshot is committed, read back and hash-checked before radio mutation. All
four states are rechecked after persistence; a changed topology or unavailable
volume is not replaced with an assumed default. The remote-control entry keeps
its separate broader backup as well.

Latency and volume POSTs require actual readback before proceeding to the next
step. The topology POST needs all selected radios to confirm the group. A lost
reply is reconciled by reads only; a rejected, unavailable or contradictory
result stops the batch with durable intent and reconciliation information.
Cancellation releases ownership but leaves the attempted write uncertain. A
failed final database commit never triggers a second hardware write. No automatic
rollback attempts to reconstruct a live clock or playback session.

The remote's safe-start checkbox applies to individual playback, not firmware
zone normalization. With that checkbox enabled, remote grouping is blocked
before any preview/write rather than silently dropping the requested safety
constraint. Both group-create APIs reject an explicit `safe_volume` with
`ZONE_SAFE_VOLUME_NOT_ESTABLISHED` before constructing radio transports. Ordinary
grouping remains available with its explicit firmware-volume warning; it is
not suitable for a test that requires a hard maximum volume. A muted group
transition and its hardware acceptance are still open, not implicitly provided
by a successful topology readback.

The existing volume contract remains: preserve mode sends no volume writes;
explicit individual start values are verified before grouping, and subsequent
firmware normalization is reported rather than silently fought. Full application
or radio backup and physical acceptance are still separate requirements.

This is **not** a claim that every legacy write is serialized or fully audited.
Remaining reconciliation callers also need review. External Bose clients,
physical buttons and firmware changes cannot be locked by this process. Existing
identity, protection, capability and expected-state checks remain mandatory.
There is no distributed atomic commit, no implicit hardware approval and no new
automatic rollback.

## Remote keys and preset selection

Native station searches also use the stable-device ownership boundary. Although
`/searchStation` is a query, the radio exposes it as a POST. The client validates
bounded XML, protection and the unchanged inventory fingerprint before creating
the transport, then verifies the exact live profile through `/info` before and
after one query. A lost reply is not retried; malformed or contradictory XML
does not become a successful empty result. Preview remains passive. This path
does not write a preset, claim audible playback or manufacture a configuration
backup for a query.

See also the [power and wireless boundary](#power-and-wireless-boundary) for
state-changing GETs and the explicit WLAN survey endpoint.

The existing `/api/devices/{id}/key` URL delegates to `key_actions.py`. Its
adapter preserves the request/response envelope but execution now requires a
known exact profile and complete live START: `/info`, `/now_playing`, `/volume`,
`/getZone`, and `/presets`. Preset actions additionally require a unique live
slot and a uniquely READY source/account. The slot's live ContentItem, not an
old local station label, defines successful playback. No preset is rewritten
as a hidden side effect of pressing its key.

The operation owns its stable target, commits a hash-backed START and records
each intent before sending a command. Identity, topology, presets and actual
state are rechecked around persistence. A lost press reply stops the composite:
one read-only observation is retained when possible, but no release, fallback,
toggle replay or automatic rollback follows an uncertain write. An unavailable
state is not interpreted as standby. For an explicitly requested safe start
volume, its actual value must be confirmed **before** an automatic wake can
resume audio. A failed volume readback is not permission to send three writes.

PLAY_PAUSE uses the observed prior state; a successful pause is not required
to report PLAY_STATE. A bounded read-only wait permits BUFFERING_STATE to become
PLAY_STATE for the same requested content. It never treats STOP or a different
station as this forward transition. STOP/PAUSE close history only after actual
stopped/paused readback; mute and volume keys require their actual fields to
change. A key acknowledgement with no established expected-state contract
(currently next/previous track and shuffle/repeat in this service) returns an
unverified outcome, not fabricated success. Read current state before another
action. Existing UI uncertainty handling remains applicable.

The optional preset fallback is limited to a definite INVALID_SOURCE for the
exact requested live ContentItem, with device policy permitting recovery and
the supported selection/URI contract validated. A stale local row, another
playing station, a manual STOP or an unavailable observation cannot trigger it.
There is at most one fallback selection attempt. Its reply does not prove
playback or audible output. Firmware volume normalization can prompt one
explicit, separately verified post-selection correction when a safe volume was
requested; that is not a retry of a lost write.

An ordinary remote request cannot silently expand its scope to zone peers.
During explicit zone creation, server code supplies the already selected and
owned participants. Their identities and snapshots are checked together;
unknown or protected indirect participants block the operation. The key result
verifies the requested target state and scoped topology/presets/volume, not
synchronous audible output on every speaker. The outer zone transaction keeps
its separate distributed topology verification. JSON cannot grant this internal
participant scope.

Final runtime/history state and the verified ledger entry commit together. A
failed database commit, changed START, cancellation or mismatch remains
unverified with reconciliation information. The shared HTTP snapshot parser
currently retains its legacy `RebootSnapshot` name in the maintenance module;
using it here does not call reboot or add another identity/protection policy.
Software simulations cover these contracts. Device latency, physical-button
races, firmware normalization and real multiroom playback still need explicit
hardware acceptance. No hardware profile was added by this refactor.

Cleanup keeps key START snapshots in a separate per-device quota from persistent
configuration backups. Frequent remote-button use therefore cannot displace
those configuration backups. Only key snapshots whose latest ledger record is
verified KEY_COMPLETED, with matching identity and content hash, are eligible
for count-based cleanup. Missing, interrupted, divergent or inconsistent records
retain their START even beyond that quota; an administrator must reconcile them
explicitly. This preserves recovery data instead of treating retention as
permission to erase an unresolved operation. It can increase disk usage during
repeated failures, which should be diagnosed rather than automatically hidden.

## Station playback and opt-in stream recovery

Manual station selection, saved timers and the playback phase of zone creation
now delegate to `station_playback`. A requested source name is insufficient:
the observed source, account and ContentItem location must match the actual
instruction. `BUFFERING_STATE` means selection observed, playback pending;
only `PLAY_STATE` produces a verified playback-state result. Neither proves
audible output. The browser and timer history preserve this distinction.

Protection precedes transport construction. The service owns the complete
server-selected scope, checks exact cached/live profiles and snapshots identity,
selection, volume/mute, presets, sources and topology. It hashes and durably
stores START and command intent, rechecks target/catalog/state, then sends each
command once. A normal station request cannot expand its target set through
JSON; only the enclosing zone operation supplies already selected participants.
Unrelated member volumes, presets and topology must not change.

No-volume mode sends neither volume nor mute commands. An explicit safe-volume
start holds the existing provider gate closed until post-select volume/mute
are observed. A firmware-restored volume can receive one separately checked
correction; a lost command reply never causes a replay. Missing initial state
does not authorize a POWER toggle. Temporarily absent post-select playStatus
permits bounded reads only. Explicit selection rejection can trigger the
existing safe shutdown, now requiring fresh identity and actual standby/volume
readback. Unknown writes, cancellation and failed final commits retain START
and reconciliation information; safe-volume failures leave the audio lock set.

Timers use the same verified key/power services for stopping. A pending STOP
cannot authorize standby. Group standby is deliberately blocked while a zone
exists: the timer does not silently dissolve a group or fall back to an
unverified GET. Use an explicitly reviewed zone transition before requesting
standalone standby. A verified group topology is not counted as playback on
every member. Partial/pending results remain visible rather than becoming a
successful listening-history entry.

`playback_keepalive` observes and evaluates provider/player health; the optional
`live_radio_reconnect` owns the single radio re-selection after a confirmed
native end-of-stream. Its existing opt-in, generation/CAS claim, cooldown and
attempt budget remain intact. Recovery now also checks the central write guard,
exact profile, complete unchanged START after the provider probe, and durable
backup integrity before selection. Full content/account, mute/volume, topology
and presets are checked afterward. A lost reply permits observation only.
Changed or uncertain results never become `PLAYBACK_VERIFIED`.

Unresolved station snapshots are retained independently of normal configuration
quota. Reconnect snapshots are conservatively retained for manual review because
their older record format has no terminal reconciliation ledger. Repeated
failures can therefore increase disk use; retention must not destroy recovery
evidence to hide that problem. No hardware approval follows from these tests.

## Manual maintenance recovery

The older manual LAB maintenance route now uses the same per-device ownership,
protection and exact hardware-profile authority as other transactions. It is
distinct from the reboot manager: maintenance may restore captured playback,
whereas the newer manager does not automatically resume playback. Its legacy
automatic scheduler remains disabled.

Before reboot, maintenance reads identity, content/state, actual volume and mute,
zone and presets. Unknown or contradictory XML and an active multiroom group
block the operation. The complete START XML is committed to a private database
backup and its SHA-256 is checked again before a durable command intent. Target
and state changes while committing that intent also block the command.

An acknowledged command is not a verified reboot. The service must observe an
unavailable interval followed by the same identity/profile. If that transition
cannot be established, or the command reply is lost, the outcome is uncertain:
no automatic replay, no restore writes and no successful-reboot timestamp.
The no-wait compatibility option reports pending readback, never success.

After a confirmed return, unchanged state needs no write. Where necessary,
actual volume and mute are restored and checked before any playback resume.
Changed topology or presets stop restoration. Content comparison includes
source, sourceAccount and location, not just PLAY_STATE. Final success requires
content/state, volume/mute, zone and stored presets to match START. Artwork,
track text and other advancing metadata do not determine restoration success.
A failed final database commit remains unverified; neither that failure nor a
lost restore response causes another hardware command.

This is software-tested behavior using simulated radios, not physical reboot
acceptance or audible-audio evidence. Front-panel/external-controller races
cannot be eliminated by a host lock. The backup is scoped to this operation,
not a full radio image; divergence requires explicit manual reconciliation.

## Browser remote and lost responses

The main browser remote now delegates command/read coordination to the
`control-state.mjs` module. It pins the stable target before an asynchronous
module load, serializes clicks and checks readback identity and volume shape.
Changing selection invalidates old generations, including an A → B → A switch;
leaving the view cancels only GETs, never an attempted POST. Updating cached
inventory can clear a stale selection without contacting another radio.

After a missing command reply, the coordinator attempts one read-only observation
if the same target/view is still valid. It does not resend the command and does
not promote a valid current state into proof that the missing reply succeeded.
The remote shows the observation with an uncertainty warning and keeps command
buttons and the volume slider disabled until an explicit successful refresh.
Failed or identity-mismatching refreshes do not release that barrier. A command
receipt explicitly marked unverified, dry-run or for another device cannot show
a successful command status. Group navigation remains available: it only opens
the separate group form and does not create a zone.

This is a document-local interaction safeguard, not durable exactly-once
execution. Reloading a browser, another tab or an external controller still
requires the server's ownership, identity, precondition and journal policies.
Software tests cover lost responses, late replies, selection changes, readback
failure and single-click behavior in Chromium and WebKit. No real radio outcome
is inferred from those simulations.

## Preset orchestration and lost responses

The HTTP router retains its compatible URLs and response envelopes. Pure XML
projection/comparison lives in `preset_contract.py`; `preset_writer.py` owns
backup, store, whole-radio comparison and delayed stability readback. Explicit
ports inject the existing guard, transport and artwork-aware content projection.
The service does not import a router; its domain failure is translated at the
HTTP adapter.

A preset POST with a transport failure has an unknown outcome. The writer stops
the remaining batch and reads `/presets`; it does not retry the POST. Success
requires the same complete target/untouched-slot comparison and delayed stability
reads as an acknowledged write. Unavailable or mismatching readback stays
unverified and retains recovery evidence. A definite HTTP rejection remains a
rejection, not proof of a successful write.

The integrity result includes bounded transport-uncertainty records (slot and
error class). An explicitly empty `sourceAccount` or artwork value is an expected
value, not permission to ignore a stale nonempty readback. The looser station
selection comparison remains separate from write verification.

The mutation ledger records revisions and intermediate states, but remaining
legacy reconciliation callers and crash recovery still need a comprehensive
cross-worker audit. Do not infer exactly-once radio execution from a local lock.

Explicit preset download/reconciliation and removal now use the same ownership,
protection and identity authorities. A complete `/presets` observation rejects
malformed XML, wrong roots/identity, duplicate slots and missing ContentItems;
invalid XML is never treated as an empty radio. Removal and profile application
also require the exact registered hardware/firmware profile before dispatch.
Unknown read-only profiles may be observed, not written.

Removal records unique hash-bound START evidence and original intent before one
`/removePreset`. A lost response is followed by readback, never a repeated write.
The target slot must be absent in two successive observations, with every other
ContentItem unchanged. An identity, backup, revision or local-state change stops
the operation. Cancellation or an uncertain result leaves the local row and
reconciliation evidence intact. The browser pins the device/slot/revision,
suppresses duplicate in-flight actions and never shows removal success for an
unverified response or applies a late result to another selected radio.

Original mutation intent is kept privately in `ConfigBackup` and checked against
the mutation hash. Explicit download can finish an uncertain operation only when
that original complete intent, original START, unchanged local previous state,
untouched slots and stable actual content agree. Today's station catalog or a
matching URL alone is not evidence of success. Legacy hash-only records cannot
be reconstructed safely: they remain pending. Actual source/account, artwork and
unknown ContentItem fields are preserved rather than re-rendered from a catalog.
Unresolved START and intent records are exempt from ordinary backup-count cleanup.

Single-slot application, cloning, saved profiles and artwork synchronization all
prepare complete intent without replacing local presets, delegate to the same
writer, and commit only the confirmed radio content. Cancellation or final DB
failure rolls back local rows and retains attempted writes as RECONCILE; it does
not replay the radio command. A short
SQLite write reservation protects the final local compare/commit. It is never
held across radio I/O, since device callbacks must be able to answer while a
radio processes a request. Partial multi-slot outcomes remain uncertain; the
single-slot download reconciler does not certify an entire interrupted profile.
There is no claim of atomic radio transactions, persistence after reboot, physical
key playability or automatic rollback. External/front-panel operations can still
race a device-side snapshot: physical compare-and-swap is not available.

Cloning copies the saved local source ContentItems, not an implicit source-radio
download. Source identity and local revision/content are frozen before the first
target write and rechecked throughout the operation. A changed source cannot
replace the original intent or be committed as if it had been sent to the target.
Both device IDs are owned, but only the target is contacted. An explicit preview
constructs no transport; the existing actual clone POST remains compatible.

Artwork synchronization observes `/info` and strict complete `/presets` first,
then changes **only** `containerArt` in the actual ContentItem. It does not add
default attributes or replace source, account, location, title or unknown fields
from the station catalog. The entire planned radio snapshot and local revision
must still match when writing begins. Even a no-op needs repeated trusted reads;
it records an observation, not a silent local-cache rebase. Skipped slots produce
a partial, unverified overall result. Failure to save auxiliary sync-display
status cannot turn a failed transaction into success or a committed write into
an invitation to retry. `status_saved` reports that separate bookkeeping result.

All three preset forms capture their target and payload, disable controls during
the request and suppress duplicate in-flight submissions. Late results are not
shown for a newly selected target. The single-slot UI requires `verified: true`,
not merely a successful HTTP response, before displaying a success message.

## Offline acceptance

The irreversible LAB factory-reset action retains explicit acknowledgement and
exact-model/build restrictions. Its service acquires the same device ownership,
rechecks the frozen database target, fresh identity/profile and standalone zone,
and persists intent before the single fixed CLI command. Baseline acceptance
reads the actual bounded regular files, checks SHA-256 and manifest consistency,
rejects symlinks/hard links/special files and validates required XML roots/IDs.
The hashes and identity are rechecked after capture and before sending. Existing
setup artifact publication now uses private temporary files, fsync and atomic
replacement without following links or truncating old content on write failure;
separate adapters have separate baseline directories.

A reset send receipt always has `verified: false`. Transport or final database
failure after invocation is OUTCOME_UNKNOWN, never NOT_SENT. The last observed
reachability is not replaced by a guessed offline state. There is no automated
probe, replay or rollback: factory configuration must be checked manually after
re-enrollment. Redacted backups cannot recover account/session secrets. This is
not a claim that the existing setup baseline is a full device-image backup, or
that physical factory-reset behavior was revalidated by these software tests.

The explicit display/direct-select action is also a playback transaction, not
a text overlay. Its preview does not construct a radio transport, even if an
old client supplies `probe: true`. Only JSON `dry_run: false` requests execution.
The service uses existing protection, exact-profile and per-device ownership
gates; it requires an unambiguous READY local-radio source and standalone zone.
It saves and hash-verifies START plus durable intent, then rechecks identity,
selection, topology and actual volume before sending one `/select` request.

After an acknowledged or uncertain POST, independent reads must match the source,
account, location, topology and unchanged actual volume/mute. `PLAY_STATE` gives
state verification, never audible-output verification. Matching buffered content
returns HTTP202/PENDING_READBACK with `verified: false`. Other unavailable or
contradictory evidence returns409/DIVERGED. There is no automatic retry, volume
write, group change or rollback; the saved snapshot supports manual reconciliation.
The state cache records actual readback, not the word `selected` from a sent POST.

This route checks stream/artwork URI policy but cannot pin the radio's later DNS
or redirect behavior. Physical controllers can still race with BASSWIESN. Its
software tests do not approve a new model, codec, source or physical display mode.

## Power and wireless boundary

`POST /api/devices/{id}/power/{action}` keeps its preview-by-default and explicit
`YES` confirmation contract. The radio's `/standby` and `/lowPowerStandby` are
**state-changing GETs**, not retryable reads. `device_power.py` owns the stable
device, checks the exact cached/live profile and requires a standalone topology.
It commits and checks a hashed START and intent before dispatch. A queued opt-in
reconnect is cancelled durably so it cannot immediately undo the user's stop.
Each power request is sent once, followed by independent observations.

Regular standby is verified only when the radio reports `STANDBY`/`STOP_STATE`
with unchanged volume, mute, topology and stored presets, and the final record
commits. An acknowledgement without that readback is not success. A lost reply
returns HTTP 202 / `PENDING_READBACK`; no second command or automatic wake follows.
Low-power always remains unverified: network loss, an acknowledgement or ordinary
standby does not establish deep sleep. Physical wake and manual reconciliation
may be necessary. These software checks do not constitute a hardware retest.

`GET /api/devices/{id}/wireless-profiles` now performs identity-bound reads only.
The old `?survey=true` request returns 409 before transport construction, with
instructions to use the separate `POST .../wireless-survey` endpoint. That
endpoint defaults to preview and requires LAB mode, an admin session, CSRF/origin
protection in required-auth mode, explicit `YES`, an exact known profile and
standalone topology. No automation-token scope grants it. The radio command is
sent once with a durable, integrity-checked previous network observation and
intent. Its completion schema is not established: the result remains HTTP 202,
`verified: false`, with any observed network divergence stated separately.

Wireless reads expose an allowlisted network projection, not unfiltered profile
XML. Missing profile counts and model-specific multi-profile support are unknown,
not guessed. Unknown fields and raw survey replies are withheld; pattern-based
redaction alone cannot protect arbitrary credential fields. Adding a profile
or a native provider station uses the separate manual-command contract below.

Power, key and survey START backups have separate retention contracts. Only a
latest exact verified completion with the matching device and SHA-256 permits
quota-based removal; low-power, uncertain, interrupted and survey observations
are retained for reconciliation. This is deliberate and can consume storage;
there is no automatic claim that these records are no longer needed.

## Native station and Wi-Fi transitions

The existing `POST /api/devices/{id}/station/add-native` and
`POST /api/devices/{id}/wireless-profiles` URLs delegate to the typed
`manual_radio_commands` service. Native addition can start audio; adding a
wireless profile can remove the radio from the current network. Neither the
stored-station comparison contract nor durable profile-switch verification is
established sufficiently to certify these commands as complete.

Both routes now default to **preview only**; this tightens the old wireless
behavior. Preview validates the request locally and shows a redacted shape,
without constructing a radio transport. Execution requires a JSON boolean
`dry_run: false`, LAB mode, `confirmation: "YES"`,
`acknowledge_manual_recovery: true`, and a fresh `operation_id` consisting of
32 lowercase hexadecimal characters. Required-auth installations additionally
require an admin session with origin/CSRF checks. No API-token scope grants
these routes. An operation key identifies one attempted command, not evidence
that it succeeded: reusing it never dispatches again, even with a different body.

Before dispatch the service verifies protection, the exact cached/live device
profile and stable identity, owns the device across workers, and reads its
playback, volume, topology and presets. Only a standalone radio is allowed.
Native addition requires the exact source/account to be uniquely READY;
wireless addition requires bounded network and active-profile observations.
The service durably stores a hashed START and intent, rechecks the target and
state around both commits, and cancels queued opt-in reconnect before a single
POST. Missing or changed evidence prevents the command. Unknown profiles do
not become writable merely because LAB mode is selected.

An acknowledgement, matching selected source, or matching active SSID is
reported only as an observation. Outcomes remain HTTP 202 / `PENDING_READBACK`,
`verified: false`, `persistence_verified: false`. Lost replies get bounded
read-only observation, never replay. Identity mismatch or a failed final commit
remains divergent. Cancellation releases ownership but leaves durable intent.
The result distinguishes unintended changes to volume/topology/presets from
the requested transition. It cannot certify audible audio, persisted station
identity or survival across reboot.

START is private scoped recovery evidence, not a complete provider database or
Wi-Fi credential backup. Passwords, opaque station tokens and raw response bodies
are not echoed or recorded; network snapshots are allowlisted. Automatic rollback
cannot reconstruct those secrets and is not attempted. Pending native/profile
backups cannot be evicted by the ordinary count-based cleanup, even if a stray
ledger record claims an unsupported completion phase.

The LAB forms keep preview visible and checked. Actual execution confirms the
captured target, freezes controls and blocks duplicate submission. Late results
cannot appear as another radio's state. Password/token inputs are cleared after
the request. An uncertain attempt blocks another action for that target in the
current page until explicit manual review; this browser aid is not the server
security boundary or a persistent recovery approval. Reading status alone does
not clear it. Real profile switching and station persistence remain **MANUAL
REQUIRED**, with an independent recovery route prepared before testing.

Tests use real local child processes for contention and abrupt exit, plus
controlled transports for lost replies, unchanged state, missing readback and
partial preset batches. Authentication tests combine real sessions/tokens with
the actual settings service and a simulated radio: authorization never bypasses
protection, exact-profile, backup or readback gates. These results are software
verification only. No test here contacts or certifies a physical radio.
