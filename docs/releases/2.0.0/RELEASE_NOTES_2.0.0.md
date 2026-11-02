# BASSWIESN 2.0.0

Status: validated release build.

## Highlights

- A persistent, browser-driven setup assistant combines exact device profile,
  backup, common preview, per-radio progress, reconnect and final read-back.
- Multiple radios can share one setup job without a single-device failure
  becoming a false all-success or destroying successful results.
- Users connect reset radios to the home network themselves. Discovery starts
  only after a visible button press; BASSWIESN never changes computer or radio
  Wi-Fi settings.
- Central protected-device policy stops configured IPs and stable device IDs
  before HTTP, discovery follow-up, DNS/redirect, CLI, SSH, Telnet, Preset,
  Multiroom and background transports.
- Unknown provider writes fail closed. Confirmed BMX restrictions and the
  product reporting scheduler use separate health and persistence contracts.
- Provider, playback, metadata, reporting, session and stream health are
  independent and correlated in the diagnostics timeline.
- Preset transactions are atomic across backup, radio write, radio read-back,
  verification and local commit. Divergence remains visible for reconciliation.
- Live track, artist, album and image URL updates require no source reselection,
  `SetURL` call or forced rebuffer.
- Multiroom supports explicit no-alignment volume behavior and reports any
  firmware-induced difference without silently correcting it.
- AirPlayReadiness records time-bounded evidence without an MFi bypass,
  authentication workaround or firmware patch.
- Responsive desktop/mobile Web UI, redacted support bundle and append-only
  radio write ledger.

## Setup

Critical setup writes are enabled only for exact researched
firmware/product/variant/platform/model profiles. Unknown combinations remain
read-only. Normal setup uses HTTP and profile-bound CLI port 17000 operations;
SSH is not a hidden prerequisite.

Rollback is labeled with its proven scope. A routing restore is not described
as a complete account/environment restore.

## Safety

- Audio validation verifies identity, sets volume to `1`, reads back `1`, then
  starts playback.
- Public artifacts contain no installation-specific device identity.
- No factory-reset button, firmware flashing, NAND modification, AirPlay/MFi
  bypass or automatic reboot loop is included.
- Supported deployment is a trusted private LAN, not direct Internet exposure.

## Hardware evidence

Release validation covered four user-connected, factory-reset radios on
firmware build `27.0.6.46330.5043500`: one SoundTouch 30, one SoundTouch 20
Series III and two SoundTouch Portables. A visible Chromium setup job
discovered and configured all four radios with independent per-device progress
and final read-back. Further hardware checks covered routing persistence,
preset write/read-back/reboot, playback at volume 1, live metadata, three-radio
Multiroom, member-reboot reconstruction and read-only AirPlay diagnostics.

## Known limitations

- A physical preset-button test may remain a documented manual step.
- Some firmware/source combinations ignore Pause or Stop; BASSWIESN reports the
  failed read-back rather than fake success.
- Preserve-volume prevents a BASSWIESN `SetVolume`, but radio firmware can
  still change member volume while forming a group.
- Browser artwork does not prove arbitrary OLED bitmap support.
- AirPlayReadiness is diagnostic; Pairing, PTP and Audio remain unknown until
  directly observed.
- Offline behavior is dependency-specific. This release does not claim every
  feature works without Internet, Bose services or a running BASSWIESN server.
- No 7-hour or 24-hour stability claim is made unless that exact run completed.
- DLNA, local media, announcements/TTS, battery patching, Telnet reboot and
  Standby Clock recovery remain experimental or LAB.

## Installation

Verify `SHA256SUMS`, extract the release archive and run:

```bash
./install.sh
```

Then open `http://<BASSWIESN-host>:1328`. See `SETUP_READ_HERE.md` for the
factory-fresh workflow and device protection configuration.

## Upgrade

Back up `data/` and `.env` first. Migrations are additive. Do not delete the
previous installation until the new container, database, Web UI and radio
read-back have been verified. Never use `docker compose down -v` as an upgrade
step.
