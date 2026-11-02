"""Real browser, mocked settings response; no household transport."""

import json
from contextlib import ExitStack

import httpx
import pytest
from playwright.sync_api import expect, sync_playwright

from test_mobile_release import _LiveServer

pytestmark = [pytest.mark.browser, pytest.mark.integration]


@pytest.mark.parametrize("engine", ["chromium", "webkit"])
@pytest.mark.parametrize("language,phrase", [("de", "Einstellungen nicht vollständig bestätigt"),
                                               ("en", "Settings not fully confirmed")])
def test_partial_write_is_visible_and_never_replayed(engine, language, phrase):
    with _LiveServer() as server, sync_playwright() as pw:
        httpx.post(server.url + "/api/system/settings", json={
            "ui_mode": "standard", "web_language": language,
            "show_startup_warning": "false", "first_run_warning_required": "false",
        }).raise_for_status()
        browser = getattr(pw, engine).launch(headless=True)
        page = browser.new_page(viewport={"width": 430, "height": 932})
        writes, errors = [], []
        page.on("pageerror", lambda error: errors.append(str(error)))

        def route(r):
            if not r.request.url.startswith(server.url + "/"):
                return r.abort()
            if r.request.url == server.url + "/api/devices":
                # A DOM-only fake option can correctly disappear when a late
                # startup inventory arrives. Model a real saved selection.
                return r.fulfill(json=[{"device_id": "EXAMPLE", "name": "Example Radio", "ip_address": "192.0.2.42"}])
            if r.request.url.endswith("/settings-apply"):
                writes.append(r.request.post_data_json)
                return r.fulfill(status=409, content_type="application/json", body=json.dumps({"detail": {
                    "status": "DIVERGED", "code": "WRITE_READBACK_MISMATCH", "applied": 1,
                    "failed_setting": "volume", "changed": [{"setting": "name", "after": "After"}],
                    "reconciliation_required": True, "job_id": "synthetic-operation",
                }}))
            if "/api/devices/EXAMPLE/settings" in r.request.url:
                return r.fulfill(content_type="application/json", body=json.dumps({"device_id": "EXAMPLE", "current": {
                    "name": "Example Radio", "volume": 5, "bass": -2, "language": "en", "clockDisplay": False,
                }}))
            return r.continue_()

        page.route("**/*", route)
        page.goto(server.url, wait_until="networkidle")
        page.wait_for_function("lang => document.documentElement.lang === lang", arg=language)
        # Insert a synthetic, local-only selection. Exercise the real form
        # listener and API helper, not a reimplementation of their behavior.
        page.evaluate("""() => {
          const select = document.querySelector('#settings-device-select');
          select.add(new Option('Example Radio', 'EXAMPLE'));
          select.value = 'EXAMPLE';
          document.querySelectorAll('.view').forEach(v => v.classList.remove('is-active'));
          document.querySelector('#view-device-settings').classList.add('is-active');
        }""")
        page.locator("#settings-device-select").dispatch_event("change")
        expect(page.locator('#device-settings-form [name="name"]')).to_have_value("Example Radio")
        page.locator('#device-settings-form [name="volume"]').fill("1")
        page.locator('#device-settings-form [name="name"]').fill("After")
        page.locator("#device-settings-apply").click()
        expect(page.locator("#device-settings-output")).to_contain_text(phrase)
        expect(page.locator("#device-settings-output")).to_contain_text("DIVERGED")
        expect(page.locator("#device-settings-output")).to_contain_text('"applied": 1')
        expect(page.locator("#device-settings-output")).to_contain_text("synthetic-operation")
        expect(page.locator("#device-settings-apply")).to_be_disabled()
        expect(page.locator("#reload-device-settings")).to_be_enabled()
        assert len(writes) == 1
        assert writes[0]["values"] == {"name": "After", "volume": 1}
        assert writes[0]["expected_previous"] == {"name": "Example Radio", "volume": 5}
        assert errors == []
        browser.close()


@pytest.fixture(params=["chromium", "webkit"])
def settings_page(request):
    with _LiveServer() as server, sync_playwright() as pw, ExitStack() as cleanup:
        httpx.post(server.url + "/api/system/settings", json={
            "ui_mode": "lab", "web_language": "en",
            "show_startup_warning": "false", "first_run_warning_required": "false",
        }).raise_for_status()
        browser = getattr(pw, request.param).launch(headless=True)
        cleanup.callback(browser.close)
        page = browser.new_page(viewport={"width": 430, "height": 932})
        errors, writes, held = [], [], []
        state = {"hold_read": False, "hold_write": False, "fail": False, "identity": None,
                 "clock_error": False, "hold_preview": False}
        page.on("pageerror", lambda error: errors.append(str(error)))

        def response(device):
            return {"device_id": state["identity"] or device, "current": {
                "name": f"Radio {device}", "volume": 5, "bass": -2, "language": state.get("language", "en"),
                "clockDisplay": "ERROR: ValueError" if state["clock_error"] else False,
                "clockConfig": {"brightnessLevel": "70", "timezoneInfo": "Europe/Berlin",
                                "timeFormat": "TIME_FORMAT_24HOUR_ID", "userOffsetMinute": "0",
                                "userUtcTime": "1000", "userEnable": "false"},
                "station_art_mode": "radio_symbol",
            }}

        def route(r):
            url = r.request.url
            if not url.startswith(server.url + "/"):
                return r.abort()
            if url == server.url + "/api/devices":
                return r.fulfill(json=[{"device_id": device, "name": f"Radio {device}", "ip_address": ip}
                                       for device, ip in (("A", "192.0.2.42"), ("B", "192.0.2.43"))])
            if url.endswith("/settings-apply"):
                writes.append((url, r.request.post_data_json))
                if state["hold_write"]:
                    held.append(r)
                    return
                return r.fulfill(json={"applied": 1, "device_id": url.split("/")[-2],
                                       "status": "VERIFIED", "preset_sync_required": False})
            if "/api/presets/" in url and url.endswith("/sync"):
                if state["hold_preview"]:
                    held.append(r)
                    return
                writes.append((url, r.request.post_data_json))
                return r.fulfill(json={"device_id": url.split("/")[-2], "verified": True, "radio_slots": []})
            if "/api/devices/" in url and "/settings?probe=true" in url:
                device = url.split("/")[-2]
                if state["hold_read"] and device == "A":
                    held.append(r)
                    return
                return r.fulfill(status=503 if state["fail"] else 200, json=response(device))
            return r.continue_()

        page.route("**/*", route)
        page.goto(server.url, wait_until="networkidle")
        page.wait_for_function("document.documentElement.lang === 'en'")
        page.evaluate("""() => {
          const select = document.querySelector('#settings-device-select');
          select.replaceChildren(new Option('Radio A', 'A'), new Option('Radio B', 'B'));
          document.querySelectorAll('.view').forEach(v => v.classList.remove('is-active'));
          document.querySelector('#view-device-settings').classList.add('is-active');
        }""")
        yield page, state, writes, held, response
        assert errors == []


def test_uninitialized_language_can_be_fixed_without_reset_or_replay(settings_page):
    page, state, writes, held, _ = settings_page
    state.update(language="id:0", hold_write=True)
    page.locator("#settings-device-select").dispatch_event("change")
    language = page.locator("#device-language-select")
    expect(language).to_be_enabled()
    expect(language.locator("option:checked")).to_have_text("Language not initialized")
    language.select_option("de")
    page.locator("#device-settings-apply").click()
    expect(page.locator("#device-settings-dirty")).to_contain_text("do not resend")
    expect(page.locator("#device-settings-apply")).to_be_disabled()
    assert writes == [(page.url.rstrip("/") + "/api/devices/A/settings-apply", {
        "values": {"language": "de"}, "expected_previous": {"language": "id:0"}})]
    state.update(language="de", hold_write=False)
    held.pop().fulfill(json={"device_id": "A", "applied": 1, "status": "VERIFIED"})
    expect(language).to_have_value("de")
    expect(language).to_be_enabled()
    assert len(writes) == 1


def select_settings(page, device):
    page.locator("#settings-device-select").select_option(device)


def test_late_read_cannot_overwrite_selected_radio_or_draft(settings_page):
    page, state, writes, held, response = settings_page
    state["hold_read"] = True
    select_settings(page, "A")
    expect(page.locator("#device-settings-apply")).to_be_disabled()
    page.wait_for_timeout(100)
    assert len(held) == 1
    select_settings(page, "B")
    name = page.locator('#device-settings-form [name="name"]')
    expect(name).to_have_value("Radio B")
    name.fill("B draft")
    held.pop().fulfill(json=response("A"))
    page.wait_for_timeout(100)
    expect(name).to_have_value("B draft")
    assert writes == []


def test_write_is_single_flight_and_result_stays_bound_to_original_target(settings_page):
    page, state, writes, held, _ = settings_page
    select_settings(page, "A")
    volume = page.locator('#device-settings-form [name="volume"]')
    expect(volume).to_be_enabled()
    volume.fill("1")
    state["hold_write"] = True
    page.locator("#device-settings-apply").click()
    page.locator("#device-settings-form").dispatch_event("submit")
    select_settings(page, "B")
    expect(page.locator('#device-settings-form [name="name"]')).to_have_value("Radio B")
    held.pop().fulfill(json={"applied": 1, "device_id": "A", "preset_sync_required": True})
    expect(page.locator("#device-settings-apply")).to_be_enabled()
    expect(page.locator("#device-settings-output")).to_contain_text('"device_id": "B"')
    assert len(writes) == 1
    assert writes[0][0].endswith("/A/settings-apply")
    assert writes[0][1] == {"values": {"volume": 1}, "expected_previous": {"volume": 5}}


def test_failed_load_cannot_submit_old_values_and_explicit_reload_recovers(settings_page):
    page, state, writes, _, _ = settings_page
    select_settings(page, "A")
    expect(page.locator('#device-settings-form [name="name"]')).to_have_value("Radio A")
    state["fail"] = True
    select_settings(page, "B")
    expect(page.locator("#device-settings-output")).to_contain_text("503")
    expect(page.locator('#device-settings-form [name="name"]')).to_be_disabled()
    page.locator("#device-settings-form").dispatch_event("submit")
    assert writes == []
    state["fail"] = False
    page.locator("#reload-device-settings").click()
    expect(page.locator('#device-settings-form [name="name"]')).to_have_value("Radio B")
    expect(page.locator("#device-settings-apply")).to_be_enabled()


def test_clock_write_contains_only_changed_fields_and_their_actual_previous_values(settings_page):
    page, _, writes, _, _ = settings_page
    select_settings(page, "A")
    brightness = page.locator('#device-settings-form [name="brightnessLevel"]')
    expect(brightness).to_have_value("70")
    brightness.fill("0")
    page.locator("#device-settings-apply").click()
    expect(page.locator("#device-settings-apply")).to_be_enabled()
    assert writes[0][1] == {
        "values": {"clockConfig": {"brightnessLevel": 0}},
        "expected_previous": {"clockConfig": {"brightnessLevel": "70"}},
    }


def test_unknown_boolean_is_not_displayed_as_enabled(settings_page):
    page, state, writes, _, _ = settings_page
    state["clock_error"] = True
    select_settings(page, "A")
    expect(page.locator('#device-settings-form [name="name"]')).to_have_value("Radio A")
    clock = page.locator('#device-settings-form [name="clockDisplay"]')
    expect(clock).to_be_disabled()
    expect(clock).to_have_value("")
    assert writes == []


def test_wrong_response_identity_blocks_form(settings_page):
    page, state, writes, _, _ = settings_page
    state["identity"] = "WRONG"
    select_settings(page, "A")
    expect(page.locator("#device-settings-output")).to_contain_text("IDENTITY_MISMATCH")
    expect(page.locator("#device-settings-apply")).to_be_disabled()
    assert writes == []


def test_old_preset_preview_cannot_authorize_new_selection(settings_page):
    page, state, writes, held, _ = settings_page
    select_settings(page, "A")
    expect(page.locator('#device-settings-form [name="name"]')).to_have_value("Radio A")
    state["hold_preview"] = True
    page.locator("#sync-device-presets").click()
    select_settings(page, "B")
    expect(page.locator('#device-settings-form [name="name"]')).to_have_value("Radio B")
    held.pop().fulfill(json={"target": {"device_id": "A"}, "expected_changes": [{"button": 1}]})
    page.wait_for_timeout(100)
    expect(page.locator("#device-settings-preset-sync-confirm")).to_be_hidden()
    page.locator("#device-settings-preset-sync-confirm").dispatch_event("click")
    assert writes == []


def test_preset_confirmation_requires_current_preview_readback_and_acknowledgement(settings_page):
    page, state, writes, held, _ = settings_page
    select_settings(page, "A")
    expect(page.locator('#device-settings-form [name="name"]')).to_have_value("Radio A")
    state["hold_preview"] = True
    page.locator("#sync-device-presets").click()
    result = {"target": {"device_id": "A"}, "expected_changes": [{"button": 1}],
              "protection": {"write_allowed": True}}
    held.pop().fulfill(json=result)
    expect(page.locator("#device-settings-preset-sync-status")).to_contain_text("No write authorized")
    page.locator("#device-settings-memory-check").check()
    expect(page.locator("#device-settings-preset-sync-confirm")).to_be_hidden()
    page.locator("#sync-device-presets").click()
    held.pop().fulfill(json={**result, "preview_readback_performed": True})
    expect(page.locator("#device-settings-preset-sync-confirm")).to_be_visible()
    state["hold_preview"] = False
    page.on("dialog", lambda dialog: dialog.accept())
    page.locator("#device-settings-preset-sync-confirm").click()
    expect(page.locator("#device-settings-preset-sync-status")).to_contain_text("readback confirmed")
    assert len(writes) == 1
    assert writes[0][0].endswith("/A/sync")
    assert writes[0][1] == {"dry_run": False, "memory_checked": True}


@pytest.mark.parametrize("engine", ["chromium", "webkit"])
def test_early_selection_waits_for_settings_module(engine):
    with _LiveServer() as server, sync_playwright() as pw:
        browser = getattr(pw, engine).launch(headless=True)
        page = browser.new_page()
        held, reads = [], []

        def route(r):
            if not r.request.url.startswith(server.url + "/"):
                return r.abort()
            if r.request.url.endswith("/js/device-settings.js"):
                held.append(r)
                return
            if "/api/devices/EARLY/settings" in r.request.url:
                reads.append(r.request.url)
                return r.fulfill(json={"device_id": "EARLY", "current": {"name": "Early radio", "volume": 5}})
            return r.continue_()

        page.route("**/*", route)
        page.goto(server.url, wait_until="domcontentloaded")
        page.wait_for_function("state.featureStatus !== null && typeof state.featureStatus === 'object'")
        # Deliberately dispatch before the module's listeners could exist, then
        # release the import. Do not wait for network-idle with a held request.
        assert len(held) == 1
        page.evaluate("""() => {
          const select = document.querySelector('#settings-device-select');
          select.replaceChildren(new Option('Early radio', 'EARLY'));
          select.dispatchEvent(new Event('change', {bubbles: true}));
        }""")
        held.pop().continue_()
        expect(page.locator('#device-settings-form [name="name"]')).to_have_value("Early radio")
        assert len(reads) == 1
        browser.close()
