#!/usr/bin/env python3
"""Build the pinned authentic OpenSSH client; Docker build stage only, no radio I/O."""
from __future__ import annotations

import argparse
import hashlib
import json
import os
from pathlib import Path, PurePosixPath
import re
import shutil
import subprocess
import tarfile
import tempfile
from typing import Any
import urllib.request

BINARIES = ("ssh", "scp", "sftp", "ssh-keygen", "ssh-keyscan", "ssh-agent", "ssh-add")
HELPERS = ("ssh-pkcs11-helper", "ssh-sk-helper")
HOST = "https://cdn.openbsd.org/pub/OpenBSD/OpenSSH/"
MAX_DOWNLOAD = 16 * 1024 * 1024
MAX_EXPANDED = 100 * 1024 * 1024


class BuildRejected(ValueError):
    """No fallback to an unsigned, unchecked or different version."""


class NoRedirect(urllib.request.HTTPRedirectHandler):
    def redirect_request(self, req: Any, fp: Any, code: int, msg: str,
                         headers: Any, newurl: str) -> None:
        raise BuildRejected("source redirect refused")


def source_lock(path: Path) -> dict[str, str]:
    raw = json.loads(path.read_text())
    required = {"version", "archive_url", "archive_sha256", "signature_url", "signature_sha256",
                "release_key_url", "release_key_sha256", "signing_fingerprint", "release_notes"}
    if not isinstance(raw, dict) or set(raw) != required or not all(isinstance(v, str) for v in raw.values()):
        raise BuildRejected("invalid source lock")
    lock: dict[str, str] = raw
    version = lock["version"]
    if not re.fullmatch(r"[0-9]{1,2}\.[0-9]{1,2}p[0-9]{1,2}", version):
        raise BuildRejected("invalid source version")
    archive = HOST + f"portable/openssh-{version}.tar.gz"
    if (lock["archive_url"] != archive or lock["signature_url"] != archive + ".asc"
            or lock["release_key_url"] != HOST + "RELEASE_KEY.asc"
            or lock["release_notes"] != "https://www.openssh.org/releasenotes.html#" + version):
        raise BuildRejected("source origin is not the pinned upstream")
    if any(not re.fullmatch(r"[a-f0-9]{64}", lock[name])
           for name in ("archive_sha256", "signature_sha256", "release_key_sha256")):
        raise BuildRejected("invalid source hash")
    if not re.fullmatch(r"[A-F0-9]{40}", lock["signing_fingerprint"]):
        raise BuildRejected("invalid signing fingerprint")
    return lock


def checked_bytes(data: bytes, expected: str, limit: int = MAX_DOWNLOAD) -> bytes:
    if not data or len(data) > limit or hashlib.sha256(data).hexdigest() != expected:
        raise BuildRejected("source size or hash mismatch")
    return data


def download(url: str, expected: str, target: Path) -> None:
    # These URLs come only from source_lock; proxies and redirects cannot silently
    # substitute a different origin. TLS, pinned content and signature are separate gates.
    opener = urllib.request.build_opener(urllib.request.ProxyHandler({}), NoRedirect())
    with opener.open(url, timeout=30) as response:
        if response.status != 200 or response.geturl() != url:
            raise BuildRejected("unexpected source response")
        content = checked_bytes(response.read(MAX_DOWNLOAD + 1), expected)
    target.write_bytes(content)


def check_signature_status(status: str, fingerprint: str) -> None:
    if any(f"[GNUPG:] {tag} " in status for tag in ("BADSIG", "ERRSIG", "EXPSIG", "EXPKEYSIG", "REVKEYSIG")):
        raise BuildRejected("signature is invalid, expired or revoked")
    signatures = [line.split() for line in status.splitlines() if line.startswith("[GNUPG:] VALIDSIG ")]
    if (len(signatures) != 1 or len(signatures[0]) != 12
            or signatures[0][2] != fingerprint or signatures[0][-1] != fingerprint):
        raise BuildRejected("unexpected signing identity")


def extract_source(archive: Path, destination: Path, version: str) -> Path:
    prefix = f"openssh-{version}"
    with tarfile.open(archive, "r:gz") as tar:
        members: list[tarfile.TarInfo] = []
        seen: set[str] = set()
        expanded = 0
        for member in tar:
            if len(members) >= 20000:
                raise BuildRejected("source member limit")
            path = PurePosixPath(member.name)
            if (not member.name or path.is_absolute() or ".." in path.parts or not path.parts
                    or path.parts[0] != prefix or not (member.isfile() or member.isdir())
                    or path.as_posix() != member.name.rstrip("/")
                    or path.as_posix() in seen or member.mode & 0o6000 or member.size < 0):
                raise BuildRejected("unsafe source member")
            seen.add(path.as_posix())
            members.append(member)
            expanded += member.size
            if expanded > MAX_EXPANDED:
                raise BuildRejected("expanded source limit")
        if not members:
            raise BuildRejected("source member limit")
        # Python 3.12 is fixed by the Docker build-stage base. The allowlist above
        # additionally refuses links/devices and never trusts archive ownership.
        tar.extractall(destination, members=members, filter="data")
    return destination / prefix


def build(lock_path: Path, output: Path) -> None:
    lock = source_lock(lock_path)
    if output.exists() or output.is_symlink():
        raise BuildRejected("output must be new")
    with tempfile.TemporaryDirectory(prefix="basswiesn-ssh-build-") as temporary:
        work = Path(temporary)
        archive, signature, key = (work / name for name in ("source.tar.gz", "source.asc", "release.asc"))
        for label, target in (("archive", archive), ("signature", signature), ("release_key", key)):
            download(lock[label + "_url"], lock[label + "_sha256"], target)
        keyring = work / "keyring"
        keyring.mkdir(mode=0o700)
        base = ["gpg", "--homedir", str(keyring), "--batch", "--no-auto-key-retrieve"]
        subprocess.run([*base, "--import", str(key)], check=True, stdout=subprocess.DEVNULL)
        verified = subprocess.run([*base, "--status-fd", "1", "--verify", str(signature), str(archive)],
                                  check=True, capture_output=True, text=True)
        check_signature_status(verified.stdout, lock["signing_fingerprint"])
        source = extract_source(archive, work, lock["version"])
        environment = {**os.environ, "LC_ALL": "C", "SOURCE_DATE_EPOCH": "0"}
        subprocess.run(["./configure", "--prefix=/usr/local", "--sysconfdir=/etc/ssh",
                        "--without-pam", "--without-kerberos5"], cwd=source, env=environment, check=True)
        subprocess.run(["make", "-j2", *BINARIES, *HELPERS], cwd=source, env=environment, check=True)
        # No sshd, setuid helper or compiler is copied to the runtime image.
        for subdir in ("bin", "libexec", "share/doc/openssh"):
            (output / subdir).mkdir(parents=True, exist_ok=True)
        files: dict[str, str] = {}
        for name in (*BINARIES, *HELPERS):
            destination = output / ("bin" if name in BINARIES else "libexec") / name
            shutil.copyfile(source / name, destination)
            destination.chmod(0o755)
            files[destination.relative_to(output).as_posix()] = hashlib.sha256(destination.read_bytes()).hexdigest()
        shutil.copyfile(source / "LICENCE", output / "share/doc/openssh/LICENCE")
        provenance = {"format": 1, "upstream": "OpenSSH portable", "version": lock["version"],
                      "source": lock, "signature_verified": True, "files": files,
                      "hardware_verified": False}
        (output / "share/doc/openssh/build-manifest.json").write_text(json.dumps(provenance, sort_keys=True, indent=2) + "\n")


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--lock", type=Path, required=True)
    parser.add_argument("--output", type=Path, required=True)
    args = parser.parse_args()
    build(args.lock, args.output)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
