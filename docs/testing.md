# Checking BASSWIESN without radios

The software workflow includes a separate native ARM64 hosted-runner job for
the real runtime image, required-auth/validation startup and additive migration
probes. The security workflow scans AMD64 and ARM64 independently; a green scan
on one architecture never certifies the other. Neither hosted ARM64 nor local
QEMU execution is a physical Raspberry Pi, network or audio acceptance. Preparing
these workflows locally does not mean they have run on GitHub.

Authenticated browser navigation covers EASY, STANDARD and LAB at desktop and
iPhone-sized viewports. The script policy is tested using a normally loaded
same-origin script: privileged browser automation evaluation cannot prove CSP
enforcement. Late health replies and action outcomes must remain bound to their
original device. Opening a view must not issue a device command; the existing
browser service-status log endpoint is only a local diagnostic observation.

Remote-key safety is covered by `tests/test_key_action_safety.py`: identity and
profile changes, protected targets before transport construction, durable START
and ownership, source/account readiness, exact preset selection, volume/mute
readback, unknown-state wake refusal, lost replies, cancellation, backup
corruption, final commit failure and server-selected zone scope. These are
software simulations, never hardware approval. The selective mutation gate
also removes key identity, preset-match and backup-integrity checks and requires
behavioral tests to catch each fault. Existing remote/history/scheduler tests
provide complete synthetic identity/state fixtures, not runtime exceptions.

The [consolidated acceptance sequence](ACCEPTANCE.md) keeps real-device checks
separate from these software gates. See also [database/restore safety](DATABASE_SAFETY.md)
and [offline performance measurements](PERFORMANCE.md). The acceptance validator
checks local records and hashes; it never grants hardware approval.

You do not need to run these tests to install BASSWIESN. They are useful if you
change the code or want to understand what a release was checked against.

## What a test result means

### Radio display behavior emulator

The browser preview and native-function harness below are different from the
implemented setup recipe. Some experiments describe earlier prototype paths;
they do not supersede the [current installation and acceptance contract](RADIO_PATCHES.md).
Installed display/boot lettering has positive owner observations. The complete
candidate setup/recovery path still needs its own hardware acceptance.

To experiment with the **physical radio's display content**, run this independent
developer tool from a source checkout (Python 3.11+, standard library only):

```bash
python3 tools/display_emulator.py
```

Open `http://127.0.0.1:8840/` on the **same computer**. If that port is occupied,
use `python3 tools/display_emulator.py --port 0` and open the address it prints.
Stop with Ctrl+C. This is not the normal BASSWIESN web interface or a Pi service;
it does not load application settings, start workers, open the database or
construct radio transports. Outbound connections and DNS are blocked in the
tool process. Its only listener is loopback, with same-origin JSON requests.

Choose a fictional station scenario, edit artist/title text and feed a metadata
snapshot. Use the field checkboxes and up/down buttons to compare orderings.
The virtual clock and explicit start/stop/+1 second/+1 minute controls let you
inspect pauses, volume overlays, standby and source changes without waiting for
a device. The demo contains a song change, pause/resume and a simulated error.
Seeking the timeline is deterministic. Changing model parameters replays the
same events with the new parameters; it is not a saved calibration of a radio.

| Layer | Fidelity and limits |
|---|---|
| Provider text composition | Calls the **same pure Python function** as runtime; no second JS implementation. Includes the existing 256-character budget, missing-field behavior and legacy fallback. |
| Metadata coalescing | Default 2-second window is informed by static analysis. Exact firmware scheduling has not been ported or hardware-verified. |
| Display raster, font, scrolling, overlay priority | Adjustable behavioral model, not established native values. Default 320 × 112 is a working canvas, **not a panel specification**. Browser glyph support proves nothing about radio fonts. |
| Fixed-region design | Shared, tested 128 × 100 layout model for both previews. Three visual areas: station header, title/artist body and clock/status footer. This is **not** an established radio API capability or installable firmware patch. |
| AirPlay scenario | Synthetic metadata input only; does not implement Apple reception or radio metadata delivery. |
| No-display class | Explicitly has no virtual OLED. Do not infer a model-specific capability from this generic profile. |

The tool does not execute Bose binaries, capture a radio framebuffer or reproduce
DirectFB glyph pixels. The separate `/review` page can display a **previously
captured** private 128 × 100 PNG or 12,800-byte gray8 image beside its models.
Image and optional legally held TTF/OTF font files stay in the browser; they are
not uploaded, bundled or persisted. A matching line break is not pixel-perfect
emulation. Compare timed captures separately to establish scrolling and priority
for each device family. Never publish the model's output as hardware evidence.

Open `http://127.0.0.1:8840/review` to test the fixed-area layout. The third card
is the selected design. Thirteen synthetic scenarios cover radio/presets, AirPlay,
Bluetooth, AUX, DLNA/NAS, a local library, a Spotify-shaped metadata input, zone
membership, Unicode and literal HTML-like text. These labels describe **input
classes**, not implemented or certified receivers/providers. Playback, pause,
buffering, stopped, standby, errors, volume/mute and system notices are explicit
controls. The clock advances only with model time, not the workstation's clock.

Long titles paginate inside the middle area every five model seconds; artist
pages change every seven. A source change clears the preceding song and rejects
late metadata from its old generation. Missing song data gets a truthful fallback;
AUX does not invent a metadata channel. Pause and reduced-motion hold the first
text page. Duplicate metadata and minute changes do not reset the paging anchor.
The station name is explicitly ellipsized if needed. Critical battery, setup and
update notices take priority; volume zero remains distinct from mute. All these
priority/timing choices are **proposed behavior**, not recovered native rules.

The review includes an independent standby-clock design. Select **Standby** and
use its clock controls: on/off, 12/24-hour format, synchronization and simulated
brightness. The large clock contains no previous song text. Missing clock sync
shows `--:--`; disabled/zero-brightness remains blank. Playback field selection
does not inadvertently disable the standby clock. The proposed policy dims after
30 seconds and shifts within nine bounded positions at minute intervals. Repeated
standby reports and metadata do not reset this timer. Reduced-motion prevents
text animation but deliberately does not pin the clock at one OLED position.
These are preview values, not recovered brightness calibration or an OLED
lifetime guarantee. Critical battery/setup/update pages still take precedence.

Open `http://127.0.0.1:8840/boot` for the separate boot-screen study. It places
an original, geometric BASSWIESN wordmark **above** the existing status field.
The derived 128 × 100 gray8 progress model matches 200 frames from an offline
execution of the archived ARM splash writer: values 1–100 in both of its progress
paths. The normal path is called by the archived init script; the other path's
caller is not established. Input values are internally scaled, not literal fill
percentages. Zero produces **no write**, so the preview keeps its previous frame.
The tool has no automatic animation, framebuffer access or installation action.
The wordmark uses authored pixel shapes, not a copied manufacturer logo/font.

The native splash writer emits a complete frame on each update, which would
erase a logo drawn by an independent writer. A private freestanding ARM
compositor instead decorates the same frame; tests prove every non-logo byte,
including the complete status field, is unchanged. An exact-hash private copy of
the splash writer calls that compositor at its original write site. It has also
run in isolated QEMU user mode with the archived loader/libc and a **regular
test file** in place of the framebuffer. That emulator exercise installs nothing
on a radio; complete proprietary candidates are not distributed. The separate
setup recipe now installs qualified boot-writer changes as documented above.
An earlier bootloader image, driver setup,
handover to BoseApp and safe boot integration remain unverified. The native
writer also ignores a failed/short framebuffer write: its exit status is not
proof that the physical panel displayed anything. The preview must not be
presented as BASSWIESN-server health or readiness.

Static inspection of the analyzed 27.0.6 BoseApp now-playing view found five image
items and **one** metadata text item, at x=4, y=43, width=120, height=56. Its text
item constructor stores separate rectangle coordinates, and its renderer supports
positioned text. This makes a multi-region change plausible, but changing a font
size or rectangle cannot create independent station/title/artist/clock bindings.
The proposed title/artist/body geometry is new, not a claim about the original.
That static finding alone could not supply the bindings or prove lifecycle and
priority behavior. The later qualified setup recipe supplies authored hooks,
exact-image guards and backup/recovery; it does not turn this early geometry
experiment into proof of all fonts, scripts, OLED behavior or device variants.

A separate private CPU harness executes exact-image original item constructors,
destructors, dynamic-item lookup/update, owner-association checks, text wrapping,
font selection and draw-command generation. It confirms that these are
`CValueDisplayItem` instances and that ID lookup stops at the first matching
item. An authored ARM adapter creates four independently addressed test items;
synthetic registry identities are not live firmware registrations. Owner mismatch
is rejected by the original code, not bypassed by the adapter.

The private harness also executes the matching original shared-library registry:
registration, ID/name lookup, vector growth and removal, plus the original string
PDO constructor and deleting destructor. Names are compared case-insensitively
for ASCII fixtures; the ID lookup compares the numeric ID, not its description.
The native registration method has no success return contract, so the proposed
binding adapter verifies that both lookups return its own object. Conflicts never
authorize replacement of an existing object's registration. Fault-injection tests
check partial-registration rollback without removing unrelated entries.

The bounded constructor/destructor tests free the object and its text/value
storage but do **not** reclaim the separate registry entry. Twenty repeated
lifecycles retain twenty 12-byte entries in the harness. This is not a diagnosis
of a leak across the complete running firmware; any external reclamation remains
unproven. It rules out claiming the prototype is safe for repeated registration
on metadata/page changes. The proposed lifecycle blocks reattachment after
detach or partial-registration rollback until entry ownership is resolved.

The native font selector is an index, not a pixel size. Private review uses the
recovered nominal-size table and scale with **host** font metrics and rasterization.
The device's DirectFB/FreeType rendering is not executed. Original text wrapping
can discard content when a region is full; an offline page controller uses its
accepted UTF-8 boundaries to retain the entire title over multiple pages while
keeping the other regions fixed. This page policy is proposed behavior, not an
installed firmware feature. Unicode byte preservation does not prove glyph
coverage, shaping or language fallback; the default-font review exposes missing
glyphs in some scripts.

These are **native-function tests**, not a booted firmware, installable patch or
hardware acceptance. Allocation, C++ strings, message transport and display
interfaces remain declared test doubles. Bounded destruction tests do not prove
the complete service's lifetime or concurrency safety. The browser preview above
still executes no firmware. For this separate registration prototype, live PDO registration/subscription, owner-thread
integration, native dirty-region clearing, overlays, script-specific fonts,
standby-clock integration and reversible installation remain separate gates.
Mutex and registry-singleton behavior are also declared fixtures: executing the
original constructor does not establish real locking or service startup safety.

The private native clock tests execute its actual formatter for all 1,440 minutes
in both 12/24-hour modes, including midnight/noon and the blank leading digit in
12-hour mode. They also check duplicate-digit suppression, brightness-zero blanking,
the cue-music indicator, dirty notifications and clock activation/deactivation.
Protobuf/PDO access and string storage are fixtures; digit-image selection is
tested, not the clock's physical rasterization. The formatter itself does not
establish synchronization or enablement: those upstream gates must be retained.
Nonzero brightness values select the same images; panel dimming is elsewhere.

JSON export/import preserves the event sequence and the **current** presentation
parameters. It accepts only bounded model events, never device commands, URLs or
credentials. Exports contain whatever text you enter: use fictional data when
sharing them. Nothing is saved automatically. PNG export captures the modeled
display only and is explicitly named as a non-hardware image.

Targeted verification (no real devices):

```bash
python -m pytest -q tests/test_display_emulator.py tests/test_display_layout.py tests/test_station_metadata.py tests/test_clock_metadata_preferences_160.py
node --test tests/js/display-emulator.test.mjs tests/js/display-review.test.mjs tests/js/fixed-display.test.mjs tests/js/boot-display.test.mjs
```

The browser portion needs the development Playwright/Chromium installation.
It checks desktop/mobile controls, local JSON roundtrip, literal HTML-like text,
bounded input and absence of external page requests. State tests cover source
generation isolation, duplicate/late metadata, coalescing, exact overlay expiry,
clock rollover and deterministic replay. The fixed-layout matrix checks 1,170
source/state/notice/zone combinations, both as a pure projection and as real
Chromium canvas output at desktop/mobile viewports. Raster-boundary checks do not
prove native glyph coverage, source interoperability or radio timing. The exported
fixed-layout snapshot explicitly records `hardwareValidated: false` and
`firmwareExecution: false`. These checks do not certify a radio.

Station/recovery tests share one acceptance bundle:

```sh
python -m pytest -q tests/test_station_playback_safety.py tests/test_live_radio_reconnect_260.py tests/test_multiroom_router.py tests/test_keepalive_circuit_breaker.py tests/test_playback_keepalive_research_160.py tests/test_key_backup_retention.py
python -m pytest -q tests/test_preset_browser_safety.py
python tools/check_safety_mutations.py --target station
python tools/check_safety_mutations.py --target reconnect
```

They distinguish the requested station from another station on the same source,
buffering from PLAY_STATE, and observed playback from audible output. Failure
injection covers changed targets/catalog/START, missing source readiness, safe
volume uncertainty, cancellation, final DB failure, timer propagation and stale
browser responses. Recovery tests change firmware, volume, mute and presets
during a provider probe and change account/topology after selection. These are
fully synthetic observations, not certification of real stream recovery.

Preset recovery/profile tests additionally inject malformed and duplicate XML,
changed identity/account/untouched slots, changed local revisions, lost replies,
cancellation and corrupted original intent/START. They assert that local rows
change only after complete readback, that pending recovery evidence survives
retention, and that a profile preview cannot construct a radio transport:

```sh
python -m pytest -q tests/test_preset_apply_safety.py tests/test_preset_recovery_safety.py tests/test_preset_writer_failures.py tests/test_preset_transactions_200.py tests/test_phase3b_preset_integrity.py tests/test_preset_cloud.py
python -m pytest -q tests/test_preset_browser_safety.py
python tools/check_safety_mutations.py --target preset-recovery
python tools/check_safety_mutations.py --target preset-apply
```

The browser checks use Chromium/WebKit and loopback fixtures. Duplicate clicks,
captured target/revision, unverified removal and late responses are tested without
contacting radios. The mutation gate deliberately accepts the wrong source/account
and collateral slot changes in separate child processes; tests must detect both.

The common apply tests cover single presets, cloning and artwork-only updates:
final DB failure, cancellation, changed clone source, stale artwork planning,
lost replies without replay, partial batches, protected/unknown targets, no-op
observation and failed auxiliary status storage. Browser tests additionally
exercise duplicate form submission and late responses for single, profile and
clone actions, plus rejection of an unverified HTTP-success response. These
remain software evidence, not radio or physical-preset-button acceptance.

For a related device-command change, run one combined focused batch before the
final full acceptance rather than rebuilding release archives after every edit:

```sh
python -m pytest -q tests/test_manual_radio_commands.py tests/test_power_action_safety.py tests/test_wireless_diagnostics_safety.py tests/test_key_backup_retention.py tests/test_authenticated_workflows.py
python -m pytest -q tests/test_manual_radio_browser.py
python tools/check_safety_mutations.py --target manual-command
```

The manual-command tests cover durable intent, duplicate operation keys, lost
responses, live identity/profile changes, START tampering, reconnect cancellation,
actual session/CSRF boundaries and absence of credentials in recorded evidence.
Chromium/WebKit exercise preview defaults, confirmation, frozen targets, late
responses, duplicate submission and explicit manual-review recovery. All radio
responses are simulated; these checks do not prove station persistence or a real
Wi-Fi transition. Full release gates still run on the final integration tree.

The scoped automation/authentication checks run without devices:

```sh
python -m pytest -q tests/test_api_tokens.py tests/test_token_boundary.py
python -m pytest -q tests/test_auth_credential_continuity.py tests/test_update_auth_boundary.py
```

They cover route/scope separation, bearer/cookie ambiguity, revocation and
rotation races, private schema backups, rollback authority and existing
protected-target/readback behavior with simulated radios. Coverage checks
include the token store/policy/router and the explicit local schema upgrade;
software evidence does not confer hardware or deployment approval.

Provider, offline administration, diagnostic privacy and token-management UI
checks have separate focused entry points:

```sh
python -m pytest -q tests/test_provider_framework.py tests/test_provider_framework_api.py tests/test_provider_framework_transport.py
python -m pytest -q tests/test_cli.py
python -m pytest -q tests/test_request_observability.py tests/test_retention_privacy_failures.py tests/test_support_export_privacy.py tests/test_request_log_privacy.py tests/test_secret_redaction_boundaries.py
python -m pytest -q tests/test_browser_security_headers.py tests/test_token_management_browser.py
```

Provider transports use simulated responses, CLI checks use temporary offline
snapshots, and browser checks use temporary authenticated loopback servers.
The optional provider remains catalog-only and LAB; no media playback or
external server acceptance is inferred. See [provider boundaries](PROVIDER_ARCHITECTURE.md),
[CLI contracts](CLI.md), and [observability scope](OBSERVABILITY.md).

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
.venv/bin/python -m pip install --require-hashes --only-binary=:all: -r requirements-dev.lock
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
| `make test-release` | Complete non-hardware suite, including browser and release contracts | A built or published archive by itself |
| `node tools/audit_languages.js --check` | 28-language catalogs, help, fallback and stable translation | Native-speaker quality in every sentence |
| `.venv/bin/python -m pytest -m "not hardware" -q` | The complete software suite | Hardware acceptance |

The complete suite can take many minutes. A final line such as `2200 passed`
is a count of checks, not radios. Test counts change as coverage improves.
One pre-package manifest test can be skipped when the release archive has not
yet been built; the actual archive must then be verified separately.

During related backend changes, use focused contracts before the combined
regression instead of rebuilding two release archives after every edit:

```bash
python -m pytest -q tests/test_power_action_safety.py tests/test_wireless_diagnostics_safety.py tests/test_key_backup_retention.py
python tools/check_safety_mutations.py --target power
```

The targeted mutation command runs its original control and two deliberate
faults in fresh interpreters. No argument still runs the complete mutation gate;
CI/release requirements have not been weakened. A targeted pass is not a full
suite, package or hardware pass. The wireless tests also exercise real admin
session/CSRF middleware with an entirely simulated radio.

Administrator-authentication checks exercise three real application factories
with mock/loopback-only transports, private temporary credential stores and
Chromium/WebKit. The locked runtime is also checked in a non-root, read-only,
network-none container in both legacy and required mode:

```sh
.venv/bin/python -m pytest tests/test_admin_store.py tests/test_admin_boundary.py tests/test_admin_browser.py -q
bash tools/test_container_offline.sh basswiesn:local-test legacy
bash tools/test_container_offline.sh basswiesn:local-test required
bash tools/test_container_offline.sh basswiesn:local-test required validation
```

Build the named local image beforehand. No account is created in the host data
directory by the container checks. A passing login test does not certify every
feature workflow, updater cutover or device-source policy; see
[the authentication preview limits](ADMIN_AUTH.md).

Updater/auth integration tests additionally execute the real internal probe in
these containers. Synthetic filesystem tests cover credential changes before
rollback, WAL-only changes, unchanged backup bytes, unknown schemas and session
revocation before worker startup. See [the updater/auth contract](UPDATER_AUTH.md)
for focused commands and remaining real-installation acceptance boundaries.

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

Browser fixtures keep their loopback socket reserved through server startup and
require the server thread to terminate before another database fixture begins.
Startup exceptions retain their original cause; browser cleanup also runs when
setup or assertions fail. The lifecycle tests deliberately inject port and
termination failures. These controls prevent known fixture races; they do not
retroactively establish the cause of an older interrupted run whose original
exception was lost.

For final archive reproducibility, use the paired staging command in the
[release checklist](../RELEASE_CHECKLIST.md). It runs the full mandatory gate
once and assembles two independent archives. Focused tests belong during editing;
the final combined gate belongs on the unchanged integration tree. Do not repeat
the whole suite merely to regenerate the same second archive.

Setup ownership tests use separate SQLite sessions and competing local threads,
not radio connections:

```sh
python -m pytest -q tests/test_setup_lease_safety.py tests/test_setup_transaction_ownership.py
python -m pytest -q tests/test_network_edge_contract.py tests/test_persistent_ssh_transaction.py
```

These cover live-worker startup, stale-session acquisition, expired heartbeat,
duplicate execution, loss after a completed write, and no-replay recovery.
The SSH transaction tests use a fake runner and verify durable backup/intent
before the fixed marker write, followed by independent identity/readback.
They do not execute SSH, validate reboot persistence, or certify real hardware.

The engineering gates are defined in the [software workflow](https://github.com/Zimbo88/BASSWIESN/blob/main/.github/workflows/quality.yml)
and the [security workflow](https://github.com/Zimbo88/BASSWIESN/blob/main/.github/workflows/security.yml). They do not
publish, deploy or run hardware acceptance. Use the [dependency guide](DEPENDENCIES.md)
for pinned environments and [security checks](SECURITY_CHECKS.md) for scanner
scope and the network-isolation boundary.

```sh
PATH="$PWD/.venv/bin:$PATH" make quality
python3 tools/check_dependency_lock.py
.venv/bin/python -m pytest -m 'not hardware and not browser' --cov=basswiesn --cov-branch --cov-report=json:coverage.json -q
python3 tools/check_coverage.py coverage.json
```

Coverage gates use measured line and branch baselines, independently. The initial
values are not a claim that every security component has reached 90%. Raise the
baseline after validated improvements; do not lower it to accommodate a failing
refactor. Browser and hardware evidence are not included in this baseline.

The bounded mutation gate additionally verifies that tests detect seeded faults
in backup manifests, approved HTTP origins, SQLite setup ownership and
account-isolated LAB group state, selection readback, factory-reset preflight
and manual maintenance identity/restore verification:

```sh
make test-mutations
```

It re-executes one small module in each isolated child interpreter; it never
edits the checkout. Each original must pass first. A fault is counted only when
the associated behavior tests fail assertions, not when imports, fixture setup
or the runner crash. The 23 seeds also check optimized update-probe dispatch and
legacy worker-readiness rejection. They are a targeted regression guard, not an
exhaustive mutation score or hardware test.

To test an isolated environment without changing `.venv`, explicitly set
`BASSWIESN_PYTHON=/absolute/path/to/that/environment/bin/python`. Test/release
scripts now respect this override even when the checkout already has a `.venv`.

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

## Browser command uncertainty and stale responses

`tests/js/control-state.test.mjs` exercises the main remote coordinator without
a browser or network. An attempted command is never replayed after a lost
reply. A valid later observation does not make that missing receipt successful;
the explicit refresh barrier stays set for that device. Malformed/mismatching
readback, protection changes, cancellation, double clicks and A → B → A races
are separate cases. Node failures are included in the normal pytest gate via
`tests/test_frontend_modules.py`.

`tests/test_controls_browser_300.py` verifies the real UI in Chromium and WebKit:
single native click after a long touch, power confirmation, no fabricated
listening-history event, lost command replies, disabled writes until refresh,
and stale responses across view/device changes. All radio calls are intercepted
with synthetic fixtures. These are software behavior tests, not audible output
or hardware evidence. The UI barrier is document-local; server-side policy
remains authoritative across clients and restarts.

## Playback observation and listener contracts

`tests/test_playback_observations.py` characterizes the pure, strictly typed
readback interpretation extracted from the keepalive worker. Absent positions
are unknown progress, not stalled playback; station labels are not song titles.
Explicitly cleared metadata differs from absent fields. Nonfinite media
positions and malformed persisted positions must not crash the worker.
Scheduling, recovery, locks and radio write gates remain in their existing
orchestrators. No new automatic playback action is introduced.

`tests/test_artwork_reconciliation.py` interleaves independent SQLite sessions
and delayed mock replies. Both late success and late failure must preserve the
newer committed observation, including failure outcomes and equal timestamps.
`tests/test_internal_service_health.py` rejects reachable error/login pages as
healthy listeners, and verifies bounded, proxy-free, non-redirecting loopback
checks. All three suites use local fixtures or mock transports, not radios.
