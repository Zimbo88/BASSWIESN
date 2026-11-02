"""BMX inactivity timer lifecycle is anchored to Play, never response receipt."""

from datetime import UTC, datetime, timedelta

import pytest

from basswiesn.app import db as app_db
from basswiesn.app.models import RestrictionState
from basswiesn.app.repositories.research_state_repository import ResearchStateRepository
from basswiesn.app.services.restrictions import UINT64_MAX, parse_restrictions


pytestmark = pytest.mark.integration


def _row(db, device_id="TIMER-DEVICE", source_key="LOCAL_INTERNET_RADIO:station"):
    return db.query(RestrictionState).filter(
        RestrictionState.device_id == device_id,
        RestrictionState.source_key == source_key,
    ).one()


def test_delayed_play_replay_pause_stop_and_provider_refresh_timer_contract():
    received = datetime(2030, 1, 1, 12, 0, tzinfo=UTC)
    first_play = received + timedelta(seconds=90)
    replay = first_play + timedelta(seconds=40)
    db = app_db.SessionLocal()
    repository = ResearchStateRepository(db)

    parsed = parse_restrictions(
        {"restrictions": {"inactivityTimeout": 300}},
        received_at=received,
        source="BMX.Station",
    )
    repository.upsert_restrictions("TIMER-DEVICE", "LOCAL_INTERNET_RADIO:station", parsed)
    db.commit()
    row = _row(db)
    assert row.received_at.replace(tzinfo=UTC) == received
    assert row.timer_started_at is None
    assert row.effective_until is None

    repository.set_restriction_timer(
        "TIMER-DEVICE",
        "LOCAL_INTERNET_RADIO:station",
        play_started_at=first_play,
        reason="delayed_play_readback",
    )
    db.commit()
    row = _row(db)
    assert row.timer_started_at.replace(tzinfo=UTC) == first_play
    assert row.effective_until.replace(tzinfo=UTC) == first_play + timedelta(seconds=300)

    # A repeated provider response is not a Play and must not renew the timer.
    refreshed = parse_restrictions(
        {"restrictions": {"inactivityTimeout": 300}},
        received_at=received + timedelta(seconds=120),
        source="BMX.Station",
    )
    repository.upsert_restrictions("TIMER-DEVICE", "LOCAL_INTERNET_RADIO:station", refreshed)
    db.commit()
    assert _row(db).effective_until.replace(tzinfo=UTC) == first_play + timedelta(seconds=300)

    # An explicit/re-observed Play resets from that Play timestamp.
    repository.set_restriction_timer(
        "TIMER-DEVICE",
        "LOCAL_INTERNET_RADIO:station",
        play_started_at=replay,
        reason="explicit_play_readback",
    )
    db.commit()
    assert _row(db).effective_until.replace(tzinfo=UTC) == replay + timedelta(seconds=300)

    for reason in ("pause_readback", "stop_readback"):
        repository.set_restriction_timer(
            "TIMER-DEVICE",
            "LOCAL_INTERNET_RADIO:station",
            play_started_at=None,
            reason=reason,
        )
        db.commit()
        assert _row(db).timer_started_at is None
        assert _row(db).effective_until is None
        repository.set_restriction_timer(
            "TIMER-DEVICE",
            "LOCAL_INTERNET_RADIO:station",
            play_started_at=replay,
            reason="recovery_play_readback",
        )

    db.close()


@pytest.mark.parametrize("timeout", [None, 0, 1, 300, 21600, UINT64_MAX])
def test_timer_values_remain_uint64_and_only_positive_values_activate(timeout):
    received = datetime(2030, 2, 1, tzinfo=UTC)
    payload = {} if timeout is None else {"restrictions": {"inactivityTimeout": timeout}}
    parsed = parse_restrictions(payload, received_at=received)
    db = app_db.SessionLocal()
    repository = ResearchStateRepository(db)
    key = f"LOCAL_INTERNET_RADIO:value-{timeout}"
    repository.upsert_restrictions("VALUES", key, parsed)
    repository.set_restriction_timer(
        "VALUES", key, play_started_at=received + timedelta(seconds=5), reason="play_readback"
    )
    db.commit()
    row = _row(db, "VALUES", key)
    assert row.inactivity_timeout_s == timeout
    if timeout and timeout < UINT64_MAX:
        assert row.timer_started_at is not None
        assert row.effective_until is not None
    elif timeout == UINT64_MAX:
        assert row.timer_started_at is not None
        assert row.effective_until is None
    else:
        assert row.timer_started_at is None
        assert row.effective_until is None
    db.close()
