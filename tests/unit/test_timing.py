import subprocess
import sys
from pathlib import Path

from ai_phone.pipeline.timing import TurnLog, TurnTimer


def _timer(base=1_000_000_000):
    t = TurnTimer(gen=1)
    ms = 1_000_000
    t.t0, t.t1, t.t2, t.t3, t.t4, t.t5 = base, base + 100 * ms, base + 250 * ms, base + 350 * ms, base + 500 * ms, base + 600 * ms
    t.stt_text, t.reply_text = "hi", "hello"
    t.worker_timings = {"llm_prompt_eval_ms": 120.0, "llm_tokens": 9}
    return t


def test_record_has_stage_ms_and_first_audible():
    r = _timer().to_record("c1", 3)
    assert r["first_audible_ms"] == 600.0 and r["stt_ms"] == 100.0 and r["llm_first_clause_ms"] == 250.0
    assert r["tts_first_chunk_ms"] == 150.0 and r["egress_ms"] == 100.0 and r["tokens"] == 9
    assert r["call_id"] == "c1" and r["turn"] == 3 and r["bargein"] is False


def test_log_appends_json_lines_and_report_prints_p50(tmp_path: Path):
    log = TurnLog(tmp_path / "turns.jsonl")
    for i in range(5):
        log.write(_timer(base=i * 10_000_000_000).to_record("c1", i))
    out = subprocess.run([sys.executable, "scripts/latency_report.py", str(tmp_path / "turns.jsonl")],
                         capture_output=True, text=True, check=True).stdout
    assert "first_audible_ms" in out and "p50=  600.0" in out and "GATE: PASS" in out
