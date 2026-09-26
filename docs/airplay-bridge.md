# AirPlay bridge — disabled LAB preview in BASSWIESN 3.0.0

Status: **unfinished prototype, disabled in the release**. Production receiver
integration and Apple multi-device grouping are not ready. This document records
the intended contract, not working release functionality or an installation guide.

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

The existing member-removal limitations also apply: an Apple deselection does
not magically make SoundTouch member removal reliable. Master departure,
member departure, regrouping, and rollback require explicit acceptance tests.
No automatic zone writes are implemented in this prototype.

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
- a 30-second maximum period with no complete input frame;
- old-session rejection and buffer clearing on pause/end/replacement;
- no disk recording, network or device control.

`ClockedMP3Relay` adds an explicitly started asyncio scheduler and one encoder
process. Its offline tests verify decodable clocked silence, identical bounded
fanout, silence expiry, cancellation and rejection of a slow consumer. A missed
clock deadline or full buffer ends the session instead of accumulating drift.
The runtime itself has no HTTP listener or radio transport.
`PCMRelay.frame()` itself is not a wall-clock scheduler.

`PCMHTTPRelay` is an explicit, one-session HTTP adapter. A real socket peer
check and random session URL restrict access to one preflight-authorized
target. No forwarded-header trust, redirects or radio API requests are involved.
It accepts HEAD without starting an encoder and consumes the session on GET.
FIFO input received before the authorized GET is discarded, not replayed later.
End-of-input closes the encoded stream; disconnection and buffer/encoder
failure stop it. A slow consumer cannot accumulate unbounded historical audio.
Offline Docker tests cover FIFO input, guarded HTTP output and MP3 decoding.
Radio selection/readback/restore and real downstream behavior remain separate
hardware acceptance gates. This pilot does not implement pause/reconnect policy
or a production enable endpoint.

The HTTP stream starts with a closed **live-audio safety gate**: it supplies
clocked digital silence but discards incoming music until the trusted owner
explicitly releases it. SoundTouch source selection can restore a remembered
volume even after a successful low-volume preflight. Therefore the owner must
mute before selection, establish ownership of the selected stream, reapply and
verify the safe volume and mute after selection, and only then release PCM and
unmute. One hardware attempt returned a mismatching ContentItem location even
while the authorized radio was fetching the session's silent MP3 stream. The
gate correctly stayed closed; there was no audible-output PASS. The revised,
offline-tested controller requires a fresh, error-free, one-use URL/real-peer
transport observation together with identity, `LOCAL_INTERNET_RADIO`,
`PLAY_STATE`, volume and mute readback. It records a mismatching ContentItem
echo separately. Neither a generic PLAY_STATE nor an open TCP port establishes
session ownership. This revised contract still needs hardware acceptance.
The gate
cannot be released by an HTTP subscriber. Offline tests verify that pre-release
nonzero PCM never reaches the encoder and cannot be replayed after release.
Silence has the same bounded lifetime as any other missing-input interval.

Silence is not a permanent all-day stream that wakes every configured radio.
An idle advertised receiver should leave the radio's current playback alone.

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

Sources:
[metadata events](https://github.com/mikebrady/shairport-sync/blob/master/MQTT.md),
[pipe output](https://github.com/mikebrady/shairport-sync/blob/master/audio_pipe.c).

## State of the implementation

| Component | Current state |
| --- | --- |
| Device inventory with `AP2 ` names | Read-only preview |
| Protected-device exclusions | Central guard reused; no device transport |
| Receiver processes and DHCP | Isolated Linux/ARM64 pilot; router DHCP verified |
| Apple-to-Pi audio input | iPhone selection and ~49 seconds decoded PCM verified in one pilot |
| Input text metadata and session end | Title/artist/album/genre observed; end cleared metadata, PID stable |
| PCM silence/audio switching | Offline implementation |
| One MP3 encoder spanning silence/music/silence | Offline FFmpeg test |
| Clocked encoder, bounded fanout and shutdown | Offline runtime and one-session HTTP tests |
| Metadata normalization and display ordering | Offline implementation |
| Apple volume event parsing, cap and stale-session rejection | Offline implementation; no integrated radio controller |
| Artwork cache | Not implemented |
| Radio display update delivery | Not implemented |
| Apple group membership adapter | Not implemented / contract unverified |
| Target-bound HTTP relay and FIFO attachment | Implemented in isolated pilot; offline end-to-end fixture PASS |
| Bose zone controller | Not implemented |
| Per-radio Apple volume control | Isolated controller prototype; hardware acceptance not completed |
| Hardware backup/restore session ownership | Not integrated |
| UI and deployment installer | Not implemented |

`GET /api/airplay-bridge/status` reads known database devices only. It reports
`enabled=false`, `OFFLINE_PROTOTYPE`, unprovisioned receivers and blockers.
There is deliberately no enable endpoint, advertisement, discovery, timer,
background task, persistent setting or startup network operation.

The application status describes the **integrated feature**, not a replay of
lab results. The successful Apple input pilot does not make the normal
application an enabled receiver. Bounded radio-output attempts did not produce
a verified audible-output result; a later prepared attempt was cancelled before
audio started. Radio audio, grouping and synchronization remain unverified.
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

Source: [Shairport RTSP implementation at the inspected commit](https://github.com/mikebrady/shairport-sync/blob/08af668a5d17b4714da38981dea4c9039263a4cc/rtsp.c).

## Safety and session ownership

Every future hardware operation must reuse the central protected-device guard
and verify `/info` against the registered identity before accessing the target.
All affected devices require a state/preset/zone/volume backup before writes.
An active session owns a generation-tagged lease. A late stop from an old
sender cannot stop new playback or restore over a user's subsequent manual
change. Writes need explicit preconditions and post-write readback.

Test volume is capped at 1. Production volume must not jump to Apple's default
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
change twice. Parsing and safety-cap tests pass offline; actual iPhone-to-radio
volume behavior is still an acceptance gate, not a released feature.

Do not log received audio or pairing data. Diagnostic output should contain
only durations, error classes, buffer sizes, state changes, hashes and
anonymized identifiers. No authentication or manufacturer identity bypass is
part of this module.

## Gates before the bridge can leave disabled LAB preview

These are future bridge acceptance gates, not completed 3.0.0 features.
The stable 3.0.0 core is released with this module disabled.

1. Build/pin the receiver stack; prove one isolated Apple receiver and bounded
   PCM/metadata output with no radio attached.
2. Prove two separately named receivers are individually and simultaneously
   selectable on an actual Apple sender. Verify isolation and session release.
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

From the repository root, with the existing test environment:

```sh
.venv/bin/python -m pytest -q tests/test_airplay_bridge_300.py \
  tests/test_airplay_receiver_contract_300.py tests/test_airplay_relay_runtime_300.py \
  tests/test_airplay_http_relay_300.py tests/test_airplay_volume_300.py
```

The optional FFmpeg tests create and decode synthetic audio in memory. They
never use an audio output device, open a LAN socket, start AirPlay or modify a
radio. Software tests continue to enforce the project's no-LAN transport guard.
