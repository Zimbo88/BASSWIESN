# Changelog

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
