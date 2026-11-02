"""Large local log tables must not prevent async radio readback completing."""
import asyncio
import threading

import httpx
import pytest

from basswiesn.app.main import create_web_app
from basswiesn.app.routers import devices, system_api, telemetry

pytestmark = pytest.mark.integration


@pytest.mark.parametrize("path,module,attribute,result", [
    ("/api/system/healthcheck", system_api.runtime_health, "release_healthcheck",
     {"status": "green", "summary": "fixture", "checks": []}),
    ("/api/maintenance/storage", devices, "storage_summary", {"request_log_count": 3}),
])
def test_slow_diagnostics_leave_live_requests_responsive(monkeypatch, path, module, attribute, result):
    entered, release = threading.Event(), threading.Event()
    loop_thread = threading.get_ident()

    def slow_read(db):
        # Fail deterministically if a later refactor puts synchronous SQL back
        # onto the event loop; do not depend on timing a machine's disk.
        assert threading.get_ident() != loop_thread
        entered.set()
        assert release.wait(5), "concurrent liveness never completed"
        return result

    monkeypatch.setattr(module, attribute, slow_read)

    async def scenario():
        transport = httpx.ASGITransport(app=create_web_app(background_tasks=False))
        async with httpx.AsyncClient(transport=transport, base_url="http://testserver") as client:
            pending = asyncio.create_task(client.get(path))
            try:
                assert await asyncio.to_thread(entered.wait, 2)
                live = await asyncio.wait_for(client.get("/api/health"), timeout=2)
                assert live.status_code == 200 and live.json()["ok"] is True
                assert not pending.done(), "the slow read should still be in progress"
            finally:
                release.set()
                response = await pending
            assert response.status_code == 200 and response.json() == result

    asyncio.run(scenario())


@pytest.mark.parametrize("endpoint", [telemetry.telemetry, telemetry.telemetry_summary])
def test_log_read_handlers_use_worker_dispatch(endpoint):
    # Payloads are loaded from synchronous SQLAlchemy before serialization.
    assert not asyncio.iscoroutinefunction(endpoint)
