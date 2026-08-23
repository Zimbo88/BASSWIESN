import asyncio
from datetime import UTC, datetime, timedelta

import httpx
import pytest
from sqlalchemy import create_engine
from sqlalchemy.orm import Session

from basswiesn.app.db.migrations import ensure_schema_baseline
from basswiesn.app.models import ArtworkCacheEntry
from basswiesn.app.services.artwork import (
    ArtworkSource,
    cache_artwork,
    choose_artwork,
)
from basswiesn.app.services.network_security import UrlValidation


pytestmark = pytest.mark.unit


def _session() -> tuple[object, Session]:
    engine = create_engine("sqlite:///:memory:")
    ensure_schema_baseline(engine)
    return engine, Session(engine)


def _allowed(_url: str) -> UrlValidation:
    return UrlValidation(True, "ok", hostname="example.test", addresses=("203.0.113.5",), scheme="https", port=443)


def test_artwork_priority_and_oled_capability_are_explicit() -> None:
    image = choose_artwork(
        image_url="https://example.test/live.png",
        provider_artwork_url="https://example.test/provider.png",
        station_logo_url="https://example.test/station.png",
    )
    provider = choose_artwork(provider_artwork_url="https://example.test/provider.png", station_logo_url="https://example.test/station.png")
    station = choose_artwork(station_logo_url="https://example.test/station.png")
    fallback = choose_artwork()
    assert [item.source for item in (image, provider, station, fallback)] == [
        ArtworkSource.IMAGE_URL,
        ArtworkSource.PROVIDER,
        ArtworkSource.STATION,
        ArtworkSource.FALLBACK,
    ]
    assert image.radio_oled_supported is None
    assert fallback.cacheable is False


def test_remote_artwork_is_bounded_persisted_and_reused(tmp_path) -> None:
    calls = 0

    async def handler(_request: httpx.Request) -> httpx.Response:
        nonlocal calls
        calls += 1
        return httpx.Response(200, headers={"content-type": "image/png", "etag": "abc"}, content=b"png-data")

    engine, db = _session()
    try:
        now = datetime(2026, 8, 14, tzinfo=UTC)
        choice = choose_artwork(image_url="https://example.test/live.png?token=secret")
        first = asyncio.run(cache_artwork(
            db, choice, media_dir=tmp_path, now=now,
            transport=httpx.MockTransport(handler), validator=_allowed,
        ))
        second = asyncio.run(cache_artwork(
            db, choice, media_dir=tmp_path, now=now + timedelta(minutes=1),
            transport=httpx.MockTransport(handler), validator=_allowed,
        ))
        row = db.query(ArtworkCacheEntry).one()
        assert first.status == "FETCHED"
        assert second.status == "HIT"
        assert calls == 1
        assert row.source_url_redacted == "https://example.test/live.png"
        assert "secret" not in row.source_url_redacted
        assert row.failure_status is None
    finally:
        db.close()
        engine.dispose()


def test_remote_artwork_falls_back_only_across_prevalidated_addresses(tmp_path) -> None:
    attempted_hosts: list[str] = []

    async def handler(request: httpx.Request) -> httpx.Response:
        attempted_hosts.append(request.url.host or "")
        if ":" in (request.url.host or ""):
            raise httpx.ConnectError("IPv6 route unavailable", request=request)
        return httpx.Response(
            200, headers={"content-type": "image/png"}, content=b"png-data"
        )

    def dual_stack(_url: str) -> UrlValidation:
        return UrlValidation(
            True,
            "ok",
            hostname="example.test",
            addresses=("2001:db8::5", "203.0.113.5"),
            scheme="https",
            port=443,
        )

    engine, db = _session()
    try:
        result = asyncio.run(
            cache_artwork(
                db,
                choose_artwork(image_url="https://example.test/live.png"),
                media_dir=tmp_path,
                transport=httpx.MockTransport(handler),
                validator=dual_stack,
            )
        )
        assert result.status == "FETCHED"
        assert attempted_hosts == ["2001:db8::5", "203.0.113.5"]
    finally:
        db.close()
        engine.dispose()


def test_blocked_and_non_image_results_record_failure_without_throwing(tmp_path) -> None:
    engine, db = _session()
    try:
        calls = 0

        def blocked_validator(_url: str) -> UrlValidation:
            nonlocal calls
            calls += 1
            return UrlValidation(False, "private target")

        choice = choose_artwork(image_url="http://192.168.50.25/admin?token=secret")
        blocked = asyncio.run(cache_artwork(
            db, choice, media_dir=tmp_path,
            validator=blocked_validator,
        ))
        blocked_again = asyncio.run(cache_artwork(
            db, choice, media_dir=tmp_path,
            validator=blocked_validator,
        ))
        assert blocked.status == "FAILED"
        assert blocked.failure_status == "URL_BLOCKED"
        assert blocked.public_url == "/static/bmx-icons/orion/monochrome.svg"
        assert "192.168.50.25" not in blocked.public_url
        assert "secret" not in str(blocked.to_dict())
        assert blocked_again.status == "FAILED"
        assert calls == 1

        async def handler(_request: httpx.Request) -> httpx.Response:
            return httpx.Response(200, headers={"content-type": "text/html"}, text="not an image")

        other = choose_artwork(provider_artwork_url="https://example.test/not-image")
        failed = asyncio.run(cache_artwork(
            db, other, media_dir=tmp_path,
            transport=httpx.MockTransport(handler), validator=_allowed,
        ))
        assert failed.status == "FAILED"
        assert failed.failure_status == "NOT_IMAGE"
        assert db.query(ArtworkCacheEntry).count() == 2
    finally:
        db.close()
        engine.dispose()
