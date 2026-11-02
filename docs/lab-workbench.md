# LAB listening workbench and offline practice

The workbench brings seven related tools together in **LAB → Listening
workbench**. It is included in 3.0.2. No update or Pi
installation is performed by opening it. Easy and Standard do not expose it;
every workbench API also requires the saved LAB mode. Mode selection is a
feature boundary, **not a substitute for authentication or a guest role**.

## Tools

1. **Playback diagnosis:** compare saved control, playback, provider, reporting
   and restriction observations. Warnings are independent. Reporting failure,
   expired metadata or an inactivity deadline alone does not establish why
   audio stopped. The conclusion remains “Cause not established”; no automatic
   restart or repair is attempted. A chronological event window retains up to
   the newest 100 events over 1–168 hours and explicitly reports truncation.
2. **Metadata origin and freshness:** inspect saved station/title/artist/album,
   provenance, age and presence of an artwork reference. Records older than
   five minutes, future timestamps and mismatched sources are identified.
   Source equality does not prove session equality. No external image is
   fetched by this panel; artwork failure is not playback failure.
3. **Recently listened:** up to 40 distinct stations from the newest 2,000
   confirmed non-internal history records. Deleted/unmapped stations remain
   visible but cannot start. Replay uses the current Station mapping, never a
   historical expiring URL. A revision check rejects a changed mapping. The
   explicit confirmation starts the selected radio at volume 1 through the
   existing playback identity, policy, audio-lock and readback path. This is a
   real playback action, **not automatic restore** of a previous source or
   volume. Hardware audio acceptance is still pending.
4. **Remote QR shortcut:** generate an SVG locally, encoding the current web
   origin and stable radio ID. No external QR service or LAN query is used.
   There are no administrator codes or credentials in the code, but the server
   address and radio ID are private household information. Opening the linked
   remote can query the radio. Reverse-proxy URL subpaths are not supported.
5. **Cached-state snapshots and differences:** save up to 10 diagnostic
   snapshots per radio, 200 globally, in the existing application database.
   Compare the saved subset against the current cache, export JSON with its
   SHA256, or explicitly delete a selected snapshot. No rolling deletion takes
   place when the limit is reached. Integrity is checked before export/compare.
   Labels and identifiers are omitted from exports; raw XML, URLs, account
   identifiers and arbitrary log payloads are never captured. Track/model text
   may still be personal: review exports before sharing. Observation timestamps
   and age are excluded from content comparison. This is **not a hardware
   backup**, configuration snapshot, rollback or EXACT restore certificate.
   The SHA256 covers the UTF-8 JSON `snapshot` object serialized with sorted
   keys, `ensure_ascii=False` and compact `(',', ':')` separators, not the
   pretty-printed download envelope or its optional local label.
6. **Listening CSV:** export 1–365 days of UTC history, with overlap clipping,
   bounded open intervals, explicit estimated durations and truncation status.
   Internal/unconfirmed/failed rows are excluded. Spreadsheet formula prefixes
   are escaped. No stream URLs, IP addresses or device IDs are exported; station
   names can still be personal. Durations do not measure audible output.
7. **Stream-format inventory:** inspect the saved codec/format hints of up to
   500 stations. MP3/AAC are candidates, not fresh playback confirmations. HLS
   is marked as requiring adaptation for this path; no native-HLS claim,
   stream probe or transcoder is started.

## Practise without a radio

Open **LAB → Practice and display profiles → Open offline tools**. This is a
separate, synthetic learning panel, not a live monitor. Choose one of seven
scenarios and press **Next observation**. You can restart the example at any time.

| Scenario | What it teaches |
|---|---|
| Connection lost | An old failed request does not describe the current connection |
| Address changed | Verify the device identity before trusting another address |
| Missing song information | Missing or old titles do not establish an audio failure |
| Reporting failed | Reporting retries and playback are separate observations |
| Provider unavailable | Compare provider and playback at the same time |
| Invalid source | Distinguish the source/account mapping from the stream itself |
| Group only partly joined | A master response does not confirm all members |

Each example has four observations. Explanations distinguish what is known,
what it could mean, a safe next step and what is **not** proven. Recovery in the
example is fictional. The panel performs no discovery, stream probe, radio
command or automatic recovery, and saves no simulated radio state.

## Reusable display profiles

In the same panel, enter a name, select the display fields and use **Move up** /
**Move down** to set their order. Preview a normal title, missing metadata or
long text. The preview uses the same title-line composer as the application,
but only fictional values. It does not promise a particular radio font or layout.

**Save profile** stores a layout in BASSWIESN, not in a radio. Up to 30 profiles
can be kept. A blank name or invalid field list is rejected. When full, review
and explicitly delete a profile; no old profile is silently removed.

To apply one later: stay in LAB, open a radio's remote, expand **Named display
profiles**, load the list, select a profile and choose **Copy to display editor**.
This changes only the visible draft. Check it, then use the existing **Save
display** button. That separate operation saves and reads back the per-radio
display preference. Profiles are not snapshots, backups or automatic restore.

## Clearer cached diagnosis

Expand a warning in the listening workbench to see its meaning, a safe next
step and the limit of the evidence. Entries are ordered by the earliest known
observation where a timestamp exists. Earlier evidence may be missing; order
alone does not prove causation. The feature never guesses a hardware defect
from a single timeout and does not replay writes.

## Background behavior

Opening the application makes **no workbench API calls**. Open the workbench
explicitly to read its local database projections. An optional checkbox refreshes
only these projections every ten seconds while the LAB panel and browser tab
remain visible. It does not subscribe to radios, run discovery, create server
workers, enable playback, restart services or change presets. Leaving LAB
disables this refresh. A pending read may finish, but never starts a radio write.

The cached information may be missing, stale or incomplete. The workbench does
not hide these limitations by probing devices. Protected radios are excluded
from the chooser and rejected by direct per-device APIs as well.

## Local software verification

```sh
.venv/bin/python -m pytest -q tests/test_lab_workbench.py
.venv/bin/python -m pytest -q tests/test_lab_workbench_browser.py
.venv/bin/python -m pytest -q tests/test_lab_learning_302.py tests/test_lab_learning_browser_302.py
node --check basswiesn/app/static/js/lab-workbench.js
```

The suite uses synthetic data and loopback-only browser servers. It rejects
household network transport. Chromium and WebKit exercise actual clicks in
English/German, narrow mobile layouts, confirmations, QR rendering, downloads,
snapshot lifecycle and the LAB boundary. The playback call is mocked for these
tests; the established safety lock is separately exercised before transport.

## At-home acceptance

First inspect the cached tools without playback. Then, with an approved radio,
verify identity, save its previous source/volume, explicitly confirm a single
volume-1 recent-station replay, listen, inspect readback and restore the prior
state. Check the QR link on the user's phone and compare cached metadata to the
display. Do not interpret a passing software test as that hardware acceptance.

AirPlay receiver integration, DLNA audio acceptance and production updater
acceptance remain separate joint tasks. These tools do not enable them.
