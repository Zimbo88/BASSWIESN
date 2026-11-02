"""Normal setup must never accidentally execute firmware operations."""
import asyncio
import json

import pytest

from basswiesn.app import db as app_db
from basswiesn.app.models import Setting
from basswiesn.app.services.radio_patch_policy import presentation_supported
from basswiesn.app.services.setup_rebuild import presentation_preferences as prefs
from basswiesn.app.services.setup_rebuild.server_target import ServerTarget
from test_setup_presentation import rig  # noqa: F401 — shared offline radio fixture
from test_setup_rebuild_160 import RecordingAdapter
from test_setup_transaction_ownership import prepared


def test_normal_sequence_never_calls_firmware_operations():
    adapter = RecordingAdapter()
    coordinator, job = prepared(adapter)
    result = asyncio.run(coordinator.execute(job))
    assert result["status"] == "completed"
    assert adapter.calls == ["identify", "backup", "backup_routing", "route", "reboot",
                             "reconnect", "configure_defaults", "pair_account", "read_presets", "playback_test"]
    evidence = result["devices"][0]["evidence"]
    assert evidence["presentation_mode"] == "normal"
    assert evidence["radio_defaults_verified"] is True
    assert "presentation_verified" not in evidence


def test_normal_defaults_failure_cannot_be_called_successful():
    class Failed(RecordingAdapter):
        async def configure_defaults(self, row):
            return {"radio_defaults_verified": False}
    adapter = Failed()
    coordinator, job = prepared(adapter)
    result = asyncio.run(coordinator.execute(job))
    assert result["status"] == "failed"
    assert "pair_account" not in adapter.calls
    assert "playback_test" not in adapter.calls


@pytest.mark.parametrize("mode", [None, "", "enabled", True, 1, [], {}])
def test_unrecognized_mode_never_creates_job(mode):
    coordinator, _ = prepared(RecordingAdapter())
    with pytest.raises(ValueError):
        coordinator.preview(device_ids=["AA11BB22CC33"],
                            target=ServerTarget("192.0.2.10", 1328, 1516, 1860),
                            options={"presentation_mode": mode})


def test_st10_never_inherits_display_support_from_firmware():
    assert not presentation_supported("SoundTouch 10", "ginger", "sm2", "27.0.6.46330.5043500")
    assert not presentation_supported("SoundTouch 20", "spotty", "sm2", "27.0.7")
    assert presentation_supported("SoundTouch 20", "spotty", "sm2", "27.0.6.46330.5043500 build")


@pytest.mark.parametrize("no_display", [True, False])
def test_normal_preferences_preserve_display_and_restore_language(rig, no_display):
    service, row, _, client = rig
    original = '<sysLanguage>3</sysLanguage>'
    if no_display:
        service.adapter.identify.return_value = {"model": "SoundTouch 10"}
        del client.radio["/clockDisplay"]  # any clock read/write fails this test
    key = "radio_display:" + row.device_id
    with app_db.SessionLocal() as db:
        db.add(Setting(key="web_language", value="de"))
        db.add(Setting(key=key, value='{"mode":"EXISTING"}'))
        db.commit()
    asyncio.run(prefs.configure(service, row, patched=False))
    assert client.radio["/language"] == '<sysLanguage>2</sysLanguage>'
    saved = json.loads((service.root(row) / "preferences.json").read_text())
    assert saved["before"] == saved["desired"]
    assert (saved["clock_before"] is None) == no_display
    asyncio.run(prefs.configure(service, row, restore=True))
    assert client.radio["/language"] == original
    with app_db.SessionLocal() as db:
        assert db.query(Setting).filter_by(key=key).one().value == '{"mode":"EXISTING"}'


def test_plan_exposes_the_difference_without_claiming_hardware():
    coordinator, _ = prepared(RecordingAdapter())
    args = dict(device_ids=["AA11BB22CC33"], target=ServerTarget("192.0.2.10", 1328, 1516, 1860))
    normal = coordinator.preview(**args)
    patched = coordinator.preview(**args, options={"presentation_mode": "patched"})
    assert normal["firmware_files_changed"] is False and normal["ssh_required"] is False
    assert normal["presentation_required"] == []
    assert patched["firmware_files_changed"] is True and patched["ssh_required"] is True
    assert patched["presentation_required"] == ["display", "bootlogo"]
    assert normal["existing_patches"] == patched["existing_patches"] == "PRESERVE"
