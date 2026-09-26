# Checking BASSWIESN without radios

You do not need to run these tests to install BASSWIESN. They are useful if you
change the code or want to understand what a release was checked against.

## What a test result means

- **PASS / passed:** the tested software behavior matched the expected result.
- **FAIL / failed:** stop before publishing; read the first failing assertion.
- **SKIP / skipped:** that check did not run. Read its reason; it is not a pass.
- A mocked playback request does **not** mean music came from a real speaker.

The default software tests use a temporary database, fictional devices and
loopback-only browser pages. They do not need your radio addresses, SSH password,
Wi-Fi password, Pi access or production database. Do not supply those values.

## Prepare a development checkout

This is for the GitHub **source checkout**, not the normal Docker release
archive. Requirements: Python 3.11+, Node.js, Git and a Bash shell. Browser tests
also need Chromium and WebKit with their operating-system libraries.

```bash
git clone https://github.com/Zimbo88/BASSWIESN.git
cd BASSWIESN
python3 -m venv .venv
.venv/bin/python -m pip install -r requirements.txt
.venv/bin/python -m playwright install chromium webkit
```

If Playwright reports missing system libraries, ask the host administrator to
install the listed packages. Do not change radio settings to fix a software test.

## Pick the check you need

Run these commands from the checkout directory, one at a time:

| Command | What it checks | What it does not prove |
|---|---|---|
| `make test-fast` | Small, isolated software behaviors | A full installation or real audio |
| `make test-integration` | Application/database/API interactions | Real household connectivity |
| `make test-ui` | Actual browser clicks using synthetic data | Every phone or a real radio display |
| `make test-release` | Packaging and release-specific contracts | A published GitHub asset by itself |
| `node tools/audit_languages.js --check` | 28-language catalogs, help, fallback and stable translation | Native-speaker quality in every sentence |
| `.venv/bin/python -m pytest -m "not hardware" -q` | The complete software suite | Hardware acceptance |

The complete suite can take many minutes. A final line such as `2200 passed`
is a count of checks, not radios. Test counts change as coverage improves.
One pre-package manifest test can be skipped when the release archive has not
yet been built; the actual archive must then be verified separately.

For the new 3.0.2 features alone:

```bash
.venv/bin/python -m pytest -q tests/test_lab_learning_302.py tests/test_lab_learning_browser_302.py
node tools/audit_languages.js --check
```

These cover all seven simulated faults, profile validation and limits,
daylight-saving boundaries, browser clicks, keyboard use, larger text and
mobile layouts. The remote tests separately prove that loading a profile
does not apply it: saving remains explicit and is followed by readback.

## Release checks beyond unit tests

A release also needs an archive manifest and SHA256 verification, a privacy
and secret review, an isolated clean installation, and a download of the actual
GitHub asset followed by the same checks. A version number in source code alone
does not establish any of these.

Do not run `make test-hardware` casually. Hardware verification is a separate,
deliberately authorized procedure with identity checks, protected-device guards,
backup, low test volume and restoration. It is not required for the commands above.

## Deutsch: kurz erklärt

Für eine normale Installation brauchst du diese Tests nicht. Wer am Code arbeitet,
kann damit prüfen, ob die Software wie erwartet reagiert. `passed` bedeutet
bestanden, `failed` bedeutet Fehler und `skipped` bedeutet nicht ausgeführt.
Ein bestandener Browser-Test ist noch kein hörbarer Radiotest.

Die Befehle oben verwenden Testdaten. Keine privaten Geräteadressen oder
Passwörter eintragen. Bei Fehlern die erste Fehlermeldung ansehen und keine
Radios auf Verdacht neu starten. Für eine Hilfeanfrage nur bereinigte Ausgaben
teilen – niemals die echte Datenbank oder Zugangsdaten.
