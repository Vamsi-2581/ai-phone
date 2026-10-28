"""An inbound caller can disappear before, or during, the final answer."""
import asyncio
from types import SimpleNamespace
from unittest.mock import Mock

import pytest

from ai_phone.config import Config
from ai_phone.telephony.endpoint import Telephony
from ai_phone.telephony.pj import pj


def native_error(status):
    error = pj.Error()
    error.status = status
    return error


def phone(*, ended=False, active=True):
    tel = Telephony(Config(), [], None, asyncio.Queue(), None, None)
    call = SimpleNamespace(getId=lambda: 7, ended=ended, isActive=Mock(return_value=active), answer=Mock())
    tel.current_call = call
    return tel, call


@pytest.mark.parametrize("state", ["missing", "different", "ended", "inactive"])
def test_answer_skips_a_call_that_is_no_longer_available(state):
    tel, call = phone(ended=state == "ended", active=state != "inactive")
    if state == "missing":
        tel.current_call = None
    assert tel.answer(8 if state == "different" else 7) is False
    call.answer.assert_not_called()


def test_cancel_between_the_activity_check_and_answer_is_a_normal_end():
    tel, call = phone()
    call.answer.side_effect = native_error(171140)  # PJSIP_ESESSIONTERMINATED

    assert tel.answer(7) is False
    call.answer.assert_called_once()
    assert call.answer.call_args.args[0].statusCode == pj.PJSIP_SC_OK
    assert tel.current_call is call  # the queued ended event still owns claim release


def test_an_unrelated_answer_error_is_not_reported_as_cancellation():
    tel, call = phone()
    error = native_error(171141)  # PJSIP_ESESSIONSTATE, not ESESSIONTERMINATED
    call.answer.side_effect = error

    with pytest.raises(pj.Error) as raised:
        tel.answer(7)
    assert raised.value is error
    assert tel.current_call is call


def test_successful_answer_keeps_the_call_claimed():
    tel, call = phone()
    assert tel.answer(7) is True
    assert tel.current_call is call
    call.answer.assert_called_once()
