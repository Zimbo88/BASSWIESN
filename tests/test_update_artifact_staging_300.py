"""Only synthetic release responses and private tmp directories. No network."""
from copy import deepcopy
import gzip
import hashlib
import json
import os
from pathlib import Path
import stat

import pytest

from basswiesn.update_helper import artifact_staging as staging
from basswiesn.update_helper import official_transport as transport
from basswiesn.update_helper.host_preflight import HostRejected
from basswiesn.update_helper.protocol import UpdateRejected
from test_release_artifact_300 import content, tar_bytes, VERSION, NAME


pytestmark = pytest.mark.unit
JOB = "12345678-1234-4234-8234-123456789abc"


def digest(data):
    return hashlib.sha256(data).hexdigest()


class OfficialFixture:
    def __init__(self, package=None):
        self.package = gzip.compress(tar_bytes() if package is None else package, mtime=0)
        self.sums = f"{digest(self.package)}  {NAME}\n".encode()
        self.calls = []
        self.metadata = {"id": 100, "tag_name": "v" + VERSION, "draft": False, "prerelease": False,
            "html_url": staging.REPOSITORY + "/releases/tag/v" + VERSION,
            "published_at": "2026-09-01T12:00:00Z", "body": "DO_NOT_SAVE_RAW_RELEASE_BODY",
            "assets": [self.asset(200, "SHA256SUMS", self.sums), self.asset(201, NAME, self.package)]}

    def asset(self, identity, name, data):
        return {"id": identity, "name": name, "size": len(data), "state": "uploaded",
                "url": transport.API + "assets/" + str(identity), "digest": "sha256:" + digest(data),
                "browser_download_url": staging.REPOSITORY + "/releases/download/v" + VERSION + "/" + name}

    def __call__(self, url, output, *, maximum, deadline):
        transport.initial_url(url)
        self.calls.append(url)
        payload = {transport.API + "tags/v" + VERSION: json.dumps(self.metadata).encode(),
                   transport.API + "assets/200": self.sums,
                   transport.API + "assets/201": self.package}[url]
        assert len(payload) <= maximum
        for offset in range(0, len(payload), 137):
            output.write(payload[offset:offset + 137])
        return len(payload)


@pytest.fixture
def area(tmp_path):
    root = tmp_path / "private-staging"
    root.mkdir(mode=0o700)
    return root


def stage(area, fetch=None, **kwargs):
    return staging.stage_release(JOB, VERSION, staging_root=str(area), owner_uid=os.getuid(),
                                 fetch=fetch if fetch is not None else OfficialFixture(), **kwargs)


def verify(area):
    return staging.verify_staged_release(JOB, VERSION, staging_root=str(area), owner_uid=os.getuid())


def test_full_acquire_extract_readback_without_tar_extract_or_code_execution(area, monkeypatch):
    import tarfile
    def forbidden(*a, **k):
        pytest.fail("never invoke generic extraction or source execution")
    monkeypatch.setattr(tarfile.TarFile, "extractall", forbidden)
    monkeypatch.setattr(tarfile.TarFile, "extract", forbidden)
    monkeypatch.setattr(os, "system", forbidden)
    fixture = OfficialFixture(tar_bytes(content(extra={"never-executed.py": b'raise RuntimeError("do not execute")\n'})))
    result = stage(area, fixture)
    assert result["state"] == "SOURCE_STAGED" and result["integrity_verified"]
    assert not result["installation_performed"] and not result["installation_available"]
    assert not result["signature_verified"]
    assert fixture.calls == [transport.API + "tags/v" + VERSION, transport.API + "assets/200",
                             transport.API + "assets/201", transport.API + "tags/v" + VERSION]
    assert result == verify(area)
    assert result["archive_sha256"] == digest(fixture.package)
    job = area / JOB
    for path in (job, *job.rglob("*")):
        assert not path.is_symlink()
        assert path.stat().st_uid == os.getuid()
        assert stat.S_IMODE(path.stat().st_mode) == (0o700 if path.is_dir() or path.name == "install.sh" else 0o600)
    assert set((job / "source/data").iterdir()) == set()
    assert (job / "source/.env.example").exists() and not (job / "source/.env").exists()
    for name in ("reservation.json", "ready.json"):
        assert "DO_NOT_SAVE_RAW_RELEASE_BODY" not in (job / name).read_text()
    assert str(area) not in json.dumps(result)


@pytest.mark.parametrize("version", ["v3.0.0", "03.0.0", "3.0.0-rc1", "3.0.0/anything", None])
def test_invalid_version_rejected_before_reservation(area, version):
    with pytest.raises(UpdateRejected):
        staging.stage_release(JOB, version, staging_root=str(area), owner_uid=os.getuid())
    assert not list(area.iterdir())


@pytest.mark.parametrize("identity", ["../other", "not-a-uuid", "12345678-1234-1234-8234-123456789abc", None])
def test_invalid_job_cannot_choose_path(area, identity):
    with pytest.raises(UpdateRejected):
        staging.stage_release(identity, VERSION, staging_root=str(area), owner_uid=os.getuid())
    assert not list(area.iterdir())


@pytest.mark.parametrize("change", [
    {"id": True}, {"id": 0}, {"tag_name": "v2.6.5"}, {"draft": True}, {"prerelease": True},
    {"html_url": "https://example.invalid/release"}, {"published_at": None},
    {"published_at": "2026-09-01"}, {"published_at": "2026-02-30T12:00:00Z"},
    {"published_at": "1900-01-01T00:00:00Z"}, {"assets": []},
])
def test_release_identity_rejected_without_asset_fetch(area, change):
    fixture = OfficialFixture()
    fixture.metadata.update(change)
    with pytest.raises(staging.StagingRejected, match="^RELEASE_INVALID$"):
        stage(area, fixture)
    assert fixture.calls == [transport.API + "tags/v" + VERSION]
    assert not (area / JOB / "ready.json").exists()


@pytest.mark.parametrize("change", [
    {"id": True}, {"id": -1}, {"size": True}, {"size": 0}, {"size": 4097},
    {"state": "new"}, {"url": "https://example.invalid/asset"},
    {"browser_download_url": "https://example.invalid/archive"},
    {"digest": "md5:bad"}, {"digest": "sha256:" + "A" * 64}, {"digest": 123},
])
def test_asset_metadata_is_bound_and_bounded(area, change):
    fixture = OfficialFixture()
    fixture.metadata["assets"][0].update(change)
    with pytest.raises(staging.StagingRejected, match="^RELEASE_INVALID$"):
        stage(area, fixture)
    assert len(fixture.calls) == 1


@pytest.mark.parametrize("kind", ["duplicate-name", "duplicate-id", "missing", "too-many", "non-object", "duplicate-json", "oversize", "invalid-json"])
def test_ambiguous_or_oversized_release_metadata(area, kind):
    fixture = OfficialFixture()
    if kind == "duplicate-name":
        fixture.metadata["assets"].append(deepcopy(fixture.metadata["assets"][0]))
    elif kind == "duplicate-id":
        fixture.metadata["assets"][1]["id"] = 200
    elif kind == "missing":
        fixture.metadata["assets"].pop()
    elif kind == "too-many":
        fixture.metadata["assets"] *= 51
    elif kind == "non-object":
        fixture.metadata["assets"].append(None)
    if kind in {"duplicate-json", "oversize", "invalid-json"}:
        payload = {"duplicate-json": b'{"id":1,"id":2}', "oversize": b"x" * (staging.MAX_RELEASE_BYTES + 1),
                   "invalid-json": b"no json"}[kind]
        with pytest.raises(staging.StagingRejected, match="^RELEASE_INVALID$"):
            staging.parse_release(payload, VERSION)
    else:
        with pytest.raises(staging.StagingRejected, match="^RELEASE_INVALID$"):
            stage(area, fixture)


def test_missing_optional_api_digest_still_requires_external_checksum(area):
    fixture = OfficialFixture()
    for item in fixture.metadata["assets"]:
        del item["digest"]
    assert stage(area, fixture)["integrity_verified"]


@pytest.mark.parametrize("asset", [0, 1])
def test_api_digest_mismatch_refuses_extraction(area, asset):
    fixture = OfficialFixture()
    fixture.metadata["assets"][asset]["digest"] = "sha256:" + "0" * 64
    with pytest.raises(staging.StagingRejected, match="^ASSET_MISMATCH$"):
        stage(area, fixture)
    assert not (area / JOB / "source").exists()
    assert not (area / JOB / "ready.json").exists()


def test_external_checksum_mismatch_is_detected_before_source_directory(area):
    fixture = OfficialFixture()
    fixture.sums = f'{"0" * 64}  {NAME}\n'.encode()
    fixture.metadata["assets"][0] = fixture.asset(200, "SHA256SUMS", fixture.sums)
    with pytest.raises(staging.archive.ArtifactRejected, match="^ARCHIVE_CHECKSUM_MISMATCH$"):
        stage(area, fixture)
    assert not (area / JOB / "source").exists()


@pytest.mark.parametrize("relative", [".env", ".env.local", ".git/config", ".ssh/config",
    "data/database.sqlite3", "data/new-file", "backups/old", "secrets/key",
    "private/setting", "diagnostic.log", "runtime.db", "module/__pycache__/code.pyc",
    "docker-compose.override.yml", "compose.yaml"])
def test_runtime_and_private_overlay_refused_before_extraction(area, relative):
    fixture = OfficialFixture(tar_bytes(content(extra={relative: b"not operational release content"})))
    with pytest.raises(staging.StagingRejected, match="^RUNTIME_OVERLAY$"):
        stage(area, fixture)
    assert not (area / JOB / "source").exists()


def test_malformed_tar_is_rejected_before_extraction(area):
    fixture = OfficialFixture(b"not a tar file")
    with pytest.raises(staging.archive.ArtifactRejected):
        stage(area, fixture)
    assert not (area / JOB / "source").exists()


def test_longnames_and_ustar_are_extracted_with_the_same_parser(area):
    import tarfile
    name = "docs/" + "a" * 70 + "/" + "b" * 60 + ".md"
    fixture = OfficialFixture(tar_bytes(content(extra={name: b"long path fixture"}), format=tarfile.USTAR_FORMAT))
    assert stage(area, fixture)["integrity_verified"]
    assert (area / JOB / "source" / name).read_bytes() == b"long path fixture"


def test_changed_release_between_reads_is_not_a_completed_candidate(area):
    fixture = OfficialFixture()
    def fetch(url, output, **kwargs):
        if len(fixture.calls) == 3:
            fixture.metadata["id"] = 999
        return fixture(url, output, **kwargs)
    with pytest.raises(staging.StagingRejected, match="^RELEASE_CHANGED$"):
        stage(area, fetch)
    assert (area / JOB / "source").is_dir()
    assert not (area / JOB / "ready.json").exists()


def test_interrupted_download_preserved_and_same_job_cannot_retry(area):
    def fail(url, output, **kwargs):
        raise transport.DownloadRejected("DOWNLOAD_TIMEOUT")
    with pytest.raises(transport.DownloadRejected):
        stage(area, fail)
    reservation = (area / JOB / "reservation.json").read_bytes()
    with pytest.raises(staging.StagingRejected, match="^STAGING_EXISTS$"):
        stage(area)
    assert (area / JOB / "reservation.json").read_bytes() == reservation


@pytest.mark.parametrize("entry", ["ready.json", "reservation.json", "SHA256SUMS", NAME, "source/README.md"])
def test_readback_rejects_changed_file(area, entry):
    stage(area)
    (area / JOB / entry).write_bytes(b"tampered synthetic fixture")
    with pytest.raises((staging.StagingRejected, staging.archive.ArtifactRejected)):
        verify(area)


@pytest.mark.parametrize("kind", ["file-symlink", "directory-symlink", "hardlink", "mode", "extra", "missing"])
def test_source_tree_readback_is_exact_and_nofollow(area, kind):
    stage(area)
    root = area / JOB / "source"
    path = root / "README.md"
    if kind in {"file-symlink", "hardlink"}:
        saved = area / "saved-fixture"
        path.rename(saved)
        if kind == "file-symlink":
            path.symlink_to(saved)
        else:
            os.link(saved, path)
    elif kind == "directory-symlink":
        (root / "data").rename(area / "saved-data")
        (root / "data").symlink_to(area / "saved-data", target_is_directory=True)
    elif kind == "mode":
        path.chmod(0o644)
    elif kind == "extra":
        (root / "unexpected").write_text("unexpected")
    else:
        path.unlink()
    with pytest.raises(staging.StagingRejected):
        verify(area)


@pytest.mark.parametrize("kind", ["shared-mode", "symlink", "no-space", "wrong-owner"])
def test_bad_staging_parent_fails_before_reservation(area, kind, monkeypatch):
    selected = area
    if kind == "shared-mode":
        area.chmod(0o777)
    elif kind == "symlink":
        selected = area.parent / "alias"
        selected.symlink_to(area, target_is_directory=True)
    elif kind == "wrong-owner":
        monkeypatch.setattr(staging.os, "geteuid", lambda: os.getuid() + 1)
    else:
        from basswiesn.update_helper.host_preflight import HostCode
        def fail(*a, **k):
            raise HostRejected(HostCode.INSUFFICIENT_SPACE)
        monkeypatch.setattr(staging, "inspect_space", fail)
    with pytest.raises((HostRejected, staging.StagingRejected)):
        stage(selected)
    assert not list(area.iterdir())


def test_partial_extraction_failure_has_no_ready_receipt_or_retry(area, monkeypatch):
    original = staging._verify_tree
    def fail(*a, **k):
        raise OSError("DO_NOT_OUTPUT_LOCAL_DETAILS")
    monkeypatch.setattr(staging, "_verify_tree", fail)
    with pytest.raises(staging.StagingRejected, match="^STAGING_IO$"):
        stage(area)
    assert not (area / JOB / "ready.json").exists()
    monkeypatch.setattr(staging, "_verify_tree", original)
    with pytest.raises(staging.StagingRejected, match="^STAGING_EXISTS$"):
        stage(area)


def test_archive_path_replacement_cannot_receive_ready_receipt(area, monkeypatch):
    original = staging._extract
    def replace_path(raw, *args):
        result = original(raw, *args)
        path = area / JOB / NAME
        path.rename(area / "original-fixture")
        path.write_bytes(b"replacement")
        return result
    monkeypatch.setattr(staging, "_extract", replace_path)
    with pytest.raises(staging.StagingRejected, match="^STAGING_UNSAFE$"):
        stage(area)
    assert not (area / JOB / "ready.json").exists()


@pytest.mark.parametrize("change", [
    {"schema": True}, {"schema": 1.0}, {"state": "COMPLETE"}, {"version": "2.6.5"},
    {"request_id": "other"}, {"release_id": True}, {"archive_asset_id": 200},
    {"repository": "https://example.invalid/"}, {"file_count": False},
    {"extra": "not allowed"}, {"checksums_sha256": "0" * 64}, {"tree_sha256": "0" * 64},
])
def test_receipt_tampering_is_never_readiness(area, change):
    stage(area)
    path = area / JOB / "ready.json"
    value = json.loads(path.read_bytes())
    value.update(change)
    path.write_text(json.dumps(value))
    with pytest.raises(staging.StagingRejected):
        verify(area)


@pytest.mark.parametrize("kind", ["hardlink", "symlink", "fifo", "shared-mode"])
def test_archived_file_readback_requires_private_regular_single_link(area, kind):
    stage(area)
    path = area / JOB / NAME
    if kind == "shared-mode":
        path.chmod(0o644)
    else:
        saved = area / "saved-archive"
        path.rename(saved)
        if kind == "hardlink":
            os.link(saved, path)
        elif kind == "symlink":
            path.symlink_to(saved)
        else:
            os.mkfifo(path, 0o600)
    with pytest.raises(staging.StagingRejected):
        verify(area)


def test_archive_corruption_between_verify_and_scan_is_refused(area, monkeypatch):
    scan = staging._scan
    def changed(raw, version):
        path = area / JOB / NAME
        with path.open("r+b") as writer:
            writer.write(b"broken")
        return scan(raw, version)
    monkeypatch.setattr(staging, "_scan", changed)
    with pytest.raises((staging.StagingRejected, staging.archive.ArtifactRejected)):
        stage(area)
    assert not (area / JOB / "ready.json").exists()


def test_fsync_failure_has_no_success_receipt(area, monkeypatch):
    def fail(*args):
        raise OSError("synthetic private disk details")
    monkeypatch.setattr(staging.os, "fsync", fail)
    with pytest.raises(staging.StagingRejected, match="^STAGING_IO$"):
        stage(area)
    assert not (area / JOB / "ready.json").exists()


def test_truncated_asset_is_not_accepted_from_zero_exit_transfer(area):
    fixture = OfficialFixture()
    def truncated(url, output, **kwargs):
        if url == transport.API + "assets/201":
            output.write(b"partial")
            return len(b"partial")
        return fixture(url, output, **kwargs)
    with pytest.raises(staging.StagingRejected, match="^ASSET_MISMATCH$"):
        stage(area, truncated)
    assert not (area / JOB / "source").exists()


def test_unhandled_provider_exception_is_redacted(area):
    def fail(*a, **k):
        raise RuntimeError("DO_NOT_OUTPUT_URL_OR_SIGNATURE")
    with pytest.raises(staging.StagingRejected, match="^STAGING_INVALID$"):
        stage(area, fail)


def test_expired_operation_cannot_create_ready_marker(area, monkeypatch):
    from types import SimpleNamespace
    fixture = OfficialFixture()
    clock = [1000]
    monkeypatch.setattr(staging, "time", SimpleNamespace(monotonic=lambda: clock[0]))
    def delayed(url, output, **kwargs):
        result = fixture(url, output, **kwargs)
        if len(fixture.calls) == 4:
            clock[0] = 1121
        return result
    with pytest.raises(staging.StagingRejected, match="^STAGING_TIMEOUT$"):
        stage(area, delayed)
    assert not (area / JOB / "ready.json").exists()


def test_metadata_edit_unrelated_to_artifact_does_not_change_identity(area):
    fixture = OfficialFixture()
    def edited(url, output, **kwargs):
        if len(fixture.calls) == 3:
            fixture.metadata["body"] = "new release prose, same reviewed assets"
        return fixture(url, output, **kwargs)
    assert stage(area, edited)["state"] == "SOURCE_STAGED"
