# Automated security checks and limits

## Reviewed static-analysis boundaries for 5.0.0

The final CodeQL review fixed HTML-report escaping, DTD/entity parsing,
unrestricted local-update preview paths and raw exception output in the
reported diagnostic paths. These fixes have offline regression tests.
Discovery summaries and debug failures also escape untrusted line breaks, so
a CIDR, device address or failure message cannot forge additional log lines.

Some static data-flow warnings require a narrower reading than their rule
title. The following are reviewed boundaries, not blanket rule exclusions:

| Rule and location | Review and validation |
|---|---|
| `py/path-injection`, `backup_restore._validate_backup_path` | The input is resolved and must remain within `data/backups/system` before existence checks or reads. Backup integrity tests reject escaping paths and symlinks. |
| `py/path-injection`, `local_updates.inspect_local_release_archive` / `_sha256` | Resolved archives must remain below `data/uploads/updates` and end in `.tar.gz`. Regression tests prove traversal, outside paths and escaping symlinks cannot reach hashing. |
| `py/path-injection`, `local_media.validate_media_root` | An administrator deliberately selects a media-library root. This call checks the directory; track resolution separately enforces that selected root and rejects traversal/symlink escapes. Legacy LAN mode still requires trusted clients; authentication is not inferred from a path check. |
| `py/stack-trace-exposure`, local update preview/prepare routes | The remaining flow is `UnsafeArchive` from `validate_archive_members`, which contains fixed validation messages, not traceback frames, filenames, tokens or arbitrary exception text. Unexpected tar-parser messages are not returned. |
| `py/overly-permissive-file`, LAB `pilot_dhcp.save_json` | Lease status is deliberately root-written and mode `0644` for an unprivileged local observer. It contains address, prefix, lease timing and a non-secret rules digest, not credentials or pairing keys. Root-owned, non-writable ancestor checks and exclusive/no-follow creation prevent other users from modifying it. Private configuration continues to require separate private-file checks. This is a local visibility trade-off, not permission to write secrets to this status file. |

These reviews apply only to the named implementations. They do not authorize
new callers to skip containment or allow general exception serialization.
The workflow continues to run every rule; future findings must be reviewed.

The engineering workflows scan Python dependencies, source with CodeQL, Git
history with Gitleaks, and the runtime image with Trivy. Action revisions are
pinned to upstream commit SHAs. The Gitleaks image is digest-pinned. Dependency
and container vulnerabilities are not broadly ignored; a failed scanner blocks
its job. Tool/database outages are failures, not evidence of a clean application.

Container JSON findings and the CycloneDX SBOM are retained even if the scanner
rejects the image. Evidence upload does not turn the failing job green. The scan
includes unfixed HIGH/CRITICAL findings; no general ignore list or
`continue-on-error` weakens that gate.

## Runtime image and source-built SSH

The October 12 rescan of the previous Debian runtime reported **44 HIGH,
zero CRITICAL** package occurrences across eight distinct CVEs on both
architectures. That image and its raw failure reports remain historical evidence.
It is not approved by an exception or silently relabelled clean.

The replacement runtime uses the digest-pinned stable Alpine 3.24 Python base,
with the same hash-locked Python dependencies and independently verified SSH
client. Unneeded Debian administration packages are absent. This is a runtime
change, not a scanner exception: both architecture-specific images must pass the
unchanged HIGH/CRITICAL scan and actual runtime/authentication/migration probes.
The exact image digest, scan database and raw JSON report are the authority for
each build, not a permanent vulnerability-free claim in this document.

The affected distribution SSH client was replaced with upstream **OpenSSH
10.6p1**, built in a separate stage from an exact source hash and detached
signature. The pinned release-key fingerprint is checked, and only client
tools, their licence and a binary-hash manifest enter the runtime. The server,
compiler and verification keyring do not enter it. See [the dependency
procedure](DEPENDENCIES.md) and [upstream release notes](https://www.openssh.org/releasenotes.html#10.6p1).
Offline tests check actual installed hashes and acceptance of the existing
firmware-compatible SSH options. They do not establish connectivity to a radio.

A package-manager scanner cannot inventory a source-built binary automatically.
`tools/check_ssh_provenance.py` therefore verifies all installed client binaries
and contributes a visibly separate source component to the CycloneDX SBOM.
The raw package SBOM and vulnerability report remain intact. This inventory
requires separate upstream advisory review; lack of a package-manager finding is not a
clean bill of health.

The previous image's findings concern ncurses, systemd source-package matches, libacl,
util-linux and Perl. A source-package match is not proof that every installed
binary contains the vulnerable function. Conversely, non-root execution,
read-only filesystems and dropped capabilities are mitigations, not patches.
The [Debian tracker](https://security-tracker.debian.org/tracker/) and individual
upstream advisories must be reviewed against the exact runtime. No unstable
distribution packages are mixed into this stable image to silence a scanner.

The retained Debian-image compatibility probe requires its PCRE2 security update
([DSA-6530-1](https://security-tracker.debian.org/tracker/DSA-6530-1)). Alpine does
not need or install that library; the probe verifies its absence and refuses an
unreviewed distribution family. Installing unrelated packages alone need not
update an existing base library.

Final evidence must name the actual architecture/image and advisory database.
ARM64 software execution under local emulation is distinct from physical Pi
acceptance. QEMU's Python imports can exceed the normal eight-second health
deadline. `BASSWIESN_TEST_EMULATED=1` permits a bounded 30-second test probe only
for ARM64 images on an AMD64 Docker host. Native CI and the production image
keep the original deadline; an emulated pass does not certify native timing.
A private test artifact is not a public release or production
approval. Source archives are deterministic; OS package closure and compiler
inputs are not a claim of bit-identical future OCI builds.

## Reviewed scanner exceptions

Four original Git-history detections were all from two constant anonymous
provider descriptors in `basswiesn/app/services/provider_registry.py`. Decoding
them yields a JSON object with a single `serial` field and the fixed label
`local-internet-radio` or `tunein`. These are not device serials, passwords,
bearer credentials, MFi data or an authorization boundary.

The exception applies only to the `generic-api-key` rule, the exact provider
registry path AND those two complete encoded strings. No commits, directories,
test suites or generic token patterns are excluded. A different value on the
same line must still be detected. Tests enforce this narrow shape.

Redaction tests construct non-key canary envelopes at runtime. The scanner's
private-key rule remains enabled for all committed test fixtures; no test-file
or key-block exception was added. Ignored interpreter bytecode is not source
evidence and is not included in release archives.

Run the same history scanner locally:

```sh
bash tools/scan_secrets.sh
```

This does not inspect ignored private files or claim that uncommitted changes
are part of Git history. CI scans the committed tree and history on every run.
Release packaging additionally uses its existing public allowlist and private
data checks. Redaction is enabled; do not publish unredacted scanner reports.

## Hardware isolation

Software pytest fixtures permit loopback and mocked transports only. In CI,
`tools/ci_offline.sh` additionally creates a new Linux network namespace with
only loopback, then runs tests as the original unprivileged runner user. Browser
children and subprocesses cannot reach radios through another transport. It is
not a privileged runtime container and grants the application no host access.

The container startup smoke test has no networking, no published ports and no
host volumes. Its temporary data filesystem is discarded with that test
container. Hardware acceptance remains **NOT RUN**, independent of software
test counts. No CI workflow publishes a release or modifies a radio.

All four software test launchers explicitly exclude the hardware marker, even
when a test also carries a unit/integration/browser marker. Direct pytest
collection rejects selected hardware tests before isolation fixtures can be
bypassed unless the hardware-only selection, explicit confirmation, full target
profile and protected-device policy are present. CI rejects them regardless.
This selection gate is not a substitute for live target verification immediately
before every real hardware action.

## Remaining boundaries

These scanners do not provide general admin authentication. The
[security model](SECURITY_MODEL.md) records trust boundaries, controls and gaps.
The listener/write review is in [the engineering baseline](ENGINEERING_BASELINE.md).
Coverage below the critical-component goal, other legacy write paths,
human/device authentication separation and full release attestation remain
tracked work. A CI YAML file is not proof that GitHub has executed it; workflow
execution requires a later explicitly authorized push.
