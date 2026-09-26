# BASSWIESN 3.0.2 Setup Guide

This guide describes the normal end-user setup flow. BASSWIESN is an
independent, unofficial project and is not affiliated with Bose.

## Requirements

- Docker Engine and Docker Compose v2
- a trusted private LAN
- a Linux host that remains available while the radios use BASSWIESN
- SoundTouch radios already connected to that LAN by the user

## Installation

For the complete download, checksum and installation commands, start with
[Quick Install](README.md#quick-install). Use the versioned release archive,
not GitHub's automatically generated “Source code” archive, for normal installation.

From a freshly unpacked release directory, run:

```bash
./install.sh
```

The installer creates the required local directories, creates `.env` from
`.env.example` when necessary, detects usable physical host addresses, builds
the container and starts BASSWIESN. It never replaces an existing `.env`.

Open the Web UI at:

```text
http://<BASSWIESN-host>:1328
```

The local SoundTouch compatibility service uses port `1516`; diagnostics use
port `1860`.

## Updating an existing installation

Do not run the new-install block on top of a running installation. Do not delete
the old folder or use `docker compose down -v`. The database, presets managed
by BASSWIESN, preferences and `.env` belong to your installation, not the release.

For a manual update on a single Docker host:

1. Note the exact old installation directory and running version. Download the
   3.0.2 archive and `SHA256SUMS` into a new directory; verify the checksum and
   unpack it, but do not start it yet.
2. Plan a short interruption. In the **old** installation directory run
   `docker compose stop`. This closes the application before the database copy.
3. Make a private backup of the complete old directory, including hidden `.env`
   and `data/`. Preserve file ownership and permissions. Check that the backup
   can be read. Do not post it in an issue.
4. Copy the stopped installation's `.env` and complete `data/` into the unpacked
   new release. Use the host administrator if permissions require it. Do not
   use a sample database or overwrite a backup with a fresh empty database.
5. Run `./install.sh` in the new release directory. Verify version **3.0.2** in
   About, your radio list, presets and settings. Only then test normal playback.
6. Keep the old installation and its matched data backup. If the update fails,
   stop the new instance before starting the old version with the **old data**.
   Do not run two instances or downgrade using a database changed by a newer version.

Installation layouts differ, so these steps deliberately do not guess paths or
automatically copy/delete your data. If unsure, stop before step 2 and ask for
help with the non-secret layout information. The LAB updater has its own
[administrator setup and rollback guide](docs/update-helper.md); checking for
a new release is not the same as installing it.

## Important Wi-Fi boundary

The user must connect every factory-reset radio to the home Wi-Fi or wired LAN
before BASSWIESN setup begins. Use the normal device/vendor procedure for this
step.

BASSWIESN does not:

- switch or reconfigure the host computer's Wi-Fi;
- provision radio SSIDs or Wi-Fi passwords;
- store household Wi-Fi credentials;
- join a radio setup access point;
- run discovery automatically when the Web UI opens.

## Factory-fresh onboarding in the Web UI

1. Connect each reset radio to the home network yourself.
2. Open **Setup** in BASSWIESN.
3. Press **Find connected radios now**. This explicit action sends bounded
   SSDP multicast and reads `/info` only from the unprotected identities found
   in that invocation.
4. Review the device ID, address, model, exact firmware/build, variant,
   platform and matched write profile for every radio.
5. Select one or more radios and choose the BASSWIESN LAN address reachable by
   them.
6. Open the common preview. Unknown or incomplete write profiles fail closed.
7. Start setup and follow the per-radio progress. One failing radio does not
   turn successful radios into a false failure or a false success.
8. Wait for backup, routing, account, reboot/reconnect and final read-back.
9. Run the optional playback check only when desired. It verifies volume `1`
   before starting audio.
10. Review the final result and the exact rollback scope.

Reloading or navigating away does not stop a persistent setup job. Only one
setup job may write at a time.

## Device protection

Public releases contain no private device identities. Configure devices that
must never be contacted by both IP address and stable device ID:

```env
PROTECTED_DEVICE_IPS=192.0.2.25
PROTECTED_DEVICE_IDS=EXAMPLE-PROTECTED-ID
```

Replace the example values with the installation's own values. Protection is
checked before HTTP, DNS-pinned redirects, CLI, SSH, Telnet, Setup, Presets,
Multiroom and background transports. SSDP replies carrying a protected
identity are discarded before unicast follow-up.

## Server address selection

The UI offers physical LAN addresses only. It excludes loopback, unspecified,
link-local and common container bridge interfaces. If more than one usable
address exists, select the network shared with the radios.

The installer may persist detected host addresses when it first creates
`.env`; it does not alter the operating system's network configuration.

## CLI 17000 and SSH

Normal setup uses the regular SoundTouch HTTP API and a profile-bound subset of
CLI port `17000` where required. Port `17000` is not exposed as a general
shell. The normal wizard contains no SSH credential fields and does not assume
an undocumented passwordless root login.

SSH remains a separate expert path with its own profile, backup, read-back and
rollback gates. It is not a hidden prerequisite for normal setup.

## Backups and rollback

Before critical writes, setup records the reachable routing/provider state,
device identity, exact firmware and checksums. A rollback preview shows which
values can be restored and verifies the result after writing.

The UI uses precise terms such as **routing rollback** when account,
environment or other firmware state cannot be proven fully restorable. It does
not call a limited restore a full rollback.

## What setup never performs

Normal setup never performs:

- factory reset;
- firmware flashing or patching;
- MFi or AirPlay authentication bypass;
- NAND or bootloader modification;
- arbitrary shell commands;
- automatic reboot loops.

## Troubleshooting

### No radio is found

- Confirm that the user has already connected the radio to the same LAN.
- Wait until the radio has completed its own network startup.
- Press the explicit discovery button again.
- Check whether SSDP multicast is allowed inside the trusted LAN.
- Do not disable device protection to make a radio appear.

### A radio is visible but cannot be selected

The card states the missing evidence. Common causes are an incomplete `/info`
read-back, an unreachable radio, or an exact firmware/product/variant/platform
combination that has no approved critical-write profile.

### Setup fails for one radio

Open that radio's error details. Other selected radios continue independently.
Use rollback only when a backup checkpoint and a documented restore scope are
available.

### Playback verification is blocked

Run the visible audio-safety check or leave the optional playback test off.
Audio validation is intentionally fail-closed and limited to volume `1`.

## Security profile

BASSWIESN supports operation on a trusted private LAN. Do not expose its ports
directly to the public Internet. Use a separately managed authenticated reverse
proxy if remote access is required.

For release-specific behavior and known limitations, see
`docs/releases/3.0.0/RELEASE_NOTES_3.0.0.md`. Advanced diagnostics and LAB
features are documented directly in the corresponding Web UI sections.
