import socket

from ai_phone.telephony.policy import PeerSet, build_peer_set, source_allowed


def _resolver_ok(host, *a, **k):
    return [(socket.AF_INET, socket.SOCK_DGRAM, 17, "", ("198.51.100.9", 0)),
            (socket.AF_INET6, socket.SOCK_DGRAM, 17, "", ("2001:db8::9", 0, 0, 0))]


def _resolver_fail(host, *a, **k):
    raise socket.gaierror("no such host")


def test_build_peer_set_mixes_ips_cidrs_and_names():
    peers, warnings = build_peer_set(["203.0.113.7", "192.76.120.0/24", "sip.example.org"], resolver=_resolver_ok)
    assert warnings == []
    assert peers.contains("203.0.113.7") and peers.contains("192.76.120.55") and peers.contains("198.51.100.9") and peers.contains("2001:db8::9")
    assert not peers.contains("203.0.113.8")


def test_unresolvable_name_is_a_warning_not_trusted():
    peers, warnings = build_peer_set(["nowhere.invalid"], resolver=_resolver_fail)
    assert warnings and "nowhere.invalid" in warnings[0]
    assert not peers.contains("198.51.100.9")


def test_modes():
    peers = build_peer_set(["192.76.120.0/24"], resolver=_resolver_fail)[0]
    assert source_allowed("any", "8.8.8.8", peers)
    assert source_allowed("lan", "192.168.1.20", peers) and source_allowed("lan", "127.0.0.1", peers) and source_allowed("lan", "fe80::1", peers)
    assert not source_allowed("lan", "8.8.8.8", peers)
    assert source_allowed("registered_trunk", "192.76.120.3", peers) and not source_allowed("registered_trunk", "8.8.8.8", peers)
    assert source_allowed("trusted", "192.76.120.3", peers) and not source_allowed("trusted", "10.0.0.1", peers)
    assert not source_allowed("registered_trunk", "not-an-ip", peers)
    assert not source_allowed("bogus", "127.0.0.1", peers)
