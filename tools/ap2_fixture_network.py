"""Reject anything except the disposable, route-isolated sender laboratory."""
import ipaddress
import fcntl
import os
from pathlib import Path
import socket
import struct

NETWORK = ipaddress.ip_network('198.18.42.0/29')
RECEIVERS = ('198.18.42.2', '198.18.42.4')
SENDER = '198.18.42.3'


def validate_bridge(interfaces, addresses, routes, uid):
    if uid == 0 or set(interfaces) != {'lo', 'eth0'}:
        raise RuntimeError('UNPRIVILEGED_ISOLATED_BRIDGE_REQUIRED')
    if len(addresses) != 1 or addresses[0] not in (*RECEIVERS, SENDER):
        raise RuntimeError('FIXTURE_ADDRESS_REQUIRED')
    for row in routes:
        destination = row.get('dst', 'default')
        if destination == 'default' or not ipaddress.ip_network(destination).subnet_of(NETWORK):
            raise RuntimeError('EXTERNAL_ROUTE_PROHIBITED')
        if row.get('dev') != 'eth0' or 'gateway' in row:
            raise RuntimeError('FIXTURE_ROUTE_REQUIRED')
    if not routes:
        raise RuntimeError('FIXTURE_ROUTE_REQUIRED')
    return addresses[0]


def require_bridge():
    if os.environ.get('BASSWIESN_ISOLATED_SENDER_FIXTURE') != '1':
        raise RuntimeError('EXPLICIT_ISOLATED_FIXTURE_REQUIRED')
    # The root orchestrator additionally verifies Docker internal=true, no
    # published ports and exact network ownership. Never an installation mode.
    with socket.socket(socket.AF_INET, socket.SOCK_DGRAM) as control:
        # SIOCGIFADDR reads the local namespace; it sends no network packet.
        value = fcntl.ioctl(control.fileno(), 0x8915, struct.pack('256s', b'eth0'))
    addresses = [socket.inet_ntoa(value[20:24])]
    routes = []
    for line in Path('/proc/net/route').read_text().splitlines()[1:]:
        row = line.split()
        destination = socket.inet_ntoa(struct.pack('<I', int(row[1], 16)))
        mask = socket.inet_ntoa(struct.pack('<I', int(row[7], 16)))
        route = {'dst': str(ipaddress.ip_network(f'{destination}/{mask}')), 'dev': row[0]}
        if int(row[2], 16):
            route['gateway'] = socket.inet_ntoa(struct.pack('<I', int(row[2], 16)))
        routes.append(route)
    if any(line.split()[0] != '00000000000000000000000000000001'
           for line in Path('/proc/net/if_inet6').read_text().splitlines()):
        raise RuntimeError('EXTERNAL_IPV6_PROHIBITED')
    return validate_bridge([name for _, name in socket.if_nameindex()], addresses, routes, os.getuid())
