# Changelog

## Unreleased — LAB only

- Add a listening workbench that explains saved playback warnings without
  guessing a cause or automatically restarting radios.
- Show metadata origin/age and recently heard stations; replay uses today's
  station mapping, explicit confirmation and the existing volume-1 safety path.
- Generate private QR links to individual remotes locally.
- Save, compare and export bounded cached-state snapshots with integrity checks;
  these are diagnostic subsets, not hardware backups.
- Export estimated listening intervals as spreadsheet-safe CSV and inspect saved
  stream-format hints without probing streams.
- Isolate cached test settings between software tests so temporary environment
  overrides cannot contaminate later protection tests. Production protections
  are unchanged.

## 3.0.0 - 2026-09-26

### Everyday use

- Make the mobile More menu fit the screen and keep it out of confirmation dialogs.
- Add light, dark and system appearance, shared with the compact remote.
- Put volume, playback and presets first; read back the radio state after each command.
- Make display settings and the compact remote easier to find. Keep technical output collapsed and internal test tools in LAB.
- Explain whether cached station data contains artist/title, is stale or has not been observed. Opening this status does not contact the radio or the stream.
- Add Korean, Thai and Traditional Chinese core interface text and safety prompts. Recognize device-language aliases without changing the radio language; clearly identify partial translations and English fallback.
- Fix the preset checker's source parsing and separate configuration checks from real listening tests.
- Respect a saved audio-safety lock when starting a station. Explain how to run the safety check before retrying; previews remain available without contacting the radio.
- Let diagnostic timeline entries reveal stored, sanitized details without probing a radio.
- Group estimated listening time by period, radio and station; correctly split sessions at midnight.
- Add an explicit read-only device check. Expired observations become unknown; marker checks do not claim verified startup or routing.
- Replace the technical device-policy line with request/retry status and the last observed response. Keep protocol details collapsed in LAB; permitted requests do not mean confirmed connectivity.
- Check for an official GitHub update in Settings. Experimental installation is LAB-only, with a separate administrator code over HTTPS after explicit host-service enrollment.
- Back up application data before switching versions and restore the previous version with its matching data if startup fails. A browser disconnect reads the saved job status instead of starting another installation.
- Add installer permission checks and isolated tests for the update service, backups and rollback. Production updater acceptance remains open; ordinary installation does not enroll the privileged service.
- In LAB, browse a selected DLNA media server and add MP3/AAC tracks to Stations without changing presets. The bounded relay is tested with synthetic media on an isolated Pi container; radio-audio acceptance is still separate.
- Accept valid DLNA folders when a server does not report the total number of entries. Independent ReadyMedia browsing, track metadata and audio relay checks passed with synthetic test media.
- Include the author's unchanged German statement, an English translation and the real publication date when known.
- Keep the unfinished AirPlay 2 bridge disabled. There is no production AP2 receiver, automatic network-address allocation or claimed Apple multi-speaker support in this release.

### Release boundary

The stable core is released independently of the unfinished LAB work. LAB does
not bypass identity checks, protected targets, backup/readback requirements or
administrator authorization. DLNA audio and the production updater remain
experimental; AirPlay receiver integration remains disabled. No new firmware
patch or claimed fix for unexplained receiver freezes is included.

## 2.6.5 - 2026-09-19

### Display and station metadata

- Choose station, artist, song title, time and other station information in any
  combination and order. Preview before saving; verify the one per-radio
  preference by read-back. All fields share the supported title line.
- Collect ICY metadata only when selected fields require it: bounded public-only
  streams, DNS pinning, redirect validation, shared station cache and backoff.
  No network scan or audio playback from metadata collection.
- Remove the dot before playback time. Preserve canonical song data, skip missing
  station information and retain legacy display choices until explicitly saved.
- Avoid conflicting clock controls when a custom display layout owns the clock.

### Restart controls and diagnostics

- Add a normal radio-restart page with exact selection preview, confirmation,
  required identity/profile checks, complete SHA-verified backup of all targets,
  parallel command dispatch and separate return/read-back results.
- Add an explicitly enabled weekday/timezone schedule. Skip active/unavailable
  radios and active zones as a batch. No missed-time catch-up, command retry,
  replay after service restart or automatic playback resume.
- Show firmware-caused boot-volume/source changes as differences. Tests on two
  researched models observed return but also changed boot volume/source rows;
  unchanged settings are not assumed merely because a radio responds again.
- Add an optional systemd host recorder for memory, CPU, pressure, processes,
  container restarts/OOMs and bounded logs. Private, rotated storage; no radio
  contact, automatic repair, database dump or audio capture.

### Server lifecycle

- Initialize the database once before starting services, avoiding parallel
  migration/background-writer contention.
- Fail and restart the whole container if a required child service exits.
  Check cloud and diagnostics health as well as the WebGUI.
- Retain the Radio Browser IPv4/dual-stack fix, protected-device guards, preset
  contracts, remote quick-group controls and existing backup/restore behavior.

The earlier intermittent radio freeze is not claimed fixed: old receiver logs
did not establish its cause. The new recorder is intended to retain better
evidence for a recurrence. See the release notes for validation boundaries.

## 2.6.0 - 2026-09-13

### Live radio and diagnostics

- Added a per-radio opt-in reconnect after confirmed unexpected live-stream
  FINISH. A fresh session, advancing reports, INVALID_SOURCE read-back, device
  identity and policy, standalone topology, stream validation and a durable
  backup are required. No volume/preset writes or automatic standby wakeup.
- Bounded reconnect attempts, cancel pending work on explicit playback actions,
  and never retry an ambiguous POST or replay a stale job after restart.
- Log STOP reason, media position and device attribution together. Provider
  reports remain accepted when diagnostic database storage is temporarily busy.
- Split playback history on confirmed station changes within the same source,
  preserving uncertainty instead of falsely attributing long sessions.

### Remote, grouping and display

- Show parsed playback state and actual volume in the standalone remote; keep
  XML/JSON details collapsed by default. Added English/German browser coverage.
- Start a SoundTouch group directly from a radio's remote, using that radio as
  master, with identity/backup preflight and distributed read-back. No volume
  alignment is sent; firmware volume changes are disclosed.
- Offer normal-mode time in the playback title, default on for new preferences
  while retaining explicit opt-outs. Respect the application timezone and avoid
  echoing clock projections back into canonical metadata.
- Retain protected-device checks, safe discovery, the dual-stack IPv4 preference,
  preset validation and separate provider/playback/reporting health.

Software, browser and clean-install checks passed. The new audible reconnect,
physical title-clock display and remote quick-group hardware checks are deferred,
not passed. Firmware can change volume on selection/group creation; publication
was approved with this limitation. Reconnect remains off by default.

## 2.5.1 - 2026-08-29

### Easy Mode and browser fixes

- Unified Setup and Radios discovery on the same explicit, bounded scan path;
  a visible Easy Mode click now discovers, identity-verifies and renders the
  same radios as Standard and LAB mode.
- Replaced the clipped desktop radio table with responsive cards so **Remove
  from BASSWIESN** remains reachable at desktop and mobile widths.
- Added About BASSWIESN to Easy Mode and completed English/German runtime
  translation coverage across Easy, Standard and LAB workflows.
- Restored Radio Browser station logos with a clean missing/broken fallback.
  Remote catalogue artwork now passes through the guarded, DNS-pinned raster
  cache instead of being requested directly by the browser.

### Presets, display and Multiroom

- Added **No station logo** as a third radio-display mode. Artwork sync takes a
  live preset snapshot, previews affected slots, backs up the radio, changes
  only `containerArt`, verifies read-back and preserves selection identity.
- Canonicalized stale `sourceAccount` values for local Internet radio and made
  the checker distinguish a matching station on another BASSWIESN origin from
  a genuinely different preset.
- Moved firmware-dependent single-member removal out of Easy and Standard into
  LAB while retaining safe complete-zone start/stop controls.

### LAB and release safety

- Added an exact-profile, backup-first LAB Factory Reset workflow using the
  confirmed CLI 17000 `sys factorydefault` command. It requires checkbox, typed
  confirmation and final browser confirmation, excludes protected devices and
  starts no automatic follow-up probe after the radio leaves the network.
- Made the package source version authoritative so an older preserved `.env`
  cannot make an upgraded server advertise an obsolete release number.
- Retained the validated Radio Browser dual-stack IPv4 preference and all
  protected-device, preset read-back and circuit-breaker recovery behavior.

## 2.5.0 - 2026-08-26

### Stability and recovery

- Added bounded SSDP rediscovery after connectivity failures. A stored IP is
  changed only after device-ID read-back confirms the new endpoint; a verified
  migration resets stale circuit-breaker state.
- Kept the validated dual-stack Radio Browser fix: a reachable IPv4 address is
  preferred when dual-stack DNS exposes an unusable IPv6 route, without
  weakening private/protected-target checks.
- Replaced the unbounded repeated BMX audio-stream response with one confirmed
  stream candidate and fail-closed malformed Orion descriptor handling.

### Presets and playback

- Preset Checker now classifies each slot as `VALID`, `WARNING`, `BROKEN` or
  `UNKNOWN` using radio, account, provider, stream and local-mapping evidence.
- A verified preset write clears stale `sourceAccount` data and leaves a
  persistent human-readable result in Easy Mode.
- Hardware evidence distinguishes unavailable streams from radio/provider
  state that can be recovered by a controlled reboot; no fake success is
  reported.

### Easy Mode and Multiroom

- Easy Mode is the default for new installations and exposes only Setup,
  Radios, Remote Control, Presets, Multiroom, Alarm & Timer and Device Settings.
- Safe start volume is optional; when disabled, playback sends no preliminary
  volume command.
- Added optional per-radio Multiroom start volumes, verified before zone
  creation. Firmware normalization after `/setZone` is reported without a
  hidden correction.
- Single-member removal now polls both master and member topology with a bounded
  deadline and fails closed if distributed read-back does not confirm removal.
- Protected devices are excluded from all network-active UI selectors.

### Packaging and documentation

- Added copy-paste release installation, Docker, update, backup and
  troubleshooting instructions.
- Public `SHA256SUMS` verifies exactly the downloadable versioned archive.

## 2.0.0 - 2026-08-23

### Safety and honest contracts

- Device protection is evaluated centrally before HTTP, discovery follow-up,
  CLI, SSH, Telnet, Setup, Presets, Multiroom and background transports.
  Public builds contain no installation-specific protected identity.
- A passive Web UI load performs no setup port probe or automatic discovery.
  SSDP starts only after a visible user action and rejects identity-free or
  protected replies before unicast follow-up.
- Duplicate method/path contracts are tested; Telnet reboot has one canonical
  handler. Unknown cloud writes return a diagnosable unsupported response
  instead of fake success.
- Radio writes are recorded in an append-only ledger. Preset transactions
  commit locally only after radio read-back; divergence remains visible for
  reconciliation.

### Setup

- Factory-reset radios are connected to the home network by the user;
  BASSWIESN never changes host or radio Wi-Fi settings.
- One visible multi-device job coordinates identity, exact profile, backup,
  preview, routing/account work, reconnect and read-back independently per
  radio. A single failure cannot produce an all-success result.
- Discovery accepts a legitimate DHCP address change only after multicast
  identity and guarded `/info` read-back agree.
- A failed current identity read-back leaves setup fail-closed even when old
  profile data remains stored.
- The normal path uses HTTP plus profile-bound CLI 17000 without hidden SSH
  credentials. Unknown firmware/product/variant/platform combinations remain
  read-only.
- Rollback reports its proven scope and does not call a routing-only restore a
  full account/environment rollback.

### Playback, providers and recovery

- Reporting, restrictions, provider, playback, metadata, session and stream
  health are separate persisted contracts.
- `BMX.Restrictions.inactivityTimeout` is an optional unsigned 64-bit value in
  seconds; missing or zero disables the timer and no six-hour default is
  invented.
- Reporting uses POST, the dynamic reporting link, `nextReportIn`, persisted
  due time, queue limit 20 and bounded retries. Reporting failure does not stop
  playback or reboot a radio.
- Automatic recovery is limited to read-back, metadata refresh, provider
  refresh and stream URL re-resolution. Higher stages require explicit action.
- `INVALID_SOURCE` and `STALLED` are evidence-based and remain `UNKNOWN` when
  evidence is insufficient.

### Presets, metadata and Multiroom

- Preset write/delete/clone operations use revision, backup, radio read-back,
  verification, local commit, divergence and reconciliation states.
- Track, artist, album and image URL can update during playback without source
  reselection, `SetURL` or forced rebuffer.
- Browser artwork uses provider image, station logo, source icon and fallback
  independently from radio-display capabilities.
- Multiroom topology, source, clock, output latency and volume are modeled
  separately. Preserve-volume sends no BASSWIESN `SetVolume` and reports
  firmware-induced changes.
- Clock-as-metadata remains LAB, default off and limited to 60-second updates.

### Diagnostics and user interface

- AirPlayReadiness records time-bounded evidence for product, authentication,
  STS, source, mDNS, pairing, PTP and audio without an MFi bypass or firmware
  patch.
- Redacted support bundles include a manifest and SHA-256 checksums.
- Per-radio timelines correlate setup, provider, reporting, metadata, playback,
  preset, Multiroom and recovery events.
- Desktop and mobile Web UI flows are covered by real Chromium automation.

### Validation boundaries

- Home-LAN validation covered visible four-device factory-fresh onboarding,
  supported setup/routing, preset write/read-back/reboot, three-radio
  Multiroom, live metadata, LAB clock metadata, browser artwork and read-only
  AirPlay diagnostics.
- A physical preset-button press and complete Internet/Bose-service outage
  scenarios may remain documented manual/open tests.
- Log duration is not reported as a 7-hour or 24-hour stability pass.
