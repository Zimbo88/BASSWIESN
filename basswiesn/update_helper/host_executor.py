"""Trusted host transaction adapter, NOT an HTTP or command-line entry point.

Only the authenticated single-flight TransactionRunner may use this adapter.
Activation requires explicit administrator onboarding; the Web process never
executes this adapter. Overrides below are test injection, never request
parameters. Runtime operations use fixed arguments and exact IDs.
"""
from dataclasses import replace
import os
import time

from . import artifact_staging, candidate_runtime as runtime, host_preflight as host
from . import installation_cutover as cutover, installation_snapshot as snapshot, local_docker
from .onboarding import HOST_ROOT, _json, _policy_value, _write_new
from .protocol import require_digest
from .transaction import Candidate, Backup


HEALTH_PROBE = r'''import json,sys,urllib.request
try:
 op=urllib.request.build_opener(urllib.request.ProxyHandler({}))
 version=None
 for port,path in ((1328,'/api/readiness'),(1516,'/bmx/registry/v1/services'),(1860,'/health')):
  with op.open('http://127.0.0.1:'+str(port)+path,timeout=2) as r:
   assert r.status==200
   b=json.loads(r.read(131073)); assert isinstance(b,dict)
   if port==1328:
    assert b.get('ready') is True and not b.get('update_validation_mode',False)
    version=b['version']
   elif port==1516: assert isinstance(b.get('bmx_services'),list)
   else: assert b.get('status')=='ok'
 print(json.dumps({'ready':True,'version':version}))
except Exception: sys.exit(2)
'''
LABEL = "io.basswiesn.update.request"
ROLE = "io.basswiesn.update.role"


class ExecutorRejected(ValueError):
    def __init__(self):
        super().__init__("HOST_ACTION_REJECTED")


class ProductionExecutor:
    def __init__(self, *, root=HOST_ROOT, owner_uid=0, run=runtime._command,
                 observe=local_docker.collect, stopped=local_docker.confirm_stopped,
                 fetch=artifact_staging.fetch_into, health_seconds=100):
        if type(health_seconds) not in (int, float) or not 0 < health_seconds <= 180:
            raise ExecutorRejected()
        self.root, self.owner_uid, self.run = root, owner_uid, run
        self.observe, self.stopped, self.fetch, self.health_seconds = observe, stopped, fetch, health_seconds
        self.active_plan = None
        self.runtime = runtime.CandidateRuntime(root=root+"/candidates", staging_root=root+"/staging",
                                               restore_root=root+"/restores", owner_uid=owner_uid, run=run)

    def _policy(self):
        return host.load_enrollment(self.root+"/enrollment.json", policy_owner_uid=self.owner_uid)

    def current_version(self):
        policy = self._policy()
        host.inspect_files(policy)
        host.inspect_runtime(policy, self.observe(policy))
        return policy.version

    def preflight(self, plan):
        policy = self._policy()
        if policy.version != plan.current_version:
            raise ExecutorRejected()
        host.inspect_files(policy)
        observation = self.observe(policy)
        host.inspect_runtime(policy, observation)
        if self.active_plan != plan:
            self.active_plan, self.original = plan, policy
            self.environment = observation.container["Config"]["Env"]
            self.ports = observation.container["HostConfig"]["PortBindings"]
            self.control_source = next((m["Source"] for m in observation.container["Mounts"]
                if m["Destination"] == "/run/basswiesn-update"), None)
            runtime.environment_bytes(self.environment, validation=False)
            self.built = self.backup = self.cutover = self.live_id = self.live_policy = None
            self.old_remove_requested = False
            self.restore_policy = policy
        if policy != self.original:
            raise ExecutorRejected()
        # The future Docker build context must exclude BOTH preserved generations.
        with host.directory(policy.root, owners={0, policy.installation_owner_uid}) as root:
            cutover._build_exclusion(root, {0, policy.installation_owner_uid})
        for name in ("candidates", "backups", "restores", "staging"):
            with host.directory(self.root+"/"+name, owners={0, self.owner_uid},
                                final_owners={self.owner_uid}, private=True):
                pass

    def _check(self, plan):
        if plan != self.active_plan:
            raise ExecutorRejected()

    def _save(self, name, value):
        with host.directory(self.root+"/candidates/"+self.active_plan.request_id, owners={0, self.owner_uid},
                            final_owners={self.owner_uid}, private=True) as job:
            _write_new(job, name, _json(value), self.owner_uid)

    def prepare_candidate(self, plan):
        self._check(plan)
        staged = artifact_staging.stage_release(plan.request_id, plan.target_version,
            staging_root=self.root+"/staging", owner_uid=self.owner_uid, fetch=self.fetch)
        self.built = self.runtime.build(plan.request_id, plan.target_version)
        self._save("original-enrollment.json", _policy_value(self.original))
        return Candidate(plan.request_id, plan.target_version, staged["archive_sha256"])

    def stop_original(self, plan):
        self._check(plan)
        # Revalidate just before mutation, not only before the minutes-long build.
        self.preflight(plan)
        self.run(["stop", "--time", "30", self.original.container_id], seconds=40)
        if self.stopped(self.original) is not True:
            raise ExecutorRejected()

    def backup_original(self, plan):
        self._check(plan)
        self.backup = snapshot.capture(self.original, plan.request_id, assert_stopped=self.stopped,
            backup_root=self.root+"/backups", owner_uid=self.owner_uid)
        self._save("snapshot.json", {"request_id": self.backup.request_id,
            "installation_id": self.backup.installation_id, "version": self.backup.version,
            "manifest_sha256": self.backup.manifest_sha256})
        return Backup(plan.request_id, self.backup.version, self.backup.manifest_sha256)

    def _create(self, plan, *, image_id, original=False):
        role = "restored" if original else "live"
        name = "basswiesn-update-"+role+"-"+plan.request_id
        env_name = role+".env"
        job_path = self.root+"/candidates/"+plan.request_id
        with host.directory(job_path, owners={0, self.owner_uid}, final_owners={self.owner_uid}, private=True) as job:
            _write_new(job, env_name, runtime.environment_bytes(self.environment, validation=False), self.owner_uid)
        policy = self.original
        args = ["create", "--name", name, "--label", LABEL+"="+plan.request_id, "--label", ROLE+"="+role,
            "--network", policy.compose_project+"_default", "--read-only", "--cap-drop", "ALL", "--init",
            "--user", "10001:10001", "--security-opt", "no-new-privileges:true", "--restart", "unless-stopped",
            "--tmpfs", "/tmp:rw,noexec,nosuid,size=64m", "--env-file", job_path+"/"+env_name,
            "--health-cmd", "python tools/run_dev.py --healthcheck", "--health-interval", "30s",
            "--health-timeout", "8s", "--health-start-period", "30s", "--health-retries", "3",
            "--mount", "type=bind,src="+policy.root+"/data,dst=/app/data",
            "--mount", "type=bind,src="+policy.root+"/data/secrets/setup-rebuild,dst=/app/secrets/setup-rebuild,readonly"]
        if self.control_source is not None:
            args.extend(["--mount", "type=bind,src="+self.control_source+",dst=/run/basswiesn-update,readonly"])
        labels = {"project": policy.compose_project, "service": "basswiesn", "oneoff": "False",
            "container-number": "1", "project.working_dir": policy.root,
            "project.config_files": policy.root+"/docker-compose.yml"}
        for key, value in labels.items():
            args.extend(["--label", "com.docker.compose."+key+"="+value])
        for port in sorted(host.PORTS):
            for binding in self.ports[port]:
                address = binding["HostIp"]
                prefix = ("["+address+"]:" if ":" in address else address+":") if address else ""
                args.extend(["--publish", prefix+binding["HostPort"]+":"+port])
        args.append(image_id)
        try:
            identity = self.run(args, seconds=20).decode("ascii").strip()
            require_digest(identity)
        except (ValueError, OSError):
            # A lost CLI response may still have created the named object. A
            # successful exact-name inspect plus ownership proof recovers its ID;
            # inability to prove this propagates, never launches another copy.
            value = host.unique_json(self.run(["inspect", "--type", "container", name], seconds=10))
            if not isinstance(value, list) or len(value) != 1 or value[0].get("Name") != "/"+name:
                raise ExecutorRejected() from None
            identity = value[0].get("Id")
            require_digest(identity)
        self._owned(identity, image_id, role)
        if not original:
            # Retain ownership in memory before a receipt fsync can fail, so the
            # transaction's rollback still stops this exact partial candidate.
            self.live_id = identity
        self._save(role+"-container.json", {"id": identity, "image_id": image_id, "role": role})
        return identity

    def _owned(self, identity, image_id, role):
        value = runtime._inspect(self.run, "container", identity)
        labels = value.get("Config", {}).get("Labels", {})
        if (value.get("Id") != identity or value.get("Image") != image_id
                or labels.get(LABEL) != self.active_plan.request_id or labels.get(ROLE) != role):
            raise ExecutorRejected()
        return value

    def start_candidate(self, plan, candidate, backup):
        self._check(plan)
        if (candidate != Candidate(plan.request_id, plan.target_version, self.built["archive_sha256"])
                or backup != Backup(plan.request_id, plan.current_version, self.backup.manifest_sha256)):
            raise ExecutorRejected()
        self.runtime.validate(self.original, self.backup, plan.target_version, assert_stopped=self.stopped,
            backup_root=self.root+"/backups", environment=self.environment)
        self.cutover = cutover.prepare(self.original, self.backup, plan.target_version, assert_stopped=self.stopped,
            backup_root=self.root+"/backups", staging_root=self.root+"/staging", owner_uid=self.owner_uid)
        self._save("cutover.json", {"request_id": self.cutover.request_id,
            "installation_id": self.cutover.installation_id, "target_version": self.cutover.target_version,
            "plan_sha256": self.cutover.plan_sha256})
        cutover.exchange(self.cutover, self.original, assert_stopped=self.stopped, owner_uid=self.owner_uid)
        self.live_id = self._create(plan, image_id=self.built["image_id"])
        hashes, env = host.capture_configuration(self.original.root, installation_owner_uid=self.original.installation_owner_uid)
        self.live_policy = replace(self.original, container_id=self.live_id, image_id=self.built["image_id"],
                                   version=plan.target_version, file_sha256=hashes, env_sha256=env)
        self.run(["start", self.live_id], seconds=20)

    def _health(self, policy):
        deadline = time.monotonic()+self.health_seconds
        initial = None
        while time.monotonic() < deadline:
            value = runtime._inspect(self.run, "container", policy.container_id)
            state = value.get("State", {})
            signature = (state.get("Pid"), state.get("StartedAt"), value.get("RestartCount"))
            if (value.get("Image") != policy.image_id or state.get("Running") is not True
                    or state.get("OOMKilled") is not False or state.get("Restarting") is not False):
                raise ExecutorRejected()
            if initial is None:
                initial = signature
            if initial != signature:
                raise ExecutorRejected()
            if state.get("Health", {}).get("Status") == "healthy":
                try:
                    result = host.unique_json(self.run(["exec", policy.container_id, "python", "-I", "-B", "-c", HEALTH_PROBE], seconds=10))
                    if result != {"ready": True, "version": policy.version}:
                        raise ExecutorRejected()
                    observation = self.observe(policy)
                    host.inspect_runtime(policy, observation)
                    final = observation.container
                    if (final["State"]["Pid"], final["State"]["StartedAt"], final["RestartCount"]) != initial:
                        raise ExecutorRejected()
                    host.inspect_files(policy)
                    return
                except runtime.CandidateRejected as error:
                    if error.code not in {"COMMAND_FAILED", "COMMAND_TIMEOUT"}:
                        raise
            time.sleep(min(0.5, max(0, deadline-time.monotonic())))
        raise ExecutorRejected()

    def _enroll(self, expected, new, name):
        if self._policy() != expected:
            raise ExecutorRejected()
        with host.directory(self.root, owners={0, self.owner_uid}, final_owners={self.owner_uid}, private=True) as root:
            temporary = "enrollment-"+self.active_plan.request_id+"-"+name+".json"
            _write_new(root, temporary, _json(_policy_value(new)), self.owner_uid)
            if self._policy() != expected:
                raise ExecutorRejected()
            os.replace(temporary, "enrollment.json", src_dir_fd=root, dst_dir_fd=root)
            os.fsync(root)
        if self._policy() != new:
            raise ExecutorRejected()

    def verify_candidate(self, plan):
        self._check(plan)
        self._health(self.live_policy)
        self._enroll(self.original, self.live_policy, "candidate")
        # Avoid leaving two Compose-labelled service containers after success.
        # Image and complete old filesystem generation remain retained. If the
        # removal reply fails, rollback can recreate ONLY this approved runtime.
        old = runtime._inspect(self.run, "container", self.original.container_id)
        if (old.get("Image") != self.original.image_id or old.get("State", {}).get("Running") is not False
                or old.get("State", {}).get("Pid") != 0):
            raise ExecutorRejected()
        self.old_remove_requested = True
        self.run(["rm", self.original.container_id], seconds=10)
        if self._old_exists():
            raise ExecutorRejected()
        self._health(self.live_policy)

    def _old_exists(self):
        output = self.run(["ps", "--all", "--quiet", "--no-trunc", "--filter", "id="+self.original.container_id], seconds=10)
        lines = output.decode("ascii").splitlines()
        if lines not in ([], [self.original.container_id]):
            raise ExecutorRejected()
        return bool(lines)

    def restore_original(self, plan, backup, *, candidate_may_have_started):
        self._check(plan)
        if backup is not None and (self.backup is None or backup != Backup(plan.request_id, plan.current_version, self.backup.manifest_sha256)):
            raise ExecutorRejected()
        old_exists = self._old_exists() if self.old_remove_requested else True
        if self.live_id:
            value = self._owned(self.live_id, self.built["image_id"], "live")
            if value.get("State", {}).get("Running"):
                self.run(["stop", "--time", "30", self.live_id], seconds=40)
        if self.cutover:
            anchor = self.original if old_exists else self.live_policy
            if anchor is None:
                raise ExecutorRejected()
            cutover.exchange(self.cutover, self.original, assert_stopped=lambda _: self.stopped(anchor),
                             rollback=True, owner_uid=self.owner_uid)
        # No file exchange happened when backup/start failed early: original
        # data is still intact and must not be overwritten by a partial backup.
        identity = self.original.container_id if old_exists else self._create(plan, image_id=self.original.image_id, original=True)
        self.restore_policy = replace(self.original, container_id=identity)
        current = self._policy()
        if current not in (self.original, self.live_policy):
            raise ExecutorRejected()
        if current != self.restore_policy:
            self._enroll(current, self.restore_policy, "restored")
        value = runtime._inspect(self.run, "container", identity)
        if value.get("Id") != identity or value.get("Image") != self.original.image_id:
            raise ExecutorRejected()
        if value.get("State", {}).get("Running") is not True:
            self.run(["start", identity], seconds=20)

    def verify_original(self, plan):
        self._check(plan)
        self._health(self.restore_policy)
        if self.live_id:
            value = self._owned(self.live_id, self.built["image_id"], "live")
            if value.get("State", {}).get("Running") is not False or value.get("State", {}).get("Pid") != 0:
                raise ExecutorRejected()
            self.run(["rm", self.live_id], seconds=10)
