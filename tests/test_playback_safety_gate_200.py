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


def test_provider_release_requires_post_select_volume_and_mute_readback():
    writer = app_db.SessionLocal()
    reader = app_db.SessionLocal()
    try:
        arm_playback_safety_gate(writer, "SAFE-GATE", safe_volume=1, station_id=7)
        verify_playback_safety_gate(writer, "SAFE-GATE", volume=1, muted=True)

        result = asyncio.run(wait_for_provider_release(reader, "SAFE-GATE"))

        assert result == {"required": True, "state": "VERIFIED", "volume_readback": 1}
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
        with pytest.raises(PlaybackSafetyGateError, match="volume 1 and mute"):
            verify_playback_safety_gate(db, "UNMUTED-GATE", volume=1, muted=False)
    finally:
        db.close()
