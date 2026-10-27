"""Direction controls at the SIP boundary, without starting an endpoint or making a call."""
import asyncio
from types import SimpleNamespace
from unittest.mock import Mock

import pytest

pytest.importorskip("ai_phone.vendor.pjsua2")

from ai_phone.config import CallsConfig, Config, LineConfig
from ai_phone.telephony import call as call_module, events as ev
from ai_phone.telephony.endpoint import DialError, LineSpec, Telephony
from ai_phone.telephony.policy import PeerSet


@pytest.mark.parametrize("allow_inbound, busy, status", [
    (False, False, 403),
    (False, True, 403),
    (True, False, 180),
    (True, True, 486),
])
def test_inbound_direction_is_checked_before_the_claim_or_ringing(monkeypatch, allow_inbound, busy, status):
    cfg = Config(calls=CallsConfig(allow_inbound=allow_inbound))
    events = asyncio.Queue()
    queued = []
    loop = SimpleNamespace(call_soon_threadsafe=lambda callback, *args: queued.append((callback, args)))
    tel = Telephony(cfg, [], loop, events, None, None)
    previous = object() if busy else None
    tel.current_call = previous
    claim = Mock(wraps=tel.try_claim_call)
    tel.try_claim_call = claim
    allowed = Mock(return_value=True)
    monkeypatch.setattr(call_module, "source_allowed", allowed)
    monkeypatch.setattr(call_module, "_post", lambda loop, queue, event: queue.put_nowait(event))
    created = []

    class Incoming:
        def __init__(self, account, call_id, direction):
            self.acc, self.call_id, self.ended = account, call_id, False
            self.answers, self.hangups = [], []
            created.append(self)

        def getId(self):
            return self.call_id

        def getInfo(self):
            return SimpleNamespace(remoteUri="sip:caller@127.0.0.1")

        def answer(self, op):
            self.answers.append(op.statusCode)

        def hangup(self, op):
            self.hangups.append(op.statusCode)

    monkeypatch.setattr(call_module, "AiCall", Incoming)
    account = SimpleNamespace(telephony=tel, spec=SimpleNamespace(accept_invites_from="any"),
                              peers=PeerSet(frozenset(), ()), line_id="agents", loop=loop, events=events)
    prm = SimpleNamespace(callId=42, rdata=SimpleNamespace(srcAddress="127.0.0.1:5062"))
    call_module.LineAccount.onIncomingCall(account, prm)
    call = created[0]
    if status != 180:
        assert call.answers == [] and call.hangups == [] and events.empty()
        assert len(queued) == 1
        callback, args = queued.pop()
        assert args[0] is call  # keep the unclaimed native Call alive until the main thread handles it
        callback(*args)
    assert queued == []
    event = events.get_nowait()
    assert events.empty()
    if status == 180:
        assert call.answers == [180] and call.hangups == []
        assert isinstance(event, ev.IncomingCall) and tel.current_call is call
    else:
        assert call.answers == [] and call.hangups == [status]
        assert isinstance(event, ev.CallRejected) and event.code == status
        assert tel.current_call is previous
    assert claim.call_count == int(allow_inbound)
    assert allowed.call_count == int(allow_inbound)


def test_deferred_rejection_skips_a_call_that_already_ended():
    call = SimpleNamespace(ended=True, hangup=Mock())
    call_module._reject_incoming(call, "127.0.0.1", 403)
    call.hangup.assert_not_called()


def test_outbound_direction_is_checked_before_account_lookup_or_sip_work():
    tel = Telephony(Config(calls=CallsConfig(allow_outbound=False)), [], None, asyncio.Queue(), None, None)
    with pytest.raises(DialError, match="outbound calls are disabled"):
        tel.make_call("missing", "sip:callee@127.0.0.1")
    assert tel.current_call is None and tel.events.empty()


def test_outbound_only_keeps_trunk_registration_enabled():
    cfg = Config(calls=CallsConfig(allow_inbound=False))
    spec = LineSpec.from_config("main", LineConfig(registrar="sip:carrier.example", dial_domain="carrier.example",
                                                 number="+15555550100"), "test-password")
    tel = Telephony(cfg, [spec], None, asyncio.Queue(), None, None)
    tel.transport_ids["udp"] = 1
    account = tel._account_config(spec)
    assert account.regConfig.registerOnAdd is True
    assert account.regConfig.registrarUri == "sip:carrier.example"
