"""PJSIP endpoint lifecycle: transports from sip.bind, null audio device, G.711-only codecs, one Account per line."""
from __future__ import annotations

import asyncio
import logging
import socket
from dataclasses import dataclass, field

from ai_phone.config import Config, LineConfig, host_of
from ai_phone.telephony.call import AiCall, LineAccount
from ai_phone.telephony.media_port import RingMediaPort
from ai_phone.telephony.pj import pj
from ai_phone.telephony.policy import PeerSet, build_peer_set
from ai_phone.telephony.rings import FrameRing

log = logging.getLogger("ai-phone.telephony")


@dataclass
class LineSpec:
    id: str
    kind: str
    user: str
    password: str | None
    display_name: str
    number: str | None
    aor: str | None
    registrar: str | None
    outbound_proxy: str | None
    dial_domain: str | None
    register_via_proxy: bool
    transport: str
    register_expiry_sec: int
    srtp: str
    accept_invites_from: str
    trusted_cidrs: list[str] = field(default_factory=list)
    trusted_peers: list[str] = field(default_factory=list)
    caller_id: str | None = None
    verified_caller_ids: list[str] = field(default_factory=list)
    persona: str = "default"

    @property
    def registrar_host(self) -> str | None:
        return host_of(self.registrar) if self.registrar else None

    @property
    def aor_host(self) -> str:
        return host_of(self.aor) if self.aor else "127.0.0.1"

    @classmethod
    def from_config(cls, line_id: str, c: LineConfig, password: str | None) -> "LineSpec":
        return cls(id=line_id, kind=c.kind, user=c.user, password=password, display_name=c.display_name, number=c.number,
                   aor=c.aor, registrar=c.registrar, outbound_proxy=c.outbound_proxy, dial_domain=c.dial_domain,
                   register_via_proxy=c.register_via_proxy, transport=c.transport, register_expiry_sec=c.register_expiry_sec,
                   srtp=c.srtp, accept_invites_from=c.accept_invites_from, trusted_cidrs=list(c.trusted_cidrs),
                   trusted_peers=list(c.trusted_peers), caller_id=c.caller_id,
                   verified_caller_ids=[v.number for v in c.verified_caller_ids], persona=c.persona)


def id_uri(spec: LineSpec) -> str:
    host = spec.registrar_host if spec.kind == "trunk" else spec.aor_host
    user = spec.user if spec.kind == "trunk" else spec.aor.split(":", 1)[1].split("@", 1)[0]
    return f'"{spec.display_name}" <sip:{user}@{host}>'


def local_uri(spec: LineSpec) -> str:
    """Per-call From. Trunks present the E.164 caller id at the registrar; sip lines use their caller_id URI verbatim."""
    if spec.kind == "trunk":
        return f'"{spec.display_name}" <sip:{spec.caller_id or spec.number}@{spec.registrar_host}>'
    return spec.caller_id or spec.aor


def srtp_values(spec: LineSpec) -> tuple[int, int]:
    use = {"off": pj.PJMEDIA_SRTP_DISABLED, "optional": pj.PJMEDIA_SRTP_OPTIONAL, "required": pj.PJMEDIA_SRTP_MANDATORY}[spec.srtp]
    return use, (0 if spec.srtp == "off" else 1)


def _system_resolvers() -> list[str]:
    try:
        return [l.split()[1] for l in open("/etc/resolv.conf") if l.startswith("nameserver")][:4]
    except OSError:
        return []


class Telephony:
    def __init__(self, cfg: Config, lines: list[LineSpec], loop: asyncio.AbstractEventLoop, events: asyncio.Queue,
                 rx: FrameRing, tx: FrameRing, echo_mode: bool = False):
        self.cfg, self.lines, self.loop, self.events, self.rx, self.tx = cfg, lines, loop, events, rx, tx
        self.echo_mode = echo_mode
        self.ep: pj.Endpoint | None = None
        self.port: RingMediaPort | None = None
        self.accounts: dict[str, LineAccount] = {}
        self.transport_ids: dict[str, int] = {}
        self.current_call: AiCall | None = None
        self.peer_warnings: list[str] = []

    @property
    def current_call_id(self) -> int | None:
        return self.current_call.getId() if self.current_call is not None else None

    def line_of(self, call_id: int) -> str | None:
        c = self.current_call
        return c.acc.line_id if c is not None and c.getId() == call_id else None

    # ---- start --------------------------------------------------------------------------------
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
        s = self.cfg.sip
        if s.stun_server and not s.public_addr:
            ec.uaConfig.stunServer.append(s.stun_server)
        for ns in (s.nameservers or _system_resolvers()):
            ec.uaConfig.nameserver.append(ns)
        ep.libInit(ec)
        ep.audDevManager().setNullDev()
        pj.ai_phone_set_keepalive(s.tcp_keepalive_sec, s.tcp_keepalive_sec)
        self.transport_ids["udp"] = ep.transportCreate(pj.PJSIP_TRANSPORT_UDP, self._tc(s.bind.udp_port))
        self.transport_ids["tcp"] = ep.transportCreate(pj.PJSIP_TRANSPORT_TCP, self._tc(s.bind.tcp_port))
        if any(l.transport == "tls" for l in self.lines):
            tc = self._tc(s.bind.tls_port)
            import certifi
            tc.tlsConfig.CaListFile = s.tls.ca_file or certifi.where()
            tc.tlsConfig.verifyServer = True
            if s.tls.cert_file:
                tc.tlsConfig.certFile = s.tls.cert_file
                tc.tlsConfig.privKeyFile = s.tls.key_file
            self.transport_ids["tls"] = ep.transportCreate(pj.PJSIP_TRANSPORT_TLS, tc)
        ep.libStart()
        wanted = tuple(f"{c}/8000" for c in self.cfg.codecs)
        for ci in ep.codecEnum2():
            ep.codecSetPriority(ci.codecId, 255 if ci.codecId.startswith(wanted) else 0)
        self.port = RingMediaPort(self.rx, self.tx, echo=self.echo_mode)
        self.ep = ep
        for spec in self.lines:
            peers = self._peers_for(spec)
            acc = LineAccount(self, spec, peers)
            acc.create(self._account_config(spec))
            self.accounts[spec.id] = acc
            log.info("line %s: %s %s transport=%s policy=%s", spec.id, spec.kind, spec.number or spec.aor, spec.transport, spec.accept_invites_from)

    def _tc(self, port: int) -> "pj.TransportConfig":
        tc = pj.TransportConfig()
        tc.port = port
        tc.boundAddress = self.cfg.sip.bind.address
        if self.cfg.sip.public_addr:
            tc.publicAddress = self.cfg.sip.public_addr
        return tc

    def _peers_for(self, spec: LineSpec) -> PeerSet:
        if spec.accept_invites_from == "registered_trunk":
            entries = list(spec.trusted_cidrs) + ([spec.registrar_host] if spec.registrar_host else [])
        elif spec.accept_invites_from == "trusted":
            entries = list(spec.trusted_peers)
        else:
            entries = []
        peers, warnings = build_peer_set(entries)
        self.peer_warnings.extend(f"line {spec.id}: {w}" for w in warnings)
        return peers

    def _account_config(self, spec: LineSpec) -> "pj.AccountConfig":
        ac = pj.AccountConfig()
        ac.idUri = id_uri(spec)
        ac.sipConfig.transportId = self.transport_ids[spec.transport]
        if spec.kind == "trunk":
            ac.regConfig.registrarUri = spec.registrar
            ac.regConfig.timeoutSec = spec.register_expiry_sec
            ac.regConfig.retryIntervalSec = 30
            ac.regConfig.firstRetryIntervalSec = 5
            ac.regConfig.randomRetryIntervalSec = 10
            ac.regConfig.dropCallsOnFail = False
            ac.regConfig.unregWaitMsec = 1000
            ac.regConfig.registerOnAdd = True
            ac.regConfig.proxyUse = (pj.PJSUA_REG_USE_OUTBOUND_PROXY | pj.PJSUA_REG_USE_ACC_PROXY) if spec.register_via_proxy else 0
            ac.sipConfig.authCreds.append(pj.AuthCredInfo("digest", "*", spec.user, 0, spec.password or ""))
            if spec.outbound_proxy:
                ac.sipConfig.proxies.append(spec.outbound_proxy)
        else:
            ac.regConfig.registerOnAdd = False
        ac.natConfig.contactRewriteUse = 1
        ac.natConfig.viaRewriteUse = 1
        ac.natConfig.sdpNatRewriteUse = 1
        ac.natConfig.udpKaIntervalSec = 15 if spec.transport == "udp" else 0
        ac.natConfig.iceEnabled = False
        ac.natConfig.turnEnabled = False
        ac.mediaConfig.transportConfig.port = self.cfg.rtp.port_min
        ac.mediaConfig.transportConfig.portRange = self.cfg.rtp.port_max - self.cfg.rtp.port_min
        if self.cfg.sip.public_addr:
            ac.mediaConfig.transportConfig.publicAddress = self.cfg.sip.public_addr
        ac.mediaConfig.srtpUse, ac.mediaConfig.srtpSecureSignaling = srtp_values(spec)
        return ac

    # ---- call control -------------------------------------------------------------------------
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
            self.current_call = None
            for acc in self.accounts.values():
                try:
                    acc.shutdown()
                except Exception:
                    pass
            self.accounts = {}
            self.port = None
            self.ep.libDestroy()
            self.ep = None
