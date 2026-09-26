"""Official stable assets -> private verified source tree. Never execute it.

No Web-controlled URL/path, extraction into production, chmod of existing files,
cleanup, Docker action, .env overlay or automatic retry. A failed reservation
remains private and cannot be reused under the same job ID.
"""
from contextlib import contextmanager
from dataclasses import dataclass
from datetime import datetime
import hashlib
import io
import json
import os
import re
import stat
import time

from basswiesn.app.services import release_artifact as archive
from .host_preflight import directory, _regular_read, unique_json, inspect_space
from .official_transport import API, DownloadRejected, fetch_into
from .protocol import require_request_id, require_version


STAGING_ROOT = "/var/lib/basswiesn-update/staging"
REPOSITORY = "https://github.com/Zimbo88/BASSWIESN"
MAX_RELEASE_BYTES = 128 * 1024
LIMITS = archive.VerificationLimits()


class StagingRejected(ValueError):
    def __init__(self, code):
        if code not in {"STAGING_EXISTS", "STAGING_UNSAFE", "STAGING_IO", "RELEASE_INVALID",
                        "RELEASE_CHANGED", "ASSET_MISMATCH", "RUNTIME_OVERLAY", "TREE_MISMATCH",
                        "STAGING_INVALID", "STAGING_TIMEOUT"}:
            code = "STAGING_INVALID"
        self.code = code
        super().__init__(code)


def _json(value):
    return (json.dumps(value, sort_keys=True, separators=(",", ":")) + "\n").encode("ascii")


def _sha(value):
    return hashlib.sha256(value).hexdigest()


@dataclass(frozen=True)
class Asset:
    asset_id: int
    name: str
    size: int
    digest: str | None

    @property
    def url(self):
        return API + "assets/" + str(self.asset_id)


@dataclass(frozen=True)
class Release:
    release_id: int
    version: str
    published_at: str
    assets: tuple[Asset, Asset]


def parse_release(data, version):
    require_version(version)
    if type(data) is not bytes or not 0 < len(data) <= MAX_RELEASE_BYTES:
        raise StagingRejected("RELEASE_INVALID")
    try:
        value = unique_json(data)
        tag = "v" + version
        if (not isinstance(value, dict) or type(value.get("id")) is not int or not 0 < value["id"] < 2**63
                or value.get("tag_name") != tag or value.get("draft") is not False
                or value.get("prerelease") is not False
                or value.get("html_url") != REPOSITORY + "/releases/tag/" + tag):
            raise ValueError()
        published = value["published_at"]
        if (not isinstance(published, str) or not re.fullmatch(r"\d{4}-\d\d-\d\dT\d\d:\d\d:\d\dZ", published)
                or datetime.fromisoformat(published[:-1] + "+00:00").year < 2008):
            raise ValueError()
        items = value["assets"]
        if not isinstance(items, list) or not 2 <= len(items) <= 100:
            raise ValueError()
        wanted = {"SHA256SUMS": 4096, f"basswiesn-docker-release-{version}.tar.gz": LIMITS.compressed_bytes}
        found = {}
        for item in items:
            if not isinstance(item, dict):
                raise ValueError()
            name = item.get("name")
            if not isinstance(name, str) or name not in wanted:
                continue
            identity, size, digest = item.get("id"), item.get("size"), item.get("digest")
            if (name in found or type(identity) is not int or not 0 < identity < 2**63
                    or type(size) is not int or not 0 < size <= wanted[name]
                    or item.get("state") != "uploaded"
                    or item.get("url") != API + "assets/" + str(identity)
                    or item.get("browser_download_url") != REPOSITORY + "/releases/download/" + tag + "/" + name):
                raise ValueError()
            if digest is not None:
                if (not isinstance(digest, str) or not digest.startswith("sha256:")
                        or not archive.SHA256.fullmatch(digest[7:])):
                    raise ValueError()
            found[name] = Asset(identity, name, size, digest[7:] if digest else None)
        if set(found) != set(wanted) or len({v.asset_id for v in found.values()}) != 2:
            raise ValueError()
        return Release(value["id"], version, published, tuple(found[n] for n in sorted(wanted)))
    except (ValueError, TypeError, KeyError, OverflowError):
        raise StagingRejected("RELEASE_INVALID") from None


def _private_file(fd, owner_uid):
    info = os.fstat(fd)
    if (not stat.S_ISREG(info.st_mode) or info.st_uid != owner_uid
            or info.st_nlink != 1 or stat.S_IMODE(info.st_mode) != 0o600):
        raise StagingRejected("STAGING_UNSAFE")
    return info


@contextmanager
def _new_file(parent, name, owner_uid):
    fd = os.open(name, os.O_RDWR | os.O_CREAT | os.O_EXCL | os.O_NOFOLLOW, 0o600, dir_fd=parent)
    with os.fdopen(fd, "w+b") as raw:
        _private_file(raw.fileno(), owner_uid)
        yield raw
        raw.flush()
        os.fsync(raw.fileno())
    os.fsync(parent)


def _mkdir(parent, name, owner_uid):
    os.mkdir(name, 0o700, dir_fd=parent)
    fd = os.open(name, os.O_RDONLY | os.O_DIRECTORY | os.O_NOFOLLOW, dir_fd=parent)
    try:
        info = os.fstat(fd)
        if info.st_uid != owner_uid or stat.S_IMODE(info.st_mode) != 0o700:
            raise StagingRejected("STAGING_UNSAFE")
        os.fsync(fd)
        os.fsync(parent)
        return fd
    except BaseException:
        os.close(fd)
        raise


def _policy_path(name, *, is_dir):
    relative = name[len(archive.ROOT) + 1:] if name != archive.ROOT else ""
    parts = relative.split("/") if relative else []
    # Release data/ is allowed EMPTY, never populated or overlaid on live state.
    if ((parts and parts[0] in {"data", "backups", "secrets", "private", ".git", ".ssh"}
         and not (relative == "data" and is_dir))
            or any(part in {".env", ".git", ".ssh", "__pycache__"} for part in parts)
            or any(part.startswith(".env.") and part != ".env.example" for part in parts)
            or any(part.endswith((".db", ".sqlite", ".sqlite3", ".log", ".pyc", ".pyo")) for part in parts)
            or relative in {"docker-compose.override.yml", "docker-compose.override.yaml", "compose.override.yml",
                            "compose.override.yaml", "compose.yml", "compose.yaml"}):
        raise StagingRejected("RUNTIME_OVERLAY")
    return relative


class _Discard:
    def write(self, block):
        return len(block)


class _Index:
    def __init__(self):
        self.directories = set()
        self.modes = {}

    def directory(self, name):
        self.directories.add(_policy_path(name, is_dir=True))

    @contextmanager
    def file(self, name, mode):
        relative = _policy_path(name, is_dir=False)
        self.modes[relative] = 0o700 if mode & 0o100 else 0o600
        yield _Discard()


@contextmanager
def _parent(root, relative):
    fd = os.dup(root)
    try:
        parts = relative.split("/")
        for part in parts[:-1]:
            child = os.open(part, os.O_RDONLY | os.O_DIRECTORY | os.O_NOFOLLOW, dir_fd=fd)
            os.close(fd)
            fd = child
        yield fd, parts[-1]
    finally:
        os.close(fd)


class _TreeSink:
    def __init__(self, root, index, owner_uid):
        self.root, self.index, self.owner_uid = root, index, owner_uid

    def directory(self, name):
        if _policy_path(name, is_dir=True) not in self.index.directories:
            raise StagingRejected("TREE_MISMATCH")

    @contextmanager
    def file(self, name, mode):
        relative = _policy_path(name, is_dir=False)
        expected_mode = self.index.modes.get(relative)
        if expected_mode != (0o700 if mode & 0o100 else 0o600):
            raise StagingRejected("TREE_MISMATCH")
        with _parent(self.root, relative) as (parent, leaf):
            with _new_file(parent, leaf, self.owner_uid) as raw:
                yield raw
                # Only this newly created descriptor, never an existing path.
                os.fchmod(raw.fileno(), expected_mode)


def _scan(raw, version):
    raw.seek(0)
    index = _Index()
    files, metadata = archive._read_tar(archive._GzipReader(raw, LIMITS), LIMITS, sink=index)
    archive._verify_contents(files, metadata, version, LIMITS)
    return index, files


def _verify_tree(root, index, files, owner_uid):
    children = {name: set() for name in index.directories}
    for name in (index.directories - {""}) | files.keys():
        parent, _, leaf = name.rpartition("/")
        children[parent].add(leaf)
    for relative in sorted(index.directories):
        with _parent(root, relative + "/placeholder" if relative else "placeholder") as (fd, _):
            info = os.fstat(fd)
            if (info.st_uid != owner_uid or stat.S_IMODE(info.st_mode) != 0o700
                    or set(os.listdir(fd)) != children[relative]):
                raise StagingRejected("TREE_MISMATCH")
    for relative, (digest, size) in sorted(files.items()):
        with _parent(root, relative) as (parent, leaf):
            fd = os.open(leaf, os.O_RDONLY | os.O_NOFOLLOW | os.O_NONBLOCK, dir_fd=parent)
            with os.fdopen(fd, "rb") as raw:
                info = os.fstat(raw.fileno())
                before = archive._fingerprint(raw)
                if (not stat.S_ISREG(info.st_mode) or info.st_uid != owner_uid or info.st_nlink != 1
                        or info.st_size != size or stat.S_IMODE(info.st_mode) != index.modes[relative]
                        or archive._hash(raw, LIMITS.member_bytes) != digest):
                    raise StagingRejected("TREE_MISMATCH")
                after = os.stat(leaf, dir_fd=parent, follow_symlinks=False)
                if (archive._fingerprint(raw) != before
                        or (after.st_dev, after.st_ino, after.st_size, after.st_mtime_ns, after.st_ctime_ns) != before):
                    raise StagingRejected("TREE_MISMATCH")
    return _sha(_json({name: [digest, size, index.modes[name]] for name, (digest, size) in sorted(files.items())}))


def _extract(raw, sums, version, job, owner_uid):
    verified = archive.verify_open_archive(raw, sums, version=version)
    before = archive._fingerprint(raw)
    index, files = _scan(raw, version)  # Reject runtime overlays before any output member.
    if archive._hash(raw, LIMITS.compressed_bytes) != verified.archive_sha256:
        raise archive.ArtifactRejected("ARCHIVE_CHANGED")
    tree = _mkdir(job, "source", owner_uid)
    try:
        for relative in sorted(index.directories - {""}, key=lambda v: (v.count("/"), v)):
            with _parent(tree, relative) as (parent, leaf):
                child = _mkdir(parent, leaf, owner_uid)
                os.close(child)
        raw.seek(0)
        extracted, metadata = archive._read_tar(archive._GzipReader(raw, LIMITS), LIMITS,
                                                sink=_TreeSink(tree, index, owner_uid))
        archive._verify_contents(extracted, metadata, version, LIMITS)
        if (files != extracted or archive._fingerprint(raw) != before
                or archive._hash(raw, LIMITS.compressed_bytes) != verified.archive_sha256):
            raise archive.ArtifactRejected("ARCHIVE_CHANGED")
        tree_hash = _verify_tree(tree, index, files, owner_uid)
        os.fsync(tree)
        os.fsync(job)
    finally:
        os.close(tree)
    return verified, tree_hash


def _release(fetch, version, deadline):
    output = io.BytesIO()
    fetch(API + "tags/v" + version, output, maximum=MAX_RELEASE_BYTES, deadline=deadline)
    return parse_release(output.getvalue(), version)


def _download(fetch, asset, job, owner_uid, deadline):
    with _new_file(job, asset.name, owner_uid) as raw:
        fetch(asset.url, raw, maximum=asset.size, deadline=deadline)
        raw.flush()
        if os.fstat(raw.fileno()).st_size != asset.size:
            raise StagingRejected("ASSET_MISMATCH")
        digest = archive._hash(raw, asset.size)
        if asset.digest is not None and digest != asset.digest:
            raise StagingRejected("ASSET_MISMATCH")
    return digest


def stage_release(request_id, version, *, staging_root=STAGING_ROOT, owner_uid=0, fetch=fetch_into):
    """Trusted host-side entry point; never call directly from an unauthenticated UI.

    The production root/UID/transport are fixed. Overrides exist only for isolated
    tests. Failure preserves its reservation; no automatic replacement or cleanup.
    """
    require_request_id(request_id)
    require_version(version)
    if type(owner_uid) is not int or owner_uid < 0 or os.geteuid() != owner_uid:
        raise StagingRejected("STAGING_UNSAFE")
    inspect_space(staging_root, owner_uid=owner_uid,
                  required_bytes=LIMITS.compressed_bytes + LIMITS.expanded_bytes + MAX_RELEASE_BYTES)
    deadline = time.monotonic() + 120
    try:
        with directory(staging_root, owners={0, owner_uid}, final_owners={owner_uid}, private=True) as parent:
            try:
                job = _mkdir(parent, request_id, owner_uid)
            except FileExistsError:
                raise StagingRejected("STAGING_EXISTS") from None
            try:
                with _new_file(job, "reservation.json", owner_uid) as raw:
                    raw.write(_json({"schema": 1, "request_id": request_id, "version": version}))
                release = _release(fetch, version, deadline)
                checksums, package = release.assets  # SHA256SUMS sorts before archive.
                sum_hash = _download(fetch, checksums, job, owner_uid, deadline)
                package_hash = _download(fetch, package, job, owner_uid, deadline)
                sums = _regular_read(job, "SHA256SUMS", owners={owner_uid}, limit=4096, private=True)
                fd = os.open(package.name, os.O_RDONLY | os.O_NOFOLLOW | os.O_NONBLOCK, dir_fd=job)
                with os.fdopen(fd, "rb") as raw:
                    _private_file(raw.fileno(), owner_uid)
                    verified, tree_hash = _extract(raw, sums, version, job, owner_uid)
                    if verified.archive_sha256 != package_hash or _sha(sums) != sum_hash:
                        raise StagingRejected("ASSET_MISMATCH")
                    current = os.stat(package.name, dir_fd=job, follow_symlinks=False)
                    if (current.st_dev, current.st_ino) != archive._fingerprint(raw)[:2]:
                        raise StagingRejected("STAGING_UNSAFE")
                if _release(fetch, version, deadline) != release:
                    raise StagingRejected("RELEASE_CHANGED")
                if time.monotonic() >= deadline:
                    raise StagingRejected("STAGING_TIMEOUT")
                receipt = {"schema": 1, "request_id": request_id, "version": version,
                    "repository": REPOSITORY, "release_id": release.release_id,
                    "published_at": release.published_at,
                    "checksum_asset_id": checksums.asset_id, "archive_asset_id": package.asset_id,
                    "checksums_sha256": sum_hash, "archive_sha256": package_hash, "tree_sha256": tree_hash,
                    "file_count": verified.file_count, "state": "SOURCE_STAGED"}
                with _new_file(job, "ready.json", owner_uid) as raw:
                    raw.write(_json(receipt))
            finally:
                os.close(job)
        return verify_staged_release(request_id, version, staging_root=staging_root, owner_uid=owner_uid)
    except (StagingRejected, DownloadRejected, archive.ArtifactRejected):
        raise
    except OSError:
        raise StagingRejected("STAGING_IO") from None
    except Exception:
        raise StagingRejected("STAGING_INVALID") from None


def verify_staged_release(request_id, version, *, staging_root=STAGING_ROOT, owner_uid=0):
    """Reverify private source/receipt before use; no execution or new download."""
    require_request_id(request_id)
    require_version(version)
    filename = f"basswiesn-docker-release-{version}.tar.gz"
    try:
        with directory(staging_root + "/" + request_id, owners={0, owner_uid},
                       final_owners={owner_uid}, private=True) as job:
            if set(os.listdir(job)) != {"reservation.json", "ready.json", "SHA256SUMS", filename, "source"}:
                raise StagingRejected("STAGING_INVALID")
            def read(name, limit):
                return _regular_read(job, name, owners={owner_uid}, limit=limit, private=True)
            reservation = unique_json(read("reservation.json", 4096))
            if _json(reservation) != _json({"schema": 1, "request_id": request_id, "version": version}):
                raise StagingRejected("STAGING_INVALID")
            receipt = unique_json(read("ready.json", 4096))
            fields = {"schema", "request_id", "version", "repository", "release_id", "published_at",
                      "checksum_asset_id", "archive_asset_id", "checksums_sha256", "archive_sha256",
                      "tree_sha256", "file_count", "state"}
            if (not isinstance(receipt, dict) or set(receipt) != fields
                    or type(receipt["schema"]) is not int or receipt["schema"] != 1
                    or receipt["request_id"] != request_id or receipt["version"] != version
                    or receipt["repository"] != REPOSITORY or receipt["state"] != "SOURCE_STAGED"
                    or any(type(receipt[k]) is not int or not 0 < receipt[k] < 2**63
                           for k in ("release_id", "checksum_asset_id", "archive_asset_id", "file_count"))
                    or receipt["checksum_asset_id"] == receipt["archive_asset_id"]
                    or not isinstance(receipt["published_at"], str) or len(receipt["published_at"]) > 32):
                raise StagingRejected("STAGING_INVALID")
            sums = read("SHA256SUMS", 4096)
            fd = os.open(filename, os.O_RDONLY | os.O_NOFOLLOW | os.O_NONBLOCK, dir_fd=job)
            with os.fdopen(fd, "rb") as raw:
                _private_file(raw.fileno(), owner_uid)
                verified = archive.verify_open_archive(raw, sums, version=version)
                before = archive._fingerprint(raw)
                index, files = _scan(raw, version)
                with directory(staging_root + "/" + request_id + "/source", owners={0, owner_uid},
                               final_owners={owner_uid}, private=True) as tree:
                    tree_hash = _verify_tree(tree, index, files, owner_uid)
                if (receipt["checksums_sha256"] != _sha(sums)
                        or receipt["archive_sha256"] != verified.archive_sha256
                        or receipt["tree_sha256"] != tree_hash or receipt["file_count"] != verified.file_count):
                    raise StagingRejected("TREE_MISMATCH")
                if (archive._fingerprint(raw) != before
                        or archive._hash(raw, LIMITS.compressed_bytes) != verified.archive_sha256):
                    raise archive.ArtifactRejected("ARCHIVE_CHANGED")
                current = os.stat(filename, dir_fd=job, follow_symlinks=False)
                if (current.st_dev, current.st_ino) != before[:2]:
                    raise StagingRejected("STAGING_UNSAFE")
            return {"state": "SOURCE_STAGED", "request_id": request_id, "version": version,
                    "archive_sha256": verified.archive_sha256, "file_count": verified.file_count,
                    "integrity_verified": True, "installation_available": False,
                    "signature_verified": False, "installation_performed": False}
    except (StagingRejected, archive.ArtifactRejected):
        raise
    except OSError:
        raise StagingRejected("STAGING_IO") from None
    except Exception:
        raise StagingRejected("STAGING_INVALID") from None
