# BASSWIESN 5.0.0

This release brings together the engineering and radio-presentation work since
3.0.2. The central rule remains: sending a command is not the same as confirming
the radio's state.

**AirPlay 2, DLNA and automatic update installation retain their LAB boundaries.**
A stable core release does not certify unfinished features. The existing MIT
license is unchanged. The actual publication timestamp comes from this version's
GitHub release, not a build or test date.

## Deutsch — Änderungen seit 3.0.2

### Einrichtung, Display und Rückbau

- Das Setup bietet **Normal** und **Mit Patches**. Normal bleibt Standard und
  verändert keine Firmwaredateien. Bereits vorhandene Patches bleiben erhalten;
  Normal bedeutet nicht automatisch Rückbau.
- Die optionale Anzeige trennt Sender, Interpret/Titel und zentrierte Uhr.
  Fehlen sinnvolle Songinformationen oder wird nur der Sender wiederholt,
  kann eine größere Uhr erscheinen. Fehlende Metadaten werden nicht erfunden.
- BASSWIESN-Bootschriftzug und Multiroom-Kennzeichnung ergänzen die Darstellung
  auf unterstützten Geräten. Der Schriftzug ersetzt nicht garantiert das
  allererste Bootloader-Bild.
- Geräteidentität, genaues Firmwareprofil, Dateihashes und Sicherungen werden
  vor Änderungen geprüft. Aktivierung erfolgt über kontrollierten Kaltstart,
  nicht durch unkontrolliertes Ersetzen der laufenden BoseApp.
- Sprache und unterstützte Standby-Uhr-Einstellungen werden angewendet und
  zurückgelesen. Geräte ohne Display erhalten keinen Displaypatch. SoundTouch 10
  bekommt dadurch keine pauschale Schreibfreigabe: Exakte Modell-, Plattform-
  und vollständige Firmwareprofile bleiben maßgeblich.
- Originaldateien lassen sich über einen eigenen Rückbauweg wiederherstellen.
  Bereinigung betrifft nur das verifizierte inaktive Patchpaket nach externer
  Archivierung. Batterieänderungen und sonstige Gerätedaten bleiben erhalten.
- Einrichtung, Rückbau und erneutes Patchen wurden vom Projektinhaber auf der
  getesteten Flotte angenommen. Das ist keine Universalabnahme aller Modelle,
  Firmwarestände oder Wiederherstellungsfälle.

### Fernbedienung, Wiedergabe und Multiroom

- **Ein/Standby in der einzelnen Fernbedienung:** Ein bewusster Tastendruck
  übermittelt jetzt die erforderliche Bestätigung. Die serverseitige
  Schutzprüfung und das tatsächliche Rücklesen bleiben erhalten.
- **Lautstärke im allgemeinen Fernbedienungsmenü:** Die Oberfläche versteht den
  zurückgegebenen Zahlenwert einschließlich Null, prüft Zielgerät und Wert
  und fragt den Zustand erneut ab. Eine erfolgreiche Änderung wird nicht mehr
  wegen eines falsch erwarteten Wahrheitswerts als Fehler dargestellt.
- Beim Aufwachen darf derselbe Sender von BUFFERING zu PLAY_STATE wechseln,
  ohne dass die Transaktion ihren eigenen Fortschritt verwirft. Andere Quellen,
  Lautstärken und Gruppenzustände bleiben Abweichungen.
- Kein blinder zweiter POWER-Befehl, keine automatische Wiederholung eines
  unklar ausgegangenen Schreibbefehls und keine Abschaltung von Schutzprüfungen.
- SoundTouch-Multiroom kann vorhandene Lautstärken beibehalten. BASSWIESN gleicht
  sie dann nicht künstlich an; Änderungen durch die Firmware werden ausgewiesen.
- Presets behalten Sicherung, Revision, erwarteten Vorzustand und
  Write/Readback/Commit. Ein gespeicherter Slot ist noch kein Hörtest.
- Provider, Stream, Wiedergabe, Metadaten und Reporting werden getrennt beurteilt.
  Ein fehlendes Cover wird nicht als ausgefallene Audiowiedergabe dargestellt.

### Oberfläche, Sprache und Hilfe

- Easy, Standard und LAB, mobile Navigation, Hell-/Dunkel-/Systemdarstellung
  sowie einklappbare technische XML-Ausgaben bleiben erhalten.
- Preset-Aktionen wie „Von Radio“, „Auf Radio“ und „Jetzt übertragen“ haben
  englische Übersetzungen und englischen Fallback für weitere Sprachen.
- 28 Sprachen bleiben auswählbar. Fragezeichenhilfe und illustrierte Tutorials
  erklären Alltag und LAB. Fachtexte sind teilweise Englisch; vollständige
  muttersprachliche Prüfung aller Texte wird nicht behauptet.
- Die Über-Seite dankt Christian (@scheilch) für
  [OpenCloudTouch](https://github.com/opencloudtouch/opencloudtouch), in allen
  28 Oberflächensprachen. Der persönliche Originaltext bleibt erhalten.
- Entwicklungsstand und öffentliches Releasedatum werden unterschieden.
  GitHub-Abfragen erfolgen ausdrücklich auf Nutzeraktion, nicht heimlich bei
  jedem Seitenaufruf.

### Sicherheit und technische Grundlage

- Der HTML-Diagnosebericht maskiert Eingaben und gespeicherte Inhalte.
  Geprüfte XML-Parser verweigern DTDs und zusätzliche Entities.
  Die lokale Update-Vorschau liest nur bereitgestellte Archive unter
  `data/uploads/updates/`; betroffene Diagnoseantworten geben keine rohen
  technischen Exception-Texte mehr aus.
- Große Backend- und Browserbereiche wurden schrittweise aufgeteilt, ohne
  Framework-Neubau oder absichtliche Änderung bestehender URLs.
- Identität, Protected Devices, exakte Profile, Sicherungen und Rücklesen
  bleiben verbindlich. Unbekannte kritische Kombinationen bleiben gesperrt.
- Optionale lokale Admin-Anmeldung nutzt Argon2id, serverseitige Sessions,
  CSRF-Schutz und beschränkte API-Tokens, getrennt von Radio-Endpunkten.
  Bestehender Legacy-LAN-Betrieb wird nicht automatisch authentifiziert.
- CLI/Doctor, redigierte Supportdaten, Ereignisse, Migrationstests und
  Transaktionskoordination erleichtern Diagnose und Wiederherstellung.
- Laufzeit- und Entwicklungsabhängigkeiten sind exakt mit Hashes gebunden.
  certifi, filelock, msgpack und uv wurden nach dem Audit aktualisiert.
- Der nur beim Bauen benötigte pip-Installer wird aus dem Laufzeitimage entfernt.
  Updates ersetzen Images, statt Pakete im laufenden Webdienst nachzuinstallieren.
- Der Container bleibt nicht privilegiert: eigener Benutzer, schreibgeschütztes
  Root-Dateisystem, keine zusätzlichen Capabilities und kein Docker-Socket.
- Software-, Browser-, Sicherheits- und Releaseprüfungen ergänzen die lokale
  Prüfung. Quellarchive werden unabhängig zweimal zusammengestellt und auf
  Bytegleichheit geprüft. Prüfsummen und Komponentenlisten begleiten den Build.

## English — changes since 3.0.2

### Setup and optional radio presentation

Setup offers **Normal** and **With patches**. Normal is the default and preserves
firmware and existing modifications. The optional supported display layout
separates station, song information and a centered clock, with a larger clock
when meaningful metadata is missing. Boot lettering and grouping indication
complement it; replacement of the earliest boot frame is not claimed.

Patch installation requires exact profile/image eligibility, pinned root SSH,
verified backups and cold activation. Original-file restoration is separate from
Normal setup. Cleanup removes only verified inactive patch files after off-device
archival; battery changes and unrelated files are preserved. Display-less models
cannot receive a display patch. Unsupported write profiles remain unsupported.
Language and supported standby-clock defaults are read back. The owner accepted
setup, restoration and renewed patching on the tested fleet, not every product
or possible failure scenario.

### Remote fixes and everyday operation

The compact remote supplies explicit confirmation for a user-requested POWER
action. The main remote correctly validates numeric volume receipts, including
zero, and independently reads actual state. A POWER wake recognizes same-content
buffering-to-playback progress while preserving divergence checks. No blind
POWER replay is introduced.

Presets, local playback and SoundTouch Zones retain identity, backup, revision
and readback checks. Preserve-volume grouping sends no BASSWIESN alignment;
firmware changes remain visible. Provider, playback, metadata and reporting
failures remain separate. HTTP success or PLAY_STATE is not independent proof
of audible output.

### Interface and engineering

The final security review fixes HTML escaping in telemetry reports, rejects
DTD/entity declarations in the reviewed protocol parsers, bounds local archive
previews to `data/uploads/updates/` and removes raw exception messages from the
affected diagnostic responses. Write/readback safeguards remain unchanged.

Easy/Standard/LAB, mobile navigation, themes, collapsed diagnostics and 28 UI
languages remain. Remaining preset action labels have English fallbacks.
Contextual help and illustrated tutorials explain normal and LAB workflows.
The Christian/OpenCloudTouch acknowledgement is translated into all 28 locales;
the original author statement is preserved. Specialist text can remain English.

Backend/browser modules are more focused without a framework rewrite. Optional
admin authentication, scoped tokens and CSRF are separated from device-facing
contracts. Legacy LAN mode remains unauthenticated until explicitly migrated.
CLI/doctor, redacted support data, transaction ownership and migration/recovery
checks improve diagnosis without inventing hardware success.

Runtime and development dependencies are hash-locked. Targeted dependency fixes
and removal of build-only pip reduce known image findings. Runtime remains
non-root, read-only and unprivileged, without a Docker socket. Software/security
workflows and deterministic source-archive checks are included. Bit-identical
source archives do not promise bit-identical independent OCI builds.

## Installation and upgrades

Use **basswiesn-docker-release-5.0.0.tar.gz** and **SHA256SUMS** from the official
release. The [README](../../../README.md#quick-install) has copy-and-paste
commands for a new Linux/ARM64 Raspberry Pi or x86-64 installation with Docker
and Compose. GitHub's automatic source archive is not the installer package.

For an existing installation follow the
[upgrade guide](../../../SETUP_READ_HERE.md#updating-an-existing-installation):
stop the old service, back up the complete data directory and hidden environment
file, preserve ownership, stage the new version and verify health. Keep the old
image and backup until accepted. Do not run two instances on the same database
or delete volumes. The experimental updater does not remove these prerequisites.

## Known limitations / bekannte Grenzen

| Area | Boundary and consequence |
|---|---|
| AirPlay 2 | LAB. Audible Apple trials do not complete permanent receiver provisioning, session rearming or coordinator handoff. An intermittent parallel test of audio hold after uncertain volume remains unresolved despite later passing runs. No default permanent receivers. |
| DLNA | LAB. Bounded library/relay software and a local fixture path were tested; this is not acceptance of all NAS products, codecs or IPv6 multicast. No NAS credentials, transcoding or generic AVTransport controller. |
| Automatic updater | LAB. Requires separately installed/authorized host helper and administrator onboarding. Final production install/rollback acceptance is separate; no Docker socket or root web runtime. |
| Cold-start playback | A safety readback timed out once after radio reboot during a native build. Explicit reconciliation succeeded. Load causality is not proven. Do not bypass the gate or blindly retry writes. |
| Reboot/zone state | Firmware can normalize volume or change source/account observations. A partial result stays partial, not a claim of unchanged state. |
| Old routing overrides | A persistent legacy SDK override can replace runtime server settings after reboot. Correct only after scoped backup and verify after reboot; do not delete arbitrary configuration. |
| Storage | Old database snapshots may consume GiB. Archive and verify before targeted cleanup; never delete active data or unrelated files for space. |
| Translation | 28 selections and translated help do not equal native review of every sentence. The personal essay has its documented English fallback outside German. |
| Software warnings | Existing deprecation and duplicate OpenAPI operation-ID warnings remain technical debt. Passing tests do not erase them. |
| Hardware | Exact model/platform/full-build gates apply. Unknown combinations remain read-only. Simulation is not hardware approval; hearing and physical display assessment need a person. |

## Verification and evidence

The accepted private core completed **5,541 software tests**, with one
artifact-before-build skip subsequently covered by actual archive verification.
The release build reruns its mandatory software gate; the exact archive's
`release-test-summary.json` records that build's result. Hardware tests are
explicitly excluded from automatic software verification.

Native ARM64 non-root container checks cover legacy and authenticated startup,
empty-database migrations and readiness. The private installation was backed up
and checked before activation. Actual mobile-browser actions on SoundTouch 30
confirmed volume, standby and wake through radio readback and restored the
starting state. That run did not independently hear audio. Owner setup/display
and multiroom observations remain separate evidence.

Dependency and container scans are point-in-time checks, not guarantees against
future vulnerabilities. SHA-256 verifies integrity, not publisher authenticity.
GitHub Actions shows actual hosted results; workflow files alone are not CI
evidence. See the [release checklist](../../../RELEASE_CHECKLIST.md),
[feature status](../../../FEATURES.md) and [radio patch guide](../../RADIO_PATCHES.md).
