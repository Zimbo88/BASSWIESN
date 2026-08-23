from __future__ import annotations

import asyncio
from pathlib import Path
from types import SimpleNamespace

from basswiesn.app.services.setup_rebuild.radio_adapter import RadioSetupAdapter
from basswiesn.app.services.setup_rebuild.server_target import ServerTarget


class _InfoClient:
    def __init__(self, account_id: str) -> None:
        self.account_id = account_id
        self.posts: list[tuple[str, str]] = []

    async def get_xml(self, path: str) -> str:
        assert path == "/info"
        return (
            '<info deviceID="112233445566"><name>Kitchen</name>'
            f"<margeAccountUUID>{self.account_id}</margeAccountUUID></info>"
        )

    async def post_xml(self, path: str, body: str) -> str:
        self.posts.append((path, body))
        return "<status />"


def test_existing_local_account_still_reapplies_route_after_reboot(monkeypatch, tmp_path):
    account_id = "3147386"
    client = _InfoClient(account_id)
    adapter = RadioSetupAdapter()
    route_calls: list[str] = []
    row = SimpleNamespace(
        device_id="112233445566",
        ip_address="192.0.2.112",
        expected_model="SoundTouch 20",
    )
    target = ServerTarget(host="192.0.2.185", web_port=1328, cloud_port=1516, debug_port=1860)

    monkeypatch.setattr(adapter, "_client", lambda _row: client)
    monkeypatch.setattr(
        "basswiesn.app.services.setup_rebuild.radio_adapter._baseline_for",
        lambda _row, _timestamp: Path(tmp_path),
    )

    async def apply_route(_row, _target):
        route_calls.append(_target.host)
        return SimpleNamespace(responses=[])

    monkeypatch.setattr(adapter, "_apply_route_with_retry", apply_route)
    result = asyncio.run(adapter.pair_account(row, target))

    assert result["account_paired"] is True
    assert result["account_changed"] is False
    assert result["routing_restored_after_pairing"] is True
    assert route_calls == ["192.0.2.185"]
    assert client.posts == []
