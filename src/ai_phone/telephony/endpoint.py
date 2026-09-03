"""PJSIP endpoint lifecycle: transports from sip.bind, null audio device, G.711-only codecs, one Account per line."""
from __future__ import annotations

import asyncio
import logging
import threading
from dataclasses import dataclass, field

from ai_phone.config import AOR_RE, DISPLAY_RE, E164_RE, SIP_URI_RE, SIP_USER_RE, USER_RE, Config, LineConfig, host_of
from ai_phone.telephony import events as ev
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
        """Builds the spec and re-checks every part that ends up inside a SIP header, so a LineConfig built in code
        (not through load_config) can never smuggle quotes or delimiters into a name-addr."""
        _grammar(c)
        return cls(id=line_id, kind=c.kind, user=c.user, password=password, display_name=c.display_name, number=c.number,
                   aor=c.aor, registrar=c.registrar, outbound_proxy=c.outbound_proxy, dial_domain=c.dial_domain,
                   register_via_proxy=c.register_via_proxy, transport=c.transport, register_expiry_sec=c.register_expiry_sec,
                   srtp=c.srtp, accept_invites_from=c.accept_invites_from, trusted_cidrs=list(c.trusted_cidrs),
                   trusted_peers=list(c.trusted_peers), caller_id=c.caller_id,
                   verified_caller_ids=[v.number for v in c.verified_caller_ids], persona=c.persona)


def _grammar(c: LineConfig) -> None:
    checks = [(DISPLAY_RE, c.display_name, "display_name")]
    if c.kind == "trunk":
        checks += [(USER_RE, c.user, "user"), (SIP_URI_RE, c.registrar or "", "registrar")]
        if c.number is not None:
            checks.append((E164_RE, c.number, "number"))
        if c.caller_id is not None:
            checks.append((E164_RE, c.caller_id, "caller_id"))
        if c.outbound_proxy is not None:
            checks.append((SIP_URI_RE, c.outbound_proxy, "outbound_proxy"))
    else:
        checks += [(SIP_USER_RE, c.user, "user"), (AOR_RE, c.aor or "", "aor")]
        if c.caller_id is not None:
            checks.append((AOR_RE, c.caller_id, "caller_id"))
    for rx, value, name in checks:
        if not rx.fullmatch(value):  # fullmatch: `$` alone would accept a trailing newline
            raise ValueError(f"line {name} {value!r} does not match {rx.pattern}")


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


class BusyError(RuntimeError):
    pass


class DialError(ValueError):
    pass


def request_uri(spec: LineSpec, target: str) -> str:
    if target.startswith(("sip:", "sips:")):
        return target
    if not E164_RE.match(target):
        raise DialError(f"target {target!r} is neither a sip: URI nor an E.164 number like +447700900123")
    if spec.kind != "trunk" or not spec.dial_domain:
        raise DialError(f"line {spec.id} cannot dial phone numbers (no dial_domain)")
    return f"sip:{target}@{spec.dial_domain}"


def check_caller_id(spec: LineSpec) -> None:
    if spec.kind != "trunk":
        return
    cid = spec.caller_id or spec.number
    if cid is None:
        raise DialError(f"line {spec.id} has neither a number nor a caller_id; nothing can be presented as From")
    if cid != spec.number and cid not in spec.verified_caller_ids:
        raise DialError(f"caller_id {cid} is neither line {spec.id}'s number nor a verified caller id (run `ai-phone lines verify-caller-id`)")


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
        self._call_lock = threading.Lock()  # make_call (asyncio thread) and onIncomingCall (PJSIP thread) both claim current_call

    @property
    def current_call_id(self) -> int | None:
        return self.current_call.getId() if self.current_call is not None else None

    def try_claim_call(self, call: AiCall) -> bool:
        """The one-call-at-a-time gate, safe across the asyncio and PJSIP threads."""
        with self._call_lock:
            if self.current_call is not None:
                return False
            self.current_call = call
            return True

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

    def make_call(self, line_id: str, target: str, timeout_sec: float = 30) -> int:
        acc = self.accounts[line_id]
        spec = acc.spec
        check_caller_id(spec)
        uri = request_uri(spec, target)
        op = pj.CallOpParam(True)
        op.txOption.localUri = local_uri(spec)
        call = AiCall(acc, pj.PJSUA_INVALID_ID, "out")
        if not self.try_claim_call(call):
            raise BusyError("a call is already active")
        # "dialling" goes on the queue before makeCall, so no PJSIP callback can queue "ringing" ahead of it
        self.events.put_nowait(ev.OutboundProgress(-1, line_id, "dialling", 0, uri))
        try:
            call.makeCall(uri, op)
        except Exception as e:
            with self._call_lock:
                self.current_call = None
            raise DialError(f"could not place the call: {e}") from e
        cid = call.getId()
        call.answer_timeout = self.loop.call_later(timeout_sec, self._cancel_if_unanswered, call)
        log.info("line %s: dialling %s", line_id, uri)
        return cid

    def _cancel_if_unanswered(self, call: AiCall) -> None:
        call.answer_timeout = None
        if self.current_call is call and not call.answered:
            call.cancelled = True
            try:
                call.hangup(pj.CallOpParam())  # early dialog: PJSIP sends CANCEL; the disconnect arrives as 487
            except Exception:
                pass

    @staticmethod
    def _drop_answer_timeout(call: AiCall) -> None:
        """A pending timer would keep the pjsua2 Call alive past libDestroy, which PJSIP aborts on. Per call, so a
        new call's timer is never cancelled by an old call's cleanup."""
        handle = getattr(call, "answer_timeout", None)
        if handle is not None:
            handle.cancel()
            call.answer_timeout = None

    def _call_ended(self, call: AiCall) -> None:
        with self._call_lock:
            if self.current_call is call:
                self.current_call = None
        self.loop.call_soon_threadsafe(self._drop_answer_timeout, call)

    def stop(self) -> None:
        if self.current_call is not None:
            self._drop_answer_timeout(self.current_call)
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
