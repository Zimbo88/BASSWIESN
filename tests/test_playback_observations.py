"""Characterization of readback interpretation, never a playback controller."""
from datetime import UTC, datetime, timedelta

import pytest

from basswiesn.app.services import playback_keepalive as observations

pytestmark = pytest.mark.unit
NOW = datetime(2026, 10, 1, 12, tzinfo=UTC)


@pytest.mark.parametrize("value,expected", [
    (None, None), ("", None), ("not-a-date", None),
    ("2026-10-01T12:00:00Z", NOW), ("2026-10-01T12:00:00", NOW),
    ("2026-10-01T14:00:00+02:00", NOW),
])
def test_timestamp_interpretation(value, expected):
    assert observations._parse_iso(value) == expected


@pytest.mark.parametrize("fields,expected", [
    ({}, None), ({"stationName": "not a clock"}, None),
    ({"position": "21.9"}, 21), ({"PositionMs": "1200"}, 1200),
    ({"{urn:example}elapsedTime": "33"}, 33),
    ({"position": "bad", "elapsedTimeMs": "4500"}, 4500),
    ({"position": "NaN"}, None), ({"position": ""}, None),
])
def test_only_explicit_position_fields_are_progress_evidence(fields, expected):
    assert observations._explicit_position({"now_playing": fields}) == expected


@pytest.mark.parametrize("status", ["PLAYING", "PLAY_STATE"])
def test_position_changes_and_repeated_positions_have_different_evidence(status):
    runtime = {"playback_state": status, "now_playing": {"position": "20"}}
    progressing, stagnant, update = observations._progress_evidence({}, runtime, NOW)
    assert progressing is True and stagnant == 0
    progressing, stagnant, repeated = observations._progress_evidence(
        update, runtime, NOW + timedelta(seconds=30))
    assert progressing is False and stagnant == 30
    runtime["now_playing"]["position"] = "21"
    progressing, stagnant, update = observations._progress_evidence(
        repeated, runtime, NOW + timedelta(seconds=31))
    assert progressing is True and stagnant == 0 and update["position_stagnant_since"] == ""


@pytest.mark.parametrize("status", ["STANDBY", "PAUSED", "UNKNOWN", "INVALID_SOURCE"])
def test_inactive_states_do_not_report_stalled_progress(status):
    result = observations._progress_evidence(
        {"explicit_position": 20, "position_observed_at": NOW.isoformat()},
        {"playback_state": status, "now_playing": {"position": "20"}},
        NOW + timedelta(seconds=600))
    assert result[:2] == (None, None)


def test_missing_position_never_proves_a_live_radio_stream_stalled():
    result = observations._progress_evidence(
        {"explicit_position": 20, "position_observed_at": NOW.isoformat()},
        {"playback_state": "PLAY_STATE", "now_playing": {"stationName": "Example"}}, NOW)
    assert result[:2] == (None, None)


@pytest.mark.parametrize("status", ["BUFFERING", "BUFFERING_STATE"])
def test_buffer_duration_is_separate_from_media_progress(status):
    result = observations._progress_evidence(
        {"buffering_since": (NOW - timedelta(seconds=40)).isoformat()},
        {"playback_state": status}, NOW)
    assert result[:2] == (None, 40)


@pytest.mark.parametrize("fields,expected", [
    ({"stationName": "Example", "itemName": "Station"}, None),
    ({"Track": " Song ", "Artist": " Artist "}, {"track": "Song", "artist": "Artist"}),
    ({"{urn:example}title": "Song", "ArtUrl": "https://example.invalid/art"},
     {"track": "Song", "imageUrl": "https://example.invalid/art"}),
    ({"track": "", "album": None}, {"track": None, "album": None}),
    ({"track": "", "title": "fallback"}, {"track": None}),
])
def test_metadata_preserves_absent_vs_explicitly_cleared_values(fields, expected):
    assert observations._radio_metadata_observation({"now_playing": fields}) == expected


@pytest.mark.parametrize("position", ["inf", "-Infinity", "1e100000", "NaN"])
def test_nonfinite_position_is_unknown_not_a_worker_exception(position):
    assert observations._explicit_position({"now_playing": {"position": position}}) is None
    assert observations._progress_evidence({}, {
        "playback_state": "PLAY_STATE", "now_playing": {"position": position}}, NOW)[:2] == (None, None)


@pytest.mark.parametrize("previous", ["corrupt", "Infinity", {}, ""])
def test_invalid_persisted_position_is_reinitialized_from_real_readback(previous):
    result = observations._progress_evidence({
        "explicit_position": previous, "position_observed_at": NOW.isoformat()}, {
        "playback_state": "PLAY_STATE", "now_playing": {"position": "20"}}, NOW)
    assert result[:2] == (True, 0)
    assert result[2]["explicit_position"] == 20


def test_future_observation_does_not_create_negative_stall_duration():
    result = observations._progress_evidence({
        "explicit_position": 20, "position_observed_at": (NOW + timedelta(seconds=50)).isoformat()}, {
        "playback_state": "PLAY_STATE", "now_playing": {"position": "20"}}, NOW)
    assert result[:2] == (False, 0)


@pytest.mark.parametrize("raw", [None, "bad", [], 7])
def test_invalid_payload_cannot_be_mistaken_for_metadata_or_progress(raw):
    assert observations._explicit_position({"now_playing": raw}) is None
    assert observations._radio_metadata_observation({"now_playing": raw}) is None


def test_source_state_and_first_text_keep_existing_normalization():
    assert observations._source({"current_source": "airplay"}) == "AIRPLAY"
    assert observations._playback_state({"playback_state": "play_state"}) == "PLAY_STATE"
    assert observations._first_text(None, " ", " title ", "ignored") == "title"
    assert observations._first_text(None, " ") == ""
