# BASSWIESN 3.0.1 Feature Status

This document separates production features from limited, experimental and
unsupported behavior. A feature is not called complete solely because a unit
test exists.

## 3.0.1 — help, language coverage and LAB workbench

Question-mark help and ten illustrated three-step guides cover setup, remote,
presets, multiroom, display, workbench, AirPlay, DLNA, updater and recovery.
These guides and 87 new help/control terms have entries in all 28 UI locales.
Compact remotes and restart pages now retain the chosen locale too. Untranslated
specialist text uses English, not German. See [language scope](docs/languages-and-help.md).
The personal German About statement is unchanged.

The [listening workbench](docs/lab-workbench.md) adds cached playback diagnosis,
metadata provenance/freshness, recent-station replay with explicit volume-1
confirmation, local QR remote links, bounded snapshot comparison/export,
listening CSV and saved stream-format inventory. Every new API requires LAB;
opening the workbench does not contact radios. These changes are included in
the 3.0.1 source tree, not the earlier 3.0.0 archive. Radio acceptance
remains pending. AirPlay, DLNA and production updater completion are unchanged.

## 3.0.0 — stable core and explicit LAB boundary

The stable core is released with the following boundaries. Unfinished functions
are LAB-only or disabled. Software checks do not replace real-radio acceptance.

| Area | Implemented in this tree | Remaining boundary |
|---|---|---|
| Mobile navigation | Height-bounded More menu, touch/click tests in Chromium and WebKit; user-confirmed iPhone UI acceptance | UI acceptance is not an audio test |
| Appearance | Shared system/light/dark preference for the main UI and compact remote | Full visual and translation review continues |
| Languages | DE/EN interface checks; 28 selectable UI locales, including Korean, Thai and Traditional Chinese core navigation and safety prompts; aliases for all device-language catalogue entries | Other locales remain partial, with an explicit English fallback notice. This is not a claim of complete native translation or radio-language modification |
| Remote control | Main controls first, command followed by current-state readback, collapsed technical output; display and compact-remote shortcuts | Readback is not proof of audible output |
| Display metadata | Distinguish missing, stale, unavailable and artist/title-bearing station metadata in the display editor without starting a probe | Stored upstream evidence is not physical display verification; absent cache does not prove absent station metadata |
| Preset checker | Actual sourceItem parsing, separate configuration/provider/stream results | Physical buttons and audible playback remain separate checks |
| Listening statistics | Today (UTC), rolling 7/30 days and all-time summaries; by-radio and station totals; midnight clipping | Estimated intervals, not continuous sound measurement |
| Device readiness | Explicit identity-guarded read-only SSH/marker check, cached with expiry; concise request/retry summary and last response, technical details in LAB | Marker presence does not prove boot persistence or the redirect destination; allowed requests are not a current connectivity check |
| Updates | Stable official release check; experimental LAB installation with explicit enrollment, HTTPS administrator action, root-sealed Unix service, backup/cutover/rollback and durable reconnect status. See [setup and limits](docs/update-helper.md) | Production updater acceptance remains open. Rootful Linux Docker/systemd and administrator setup required. No unattended updates, arbitrary archives or host commands. Checksums are not publisher signatures |
| About | Unchanged German author statement, English translation, observed release date | Other languages explicitly use the English essay fallback |
| AirPlay bridge | Disabled LAB preview and offline safety checks | Production receiver/network/group integration is unfinished. No AP2 audio, volume, metadata or multi-device PASS is claimed |
| DLNA library | Explicit ContentDirectory server connection, folder browsing (including unknown totals), MP3/AAC import into Stations and bounded same-origin byte/range relay; Chromium/WebKit DE/EN and independent ReadyMedia synthetic-media Pi checks | No multicast discovery, NAS credentials, transcoding, playlists or generic AVTransport control. Real-radio audio and broader NAS-vendor acceptance are separate; see [library guide](docs/dlna-library.md) |

No feature in this section enables radio SSH, rewrites firmware or relaxes
protected-device checks. Deferred LAB work is not advertised as production-ready.

## Production path

### Web interface

- Easy Mode is the default for new installations and exposes only Setup,
  Radios, Remote Control, Presets, Multiroom, Alarm & Timer and Device Settings
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
- bounded rediscovery after transport failure, with verified IP migration and
  circuit-breaker recovery instead of extended stale-address backoff
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
- separate per-radio opt-in for one-shot recovery after a confirmed local
  live-radio FINISH: fresh identity, hashed backup, public-only stream probe,
  no standby wakeup, no zone changes, no volume/preset write and verified playback
- explicit commands invalidate queued reconnects; finite retry budget and no
  replay of pending work after process restart
- no automatic reboot or factory-reset recovery

### Presets

- read, write, delete, clone, sync and compare
- revision and expected previous state
- backup and SHA-256 reference
- radio write followed by radio read-back before local commit
- divergence marker, reconciliation and rollback states
- station logo/source icon data where supported by the normal preset contract
- explicit no-station-logo mode with artwork-only preview, backup and read-back
- distinct BASSWIESN Multiroom presets
- per-slot checker states `VALID`, `WARNING`, `BROKEN` and `UNKNOWN`, based on
  radio read-back, source/account, provider, stream and local mapping evidence

### Metadata and artwork

- station, track, artist, album and `imageUrl`
- provenance, confidence, update time and stale state
- runtime metadata changes without source reselect, `SetURL` or rebuffer
- scheduler floor/coalescing based on confirmed research behavior
- browser artwork cache with provider image, station logo, source icon and
  fallback
- radio display capability kept separate from browser artwork
- optional local time in the playback title, enabled for new preferences,
  preserving existing opt-outs and canonical song metadata; no preset rewrite
- track/artist availability depends on supplied metadata; no universal live
  song metadata is guaranteed
- opt-in, bounded ICY extraction for compatible public streams with guarded
  DNS/redirect handling, per-station caching and backoff
- any combination of station, artist, title, local time and other station
  information, ordered individually in the radio's title line
- preview/edit/save/read-back; missing information omitted and long fields
  shortened without removing a selected clock
- no separator dot before the clock; native header/font/scrolling remain
  firmware-controlled

### Explicit radio restarts

- one or multiple selected radios; never the protected devices
- current identity and exact model/firmware profile, full required readbacks,
  routing backup and verified SHA256 before any command in the batch
- fixed researched reboot command, maximum four concurrent dispatches
- per-radio return/read-back, explicit differences and no fake all-green state
- firmware may change boot volume or the source registry; before/after values
  are shown, not silently overwritten
- no playback resume, volume write or factory reset
- optional weekdays/time/timezone schedule, off by default, standby-only,
  no active group, no catch-up and no duplicate/replayed occurrence
- missing required backup or an unavailable device blocks the whole batch
- reboot backups remain private; a full 512 MiB budget blocks new jobs until
  the operator exports/reviews existing backups

### Multiroom

- create, join, leave and reconnect
- master/member topology and source observation
- clock, output latency and volume treated as separate contracts
- preserve-volume option sends no BASSWIESN `SetVolume`
- volumes recorded before and after so firmware changes remain visible
- optional per-radio start volumes, verified before zone creation
- BASSWIESN Multiroom preset reconstruction

### Diagnostics and persistence

- additive database migrations
- per-radio diagnostics timeline
- redacted support bundle with manifest and checksums
- append-only write ledger
- request and master logs with secret redaction
- firmware/capability profiles and AirPlayReadiness evidence
- retention and cleanup jobs
- parent-only schema initialization before child services start
- a failed required service terminates its siblings so Docker can restart the
  whole application; readiness checks WebGUI, cloud and diagnostics separately
- optional systemd host observer: minute samples, CPU/RAM/process/container/log
  evidence, fourteen-day rolling retention, 1 GiB budget and 512 MiB free reserve
- observer is host-only, does not query radios and never repairs/reboots services

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

- Telnet reboot with explicit confirmation
- Standby Clock recovery
- BatteryMonitor patch and rollback for specifically validated binaries
- local media catalog and DLNA experiments
- announcements/TTS experiments
- advanced SSH/profile diagnostics
- manual recovery stages above stream re-resolution
- firmware-dependent single-member Multiroom removal with bounded distributed
  read-back
- exact-profile, backup-first Factory Reset with explicit typed confirmation;
  manual Wi-Fi reconnection may be required afterward

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
