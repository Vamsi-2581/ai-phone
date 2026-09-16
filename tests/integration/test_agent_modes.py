"""The agent tool end to end on one machine: fake speech models, pjsua peers or a second daemon, the real CLI.

Fixtures start every process (daemons, pjsua peers, the stub brain) and stop it at teardown. Each one is registered the
moment it exists, so a failure part-way through setup never leaves one running with the suite's ports."""
import json
import queue
import shutil
import subprocess
import tempfile
import threading
import time
from http.server import BaseHTTPRequestHandler, HTTPServer
from pathlib import Path

import pytest

from ai_phone import paths
from ai_phone.cli import main
from ai_phone.control.client import DaemonGone, request
from tests.integration.ports import PEER_PORT, SECOND_SIP_PORT, SIP_PORT
from tests.integration.test_daemon_smoke import _start_daemon, _stop_daemon, _tone

pytestmark = pytest.mark.integration

LINE = ('lines:\n  {id}:\n    kind: sip\n    user: {id}\n    aor: "sip:{id}@127.0.0.1"\n    display_name: {id}\n'
        '    accept_invites_from: lan\n    caller_id: "sip:{id}@127.0.0.1"\n')
CFG = (LINE.format(id="agents") +
       'inbound:\n  greeting: "Hi, you reached the test agent."\n  instructions: "Take a message."\n'
       'calls:\n  relay_filler_after_ms: 400\n  relay_second_filler_sec: 2\n  relay_reply_timeout_sec: {timeout}\n'
       'log:\n  dir: "{log}"\n')
PEER = f"sip:peer@127.0.0.1:{PEER_PORT}"
CODECS = ["--dis-codec", "speex", "--dis-codec", "ilbc", "--dis-codec", "gsm", "--dis-codec", "g722", "--dis-codec", "opus"]
DISCLOSURE = "Hi, this is an AI assistant calling."


class Daemon:
    """A running daemon and what it has printed, for failure messages."""

    def __init__(self, name: str, proc: subprocess.Popen, lines: queue.Queue):
        self.name, self.proc, self._lines, self._seen = name, proc, lines, []

    def tail(self, chars: int = 3000) -> str:
        while True:
            try:
                self._seen.append(self._lines.get_nowait())
            except queue.Empty:
                break
        return f"--- output tail of the daemon {self.name} ---\n" + "".join(self._seen)[-chars:]


def why(reply, *daemons: Daemon) -> str:
    """An assertion message: the reply that failed the check, then each daemon's output tail."""
    return "\n".join([f"reply: {json.dumps(reply)}"] + [d.tail() for d in daemons])


@pytest.fixture
def home():
    """AF_UNIX socket paths are capped near 100 bytes on macOS; pytest's tmp_path can be longer."""
    d = Path(tempfile.mkdtemp(prefix="aip-", dir="/private/tmp"))
    yield d
    shutil.rmtree(d, ignore_errors=True)


@pytest.fixture
def daemons(home):
    """start(home, config, port) runs a daemon; every daemon started is stopped at teardown, before `home` goes."""
    started: list[Daemon] = []

    def start(daemon_home: Path, cfg_text: str, port: int = SIP_PORT) -> Daemon:
        proc, lines = _start_daemon(daemon_home, cfg_text, port=port)  # a daemon that never gets ready is killed there
        d = Daemon(f"on port {port}", proc, lines)
        started.append(d)
        return d

    yield start
    for d in reversed(started):
        _stop_daemon(d.proc)


@pytest.fixture
def peers(daemons):
    """start(argv) runs a pjsua process; every one is stopped at teardown, before the daemons (it depends on them)."""
    started: list[subprocess.Popen] = []

    def start(argv: list[str]) -> subprocess.Popen:
        p = subprocess.Popen(argv, stdin=subprocess.PIPE, stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)
        started.append(p)
        return p

    yield start
    for p in reversed(started):
        stop_peer(p)


def answering_peer(play: Path, duration: int) -> list[str]:
    return ["pjsua", "--local-port", str(PEER_PORT), "--null-audio", "--auto-answer", "200",
            "--duration", str(duration), "--play-file", str(play), "--auto-play", *CODECS,
            "--app-log-level", "2", "--log-level", "2"]


def stop_peer(peer):
    try:
        peer.stdin.write(b"q\n")
        peer.stdin.flush()
        peer.wait(timeout=10)
    except Exception:
        peer.terminate()
        try:
            peer.wait(timeout=5)
        except Exception:
            peer.kill()


def cli_json(capsys, argv):
    rc = main(argv + ["--json"])
    out = capsys.readouterr().out.strip().splitlines()
    return rc, (json.loads(out[-1]) if out else None)


def wait_for_listener(d: Daemon, timeout: float = 15.0) -> None:
    """Until the daemon reports a relay listener waiting (an incoming call before that would get phone agent mode)."""
    deadline = time.monotonic() + timeout
    reply = None
    while time.monotonic() < deadline:
        try:
            reply = request(paths.socket_path(), {"cmd": "status"}, timeout=5)
        except DaemonGone as e:
            reply = {"error": str(e)}
        if reply.get("relay_listener"):
            return
        time.sleep(0.1)
    raise AssertionError(f"no relay listener after {timeout:.0f} s\n" + why(reply, d))


class ScriptedBrain(BaseHTTPRequestHandler):
    """An OpenAI-compatible stub: two ordinary replies, then a goodbye with the end marker."""

    def do_POST(self):
        body = json.loads(self.rfile.read(int(self.headers.get("content-length", 0))) or b"{}")
        users = sum(1 for m in body.get("messages", []) if m.get("role") == "user")
        reply = {1: ["Hi", ", I am calling", " about your hours", "."],
                 2: ["Could you", " tell me", " when you open", "?"]}.get(users, ["Thanks", ", goodbye", ".", " [", "END", "]"])
        self.send_response(200)
        self.send_header("content-type", "text/event-stream")
        self.end_headers()
        for tok in reply:
            self.wfile.write(f"data: {json.dumps({'choices': [{'delta': {'content': tok}}]})}\n\n".encode())
            self.wfile.flush()
        self.wfile.write(b"data: [DONE]\n\n")

    def log_message(self, *a):
        pass


@pytest.fixture
def brain():
    """The stub brain's port. The server is shut down and its socket closed at teardown."""
    srv = HTTPServer(("127.0.0.1", 0), ScriptedBrain)
    serving = threading.Thread(target=srv.serve_forever, daemon=True)
    try:
        serving.start()
        yield srv.server_address[1]
    finally:
        if serving.is_alive():
            srv.shutdown()
        srv.server_close()


def test_phone_agent_call_between_two_daemons_ends_itself(brain, daemons, home, monkeypatch, capsys):
    a_home, b_home = home / "a", home / "b"
    a_home.mkdir()
    b_home.mkdir()
    a_cfg = (LINE.format(id="caller") +
             f'brain:\n  kind: openai_compatible\n  base_url: "http://127.0.0.1:{brain}/v1"\n  model: test\n'
             f'conversation:\n  max_call_sec: 60\nlog:\n  dir: "{a_home}"\n')
    a = daemons(a_home, a_cfg)
    b = daemons(b_home, CFG.format(timeout=25, log=b_home), port=SECOND_SIP_PORT)
    monkeypatch.setenv("AI_PHONE_HOME", str(a_home))
    rc, r = cli_json(capsys, ["call", f"sip:agents@127.0.0.1:{SECOND_SIP_PORT}", "--line", "caller",
                              "--prompt", "Ask when they open."])
    assert rc == 0 and r["mode"] == "agent" and r["ended_by"] == "agent", why(r, a, b)
    agent = [t["text"] for t in r["transcript"] if t["who"] == "agent"]
    them = [t["text"] for t in r["transcript"] if t["who"] == "them"]
    assert agent[0] == DISCLOSURE and agent[-1] == "Thanks, goodbye.", why(r, a, b)
    # the goodbye answers the 3rd user message and the 1st is the opening cue, so A heard B at least twice
    assert len(them) >= 2 and all(t.startswith("fake utterance") for t in them), why(r, a, b)
    b_turns = [json.loads(l) for l in (b_home / "turns.jsonl").read_text().splitlines() if l.strip()]
    assert b_turns and all(t["direction"] == "in" and t["mode"] == "agent" for t in b_turns), why(b_turns, b)


def test_relay_outbound_session(daemons, peers, home, monkeypatch, capsys):
    monkeypatch.setenv("AI_PHONE_HOME", str(home))
    d = daemons(home, CFG.format(timeout=20, log=home))
    play = home / "speech.wav"
    _tone(play, lead_silence=3.0, tail_silence=20)
    peers(answering_peer(play, 40))
    time.sleep(1.0)
    rc, r = cli_json(capsys, ["call", PEER, "--line", "agents", "--relay"])
    assert rc == 0 and r["mode"] == "relay", why(r, d)
    cid = r["call_id"]
    # a heard reply names no call; --call makes the daemon take the event from this call's queue
    rc, h = cli_json(capsys, ["listen", "--call", cid, "--timeout-sec", "20"])
    assert rc == 0 and h["kind"] == "heard" and h["text"].startswith("fake utterance"), why(h, d)
    rc, s = cli_json(capsys, ["say", "Thanks, noted."])
    assert rc == 0 and s["result"] == "said" and s["call_id"] == cid, why(s, d)
    rc, e = cli_json(capsys, ["hangup", "--say", "Bye now."])
    assert rc == 0 and e["call_id"] == cid and e["ended_by"] == "hangup_command", why(e, d)
    who = [t["who"] for t in e["transcript"]]
    said = [t["text"] for t in e["transcript"] if t["who"] == "agent"]
    assert who[:2] == ["agent", "them"], why(e, d)
    assert said[0] == DISCLOSURE and "Thanks, noted." in said and "Bye now." in said, why(e, d)
    assert said.index("Thanks, noted.") < said.index("Bye now."), why(e, d)


def test_relay_inbound_session(daemons, peers, home, monkeypatch, capsys):
    monkeypatch.setenv("AI_PHONE_HOME", str(home))
    d = daemons(home, CFG.format(timeout=20, log=home))
    play, rec = home / "speech.wav", home / "rec.wav"
    _tone(play, lead_silence=3.0, tail_silence=8)
    got = {}
    waiter = threading.Thread(target=lambda: got.update(rc=main(["listen", "--timeout-sec", "30", "--json"])), daemon=True)
    waiter.start()
    wait_for_listener(d)
    peers(["bash", "scripts/pjsua_peer.sh", str(play), str(rec), "20", f"sip:agents@127.0.0.1:{SIP_PORT}"])
    waiter.join(50)
    assert not waiter.is_alive(), why("listen still waiting after 50 s", d)
    out = capsys.readouterr().out.strip().splitlines()
    incoming = json.loads(out[-1]) if out else None
    assert got.get("rc") == 0 and incoming is not None and incoming["kind"] == "incoming" and incoming["line"] == "agents", \
        why(incoming, d)
    rc, h = cli_json(capsys, ["listen", "--timeout-sec", "20"])
    assert rc == 0 and h["kind"] == "heard" and h["text"].startswith("fake utterance"), why(h, d)
    rc, s = cli_json(capsys, ["say", "Got it, thank you."])
    assert rc == 0 and s["result"] == "said", why(s, d)
    rc, e = cli_json(capsys, ["listen", "--timeout-sec", "30"])   # the peer hangs up when its file ends
    assert rc == 0 and e["kind"] == "ended" and e["ended_by"] == "them", why(e, d)
    assert e["transcript"][0]["text"] == "Hi, you reached the test agent.", why(e, d)
    assert "Got it, thank you." in [t["text"] for t in e["transcript"] if t["who"] == "agent"], why(e, d)


def test_relay_fillers_then_timeout(daemons, peers, home, monkeypatch, capsys):
    monkeypatch.setenv("AI_PHONE_HOME", str(home))
    d = daemons(home, CFG.format(timeout=4, log=home))
    play = home / "speech.wav"
    _tone(play, lead_silence=4.0, tail_silence=20)   # the first say must start before the peer's tone does
    peers(answering_peer(play, 40))
    time.sleep(1.0)
    rc, r = cli_json(capsys, ["call", PEER, "--line", "agents", "--relay"])
    assert rc == 0, why(r, d)
    rc, s = cli_json(capsys, ["say", "Hello, a quick question."])   # arms the fillers (none before the first say)
    assert rc == 0, why(s, d)
    rc, h = cli_json(capsys, ["listen", "--timeout-sec", "20"])
    assert rc == 0 and h["kind"] == "heard", why(h, d)
    rc, e = cli_json(capsys, ["listen", "--timeout-sec", "20"])      # and never reply
    assert rc == 0 and e["kind"] == "ended" and e["ended_by"] == "timeout", why(e, d)
    said = [t["text"] for t in e["transcript"] if t["who"] == "agent"]
    assert said[-3:] == ["One moment.", "Still with you, one moment.", "Sorry, I have to go. Goodbye."], why(e, d)


def test_emergency_numbers_and_the_daily_limit(daemons, peers, home, monkeypatch, capsys):
    monkeypatch.setenv("AI_PHONE_HOME", str(home))
    d = daemons(home, CFG.format(timeout=25, log=home).replace("calls:\n", "calls:\n  daily_limit: 1\n"))
    peers(["pjsua", "--local-port", str(PEER_PORT), "--null-audio", "--auto-answer", "486", "--duration", "10",
           "--app-log-level", "2", "--log-level", "2"])
    time.sleep(1.0)
    # the busy peer's port: if the block ever broke, the call would reach that peer, never whatever listens on 5060
    rc, r = cli_json(capsys, ["call", f"sip:911@127.0.0.1:{PEER_PORT}", "--line", "agents"])
    assert rc == 13, why(r, d)
    rc, r = cli_json(capsys, ["call", PEER, "--line", "agents"])
    assert rc == 5, why(r, d)   # busy, and it counts
    rc, r = cli_json(capsys, ["call", PEER, "--line", "agents"])
    assert rc == 14, why(r, d)
