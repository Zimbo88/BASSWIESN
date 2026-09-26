import asyncio

import pytest

from basswiesn.app import db as app_db
from basswiesn.app.services.playback_safety_gate import (
    PlaybackSafetyGateError,
    arm_playback_safety_gate,
    fail_playback_safety_gate,
    load_playback_safety_gate,
    verify_playback_safety_gate,
    wait_for_provider_release,
)


@pytest.mark.parametrize("safe_volume", [0, 1, 5])
def test_provider_release_requires_post_select_volume_and_mute_readback(safe_volume):
    writer = app_db.SessionLocal()
    reader = app_db.SessionLocal()
    try:
        arm_playback_safety_gate(writer, "SAFE-GATE", safe_volume=safe_volume, station_id=7)
        verify_playback_safety_gate(writer, "SAFE-GATE", volume=safe_volume, muted=True)

        result = asyncio.run(wait_for_provider_release(reader, "SAFE-GATE"))

        assert result == {"required": True, "state": "VERIFIED", "volume_readback": safe_volume}
        assert load_playback_safety_gate(reader, "SAFE-GATE")["expired"] is False
    finally:
        reader.close()
        writer.close()


def test_failed_gate_withholds_provider_audio_url():
    writer = app_db.SessionLocal()
    reader = app_db.SessionLocal()
    try:
        arm_playback_safety_gate(writer, "FAILED-GATE", safe_volume=1, station_id=8)
        fail_playback_safety_gate(writer, "FAILED-GATE", "mute readback failed")

        with pytest.raises(PlaybackSafetyGateError, match="mute readback failed"):
            asyncio.run(wait_for_provider_release(reader, "FAILED-GATE"))
    finally:
        reader.close()
        writer.close()


def test_gate_rejects_unmuted_verification():
    db = app_db.SessionLocal()
    try:
        arm_playback_safety_gate(db, "UNMUTED-GATE", safe_volume=1, station_id=9)
        with pytest.raises(PlaybackSafetyGateError, match="safe volume and mute"):
            verify_playback_safety_gate(db, "UNMUTED-GATE", volume=1, muted=False)
    finally:
        db.close()


@pytest.mark.parametrize("armed,observed", [(0, 1), (1, 5), (5, 1)])
def test_readback_cannot_verify_a_different_requested_volume(armed, observed):
    db = app_db.SessionLocal()
    try:
        arm_playback_safety_gate(db, "MISMATCH-GATE", safe_volume=armed, station_id=9)
        with pytest.raises(PlaybackSafetyGateError):
            verify_playback_safety_gate(db, "MISMATCH-GATE", volume=observed, muted=True)
        assert load_playback_safety_gate(db, "MISMATCH-GATE")["state"] == "ARMED"
    finally:
        db.close()


def test_unsolicited_and_failed_gate_cannot_be_verified():
    db = app_db.SessionLocal()
    try:
        with pytest.raises(PlaybackSafetyGateError):
            verify_playback_safety_gate(db, "ABSENT-GATE", volume=1, muted=True)
        arm_playback_safety_gate(db, "FAILED-GATE", safe_volume=1, station_id=9)
        fail_playback_safety_gate(db, "FAILED-GATE", "stopped")
        with pytest.raises(PlaybackSafetyGateError):
            verify_playback_safety_gate(db, "FAILED-GATE", volume=1, muted=True)
    finally:
        db.close()
