#!/usr/bin/env python3
"""Verify hash locks and their reviewed inputs without resolving or networking."""
from __future__ import annotations

import argparse
import hashlib
import json
import re
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
INPUTS = ("requirements.in", "requirements-dev.in", "requirements.lock", "requirements-dev.lock")
LOCKS = ("requirements.lock", "requirements-dev.lock")
PIN = re.compile(r"^([A-Za-z0-9_.-]+)(?:\[[A-Za-z0-9_,.-]+\])?==([^\s;]+)(?:\s*;[^\\]+)?$")
HASH = re.compile(r"--hash=sha256:[0-9a-f]{64}")


def pins(text: str) -> dict[str, set[str]]:
    result: dict[str, set[str]] = {}
    logical = text.replace("\\\n", " ")
    for line in logical.splitlines():
        line = line.strip()
        if not line or line.startswith("#"):
            continue
        if not HASH.search(line):
            raise ValueError("dependency has no SHA256 hashes")
        requirement = HASH.sub("", line).strip()
        match = PIN.fullmatch(requirement)
        if match is None or "@" in requirement or "://" in requirement:
            raise ValueError("dependency is not an exact registry pin")
        name, version = match.groups()
        name = re.sub(r"[-_.]+", "-", name).lower()
        result.setdefault(name, set()).add(version)
    if not result:
        raise ValueError("empty dependency lock")
    return result


def fingerprint(root: Path) -> dict[str, object]:
    return {"schema_version": 1, "generator": "uv 0.13.4",
            "resolution": "universal, Python >=3.11; wheels required at installation",
            "sha256": {name: hashlib.sha256((root / name).read_bytes()).hexdigest() for name in INPUTS}}


def check(root: Path) -> list[str]:
    errors: list[str] = []
    inventories = {name: pins((root / name).read_text(encoding="utf-8")) for name in LOCKS}
    runtime, dev = (inventories[name] for name in LOCKS)
    for name, versions in runtime.items():
        if dev.get(name) != versions:
            errors.append(f"development/runtime version divergence: {name}")
    if {"pytest", "playwright", "ruff", "mypy", "pip-audit", "uv"} & runtime.keys():
        errors.append("development tools leaked into runtime lock")
    recorded = json.loads((root / "dependency-lock.json").read_text(encoding="utf-8"))
    if recorded != fingerprint(root):
        errors.append("dependency inputs/locks changed without a reviewed lock regeneration")
    return errors


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--root", type=Path, default=ROOT)
    parser.add_argument("--record", action="store_true", help="Record deliberately regenerated lock/input hashes")
    args = parser.parse_args()
    try:
        if args.record:
            for name in LOCKS:
                pins((args.root / name).read_text(encoding="utf-8"))
            (args.root / "dependency-lock.json").write_text(
                json.dumps(fingerprint(args.root), indent=2, sort_keys=True) + "\n", encoding="utf-8"
            )
        errors = check(args.root)
    except (ValueError, OSError) as exc:
        print(f"FAIL: lock validation ({type(exc).__name__})")
        return 1
    for error in errors:
        print(f"FAIL: {error}")
    if not errors:
        print("PASS: runtime/development hash-lock contract")
    return int(bool(errors))


if __name__ == "__main__":
    raise SystemExit(main())
