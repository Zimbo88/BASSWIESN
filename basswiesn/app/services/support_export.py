"""Common, bounded and redacted support archive construction."""

from __future__ import annotations

import hashlib
import io
import json
import os
from pathlib import Path
import stat
from typing import Mapping
from zipfile import ZIP_DEFLATED, ZipFile, ZipInfo

# Re-export these names for existing callers; policy lives in one module.
from basswiesn.app.core.redaction import redact_payload, redact_text


class SupportBundleTooLarge(ValueError):
    pass


MAX_SUPPORT_ENTRIES = 256
_RESERVED_NAMES = frozenset({"support-manifest.json", "sha256sums"})


def tail_text(path: Path, *, max_bytes: int = 4 * 1024 * 1024, max_lines: int = 1000) -> str:
    """Read a bounded tail without loading an entire growing log."""

    if max_bytes <= 0 or max_lines <= 0:
        return ""
    try:
        # Reject symlinks and FIFOs; diagnostics must never block on a pipe or
        # follow a substituted log into the secrets directory.
        absolute = Path(os.path.abspath(path))
        directory = os.open(absolute.anchor, os.O_RDONLY | os.O_DIRECTORY | os.O_NOFOLLOW)
        try:
            for part in absolute.parts[1:-1]:
                child = os.open(part, os.O_RDONLY | os.O_DIRECTORY | os.O_NOFOLLOW, dir_fd=directory)
                os.close(directory)
                directory = child
            fd = os.open(absolute.name, os.O_RDONLY | os.O_NOFOLLOW | os.O_NONBLOCK, dir_fd=directory)
        finally:
            os.close(directory)
        with os.fdopen(fd, "rb") as handle:
            if not stat.S_ISREG(os.fstat(handle.fileno()).st_mode):
                return ""
            handle.seek(0, 2)
            size = handle.tell()
            offset = max(0, size - max_bytes)
            handle.seek(offset)
            data = handle.read(max_bytes)
    except OSError:
        return ""
    if offset:
        # The first fragment may start inside a quoted credential and have no
        # recognizable field left. Omit it rather than export a secret suffix.
        _, separator, data = data.partition(b"\n")
        if not separator:
            return ""
    # Redact multiline envelopes BEFORE the line limit can remove their opener.
    lines = redact_text(data.decode("utf-8", errors="replace"), anonymize_ips=False).splitlines()[-max_lines:]
    return "\n".join(lines)


def _bytes(value: object) -> bytes:
    if isinstance(value, bytes):
        return value
    if isinstance(value, str):
        return value.encode("utf-8")
    return json.dumps(value, ensure_ascii=False, indent=2, sort_keys=True).encode("utf-8")


def _redacted_entry(name: str, value: bytes | str) -> bytes:
    # Byte input must not bypass the same redaction used for string input.
    # Support bundles are text diagnostics, not opaque hardware/DB backups.
    try:
        text = value.decode("utf-8") if isinstance(value, bytes) else value
    except UnicodeDecodeError:
        raise ValueError("Support entries must be inspectable UTF-8 text") from None
    if name.lower().endswith(".json"):
        try:
            parsed = json.loads(text)
        except ValueError:
            pass  # A truncated diagnostic remains text, never trusted JSON.
        else:
            return _bytes(redact_payload(parsed))
    return redact_text(text).encode("utf-8")


def _validate_member_names(entries: Mapping[str, bytes | str]) -> None:
    seen: set[str] = set()
    for name in entries:
        parts = name.split("/")
        folded = name.casefold()
        if (not name or len(name) > 240 or not name.isascii()
                or any(ord(char) < 32 or ord(char) == 127 for char in name)
                or "\\" in name or ":" in name
                or any(part in {"", ".", ".."} or part.endswith((".", " ")) for part in parts)
                or folded in _RESERVED_NAMES or folded in seen):
            raise ValueError("support bundle contains an unsafe member name")
        seen.add(folded)


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

    if max_bytes <= 0:
        raise ValueError("support bundle size limit must be positive")
    if len(entries) > MAX_SUPPORT_ENTRIES:
        raise SupportBundleTooLarge("support bundle exceeds the configured entry limit")
    _validate_member_names(entries)
    # Bound original input BEFORE JSON parsing/redaction/compression. Neither a
    # large redacted secret nor a highly compressible payload may bypass quota.
    metadata_bytes = _bytes(dict(metadata or {}))
    input_bytes = len(metadata_bytes)
    for value in entries.values():
        input_bytes += len(value) if isinstance(value, bytes) else len(value.encode("utf-8"))
        if input_bytes > max_bytes:
            raise SupportBundleTooLarge("support bundle exceeds the configured size limit")
    if input_bytes > max_bytes:
        raise SupportBundleTooLarge("support bundle exceeds the configured size limit")
    payloads = {name: _redacted_entry(name, value) for name, value in entries.items()}
    files = [
        {"path": name, "size_bytes": len(payloads[name]), "sha256": hashlib.sha256(payloads[name]).hexdigest()}
        for name in sorted(payloads)
    ]
    manifest = {
        "format": "basswiesn-support-bundle-v1",
        "metadata": redact_payload(json.loads(metadata_bytes)),
        "files": files,
    }
    manifest_bytes = json.dumps(manifest, ensure_ascii=False, indent=2, sort_keys=True).encode("utf-8")
    sums = "".join(f"{item['sha256']}  {item['path']}\n" for item in files)
    if sum(len(value) for value in payloads.values()) + len(manifest_bytes) + len(sums) > max_bytes:
        raise SupportBundleTooLarge("support bundle exceeds the configured size limit")
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
