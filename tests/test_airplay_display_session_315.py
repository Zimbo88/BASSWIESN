"""Actual DB/cloud projection; software evidence, never physical OLED proof."""
import asyncio
import base64
import json
from types import SimpleNamespace

from fastapi import FastAPI
from fastapi.testclient import TestClient
import pytest
from sqlalchemy import select

from basswiesn.app import db as app_db
from basswiesn.app.models import Device, MetadataState, RuntimeState, Station
from basswiesn.app.routers import cloud
from basswiesn.app.services import device_ownership, station_metadata
from basswiesn.app.services.airplay_bridge import display_session as projection
from basswiesn.app.services.airplay_bridge.metadata import BridgeMetadata
from basswiesn.app.services.airplay_bridge.metadata_wire import MetadataWireDecoder
from basswiesn.app.services.clock_metadata import save_clock_metadata_preference
from basswiesn.app.services.display_composition import DisplayPreference

pytestmark = pytest.mark.integration
DEVICE = "AP2-FIXTURE-A"
CONTRACT = "ap2-fixture"
RECEIVER = "a" * 32


@pytest.fixture
def station_id():
    with app_db.SessionLocal() as db:
        station = Station(name="AP2 Example", stream_url="http://192.0.2.43:8088/private-fixture.mp3",
                          provider_station_id=CONTRACT, internal=True,
                          lab_only=True, purpose=projection.PURPOSE)
        db.add_all([Device(device_id=DEVICE, name="Example", ip_address="192.0.2.42"), station])
        db.commit()
        save_clock_metadata_preference(db, DEVICE, enabled=False, mode="APPEND")
        return station.id


def owner(device_id=DEVICE, root=None):
    return device_ownership.DeviceOwnership(root or device_ownership.lock_root(), device_id, timeout=0)


def read(station_id):
    # Another worker has another identity map/connection, not the publisher's.
    with app_db.SessionLocal() as db:
        return projection.display_metadata(db, db.scalar(select(Device).where(Device.device_id == DEVICE)),
                                           db.get(Station, station_id))


def test_complete_batches_cross_worker_projection_and_cleanup(station_id):
    async def scenario():
        async with owner() as ownership:
            with app_db.SessionLocal() as db:
                publisher = projection.DisplayPublisher.begin(db, DEVICE, station_id, ownership)
                publisher.publish(db, {"track": "First", "artist": "Artist", "album": "Album"})
                assert read(station_id) == {"track": "First", "artist": "Artist", "album": "Album"}
                publisher.publish(db, {"track": "Podcast\n\x00 episode"})
                assert read(station_id) == {"track": "Podcast episode"}
                publisher.renew(db)
                assert read(station_id) == {"track": "Podcast episode"}
                publisher.end(db)
                assert read(station_id) == {}
                with pytest.raises(projection.DisplaySessionRejected):
                    publisher.publish(db, {"track": "Late update"})
    asyncio.run(scenario())


@pytest.mark.parametrize("mode,fields,expected", [
    (None, {"track": "Podcast"}, ("Podcast", "")),
    ("TRACK_ARTIST", {"track": "Song", "artist": "Artist", "album": "Album"}, ("Song", "Artist")),
    ("TRACK_ARTIST", {"track": "Podcast"}, ("Podcast", "")),
    ("STATION", {"track": "Song", "artist": "Artist"}, ("AP2 Example", "")),
    ("CUSTOM", {"track": "Song", "artist": "Artist"}, ("Artist — Song", "")),
])
def test_bmx_get_and_reporting_use_same_live_batch_not_icy_or_radio_echo(station_id, mode, fields, expected):
    async def scenario():
        collector = station_metadata.StationMetadataCollector(app_db.SessionLocal)
        app = FastAPI()
        app.include_router(cloud.router)
        app.state.station_metadata_collector = collector
        async with owner() as ownership:
            with app_db.SessionLocal() as db:
                station = db.get(Station, station_id)
                db.add(MetadataState(device_id=DEVICE, station_id=CONTRACT, track="Stale echo",
                                     artist="Old artist", album="Old album", artwork_url="https://example.invalid/old.png"))
                db.add(RuntimeState(key=station_metadata.cache_key(station.stream_url), value=json.dumps({
                    "track": "Stale ICY", "artist": "Old ICY artist"})))
                db.commit()
                if mode:
                    station_metadata.save_display_preference(db, DEVICE, DisplayPreference(
                        mode, fields=("artist", "title")))
                publisher = projection.DisplayPublisher.begin(db, DEVICE, station_id, ownership)
                publisher.publish(db, fields)
            with TestClient(app) as client:
                headers = {"x-basswiesn-device-id": DEVICE}
                current = client.get(f"/bmx/orion/now-playing/station/{CONTRACT}", headers=headers)
                reported = client.post(f"/bmx/orion/reporting/station/{CONTRACT}",
                                       json={"eventType": "TIMED", "timeIntoTrack": 120}, headers=headers)
                assert current.status_code == reported.status_code == 200
                body = current.json()
                assert body == reported.json()["_embedded"]["bmx_nowplaying"]
                assert (body["track"], body["artist"]) == expected
                assert body["album"] == fields.get("album", "")
                assert body["imageUrl"] == ""  # No invented artwork transport.
                with app_db.SessionLocal() as db:
                    publisher.end(db)
                ended = client.get(f"/bmx/orion/now-playing/station/{CONTRACT}", headers=headers).json()
                assert ended["track"] in {"AP2 Example", ""}
                assert ended["artist"] == ended["album"] == ended["imageUrl"] == ""
            assert not collector.tasks  # Must not consume the one-use relay via an ICY probe.
        await collector.shutdown()
    asyncio.run(scenario())


def test_missing_title_clock_mode_preserves_actual_apple_title(station_id):
    async def scenario():
        async with owner() as ownership:
            with app_db.SessionLocal() as db:
                save_clock_metadata_preference(db, DEVICE, enabled=True, mode="MISSING_TITLE")
                publisher = projection.DisplayPublisher.begin(db, DEVICE, station_id, ownership)
                publisher.publish(db, {"track": "Podcast"})
            with app_db.SessionLocal() as db:
                payload = cloud._orion_now_playing_payload(db,
                    db.scalar(select(Device).where(Device.device_id == DEVICE)), CONTRACT)
                assert payload["track"] == "Podcast"
    asyncio.run(scenario())


def test_expired_owner_cannot_renew_or_erase_a_replacement(station_id, monkeypatch):
    # Separate clock object: do not patch asyncio's own monotonic clock.
    ticks = [100.0]
    monkeypatch.setattr(projection, "time", SimpleNamespace(monotonic=lambda: ticks[0]))
    async def scenario():
        async with owner() as ownership:
            with app_db.SessionLocal() as db:
                old = projection.DisplayPublisher.begin(db, DEVICE, station_id, ownership)
                old.publish(db, {"track": "First"})
                ticks[0] += 10
                assert read(station_id) == {}
                with pytest.raises(projection.DisplaySessionRejected):
                    old.renew(db)
                new = projection.DisplayPublisher.begin(db, DEVICE, station_id, ownership)
                new.publish(db, {"track": "Second"})
                with pytest.raises(projection.DisplaySessionRejected):
                    old.end(db)
                assert read(station_id) == {"track": "Second"}
                new.end(db)
    asyncio.run(scenario())


def test_live_owner_cannot_be_replaced(station_id):
    async def scenario():
        async with owner() as ownership:
            with app_db.SessionLocal() as db:
                publisher = projection.DisplayPublisher.begin(db, DEVICE, station_id, ownership)
                publisher.publish(db, {"track": "First"})
                with pytest.raises(projection.DisplaySessionRejected):
                    projection.DisplayPublisher.begin(db, DEVICE, station_id, ownership)
                assert read(station_id) == {"track": "First"}
    asyncio.run(scenario())


def test_verified_readback_rebinds_expired_text_without_reviving_old_lease(station_id, monkeypatch):
    ticks = [100.0]
    monkeypatch.setattr(projection, 'time', SimpleNamespace(monotonic=lambda: ticks[0]))
    def observation(sequence, text):
        return dict(version=1, session=RECEIVER, sequence=sequence,
                    observed_monotonic=ticks[0], active=True, fields={'track':text})
    async def scenario():
        async with owner() as ownership:
            with app_db.SessionLocal() as db:
                old = projection.DisplayPublisher.begin(db, DEVICE, station_id, ownership,
                                                        receiver_session=RECEIVER)
                old.accept_receiver(db, observation(1, 'First'))
                ticks[0] += 11  # Slow but successful device transaction.
                assert read(station_id) == {}
                new = old.accept_after_verified_readback(db, observation(2, 'Second'))
                assert new is not old and new._record.session != old._record.session
                assert not old._record.active and read(station_id) == {'track':'Second'}
                with pytest.raises(projection.DisplaySessionRejected):
                    old.renew(db)
                with pytest.raises(projection.DisplaySessionRejected):
                    old.end(db)
                # Rollover preserves the old receiver cursor, not a replay window.
                with pytest.raises(projection.DisplaySessionRejected):
                    new.accept_after_verified_readback(db, observation(1, 'Replay'))
                assert read(station_id) == {'track':'Second'}
                new.end(db)
    asyncio.run(scenario())


@pytest.mark.parametrize('failure', ['stale', 'session', 'replay', 'identity', 'replacement'])
def test_text_rollover_never_hides_receiver_binding_or_cas_failure(station_id, monkeypatch, failure):
    ticks = [100.0]
    monkeypatch.setattr(projection, 'time', SimpleNamespace(monotonic=lambda: ticks[0]))
    async def scenario():
        async with owner() as ownership:
            with app_db.SessionLocal() as db:
                old = projection.DisplayPublisher.begin(db, DEVICE, station_id, ownership,
                                                        receiver_session=RECEIVER)
                obs = dict(version=1, session=RECEIVER, sequence=2,
                           observed_monotonic=100.0, active=True, fields={'track':'First'})
                old.accept_receiver(db, obs)
                ticks[0] += 11
                fresh = {**obs, 'sequence':3, 'observed_monotonic':111.0}
                if failure == 'stale': fresh['observed_monotonic'] = 100.0
                if failure == 'session': fresh['session'] = 'b' * 32
                if failure == 'replay': fresh['sequence'] = 1
                if failure == 'identity':
                    db.scalar(select(Device).where(Device.device_id == DEVICE)).ip_address = '192.0.2.99'
                    db.commit()
                if failure == 'replacement':
                    new = projection.DisplayPublisher.begin(db, DEVICE, station_id, ownership,
                                                            receiver_session=RECEIVER)
                    new.publish(db, {'track':'Replacement'})
                with pytest.raises(projection.DisplaySessionRejected):
                    old.accept_after_verified_readback(db, fresh)
                assert read(station_id) == ({'track':'Replacement'} if failure == 'replacement' else {})
    asyncio.run(scenario())


@pytest.mark.parametrize("change", ["endpoint", "station_url", "owner", "protected", "purpose"])
def test_changed_binding_or_protection_stops_display_and_renewal(station_id, monkeypatch, change):
    async def scenario():
        async with owner() as ownership:
            with app_db.SessionLocal() as db:
                publisher = projection.DisplayPublisher.begin(db, DEVICE, station_id, ownership)
                publisher.publish(db, {"track": "Private song"})
                if change == "endpoint":
                    db.scalar(select(Device).where(Device.device_id == DEVICE)).ip_address = "192.0.2.44"
                elif change == "station_url":
                    db.get(Station, station_id).stream_url = "http://192.0.2.43:8088/new.mp3"
                elif change == "purpose":
                    db.get(Station, station_id).purpose = "different"
                elif change == "owner":
                    monkeypatch.setattr(projection, "_process_identity", lambda pid: "f" * 64)
                else:
                    monkeypatch.setattr(projection, "is_device_access_protected", lambda *args: True)
                db.commit()
                assert not read(station_id)
                with pytest.raises(projection.DisplaySessionRejected):
                    publisher.renew(db)
                publisher.end(db)  # Local cleanup is safe even after device protection changed.
    asyncio.run(scenario())


@pytest.mark.parametrize("bad_fields", [
    {"url": "http://example.invalid/artwork"}, {"track": 7}, {"track": "x" * 4097},
    {"pairing_secret": "must-not-echo"},
])
def test_metadata_ingress_rejects_unbounded_or_non_display_fields(station_id, bad_fields):
    async def scenario():
        async with owner() as ownership:
            with app_db.SessionLocal() as db:
                publisher = projection.DisplayPublisher.begin(db, DEVICE, station_id, ownership)
                with pytest.raises(projection.DisplaySessionRejected, match="^AIRPLAY_DISPLAY_SESSION_UNVERIFIED$"):
                    publisher.publish(db, bad_fields)
                assert read(station_id) == {}
    asyncio.run(scenario())


@pytest.mark.parametrize("wrong", ["device", "namespace", "released"])
def test_unowned_or_wrong_device_lock_cannot_publish(station_id, tmp_path, wrong):
    async def scenario():
        ownership = owner("OTHER" if wrong == "device" else DEVICE,
                          root=tmp_path / "unrelated-locks" if wrong == "namespace" else None)
        await ownership.acquire()
        if wrong == "released":
            ownership.release()
        try:
            with app_db.SessionLocal() as db:
                with pytest.raises(projection.DisplaySessionRejected):
                    projection.DisplayPublisher.begin(db, DEVICE, station_id, ownership)
        finally:
            if ownership.owned_by_current_task():
                ownership.release()
    asyncio.run(scenario())


@pytest.mark.parametrize("raw", ["{}", "not json", "x" * 16385,
                                 '{"fields": {}, "fields": {"track": "stale"}}'])
def test_malformed_runtime_rows_fail_closed(station_id, raw):
    with app_db.SessionLocal() as db:
        db.add(RuntimeState(key=projection._key(DEVICE), value=raw))
        db.commit()
    assert read(station_id) == {}


def test_public_station_cannot_be_hijacked_for_bridge_text(station_id):
    async def scenario():
        async with owner() as ownership:
            with app_db.SessionLocal() as db:
                db.get(Station, station_id).internal = False
                db.commit()
                with pytest.raises(projection.DisplaySessionRejected):
                    projection.DisplayPublisher.begin(db, DEVICE, station_id, ownership)
                assert db.scalar(select(RuntimeState).where(RuntimeState.key == projection._key(DEVICE))) is None
                assert read(station_id) is None
    asyncio.run(scenario())


def wire(kind, code, value=b"", *, length=None):
    return (f'<item><type>{kind.encode().hex()}</type><code>{code.encode().hex()}</code>'
            f'<length>{len(value) if length is None else length}</length><data encoding="base64">'
            f'{base64.b64encode(value).decode()}</data></item>').encode()


def test_real_wire_to_private_receiver_observation_to_bmx_projection(station_id, monkeypatch):
    ticks = [100.0]
    monkeypatch.setattr(projection, "time", SimpleNamespace(monotonic=lambda: ticks[0]))
    receiver = BridgeMetadata("AP2 Example")
    generation = receiver.begin()
    decoder = MetadataWireDecoder(on_discard=receiver.abort_pending)
    raw = (wire("ssnc", "mdst") + wire("core", "minm", "A track – München".encode())
           + wire("core", "asar", b"An artist") + wire("ssnc", "mden"))
    for index, (kind, code, data) in enumerate(decoder.iter_events(raw)):
        receiver.apply(generation, index, kind, code, data)
    async def scenario():
        async with owner() as ownership:
            with app_db.SessionLocal() as db:
                publisher = projection.DisplayPublisher.begin(db, DEVICE, station_id, ownership,
                                                              receiver_session=RECEIVER)
                observation = receiver.observation(RECEIVER, 1, now=ticks[0])
                assert publisher.accept_receiver(db, observation)
                sequence = publisher._record.sequence
                assert not publisher.accept_receiver(db, observation)  # Duplicate is not a renewal.
                ticks[0] += 4
                assert not publisher.accept_receiver(db, receiver.observation(RECEIVER, 2, now=ticks[0]))
                assert publisher._record.sequence == sequence
                ticks[0] += 1
                assert publisher.accept_receiver(db, receiver.observation(RECEIVER, 3, now=ticks[0]))
                assert publisher._record.sequence == sequence + 1
                with app_db.SessionLocal() as reader:
                    payload = cloud._orion_now_playing_payload(reader,
                        reader.scalar(select(Device).where(Device.device_id == DEVICE)), CONTRACT)
                assert payload["track"] == "A track – München" and payload["artist"] == "An artist"
                receiver.end(generation)
                ticks[0] += 1
                assert publisher.accept_receiver(db, receiver.observation(RECEIVER, 4, now=ticks[0]))
                assert read(station_id) == {}
    asyncio.run(scenario())


@pytest.mark.parametrize("bad", ["expired", "session", "extra", "sequence", "changed_duplicate", "missing_binding"])
def test_private_observation_failure_is_terminal_for_this_publisher(station_id, monkeypatch, bad):
    ticks = [100.0]
    monkeypatch.setattr(projection, "time", SimpleNamespace(monotonic=lambda: ticks[0]))
    receiver = BridgeMetadata("AP2 Example")
    receiver.begin()
    async def scenario():
        async with owner() as ownership:
            with app_db.SessionLocal() as db:
                publisher = projection.DisplayPublisher.begin(db, DEVICE, station_id, ownership,
                    receiver_session=None if bad == "missing_binding" else RECEIVER)
                observation = receiver.observation(RECEIVER, 1, now=ticks[0])
                if bad != "missing_binding":
                    publisher.accept_receiver(db, observation)
                if bad == "expired":
                    ticks[0] += 5
                elif bad == "session":
                    observation["session"] = "b" * 32
                elif bad == "extra":
                    observation["url"] = "http://example.invalid/ignored"
                elif bad == "sequence":
                    observation["sequence"] = 0
                elif bad == "changed_duplicate":
                    observation["fields"] = {"track": "Inconsistent duplicate"}
                with pytest.raises(projection.DisplaySessionRejected):
                    publisher.accept_receiver(db, observation)
                with pytest.raises(projection.DisplaySessionRejected):
                    publisher.accept_receiver(db, receiver.observation(RECEIVER, 2, now=ticks[0]))
                publisher.end(db)
                assert read(station_id) == {}
    asyncio.run(scenario())


def test_malformed_field_aborts_exact_batch_even_when_all_items_arrive_together():
    receiver = BridgeMetadata("AP2 Example")
    generation = receiver.begin()
    decoder = MetadataWireDecoder(on_discard=receiver.abort_pending)
    raw = (wire("ssnc", "mdst") + wire("core", "minm", b"Incomplete new track")
           + wire("core", "asar", b"bad length", length=1) + wire("ssnc", "mden")
           + wire("ssnc", "mdst") + wire("core", "minm", b"Complete podcast")
           + wire("ssnc", "mden"))
    commits = []
    for index, (kind, code, value) in enumerate(decoder.iter_events(raw)):
        if receiver.apply(generation, index, kind, code, value):
            commits.append(receiver.text_fields())
    assert commits == [{"track": "Complete podcast"}]
    assert decoder.discarded == 1
