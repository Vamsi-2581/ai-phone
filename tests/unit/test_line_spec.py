import pytest

pytest.importorskip("ai_phone.vendor.pjsua2")  # endpoint.py imports the pjsua2 extension at module load

from ai_phone.config import LineConfig
from ai_phone.telephony.endpoint import LineSpec, id_uri, local_uri, srtp_values


def _trunk(**kw):
    base = dict(kind="trunk", number="+442079460000", display_name="Sam's assistant", registrar="sip:sip.telnyx.com",
                dial_domain="sip.telnyx.com", user="aimain7f3a", caller_id="+442079460000", trusted_cidrs=["192.76.120.0/24"])
    base.update(kw)
    return LineSpec.from_config("main", LineConfig(**base), "pw")


def test_trunk_uris():
    s = _trunk()
    assert id_uri(s) == '"Sam\'s assistant" <sip:aimain7f3a@sip.telnyx.com>'
    assert local_uri(s) == '"Sam\'s assistant" <sip:+442079460000@sip.telnyx.com>'
    assert s.registrar_host == "sip.telnyx.com" and s.password == "pw" and s.id == "main"


def test_sip_line_uris_use_the_aor_verbatim():
    s = LineSpec.from_config("agents", LineConfig(kind="sip", user="agents", aor="sip:agents@home.example.org",
                                                 accept_invites_from="trusted", trusted_peers=["203.0.113.7"],
                                                 caller_id="sip:agents@home.example.org", display_name="agents"), None)
    assert id_uri(s) == '"agents" <sip:agents@home.example.org>'
    assert local_uri(s) == "sip:agents@home.example.org"
    assert s.registrar_host is None


def test_from_config_refuses_unvalidated_parts():
    with pytest.raises(ValueError, match="display_name"):
        _trunk(display_name='evil" <sip:x@y>')
    with pytest.raises(ValueError, match="user"):
        _trunk(user="a b")
    with pytest.raises(ValueError, match="display_name"):
        _trunk(display_name="safe\n")  # `$` alone would let a trailing newline through
    with pytest.raises(ValueError, match="number"):
        _trunk(number="+442079460000\n")
    with pytest.raises(ValueError, match="aor"):
        LineSpec.from_config("s", LineConfig(kind="sip", user="ok", aor="sip:ok@host;evil", display_name="ok"), None)


def test_srtp_mapping():
    from ai_phone.telephony.pj import pj
    assert srtp_values(_trunk()) == (pj.PJMEDIA_SRTP_DISABLED, 0)
    assert srtp_values(_trunk(transport="tls", srtp="optional")) == (pj.PJMEDIA_SRTP_OPTIONAL, 1)
    assert srtp_values(_trunk(transport="tls", srtp="required")) == (pj.PJMEDIA_SRTP_MANDATORY, 1)


from ai_phone.telephony.endpoint import DialError, check_caller_id, request_uri


def test_request_uri_for_pstn_and_sip_targets():
    s = _trunk()
    assert request_uri(s, "+447700900123") == "sip:+447700900123@sip.telnyx.com"
    assert request_uri(s, "sip:bob@203.0.113.7:5062") == "sip:bob@203.0.113.7:5062"
    with pytest.raises(DialError, match="E.164"):
        request_uri(s, "07700900123")
    with pytest.raises(DialError, match="E.164"):
        request_uri(s, "+447700900123\n")               # `$` alone would let a trailing newline through
    for bad in ("sip:bob@host;evil=1", "sip:bob@host\r\nVia: x", "sips:bob@host", "sip:bob"):
        with pytest.raises(DialError, match="sip:"):
            request_uri(s, bad)


def test_caller_id_must_be_own_number_or_verified():
    check_caller_id(_trunk())  # own number
    check_caller_id(_trunk(caller_id="+447700900123", verified_caller_ids=[__import__("ai_phone.config", fromlist=["VerifiedCallerId"]).VerifiedCallerId("+447700900123", "telnyx", "2026-09-10T00:00:00Z")]))
    with pytest.raises(DialError, match="verified"):
        check_caller_id(_trunk(caller_id="+447700900999"))


class _Call:
    def __init__(self, cid):
        self._cid, self.ended, self.answer_timeout = cid, False, None

    def getId(self):
        return self._cid


def test_claim_is_held_until_the_daemon_releases_it():
    import asyncio
    from ai_phone.config import Config
    from ai_phone.telephony.endpoint import Telephony
    tel = Telephony(Config(), [], asyncio.new_event_loop(), asyncio.Queue(), None, None)
    a, b = _Call(1), _Call(2)
    assert tel.try_claim_call(a) and not tel.try_claim_call(b)
    tel._call_ended(a)                      # PJSIP says disconnected: still claimed
    assert not tel.try_claim_call(b)
    assert not tel.release_call(2) and tel.release_call(1)
    assert tel.try_claim_call(b)
    tel._stopping = True
    tel.release_call(2)
    assert not tel.try_claim_call(a)        # nothing is claimed while stopping
    tel.loop.close()


def test_trunk_without_number_or_caller_id_is_refused():
    with pytest.raises(DialError, match="neither"):
        check_caller_id(_trunk(number=None, caller_id=None))


# ---- incoming request -> line ---------------------------------------------------------------------------
from ai_phone.telephony.endpoint import line_for_request, request_target  # noqa: E402
from ai_phone.telephony.policy import build_peer_set  # noqa: E402


def _specs():
    local = LineSpec.from_config("local", LineConfig(kind="sip", aor="sip:ai@127.0.0.1", accept_invites_from="lan"), None)
    main = LineSpec.from_config("main", LineConfig(kind="trunk", number="+15555550123", registrar="sip:sip.example.net", user="user1234",
                                                  password_env="AI_PHONE_LINE_MAIN_PASSWORD", accept_invites_from="registered_trunk",
                                                  trusted_cidrs=["192.76.120.10/32", "64.16.250.10/32"]), "pw")
    peers = {"main": build_peer_set(["192.76.120.10/32", "64.16.250.10/32"])[0], "local": build_peer_set([])[0]}
    return [local, main], peers


LAN_INVITE = "INVITE sip:ai@127.0.0.1 SIP/2.0\r\nVia: SIP/2.0/UDP 127.0.0.1:5062\r\nFrom: <sip:192.168.0.58>;tag=1\r\nTo: <sip:ai@127.0.0.1>\r\n\r\n"
TRUNK_INVITE = ("INVITE sip:user1234@203.0.113.11:58756;transport=TCP;ob SIP/2.0\r\nv: SIP/2.0/UDP 10.31.35.4:6000\r\n"
                "f: \"447700900555\"<sip:447700900555@sip.example.net>;tag=x\r\nt: <sip:15555550123@192.0.2.10>\r\n\r\n")


def test_request_target_prefers_request_uri_then_to():
    assert request_target(LAN_INVITE) == ("ai", "127.0.0.1")
    assert request_target(TRUNK_INVITE) == ("user1234", "203.0.113.11")
    assert request_target("SIP/2.0 200 OK\r\nTo: \"x\" <sip:bob@[::1]:5060>;tag=1\r\n\r\n") == ("bob", "::1")


def test_lan_invite_goes_to_the_sip_line_not_the_trunk():
    specs, peers = _specs()
    assert line_for_request(specs, peers, LAN_INVITE, "127.0.0.1") == "local"
    # any address of this machine in the request URI still reaches the sip line by user
    assert line_for_request(specs, peers, LAN_INVITE.replace("127.0.0.1", "192.168.0.58"), "192.168.0.7") == "local"


def test_trunk_invite_matches_by_registrar_proxy_or_sip_user():
    specs, peers = _specs()
    assert line_for_request(specs, peers, TRUNK_INVITE, "64.16.250.10") == "main"   # from a trusted proxy
    assert line_for_request(specs, peers, TRUNK_INVITE, "203.0.113.9") == "main"    # unknown source, but names our SIP user


def test_unknown_request_keeps_pjsips_choice():
    specs, peers = _specs()
    assert line_for_request(specs, peers, "INVITE sip:nobody@example.org SIP/2.0\r\nTo: <sip:nobody@example.org>\r\n\r\n", "203.0.113.9") is None
    assert line_for_request(specs, peers, "garbage", "203.0.113.9") is None
