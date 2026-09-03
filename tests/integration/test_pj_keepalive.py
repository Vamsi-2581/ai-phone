import pytest

pytestmark = pytest.mark.integration


def test_keepalive_setter_round_trips():
    from ai_phone.telephony.pj import pj
    assert pj.ai_phone_get_keepalive() == (90, 90)  # PJSIP's compiled defaults
    pj.ai_phone_set_keepalive(30, 30)
    assert pj.ai_phone_get_keepalive() == (30, 30)
    pj.ai_phone_set_keepalive(90, 90)


def test_symbols_needed_by_m2_exist():
    from ai_phone.telephony.pj import pj
    for name in ("PJSIP_TRANSPORT_TLS", "PJSIP_REDIRECT_STOP", "PJMEDIA_SRTP_DISABLED", "PJMEDIA_SRTP_MANDATORY", "PJSUA_REG_USE_OUTBOUND_PROXY"):
        assert hasattr(pj, name), name
