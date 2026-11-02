# ADR 0002: cooperative ownership across application workers

Status: implemented increment; remaining legacy paths are explicit gaps.

Separate web/cloud workers cannot coordinate device changes with an in-memory
async lock. Use UID-private filesystem advisory locks keyed by canonical stable
identity, with strict namespace checks, bounded async acquisition and automatic
kernel release on process exit. No database migration is required.

For composed operations, acquire the complete known target set in sorted order.
Borrowing is allowed only for an already-owned subset in the same task. Child
tasks and target expansion cannot borrow authority. Never unlink a lock file.

This choice preserves existing async-lock callers while providing real local
cross-process exclusion. It requires a common data mount and supported local
filesystem; it does not coordinate external controllers or replace identity,
capability, previous-state, backup or readback validation. Networked/distributed
deployments require a separately reviewed ownership mechanism.

See [adopted paths, failure semantics and tests](../DEVICE_TRANSACTIONS.md).
