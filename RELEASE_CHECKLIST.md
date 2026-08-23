# BASSWIESN 2.0 Release Checklist

Do not push, tag or create a GitHub release while a critical gate is failed or
unverified. A green unit suite alone is not a release gate.

## 1. Source and version

- [ ] `git status` contains only reviewed release changes.
- [ ] Backend, Web UI, package and documentation report `2.0.0`.
- [ ] `git diff --check` passes.
- [ ] No runtime database, logs, `.env`, secrets, private keys, hardware
      backups, research source material or test artifacts are publishable.
- [ ] Public entry documentation is professional English.
- [ ] Claims match current software and hardware evidence.

## 2. Protected-device gate

Public code contains no installation-specific radio identity. Real
installations configure protected targets by both IP and stable device ID:

```env
PROTECTED_DEVICE_IPS=192.0.2.25
PROTECTED_DEVICE_IDS=EXAMPLE-PROTECTED-ID
```

- [ ] Configured IP and device-ID protection is loaded from environment and
      database settings.
- [ ] Direct IP, moved identity, DNS alias and redirect tests pass.
- [ ] HTTP, CLI, SSH, Telnet, Setup, discovery follow-up, Presets, Multiroom and
      background workers stop before transport creation.
- [ ] A passive Web UI load with a protected stored device opens zero radio
      transports.
- [ ] SSDP replies with no identity or a protected identity receive no unicast
      follow-up.
- [ ] The installation-specific audit confirms zero contacts to its protected
      device during final validation.

## 3. Software suites

Run in this order:

```bash
.venv/bin/python -m compileall -q basswiesn tests tools
git diff --check
make test-fast
make test-integration
make test-ui
node --check basswiesn/app/static/app.js
node --check basswiesn/app/static/js/translations.js
make test-release
```

- [ ] Fast suite passes.
- [ ] Integration suite passes.
- [ ] Chromium/Playwright suite passes.
- [ ] Complete non-hardware release suite passes.
- [ ] Warnings are recorded separately from failures.

## 4. Factory-fresh setup

- [ ] The user manually connects every reset radio to the trusted LAN.
- [ ] BASSWIESN changes neither host Wi-Fi nor radio Wi-Fi credentials.
- [ ] Setup remains passive until the visible discovery button is pressed.
- [ ] Newly assigned DHCP addresses are accepted only after advertised identity
      and `/info` agree.
- [ ] Exact firmware, product, variant, platform and model profile are visible.
- [ ] Multiple radios can be selected in one job.
- [ ] Preview and backup status are readable.
- [ ] Progress and errors are independent per radio.
- [ ] Partial failure cannot become all-success.
- [ ] Reboot/reconnect and final read-back complete.
- [ ] Desktop and mobile human flows pass in a real browser.
- [ ] Normal setup uses no hidden SSH requirement.

## 5. Hardware validation

Before audio, verify device ID, read volume, set volume `1`, confirm read-back
`1`, then start playback.

- [ ] Setup write and read-back pass on every release profile in scope.
- [ ] Rollback is tested and labeled with its exact proven scope.
- [ ] Preset read/write/read-back/reboot/restore passes on at least two device
      families.
- [ ] A physical preset-button check is either passed or explicitly marked as
      a manual validation step.
- [ ] Play/source/preset/reconnect behavior uses radio read-back.
- [ ] Ignored Pause/Stop commands are reported as failures, not fake success.
- [ ] Live track/artist/album/image URL changes do not reselect the source,
      call `SetURL` or rebuffer.
- [ ] Multiroom create/join/leave/reconnect and topology read-back pass.
- [ ] Preserve-volume sends no BASSWIESN `SetVolume`; firmware changes are
      reported.
- [ ] BASSWIESN Multiroom preset reconstruction passes.
- [ ] AirPlayReadiness is validated read-only against a positive reference;
      no MFi bypass or firmware modification is used.
- [ ] Offline/cloud dependency results are reported per feature.
- [ ] A 7-hour or 24-hour claim is made only when that exact live run completed.

## 6. Browser QA

Desktop:

- [ ] 1920×1080
- [ ] 1440×900

Mobile:

- [ ] 390×844
- [ ] 393×852
- [ ] 430×932

Check Dashboard, Setup, Radio, Presets, Multiroom, Health, Diagnostics, LAB and
Settings. Confirm no horizontal overflow, inaccessible dialog action, body
scroll lock, clipped progress or unreachable LAB menu.

## 7. Release package

Build only after the preceding software gates pass:

```bash
tools/package_release.sh
```

Expected public assets:

- `dist/basswiesn-docker-release-2.0.0.tar.gz`
- `dist/SHA256SUMS`

- [ ] Archive ownership, order and timestamps are reproducible.
- [ ] `.git`, `.venv`, `.env`, databases, logs, secrets, hardware evidence,
      research trees and reference projects are absent.
- [ ] Manifest and SHA-256 checksums verify.
- [ ] Only public product documentation is included.
- [ ] Configurable device protection remains in the archive without private
      values.

## 8. Clean install from the local asset

Extract the archive into an empty temporary directory and act as a new user:

- [ ] `install.sh` succeeds.
- [ ] Compose configuration validates.
- [ ] Container runs non-root and becomes healthy.
- [ ] Database and migrations initialize from empty state.
- [ ] Web UI reports version `2.0.0`.
- [ ] Setup entry is visible on desktop and mobile.
- [ ] Page load performs no discovery or radio probe.
- [ ] Discovery starts only after visible user action.

## 9. Public repository gate

Repository replacement is destructive and happens only after every critical
gate above passes.

- [ ] Remote owner/name are displayed and verified.
- [ ] A complete local `git bundle` preserves the old repository.
- [ ] Existing remote-only issues/releases/metadata are documented.
- [ ] Final assets and checksums are stored separately.
- [ ] A publishable-source staging tree contains no private/internal artifacts.
- [ ] The new public repository starts with one reviewed commit:
      `BASSWIESN 2.0.0` on `main`.
- [ ] Tag `v2.0.0` points to that commit.
- [ ] English release notes use no unsupported claim.

## 10. Validation from GitHub

After publication:

- [ ] Download the GitHub release asset, not the local source file.
- [ ] Verify its SHA-256 checksum.
- [ ] Extract it into another empty directory.
- [ ] Repeat Compose, first-start, version, Web UI and passive-setup checks.
- [ ] Record the release URL and downloaded-asset evidence.

## 11. Final decision

Release only when every hard gate is `PASS`. If factory-fresh multi-device
setup, protected-device safety, package install or any other critical gate is
not proven, publish no release and write a blocker report instead.
