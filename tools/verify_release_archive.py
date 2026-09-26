#!/usr/bin/env python3
"""Read-only release checker. Does not download, extract, install or run code."""
import argparse
from dataclasses import asdict
import json
import os
from pathlib import Path
import stat
import sys

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from basswiesn.app.services.release_artifact import ArtifactRejected, verify_release_archive


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("archive", type=Path)
    parser.add_argument("--version", required=True, help="Exact approved version, without v")
    parser.add_argument("--checksums", type=Path, help="Defaults to SHA256SUMS beside the archive")
    args = parser.parse_args()
    checksums = args.checksums or args.archive.parent / "SHA256SUMS"
    try:
        if not hasattr(os, "O_NOFOLLOW"):
            raise ArtifactRejected("UNSUPPORTED_HOST")
        fd = os.open(checksums, os.O_RDONLY | os.O_NOFOLLOW | os.O_NONBLOCK)
        with os.fdopen(fd, "rb") as stream:
            if not stat.S_ISREG(os.fstat(stream.fileno()).st_mode):
                raise ArtifactRejected("CHECKSUM_NOT_REGULAR")
            data = stream.read(4097)
        result = verify_release_archive(args.archive, data, version=args.version)
    except (ArtifactRejected, OSError) as error:
        # Do not print attacker-controlled filenames, JSON/header values or
        # local filesystem exception strings in UI/CI diagnostics.
        print(json.dumps({"status": "rejected", "code": getattr(error, "code", "CHECKSUM_UNREADABLE")}))
        return 1
    print(json.dumps({"status": "verified", **asdict(result)}, sort_keys=True))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
