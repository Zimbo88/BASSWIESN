"""Provider behavior is tested entirely with injected HTTP/DNS transports."""

import asyncio
import json
from dataclasses import asdict, replace

import httpx
import pytest

from basswiesn.app.services.providers.contracts import (
    ProviderCapabilities, ProviderError, ProviderErrorCode, ProviderHealth, ProviderState,
    validate_page, validate_text,
)
from basswiesn.app.services.providers.local import LocalStation, LocalStationAdapter
from basswiesn.app.services.providers.manager import ProviderManager
from basswiesn.app.services.providers.music_assistant import MusicAssistantAdapter, VERIFIED_VERSION
from basswiesn.app.services.providers.runtime import ProviderRuntimeConfig, build_provider_manager
from basswiesn.app.services.providers.transport import BoundedJSONClient, TargetPolicy, canonical_ip, validate_origin

pytestmark = pytest.mark.integration
PUBLIC = "93.184.216.34"
PRIVATE = "10.20.30.40"
TOKEN = "offline-provider-fixture"
TRACK = {"uri": "library://track/12", "name": "Fixture track", "media_type": "track",
         "artists": [{"name": "Fixture artist"}], "album": {"name": "Fixture album"}, "duration": 240}
FOLDER = {"path": "filesystem--fixture://", "name": "Fixture folder", "media_type": "folder"}


async def public_resolver(host, port):
    return (PUBLIC,)


def policy(**kwargs):
    return TargetPolicy(blocked_addresses=kwargs.pop("blocked_addresses", lambda: frozenset()),
                        resolver=kwargs.pop("resolver", public_resolver), **kwargs)


def modern(handler=None, *, version=VERIFIED_VERSION, result=None, **kwargs):
    calls = []
    def respond(request):
        calls.append(request)
        if handler:
            return handler(request)
        return httpx.Response(200, json={"server_version": version} if request.url.path == "/info" else result)
    client = BoundedJSONClient(
        policy(origin="https://catalog.invalid:8095", allowed_ips=frozenset({PUBLIC}), **kwargs),
        token=TOKEN, transport=httpx.MockTransport(respond),
    )
    return MusicAssistantAdapter(client, enabled=True), calls


def local(stations=None, *, target_policy=None):
    rows = stations if stations is not None else (LocalStation("1", "Fixture station", f"https://{PUBLIC}/radio.mp3", "audio/mpeg"),)
    def read(query, item_id, limit, offset):
        matches = tuple(row for row in rows if (not query or query.lower() in row.name.lower()) and (not item_id or row.item_id == item_id))
        return matches[offset:offset + limit]
    return LocalStationAdapter(read, target_policy or policy())


@pytest.mark.parametrize("limit,offset", [(0, 0), (101, 0), (True, 0), (1, True), (1, -1), (1, 1001), ("1", 0)])
def test_page_bounds(limit, offset):
    with pytest.raises(ProviderError, match="PROVIDER_INVALID_REQUEST"):
        validate_page(limit, offset)


@pytest.mark.parametrize("text", ["", " ", "bad\nquery", "bad\x00query", "x" * 513])
def test_text_bounds(text):
    with pytest.raises(ProviderError):
        validate_text(text)


def test_default_construction_and_catalog_health_are_offline(monkeypatch):
    def no_dns(*args, **kwargs):
        pytest.fail("disabled provider or catalog listing resolved DNS")
    monkeypatch.setattr("socket.getaddrinfo", no_dns)
    manager = build_provider_manager(ProviderRuntimeConfig())
    assert [item.provider_id for item in manager.descriptors()] == ["local_stations", "music_assistant"]
    assert manager.cached_health("music_assistant").provider == ProviderState.DISABLED
    assert manager.cached_health("local_stations").provider == ProviderState.UNKNOWN
    with pytest.raises(ProviderError, match="PROVIDER_DISABLED"):
        asyncio.run(manager.search("music_assistant", "music"))
    assert asyncio.run(manager.health("local_stations")).provider == ProviderState.HEALTHY


def test_native_catalog_and_candidate_do_not_claim_playback():
    adapter = local()
    manager = ProviderManager((adapter,))
    assert asyncio.run(manager.search("local_stations", "fixture")).items[0].name == "Fixture station"
    assert asyncio.run(manager.browse("local_stations")).items[0].resolvable
    assert asyncio.run(manager.metadata("local_stations", "1")).item_id == "1"
    candidate = asyncio.run(manager.resolve("local_stations", "1"))
    assert candidate.uri == f"https://{PUBLIC}/radio.mp3"
    assert candidate.validated_addresses == (PUBLIC,)
    assert candidate.policy_recheck_required and not candidate.playback_verified
    assert "radio.mp3" not in repr(candidate)
    health = manager.cached_health("local_stations")
    assert health.provider == ProviderState.HEALTHY
    assert health.playback == "UNKNOWN" and health.reporting == "NOT_APPLICABLE"


@pytest.mark.parametrize("item_id", ["", "-1", "1.1", "abc", "0", "１", "1" * 19])
def test_native_identifier_is_bounded(item_id):
    with pytest.raises(ProviderError, match="PROVIDER_INVALID_REQUEST"):
        asyncio.run(local().metadata(item_id))


def test_native_pagination_and_missing_ids():
    adapter = local(tuple(LocalStation(str(index), f"Station {index}", f"https://{PUBLIC}/radio.mp3") for index in range(1, 5)))
    result = asyncio.run(adapter.browse("root", limit=2, offset=1))
    assert [item.item_id for item in result.items] == ["2", "3"] and result.has_more
    with pytest.raises(ProviderError, match="PROVIDER_NOT_FOUND"):
        asyncio.run(adapter.metadata("99"))
    with pytest.raises(ProviderError, match="PROVIDER_NOT_FOUND"):
        asyncio.run(adapter.browse("nested", limit=1, offset=0))


@pytest.mark.parametrize("url", [f"https://{PUBLIC}/stream?token=secret", "http://[invalid/", "file:///media/music", "https://user:secret@example.invalid/music"])
def test_native_sensitive_or_unsafe_candidates_are_not_exposed(url):
    with pytest.raises(ProviderError, match="PROVIDER_UNSAFE_TARGET"):
        asyncio.run(local((LocalStation("1", "fixture", url),)).resolve("1"))


def test_modern_exact_protocol_search_metadata_browse_and_health():
    results = {"music/search": {"tracks": [TRACK], "radio": [{**TRACK, "uri": "library://radio/4", "media_type": "radio"}]},
               "music/browse": [FOLDER, TRACK], "music/item_by_uri": TRACK}
    def handler(request):
        if request.url.path == "/info":
            return httpx.Response(200, json={"server_version": VERIFIED_VERSION})
        command = json.loads(request.content)
        assert set(command) == {"message_id", "command", "args"}
        assert command["command"] in results
        if command["command"] == "music/search":
            assert command["args"] == {"search_query": "Fixture", "media_types": ["track", "radio", "album", "artist", "playlist"], "limit": 25, "library_only": False}
        return httpx.Response(200, json=results[command["command"]])
    adapter, calls = modern(handler)
    manager = ProviderManager((adapter,))
    result = asyncio.run(manager.search("music_assistant", "Fixture"))
    assert [item.media_type for item in result.items] == ["track", "radio"]
    metadata = asyncio.run(manager.metadata("music_assistant", TRACK["uri"]))
    assert metadata.artists == ("Fixture artist",) and metadata.album == "Fixture album"
    result = asyncio.run(manager.browse("music_assistant", limit=1))
    assert result.items[0].browsable and result.has_more
    assert asyncio.run(manager.health("music_assistant")).provider == ProviderState.HEALTHY
    assert len(calls) == 8
    for request in calls:
        assert request.url.host == PUBLIC
        assert request.headers["Host"] == "catalog.invalid:8095"
        assert request.extensions["sni_hostname"] == "catalog.invalid"
        assert request.headers["Accept-Encoding"] == "identity"
        if request.url.path == "/info":
            assert "Authorization" not in request.headers and request.method == "GET"
        else:
            assert request.headers["Authorization"] == f"Bearer {TOKEN}"
            assert request.method == "POST"
    descriptor = adapter.descriptor
    assert descriptor.stage == "LAB" and not descriptor.capabilities.resolve and not descriptor.capabilities.playback
    assert TOKEN not in repr(adapter) + json.dumps(asdict(descriptor)) + json.dumps(asdict(metadata))


@pytest.mark.parametrize("version", [None, "2.7.3", "2.7.4b1", "2.8.0", {}, 274])
def test_unverified_version_never_receives_token(version):
    adapter, calls = modern(version=version)
    with pytest.raises(ProviderError, match="PROVIDER_VERSION_UNSUPPORTED"):
        asyncio.run(adapter.health())
    assert len(calls) == 1 and "Authorization" not in calls[0].headers


@pytest.mark.parametrize("method,args", [
    ("resolve", ("library://track/12",)),
    ("metadata", ("https://example.invalid/music",)),
    ("metadata", ("builtin://track/anything",)),
    ("metadata", ("library://track/../secrets",)),
    ("metadata", ("library://track/%2Fsecret",)),
])
def test_no_synthesized_streams_or_arbitrary_url_indirection(method, args):
    adapter, calls = modern()
    with pytest.raises(ProviderError):
        asyncio.run(getattr(adapter, method)(*args))
    assert calls == []


@pytest.mark.parametrize("query,offset", [("https://example.invalid", 0), ("good", 1)])
def test_unsupported_search_modes_no_io(query, offset):
    adapter, calls = modern()
    with pytest.raises(ProviderError):
        asyncio.run(adapter.search(query, limit=1, offset=offset))
    assert calls == []


@pytest.mark.parametrize("raw", [None, [], {}, {**TRACK, "duration": True}, {**TRACK, "duration": -1},
    {**TRACK, "duration": 604801}, {**TRACK, "name": "x" * 513}, {**TRACK, "artists": "bad"},
    {**TRACK, "artists": [None]}, {**TRACK, "artists": [{}]}, {**TRACK, "uri": "http://example.invalid"},
    {**TRACK, "media_type": "secret"}, {**TRACK, "uri": "library://track/other"}])
def test_malformed_metadata_is_fixed_error(raw):
    adapter, _ = modern(result=raw)
    with pytest.raises(ProviderError, match="PROVIDER_INVALID_RESPONSE"):
        asyncio.run(adapter.metadata(TRACK["uri"]))


@pytest.mark.parametrize("raw", [[], {"tracks": "bad"}, {"tracks": [TRACK] * 101}, {"tracks": [None]}])
def test_malformed_search_results(raw):
    adapter, _ = modern(result=raw)
    with pytest.raises(ProviderError, match="PROVIDER_INVALID_RESPONSE"):
        asyncio.run(adapter.search("music", limit=1, offset=0))


@pytest.mark.parametrize("raw", [{}, [FOLDER] * 5001, [None]])
def test_malformed_browse_results(raw):
    adapter, _ = modern(result=raw)
    with pytest.raises(ProviderError, match="PROVIDER_INVALID_RESPONSE"):
        asyncio.run(adapter.browse("", limit=1, offset=0))


def test_upstream_failure_leaves_native_and_playback_planes_untouched():
    adapter, calls = modern(lambda request: httpx.Response(503, text=f"upstream secret {TOKEN}"))
    manager = ProviderManager((local(), adapter))
    with pytest.raises(ProviderError, match="PROVIDER_UNAVAILABLE") as caught:
        asyncio.run(manager.health("music_assistant"))
    assert TOKEN not in str(caught.value)
    assert len(calls) == 1
    assert asyncio.run(manager.browse("local_stations")).items
    state = manager.cached_health("music_assistant")
    assert state.provider == ProviderState.UNAVAILABLE and state.playback == "UNKNOWN"
    assert state.reporting == "NOT_APPLICABLE"


def test_auth_failure_is_not_playback_failure():
    adapter, _ = modern(lambda request: httpx.Response(401))
    manager = ProviderManager((adapter,))
    with pytest.raises(ProviderError, match="PROVIDER_AUTH_REQUIRED"):
        asyncio.run(manager.health("music_assistant"))
    assert manager.cached_health("music_assistant").provider == ProviderState.AUTH_REQUIRED
    assert manager.cached_health("music_assistant").playback == "UNKNOWN"


def test_manager_single_flight_timeout_and_cancellation_are_isolated():
    async def scenario():
        started = asyncio.Event()
        release = asyncio.Event()
        adapter = local()
        async def slow_health():
            started.set()
            await release.wait()
            return ProviderHealth("local_stations", ProviderState.HEALTHY)
        adapter.health = slow_health
        manager = ProviderManager((adapter,), timeout=0.05)
        task = asyncio.create_task(manager.health("local_stations"))
        await started.wait()
        with pytest.raises(ProviderError, match="PROVIDER_BUSY"):
            await manager.health("local_stations")
        with pytest.raises(ProviderError, match="PROVIDER_TIMEOUT"):
            await task
        assert manager.cached_health("local_stations").playback == "UNKNOWN"
        task = asyncio.create_task(manager.health("local_stations"))
        await asyncio.sleep(0)
        task.cancel()
        with pytest.raises(asyncio.CancelledError):
            await task
        release.set()
        assert (await manager.health("local_stations")).provider == ProviderState.HEALTHY
    asyncio.run(scenario())


def test_manager_unexpected_failures_unknown_provider_and_unsupported_operations():
    adapter = local()
    async def broken():
        raise RuntimeError(f"do not expose {TOKEN}")
    adapter.health = broken
    manager = ProviderManager((adapter, MusicAssistantAdapter(enabled=True)))
    with pytest.raises(ProviderError, match="PROVIDER_UNAVAILABLE") as caught:
        asyncio.run(manager.health("local_stations"))
    assert TOKEN not in str(caught.value)
    for operation in (lambda: manager.cached_health("unknown"), lambda: asyncio.run(manager.health("unknown"))):
        with pytest.raises(ProviderError, match="PROVIDER_NOT_FOUND"):
            operation()
    with pytest.raises(ProviderError, match="PROVIDER_OPERATION_UNSUPPORTED"):
        asyncio.run(manager.resolve("music_assistant", "library://track/12"))
    with pytest.raises(ProviderError, match="PROVIDER_UNAVAILABLE"):
        asyncio.run(manager.health("music_assistant"))
    with pytest.raises(ValueError):
        ProviderManager((adapter, adapter))
    with pytest.raises(ValueError):
        ProviderManager((adapter,), timeout=31)


@pytest.mark.parametrize("config", [
    ProviderRuntimeConfig(True), ProviderRuntimeConfig(True, "http://catalog.invalid", TOKEN, frozenset({PRIVATE})),
    ProviderRuntimeConfig(True, "https://catalog.invalid", TOKEN),
    ProviderRuntimeConfig(True, "https://catalog.invalid", "", frozenset({PRIVATE})),
])
def test_optional_bad_configuration_does_not_break_core(config):
    manager = build_provider_manager(config)
    assert asyncio.run(manager.health("local_stations")).provider == ProviderState.HEALTHY
    with pytest.raises(ProviderError):
        asyncio.run(manager.health("music_assistant"))


def test_environment_configuration_is_opt_in_and_secrets_not_in_repr():
    config = ProviderRuntimeConfig.from_environment({"MUSIC_ASSISTANT_TOKEN": TOKEN,
        "MUSIC_ASSISTANT_URL": "https://catalog.invalid", "MUSIC_ASSISTANT_ALLOWED_IPS": f"{PRIVATE}, ::ffff:{PRIVATE}"})
    assert not config.music_assistant_enabled and TOKEN not in repr(config)
    assert config.music_assistant_allowed_ips == frozenset({PRIVATE, f"::ffff:{PRIVATE}"})
    assert ProviderRuntimeConfig.from_environment({"MUSIC_ASSISTANT_ENABLED": "true"}).music_assistant_enabled
    assert not ProviderRuntimeConfig.from_environment({"MUSIC_ASSISTANT_ENABLED": "yes"}).music_assistant_enabled


def test_adapter_guards_and_bounded_search_are_honest():
    with pytest.raises(ProviderError, match="PROVIDER_DISABLED"):
        asyncio.run(MusicAssistantAdapter().health())
    adapter, calls = modern(result={"tracks": [TRACK, {**TRACK, "uri": "library://track/13"}]})
    with pytest.raises(ProviderError, match="PROVIDER_OPERATION_UNSUPPORTED"):
        asyncio.run(adapter._command("player_queues/play_media", {}))
    assert calls == []
    result = asyncio.run(adapter.search("Fixture", limit=1, offset=0))
    assert len(result.items) == 1 and result.truncated and result.has_more is None
    assert result.offset_supported is False
    invalid, _ = modern(result={})
    with pytest.raises(ProviderError, match="PROVIDER_INVALID_RESPONSE"):
        asyncio.run(invalid.health())


def test_unexpected_optional_constructor_failure_is_isolated(monkeypatch):
    def fail(*args, **kwargs):
        raise RuntimeError("do not expose optional constructor details")
    monkeypatch.setattr("basswiesn.app.services.providers.runtime.BoundedJSONClient", fail)
    manager = build_provider_manager(ProviderRuntimeConfig(True, "https://catalog.invalid", TOKEN, frozenset({PUBLIC})))
    assert asyncio.run(manager.health("local_stations")).provider == ProviderState.HEALTHY
    with pytest.raises(ProviderError, match="PROVIDER_UNAVAILABLE"):
        asyncio.run(manager.health("music_assistant"))
