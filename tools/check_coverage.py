#!/usr/bin/env python3
"""Measured line/branch coverage ratchet. No synthetic hardware coverage."""
from __future__ import annotations

import argparse
import json
from pathlib import Path
from typing import TypedDict, cast


class Counts(TypedDict):
    covered_lines: int
    num_statements: int
    covered_branches: int
    num_branches: int


class CoverageFile(TypedDict):
    summary: Counts


class CoverageReport(TypedDict):
    totals: Counts
    files: dict[str, CoverageFile]


class Minimum(TypedDict):
    line: float
    branch: float


class Baseline(TypedDict):
    minimums: dict[str, Minimum]


def percentages(counts: Counts) -> Minimum:
    # Declarative registries can have statements but no branches. An empty
    # branch set has no uncovered outcomes; adding a branch later must still
    # activate the existing threshold, rather than exempting the module.
    return {"line": 100 * counts["covered_lines"] / counts["num_statements"] if counts["num_statements"] else 100.0,
            "branch": 100 * counts["covered_branches"] / counts["num_branches"] if counts["num_branches"] else 100.0}


def check(report: CoverageReport, baseline: Baseline) -> list[str]:
    failures: list[str] = []
    for name, minimum in baseline["minimums"].items():
        if name == "global":
            counts = report["totals"]
        elif name not in report["files"]:
            failures.append(f"missing measured coverage: {name}")
            continue
        else:
            counts = report["files"][name]["summary"]
        measured = percentages(counts)
        for dimension, observed, threshold in (
            ("line", measured["line"], minimum["line"]),
            ("branch", measured["branch"], minimum["branch"]),
        ):
            if observed + 0.000001 < threshold:
                failures.append(f"{name}: {dimension} {observed:.2f}% < {threshold:.2f}%")
    if not baseline["minimums"] or "global" not in baseline["minimums"]:
        failures.append("coverage baseline must include global measurements")
    return failures


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("report", type=Path)
    parser.add_argument("--baseline", type=Path, default=Path(__file__).resolve().parents[1] / "coverage-baseline.json")
    args = parser.parse_args()
    try:
        report = cast(CoverageReport, json.loads(args.report.read_text(encoding="utf-8")))
        baseline = cast(Baseline, json.loads(args.baseline.read_text(encoding="utf-8")))
        failures = check(report, baseline)
    except (OSError, ValueError, KeyError, TypeError) as exc:
        print(f"FAIL: invalid coverage evidence ({type(exc).__name__})")
        return 1
    for message in failures:
        print(f"FAIL: {message}")
    if not failures:
        totals = percentages(report["totals"])
        print(f"PASS: measured coverage ratchet; lines {totals['line']:.2f}%, branches {totals['branch']:.2f}%")
    return int(bool(failures))


if __name__ == "__main__":
    raise SystemExit(main())
