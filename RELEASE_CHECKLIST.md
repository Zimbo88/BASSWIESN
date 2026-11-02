# BASSWIESN 5.0.0 Release Checklist

Do not push, tag or create a GitHub release while a critical gate is failed or
unverified. Unverified feature-specific hardware
checks require explicit project-owner acceptance and a public limitation; never
mark them passed. A green unit suite alone is not hardware validation.

For 5.0.0, installed-display observations and two-radio reboot observations are separate from
software tests. Native firmware can change boot/group volume; capture and report
that result. Reconnect and restart schedules remain off by default. No blanket
long-run stability or universal display-layout claim is permitted. Privacy,
protected-device, software, package and installation gates are never waived.

## 1. Source and version

Engineering gates (in addition to the existing software/hardware checklist):

- [ ] `python3 tools/check_version.py` passes; OCI label, package and artifacts agree.
- [ ] `python3 tools/check_dependency_lock.py` passes; lock regeneration has no unreviewed diff.
- [ ] Runtime installs only hash-verified, fully pinned dependencies; test tools stay out of the image.
- [ ] `ruff check .`, `mypy`, `make check-js` and measured line/branch coverage gates pass.
- [ ] Dependency, Git-history secret, CodeQL and container scans pass or have individually reviewed, narrow exceptions.
- [ ] Two independently staged source archives have identical SHA256.
- [ ] Isolated non-root container starts with no network, no host volumes, read-only root and all capabilities dropped.
- [ ] SBOM evidence is retained. Source-archive determinism is not a claim of bit-identical apt/OCI rebuilds.
- [ ] GitHub workflow execution is verified separately; a locally created workflow file is not a CI PASS.

- [ ] `git status` contains only reviewed release changes.
- [ ] Backend, Web UI, package and documentation report `5.0.0`.
- [ ] Candidate status remains explicit until release approval; a numeric version is not proof of publication.
- [ ] Keep the owner's confirmed existing MIT license unchanged; review notices and dependency obligations before publication.
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
- [ ] Presentation setup explains its pinned root-SSH requirement before any write; no default password or automatic host-key trust.
- [ ] Display and boot-lettering composition preserves installed battery modifications.
- [ ] System-language mapping and standby-clock defaults are read back, including preserved model-specific fields.
- [ ] Cold activation and boot-guard recovery are verified; an adopted existing patch is not credited as a fresh installation test.

## 5. Hardware validation

Before audio, verify device ID, read volume, set volume `1`, confirm read-back
`1`, and assess firmware behavior before any source/zone transition. These
prechecks do not guarantee a hard ceiling: firmware may restore source volume
or normalize group volume. Defer the test if its required ceiling cannot be
maintained; never treat post-action correction as prevention.

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
- [ ] AP2 bridge claims are separate from native AirPlayReadiness: bounded
      sessions and an automated non-Apple sender do not qualify permanent use.
- [ ] Before enabling production AP2: receiver provisioning, lease supervision,
      fresh relay grants after disconnect, coordinator handoff and crash
      reconciliation pass software tests in the integrated deployment.
- [ ] Genuine Apple acceptance covers each receiver alone, grouped audio,
      individual volume/mute, pause/resume, member and master departure,
      reconnect, metadata and final captured-state restoration. Record audible
      results separately from transport/readback; report measured delay.
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

For the deterministic comparison, reserve two new absolute output paths and
stage both archives after **one** mandatory complete software gate:

```bash
tools/package_release.sh --output-dir "$PWD/build-one" --repro-output-dir "$PWD/build-two"
```

The second archive is assembled in an independent staging directory; the script
compares bytes and rejects pre-existing output paths. There is no skip-tests
option. Both release runners stop on the first failing test and preserve its
trace rather than spending time on fixture-error cascades. This does not omit
any selected software test from a successful run.

Expected public assets:

- `dist/basswiesn-docker-release-5.0.0.tar.gz`
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
- [ ] Web UI reports version `5.0.0` with the correct candidate/stable status.
- [ ] Setup entry is visible on desktop and mobile.
- [ ] Page load performs no discovery or radio probe.
- [ ] Discovery starts only after visible user action.

## 9. Public repository gate

The owner requests a new one-root-commit public repository for 5.0.0. This is
a **final publication operation**, not part of local preparation. Until every
critical gate passes, the license decision is recorded and final publication
is approved, leave GitHub and existing local history unchanged. Never use a
force push to simulate this operation.

- [ ] Resolve and verify exactly `Zimbo88/BASSWIESN`; no other repository is in scope.
- [ ] Preserve a complete mirror and all-ref bundle; `git fsck --full`, bundle verification and SHA256 checks pass.
- [ ] Export repository settings, topics, Pages, issues/PRs, discussions and releases with every asset; preserve any wiki. Unavailable/incomplete exports block deletion.
- [ ] Preserve the existing local worktree, uncommitted work and private evidence separately; a remote mirror does not contain them.
- [ ] Final assets and checksums are stored separately.
- [ ] A publishable-source staging tree contains no private/internal artifacts.
- [ ] Review license, notices, publishable tree and final gate evidence before authorizing the destructive step. Backups remain local permanently.
- [ ] Create and verify one root commit in a separate staging repository, never by resetting the working repository.
- [ ] Only at approved publication: delete/recreate the exact repository, upload only the reviewed new history, and verify one reachable commit. Record deletion/recreation receipts privately.
- [ ] Tag `v5.0.0` points to that commit; no old public tags/releases were copied.
- [ ] Restore intended repository settings and validate hosted workflows and fresh clone before calling publication complete.
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
