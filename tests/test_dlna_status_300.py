"""A feature flag must never turn a placeholder into a claimed DLNA scan."""
import asyncio
from types import SimpleNamespace

import pytest

from basswiesn.app.services import dlna_experimental as dlna

pytestmark = pytest.mark.unit


@pytest.mark.parametrize("enabled", [False, True])
def test_dlna_flag_reports_capability_honestly_without_network(monkeypatch, enabled):
    monkeypatch.setattr(dlna, "get_settings", lambda: SimpleNamespace(experimental_dlna=enabled))
    status = dlna.dlna_status()
    assert status["enabled"] is enabled
    assert status["ready"] is enabled
    assert status["hardware_verified"] is False
    assert status["implementation_status"] == "EXPLICIT_CONTENT_DIRECTORY"
    assert status["background_services_started"] is False
    assert status["renderer_discovery"] == "not_supported"
    assert status["content_browse"] == "implemented"
    assert status["transcoding"] == "not_implemented"

    # The suite's socket guard rejects non-loopback network access.
    result = asyncio.run(dlna.discover_renderers())
    assert result["skipped"] is True
    assert result["renderers"] == []
    assert result["reason_code"] == ("EXPLICIT_SERVER_REQUIRED" if enabled else "FEATURE_DISABLED")
