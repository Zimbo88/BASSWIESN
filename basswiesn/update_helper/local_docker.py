"""Explicit root-only, read-only inspection of the LOCAL Docker daemon.

No command accepts a URL, executable, daemon address or arbitrary Docker option.
No source .env/Compose evaluation, exec, logs, start, stop, build, pull or writes.
Stopped-writer verification may enumerate active containers and inspect mount
metadata to refuse a snapshot while another container shares writable data.
Environment/context/proxy overrides are excluded. Raw inspection JSON (including
container environment) stays in memory and is never logged or saved.
"""
import os
import re
import selectors
import stat
import subprocess
import tempfile
import time

from .host_preflight import (Enrollment, HostCode, HostRejected, ProcessIdentity,
                             RuntimeObservation, directory, unique_json)


DOCKER = "/usr/bin/docker"
SOCKET = "/run/docker.sock"
CLIENT_CONFIG = "/var/lib/basswiesn-update/docker-client"
MAX_OUTPUT = 2 * 1024 * 1024
TOTAL_SECONDS = 15.0
WRITER_LIST = ["ps", "--no-trunc", "--format", "{{json .ID}}"]


def _trusted_tools():
    if os.geteuid() != 0:
        raise HostRejected(HostCode.LOCAL_DOCKER_UNSAFE)
    try:
        with directory("/usr/bin", owners={0}) as fd:
            binary = os.stat("docker", dir_fd=fd, follow_symlinks=False)
            if (not stat.S_ISREG(binary.st_mode) or binary.st_uid != 0 or binary.st_mode & 0o7022
                    or not binary.st_mode & stat.S_IXUSR):
                raise HostRejected(HostCode.LOCAL_DOCKER_UNSAFE)
        # /run is a fixed root-owned parent; no /var/run symlink traversal.
        parent = os.open("/run", os.O_RDONLY | os.O_DIRECTORY | os.O_NOFOLLOW)
        try:
            info = os.fstat(parent)
            endpoint = os.stat("docker.sock", dir_fd=parent, follow_symlinks=False)
            if (info.st_uid != 0 or info.st_mode & 0o022 or not stat.S_ISSOCK(endpoint.st_mode)
                    or endpoint.st_uid != 0 or endpoint.st_mode & 0o002):
                raise HostRejected(HostCode.LOCAL_DOCKER_UNSAFE)
        finally:
            os.close(parent)
        with directory(CLIENT_CONFIG, owners={0}, final_owners={0}, private=True) as fd:
            if os.listdir(fd):
                raise HostRejected(HostCode.LOCAL_DOCKER_UNSAFE)
    except (OSError, HostRejected):
        raise HostRejected(HostCode.LOCAL_DOCKER_UNSAFE) from None


def _read_command(arguments, deadline):
    """Fixed caller-generated argv, bounded stdout/time; stderr never retained."""
    process = None
    client_directory = None
    try:
        if not isinstance(arguments, list) or not all(isinstance(v, str) for v in arguments):
            raise HostRejected(HostCode.LOCAL_DOCKER_UNSAFE)
        allowed = arguments in (["info", "--format", "{{json .}}"], WRITER_LIST)
        if len(arguments) == 4 and arguments[:2] == ["inspect", "--type"]:
            kind, identity = arguments[2:]
            pattern = r"[0-9a-f]{64}" if kind == "container" else r"sha256:[0-9a-f]{64}" if kind == "image" else None
            allowed = bool(pattern and isinstance(identity, str) and re.fullmatch(pattern, identity))
        if not allowed:
            raise HostRejected(HostCode.LOCAL_DOCKER_UNSAFE)
        remaining = deadline - time.monotonic()
        if remaining <= 0:
            raise HostRejected(HostCode.LOCAL_DOCKER_UNAVAILABLE)
        client_directory = tempfile.TemporaryDirectory(prefix="command-", dir=CLIENT_CONFIG)
        process = subprocess.Popen(
            [DOCKER, "--host", "unix://" + SOCKET, "--config", client_directory.name, *arguments],
            stdin=subprocess.DEVNULL, stdout=subprocess.PIPE, stderr=subprocess.DEVNULL,
            cwd="/", env={"PATH": "/usr/bin:/bin", "LC_ALL": "C"}, close_fds=True,
        )
        output = bytearray()
        with selectors.DefaultSelector() as selector:
            selector.register(process.stdout, selectors.EVENT_READ)
            while True:
                remaining = deadline - time.monotonic()
                if remaining <= 0:
                    raise HostRejected(HostCode.LOCAL_DOCKER_UNAVAILABLE)
                if not selector.select(remaining):
                    raise HostRejected(HostCode.LOCAL_DOCKER_UNAVAILABLE)
                chunk = os.read(process.stdout.fileno(), min(65536, MAX_OUTPUT + 1 - len(output)))
                if not chunk:
                    break
                output.extend(chunk)
                if len(output) > MAX_OUTPUT:
                    raise HostRejected(HostCode.LOCAL_DOCKER_UNAVAILABLE)
        process.wait(timeout=max(0.001, deadline - time.monotonic()))
        if process.returncode:
            raise HostRejected(HostCode.LOCAL_DOCKER_UNAVAILABLE)
        try:
            if arguments == WRITER_LIST:
                lines = bytes(output).splitlines()
                if len(lines) > 256:
                    raise HostRejected(HostCode.LOCAL_DOCKER_UNAVAILABLE)
                identities = [unique_json(line) for line in lines]
                if (any(not isinstance(v, str) or not re.fullmatch(r"[0-9a-f]{64}", v) for v in identities)
                        or len(set(identities)) != len(identities)):
                    raise HostRejected(HostCode.LOCAL_DOCKER_UNAVAILABLE)
                return identities
            return unique_json(bytes(output))
        except HostRejected:
            raise HostRejected(HostCode.LOCAL_DOCKER_UNAVAILABLE) from None
    except HostRejected:
        raise
    except (OSError, ValueError, subprocess.SubprocessError):
        raise HostRejected(HostCode.LOCAL_DOCKER_UNAVAILABLE) from None
    finally:
        if process is not None:
            if process.poll() is None:
                process.kill()  # only the exact inspection child we just created
            process.wait()
            if process.stdout is not None:
                process.stdout.close()
        if client_directory is not None:
            client_directory.cleanup()


def _proc_read(pid, name):
    if type(pid) is not int or not 1 <= pid <= 2**31 - 1 or name not in {"status", "stat", "uid_map", "gid_map"}:
        raise HostRejected(HostCode.PROCESS_IDENTITY_UNVERIFIED)
    fd = None
    try:
        fd = os.open(f"/proc/{pid}/{name}", os.O_RDONLY | os.O_NOFOLLOW | os.O_NONBLOCK)
        data = bytearray()
        while len(data) <= 65536:
            chunk = os.read(fd, min(8192, 65537 - len(data)))
            if not chunk:
                break
            data.extend(chunk)
        if len(data) > 65536:
            raise ValueError()
        return bytes(data).decode("ascii")
    except (OSError, ValueError):
        raise HostRejected(HostCode.PROCESS_IDENTITY_UNVERIFIED) from None
    finally:
        if fd is not None:
            os.close(fd)


def _start_ticks(text):
    # comm may itself contain spaces/parentheses. Field22 follows the final ')'.
    try:
        tail = text.rsplit(")", 1)[1].split()
        value = int(tail[19])
        if value <= 0:
            raise ValueError()
        return value
    except (ValueError, IndexError):
        raise HostRejected(HostCode.PROCESS_IDENTITY_UNVERIFIED) from None


def process_identity(pid):
    before = _start_ticks(_proc_read(pid, "stat"))
    try:
        fields = {}
        for line in _proc_read(pid, "status").splitlines():
            key, separator, value = line.partition(":")
            if separator and key in {"Uid", "Gid"}:
                if key in fields:
                    raise ValueError()
                numbers = list(map(int, value.split()))
                if len(numbers) != 4 or len(set(numbers)) != 1 or numbers[0] < 0:
                    raise ValueError()
                fields[key] = numbers[0]
        maps = [_proc_read(pid, name).split() for name in ("uid_map", "gid_map")]
        same_namespace = all(value == ["0", "0", "4294967295"] for value in maps)
        after = _start_ticks(_proc_read(pid, "stat"))
        if before != after:
            raise HostRejected(HostCode.OBSERVATION_CHANGED)
        return ProcessIdentity(pid, fields["Uid"], fields["Gid"], same_namespace, after)
    except HostRejected:
        raise
    except (KeyError, ValueError):
        raise HostRejected(HostCode.PROCESS_IDENTITY_UNVERIFIED) from None


def _single(value):
    if not isinstance(value, list) or len(value) != 1 or not isinstance(value[0], dict):
        raise HostRejected(HostCode.LOCAL_DOCKER_UNAVAILABLE)
    return value[0]


def collect(enrollment: Enrollment):
    """Explicit local inspection only. No discovery/list-all or remote daemon."""
    _trusted_tools()
    deadline = time.monotonic() + TOTAL_SECONDS
    daemon = _read_command(["info", "--format", "{{json .}}"], deadline)
    first = _single(_read_command(["inspect", "--type", "container", enrollment.container_id], deadline))
    if first.get("Id") != enrollment.container_id or first.get("Image") != enrollment.image_id:
        raise HostRejected(HostCode.CONTAINER_MISMATCH)
    image = _single(_read_command(["inspect", "--type", "image", enrollment.image_id], deadline))
    try:
        if first["State"].get("Running") is not True or not first["State"].get("Pid"):
            raise HostRejected(HostCode.CONTAINER_UNHEALTHY)
        identity = process_identity(first["State"]["Pid"])
        final = _single(_read_command(["inspect", "--type", "container", enrollment.container_id], deadline))
        if (any(first.get(key) != final.get(key) for key in ("Id", "Image", "RestartCount"))
                or any(first["State"].get(key) != final["State"].get(key) for key in ("Pid", "StartedAt"))
                or process_identity(final["State"]["Pid"]) != identity):
            raise HostRejected(HostCode.OBSERVATION_CHANGED)
    except (KeyError, TypeError):
        raise HostRejected(HostCode.LOCAL_DOCKER_UNAVAILABLE) from None
    if time.monotonic() > deadline:
        raise HostRejected(HostCode.LOCAL_DOCKER_UNAVAILABLE)
    return RuntimeObservation(daemon, final, image, identity)


def confirm_stopped(enrollment: Enrollment):
    """Read-only snapshot guard, not a command to stop the application.

    Exact original container must be exited with PID zero; no active container
    may have a writable bind spanning its enrolled installation. Observe again
    after inspection; snapshot code calls this before and after its own complete
    byte/metadata readback. An external host writer/operator is not frozen by
    Docker: supported onboarding must exclude external writers, and concurrent
    filesystem changes fail the independent snapshot checks.
    """
    _trusted_tools()
    deadline = time.monotonic() + TOTAL_SECONDS

    def original():
        value = _single(_read_command(["inspect", "--type", "container", enrollment.container_id], deadline))
        state = value.get("State", {})
        if (value.get("Id") != enrollment.container_id or value.get("Image") != enrollment.image_id
                or state.get("Running") is not False or state.get("Restarting") is not False
                or state.get("Paused") is not False or state.get("Dead") is not False
                or type(state.get("Pid")) is not int or state["Pid"] != 0
                or state.get("Status") != "exited"):
            raise HostRejected(HostCode.CONTAINER_MISMATCH)
        return value

    before = original()
    identities = _read_command(WRITER_LIST, deadline)
    if enrollment.container_id in identities:
        raise HostRejected(HostCode.OBSERVATION_CHANGED)
    for identity in identities:
        value = _single(_read_command(["inspect", "--type", "container", identity], deadline))
        if value.get("Id") != identity or not isinstance(value.get("Mounts"), list):
            raise HostRejected(HostCode.OBSERVATION_CHANGED)
        for mount in value["Mounts"]:
            if not isinstance(mount, dict) or type(mount.get("RW")) is not bool:
                raise HostRejected(HostCode.MOUNTS_UNSUPPORTED)
            if mount["RW"]:
                path = mount.get("Source")
                if not isinstance(path, str) or not path.startswith("/"):
                    raise HostRejected(HostCode.MOUNTS_UNSUPPORTED)
                # A bind of the parent directory is also a writer, including '/'.
                root = enrollment.root
                if (path == "/" or path == root or path.startswith(root + "/")
                        or root.startswith(path.rstrip("/") + "/")):
                    raise HostRejected(HostCode.MOUNTS_UNSUPPORTED)
    after = original()
    if (before != after or set(_read_command(WRITER_LIST, deadline)) != set(identities)
            or time.monotonic() > deadline):
        raise HostRejected(HostCode.OBSERVATION_CHANGED)
    return True
