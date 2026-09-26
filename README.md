# BASSWIESN

Keep your Bose SoundTouch radios useful with a local server and a web browser.

BASSWIESN runs on a Linux computer or Raspberry Pi in your home network.
Use your phone or computer to choose stations, manage the six radio presets,
control playback and create SoundTouch multiroom groups. The server must remain
on while your radios use it; you do not need to keep the browser open.

**New here?** Start with [Quick Install](#quick-install), then follow the
[setup guide](SETUP_READ_HERE.md). [What changed in 3.0.2?](docs/releases/3.0.2/RELEASE_NOTES_3.0.2.md)

**Deutsch:** BASSWIESN steuert deine SoundTouch-Radios lokal im Heimnetz.
Sender, Presets, Fernbedienung und Multiroom erreichst du im Browser – auch
auf dem Handy. Die Oberfläche bietet Deutsch und 27 weitere Sprachen.
Die [Installation unten](#quick-install) ist zum Kopieren; die
[Änderungen für 3.0.2](docs/releases/3.0.2/RELEASE_NOTES_3.0.2.md) sind auch auf Deutsch erklärt.

BASSWIESN is not affiliated with, endorsed by or supported by Bose.

## Why it exists

SoundTouch radios depend on service contracts that extend beyond a stream URL.
Provider sessions, reporting, restrictions, metadata, source state and radio
read-back all affect reliable playback. BASSWIESN reconstructs the confirmed
parts of those contracts locally and reports unsupported behavior honestly.

The core rule is simple: a sent command is not success. Critical workflows use
identity checks, backups and read-back before reporting a successful result.

## Release status

Version **3.0.2** adds clearer diagnosis, local-day listening statistics,
browser-local text size and high contrast, and offline practice tools in LAB.
Named display profiles let you prepare and reuse a layout without immediately
changing a radio. It also includes the help and workbench work prepared as
3.0.1; there was no separate public 3.0.1 release.

Question-mark help and ten illustrated tutorials cover all 28 UI languages.
New safety explanations and confirmations are translated too. Some specialist
text still uses English; [the language guide](docs/languages-and-help.md)
explains the scope without claiming complete native-speaker review.

BASSWIESN `3.0.0` improves mobile navigation, light/dark appearance, remote
controls, preset checks, listening statistics and readable diagnostics. The
display layouts, guarded restarts and host diagnostics from 2.6.5 are retained.
Release validation separates software,
Chromium workflows, clean installation and real-hardware read-back. Easy Mode
is the default UI, while experimental functions are kept
out of the normal user path or marked LAB. Long-playback evidence and
continuous HTTP reachability are reported separately rather than turned into
an unsupported blanket uptime claim.

Station/song/time display was confirmed on a SoundTouch 30. Restart dispatch
and return were tested on SoundTouch 30 SCM and SoundTouch 20 Series III SM2;
firmware volume/source changes are reported explicitly. Reconnect is off by default.
Firmware may change volume when selecting a source or creating a group; there
is no guaranteed maximum-volume lock. See the [3.0.0 release notes](docs/releases/3.0.0/RELEASE_NOTES_3.0.0.md#hardware-boundary).

## Supported hardware

Read-only discovery and diagnostics can describe additional SoundTouch models.
Critical setup writes fail closed and are currently profile-bound to the exact
researched firmware build for:

- SoundTouch 20 SCM and SM2/Series III variants
- SoundTouch 30 SCM and SM2/Series III variants
- SoundTouch Portable SCM

## Supported firmware

Critical writes are validated for exact build `27.0.6.46330.5043500` on the
profiles above.

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
- selectable station name, artist, song title, time and other station information,
  with configurable order, an example preview and explicit save/read-back;
- bounded, opt-in ICY song-information collection for compatible public streams;
- restart one or selected radios with identity, complete backup and read-back;
  optional weekday/timezone schedule, off by default and standby-only;
- optional reboot-persistent, storage-bounded Linux host flight recorder for
  memory, processes, container state, application logs and kernel warnings;
- Web UI artwork caching with provider image, station logo, source icon and
  fallback handling;
- consistent English and German end-user text in Easy, Standard and LAB mode;
- Multiroom topology, source, clock, output latency and volume observation,
  including an option that sends no BASSWIESN volume alignment;
- AirPlayReadiness diagnostics for product, authentication hardware, STS,
  source, mDNS, pairing, PTP and audio evidence;
- a redacted support bundle, append-only write ledger and per-radio diagnostic
  timeline;
- responsive desktop and mobile Web UI.

For everyday use: start in **Easy**, choose **Standard** for more controls, and
enter **LAB** only when you want experimental or diagnostic tools.
See [Feature status](FEATURES.md) for supported paths and honest limitations,
and [the changelog](CHANGELOG.md) for a version-by-version summary.

### Your radio display

Open a radio's remote and choose **Radio display**. Select any combination of
station, artist, title, time and other information; use the arrows to arrange
their order. The preview is an example, not a claim about current station data.
Save applies a single device preference followed by read-back. No preset or
source rewrite is required, and the separator dot before the time is removed.

Selected fields share the title line. The firmware still controls its native
station header, fonts and scrolling. Missing stream metadata is omitted, never
invented. Long fields are shortened; album/cover/lyrics are not guaranteed by ICY.

### Radio restarts

Open **Radio restarts** from Radios or an individual remote. Review the selected
devices, acknowledge the interruption and confirm. Every radio must pass identity,
exact-profile and SHA-verified backup checks before any command is sent. Commands
are dispatched together (maximum four); results are verified separately. An
unresponsive radio or an active SoundTouch group blocks the batch.

The optional schedule uses selected weekdays, wall-clock time and an IANA
timezone. It is disabled by default, never interrupts active playback and does
not catch up missed times. Ambiguous commands and interrupted jobs are never
replayed. No volume, source resume or factory-reset command is sent by this
feature. Firmware can restore a different boot volume; the result shows the
actual before/after values and any source-list difference.

For persistent Pi/host diagnostics, see [Host flight recorder](docs/PI_OBSERVER.md).

## Easy Mode

New installations open in Easy Mode. It presents seven clear areas:

1. Setup
2. Radios
3. Remote Control
4. Presets
5. Multiroom
6. Alarm & Timer
7. Device Settings

Advanced diagnostics and LAB functions remain available through an explicit
mode switch. New DLNA enrollment/browsing and update installation require LAB;
changing modes does not cancel an already-running update or media relay.

## Advanced Mode and LAB Mode

Standard Mode exposes stable diagnostics and administrative controls. LAB Mode
adds clearly marked experimental and manual recovery tools. LAB is not enabled
by default and never bypasses protected-device or write-profile gates.

### Interface and release information

The interface improves the mobile More menu, adds a shared
light/dark/system appearance and puts the main remote controls before display
options. Commands are followed by a radio-state readback; a successful command
alone is not presented as a verified new volume or audible playback.

Listening statistics offer today in the configured timezone, rolling 7/30 days
and all-time views, grouped by radio and station. Daylight-saving changes are
handled as 23/25-hour days. Recorded session endings and open intervals are
shown separately: they do not prove an audible dropout or its cause.
These are estimates from recorded radio states, not measurements of uninterrupted sound.
Advanced device checks are explicit,
read-only and identity-guarded; they do not enable SSH or change redirects.

Settings can check the official stable GitHub release without configuring a
manifest URL. **Checking does not install an update.** The experimental LAB updater
has explicit installer onboarding, an HTTPS administrator action, a restricted
host service, private backups and version/image/data rollback. Production
upgrade acceptance remains open; see [update setup and limits](docs/update-helper.md).
The unfinished AirPlay bridge remains disabled. The experimental LAB DLNA library
can browse an explicitly selected server and relay MP3/AAC items; radio-audio
and broader server-compatibility acceptance remain separate. These are
deferred LAB features, not completed production functions. See [feature status](FEATURES.md).

About remains available in all three modes. The author's German statement is
preserved verbatim, with a separate English translation and a separate technical
status note. Other interface languages explicitly identify the English fallback
for this essay; a translated navigation label does not mean the essay is translated.

The interface offers 28 language choices, including Korean, Thai
and Traditional Chinese. All codes in the radio-language catalogue have an
equivalent UI choice, but the two settings remain independent. English and
German are the primary interface languages. Other translations are partial;
Settings states explicitly that untranslated controls and explanations use
English. A populated translation catalogue is not proof of full translation.

Version information shows the installed version's public GitHub release date,
not its build date or the computer's current date. Normal page loads use local
information only. **Check release date on GitHub** explicitly reads the official
release metadata; it does not install an update. If no publication date is known,
the interface says so. A failed check preserves an already verified date.

## Screenshots

Release screenshots show the real Chromium-tested desktop and mobile Easy Mode
flows. They are published without household device identities, private
addresses or hardware backups.

## Quick Install

For a **new installation** on Linux with Docker Engine and Docker Compose v2
already installed. Run this as your normal Docker-enabled user. If Docker
reports a permission error, stop and ask the host administrator to configure
access; do not make the Docker socket world-writable.

Copy the complete block into a Bash terminal. It stops on a failed download or
checksum and refuses to reuse an existing installation folder:

```bash
(
set -eu
mkdir "$HOME/basswiesn-3.0.2"
cd "$HOME/basswiesn-3.0.2"
curl --fail --location --retry 3 --output basswiesn-docker-release-3.0.2.tar.gz https://github.com/Zimbo88/BASSWIESN/releases/download/v3.0.2/basswiesn-docker-release-3.0.2.tar.gz
curl --fail --location --retry 3 --output SHA256SUMS https://github.com/Zimbo88/BASSWIESN/releases/download/v3.0.2/SHA256SUMS
sha256sum -c SHA256SUMS
tar -xzf basswiesn-docker-release-3.0.2.tar.gz
cd basswiesn-release
./install.sh
)
```

When the checksum prints **OK**, the download matches the release checksum.
This is an integrity check, not a cryptographic publisher signature.
The installer then builds and starts BASSWIESN. Open `http://<server-address>:1328`
in your browser, select your language, and follow **Setup**. No radio discovery
runs until you request it. The installer never changes host Wi-Fi.

**Deutsch:** Der Block ist für eine Neuinstallation, nicht zum Überschreiben
deiner bisherigen Installation. Bei `OK` stimmt die Prüfsumme. Danach im Browser
die Adresse deines Servers mit `:1328` öffnen und **Setup** wählen.

**Already using BASSWIESN?** Do not start a second instance beside it or delete
its data. Back up the existing `.env` and `data/`, and read the
[upgrade instructions](SETUP_READ_HERE.md#updating-an-existing-installation).
The in-app installer remains an explicitly enrolled, experimental LAB path.

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

## Trying the improvements without radios

In LAB, open **Practice and display profiles**. Seven fictional failures explain
how to distinguish connection, provider, reporting and group problems. The
simulator does not contact equipment or change playback. Display previews use
fictional songs. A saved profile only becomes a radio preference when you copy
it into that radio's remote editor and explicitly save.

At the bottom of each page, **Reading and keyboard** offers larger text and
high contrast. These settings stay in this browser, including the compact remote;
they do not change radio settings. Keyboard users can skip to the main content.

Want to run the software tests yourself? The [testing guide](docs/testing.md)
explains each command, the expected result and what a passing test does **not** prove.

## First setup and adding a radio

Connect each SoundTouch radio to the home LAN yourself, then open **Setup**.
Discovery starts only when you press the visible scan button. Select one or
more radios, review identity/profile, server target and backup status, open the
preview, start the job and wait for per-radio read-back.

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

Use **Online Station Search** to add a station, choose the radio and slot in
**Preset Builder**, and wait for radio read-back. **Copy Presets** previews and
verifies every target rather than treating a local database write as success.

### Search Internet radio stations

Use Online Station Search, review compatibility information, add the station,
then select it in Preset Builder.

### Create and copy presets

Choose a radio and slot, save, and wait for verified read-back. Copy Presets
shows source/target radios and verifies every copied slot.

## Playback and recovery

Radio `/now_playing`, source and play state are authoritative. Provider,
stream, reporting and metadata evidence are secondary and remain separate.

Automatic recovery is limited to safe stages:

1. radio read-back;
2. metadata refresh;
3. provider refresh;
4. stream URL re-resolution.

Source reselection normally requires explicit controlled action. The standalone
remote additionally offers **Reconnect live radio after stream end**, disabled
by default. Enable it before manually starting an Internet-radio station. A
fresh native `STOP / FINISH` following advancing playback reports can then
trigger one verified same-station selection if the radio reports `INVALID_SOURCE`.
This handles an exhausted live stream without a blind six-hour restart timer.

Reconnect never wakes standby, replaces an active source, operates within a
SoundTouch Zone, writes presets, or sends volume commands. It uses fresh identity
checks, a hashed before-state backup, public-only guarded stream revalidation,
and playback read-back. Explicit application playback/key/zone actions cancel
pending recovery. A lost POST response is not retried. Attempts are limited to
three per hour with a five-minute cooldown; pending jobs expire and are not
replayed at server startup. Device Safe Mode and protection still apply. Manual
radio actions are checked through fresh read-back; the firmware does not offer
an atomic compare-and-select operation, so an external action concurrent with
the final network request cannot be locked by BASSWIESN.

Stop/play, service restart and radio reboot remain controlled manual actions.
Factory reset is never an automatic recovery stage.

## Multiroom

BASSWIESN models master, members, source, clock, output latency and volume as
separate contracts. With **Preserve existing volumes**, BASSWIESN reads volumes
before and after zone creation but sends no artificial `SetVolume`. Any change
made by radio firmware is reported rather than silently corrected.

Optional per-radio start volumes are written and read back before zone
creation. SoundTouch firmware can still resume the last source, clear mute or
normalize volume while forming a zone; BASSWIESN reports that observed change.
Stopping a complete zone remains a normal feature. Removing one member is
firmware-dependent and is therefore exposed only as an experimental LAB tool.

## Remote control, alarms and device settings

Remote Control exposes real radio keys and an optional safe-start-volume
checkbox. When it is off, BASSWIESN does not change volume before playback.
The standalone remote shows station, track, state and actual volume without raw
XML; **Technical details / XML** is collapsed until opened. **Play radios
together** uses the displayed radio as master and lets you select the additional
radios. It backs up, creates and verifies the group without volume commands;
firmware-induced volume changes are shown separately. Current playback must be
suitable for SoundTouch grouping and the selected radios must be standalone.
Alarm & Timer and Device Settings remain fully available in Easy Mode.

## Backup and restore

Setup captures reachable identity, routing, presets and supported device-state
evidence with SHA-256 hashes before critical writes. Restore previews its exact
scope and verifies the resulting radio state. A routing-only rollback is never
called a full device restore.

## Metadata and artwork

Track, artist, album and image URL can update during playback without
reselecting the source, calling `SetURL` or forcing a rebuffer. Artwork shown in
the browser is separate from model-dependent radio display capabilities.
Radio Browser logos are fetched through BASSWIESN's guarded, DNS-pinned raster
cache; the browser never follows an untrusted catalogue URL directly.

Radio Display offers the firmware's normal source symbol, a station logo, or
an explicit **No station logo** mode. Changing this setting previews the
affected slots and updates only `containerArt` after backup and live radio
read-back; source, account, location and item identity are preserved.

**Time in playback title** is available on each standalone remote. New
preferences default to appending the configured local time, for example
`Example Track · 20:15`; existing explicit opt-outs are retained. Without a song
title, the station name is used. Clock metadata uses the existing provider
reporting path and a minimum 60-second clock preference interval, not a new
radio-polling loop. Native standby-clock settings and presets are unchanged.

Artist/title can only be displayed if a metadata source supplies them. The
local station adapter does not yet extract live ICY song information from every
stream. A station-only title and empty artist in provider/read-back data are not
evidence of a broken radio display. Clock text is a metadata projection, not a
firmware patch or a separate native playback-clock region.

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

## Security and safety

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
- The development [DLNA library](docs/dlna-library.md) connects an explicitly
  selected ContentDirectory server, browses folders and imports MP3/AAC tracks
  into Stations through a bounded relay. No automatic LAN scan, NAS password,
  transcoding or generic renderer control. Protocol tests are not radio-audio
  acceptance. It is LAB-only and disabled by default in 3.0.0.
- Local file indexing is separate from DLNA; successfully indexing a format
  does not prove that a radio can play it.
- Local media, announcements/TTS, battery patching, Telnet reboot and
  standby-clock recovery remain experimental or LAB features.
- Factory reset is an exact-profile, explicitly confirmed LAB function. It
  erases radio configuration and can require the user to reconnect Wi-Fi; it
  is never part of automatic recovery.

## Development and tests

The test suite is split by feedback speed:

After installing the Python requirements in your development environment,
install the browsers used by the UI suite (Linux dependencies may require
administrator rights):

```bash
python -m playwright install --with-deps chromium webkit
```

```bash
make test-fast
make test-integration
make test-ui
make test-hardware   # explicitly gated; real devices
make test-release    # complete software suite
```

Visible user workflows are exercised with Playwright, including Chromium and
WebKit for mobile navigation. WebKit simulation is not a substitute for a final
real-iPhone check. Software tests use isolated data and mocked device access;
hardware tests remain separate and require explicit target authorization.

## Docker commands

Run these commands from the unpacked `basswiesn-release` directory:

```bash
docker compose up -d          # start
docker compose down           # stop; preserves the bind-mounted data directory
docker compose restart        # restart
docker compose ps             # status
docker compose logs -f        # follow logs
```

## Updating

For the experimental installer, switch to LAB and use Settings → Updates over
HTTPS with the optional host helper enrolled. Production-host acceptance remains open.
Checking is read-only; installation requires the private administrator code and
explicit confirmation. The helper checks the official archive, backs up the
stopped installation and restores the previous image **and matching data** if
the candidate fails. See [setup, supported hosts and recovery](docs/update-helper.md).

Without that helper, download and verify the new release in a separate directory,
stop the old installation for a consistent backup, then copy its `.env` and
`data/` and run `./install.sh --no-updater`. Keep the old image and backup until
validation is complete. Do not relocate a helper-enrolled installation this way:
its root service is deliberately bound to the original directory and container.
Never use `docker compose down -v` as an update step.

To uninstall, run `docker compose down` and archive the local `.env` and
`data/` before deleting the release directory. BASSWIESN does not provide a
destructive one-command uninstall.

## Backup commands

Stop BASSWIESN for a consistent filesystem backup, then archive configuration
and runtime data:

```bash
docker compose down
tar -czf "basswiesn-backup-$(date +%Y%m%d).tar.gz" .env data
docker compose up -d
```

## Troubleshooting

- Radio appears offline: use the visible recheck action. BASSWIESN can perform
  bounded rediscovery and accepts a new IP only after device-ID verification.
- Preset does not play: run Preset Checker and inspect radio read-back,
  provider and stream evidence. `BROKEN` is not repaired without preview.
- Multiroom volume changed: SoundTouch firmware may normalize it during zone
  formation; inspect the displayed before/after values.
- Setup does not allow a write: verify the exact firmware/product/variant
  profile. Unknown combinations deliberately remain read-only.
- For support, export a redacted diagnostic bundle; never publish `.env`, the
  runtime database or hardware backups.

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
