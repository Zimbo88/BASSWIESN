"""Time-bounded AP2 pilot. NEVER sends radio control requests.

Only sanitized counters and current display text are retained in tmpfs. No raw
receiver output, RTSP headers, pairing data, PCM recording or remote API.
Default mode counts/discards PCM. Explicit relay configuration serves a single
target-bound HTTP MP3 stream; a separate owner must back up/verify/restore it.
"""
import collections
import json
import os
from pathlib import Path
import selectors
import signal
import subprocess
import time

from metadata_wire import MetadataWireDecoder
from network_contract import validate_config, firewall_rules

ROOT = Path('/run/ap2')
stopping = False


def stop(signum, frame):
    global stopping
    stopping = True


def run(args, **kwargs):
    return subprocess.run(args, check=True, stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL, **kwargs)


def write_json(path, value):
    tmp = path.with_suffix('.tmp')
    tmp.write_text(json.dumps(value))
    tmp.replace(path)


def main():
    os.umask(0o077)
    signal.signal(signal.SIGTERM, stop)
    signal.signal(signal.SIGINT, stop)
    cfg = validate_config(json.loads(Path('/config/receiver.json').read_text()))
    ROOT.mkdir(mode=0o700, exist_ok=True)
    state = {'stage': 'WAIT_INTERFACE', 'radio_writes': 0, 'audio_bytes': 0,
             'nonzero_chunks': 0, 'metadata_events': {}, 'display': {}, 'children': {}}
    processes = []
    selector = selectors.DefaultSelector()
    handles = []
    deadline = time.monotonic() + cfg['lifetime_seconds']

    def publish():
        write_json(ROOT / 'status.json', {**state, 'updated_epoch': time.time()})

    def spawn(name, args):
        process = subprocess.Popen(args, stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)
        processes.append((name, process))
        state['children'][name] = process.pid
        return process

    try:
        publish()
        while not stopping and not (ROOT / 'provisioned').exists():
            if time.monotonic() > deadline:
                raise TimeoutError('interface unavailable')
            time.sleep(.2)
        if stopping:
            return
        # No network traffic is possible until the isolated interface is UP.
        # IPv6 is disabled by the container sysctl before interface creation.
        for key in ('all', 'default', cfg['interface']):
            if Path(f'/proc/sys/net/ipv6/conf/{key}/disable_ipv6').read_text().strip() != '1':
                raise RuntimeError('IPv6 isolation missing')
        run(['nft', '-f', '-'], input=firewall_rules(cfg).encode())
        for address in cfg['deny_ips']:
            run(['ip', '-4', 'route', 'add', 'prohibit', address + '/32'])
        run(['ip', 'link', 'set', cfg['interface'], 'up'])
        state['stage'] = 'DHCP'
        publish()
        dhcp = spawn('dhcp', ['udhcpc', '-f', '-n', '-B', '-R', '-i', cfg['interface'],
            '-s', '/opt/basswiesn-ap2/dhcp_hook.py', '-t', '3', '-T', '3', '-x',
            'hostname:basswiesn-ap2-probe'])
        lease = {}
        lease_deadline = time.monotonic() + 25
        while not stopping and time.monotonic() < lease_deadline:
            if (ROOT / 'lease.json').exists():
                lease = json.loads((ROOT / 'lease.json').read_text())
                if lease.get('state') == 'BOUND':
                    break
                if lease.get('state') in ('REJECTED', 'ADDRESS_CONFLICT'):
                    raise RuntimeError('lease rejected')
            if dhcp.poll() is not None:
                raise RuntimeError('DHCP exited')
            time.sleep(.2)
        if lease.get('state') != 'BOUND':
            raise RuntimeError('no DHCP lease')
        state['address'] = lease['address']
        state['stage'] = 'STARTING_SERVICES'
        publish()
        # Never use upstream's entrypoint: it includes unbounded waits and
        # starts services before our network/protected-target gate.
        Path('/run/dbus').mkdir(exist_ok=True)
        # Avahi drops to its own UID and must reach the credential-checked
        # local D-Bus socket. Metadata/config files remain private separately.
        Path('/run/dbus').chmod(0o755)
        run(['dbus-uuidgen', '--ensure'])
        dbus = spawn('dbus', ['dbus-daemon', '--system', '--nofork'])
        bus_deadline = time.monotonic() + 5
        while not Path('/run/dbus/system_bus_socket').exists():
            if stopping or time.monotonic() > bus_deadline or dbus.poll() is not None:
                raise RuntimeError('DBus startup failed')
            time.sleep(.1)
        avahi_cfg = ROOT / 'avahi.conf'
        avahi_cfg.write_text('[server]\nuse-ipv4=yes\nuse-ipv6=no\nallow-interfaces=' +
            cfg['interface'] + '\n[publish]\npublish-workstation=no\npublish-hinfo=no\n')
        spawn('avahi', ['avahi-daemon', '--no-chroot', '-f', str(avahi_cfg)])
        spawn('nqptp', ['nqptp'])
        for name in ('audio', 'metadata'):
            path = ROOT / (name + '.fifo')
            os.mkfifo(path, 0o600)
            fd = os.open(path, os.O_RDWR | os.O_NONBLOCK)
            handles.append(fd)
            if name != 'audio' or not cfg.get('relay_target'):
                selector.register(fd, selectors.EVENT_READ, name)
        configuration = '''general = {
 name = %s;
 interface = %s;
 output_backend = "pipe";
 ignore_volume_control = %s;
};
pipe = {
 name = "/run/ap2/audio.fifo";
 output_rate = 48000;
 output_format = "S32_LE";
 output_channels = 2;
};
metadata = {
 enabled = "yes";
 include_cover_art = "no";
 cover_art_cache_directory = "";
 pipe_name = "/run/ap2/metadata.fifo";
};
diagnostics = { log_verbosity = 0; statistics = "no"; };
''' % (json.dumps(cfg['name'], ensure_ascii=False), json.dumps(cfg['interface']),
       json.dumps('yes' if cfg.get('radio_volume_forwarding') else 'no'))
        (ROOT / 'shairport.conf').write_text(configuration)
        # Check both daemons, wait boundedly for the local Avahi pidfile.
        start_deadline = time.monotonic() + 8
        while not Path('/run/avahi-daemon/pid').exists():
            if stopping or time.monotonic() > start_deadline or dbus.poll() is not None:
                raise RuntimeError('Avahi startup failed')
            time.sleep(.1)
        spawn('shairport', ['shairport-sync', '-c', str(ROOT / 'shairport.conf')])
        if cfg.get('relay_target'):
            spawn('radio_output', ['python3', '/opt/basswiesn-ap2/radio_output.py'])
        decoder = MetadataWireDecoder()
        events = collections.Counter()
        pending = None
        state['stage'] = 'RECEIVER_RUNNING_NOT_APPLE_VERIFIED'
        last_publish = 0
        while not stopping and time.monotonic() < deadline:
            if any(p.poll() is not None for _, p in processes):
                raise RuntimeError('receiver child exited')
            current = json.loads((ROOT / 'lease.json').read_text())
            if (current.get('state') != 'BOUND' or current.get('address') != lease['address']
                    or current.get('expires_monotonic', 0) <= time.monotonic()):
                raise RuntimeError('lease lost or changed')
            for key, _ in selector.select(.1):
                chunk = os.read(key.fd, 8192)
                if key.data == 'audio':
                    state['audio_bytes'] += len(chunk)
                    state['nonzero_chunks'] += int(any(chunk))
                else:
                    for kind, code, value in decoder.feed(chunk):
                        events[kind + '/' + code] += 1
                        if code == 'pvol':
                            state['volume'] = {'sequence': events['ssnc/pvol'],
                                               'airplay_db': float(value.decode('ascii'))}
                        elif code == 'mdst':
                            pending = {}
                        elif kind == 'core' and pending is not None:
                            pending[{'minm': 'title', 'asar': 'artist', 'asal': 'album', 'asgn': 'genre'}[code]] = value.decode()
                        elif code == 'mden' and pending is not None:
                            state['display'] = pending
                            pending = None
                        elif code in ('aend', 'abeg', 'pend'):
                            state['display'] = {}
                            if code in ('aend', 'pend'):
                                state.pop('volume', None)
                            pending = None
                            if cfg.get('relay_target') and code in ('pend', 'aend'):
                                (ROOT / 'output-end').touch(mode=0o600)
            if time.monotonic() - last_publish > 1:
                if cfg.get('relay_target') and (ROOT / 'output-status.json').exists():
                    output = json.loads((ROOT / 'output-status.json').read_text())
                    state['output'] = output
                    state['audio_bytes'] = output['audio_bytes']
                    state['nonzero_chunks'] = output['nonzero_chunks']
                    if output['failure']:
                        raise RuntimeError('radio relay failed')
                state['metadata_events'] = dict(events)
                state['metadata_discarded'] = decoder.discarded
                publish()
                last_publish = time.monotonic()
        state['stage'] = 'STOPPED_OR_EXPIRED'
    except Exception as exc:
        # Do not emit exception messages: DHCP/environment/subprocess text may
        # contain local identifiers. Detailed diagnostics remain structured.
        state['failure_stage'] = state['stage']
        state['stage'] = 'FAILED'
        state['error_class'] = type(exc).__name__
    finally:
        for name, process in reversed(processes):
            if process.poll() is None:
                process.terminate()
                try:
                    process.wait(timeout=3)
                except subprocess.TimeoutExpired:
                    process.kill()
                    process.wait(timeout=3)
            state['children'][name] = {'pid': process.pid, 'exit_code': process.returncode}
        selector.close()
        for fd in handles:
            os.close(fd)
        state['display'] = {}
        publish()
        # Allow read-only extraction of final counters for at most 30 seconds.
        if not stopping:
            time.sleep(3)


if __name__ == '__main__':
    main()
