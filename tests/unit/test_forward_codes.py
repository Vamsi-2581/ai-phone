import pytest

from ai_phone.lines.forward import WHEN, divert_codes


def test_codes_use_register_and_activate_form():
    c = divert_codes("no-answer", "+442079460000")
    assert c == {"activate": "**61*+442079460000#", "check": "*#61#", "cancel": "##61#", "cancel_all": "##002#"}
    assert divert_codes("all", "+442079460000")["activate"] == "**21*+442079460000#"
    assert divert_codes("busy", "+442079460000")["activate"].startswith("**67*")
    assert divert_codes("unreachable", "+442079460000")["activate"].startswith("**62*")
    assert set(WHEN) == {"all", "busy", "no-answer", "unreachable"}
    with pytest.raises(ValueError):
        divert_codes("sometimes", "+442079460000")
