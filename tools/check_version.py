#!/usr/bin/env python3
"""Offline version contract; never import application startup or contact a device."""
from __future__ import annotations

import argparse
import ast
import json
import re
from pathlib import Path

VERSION_PATTERN = re.compile(r"[0-9]+\.[0-9]+\.[0-9]+(?:[a-z0-9.+-]+)?")
DOCKER_ARGUMENT = re.compile(r"^ARG BASSWIESN_VERSION=(\S+)$", re.MULTILINE)
ENV_VERSION = re.compile(r"^BASSWIESN_VERSION=(\S+)$", re.MULTILINE)


def project_version(root: Path) -> str:
    tree = ast.parse((root / "basswiesn/__init__.py").read_text(encoding="utf-8"))
    versions = [
        node.value.value
        for node in tree.body
        if isinstance(node, ast.Assign)
        and any(isinstance(target, ast.Name) and target.id == "__version__" for target in node.targets)
        and isinstance(node.value, ast.Constant)
        and isinstance(node.value.value, str)
    ]
    if len(versions) != 1 or not VERSION_PATTERN.fullmatch(versions[0]):
        raise ValueError("package must declare exactly one literal release version")
    return versions[0]


def check_version(root: Path, *, manifest: Path | None = None, artifact: Path | None = None) -> list[str]:
    version = project_version(root)
    errors: list[str] = []
    dockerfile = (root / "Dockerfile").read_text(encoding="utf-8")
    if DOCKER_ARGUMENT.findall(dockerfile) != [version]:
        errors.append("Docker version argument differs from basswiesn.__version__")
    if 'org.opencontainers.image.version="${BASSWIESN_VERSION}"' not in dockerfile:
        errors.append("Docker OCI version label must use the checked version argument")
    if "assert __version__ == sys.argv[1]" not in dockerfile:
        errors.append("Docker build must reject a version override that differs from the package")
    # An existing installation may retain its old .env; runtime ignores that
    # version. The distributed template must nevertheless describe this build.
    template = root / ".env.example"
    if not template.is_file() or ENV_VERSION.findall(template.read_text(encoding="utf-8")) != [version]:
        errors.append("distributed environment version differs from basswiesn.__version__")
    if manifest is not None:
        data = json.loads(manifest.read_text(encoding="utf-8"))
        if not isinstance(data, dict) or data.get("version") != version:
            errors.append("release manifest version differs from package version")
    if artifact is not None and artifact.name != f"basswiesn-docker-release-{version}.tar.gz":
        errors.append("versioned release filename differs from package version")
    return errors


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--root", type=Path, default=Path(__file__).resolve().parents[1])
    parser.add_argument("--manifest", type=Path)
    parser.add_argument("--artifact", type=Path)
    args = parser.parse_args()
    try:
        errors = check_version(args.root, manifest=args.manifest, artifact=args.artifact)
    except (OSError, ValueError, SyntaxError) as exc:
        print(f"FAIL: version contract cannot be checked ({type(exc).__name__})")
        return 1
    for error in errors:
        print(f"FAIL: {error}")
    if not errors:
        print(f"PASS: version contract {project_version(args.root)}")
    return int(bool(errors))


if __name__ == "__main__":
    raise SystemExit(main())
