"""Explicit deployment of a sealed helper, never from a service's mutable checkout.

The operator must independently trust the reviewed source and approve the whole
bundle hash. A self-contained manifest detects corruption, not publisher identity.
Only initial installation is supported: partial/existing state is never replaced.
"""
import ast
import hashlib
import hmac
import io
import os
from pathlib import Path
import secrets
import stat
import zipfile

from .host_preflight import APP_UID, APP_GID, directory, unique_json, _regular_read, load_enrollment
from .onboarding import HOST_ROOT, PEER, _json, _write_new, _mkdir_new, verify_preparation
from .protocol import Authorization, CREDENTIAL, require_digest


SOCKET_PATH = "/run/basswiesn-update/control.sock"
UNIT_ROOT = "/etc/systemd/system"
SERVICE_NAME = "basswiesn-update.service"
SOCKET_NAME = "basswiesn-update.socket"
MAX_BUNDLE = 2 * 1024 * 1024
MODULES = ("__init__", "protocol", "journal", "transaction", "service", "host_preflight",
           "local_docker", "onboarding", "official_transport", "artifact_staging", "deployment", "daemon",
           "installation_snapshot", "candidate_runtime", "installation_cutover", "host_executor")
ENTRY = b'from basswiesn.update_helper.daemon import main\nif __name__ == "__main__":\n    raise SystemExit(main())\n'
GENERATED = {"__main__.py": ENTRY, "basswiesn/__init__.py": b"",
             "basswiesn/app/__init__.py": b"", "basswiesn/app/services/__init__.py": b""}
SOURCES = {f"basswiesn/update_helper/{name}.py" for name in MODULES} | {
    "basswiesn/app/services/release_artifact.py"}
EXPECTED = SOURCES | GENERATED.keys()
SERVICE = """[Unit]
Description=BASSWIESN restricted update helper
Requires=basswiesn-update.socket
After=basswiesn-update.socket

[Service]
Type=simple
ExecStart=/usr/bin/python3 -I -S -B /var/lib/basswiesn-update/service/helper.pyz
User=root
Group=root
UMask=0077
WorkingDirectory=/
NoNewPrivileges=yes
ProtectSystem=strict
ProtectHome=read-only
ReadWritePaths=/var/lib/basswiesn-update
PrivateTmp=yes
PrivateDevices=yes
ProtectKernelTunables=yes
ProtectKernelModules=yes
ProtectControlGroups=yes
RestrictSUIDSGID=yes
LockPersonality=yes
RestrictAddressFamilies=AF_UNIX AF_INET AF_INET6
MemoryMax=256M
TasksMax=32
LimitCORE=0
TimeoutStopSec=130
Restart=no
StandardOutput=null
StandardError=null
"""
SOCKET = """[Unit]
Description=BASSWIESN local update control socket

[Socket]
ListenStream=/run/basswiesn-update/control.sock
DirectoryMode=0755
SocketUser=root
SocketGroup=10001
SocketMode=0660
Accept=no
Backlog=8
FileDescriptorName=basswiesn-update
RemoveOnStop=yes
Service=basswiesn-update.service

[Install]
WantedBy=sockets.target
"""
UNITS = {SERVICE_NAME: SERVICE.encode(), SOCKET_NAME: SOCKET.encode()}
FILES = {"reservation.json", "helper.pyz", "authorization.json", "administrator.capability", "installed.json"}


def units_for(policy):
    """Grant writes only to the enrolled installation, not its parent directory.

    Escape systemd specifiers; no shell/environment expansion. Restoring directory
    setgid requires chmod support. File privilege bits and unsafe directory modes
    are independently rejected by the snapshot/cutover metadata validators.
    """
    from .host_preflight import absolute_path
    path = absolute_path(policy.root).replace("%", "%%")
    service = SERVICE.replace("ReadWritePaths=/var/lib/basswiesn-update\n",
        'ReadWritePaths=/var/lib/basswiesn-update "'+path+'"\n')
    service = service.replace("RestrictSUIDSGID=yes\n", "RestrictSUIDSGID=no\n")
    return {SERVICE_NAME: service.encode(), SOCKET_NAME: SOCKET.encode()}


class DeploymentRejected(ValueError):
    def __init__(self, code="DEPLOYMENT_INVALID"):
        self.code = code if code in {"DEPLOYMENT_INVALID", "DEPLOYMENT_EXISTS", "DEPLOYMENT_IO",
            "ADMIN_REQUIRED", "APPROVAL_REQUIRED", "BUNDLE_INVALID", "BUNDLE_CHANGED"} else "DEPLOYMENT_INVALID"
        super().__init__(self.code)


def sha(data):
    return hashlib.sha256(data).hexdigest()


def build_bundle(root):
    """Read reviewed sources as bytes; no imports/execution of candidate files."""
    values = dict(GENERATED)
    for name in sorted(SOURCES):
        fd = os.open(Path(root) / name, os.O_RDONLY | os.O_NOFOLLOW | os.O_NONBLOCK)
        with os.fdopen(fd, "rb") as source:
            before = os.fstat(source.fileno())
            if not stat.S_ISREG(before.st_mode) or not 0 < before.st_size <= MAX_BUNDLE // 2:
                raise DeploymentRejected("BUNDLE_INVALID")
            data = source.read(MAX_BUNDLE // 2 + 1)
            after = os.fstat(source.fileno())
            if len(data) != before.st_size or (before.st_ino, before.st_size, before.st_mtime_ns, before.st_ctime_ns) != (after.st_ino, after.st_size, after.st_mtime_ns, after.st_ctime_ns):
                raise DeploymentRejected("BUNDLE_CHANGED")
            ast.parse(data, filename=name)
            values[name] = data
    values["manifest.json"] = _json({"schema": 1, "files": {name: sha(data) for name, data in sorted(values.items())}})
    output = io.BytesIO()
    with zipfile.ZipFile(output, "w", compression=zipfile.ZIP_STORED) as bundle:
        for name, data in sorted(values.items()):
            member = zipfile.ZipInfo(name, date_time=(1980, 1, 1, 0, 0, 0))
            member.create_system = 3
            member.external_attr = (stat.S_IFREG | 0o600) << 16
            bundle.writestr(member, data)
    result = output.getvalue()
    verify_bundle(result)
    return result


def verify_bundle(data):
    if type(data) is not bytes or not 0 < len(data) <= MAX_BUNDLE:
        raise DeploymentRejected("BUNDLE_INVALID")
    try:
        with zipfile.ZipFile(io.BytesIO(data)) as bundle:
            entries = bundle.infolist()
            if (len(entries) != len(EXPECTED) + 1 or set(i.filename for i in entries) != EXPECTED | {"manifest.json"}
                    or bundle.comment or any(i.compress_type != zipfile.ZIP_STORED or i.flag_bits
                    or i.extra or i.comment or i.file_size > MAX_BUNDLE // 2 or i.file_size < 0
                    or i.compress_size != i.file_size or i.external_attr >> 16 != stat.S_IFREG | 0o600
                    for i in entries) or sum(i.file_size for i in entries) > MAX_BUNDLE):
                raise ValueError()
            values = {name: bundle.read(name) for name in EXPECTED}
            manifest = unique_json(bundle.read("manifest.json"))
            if _json(manifest) != _json({"schema": 1, "files": {name: sha(value) for name, value in values.items()}}):
                raise ValueError()
            if any(values[name] != expected for name, expected in GENERATED.items()):
                raise ValueError()
            for name, value in values.items():
                ast.parse(value, filename=name)
    except Exception:
        raise DeploymentRejected("BUNDLE_INVALID") from None
    return sha(data)


def _absent(parent, names):
    for name in names:
        try:
            os.stat(name, dir_fd=parent, follow_symlinks=False)
        except FileNotFoundError:
            continue
        raise DeploymentRejected("DEPLOYMENT_EXISTS")


def provision(data, *, approve_sha256, approve=False, root=HOST_ROOT, unit_root=UNIT_ROOT, owner_uid=0):
    """Admin entry; alternate paths/UID only for isolated tests. Never start units.

    The generated capability is retained ONLY in an owner-private credential file,
    never returned, logged, passed in argv or copied to the Web container.
    """
    if type(owner_uid) is not int or os.geteuid() != owner_uid:
        raise DeploymentRejected("ADMIN_REQUIRED")
    require_digest(approve_sha256)
    if approve is not True or not hmac.compare_digest(sha(data), approve_sha256):
        raise DeploymentRejected("APPROVAL_REQUIRED")
    verify_bundle(data)
    try:
        # This check must precede reservation; an already used enrollment cannot
        # be silently reprovisioned with new credentials or an empty journal.
        verify_preparation(root, owner_uid=owner_uid)
        with directory(root, owners={0, owner_uid}, final_owners={owner_uid}, private=True) as host:
            with directory(unit_root, owners={0, owner_uid}, final_owners={owner_uid}) as units:
                _absent(host, {"service"})
                _absent(units, UNITS)
                policy = load_enrollment(root + "/enrollment.json", policy_owner_uid=owner_uid)
                unit_files = units_for(policy)
                _mkdir_new(host, "service", owner_uid)
                with directory(root + "/service", owners={0, owner_uid}, final_owners={owner_uid}, private=True) as service:
                    _write_new(service, "reservation.json", _json({"schema": 1, "purpose": "helper-service", "installation_id": policy.installation_id}), owner_uid)
                    _write_new(service, "helper.pyz", data, owner_uid)
                    token = secrets.token_urlsafe(32)
                    if not CREDENTIAL.fullmatch(token):
                        raise DeploymentRejected()
                    credential_hash = sha(token.encode("ascii"))
                    _write_new(service, "administrator.capability", token.encode("ascii") + b"\n", owner_uid)
                    del token
                    _write_new(service, "authorization.json", _json({"schema": 1, "allowed_peer_uid": APP_UID,
                        "credential_sha256": credential_hash}), owner_uid)
                    # Exclusive root-private regular files are readable by systemd.
                    for name, content in unit_files.items():
                        _write_new(units, name, content, owner_uid)
                    _write_new(service, "installed.json", _json({"schema": 1,
                        "installation_id": policy.installation_id, "bundle_sha256": sha(data),
                        "units": {name: sha(value) for name, value in unit_files.items()}}), owner_uid)
        verify_deployment(root=root, unit_root=unit_root, owner_uid=owner_uid)
        return {"state": "SERVICE_FILES_INSTALLED", "service_enabled": False,
                "capability_provisioned": True, "capability_delivered_to_web": False,
                "installation_available": False, "backup_restore_verified": False}
    except DeploymentRejected:
        raise
    except OSError:
        raise DeploymentRejected("DEPLOYMENT_IO") from None
    except Exception:
        raise DeploymentRejected() from None


def verify_deployment(*, root=HOST_ROOT, unit_root=UNIT_ROOT, owner_uid=0):
    """Return only a digest-based authorization object, not a plaintext capability."""
    try:
        policy = load_enrollment(root + "/enrollment.json", policy_owner_uid=owner_uid)
        unit_files = units_for(policy)
        with directory(root, owners={0, owner_uid}, final_owners={owner_uid}, private=True) as host:
            if _json(unique_json(_regular_read(host, "peer.json", owners={owner_uid}, limit=4096, private=True))) != _json(PEER):
                raise DeploymentRejected()
        with directory(root + "/service", owners={0, owner_uid}, final_owners={owner_uid}, private=True) as service:
            if set(os.listdir(service)) != FILES:
                raise DeploymentRejected()
            def read(name, limit=4096):
                return _regular_read(service, name, owners={owner_uid}, limit=limit, private=True)
            reservation = unique_json(read("reservation.json"))
            if _json(reservation) != _json({"schema": 1, "purpose": "helper-service", "installation_id": policy.installation_id}):
                raise DeploymentRejected()
            receipt = unique_json(read("installed.json"))
            expected = {"schema": 1, "installation_id": policy.installation_id,
                "bundle_sha256": verify_bundle(read("helper.pyz", MAX_BUNDLE)),
                "units": {name: sha(value) for name, value in unit_files.items()}}
            if _json(receipt) != _json(expected):
                raise DeploymentRejected()
            auth = unique_json(read("authorization.json"))
            if (set(auth) != {"schema", "allowed_peer_uid", "credential_sha256"}
                    or type(auth["schema"]) is not int or auth["schema"] != 1
                    or type(auth["allowed_peer_uid"]) is not int or auth["allowed_peer_uid"] != APP_UID):
                raise DeploymentRejected()
            authorization = Authorization(auth["allowed_peer_uid"], auth["credential_sha256"])
            token = read("administrator.capability", 44)
            if len(token) != 44 or not token.endswith(b"\n") or not CREDENTIAL.fullmatch(token[:-1].decode("ascii")):
                raise DeploymentRejected()
            if not hmac.compare_digest(sha(token[:-1]), authorization.credential_sha256):
                raise DeploymentRejected()
        with directory(unit_root, owners={0, owner_uid}, final_owners={owner_uid}) as units:
            for name, expected in unit_files.items():
                if _regular_read(units, name, owners={owner_uid}, limit=4096, private=True) != expected:
                    raise DeploymentRejected()
        return authorization
    except DeploymentRejected:
        raise
    except Exception:
        raise DeploymentRejected() from None
