# Device settings: verified state, not transport acceptance

Status: implemented and software-tested with synthetic devices. Hardware
validation: **NOT RUN**. This change does not publish a release or grant a new
device class hardware approval.

The existing rename, single-setting and batch-setting URLs remain in place.
Their router validates input and delegates orchestration to
`services/device_settings_writer.py`. Fixed XML shapes and typed comparisons
live in `services/settings_contract.py`; neither contains another network
transport or a replacement protection policy.

## Explicit boundary

Ordinary rename now uses only the documented HTTP name operation. The old
automatic SSH remount/edit of the persistent configuration file has been
removed. It had no verified backup/restore contract and could report success
from a later shell command even if the actual edit failed. Its helper name
incorrectly suggested a read-only operation.

The response retains a `persistence` object, but explicitly reports that no
extra persistent write was performed and reboot persistence was not tested.
This is intentionally not a guarantee that the HTTP name survives a reboot.
A future persistent repair needs its own preview, scope, backup and restore
approval; it must not be hidden inside a normal rename.

## Transaction sequence

1. Validate every requested field before creating a transport. Reject unknown
   setting names, malformed numbers, ambiguous booleans, unsafe XML names,
   unsupported latency modes and conflicting clock settings.
2. Acquire the existing cooperative per-device lock. Enforce the shared IP and
   stable-ID protection policy, including configured write restrictions, before
   constructing the identity-bound HTTP client.
3. Read `/info`. Compare stable identity, model, exact firmware, variant and
   platform with stored evidence. Reuse the existing exact hardware-profile
   registry; do not authorize a new build using a version prefix. Product ID is
   checked when present, otherwise the existing unique-profile derivation is
   retained. This registry is a conservative prerequisite, not a per-setting
   hardware certification.
4. Read every affected endpoint and parse actual state. Unknown/missing state
   blocks the operation. A target volume is not the actual volume; absent state
   is not zero or false.
5. Persist START XML in `ConfigBackup`, commit, reload and verify SHA-256. No
   write follows a failed backup. The intent ledger references the backup ID
   and digest. This uses existing storage, with no destructive migration.
6. Before each changed field, verify identity and state again. An optional
   `expected_previous` map can bind a request to the caller's observed state.
   A value changed since START is not silently overwritten.
7. Persist a `PENDING_READBACK` intent **before** sending the single POST. Do
   not replay a timed-out POST. Read `/info` and the affected field to determine
   actual state, including when the transport reply was lost.
8. Only exact requested/actual agreement commits a verified change. A rename
   changes the local cached name only after verified `/info` readback. Lost
   transport replies remain visible as `transport_uncertain` even if the final
   state is verified.

An unchanged value produces no POST. Partial clock configuration contains only
explicit fields; it does not inject default timezone, enablement or brightness.
Clock comparison ignores unrelated fields but verifies every requested field.
Advancing or firmware-normalized requested clock values can still diverge; they
are never silently accepted as a hardware PASS.

For `clockConfig`, `expected_previous` may contain just the attributes being
changed. It must cover **all** requested attributes, retain their XML string
types and match the actual readback. An empty or incomplete snapshot fails.
If a caller supplies additional snapshot attributes, those also have to match.
This prevents an advancing, untouched clock value from invalidating a change
to brightness alone without ignoring a brightness conflict.

For example, after a fresh read reports actual volume 5 and clock brightness
`"70"`, an explicit batch may contain:

```json
{
  "values": {"volume": 1, "clockConfig": {"brightnessLevel": 0}},
  "expected_previous": {"volume": 5, "clockConfig": {"brightnessLevel": "70"}}
}
```

The request targets the selected stable ID in the existing `settings-apply`
URL. The numeric requested brightness is normalized to an XML attribute by
the contract; the previous attribute remains an exact string from readback.
There is deliberately no timezone, enablement or UTC reset in this example.
If either requested previous value differs at preflight, the batch is blocked
before a POST. Clients must not respond by discarding the precondition and
automatically resubmitting. The legacy API keeps `expected_previous` optional
for compatibility, but the browser now always supplies it for its changed fields.

The local `station_art_mode` preference does not construct a radio transport.
It continues to return `preset_sync_required`, not a claim that the display or
presets have already changed. A mixed batch commits that local preference only
after the radio portion has completed.
The local preference also accepts an `expected_previous` value. A mismatch
stops a mixed batch before any radio transport is constructed.

## Target-bound browser state

The settings controller is an isolated, dependency-injected ES module in
`static/js/device-settings.js`. Loading this module makes no device request.
Opening settings, changing the selected device or pressing Reload explicitly
starts the read. Reload reads this target, rather than refreshing every radio.

Each read belongs to a stable device ID and a monotonically increasing view
generation. A new selection cancels the prior browser GET. Generation checks
also reject a result that was already queued before cancellation. A failed
read or contradictory response ID leaves the controls blocked. Missing fields
and error strings are not converted into zero, false or an enabled clock.

Apply captures the target, the changed fields and their previous values in one
request. A single clock edit sends only that attribute: it does not reset UTC,
enablement, timezone or other unchanged controls. Browser single-flight guards
reject double submission even if a caller dispatches another submit event.
The server independently validates the snapshot and all safety prerequisites.

Changing the selection while a POST is outstanding does not cancel or replay
that POST. Its outcome must not populate the newly selected radio's form or
start a follow-up synchronization against that other device. After a partial
or uncertain outcome on the current target, Apply remains blocked until an
explicit fresh read. A server-side durable intent/readback record remains the
source of truth, not whether the browser stayed on that page.

Preset synchronization has its own target-bound preview. Switching targets or
editing settings invalidates it. A late preview cannot reveal a confirmation
button for a different radio. A local display change offers a separate preview
and confirmation; it does not automatically start that follow-up. These UI
guards supplement the existing server preset-write checks; they do not solve
cross-process revisions or replace server-side authorization.

## Partial failure is an outcome

A batch is not an atomic radio transaction. If volume verifies but bass does
not, the response is HTTP 409 with, for example:

```json
{
  "detail": {
    "ok": false,
    "status": "DIVERGED",
    "code": "WRITE_READBACK_MISMATCH",
    "applied": 1,
    "failed_setting": "bass",
    "reconciliation_required": true,
    "changed": [{"setting": "volume", "after": 1}],
    "job_id": "settings-example"
  }
}
```

This abbreviated example omits backup references and the full per-field
readback. The real result retains the verified prefix. The service stops before
later writes; it does not automatically roll back or repeat the failing field.
Rollback would itself overwrite state and needs a fresh identity/precondition
check. The UI displays the partial outcome and operation identifier, not the
misleading statement that nothing was written.

If the process is cancelled or dies after durable intent, that intent remains
pending rather than being incorrectly marked successful. A database failure
after radio readback likewise cannot be called a committed transaction. A
best-effort divergence entry supplements the already durable intent; an
unavailable audit database cannot erase that distinction.

## Limits and regression evidence

The lock coordinates cooperating jobs in one process. It cannot serialize a
second application process, another controller or the radio's physical keys.
SoundTouch exposes no atomic compare-and-swap across the check and POST. These
limits are not solved by claiming that HTTP succeeded. Durable cross-process
job ownership remains a separate engineering task.

Tests cover protected identity before construction, unknown firmware, changed
identity before/after POST, actual-versus-target volume, lost replies, missing
readback, partial batches, absent state, backup failure, post-write database
failure, front-panel changes, exact readback, unchanged values, strict XML
validation and local-only preferences. Browser tests use mocked responses and
loopback only. No test establishes sound output, reboot persistence or a new
model's compatibility.

Other router/legacy setup paths are not automatically fixed by this service.
Their write-path audits and hardware acceptance remain separate.
## Shared multi-device latency setting

The standalone `/api/multiroom/latency` route uses the same settings transaction,
not its own substring-based verification. It protects and acquires ownership of
all selected devices, checks every identity/profile and current mode before any
write, then applies the setting with each captured value as its precondition.
Each device gets the normal durable START backup, intent and strict XML readback.
A failure stops remaining targets and returns the verified prefix and failed
target; no automatic retry or rollback is performed. The batch is not atomic,
and external/front-panel changes can still race radio-side operations.
