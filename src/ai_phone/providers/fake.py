"""In-memory carrier for tests: same step structure as the real ones, with crash and pending injection."""
from __future__ import annotations

import json
import os
from pathlib import Path

from ai_phone.lines.journal import Journal
from ai_phone.providers.base import Number, ProviderError, ProvisionResult, ReleaseResult, VerificationStep

STEPS = ("profile", "connection", "number")
OWNED = "+442079460099"  # the one number already on the fake account


class FakeProvider:
    name = "fake"
    signalling_cidrs = ["203.0.113.0/24"]

    def __init__(self, key: str = "", secret: str | None = None, crash_after: str | None = None, pending_polls: int = 0):
        self.crash_after, self.pending_polls = crash_after, pending_polls
        self.carrier: dict[str, dict] = {}      # kind -> {"id": …, "name": …}
        self.created: dict[str, int] = {}
        self.verified: set[str] = set()
        self._store = None
        if os.environ.get("AI_PHONE_HOME"):  # under the CLI the fake carrier persists between invocations, like a real one
            self._store = Path(os.environ["AI_PHONE_HOME"]) / "fake-carrier.json"
            if self._store.exists():
                d = json.loads(self._store.read_text())
                self.carrier, self.created = d.get("carrier", {}), d.get("created", {})

    def _persist(self) -> None:
        if self._store is not None:
            self._store.write_text(json.dumps({"carrier": self.carrier, "created": self.created}))

    def search_numbers(self, country, area, limit):
        return [Number(f"+4420794600{i:02d}", 1.0, "GBP", False, f"src-{i}") for i in range(limit)]

    def owned_numbers(self) -> list[Number]:
        return [Number(OWNED, None, None, False, "owned-1")]

    def _find(self, kind: str, name: str) -> dict | None:
        r = self.carrier.get(kind)
        return r if r and r["name"] == name else None

    def _create(self, kind: str, name: str) -> dict:
        self.created[kind] = self.created.get(kind, 0) + 1
        self.carrier[kind] = {"id": f"{kind}-{self.created[kind]}", "name": name}
        self._persist()
        return self.carrier[kind]

    def _step(self, journal: Journal, step: str, name: str) -> None:
        status, data = journal.start(step, name=name)
        if status == "done":
            return
        existing = self._find(step, name) if status == "started" else None   # reconcile after a crash
        rec = existing or self._create(step, name)
        if self.crash_after == step and existing is None:
            raise ProviderError(f"simulated crash after {step}")
        journal.finish(step, **{f"{step}_id": rec["id"]})

    def provision(self, journal: Journal, number: Number, line_id: str, sip_user: str, sip_password: str) -> ProvisionResult:
        for step in STEPS:
            self._step(journal, step, number.e164 if step == "number" else f"ai-phone:{line_id}")
        if self.pending_polls > 0:
            journal.set_state("pending")
            return ProvisionResult("pending", "order pending", self._line(journal, sip_user))
        journal.set_state("ready")
        return ProvisionResult("ready", "ready", self._line(journal, sip_user))

    def attach(self, journal: Journal, number: Number, line_id: str, sip_user: str, sip_password: str) -> ProvisionResult:
        st, _ = journal.start("claim", phone_number=number.e164)
        if st != "done":
            if number.e164 != OWNED:
                raise ProviderError(f"{number.e164} is not on this fake account")
            journal.finish("claim", number_id="owned-1", number_brought=True)
        for step in ("profile", "connection"):
            self._step(journal, step, f"ai-phone:{line_id}")
        self.carrier["owned"] = {"id": "owned-1", "name": number.e164, "connection": journal.resources()["connection_id"]}
        self._persist()
        journal.set_state("ready")
        return ProvisionResult("ready", "connected", self._line(journal, sip_user))

    def poll(self, journal: Journal) -> ProvisionResult:
        self.pending_polls -= 1
        if self.pending_polls > 0:
            return ProvisionResult("pending", "still pending", {})
        journal.set_state("ready")
        return ProvisionResult("ready", "ready", self._line(journal, None))

    def _line(self, journal: Journal, sip_user: str | None) -> dict:
        e164 = next((s["planned"]["name"] if s["name"] == "number" else s["planned"]["phone_number"]
                     for s in journal.doc["steps"] if s["name"] in ("number", "claim")), None)
        ids = {k: v for k, v in journal.resources().items() if k.endswith("_id")}  # carrier ids only, as the real providers do
        d = {"kind": "trunk", "number": e164, "caller_id": e164, "registrar": "sip:sip.fake.invalid", "dial_domain": "sip.fake.invalid",
             "transport": "udp", "register_via_proxy": True, "trusted_cidrs": list(self.signalling_cidrs),
             "provider": {"name": self.name, "state": journal.state, "resources": ids}}
        if sip_user:
            d["user"] = sip_user
        return d

    def release(self, journal: Journal) -> ReleaseResult:
        brought = journal.resources().get("number_brought")
        for step in reversed(STEPS):
            if step != "number" or not brought:  # a brought number is only disconnected, never released
                self.carrier.pop(step, None)
        if brought and "owned" in self.carrier:
            self.carrier["owned"]["connection"] = None
        self._persist()
        journal.set_state("pending")
        return ReleaseResult(True, {})

    def start_caller_id_verification(self, e164: str) -> VerificationStep:
        return VerificationStep("enter_code_in_cli", f"Enter the 6-digit code sent to {e164}", {"e164": e164})

    def complete_caller_id_verification(self, step: VerificationStep, code: str | None) -> bool:
        ok = code == "123456"
        if ok:
            self.verified.add(step.handle["e164"])
        return ok
