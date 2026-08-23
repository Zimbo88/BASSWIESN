"""SQLAlchemy repositories for SoundTouch device persistence."""

from sqlalchemy.orm import Session

from basswiesn.app.db.models import (
    ConfigBackup,
    Device,
    MultiroomScenario,
    PlayHistory,
    Preset,
    ReferenceSetup,
    ScheduledAction,
    SetupPlan,
    TelemetryEvent,
)


class DeviceRepository:
    def __init__(self, session: Session) -> None:
        self.session = session

    def get_by_device_id(self, device_id: str) -> Device | None:
        return self.session.query(Device).filter(Device.device_id == device_id).one_or_none()

    def list_by_name(self) -> list[Device]:
        return self.session.query(Device).order_by(Device.name).all()

    def get_latest_by_ip(self, ip_address: str) -> Device | None:
        return (
            self.session.query(Device)
            .filter(Device.ip_address == ip_address)
            .order_by(Device.last_seen.desc())
            .first()
        )

    def add(self, device: Device) -> Device:
        self.session.add(device)
        return device

    def commit(self) -> None:
        self.session.commit()


class DeviceIdentityRepository:
    def __init__(self, session: Session) -> None:
        self.session = session

    def get_by_device_id(self, device_id: str) -> Device | None:
        return self.session.query(Device).filter(Device.device_id == device_id).one_or_none()

    def merge(self, source: Device, target: Device) -> dict:
        """Merge an IP-based device row and all known references into target."""

        if source.id == target.id:
            return {"merged": False}
        old, new = source.device_id, target.device_id
        preset_count = 0
        for preset in self.session.query(Preset).filter(Preset.device_id == old).all():
            existing = (
                self.session.query(Preset)
                .filter(Preset.device_id == new, Preset.button == preset.button)
                .one_or_none()
            )
            if existing is None:
                preset.device_id = new
            else:
                if not existing.station_id and preset.station_id:
                    existing.station_id = preset.station_id
                    existing.source = preset.source
                    existing.source_account = preset.source_account
                    existing.location = preset.location
                    existing.content_item_xml = preset.content_item_xml
                self.session.delete(preset)
            preset_count += 1
        counts = {
            "presets": preset_count,
            "setup_plans": self._update_text_ids(SetupPlan, "device_id", old, new),
            "play_history": self._update_text_ids(PlayHistory, "device_id", old, new),
            "telemetry_events": self._update_text_ids(TelemetryEvent, "device_id", old, new),
            "config_backups": self._update_text_ids(ConfigBackup, "device_id", old, new),
            "reference_setups": self._update_text_ids(ReferenceSetup, "source_device_id", old, new),
            "multiroom_master": self._update_text_ids(MultiroomScenario, "master_device_id", old, new),
            "multiroom_trigger": self._update_text_ids(MultiroomScenario, "trigger_device_id", old, new),
            "scheduled_multiroom_master": self._update_text_ids(ScheduledAction, "multiroom_master_id", old, new),
            "play_history_zone_master": self._update_text_ids(PlayHistory, "zone_master_id", old, new),
            "multiroom_members": self._update_csv_ids(MultiroomScenario, "member_device_ids", old, new),
            "scheduled_devices": self._update_csv_ids(ScheduledAction, "device_ids", old, new),
            "scheduled_multiroom_members": self._update_csv_ids(ScheduledAction, "multiroom_member_ids", old, new),
            "play_history_zone_members": self._update_csv_ids(PlayHistory, "zone_member_ids", old, new),
        }
        for field in ("name", "model", "ip_address", "firmware", "capabilities_xml", "info_xml"):
            value = getattr(source, field)
            if value and not getattr(target, field):
                setattr(target, field, value)
        if source.last_seen and (not target.last_seen or source.last_seen > target.last_seen):
            target.last_seen = source.last_seen
        self.session.delete(source)
        return {
            "merged": True,
            "old_device_id": old,
            "new_device_id": new,
            "updated_rows": counts,
        }

    def _update_text_ids(self, model, column_name: str, old: str, new: str) -> int:
        rows = self.session.query(model).filter(getattr(model, column_name) == old).all()
        for row in rows:
            setattr(row, column_name, new)
        return len(rows)

    def _update_csv_ids(self, model, column_name: str, old: str, new: str) -> int:
        changed = 0
        for row in self.session.query(model).all():
            parts = [part.strip() for part in (getattr(row, column_name) or "").split(",") if part.strip()]
            replaced: list[str] = []
            did_change = False
            for part in parts:
                candidate = new if part == old else part
                did_change = did_change or part == old
                if candidate not in replaced:
                    replaced.append(candidate)
            if did_change:
                setattr(row, column_name, ",".join(replaced))
                changed += 1
        return changed
