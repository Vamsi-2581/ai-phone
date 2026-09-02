from ai_phone.telephony.policy import source_allowed


def test_lan_mode_accepts_private_loopback_and_linklocal():
    for ip in ("192.168.1.20", "10.0.0.5", "172.16.4.4", "127.0.0.1", "::1", "fe80::1"):
        assert source_allowed("lan", ip, [], None), ip


def test_lan_mode_rejects_public():
    for ip in ("8.8.8.8", "2001:4860::8888", "100.64.0.1"):
        assert not source_allowed("lan", ip, [], None), ip


def test_registered_trunk_accepts_only_registrar_and_trusted():
    assert source_allowed("registered_trunk", "203.0.113.5", [], "203.0.113.5")
    assert source_allowed("registered_trunk", "198.51.100.9", ["198.51.100.9"], "203.0.113.5")
    assert not source_allowed("registered_trunk", "192.168.1.20", [], "203.0.113.5")
    assert not source_allowed("registered_trunk", "203.0.113.5", [], None)


def test_any_mode_accepts_everything():
    assert source_allowed("any", "8.8.8.8", [], None)


def test_garbage_ip_is_rejected():
    assert not source_allowed("lan", "not-an-ip", [], None)
