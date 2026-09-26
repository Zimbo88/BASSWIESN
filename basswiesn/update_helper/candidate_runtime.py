"""Build a verified release and validate it in a network-isolated container.

No production stop/start, live data mount, public port, Compose evaluation or
arbitrary request command. Build runs ONLY administrator-approved official source
in Docker; dependency acquisition is not an offline operation. Runtime validation
is network-none and uses a separately verified snapshot copy, never live data.
This is the candidate half of the installer, not an operational cutover executor.
"""
import os
import re
import selectors
import subprocess
import tempfile
import time

from . import artifact_staging, installation_snapshot, local_docker
from .host_preflight import directory, _regular_read, unique_json
from .onboarding import _json, _mkdir_new, _write_new
from .protocol import require_digest, require_request_id, require_version


CANDIDATE_ROOT = "/var/lib/basswiesn-update/candidates"
RESTORE_ROOT = "/var/lib/basswiesn-update/restores"
LABEL = "io.basswiesn.update.request"
ROLE = "io.basswiesn.update.role"
MAX_OUTPUT = 2 * 1024 * 1024
MAX_ENV = 128 * 1024

# Constant code, executed as the image's unprivileged user INSIDE the isolated
# container. It never prints private data, schema SQL, exceptions or headers.
PROBE = r'''import sys,json,hashlib,urllib.request
sys.path.insert(0,"/app")
try:
 from basswiesn.app.config import get_settings
 from basswiesn.app.db import engine, Base
 import basswiesn.app.models
 from sqlalchemy import text
 s=get_settings()
 assert s.update_validation_mode is True
 assert engine.dialect.name=="sqlite"
 op=urllib.request.build_opener(urllib.request.ProxyHandler({}))
 for port,path in ((1328,"/api/readiness"),(1516,"/bmx/registry/v1/services"),(1860,"/health")):
  with op.open("http://127.0.0.1:"+str(port)+path,timeout=2) as response:
   assert response.status==200
   body=json.loads(response.read(131073))
   assert isinstance(body,dict)
   if port==1328:
    assert body.get("ready") is True and body.get("version")==s.version
    assert body.get("update_validation_mode") is True
    assert body.get("checks",{}).get("background_tasks")==[]
   elif port==1516: assert isinstance(body.get("bmx_services"),list)
   else: assert body.get("status")=="ok"
 with engine.connect() as c:
  assert c.execute(text("PRAGMA quick_check")).fetchall()==[("ok",)]
  rows=[list(r) for r in c.execute(text("SELECT type,name,tbl_name,sql FROM sqlite_master ORDER BY type,name"))]
  tables={r[1] for r in rows if r[0]=="table"}
  assert set(Base.metadata.tables)<=tables
 digest=hashlib.sha256(json.dumps(rows,sort_keys=True,separators=(",",":")).encode()).hexdigest()
 print(json.dumps({"ready":True,"version":s.version,"validation_mode":True,"sqlite_integrity":True,"schema_sha256":digest,"table_count":len(tables)}))
except Exception:
 sys.exit(2)
'''


class CandidateRejected(ValueError):
    def __init__(self, code):
        self.code = code if code in {"CANDIDATE_EXISTS", "CANDIDATE_INVALID", "CANDIDATE_CHANGED",
            "BUILD_FAILED", "COMMAND_TIMEOUT", "COMMAND_FAILED", "OUTPUT_LIMIT", "IMAGE_INVALID",
            "ENVIRONMENT_INVALID", "CONTAINER_INVALID", "CANDIDATE_UNHEALTHY", "CLEANUP_FAILED"} else "CANDIDATE_INVALID"
        super().__init__(self.code)


def _command(arguments, *, seconds):
    """Internal caller-generated argv only; no shell, inherited context or logs."""
    local_docker._trusted_tools()
    if (type(seconds) not in (int, float) or not 0 < seconds <= 900
            or not isinstance(arguments, list) or not arguments
            or arguments[0] not in {"build", "inspect", "create", "start", "stop", "exec", "rm", "ps"}
            or any(not isinstance(v, str) or "\x00" in v for v in arguments)):
        raise CandidateRejected("CANDIDATE_INVALID")
    process = None
    client_directory = None
    deadline = time.monotonic() + seconds
    try:
        # Recent Docker/buildx creates a token seed and builder state even with
        # an explicitly empty --config. Give EACH invocation its own private
        # directory; never reuse those credentials/contexts or pollute enrollment.
        client_directory = tempfile.TemporaryDirectory(prefix="command-", dir=local_docker.CLIENT_CONFIG)
        process = subprocess.Popen([local_docker.DOCKER, "--host", "unix://" + local_docker.SOCKET,
            "--config", client_directory.name, *arguments], stdin=subprocess.DEVNULL,
            stdout=subprocess.PIPE, stderr=subprocess.DEVNULL, close_fds=True, cwd="/",
            env={"PATH": "/usr/bin:/bin", "LC_ALL": "C"})
        output = bytearray()
        with selectors.DefaultSelector() as poll:
            poll.register(process.stdout, selectors.EVENT_READ)
            while True:
                remaining = deadline - time.monotonic()
                if remaining <= 0 or not poll.select(remaining):
                    raise CandidateRejected("COMMAND_TIMEOUT")
                chunk = os.read(process.stdout.fileno(), min(65536, MAX_OUTPUT + 1 - len(output)))
                if not chunk:
                    break
                output.extend(chunk)
                if len(output) > MAX_OUTPUT:
                    raise CandidateRejected("OUTPUT_LIMIT")
        process.wait(timeout=max(0.001, deadline - time.monotonic()))
        if process.returncode:
            raise CandidateRejected("COMMAND_FAILED")
        return bytes(output)
    except (OSError, subprocess.SubprocessError):
        raise CandidateRejected("COMMAND_FAILED") from None
    finally:
        if process is not None:
            if process.poll() is None:
                process.kill()
            process.wait()
            if process.stdout is not None:
                process.stdout.close()
        if client_directory is not None:
            client_directory.cleanup()  # only this invocation's newly reserved directory


def _image_id(value):
    if not isinstance(value, str) or not value.startswith("sha256:"):
        raise CandidateRejected("IMAGE_INVALID")
    require_digest(value[7:])
    return value


def _inspect(run, kind, identity):
    if kind == "image":
        _image_id(identity)
    elif kind == "container":
        require_digest(identity)
    else:
        raise CandidateRejected("CANDIDATE_INVALID")
    value = unique_json(run(["inspect", "--type", kind, identity], seconds=10))
    if not isinstance(value, list) or len(value) != 1 or not isinstance(value[0], dict):
        raise CandidateRejected("CANDIDATE_INVALID")
    return value[0]


def _image(run, identity, version):
    value = _inspect(run, "image", identity)
    config = value.get("Config", {})
    if (value.get("Id") != identity or value.get("Os") != "linux"
            or value.get("Architecture") not in {"amd64", "arm64", "arm"}
            or config.get("User") not in {"basswiesn", "10001", "10001:10001"}
            or config.get("WorkingDir") != "/app" or config.get("Entrypoint") not in (None, [])
            or config.get("Cmd") != ["python", "tools/run_dev.py"]
            or config.get("Labels", {}).get("org.opencontainers.image.version") != version
            or config.get("Labels", {}).get("org.opencontainers.image.source") != artifact_staging.REPOSITORY):
        raise CandidateRejected("IMAGE_INVALID")
    return value


def environment_bytes(values, *, validation=True):
    """Preserve resolved app configuration, not Compose quoting or host defaults."""
    if not isinstance(values, list) or len(values) > 512:
        raise CandidateRejected("ENVIRONMENT_INVALID")
    result = {}
    for item in values:
        if not isinstance(item, str) or len(item) > 16384 or any(c in item for c in "\r\n\x00"):
            raise CandidateRejected("ENVIRONMENT_INVALID")
        key, separator, value = item.partition("=")
        if not separator or not re.fullmatch(r"[A-Za-z_][A-Za-z0-9_]{0,127}", key) or key in result:
            raise CandidateRejected("ENVIRONMENT_INVALID")
        result[key] = value
    # Never inherit executable search paths or Python/library injection settings.
    allowed = {k: v for k, v in result.items() if k.startswith("BASSWIESN_") or
               k in {"PROTECTED_DEVICE_IDS", "PROTECTED_DEVICE_IPS", "LANG", "LC_ALL", "LC_MESSAGES"}}
    if type(validation) is not bool:
        raise CandidateRejected("ENVIRONMENT_INVALID")
    allowed["BASSWIESN_UPDATE_VALIDATION_MODE"] = "1" if validation else "0"
    if validation:
        allowed["BASSWIESN_ENABLE_HTTPS"] = "0"
    data = "".join(k + "=" + v + "\n" for k, v in sorted(allowed.items())).encode("utf-8")
    if len(data) > MAX_ENV:
        raise CandidateRejected("ENVIRONMENT_INVALID")
    return data


class CandidateRuntime:
    """Trusted host adapter component; overrides are for isolated tests only."""
    def __init__(self, *, root=CANDIDATE_ROOT, staging_root=artifact_staging.STAGING_ROOT,
                 restore_root=RESTORE_ROOT, owner_uid=0, run=_command):
        self.root, self.staging_root, self.restore_root = root, staging_root, restore_root
        self.owner_uid, self.run = owner_uid, run

    def build(self, request_id, version):
        require_request_id(request_id)
        require_version(version)
        staged = artifact_staging.verify_staged_release(request_id, version,
            staging_root=self.staging_root, owner_uid=self.owner_uid)
        with directory(self.root, owners={0, self.owner_uid}, final_owners={self.owner_uid}, private=True) as base:
            try:
                _mkdir_new(base, request_id, self.owner_uid)
            except FileExistsError:
                raise CandidateRejected("CANDIDATE_EXISTS") from None
        job_path = self.root + "/" + request_id
        source = self.staging_root + "/" + request_id + "/source"
        with directory(job_path, owners={0, self.owner_uid}, final_owners={self.owner_uid}, private=True) as job:
            _write_new(job, "reservation.json", _json({"schema": 1, "request_id": request_id, "version": version,
                       "archive_sha256": staged["archive_sha256"]}), self.owner_uid)
            raw = self.run(["build", "--quiet", "--pull=false", "--network=default",
                            "--tag", "basswiesn-update-" + request_id + ":candidate", source], seconds=900)
            try:
                identity = _image_id(raw.decode("ascii").strip())
            except (UnicodeError, ValueError):
                raise CandidateRejected("BUILD_FAILED") from None
            _image(self.run, identity, version)
            after = artifact_staging.verify_staged_release(request_id, version,
                staging_root=self.staging_root, owner_uid=self.owner_uid)
            if after != staged:
                raise CandidateRejected("CANDIDATE_CHANGED")
            result = {"schema": 1, "request_id": request_id, "version": version,
                      "image_id": identity, "archive_sha256": staged["archive_sha256"]}
            _write_new(job, "image.json", _json(result), self.owner_uid)
            return result

    def _built(self, request_id, version):
        require_request_id(request_id)
        require_version(version)
        with directory(self.root + "/" + request_id, owners={0, self.owner_uid},
                       final_owners={self.owner_uid}, private=True) as job:
            value = unique_json(_regular_read(job, "image.json", owners={self.owner_uid}, limit=4096, private=True))
            reserved = unique_json(_regular_read(job, "reservation.json", owners={self.owner_uid}, limit=4096, private=True))
        if (not isinstance(value, dict) or set(value) != {"schema", "request_id", "version", "image_id", "archive_sha256"}
                or type(value["schema"]) is not int or value["schema"] != 1
                or value["request_id"] != request_id or value["version"] != version
                or _json(reserved) != _json({k: v for k, v in value.items() if k != "image_id"})):
            raise CandidateRejected("CANDIDATE_INVALID")
        require_digest(value["archive_sha256"])
        _image(self.run, value["image_id"], version)
        return value

    def _owned(self, identity, request_id, image_id):
        value = _inspect(self.run, "container", identity)
        labels = value.get("Config", {}).get("Labels", {})
        if (value.get("Id") != identity or value.get("Image") != image_id
                or labels.get(LABEL) != request_id or labels.get(ROLE) != "validation"):
            raise CandidateRejected("CONTAINER_INVALID")
        return value

    def _stop_remove(self, identity, request_id, image_id):
        value = self._owned(identity, request_id, image_id)
        if value.get("State", {}).get("Running") is True:
            self.run(["stop", "--time", "10", identity], seconds=15)
        value = self._owned(identity, request_id, image_id)
        if value.get("State", {}).get("Running") is not False or value.get("State", {}).get("Pid") != 0:
            raise CandidateRejected("CLEANUP_FAILED")
        self.run(["rm", identity], seconds=10)  # exact owned validation container; never --force/--volumes

    def _create(self, arguments, request_id, image_id):
        """An interrupted CLI reply is not proof that Docker created nothing."""
        try:
            identity = self.run(arguments, seconds=15).decode("ascii").strip()
            require_digest(identity)
            return identity
        except (ValueError, OSError) as original:
            # Query only the fixed name reserved by this operation. Never scan
            # containers or infer ownership from a name without label/image proof.
            name = "basswiesn-update-check-" + request_id
            try:
                found = unique_json(self.run(["inspect", "--type", "container", name], seconds=10))
                if (not isinstance(found, list) or len(found) != 1 or not isinstance(found[0], dict)
                        or found[0].get("Name") != "/" + name):
                    raise CandidateRejected("CONTAINER_INVALID")
                identity = found[0].get("Id")
                require_digest(identity)
                self._stop_remove(identity, request_id, image_id)
            except (ValueError, OSError):
                # Even a failed inspect is ambiguous (missing object OR daemon
                # unavailable). No success/automatic retry; operator must inspect.
                raise CandidateRejected("CLEANUP_FAILED") from None
            raise CandidateRejected(getattr(original, "code", "CONTAINER_INVALID")) from None

    def validate(self, policy, snapshot, version, *, assert_stopped, environment,
                 backup_root=installation_snapshot.BACKUP_ROOT, seconds=90):
        if type(seconds) not in (int, float) or not 1 <= seconds <= 180:
            raise CandidateRejected("CANDIDATE_INVALID")
        request_id = snapshot.request_id
        built = self._built(request_id, version)
        env = environment_bytes(environment)  # not argv, log output or Web response
        installation_snapshot.materialize(snapshot, policy, assert_stopped=assert_stopped,
            backup_root=backup_root, restore_root=self.restore_root, owner_uid=self.owner_uid)
        data_root = self.restore_root + "/" + request_id
        with directory(self.root + "/" + request_id, owners={0, self.owner_uid},
                       final_owners={self.owner_uid}, private=True) as job:
            _write_new(job, "candidate.env", env, self.owner_uid)
            env_path = self.root + "/" + request_id + "/candidate.env"
            args = ["create", "--name", "basswiesn-update-check-" + request_id,
                "--label", LABEL + "=" + request_id, "--label", ROLE + "=validation",
                "--network", "none", "--read-only", "--cap-drop", "ALL", "--user", "10001:10001",
                "--security-opt", "no-new-privileges:true", "--pids-limit", "128", "--memory", "512m",
                "--cpus", "1", "--restart", "no", "--init", "--no-healthcheck",
                "--tmpfs", "/tmp:rw,noexec,nosuid,size=64m", "--env-file", env_path,
                "--mount", "type=bind,src=" + data_root + "/data,dst=/app/data",
                "--mount", "type=bind,src=" + data_root + "/data/secrets/setup-rebuild,dst=/app/secrets/setup-rebuild,readonly",
                built["image_id"]]
            identity = None
            try:
                identity = self._create(args, request_id, built["image_id"])
                value = self._owned(identity, request_id, built["image_id"])
                host = value.get("HostConfig", {})
                if (host.get("NetworkMode") != "none" or host.get("PortBindings")
                        or host.get("ReadonlyRootfs") is not True or host.get("Privileged") is not False
                        or value.get("Config", {}).get("User") != "10001:10001"):
                    raise CandidateRejected("CONTAINER_INVALID")
                _write_new(job, "container.json", _json({"schema": 1, "id": identity, "image_id": built["image_id"]}), self.owner_uid)
                self.run(["start", identity], seconds=15)
                deadline = time.monotonic() + seconds
                initial = self._owned(identity, request_id, built["image_id"])
                probe = None
                while time.monotonic() < deadline:
                    current = self._owned(identity, request_id, built["image_id"])
                    state = current.get("State", {})
                    if (state.get("Running") is not True or state.get("OOMKilled") is not False
                            or current.get("RestartCount") != initial.get("RestartCount")
                            or state.get("StartedAt") != initial.get("State", {}).get("StartedAt")):
                        raise CandidateRejected("CANDIDATE_UNHEALTHY")
                    try:
                        raw = self.run(["exec", identity, "python", "-I", "-B", "-c", PROBE], seconds=min(10, deadline-time.monotonic()))
                        probe = unique_json(raw)
                    except CandidateRejected as error:
                        if error.code not in {"COMMAND_FAILED", "COMMAND_TIMEOUT"}:
                            raise
                        time.sleep(min(0.5, max(0, deadline-time.monotonic())))
                        continue
                    if (not isinstance(probe, dict) or set(probe) != {"ready", "version", "validation_mode",
                            "sqlite_integrity", "schema_sha256", "table_count"} or probe["ready"] is not True
                            or probe["version"] != version or probe["validation_mode"] is not True
                            or probe["sqlite_integrity"] is not True or type(probe["table_count"]) is not int
                            or not 1 <= probe["table_count"] <= 10000):
                        raise CandidateRejected("CANDIDATE_UNHEALTHY")
                    require_digest(probe["schema_sha256"])
                    final = self._owned(identity, request_id, built["image_id"])
                    if (final.get("State", {}).get("Running") is not True
                            or final.get("State", {}).get("OOMKilled") is not False
                            or final.get("State", {}).get("Pid") != state.get("Pid")
                            or final.get("RestartCount") != current.get("RestartCount")
                            or final.get("State", {}).get("StartedAt") != state.get("StartedAt")):
                        raise CandidateRejected("CANDIDATE_UNHEALTHY")
                    break
                else:
                    raise CandidateRejected("CANDIDATE_UNHEALTHY")
            finally:
                if identity is not None:
                    self._stop_remove(identity, request_id, built["image_id"])
            result = {"state": "CANDIDATE_VALIDATED", "request_id": request_id, "version": version,
                      "image_id": built["image_id"], "snapshot_sha256": snapshot.manifest_sha256,
                      "schema_sha256": probe["schema_sha256"], "table_count": probe["table_count"],
                      "production_changed": False}
            _write_new(job, "validated.json", _json(result), self.owner_uid)
            return result
