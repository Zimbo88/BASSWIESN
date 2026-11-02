"""Shared secret redaction for logs, errors and exported support text.

Redaction is a last line of defence, not permission to collect credentials.
Opaque secrets without a recognizable field or envelope cannot be identified
reliably: callers must avoid collecting them or mark their fields explicitly.
"""
from __future__ import annotations

import ipaddress
import re

REDACTED = "***REDACTED***"
_MARKERS = (
    "password", "passwd", "passphrase", "token", "secret", "credential",
    "authorization", "cookie", "privatekey", "sshkey", "apikey",
)
_KEY = r"[A-Za-z0-9_.:-]{0,96}(?:password|passwd|passphrase|token|secret|credential|authorization|cookie|api[_-]?key|private[_-]?key|ssh[_-]?key)[A-Za-z0-9_.:-]{0,96}"
_PEM = re.compile(r"-----BEGIN (?:[A-Z0-9]+ )*PRIVATE KEY-----.*?(?:-----END (?:[A-Z0-9]+ )*PRIVATE KEY-----|\Z)", re.S)
_PEM_ORPHAN = re.compile(r"\A.*?-----END (?:[A-Z0-9]+ )*PRIVATE KEY-----", re.S)
_TAG = re.compile(rf"(<(?P<tag>{_KEY})\b[^>]*>).*?(</(?P=tag)\s*>|\Z)", re.I | re.S)
_JSON = re.compile(rf'("{_KEY}"\s*:\s*")((?:\\.|[^"\\])*)(")', re.I)
_ASSIGNMENT = re.compile(rf"(\b{_KEY}[\"']?\s*[=:]\s*)(?:\"(?:\\.|[^\"\\])*(?:\"|\\?\Z)|'(?:\\.|[^'\\])*(?:'|\\?\Z)|[^\s,;&<>\"']+)", re.I)
_HEADERS = re.compile(r"\b(?:proxy-authorization|authorization|set-cookie|cookie)\s*:\s*[^\r\n]+(?:\r?\n[ \t]+[^\r\n]*)*", re.I)
_AUTH = re.compile(r"\b(Bearer|Basic)\s+[A-Za-z0-9._~+/-]+=*", re.I)
_AUTOMATION = re.compile(r"bwapi_[a-f0-9]{32}_[A-Za-z0-9_-]{43}")
_URL_USERINFO = re.compile(r"(https?://)[^\s/@]+@", re.I)
_QUERY = re.compile(r"([?&](?:token|key|secret|password|signature)=)[^&\s\"'<>]+", re.I)
_PATH = re.compile(r"(/(?:token|secret|password|credential)/)[^/\s<>\"']+", re.I)
_IPV4 = re.compile(r"(?<![\d.])(?:\d{1,3}\.){3}\d{1,3}(?![\d.])")
_IPV6 = re.compile(r"(?<![A-Za-z0-9:.])(?:[A-Fa-f0-9]{0,4}:){2,}[A-Fa-f0-9:.]+(?:%[A-Za-z0-9_.-]+)?(?![A-Za-z0-9:.])")


def is_secret_field(key: str) -> bool:
    normalized = re.sub(r"[^a-z0-9]", "", key.lower())
    return any(marker in normalized for marker in _MARKERS)


def _anonymize_address(match: re.Match[str]) -> str:
    try:
        ipaddress.ip_address(match.group(0))
    except ValueError:
        return match.group(0)
    return "<redacted-ip>"


def _redact_assignment(match: re.Match[str]) -> str:
    prefix = match.group(1)
    value = match.group(0)[len(prefix):]
    quote = value[0] if value.startswith(('"', "'")) else ""
    return f"{prefix}{quote}{REDACTED}{quote}"


def redact_text(value: str, *, anonymize_ips: bool = True) -> str:
    text = _AUTOMATION.sub(REDACTED, value)
    text = _PEM.sub(REDACTED, text)
    # A bounded log tail may start inside a multiline key. After complete PEM
    # envelopes are removed, an orphan closing marker makes its prefix unsafe.
    text = _PEM_ORPHAN.sub(REDACTED, text)
    text = _TAG.sub(lambda m: f"{m.group(1)}{REDACTED}{m.group(3)}", text)
    text = _JSON.sub(lambda m: f"{m.group(1)}{REDACTED}{m.group(3)}", text)
    # Headers include spaces/semicolons, unlike a scalar key=value assignment.
    text = _HEADERS.sub(REDACTED, text)
    text = _ASSIGNMENT.sub(_redact_assignment, text)
    text = _AUTH.sub(lambda m: f"{m.group(1)} {REDACTED}", text)
    text = _URL_USERINFO.sub(lambda m: f"{m.group(1)}{REDACTED}@", text)
    text = _QUERY.sub(lambda m: f"{m.group(1)}{REDACTED}", text)
    text = _PATH.sub(lambda m: f"{m.group(1)}{REDACTED}", text)
    if anonymize_ips:
        text = _IPV6.sub(_anonymize_address, text)
        text = _IPV4.sub(_anonymize_address, text)
    return text


def redact_payload(value: object, *, anonymize_ips: bool = True) -> object:
    if isinstance(value, dict):
        return {
            str(key): REDACTED if is_secret_field(str(key)) else redact_payload(item, anonymize_ips=anonymize_ips)
            for key, item in value.items()
        }
    if isinstance(value, (tuple, list)):
        return [redact_payload(item, anonymize_ips=anonymize_ips) for item in value[:500]]
    if isinstance(value, str):
        # Redact before truncating: otherwise the beginning of a secret may leak.
        return redact_text(value, anonymize_ips=anonymize_ips)[:100000]
    if isinstance(value, (int, float, bool)) or value is None:
        return value
    return redact_text(str(value), anonymize_ips=anonymize_ips)[:100000]
