# AirPlay bridge — bounded LAB sessions

Status: **unfinished integration, no production activation**. Permanent receiver
integration and full Apple multi-device acceptance are not ready. This document records
the intended contract and bounded lab evidence, not working release functionality
or an installation guide. On 2026-10-09 a genuine iPhone-to-Pi-to-SoundTouch 30
trial produced user-confirmed audible, stable music. This is single-radio evidence,
not a production or synchronized-multi-receiver acceptance.

## Intended user experience

The Raspberry Pi presents one virtual receiver per explicitly enabled,
unprotected SoundTouch radio. Receiver names begin with `AP2 `, for example
`AP2 Example Radio`. An Apple sender can select one receiver or multiple
receivers. The audio reaches the Pi first; firmware on the radios is unchanged.

The objective is selection in the **Apple picker**, not a single virtual room
whose membership must be manually configured in BASSWIESN. The latter is a
simpler experiment but does not satisfy the full requirement.

Title, artist, album and optional artwork should remain associated with the
correct receiver session. Text should use the existing user-selectable display
fields and ordering, including the clock. Missing metadata must not interrupt
audio, fetch arbitrary sender-supplied URLs, rewrite presets or retain the
previous sender's song. Artwork support requires its own bounded image cache
and validation; the prototype intentionally discards picture payloads.

## Two synchronization boundaries

```text
Apple sender
    |
    +--> virtual AP2 receiver A --+
    +--> virtual AP2 receiver B --+--> verified session/group coordinator
                                 |
                           one elected audio feed
                                 |
                      continuous PCM -> MP3 relay
                                 |
                        SoundTouch Zone master
                                 |
                        SoundTouch Zone members

receiver metadata --> per-session display projection --> provider updates
receiver volume   --> explicit per-radio volume policy --> verified readback
```

This is a candidate architecture, not a verified topology. Independent Apple
sessions must remain independent: they need separate relays and cannot be
merged into the same Bose zone. Matching titles, sender IPs or start times are
not proof that sessions belong to the same Apple group.

A shared PCM/MP3 relay loses the original AirPlay presentation timestamps at
the HTTP player boundary. Bose can synchronize its own members behind a
master, but simultaneous standalone HTTP players have independent buffers.
Synchronization with native AirPlay receivers, HomePods or other downstream
zones is **not established**. No UI may label that property as supported based
on successful advertisement, pairing or audio decoding alone.

The bounded controller now implements source selection, member changes and
restoration with actual device readback, only after an explicit authorized
session start and verified native input. This is not an always-on service.
An Apple deselection does not magically make SoundTouch member removal reliable.
Master departure currently requests a handoff and ends the old ownership grant;
it does not automatically elect and start a replacement master. Regrouping and
rollback require separate acceptance tests.

## Explicit local session control

The LAB control panel can start and stop **already prepared** session profiles.
It does not provision receivers. Reading status, loading the page or starting
BASSWIESN never launches a receiver, discovers radios or selects a source.
A single-radio profile no longer requires a second receiver. Multi-radio
profiles still wait for their exact configured initial group; this is not yet
arbitrary grouping of permanently available receivers.

The web application talks to an independently launched, unprivileged session
service over a private Unix socket. The service accepts only `status`, `start`
and `stop`, with a fixed profile name and request ID. Requests cannot contain
shell commands, filesystem paths, radio addresses or relay URLs. Unix peer
credentials must match; the human write boundary additionally requires an
administrator session, same-origin request, CSRF token and LAB mode. Automation
tokens cannot authorize this new control path. The application receives no
Docker socket or root privileges.

Start means **accepted**, not audible playback. A durable target reservation is
written before a worker is constructed. Repeating the same request ID cannot
repeat a write, and changing the request ID cannot revive a consumed relay or
controller capability. Concurrent sessions may not share a radio, listener,
receiver socket or result file. A lost start response leads to status polling,
not another start request.

Stop signals the existing owner to restore captured state; it does not cancel
the owner midway through a radio transaction. `FINISHED` requires verified
restoration (or no attempted radio write) and cleanup of the exact temporary
station. An uncertain result remains `RECOVERY_REQUIRED`, and its radios stay
reserved across service restarts. Recovery is not an unchecked browser reset.
Journal failure stops active workers and prevents queued workers from starting;
it never silently declares a clean shutdown. The safety lifetime remains
bounded: this work does **not** remove the existing ten-minute pilot limit.

Software tests exercise the real local socket and controller against simulated
radios, including independent single-radio playback, restoration, duplicate
starts, lost responses, unsafe files, full storage and interrupted shutdown.
Chromium tests cover German/mobile and English/desktop operation. These tests
do not establish acoustic output or Apple acceptance. The separate automated
sender exercises native decoding as documented in the module README.

Still required before permanent use: production receiver provisioning and
lease supervision, fresh session/relay rearming, coordinator handoff, and
end-to-end regression of that integrated deployment. Only after those software
gates pass should the final iPhone acceptance begin. The current panel is a LAB
control surface, not a claim that only a listening test remains.

## Why separate receiver isolation is under consideration

[Shairport Sync's AirPlay 2 documentation](https://github.com/mikebrady/shairport-sync/blob/master/AIRPLAY2.md)
warns against multiple receivers at the same IP address. NQPTP owns UDP ports
319/320 for timing. Running several named processes or changing only RTSP
ports is therefore not an adequate implementation plan.

The candidate deployment uses a separate network namespace and address for
each receiver, its own Shairport/NQPTP pair, and isolated timing IPC. Receiver
identities must be unique, stable local virtual identities, never copied from
a radio or an Apple device. Upstream
[multi-instance discussion](https://github.com/mikebrady/shairport-sync/discussions/1444)
contains isolated-container reports, but those are not proof of compatibility
with this deployment or current Apple senders.

Required checks before provisioning:

- a valid router-issued DHCP lease, or an explicitly reserved static address;
  never guess unused household addresses;
- usable wired parent interface and multicast routing;
- separation of each receiver's PTP ports and shared-memory namespace;
- a private, authenticated control channel to BASSWIESN;
- Pi-to-receiver communication despite macvlan host isolation, if macvlan is
  selected; do not silently modify the host's default route;
- receiver resource budgets and a full reversible deployment record;
- dependency commit/image pins, licensing notices and build verification.

The normal application starts no container, multicast advertisement or receiver.
The explicitly provisioned receiver pilot is a separate, time-bounded container;
the normal hardened BASSWIESN container is not granted new privileges or host
networking. Pilot details and remaining deployment requirements are described in
[the experimental module](../modules/airplay-bridge/README.md).

### Automatic address assignment

The intended default is DHCP, not a ping sweep followed by taking an apparently
unused address. Each isolated virtual receiver should have its own stable,
locally generated network identity and a DHCP client. The router assigns the
lease; the module tracks renewal, address changes and expiry. No receiver may
start advertising before its lease and local conflict checks are valid. On
lease loss, it must stop using that address and withdraw its advertisement.
Protected addresses and known device/host addresses remain excluded.

The isolated pilot now implements lease validation, renewal monitoring and
fail-closed expiry. A Docker-internal DHCP fixture verifies successful startup
and shutdown on lease withdrawal; a separate ARM64 Pi pilot has acquired a
router-issued lease. Production provisioning and reconnect behavior remain
unverified. This is not a capability provided by the inventory preview.
Docker's default macvlan
address management must not be mistaken for the household router's DHCP
server. The integration must also isolate the DHCP client's route/DNS changes
from the Pi host and existing BASSWIESN services.

A user-supplied static range is an optional fallback only when reserved for
this purpose and excluded from other allocation. A scan cannot reserve an
address and cannot prove that an offline device does not own it. Do not
silently fall back to static allocation when DHCP fails. Similarly, a preferred
range does not force the router to offer addresses from that range; it must be
configured or reserved at the router if that restriction is required. No router
configuration change is implicit in enabling automatic allocation.

References: [DHCP protocol](https://www.rfc-editor.org/rfc/rfc2131.html),
[IPv4 conflict detection](https://www.rfc-editor.org/rfc/rfc5227.html),
[Docker macvlan networking](https://docs.docker.com/engine/network/drivers/macvlan/).

## Continuous silence, not an empty HTTP response

The relay must send real decodable audio frames. During a short active-session
pause or input gap, zero-valued PCM samples can feed the **same encoder** that
also receives music. The stream URL and encoder remain stable through that
transition. An empty response, stopped TCP stream or zero-length body would
instead look like missing audio or a disconnected source.

`PCMRelay` implements only the bounded sample switch:

- 20 ms frames, explicitly agreed rate/sample format/stereo layout;
- Shairport 5 AP2 pipe defaults: S32LE at 48 kHz; explicitly configured
  S16LE/44.1 kHz is also supported;
- fragment handling when FIFO reads split sample frames;
- 200 ms default buffer, with fail-closed overflow rather than unbounded drift;
- a 30-second maximum unexpected input gap; explicit pause can use a separate
  bounded budget (300 seconds in the current pilot, at most 900 seconds);
- old-session rejection and buffer clearing on pause/end/replacement;
- no disk recording, network or device control.

`ClockedMP3Relay` adds an explicitly started asyncio scheduler and one encoder
process. Its offline tests verify decodable clocked silence, identical bounded
fanout, silence expiry, cancellation and rejection of a slow consumer. A missed
clock deadline or full buffer ends the session instead of accumulating drift.
The runtime itself has no HTTP listener or radio transport.
`PCMRelay.frame()` itself is not a wall-clock scheduler.

Because the PCM format is explicitly known, the encoder does not spend several
seconds probing the live input pipe. A local comparison reduced time to first
encoded packet from approximately 4.3 seconds to 0.09 seconds. This removes an
unnecessary upstream delay; it does not predict the radio's buffering latency.
Loopback tests decode the resulting MP3 and check that music, silence and
pause/resume boundaries carry the intended synthetic signal.

`PCMHTTPRelay` is an explicit, one-session HTTP adapter. A real socket peer
check and random session URL restrict access to one preflight-authorized
target. No forwarded-header trust, redirects or radio API requests are involved.
It accepts HEAD without starting an encoder and consumes the session on GET.
FIFO input received before the authorized GET is discarded, not replayed later.
End-of-input closes the encoded stream; disconnection and buffer/encoder
failure stop it. A slow consumer cannot accumulate unbounded historical audio.
Offline Docker tests cover FIFO input, guarded HTTP output and MP3 decoding.
Radio selection/readback/restore and real downstream behavior remain separate
hardware acceptance gates. This pilot implements bounded pause handling, but not
production reconnect orchestration or an enable endpoint.

The HTTP stream starts with a closed **live-audio safety gate**: it supplies
clocked digital silence but discards incoming music until the trusted owner
explicitly releases it. SoundTouch source selection can restore a remembered
volume even after a successful low-volume preflight. Therefore the owner must
mute before selection, establish ownership of the selected stream, reapply and
verify the safe volume and mute after selection, and only then release PCM and
unmute. An earlier hardware attempt returned a mismatching ContentItem location even
while the authorized radio was fetching the session's silent MP3 stream. The
gate correctly stayed closed; there was no audible-output PASS. The revised,
offline-tested controller requires a fresh, error-free, one-use URL/real-peer
transport observation together with identity, `LOCAL_INTERNET_RADIO`,
`PLAY_STATE`, volume and mute readback. It records a mismatching ContentItem
echo separately. Neither a generic PLAY_STATE nor an open TCP port establishes
session ownership. The successful 2026-10-09 trial used the stricter matching
ContentItem path: the decoded station descriptor matched the fresh relay URL,
and identity, source, PLAY_STATE and volume were read back before releasing music.
It does not validate accepting a mismatching ContentItem through another path.
The gate
cannot be released by an HTTP subscriber. Offline tests verify that pre-release
nonzero PCM never reaches the encoder and cannot be replayed after release.
Silence has the same bounded lifetime as any other missing-input interval.

Selection may return `PENDING_READBACK` after the owned source and volume have
been confirmed but while the radio still reports `BUFFERING_STATE`. That is not
a reason to send `/select` again. `StartupReadbackGate` models a bounded window
of additional reads. Identity/source changes, unsafe volume, a zone conflict,
relay failure or expiry require reconciliation. Only actual `PLAY_STATE` with
the owned stream at a safe volume confirms readiness. The prototype still keeps
live music gated until that check; waiting cannot renew the silence lifetime.
In the successful single-radio trial, the additional readback observed the
transition from buffering to actual PLAY_STATE. Only then was live PCM released.
The user confirmed audible, stable output. Startup was still noticeably buffered;
the faster encoder does not make the downstream player a low-latency receiver.

Silence is not a permanent all-day stream that wakes every configured radio.
An idle advertised receiver should leave the radio's current playback alone.

### Activity expiry is not stream teardown

A subsequent real pause test exposed a lifecycle error: after `paus`, the
receiver emitted `aend` without `pend`. Treating that activity notification as
stream termination closed the one-use relay and caused the controller to restore
standby. The user confirmed audible pause but could not resume that trial.

The corrected adapter distinguishes the activity monitor from player lifetime.
`abeg`/`aend` neither terminate the relay nor discard its slider/track snapshot.
Only `ssnc/pend` is treated as a player-stream end; process failure, lease expiry
and bounded silence still stop the pilot independently. An activity event cannot
renew those limits or reopen a terminated session. Regression tests replay the
pause/activity-expiry/resume sequence; long pauses and production reconnect
policy still need separate acceptance.

The corrected receiver subsequently passed a real short-pause trial: the user
confirmed audible resume, and the same HTTP stream survived `paus`, `aend`,
`abeg` and resume events without selecting the source again. The observed pause
was approximately 11 seconds. In the same trial, Apple mute/unmute produced
actual radio readbacks 5/unmuted -> 0/muted -> 5/unmuted, with user confirmation.
A sender slider value of 100 was capped to 5. This validates those bounded
control transitions, not an uncapped loudness curve or long-pause behavior.
The updated pilot separates an explicit pause from unexpected input loss.
Pausing permits bounded silence on the same encoder/HTTP connection; toggling
pause/resume cannot renew either budget without actual input. A separate worker
checks a monotonic supervisor heartbeat and stops after five seconds of stale
supervision, even during an otherwise permitted pause. The supervisor renews
only after checking its children and DHCP lease. This does not replace a future
production controller's ownership lease.

Offline verification covers a simulated two-minute pause and a real 31-second
wall-clock pause through FFmpeg and HTTP: decoded audio resumes on the same
connection after the silent interval. That is software evidence, not a new
long-pause hardware acceptance. Reconnect after a terminal session still requires
a fresh owner, stream grant and identity/readback checks.

This distinction matches the upstream
[activity monitor](https://github.com/mikebrady/shairport-sync/blob/08af668a5d17b4714da38981dea4c9039263a4cc/activity_monitor.c)
and [event documentation](https://github.com/mikebrady/shairport-sync/blob/08af668a5d17b4714da38981dea4c9039263a4cc/MQTT.md).
The inspected source is not proof of byte-for-byte provenance for the pilot's
upstream binary, which reports a dirty build version.

## Metadata contract

Shairport's documented `core` text events include `minm` (title), `asar`
(artist), `asal` (album) and `asgn` (genre). The adapter accepts these only
inside `ssnc/mdst` and `ssnc/mden` batches. It prepares one atomic snapshot for
the existing BASSWIESN display composer. Its local generation and sequence
numbers reject queued events from an old session.

Unbatched metadata is currently ignored. Every completed batch replaces the
track snapshot; missing artist/album fields clear rather than leaking from the
previous track. This conservative policy needs comparison with real senders'
complete and partial update behavior before production use. Delayed metadata
also needs alignment with the downstream audio buffer, not just receipt time.

Only bounded decoded display fields enter the adapter. Raw RTSP headers,
client names/addresses, pairing values, keys, arbitrary URLs and artwork are
not stored. The adapter does not parse or log secret-bearing receiver output.
Genre can use the existing optional information field; album remains distinct.

The projected radio source is `LOCAL_INTERNET_RADIO`, not native `AIRPLAY`.
Bridge success must not be presented as a firmware AirPlay unlock. The
`AIRPLAY_BRIDGE` provider label is an internal application label.

### Display projection and ownership

An offline-tested private projection now connects the metadata adapter to the
actual BMX now-playing and reporting responses. It binds a short-lived record
to the exact radio identity and address, internal LAB station, selected stream
fingerprint, controller process identity and receiver lifetime. The controller
must already hold the central device-ownership lock before publishing. Merely
receiving metadata does not select a source or authorize a radio write.

The projection is shared through the existing database because the web and
device-facing servers can be separate processes. It expires after ten seconds;
late cleanup from an old session cannot erase a newer session. Unknown, stale,
malformed or mismatched observations cannot renew it. End clears the fields.
The internal one-use audio relay is never probed for ICY metadata, and an empty
projection does not fall back to old radio-song data. Title-only Apple tracks
are supported without weakening the existing ICY title/artist policy.

Existing display preferences, field order and clock composition are reused.
Loopback tests cover the real provider routes, cross-process database reads,
session replacement, lease expiry and malformed metadata batches. Delivery to
the physical display still needs the trusted controller integration and a
hardware test; this change does not claim either. Artwork remains unsupported.

Sources:
[metadata events](https://github.com/mikebrady/shairport-sync/blob/master/MQTT.md),
[pipe output](https://github.com/mikebrady/shairport-sync/blob/master/audio_pipe.c).

## State of the implementation

| Component | Current state |
| --- | --- |
| Device inventory with `AP2 ` names | Read-only preview |
| Protected-device exclusions | Central guard reused; no device transport |
| Receiver processes and DHCP | Isolated Linux/ARM64 pilot; router DHCP verified |
| Apple-to-Pi audio input | Single receiver verified; two simultaneous iPhone selections and decoded PCM observed in an input-only trial on 2026-10-13 |
| Apple-to-Pi-to-radio audible output | One bounded SoundTouch 30 trial: user confirmed audible, stable music; not a long-duration test |
| Input text metadata and session end | Title/artist/album/genre observed; end cleared metadata, PID stable |
| PCM silence/audio switching | Offline implementation |
| One MP3 encoder spanning silence/music/silence | Offline FFmpeg test |
| Clocked encoder, bounded fanout and shutdown | Offline runtime and one-session HTTP tests |
| Metadata normalization and display ordering | Offline implementation |
| Apple volume event parsing, cap and stale-session rejection | Offline tests; capped volume/mute/unmute read back and audibly confirmed in a single-radio trial |
| Artwork cache | Not implemented |
| Radio display update delivery | Private, expiring projection into actual BMX responses software-tested; production owner integration and physical display acceptance pending |
| Apple group membership adapter | Opt-in source-pinned native observer passed a bounded genuine-iPhone dual-input/member-removal trial on a physical Pi; production coordinator integration and independent-sender acceptance pending |
| Target-bound HTTP relay and FIFO attachment | Implemented in isolated pilot; offline end-to-end fixture PASS |
| Bose zone controller | Bounded, explicit-owner service implemented against the existing backup/readback transactions; simulated create/join/remove/end tests. Live receiver/relay-owner wiring, automatic master handoff and hardware acceptance pending |
| Per-radio Apple volume control | One radio: mute/unmute and cap verified; full range and independent multi-receiver controls unverified |
| Pause/resume | Approximately 11-second hardware pause/resume passed; separate bounded long-pause policy and 31-second encoder/HTTP test passed offline; long-pause hardware acceptance pending |
| Hardware backup/restore session ownership | Captured stable state restored in isolated controller trials; not integrated into the normal application |
| UI and deployment installer | Not implemented |

`GET /api/airplay-bridge/status` reads known database devices only. It reports
`enabled=false`, `OFFLINE_PROTOTYPE`, unprovisioned receivers and blockers.
There is deliberately no enable endpoint, advertisement, discovery, timer,
background task, persistent setting or startup network operation.

The application status describes the **integrated feature**, not a replay of
lab results. The successful single-radio output pilot does not make the normal
application an enabled receiver. Earlier failed/cancelled attempts remain useful
failure evidence; the later audible result supersedes only the statement that
single-radio output has never been confirmed. The receiver was removed after
the Apple session ended, its temporary station was removed, and captured presets,
volume/mute, bass, zone and settings matched the pre-test state. No complete
volatile-state equivalence is claimed. A subsequent trial confirmed short
pause/resume and bounded user-operated volume/mute changes. Grouping,
synchronization with other Apple outputs and extended-pause/reconnect remain
unverified.
PCM reception alone also does not establish the exact
negotiated AirPlay transport or source codec; raw session secrets were not
logged to obtain such a claim.

### Group-coordination evidence boundary

In the inspected Shairport source, a PTP SETUP can supply `groupUUID` and
`groupContainsGroupLeader`. The receiver advertises the session group as `gid`,
but falls back to its own persistent receiver identity when no session group
exists. Consequently, a `gid` value alone is not proof of an active Apple group.
The same source's SETPEERS handler acknowledges the request while its old peer
list processing block is commented out. An accepted SETPEERS response therefore
must not be treated as a membership event by BASSWIESN.

This is a source observation, not an assertion that the pinned container build
has identical internals. A safe coordinator still needs a version-matched,
session-scoped event contract, verified with two real Apple-selected receivers.
It must not infer grouping from song titles, sender addresses, or a shared
persistent identifier. Raw RTSP logging is not an acceptable shortcut.

The 2026-10-13 input-only trial adds a narrower runtime observation: two real
iPhone-selected receivers decoded PCM and advertised the same live `gid`.
Deselecting one produced `pend`, stopped its audio counters, cleared its display
and volume snapshot, and changed its advertised identifier. The remaining
receiver continued decoding audio; the user confirmed continued playback. On
returning output to the iPhone, that receiver also ended and cleared its fields.
Only exact test services were queried through their private local Avahi buses;
no radio transport was created. Separate fallback identifiers were observed
after teardown. No raw session trace or audio was retained.

This verifies simultaneous selection and independent input teardown, not a
trusted group-control API. DNS-SD observations can be stale or spoofed and are
not authority to change a Bose zone. The production adapter must bind group
events to the owned receiver process/session and test replacement, loss and
independent senders before it can drive zone transactions.

Source: [Shairport RTSP implementation at the inspected commit](https://github.com/mikebrady/shairport-sync/blob/08af668a5d17b4714da38981dea4c9039263a4cc/rtsp.c).

A separate opt-in native observer now exports connection ownership and a hashed
group label directly from a source-pinned receiver. Its real SETUP, teardown,
connection interruption and TCP-loss paths are tested on isolated loopback,
including an emulated ARM64 build. No Bonjour fallback enters this channel.
The modified ARM64 build subsequently passed a bounded genuine-iPhone trial on
a physical Pi on 2026-10-13. Both selected receivers reported the same native
group hash, decoded nonzero PCM and received text metadata. Removing one
cleared its group and metadata without interrupting the remaining input;
returning to the iPhone cleared both. Both temporary receivers were removed,
production BASSWIESN and host routes were unchanged, and no radio was contacted.
This is input acceptance, not completed zone integration or audible group
synchronization. Build and trust boundaries are documented in
[the module's observer section](../modules/airplay-bridge/README.md#opt-in-native-group-observer).

## Safety and session ownership

### Bounded group owner (software-tested, not production-enabled)

`group_session.py` binds native observations to the exact locally enabled
receiver/device set, receiver nonce/PID and independent supervisor heartbeats.
It never uses song text, a Bonjour fallback or sender IP to infer membership.
Only members matching the chosen receiver's active native group are planned.
Missing samples, unknown membership and new receiver connections fail closed;
they are not treated as an empty group or permission to reuse an old stream.

`group_zone.py` is an explicit application service, not a new web endpoint or
startup task. It acquires the existing cross-worker device ownership, freezes
identity/profile/address inputs, refuses pre-existing foreign zones and saves
a durable hashed baseline. It reuses `creation_scope`, `change_zone` and the
settings writer for topology and latency changes. All affected radios must
provide matching readback. Lost POST responses are reconciled without replay;
external source/topology changes, expired ownership and failed audit commits
stop the operation and retain reconciliation evidence.

No volume, preset, key or source-selection writes are issued by this service.
Firmware volume normalization is returned as an observation, never silently
overwritten or described as preserved volume. Successful topology readback
does not release audio: the outer playback owner still has to verify the
actual volumes, source and target-bound relay. Closing a PCM gate cannot recall
already encoded or radio-buffered audio; it is not a verified hardware mute.

Normal member removal uses distributed readback and restores only latency
settings the session changed, conditional on their expected previous value.
When the relay master leaves, the owned zone is dissolved and the result is
`HANDOFF_REQUIRED` if another receiver remains. Automatic election, new relay
grant/selection and subsequent playback are **not implemented** by this path.
Failed/expired owners do not perform blind rollback; source/standby restoration
remains the outer playback owner's responsibility.

The optional receiver `controller_nonce` mode adds a separate, five-second
local controller lease to the actual audio worker. In this mode an old
`output-release` file has no authority. Sequence, nonce, clock and hold-epoch
validation are mandatory; missing/invalid owner observations close the worker.
The worker reports the accepted sequence/epoch and actual gate state. An
external controller must wait for its hold acknowledgement before a topology
write. `control_owner.py` now waits for an exact, nonce-bound acknowledgement;
`receiver_port.py` and `unix_ipc.py` connect it to a fixed private receiver IPC
protocol. `group_controller.py` refreshes native input independently while the
foreground performs the existing zone transactions. Changed membership during
a write invalidates that transaction instead of silently retargeting it.

The optional host `namespace_ipc.py` adapter accepts four fixed operations over
a private Unix socket. It checks peer UID and the immutable container/image
identity, non-root user, read-only root and dropped capabilities. It neither
exposes a Docker socket nor accepts arbitrary commands, paths or container names
from the application. Importing these modules launches nothing.

`pilot_source.py` adds an explicit standby-only test scope: complete captured
state, durable hashes, existing guarded source selection, safe-volume readback
and restoration only while the current state still belongs to that test.
Preset/source/volume changes by another user require reconciliation, not an
automatic overwrite. The pilot cap is not a new production volume default.
Zone-induced volume normalization is still a hardware acceptance boundary.
`GroupController.run_owned` keeps the acknowledged hold and receiver
supervision alive while preparing that source. Waiting for input does not
construct a radio transport. The preparation is pinned to one native plan;
changed membership aborts it rather than retargeting an in-flight selection.
The combined simulator exercises source selection, zone creation, metadata
projection, teardown and restoration through the real application services.
A standalone self-coordinator after zone teardown is treated as standalone;
foreign members or changed peer addresses are not normalized away.

The actual FIFO/FFmpeg/HTTP worker and owner acknowledgement were exercised on
native Raspberry Pi ARM64 with networking disabled. This exposed and corrected
a shutdown ordering bug: active HTTP handlers must close before awaiting server
shutdown, and encoder cancellation must close its input pipe. No radio or Apple
sender was involved in that offline test. The bounded private host launcher now
assembles receiver provisioning, private IPC and the application owner; paired
acceptance remains limited to the observations below. The production Pi app also
needs a qualified metadata-consumer update before physical display delivery
can be claimed. There is no automatic radio activation or production maturity
change.

Every future hardware operation must reuse the central protected-device guard
and verify `/info` against the registered identity before accessing the target.
All affected devices require a state/preset/zone/volume backup before writes.
An active session owns a generation-tagged lease. A late stop from an old
sender cannot stop new playback or restore over a user's subsequent manual
change. Writes need explicit preconditions and post-write readback.

Test volume defaults to a cap of 1; a different bounded level requires an explicit
choice. The successful single-radio trial used the user's requested level 5.
An initial Apple slider value projecting to 20 was limited to 5 and the physical
radio value was read back. This first trial proved the startup cap; a later trial
confirmed interactive mute/unmute at levels 0 and 5. Other volume points remain
unverified on hardware. Production volume must not jump to Apple's default
maximum, change unrelated radios, or be attenuated twice by receiver PCM and
radio controls. Whether Apple controls PCM gain or the physical radio volume
must be one explicit policy, with per-member volume tested independently.

The prototype decoder accepts only the numeric source-slider value from the
receiver's `pvol` event. `SessionVolumeCursor` rejects stale generations and
duplicate or out-of-order sequence numbers. `project_volume` maps the source
slider to a bounded radio control position and a separate mute intent; this is
not a calibration of acoustic loudness. A controller must apply the value only
after the startup safety checks and verify each hardware change. The isolated
pilot disables receiver-side PCM attenuation to avoid applying the same volume
change twice. Parsing and safety-cap tests pass offline; a bounded iPhone-to-radio
mute/unmute trial also passed. Full-range/multi-receiver volume remains an
acceptance gate, not a released feature.

Do not log received audio or pairing data. Diagnostic output should contain
only durations, error classes, buffer sizes, state changes, hashes and
anonymized identifiers. No authentication or manufacturer identity bypass is
part of this module.

## Gates before the bridge can leave disabled LAB preview

These are future bridge acceptance gates, not completed 3.0.0 features.
The stable 3.0.0 core is released with this module disabled.

1. Build/pin the receiver stack; prove one isolated Apple receiver and bounded
   PCM/metadata output with no radio attached. Bounded single- and dual-input
   pilots passed; production reproducibility/provenance remain open.
2. Prove two separately named receivers are individually and simultaneously
   selectable on an actual Apple sender. Verify isolation and session release.
   The bounded input-only trial passed on 2026-10-13; this is not grouped-radio
   synchronization acceptance.
3. Identify group membership using a supported structured backend contract.
   The inspected upstream source parses `groupUUID` and peer information, but
   the ordinary text metadata events alone do not expose a complete verified
group contract. Do not infer grouping from song text or dump RTSP secrets.
4. Deliver one relay to one backed-up radio, then a Bose pair; measure latency,
   buffering, audible synchronization, resource use and restoration.
5. Exercise independent streams, Apple multi-selection, individual volume,
   pause/resume, member/master leave, reconnect, metadata and artwork changes.
6. Run failure/cleanup tests: receiver crash, encoder crash, network loss,
   radio source change, Pi reboot, expired lease and partial zone restore.
7. Complete UI, DE/EN text, deployment/upgrade/rollback, privacy/licensing,
   targeted regressions followed by release/fresh-install gates.

Lossless end-to-end, Atmos, AirPlay video, Siri/Home automation behavior,
downstream synchronization with native AP2 devices and every possible remote
control function are not promised. The proposed MP3 output is lossy. Feature
claims will follow actual evidence, not the `AP2 ` prefix.

## Offline verification

The FIFO reader budgets each read against free space in the existing 200 ms
PCM queue. The pinned receiver uses a blocking pipe writer, so a burst can be
backpressured without growing the queue or silently dropping samples. Held or
paused input is still discarded, not replayed after release. Direct callers
that overfill the buffer still fail closed. Synthetic tests exercise exact byte
ordering, a two-second unpaced burst through the real MP3 encoder, cancellation
and pause/hold draining. This fixes a demonstrated input-flow mismatch; it is
not proof of long-running Apple clock stability.

When an authenticated owner is present, native input end may retain **silence
only** on the existing HTTP connection for at most 120 seconds while verified
zone teardown runs. The independent five-second controller lease still applies.
The grace period cannot be renewed, accept another HTTP client or resume input.
Without a controller, input end closes the stream immediately. Device state
still requires readback: keeping HTTP open does not establish successful cleanup.

From the repository root, with the existing test environment:

```sh
.venv/bin/python -m pytest -q tests/test_airplay_bridge_300.py \
  tests/test_airplay_receiver_contract_300.py tests/test_airplay_relay_runtime_300.py \
  tests/test_airplay_http_relay_300.py tests/test_airplay_volume_300.py \
  tests/test_airplay_fifo_input_315.py
```

The optional FFmpeg tests create and decode synthetic audio in memory. They
never use an audio output device, open a LAN socket, start AirPlay or modify a
radio. Software tests continue to enforce the project's no-LAN transport guard.
