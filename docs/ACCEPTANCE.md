# One acceptance session, separate evidence classes

Software verification and a real device experiment are different deliverables.
Passing CI never enables an unknown firmware/profile combination. LAB remains a
maturity label, not permission to bypass protection, authentication or readback.

Use this order to prepare a single supervised session. It is a checklist, **not
an executable radio controller**. Do not begin until release/security gates and
the remaining software review are explicitly clear. An unresolved container CVE,
missing ownership boundary or failed regression is not a manual acceptance task.

## Preparation without devices

Run quality, unit/integration, browser, coverage, mutation, secret, dependency and
container checks from the locked environment. Preserve results and source hashes
privately. Build and compare two archives from the exact accepted tree. Confirm
the clean-install and updater rollback tests. Hosted CI remains separate evidence
until it has actually run on GitHub.

Create a private session directory (0700) outside Git. Select the exact test
version and targets. For each target, record stable identity, full firmware,
model, variant and platform. Similar version numbers or model names are not
capability approval. Keep protected addresses **and** identities excluded before
constructing any device transport. A changed address requires fresh identity
verification; no blind retries and no broad LAN scan.

## Supervised sequence

| Stage | Action and observation | Stop condition / completion evidence |
|---|---|---|
| Installation/admin | Local clean installation; login, logout, expiry, CSRF and least-privilege tokens; correct listener separation | Anonymous device traffic must not gain admin access; admin protection must not break permitted device traffic |
| START | Explicit identity read; presets, settings, routes, source, zone, volume and runtime snapshot; checksummed backups | Any mismatch, missing mandatory backup or unknown write profile stops that target |
| Everyday controls | One designated radio, agreed low test volume; one command, actual readback, then next command | HTTP success alone is insufficient; uncertain write is reconciled, not replayed |
| Presets and metadata | One approved reversible slot change; stored readback, actual playback, physical button if required, station/title/artist/clock observation | Stored slot does not prove audible playability; save restore evidence |
| DLNA | Explicit media-server search or literal URL; separately connect; browse; import a generated MP3/AAC fixture; playback and seek/end-of-track observations | UDN/address change, protected target, codec mismatch or relay error stops the test; no household media required |
| Zone | One coordinator and one member, no implicit extra rooms; create/join/remove, distributed readback and observed volume/mute behavior | Partial member state is not success; inspect and restore each affected device |
| Recovery | Only agreed failure scenarios; observe reconnect and identity before further operations | IP reuse must abort; no uncontrolled power interruption or battery removal |
| Optional provider | Explicitly configured separate service; failure isolation, catalog/metadata, then any approved playable-media path | Catalog-only support must not be relabelled as verified playback |
| AirPlay | Only a separately approved receiver test; genuine Apple sender; audible output, metadata/volume, stable receiver and join/leave if claimed | Advertisement/simulation is not audio; missing stability leaves LAB status unchanged |
| Updater | Backed-up disposable/approved installation; candidate health, migration, commit and controlled rollback | No production data replacement without a verified recovery point; no half-success |
| END | Restore approved originals, re-identify, read back stable state and compare START/END | EXACT only for verified stable fields; volatile metadata is classified separately; PARTIAL/MANUAL remain open |

Do not perform destructive factory reset, firmware/authentication modification,
or a new hardware experiment just to complete a checklist. Each experiment needs
an explicit target and authorization. Diagnostic exports remain private until
redacted; archive hashes do not make private contents publishable.

## Machine-checked private records

`python -m basswiesn.acceptance /absolute/private/session/record.json` reads only
the named local record and its bounded sibling evidence files. It opens no
socket, changes no database and grants no device capability. A minimal software
record is:

```json
{
  "schema_version": 1,
  "test_id": "synthetic-dlna-parser",
  "feature": "dlna",
  "version": "3.0.2",
  "origin": "SIMULATED",
  "software_result": "PASS",
  "hardware_result": "NOT_RUN",
  "evidence": []
}
```

Actual hardware records use `origin: HARDWARE_TESTED`, an explicit observer and
timezone-aware observation timestamp, exact `expected`, `observed_start` and
`observed_end` identity objects (`device_id`, `model`, `firmware`, `variant`,
`platform`), and selected stable `expected_state`/`readback_state` fields.
Evidence entries have `role`, a plain sibling `file` name and `sha256`.
Required roles are START, END, READBACK and OBSERVATION; writes additionally need
BACKUP and COMMAND, `writes_performed: true`, and an explicit restore result.

The validator rejects mismatched identities, unknown/wildcard profiles, missing
evidence, hash changes, duplicate JSON keys, symlinks, traversal and attempts to
claim hardware PASS from simulation. PASS hardware declarations require matching
state, software PASS and EXACT restore for writes. Failure/partial records are
retained as failures, not converted into approvals.

The tool reports **record integrity**, never the authenticity of an audible
observation. Even a complete hardware record returns `MANUAL_REVIEW_REQUIRED` and
`hardware_verified_by_tool: false`. Review the actual experiment before changing
any versioned capability policy. There is no automatic import into write allowlists.

The complete schema is available offline through
`basswiesn.acceptance.AcceptanceRecord.model_json_schema()`. Keep real records
and device identities outside the public repository.
