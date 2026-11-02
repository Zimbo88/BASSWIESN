"""Enrollment tests with private fixtures; never inspect real Docker or radios."""
from concurrent.futures import ThreadPoolExecutor
from dataclasses import replace
import json
import os
from pathlib import Path
import stat
import threading

import pytest

from basswiesn.update_helper import onboarding as setup
from basswiesn.update_helper import host_preflight as host
from basswiesn.update_helper import local_docker
from tools import prepare_update_host as cli
from test_update_host_preflight_300 import enrolled, observed  # reusable synthetic fixtures


pytestmark = pytest.mark.unit


@pytest.fixture(autouse=True)
def no_real_docker(monkeypatch):
    def forbidden(*args, **kwargs):
        pytest.fail("enrollment test attempted real Docker inspection")
    monkeypatch.setattr(local_docker, "collect", forbidden)


@pytest.fixture
def params(enrolled, tmp_path):
    policy = enrolled[0]
    return {"root": policy.root, "installation_owner_uid": os.getuid(),
            "compose_project": policy.compose_project, "container_id": policy.container_id,
            "image_id": policy.image_id, "version": policy.version,
            "target": str(tmp_path / "new-private-host-state"), "owner_uid": os.getuid()}


def apply(plan, **kwargs):
    return setup.prepare(plan, approve=True, approval_sha256=plan.approval_sha256,
                         observe=kwargs.pop("observe", observed), **kwargs)


def tree(path):
    return {str(item.relative_to(path)): (stat.S_IMODE(item.stat().st_mode),
            item.read_bytes() if item.is_file() else None)
            for item in Path(path).rglob("*")}


def test_preview_is_repeatable_and_has_no_side_effects_or_private_output(params):
    before = tree(params["root"])
    plan = setup.preview(**params)
    again = setup.preview(**params)
    assert plan.approval_sha256 == again.approval_sha256
    assert not Path(plan.target).exists()
    assert before == tree(params["root"])
    value = plan.public()
    assert value["state"] == "AWAITING_ADMIN_APPROVAL"
    assert not value["runtime_observed"] and not value["installation_available"]
    assert value["local_docker_reads_on_apply"]
    for private in (plan.policy.root, plan.policy.container_id, plan.policy.image_id,
                    plan.policy.env_sha256, "never-execute-this"):
        assert private not in json.dumps(value)
        assert private not in repr(plan)


def test_preparation_creates_only_private_inert_state_and_preserves_application(params):
    plan = setup.preview(**params)
    before = tree(params["root"])
    calls = []
    def observe(policy):
        calls.append(policy.container_id)
        root = Path(plan.target)
        assert not (root / "enrollment.json").exists()
        assert not (root / "prepared.json").exists()
        return observed(policy)
    result = apply(plan, observe=observe)
    assert calls == [params["container_id"]]
    assert result["enrollment_prepared"]
    for key in ("installation_available", "onboarding_complete", "service_installed",
                "backup_restore_verified", "capability_provisioned"):
        assert result[key] is False
    assert before == tree(params["root"])
    root = Path(plan.target)
    assert set(p.name for p in root.iterdir()) == setup.ROOT_FILES | setup.SUBDIRECTORIES
    for item in (root, *root.rglob("*")):
        assert item.stat().st_uid == os.getuid()
        assert stat.S_IMODE(item.stat().st_mode) == (0o700 if item.is_dir() else 0o600)
    policy = host.load_enrollment(str(root / "enrollment.json"), policy_owner_uid=os.getuid())
    assert policy.installation_id != setup.PREVIEW_ID
    assert policy.env_sha256 == plan.policy.env_sha256
    assert policy.file_sha256 == plan.policy.file_sha256
    assert json.loads((root / "peer.json").read_text()) == setup.PEER
    assert setup.verify_preparation(plan.target, owner_uid=os.getuid()) == result
    serialized = str(tree(plan.target)) + json.dumps(result)
    for secret in ("never-execute-this", "DO_NOT_REPORT_THIS_VALUE", "SYNTHETIC_SECRET"):
        assert secret not in serialized


@pytest.mark.parametrize("approve,digest", [(False, "correct"), (1, "correct"),
    ("yes", "correct"), (True, ""), (True, "invalid"), (True, "0" * 64)])
def test_no_writes_without_exact_explicit_consent(params, approve, digest):
    plan = setup.preview(**params)
    with pytest.raises(setup.SetupRejected):
        setup.prepare(plan, approve=approve, approval_sha256=plan.approval_sha256 if digest == "correct" else digest)
    assert not Path(plan.target).exists()


@pytest.mark.parametrize("field,value", [
    ("version", "3.0.0"), ("compose_project", "different"),
    ("container_id", "d" * 64), ("image_id", "sha256:" + "d" * 64),
])
def test_changed_requested_identity_invalidates_previous_preview(params, field, value):
    original = setup.preview(**params)
    params[field] = value
    new = setup.preview(**params)
    assert new.approval_sha256 != original.approval_sha256
    with pytest.raises(setup.SetupRejected, match="^PREVIEW_CHANGED$"):
        setup.prepare(new, approve=True, approval_sha256=original.approval_sha256)
    assert not Path(new.target).exists()


@pytest.mark.parametrize("name", sorted(host.RELEASE_FILES | {".env"}))
def test_configuration_change_invalidates_preview_before_any_host_write(params, name):
    plan = setup.preview(**params)
    (Path(params["root"]) / name).write_bytes(b"changed fixture bytes")
    with pytest.raises(host.HostRejected, match="^CONFIG_CHANGED$"):
        apply(plan)
    assert not Path(plan.target).exists()
    assert setup.preview(**params).approval_sha256 != plan.approval_sha256


@pytest.mark.parametrize("kind", ["file", "directory", "symlink", "fifo"])
def test_existing_target_never_overwritten_even_when_empty(params, kind):
    plan = setup.preview(**params)
    target = Path(plan.target)
    if kind == "file":
        target.write_bytes(b"keep me")
    elif kind == "directory":
        target.mkdir(mode=0o700)
    elif kind == "symlink":
        target.symlink_to(params["root"], target_is_directory=True)
    else:
        os.mkfifo(target)
    before = target.lstat()
    for operation in (lambda: setup.preview(**params), lambda: apply(plan)):
        with pytest.raises(setup.SetupRejected, match="^TARGET_EXISTS$"):
            operation()
    assert target.lstat() == before


@pytest.mark.parametrize("change", [
    {"root": "/"}, {"root": "/tmp/../not-allowed"}, {"version": "3.0.0-rc1"},
    {"container_id": "--host=anything"}, {"image_id": "latest"},
    {"compose_project": "../other"}, {"installation_owner_uid": 10001},
    {"installation_owner_uid": True},
])
def test_identity_schema_validated_before_filesystem_read(params, change, monkeypatch):
    params.update(change)
    monkeypatch.setattr(setup, "capture_configuration", lambda *a, **k: pytest.fail("read before identity validation"))
    with pytest.raises(host.HostRejected, match="^POLICY_INVALID$"):
        setup.preview(**params)


@pytest.mark.parametrize("target_kind", ["inside", "equal", "ancestor"])
def test_preparation_cannot_write_inside_or_over_application(params, target_kind):
    root = Path(params["root"])
    params["target"] = str(root / "host-state" if target_kind == "inside" else root if target_kind == "equal" else root.parent)
    with pytest.raises(setup.SetupRejected, match="^TARGET_UNSAFE$"):
        setup.preview(**params)


def test_root_or_test_owner_required_before_reads_and_writes(params, monkeypatch):
    monkeypatch.setattr(setup.os, "geteuid", lambda: os.getuid() + 1)
    with pytest.raises(setup.SetupRejected, match="^ADMIN_REQUIRED$"):
        setup.preview(**params)
    assert not Path(params["target"]).exists()


@pytest.mark.parametrize("kind", ["env-mode", "parent-mode", "parent-symlink", "override"])
def test_unsafe_layout_is_not_repaired(params, tmp_path, kind):
    if kind == "env-mode":
        (Path(params["root"]) / ".env").chmod(0o644)
    elif kind == "parent-mode":
        tmp_path.chmod(0o777)
    elif kind == "parent-symlink":
        alias = tmp_path / "alias"
        alias.symlink_to(Path(params["root"]).parent, target_is_directory=True)
        params["target"] = str(alias / "host-state")
    else:
        (Path(params["root"]) / "compose.override.yml").write_text("keep this override")
    modified = tree(params["root"])
    with pytest.raises(host.HostRejected):
        setup.preview(**params)
    assert tree(params["root"]) == modified
    assert not Path(params["target"]).exists()


@pytest.mark.parametrize("kind", ["raw-exception", "no-observation", "expired", "wrong-container", "restart", "wrong-peer"])
def test_runtime_failure_retains_reservation_without_policy_or_completion(params, kind):
    plan = setup.preview(**params)
    def observe(policy):
        if kind == "raw-exception":
            raise RuntimeError("DO_NOT_REPORT_THIS_VALUE")
        if kind == "no-observation":
            return None
        value = observed(policy)
        if kind == "expired":
            return replace(value, captured_monotonic=value.captured_monotonic - 20)
        if kind == "wrong-container":
            value.container["Id"] = "b" * 64
        if kind == "restart":
            value.container["State"]["Restarting"] = True
        if kind == "wrong-peer":
            return replace(value, process=replace(value.process, uid=0))
        return value
    with pytest.raises((host.HostRejected, setup.SetupRejected)) as error:
        apply(plan, observe=observe)
    assert "DO_NOT_REPORT_THIS_VALUE" not in str(error.value)
    root = Path(plan.target)
    assert root.is_dir() and (root / "reservation.json").is_file()
    assert not (root / "enrollment.json").exists()
    assert not (root / "prepared.json").exists()
    saved = tree(plan.target)
    with pytest.raises(setup.SetupRejected, match="^TARGET_EXISTS$"):
        apply(plan)
    assert tree(plan.target) == saved


def test_config_change_during_inspection_not_approved(params):
    plan = setup.preview(**params)
    def observe(policy):
        (Path(policy.root) / ".env").write_bytes(b"changed fixture setting")
        return observed(policy)
    with pytest.raises(host.HostRejected, match="^CONFIG_CHANGED$"):
        apply(plan, observe=observe)
    assert not (Path(plan.target) / "prepared.json").exists()


@pytest.mark.parametrize("name", sorted(setup.ROOT_FILES))
def test_interrupted_file_creation_is_not_retried_or_marked_complete(params, name, monkeypatch):
    plan = setup.preview(**params)
    write_new = setup._write_new
    def interrupted(parent, filename, data, owner_uid):
        if name == filename:
            write_new(parent, filename, b"partial", owner_uid)
            raise OSError("DO_NOT_REPORT_THIS_VALUE")
        return write_new(parent, filename, data, owner_uid)
    monkeypatch.setattr(setup, "_write_new", interrupted)
    with pytest.raises(setup.SetupRejected, match="^PREPARATION_IO$"):
        apply(plan)
    assert (Path(plan.target) / name).read_bytes() == b"partial"
    with pytest.raises((host.HostRejected, setup.SetupRejected)):
        setup.verify_preparation(plan.target, owner_uid=os.getuid())
    with pytest.raises(setup.SetupRejected, match="^TARGET_EXISTS$"):
        apply(plan)


def test_fsync_failure_never_returns_success(params, monkeypatch):
    plan = setup.preview(**params)
    monkeypatch.setattr(setup.os, "fsync", lambda *a: (_ for _ in ()).throw(OSError("private failure detail")))
    with pytest.raises(setup.SetupRejected, match="^PREPARATION_IO$"):
        apply(plan)
    assert Path(plan.target).is_dir()
    assert not (Path(plan.target) / "prepared.json").exists()


def test_journal_initialization_failure_cannot_produce_prepared_receipt(params, monkeypatch):
    plan = setup.preview(**params)
    monkeypatch.setattr(setup.Journal, "initialize", lambda *a: (_ for _ in ()).throw(OSError()))
    with pytest.raises(setup.SetupRejected, match="^PREPARATION_IO$"):
        apply(plan)
    assert not (Path(plan.target) / "prepared.json").exists()


@pytest.mark.parametrize("entry", ["enrollment.json", "peer.json", "reservation.json", "prepared.json",
                                  "journal/state.json", "journal/operation.lock"])
def test_any_preparation_file_tampering_rejected(params, entry):
    plan = setup.preview(**params)
    apply(plan)
    (Path(plan.target) / entry).write_bytes(b"tampered")
    with pytest.raises((host.HostRejected, setup.SetupRejected)):
        setup.verify_preparation(plan.target, owner_uid=os.getuid())


@pytest.mark.parametrize("kind", ["file-mode", "directory-mode", "file-hardlink", "file-symlink", "directory-symlink", "extra-file", "nonempty-client"])
def test_verify_rejects_unsafe_prepared_tree_without_repairs(params, kind):
    plan = setup.preview(**params)
    apply(plan)
    root = Path(plan.target)
    path = root / "peer.json"
    if kind == "file-mode":
        path.chmod(0o644)
    elif kind == "directory-mode":
        (root / "staging").chmod(0o755)
    elif kind in {"file-hardlink", "file-symlink"}:
        saved = root.parent / "saved-peer"
        path.rename(saved)
        if kind == "file-hardlink":
            os.link(saved, path)
        else:
            path.symlink_to(saved)
    elif kind == "directory-symlink":
        path = root / "staging"
        path.rename(root.parent / "saved-staging")
        path.symlink_to(root.parent / "saved-staging", target_is_directory=True)
    elif kind == "extra-file":
        (root / "unexpected").write_text("unexpected")
    else:
        (root / "docker-client" / "config.json").write_text("unexpected")
    with pytest.raises((host.HostRejected, setup.SetupRejected)):
        setup.verify_preparation(plan.target, owner_uid=os.getuid())


def test_repeat_preparation_never_reinitializes_journal(params):
    plan = setup.preview(**params)
    apply(plan)
    before = tree(plan.target)
    with pytest.raises(setup.SetupRejected, match="^TARGET_EXISTS$"):
        apply(plan)
    assert before == tree(plan.target)


def test_concurrent_preparation_only_one_runtime_observation_and_no_replacement(params):
    plan = setup.preview(**params)
    entered, release = threading.Event(), threading.Event()
    def observe(policy):
        entered.set()
        assert release.wait(5)
        return observed(policy)
    with ThreadPoolExecutor(max_workers=2) as pool:
        first = pool.submit(apply, plan, observe=observe)
        assert entered.wait(5)
        try:
            with pytest.raises(setup.SetupRejected, match="^TARGET_EXISTS$"):
                apply(plan)
        finally:
            release.set()
        assert first.result(timeout=5)["enrollment_prepared"]


def test_short_writes_are_completed_without_permissive_chmod(params, monkeypatch):
    plan = setup.preview(**params)
    write = setup.os.write
    monkeypatch.setattr(setup.os, "write", lambda fd, data: write(fd, data[:5]))
    assert apply(plan)["enrollment_prepared"]


def test_unsafe_nonproduction_collector_override_cannot_fall_back_to_real_docker(params):
    plan = setup.preview(**params)
    with pytest.raises(setup.SetupRejected, match="^TARGET_UNSAFE$"):
        setup.prepare(plan, approve=True, approval_sha256=plan.approval_sha256)


def test_expiry_after_journal_work_prevents_final_receipt(params, monkeypatch):
    plan = setup.preview(**params)
    real = setup._fingerprints
    observation = observed(plan.policy)
    def expire(*args, **kwargs):
        value = real(*args, **kwargs)
        object.__setattr__(observation, "captured_monotonic", observation.captured_monotonic - 20)
        return value
    monkeypatch.setattr(setup, "_fingerprints", expire)
    with pytest.raises(host.HostRejected, match="^OBSERVATION_EXPIRED$"):
        apply(plan, observe=lambda _: observation)
    assert not (Path(plan.target) / "prepared.json").exists()


def cli_args(params):
    return ["--installation-root", params["root"], "--installation-owner-uid", str(params["installation_owner_uid"]),
            "--compose-project", params["compose_project"], "--container-id", params["container_id"],
            "--image-id", params["image_id"], "--version", params["version"]]


def test_cli_default_is_preview_only(params, monkeypatch, capsys):
    plan = setup.preview(**params)
    monkeypatch.setattr(cli, "preview", lambda **kwargs: plan)
    monkeypatch.setattr(cli, "prepare", lambda *a, **k: pytest.fail("unexpected apply"))
    assert cli.main(cli_args(params)) == 0
    result = json.loads(capsys.readouterr().out)
    assert result == plan.public()
    assert not Path(plan.target).exists()


@pytest.mark.parametrize("arguments", [[], ["--apply"], ["--approve-preview", "a" * 64],
    ["--acknowledge-private-host-state"], ["--verify-prepared", "--version", "2.6.5"],
    ["--verify-prepared", "--apply"]])
def test_cli_refuses_ambiguous_arguments_before_read_or_write(arguments, monkeypatch):
    monkeypatch.setattr(cli, "preview", lambda *a, **k: pytest.fail("unexpected preview"))
    monkeypatch.setattr(cli, "verify_preparation", lambda *a, **k: pytest.fail("unexpected read"))
    with pytest.raises(SystemExit) as error:
        cli.main(arguments)
    assert error.value.code == 2


def test_cli_apply_recomputes_plan_and_passes_both_consents(params, monkeypatch, capsys):
    plan = setup.preview(**params)
    monkeypatch.setattr(cli, "preview", lambda **kwargs: plan)
    def prepare(p, **kwargs):
        assert kwargs == {"approve": True, "approval_sha256": plan.approval_sha256}
        return apply(p)
    monkeypatch.setattr(cli, "prepare", prepare)
    assert cli.main([*cli_args(params), "--apply", "--approve-preview", plan.approval_sha256,
                     "--acknowledge-private-host-state"]) == 0
    assert json.loads(capsys.readouterr().out)["enrollment_prepared"]


def test_cli_errors_do_not_print_paths_secrets_or_tracebacks(params, monkeypatch, capsys):
    def fail(**kwargs):
        raise RuntimeError("DO_NOT_REPORT_THIS_VALUE " + params["root"])
    monkeypatch.setattr(cli, "preview", fail)
    assert cli.main(cli_args(params)) == 2
    output = capsys.readouterr()
    assert output.err == ""
    assert "DO_NOT_REPORT_THIS_VALUE" not in output.out and params["root"] not in output.out
    assert json.loads(output.out)["code"] == "PREPARATION_FAILED"


def test_cli_verify_is_read_only_and_never_docker(params, monkeypatch, capsys):
    plan = setup.preview(**params)
    expected = apply(plan)
    before = tree(plan.target)
    monkeypatch.setattr(cli, "verify_preparation", lambda target: setup.verify_preparation(plan.target, owner_uid=os.getuid()))
    assert cli.main(["--verify-prepared"]) == 0
    assert json.loads(capsys.readouterr().out) == expected
    assert tree(plan.target) == before


@pytest.mark.parametrize("error", [KeyboardInterrupt, SystemExit])
def test_operator_interruption_leaves_explicit_incomplete_reservation(params, error):
    plan = setup.preview(**params)
    def interrupted(_):
        raise error()
    with pytest.raises(error):
        apply(plan, observe=interrupted)
    assert (Path(plan.target) / "reservation.json").is_file()
    assert not (Path(plan.target) / "prepared.json").exists()
    with pytest.raises(setup.SetupRejected, match="^TARGET_EXISTS$"):
        setup.preview(**params)


def test_changed_parent_identity_invalidates_preview(params):
    plan = setup.preview(**params)
    forged = replace(plan, parent_identity=(*plan.parent_identity[:-1], 0))
    with pytest.raises(setup.SetupRejected, match="^PREVIEW_CHANGED$"):
        apply(forged)
    assert not Path(plan.target).exists()


def test_changed_preview_target_requires_new_approval(params):
    plan = setup.preview(**params)
    other = replace(plan, target=plan.target + "-other")
    with pytest.raises(setup.SetupRejected, match="^PREVIEW_CHANGED$"):
        setup.prepare(other, approve=True, approval_sha256=plan.approval_sha256)
    assert not Path(other.target).exists()


@pytest.mark.parametrize("kind", ["schema-bool", "schema-float", "extra-key", "missing-key", "extra-hash", "duplicate-key"])
def test_completion_receipt_schema_is_exact(params, kind):
    plan = setup.preview(**params)
    apply(plan)
    path = Path(plan.target) / "prepared.json"
    payload = json.loads(path.read_text())
    if kind == "schema-bool":
        payload["schema"] = True
    elif kind == "schema-float":
        payload["schema"] = 1.0
    elif kind == "extra-key":
        payload["installation_available"] = True
    elif kind == "missing-key":
        del payload["state"]
    elif kind == "extra-hash":
        payload["files"]["unexpected"] = "a" * 64
    if kind == "duplicate-key":
        path.write_text('{"schema":1,"schema":1}')
    else:
        path.write_text(json.dumps(payload))
    with pytest.raises((host.HostRejected, setup.SetupRejected)):
        setup.verify_preparation(plan.target, owner_uid=os.getuid())


def test_unused_verifier_rejects_journal_jobs_even_with_updated_file_hash(params):
    import hashlib
    from basswiesn.update_helper.protocol import Request
    plan = setup.preview(**params)
    apply(plan)
    root = Path(plan.target)
    journal = setup.Journal(root / "journal", owner_uid=os.getuid())
    try:
        with journal.exclusive():
            # Reuse the journal's real creation contract, not a fake corrupt file.
            request = Request("install", "12345678-1234-4234-8234-123456789abc", "3.0.0", "2.6.5")
            journal.begin(request)
    finally:
        journal.close()
    receipt = json.loads((root / "prepared.json").read_text())
    receipt["files"]["journal/state.json"] = hashlib.sha256((root / "journal/state.json").read_bytes()).hexdigest()
    (root / "prepared.json").write_text(json.dumps(receipt))
    with pytest.raises(setup.SetupRejected, match="^PREPARATION_INVALID$"):
        setup.verify_preparation(plan.target, owner_uid=os.getuid())


def test_file_write_race_refuses_existing_file_without_overwrite(params, monkeypatch):
    plan = setup.preview(**params)
    original = setup._write_new
    def race(parent, name, data, owner_uid):
        if name == "peer.json":
            original(parent, name, b"preexisting race sentinel", owner_uid)
        return original(parent, name, data, owner_uid)
    monkeypatch.setattr(setup, "_write_new", race)
    with pytest.raises(setup.SetupRejected, match="^PREPARATION_IO$"):
        apply(plan)
    assert (Path(plan.target) / "peer.json").read_bytes() == b"preexisting race sentinel"
    assert not (Path(plan.target) / "prepared.json").exists()


def test_failed_zero_byte_write_stops_without_completion(params, monkeypatch):
    plan = setup.preview(**params)
    monkeypatch.setattr(setup.os, "write", lambda *args: 0)
    with pytest.raises(setup.SetupRejected, match="^PREPARATION_IO$"):
        apply(plan)
    assert not (Path(plan.target) / "prepared.json").exists()


def test_directory_context_preserves_body_io_error_and_closes_descriptor(tmp_path):
    with pytest.raises(OSError, match="^fixture I/O failure$"):
        with host.directory(str(tmp_path), owners={0, os.getuid()}) as fd:
            captured = fd
            raise OSError("fixture I/O failure")
    with pytest.raises(OSError):
        os.fstat(captured)
