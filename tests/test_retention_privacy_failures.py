"""Failure injection for bounded retention and artwork refresh ownership."""

import asyncio
from datetime import UTC, datetime, timedelta
import os

import httpx
import pytest

from basswiesn.app import db as app_db
from basswiesn.app.models import ArtworkCacheEntry, DiagnosticEvent, DeviceInteraction, ProviderHealthState
from basswiesn.app.services import artwork, research_state_retention as retention
from basswiesn.app.services.artwork_cache_lock import ArtworkCacheBusy, artwork_cache_lock
from basswiesn.app.services.network_security import UrlValidation


pytestmark = pytest.mark.unit
NOW = datetime(2026, 9, 30, tzinfo=UTC)
OLD = NOW - timedelta(days=40)


def _seed(db, tmp_path, *, path=None):
    root = tmp_path / "cache"
    root.mkdir(exist_ok=True)
    cached = path or (root / "expired.png")
    if path is None:
        cached.write_bytes(b"expired-fixture")
    row = ArtworkCacheEntry(cache_key="expired-fixture", cached_path=str(cached), expires_at=OLD)
    db.add(row)
    db.commit()
    return root, cached, row


def test_initial_commit_failure_preserves_all_rows_and_artwork(tmp_path, monkeypatch):
    with app_db.SessionLocal() as db:
        root, path, row = _seed(db, tmp_path)
        db.add(DiagnosticEvent(event_id="old-event", occurred_at=OLD, code="OLD"))
        db.commit()

        def fail():
            raise RuntimeError("commit failed")

        monkeypatch.setattr(db, "commit", fail)
        with pytest.raises(RuntimeError, match="commit failed"):
            retention.apply_research_retention(db, now=NOW, artwork_cache_root=root)
        assert path.read_bytes() == b"expired-fixture"
        assert db.query(DiagnosticEvent).count() == 1
        assert db.query(ArtworkCacheEntry).one().failure_status is None
        with artwork_cache_lock(root):
            pass  # Exceptions must release process-shared ownership.


def test_commit_failure_after_unlink_leaves_durable_tombstone_for_retry(tmp_path, monkeypatch):
    with app_db.SessionLocal() as db:
        root, path, row = _seed(db, tmp_path)
        original = db.commit
        count = 0

        def fail_second():
            nonlocal count
            count += 1
            if count == 2:
                raise RuntimeError("post-unlink commit failed")
            original()

        monkeypatch.setattr(db, "commit", fail_second)
        with pytest.raises(RuntimeError, match="post-unlink"):
            retention.apply_research_retention(db, now=NOW, artwork_cache_root=root)
        assert not path.exists()
        assert db.query(ArtworkCacheEntry).one().failure_status == "RETENTION_PENDING"
        monkeypatch.setattr(db, "commit", original)
        result = retention.apply_research_retention(db, now=NOW, artwork_cache_root=root)
        assert result["artwork_files_missing"] == 1
        assert result["artwork_entries"] == 1
        assert db.query(ArtworkCacheEntry).count() == 0


def test_unlink_failure_keeps_tombstone_and_later_retry_completes(tmp_path, monkeypatch):
    with app_db.SessionLocal() as db:
        root, path, row = _seed(db, tmp_path)
        original = retention._unlink_cached_file

        def denied(*args):
            raise PermissionError("synthetic permission failure")

        monkeypatch.setattr(retention, "_unlink_cached_file", denied)
        result = retention.apply_research_retention(db, now=NOW, artwork_cache_root=root)
        assert result["artwork_file_errors"] == 1
        assert result["artwork_entries"] == 0
        assert path.exists()
        assert db.query(ArtworkCacheEntry).one().failure_status == "RETENTION_PENDING"
        monkeypatch.setattr(retention, "_unlink_cached_file", original)
        result = retention.apply_research_retention(db, now=NOW, artwork_cache_root=root)
        assert result["artwork_entries"] == 1
        assert not path.exists()


def test_busy_owner_preserves_artwork_and_can_still_purge_history(tmp_path):
    with app_db.SessionLocal() as db:
        root, path, row = _seed(db, tmp_path)
        db.add(DiagnosticEvent(event_id="old-event", occurred_at=OLD, code="OLD"))
        db.commit()
        with artwork_cache_lock(root):
            result = retention.apply_research_retention(db, now=NOW, artwork_cache_root=root)
        assert result["diagnostic_events"] == 1
        assert result["artwork_entries"] == 0
        assert result["artwork_file_errors"] == 1
        assert path.exists()
        assert db.query(ArtworkCacheEntry).one().failure_status is None
        assert retention.apply_research_retention(db, now=NOW, artwork_cache_root=root)["artwork_entries"] == 1


def test_cache_lock_excludes_independent_descriptors_and_never_removes_leaf(tmp_path):
    root = tmp_path / "cache"
    with artwork_cache_lock(root):
        with pytest.raises(ArtworkCacheBusy):
            with artwork_cache_lock(root):
                pass
    assert (root / ".retention.lock").is_file()
    with artwork_cache_lock(root):
        pass


def test_expired_row_cannot_unlink_the_currently_held_lock_inode(tmp_path):
    root = tmp_path / "cache"
    with artwork_cache_lock(root):
        pass
    lock = root / ".retention.lock"
    inode = lock.stat().st_ino
    with app_db.SessionLocal() as db:
        db.add(ArtworkCacheEntry(cache_key="hostile-cache-row", cached_path=str(lock), expires_at=OLD))
        db.commit()
        result = retention.apply_research_retention(db, now=NOW, artwork_cache_root=root)
        assert result["artwork_files_blocked"] == 1
        assert result["artwork_entries"] == 0
    assert lock.stat().st_ino == inode
    with artwork_cache_lock(root):
        with pytest.raises(ArtworkCacheBusy):
            with artwork_cache_lock(root):
                pass


@pytest.mark.parametrize("kind", ["symlink", "hardlink", "fifo", "permissions"])
def test_cache_lock_fails_closed_on_unsafe_leaf_without_changing_it(tmp_path, kind):
    root = tmp_path / "cache"
    root.mkdir()
    lock = root / ".retention.lock"
    source = tmp_path / "outside"
    source.write_bytes(b"do-not-touch")
    if kind == "symlink":
        lock.symlink_to(source)
    elif kind == "hardlink":
        os.link(source, lock)
    elif kind == "fifo":
        os.mkfifo(lock, mode=0o600)
    else:
        lock.write_bytes(b"private-lock")
        lock.chmod(0o644)
    with pytest.raises(ArtworkCacheBusy):
        with artwork_cache_lock(root):
            pytest.fail("unsafe lock was acquired")
    assert source.read_bytes() == b"do-not-touch"


def test_ancestor_symlink_is_never_followed_for_lock_or_deletion(tmp_path):
    actual = tmp_path / "actual"
    actual.mkdir()
    (actual / "cache").mkdir()
    path = actual / "cache" / "keep.png"
    path.write_bytes(b"outside-fixture")
    link = tmp_path / "linked"
    link.symlink_to(actual, target_is_directory=True)
    with pytest.raises(ArtworkCacheBusy):
        with artwork_cache_lock(link / "cache"):
            pass
    with pytest.raises(retention.UnsafeArtworkPath):
        retention._unlink_cached_file(link / "cache", str(link / "cache" / "keep.png"))
    assert path.read_bytes() == b"outside-fixture"


def test_directory_substitution_cannot_redirect_unlink_outside_cache(tmp_path, monkeypatch):
    root = tmp_path / "cache"
    inside = root / "nested"
    inside.mkdir(parents=True)
    (inside / "leaf.png").write_bytes(b"expired")
    outside = tmp_path / "outside"
    outside.mkdir()
    (outside / "leaf.png").write_bytes(b"preserved")
    original = os.stat
    swapped = False

    def swap_before_stat(path, *args, **kwargs):
        nonlocal swapped
        if path == "leaf.png" and kwargs.get("dir_fd") is not None and not swapped:
            swapped = True
            inside.rename(root / "pinned-original")
            inside.symlink_to(outside, target_is_directory=True)
        return original(path, *args, **kwargs)

    monkeypatch.setattr(retention.os, "stat", swap_before_stat)
    assert retention._unlink_cached_file(root, str(inside / "leaf.png")) is True
    assert (outside / "leaf.png").read_bytes() == b"preserved"
    assert not (root / "pinned-original" / "leaf.png").exists()


def _fetch(db, media_dir):
    async def handler(request):
        return httpx.Response(200, headers={"content-type": "image/png"}, content=b"fresh-fixture")

    return asyncio.run(artwork.cache_artwork(
        db, artwork.choose_artwork(image_url="https://example.test/fresh.png"), media_dir=media_dir,
        now=NOW, transport=httpx.MockTransport(handler),
        validator=lambda url: UrlValidation(True, "ok", hostname="example.test",
                                            addresses=("203.0.113.5",), scheme="https", port=443),
    ))


def test_refresh_attempt_between_retention_check_and_unlink_is_busy(tmp_path, monkeypatch):
    with app_db.SessionLocal() as db:
        first = _fetch(db, tmp_path)
        assert first.status == "FETCHED"
        row = db.query(ArtworkCacheEntry).one()
        row.expires_at = OLD
        db.commit()
        root = tmp_path / "artwork-cache"
        original = retention._unlink_cached_file
        attempted = []

        def attempt_refresh(cache_root, path):
            with app_db.SessionLocal() as refresh_db:
                attempted.append(_fetch(refresh_db, tmp_path))
            return original(cache_root, path)

        monkeypatch.setattr(retention, "_unlink_cached_file", attempt_refresh)
        result = retention.apply_research_retention(db, now=NOW, artwork_cache_root=root)
        assert attempted[0].failure_status == "CACHE_BUSY"
        assert result["artwork_entries"] == 1
        assert result["artwork_files_deleted"] == 1
        assert db.query(ArtworkCacheEntry).count() == 0
        db.expunge_all()
        assert _fetch(db, tmp_path).status == "FETCHED"


def test_cleanup_during_producer_publication_cannot_remove_new_file(tmp_path, monkeypatch):
    with app_db.SessionLocal() as db:
        original = artwork._upsert_entry
        attempted = []

        def attempt_cleanup(session, key, **values):
            with app_db.SessionLocal() as cleanup_db:
                attempted.append(retention.apply_research_retention(
                    cleanup_db, now=NOW, artwork_cache_root=tmp_path / "artwork-cache",
                ))
            return original(session, key, **values)

        monkeypatch.setattr(artwork, "_upsert_entry", attempt_cleanup)
        assert _fetch(db, tmp_path).status == "FETCHED"
        assert attempted[0]["artwork_file_errors"] == 1
        assert db.query(ArtworkCacheEntry).one().failure_status is None


def test_retention_batch_and_preview_preserve_current_snapshots(tmp_path):
    with app_db.SessionLocal() as db:
        root, path, row = _seed(db, tmp_path)
        db.add_all([DiagnosticEvent(event_id=f"old-{number}", occurred_at=OLD, code="OLD") for number in range(5)])
        db.add(ProviderHealthState(device_id="fixture", provider_id="fixture", state="HEALTHY"))
        db.commit()
        plan = retention.research_retention_plan(db, now=NOW, batch_size=2)
        assert plan["eligible_total"] == 6
        assert path.exists()
        assert db.query(DiagnosticEvent).count() == 5
        result = retention.apply_research_retention(db, now=NOW, batch_size=2, artwork_cache_root=root)
        assert result["deleted_total"] == 2
        assert db.query(DiagnosticEvent).count() == 3
        assert db.query(ProviderHealthState).count() == 1
        assert path.exists()


def test_only_completed_known_read_interactions_age_out_with_shared_budget(tmp_path):
    with app_db.SessionLocal() as db:
        candidates = [
            ("old-read", "GET", "/info", OLD, "ok"),
            ("old-head", "head", "/info", OLD, "error"),
            ("fresh", "GET", "/info", NOW, "ok"),
            ("unfinished", "GET", "/info", None, ""),
            ("unknown-state", "GET", "/info", OLD, "pending"),
            ("write", "POST", "/volume", OLD, "ok"),
            ("unknown-get", "GET", "/reboot", OLD, "ok"),
        ]
        for name, method, endpoint, ended, result in candidates:
            db.add(DeviceInteraction(event_id=name, method=method, endpoint=endpoint,
                                     started_at=OLD, ended_at=ended, result=result))
        db.add(DiagnosticEvent(event_id="old-event", occurred_at=OLD, code="OLD"))
        db.commit()
        plan = retention.research_retention_plan(db, now=NOW, interaction_days=14, batch_size=2)
        assert plan["device_interactions"] == 2
        assert plan["eligible_total"] == 3
        result = retention.apply_research_retention(db, now=NOW, interaction_days=14, batch_size=2,
                                                    artwork_cache_root=tmp_path / "cache")
        assert result["deleted_total"] == 2
        assert result["diagnostic_events"] == result["device_interactions"] == 1
        assert {row.event_id for row in db.query(DeviceInteraction)} == {
            "old-head", "fresh", "unfinished", "unknown-state", "write", "unknown-get",
        }
        assert retention.research_retention_plan(db, now=NOW, interaction_days=90)["device_interactions"] == 0


def test_interaction_retention_rolls_back_when_initial_commit_fails(tmp_path, monkeypatch):
    with app_db.SessionLocal() as db:
        db.add(DeviceInteraction(event_id="old-read", method="GET", endpoint="/info",
                                 started_at=OLD, ended_at=OLD, result="ok"))
        db.commit()

        def fail():
            raise RuntimeError("commit denied")

        monkeypatch.setattr(db, "commit", fail)
        with pytest.raises(RuntimeError, match="commit denied"):
            retention.apply_research_retention(db, now=NOW, artwork_cache_root=tmp_path / "cache")
        assert db.query(DeviceInteraction).one().event_id == "old-read"


def test_research_runtime_uses_configured_interaction_retention_days(tmp_path, monkeypatch):
    from basswiesn.app.services import research_runtime

    settings = research_runtime.get_settings().model_copy(update={
        "data_dir": tmp_path, "interaction_retention_days": 60,
    })
    (tmp_path / "media").mkdir()
    monkeypatch.setattr(research_runtime, "get_settings", lambda: settings)
    monkeypatch.setattr(retention, "get_settings", lambda: settings)
    with app_db.SessionLocal() as db:
        db.add(DeviceInteraction(event_id="runtime-old-read", method="GET", endpoint="/info",
                                 started_at=OLD, ended_at=OLD, result="ok"))
        db.commit()
    runtime = research_runtime.ResearchRuntime(app_db.SessionLocal, clock=lambda: NOW)
    assert runtime.run_retention_once()["device_interactions"] == 0
    settings = settings.model_copy(update={"interaction_retention_days": 14})
    assert runtime.run_retention_once()["device_interactions"] == 1
