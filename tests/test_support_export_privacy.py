"""Archive ambiguity, secret canaries and resource bounds for local exports."""

import hashlib
import json
import os
from zipfile import ZipFile

import pytest

from basswiesn.app.services import support_export


pytestmark = pytest.mark.unit


@pytest.mark.parametrize("name", [
    "", "/absolute", "../parent", "a/../parent", "a/./leaf", "a//leaf", "a/",
    "C:drive", "a\\escape", "hidden\x00.txt", "line\nother", "tab\tfile", "control\x7f",
    "support-manifest.json", "SUPPORT-MANIFEST.JSON", "SHA256SUMS", "sha256sums",
    "a/space ", "a/dot.", "\u00e4.txt", "a" * 241,
])
def test_unsafe_or_reserved_archive_names_are_rejected(name):
    with pytest.raises(ValueError, match="unsafe"):
        support_export.build_support_bundle({name: "fixture"}, max_bytes=4096)


def test_case_collisions_are_rejected():
    with pytest.raises(ValueError, match="unsafe"):
        support_export.build_support_bundle({"A.json": "{}", "a.json": "{}"}, max_bytes=4096)


def test_source_quota_is_enforced_before_parsing_redaction(monkeypatch):
    def forbidden(*args, **kwargs):
        raise AssertionError("redaction was called before source quota enforcement")

    monkeypatch.setattr(support_export, "_redacted_entry", forbidden)
    with pytest.raises(support_export.SupportBundleTooLarge):
        support_export.build_support_bundle({"state.json": '{"token":"' + "s" * 9000 + '"}'}, max_bytes=4096)


def test_metadata_and_entry_count_are_bounded_before_archive_construction():
    with pytest.raises(support_export.SupportBundleTooLarge):
        support_export.build_support_bundle({}, metadata={"token": "s" * 9000}, max_bytes=4096)
    with pytest.raises(support_export.SupportBundleTooLarge):
        support_export.build_support_bundle({f"{index}.txt": "" for index in range(257)}, max_bytes=100000)
    with pytest.raises(ValueError, match="positive"):
        support_export.build_support_bundle({}, max_bytes=0)


def test_manifest_overhead_and_archive_overhead_count_toward_limit():
    with pytest.raises(support_export.SupportBundleTooLarge):
        support_export.build_support_bundle({"file.txt": ""}, max_bytes=50)
    with pytest.raises(support_export.SupportBundleTooLarge):
        support_export.build_support_bundle({}, max_bytes=120)


def test_metadata_is_redacted_including_addresses_and_payload_hashes_are_verifiable():
    bundle = support_export.build_support_bundle(
        {"state.JSON": '{"token":1234,"status":"OK","peer":"192.0.2.18"}',
         "logs/errors.txt": "Cookie: opaque-test-cookie\npeer=[2001:db8::18]"},
        metadata={"password": "opaque-test-password", "peer": "192.0.2.18", "v6": "2001:db8::18"},
        max_bytes=8192,
    )
    with ZipFile(bundle) as archive:
        manifest = json.loads(archive.read("support-manifest.json"))
        all_data = b"".join(archive.read(name) for name in archive.namelist())
        assert not any(canary in all_data for canary in [
            b"opaque-test-password", b"opaque-test-cookie", b"192.0.2.18", b"2001:db8::18", b"1234",
        ])
        assert json.loads(archive.read("state.JSON"))["status"] == "OK"
        for member in manifest["files"]:
            data = archive.read(member["path"])
            assert member["size_bytes"] == len(data)
            assert member["sha256"] == hashlib.sha256(data).hexdigest()
            assert f"{member['sha256']}  {member['path']}\n".encode() in archive.read("SHA256SUMS")


def test_tail_reader_is_bounded_and_never_reads_symlink_or_fifo(tmp_path):
    source = tmp_path / "log"
    source.write_bytes(b"one\ntwo\nthree\nfour")
    assert support_export.tail_text(source, max_bytes=10, max_lines=1) == "four"
    assert support_export.tail_text(source, max_lines=0) == ""
    assert support_export.tail_text(source, max_bytes=0) == ""
    assert support_export.tail_text(tmp_path / "missing") == ""
    symlink = tmp_path / "symlink"
    symlink.symlink_to(source)
    assert support_export.tail_text(symlink) == ""
    directory_link = tmp_path / "linked-directory"
    directory_link.symlink_to(tmp_path, target_is_directory=True)
    assert support_export.tail_text(directory_link / "log") == ""
    fifo = tmp_path / "fifo"
    os.mkfifo(fifo)
    assert support_export.tail_text(fifo) == ""


def test_tail_byte_and_line_boundaries_never_orphan_secret_suffixes(tmp_path):
    path = tmp_path / "log"
    path.write_text('password="' + "A" * 1000 + 'TAIL_CANARY"\n')
    assert "TAIL_CANARY" not in support_export.tail_text(path, max_bytes=32)
    label = "PRIVATE" + " KEY"
    block = f"-----BEGIN {label}-----\n" + ("opaque-canary-key-line\n" * 10) + f"-----END {label}-----\n"
    path.write_text(block)
    assert "opaque-canary-key-line" not in support_export.tail_text(path, max_lines=3)
    assert "opaque-canary-key-line" not in support_export.tail_text(path, max_bytes=100, max_lines=10)
