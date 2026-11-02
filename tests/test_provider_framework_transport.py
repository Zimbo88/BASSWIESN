"""SSRF, exact peer binding and credential isolation; zero real network."""

import asyncio

import httpx
import pytest

from basswiesn.app.services.providers.contracts import ProviderError
from basswiesn.app.services.providers.transport import BoundedJSONClient, TargetPolicy, canonical_ip, resolve_addresses, validate_origin
from test_provider_framework import PRIVATE, PUBLIC, TOKEN, modern, policy

pytestmark = pytest.mark.integration


@pytest.mark.parametrize("origin", ["", "http://catalog.invalid", "file:///tmp/file", "https://user:pass@catalog.invalid",
    "https://catalog.invalid/path", "https://catalog.invalid?token=value", "https://catalog.invalid#fragment",
    "https://catalog.invalid:bad", "https://catalog.invalid:0", "https://catalog.invalid:65536",
    "https://catalog.invalid\\@another.invalid", "https://catalog.invalid\n", "https://[fe80::1%25en0]", "https://@catalog.invalid"])
def test_origin_syntax_is_strict_and_offline(origin, monkeypatch):
    monkeypatch.setattr("socket.getaddrinfo", lambda *args: pytest.fail("origin parsing did DNS"))
    with pytest.raises(ProviderError, match="PROVIDER_UNSAFE_TARGET"):
        validate_origin(origin)


def test_origin_canonicalization_and_ip_literal_dns_free(monkeypatch):
    monkeypatch.setattr("socket.getaddrinfo", lambda *args: pytest.fail("literal parsing did DNS"))
    assert validate_origin("https://catalog.invalid:8095/") == "https://catalog.invalid:8095"
    assert asyncio.run(resolve_addresses(PUBLIC, 443)) == (PUBLIC,)
    assert str(canonical_ip(f"::ffff:{PRIVATE}")) == PRIVATE


def test_async_system_resolver_is_mocked_and_deduplicates(monkeypatch):
    async def scenario():
        async def answers(host, port, **kwargs):
            assert (host, port) == ("catalog.invalid", 443)
            return [(None, None, None, None, (PUBLIC, 443)), (None, None, None, None, (PUBLIC, 443))]
        monkeypatch.setattr(asyncio.get_running_loop(), "getaddrinfo", answers)
        assert await resolve_addresses("catalog.invalid", 443) == (PUBLIC,)
    asyncio.run(scenario())


@pytest.mark.parametrize("url", ["file:///data/music", "https:///missing", "https://user:password@catalog.invalid",
    "https://catalog.invalid#secret", "https://catalog.invalid:0", "https://catalog.invalid:99999",
    "https://catalog.invalid:bad", "https://[fe80::1%eth0]/", "https://catalog.invalid\\private",
    "https://catalog.invalid/ white", "https://catalog.invalid/" + "x" * 4096, "https://@catalog.invalid"])
def test_target_syntax_fails_before_resolution(url):
    async def never(*args):
        pytest.fail("malformed URL reached resolver")
    with pytest.raises(ProviderError, match="PROVIDER_UNSAFE_TARGET"):
        asyncio.run(policy(resolver=never).prepare(url))


@pytest.mark.parametrize("address", ["127.0.0.1", "::1", "169.254.169.254", "169.254.1.1", "fe80::1",
    "0.0.0.0", "::", "224.0.0.1", "ff02::1", "100.64.0.1", "192.0.2.1", "2002:7f00:1::", "2001::1",
    "::ffff:127.0.0.1", "::ffff:169.254.169.254", "fe80::1%eth0", "240.0.0.1", "64:ff9b::7f00:1", "64:ff9b:1::1"])
def test_forbidden_peer_cannot_be_allowlisted(address):
    async def resolve(*args):
        return (address,)
    with pytest.raises(ProviderError, match="PROVIDER_UNSAFE_TARGET"):
        asyncio.run(policy(origin="https://catalog.invalid", allowed_ips=frozenset({address}), resolver=resolve).prepare("https://catalog.invalid/api"))


@pytest.mark.parametrize("address", [PRIVATE, "172.20.30.40", "192.168.12.34", "fd12::1", f"::ffff:{PRIVATE}"])
def test_private_peer_requires_exact_operator_configuration(address):
    async def resolve(*args):
        return (address,)
    with pytest.raises(ProviderError, match="PROVIDER_UNSAFE_TARGET"):
        asyncio.run(policy(resolver=resolve).prepare("https://catalog.invalid/api"))
    target = asyncio.run(policy(origin="https://catalog.invalid", allowed_ips=frozenset({address}), resolver=resolve).prepare("https://catalog.invalid/api"))
    assert target.addresses == (str(canonical_ip(address)),)
    assert target.headers == {"Host": "catalog.invalid"}
    assert target.extensions == {"sni_hostname": "catalog.invalid"}
    assert target.url.startswith("https://[fd12::1]" if ":" in address and not address.startswith("::ffff:") else f"https://{str(canonical_ip(address))}")


@pytest.mark.parametrize("url", ["https://another.invalid/api", "http://catalog.invalid/api", "https://catalog.invalid:8095/api"])
def test_exact_origin_rejects_changed_host_scheme_or_port(url):
    with pytest.raises(ProviderError, match="PROVIDER_UNSAFE_TARGET"):
        asyncio.run(policy(origin="https://catalog.invalid", allowed_ips=frozenset({PUBLIC})).prepare(url))


@pytest.mark.parametrize("addresses", [(), (PUBLIC, PRIVATE), (PUBLIC, "127.0.0.1"), ("not-an-ip",), (PUBLIC,) * 33])
def test_empty_mixed_malformed_or_unbounded_dns_answers_fail_closed(addresses):
    async def resolve(*args):
        return addresses
    with pytest.raises(ProviderError, match="PROVIDER_UNSAFE_TARGET"):
        asyncio.run(policy(origin="https://catalog.invalid", allowed_ips=frozenset({PUBLIC}), resolver=resolve).prepare("https://catalog.invalid/api"))


@pytest.mark.parametrize("blocked", [frozenset({PUBLIC}), frozenset({f"::ffff:{PUBLIC}"}), frozenset({"broken-inventory-address"})])
def test_known_radio_inventory_excludes_every_address_spelling(blocked):
    with pytest.raises(ProviderError, match="PROVIDER_UNSAFE_TARGET"):
        asyncio.run(policy(blocked_addresses=lambda: blocked).prepare(f"https://{PUBLIC}/music"))


def test_protected_peer_remains_blocked_when_explicitly_allowlisted():
    async def resolve(*args):
        return ("192.168.50.25",)
    with pytest.raises(ProviderError, match="PROVIDER_UNSAFE_TARGET"):
        asyncio.run(policy(origin="https://catalog.invalid", allowed_ips=frozenset({"192.168.50.25"}), resolver=resolve).prepare("https://catalog.invalid/api"))


@pytest.mark.parametrize("failure", ["dns", "inventory", "protection"])
def test_unavailable_policy_never_connects(failure, monkeypatch):
    async def resolver(*args):
        if failure == "dns":
            raise OSError("DNS cannot expose details")
        return (PUBLIC,)
    def blocked():
        if failure == "inventory":
            raise RuntimeError("inventory unavailable")
        return frozenset()
    if failure == "protection":
        monkeypatch.setattr("basswiesn.app.services.providers.transport.first_protected_ip", lambda addresses: (_ for _ in ()).throw(RuntimeError("failed policy")))
    adapter, calls = modern(resolver=resolver, blocked_addresses=blocked)
    with pytest.raises(ProviderError, match="PROVIDER_UNSAFE_TARGET"):
        asyncio.run(adapter.health())
    assert not calls


def test_rebinding_between_info_and_authenticated_api_is_refused():
    lookups = []
    async def resolver(host, port):
        lookups.append((host, port))
        return (PUBLIC,) if len(lookups) == 1 else (PRIVATE,)
    adapter, calls = modern(result=[], resolver=resolver)
    with pytest.raises(ProviderError, match="PROVIDER_UNSAFE_TARGET"):
        asyncio.run(adapter.health())
    assert len(lookups) == 2 and len(calls) == 1
    assert calls[0].url.host == PUBLIC and "Authorization" not in calls[0].headers


def test_dns_is_not_requeried_by_transport_and_environment_proxy_disabled(monkeypatch):
    lookups = []
    async def resolver(host, port):
        lookups.append((host, port))
        return (PUBLIC,)
    monkeypatch.setenv("HTTPS_PROXY", "http://127.0.0.1:9")
    monkeypatch.setenv("ALL_PROXY", "http://127.0.0.1:9")
    monkeypatch.setattr("socket.getaddrinfo", lambda *args: pytest.fail("HTTP client re-resolved validated target"))
    real_client = httpx.AsyncClient
    options = []
    def client(**kwargs):
        options.append(kwargs)
        return real_client(**kwargs)
    monkeypatch.setattr("basswiesn.app.services.providers.transport.httpx.AsyncClient", client)
    adapter, calls = modern(result=[], resolver=resolver)
    asyncio.run(adapter.health())
    assert len(lookups) == 2 and len(calls) == 2
    assert all(item["trust_env"] is False and item["follow_redirects"] is False for item in options)


@pytest.mark.parametrize("status", [301, 302, 307, 308, 404, 429, 500])
def test_redirect_and_error_body_never_followed_or_exposed(status):
    adapter, calls = modern(lambda request: httpx.Response(status, text=f"private {TOKEN}", headers={"Location": "https://127.0.0.1/private"}))
    with pytest.raises(ProviderError, match="PROVIDER_UNAVAILABLE") as caught:
        asyncio.run(adapter.health())
    assert len(calls) == 1 and TOKEN not in str(caught.value)


@pytest.mark.parametrize("response", [
    lambda: httpx.Response(200, text="not-json"),
    lambda: httpx.Response(200, content=b"{invalid}", headers={"Content-Type": "application/json"}),
    lambda: httpx.Response(200, json={}, headers={"Content-Length": "invalid"}),
    lambda: httpx.Response(200, json={}, headers={"Content-Length": "999999999"}),
    lambda: httpx.Response(200, content=b"x" * 524289, headers={"Content-Type": "application/json", "Content-Length": "1"}),
    lambda: httpx.Response(200, content=b"[" * 2000 + b"]" * 2000, headers={"Content-Type": "application/json"}),
    lambda: httpx.Response(200, json={"server_version": "2.7.4", "ignored": [0] * 20001}),
])
def test_bounded_and_well_formed_json_only(response):
    adapter, _ = modern(lambda request: response())
    with pytest.raises(ProviderError, match="PROVIDER_INVALID_RESPONSE"):
        asyncio.run(adapter.health())


def test_compressed_bodies_rejected_before_decoding():
    # A custom stream avoids HTTPX eagerly decompressing a fixture at creation.
    class Compressed(httpx.AsyncByteStream):
        async def __aiter__(self):
            pytest.fail("compressed payload should not be consumed")
            yield b"unreachable"
    adapter, _ = modern(lambda request: httpx.Response(200, stream=Compressed(), headers={"Content-Type": "application/json", "Content-Encoding": "gzip"}))
    with pytest.raises(ProviderError, match="PROVIDER_INVALID_RESPONSE"):
        asyncio.run(adapter.health())


@pytest.mark.parametrize("exception,code", [(httpx.ReadTimeout("secret"), "PROVIDER_TIMEOUT"),
    (httpx.ConnectError("secret"), "PROVIDER_UNAVAILABLE"), (TimeoutError("secret"), "PROVIDER_TIMEOUT")])
def test_transport_errors_are_typed_redacted(exception, code):
    def handler(request):
        raise exception
    adapter, _ = modern(handler)
    with pytest.raises(ProviderError, match=code) as caught:
        asyncio.run(adapter.health())
    assert "secret" not in str(caught.value)


@pytest.mark.parametrize("token", ["", "short", "whitespace token with spaces", "nonascii-ä-1234567890", "newline\n1234567890", "x" * 8193])
def test_credentials_validated_without_io(token):
    with pytest.raises(ProviderError, match="PROVIDER_AUTH_REQUIRED"):
        BoundedJSONClient(policy(origin="https://catalog.invalid", allowed_ips=frozenset({PUBLIC})), token=token)


def test_transport_constructor_and_path_bounds():
    with pytest.raises(ProviderError):
        policy(origin="https://catalog.invalid", allowed_ips=frozenset({"invalid"}))
    with pytest.raises(ProviderError):
        policy(origin="https://catalog.invalid", allowed_ips=frozenset())
    peer = policy(origin="https://catalog.invalid", allowed_ips=frozenset({PUBLIC}))
    with pytest.raises(ValueError):
        BoundedJSONClient(peer, token=TOKEN, max_bytes=1)
    client = BoundedJSONClient(peer, token=TOKEN)
    for path, payload in (("/secret", None), ("/info", {}), ("/api", None)):
        with pytest.raises(ProviderError, match="PROVIDER_INVALID_REQUEST"):
            asyncio.run(client.request(path, payload))
