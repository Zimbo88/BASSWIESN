# LAB listening workbench — unreleased development

The workbench brings seven related tools together in **LAB → Listening
workbench**. It is not part of the released 3.0.0 package. No release or Pi
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
