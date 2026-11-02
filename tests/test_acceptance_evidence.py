"""An integrity-checked test document is not a hardware capability grant."""

from copy import deepcopy
import hashlib
import json

import pytest
from pydantic import ValidationError

from basswiesn import acceptance

pytestmark = pytest.mark.unit


def record():
    identity = {"device_id": "AA11BB22CC33", "model": "Synthetic SoundTouch", "firmware": "27.0.6.123.456",
                "variant": "EXAMPLE", "platform": "SM2"}
    return {"schema_version": 1, "test_id": "example-volume", "feature": "volume", "version": "3.0.2",
            "origin": "HARDWARE_TESTED", "software_result": "PASS", "hardware_result": "PASS",
            "writes_performed": True, "expected": identity, "observed_start": dict(identity),
            "observed_end": dict(identity), "expected_state": {"volume": 1}, "readback_state": {"volume": 1},
            "restore": "EXACT", "observer": "Synthetic observer", "observed_at": "2026-10-01T16:00:00+00:00",
            "evidence": [{"role": role, "file": role.lower() + ".json", "sha256": "0" * 64}
                         for role in ("START", "END", "COMMAND", "READBACK", "BACKUP", "OBSERVATION")]}


def write_record(tmp_path, value):
    for item in value["evidence"]:
        content = ("synthetic " + item["role"]).encode()
        (tmp_path / item["file"]).write_bytes(content)
        item["sha256"] = hashlib.sha256(content).hexdigest()
    path = tmp_path / "record.json"
    path.write_text(json.dumps(value))
    return path


def test_valid_record_still_needs_human_review_and_never_grants_capability(tmp_path):
    path = write_record(tmp_path, record())
    result = acceptance.validate_record(path)
    assert result["record_validation"] == "PASS"
    assert result["acceptance"] == "MANUAL_REVIEW_REQUIRED"
    assert result["hardware_verified_by_tool"] is False
    assert result["runtime_capability_changed"] is False
    assert "AA11BB22CC33" not in json.dumps(result)
    assert "Synthetic observer" not in json.dumps(result)


@pytest.mark.parametrize("origin", ["SIMULATED", "SOFTWARE_TESTED"])
def test_simulation_cannot_claim_hardware_pass(origin):
    value = record()
    value.update(origin=origin)
    with pytest.raises(ValidationError):
        acceptance.AcceptanceRecord.model_validate(value)
    value.update(hardware_result="NOT_RUN", writes_performed=False)
    assert acceptance.AcceptanceRecord.model_validate(value).hardware_result == "NOT_RUN"


@pytest.mark.parametrize("field,change", [
    ("observed_start", None), ("observed_end", None), ("expected", None), ("observer", None),
    ("observed_at", "2026-10-01T16:00:00"), ("observed_at", "not a date"),
    ("restore", "PARTIAL"), ("restore", "MANUAL_REQUIRED"), ("restore", "NOT_APPLICABLE"),
    ("software_result", "FAIL"), ("readback_state", {}), ("readback_state", {"volume": 20}),
    ("evidence", []), ("hardware_result", "NOT_RUN"),
    ("schema_version", True), ("schema_version", 1.0),
    ("readback_state", {"volume": True}), ("readback_state", {"volume": 1.0}),
])
def test_incomplete_or_diverged_record_is_never_successful(field, change):
    value = record()
    value[field] = change
    with pytest.raises(ValidationError):
        acceptance.AcceptanceRecord.model_validate(value)


@pytest.mark.parametrize("field", ["device_id", "model", "firmware", "variant", "platform"])
def test_each_identity_field_is_exact(field):
    value = record()
    value["observed_end"][field] = "BB22CC33DD44" if field == "device_id" else "Changed"
    with pytest.raises(ValidationError):
        acceptance.AcceptanceRecord.model_validate(value)


@pytest.mark.parametrize("value", ["UNKNOWN", "*", "27.*", "ANY", "?"])
def test_unknown_profile_cannot_support_acceptance(value):
    data = record()
    data["expected"]["firmware"] = value
    with pytest.raises(ValidationError):
        acceptance.AcceptanceRecord.model_validate(data)


def test_failure_is_recorded_without_calling_it_success(tmp_path):
    data = record()
    data.update(hardware_result="FAIL", restore="MANUAL_REQUIRED", readback_state={})
    result = acceptance.validate_record(write_record(tmp_path, data))
    assert result["hardware_result_declared"] == "FAIL"
    assert result["hardware_verified_by_tool"] is False


def test_failed_identity_must_stop_before_write():
    data = record()
    data["observed_start"]["device_id"] = "BB22CC33DD44"
    data.update(hardware_result="FAIL", restore="MANUAL_REQUIRED")
    with pytest.raises(ValidationError):
        acceptance.AcceptanceRecord.model_validate(data)
    data.update(writes_performed=False)
    assert acceptance.AcceptanceRecord.model_validate(data).hardware_result == "FAIL"


def test_tampered_evidence_fails_without_dumping_payload(tmp_path, capsys):
    path = write_record(tmp_path, record())
    (tmp_path / "readback.json").write_text("SYNTHETIC_PRIVATE_DATA")
    assert acceptance.main([str(path)]) == 2
    output = capsys.readouterr().out
    assert "PRIVATE_DATA" not in output and str(tmp_path) not in output
    assert json.loads(output)["record_validation"] == "FAIL"


@pytest.mark.parametrize("name", ["../private.json", "sub/file.json", "/etc/passwd", "a..b.json"])
def test_path_traversal_is_not_evidence(tmp_path, name):
    data = record()
    data["evidence"][0]["file"] = name
    path = tmp_path / "record.json"
    path.write_text(json.dumps(data))
    with pytest.raises((ValueError, ValidationError)):
        acceptance.validate_record(path)


def test_symlink_or_missing_evidence_is_rejected(tmp_path):
    path = write_record(tmp_path, record())
    (tmp_path / "readback.json").unlink()
    (tmp_path / "readback.json").symlink_to(tmp_path / "start.json")
    assert acceptance.main([str(path)]) == 2


def test_duplicate_json_keys_cannot_override_evidence(tmp_path):
    path = tmp_path / "record.json"
    path.write_text('{"hardware_result":"FAIL","hardware_result":"PASS"}')
    assert acceptance.main([str(path)]) == 2


def test_reusing_one_file_for_every_phase_is_rejected():
    data = record()
    data["evidence"][1] = deepcopy(data["evidence"][0])
    with pytest.raises(ValidationError):
        acceptance.AcceptanceRecord.model_validate(data)


def test_offline_software_record_never_infers_hardware(tmp_path, capsys):
    value = {"schema_version": 1, "test_id": "parser", "feature": "DLNA", "version": "3.0.2",
             "origin": "SIMULATED", "software_result": "PASS", "hardware_result": "NOT_RUN", "evidence": []}
    path = write_record(tmp_path, value)
    assert acceptance.main([str(path)]) == 0
    assert json.loads(capsys.readouterr().out)["acceptance"] == "HARDWARE_NOT_RUN"
