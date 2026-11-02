import json
import base64
from pathlib import Path

import httpx
import pytest
from playwright.sync_api import expect, sync_playwright

from basswiesn.app import db as app_db
from basswiesn.app.models import Device
from tests.test_mobile_release import _LiveServer


pytestmark = [pytest.mark.browser, pytest.mark.integration]


SYNTHETIC_DEVICE_ID = "AABBCCDDEEFF"


def _set_ui(server: _LiveServer, *, mode: str, language: str = "en") -> None:
    response = httpx.post(
        f"{server.url}/api/system/settings",
        json={
            "ui_mode": mode,
            "web_language": language,
            "show_startup_warning": "false",
            "first_run_warning_required": "false",
        },
        timeout=3,
    )
    response.raise_for_status()


def _artifact(name: str) -> Path:
    path = Path("test-artifacts/2.5.1") / name
    path.parent.mkdir(parents=True, exist_ok=True)
    return path


@pytest.mark.parametrize("width,height", [(1440, 900), (390, 844)])
def test_easy_setup_and_radios_click_the_real_common_discovery_api(width: int, height: int):
    found = {
        "device_id": SYNTHETIC_DEVICE_ID,
        "ip_address": "192.0.2.31",
        "name": "Visible Test Radio",
        "model": "SoundTouch 20",
        "firmware": "27.0.6",
        "identity_verified": True,
    }
    calls: list[dict] = []

    with _LiveServer() as server, sync_playwright() as playwright:
        _set_ui(server, mode="easy")
        browser = playwright.chromium.launch(headless=True, args=["--no-sandbox"])
        page = browser.new_page(
            viewport={"width": width, "height": height},
            is_mobile=width < 600,
            has_touch=width < 600,
        )

        def scan_route(route):
            calls.append(route.request.post_data_json)
            route.fulfill(
                status=200,
                content_type="application/json",
                body=json.dumps({"cidr": "192.0.2.0/24", "scanned": 1, "found": [found]}),
            )

        page.route("**/api/devices/scan", scan_route)
        page.goto(server.url, wait_until="networkidle")
        assert "easy-mode" in (page.locator("body").get_attribute("class") or "").split()

        page.locator('.topnav > .nav-button[data-view="setup"]').click()
        with page.expect_request(lambda request: request.url.endswith("/api/devices/scan") and request.method == "POST"):
            page.locator("#setup-rebuild-discover").click()
        expect(page.locator("#setup-rebuild-discovery-status")).to_contain_text("1 of 1")

        page.locator('.topnav > .nav-button[data-view="devices"]').click()
        with page.expect_request(lambda request: request.url.endswith("/api/devices/scan") and request.method == "POST"):
            page.locator("#scan-radios-now").click()
        expect(page.locator("#device-scan-results")).to_contain_text("Visible Test Radio")
        assert len(calls) == 2
        assert all(call.get("save") is True for call in calls)
        assert page.evaluate("document.documentElement.scrollWidth <= document.documentElement.clientWidth")
        page.screenshot(path=str(_artifact(f"easy-discovery-{width}x{height}.png")), full_page=True)
        browser.close()


@pytest.mark.parametrize("mode", ["standard", "lab"])
def test_standard_and_lab_keep_the_same_explicit_radios_discovery(mode: str):
    calls = 0
    with _LiveServer() as server, sync_playwright() as playwright:
        _set_ui(server, mode=mode)
        browser = playwright.chromium.launch(headless=True, args=["--no-sandbox"])
        page = browser.new_page(viewport={"width": 1440, "height": 900})

        def scan_route(route):
            nonlocal calls
            calls += 1
            route.fulfill(status=200, content_type="application/json", body='{"cidr":"192.0.2.0/24","scanned":0,"found":[]}')

        page.route("**/api/devices/scan", scan_route)
        page.goto(server.url, wait_until="networkidle")
        page.locator('.topnav > .nav-button[data-view="devices"]').click()
        page.locator("#scan-radios-now").click()
        expect(page.locator("#device-scan-results")).to_contain_text("No SoundTouch radios found")
        assert calls == 1
        browser.close()


def _add_offline_device() -> None:
    with app_db.SessionLocal() as db:
        db.add(
            Device(
                device_id=SYNTHETIC_DEVICE_ID,
                ip_address="192.0.2.44",
                name="Offline Test Radio With A Deliberately Long Name",
                model="SoundTouch 30",
                firmware="27.0.6",
                reachable=False,
                offline_reason="synthetic browser regression fixture",
                identity_verified=True,
            )
        )
        db.commit()


@pytest.mark.parametrize(
    "width,height",
    [(1920, 1080), (1440, 900), (1366, 768), (390, 844), (430, 932)],
)
def test_remove_radio_action_is_inside_the_responsive_card_and_viewport(width: int, height: int):
    _add_offline_device()
    with _LiveServer() as server, sync_playwright() as playwright:
        _set_ui(server, mode="standard")
        browser = playwright.chromium.launch(headless=True, args=["--no-sandbox"])
        page = browser.new_page(viewport={"width": width, "height": height})
        page.goto(server.url, wait_until="networkidle")
        page.locator('.topnav > .nav-button[data-view="devices"]').click()
        card = page.locator("#devices-cards .device-card")
        remove = card.locator("[data-remove-device]")
        expect(remove).to_be_visible()
        card_box = card.bounding_box()
        button_box = remove.bounding_box()
        assert card_box and button_box
        assert button_box["x"] >= card_box["x"]
        assert button_box["x"] + button_box["width"] <= card_box["x"] + card_box["width"] + 1
        assert button_box["x"] + button_box["width"] <= width + 1
        assert page.evaluate("document.documentElement.scrollWidth <= document.documentElement.clientWidth")
        page.screenshot(path=str(_artifact(f"remove-radio-{width}x{height}.png")), full_page=True)
        browser.close()


def test_remove_radio_button_performs_the_local_only_delete_from_desktop():
    _add_offline_device()
    with _LiveServer() as server, sync_playwright() as playwright:
        _set_ui(server, mode="standard")
        browser = playwright.chromium.launch(headless=True, args=["--no-sandbox"])
        page = browser.new_page(viewport={"width": 1366, "height": 768})
        page.goto(server.url, wait_until="networkidle")
        page.locator('.topnav > .nav-button[data-view="devices"]').click()
        page.once("dialog", lambda dialog: dialog.accept("YES"))
        page.locator(f'#devices-cards [data-remove-device="{SYNTHETIC_DEVICE_ID}"]').click()
        expect(page.locator("#devices-cards")).to_contain_text("No devices configured")
        with app_db.SessionLocal() as db:
            assert db.query(Device).filter(Device.device_id == SYNTHETIC_DEVICE_ID).one_or_none() is None
        browser.close()


def test_factory_reset_is_reachable_only_in_lab_and_browser_preview_is_profile_bound():
    info = (
        f'<info deviceID="{SYNTHETIC_DEVICE_ID}"><name>LAB Test Radio</name>'
        '<type>SoundTouch 20 Series III</type><components><component><softwareVersion>'
        '27.0.6.46330.5043500 epdbuild.release</softwareVersion></component></components>'
        '<productID>093B</productID><variant>spotty</variant><moduleType>sm2</moduleType></info>'
    )
    with app_db.SessionLocal() as db:
        db.add(
            Device(
                device_id=SYNTHETIC_DEVICE_ID,
                ip_address="192.0.2.112",
                name="LAB Test Radio",
                model="SoundTouch 20 Series III",
                firmware="27.0.6.46330.5043500 epdbuild.release",
                info_xml=info,
                identity_verified=True,
                reachable=True,
            )
        )
        db.commit()

    with _LiveServer() as server, sync_playwright() as playwright:
        _set_ui(server, mode="standard")
        browser = playwright.chromium.launch(headless=True, args=["--no-sandbox"])
        page = browser.new_page(viewport={"width": 1440, "height": 900})
        page.goto(server.url, wait_until="networkidle")
        expect(page.locator('.advanced-nav .nav-button[data-view="lab"]')).not_to_be_visible()

        _set_ui(server, mode="lab")
        page.reload(wait_until="networkidle")
        page.locator(".advanced-nav > summary").click()
        page.locator('.advanced-nav .nav-button[data-view="lab"]').click()
        expect(page.locator("#lab-factory-reset-card")).to_be_visible()
        expect(page.locator("#factory-reset-identity")).to_contain_text(SYNTHETIC_DEVICE_ID)
        page.locator('[data-factory-reset-action="preview"]').click()
        expect(page.locator("#factory-reset-status")).to_contain_text("PROFILE VERIFIED")
        expect(page.locator("#factory-reset-output")).to_contain_text("sys factorydefault")
        page.screenshot(path=str(_artifact("lab-factory-reset-preview-1440x900.png")), full_page=True)
        browser.close()


@pytest.mark.parametrize("width,height", [(1440, 900), (390, 844)])
def test_online_station_search_renders_real_favicons_and_a_clean_fallback(width: int, height: int):
    station_names = ["Bayern 3", "Antenne Bayern", "SWR3", "BBC Radio 1"]
    payload = [
        {
            "name": name,
            "stream_url": f"https://streams.example/{index}.mp3",
            "image_url": f"https://logos.example/{index}.svg",
            "stream_format": "mp3",
            "country": "Test",
            "tags": "radio",
        }
        for index, name in enumerate(station_names)
    ]
    payload.append({"name": "No Logo FM", "stream_url": "https://streams.example/none.mp3", "image_url": "", "stream_format": "mp3"})

    with _LiveServer() as server, sync_playwright() as playwright:
        _set_ui(server, mode="easy")
        browser = playwright.chromium.launch(headless=True, args=["--no-sandbox"])
        page = browser.new_page(viewport={"width": width, "height": height})
        page.route(
            "**/api/stations/search-online?*",
            lambda route: route.fulfill(status=200, content_type="application/json", body=json.dumps(payload)),
        )
        proxied_logos = []
        png = base64.b64decode("iVBORw0KGgoAAAANSUhEUgAAAAEAAAABCAQAAAC1HAwCAAAAC0lEQVR42mNk+A8AAQUBAScY42YAAAAASUVORK5CYII=")

        def artwork_route(route):
            proxied_logos.append(route.request.url)
            route.fulfill(status=200, content_type="image/png", body=png)

        page.route("**/api/stations/online-artwork?*", artwork_route)
        page.goto(server.url, wait_until="networkidle")
        page.locator('.topnav > .nav-button[data-view="presets"]').click()
        page.locator('#online-search-form input[name="q"]').fill("radio")
        page.locator('#online-search-form button[type="submit"]').click()
        expect(page.locator("#online-station-results .station-result-card")).to_have_count(5)
        for name in station_names:
            expect(page.locator("#online-station-results")).to_contain_text(name)
        images = page.locator("#online-station-results [data-online-station-logo]")
        expect(images).to_have_count(4)
        page.wait_for_function("() => [...document.querySelectorAll('[data-online-station-logo]')].every(img => img.complete && img.naturalWidth > 0)")
        assert len(proxied_logos) == 4
        assert all(url.startswith(f"{server.url}/api/stations/online-artwork?") for url in proxied_logos)
        expect(page.locator("#online-station-results .station-logo-placeholder")).to_have_count(1)
        assert page.evaluate("document.documentElement.scrollWidth <= document.documentElement.clientWidth")
        page.screenshot(path=str(_artifact(f"station-logos-{width}x{height}.png")), full_page=True)
        browser.close()
