import pytest

pytestmark = pytest.mark.integration


def test_keepalive_setter_round_trips():
    from ai_phone.telephony.pj import pj
    before = pj.ai_phone_get_keepalive()  # process-wide: another test's endpoint may already have changed it
    assert all(isinstance(v, int) for v in before)
    try:
        pj.ai_phone_set_keepalive(45, 46)
        assert pj.ai_phone_get_keepalive() == (45, 46)
    finally:
        pj.ai_phone_set_keepalive(*before)  # process-wide: never leave a failed assertion's values behind
    assert pj.ai_phone_get_keepalive() == before


def test_symbols_needed_by_m2_exist():
    from ai_phone.telephony.pj import pj
    for name in ("PJSIP_TRANSPORT_TLS", "PJSIP_REDIRECT_STOP", "PJMEDIA_SRTP_DISABLED", "PJMEDIA_SRTP_MANDATORY", "PJSUA_REG_USE_OUTBOUND_PROXY"):
        assert hasattr(pj, name), name
