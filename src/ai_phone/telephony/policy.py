"""Who may ring us: evaluation of `accept_invites_from` against the INVITE's source address."""
from __future__ import annotations

import ipaddress

_LAN_NETS = [ipaddress.ip_network(n) for n in
             ("10.0.0.0/8", "172.16.0.0/12", "192.168.0.0/16", "127.0.0.0/8", "::1/128", "fe80::/10")]


def source_allowed(mode: str, source_ip: str, trusted: list[str], registrar_ip: str | None) -> bool:
    try:
        ip = ipaddress.ip_address(source_ip)
    except ValueError:
        return False
    if mode == "any":
        return True
    if mode == "lan":
        return any(ip in net for net in _LAN_NETS)
    if mode == "registered_trunk":
        if registrar_ip is not None and str(ip) == registrar_ip:
            return True
        return str(ip) in set(trusted)
    return False
