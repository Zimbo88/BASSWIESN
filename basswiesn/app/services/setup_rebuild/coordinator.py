"""Persistent, sequential setup coordinator.

The coordinator is deliberately adapter-based. Hardware transports can be
replaced with fakes in integration tests, while every real step still uses
the same durable state transitions and lease.
"""

from __future__ import annotations

import asyncio
from datetime import UTC, datetime
import json
from typing import Any, Protocol
from uuid import uuid4

from basswiesn.app import db as app_db
from basswiesn.app.models import SetupRebuildJob
from basswiesn.app.services.setup_rebuild.repository import SetupRepository
from basswiesn.app.services.setup_rebuild.server_target import ServerTarget
from basswiesn.app.services.setup_rebuild.states import SetupState
from basswiesn.app.services.action_journal import record_action


class SetupDeviceAdapter(Protocol):
    async def identify(self, row: Any) -> dict[str, Any]: ...
    async def backup(self, row: Any) -> dict[str, Any]: ...
    async def ssh_status(self, row: Any) -> dict[str, Any]: ...
    async def activate_ssh(self, row: Any) -> dict[str, Any]: ...
    async def persist_ssh(self, row: Any) -> dict[str, Any]: ...
    async def reboot_verify_ssh(self, row: Any) -> dict[str, Any]: ...
    async def backup_routing(self, row: Any) -> dict[str, Any]: ...
    async def route(self, row: Any, target: ServerTarget) -> dict[str, Any]: ...
    async def reboot(self, row: Any) -> dict[str, Any]: ...
    async def reconnect(self, row: Any) -> dict[str, Any]: ...
    async def pair_account(self, row: Any, target: ServerTarget) -> dict[str, Any]: ...
    async def read_presets(self, row: Any) -> dict[str, Any]: ...
    async def playback_test(self, row: Any, target: ServerTarget) -> dict[str, Any]: ...
    async def rollback(self, row: Any) -> dict[str, Any]: ...


class SetupCancellationRequested(RuntimeError):
    """Stop a setup job at the next durable operation boundary."""


_STATE_PROGRESS = {
    SetupState.UNKNOWN: 0,
    SetupState.DISCOVERED: 5,
    SetupState.IDENTIFIED: 10,
    SetupState.BACKUP_PENDING: 15,
    SetupState.BACKUP_COMPLETE: 25,
    SetupState.SSH_STATUS_PENDING: 27,
    SetupState.SSH_ALREADY_ACTIVE: 29,
    SetupState.SSH_ACTIVATION_PENDING: 30,
    SetupState.SSH_TEMPORARY_ACTIVE: 32,
    SetupState.SSH_PERSISTENCE_PENDING: 34,
    SetupState.SSH_REBOOT_PENDING: 36,
    SetupState.SSH_VERIFIED: 38,
    SetupState.ROUTING_BACKUP_COMPLETE: 40,
    SetupState.BASSWIESN_ROUTE_PENDING: 45,
    SetupState.BASSWIESN_ROUTE_ACTIVE: 55,
    SetupState.RADIO_REBOOT_PENDING: 58,
    SetupState.RADIO_REBOOTED: 62,
    SetupState.RADIO_RECONNECT_PENDING: 65,
    SetupState.RADIO_REACHABLE: 72,
    SetupState.MARGE_ACCOUNT_PENDING: 76,
    SetupState.MARGE_ACCOUNT_ACTIVE: 84,
    SetupState.PRESETS_READABLE: 90,
    SetupState.PLAYBACK_READY: 96,
    SetupState.VERIFIED: 100,
}


class DryRunAdapter:
    """No-transport adapter used for previews and software integration tests."""

    def __init__(self, *, delay_seconds: float = 0.0):
        self.delay_seconds = max(0.0, float(delay_seconds))

    async def _pause(self) -> None:
        if self.delay_seconds:
            await asyncio.sleep(self.delay_seconds)

    async def identify(self, row: Any) -> dict[str, Any]:
        await self._pause()
        return {"device_id": row.device_id, "model": row.expected_model, "firmware": "dry-run", "verified": True}

    async def backup(self, row: Any) -> dict[str, Any]:
        await self._pause()
        return {"backup_path": f"simulation://{row.device_id}/backup", "sha256": {}, "verified": True}

    async def ssh_status(self, row: Any) -> dict[str, Any]:
        await self._pause()
        return {"ssh_status": "SSH_UNKNOWN", "already_active": True, "profile_key": "dry-run"}

    async def activate_ssh(self, row: Any) -> dict[str, Any]:
        await self._pause()
        return {"ssh_status": "SSH_TEMPORARILY_ENABLED"}

    async def persist_ssh(self, row: Any) -> dict[str, Any]:
        await self._pause()
        return {"ssh_status": "SSH_PERSISTENTLY_ENABLED"}

    async def reboot_verify_ssh(self, row: Any) -> dict[str, Any]:
        await self._pause()
        return {"ssh_status": "SSH_VERIFIED_AFTER_REBOOT", "verified": True}

    async def backup_routing(self, row: Any) -> dict[str, Any]:
        await self._pause()
        return {"routing_backup": True, "sha256": {}}

    async def route(self, row: Any, target: ServerTarget) -> dict[str, Any]:
        await self._pause()
        return {"routing_status": "active", "target": target.to_public_dict()}

    async def reconnect(self, row: Any) -> dict[str, Any]:
        await self._pause()
        return {"reachable": True}

    async def reboot(self, row: Any) -> dict[str, Any]:
        await self._pause()
        return {"reboot_requested": True}

    async def pair_account(self, row: Any, target: ServerTarget) -> dict[str, Any]:
        await self._pause()
        return {"account_paired": True, "account_changed": False, "account_id": "dry-run"}

    async def read_presets(self, row: Any) -> dict[str, Any]:
        await self._pause()
        return {"presets_readable": True, "count": 0}

    async def playback_test(self, row: Any, target: ServerTarget) -> dict[str, Any]:
        del target
        await self._pause()
        return {"playback_ready": True, "volume": 1}

    async def rollback(self, row: Any) -> dict[str, Any]:
        await self._pause()
        return {
            "rolled_back": True,
            "fully_restored": True,
            "rollback_scope": "SIMULATION",
        }


class SetupCoordinator:
    def __init__(
        self,
        *,
        adapter: SetupDeviceAdapter | None = None,
        repository: SetupRepository | None = None,
        session_factory=None,
    ):
        self.adapter = adapter
        self.repository = repository or SetupRepository()
        self.owner_id = str(uuid4())
        self.session_factory = session_factory or app_db.SessionLocal

    def preview(
        self,
        *,
        device_ids: list[str],
        target: ServerTarget,
        options: dict[str, Any] | None = None,
    ) -> dict[str, Any]:
        normalized = [str(item or "").strip().upper() for item in device_ids]
        if not normalized or len(normalized) != len(set(normalized)):
            raise ValueError("Bitte mindestens ein Radio genau einmal auswählen.")
        public_options = {
            "ssh_required": bool((options or {}).get("ssh_required", False)),
            "pair_account": bool((options or {}).get("pair_account", True)),
            "playback_test": bool((options or {}).get("playback_test", True)),
            "simulation": bool((options or {}).get("simulation", False)),
        }
        sequence = ["identity", "backup"]
        if public_options["ssh_required"]:
            sequence.extend(
                ["ssh_status", "ssh_activation", "ssh_persistence", "ssh_reboot_verification"]
            )
        sequence.extend(["routing_backup", "routing", "radio_reboot", "reconnect"])
        if public_options["pair_account"]:
            sequence.append("local_account_pairing")
        sequence.append("preset_readback")
        if public_options["playback_test"]:
            sequence.append("volume_1_playback")
        sequence.append("final_readback")
        plan = {
            "engine": "setup-rebuild-v2",
            "sequence": sequence,
            "device_ids": normalized,
            "target_server": target.to_public_dict(),
            "options": public_options,
            "normal_transport_path": ["HTTP 8090", "CLI 17000"],
            "ssh_required": public_options["ssh_required"],
            "ssh_reason": (
                "expliziter Expertenlauf"
                if public_options["ssh_required"]
                else "Der bestätigte Standardpfad verwendet HTTP und CLI 17000."
            ),
            "maximum_audio_volume": 1,
            "usb_required": False,
            "factory_reset": False,
            "firmware_flash": False,
        }
        return plan

    def start(
        self,
        db,
        *,
        device_ids: list[str],
        target: ServerTarget,
        dry_run: bool = False,
        options: dict[str, Any] | None = None,
    ) -> dict[str, Any]:
        plan = self.preview(device_ids=device_ids, target=target, options=options)
        job = self.repository.create_job(
            db,
            device_ids=device_ids,
            target_server=target.to_public_dict(),
            plan=plan | {"dry_run": bool(dry_run)},
        )
        if not self.repository.acquire_lease(db, owner_id=self.owner_id, job_id=job.job_id):
            raise RuntimeError("another setup rebuild is already active")
        job.owner_id = self.owner_id
        job.started_at = datetime.now(UTC)
        job.status = "running"
        job.updated_at = datetime.now(UTC)
        db.commit()
        return self.repository.public_job(db, job)

    async def execute(self, job_id: str, *, dry_run: bool = False) -> dict[str, Any]:
        # A non-dry run must never silently degrade to a preview. The real
        # adapter is created per execution so its baseline timestamp is
        # stable for the complete job.
        if dry_run:
            adapter = DryRunAdapter(delay_seconds=0.15)
        else:
            from basswiesn.app.services.setup_rebuild.radio_adapter import RadioSetupAdapter

            adapter = self.adapter or RadioSetupAdapter()
        db = self.session_factory()
        lease_acquired = False
        heartbeat = None
        try:
            job = self.repository.job(db, job_id)
            if job is None:
                raise ValueError("setup rebuild job not found")
            try:
                lease_acquired = self.repository.acquire_lease(db, owner_id=self.owner_id, job_id=job_id)
            except Exception as exc:
                job.status = "failed"
                job.error = f"coordinator lease failed: {str(exc)[:500]}"
                job.ended_at = datetime.now(UTC)
                db.commit()
                return self.repository.public_job(db, job)
            if not lease_acquired:
                return self.repository.public_job(db, job)

            if self._cancellation_requested(job_id):
                job.status = "cancelled"
                job.ended_at = datetime.now(UTC)
                db.commit()
                return self.repository.public_job(db, job)

            async def renew_lease() -> None:
                while True:
                    await asyncio.sleep(20)
                    self.repository.renew_lease(db, owner_id=self.owner_id, job_id=job_id)

            heartbeat = asyncio.create_task(renew_lease())
            job.status = "running"
            job.started_at = job.started_at or datetime.now(UTC)
            db.commit()
            rows = self.repository.states(db, job_id)
            total = len(rows)
            for index, row in enumerate(rows, start=1):
                if self._cancellation_requested(job_id):
                    job.status = "cancelled"
                    job.ended_at = datetime.now(UTC)
                    db.commit()
                    break
                job.current_device_id = row.device_id
                db.commit()
                await self._run_device(db, job, row, adapter, dry_run=dry_run)
                if job.status == "cancelled":
                    job.updated_at = datetime.now(UTC)
                    db.commit()
                    break
                job.progress = int(index / max(total, 1) * 100)
                job.updated_at = datetime.now(UTC)
                db.commit()
            else:
                verified = [row.device_id for row in rows if row.state == SetupState.VERIFIED.value]
                failed = [row.device_id for row in rows if row.state == SetupState.FAILED.value]
                job.status = (
                    "completed"
                    if len(verified) == len(rows)
                    else "partial_failure"
                    if verified
                    else "failed"
                )
                job.current_state = (
                    SetupState.VERIFIED.value if verified else SetupState.FAILED.value
                )
                job.ended_at = datetime.now(UTC)
                job.progress = 100
                job.error = "; ".join(
                    f"{row.device_id}: {row.last_error or 'not verified'}"
                    for row in rows
                    if row.state == SetupState.FAILED.value
                )
                job.result_json = json.dumps({
                    "total_devices": len(rows),
                    "verified_count": len(verified),
                    "failed_count": len(failed),
                    "verified_devices": verified,
                    "failed_devices": failed,
                })
                db.commit()
            return self.repository.public_job(db, job)
        finally:
            if heartbeat is not None:
                heartbeat.cancel()
                try:
                    await heartbeat
                except asyncio.CancelledError:
                    pass
            if lease_acquired:
                self.repository.release_lease(db, owner_id=self.owner_id, job_id=job_id)
            db.close()

    async def resume_pending_jobs(self) -> None:
        """Recover interrupted jobs without replaying a hardware operation.

        A persisted checkpoint proves where the previous process stopped, but
        it cannot prove whether the radio applied the last command immediately
        before that process died.  Blindly restarting ``_run_device`` would
        either violate the state machine or repeat a write.  Keep all backup
        evidence and fail closed so the user can inspect/read back and choose
        an explicit rollback or a new setup run.
        """

        db = self.session_factory()
        try:
            jobs = db.query(SetupRebuildJob).filter(
                SetupRebuildJob.status.in_(("pending", "running")),
            ).order_by(SetupRebuildJob.created_at).all()
            interrupted_at = datetime.now(UTC)
            for job in jobs:
                cancelled = bool(job.cancel_requested)
                error = (
                    "Setup wurde während des angeforderten Abbruchs durch einen "
                    "Dienstneustart unterbrochen. Es wurde kein Schritt automatisch "
                    "wiederholt. Bitte Sicherung und Gerätezustand prüfen."
                    if cancelled
                    else
                    "Setup wurde durch einen Dienstneustart unterbrochen. "
                    "Aus Sicherheitsgründen wurde kein Schritt automatisch wiederholt. "
                    "Bitte Readback und Sicherung prüfen und anschließend explizit "
                    "Rollback oder einen neuen Setup-Lauf wählen."
                )
                for row in self.repository.states(db, job.job_id):
                    if row.state in {
                        SetupState.VERIFIED.value,
                        SetupState.FAILED.value,
                        SetupState.ROLLED_BACK.value,
                    }:
                        continue
                    try:
                        self.repository.transition_state(
                            db, row, SetupState.FAILED, error=error
                        )
                    except (TypeError, ValueError):
                        # A corrupt/unknown checkpoint is even less safe to
                        # replay. Preserve its evidence but force manual review.
                        row.state = SetupState.FAILED.value
                        row.last_error = error
                        row.updated_at = interrupted_at
                    row.recovery_status = (
                        "INTERRUPTED_CANCELLED"
                        if cancelled
                        else "INTERRUPTED_REVIEW_REQUIRED"
                    )
                job.status = "cancelled" if cancelled else "failed"
                job.current_state = SetupState.FAILED.value
                job.error = error
                job.ended_at = interrupted_at
                job.updated_at = interrupted_at
            db.commit()
        finally:
            db.close()

    async def _run_device(self, db, job, row, adapter: SetupDeviceAdapter, *, dry_run: bool) -> None:
        def update_progress(target: SetupState) -> None:
            local = _STATE_PROGRESS.get(target)
            if local is None:
                return
            rows = self.repository.states(db, job.job_id)
            total = max(1, len(rows))
            position = next((index for index, item in enumerate(rows) if item.id == row.id), 0)
            job.progress = min(100, int(((position * 100) + local) / total))
            job.updated_at = datetime.now(UTC)

        def cancellation_checkpoint() -> None:
            # Cancellation is written by a different HTTP request/session. A
            # short-lived control session avoids both a stale ORM identity and
            # an open SQLite read transaction while a hardware operation is in
            # flight. The operation already in flight is allowed to finish and
            # have its evidence committed; no following operation is started.
            if self._cancellation_requested(job.job_id):
                raise SetupCancellationRequested(
                    "Setup wurde auf Wunsch an einer sicheren Schrittgrenze abgebrochen. "
                    "Der zuletzt gestartete Einzelschritt wurde dokumentiert; es wurde "
                    "kein weiterer Gerätebefehl gesendet."
                )

        async def transition_only(target: SetupState) -> None:
            cancellation_checkpoint()
            self.repository.transition_state(db, row, target)
            job.current_state = target.value
            update_progress(target)
            db.commit()
            cancellation_checkpoint()

        async def step(target: SetupState, callback, *, field: str = "") -> dict[str, Any]:
            cancellation_checkpoint()
            result = await callback(row)
            self.repository.transition_state(db, row, target)
            job.current_state = target.value
            update_progress(target)
            if field and field in result:
                setattr(row, field, result[field])
            if "ssh_status" in result:
                row.ssh_status = str(result["ssh_status"])
            if "profile_key" in result:
                row.ssh_profile_key = str(result["profile_key"])
            if "routing_status" in result:
                row.routing_status = str(result["routing_status"])
            if result:
                row.evidence_json = json.dumps(
                    {**self._load_json(row.evidence_json), **self._public_result(result)}
                )
            row.updated_at = datetime.now(UTC)
            db.commit()
            cancellation_checkpoint()
            return result

        try:
            plan = self._load_json(job.plan_json)
            options = plan.get("options") if isinstance(plan.get("options"), dict) else {}
            ssh_required = bool(options.get("ssh_required", False))
            pair_account = bool(options.get("pair_account", True))
            playback_test = bool(options.get("playback_test", True))
            identity = await step(SetupState.DISCOVERED, adapter.identify)
            if identity.get("verified") is False:
                raise RuntimeError("Die Radio-Identität konnte nicht bestätigt werden.")
            await transition_only(SetupState.IDENTIFIED)
            await transition_only(SetupState.BACKUP_PENDING)
            backup = await step(SetupState.BACKUP_COMPLETE, adapter.backup)
            if backup.get("verified") is False:
                raise RuntimeError("Das Backup ist unvollständig; es wurde nichts geschrieben.")
            row.backup_path = str(backup.get("backup_path") or "")
            row.backup_sha256_json = json.dumps(backup.get("sha256") or {})
            db.commit()
            if ssh_required:
                await transition_only(SetupState.SSH_STATUS_PENDING)
                status = await step(SetupState.SSH_STATUS_PENDING, adapter.ssh_status)
                row.ssh_status = str(status.get("ssh_status") or "SSH_UNKNOWN")
                row.ssh_profile_key = str(status.get("profile_key") or "")
                db.commit()
                if status.get("already_active"):
                    await transition_only(SetupState.SSH_ALREADY_ACTIVE)
                else:
                    await transition_only(SetupState.SSH_ACTIVATION_PENDING)
                    await step(SetupState.SSH_TEMPORARY_ACTIVE, adapter.activate_ssh)
                await step(SetupState.SSH_PERSISTENCE_PENDING, adapter.persist_ssh)
                reboot = await step(SetupState.SSH_REBOOT_PENDING, adapter.reboot_verify_ssh)
                verified = await step(SetupState.SSH_VERIFIED, lambda _row: asyncio.sleep(0, result=reboot))
                if verified.get("verified") is False:
                    raise RuntimeError("SSH konnte nach dem Neustart nicht bestätigt werden.")
            else:
                row.ssh_status = "NOT_REQUIRED"
                db.commit()
            routing_backup = await step(SetupState.ROUTING_BACKUP_COMPLETE, adapter.backup_routing)
            if routing_backup.get("routing_backup") is False:
                raise RuntimeError("Die bisherigen Serverziele konnten nicht sicher gesichert werden.")
            target_data = self._load_json(job.target_server_json)
            target_ports = target_data.get("ports") or {}
            target = ServerTarget(
                host=str(target_data.get("host") or ""),
                web_port=int(target_ports.get("web") or 0),
                cloud_port=int(target_ports.get("cloud") or 0),
                debug_port=int(target_ports.get("debug") or 0),
            )
            await transition_only(SetupState.BASSWIESN_ROUTE_PENDING)
            await step(SetupState.BASSWIESN_ROUTE_ACTIVE, lambda _row: adapter.route(_row, target))
            await transition_only(SetupState.RADIO_REBOOT_PENDING)
            reboot = await step(SetupState.RADIO_REBOOTED, adapter.reboot)
            if reboot.get("reboot_requested") is False:
                raise RuntimeError("Der kontrollierte Radio-Neustart wurde nicht bestätigt.")
            await transition_only(SetupState.RADIO_RECONNECT_PENDING)
            reconnect = await adapter.reconnect(row)
            reachable = await step(SetupState.RADIO_REACHABLE, lambda _row: asyncio.sleep(0, result=reconnect))
            if reachable.get("reachable") is False:
                raise RuntimeError("Das Radio war nach der Umstellung nicht wieder erreichbar.")
            if pair_account:
                await transition_only(SetupState.MARGE_ACCOUNT_PENDING)
                account = await step(
                    SetupState.MARGE_ACCOUNT_ACTIVE,
                    lambda _row: adapter.pair_account(_row, target),
                )
                if account.get("account_paired") is False:
                    raise RuntimeError("Das lokale Radio-Konto konnte nicht bestätigt werden.")
            await step(SetupState.PRESETS_READABLE, adapter.read_presets)
            if playback_test:
                playback = await step(
                    SetupState.PLAYBACK_READY,
                    lambda _row: adapter.playback_test(_row, target),
                )
                if playback.get("playback_ready") is False:
                    raise RuntimeError("Die Wiedergabeprüfung bei Lautstärke 1 war nicht erfolgreich.")
            await step(
                SetupState.VERIFIED,
                lambda _row: asyncio.sleep(
                    0,
                    result={
                        "verified": True,
                        "playback_test": "passed" if playback_test else "not_requested",
                        "ssh": "used" if ssh_required else "not_required",
                    },
                ),
            )
            if not bool(plan.get("dry_run", False)):
                record_action(
                    db,
                    job_id=job.job_id,
                    device_id=row.device_id,
                    ip_address=row.ip_address,
                    action="setup_rebuild",
                    trigger="webui",
                    phase=SetupState.VERIFIED.value,
                    requested_state={
                        "target": target.to_public_dict(),
                        "pair_account": pair_account,
                        "playback_test": playback_test,
                        "ssh_required": ssh_required,
                    },
                    backup_ref=row.backup_path,
                    result="setup_readback_verified",
                    readback=self._load_json(row.evidence_json),
                    rollback_ref=f"setup-job:{job.job_id}:routing-rollback",
                    verified=True,
                )
                db.commit()
        except SetupCancellationRequested as exc:
            cancelled_at = datetime.now(UTC)
            row.last_error = str(exc)[:1000]
            row.recovery_status = "CANCELLED_AT_CHECKPOINT"
            if row.state != SetupState.VERIFIED.value:
                try:
                    self.repository.transition_state(
                        db,
                        row,
                        SetupState.FAILED,
                        error=row.last_error,
                    )
                except (TypeError, ValueError):
                    row.state = SetupState.FAILED.value
                    row.updated_at = cancelled_at
            job.status = "cancelled"
            job.current_state = row.state
            job.error = row.last_error
            job.ended_at = cancelled_at
            job.updated_at = cancelled_at
            db.commit()
        except Exception as exc:
            row.last_error = str(exc)[:1000]
            if row.last_error.startswith("VOLUME_SAFETY_LOCK"):
                evidence = self._load_json(row.evidence_json)
                evidence.update(
                    {
                        "audio_test_locked": True,
                        "audio_lock_reason": row.last_error[:500],
                        "audio_lock_volume_limit": 1,
                        "audio_lock_recovery": "manual review after STOP/STANDBY and volume-1 readback",
                    }
                )
                row.evidence_json = json.dumps(evidence, ensure_ascii=False, sort_keys=True)
            if row.state != SetupState.FAILED.value:
                try:
                    self.repository.transition_state(db, row, SetupState.FAILED, error=row.last_error)
                except ValueError:
                    row.state = SetupState.FAILED.value
            job.current_state = SetupState.FAILED.value
            job.error = row.last_error
            if not bool(plan.get("dry_run", False)):
                record_action(
                    db,
                    job_id=job.job_id,
                    device_id=row.device_id,
                    ip_address=row.ip_address,
                    action="setup_rebuild",
                    trigger="webui",
                    phase=SetupState.FAILED.value,
                    requested_state={"options": options},
                    backup_ref=row.backup_path,
                    result="setup_not_verified",
                    readback=self._load_json(row.evidence_json),
                    rollback_ref=(
                        f"setup-job:{job.job_id}:routing-rollback"
                        if row.backup_path
                        else ""
                    ),
                    error_category=exc.__class__.__name__,
                    verified=False,
                )
            db.commit()

    def _cancellation_requested(self, job_id: str) -> bool:
        """Read the cancellation flag without relying on a long-lived job row."""

        control_db = self.session_factory()
        try:
            control_job = self.repository.job(control_db, job_id)
            return bool(control_job and control_job.cancel_requested)
        finally:
            control_db.close()

    @staticmethod
    def _load_json(value: str) -> dict[str, Any]:
        try:
            loaded = json.loads(value or "{}")
            return loaded if isinstance(loaded, dict) else {}
        except ValueError:
            return {}

    @staticmethod
    def _public_result(value: dict[str, Any]) -> dict[str, Any]:
        forbidden = ("password", "secret", "token", "private_key", "command")
        return {
            key: item
            for key, item in value.items()
            if not any(part in key.lower() for part in forbidden)
        }

    async def rollback(self, job_id: str) -> dict[str, Any]:
        db = self.session_factory()
        try:
            job = self.repository.job(db, job_id)
            if job is None:
                raise ValueError("setup rebuild job not found")
            plan = self._load_json(job.plan_json)
            if bool(plan.get("dry_run", False)):
                adapter: SetupDeviceAdapter = DryRunAdapter(delay_seconds=0.05)
            else:
                from basswiesn.app.services.setup_rebuild.radio_adapter import RadioSetupAdapter

                adapter = self.adapter or RadioSetupAdapter()
            if not self.repository.acquire_lease(db, owner_id=self.owner_id, job_id=job_id):
                raise RuntimeError("another setup rebuild is already active")
            rollback_ok = True
            rollback_full = True
            rollback_attempted = False
            for row in self.repository.states(db, job_id):
                if row.state not in {SetupState.VERIFIED.value, SetupState.FAILED.value}:
                    continue
                if not row.backup_path:
                    row.recovery_status = "NO_WRITE_NO_ROLLBACK_REQUIRED"
                    continue
                rollback_attempted = True
                self.repository.transition_state(db, row, SetupState.ROLLBACK_PENDING)
                try:
                    result = await adapter.rollback(row)
                    step_ok = bool(result.get("rolled_back"))
                    step_full = bool(result.get("fully_restored"))
                    row.recovery_status = (
                        "ROLLED_BACK"
                        if step_ok and step_full
                        else "ROUTING_ROLLBACK_VERIFIED_PERSISTENCE_OPEN"
                        if step_ok
                        else "ROLLBACK_FAILED"
                    )
                    if result:
                        row.evidence_json = json.dumps(
                            {
                                **self._load_json(row.evidence_json),
                                "rollback": self._public_result(result),
                            }
                        )
                    self.repository.transition_state(
                        db,
                        row,
                        SetupState.ROLLED_BACK if step_ok else SetupState.FAILED,
                        error="" if step_ok else "rollback failed",
                    )
                    if not bool(plan.get("dry_run", False)):
                        record_action(
                            db,
                            job_id=job.job_id,
                            device_id=row.device_id,
                            ip_address=row.ip_address,
                            action="setup_rebuild_rollback",
                            trigger="webui",
                            phase=row.state,
                            requested_state={"scope": "ROUTING_ONLY"},
                            backup_ref=row.backup_path,
                            result=(
                                "rollback_fully_verified"
                                if step_ok and step_full
                                else "routing_readback_verified_persistence_open"
                                if step_ok
                                else "rollback_not_verified"
                            ),
                            readback=self._public_result(result),
                            rollback_ref=row.backup_path,
                            verified=step_ok and step_full,
                        )
                    rollback_ok = rollback_ok and step_ok
                    rollback_full = rollback_full and step_ok and step_full
                except Exception as exc:
                    rollback_ok = False
                    row.recovery_status = "ROLLBACK_FAILED"
                    row.last_error = str(exc)[:1000]
                    self.repository.transition_state(db, row, SetupState.FAILED, error=row.last_error)
            job.status = (
                "rolled_back"
                if rollback_attempted and rollback_ok and rollback_full
                else "rollback_limited"
                if rollback_attempted and rollback_ok
                else "rollback_not_required"
                if not rollback_attempted
                else "rollback_failed"
            )
            job.current_state = (
                SetupState.ROLLED_BACK.value if rollback_ok else SetupState.FAILED.value
            )
            job.ended_at = datetime.now(UTC)
            db.commit()
            return self.repository.public_job(db, job)
        finally:
            self.repository.release_lease(db, owner_id=self.owner_id, job_id=job_id)
            db.close()


_COORDINATOR: SetupCoordinator | None = None


def get_coordinator() -> SetupCoordinator:
    global _COORDINATOR
    if _COORDINATOR is None:
        _COORDINATOR = SetupCoordinator()
    return _COORDINATOR
