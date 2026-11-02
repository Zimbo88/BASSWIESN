"""Actual local IPC parser/socket paths; Docker/radios are never contacted."""
import asyncio
import importlib
import importlib.util
import json
import os
from pathlib import Path
import selectors
import sys
import time

import pytest

from basswiesn.app.services.airplay_bridge.receiver_port import ReceiverPort
from basswiesn.app.services.airplay_bridge.unix_ipc import UnixReceiverIPC
from test_airplay_receiver_contract_300 import config, contract
from test_airplay_control_lease_315 import NONCE, control

ROOT = Path(__file__).resolve().parents[1]
IMAGE = 'sha256:' + 'f'*64
CID = 'c'*64


def module(name):
    spec = importlib.util.spec_from_file_location(name, ROOT/'modules/airplay-bridge'/f'{name}.py')
    loaded = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(loaded)
    return loaded


def test_controlled_receiver_waits_for_first_fresh_held_command(tmp_path, monkeypatch):
    for name in ('control_lease', 'native_group', 'lifecycle'):
        monkeypatch.setitem(sys.modules, 'airplay_core.'+name,
            importlib.import_module('basswiesn.app.services.airplay_bridge.'+name))
    monkeypatch.setitem(sys.modules, 'metadata_wire',
        importlib.import_module('basswiesn.app.services.airplay_bridge.metadata_wire'))
    monkeypatch.setitem(sys.modules, 'network_contract', contract)
    receiver = module('receiver_probe')
    cfg = {'relay_target': '192.0.2.43', 'controller_nonce': NONCE}
    tmp_path.chmod(0o700)
    assert not receiver.output_start_authorized({}, tmp_path, time.monotonic())
    assert receiver.output_start_authorized({'relay_target':'192.0.2.43'}, tmp_path, time.monotonic())
    assert not receiver.output_start_authorized(cfg, tmp_path, time.monotonic())
    path = tmp_path/'controller.json'
    for changes, accepted in (({}, True), ({'observed_monotonic':1.}, False),
                              ({'allow_audio':True}, False), ({'nonce':'f'*32}, False)):
        path.write_bytes(control(observed_monotonic=time.monotonic(), **changes)
            if 'observed_monotonic' not in changes else control(**changes))
        path.chmod(0o600)
        if accepted:
            assert receiver.output_start_authorized(cfg, tmp_path, time.monotonic())
        else:
            with pytest.raises(ValueError):
                receiver.output_start_authorized(cfg, tmp_path, time.monotonic())


def test_pending_output_drains_pcm_then_transfers_exactly_one_reader(tmp_path, monkeypatch):
    for name in ('control_lease', 'native_group', 'lifecycle'):
        monkeypatch.setitem(sys.modules, 'airplay_core.' + name,
            importlib.import_module('basswiesn.app.services.airplay_bridge.' + name))
    monkeypatch.setitem(sys.modules, 'metadata_wire',
        importlib.import_module('basswiesn.app.services.airplay_bridge.metadata_wire'))
    monkeypatch.setitem(sys.modules, 'network_contract', contract)
    receiver = module('receiver_probe')
    root = tmp_path / 'receiver'
    root.mkdir(mode=0o700)
    cfg = {'relay_target': '192.0.2.43', 'controller_nonce': NONCE}
    read_fd, write_fd = os.pipe2(os.O_NONBLOCK)
    selector = selectors.DefaultSelector()
    selector.register(read_fd, selectors.EVENT_READ, 'audio')
    spawned = []
    def spawn(name, args):
        # The native worker must never compete with the probe for PCM bytes.
        with pytest.raises(KeyError):
            selector.get_key(read_fd)
        spawned.append((name, args))
    try:
        for _ in range(64):
            assert not receiver.start_output_when_authorized(
                cfg, root, time.monotonic(), selector, read_fd, spawn)
            block = b'\x7f' * 4096
            assert os.write(write_fd, block) == len(block)
            event, _ = selector.select(.2)[0]
            assert event.data == 'audio'
            assert os.read(event.fd, 8192) == block
        assert not spawned
        path = root / 'controller.json'
        path.write_bytes(control(observed_monotonic=time.monotonic()))
        path.chmod(0o600)
        assert receiver.start_output_when_authorized(
            cfg, root, time.monotonic(), selector, read_fd, spawn)
        assert len(spawned) == 1 and spawned[0][0] == 'radio_output'
        # No buffered old input remains, and no second reader was registered.
        with pytest.raises(BlockingIOError):
            os.read(read_fd, 8192)
        assert not selector.get_map()
    finally:
        selector.close()
        os.close(read_fd)
        os.close(write_fd)


@pytest.mark.parametrize('failure', ['stale', 'released', 'wrong_nonce', 'spawn'])
def test_output_reader_transfer_fails_closed(tmp_path, monkeypatch, failure):
    for name in ('control_lease', 'native_group', 'lifecycle'):
        monkeypatch.setitem(sys.modules, 'airplay_core.' + name,
            importlib.import_module('basswiesn.app.services.airplay_bridge.' + name))
    monkeypatch.setitem(sys.modules, 'metadata_wire',
        importlib.import_module('basswiesn.app.services.airplay_bridge.metadata_wire'))
    monkeypatch.setitem(sys.modules, 'network_contract', contract)
    receiver = module('receiver_probe')
    root = tmp_path / 'receiver'
    root.mkdir(mode=0o700)
    cfg = {'relay_target': '192.0.2.43', 'controller_nonce': NONCE}
    read_fd, write_fd = os.pipe2(os.O_NONBLOCK)
    selector = selectors.DefaultSelector()
    selector.register(read_fd, selectors.EVENT_READ, 'audio')
    change = {'observed_monotonic': time.monotonic()}
    if failure == 'stale':
        change['observed_monotonic'] = 1.
    elif failure == 'released':
        change['allow_audio'] = True
    elif failure == 'wrong_nonce':
        change['nonce'] = 'f' * 32
    path = root / 'controller.json'
    path.write_bytes(control(**change))
    path.chmod(0o600)
    calls = []
    def spawn(*args):
        calls.append(args)
        raise OSError('simulated worker launch failure')
    try:
        with pytest.raises((OSError, ValueError)):
            receiver.start_output_when_authorized(
                cfg, root, time.monotonic(), selector, read_fd, spawn)
        assert len(calls) == (1 if failure == 'spawn' else 0)
        assert bool(selector.get_map()) == (failure != 'spawn')
    finally:
        selector.close()
        os.close(read_fd)
        os.close(write_fd)


def test_receiver_ipc_fixed_operations_and_large_unicode_metadata(tmp_path, monkeypatch):
    for name in ('control_lease', 'native_group', 'private_ipc'):
        monkeypatch.setitem(sys.modules, 'airplay_core.'+name,
            importlib.import_module('basswiesn.app.services.airplay_bridge.'+name))
    monkeypatch.setitem(sys.modules, 'network_contract', contract)
    ipc = module('receiver_ipc')
    tmp_path.chmod(0o700)
    monkeypatch.setattr(ipc, 'ROOT', tmp_path)
    cfgfile = tmp_path/'config.json'
    cfgfile.write_text(json.dumps({**config(), 'relay_target': '192.0.2.43',
        'relay_token': 'a'*64, 'controller_nonce': NONCE}))
    monkeypatch.setattr(ipc, 'Path', lambda _: cfgfile)
    def save(name, data):
        file = tmp_path/name
        file.write_text(json.dumps(data))
        file.chmod(0o600)
    save('native-group.json', {'example': True})
    save('owner-observation.json', {'fields': {key:'界'*256 for key in ('track','artist','album','genre')}})
    assert (tmp_path/'owner-observation.json').stat().st_size > 2048
    assert ipc.process({'operation': 'observe'})['observation']['fields']['track'] == '界'*256
    assert ipc.process({'operation': 'ack'}) == {'pending': True}
    assert ipc.process({'operation': 'control', 'record':json.loads(control(observed_monotonic=time.monotonic()))})
    assert (tmp_path/'controller.json').stat().st_mode & 0o777 == 0o600
    for request in ({'operation':'exec','command':'reboot'}, {'operation':'observe','path':'/etc/passwd'},
                    {'operation':'control','record':{}}, {'operation':'control','record':json.loads(control())}):
        with pytest.raises(Exception):
            ipc.process(request)


class DockerBridge:
    def __init__(self):
        self.bridge = module('namespace_ipc').NamespaceBridge(CID, IMAGE, os.getuid())
        self.inspected = dict(Id=CID, Image=IMAGE, State={'Running':True, 'Restarting':False},
            RestartCount=0, Config={'User':'avahi'}, Mounts=[], HostConfig={
                'ReadonlyRootfs':True, 'Privileged':False, 'CapAdd':[], 'CapDrop':['ALL'],
                'NetworkMode':'none', 'PidMode':'', 'IpcMode':'private',
                'SecurityOpt':['no-new-privileges:true']})
        self.calls = []
        async def command(*args, data=None):
            self.calls.append((args, data))
            if args[0] == 'inspect':
                return json.dumps([self.inspected]).encode()
            return b'{"pending":true}\n'
        self.bridge.command = command


def test_host_bridge_cannot_execute_arbitrary_container_command():
    async def run():
        mock = DockerBridge()
        assert await mock.bridge.exchange(b'{"operation":"ack"}') == b'{"pending":true}\n'
        assert mock.calls[-1][0] == ('exec','-i','--user','avahi',CID,'python3',
                                    '/opt/basswiesn-ap2/receiver_ipc.py')
        count = len(mock.calls)
        with pytest.raises(ValueError):
            await mock.bridge.exchange(b'{"operation":"exec","command":"reboot"}')
        assert len(mock.calls) == count
        with pytest.raises(ValueError):
            await mock.bridge.exchange(b'{"operation":"ack"}')
    asyncio.run(run())


@pytest.mark.parametrize('change', ['replaced','image','root','privileged','writable','socket','caps',
                                    'restarted','host_network','host_pid','host_ipc'])
def test_host_bridge_rechecks_pinned_instance_and_hardening(change):
    async def run():
        mock = DockerBridge()
        data = mock.inspected
        if change == 'replaced': data['Id'] = 'a'*64
        if change == 'image': data['Image'] = 'sha256:' + 'a'*64
        if change == 'root': data['Config']['User'] = 'root'
        if change == 'privileged': data['HostConfig']['Privileged'] = True
        if change == 'writable': data['HostConfig']['ReadonlyRootfs'] = False
        if change == 'socket': data['Mounts'] = [{'Destination':'/var/run/docker.sock'}]
        if change == 'caps': data['HostConfig']['CapAdd'] = ['NET_ADMIN']
        if change == 'restarted': data['RestartCount'] = 1
        if change == 'host_network': data['HostConfig']['NetworkMode'] = 'host'
        if change == 'host_pid': data['HostConfig']['PidMode'] = 'host'
        if change == 'host_ipc': data['HostConfig']['IpcMode'] = 'host'
        with pytest.raises(ValueError):
            await mock.bridge.exchange(b'{"operation":"ack"}')
        assert [args[0] for args, _ in mock.calls] == ['inspect']
    asyncio.run(run())


def test_actual_private_unix_socket_and_peer_uid(tmp_path):
    tmp_path.chmod(0o700)
    path = tmp_path/'receiver.sock'
    async def run():
        mock = DockerBridge()
        server = await asyncio.start_unix_server(mock.bridge.connection, str(path), limit=4097)
        path.chmod(0o600)
        try:
            port = UnixReceiverIPC(path, peer_uid=os.getuid())
            assert json.loads(await port.exchange(b'{"operation":"ack"}')) == {'pending':True}
            with pytest.raises(ValueError, match='PEER_MISMATCH'):
                await UnixReceiverIPC(path, peer_uid=os.getuid()+1).exchange(b'{"operation":"ack"}')
            path.chmod(0o666)
            with pytest.raises(ValueError, match='PRIVATE_SOCKET'):
                await port.exchange(b'{"operation":"ack"}')
        finally:
            server.close()
            await server.wait_closed()
    asyncio.run(run())


def test_host_socket_ownership_and_cleanup_use_pinned_inodes(tmp_path):
    tmp_path.chmod(0o700)
    ipc = module('namespace_ipc')
    path = tmp_path/'receiver.sock'
    async def run():
        mock = DockerBridge()
        async with ipc.private_server(path, os.getuid(), mock.bridge.connection):
            port = UnixReceiverIPC(path, peer_uid=os.getuid())
            assert json.loads(await port.exchange(b'{"operation":"ack"}')) == {'pending':True}
            # A caller's replacement entry must survive helper cleanup.
            path.rename(tmp_path/'original.sock')
            path.write_text('application-owned replacement')
        assert path.read_text() == 'application-owned replacement'
        assert path.stat().st_mode & 0o777 != 0  # No cleanup chmod/chown by path.
    asyncio.run(run())


def test_host_socket_refuses_existing_symlink_without_touching_target(tmp_path):
    tmp_path.chmod(0o700)
    target = tmp_path/'untouched'
    target.write_text('keep')
    target.chmod(0o640)
    path = tmp_path/'receiver.sock'
    path.symlink_to(target)
    async def run():
        with pytest.raises(OSError):
            async with module('namespace_ipc').private_server(path, os.getuid(), DockerBridge().bridge.connection):
                pytest.fail('existing socket path accepted')
    asyncio.run(run())
    assert target.read_text() == 'keep' and target.stat().st_mode & 0o777 == 0o640


def test_receiver_binding_requires_pinned_build_and_exact_namespace_nonce():
    class IPC:
        async def exchange(self, _):
            return json.dumps(dict(version=1, contract='basswiesn-group-v1', binary_sha256='a'*64,
                nonce=NONCE, pid=23, started=99.)).encode()
    async def run():
        port = ReceiverPort(IPC())
        with pytest.raises(ValueError):
            await port.bind('AP2-MASTER', binary_sha256='b'*64)
        assert (await port.bind('AP2-MASTER', binary_sha256='a'*64)).nonce == NONCE
        with pytest.raises(ValueError):
            await port.bind('AP2-MASTER', binary_sha256='a'*64)
    asyncio.run(run())
