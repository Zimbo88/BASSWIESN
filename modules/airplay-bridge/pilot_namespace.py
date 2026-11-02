"""Explicit root owner for one disposable, paired AP2 pilot receiver.

Never installed/launched automatically. Only --config is accepted. The driver
owns application/radio transactions; this process only owns its exact receiver,
macvlan, DHCP child and private IPC bridge. No production container is touched.
The code directory must be on an executable filesystem: udhcpc executes its
fixed hook directly. Runtime config and receipts may live on a noexec /run.
"""
import argparse
import fcntl
from hashlib import sha256
import ipaddress
import json
import math
import os
from pathlib import Path
import re
import signal
import stat
import subprocess
import sys
import time

from network_contract import firewall_rules, validate_config
from pilot_dhcp import read_root_json, root_path, save_json
from receiver_identity import virtual_mac

IMAGE_ID = 'sha256:a447eef2c8f0479e9adab08d0b7f8b6138c58db019ac0a64444c72cb3f7edd2b'
BINARY_SHA256 = '30eeb2232d6a2d2afe737f2fccd720a8b0652afbbd51aa7a9fa16aa0d42039fe'
LABEL = 'basswiesn.private.paired-ap2-owner'
IDENTITY_LABEL = 'basswiesn.ap2.virtual-identity'
MAC_LABEL = 'basswiesn.ap2.virtual-mac'
IDENTITY_LOCKS = Path('/run/basswiesn-ap2-identities')
HERE = Path(__file__).resolve().parent
IP, DOCKER, NSENTER = '/usr/sbin/ip', '/usr/bin/docker', '/usr/bin/nsenter'
IPC_PREFLIGHT = '''import hashlib,json,sys,time
from pathlib import Path
sys.path.insert(0, '/opt/basswiesn-ap2')
from airplay_core.native_group import NativeGroupCursor,read_native_group
root=Path('/run/ap2')
binding=json.loads(read_native_group(root/'owner-binding.json'))
observation=json.loads(read_native_group(root/'owner-observation.json', maximum=8192))
now=time.monotonic()
native=NativeGroupCursor(nonce=binding['nonce'],pid=binding['pid'],started=binding['started']).accept(
    read_native_group(root/'native-group.json'),now=now,process_alive=observation['process_alive'])
assert binding['contract']=='basswiesn-group-v1'
assert binding['binary_sha256']==sys.argv[1]
assert hashlib.sha256(Path('/opt/basswiesn-ap2/shairport-sync-observed').read_bytes()).hexdigest()==sys.argv[1]
assert native.state=='IDLE' and observation['version']==1
assert type(observation['heartbeat_sequence']) is int and observation['heartbeat_sequence']>0
assert 0 <= now-observation['observed_monotonic'] <= 3
print('ready')
'''


class OwnerRejected(ValueError):
    """A fixed, non-sensitive diagnostic code, never a subprocess payload."""
    def __init__(self, code):
        self.code = code
        super().__init__(code)


def failure_details(stage, exc):
    if isinstance(exc, OwnerRejected):
        code = exc.code
    elif isinstance(exc, subprocess.TimeoutExpired):
        code = 'OWNER_COMMAND_TIMEOUT'
    elif isinstance(exc, subprocess.CalledProcessError):
        code = 'OWNER_COMMAND_FAILED'
    elif isinstance(exc, OSError):
        code = 'OWNER_FILESYSTEM_REJECTED'
    else:
        code = 'OWNER_PREFLIGHT_OR_LIFETIME_REJECTED'
    return {'failure_stage': stage, 'error_code': code, 'error_class': type(exc).__name__}


def validate_owner_config(value):
    fields = {'trial_id', 'slot', 'parent_interface', 'image_id', 'run_dir',
        'application_uid', 'ipc_socket', 'lifetime_seconds', 'radio_ips', 'receiver'}
    if type(value) is not dict or set(value) not in (fields, fields | {'receiver_key'}):
        raise ValueError('explicit complete owner configuration required')
    if 'receiver_key' in value and (not isinstance(value['receiver_key'], str)
            or not re.fullmatch('[a-f0-9]{32}', value['receiver_key'])):
        raise ValueError('explicit locally generated receiver identity required')
    if (not isinstance(value['trial_id'], str)
            or not re.fullmatch('[a-z0-9][a-z0-9-]{0,39}', value['trial_id'])
            or type(value['slot']) is not int or value['slot'] not in (1, 2)
            or value['parent_interface'] != 'eth0' or value['image_id'] != IMAGE_ID
            or type(value['application_uid']) is not int or value['application_uid'] != 10001
            or type(value['lifetime_seconds']) is not int
            or not 60 <= value['lifetime_seconds'] <= 900):
        raise ValueError('invalid fixed pilot scope')
    for key in ('run_dir', 'ipc_socket'):
        if (not isinstance(value[key], str) or not Path(value[key]).is_absolute()
                or '..' in Path(value[key]).parts or Path(value[key]) == Path('/')
                or ',' in value[key] or any(not c.isprintable() for c in value[key])):
            raise ValueError('invalid absolute pilot path')
    # namespace_ipc binds through a pinned /proc/self/fd path. The application's
    # mount may have a much shorter path than this host-side directory.
    if len(Path(value['ipc_socket']).name.encode()) > 64:
        raise ValueError('IPC socket basename too long')
    if type(value['radio_ips']) is not list or not value['radio_ips']:
        raise ValueError('complete radio inventory required')
    radios = {str(ipaddress.IPv4Address(address)) for address in value['radio_ips']}
    if len(radios) != len(value['radio_ips']):
        raise ValueError('duplicate radio inventory')
    if type(value['receiver']) is not dict:
        raise ValueError('explicit receiver configuration required')
    allowed = {'interface', 'name', 'network', 'dhcp_server', 'host_ip', 'deny_ips',
        'lifetime_seconds', 'max_pause_seconds', 'external_network', 'native_group_observer',
        'relay_target', 'relay_token', 'controller_nonce', 'radio_volume_forwarding'}
    if set(value['receiver']) - allowed:
        raise ValueError('unknown receiver option')
    cfg = validate_config(value['receiver'])
    if (cfg['interface'] != 'ap2' or cfg.get('external_network') is not True
            or cfg.get('native_group_observer') is not True
            or cfg['lifetime_seconds'] != value['lifetime_seconds']):
        raise ValueError('external native observer required')
    target = cfg.get('relay_target')
    if value['slot'] == 1:
        if target not in radios or not cfg.get('controller_nonce') or not cfg.get('relay_token'):
            raise ValueError('master needs explicit controlled radio relay')
    elif any(key in cfg for key in ('relay_target', 'relay_token', 'controller_nonce')):
        raise ValueError('second receiver is input-only')
    if radios - ({target} if target else set()) - set(cfg['deny_ips']):
        raise ValueError('all non-master radios must be prohibited')
    if any(ipaddress.IPv4Address(address).is_multicast
           or ipaddress.IPv4Address(address).is_unspecified for address in radios):
        raise ValueError('unicast radio inventory required')
    return {**value, 'radio_ips': sorted(radios), 'receiver': cfg}


def identity(cfg):
    owner = f"{cfg['trial_id']}-slot{cfg['slot']}"
    digest = sha256(owner.encode()).hexdigest()
    if 'receiver_key' in cfg:
        mac = virtual_mac(cfg['receiver_key'])
    else:
        mac = '02:ba:' + ':'.join(digest[i:i+2] for i in range(0, 8, 2))
    return owner, 'bwpa' + digest[:9], mac


def identity_labels(cfg):
    if 'receiver_key' not in cfg:
        return []
    # Neither the real radio MAC nor its stable ID is cloned into Bonjour/DHCP.
    # The key is installation-generated and retained across receiver restarts.
    key = sha256(cfg['receiver_key'].encode()).hexdigest()
    return ['--label', f'{IDENTITY_LABEL}={key}', '--label', f'{MAC_LABEL}={identity(cfg)[2]}']


class IdentityReservation:
    """Host-wide lock before provisioning; orphan containers also block reuse.

    The lock is on the derived virtual MAC, not only the installation key, so
    even a truncated-hash collision cannot create two simultaneous owners.
    The inode remains on disk after unlock: unlinking it would split a flock.
    """
    def __init__(self, cfg, *, root=IDENTITY_LOCKS):
        self.fd = None
        self.cfg, self.root = cfg, root

    def acquire(self, call):
        if self.fd is not None:
            raise OwnerRejected('RECEIVER_IDENTITY_ALREADY_RESERVED')
        self.root.mkdir(mode=0o700, exist_ok=True)
        root_path(self.root, directory=True, private=True)
        mac = identity(self.cfg)[2]
        fd = os.open(self.root / (mac.replace(':', '') + '.lock'),
                     os.O_RDWR | os.O_CREAT | os.O_NOFOLLOW | os.O_NONBLOCK, 0o600)
        try:
            info = os.fstat(fd)
            if (not stat.S_ISREG(info.st_mode) or info.st_uid != 0
                    or stat.S_IMODE(info.st_mode) != 0o600 or info.st_nlink != 1):
                raise OwnerRejected('RECEIVER_IDENTITY_LOCK_UNSAFE')
            fcntl.flock(fd, fcntl.LOCK_EX | fcntl.LOCK_NB)
            # Covers a previous owner dying before exact container cleanup,
            # including stopped containers whose identity must be reconciled.
            found = call([DOCKER, 'ps', '-aq', '--filter', f'label={MAC_LABEL}={mac}'])
            if found.stdout.strip():
                raise OwnerRejected('RECEIVER_IDENTITY_ORPHANED')
            # A crash between macvlan creation and namespace attachment can
            # leave a link on the host even if no container survived. Never
            # silently reuse its address or remove an unadopted link.
            links = json.loads(call([IP, '-j', 'link', 'show']).stdout)
            if not isinstance(links, list) or any(not isinstance(row, dict) for row in links):
                raise OwnerRejected('RECEIVER_IDENTITY_LINK_CHECK_FAILED')
            if any(str(row.get('address', '')).lower() == mac for row in links):
                raise OwnerRejected('RECEIVER_IDENTITY_ORPHANED')
            self.fd = fd
        except BaseException:
            os.close(fd)
            raise

    def close(self):
        if self.fd is not None:
            os.close(self.fd)
            self.fd = None


def docker_arguments(cfg, name, config_dir, cidfile):
    return [DOCKER, 'run', '-d', '--name', name, '--cidfile', str(cidfile),
        '--network', 'none', '--read-only', '--user', 'avahi', '--cap-drop', 'ALL',
        '--security-opt', 'no-new-privileges:true', '--init', '--log-driver', 'none',
        '--sysctl', 'net.ipv6.conf.all.disable_ipv6=1',
        '--sysctl', 'net.ipv6.conf.default.disable_ipv6=1',
        '--sysctl', 'net.ipv4.ip_unprivileged_port_start=0',
        '--pids-limit', '128', '--memory', '192m', '--cpus', '1',
        '--tmpfs', '/run:rw,nosuid,nodev,noexec,mode=1777,size=16m', '--shm-size', '16m',
        '--mount', f'type=bind,src={config_dir},dst=/config,readonly',
        '--label', f'{LABEL}={identity(cfg)[0]}', *identity_labels(cfg), cfg['image_id']]


def dhcp_guard(cfg):
    """DHCP firewall in addition to the receiver's exact attested radio guard."""
    router = cfg['receiver']['dhcp_server']
    return f'''table ip ap2_dhcp_guard {{
 chain input {{ type filter hook input priority -10; policy accept;
  ip saddr {router} udp sport 67 udp dport 68 accept
  udp sport 67 counter drop
  udp dport 68 counter drop
 }}
 chain output {{ type filter hook output priority -10; policy accept;
  ip daddr {{ 255.255.255.255, {router} }} udp sport 68 udp dport 67 accept
  udp sport 68 counter drop
  udp dport 67 counter drop
 }}
}}
'''


def native_ready(status):
    if status.get('stage') != 'RECEIVER_RUNNING_NOT_APPLE_VERIFIED':
        return False
    if status.get('native_group') is None:
        return False  # The first native heartbeat follows receiver startup.
    native = status['native_group']
    if (status.get('native_group_contract') != 'basswiesn-group-v1'
            or status.get('native_group_binary_sha256') != BINARY_SHA256
            or type(native) is not dict
            or set(native) != {'revision', 'connection', 'state', 'group_hash', 'event_monotonic'}
            or native.get('state') != 'IDLE' or native.get('connection') != 0
            or native.get('group_hash') != '' or type(native.get('revision')) is not int
            or native['revision'] < 1 or type(native.get('event_monotonic')) not in (int, float)
            or not math.isfinite(native['event_monotonic']) or native['event_monotonic'] < 0):
        raise ValueError('native receiver preflight failed')
    ipaddress.IPv4Address(status['address'])
    return True


class NamespaceOwner:
    def __init__(self, cfg):
        self.cfg = cfg
        self.run_dir = Path(cfg['run_dir'])
        self.owner, self.link, self.mac = identity(cfg)
        self.name = 'bw-ap2-' + self.owner
        self.cid = None
        self.child = self.bridge = None
        self.directory_created = False
        self.ipc_directory = self.ipc_inode = None
        self.link_created = False
        self.identity_reservation = None
        self.stop = False
        self.stage = 'VALIDATED'
        self.deadline = time.monotonic() + cfg['lifetime_seconds']
        self.result = {'trial_id': cfg['trial_id'], 'slot': cfg['slot'],
            'image_id': cfg['image_id'], 'ready': False, 'receiver_created': False,
            'radio_control_requests': 0}

    def call(self, args, *, check=True, **kwargs):
        return subprocess.run(args, check=check, capture_output=True, timeout=20, **kwargs)

    def inspect(self, target):
        return json.loads(self.call([DOCKER, 'inspect', target]).stdout)[0]

    def routes(self):
        return {version: json.loads(self.call([IP, '-j', version, 'route', 'show', 'table', 'all']).stdout)
            for version in ('-4', '-6')}

    def stopped(self):
        return self.stop or time.monotonic() >= self.deadline or (self.run_dir / 'STOP').exists()

    def setup(self):
        self.stage = 'CREATE_PRIVATE_RUN_DIRECTORY'
        root_path(self.run_dir.parent, directory=True, private=True)
        # A trial directory is never reused; receipts/config from earlier trials survive.
        self.run_dir.mkdir(mode=0o700)
        self.directory_created = True
        self.stage = 'CHECK_APPLICATION_IPC_DIRECTORY'
        endpoint = Path(self.cfg['ipc_socket'])
        directory = os.open(endpoint.parent, os.O_RDONLY | os.O_DIRECTORY | os.O_NOFOLLOW)
        try:
            info = os.fstat(directory)
            if info.st_uid != 10001 or stat.S_IMODE(info.st_mode) != 0o700:
                raise ValueError('application-owned mode0700 IPC parent required')
            if os.path.lexists(endpoint):
                raise ValueError('IPC endpoint already exists')
        except BaseException:
            os.close(directory)
            raise
        self.ipc_directory = directory
        self.stage = 'CAPTURE_HOST_ROUTES'
        self.before = self.routes()
        save_json(self.run_dir / 'START.json', {'routes': self.before, **self.result})
        if 'receiver_key' in self.cfg:
            self.stage = 'RESERVE_STABLE_RECEIVER_IDENTITY'
            self.identity_reservation = IdentityReservation(self.cfg)
            self.identity_reservation.acquire(self.call)
        self.stage = 'CHECK_DHCP_HOOK_EXECUTABLE'
        # Permission bits alone do not suffice on noexec staging filesystems.
        if not os.access(HERE / 'pilot_dhcp.py', os.X_OK):
            raise OwnerRejected('DHCP_HOOK_NOT_EXECUTABLE')
        self.stage = 'CHECK_UNIQUE_CONTAINER_AND_LINK'
        if self.call([DOCKER, 'inspect', self.name], check=False).returncode == 0:
            raise ValueError('container name already exists')
        if self.call([IP, 'link', 'show', self.link], check=False).returncode == 0:
            raise ValueError('interface name already exists')
        parent = json.loads(self.call([IP, '-j', 'link', 'show', 'eth0']).stdout)[0]
        if parent.get('operstate') != 'UP':
            raise ValueError('fixed parent interface unavailable')
        self.stage = 'CHECK_PINNED_RECEIVER_IMAGE'
        image = self.inspect(self.cfg['image_id'])
        if image['Id'] != self.cfg['image_id'] or image['Config']['User'] != 'avahi':
            raise ValueError('pinned non-root receiver image required')
        self.stage = 'WRITE_RECEIVER_CONFIGURATION'
        config_dir = self.run_dir / 'config'
        config_dir.mkdir(mode=0o755)
        config_dir.chmod(0o755)
        (config_dir / 'network').mkdir(mode=0o755)
        (config_dir / 'network').chmod(0o755)
        save_json(config_dir / 'receiver.json', self.cfg['receiver'], mode=0o644)
        self.stage = 'START_PINNED_RECEIVER'
        self.call(docker_arguments(self.cfg, self.name, config_dir, self.run_dir / 'container.cid'))
        self.capture_cid()
        info = self.owned_container()
        pid = info['State']['Pid']
        if type(pid) is not int or pid <= 1 or info['State']['Running'] is not True:
            raise ValueError('owned receiver did not start')
        ns = [NSENTER, '-t', str(pid), '-n']
        netns = os.readlink(f'/proc/{pid}/ns/net')
        if netns == os.readlink('/proc/1/ns/net'):
            raise ValueError('private receiver namespace required')
        self.stage = 'CREATE_PRIVATE_MACVLAN'
        self.call([IP, 'link', 'add', 'link', 'eth0', 'name', self.link,
            'address', self.mac, 'type', 'macvlan', 'mode', 'bridge'])
        self.link_created = True
        self.call([IP, 'link', 'set', self.link, 'netns', str(pid)])
        self.call([*ns, IP, 'link', 'set', self.link, 'name', 'ap2'])
        self.call([*ns, '/usr/sbin/sysctl', '-w', 'net.ipv6.conf.ap2.disable_ipv6=1'])
        self.stage = 'INSTALL_NAMESPACE_NETWORK_GUARDS'
        self.call([*ns, '/usr/sbin/nft', '-f', '-'],
            input=(firewall_rules(self.cfg['receiver']) + dhcp_guard(self.cfg)).encode())
        for address in self.cfg['receiver']['deny_ips']:
            self.call([*ns, IP, '-4', 'route', 'add', 'prohibit', address + '/32'])
        # No packet-capable link until both guards and all prohibited routes exist.
        if self.stopped():
            raise TimeoutError('owner stopped before namespace activation')
        self.stage = 'ACTIVATE_PRIVATE_INTERFACE'
        self.call([*ns, IP, 'link', 'set', 'ap2', 'up'])
        self.stage = 'START_PRIVATE_DHCP'
        self.child = subprocess.Popen([*ns, '/usr/bin/busybox', 'udhcpc', '-f', '-n', '-B', '-R',
            '-i', 'ap2', '-s', str(HERE / 'pilot_dhcp.py'), '-t', '3', '-T', '3', '-x',
            f"hostname:basswiesn-ap2-slot{self.cfg['slot']}"],
            env={'PATH': '/usr/sbin:/usr/bin:/sbin:/bin',
                'BASSWIESN_AP2_CONFIG': str(config_dir / 'receiver.json'),
                'BASSWIESN_AP2_NETNS': netns}, stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)

    def capture_cid(self):
        path = self.run_dir / 'container.cid'
        if path.exists():
            root_path(path)
            value = path.read_text().strip()
            if not re.fullmatch('[a-f0-9]{64}', value):
                raise ValueError('exact receiver container ID required')
            self.cid = value
            self.result['container_id'] = value
            self.result['receiver_created'] = True
        if not self.cid:
            raise ValueError('missing owned receiver container ID')

    def owned_container(self):
        info = self.inspect(self.cid)
        if (info['Id'] != self.cid or info['Image'] != self.cfg['image_id']
                or info['Config'].get('Labels', {}).get(LABEL) != self.owner):
            raise ValueError('receiver ownership changed')
        return info

    def wait(self):
        self.stage = 'WAIT_NATIVE_RECEIVER'
        while not self.stopped():
            if self.child.poll() is not None:
                raise RuntimeError('DHCP owner exited')
            if self.bridge is not None and self.bridge.poll() is not None:
                raise RuntimeError('private IPC bridge exited')
            sample = self.call([DOCKER, 'exec', '--user', 'avahi', self.cid,
                'cat', '/run/ap2/status.json'], check=False)
            if sample.returncode:
                time.sleep(.2)
                continue
            if len(sample.stdout) > 32768:
                raise ValueError('excessive receiver status')
            status = json.loads(sample.stdout)
            save_json(self.run_dir / 'STATUS.json', status)
            if status.get('stage') in ('FAILED', 'STOPPED_OR_EXPIRED'):
                raise RuntimeError('receiver failed')
            if not self.result['ready'] and native_ready(status):
                if self.bridge is None:
                    self.stage = 'PREFLIGHT_FIXED_NATIVE_FILES'
                    # Do not invoke the fail-closed IPC bridge while any first
                    # binding/native/observation file is still unpublished.
                    preflight = self.call([DOCKER, 'exec', '--user', 'avahi', self.cid,
                        'python3', '-c', IPC_PREFLIGHT, BINARY_SHA256], check=False)
                    if preflight.returncode or preflight.stdout.strip() != b'ready':
                        time.sleep(.2)
                        continue
                    remaining = max(1, int(self.deadline - time.monotonic()))
                    self.stage = 'START_PRIVATE_IPC_BRIDGE'
                    self.bridge = subprocess.Popen(['/usr/bin/python3', str(HERE / 'namespace_ipc.py'),
                        '--container-id', self.cid, '--image-id', self.cfg['image_id'],
                        '--application-uid', '10001', '--socket', self.cfg['ipc_socket'],
                        '--seconds', str(remaining)], stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL,
                        env={'PATH': '/usr/sbin:/usr/bin:/sbin:/bin'})
                endpoint = Path(self.cfg['ipc_socket'])
                if endpoint.exists():
                    info = os.stat(endpoint.name, dir_fd=self.ipc_directory, follow_symlinks=False)
                    if not stat.S_ISSOCK(info.st_mode) or info.st_uid != 10001 or info.st_mode & 0o077:
                        raise ValueError('invalid private IPC socket')
                    self.ipc_inode = info.st_ino
                    self.result.update(ready=True, address=status['address'])
                    self.stage = 'RECEIVER_READY'
                    ready = {**self.result, 'ipc_socket': str(endpoint),
                        'receiver_name': self.cfg['receiver']['name']}
                    save_json(self.run_dir / 'READY.json', ready)
                    print(json.dumps(ready), flush=True)
            time.sleep(.2)
        self.result['stop_reason'] = 'requested' if self.stop or (self.run_dir / 'STOP').exists() else 'deadline'

    def cleanup(self):
        errors = []
        for child in (self.bridge, self.child):
            try:
                if child is not None and child.poll() is None:
                    # asyncio.run unwinds private_server on SIGINT; SIGTERM has
                    # no handler in the fixed bridge and would leave its socket.
                    child.send_signal(signal.SIGINT if child is self.bridge else signal.SIGTERM)
                    try:
                        child.wait(timeout=8)
                    except subprocess.TimeoutExpired:
                        child.kill()
                        child.wait(timeout=3)
            except Exception as exc:
                errors.append('child:' + type(exc).__name__)
        if self.ipc_directory is not None:
            try:
                endpoint = Path(self.cfg['ipc_socket'])
                if self.ipc_inode is not None:
                    info = os.stat(endpoint.name, dir_fd=self.ipc_directory, follow_symlinks=False)
                    if info.st_ino == self.ipc_inode:
                        os.unlink(endpoint.name, dir_fd=self.ipc_directory)
            except FileNotFoundError:
                pass
            except Exception as exc:
                errors.append('socket:' + type(exc).__name__)
            finally:
                os.close(self.ipc_directory)
                self.ipc_directory = None
        try:
            if self.directory_created and self.cid is None and (self.run_dir / 'container.cid').exists():
                self.capture_cid()
            if self.cid:
                self.owned_container()
                self.call([DOCKER, 'stop', '-t', '12', self.cid], check=False)
                self.call([DOCKER, 'rm', self.cid])
                self.result['container_removed'] = self.call([DOCKER, 'inspect', self.cid], check=False).returncode != 0
        except Exception as exc:
            errors.append('container:' + type(exc).__name__)
        try:
            if self.link_created and self.call([IP, 'link', 'show', self.link], check=False).returncode == 0:
                row = json.loads(self.call([IP, '-j', 'link', 'show', self.link]).stdout)[0]
                if row['address'] != self.mac:
                    raise ValueError('link ownership changed')
                self.call([IP, 'link', 'delete', self.link])
        except Exception as exc:
            errors.append('link:' + type(exc).__name__)
        if hasattr(self, 'before'):
            try:
                self.result['host_routes_unchanged'] = self.routes() == self.before
                if not self.result['host_routes_unchanged']:
                    errors.append('host_routes_changed')
            except Exception as exc:
                errors.append('host_routes:' + type(exc).__name__)
        self.result['cleanup_errors'] = errors
        if self.identity_reservation is not None:
            # A surviving container retains the MAC label and blocks the next
            # owner; releasing a process lock is not a cleanup-success claim.
            self.identity_reservation.close()
        self.result['cleanup_status'] = ('MANUAL_REQUIRED' if errors else
            'OWNED_RESOURCES_REMOVED' if self.cid or self.link_created else 'NO_RECEIVER_CREATED')
        if self.directory_created:
            save_json(self.run_dir / 'RESULT.json', self.result)
        return not errors


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--config', type=Path, required=True)
    args = parser.parse_args()
    if os.geteuid() != 0:
        raise ValueError('explicit root host owner required')
    os.umask(0o077)
    cfg = validate_owner_config(read_root_json(args.config, private=True))
    owner = NamespaceOwner(cfg)
    def stop(*_):
        owner.stop = True
    for signum in (signal.SIGINT, signal.SIGTERM, signal.SIGHUP):
        signal.signal(signum, stop)
    success = False
    try:
        owner.setup()
        owner.wait()
        success = owner.result['ready']
    except Exception as exc:
        owner.result.update(failure_details(owner.stage, exc))
    finally:
        clean = owner.cleanup()
        print(json.dumps({'finished': True, **owner.result}), flush=True)
    return 0 if success and clean else 1


if __name__ == '__main__':
    sys.exit(main())
