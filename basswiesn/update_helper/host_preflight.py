"""Read-only host-layout and Docker observation checks, with no app imports.

Enrollment is an administrator-owned policy, not a Web request. This checker
does not grant install permission, execute Compose, source .env, probe a radio,
repair permissions or claim that a live data backup has been verified.
"""
from contextlib import contextmanager
from dataclasses import dataclass, field
from enum import StrEnum
import hashlib
import json
import math
import os
from pathlib import PurePosixPath
import re
import stat
import time
from types import MappingProxyType

from .protocol import require_digest, require_request_id, require_version, UpdateRejected


MAX_POLICY_BYTES = 16384
MAX_CONFIG_BYTES = 1024 * 1024
RELEASE_FILES = frozenset({"docker-compose.yml", "Dockerfile", ".dockerignore", "basswiesn/__init__.py"})
SOURCE = "https://github.com/Zimbo88/BASSWIESN"
APP_UID = APP_GID = 10001
PORTS = frozenset({"1328/tcp", "1329/tcp", "1516/tcp", "1860/tcp"})


class HostCode(StrEnum):
    POLICY_INVALID = "POLICY_INVALID"
    UNSAFE_PATH = "UNSAFE_PATH"
    FILE_UNSAFE = "FILE_UNSAFE"
    FILE_CHANGED = "FILE_CHANGED"
    FILE_TOO_LARGE = "FILE_TOO_LARGE"
    FILE_MISSING = "FILE_MISSING"
    FILESYSTEM_UNAVAILABLE = "FILESYSTEM_UNAVAILABLE"
    CONFIG_CHANGED = "CONFIG_CHANGED"
    INSUFFICIENT_SPACE = "INSUFFICIENT_SPACE"
    RUNTIME_NOT_OBSERVED = "RUNTIME_NOT_OBSERVED"
    DOCKER_UNSUPPORTED = "DOCKER_UNSUPPORTED"
    CONTAINER_MISMATCH = "CONTAINER_MISMATCH"
    CONTAINER_UNHEALTHY = "CONTAINER_UNHEALTHY"
    IMAGE_MISMATCH = "IMAGE_MISMATCH"
    COMPOSE_MISMATCH = "COMPOSE_MISMATCH"
    RUNTIME_UNSAFE = "RUNTIME_UNSAFE"
    MOUNTS_UNSUPPORTED = "MOUNTS_UNSUPPORTED"
    PORTS_UNSUPPORTED = "PORTS_UNSUPPORTED"
    PROCESS_IDENTITY_UNVERIFIED = "PROCESS_IDENTITY_UNVERIFIED"
    LOCAL_DOCKER_UNAVAILABLE = "LOCAL_DOCKER_UNAVAILABLE"
    LOCAL_DOCKER_UNSAFE = "LOCAL_DOCKER_UNSAFE"
    OBSERVATION_CHANGED = "OBSERVATION_CHANGED"
    OBSERVATION_EXPIRED = "OBSERVATION_EXPIRED"


class HostRejected(ValueError):
    def __init__(self, code: HostCode):
        self.code = HostCode(code)
        super().__init__(self.code.value)


def unique_json(data):
    def pairs(items):
        result = {}
        for key, value in items:
            if key in result:
                raise HostRejected(HostCode.POLICY_INVALID)
            result[key] = value
        return result
    try:
        return json.loads(data, object_pairs_hook=pairs,
                          parse_constant=lambda _: (_ for _ in ()).throw(ValueError()))
    except (ValueError, TypeError, RecursionError):
        raise HostRejected(HostCode.POLICY_INVALID) from None


def absolute_path(value):
    if (not isinstance(value, str) or len(value) > 4096 or not value.startswith("/")
            or value.startswith("//") or any(ord(c) < 32 or ord(c) == 127 for c in value)
            or any(c in value for c in ("\\", ":", ",", '"'))):
        raise HostRejected(HostCode.UNSAFE_PATH)
    path = PurePosixPath(value)
    if str(path) != value or len(path.parts) < 3 or any(p in {".", ".."} for p in path.parts):
        raise HostRejected(HostCode.UNSAFE_PATH)
    return value


@dataclass(frozen=True, repr=False)
class Enrollment:
    """Trusted host policy. Never construct it from an HTTP install request."""
    installation_id: str
    root: str
    installation_owner_uid: int
    compose_project: str
    container_id: str
    image_id: str
    version: str
    file_sha256: dict = field(repr=False)
    env_sha256: str = field(repr=False)

    def __post_init__(self):
        object.__setattr__(self, "file_sha256", MappingProxyType(dict(self.file_sha256)))

    @classmethod
    def parse(cls, data: bytes):
        if type(data) is not bytes or not 0 < len(data) <= MAX_POLICY_BYTES:
            raise HostRejected(HostCode.POLICY_INVALID)
        value = unique_json(data)
        fields = {"schema", "installation_id", "root", "installation_owner_uid", "compose_project",
                  "container_id", "image_id", "version", "file_sha256", "env_sha256"}
        if not isinstance(value, dict) or set(value) != fields or type(value["schema"]) is not int or value["schema"] != 1:
            raise HostRejected(HostCode.POLICY_INVALID)
        try:
            require_request_id(value["installation_id"])
            absolute_path(value["root"])
            require_digest(value["container_id"])
            require_version(value["version"])
            require_digest(value["env_sha256"])
            if not isinstance(value["image_id"], str) or not value["image_id"].startswith("sha256:"):
                raise ValueError()
            require_digest(value["image_id"][7:])
            if (type(value["installation_owner_uid"]) is not int or not 0 <= value["installation_owner_uid"] < 2**32 - 1
                    or value["installation_owner_uid"] == APP_UID
                    or not isinstance(value["compose_project"], str)
                    or not re.fullmatch(r"[a-z0-9][a-z0-9_-]{0,62}", value["compose_project"])
                    or not isinstance(value["file_sha256"], dict) or set(value["file_sha256"]) != RELEASE_FILES):
                raise ValueError()
            for digest in value["file_sha256"].values():
                require_digest(digest)
        except (ValueError, TypeError, UpdateRejected):
            raise HostRejected(HostCode.POLICY_INVALID) from None
        return cls(**{k: v for k, v in value.items() if k != "schema"})


@contextmanager
def directory(path, *, owners, final_owners=None, private=False, runtime_group=False):
    """No symlink traversal; root-owned sticky temp parents allowed for tests."""
    absolute_path(path)
    fd = os.open("/", os.O_RDONLY | os.O_DIRECTORY)
    try:
        try:
            parts = PurePosixPath(path).parts[1:]
            for index, part in enumerate(parts):
                child = os.open(part, os.O_RDONLY | os.O_DIRECTORY | os.O_NOFOLLOW, dir_fd=fd)
                os.close(fd)
                fd = child
                info = os.fstat(fd)
                last = index == len(parts) - 1
                allowed = final_owners if last and final_owners is not None else owners
                sticky = info.st_uid == 0 and bool(info.st_mode & stat.S_ISVTX) and not last
                group_ok = runtime_group and last and info.st_gid == APP_GID
                if (info.st_uid not in allowed or (info.st_mode & 0o002 and not sticky)
                        or (info.st_mode & 0o020 and not (sticky or group_ok))
                        or (last and private and stat.S_IMODE(info.st_mode) != 0o700)):
                    raise HostRejected(HostCode.UNSAFE_PATH)
        except FileNotFoundError:
            raise HostRejected(HostCode.FILE_MISSING) from None
        except OSError:
            raise HostRejected(HostCode.UNSAFE_PATH) from None
        # Classify directory-opening errors, not unrelated I/O inside the body.
        # In particular a failed durable write is NOT a permissions diagnosis.
        yield fd
    finally:
        os.close(fd)


def _regular_read(parent, name, *, owners, limit, private=False):
    """Read only a pinned, single-link regular file; detect concurrent changes."""
    fd = None
    try:
        fd = os.open(name, os.O_RDONLY | os.O_NOFOLLOW | os.O_NONBLOCK, dir_fd=parent)
        before = os.fstat(fd)
        if (not stat.S_ISREG(before.st_mode) or before.st_uid not in owners or before.st_nlink != 1
                or before.st_mode & 0o022 or before.st_mode & 0o7000
                or (private and stat.S_IMODE(before.st_mode) != 0o600)):
            raise HostRejected(HostCode.FILE_UNSAFE)
        if not 0 <= before.st_size <= limit:
            raise HostRejected(HostCode.FILE_TOO_LARGE)
        result = bytearray()
        while len(result) <= limit:
            chunk = os.read(fd, min(65536, limit + 1 - len(result)))
            if not chunk:
                break
            result.extend(chunk)
        after = os.fstat(fd)
        current = os.stat(name, dir_fd=parent, follow_symlinks=False)
        identity = lambda s: (s.st_dev, s.st_ino, s.st_size, s.st_mtime_ns, s.st_ctime_ns, s.st_mode, s.st_uid, s.st_nlink)
        if identity(before) != identity(after) or identity(before) != identity(current):
            raise HostRejected(HostCode.FILE_CHANGED)
        if len(result) > limit:
            raise HostRejected(HostCode.FILE_TOO_LARGE)
        return bytes(result)
    except FileNotFoundError:
        raise HostRejected(HostCode.FILE_MISSING) from None
    except OSError:
        raise HostRejected(HostCode.FILE_UNSAFE) from None
    finally:
        if fd is not None:
            os.close(fd)


def load_enrollment(path: str, *, policy_owner_uid=0):
    """Production policy owner is root; alternate owner is for offline tests."""
    absolute_path(path)
    value = PurePosixPath(path)
    with directory(str(value.parent), owners={0, policy_owner_uid}, final_owners={policy_owner_uid}, private=True) as fd:
        data = _regular_read(fd, value.name, owners={policy_owner_uid}, limit=MAX_POLICY_BYTES, private=True)
    return Enrollment.parse(data)


def _runtime_directories(root_fd, owners):
    fd = os.dup(root_fd)
    try:
        for part in ("data", "secrets", "setup-rebuild"):
            child = os.open(part, os.O_RDONLY | os.O_DIRECTORY | os.O_NOFOLLOW, dir_fd=fd)
            os.close(fd)
            fd = child
            info = os.fstat(fd)
            if (info.st_uid not in owners | {APP_UID} or info.st_mode & 0o002
                    or (info.st_mode & 0o020 and info.st_gid != APP_GID)):
                raise HostRejected(HostCode.UNSAFE_PATH)
    except FileNotFoundError:
        raise HostRejected(HostCode.FILE_MISSING) from None
    except OSError:
        raise HostRejected(HostCode.UNSAFE_PATH) from None
    finally:
        os.close(fd)


def capture_configuration(root_path: str, *, installation_owner_uid: int):
    """Capture only fixed configuration fingerprints, never their contents.

    Shared by administrator enrollment and preflight. This does not authenticate
    a publisher, parse Compose, source .env or read application data.
    """
    if (type(installation_owner_uid) is not int or not 0 <= installation_owner_uid < 2**32 - 1
            or installation_owner_uid == APP_UID):
        raise HostRejected(HostCode.POLICY_INVALID)
    owners = {0, installation_owner_uid}
    hashes = {}
    with directory(root_path, owners=owners) as root:
        for name in sorted(RELEASE_FILES):
            if "/" in name:
                with directory(root_path + "/basswiesn", owners=owners) as subdir:
                    data = _regular_read(subdir, "__init__.py", owners=owners, limit=MAX_CONFIG_BYTES)
            else:
                data = _regular_read(root, name, owners=owners, limit=MAX_CONFIG_BYTES)
            hashes[name] = hashlib.sha256(data).hexdigest()
        # Hash only, never source .env or include values/digest in public output.
        env = _regular_read(root, ".env", owners=owners, limit=MAX_CONFIG_BYTES, private=True)
        # Compose implicit overrides are not part of the supported fixed layout.
        overrides = {"compose.yml", "compose.yaml", "docker-compose.yaml", "compose.override.yml",
                     "compose.override.yaml", "docker-compose.override.yml", "docker-compose.override.yaml"}
        for name in overrides:
            try:
                os.stat(name, dir_fd=root, follow_symlinks=False)
            except FileNotFoundError:
                continue
            raise HostRejected(HostCode.COMPOSE_MISMATCH)
        # Check fixed mount source directories, not their private file contents.
        # Recursive backup safety, ACL/effective write access and data integrity
        # remain separate executor gates; no live database or secret is read.
        _runtime_directories(root, owners)
    return hashes, hashlib.sha256(env).hexdigest()


def inspect_files(enrollment: Enrollment):
    """No directory recursion, DB read, env parsing, code import or write."""
    hashes, env_hash = capture_configuration(
        enrollment.root, installation_owner_uid=enrollment.installation_owner_uid,
    )
    if hashes != enrollment.file_sha256 or env_hash != enrollment.env_sha256:
        raise HostRejected(HostCode.CONFIG_CHANGED)


def inspect_space(staging_directory: str, *, owner_uid=0, required_bytes: int):
    """Explicit caller budget, NOT an estimate of the existing backup size."""
    if type(required_bytes) is not int or not 0 < required_bytes <= 2**63 - 1:
        raise HostRejected(HostCode.POLICY_INVALID)
    with directory(staging_directory, owners={0, owner_uid}, final_owners={owner_uid}, private=True) as fd:
        try:
            space = os.fstatvfs(fd)
        except OSError:
            raise HostRejected(HostCode.FILESYSTEM_UNAVAILABLE) from None
        if (space.f_flag & os.ST_RDONLY or space.f_frsize <= 0
                or space.f_bavail * space.f_frsize < required_bytes or space.f_favail < 128):
            raise HostRejected(HostCode.INSUFFICIENT_SPACE)


@dataclass(frozen=True, repr=False)
class ProcessIdentity:
    pid: int
    uid: int
    gid: int
    identity_uid_map: bool
    start_ticks: int


@dataclass(frozen=True, repr=False)
class RuntimeObservation:
    """Ephemeral local observations. Raw Docker environment/logs NEVER persist."""
    daemon: dict
    container: dict
    image: dict
    process: ProcessIdentity | None
    captured_monotonic: float = field(default_factory=time.monotonic)


def inspect_runtime(enrollment: Enrollment, observation: RuntimeObservation):
    try:
        _runtime_checks(enrollment, observation)
    except HostRejected:
        raise
    except (KeyError, TypeError, ValueError, AttributeError):
        raise HostRejected(HostCode.RUNTIME_UNSAFE) from None


def _runtime_checks(policy, observation):
    captured = observation.captured_monotonic
    if (type(captured) not in (int, float) or not math.isfinite(captured)
            or not 0 <= time.monotonic() - captured <= 15.0):
        raise HostRejected(HostCode.OBSERVATION_EXPIRED)
    info, container, image = observation.daemon, observation.container, observation.image
    security = info.get("SecurityOptions")
    if (info.get("OSType") != "linux" or not isinstance(security, list)
            or any(not isinstance(v, str) or "rootless" in v.lower() or "userns" in v.lower() for v in security)
            or info.get("Swarm", {}).get("LocalNodeState") not in {"inactive", ""}):
        raise HostRejected(HostCode.DOCKER_UNSUPPORTED)
    if container.get("Id") != policy.container_id or container.get("Image") != policy.image_id:
        raise HostRejected(HostCode.CONTAINER_MISMATCH)
    state = container["State"]
    if (state.get("Running") is not True or state.get("Paused") is not False
            or state.get("Restarting") is not False or state.get("OOMKilled") is not False
            or state.get("Dead") is not False or state.get("Status") != "running"
            or state.get("Health", {}).get("Status") != "healthy"):
        raise HostRejected(HostCode.CONTAINER_UNHEALTHY)
    process = observation.process
    if (not isinstance(process, ProcessIdentity) or type(state.get("Pid")) is not int
            or state["Pid"] <= 0 or process.pid != state["Pid"]
            or process.uid != APP_UID or process.gid != APP_GID
            or process.identity_uid_map is not True or process.start_ticks <= 0):
        raise HostRejected(HostCode.PROCESS_IDENTITY_UNVERIFIED)
    cfg, img_cfg, host = container["Config"], image["Config"], container["HostConfig"]
    labels = cfg["Labels"]
    if (labels.get("com.docker.compose.project") != policy.compose_project
            or labels.get("com.docker.compose.service") != "basswiesn"
            or labels.get("com.docker.compose.oneoff") != "False"
            or labels.get("com.docker.compose.project.working_dir") != policy.root
            or labels.get("com.docker.compose.project.config_files") != policy.root + "/docker-compose.yml"):
        raise HostRejected(HostCode.COMPOSE_MISMATCH)
    if (image.get("Id") != policy.image_id or image.get("Os") != "linux"
            or image.get("Architecture") not in {"amd64", "arm64", "arm"}
            or img_cfg.get("Labels", {}).get("org.opencontainers.image.version") != policy.version
            or img_cfg.get("Labels", {}).get("org.opencontainers.image.source") != SOURCE):
        raise HostRejected(HostCode.IMAGE_MISMATCH)
    for config in (cfg, img_cfg):
        if (config.get("User") not in {"basswiesn", "10001", "10001:10001"}
                or config.get("WorkingDir") != "/app" or config.get("Entrypoint") not in (None, [])
                or config.get("Cmd") != ["python", "tools/run_dev.py"]):
            raise HostRejected(HostCode.RUNTIME_UNSAFE)
    empty = (None, [])
    if (host.get("Privileged") is not False or host.get("ReadonlyRootfs") is not True
            or host.get("Init") is not True or host.get("CapDrop") != ["ALL"]
            or host.get("CapAdd") not in empty
            or host.get("SecurityOpt") not in (["no-new-privileges"], ["no-new-privileges:true"])
            or host.get("NetworkMode") != policy.compose_project + "_default"
            or host.get("PidMode") != "" or host.get("IpcMode") not in {"private", ""}
            or host.get("UTSMode") != "" or host.get("UsernsMode") != ""
            or any(host.get(k) not in empty for k in ("Devices", "DeviceRequests", "VolumesFrom", "Links"))
            or host.get("RestartPolicy", {}).get("Name") != "unless-stopped"):
        raise HostRejected(HostCode.RUNTIME_UNSAFE)
    tmpfs = host.get("Tmpfs")
    if (not isinstance(tmpfs, dict) or set(tmpfs) != {"/tmp"}
            or tmpfs["/tmp"] != "rw,noexec,nosuid,size=64m"):
        raise HostRejected(HostCode.RUNTIME_UNSAFE)
    expected = {"/app/data": (policy.root + "/data", True),
                "/app/secrets/setup-rebuild": (policy.root + "/data/secrets/setup-rebuild", False)}
    mounts = container["Mounts"]
    # The optional third bind exposes ONLY the helper's socket directory,
    # read-only. Disabled installations bind an empty application directory.
    # Neither /run nor docker.sock nor helper credentials are exposed.
    if isinstance(mounts, list) and len(mounts) == 3:
        controls = [m for m in mounts if m.get("Destination") == "/run/basswiesn-update"]
        if len(controls) != 1 or controls[0].get("Source") not in {
                "/run/basswiesn-update", policy.root + "/data/update-disabled"}:
            raise HostRejected(HostCode.MOUNTS_UNSUPPORTED)
        expected["/run/basswiesn-update"] = (controls[0]["Source"], False)
    if not isinstance(mounts, list) or len(mounts) != len(expected):
        raise HostRejected(HostCode.MOUNTS_UNSUPPORTED)
    seen = set()
    for mount in mounts:
        destination = mount.get("Destination")
        if destination not in expected or destination in seen:
            raise HostRejected(HostCode.MOUNTS_UNSUPPORTED)
        seen.add(destination)
        source, writable = expected[destination]
        if (mount.get("Type") != "bind" or mount.get("Source") != source
                or mount.get("RW") is not writable or mount.get("Propagation") != "rprivate"):
            raise HostRejected(HostCode.MOUNTS_UNSUPPORTED)
    ports = host.get("PortBindings")
    if not isinstance(ports, dict) or set(ports) != PORTS:
        raise HostRejected(HostCode.PORTS_UNSUPPORTED)
    for port, bindings in ports.items():
        if not isinstance(bindings, list) or not 1 <= len(bindings) <= 2:
            raise HostRejected(HostCode.PORTS_UNSUPPORTED)
        seen_hosts = set()
        for binding in bindings:
            if (set(binding) != {"HostIp", "HostPort"} or binding["HostIp"] not in {"", "0.0.0.0", "::", "127.0.0.1", "::1"}
                    or binding["HostPort"] != port.split("/")[0] or binding["HostIp"] in seen_hosts):
                raise HostRejected(HostCode.PORTS_UNSUPPORTED)
            seen_hosts.add(binding["HostIp"])


def evaluate(enrollment: Enrollment, *, observation: RuntimeObservation | None = None):
    """Public result excludes paths, container IDs, env/config hashes and logs."""
    checks = []
    for name, check in (("installation_files", lambda: inspect_files(enrollment)),
                        ("runtime_layout", lambda: inspect_runtime(enrollment, observation))):
        if name == "runtime_layout" and observation is None:
            checks.append({"check": name, "status": "UNKNOWN", "code": HostCode.RUNTIME_NOT_OBSERVED.value})
            continue
        try:
            check()
            checks.append({"check": name, "status": "PASS", "code": None})
        except HostRejected as error:
            checks.append({"check": name, "status": "FAIL", "code": error.code.value})
    return {"layout_checks_passed": all(v["status"] == "PASS" for v in checks),
            "installation_available": False, "backup_restore_verified": False,
            "onboarding_complete": False, "checks": checks}
