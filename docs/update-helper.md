# Restricted in-app updates

Developer packaging also refuses to overwrite existing release output. Use
`bash tools/package_release.sh --output-dir /absolute/new/output-directory`
for a separate candidate build. The directory must not exist; all software tests
and archive checks still run. This does not change the source version or publish
anything, and a candidate package is not final release acceptance.

Settings separates checking for a release from installing it. Checking is
read-only. Installation requires an explicitly enrolled host service, HTTPS and
the separate administrator code; ordinary access to the LAN interface is not
enough. No update runs automatically. In 3.0.0, installation is an experimental
LAB-only workflow: isolated install/cutover/rollback checks passed, but production
host acceptance remains open. Switch to LAB before submitting an installation.
The interface mode is not a security boundary; host authorization is still required.

## Initial setup and everyday use

Run the trusted release's `./install.sh --enable-updater`, or accept the updater
prompt during interactive installation. The installer explains the root service's
permissions and asks for local administrator privilege. `--no-updater` keeps a
normal installation without the privileged service. Unattended installation does
not silently grant root access. Python 3.11+, systemd and local rootful Docker are
required; unsupported/custom layouts fail closed.
Use a persistent installation directory, not `/tmp`, `/var/tmp` or `/run`:
the daemon intentionally has a private temporary-filesystem view. Setup creates
the host socket group for numeric GID 10001 only if that group does not exist.
The invoking local administrator's UID is bound into enrollment; under sudo this
is the original administrator, not just the root owner of the final directory.
Thus a root-owned installation inside that administrator's home is supported.
Every ancestor must still pass the no-symlink/ownership/permission guard. Unrelated
owners or writable shared ancestors are not automatically trusted or repaired.
Run as the installation administrator, using sudo when needed; a direct root
login without an original administrator identity cannot infer trust in another
user's home. Path checks run before privileged bootstrap or data changes.

Setup retains the previous `.env` privately, enables HTTPS on port 1329, mounts
only `/run/basswiesn-update` read-only, enrolls the exact healthy container and
seals the helper code. It enables the socket and checks it from the application's
actual UID and mount. Partial/existing enrollment is not automatically overwritten.

The private administrator code can be shown deliberately **only on the local
terminal**, not standard output, with:

```sh
sudo /usr/bin/python3 tools/install_update_helper.py show-code --approve-root-helper
```

Open the HTTPS interface and compare the server certificate fingerprint with the
installation output before entering this code. A self-signed certificate requires
explicit browser trust; do not dismiss a changed certificate without checking.
Reverse-proxy transport is not accepted by this initial privilege boundary.
In Settings, check the official release, enter the administrator code and approve
the interruption. A checksum is an integrity check, not a publisher signature.

The code is never saved by the browser or application. Only a random request UUID
is retained in session storage so a reload can continue reading status. A lost
HTTP response is **not** retried as a new installation. The root worker
acknowledges durable intent before proceeding independently of the browser.
An uncertain submission remains uncertain until its journal status is observed.

## Release and data cutover

The executor builds the verified candidate before stopping the enrolled service.
It then captures the stopped data/configuration, validates an isolated copy, and
prepares a new filesystem generation beside the installation. Its private operation
directory has mode 0700 and is not accessible to the application. Both the current
and candidate `.dockerignore` must end with the effective exclusion
`/.basswiesn-update-*`; a Dockerfile-specific override is refused. This prevents a
later manual build from uploading retained private data. Older installations need
this exclusion established during explicit administrator enrollment, not silently
patched halfway through an update.

Linux `renameat2` exchanges each top-level code/configuration/data entry atomically.
The root-private immutable plan records original and candidate inode identities and
content/metadata hashes before the first exchange. A partial exchange can therefore
be reversed without overwriting an unknown entry. The original generation remains
retained, including its database and WAL. Candidate database migrations affect only
the replacement generation. Rollback verifies the preserved original before
putting it back; modified originals, unexpected mounts/inodes, links, unsupported
metadata or an unproven stopped-writer condition stop the operation.

Installer-created setgid directories are preserved with their original owner,
group, mode and timestamp. Executable privilege bits, sticky data directories and
world-writable entries remain forbidden. ACLs and extended attributes remain
unsupported rather than being silently discarded.

The host adapter starts the candidate with the enrolled application environment,
fixed non-root/read-only runtime, and **unchanged port bindings**, including
loopback-only installations. Health checks require the exact version, all three
local services, a stable process identity and the supported runtime layout. The
enrollment then changes to the new immutable image/container and configuration
hashes. The stopped original container is retired only after this verification;
its image and full filesystem generation remain available. If a late failure
occurs after retirement, rollback recreates the approved original runtime against
the restored old data and updates enrollment to its new container identity.

The host journal still owns single-flight execution and interruption handling.
An ambiguous process loss requires operator inspection; service restart does not
blindly replay a partially completed update. Generated service units grant writes
only to the enrolled installation and private helper state, never a whole home or
workspace. Setgid directory restoration is permitted; file privilege bits remain
rejected by the metadata gate. Full release-archive/fresh-install acceptance is
separate from synthetic development fixtures and must precede publication.

## Privilege boundary

The Web container keeps its existing non-root user, dropped capabilities and
read-only filesystem. It must not receive the Docker socket, general-purpose
sudo, arbitrary host paths or a shell execution endpoint.

A host service accepts one bounded request over a root-managed Unix
socket. The kernel's `SO_PEERCRED` supplies the caller UID; a JSON field cannot
choose that identity. The allowlisted **host** UID needs explicit onboarding,
including any container user-namespace mapping. A container UID or an environment
flag does not establish host authorization.

Installation also requires a generated 256-bit administrator capability. The
service holds its SHA-256 digest, compares it in constant time and never places
the capability in its journal, response, object representation or error text.
Initial provisioning retains the generated capability only in a root-private
credential file; the daemon authorization object contains its digest. Intentional
local-terminal delivery is separate from ordinary program output. Automated
credential rotation is not implemented; keep the code private and disable the
socket if compromise is suspected. The HTTP route requires real HTTPS, matching
Origin/Host/port, a custom request header, JSON and same-origin fetch context.
Forwarded transport headers are refused and the bundled server disables proxy
header processing. Validation errors do not echo the submitted body.

## Local installation preflight

`tools/check_update_host.py` is a development/admin diagnostic, not an installer.
It reads an existing root-owned enrollment policy. It does not generate that
policy, copy application files into a privileged service, install a daemon or
change any permissions. The separate preparation tool below creates a policy
only after explicit administrator approval and successful local observation.

The policy binds one installation UUID, absolute installation root and owner,
Compose project, full container ID, immutable image ID, version, approved
configuration fingerprints and the `.env` fingerprint. The private JSON schema
is exact and bounded to 16 KiB. Its parent must be private and the file root-owned,
single-link, mode 0600 and free of symlink traversal. Policy hashes describe an
administrator-approved baseline; they do **not** authenticate a publisher.

The read-only filesystem pass checks approved Compose/Dockerfile/build-context
configuration and the package version file without executing any of them. `.env`
is hashed as bytes, never sourced or evaluated. Configuration reads are limited
to 1 MiB and checked for replacement while reading. Unexpected Compose overrides,
unsafe ownership/permissions, symlinks and hard links cause refusal rather than
automatic repair. Fixed data and setup-secret mount directories are checked
without traversing private files, reading databases or dumping credentials.

An explicit `--inspect-local-docker` request is required to inspect the runtime.
The default filesystem check does not invoke Docker. Runtime collection requires
root, a trusted `/usr/bin/docker`, the root-owned `/run/docker.sock`, and an empty
root-private client configuration directory provisioned by enrollment preparation.
Each CLI invocation gets its own temporary child directory, removed after the
child process exits. Docker/buildx may create token seeds or builder state there;
these are never reused as credentials or allowed to contaminate the parent.
There is no remote Docker URL, custom executable or arbitrary CLI option input.
Inherited Docker contexts, proxy variables and user client configuration are not
used. The preflight uses `info` and `inspect` of the exact enrolled container/image.
The separate stopped-writer guard also lists active container IDs and inspects
their mount metadata, refusing writable mounts that overlap the installation.
No `exec`, logs, discovery, build, pull, start or stop is allowed.
The collection has a 15-second budget and 2 MiB limit per command; failed child
processes are reaped and raw stderr is discarded.

The initial supported runtime contract is deliberately narrow:

| Requirement | Why a mismatch blocks the check |
| --- | --- |
| Local rootful Linux Docker, no Swarm/user-namespace remap | Host identity/ownership must not be guessed |
| Enrolled Compose project/service/config path | A similarly named container is not the same installation |
| Exact image ID and matching version/source labels | Tags and names alone are mutable; labels still are not publisher authenticity |
| Healthy running container, not paused/restarting/OOM-marked | A broken baseline cannot be mistaken for a healthy update starting point |
| Actual process UID/GID 10001, identity UID/GID maps | A `User` string alone does not prove host peer identity |
| Fixed application command, dropped capabilities, read-only root and no-new-privileges | Do not normalize unknown elevated runtime configurations silently |
| Expected data/setup-secret binds, optionally the exact read-only update-control directory, and normal ports | Do not grant arbitrary host filesystem access |
| Same process start identity and restart count across observations | A restart or PID reuse invalidates the collected baseline |

Process checks read only `/proc` identity/status/start-time and UID/GID maps,
never environment or process memory. Runtime observations expire after 15 seconds.
Raw Docker inspection can include environment values and health-log text; these
remain in memory and are never part of a report, journal or object representation.
Public results contain only fixed check names, status and reason codes—not local
paths, container IDs, configuration hashes, environment values or raw exceptions.

Even when `layout_checks_passed=true`, the result explicitly keeps
`installation_available=false`, `onboarding_complete=false` and
`backup_restore_verified=false`. Unsupported custom/rootless layouts need an
explicitly designed alternative, not a hidden fallback. A separate space-budget
check exists for a trusted private staging filesystem; it requires a caller-
calculated byte budget and does not estimate backup size or verify Docker's image
storage. Recursive backup safety, effective ACL/writability, actual running app
version, restore integrity and candidate isolation remain executor gates.

## Explicit administrator preparation

`tools/prepare_update_host.py` is a **development/admin preparation tool**, not
an installation feature exposed by Settings. Use it only from code whose source
and integrity the administrator has independently reviewed. Do not execute an
untrusted or mutable application checkout as root. Preparation does not copy
code into a privileged service, install a systemd unit, create a listening socket,
grant sudo access or mount Docker into the Web container. The full installer
composes this preparation with the separate sealed-service provisioning stage.

Three operations are available:

1. Without an action flag, inspect fixed configuration files and print a
   filesystem-only preview. **No Docker call or write occurs.**
2. With `--apply`, `--approve-preview` and
   `--acknowledge-private-host-state`, recompute and compare the preview, reserve
   private host state, inspect the explicitly identified local container, and
   prepare its policy and empty journal. Both approval inputs are required.
3. With `--verify-prepared`, verify an unused preparation from disk without Docker
   access. This is not an update-readiness or application-health check.

The administrator supplies six identity parameters:

| Argument | Required value |
| --- | --- |
| `--installation-root` | Exact existing installation directory |
| `--installation-owner-uid` | Trusted installation owner, not application UID 10001 |
| `--compose-project` | Exact approved project |
| `--container-id` | Full container ID, not a name or prefix |
| `--image-id` | Full `sha256:` image identity, not a tag |
| `--version` | Current stable version |

The CLI accepts neither an alternative Docker endpoint nor an alternative host
state directory. It does not enumerate containers to guess the installation.
The production destination is fixed at `/var/lib/basswiesn-update`. Test-only
library parameters allow isolated temporary directories owned by the test user;
they are not CLI or Web options.

The approval digest binds the installation identity, configuration/environment
fingerprints, destination, administrator owner and destination-parent identity.
It is **not a publisher signature**. Public output contains no environment
fingerprint or values, installation paths, container IDs, raw Docker inspection
or free-form exceptions. Configuration is checked again after runtime inspection
and before completion.

Existing directories are never reused, even if empty. Files are exclusively
created, never replaced. Insecure permissions are refused, not repaired: an older
installation with a readable `.env` needs a separate, deliberate administrator
permissions review. Preparation does not chmod it, rewrite Compose, stop the
container or migrate application data.

The new root-private state has this layout:

```text
/var/lib/basswiesn-update/       root-owned; mode 0700
  reservation.json             mode 0600; one-shot preparation marker
  enrollment.json              mode 0600; private approved policy
  peer.json                    mode 0600; observed host UID/GID contract
  prepared.json                mode 0600; completion receipt and hashes
  docker-client/               mode 0700; empty Docker client configuration
  journal/                     mode 0700; initialized, no jobs
    operation.lock             mode 0600
    state.json                 mode 0600
  staging/                     mode 0700; empty
  backups/                     mode 0700; empty, NOT an application backup
  candidates/                  mode 0700; candidate build/validation receipts
  restores/                    mode 0700; independently verified data copies
```

Files and directories are synchronized to disk. The completion receipt is written
last; prepared files are then read back and verified. Before acceptance, the
read-only runtime check must confirm the exact image/container, healthy baseline,
Compose/mount contract, host process identity and fresh observation. No capability
is generated or delivered: host UID evidence alone must not grant privileged
installation to the ordinary LAN Web UI.

An interruption or failed inspection can leave a reserved directory without a
valid completion receipt. This is an **incomplete preparation**, not an updater.
Repeating returns `TARGET_EXISTS`; it does not erase evidence, reset a journal
or repair an earlier operation. Preserve the private directory for administrator
inspection before separately reviewed recovery. There is no recursive cleanup or
reset flag. Do not publish this state: it contains local paths and installation
identities, even though environment and Docker log values are not copied into it.

Success returns `ENROLLMENT_PREPARED` and `enrollment_prepared=true`, but still
`installation_available=false`, `onboarding_complete=false`,
`backup_restore_verified=false`, `service_installed=false` and
`capability_provisioned=false`. The receipt verifies an initial unused state.
Once the service writes jobs, this checker must not reset or certify its
journal as empty. Tests use synthetic runtime observations; preparation has not
been installed on a production Pi.

## Sealed service and administrator capability

`tools/prepare_update_service.py` separates an unprivileged build from explicit
administrator installation. It does not run `systemctl`, start Docker, install an
application update or modify Compose. Do not run it as root from untrusted source.
The operator must independently trust the reviewed source/release and approve
the complete bundle SHA256. An embedded manifest is not a publisher signature.

The reproducible ZIP application contains only an explicit set of stdlib-only
host modules, the archive verifier and inert package initializers. It imports no
Web application, database, `.env` or user site packages. Source is syntax-checked
without execution. Duplicate/unexpected ZIP entries, compressed or linked entries,
changed hashes, a changed entry point and invalid Python are refused. The bundle
is bounded to 2 MiB; no filesystem extraction is needed to run it.

The four CLI actions are:

| Action | Effect |
| --- | --- |
| `build --bundle NEW_FILE` | Unprivileged, exclusive output creation; prints size/hash, never replaces a file |
| `inspect --bundle FILE` | Read-only structure, syntax and integrity check |
| `install --bundle FILE --approve-sha256 HASH --acknowledge-root-service-files` | Root-only, requires a valid unused enrollment; exclusively creates private service/capability files and two systemd unit files |
| `verify-installed` | Recheck installed code, units, enrollment identity, peer policy, permissions and capability/digest consistency; no activation |

Production paths are fixed. Installation creates `service/` under the existing
`/var/lib/basswiesn-update`, retaining its original journal and policy. This
directory contains `helper.pyz`, `authorization.json`, `administrator.capability`,
a reservation and a completion receipt. Directories are 0700 and files 0600.
The capability is never returned on standard output, put in argv, copied into the Web
container, written to a journal, or included in error output. A partial deployment
is not retried or erased; an existing service or unit is never overwritten.

The unit files are `/etc/systemd/system/basswiesn-update.service` and
`basswiesn-update.socket`. The socket accepts only AF_UNIX connections at
`/run/basswiesn-update/control.sock`, mode 0660, root-owned with the enrolled application
group 10001. Kernel peer UID is still checked on every connection. Socket file
access or root ownership alone does not authorize an installation; the separate
administrator capability is required by the protocol.

The service executes the root-managed bundle with isolated/no-site/no-bytecode
Python, not a user's mutable checkout. It requires Python 3.11 or newer, checks
the exact inherited systemd socket descriptor/name/PID, and reconciles interrupted
journal entries without replaying a host action. At most eight connections are
handled concurrently; status can be served while an installation holds the
transaction lock. Slow input remains bounded by the existing framing deadline.
SIGTERM stops accepting and allows active work to finish. Forced termination
retains durable recovery intent; it is not a promise of automatic rollback.

The unit uses a read-only host filesystem view with only private helper state and
the exact enrolled installation writable, no new privileges, private temporary/device views, bounded processes
and memory, and disabled core dumps. It does not expose a TCP listener or grant
the Web container Docker access. Journal/systemd status can diagnose process exit;
raw helper stdout/stderr are suppressed to avoid accidental credential logging.
No parent/home-wide write scope is granted. Service-file deployment and syntax
verification are not actual systemd activation/upgrade acceptance.

The real isolated Python-3.12 check provisions only synthetic files inside a
disposable network-disabled container. It starts the sealed bundle using an
inherited Unix socket, checks denied and allowed OS peers, confirms status replies
and verifies graceful exit. It does not emulate a successful application update.
This remains distinct from an actual installed-systemd/Web acceptance and a
production Pi upgrade. Test results must identify which boundary was exercised.

## Official asset acquisition and private staging

The host-side `artifact_staging.stage_release` accepts an exact stable version
and a UUIDv4 job identity, not a download URL, archive path or shell command.
The Web boundary enforces secure administrator submission and online/offline
policy; the helper independently verifies authorization and installation policy.

The fixed GitHub release-by-tag API identifies the two required assets:
`basswiesn-docker-release-VERSION.tar.gz` and `SHA256SUMS`. Drafts, prereleases,
mismatched tags/repositories/asset URLs, duplicate assets, invalid sizes and
malformed digests fail closed. The downloader uses exact asset IDs, not a
mutable user-supplied URL. After transfer it fetches release metadata again and
compares the relevant release and asset identities. Edits to unrelated release
prose do not invalidate the artifact.

GitHub documents binary asset responses as HTTP 200 or a 302 redirect. The
implementation permits one asset redirect to the exact
`release-assets.githubusercontent.com` release-asset path; it does not follow
arbitrary redirects or redirects for the release metadata request. A future
change to GitHub's CDN contract must be reviewed explicitly.
[GitHub's asset API contract](https://docs.github.com/en/rest/releases/assets).

Each HTTPS operation runs in a spawned worker whose parent enforces the remaining
network deadline. This also bounds a stalled DNS resolver, slow TLS handshake or
trickling HTTP headers, not only body reads. The worker is terminated/reaped on
failure. It validates all resolved addresses, refuses private/special/translated
destinations, pins numeric connections, and keeps the original hostname for TLS
certificate checking and SNI. Both IPv4 and IPv6 candidates remain usable.
Proxy and TLS trust-store environment overrides are not inherited by the worker.
No token, cookie or authorization header is sent.

| Boundary | Limit or behavior |
| --- | --- |
| Release metadata | 128 KiB |
| External checksums | 4 KiB, exact versioned archive entry |
| Compressed archive | 128 MiB maximum and exact API-reported size |
| Expanded archive | 512 MiB maximum |
| One archive member | 32 MiB maximum |
| Archive headers | 8,192 maximum |
| Network deadline | Shared 120 seconds across metadata, transfers and metadata recheck |
| Worker-to-parent frames | At most 64 KiB of body per frame |
| Compression/partial responses | No HTTP content encoding, HTTP 206 or resume/append |
| Failed staging | Preserved privately; same job ID cannot replace it |

Signed CDN query values remain inside the worker. They are not command-line
arguments, log entries, receipts or public errors. HTTP errors are content-free
reason codes; raw upstream error bodies are not retained as diagnostics.
Source acquisition relies on the fixed official repository over verified HTTPS.
That is not a release signature, and it does not protect against compromise of
the publisher's GitHub account. API digests, when supplied, and the external
checksums must both match. An offline checksum alone never establishes origin.

A fresh job directory is reserved under the private staging root. Before writing,
the staging filesystem must have room for the configured compressed and expanded
budgets; this does not measure Docker image storage or future backup requirements.
Neither an existing job nor its partial files are overwritten. Directories are
owner-only 0700; new files are 0600, or 0700 for owner-executable source files.
The host does not adopt archive owners, permissive modes or timestamps.

The compressed file descriptor stays pinned while the full archive is verified.
The same bounded raw-header parser is reused for policy scanning and extraction:
there is no second permissive tar parser or `extractall`. Symlinks, hard links,
special files, traversal, ambiguous names, duplicate paths, unsafe extension
records and manifest/checksum/version mismatches are rejected. Runtime/private
overlays such as `.env`, populated `data/`, secret directories, databases,
logs and implicit Compose overrides are refused before output members are
created. An empty packaged `data/` directory is allowed; it is never copied
onto an active installation.

Source files are created exclusively beneath a new private directory. After
extraction every file is read back and compared, including its mode, size and
checksum; directories must contain exactly the expected entries. The archive is
hashed again to detect changes. A minimal receipt is written last and the whole
stage is reverified. Future execution must call `verify_staged_release` again;
the existence of `ready.json` by itself is not approval to run anything.
Disk work is size-bounded and synchronized but is not a hard real-time filesystem
guarantee; an unresponsive storage device remains a host recovery concern.

Success means `SOURCE_STAGED`, not installation, readiness, publisher signature
verification or a tested rollback. No installer script or candidate Python code
is run, no Compose/container state changes, and no application data is read or
overlaid. The live acquisition check used the already published 2.6.5 package:
170 files, matching the previously recorded archive hash. This was a local
download/extraction check, not a Pi deployment or a 3.0.0 release test.

## Candidate build and isolated startup

`CandidateRuntime` is an internal host component, not a Web action or a completed
installer. It consumes an existing verified source stage, a stopped-writer snapshot
and the enrolled installation identity. It cannot stop/start production, choose an
arbitrary image or mount the active data directory.

Before building, it reserves a new private job directory and rechecks every source
file. Docker builds only that staged source, with no inherited user Docker context,
proxy settings or credentials. Builds may acquire base images and dependencies;
they are not claimed to be offline or cryptographically reproducible. The returned
immutable image ID must have the expected version/source labels, application
command, working directory and non-root user. Source is verified again after the
build. Labels validate the runtime contract, not publisher authenticity.

The separate validation step materializes a verified copy of the snapshot. The
candidate starts with these fixed limits:

| Boundary | Validation contract |
| --- | --- |
| Network | `none`, no published ports; probes use only container loopback |
| Data | Separate snapshot copy, never the enrolled live data directory |
| Privileges | UID/GID 10001, read-only root filesystem, all capabilities dropped, no-new-privileges |
| Resources | 512 MiB RAM, one CPU, 128 processes, 64 MiB non-executable temporary filesystem |
| Background work | Internal validation mode suppresses application background and event jobs |
| Environment | Bounded private file, no shell evaluation; executable/library search-path overrides excluded |
| Health | Web readiness, cloud registry, diagnostics, exact version and stable process identity |
| Database | SQLite `quick_check`, expected application tables and schema digest after startup |
| Cleanup | Stop/remove only the exact job-labelled validation container; retain image, data and receipts privately |

The validation flag is not a security sandbox by itself. Docker network isolation,
unpublished ports and independent mounts remain necessary even when the application
reports validation mode. An expected table set and successful SQLite integrity check
also do not prove semantic correctness of every data migration.

Command time/output are bounded; raw stderr and data rows are not returned. Build
timeout is not proof that every Docker builder operation has stopped. The reserved
job must be inspected rather than silently retried. If `docker create` loses its
reply, cleanup queries only that job's fixed container name and requires exact
image/label identity before removal. Unverifiable cleanup fails closed.

The completion receipt is written only after readiness checks and successful
container cleanup. `CANDIDATE_VALIDATED` means the isolated candidate passed; it
does not mean an update is installed. The local acceptance uses synthetic data and
a cached application base image with the current source. It tests real Docker
startup without touching a Pi, radio, production installation or published release.
Production cutover, interruption handling, original-image/data rollback and
secure Web approval are separate components with their own acceptance tests.

## Request and response boundary

The connection carries a four-byte, unsigned, big-endian JSON byte length followed
by one JSON object. Requests are limited to 2,048 bytes and five seconds total,
including partial delivery. Each connection is closed after its response. A
disconnected browser must not cancel or repeat an already accepted operation.

| Action | Exact fields | Authorization |
| --- | --- | --- |
| `status` | `protocol=1`, `action` | Allowlisted kernel peer UID |
| `install` | `protocol=1`, `action`, `request_id`, `target_version`, `expected_current_version`, `approve=true`, `credential` | Peer UID plus administrator capability |
| `submit` | Same fields as `install`; acknowledges the durable request before execution finishes | Peer UID plus administrator capability |

Versions are exact stable `major.minor.patch` identities. Request IDs are
canonical lowercase UUIDv4 values. Duplicate JSON keys, additional fields,
arbitrary URLs, paths, commands, environment variables and Docker options are
rejected. The target must be newer than the expected current version. The
current version is checked again after preparation, immediately before stopping
the old service.

`ok=true` means the request was processed, **not** that installation succeeded.
The result's state is authoritative: `COMPLETE`, `RESTORED`, `FAILED` and
`MANUAL_ACTION_REQUIRED` have different meanings. Errors contain fixed codes,
never raw exceptions or subprocess output. A bare runner without an executor
returns `EXECUTOR_UNAVAILABLE`; the enrolled daemon explicitly attaches the
constrained production executor. A `submit` acknowledgement is not a success
result: the browser must read the durable final state.

## Durable transaction

The journal uses an explicitly provisioned private directory, owner-only file
permissions, descriptor-relative operations, no symlinks or hard links, atomic
replacement and file/directory `fsync`. Opening the journal does not initialize
it. Explicit initialization works only once in an empty directory; a missing or
corrupt existing journal is never replaced with empty history.

An in-process mutex and a nonblocking process lock serialize writers. Up to 128
job identities and 32 events per job are retained; reaching capacity fails closed
rather than evicting replay protection. The retention/maintenance procedure is
still an operator-facing prerequisite, not an automatic deletion policy.

| State | Meaning before advancing |
| --- | --- |
| `REQUESTED` | Authenticated request recorded durably |
| `VALIDATED` | Version, installation and rollback prerequisites checked |
| `STAGING` | Preparing the candidate; production still unchanged |
| `STAGED` | Candidate prepared; archive digest and request/version identity recorded |
| `STOP_REQUESTED` | Intent persisted **before** stopping the original service |
| `STOPPED` | Original service stopped |
| `BACKUP_STARTED` | Intent persisted before consistent snapshot creation |
| `BACKED_UP` | Matching backup receipt and manifest digest verified and recorded |
| `START_REQUESTED` | Intent persisted before candidate start or data migration |
| `STARTED` | Candidate start returned successfully; health not yet proved |
| `VERIFIED` | Target version, readiness and data checks passed |
| `COMPLETE` | Completed transaction durably recorded |

Preparation happens before downtime. A full data/configuration snapshot happens
**after stopping writers and before starting the candidate**. Copying a running
SQLite file together with a separately copied WAL is not treated as a consistent
backup. The production adapter must retain original image identity, configuration,
permissions and matching data, not just a database file.

Failure before cutover records `FAILED` without touching the active installation.
Failure during or after stopping records `ROLLING_BACK` before restoration. If
the candidate could not have started, the adapter can restart the original
against unchanged data. Once a candidate may have started, restoration requires
the matching verified backup and original image/configuration. A start failure
is conservatively treated as a possible partial start.

Only a verified original state permits `RESTORED`. Failed restoration or failed
post-restore verification requires `MANUAL_ACTION_REQUIRED`, which blocks new
updates. No success is inferred from a subprocess exit alone.

## Restart and I/O failure

On helper restart, incomplete pre-cutover jobs become `FAILED`; interrupted
cutover/rollback jobs become `MANUAL_ACTION_REQUIRED`. The core never replays a
stop, install or rollback automatically. Process-interruption recovery is an
administrator procedure; the normal executor's exception rollback is not a
claim of automatic recovery after power loss.

A repeated request ID with the same versions returns the original result without
running again. Reusing it for different versions is rejected. Journal I/O failure
blocks further writes in the current process, even if the filesystem later seems
healthy. If a durable transition cannot be recorded, no new host action begins.
Ambiguous cutover state requires recovery inspection, not a fabricated restore.

## Private application snapshots

`installation_snapshot.py` captures the complete enrolled `data/` mount, including
SQLite database, WAL and SHM files, settings, setup credentials and empty folders.
It also captures the fixed enrolled release configuration and `.env` as bytes,
without evaluating their contents. Its private manifest binds the installation,
request, original version and immutable image ID. It does not export an image or
prove that Docker still retains it: the executor must separately establish that.

Capture requires an explicit stopped-writer check before and after copying and
after complete verification. The local Docker guard checks the exact original
container is exited with PID zero and no active container has a writable mount
spanning the installation. Other host writers must be excluded by supported
onboarding; a Docker check cannot freeze arbitrary host processes. Three full
filesystem inventories, byte hashes and metadata checks detect changing input.
This is deliberately not advertised as an atomic snapshot of a running database.

Private backup objects are single-link regular files with mode 0600 below mode
0700 directories. A request reserves a fresh directory, never overwrites a prior
snapshot, and retains partial output on failure. Defaults bound capture to 20,000
entries, 4 GiB total, 2 GiB per file and a 300-second operation budget. Capacity is
checked before reservation. Unsupported links, FIFOs, devices, nested filesystems,
world-writable/special modes and extended attributes/ACLs cause refusal. They are
not silently omitted. The backup location cannot overlap the installation.

Restoration first rehashes every object and checks the manifest against the
receipt and enrolled configuration. It creates a **new private restore tree**,
preserving bytes, owners, groups, modes and modification times. It reads the whole
tree back before writing a ready marker. The current installation and backup are
never replaced or deleted by this operation. Access/change times and inode numbers
are not restored. No claim of exact filesystem identity includes those values.

Offline tests use real SQLite files, including committed WAL content and a
subsequent simulated schema change. The restored copy passes SQLite `quick_check`
and contains the original schema/data while the changed original remains intact.
An isolated Python 3.12 container also verifies ownership restoration for UID/GID
10001. These component results are complemented by actual local Docker
cutover/rollback tests, including failure after retirement of the old container.
Synthetic fixture results are not acceptance of the published release asset.

## Release and operational limits

- Test the final archive through clean installation, upgrade and forced rollback
  before deploying to production. A development fixture is not the final asset.
- Interrupted cutover fails closed to manual inspection; it is not silently
  replayed. Preserve the journal, image and retained filesystem generations.
- Root helper code is sealed during initial enrollment. Ordinary application
  updates do not replace this privileged code or automatically rotate credentials.
- The journal retains up to 128 jobs. Capacity/retention maintenance requires an
  administrator; automatic backup deletion is intentionally absent.
- Custom/rootless/ACL-bearing installations are not automatically normalized.
- A readiness failure is not evidence that a radio or its firmware needs reset.

No adapter may contact radios, trigger playback, change volume or reset a device
as part of application update verification.

## Offline development tests

```sh
.venv/bin/python -m pytest -q tests/test_update_helper_300.py
.venv/bin/python -m pytest -q tests/test_update_host_preflight_300.py
.venv/bin/python -m pytest -q tests/test_update_onboarding_300.py
.venv/bin/python -m pytest -q tests/test_update_service_deployment_300.py tests/test_update_installation_snapshot_300.py
.venv/bin/python -m pytest -q tests/test_update_official_transport_300.py tests/test_update_artifact_staging_300.py
.venv/bin/python -m pytest -q tests/test_release_artifact_300.py
```

The tests use temporary private files, synthetic installation state and local
`socketpair` descriptors. They cover invalid requests, authorization, duplicate
jobs, process/thread concurrency, corrupt/missing/unsafe journals, write/fsync
failures, interruption at every recorded state and rollback classification. No
radio, Pi, LAN listener, Docker container or production configuration is used.
Preflight tests use synthetic Docker observations; subprocess boundary tests run
local Python fixtures instead of Docker. They do not certify the live Pi layout.
Enrollment tests additionally cover mismatched approval, changed configuration,
concurrent/repeated setup, unsafe filesystem entries, interrupted writes, failed
synchronization, incomplete preparation, expired observations and tampered
completion receipts. No test provisions the actual host service.
The transport tests use mocked HTTPS/DNS and spawned, network-free fixture
workers. Staging tests use synthetic GitHub responses and tiny generated archives.
The separate public-asset acceptance check is not part of the offline test suite.
