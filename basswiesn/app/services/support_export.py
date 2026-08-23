"""Common, bounded and redacted support archive construction."""

from __future__ import annotations

import hashlib
import ipaddress
import io
import json
import re
from pathlib import Path
from typing import Mapping
from zipfile import ZIP_DEFLATED, ZipFile, ZipInfo


class SupportBundleTooLarge(ValueError):
    pass


_SECRET_ASSIGNMENT = re.compile(
    r"(?i)(password|passwd|token|secret|credential|authorization|cookie|api[_-]?key|private[_-]?key)"
    r"(\s*[=:]\s*)([^\s,;&<\"]+)"
)
_SENSITIVE_QUERY = re.compile(r"(?i)([?&](?:token|key|secret|password|signature)=)[^&\s]+")
_SECRET_TAG = re.compile(
    r"(?is)(<(?P<tag>[A-Za-z0-9_.:-]*(?:password|passwd|token|secret|credential|authorization|cookie|private[_-]?key)[A-Za-z0-9_.:-]*)\b[^>]*>)"
    r"(.*?)"
    r"(</(?P=tag)\s*>)"
)
_SENSITIVE_PATH = re.compile(r"(?i)(/(?:token|secret|password|credential)/)[^/\s<]+")
_SECRET_JSON = re.compile(r'(?i)("(?:password|passwd|token|secret|credential|authorization|cookie|private[_-]?key)"\s*:\s*")([^"]*)(")')
_IP = re.compile(r"(?<![\d.])(?:\d{1,3}\.){3}\d{1,3}(?![\d.])")


def redact_text(value: str, *, anonymize_ips: bool = True) -> str:
    text = str(value or "")
    text = _SECRET_TAG.sub(lambda match: f"{match.group(1)}***REDACTED***{match.group(4)}", text)
    text = _SECRET_JSON.sub(r"\1***REDACTED***\3", text)
    text = _SECRET_ASSIGNMENT.sub(lambda match: f"{match.group(1)}{match.group(2)}***REDACTED***", text)
    text = _SENSITIVE_QUERY.sub(r"\1***REDACTED***", text)
    text = _SENSITIVE_PATH.sub(r"\1***REDACTED***", text)
    if anonymize_ips:
        def replace_ip(match: re.Match) -> str:
            try:
                ipaddress.ip_address(match.group(0))
                return "<redacted-ip>"
            except ValueError:
                return match.group(0)
        text = _IP.sub(replace_ip, text)
    return text


def redact_payload(value, *, anonymize_ips: bool = True):
    if isinstance(value, dict):
        result = {}
        for key, item in value.items():
            key_text = str(key)
            if any(marker in key_text.lower() for marker in ("password", "passwd", "token", "secret", "credential", "authorization", "cookie", "private_key", "api_key")):
                result[key_text] = "***REDACTED***"
            else:
                result[key_text] = redact_payload(item, anonymize_ips=anonymize_ips)
        return result
    if isinstance(value, (list, tuple)):
        return [redact_payload(item, anonymize_ips=anonymize_ips) for item in value[:500]]
    if isinstance(value, str):
        return redact_text(value, anonymize_ips=anonymize_ips)[:100000]
    return value


def tail_text(path: Path, *, max_bytes: int = 4 * 1024 * 1024, max_lines: int = 1000) -> str:
    """Read a bounded tail without loading an entire growing log."""

    try:
        with path.open("rb") as handle:
            handle.seek(0, 2)
            size = handle.tell()
            handle.seek(max(0, size - max_bytes))
            data = handle.read(max_bytes)
    except OSError:
        return ""
    lines = data.decode("utf-8", errors="replace").splitlines()[-max_lines:]
    return "\n".join(lines)


def _bytes(value) -> bytes:
    if isinstance(value, bytes):
        return value
    if isinstance(value, str):
        return value.encode("utf-8")
    return json.dumps(value, ensure_ascii=False, indent=2, sort_keys=True).encode("utf-8")


def build_support_bundle(
    entries: Mapping[str, bytes | str],
    *,
    max_bytes: int,
    metadata: Mapping[str, object] | None = None,
) -> io.BytesIO:
    """Build a deterministic ZIP with a manifest and SHA256SUMS.

    The manifest and checksum file cover all payload files.  They are kept
    under dedicated names so existing compatibility files named
    ``manifest.json`` remain unchanged.
    """

    payloads = {
        str(name): _bytes(redact_text(value) if isinstance(value, str) else value)
        for name, value in entries.items()
    }
    if any(not name or name.startswith("/") or ".." in name.split("/") for name in payloads):
        raise ValueError("support bundle contains an unsafe member name")
    if sum(len(value) for value in payloads.values()) > max_bytes:
        raise SupportBundleTooLarge("support bundle exceeds the configured size limit")
    manifest = {
        "format": "basswiesn-support-bundle-v1",
        "metadata": redact_payload(dict(metadata or {}), anonymize_ips=False),
        "files": [
            {"path": name, "size_bytes": len(payloads[name]), "sha256": hashlib.sha256(payloads[name]).hexdigest()}
            for name in sorted(payloads)
        ],
    }
    manifest_bytes = json.dumps(manifest, ensure_ascii=False, indent=2, sort_keys=True).encode("utf-8")
    sums = "".join(f"{item['sha256']}  {item['path']}\n" for item in manifest["files"])
    output = io.BytesIO()
    with ZipFile(output, "w", ZIP_DEFLATED) as archive:
        for name in sorted(payloads):
            info = ZipInfo(name, date_time=(1980, 1, 1, 0, 0, 0))
            info.compress_type = ZIP_DEFLATED
            archive.writestr(info, payloads[name])
        info = ZipInfo("support-manifest.json", date_time=(1980, 1, 1, 0, 0, 0))
        info.compress_type = ZIP_DEFLATED
        archive.writestr(info, manifest_bytes)
        info = ZipInfo("SHA256SUMS", date_time=(1980, 1, 1, 0, 0, 0))
        info.compress_type = ZIP_DEFLATED
        archive.writestr(info, sums.encode("ascii"))
    if output.tell() > max_bytes:
        raise SupportBundleTooLarge("support bundle exceeds the configured size limit")
    output.seek(0)
    return output
