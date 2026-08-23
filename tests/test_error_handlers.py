import httpx
import pytest
from fastapi.testclient import TestClient
from xml.etree import ElementTree as ET

from basswiesn.app.core.errors import (
    CLICommandFailed,
    DeviceUnavailable,
    InvalidXML,
    UnsafeActionBlocked,
)
from basswiesn.app.main import create_web_app


@pytest.mark.parametrize(
    ("error", "status", "code"),
    [
        (DeviceUnavailable("radio offline"), 503, "DEVICE_UNREACHABLE"),
        (InvalidXML("bad payload"), 502, "INVALID_XML"),
        (CLICommandFailed("CLI failed"), 502, "CLI_COMMAND_FAILED"),
        (UnsafeActionBlocked("confirmation required"), 409, "UNSAFE_ACTION_BLOCKED"),
        (httpx.ConnectError("connection refused"), 503, "DEVICE_UNREACHABLE"),
        (ET.ParseError("malformed XML"), 502, "INVALID_XML"),
    ],
)
def test_exception_handlers_return_structured_errors(error, status, code):
    app = create_web_app()

    async def fail():
        raise error

    app.add_api_route("/_test/error", fail)
    with TestClient(app, raise_server_exceptions=False) as client:
        response = client.get("/_test/error")

    assert response.status_code == status
    assert response.json() == {
        "ok": False,
        "error": {"code": code, "message": str(error)},
    }
import pytest as _pytest_marker
pytestmark = _pytest_marker.mark.integration
