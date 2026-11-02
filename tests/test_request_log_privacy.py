"""Legacy protocol diagnostics must redact before storage/truncation."""

import json

import pytest

from basswiesn.app import db as app_db
from basswiesn.app.models import RequestLog
from basswiesn.app.services import diagnostics


pytestmark = pytest.mark.unit


@pytest.mark.parametrize("body", [
    json.dumps({"password": "canary-private-prefix-" + "s" * 5000}),
    '<root><password>canary-private-prefix-' + "s" * 5000 + '</password></root>',
    "password='canary-private-prefix-" + "s" * 1000,
    'password="canary-private-prefix-' + "s" * 1000,
    "Authorization: Bearer canary-private-prefix-opaque\nCookie: canary-private-prefix-cookie",
    "https://user:canary-private-prefix-opaque@example.invalid/path?token=canary-private-prefix-query",
])
@pytest.mark.parametrize("binary", [False, True])
def test_protocol_request_log_redacts_secret_before_prefix_is_stored(body, binary):
    with app_db.SessionLocal() as db:
        diagnostics.log_request(db, direction="in", service="fixture", method="POST",
                                path="/token/canary-private-prefix-path", host="fixture.invalid",
                                body=body.encode() if binary else body)
        row = db.query(RequestLog).one()
        assert "canary-private-prefix-" not in row.body + row.path + row.host
        assert len(row.body) <= 4096


@pytest.mark.parametrize("body", ["x" * 65537, b"x" * 65537, "\u00e4" * 40000])
def test_oversized_body_is_omitted_before_redaction(monkeypatch, body):
    seen = []
    original = diagnostics.redact_text

    def bounded(value, **kwargs):
        seen.append(len(value))
        assert len(value) < 4096
        return original(value, **kwargs)

    monkeypatch.setattr(diagnostics, "redact_text", bounded)
    assert diagnostics._request_body(body) == diagnostics._OMITTED_REQUEST_TEXT
    assert seen


def test_structured_secret_types_and_safe_fields_are_preserved():
    body = diagnostics._request_body(json.dumps({"password": 1234, "nested": {"cookie": ["private"]}, "volume": 3}))
    assert json.loads(body) == {"password": "***REDACTED***", "nested": {"cookie": "***REDACTED***"}, "volume": 3}


def test_diagnostics_does_not_accept_an_arbitrary_headers_collection():
    with app_db.SessionLocal() as db:
        with pytest.raises(TypeError):
            diagnostics.log_request(db, direction="in", service="fixture", method="GET", path="/",
                                    headers={"X-Private-Capability": "opaque-canary"})
        assert db.query(RequestLog).count() == 0
