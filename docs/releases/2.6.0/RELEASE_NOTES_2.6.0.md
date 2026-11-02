# BASSWIESN 2.6.0

Released September 13, 2026. This release has software, browser and clean-install
validation. The new audible reconnect, physical title-clock display and remote
quick-group hardware checks remain unverified; see the validation boundary below.

## Long-running Internet radio

A captured live-radio session ended at the HTTP audio boundary after approximately
six hours. The receiver drained its buffers, reported FINISH, exhausted its
stream alternatives and deselected the source. The application processes stayed
alive. A different station continued past the same duration. This establishes
the missing-reconnect path, not a universal radio timer or a proven broadcaster
policy. No firmware changes or repeated-stream-list workaround are used.

The standalone remote now offers **Reconnect live radio after stream end**.
It is **off by default**. Enable it before manually starting the station. Only
a fresh, attributed FINISH after advancing reports can schedule recovery.
Recovery requires current INVALID_SOURCE, matching identity, a standalone radio,
an enabled device policy, a hashed before-state backup, and a guarded fresh
stream check. It sends one select and verifies actual playback. It never sends
volume, preset, power, reboot or zone commands.

Standby, another source, an active zone, a changed session, expired evidence,
missing backup, protected target or failed stream validation cancel the attempt.
Explicit BASSWIESN playback/key/zone actions invalidate pending recovery. Attempts
are bounded to three per hour with a five-minute cooldown. A missing POST response
is not retried; pending jobs are not replayed on restart. The SoundTouch API has
no atomic compare-and-select operation, so a truly simultaneous external hardware
action cannot be locked by the server.

## A simpler remote

- Station, track/artist, playback state and actual volume are shown as readable
  fields; XML/JSON is collapsed under **Technical details / XML**.
- **Play radios together** uses the radio whose remote is open as the master.
  Select additional radios and confirm. Identity checks, backups and distributed
  topology read-back precede a success message.
- This quick group path sends no volume commands. Firmware can still change
  volume during zone creation; before/after values are displayed explicitly.
- Safe start volume remains optional. Turning it off leaves the current radio
  volume alone before normal remote playback actions.
- Remote controls, preference confirmation and errors have English/German text
  and targeted desktop/mobile Chromium regression coverage.

## Time and metadata

**Time in playback title** is now a normal remote preference, not a LAB-only
switch. New preferences append local time to the title; explicit prior opt-outs
are retained. The application timezone is used, including daylight-saving rules.
When no song title exists, the station name provides the text before the clock.

The projection uses the existing metadata/reporting contract. It does not patch
the display, change native standby-clock settings or rewrite presets. Recognized
radio echoes cannot append the time repeatedly, replace a newer canonical song
title, or falsely refresh stale metadata evidence.

Artist/title still require a supplying metadata source. The local adapter does
not provide universal ICY extraction. Empty artist/title in the provider response
cannot be solved merely by changing the physical display mode.

## Diagnostics and compatibility

- Valid radio reports receive the confirmed response even when diagnostic
  storage is temporarily busy; storage degradation is logged explicitly.
- STOP diagnostics include bounded reason, media position and device attribution.
- Confirmed same-source station switches split playback history; missing or weak
  labels do not invent new sessions.
- The Radio Browser IPv4/dual-stack fix, DNS/redirect guards, device protection,
  preset validation and circuit-breaker recovery are retained.

## Validation boundary

Software tests and mocked browser/radio interactions do not establish audible
hardware recovery, physical display rendering, zone volume preservation or an
exact restore. These new hardware checks were deliberately deferred because
firmware can restore remembered volumes on selection and normalize volume during
group creation. Setting volume before an action is not a hard maximum-volume
guarantee. Do not use grouping where an absolute volume ceiling is required.

The project owner approved publication with these explicit limitations. Reconnect
remains opt-in and off by default, and no audio or group test was performed merely
to complete the release checklist. Existing captured long-playback evidence
supports the diagnosis, not a claim that the new recovery has passed on hardware.

Validation includes the full non-hardware test suite, desktop/mobile Chromium,
archive checksums and a real Docker installation from the release package.
No firmware, preset or radio configuration change is required by the upgrade.
