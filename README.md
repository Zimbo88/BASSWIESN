# BASSWIESN

Local server and cloud replacement for Bose SoundTouch systems.

BASSWIESN is an independent, unofficial project that keeps supported
SoundTouch radios useful on a trusted home network. It combines a local
SoundTouch compatibility service, a browser-based setup assistant, radio and
preset management, Multiroom controls, metadata, health models and diagnostics.

BASSWIESN is not affiliated with, endorsed by or supported by Bose.

## Why it exists

SoundTouch radios depend on service contracts that extend beyond a stream URL.
Provider sessions, reporting, restrictions, metadata, source state and radio
read-back all affect reliable playback. BASSWIESN reconstructs the confirmed
parts of those contracts locally and reports unsupported behavior honestly.

The core rule is simple: a sent command is not success. Critical workflows use
identity checks, backups and read-back before reporting a successful result.

## Release status

BASSWIESN `2.0.0` is the current release. Its release gate covers software,
real Chromium workflows, clean installation and supported real-hardware
read-back. Experimental functions are kept out of the normal user path or
marked LAB. The project does not claim a 7-hour or 24-hour stability result;
those duration tests were not performed for this release.

## Supported device profiles

Read-only discovery and diagnostics can describe additional SoundTouch models.
Critical setup writes fail closed and are currently profile-bound to the exact
researched firmware build `27.0.6.46330.5043500` for:

- SoundTouch 20 SCM and SM2/Series III variants
- SoundTouch 30 SCM and SM2/Series III variants
- SoundTouch Portable SCM

Variant, platform/module type and complete firmware build must all match. A
radio-reported Product ID must also match; when firmware does not expose it,
the UI labels the Product ID as derived from the one uniquely matched approved
profile. Unknown combinations remain read-only.

## Main features

- local SoundTouch compatibility endpoints for confirmed Marge, BMX, Orion,
  station, source, account and reporting contracts;
- explicit SSDP discovery with stable device identity and protected-device
  filtering before unicast follow-up;
- persistent single- and multi-device setup jobs with per-radio progress,
  backup, preview, reconnect and read-back;
- atomic preset write/delete/clone flows with revision, expected previous
  state, backup, radio read-back, divergence and reconciliation;
- playback controls backed by authoritative radio state rather than stream
  reachability alone;
- separate ProviderHealth, PlaybackHealth, MetadataHealth, ReportingHealth,
  SessionHealth and StreamHealth;
- provider restrictions including optional `inactivityTimeout` as an unsigned
  64-bit value in seconds, where missing or zero means disabled;
- a persisted reporting scheduler with dynamic `nextReportIn`, bounded queue
  and retry behavior;
- live station, track, artist, album and `imageUrl` metadata without source
  reselection or stream restart;
- Web UI artwork caching with provider image, station logo, source icon and
  fallback handling;
- Multiroom topology, source, clock, output latency and volume observation,
  including an option that sends no BASSWIESN volume alignment;
- AirPlayReadiness diagnostics for product, authentication hardware, STS,
  source, mDNS, pairing, PTP and audio evidence;
- a redacted support bundle, append-only write ledger and per-radio diagnostic
  timeline;
- responsive desktop and mobile Web UI.

## Installation

Requirements:

- Linux on x86-64 or ARM64
- Docker Engine
- Docker Compose v2
- a trusted private LAN shared with the radios

From an unpacked release:

```bash
./install.sh
```

Then open:

```text
http://<BASSWIESN-host>:1328
```

Other local ports are `1516` for the SoundTouch compatibility service and
`1860` for diagnostics. Existing `.env` files and `data/` are preserved.
Never use `docker compose down -v` as an upgrade step.

See [SETUP_READ_HERE.md](SETUP_READ_HERE.md) for the complete workflow.

## Factory-fresh onboarding

Wi-Fi provisioning is intentionally outside BASSWIESN.

1. The user connects every reset radio to the home Wi-Fi or wired LAN using the
   normal device procedure.
2. The user opens **Setup** and presses the visible discovery button.
3. BASSWIESN verifies only the unprotected radios found in that explicit SSDP
   invocation through `/info`.
4. The user selects one or more radios, reviews their exact profiles and a
   common preview, then starts one persistent job.
5. Progress, failures and final read-back remain separate per radio.

BASSWIESN never changes the host computer's Wi-Fi, joins a radio setup access
point, stores SSIDs/passwords or performs an automatic discovery scan on page
load.

## Presets

Preset mutations follow:

```text
PREPARED -> RADIO_WRITE -> RADIO_READBACK -> VERIFIED -> LOCAL_COMMIT
```

Failures become reconciliation or rollback work; they do not create a false
local success. Local radio presets and BASSWIESN Multiroom presets are modeled
separately. A physical preset-button check remains a manual validation step
when automation cannot press the hardware button.

## Playback and recovery

Radio `/now_playing`, source and play state are authoritative. Provider,
stream, reporting and metadata evidence are secondary and remain separate.

Automatic recovery is limited to safe stages:

1. radio read-back;
2. metadata refresh;
3. provider refresh;
4. stream URL re-resolution.

Source reselection, stop/play, local service restart and radio reboot require
explicit controlled action. Factory reset is never an automatic recovery
stage.

## Multiroom

BASSWIESN models master, members, source, clock, output latency and volume as
separate contracts. With **Preserve existing volumes**, BASSWIESN reads volumes
before and after zone creation but sends no artificial `SetVolume`. Any change
made by radio firmware is reported rather than silently corrected.

## Metadata and artwork

Track, artist, album and image URL can update during playback without
reselecting the source, calling `SetURL` or forcing a rebuffer. Artwork shown in
the browser is separate from model-dependent radio display capabilities.

Clock-as-metadata is a LAB option, disabled by default and limited to a
60-second update interval. It is not a display firmware patch.

## AirPlayReadiness

AirPlay support is diagnostic only. BASSWIESN records time-bounded evidence for
product, auth hardware, STS, source visibility, mDNS, pairing, PTP and audio.
Unknown evidence remains `UNKNOWN`; source visibility or mDNS alone is not
reported as fully ready.

BASSWIESN includes no MFi bypass, authentication workaround or firmware patch.

## Diagnostics

The Web UI exposes health states, reporting, restrictions, metadata freshness,
recovery actions and a correlated timeline. Support bundles are redacted and
contain a manifest plus SHA-256 checksums. The write ledger records the action,
device, requested state, backup reference, result, read-back and origin without
storing secrets.

## Safety

- Configure completely protected radios by both IP and stable device ID.
- Protection is evaluated before network transport, including DNS-resolved and
  redirect targets.
- Audio validation requires identity, a volume write to `1` and read-back of
  `1` before playback.
- Critical write profiles are exact-build profiles; `27.*` wildcards are not
  accepted.
- Unknown provider writes fail closed instead of returning fake success.
- The supported deployment is a trusted LAN. Do not expose BASSWIESN ports
  directly to the public Internet.

## Known limitations

- Not every SoundTouch model or firmware build is write-enabled.
- Full operation without Internet, Bose services and BASSWIESN is
  feature-dependent; do not use “fully offline” as a blanket claim.
- Spotify, Deezer, Pandora and similar providers are not complete replacement
  adapters unless explicitly marked supported.
- Radio firmware may ignore Pause or Stop for some sources; the UI reports the
  failed read-back instead of faking success.
- Arbitrary OLED bitmap artwork is not claimed.
- Physical preset-button validation may require a person.
- DLNA, local media, announcements/TTS, battery patching, Telnet reboot and
  standby-clock recovery remain experimental or LAB features.
- Factory reset is documented but is not a normal BASSWIESN product function.

## Development and tests

The test suite is split by feedback speed:

```bash
make test-fast
make test-integration
make test-ui
make test-hardware   # explicitly gated; real devices
make test-release    # complete software suite
```

Visible user workflows are exercised with Chromium/Playwright. Hardware tests
remain separate and require explicit target authorization.

## Contributing

Bug reports should include the BASSWIESN version, exact radio model, full
firmware build, variant/platform when available, reproduction steps and a
redacted support bundle. Never attach credentials, private keys, tokens,
runtime databases or unredacted household network details.

Contributions must preserve fail-closed device protection, read-back before
success, additive database migration and the separation between normal and LAB
features. Do not copy code from reference projects; use independently
implemented concepts and documented protocol evidence.

## License and trademarks

BASSWIESN is released under the MIT License. See [LICENSE](LICENSE).

Bose and SoundTouch are trademarks of their respective owners. Their use here
is solely to identify compatible products. This project is not affiliated with
or endorsed by Bose Corporation.
