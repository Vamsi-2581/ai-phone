"""Events the telephony layer posts to the pipeline's asyncio queue (never PJSIP objects). Every call/registration event names its line."""
from dataclasses import dataclass


@dataclass(frozen=True)
class IncomingCall:
    call_id: int
    line_id: str
    from_uri: str
    source_ip: str


@dataclass(frozen=True)
class CallStateChanged:
    call_id: int
    line_id: str
    state: str  # "ringing" | "active" | "ended"
    code: int = 0
    reason: str = ""


@dataclass(frozen=True)
class MediaActive:
    call_id: int


@dataclass(frozen=True)
class Dtmf:
    call_id: int
    digit: str


@dataclass(frozen=True)
class RegState:
    line_id: str
    active: bool
    code: int
    reason: str
    expires_sec: int = 0


@dataclass(frozen=True)
class CallRejected:
    call_id: int
    line_id: str
    source_ip: str
    code: int  # 403 Forbidden or 486 Busy Here


@dataclass(frozen=True)
class OutboundProgress:
    call_id: int
    line_id: str
    state: str  # dialling | ringing | answered | busy | no_answer | rejected | redirected
    code: int = 0
    reason: str = ""
