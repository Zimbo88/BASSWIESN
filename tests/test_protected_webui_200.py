"""Browser proof that a passive WebUI load never contacts protected devices."""

from __future__ import annotations

import ipaddress
from pathlib import Path
import socket
import threading
import time

import httpx
import pytest
import uvicorn
from playwright.sync_api import sync_playwright

from basswiesn.app import db as app_db
from basswiesn.app.config import get_settings
from basswiesn.app.main import create_web_app
from basswiesn.app.models import Device


pytestmark = [pytest.mark.browser, pytest.mark.integration, pytest.mark.release]

PROTECTED_IP = "192.0.2.25"
PROTECTED_DEVICE_ID = "TEST-PROTECTED-DEVICE"


def _free_port() -> int:
    with socket.socket() as sock:
        sock.bind(("127.0.0.1", 0))
        return int(sock.getsockname()[1])


def _is_loopback_host(value: object) -> bool:
    host = str(value or "").strip().strip("[]")
    if host in {"localhost", "ip6-localhost"}:
        return True
    try:
        return ipaddress.ip_address(host).is_loopback
    except ValueError:
        return False


@pytest.mark.parametrize("stored_protected_device", [False, True])
def test_passive_webui_load_opens_zero_radio_transports(
    monkeypatch, tmp_path, stored_protected_device, request
):
    monkeypatch.setenv("PROTECTED_DEVICE_IPS", PROTECTED_IP)
    monkeypatch.setenv("PROTECTED_DEVICE_IDS", PROTECTED_DEVICE_ID)
    get_settings.cache_clear()
    request.addfinalizer(get_settings.cache_clear)
    if stored_protected_device:
        with app_db.SessionLocal() as db:
            db.add(
                Device(
                    device_id=PROTECTED_DEVICE_ID,
                    ip_address=PROTECTED_IP,
                    name="Vollständig geschütztes Radio",
                    model="SoundTouch 10",
                    reachable=True,
                )
            )
            db.commit()

    blocked_attempts: list[tuple[str, str, int]] = []
    protected_attempts: list[tuple[str, str, int]] = []
    original_connect = socket.socket.connect
    original_connect_ex = socket.socket.connect_ex
    original_sendto = socket.socket.sendto
    original_create_connection = socket.create_connection

    def inspect_address(operation: str, address: object) -> None:
        if not isinstance(address, tuple) or not address:
            return
        host = str(address[0])
        port = int(address[1]) if len(address) > 1 else 0
        if _is_loopback_host(host):
            return
        attempt = (operation, host, port)
        blocked_attempts.append(attempt)
        if host == PROTECTED_IP:
            protected_attempts.append(attempt)
        raise OSError(f"test blocked non-loopback transport: {operation} {host}:{port}")

    def guarded_connect(sock, address):
        inspect_address("connect", address)
        return original_connect(sock, address)

    def guarded_connect_ex(sock, address):
        inspect_address("connect_ex", address)
        return original_connect_ex(sock, address)

    def guarded_sendto(sock, data, *args):
        address = args[-1] if args else None
        inspect_address("sendto", address)
        return original_sendto(sock, data, *args)

    def guarded_create_connection(address, *args, **kwargs):
        inspect_address("create_connection", address)
        return original_create_connection(address, *args, **kwargs)

    monkeypatch.setattr(socket.socket, "connect", guarded_connect)
    monkeypatch.setattr(socket.socket, "connect_ex", guarded_connect_ex)
    monkeypatch.setattr(socket.socket, "sendto", guarded_sendto)
    monkeypatch.setattr(socket, "create_connection", guarded_create_connection)

    port = _free_port()
    url = f"http://127.0.0.1:{port}"
    server = uvicorn.Server(
        uvicorn.Config(
            create_web_app(background_tasks=False),
            host="127.0.0.1",
            port=port,
            log_level="warning",
            access_log=False,
        )
    )
    thread = threading.Thread(target=server.run, daemon=True)
    thread.start()
    deadline = time.monotonic() + 10
    while time.monotonic() < deadline:
        try:
            if httpx.get(f"{url}/api/health", timeout=0.25).status_code == 200:
                break
        except Exception:
            time.sleep(0.05)
    else:
        raise AssertionError("local WebUI did not start")

    browser_requests: list[str] = []
    try:
        with sync_playwright() as playwright:
            browser = playwright.chromium.launch(headless=True, args=["--no-sandbox"])
            page = browser.new_page(viewport={"width": 1440, "height": 900})
            page.on("request", lambda request: browser_requests.append(request.url))
            page.goto(url, wait_until="networkidle")
            page.screenshot(
                path=str(tmp_path / "protected-passive-webui.png"),
                full_page=True,
            )
            browser.close()
    finally:
        server.should_exit = True
        thread.join(timeout=5)

    assert protected_attempts == []
    assert all(PROTECTED_IP not in request_url for request_url in browser_requests)
    # Health may inspect BASSWIESN's own cloud/debug listener through the
    # machine's LAN address. No passive load may contact a stored radio.
    own_host = get_settings().lan_host
    assert all(host == own_host for _operation, host, _port in blocked_attempts)
