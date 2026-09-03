"""The control handler's failure paths, driven without PJSIP: a stub telephony object stands in for the endpoint."""
import asyncio

import pytest

from ai_phone.config import Config, LineConfig
from ai_phone.daemon import Daemon
from ai_phone.telephony.endpoint import BusyError, DialError, LineSpec


class StubTel:
    def __init__(self, exc):
        self.exc = exc
        self.current_call_id = None

    def make_call(self, line_id, to, timeout_sec):
        raise self.exc


def _ctx(tel):
    spec = LineSpec.from_config("agents", LineConfig(kind="sip", user="agents", aor="sip:agents@127.0.0.1", display_name="agents",
                                                     accept_invites_from="lan", caller_id="sip:agents@127.0.0.1"), None)
    return {"tel": tel, "specs": {"agents": spec}, "personas": {"default": object()}, "events": asyncio.Queue()}


@pytest.mark.asyncio
async def test_busy_while_an_inbound_call_is_active_replies_busy_and_keeps_the_call(tmp_path, monkeypatch):
    monkeypatch.setenv("AI_PHONE_HOME", str(tmp_path))
    d = Daemon(Config(), "t4", tmp_path, lan=True, fake=True)
    d.active_call = {"call_id": 3, "line_id": "agents", "remote": "sip:someone@10.0.0.2", "started_at": 1.0, "direction": "in"}
    sent = []

    async def send(m):
        sent.append(m)

    await d._control({"cmd": "call", "to": "sip:x@127.0.0.1", "line": "agents"}, send, _ctx(StubTel(BusyError("a call is already active"))))
    assert sent == [{"ok": False, "error": "busy"}]
    assert d.active_call["call_id"] == 3 and d._call_waiters == [] and d._outbound is None


@pytest.mark.asyncio
async def test_dial_error_ends_with_rejected_and_touches_no_state(tmp_path, monkeypatch):
    monkeypatch.setenv("AI_PHONE_HOME", str(tmp_path))
    d = Daemon(Config(), "t4", tmp_path, lan=True, fake=True)
    sent = []

    async def send(m):
        sent.append(m)

    await d._control({"cmd": "call", "to": "+441", "line": "agents"}, send, _ctx(StubTel(DialError("bad target"))))
    assert sent[-1]["event"] == "ended" and sent[-1]["reason"] == "rejected" and sent[-1]["exit_code"] == 7
    assert d.active_call is None and not (tmp_path / "state.json").exists()
