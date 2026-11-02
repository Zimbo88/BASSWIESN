# BASSWIESN 2.6.5

Individual radio-display layouts, guarded restart controls and better evidence
when an intermittent failure happens again.

## Display what matters to you

The standalone remote now offers independent station, artist, title, time and
other-information choices. Arrange their order with the arrows, inspect the
example preview, then save. The server stores one atomic per-radio preference
and the browser reads it back. No source selection, preset rewrite, reboot or
volume change is needed. The dot before the clock is removed.

All selected fields use the confirmed playback-title metadata contract. Native
station headers, fonts, line count and scrolling are controlled by firmware;
this is not an arbitrary OLED layout editor. Missing information is omitted.
Long fields are shortened while keeping selected fields and the clock represented.

When song/information fields are enabled, a bounded collector can read compatible
ICY station metadata. Public DNS/redirect validation, connection pinning, byte/time
limits, shared per-station caching and failure backoff apply. It never plays or
stores stream audio. Artist/title splitting is conservative; album, artwork,
presenter names and lyrics are not guaranteed. Legacy display settings retain
their meaning until an explicit save.

## Restart one or selected radios

**Radio restarts** is available from Radios and the individual remote. Preview
the exact selection and acknowledge the restart. Every selected device must
pass current identity, exact firmware/profile and mandatory snapshot/routing
backup checks before any command is dispatched. Each backup has verified SHA256.
Up to four commands are dispatched together, then each radio is observed and
read back independently. No arbitrary shell input is accepted.

An unresponsive device cannot be backed up and blocks the batch. A frozen radio
may therefore still need a manual power cycle. Active SoundTouch groups must be
stopped explicitly first. Manual playback interruption needs its own checkbox.
This feature sends no volume command, playback resume or factory reset.

The optional schedule is **off by default**. Select radios, weekdays, local time
and timezone, and explicitly authorize it. Only a standby/standalone batch can
run. Active or unavailable radios, missing backups and conflicting setup jobs
block it. Missed times are skipped, including nonexistent daylight-saving times;
repeated autumn clock hours do not produce duplicate jobs. Ambiguous commands
and interrupted jobs are not replayed after a server restart.

### Hardware boundary

A controlled batch on SoundTouch 30 SCM and SoundTouch 20 Series III SM2, exact
build `27.0.6.46330.5043500`, confirmed command dispatch and endpoint return.
Presets, routing, bass, language, clock display, timeout and latency readbacks
matched. Both radios restored a firmware-selected boot volume rather than the
temporary test volume; some source rows also differed immediately after boot.
The workflow correctly returned a state difference instead of claiming an exact
unchanged state. The test restored both original volumes, the original station
on the playing radio and standby on the other. Runtime timestamps/process state
are not byte-identical after a reboot.

Station/song/time display was confirmed by the owner on the tested SoundTouch30
class. All field subsets and orderings have deterministic offline tests, with
real Chromium checkbox/reorder/save/read-back flows. This does not claim visual
verification of every permutation on every SoundTouch display.

## Capture the next failure

The optional host observer runs independently of the laptop and survives host
reboot. It captures memory/CPU/pressure, process state, container starts/exits/OOM,
bounded application logs and kernel/warning journal entries. It uses the local
Docker Unix socket and host files, never radio probes or LAN discovery.

Private recordings rotate with SHA256 sidecars, fourteen-day rolling retention,
a 1 GiB budget and a 512 MiB free-space reserve. Redaction is not a publication
guarantee: review recordings before sharing. See [installation and operation](../../PI_OBSERVER.md).

## Server reliability and unchanged contracts

Database initialization completes once before child services start. A failed
required child now fails the container rather than leaving an apparently healthy
WebGUI with an unavailable cloud service. Health checks cover all three required
services. The protected-device guard, Radio Browser IPv4 preference, preset
read-back and existing remote/group contracts remain in place.

No proven root cause is claimed for the earlier intermittent receiver freeze.
A stream-specific roughly six-hour EOF and a later receiver connectivity failure
are separate observations; neither establishes a universal timer or a software
fix for every radio. Automatic live-radio reconnect remains opt-in. Firmware can
change volume on selection/grouping/reboot, so no absolute native volume lock
is claimed. Single-member group removal remains LAB/firmware-dependent.

## Validation

Software gates are offline: fast, integration, Chromium and complete release
tests; no radio is contacted by those suites. Publication additionally requires
a sanitized archive, checksum/manifest validation, a fresh install and a fresh
download of the actual GitHub asset. Hardware observations above are kept
separate from software counts and from unperformed long-duration monitoring.
