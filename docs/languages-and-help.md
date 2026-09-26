# Languages and contextual help

BASSWIESN offers 28 interface locales: German, English, French, Spanish,
Italian, Portuguese, Dutch, Danish, Swedish, Norwegian Bokmål, Finnish, Polish,
Czech, Slovak, Hungarian, Romanian, Bulgarian, Croatian, Slovenian, Greek,
Turkish, Russian, Ukrainian, Japanese, Simplified Chinese, Korean, Thai and
Traditional Chinese. The web locale does **not** change the radio firmware's
language setting.

## Where to find explanations

Use **?** in a page heading for its workflow and important boundaries. Open
**Tutorial** to step through an illustrated explanation. Dedicated links are
available at the workbench, DLNA library, update installer, AirPlay readiness,
presets, multiroom, display and compact remote. Additional question marks
explain factory reset, battery experiments, SSH and restarts.

The ten tutorials have three translated steps in every locale. The illustrations
are responsive, translated flow diagrams, not screenshots of somebody's radios.
They work with touch, keyboard, Escape and screen readers. Opening or advancing
a tutorial does not discover, read, reconfigure, restart or play a radio. Close
the guide to return to the original control; it never presses that control.

AirPlay readiness is not the BASSWIESN bridge. A visible receiver is not proof
of audio. The bridge remains a disabled LAB preview. DLNA import does not start
playback or write presets; real-radio audio acceptance remains pending. Update
checks do not install. LAB installation needs the enrolled host service, HTTPS
and an administrator code and may interrupt active streams. Guides do not
override any of these requirements.

## Translation boundary

Version 3.0.1 adds 87 help/control terms per locale and 30 tutorial steps per
locale. Existing core translations are retained. New LAB widgets and standalone
pages use a shared exact-phrase catalog and English fallback. Additional
specialist context currently has complete DE/EN copy; other locales visibly
mark those English paragraphs. The original German About essay is preserved;
the English essay remains the fallback for other locales.

**This is not a claim that every older diagnostic sentence is natively
translated, or that every language has received a native-speaker review.**
Protocol tokens, URLs, station names, track titles, device names and raw XML
are data, not translation targets. New widget values are not fed through the
legacy DOM translator. Error codes can remain technical identifiers.

## Offline maintenance checks

```sh
node tools/audit_languages.js --check
node tools/audit_languages.js > language-coverage.json
python -m pytest tests/test_help_languages_301.py -q
```

The audit checks catalog presence, non-empty help, tutorial parity, scoped
fallback and stable repeated translation. It reports English-equivalent entries
separately; shared words such as `Album` or protocol terms are not necessarily
missing translations. Counts do not measure linguistic accuracy. The tool does
not call an online translation service or contact hardware.

Catalogs live in `static/js/translations.js`, `language-extension.js`,
`locale-301.js` and `help-content.js` under `basswiesn/app`. Keep the ordered
vocabulary rows aligned, preserve confirmation semantics and run the audit
after edits. Never translate a protocol token, confirmation value or payload.
Human corrections to terminology and naturalness are welcome.

## Deutsch

Die Fragezeichen erklären den jeweiligen Ablauf; **Anleitung** zeigt ihn in
drei illustrierten Schritten. Die neuen Anleitungen sind in allen 28 Sprachen
vorhanden. Technische Spezialtexte sind teilweise weiterhin Englisch. Keine
Hilfe führt Aktionen aus. AirPlay, DLNA und Updateinstallation behalten ihre
LAB-Grenzen; ein übersetzter Button ist kein Nachweis einer Geräteabnahme.
