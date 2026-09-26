"""Pure DHCP validation, used inside an isolated receiver network namespace."""
from __future__ import annotations

import ipaddress
import re


def validate_config(value: dict) -> dict:
    if not re.fullmatch(r"[a-zA-Z0-9_-]{1,15}", value.get("interface", "")):
        raise ValueError("invalid interface")
    name = value.get("name", "")
    if not isinstance(name, str) or not name.startswith("AP2 ") or len(name.encode()) > 63:
        raise ValueError("invalid receiver name")
    if any(not c.isprintable() for c in name):
        raise ValueError("invalid receiver name")
    network = ipaddress.IPv4Network(value["network"], strict=True)
    server = ipaddress.IPv4Address(value["dhcp_server"])
    denied = {ipaddress.IPv4Address(x) for x in value["deny_ips"]}
    host = ipaddress.IPv4Address(value["host_ip"])
    if not denied or server in denied or host in denied or server not in network or host not in network:
        raise ValueError("invalid server/host/protection configuration")
    if (server in (network.network_address, network.broadcast_address)
            or host in (network.network_address, network.broadcast_address) or host == server):
        raise ValueError("invalid server/host address")
    seconds = value.get("lifetime_seconds", 900)
    forwarding = value.get('radio_volume_forwarding', False)
    if type(forwarding) is not bool or (forwarding and not value.get('relay_target')):
        raise ValueError('volume forwarding requires an explicit output target')
    if type(seconds) is not int or not 60 <= seconds <= 1800:
        raise ValueError("probe lifetime must be 60..1800 seconds")
    if value.get('relay_target') is not None:
        target = ipaddress.IPv4Address(value['relay_target'])
        if (target in denied or target not in network or target in
                (server, host, network.network_address, network.broadcast_address)):
            raise ValueError('relay target is excluded')
        if not re.fullmatch(r'[a-f0-9]{64}', value.get('relay_token', '')):
            raise ValueError('invalid relay token')
    return {**value, "network": str(network), "dhcp_server": str(server),
            "host_ip": str(host), "deny_ips": sorted(str(x) for x in denied),
            "lifetime_seconds": seconds}


def validate_lease(config: dict, env: dict) -> dict:
    config = validate_config(config)
    if env.get("interface") != config["interface"]:
        raise ValueError("interface mismatch")
    server = ipaddress.IPv4Address(env.get("serverid", ""))
    address = ipaddress.IPv4Address(env.get("ip", ""))
    subnet = ipaddress.IPv4Network(config["network"])
    offered = ipaddress.IPv4Network(f"{address}/{env.get('subnet', '')}", strict=False)
    if str(server) != config["dhcp_server"] or offered != subnet:
        raise ValueError("unexpected DHCP server or subnet")
    excluded = set(config["deny_ips"]) | {config["host_ip"], config["dhcp_server"],
        str(subnet.network_address), str(subnet.broadcast_address), config.get('relay_target')}
    if str(address) in excluded or address not in subnet:
        raise ValueError("excluded lease address")
    lease = int(env.get("lease", "0"))
    if not 60 <= lease <= 2**32 - 1:
        raise ValueError("invalid lease duration")
    # No default gateway, DNS resolver, or arbitrary server routes are applied.
    # The local subnet is sufficient for this receive-only pilot.
    return {"address": str(address), "prefix": subnet.prefixlen, "lease_seconds": lease,
            "server": str(server)}


def firewall_rules(config):
    cfg = validate_config(config)
    denied = ', '.join(cfg['deny_ips'])
    target = cfg.get('relay_target')
    output = input_rule = ''
    if target:
        output = (f'ip daddr {target} tcp sport 8098 ct state established accept\n'
                  f'ip daddr {target} counter drop\n')
        input_rule = (f'ip saddr {target} tcp dport 8098 accept\n'
                      f'ip saddr {target} counter drop\n')
    return f'''table ip ap2_guard {{
 chain output {{ type filter hook output priority 0; policy accept;
  ip daddr {{ {denied} }} counter drop
  {output}
 }}
 chain input {{ type filter hook input priority 0; policy accept;
  ip saddr {{ {denied} }} counter drop
  {input_rule}
 }}
}}
'''
