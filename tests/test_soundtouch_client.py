import asyncio

import httpx
import pytest

from basswiesn.app.adapters.soundtouch_client import SoundTouchClient
from basswiesn.app.services.soundtouch_client import SoundTouchClient as CompatibilityClient


def test_get_xml_uses_injected_client_without_real_network():
    async def handler(request: httpx.Request) -> httpx.Response:
        assert request.url == httpx.URL("http://192.0.2.10:8090/info")
        return httpx.Response(200, text="<info />")

    async def scenario() -> str:
        async with httpx.AsyncClient(transport=httpx.MockTransport(handler)) as http_client:
            return await SoundTouchClient(
                "192.0.2.10", http_client=http_client, get_timeout=0.25
            ).get_xml("/info")

    assert asyncio.run(scenario()) == "<info />"


def test_post_xml_sends_xml_and_preserves_additional_headers():
    async def handler(request: httpx.Request) -> httpx.Response:
        assert request.method == "POST"
        assert request.url == httpx.URL("http://192.0.2.11:18090/volume")
        assert request.headers["content-type"] == "application/xml"
        assert request.headers["propagate"] == "true"
        assert await request.aread() == b"<volume>5</volume>"
        return httpx.Response(200, text="<volume>5</volume>")

    async def scenario() -> str:
        async with httpx.AsyncClient(transport=httpx.MockTransport(handler)) as http_client:
            return await SoundTouchClient(
                "192.0.2.11",
                http_client=http_client,
                radio_port=18090,
            ).post_xml("/volume", "<volume>5</volume>", headers={"propagate": "true"})

    assert asyncio.run(scenario()) == "<volume>5</volume>"


def test_info_parses_soundtouch_identity_fields():
    xml = (
        '<info deviceID="DEVICE-1">'
        "<name>Wohnzimmer</name><type>SoundTouch 30</type>"
        "<margeURL>http://basswiesn:1516</margeURL>"
        "</info>"
    )

    async def handler(_request: httpx.Request) -> httpx.Response:
        return httpx.Response(200, text=xml)

    async def scenario() -> dict[str, str]:
        async with httpx.AsyncClient(transport=httpx.MockTransport(handler)) as http_client:
            return await SoundTouchClient("192.0.2.12", http_client=http_client).info()

    assert asyncio.run(scenario()) == {
        "device_id": "DEVICE-1",
        "ip_address": "192.0.2.12",
        "name": "Wohnzimmer",
        "model": "SoundTouch 30",
        "marge_url": "http://basswiesn:1516",
        "raw": xml,
    }


def test_http_status_errors_remain_available_to_service_layer():
    async def handler(_request: httpx.Request) -> httpx.Response:
        return httpx.Response(503, text="unavailable")

    async def scenario() -> None:
        async with httpx.AsyncClient(transport=httpx.MockTransport(handler)) as http_client:
            client = SoundTouchClient("192.0.2.13", http_client=http_client)
            await client.get_xml("/info")

    with pytest.raises(httpx.HTTPStatusError) as error:
        asyncio.run(scenario())
    assert error.value.response.status_code == 503
    assert CompatibilityClient is SoundTouchClient
import pytest as _pytest_marker
pytestmark = _pytest_marker.mark.unit
