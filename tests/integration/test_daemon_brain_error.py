"""A local OpenAI-compatible server that answers the caller's second utterance with HTTP 503: the turn is recorded as a
brain error, the apology path runs, and the call continues (the audio itself is not inspected here)."""
import json
import os
import subprocess
import threading
import time
from http.server import BaseHTTPRequestHandler, HTTPServer
from pathlib import Path

import pytest

pytestmark = pytest.mark.integration


class Brain(BaseHTTPRequestHandler):
    calls = 0

    def do_POST(self):
        Brain.calls += 1
        body = json.loads(self.rfile.read(int(self.headers.get("content-length", 0))) or b"{}")
        last = (body.get("messages") or [{}])[-1].get("content", "")
        if "fake utterance 2" in last:   # the caller's second utterance (warm-up and turn 1 succeed)
            self.send_response(503); self.end_headers(); return
        self.send_response(200); self.send_header("content-type", "text/event-stream"); self.end_headers()
        for tok in ["Hello", " there", "."]:
            self.wfile.write(f"data: {json.dumps({'choices': [{'delta': {'content': tok}}]})}\n\n".encode()); self.wfile.flush()
        self.wfile.write(b"data: [DONE]\n\n")

    def log_message(self, *a):
        pass


def test_brain_failover(tmp_path: Path, monkeypatch):
    srv = HTTPServer(("127.0.0.1", 0), Brain)
    threading.Thread(target=srv.serve_forever, daemon=True).start()
    port = srv.server_address[1]
    cfg = tmp_path / "config.yaml"
    cfg.write_text(f'brain:\n  kind: openai_compatible\n  base_url: "http://127.0.0.1:{port}/v1"\n  model: test\n'
                   f'inbound:\n  greeting: "Hi."\n  instructions: "s"\nlog:\n  dir: "{tmp_path}"\n')
    # --fake keeps STT/TTS fake; the brain comes from the config (the daemon honours brain.kind even with --fake)
    from tests.integration.test_daemon_smoke import _call_and_wait, _start_daemon, _stop_daemon  # shared helpers
    d, lines = _start_daemon(tmp_path, cfg.read_text())
    try:
        recs = _call_and_wait(d, lines, tmp_path, turns=2)   # two utterances in one call
        assert [r.get("brain_error") for r in recs[:2]] == [None, "http_503"]
        assert recs[0]["reply_text"] == "Hello there." and not any(r.get("state_at_end") == "ENDING" for r in recs)  # the call went on
    finally:
        _stop_daemon(d); srv.shutdown()
