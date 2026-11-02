import hashlib
import io
import json
from pathlib import Path
import tarfile

import pytest

from tools.build_ssh_client import (
    BuildRejected, checked_bytes, check_signature_status, extract_source, source_lock,
)

pytestmark = pytest.mark.unit
LOCK = Path(__file__).resolve().parents[1] / "tools/openssh-source.json"


def test_source_is_exact_authenticated_upstream_not_unstable_distro_package():
    lock = source_lock(LOCK)
    assert lock["version"] == "10.6p1"
    assert lock["archive_sha256"] == "a9dc9565dffe8640f64d863cd29a32bc4a3dbdec0566a7fc44c5d6ee767d5f39"
    assert lock["signing_fingerprint"] == "7168B983815A5EEF59A4ADFD2A3F414E736060BA"


@pytest.mark.parametrize("field,value", [
    ("archive_url", "http://example.invalid/source.tar.gz"),
    ("archive_url", "https://cdn.openbsd.org.evil.invalid/source.tar.gz"),
    ("signature_url", "https://example.invalid/source.asc"),
    ("release_key_url", "https://example.invalid/key.asc"),
    ("archive_sha256", ""), ("signature_sha256", "0" * 63),
    ("release_key_sha256", "G" * 64), ("signing_fingerprint", "short"),
    ("version", "../../bad"), ("version", 106),
    ("release_notes", "https://example.invalid/notes"),
])
def test_malformed_or_redirected_source_lock_is_denied(tmp_path, field, value):
    lock = json.loads(LOCK.read_text())
    lock[field] = value
    path = tmp_path / "lock.json"
    path.write_text(json.dumps(lock))
    with pytest.raises(BuildRejected):
        source_lock(path)


def test_content_hash_size_and_nonempty_are_all_required():
    content = b"synthetic source fixture"
    expected = hashlib.sha256(content).hexdigest()
    assert checked_bytes(content, expected) == content
    for data, digest, limit in ((content + b"x", expected, 100), (content, expected, 3),
                                (b"", hashlib.sha256(b"").hexdigest(), 100)):
        with pytest.raises(BuildRejected):
            checked_bytes(data, digest, limit)


def test_verified_signature_must_match_exact_pinned_signer():
    signer = source_lock(LOCK)["signing_fingerprint"]
    good = f"[GNUPG:] VALIDSIG {signer} 2026-10-06 1791280023 0 4 0 1 10 00 {signer}\n"
    check_signature_status(good, signer)
    for status in ("", good + good, good.replace(signer, "A" * 40),
                   good + "[GNUPG:] BADSIG key bad\n", good + "[GNUPG:] REVKEYSIG key revoked\n",
                   good + "[GNUPG:] EXPKEYSIG key expired\n"):
        with pytest.raises(BuildRejected):
            check_signature_status(status, signer)


def source_tar(tmp_path, name, kind=tarfile.REGTYPE, mode=0o644):
    path = tmp_path / "source.tar.gz"
    with tarfile.open(path, "w:gz") as archive:
        member = tarfile.TarInfo(name)
        member.type, member.mode = kind, mode
        member.linkname = "../../outside" if kind in (tarfile.SYMTYPE, tarfile.LNKTYPE) else ""
        member.size = 3 if kind == tarfile.REGTYPE else 0
        archive.addfile(member, io.BytesIO(b"abc") if member.size else None)
    return path


def test_regular_verified_source_extracts_only_inside_declared_tree(tmp_path):
    path = source_tar(tmp_path, "openssh-10.6p1/README")
    root = extract_source(path, tmp_path / "out", "10.6p1")
    assert (root / "README").read_bytes() == b"abc"


@pytest.mark.parametrize("name,kind,mode", [
    ("../outside", tarfile.REGTYPE, 0o644), ("/outside", tarfile.REGTYPE, 0o644),
    ("openssh-10.6p1/../../outside", tarfile.REGTYPE, 0o644),
    ("other-version/file", tarfile.REGTYPE, 0o644),
    ("openssh-10.6p1/link", tarfile.SYMTYPE, 0o777),
    ("openssh-10.6p1/link", tarfile.LNKTYPE, 0o777),
    ("openssh-10.6p1/device", tarfile.CHRTYPE, 0o644),
    ("openssh-10.6p1/program", tarfile.REGTYPE, 0o4755),
])
def test_unsafe_source_members_never_extract(tmp_path, name, kind, mode):
    path = source_tar(tmp_path, name, kind, mode)
    with pytest.raises(BuildRejected):
        extract_source(path, tmp_path / "out", "10.6p1")
    assert not (tmp_path / "outside").exists()
    assert not (tmp_path / "out").exists()
