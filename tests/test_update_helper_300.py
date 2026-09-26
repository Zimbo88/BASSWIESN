"""Offline host-helper contract tests. No Docker, network or installation writes.

The synthetic executor tests orchestration, NOT a production upgrade/rollback.
Unix socketpair tests use only connected local descriptors (no listener).
"""
from concurrent.futures import ThreadPoolExecutor
from dataclasses import replace
import hashlib
import json
import os
from pathlib import Path
import socket
import struct
import subprocess
import sys
import threading
import uuid

import pytest

from basswiesn.update_helper import journal as storage
from basswiesn.update_helper.journal import Journal
from basswiesn.update_helper.protocol import Authorization, Code, Request, UpdateRejected, decode_request
from basswiesn.update_helper.service import UpdateService, serve_connection
from basswiesn.update_helper.transaction import Backup, Candidate, TransactionRunner


pytestmark = pytest.mark.unit
# Synthetic test capability only. No deployed credential is embedded here.
TOKEN = "A" * 43
DIGEST = hashlib.sha256(TOKEN.encode()).hexdigest()
REQUEST_ID = "12345678-1234-4234-8234-123456789abc"


def wire(**changes):
    body = {"protocol": 1, "action": "install", "approve": True,
            "request_id": REQUEST_ID, "target_version": "3.0.0",
            "expected_current_version": "2.6.5", "credential": TOKEN}
    body.update(changes)
    return json.dumps(body).encode()


@pytest.fixture
def store(tmp_path):
    directory = tmp_path / "private-journal"
    directory.mkdir(mode=0o700)
    value = Journal(directory, owner_uid=os.getuid())
    value.initialize()
    yield value
    value.close()


class FakeHost:
    """Only in-memory synthetic installation; failure may follow partial action."""
    def __init__(self, store, *, fail=None, interrupt=None):
        self.store = store
        self.fail = set(fail or ())
        self.interrupt = interrupt
        self.calls = []
        self.version = "2.6.5"
        self.running = True
        self.data = b"original synthetic data"
        self.backup_data = None
        self.rollback_used_backup = None

    def note(self, method, expected=None):
        if expected is not None:
            assert self.store.public_status()["state"] == expected
        self.calls.append(method)
        if self.interrupt == method:
            raise KeyboardInterrupt("simulated process death")
        if method in self.fail:
            raise RuntimeError("SECRET-FIXTURE: private exception must not be persisted")

    def current_version(self):
        self.note("current_version")
        return self.version

    def preflight(self, plan):
        self.note("preflight")

    def prepare_candidate(self, plan):
        self.note("prepare_candidate", "STAGING")
        return Candidate(plan.request_id, plan.target_version, "c" * 64)

    def stop_original(self, plan):
        self.running = False
        self.note("stop_original", "STOP_REQUESTED")

    def backup_original(self, plan):
        assert not self.running
        self.note("backup_original", "BACKUP_STARTED")
        self.backup_data = self.data
        return Backup(plan.request_id, plan.current_version, hashlib.sha256(self.backup_data).hexdigest())

    def start_candidate(self, plan, candidate, backup):
        assert self.backup_data == b"original synthetic data"
        self.version, self.running = plan.target_version, True
        self.data = b"synthetic migrated data"
        self.note("start_candidate", "START_REQUESTED")

    def verify_candidate(self, plan):
        self.note("verify_candidate", "STARTED")
        assert self.version == plan.target_version and self.running

    def restore_original(self, plan, backup, *, candidate_may_have_started):
        self.note("restore_original", "ROLLING_BACK")
        self.rollback_used_backup = candidate_may_have_started
        if candidate_may_have_started:
            assert backup and hashlib.sha256(self.backup_data).hexdigest() == backup.manifest_sha256
            self.data = self.backup_data
        else:
            assert self.data == b"original synthetic data"
        self.version, self.running = plan.current_version, True

    def verify_original(self, plan):
        self.note("verify_original", "ROLLING_BACK")
        assert self.version == plan.current_version and self.running
        assert self.data == b"original synthetic data"


def service(store, host=None):
    return UpdateService(Authorization(os.getuid(), DIGEST), TransactionRunner(store, host))


def run(store, host, **changes):
    return service(store, host).handle(wire(**changes), peer_uid=os.getuid())


def test_protocol_parses_and_never_reprs_capability():
    request = decode_request(wire())
    assert request.action == "install" and request.target_version == "3.0.0"
    assert TOKEN not in repr(request)
    assert DIGEST not in repr(Authorization(os.getuid(), DIGEST))


def test_submit_acknowledges_durable_intent_before_executor_and_survives_disconnect(store):
    host = FakeHost(store)
    left, right = socket.socketpair()
    right.settimeout(3)
    entered, finish = threading.Event(), threading.Event()
    original = host.prepare_candidate
    def prepare(plan):
        entered.set()
        assert finish.wait(5)
        return original(plan)
    host.prepare_candidate = prepare
    worker = threading.Thread(target=serve_connection, args=(left, service(store, host)))
    worker.start()
    payload = wire(action="submit")
    try:
        right.sendall(struct.pack("!I", len(payload))+payload)
        count = struct.unpack("!I", right.recv(4))[0]
        data = bytearray()
        while len(data) < count:
            data.extend(right.recv(count-len(data)))
        answer = json.loads(data)
        assert answer["ok"] and answer["result"]["state"] == "REQUESTED"
        assert entered.wait(3)
        assert store.public_status()["state"] == "STAGING"
        right.close()  # browser/container can disappear; root worker continues
    finally:
        finish.set()
        worker.join(5)
        right.close()
    assert not worker.is_alive() and store.public_status()["state"] == "COMPLETE"
    assert host.calls.count("start_candidate") == 1


@pytest.mark.parametrize("change", [
    {"url": "https://example.invalid"}, {"path": "/arbitrary"}, {"command": "anything"},
    {"environment": {}}, {"docker_options": []}, {"peer_uid": 0}, {"rollback": True},
    {"protocol": True}, {"protocol": 2}, {"approve": 1}, {"approve": False},
    {"action": "reboot"}, {"target_version": "3.0.0-rc1"}, {"target_version": "v3.0.0"},
    {"target_version": "03.0.0"}, {"target_version": "3.0.0\n"}, {"target_version": "../../bad"},
    {"expected_current_version": None}, {"request_id": "not-uuid"},
    {"request_id": REQUEST_ID.upper()}, {"request_id": "12345678-1234-1234-8234-123456789abc"},
    {"credential": ""}, {"credential": "é" * 43}, {"credential": []},
])
def test_invalid_protocol_cannot_invoke_executor_or_record_job(store, change):
    host = FakeHost(store)
    result = run(store, host, **change)
    assert not result["ok"] and result["code"] in {"BAD_REQUEST", "AUTHORIZATION_REQUIRED"}
    assert not host.calls and store.read()["jobs"] == []
    assert TOKEN not in json.dumps(result)


@pytest.mark.parametrize("data", [b"", b"null", b"[]", b"\xff", b"{" * 2048, b" " * 2049,
    b'{"protocol":1,"protocol":1,"action":"status"}', b'{"protocol":1,"action":"status","credential":"x"}',
    b'{"protocol":1,"action":"status"} trailing'])
def test_bad_json_is_bounded_and_has_safe_errors(store, data):
    assert service(store).handle(data, peer_uid=os.getuid()) == {"ok": False, "code": "BAD_REQUEST"}


@pytest.mark.parametrize("uid", [-1, True, "0", None])
def test_peer_is_kernel_integer_identity_not_request_field(store, uid):
    assert service(store).handle(wire(), peer_uid=uid) == {"ok": False, "code": "PEER_DENIED"}


def test_uid_and_admin_capability_are_independent_requirements(store):
    host = FakeHost(store)
    svc = service(store, host)
    assert svc.handle(wire(), peer_uid=os.getuid() + 1)["code"] == "PEER_DENIED"
    assert run(store, host, credential="B" * 43)["code"] == "AUTHORIZATION_REQUIRED"
    assert not host.calls and not store.read()["jobs"]
    assert svc.handle(b'{"protocol":1,"action":"status"}', peer_uid=os.getuid())["result"]["state"] == "IDLE"


def test_unimplemented_host_cannot_report_installation_success(store):
    assert run(store, None) == {"ok": False, "code": "EXECUTOR_UNAVAILABLE"}
    assert store.read()["jobs"] == []


def test_success_sequence_has_durable_intents_consistent_backup_and_no_secret(store):
    host = FakeHost(store)
    result = run(store, host)
    assert result["ok"] and result["result"]["state"] == "COMPLETE"
    assert [e["state"] for e in result["result"]["events"]] == storage.ORDER
    assert host.calls == ["current_version", "preflight", "prepare_candidate", "current_version", "preflight",
                          "stop_original", "backup_original", "start_candidate", "verify_candidate"]
    assert host.version == "3.0.0" and host.data == b"synthetic migrated data"
    data = json.dumps(store.read())
    assert TOKEN not in data and "SECRET-FIXTURE" not in data and "credential" not in data
    assert store.read()["jobs"][0]["backup_sha256"] == hashlib.sha256(b"original synthetic data").hexdigest()
    assert store.read()["jobs"][0]["artifact_sha256"] == "c" * 64


def test_request_id_retry_returns_original_result_without_second_update(store):
    host = FakeHost(store)
    first = run(store, host)
    calls = list(host.calls)
    assert run(store, host) == first
    assert host.calls == calls
    assert run(store, host, target_version="3.0.1")["code"] == "REQUEST_ID_REUSED"
    assert host.calls == calls


@pytest.mark.parametrize("version", ["2.6.5", "2.6.4", "1.9.0"])
def test_downgrade_or_reinstall_not_implicitly_authorized(store, version):
    host = FakeHost(store)
    outcome = run(store, host, target_version=version)["result"]
    assert outcome["state"] == "FAILED" and outcome["code"] == "NOT_AN_UPGRADE"
    assert not host.calls


def test_changed_current_version_stops_before_downtime(store):
    host = FakeHost(store)
    host.version = "2.6.6"
    assert run(store, host)["result"]["code"] == "CURRENT_VERSION_CHANGED"
    assert host.calls == ["current_version"] and host.running


def test_version_is_rechecked_after_candidate_preparation(store):
    class ChangedHost(FakeHost):
        def prepare_candidate(self, plan):
            result = super().prepare_candidate(plan)
            self.version = "2.6.6"
            return result
    host = ChangedHost(store)
    result = run(store, host)["result"]
    assert result["state"] == "FAILED" and result["code"] == "CURRENT_VERSION_CHANGED"
    assert "stop_original" not in host.calls and host.running


@pytest.mark.parametrize("method,code,restore,used_backup", [
    ("current_version", "PREFLIGHT_FAILED", False, None),
    ("preflight", "PREFLIGHT_FAILED", False, None),
    ("prepare_candidate", "SOURCE_VERIFICATION_FAILED", False, None),
    ("stop_original", "STOP_FAILED", True, False),
    ("backup_original", "BACKUP_FAILED", True, False),
    ("start_candidate", "START_FAILED", True, True),
    ("verify_candidate", "HEALTH_FAILED", True, True),
])
def test_each_adapter_failure_is_classified_and_restored_only_when_verified(store, method, code, restore, used_backup):
    host = FakeHost(store, fail={method})
    result = run(store, host)["result"]
    assert result["state"] == ("RESTORED" if restore else "FAILED")
    assert result["code"] == code and not result["recovery_required"]
    assert host.running and host.version == "2.6.5" and host.data == b"original synthetic data"
    assert ("restore_original" in host.calls) == restore
    assert host.rollback_used_backup == used_backup
    assert "SECRET-FIXTURE" not in json.dumps(store.read())
    calls = list(host.calls)
    assert run(store, host)["result"] == result and host.calls == calls


@pytest.mark.parametrize("failure", ["restore_original", "verify_original"])
def test_restore_failure_blocks_followup_jobs_and_never_claims_restored(store, failure):
    host = FakeHost(store, fail={"verify_candidate", failure})
    result = run(store, host)["result"]
    assert result["state"] == "MANUAL_ACTION_REQUIRED" and result["recovery_required"]
    assert result["code"] == "RESTORE_FAILED"
    calls = list(host.calls)
    assert run(store, host, request_id=str(uuid.uuid4()))["code"] == "RECOVERY_REQUIRED"
    assert host.calls == calls
    store.recover_interrupted()
    assert store.public_status() == result


@pytest.mark.parametrize("receipt_kind,changes", [
    ("candidate", {"version": "9.9.9"}), ("candidate", {"request_id": str(uuid.uuid4())}),
    ("candidate", {"archive_sha256": "not-a-digest"}),
    ("backup", {"version": "9.9.9"}), ("backup", {"request_id": str(uuid.uuid4())}),
    ("backup", {"manifest_sha256": "not-a-digest"}),
])
def test_receipts_must_match_request_version_and_valid_hash(store, receipt_kind, changes):
    class WrongReceipt(FakeHost):
        def prepare_candidate(self, plan):
            result = super().prepare_candidate(plan)
            return replace(result, **changes) if receipt_kind == "candidate" else result
        def backup_original(self, plan):
            result = super().backup_original(plan)
            return replace(result, **changes) if receipt_kind == "backup" else result
    host = WrongReceipt(store)
    result = run(store, host)["result"]
    assert result["state"] == ("FAILED" if receipt_kind == "candidate" else "RESTORED")
    assert "start_candidate" not in host.calls


@pytest.mark.parametrize("method,expected", [
    ("preflight", "FAILED"), ("prepare_candidate", "FAILED"),
    ("stop_original", "MANUAL_ACTION_REQUIRED"), ("backup_original", "MANUAL_ACTION_REQUIRED"),
    ("start_candidate", "MANUAL_ACTION_REQUIRED"), ("verify_candidate", "MANUAL_ACTION_REQUIRED"),
])
def test_interrupted_process_is_reconciled_without_replaying_actions(store, method, expected):
    host = FakeHost(store, interrupt=method)
    with pytest.raises(KeyboardInterrupt):
        TransactionRunner(store, host).execute(decode_request(wire()))
    calls = list(host.calls)
    store.recover_interrupted()
    assert store.public_status()["state"] == expected and host.calls == calls
    store.recover_interrupted()
    assert store.public_status()["state"] == expected and host.calls == calls


def test_separate_instances_share_process_lock(store, tmp_path):
    other = Journal(tmp_path / "private-journal", owner_uid=os.getuid())
    try:
        with store.exclusive():
            with pytest.raises(UpdateRejected, match="^BUSY$"):
                with other.exclusive():
                    pytest.fail("second writer entered")
        with other.exclusive():
            assert other.read() == store.read()
    finally:
        other.close()


def test_independent_process_cannot_acquire_existing_writer_lock(store, tmp_path):
    code = """
import os, sys
from pathlib import Path
from basswiesn.update_helper.journal import Journal
from basswiesn.update_helper.protocol import UpdateRejected
value = Journal(Path(sys.argv[1]), owner_uid=os.getuid())
try:
    with value.exclusive():
        print('ACQUIRED')
except UpdateRejected as error:
    print(error.code.value)
finally:
    value.close()
"""
    def probe():
        return subprocess.run([sys.executable, "-c", code, str(tmp_path / "private-journal")],
                              capture_output=True, text=True, timeout=5, check=True).stdout.strip()
    with store.exclusive():
        assert probe() == "BUSY"
    assert probe() == "ACQUIRED"


def test_threads_cannot_release_each_others_lock_or_save_without_ownership(store):
    with store.exclusive(), ThreadPoolExecutor(max_workers=1) as pool:
        def contender():
            with pytest.raises(UpdateRejected, match="^BUSY$"):
                with store.exclusive():
                    pytest.fail("thread entered")
            with pytest.raises(UpdateRejected, match="^BUSY$"):
                store.save({"schema": 1, "jobs": []})
        pool.submit(contender).result(timeout=2)
        assert store.lock_owner == threading.get_ident()
        store.save({"schema": 1, "jobs": []})
    with store.exclusive():
        pass


def test_nested_lock_rejected_without_disturbing_outer_lock(store):
    with store.exclusive():
        with pytest.raises(UpdateRejected, match="^BUSY$"):
            with store.exclusive():
                pass
        with pytest.raises(UpdateRejected, match="^BUSY$"):
            store.close()
        store.save({"schema": 1, "jobs": []})


@pytest.mark.parametrize("mode", [0o777, 0o755, 0o750])
def test_state_directory_must_be_owner_private(tmp_path, mode):
    path = tmp_path / "unsafe"
    path.mkdir(mode=mode)
    path.chmod(mode)
    with pytest.raises(UpdateRejected, match="^JOURNAL_UNSAFE$"):
        Journal(path, owner_uid=os.getuid())


def test_symlink_directory_and_shared_writable_parent_rejected(tmp_path):
    target = tmp_path / "target"
    target.mkdir(mode=0o700)
    alias = tmp_path / "alias"
    alias.symlink_to(target, target_is_directory=True)
    with pytest.raises(UpdateRejected, match="^JOURNAL_UNSAFE$"):
        Journal(alias, owner_uid=os.getuid())
    parent = tmp_path / "shared"
    parent.mkdir(mode=0o777)
    parent.chmod(0o777)
    child = parent / "private"
    child.mkdir(mode=0o700)
    with pytest.raises(UpdateRejected, match="^JOURNAL_UNSAFE$"):
        Journal(child, owner_uid=os.getuid())


@pytest.mark.parametrize("name", ["state.json", "operation.lock"])
@pytest.mark.parametrize("kind", ["symlink", "hardlink", "fifo", "permissions", "deleted"])
def test_unsafe_or_missing_files_never_reset_history(store, tmp_path, name, kind):
    path = tmp_path / "private-journal" / name
    if kind == "permissions":
        path.chmod(0o644)
    else:
        old = tmp_path / (name + ".original")
        path.rename(old)
        if kind == "symlink":
            path.symlink_to(old)
        elif kind == "hardlink":
            os.link(old, path)
        elif kind == "fifo":
            os.mkfifo(path, mode=0o600)
    host = FakeHost(store)
    result = run(store, host)
    assert not result["ok"] and result["code"].startswith("JOURNAL_")
    assert not host.calls
    with pytest.raises(UpdateRejected):
        store.initialize()


def test_initialization_is_explicit_and_never_repeatable(tmp_path):
    directory = tmp_path / "new"
    directory.mkdir(mode=0o700)
    value = Journal(directory, owner_uid=os.getuid())
    try:
        with pytest.raises(UpdateRejected, match="^JOURNAL_CORRUPT$"):
            value.read()
        value.initialize()
        assert set(p.name for p in directory.iterdir()) == {"operation.lock", "state.json"}
        with pytest.raises(UpdateRejected, match="^JOURNAL_UNSAFE$"):
            value.initialize()
        assert (directory / "state.json").stat().st_mode & 0o777 == 0o600
    finally:
        value.close()


@pytest.mark.parametrize("data", [b"", b"{}", b'{"schema":1,"jobs":[],"secret":"redact"}',
    b'{"schema":1,"schema":1,"jobs":[]}', b'{"schema":true,"jobs":[]}', b"x" * (storage.MAX_BYTES + 1)])
def test_corrupt_journal_is_rejected_without_repair(store, tmp_path, data):
    path = tmp_path / "private-journal/state.json"
    path.write_bytes(data)
    assert run(store, FakeHost(store))["code"] == "JOURNAL_CORRUPT"
    assert path.read_bytes() == data


def test_history_schema_rejects_forged_transition_and_unbounded_error_text(store, tmp_path):
    with store.exclusive():
        store.begin(decode_request(wire()))
    value = store.read()
    path = tmp_path / "private-journal/state.json"
    value["jobs"][0]["events"][0]["code"] = "SECRET-FIXTURE: must not reach status"
    path.write_text(json.dumps(value))
    assert service(store).handle(b'{"protocol":1,"action":"status"}', peer_uid=os.getuid())["code"] == "JOURNAL_CORRUPT"
    value["jobs"][0]["events"][0]["code"] = None
    value["jobs"][0]["state"] = "COMPLETE"
    value["jobs"][0]["events"][0]["state"] = "COMPLETE"
    path.write_text(json.dumps(value))
    assert run(store, FakeHost(store))["code"] == "JOURNAL_CORRUPT"


def test_journal_failure_before_stop_never_stops_installation(store, monkeypatch):
    host = FakeHost(store)
    original = store.transition
    def fail(request_id, state, **kwargs):
        if state == "STOP_REQUESTED":
            raise UpdateRejected(Code.JOURNAL_IO)
        return original(request_id, state, **kwargs)
    monkeypatch.setattr(store, "transition", fail)
    assert run(store, host)["code"] == "JOURNAL_IO"
    assert host.running and "stop_original" not in host.calls
    store.recover_interrupted()
    assert store.public_status()["state"] == "FAILED"


def test_journal_failure_after_stop_requires_recovery_not_fake_restore(store, monkeypatch):
    host = FakeHost(store)
    original = store.transition
    def fail(request_id, state, **kwargs):
        if state == "STOPPED":
            raise UpdateRejected(Code.JOURNAL_IO)
        return original(request_id, state, **kwargs)
    monkeypatch.setattr(store, "transition", fail)
    assert run(store, host)["code"] == "JOURNAL_IO"
    assert not host.running and "start_candidate" not in host.calls and "restore_original" not in host.calls
    store.recover_interrupted()
    assert store.public_status()["state"] == "MANUAL_ACTION_REQUIRED"


def test_fsync_failure_never_calls_host_and_leaves_no_partial_temp(store, tmp_path, monkeypatch):
    def fail(*args):
        raise OSError("sensitive filesystem detail")
    host = FakeHost(store)
    monkeypatch.setattr(storage.os, "fsync", fail)
    assert run(store, host)["code"] == "JOURNAL_IO" and not host.calls
    assert store.read()["jobs"] == []
    assert set(p.name for p in (tmp_path / "private-journal").iterdir()) == {"state.json", "operation.lock"}


def test_failed_directory_fsync_is_uncertain_and_blocks_same_process(store, tmp_path, monkeypatch):
    original = storage.os.fsync
    def fail_directory(fd):
        if fd == store.fd:
            raise OSError("simulated directory fsync failure after atomic replace")
        return original(fd)
    host = FakeHost(store)
    with monkeypatch.context() as patch:
        patch.setattr(storage.os, "fsync", fail_directory)
        assert run(store, host)["code"] == "JOURNAL_IO"
    assert not host.calls and store.read()["jobs"][0]["state"] == "REQUESTED"
    # Restored filesystem availability does not silently clear an uncertainty.
    assert run(store, host)["code"] == "JOURNAL_IO" and not host.calls
    reopened = Journal(tmp_path / "private-journal", owner_uid=os.getuid())
    try:
        reopened.recover_interrupted()
        assert reopened.public_status()["state"] == "FAILED"
        assert reopened.public_status()["code"] == "INTERRUPTED_BEFORE_CUTOVER"
    finally:
        reopened.close()


def test_atomic_replace_failure_preserves_old_journal(store, tmp_path, monkeypatch):
    original = (tmp_path / "private-journal/state.json").read_bytes()
    def fail(*args, **kwargs):
        raise OSError("simulated replace failure")
    monkeypatch.setattr(storage.os, "replace", fail)
    host = FakeHost(store)
    assert run(store, host)["code"] == "JOURNAL_IO" and not host.calls
    assert (tmp_path / "private-journal/state.json").read_bytes() == original
    assert not list((tmp_path / "private-journal").glob(".state-*.tmp"))


@pytest.mark.parametrize("state", storage.ORDER[1:-1] + ["ROLLING_BACK"])
def test_reconciliation_covers_every_persisted_nonterminal_state(store, state):
    request = decode_request(wire())
    with store.exclusive():
        store.begin(request)
        target = "STOP_REQUESTED" if state == "ROLLING_BACK" else state
        for stage in storage.ORDER[1:storage.ORDER.index(target) + 1]:
            kwargs = ({"artifact_sha256": "c" * 64} if stage == "STAGED" else
                      {"backup_sha256": "b" * 64} if stage == "BACKED_UP" else {})
            store.transition(REQUEST_ID, stage, **kwargs)
        if state == "ROLLING_BACK":
            store.transition(REQUEST_ID, "ROLLING_BACK", code=Code.STOP_FAILED)
    store.recover_interrupted()
    assert store.public_status()["state"] == ("FAILED" if state in storage.BEFORE_CUTOVER else "MANUAL_ACTION_REQUIRED")


def test_terminal_jobs_cannot_restart_or_be_mutated(store):
    run(store, FakeHost(store))
    with store.exclusive():
        with pytest.raises(UpdateRejected, match="^INVALID_TRANSITION$"):
            store.transition(REQUEST_ID, "REQUESTED")
    before = store.read()
    store.recover_interrupted()
    assert store.read() == before


def test_new_authenticated_job_can_follow_verified_restore(store):
    failed = FakeHost(store, fail={"verify_candidate"})
    assert run(store, failed)["result"]["state"] == "RESTORED"
    healthy = FakeHost(store)
    assert run(store, healthy, request_id=str(uuid.uuid4()))["result"]["state"] == "COMPLETE"
    assert len(store.read()["jobs"]) == 2


def test_capacity_does_not_evict_replay_ids(store, monkeypatch):
    monkeypatch.setattr(storage, "MAX_JOBS", 1)
    run(store, FakeHost(store), target_version="2.6.5")
    assert run(store, FakeHost(store), request_id=str(uuid.uuid4()))["code"] == "JOURNAL_CAPACITY"
    assert len(store.read()["jobs"]) == 1


def exchange(svc, data, *, timeout=0.1, declared_size=None, close_write=False):
    client, server = socket.socketpair(socket.AF_UNIX, socket.SOCK_STREAM)
    with ThreadPoolExecutor(max_workers=1) as pool, client:
        future = pool.submit(serve_connection, server, svc, request_timeout=timeout)
        client.settimeout(2)
        try:
            client.sendall(struct.pack("!I", len(data) if declared_size is None else declared_size) + data)
        except BrokenPipeError:
            # Peer authorization may reject and close before reading any frame.
            # Still require the complete, correct response below; no fake PASS.
            pass
        if close_write:
            client.shutdown(socket.SHUT_WR)
        def receive(size):
            result = b""
            while len(result) < size:
                block = client.recv(size - len(result))
                assert block
                result += block
            return result
        length = struct.unpack("!I", receive(4))[0]
        assert length < 16384
        response = json.loads(receive(length))
        future.result(timeout=2)
        return response


def test_local_socket_uses_kernel_peer_and_install_capability(store):
    svc = service(store, FakeHost(store))
    assert exchange(svc, b'{"protocol":1,"action":"status"}')["result"]["state"] == "IDLE"
    assert exchange(svc, wire(credential="B" * 43))["code"] == "AUTHORIZATION_REQUIRED"
    assert exchange(svc, wire())["result"]["state"] == "COMPLETE"


def test_socket_denies_other_kernel_peer(store):
    svc = UpdateService(Authorization(os.getuid() + 1, DIGEST), TransactionRunner(store))
    assert exchange(svc, b"") == {"ok": False, "code": "PEER_DENIED"}


@pytest.mark.parametrize("size,data,close_write", [
    (0, b"", False), (2049, b"", False), (4294967295, b"", False),
    (12, b"{", True), (12, b"{", False),
])
def test_socket_truncated_oversized_or_stalled_frame_fails_closed(store, size, data, close_write):
    assert exchange(service(store), data, declared_size=size, close_write=close_write)["code"] == "BAD_REQUEST"


def test_socket_client_disconnect_does_not_cancel_or_repeat_transaction(store):
    host = FakeHost(store)
    client, server = socket.socketpair(socket.AF_UNIX, socket.SOCK_STREAM)
    data = wire()
    client.sendall(struct.pack("!I", len(data)) + data)
    client.close()
    serve_connection(server, service(store, host))
    assert store.public_status()["state"] == "COMPLETE"
    assert host.calls.count("start_candidate") == 1
