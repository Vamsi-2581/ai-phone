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


def test_caller_id_must_be_own_number_or_verified():
    check_caller_id(_trunk())  # own number
    check_caller_id(_trunk(caller_id="+447700900123", verified_caller_ids=[__import__("ai_phone.config", fromlist=["VerifiedCallerId"]).VerifiedCallerId("+447700900123", "telnyx", "2026-09-10T00:00:00Z")]))
    with pytest.raises(DialError, match="verified"):
        check_caller_id(_trunk(caller_id="+447700900999"))


def test_trunk_without_number_or_caller_id_is_refused():
    with pytest.raises(DialError, match="neither"):
        check_caller_id(_trunk(number=None, caller_id=None))
