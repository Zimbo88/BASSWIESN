#!/usr/bin/env python3
"""Check local Markdown file destinations in a staged package, without networking.

This is a file-link gate, not a CommonMark renderer or an external URL/anchor
checker. Inline/image links and reference definitions are covered. Code fences
are examples, not navigation. Never repair a package by copying arbitrary links.
"""
from __future__ import annotations

import argparse
from pathlib import Path
import re
from urllib.parse import unquote, urlsplit

LINK = re.compile(r"!?\[[^\]\n]*\]\(\s*(<[^>\n]+>|[^\s)]+)(?:\s+[^)\n]*)?\)")
REFERENCE = re.compile(r"^\s{0,3}\[[^\]\n]+\]:\s*(<[^>\n]+>|\S+)", re.M)
FENCE = re.compile(r"^\s{0,3}(`{3,}|~{3,})")


def prose(text: str) -> str:
    lines: list[str] = []
    fence = ""
    for line in text.splitlines():
        match = FENCE.match(line)
        if match:
            marker = match[1]
            if not fence:
                fence = marker
            elif marker[0] == fence[0] and len(marker) >= len(fence):
                fence = ""
            continue
        if not fence:
            lines.append(line)
    return "\n".join(lines)


def check_links(root: Path) -> list[str]:
    root = root.resolve(strict=True)
    errors: list[str] = []
    for document in sorted(root.rglob("*.md")):
        if document.is_symlink():
            errors.append(f"{document.relative_to(root)}: symlinked documentation")
            continue
        text = prose(document.read_text(encoding="utf-8"))
        targets = [m[1].strip("<>") for pattern in (LINK, REFERENCE) for m in pattern.finditer(text)]
        for target in targets:
            try:
                url = urlsplit(target)
                if url.scheme or url.netloc or not url.path:
                    continue
                destination = (document.parent / unquote(url.path)).resolve()
                if not destination.is_relative_to(root) or not destination.is_file():
                    errors.append(f"{document.relative_to(root)}: missing or out-of-package file: {target}")
            except ValueError:
                errors.append(f"{document.relative_to(root)}: invalid link")
    return errors


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("root", type=Path, help="Staged package directory, not a live installation")
    args = parser.parse_args()
    try:
        errors = check_links(args.root)
    except (OSError, UnicodeError, ValueError) as exc:
        print(f"FAIL: package links unreadable ({type(exc).__name__})")
        return 1
    for error in errors:
        print(f"FAIL: {error}")
    if not errors:
        print("PASS: packaged Markdown file links (external URLs and anchors not checked)")
    return int(bool(errors))


if __name__ == "__main__":
    raise SystemExit(main())
