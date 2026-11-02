"""Independent sessions model provider replies completing out of order."""
import asyncio
from datetime import UTC, datetime, timedelta
from pathlib import Path

import httpx
import pytest
from sqlalchemy import create_engine
from sqlalchemy.orm import Session

from basswiesn.app.db.migrations import ensure_schema_baseline
from basswiesn.app.models import ArtworkCacheEntry
from basswiesn.app.services.artwork import cache_artwork, choose_artwork
from basswiesn.app.services.network_security import UrlValidation

pytestmark = pytest.mark.unit


def allowed(_url):
    return UrlValidation(True, "fixture", hostname="example.test",
                         addresses=("203.0.113.42",), scheme="https", port=443)


@pytest.mark.parametrize("late_outcome", ["failure", "success"])
@pytest.mark.parametrize("newer_outcome", ["failure", "success"])
@pytest.mark.parametrize("delay_seconds", [0, 1])
def test_late_cache_reply_preserves_newer_committed_observation(
    tmp_path, late_outcome, newer_outcome, delay_seconds
):
    engine = create_engine(f"sqlite:///{tmp_path / 'cache.db'}")
    ensure_schema_baseline(engine)
    first, second = Session(engine), Session(engine)
    choice = choose_artwork(image_url="https://example.test/image.png")
    started = datetime(2026, 10, 1, tzinfo=UTC)

    async def scenario():
        entered, finish = asyncio.Event(), asyncio.Event()

        async def delayed(_request):
            entered.set()
            await finish.wait()
            return httpx.Response(503) if late_outcome == "failure" else httpx.Response(
                200, headers={"content-type": "image/png"}, content=b"older-payload")

        pending = asyncio.create_task(cache_artwork(
            first, choice, media_dir=tmp_path, now=started,
            transport=httpx.MockTransport(delayed), validator=allowed))
        await entered.wait()
        newer = await cache_artwork(
            second, choice, media_dir=tmp_path, now=started + timedelta(seconds=delay_seconds),
            transport=httpx.MockTransport(lambda _: httpx.Response(502) if newer_outcome == "failure" else httpx.Response(
                200, headers={"content-type": "image/png"}, content=b"newer-payload")),
            validator=allowed)
        assert newer.status == ("FETCHED" if newer_outcome == "success" else "FAILED")
        finish.set()
        result = await pending
        second.expire_all()
        row = second.query(ArtworkCacheEntry).one()
        assert row.fetched_at.replace(tzinfo=UTC) == started + timedelta(seconds=delay_seconds)
        if newer_outcome == "success":
            assert row.failure_status is None
            assert Path(row.cached_path).read_bytes() == b"newer-payload"
            assert result.status == "HIT" and result.failure_status is None
        else:
            assert row.failure_status == "HTTP_STATUS" and row.failure_count == 1
            assert row.last_error == "HTTP 502" and row.cached_path is None
            assert result.status == "FAILED" and result.failure_status == "HTTP_STATUS"

    try:
        asyncio.run(scenario())
    finally:
        first.close()
        second.close()
        engine.dispose()
