from __future__ import annotations

import asyncio

from basswiesn.app.services.setup_rebuild.radio_adapter import RadioSetupAdapter


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
