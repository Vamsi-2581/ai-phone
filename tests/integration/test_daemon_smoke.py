import json
import math
import signal
import struct
import subprocess
import time
import wave
from pathlib import Path

import pytest

pytestmark = pytest.mark.integration


def _tone(path: Path, seconds=1.2, rate=8000, tail_silence=3.0):
    """A tone followed by silence: the endpointer needs silence after speech to close the turn."""
    n = int(seconds * rate)
    frames = b"".join(struct.pack("<h", int(12000 * math.sin(2 * math.pi * 300 * i / rate))) for i in range(n))
    frames += bytes(2 * int(tail_silence * rate))
    with wave.open(str(path), "wb") as w:
        w.setnchannels(1)
        w.setsampwidth(2)
        w.setframerate(rate)
        w.writeframes(frames)


def test_daemon_answers_and_logs_a_turn_with_fake_backends(tmp_path: Path):
    cfg = tmp_path / "config.yaml"
    cfg.write_text('sip:\n  user: "ai"\n  accept_invites_from: lan\nconversation:\n  greeting: "Hi."\n'
                   '  system_prompt: "s"\n  endpoint_silence_ms: 300\nlog:\n  dir: "%s"\n' % tmp_path)
    d = subprocess.Popen(["uv", "run", "ai-phone", "run", "--config", str(cfg), "--lan", "--fake"],
                         stdout=subprocess.PIPE, stderr=subprocess.STDOUT, text=True)
    try:
        deadline = time.time() + 60
        while time.time() < deadline:
            line = d.stdout.readline()
            if line.strip() == "ready":
                break
        else:
            raise AssertionError("daemon never became ready")
        play, rec = tmp_path / "tone.wav", tmp_path / "rec.wav"
        _tone(play)
        peer = subprocess.Popen(["bash", "scripts/pjsua_peer.sh", str(play), str(rec), "6"],
                                stdin=subprocess.PIPE, stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)
        time.sleep(8)
        try:
            peer.stdin.write(b"q\n")
            peer.stdin.flush()
            peer.wait(timeout=10)
        except Exception:
            peer.terminate()
        time.sleep(1.0)
        turns = tmp_path / "turns.jsonl"
        if not turns.exists():
            d.send_signal(signal.SIGTERM)
            out = d.stdout.read()
            raise AssertionError("no turns.jsonl written; daemon output tail:\n" + out[-3000:])
        rec_ = json.loads(turns.read_text().splitlines()[0])
        assert rec_["reply_text"] == "Hello, this is a fake reply." and rec_["first_audible_ms"] is not None
    finally:
        d.send_signal(signal.SIGTERM)
        try:
            d.wait(10)
        except subprocess.TimeoutExpired:
            d.kill()
