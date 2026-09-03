"""Who may ring a line: `accept_invites_from` evaluated against the INVITE's source address and the line's peer set."""
from __future__ import annotations

import ipaddress
import socket
from dataclasses import dataclass

_LAN_NETS = [ipaddress.ip_network(n) for n in
             ("10.0.0.0/8", "172.16.0.0/12", "192.168.0.0/16", "127.0.0.0/8", "::1/128", "fe80::/10")]


@dataclass(frozen=True)
class PeerSet:
    ips: frozenset[str]
    nets: tuple

    def contains(self, ip: str) -> bool:
        try:
            addr = ipaddress.ip_address(ip)
        except ValueError:
            return False
        return str(addr) in self.ips or any(addr in n for n in self.nets)


def build_peer_set(entries: list[str], resolver=socket.getaddrinfo) -> tuple[PeerSet, list[str]]:
    """IPs are kept as they are, CIDRs become networks, anything else is resolved once (all A/AAAA records)."""
    ips: set[str] = set()
    nets: list = []
    warnings: list[str] = []
    for e in entries:
        try:
            ips.add(str(ipaddress.ip_address(e)))
            continue
        except ValueError:
            pass
        try:
            nets.append(ipaddress.ip_network(e, strict=False))
            continue
        except ValueError:
            pass
        try:
            for fam, _t, _p, _c, sockaddr in resolver(e, None):
                ips.add(str(ipaddress.ip_address(sockaddr[0])))
        except (socket.gaierror, OSError, ValueError) as err:
            warnings.append(f"trusted peer {e!r} did not resolve ({err}); it is not trusted until the daemon restarts")
    return PeerSet(frozenset(ips), tuple(nets)), warnings


def source_allowed(mode: str, source_ip: str, peers: PeerSet) -> bool:
    try:
        ip = ipaddress.ip_address(source_ip)
    except ValueError:
        return False
    if mode == "any":
        return True
    if mode == "lan":
        return any(ip in net for net in _LAN_NETS)
    if mode in ("registered_trunk", "trusted"):
        return peers.contains(str(ip))
    return False
