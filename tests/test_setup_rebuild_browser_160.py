import socket
import threading
import time
from pathlib import Path

import httpx
import pytest
import uvicorn
from playwright.sync_api import expect, sync_playwright

from basswiesn.app import config
from basswiesn.app.main import create_web_app
from basswiesn.app.models import Device


pytestmark = [
    pytest.mark.browser,
    pytest.mark.integration,
    pytest.mark.slow,
    pytest.mark.release,
]


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

    def __enter__(self):
        self.thread.start()
        deadline = time.monotonic() + 10
        while time.monotonic() < deadline:
            try:
                if httpx.get(f"{self.url}/api/health", timeout=0.25).status_code == 200:
                    return self
            except Exception:
                time.sleep(0.05)
        raise RuntimeError("test web server did not start")

    def __exit__(self, exc_type, exc, tb):
        self.server.should_exit = True
        self.thread.join(timeout=5)


@pytest.mark.parametrize(
    ("width", "height", "artifact_kind"),
    [
        (1920, 1080, "desktop"),
        (1440, 900, "desktop"),
        (390, 844, "mobile"),
        (393, 852, "mobile"),
        (430, 932, "mobile"),
    ],
)
def test_human_setup_preview_start_progress_readback_and_rollback(
    monkeypatch,
    width,
    height,
    artifact_kind,
):
    """No route mocks: browser -> HTTP API -> coordinator -> isolated DB."""

    from basswiesn.app.services.setup_rebuild import coordinator as coordinator_module

    monkeypatch.setenv("BASSWIESN_TEST_MODE", "true")
    config.get_settings.cache_clear()
    coordinator_module._COORDINATOR = None
    try:
        with LiveServer() as server, sync_playwright() as playwright:
            browser = playwright.chromium.launch(headless=True, args=["--no-sandbox"])
            page = browser.new_page(viewport={"width": width, "height": height})
            page.goto(server.url, wait_until="networkidle")

            warning = page.locator("#first-run-warning")
            if warning.is_visible():
                page.locator("#first-run-warning-read").check()
                page.locator("#first-run-warning-ack").click()

            page.locator('button.nav-button[data-view="setup"]').click()
            expect(page.locator("#setup-rebuild-assistant")).to_be_visible()
            page.locator("#setup-rebuild-host").select_option("192.0.2.10")
            expect(page.locator("#setup-rebuild-host")).to_have_value("192.0.2.10")
            radio = page.locator('[data-setup-rebuild-device="BASSWIESN-SIM-160"]')
            expect(radio).to_be_visible()
            expect(radio).not_to_be_checked()
            if width <= 760:
                card_box = radio.locator("xpath=ancestor::article").bounding_box()
                assert card_box is not None and card_box["height"] < 420
                assert page.evaluate(
                    "document.documentElement.scrollWidth <= window.innerWidth + 1"
                )
            radio.check()
            expect(page.locator('[data-setup-audio-safety="BASSWIESN-SIM-160"]')).to_be_visible()

            # Playback is an audible side effect and therefore deliberately
            # opt-in.  The human browser flow must select it visibly before a
            # successful playback readback can be expected.
            playback_test = page.locator("#setup-rebuild-playback")
            expect(playback_test).not_to_be_checked()
            playback_test.check()
            expect(playback_test).to_be_checked()

            page.locator("#setup-rebuild-preview").click()
            expect(page.locator("#setup-rebuild-output")).to_contain_text('"engine": "setup-rebuild-v2"')
            expect(page.locator("#setup-rebuild-output")).to_contain_text('"ready_for_start": true')
            expect(page.locator("#setup-rebuild-details")).to_have_attribute("open", "")
            page.wait_for_timeout(1500)
            expect(page.locator("#setup-rebuild-output")).to_contain_text('"ready_for_start": true')

            page.once("dialog", lambda dialog: dialog.accept())
            page.locator("#setup-rebuild-start").click()
            expect(page.locator("#setup-rebuild-status")).to_contain_text("running")
            expect(page.locator("#setup-rebuild-status")).to_contain_text("completed", timeout=10_000)
            expect(page.locator("#setup-rebuild-output")).to_contain_text('"playback_test": "passed"')
            expect(page.locator("#setup-rebuild-rollback")).to_be_enabled()

            page.once("dialog", lambda dialog: dialog.accept())
            page.locator("#setup-rebuild-rollback").click()
            expect(page.locator("#setup-rebuild-output")).to_contain_text('"state": "ROLLED_BACK"')

            artifact = Path(
                f"test-artifacts/1.6.0/{artifact_kind}/setup-test-mode-{width}x{height}.png"
            )
            artifact.parent.mkdir(parents=True, exist_ok=True)
            page.evaluate("window.scrollTo(0, 0)")
            page.wait_for_timeout(100)
            page.screenshot(path=str(artifact), full_page=True)
            browser.close()
    finally:
        coordinator_module._COORDINATOR = None
        config.get_settings.cache_clear()


def test_fresh_database_discovery_requires_visible_user_action_before_setup_population(
    monkeypatch,
):
    """Fresh Setup stays passive until the human explicitly starts discovery."""

    from basswiesn.app.routers import setup_rebuild as setup_rebuild_router
    from basswiesn.app.services.setup_rebuild import coordinator as coordinator_module

    device_id = "FRESH-SCAN-160"
    info_xml = (
        f'<info deviceID="{device_id}"><name>Frisch erkanntes Radio</name>'
        "<type>SoundTouch 20</type><components><component>"
        "<softwareVersion>27.0.6.46330.5043500 epdbuild.test</softwareVersion>"
        "</component></components><moduleType>sm2</moduleType><variant>spotty</variant></info>"
    )

    discovery_calls = []

    async def fake_discovery(db, *, timeout_seconds):
        discovery_calls.append(timeout_seconds)
        db.add(Device(
            device_id=device_id,
            ip_address="192.0.2.31",
            name="Frisch erkanntes Radio",
            model="SoundTouch 20",
            discovery_method="ssdp",
            identity_verified=True,
        ))
        return {
            "devices": [{"device_id": device_id, "ip_address": "192.0.2.31"}],
            "errors": [],
        }

    class FakeInfoClient:
        def __init__(self, _host, _device_id="", **_kwargs):
            pass

        async def get_xml(self, path):
            assert path == "/info"
            return info_xml

    monkeypatch.setenv("BASSWIESN_TEST_MODE", "true")
    config.get_settings.cache_clear()
    monkeypatch.setattr(
        setup_rebuild_router,
        "_ensure_test_mode_simulation_device",
        lambda _db: None,
    )
    monkeypatch.setattr(setup_rebuild_router, "manual_discovery_test", fake_discovery)
    monkeypatch.setattr(setup_rebuild_router, "_explicit_identity_client", FakeInfoClient)
    coordinator_module._COORDINATOR = None
    try:
        with LiveServer() as server, sync_playwright() as playwright:
            browser = playwright.chromium.launch(headless=True, args=["--no-sandbox"])
            page = browser.new_page(viewport={"width": 1440, "height": 900})
            page.goto(server.url, wait_until="networkidle")

            warning = page.locator("#first-run-warning")
            if warning.is_visible():
                page.locator("#first-run-warning-read").check()
                page.locator("#first-run-warning-ack").click()

            page.locator('button.nav-button[data-view="setup"]').click()
            radio = page.locator(f'[data-setup-rebuild-device="{device_id}"]')
            expect(radio).to_have_count(0)
            expect(page.locator("#setup-rebuild-devices")).to_contain_text(
                "Noch kein geeignetes Radio"
            )
            expect(page.locator("#setup-rebuild-discover")).to_be_visible()
            assert discovery_calls == []

            page.locator("#setup-rebuild-discover").click()
            expect(page.locator("#setup-rebuild-discovery-status")).to_contain_text(
                "1 von 1",
                timeout=10_000,
            )
            assert discovery_calls == [3]

            expect(radio).to_be_visible(timeout=10_000)
            expect(radio.locator("xpath=ancestor::article")).to_contain_text(
                "Frisch erkanntes Radio"
            )
            expect(radio).not_to_be_checked()
            artifact = Path(
                "test-artifacts/2.0.0/fresh-install/"
                "fresh-db-explicit-discovery-1440x900.png"
            )
            artifact.parent.mkdir(parents=True, exist_ok=True)
            page.screenshot(path=str(artifact), full_page=True)
            browser.close()
    finally:
        coordinator_module._COORDINATOR = None
        config.get_settings.cache_clear()
