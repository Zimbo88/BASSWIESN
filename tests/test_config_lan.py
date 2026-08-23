from basswiesn.app import config


def test_lan_environment_overrides_auto_detection(monkeypatch, tmp_path):
    monkeypatch.setenv("BASSWIESN_LAN_HOST", "192.168.50.77")
    monkeypatch.setenv(
        "BASSWIESN_LAN_HOST_CANDIDATES",
        "192.168.50.77,10.20.30.40,127.0.0.1,192.168.50.77",
    )
    monkeypatch.setenv("BASSWIESN_LOCAL_BASE_URL", "http://192.168.50.77:1516")
    monkeypatch.setenv("BASSWIESN_WEB_BASE_URL", "http://192.168.50.77:1328")
    monkeypatch.setenv("BASSWIESN_DEBUG_BASE_URL", "http://192.168.50.77:1860")
    monkeypatch.setattr(config, "_default_lan_host", lambda: "172.18.0.2")
    config.get_settings.cache_clear()

    settings = config.get_settings()

    assert settings.lan_host == "192.168.50.77"
    assert settings.lan_host_configured is True
    assert settings.lan_host_candidates == ("192.168.50.77", "10.20.30.40")
    assert settings.local_base_url == "http://192.168.50.77:1516"
    assert settings.web_base_url == "http://192.168.50.77:1328"
    assert settings.debug_base_url == "http://192.168.50.77:1860"
    config.get_settings.cache_clear()


def test_auto_detection_skips_docker_network(monkeypatch):
    monkeypatch.setattr(
        config.socket,
        "gethostbyname_ex",
        lambda _hostname: ("rpi", [], ["172.18.0.2", "192.168.50.77"]),
    )
    monkeypatch.setattr(
        config.subprocess,
        "run",
        lambda *_args, **_kwargs: type("Result", (), {"stdout": ""})(),
    )

    assert config._default_lan_host() == "192.168.50.77"


def test_auto_detection_uses_local_route_metadata_without_socket_connect(monkeypatch):
    calls = []

    def fake_run(command, **_kwargs):
        calls.append(command)
        if command[:5] == ["ip", "-o", "-4", "route", "get"]:
            return type("Result", (), {"stdout": "192.0.2.1 via 192.168.50.1 dev wlan0 src 192.168.50.77 uid 1000\n"})()
        return type("Result", (), {"stdout": "2: wlan0 inet 192.168.50.77/24 brd 192.168.50.255 scope global wlan0\n"})()

    monkeypatch.setattr(config.subprocess, "run", fake_run)
    monkeypatch.setattr(
        config.socket.socket,
        "connect",
        lambda *_args, **_kwargs: (_ for _ in ()).throw(AssertionError("no socket connect allowed")),
    )

    assert config._default_lan_host() == "192.168.50.77"
    assert calls


def test_private_lan_ranges_are_radio_reachable():
    assert config._is_radio_reachable_ipv4("10.1.2.3") is True
    assert config._is_radio_reachable_ipv4("172.18.0.2") is True
    assert config._is_radio_reachable_ipv4("192.168.50.77") is True
    assert config._is_radio_reachable_ipv4("172.32.0.2") is False
    assert config.is_safe_radio_host("192.168.50.200") is True


def test_scan_cidr_comes_from_lan_host():
    assert config.scan_cidr_for_host("192.168.50.77") == "192.168.50.0/24"
    assert config.scan_cidr_for_host("192.168.1.50") == "192.168.1.0/24"
    assert config.scan_cidr_for_host("192.168.0.20") == "192.168.0.0/24"
    assert config.scan_cidr_for_host("10.0.0.50") == "10.0.0.0/24"
    assert config.scan_cidr_for_host("10.1.2.3") == "10.1.2.0/24"
    assert config.scan_cidr_for_host("172.16.5.10") == "172.16.5.0/24"
    assert config.scan_cidr_for_host("172.31.9.8") == "172.31.9.0/24"
    assert config.scan_cidr_for_host("127.0.0.1") == ""
    assert config.scan_cidr_for_host("172.32.0.2") == ""
    assert config.scan_cidr_for_host("content.api.bose.io") == ""


def test_empty_lan_environment_keeps_auto_detection(monkeypatch):
    monkeypatch.setenv("BASSWIESN_LAN_HOST", "")
    monkeypatch.setattr(config, "_default_lan_host", lambda: "192.168.50.9")
    config.get_settings.cache_clear()
    try:
        settings = config.get_settings()
        assert settings.lan_host == "192.168.50.9"
        assert settings.lan_host_configured is False
    finally:
        config.get_settings.cache_clear()


def test_bose_cloud_environment_is_not_used_as_lan_host(monkeypatch):
    monkeypatch.setenv("BASSWIESN_LAN_HOST", "content.api.bose.io")
    monkeypatch.setattr(config, "_default_lan_host", lambda: "")
    config.get_settings.cache_clear()
    try:
        settings = config.get_settings()
        assert settings.lan_host == ""
        assert settings.local_base_url == ""
    finally:
        config.get_settings.cache_clear()
import pytest as _pytest_marker
pytestmark = _pytest_marker.mark.unit
