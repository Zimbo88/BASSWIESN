# Experimental isolated AirPlay pilot

This is development infrastructure for BASSWIESN 3.0.0, **not the released
module or a supported installation procedure**. It is not started by Compose
or the application. It cannot select a radio or create a Bose zone. Default
input-only mode counts and discards PCM. Explicit output mode can serve a
single session as HTTP MP3 to one preflight-authorized target. It never records
audio. Target verification, backup, selection and restore belong to a separate
trusted controller, not to the receiver container.

## Isolation and lifecycle

The pilot uses a pinned upstream Shairport Sync image, with a separate D-Bus,
Avahi and NQPTP instance per receiver. It expects an isolated network namespace
and a DOWN interface provisioned by a trusted controller. IPv6 must be disabled
before provisioning. Before bringing the interface UP, the network owner installs
both firewall drops and prohibit routes for every configured excluded address.
No default route or DNS resolver from DHCP is installed.

With `external_network=true`, that owner runs outside the receiver. The receiver
requires a non-root UID, zero effective/permitted/bounding capabilities and a
read-only configuration mount. It waits for a bounded DHCP lease published by
the owner, validates its expiry, subnet, server and exact firewall fingerprint,
and checks exclusion routes locally. It cannot configure the host network or
access a Docker socket. An ARM64 pilot exercised this mode with D-Bus, Avahi,
NQPTP and Shairport all unprivileged. The older internal-owner mode is development
infrastructure, not a production deployment recommendation.

Output mode excludes its one target from the blanket deny set, but allows only
incoming HTTP connections on port 8098 and established responses from that
port. Other traffic to that target is dropped; all remaining exclusions stay
blocked. The HTTP server additionally checks the real socket peer and an
unguessable session URL. Forwarded headers cannot override the peer check.
HEAD does not consume the session; the first GET consumes it and subsequent
GETs receive 410. There is no arbitrary redirect, URL proxy or control API.
Tokens stay in private configuration and never enter diagnostic snapshots.

An accepted GET initially receives **digital silence only**. SoundTouch can
restore a remembered volume during source selection, so a volume check before
selection is insufficient. The trusted owner must mute before selection and
verify session ownership, actual volume and mute afterward. The latest
offline-tested controller corroborates a fresh, one-use URL/real-peer stream
observation with radio identity, source and PLAY_STATE; a stale ContentItem
location is not silently treated as a matching URL. This ownership contract
still requires hardware acceptance. Only its local
release marker enables live PCM; the receiver has no network release endpoint.
Input received while gated is discarded, not queued for later playback.
The owner then unmutes at the confirmed test level and continuously monitors
the radio. This is a required safety contract, not an optional UI convention.

The DHCP hook validates the expected server, subnet, interface, duration and
excluded addresses before accepting a lease. Duplicate-address detection probes
only the offered, already validated address; it does not scan an address range.
There is no guessed-static-address fallback. Lease loss, expiry, address change
or child-process exit stops the receiver. A mandatory bounded lifetime prevents
an unattended development process from becoming permanent infrastructure.

The provisioning marker is a local file inside the isolated namespace, not an
HTTP administration endpoint. The current private lab controller is deliberately
not an end-user installer. A production controller must derive exclusions from
the central protected-device policy and verify namespace cleanup, renewal,
restart, stable virtual identity and rollback. A manually supplied deny list is
not a substitute for that integration.

## What is retained

### Opt-in native group observer

`Dockerfile.observer` builds a separate receiver from an exact source-archive
checksum and patches only ownership/SETUP/teardown observation hooks. Enable it
explicitly with `native_group_observer: true`. The ordinary image and the
previous Apple-accepted pilot are unchanged. The private channel exports a
process nonce, PID, connection number, revision, monotonic event time, state and
SHA-256 of a validated group UUID; never raw UUIDs, sender identities or secrets.

The supervisor verifies the binary manifest, binds observations to its original
child and lifetime nonce, checks file permissions without following symlinks,
and rejects malformed/replayed state. Missing group UUID is `UNASSIGNED`, never
a Bonjour fallback. Old teardown cannot erase new ownership. Lost observation
files stop the pilot; final status clears membership. The channel reports a
parsed group label, not authenticated household membership or radio permission.
Backup, capability, ownership and actual readback remain controller obligations.
New native connections cannot automatically rearm a terminal one-session pilot.

Local build after preparing the baseline image (not a deployment command):

```sh
docker build -f modules/airplay-bridge/Dockerfile.observer \
  --build-arg PILOT_IMAGE=basswiesn-ap2:private-20261013 \
  -t basswiesn-ap2:private-native-group .
```

For ARM64, choose an ARM64 baseline and `--platform linux/arm64`. This is a
different build from the previously accepted `5.5.2-dirty` binary. A fresh
bounded two-receiver iPhone input trial passed on a physical Pi on 2026-10-13:
both native records reported the same group hash while decoding nonzero PCM
and text metadata. Removing one cleared its native group and metadata while
the other kept playing; returning to the iPhone cleared both. This does not
validate grouped radio output, independent senders or a production controller.
Source and binary hashes are verified, but Alpine build packages are not yet
a complete reproducible release lock.
The source pin is tag `5.5.2`, commit
`7bad231c18368dbd26f298577f6210e36e4b0797`. Its archive's `configure.ac` still
reports `5.5.1` without Git metadata, so the build manifest's commit and binary
hash—not the banner alone—identify this experiment.

The C harness in `tests/test_airplay_native_group_315.py` compiles the real header
and needs a C compiler and OpenSSL development headers. The following explicit
offline test exercises real patched SETUP/TEARDOWN dispatch with synthetic
loopback plists. It does not test Apple pairing, audio or radio hardware. Its
verbose synthetic failure capture must never be used in a real Apple session.

```sh
docker run --rm -i --network none --read-only --cap-drop ALL \
  --security-opt no-new-privileges:true --user avahi \
  --sysctl net.ipv4.ip_unprivileged_port_start=0 \
  --tmpfs /run:rw,nosuid,nodev,noexec,mode=1777,size=16m --shm-size 16m \
  --entrypoint python3 basswiesn-ap2:private-native-group - \
  < tools/test_airplay_observer_offline.py
```

## Private diagnostic surface

### Local application session service

`tools/run_airplay_service.py` runs the unprivileged application owner separately
from the web server and privileged network provisioner. It requires an existing
private mode-0700 state directory and explicitly supplied, validated private
profiles. It does not install receivers or accept configuration from HTTP.
For a reviewed laboratory deployment, its invocation shape is:

```sh
python -m tools.run_airplay_service \
  --state-dir /private/application-data/airplay-runtime \
  --profile single=/private/prepared/single.json
```

These are placeholder paths, not a complete installation command. Use the same
unprivileged UID as the application. The web control socket is fixed at
`<BASSWIESN data directory>/airplay-runtime/control.sock`; the directory must be
shared explicitly if the processes run in different containers. Do not mount a
Docker socket or run the application as root to make this work. Profiles retain
the existing full identity, protection, backup and readback checks.

The local protocol exposes only predefined profile names and sanitized state.
The journal and process lock prevent replay and conflicting sessions. SIGTERM
requests verified restoration and keeps the lock until cleanup finishes; give
the service sufficient shutdown time for the existing two-minute restoration
budget. A forced process kill leaves an unresolved reservation, never an
automatic restart. Preserve the journal and private runner receipts for
reconciliation. Never delete them simply to enable another start.

Profiles are one-use grants, including after successful restoration. A new
session needs newly provisioned receiver/relay authorization and fresh result
paths. Changing only a timeout or volume cannot reuse an old capability. This
service is an integration boundary for the bounded LAB runner; it is **not**
the permanent native receiver supervisor or end-user installer.

### Automated sender laboratory

The opt-in tools `ap2_buffered_test_sender.py`,
`ap2_sender_receiver_fixture.py` and `ap2_fixture_network.py` exercise real
native receivers without household devices. A private Docker owner creates an
**internal**, disposable `198.18.42.0/29` network with two exact receiver
addresses and one sender. The tools reject root, additional interfaces,
non-fixture addresses, IPv6 and external/default routes. There are no published
ports, radio controllers, discovery scans or account credentials. The pinned
external sender is obtained separately; see `THIRD_PARTY_NOTICES.md`.

The realtime/PTP case passed on ARM64 on 2026-10-15: two receiver processes,
two sessions each, actual decoded PCM, metadata, fresh volume callbacks at
0/50/80/100/50 percent, pause/resume and native teardown. The same run passed
decoded audio through BASSWIESN's real bounded FIFO reader, MP3 encoder and HTTP
relay into a local decoding probe. A -20 dBFS synthetic signal emerged at about
-20.2 dBFS initially. Pause requires a complete quiet window at both native
input and decoded relay output; resume requires new samples. A command ACK,
old peak measurement or HTTP 200 alone cannot pass the audio test.

The synthetic consumer keeps one local HTTP stream across native reconnects
to measure the signal path. That is **not** production radio-session teardown.
Physical volume forwarding, speaker audibility, Apple group selection and
restoration of radio state require their separate controller and hardware
acceptance. The fixture ignores native PCM volume attenuation, like the
explicit radio-volume-forwarding mode: the volume test verifies callbacks,
not an audible mute of the synthetic stream. The receivers are tested
sequentially, not as an invented Apple group.

`BASSWIESN_TEST_STREAM=realtime` selects this verified diagnostic case.
`buffered` remains a compatibility investigation: connection and metadata
succeeded but no decoded audio was observed with this sender/receiver pair.
Do not weaken that test, change receiver codecs speculatively, or infer that
an actual Apple buffered session has failed from this result. The sender's
clock-readiness label also remained `cold` during successful realtime audio;
that heuristic alone is not proof that NQPTP lacks a usable clock.

For an explicitly controlled output pilot, configure a fresh 32-lowercase-hex
`controller_nonce` together with the target-bound relay. The default remains
unchanged. In controlled mode, the owner must atomically publish a private
`/run/ap2/controller.json` record owned by the receiver UID (0700 directory,
0600 regular file, no symlinks): `version=1`, matching `nonce`, monotonically
increasing `sequence` and `observed_monotonic`, boolean `allow_audio`, and a
`hold_epoch` incremented whenever permission is withdrawn. These are local
one-session values, never a browser or sender API.

The worker closes on owner loss/invalid state, independently of the receiver
heartbeat. It ignores legacy `output-release` markers in this mode, discards
queued PCM on a new hold epoch, and exposes `controller_sequence`,
`controller_hold_epoch` and actual `audio_released` in its private status. Wait
for the hold acknowledgement before zone changes; release only after actual
radio readback. This cannot retract already transmitted audio and is not a
physical mute guarantee. `controller-ack.json` provides a private, nonce-bound
receipt for the exact command and hold epoch; a successful IPC send alone
does not prove that the worker closed the gate.

`receiver_ipc.py` exposes only bind/observe/control/ack operations on standard
input/output. The opt-in host `namespace_ipc.py` helper pins an immutable
container/image, checks receiver hardening and peer UID, and forwards only
that fixed command set through a private Unix socket. It does not expose a
Docker socket or an arbitrary shell command to BASSWIESN. Neither is launched
automatically. `group_controller.py` joins the private observations to the
existing source and zone transactions; the complete application lifecycle has
simulator coverage. The bounded application/host composition described below
has reached real Pi startup; grouped radio audibility remains unverified.

### Bounded paired application owner

`tools/run_airplay_pilot.py --config <private-config> --check-only` validates the
existing database schema, exact selected device fingerprints and shared lock
directory without contacting a radio. The explicit run, without `--check-only`,
uses the same unprivileged application UID and data/lock directory as BASSWIESN.
It does not run normal application startup, migrations or discovery. It exposes
only the session-bound provider routes, not another administration interface.

Both native receiver inputs must be selected before source preparation begins.
The selection wait defaults to two minutes and can explicitly allow up to five;
the whole pilot remains bounded to ten minutes. This wait does not relax the
independent five-second audio-owner lease. Under the existing per-device locks,
the application verifies fresh identity, zone and latency readback for both
participants, captures their standalone standby state, checks the explicitly
configured test volume (1–10; explicit operator choice, not an automatic increase),
and selects the owned relay. Only verified source and zone readback may release
audio. Missing cached capabilities are not fabricated or written into the
database: actual capability readback is required before the first write.

`pilot_namespace.py` is the separately invoked privileged host owner for one
isolated receiver. Its fixed DHCP hook needs a root-owned **executable** code
filesystem. `/run` may be mounted `noexec`: keep private runtime receipts there,
but stage code on a suitable executable filesystem; do not remount `/run` or
disable its protection. The helper verifies the pinned image and native binary,
uses router-issued addresses and creates the private IPC endpoint only after
the native ownership records are ready. It cannot issue radio control requests.

The explicit provisioner can supply a persistent, locally generated
`receiver_key`. `receiver_identity.py` stores these keys in a root-owned private
registry, verifies the saved result, and preserves entries for temporarily
absent devices. The resulting virtual MAC is independent of a run identifier,
IP address or friendly name; it never copies a physical radio's MAC. The owner
locks that virtual identity before creating resources. A previous labeled
container or an orphaned host interface blocks reuse and requires reconciliation;
neither is automatically deleted. Omitting the key retains the older disposable
pilot behavior. This is a restart prerequisite, not a permanent service or a
claim that Apple reconnection has been validated.

Before the first fresh, held controller command, the receiver drains and
discards input PCM without opening an HTTP audio output. This keeps the native
pipe player from blocking while the application prepares its source. The probe
then relinquishes the FIFO to the audio worker; there is never a competing pair
of readers. Source/readback verification must still explicitly release audio.
An output-worker startup failure ends the pilot instead of reopening an old
grant. These mechanics have isolated software tests; they do not establish
the cause of an earlier Apple connection failure.

The application records fixed startup phases and a final restore receipt without
logging relay capabilities or exception messages. Normal session completion
restores captured volume only after standby readback. Unexpected ownership or
state changes can require manual reconciliation; a stopped process is not proof
of restoration. Host cleanup separately proves exact owned-container removal and
unchanged host routes. No receiver, installer or radio test starts automatically.
This is private LAB infrastructure, not a production installation path.

The actual audio worker has also passed a native Pi ARM64 test using synthetic
PCM, real FFmpeg and loopback HTTP with networking disabled. The test checks
held/released PCM, exact acknowledgements and shutdown when owner input
vanishes despite continuing receiver supervision. It is not Apple/radio
acceptance. After explicitly building the observer image from the current
tree, run this isolated check from the repository root:

```sh
docker run --rm --network none --read-only --cap-drop ALL \
  --security-opt no-new-privileges:true --user avahi \
  --tmpfs /run:rw,noexec,nosuid,nodev,mode=1777,size=16m \
  --tmpfs /tmp:rw,noexec,nosuid,nodev,mode=1777,size=8m \
  --mount type=bind,src="$PWD/tests/fixtures/ap2-control",dst=/config,readonly \
  --mount type=bind,src="$PWD/tools/test_airplay_control_offline.py",dst=/test.py,readonly \
  --entrypoint python3 basswiesn-ap2:private-native-group /test.py
```

Only aggregate byte/event counts, process status and current allowlisted display
text enter `/run/ap2/status.json`. The FIFOs and status are private tmpfs files.
Raw RTSP output, headers, pairing data, client identities, artwork and audio are
not exported. Child-process standard output/error are suppressed. The metadata
decoder bounds XML input and decodes only approved display fields; it is not a
generic receiver-log redactor.

Lifecycle failures report fixed stages and exception classes, not arbitrary
exception strings. This intentionally limits diagnostic detail. Any additional
instrumentation must use structured allowlisted observations, never unrestricted
verbose receiver logging. Ephemeral receiver state must not be exported when a
test container is removed.

## Current verification

- Local isolated DHCP fixture: startup, expected listeners, protected-route
  rejection and shutdown after lease withdrawal verified.
- ARM64 container build: verified on a separate Pi lab image, without replacing
  the running BASSWIESN installation.
- Router-issued DHCP lease and receiver-process startup: observed in the Pi pilot.
- Actual iPhone selection, decoded nonzero PCM and text metadata: verified in
  one bounded input-only pilot. The user confirmed visibility and more than
  30 seconds of playback. About 49 seconds of increasing audio counters, stable
  receiver PID, valid title/artist/album/genre and clean session-end events were
  observed. Metadata cleared after output returned to the iPhone.
- Offline isolated FIFO-to-HTTP test: synthetic PCM became MP3, was fetched
  by an authorized fixture peer, and decoded into nonzero audio. End-of-input
  closed the response cleanly. Peer/path rejection and disconnect cleanup are
  also covered by loopback tests.
- Earlier bounded downstream attempts: no verified audible SoundTouch output. One
  attempt remained safely silent because the ContentItem echo did not match
  the selected stream. The revised ownership/volume controller was tested
  offline; the subsequent prepared hardware attempt was cancelled before audio.
- A later attempt through the verified application selection path established
  the owned source, safe volume and a flowing MP3 connection, but returned
  `PENDING_READBACK` while the radio was still `BUFFERING_STATE`. The controller
  stopped, restored stable settings and removed its temporary station. No audible
  output was confirmed. A bounded additional readback window is now tested
  offline; it never resends selection or treats buffering as success.
- Fixed-format PCM input now disables unnecessary FFmpeg probing. A local
  comparison measured first MP3 output at about 0.09 seconds rather than 4.3
  seconds. This is encoder startup only, not end-to-end or radio latency. Tests
  decode synthetic audio and distinguish post-release tones from discarded
  pre-release/paused input.
- On 2026-10-09, a subsequent genuine iPhone -> Pi -> SoundTouch 30 trial
  produced user-confirmed audible, stable music. The controller waited for actual
  PLAY_STATE, matched the fresh relay URL, verified the explicitly requested
  test volume 5 and then released live PCM. No relay failure was recorded.
  The initial Apple volume intent was capped to 5 and read back. Interactive
  slider/mute behavior was not part of this acceptance.
- After that Apple session ended, the radio returned to its captured standby,
  volume and mute state; presets, bass, zone and other captured stable settings
  matched. The temporary station and receiver were removed. This is bounded
  single-radio evidence, not continuous-use or complete volatile-state equality.
- Radio restoration was verified after the earlier attempts. This does not
  establish successful playback or a complete production session lifecycle.
- On 2026-10-13, the user selected both independently named receivers on a real
  iPhone. Both decoded nonzero PCM and text metadata, with the same live
  advertised group identifier. Deselecting one emitted its player-end event,
  cleared its text/volume snapshot and changed its advertised identifier; the
  other continued receiving audio, also confirmed by the user. This input-only
  trial blocked all radio addresses. It proves simultaneous selection and
  independent input teardown, not Bose-zone creation or audible synchronization.
  Full-range independent per-radio volume and grouped radio output remain
  unverified. Advertised group identity alone is not an authenticated membership
  contract and cannot authorize zone writes.
- A real pause trial found an adapter defect: activity timeout `aend` was
  incorrectly treated as `pend`, closing the relay and restoring standby.
  The user confirmed pause, not resume. Both state and end-marker paths now use
  the same stream-end predicate. Activity expiry preserves paused-session state;
  bounded silence, process and lease expiry remain enforced. Regression and
  native non-root offline checks pass; do not infer long-pause hardware support.
- With that correction, a later single-radio trial passed an approximately
  11-second pause and audible resume on the same relay, without reselection.
  Apple mute/unmute produced verified physical states 5/unmuted -> 0/muted ->
  5/unmuted; the user confirmed silence and restored music. High sender values
  stayed capped at 5. The owner then ended the trial and restored captured stable
  radio settings; receiver and temporary station were removed. Long pauses,
  uncapped volume and multi-receiver behavior are not covered by this acceptance.

For software-controlled radio relay output, the receiver uses a 30 dB flat
volume range instead of its pipe backend's default 96.3 dB range. The pinned
native implementation maps an Apple request of -24 dB to approximately
-54.9 dB with the default taper, before the radio's own volume control. The
relay-specific curve retains native mute and a maximum of unity gain; it does
not amplify above the source level or authorize radio volume writes. Explicit
radio-volume-forwarding mode and input-only receivers retain their separate
policies. Acoustic loudness, synchronization and user acceptance still require
real hardware tests; configuration tests alone do not establish them.

A bounded paired test on 2026-10-15 produced user-confirmed audible,
perceptually synchronized output on the selected SoundTouch 30 and Portable.
Both radio volume readbacks were 10, with the Apple slider at 100% (0 dB).
An eight-second sample of the MP3 actually sent to the coordinator measured
-16.7 dB mean / -5.5 dB peak. A separate normal-radio sample measured
-17.8 dB mean / -4.9 dB peak, and the user confirmed normal sound. These were
different program excerpts, not calibrated loudness or synchronization tests.
The earlier relay sample was severely attenuated (-74.1 dB mean). Practical
slider behavior at lower positions, independent member volume, long-running
stability and full session teardown acceptance remain incomplete. This is
bounded paired hardware evidence, not production approval for all AirPlay 2.

The updated receiver separates a bounded explicit pause (default 300 seconds)
from the 30-second unexpected input-gap limit. A stalled supervisor invalidates
the audio worker heartbeat within five seconds. Offline tests cover a two-minute
simulated pause and 31 seconds of actual clocked encoder/HTTP silence followed by
decoded audio on the same connection. Long-pause hardware validation remains
pending. Private session-bound text observations now have an offline-tested
consumer in the actual BMX response path; the production controller still needs
to connect that consumer after owned source/identity/volume readback.

This proves the Apple-to-Pi input path and one audible single-radio output trial,
not all AirPlay 2 features. No raw RTSP trace was captured, so the specific negotiated
transport/codec and Apple group behavior are not inferred from PCM alone.
The test container was removed afterward; production BASSWIESN and host network
configuration remained unchanged.

The upstream image reports Shairport Sync `5.5.2-dirty` with NQPTP `1.2.8`.
Its reduced FFmpeg libraries are needed by the upstream receiver, but cannot
serve as the library set for Alpine's full FFmpeg CLI. A CLI-only wrapper uses
the matching Alpine libraries; Shairport's loader path and upstream libraries
are unchanged. The image build tests encoder startup to catch loader conflicts.
The image digest is fixed in the Dockerfile, but the additional Alpine packages
are not yet locked to a reproducible package set. Dependency provenance,
redistribution notices, SBOM and production privilege/resource hardening remain
release gates. Do not describe the lab image as a fully reproducible release.

Offline unit tests (no receivers or radios contacted):

```sh
.venv/bin/python -m pytest -q tests/test_airplay_bridge_300.py \
  tests/test_airplay_receiver_contract_300.py tests/test_airplay_relay_runtime_300.py \
  tests/test_airplay_http_relay_300.py tests/test_airplay_volume_300.py \
  tests/test_airplay_lifecycle_315.py tests/test_airplay_startup_315.py \
  tests/test_airplay_pause_315.py tests/test_airplay_display_session_315.py \
  tests/test_airplay_native_group_315.py
```

These are protocol/state simulations and actual local encoder tests, not an
emulated Bose board. They do not reproduce radio DSP, Wi-Fi firmware, physical
keys, authentication hardware or audible output.

See [the bridge development contract](../../docs/airplay-bridge.md) for the
separate Apple and Bose synchronization boundaries and acceptance criteria.
