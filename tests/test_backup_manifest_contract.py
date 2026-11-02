"""Pure format contract used by unit tests and the bounded mutation gate."""
from __future__ import annotations

from copy import deepcopy

import pytest
from hypothesis import given, strategies as st

from basswiesn.app.services.backup_manifest import validate_system_manifest

pytestmark = pytest.mark.unit


def valid_manifest() -> dict:
    return {
        "format": "basswiesn-backup-v1", "schema_baseline": 2,
        "version": "1.5.0", "created_at": "2026-09-27T12:00:00+00:00",
        "quick_check": "ok", "restore_mode": "prepare_and_restart",
        "files": [
            {"path": "database.sqlite", "sha256": "a" * 64},
            {"path": "settings.json", "sha256": "b" * 64},
        ],
    }


def test_existing_v1_format_and_older_release_remain_compatible():
    assert validate_system_manifest(valid_manifest()) == {
        "database.sqlite": "a" * 64, "settings.json": "b" * 64,
    }


@pytest.mark.parametrize("field,value", [
    ("format", "future-backup-v9"), ("schema_baseline", 3), ("schema_baseline", True),
    ("restore_mode", "unrecognized"), ("quick_check", "failed"),
    ("version", ""), ("version", None), ("version", "not-a-version"),
    ("created_at", "2026-09-27T12:00:00"), ("created_at", None),
    ("created_at", "not-a-date"), ("files", []), ("files", {}),
])
def test_unproven_or_incompatible_manifest_is_rejected(field, value):
    with pytest.raises(ValueError):
        validate_system_manifest({**valid_manifest(), field: value})


@pytest.mark.parametrize("value", [None, [], "", 1])
def test_manifest_must_be_an_object(value):
    with pytest.raises(ValueError):
        validate_system_manifest(value)


@pytest.mark.parametrize("field,value", [
    ("path", "../database.sqlite"), ("path", "manifest.json"), ("path", "settings.json"),
    ("path", None), ("sha256", None), ("sha256", "z" * 64),
    ("sha256", "a" * 64 + "suffix"), ("sha256", "a" * 63),
])
def test_component_identity_and_digest_are_strict(field, value):
    manifest = deepcopy(valid_manifest())
    manifest["files"][0][field] = value
    with pytest.raises(ValueError):
        validate_system_manifest(manifest)


@given(st.integers().filter(lambda value: value != 2))
def test_only_supported_schema_can_be_restored(schema):
    with pytest.raises(ValueError):
        validate_system_manifest({**valid_manifest(), "schema_baseline": schema})


@given(st.binary(min_size=32, max_size=32))
def test_valid_sha256_value_is_preserved(digest):
    manifest = deepcopy(valid_manifest())
    manifest["files"][0]["sha256"] = digest.hex()
    assert validate_system_manifest(manifest)["database.sqlite"] == digest.hex()
