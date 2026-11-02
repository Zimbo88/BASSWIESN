from __future__ import annotations

import base64
from datetime import UTC, datetime, timedelta
from types import SimpleNamespace

import pytest
from fastapi import FastAPI
from fastapi.testclient import TestClient
from sqlalchemy import create_engine
from sqlalchemy.orm import sessionmaker

from basswiesn.app.db import get_db
from basswiesn.app.db.migrations import ensure_schema_baseline
from basswiesn.app.models import ArtworkCacheEntry, Device, MetadataState, Station
from basswiesn.app.routers import research_state
from basswiesn.app.services.artwork import artwork_cache_key, choose_artwork


pytestmark = pytest.mark.unit

LIVE_ART_URL = "https://art.example/live.png?token=never-in-browser"
STATION_ART_URL = "https://logos.example/station.png?key=also-private"
PNG_1X1 = base64.b64decode(
    "iVBORw0KGgoAAAANSUhEUgAAAAEAAAABCAQAAAC1HAwCAAAAC0lEQVR42mNk+A8AAQUBAScY42YAAAAASUVORK5CYII="
)


@pytest.fixture
def artwork_app(tmp_path, monkeypatch):
    engine = create_engine(
        f"sqlite:///{tmp_path / 'artwork-webui.db'}",
        connect_args={"check_same_thread": False},
    )
    ensure_schema_baseline(engine)
    factory = sessionmaker(bind=engine, autoflush=False, autocommit=False)
    media_dir = tmp_path / "media"
    cache_dir = media_dir / "artwork-cache"
    cache_dir.mkdir(parents=True)
    monkeypatch.setattr(
        research_state,
        "get_settings",
        lambda: SimpleNamespace(data_dir=tmp_path),
    )

    def override_db():
        with factory() as db:
            yield db

    app = FastAPI()
    app.include_router(research_state.router)
    app.dependency_overrides[get_db] = override_db

    with factory() as db:
        station = Station(
            name="Cache Radio",
            stream_url="https://radio.example/live.mp3",
            image_url=STATION_ART_URL,
            provider="LOCAL_INTERNET_RADIO",
            provider_station_id="station-cache-160",
        )
        db.add_all(
            [
                Device(
                    device_id="ART160",
                    name="Artwork Test Radio",
                    model="SoundTouch Test",
                    ip_address="192.0.2.160",
                ),
                Device(
                    device_id="NOART160",
                    name="Fallback Test Radio",
                    model="SoundTouch Test",
                    ip_address="192.0.2.161",
                ),
                station,
            ]
        )
        db.flush()
        db.add(
            MetadataState(
                device_id="ART160",
                station_name=station.name,
                station_id=station.provider_station_id,
                track="Cached title",
                artwork_url=LIVE_ART_URL,
                artwork_provenance="STREAM",
                provider="LOCAL_INTERNET_RADIO",
                source="LOCAL_INTERNET_RADIO",
                provenance="STREAM",
                confidence=100,
                updated_at=datetime.now(UTC),
                stale=False,
            )
        )

        live_choice = choose_artwork(
            image_url=LIVE_ART_URL,
            station_logo_url=STATION_ART_URL,
        )
        live_key = artwork_cache_key(
            live_choice,
            provider_id="LOCAL_INTERNET_RADIO",
            station_id="station-cache-160",
        )
        live_path = cache_dir / f"{live_key}.png"
        live_path.write_bytes(PNG_1X1)
        station_choice = choose_artwork(station_logo_url=STATION_ART_URL)
        station_key = artwork_cache_key(
            station_choice,
            provider_id="LOCAL_INTERNET_RADIO",
            station_id="station-cache-160",
        )
        station_path = cache_dir / f"{station_key}.png"
        station_path.write_bytes(PNG_1X1)
        expires = datetime.now(UTC) + timedelta(hours=1)
        db.add_all(
            [
                ArtworkCacheEntry(
                    cache_key=live_key,
                    device_id="ART160",
                    provider_id="LOCAL_INTERNET_RADIO",
                    station_id="station-cache-160",
                    source="IMAGE_URL",
                    source_url_hash="live",
                    source_url_redacted="https://art.example/live.png",
                    cached_path=str(live_path),
                    mime_type="image/png",
                    fetched_at=datetime.now(UTC),
                    expires_at=expires,
                ),
                ArtworkCacheEntry(
                    cache_key=station_key,
                    provider_id="LOCAL_INTERNET_RADIO",
                    station_id="station-cache-160",
                    source="STATION",
                    source_url_hash="station",
                    source_url_redacted="https://logos.example/station.png",
                    cached_path=str(station_path),
                    mime_type="image/png",
                    fetched_at=datetime.now(UTC),
                    expires_at=expires,
                ),
            ]
        )
        db.commit()
        station_id = station.id

    try:
        yield app, factory, station_id, live_key, tmp_path
    finally:
        engine.dispose()


def test_artwork_api_exposes_only_same_origin_cache_assets(artwork_app) -> None:
    app, _factory, station_id, live_key, _tmp_path = artwork_app
    with TestClient(app) as client:
        device = client.get("/api/devices/ART160/artwork")
        image = client.get(f"/api/artwork-cache/{live_key}")
        station = client.get(f"/api/stations/{station_id}/artwork")
        station_image = client.get(
            f"/api/stations/{station_id}/artwork/image", follow_redirects=True
        )
        fallback = client.get("/api/devices/NOART160/artwork")

    assert device.status_code == 200
    assert device.json()["source"] == "IMAGE_URL"
    assert device.json()["public_url"] == f"/api/artwork-cache/{live_key}"
    assert "art.example" not in device.text
    assert "never-in-browser" not in device.text
    assert image.status_code == 200
    assert image.headers["content-type"] == "image/png"
    assert image.headers["x-content-type-options"] == "nosniff"
    assert image.content == PNG_1X1
    assert station.status_code == 200
    assert station.json()["source"] == "STATION"
    assert station.json()["public_url"].startswith("/api/artwork-cache/")
    assert "logos.example" not in station.text
    assert station_image.status_code == 200
    assert station_image.content == PNG_1X1
    assert fallback.json()["source"] == "FALLBACK"
    assert fallback.json()["public_url"].startswith("/static/")


def test_artwork_cache_route_rejects_traversal_and_database_escape(artwork_app) -> None:
    app, factory, _station_id, _live_key, _tmp_path = artwork_app
    escaped_key = "c" * 64
    with factory() as db:
        db.add(
            ArtworkCacheEntry(
                cache_key=escaped_key,
                source="IMAGE_URL",
                source_url_hash="escaped",
                cached_path="/etc/passwd",
                mime_type="image/png",
                fetched_at=datetime.now(UTC),
                expires_at=datetime.now(UTC) + timedelta(hours=1),
            )
        )
        db.commit()

    with TestClient(app) as client:
        escaped = client.get(f"/api/artwork-cache/{escaped_key}")
        malformed = client.get("/api/artwork-cache/not-a-cache-key")
        traversal = client.get(
            "/api/artwork-cache/..%2F..%2Fetc%2Fpasswd", follow_redirects=False
        )

    assert escaped.status_code == 404
    assert malformed.status_code == 404
    assert traversal.status_code in {404, 422}
    assert b"root:" not in escaped.content
