"""Human-click remote flows with every HTTP request intercepted offline."""
import json
from pathlib import Path
from urllib.parse import urlparse

from fastapi.testclient import TestClient
from playwright.sync_api import sync_playwright, expect
import pytest

from basswiesn.app.main import create_web_app

pytestmark = [pytest.mark.browser, pytest.mark.integration, pytest.mark.release]


@pytest.mark.parametrize("language,width,height", [("de", 390, 844), ("en", 1440, 900), ("de", 1920, 1080), ("en", 430, 932)])
def test_remote_human_flow_without_radio_contacts(tmp_path, language, width, height):
    with TestClient(create_web_app(background_tasks=False)) as client:
        html = client.get("/remote/REMOTE-A").text
    assets = Path("basswiesn/app/static")
    calls = []
    clock_preference = {"enabled": True, "mode": "APPEND", "timezone": "Europe/Berlin"}
    now = {"verified": True, "volume": 23, "source": "LOCAL_INTERNET_RADIO", "play_status": "PLAY_STATE",
           "now_playing": {"stationName": "Example Station", "track": "Example Track", "artist": "Example Artist"},
           "raw": {"/now_playing": "<nowPlaying><track>Example Track</track></nowPlaying>"}}
    radios = [{"device_id": "REMOTE-A", "name": "Main Radio", "reachable": True},
              {"device_id": "REMOTE-B", "name": "Other Radio", "reachable": True},
              {"device_id": "PROTECTED", "name": "Never contact", "reachable": True, "protected": True},
              {"device_id": "OFFLINE", "name": "Offline", "reachable": False}]

    reconnect_preference = {"enabled": False, "state": "IDLE"}
    field_order = ["station", "artist", "title", "clock", "other"]
    display_preference = {"mode": "STATION", "show_other_info": False, "fields": ["station", "clock"], "field_order": field_order}

    def handler(route):
        request = route.request
        path = urlparse(request.url).path
        body = request.post_data_json if request.method in {"POST", "PUT"} else None
        calls.append((request.method, path, body))
        if path == "/api/devices/REMOTE-A/metadata/display":
            if request.method == "PUT":
                display_preference.update(body)
            return route.fulfill(json=display_preference)
        if path == "/api/devices/REMOTE-A/live-reconnect":
            if request.method == "PUT":
                reconnect_preference.update(body)
            return route.fulfill(json=reconnect_preference)
        if path == "/api/devices/REMOTE-A/metadata/clock":
            if request.method == "PUT":
                clock_preference.update(body)
            return route.fulfill(json=clock_preference)
        if path == "/remote/REMOTE-A":
            return route.fulfill(body=html, content_type="text/html")
        if path in {"/static/remote.js", "/static/remote.css", "/static/theme.css", "/static/js/theme.js",
                    "/static/js/translations.js", "/static/js/language-extension.js", "/static/js/locale-301.js",
                    "/static/js/help-content.js", "/static/js/help.js", "/static/help.css"}:
            return route.fulfill(body=(assets / path.removeprefix("/static/")).read_text(), content_type="text/javascript" if path.endswith(".js") else "text/css")
        response = {
            "/api/system/settings": {"web_language": language, "safe_startup_volume": 1},
            "/api/devices": radios,
            "/api/stations": [{"id": 1, "name": "Example Station"}, {"id": 2, "name": '<img src=x onerror="window.BAD=true">'}],
            "/api/presets/REMOTE-A": [{"button": 1, "station_name": "Example Station"}],
            "/api/devices/REMOTE-A/remote-state": now,
            "/api/multiroom/preview": {"blocked": False},
            "/api/multiroom/remote-start": {"verification": [{"device_id": "REMOTE-A", "ok": True}, {"device_id": "REMOTE-B", "ok": True}], "volume_warnings": [],
                "volume_observations": [{"device_id": "REMOTE-A", "before": 23, "after": 23}, {"device_id": "REMOTE-B", "before": 8, "after": 8}]},
            "/api/devices/REMOTE-A/stations/1/play": {"verified": True},
            "/api/devices/REMOTE-A/key": {"verified": True},
        }
        assert path in response, f"Unexpected request (never sent): {request.method} {path}"
        route.fulfill(json=response[path])

    with sync_playwright() as pw:
        browser = pw.chromium.launch(headless=True, args=["--no-sandbox"])
        page = browser.new_page(viewport={"width": width, "height": height})
        page.route("**/*", handler)
        page.goto("http://127.0.0.1:8765/remote/REMOTE-A")
        expect(page.locator("#remote-volume-label")).to_have_text("23%")
        expect(page.locator("#remote-now")).to_have_text("Example Station")
        expect(page.locator("#remote-track")).to_have_text("Example Artist — Example Track")
        assert page.locator("html").get_attribute("lang") == language
        assert page.locator("#remote-details").get_attribute("open") is None
        expect(page.locator("#remote-output")).not_to_be_visible()
        expect(page.locator("#remote-safe-start-field")).not_to_be_visible()
        assert page.evaluate("document.documentElement.scrollWidth <= innerWidth")
        assert page.evaluate("window.BAD") is None
        expect(page.locator("#remote-display-clock")).to_be_enabled()
        expect(page.locator("#remote-display-clock")).to_be_checked()
        assert not [call for call in calls if call[0] in {"POST", "PUT"}]
        expect(page.locator("#remote-display-artist")).not_to_be_checked()
        expect(page.locator("#remote-display-title")).not_to_be_checked()
        expect(page.locator("#remote-display-other")).not_to_be_checked()
        page.locator("#remote-display-artist").check()
        page.locator("#remote-display-title").check()
        page.locator("#remote-display-other").check()
        for _ in range(3):
            page.locator('[data-field="clock"][data-move="up"]').click()
        assert not [call for call in calls if call[0] in {"POST", "PUT"}], "editing is only a draft"
        expect(page.locator("#remote-display-preview")).to_have_text(
            "20:15 — Beispielsender — Beispielinterpret — Beispieltitel — Senderhinweis" if language == "de" else
            "20:15 — Example Station — Example Artist — Example Song — Station information")
        expected_order = ["clock", "station", "artist", "title", "other"]
        page.locator("#remote-display-save").click()
        expect(page.locator("#remote-display-station")).to_be_enabled()
        expect(page.locator("#remote-display-save")).to_be_disabled()
        assert display_preference["mode"] == "CUSTOM"
        assert display_preference["fields"] == display_preference["field_order"] == expected_order
        assert calls[-1][:2] == ("GET", "/api/devices/REMOTE-A/metadata/display")
        # Persist/reload the actual browser view: no implicit writes.
        writes_before = len([call for call in calls if call[0] in {"POST", "PUT"}])
        page.reload()
        expect(page.locator("#remote-display-clock")).to_be_enabled()
        assert page.locator(".remote-display-row").first.get_attribute("data-field") == "clock"
        assert len([call for call in calls if call[0] in {"POST", "PUT"}]) == writes_before
        # Exercise all32 field combinations with real controls on one mobile
        # viewport; every save is a single preference PUT followed by GET.
        if language == "de" and width == 390:
            for mask in range(32):
                for index, field in enumerate(field_order):
                    page.locator(f"#remote-display-{field}").set_checked(bool(mask & (1 << index)))
                if page.locator("#remote-display-save").is_enabled():
                    page.locator("#remote-display-save").click()
                    expect(page.locator("#remote-display-station")).to_be_enabled()
                    expect(page.locator("#remote-display-save")).to_be_disabled()
                expected_fields = [field for field in expected_order if mask & (1 << field_order.index(field))]
                assert display_preference["fields"] == expected_fields
                assert display_preference["field_order"] == expected_order
            # Disabled fields retain their position in the next layout.
            page.locator("#remote-display-clock").uncheck()
            page.locator('[data-field="clock"][data-move="down"]').click()
            page.locator("#remote-display-save").click()
            expect(page.locator("#remote-display-station")).to_be_enabled()
            expect(page.locator("#remote-display-save")).to_be_disabled()
            assert "clock" not in display_preference["fields"]
            assert display_preference["field_order"][1] == "clock"
        expect(page.locator("#remote-reconnect")).not_to_be_checked()
        page.locator("#remote-reconnect").check()
        expect(page.locator("#remote-reconnect")).to_be_enabled()
        expect(page.locator("#remote-reconnect")).to_be_checked()
        assert [call for call in calls if call[0] == "PUT"][-1][2] == {"enabled": True}
        assert calls[-1][:2] == ("GET", "/api/devices/REMOTE-A/live-reconnect")
        page.locator("#remote-play-station").click()
        expect(page.locator("#remote-play-station")).to_be_enabled()
        playback = [call for call in calls if call[1].endswith("/play")]
        assert playback[-1][2] == {"dry_run": False}
        page.locator("#remote-safe-start-enabled").check()
        expect(page.locator("#remote-safe-start-field")).to_be_visible()
        page.locator("button[data-preset='1']").click()
        expect(page.locator("button[data-preset='1']")).to_be_enabled()
        assert [call for call in calls if call[1].endswith("/key")][-1][2] == {"key": "PRESET_1", "safe_volume": 1}
        page.locator("#remote-multiroom-open").click()
        expect(page.locator("#remote-multiroom")).to_be_visible()
        expect(page.locator("#remote-master")).to_contain_text("Main Radio")
        assert page.locator("#remote-members input").count() == 1
        page.locator("#remote-members input").check()
        page.on("dialog", lambda dialog: dialog.accept())
        page.locator("#remote-multiroom-start").click()
        expect(page.locator("#remote-message")).to_contain_text("bestätigt" if language == "de" else "confirmed")
        expect(page.locator("#remote-volume-readback")).to_be_visible()
        expect(page.locator("#remote-volume-readback")).to_contain_text("Other Radio: 8% → 8%")
        group = [call for call in calls if call[1] == "/api/multiroom/remote-start"][-1][2]
        assert group == {"master_device_id": "REMOTE-A", "member_device_ids": ["REMOTE-B"], "preserve_volumes": True, "set_start_volumes": False}
        assert not any(call[1].endswith("/volume") or "discovery" in call[1] or "scan" in call[1] for call in calls)
        assert not any("PROTECTED" in call[1] or "OFFLINE" in call[1] for call in calls)
        assert page.evaluate("document.documentElement.scrollWidth <= innerWidth")
        page.screenshot(path=str(tmp_path / f"remote-{language}-{width}.png"), full_page=True)
        page.locator("#remote-details summary").click()
        expect(page.locator("#remote-output")).to_be_visible()
        browser.close()
