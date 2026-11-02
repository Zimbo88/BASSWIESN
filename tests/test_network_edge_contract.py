"""Offline characterization of malformed URLs and approved-origin binding."""
import ipaddress
import socket

import pytest

from basswiesn.app.services import network_security as network
from hypothesis import given, settings, strategies as st

pytestmark = pytest.mark.unit
real_resolve = network._resolve


@pytest.fixture(autouse=True)
def literal_policy_only(monkeypatch):
    monkeypatch.setattr(network, "first_protected_ip", lambda addresses: None)
    monkeypatch.setattr(network, "_resolve", lambda host: (ipaddress.ip_address(host),))


@pytest.mark.parametrize("url", [
    "http://192.0.2.42:invalid/", "http://192.0.2.42:99999/", "http://[invalid]/",
])
def test_untrusted_descriptor_is_rejected_not_an_unhandled_parser_error(url):
    assert not network.validate_local_soundtouch_url(url).ok


@pytest.mark.parametrize("url", ["http://192.0.2.42:0/", "http://@192.0.2.42/", "http://:@192.0.2.42/"])
def test_descriptor_does_not_normalize_invalid_authority_into_approval(url):
    assert not network.validate_local_soundtouch_url(url).ok


@pytest.mark.parametrize("address", [
    "100.64.0.1", "100.127.255.254", "64:ff9b::c000:22a", "64:ff9b:1::c000:22a",
    "2002:c000:022a::1",
])
def test_public_policy_does_not_allow_nonpublic_or_address_translation_routes(address):
    host = f"[{address}]" if ":" in address else address
    assert not network.validate_outbound_http_url(f"https://{host}/", public_only=True).ok


@pytest.mark.parametrize("url", [
    "http://example.invalid/audio", "https://example.invalid:8443/audio",
    "ftp://example.invalid/audio", "https://user@example.invalid/audio",
])
def test_pinned_target_stays_inside_the_approved_origin(url):
    approved = network.UrlValidation(True, "ok", hostname="example.invalid",
                                     addresses=("8.8.8.8",), scheme="https", port=443)
    with pytest.raises(ValueError):
        network.pinned_http_target(url, approved)


def test_an_explicit_default_port_remains_same_origin():
    approved = network.UrlValidation(True, "ok", hostname="example.invalid",
                                     addresses=("8.8.8.8",), scheme="https", port=443)
    target, headers, _ = network.pinned_http_target("https://example.invalid:443/audio", approved)
    assert target == "https://8.8.8.8:443/audio"
    assert headers["Host"] == "example.invalid"


@given(st.ip_addresses(v=4).filter(lambda value: not value.is_global or value.is_multicast))
@settings(max_examples=100, deadline=None)
def test_ipv4_mapped_forms_keep_the_same_public_rejection(value):
    assert network._is_blocked_outbound(ipaddress.ip_address(f"::ffff:{value}"))


def test_invalid_authorities_are_rejected_before_dns(monkeypatch):
    def no_dns(*args):
        pytest.fail("invalid authority reached DNS")
    monkeypatch.setattr(network, "_resolve", no_dns)
    for url in ("http://[", "http://[invalid]/", "http://192.0.2.42:0/",
                "http://192.0.2.42:/", "http://@192.0.2.42/", "http://192.0.2.42:65536/",
                "http://192.0.2.42\\@example.invalid/", "http://192.0.2.42/\r\n",
                "\thttp://192.0.2.42/", "http://%31%32%37.0.0.1/", "not a url", ""):
        assert not network.validate_outbound_http_url(url).ok
        assert not network.validate_local_soundtouch_url(url).ok


def test_empty_descriptor_port_policy_is_not_default_permission():
    assert not network.validate_local_soundtouch_url("http://192.0.2.42/", allowed_ports=set()).ok


def test_descriptor_private_address_class_does_not_include_unspecified_or_reserved():
    for host in ("0.0.0.0", "240.0.0.1", "127.0.0.1", "169.254.1.2"):
        assert not network.validate_local_soundtouch_url(f"http://{host}/").ok
    for host in ("192.0.2.42", "198.51.100.42", "203.0.113.42"):
        assert network.validate_local_soundtouch_url(f"http://{host}/").ok


def test_rejected_or_incomplete_origin_receipts_are_not_authorization():
    for validation in (
        network.UrlValidation(False, "blocked", "example.invalid", ("8.8.8.8",), "https", 443),
        network.UrlValidation(True, "incomplete", "example.invalid", ("8.8.8.8",)),
        network.UrlValidation(True, "missing address", "example.invalid", (), "https", 443),
        network.UrlValidation(True, "malformed address", "example.invalid", ("invalid",), "https", 443),
    ):
        with pytest.raises(ValueError):
            network.pinned_http_target("https://example.invalid/audio", validation)


@pytest.mark.parametrize("error", [OSError("private resolver detail"), UnicodeError("private resolver detail")])
def test_dns_failures_are_bounded_rejections_without_raw_resolver_details(monkeypatch, error):
    def broken(*args):
        raise error
    monkeypatch.setattr(network, "_resolve", broken)
    results = (
        network.validate_outbound_http_url("https://example.invalid/audio"),
        network.validate_local_soundtouch_url("http://example.invalid/descriptor"),
        network.validate_outbound_host("example.invalid", port=22),
    )
    assert all(not result.ok and result.reason == "DNS resolution failed" for result in results)


def test_mixed_public_and_translation_dns_answer_is_not_filtered_to_permission(monkeypatch):
    monkeypatch.setattr(network, "_resolve", lambda host: (
        ipaddress.ip_address("8.8.8.8"), ipaddress.ip_address("64:ff9b::c000:22a"),
    ))
    assert not network.validate_public_callback_url("https://example.invalid/hook").ok


def test_resolver_deduplicates_addresses_and_ignores_non_ip_sockaddr(monkeypatch):
    monkeypatch.setattr(socket, "getaddrinfo", lambda *args: [
        (socket.AF_INET, socket.SOCK_STREAM, 6, "", ("8.8.8.8", 80)),
        (socket.AF_INET, socket.SOCK_STREAM, 6, "", ("8.8.8.8", 80)),
        (socket.AF_INET, socket.SOCK_STREAM, 6, "", ("invalid", 80)),
        (socket.AF_UNIX, socket.SOCK_STREAM, 0, "", (1, 80)),
    ])
    assert real_resolve("example.invalid") == (ipaddress.ip_address("8.8.8.8"),)


def test_boolean_port_is_not_an_origin_receipt():
    validation = network.UrlValidation(True, "malformed", "example.invalid", ("8.8.8.8",), "http", True)
    with pytest.raises(ValueError):
        network.pinned_http_target("http://example.invalid:1/audio", validation)
