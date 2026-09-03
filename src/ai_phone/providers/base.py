"""What a carrier module must implement. Providers run only inside CLI commands, never in the daemon."""
from __future__ import annotations

from dataclasses import dataclass, field
from typing import Literal, Protocol

from ai_phone.lines.journal import Journal


@dataclass
class Number:
    e164: str
    monthly_cost: float | None
    currency: str | None
    requires_regulatory: bool
    source_id: str
    country: str = ""  # ISO 3166-1 alpha-2 of the number, when the carrier says


@dataclass
class ProvisionResult:
    state: Literal["pending", "ready", "failed"]
    message: str
    line: dict = field(default_factory=dict)   # config fields for the line (registrar, dial_domain, user, trusted_cidrs, provider…)


@dataclass
class ReleaseResult:
    ok: bool
    leftover: dict = field(default_factory=dict)


@dataclass
class VerificationStep:
    kind: Literal["enter_code_in_cli", "enter_code_on_phone"]
    instructions: str
    handle: dict = field(default_factory=dict)


class ProviderError(RuntimeError):
    def __init__(self, message: str, retryable: bool = False, status: int | None = None):
        super().__init__(message)
        self.retryable = retryable
        self.status = status  # HTTP status when the carrier answered, else None


class Provider(Protocol):
    name: str
    signalling_cidrs: list[str]

    def search_numbers(self, country: str, area: str | None, limit: int) -> list[Number]: ...
    def provision(self, journal: Journal, number: Number, line_id: str, sip_user: str, sip_password: str) -> ProvisionResult: ...
    def poll(self, journal: Journal) -> ProvisionResult: ...
    def release(self, journal: Journal) -> ReleaseResult: ...
    def start_caller_id_verification(self, e164: str) -> VerificationStep: ...
    def complete_caller_id_verification(self, step: VerificationStep, code: str | None) -> bool: ...
