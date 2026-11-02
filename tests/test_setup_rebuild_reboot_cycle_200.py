from __future__ import annotations

import asyncio
import pytest

from basswiesn.app.services.setup_rebuild.radio_adapter import RadioSetupAdapter


@pytest.mark.parametrize("settles", [True, False])
def test_mute_recovery_observes_delayed_readback_without_repeating_toggle(monkeypatch, settles):
    class Client:
        reads = 0
        writes = []
        async def get_xml(self, path):
            assert path == "/volume"
            self.reads += 1
            muted = "false" if settles and self.reads >= 4 else "true"
            return f'<volume><actualvolume>1</actualvolume><muteenabled>{muted}</muteenabled></volume>'
        async def post_xml(self, path, body):
            assert path == "/key"
            self.writes.append(body)
            return '<status>OK</status>'
    async def no_sleep(_):
        pass
    monkeypatch.setattr(asyncio, "sleep", no_sleep)
    client = Client()
    operation = RadioSetupAdapter()._set_mute(client, False)
    if settles:
        assert asyncio.run(operation) == (1, False)
    else:
        with pytest.raises(RuntimeError, match="VOLUME_SAFETY_LOCK"):
            asyncio.run(operation)
    assert len(client.writes) == 2
    assert 'state="press"' in client.writes[0]
    assert 'state="release"' in client.writes[1]
    assert client.reads <= 21


def test_reboot_readback_ignores_pre_shutdown_http_success(monkeypatch):
    adapter = RadioSetupAdapter(reboot_timeout=30)
    observations: list[str] = []
    outcomes: list[object] = [
        {"verified": True, "marker": "pre-reboot"},
        OSError("radio offline"),
        {"verified": True, "marker": "post-reboot"},
    ]

    async def identify(_row):
        outcome = outcomes.pop(0)
        if isinstance(outcome, BaseException):
            observations.append("offline")
            raise outcome
        observations.append(str(outcome["marker"]))
        return outcome

    async def no_sleep(_seconds):
        return None

    monkeypatch.setattr(adapter, "identify", identify)
    monkeypatch.setattr(
        "basswiesn.app.services.setup_rebuild.radio_adapter.asyncio.sleep",
        no_sleep,
    )

    result = asyncio.run(adapter._wait_for_reboot_identity(object()))

    assert result["marker"] == "post-reboot"
    assert observations == ["pre-reboot", "offline", "post-reboot"]
