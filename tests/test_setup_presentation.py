"""Offline transaction failures. Fixtures contain no manufacturer firmware."""
import asyncio
from dataclasses import replace
import io
import json
from pathlib import Path
from types import SimpleNamespace
import tarfile
from unittest.mock import AsyncMock

import pytest

from basswiesn.app import db as app_db
from basswiesn.app.config import get_settings
from basswiesn.app.models import Setting, SetupRebuildJob
from basswiesn.app.services import radio_patch_policy as policy
from basswiesn.app.services.setup_rebuild import presentation as service
from basswiesn.app.services.setup_rebuild import presentation_model as m
from basswiesn.app.services.setup_rebuild import presentation_preferences as prefs
from basswiesn.app.services.setup_rebuild import presentation_transport as wire
from basswiesn.app.services.setup_rebuild.backup import write_artifact
from basswiesn.app.services.setup_rebuild.ssh_runner import SshConfig

IDENTITY = "AA11BB22CC33"
TARGET = m.Target(IDENTITY, "spotty", "SoundTouch 20", "sm2", policy.FIRMWARE)


def snapshot(**kw):
    return dict(boot="boot-one", app=policy.APP_STOCK, battery=policy.BATTERY_STOCK,
                splash_a=policy.SPLASH_STOCK, splash_b=policy.SPLASH_STOCK, rc="absent",
                root_ro="yes", base="none", control="absent", rc_installed="absent",
                healthy="none", pending="no", bind_ro="no", space="100000", root_space="10000") | kw


def archive(files):
    output = io.BytesIO()
    with tarfile.open(fileobj=output, mode="w") as tar:
        for name, raw in files.items():
            entry = tarfile.TarInfo(name)
            entry.size = len(raw)
            tar.addfile(entry, io.BytesIO(raw))
    return output.getvalue()


@pytest.mark.parametrize("change", [dict(identity="bad;exit"), dict(firmware=""), dict(firmware="27.0.7"), dict(platform="unknown"), dict(protected=(IDENTITY,)), dict(protected=("bad;exit",))])
def test_target_is_exact_and_protected(change):
    with pytest.raises(m.PresentationError):
        replace(TARGET, **change).validate()


@pytest.mark.parametrize("kw", [dict(app="0"*64), dict(rc="0"*64), dict(base=m.BASE), dict(root_space="12"), dict(space="12")])
def test_unknown_image_startup_and_space_fail_closed(kw):
    with pytest.raises(m.PresentationError):
        m.classify(TARGET, snapshot(**kw))


def test_battery_variants_and_legacy_application_are_preserved():
    for battery in (policy.BATTERY_STOCK, policy.BATTERY_PATCHED, policy.BATTERY_LEGACY):
        for app in (policy.APP_STOCK, policy.APP_LEGACY):
            assert m.classify(TARGET, snapshot(battery=battery, app=app)) == "install"


@pytest.mark.parametrize("app", sorted(policy.ADOPTABLE_DISPLAY_IMAGES))
def test_existing_install_needs_offdevice_approval_and_guard(app):
    snap = snapshot(app=app, splash_a=policy.SPLASH_LOGO, splash_b=policy.SPLASH_LOGO,
                    base=m.BASE, control="a"*64, rc="b"*64, rc_installed="b"*64, bind_ro="yes", healthy="boot-one")
    receipt = dict(identity=IDENTITY, verified=True, control="a"*64, rc="b"*64, app=app)
    assert m.classify(TARGET, snap, receipt) == "existing"
    for bad in ({}, receipt | {"identity": "BB11BB22CC33"}, receipt | {"verified": False}):
        with pytest.raises(m.PresentationError):
            m.classify(TARGET, snap, bad)
    with pytest.raises(m.PresentationError):
        m.classify(TARGET, snap | {"pending": "yes"}, receipt)


def test_shipped_recipe_and_catalogue_agree_without_manufacturer_fixture():
    spec = m.recipe()["app"]
    assert spec["results"] == {policy.APP_STOCK: policy.APP_DISPLAY,
                               policy.APP_LEGACY: policy.APP_COMBINED}
    assert m.digest(bytes.fromhex(spec["payload_hex"])) == spec["payload_sha256"]
    assert policy.APP_DISPLAY_ACCEPTED in policy.ADOPTABLE_DISPLAY_IMAGES
    assert policy.APP_COMBINED_ACCEPTED in policy.ADOPTABLE_DISPLAY_IMAGES
    assert policy.APP_DISPLAY_ACCEPTED != policy.APP_DISPLAY
    # New role labels use the existing local event callback; no network transport.
    zone_offset = 0x454C64 - 0x8000
    assert any(p["offset"] == zone_offset and p["before"] == "b0b582b0"
               for p in spec["changes"])


@pytest.mark.parametrize("name", ["../escape", "/opt/Bose/BoseApp", "mnt/nv/password", "unknown"])
def test_backup_archive_rejects_unexpected_paths(name):
    with pytest.raises(m.PresentationError):
        m.unpack_backup(archive({name: b"synthetic"}))


def test_backup_rejects_symlinks_and_duplicate_entries():
    out = io.BytesIO()
    with tarfile.open(fileobj=out, mode="w") as tar:
        node = tarfile.TarInfo(m.FILES[0]); node.type = tarfile.SYMTYPE; node.linkname = "/etc/passwd"
        tar.addfile(node)
    with pytest.raises(m.PresentationError):
        m.unpack_backup(out.getvalue())
    with pytest.raises(m.PresentationError):
        m.unpack_backup(archive({m.FILES[0]: b"synthetic"}))


def test_private_backups_no_symlink_and_bounded(tmp_path):
    write_artifact(tmp_path, "image.private.tar", b"synthetic")
    assert service.read_private(tmp_path / "image.private.tar") == b"synthetic"
    (tmp_path / "linked").symlink_to(tmp_path / "image.private.tar")
    with pytest.raises(OSError):
        service.read_private(tmp_path / "linked")
    with pytest.raises(m.PresentationError):
        service.read_private(tmp_path / "image.private.tar", 1)
    with pytest.raises(ValueError):
        write_artifact(tmp_path, "large", b"xx", max_bytes=1)


@pytest.fixture
def rig(tmp_path, monkeypatch):
    monkeypatch.setenv("BASSWIESN_DATA_DIR", str(tmp_path))
    get_settings.cache_clear()
    # Patch the settings accessor explicitly so no developer data path is used.
    settings = SimpleNamespace(data_dir=tmp_path)
    monkeypatch.setattr(service, "get_settings", lambda: settings)
    facts = dict(model=TARGET.model, variant=TARGET.variant, platform=TARGET.platform, firmware=TARGET.firmware)
    radio = {"/clockDisplay": '<clockDisplay><clockConfig userEnable="true" brightness="3"/></clockDisplay>',
             "/language": '<sysLanguage>3</sysLanguage>'}
    async def get_xml(path):
        return radio[path]
    async def post_xml(path, body):
        radio[path] = body
    client = SimpleNamespace(get_xml=AsyncMock(side_effect=get_xml), post_xml=AsyncMock(side_effect=post_xml), radio=radio)
    adapter = SimpleNamespace(identify=AsyncMock(return_value=facts), ssh_config=None, _client=lambda row: client)
    row = SimpleNamespace(device_id=IDENTITY, ip_address="192.0.2.42", backup_path=str(tmp_path / "setup-rebuild" / "run" / IDENTITY / "baseline"))
    instance = service.SetupPresentation(adapter)
    class Transport:
        target = TARGET
        def __init__(self):
            self.snap = snapshot()
            self.calls = []
            self.fail = ""
        async def exchange(self, operation, **kwargs):
            self.calls.append(operation)
            if operation == self.fail:
                raise m.PresentationError("LOST_REPLY")
            if operation == "inspect":
                return "".join(f"{k}={v}\n" for k,v in self.snap.items()).encode()
            if operation == "stage":
                return f'{policy.SPLASH_STOCK}  splash.original\n'.encode()
            return b""
    transport = Transport()
    monkeypatch.setattr(instance, "transport", AsyncMock(return_value=transport))
    backup = write_artifact(instance.root(row), "firmware.private.tar", b"private-fixture")
    candidate = write_artifact(instance.root(row), "candidate.private.tar", b"authored-fixture")
    value = dict(identity=IDENTITY, mode="install", before=snapshot(), backup_sha256=backup.sha256,
                 package_sha256=candidate.sha256,
                 hashes={"control.sh": "a"*64, "rc.installed": "b"*64, "BoseApp.candidate": policy.APP_DISPLAY, "splash.candidate": policy.SPLASH_LOGO})
    instance.save(row, value, "PREPARED")
    return instance, row, transport, client


def booted(rig):
    _, _, transport, _ = rig
    transport.snap.update(boot="boot-two", app=policy.APP_DISPLAY, splash_a=policy.SPLASH_LOGO, splash_b=policy.SPLASH_LOGO,
                          base=m.BASE, control="a"*64, rc="b"*64, rc_installed="b"*64, bind_ro="yes", healthy="boot-two")


def test_install_is_not_verified_until_new_boot_and_guard(rig):
    instance, row, transport, _ = rig
    async def run():
        result = await instance.activate(row)
        assert result["presentation_activation"] == "cold_boot_pending"
        assert "presentation_verified" not in result
        with pytest.raises(m.PresentationError, match="NEW_BOOT"):
            await instance.verify(row)
        booted(rig)
        result = await instance.verify(row)
        assert result["presentation_verified"] is True
        assert result["presentation_zone_labels"] == "INSTALLED_VISUAL_ACCEPTANCE_REQUIRED"
        assert instance.state(row)["phase"] == "VERIFIED"
    asyncio.run(run())
    assert transport.calls.count("activate") == 1


@pytest.mark.parametrize("app", [policy.APP_DISPLAY_ACCEPTED, policy.APP_COMBINED_ACCEPTED])
def test_adopting_accepted_layout_never_claims_new_zone_labels(rig, app):
    instance, row, transport, _ = rig
    value = instance.state(row)
    value.update(mode="existing", hashes={}, activation_boot="boot-one")
    value["before"].update(app=app, base=m.BASE, control="a"*64, rc="b"*64)
    instance.save(row, value, "ADOPTED_REBOOT_REQUIRED")
    booted(rig)
    transport.snap["app"] = app
    result = asyncio.run(instance.verify(row))
    assert result["presentation_verified"] is True
    assert result["presentation_zone_labels"] == "NOT_INSTALLED_EXISTING_LAYOUT_PRESERVED"
    assert transport.calls == ["inspect"]


@pytest.mark.parametrize("operation,phase", [("stage","STAGE_INTENT"), ("receipt","RECEIPT_INTENT"), ("activate","ACTIVATION_INTENT")])
def test_lost_write_reply_is_not_replayed(rig, operation, phase):
    instance, row, transport, _ = rig
    transport.fail = operation
    async def run():
        with pytest.raises(m.PresentationError, match="LOST_REPLY"):
            await instance.activate(row)
        assert instance.state(row)["phase"] == phase
        with pytest.raises(m.PresentationError, match="REPLAY"):
            await instance.activate(row)
    asyncio.run(run())
    assert transport.calls.count(operation) == 1


def test_corrupt_offdevice_backup_stops_before_any_write(rig):
    instance, row, transport, _ = rig
    write_artifact(instance.root(row), "firmware.private.tar", b"changed")
    with pytest.raises(m.PresentationError, match="BACKUP_CORRUPT"):
        asyncio.run(instance.activate(row))
    assert transport.calls == ["inspect"]


@pytest.mark.parametrize("field,value", [("battery",policy.BATTERY_PATCHED), ("app",policy.APP_STOCK), ("bind_ro","no"), ("rc","c"*64), ("splash_b",policy.SPLASH_STOCK)])
def test_runtime_divergence_never_reports_success(rig, field, value):
    instance, row, transport, _ = rig
    asyncio.run(instance.activate(row))
    booted(rig)
    transport.snap[field] = value
    with pytest.raises(m.PresentationError, match="READBACK_MISMATCH"):
        asyncio.run(instance.verify(row))
    assert instance.state(row)["phase"] != "VERIFIED"


def test_owned_rollback_requires_actual_original_after_new_boot(rig):
    instance, row, transport, _ = rig
    async def run():
        await instance.activate(row)
        booted(rig)
        await instance.verify(row)
        assert await instance.undo(row)
        with pytest.raises(m.PresentationError, match="REBOOT_UNCONFIRMED"):
            await instance.verify_undo(row)
        transport.snap = snapshot(boot="boot-three", base=m.BASE, control="a"*64)
        await instance.verify_undo(row)
        assert instance.state(row)["phase"] == "ROLLED_BACK"
    asyncio.run(run())


def test_preferences_preserve_clock_attributes_and_restore_absent_settings(rig):
    instance, row, _, client = rig
    asyncio.run(prefs.configure(instance, row))
    client.post_xml.assert_not_awaited()
    with app_db.SessionLocal() as db:
        assert json.loads(db.query(Setting).filter(Setting.key=="radio_display:"+IDENTITY).one().value)["mode"] == "TRACK_ARTIST"
    asyncio.run(prefs.configure(instance, row, restore=True))
    with app_db.SessionLocal() as db:
        assert db.query(Setting).filter(Setting.key=="radio_display:"+IDENTITY).one_or_none() is None


def test_rollback_lost_reply_is_observed_not_replayed(rig):
    instance, row, transport, _ = rig
    async def run():
        await instance.activate(row)
        booted(rig)
        await instance.verify(row)
        transport.fail = "undo"
        with pytest.raises(m.PresentationError, match="LOST_REPLY"):
            await instance.undo(row)
        transport.fail = ""
        with pytest.raises(m.PresentationError, match="UNCERTAIN_MANUAL_REVIEW"):
            await instance.undo(row)
        # Firmware rollback did in fact finish before its reply was lost.
        transport.snap.update(rc="absent", splash_a=policy.SPLASH_STOCK, splash_b=policy.SPLASH_STOCK)
        assert await instance.undo(row)  # full reboot still required
        assert transport.calls.count("undo") == 1
    asyncio.run(run())


def test_clock_write_reply_lost_but_readback_is_authoritative(rig):
    instance, row, _, client = rig
    client.radio["/clockDisplay"] = '<clockDisplay><clockConfig userEnable="false" brightness="3"/></clockDisplay>'
    async def lost(path, body):
        client.radio[path] = '<clockDisplay><clockConfig userEnable="true" brightness="3"/></clockDisplay>'
        raise TimeoutError()
    client.post_xml.side_effect = lost
    asyncio.run(prefs.configure(instance, row))
    client.post_xml.assert_awaited_once()
    assert 'userEnable="true"' in client.post_xml.call_args.args[1]
    assert "brightness" not in client.post_xml.call_args.args[1]


@pytest.mark.parametrize("locale,expected", [("de-DE", "de"), ("zh-Hant", "zh_hant"), ("zh", "zh_hans"),
                                              ("zh_CN.UTF-8", "zh_hans"), ("nb", "no"), ("uk", "en"), ("", "en")])
def test_setup_maps_system_language_without_guessing_firmware_ids(locale, expected):
    from basswiesn.app.services.setup_rebuild.defaults import radio_language
    assert radio_language(locale) == expected


def test_setup_initializes_factory_language_and_restores_exact_previous_value(rig):
    instance, row, _, client = rig
    client.radio["/language"] = '<sysLanguage>0</sysLanguage>'
    with app_db.SessionLocal() as db:
        db.add(Setting(key="web_language", value="de"))
        db.add(Setting(key="device_language_default", value="fr"))
        db.commit()
    asyncio.run(prefs.configure(instance, row))
    assert client.radio["/language"] == '<sysLanguage>2</sysLanguage>'
    saved = json.loads((instance.root(row) / "preferences.json").read_text())
    assert saved["language_before"] == 0 and saved["language_desired"] == 2
    assert saved["setup_defaults"]["clockDisplay"] is True
    asyncio.run(prefs.configure(instance, row, restore=True))
    assert client.radio["/language"] == '<sysLanguage>0</sysLanguage>'


@pytest.mark.parametrize("changed", [True, False])
def test_setup_language_lost_reply_uses_readback_not_retry(rig, changed):
    instance, row, _, client = rig
    client.radio["/language"] = '<sysLanguage>0</sysLanguage>'
    async def lost(path, body):
        if changed:
            client.radio[path] = body
        raise TimeoutError()
    client.post_xml.side_effect = lost
    if changed:
        asyncio.run(prefs.configure(instance, row))
    else:
        with pytest.raises(m.PresentationError, match="LANGUAGE_READBACK_MISMATCH"):
            asyncio.run(prefs.configure(instance, row))
        with app_db.SessionLocal() as db:
            assert db.query(Setting).filter(Setting.key=="radio_display:"+IDENTITY).one_or_none() is None
    client.post_xml.assert_awaited_once()


def test_setup_language_concurrent_change_and_unknown_schema_block_writes(rig):
    instance, row, _, client = rig
    async def concurrent(path):
        if path == "/language":
            value = client.radio[path]
            client.radio[path] = '<sysLanguage>5</sysLanguage>'
            return value
        return client.radio[path]
    client.radio["/language"] = '<sysLanguage>0</sysLanguage>'
    client.get_xml.side_effect = concurrent
    with pytest.raises(m.PresentationError, match="LANGUAGE_DIVERGED"):
        asyncio.run(prefs.configure(instance, row))
    client.post_xml.assert_not_awaited()
    for bad in ("<language>2</language>", "<sysLanguage>999</sysLanguage>", "<sysLanguage/>"):
        with pytest.raises(m.PresentationError, match="LANGUAGE_SCHEMA_UNKNOWN"):
            prefs.language(bad)


def test_multi_radio_setup_keeps_language_from_explicit_job_plan(rig):
    instance, row, _, client = rig
    row.job_id = "synthetic-plan"
    with app_db.SessionLocal() as db:
        db.add(Setting(key="web_language", value="fr"))
        db.add(SetupRebuildJob(job_id=row.job_id, plan_json=json.dumps({
            "radio_defaults": {"language": "de", "clockDisplay": True}})))
        db.commit()
    asyncio.run(prefs.configure(instance, row))
    assert client.radio["/language"] == '<sysLanguage>2</sysLanguage>'


def test_transport_protection_precedes_process_and_credentials(monkeypatch):
    create = AsyncMock()
    monkeypatch.setattr(wire.asyncio, "create_subprocess_exec", create)
    def refused(*args, **kwargs):
        raise PermissionError("PROTECTED")
    monkeypatch.setattr(wire, "assert_transport_allowed", refused)
    config = SshConfig("root",22,5,0,"","","/missing","strict")
    with pytest.raises(PermissionError):
        asyncio.run(wire.PresentationTransport("192.0.2.25",TARGET,config).exchange("inspect"))
    create.assert_not_awaited()


def test_command_builders_never_warm_kill_or_overwrite_app():
    snap = snapshot()
    hashes = {k:"a"*64 for k in ("control.sh","rc.installed","BoseApp.candidate","splash.candidate")}
    all_scripts = wire.envelope(TARGET) + wire.inspect_command() + wire.backup_command()
    for operation in ("stage","receipt","activate","undo"):
        all_scripts += wire.mutation_command(operation,snap,hashes,"b"*64)
    assert "killall" not in all_scripts and "pkill" not in all_scripts
    assert "> /opt/Bose/BoseApp" not in all_scripts
    with pytest.raises(m.PresentationError):
        wire.mutation_command("stage",snap,hashes,";false")


@pytest.mark.parametrize("failure", ["presentation_preflight", "presentation_prepare", "presentation_activate", "presentation_verify"])
def test_coordinator_fails_closed_in_each_presentation_phase(failure):
    from test_setup_rebuild_160 import RecordingAdapter
    from test_setup_transaction_ownership import prepared
    adapter = RecordingAdapter()
    async def failed(row):
        adapter.calls.append(failure)
        if failure == "presentation_verify":
            return {"presentation_verified": False}
        raise m.PresentationError("FIXTURE_FAILURE")
    setattr(adapter, failure, failed)
    coordinator, job = prepared(adapter, presentation_mode="patched")
    result = asyncio.run(coordinator.execute(job))
    assert result["status"] == "failed"
    if failure in {"presentation_preflight","presentation_prepare"}:
        assert "route" not in adapter.calls
    assert "pair_account" not in adapter.calls
    assert "playback_test" not in adapter.calls
    assert result["devices"][0]["evidence"]["verified"] is False


@pytest.mark.parametrize("ack", [None, False, "true", 1])
def test_live_http_start_needs_literal_presentation_acknowledgement(monkeypatch, ack):
    from fastapi import FastAPI
    from fastapi.testclient import TestClient
    from basswiesn.app.routers import setup_rebuild as router
    from test_setup_rebuild_160 import _device
    with app_db.SessionLocal() as db:
        db.add(_device(IDENTITY)); db.commit()
    def forbidden():
        raise AssertionError("No job/transport before explicit firmware acknowledgement")
    monkeypatch.setattr(router, "get_coordinator", forbidden)
    app = FastAPI(); app.include_router(router.router)
    with TestClient(app) as client:
        response = client.post("/api/setup/rebuild/start", json={
            "device_ids": [IDENTITY], "server_host": "192.0.2.10", "presentation_mode": "patched", "presentation_acknowledged": ack})
    assert response.status_code == 400
    assert "bestätigen" in response.json()["detail"]


def test_generated_shell_is_syntax_valid_not_executed():
    import subprocess
    hashes = {k: "a"*64 for k in ("control.sh", "rc.installed", "BoseApp.candidate", "splash.candidate")}
    scripts = [wire.inspect_command(), wire.backup_command()]
    scripts += [wire.mutation_command(op, snapshot(), hashes, "b"*64) for op in ("stage", "receipt", "activate", "undo")]
    for script in scripts:
        result = subprocess.run(["/bin/sh", "-n"], input=wire.envelope(TARGET)+script, text=True, capture_output=True)
        assert result.returncode == 0, result.stderr


@pytest.mark.parametrize("username", ["root", ""])
def test_transport_pinned_command_and_no_secret_error_output(monkeypatch, tmp_path, username):
    create = AsyncMock()
    monkeypatch.setattr(wire.asyncio, "create_subprocess_exec", create)
    monkeypatch.setattr(wire, "assert_transport_allowed", lambda *a, **k: "192.0.2.42")
    monkeypatch.setattr(wire, "record_transport_attempt", lambda **k: None)
    config = SshConfig(username, 22, 3, 5, "", "", str(tmp_path / "known_hosts"), "strict")
    async def run():
        out, err = asyncio.StreamReader(), asyncio.StreamReader()
        out.feed_data(b"private output"); out.feed_eof()
        err.feed_data(b"credential=must-not-leak"); err.feed_eof()
        process = SimpleNamespace(stdout=out, stderr=err, stdin=SimpleNamespace(write=lambda data: None,
            drain=AsyncMock(), close=lambda: None), returncode=1, wait=AsyncMock(return_value=1))
        create.return_value = process
        with pytest.raises(m.PresentationError, match="SSH_REFUSED") as raised:
            await wire.PresentationTransport("192.0.2.42", TARGET, config).exchange("inspect")
        assert "credential" not in str(raised.value) and "private output" not in str(raised.value)
    asyncio.run(run())
    create.assert_awaited_once()
    args = create.call_args.args
    assert "StrictHostKeyChecking=yes" in args and "ConnectionAttempts=1" in args
    assert "root@192.0.2.42" in args and args[-1].startswith("set -eu")
    assert "BatchMode=yes" in args and "IdentityAgent=none" in args
    assert "sshpass" not in args and "-i" not in args
    assert config.username == username  # Do not change generic SSH settings.


def test_no_automatic_ssh_trust(monkeypatch):
    create = AsyncMock()
    monkeypatch.setattr(wire.asyncio, "create_subprocess_exec", create)
    monkeypatch.setattr(wire, "assert_transport_allowed", lambda *a, **k: "192.0.2.42")
    for host_policy in ("off", "accept-new"):
        config = SshConfig("root",22,5,0,"","","/missing",host_policy)
        with pytest.raises(m.PresentationError, match="PINNED_ROOT_SSH_REQUIRED"):
            asyncio.run(wire.PresentationTransport("192.0.2.42",TARGET,config).exchange("inspect"))
    create.assert_not_awaited()


def test_presentation_explicit_nonroot_is_not_silently_overridden(monkeypatch):
    create = AsyncMock()
    monkeypatch.setattr(wire.asyncio, "create_subprocess_exec", create)
    monkeypatch.setattr(wire, "assert_transport_allowed", lambda *a, **k: "192.0.2.42")
    config = SshConfig("operator", 22, 5, 0, "", "", "/missing", "strict")
    with pytest.raises(m.PresentationError, match="PINNED_ROOT_SSH_REQUIRED"):
        asyncio.run(wire.PresentationTransport("192.0.2.42", TARGET, config).exchange("inspect"))
    create.assert_not_awaited()
