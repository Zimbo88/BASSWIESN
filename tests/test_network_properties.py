"""Property tests for policy, not actual network experiments."""
import ipaddress

import pytest
from hypothesis import given, settings, strategies as st

from basswiesn.app.services import network_security as network
from basswiesn.app.services.protected_devices import is_protected_ip, _normalize_ip

pytestmark = pytest.mark.unit


@given(st.ip_addresses(v=4).filter(lambda address: address.is_loopback or address.is_link_local or address.is_private or address.is_multicast or address.is_unspecified))
@settings(max_examples=100, deadline=None)
def test_non_public_addresses_never_become_public_media(address):
    assert not network.validate_outbound_http_url(f"http://{address}/media", public_only=True).ok


@given(st.sampled_from(["0.0.0.0", "127.0.0.1", "169.254.169.254", "192.0.2.25"]))
def test_ipv4_mapped_ipv6_preserves_identity(address):
    # IPv4-mapped literals reach the same IPv4 host; spelling cannot defeat protection.
    assert _normalize_ip("::ffff:" + address) == _normalize_ip(address)


def test_mapped_protected_target_rejected_before_transport(monkeypatch):
    monkeypatch.setenv("PROTECTED_DEVICE_IPS", "192.0.2.25")
    assert is_protected_ip("::ffff:192.0.2.25")
    assert not network.validate_outbound_http_url("http://[::ffff:192.0.2.25]/").ok


def test_mixed_dns_answer_is_rejected_not_filtered(monkeypatch):
    monkeypatch.setattr(network, "_resolve", lambda _: (
        ipaddress.ip_address("8.8.8.8"), ipaddress.ip_address("127.0.0.1")))
    assert not network.validate_outbound_http_url("https://example.invalid/audio", public_only=True).ok


def test_pinning_does_not_resolve_again(monkeypatch):
    approved = network.UrlValidation(True, "ok", hostname="example.invalid", addresses=("8.8.8.8",), scheme="https", port=443)
    def forbidden(*args, **kwargs):
        pytest.fail("pinning must not make a second DNS lookup")
    monkeypatch.setattr(network.socket, "getaddrinfo", forbidden)
    target, headers, extensions = network.pinned_http_target("https://example.invalid/audio", approved)
    assert target == "https://8.8.8.8/audio"
    assert headers["Host"] == "example.invalid"
    assert extensions["sni_hostname"] == "example.invalid"
    with pytest.raises(ValueError):
        network.pinned_http_target("https://different.invalid/audio", approved)
