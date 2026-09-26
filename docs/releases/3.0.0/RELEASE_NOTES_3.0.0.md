# BASSWIESN 3.0.0

## What changed since 2.6.5

- A mobile More menu that fits the screen, including iPhone/WebKit.
- Light, dark and system themes shared by the main interface and compact remote.
- Volume, playback and presets come first. Display settings are easier to find;
  technical XML stays collapsed.
- Preset checks separate valid configuration from unobserved provider or physical
  button evidence. UNKNOWN is not reported as a broken preset.
- Readable device status, expandable diagnostic events, and listening estimates
  by radio, station and period.
- Station metadata status explains missing or stale artist/title information.
  Display field combinations and ordering remain available.
- A station start now respects a saved audio-safety lock before contacting a
  radio. Read-only previews remain available.
- Official GitHub release checks, the author's original About text, and observed
  publication dates. Dates are fetched explicitly, not invented from build time.
- German/English interface checks and 28 selectable interface locales. Additional
  languages remain partial with an explicit English fallback.

## LAB — deliberately unfinished

The **AirPlay 2 bridge remains disabled**. Receiver provisioning, Apple grouping
and reliable end-to-end audio are not finished. No network addresses are claimed
automatically and no AP2 receiver is started by installing this release.

The **DLNA/music library** is experimental and LAB-only. Explicit server browsing,
MP3/AAC import and bounded relay have software and independent ReadyMedia tests
using synthetic media. Audible radio playback and wider NAS compatibility have
not completed acceptance. No automatic discovery or NAS credentials are used.

**Update installation** is experimental and LAB-only. It requires explicit
installer enrollment, supported Linux Docker/systemd, HTTPS and a separate
administrator code. Isolated installation, cutover and matching-data rollback
were tested; production-host updater acceptance remains open. Ordinary release
checks do not install anything. Existing installations are not enrolled silently.

## Hardware boundary

The iPhone UI was accepted by the user. That does not prove audio, physical display,
DLNA or Apple multi-speaker behavior. Existing 2.6.5 display/restart observations
remain historical evidence, not newly repeated 3.0.0 hardware certification.

Recent controlled station trials stopped behind safety checks; no new audible
PASS is claimed. Firmware can normalize volume during source/group changes:
there is **no guaranteed maximum-volume lock**. Identity guards, protected targets,
backups, readback and saved safety locks are retained. No firmware changes or
claim that unexplained receiver freezes are fixed are included.

## Installation

Use the versioned archive and SHA256SUMS from the official release. Follow the
README quick install. Back up an existing installation and its data first; do
not unpack over running application files. Keep the previous version and its
matching data for rollback. See the bundled update-helper guide before opting
into the LAB host service.

## Kurz auf Deutsch

3.0.0 verbessert die mobile Bedienung, Hell-/Dunkelmodus, Fernbedienung,
Preset-Prüfung, Diagnosen und Statistiken. Der fertige Kern ist veröffentlicht.
AirPlay bleibt deaktiviert; DLNA und Updateinstallation bleiben ausdrücklich
experimentell im LAB. Bestehende Einstellungen und Sicherheitsprüfungen werden
nicht automatisch verändert. Weitere Sprachen sind teilweise übersetzt und
kennzeichnen den englischen Fallback.
