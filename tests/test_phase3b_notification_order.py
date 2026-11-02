from __future__ import annotations

import pytest

from basswiesn.app.routers.stations_presets import classify_notification_sequence


pytestmark = pytest.mark.unit


@pytest.mark.parametrize(
    ("variant", "sequence", "safe_for_full_sync", "write_integrity_verified"),
    [
        ("A", ["storePreset", "readback", "notification", "final_readback"], False, False),
        ("B", ["notification", "storePreset", "readback"], False, False),
        ("C", ["storePreset", "readback"], True, True),
        ("D", ["notification"], False, False),
    ],
)
def test_notification_order_variants_are_classified_without_network(
    variant: str,
    sequence: list[str],
    safe_for_full_sync: bool,
    write_integrity_verified: bool,
):
    result = classify_notification_sequence(sequence)

    assert result["variant"] == variant
    assert result["safe_for_full_sync"] is safe_for_full_sync
    assert result["write_integrity_verified"] is write_integrity_verified
    assert result["sequence"] == sequence


def test_unknown_notification_order_requires_manual_review():
    result = classify_notification_sequence(["readback", "notification"])

    assert result["variant"] == "unbekannt"
    assert result["safe_for_full_sync"] is False
    assert result["write_integrity_verified"] is False
