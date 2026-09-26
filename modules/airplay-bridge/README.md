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
before provisioning. Before bringing the interface UP, the receiver installs
both firewall drops and prohibit routes for every configured excluded address.
No default route or DNS resolver from DHCP is installed.

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
- Bounded downstream attempts: no verified audible SoundTouch output. One
  attempt remained safely silent because the ContentItem echo did not match
  the selected stream. The revised ownership/volume controller was tested
  offline; the subsequent prepared hardware attempt was cancelled before audio.
- Radio restoration was verified after the earlier attempts. This does not
  establish successful playback or a complete production session lifecycle.
- Multiple Apple selections, synchronization, join/leave and end-to-end
  per-radio volume: not yet verified.

This proves the Apple-to-Pi input path, not audible SoundTouch output or all
AirPlay 2 features. No raw RTSP trace was captured, so the specific negotiated
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
  tests/test_airplay_http_relay_300.py tests/test_airplay_volume_300.py
```

See [the bridge development contract](../../docs/airplay-bridge.md) for the
separate Apple and Bose synchronization boundaries and acceptance criteria.
