"""Call ownership at shutdown, without starting a native endpoint or making calls."""
import asyncio
import weakref

import pytest

from ai_phone.config import Config
from ai_phone.telephony.endpoint import Telephony


class Call:
    ended = True
    answer_timeout = None

    def __init__(self, call_id):
        self.call_id = call_id

    def getId(self):
        return self.call_id


class Endpoint:
    def __init__(self, order, call_ref):
        self.order, self.call_ref = order, call_ref

    def libDestroy(self):
        self.order.append("endpoint destroyed")
        assert self.call_ref() is None, "the native Call must be destroyed before its endpoint"


def telephony():
    return Telephony(Config(), [], asyncio.get_running_loop(), asyncio.Queue(), None, None)


@pytest.mark.asyncio
async def test_queued_ended_cleanup_does_not_keep_a_call_alive_past_endpoint_shutdown():
    tel = telephony()
    order, callback_errors = [], []
    call = Call(7)
    call_ref = weakref.ref(call)
    weakref.finalize(call, order.append, "call destroyed")
    tel.ep = Endpoint(order, call_ref)
    assert tel.try_claim_call(call)
    timer = call.answer_timeout = tel.loop.call_later(3600, tel._cancel_if_unanswered, call)
    tel._call_ended(call)  # queued on asyncio while PJSIP reports DISCONNECTED
    del call
    old_handler = tel.loop.get_exception_handler()
    tel.loop.set_exception_handler(lambda loop, context: callback_errors.append(context))
    try:
        tel.stop()  # shutdown happens before the queued callback gets to run
        assert timer.cancelled() and call_ref() is None
        assert order == ["call destroyed", "endpoint destroyed"]
        await asyncio.sleep(0)  # the old callback must tolerate its dead weak reference
        assert callback_errors == [] and order == ["call destroyed", "endpoint destroyed"]
    finally:
        timer.cancel()
        tel.loop.set_exception_handler(old_handler)


@pytest.mark.asyncio
async def test_release_cancels_the_call_owning_timer_before_dropping_the_claim():
    tel = telephony()
    call = Call(7)
    call_ref = weakref.ref(call)
    assert tel.try_claim_call(call)
    timer = tel.loop.call_later(3600, tel._cancel_if_unanswered, call)
    cancelled_while_claimed = []

    class CheckedTimer:
        def cancel(self):
            cancelled_while_claimed.append(tel.current_call is call_ref())
            timer.cancel()

    call.answer_timeout = CheckedTimer()
    try:
        assert tel.release_call(7)
        assert cancelled_while_claimed == [True] and timer.cancelled()
        assert tel.current_call is None and call.answer_timeout is None
        del call
        assert call_ref() is None, "the cancelled timer must no longer own the Call"
    finally:
        timer.cancel()


@pytest.mark.asyncio
async def test_old_queued_ended_cleanup_does_not_cancel_the_next_calls_timer():
    tel = telephony()
    old = Call(7)
    old_ref = weakref.ref(old)
    assert tel.try_claim_call(old)
    old_timer = old.answer_timeout = tel.loop.call_later(3600, tel._cancel_if_unanswered, old)
    tel._call_ended(old)
    assert tel.release_call(7)
    del old
    new = Call(7)  # PJSIP may reuse the native id; cleanup must follow the old object.
    new_timer = new.answer_timeout = tel.loop.call_later(3600, tel._cancel_if_unanswered, new)
    try:
        assert old_ref() is None and old_timer.cancelled()
        assert tel.try_claim_call(new)
        await asyncio.sleep(0)
        assert tel.current_call is new and new.answer_timeout is new_timer
        assert not new_timer.cancelled()
    finally:
        old_timer.cancel()
        new_timer.cancel()
        tel.release_call(7)
