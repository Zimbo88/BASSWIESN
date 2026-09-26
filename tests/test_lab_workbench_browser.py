"""Real browser clicks against synthetic local state, no radio transport."""
from datetime import UTC, datetime, timedelta

import httpx
from playwright.sync_api import expect, sync_playwright
import pytest

from basswiesn.app import db as app_db
from basswiesn.app.main import create_web_app
from basswiesn.app.models import Device, MetadataState, PlayHistory, Station
from test_mobile_release import _LiveServer

pytestmark = [pytest.mark.browser, pytest.mark.integration]


def seed():
    now = datetime.now(UTC)
    with app_db.SessionLocal() as db:
        station=Station(name="Example Station",stream_url="https://example.invalid/live.mp3",stream_format="mp3")
        db.add_all([station,Device(device_id="LAB-BROWSER",name="Example Radio",ip_address="192.0.2.42")])
        db.flush()
        db.add(PlayHistory(device_id="LAB-BROWSER",station_id=station.id,station_name=station.name,
                           started_at=now-timedelta(minutes=10),ended_at=now-timedelta(minutes=1)))
        db.add(MetadataState(device_id="LAB-BROWSER",track="Example Track",artist="Example Artist",
                             provenance="STREAM",updated_at=now,stale=False))
        db.commit()


@pytest.mark.parametrize("language", ["de", "en"])
@pytest.mark.parametrize("engine,width", [("chromium",1440),("chromium",390),("webkit",430)])
def test_workbench_clicks_downloads_and_mobile_layout(language, engine, width, tmp_path):
    seed()
    server=_LiveServer()
    server.server.config.app=create_web_app(background_tasks=False)
    with server, sync_playwright() as pw:
        httpx.post(server.url+"/api/system/settings",json={"ui_mode":"lab","web_language":language,
            "show_startup_warning":"false","first_run_warning_required":"false"}).raise_for_status()
        browser=getattr(pw,engine).launch(headless=True)
        page=browser.new_page(viewport={"width":width,"height":932},has_touch=width<500,is_mobile=width<500)
        requests, errors, plays=[],[],[]
        page.on("pageerror",lambda error:errors.append(str(error)))
        def route(r):
            if not r.request.url.startswith(server.url+"/"):
                return r.abort()
            path=r.request.url.removeprefix(server.url)
            if path.startswith("/api/lab/workbench"):
                requests.append(path)
            if path.endswith("/play"):
                plays.append(r.request.post_data_json)
                return r.fulfill(json={"verified":True,"readback":{"volume":1}})
            return r.continue_()
        page.route("**/*",route)
        page.clock.install()
        page.goto(server.url,wait_until="networkidle")
        page.wait_for_function('document.body.classList.contains("lab-mode")')
        assert requests==[]  # no workbench fetch on application startup
        page.locator(".advanced-nav summary").click()
        page.locator('[data-view="lab"]').click()
        panel=page.locator("#lab-workbench")
        expect(panel).to_be_visible()
        panel.locator('[data-workbench="load"]').click()
        expect(panel.locator("#workbench-recents")).to_contain_text("Example Station")
        expect(panel).to_contain_text("Example Track")
        assert plays==[]
        assert page.evaluate("document.documentElement.scrollWidth <= innerWidth")
        panel.screenshot(path=str(tmp_path/"workbench.png"))

        # Each new tool is operated by an actual browser click.
        panel.get_by_text("Fernbedienungs-Direktzugang" if language=="de" else "Remote shortcut",exact=True).click()
        panel.locator('[data-workbench="qr"]').click()
        expect(panel.locator(".workbench-qr")).to_be_visible()
        page.wait_for_function('document.querySelector(".workbench-qr")?.naturalWidth > 0')
        expect(panel.locator('a[href$="/remote/LAB-BROWSER"]')).to_be_visible()

        panel.locator("#workbench-snapshots summary").click()
        panel.locator("#workbench-label").fill("Before test")
        panel.locator('[data-workbench="capture"]').click()
        expect(panel.locator("#workbench-snapshots")).to_contain_text("Before test")
        panel.locator('[data-workbench="compare"]').click()
        expect(panel.locator("#workbench-snapshots")).to_contain_text("Keine inhaltlichen Unterschiede" if language=="de" else "No content differences")
        with page.expect_download() as info:
            panel.locator('[data-workbench="download"]').click()
        info.value.save_as(tmp_path/"snapshot.json")
        assert "LAB-BROWSER" not in (tmp_path/"snapshot.json").read_text()

        panel.get_by_text("Hörzeiten exportieren" if language=="de" else "Export listening times",exact=True).click()
        with page.expect_download() as info:
            panel.locator('[data-workbench="csv"]').click()
        info.value.save_as(tmp_path/"listening.csv")
        assert "Example Station" in (tmp_path/"listening.csv").read_text()

        page.once("dialog",lambda dialog:dialog.dismiss())
        panel.locator('[data-workbench="play"]').click()
        assert plays==[]
        page.once("dialog",lambda dialog:dialog.accept())
        panel.locator('[data-workbench="play"]').click()
        expect(panel.locator("#workbench-status")).to_contain_text("Wiedergabeanfrage abgeschlossen" if language=="de" else "Playback request completed")
        assert len(plays)==1 and plays[0]["safe_volume"]==1 and plays[0]["approve"] is True
        assert page.evaluate("document.documentElement.scrollWidth <= innerWidth")

        page.once("dialog",lambda dialog:dialog.accept())
        panel.locator('[data-workbench="remove"]').click()
        expect(panel.locator("#workbench-snapshots")).not_to_contain_text("Before test")
        # Optional browser refresh hits only cache routes, and stops outside LAB.
        panel.locator("#workbench-auto").check()
        old_reads=len([p for p in requests if "/overview?" in p])
        page.clock.fast_forward(10100)
        expect(panel.locator("#workbench-auto")).to_be_checked()
        page.wait_for_function('!document.querySelector("#lab-workbench [data-workbench=load]").disabled')
        assert len([p for p in requests if "/overview?" in p])>old_reads
        page.locator("#ui-mode-switch").select_option("standard")
        expect(panel).not_to_be_visible()
        last_count=len(requests)
        page.clock.fast_forward(20100)
        assert len(requests)==last_count
        assert httpx.get(server.url+"/api/lab/workbench/devices").status_code==409
        assert errors==[]
        browser.close()


@pytest.mark.parametrize("mode", ["easy","standard"])
def test_workbench_is_absent_from_normal_modes_and_no_background_calls(mode):
    server=_LiveServer();server.server.config.app=create_web_app(background_tasks=False)
    with server, sync_playwright() as pw:
        httpx.post(server.url+"/api/system/settings",json={"ui_mode":mode,
            "show_startup_warning":"false","first_run_warning_required":"false"}).raise_for_status()
        browser=pw.chromium.launch(headless=True)
        page=browser.new_page()
        calls=[]
        page.on("request",lambda req:calls.append(req.url) if "/api/lab/workbench" in req.url else None)
        page.goto(server.url,wait_until="networkidle")
        expect(page.locator("#lab-workbench")).not_to_be_visible()
        assert calls==[]
        browser.close()
