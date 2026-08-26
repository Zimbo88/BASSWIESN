import socket
import threading
import time

import httpx
import pytest
import uvicorn
from playwright.sync_api import sync_playwright

from basswiesn import __version__
from basswiesn.app.main import create_web_app


def _free_port() -> int:
    with socket.socket() as sock:
        sock.bind(("127.0.0.1", 0))
        return int(sock.getsockname()[1])


class _LiveServer:
    def __init__(self) -> None:
        self.port = _free_port()
        self.url = f"http://127.0.0.1:{self.port}"
        self.server = uvicorn.Server(uvicorn.Config(create_web_app(), host="127.0.0.1", port=self.port, log_level="warning", access_log=False))
        self.thread = threading.Thread(target=self.server.run, daemon=True)

    def __enter__(self):
        self.thread.start()
        deadline = time.time() + 10
        while time.time() < deadline:
            try:
                if httpx.get(f"{self.url}/api/health", timeout=0.25).status_code == 200:
                    return self
            except httpx.HTTPError:
                time.sleep(0.1)
        raise RuntimeError("test web server did not start")

    def __exit__(self, *_args) -> None:
        self.server.should_exit = True
        self.thread.join(timeout=5)


@pytest.mark.parametrize("width,height", [(1440, 900), (768, 1024), (430, 932), (390, 844), (375, 667), (320, 568)])
def test_dashboard_has_no_document_overflow_and_live_backend_version(width: int, height: int):
    with _LiveServer() as server, sync_playwright() as playwright:
        browser = playwright.chromium.launch(headless=True, args=["--no-sandbox"])
        page = browser.new_page(viewport={"width": width, "height": height})
        page_errors: list[str] = []
        failed_assets: list[str] = []
        page.on("pageerror", lambda error: page_errors.append(str(error)))
        page.on("response", lambda response: failed_assets.append(response.url) if "/static/" in response.url and response.status >= 400 else None)
        page.goto(server.url, wait_until="domcontentloaded")
        page.wait_for_function(f"document.querySelector('#server-identity').textContent.includes('v{__version__}')")

        measurements = page.evaluate("""() => ({
          scrollWidth: document.documentElement.scrollWidth,
          clientWidth: document.documentElement.clientWidth,
          shellRight: document.querySelector('.app-shell').getBoundingClientRect().right,
          viewport: innerWidth,
        })""")
        assert measurements["scrollWidth"] <= measurements["clientWidth"]
        assert measurements["shellRight"] <= measurements["viewport"] + 1
        assert page.locator("#server-identity").text_content().startswith(f"v{__version__}")
        assert page.locator(".topnav").evaluate("element => element.scrollWidth >= element.clientWidth")
        assert page_errors == []
        assert failed_assets == []
        browser.close()


def test_mobile_active_tab_is_scrolled_into_the_reachable_tab_strip():
    with _LiveServer() as server, sync_playwright() as playwright:
        settings = httpx.post(
            f"{server.url}/api/system/settings",
            json={"ui_mode": "standard", "show_startup_warning": "false", "first_run_warning_required": "false"},
            timeout=2,
        )
        assert settings.status_code == 200
        browser = playwright.chromium.launch(headless=True, args=["--no-sandbox"])
        page = browser.new_page(viewport={"width": 390, "height": 844})
        page.goto(server.url, wait_until="domcontentloaded")
        target = page.locator('.topnav > .nav-button[data-view="system-settings"]')
        target.click()
        page.wait_for_timeout(500)
        box = target.bounding_box()
        nav_box = page.locator(".topnav").bounding_box()
        assert box is not None and nav_box is not None
        assert box["x"] >= nav_box["x"] - 1
        assert box["x"] + box["width"] <= nav_box["x"] + nav_box["width"] + 1
        assert page.evaluate("document.documentElement.scrollWidth <= document.documentElement.clientWidth")
        browser.close()


@pytest.mark.parametrize("width,height", [(320, 568), (360, 640), (375, 667), (390, 844), (393, 852), (414, 896), (568, 320)])
def test_mobile_advanced_lab_menu_stays_in_viewport_and_all_items_are_clickable(width: int, height: int):
    with _LiveServer() as server, sync_playwright() as playwright:
        settings = httpx.post(
            f"{server.url}/api/system/settings",
            json={"ui_mode": "lab", "show_startup_warning": "false", "first_run_warning_required": "false"},
            timeout=2,
        )
        assert settings.status_code == 200

        browser = playwright.chromium.launch(headless=True, args=["--no-sandbox"])
        page = browser.new_page(viewport={"width": width, "height": height}, is_mobile=True, has_touch=True)
        page_errors: list[str] = []
        page.on("pageerror", lambda error: page_errors.append(str(error)))
        page.goto(server.url, wait_until="networkidle")
        page.locator(".advanced-nav > summary").click()
        page.wait_for_timeout(50)
        assert page.locator("body.nav-menu-open").count() == 1

        measurements = page.evaluate("""() => {
          const doc = document.documentElement;
          const menu = document.querySelector(".advanced-nav > div");
          const rect = menu.getBoundingClientRect();
          return {
            scrollWidth: doc.scrollWidth,
            clientWidth: doc.clientWidth,
            menuLeft: rect.left,
            menuRight: rect.right,
            menuWidth: rect.width,
            menuScrollWidth: menu.scrollWidth,
            menuClientWidth: menu.clientWidth,
            menuOverflowY: getComputedStyle(menu).overflowY,
          };
        }""")
        assert measurements["scrollWidth"] <= measurements["clientWidth"]
        assert measurements["menuLeft"] >= 0
        assert measurements["menuRight"] <= measurements["clientWidth"]
        assert measurements["menuWidth"] <= measurements["clientWidth"]
        assert measurements["menuScrollWidth"] <= measurements["menuClientWidth"] + 1
        assert measurements["menuOverflowY"] in {"auto", "scroll"}

        last_item = page.locator(".advanced-nav .nav-button").last
        last_item.scroll_into_view_if_needed()
        assert page.evaluate("""() => {
          const menu = document.querySelector('.advanced-nav > div');
          const item = menu?.querySelector('.nav-button:last-child');
          if (!menu || !item) return false;
          const menuRect = menu.getBoundingClientRect();
          const itemRect = item.getBoundingClientRect();
          return itemRect.top >= menuRect.top - 1 && itemRect.bottom <= menuRect.bottom + 1;
        }""")

        for view in ["display", "media", "backup", "config", "telnet", "debug", "telemetry", "lab"]:
            target = page.locator(f'.advanced-nav .nav-button[data-view="{view}"]')
            target.scroll_into_view_if_needed()
            target.click()
            page.wait_for_timeout(50)
            assert page.locator(f"#view-{view}.is-active").count() == 1
            assert page.locator("body.nav-menu-open").count() == 0
            page.locator(".advanced-nav > summary").click()

        page.evaluate("""() => {
          const modal = document.querySelector('#operation-overlay');
          modal.hidden = false;
          window.syncBodyScrollLock();
        }""")
        assert page.locator("body.modal-open").count() == 1
        page.locator("#operation-overlay-close").click()
        assert page.locator("body.modal-open").count() == 0
        assert page.evaluate("document.documentElement.scrollWidth <= document.documentElement.clientWidth")
        assert page.evaluate("document.scrollingElement.scrollHeight >= window.innerHeight")

        assert page_errors == []
        browser.close()


def test_web_language_switch_wires_all_supported_languages_to_visible_ui():
    with _LiveServer() as server, sync_playwright() as playwright:
        languages = httpx.get(f"{server.url}/api/system/settings", timeout=2).json()["web_languages"]
        assert len(languages) == 25

        browser = playwright.chromium.launch(headless=True, args=["--no-sandbox"])
        page = browser.new_page(viewport={"width": 1440, "height": 900})
        page_errors: list[str] = []
        page.on("pageerror", lambda error: page_errors.append(str(error)))

        for language in languages:
            code = language["code"]
            settings = httpx.post(
                f"{server.url}/api/system/settings",
                json={"web_language": code, "ui_mode": "standard", "show_startup_warning": "false", "first_run_warning_required": "false"},
                timeout=2,
            )
            assert settings.status_code == 200
            page.goto(server.url, wait_until="networkidle")
            page.wait_for_function("(code) => document.documentElement.lang === code", arg=code)
            expected = page.evaluate("""(code) => ({
              deviceSettings: window.BasswiesnI18n.catalogs[code].device_settings,
              settings: window.BasswiesnI18n.catalogs[code].settings,
              webDefaults: window.BasswiesnI18n.catalogs[code].webgui_defaults,
              aboutTitle: window.BasswiesnI18n.catalogs[code].about_basswiesn,
              save: window.BasswiesnI18n.catalogs[code].save_settings,
            })""", arg=code)
            assert page.locator('.topnav > .nav-button[data-view="device-settings"]').text_content() == expected["deviceSettings"]
            page.locator('.topnav > .nav-button[data-view="system-settings"]').click()
            assert page.locator("#view-system-settings .page-head h2").text_content() == expected["settings"]
            assert page.locator("#view-system-settings h3").first.text_content() == expected["webDefaults"]
            assert page.locator('#system-settings-form button[type="submit"]').text_content() == expected["save"]
            page.locator('.topnav > .nav-button[data-view="about"]').scroll_into_view_if_needed()
            page.locator('.topnav > .nav-button[data-view="about"]').click()
            assert page.locator("#view-about .page-head h2").text_content() == expected["aboutTitle"]
            assert page.locator("#view-about .about-release-copy p").count() >= 12
            about_text = page.locator("#view-about").inner_text()
            assert "400" in about_text
            assert "Raspberry Pi 5" in about_text
            if code != "en":
                assert "BASSWIESN grew out of more than 400 hours" not in about_text
                assert "Thank you. Greetings from Bavaria" not in about_text

        assert page_errors == []
        browser.close()
import pytest as _pytest_marker
pytestmark = [_pytest_marker.mark.browser, _pytest_marker.mark.integration, _pytest_marker.mark.slow, _pytest_marker.mark.release]
