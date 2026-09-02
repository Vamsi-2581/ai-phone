"""Events the telephony layer posts to the pipeline's asyncio queue (never PJSIP objects)."""
from dataclasses import dataclass


@dataclass(frozen=True)
class IncomingCall:
    call_id: int
    from_uri: str
    source_ip: str


@dataclass(frozen=True)
class CallStateChanged:
    call_id: int
    state: str  # "ringing" | "active" | "ended"


@dataclass(frozen=True)
class MediaActive:
    call_id: int


@dataclass(frozen=True)
class Dtmf:
    call_id: int
    digit: str


@dataclass(frozen=True)
class RegState:
    active: bool
    code: int
    reason: str
