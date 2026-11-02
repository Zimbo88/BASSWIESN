from datetime import UTC, datetime

import pytest

from basswiesn.app.models import (
    DeviceCapabilities,
    PresetDescriptor,
    SourceDescriptor,
    StreamDescriptor,
    ZoneMember,
    ZoneState,
)


def test_device_capabilities_preserve_unknown_separately_from_false() -> None:
    value = DeviceCapabilities(sources=("AIRPLAY",), supports_airplay=None, has_clock=False)
    assert value.to_dict() == {
        "sources": ["AIRPLAY"],
        "has_display": None,
        "has_clock": False,
        "has_battery": None,
        "supports_multiroom": None,
        "supports_bluetooth": None,
        "supports_airplay": None,
        "metadata_fields": [],
    }


def test_source_account_and_stream_secrets_are_redacted_at_api_boundary() -> None:
    source = SourceDescriptor(
        source_id="LOCAL_INTERNET_RADIO",
        source_account="account-123",
        provider_id="bmx",
        available=True,
    )
    stream = StreamDescriptor(url="https://token@example.test/live.mp3?sig=secret#frag")
    assert source.to_dict()["source_account"] == "<redacted>"
    assert stream.to_dict()["url"] == "https://example.test/live.mp3"
    assert stream.to_dict(redact=False)["url"].endswith("sig=secret#frag")


def test_stream_descriptor_rejects_non_http_and_negative_timeouts() -> None:
    with pytest.raises(ValueError):
        StreamDescriptor(url="file:///etc/passwd")
    with pytest.raises(ValueError):
        StreamDescriptor(url="https://example.test/a", connection_timeout_s=-1)


def test_preset_and_zone_keep_volume_separate_from_topology() -> None:
    preset = PresetDescriptor(
        slot=1,
        source="LOCAL_INTERNET_RADIO",
        source_account="private-account",
        location="https://example.test/station?id=secret",
        normalized_at=datetime(2026, 8, 14, tzinfo=UTC),
    )
    member = ZoneMember(
        device_id="MEMBER",
        ip_address="192.0.2.20",
        registered=True,
        connected=True,
        volume=17,
        output_latency_ms=40,
    )
    zone = ZoneState(master_device_id="MASTER", members=(member,))
    data = zone.to_dict()
    assert data["members"][0]["volume"] == 17
    assert data["members"][0]["output_latency_ms"] == 40
    assert data["members"][0]["ip_address"] == "<redacted>"
    assert preset.to_dict()["source_account"] == "<redacted>"
    assert preset.to_dict()["location"] == "https://example.test/station"


def test_uint8_preset_and_volume_ranges_are_validated() -> None:
    with pytest.raises(ValueError):
        PresetDescriptor(slot=256, source="A", location="https://example.test")
    with pytest.raises(ValueError):
        ZoneMember(device_id="MEMBER", volume=101)
