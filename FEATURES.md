# BASSWIESN 2.0 Feature Status

This document separates production features from limited, experimental and
unsupported behavior. A feature is not called complete solely because a unit
test exists.

## Production path

### Web interface

- responsive dashboard and device views
- guided Setup 2.0 with visible preview and job progress
- presets, stations, playback remote and Multiroom controls
- provider/playback/metadata/reporting health and timeline
- settings, diagnostics and clearly separated LAB navigation
- Chromium coverage for desktop and 390–430 px mobile viewports

### Discovery and identity

- no automatic discovery on normal page load
- user-triggered, bounded SSDP discovery
- protected IP and advertised device-ID filtering before unicast follow-up
- `/info` identity read-back for devices found by the current action only
- stable device ID with safe DHCP-address rebinding
- exact model, firmware, product, variant and platform evidence
- failed current identity read-back makes setup ineligible even when historic
  data remains stored

### Setup

- one or more radios in a `MultiDeviceSetupJob`
- independent per-radio phases and partial-failure reporting
- identity and exact write-profile preflight
- backup and SHA-256 evidence before critical writes
- explicit server target and common preview
- profile-bound HTTP/CLI-17000 operations
- reconnect and final read-back
- persistent progress across navigation/reload
- accurate routing-rollback scope
- optional volume-1 playback verification
- no normal-path SSH credential requirement

The user connects factory-reset radios to the home network before setup.
BASSWIESN does not configure computer or radio Wi-Fi.

### Protected devices

- configurable protected IPs and stable device IDs
- centralized guard before HTTP, URL redirects, DNS-pinned targets, CLI, SSH,
  Telnet, Setup, Presets, Multiroom and background transports
- protected SSDP replies discarded before descriptor fetch
- public builds contain no private installation identity

### Cloud/provider contracts

- confirmed Marge/BMX/Orion/station/source/account compatibility paths
- unknown writes fail closed with diagnostics rather than fake success
- provider and playback state modeled separately
- ReportingScheduler with POST, dynamic URL, `nextReportIn`, persisted due
  time, queue limit 20 and bounded retries
- restrictions parser for optional unsigned 64-bit `inactivityTimeout`
- missing or zero inactivity timeout means disabled; no local six-hour default

### Playback and health

- Play, source and preset selection with radio read-back
- Pause/Stop report failure when firmware ignores the command
- PlaybackHealth: stopped, starting, buffering, playing, paused, stalled,
  recovering and failed
- ProviderHealth, MetadataHealth, ReportingHealth, SessionHealth and
  StreamHealth remain separate
- evidence-based `INVALID_SOURCE` classification with `UNKNOWN` fallback
- automatic recovery limited to read-back, metadata refresh, provider refresh
  and stream URL re-resolution
- no automatic reboot or factory-reset recovery

### Presets

- read, write, delete, clone, sync and compare
- revision and expected previous state
- backup and SHA-256 reference
- radio write followed by radio read-back before local commit
- divergence marker, reconciliation and rollback states
- station logo/source icon data where supported by the normal preset contract
- distinct BASSWIESN Multiroom presets

### Metadata and artwork

- station, track, artist, album and `imageUrl`
- provenance, confidence, update time and stale state
- runtime metadata changes without source reselect, `SetURL` or rebuffer
- scheduler floor/coalescing based on confirmed research behavior
- browser artwork cache with provider image, station logo, source icon and
  fallback
- radio display capability kept separate from browser artwork

### Multiroom

- create, join, leave and reconnect
- master/member topology and source observation
- clock, output latency and volume treated as separate contracts
- preserve-volume option sends no BASSWIESN `SetVolume`
- volumes recorded before and after so firmware changes remain visible
- BASSWIESN Multiroom preset reconstruction

### Diagnostics and persistence

- additive database migrations
- per-radio diagnostics timeline
- redacted support bundle with manifest and checksums
- append-only write ledger
- request and master logs with secret redaction
- firmware/capability profiles and AirPlayReadiness evidence
- retention and cleanup jobs

## Diagnostic only

### AirPlayReadiness

BASSWIESN evaluates time-bounded evidence for:

- product
- authentication hardware
- STS registration
- source visibility
- `_airplay._tcp` and `_raop._tcp` mDNS
- pairing
- PTP
- audio

The normal UI reports Ready, Partially ready, Not supported, Blocked or
Unknown. It does not implement MFi bypasses or firmware patches.

## Limited or model-dependent

- Write-enabled setup is restricted to exact researched firmware profiles.
- Pause and Stop behavior varies by source/firmware.
- Multiroom firmware may alter a member volume even when BASSWIESN sends no
  volume command; the change is reported, not silently reversed.
- Radio-button preset verification may require a manual physical press.
- Routing rollback does not imply a full restoration of every internal account
  or environment value.
- Offline capability is reported per dependency; “fully offline” is not a
  blanket product claim.
- Browser artwork does not prove arbitrary OLED bitmap support.

## LAB / experimental

- clock-as-metadata, default off, minimum 60-second interval
- Telnet reboot with explicit confirmation
- Standby Clock recovery
- BatteryMonitor patch and rollback for specifically validated binaries
- local media catalog and DLNA experiments
- announcements/TTS experiments
- advanced SSH/profile diagnostics
- manual recovery stages above stream re-resolution

LAB functions are never silently promoted into normal setup or automatic
recovery.

## Not implemented or deliberately excluded

- automatic Wi-Fi provisioning or setup-access-point joining
- factory reset as a normal product button
- firmware flashing/patching, NAND or bootloader modification
- AirPlay/MFi authentication bypass
- arbitrary SSH/Telnet shell console
- fake-success catch-all provider writes
- automatic radio reboot loops
- a guarantee for all SoundTouch models or firmware versions
- complete replacements for every discontinued third-party music provider

## Validation commands

```bash
make test-fast
make test-integration
make test-ui
make test-hardware
make test-release
```

Hardware results are reported separately by exact model and firmware. Long-run
stability is never inferred from short software tests or log duration alone.
