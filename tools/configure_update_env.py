#!/usr/bin/env python3
"""Explicit local installer setting; preserve a private original, never source it."""
import os
from pathlib import Path
import re
import stat
import sys


def configure(root):
    path = Path(root) / ".env"
    fd = os.open(path, os.O_RDONLY | os.O_NOFOLLOW)
    with os.fdopen(fd, "rb") as source:
        info = os.fstat(source.fileno())
        if not stat.S_ISREG(info.st_mode) or info.st_nlink != 1 or info.st_size > 1024*1024:
            raise ValueError("UNSAFE_ENV")
        before = source.read()
    settings = {"BASSWIESN_UPDATE_CONTROL_DIR": "/run/basswiesn-update",
                "BASSWIESN_ENABLE_HTTPS": "true", "BASSWIESN_HTTPS_PORT": "1329"}
    lines = before.decode("utf-8").splitlines()
    lines = [line for line in lines if not any(re.match(r"^\s*(?:export\s+)?"+key+r"\s*=", line) for key in settings)]
    after = ("\n".join(lines)+"\n"+"".join(k+"="+v+"\n" for k,v in settings.items())).encode()
    backup = Path(root) / ".env.before-update-helper"
    fd = os.open(backup, os.O_WRONLY | os.O_CREAT | os.O_EXCL | os.O_NOFOLLOW, 0o600)
    with os.fdopen(fd, "wb") as target:
        target.write(before); target.flush(); os.fsync(target.fileno())
    # Exclusive temporary; no overwrite/retry of partial setup.
    temp = Path(root) / ".env.update-helper-pending"
    fd = os.open(temp, os.O_WRONLY | os.O_CREAT | os.O_EXCL | os.O_NOFOLLOW, 0o600)
    with os.fdopen(fd, "wb") as target:
        target.write(after); target.flush(); os.fsync(target.fileno())
    current = path.lstat()
    if (current.st_ino, current.st_size, current.st_mtime_ns) != (info.st_ino, info.st_size, info.st_mtime_ns):
        raise ValueError("ENV_CHANGED")
    os.replace(temp, path)


if __name__ == "__main__":
    if sys.argv[1:] != ["--enable-updater"]:
        raise SystemExit("Use --enable-updater from the trusted installation directory")
    configure(Path.cwd())
