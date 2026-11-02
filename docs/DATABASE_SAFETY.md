# Database startup and restore safety

The application keeps its existing additive SQLite migrations. It does not
replace the database or rewrite historical device/preset records. Table creation
and later column/index changes use separate transactions, so an interrupted
upgrade can leave **partial but resumable work**. The baseline marker is now
written only after the migrator's structural validation succeeds. An old marker
is historical evidence, not proof that a newly started migration completed.

## Before a changed startup migration

`init_db()` serializes startup with a private lock beside the configured database,
not a fixed lock relative to the current working directory. A second concurrent
initializer fails closed and must retry startup after the owner finishes.
This does not lock out an older running application: stop old writers before an
upgrade. The updater's separate cutover/rollback boundary still applies.

For `data/basswiesn.db`, startup records live under
`data/basswiesn.db.migration-state/` (0700). Files are 0600. A migration-code/model
fingerprint and actual SQLite schema fingerprint detect when a snapshot is needed.
A changed migration/schema or an earlier failed attempt triggers:

1. SQLite online backup, including committed WAL pages.
2. Integrity check of the copied database and a SHA-256 receipt.
3. Durable `PREPARED` journal before migration writes.
4. Existing additive migration and structural checks.
5. Durable `COMPLETE` or `FAILED` journal, without raw SQL or exception text.

An unchanged successful restart still runs the idempotent migration checks, but
does not accumulate another backup. A new database has no fictitious old snapshot.
An existing unsafe journal/lock/path stops startup; symlinked database files are
not silently followed. Backup or journal failure prevents migration execution.

Snapshots and attempt receipts are never automatically deleted or overwritten.
After a failed attempt, retain its original snapshot even if a later retry succeeds.
Repeated failed upgrades can consume disk; inspect the private directory before
retrying indefinitely. No blanket retention rule deletes these recovery anchors.

This is **not** an automatic database rollback engine. Stop every writer, retain
the failed database and WAL files, verify the selected snapshot's hash, and review
the matching code/schema before a manual restoration. Authentication/session
state has its own [security boundary](ADMIN_AUTH.md) and must not be blindly
revived from a historical database generation.

## Preparing a system restore

The existing archive format and public API remain compatible. The archive is
bounded, checksummed, structurally validated and copied to a private staging file.
The staged bytes are checked again before a safety backup is created. That
safety archive is reopened: its manifest, component hashes and SQLite integrity
must actually validate. A successful function return or a filename alone is not
enough. Backup creation synchronizes file contents before rename and the parent
directory afterward; filesystem synchronization failure cannot return success.

Publishing `restore-pending.zip` now uses atomic create-if-absent. Two workers or
a double click cannot replace an already prepared restore. A losing attempt
returns `RESTORE_ALREADY_PENDING`; its safety backup remains available. Inspect
the pending restore and explicitly resolve it before preparing another. The
application still does **not** replace a live SQLite database.

Preparation synchronizes the candidate before publication and its directory
afterward, then reads the published archive back. `pending_sha256` identifies
the verified bytes. Failure before publication returns
`RESTORE_PREPARATION_FAILED` without creating a new pending restore. Failure
after publication returns `RESTORE_PUBLICATION_UNCONFIRMED`, `prepared=false`
and `state=DIVERGED`. The pending path may already exist (or an external fault
may have removed it); inspect it and the retained safety backup before making
another decision. Do not interpret the error as permission to retry or to delete
the pending file. This is local filesystem verification, not proof of power-loss
behavior on every storage device or a completed live database restore.

## Installation permissions

The installer no longer recursively adds group permissions to all data files.
It prepares host-owned runtime directories, preserves runtime-owned private
files/directories, and does not traverse the secrets tree. It removes inherited
setgid from ordinary runtime directories so newly created private directories
can satisfy exact 0700 checks. Only the secrets parent can be made writable for
creation of a new private administrator directory; secret contents are untouched.

A legacy database that UID 10001 cannot write now stops installation with a
repair instruction instead of silently widening private file access. Keep a
backup and repair ownership explicitly with the service stopped. Rootless Docker
and ownership overrides still require local installation acceptance.

The installer now waits up to two minutes for the exact single Compose service
to become healthy, whether or not the optional update helper was selected.
An ambiguous/missing container, failed inspection, unhealthy state or timeout
returns failure. Data and logs remain available; there is no blind retry or
automatic rollback. `tools/install_release.sh` delegates to this same installer
instead of maintaining a second path with different safety checks.

## Verification

```sh
python -m pytest -q tests/test_database_migrations.py tests/test_migration_failure_safety.py tests/test_migration_snapshots.py
python -m pytest -q tests/test_backup_integrity.py tests/test_backup_manifest_contract.py tests/test_update_installer_300.py
```

These are temporary local database/filesystem tests, not production restore
acceptance. They include partial migration failure, WAL contents, duplicate
startup, failed/corrupt safety backups, synchronization errors, post-publication
readback failure, invalid/symlinked state and competing restore preparation.

The runtime image can additionally exercise real SQLite/migration code without
development dependencies. Use an already built **local** image:

```sh
docker run --rm --network none --read-only --cap-drop ALL \
  --security-opt no-new-privileges:true --tmpfs /tmp:rw,noexec,nosuid,size=128m \
  --entrypoint python LOCAL_IMAGE tools/probe_migrations.py
```

The probe constructs three synthetic compatibility schemas in its own temporary
directory. It checks additive repair, preset-source normalization, preserved
extension data, exact pre-change snapshot contents, idempotent restart and
restoration into a separate stopped database. A fourth scenario interrupts a
migration after a real SQLite schema change and retains its failed journal
while verifying safe resumption. Network calls are blocked; there are no host
mounts, installation data or radios. Results identify the actual architecture.
These fixtures are not full historical release images, a host-helper cutover
test or permission to overwrite a running database.
