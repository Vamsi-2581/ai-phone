"""A real SIP CANCEL must not kill the daemon or consume a relay listener.

Only the child daemon's first answer is delayed. PJSIP still processes a real
INVITE/CANCEL on its worker thread; speech and language models are fakes.
"""
from contextlib import ExitStack
import json
import os
from pathlib import Path
import select
import shutil
import signal
import socket
import subprocess
import sys
import time
import uuid

import pytest

from ai_phone import paths
from ai_phone.control.client import request
from tests.integration.ports import PEER_PORT, SIP_PORT, rtp_range
from tests.integration.test_agent_modes import CODECS, LINE, home
from tests.integration.test_daemon_smoke import _tone

pytestmark = pytest.mark.integration
ROOT = Path(__file__).resolve().parents[2]

# This is a file, rather than python -c, so multiprocessing's spawn can import
# its main module without installing the patch in the model workers.
CHILD = '''\
import os
import resource
import sys
import time


def main():
    resource.setrlimit(resource.RLIMIT_CORE, (0, 0))
    if sys.argv[1] == "peer":
        os.execvp(sys.argv[2], sys.argv[2:])
    from ai_phone.telephony.call import AiCall
    from ai_phone.telephony.endpoint import Telephony
    from ai_phone.telephony.pj import pj
    armed = True

    def wait_for_cancel(ended):
        print("CANCEL_TEST: waiting for native disconnect", flush=True)
        deadline = time.monotonic() + 8
        while not ended():
            if time.monotonic() >= deadline:
                raise RuntimeError("CANCEL_TEST: native disconnect never arrived")
            time.sleep(0.005)  # main thread only; the SIP worker remains free
        print("CANCEL_TEST: native disconnect observed", flush=True)

    if sys.argv[1] == "before-answer":
        original = Telephony.answer

        def answer(self, call_id):
            nonlocal armed
            if armed:
                armed = False
                wait_for_cancel(lambda: self.current_call is not None and self.current_call.ended)
            return original(self, call_id)

        Telephony.answer = answer
    else:
        original = AiCall.answer

        def answer(self, op):
            nonlocal armed
            if armed and op.statusCode == 200:
                armed = False
                wait_for_cancel(lambda: self.ended)
            try:
                return original(self, op)
            except pj.Error as error:
                print("CANCEL_TEST: native answer error " + str(error.status), flush=True)
                raise

        AiCall.answer = answer
    from ai_phone.cli import main as run
    return run(["run", "--config", sys.argv[2], "--lan", "--fake"])


if __name__ == "__main__":
    raise SystemExit(main())
'''


def _wait(predicate, timeout=10):
    deadline = time.monotonic() + timeout
    while time.monotonic() < deadline:
        value = predicate()
        if value:
            return value
        time.sleep(0.025)
    raise AssertionError(f"condition not reached after {timeout}s")


def _group_exists(pgid):
    try:
        os.killpg(pgid, 0)
        return True
    except ProcessLookupError:
        return False


def _cleanup(proc):
    # The group's original ID survives a native abort of its leader. getpgid(pid)
    # cannot find it once that leader has been reaped, leaving workers behind.
    for sig in (signal.SIGTERM, signal.SIGKILL):
        try:
            os.killpg(proc.pid, sig)
        except ProcessLookupError:
            break
        deadline = time.monotonic() + 5
        while time.monotonic() < deadline:
            proc.poll()  # reap the leader, including if it exited before cleanup
            if not _group_exists(proc.pid):
                break
            time.sleep(0.025)
        if not _group_exists(proc.pid):
            break
    proc.wait(timeout=5)
    assert not _group_exists(proc.pid), f"test process group {proc.pid} survived cleanup"
    if proc.stdin is not None:
        proc.stdin.close()


def _cancel_incoming(wait_for_answer, wire_log):
    """Cancel after 180, then ACK the INVITE's 487; no peer-log buffering race."""
    with socket.socket(socket.AF_INET, socket.SOCK_DGRAM) as peer:
        peer.bind(("127.0.0.1", 0))
        port = peer.getsockname()[1]
        token = uuid.uuid4().hex
        target = f"sip:agents@127.0.0.1:{SIP_PORT}"
        via = f"Via: SIP/2.0/UDP 127.0.0.1:{port};branch=z9hG4bK{token};rport"
        sender = f"From: <sip:cancel-test@127.0.0.1>;tag={token}"
        recipient = f"To: <{target}>"
        call_id = f"Call-ID: {token}@127.0.0.1"
        body = ("v=0\r\no=cancel-test 1 1 IN IP4 127.0.0.1\r\ns=Cancellation test\r\n"
                "c=IN IP4 127.0.0.1\r\nt=0 0\r\n"
                f"m=audio {rtp_range(PEER_PORT)[0]} RTP/AVP 0\r\n"
                "a=rtpmap:0 PCMU/8000\r\na=sendrecv\r\n")

        def send(method, to=recipient, content=""):
            headers = [f"{method} {target} SIP/2.0", via, "Max-Forwards: 70", sender,
                       to, call_id, f"CSeq: 1 {method}",
                       f"Contact: <sip:cancel-test@127.0.0.1:{port}>"]
            if content:
                headers.append("Content-Type: application/sdp")
            packet = "\r\n".join([*headers, f"Content-Length: {len(content.encode())}", "", ""]) + content
            wire_log.write("SENT " + packet + "\n")
            wire_log.flush()
            peer.sendto(packet.encode(), ("127.0.0.1", SIP_PORT))

        def receive(deadline):
            assert time.monotonic() < deadline, "SIP response deadline expired"
            peer.settimeout(max(0.01, deadline - time.monotonic()))
            packet = peer.recv(65535).decode("utf-8", errors="replace")
            wire_log.write("RECEIVED " + packet + "\n")
            wire_log.flush()
            lines = packet.partition("\r\n\r\n")[0].splitlines()
            headers = dict(line.split(":", 1) for line in lines[1:] if ":" in line)
            headers = {key.lower(): value.strip() for key, value in headers.items()}
            assert headers["call-id"] == f"{token}@127.0.0.1"
            return int(lines[0].split()[1]), headers

        send("INVITE", content=body)
        deadline = time.monotonic() + 5
        while True:
            code, headers = receive(deadline)
            assert code < 200, f"call ended before the cancellation: {code}"
            if code == 180:
                break
        wait_for_answer()
        send("CANCEL")
        finals = set()
        deadline = time.monotonic() + 5
        while len(finals) < 2:
            code, headers = receive(deadline)
            if code < 200:
                continue
            method = headers["cseq"].split()[1]
            assert (method, code) in {("CANCEL", 200), ("INVITE", 487)}
            finals.add(method)
            if method == "INVITE":
                send("ACK", to="To: " + headers["to"])


@pytest.mark.parametrize("delay_at", ["before-answer", "native-answer"])
def test_cancelled_incoming_call_preserves_listener_and_answers_next_call(tmp_path, home, monkeypatch, delay_at):
    assert 5060 not in range(SIP_PORT, PEER_PORT + 1), "Set AI_PHONE_TEST_SIP_PORT=5080"
    pjsua = shutil.which("pjsua")
    if pjsua is None:
        pytest.skip("pjsua is required for the real SIP/RTP follow-up call")
    for port in (SIP_PORT, PEER_PORT):
        for kind in (socket.SOCK_DGRAM, socket.SOCK_STREAM):
            with socket.socket(socket.AF_INET, kind) as probe:
                if kind == socket.SOCK_STREAM:
                    probe.setsockopt(socket.SOL_SOCKET, socket.SO_REUSEADDR, 1)
                probe.bind(("127.0.0.1", port))

    monkeypatch.setenv("AI_PHONE_HOME", str(home))
    env = {**os.environ, "PYTHONUNBUFFERED": "1", "HF_HUB_OFFLINE": "1", "TRANSFORMERS_OFFLINE": "1"}
    low, high = rtp_range(SIP_PORT)
    config = tmp_path / "config.yaml"
    config.write_text(
        f"sip:\n  bind: {{address: '127.0.0.1', udp_port: {SIP_PORT}, tcp_port: {SIP_PORT}, tls_port: {SIP_PORT + 1}}}\n"
        f"rtp: {{port_min: {low}, port_max: {high}}}\n" + LINE.format(id="agents") +
        "inbound:\n  greeting: 'Hi.'\n  instructions: 'Take a message.'\n"
        f"log:\n  dir: '{tmp_path}'\n  pjsip_level: 5\n")
    config.chmod(0o600)
    runner = tmp_path / "run_cancel_case.py"
    runner.write_text(CHILD)
    processes = []
    daemon_log = tmp_path / "daemon.log"
    with ExitStack() as stack:
        def start(args, log):
            proc = subprocess.Popen([sys.executable, str(runner), *args], cwd=ROOT, env=env,
                                    stdin=subprocess.PIPE, stdout=stack.enter_context(log.open("w")),
                                    stderr=subprocess.STDOUT, start_new_session=True)
            processes.append(proc)
            return proc

        daemon = start([delay_at, str(config)], daemon_log)

        def alive():
            assert daemon.poll() is None, f"daemon exited {daemon.returncode}"

        def control(message, timeout=5):
            alive()
            return request(paths.socket_path(), message, timeout=timeout)

        try:
            _wait(lambda: (alive() is None) and "\nready\n" in daemon_log.read_text(), timeout=60)
            with socket.socket(socket.AF_UNIX) as listener:
                listener.connect(str(paths.socket_path()))
                listener.sendall(b'{"cmd":"listen","timeout_sec":30}\n')
                _wait(lambda: control({"cmd": "status"})["relay_listener"])
                with (tmp_path / "cancel-sip.log").open("w") as wire_log:
                    _cancel_incoming(
                        lambda: _wait(lambda: "CANCEL_TEST: waiting" in daemon_log.read_text()), wire_log)
                _wait(lambda: "CANCEL_TEST: native disconnect observed" in daemon_log.read_text())
                _wait(lambda: control({"cmd": "status"})["active_call"] is None)
                state = control({"cmd": "status"})
                assert state["call"] is None and state["relay_listener"] is True, state
                assert not select.select([listener], [], [], 0.1)[0], "cancelled call consumed the waiting listener"
                native_error = "CANCEL_TEST: native answer error 171140" in daemon_log.read_text()
                assert native_error == (delay_at == "native-answer")
            _wait(lambda: not control({"cmd": "status"})["relay_listener"])

            play = tmp_path / "caller.wav"
            _tone(play, lead_silence=1.0, tail_silence=30)
            peer = start(["peer", pjsua, "--local-port", str(PEER_PORT), "--bound-addr", "127.0.0.1",
                          "--ip-addr", "127.0.0.1", "--rtp-port", str(rtp_range(PEER_PORT)[0]),
                          "--null-audio", "--play-file", str(play), "--auto-play", *CODECS,
                          f"sip:agents@127.0.0.1:{SIP_PORT}"], tmp_path / "next-call.log")
            turns_path = tmp_path / "turns.jsonl"

            def completed_turn():
                alive()
                if not turns_path.exists():
                    return None
                rows = [json.loads(line) for line in turns_path.read_text().splitlines(keepends=True)
                        if line.endswith("\n") and line.strip()]
                return next((row for row in rows if not row.get("incomplete")), None)

            turn = _wait(completed_turn, timeout=20)
            assert turn["stt_text"].startswith("fake utterance"), turn
            assert turn["reply_text"] == "Hello, this is a fake reply.", turn
            assert turn["first_audible_ms"] is not None and turn["direction"] == "in", turn
            ended = control({"cmd": "hangup"}, timeout=15)
            assert ended.get("ok") and ended["result"]["outcome"] == "completed", ended
            _wait(lambda: control({"cmd": "status"})["active_call"] is None)
            time.sleep(0.2)  # peer receives BYE before quitting its WAV player
            peer.stdin.write(b"q\n")
            peer.stdin.flush()
            assert peer.wait(timeout=10) == 0
            daemon.send_signal(signal.SIGTERM)
            assert daemon.wait(timeout=15) == 0
            output = daemon_log.read_text()
            assert "Assertion failed" not in output and "Traceback (most recent call last)" not in output
        except Exception as error:
            error.add_note(f"SIP regression logs: {tmp_path}\n" + daemon_log.read_text()[-5000:])
            raise
        finally:
            failure = sys.exception()
            cleanup_errors = []
            for proc in reversed(processes):
                try:
                    _cleanup(proc)
                except Exception as error:
                    cleanup_errors.append(str(error))
            if cleanup_errors:
                detail = "Test process cleanup failed: " + "; ".join(cleanup_errors)
                if failure is not None:
                    failure.add_note(detail)
                else:
                    raise AssertionError(detail)
