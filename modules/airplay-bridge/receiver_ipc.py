"""Fixed private stdin/stdout operations for an authorized namespace owner.

No listener, Docker socket, radio requests, arbitrary paths or shell commands.
Run as the receiver UID in its mount/PID namespace; never log these payloads.
"""
import json
from pathlib import Path
import sys
import time

from airplay_core.control_lease import ControlLease
from airplay_core.native_group import read_native_group
from airplay_core.private_ipc import publish_control
from network_contract import validate_config

ROOT = Path('/run/ap2')


def process(request):
    if type(request) is not dict or request.get('operation') not in {'observe', 'control', 'ack', 'bind'}:
        raise ValueError('invalid operation')
    operation = request['operation']
    if set(request) != ({'operation', 'record'} if operation == 'control' else {'operation'}):
        raise ValueError('invalid fields')
    if operation == 'control':
        cfg = validate_config(json.loads(Path('/config/receiver.json').read_text()))
        raw = json.dumps(request['record'], allow_nan=False).encode()
        # Full monotonic/replay validation belongs to the long-lived worker.
        # This stateless ingress still checks schema/nonce/current age.
        ControlLease(cfg['controller_nonce']).accept(raw, now=time.monotonic())
        publish_control(ROOT, raw)
        return {'published': True}
    if operation == 'ack':
        try:
            return json.loads(read_native_group(ROOT / 'controller-ack.json'))
        except FileNotFoundError:
            return {'pending': True}
    if operation == 'bind':
        return json.loads(read_native_group(ROOT / 'owner-binding.json'))
    return {'native': json.loads(read_native_group(ROOT / 'native-group.json')),
            'observation': json.loads(read_native_group(ROOT / 'owner-observation.json', maximum=8192))}


def main():
    def pairs(items):
        value = {}
        for key, item in items:
            if key in value:
                raise ValueError('duplicate')
            value[key] = item
        return value
    try:
        raw = sys.stdin.buffer.read(4097)
        if len(raw) > 4096:
            raise ValueError('excessive')
        result = process(json.loads(raw, object_pairs_hook=pairs))
        print(json.dumps(result, allow_nan=False))
    except Exception:
        # Never echo a nonce, stream token, exception payload or pairing data.
        print('{"error":"AIRPLAY_PRIVATE_IPC_REJECTED"}')
        raise SystemExit(1) from None


if __name__ == '__main__':
    main()
