"""Real C observer plus strict private IPC, offline with no radio/network."""
from hashlib import sha256
import json
import os
from pathlib import Path
import subprocess
import time

import pytest

from basswiesn.app.services.airplay_bridge.native_group import (
    GroupObservationRejected, NativeGroupCursor, read_native_group,
)

pytestmark = pytest.mark.unit
ROOT = Path(__file__).resolve().parents[1]
NONCE = 'a' * 32
UUID = '12345678-1234-4321-abcd-123456789abc'


@pytest.fixture(scope='module')
def native(tmp_path_factory):
    build = tmp_path_factory.mktemp('ap2-native-build')
    binary = build / 'observer-test'
    # Fail, not skip, when the development C/OpenSSL toolchain is absent.
    subprocess.run(['cc', '-std=c11', '-D_GNU_SOURCE', '-Wall', '-Wextra', '-Werror',
                    '-O2', '-pthread', '-I', str(ROOT/'modules/airplay-bridge/native'),
                    str(ROOT/'tests/fixtures/ap2_group_observer.c'), '-lcrypto', '-o', str(binary)],
                   check=True, capture_output=True, timeout=30)
    return binary


def run_native(native, tmp_path, *actions, nonce=NONCE):
    result = subprocess.run([str(native), str(tmp_path), *actions],
        env={**os.environ, 'BASSWIESN_AP2_OBSERVER_NONCE': nonce},
        check=True, capture_output=True, text=True, timeout=5)
    assert not result.stderr
    return [json.loads(line) for line in result.stdout.splitlines()]


def test_native_acquire_group_leave_and_old_cleanup(native, tmp_path):
    rows = run_native(native, tmp_path, 'acquire:0', 'acquire:7', f'group:7:{UUID}',
                      'acquire:8', f'group:8:{UUID.upper()}', 'clear:7', 'clear:8')
    assert [r['state'] for r in rows] == ['IDLE','UNASSIGNED','GROUPED','UNASSIGNED',
                                       'GROUPED','GROUPED','IDLE']
    assert rows[2]['group_hash'] == rows[4]['group_hash'] == sha256(UUID.encode()).hexdigest()
    assert rows[4] == rows[5]  # late cleanup cannot erase the new connection
    assert rows[-1]['connection'] == 0 and rows[-1]['group_hash'] == ''
    assert UUID not in json.dumps(rows)
    cursor = NativeGroupCursor(nonce=NONCE, pid=rows[0]['pid'], started=rows[0]['event_monotonic'])
    for row in rows:
        assert cursor.accept(json.dumps(row).encode(), now=time.monotonic(), process_alive=True).state == row['state']
    assert (tmp_path/'native-group.json').stat().st_mode & 0o777 == 0o600


@pytest.mark.parametrize('value', ['', 'no-uuid', UUID + 'x', UUID.replace('-', '_'),
                                  UUID.replace('a', 'z'), 'secret=should-not-escape',
                                  '00000000-0000-0000-0000-000000000000'])
def test_native_invalid_group_is_unassigned_not_copied(native, tmp_path, value):
    rows = run_native(native, tmp_path, 'acquire:1', f'group:1:{value}')
    assert rows[-1]['state'] == 'UNASSIGNED' and rows[-1]['group_hash'] == ''
    assert 'secret' not in json.dumps(rows)


@pytest.mark.parametrize('nonce', ['', 'A' * 32, 'a' * 31, 'a' * 33, 'g' * 32])
def test_native_disabled_for_invalid_owner_nonce(native, tmp_path, nonce):
    assert run_native(native, tmp_path, 'acquire:1', f'group:1:{UUID}', nonce=nonce) == [None, None]


def test_native_refuses_shared_directory(native, tmp_path):
    tmp_path.chmod(0o755)
    assert run_native(native, tmp_path, 'acquire:1') == [None]


def test_native_old_group_update_cannot_replace_current_owner(native, tmp_path):
    rows = run_native(native, tmp_path, 'acquire:1', f'group:1:{UUID}', 'acquire:2', f'group:1:{UUID}')
    assert rows[2] == rows[3] and rows[3]['state'] == 'UNASSIGNED'


def observation(**changes):
    return {'version':1, 'nonce':NONCE, 'pid':42, 'revision':1, 'connection':1,
            'state':'GROUPED', 'group_hash':'b' * 64, 'event_monotonic':10.0, **changes}


def accept(cursor, value, *, now=11, alive=True):
    return cursor.accept(json.dumps(value).encode(), now=now, process_alive=alive)


def test_unchanged_event_not_confused_with_expired_heartbeat():
    cursor = NativeGroupCursor(nonce=NONCE, pid=42, started=9)
    first = accept(cursor, observation())
    assert accept(cursor, observation(), now=999) == first
    with pytest.raises(GroupObservationRejected):
        accept(cursor, observation(), now=1000, alive=False)
    with pytest.raises(GroupObservationRejected):
        accept(cursor, observation(), now=1001)  # process resurrection not accepted


@pytest.mark.parametrize('changes', [
    {'version':True}, {'version':1.0}, {'nonce':'c'*32}, {'pid':True}, {'pid':43},
    {'revision':True}, {'revision':0}, {'revision':2**53}, {'connection':-1},
    {'connection':True}, {'connection':2**31}, {'connection':0},
    {'state':'ADVERTISED'}, {'state':'IDLE'}, {'state':'UNASSIGNED'},
    {'group_hash':'c'*63}, {'group_hash':'C'*64}, {'group_hash':''},
    {'event_monotonic':float('nan')}, {'event_monotonic':float('inf')},
    {'event_monotonic':True}, {'event_monotonic':8.0}, {'event_monotonic':12.0},
    {'sender_secret':'discard'},
])
def test_invalid_native_record_fails_closed_without_echo(changes):
    cursor = NativeGroupCursor(nonce=NONCE, pid=42, started=9)
    with pytest.raises(GroupObservationRejected, match='^AIRPLAY_GROUP_OBSERVATION_UNVERIFIED$'):
        accept(cursor, observation(**changes))


def test_duplicate_json_keys_are_rejected():
    cursor = NativeGroupCursor(nonce=NONCE, pid=42, started=9)
    raw = json.dumps(observation()).replace('"version": 1', '"version": 1, "version": 1')
    with pytest.raises(GroupObservationRejected):
        cursor.accept(raw.encode(), now=11, process_alive=True)


def test_retired_connection_and_altered_duplicate_fail_closed():
    cursor = NativeGroupCursor(nonce=NONCE, pid=42, started=9)
    accept(cursor, observation())
    accept(cursor, observation(revision=2, connection=2))
    with pytest.raises(GroupObservationRejected):
        accept(cursor, observation(revision=3, connection=1))
    other = NativeGroupCursor(nonce=NONCE, pid=42, started=9)
    accept(other, observation())
    with pytest.raises(GroupObservationRejected):
        accept(other, observation(group_hash='c'*64))


def test_private_file_loader_rejects_symlinks_permissions_and_large_files(tmp_path):
    path = tmp_path/'native-group.json'
    path.write_bytes(b'{}')
    path.chmod(0o600)
    assert read_native_group(path) == b'{}'
    link = tmp_path/'link'
    link.symlink_to(path)
    with pytest.raises(OSError):
        read_native_group(link)
    path.chmod(0o644)
    with pytest.raises(GroupObservationRejected):
        read_native_group(path)
    path.chmod(0o600)
    path.write_bytes(b' ' * 2049)
    with pytest.raises(GroupObservationRejected):
        read_native_group(path)
