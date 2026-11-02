"""A per-radio start-volume feature cannot silently become an unbounded zone."""
import pytest
from fastapi.testclient import TestClient

from basswiesn.app.main import create_web_app
from basswiesn.app.routers import multiroom

pytestmark = pytest.mark.integration


@pytest.mark.parametrize("path", ["/api/multiroom/remote-start", "/api/multiroom/set"])
@pytest.mark.parametrize("value", [0, 1, 5])
def test_explicit_safe_cap_is_rejected_before_radio_transport(monkeypatch, path, value):
    def forbidden(*args, **kwargs):
        pytest.fail("Unsupported safe grouping must not create a radio transport")

    monkeypatch.setattr(multiroom, "SoundTouchClient", forbidden)
    with TestClient(create_web_app(background_tasks=False)) as client:
        reply = client.post(path, json={"master_device_id": "SYNTHETIC",
            "member_device_ids": ["SYNTHETIC-MEMBER"], "safe_volume": value,
            "preserve_volumes": True, "dry_run": False, "memory_checked": True})
    assert reply.status_code == 409
    assert reply.json()["detail"] == {
        "code": "ZONE_SAFE_VOLUME_NOT_ESTABLISHED", "status": "BLOCKED",
        "command_sent": False, "automatic_retry": False,
        "message": "A fixed safe volume during zone creation is not established; no radio command was sent.",
    }
