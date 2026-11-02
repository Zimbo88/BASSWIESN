#!/usr/bin/env python3
"""Verify installed source-built SSH binaries and emit their CycloneDX inventory.

Run inside the built image without networking. A dpkg-only SBOM cannot account
for this source component. Inventory is not a vulnerability or hardware verdict.
"""
from __future__ import annotations

import argparse
import hashlib
import json
from pathlib import Path
import stat
import sys
from typing import Any

if __package__ in (None, ""):
    sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from tools.build_ssh_client import BINARIES, HELPERS, source_lock


def checked_file(path: Path, *, executable: bool = False) -> bytes:
    entry = path.lstat()
    if (not stat.S_ISREG(entry.st_mode) or entry.st_nlink != 1
            or entry.st_mode & 0o6022 or entry.st_size > 32 * 1024 * 1024
            or (executable and stat.S_IMODE(entry.st_mode) != 0o755)):
        raise ValueError("SSH_PROVENANCE_FILE_INVALID")
    return path.read_bytes()


def component(root: Path, lock_path: Path) -> dict[str, Any]:
    """Check the complete file set, not just a self-reported version string."""
    lock = source_lock(lock_path)
    manifest = json.loads(checked_file(root / "share/doc/openssh/build-manifest.json"))
    names = {"bin/" + name for name in BINARIES} | {"libexec/" + name for name in HELPERS}
    if (not isinstance(manifest, dict) or manifest.get("format") != 1
            or manifest.get("source") != lock or manifest.get("version") != lock["version"]
            or manifest.get("signature_verified") is not True
            or not isinstance(manifest.get("files"), dict)
            or set(manifest["files"]) != names):
        raise ValueError("SSH_PROVENANCE_MANIFEST_INVALID")
    hashes = {}
    for name in sorted(names):
        # Names are our fixed allowlist. No path supplied by the manifest is used.
        parent = root / name.split("/")[0]
        if parent.is_symlink() or root.is_symlink():
            raise ValueError("SSH_PROVENANCE_FILE_INVALID")
        digest = hashlib.sha256(checked_file(root / name, executable=True)).hexdigest()
        if digest != manifest["files"][name]:
            raise ValueError("SSH_PROVENANCE_HASH_MISMATCH")
        hashes[name] = digest
    licence = checked_file(root / "share/doc/openssh/LICENCE")
    if not licence:
        raise ValueError("SSH_PROVENANCE_LICENCE_MISSING")
    return {
        "type": "application", "name": "openssh-portable", "version": lock["version"],
        "bom-ref": "pkg:generic/openssh-portable@" + lock["version"],
        "purl": "pkg:generic/openssh-portable@" + lock["version"],
        "description": "Upstream OpenSSH client tools, no server; source-built in a separate stage.",
        "externalReferences": [
            {"type": "distribution", "url": lock["archive_url"],
             "hashes": [{"alg": "SHA-256", "content": lock["archive_sha256"]}]},
            {"type": "release-notes", "url": lock["release_notes"]},
        ],
        "properties": [
            {"name": "basswiesn:source-signature-fingerprint", "value": lock["signing_fingerprint"]},
            {"name": "basswiesn:installed-file-sha256", "value": json.dumps(hashes, sort_keys=True)},
            {"name": "basswiesn:licence-sha256", "value": hashlib.sha256(licence).hexdigest()},
            {"name": "basswiesn:security-assessment", "value": "Separate upstream advisory review required; inventory is not a clean scan."},
        ],
    }


def supplement(sbom: dict[str, Any], ssh: dict[str, Any]) -> dict[str, Any]:
    if (sbom.get("bomFormat") != "CycloneDX" or not isinstance(sbom.get("components"), list)
            or ssh.get("name") != "openssh-portable"
            or any(c.get("bom-ref") == ssh.get("bom-ref") for c in sbom["components"])):
        raise ValueError("SSH_SBOM_INPUT_INVALID")
    # Preserve scanner metadata and all existing components. The supplemental
    # entry is visibly generic, never disguised as a patched Debian package.
    result = {**sbom, "components": [*sbom["components"], ssh]}
    parent = result.get("metadata", {}).get("component", {}).get("bom-ref")
    if parent:
        dependencies = [dict(entry) for entry in result.get("dependencies", [])]
        matching = [entry for entry in dependencies if entry.get("ref") == parent]
        if len(matching) > 1:
            raise ValueError("SSH_SBOM_INPUT_INVALID")
        if matching:
            matching[0]["dependsOn"] = sorted(set(matching[0].get("dependsOn", [])) | {ssh["bom-ref"]})
        else:
            dependencies.append({"ref": parent, "dependsOn": [ssh["bom-ref"]]})
        result["dependencies"] = dependencies
    return result


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--root", type=Path, default=Path("/usr/local"))
    parser.add_argument("--lock", type=Path, default=Path(__file__).with_name("openssh-source.json"))
    parser.add_argument("--merge-sbom", type=Path)
    parser.add_argument("--component", type=Path)
    args = parser.parse_args()
    if bool(args.merge_sbom) != bool(args.component):
        parser.error("--merge-sbom and --component must be supplied together")
    if args.merge_sbom:
        result = supplement(json.loads(args.merge_sbom.read_text()), json.loads(args.component.read_text()))
    else:
        result = component(args.root, args.lock)
    print(json.dumps(result, sort_keys=True, indent=2))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
