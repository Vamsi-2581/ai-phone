"""Call-direction policy through real loopback SIP and control sockets, with fake models."""
from concurrent.futures import ThreadPoolExecutor
import json
import socket
import sys
import time
import uuid

import pytest

from ai_phone import paths
from ai_phone.control.client import request
from tests.integration.ports import PEER_PORT, SIP_PORT, rtp_range
from tests.integration.test_agent_modes import (
    CFG,
    PEER,
    answering_peer,
    daemons,
    home,
    peers,
    wait_for_listener,
    why,
)
from tests.integration.test_daemon_smoke import _tone

pytestmark = pytest.mark.integration


@pytest.fixture(autouse=True)
def isolated_ports():
    assert 5060 not in range(SIP_PORT, PEER_PORT + 1), \
        "Set AI_PHONE_TEST_SIP_PORT=5080; never test against production port 5060"


def _config(home, *, inbound: bool, outbound: bool):
    return CFG.format(timeout=25, log=home).replace(
        "calls:\n",
        f"calls:\n  allow_inbound: {str(inbound).lower()}\n  allow_outbound: {str(outbound).lower()}\n",
    )


def _control(message, timeout=30):
    return request(paths.socket_path(), message, timeout=timeout)


def _hangup_active(d):
    """End a call before the peer fixture quits its active WAV player."""
    original_failure = sys.exception()
    try:
        active = _control({"cmd": "status"}, timeout=5)["active_call"]
        if active is not None:
            reply = _control({"cmd": "hangup", "call_id": active["call"]}, timeout=45)
            assert reply.get("ok"), why(reply, d)
        deadline = time.monotonic() + 5
        while time.monotonic() < deadline:
            if _control({"cmd": "status"}, timeout=5)["active_call"] is None:
                time.sleep(0.2)  # let the peer process the BYE before fixture teardown sends q
                return
            time.sleep(0.05)
        raise AssertionError("call did not release after hangup")
    except Exception as error:
        detail = why(f"call cleanup failed ({type(error).__name__}): {error}", d)
        if original_failure is not None:
            # A dead socket raises DaemonGone from None; do not let that hide the test's first failure.
            original_failure.add_note(detail)
            return
        raise AssertionError(detail) from error


def _peer_args(play):
    return answering_peer(play, 30) + [
        "--bound-addr", "127.0.0.1", "--ip-addr", "127.0.0.1",
        "--rtp-port", str(rtp_range(PEER_PORT)[0]),
    ]


def _invite_response(with_sdp=False):
    """Send an INVITE with or without a PCMU offer and acknowledge its final response."""
    with socket.socket(socket.AF_INET, socket.SOCK_DGRAM) as peer:
        peer.bind(("127.0.0.1", 0))
        port = peer.getsockname()[1]
        token = uuid.uuid4().hex
        target = f"sip:agents@127.0.0.1:{SIP_PORT}"
        via = f"Via: SIP/2.0/UDP 127.0.0.1:{port};branch=z9hG4bK{token};rport"
        sender = f"From: <sip:direction-test@127.0.0.1>;tag={token}"
        call_id = f"Call-ID: {token}@127.0.0.1"
        body = ("v=0\r\no=direction-test 1 1 IN IP4 127.0.0.1\r\ns=Call direction test\r\n"
                "c=IN IP4 127.0.0.1\r\nt=0 0\r\n"
                f"m=audio {rtp_range(PEER_PORT)[0]} RTP/AVP 0\r\n"
                "a=rtpmap:0 PCMU/8000\r\na=sendrecv\r\n") if with_sdp else ""
        headers = [via, "Max-Forwards: 70", sender, f"To: <{target}>", call_id,
                   "CSeq: 1 INVITE", f"Contact: <sip:direction-test@127.0.0.1:{port}>"]
        if with_sdp:
            headers.append("Content-Type: application/sdp")
        headers.extend([f"Content-Length: {len(body.encode())}", "", ""])
        peer.sendto((f"INVITE {target} SIP/2.0\r\n" + "\r\n".join(headers) + body).encode(),
                    ("127.0.0.1", SIP_PORT))
        deadline = time.monotonic() + 5
        while time.monotonic() < deadline:
            peer.settimeout(max(0.01, deadline - time.monotonic()))
            data, source = peer.recvfrom(65535)
            response = data.decode("utf-8", errors="replace")
            code = int(response.splitlines()[0].split()[1])
            if code < 200:
                continue
            to = next(line for line in response.splitlines() if line.lower().startswith("to:"))
            ack = [f"ACK {target} SIP/2.0", via, "Max-Forwards: 70", sender, to, call_id,
                   "CSeq: 1 ACK", "Content-Length: 0", "", ""]
            peer.sendto("\r\n".join(ack).encode(), source)
            return code
    raise AssertionError("No final SIP response to the loopback INVITE")


@pytest.mark.parametrize("with_sdp", [False, True], ids=["offerless", "pcmu-offer"])
def test_outbound_only_rejects_invites_and_keeps_outbound_relay_usable(daemons, peers, home, monkeypatch, with_sdp):
    monkeypatch.setenv("AI_PHONE_HOME", str(home))
    d = daemons(home, _config(home, inbound=False, outbound=True))
    counter = home / "calls-today.json"
    try:
        idle = _control({"cmd": "listen", "timeout_sec": 1})
        assert idle.get("ok") is False and idle.get("code") == 7, why(idle, d)
        assert "inbound calls are disabled" in idle["error"], why(idle, d)
        assert _invite_response(with_sdp=with_sdp) == 403
        state = _control({"cmd": "status"})
        assert state["active_call"] is None and state["call"] is None, why(state, d)
        assert state["relay_listener"] is False and not counter.exists(), why(state, d)

        play = home / "callee.wav"
        _tone(play, lead_silence=3.0, tail_silence=20)
        peers(_peer_args(play))
        time.sleep(1.0)
        answered = _control({"cmd": "call", "to": PEER, "line": "agents", "relay": True, "timeout_sec": 10})
        assert answered.get("ok") and answered.get("event") == "answered", why(answered, d)
        cid = answered["call_id"]
        # No call_id: disabling incoming calls must not disable listening on the active outbound relay.
        heard = _control({"cmd": "listen", "timeout_sec": 15})
        assert heard.get("kind") == "heard" and heard["text"].startswith("fake utterance"), why(heard, d)
        said = _control({"cmd": "say", "call_id": cid, "text": "Thanks, noted."})
        assert said.get("result") == "said", why(said, d)
        ended = _control({"cmd": "hangup", "call_id": cid}, timeout=45)
        assert ended.get("ok") and ended["result"]["direction"] == "out", why(ended, d)
        assert ended["result"]["ended_by"] == "hangup_command", why(ended, d)
        retained = _control({"cmd": "listen", "call_id": cid, "timeout_sec": 1})
        assert retained.get("kind") == "ended" and retained["result"]["call_id"] == cid, why(retained, d)
        assert json.loads(counter.read_text())["count"] == 1
    finally:
        _hangup_active(d)


def test_inbound_only_refuses_dialling_without_counting_and_accepts_a_relay_call(daemons, peers, home, monkeypatch):
    monkeypatch.setenv("AI_PHONE_HOME", str(home))
    d = daemons(home, _config(home, inbound=True, outbound=False))
    counter = home / "calls-today.json"
    try:
        # A sentinel on the target port proves refused agent and relay requests send no SIP INVITE.
        with socket.socket(socket.AF_INET, socket.SOCK_DGRAM) as sentinel:
            sentinel.bind(("127.0.0.1", PEER_PORT))
            for relay in (False, True):
                events = []
                refused = request(paths.socket_path(),
                                  {"cmd": "call", "to": PEER, "line": "agents", "relay": relay, "timeout_sec": 1},
                                  on_event=events.append, timeout=5)
                assert refused.get("ok") is False and refused.get("code") == 7, why(refused, d)
                assert "outbound calls are disabled" in refused["error"], why(refused, d)
                assert not events and not counter.exists(), why(events, d)
            sentinel.settimeout(0.2)
            with pytest.raises(socket.timeout):
                sentinel.recvfrom(65535)
        state = _control({"cmd": "status"})
        assert state["active_call"] is None and state["call"] is None, why(state, d)

        play = home / "caller.wav"
        _tone(play, lead_silence=3.0, tail_silence=20)
        with ThreadPoolExecutor(max_workers=1) as pool:
            listener = pool.submit(_control, {"cmd": "listen", "timeout_sec": 10}, 15)
            wait_for_listener(d)
            peers(_peer_args(play) + [f"sip:agents@127.0.0.1:{SIP_PORT}"])
            incoming = listener.result(timeout=15)
        assert incoming.get("kind") == "incoming" and incoming["line"] == "agents", why(incoming, d)
        cid = incoming["call_id"]
        heard = _control({"cmd": "listen", "call_id": cid, "timeout_sec": 15})
        assert heard.get("kind") == "heard" and heard["text"].startswith("fake utterance"), why(heard, d)
        ended = _control({"cmd": "hangup", "call_id": cid}, timeout=45)
        assert ended.get("ok") and ended["result"]["direction"] == "in", why(ended, d)
        assert ended["result"]["transcript"][0]["text"] == "Hi, you reached the test agent.", why(ended, d)
        assert not counter.exists()
    finally:
        _hangup_active(d)
