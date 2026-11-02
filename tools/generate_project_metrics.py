#!/usr/bin/env python3
"""Collect deterministic project metrics without running hardware tests."""

from __future__ import annotations

import argparse
import json
import re
import subprocess
import sys
from datetime import UTC, datetime
from pathlib import Path
from typing import Any, Callable

MARKERS = ("unit", "integration", "browser", "slow", "release", "hardware")
COUNT_RE = re.compile(r"(?:(?P<selected>\d+)/)?(?P<count>\d+)\s+tests?\s+collected")
DURATION_RE = re.compile(r"in\s+(?P<seconds>[0-9]+(?:\.[0-9]+)?)s")


def parse_collected_count(output: str) -> int:
    matches = list(COUNT_RE.finditer(output))
    if not matches:
        return 0
    match = matches[-1]
    return int(match.group("selected") or match.group("count"))


def parse_duration(output: str) -> float | None:
    matches = list(DURATION_RE.finditer(output))
    return float(matches[-1].group("seconds")) if matches else None


def run_command(command: list[str], root: Path) -> subprocess.CompletedProcess[str]:
    return subprocess.run(
        command,
        cwd=root,
        text=True,
        capture_output=True,
        check=False,
    )


def git_value(root: Path, command: list[str], default: str = "") -> str:
    result = run_command(["git", *command], root)
    return result.stdout.strip() if result.returncode == 0 else default


def source_metrics(root: Path) -> dict[str, Any]:
    modules = []
    for path in sorted((root / "basswiesn/app").rglob("*.py")):
        if "__pycache__" in path.parts:
            continue
        lines = len(path.read_text(encoding="utf-8").splitlines())
        modules.append({"file": path.relative_to(root).as_posix(), "lines": lines})
    modules.sort(key=lambda item: (-item["lines"], item["file"]))
    return {
        "python_files": len(modules),
        "backend_lines": sum(item["lines"] for item in modules),
        "largest_modules": modules[:10],
        "documentation_files": len(list((root / "docs").rglob("*.md"))),
        "test_files": len(list((root / "tests").glob("test_*.py"))),
    }


def metrics_from_outputs(
    root: Path,
    *,
    generated_at: str,
    collected_output: str,
    marker_outputs: dict[str, str],
    git_commit: str,
    dirty: bool,
    target_runtimes: list[dict[str, Any]] | None = None,
) -> dict[str, Any]:
    counts = {"total": parse_collected_count(collected_output)}
    counts.update({marker: parse_collected_count(marker_outputs.get(marker, "")) for marker in MARKERS})
    return {
        "schema_version": 1,
        "generated_at_utc": generated_at,
        "git": {"commit": git_commit, "dirty": dirty},
        "tests": {"counts": counts},
        "source": source_metrics(root),
        "target_runtimes": target_runtimes or [],
        "collection_only": True,
        "hardware_tests_started": False,
        "generator": "tools/generate_project_metrics.py",
    }


def collect_project_metrics(root: Path, command_runner: Callable[[list[str], Path], subprocess.CompletedProcess[str]] = run_command) -> dict[str, Any]:
    base = [sys.executable, "-m", "pytest", "--collect-only", "-q"]
    collected = command_runner(base, root)
    marker_outputs: dict[str, str] = {}
    for marker in MARKERS:
        result = command_runner(base + ["-m", marker], root)
        marker_outputs[marker] = result.stdout + result.stderr
    return metrics_from_outputs(
        root,
        generated_at=datetime.now(UTC).isoformat(),
        collected_output=collected.stdout + collected.stderr,
        marker_outputs=marker_outputs,
        git_commit=git_value(root, ["rev-parse", "HEAD"]),
        dirty=bool(git_value(root, ["status", "--porcelain"])),
    )


def collect_target_runtimes(root: Path, targets: list[str], command_runner: Callable[[list[str], Path], subprocess.CompletedProcess[str]]) -> list[dict[str, Any]]:
    result: list[dict[str, Any]] = []
    for target in targets:
        completed = command_runner([sys.executable, "-m", "pytest", target, "-q", "--tb=short"], root)
        output = completed.stdout + completed.stderr
        result.append({
            "target": target,
            "returncode": completed.returncode,
            "seconds": parse_duration(output),
        })
    return result


def write_outputs(root: Path, metrics: dict[str, Any]) -> None:
    generated = root / "docs/generated"
    generated.mkdir(parents=True, exist_ok=True)
    (generated / "project-metrics.json").write_text(
        json.dumps(metrics, ensure_ascii=True, indent=2, sort_keys=True) + "\n",
        encoding="utf-8",
    )
    counts = metrics["tests"]["counts"]
    source = metrics["source"]
    lines = [
        "# Project Metrics",
        "",
        f"Erzeugt: {metrics['generated_at_utc']}",
        f"Git: {metrics['git']['commit']} ({'dirty' if metrics['git']['dirty'] else 'clean'})",
        "",
        "| Metrik | Wert |",
        "|---|---:|",
        f"| Tests gesammelt | {counts['total']} |",
    ]
    for marker in MARKERS:
        lines.append(f"| {marker} | {counts[marker]} |")
    lines.extend([
        f"| Python-Dateien im Backend | {source['python_files']} |",
        f"| Backend-Zeilen | {source['backend_lines']} |",
        f"| Testdateien | {source['test_files']} |",
        f"| Markdown-Dokumente | {source['documentation_files']} |",
        "",
        "Hardwaretests wurden durch dieses Werkzeug nicht gestartet.",
        "Die Markerwerte stammen aus pytest-Collection-only-Aufrufen.",
        "",
    ])
    (generated / "PROJECT_METRICS.md").write_text("\n".join(lines), encoding="utf-8")


def main() -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument("--root", type=Path, default=Path(__file__).resolve().parents[1])
    parser.add_argument("--write", action="store_true")
    parser.add_argument("--target", action="append", default=[], help="optional explicit pytest target to execute once")
    args = parser.parse_args()
    root = args.root.resolve()
    metrics = collect_project_metrics(root)
    if args.target:
        metrics["target_runtimes"] = collect_target_runtimes(root, args.target, run_command)
    if args.write:
        write_outputs(root, metrics)
    else:
        print(json.dumps(metrics, ensure_ascii=True, indent=2, sort_keys=True))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
