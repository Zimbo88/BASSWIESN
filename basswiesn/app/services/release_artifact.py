"""Offline, read-only verification of the BASSWIESN release package contract.

No extraction, execution, downloads or application imports. SHA256 proves
consistency with the supplied checksum, NOT the publisher's identity. A future
installer must obtain both artifacts from its trusted official-release flow,
keep them in private staging, and reverify before use (never reopen an arbitrary
user path on the strength of this result).

Only GNU/USTAR regular files and directories are supported. PAX, sparse files,
links and special files fail closed. Bounded GNU longnames support the release
producer without allowing tarfile's automatic extension-header processing.
"""
from __future__ import annotations

import ast
from contextlib import nullcontext
from dataclasses import dataclass
import hashlib
import json
import os
from pathlib import Path
import re
import stat
import tarfile
from typing import BinaryIO
import zlib


ROOT = "basswiesn-release"
CHUNK = 64 * 1024
VERSION = re.compile(r"(?:0|[1-9][0-9]{0,3})\.(?:0|[1-9][0-9]{0,3})\.(?:0|[1-9][0-9]{0,3})")
SHA256 = re.compile(r"[0-9a-f]{64}")
COMPONENT = re.compile(r"[A-Za-z0-9_.-]{1,255}")
REQUIRED_FILES = frozenset({
    "basswiesn/__init__.py", "install.sh", "Dockerfile", "docker-compose.yml",
    "requirements.txt", "README.md", "LICENSE", ".env.example", ".dockerignore",
})


class ArtifactRejected(ValueError):
    """Stable reason code only; never include untrusted paths or file content."""

    def __init__(self, code: str):
        self.code = code
        super().__init__(code)


@dataclass(frozen=True)
class VerificationLimits:
    compressed_bytes: int = 128 * 1024 * 1024
    expanded_bytes: int = 512 * 1024 * 1024
    member_bytes: int = 32 * 1024 * 1024
    metadata_bytes: int = 2 * 1024 * 1024
    headers: int = 8192
    path_bytes: int = 1024

    def __post_init__(self):
        if any(type(value) is not int or value <= 0 for value in vars(self).values()):
            raise ValueError("positive integer limits required")


@dataclass(frozen=True)
class VerifiedRelease:
    version: str
    archive_sha256: str
    compressed_bytes: int
    expanded_bytes: int
    file_count: int
    integrity_verified: bool = True
    # An offline checksum is not a signature or a transport provenance check.
    authenticity_verified: bool = False
    installation_performed: bool = False


def _reject(code: str):
    raise ArtifactRejected(code)


def _path(value: str, limit: int) -> str:
    if not isinstance(value, str) or not 0 < len(value) <= limit:
        _reject("UNSAFE_PATH")
    parts = value.split("/")
    if any(not COMPONENT.fullmatch(part) or part in {".", ".."}
           or part.endswith(".") for part in parts):
        _reject("UNSAFE_PATH")
    return value


def _checksums(data: bytes, limit: int, paths: int, path_limit: int) -> dict[str, str]:
    if not isinstance(data, bytes) or not 0 < len(data) <= limit:
        _reject("CHECKSUM_FORMAT")
    try:
        text = data.decode("ascii")
    except UnicodeError:
        _reject("CHECKSUM_FORMAT")
    # Exactly the sha256sum text/binary grammar; no comments, escapes, aliases
    # or arbitrary filenames accepted by a shell. Never invoke sha256sum here.
    lines = text.split("\n")
    if lines[-1] == "":
        lines.pop()
    if not 0 < len(lines) <= paths:
        _reject("CHECKSUM_FORMAT")
    entries = {}
    for line in lines:
        if len(line) < 67 or not SHA256.fullmatch(line[:64]) or line[64:66] not in {"  ", " *"}:
            _reject("CHECKSUM_FORMAT")
        path = _path(line[66:], path_limit)
        if path in entries:
            _reject("DUPLICATE_CHECKSUM")
        entries[path] = line[:64]
    return entries


class _GzipReader:
    """Bounded streaming gzip decoder; rejects truncation and concatenation."""

    def __init__(self, raw: BinaryIO, limits: VerificationLimits):
        self.raw = raw
        self.limits = limits
        self.expanded = 0
        self.compressed = 0
        self.buffer = bytearray()
        self.chunks = self._chunks()

    def _chunks(self):
        decoder = zlib.decompressobj(31)
        while True:
            chunk = self.raw.read(CHUNK)
            self.compressed += len(chunk)
            if self.compressed > self.limits.compressed_bytes:
                _reject("COMPRESSED_LIMIT")
            if not chunk:
                _reject("GZIP_TRUNCATED")
            while chunk:
                output = decoder.decompress(chunk, CHUNK)
                self.expanded += len(output)
                if self.expanded > self.limits.expanded_bytes:
                    _reject("EXPANDED_LIMIT")
                if output:
                    yield output
                if decoder.eof:
                    if decoder.unused_data or self.raw.read(1):
                        _reject("GZIP_TRAILING_DATA")
                    return
                chunk = decoder.unconsumed_tail

    def read(self, size: int) -> bytes:
        assert 0 <= size <= CHUNK
        while len(self.buffer) < size:
            block = next(self.chunks, None)
            if block is None:
                break
            self.buffer.extend(block)
        value = bytes(self.buffer[:size])
        del self.buffer[:size]
        return value

    def exact(self, size: int) -> bytes:
        value = self.read(size)
        if len(value) != size:
            _reject("TAR_TRUNCATED")
        return value

    def payload(self, size: int, *, capture: bool = False, output=None) -> tuple[str, bytes]:
        digest = hashlib.sha256()
        captured = bytearray()
        remaining = size
        while remaining:
            chunk = self.exact(min(CHUNK, remaining))
            digest.update(chunk)
            if output is not None:
                output.write(chunk)
            if capture:
                captured.extend(chunk)
            remaining -= len(chunk)
        if any(self.exact((-size) % 512)):
            _reject("TAR_NONZERO_PADDING")
        return digest.hexdigest(), bytes(captured)


def _cstring(field: bytes) -> str:
    head, separator, tail = field.partition(b"\0")
    if separator and any(tail):
        _reject("TAR_AMBIGUOUS_NAME")
    return head.decode("ascii")


def _read_tar(reader: _GzipReader, limits: VerificationLimits, *, sink=None):
    files, directories, seen, metadata = {}, set(), set(), {}
    pending_name = None
    header_count = 0
    while True:
        block = reader.exact(512)
        if block == bytes(512):
            if pending_name is not None or reader.exact(512) != bytes(512):
                _reject("TAR_END_MARKER")
            while chunk := reader.read(CHUNK):
                if any(chunk):
                    _reject("TAR_TRAILING_DATA")
            if reader.expanded % 512:
                _reject("TAR_ALIGNMENT")
            break
        header_count += 1
        if header_count > limits.headers:
            _reject("HEADER_LIMIT")
        if block[257:265] not in {b"ustar\x0000", b"ustar  \x00"}:
            _reject("TAR_FORMAT")
        # Only parse a single fixed-size header. TarFile iteration would process
        # unbounded extension records before a caller could inspect each entry.
        info = tarfile.TarInfo.frombuf(block, "ascii", "strict")
        if info.type == tarfile.GNUTYPE_LONGNAME:
            if pending_name is not None or not 1 <= info.size <= limits.path_bytes + 2:
                _reject("LONGNAME_LIMIT")
            _, name = reader.payload(info.size, capture=True)
            if not name.endswith(b"\0") or b"\0" in name[:-1]:
                _reject("TAR_AMBIGUOUS_NAME")
            pending_name = name[:-1].decode("ascii")
            continue
        if info.type not in {tarfile.REGTYPE, tarfile.AREGTYPE, tarfile.DIRTYPE}:
            _reject("TAR_ENTRY_TYPE")
        is_dir = info.type == tarfile.DIRTYPE
        if info.linkname or info.uid != 0 or info.gid != 0 or info.mode & ~0o775:
            _reject("TAR_ATTRIBUTES")
        if info.size < 0 or info.size > limits.member_bytes:
            _reject("MEMBER_LIMIT")
        if is_dir and info.size:
            _reject("DIRECTORY_PAYLOAD")
        # Read the original name rather than TarInfo's normalized directory
        # name, so repeated slashes/dot aliases cannot silently disappear.
        name = _cstring(block[:100])
        if block[257:265] == b"ustar\x0000":
            prefix = _cstring(block[345:500])
            name = prefix + "/" + name if prefix else name
        if pending_name is not None:
            name, pending_name = pending_name, None
        if is_dir and name.endswith("/"):
            name = name[:-1]
        name = _path(name, limits.path_bytes)
        if name != ROOT and not name.startswith(ROOT + "/"):
            _reject("WRONG_ROOT")
        if name in seen:
            _reject("DUPLICATE_PATH")
        seen.add(name)
        if is_dir:
            directories.add(name)
            if sink is not None:
                sink.directory(name)
            continue
        if name == ROOT:
            _reject("WRONG_ROOT")
        relative = name[len(ROOT) + 1:]
        capture_limit = {"manifest.json": limits.metadata_bytes,
                         "SHA256SUMS": limits.metadata_bytes,
                         "basswiesn/__init__.py": 16 * 1024}.get(relative)
        if capture_limit is not None and info.size > capture_limit:
            _reject("METADATA_LIMIT")
        # The offline verifier never supplies a sink. A host staging caller may
        # reuse this exact parser only AFTER complete verification on a pinned FD.
        with sink.file(name, info.mode) if sink is not None else nullcontext() as output:
            digest, content = reader.payload(info.size, capture=capture_limit is not None, output=output)
        files[relative] = (digest, info.size)
        if capture_limit is not None:
            metadata[relative] = content
        if relative == "install.sh" and not info.mode & 0o100:
            _reject("INSTALLER_NOT_EXECUTABLE")
    if ROOT not in directories:
        _reject("MISSING_ROOT_DIRECTORY")
    for name in seen - {ROOT}:
        parent = name.rsplit("/", 1)[0]
        if parent not in directories:
            _reject("MISSING_PARENT_DIRECTORY")
    return files, metadata


def _unique_object(pairs):
    result = {}
    for key, value in pairs:
        if key in result:
            _reject("DUPLICATE_JSON_KEY")
        result[key] = value
    return result


def _manifest_json(data: bytes):
    # Apply a depth bound before JSON object allocation. Python versions differ
    # in how deeply the decoder can nest before raising RecursionError.
    quoted, escaped, depth = False, False, 0
    for value in data:
        if quoted:
            if escaped:
                escaped = False
            elif value == 92:
                escaped = True
            elif value == 34:
                quoted = False
        elif value == 34:
            quoted = True
        elif value in (91, 123):
            depth += 1
            if depth > 8:
                _reject("MANIFEST_DEPTH")
        elif value in (93, 125):
            depth -= 1
    return json.loads(data, object_pairs_hook=_unique_object)


def _verify_contents(files: dict, metadata: dict, version: str, limits: VerificationLimits):
    if not REQUIRED_FILES | {"manifest.json", "SHA256SUMS"} <= files.keys():
        _reject("MISSING_REQUIRED_FILE")
    manifest = _manifest_json(metadata["manifest.json"])
    if not isinstance(manifest, dict) or set(manifest) != {"format", "version", "source_date_epoch", "files"}:
        _reject("MANIFEST_SCHEMA")
    if type(manifest["format"]) is not int or manifest["format"] != 1 or manifest["version"] != version:
        _reject("MANIFEST_VERSION")
    if type(manifest["source_date_epoch"]) is not int or not 0 <= manifest["source_date_epoch"] <= 253_402_300_799:
        _reject("MANIFEST_SCHEMA")
    entries = manifest["files"]
    if not isinstance(entries, list) or not 0 < len(entries) <= limits.headers:
        _reject("MANIFEST_SCHEMA")
    expected = {}
    for entry in entries:
        if not isinstance(entry, dict) or set(entry) != {"path", "size", "sha256"}:
            _reject("MANIFEST_SCHEMA")
        name = _path(entry["path"], limits.path_bytes)
        if name in expected:
            _reject("DUPLICATE_MANIFEST_PATH")
        if (type(entry["size"]) is not int or entry["size"] < 0
                or not isinstance(entry["sha256"], str) or not SHA256.fullmatch(entry["sha256"])):
            _reject("MANIFEST_SCHEMA")
        expected[name] = (entry["sha256"], entry["size"])
    actual = {path: value for path, value in files.items() if path not in {"manifest.json", "SHA256SUMS"}}
    if expected != actual:
        _reject("MANIFEST_MISMATCH")
    sums = _checksums(metadata["SHA256SUMS"], limits.metadata_bytes, limits.headers, limits.path_bytes)
    if sums != {path: value[0] for path, value in files.items() if path != "SHA256SUMS"}:
        _reject("INTERNAL_CHECKSUM_MISMATCH")
    # Parse, never import or execute the release's Python metadata.
    module = ast.parse(metadata["basswiesn/__init__.py"])
    assignments = [node for node in module.body if isinstance(node, ast.Assign)
                   and any(isinstance(target, ast.Name) and target.id == "__version__" for target in node.targets)]
    if (len(assignments) != 1 or not isinstance(assignments[0].value, ast.Constant)
            or assignments[0].value.value != version):
        _reject("RUNTIME_VERSION_MISMATCH")


def _fingerprint(raw: BinaryIO):
    value = os.fstat(raw.fileno())
    return value.st_dev, value.st_ino, value.st_size, value.st_mtime_ns, value.st_ctime_ns


def _hash(raw: BinaryIO, maximum: int) -> str:
    raw.seek(0)
    digest = hashlib.sha256()
    total = 0
    while block := raw.read(CHUNK):
        total += len(block)
        if total > maximum:
            _reject("COMPRESSED_LIMIT")
        digest.update(block)
    return digest.hexdigest()


def verify_open_archive(raw: BinaryIO, checksum_manifest: bytes, *, version: str,
                        limits: VerificationLimits = VerificationLimits()) -> VerifiedRelease:
    """Read-only verification on a caller-pinned regular file descriptor.

    No reopening a path. The host caller must additionally enforce private
    ownership, single-link staging, transport provenance and safe destination.
    """
    if not isinstance(version, str) or not VERSION.fullmatch(version):
        _reject("INVALID_VERSION")
    expected_name = f"basswiesn-docker-release-{version}.tar.gz"
    checksums = _checksums(checksum_manifest, 4096, 1, 255)
    if set(checksums) != {expected_name}:
        _reject("CHECKSUM_TARGET")
    try:
        if not stat.S_ISREG(os.fstat(raw.fileno()).st_mode):
            _reject("ARCHIVE_NOT_REGULAR")
        before = _fingerprint(raw)
        if not 0 < before[2] <= limits.compressed_bytes:
            _reject("COMPRESSED_LIMIT")
        digest = _hash(raw, limits.compressed_bytes)
        if digest != checksums[expected_name]:
            _reject("ARCHIVE_CHECKSUM_MISMATCH")
        raw.seek(0)
        reader = _GzipReader(raw, limits)
        files, metadata = _read_tar(reader, limits)
        _verify_contents(files, metadata, version, limits)
        if _hash(raw, limits.compressed_bytes) != digest or _fingerprint(raw) != before:
            _reject("ARCHIVE_CHANGED")
        return VerifiedRelease(version, digest, before[2], reader.expanded, len(files))
    except ArtifactRejected:
        raise
    except (OSError, tarfile.TarError, zlib.error, ValueError, TypeError, UnicodeError,
            SyntaxError, RecursionError, OverflowError):
        raise ArtifactRejected("MALFORMED_OR_UNREADABLE_ARCHIVE") from None


def verify_release_archive(archive: Path, checksum_manifest: bytes, *, version: str,
                           limits: VerificationLimits = VerificationLimits()) -> VerifiedRelease:
    """Verify a locally staged archive without extracting a single member.

    The caller supplies an approved stable version (without 'v') and the
    separately acquired SHA256SUMS. No source authenticity is inferred here.
    All errors are stable, content-free codes, including filesystem failures.
    """
    if not isinstance(version, str) or not VERSION.fullmatch(version):
        _reject("INVALID_VERSION")
    expected_name = f"basswiesn-docker-release-{version}.tar.gz"
    archive = Path(archive)
    if archive.name != expected_name:
        _reject("ARCHIVE_NAME")
    checksums = _checksums(checksum_manifest, 4096, 1, 255)
    if set(checksums) != {expected_name}:
        _reject("CHECKSUM_TARGET")
    # Linux host deployment contract: avoid symlinks and blocking FIFO opens.
    if not hasattr(os, "O_NOFOLLOW"):
        _reject("UNSUPPORTED_HOST")
    try:
        fd = os.open(archive, os.O_RDONLY | os.O_NOFOLLOW | os.O_NONBLOCK)
        with os.fdopen(fd, "rb") as raw:
            return verify_open_archive(raw, checksum_manifest, version=version, limits=limits)
    except ArtifactRejected:
        raise
    except (OSError, tarfile.TarError, zlib.error, ValueError, TypeError, UnicodeError,
            SyntaxError, RecursionError, OverflowError):
        raise ArtifactRejected("MALFORMED_OR_UNREADABLE_ARCHIVE") from None
