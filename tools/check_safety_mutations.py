#!/usr/bin/env python3
"""Bounded failure-detection gate; no source edits or hardware interaction.

Each target runs in a fresh child interpreter. Its real tests must pass on the
original and detect every seeded behavioral fault, not an import/setup error.
This is deliberately not an exhaustive application mutation score.
"""
from __future__ import annotations

from dataclasses import dataclass
import importlib
import os
from pathlib import Path
import subprocess
import sys
import tempfile
from xml.etree import ElementTree as ET

ROOT = Path(__file__).resolve().parents[1]


@dataclass(frozen=True)
class Target:
    module: str
    tests: tuple[str, ...]
    mutations: dict[str, tuple[str, str]]


TARGETS = {
    "ingress": Target("basswiesn.app.core.ingress_budget", ("tests/test_ingress_shared_budget.py",), {
        "restart-replenishes-tokens": (
            'tokens, observed = state["tokens"], state["observed"]',
            'tokens, observed = self.burst, state["observed"]',
        ),
        "backward-clock-refills": (
            'if now < observed:', 'if False:',
        ),
    }),
    "restore": Target("basswiesn.app.services.backup_restore", ("tests/test_backup_integrity.py",), {
        "corrupt-safety-copy-accepted": (
            'if not safety_preview["ok"] or safety_preview["manifest"] != safety.get("manifest"):',
            'if False:',
        ),
        "backup-not-synchronized": (
            '_sync_file(tmp_path)', 'pass',
        ),
    }),
    "station": Target("basswiesn.app.services.station_playback", ("tests/test_station_playback_safety.py",), {
        "changed-backup-accepted": (
            'or sha256(saved.content.encode()).hexdigest() != digest', 'or False',
        ),
        "volume-side-effect-ignored": (
            'if preserves_source and observed != prior and not correcting and not own_progress and not (',
            'if False and observed != prior and not correcting and not own_progress and not (',
        ),
    }),
    "reconnect": Target("basswiesn.app.services.live_radio_reconnect", ("tests/test_live_radio_reconnect_260.py",), {
        "changed-start-accepted": (
            'if not start.same_state(RebootSnapshot.parse(latest, device_id)):', 'if False:',
        ),
        "wrong-account-readback-accepted": (
            'if now.same_content(desired) and now.play_status == "PLAY_STATE":',
            'if now.play_status == "PLAY_STATE":',
        ),
    }),
    "preset-apply": Target("basswiesn.app.services.preset_writer", ("tests/test_preset_apply_safety.py",), {
        "stale-artwork-start-accepted": (
            'if expected_before is not None and before.slots != expected_before.slots:', 'if False:',
        ),
        "clone-source-precondition-removed": (
            'previous = local_version(db, device.device_id)',
            'precondition = None\n        previous = local_version(db, device.device_id)',
        ),
    }),
    "preset-recovery": Target("basswiesn.app.services.preset_recovery", ("tests/test_preset_recovery_safety.py",), {
        "same-location-wrong-content-accepted": (
            'return actual.slots.get(row.button) == desired.slots[row.button], requested',
            'return row.button in actual.slots, requested',
        ),
        "delete-collateral-change-ignored": (
            'if actual.without(button) != before.without(button):', 'if False:',
        ),
    }),
    "manual-command": Target("basswiesn.app.services.manual_radio_commands", ("tests/test_manual_radio_commands.py",), {
        "duplicate-operation-replayed": ('if previous is not None:', 'if False:'),
        "changed-start-accepted": (
            'or sha256(row.content.encode()).hexdigest() != digest', 'or False',
        ),
    }),
    "backup": Target("basswiesn.app.services.backup_manifest", ("tests/test_backup_manifest_contract.py",), {
        "format-not-checked": ('if value.get("format") != "basswiesn-backup-v1":', "if False:"),
        "schema-not-checked": ('if type(value.get("schema_baseline")) is not int or value["schema_baseline"] != 2:', "if False:"),
        "restore-mode-not-checked": ('if value.get("restore_mode") != "prepare_and_restart":', "if False:"),
        "failed-integrity-accepted": ('if value.get("quick_check") != "ok":', "if False:"),
        "version-not-checked": ('if not isinstance(version, str) or not re.fullmatch(r"\\d+\\.\\d+\\.\\d+(?:[-+][A-Za-z0-9.-]+)?", version):', "if False:"),
        "timezone-not-checked": ("if timestamp.tzinfo is None:", "if False:"),
        "digest-suffix-accepted": ('re.fullmatch(r"[0-9a-f]{64}", digest)', 're.match(r"[0-9a-f]{64}", digest)'),
    }),
    "network": Target("basswiesn.app.services.network_security", ("tests/test_network_edge_contract.py",), {
        "approved-origin-not-checked": (
            "if type(validation.port) is not int or (parsed.scheme, effective_port) != (validation.scheme, validation.port):",
            "if False:",
        ),
        "nonpublic-address-accepted": ("or not address.is_global", "or False"),
    }),
    "lease": Target("basswiesn.app.services.setup_rebuild.lease", ("tests/test_setup_lease_safety.py",), {
        "same-owner-different-job": (
            "and_(Lease.owner_id == owner_id, Lease.job_id == job_id, Lease.expires_at > now)",
            "and_(Lease.owner_id == owner_id, Lease.expires_at > now)",
        ),
        "expired-heartbeat-revived": (
            'update(Lease).where(Lease.lease_key == "global", Lease.owner_id == owner_id,\n'
            '                            Lease.job_id == job_id, Lease.expires_at > now)',
            'update(Lease).where(Lease.lease_key == "global", Lease.owner_id == owner_id,\n'
            '                            Lease.job_id == job_id)',
        ),
        "release-replacement-owner": (
            'update(Lease).where(Lease.lease_key == "global", Lease.owner_id == owner_id, Lease.job_id == job_id)',
            'update(Lease).where(Lease.lease_key == "global")',
        ),
        "lost-lease-ignored": ("if self.lost or not owned:", "if False:"),
    }),
    "group": Target("basswiesn.app.services.cloud_groups", ("tests/test_cloud_group_boundary.py",), {
        "other-account-member-accepted": (
            'device is None or account_from_info(device.info_xml or "") != account',
            "device is None",
        ),
        "concurrent-update-overwritten": (
            "update(Setting).where(Setting.key == key, Setting.value == raw).values(value=encoded)",
            "update(Setting).where(Setting.key == key).values(value=encoded)",
        ),
    }),
    "selection": Target("basswiesn.app.services.direct_selection", ("tests/test_direct_select_transaction.py",), {
        "unavailable-source-accepted": (
            'if len(matches) != 1 or matches[0].get("status") != "READY":', "if False:",
        ),
        "wrong-selection-readback-accepted": (
            'if not actual.same_content(desired) or after_zone != zone or after_volume != volume:', "if False:",
        ),
    }),
    "reset": Target("basswiesn.app.services.factory_reset", ("tests/test_factory_reset_outcomes.py",), {
        "altered-backup-accepted": (
            'if len(data) != info.st_size or sha256(data).hexdigest() != digest:', "if False:",
        ),
        "changed-hardware-accepted": (
            'if str(identity.get(field, "")).strip().casefold() != str(getattr(candidate, field)).strip().casefold():',
            "if False:",
        ),
    }),
    "update-probe": Target("basswiesn.update_helper.candidate_runtime", ("tests/test_runtime_probe.py",), {
        "optimized-auth-fallback-accepted": (
            'if getattr(get_settings(),"admin_auth_mode","legacy")!="legacy": raise ValueError("RUNTIME_PROBE_UNVERIFIED")',
            'assert getattr(get_settings(),"admin_auth_mode","legacy")=="legacy"',
        ),
        "failed-legacy-worker-accepted": (
            'if not condition: raise ValueError("RUNTIME_PROBE_UNVERIFIED")',
            'if False: raise ValueError("RUNTIME_PROBE_UNVERIFIED")',
        ),
    }),
    "maintenance": Target("basswiesn.app.services.maintenance_reboot", ("tests/test_maintenance_reboot_safety.py",), {
        "wrong-final-state-accepted": (
            'if not before.same_state(actual):', 'if False:',
        ),
        "live-identity-not-checked": (
            'verify_settings_identity(device, raw["/info"])\n                for path in PATHS:',
            'None\n                for path in PATHS:',
        ),
    }),
    "keys": Target("basswiesn.app.services.key_actions", ("tests/test_key_action_safety.py",), {
        "wrong-preset-accepted": (
            'return playing and actual.same_content(desired)', 'return playing',
        ),
        "live-identity-not-checked": (
            '_root(raw["/info"], "info", item.device_id)\n                    verify_settings_identity(item, raw["/info"])',
            '_root(raw["/info"], "info", item.device_id)\n                    None',
        ),
        "changed-backup-accepted": (
            'if row is None or row.device_id != identity or sha256(row.content.encode()).hexdigest() != digest:',
            'if row is None:',
        ),
    }),
    "power": Target("basswiesn.app.services.device_power", ("tests/test_power_action_safety.py",), {
        "uncertain-power-marked-verified": (
            'if lost_reply or action == "low_power_standby":', 'if False:',
        ),
        "changed-power-backup-accepted": (
            'if stored is None or stored.device_id != identity or sha256(stored.content.encode()).hexdigest() != digest:',
            'if stored is None:',
        ),
    }),
}


def child(target_name: str, name: str, report: str) -> int:
    import pytest

    target = TARGETS[target_name]
    module = importlib.import_module(target.module)
    path = ROOT / (target.module.replace(".", "/") + ".py")
    source = path.read_text(encoding="utf-8")
    if name != "control":
        old, new = target.mutations[name]
        if source.count(old) != 1:
            raise RuntimeError("Mutation anchor changed: review the contract and fault seed")
        source = source.replace(old, new, 1)
    exec(compile(source, str(path), "exec"), module.__dict__)
    return int(pytest.main(["-q", "--tb=no", "-o", "tmp_path_retention_policy=failed",
                           *target.tests, f"--junitxml={report}"]))


def expected_outcome(name: str, returncode: int, tests: int, errors: int, failures: int) -> bool:
    if tests <= 0 or errors:
        return False
    return (returncode == 0 and failures == 0) if name == "control" else (returncode == 1 and failures > 0)


def main() -> int:
    if len(sys.argv) == 5 and sys.argv[1] == "--child":
        return child(sys.argv[2], sys.argv[3], sys.argv[4])
    if len(sys.argv) == 3 and sys.argv[1] == "--target" and sys.argv[2] in TARGETS:
        targets = {sys.argv[2]: TARGETS[sys.argv[2]]}
    elif len(sys.argv) == 1:
        targets = TARGETS
    else:
        raise SystemExit("Usage: python tools/check_safety_mutations.py [--target NAME]")
    env = {**os.environ, "PYTHONPATH": str(ROOT)}
    with tempfile.TemporaryDirectory(prefix="basswiesn-mutations-") as directory:
        for target_name, target in targets.items():
            for name in ("control", *target.mutations):
                report = Path(directory) / f"{target_name}-{name}.xml"
                try:
                    result = subprocess.run(
                        [sys.executable, str(Path(__file__).resolve()), "--child", target_name, name, str(report)],
                        cwd=ROOT, env=env, capture_output=True, text=True, timeout=90, check=False,
                    )
                    suites = ET.parse(report).getroot().findall("testsuite")
                    tests = sum(int(suite.attrib.get("tests", "0")) for suite in suites)
                    errors = sum(int(suite.attrib.get("errors", "0")) for suite in suites)
                    failures = sum(int(suite.attrib.get("failures", "0")) for suite in suites)
                except (OSError, subprocess.TimeoutExpired, ET.ParseError, ValueError):
                    print(f"FAIL {target_name}/{name}: runner or test infrastructure failed")
                    return 1
                if not expected_outcome(name, result.returncode, tests, errors, failures):
                    print(f"FAIL {target_name}/{name}: unexpected outcome "
                          f"(tests={tests}, errors={errors}, failures={failures}, exit={result.returncode})")
                    return 1
                print(f"PASS {target_name}/{name}: "
                      f"{'original contract passes' if name == 'control' else 'fault detected by tests'}", flush=True)
    total = sum(len(target.mutations) for target in targets.values())
    scope = "full" if targets is TARGETS else "targeted " + ", ".join(targets)
    print(f"Mutation smoke gate PASS ({scope}): {total} seeded faults detected; no hardware validation")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
