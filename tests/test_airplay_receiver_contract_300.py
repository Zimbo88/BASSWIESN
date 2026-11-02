"""Network/DHCP/metadata contracts without DHCP packets or radio traffic."""
import base64
import importlib.util
from hashlib import sha256
from pathlib import Path

import pytest

from basswiesn.app.services.airplay_bridge.metadata_wire import MetadataWireDecoder

pytestmark = pytest.mark.unit

path = Path(__file__).resolve().parents[1] / 'modules/airplay-bridge/network_contract.py'
spec = importlib.util.spec_from_file_location('ap2_network_contract', path)
contract = importlib.util.module_from_spec(spec)
spec.loader.exec_module(contract)


def config():
    return {'interface': 'ap2', 'name': 'AP2 Example', 'network': '192.0.2.0/24',
            'dhcp_server': '192.0.2.1', 'host_ip': '192.0.2.10',
            'deny_ips': ['192.0.2.25'], 'lifetime_seconds': 900}


def test_relay_volume_curve_preserves_native_mute_and_unity_ceiling():
    cfg = {**config(), 'relay_target': '192.0.2.43', 'relay_token': 'a'*64}
    assert contract.relay_volume_configuration(cfg) == (
        ' volume_range_db = 30;\n volume_control_profile = "flat";\n')
    assert contract.relay_volume_configuration(config()) == ''
    assert contract.relay_volume_configuration({**cfg, 'radio_volume_forwarding': True}) == ''
    with pytest.raises(ValueError):
        contract.relay_volume_configuration({**cfg, 'relay_target': '192.0.2.25'})
    with pytest.raises(ValueError):
        contract.relay_volume_configuration({**cfg, 'radio_volume_forwarding': 'true'})


def lease():
    return {'interface': 'ap2', 'serverid': '192.0.2.1', 'ip': '192.0.2.42',
            'subnet': '255.255.255.0', 'lease': '3600'}


def item(kind, code, value=b'', *, length=None):
    return (f'<item><type>{kind.encode().hex()}</type><code>{code.encode().hex()}</code>'
            f'<length>{len(value) if length is None else length}</length>'
            f'<data encoding="base64">{base64.b64encode(value).decode()}</data></item>').encode()


def test_valid_router_lease_only_returns_necessary_fields():
    result = contract.validate_lease(config(), {**lease(), 'dns': '203.0.113.1', 'router': '192.0.2.99'})
    assert result == {'address': '192.0.2.42', 'prefix': 24, 'lease_seconds': 3600, 'server': '192.0.2.1'}


def external_lease():
    cfg = {**config(), 'external_network': True}
    value = {**contract.validate_lease(cfg, lease()), 'state': 'BOUND',
             'expires_monotonic': 3700.0,
             'guard_sha256': sha256(contract.firewall_rules(cfg).encode()).hexdigest()}
    return cfg, value


def test_external_network_requires_live_owner_lease_and_exact_exclusions():
    cfg, value = external_lease()
    assert contract.validate_external_lease(cfg, value, 100.0) == value
    with pytest.raises(ValueError):
        contract.validate_external_lease({**cfg, 'deny_ips': ['192.0.2.26']}, value, 100.0)
    with pytest.raises(ValueError):
        contract.validate_external_lease({**cfg, 'external_network': False}, value, 100.0)


@pytest.mark.parametrize('field,value', [
    ('state', 'UNBOUND'), ('state', 'REJECTED'), ('guard_sha256', ''),
    ('expires_monotonic', 100.0), ('expires_monotonic', True),
    ('expires_monotonic', float('inf')), ('expires_monotonic', float('nan')),
    ('expires_monotonic', 4000.0), ('address', '192.0.2.25'),
    ('address', '192.0.2.10'), ('server', '192.0.2.2'), ('prefix', 16),
])
def test_external_network_rejects_missing_expired_or_different_scope(field, value):
    cfg, observation = external_lease()
    with pytest.raises(ValueError):
        contract.validate_external_lease(cfg, {**observation, field: value}, 100.0)


@pytest.mark.parametrize('value', ['true', 1, None])
def test_external_network_flag_is_not_truthy_input(value):
    with pytest.raises(ValueError):
        contract.validate_config({**config(), 'external_network': value})


@pytest.mark.parametrize('value', ['true', 1, None])
def test_native_group_observer_flag_requires_explicit_boolean(value):
    with pytest.raises(ValueError):
        contract.validate_config({**config(), 'native_group_observer': value})


@pytest.mark.parametrize('key,value', [
    ('ip', '192.0.2.25'), ('ip', '192.0.2.1'), ('ip', '192.0.2.10'), ('ip', '192.0.2.0'),
    ('ip', '192.0.2.255'), ('ip', '198.51.100.42'), ('ip', 'example.invalid'),
    ('serverid', '192.0.2.25'), ('serverid', '192.0.2.2'), ('serverid', ''),
    ('subnet', '255.255.0.0'), ('interface', 'eth0'), ('lease', '0'), ('lease', '-1'),
    ('lease', 'nan'), ('lease', '4294967296'), ('ip', '192.0.2.42; reboot'),
])
def test_bad_lease_never_becomes_configuration(key, value):
    with pytest.raises(ValueError):
        contract.validate_lease(config(), {**lease(), key: value})


@pytest.mark.parametrize('key,value', [
    ('interface', 'ap2;reboot'), ('interface', ''), ('name', 'Not AP2'), ('name', 'AP2 \nInjected'),
    ('name', 'AP2 ' + '🎵' * 20), ('deny_ips', []), ('dhcp_server', '192.0.2.25'),
    ('dhcp_server', '192.0.2.255'), ('host_ip', '192.0.2.25'), ('host_ip', '192.0.2.255'),
    ('host_ip', '192.0.2.0'), ('lifetime_seconds', 1801),
    ('lifetime_seconds', True), ('network', '192.0.2.42/24'),
    ('max_pause_seconds', True), ('max_pause_seconds', 29), ('max_pause_seconds', 901),
    ('max_pause_seconds', '300'),
])
def test_bad_config_rejected(key, value):
    with pytest.raises(ValueError):
        contract.validate_config({**config(), key: value})


def test_long_pause_is_explicitly_bounded_in_receiver_config():
    assert contract.validate_config(config())['max_pause_seconds'] == 300
    assert contract.validate_config({**config(), 'max_pause_seconds': 30})['max_pause_seconds'] == 30


def test_fragmented_metadata_and_multiple_items():
    decoder = MetadataWireDecoder()
    wire = item('ssnc', 'mdst') + item('core', 'minm', 'München'.encode()) + item('ssnc', 'mden')
    events = []
    for byte in wire:
        events.extend(decoder.feed(bytes([byte])))
    assert events == [('ssnc', 'mdst', b''), ('core', 'minm', 'München'.encode()), ('ssnc', 'mden', b'')]
    assert not decoder._buffer


@pytest.mark.parametrize('code', ['acre', 'daid', 'clip', 'cmac', 'snam', 'PICT'])
def test_sensitive_and_artwork_data_never_emitted(code):
    decoder = MetadataWireDecoder()
    assert decoder.feed(item('ssnc', code, b'private-fixture-value')) == []
    assert not decoder._buffer
    assert 'private-fixture-value' not in repr(decoder.__dict__)


@pytest.mark.parametrize('wire', [
    item('core', 'minm', b'wrong length', length=1),
    item('core', 'minm', b'\xff'),
    item('core', 'minm', b'x' * 4097),
    b'<!DOCTYPE item [<!ENTITY x SYSTEM "file:///etc/passwd">]><item>&x;</item>',
    b'<item><type>zz</type></item>',
    b'<item><type>636f7265</type><code>6d696e6d</code><length>1</length><data encoding="base64">@</data></item>',
])
def test_malformed_metadata_is_discarded(wire):
    decoder = MetadataWireDecoder()
    assert decoder.feed(wire) == []
    assert decoder.discarded == 1
    assert decoder.feed(item('core', 'minm', b'Good')) == [('core', 'minm', b'Good')]


def test_metadata_buffer_is_bounded_and_controls_removed():
    decoder = MetadataWireDecoder()
    assert decoder.feed(b'x' * 20000) == []
    assert not decoder._buffer
    assert decoder.feed(item('core', 'minm', b'Hello\x00\nworld')) == [('core', 'minm', b'Helloworld')]


def test_lifecycle_payloads_not_retained():
    decoder = MetadataWireDecoder()
    assert decoder.feed(item('ssnc', 'mdst', b'irrelevant-value')) == [('ssnc', 'mdst', b'')]


def test_relay_target_cannot_be_a_dhcp_lease_and_firewall_only_allows_responses():
    cfg = {**config(), 'relay_target': '192.0.2.42', 'relay_token': 'a' * 64}
    with pytest.raises(ValueError):
        contract.validate_lease(cfg, lease())
    rules = contract.firewall_rules(cfg)
    assert 'ip daddr 192.0.2.42 tcp sport 8098 ct state established accept' in rules
    assert 'ip daddr 192.0.2.42 counter drop' in rules
    assert 'ip daddr { 192.0.2.25 } counter drop' in rules


@pytest.mark.parametrize('target', ['192.0.2.25', '192.0.2.1', '192.0.2.10', '192.0.2.255', '198.51.100.42'])
def test_excluded_relay_target_rejected(target):
    with pytest.raises(ValueError):
        contract.validate_config({**config(), 'relay_target': target, 'relay_token': 'a' * 64})
