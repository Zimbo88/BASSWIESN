"""Offline-only basswiesnctl. No startup, network administration or device writes."""

from __future__ import annotations

import argparse
from collections.abc import Sequence
import json
import os
from pathlib import Path
import sys
from typing import NoReturn

from basswiesn import __version__
from basswiesn.cli_checks import doctor, load_config
from basswiesn.cli_contract import Check, OfflineError, Report
from basswiesn.cli_storage import create_backup, verify_backup, write_json_artifact
from basswiesn.cli_views import devices, health, providers, status


class SafeParser(argparse.ArgumentParser):
    def error(self, message: str) -> NoReturn:
        del message  # argparse's default errors can echo submitted secret values.
        self.print_usage(sys.stderr)
        self.exit(2, "basswiesnctl: invalid arguments; use --help for the supported offline contract.\n")


def _common(parser: argparse.ArgumentParser, *, suppress_defaults: bool = False) -> None:
    def default(value: object) -> object:
        return argparse.SUPPRESS if suppress_defaults else value

    parser.add_argument("--json", action="store_true", default=default(False), help="emit the versioned JSON contract")
    parser.add_argument("--data-dir", type=Path, default=default(Path("data")), help="offline installation data directory (default: ./data)")
    parser.add_argument("--database", type=Path, default=default(None), help="stopped/checkpointed application SQLite snapshot (default: DATA_DIR/basswiesn.db)")
    parser.add_argument("--config", type=Path, default=default(None), help="explicit literal dotenv file; environment values override it; never shell-evaluated")


def parser() -> argparse.ArgumentParser:
    result = SafeParser(prog="basswiesnctl", description="Offline local administration only. Never starts services, migrates, discovers, contacts devices, repairs or restores.")
    _common(result)
    commands = result.add_subparsers(dest="command", required=True)
    help_text = {
        "status": "inspect the local database snapshot, not running-service liveness",
        "doctor": "read-only PASS/WARN/FAIL preflight and remediation",
        "devices": "list bounded cached devices (hashed identifiers by default)",
        "health": "read bounded cached health history; never run live checks",
        "backup": "create a sensitive private v1 archive from a stopped database",
        "verify-backup": "verify ZIP contract, hashes and SQLite without restoring",
        "diagnostics": "produce allowlisted, redacted offline diagnostics without logs/settings",
        "providers": "list static provider contracts, not live support claims",
        "version": "show the canonical source version",
    }
    for name, description in help_text.items():
        command = commands.add_parser(name, help=description, description=description)
        _common(command, suppress_defaults=True)
        if name in {"devices", "health", "diagnostics"}:
            command.add_argument("--limit", type=int, default=100, help="maximum cached rows, 1..500 (default: 100)")
        if name == "devices":
            command.add_argument("--include-identifiers", action="store_true", help="include local device IDs/IPs; names, URLs and credentials remain excluded")
        if name in {"backup", "diagnostics"}:
            command.add_argument("--output", type=Path, required=name == "backup", help="new filename under an owned 0700 directory; never overwrites")
        if name == "backup":
            command.add_argument("--offline-confirmed", action="store_true", help="confirm all source writers are stopped or this is an isolated checkpointed snapshot")
        if name == "verify-backup":
            command.add_argument("archive", type=Path)
    return result


def _execute(args: argparse.Namespace) -> Report:
    command = str(args.command)
    if command == "version":
        return Report(command, data={"version": __version__, "output_schema_version": 1, "network_support": False})
    if command == "providers":
        return providers()
    if command == "verify-backup":
        payload = verify_backup(args.archive)
        checks = [Check("backup.verified", "PASS", "Archive hashes and SQLite integrity verified; no restore occurred.")]
        if payload["compatibility"] != "STRUCTURAL_MATCH":
            checks.append(Check("backup.compatibility", "WARN", "Archive version or declared schema compatibility is not confirmed.", "Use the documented upgrade/restore review; hashes alone never authorize restore."))
        return Report(command, checks, payload)
    limit = getattr(args, "limit", 100)
    if not 1 <= limit <= 500:
        raise OfflineError("arguments.limit", "The row limit must be between 1 and 500.", "Choose a bounded --limit value.")
    config = load_config(args.data_dir, args.database, args.config, os.environ)
    if command == "doctor":
        return doctor(config)
    if command == "status":
        return status(config)
    if command == "devices":
        return devices(config, limit=limit, identifiers=args.include_identifiers)
    if command == "health":
        return health(config, limit=limit)
    if command == "backup":
        if not args.offline_confirmed:
            raise OfflineError("backup.confirmation", "Backup requires explicit confirmation of an offline/checkpointed source.", "Stop all writers or select an isolated checkpointed snapshot, then pass --offline-confirmed.")
        payload = create_backup(config.database, args.output)
        return Report(command, [Check("backup.created", "PASS", "A verified private backup was published at the requested new filename.")], payload)
    report = doctor(config, command="diagnostics")
    if report.status != "FAIL":
        report.data["devices"] = devices(config, limit=limit, identifiers=False).data
        report.data["health"] = health(config, limit=limit).data
    report.data["providers"] = providers().data
    report.data["excluded"] = ["logs", "settings_values", "credentials", "authentication_database", "device_names", "device_addresses", "raw_errors", "environment_values"]
    if args.output is not None:
        write_json_artifact(args.output, report.as_dict())
        report.data["output_written"] = True
    return report


def _render(report: Report, *, as_json: bool) -> str:
    if as_json:
        return json.dumps(report.as_dict(), ensure_ascii=True, sort_keys=True)
    lines = [f"BASSWIESN {__version__} | {report.command} | {report.status} | OFFLINE"]
    for check in report.checks:
        lines.append(f"{check.status} {check.code}: {check.message}")
        if check.remediation:
            lines.append(f"  Next: {check.remediation}")
    if report.data:
        lines.append(json.dumps(report.data, indent=2, ensure_ascii=True, sort_keys=True))
    return "\n".join(lines)


def main(argv: Sequence[str] | None = None) -> int:
    args = parser().parse_args(argv)
    try:
        report = _execute(args)
    except OfflineError as exc:
        report = Report(str(args.command))
        report.failure(exc)
    except (OSError, ValueError, TypeError, RecursionError):
        # Raw exceptions can contain filenames, config/SQL values or secrets.
        report = Report(str(args.command))
        report.failure(OfflineError("local.operation_failed", "The local operation could not complete safely.", "Check selected paths, private permissions, available space and snapshot format; no device was contacted."))
    except KeyboardInterrupt:
        return 130
    try:
        print(_render(report, as_json=bool(args.json)))
    except BrokenPipeError:
        return 0
    return report.exit_code


if __name__ == "__main__":
    sys.exit(main())
