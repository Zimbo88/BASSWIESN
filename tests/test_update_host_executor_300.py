"""Real journal/staging/snapshot/rename/policy, synthetic Docker command boundary.

Not a production acceptance test. Fake Docker migrates an actual SQLite fixture
at candidate startup. No daemon, network, radios or process launch is used.
"""
from copy import deepcopy
from dataclasses import replace
import hashlib
import json
import os
from pathlib import Path
import sqlite3

import pytest

from basswiesn.update_helper import host_executor as executor, host_preflight as host
from basswiesn.update_helper import candidate_runtime as runtime, installation_cutover as cutover
from basswiesn.update_helper.journal import Journal
from basswiesn.update_helper.onboarding import _policy_value
from basswiesn.update_helper.protocol import Request
from basswiesn.update_helper.transaction import TransactionRunner
from test_update_host_preflight_300 import enrolled, observed  # noqa: F401
from test_update_installation_snapshot_300 import context  # noqa: F401
from test_update_artifact_staging_300 import OfficialFixture
from test_release_artifact_300 import VERSION, tar_bytes, content


pytestmark = pytest.mark.unit
JOB = "11223344-1234-4234-8234-123456789abc"
NEW_IMAGE = "sha256:" + "b" * 64


class DockerFixture:
    def __init__(self, policy, failure=None):
        self.policy, self.failure = policy, failure
        self.containers = {policy.container_id: deepcopy(observed(policy).container)}
        self.containers[policy.container_id]["Name"] = "/example-basswiesn-1"
        self.calls = []
        self.new_id = None
        self.old_removed = False
        self.serial = 1

    def observe(self, policy):
        value = observed(policy)
        return replace(value, container=deepcopy(self.containers[policy.container_id]))

    def stopped(self, policy):
        value = self.containers[policy.container_id]
        assert value["Image"] == policy.image_id
        return not any(c["State"]["Running"] for c in self.containers.values())

    def __call__(self, args, *, seconds):
        self.calls.append(args)
        assert 0 < seconds <= 900
        action = args[0]
        if action == "build":
            return (NEW_IMAGE+"\n").encode()
        if action == "inspect":
            if args[2] == "image":
                policy = replace(self.policy, image_id=args[3], version=VERSION if args[3] == NEW_IMAGE else self.policy.version)
                return json.dumps([observed(policy).image]).encode()
            item = self.containers.get(args[3]) or next((v for v in self.containers.values() if v["Name"] == "/"+args[3]),None)
            if item is None:
                raise runtime.CandidateRejected("COMMAND_FAILED")
            return json.dumps([item]).encode()
        if action == "create":
            role = "restored" if args[args.index("--name")+1].startswith("basswiesn-update-restored-") else "live"
            if self.failure == "create-before" and role == "live":
                raise runtime.CandidateRejected("COMMAND_FAILED")
            identity = f"{self.serial:064x}"
            self.serial += 1
            image = args[-1]
            policy = replace(self.policy, container_id=identity, image_id=image,
                             version=VERSION if role == "live" else self.policy.version)
            value = deepcopy(observed(policy).container)
            value["Name"] = "/"+args[args.index("--name")+1]
            value["Config"]["Labels"].update({executor.LABEL:JOB,executor.ROLE:role})
            value["Config"]["Env"] = Path(args[args.index("--env-file")+1]).read_text().splitlines()
            value["State"].update(Running=False,Pid=0,Status="created")
            for index, argument in enumerate(args):
                if argument == "--mount" and ",dst=/run/basswiesn-update," in args[index+1]:
                    mount = args[index+1]
                    assert mount.endswith(",readonly")
                    source = mount.split(",src=",1)[1].split(",dst=",1)[0]
                    value["Mounts"].append({"Type":"bind","Source":source,"Destination":"/run/basswiesn-update",
                                            "RW":False,"Propagation":"rprivate"})
            self.containers[identity] = value
            if role == "live":
                self.new_id = identity
            if self.failure == "create-lost-reply" and role == "live":
                raise runtime.CandidateRejected("COMMAND_FAILED")
            return (identity+"\n").encode()
        if action == "stop":
            self.containers[args[-1]]["State"].update(Running=False,Pid=0,Status="exited")
            return b""
        if action == "start":
            value = self.containers[args[1]]
            value["State"].update(Running=True,Pid=12345,Status="running")
            if value["Image"] == NEW_IMAGE:
                with sqlite3.connect(Path(self.policy.root) / "data/app.db") as db:
                    db.execute("ALTER TABLE fixture ADD COLUMN newer TEXT")
                    db.execute("UPDATE fixture SET value='new version',newer='new schema'")
                if self.failure == "start-after":
                    raise runtime.CandidateRejected("COMMAND_FAILED")
            return b""
        if action == "exec":
            image = self.containers[args[1]]["Image"]
            version = VERSION if image == NEW_IMAGE else self.policy.version
            if image == NEW_IMAGE and (self.failure == "health" or self.failure == "after-retire" and self.old_removed):
                version = "0.0.0"
            return json.dumps({"ready":True,"version":version}).encode()
        if action == "rm":
            assert self.containers[args[1]]["State"]["Running"] is False
            del self.containers[args[1]]
            if args[1] == self.policy.container_id:
                self.old_removed = True
                if self.failure == "remove-lost-reply":
                    raise runtime.CandidateRejected("COMMAND_FAILED")
            return b""
        if action == "ps":
            assert args == ["ps","--all","--quiet","--no-trunc","--filter","id="+self.policy.container_id]
            return (self.policy.container_id+"\n").encode() if self.policy.container_id in self.containers else b""
        pytest.fail("Unexpected Docker operation")


@pytest.fixture
def setup(context, tmp_path, monkeypatch):
    policy = context[0]
    ignore = (cutover.BUILD_EXCLUSION+"\n").encode()
    (Path(policy.root) / ".dockerignore").write_bytes(ignore)
    policy = replace(policy,file_sha256={**policy.file_sha256,".dockerignore":hashlib.sha256(ignore).hexdigest()})
    root = tmp_path / "executor-host"
    root.mkdir(mode=0o700)
    for name in ("candidates","staging","backups","restores","journal"):
        (root / name).mkdir(mode=0o700)
    (root / "enrollment.json").write_text(json.dumps(_policy_value(policy)))
    (root / "enrollment.json").chmod(0o600)
    source = OfficialFixture(tar_bytes(content(extra={".dockerignore":ignore})))
    journal = Journal(root / "journal", owner_uid=os.getuid())
    journal.initialize()
    # Network-isolated candidate build/DB probe has its own real Docker
    # acceptance; here the focus is host orchestration around that component.
    monkeypatch.setattr(runtime.CandidateRuntime,"validate",lambda *a,**k:{"state":"CANDIDATE_VALIDATED"})
    yield policy,root,source,journal
    journal.close()


@pytest.mark.parametrize("failure", [None,"create-before","create-lost-reply","start-after","health","after-retire","remove-lost-reply"])
def test_real_host_transaction_and_matching_schema_rollback(setup, failure):
    policy,root,fetch,journal = setup
    docker = DockerFixture(policy,failure)
    adapter = executor.ProductionExecutor(root=str(root),owner_uid=os.getuid(),run=docker,
        observe=docker.observe,stopped=docker.stopped,fetch=fetch,health_seconds=1)
    request = Request("install",JOB,VERSION,policy.version)
    outcome = TransactionRunner(journal,adapter).execute(request)
    expected = "COMPLETE" if failure in (None,"create-lost-reply") else "RESTORED"
    assert outcome["state"] == expected
    current = host.load_enrollment(str(root / "enrollment.json"),policy_owner_uid=os.getuid())
    host.inspect_files(current)
    host.inspect_runtime(current,docker.observe(current))
    assert len(docker.containers) == 1
    assert next(iter(docker.containers.values()))["State"]["Running"]
    with sqlite3.connect(Path(policy.root) / "data/app.db") as db:
        assert db.execute("PRAGMA quick_check").fetchall() == [("ok",)]
        if expected == "COMPLETE":
            assert current.version == VERSION and current.image_id == NEW_IMAGE
            assert db.execute("SELECT * FROM fixture").fetchall() == [(1,"new version","new schema")]
        else:
            assert current.version == policy.version and current.image_id == policy.image_id
            assert db.execute("SELECT * FROM fixture").fetchall() == [(1,"old schema")]
    assert "DO_NOT_REPORT_THIS_VALUE" not in json.dumps(outcome)+json.dumps(journal.read())
    calls = list(docker.calls)
    assert TransactionRunner(journal,adapter).execute(request) == outcome
    assert docker.calls == calls  # reconnecting Web client cannot replay work


def test_live_environment_preserves_https_but_cannot_enable_validation_mode():
    body = runtime.environment_bytes(["BASSWIESN_ENABLE_HTTPS=1", "BASSWIESN_UPDATE_VALIDATION_MODE=1",
        "BASSWIESN_LAN_HOST=192.0.2.42","PYTHONPATH=/untrusted","LD_PRELOAD=/untrusted"],validation=False)
    assert b"BASSWIESN_ENABLE_HTTPS=1\n" in body
    assert b"BASSWIESN_UPDATE_VALIDATION_MODE=0\n" in body
    assert b"PYTHONPATH" not in body and b"LD_PRELOAD" not in body


@pytest.mark.parametrize("failure", [None, "after-retire"])
def test_update_and_late_rollback_preserve_narrow_control_directory(setup, failure):
    policy,root,fetch,journal = setup
    docker = DockerFixture(policy,failure)
    expected={"Type":"bind","Source":"/run/basswiesn-update","Destination":"/run/basswiesn-update",
              "RW":False,"Propagation":"rprivate"}
    docker.containers[policy.container_id]["Mounts"].append(expected)
    adapter = executor.ProductionExecutor(root=str(root),owner_uid=os.getuid(),run=docker,
        observe=docker.observe,stopped=docker.stopped,fetch=fetch,health_seconds=1)
    outcome=TransactionRunner(journal,adapter).execute(Request("install",JOB,VERSION,policy.version))
    assert outcome["state"] == ("COMPLETE" if failure is None else "RESTORED")
    container=next(iter(docker.containers.values()))
    assert len(container["Mounts"])==3 and expected in container["Mounts"]


@pytest.mark.parametrize("bind", ["127.0.0.1", "::1", "0.0.0.0", "::", ""])
def test_loopback_bindings_never_widen_during_replacement(setup, bind):
    policy,root,fetch,journal = setup
    docker = DockerFixture(policy)
    for bindings in docker.containers[policy.container_id]["HostConfig"]["PortBindings"].values():
        bindings[0]["HostIp"] = bind
    adapter = executor.ProductionExecutor(root=str(root),owner_uid=os.getuid(),run=docker,
        observe=docker.observe,stopped=docker.stopped,fetch=fetch,health_seconds=1)
    result = TransactionRunner(journal,adapter).execute(Request("install",JOB,VERSION,policy.version))
    assert result["state"] == "COMPLETE"
    call = next(c for c in docker.calls if c[0] == "create")
    flags = [call[i+1] for i,x in enumerate(call) if x == "--publish"]
    prefix = "["+bind+"]:" if ":" in bind else bind+":" if bind else ""
    assert flags == [prefix+p.split("/")[0]+":"+p for p in sorted(host.PORTS)]
