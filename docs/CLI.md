# Offline administration: basswiesnctl

`basswiesnctl` provides local, bounded inspection and explicit private backup
creation. It never starts the application, imports its settings/ORM lifecycle,
discovers devices, resolves hosts, opens network sockets, calls Docker, runs
subprocesses, migrates, repairs, restores, or writes to a radio. There is no
`--url`, token, playback, reboot, setup, update or other remote-operation mode.
Local filesystem access is the authority; this is not a bypass around browser
authentication or scoped API-token permissions.

Use Python 3.11+ on Linux from the installation's hash-locked environment, from the
source installation directory:

```sh
python tools/basswiesnctl --help
python -m basswiesn.cli version --json
python tools/basswiesnctl doctor --data-dir /path/to/offline-data --config /path/to/resolved.env
python tools/basswiesnctl status --database /path/to/checkpointed-snapshot.db --json
python tools/basswiesnctl devices --database /path/to/checkpointed-snapshot.db --limit 50
python tools/basswiesnctl health --database /path/to/checkpointed-snapshot.db --json
python tools/basswiesnctl providers --json
```

The executable source launcher is `tools/basswiesnctl`; the Python module is
equivalent. A container distribution may expose the launcher on `PATH`.
`--data-dir`, `--database`, `--config` and `--json` work before or after the
subcommand. Defaults are `./data` and `DATA_DIR/basswiesn.db`, relative to the
current working directory, not the launcher directory. Paths are explicit
offline inputs, not changes to application configuration.

## Command contract

| Command | Evidence / allowed local writes |
|---|---|
| `version` | Canonical source version and output schema version; no database needed |
| `status` | Application SQLite integrity/core structure and cached device counts; no writes |
| `doctor` | Offline preflight checks, constant diagnostic codes and remediation; no writes |
| `devices` | Up to 500 cached records, hashed references by default; no discovery or probes |
| `health` | Stored health-run metadata and provider-state counts, not a new health check |
| `providers` | Static Bose-compatible registry contracts, not fresh optional-adapter health |
| `backup` | New private verified system archive, only with explicit offline confirmation |
| `verify-backup` | Bounded temporary verification copy only; never prepares or performs restore |
| `diagnostics` | Allowlisted offline report to stdout; optional new private JSON file |

Exit codes are `0` PASS, `1` WARN and `2` FAIL/usage error; an interrupt returns
`130`. Warnings are not failures, but automated preflight should review them
instead of ignoring nonzero exit codes. `doctor` deliberately warns that live
runtime readiness is **NOT_PROBED**, even when every local structural check
passes. Legacy unauthenticated mode is another warning. A PASS status query
means its snapshot is readable, not that any service or device is online.

`--json` emits one JSON document to stdout with this stable envelope:

```json
{
  "schema_version": 1,
  "version": "3.0.2",
  "command": "status",
  "offline": true,
  "status": "PASS",
  "checks": [
    {"code": "status.snapshot", "status": "PASS", "message": "...", "remediation": ""}
  ],
  "data": {"live_runtime": "NOT_PROBED", "evidence": "offline_snapshot_only"}
}
```

Consumers should branch on `schema_version`, `command`, `status` and check
`code`, not message text. Ordinary operational failures use this envelope;
argument syntax errors use argparse's stderr/help and exit 2. Raw filesystem,
SQLite and configuration exceptions are not printed. Source version is read
from `basswiesn.__version__`, never a preserved environment override.

## Offline database boundary

Read a stopped installation or an isolated, checkpointed application snapshot.
The CLI uses SQLite `mode=ro&immutable=1` to avoid creating or updating the
database, WAL, SHM, journals, migration records or locks. Nonempty WAL/rollback
journals are refused, not deleted or checkpointed. Symlinked paths (including
ancestors), hardlinked files, special files and databases above 128 MiB are
refused. SQLite work has a bounded five-second progress budget; backup copying
has its own five-second budget. An observed database identity/size/mtime/ctime
change discards the result.

Immutable reads are **not a live-database consistency protocol**. The metadata
checks cannot prove that a writer is stopped or prevent a trusted local process
from racing them. Do not use a successful read to certify a running database.
Use the application's existing live-backup workflow for a running installation,
or stop writers through a separately authorized operating procedure. The CLI
never stops a service. A read-only query can update filesystem access times;
“no writes” here means no application/source-file or SQLite-sidecar mutation.

Schema inspection projects all declared table/column names from the installed
source without executing model imports. A parity test compares this projection
with SQLAlchemy metadata. It detects historical/missing tables and columns;
it does not prove column types, indexes, constraints, migration history, semantic
compatibility or a future successful restore. Those remain installation/restore
acceptance responsibilities. Missing or unsupported source metadata fails
closed, so use a complete matching source distribution.

## Doctor checks

Doctor inspects Python compatibility, SQLite integrity and declared structural
schema, data/database permissions, free disk against a conservative backup
reserve, explicit secret/configuration path permissions, administrator
bootstrap/schema metadata, auth/TLS/origin configuration, protected-device
policy and configured listener numbers. It reads only known runtime timestamps
and a bounded restart count from the snapshot. It performs no port bind,
reachability, certificate handshake, service-manager or executable probe.

Secret contents, administrator/password hashes, session/token rows, environment
values and free-text errors are not collected. Secret inspection covers the
known auth store and explicitly configured secret files, not an exhaustive
recursive secret scanner. Paths are not echoed in reports. Run under the
appropriate service account: auth and output ownership checks use the current
local UID, with exact 0700 directories and 0600 secret files. Doctor reports
problems; it never chmods/chowns, bootstraps or resets credentials.

There is no implicit `.env` load. `--config FILE` parses a bounded UTF-8 literal
dotenv file, with environment values taking precedence for the relevant
allowlisted keys. No shell sourcing, interpolation, command substitution,
multiline values or escape processing is performed. Docker Compose profile
overrides and container-to-host secret path remappings cannot be inferred:
provide resolved configuration and the correct local paths/account explicitly.
HTTPS port metadata is configurable; web/cloud/debug listener defaults are
1328/1516/1860. Port availability and actual deployed exposure remain unknown.

Protected-device evaluation includes literal immutable build constants and the
persisted snapshot policy. It mirrors runtime alias precedence:
`PROTECTED_DEVICE_IPS`/`PROTECTED_DEVICE_IDS` override their nonempty
`BASSWIESN_` aliases; doctor warns when both differ so shadowed entries are not
silently mistaken for protection. Persisted entries are unioned, mapped IPv6
addresses are normalized, and malformed entries fail closed. Protection counts
are **not permission grants** or evidence that the currently running service
loaded this configuration. No operation is authorized from these counts.

## Private backups and verification

Choose an existing private output directory owned by the current user (0700),
and a new filename. Once the source is a stopped/checkpointed snapshot:

```sh
python tools/basswiesnctl backup \
  --database /path/to/checkpointed-snapshot.db \
  --output /path/to/private-backups/new-snapshot.zip \
  --offline-confirmed --json
python tools/basswiesnctl verify-backup /path/to/private-backups/new-snapshot.zip --json
```

`--offline-confirmed` is an operator assertion, not an automatic service-stop
command. Creation requires the complete current declared structural schema
before labeling the manifest with the source's supported baseline. It uses
SQLite's backup API, verifies the copied database and final archive, writes mode
0600, fsyncs and publishes atomically with exclusive no-overwrite semantics.
All output directory components are opened without following symlinks and
retained as directory descriptors. JSON/ZIP writing and archive verification
use the retained staging-file descriptor, never a reopened staging pathname.
Publication links that exact inode through Linux `/proc/self/fd` into the pinned
private directory. A missing/unsupported procfs link fails closed; there is no
path-based fallback. Parent traversal (`..`) in artifact paths is refused.
Directory bindings, ownership and mode are rechecked before/after publication;
an ancestor substitution cannot redirect writes or cleanup into its replacement.
Cleanup removes only inode-matching entries under the retained directory.
Failed prepublication work removes only its own temporary files. A crash after
publication may leave a completed archive even if no success was printed; do
not retry by overwriting it. In a storage/fsync failure, inspect the selected
filename before retrying with a new one.

The archive uses the existing `basswiesn-backup-v1` manifest and the shared
manifest/archive security validators. It contains `database.sqlite`,
`settings.json` and `manifest.json`; existing system-backup preview accepts it.
`settings.json` is deliberately `{}`: application settings remain in SQLite,
not duplicated in a falsely “redacted” textual sidecar. The SQLite snapshot can
contain **sensitive settings, URLs and device configuration**. This is not a
support export, is not encrypted, and must remain private. The separate
administrator authentication database, host environment, secret directory and
media assets are not included: this is not a complete installation backup.

Verification refuses unknown/missing/duplicate/traversing/symlink/special-file
members, unsupported manifests, excessive expansion, hash mismatches, malformed
settings and unusable SQLite. The compressed and expanded size limits are
128 MiB; manifest/settings metadata is limited to 1 MiB each. It does not
extract into the installation. Current source-version equality and declared
schema compatibility are reported separately from integrity: a valid archive
from another version or incomplete schema yields WARN `NOT_CONFIRMED`, not a
claim of restore compatibility. Even `STRUCTURAL_MATCH` is only structural
evidence, not migration/index/constraint or application-level validation.

SHA-256 detects corruption, **not authenticity**. No archive verification
authorizes restore, account changes, startup, migrations or device operations.
There is intentionally no restore command; see the existing backup/restore and
installation procedures for separately reviewed, authorized recovery.

## Privacy and diagnostics

```sh
python tools/basswiesnctl diagnostics \
  --data-dir /path/to/offline-data \
  --output /path/to/private-reports/new-report.json --json
```

This combines doctor with bounded cached-device/health summaries and static
provider metadata. A report can be exported even when checks fail, so the
operator can review constant diagnostic codes. It does not include logs,
settings values, raw exceptions, arbitrary state JSON, device names, addresses,
free-text provider errors or any authentication database. Hashed device
references are pseudonyms, not cryptographic anonymity: known identifiers can
be correlated. Share reports deliberately. Export is opt-in, private and
no-overwrite; stdout is the default.

`devices --include-identifiers` is a separate explicit local-inspection opt-in
for validated 12-hex device IDs and parsed IP addresses. Names and arbitrary
identifiers remain excluded, and diagnostic exports never enable this flag.
`providers` shows the static compatibility registry only: it does not instantiate
optional adapters or claim fresh Music Assistant/provider authentication or
health. AirPlay and UPnP/DLNA remain experimental/manual-acceptance boundaries.

Software tests cover offline reads, no startup imports/transports, real WAL
refusal, schema projection parity, auth metadata, privacy, atomic no-overwrite
publication, archive adversaries and compatibility with the system-backup
preview. Live runtime, filesystem-crash durability, real-device/Pi/container
deployment, full restore and hardware acceptance are not certified by these
CLI tests.
