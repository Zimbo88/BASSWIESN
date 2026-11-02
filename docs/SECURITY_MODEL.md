# Security model and implementation boundaries

BASSWIESN controls devices whose firmware and local APIs do not provide a
uniform transaction protocol. Its central invariant is **transport acceptance
is not verified device success**. Security must protect both the administration
service and the physical devices. An HTTP 200, a passed simulator or a source
code scanner cannot substitute for actual device readback.

This is a model of the inspected implementation, including gaps. It does **not**
claim that the requested authenticated hardened-LAN profile is implemented.
An opt-in [administrator authentication preview](ADMIN_AUTH.md) now provides
local bootstrap, Argon2id, sessions and Origin/CSRF protection. Legacy mode is
still unauthenticated. A bounded [scoped-token API](API_TOKENS.md) is implemented
for explicitly reviewed web routes. Literal device-peer allowlists, ingress quotas
and path-identity binding are implemented in required mode. Body-identity policy,
wider automation grants and deployment acceptance remain open; do
not expose the administration or debug listeners to the Internet or an untrusted LAN.

## Assets and attackers

| Asset | Failure to prevent |
|---|---|
| Stable radio identity and protection policy | Contacting a protected device, or treating an IP reused by a different device as the original radio |
| Presets, source routes and settings | Unintended overwrite, partial changes reported as successful, missing recovery evidence |
| Backups and database | Corruption, incompatible restore, replaced snapshot, unauthorized reading of private configuration |
| Administrator sessions and update authorization | Credential disclosure, confused-deputy host operation, replay or excessive privilege |
| Provider responses and media URLs | SSRF, redirect to a protected destination, unbounded parsing/resource use |
| Listening history and diagnostics | Disclosure of household identifiers, credentials, usage history or pairing material |
| Release artifacts and dependency graph | Mutable dependencies, changed artifact bytes, false provenance or unsafe upgrade |

The threat actors include an unauthenticated LAN client, compromised IoT device,
malicious browser page, hostile media/provider server, DNS or redirect attacker,
tampered release source and compromised application container. The model does
not assume that local-network origin implies administrative authority.

## Trust boundaries

| Boundary | Existing controls | Remaining requirements |
|---|---|---|
| Browser / human API → web listeners | Opt-in administrator session/CSRF boundary, explicit action routes, per-feature validation, hardware safety controls | Complete authenticated workflow acceptance and deployment review |
| Automation → web API | Private hashed expiring/revocable tokens; exact method/template/handler grants; no bearer-to-cookie fallback; session/CSRF-only token-management UI | More reviewed contracts and optional target restrictions/quotas; no blanket admin token |
| Radio → cloud compatibility endpoints | Explicit socket-peer allowlist, bounded body/framing/time/rate/concurrency, host-shared rate/active quotas, cached path-ID binding; no forwarded-header trust | Remaining provider-specific schema/identity contracts and actual radio interoperability; no administrator privileges or multi-host quota claim |
| Device compatibility on the web port | Effective first-route authorization and the same device-ingress policy as cloud | Endpoint-specific schema/identity acceptance; blanket human login would break device communication |
| Debug listener | Required-mode session/origin access control; minimal `/live` and `/ready` public | Deployment/HTTPS review; do not imply port separation isolates cookies |
| Application → radio transports | Central IP/stable-ID protection, mapping checks, capability/profile checks in setup and verified settings transactions | Audit remaining legacy paths; prove every critical write's exact state contract |
| Application → provider/media URLs | Central URL policy, DNS pinning, redirect checks in guarded clients | Continue auditing every transport and redirect chain, including actual peer and IPv6 behavior |
| Container → host updater | Separate UNIX socket helper, peer/capability authorization, staging/journal/rollback | General web auth cannot replace host-helper authorization; candidate acceptance needs software and deployment tests |
| Database/filesystem → restore | Bounded archive validation, complete v1 manifest, SHA-256, SQLite checks, safety backup and staged-file validation | Restore remains prepare-and-restart, not a fully automated rollback transaction |
| Source/dependencies → runtime | Hash-locked dependencies, version contract, local CI definitions/scanners, deterministic source-archive machinery | Remote CI execution and clean-install/upgrade acceptance; local double-build evidence is not publication approval. Container vulnerabilities must be triaged, not silently ignored |

The listeners are mapped in more detail in
[the engineering baseline](ENGINEERING_BASELINE.md). Human authentication must be
enforced on the server, not inferred from the selected UI mode. EASY, STANDARD
and LAB describe usability and maturity; none disables target protection.

[ADR 0001](adr/0001-administration-boundaries.md) records the auth
implementation contract. The cloud listener also contains a human inventory
route (`/api/cloud/stations`), so neither listener names nor broad URL-prefix
exemptions are sufficient. Cookies are not isolated by port. These are review
requirements, not a claim that all hardened-LAN acceptance criteria are complete.

## Protected targets: fail closed before transport

Protection combines configuration and database policy. Both stable identity and
address matter. IPv4-mapped IPv6 literals now canonicalize to their IPv4 target,
so an alternate address spelling cannot evade the same-target comparison.

If persisted protection policy cannot be read, the policy accessor raises
`PROTECTION_POLICY_UNAVAILABLE`. It does not return an empty set. The request
must stop before a socket/client is constructed. An empty, readable fresh
database is different: it has no additional configured policy, but immutable
and environment protections still apply.

No cache is substituted for an unreadable policy. A stale cached allow decision
would let a background job ignore a newly protected target. Failure tests cover
direct radio access, explicit discovery follow-up and generic URL consumers.

## Outbound URL authority

The shared HTTP validator rejects malformed ports, userinfo (including an empty
username), control/whitespace characters, ambiguous backslashes and encoded host
characters before DNS. A pinning receipt binds the hostname, scheme and effective
port as well as the approved address set. It cannot authorize a different origin.
Pinning performs no second DNS lookup; every redirect needs fresh validation.

Public-only requests reject non-global addresses, IPv4-mapped restricted ranges
and IPv6 transition mechanisms such as NAT64, 6to4 and Teredo whose eventual
inner peer this policy cannot establish. This conservative limitation is not a
claim that all translated public networks are inherently unsafe. Explicit local
media routes retain their separate LAN policy; protected-target rejection applies
to both policies. SoundTouch descriptor discovery accepts only its established
IPv4 LAN/fixture contract, not arbitrary reserved addresses. An explicitly empty
port allowlist grants no access. These checks do not by themselves prove every
legacy transport has been migrated to actual-peer verification.

## Ambiguous writes and recovery

The transport coordinator may spend a retry budget on GET reads only. A POST
is sent at most once by that coordinator, even if the reply times out. This is
not a complete write transaction: the caller still owns identity, capability,
previous state, expected state, actual readback and reconciliation.

An uncertain write must not be presented as definitely failed or automatically
replayed. Other legacy transports and higher-level retry loops still require
the write-path review; this coordinator fix is not proof that all callers
already implement the complete desired contract.

Rename and human device settings now delegate to a shared
[settings transaction](SETTINGS_TRANSACTIONS.md): identity/profile verification,
durable START backup and intent, a single write attempt, actual-state comparison
and an explicit partial/divergent result. Ordinary rename has no hidden SSH
fallback. Its software tests do not establish reboot persistence or globally
serialize other processes and external controllers.

### Explicit SSH persistence recovery

The legacy recovery URL delegates to a typed marker transaction. An actual write
requires a JSON boolean `dry_run: false` and explicit confirmation. It acquires
the shared device owner, checks protection before transports, verifies live
identity against the existing exact hardware-profile registry, and reads a
bounded marker snapshot through an already working SSH connection. The hashed
START snapshot and intent are committed before any write. Target, marker state
and stored snapshot integrity are checked again before the one write attempt.

Existing marker files are not overwritten or touched. Only missing fixed markers
are created; no daemon restart, startup-script edit, reboot, automatic retry or automatic rollback
is added. A new SSH read and another identity check establish the observed result.
A lost reply is not proof of failure; incomplete readback, changed identity or a
failed result commit produces divergence, never verified success. Cancellation
retains the durable intent for review and releases cooperative ownership.

The backup scope is marker existence plus identity, not a full filesystem image.
Reconciliation of newly created markers is manual. Marker readback proves neither
reboot persistence nor full SSH hardening; `persistent_ssh_boot_verified` remains
false. This route still uses the legacy SSH transport and its host-key policy;
the stricter setup runner does not automatically protect this caller. Firmware
compatibility, reboot behavior and improved host-key enrollment require separate
acceptance. Cooperative locking cannot prevent a front-panel action, an external
controller or a malicious process on the radio from racing a check.

### Setup worker ownership and interruption

Setup and reboot workers share a SQLite lease. Conditional SQL acquisition,
renewal and release compare the current owner and job in the database, not a
cached ORM row. A missing job record or terminal status does not release an
unexpired lease: another worker may be performing recovery. The same owner
cannot substitute a different job, and a heartbeat cannot revive an expired
lease. Duplicate execute/rollback calls in one coordinator are rejected.

Startup recovery only marks interrupted checkpoints for review after acquiring
this lease. Production startup makes bounded local database checks for up to
about 100 seconds, allowing the usual 90-second stale lease to expire. A live
worker keeps its job. If ownership remains active, startup leaves it untouched;
it does not force recovery or replay a command. An unusually long stale lease
may require a later explicit restart/review.

Heartbeat uses a separate database session, so it cannot commit an executor's
incomplete state. Setup checks live ownership at step boundaries. On loss, it
preserves the completed step's evidence and starts no following operation.
Terminal or interrupted checkpoints are not automatic retry entry points.
These checks supplement the shared per-device filesystem lock; they do not
make an already in-flight radio operation cancellable or coordinate another
application controlling the same radio. Rollback/readback remains explicit.

## Backups are private, not authenticated release artifacts

System backups contain a full database; the separately redacted settings JSON
does **not** make that database safe to publish. New archive files are created
with mode 0600, have unique names even within the same second and are atomically
placed at their final name. SQLite's backup API captures the database, and the
copied database is checked before a successful archive result is returned.
File and parent-directory synchronization must succeed before backup creation
returns success. A synchronization error after publication may leave an archive
for inspection; it is not reported as a verified backup.

Preview rejects missing/duplicate components, unsupported formats or schema,
invalid digests, unsafe archive members, failed integrity state and non-SQLite
payloads. Hashes must match; a forged `quick_check: ok` field is insufficient.
Restore preparation validates the copied candidate again, creates and reopens a
separate pre-restore backup for integrity checks, then atomically publishes the
pending archive only if none exists. It synchronizes the candidate and directory
and verifies the published bytes before reporting preparation success. A failed
mandatory backup cannot authorize publication. Uncertain post-publication state
returns `DIVERGED` and preserves evidence for manual review; no blind retry or
replacement of an existing pending restore is performed.

SHA-256 detects corruption relative to a manifest; it does not authenticate a
manifest supplied by an attacker. The private local backup directory therefore
remains a trust boundary. Restoring still requires an explicit maintenance
step, followed by migration and health verification. No device is contacted by
the archive preview or preparation implementation.

## Secret handling and diagnostic exports

Shared redaction handles structured sensitive fields and recognizable secrets
in free-form error strings. Tests cover bearer headers, cookies, quoted values,
URL credentials/query parameters, XML/JSON fields and private-key envelopes.
Redaction occurs before truncation. Support text anonymizes both IPv4 and IPv6;
private operational logs may retain addresses for local diagnosis.

Redaction is defence in depth, not permission to log credentials. Arbitrary
opaque bytes without a sensitive field/envelope cannot be recognized reliably.
Do not collect private keys, pairing material, session cookies or authorization
capabilities into general diagnostic payloads. Review exports before sharing;
not every historical log is retroactively sanitized by these changes.

## Runtime confinement and verification

The application remains non-root with a read-only root filesystem, all Linux
capabilities dropped, `no-new-privileges` and restricted temporary filesystems.
No Docker socket is mounted. Host maintenance belongs to the narrow updater,
not a privileged application container.

Software tests use mocks or loopback; CI adds a network namespace that has no
external network interfaces. The isolated container smoke test has no network
and no host data bind. Property/failure tests and a small mutation gate test
specific safety assumptions. They do not imply hardware acceptance.

See [security checks](SECURITY_CHECKS.md) for scanner scope and narrowly reviewed
exceptions. Dependency, container and application security are distinct gates.
A clean Python dependency audit must not hide a failing base-image scan.

Updater authentication has a separate [runtime-attestation and credential
continuity contract](UPDATER_AUTH.md). Its loopback capability is read-only and
not an administrator session. Rollback refuses changed/unknown credential state;
the supported launcher revokes restored sessions before any worker can serve
requests. This does not resolve the remaining hardened-LAN acceptance work.
