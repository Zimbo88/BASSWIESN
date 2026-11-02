"""Time-bounded AP2 pilot. NEVER sends radio control requests.

Only sanitized counters and current display text are retained in tmpfs. No raw
receiver output, RTSP headers, pairing data, PCM recording or remote API.
Default mode counts/discards PCM. Explicit relay configuration serves a single
target-bound HTTP MP3 stream; a separate owner must back up/verify/restore it.
"""
import collections
from dataclasses import asdict
from hashlib import sha256
import json
import os
from pathlib import Path
import selectors
import secrets
import signal
import subprocess
import time

from metadata_wire import MetadataWireDecoder
from airplay_core.lifecycle import PilotPlayback, ends_stream
from airplay_core.native_group import NativeGroupCursor, read_native_group
from airplay_core.control_lease import ControlLease
from basswiesn.app.services.airplay_bridge.metadata import BridgeMetadata
from network_contract import validate_config, firewall_rules, validate_external_lease, relay_volume_configuration

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


def output_start_authorized(cfg, root, now):
    if not cfg.get('relay_target'):
        return False
    if not cfg.get('controller_nonce'):
        return True
    try:
        raw = read_native_group(root / 'controller.json')
    except FileNotFoundError:
        return False
    record = ControlLease(cfg['controller_nonce']).accept(raw, now=now)
    if record.allow_audio:
        raise ValueError('controlled output must start held')
    return True


def start_output_when_authorized(cfg, root, now, selector, audio_fd, spawn):
    """Transfer the FIFO's sole reader only after the held control grant.

    While waiting for the application owner, the probe must drain/discard PCM.
    Leaving the pipe unread blocks the native player's pipe backend and can
    stall a sender before source preparation even starts. The worker still
    starts held and must receive its independent readback-based audio release.
    A failed spawn is fatal; it must not silently resume a previous grant.
    """
    if not output_start_authorized(cfg, root, now):
        return False
    selector.unregister(audio_fd)
    spawn('radio_output', ['python3', '/opt/basswiesn-ap2/radio_output.py'])
    return True


def main():
    os.umask(0o077)
    signal.signal(signal.SIGTERM, stop)
    signal.signal(signal.SIGINT, stop)
    cfg = validate_config(json.loads(Path('/config/receiver.json').read_text()))
    ROOT.mkdir(mode=0o700, exist_ok=True)
    state = {'stage': 'WAIT_INTERFACE', 'radio_writes': 0, 'audio_bytes': 0,
             'nonzero_chunks': 0, 'metadata_events': {}, 'display': {}, 'children': {}}
    processes = []
    receiver_session = secrets.token_hex(16)
    display_sequence = 0
    display = BridgeMetadata(cfg['name'])
    display_generation = display.begin()
    selector = selectors.DefaultSelector()
    handles = []
    deadline = time.monotonic() + cfg['lifetime_seconds']

    def publish():
        write_json(ROOT / 'status.json', {**state, 'updated_epoch': time.time()})

    def spawn(name, args, **options):
        process = subprocess.Popen(args, stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL, **options)
        processes.append((name, process))
        state['children'][name] = process.pid
        return process

    try:
        publish()
        external = cfg.get('external_network', False)
        marker = Path('/config/network/lease.json') if external else ROOT / 'provisioned'
        if external:
            if os.geteuid() == 0:
                raise RuntimeError('external-network receiver must be non-root')
            capabilities = {line.split(':')[0]: line.split(':')[1].strip()
                            for line in Path('/proc/self/status').read_text().splitlines() if ':' in line}
            if any(int(capabilities[key], 16) for key in ('CapEff', 'CapPrm', 'CapBnd')):
                raise RuntimeError('receiver capabilities must be dropped')
            if not os.statvfs('/config').f_flag & os.ST_RDONLY:
                raise RuntimeError('owner configuration must be mounted read-only')
        while not stopping:
            if marker.exists():
                if not external:
                    break
                # udhcpc emits deconfig before obtaining a lease. That marker
                # is not readiness and must not race daemon startup.
                owner_state = json.loads(marker.read_text()).get('state')
                if owner_state == 'BOUND':
                    break
                if owner_state in ('REJECTED', 'ADDRESS_CONFLICT'):
                    raise RuntimeError('owner rejected network lease')
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
        lease = {}
        if external:
            lease = validate_external_lease(cfg, json.loads(marker.read_text()), time.monotonic())
            for address in cfg['deny_ips']:
                # Route lookup sends no packets. Non-root cannot remove these
                # prohibit routes; all mutable networking belongs to the owner.
                check = subprocess.run(['ip', '-4', 'route', 'get', address],
                                       stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)
                if check.returncode == 0:
                    raise RuntimeError('protected route remains usable')
            write_json(ROOT / 'lease.json', lease)
        else:
            run(['nft', '-f', '-'], input=firewall_rules(cfg).encode())
            for address in cfg['deny_ips']:
                run(['ip', '-4', 'route', 'add', 'prohibit', address + '/32'])
            run(['ip', 'link', 'set', cfg['interface'], 'up'])
            state['stage'] = 'DHCP'
            publish()
            dhcp = spawn('dhcp', ['udhcpc', '-f', '-n', '-B', '-R', '-i', cfg['interface'],
                '-s', '/opt/basswiesn-ap2/dhcp_hook.py', '-t', '3', '-T', '3', '-x',
                'hostname:basswiesn-ap2-probe'])
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
        if external:
            # This bus is confined to this receiver's private mount and PID
            # namespaces. No host socket or system service activation is used.
            bus_cfg = ROOT / 'bus.conf'
            bus_cfg.write_text('<busconfig><type>system</type>'
                '<listen>unix:path=/run/dbus/system_bus_socket</listen>'
                '<policy context="default"><allow own="*"/>'
                '<allow send_destination="*"/><allow receive_sender="*"/>'
                '</policy></busconfig>')
            Path('/run/avahi-daemon').mkdir(exist_ok=True)
            dbus = spawn('dbus', ['dbus-daemon', '--nofork', '--config-file=' + str(bus_cfg)])
        else:
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
        avahi_args = ['avahi-daemon', '--no-chroot', '-f', str(avahi_cfg)]
        if external:
            # Already unprivileged as the image's Avahi UID; do not attempt a
            # second privilege transition or resource-limit increase.
            avahi_args.extend(['--no-drop-root', '--no-rlimits'])
        spawn('avahi', avahi_args)
        spawn('nqptp', ['nqptp'])
        audio_fd = None
        for name in ('audio', 'metadata'):
            path = ROOT / (name + '.fifo')
            os.mkfifo(path, 0o600)
            fd = os.open(path, os.O_RDWR | os.O_NONBLOCK)
            handles.append(fd)
            selector.register(fd, selectors.EVENT_READ, name)
            if name == 'audio':
                audio_fd = fd
        configuration = '''general = {
 name = %s;
 interface = %s;
 output_backend = "pipe";
 ignore_volume_control = %s;
%s
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
       json.dumps('yes' if cfg.get('radio_volume_forwarding') else 'no'),
       relay_volume_configuration(cfg))
        (ROOT / 'shairport.conf').write_text(configuration)
        # Check both daemons, wait boundedly for the local Avahi pidfile.
        start_deadline = time.monotonic() + 8
        while not Path('/run/avahi-daemon/pid').exists():
            if stopping or time.monotonic() > start_deadline or dbus.poll() is not None:
                raise RuntimeError('Avahi startup failed')
            time.sleep(.1)
        native_group = None
        native_started = time.monotonic()
        if cfg.get('native_group_observer', False):
            binary = Path('/opt/basswiesn-ap2/shairport-sync-observed')
            manifest = json.loads(Path('/opt/basswiesn-ap2/observer-build.json').read_text())
            if (manifest.get('version') != 1 or manifest.get('contract') != 'basswiesn-group-v1'
                    or manifest.get('source_commit') != '7bad231c18368dbd26f298577f6210e36e4b0797'
                    or sha256(binary.read_bytes()).hexdigest() != manifest.get('binary_sha256')):
                raise RuntimeError('native observer build mismatch')
            receiver = spawn('shairport', [str(binary), '-c', str(ROOT / 'shairport.conf')],
                env={**os.environ, 'LD_LIBRARY_PATH': '/usr/lib',
                     'BASSWIESN_AP2_OBSERVER_NONCE': receiver_session})
            native_group = NativeGroupCursor(nonce=receiver_session, pid=receiver.pid, started=native_started)
            state['native_group_contract'] = 'basswiesn-group-v1'
            state['native_group_binary_sha256'] = manifest['binary_sha256']
            # Kept private, unlike the display/status projection. A separately
            # authorized host owner uses this once to bind the original child.
            write_json(ROOT / 'owner-binding.json', dict(version=1, nonce=receiver_session,
                pid=receiver.pid, started=native_started,
                contract=manifest['contract'], binary_sha256=manifest['binary_sha256']))
        else:
            receiver = spawn('shairport', ['shairport-sync', '-c', str(ROOT / 'shairport.conf')])
        playback = PilotPlayback()
        write_json(ROOT / 'output-playback.json', playback.snapshot())
        heartbeat_sequence = 0
        write_json(ROOT / 'output-heartbeat.json', {
            'sequence': heartbeat_sequence, 'observed_monotonic': time.monotonic()})
        output_started = start_output_when_authorized(
            cfg, ROOT, time.monotonic(), selector, audio_fd, spawn)
        decoder = MetadataWireDecoder(on_discard=display.abort_pending)
        events = collections.Counter()
        event_sequence = 0
        state['stage'] = 'RECEIVER_RUNNING_NOT_APPLE_VERIFIED'
        last_publish = 0
        while not stopping and time.monotonic() < deadline:
            if any(p.poll() is not None for _, p in processes):
                raise RuntimeError('receiver child exited')
            current = (validate_external_lease(cfg, json.loads(marker.read_text()), time.monotonic())
                       if external else json.loads((ROOT / 'lease.json').read_text()))
            if (current.get('state') != 'BOUND' or current.get('address') != lease['address']
                    or current.get('expires_monotonic', 0) <= time.monotonic()):
                raise RuntimeError('lease lost or changed')
            # The five-second control lease starts at worker launch, not while
            # the owner is still provisioning its second namespace. No output
            # socket exists until the first fresh held command arrives.
            if not output_started:
                output_started = start_output_when_authorized(
                    cfg, ROOT, time.monotonic(), selector, audio_fd, spawn)
            for key, _ in selector.select(.1):
                chunk = os.read(key.fd, 8192)
                if key.data == 'audio':
                    state['audio_bytes'] += len(chunk)
                    state['nonzero_chunks'] += int(any(chunk))
                else:
                    for kind, code, value in decoder.iter_events(chunk):
                        event_sequence += 1
                        events[kind + '/' + code] += 1
                        if playback.apply(kind, code):
                            write_json(ROOT / 'output-playback.json', playback.snapshot())
                        if code == 'pvol' and playback.state != 'ENDED':
                            state['volume'] = {'sequence': events['ssnc/pvol'],
                                               'airplay_db': float(value.decode('ascii'))}
                        if ends_stream(kind, code):
                            # Activity timeout during pause must preserve the
                            # stream, slider and track for a later resume.
                            state['display'] = {}
                            state.pop('volume', None)
                            display.end(display_generation)
                            if cfg.get('relay_target'):
                                (ROOT / 'output-end').touch(mode=0o600)
                        elif display.apply(display_generation, event_sequence, kind, code, value):
                            state['display'] = {('title' if key == 'track' else key): text
                                                for key, text in display.text_fields().items()}
            if time.monotonic() - last_publish > 1:
                if native_group is not None:
                    try:
                        observed = native_group.accept(read_native_group(ROOT / 'native-group.json'),
                            now=time.monotonic(), process_alive=receiver.poll() is None)
                        state['native_group'] = asdict(observed)
                    except FileNotFoundError:
                        if native_group.previous is not None or time.monotonic() - native_started > 5:
                            raise RuntimeError('native group observation lost') from None
                heartbeat_sequence += 1
                write_json(ROOT / 'output-heartbeat.json', {
                    'sequence': heartbeat_sequence, 'observed_monotonic': time.monotonic()})
                if cfg.get('relay_target') and (ROOT / 'output-status.json').exists():
                    output = json.loads((ROOT / 'output-status.json').read_text())
                    state['output'] = output
                    state['audio_bytes'] = output['audio_bytes']
                    state['nonzero_chunks'] = output['nonzero_chunks']
                    if output['failure']:
                        raise RuntimeError('radio relay failed')
                state['metadata_events'] = dict(events)
                state['playback'] = playback.snapshot()
                state['metadata_discarded'] = decoder.discarded
                # Private, source-bound projection consumed by a local owner;
                # it is not an Internet API or an Apple group identifier.
                display_sequence += 1
                state['display_observation'] = display.observation(
                    receiver_session, display_sequence, now=time.monotonic())
                write_json(ROOT / 'owner-observation.json', dict(version=1,
                    heartbeat_sequence=heartbeat_sequence, observed_monotonic=time.monotonic(),
                    process_alive=receiver.poll() is None,
                    playback=state['playback'], display=state['display_observation'],
                    volume=state.get('volume'),
                    volume_mode='radio' if cfg.get('radio_volume_forwarding') else 'software'))
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
        # A final diagnostic snapshot cannot retain apparently live membership.
        state.pop('native_group', None)
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
        display.end(display_generation)
        display_sequence += 1
        state['display_observation'] = display.observation(
            receiver_session, display_sequence, now=time.monotonic())
        publish()
        # Allow read-only extraction of final counters for at most 30 seconds.
        if not stopping:
            time.sleep(3)


if __name__ == '__main__':
    main()
