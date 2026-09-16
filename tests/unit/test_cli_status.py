import json
from pathlib import Path

from ai_phone.cli import main
from ai_phone.cli_status import render_last_call, render_status

STATE = {"written_at": 1.0, "brain": {"kind": "local", "base_url": None}, "active_call": None,
         "lines": {"main": {"id": "main", "kind": "trunk", "address": "+442079460000", "state": "registered", "registrar": "sip.telnyx.com",
                            "expires_at": 1700000300.0, "last_error": None, "since": 1700000000.0},
                   "agents": {"id": "agents", "kind": "sip", "address": "sip:agents@home.example.org", "state": "unregistered",
                              "registrar": None, "expires_at": None, "last_error": None, "since": 1700000000.0}}}


def test_render_status_rows():
    out = render_status(STATE, alive=True)
    assert "main" in out and "registered" in out and "+442079460000" in out and "agents" in out and "brain: local" in out


def test_render_status_tolerates_a_partial_state_file():
    """A state file written by an older version (or hand-edited) prints placeholders; status never aborts."""
    partial = {"written_at": 1.0, "lines": {"main": {"id": "main"}}, "active_call": {"call_id": "9"}}
    out = render_status(partial, alive=True)
    assert "main" in out and "active call: ? on ? with ?" in out


def test_render_status_shows_the_call_id_and_mode():
    state = dict(STATE, active_call={"call_id": 4, "call": "c7f3a2", "mode": "relay", "direction": "out",
                                     "line_id": "main", "remote": "+15550000000"})
    assert "active call: out relay call c7f3a2 on main with +15550000000" in render_status(state, alive=True)


def test_status_command_reads_state_file(tmp_path: Path, monkeypatch, capsys):
    monkeypatch.setenv("AI_PHONE_HOME", str(tmp_path))
    (tmp_path / "state.json").write_text(json.dumps(STATE))
    assert main(["status"]) == 0
    out = capsys.readouterr().out
    assert "daemon not running" in out and "registered" in out


def test_status_without_state_file(tmp_path: Path, monkeypatch, capsys):
    monkeypatch.setenv("AI_PHONE_HOME", str(tmp_path))
    assert main(["status"]) == 1
    assert "never started" in capsys.readouterr().out


def test_last_call_summary(tmp_path: Path):
    turns = tmp_path / "turns.jsonl"
    rows = [{"call_id": "6", "turn": 0, "first_audible_ms": 999.0, "rx_overruns": 0, "tx_underruns": 0, "direction": "in", "brain_error": None},
            {"call_id": "7", "turn": 0, "first_audible_ms": 900.0, "rx_overruns": 0, "tx_underruns": 0, "direction": "in", "brain_error": None},
            {"call_id": "7", "turn": 1, "first_audible_ms": 800.0, "rx_overruns": 0, "tx_underruns": 1, "direction": "in", "brain_error": "idle_timeout"}]
    turns.write_text("\n".join(json.dumps(r) for r in rows) + "\n")
    out = render_last_call(turns)
    assert "call 7" in out and "2 turns" in out and "p50" in out and "underruns: 1" in out and "brain errors: 1" in out


def test_last_call_survives_a_truncated_record(tmp_path: Path):
    turns = tmp_path / "turns.jsonl"
    turns.write_text(json.dumps({"call_id": "3", "turn": 0, "first_audible_ms": 700.0, "rx_overruns": 0, "tx_underruns": 0}) + "\n" + '{"call_id": "4", "tu')
    assert "call 3" in render_last_call(turns)


def test_last_call_groups_by_the_unique_call_id_when_records_carry_one(tmp_path: Path):
    turns = tmp_path / "turns.jsonl"
    rows = [{"call_id": "2", "call": "a1b2c3", "turn": 0, "first_audible_ms": 900.0, "brain_error": "http_503"},
            {"call_id": "2", "call": "a1b2c3", "turn": 1, "first_audible_ms": 900.0},
            {"call_id": "2", "call": "d4e5f6", "turn": 0, "first_audible_ms": 700.0}]   # PJSIP reused call ID 2
    turns.write_text("\n".join(json.dumps(r) for r in rows) + "\n")
    out = render_last_call(turns)
    assert out.startswith("call d4e5f6: 1 turns") and "p50 700 ms" in out and "brain errors: 0" in out
