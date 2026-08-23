from basswiesn.app.adapters.soundtouch_client import _redact_text
from basswiesn.app.services.support_export import redact_text


def test_soundtouch_xml_user_auth_token_is_redacted_from_masterlog_preview():
    raw = (
        "<PairDeviceWithAccount><accountId>123</accountId>"
        "<userAuthToken>highly-sensitive-local-token</userAuthToken>"
        "<boseServer>http://192.0.2.1:1516</boseServer></PairDeviceWithAccount>"
    )

    redacted = _redact_text(raw, limit=4096)

    assert "highly-sensitive-local-token" not in redacted
    assert "<userAuthToken>***REDACTED***</userAuthToken>" in redacted
    assert "192.0.2.1" in redacted


def test_support_redaction_covers_sensitive_xml_tag_names_with_prefixes():
    raw = (
        "<root><userAuthToken>token-value</userAuthToken>"
        "<apiCredential>credential-value</apiCredential></root>"
    )

    redacted = redact_text(raw, anonymize_ips=False)

    assert "token-value" not in redacted
    assert "credential-value" not in redacted
    assert redacted.count("***REDACTED***") == 2
