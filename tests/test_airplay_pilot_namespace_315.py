"""Root owner safety contracts: mocked commands only, never Docker or LAN."""
from hashlib import sha256
import importlib
import json
import os
from pathlib import Path
import stat
import sys
from types import SimpleNamespace

import pytest

MODULES = Path(__file__).resolve().parents[1] / 'modules/airplay-bridge'
sys.path.insert(0, str(MODULES))
pilot = importlib.import_module('pilot_namespace')
dhcp = importlib.import_module('pilot_dhcp')
sys.path.remove(str(MODULES))
pytestmark = pytest.mark.unit


def config(slot=1):
    cfg = {'trial_id': 'offline-example', 'slot': slot, 'parent_interface': 'eth0',
        'image_id': pilot.IMAGE_ID, 'application_uid': 10001,
        'run_dir': f'/run/example/slot{slot}', 'ipc_socket': f'/run/app-private/slot{slot}.sock',
        'lifetime_seconds': 600, 'radio_ips': ['192.0.2.20', '192.0.2.21', '192.0.2.25'],
        'receiver': {'interface': 'ap2', 'name': 'AP2 Offline Example',
            'network': '192.0.2.0/24', 'dhcp_server': '192.0.2.1', 'host_ip': '192.0.2.10',
            'deny_ips': ['192.0.2.21', '192.0.2.25'], 'lifetime_seconds': 600,
            'native_group_observer': True, 'external_network': True}}
    if slot == 1:
        cfg['receiver'].update(relay_target='192.0.2.20', relay_token='a' * 64, controller_nonce='b' * 32)
    else:
        cfg['receiver']['deny_ips'].append('192.0.2.20')
    return cfg


def status():
    return {'stage': 'RECEIVER_RUNNING_NOT_APPLE_VERIFIED', 'address': '192.0.2.42',
        'native_group_contract': 'basswiesn-group-v1', 'native_group_binary_sha256': pilot.BINARY_SHA256,
        'native_group': {'revision': 1, 'connection': 0, 'state': 'IDLE',
            'group_hash': '', 'event_monotonic': 10.0}}


def lease(**updates):
    return {'interface': 'ap2', 'serverid': '192.0.2.1', 'ip': '192.0.2.42',
        'subnet': '255.255.255.0', 'lease': '3600', **updates}


@pytest.mark.parametrize('slot', [1, 2])
def test_fixed_valid_pair_and_unique_ownership(slot):
    cfg = pilot.validate_owner_config(config(slot))
    owner, link, mac = pilot.identity(cfg)
    assert owner.endswith(f'slot{slot}') and len(link) <= 15 and mac.startswith('02:ba:')
    assert pilot.identity(config(1)) != pilot.identity(config(2))
    assert pilot.identity(cfg) != pilot.identity({**cfg, 'trial_id': 'another-trial'})


def test_stable_virtual_identity_survives_restart_without_cloning_radio():
    cfg = pilot.validate_owner_config({**config(), 'receiver_key': 'c' * 32})
    first = pilot.identity(cfg)
    later = pilot.identity({**cfg, 'trial_id': 'next-process'})
    assert first[:2] != later[:2] and first[2] == later[2]
    assert first[2].startswith('02:') and len(first[2]) == 17
    assert pilot.identity({**cfg, 'receiver_key': 'd' * 32})[2] != first[2]
    args = pilot.docker_arguments(cfg, 'owned', '/run/config', '/run/cid')
    assert f'{pilot.MAC_LABEL}={first[2]}' in args
    assert not any('c' * 32 in arg for arg in args)


@pytest.mark.parametrize('key', [None, True, '', 'X' * 32, 'a' * 31, '../../untrusted'])
def test_invalid_stable_identity_never_reaches_provisioning(key):
    with pytest.raises(ValueError):
        pilot.validate_owner_config({**config(), 'receiver_key': key})


@pytest.fixture
def identity_lock(tmp_path, monkeypatch):
    # Real flock/inodes in a disposable directory; root ownership is simulated,
    # never sudo or an actual host-network operation.
    original = os.fstat
    def root_stat(fd):
        value = list(original(fd))
        value[4] = 0
        return os.stat_result(value)
    monkeypatch.setattr(pilot.os, 'fstat', root_stat)
    monkeypatch.setattr(pilot, 'root_path', lambda path, **kw: path)
    cfg = pilot.validate_owner_config({**config(), 'receiver_key': 'c' * 32})
    return cfg, tmp_path / 'identities'


def test_stable_identity_reservation_blocks_second_owner_and_preserves_lock_inode(identity_lock):
    cfg, root = identity_lock
    first = pilot.IdentityReservation(cfg, root=root)
    second = pilot.IdentityReservation({**cfg, 'trial_id': 'second'}, root=root)
    calls = []
    def call(args):
        calls.append(args)
        return SimpleNamespace(stdout=b'[]' if args[0] == pilot.IP else b'')
    first.acquire(call)
    try:
        with pytest.raises(BlockingIOError):
            second.acquire(call)
        assert len(calls) == 2
        node = next(root.iterdir())
        inode = node.stat().st_ino
    finally:
        first.close()
    second.acquire(call)
    second.close()
    assert node.stat().st_ino == inode
    assert calls[0][:4] == [pilot.DOCKER, 'ps', '-aq', '--filter']


def test_orphaned_receiver_blocks_identity_reuse_without_deleting_anything(identity_lock):
    cfg, root = identity_lock
    owner = pilot.IdentityReservation(cfg, root=root)
    calls = []
    def orphan(args):
        calls.append(args)
        return SimpleNamespace(stdout=b'owned-old-container\n')
    with pytest.raises(pilot.OwnerRejected, match='ORPHANED'):
        owner.acquire(orphan)
    assert owner.fd is None and len(calls) == 1
    # The lock is released on failure; explicit reconciliation must handle the
    # orphan, not this helper. A later empty readback can reserve the identity.
    owner.acquire(lambda args: SimpleNamespace(stdout=b'[]' if args[0] == pilot.IP else b''))
    owner.close()


def test_orphaned_host_link_blocks_identity_without_deleting_it(identity_lock):
    cfg, root = identity_lock
    owner = pilot.IdentityReservation(cfg, root=root)
    calls = []
    def orphan(args):
        calls.append(args)
        return SimpleNamespace(stdout=json.dumps([
            {'ifname': 'old-owned-link', 'address': pilot.identity(cfg)[2]}]).encode()
            if args[0] == pilot.IP else b'')
    with pytest.raises(pilot.OwnerRejected, match='ORPHANED'):
        owner.acquire(orphan)
    assert owner.fd is None
    assert calls == [
        [pilot.DOCKER, 'ps', '-aq', '--filter', f'label={pilot.MAC_LABEL}={pilot.identity(cfg)[2]}'],
        [pilot.IP, '-j', 'link', 'show'],
    ]


@pytest.mark.parametrize('unsafe', ['symlink', 'hardlink', 'permissions'])
def test_stable_identity_rejects_unsafe_lock_before_docker(identity_lock, unsafe):
    cfg, root = identity_lock
    root.mkdir(mode=0o700)
    node = root / (pilot.identity(cfg)[2].replace(':', '') + '.lock')
    other = root / 'other'
    other.touch(mode=0o600)
    if unsafe == 'symlink':
        node.symlink_to(other)
    elif unsafe == 'hardlink':
        os.link(other, node)
    else:
        node.touch(mode=0o600)
        node.chmod(0o666)
    owner = pilot.IdentityReservation(cfg, root=root)
    with pytest.raises((OSError, pilot.OwnerRejected)):
        owner.acquire(lambda args: pytest.fail('unsafe lock must precede Docker access'))
    assert owner.fd is None


def test_long_host_mount_path_allowed_for_proc_fd_socket_binding():
    cfg = {**config(), 'ipc_socket': '/home/operator/' + '/long-directory' * 12 + '/receiver.sock'}
    assert pilot.validate_owner_config(cfg)['ipc_socket'] == cfg['ipc_socket']


def test_failure_reporting_never_copies_sensitive_exception_payloads():
    secret = 'a private command nonce or stream URL'
    value = pilot.failure_details('CHECK_DHCP_HOOK_EXECUTABLE',
        pilot.OwnerRejected('DHCP_HOOK_NOT_EXECUTABLE'))
    assert value == {'failure_stage': 'CHECK_DHCP_HOOK_EXECUTABLE',
        'error_code': 'DHCP_HOOK_NOT_EXECUTABLE', 'error_class': 'OwnerRejected'}
    value = pilot.failure_details('START_PINNED_RECEIVER',
        pilot.subprocess.CalledProcessError(1, secret, output=secret, stderr=secret))
    assert value['error_code'] == 'OWNER_COMMAND_FAILED' and secret not in json.dumps(value)


@pytest.mark.parametrize('key,value', [
    ('trial_id', '../escape'), ('trial_id', ''), ('trial_id', 'X'), ('trial_id', 'x' * 41),
    ('slot', True), ('slot', 3), ('parent_interface', 'wlan0'), ('image_id', 'tag:latest'),
    ('image_id', 'sha256:' + 'a' * 64), ('application_uid', 0), ('application_uid', 10002),
    ('lifetime_seconds', 901), ('lifetime_seconds', True), ('lifetime_seconds', 59),
    ('run_dir', '/'), ('run_dir', '/run/../etc'), ('run_dir', 'relative'),
    ('run_dir', '/run/example,readonly=false'), ('ipc_socket', '/run/' + 'x' * 101),
    ('radio_ips', []), ('radio_ips', ['192.0.2.20', '192.0.2.20']),
])
def test_owner_config_rejects_scope_widening(key, value):
    with pytest.raises(ValueError):
        pilot.validate_owner_config({**config(), key: value})


@pytest.mark.parametrize('mutate', [
    lambda cfg: cfg.update(unplanned_command='no'),
    lambda cfg: cfg['receiver'].update(interface='eth0'),
    lambda cfg: cfg['receiver'].update(external_network=False),
    lambda cfg: cfg['receiver'].update(native_group_observer=False),
    lambda cfg: cfg['receiver'].update(lifetime_seconds=900),
    lambda cfg: cfg['receiver'].update(deny_ips=['192.0.2.25']),
    lambda cfg: cfg['receiver'].pop('controller_nonce'),
    lambda cfg: cfg['receiver'].update(relay_target='192.0.2.29'),
    lambda cfg: cfg['receiver'].update(deny_ips=['192.0.2.20', '192.0.2.21', '192.0.2.25']),
    lambda cfg: cfg['receiver'].update(unplanned='no'),
])
def test_master_policy_requires_complete_prohibitions_and_control(mutate):
    cfg = config()
    mutate(cfg)
    with pytest.raises(ValueError):
        pilot.validate_owner_config(cfg)


@pytest.mark.parametrize('key,value', [('relay_target', None), ('relay_token', 'a' * 64),
    ('controller_nonce', 'b' * 32)])
def test_second_receiver_never_has_a_relay_or_controller(key, value):
    cfg = config(2)
    cfg['receiver'][key] = value
    with pytest.raises(ValueError):
        pilot.validate_owner_config(cfg)


def test_docker_command_nonroot_pinned_no_network_no_capabilities():
    args = pilot.docker_arguments(config(), 'exact-name', '/run/owned/config', '/run/owned/cid')
    assert args[-1] == pilot.IMAGE_ID and 'tag:latest' not in args
    for option, value in [('--network', 'none'), ('--user', 'avahi'), ('--cap-drop', 'ALL'),
            ('--security-opt', 'no-new-privileges:true')]:
        assert args[args.index(option) + 1] == value
    assert '--read-only' in args and '--privileged' not in args
    assert 'net.ipv6.conf.all.disable_ipv6=1' in args and 'net.ipv6.conf.default.disable_ipv6=1' in args
    assert 'noexec' in args[args.index('--tmpfs') + 1]
    assert args[args.index('--mount') + 1].endswith(',dst=/config,readonly')
    assert not any('docker.sock' in arg or arg == '--restart' for arg in args)


def test_firewall_only_router_dhcp_and_target_stream_never_radio_control():
    cfg = pilot.validate_owner_config(config())
    guard = pilot.dhcp_guard(cfg)
    assert 'ip saddr 192.0.2.1 udp sport 67 udp dport 68 accept' in guard
    assert 'udp sport 67 counter drop' in guard and 'udp dport 68 counter drop' in guard
    guard = pilot.firewall_rules(cfg['receiver'])
    assert 'ip daddr 192.0.2.20 tcp sport 8098 ct state established accept' in guard
    assert 'ip daddr 192.0.2.20 counter drop' in guard
    assert 'ip saddr 192.0.2.20 tcp dport 8098 accept' in guard
    assert '192.0.2.21' in guard and '192.0.2.25' in guard


def test_native_ready_requires_idle_and_pinned_binary():
    assert pilot.native_ready(status())
    assert not pilot.native_ready({'stage': 'WAIT_INTERFACE'})
    assert not pilot.native_ready({**status(), 'native_group': None})
    for patch in [{'native_group_binary_sha256': 'a' * 64}, {'native_group_contract': 'unknown'},
            {'native_group': {**status()['native_group'], 'connection': 1}},
            {'native_group': {**status()['native_group'], 'state': 'GROUPED'}},
            {'native_group': {**status()['native_group'], 'event_monotonic': float('nan')}}]:
        with pytest.raises(ValueError):
            pilot.native_ready({**status(), **patch})
    for filename in ('owner-binding.json', 'native-group.json', 'owner-observation.json'):
        assert filename in pilot.IPC_PREFLIGHT
    assert 'NativeGroupCursor' in pilot.IPC_PREFLIGHT and 'hashlib.sha256' in pilot.IPC_PREFLIGHT


@pytest.mark.parametrize('bad', ['symlink', 'owner', 'writable', 'filetype'])
def test_root_path_rejects_unsafe_components(monkeypatch, bad):
    def info(path):
        mode = stat.S_IFREG | 0o600 if path == Path('/run/private/config.json') else stat.S_IFDIR | 0o755
        uid = 0
        if path == Path('/run/private'):
            if bad == 'symlink': mode = stat.S_IFLNK | 0o777
            if bad == 'owner': uid = 10001
            if bad == 'writable': mode |= 0o020
            if bad == 'filetype': mode = stat.S_IFREG | 0o600
        return SimpleNamespace(st_uid=uid, st_mode=mode, st_nlink=1)
    monkeypatch.setattr(Path, 'lstat', info)
    with pytest.raises(ValueError):
        dhcp.root_path('/run/private/config.json', private=True)


def test_dhcp_refuses_host_namespace_before_config_or_commands(monkeypatch):
    monkeypatch.setattr(dhcp.os, 'geteuid', lambda: 0)
    monkeypatch.setattr(dhcp.os, 'readlink', lambda path: 'net:[1]')
    monkeypatch.setenv('BASSWIESN_AP2_NETNS', 'net:[1]')
    monkeypatch.setattr(dhcp, 'read_root_json', lambda *_: pytest.fail('must not read config'))
    with pytest.raises(ValueError, match='private namespace'):
        dhcp.main()


@pytest.fixture
def hook(monkeypatch, tmp_path):
    calls, saved = [], []
    def run(args, **options):
        calls.append(args)
        return SimpleNamespace(returncode=0)
    monkeypatch.setattr(dhcp, 'save_json', lambda path, value, **kw: saved.append(value))
    return calls, saved, run, tmp_path / 'lease.json'


def test_dhcp_validates_then_conflict_checks_and_sets_only_private_address(hook):
    calls, saved, run, state = hook
    cfg = config()['receiver']
    value = dhcp.apply_lease('bound', cfg, state,
        lease(router='192.0.2.99', dns='203.0.113.8', staticroutes='arbitrary'), run=run, now=lambda: 10)
    assert calls[0] == ['/usr/bin/busybox', 'arping', '-D', '-c', '3', '-w', '4', '-I', 'ap2', '192.0.2.42']
    assert calls[1] == ['/usr/sbin/ip', '-4', 'addr', 'replace', '192.0.2.42/24', 'dev', 'ap2']
    assert value['state'] == 'BOUND' and value['expires_monotonic'] == 3610
    assert value['guard_sha256'] == sha256(pilot.firewall_rules(cfg).encode()).hexdigest()
    assert saved == [value] and all('route' not in command for command in calls)


@pytest.mark.parametrize('updates', [{'ip': '192.0.2.20'}, {'ip': '192.0.2.21'},
    {'ip': '192.0.2.25'}, {'ip': '192.0.2.10'}, {'serverid': '192.0.2.99'},
    {'interface': 'eth0'}, {'subnet': '255.255.0.0'}])
def test_dhcp_rejected_offer_never_arping_or_applies_address(hook, updates):
    calls, saved, run, state = hook
    value = dhcp.apply_lease('bound', config()['receiver'], state, lease(**updates), run=run)
    assert value == {'state': 'REJECTED'}
    assert calls == [['/usr/sbin/ip', '-4', 'addr', 'flush', 'dev', 'ap2']]


def test_dhcp_conflict_flushes_and_reports_without_address_install(hook):
    calls, saved, run, state = hook
    def conflict(args, **options):
        result = run(args, **options)
        result.returncode = 1 if 'arping' in args else 0
        return result
    assert dhcp.apply_lease('bound', config()['receiver'], state, lease(), run=conflict) == {'state': 'ADDRESS_CONFLICT'}
    assert not any('replace' in args for args in calls)
    assert calls[-1] == ['/usr/sbin/ip', '-4', 'addr', 'flush', 'dev', 'ap2']


def test_existing_run_directory_cannot_be_adopted_or_overwritten(monkeypatch, tmp_path):
    cfg = {**config(), 'run_dir': str(tmp_path)}
    owner = pilot.NamespaceOwner(cfg)
    monkeypatch.setattr(pilot, 'root_path', lambda *_args, **_kw: None)
    monkeypatch.setattr(pilot, 'save_json', lambda *_args, **_kw: pytest.fail('must not overwrite old run'))
    monkeypatch.setattr(owner, 'call', lambda *_args, **_kw: pytest.fail('must not execute commands'))
    with pytest.raises(FileExistsError):
        owner.setup()
    assert owner.cleanup()


def test_cleanup_only_exact_cid_and_mac(monkeypatch, tmp_path):
    owner = pilot.NamespaceOwner({**config(), 'run_dir': str(tmp_path)})
    owner.cid, owner.link_created = 'c' * 64, True
    calls = []
    monkeypatch.setattr(owner, 'inspect', lambda target: {'Id': target, 'Image': pilot.IMAGE_ID,
        'Config': {'Labels': {pilot.LABEL: owner.owner}}})
    def call(args, **options):
        calls.append(args)
        payload = json.dumps([{'address': owner.mac}]).encode() if '-j' in args else b''
        return SimpleNamespace(returncode=1 if args[1:2] == ['inspect'] else 0, stdout=payload)
    monkeypatch.setattr(owner, 'call', call)
    assert owner.cleanup()
    assert [pilot.DOCKER, 'stop', '-t', '12', 'c' * 64] in calls
    assert [pilot.DOCKER, 'rm', 'c' * 64] in calls
    assert [pilot.IP, 'link', 'delete', owner.link] in calls
    assert all('production' not in args and owner.name not in args for args in calls)


def test_cleanup_refuses_mismatched_container_and_link(monkeypatch, tmp_path):
    owner = pilot.NamespaceOwner({**config(), 'run_dir': str(tmp_path)})
    owner.cid, owner.link_created = 'c' * 64, True
    monkeypatch.setattr(owner, 'inspect', lambda target: {'Id': target, 'Image': 'wrong'})
    calls = []
    def call(args, **options):
        calls.append(args)
        return SimpleNamespace(returncode=0, stdout=json.dumps([{'address': 'wrong'}]).encode())
    monkeypatch.setattr(owner, 'call', call)
    assert not owner.cleanup()
    assert len(owner.result['cleanup_errors']) == 2
    assert not any('stop' in args or 'rm' in args or 'delete' in args for args in calls)


def test_stop_file_signal_and_absolute_deadline_are_independent(tmp_path):
    owner = pilot.NamespaceOwner({**config(), 'run_dir': str(tmp_path)})
    assert not owner.stopped()
    owner.stop = True
    assert owner.stopped()
    owner.stop = False
    (tmp_path / 'STOP').touch()
    assert owner.stopped()
    (tmp_path / 'STOP').unlink()
    owner.deadline = 0
    assert owner.stopped()


def test_startup_installs_both_guards_and_all_prohibits_before_link_up(monkeypatch, tmp_path):
    endpoint = tmp_path / 'ipc'
    endpoint.mkdir(mode=0o700)
    cfg = pilot.validate_owner_config({**config(), 'run_dir': str(tmp_path / 'new-run'),
        'ipc_socket': str(endpoint / 'receiver.sock')})
    owner = pilot.NamespaceOwner(cfg)
    commands, children = [], []
    original_fstat = pilot.os.fstat
    directory_inode = endpoint.stat().st_ino
    def fstat(fd):
        info = original_fstat(fd)
        return SimpleNamespace(st_uid=10001, st_mode=stat.S_IFDIR | 0o700) if info.st_ino == directory_inode else info
    monkeypatch.setattr(pilot.os, 'fstat', fstat)
    monkeypatch.setattr(pilot.os, 'readlink', lambda path: 'net:[1]' if path == '/proc/1/ns/net' else 'net:[2]')
    monkeypatch.setattr(pilot, 'root_path', lambda *_args, **_kw: None)
    monkeypatch.setattr(pilot, 'save_json', lambda *_args, **_kw: None)
    monkeypatch.setattr(owner, 'routes', lambda: {})
    monkeypatch.setattr(owner, 'inspect', lambda target: {'Id': pilot.IMAGE_ID, 'Config': {'User': 'avahi'}})
    monkeypatch.setattr(owner, 'owned_container', lambda: {'State': {'Pid': 1234, 'Running': True}})
    def call(args, **options):
        commands.append((args, options))
        if args[:2] == [pilot.DOCKER, 'run']:
            (owner.run_dir / 'container.cid').write_text('c' * 64)
        if args[:5] == [pilot.IP, '-j', 'link', 'show', 'eth0']:
            return SimpleNamespace(returncode=0, stdout=b'[{"operstate":"UP"}]')
        if args[:2] == [pilot.DOCKER, 'inspect'] or args[:3] == [pilot.IP, 'link', 'show']:
            return SimpleNamespace(returncode=1, stdout=b'')
        return SimpleNamespace(returncode=0, stdout=b'')
    monkeypatch.setattr(owner, 'call', call)
    def spawn(args, **options):
        children.append((args, options))
        return SimpleNamespace(poll=lambda: None)
    monkeypatch.setattr(pilot.subprocess, 'Popen', spawn)
    try:
        owner.setup()
        nft_index = next(i for i, (args, _) in enumerate(commands) if '/usr/sbin/nft' in args)
        up_index = next(i for i, (args, _) in enumerate(commands) if args[-4:] == ['link', 'set', 'ap2', 'up'])
        prohibit_indices = [i for i, (args, _) in enumerate(commands) if 'prohibit' in args]
        assert len(prohibit_indices) == len(cfg['receiver']['deny_ips'])
        assert nft_index < min(prohibit_indices) <= max(prohibit_indices) < up_index
        assert b'table ip ap2_guard' in commands[nft_index][1]['input']
        assert b'table ip ap2_dhcp_guard' in commands[nft_index][1]['input']
        assert all(args[0] == pilot.NSENTER for args, _ in commands if 'route' in args)
        assert children[0][1]['env']['BASSWIESN_AP2_CONFIG'] == str(owner.run_dir / 'config/receiver.json')
        assert children[0][1]['env']['BASSWIESN_AP2_NETNS'] == 'net:[2]'
        assert not any('namespace_ipc.py' in arg for args, _ in children for arg in args)
    finally:
        pilot.os.close(owner.ipc_directory)


def test_bridge_is_not_exposed_when_fixed_native_files_are_not_ready(monkeypatch, tmp_path):
    owner = pilot.NamespaceOwner({**config(), 'run_dir': str(tmp_path)})
    owner.cid = 'c' * 64
    owner.child = SimpleNamespace(poll=lambda: None)
    writes, commands = [], []
    monkeypatch.setattr(pilot, 'save_json', lambda path, value: writes.append(path.name))
    monkeypatch.setattr(pilot.time, 'sleep', lambda _: None)
    monkeypatch.setattr(pilot.subprocess, 'Popen', lambda *_args, **_kw: pytest.fail('IPC must remain unavailable'))
    def call(args, **options):
        commands.append(args)
        if args[-1] == '/run/ap2/status.json':
            return SimpleNamespace(returncode=0, stdout=json.dumps(status()).encode())
        owner.stop = True
        return SimpleNamespace(returncode=1, stdout=b'')
    monkeypatch.setattr(owner, 'call', call)
    owner.wait()
    assert not owner.result['ready'] and writes == ['STATUS.json']
    assert any(pilot.IPC_PREFLIGHT in args for args in commands)
