import asyncio

from basswiesn.app.routers import api


def test_setup_override_uses_stock_config_as_template(monkeypatch):
    async def fake_run(_ip, _user, command, _timeout=12):
        assert "/opt/Bose/etc/SoundTouchSdkPrivateCfg.xml" in command
        return {"returncode": 0, "stdout": "__STOCK__\n<SoundTouchSdkPrivateCfg><margeServerUrl>old</margeServerUrl></SoundTouchSdkPrivateCfg>\n", "stderr": ""}

    monkeypatch.setattr(api, "_run_ssh_readonly_command", fake_run)
    result = asyncio.run(api._read_ssh_setup_override("192.0.2.10"))
    assert result["present"] is False
    assert result["template_present"] is True
    assert result["source"] == "stock"
    assert result["content"].startswith("<SoundTouchSdkPrivateCfg>")
import pytest as _pytest_marker
pytestmark = _pytest_marker.mark.unit
