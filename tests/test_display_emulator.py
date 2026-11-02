"""Offline display model, shared composition, loopback boundary and real browser."""
from __future__ import annotations

from itertools import permutations
import json
import os
from pathlib import Path
import select
import subprocess
import sys

import httpx
import pytest

from basswiesn.app.services import display_composition as pure
from basswiesn.app.services import station_metadata as runtime
from tools.display_emulator import compose, network_guard

pytestmark = pytest.mark.integration


def payload(**changes):
    return {"station": "Example", "metadata": {"track": "Song", "artist": "Artist"}, "mode": "CUSTOM",
            "fields": list(pure.DISPLAY_FIELDS[:-1]), "order": list(pure.DISPLAY_FIELDS), "clock": "20:15", **changes}


def test_composer_is_the_runtime_function_not_an_emulator_copy():
    assert runtime.display_fields is pure.display_fields
    assert runtime.DisplayPreference is pure.DisplayPreference
    assert compose(payload())["track"] == "Example — Artist — Song 20:15"


def test_emulator_all_3840_field_choices_and_orders_match_runtime():
    for order in permutations(pure.DISPLAY_FIELDS):
        for mask in range(32):
            fields = [f for i, f in enumerate(pure.DISPLAY_FIELDS) if mask & (1 << i)]
            body = payload(fields=fields, order=list(order))
            expected = runtime.display_fields(runtime.DisplayPreference("CUSTOM", fields=tuple(fields), field_order=order),
                                              body["station"], body["metadata"], clock_text=body["clock"])
            actual = compose(body)
            assert (actual["track"], actual["artist"]) == (expected["track"], expected["artist"])


@pytest.mark.parametrize("body", [None, [], {"host": "example.invalid"}, payload(station="x" * 1025),
    payload(clock="25:00"), payload(clock=123), payload(metadata={"imageUrl": "https://example.invalid"}),
    payload(metadata={"track": 5}), payload(order=["title"] * 5), payload(fields=[True]),
    payload(fields=["title", "title"]), payload(fields="title"), payload(mode="REBOOT")])
def test_invalid_composition_cannot_expand_into_device_actions(body):
    with pytest.raises(ValueError):
        compose(body)


def test_legacy_partial_metadata_and_empty_custom_are_not_hidden():
    assert compose(payload(mode="TRACK_ARTIST", metadata={"track":"Song"}))["track"] == "Example"
    assert compose(payload(fields=[]))["track"] == ""
    assert compose(payload(metadata={"track":"<script>alert(1)</script>"}))["track"] == "Example — <script>alert(1)</script> 20:15"


def test_pure_import_does_not_load_db_settings_or_network(tmp_path):
    root = Path.cwd()
    before = set(tmp_path.iterdir())
    check = ("import sys; from tools.display_emulator import compose; "
             "assert not any(k in sys.modules for k in ['sqlalchemy','basswiesn.app.config','basswiesn.app.main','httpx'])")
    result = subprocess.run([sys.executable, "-c", check], cwd=tmp_path,
                            env={**os.environ, "PYTHONPATH": str(root)}, capture_output=True, text=True, timeout=10)
    assert result.returncode == 0, result.stderr
    assert set(tmp_path.iterdir()) == before


def test_audit_guard_blocks_outgoing_and_non_loopback_bind():
    for event in ("socket.connect", "socket.sendto", "socket.getaddrinfo", "socket.gethostbyname", "subprocess.Popen"):
        with pytest.raises(PermissionError):
            network_guard(event, ())
    with pytest.raises(PermissionError):
        network_guard("socket.bind", (None, ("0.0.0.0", 8840)))
    network_guard("socket.bind", (None, ("127.0.0.1", 0)))


@pytest.fixture
def emulator_url(tmp_path):
    script = Path("tools/display_emulator.py").resolve()
    proc = subprocess.Popen([sys.executable, str(script), "--port", "0"], cwd=tmp_path,
                            stdout=subprocess.PIPE, stderr=subprocess.PIPE, text=True)
    try:
        assert select.select([proc.stdout], [], [], 10)[0], "local emulator startup timed out"
        line = proc.stdout.readline().strip()
        assert line.startswith("Display emulator: http://127.0.0.1:"), line
        yield line.removeprefix("Display emulator: ").rstrip("/")
    finally:
        proc.terminate()
        proc.communicate(timeout=10)


def test_loopback_http_is_bounded_same_origin_and_has_no_hardware_routes(emulator_url, tmp_path):
    with httpx.Client(base_url=emulator_url, trust_env=False) as client:
        page = client.get("/")
        assert page.status_code == 200
        assert client.get("/review").status_code == 200
        assert client.get("/review-model.mjs").status_code == 200
        assert client.get("/fixed-layout.mjs").status_code == 200
        assert client.get("/boot").status_code == 200
        assert client.get("/boot-layout.mjs").status_code == 200
        assert client.get("/boot-mark.json").json()["text"] == "BASSWIESN"
        assert "unsafe-inline" not in page.headers["content-security-policy"]
        assert client.get("/api/contract").json()["firmware_execution"] is False
        assert client.get("/", headers={"Host":"evil.invalid"}).status_code == 403
        assert client.get("/", headers={"Sec-Fetch-Site":"cross-site"}).status_code == 403
        assert client.get("/../.env").status_code == 404
        assert client.get("/radio-files/opt/Bose/BoseApp").status_code == 404
        assert client.post("/api/compose", json=payload()).status_code == 403
        assert client.post("/api/compose", json=payload(), headers={"Origin":"http://evil.invalid"}).status_code == 403
        headers = {"Origin":emulator_url}
        assert client.post("/api/compose", json=payload(), headers=headers).json()["track"] == "Example — Artist — Song 20:15"
        assert client.post("/api/compose", content="x", headers=headers).status_code == 415
        assert client.post("/api/compose", content="[", headers={**headers,"Content-Type":"application/json"}).status_code == 400
        assert client.post("/api/compose", json={"station":"x" * 20000}, headers=headers).status_code == 413
        assert client.post("/api/devices/example/key", json={}, headers=headers).status_code == 404
    assert not (tmp_path / "data").exists()


@pytest.mark.browser
def test_boot_preview_preserves_status_and_previous_frame_on_no_write(emulator_url, tmp_path):
    from playwright.sync_api import expect, sync_playwright
    with sync_playwright() as pw:
        browser = pw.chromium.launch(headless=True, args=["--disable-background-networking"])
        try:
            for width in (1440, 430):
                page = browser.new_page(viewport={"width": width, "height": 1050}, reduced_motion="reduce")
                external, errors = [], []
                page.on("pageerror", lambda error: errors.append(str(error)))

                def local_only(route):
                    if not route.request.url.startswith(emulator_url + "/"):
                        external.append(route.request.url)
                        route.abort()
                    else:
                        route.continue_()

                page.route("**/*", local_only)
                page.goto(emulator_url + "/boot")
                expect(page.locator("#boot-status")).to_contain_text("Statusfeld unverändert")
                assert page.evaluate("document.documentElement.scrollWidth <= innerWidth")
                for kind in ("normal", "full"):
                    page.select_option("#progress-kind", kind)
                    for progress in (1, 25, 50, 75, 100):
                        page.locator("#progress").fill(str(progress))
                        page.locator("#progress").dispatch_event("input")
                        assert page.evaluate("""() => {
                            const pixels = id => document.querySelector(id).getContext('2d').getImageData(0,0,128,100).data;
                            const a=pixels('#boot-original'), b=pixels('#boot-proposed');
                            return a.slice(40*128*4,60*128*4).every((v,i)=>v===b[40*128*4+i]) &&
                                   a.some((v,i)=>v!==b[i]);
                        }""")
                previous = page.locator("#boot-proposed").evaluate("canvas => canvas.toDataURL()")
                page.locator("#progress").fill("0")
                page.locator("#progress").dispatch_event("input")
                expect(page.locator("#boot-status")).to_contain_text("kein neues Bild")
                assert previous == page.locator("#boot-proposed").evaluate("canvas => canvas.toDataURL()")
                page.select_option("#progress-kind", "normal")
                page.locator("#progress").fill("50")
                page.locator("#progress").dispatch_event("input")
                capture = os.getenv("DISPLAY_EMULATOR_SCREENSHOTS")
                if capture:
                    destination = Path(capture)
                    destination.mkdir(parents=True, exist_ok=True)
                    page.screenshot(path=str(destination / f"boot-preview-{width}.png"), full_page=True)
                assert not external and not errors, (external, errors)
                page.close()
        finally:
            browser.close()


@pytest.mark.browser
def test_display_emulator_browser_interaction_no_external_requests(emulator_url, tmp_path):
    from playwright.sync_api import expect, sync_playwright
    with sync_playwright() as pw:
        browser = pw.chromium.launch(headless=True, args=["--disable-background-networking"])
        try:
            for width in (1440, 430):
                context = browser.new_context(viewport={"width":width,"height":1050}, reduced_motion="reduce", accept_downloads=True)
                page = context.new_page()
                errors, external = [], []
                page.on("pageerror", lambda error: errors.append(str(error)))
                def guard(route):
                    if not route.request.url.startswith(emulator_url + "/"):
                        external.append(route.request.url)
                        route.abort()
                    else:
                        route.continue_()
                page.route("**/*", guard)
                page.goto(emulator_url)
                expect(page.locator("#payload")).to_contain_text("Unterwegs")
                assert page.locator("#page-kind").inner_text() == "PLAYING"
                page.locator("#concept").check()
                for index in range(8):
                    page.select_option("#scenario",str(index))
                    expect(page.locator("#payload")).to_contain_text("presentation")
                    assert page.evaluate("document.documentElement.scrollWidth <= innerWidth")
                assert page.locator("img,iframe").count() == 0
                assert "<img" in page.locator("#artist").input_value()
                page.select_option("#scenario","1")
                expect(page.locator("#payload")).to_contain_text("Nachtorchester")
                capture = os.getenv("DISPLAY_EMULATOR_SCREENSHOTS")
                if capture:
                    destination=Path(capture);destination.mkdir(parents=True,exist_ok=True)
                    page.screenshot(path=str(destination / f"display-emulator-{width}.png"),full_page=True)
                page.locator("#states [data-state=PAUSED]").click()
                assert page.locator("#page-kind").inner_text() == "PAUSED"
                page.locator("#states [data-state=PLAYING]").click()
                page.locator("#volume").fill("0")
                page.locator("#volume").dispatch_event("change")
                assert page.locator("#page-kind").inner_text() == "VOLUME"
                page.locator("#step").click();page.locator("#step").click()
                assert page.locator("#page-kind").inner_text() == "PLAYING"
                page.locator("#track").fill("New synthetic song")
                page.locator("#metadata").click()
                page.locator("#step").click();page.locator("#step").click()
                expect(page.locator("#payload")).to_contain_text("New synthetic song")
                page.locator("#minute").click()
                assert page.locator("#clock").inner_text() == "20:16"
                page.locator("#states [data-state=STANDBY]").click()
                assert page.locator("#page-kind").inner_text() == "STANDBY"
                page.get_by_text("Verlauf und lokale Dateien", exact=True).click()
                with page.expect_download() as info:
                    page.locator("#export").click()
                downloaded = tmp_path / f"replay-{width}.json"
                info.value.save_as(downloaded)
                value=json.loads(downloaded.read_text())
                assert value["kind"] == "DISPLAY_BEHAVIOR_MODEL"
                page.locator("#reset").click()
                page.locator("#import").set_input_files(downloaded)
                expect(page.locator("#page-kind")).to_have_text("STANDBY")
                page.locator("#import").set_input_files({"name":"bad.json","mimeType":"application/json","buffer":b'{"host":"example.invalid"}'})
                expect(page.locator("#status")).to_contain_text("Import abgelehnt")
                assert not errors and not external, (errors,external)
                context.close()
            # Exercise actual canvas geometry and reproducible scroll seeking,
            # independently of the reduced-motion accessibility profile above.
            context = browser.new_context(reduced_motion="no-preference")
            page = context.new_page()
            page.goto(emulator_url)
            page.select_option("#scenario", "1")
            expect(page.locator("#payload")).to_contain_text("Nachtorchester")
            first = page.locator("#display").evaluate("canvas => canvas.toDataURL()")
            for _ in range(4):
                page.locator("#step").click()
            moved = page.locator("#display").evaluate("canvas => canvas.toDataURL()")
            assert first != moved
            page.locator("#position").fill("3500")
            page.locator("#position").dispatch_event("input")
            assert page.locator("#display").evaluate("canvas => canvas.toDataURL()") == first
            page.get_by_text("Modell kalibrieren", exact=True).click()
            page.locator("#oled").uncheck()
            page.locator("#calibrate").click()
            assert page.locator("#page-kind").inner_text() == "NO_DISPLAY"
            assert page.locator("#display").evaluate("canvas => [...canvas.getContext('2d').getImageData(0,0,canvas.width,canvas.height).data].every((v,i) => i%4===3 ? v===255 : v===0)")
            context.close()
            # Deterministic race: the initial composition completes AFTER a
            # rejected import. Its informational reply must not erase the error.
            context = browser.new_context()
            page = context.new_page()
            held = []
            page.route(emulator_url + "/api/compose", lambda route: held.append(route))
            page.goto(emulator_url, wait_until="domcontentloaded")
            page.locator("#import").set_input_files({"name":"bad.json","mimeType":"application/json","buffer":b'{"host":"example.invalid"}'})
            expect(page.locator("#status")).to_contain_text("Import abgelehnt")
            assert held
            for route in held:
                route.continue_()
            expect(page.locator("#payload")).to_contain_text("presentation")
            expect(page.locator("#status")).to_contain_text("Import abgelehnt")
            context.close()
        finally:
            browser.close()


@pytest.mark.browser
def test_fixed_display_playback_matrix_and_overlay_controls(emulator_url, tmp_path):
    from playwright.sync_api import expect, sync_playwright

    with sync_playwright() as pw:
        browser = pw.chromium.launch(headless=True, args=["--disable-background-networking"])
        try:
            for width in (1440, 430):
                context = browser.new_context(viewport={"width": width, "height": 1000},
                                              reduced_motion="no-preference", accept_downloads=True)
                page = context.new_page()
                errors, external = [], []
                page.on("pageerror", lambda error: errors.append(str(error)))

                def guard(route):
                    if not route.request.url.startswith(emulator_url + "/"):
                        external.append(route.request.url)
                        route.abort()
                    else:
                        route.continue_()

                page.route("**/*", guard)
                page.goto(emulator_url + "/review")
                expect(page.locator("#status")).to_contain_text("Vorschau aktualisiert")
                canvas = page.locator("#layout")
                for index in range(13):
                    page.select_option("#layout-scenario", str(index))
                    expect(page.locator("#status")).to_contain_text("Vorschau aktualisiert")
                    expect(canvas).to_have_attribute("data-kind", "PLAYING")
                    assert canvas.evaluate("c => c.width === 128 && c.height === 100")
                    for state in ("PAUSED", "BUFFERING", "STOPPED", "STANDBY", "ERROR", "PLAYING"):
                        page.select_option("#layout-state", state)
                        expect(canvas).to_have_attribute("data-kind", state)
                    assert page.evaluate("document.documentElement.scrollWidth <= innerWidth")
                assert page.locator("img,iframe").count() == 0
                expect(page.locator("#layout-text")).to_contain_text("<img")

                page.select_option("#layout-source", "AUX")
                expect(page.locator("#layout-text")).to_contain_text("AUX-Eingang")
                assert "<img" not in canvas.get_attribute("aria-label")
                page.locator("#layout-volume").fill("0")
                expect(canvas).to_have_attribute("data-kind", "VOLUME")
                expect(canvas).to_have_attribute("aria-label", "VOLUME: Lautstärke 0")
                page.locator("#layout-mute").check()
                expect(canvas).to_have_attribute("aria-label", "VOLUME: Lautstärke 0, stumm")
                page.select_option("#layout-notice", "CRITICAL_BATTERY")
                expect(canvas).to_have_attribute("data-kind", "CRITICAL_BATTERY")
                page.locator("#layout-step").click()
                expect(canvas).to_have_attribute("data-kind", "CRITICAL_BATTERY")
                page.select_option("#layout-notice", "NONE")
                expect(canvas).to_have_attribute("data-kind", "PLAYING")
                page.select_option("#layout-scenario", "7")
                expect(page.locator("#status")).to_contain_text("Vorschau aktualisiert")
                expect(page.locator("#layout-volume")).to_have_value("5")
                expect(page.locator("#layout-mute")).not_to_be_checked()
                assert int(canvas.get_attribute("data-title-pages")) > 1
                page.locator("#layout-step").click()
                expect(canvas).to_have_attribute("data-title-page", "2")
                page.locator("#layout-minute").click()
                expect(page.locator("#layout-text")).to_contain_text("20:16")

                with page.expect_download() as info:
                    page.locator("#layout-export").click()
                downloaded = tmp_path / f"fixed-frame-{width}.json"
                info.value.save_as(downloaded)
                saved = json.loads(downloaded.read_text())
                assert saved["kind"] == "FIXED_LAYOUT_PROTOTYPE"
                assert saved["hardwareValidated"] is saved["firmwareExecution"] is False
                assert saved["frame"]["clock"] == "20:16"

                # Exercise the renderer, not just JSON, with the browser's font
                # metrics over every source/state/notice/zone combination.
                rendered = page.evaluate("""async () => {
                    const {initial,apply,STATES} = await import('/model.mjs');
                    const {fixedFrame,drawFixedFrame,FIXED_SCENARIOS,NOTICES} = await import('/fixed-layout.mjs');
                    const target = document.createElement('canvas'), metrics = document.createElement('canvas').getContext('2d');
                    let frames = 0;
                    for(const scenario of FIXED_SCENARIOS) {
                        let s = apply(initial(), {at:0,type:'SELECT',data:{source:scenario.source,station:scenario.station}});
                        s = apply(s,{at:0,type:'METADATA',data:{generation:1,track:scenario.track,artist:scenario.artist,other_info:''}});
                        for(const state of STATES) for(const notice of Object.keys(NOTICES)) for(const zone of ['solo','master','member']) {
                            const snapshot=apply(s,{at:2000,type:'STATE',data:{state}});
                            const frame=fixedFrame(snapshot,(size,text)=>{metrics.font=`${size}px Arial, sans-serif`;return metrics.measureText(text).width;},{notice,zone});
                            drawFixedFrame(target,frame);
                            const pixels=target.getContext('2d').getImageData(0,0,128,100).data;
                            for(let y=0;y<100;y++) for(let x=0;x<128;x++) {
                                if(x>=4 && x<124 && y>=3 && y<99) continue;
                                const i=(y*128+x)*4;
                                if(pixels[i]!==1 || pixels[i+1]!==1 || pixels[i+2]!==1 || pixels[i+3]!==255) throw Error('Drawing escaped the reserved frame');
                            }
                            frames++;
                        }
                    }
                    return frames;
                }""")
                assert rendered == 1170
                page.select_option("#layout-state", "STANDBY")
                expect(page.locator("#layout-text")).to_contain_text("Standby")
                page.select_option("#standby-format", "12")
                expect(page.locator("#layout-text")).to_contain_text("PM")
                page.locator("#standby-synced").uncheck()
                expect(page.locator("#layout-text")).to_contain_text("nicht synchronisiert")
                page.locator("#standby-enabled").uncheck()
                expect(page.locator("#layout-text")).to_contain_text("Anzeige aus")
                assert canvas.evaluate("c => [...c.getContext('2d').getImageData(0,0,128,100).data].every((v,i)=>i%4===3?v===255:v===1)")
                page.locator("#standby-enabled").check()
                page.locator("#standby-synced").check()
                page.locator("#standby-brightness").fill("80")
                page.locator("#layout-minute").click()
                expect(page.locator("#layout-text")).to_contain_text("PM")
                capture = os.getenv("DISPLAY_EMULATOR_SCREENSHOTS")
                if capture:
                    destination = Path(capture)
                    destination.mkdir(parents=True, exist_ok=True)
                    page.screenshot(path=str(destination / f"fixed-layout-{width}.png"), full_page=True)
                    page.locator("#fixed-panel").screenshot(path=str(destination / f"fixed-layout-panel-{width}.png"))
                assert not errors and not external, (errors, external)
                context.close()
        finally:
            browser.close()


@pytest.mark.browser
def test_display_review_is_offline_bounded_and_distinguishes_designs(emulator_url):
    from playwright.sync_api import expect, sync_playwright
    with sync_playwright() as pw:
        browser = pw.chromium.launch(headless=True, args=["--disable-background-networking"])
        try:
            for width in (1440, 430):
                context = browser.new_context(viewport={"width": width, "height": 1000})
                page = context.new_page()
                errors, external, posts = [], [], []
                page.on("pageerror", lambda error: errors.append(str(error)))

                def guard(route):
                    if not route.request.url.startswith(emulator_url + "/"):
                        external.append(route.request.url)
                        route.abort()
                    else:
                        if route.request.method == "POST":
                            posts.append(route.request.url)
                        route.continue_()

                page.route("**/*", guard)
                page.goto(emulator_url + "/review")
                expect(page.locator("#status")).to_contain_text("Vorschau aktualisiert")
                assert page.evaluate("document.documentElement.scrollWidth <= innerWidth")
                assert page.locator("canvas").count() == 4
                assert page.locator("#current").evaluate("c => c.width === 128 && c.height === 100")
                expect(page.locator("#pages-text")).to_contain_text("Titel")
                page.locator("#pages-next").click()
                expect(page.locator("#pages-text")).to_contain_text("Interpret")
                page.locator("#pages-next").click()
                expect(page.locator("#pages-text")).to_contain_text("Sender und Uhr")
                page.locator("#track").fill("<img src=x onerror=alert(1)> " * 5)
                page.locator("#compare").click()
                expect(page.locator("#layout-text")).to_contain_text("<img")
                assert page.locator("img,iframe").count() == 0
                assert page.evaluate("document.documentElement.scrollWidth <= innerWidth")
                before = len(posts)
                raw = bytes(range(256)) * 50
                page.locator("#reference").set_input_files({"name": "synthetic.gray8", "mimeType": "application/octet-stream", "buffer": raw})
                expect(page.locator("#reference-status")).to_contain_text("Originalbild lokal geladen")
                assert page.locator("#reference-display").evaluate(
                    "c => [...c.getContext('2d').getImageData(0,0,128,100).data].every((v,i) => i%4===3 ? v===255 : v===Math.floor(i/4)%256)"
                )
                page.locator("#reference").set_input_files({"name": "bad.png", "mimeType": "image/png", "buffer": b'<svg onload="alert(1)"></svg>'})
                expect(page.locator("#reference-status")).to_contain_text("Bild nicht geladen")
                page.locator("#font-file").set_input_files({"name": "bad.ttf", "mimeType": "font/ttf", "buffer": b'not really a font, just text'})
                expect(page.locator("#font-status")).to_contain_text("Schrift nicht geladen")
                assert len(posts) == before  # Files never uploaded, not even to loopback.
                assert all(url == emulator_url + "/api/compose" for url in posts)
                assert not errors and not external, (errors, external)
                capture = os.getenv("DISPLAY_EMULATOR_SCREENSHOTS")
                if capture:
                    page.locator("#track").fill("Unterwegs nach Hause")
                    page.locator("#compare").click()
                    expect(page.locator("#layout-text")).to_contain_text("Unterwegs")
                    destination = Path(capture)
                    destination.mkdir(parents=True, exist_ok=True)
                    page.screenshot(path=str(destination / f"display-review-{width}.png"), full_page=True)
                context.close()
        finally:
            browser.close()
