"""Adversarial, entirely offline tests of the release verification boundary."""
from dataclasses import asdict, replace
import gzip
import hashlib
import io
import json
import os
from pathlib import Path
import tarfile

import pytest

from basswiesn.app.services import release_artifact as verify


pytestmark = pytest.mark.unit
VERSION = "3.0.0"
NAME = f"basswiesn-docker-release-{VERSION}.tar.gz"


def sha(data):
    return hashlib.sha256(data).hexdigest()


def content(*, change_manifest=None, change_sums=None, runtime_version=VERSION, extra=None):
    files = {name: b"synthetic offline fixture\n" for name in verify.REQUIRED_FILES}
    files["basswiesn/__init__.py"] = f'__version__ = "{runtime_version}"\n'.encode()
    files.update(extra or {})
    manifest = {"format": 1, "version": VERSION, "source_date_epoch": 0,
                "files": [{"path": name, "sha256": sha(value), "size": len(value)}
                          for name, value in sorted(files.items())]}
    if change_manifest:
        change_manifest(manifest)
    files["manifest.json"] = json.dumps(manifest).encode()
    sums = {name: sha(value) for name, value in files.items()}
    if change_sums:
        change_sums(sums)
    files["SHA256SUMS"] = "".join(f"{value}  {name}\n" for name, value in sorted(sums.items())).encode()
    return files


def tar_bytes(files=None, *, entries=(), mutate=None, format=tarfile.GNU_FORMAT):
    files = content() if files is None else files
    output = io.BytesIO()
    with tarfile.open(fileobj=output, mode="w", format=format) as archive:
        dirs = {verify.ROOT, verify.ROOT + "/data"}
        for name in files:
            dirs.update(str(parent) for parent in Path(verify.ROOT, name).parents if str(parent) != ".")
        for name in sorted(dirs):
            info = tarfile.TarInfo(name + "/")
            info.type, info.mode = tarfile.DIRTYPE, 0o755
            if mutate:
                mutate(info)
            archive.addfile(info)
        for name, data in sorted(files.items()):
            info = tarfile.TarInfo(verify.ROOT + "/" + name)
            info.size, info.mode = len(data), 0o755 if name == "install.sh" else 0o644
            if mutate:
                mutate(info)
            archive.addfile(info, io.BytesIO(data))
        for info, data in entries:
            archive.addfile(info, io.BytesIO(data))
    return output.getvalue()


def staged(tmp_path, tar=None, *, compressed=None):
    path = tmp_path / NAME
    data = compressed if compressed is not None else gzip.compress(tar if tar is not None else tar_bytes(), mtime=0)
    path.write_bytes(data)
    sums = f"{sha(data)}  {NAME}\n".encode()
    return path, sums


def rejected(tmp_path, code, *, tar=None, compressed=None, limits=None):
    path, sums = staged(tmp_path, tar, compressed=compressed)
    with pytest.raises(verify.ArtifactRejected) as error:
        verify.verify_release_archive(path, sums, version=VERSION, **({"limits": limits} if limits else {}))
    assert error.value.code == code
    assert str(error.value) == code  # never echo path, headers or payloads


@pytest.mark.parametrize("format", [tarfile.GNU_FORMAT, tarfile.USTAR_FORMAT])
def test_real_package_layout_is_verified_without_writes_or_execution(tmp_path, monkeypatch, format):
    files = content(extra={"not-executed.py": b'raise RuntimeError("must never execute")\n'})
    path, sums = staged(tmp_path, tar_bytes(files, format=format))
    before = {p: p.read_bytes() for p in tmp_path.iterdir()}
    def forbidden(*args, **kwargs):
        pytest.fail("Verifier must never extract or execute")
    monkeypatch.setattr(tarfile.TarFile, "extractall", forbidden)
    monkeypatch.setattr(tarfile.TarFile, "extract", forbidden)
    result = verify.verify_release_archive(path, sums, version=VERSION)
    assert result.version == VERSION and result.file_count == len(files)
    assert result.archive_sha256 == sha(path.read_bytes())
    assert result.integrity_verified and not result.authenticity_verified
    assert not result.installation_performed
    assert before == {p: p.read_bytes() for p in tmp_path.iterdir()}
    assert str(tmp_path) not in json.dumps(asdict(result))


def test_bounded_gnu_longname_and_ustar_prefix(tmp_path):
    name = "docs/" + "a" * 70 + "/" + "b" * 60 + ".md"
    for format in (tarfile.GNU_FORMAT, tarfile.USTAR_FORMAT):
        path, sums = staged(tmp_path, tar_bytes(content(extra={name: b"fixture"}), format=format))
        assert verify.verify_release_archive(path, sums, version=VERSION).integrity_verified


@pytest.mark.parametrize("version", ["v3.0.0", "3.0", "03.0.0", "3.0.0-rc1", "3.0.0/evil", "3.0.0\n", None])
def test_version_must_be_exact_stable_identity(tmp_path, version):
    path, sums = staged(tmp_path)
    with pytest.raises(verify.ArtifactRejected, match="^INVALID_VERSION$"):
        verify.verify_release_archive(path, sums, version=version)


def test_wrong_archive_name_is_rejected_before_open(tmp_path):
    with pytest.raises(verify.ArtifactRejected, match="^ARCHIVE_NAME$"):
        verify.verify_release_archive(tmp_path / "different.tar.gz", b"ignored", version=VERSION)


@pytest.mark.parametrize("mutate", [lambda s: s + s, lambda s: b"# comment\n" + s,
    lambda s: s.replace(b"  ", b" ../"), lambda s: s.replace(b"\n", b"\r\n"),
    lambda s: s + b"\n", lambda s: s.replace(b"  ", b" \t"),
    lambda s: b"\\" + s, lambda s: s.replace(b"basswiesn-", b"different-"),
    lambda s: s * 60, lambda s: b"", lambda s: b"\xff" + s])
def test_untrusted_external_checksum_grammar_and_target(tmp_path, mutate):
    path, sums = staged(tmp_path)
    with pytest.raises(verify.ArtifactRejected):
        verify.verify_release_archive(path, mutate(sums), version=VERSION)


def test_checksum_is_checked_before_parsing_any_archive(tmp_path, monkeypatch):
    path, _ = staged(tmp_path, compressed=b"not a gzip")
    def forbidden(*args, **kwargs):
        pytest.fail("Parser must not run on an unverified compressed artifact")
    monkeypatch.setattr(verify, "_read_tar", forbidden)
    with pytest.raises(verify.ArtifactRejected, match="^ARCHIVE_CHECKSUM_MISMATCH$"):
        verify.verify_release_archive(path, f'{"0" * 64}  {NAME}\n'.encode(), version=VERSION)


@pytest.mark.parametrize("path", ["../escape", "/absolute", "wrong-root/file", verify.ROOT + "/../escape",
    verify.ROOT + "//double", verify.ROOT + "/./dot", verify.ROOT + "/a\\b", verify.ROOT + "/file ",
    verify.ROOT + "/secret:\nvalue", verify.ROOT + "/colon:name", verify.ROOT + "/tail."])
def test_bad_member_names_fail_even_with_valid_external_checksum(tmp_path, path):
    info = tarfile.TarInfo(path)
    with pytest.raises(verify.ArtifactRejected):
        archive, sums = staged(tmp_path, tar_bytes(entries=[(info, b"")]))
        verify.verify_release_archive(archive, sums, version=VERSION)
    assert not (tmp_path / "escape").exists()


@pytest.mark.parametrize("kind", [tarfile.SYMTYPE, tarfile.LNKTYPE, tarfile.FIFOTYPE,
    tarfile.CHRTYPE, tarfile.BLKTYPE, tarfile.GNUTYPE_SPARSE, tarfile.XHDTYPE,
    tarfile.XGLTYPE, tarfile.GNUTYPE_LONGLINK, tarfile.CONTTYPE])
def test_links_sparse_devices_and_extension_records_are_rejected(tmp_path, kind):
    info = tarfile.TarInfo(verify.ROOT + "/unsafe")
    info.type, info.linkname = kind, ""
    rejected(tmp_path, "TAR_ENTRY_TYPE", tar=tar_bytes(entries=[(info, b"")]))


@pytest.mark.parametrize("field,value", [("mode", 0o4775), ("mode", 0o2775), ("mode", 0o777),
    ("uid", 1000), ("gid", 1000), ("linkname", "/outside")])
def test_dangerous_header_attributes(tmp_path, field, value):
    info = tarfile.TarInfo(verify.ROOT + "/unsafe")
    setattr(info, field, value)
    rejected(tmp_path, "TAR_ATTRIBUTES", tar=tar_bytes(entries=[(info, b"")]))


def test_duplicate_members_and_file_directory_collision(tmp_path):
    for name, kind in [("README.md", tarfile.REGTYPE), ("README.md", tarfile.DIRTYPE), ("data/", tarfile.DIRTYPE)]:
        info = tarfile.TarInfo(verify.ROOT + "/" + name)
        info.type = kind
        rejected(tmp_path, "DUPLICATE_PATH", tar=tar_bytes(entries=[(info, b"")]))


def test_parent_must_be_an_explicit_directory(tmp_path):
    info = tarfile.TarInfo(verify.ROOT + "/README.md/child")
    rejected(tmp_path, "MISSING_PARENT_DIRECTORY", tar=tar_bytes(entries=[(info, b"")]))


def test_directory_slash_alias_is_not_normalized_into_acceptance(tmp_path):
    info = tarfile.TarInfo(verify.ROOT + "/alias//")
    info.type = tarfile.DIRTYPE
    # tarfile's writer normalizes directory names; build the raw header to keep
    # the malicious spelling. Recompute checksum like an adversarial sender.
    data = tar_bytes()
    header = bytearray(info.tobuf(tarfile.GNU_FORMAT))
    name = (verify.ROOT + "/alias//").encode()
    header[:100] = name.ljust(100, b"\0")
    header[148:156] = b"        "
    header[148:156] = f"{sum(header):06o}\0 ".encode()
    rejected(tmp_path, "UNSAFE_PATH", tar=bytes(header) + data)


@pytest.mark.parametrize("field,maximum,code", [("compressed_bytes", 10, "COMPRESSED_LIMIT"),
    ("expanded_bytes", 700, "EXPANDED_LIMIT"), ("member_bytes", 10, "MEMBER_LIMIT"),
    ("metadata_bytes", 10, "METADATA_LIMIT"), ("headers", 2, "HEADER_LIMIT"),
    ("path_bytes", 10, "UNSAFE_PATH")])
def test_all_resource_budgets_are_enforced(tmp_path, field, maximum, code):
    rejected(tmp_path, code, limits=replace(verify.VerificationLimits(), **{field: maximum}))


def test_zero_padding_bomb_and_gzip_crc_truncation_concatenation(tmp_path):
    rejected(tmp_path, "EXPANDED_LIMIT", compressed=gzip.compress(bytes(128 * 1024)),
             limits=replace(verify.VerificationLimits(), expanded_bytes=16 * 1024))
    good = gzip.compress(tar_bytes())
    rejected(tmp_path, "GZIP_TRUNCATED", compressed=good[:-8])
    rejected(tmp_path, "GZIP_TRAILING_DATA", compressed=good + gzip.compress(b""))
    rejected(tmp_path, "GZIP_TRAILING_DATA", compressed=good + b"trailer")
    bad = bytearray(good)
    bad[-8] ^= 1
    rejected(tmp_path, "MALFORMED_OR_UNREADABLE_ARCHIVE", compressed=bytes(bad))


def test_tar_truncation_end_marker_and_trailing_payload(tmp_path):
    rejected(tmp_path, "TAR_TRUNCATED", tar=tar_bytes()[:600])
    rejected(tmp_path, "TAR_END_MARKER", tar=bytes(512) + b"x" * 512)
    rejected(tmp_path, "TAR_TRAILING_DATA", tar=tar_bytes() + b"hidden archive")
    rejected(tmp_path, "TAR_ALIGNMENT", tar=tar_bytes() + b"\0")
    invalid = bytearray(tar_bytes())
    invalid[3] ^= 1
    rejected(tmp_path, "MALFORMED_OR_UNREADABLE_ARCHIVE", tar=bytes(invalid))


@pytest.mark.parametrize("change,code", [
    (lambda m: m.update(version="2.6.5"), "MANIFEST_VERSION"),
    (lambda m: m.update(format=True), "MANIFEST_VERSION"),
    (lambda m: m.update(source_date_epoch=True), "MANIFEST_SCHEMA"),
    (lambda m: m.update(extra="unrecognized"), "MANIFEST_SCHEMA"),
    (lambda m: m["files"].pop(), "MANIFEST_MISMATCH"),
    (lambda m: m["files"].append(m["files"][0]), "DUPLICATE_MANIFEST_PATH"),
    (lambda m: m["files"][0].update(size=True), "MANIFEST_SCHEMA"),
    (lambda m: m["files"][0].update(size=123456), "MANIFEST_MISMATCH"),
    (lambda m: m["files"][0].update(sha256="0" * 64), "MANIFEST_MISMATCH"),
    (lambda m: m["files"][0].update(path="../escape"), "UNSAFE_PATH"),
])
def test_manifest_is_complete_unique_and_exact(tmp_path, change, code):
    rejected(tmp_path, code, tar=tar_bytes(content(change_manifest=change)))


@pytest.mark.parametrize("change", [lambda s: s.pop("README.md"),
    lambda s: s.update({"absent.txt": "0" * 64}), lambda s: s.update({"manifest.json": "0" * 64})])
def test_internal_checksums_cover_manifest_and_all_other_files(tmp_path, change):
    rejected(tmp_path, "INTERNAL_CHECKSUM_MISMATCH", tar=tar_bytes(content(change_sums=change)))


def test_corrupt_payload_undeclared_files_and_missing_required_files(tmp_path):
    files = content()
    files["README.md"] = b"changed"
    rejected(tmp_path, "MANIFEST_MISMATCH", tar=tar_bytes(files))
    files = content()
    files["undeclared"] = b"unexpected"
    rejected(tmp_path, "MANIFEST_MISMATCH", tar=tar_bytes(files))
    files = content()
    del files["install.sh"]
    rejected(tmp_path, "MISSING_REQUIRED_FILE", tar=tar_bytes(files))


def test_duplicate_json_keys_and_excessive_nesting_are_rejected(tmp_path):
    files = content()
    files["manifest.json"] = files["manifest.json"].replace(b'"format": 1', b'"format": 1, "format": 1')
    rejected(tmp_path, "DUPLICATE_JSON_KEY", tar=tar_bytes(files))
    files["manifest.json"] = b"[" * 10000 + b"]" * 10000
    rejected(tmp_path, "MANIFEST_DEPTH", tar=tar_bytes(files))


def test_runtime_version_must_match_without_importing_package(tmp_path):
    rejected(tmp_path, "RUNTIME_VERSION_MISMATCH", tar=tar_bytes(content(runtime_version="2.6.5")))
    files = content(extra={"basswiesn/__init__.py": b'__version__ = str("3.0.0")\n'})
    rejected(tmp_path, "RUNTIME_VERSION_MISMATCH", tar=tar_bytes(files))


def test_installer_must_be_executable(tmp_path):
    def mutate(info):
        if info.name.endswith("/install.sh"):
            info.mode = 0o644
    rejected(tmp_path, "INSTALLER_NOT_EXECUTABLE", tar=tar_bytes(mutate=mutate))


def test_archive_cannot_be_a_symlink_or_fifo(tmp_path):
    target = tmp_path / "payload"
    target.write_bytes(b"private payload")
    archive = tmp_path / NAME
    archive.symlink_to(target)
    sums = f'{"0" * 64}  {NAME}\n'.encode()
    with pytest.raises(verify.ArtifactRejected, match="^MALFORMED_OR_UNREADABLE_ARCHIVE$"):
        verify.verify_release_archive(archive, sums, version=VERSION)
    archive.unlink()  # test-owned synthetic link, not a release artifact
    os.mkfifo(archive)
    with pytest.raises(verify.ArtifactRejected, match="^ARCHIVE_NOT_REGULAR$"):
        verify.verify_release_archive(archive, sums, version=VERSION)


def test_archive_change_during_validation_is_detected(tmp_path, monkeypatch):
    archive, sums = staged(tmp_path)
    original = verify._verify_contents
    def change(*args):
        original(*args)
        with archive.open("ab") as stream:
            stream.write(b"changed")
    monkeypatch.setattr(verify, "_verify_contents", change)
    with pytest.raises(verify.ArtifactRejected, match="^ARCHIVE_CHANGED$"):
        verify.verify_release_archive(archive, sums, version=VERSION)


def test_checksum_binary_marker_supported(tmp_path):
    archive, sums = staged(tmp_path)
    assert verify.verify_release_archive(archive, sums.replace(b"  ", b" *"), version=VERSION).integrity_verified


def test_offline_cli_reports_only_safe_summary_and_failure_code(tmp_path, monkeypatch, capsys):
    from tools.verify_release_archive import main
    archive, sums = staged(tmp_path)
    checksum_file = tmp_path / "SHA256SUMS"
    checksum_file.write_bytes(sums)
    monkeypatch.setattr("sys.argv", ["verify_release_archive.py", str(archive), "--version", VERSION])
    assert main() == 0
    success = json.loads(capsys.readouterr().out)
    assert success["status"] == "verified" and not success["authenticity_verified"]
    checksum_file.write_bytes(b"secret: must never appear in diagnostics")
    assert main() == 1
    failure = capsys.readouterr().out
    assert json.loads(failure) == {"status": "rejected", "code": "CHECKSUM_FORMAT"}
    assert "secret" not in failure and str(tmp_path) not in failure


def test_longname_header_size_bound_and_orphan(tmp_path):
    info = tarfile.TarInfo("././@LongLink")
    info.type, info.size = tarfile.GNUTYPE_LONGNAME, 1027
    rejected(tmp_path, "LONGNAME_LIMIT", tar=tar_bytes(entries=[(info, b"x" * 1027)]))
    info.size = 10
    rejected(tmp_path, "TAR_END_MARKER", tar=tar_bytes(entries=[(info, b"123456789\0")]))


def test_payload_padding_must_be_zero(tmp_path):
    data = bytearray(tar_bytes())
    offset = 0
    while True:
        info = tarfile.TarInfo.frombuf(data[offset:offset + 512], "ascii", "strict")
        if info.size and info.size % 512:
            data[offset + 512 + info.size] = 1
            break
        offset += 512 + ((info.size + 511) // 512) * 512
    rejected(tmp_path, "TAR_NONZERO_PADDING", tar=bytes(data))


def test_malformed_json_and_executable_metadata_are_never_evaluated(tmp_path):
    files = content()
    files["manifest.json"] = b"not json"
    rejected(tmp_path, "MALFORMED_OR_UNREADABLE_ARCHIVE", tar=tar_bytes(files))
    files = content(extra={"basswiesn/__init__.py": b'__version__ = __import__("os").abort()\n'})
    rejected(tmp_path, "RUNTIME_VERSION_MISMATCH", tar=tar_bytes(files))
