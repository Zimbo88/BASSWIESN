"""Human-facing browser evidence for the BASSWIESN 1.6 WebUI.

The browser talks to a real local Uvicorn socket.  Test data is seeded in the
isolated pytest database; visible functions are exercised through UI controls,
never by replacing their HTTP routes in the browser.
"""

from __future__ import annotations

import base64
from datetime import UTC, datetime, timedelta
import json
from pathlib import Path
import re
import socket
import threading
import time

import httpx
import pytest
import uvicorn
from playwright.sync_api import Page, expect, sync_playwright

from basswiesn.app import config, db as app_db
from basswiesn.app.main import create_web_app
from basswiesn.app.models import (
    AirPlayReadinessState,
    ArtworkCacheEntry,
    Device,
    DiagnosticEvent,
    MetadataState,
    PlaybackHealthState,
    ProviderHealthState,
    ReportingState,
    RestrictionState,
    Station,
)
from basswiesn.app.services.artwork import artwork_cache_key, choose_artwork


pytestmark = [
    pytest.mark.browser,
    pytest.mark.integration,
    pytest.mark.slow,
    pytest.mark.release,
]

DEVICE_ID = "BASSWIESN-SIM-160"
LIVE_ARTWORK_URL = "https://art.example/live.png?token=must-not-reach-browser"
STATION_ARTWORK_URL = "https://logos.example/station.png?key=must-not-reach-browser"
PNG_1X1 = base64.b64decode(
    "iVBORw0KGgoAAAANSUhEUgAAAAEAAAABCAQAAAC1HAwCAAAAC0lEQVR42mNk+A8AAQUBAScY42YAAAAASUVORK5CYII="
)


def _free_port() -> int:
    with socket.socket() as sock:
        sock.bind(("127.0.0.1", 0))
        return int(sock.getsockname()[1])


class LiveServer:
    def __init__(self) -> None:
        self.port = _free_port()
        self.url = f"http://127.0.0.1:{self.port}"
        self.server = uvicorn.Server(
            uvicorn.Config(
                create_web_app(background_tasks=False),
                host="127.0.0.1",
                port=self.port,
                log_level="warning",
                access_log=False,
            )
        )
        self.thread = threading.Thread(target=self.server.run, daemon=True)

    def __enter__(self) -> "LiveServer":
        self.thread.start()
        deadline = time.monotonic() + 10
        while time.monotonic() < deadline:
            try:
                if httpx.get(f"{self.url}/api/health", timeout=0.25).status_code == 200:
                    return self
            except Exception:
                time.sleep(0.05)
        raise RuntimeError("test web server did not start")

    def __exit__(self, exc_type, exc, tb) -> None:
        self.server.should_exit = True
        self.thread.join(timeout=5)


def _seed_research_timeline(data_dir: Path) -> None:
    now = datetime.now(UTC)
    artwork_dir = data_dir / "media" / "artwork-cache"
    artwork_dir.mkdir(parents=True, exist_ok=True)
    with app_db.SessionLocal() as db:
        station = Station(
            name="BASSWIESN Simulation Radio",
            stream_url="https://radio.example/simulation.mp3",
            image_url=STATION_ARTWORK_URL,
            provider="LOCAL_INTERNET_RADIO",
            provider_station_id="sim-160",
        )
        db.add_all([
            Device(
                device_id=DEVICE_ID,
                name="Sehr langer simulierter Küchenradio-Name für Responsive QA",
                ip_address="192.0.2.160",
                model="SoundTouch 20",
                firmware="27.0.6",
                identity_verified=True,
                reachable=True,
                info_xml=(
                    '<info deviceID="BASSWIESN-SIM-160"><name>Simuliertes Radio</name>'
                    '<type>SoundTouch 20</type><components><component>'
                    '<softwareVersion>27.0.6</softwareVersion></component></components></info>'
                ),
            ),
            station,
        ])
        db.add(
            PlaybackHealthState(
                device_id=DEVICE_ID,
                state="PLAYING",
                source_valid=True,
                stream_alive=True,
                position_advancing=True,
                provider_health="HEALTHY",
                reason="RADIO_READBACK_PLAYING",
                evidence_json='[{"source":"radio_readback","confidence":100}]',
                since=now - timedelta(minutes=20),
                observed_at=now,
            )
        )
        db.add(
            ProviderHealthState(
                device_id=DEVICE_ID,
                provider_id="LOCAL_INTERNET_RADIO",
                source="LOCAL_INTERNET_RADIO",
                availability="AVAILABLE",
                association="AVAILABLE",
                state="HEALTHY",
                evidence_json='[{"source":"provider_contract","confidence":100}]',
                last_success_at=now,
                since=now - timedelta(minutes=20),
                changed_at=now - timedelta(minutes=20),
                user_visible_reason="Lokaler Provider antwortet vertragsgemäß.",
            )
        )
        db.add(
            MetadataState(
                device_id=DEVICE_ID,
                station_name="BASSWIESN Simulation Radio",
                station_id="sim-160",
                track="Menschlicher Browser-Test",
                artist="BASSWIESN QA",
                album="Version 1.6.0",
                artwork_url=LIVE_ARTWORK_URL,
                artwork_provenance="STREAM",
                provider="LOCAL_INTERNET_RADIO",
                source="LOCAL_INTERNET_RADIO",
                provenance="STREAM",
                confidence=100,
                updated_at=now,
                stale=False,
            )
        )
        db.add(
            RestrictionState(
                device_id=DEVICE_ID,
                source_key="LOCAL_INTERNET_RADIO:sim-160",
                inactivity_timeout_s=0,
                timer_enabled=False,
                received_at=now,
                origin="BMX.Station",
                evidence_json='[{"source":"BMX.Restrictions","confidence":100}]',
            )
        )
        db.add(
            ReportingState(
                device_id=DEVICE_ID,
                provider_id="LOCAL_INTERNET_RADIO",
                state="SUCCESS",
                report_url="http://192.0.2.10:1516/bmx/orion/reporting",
                queue_depth=0,
                retry_count=0,
                next_due_at=now + timedelta(minutes=10),
                last_http_status=200,
                last_success_at=now,
            )
        )
        db.add(
            AirPlayReadinessState(
                device_id=DEVICE_ID,
                firmware_version="27.0.6",
                product_id="093B",
                variant="sm2",
                platform="ST20III",
                product_allowed=True,
                auth_hardware_expected=True,
                auth_hardware_detected=True,
                sts_registered=True,
                source_visible=True,
                mdns_visible=True,
                pairing_ready=True,
                ptp_ready=True,
                audio_ready=True,
                blocking_stage="NONE",
                confidence=100,
                evidence_json='[{"source":"synthetic_ui_fixture","confidence":100}]',
                observed_at=now,
            )
        )
        for index, (domain, code, message) in enumerate(
            (
                ("PROVIDER", "HEALTHY", "Provider HEALTHY"),
                ("PLAYBACK", "PLAYING", "Playback PLAYING"),
                ("METADATA", "UPDATED", "Metadata updated"),
                ("REPORTING", "SUCCESS", "Report OK"),
            )
        ):
            db.add(
                DiagnosticEvent(
                    event_id=f"ui-160-{index}",
                    occurred_at=now - timedelta(minutes=15 - index * 4),
                    device_id=DEVICE_ID,
                    domain=domain,
                    severity="INFO",
                    code=code,
                    message=message,
                    evidence_json='[{"confidence":100}]',
                )
            )
        live_choice = choose_artwork(
            image_url=LIVE_ARTWORK_URL,
            station_logo_url=STATION_ARTWORK_URL,
        )
        live_key = artwork_cache_key(
            live_choice,
            provider_id="LOCAL_INTERNET_RADIO",
            station_id="sim-160",
        )
        station_choice = choose_artwork(station_logo_url=STATION_ARTWORK_URL)
        station_key = artwork_cache_key(
            station_choice,
            provider_id="LOCAL_INTERNET_RADIO",
            station_id="sim-160",
        )
        for key in (live_key, station_key):
            (artwork_dir / f"{key}.png").write_bytes(PNG_1X1)
        expires_at = now + timedelta(hours=1)
        db.add_all([
            ArtworkCacheEntry(
                cache_key=live_key,
                device_id=DEVICE_ID,
                provider_id="LOCAL_INTERNET_RADIO",
                station_id="sim-160",
                source="IMAGE_URL",
                source_url_hash="live-browser-fixture",
                source_url_redacted="https://art.example/live.png",
                cached_path=str(artwork_dir / f"{live_key}.png"),
                mime_type="image/png",
                fetched_at=now,
                expires_at=expires_at,
            ),
            ArtworkCacheEntry(
                cache_key=station_key,
                provider_id="LOCAL_INTERNET_RADIO",
                station_id="sim-160",
                source="STATION",
                source_url_hash="station-browser-fixture",
                source_url_redacted="https://logos.example/station.png",
                cached_path=str(artwork_dir / f"{station_key}.png"),
                mime_type="image/png",
                fetched_at=now,
                expires_at=expires_at,
            ),
        ])
        db.commit()


def _ack_first_run(page: Page) -> None:
    warning = page.locator("#first-run-warning")
    if warning.is_visible():
        viewport = page.viewport_size
        card_box = warning.locator(".modal-card").bounding_box()
        assert viewport is not None and card_box is not None
        assert card_box["x"] >= 0 and card_box["y"] >= 0, card_box
        assert card_box["x"] + card_box["width"] <= viewport["width"] + 1, card_box
        assert card_box["y"] + card_box["height"] <= viewport["height"] + 1, card_box
        expect(page.locator("body")).to_have_class(re.compile(r"\bmodal-open\b"))
        page.locator("#first-run-warning-read").check()
        page.locator("#first-run-warning-ack").click()
        expect(warning).not_to_be_visible()
        expect(page.locator("body")).not_to_have_class(re.compile(r"\bmodal-open\b"))


def _assert_no_body_overflow(page: Page) -> None:
    dimensions = page.evaluate(
        """() => ({
          viewport: window.innerWidth,
          html: document.documentElement.scrollWidth,
          body: document.body.scrollWidth,
          offenders: Array.from(document.querySelectorAll('*'))
            .filter(node => {
              const box = node.getBoundingClientRect();
              const ancestry = [];
              for (let current = node; current && current !== document.body; current = current.parentElement) ancestry.push(current);
              const intentionallyScrollable = ancestry.some(current => {
                const overflow = getComputedStyle(current).overflowX;
                return (overflow === 'auto' || overflow === 'scroll')
                  && current.scrollWidth > current.clientWidth + 1;
              });
              return !intentionallyScrollable && !node.closest('[aria-hidden="true"]') && box.width > 0
                && (box.right > window.innerWidth + 1 || box.left < -1);
            })
            .slice(0, 12)
            .map(node => ({
              tag: node.tagName,
              id: node.id,
              className: String(node.className || ''),
              left: node.getBoundingClientRect().left,
              right: node.getBoundingClientRect().right,
              width: node.getBoundingClientRect().width,
              text: String(node.textContent || '').trim().slice(0, 80),
            })),
        })"""
    )
    evidence = json.dumps(dimensions, ensure_ascii=False, indent=2)
    assert dimensions["html"] <= dimensions["viewport"] + 1, evidence
    assert not dimensions["offenders"], evidence


def _open_view(page: Page, view: str) -> None:
    button = page.locator(f'.nav-button[data-view="{view}"]')
    if button.locator("xpath=ancestor::details").count():
        advanced = page.locator("details.advanced-nav")
        if not advanced.evaluate("node => node.open"):
            advanced.locator("summary").click()
        if (page.viewport_size or {}).get("width", 9999) <= 900:
            viewport = page.viewport_size
            drawer_box = advanced.locator(":scope > div").bounding_box()
            assert viewport is not None and drawer_box is not None
            assert drawer_box["x"] >= 0 and drawer_box["y"] >= 0, drawer_box
            assert drawer_box["x"] + drawer_box["width"] <= viewport["width"] + 1, drawer_box
            assert drawer_box["y"] + drawer_box["height"] <= viewport["height"] + 1, drawer_box
            expect(page.locator("body")).to_have_class(re.compile(r"\bnav-menu-open\b"))
    button.click()
    expect(page.locator("body")).not_to_have_class(re.compile(r"\bnav-menu-open\b"))
    expect(page.locator(f"#view-{view}")).to_be_visible()
    _assert_no_body_overflow(page)


def _assert_mobile_chrome(page: Page) -> None:
    topbar = page.locator(".topbar")
    nav = page.locator(".topnav")
    assert topbar.evaluate("node => getComputedStyle(node).position") == "sticky"
    assert nav.evaluate("node => getComputedStyle(node).overflowX") in {"auto", "scroll"}
    assert page.locator(".topnav > .nav-button").evaluate_all(
        "nodes => nodes.every(node => node.getBoundingClientRect().height >= 44)"
    )
    page.evaluate("window.scrollTo(0, Math.min(800, document.documentElement.scrollHeight - window.innerHeight))")
    page.wait_for_timeout(100)
    topbar_box = topbar.bounding_box()
    assert topbar_box is not None and abs(topbar_box["y"]) <= 1, topbar_box
    page.evaluate("window.scrollTo(0, 0)")


def _exercise_mobile_help_drawer(page: Page, artifact: Path) -> None:
    help_button = page.locator(".view.is-active .page-help-button")
    expect(help_button).to_be_visible()
    help_button.click()
    drawer = page.locator("#page-help")
    expect(drawer).to_have_attribute("aria-hidden", "false")
    page.wait_for_timeout(300)
    viewport = page.viewport_size
    drawer_box = drawer.bounding_box()
    assert viewport is not None and drawer_box is not None
    assert drawer_box["x"] >= 0 and drawer_box["y"] >= 0, drawer_box
    assert drawer_box["x"] + drawer_box["width"] <= viewport["width"] + 1, drawer_box
    assert drawer_box["y"] + drawer_box["height"] <= viewport["height"] + 1, drawer_box
    page.screenshot(path=str(artifact))
    page.locator("#page-help-close").click()
    expect(drawer).to_have_attribute("aria-hidden", "true")
    _assert_no_body_overflow(page)


def _enable_lab_mode(page: Page) -> None:
    _open_view(page, "system-settings")
    toggle = page.locator("#lab-mode")
    if not toggle.is_checked():
        toggle.check()
        page.locator('#system-settings-form button[type="submit"]').click()
        expect(page.locator("body")).to_have_class(re.compile("lab-mode"))


@pytest.fixture
def human_ui_server(monkeypatch, tmp_path):
    from basswiesn.app.services.setup_rebuild import coordinator as coordinator_module
    from basswiesn.app.routers import research_state as research_state_router

    monkeypatch.setenv("BASSWIESN_TEST_MODE", "true")
    config.get_settings.cache_clear()
    artwork_settings = config.get_settings().model_copy(update={"data_dir": tmp_path})
    monkeypatch.setattr(research_state_router, "get_settings", lambda: artwork_settings)
    coordinator_module._COORDINATOR = None
    _seed_research_timeline(tmp_path)
    try:
        with LiveServer() as server:
            yield server
    finally:
        coordinator_module._COORDINATOR = None
        config.get_settings.cache_clear()


def test_desktop_human_navigation_health_lab_and_evidence(human_ui_server):
    normal_views = (
        "dashboard", "features", "setup", "devices", "health", "controls",
        "stations", "presets", "multiroom", "schedules", "device-settings",
        "about", "system-settings",
    )
    lab_views = ("display", "media", "backup", "config", "telnet", "debug", "telemetry", "lab")
    artifact_root = Path("test-artifacts/1.6.0/desktop")
    artifact_root.mkdir(parents=True, exist_ok=True)

    with sync_playwright() as playwright:
        browser = playwright.chromium.launch(headless=True, args=["--no-sandbox"])
        for width, height in ((1920, 1080), (1440, 900)):
            page = browser.new_page(viewport={"width": width, "height": height})
            image_requests = []
            page.on("request", lambda request: image_requests.append(request.url) if request.resource_type == "image" else None)
            page.goto(human_ui_server.url, wait_until="networkidle")
            _ack_first_run(page)
            for view in normal_views:
                _open_view(page, view)
                page.screenshot(path=str(artifact_root / f"{width}x{height}-{view}.png"), full_page=True)

            _enable_lab_mode(page)
            for view in lab_views:
                _open_view(page, view)
                page.screenshot(path=str(artifact_root / f"{width}x{height}-{view}.png"), full_page=True)

            _open_view(page, "health")
            expect(page.locator("#playback-health-badge")).to_have_text("PLAYING")
            expect(page.locator("#provider-health-badge")).to_have_text("HEALTHY")
            expect(page.locator("#metadata-health-details")).to_contain_text("Menschlicher Browser-Test")
            expect(page.locator("#metadata-artwork")).to_have_attribute("src", re.compile(r"^/api/artwork-cache/[0-9a-f]{64}$"))
            expect(page.locator("#metadata-artwork")).to_have_js_property("complete", True)
            expect(page.locator("#reporting-health-details")).to_contain_text("Queue 0/20")
            expect(page.locator("#airplay-health-badge")).to_have_text("Bereit")
            expect(page.locator("#diagnostics-timeline")).to_contain_text("Report OK")
            _open_view(page, "stations")
            expect(page.locator("#stations-table .station-logo-small")).to_have_attribute(
                "src", re.compile(r"^/api/stations/[0-9]+/artwork/image$")
            )
            rendered_images = page.locator("img").evaluate_all("nodes => nodes.map(node => node.src)")
            assert rendered_images
            assert all(url.startswith(human_ui_server.url) for url in rendered_images), rendered_images
            assert all("art.example" not in url and "logos.example" not in url for url in image_requests), image_requests
            page.close()
        browser.close()


def test_mobile_human_setup_navigation_modals_scroll_and_rollback(human_ui_server):
    viewports = ((390, 844), (393, 852), (430, 932))
    mobile_views = (
        "dashboard", "features", "setup", "devices", "health", "controls",
        "stations", "presets", "multiroom", "schedules", "device-settings",
        "about", "system-settings",
    )
    lab_views = ("display", "media", "backup", "config", "telnet", "debug", "telemetry", "lab")
    artifact_root = Path("test-artifacts/1.6.0/mobile")
    artifact_root.mkdir(parents=True, exist_ok=True)

    with sync_playwright() as playwright:
        browser = playwright.chromium.launch(headless=True, args=["--no-sandbox"])
        for width, height in viewports:
            page = browser.new_page(viewport={"width": width, "height": height}, is_mobile=True, has_touch=True)
            page.goto(human_ui_server.url, wait_until="networkidle")
            _ack_first_run(page)
            for view in mobile_views:
                _open_view(page, view)
                page.screenshot(path=str(artifact_root / f"{width}x{height}-{view}.png"), full_page=True)

            _open_view(page, "features")
            _assert_mobile_chrome(page)
            _exercise_mobile_help_drawer(
                page,
                artifact_root / f"{width}x{height}-help-drawer.png",
            )

            _enable_lab_mode(page)
            for view in lab_views:
                _open_view(page, view)
                page.screenshot(path=str(artifact_root / f"{width}x{height}-{view}.png"), full_page=True)

            advanced = page.locator("details.advanced-nav")
            advanced.locator("summary").click()
            expect(page.locator("body")).to_have_class(re.compile(r"\bnav-menu-open\b"))
            page.screenshot(path=str(artifact_root / f"{width}x{height}-lab-menu.png"))
            advanced.locator("summary").click()
            expect(page.locator("body")).not_to_have_class(re.compile(r"\bnav-menu-open\b"))

            _open_view(page, "setup")
            page.locator("#setup-rebuild-host").select_option("192.0.2.10")
            radio = page.locator(f'[data-setup-rebuild-device="{DEVICE_ID}"]')
            radio.check()
            # Audio validation is an explicit opt-in in the real setup UI.
            page.locator("#setup-rebuild-playback").check()
            page.locator("#setup-rebuild-preview").click()
            expect(page.locator("#setup-rebuild-output")).to_contain_text('"ready_for_start": true')
            page.once("dialog", lambda dialog: dialog.accept())
            page.locator("#setup-rebuild-start").click()
            expect(page.locator("#setup-rebuild-status")).to_contain_text("completed", timeout=10_000)
            expect(page.locator("#setup-rebuild-output")).to_contain_text('"playback_test": "passed"')
            details = page.locator("#setup-rebuild-output").locator("xpath=ancestor::details")
            if not details.evaluate("node => node.open"):
                details.locator("summary").click()
            expect(page.locator("#setup-rebuild-rollback")).to_be_visible()
            expect(page.locator("#setup-rebuild-rollback")).to_be_enabled()
            page.once("dialog", lambda dialog: dialog.accept())
            page.locator("#setup-rebuild-rollback").click()
            expect(page.locator("#setup-rebuild-output")).to_contain_text('"state": "ROLLED_BACK"')
            _assert_no_body_overflow(page)
            page.evaluate("window.scrollTo(0, 0)")
            page.wait_for_timeout(100)
            page.screenshot(path=str(artifact_root / f"{width}x{height}-setup-complete-rollback.png"), full_page=True)
            page.close()
        browser.close()
