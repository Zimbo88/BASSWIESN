"""Run INSIDE the explicit receiver test image with Docker --network none.

Exercises real patched SETUP/TEARDOWN dispatch on loopback with synthetic plists.
Not pairing, audio, Apple compatibility or radio hardware acceptance. No secrets.
"""
from hashlib import sha256
import json
import os
from pathlib import Path
import plistlib
import secrets
import socket
import subprocess
import sys
import time

sys.path.insert(0, '/opt/basswiesn-ap2')
from airplay_core.native_group import NativeGroupCursor, read_native_group  # noqa: E402


def main():
    assert os.getuid() != 0
    assert set(os.listdir('/sys/class/net')) == {'lo'}, 'network-none test only'
    os.umask(0o077)
    root = Path('/run/ap2')
    root.mkdir(mode=0o700)
    Path('/run/dbus').mkdir()
    Path('/run/avahi-daemon').mkdir()
    (root/'bus.conf').write_text('''<busconfig><type>system</type>
<listen>unix:path=/run/dbus/system_bus_socket</listen><policy context="default">
<allow own="*"/><allow send_destination="*"/><allow receive_sender="*"/>
</policy></busconfig>''')
    (root/'avahi.conf').write_text('[server]\nuse-ipv4=yes\nuse-ipv6=no\nallow-interfaces=lo\n'
                                 '[publish]\npublish-workstation=no\npublish-hinfo=no\n')
    (root/'shairport.conf').write_text('general={name="AP2 Offline Test"; interface="lo"; '
        'output_backend="dummy"; port=7000;}; diagnostics={log_verbosity=2; log_output_to="stderr";};'
        'metadata={enabled="yes"; include_cover_art="no"; pipe_name="/run/ap2/metadata.fifo";};')
    binary = Path('/opt/basswiesn-ap2/shairport-sync-observed')
    manifest = json.loads(Path('/opt/basswiesn-ap2/observer-build.json').read_text())
    assert sha256(binary.read_bytes()).hexdigest() == manifest['binary_sha256']
    nonce = secrets.token_hex(16)
    children, connections = [], []
    try:
        for args in (
            ['dbus-daemon', '--nofork', '--config-file='+str(root/'bus.conf')],
            ['avahi-daemon', '--no-chroot', '--no-drop-root', '--no-rlimits', '-f', str(root/'avahi.conf')],
            ['nqptp'],
        ):
            children.append(subprocess.Popen(args, stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL))
            time.sleep(.4)
        started = time.monotonic()
        receiver_log = (root/'offline-receiver.log').open('wb')
        receiver = subprocess.Popen([str(binary), '-v', '-c', str(root/'shairport.conf')],
            env={**os.environ, 'LD_LIBRARY_PATH':'/usr/lib', 'BASSWIESN_AP2_OBSERVER_NONCE':nonce},
            stdout=receiver_log, stderr=receiver_log)
        receiver_log.close()
        children.append(receiver)
        cursor = NativeGroupCursor(nonce=nonce, pid=receiver.pid, started=started)

        def wait_state(expected):
            # Upstream waits ten seconds for a non-loopback MAC in network-none.
            # Only this offline fixture needs that startup allowance.
            deadline = time.monotonic() + (20 if cursor.previous is None else 8)
            while time.monotonic() < deadline:
                if not all(child.poll() is None for child in children):
                    print((root/'offline-receiver.log').read_text(errors='replace')[-2500:], file=sys.stderr)
                    raise AssertionError('receiver stack exited')
                try:
                    record = cursor.accept(read_native_group(root/'native-group.json'),
                        now=time.monotonic(), process_alive=receiver.poll() is None)
                    if record.state == expected:
                        return record
                except FileNotFoundError:
                    pass
                time.sleep(.05)
            # Only synthetic, network-none diagnostics; never use this capture
            # in the real Apple/radio pilot, where receiver output is suppressed.
            print((root/'offline-receiver.log').read_text(errors='replace')[-2000:], file=sys.stderr)
            print({'files':sorted(p.name for p in root.iterdir()), 'mode':oct(root.stat().st_mode),
                   'uid':root.stat().st_uid, 'receiver_pid':receiver.pid}, file=sys.stderr)
            raise AssertionError('native state not observed: ' + expected)

        def request(connection, method, seq, body):
            message = (f'{method} rtsp://127.0.0.1/session RTSP/1.0\r\nCSeq: {seq}\r\n'
                       'Content-Type: application/x-apple-binary-plist\r\n'
                       f'Content-Length: {len(body)}\r\n\r\n').encode() + body
            connection.sendall(message)
            response = b''
            while b'\r\n\r\n' not in response:
                chunk = connection.recv(1)
                assert chunk, 'closed before response'
                response += chunk
                assert len(response) <= 8192
            head = response.decode('ascii')
            fields = {line.split(':', 1)[0].lower():line.split(':', 1)[1].strip()
                      for line in head.split('\r\n')[1:] if ':' in line}
            remaining = int(fields.get('content-length', '0'))
            assert 0 <= remaining <= 65536
            while remaining:
                chunk = connection.recv(min(4096, remaining))
                assert chunk
                remaining -= len(chunk)
            assert head.split('\r\n')[0] == 'RTSP/1.0 200 OK', head.split('\r\n')[0]

        assert wait_state('IDLE').connection == 0
        # Synthetic local protocol fixture, not an authenticated Apple session.
        uuid = '12345678-1234-4321-abcd-123456789abc'
        connection = socket.create_connection(('127.0.0.1', 7000), timeout=5)
        connections.append(connection)
        body = plistlib.dumps({'timingProtocol':'PTP', 'groupUUID':uuid,
            'groupContainsGroupLeader':True, 'timingPeerInfo':{'Addresses':['127.0.0.1']}},
            fmt=plistlib.FMT_BINARY)
        request(connection, 'SETUP', 1, body)
        grouped = wait_state('GROUPED')
        assert grouped.group_hash == sha256(uuid.encode()).hexdigest()
        # A second connection takes ownership while the old one still exists.
        # Its predecessor's cancellation/cleanup must not clear the new group.
        successor = socket.create_connection(('127.0.0.1', 7000), timeout=5)
        connections.append(successor)
        request(successor, 'SETUP', 1, body)
        switched = wait_state('GROUPED')
        assert switched.connection != grouped.connection
        assert switched.group_hash == grouped.group_hash
        connection.close()
        connections.remove(connection)
        connection = successor
        request(connection, 'TEARDOWN', 2, plistlib.dumps({}, fmt=plistlib.FMT_BINARY))
        ended = wait_state('IDLE')
        assert ended.connection == 0 and ended.group_hash == ''
        assert ended.revision > grouped.revision
        connection.close()
        connections.remove(connection)
        # New connections need fresh native identities; loss of TCP must clear
        # membership without receiving a friendly TEARDOWN.
        connection = socket.create_connection(('127.0.0.1', 7000), timeout=5)
        connections.append(connection)
        request(connection, 'SETUP', 1, body)
        replacement = wait_state('GROUPED')
        assert replacement.connection != grouped.connection
        connection.close()
        connections.remove(connection)
        assert wait_state('IDLE').group_hash == ''
        # No group UUID means no group claim, despite upstream's advertised gid.
        connection = socket.create_connection(('127.0.0.1', 7000), timeout=5)
        connections.append(connection)
        request(connection, 'SETUP', 1, plistlib.dumps({'timingProtocol':'PTP',
            'timingPeerInfo':{'Addresses':['127.0.0.1']}}, fmt=plistlib.FMT_BINARY))
        assert wait_state('UNASSIGNED').group_hash == ''
        request(connection, 'TEARDOWN', 2, plistlib.dumps({}, fmt=plistlib.FMT_BINARY))
        wait_state('IDLE')
        print(json.dumps({'result':'PASS', 'network':'none', 'uid':os.getuid(),
            'source_commit':manifest['source_commit'], 'binary_sha256':manifest['binary_sha256'],
            'native_setup_group_teardown':'PASS', 'connection_loss':'PASS',
            'new_connection':'PASS', 'interrupted_connection_cleanup':'PASS',
            'missing_group_not_fallback':'PASS',
            'apple_validation':'NOT_RUN', 'radio_contacts':0}))
    finally:
        for connection in connections:
            connection.close()
        for child in reversed(children):
            if child.poll() is None:
                child.terminate()
                try:
                    child.wait(timeout=3)
                except subprocess.TimeoutExpired:
                    child.kill()
                    child.wait(timeout=3)


if __name__ == '__main__':
    main()
