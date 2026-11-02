"""Actual Linux atomic rename/copy/SQLite; synthetic files, no daemon/network."""
from dataclasses import replace
import hashlib
import json
import os
from pathlib import Path
import sqlite3

import pytest

from basswiesn.update_helper import installation_cutover as cutover
from basswiesn.update_helper import artifact_staging as staging
from tests.test_update_installation_snapshot_300 import context, capture, tree_signature  # noqa: F401
from tests.test_update_host_preflight_300 import enrolled  # noqa: F401
from test_update_artifact_staging_300 import OfficialFixture
from test_release_artifact_300 import VERSION, tar_bytes, content


pytestmark = pytest.mark.unit


@pytest.fixture
def prepared(context, tmp_path):
    policy, backups, _ = context
    ignore = (cutover.BUILD_EXCLUSION + "\n").encode()
    (Path(policy.root) / ".dockerignore").write_bytes(ignore)
    policy = replace(policy, file_sha256={**policy.file_sha256, ".dockerignore": hashlib.sha256(ignore).hexdigest()})
    context = (policy, *context[1:])
    source = tmp_path / "cutover-staging"
    source.mkdir(mode=0o700)
    receipt = capture(context)
    fixture = OfficialFixture(tar_bytes(content(extra={"docs/new-guide.md": b"synthetic new release\n", ".dockerignore": ignore})))
    staging.stage_release(receipt.request_id, VERSION, staging_root=str(source), owner_uid=os.getuid(), fetch=fixture)
    original = tree_signature(Path(policy.root))
    result = cutover.prepare(policy, receipt, VERSION, assert_stopped=lambda _: True,
                             staging_root=str(source), backup_root=str(backups), owner_uid=os.getuid())
    return policy, result, original, receipt, source


def perform(prepared, **kwargs):
    return cutover.exchange(prepared[1], prepared[0], assert_stopped=kwargs.pop("assert_stopped", lambda _: True),
                            owner_uid=os.getuid(), **kwargs)


def original_signature(root):
    # The operation reservation itself is intentionally retained. Parent mtime
    # changes during an atomic exchange and is not application configuration.
    return {p: v for p, v in tree_signature(root).items() if p != "." and not p.startswith(cutover.PREFIX)}


def test_actual_cutover_then_schema_mutation_rolls_back_complete_old_tree(prepared):
    policy, receipt, before, _, _ = prepared
    root = Path(policy.root)
    assert original_signature(root) == {p: v for p, v in before.items() if p != "."}
    result = perform(prepared)
    assert result == {"state": "CANDIDATE_FILES_INSTALLED", "originals_retained": True, "application_health_verified": False}
    assert VERSION in (root / "basswiesn/__init__.py").read_text()
    assert (root / "docs/new-guide.md").read_bytes() == b"synthetic new release\n"
    with sqlite3.connect(root / "data/app.db") as db:
        db.execute("ALTER TABLE fixture ADD COLUMN newer TEXT")
        db.execute("UPDATE fixture SET value='candidate changed', newer='only candidate'")
    (root / "data/new-file").write_text("candidate private runtime state")
    result = perform(prepared, rollback=True)
    assert result["state"] == "ORIGINAL_FILES_RESTORED"
    assert original_signature(root) == {p: v for p, v in before.items() if p != "."}
    retained = root / (cutover.PREFIX + receipt.request_id) / "pending"
    assert (retained / "data/new-file").read_text() == "candidate private runtime state"
    with sqlite3.connect(root / "data/app.db") as db:
        assert db.execute("SELECT * FROM fixture").fetchall() == [(1, "old schema")]
        assert db.execute("PRAGMA quick_check").fetchall() == [("ok",)]
    # A completed rollback can never silently reactivate the failed candidate.
    with pytest.raises(cutover.CutoverRejected, match="^CUTOVER_EXISTS$"):
        perform(prepared)


@pytest.mark.parametrize("after", [False, True])
@pytest.mark.parametrize("step", [1, 3, 6])
def test_interruption_on_either_side_of_atomic_exchange_is_reversible(prepared, monkeypatch, after, step):
    real = cutover._rename
    calls = 0
    def interrupted(*args, **kwargs):
        nonlocal calls
        calls += 1
        if calls == step and not after:
            raise KeyboardInterrupt()
        real(*args, **kwargs)
        if calls == step and after:
            raise KeyboardInterrupt()
    monkeypatch.setattr(cutover, "_rename", interrupted)
    with pytest.raises(KeyboardInterrupt):
        perform(prepared)
    assert not (Path(prepared[0].root) / (cutover.PREFIX + prepared[1].request_id) / "applied.json").exists()
    monkeypatch.setattr(cutover, "_rename", real)
    assert perform(prepared, rollback=True)["state"] == "ORIGINAL_FILES_RESTORED"
    assert original_signature(Path(prepared[0].root)) == {p: v for p, v in prepared[2].items() if p != "."}


@pytest.mark.parametrize("kind", ["original-bytes", "candidate-bytes", "symlink", "inode", "plan", "hash"])
def test_unsafe_or_changed_tree_fails_before_any_live_exchange(prepared, monkeypatch, kind):
    policy, receipt, _, _, _ = prepared
    root = Path(policy.root)
    job = root / (cutover.PREFIX + receipt.request_id)
    if kind == "original-bytes":
        (root / "data/settings.json").write_text("external change")
    elif kind == "candidate-bytes":
        (job / "pending/data/settings.json").write_text("external change")
    elif kind == "symlink":
        (root / "data").rename(root / "retained-user-data")
        (root / "data").symlink_to("retained-user-data", target_is_directory=True)
    elif kind == "inode":
        (root / "data").rename(root / "retained-user-data")
        (root / "data").mkdir()
    elif kind == "plan":
        (job / "plan.json").write_text("{}")
    else:
        prepared = (policy, replace(receipt, plan_sha256="0" * 64), *prepared[2:])
    def forbidden(*args, **kwargs):
        pytest.fail("changed trees must fail BEFORE any swap")
    monkeypatch.setattr(cutover, "_rename", forbidden)
    with pytest.raises(cutover.CutoverRejected, match="^CUTOVER_CHANGED$"):
        perform(prepared)


def test_rollback_refuses_modified_preserved_original(prepared):
    perform(prepared)
    root = Path(prepared[0].root)
    original = root / (cutover.PREFIX + prepared[1].request_id) / "pending/data/settings.json"
    original.write_text("changed outside updater")
    with pytest.raises(cutover.CutoverRejected, match="^CUTOVER_CHANGED$"):
        perform(prepared, rollback=True)
    assert VERSION in (root / "basswiesn/__init__.py").read_text()


def test_stop_proof_must_be_true_and_rechecked_between_swaps(prepared):
    calls = []
    def stopped(_):
        calls.append(True)
        return len(calls) < 3
    from basswiesn.update_helper.installation_snapshot import SnapshotRejected
    with pytest.raises(SnapshotRejected, match="^WRITERS_NOT_STOPPED$"):
        perform(prepared, assert_stopped=stopped)
    assert len(calls) == 3
    perform(prepared, rollback=True)
    assert original_signature(Path(prepared[0].root)) == {p: v for p, v in prepared[2].items() if p != "."}


def test_prepare_never_reuses_reservation(prepared, context):
    policy, _, _, receipt, source = prepared
    with pytest.raises(cutover.CutoverRejected, match="^CUTOVER_EXISTS$"):
        cutover.prepare(policy, receipt, VERSION, assert_stopped=lambda _: True,
            staging_root=str(source), backup_root=str(context[1]), owner_uid=os.getuid())


def test_prepared_plan_binds_identities_content_and_snapshot(prepared):
    policy, receipt, _, snapshot, _ = prepared
    job = Path(policy.root) / (cutover.PREFIX + receipt.request_id)
    raw = (job / "plan.json").read_bytes()
    plan = json.loads(raw)
    assert hashlib.sha256(raw).hexdigest() == receipt.plan_sha256
    assert plan["snapshot_sha256"] == snapshot.manifest_sha256
    assert set(plan["entries"]) >= {"data", ".env", "basswiesn", "Dockerfile", "docker-compose.yml"}
    assert "candidate private runtime state" not in raw.decode()
    assert str(policy.root) not in repr(receipt)
    for name in ("plan.json", "reservation.json"):
        assert (job / name).stat().st_mode & 0o7777 == 0o600
    assert job.stat().st_mode & 0o7777 == 0o700


@pytest.mark.parametrize("rules,override,valid", [("/.basswiesn-update-*\n",False,True),
    ("data\n/.basswiesn-update-*\n# comment\n",False,True),
    ("data\n",False,False), ("/.basswiesn-update-*\n!**\n",False,False),
    ("/.basswiesn-update-*\n",True,False)])
def test_both_generations_must_exclude_private_retained_state_from_build(tmp_path, rules, override, valid):
    (tmp_path / ".dockerignore").write_text(rules)
    (tmp_path / ".dockerignore").chmod(0o644)
    if override:
        (tmp_path / "Dockerfile.dockerignore").write_text("!**\n")
    fd = os.open(tmp_path, os.O_RDONLY | os.O_DIRECTORY)
    try:
        if valid:
            cutover._build_exclusion(fd,{os.getuid()})
        else:
            with pytest.raises(cutover.CutoverRejected, match="^CUTOVER_UNSUPPORTED$"):
                cutover._build_exclusion(fd,{os.getuid()})
    finally:
        os.close(fd)
