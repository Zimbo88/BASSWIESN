from hashlib import sha256
import json

from fastapi import FastAPI
from fastapi.testclient import TestClient
import pytest

from basswiesn.app import db as app_db
from basswiesn.app.models import Device, Setting
from basswiesn.app.routers import radio_patches as router
from basswiesn.app.services import radio_patch_policy as p


def facts():
    return dict(model="SoundTouch Portable",variant="taigan",platform="scm",firmware=p.FIRMWARE,
                app=p.APP_STOCK,battery=p.BATTERY_STOCK,splash=(p.SPLASH_STOCK,p.SPLASH_STOCK),framebuffer=(128,100,8,128))


@pytest.mark.parametrize("battery",[p.BATTERY_STOCK,p.BATTERY_PATCHED,p.BATTERY_LEGACY])
@pytest.mark.parametrize("app",[p.APP_STOCK,p.APP_LEGACY,p.APP_DISPLAY,p.APP_COMBINED])
def test_display_compatibility_preserves_all_known_battery_states(battery,app):
    f=facts();f.update(battery=battery,app=app);r=p.compatibility(**f)
    assert r["eligible_for_private_trial"]
    assert r["battery_must_remain_unchanged"]
    assert r["existing_app_changes_must_be_preserved"]
    assert not r["write_authorized"]
    assert r["hardware_acceptance_required"]
    assert r["candidate_app_sha256"]==(p.APP_COMBINED if app in {p.APP_LEGACY,p.APP_COMBINED} else p.APP_DISPLAY)


@pytest.mark.parametrize("field,value",[("app","unknown"),("battery","unknown"),("firmware","27.0.7"),
    ("variant","unknown"),("platform","unknown"),("framebuffer",(128,100,16,256)),
    ("splash",(p.SPLASH_STOCK,p.SPLASH_LOGO)),("splash",())])
def test_unknown_or_partial_combination_fails_closed(field,value):
    f=facts();f[field]=value
    assert not p.compatibility(**f)["eligible_for_private_trial"]


def test_composition_keeps_existing_unowned_changes():
    original=b"originalPATCHexisting-user-modification"
    expected=b"originalOWNEDexisting-user-modification"+b"payload"
    change=p.ImageChange(8,b"PATCH",b"OWNED")
    result=p.compose_image(original,expected=sha256(original).hexdigest(),result=sha256(expected).hexdigest(),changes=(change,),tail=b"payload")
    assert result==expected
    assert b"existing-user-modification" in result
    for changes in ((p.ImageChange(8,b"WRONG",b"OWNED"),),(change,change),(p.ImageChange(-1,b"a",b"b"),)):
        with pytest.raises(ValueError):p.compose_image(original,expected=sha256(original).hexdigest(),result=sha256(expected).hexdigest(),changes=changes,tail=b"payload")


def prepare(mode="lab",model="SoundTouch Portable",identity="AA11BB22CC33"):
    with app_db.SessionLocal() as db:
        db.merge(Setting(key="ui_mode",value=mode))
        db.add(Device(device_id=identity,name="Example",model=model,ip_address="192.0.2.42"));db.commit()
    app=FastAPI();app.include_router(router.router)
    return TestClient(app)


def test_selection_is_local_and_battery_keep_is_default():
    with prepare() as client:
        catalog=client.get("/api/lab/radio-patches").json()
        assert not catalog["radio_contacted"] and not catalog["installation_available"]
        assert catalog["setup_presentation"]["required_components"]==[]
        assert catalog["setup_presentation"]["optional_components"]==["display","bootlogo"]
        assert catalog["setup_presentation"]["setup_integration_status"]=="IMPLEMENTED_HARDWARE_ACCEPTANCE_REQUIRED"
        path="/api/lab/radio-patches/AA11BB22CC33/selection"
        current=client.get(path).json()
        assert current["display"]==current["bootlogo"]=="keep"
        assert current["battery"]=="keep"
        body={k:current[k] for k in ("display","bootlogo","battery","battery_kind","revision")}
        body.update(display="enable",bootlogo="disable")
        result=client.put(path,json=body)
        assert result.status_code==200
        assert result.json()["status"]=="PLANNED" and not result.json()["installed"]
        assert client.put(path,json=body).status_code==409
        saved=client.get(path).json()
        assert saved["display"]=="enable" and saved["bootlogo"]=="disable" and saved["battery"]=="keep"


def test_standard_presentation_never_authorizes_unknown_firmware_or_battery_changes():
    assert p.default_selection()=={"display":"keep","bootlogo":"keep","battery":"keep","battery_kind":"unknown"}
    contract=p.setup_presentation_contract()
    assert contract["cold_boot_and_actual_readback_required"]
    assert contract["exact_image_and_offdevice_backup_required"]
    assert contract["recovery_removal_allowed"]
    assert contract["guided_installer_available"]
    assert not contract["may_claim_standard_presentation_installed"]
    f=facts();f["firmware"]="27.0.7"
    result=p.compatibility(**f)
    assert not result["eligible_for_private_trial"] and not result["write_authorized"]


def test_existing_rollback_choice_is_not_reenabled_by_new_setup_defaults():
    with prepare() as client:
        path="/api/lab/radio-patches/AA11BB22CC33/selection"
        current=client.get(path).json()
        body={k:current[k] for k in ("display","bootlogo","battery","battery_kind","revision")}
        body.update(display="disable",bootlogo="keep")
        assert client.put(path,json=body).status_code==200
        saved=client.get(path).json()
        assert saved["display"]=="disable" and saved["bootlogo"]=="keep"


def test_original_battery_cannot_accidentally_enable_patch():
    with prepare() as client:
        path="/api/lab/radio-patches/AA11BB22CC33/selection"
        current=client.get(path).json()
        body={k:current[k] for k in ("display","bootlogo","battery","battery_kind","revision")}
        body.update(battery="enable",battery_kind="original")
        assert client.put(path,json=body).status_code==409
        body.update(battery_kind="compatible_replacement")
        assert client.put(path,json=body).status_code==200


def test_nonlab_and_protected_selection_blocked():
    with prepare("standard") as client:
        assert client.get("/api/lab/radio-patches").status_code==409


def test_protected_radio_never_in_catalogue():
    with prepare(identity="CCDDEEFF0011") as client:
        assert not client.get("/api/lab/radio-patches").json()["devices"]
        assert client.get("/api/lab/radio-patches/CCDDEEFF0011/selection").status_code in {403,409}
