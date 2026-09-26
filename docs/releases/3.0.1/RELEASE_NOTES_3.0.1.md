# BASSWIESN 3.0.1

Prepared release notes. Publication and installation must be verified separately.

## What changed

- LAB gains a listening workbench: saved-state diagnosis, metadata freshness,
  recently heard stations, private remote QR links, snapshot comparison,
  listening-time CSV and saved format inventory.
- Question marks now open contextual help. Ten translated tutorials explain
  everyday workflows and the AirPlay, DLNA, updater and workbench boundaries.
- All 28 UI locales receive new help/control vocabulary and tutorial steps.
  Compact remotes and restarts retain the chosen language. Untranslated
  specialist text falls back to English; native-language coverage is not complete.
- Tutorial illustrations adapt to small screens and do not expose real devices.
- A repeated-translation loop in Traditional Chinese is prevented.

## Safety and scope

Workbench inspection reads saved BASSWIESN data. Only explicitly confirmed
replay starts a radio at requested volume 1, using the existing safety guards;
there is no automatic source/volume restoration. Firmware can still change
volume. Diagnostic snapshots are not hardware backups or restore certificates.

No firmware changes, radio tests or Pi deployment form part of this offline
3.0.1 authoring run. AirPlay bridge, DLNA audio and production updater acceptance
remain pending; these features are not promoted out of LAB. Existing protected
device checks remain in force.

## Kurz auf Deutsch

Die neue LAB-Hör-Werkstatt bündelt gespeicherte Diagnose- und Hördaten.
Fragezeichen und Anleitungen erklären die Bedienung, einschließlich der
Grenzen experimenteller Funktionen. Alle 28 Oberflächensprachen erhalten neue
Hilfetexte und Anleitungen; fehlende Spezialübersetzungen bleiben Englisch.
Die Geräteabnahme der LAB-Funktionen bleibt ein eigener Schritt.
