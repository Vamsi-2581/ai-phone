"""PJSIP endpoint lifecycle: transports, null audio device, G.711-only codecs, account, call control."""
from __future__ import annotations

import asyncio
import os

from ai_phone.config import Config
from ai_phone.telephony.call import AiAccount, AiCall
from ai_phone.telephony.media_port import RingMediaPort
from ai_phone.telephony.pj import pj
from ai_phone.telephony.rings import FrameRing


class Telephony:
    def __init__(self, cfg: Config, loop: asyncio.AbstractEventLoop, events: asyncio.Queue,
                 rx: FrameRing, tx: FrameRing, echo_mode: bool = False):
        self.cfg, self.loop, self.events, self.rx, self.tx = cfg, loop, events, rx, tx
        self.echo_mode = echo_mode
        self.ep: pj.Endpoint | None = None
        self.acc: AiAccount | None = None
        self.port: RingMediaPort | None = None
        self.current_call: AiCall | None = None
        self.registrar_ip: str | None = None

    @property
    def current_call_id(self) -> int | None:
        return self.current_call.getId() if self.current_call is not None else None

    def start(self) -> None:
        ep = pj.Endpoint()
        ep.libCreate()
        ec = pj.EpConfig()
        ec.uaConfig.threadCnt = 1
        ec.medConfig.clockRate = ec.medConfig.sndClockRate = 16000
        ec.medConfig.audioFramePtime = 20
        ec.medConfig.ecTailLen = 0
        ec.medConfig.noVad = True
        ec.logConfig.level = self.cfg.log.pjsip_level
        ep.libInit(ec)
        ep.audDevManager().setNullDev()
        tc = pj.TransportConfig()
        tc.port = self.cfg.sip.bind.port
        tc.boundAddress = self.cfg.sip.bind.addr
        if self.cfg.sip.public_addr:
            tc.publicAddress = self.cfg.sip.public_addr
        ep.transportCreate(pj.PJSIP_TRANSPORT_UDP, tc)
        ep.transportCreate(pj.PJSIP_TRANSPORT_TCP, tc)
        ep.libStart()
        wanted = tuple(f"{c}/8000" for c in self.cfg.codecs)  # ids carry a channel suffix, e.g. PCMU/8000/1
        for ci in ep.codecEnum2():
            ep.codecSetPriority(ci.codecId, 255 if ci.codecId.startswith(wanted) else 0)
        self.port = RingMediaPort(self.rx, self.tx, echo=self.echo_mode)
        ac = pj.AccountConfig()
        s = self.cfg.sip
        if s.domain:
            ac.idUri = f"sip:{s.user}@{s.domain}"
            ac.regConfig.registrarUri = f"sip:{s.domain}"
            ac.regConfig.timeoutSec = s.register_expiry_sec
            pw = s.password or (os.environ.get(s.password_env, "") if s.password_env else "")
            ac.sipConfig.authCreds.append(pj.AuthCredInfo("digest", "*", s.user, 0, pw))
        else:
            ac.idUri = f"sip:{s.user}@{s.bind.addr if s.bind.addr != '0.0.0.0' else '127.0.0.1'}"
            ac.regConfig.registerOnAdd = False
        ac.mediaConfig.transportConfig.port = self.cfg.rtp.port_min
        ac.mediaConfig.transportConfig.portRange = self.cfg.rtp.port_max - self.cfg.rtp.port_min
        ac.mediaConfig.srtpUse = pj.PJMEDIA_SRTP_OPTIONAL
        ac.mediaConfig.srtpSecureSignaling = 0  # accept SRTP when offered, but never require TLS signalling for it
        self.acc = AiAccount(self)
        self.acc.create(ac)
        self.ep = ep

    def answer(self, call_id: int) -> None:
        if self.current_call is not None and self.current_call.getId() == call_id:
            op = pj.CallOpParam()
            op.statusCode = pj.PJSIP_SC_OK
            self.current_call.answer(op)

    def hangup(self, call_id: int, code: int = 200) -> None:
        if self.current_call is not None and self.current_call.getId() == call_id:
            op = pj.CallOpParam()
            op.statusCode = code
            self.current_call.hangup(op)

    def _call_ended(self, call: AiCall) -> None:
        if self.current_call is call:
            self.current_call = None

    def stop(self) -> None:
        if self.current_call is not None:
            try:
                self.hangup(self.current_call.getId())
            except Exception:
                pass
        if self.ep is not None:
            # pjsua2 objects must be released before libDestroy or PJSIP asserts at interpreter exit
            self.current_call = None
            if self.acc is not None:
                try:
                    self.acc.shutdown()
                except Exception:
                    pass
            self.acc = None
            self.port = None
            self.ep.libDestroy()
            self.ep = None
