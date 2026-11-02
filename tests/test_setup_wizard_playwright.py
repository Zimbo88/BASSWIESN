"""Browser guard for removal of the obsolete mocked setup wizard.

The real 1.6 setup journey is covered by ``test_setup_rebuild_browser_160``
and ``test_human_ui_160``.  This regression test only ensures that the old
eight-step wizard cannot reappear when LAB mode is enabled.
"""

from __future__ import annotations

import socket
import threading
import time

import httpx
import pytest
import uvicorn
from playwright.sync_api import expect, sync_playwright

from basswiesn.app.main import create_web_app


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


def test_only_the_real_setup_rebuild_is_visible_even_in_lab_mode():
    port = _free_port()
    app = create_web_app(background_tasks=False)
    server = uvicorn.Server(
        uvicorn.Config(
            app,
            host="127.0.0.1",
            port=port,
            log_level="warning",
            access_log=False,
        )
    )
    thread = threading.Thread(target=server.run, daemon=True)
    thread.start()
    url = f"http://127.0.0.1:{port}"
    try:
        deadline = time.monotonic() + 10
        while time.monotonic() < deadline:
            try:
                if httpx.get(f"{url}/api/health", timeout=0.25).status_code == 200:
                    break
            except Exception:
                time.sleep(0.05)
        else:
            raise RuntimeError("test web server did not start")

        assert httpx.post(
            f"{url}/api/system/settings",
            json={"first_run_warning_required": "false", "lab_mode": "true"},
            timeout=3,
        ).status_code == 200

        with sync_playwright() as playwright:
            browser = playwright.chromium.launch(headless=True, args=["--no-sandbox"])
            page = browser.new_page(viewport={"width": 1280, "height": 900})
            page.goto(url, wait_until="networkidle")
            page.locator('button.nav-button[data-view="setup"]').click()
            expect(page.locator("#setup-rebuild-assistant")).to_be_visible()
            expect(page.locator("#setup-layout-main")).to_be_hidden()
            expect(page.locator("#setup-oneclick")).to_be_hidden()
            expect(page.locator("#setup-risk-box")).to_be_hidden()
            browser.close()
    finally:
        server.should_exit = True
        thread.join(timeout=5)
