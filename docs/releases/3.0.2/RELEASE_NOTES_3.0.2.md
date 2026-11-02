# BASSWIESN 3.0.2

## Deutsch

Dieses Update macht Bedienung, Hilfe und Diagnose verständlicher. Es enthält
auch die bisher nur lokal vorbereiteten Verbesserungen aus 3.0.1.

- **Besser lesbar:** größere Schrift, hoher Kontrast und Tastaturzugang zum
  Hauptinhalt. Die Einstellung bleibt in deinem Browser, nicht im Radio.
- **Hörstatistik mit deiner Tagesgrenze:** „Heute“ beginnt um Mitternacht in
  der eingestellten Zeitzone. Sommer-/Winterzeit werden berücksichtigt.
  Offene Sitzungen und gespeicherte Endgründe sind getrennt sichtbar. Das sind
  Schätzungen aus Zustandsmeldungen, keine Messung von hörbaren Aussetzern.
- **Verständlichere Diagnose:** gespeicherte Warnungen erklären Beobachtung,
  Bedeutung und einen sicheren nächsten Schritt. Unbekannte Ursachen bleiben
  unbekannt; nichts wird automatisch neu gestartet.
- **Mehr übersetzte Hilfe:** Fragezeichen und zehn bebilderte Anleitungen in
  28 Sprachen, dazu neue Sicherheitsmeldungen und Bestätigungen. Noch nicht
  übersetzte technische Spezialtexte erscheinen auf Englisch.
- **LAB-Fehlersimulator:** sieben erfundene Fehlerbilder durchklicken und
  Diagnose üben – ohne Radios zu kontaktieren oder Wiedergabe zu verändern.
- **LAB-Displayprofile:** bis zu 30 benannte Anordnungen speichern und mit
  normalen, fehlenden oder langen Titelinformationen ansehen. Erst in der
  Fernbedienung übernehmen und ausdrücklich speichern, um sie anzuwenden.
- **LAB-Hörwerkbank:** gespeicherte Metadaten prüfen, letzte Sender ansehen,
  QR-Fernbedienungslinks erzeugen, Cache-Aufnahmen vergleichen und Hörzeiten
  exportieren. Ein erneuter Senderstart ist eine gesonderte bestätigte Aktion.
- **Klarere GitHub-Seiten:** neue Installationsbefehle zum Kopieren, verständliche
  Feature-Liste und eine Anleitung, was die Softwaretests tatsächlich prüfen.

### Was bleibt im LAB?

Die AirPlay-Bridge bleibt deaktiviert. DLNA-Audio und die produktive Installation
über den Updater benötigen weiterhin die gemeinsame Geräteabnahme. Dieses
Release ändert keine Radio-Firmware und installiert nichts automatisch auf
deinem Raspberry Pi. Hardwaretests sind keine Behauptung dieser Offline-Prüfung.

Installation: [README](../../../README.md#quick-install).
Bestehende Installation: erst [sichern und den Updateweg lesen](../../../SETUP_READ_HERE.md#updating-an-existing-installation).

## English

This update improves reading, help and diagnosis, and includes the work prepared
locally as 3.0.1. There was no separate public 3.0.1 release.

- Larger text, high contrast and keyboard navigation; browser-local preferences.
- Listening statistics use the configured local midnight, including daylight-saving
  changes. Open intervals and recorded endings remain estimates, not audio proof.
- Cached diagnosis explains observations, safe next steps and uncertainty.
- Illustrated help in 28 languages, with additional translated safety copy and
  English fallback for untranslated specialist explanations.
- LAB fault practice with seven synthetic scenarios and no radio communication.
- Up to 30 reusable LAB display profiles, fictional previews and explicit
  copy-to-editor/save behavior. Loading a profile is not a radio write.
- LAB listening workbench, cached snapshots, local QR shortcuts and CSV export.
- Clearer installation, feature, change and testing documentation.

AirPlay receiver integration remains disabled. DLNA audio and production updater
acceptance remain separate LAB tasks. No firmware changes or Pi installation
are performed by publishing this release. Software/browser/package checks do
not certify audible hardware playback or perfect native translation.

Download the versioned Docker archive and `SHA256SUMS` from this release.
The checksum verifies file integrity; it is not a publisher signature.
