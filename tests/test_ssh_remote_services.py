from fastapi.testclient import TestClient

from basswiesn.app.main import create_web_app


def test_remote_services_download_is_empty_and_has_exact_filename():
    with TestClient(create_web_app()) as client:
        response = client.get("/api/ssh/remote-services-file")

    assert response.status_code == 200
    assert response.content == b""
    assert response.headers["content-disposition"] == 'attachment; filename="remote_services"'


def test_normal_setup_does_not_expose_legacy_remote_services_download():
    with TestClient(create_web_app()) as client:
        html = client.get("/").text

    assert 'href="/api/ssh/remote-services-file"' not in html
    assert 'download="remote_services"' not in html
    for instruction in ("FAT32", "Root-Verzeichnis", "USB-Stick in das Radio", "Radio neu starten", "SSH-Verbindung prüfen"):
        assert instruction not in html
    assert "SSH im normalen Setup nicht erforderlich" in html
import pytest as _pytest_marker
pytestmark = _pytest_marker.mark.integration
