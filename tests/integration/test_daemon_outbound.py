import json
import os
import queue
import subprocess
import threading
import time
from pathlib import Path

import pytest

from ai_phone.cli import main

pytestmark = pytest.mark.integration


from tests.integration.test_daemon_smoke import _start_daemon, _stop_daemon  # shared helpers
from tests.integration.ports import PEER_PORT, SIP_PORT  # noqa: E402


CFG = ('lines:\n  agents:\n    kind: sip\n    user: agents\n    aor: "sip:agents@127.0.0.1"\n    display_name: agents\n'
       '    accept_invites_from: lan\n    caller_id: "sip:agents@127.0.0.1"\n'
       'personas:\n  default:\n    greeting: "Hi."\n    system_prompt: "s"\n    outbound_opening: "Hello, AI here."\n'
       'log:\n  dir: "%s"\n')


def test_outbound_call_through_control_socket(tmp_path: Path, monkeypatch):
    monkeypatch.setenv("AI_PHONE_HOME", str(tmp_path))
    d, lines = _start_daemon(tmp_path, CFG % tmp_path)
    from tests.integration.test_daemon_smoke import _tone
    play = tmp_path / "callee.wav"
    _tone(play, turns=1)  # the callee "speaks" once after answering, so the agent takes one outbound turn
    peer = subprocess.Popen(["pjsua", "--local-port", str(PEER_PORT), "--null-audio", "--auto-answer", "200", "--duration", "9",
                             "--play-file", str(play), "--auto-play",
                             "--dis-codec", "speex", "--dis-codec", "ilbc", "--dis-codec", "gsm", "--dis-codec", "g722", "--dis-codec", "opus",
                             "--app-log-level", "2", "--log-level", "2"], stdin=subprocess.PIPE, stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)
    try:
        time.sleep(1.0)
        rc = main(["call", f"sip:peer@127.0.0.1:{PEER_PORT}", "--line", "agents", "--timeout-sec", "10"])
        assert rc == 0
        recs = [json.loads(l) for l in (tmp_path / "turns.jsonl").read_text().splitlines()] if (tmp_path / "turns.jsonl").exists() else []
        state = json.loads((tmp_path / "state.json").read_text())
        assert state["active_call"] is None
        assert recs and all(r.get("direction") == "out" for r in recs), f"expected outbound turn records, got {recs}"
    finally:
        try:
            peer.stdin.write(b"q\n"); peer.stdin.flush(); peer.wait(timeout=10)
        except Exception:
            peer.terminate()
        _stop_daemon(d)


def test_outbound_busy_exit_code(tmp_path: Path, monkeypatch):
    monkeypatch.setenv("AI_PHONE_HOME", str(tmp_path))
    d, lines = _start_daemon(tmp_path, CFG % tmp_path)
    peer = subprocess.Popen(["pjsua", "--local-port", str(PEER_PORT), "--null-audio", "--auto-answer", "486", "--duration", "5",
                             "--app-log-level", "2", "--log-level", "2"], stdin=subprocess.PIPE, stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)
    try:
        time.sleep(1.0)
        assert main(["call", f"sip:peer@127.0.0.1:{PEER_PORT}", "--line", "agents", "--timeout-sec", "10"]) == 5
    finally:
        try:
            peer.stdin.write(b"q\n"); peer.stdin.flush(); peer.wait(timeout=10)
        except Exception:
            peer.terminate()
        _stop_daemon(d)


def test_busy_leaves_no_active_call_in_state(tmp_path: Path, monkeypatch):
    monkeypatch.setenv("AI_PHONE_HOME", str(tmp_path))
    d, lines = _start_daemon(tmp_path, CFG % tmp_path)
    peer = subprocess.Popen(["pjsua", "--local-port", str(PEER_PORT), "--null-audio", "--auto-answer", "486", "--duration", "5",
                             "--app-log-level", "2", "--log-level", "2"], stdin=subprocess.PIPE, stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)
    try:
        time.sleep(1.0)
        assert main(["call", f"sip:peer@127.0.0.1:{PEER_PORT}", "--line", "agents", "--timeout-sec", "10"]) == 5
        time.sleep(0.5)
        assert json.loads((tmp_path / "state.json").read_text())["active_call"] is None
    finally:
        try:
            peer.stdin.write(b"q\n"); peer.stdin.flush(); peer.wait(timeout=10)
        except Exception:
            peer.terminate()
        _stop_daemon(d)
