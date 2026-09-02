"""pjsua2 Account and Call subclasses: decide fast on PJSIP's thread, post events to the asyncio loop."""
from __future__ import annotations

import asyncio

from ai_phone.telephony import events as ev
from ai_phone.telephony.pj import pj
from ai_phone.telephony.policy import source_allowed


def _post(loop: asyncio.AbstractEventLoop, q: asyncio.Queue, item) -> None:
    loop.call_soon_threadsafe(q.put_nowait, item)


class AiCall(pj.Call):
    def __init__(self, acc: "AiAccount", call_id: int = pj.PJSUA_INVALID_ID):
        super().__init__(acc, call_id)
        self.acc = acc
        self.media_connected = False

    def onCallState(self, prm) -> None:
        ci = self.getInfo()
        if ci.state == pj.PJSIP_INV_STATE_CONFIRMED:
            _post(self.acc.loop, self.acc.events, ev.CallStateChanged(self.getId(), "active"))
        elif ci.state == pj.PJSIP_INV_STATE_DISCONNECTED:
            _post(self.acc.loop, self.acc.events, ev.CallStateChanged(self.getId(), "ended"))
            self.acc.telephony._call_ended(self)

    def onCallMediaState(self, prm) -> None:
        ci = self.getInfo()
        for mi in ci.media:
            if mi.type == pj.PJMEDIA_TYPE_AUDIO and mi.status == pj.PJSUA_CALL_MEDIA_ACTIVE and not self.media_connected:
                am = self.getAudioMedia(mi.index)
                port = self.acc.telephony.port
                am.startTransmit(port)
                port.startTransmit(am)
                self.media_connected = True
                _post(self.acc.loop, self.acc.events, ev.MediaActive(self.getId()))

    def onDtmfDigit(self, prm) -> None:
        _post(self.acc.loop, self.acc.events, ev.Dtmf(self.getId(), prm.digit))


class AiAccount(pj.Account):
    def __init__(self, telephony: "Telephony"):
        super().__init__()
        self.telephony = telephony
        self.loop, self.events = telephony.loop, telephony.events

    def onRegState(self, prm) -> None:
        ai = self.getInfo()
        _post(self.loop, self.events, ev.RegState(bool(ai.regIsActive), int(prm.code), str(prm.reason)))

    def onIncomingCall(self, prm) -> None:  # PJSIP thread: decide fast, no models
        t = self.telephony
        call = AiCall(self, prm.callId)
        source_ip = str(prm.rdata.srcAddress).rsplit(":", 1)[0].strip("[]")
        op = pj.CallOpParam()
        if not source_allowed(t.cfg.sip.accept_invites_from, source_ip, t.cfg.sip.trusted_peers, t.registrar_ip):
            op.statusCode = pj.PJSIP_SC_FORBIDDEN
            call.hangup(op)
            return
        if t.current_call is not None:
            op.statusCode = pj.PJSIP_SC_BUSY_HERE
            call.hangup(op)
            return
        t.current_call = call
        op.statusCode = pj.PJSIP_SC_RINGING
        call.answer(op)
        _post(self.loop, self.events, ev.IncomingCall(call.getId(), str(call.getInfo().remoteUri), source_ip))
